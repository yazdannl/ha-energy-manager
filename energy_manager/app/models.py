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
from typing import Any, Iterable, Sequence

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
    POWER_CLAMP = "power_clamp"
    GRID_IMPORT_LIMIT = "grid_import_limit"
    GRID_EXPORT_LIMIT = "grid_export_limit"
    RAMP_LIMIT = "ramp_limit"
    DRIVER_ERROR = "driver_error"
    IDEMPOTENT_SKIP = "idempotent_skip"
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
        return problems


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
