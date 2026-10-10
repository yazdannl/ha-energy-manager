"""Optimiser package (SPEC section 8): SoC dynamic program plus per-battery allocation."""

from .allocate import (
    allocate_power,
    allocated_total,
    battery_energy_kwh,
    floor_energy_kwh,
    headroom_w,
)
from .dp import (
    BatteryAggregate,
    OptimizerOptions,
    SlotInput,
    align_slots,
    next_energy_kwh,
    power_bounds_kw,
    slot_cost_eur,
    solve,
    solve_from_forecasts,
)

__all__ = [
    "BatteryAggregate",
    "OptimizerOptions",
    "SlotInput",
    "align_slots",
    "allocate_power",
    "allocated_total",
    "battery_energy_kwh",
    "floor_energy_kwh",
    "headroom_w",
    "next_energy_kwh",
    "power_bounds_kw",
    "slot_cost_eur",
    "solve",
    "solve_from_forecasts",
]
