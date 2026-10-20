"""Frozen shared vocabulary for the Smart Energy Manager add-on.

Owner: orchestrator (SPEC.md section 6). Do not change the public surface of this module without
amending SPEC.md in the same change. Every worker may import from here; nobody may redefine these
shapes elsewhere.

Conventions
-----------
* All datetimes are timezone-aware ``datetime`` objects in UTC internally. Serialisation is ISO-8601
  with offset (``to_iso``), parsing accepts both ``Z`` and ``+00:00`` suffixes (``from_iso``).
* All powers are watts (AC side) unless the field name says ``_kw``.
* All energies are kWh; all prices are EUR per kWh, tax included, at the meter.
* Battery AC power sign convention: positive = charging (consumes from AC), negative = discharging.
* No instance-specific values (entity ids, coordinates, keys) may have defaults in this module.

Deliberate simplification: ``Reason`` is a namespace of string constants rather than an ``Enum``, so
that newer drivers can attach a free-form slug without a coordinated release.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "Mode",
    "Action",
    "Reason",
    "PRICE_UNITS",
    "BatteryConfig",
    "BatteryState",
    "SystemState",
    "PriceSlot",
    "PriceForecast",
    "SolarSlot",
    "SolarForecast",
    "LoadSlot",
    "LoadForecast",
    "PlanSlot",
    "PlanSummary",
    "Plan",
    "Measurement",
    "AlertRecord",
    "DispatchResult",
    "SavingsBucket",
    "AccuracyReport",
    "ProviderStatus",
    "ConfigError",
    "to_iso",
    "from_iso",
    "utcnow",
    "quarter_floor",
    "slot_range",
    "slot_minutes_between",
    "eur_per_kwh_from_raw",
    "clamp",
    "safe_div",
    "MANUAL_LIMIT_DIRECTIONS",
    "clean_manual_limits",
    "resolve_manual_limit",
]


class ConfigError(Exception):
    """Raised when required runtime configuration is missing or invalid."""


# --------------------------------------------------------------------------------------
# Enumerations and reason slugs
# --------------------------------------------------------------------------------------


class Mode(str, Enum):
    """Control authority. ``advisory`` is the shipped default and never writes to hardware."""

    OFF = "off"
    ADVISORY = "advisory"
    AUTO = "auto"


class Action(str, Enum):
    """What the plan does with the battery in a slot."""

    HOLD = "hold"
    CHARGE = "charge"
    DISCHARGE = "discharge"
    PV_CHARGE = "pv_charge"
    EXPORT = "export"
    SELF_CONSUMPTION = "self_consumption"
    RESERVE = "reserve"


class Reason:
    """Stable, human-readable slugs explaining a slot decision or a blocked dispatch.

    UI, notifications and the log all key off these strings; keep them stable.
    """

    # plan reasons
    ARBITRAGE_CHARGE = "arbitrage_charge"
    PV_SURPLUS_CHARGE = "pv_surplus_charge"
    PEAK_DISCHARGE = "peak_discharge"
    SELF_CONSUMPTION = "self_consumption"
    RESERVE_HOLD = "reserve_hold"
    GRID_LIMIT_HOLD = "grid_limit_hold"
    SPREAD_BELOW_THRESHOLD = "spread_below_threshold"
    EFFICIENCY_LOSS = "efficiency_loss"
    TERMINAL_TARGET = "terminal_target"
    FULL = "battery_full"
    EMPTY = "battery_empty"
    NO_SLOTS = "no_forecast_slots"
    STALE_INPUT = "stale_input"

    # dispatch blocks
    MODE_NOT_AUTO = "mode_not_auto"
    DRY_RUN = "dry_run"
    OVERRIDE_ACTIVE = "override_active"
    STALE_STATE = "stale_state"
    HA_DISCONNECTED = "ha_disconnected"
    BATTERY_UNAVAILABLE = "battery_unavailable"
    SOC_FLOOR = "soc_floor"
    SOC_CEILING = "soc_ceiling"
    #: The plan's own SoC trajectory for this slot is reached or about to be: the setpoint is cut
    #: (a charge to zero, a discharge to zero) so the pack lands on the plan instead of running the
    #: slot at its modelled power past the target (SPEC 9.3 clause 5b, 0.31.0).
    PLAN_TARGET = "plan_soc_target"
    POWER_CLAMP = "power_clamp"
    GRID_IMPORT_LIMIT = "grid_import_limit"
    GRID_EXPORT_LIMIT = "grid_export_limit"
    RAMP_LIMIT = "ramp_limit"
    DRIVER_ERROR = "driver_error"
    #: Cost-basis sell floor (0.18.0): a discharge/export whose value leg sits below the
    #: break-even of the energy actually held is refused, in the plan and at dispatch.
    SELL_FLOOR = "sell_floor"
    #: Optimizer v2 stage 1b: the tariff cannot price this export (unsupported contract, reachable
    #: threshold, or an unconfirmed tax component), so the battery-sourced export leg is refused
    #: rather than priced at a guessed rate. Forced PV export keeps its own conservative floor.
    UNPRICED_EXPORT = "unpriced_export"
    IDEMPOTENT_SKIP = "idempotent_skip"
    # cell-voltage protection (0.16.0): a battery latched below its cutoff may not discharge and
    # is charged out of danger; a stale reading keeps the previous latch.
    CELL_VOLTAGE_GUARD = "cell_voltage_guard"
    CELL_VOLTAGE_EMERGENCY_CHARGE = "cell_voltage_emergency_charge"
    CELL_VOLTAGE_STALE = "cell_voltage_stale"
    #: The command is in force in Home Assistant, but the device has not followed it yet: an
    #: asynchronous control surface (the Zendure manager applies manual power on its next P1
    #: event) must not be mistaken for a broken one.  Not an error, and never a claim that the
    #: measured flow confirms it -- the plan execution view reports measured vs planned.
    COMMANDED = "commanded"
    OK = "ok"


PRICE_UNITS = ("auto", "eur_per_kwh", "eur_per_mwh", "micro_eur_per_kwh")

#: Write paths a battery may be pinned to (``BatteryConfig.control_path``); ``auto`` lets the
#: driver pick, which keeps the shipped behaviour for every existing configuration.
CONTROL_PATHS = ("auto", "manager", "device")

#: Where a battery's capacity and round-trip efficiency come from (SPEC.md section 5): ``auto``
#: uses the values measured from the pack's own cycles once the estimate is confident and fresh and
#: keeps the typed values as the seed/fallback; ``pinned`` always uses what was typed.  ``manual``
#: and ``learned`` are the pre-0.31.0 spellings (both pinned / both learned) and are still accepted
#: on load, but they no longer mean "typed always wins": 0.31.0 made the learned values the
#: default because a typed capacity is a guess while the estimate is a measurement.
CAPACITY_SOURCES = ("auto", "pinned")
LEGACY_CAPACITY_SOURCES = ("manual", "learned")
#: Values ``capacity_source`` may carry; ``pinned`` is the only one that blocks learning.
ALL_CAPACITY_SOURCES = CAPACITY_SOURCES + LEGACY_CAPACITY_SOURCES


# --------------------------------------------------------------------------------------
# Batteries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BatteryConfig:
    """User-confirmed description of one controllable battery.

    ``mapping`` is driver-specific and resolved at runtime from the HA entity registry; it must never
    contain a hardcoded instance entity id coming from source code (only from what the user
    confirmed). Keys used by the shipped drivers:

    zendure: manual_mode_entity, manual_mode_value, power_setpoint_entity, charge_limit_entity,
             discharge_limit_entity, soc_entity, soc_target_entity, min_soc_entity,
             capacity_entity, pv_power_entity, output_power_entity, ac_mode_entity
    generic: power_setpoint_entity | (charge_power_entity + discharge_power_entity),
             mode_select_entity, mode_manual_value, soc_entity, capacity_entity

    Efficiency is AC-side: charging stores ``eta_charge`` of the AC energy taken in and discharging
    returns ``eta_discharge`` of the stored energy. The round trip is their product; the UI collects
    it as one number and splits it evenly unless per-direction values are given explicitly.
    """

    id: str
    name: str
    driver: str
    capacity_kwh: float
    min_soc_pct: float = 10.0
    max_soc_pct: float = 100.0
    max_charge_w: float = 800.0
    max_discharge_w: float = 800.0
    eta_charge: float = 0.95
    eta_discharge: float = 0.95
    mapping: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    #: Purchase price of the pack in EUR and its rated full-cycle life (SPEC.md section 5). Both
    #: are optional: 0 means "not known", never a guess, and the derived wear is then 0.
    price_eur: float = 0.0
    cycle_life: float = 0.0
    #: Which write path the driver must use: ``auto`` (driver preference: Zendure manager first),
    #: ``manager`` (the Zendure manager's system-wide ``manual_power``) or ``device`` (the
    #: inverter's own direction select plus ``input_limit``/``output_limit``).  The device path
    #: talks to the inverter directly, so it does not wait for the manager's next P1 event; the
    #: manager path is the only one that can split power across a fuse group.
    control_path: str = "auto"
    #: ``auto`` (default) or ``pinned`` (SPEC.md section 5).  ``auto`` keeps ``capacity_kwh`` and
    #: the typed round trip as seeds/fallbacks and uses the self-measured capacity and round-trip
    #: efficiency for planning, allocation, UI and the published summaries once they are confident
    #: and fresh; ``pinned`` always uses the typed values.  The pre-0.31.0 spellings ``manual`` and
    #: ``learned`` are still accepted and both mean ``auto`` (see ``ALL_CAPACITY_SOURCES``).
    capacity_source: str = "auto"

    def __post_init__(self) -> None:
        """Keep one canonical spelling (SPEC.md section 5): the legacy ones both mean ``auto``."""
        source = str(self.capacity_source or "auto").strip().lower()
        canonical = "auto" if source in LEGACY_CAPACITY_SOURCES else source
        if canonical != self.capacity_source:
            object.__setattr__(self, "capacity_source", canonical)

    def usable_kwh(self) -> float:
        """Energy between the configured SoC floor and ceiling."""
        return max(0.0, self.capacity_kwh * (self.max_soc_pct - self.min_soc_pct) / 100.0)

    def energy_bounds_kwh(self) -> tuple[float, float]:
        return (
            self.capacity_kwh * self.min_soc_pct / 100.0,
            self.capacity_kwh * self.max_soc_pct / 100.0,
        )

    @property
    def round_trip_efficiency(self) -> float:
        """AC-to-AC efficiency of a full charge/discharge cycle (``eta_charge * eta_discharge``)."""
        return self.eta_charge * self.eta_discharge

    @property
    def wear_eur_per_kwh(self) -> float:
        """Wear cost of one kWh **delivered** (AC): ``price / (cycle_life × usable kWh × η_dis)``.

        A full cycle spreads the purchase price over its rated cycles and delivers
        ``usable_kwh × eta_discharge`` AC kWh, so dividing by that delivered energy makes one full
        discharge cost exactly ``price / cycle_life`` (SPEC.md section 5). Returns 0.0 while either
        input is unset, which is exactly the old "no wear configured" default; it never guesses a
        price or a life.
        """
        if self.price_eur <= 0.0 or self.cycle_life <= 0.0:
            return 0.0
        delivered = self.usable_kwh() * self.eta_discharge
        if delivered <= 0.0:
            return 0.0
        return self.price_eur / (self.cycle_life * delivered)

    def break_even_sell_eur_per_kwh(
        self, buy_eur_per_kwh: float, wear_eur_per_kwh: float | None = None
    ) -> float | None:
        """Lowest sale price (EUR per kWh AC delivered) that covers buying at ``buy_eur_per_kwh``.

        ``buy / round_trip`` is the energy the round trip loses; ``wear`` is the delivered kWh's
        share of the pack's life. Both terms are per delivered AC kWh, so the formula uses the
        round trip alone (SPEC.md section 5). ``wear_eur_per_kwh=None`` reads the battery's own
        derived value; an explicit number (the advanced override) wins. Returns ``None`` when the
        round trip makes the calculation meaningless.
        """
        if self.round_trip_efficiency <= 0:
            return None
        wear = self.wear_eur_per_kwh if wear_eur_per_kwh is None else wear_eur_per_kwh
        return buy_eur_per_kwh / self.round_trip_efficiency + wear

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means usable."""
        problems: list[str] = []
        if not self.id:
            problems.append("battery id is required")
        if not self.driver:
            problems.append("driver is required")
        if self.capacity_kwh <= 0:
            problems.append("capacity_kwh must be > 0")
        if not 0 <= self.min_soc_pct < self.max_soc_pct <= 100:
            problems.append("min_soc_pct must be < max_soc_pct and both within 0..100")
        if self.max_charge_w <= 0 and self.max_discharge_w <= 0:
            problems.append("at least one of max_charge_w / max_discharge_w must be > 0")
        if not 0 < self.eta_charge <= 1 or not 0 < self.eta_discharge <= 1:
            problems.append("efficiencies must be in (0, 1]")
        for name, value in (("price_eur", self.price_eur), ("cycle_life", self.cycle_life)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                problems.append(f"{name} must be a finite number")
            elif value < 0:
                problems.append(f"{name} must be >= 0")
        if not self.mapping:
            problems.append("mapping is empty: confirm the entity mapping in the UI")
        if self.control_path not in CONTROL_PATHS:
            problems.append(f"control_path must be one of {', '.join(CONTROL_PATHS)}")
        if self.capacity_source not in ALL_CAPACITY_SOURCES:
            problems.append(
                f"capacity_source must be one of {', '.join(ALL_CAPACITY_SOURCES)}"
            )
        return problems

    @property
    def learns_from_measurements(self) -> bool:
        """True unless the operator pinned the typed capacity and round trip (SPEC.md section 5)."""
        return self.capacity_source != "pinned"


@dataclass
class BatteryState:
    """Live state of one battery as last read from Home Assistant."""

    id: str
    available: bool = False
    soc_pct: float | None = None
    power_w: float | None = None
    capacity_kwh: float | None = None
    energy_kwh: float | None = None
    pv_power_w: float | None = None
    temperature_c: float | None = None
    read_at: datetime | None = None
    last_error: str | None = None
    stale: bool = False
    # Additive (0.13.0 measured savings): True only when ``power_w`` is a real flow measurement.
    # Drivers whose power entity is the setpoint they wrote report False, because a commanded value
    # is not proof the battery moved energy; savings must never be earned by an unconfirmed command.
    power_measured: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.read_at is None:
            return None
        return (now or utcnow()).timestamp() - self.read_at.timestamp()


# --------------------------------------------------------------------------------------
# Forecasts
# --------------------------------------------------------------------------------------


@dataclass
class PriceSlot:
    t: datetime
    buy: float  # EUR/kWh, tax included
    sell: float  # EUR/kWh received for export
    source: str = "unknown"
    is_actual: bool = False
    stale: bool = False
    # Energy-tax component of ``buy`` in EUR/kWh, when the provider publishes a tax-excluded price
    # next to it (Zonneplan does; wholesale sources get the configured adder). The derived feed-in
    # price subtracts it when the contract does not pay the energy tax back (SPEC.md section 4).
    energy_tax: float = 0.0


@dataclass
class PriceForecast:
    slots: list[PriceSlot] = field(default_factory=list)
    generated_at: datetime | None = None
    source: str = "unknown"
    stale: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    #: Additive (optimizer v2 stage 3, docs/OPTIMIZER_V2.md section 4): the provider entries past
    #: the plan horizon, gridded out to ``optimizer.terminal_tail_hours`` on the same 15-minute
    #: grid and filled periodically by time-of-day from the fetched window beyond the last real
    #: entry. ``slots`` is deliberately unchanged -- the plan horizon, the feed-in settlement and
    #: the saldering period volumes must not move -- and nothing in the live path reads this;
    #: ``optimize/terminal.py`` is its only consumer.
    tail_slots: list[PriceSlot] = field(default_factory=list)

    def covers(self, start: datetime, end: datetime) -> bool:
        if not self.slots:
            return False
        return self.slots[0].t <= start and self.slots[-1].t >= end

    def median_buy(self) -> float:
        values = sorted(s.buy for s in self.slots)
        if not values:
            return 0.0
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


@dataclass
class SolarSlot:
    t: datetime
    pv_kw: float
    uncertainty_pct: float = 25.0


@dataclass
class SolarForecast:
    slots: list[SolarSlot] = field(default_factory=list)
    generated_at: datetime | None = None
    source: str = "unknown"
    stale: bool = False
    planes: list[dict[str, Any]] = field(default_factory=list)
    correction_applied: float = 1.0
    notes: list[str] = field(default_factory=list)
    # Additive (0.12.0): one status per configured merge source, empty for the single-source path.
    sources: list[ProviderStatus] = field(default_factory=list)

    def total_kwh(self) -> float:
        """Energy under the curve, assuming equally spaced slots."""
        return sum(max(0.0, s.pv_kw) for s in self.slots) * slot_hours_from(self.slots)


def slot_hours_from(slots: Sequence[Any], default_minutes: int = 15) -> float:
    """Hours per slot for a series, from the first two timestamps."""
    if len(slots) >= 2:
        delta = slots[1].t - slots[0].t
        hours = delta.total_seconds() / 3600.0
        if hours > 0:
            return hours
    return default_minutes / 60.0


@dataclass
class LoadSlot:
    t: datetime
    load_kw: float
    p10_kw: float | None = None
    p90_kw: float | None = None


@dataclass
class LoadForecast:
    slots: list[LoadSlot] = field(default_factory=list)
    generated_at: datetime | None = None
    source: str = "unknown"
    confidence: str = "low"  # low | medium | high
    stale: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------------------


@dataclass
class PlanSlot:
    t: datetime
    buy: float
    sell: float
    pv_kw: float
    load_kw: float
    soc_kwh: float
    power_w: float  # aggregated AC-side battery power for the slot
    action: Action
    reason: str
    load_p10_kw: float | None = None
    load_p90_kw: float | None = None
    price_stale: bool = False
    soc_pct: float | None = None  # capacity-weighted mean SoC, percent; shown in the UI/API
    pv_source: str = ""  # provider that supplied pv_kw for this slot
    price_source: str = ""  # provider that supplied buy/sell for this slot
    #: Additive (0.20.1, SPEC 8.16): the duration the solver actually modelled for this slot, in
    #: minutes.  Full ``plan.slot_minutes`` for every slot except a first slot that began before
    #: the plan was made, whose value is the remaining part of that clock slot (>= 1.0).  The
    #: slot's timestamp stays clock-aligned; only this duration changes, so the schedule, its
    #: energy and its money all match the time the dispatcher really has.  ``0.0`` (the default)
    #: means "unknown -- fall back to ``plan.slot_minutes``", e.g. a plan reloaded from an older
    #: store.
    minutes: float = 0.0

    def grid_kw(self) -> float:
        # p > 0 charges (consumes from AC), so a charge *raises* grid import.
        return self.load_kw - self.pv_kw + self.power_w / 1000.0

    def cost_eur(self, dt_hours: float) -> float:
        grid = self.grid_kw()
        if grid >= 0:
            return self.buy * grid * dt_hours
        return -self.sell * (-grid) * dt_hours


@dataclass
class PlanSummary:
    cost_estimate_eur: float = 0.0
    baseline_cost_eur: float = 0.0
    saved_eur: float = 0.0
    charged_kwh: float = 0.0
    discharged_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    pv_charge_kwh: float = 0.0
    cycles: float = 0.0
    arbitrage_kwh: float = 0.0
    effective_step_kwh: float = 0.0
    solver_iterations: int = 0
    cycle_cost_eur: float = 0.0  # battery-wear term included in the objective
    #: The wear constant the plan assumed, EUR per kWh delivered. It is the derived fleet value
    #: (or the advanced override) and is what ``break_even_sell_eur_per_kwh`` adds to
    #: ``buy / round_trip`` (SPEC.md section 5).
    wear_eur_per_kwh: float = 0.0
    objective_eur: float = 0.0  # the exact value the optimiser minimised
                              # (grid cost + cycle cost - terminal salvage)
                              # cost_estimate_eur / baseline_cost_eur / saved_eur are grid-only and
                              # therefore directly comparable to each other; cycle cost is reported
                              # separately so a saving is never inflated by ignoring wear.
    # Battery-efficiency context (SPEC.md section 8.1): the aggregate the optimiser assumed and the
    # resulting break-even sale price, so /plan, /status and the UI can explain a slot that stays
    # idle because the spread does not cover the round-trip loss.
    eta_charge: float | None = None
    eta_discharge: float | None = None
    round_trip_efficiency: float | None = None
    break_even_sell_eur_per_kwh: float | None = None
    #: Cost-basis sell floor (SPEC 5, additive 0.18.0): the fleet's energy-weighted mean
    #: break-even of the energy actually in the packs, or ``None`` when the ledger is empty or
    #: the floor is disabled. The DP blocks a discharge/export leg below it with
    #: ``Reason.SELL_FLOOR``.
    sell_floor_eur_per_kwh: float | None = None
    #: Scheduled full-charge maintenance (SPEC 8.1, additive 0.17.0): the plan-level block
    #: (``enabled``/``active``/``due``/``target_soc_pct``/``batteries``) when a battery's deadline
    #: falls inside the horizon, else ``None``. The optimiser attaches it verbatim; the API and UI
    #: read it through ``getattr`` so an older stored summary yields ``None`` instead of failing.
    maintenance: dict[str, Any] | None = None


@dataclass
class Plan:
    generated_at: datetime
    horizon_start: datetime
    horizon_end: datetime
    slot_minutes: int
    feasible: bool = True
    infeasible_reason: str | None = None
    slots: list[PlanSlot] = field(default_factory=list)
    summary: PlanSummary = field(default_factory=PlanSummary)
    batteries: list[dict[str, Any]] = field(default_factory=list)  # per-plan battery snapshots
    mode: str = Mode.ADVISORY.value
    dry_run: bool = True
    sources: dict[str, str] = field(default_factory=dict)
    #: Additive (0.17.0): the same scheduled-full-charge block as ``summary.maintenance``, surfaced
    #: at the top of ``GET /api/v1/plan`` for clients that read the plan but not its summary.
    maintenance: dict[str, Any] | None = None

    def slot_at(self, when: datetime) -> PlanSlot | None:
        for slot in self.slots:
            if slot.t <= when < slot.t + timedelta(minutes=self.slot_minutes):
                return slot
        return None

    def allocations(self, slot: PlanSlot) -> list[tuple[str, float]]:
        """Per-battery split of a slot's aggregated power, as recorded at plan time."""
        out: list[tuple[str, float]] = []
        for entry in self.batteries:
            share = entry.get("share_w")
            if share is not None:
                out.append((str(entry.get("id")), float(share)))
        return out


# --------------------------------------------------------------------------------------
# Runtime state, dispatch and reporting
# --------------------------------------------------------------------------------------


@dataclass
class SystemState:
    version: str = "0.0.0"
    commit: str = ""
    uptime_s: float = 0.0
    mode: str = Mode.ADVISORY.value
    dry_run: bool = True
    ha_connected: bool = False
    ha_last_error: str | None = None
    location: str = ""  # human label only, never a literal coordinate
    now: datetime | None = None
    price_now: float | None = None
    price_rank_pct: float | None = None
    pv_now_w: float | None = None
    load_now_w: float | None = None
    grid_now_w: float | None = None
    batteries: list[BatteryState] = field(default_factory=list)
    active_plan: Plan | None = None
    last_plan_at: datetime | None = None
    last_tick_at: datetime | None = None
    next_cheap_window: tuple[datetime, datetime] | None = None
    next_expensive_window: tuple[datetime, datetime] | None = None
    override: dict[str, Any] | None = None
    #: Additive (0.20.0): per-direction manual caps the operator sets from the Dashboard.  The
    #: mapping is ``{"*": {...}, "<battery_id>": {...}}`` with ``charge_w``/``discharge_w`` values:
    #: ``None`` (absent) = no limit, ``0.0`` = that direction is blocked, a number = a cap in watts
    #: that never raises the battery's configured maximum (SPEC 5/9.3).  A per-battery key wins
    #: over ``"*"``.  Resolved by :func:`resolve_manual_limit`, never read ad hoc.
    manual_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    alerts: list[AlertRecord] = field(default_factory=list)
    provider_status: list[ProviderStatus] = field(default_factory=list)
    scheduler_healthy: bool = True


@dataclass
class Measurement:
    ts: datetime
    role: str  # grid_power | pv_power | load_power | battery_power | battery_soc | price
    value: float
    unit: str
    entity_id: str | None = None


@dataclass
class AlertRecord:
    ts: datetime
    level: str  # info | warning | error
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False


@dataclass
class DispatchResult:
    battery_id: str
    ts: datetime
    requested_w: float
    applied_w: float
    ok: bool
    dry_run: bool = False
    code: str = Reason.OK
    message: str = ""
    writes: list[dict[str, Any]] = field(default_factory=list)  # {entity_id, value, service}
    readback_w: float | None = None


@dataclass
class SavingsBucket:
    key: str  # day/week/month/year label
    start: datetime
    end: datetime
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    pv_kwh: float = 0.0
    cost_eur: float = 0.0
    revenue_eur: float = 0.0
    baseline_cost_eur: float = 0.0
    savings_eur: float = 0.0
    cycles: float = 0.0
    # Additive (0.13.0 measured savings): measured AC battery flows and what they were worth at
    # flow time.  None = unknown (no confirmed measurement for every interval in the bucket),
    # never a fabricated 0.  ``battery_measured`` is the bucket's coverage flag; the others mirror
    # the measured series the grid/load/PV accounting was built from.
    charged_kwh: float | None = None
    discharged_kwh: float | None = None
    charge_cost_eur: float | None = None
    discharge_value_eur: float | None = None
    grid_measured: bool = False
    load_measured: bool = False
    pv_measured: bool = False
    battery_measured: bool = False
    # Additive (0.22.1): whether any stored row fell in this bucket.  False is "no recorded
    # activity" (the bucket is listed for a steady chart), which is different from a bucket that
    # has data but no knowable baseline.  Both report baseline/savings as None.
    has_data: bool = False
    # Additive (0.30.0): the sampled time each figure covers, in hours.  ``savings_hours`` is the
    # time behind baseline/savings, ``battery_hours`` the time behind the measured flows,
    # ``grid_hours`` the metered time behind import/export and ``total_hours`` the sampled time they
    # are a share of.  0 means the row predates coverage tracking, not that nothing was measured.
    total_hours: float = 0.0
    grid_hours: float = 0.0
    savings_hours: float = 0.0
    battery_hours: float = 0.0


@dataclass
class AccuracyReport:
    kind: str  # pv | load | price
    days: int
    mape_pct: float | None = None
    bias_pct: float | None = None
    mae: float | None = None
    samples: int = 0
    series: list[dict[str, Any]] = field(default_factory=list)  # {day, mape_pct, bias_pct}


@dataclass
class ProviderStatus:
    kind: str  # solar | price | load
    name: str
    active: bool = False
    ok: bool = False
    last_success: datetime | None = None
    last_error: str | None = None
    stale: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    # Additive (0.12.0 multi-source solar merge): the configured weight, the coarse state
    # (ok | degraded | failed | disabled; empty = derive from active/ok) and a one-line note.
    # Existing producers leave them unset, so the frozen surface is unchanged.
    weight: float | None = None
    state: str = ""
    note: str = ""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def from_iso(value: str | int | float | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string or epoch seconds into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unparsable datetime: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def quarter_floor(value: datetime | None = None, minutes: int = 15) -> datetime:
    """Floor a datetime to the start of its slot (default 15 minutes)."""
    moment = value or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    discard = (moment.minute % minutes) * 60 + moment.second
    return (moment - timedelta(seconds=discard)).replace(microsecond=0)


def slot_range(start: datetime, end: datetime, minutes: int = 15) -> list[datetime]:
    """Slot start times covering [start, end); ``start`` is floored to the slot boundary."""
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    if end <= start:
        return []
    cursor = quarter_floor(start, minutes)
    step = timedelta(minutes=minutes)
    out: list[datetime] = []
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out


def slot_minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


#: The two limit directions of a manual cap (SPEC 5/9.3, 0.20.0).
MANUAL_LIMIT_DIRECTIONS: tuple[str, ...] = ("charge", "discharge")


def _manual_limit_key(direction: str) -> str:
    return "charge_w" if str(direction).strip().lower() == "charge" else "discharge_w"


def clean_manual_limits(raw: Any) -> dict[str, dict[str, float]]:
    """A stored limits mapping reduced to ``{key: {"charge_w"|"discharge_w": float >= 0}}``.

    Used when loading the settings store: junk, negative values, NaN and entries with no usable
    direction are dropped (never guessed), and an entry that ends up empty is dropped as well, so
    the in-memory mapping is always the canonical shape the API returns.
    """
    out: dict[str, dict[str, float]] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        clean: dict[str, float] = {}
        for name in ("charge_w", "discharge_w"):
            value = entry.get(name)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number != number or number < 0.0:  # NaN / negative junk from a hand-edited store
                continue
            clean[name] = number
        if clean:
            out[str(key)] = clean
    return out


def resolve_manual_limit(
    limits: Mapping[str, Mapping[str, Any]] | None,
    battery_id: str,
    direction: str,
    configured_max_w: float,
) -> float | None:
    """The manual cap in force for one battery direction, or ``None`` when unlimited.

    Resolution order (SPEC 5/9.3, 0.20.0): the battery's own entry, then the ``"*"`` fleet entry,
    then the configured maximum.  The result never exceeds ``configured_max_w``: a cap left above
    it (a hand-edited store, or a maximum lowered after the cap was set) is clamped here too, so
    no code path can raise a battery's configured maximum.
    """
    if not isinstance(limits, Mapping):
        return None
    name = _manual_limit_key(direction)
    entry = limits.get(str(battery_id))
    value = entry.get(name) if isinstance(entry, Mapping) else None
    if value is None:
        fleet = limits.get("*")
        value = fleet.get(name) if isinstance(fleet, Mapping) else None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0.0:
        return None
    try:
        maximum = float(configured_max_w or 0.0)
    except (TypeError, ValueError):
        return None
    return min(number, max(0.0, maximum))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def eur_per_kwh_from_raw(
    value: float,
    unit: str = "eur_per_kwh",
    scale_divisor: float = 1.0,
    vat_pct: float = 0.0,
    add_eur_per_kwh: float = 0.0,
) -> float:
    """Normalise a raw provider price into EUR/kWh, tax included, at the meter.

    ``unit`` is the provider's declared unit; ``scale_divisor`` handles providers that publish a
    fixed-point integer (documented per provider in docs/EXTERNAL_APIS.md). ``vat_pct`` and
    ``add_eur_per_kwh`` are applied only when the provider's price is known to exclude tax/levies.
    ``unit="auto"`` means "the provider declared its own unit", which is identical to
    ``eur_per_kwh`` here: callers must pass a concrete unit, inferred values are not guessed.
    """
    if unit not in PRICE_UNITS:
        raise ValueError(f"unknown price unit: {unit!r}")
    scaled = safe_div(float(value), float(scale_divisor) or 1.0)
    if unit == "eur_per_mwh":
        scaled /= 1000.0
    elif unit == "micro_eur_per_kwh":
        scaled /= 1_000_000.0
    if vat_pct:
        scaled *= 1.0 + vat_pct / 100.0
    return scaled + add_eur_per_kwh
