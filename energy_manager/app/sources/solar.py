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
