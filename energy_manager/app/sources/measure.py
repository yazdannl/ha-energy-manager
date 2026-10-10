"""Measurement sampling (SPEC.md section 7.3).

Every control tick the engine calls :func:`collect`: configured roles are read with **targeted**
``GET /api/states/<entity>`` calls (never the ~855 KB unfiltered ``/api/states`` snapshot,
docs/EXTERNAL_APIS.md) and bucketed to the documented 5-minute resolution. The engine persists
both the measurement rows and the actuals row: ``app/store.py`` is the only writer, so a failed
battery write can never skip a sample and there is no second writer to disagree with.

Battery roles are AC-side, following the :mod:`app.models` convention: **positive = charging,
negative = discharging** (the Zendure driver negates at its own boundary, SPEC.md appendix A).
``actuals["battery_kw"]`` is the sum over enabled batteries under the same convention; ``collect``
reports live values for the current tick, while the engine owns the persisted actuals row.

Role names in ``measurements`` follow :class:`app.models.Measurement`; per-battery roles are suffixed
(``battery_power:<battery id>``, ``battery_soc:<battery id>``, ``battery_pv:<battery id>``,
``battery_output:<battery id>``) so several batteries stay distinguishable.

Single reference for the settings-key -> role mapping resolved by :func:`role_entities`:

============================  ========================
setting key                   sampling role
============================  ========================
``solar.measured_entity``     ``pv_power``
``load.entity``               ``load_power``
``price.entity``              ``price``
``grid.power_entity``         ``grid_power``
``grid.import_entity``        ``grid_import``
``grid.export_entity``        ``grid_export``
battery ``mapping``           ``battery_power:<id>``, ``battery_charge:<id>``,
                              ``battery_discharge:<id>``, ``battery_pv:<id>``,
                              ``battery_output:<id>``, ``battery_soc:<id>``
============================  ========================

A role passed through ``entities=`` (the engine's user-confirmed mapping) always wins over the
settings key for the same role.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping

from ..models import BatteryConfig, Measurement, quarter_floor, utcnow
from . import unit_and_divisor

log = logging.getLogger(__name__)

__all__ = [
    "MEASUREMENT_MINUTES",
    "collect",
    "power_to_w",
    "role_entities",
]

#: SPEC.md section 7.3 resolution: every tick upserts into this bucket instead of a new row.
MEASUREMENT_MINUTES = 5
_POWER_TO_W = {"w": 1.0, "kw": 1e3, "mw": 1e6}


def power_to_w(value: Any, unit: str | None) -> float | None:
    """Convert a power reading to W; ``None`` when the unit is missing or unsupported."""
    if unit is None:
        return None
    factor = _POWER_TO_W.get(str(unit).strip().lower())
    if factor is None:
        return None
    try:
        return float(value) * factor
    except (TypeError, ValueError):
        return None


def _to_percent(value: Any, unit: str | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if unit in (None, "%", "percent"):
        return number
    return None


def _price_eur_per_kwh(value: Any, unit: str | None) -> float | None:
    declared = unit_and_divisor(unit)
    if declared is None:
        return None
    price_unit, divisor = declared
    try:
        number = float(value) / divisor
    except (TypeError, ValueError):
        return None
    if price_unit == "eur_per_mwh":
        return number / 1000.0
    if price_unit == "micro_eur_per_kwh":
        return number / 1_000_000.0
    return number


def role_entities(
    settings: Any,
    batteries: Iterable[BatteryConfig] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the sampling role -> entity id map from settings, batteries and engine overrides.

    ``extra`` (from the engine's confirmed mapping) wins; it may add ``grid_power``, ``grid_import``,
    ``grid_export``, ``pv_power``, ``load_power`` and the derived-load roles used by
    :mod:`app.sources.load`. Grid roles otherwise come from ``grid.power_entity`` /
    ``grid.import_entity`` / ``grid.export_entity``.
    """
    mapping: dict[str, str] = dict(extra or {})
    keys = (
        ("pv_power", "solar.measured_entity"),
        ("load_power", "load.entity"),
        ("price", "price.entity"),
        ("grid_power", "grid.power_entity"),
        ("grid_import", "grid.import_entity"),
        ("grid_export", "grid.export_entity"),
    )
    for role, key in keys:
        entity = str(settings.get(key, "") or "")
        mapping.setdefault(role, entity)
    for battery in batteries:
        battery_mapping = battery.mapping or {}
        candidates = {
            f"battery_power:{battery.id}": battery_mapping.get("power_setpoint_entity"),
            f"battery_charge:{battery.id}": battery_mapping.get("charge_power_entity"),
            f"battery_discharge:{battery.id}": battery_mapping.get("discharge_power_entity"),
            f"battery_pv:{battery.id}": battery_mapping.get("pv_power_entity"),
            f"battery_output:{battery.id}": battery_mapping.get("output_power_entity"),
            f"battery_soc:{battery.id}": battery_mapping.get("soc_entity"),
        }
        for role, entity in candidates.items():
            if entity:
                mapping.setdefault(role, str(entity))
    return {role: entity for role, entity in mapping.items() if entity}


def _state_map(states: Any) -> dict[str, Mapping[str, Any]]:
    if states is None:
        return {}
    if isinstance(states, Mapping):
        return {str(key): value for key, value in states.items()}
    return {str(item.get("entity_id")): item for item in states if item.get("entity_id")}


def _fetch_states(client: Any, entity_ids: Iterable[str]) -> dict[str, Mapping[str, Any]]:
    """Targeted ``GET /api/states/<entity>`` per mapped entity (docs/EXTERNAL_APIS.md).

    The unfiltered ``/api/states`` snapshot is ~855 KB for 1 322 entities, so it is never fetched on
    a schedule. One failing entity must not lose the rest of the sample; it is logged and skipped.
    """
    snapshot: dict[str, Mapping[str, Any]] = {}
    for entity_id in sorted(set(entity_ids)):
        try:
            state = client.get_state(entity_id)
        except Exception as err:  # noqa: BLE001 - one unreadable entity must not skip the tick
            log.debug("cannot read %s: %s", entity_id, err)
            continue
        if state:
            snapshot[str(entity_id)] = state
    return snapshot


def _battery_power_w(battery_id: str, values: Mapping[str, float]) -> float | None:
    """Battery AC power in W, positive = charging (models.py section 8.1 convention).

    The signed ``battery_power:<id>`` role is used when mapped, else the measured AC pair
    ``battery_charge:<id>`` - ``battery_discharge:<id>`` (a missing side counts as 0). DC PV input
    (``battery_pv``) is never mixed in: it is not AC-side power, and drivers/zendure.py derives its
    AC value from ``grid_input_power - output_home_power`` (SPEC.md appendix A).
    """
    if f"battery_power:{battery_id}" in values:
        return values[f"battery_power:{battery_id}"]
    charge = values.get(f"battery_charge:{battery_id}")
    discharge = values.get(f"battery_discharge:{battery_id}")
    if charge is not None or discharge is not None:
        return (charge or 0.0) - (discharge or 0.0)
    return None


def collect(
    client: Any,
    settings: Any,
    batteries: Iterable[BatteryConfig] = (),
    *,
    entities: Mapping[str, str] | None = None,
    states: Any = None,
    now: datetime | None = None,
) -> tuple[list[Measurement], dict[str, Any]]:
    """Sample the configured roles and bucket them at :data:`MEASUREMENT_MINUTES`.

    Without an explicit ``states`` snapshot only the mapped entities are read, one targeted call
    each. Returns ``(measurements, actuals)``; the engine is the single writer of both tables
    (``actuals`` here is the live tick value, not a row to persist).
    """
    moment = now or utcnow()
    bucket = quarter_floor(moment, MEASUREMENT_MINUTES)
    battery_list = [battery for battery in batteries if battery.enabled]
    role_map = role_entities(settings, battery_list, entities)
    if states is not None:
        state_map = _state_map(states)
    elif client is not None:
        state_map = _fetch_states(client, role_map.values())
    else:
        state_map = {}
    measurements: list[Measurement] = []
    values: dict[str, float] = {}

    for role, entity_id in role_map.items():
        state = state_map.get(entity_id)
        if state is None:
            continue
        attributes = state.get("attributes") or {}
        unit = attributes.get("unit_of_measurement")
        raw = state.get("state")
        if role.startswith("battery_soc:"):
            value, canonical = _to_percent(raw, unit), "%"
        elif role == "price":
            value, canonical = _price_eur_per_kwh(raw, unit), "EUR/kWh"
        else:
            value, canonical = power_to_w(raw, unit), "W"
        if value is None:
            continue
        values[role] = value
        measurements.append(Measurement(ts=bucket, role=role, value=value, unit=canonical, entity_id=entity_id))

    battery_power_w: list[float] = []
    soc_kwh = 0.0
    for battery in battery_list:
        power = _battery_power_w(battery.id, values)
        if power is not None:
            battery_power_w.append(power)
        soc_pct = values.get(f"battery_soc:{battery.id}")
        if soc_pct is not None:
            soc_kwh += max(0.0, soc_pct) / 100.0 * float(battery.capacity_kwh)

    grid_w = values.get("grid_power")
    if grid_w is None and ("grid_import" in values or "grid_export" in values):
        grid_w = values.get("grid_import", 0.0) - values.get("grid_export", 0.0)
    actuals: dict[str, Any] = {
        "ts": bucket,
        "grid_kw": _kw(grid_w),
        "pv_kw": _kw(values.get("pv_power")),
        "load_kw": _kw(values.get("load_power")),
        "battery_kw": _kw(sum(battery_power_w) if battery_power_w else None),
        "soc_kwh": soc_kwh if any(f"battery_soc:{battery.id}" in values for battery in battery_list) else None,
        "price_buy": values.get("price"),
    }
    return measurements, actuals


def _kw(watts: Any) -> float | None:
    return None if watts is None else float(watts) / 1000.0
