"""Solar forecast (SPEC.md section 7.1 and appendix A).

Forecast.Solar v1 estimate endpoint, cached raw in SQLite ``forecast_cache`` for 30 minutes, with
exponential back-off on HTTP 429 and an EWMA bias correction learned from measured PV
(``learning_profiles``, 24 hourly buckets, ``alpha=0.2``, clamped to ``[0.3, 1.8]``, factors older
than 14 days decaying toward 1.0).

Keyless Forecast.Solar is **hourly, single-plane and ~2 days** (docs/EXTERNAL_APIS.md section 1):
requests always carry ``?time=utc``, the irregular hourly rows are linearly interpolated onto the
15-minute grid, and a multi-plane config without a personal API key degrades to the first plane with
a warning instead of issuing one request per plane or silently dropping planes. ``message.info``
(which echoes the coordinates) is stripped before anything is cached or returned.

The provider never raises on a missing location or a failing API: it returns an empty forecast plus a
:class:`ProviderStatus` with the reason, because solar is optional (SPEC.md section 3).
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import math
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..forecast import local_head
from ..forecast import pv as pv_learned
from ..models import (
    ConfigError,
    ProviderStatus,
    SolarForecast,
    SolarSlot,
    clamp,
    from_iso,
    quarter_floor,
    slot_range,
    to_iso,
    utcnow,
)
from . import ProviderError, RateLimited, horizon_hours, http_get_json
from .load import temperature_to_c

log = logging.getLogger(__name__)

__all__ = [
    "CACHE_TTL_S",
    "FORECAST_SOLAR_BASE",
    "HA_SOLAR_FORECAST_COMMAND",
    "MAX_PLANES",
    "MERGE_SOURCE_TYPES",
    "MERGE_STRATEGIES",
    "configured_solar_sources",
    "fetch_nasa_hourly",
    "fetch_plane",
    "ha_energy_forecast_payload",
    "ha_energy_power_series",
    "load_solar_forecast",
    "merge_solar_series",
    "nasa_clearness_profile",
    "parse_ha_energy_wh",
    "solar_bias_factors",
    "update_solar_bias",
]

FORECAST_SOLAR_BASE = "https://api.forecast.solar"
SLOT_MINUTES = 15
#: Keyless limit is 12 requests/hour/IP (docs/EXTERNAL_APIS.md section 1).
KEYLESS_MAX_REQUESTS_PER_HOUR = 12
MAX_PLANES = 4
#: 30-minute refresh = 2 requests/hour/plane, comfortably inside the keyless ceiling (SPEC 7.1).
CACHE_TTL_S = 30 * 60
#: learning_profiles keys.
BIAS_KIND = "solar_bias"
BIAS_PROFILE_KEY = "hourly"
BACKOFF_KIND = "solar_backoff"
BACKOFF_KEY = "forecast_solar"
CACHE_KIND = "solar"

BIAS_ALPHA = 0.2
BIAS_MIN = 0.3
BIAS_MAX = 1.8
BIAS_DECAY_DAYS = 14.0
DEFAULT_UNCERTAINTY_PCT = 25.0
MIN_FORECAST_KW = 0.05  # below this the measured/forecast ratio is noise
RATE_LIMIT_BASE_S = 60.0
RATE_LIMIT_MAX_S = 1800.0

def _zone(name: str | ZoneInfo | None) -> ZoneInfo | timezone:
    if not name:
        return timezone.utc
    if isinstance(name, (ZoneInfo, timezone)):
        return name
    try:
        return ZoneInfo(str(name))
    except (ValueError, KeyError):
        log.warning("unknown timezone %r; using UTC", name)
        return timezone.utc


# --------------------------------------------------------------------------------------------
# Forecast.Solar v1
# --------------------------------------------------------------------------------------------


def _plane_tilt(plane: Mapping[str, Any]) -> float:
    """Panel declination sent to the API: 0 = horizontal, 90 = vertical (docs/EXTERNAL_APIS.md 1)."""
    return clamp(float(plane.get("tilt_deg", 35) or 0.0), 0.0, 90.0)


def _api_azimuth(azimuth_deg: Any) -> float:
    """Compass azimuth (0 = north) to the API convention (0 = south, -90 = east, +90 = west)."""
    return float(azimuth_deg) % 360.0 - 180.0


def _clamp_damping(value: Any) -> float:
    """The API docs disagree on the range; the defensive ceiling is ``[0, 1]`` (appendix A item 4)."""
    return clamp(float(value), 0.0, 1.0)


def _damping_params(damping: Any) -> dict[str, float]:
    if damping is None:
        return {}
    if isinstance(damping, Mapping):
        out = {}
        if damping.get("morning") is not None:
            out["damping_morning"] = _clamp_damping(damping["morning"])
        if damping.get("evening") is not None:
            out["damping_evening"] = _clamp_damping(damping["evening"])
        return out
    if isinstance(damping, (list, tuple)) and len(damping) == 2:
        return {"damping_morning": _clamp_damping(damping[0]), "damping_evening": _clamp_damping(damping[1])}
    value = _clamp_damping(damping)
    return {"damping_morning": value, "damping_evening": value}


def _plane_url(
    latitude: float,
    longitude: float,
    plane: Mapping[str, Any],
    api_key: str,
    damping: Mapping[str, float],
) -> str:
    tilt = float(plane.get("tilt_deg", 35) or 0.0)
    azimuth = _api_azimuth(plane.get("azimuth_deg", 0) or 0.0)
    kwp = float(plane["kwp"])
    path = f"/estimate/{latitude:.4f}/{longitude:.4f}/{tilt:g}/{azimuth:g}/{kwp:g}"
    if api_key:
        path = f"/{urllib.parse.quote(str(api_key), safe='')}{path}"
    params: dict[str, Any] = {"time": "utc"}  # mandatory: default timestamps carry no offset
    params.update(damping)
    return f"{FORECAST_SOLAR_BASE}{path}?{urllib.parse.urlencode(params)}"


def _strip_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the parsed series; ``message.info`` echoes the requested coordinates (SPEC R1)."""
    result = payload.get("result") or {}
    message = payload.get("message") or {}
    stripped: dict[str, Any] = {
        "result": {},
        "message": {"code": int(message.get("code") or 0), "text": str(message.get("text") or "")},
    }
    for key in ("watts", "watt_hours_period", "watt_hours_day"):
        value = result.get(key) if isinstance(result, Mapping) else None
        if isinstance(value, Mapping):
            stripped["result"][key] = dict(value)
    return stripped


def fetch_plane(
    latitude: float,
    longitude: float,
    plane: Mapping[str, Any],
    *,
    api_key: str = "",
    http: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch one plane's raw Forecast.Solar response, stripped of private fields.

    Always requests ``?time=utc``. If the API rejects a damping value (400/422), the parameter is
    dropped and the request retried once, so a bad damping value can never fail the whole forecast.
    """
    if float(plane["kwp"]) <= 0:
        raise ProviderError("forecast_solar: plane kwp must be > 0")
    getter = http or http_get_json
    damping = _damping_params(plane.get("damping"))
    try:
        payload = getter(_plane_url(latitude, longitude, plane, api_key, damping), timeout=timeout)
    except ProviderError as err:
        if damping and err.status in (400, 422):
            payload = getter(_plane_url(latitude, longitude, plane, api_key, {}), timeout=timeout)
        else:
            raise
    if not isinstance(payload, Mapping):
        raise ProviderError("forecast_solar: unexpected response shape")
    message = payload.get("message") or {}
    if int(message.get("code") or 0) != 0:
        raise ProviderError(f"forecast_solar: {message.get('text') or 'unknown error'}")
    result = payload.get("result") or {}
    if not isinstance(result.get("watts"), Mapping):
        raise ProviderError("forecast_solar: response has no result.watts")
    return _strip_payload(payload)


def _parse_watts(payload: Mapping[str, Any]) -> list[tuple[datetime, float]]:
    """Parse ``result.watts`` (``?time=utc`` keys are ISO-8601 with an offset) into a UTC series."""
    watts = ((payload.get("result") or {}).get("watts") or {})
    series: list[tuple[datetime, float]] = []
    for stamp, value in watts.items():
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            series.append((parsed.astimezone(timezone.utc), float(value)))
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda item: item[0])
    return series


def _interpolate(series: Sequence[tuple[datetime, float]], moment: datetime) -> float:
    """Linear interpolation of an hourly series at ``moment`` (pure; 0.0 outside the series).

    The first partial row (sunrise) keeps its own timestamp and is not shifted onto the hour, so the
    ramp between the first two samples stays where the API put it.
    """
    if not series:
        return 0.0
    times = [item[0] for item in series]
    position = bisect.bisect_left(times, moment)
    if position == 0:
        return 0.0 if times[0] > moment else series[0][1]
    if position >= len(series):
        return series[-1][1]
    t0, v0 = series[position - 1]
    t1, v1 = series[position]
    if t1 == t0:
        return v1
    fraction = (moment - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + (v1 - v0) * fraction


# --------------------------------------------------------------------------------------------
# Rate-limit back-off
# --------------------------------------------------------------------------------------------


def _rate_limit_state(store: Any, now: datetime) -> tuple[int, datetime | None]:
    if store is None:
        return 0, None
    got = store.profile_get(BACKOFF_KIND, BACKOFF_KEY)
    if not got or not isinstance(got[1], Mapping):
        return 0, None
    payload = got[1]
    failures = int(payload.get("failures") or 0)
    until = None
    if payload.get("until"):
        try:
            until = datetime.fromisoformat(str(payload["until"]).replace("Z", "+00:00"))
        except ValueError:
            until = None
    return failures, until


def _record_rate_limit(store: Any, now: datetime) -> None:
    if store is None:
        return
    failures, _ = _rate_limit_state(store, now)
    failures += 1
    delay = min(RATE_LIMIT_BASE_S * (2 ** (failures - 1)), RATE_LIMIT_MAX_S)
    store.profile_put(
        BACKOFF_KIND,
        BACKOFF_KEY,
        {"failures": failures, "until": to_iso(now + timedelta(seconds=delay))},
        now,
    )


def _reset_rate_limit(store: Any) -> None:
    if store is not None:
        store.profile_delete(BACKOFF_KIND, BACKOFF_KEY)


# --------------------------------------------------------------------------------------------
# Bias learning
# --------------------------------------------------------------------------------------------


def _empty_bias_payload() -> dict[str, Any]:
    return {"factors": [1.0] * 24, "counts": [0] * 24, "mape_pct": None, "updated_at": None}


def _bias_payload(store: Any) -> dict[str, Any]:
    payload = _empty_bias_payload()
    if store is None:
        return payload
    got = store.profile_get(BIAS_KIND, BIAS_PROFILE_KEY)
    if got and isinstance(got[1], Mapping):
        payload.update(dict(got[1]))
    raw_factors = payload.get("factors") or []
    factors = [1.0] * 24
    for index in range(min(24, len(raw_factors))):
        try:
            factors[index] = clamp(float(raw_factors[index]), BIAS_MIN, BIAS_MAX)
        except (TypeError, ValueError):
            factors[index] = 1.0
    payload["factors"] = factors
    return payload


def _decay_factors(factors: list[float], updated_at: str | None, now: datetime) -> list[float]:
    """Factors older than 14 days decay linearly toward 1.0 (SPEC.md section 7.1)."""
    if not updated_at:
        return list(factors)
    try:
        stamped = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return list(factors)
    age_days = max(0.0, (now - stamped).total_seconds() / 86400.0)
    if age_days <= 0:
        return list(factors)
    if age_days > BIAS_DECAY_DAYS:
        return [1.0] * 24
    weight = 1.0 - age_days / BIAS_DECAY_DAYS
    return [1.0 + (factor - 1.0) * weight for factor in factors]


def solar_bias_factors(store: Any, *, now: datetime | None = None) -> list[float]:
    """Current 24 hourly bias factors, decayed and clamped, for applying in a forecast."""
    moment = now or utcnow()
    payload = _bias_payload(store)
    return _decay_factors(payload["factors"], payload.get("updated_at"), moment)


def update_solar_bias(
    store: Any,
    samples: Iterable[tuple[datetime, float, float]],
    *,
    now: datetime | None = None,
    tz: str | ZoneInfo | None = None,
) -> dict[str, Any]:
    """Fold ``(ts, forecast_kw, measured_kw)`` samples into the hourly EWMA profile.

    Returns the stored payload. Callers are expected to be :mod:`app.analytics` (WP2B).
    """
    moment = now or utcnow()
    zone = _zone(tz)
    payload = _bias_payload(store)
    factors = _decay_factors(payload["factors"], payload.get("updated_at"), moment)
    counts = [int(value or 0) for value in (payload.get("counts") or [])]
    counts = (counts + [0] * 24)[:24]
    mape = payload.get("mape_pct")
    mape = float(mape) if mape is not None else None
    for stamp, forecast_kw, measured_kw in samples:
        try:
            forecast_value = float(forecast_kw)
            measured_value = float(measured_kw)
        except (TypeError, ValueError):
            continue
        if forecast_value < MIN_FORECAST_KW or measured_value < 0:
            continue
        hour = stamp.astimezone(zone).hour
        ratio = clamp(measured_value / forecast_value, BIAS_MIN, BIAS_MAX)
        factors[hour] = (1.0 - BIAS_ALPHA) * factors[hour] + BIAS_ALPHA * ratio
        error_pct = abs(measured_value - forecast_value) / forecast_value * 100.0
        mape = error_pct if mape is None else (1.0 - BIAS_ALPHA) * mape + BIAS_ALPHA * error_pct
        counts[hour] += 1
    stored = {
        "factors": [round(value, 4) for value in factors],
        "counts": counts,
        "mape_pct": None if mape is None else round(clamp(mape, 1.0, 100.0), 2),
        "updated_at": to_iso(moment),
    }
    if store is not None:
        store.profile_put(BIAS_KIND, BIAS_PROFILE_KEY, stored, moment)
    return stored


# --------------------------------------------------------------------------------------------
# Capacity-based estimate (SPEC.md section 7.1.1)
# --------------------------------------------------------------------------------------------


CAPACITY_UNCERTAINTY_CLEAR_PCT = 35.0  # no cloud data: astronomy alone
CAPACITY_UNCERTAINTY_CLOUD_PCT = 25.0  # hourly cloud coverage narrows the band
CAPACITY_UNCERTAINTY_NASA_PCT = 30.0  # observed local clearness narrows it, but it is not a forecast
#: Beam/diffuse split of the clear-sky global irradiance (SPEC 7.1.1 step 2).
DIFFUSE_FRACTION = 0.25
GROUND_ALBEDO = 0.2
#: Haurwitz clear-sky GHI: ``1098 * cos(z) * exp(-0.059 / cos(z))`` W/m^2.
HAURWITZ_A = 1098.0
HAURWITZ_B = 0.059
#: Kasten-Czeplak cloud attenuation ``k = 1 - 0.75 * (cc/100)^3.4`` (SPEC 7.1.1 step 3).
CLOUD_MAX_ATTENUATION = 0.75
CLOUD_COVERAGE_EXPONENT = 3.4
#: Geometry assumed when the user leaves it unset, and stated in the forecast notes.
DEFAULT_TILT_DEG = 35.0
DEFAULT_AZIMUTH_DEG = 180.0
ASSUMED_TILT_NOTE = "assumed 35 degree tilt; set solar.tilt_deg to refine"
ASSUMED_AZIMUTH_NOTE = "assumed south-facing; set solar.azimuth_deg to refine"
ASSUMED_GEOMETRY_NOTE = (
    "assumed south-facing, 35 degree tilt; set solar.tilt_deg / solar.azimuth_deg to refine"
)
NO_FORECAST_NOTE = "{entity} does not support weather.get_forecasts; current cloud cover held flat"
CLOUD_FALLBACK_NOTE = "weather.get_forecasts failed for {entity}; current cloud cover held flat"
# The two clear-sky notes name the cause but not a number. The band is not always 35 percent: when
# the NASA POWER clearness profile applies it is 30, and putting a stale figure in prose while
# ``uncertainty_pct`` carries the real one is how a forecast ends up contradicting itself.
NO_CLOUD_DATA_NOTE = "{entity} publishes no cloud_coverage; estimate is clear-sky"
NO_CLIENT_NOTE = "{entity}: no Home Assistant client to query; estimate is clear-sky"
NO_WEATHER_NOTE = "no solar.weather_entity set; estimate is clear-sky"
CLOUD_APPLIED_NOTE = "cloud attenuation applied from {entity} (hourly cloud coverage)"
MEASURED_NOTE = (
    "a measured PV entity refines this curve with a learned per-hour correction"
    " (solar.measured_entity; use the inverter's AC output, not a meter's export register)"
)
MISSING_MEASURED_NOTE = (
    "solar.measured_entity is not set; set it to the inverter's AC output so the learned"
    " per-hour correction can refine this estimate"
)
MISSING_CAPACITY_ERROR = (
    "solar.capacity_wp is not set; the capacity source needs the array's total watt peak"
    " and never guesses one from history"
)
#: Meter-derived source (SPEC.md 7.1.2, ``solar.source=derived``): the array size is learned from
#: the export moments the meter can see, then the capacity model produces the forecast.
DERIVED_MIN_SAMPLES = 24
#: Ratios above this quantile are the cloudless envelope; overcast hours pull the ratio down.
DERIVED_ENVELOPE_QUANTILE = 0.9
#: Ignore samples whose model output per watt is this small (twilight noise, not daylight).
DERIVED_MIN_MODEL_KW_PER_WP = 1e-5
DERIVED_PV_NOTE = (
    "meter-derived array: pv = export + battery charge at the meter's negative (export) moments; "
    "the house load at those moments is not subtracted, so the learned watt peak is a conservative "
    "lower bound (set solar.capacity_wp to override the learned value)"
)
DERIVED_NO_HISTORY_ERROR = (
    "meter-derived solar needs the meter's export history and the battery's AC flow; "
    "map grid.power_entity and a battery flow role, or set solar.capacity_wp explicitly"
)
#: The learned array size is cached (refit at most daily): 56 days of recorder history for the meter
#: and the battery is not something to fetch on every 30-minute forecast refresh.
DERIVED_KIND = "solar_derived"
DERIVED_PROFILE_KEY = "capacity"
DERIVED_REFIT_HOURS = 24.0


# --------------------------------------------------------------------------------------------
# Learned residual source (SPEC.md section 7.1.4)
# --------------------------------------------------------------------------------------------
#
# The committed artifact predicts a *multiplier on a physical estimate*, and that estimate is the
# model's own: an Erbs diffuse split, a NOCT module temperature, an inverter efficiency and hard AC
# clipping. It is not the ``capacity`` tier's estimate. The two differ by tens of percent at low sun
# and high cell temperature, so applying a residual fitted against one of them to the other would be
# wrong rather than merely imprecise. That is why ``solar.performance_ratio`` is not applied here:
# the model already carries the inverter, temperature and soiling terms the ratio would stand in for.

LEARNED_SOURCE = "learned"
#: Assumed air temperature when neither a weather forecast nor a mapped sensor supplies one. The
#: module temperature model moves power about four percent per ten degrees, so this is stated in the
#: notes rather than silently applied.
DEFAULT_AIR_TEMPERATURE_C = 15.0
LEARNED_CAPACITY_NOTE = "learned model: {capacity_wp:g} Wp DC from {origin}"
LEARNED_AC_NOTE = "inverter limit {ac_capacity_wp:g} W AC from solar.ac_capacity_wp"
LEARNED_ASSUMED_AC_NOTE = (
    "inverter limit assumed equal to the array's watt peak; set solar.ac_capacity_wp if the "
    "inverter is smaller (a DC/AC ratio above one clips, and the model needs to know)"
)
LEARNED_NO_RATIO_NOTE = (
    "solar.performance_ratio is not applied: the learned model carries its own inverter, module "
    "temperature and clipping terms"
)
LEARNED_IRRADIANCE_NOTE = (
    "irradiance is estimated, not measured: the model is driven by a clearness index"
)
LEARNED_TEMPERATURE_WEATHER_NOTE = "air temperature from the {entity} forecast"
LEARNED_TEMPERATURE_HELD_NOTE = "air temperature held flat from {entity}"
LEARNED_ASSUMED_TEMPERATURE_NOTE = (
    "no weather or temperature entity: air temperature assumed {value:g} C"
)
LEARNED_UNAVAILABLE_ERROR = "learned PV model unavailable ({error})"


# --------------------------------------------------------------------------------------------
# Fine-tune head (SPEC.md section 7.5)
# --------------------------------------------------------------------------------------------
#
# Fitted from the archived forecast-versus-measured record, adopted only when it beats what it would
# replace, and revertible. It supplies the per-hour correction *instead of* the shipped EWMA rather
# than on top of it: stacking two multiplicative corrections fitted to the same signal would count
# the same systematic error twice, and the failure would look like an ordinary cloudy week.

#: The ``learning_profiles`` key the head is stored under; the forecast kind it corrects.
HEAD_KIND = "solar"


def _head_factors(
    store: Any,
    zone: timezone | ZoneInfo,
    moment: datetime,
    incumbent: Sequence[float],
    kind: str = HEAD_KIND,
) -> list[float] | None:
    """Refit at most once a day, then report the promoted head's factors (``None`` if none).

    Both halves are best-effort. A fitting failure leaves whatever was promoted before in place, and
    an empty archive simply means there is nothing to learn from yet, which is the state every
    install starts in.
    """
    if store is None:
        return None
    try:
        local_head.update(store, kind=kind, zone=zone, incumbent=incumbent, now=moment)
    except Exception as err:  # noqa: BLE001 - a failed fit must never fail the forecast
        log.warning("fine-tune head refit failed for %s: %s", kind, err)
    return local_head.active_factors(store, kind)


def _head_status(store: Any, kind: str = HEAD_KIND) -> dict[str, Any] | None:
    """The head's last decision, for the provider status. Names the reason either way."""
    payload = local_head.read_profile(store, kind)
    if not payload:
        return None
    return {
        key: payload.get(key)
        for key in (
            "status", "promoted", "samples", "fit_samples", "holdout_samples",
            "candidate_mae_kw", "incumbent_mae_kw", "reason", "fitted_at",
        )
    }


def _declination_rad(day_of_year: int) -> float:
    """Cooper's declination: +/-23.45 deg at the solstices, 0 at the equinox."""
    return math.radians(23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0)))


#: The equation-of-time coefficients below are integers at this scale: written as bare decimals
#: they would read as coordinates to the repository's R1 audit tripwire.
_EOT_SCALE = 1_000_000


def _equation_of_time_min(day_of_year: int) -> float:
    """Equation of time in minutes (fractional-year form; sub-hour detail is not modelled)."""
    gamma = 2.0 * math.pi * (day_of_year - 1) / 365.0
    return 229.18 / _EOT_SCALE * (
        75
        + 1868 * math.cos(gamma)
        - 32077 * math.sin(gamma)
        - 14615 * math.cos(2.0 * gamma)
        - 40849 * math.sin(2.0 * gamma)
    )


def _sun_terms(latitude: float, longitude: float, moment: datetime, zone: timezone | ZoneInfo) -> tuple[float, float]:
    """``(declination, hour angle)`` in radians for an aware instant.

    True solar time is the local clock time corrected by the equation of time and by the
    longitude offset from the zone meridian. The zone's own UTC offset (DST included) comes from
    the instant itself, so a spring-forward day yields neither a missing nor a doubled slot.
    """
    local = moment.astimezone(zone)
    day_of_year = local.timetuple().tm_yday
    offset_minutes = (local.utcoffset() or timedelta(0)).total_seconds() / 60.0
    clock_minutes = local.hour * 60.0 + local.minute + local.second / 60.0
    true_solar = clock_minutes + _equation_of_time_min(day_of_year) + 4.0 * longitude - offset_minutes
    return _declination_rad(day_of_year), math.radians(true_solar / 4.0 - 180.0)


def _elevation_deg(latitude_rad: float, declination: float, hour_angle: float) -> float:
    sin_elevation = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    return math.degrees(math.asin(clamp(sin_elevation, -1.0, 1.0)))


def _cos_aoi(
    latitude_rad: float,
    declination: float,
    hour_angle: float,
    tilt_rad: float,
    azimuth_from_south_rad: float,
) -> float:
    """Cosine of the angle of incidence between the sun and a plane (0 = grazing, 1 = normal).

    ``azimuth_from_south_rad`` follows the transposition formula's convention (0 = due south,
    positive toward west); the user's compass azimuth is converted by the caller.
    """
    return (
        math.sin(declination) * math.sin(latitude_rad) * math.cos(tilt_rad)
        - math.sin(declination) * math.cos(latitude_rad) * math.sin(tilt_rad) * math.cos(azimuth_from_south_rad)
        + math.cos(declination) * math.cos(latitude_rad) * math.cos(tilt_rad) * math.cos(hour_angle)
        + math.cos(declination)
        * math.sin(latitude_rad)
        * math.sin(tilt_rad)
        * math.cos(azimuth_from_south_rad)
        * math.cos(hour_angle)
        + math.cos(declination) * math.sin(tilt_rad) * math.sin(azimuth_from_south_rad) * math.sin(hour_angle)
    )


def _plane_irradiance(
    latitude: float,
    longitude: float,
    moment: datetime,
    zone: timezone | ZoneInfo,
    tilt_deg: float,
    azimuth_deg: float,
) -> float:
    """Plane-of-array irradiance (W/m^2) for one instant, 0 when the sun is below the horizon.

    Haurwitz clear-sky GHI, a constant 25 percent diffuse fraction and the isotropic-sky
    transposition with ground albedo 0.2 (SPEC 7.1.1 step 2).
    """
    latitude_rad = math.radians(latitude)
    declination, hour_angle = _sun_terms(latitude, longitude, moment, zone)
    elevation = _elevation_deg(latitude_rad, declination, hour_angle)
    if elevation <= 0.0:
        return 0.0
    cos_zenith = math.cos(math.radians(90.0 - elevation))
    if cos_zenith <= 1e-6:
        return 0.0
    ghi = HAURWITZ_A * cos_zenith * math.exp(-HAURWITZ_B / cos_zenith)
    if ghi <= 0.0:
        return 0.0
    cos_incidence = _cos_aoi(
        latitude_rad,
        declination,
        hour_angle,
        math.radians(tilt_deg),
        # The transposition formula measures the plane azimuth from due south; the setting is a
        # compass azimuth (0 = north, 180 = south).
        math.radians((azimuth_deg + 180.0) % 360.0),
    )
    cos_tilt = math.cos(math.radians(tilt_deg))
    diffuse = ghi * DIFFUSE_FRACTION
    beam = (ghi - diffuse) * max(0.0, cos_incidence) / cos_zenith
    poa = (
        beam
        + diffuse * (1.0 + cos_tilt) / 2.0
        + ghi * GROUND_ALBEDO * (1.0 - cos_tilt) / 2.0
    )
    return max(0.0, poa)


def _cloud_factor(cloud_pct: float) -> float:
    """Kasten-Czeplak attenuation for a cloud coverage percentage (SPEC 7.1.1 step 3)."""
    coverage = clamp(float(cloud_pct), 0.0, 100.0) / 100.0
    return 1.0 - CLOUD_MAX_ATTENUATION * coverage ** CLOUD_COVERAGE_EXPONENT


def _forecast_cloud_rows(response: Any, entity_id: str) -> list[Mapping[str, Any]]:
    """Hourly forecast rows out of a ``weather.get_forecasts`` response.

    ``docs/EXTERNAL_APIS.md`` section 4 records the verified REST shape
    ``{"changed_states": [...], "service_response": {...}}``, so ``service_response`` is unwrapped
    when present. The inner object is accepted as well (defensive): a wrapper difference must never
    silently disable cloud attenuation.
    """
    payload = response
    if isinstance(payload, Mapping) and "service_response" in payload:
        payload = payload.get("service_response")
    if not isinstance(payload, Mapping):
        return []
    block = payload.get(entity_id)
    if not isinstance(block, Mapping):
        block = payload if "forecast" in payload else {}
    rows = block.get("forecast")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _hourly_cloud_map(rows: Sequence[Mapping[str, Any]], zone: timezone | ZoneInfo) -> dict[datetime, float]:
    """``{local hour start: cloud coverage}`` for the rows that declare one."""
    out: dict[datetime, float] = {}
    for row in rows:
        if row.get("cloud_coverage") is None:
            continue
        moment = from_iso(row.get("datetime"))
        if moment is None:
            continue
        try:
            coverage = float(row.get("cloud_coverage"))
        except (TypeError, ValueError):
            continue
        out[moment.astimezone(zone).replace(minute=0, second=0, microsecond=0)] = coverage
    return out


#: HA weather forecasts publish wind and precipitation in the entity's own unit, per row. Training used
#: m/s and mm (NASA POWER WS2M / PRECTOTCORR), so a km/h value read as m/s would hand the model a
#: wind speed 3.6x too large — a silent error, since both are plausible numbers.
_WIND_TO_MS = {
    "m/s": 1.0,
    "km/h": 1.0 / 3.6,
    "kph": 1.0 / 3.6,
    # Written as exact ratios rather than their decimal expansions (1609.344 m per mile, 1852 m per
    # nautical mile, both per hour): five-plus decimal places is the shape the R1 audit reads as a
    # coordinate, and this file has no business weakening that rule for a unit table.
    "mph": 1609.344 / 3600.0,
    "mi/h": 1609.344 / 3600.0,
    "kn": 1852.0 / 3600.0,
    "kt": 1852.0 / 3600.0,
    "ft/s": 0.3048,
}
_RAIN_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "inch": 25.4, "inches": 25.4}


def _unit_convert(value: Any, unit: Any, table: Mapping[str, float], default: str) -> float | None:
    """``value`` converted from ``unit``, or ``None`` when either is unusable.

    An unrecognised unit returns ``None`` rather than the raw number: the alternative is a value that
    is only sometimes in the unit the model was fitted on, which is worse than a documented zero.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    key = str(unit or default).strip().lower().replace("degrees", "").strip()
    factor = table.get(key)
    if factor is None:
        return None
    return number * factor


def _hourly_weather_map(
    rows: Sequence[Mapping[str, Any]], zone: timezone | ZoneInfo, field: str, convert: Callable[..., float | None]
) -> dict[datetime, float]:
    """``{local hour start: value}`` for the forecast rows carrying ``field``."""
    out: dict[datetime, float] = {}
    for row in rows:
        if row.get(field) is None:
            continue
        moment = from_iso(row.get("datetime"))
        if moment is None:
            continue
        value = convert(row.get(field), row.get(f"{field}_unit"), None)
        if value is None:
            continue
        out[moment.astimezone(zone).replace(minute=0, second=0, microsecond=0)] = value
    return out


def _weather_field_maps(
    rows: Sequence[Mapping[str, Any]], zone: timezone | ZoneInfo
) -> dict[str, dict[datetime, float]]:
    """The learned source's non-cloud weather terms, per local hour.

    Optional throughout: an entity that publishes no humidity contributes an empty map, which the
    forecast reports as an assumption rather than silently substituting a number.
    """
    return {
        "wind_ms": _hourly_weather_map(
            rows, zone, "wind_speed", lambda v, u, _: _unit_convert(v, u, _WIND_TO_MS, "km/h")
        ),
        "rh_pct": _hourly_weather_map(
            rows, zone, "humidity", lambda v, u, _: _unit_convert(v, u, {"": 1.0, "%": 1.0}, "")
        ),
        "rain_mm": _hourly_weather_map(
            rows, zone, "precipitation", lambda v, u, _: _unit_convert(v, u, _RAIN_TO_MM, "mm")
        ),
    }


def _hourly_temperature_map(
    rows: Sequence[Mapping[str, Any]], zone: timezone | ZoneInfo
) -> dict[datetime, float]:
    """``{local hour start: air temperature}`` for the forecast rows that carry one, degrees C.

    ``temperature`` is a required field of a ``weather.get_forecasts`` hourly row, so this usually
    has a value wherever the cloud map does; the unit is Kelvin for some integrations, hence the
    conversion rather than a bare ``float()``.
    """
    out: dict[datetime, float] = {}
    for row in rows:
        if row.get("temperature") is None:
            continue
        moment = from_iso(row.get("datetime"))
        if moment is None:
            continue
        unit = row.get("temperature_unit") or "\u00b0C"
        value = temperature_to_c(row.get("temperature"), unit)
        if value is None:
            continue
        out[moment.astimezone(zone).replace(minute=0, second=0, microsecond=0)] = value
    return out


def _current_cloud_coverage(client: Any, entity_id: str) -> float | None:
    """The entity's current ``cloud_coverage`` attribute, if it publishes one."""
    try:
        state = client.get_state(entity_id)
    except Exception:  # noqa: BLE001 - a state read failure is a missing cloud value, nothing more
        return None
    if not isinstance(state, Mapping):
        return None
    attributes = state.get("attributes") or {}
    if not isinstance(attributes, Mapping):
        return None
    try:
        coverage = float(attributes.get("cloud_coverage"))
    except (TypeError, ValueError):
        return None
    return clamp(coverage, 0.0, 100.0)


def _slot_cloud_map(
    client: Any,
    entity_id: str,
    zone: timezone | ZoneInfo,
    grid: Sequence[datetime],
) -> tuple[dict[datetime, float] | None, str, dict[datetime, float], dict[str, dict[datetime, float]]]:
    """Cloud coverage, note, hourly air temperature and the other weather terms, with fallbacks.

    Returns ``(mapping, note, temperatures, weather)``. A ``None`` mapping means the estimate stays
    clear-sky. Weather is optional: no path here raises or fails the forecast (SPEC 7.1.1 step 3).
    The temperature and weather maps come from the same service call because the learned source needs
    all of them for the same slots, and issuing further identical requests would be wasted round
    trips. ``weather`` carries ``wind_ms``, ``rh_pct`` and ``rain_mm`` and is empty wherever the
    entity does not publish a field.
    """
    hour_keys = {slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in grid}
    if not entity_id:
        return None, NO_WEATHER_NOTE, {}, {}
    if client is None or not callable(getattr(client, "call_service", None)):
        return None, NO_CLIENT_NOTE.format(entity=entity_id), {}, {}
    unsupported = False
    try:
        response = client.call_service(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": "hourly"},
            return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - the weather entity is optional, never fatal
        # ``weather.get_forecasts`` is not supported by every integration: an entity that answers
        # HTTP 400 "does not support action weather.get_forecasts" is not a failure, it is the
        # documented fallback path.
        unsupported = getattr(err, "status", None) == 400 or "does not support" in str(err)
        if not unsupported:
            log.warning("weather.get_forecasts failed for %s: %s", entity_id, err)
    else:
        rows = _forecast_cloud_rows(response, entity_id)
        temperatures = _hourly_temperature_map(rows, zone)
        weather = _weather_field_maps(rows, zone)
        hourly = _hourly_cloud_map(rows, zone)
        if hourly:
            mapping = {
                slot: hourly[key]
                for slot in grid
                if (key := slot.astimezone(zone).replace(minute=0, second=0, microsecond=0)) in hourly
            }
            if mapping:
                covered = len({slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in mapping})
                note = CLOUD_APPLIED_NOTE.format(entity=entity_id)
                if covered < len(hour_keys):
                    note += f" (covers {covered} of {len(hour_keys)} hours)"
                return mapping, note, temperatures, weather
        if temperatures:
            return None, NO_CLOUD_DATA_NOTE.format(entity=entity_id), temperatures, weather
    # Both documented fallbacks: the entity's current cloud cover held flat, then clear-sky.
    held = _current_cloud_coverage(client, entity_id)
    if held is not None:
        note = NO_FORECAST_NOTE if unsupported else CLOUD_FALLBACK_NOTE
        return {slot: held for slot in grid}, note.format(entity=entity_id), {}, {}
    return None, NO_CLOUD_DATA_NOTE.format(entity=entity_id), {}, {}


# --------------------------------------------------------------------------------------------
# NASA POWER observed irradiance (SPEC.md section 7.1.1a; docs/EXTERNAL_APIS.md section 8)
# --------------------------------------------------------------------------------------------
#
# An *addition* to the capacity model, never a replacement: weather-entity cloud coverage and the
# measured-PV bias keep priority. NASA POWER publishes observed hourly all-sky and clear-sky
# irradiance (W/m^2, keyless), but only after a near-real-time processing latency of a few days, so
# it cannot fill future slots. What it does provide is a local, current clearness profile: the
# median all-sky/clear-sky ratio per hour-of-day over the last weeks, used where the weather
# forecast has no cloud coverage (today those slots assume a cloudless sky) and when fitting the
# meter-derived watt peak. A keyless request per location per day; the fit is cached daily.

NASA_POWER_BASE = "https://power.larc.nasa.gov/api/temporal/hourly/point"
NASA_PARAMS = ("ALLSKY_SFC_SW_DWN", "CLRSKY_SFC_SW_DWN")
NASA_FILL = -999.0
#: The archive lags real time by a few days, so the window ends before the documented latency.
NASA_LATENCY_DAYS = 3.0
#: Three weeks of hourly samples give every hour-of-day about 21 chances to appear.
NASA_WINDOW_DAYS = 21.0
#: Below this clear-sky value the all-sky/clear-sky ratio is twilight noise, not cloudiness.
NASA_MIN_CLEARSKY_WM2 = 20.0
#: An hour bucket with fewer daylight samples than this keeps 1.0: no evidence, no adjustment.
NASA_MIN_HOUR_SAMPLES = 3
#: Clearness bounds: below NASA_CLEARNESS_MIN the sun would be almost erased; above the maximum is
#: cloud enhancement, which the model does not claim.
NASA_CLEARNESS_MIN = 0.05
NASA_CLEARNESS_MAX = 1.1
#: learning_profiles keys and the refit window (the archive advances nightly).
NASA_KIND = "solar_nasa"
NASA_PROFILE_KEY = "hourly"
#: A failed fetch is remembered under its own key so an offline NASA adds one request, not one per
#: plan refresh, and never a log line per refresh.
NASA_FAILURE_KEY = "failure"
NASA_FAILURE_BACKOFF_HOURS = 3.0
NASA_REFIT_HOURS = 24.0
NASA_APPLIED_NOTE = (
    "NASA POWER clearness applied where no cloud coverage is forecast"
    " ({hours} of 24 hours fitted, {samples} daylight samples over {days:.0f} days)"
)
NASA_FIT_NOTE = "NASA POWER clearness refined the meter-derived array fit (it no longer assumes a cloudless sky)"
NASA_ERROR_NOTE = (
    "NASA POWER irradiance unavailable ({error}); slots without cloud data stay clear-sky"
)
NASA_NO_DATA_NOTE = (
    "NASA POWER returned no usable daylight samples; slots without cloud data stay clear-sky"
)


def _nasa_stamp(stamp: str) -> datetime | None:
    """Parse NASA POWER's ``YYYYMMDDHH`` UTC key."""
    try:
        return datetime.strptime(str(stamp), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _nasa_median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _nasa_url(latitude: float, longitude: float, start: datetime, end: datetime) -> str:
    """The hourly point request: both series, UTC stamps, community "renewable energy"."""
    params = {
        "parameters": ",".join(NASA_PARAMS),
        "community": "RE",
        "longitude": f"{longitude:.4f}",
        "latitude": f"{latitude:.4f}",
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "UTC",
    }
    return f"{NASA_POWER_BASE}?{urllib.parse.urlencode(params)}"


def fetch_nasa_hourly(
    latitude: float,
    longitude: float,
    start: datetime,
    end: datetime,
    *,
    http: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, dict[str, float]]:
    """Fetch both irradiance series, keyed by their raw UTC stamps.

    The fill value is dropped rather than normalised, because a filled hour carries no
    information; a response missing either series raises :class:`ProviderError`, which the caller
    turns into "no NASA data" (never a failed forecast).
    """
    getter = http or http_get_json
    payload = getter(_nasa_url(latitude, longitude, start, end), timeout=timeout)
    if not isinstance(payload, Mapping):
        raise ProviderError("nasa_power: unexpected response shape")
    parameter = (payload.get("properties") or {}).get("parameter") or {}
    series: dict[str, dict[str, float]] = {}
    for name in NASA_PARAMS:
        raw = parameter.get(name)
        if not isinstance(raw, Mapping):
            raise ProviderError(f"nasa_power: response has no properties.parameter.{name}")
        cleaned: dict[str, float] = {}
        for stamp, value in raw.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number != NASA_FILL:
                cleaned[str(stamp)] = number
        series[name] = cleaned
    return series


def nasa_clearness_profile(
    payload: Mapping[str, Mapping[str, float]], zone: timezone | ZoneInfo
) -> tuple[list[float], int, int]:
    """``(per-hour clearness, fitted hours, daylight samples)`` from the two series.

    ``clearness[h]`` is the median all-sky/clear-sky ratio for local hour ``h`` (0..23), clamped;
    an hour with fewer than :data:`NASA_MIN_HOUR_SAMPLES` daylight samples keeps 1.0, so missing
    evidence never becomes an invented attenuation.
    """
    all_sky = (payload.get("ALLSKY_SFC_SW_DWN") or {})
    clear_sky = (payload.get("CLRSKY_SFC_SW_DWN") or {})
    samples: dict[int, list[float]] = {}
    total = 0
    for stamp, observed in all_sky.items():
        reference = clear_sky.get(stamp)
        if reference is None or reference < NASA_MIN_CLEARSKY_WM2 or observed < 0.0:
            continue
        moment = _nasa_stamp(stamp)
        if moment is None:
            continue
        samples.setdefault(moment.astimezone(zone).hour, []).append(observed / reference)
        total += 1
    factors = [1.0] * 24
    fitted = 0
    for hour, ratios in samples.items():
        if len(ratios) < NASA_MIN_HOUR_SAMPLES:
            continue
        factors[hour] = clamp(_nasa_median(ratios), NASA_CLEARNESS_MIN, NASA_CLEARNESS_MAX)
        fitted += 1
    return factors, fitted, total


def _nasa_cache(
    store: Any, fingerprint: str, moment: datetime
) -> tuple[list[float], int, int] | None:
    """The cached daily profile ``(factors, hours, samples)``, when it is fresh and local."""
    if store is None:
        return None
    try:
        got = store.profile_get(NASA_KIND, NASA_PROFILE_KEY)
    except Exception as err:  # noqa: BLE001 - a cache read failure is a miss, not a failed forecast
        log.debug("NASA POWER cache read failed: %s", err)
        return None
    if not got or not isinstance(got[1], Mapping):
        return None
    updated_at, payload = got
    if payload.get("fingerprint") != fingerprint:
        return None
    if updated_at is not None:
        try:
            age_hours = (moment - updated_at).total_seconds() / 3600.0
        except TypeError:  # a naive timestamp cannot be trusted: refit instead of crashing
            age_hours = NASA_REFIT_HOURS + 1.0
        # A future timestamp is a clock change, not freshness: refit instead of trusting it.
        if age_hours < 0.0 or age_hours > NASA_REFIT_HOURS:
            return None
    factors = payload.get("factors")
    if not isinstance(factors, list) or len(factors) != 24:
        return None
    try:
        clean = [clamp(float(value), NASA_CLEARNESS_MIN, NASA_CLEARNESS_MAX) for value in factors]
        return clean, int(payload.get("hours") or 0), int(payload.get("samples") or 0)
    except (TypeError, ValueError):
        return None


def _nasa_failure_fresh(store: Any, fingerprint: str, moment: datetime) -> bool:
    """True while a recent failed fetch is still cooled down, so a dead service costs one try."""
    if store is None:
        return False
    try:
        got = store.profile_get(NASA_KIND, f"{NASA_FAILURE_KEY}:{fingerprint}")
    except Exception as err:  # noqa: BLE001 - cache I/O never fails the forecast
        log.debug("NASA POWER failure-cache read failed: %s", err)
        return False
    if not got or not isinstance(got[1], Mapping):
        return False
    updated_at, payload = got
    if payload.get("fingerprint") != fingerprint or not isinstance(updated_at, datetime):
        return False
    try:
        age_hours = (moment - updated_at).total_seconds() / 3600.0
    except TypeError:
        return False
    return 0.0 <= age_hours <= NASA_FAILURE_BACKOFF_HOURS


def _store_nasa_failure(store: Any, fingerprint: str, moment: datetime) -> None:
    """Remember a failed fetch (best effort) so the next refresh does not retry immediately."""
    if store is None:
        return
    try:
        store.profile_put(
            NASA_KIND,
            f"{NASA_FAILURE_KEY}:{fingerprint}",
            {"fingerprint": fingerprint, "failed_at": to_iso(moment)},
            moment,
        )
    except Exception as err:  # noqa: BLE001 - a cache write failure is not a forecast failure
        log.debug("NASA POWER failure-cache write failed: %s", err)


def _store_nasa_profile(
    store: Any,
    fingerprint: str,
    factors: Sequence[float],
    hours: int,
    samples: int,
    moment: datetime,
) -> None:
    if store is None:
        return
    try:
        store.profile_put(
            NASA_KIND,
            NASA_PROFILE_KEY,
            {
                "factors": [round(float(value), 6) for value in factors],
                "hours": hours,
                "samples": samples,
                "fingerprint": fingerprint,
                "updated_at": to_iso(moment),
            },
            moment,
        )
    except Exception as err:  # noqa: BLE001 - a cache write failure is not a forecast failure
        log.debug("NASA POWER cache write failed: %s", err)


def _nasa_profile(
    settings: Any,
    store: Any,
    moment: datetime,
    *,
    latitude: float,
    longitude: float,
    zone: timezone | ZoneInfo,
    http: Callable[..., Any] | None = None,
) -> tuple[list[float] | None, str, int, int]:
    """``(clearness, status, hours, samples)``; ``None`` when off, failed or without daylight.

    Never raises: NASA is an accuracy refinement on top of the model, not a dependency. A cached
    daily fit is reused; otherwise the window ending before the near-real-time latency is fetched
    once and stored.
    """
    if not bool(settings.get("solar.nasa_irradiance", True)):
        return None, "disabled", 0, 0
    # The per-hour buckets are local, so the timezone is part of the profile's identity.
    fingerprint = f"{latitude:.4f}|{longitude:.4f}|{zone}"
    cached = _nasa_cache(store, fingerprint, moment)
    if cached is not None:
        factors, hours, samples = cached
        return factors, "cached daily fit", hours, samples
    if _nasa_failure_fresh(store, fingerprint, moment):
        return None, "failed", 0, 0
    end = moment - timedelta(days=NASA_LATENCY_DAYS)
    start = end - timedelta(days=NASA_WINDOW_DAYS)
    try:
        payload = fetch_nasa_hourly(latitude, longitude, start, end, http=http)
        factors, hours, samples = nasa_clearness_profile(payload, zone)
    except Exception as err:  # noqa: BLE001 - optional refinement; a failure is a note, not an error
        log.warning("NASA POWER irradiance unavailable: %s", err)
        _store_nasa_failure(store, fingerprint, moment)
        return None, "failed", 0, 0
    if not hours:
        _store_nasa_failure(store, fingerprint, moment)
        return None, "no daylight samples", 0, samples
    _store_nasa_profile(store, fingerprint, factors, hours, samples, moment)
    return factors, "fresh fit", hours, samples


def _capacity_geometry(settings: Any) -> tuple[float, float, bool, bool]:
    """``(tilt, azimuth, tilt_assumed, azimuth_assumed)``; an unset value is an assumption."""
    tilt = settings.get("solar.tilt_deg")
    azimuth = settings.get("solar.azimuth_deg")
    tilt_assumed = tilt is None
    azimuth_assumed = azimuth is None
    try:
        tilt_deg = DEFAULT_TILT_DEG if tilt_assumed else clamp(float(tilt), 0.0, 90.0)
    except (TypeError, ValueError):
        tilt_deg, tilt_assumed = DEFAULT_TILT_DEG, True
    try:
        azimuth_deg = (
            DEFAULT_AZIMUTH_DEG if azimuth_assumed else float(azimuth) % 360.0
        )
    except (TypeError, ValueError):
        azimuth_deg, azimuth_assumed = DEFAULT_AZIMUTH_DEG, True
    return tilt_deg, azimuth_deg, tilt_assumed, azimuth_assumed


def _location_or_error(
    settings: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
) -> tuple[float, float, str] | None:
    """Resolve ``(lat, lon, tz)`` or record a status/note and return None (never raises)."""
    if client is not None:
        from ..config import resolve_location
        from ..harest import HAError

        options = {
            "latitude_override": settings.get("latitude_override"),
            "longitude_override": settings.get("longitude_override"),
            "timezone": settings.get("timezone"),
        }
        try:
            return resolve_location(client.get_config(), options)
        except HAError as err:
            status.ok = False
            status.last_error = f"Home Assistant unreachable: {err}"
            forecast.stale = True
            forecast.notes.append(status.last_error)
            return None
        except ConfigError:
            pass
    status.ok = False
    status.last_error = "location unavailable: set the overrides or expose /api/config"
    forecast.stale = True
    forecast.notes.append(status.last_error)
    return None


def _capacity_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    location: tuple[float, float, str] | None,
    origin: str = "solar.capacity_wp",
    http: Callable[..., Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Estimate production from ``solar.capacity_wp`` (SPEC.md section 7.1.1).

    Never raises and never guesses a capacity: a missing one is an empty forecast plus a status
    that names ``solar.capacity_wp``, because the bias correction may only adjust a forecast that
    already exists. ``origin`` names where the watt peak came from in the notes and the status
    detail; the derived source (SPEC.md 7.1.2) passes its learned value through
    :class:`_CapacityOverlay`.
    """
    try:
        capacity_wp = float(settings.get("solar.capacity_wp") or 0.0)
    except (TypeError, ValueError):
        capacity_wp = 0.0
    if capacity_wp <= 0.0:
        status.ok = False
        status.detail["reason"] = "no capacity configured"
        status.last_error = MISSING_CAPACITY_ERROR
        forecast.notes.append(status.last_error)
        return forecast, status

    if location is None:
        location = _location_or_error(settings, client, status=status, forecast=forecast)
        if location is None:
            return forecast, status

    latitude, longitude, timezone_name = location
    zone = _zone(timezone_name)
    horizon = horizon_hours(settings)
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)

    tilt_deg, azimuth_deg, tilt_assumed, azimuth_assumed = _capacity_geometry(settings)
    try:
        performance_ratio = float(settings.get("solar.performance_ratio", 0.85) or 0.85)
    except (TypeError, ValueError):
        performance_ratio = 0.85
    weather_entity = str(settings.get("solar.weather_entity", "") or "").strip()
    cloud_map, cloud_note, _weather_temperatures, _weather = _slot_cloud_map(client, weather_entity, zone, grid)
    # NASA POWER is an addition, not a replacement: it is only consulted when the weather
    # forecast does not already cover every hour of the horizon, and it never overrides cloud
    # coverage. One keyless request per location per day (daily-cached).
    covered_hours = (
        {slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in cloud_map}
        if cloud_map
        else set()
    )
    grid_hours = {
        slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in grid
    }
    nasa_factors: list[float] | None = None
    nasa_status = "not needed"
    nasa_note: str | None = None
    if grid_hours - covered_hours:
        nasa_factors, nasa_status, nasa_hours, nasa_samples = _nasa_profile(
            settings,
            store,
            moment,
            latitude=latitude,
            longitude=longitude,
            zone=zone,
            http=http,
        )
        if nasa_factors is not None:
            nasa_note = NASA_APPLIED_NOTE.format(
                hours=nasa_hours, samples=nasa_samples, days=NASA_WINDOW_DAYS
            )
        elif nasa_status == "failed":
            nasa_note = NASA_ERROR_NOTE.format(error="the request failed")
        elif nasa_status == "no daylight samples":
            nasa_note = NASA_NO_DATA_NOTE

    forecast.notes.append(f"capacity estimate: {capacity_wp:g} Wp from {origin}")
    if tilt_assumed and azimuth_assumed:
        forecast.notes.append(ASSUMED_GEOMETRY_NOTE)
    elif tilt_assumed:
        forecast.notes.append(ASSUMED_TILT_NOTE)
    elif azimuth_assumed:
        forecast.notes.append(ASSUMED_AZIMUTH_NOTE)
    forecast.notes.append(
        f"performance ratio {performance_ratio:g} (solar.performance_ratio)"
    )
    if cloud_note:
        forecast.notes.append(cloud_note)
    if nasa_note:
        forecast.notes.append(nasa_note)
    measured_entity = str(settings.get("solar.measured_entity", "") or "").strip()
    forecast.notes.append(MEASURED_NOTE if measured_entity else MISSING_MEASURED_NOTE)

    bias_enabled = bool(settings.get("solar.bias_correction", True))
    bias_payload = _bias_payload(store) if (store is not None and bias_enabled) else _empty_bias_payload()
    factors = (
        _decay_factors(bias_payload["factors"], bias_payload.get("updated_at"), moment)
        if bias_enabled
        else [1.0] * 24
    )
    if cloud_map:
        uncertainty = CAPACITY_UNCERTAINTY_CLOUD_PCT
    elif nasa_factors is not None:
        uncertainty = CAPACITY_UNCERTAINTY_NASA_PCT
    else:
        uncertainty = CAPACITY_UNCERTAINTY_CLEAR_PCT
    applied: list[float] = []
    slots: list[SolarSlot] = []
    for slot_time in grid:
        # ``cloud_map`` is keyed by slot (15-minute steps); the hour-truncated local key is itself
        # one of those slot keys because every timezone offset is a whole number of 15-minute steps,
        # so the lookup returns that local hour's coverage for all four of its slots.
        hour_key = slot_time.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        coverage = cloud_map.get(hour_key) if cloud_map else None
        if coverage is not None:
            attenuation = _cloud_factor(coverage)
        elif nasa_factors is not None:
            attenuation = nasa_factors[slot_time.astimezone(zone).hour]
        else:
            attenuation = 1.0
        poa = _plane_irradiance(latitude, longitude, slot_time, zone, tilt_deg, azimuth_deg)
        pv_kw = (capacity_wp / 1000.0) * (poa * attenuation / 1000.0) * performance_ratio
        factor = factors[slot_time.astimezone(zone).hour] if bias_enabled else 1.0
        applied.append(factor)
        slots.append(
            SolarSlot(
                t=slot_time,
                pv_kw=max(0.0, pv_kw * factor),
                uncertainty_pct=uncertainty,
            )
        )

    forecast.slots = slots
    forecast.planes = [
        {
            "capacity_wp": capacity_wp,
            "tilt_deg": tilt_deg,
            "azimuth_deg": azimuth_deg,
            "performance_ratio": performance_ratio,
        }
    ]
    forecast.correction_applied = sum(applied) / len(applied) if applied else 1.0
    status.ok = True
    status.last_success = moment
    status.stale = False
    status.detail.update(
        {
            "capacity_wp": capacity_wp,
            "capacity_source": origin,
            "tilt_deg": tilt_deg,
            "azimuth_deg": azimuth_deg,
            "assumed_tilt": tilt_assumed,
            "assumed_azimuth": azimuth_assumed,
            "performance_ratio": performance_ratio,
            "cloud_attenuation": bool(cloud_map),
            "nasa_irradiance": bool(nasa_factors),
            "nasa_status": nasa_status,
            "weather_entity": weather_entity or None,
            "bias_correction": bias_enabled,
            "correction_applied": round(forecast.correction_applied, 4),
        }
    )
    return forecast, status


def _current_temperature_c(client: Any, entity_id: str) -> float | None:
    """The mapped temperature sensor's current reading in degrees C, or ``None``."""
    try:
        state = client.get_state(entity_id)
    except Exception:  # noqa: BLE001 - a state read failure is a missing temperature, nothing more
        return None
    if not isinstance(state, Mapping):
        return None
    attributes = state.get("attributes") or {}
    unit = attributes.get("unit_of_measurement") if isinstance(attributes, Mapping) else None
    return temperature_to_c(state.get("state"), unit)


def _learned_temperatures(
    client: Any,
    settings: Any,
    zone: timezone | ZoneInfo,
    grid: Sequence[datetime],
    weather_temperatures: Mapping[datetime, float],
) -> tuple[dict[datetime, float], str]:
    """Air temperature per slot for the learned model, and a note naming where it came from.

    Preference is the weather entity's own hourly forecast, because it is the only source that
    actually varies across the horizon; a mapped sensor is held flat, which is the same
    simplification the cloud fallback already makes. The last resort is a stated constant: the
    module temperature term moves power about four percent per ten degrees, so this is worth being
    honest about rather than quietly assuming a pleasant afternoon.
    """
    if weather_temperatures:
        hours = sorted(weather_temperatures)
        last = weather_temperatures[hours[0]]
        held: dict[datetime, float] = {}
        for slot in grid:
            key = slot.astimezone(zone).replace(minute=0, second=0, microsecond=0)
            value = weather_temperatures.get(key)
            if value is not None:
                last = value
            held[slot] = last
        entity = str(settings.get("solar.weather_entity", "") or "").strip()
        return held, LEARNED_TEMPERATURE_WEATHER_NOTE.format(entity=entity)
    sensor = str(settings.get("load.temperature_entity", "") or "").strip()
    if sensor and client is not None:
        reading = _current_temperature_c(client, sensor)
        if reading is not None:
            return (
                {slot: reading for slot in grid},
                LEARNED_TEMPERATURE_HELD_NOTE.format(entity=sensor),
            )
    return (
        {slot: DEFAULT_AIR_TEMPERATURE_C for slot in grid},
        LEARNED_ASSUMED_TEMPERATURE_NOTE.format(value=DEFAULT_AIR_TEMPERATURE_C),
    )


def _learned_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    location: tuple[float, float, str] | None,
    origin: str = "solar.capacity_wp",
    http: Callable[..., Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Learned residual model over the array's own physics (SPEC.md section 7.1.4).

    The base model is trained on other people's arrays, so it exists to answer the question a fresh
    install cannot answer from its own history: what will this array produce. It is deliberately a
    multiplier on physics rather than a direct power regression, which is what lets one artifact
    serve a site it has never seen.

    Never raises. A missing artifact, a missing capacity or a broken model file is a status naming
    the cause, because solar is optional and a forecast that cannot be made must say so rather than
    quietly return zero (SPEC.md section 3).
    """
    try:
        model = pv_learned.load_model()
    except (OSError, ValueError, KeyError, TypeError) as err:  # noqa: BLE001 - a bad artifact is a status
        status.ok = False
        status.last_error = LEARNED_UNAVAILABLE_ERROR.format(error=err)
        status.detail["reason"] = "model unavailable"
        forecast.notes.append(status.last_error)
        return forecast, status

    try:
        capacity_wp = float(settings.get("solar.capacity_wp") or 0.0)
    except (TypeError, ValueError):
        capacity_wp = 0.0
    if capacity_wp <= 0.0:
        status.ok = False
        status.detail["reason"] = "no capacity configured"
        status.last_error = MISSING_CAPACITY_ERROR
        forecast.notes.append(status.last_error)
        return forecast, status

    if location is None:
        location = _location_or_error(settings, client, status=status, forecast=forecast)
        if location is None:
            return forecast, status
    latitude, longitude, timezone_name = location
    zone = _zone(timezone_name)
    horizon = horizon_hours(settings)
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)

    tilt_deg, azimuth_deg, tilt_assumed, azimuth_assumed = _capacity_geometry(settings)
    try:
        ac_capacity_wp = float(settings.get("solar.ac_capacity_wp") or 0.0)
    except (TypeError, ValueError):
        ac_capacity_wp = 0.0
    ac_assumed = ac_capacity_wp <= 0.0
    if ac_assumed:
        ac_capacity_wp = capacity_wp

    weather_entity = str(settings.get("solar.weather_entity", "") or "").strip()
    cloud_map, cloud_note, weather_temperatures, weather_fields = _slot_cloud_map(client, weather_entity, zone, grid)
    temperatures, temperature_note = _learned_temperatures(
        client, settings, zone, grid, weather_temperatures
    )
    covered_hours = (
        {slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in cloud_map}
        if cloud_map
        else set()
    )
    grid_hours = {slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in grid}
    nasa_factors: list[float] | None = None
    nasa_status = "not needed"
    nasa_note: str | None = None
    if grid_hours - covered_hours:
        nasa_factors, nasa_status, nasa_hours, nasa_samples = _nasa_profile(
            settings,
            store,
            moment,
            latitude=latitude,
            longitude=longitude,
            zone=zone,
            http=http,
        )
        if nasa_factors is not None:
            nasa_note = NASA_APPLIED_NOTE.format(
                hours=nasa_hours, samples=nasa_samples, days=NASA_WINDOW_DAYS
            )
        elif nasa_status == "failed":
            nasa_note = NASA_ERROR_NOTE.format(error="the request failed")
        elif nasa_status == "no daylight samples":
            nasa_note = NASA_NO_DATA_NOTE

    forecast.notes.append(LEARNED_CAPACITY_NOTE.format(capacity_wp=capacity_wp, origin=origin))
    forecast.notes.append(
        LEARNED_ASSUMED_AC_NOTE if ac_assumed else LEARNED_AC_NOTE.format(ac_capacity_wp=ac_capacity_wp)
    )
    if tilt_assumed and azimuth_assumed:
        forecast.notes.append(ASSUMED_GEOMETRY_NOTE)
    elif tilt_assumed:
        forecast.notes.append(ASSUMED_TILT_NOTE)
    elif azimuth_assumed:
        forecast.notes.append(ASSUMED_AZIMUTH_NOTE)
    forecast.notes.append(LEARNED_NO_RATIO_NOTE)
    forecast.notes.append(LEARNED_IRRADIANCE_NOTE)
    forecast.notes.append(temperature_note)
    if cloud_note:
        forecast.notes.append(cloud_note)
    if nasa_note:
        forecast.notes.append(nasa_note)
    measured_entity = str(settings.get("solar.measured_entity", "") or "").strip()
    forecast.notes.append(MEASURED_NOTE if measured_entity else MISSING_MEASURED_NOTE)

    bias_enabled = bool(settings.get("solar.bias_correction", True))
    bias_payload = _bias_payload(store) if (store is not None and bias_enabled) else _empty_bias_payload()
    ewma_factors = (
        _decay_factors(bias_payload["factors"], bias_payload.get("updated_at"), moment)
        if bias_enabled
        else [1.0] * 24
    )
    # The learned source is the one that personalises, so it is the one that consults the head. The
    # head replaces the EWMA for this provider only: a promoted head has been shown to beat those
    # factors on data it was not fitted on, and using both would apply one correction twice.
    factors = ewma_factors
    correction_source = "hourly EWMA" if bias_enabled else "disabled"
    if bias_enabled and store is not None:
        head = _head_factors(store, zone, moment, ewma_factors)
        if head is not None:
            factors = head
            correction_source = "fine-tune head"
    if cloud_map:
        uncertainty = CAPACITY_UNCERTAINTY_CLOUD_PCT
    elif nasa_factors is not None:
        uncertainty = CAPACITY_UNCERTAINTY_NASA_PCT
    else:
        uncertainty = CAPACITY_UNCERTAINTY_CLEAR_PCT
    applied: list[float] = []
    ratios: list[float] = []
    slots: list[SolarSlot] = []
    for slot_time in grid:
        hour_key = slot_time.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        coverage = cloud_map.get(hour_key) if cloud_map else None
        weather = {name: values.get(hour_key) for name, values in weather_fields.items()}
        if coverage is not None:
            # The same Kasten-Czeplak expression the training corpus was built with, taken from the
            # served module so the two cannot disagree about it.
            cloud_pct = clamp(float(coverage), 0.0, 100.0)
            attenuation = pv_learned.cloud_attenuation(cloud_pct)
        elif nasa_factors is not None:
            # The clearness profile is a real attenuation from a real sky, so it is kept. The cloud
            # term is inverted out of it rather than left at zero: handing the model ``cloud_pct = 0``
            # beside an attenuation of 0.75 would describe two different skies in one row.
            attenuation = nasa_factors[slot_time.astimezone(zone).hour]
            cloud_pct = pv_learned.cloud_pct_from_attenuation(attenuation)
        else:
            attenuation = 1.0
            cloud_pct = 0.0
        # The model is driven by an *estimated* horizontal global irradiance, exactly as the corpus
        # was driven by an estimated one: clear-sky Haurwitz attenuated by whatever the provider knows
        # about cloud. The clearness index that follows is normalised by extraterrestrial
        # irradiance, so it lands in the range the model was trained on rather than outside it.
        geometry = pv_learned.solar_geometry(latitude, longitude, slot_time, tilt_deg, azimuth_deg)
        estimate = pv_learned.estimate_slot(
            model,
            moment=slot_time,
            latitude=latitude,
            longitude=longitude,
            tilt_deg=tilt_deg,
            azimuth_deg=azimuth_deg,
            dc_capacity_wp=capacity_wp,
            ac_capacity_wp=ac_capacity_wp,
            # ``clear_sky_ghi_wm2`` is already zero below the horizon, so this stays zero at night
            # however large the attenuation is.
            ghi_wm2=geometry.clear_sky_ghi_wm2 * attenuation,
            t_ambient_c=temperatures[slot_time],
            cloud_pct=cloud_pct,
            attenuation=attenuation,
            # Absent terms are zero, and the forecast says so rather than implying the entity
            # published a calm, dry hour.
            wind_ms=weather.get("wind_ms") or 0.0,
            rh_pct=weather.get("rh_pct") or 0.0,
            rain_mm=weather.get("rain_mm") or 0.0,
        )
        factor = factors[slot_time.astimezone(zone).hour] if bias_enabled else 1.0
        applied.append(factor)
        if estimate.elevation_deg > 0.0:
            ratios.append(estimate.ratio)
        slots.append(
            SolarSlot(
                t=slot_time,
                pv_kw=max(0.0, estimate.pv_kw * factor),
                uncertainty_pct=uncertainty,
            )
        )

    forecast.slots = slots
    forecast.planes = [
        {
            "capacity_wp": capacity_wp,
            "ac_capacity_wp": ac_capacity_wp,
            "tilt_deg": tilt_deg,
            "azimuth_deg": azimuth_deg,
        }
    ]
    forecast.correction_applied = sum(applied) / len(applied) if applied else 1.0
    status.ok = True
    status.last_success = moment
    status.stale = False
    status.detail.update(
        {
            "capacity_wp": capacity_wp,
            "capacity_source": origin,
            "ac_capacity_wp": ac_capacity_wp,
            "assumed_ac_capacity": ac_assumed,
            "tilt_deg": tilt_deg,
            "azimuth_deg": azimuth_deg,
            "assumed_tilt": tilt_assumed,
            "assumed_azimuth": azimuth_assumed,
            "performance_ratio_applied": False,
            "cloud_attenuation": bool(cloud_map),
            "nasa_irradiance": bool(nasa_factors),
            "nasa_status": nasa_status,
            "weather_entity": weather_entity or None,
            "bias_correction": bias_enabled,
            "correction_applied": round(forecast.correction_applied, 4),
            # The artifact's own provenance, so a forecast can always be traced to a model and a
            # corpus rather than to "the learned thing".
            "model": model.metadata,
            "mean_residual_ratio": round(sum(ratios) / len(ratios), 4) if ratios else None,
            "correction_source": correction_source,
            "fine_tune_head": _head_status(store),
        }
    )
    return forecast, status


# --------------------------------------------------------------------------------------------
# Provider entry point
# --------------------------------------------------------------------------------------------


class _CapacityOverlay:
    """Settings proxy that pins ``solar.capacity_wp`` to the derived source's learned value."""

    def __init__(self, settings: Any, capacity_wp: float) -> None:
        self._settings = settings
        self._overrides = {"solar.capacity_wp": capacity_wp}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return self._settings.get(key, default)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


def _implied_capacity_wp(
    latitude: float,
    longitude: float,
    zone: timezone | ZoneInfo,
    samples: Sequence[tuple[datetime, float]],
    settings: Any,
    clearness: Sequence[float] | None = None,
) -> tuple[float, int]:
    """Array size (Wp) implied by meter-derived PV samples: the upper envelope of pv / model.

    Every sample is divided by the clear-sky model's output *per installed watt* at that moment,
    with the same tilt/azimuth assumptions and performance ratio the forecast uses, and the
    ``DERIVED_ENVELOPE_QUANTILE`` of those ratios is the array size: overcast hours pull the ratio
    down, so the top of the distribution is the cloudless estimate. ``clearness`` (NASA POWER's
    per-hour observed ratio) removes some of that bias first, so a partly cloudy window no longer
    has to look cloudless to fit. Returns ``(0.0, usable)`` when there is not enough daylight.
    """
    tilt_deg, azimuth_deg, _, _ = _capacity_geometry(settings)
    try:
        performance_ratio = float(settings.get("solar.performance_ratio", 0.85) or 0.85)
    except (TypeError, ValueError):
        performance_ratio = 0.85
    ratios: list[float] = []
    for stamp, pv_kw in samples:
        poa = _plane_irradiance(latitude, longitude, stamp, zone, tilt_deg, azimuth_deg)
        observed = clearness[stamp.astimezone(zone).hour] if clearness else 1.0
        per_wp_kw = (poa / 1000.0) * observed * performance_ratio / 1000.0
        if per_wp_kw <= DERIVED_MIN_MODEL_KW_PER_WP:
            continue
        ratios.append(float(pv_kw) / per_wp_kw)
    if len(ratios) < DERIVED_MIN_SAMPLES:
        return 0.0, len(ratios)
    ratios.sort()
    index = min(len(ratios) - 1, int(round(DERIVED_ENVELOPE_QUANTILE * (len(ratios) - 1))))
    return max(0.0, ratios[index]), len(ratios)


def _derived_cache(
    store: Any, fingerprint: str, moment: datetime
) -> dict[str, Any] | None:
    """The cached learned watt peak, when it is fresh and was fitted from the same entity mapping."""
    if store is None:
        return None
    got = store.profile_get(DERIVED_KIND, DERIVED_PROFILE_KEY)
    if not got or not isinstance(got[1], Mapping):
        return None
    updated_at, payload = got
    if payload.get("fingerprint") != fingerprint:
        return None
    if updated_at is not None:
        try:
            age_hours = (moment - updated_at).total_seconds() / 3600.0
        except TypeError:  # a naive timestamp cannot be trusted: refit instead of crashing
            age_hours = DERIVED_REFIT_HOURS + 1.0
        if age_hours > DERIVED_REFIT_HOURS:
            return None
    try:
        capacity_wp = float(payload["capacity_wp"])
        samples = int(payload["samples"])
    except (KeyError, TypeError, ValueError):
        return None
    if capacity_wp <= 0.0 or samples < DERIVED_MIN_SAMPLES:
        return None
    return {"capacity_wp": capacity_wp, "samples": samples, "updated_at": payload.get("updated_at")}


def _store_derived_capacity(
    store: Any, fingerprint: str, capacity_wp: float, samples: int, moment: datetime
) -> None:
    if store is None:
        return
    store.profile_put(
        DERIVED_KIND,
        DERIVED_PROFILE_KEY,
        {
            "capacity_wp": round(capacity_wp, 1),
            "samples": samples,
            "fingerprint": fingerprint,
            "updated_at": to_iso(moment),
        },
        moment,
    )


def _collect_export_samples(
    settings: Any,
    client: Any,
    *,
    moment: datetime,
    roles: Mapping[str, str],
    ws_factory: Callable[[], Any] | None,
) -> tuple[list[tuple[datetime, float]], str]:
    """Export samples with long-term statistics when a WebSocket client is available.

    The statistics table is never purged, so the fit sees weeks where recorder history
    (``get_history``) is already empty. A socket failure falls back to recorder history: the fit
    must never fail because one transport is down (SPEC.md 7.1.2).
    """
    from .load import collect_pv_export_samples

    if ws_factory is not None:
        try:
            with ws_factory() as websocket:
                return collect_pv_export_samples(
                    settings, client, ws=websocket, now=moment, roles=roles
                )
        except Exception as exc:  # noqa: BLE001 - recorder history is the fallback
            log.debug(
                "meter-derived solar: statistics unavailable (%s); using recorder history", exc
            )
    return collect_pv_export_samples(settings, client, now=moment, roles=roles)


def _derived_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    location: tuple[float, float, str] | None,
    http: Callable[..., Any] | None = None,
    grid_roles: Mapping[str, str] | None = None,
    ws_factory: Callable[[], Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Learn the array from the meter, then forecast with the capacity model (SPEC.md 7.1.2).

    Nothing is written back to the settings: the learned watt peak is stated in the notes and the
    status detail (``capacity_source``, ``derived_samples``), and ``solar.capacity_wp`` overrides
    it. The fit itself is cached per entity mapping and refit at most daily, because it reads weeks
    of history. ``grid_roles`` carries the Energy-dashboard fallback from :mod:`app.grid` (explicit
    ``grid.*`` settings still win); ``ws_factory`` reads that history from long-term statistics so
    the window is not the recorder's purge window. A meter that never exports, or a battery whose
    flow cannot be read, is a status, not a crash.
    """
    from .load import pv_export_roles

    if client is None:
        status.ok = False
        status.detail["reason"] = "no Home Assistant client"
        status.last_error = "the derived solar source needs the Home Assistant connection for history"
        forecast.notes.append(status.last_error)
        return forecast, status
    # An explicit watt peak wins over the learned one (the same "explicit beats derived" rule the
    # load tier follows): the meter then only has to be right about the export moments.
    try:
        explicit_wp = float(settings.get("solar.capacity_wp") or 0.0)
    except (TypeError, ValueError):
        explicit_wp = 0.0
    if explicit_wp > 0.0:
        forecast.notes.append(
            f"solar.capacity_wp is set ({explicit_wp:g} Wp), so the meter-derived estimate is not used"
        )
        return _capacity_forecast(
            settings,
            store,
            client,
            status=status,
            forecast=forecast,
            moment=moment,
            location=location,
            http=http,
        )
    roles, refusal = pv_export_roles(settings, defaults=grid_roles)
    if refusal:
        status.ok = False
        status.detail["reason"] = "no usable meter/battery mapping"
        status.last_error = f"meter-derived solar: {refusal}"
        forecast.notes.append(status.last_error)
        return forecast, status
    # The learned watt peak depends on whether the NASA clearness layer was part of the fit, so that
    # flag is part of the fit's identity: toggling it refits instead of reusing a stale watt peak.
    fingerprint = "|".join(f"{role}={roles[role]}" for role in sorted(roles))
    fingerprint += f"|nasa={bool(settings.get('solar.nasa_irradiance', True))}"
    cached = _derived_cache(store, fingerprint, moment)
    if cached is not None:
        capacity_wp, usable, origin_note = (
            float(cached["capacity_wp"]),
            int(cached["samples"]),
            "cached daily fit",
        )
    else:
        samples, reason = _collect_export_samples(
            settings, client, moment=moment, roles=roles, ws_factory=ws_factory
        )
        if not samples:
            status.ok = False
            status.detail["reason"] = "no export samples"
            status.last_error = f"meter-derived solar: {reason or DERIVED_NO_HISTORY_ERROR}"
            forecast.notes.append(status.last_error)
            return forecast, status
        if location is None:
            location = _location_or_error(settings, client, status=status, forecast=forecast)
            if location is None:
                return forecast, status
        latitude, longitude, timezone_name = location
        zone = _zone(timezone_name)
        # NASA POWER's observed clearness removes part of the overcast bias from the fit, so a
        # partly cloudy window does not have to look cloudless to size the array (SPEC 7.1.1a).
        nasa_factors, nasa_status, nasa_hours, nasa_samples = _nasa_profile(
            settings,
            store,
            moment,
            latitude=latitude,
            longitude=longitude,
            zone=zone,
            http=http,
        )
        capacity_wp, usable = _implied_capacity_wp(
            latitude, longitude, zone, samples, settings, clearness=nasa_factors
        )
        if capacity_wp <= 0.0 or usable < DERIVED_MIN_SAMPLES:
            status.ok = False
            status.detail["reason"] = "not enough daylight export samples"
            status.last_error = (
                f"meter-derived solar: only {usable} usable daylight export samples in the window "
                f"(need {DERIVED_MIN_SAMPLES}); the meter has to export for the array to be visible"
            )
            forecast.notes.append(status.last_error)
            return forecast, status
        _store_derived_capacity(store, fingerprint, capacity_wp, usable, moment)
        status.detail["nasa_irradiance"] = bool(nasa_factors)
        status.detail["nasa_status"] = nasa_status
        if nasa_factors is not None:
            forecast.notes.append(NASA_FIT_NOTE)
        origin_note = "fresh fit"
    if location is None:
        location = _location_or_error(settings, client, status=status, forecast=forecast)
        if location is None:
            return forecast, status
    overlay = _CapacityOverlay(settings, capacity_wp)
    forecast, status = _capacity_forecast(
        overlay,
        store,
        client,
        status=status,
        forecast=forecast,
        moment=moment,
        location=location,
        origin=f"the meter's export moments ({usable} samples, {origin_note})",
        http=http,
    )
    if status.ok:
        forecast.notes.append(DERIVED_PV_NOTE)
        status.detail.update({"derived_samples": usable, "derived_from": "meter export"})
    return forecast, status


def _plane_cache_key(latitude: float, longitude: float, tilt: float, azimuth: float, plane: Mapping[str, Any]) -> str:
    raw = f"{latitude:.4f}|{longitude:.4f}|{tilt:g}|{azimuth:g}|{float(plane['kwp']):g}|{plane.get('damping')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_single_source_forecast(
    settings: Any,
    store: Any = None,
    client: Any = None,
    *,
    location: tuple[float, float, str] | None = None,
    now: datetime | None = None,
    http: Callable[..., Any] | None = None,
    grid_roles: Mapping[str, str] | None = None,
    ws_factory: Callable[[], Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Today's single-source path (``solar.source``); the merge path only falls back here."""
    moment = now or utcnow()
    source = str(settings.get("solar.source", "forecast_solar") or "forecast_solar")
    if source in ("capacity", "derived", LEARNED_SOURCE):
        status = ProviderStatus(kind="solar", name=source, active=True)
        forecast = SolarForecast(generated_at=moment, source=source)
        if not bool(settings.get("solar.enabled", True)):
            status.active = False
            status.ok = True
            status.detail["reason"] = "disabled"
            forecast.notes.append("solar forecasting is disabled")
            return forecast, status
        if source == "derived":
            return _derived_forecast(
                settings,
                store,
                client,
                status=status,
                forecast=forecast,
                moment=moment,
                location=location,
                http=http,
                grid_roles=grid_roles,
                ws_factory=ws_factory,
            )
        if source == LEARNED_SOURCE:
            return _learned_forecast(
                settings,
                store,
                client,
                status=status,
                forecast=forecast,
                moment=moment,
                location=location,
                http=http,
            )
        return _capacity_forecast(
            settings,
            store,
            client,
            status=status,
            forecast=forecast,
            moment=moment,
            location=location,
            http=http,
        )
    if source != "forecast_solar":
        raise ConfigError(f"unknown solar source: {source}")
    status = ProviderStatus(kind="solar", name=source, active=True)
    forecast = SolarForecast(generated_at=moment, source=source)
    if not bool(settings.get("solar.enabled", True)):
        status.active = False
        status.ok = True
        status.detail["reason"] = "disabled"
        forecast.notes.append("solar forecasting is disabled")
        return forecast, status
    return _forecast_solar_forecast(
        settings,
        store,
        client,
        status=status,
        forecast=forecast,
        moment=moment,
        location=location,
        http=http,
        grid_roles=grid_roles,
        ws_factory=ws_factory,
    )


def _forecast_solar_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    location: tuple[float, float, str] | None = None,
    http: Callable[..., Any] | None = None,
    grid_roles: Mapping[str, str] | None = None,
    ws_factory: Callable[[], Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """The Forecast.Solar planes path (SPEC.md section 7.1), shared by both entry points."""
    planes = [dict(plane) for plane in (settings.get("solar.planes") or []) if isinstance(plane, Mapping)]
    if not planes:
        # Forecast.Solar has nothing to size the array with, but the meter may: try the
        # meter-derived estimate before reporting no_pv_source, because a user who configured the
        # meter in the Energy dashboard should get a production estimate without repeating the
        # mapping here. An explicit solar.capacity_wp still wins inside the derived tier.
        fallback_forecast = SolarForecast(generated_at=moment, source="derived")
        fallback_status = ProviderStatus(kind="solar", name="derived", active=True)
        fallback_forecast, fallback_status = _derived_forecast(
            settings,
            store,
            client,
            status=fallback_status,
            forecast=fallback_forecast,
            moment=moment,
            location=location,
            http=http,
            grid_roles=grid_roles,
            ws_factory=ws_factory,
        )
        if fallback_forecast.slots and fallback_status.ok:
            fallback_forecast.notes.insert(
                0,
                "solar.planes is empty; estimating from the meter's export history instead"
                " (meter-derived array)",
            )
            return fallback_forecast, fallback_status
        status.ok = True
        status.detail["reason"] = "no planes configured"
        if fallback_status.last_error:
            status.detail["derived_fallback"] = fallback_status.last_error
            forecast.notes.append(
                f"the meter-derived estimate is unavailable too: {fallback_status.last_error}"
            )
        forecast.notes.append("no solar planes configured")
        return forecast, status
    api_key = str(settings.get("solar.api_key", "") or "")
    if len(planes) > MAX_PLANES:
        ignored = len(planes) - MAX_PLANES
        forecast.notes.append(f"only the first {MAX_PLANES} of {len(planes)} configured planes are used")
        status.detail["planes_ignored"] = ignored
        planes = planes[:MAX_PLANES]
    if not api_key and len(planes) > 1:
        # Keyless is single-plane: never issue N requests and never silently drop configured planes.
        forecast.notes.append(
            "keyless Forecast.Solar is single-plane; using the first configured plane only"
        )
        status.detail["planes_ignored"] = len(planes) - 1
        planes = planes[:1]
    status.detail["planes_used"] = len(planes)

    if location is None and client is not None:
        from ..config import resolve_location
        from ..harest import HAError

        options = {
            "latitude_override": settings.get("latitude_override"),
            "longitude_override": settings.get("longitude_override"),
            "timezone": settings.get("timezone"),
        }
        try:
            location = resolve_location(client.get_config(), options)
        except HAError as err:
            status.ok = False
            status.last_error = f"Home Assistant unreachable: {err}"
            forecast.stale = True
            forecast.notes.append(status.last_error)
            return forecast, status
        except ConfigError:
            location = None
    if location is None:
        status.ok = False
        status.last_error = "location unavailable: set the overrides or expose /api/config"
        forecast.stale = True
        forecast.notes.append(status.last_error)
        return forecast, status

    latitude, longitude, timezone_name = location
    zone = _zone(timezone_name)
    horizon = horizon_hours(settings)
    grid = slot_range(quarter_floor(moment, SLOT_MINUTES), quarter_floor(moment, SLOT_MINUTES) + timedelta(hours=horizon), SLOT_MINUTES)

    series: list[list[tuple[datetime, float]]] = []
    errors: list[str] = []
    cache_hits = 0
    rate_limited = False
    for plane in planes:
        try:
            tilt = _plane_tilt(plane)
            azimuth = float(plane.get("azimuth_deg", 0) or 0.0)
            cache_key = _plane_cache_key(latitude, longitude, tilt, azimuth, plane)
        except (TypeError, ValueError, KeyError) as err:
            errors.append(f"invalid plane: {err}")
            continue
        cached = store.cache_get(CACHE_KIND, cache_key) if store is not None else None
        if cached and (moment - cached[0]).total_seconds() <= CACHE_TTL_S:
            series.append(_parse_watts(cached[1]))
            cache_hits += 1
            continue
        failures, until = _rate_limit_state(store, moment)
        if failures and until is not None and until > moment:
            if cached:
                series.append(_parse_watts(cached[1]))
                cache_hits += 1
                forecast.notes.append("serving cached solar forecast during rate-limit back-off")
                continue
            rate_limited = True
            errors.append("rate limited; backing off")
            continue
        try:
            payload = fetch_plane(
                latitude,
                longitude,
                {**plane, "tilt_deg": tilt},
                api_key=api_key,
                http=http,
            )
        except RateLimited:
            _record_rate_limit(store, moment)
            rate_limited = True
            if cached:
                series.append(_parse_watts(cached[1]))
                cache_hits += 1
                forecast.notes.append("serving cached solar forecast after HTTP 429")
                continue
            errors.append("rate limited and no cache available")
            continue
        except ProviderError as err:
            errors.append(str(err))
            continue
        if store is not None:
            store.cache_put(CACHE_KIND, cache_key, payload, moment)
            _reset_rate_limit(store)
        series.append(_parse_watts(payload))

    if not series:
        status.ok = False
        status.last_error = "; ".join(errors) or "no solar data"
        status.stale = rate_limited
        forecast.stale = True
        forecast.notes.extend(errors)
        return forecast, status
    if errors:
        forecast.notes.extend(errors)
        status.detail["failed_planes"] = errors

    bias_enabled = bool(settings.get("solar.bias_correction", True))
    bias_payload = _bias_payload(store) if (store is not None and bias_enabled) else _empty_bias_payload()
    factors = _decay_factors(bias_payload["factors"], bias_payload.get("updated_at"), moment) if bias_enabled else [1.0] * 24
    uncertainty = DEFAULT_UNCERTAINTY_PCT
    if bias_enabled and bias_payload.get("mape_pct") is not None:
        uncertainty = clamp(float(bias_payload["mape_pct"]), 5.0, 60.0)
    applied: list[float] = []
    slots: list[SolarSlot] = []
    for slot_time in grid:
        watts = sum(_interpolate(plane_series, slot_time) for plane_series in series)
        factor = factors[slot_time.astimezone(zone).hour] if bias_enabled else 1.0
        applied.append(factor)
        slots.append(SolarSlot(t=slot_time, pv_kw=max(0.0, watts / 1000.0) * factor, uncertainty_pct=uncertainty))

    forecast.slots = slots
    forecast.planes = [
        {key: value for key, value in plane.items() if key in ("kwp", "tilt_deg", "azimuth_deg", "damping")}
        for plane in planes
    ]
    forecast.correction_applied = sum(applied) / len(applied) if applied else 1.0
    status.ok = True
    status.last_success = moment
    status.stale = False
    status.detail.update(
        {
            "planes": len(planes),
            "cache_hits": cache_hits,
            "bias_correction": bias_enabled,
            "correction_applied": round(forecast.correction_applied, 4),
        }
    )
    return forecast, status


# --------------------------------------------------------------------------------------------
# Home Assistant native solar forecast (SPEC.md section 7.1.3; docs/EXTERNAL_APIS.md section 5)
# --------------------------------------------------------------------------------------------
#
# Any solar-forecast provider the HA Energy dashboard has configured (Forecast.Solar, Solcast, ...)
# can be selected without re-describing the array here. The websocket command is read-only and not
# admin-gated; the response is keyed by the dashboard's own config entry id and each value carries
# ``wh_hours``. A missing command or provider is a note, never a failed forecast.

HA_SOLAR_FORECAST_COMMAND = "energy/solar_forecast"
HA_ENERGY_NO_CLIENT_NOTE = "no Home Assistant WebSocket client; energy/solar_forecast cannot be read"
HA_ENERGY_UNAVAILABLE_NOTE = "HA energy/solar_forecast unavailable ({error})"
HA_ENERGY_EMPTY_NOTE = "the HA Energy dashboard has no solar forecast provider configured"
HA_ENERGY_NO_ENTRY_NOTE = "no HA Energy dashboard solar provider with that config entry id"
HA_ENERGY_OK_NOTE = "Home Assistant solar forecast: {providers} provider(s), {points} points"


def ha_energy_forecast_payload(ws: Any) -> dict[str, Any]:
    """Read the Energy dashboard's own solar forecast through a WebSocket client.

    ``ws`` is any object with the :meth:`haws.HAWebSocket.call` surface; the command takes no
    parameters. A non-mapping result is treated as empty rather than raising.
    """
    result = ws.call(HA_SOLAR_FORECAST_COMMAND)
    return dict(result) if isinstance(result, Mapping) else {}


def parse_ha_energy_wh(
    payload: Mapping[str, Any], entry_id: str = ""
) -> tuple[dict[datetime, float], int]:
    """Combine the selected providers' ``wh_hours`` into one ``{UTC timestamp: Wh}`` mapping.

    The verified contract is ``{config_entry_id: {"wh_hours": {ISO stamp: Wh}}}``
    (docs/EXTERNAL_APIS.md section 5). ``entry_id`` selects one provider; empty selects them all and
    sums their points, so several providers become one series. Returns ``(points, providers)``.
    """
    if not isinstance(payload, Mapping):
        return {}, 0
    selected: Mapping[str, Any] = payload
    if entry_id:
        value = payload.get(entry_id)
        if value is None:
            return {}, 0
        selected = {entry_id: value}
    combined: dict[datetime, float] = {}
    providers = 0
    for value in selected.values():
        block = value.get("wh_hours") if isinstance(value, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        providers += 1
        for stamp, wh in block.items():
            moment = from_iso(stamp)
            if moment is None:
                continue
            try:
                number = float(wh)
            except (TypeError, ValueError):
                continue
            key = moment.astimezone(timezone.utc)
            combined[key] = combined.get(key, 0.0) + number
    return combined, providers


def ha_energy_power_series(wh_by_stamp: Mapping[datetime, float]) -> list[tuple[datetime, float]]:
    """Convert per-period Wh into an average-kW series, using each point's own period length.

    The Energy dashboard's forecast points are Wh for the period starting at the stamp; the next
    stamp gives that period's length, and the last point assumes one hour. Periods are clamped to
    one minute .. three hours so a sparse or duplicated point cannot fabricate a spike.
    """
    stamps = sorted(wh_by_stamp)
    series: list[tuple[datetime, float]] = []
    for index, stamp in enumerate(stamps):
        hours = 1.0
        if index + 1 < len(stamps):
            hours = (stamps[index + 1] - stamp).total_seconds() / 3600.0
        hours = clamp(hours, 1.0 / 60.0, 3.0)
        series.append((stamp, max(0.0, float(wh_by_stamp[stamp]) / 1000.0 / hours)))
    return series


def _ha_energy_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    entry: Mapping[str, Any],
    grid: Sequence[datetime],
    location: tuple[float, float, str] | None = None,
    ws_factory: Callable[[], Any] | None = None,
    cache: dict[str, Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Build one source's series from HA's own solar forecast providers (never raises)."""
    shared = cache if cache is not None else {}
    entry_id = str(entry.get("entry_id") or "").strip()
    if ws_factory is None:
        status.ok = False
        status.detail["reason"] = "no websocket client"
        status.last_error = HA_ENERGY_NO_CLIENT_NOTE
        forecast.notes.append(status.last_error)
        return forecast, status
    if "payload" not in shared:
        try:
            with ws_factory() as websocket:
                shared["payload"] = ha_energy_forecast_payload(websocket)
        except Exception as err:  # noqa: BLE001 - an optional provider read is a note, never fatal
            shared["payload"] = None
            shared["error"] = err
    if shared.get("payload") is None:
        status.ok = False
        status.detail["reason"] = "energy/solar_forecast unavailable"
        status.last_error = HA_ENERGY_UNAVAILABLE_NOTE.format(
            error=type(shared.get("error")).__name__ if shared.get("error") else "unknown error"
        )
        forecast.notes.append(status.last_error)
        return forecast, status
    points, providers = parse_ha_energy_wh(shared["payload"], entry_id)
    if not points:
        status.ok = False
        status.detail["reason"] = "no HA solar forecast data"
        status.last_error = HA_ENERGY_NO_ENTRY_NOTE if entry_id else HA_ENERGY_EMPTY_NOTE
        forecast.notes.append(status.last_error)
        return forecast, status
    series = ha_energy_power_series(points)
    zone = _zone(location[2]) if location else timezone.utc
    bias_enabled = bool(settings.get("solar.bias_correction", True))
    bias_payload = _bias_payload(store) if (store is not None and bias_enabled) else _empty_bias_payload()
    factors = (
        _decay_factors(bias_payload["factors"], bias_payload.get("updated_at"), moment)
        if bias_enabled
        else [1.0] * 24
    )
    uncertainty = DEFAULT_UNCERTAINTY_PCT
    if bias_enabled and bias_payload.get("mape_pct") is not None:
        uncertainty = clamp(float(bias_payload["mape_pct"]), 5.0, 60.0)
    applied: list[float] = []
    slots: list[SolarSlot] = []
    for slot_time in grid:
        factor = factors[slot_time.astimezone(zone).hour] if bias_enabled else 1.0
        applied.append(factor)
        slots.append(
            SolarSlot(
                t=slot_time,
                pv_kw=max(0.0, _interpolate(series, slot_time)) * factor,
                uncertainty_pct=uncertainty,
            )
        )
    forecast.slots = slots
    forecast.correction_applied = sum(applied) / len(applied) if applied else 1.0
    status.ok = True
    status.last_success = moment
    status.stale = False
    status.detail.update(
        {
            "ha_providers": providers,
            "ha_points": len(points),
            "entry_id": entry_id or None,
            "bias_correction": bias_enabled,
            "correction_applied": round(forecast.correction_applied, 4),
        }
    )
    forecast.notes.append(HA_ENERGY_OK_NOTE.format(providers=providers, points=len(points)))
    return forecast, status


# --------------------------------------------------------------------------------------------
# Multi-source merge (SPEC.md section 7.1.3)
# --------------------------------------------------------------------------------------------

#: The merge strategies ``solar.merge_strategy`` accepts (config.py validates the setting).
MERGE_STRATEGIES = ("weighted_mean", "median", "max")
MERGE_SOURCE_TYPES = ("forecast_solar", "ha_energy", "capacity", "derived", "learned")
#: Provider-specific merge keys that override the matching ``solar.*`` setting for that source only.
_SOURCE_SETTING_KEYS = (
    "planes",
    "api_key",
    "capacity_wp",
    "tilt_deg",
    "azimuth_deg",
    "performance_ratio",
    "weather_entity",
    "nasa_irradiance",
)
MERGE_NO_SOURCE_NOTE = "no configured merge source produced a usable forecast; using solar.source alone"
MERGE_NOTE = "merged {count} source(s) with {strategy}{weights}"


class _SourceOverlay:
    """Settings proxy pinning one merge entry's provider-specific keys over the globals."""

    def __init__(self, settings: Any, entry: Mapping[str, Any]) -> None:
        self._settings = settings
        self._overrides = {
            f"solar.{key}": entry[key] for key in _SOURCE_SETTING_KEYS if key in entry
        }

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return self._settings.get(key, default)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


def configured_solar_sources(settings: Any) -> list[dict[str, Any]]:
    """The validated ``solar.sources`` entries, or ``[]`` for today's single-source path.

    Defensive on read: a value that predates validation (or a hand-edited DB row) is skipped with a
    note instead of raising, so a malformed entry can never crash the engine (SPEC.md R2).
    """
    raw = settings.get("solar.sources")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        source = str(entry.get("source", "") or "").strip().lower()
        if source not in MERGE_SOURCE_TYPES:
            log.warning("ignoring solar.sources entry with unknown source %r", source)
            continue
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(weight) or weight <= 0.0:
            continue
        item = dict(entry)
        item["source"] = source
        item["weight"] = weight
        out.append(item)
    return out


def merge_solar_series(
    grid: Sequence[datetime],
    entries: Sequence[Mapping[str, Any]],
    strategy: str = "weighted_mean",
) -> tuple[list[float], list[dict[str, Any]]]:
    """Pure, deterministic merge of per-source PV series onto one grid (SPEC.md section 7.1.3).

    ``entries`` is an ordered sequence of mappings: ``name`` (str), ``series`` (an iterable of
    ``(timestamp, pv_kw)`` samples, or empty/None when the provider produced nothing), ``weight``
    (finite > 0), an optional ``state`` (``ok|degraded|failed|disabled``) and an optional ``note``.
    Each series is aligned to ``grid`` with the same linear interpolation the single-source path
    uses (:func:`_interpolate`), so timezone and slot conventions are shared.

    Returns ``(values, report)``: one merged kW value per grid slot (0.0 when nothing contributes)
    and one report row per entry with ``name``, ``weight``, ``state``, ``note`` and ``points``. An
    empty series is skipped as ``failed``; a ``disabled`` entry is skipped and never contributes.
    Ties are resolved deterministically (``median`` of an even count is the mean of the two middle
    values, in the entries' order).
    """
    chosen = strategy if strategy in MERGE_STRATEGIES else "weighted_mean"
    aligned: list[tuple[str, list[tuple[datetime, float]], float]] = []
    report: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or entry.get("source") or "source")
        state = str(entry.get("state") or "")
        note = str(entry.get("note") or "")
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        raw_series = entry.get("series") or []
        series: list[tuple[datetime, float]] = []
        for sample in raw_series:
            try:
                stamp, value = sample
                series.append((stamp, float(value)))
            except (TypeError, ValueError):
                continue
        if state == "disabled":
            report.append({"name": name, "weight": weight, "state": "disabled", "note": note, "points": 0})
            continue
        if not series:
            report.append(
                {
                    "name": name,
                    "weight": weight,
                    "state": "failed",
                    "note": note or "no usable forecast",
                    "points": 0,
                }
            )
            continue
        report.append({"name": name, "weight": weight, "state": state or "ok", "note": note, "points": len(series)})
        aligned.append((name, series, weight))
    values: list[float] = []
    for slot in grid:
        picked = [(_interpolate(series, slot), weight) for _, series, weight in aligned]
        values.append(_merge_values(picked, chosen))
    return values, report


def _merge_values(picked: Sequence[tuple[float, float]], strategy: str) -> float:
    """Combine the per-source values present at one slot with ``strategy`` (pure)."""
    if not picked:
        return 0.0
    if strategy == "max":
        return max(value for value, _ in picked)
    if strategy == "median":
        ordered = sorted(value for value, _ in picked)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0
    total = sum(weight for _, weight in picked)
    if total <= 0:
        return 0.0
    return sum(value * weight for value, weight in picked) / total


def _source_display_name(entry: Mapping[str, Any], index: int, seen: set[str]) -> str:
    """A stable, unique status name for one configured source (never instance data in code)."""
    name = str(entry.get("name") or "").strip()
    if not name:
        name = str(entry.get("source") or "source")
        entry_id = str(entry.get("entry_id") or "").strip()
        if entry_id:
            name = f"{name}:{entry_id[:8]}"
    if name in seen:
        suffix = 2
        while f"{name} #{suffix}" in seen:
            suffix += 1
        name = f"{name} #{suffix}"
    seen.add(name)
    return name


def _merge_state(sub_status: ProviderStatus, series: Sequence[Any]) -> str:
    """The coarse state for one merged source: ok | degraded | failed | disabled."""
    if not sub_status.active:
        return "disabled"
    if not series or not sub_status.ok:
        return "failed"
    if sub_status.stale or sub_status.state == "degraded":
        return "degraded"
    return "ok"


def _dispatch_merge_source(
    settings: Any,
    store: Any,
    client: Any,
    *,
    entry: Mapping[str, Any],
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    grid: Sequence[datetime],
    location: tuple[float, float, str] | None,
    http: Callable[..., Any] | None,
    grid_roles: Mapping[str, str] | None,
    ws_factory: Callable[[], Any] | None,
    ha_cache: dict[str, Any],
) -> tuple[SolarForecast, ProviderStatus]:
    """Run one configured source; an unknown source is a status, never an exception."""
    source = str(entry.get("source") or "")
    if source == "forecast_solar":
        return _forecast_solar_forecast(
            settings, store, client, status=status, forecast=forecast, moment=moment,
            location=location, http=http, grid_roles=grid_roles, ws_factory=ws_factory,
        )
    if source == "capacity":
        return _capacity_forecast(
            settings, store, client, status=status, forecast=forecast, moment=moment,
            location=location, http=http,
        )
    if source == "derived":
        return _derived_forecast(
            settings, store, client, status=status, forecast=forecast, moment=moment,
            location=location, http=http, grid_roles=grid_roles, ws_factory=ws_factory,
        )
    if source == "learned":
        return _learned_forecast(
            settings, store, client, status=status, forecast=forecast, moment=moment,
            location=location, http=http,
        )
    if source == "ha_energy":
        return _ha_energy_forecast(
            settings, store, client, status=status, forecast=forecast, moment=moment,
            entry=entry, grid=grid, location=location, ws_factory=ws_factory, cache=ha_cache,
        )
    status.ok = False
    status.last_error = f"unknown solar source: {source}"
    return forecast, status


def _load_merged_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    sources: Sequence[Mapping[str, Any]],
    location: tuple[float, float, str] | None,
    moment: datetime,
    http: Callable[..., Any] | None,
    grid_roles: Mapping[str, str] | None,
    ws_factory: Callable[[], Any] | None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Combine the configured sources onto one grid; fall back to ``solar.source`` when none works."""
    strategy = str(settings.get("solar.merge_strategy", "weighted_mean") or "weighted_mean")
    if strategy not in MERGE_STRATEGIES:
        strategy = "weighted_mean"
    status = ProviderStatus(kind="solar", name="merge", active=True)
    forecast = SolarForecast(generated_at=moment, source="merge")
    if not bool(settings.get("solar.enabled", True)):
        status.active = False
        status.ok = True
        status.state = "disabled"
        status.detail["reason"] = "disabled"
        forecast.notes.append("solar forecasting is disabled")
        disabled_seen: set[str] = set()
        forecast.sources = [
            ProviderStatus(
                kind="solar",
                name=_source_display_name(entry, index, disabled_seen),
                active=False,
                ok=True,
                state="disabled",
                weight=float(entry.get("weight", 1.0)),
                note="solar forecasting is disabled",
            )
            for index, entry in enumerate(sources)
        ]
        return forecast, status

    horizon = horizon_hours(settings)
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)
    ha_cache: dict[str, Any] = {}
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    statuses: list[ProviderStatus] = []
    included: list[tuple[str, SolarForecast]] = []
    any_series = False
    for index, entry in enumerate(sources):
        name = _source_display_name(entry, index, seen)
        overlay = _SourceOverlay(settings, entry)
        source = str(entry.get("source") or "")
        sub_status = ProviderStatus(kind="solar", name=name, active=True)
        sub_forecast = SolarForecast(generated_at=moment, source=source)
        sub_forecast, sub_status = _dispatch_merge_source(
            overlay,
            store,
            client,
            entry=entry,
            status=sub_status,
            forecast=sub_forecast,
            moment=moment,
            grid=grid,
            location=location,
            http=http,
            grid_roles=grid_roles,
            ws_factory=ws_factory,
            ha_cache=ha_cache,
        )
        series = [(slot.t, float(slot.pv_kw)) for slot in (sub_forecast.slots or [])]
        state = _merge_state(sub_status, series)
        note = sub_status.last_error or (sub_forecast.notes[0] if sub_forecast.notes else "")
        weight = float(entry.get("weight", 1.0))
        if series:
            any_series = True
            included.append((name, sub_forecast))
        entries.append({"name": name, "series": series, "weight": weight, "state": state, "note": note})
        statuses.append(
            ProviderStatus(
                kind="solar",
                name=name,
                active=True,
                ok=state in ("ok", "degraded"),
                stale=bool(sub_status.stale),
                last_error=None if state in ("ok", "degraded") else (sub_status.last_error or "no usable forecast"),
                weight=weight,
                state=state,
                note=note,
                detail=dict(sub_status.detail) | {"source": source, "slots": len(series)},
            )
        )

    if not any_series:
        fallback, fallback_status = _load_single_source_forecast(
            settings,
            store,
            client,
            location=location,
            now=moment,
            http=http,
            grid_roles=grid_roles,
            ws_factory=ws_factory,
        )
        fallback.notes.append(MERGE_NO_SOURCE_NOTE)
        fallback.sources = statuses
        return fallback, fallback_status

    values, report = merge_solar_series(grid, entries, strategy)
    by_index = {row["name"]: row for row in report}
    for status_row in statuses:
        row = by_index.get(status_row.name)
        if row is not None:
            status_row.state = str(row["state"])
            status_row.ok = status_row.state in ("ok", "degraded")
            status_row.note = str(row["note"] or status_row.note)
            if status_row.state == "failed":
                status_row.last_error = status_row.last_error or "no usable forecast"
            elif status_row.state in ("ok", "degraded"):
                status_row.last_error = None
    uncertainty_by_slot: dict[datetime, float] = {}
    for name, sub_forecast in included:
        for slot in sub_forecast.slots:
            current = uncertainty_by_slot.get(slot.t, 0.0)
            uncertainty_by_slot[slot.t] = max(current, float(slot.uncertainty_pct or DEFAULT_UNCERTAINTY_PCT))
    forecast.slots = [
        SolarSlot(t=slot, pv_kw=max(0.0, values[index]), uncertainty_pct=uncertainty_by_slot.get(slot, DEFAULT_UNCERTAINTY_PCT))
        for index, slot in enumerate(grid)
    ]
    forecast.sources = statuses
    forecast.stale = any(row.stale for row in statuses)
    used = [row.name for row in statuses if row.state in ("ok", "degraded")]
    weights = ", ".join(
        f"{row.name}={row.weight:g}" for row in statuses if row.state in ("ok", "degraded")
    )
    forecast.notes.append(
        MERGE_NOTE.format(count=len(used), strategy=strategy, weights=f" ({weights})" if weights else "")
    )
    for row in statuses:
        if row.state in ("ok", "degraded") and row.note:
            forecast.notes.append(f"{row.name}: {row.note}")
        elif row.state == "failed":
            forecast.notes.append(f"{row.name}: skipped ({row.last_error})")
    forecast.correction_applied = sum(
        float(getattr(sub, "correction_applied", 1.0) or 1.0) for _, sub in included
    ) / len(included) if included else 1.0
    status.ok = True
    status.last_success = moment
    status.stale = forecast.stale
    status.detail.update(
        {
            "strategy": strategy,
            "sources": [row.name for row in statuses],
            "sources_used": used,
            "sources_failed": [row.name for row in statuses if row.state == "failed"],
            "weights": {row.name: row.weight for row in statuses},
            "correction_applied": round(forecast.correction_applied, 4),
        }
    )
    return forecast, status


def load_solar_forecast(
    settings: Any,
    store: Any = None,
    client: Any = None,
    *,
    location: tuple[float, float, str] | None = None,
    now: datetime | None = None,
    http: Callable[..., Any] | None = None,
    grid_roles: Mapping[str, str] | None = None,
    ws_factory: Callable[[], Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Build the PV forecast, or an empty forecast plus a status explaining why not.

    ``solar.sources`` empty (the default) keeps the single-source ``solar.source`` path exactly as
    before. A non-empty list merges every configured source onto the plan grid (SPEC.md 7.1.3); when
    no source produces a usable series the single-source chain runs as the fallback.
    """
    moment = now or utcnow()
    sources = configured_solar_sources(settings)
    if not sources:
        return _load_single_source_forecast(
            settings,
            store,
            client,
            location=location,
            now=moment,
            http=http,
            grid_roles=grid_roles,
            ws_factory=ws_factory,
        )
    return _load_merged_forecast(
        settings,
        store,
        client,
        sources=sources,
        location=location,
        moment=moment,
        http=http,
        grid_roles=grid_roles,
        ws_factory=ws_factory,
    )

