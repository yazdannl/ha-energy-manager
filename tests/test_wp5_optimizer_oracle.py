"""WP5 independent optimizer oracle (SPEC sections 8.1/8.2/14).

This module does **not** touch ``app.optimize.dp`` to build its reference: the oracle is a full
depth-first enumeration over the spec's discrete action set, with the spec's energy transition and
cost model re-derived here from SPEC 8.1.  The shipped DP is only called through its public
``solve`` / ``Plan`` surface for the comparison.

Two kinds of assertions:

* ``dp_objective >= oracle_optimum`` always holds for a *feasible* reconstructed plan, because the
  oracle enumerates every action sequence the DP may reconstruct.  A violation means the DP found
  a path outside the spec's action space (or the cost model diverged).
* On the lattice family every reachable energy lands exactly on a DP grid point (verified below by
  reachability search, not by reading the DP), so the DP must equal the oracle to 1e-6.

All numbers are obviously synthetic.  No instance data.
"""

from __future__ import annotations

import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.models import BatteryConfig, clamp  # noqa: E402
from app.optimize import dp as shipped_dp  # noqa: E402

T0 = datetime(2026, 2, 1, 6, 0, tzinfo=timezone.utc)
EPS = 1e-9
TARGET_HARD_PENALTY = 1e6  # matches the documented hard terminal penalty


# --------------------------------------------------------------------------------------------
# Independent physics (SPEC 8.1) -- no dp.py import used here
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    """Everything the optimisation needs, in spec terms."""

    capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    reserve_pct: float
    max_charge_w: float
    max_discharge_w: float
    eta_charge: float
    eta_discharge: float
    slot_minutes: int
    power_steps: int
    soc_step_kwh: float
    start_kwh: float
    pv_kw: tuple[float, ...]
    load_kw: tuple[float, ...]
    buy: tuple[float, ...]
    sell: tuple[float, ...]
    max_import_kw: float = 0.0
    max_export_kw: float = 0.0
    allow_grid_charge: bool = True
    allow_export: bool = True
    cycle_cost: float = 0.0
    salvage_multiplier: float = 1.0
    target_soc_pct_end: float | None = None
    min_spread_eur_per_kwh: float = 0.0

    @property
    def dt(self) -> float:
        return self.slot_minutes / 60.0

    @property
    def n(self) -> int:
        return len(self.pv_kw)

    @property
    def e_min(self) -> float:
        return self.capacity_kwh * max(self.min_soc_pct, self.reserve_pct) / 100.0

    @property
    def e_max(self) -> float:
        return self.capacity_kwh * self.max_soc_pct / 100.0

    @property
    def p_max(self) -> float:
        return self.max_charge_w / 1000.0

    @property
    def p_min(self) -> float:
        return -self.max_discharge_w / 1000.0

    @property
    def salvage(self) -> float:
        # stored energy only delivers eta_dis AC per stored kWh (SPEC 8.1)
        return self.salvage_multiplier * median(self.buy) * self.eta_discharge

    @property
    def target_kwh(self) -> float | None:
        if self.target_soc_pct_end is None:
            return None
        return clamp(
            self.capacity_kwh * float(self.target_soc_pct_end) / 100.0, self.e_min, self.e_max
        )

    def battery_config(self, battery_id: str = "example_battery_1") -> BatteryConfig:
        return BatteryConfig(
            id=battery_id,
            name="Example Battery",
            driver="generic",
            capacity_kwh=self.capacity_kwh,
            min_soc_pct=self.min_soc_pct,
            max_soc_pct=self.max_soc_pct,
            max_charge_w=self.max_charge_w,
            max_discharge_w=self.max_discharge_w,
            eta_charge=self.eta_charge,
            eta_discharge=self.eta_discharge,
            mapping={"power_setpoint_entity": "number.example_power"},
        )

    def slots(self) -> list:
        base = T0
        return [
            shipped_dp.SlotInput(
                t=base + timedelta(minutes=self.slot_minutes * i),
                pv_kw=self.pv_kw[i],
                load_kw=self.load_kw[i],
                buy=self.buy[i],
                sell=self.sell[i],
            )
            for i in range(self.n)
        ]


def median(values: list[float] | tuple[float, ...]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def grid_kw(inst: Instance, index: int, p_kw: float) -> float:
    """SPEC 8.1 sign convention: p > 0 charges and raises grid import."""
    return inst.load_kw[index] - inst.pv_kw[index] + p_kw


def slot_cost(inst: Instance, index: int, p_kw: float) -> float:
    grid = grid_kw(inst, index, p_kw)
    cost = inst.dt * (
        inst.buy[index] * max(grid, 0.0) - inst.sell[index] * max(-grid, 0.0)
    )
    if inst.min_spread_eur_per_kwh > 0 and p_kw > 0:
        # The shipped interpretation of optimizer.min_spread_eur_per_kwh: grid charging has to
        # earn back the minimum spread; PV surplus is exempt.
        surplus = max(0.0, inst.pv_kw[index] - inst.load_kw[index])
        cost += inst.min_spread_eur_per_kwh * max(0.0, p_kw - surplus) * inst.dt
    return cost


def next_energy(inst: Instance, e_kwh: float, p_kw: float) -> float:
    if p_kw >= 0:
        return e_kwh + inst.dt * p_kw * inst.eta_charge
    return e_kwh + inst.dt * p_kw / inst.eta_discharge


def exact_power_to(inst: Instance, e_kwh: float, target: float) -> float:
    delta = target - e_kwh
    if delta >= 0:
        return delta / (inst.dt * inst.eta_charge)
    return delta * inst.eta_discharge / inst.dt


def feasible_bounds(inst: Instance, index: int, e_kwh: float) -> tuple[float, float, bool]:
    """Spec-feasible [lo, hi] for one slot/state, plus a flag that the fallback was needed."""
    soc_hi = min(inst.p_max, max(0.0, inst.e_max - e_kwh) / (inst.dt * inst.eta_charge))
    soc_lo = max(inst.p_min, -max(0.0, e_kwh - inst.e_min) * inst.eta_discharge / inst.dt)
    soc_lo = min(soc_lo, soc_hi)

    base = inst.load_kw[index] - inst.pv_kw[index]
    grid_hi, grid_lo = inst.p_max, inst.p_min
    if inst.max_import_kw > 0:
        grid_hi = min(grid_hi, inst.max_import_kw - base)
    if inst.max_export_kw > 0:
        grid_lo = max(grid_lo, -(inst.max_export_kw + base))
    if not inst.allow_export:
        grid_lo = max(grid_lo, -base)
    if not inst.allow_grid_charge:
        grid_hi = min(grid_hi, max(0.0, inst.pv_kw[index] - inst.load_kw[index]))

    lo, hi = max(soc_lo, grid_lo), min(soc_hi, grid_hi)
    fallback = lo > hi + EPS
    if fallback:
        # Spec-consistent last resort: keep the SoC envelope, report the grid as infeasible.
        middle = (grid_lo + grid_hi) / 2.0
        lo = hi = clamp(middle, soc_lo, soc_hi)
    return lo, hi, fallback


def action_set(inst: Instance, lo: float, hi: float) -> list[float]:
    """SPEC 8.2: ``power_steps`` evenly spaced values in [lo, hi], always including 0."""
    steps = max(1, int(inst.power_steps))
    if hi - lo <= 1e-12:
        values = [clamp(0.0, lo, hi)]
    elif steps == 1:
        values = [(lo + hi) / 2.0]
    else:
        values = [lo + (hi - lo) * j / (steps - 1) for j in range(steps)]
    values.append(clamp(0.0, lo, hi))
    return sorted({round(v, 10) for v in values})


def oracle_optimum(inst: Instance) -> tuple[float, tuple[float, ...]]:
    """Full DFS over every action sequence; returns (objective, best powers)."""
    best: list[tuple[float, tuple[float, ...]]] = [(math.inf, ())]

    def walk(index: int, energy: float, objective: float, powers: tuple[float, ...]) -> None:
        if index == inst.n:
            total = objective - inst.salvage * energy
            if inst.target_kwh is not None:
                total += TARGET_HARD_PENALTY * abs(energy - inst.target_kwh)
            if total < best[0][0]:
                best[0] = (total, powers)
            return
        lo, hi, _fallback = feasible_bounds(inst, index, energy)
        assert lo <= hi + EPS, (index, energy, lo, hi)
        actions = action_set(inst, lo, hi)
        if inst.target_kwh is not None and index == inst.n - 1:
            extra = exact_power_to(inst, energy, inst.target_kwh)
            if lo - EPS <= extra <= hi + EPS:
                actions = sorted(set(actions) | {round(clamp(extra, lo, hi), 10)})
        for power in actions:
            e_next = next_energy(inst, energy, power)
            cost = slot_cost(inst, index, power) + inst.cycle_cost * inst.eta_discharge * max(0.0, energy - e_next)
            walk(index + 1, e_next, objective + cost, powers + (power,))

    walk(0, inst.start_kwh, 0.0, ())
    return best[0]


def reachable_lattice(inst: Instance) -> set[float]:
    """Every energy reachable from the start under the oracle dynamics (for the exactness check)."""
    seen: set[tuple[int, float]] = set()
    energies: set[float] = set()
    stack = [(0, inst.start_kwh)]
    while stack:
        index, energy = stack.pop()
        key = (index, round(energy, 9))
        if key in seen:
            continue
        seen.add(key)
        if index == inst.n:
            energies.add(round(energy, 9))
            continue
        lo, hi, fallback = feasible_bounds(inst, index, energy)
        if fallback:
            continue
        actions = action_set(inst, lo, hi)
        if inst.target_kwh is not None and index == inst.n - 1:
            extra = exact_power_to(inst, energy, inst.target_kwh)
            if lo - EPS <= extra <= hi + EPS:
                actions = sorted(set(actions) | {round(clamp(extra, lo, hi), 10)})
        for power in actions:
            stack.append((index + 1, next_energy(inst, energy, power)))
    return energies


def on_dp_grid(inst: Instance, energy: float) -> bool:
    """True when ``energy`` is one of ``e_min + k*step`` (the DP's state grid)."""
    offset = (energy - inst.e_min) / inst.soc_step_kwh
    return abs(offset - round(offset)) < 1e-6


def my_aggregate(inst: Instance) -> shipped_dp.BatteryAggregate:
    """Aggregate computed here, then handed to the shipped solver (asserted equal in tests)."""
    return shipped_dp.BatteryAggregate(
        capacity_kwh=inst.capacity_kwh,
        e_min_kwh=inst.e_min,
        e_max_kwh=inst.e_max,
        p_charge_kw=inst.p_max,
        p_discharge_kw=-inst.p_min,
        eta_charge=inst.eta_charge,
        eta_discharge=inst.eta_discharge,
        usable_kwh=inst.e_max - inst.e_min,
        batteries=1,
    )


def plan_objective(inst: Instance, plan) -> float:
    """Exact objective of the reconstructed plan (SPEC 8.1): costs + cycle cost - salvage*e_N."""
    total = 0.0
    energy = None
    for index, slot in enumerate(plan.slots):
        power = slot.power_w / 1000.0
        total += slot_cost(inst, index, power)
        energy = slot.soc_kwh
        e_next = next_energy(inst, energy, power)
        total += inst.cycle_cost * inst.eta_discharge * max(0.0, energy - e_next)
        energy = e_next
    assert energy is not None
    return total - inst.salvage * energy


def solve(inst: Instance, options: shipped_dp.OptimizerOptions | None = None):
    opts = options or shipped_dp.OptimizerOptions(
        slot_minutes=inst.slot_minutes,
        soc_step_kwh=inst.soc_step_kwh,
        power_steps=inst.power_steps,
        salvage_multiplier=inst.salvage_multiplier,
        wear_eur_per_kwh=inst.cycle_cost,
        allow_grid_charge=inst.allow_grid_charge,
        allow_export=inst.allow_export,
        max_import_kw=inst.max_import_kw,
        max_export_kw=inst.max_export_kw,
        target_soc_pct_end=inst.target_soc_pct_end,
        min_spread_eur_per_kwh=inst.min_spread_eur_per_kwh,
    )
    return shipped_dp.solve(
        inst.slots(),
        aggregate=my_aggregate(inst),
        start_kwh=inst.start_kwh,
        options=opts,
        generated_at=T0,
    )


def assert_plan_feasible(inst: Instance, plan, *, grid_caps_required: bool = True) -> None:
    final = inst.start_kwh
    for index, slot in enumerate(plan.slots):
        power = slot.power_w / 1000.0
        # the recorded state must be exactly the previous slot's transition
        assert slot.soc_kwh == pytest.approx(final, abs=1e-9), (index, slot.soc_kwh, final)
        assert math.isfinite(power) and math.isfinite(slot.soc_kwh)
        assert inst.p_min - 1e-6 <= power <= inst.p_max + 1e-6, (index, power)
        grid = grid_kw(inst, index, power)
        if grid_caps_required:
            if inst.max_import_kw > 0:
                assert grid <= inst.max_import_kw + 1e-6, (index, grid)
            if inst.max_export_kw > 0:
                assert -grid <= inst.max_export_kw + 1e-6, (index, grid)
            if not inst.allow_export:
                assert grid >= -1e-6, (index, grid)
            if not inst.allow_grid_charge:
                assert power <= max(0.0, inst.pv_kw[index] - inst.load_kw[index]) + 1e-6
        assert inst.e_min - 1e-6 <= slot.soc_kwh <= inst.e_max + 1e-6, (index, slot.soc_kwh)
        final = next_energy(inst, slot.soc_kwh, power)
        assert inst.e_min - 1e-6 <= final <= inst.e_max + 1e-6
    if inst.target_kwh is not None:
        assert abs(final - inst.target_kwh) < 1e-6, (final, inst.target_kwh)


# --------------------------------------------------------------------------------------------
# Instances
# --------------------------------------------------------------------------------------------

#: 60 kWh pack, 25% reserve floor (so e_min = 15 kWh), 1 kW charge/discharge, eta = 0.5.
#: Every action lands on the 0.5 kWh lattice; energies never reach the SoC bounds.
BASE = dict(
    capacity_kwh=60.0,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
    reserve_pct=25.0,
    max_charge_w=1000.0,
    max_discharge_w=1000.0,
    eta_charge=0.5,
    eta_discharge=0.5,
    slot_minutes=60,
    power_steps=3,
    soc_step_kwh=0.5,
    start_kwh=40.0,
)

ARBITRAGE = Instance(
    **BASE,
    pv_kw=(0.0, 0.0, 2.0, 0.5, 0.0),
    load_kw=(1.0, 0.5, 0.5, 1.0, 1.0),
    buy=(0.05, -0.02, 0.10, 0.45, 0.30),
    sell=(0.01, 0.00, 0.02, 0.10, 0.05),
    cycle_cost=0.01,
)

TARGET = Instance(
    **{**BASE, "target_soc_pct_end": 70.0},
    pv_kw=(0.0, 2.0, 0.0, 0.0, 0.0),
    load_kw=(1.0, 0.5, 1.0, 1.0, 1.0),
    buy=(0.05, 0.08, 0.45, 0.20, 0.30),
    sell=(0.01, 0.02, 0.10, 0.05, 0.05),
)

RESERVE_FLOOR = Instance(
    **BASE,
    pv_kw=(0.0, 0.0, 0.0, 0.0, 0.0),
    load_kw=(1.0, 1.0, 1.0, 1.0, 1.0),
    buy=(0.02, 0.60, 0.02, 0.60, 0.02),
    sell=(0.01, 0.01, 0.01, 0.01, 0.01),
    salvage_multiplier=0.0,  # no free salvage dumping of the floor
)

#: min_spread_eur_per_kwh makes grid charging pay a surcharge; the reported slot cost stays the
#: real grid cost, so this instance pins the difference between the planning objective and the
#: summary.
MIN_SPREAD = Instance(
    **BASE,
    pv_kw=(0.0, 0.0, 2.0, 0.0, 0.0),
    load_kw=(1.0, 0.5, 0.5, 1.0, 1.0),
    buy=(0.05, -0.02, 0.10, 0.45, 0.30),
    sell=(0.01, 0.00, 0.02, 0.10, 0.05),
    min_spread_eur_per_kwh=0.05,
)

#: Import cap pins the effective charge bound to exactly 0 (base == cap), keeping the lattice.
IMPORT_CAP = Instance(
    **BASE,
    pv_kw=(0.0, 0.0, 0.0, 0.0, 0.0),
    load_kw=(1.5, 1.5, 1.5, 1.5, 1.5),
    buy=(0.02, 0.02, 0.60, 0.60, 0.02),
    sell=(0.01, 0.01, 0.01, 0.01, 0.01),
    max_import_kw=1.5,
)

#: Export cap pins the effective discharge bound to exactly 0 (base == -cap).
EXPORT_CAP = Instance(
    **{**BASE, "soc_step_kwh": 0.25},
    pv_kw=(1.5, 1.5, 1.5, 1.5, 1.5),
    load_kw=(0.0, 0.0, 0.0, 0.0, 0.0),
    buy=(0.02, 0.02, 0.60, 0.60, 0.02),
    sell=(0.20, 0.20, 0.01, 0.01, 0.20),
    max_export_kw=1.5,
)

#: allow_grid_charge=False: surplus slots charge from PV, non-surplus slots cannot charge at all.
PV_ONLY_CHARGE = Instance(
    **BASE,
    pv_kw=(1.0, 1.0, 0.0, 0.0, 1.0),
    load_kw=(0.0, 0.0, 1.0, 1.0, 0.0),
    buy=(0.30, 0.30, 0.02, 0.02, 0.30),
    sell=(0.02, 0.02, 0.02, 0.02, 0.02),
    allow_grid_charge=False,
)

#: allow_export=False: the battery may still discharge into load, never into the grid.
NO_EXPORT = Instance(
    **{**BASE, "soc_step_kwh": 0.25},
    pv_kw=(0.5, 0.5, 0.5, 0.5, 0.5),
    load_kw=(0.5, 0.5, 0.5, 0.5, 0.5),
    buy=(0.30, 0.30, 0.30, 0.60, 0.60),
    sell=(0.50, 0.50, 0.50, 0.50, 0.50),
    allow_export=False,
)

LATTICE_INSTANCES = {
    "arbitrage": ARBITRAGE,
    "terminal_target": TARGET,
    "reserve_floor": RESERVE_FLOOR,
    "import_cap": IMPORT_CAP,
    "export_cap": EXPORT_CAP,
    "pv_only_charge": PV_ONLY_CHARGE,
    "no_export": NO_EXPORT,
    "min_spread": MIN_SPREAD,
}


# --------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LATTICE_INSTANCES))
def test_oracle_matches_dp_exactly_on_lattice_instances(name):
    inst = LATTICE_INSTANCES[name]
    # The exactness premise is proven structurally, not assumed: every reachable energy must be
    # on the DP's state grid e_min + k*soc_step.
    surface = sorted(reachable_lattice(inst))
    assert surface, name
    off_grid = [energy for energy in surface if not on_dp_grid(inst, energy)]
    assert not off_grid, f"{name}: reachable energies off the grid: {off_grid[:5]}"
    # no SoC/grid fallback anywhere reachable
    for index in range(inst.n):
        for energy in surface:
            lo, hi, fallback = feasible_bounds(inst, index, energy)
            assert not fallback, (name, index, energy, lo, hi)

    optimum, _best = oracle_optimum(inst)
    plan = solve(inst)

    assert plan.feasible is True, (name, plan.infeasible_reason)
    assert_plan_feasible(inst, plan)
    objective = plan_objective(inst, plan)
    assert objective >= optimum - 1e-6, (name, objective, optimum)  # never beats exhaustive
    assert objective == pytest.approx(optimum, abs=1e-6), (name, objective, optimum)
    # the summary's grid-cost is the sum of the frozen PlanSlot helper
    dt = inst.dt
    assert plan.summary.cost_estimate_eur == pytest.approx(
        sum(slot.cost_eur(dt) for slot in plan.slots), abs=1e-9
    )


def test_agreement_of_own_aggregate_with_shipped_aggregate():
    for inst in LATTICE_INSTANCES.values():
        mine = my_aggregate(inst)
        theirs = shipped_dp.BatteryAggregate.from_configs(
            [inst.battery_config()], inst.reserve_pct
        )
        assert mine == theirs, inst


def grid_only_cost(inst: Instance, index: int, p_kw: float) -> float:
    grid = grid_kw(inst, index, p_kw)
    return inst.dt * (inst.buy[index] * max(grid, 0.0) - inst.sell[index] * max(-grid, 0.0))


def test_plan_slot_cost_eur_matches_the_independent_cost_model():
    """The reported slot cost is the SPEC 8.1 grid cost, with no planning surcharges."""
    for inst in LATTICE_INSTANCES.values():
        plan = solve(inst)
        for index, slot in enumerate(plan.slots):
            power = slot.power_w / 1000.0
            expected = grid_only_cost(inst, index, power)
            assert slot.grid_kw() == pytest.approx(
                inst.load_kw[index] - inst.pv_kw[index] + power, abs=1e-12
            )
            assert slot.cost_eur(inst.dt) == pytest.approx(expected, abs=1e-12)
        if inst.min_spread_eur_per_kwh > 0:
            # grid charging pays the planning surcharge; the reported slot cost does not
            assert slot_cost(inst, 0, 1.0) > grid_only_cost(inst, 0, 1.0) + 1e-9
            # PV-surplus charging is exempt
            assert slot_cost(inst, 2, 0.5) == pytest.approx(grid_only_cost(inst, 2, 0.5))


def test_reserve_floor_is_never_crossed_when_it_binds():
    """A discharge-heavy instance starting just above the floor must never cross it."""
    inst = Instance(
        **{**BASE, "soc_step_kwh": 0.25, "start_kwh": 17.0},
        pv_kw=(0.0, 0.0, 0.0, 0.0, 0.0),
        load_kw=(0.5, 0.5, 0.5, 0.5, 0.5),
        buy=(0.60, 0.60, 0.60, 0.60, 0.60),
        sell=(0.50, 0.50, 0.50, 0.50, 0.50),
        salvage_multiplier=0.0,
    )
    plan = solve(inst)
    assert_plan_feasible(inst, plan, grid_caps_required=False)
    assert all(slot.soc_kwh >= inst.e_min - 1e-6 for slot in plan.slots)
    assert min(slot.soc_kwh for slot in plan.slots) == pytest.approx(inst.e_min, abs=1e-6)
    assert any(slot.power_w < 0 for slot in plan.slots), "the instance must try to discharge"


def test_dp_is_deterministic_across_three_runs():
    for name in ("arbitrage", "terminal_target", "import_cap"):
        inst = LATTICE_INSTANCES[name]
        runs = []
        for _ in range(3):
            plan = solve(inst)
            runs.append(
                [
                    (slot.t, slot.power_w, slot.soc_kwh, slot.action.value, slot.reason)
                    for slot in plan.slots
                ]
                + [plan.summary.cost_estimate_eur, plan.summary.solver_iterations]
            )
        assert runs[0] == runs[1] == runs[2], name


def random_instance(rng: random.Random) -> Instance:
    minutes = rng.choice([60, 30])
    power_w = rng.choice([500.0, 1000.0])
    capacity = rng.choice([30.0, 40.0, 60.0])
    start = capacity * rng.choice([0.4, 0.5, 0.6])
    count = rng.randint(4, 6)
    return Instance(
        capacity_kwh=capacity,
        min_soc_pct=0.0,
        max_soc_pct=100.0,
        reserve_pct=rng.choice([0.0, 10.0, 25.0]),
        max_charge_w=power_w,
        max_discharge_w=power_w,
        eta_charge=rng.choice([0.25, 0.5]),
        eta_discharge=rng.choice([0.25, 0.5]),
        slot_minutes=minutes,
        power_steps=rng.choice([3, 4, 5]),
        soc_step_kwh=rng.choice([0.25, 0.5]),
        start_kwh=start,
        pv_kw=tuple(round(rng.uniform(0.0, 2.5), 2) for _ in range(count)),
        load_kw=tuple(round(rng.uniform(0.0, 2.0), 2) for _ in range(count)),
        buy=tuple(round(rng.uniform(-0.05, 0.6), 3) for _ in range(count)),
        sell=tuple(round(rng.uniform(0.0, 0.3), 3) for _ in range(count)),
        max_import_kw=rng.choice([0.0, 1.0, 1.5, 2.0]),
        max_export_kw=rng.choice([0.0, 1.0, 1.5, 2.0]),
        allow_grid_charge=rng.choice([True, True, False]),
        allow_export=rng.choice([True, True, False]),
        cycle_cost=rng.choice([0.0, 0.02]),
        salvage_multiplier=rng.choice([0.0, 0.5, 1.0]),
    )


def test_dp_never_beats_exhaustive_search_on_random_instances():
    """Feasibility invariant: the reconstructed path is one of the oracle's enumerated paths."""
    rng = random.Random(99)
    checked = 0
    for _ in range(40):
        inst = random_instance(rng)
        plan = solve(inst)
        if plan.feasible:
            assert_plan_feasible(inst, plan, grid_caps_required=True)
        else:
            # an infeasible plan is allowed only for the documented grid/terminal downgrade,
            # and every cap violation must be physically unavoidable (max mitigating power).
            assert plan.infeasible_reason in (
                "grid_limits_infeasible",
                "terminal_target_unreachable",
            ), plan.infeasible_reason
            assert_plan_feasible(inst, plan, grid_caps_required=False)
            for index, slot in enumerate(plan.slots):
                power = slot.power_w / 1000.0
                grid = grid_kw(inst, index, power)
                if inst.max_import_kw > 0 and grid > inst.max_import_kw + 1e-6:
                    assert power == pytest.approx(inst.p_min, abs=1e-6), (index, power)
                if inst.max_export_kw > 0 and -grid > inst.max_export_kw + 1e-6:
                    assert power == pytest.approx(inst.p_max, abs=1e-6), (index, power)
        objective = plan_objective(inst, plan)
        optimum, _ = oracle_optimum(inst)
        assert objective >= optimum - 1e-6, (inst, objective, optimum)
        checked += 1
    assert checked >= 15, f"too few comparable instances: {checked}"


def test_oracle_is_not_vacuous_bruteforce_finds_nonzero_action():
    """Guard against both sides collapsing to do-nothing: the arbitrage instance must cycle."""
    plan = solve(ARBITRAGE)
    assert any(abs(slot.power_w) > 1e-6 for slot in plan.slots)
    assert plan.summary.charged_kwh > 0 and plan.summary.discharged_kwh > 0


def test_plan_summary_separates_grid_money_from_the_dp_objective():
    """WP5 finding 6 (fixed): ``PlanSummary`` reports grid money and the true objective separately.

    ``cost_estimate_eur``/``baseline_cost_eur``/``saved_eur`` stay grid-only and comparable with
    ``PlanSlot.cost_eur``, while ``objective_eur`` is the exact value the DP minimised (grid cost +
    cycle cost - terminal salvage + any target/min-spread terms) and ``cycle_cost_eur`` is its wear
    part.  The independent oracle is the reference, so this is not a tautology on dp.py internals.
    """
    instance = LATTICE_INSTANCES["arbitrage"]
    plan = solve(instance)
    summary = plan.summary
    grid_cost = sum(slot.cost_eur(instance.slot_minutes / 60.0) for slot in plan.slots)
    objective = plan_objective(instance, plan)

    assert summary.cost_estimate_eur == pytest.approx(grid_cost, abs=1e-9)
    assert summary.saved_eur == pytest.approx(summary.baseline_cost_eur - summary.cost_estimate_eur, abs=1e-9)
    # the exact minimised value, independently reconstructed
    assert summary.objective_eur == pytest.approx(objective, abs=1e-9)
    # the wear part is a real, separate number on a cycling schedule
    assert summary.cycle_cost_eur > 0.0, summary
    assert summary.cycles > 0.0, summary
    assert summary.objective_eur < summary.cost_estimate_eur, (
        "with salvage the objective is a different quantity from the grid bill"
    )
