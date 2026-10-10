"""Load forecast and learning (SPEC.md section 7.2).

Source priority (tier 1 wins):

1. long-term statistics (``haws.statistics_during_period``, 60 days, hourly);
2. recorder history (``GET /api/history/period``);
3. derived from mapped power sensors: ``load = grid + pv + discharge - charge`` (grid roles from
   ``grid.power_entity`` / ``grid.import_entity`` / ``grid.export_entity``, or an explicit mapping);
4. fallback flat profile from ``load.base_kwh_per_day`` spread by a generic residential shape.

With ``load.temperature_entity`` mapped, :func:`collect_history` also fetches that sensor's hourly
history and carries it to the regression fit.

Model: median per (weekend|weekday, hour) over the last 8 weeks, level-scaled by
``recent_7d_mean / profile_mean`` (clamped ``[0.5, 2.0]``), EWMA bias per hour bucket over the last
7 days, then an optional least-squares temperature slope. The regression is stored with the profile;
at prediction time only its slope is applied to the profile level
(``load = profile + b * (T - T_mean)``) — the SPEC.md section 7.2 formula ``a + b*T`` carries the
level that the learned profile already represents. Hourly values are linearly interpolated to 15-min
slots. Cold start reports ``confidence: low``.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..models import (
    LoadForecast,
    LoadSlot,
    ProviderStatus,
    clamp,
    from_iso,
    quarter_floor,
    slot_range,
    utcnow,
)
from . import horizon_hours

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SHAPE",
    "HistoryResult",
    "LoadSamples",
    "build_profile",
    "collect_history",
    "collect_temperatures",
    "load_forecast",
    "load_profile",
    "save_profile",
    "update_load_profile",
]

PROFILE_KIND = "load"
PROFILE_KEY = "profile"
SLOT_MINUTES = 15
WINDOW_DAYS = 56
MIN_SAMPLES = 24
EWMA_ALPHA = 0.2
BIAS_MIN, BIAS_MAX = 0.5, 2.0
SCALE_MIN, SCALE_MAX = 0.5, 2.0
MIN_TEMPERATURE_R2 = 0.05

#: Generic residential shape (24 relative hourly weights, normalised to 24 in code).
DEFAULT_SHAPE = (
    0.75, 0.65, 0.60, 0.60, 0.65, 0.85, 1.15, 1.35, 1.25, 1.05, 0.95, 0.95,
    0.95, 0.95, 0.95, 1.05, 1.30, 1.55, 1.55, 1.35, 1.15, 1.00, 0.90, 0.80,
)

_POWER_TO_KW = {"w": 1e-3, "kw": 1.0, "mw": 1e3}


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


def power_to_kw(value: Any, unit: str | None) -> float | None:
    """Convert a power reading to kW; ``None`` when the unit is missing or unsupported."""
    if unit is None:
        return None
    factor = _POWER_TO_KW.get(str(unit).strip().lower())
    if factor is None:
        return None
    try:
        return float(value) * factor
    except (TypeError, ValueError):
        return None


def temperature_to_c(value: Any, unit: str | None) -> float | None:
    """Convert a temperature reading to °C; ``None`` when the unit is unsupported."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if unit is None:
        return None
    key = str(unit).strip().lower().replace("°", "").replace(" ", "")
    if key in ("c", "celsius"):
        return number
    if key in ("f", "fahrenheit"):
        return (number - 32.0) / 1.8
    if key in ("k", "kelvin"):
        return number - 273.15
    return None


# --------------------------------------------------------------------------------------------
# History collection (tiers 1-3)
# --------------------------------------------------------------------------------------------


class LoadSamples(list):
    """Hourly ``(UTC stamp, kW)`` load samples that optionally carry the aligned outdoor
    temperature series.

    ``load.temperature_entity`` feeds the optional regression term; :func:`collect_history` puts the
    fetched temperatures here so the caller can hand ``history.samples`` straight to
    :func:`update_load_profile` without a new parameter (a plain list behaves exactly as before,
    with no temperature term). ponytail: one duck-typed attribute instead of a second return value.
    """

    #: ``{hour-UTC: °C}``; empty when the knob is unset or the sensor has no usable history.
    temperatures: dict[datetime, float]

    def __init__(
        self,
        samples: Iterable[tuple[datetime, float]] = (),
        temperatures: Mapping[datetime, float] | None = None,
    ) -> None:
        super().__init__(samples)
        self.temperatures = dict(temperatures or {})


@dataclass
class HistoryResult:
    samples: LoadSamples = field(default_factory=LoadSamples)  # (hour, kW)
    source: str = "fallback"
    status: ProviderStatus = field(default_factory=lambda: ProviderStatus(kind="load", name="fallback"))
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.samples)


def _entity_unit(client: Any, entity_id: str) -> str | None:
    """Return the declared unit for one entity, if it is reachable."""
    if client is None or not entity_id:
        return None
    try:
        state = client.get_state(entity_id)
    except Exception:  # pragma: no cover - transport errors are the engine's to report
        return None
    attributes = state.get("attributes") or {}
    return attributes.get("unit_of_measurement")


def _hour_key(moment: datetime) -> datetime:
    """UTC hour bucket for a reading: the key both the temperature series and the fit use."""
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _hourly_means(samples: Iterable[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    buckets: dict[datetime, list[float]] = {}
    for moment, value in samples:
        key = _hour_key(moment)
        buckets.setdefault(key, []).append(value)
    return [(key, statistics.fmean(values)) for key, values in sorted(buckets.items())]


def _statistics_samples(settings: Any, client: Any, ws: Any, now: datetime, days: int) -> tuple[list[tuple[datetime, float]], str]:
    entity = str(settings.get("load.entity", "") or "")
    if not entity:
        return [], "no load.entity mapped"
    if ws is None:
        return [], "no WebSocket client for statistics"
    unit = _entity_unit(client, entity)
    if power_to_kw(1.0, unit) is None:
        return [], f"load entity unit {unit!r} is not a power unit"
    data = ws.statistics_during_period(
        [entity], now - timedelta(days=days), now, period="hour", types=["mean"]
    )
    samples: list[tuple[datetime, float]] = []
    for entry in (data or {}).get(entity, []) or []:
        mean = entry.get("mean")
        if mean is None:
            continue
        moment = from_iso(entry.get("start"))
        value = power_to_kw(mean, unit)
        if moment is not None and value is not None:
            samples.append((moment, value))
    samples.sort(key=lambda item: item[0])
    return samples, "" if samples else "statistics returned no rows"


def _recorder_samples(client: Any, entity_id: str, now: datetime, days: int) -> tuple[list[tuple[datetime, float]], str]:
    if client is None or not entity_id:
        return [], "no entity or client"
    rows = client.get_history(entity_id, now - timedelta(days=days), now)
    raw: list[tuple[datetime, float]] = []
    for entity_rows in rows or []:
        for state in entity_rows or []:
            attributes = state.get("attributes") or {}
            value = power_to_kw(state.get("state"), attributes.get("unit_of_measurement"))
            moment = from_iso(state.get("last_changed") or state.get("last_updated"))
            if value is not None and moment is not None:
                raw.append((moment, value))
    return _hourly_means(raw), "" if raw else "recorder returned no usable rows"


def collect_temperatures(
    client: Any, entity_id: str, *, now: datetime | None = None, days: int = 14
) -> list[tuple[datetime, float]]:
    """Hourly outdoor temperature history for the optional regression term (section 7.2)."""
    if client is None or not entity_id:
        return []
    moment = now or utcnow()
    rows = client.get_history(entity_id, moment - timedelta(days=days), moment)
    raw: list[tuple[datetime, float]] = []
    for entity_rows in rows or []:
        for state in entity_rows or []:
            attributes = state.get("attributes") or {}
            value = temperature_to_c(state.get("state"), attributes.get("unit_of_measurement"))
            stamp = from_iso(state.get("last_changed") or state.get("last_updated"))
            if value is not None and stamp is not None:
                raw.append((stamp, value))
    return _hourly_means(raw)


def _configured_batteries(settings: Any) -> list[dict[str, Any]]:
    """Enabled battery configs from a :class:`Settings` (or a plain mapping) as ``{mapping}`` dicts."""
    loader = getattr(settings, "battery_list", None)
    if callable(loader):
        try:
            return [
                {"mapping": dict(getattr(battery, "mapping", None) or {})}
                for battery in loader()
                if getattr(battery, "enabled", True)
            ]
        except Exception:  # noqa: BLE001 - a broken config must not crash the learner
            return []
    raw = settings.get("batteries") if hasattr(settings, "get") else None
    if not isinstance(raw, list):
        return []
    return [
        {"mapping": dict(entry.get("mapping") or {})}
        for entry in raw
        if isinstance(entry, Mapping) and bool(entry.get("enabled", True))
    ]


def _map_battery_flows(settings: Any, roles: dict[str, str]) -> str:
    """Add the battery's AC flow roles to ``roles``; return a refusal reason instead when impossible.

    The derived tier may only learn intervals whose battery flow it can subtract (models.py
    convention: positive = charging). A controllable battery therefore needs
    ``charge_power_entity`` + ``discharge_power_entity`` (the measured AC pair) or a signed
    ``power_setpoint_entity``; otherwise the tier is refused rather than learning cycling as load.
    """
    batteries = _configured_batteries(settings)
    if not batteries:
        return ""
    if len(batteries) > 1:
        return "derived load supports one battery; map charge/discharge roles explicitly"
    mapping = batteries[0]["mapping"]
    charge = mapping.get("charge_power_entity")
    discharge = mapping.get("discharge_power_entity")
    if charge and discharge:
        roles["charge"], roles["discharge"] = str(charge), str(discharge)
        return ""
    signed = mapping.get("power_setpoint_entity")
    if signed:
        roles["battery"] = str(signed)
        return ""
    return (
        "the configured battery has no AC flow mapping (power_setpoint_entity or "
        "charge_power_entity/discharge_power_entity); refusing to learn its cycling as load"
    )


def _derived_samples(
    client: Any,
    derived_entities: Mapping[str, str] | None,
    now: datetime,
    days: int,
) -> tuple[list[tuple[datetime, float]], str]:
    """Gross household load from power sensors (SPEC.md section 7.2, tier 3).

    ``load = grid_import - grid_export + pv + discharge - charge`` (kW, AC side; the battery pair
    follows models.py, positive = charging). An interval is learned only when every *required*
    series has a sample for that stamp: a missing pv or battery-flow stamp is skipped, never read as
    zero. The tier is refused when ``pv`` is unmapped (net grid import is not household load);
    whether a configured battery's flow had to be mapped is enforced by :func:`collect_history` via
    :func:`_map_battery_flows`.
    """
    if not derived_entities:
        return [], "no derived entity mapping supplied"
    series: dict[str, dict[datetime, float]] = {}
    for role, entity_id in derived_entities.items():
        samples, _ = _recorder_samples(client, entity_id, now, days)
        if samples:
            series[role] = dict(samples)
    if "pv" not in series:
        return [], (
            "derived load needs a pv series (solar.measured_entity or derived_entities['pv']): "
            "without it the learned load is net grid import, not gross consumption"
        )
    missing_flow = [
        role for role in ("battery", "charge", "discharge") if role in derived_entities and role not in series
    ]
    if missing_flow:
        return [], (
            f"battery flow data ({', '.join(missing_flow)}) is unavailable for the learning window; "
            "refusing to learn its cycling as household load"
        )
    grid: dict[datetime, float] = {}
    if "grid_power" in series:
        grid = dict(series["grid_power"])
    elif "grid_import" in series or "grid_export" in series:
        stamps = set(series.get("grid_import", {})) | set(series.get("grid_export", {}))
        grid = {
            stamp: series.get("grid_import", {}).get(stamp, 0.0)
            - series.get("grid_export", {}).get(stamp, 0.0)
            for stamp in stamps
        }
    if not grid:
        return [], "derived load needs grid_power or grid_import/grid_export"
    pv = series["pv"]
    battery = series.get("battery")
    charge, discharge = series.get("charge"), series.get("discharge")
    has_flow = battery is not None or charge is not None or discharge is not None
    out: list[tuple[datetime, float]] = []
    for stamp, grid_kw in sorted(grid.items()):
        if stamp not in pv:
            continue  # missing PV at this stamp: unknown, not zero
        if has_flow and not (
            (battery is not None and stamp in battery)
            or (charge is not None and stamp in charge)
            or (discharge is not None and stamp in discharge)
        ):
            continue  # battery cycling with no flow sample: never learn it as load
        load = grid_kw + pv[stamp]
        if battery is not None:
            load -= battery[stamp]  # AC-side battery power: positive = charging
        else:
            load += (discharge or {}).get(stamp, 0.0) - (charge or {}).get(stamp, 0.0)
        out.append((stamp, max(0.0, load)))
    return out, "" if out else "derived series is empty"


def _temperature_samples(
    settings: Any, client: Any, moment: datetime, days: int
) -> tuple[dict[datetime, float], str]:
    """Hourly temperatures for the regression window (``load.temperature_entity``, section 7.2).

    Returns ``({}, "")`` when the knob is unset (nothing is fetched, exactly the old behaviour) and
    a note instead of raising when the sensor is missing or broken: a bad thermometer must never
    fail load learning.
    """
    entity = str(settings.get("load.temperature_entity", "") or "")
    if not entity:
        return {}, ""
    try:
        rows = collect_temperatures(client, entity, now=moment, days=days)
    except Exception as exc:  # noqa: BLE001 - any transport/parse failure degrades the term
        return {}, f"temperature: {type(exc).__name__}: {exc}"
    temperatures = {stamp: float(value) for stamp, value in rows}
    if not temperatures:
        return {}, f"temperature: no usable readings from {entity} in the last {days} days"
    return temperatures, ""


def collect_history(
    settings: Any,
    client: Any,
    *,
    ws: Any = None,
    now: datetime | None = None,
    derived_entities: Mapping[str, str] | None = None,
    days: int = 60,
) -> HistoryResult:
    """Collect hourly load samples from the highest-priority available source.

    ``derived_entities`` maps ``grid_power`` or ``grid_import``/``grid_export`` (plus optional
    ``pv``, ``battery``, ``charge``, ``discharge``) to entity ids; explicit roles from the engine's
    user-confirmed mapping win, and the grid roles otherwise fall back to ``grid.power_entity`` /
    ``grid.import_entity`` / ``grid.export_entity`` and ``pv`` to ``solar.measured_entity``
    (SPEC.md section 3). A configured battery's AC flow roles are added from its own mapping
    (``charge_power_entity``/``discharge_power_entity``, else the signed ``power_setpoint_entity``);
    when that is impossible the derived tier is refused with a note instead of learning battery
    cycling as household load. ``pv`` is always required, and intervals lacking a required sample
    are skipped (``load.entity`` keeps its own tier).

    When ``load.temperature_entity`` is mapped its hourly history is fetched over the same window and
    attached to ``result.samples`` (see :class:`LoadSamples`) for the regression term; an empty or
    broken sensor is reported in ``notes`` and leaves the no-temperature behaviour untouched.
    """
    moment = now or utcnow()
    mode = str(settings.get("load.mode", "auto") or "auto")
    entity = str(settings.get("load.entity", "") or "")
    derived_roles: dict[str, str] = dict(derived_entities or {})
    for role, key in (
        ("grid_power", "grid.power_entity"),
        ("grid_import", "grid.import_entity"),
        ("grid_export", "grid.export_entity"),
        ("pv", "solar.measured_entity"),
    ):
        mapped = str(settings.get(key, "") or "")
        if mapped:
            derived_roles.setdefault(role, mapped)
    battery_note = ""
    if not any(role in derived_roles for role in ("battery", "charge", "discharge")):
        battery_note = _map_battery_flows(settings, derived_roles)
    notes: list[str] = []
    temperatures, temperature_note = _temperature_samples(settings, client, moment, days)

    def result(samples: Iterable[tuple[datetime, float]], source: str, status: ProviderStatus) -> HistoryResult:
        if temperature_note:
            notes.append(temperature_note)
        return HistoryResult(LoadSamples(samples, temperatures), source, status, notes)

    if mode in ("auto", "entity") and entity:
        samples, note = _statistics_samples(settings, client, ws, moment, days)
        if samples:
            return result(samples, "statistics", ProviderStatus(kind="load", name="statistics", ok=True, last_success=moment, detail={"samples": len(samples)}))
        if note:
            notes.append(f"statistics: {note}")
        samples, note = _recorder_samples(client, entity, moment, days)
        if samples:
            return result(samples, "history", ProviderStatus(kind="load", name="history", ok=True, last_success=moment, detail={"samples": len(samples)}))
        if note:
            notes.append(f"history: {note}")
    if mode in ("auto", "derived") and (mode == "derived" or not entity):
        if battery_note:
            notes.append(f"derived: {battery_note}")
        else:
            samples, note = _derived_samples(client, derived_roles, moment, days)
            if samples:
                return result(samples, "derived", ProviderStatus(kind="load", name="derived", ok=True, last_success=moment, detail={"samples": len(samples)}))
            if note:
                notes.append(f"derived: {note}")
    if mode == "entity" and not entity:
        notes.append("load.mode=entity but no load.entity is mapped")
    notes.append("falling back to the flat profile")
    return result([], "fallback", ProviderStatus(kind="load", name="fallback", ok=True, detail={"reason": "no history"}))


# --------------------------------------------------------------------------------------------
# Profile learning
# --------------------------------------------------------------------------------------------


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bucket_index(moment_local: datetime) -> int:
    return (1 if moment_local.weekday() >= 5 else 0) * 24 + moment_local.hour


def _fit_temperature(
    samples: Sequence[tuple[datetime, float]], temperatures: Mapping[datetime, float]
) -> dict[str, float | None]:
    pairs: list[tuple[float, float]] = []
    for stamp, load_kw in samples:
        key = _hour_key(stamp)
        temp = temperatures.get(key)
        if temp is not None:
            pairs.append((float(temp), float(load_kw)))
    if len(pairs) < 8:
        return {"a": None, "b": 0.0, "r2": 0.0, "t_mean": None}
    temps = [pair[0] for pair in pairs]
    loads = [pair[1] for pair in pairs]
    temp_mean = statistics.fmean(temps)
    load_mean = statistics.fmean(loads)
    covariance = sum((t - temp_mean) * (l - load_mean) for t, l in pairs)
    variance = sum((t - temp_mean) ** 2 for t in temps)
    if variance <= 0:
        return {"a": None, "b": 0.0, "r2": 0.0, "t_mean": temp_mean}
    slope = covariance / variance
    intercept = load_mean - slope * temp_mean
    total = sum((l - load_mean) ** 2 for l in loads)
    residual = sum((l - (intercept + slope * t)) ** 2 for t, l in pairs)
    r2 = 0.0 if total <= 0 else max(0.0, 1.0 - residual / total)
    return {"a": intercept, "b": slope, "r2": r2, "t_mean": temp_mean}


def build_profile(
    samples: Iterable[tuple[datetime, float]],
    *,
    now: datetime | None = None,
    tz: str | ZoneInfo | None = None,
    temperatures: Mapping[datetime, float] | None = None,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """Learn the 48-bucket median profile, level scale, EWMA bias and temperature slope.

    ``temperatures`` defaults to the series carried by a :class:`LoadSamples` (the collection path);
    a plain list therefore learns exactly the no-temperature profile it always did.
    """
    moment = now or utcnow()
    zone = _zone(tz)
    cutoff = moment - timedelta(days=window_days)
    usable = [(stamp, float(value)) for stamp, value in samples if stamp >= cutoff]
    if temperatures is None:  # the collection path attaches it to the sample list (see LoadSamples)
        temperatures = getattr(samples, "temperatures", None) or {}
    buckets: list[list[float]] = [[] for _ in range(48)]
    for stamp, value in usable:
        buckets[_bucket_index(stamp.astimezone(zone))].append(value)

    medians = [_quantile(values, 0.5) if values else 0.0 for values in buckets]
    p10s = [_quantile(values, 0.1) if values else 0.0 for values in buckets]
    p90s = [_quantile(values, 0.9) if values else 0.0 for values in buckets]

    weekday_median = statistics.fmean(medians[:24]) if any(medians[:24]) else 0.0
    weekend_median = statistics.fmean(medians[24:]) if any(medians[24:]) else 0.0
    profile_mean = (5.0 * weekday_median + 2.0 * weekend_median) / 7.0

    recent_cutoff = moment - timedelta(days=7)
    recent = [value for stamp, value in usable if stamp >= recent_cutoff]
    recent_mean = statistics.fmean(recent) if recent else profile_mean
    scale = 1.0
    if profile_mean > 0:
        scale = clamp(recent_mean / profile_mean, SCALE_MIN, SCALE_MAX)

    bias = [1.0] * 48
    day_buckets: dict[tuple[Any, int], list[float]] = {}
    for stamp, value in usable:
        if stamp < recent_cutoff:
            continue
        local = stamp.astimezone(zone)
        day_buckets.setdefault((local.date(), _bucket_index(local)), []).append(value)
    for (day, index) in sorted(day_buckets, key=lambda item: item[0]):
        values = day_buckets[(day, index)]
        median = medians[index]
        if median <= 0:
            continue
        ratio = clamp(statistics.fmean(values) / median, BIAS_MIN, BIAS_MAX)
        bias[index] = (1.0 - EWMA_ALPHA) * bias[index] + EWMA_ALPHA * ratio

    days = len({stamp.astimezone(zone).date() for stamp, _ in usable})
    regression = _fit_temperature(usable, temperatures or {})
    return {
        "buckets": [
            {
                "median": round(medians[index], 4),
                "p10": round(p10s[index], 4),
                "p90": round(p90s[index], 4),
                "count": len(buckets[index]),
            }
            for index in range(48)
        ],
        "bias": [round(value, 4) for value in bias],
        "scale": round(scale, 4),
        "profile_mean": round(profile_mean, 4),
        "recent_7d_mean": round(recent_mean, 4),
        "a": regression["a"],
        "b": regression["b"],
        "r2": regression["r2"],
        "t_mean": regression["t_mean"],
        # How many hourly load samples actually met a temperature reading (0 = no term fitted).
        "temperature_hours": len({_hour_key(stamp) for stamp, _ in usable} & set(temperatures)),
        "samples": len(usable),
        "days": days,
        "updated_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def load_profile(store: Any) -> dict[str, Any] | None:
    if store is None:
        return None
    got = store.profile_get(PROFILE_KIND, PROFILE_KEY)
    if not got or not isinstance(got[1], Mapping):
        return None
    return dict(got[1])


def save_profile(store: Any, profile: Mapping[str, Any], now: datetime | None = None) -> None:
    if store is not None:
        store.profile_put(PROFILE_KIND, PROFILE_KEY, dict(profile), now)


def update_load_profile(
    store: Any,
    samples: Iterable[tuple[datetime, float]],
    *,
    now: datetime | None = None,
    tz: str | ZoneInfo | None = None,
    temperatures: Mapping[datetime, float] | None = None,
) -> dict[str, Any]:
    """Learn and persist the load profile from hourly samples.

    When ``samples`` came from :func:`collect_history` and ``load.temperature_entity`` is mapped,
    the aligned temperatures ride along on the list and feed the regression term here.
    """
    moment = now or utcnow()
    profile = build_profile(samples, now=moment, tz=tz, temperatures=temperatures)
    save_profile(store, profile, moment)
    return profile


# --------------------------------------------------------------------------------------------
# Prediction
# --------------------------------------------------------------------------------------------


def _confidence(profile: Mapping[str, Any] | None) -> str:
    if not profile:
        return "low"
    if int(profile.get("samples") or 0) < MIN_SAMPLES * 2:
        return "low"
    if int(profile.get("days") or 0) < 14:
        return "medium"
    return "high"


def _fallback_hourly(base_kwh_per_day: float) -> list[float]:
    total = sum(DEFAULT_SHAPE)
    return [base_kwh_per_day * weight / total for weight in DEFAULT_SHAPE]


def _hourly_values(
    profile: Mapping[str, Any] | None,
    *,
    first_hour: datetime,
    last_hour: datetime,
    zone: ZoneInfo | timezone,
    base_kwh_per_day: float,
    temperature_c: Mapping[datetime, float] | None,
) -> dict[datetime, float]:
    """Predicted kW for every UTC hour in ``[first_hour, last_hour]``."""
    values: dict[datetime, float] = {}
    if not profile:
        shape = _fallback_hourly(base_kwh_per_day)
        cursor = first_hour
        while cursor <= last_hour:
            values[cursor] = shape[cursor.astimezone(zone).hour]
            cursor += timedelta(hours=1)
        return values

    buckets = profile.get("buckets") or [{} for _ in range(48)]
    bias = profile.get("bias") or [1.0] * 48
    scale = float(profile.get("scale") or 1.0)
    slope = float(profile.get("b") or 0.0)
    r2 = float(profile.get("r2") or 0.0)
    t_mean = profile.get("t_mean")
    temperatures = temperature_c or {}
    cursor = first_hour
    while cursor <= last_hour:
        local = cursor.astimezone(zone)
        index = _bucket_index(local)
        entry = buckets[index] if index < len(buckets) else {}
        value = float(entry.get("median") or 0.0) * scale * float(bias[index] if index < len(bias) else 1.0)
        if slope and r2 >= MIN_TEMPERATURE_R2 and t_mean is not None:
            temperature = temperatures.get(cursor)
            if temperature is not None:
                value += slope * (float(temperature) - float(t_mean))
        values[cursor] = max(0.0, value)
        cursor += timedelta(hours=1)
    return values


def load_forecast(
    settings: Any,
    store: Any = None,
    *,
    now: datetime | None = None,
    profile: Mapping[str, Any] | None = None,
    temperature_c: Mapping[datetime, float] | None = None,
    tz: str | ZoneInfo | None = None,
    hours: int | None = None,
) -> tuple[LoadForecast, ProviderStatus]:
    """Build the 15-minute load forecast from the learned profile (or the fallback shape)."""
    moment = now or utcnow()
    horizon = int(hours) if hours else horizon_hours(settings)
    zone = _zone(tz or settings.get("timezone"))
    learned = profile if profile is not None else load_profile(store)
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)
    base_kwh_per_day = float(settings.get("load.base_kwh_per_day", 0.0) or 0.0)

    source = "profile" if learned else "fallback"
    confidence = _confidence(learned)
    if not learned and base_kwh_per_day <= 0:
        source = "fallback-empty"
    hourly = _hourly_values(
        learned,
        first_hour=start.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
        last_hour=(grid[-1].replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)) if grid else start,
        zone=zone,
        base_kwh_per_day=base_kwh_per_day,
        temperature_c=temperature_c,
    )
    buckets = (learned or {}).get("buckets") or []
    slots: list[LoadSlot] = []
    for slot_time in grid:
        hour_key = slot_time.replace(minute=0, second=0, microsecond=0)
        value = hourly.get(hour_key, 0.0)
        next_key = hour_key + timedelta(hours=1)
        next_value = hourly.get(next_key)
        if next_value is not None:
            fraction = slot_time.minute / 60.0
            value = value + (next_value - value) * fraction
        p10 = p90 = None
        if learned and value > 0:
            index = _bucket_index(slot_time.astimezone(zone))
            entry = buckets[index] if index < len(buckets) else {}
            median = float(entry.get("median") or 0.0)
            if median > 0:
                p10 = value * float(entry.get("p10") or 0.0) / median
                p90 = value * float(entry.get("p90") or 0.0) / median
        if p10 is None or p90 is None:
            p10, p90 = max(0.0, value * 0.7), value * 1.3
        slots.append(LoadSlot(t=slot_time, load_kw=max(0.0, value), p10_kw=p10, p90_kw=p90))

    notes: list[str] = []
    if not learned:
        notes.append("cold start: no learned profile yet")
    forecast = LoadForecast(
        slots=slots,
        generated_at=moment,
        source=source,
        confidence=confidence,
        notes=notes,
    )
    status = ProviderStatus(
        kind="load",
        name=source,
        active=True,
        ok=True,
        last_success=moment,
        detail={"confidence": confidence, "samples": int((learned or {}).get("samples") or 0)},
    )
    return forecast, status
