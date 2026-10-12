"""Per-battery allocation of an aggregated plan power (SPEC section 8.3).

Greedy and deterministic: split the demand proportionally to the remaining headroom, clamp per
battery, then redistribute the shortfall once (no loops).  When only part of the demand fits, the
leftover goes to the batteries with the better efficiency first.

Every discharge allocation respects each battery's own floor: ``max(min_soc_pct,
reserve_soc_pct)`` of *that* battery's capacity (the reserve is a per-battery floor, never only an
aggregate one), and the leftover is redistributed across the batteries that still have room.

Ponytail: the split ignores per-battery price differences (there are none: one grid meter, one
price) and per-battery efficiency is only used in the redistribution tie-break.  Upgrade path: an
efficiency-weighted proportional split if a future driver ever reports its own energy price.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..models import BatteryConfig

DEFAULT_SLOT_HOURS = 0.25


def floor_energy_kwh(battery: BatteryConfig, reserve_soc_pct: float = 0.0) -> float:
    """Energy this battery must never be discharged below: its own floor or the reserve.

    ``optimizer.reserve_soc_pct`` is an extra floor above ``min_soc`` (SPEC 3); it applies to
    every battery's *own* capacity, so a fleet-wide reserve cannot be spent entirely out of the
    smallest battery while a fuller one is left untouched.
    """
    return battery.capacity_kwh * max(battery.min_soc_pct, float(reserve_soc_pct or 0.0)) / 100.0


def battery_energy_kwh(
    battery: BatteryConfig, energy_kwh: float | None, reserve_soc_pct: float = 0.0
) -> float:
    """Live energy in kWh, falling back to the configured floor when unknown.

    Treating an unknown battery as empty is the safe direction: it may still charge, it will not
    be asked to discharge below its floor.
    """
    if energy_kwh is not None:
        return float(energy_kwh)
    return floor_energy_kwh(battery, reserve_soc_pct)


def headroom_w(
    battery: BatteryConfig,
    energy_kwh: float | None,
    *,
    charging: bool,
    slot_hours: float = DEFAULT_SLOT_HOURS,
    reserve_soc_pct: float = 0.0,
) -> float:
    """AC-side watts this battery can still absorb (charging) or deliver (discharging).

    Discharge room is measured down to ``max(min_soc_pct, reserve_soc_pct)`` of *this* battery's
    capacity, so the reserve floor holds per battery, not only on the aggregate (WP5 finding).
    """
    hours = slot_hours if slot_hours > 0 else DEFAULT_SLOT_HOURS
    energy = battery_energy_kwh(battery, energy_kwh, reserve_soc_pct)
    _, e_max = battery.energy_bounds_kwh()
    if charging:
        room = max(0.0, e_max - energy)
        by_soc = (room / (hours * battery.eta_charge) if battery.eta_charge > 0 else 0.0) * 1000.0
        return max(0.0, min(battery.max_charge_w, by_soc))
    available = max(0.0, energy - floor_energy_kwh(battery, reserve_soc_pct))
    by_soc = available * battery.eta_discharge / hours * 1000.0
    return max(0.0, min(battery.max_discharge_w, by_soc))


def allocate_power(
    target_w: float,
    batteries: Sequence[BatteryConfig],
    energies_kwh: Mapping[str, float | None] | None = None,
    *,
    slot_hours: float = DEFAULT_SLOT_HOURS,
    reserve_soc_pct: float = 0.0,
    block_discharge_ids: Sequence[str] | None = None,
) -> list[tuple[BatteryConfig, float]]:
    """Split ``target_w`` (AC side, + charge / - discharge) across the enabled batteries.

    Returns one entry per enabled battery, in stable id order, including zeros.  The sum is the
    requested total minus whatever is physically impossible.  ``reserve_soc_pct``
    (``optimizer.reserve_soc_pct``) is enforced on every battery, not on the aggregate.
    ``block_discharge_ids`` are batteries a guard (e.g. low cell voltage) must never discharge:
    their discharge room is zero, so the shortfall is redistributed to the others.
    """
    energies = energies_kwh or {}
    enabled = sorted((b for b in batteries if b.enabled), key=lambda b: b.id)
    if not enabled:
        return []
    if abs(target_w) < 1e-9:
        return [(b, 0.0) for b in enabled]

    charging = target_w > 0
    demand = abs(float(target_w))
    limit = (lambda b: b.max_charge_w) if charging else (lambda b: b.max_discharge_w)
    eta = (lambda b: b.eta_charge) if charging else (lambda b: b.eta_discharge)

    room: list[float] = []
    for battery in enabled:
        free = headroom_w(
            battery,
            energies.get(battery.id),
            charging=charging,
            slot_hours=slot_hours,
            reserve_soc_pct=reserve_soc_pct,
        )
        room.append(max(0.0, min(free, limit(battery))))
    blocked = set(block_discharge_ids or ())
    if not charging and blocked:
        for index, battery in enumerate(enabled):
            if battery.id in blocked:
                room[index] = 0.0

    total_room = sum(room)
    allocated = [0.0] * len(enabled)
    if total_room > 0:
        for i, free in enumerate(room):
            allocated[i] = min(free, demand * free / total_room)

    # One redistribution pass: fill whatever the proportional split could not place, preferring
    # the better efficiency (and id order for a stable result).
    shortfall = demand - sum(allocated)
    if shortfall > 1e-9:
        order = sorted(range(len(enabled)), key=lambda i: (-eta(enabled[i]), enabled[i].id))
        for i in order:
            spare = room[i] - allocated[i]
            if spare <= 1e-9:
                continue
            take = min(spare, shortfall)
            allocated[i] += take
            shortfall -= take
            if shortfall <= 1e-9:
                break

    sign = 1.0 if charging else -1.0
    return [(battery, sign * allocated[i]) for i, battery in enumerate(enabled)]


def allocated_total(entries: Sequence[tuple[BatteryConfig, float]]) -> float:
    return sum(float(power) for _, power in entries)
