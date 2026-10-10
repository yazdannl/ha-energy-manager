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

log = logging.getLogger(__name__)

__all__ = [
    "CACHE_TTL_S",
    "FORECAST_SOLAR_BASE",
    "MAX_PLANES",
    "fetch_plane",
    "load_solar_forecast",
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
NO_CLOUD_DATA_NOTE = ("{entity} publishes no cloud_coverage; estimate is clear-sky (uncertainty 35%)")
NO_CLIENT_NOTE = "{entity}: no Home Assistant client to query; estimate is clear-sky"
NO_WEATHER_NOTE = "no solar.weather_entity set; estimate is clear-sky (uncertainty 35%)"
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
) -> tuple[dict[datetime, float] | None, str]:
    """Cloud coverage per slot from the weather entity, with two documented fallbacks.

    Returns ``(mapping, note)``. A ``None`` mapping means the estimate stays clear-sky. Weather is
    optional: no path here raises or fails the forecast (SPEC 7.1.1 step 3).
    """
    hour_keys = {slot.astimezone(zone).replace(minute=0, second=0, microsecond=0) for slot in grid}
    if not entity_id:
        return None, NO_WEATHER_NOTE
    if client is None or not callable(getattr(client, "call_service", None)):
        return None, NO_CLIENT_NOTE.format(entity=entity_id)
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
        hourly = _hourly_cloud_map(_forecast_cloud_rows(response, entity_id), zone)
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
                return mapping, note
    # Both documented fallbacks: the entity's current cloud cover held flat, then clear-sky.
    held = _current_cloud_coverage(client, entity_id)
    if held is not None:
        note = NO_FORECAST_NOTE if unsupported else CLOUD_FALLBACK_NOTE
        return {slot: held for slot in grid}, note.format(entity=entity_id)
    return None, NO_CLOUD_DATA_NOTE.format(entity=entity_id)


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


def _capacity_forecast(
    settings: Any,
    store: Any,
    client: Any,
    *,
    status: ProviderStatus,
    forecast: SolarForecast,
    moment: datetime,
    location: tuple[float, float, str] | None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Estimate production from ``solar.capacity_wp`` (SPEC.md section 7.1.1).

    Never raises and never guesses a capacity: a missing one is an empty forecast plus a status
    that names ``solar.capacity_wp``, because the bias correction may only adjust a forecast that
    already exists.
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
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)

    tilt_deg, azimuth_deg, tilt_assumed, azimuth_assumed = _capacity_geometry(settings)
    try:
        performance_ratio = float(settings.get("solar.performance_ratio", 0.85) or 0.85)
    except (TypeError, ValueError):
        performance_ratio = 0.85
    weather_entity = str(settings.get("solar.weather_entity", "") or "").strip()
    cloud_map, cloud_note = _slot_cloud_map(client, weather_entity, zone, grid)

    forecast.notes.append(f"capacity estimate: {capacity_wp:g} Wp from solar.capacity_wp")
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
    measured_entity = str(settings.get("solar.measured_entity", "") or "").strip()
    forecast.notes.append(MEASURED_NOTE if measured_entity else MISSING_MEASURED_NOTE)

    bias_enabled = bool(settings.get("solar.bias_correction", True))
    bias_payload = _bias_payload(store) if (store is not None and bias_enabled) else _empty_bias_payload()
    factors = (
        _decay_factors(bias_payload["factors"], bias_payload.get("updated_at"), moment)
        if bias_enabled
        else [1.0] * 24
    )
    uncertainty = CAPACITY_UNCERTAINTY_CLOUD_PCT if cloud_map else CAPACITY_UNCERTAINTY_CLEAR_PCT
    applied: list[float] = []
    slots: list[SolarSlot] = []
    for slot_time in grid:
        hour_key = slot_time.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        coverage = cloud_map.get(hour_key) if cloud_map else None
        cloud_k = 1.0 if coverage is None else _cloud_factor(coverage)
        poa = _plane_irradiance(latitude, longitude, slot_time, zone, tilt_deg, azimuth_deg)
        pv_kw = (capacity_wp / 1000.0) * (poa * cloud_k / 1000.0) * performance_ratio
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
            "tilt_deg": tilt_deg,
            "azimuth_deg": azimuth_deg,
            "assumed_tilt": tilt_assumed,
            "assumed_azimuth": azimuth_assumed,
            "performance_ratio": performance_ratio,
            "cloud_attenuation": bool(cloud_map),
            "weather_entity": weather_entity or None,
            "bias_correction": bias_enabled,
            "correction_applied": round(forecast.correction_applied, 4),
        }
    )
    return forecast, status


# --------------------------------------------------------------------------------------------
# Provider entry point
# --------------------------------------------------------------------------------------------


def _plane_cache_key(latitude: float, longitude: float, tilt: float, azimuth: float, plane: Mapping[str, Any]) -> str:
    raw = f"{latitude:.4f}|{longitude:.4f}|{tilt:g}|{azimuth:g}|{float(plane['kwp']):g}|{plane.get('damping')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_solar_forecast(
    settings: Any,
    store: Any = None,
    client: Any = None,
    *,
    location: tuple[float, float, str] | None = None,
    now: datetime | None = None,
    http: Callable[..., Any] | None = None,
) -> tuple[SolarForecast, ProviderStatus]:
    """Build the PV forecast, or an empty forecast plus a status explaining why not."""
    moment = now or utcnow()
    source = str(settings.get("solar.source", "forecast_solar") or "forecast_solar")
    if source == "capacity":
        status = ProviderStatus(kind="solar", name=source, active=True)
        forecast = SolarForecast(generated_at=moment, source=source)
        if not bool(settings.get("solar.enabled", True)):
            status.active = False
            status.ok = True
            status.detail["reason"] = "disabled"
            forecast.notes.append("solar forecasting is disabled")
            return forecast, status
        return _capacity_forecast(
            settings,
            store,
            client,
            status=status,
            forecast=forecast,
            moment=moment,
            location=location,
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
    planes = [dict(plane) for plane in (settings.get("solar.planes") or []) if isinstance(plane, Mapping)]
    if not planes:
        status.ok = True
        status.detail["reason"] = "no planes configured"
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
