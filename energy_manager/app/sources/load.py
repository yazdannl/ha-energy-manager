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
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..grid import GRID_ENERGY_ROLES
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
    "collect_pv_export_samples",
    "collect_temperatures",
    "load_forecast",
    "load_profile",
    "pv_export_roles",
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
#: Energy counter units -> kWh. An hourly statistics ``change`` in kWh is also the average
#: power over that hour in kW, which is how an energy meter becomes a power series.
_ENERGY_TO_KWH = {"wh": 1e-3, "kwh": 1.0, "mwh": 1e3}
#: Roles whose readings are instantaneous power; the energy-counter roles are deliberately absent.
_POWER_ROLES = frozenset(
    ("grid_power", "grid_import", "grid_export", "pv", "battery", "charge", "discharge")
)
#: The derived PV tier accepts energy counters too, so the role set is not just the power one.
_PV_EXPORT_ROLES = ("grid_power", "grid_import", "grid_export", "grid_energy_import", "grid_energy_export")


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
    #: Set by the night-only tier, whose samples cover roughly half the day by construction. The
    #: profile builder has to know, because an unobserved bucket there means "not looked at" rather
    #: than "measured zero", and reading it as the latter forecasts zero load all day.
    assumed_daytime_shape: bool = False

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


def _statistic_meta(ws: Any, entity_id: str) -> dict[str, Any]:
    """Statistics metadata for one id; ``{}`` when the recorder has none (or cannot be asked).

    ``recorder/get_statistics_metadata`` is how an external statistic (no state to read) is
    classified. An older Home Assistant without the command degrades to the state unit.
    """
    reader = getattr(ws, "statistics_metadata", None)
    if not callable(reader):
        return {}
    try:
        found = reader([entity_id])
    except Exception:  # noqa: BLE001 - unsupported command / transport: use the state unit
        return {}
    row = found.get(entity_id) if isinstance(found, Mapping) else None
    return dict(row) if isinstance(row, Mapping) else {}


def _statistics_history_samples(
    ws: Any, client: Any, entity_id: str, role: str, now: datetime, days: int
) -> tuple[list[tuple[datetime, float]], str]:
    """Hourly kW samples for one mapped id from long-term statistics (never purged).

    A power statistic contributes its hourly ``mean``; an energy counter contributes its hourly
    ``change`` (kWh), which over that hour is an average power in kW. The statistic id comes from
    the mapping, so an external statistic works without an entity state. A unit that cannot be
    established yields a reason, never a guess: reading an energy counter's ``mean`` as power would
    silently warp every learned profile.
    """
    if ws is None or not entity_id:
        return [], "no WebSocket client for statistics"
    meta = _statistic_meta(ws, entity_id)
    unit_class = str(meta.get("unit_class") or "").strip().lower()
    unit = str(
        meta.get("statistics_unit_of_measurement")
        or meta.get("unit_of_measurement")
        or _entity_unit(client, entity_id)
        or ""
    )
    if unit_class in ("power", "energy"):
        kind = unit_class
    elif role in GRID_ENERGY_ROLES:
        kind = "energy"
    elif role in _POWER_ROLES:
        kind = "power"
    elif meta.get("has_mean") and not meta.get("has_sum"):
        kind = "power"
    elif meta.get("has_sum") and not meta.get("has_mean"):
        kind = "energy"
    else:
        return [], f"statistics metadata for {entity_id!r} is missing or not power/energy"
    if kind == "energy":
        factor = _ENERGY_TO_KWH.get(unit.strip().lower())
        if factor is None:
            return [], f"statistic {entity_id!r} is an energy counter with unusable unit {unit!r}"
        data = ws.statistics_during_period(
            [entity_id], now - timedelta(days=days), now, period="hour", types=["change"]
        )
        samples: list[tuple[datetime, float]] = []
        for entry in (data or {}).get(entity_id, []) or []:
            change = entry.get("change")
            if change is None:
                continue
            start = from_iso(entry.get("start"))
            if start is None:
                continue
            end = from_iso(entry.get("end"))
            hours = (end - start).total_seconds() / 3600.0 if end is not None else 1.0
            if hours <= 0.0:
                hours = 1.0
            try:
                # A reset can surface a negative change; a meter cannot send energy back through
                # its import register, so the row is floor-ed rather than trusted as negative PV.
                samples.append((start, max(0.0, float(change) * factor / hours)))
            except (TypeError, ValueError):
                continue
        return samples, "" if samples else f"statistics returned no rows for {entity_id}"
    factor = _POWER_TO_KW.get(unit.strip().lower())
    if factor is None:
        return [], f"statistic {entity_id!r} is a power sensor with unusable unit {unit!r}"
    data = ws.statistics_during_period(
        [entity_id], now - timedelta(days=days), now, period="hour", types=["mean"]
    )
    samples = []
    for entry in (data or {}).get(entity_id, []) or []:
        mean = entry.get("mean")
        if mean is None:
            continue
        start = from_iso(entry.get("start"))
        try:
            value = float(mean) * factor
        except (TypeError, ValueError):
            continue
        if start is not None:
            samples.append((start, value))
    return samples, "" if samples else f"statistics returned no rows for {entity_id}"


def _history_samples(
    client: Any, ws: Any, entity_id: str, role: str, now: datetime, days: int
) -> tuple[list[tuple[datetime, float]], str]:
    """Hourly kW for one role: long-term statistics when possible, else recorder history."""
    if ws is not None:
        samples, _ = _statistics_history_samples(ws, client, entity_id, role, now, days)
        if samples:
            return samples, ""
    return _recorder_samples(client, entity_id, now, days)


def _grid_from_series(
    series: Mapping[str, Mapping[datetime, float]],
) -> dict[datetime, float]:
    """Signed grid power (kW, positive = import) from whichever grid roles have samples."""
    if series.get("grid_power"):
        return dict(series["grid_power"])
    imports = [values for role in ("grid_import", "grid_energy_import") if (values := series.get(role))]
    exports = [values for role in ("grid_export", "grid_energy_export") if (values := series.get(role))]
    stamps: set[datetime] = set()
    for values in imports + exports:
        stamps.update(values)
    return {
        stamp: sum(values.get(stamp, 0.0) for values in imports)
        - sum(values.get(stamp, 0.0) for values in exports)
        for stamp in stamps
    }


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


#: The sun elevation below which production is zero for any array. Deliberately deeper than the
#: horizon: at 0 degrees a tilted plane can still be lit by diffuse sky, so "the sun is down" is not
#: the same claim as "this array is producing nothing". Six degrees below civil twilight makes the
#: claim safe for a roof at any tilt, which is what lets the night tier drop the pv series entirely.
NIGHT_ELEVATION_MAX_DEG = -6.0


def night_hours(
    stamps: Iterable[datetime], latitude: float, longitude: float
) -> set[datetime]:
    """The subset of ``stamps`` at which no array can be producing power.

    Only the sun is consulted, never a measurement, because this is the condition that lets the caller
    treat pv as exactly zero without having a pv sensor to check it against. The geometry is the same
    one the solar forecast uses, taken from :mod:`app.forecast.pv` rather than re-derived, so "night"
    cannot come to mean two different things inside one add-on.
    """
    from ..forecast import pv

    out: set[datetime] = set()
    latitude_rad = math.radians(latitude)
    for stamp in stamps:
        utc = stamp.astimezone(timezone.utc)
        day_of_year = utc.timetuple().tm_yday
        fractional_hour = utc.hour + utc.minute / 60.0
        declination = pv.declination_rad(day_of_year)
        hour_angle = pv.hour_angle_rad(longitude, day_of_year, fractional_hour)
        if pv.sun_elevation_deg(latitude_rad, declination, hour_angle) <= NIGHT_ELEVATION_MAX_DEG:
            out.add(stamp)
    return out


def _night_load_samples(
    series: Mapping[str, Mapping[datetime, float]],
    latitude: float,
    longitude: float,
) -> list[tuple[datetime, float]]:
    """Gross household load from grid and battery alone, over night hours only (SPEC 7.2 tier 3b).

    ``load = grid - battery`` (kW, AC side; battery positive = charging) with pv omitted because it is
    provably zero, which is the whole point of the tier: the ordinary derived path needs a pv series,
    so an install whose inverter reports nothing can never learn its own consumption. About half of
    every day is night, and those hours are enough to learn a level and a temperature slope.

    What it cannot learn is the *shape* of the day, and it does not pretend to -- the caller fills the
    unobserved buckets from the documented default shape and says so in the forecast notes. A battery
    whose flow is unknown is excluded for the same reason as the derived tier: its cycling would
    otherwise be learned as household load.
    """
    grid = _grid_from_series(series)
    if not grid:
        return []
    battery = series.get("battery")
    charge, discharge = series.get("charge"), series.get("discharge")
    if battery is None and charge is None and discharge is None:
        return []
    nights = night_hours(grid, latitude, longitude)
    out: list[tuple[datetime, float]] = []
    for stamp, grid_kw in sorted(grid.items()):
        if stamp not in nights:
            continue
        if battery is not None and stamp in battery:
            load = float(grid_kw) - float(battery[stamp])
        elif charge is not None or discharge is not None:
            load = (
                float(grid_kw)
                - float((charge or {}).get(stamp, 0.0))
                + float((discharge or {}).get(stamp, 0.0))
            )
        else:
            continue  # a night with no battery flow sample: unknown, never read as zero
        out.append((stamp, max(0.0, load)))
    return out


def _fill_assumed_shape(
    medians: list[float], p10s: list[float], p90s: list[float]
) -> tuple[list[float], list[float], list[float]]:
    """Replace buckets with no samples by the default residential shape, at the observed level.

    Only meaningful for a profile learned from night hours, where an empty bucket is a fact about the
    *observation window* rather than about the house. Two failures follow from skipping it, and the
    second is worse than the first:

    * an empty bucket reads as a median of 0.0 kW, forecasting a literal zero for those hours;
    * ``profile_mean`` is then averaged over a mostly-empty day, so the level ``scale`` is computed
      against a fraction of real consumption and inflates *every* hour by up to its clamp. Measured on
      a synthetic 1 kW house: the night hours, the only ones actually observed, came out at 2.0 kW.

    So this runs inside :func:`build_profile` before the level is derived, not as a patch applied to
    the finished dict afterwards. The magnitude still comes entirely from the observed hours.
    """
    if not any(medians):
        return medians, p10s, p90s
    # The level of the hours that were *observed*, not the mean over all 48 buckets: averaging in the
    # thirty-six empty ones would describe a mostly-empty day, and the filled daytime would then be
    # scaled to a fraction of consumption -- which is the same inflation by a different route.
    observed = statistics.fmean([value for value in medians if value > 0.0])
    empty = [index for index, value in enumerate(medians) if value <= 0.0]
    if not empty:
        return medians, p10s, p90s
    # Chosen so the finished day averages exactly the observed level, rather than normalising by the
    # shape's own mean. Those differ: the shape's mean is taken over all 24 hours, while only the
    # unobserved ones are being filled, and the daytime half of a residential shape sits above its own
    # average. Normalising by the shape mean made a synthetic 1 kW house come out 5.7% high, which the
    # recent-window ``scale`` then clawed back by pushing the *measured* night hours below 1 kW.
    shape_total = sum(DEFAULT_SHAPE[index % 24] for index in empty)
    factor = observed * len(empty) / shape_total if shape_total > 0 else 0.0
    for index in empty:
        value = DEFAULT_SHAPE[index % 24] * factor
        medians[index] = round(value, 4)
        if p10s[index] <= 0.0:
            p10s[index] = round(value * 0.55, 4)
        if p90s[index] <= 0.0:
            p90s[index] = round(value * 1.55, 4)
    return medians, p10s, p90s


def _fetch_derived_series(
    client: Any, derived_entities: Mapping[str, str], now: datetime, days: int, ws: Any = None
) -> dict[str, dict[datetime, float]]:
    """Hourly series for whichever assigned roles report anything (long-term statistics first)."""
    series: dict[str, dict[datetime, float]] = {}
    for role, entity_id in derived_entities.items():
        samples, _ = _history_samples(client, ws, entity_id, role, now, days)
        if samples:
            series[role] = dict(samples)
    return series


def _resolve_location(settings: Any, client: Any) -> tuple[float, float, str] | None:
    """``(latitude, longitude, timezone)`` or ``None``; never raises.

    The night tier runs while the add-on is collecting history, so a missing location must degrade to
    "this tier is unavailable" rather than propagate out of a collection pass.
    """
    if client is None or not callable(getattr(client, "get_config", None)):
        return None
    from ..config import ConfigError, resolve_location
    from ..harest import HAError

    options = {
        "latitude_override": settings.get("latitude_override"),
        "longitude_override": settings.get("longitude_override"),
        "timezone": settings.get("timezone"),
    }
    try:
        return resolve_location(client.get_config(), options)
    except (HAError, ConfigError):
        return None


def _night_derived_samples(
    settings: Any,
    client: Any,
    derived_entities: Mapping[str, str] | None,
    now: datetime,
    days: int,
    ws: Any = None,
) -> tuple[list[tuple[datetime, float]], str]:
    """Night-only gross household load, needing grid and battery but no pv sensor (SPEC 7.2 tier 3b).

    This is the tier that breaks the circularity an install without a pv sensor is stuck in: the
    derived path cannot run without pv, pv cannot be inferred without a load estimate, and neither is
    needed once the sun is down. A missing location is refused explicitly rather than guessed, because
    guessing it would admit daylight hours as if pv were zero and inflate the learned load.
    """
    if not derived_entities:
        return [], "no derived entity mapping supplied"
    series = _fetch_derived_series(client, derived_entities, now, days, ws=ws)
    if not any(role in series for role in ("battery", "charge", "discharge")):
        return [], (
            "night load needs a battery flow series: without it the grid reading includes the "
            "battery's cycling, which is not household consumption"
        )
    if not any(role in series for role in ("grid_power", "grid_import", "grid_export")):
        return [], "night load needs grid_power or grid_import/grid_export"
    location = _resolve_location(settings, client)
    if location is None:
        return [], "night load needs the location, to know when the sun is down"
    latitude, longitude, _zone = location
    samples = _night_load_samples(series, latitude, longitude)
    if not samples:
        return [], "no night hour had both a grid and a battery-flow sample"
    return samples, ""


def _derived_samples(
    client: Any,
    derived_entities: Mapping[str, str] | None,
    now: datetime,
    days: int,
    ws: Any = None,
) -> tuple[list[tuple[datetime, float]], str]:
    """Gross household load from power sensors (SPEC.md section 7.2, tier 3).

    ``load = grid_import - grid_export + pv + discharge - charge`` (kW, AC side; the battery pair
    follows models.py, positive = charging). An interval is learned only when every *required*
    series has a sample for that stamp: a missing pv or battery-flow stamp is skipped, never read as
    zero. The tier is refused when ``pv`` is unmapped (net grid import is not household load);
    whether a configured battery's flow had to be mapped is enforced by :func:`collect_history` via
    :func:`_map_battery_flows`. With a WebSocket client, every series is read from long-term
    statistics first (energy counters included), so the learning window is not the recorder's
    purge window.
    """
    if not derived_entities:
        return [], "no derived entity mapping supplied"
    series = _fetch_derived_series(client, derived_entities, now, days, ws=ws)
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
    grid = _grid_from_series(series)
    if not grid:
        return [], "derived load needs grid_power, grid_import/grid_export or the energy counters"
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


def pv_export_roles(
    settings: Any, defaults: Mapping[str, str] | None = None
) -> tuple[dict[str, str], str]:
    """Resolve the entities the meter-derived PV tier reads: ``(role -> entity, refusal reason)``.

    The grid role comes from ``grid.power_entity`` (or the import/export pair, or the Energy
    dashboard's energy counters via ``defaults``) and the battery's AC flow from its own mapping
    (``charge_power_entity``/``discharge_power_entity``, else the signed ``power_setpoint_entity``).
    A configured battery whose flow cannot be read is a refusal, never a silent omission: its
    discharge would be read as PV. An explicit ``grid.*`` setting replaces ``defaults`` as a whole.
    """
    roles: dict[str, str] = {}
    for role, entity in (defaults or {}).items():
        if role in _PV_EXPORT_ROLES and entity:
            roles[role] = str(entity)
    explicit: dict[str, str] = {}
    for role, key in (
        ("grid_power", "grid.power_entity"),
        ("grid_import", "grid.import_entity"),
        ("grid_export", "grid.export_entity"),
    ):
        mapped = str(settings.get(key, "") or "")
        if mapped:
            explicit[role] = mapped
    if explicit:
        for role in _PV_EXPORT_ROLES:
            roles.pop(role, None)
        roles.update(explicit)
    if not any(role in roles for role in _PV_EXPORT_ROLES):
        return {}, (
            "the derived solar source needs grid.power_entity (or the import/export pair, or a grid"
            " meter in your Energy dashboard): the meter's export is the only place the array is"
            " visible"
        )
    refusal = _map_battery_flows(settings, roles)
    if refusal:
        return {}, refusal
    return roles, ""


def collect_pv_export_samples(
    settings: Any,
    client: Any,
    *,
    ws: Any = None,
    now: datetime | None = None,
    days: int = WINDOW_DAYS,
    roles: Mapping[str, str] | None = None,
) -> tuple[list[tuple[datetime, float]], str]:
    """PV power (kW) seen through the meter's export moments (SPEC.md 7.1.2).

    The sign convention is ``grid = load - pv + p`` (SPEC.md 8.1, ``p > 0`` = charging), so at a
    stamp whose signed grid power is negative the array must be producing at least
    ``-grid + p``. The house load at that moment is *not* subtracted (a PV forecast is exactly what
    is missing to know it), which makes every sample a lower bound and the learned array size
    conservative. Stamps without export carry no information and are skipped, as are stamps where
    the battery flow is unknown. With a WebSocket client, every series is read from long-term
    statistics first (energy counters included), which is why the fit is not limited to the
    recorder's purge window. Returns ``(samples, reason)``; an empty list always carries a reason a
    status can show.
    """
    moment = now or utcnow()
    if roles is None:
        roles, refusal = pv_export_roles(settings)
        if refusal:
            return [], refusal
    series: dict[str, dict[datetime, float]] = {}
    for role, entity_id in roles.items():
        samples, _ = _history_samples(client, ws, entity_id, role, moment, days)
        if samples:
            series[role] = dict(samples)
    grid = _grid_from_series(series)
    if not grid:
        return [], "no grid power history for the learning window"
    battery = series.get("battery")
    charge, discharge = series.get("charge"), series.get("discharge")
    has_flow = battery is not None or charge is not None or discharge is not None
    out: list[tuple[datetime, float]] = []
    for stamp, grid_kw in sorted(grid.items()):
        if grid_kw >= 0.0:
            continue  # importing: the array is hidden behind the house load
        if has_flow and not (
            (battery is not None and stamp in battery)
            or (charge is not None and stamp in charge)
            or (discharge is not None and stamp in discharge)
        ):
            continue  # unknown battery flow: its discharge would read as PV
        net_charge = (
            battery[stamp]
            if battery is not None
            else (charge or {}).get(stamp, 0.0) - (discharge or {}).get(stamp, 0.0)
        )
        pv_kw = -grid_kw + net_charge
        if pv_kw > 0.0:
            out.append((stamp, pv_kw))
    if not out:
        return [], "no export samples in the learning window (the meter never went negative)"
    return out, ""


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
            samples, note = _derived_samples(client, derived_roles, moment, days, ws=ws)
            if samples:
                return result(samples, "derived", ProviderStatus(kind="load", name="derived", ok=True, last_success=moment, detail={"samples": len(samples)}))
            if note:
                notes.append(f"derived: {note}")
        # Night-only fallback: the derived tier needs a pv series, which an install with no inverter
        # reporting has no way to supply, and without it that install can never learn its own load.
        # Night hours are pv-free by geometry, so they need nothing but grid and battery.
        samples, note = _night_derived_samples(settings, client, derived_roles, moment, days, ws=ws)
        if samples:
            return HistoryResult(
                LoadSamples(samples, temperatures),
                "derived-night",
                ProviderStatus(
                    kind="load",
                    name="derived-night",
                    ok=True,
                    last_success=moment,
                    detail={"samples": len(samples), "assumed_daytime_shape": True},
                ),
                notes
                + [
                    f"night load: {len(samples)} night hours calibrated from grid and battery with no "
                    "pv sensor; the daytime shape is the default residential one scaled to that level"
                ],
                assumed_daytime_shape=True,
            )
        if note:
            notes.append(f"night load: {note}")
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
    assumed_daytime_shape: bool = False,
) -> dict[str, Any]:
    """Learn the 48-bucket median profile, level scale, EWMA bias and temperature slope.

    ``temperatures`` defaults to the series carried by a :class:`LoadSamples` (the collection path);
    a plain list therefore learns exactly the no-temperature profile it always did.

    ``assumed_daytime_shape`` fills buckets outside the observation window from the default shape, and
    must be decided *here* rather than by the caller, because the level and the bias both depend on
    the medians it produces.
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
    if assumed_daytime_shape:
        medians, p10s, p90s = _fill_assumed_shape(medians, p10s, p90s)

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
        "assumed_daytime_shape": bool(assumed_daytime_shape),
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
    assumed_daytime_shape: bool = False,
) -> dict[str, Any]:
    """Learn and persist the load profile from hourly samples.

    When ``samples`` came from :func:`collect_history` and ``load.temperature_entity`` is mapped,
    the aligned temperatures ride along on the list and feed the regression term here.

    ``assumed_daytime_shape`` forwards to :func:`build_profile`, which has to make the decision before
    it derives the level: an unobserved bucket is not a measured zero.
    """
    moment = now or utcnow()
    profile = build_profile(
        samples,
        now=moment,
        tz=tz,
        temperatures=temperatures,
        assumed_daytime_shape=assumed_daytime_shape,
    )
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
