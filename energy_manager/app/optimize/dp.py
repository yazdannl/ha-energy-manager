"""SoC dynamic program over the plan horizon (SPEC section 8).

Pure: no I/O, no clock reads (``now`` is passed in), no module state.  Identical inputs produce
byte-identical plans, so the solver never iterates a dict/set when producing output.

Model (SPEC 8.1): AC-side battery power ``p`` in kW, positive = charging.
``grid = load - pv + p`` and ``cost = dt * (buy*max(grid,0) - sell*max(-grid,0))``.
Objective: ``sum(cost) + wear*discharged_kwh - salvage*e_N`` with
``salvage = salvage_multiplier * median(buy) * eta_dis`` — a stored kWh only ever delivers
``eta_dis`` AC kWh, so valuing it at the full buy price would over-charge the pack (SPEC 8.1).
``wear`` is EUR per kWh **delivered**, charged on the discharge leg only (delivered AC kWh is
stored decrease times ``eta_dis``).

Sign note (resolved): SPEC 8.1 originally wrote ``grid_i = load_i - pv_i - p_i`` while also defining
positive ``p`` as charging; the two cannot both hold (subtracting a charging load would make every
kWh free).  The spec and the frozen ``models.PlanSlot.grid_kw()`` were corrected to the physically
consistent ``+``, which is what this module uses and what safety/allocation/dispatch assume; a
regression test asserts that ``PlanSlot.cost_eur(dt)`` equals this module's slot cost.

Algorithm (SPEC 8.2): value iteration over a discretised energy grid (<= 400 states), evaluating
the next slot's value function by interpolation at the exact reachable energy, and reconstructing
the path from the stored per-(slot, state) argmin.

Ponytail: the reported ``cost_estimate_eur`` is the realised grid cost of the reconstructed
schedule, not the DP objective value (which also contains salvage/cycle costs that are not money
in the bank); ``summary.objective_eur`` reports that objective separately (with
``summary.cycle_cost_eur`` as its wear part, now charged per kWh delivered) so a saving is never
inflated by ignoring wear.  The state grid is anchored on the current and target energy, so those two points
are exact while everything else is quantised; upgrade path is a finer grid plus local refinement.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..models import (
    Action,
    BatteryConfig,
    LoadForecast,
    Plan,
    PlanSlot,
    PlanSummary,
    PriceForecast,
    Reason,
    SolarForecast,
    clamp,
    quarter_floor,
    slot_range,
    utcnow,
)

log = logging.getLogger(__name__)

MAX_STATES = 400
MAX_ITERATIONS = 2_000_000
EPS_KW = 1e-6
EPS_KWH = 1e-9

# Terminal-target weights, EUR per kWh of deviation.  ``HARD`` dominates any plausible price
# difference, so a reachable target is hit exactly; ``SOFT`` is the documented downgrade for an
# unreachable target (SPEC 8.1) and only nudges the schedule towards it.
TARGET_HARD_PENALTY = 1e6
TARGET_SOFT_PENALTY = 2.0

#: A stand-in config that only ever carries the aggregate etas, so the frozen
#: ``BatteryConfig.break_even_sell_eur_per_kwh`` computes the pack's break-even without a second
#: copy of the formula (SPEC 8.1).  The pack is optimised as one battery with capacity-weighted
#: etas, which is exactly what this host is filled with.
_AGGREGATE_ETA_HOST = BatteryConfig(
    id="aggregate",
    name="aggregate",
    driver="generic",
    capacity_kwh=0.0,
    min_soc_pct=0.0,
    max_soc_pct=100.0,
    max_charge_w=0.0,
    max_discharge_w=0.0,
    eta_charge=1.0,
    eta_discharge=1.0,
    mapping={},
)


def aggregate_break_even_sell_eur_per_kwh(
    eta_charge: float,
    eta_discharge: float,
    buy_eur_per_kwh: float,
    wear_eur_per_kwh: float = 0.0,
) -> float | None:
    """The frozen break-even helper applied to a pair of etas and a per-delivered-kWh wear."""
    host = replace(
        _AGGREGATE_ETA_HOST, eta_charge=eta_charge, eta_discharge=eta_discharge
    )
    return host.break_even_sell_eur_per_kwh(buy_eur_per_kwh, wear_eur_per_kwh)


#: ``optimizer.pv_charge_preference`` values (SPEC 8.1, 0.19.0): how the objective prices the PV
#: energy a charge puts into the pack.  ``auto`` values it at the slot's export price (the revenue
#: forgone, economically exact), ``prefer_pv`` at zero (absorb the surplus first) and ``avoid_pv``
#: at the slot's buy price (export the PV and charge from the grid in the cheapest hours).
PV_CHARGE_PREFERENCES = ("auto", "prefer_pv", "avoid_pv")


def normalize_pv_charge_preference(value: Any) -> str:
    """A valid preference, else ``auto`` (a stored typo must never break a plan; SPEC 3/8.1)."""
    text = str(value or "").strip().lower()
    return text if text in PV_CHARGE_PREFERENCES else "auto"


@dataclass(frozen=True)
class OptimizerOptions:
    """Tunables read from ``optimizer.*`` settings (SPEC section 3)."""

    #: Plan and dispatch resolution. The shipped default matches the common hourly dynamic
    #: tariff; ``config.DEFAULTS`` must agree (a regression test pins the two together).
    slot_minutes: int = 60
    horizon_hours: int = 30
    soc_step_kwh: float = 0.02
    power_steps: int = 24
    salvage_multiplier: float = 1.0
    #: EUR per kWh delivered: the fleet's derived wear, or the advanced override from
    #: ``optimizer.cycle_cost_eur_per_kwh`` (SPEC 5). Charged on the discharge leg only.
    wear_eur_per_kwh: float = 0.0
    allow_grid_charge: bool = True
    allow_export: bool = True
    max_import_kw: float = 0.0
    max_export_kw: float = 0.0
    min_spread_eur_per_kwh: float = 0.0
    #: How the objective prices PV energy charged into the pack (SPEC 8.1, 0.19.0): ``auto`` = the
    #: slot's sell (the export forgone), ``prefer_pv`` = 0, ``avoid_pv`` = the slot's buy.  An
    #: internal shadow price: the reported plan money stays the true grid cost of the schedule.
    pv_charge_preference: str = "auto"
    target_soc_pct_end: float | None = None
    #: Cost-basis sell floor (SPEC 5, additive 0.18.0): the fleet's energy-weighted mean
    #: break-even of the energy actually in the packs, EUR per kWh AC delivered, plus the
    #: configured margin. ``None`` = no floor (setting off, or no ledger yet) and today's plan.
    #: The engine derives it from the ledger and passes it in; the DP never reads the store.
    sell_floor_eur_per_kwh: float | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> "OptimizerOptions":
        get = settings.get
        return cls(
            slot_minutes=int(get("optimizer.slot_minutes", 60) or 60),
            horizon_hours=int(get("optimizer.horizon_hours", 30) or 30),
            soc_step_kwh=float(get("optimizer.soc_step_kwh", 0.02) or 0.02),
            power_steps=int(get("optimizer.power_steps", 24) or 24),
            salvage_multiplier=float(get("optimizer.salvage_multiplier", 1.0)),
            wear_eur_per_kwh=float(get("optimizer.cycle_cost_eur_per_kwh", 0.0) or 0.0),
            allow_grid_charge=bool(get("optimizer.allow_grid_charge", True)),
            allow_export=bool(get("optimizer.allow_export", True)),
            max_import_kw=float(get("optimizer.max_grid_import_w", 0) or 0) / 1000.0,
            max_export_kw=float(get("optimizer.max_grid_export_w", 0) or 0) / 1000.0,
            min_spread_eur_per_kwh=float(get("optimizer.min_spread_eur_per_kwh", 0.0) or 0.0),
            pv_charge_preference=normalize_pv_charge_preference(
                get("optimizer.pv_charge_preference", "auto")
            ),
            target_soc_pct_end=get("optimizer.target_soc_pct_end", None),
        )


@dataclass(frozen=True)
class BatteryAggregate:
    """The controllable pack seen as one battery (SPEC 8.1)."""

    capacity_kwh: float
    e_min_kwh: float
    e_max_kwh: float
    p_charge_kw: float
    p_discharge_kw: float
    eta_charge: float
    eta_discharge: float
    usable_kwh: float
    batteries: int = 0
    #: Delivered-energy-weighted mean of the fleet's per-battery derived wear, EUR per delivered
    #: kWh (SPEC 5). Each battery contributes ``usable kWh × eta_discharge`` delivered per cycle,
    #: so that delivered energy is also the weight. The DP plans one aggregated pack, so one wear
    #: constant represents it.
    wear_eur_per_kwh: float = 0.0

    def soc_pct_for(self, energy_kwh: float) -> float | None:
        """Capacity-weighted mean SoC of the aggregate at ``energy_kwh`` (SPEC 10 `/plan`)."""
        if self.capacity_kwh <= 0:
            return None
        return round(clamp(energy_kwh / self.capacity_kwh * 100.0, 0.0, 100.0), 2)

    @classmethod
    def from_configs(
        cls, configs: Sequence[BatteryConfig], reserve_soc_pct: float = 0.0
    ) -> "BatteryAggregate":
        enabled = [c for c in configs if c.enabled]
        capacity = sum(c.capacity_kwh for c in enabled)

        def weighted(attr: str) -> float:
            if capacity <= 0:
                return 1.0
            return sum(c.capacity_kwh * getattr(c, attr) for c in enabled) / capacity

        # The aggregate round trip is the *product* of the two capacity-weighted per-direction
        # etas.  That is exact when the pack's batteries share one eta pair, and for a pack of very
        # different batteries it understates the mean round trip, because the weighted product
        # sits below both weighted factors.  The DP therefore plans the pack as one battery with
        # this single aggregate round trip, and ``allocate_power`` afterwards spends the best
        # battery first, so the most efficient battery is the one that actually charges while a
        # lossy one sits idle (SPEC 8.1/8.2).

        e_min = sum(
            c.capacity_kwh * max(c.min_soc_pct, reserve_soc_pct) / 100.0 for c in enabled
        )
        e_max = sum(c.capacity_kwh * c.max_soc_pct / 100.0 for c in enabled)
        wear_weight_kwh = sum(c.usable_kwh() * c.eta_discharge for c in enabled)
        wear = (
            sum(c.usable_kwh() * c.eta_discharge * c.wear_eur_per_kwh for c in enabled)
            / wear_weight_kwh
            if wear_weight_kwh > 0
            else 0.0
        )
        return cls(
            capacity_kwh=capacity,
            e_min_kwh=e_min,
            e_max_kwh=e_max,
            p_charge_kw=sum(c.max_charge_w for c in enabled) / 1000.0,
            p_discharge_kw=sum(c.max_discharge_w for c in enabled) / 1000.0,
            eta_charge=weighted("eta_charge"),
            eta_discharge=weighted("eta_discharge"),
            usable_kwh=max(0.0, e_max - e_min),
            batteries=len(enabled),
            wear_eur_per_kwh=wear,
        )

    def soc_pct(self, energy_kwh: float) -> float:
        if self.capacity_kwh <= 0:
            return 0.0
        return clamp(energy_kwh / self.capacity_kwh * 100.0, 0.0, 100.0)


@dataclass(frozen=True)
class SlotInput:
    """One optimiser slot: forecasts already aligned on the plan grid."""

    t: datetime
    pv_kw: float
    load_kw: float
    buy: float
    sell: float
    load_p10_kw: float | None = None
    load_p90_kw: float | None = None
    price_stale: bool = False
    pv_source: str = ""  # provider that supplied pv_kw for this slot
    price_source: str = ""  # provider that supplied buy/sell for this slot


def _forward_fill(times: Sequence[datetime], points: Sequence[datetime], values: Sequence[Any]):
    """Value of the last point at or before each time (first point back-fills earlier times)."""
    idx = 0
    out: list[tuple[Any, datetime | None]] = []
    for t in times:
        while idx + 1 < len(points) and points[idx + 1] <= t:
            idx += 1
        if points and points[idx] <= t:
            out.append((values[idx], points[idx]))
        elif points:
            out.append((values[0], points[0]))
        else:
            out.append((None, None))
    return out


def align_slots(
    price: PriceForecast,
    solar: SolarForecast,
    load: LoadForecast,
    start: datetime,
    end: datetime,
    minutes: int,
) -> list[SlotInput]:
    """Align price/solar/load onto the plan grid by step-interpolation (SPEC section 4/7).

    Price slots are gap-filled from the last known value with ``stale=True``.  When every price
    slot is stale the horizon is treated as flat (equal prices) so the optimiser cannot invent
    arbitrage from a dead forecast.
    """
    times = slot_range(start, end, minutes)
    if not times:
        return []
    if not price.slots:
        raise ValueError("price forecast has no slots")
    if not load.slots:
        raise ValueError("load forecast has no slots")

    price_by_t = sorted(price.slots, key=lambda s: s.t)
    solar_by_t = sorted(solar.slots, key=lambda s: s.t)
    load_by_t = sorted(load.slots, key=lambda s: s.t)

    prices = _forward_fill(times, [s.t for s in price_by_t], list(price_by_t))
    solars = _forward_fill(times, [s.t for s in solar_by_t], list(solar_by_t))
    loads = _forward_fill(times, [s.t for s in load_by_t], list(load_by_t))

    out: list[SlotInput] = []
    for i, t in enumerate(times):
        pslot, pt = prices[i]
        sslot, _ = solars[i]
        lslot, _ = loads[i]
        if pslot is None:
            pslot = price_by_t[0]
            pt = pslot.t
        stale = bool(pslot.stale) or abs((t - pt).total_seconds()) > 60.0
        out.append(
            SlotInput(
                t=t,
                pv_kw=max(0.0, float(sslot.pv_kw)) if sslot is not None else 0.0,
                load_kw=float(lslot.load_kw) if lslot is not None else 0.0,
                buy=float(pslot.buy),
                sell=float(pslot.sell),
                load_p10_kw=getattr(lslot, "p10_kw", None),
                load_p90_kw=getattr(lslot, "p90_kw", None),
                price_stale=stale,
                pv_source=str(getattr(sslot, "source", "") or getattr(solar, "source", "") or "")
                if sslot is not None
                else "",
                price_source=str(getattr(pslot, "source", "") or getattr(price, "source", "") or ""),
            )
        )
    if out and all(slot.price_stale for slot in out):
        # A dead forecast must not invent arbitrage: treat the horizon as flat at the median of
        # whatever the provider last said (SPEC section 4).
        flat_buy = _median([slot.buy for slot in out])
        flat_sell = _median([slot.sell for slot in out])
        out = [
            replace(slot, buy=flat_buy, sell=flat_sell, price_stale=True) for slot in out
        ]
    return out


def _finite_float(value: Any) -> float | None:
    """A finite float for a settings/override value, else ``None`` (never a guess)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = clamp(fraction, 0.0, 1.0) * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    frac = index - low
    return ordered[low] + frac * (ordered[high] - ordered[low])


def grid_kw_at(slot: SlotInput, p_kw: float) -> float:
    """Metered grid flow for a charging-positive battery power (positive = import)."""
    return slot.load_kw - slot.pv_kw + p_kw


def plan_grid_kw(slot: PlanSlot) -> float:
    """Same, for a reconstructed plan slot.

    Kept as the optimiser's own one-liner; ``_summary`` and ``_grid_violation`` deliberately call the
    frozen :meth:`models.PlanSlot.grid_kw` instead, so the reported cost and the shipped helper can
    never diverge (a regression test pins that equality).
    """
    return slot.load_kw - slot.pv_kw + slot.power_w / 1000.0


def slot_cost_eur(p_kw: float, slot: SlotInput, dt_hours: float, opts: OptimizerOptions) -> float:
    """Grid cost of one slot at battery power ``p_kw`` (positive = charging).

    With a non-``auto`` ``opts.pv_charge_preference`` the PV part of a charge carries an internal
    shadow price (SPEC 8.1, 0.19.0): the objective values that energy at zero (``prefer_pv``) or at
    the slot's buy (``avoid_pv``) instead of the export it forgoes.  Only the solver's objective is
    affected -- ``_summary`` still reports the true grid cost of the returned schedule.
    """
    grid = grid_kw_at(slot, p_kw)
    # An unknown sell price means "no export revenue", never a crash (audit minor).
    sell = float(slot.sell or 0.0)
    cost = dt_hours * (slot.buy * max(grid, 0.0) - sell * max(-grid, 0.0))
    if p_kw > 0:
        surplus = max(0.0, slot.pv_kw - slot.load_kw)
        pv_kwh = min(p_kw, surplus)
        pv_price = _pv_price_eur_per_kwh(slot, opts)
        if pv_kwh > 0.0 and pv_price != sell:
            cost += dt_hours * (pv_price - sell) * pv_kwh
        if opts.min_spread_eur_per_kwh > 0:
            # A thin-spread guard is charged to energy that has to come from the grid: charging
            # above the PV surplus has to earn back ``min_spread`` before it is worth doing at all.
            cost += opts.min_spread_eur_per_kwh * max(0.0, p_kw - surplus) * dt_hours
    return cost


def _pv_price_eur_per_kwh(slot: SlotInput, opts: OptimizerOptions) -> float:
    """The price the objective assigns to PV energy charged into the pack in this slot."""
    if opts.pv_charge_preference == "prefer_pv":
        return 0.0
    if opts.pv_charge_preference == "avoid_pv":
        return float(slot.buy)
    return float(slot.sell or 0.0)  # auto: the export the charge forgoes


def next_energy_kwh(e_kwh: float, p_kw: float, dt_hours: float, agg: BatteryAggregate) -> float:
    """Exact energy transition, including at ``p = 0`` (SPEC 8.1)."""
    if p_kw >= 0:
        return e_kwh + dt_hours * p_kw * agg.eta_charge
    return e_kwh + dt_hours * p_kw / agg.eta_discharge


def wear_cost_eur(
    wear_eur_per_kwh: float, eta_discharge: float, e_from: float, e_to: float
) -> float:
    """Wear of one transition: charged per kWh **delivered**, so only the discharge leg pays.

    A discharge from ``e_from`` to ``e_to`` moves ``e_from - e_to`` stored kWh and delivers
    ``(e_from - e_to) * eta_discharge`` AC kWh; charging and holding cost no wear (SPEC 8.1).
    """
    return wear_eur_per_kwh * eta_discharge * max(0.0, e_from - e_to)


def exact_power_kw(
    e_kwh: float, e_target: float, dt_hours: float, agg: BatteryAggregate
) -> float:
    """Power that lands exactly on ``e_target`` from ``e_kwh`` in one slot."""
    delta = e_target - e_kwh
    if delta >= 0:
        return delta / (dt_hours * agg.eta_charge) if dt_hours > 0 else 0.0
    return delta * agg.eta_discharge / dt_hours if dt_hours > 0 else 0.0


def soc_window_kw(e_kwh: float, agg: BatteryAggregate, dt_hours: float) -> tuple[float, float]:
    """SoC-safe ``[lo, hi]`` for one state: the hardware envelope narrowed by reachability.

    One half of the interval :func:`power_bounds_kw` intersects; kept separate so a caller can
    tell which window the chosen power really sits on (0.20.2).  ``lo <= hi`` always.
    """
    lo, hi = -agg.p_discharge_kw, agg.p_charge_kw
    if dt_hours > 0 and agg.eta_charge > 0 and agg.eta_discharge > 0:
        hi = min(hi, max(0.0, agg.e_max_kwh - e_kwh) / (dt_hours * agg.eta_charge))
        lo = max(lo, -max(0.0, e_kwh - agg.e_min_kwh) * agg.eta_discharge / dt_hours)
    return min(lo, hi), hi


def grid_window_kw(
    slot: SlotInput, agg: BatteryAggregate, opts: OptimizerOptions
) -> tuple[float, float]:
    """Grid-feasible ``[lo, hi]`` for one slot, from the limits alone (no SoC state).

    Import/export caps, the no-export rules and the cost-basis sell floor.  The other half of
    :func:`power_bounds_kw`'s interval.
    """
    base = slot.load_kw - slot.pv_kw
    lo, hi = -agg.p_discharge_kw, agg.p_charge_kw
    if opts.max_import_kw > 0:
        hi = min(hi, opts.max_import_kw - base)  # import cap: base + p <= limit
    if opts.max_export_kw > 0:
        lo = max(lo, -(opts.max_export_kw + base))  # export cap: -(base + p) <= limit
    # Exporting at a zero (or unknown) sell price earns nothing and only spends energy, so it is
    # never planned -- independent of ``allow_export`` (audit minor).
    if not opts.allow_export or slot.sell is None or float(slot.sell) <= 0.0:
        lo = max(lo, -base)
    if not opts.allow_grid_charge:
        hi = min(hi, max(0.0, slot.pv_kw - slot.load_kw))

    # Cost-basis sell floor (SPEC 5, 0.18.0): a discharge/export whose *value leg* sits below the
    # break-even of what the energy in the pack actually cost is refused. The leg is the slot's
    # ``sell`` when the flow exports and the slot's ``buy`` when it serves the house, and
    # ``grid_kw_at`` is exactly the sign test (SPEC 8.1). Charging is never touched. When the
    # house-serving leg is refused while the export leg would pass (an inverted tariff where
    # ``sell > buy``), the whole discharge is refused: the feasible set stays one interval.
    floor = opts.sell_floor_eur_per_kwh
    if floor is not None:
        if float(slot.sell or 0.0) < floor:
            # No flow below the house's own demand: nothing may be exported below the floor.  A PV
            # surplus (``base < 0``) has no cost basis and keeps exporting freely, so the bound is
            # the house demand alone -- never ``-base``, which turned a surplus into a *positive*
            # lower bound and forced the pack to swallow it at any price (0.19.0 fix, SPEC 8.14).
            lo = max(lo, -max(0.0, base))
        if base > 0.0 and float(slot.buy) < floor:
            # Even displacing import cannot pay for the held energy: hold instead of discharge.
            lo = max(lo, 0.0)
    return lo, hi


def power_bounds_kw(
    slot: SlotInput, e_kwh: float, agg: BatteryAggregate, opts: OptimizerOptions, dt_hours: float
) -> tuple[float, float, bool]:
    """Feasible ``[p_lo, p_hi]`` for a state, clipped by SoC reachability and grid limits.

    Returns ``(lo, hi, grid_binding)`` where ``grid_binding`` says the grid limits actually
    tightened the SoC-safe window (not merely that limits are configured).  The interval is always
    valid (``lo <= hi``): when the grid and SoC windows do not intersect, the value nearest the
    grid window is held inside the SoC-safe envelope; such slots are reported as
    ``grid_limits_infeasible`` by the caller.
    """
    soc_lo, soc_hi = soc_window_kw(e_kwh, agg, dt_hours)
    grid_lo, grid_hi = grid_window_kw(slot, agg, opts)
    lo = max(soc_lo, grid_lo)
    hi = min(soc_hi, grid_hi)
    if lo > hi:
        middle = (grid_lo + grid_hi) / 2.0
        lo = hi = clamp(middle, soc_lo, soc_hi)
    if hi < lo:  # numerically impossible, but never return an inverted interval
        lo = hi = clamp(0.0, soc_lo, soc_hi)
    binding = grid_lo > soc_lo + EPS_KW or grid_hi < soc_hi - EPS_KW
    return lo, hi, binding


def _actions(lo: float, hi: float, steps: int, extra: float | None = None) -> list[float]:
    """Evenly spaced actions in ``[lo, hi]``, always including 0 when it is feasible."""
    steps = max(1, int(steps))
    if hi - lo <= EPS_KW:
        return [clamp(0.0, lo, hi)]
    if steps == 1:
        values = [(lo + hi) / 2.0]
    else:
        values = [lo + (hi - lo) * j / (steps - 1) for j in range(steps)]
    values.append(clamp(0.0, lo, hi))
    if extra is not None and lo - EPS_KW <= extra <= hi + EPS_KW:
        values.append(clamp(extra, lo, hi))
    out: list[float] = []
    for value in sorted(values):
        if not out or abs(value - out[-1]) > 1e-9:
            out.append(value)
    return out


def _interp(values: Sequence[float], grid: Sequence[float], e_kwh: float) -> float:
    if not grid:
        return 0.0
    if e_kwh <= grid[0]:
        return values[0]
    if e_kwh >= grid[-1]:
        return values[-1]
    i = max(0, bisect_right(grid, e_kwh) - 1)
    if i + 1 >= len(grid):
        return values[-1]
    span = grid[i + 1] - grid[i]
    if span <= 0:
        return values[i]
    return values[i] + (e_kwh - grid[i]) / span * (values[i + 1] - values[i])


def _build_grid(
    agg: BatteryAggregate, step_kwh: float, anchors: Sequence[float]
) -> tuple[list[float], float]:
    span = max(0.0, agg.e_max_kwh - agg.e_min_kwh)
    step = max(1e-4, float(step_kwh))
    budget = MAX_STATES - 3  # reserve room for the anchor points
    if span / step > budget - 1:
        step = span / (budget - 1)
    points = [agg.e_min_kwh + i * step for i in range(max(1, int(span / step) + 1))]
    if points[-1] < agg.e_max_kwh - EPS_KWH:
        points.append(agg.e_max_kwh)
    for anchor in anchors:
        if anchor is None:
            continue
        value = clamp(float(anchor), agg.e_min_kwh, agg.e_max_kwh)
        if all(abs(value - point) > 1e-6 for point in points):
            points.append(value)
    points.sort()
    return points, step


@dataclass
class _DpResult:
    slots: list[PlanSlot]
    iterations: int
    effective_step_kwh: float
    terminal_error_kwh: float
    cycle_cost_eur: float = 0.0
    #: Objective of the *returned* schedule: grid + min_spread + the PV shadow price + cycle cost
    #: - terminal salvage (plus the terminal-target penalty when one was set), i.e. exactly what
    #: the DP minimised.
    objective_eur: float = 0.0
    #: The value function's entry at the start state, before path reconstruction (they agree unless
    #: the reconstructed path leaves the discretised grid; the schedule's own value is reported).
    internal_objective_eur: float = 0.0


def _solve_once(
    slots: Sequence[SlotInput],
    agg: BatteryAggregate,
    start_kwh: float,
    opts: OptimizerOptions,
    target_penalty: float,
    e_target: float | None,
    salvage: float,
    minutes_of: Sequence[float] | None = None,
) -> _DpResult:
    # Per-slot modelled duration in minutes (0.20.1, SPEC 8.16): the caller passes a shorter first
    # slot when the plan is made mid-slot; ``None`` keeps the uniform pre-0.20.1 model so existing
    # callers stay bit-for-bit unchanged.
    minutes = (
        [float(value) for value in minutes_of]
        if minutes_of is not None
        else [float(opts.slot_minutes)] * len(slots)
    )
    dt = opts.slot_minutes / 60.0  # uniform reference, used only by _classify's tail heuristic
    anchors = [clamp(start_kwh, agg.e_min_kwh, agg.e_max_kwh)]
    if e_target is not None:
        anchors.append(e_target)
    grid, step = _build_grid(agg, opts.soc_step_kwh, anchors)
    n = len(slots)
    # Complexity guard (SPEC 8.2): slots x states x actions stays under the iteration budget.
    action_count = max(2, int(opts.power_steps) + 1)
    budget = max(2, MAX_ITERATIONS // max(1, n * action_count))
    if len(grid) > budget:
        span = max(0.0, agg.e_max_kwh - agg.e_min_kwh)
        grid, step = _build_grid(agg, max(step, span / max(1, budget - 1)), anchors)
        log.debug(
            "optimizer: coarsened to %d states (step %.4f kWh) for %d slots x %d actions",
            len(grid),
            step,
            n,
            action_count,
        )

    value = [[0.0] * len(grid) for _ in range(n + 1)]
    for k, e in enumerate(grid):
        terminal = -salvage * e
        if e_target is not None:
            terminal += target_penalty * abs(e - e_target)
        value[n][k] = terminal

    iterations = 0
    argmin: list[list[float]] = [[0.0] * len(grid) for _ in range(n)]
    for i in range(n - 1, -1, -1):
        slot = slots[i]
        dt_hours = minutes[i] / 60.0
        next_value = value[i + 1]
        row = value[i]
        choices = argmin[i]
        for k, e in enumerate(grid):
            lo, hi, _ = power_bounds_kw(slot, e, agg, opts, dt_hours)
            extra = (
                exact_power_kw(e, e_target, dt_hours, agg)
                if (i == n - 1 and e_target is not None)
                else None
            )
            best = float("inf")
            best_p = clamp(0.0, lo, hi)
            for p in _actions(lo, hi, opts.power_steps, extra):
                e_next = next_energy_kwh(e, p, dt_hours, agg)
                cost = slot_cost_eur(p, slot, dt_hours, opts)
                cost += wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, e, e_next)
                cost += _interp(next_value, grid, e_next)
                iterations += 1
                if cost < best:  # strict: ties resolve to the lowest power (deterministic)
                    best = cost
                    best_p = p
            row[k] = best
            choices[k] = best_p

    def best_at(i: int, e: float) -> float:
        dt_hours = minutes[i] / 60.0
        for index, point in enumerate(grid):
            if abs(point - e) <= 1e-9:
                return argmin[i][index]
        lo, hi, _ = power_bounds_kw(slots[i], e, agg, opts, dt_hours)
        extra = (
            exact_power_kw(e, e_target, dt_hours, agg)
            if (i == n - 1 and e_target is not None)
            else None
        )
        best = float("inf")
        best_p = clamp(0.0, lo, hi)
        for p in _actions(lo, hi, opts.power_steps, extra):
            e_next = next_energy_kwh(e, p, dt_hours, agg)
            cost = slot_cost_eur(p, slots[i], dt_hours, opts)
            cost += wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, e, e_next)
            cost += _interp(value[i + 1], grid, e_next)
            if cost < best:
                best = cost
                best_p = p
        return best_p

    energies: list[float] = []
    powers: list[float] = []
    #: The two windows ``power_bounds_kw`` intersected, kept per slot so ``_classify`` can name
    #: the constraint the chosen power really sits on (0.20.2).
    windows: list[tuple[float, float, float, float]] = []
    cycle_cost = 0.0
    objective = 0.0
    e_now = clamp(start_kwh, agg.e_min_kwh, agg.e_max_kwh)
    for i, slot in enumerate(slots):
        dt_hours = minutes[i] / 60.0
        lo, hi, _ = power_bounds_kw(slot, e_now, agg, opts, dt_hours)
        p = clamp(best_at(i, e_now), lo, hi)
        energies.append(e_now)
        powers.append(p)
        soc_lo, soc_hi = soc_window_kw(e_now, agg, dt_hours)
        grid_lo, grid_hi = grid_window_kw(slot, agg, opts)
        windows.append((soc_lo, soc_hi, grid_lo, grid_hi))
        e_next = next_energy_kwh(e_now, p, dt_hours, agg)
        cycle_cost += wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, e_now, e_next)
        objective += slot_cost_eur(p, slot, dt_hours, opts)
        e_now = clamp(e_next, agg.e_min_kwh, agg.e_max_kwh)
    objective += cycle_cost - salvage * e_now
    if e_target is not None:
        objective += target_penalty * abs(e_now - e_target)
    terminal_error = abs(e_now - e_target) if e_target is not None else 0.0
    start_index = min(range(len(grid)), key=lambda k: abs(grid[k] - clamp(start_kwh, agg.e_min_kwh, agg.e_max_kwh)))

    reasons = _classify(slots, agg, energies, powers, windows, e_target, start_kwh, dt, step, opts)
    plan_slots = [
        PlanSlot(
            t=slot.t,
            buy=slot.buy,
            sell=float(slot.sell or 0.0),
            pv_kw=slot.pv_kw,
            load_kw=slot.load_kw,
            soc_kwh=energies[i],
            power_w=powers[i] * 1000.0,
            action=reasons[i][0],
            reason=reasons[i][1],
            load_p10_kw=slot.load_p10_kw,
            load_p90_kw=slot.load_p90_kw,
            price_stale=slot.price_stale,
            soc_pct=agg.soc_pct_for(energies[i]),
            pv_source=slot.pv_source,
            price_source=slot.price_source,
            minutes=minutes[i],
        )
        for i, slot in enumerate(slots)
    ]
    return _DpResult(
        slots=plan_slots,
        iterations=iterations,
        effective_step_kwh=step,
        terminal_error_kwh=terminal_error,
        cycle_cost_eur=cycle_cost,
        objective_eur=objective,
        internal_objective_eur=value[0][start_index],
    )


def _cheapest_buy_so_far(slots: Sequence[SlotInput]) -> list[float]:
    """Cheapest buy price at or before each slot: a cycle can only buy energy already available."""
    out: list[float] = []
    cheapest = float("inf")
    for slot in slots:
        cheapest = min(cheapest, float(slot.buy))
        out.append(cheapest)
    return out


def _sell_floor_blocks(slot: SlotInput, opts: OptimizerOptions) -> bool:
    """True when the cost-basis floor (SPEC 5) leaves no discharge possible in this slot."""
    floor = opts.sell_floor_eur_per_kwh
    if floor is None:
        return False
    base = slot.load_kw - slot.pv_kw
    if float(slot.sell or 0.0) < floor and base <= 0.0:
        return True  # every discharge would export below the floor
    return base > 0.0 and float(slot.buy) < floor  # even serving the house cannot pay for it


def _round_trip_blocks_arbitrage(
    slot: SlotInput, cheapest_buy: float, agg: BatteryAggregate, opts: OptimizerOptions
) -> bool:
    """True when a sale looks profitable before the losses but cannot pay them back.

    ``optimizer.min_spread_eur_per_kwh`` is the user's own, separate floor: a spread that never
    cleared it keeps ``Reason.SPREAD_BELOW_THRESHOLD`` (SPEC 8.1).  The round-trip break-even
    itself comes from the frozen ``BatteryConfig`` helper, never a second copy of the formula.
    """
    if cheapest_buy == float("inf"):
        return False
    raw_spread = float(slot.sell) - cheapest_buy
    if raw_spread <= 0.0:
        return False  # the cycle does not even look profitable before the losses
    if opts.min_spread_eur_per_kwh > 0.0 and raw_spread <= opts.min_spread_eur_per_kwh:
        return False  # the user's floor is the binding reason
    break_even = aggregate_break_even_sell_eur_per_kwh(
        agg.eta_charge, agg.eta_discharge, cheapest_buy, opts.wear_eur_per_kwh
    )
    if break_even is None:
        return False
    return float(slot.sell) <= break_even


def _at_grid_hi(p_kw: float, soc_hi: float, grid_hi: float) -> bool:
    """True when ``p`` sits on a grid upper bound that is *tighter* than the SoC ceiling.

    ``power_bounds_kw``'s ``binding`` flag only says some grid rule narrowed the interval, which is
    not the same as the chosen power sitting on a grid bound: the 0.20.1 live plan put a charge on
    the pack's 100 % ceiling while the sell-floor rule made the (unrelated) lower bound ``0``.  A
    bound that merely coincides with the SoC bound is the SoC bound, never a grid one.
    """
    return abs(p_kw - grid_hi) <= EPS_KW and grid_hi < soc_hi - EPS_KW


def _at_grid_lo(p_kw: float, soc_lo: float, grid_lo: float) -> bool:
    """The discharge-side mirror of :func:`_at_grid_hi`."""
    return abs(p_kw - grid_lo) <= EPS_KW and grid_lo > soc_lo + EPS_KW


def _classify(
    slots: Sequence[SlotInput],
    agg: BatteryAggregate,
    energies: Sequence[float],
    powers: Sequence[float],
    windows: Sequence[tuple[float, float, float, float]],
    e_target: float | None,
    start_kwh: float,
    dt: float,
    step_kwh: float,
    opts: OptimizerOptions,
) -> list[tuple[Action, str]]:
    """Deterministic action + reason slugs for a reconstructed schedule (SPEC 8.2)."""
    n = len(slots)
    median_buy = _median([s.buy for s in slots])
    p75_buy = _percentile([s.buy for s in slots], 0.75)
    cheapest_buy = _cheapest_buy_so_far(slots)
    tail = 0
    if e_target is not None and dt > 0:
        eff_power = max(
            agg.p_charge_kw * agg.eta_charge,
            agg.p_discharge_kw / agg.eta_discharge if agg.p_discharge_kw else 0.0,
            1e-6,
        )
        tail = max(1, int(abs(e_target - start_kwh) / max(dt * eff_power, 1e-6)) + 1)

    out: list[tuple[Action, str]] = []
    half_step = max(step_kwh, 1e-6) / 2.0
    for i, slot in enumerate(slots):
        p = powers[i]
        e = energies[i]
        soc_lo, soc_hi, grid_lo, grid_hi = windows[i]
        surplus = max(0.0, slot.pv_kw - slot.load_kw)
        near_target = e_target is not None and i >= n - tail
        if p > 1e-4:
            if _at_grid_hi(p, soc_hi, grid_hi):
                # A *grid* cap can leave a non-zero power: report the direction it holds, so
                # ``sensor.sem_plan_action`` never says "hold" while the battery charges (audit
                # minor).  A power on the SoC ceiling keeps its PV/arbitrage reason.
                action, reason = Action.CHARGE, Reason.GRID_LIMIT_HOLD
            elif p <= surplus + 1e-4:
                action, reason = Action.PV_CHARGE, Reason.PV_SURPLUS_CHARGE
            elif near_target and slot.buy >= median_buy:
                action, reason = Action.CHARGE, Reason.TERMINAL_TARGET
            else:
                action, reason = Action.CHARGE, Reason.ARBITRAGE_CHARGE
        elif p < -1e-4:
            exporting = grid_kw_at(slot, p) < -1e-4
            if _at_grid_lo(p, soc_lo, grid_lo):
                action, reason = Action.DISCHARGE, Reason.GRID_LIMIT_HOLD
            elif near_target and slot.buy <= median_buy:
                action, reason = Action.DISCHARGE, Reason.TERMINAL_TARGET
            elif slot.buy >= p75_buy:
                action = Action.EXPORT if exporting else Action.DISCHARGE
                reason = Reason.PEAK_DISCHARGE
            else:
                action = Action.EXPORT if exporting else Action.DISCHARGE
                reason = Reason.SELF_CONSUMPTION
        else:
            floor_blocks = _sell_floor_blocks(slot, opts)
            if not floor_blocks and (
                _at_grid_lo(0.0, soc_lo, grid_lo) or _at_grid_hi(0.0, soc_hi, grid_hi)
            ):
                action, reason = Action.HOLD, Reason.GRID_LIMIT_HOLD
            elif e <= agg.e_min_kwh + half_step:
                action, reason = Action.HOLD, Reason.RESERVE_HOLD
            elif e >= agg.e_max_kwh - half_step:
                action, reason = Action.HOLD, Reason.FULL
            elif floor_blocks:
                # The energy held cannot pay for this sale at this slot's value leg (SPEC 5).
                action, reason = Action.HOLD, Reason.SELL_FLOOR
            else:
                if _round_trip_blocks_arbitrage(slot, cheapest_buy[i], agg, opts):
                    # The sale looks profitable against the cheapest charge in the horizon, but
                    # the round-trip loss and wear eat the margin, so the battery stays put
                    # (SPEC 8.1).  Distinct from the user's own spread floor.
                    action, reason = Action.HOLD, Reason.EFFICIENCY_LOSS
                else:
                    action, reason = Action.HOLD, Reason.SPREAD_BELOW_THRESHOLD
        out.append((action, reason))
    return out


def median_buy_eur_per_kwh(slots: Sequence[Any]) -> float:
    """The reference price the terminal value assumes: the median buy over the horizon (SPEC 8.1)."""
    return _median([float(slot.buy) for slot in slots])


def _summary(
    plan_slots: Sequence[PlanSlot],
    agg: BatteryAggregate,
    dt: float,
    iterations: int,
    step_kwh: float,
    *,
    cycle_cost_eur: float = 0.0,
    objective_eur: float = 0.0,
    wear_eur_per_kwh: float = 0.0,
    maintenance: Mapping[str, Any] | None = None,
    sell_floor_eur_per_kwh: float | None = None,
) -> PlanSummary:
    def hours_of(slot: PlanSlot) -> float:
        # The duration the slot was actually modelled with (0.20.1, SPEC 8.16); ``dt`` is only the
        # fallback for a slot that carries none (an older stored plan or a hand-built fixture).
        return slot.minutes / 60.0 if slot.minutes > 0 else dt

    cost = 0.0
    for s in plan_slots:
        # The frozen PlanSlot helper is the source of truth for a slot's cost (SPEC 8.1).
        cost += s.cost_eur(hours_of(s))
    baseline = sum(
        hours_of(s)
        * (
            s.buy * max(s.load_kw - s.pv_kw, 0.0)
            - float(s.sell or 0.0) * max(s.pv_kw - s.load_kw, 0.0)
        )
        for s in plan_slots
    )
    charged = sum(max(0.0, s.power_w) / 1000.0 * hours_of(s) for s in plan_slots)
    discharged = sum(max(0.0, -s.power_w) / 1000.0 * hours_of(s) for s in plan_slots)
    pv_charge = sum(
        min(max(0.0, s.power_w) / 1000.0, max(0.0, s.pv_kw - s.load_kw)) * hours_of(s)
        for s in plan_slots
    )
    grid_charge = max(0.0, charged - pv_charge)
    summary = PlanSummary(
        cost_estimate_eur=cost,
        baseline_cost_eur=baseline,
        saved_eur=baseline - cost,
        charged_kwh=charged,
        discharged_kwh=discharged,
        grid_charge_kwh=grid_charge,
        pv_charge_kwh=pv_charge,
        cycles=(discharged / agg.usable_kwh) if agg.usable_kwh > 0 else 0.0,
        arbitrage_kwh=min(grid_charge, discharged),
        effective_step_kwh=step_kwh,
        solver_iterations=iterations,
        cycle_cost_eur=cycle_cost_eur,
        objective_eur=objective_eur,
    )
    # The efficiency the plan assumed (SPEC 5/8.1): the capacity-weighted aggregate, with the
    # break-even taken at the same median buy price the salvage uses.  ``models.PlanSummary`` is
    # orchestrator-owned, so these additive values ride on the summary instance (the store already
    # persists ``summary.__dict__`` and ignores unknown keys when loading) and the API reads them
    # through ``getattr``.
    summary.eta_charge = agg.eta_charge
    summary.eta_discharge = agg.eta_discharge
    summary.round_trip_efficiency = agg.eta_charge * agg.eta_discharge
    summary.wear_eur_per_kwh = wear_eur_per_kwh
    summary.break_even_sell_eur_per_kwh = aggregate_break_even_sell_eur_per_kwh(
        agg.eta_charge, agg.eta_discharge, median_buy_eur_per_kwh(plan_slots), wear_eur_per_kwh
    )
    # Cost-basis sell floor (SPEC 5): the floor the DP enforced, so /plan and the UI can state it.
    summary.sell_floor_eur_per_kwh = sell_floor_eur_per_kwh
    # Scheduled full-charge maintenance (SPEC 8.1): the block the engine computed, carried verbatim
    # so the API/UI can say which batteries the plan charges to balance and against which deadline.
    summary.maintenance = dict(maintenance) if maintenance else None
    return summary


def _grid_violation(plan: Plan, options: OptimizerOptions) -> int | None:
    """Index of the first slot whose grid flow cannot respect the configured limits."""
    for i, slot in enumerate(plan.slots):
        grid_kw = slot.grid_kw()
        if options.max_import_kw > 0 and grid_kw > options.max_import_kw + 1e-6:
            return i
        if options.max_export_kw > 0 and -grid_kw > options.max_export_kw + 1e-6:
            return i
        if not options.allow_export and grid_kw < -1e-6:
            return i
    return None


def solve(
    slots: Sequence[SlotInput],
    *,
    aggregate: BatteryAggregate,
    start_kwh: float,
    options: OptimizerOptions,
    generated_at: datetime | None = None,
    source_names: Mapping[str, str] | None = None,
    mode: str = "advisory",
    dry_run: bool = True,
    maintenance: Mapping[str, Any] | None = None,
    sell_floor_eur_per_kwh: float | None = None,
    now: datetime | None = None,
) -> Plan:
    """Solve the horizon and return a ``Plan`` (SPEC 8.2).  Deterministic.

    ``maintenance`` is the scheduled-full-charge block from ``app.full_charge.plan_block``: while it
    is ``active`` the plan aims for the higher of ``optimizer.target_soc_pct_end`` and the
    maintenance target, reusing the existing hard terminal-target machinery so the DP charges in
    the cheapest slots before the deadline.  When it is absent or inactive the terminal target is
    exactly the user's setting: a non-due battery never distorts the schedule.

    ``sell_floor_eur_per_kwh`` is the engine's cost-basis floor (SPEC 5, 0.18.0): when set, no
    discharge/export transition may carry a value leg below it. ``None`` keeps the schedule
    bit-for-bit as before.

    ``now`` is the moment the plan is made (0.20.1, SPEC 8.16): when it falls inside the first
    slot, that slot is modelled with only the minutes left in its clock window (a 1-minute floor
    guards a sub-minute or negative remainder), so the schedule, its energy and its money all
    match the time the dispatcher really has.  ``None`` keeps the uniform full-slot model, so an
    older caller (or a recompute for a plan already on the grid) is bit-for-bit unchanged.
    """
    moment = generated_at or utcnow()
    sources = dict(source_names or {})
    if sell_floor_eur_per_kwh is not None:
        options = replace(options, sell_floor_eur_per_kwh=float(sell_floor_eur_per_kwh))
    if options.wear_eur_per_kwh <= 0.0 and aggregate.wear_eur_per_kwh > 0.0:
        # Zero means "derive it": the aggregate already carries the fleet's delivered-energy-weighted mean
        # of the per-battery derived wear (SPEC 5). A non-zero setting is the advanced override
        # and wins. Resolved here so every caller (API recompute, tests, selfcheck) agrees.
        options = replace(options, wear_eur_per_kwh=aggregate.wear_eur_per_kwh)
    active_maintenance: dict[str, Any] | None = None
    target_soc_pct_end = options.target_soc_pct_end
    if maintenance and maintenance.get("active"):
        active_maintenance = dict(maintenance)
        configured = _finite_float(active_maintenance.get("target_soc_pct"))
        if configured is not None:
            current = _finite_float(target_soc_pct_end)
            target_soc_pct_end = configured if current is None else max(current, configured)
            active_maintenance["effective_target_soc_pct"] = target_soc_pct_end
        options = replace(options, target_soc_pct_end=target_soc_pct_end)
    horizon_start = slots[0].t if slots else quarter_floor(moment, options.slot_minutes)
    horizon_end = (
        slots[-1].t + timedelta(minutes=options.slot_minutes)
        if slots
        else horizon_start + timedelta(hours=clamp(options.horizon_hours, 12, 48))
    )

    def make_plan(slots_out, summary, feasible=True, reason=None) -> Plan:
        return Plan(
            generated_at=moment,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            slot_minutes=options.slot_minutes,
            feasible=feasible,
            infeasible_reason=reason,
            slots=list(slots_out),
            summary=summary,
            mode=mode,
            dry_run=dry_run,
            sources=sources,
            maintenance=active_maintenance,
        )

    if not slots:
        return make_plan([], PlanSummary(), feasible=False, reason=Reason.NO_SLOTS)
    # Per-slot modelled duration (0.20.1, SPEC 8.16).  The grid and the timestamps do not move;
    # only a first slot that began before ``now`` is shortened to the time it really has left.
    minutes_of = [float(options.slot_minutes)] * len(slots)
    if now is not None:
        slot_end = slots[0].t + timedelta(minutes=options.slot_minutes)
        minutes_of[0] = clamp(
            (slot_end - now).total_seconds() / 60.0, 1.0, float(options.slot_minutes)
        )
    if aggregate.batteries == 0 or aggregate.e_max_kwh <= 0:
        # No controllable battery: publish the do-nothing schedule so the timeline still renders.
        dt = options.slot_minutes / 60.0
        plan_slots = [
            PlanSlot(
                t=s.t,
                buy=s.buy,
                sell=s.sell,
                pv_kw=s.pv_kw,
                load_kw=s.load_kw,
                soc_kwh=0.0,
                power_w=0.0,
                action=Action.HOLD,
                reason=Reason.RESERVE_HOLD,
                load_p10_kw=s.load_p10_kw,
                load_p90_kw=s.load_p90_kw,
                price_stale=s.price_stale,
                soc_pct=aggregate.soc_pct_for(0.0),
                pv_source=s.pv_source,
                price_source=s.price_source,
                minutes=minutes_of[i],
            )
            for i, s in enumerate(slots)
        ]
        return make_plan(
            plan_slots,
            _summary(
                plan_slots,
                aggregate,
                dt,
                0,
                0.0,
                maintenance=active_maintenance,
                sell_floor_eur_per_kwh=options.sell_floor_eur_per_kwh,
            ),
        )

    dt = options.slot_minutes / 60.0
    # Stored energy only delivers eta_dis AC per stored kWh, so the terminal value is the buy
    # price discounted by the discharge efficiency (SPEC 8.1).
    salvage = (
        options.salvage_multiplier * _median([s.buy for s in slots]) * aggregate.eta_discharge
    )
    e_target: float | None = None
    if options.target_soc_pct_end is not None and aggregate.capacity_kwh > 0:
        e_target = clamp(
            aggregate.capacity_kwh * float(options.target_soc_pct_end) / 100.0,
            aggregate.e_min_kwh,
            aggregate.e_max_kwh,
        )

    result = _solve_once(
        slots, aggregate, start_kwh, options, TARGET_HARD_PENALTY, e_target, salvage,
        minutes_of=minutes_of,
    )
    infeasible: str | None = None
    if e_target is not None:
        tolerance = max(result.effective_step_kwh / 2.0, 1e-6)
        if result.terminal_error_kwh > tolerance:
            # Documented downgrade: an unreachable terminal target becomes a soft penalty plus a
            # reason, never an exception (SPEC 8.1).
            result = _solve_once(
                slots, aggregate, start_kwh, options, TARGET_SOFT_PENALTY, e_target, salvage,
                minutes_of=minutes_of,
            )
            infeasible = "terminal_target_unreachable"

    plan = make_plan(
        result.slots,
        _summary(
            result.slots,
            aggregate,
            dt,
            result.iterations,
            result.effective_step_kwh,
            cycle_cost_eur=result.cycle_cost_eur,
            objective_eur=result.objective_eur,
            wear_eur_per_kwh=options.wear_eur_per_kwh,
            maintenance=active_maintenance,
            sell_floor_eur_per_kwh=options.sell_floor_eur_per_kwh,
        ),
    )
    if infeasible is not None:
        plan.feasible = False
        plan.infeasible_reason = infeasible
    elif _grid_violation(plan, options) is not None:
        plan.feasible = False
        plan.infeasible_reason = "grid_limits_infeasible"
    log.debug(
        "optimizer: %d slots, step %.4f kWh, %d iterations, target=%s",
        len(result.slots),
        result.effective_step_kwh,
        result.iterations,
        e_target,
    )
    return plan


def solve_from_forecasts(
    price: PriceForecast,
    solar: SolarForecast,
    load: LoadForecast,
    *,
    battery_configs: Sequence[BatteryConfig],
    start_kwh: float,
    settings: Any,
    now: datetime | None = None,
    mode: str = "advisory",
    dry_run: bool = True,
    maintenance: Mapping[str, Any] | None = None,
    sell_floor_eur_per_kwh: float | None = None,
) -> Plan:
    """Align the three forecasts, then solve.  Raises ``ValueError`` when inputs are unusable."""
    moment = now or utcnow()
    options = OptimizerOptions.from_settings(settings)
    hours = int(clamp(options.horizon_hours, 12, 48))
    start = quarter_floor(moment, options.slot_minutes)
    slots = align_slots(
        price, solar, load, start, start + timedelta(hours=hours), options.slot_minutes
    )
    reserve = float(settings.get("optimizer.reserve_soc_pct", 0.0) or 0.0)
    aggregate = BatteryAggregate.from_configs(battery_configs, reserve)
    sources = {"price": price.source, "solar": solar.source, "load": load.source}
    flattened = bool(slots) and all(s.price_stale for s in slots)
    if flattened:
        sources["price_stale"] = "flat"
    return solve(
        slots,
        aggregate=aggregate,
        start_kwh=start_kwh,
        options=options,
        generated_at=moment,
        source_names=sources,
        mode=mode,
        dry_run=dry_run,
        maintenance=maintenance,
        sell_floor_eur_per_kwh=sell_floor_eur_per_kwh,
        # The horizon start stays floored to the slot grid, so the first slot can begin before
        # ``moment``; passing it lets the solver model that slot with only its remaining minutes
        # (0.20.1, SPEC 8.16).
        now=moment,
    )
