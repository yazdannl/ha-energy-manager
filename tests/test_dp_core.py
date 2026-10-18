"""Differential and runtime-verification tests for the optional CPython core (SPEC 8.2,
docs/OPTIMIZER_V2.md section 6).

Three layers are covered:

* **Differential**: the C core must reproduce the Python reference (``dp._solve_once_numeric``)
  exactly over the edge cases of the cost model.  The autouse fixture turns the periodic full
  cross-check off so each test's comparison is explicit; the cross-check itself has its own tests.
* **Runtime verification**: tampered, malformed, oversized or numerically pathological extension
  output must be rejected and fall back to the Python reference without raising: per-solve
  wherever a check covers it, and by the periodic full differential otherwise.  The per-solve
  contract is self-consistency, not interior optimality -- see
  ``test_an_unsampled_interior_value_is_only_caught_by_the_periodic_differential``.
* **Budget**: 120 slots, <= 400 states, ``power_steps`` 24 -- the C sweep must stay under 20 ms.

All numbers here are obviously synthetic.  No instance data.
"""

from __future__ import annotations

import array
import hashlib
import logging
import math
import os
import random
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from time import perf_counter as _perf_counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.models import BatteryConfig  # noqa: E402
from app.optimize import _fast, dp  # noqa: E402

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_core():
    _fast.reset()
    interval = _fast._CHECK_INTERVAL
    _fast.set_check_interval(0)  # differential tests compare explicitly; cross-check has its own
    yield
    _fast.set_check_interval(interval)
    _fast.reset()


def _battery(**overrides) -> BatteryConfig:
    base = dict(
        id="b_core",
        name="b_core",
        driver="generic",
        capacity_kwh=8.0,
        min_soc_pct=5.0,
        max_soc_pct=100.0,
        max_charge_w=4000.0,
        max_discharge_w=4000.0,
        eta_charge=0.94,
        eta_discharge=0.92,
        mapping={},
    )
    base.update(overrides)
    return BatteryConfig(**base)


@dataclass
class Scenario:
    slots: list[dp.SlotInput]
    agg: dp.BatteryAggregate
    opts: dp.OptimizerOptions
    start_kwh: float
    e_target: float | None
    target_penalty: float
    salvage: float
    minutes: list[float]


def _scenario(
    *,
    pv: list[float],
    load: list[float],
    buy: list[float],
    sell: list[float | None],
    slot_minutes: int = 60,
    minutes: list[float] | None = None,
    start_kwh: float = 3.0,
    capacity_kwh: float = 8.0,
    min_soc_pct: float = 5.0,
    max_charge_w: float = 4000.0,
    max_discharge_w: float = 4000.0,
    e_target: float | None = None,
    target_penalty: float = dp.TARGET_HARD_PENALTY,
    salvage_multiplier: float = 1.0,
    **opts_kw,
) -> Scenario:
    n = len(pv)
    minutes = list(minutes) if minutes is not None else [float(slot_minutes)] * n
    slots = [
        dp.SlotInput(
            t=T0 + timedelta(minutes=slot_minutes * i),
            pv_kw=pv[i],
            load_kw=load[i],
            buy=buy[i],
            sell=sell[i],
        )
        for i in range(n)
    ]
    config = _battery(
        capacity_kwh=capacity_kwh,
        min_soc_pct=min_soc_pct,
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=slot_minutes,
        soc_step_kwh=opts_kw.pop("soc_step_kwh", 0.05),
        power_steps=opts_kw.pop("power_steps", 8),
        **opts_kw,
    )
    salvage = (
        salvage_multiplier
        * dp._median([float(value) for value in buy])
        * agg.eta_discharge
    )
    return Scenario(slots, agg, opts, start_kwh, e_target, target_penalty, salvage, minutes)


def _python_ref(sc: Scenario, terminal_row: list[float] | None = None):
    return dp._solve_once_numeric(
        sc.slots,
        sc.agg,
        sc.start_kwh,
        sc.opts,
        sc.target_penalty,
        sc.e_target,
        sc.salvage,
        sc.minutes,
        terminal_row=terminal_row,
    )


def _c_result(sc: Scenario, terminal_row: list[float] | None = None):
    _fast.reset()
    result = _fast.solve(
        sc.slots,
        sc.agg,
        sc.start_kwh,
        sc.opts,
        sc.target_penalty,
        sc.e_target,
        sc.salvage,
        sc.minutes,
        terminal_row=terminal_row,
    )
    assert result is not None, "the C core must accept a valid scenario"
    return result


def _flatten(windows) -> list[float]:
    return [value for window in windows for value in window]


def _assert_same(sc: Scenario, terminal_row: list[float] | None = None) -> None:
    reference = _python_ref(sc, terminal_row)
    fast = _c_result(sc, terminal_row)
    assert fast.iterations == reference.iterations
    assert fast.states == reference.states
    assert fast.effective_step_kwh == pytest.approx(reference.effective_step_kwh, abs=1e-15)
    assert fast.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
    assert fast.internal_objective_eur == pytest.approx(reference.internal_objective_eur, abs=1e-9)
    assert fast.cycle_cost_eur == pytest.approx(reference.cycle_cost_eur, abs=1e-9)
    assert fast.terminal_error_kwh == pytest.approx(reference.terminal_error_kwh, abs=1e-12)
    assert fast.powers == pytest.approx(reference.powers, abs=1e-12)
    assert fast.energies == pytest.approx(reference.energies, abs=1e-12)
    assert _flatten(fast.windows) == pytest.approx(_flatten(reference.windows), abs=1e-12)


def _row_grid(sc: Scenario) -> list[float]:
    """The grid the core will rebuild for this scenario (the row must be aligned to it)."""
    grid, _step = _fast._reference_grid(dp, sc.slots, sc.agg, sc.opts, sc.start_kwh, sc.e_target)
    return grid


def _tail_row(sc: Scenario, length: int = 4, penalty: float | None = None) -> list[float]:
    """A genuine stage-3 continuation row: a short tail swept on the *same* grid.

    Built with the reference ``_backward_sweep`` from the salvage row, exactly what
    ``terminal_value.continuation_row`` does for a plan, and then given the target penalty
    (``_row_with_penalty``) when the scenario has a target -- the row the core must consume
    already contains that penalty, so it must never be added again anywhere.
    """
    grid = _row_grid(sc)
    tail = [
        dp.SlotInput(
            t=sc.slots[-1].t + timedelta(minutes=sc.opts.slot_minutes * (i + 1)),
            pv_kw=0.5,
            load_kw=0.6,
            buy=0.22,
            sell=0.06,
        )
        for i in range(length)
    ]
    minutes = [float(sc.opts.slot_minutes)] * length
    salvage_row = dp._salvage_terminal_row(grid, sc.salvage, 0.0, None)
    value, _argmin, _iterations = dp._backward_sweep(
        grid, tail, minutes, sc.agg, sc.opts, salvage_row
    )
    row = list(value[0])
    if sc.e_target is not None:
        row = dp._row_with_penalty(
            row, grid, sc.e_target, sc.target_penalty if penalty is None else penalty
        )
    return row


def _shaped_row(sc: Scenario) -> list[float]:
    """A row whose minimum is in the middle of the SoC range (not a salvage-like straight line)."""
    grid = _row_grid(sc)
    middle = len(grid) // 2
    return [0.4 * abs(index - middle) + 0.05 * (index % 3) for index in range(len(grid))]


def _path_objective(powers: list[float], sc: Scenario) -> tuple[float, float, float]:
    """Objective, cycle cost and terminal error of an arbitrary power sequence (reference model)."""
    e = dp.clamp(sc.start_kwh, sc.agg.e_min_kwh, sc.agg.e_max_kwh)
    objective = 0.0
    cycle = 0.0
    for i, power in enumerate(powers):
        dt = sc.minutes[i] / 60.0
        objective += dp.slot_cost_eur(power, sc.slots[i], dt, sc.opts)
        e_next = dp.next_energy_kwh(e, power, dt, sc.agg)
        cycle += dp.wear_cost_eur(sc.opts.wear_eur_per_kwh, sc.agg.eta_discharge, e, e_next)
        e = dp.clamp(e_next, sc.agg.e_min_kwh, sc.agg.e_max_kwh)
    objective += cycle - sc.salvage * e
    terminal = 0.0
    if sc.e_target is not None:
        objective += sc.target_penalty * abs(e - sc.e_target)
        terminal = abs(e - sc.e_target)
    return objective, cycle, terminal


requires_core = pytest.mark.skipif(
    _fast._core is None,
    reason=f"no usable C core in this checkout ({_fast._import_error or 'not compiled'})",
)


# --------------------------------------------------------------------------------------------
# Differential coverage (SPEC 8.1/8.2 edge cases)
# --------------------------------------------------------------------------------------------


@requires_core
def test_grid_import_and_export_caps():
    _assert_same(
        _scenario(
            pv=[0.0, 3.0, 0.0, 4.0],
            load=[1.2, 0.4, 1.5, 0.2],
            buy=[0.30, 0.10, 0.35, 0.05],
            sell=[0.10, 0.10, 0.10, 0.10],
            max_import_kw=0.5,
            max_export_kw=0.4,
        )
    )


@requires_core
def test_allow_export_false():
    _assert_same(
        _scenario(
            pv=[0.0, 5.0, 0.0, 3.0],
            load=[0.3, 0.2, 0.6, 0.1],
            buy=[0.40, 0.05, 0.42, 0.08],
            sell=[0.30, 0.30, 0.30, 0.30],
            allow_export=False,
        )
    )


@requires_core
def test_cost_basis_sell_floor():
    _assert_same(
        _scenario(
            pv=[0.0, 0.0, 4.0, 0.0],
            load=[0.5, 0.5, 0.4, 0.5],
            buy=[0.50, 0.20, 0.10, 0.45],
            sell=[0.05, 0.05, 0.05, 0.05],
            sell_floor_eur_per_kwh=0.30,
        )
    )


@requires_core
def test_refuse_battery_export_bounds_battery_export_to_zero():
    """Optimizer v2 stage 1b: the contract refuses only the battery-sourced export leg (D2)."""
    scenario = _scenario(
        pv=[0.0, 3.0, 0.0, 4.0],
        load=[1.2, 0.4, 1.5, 0.2],
        buy=[0.45, 0.08, 0.42, 0.10],
        sell=[0.35, 0.35, 0.35, 0.35],
        refuse_battery_export=True,
    )
    _assert_same(scenario)
    reference = _python_ref(scenario)
    for i, slot in enumerate(scenario.slots):
        base = slot.load_kw - slot.pv_kw
        if base > 0.0:
            assert reference.powers[i] >= -base - 1e-9, "a deficit slot may not export"
        else:
            assert reference.powers[i] >= -1e-9, "a surplus slot refuses battery discharge"


@requires_core
def test_refuse_battery_export_keeps_forced_pv_export_at_a_negative_floor():
    """A negative justified floor must still price the forced PV export (never suppress it)."""
    scenario = _scenario(
        pv=[4.0, 4.0],
        load=[0.3, 0.3],
        buy=[0.05, 0.05],
        sell=[-0.20, -0.20],
        start_kwh=7.6,
        refuse_battery_export=True,
    )
    _assert_same(scenario)
    reference = _python_ref(scenario)
    assert all(power >= -1e-9 for power in reference.powers), reference.powers
    assert reference.powers[0] + scenario.slots[0].load_kw - scenario.slots[0].pv_kw < 0.0


@requires_core
def test_refuse_battery_export_combines_with_the_cost_basis_sell_floor():
    _assert_same(
        _scenario(
            pv=[0.0, 0.0, 4.0, 0.0],
            load=[0.5, 0.5, 0.4, 0.5],
            buy=[0.50, 0.20, 0.10, 0.45],
            sell=[0.05, 0.05, 0.05, 0.05],
            sell_floor_eur_per_kwh=0.30,
            refuse_battery_export=True,
        )
    )


@requires_core
def test_a_core_ignoring_the_refusal_option_is_rejected(monkeypatch):
    """An old core (no refusal branch) is rejected on the solve itself: the validator rebuilds the bound (D1)."""
    scenario = _scenario(
        pv=[0.0, 3.0, 0.0, 4.0],
        load=[1.2, 0.4, 1.5, 0.2],
        buy=[0.45, 0.08, 0.42, 0.10],
        sell=[0.35, 0.35, 0.35, 0.35],
        refuse_battery_export=True,
    )
    real_solve = _fast._core.solve

    def ignoring(pv, load, buy, sell, minutes, config):
        config = {key: value for key, value in config.items() if key != "refuse_battery_export"}
        return real_solve(pv, load, buy, sell, minutes, config)

    monkeypatch.setattr(_fast._core, "solve", ignoring)
    assert (
        _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
        )
        is None
    )
    assert not _fast.available(), "a core that ignores the refusal bound must be disabled"
    plan = dp.solve(
        scenario.slots,
        aggregate=scenario.agg,
        start_kwh=scenario.start_kwh,
        options=scenario.opts,
        generated_at=T0,
    )
    reference = _python_ref(scenario)
    assert [slot.power_w for slot in plan.slots] == pytest.approx(
        [power * 1000.0 for power in reference.powers], abs=1e-9
    )


@requires_core
def test_negative_and_unknown_prices():
    _assert_same(
        _scenario(
            pv=[0.0, 2.0, 0.0, 1.0],
            load=[0.6, 0.5, 0.9, 0.4],
            buy=[-0.05, 0.10, -0.02, 0.20],
            sell=[-0.10, -0.05, 0.15, None],
        )
    )


@requires_core
def test_sell_above_buy():
    _assert_same(
        _scenario(
            pv=[0.0, 0.0, 0.0, 0.0],
            load=[0.4, 0.4, 0.4, 0.4],
            buy=[0.10, 0.10, 0.10, 0.10],
            sell=[0.40, 0.40, 0.40, 0.40],
            sell_floor_eur_per_kwh=0.20,
        )
    )


@requires_core
def test_partial_first_slot():
    _assert_same(
        _scenario(
            pv=[0.0, 4.0, 0.0, 0.0],
            load=[0.7, 0.3, 1.1, 0.5],
            buy=[0.45, 0.08, 0.40, 0.12],
            sell=[0.20, 0.20, 0.20, 0.20],
            slot_minutes=15,
            minutes=[4.0, 15.0, 15.0, 15.0],
        )
    )


@requires_core
def test_terminal_target_hard_and_soft():
    hard = _scenario(
        pv=[0.0] * 6,
        load=[0.3] * 6,
        buy=[0.50, 0.10, 0.45, 0.08, 0.40, 0.09],
        sell=[0.05] * 6,
        capacity_kwh=10.0,
        start_kwh=2.0,
        e_target=8.0,
        target_penalty=dp.TARGET_HARD_PENALTY,
    )
    _assert_same(hard)
    soft = _scenario(
        pv=[0.0] * 3,
        load=[0.3] * 3,
        buy=[0.50, 0.10, 0.45],
        sell=[0.05] * 3,
        capacity_kwh=20.0,
        max_charge_w=1000.0,
        start_kwh=0.0,
        e_target=19.0,
        target_penalty=dp.TARGET_SOFT_PENALTY,
    )
    _assert_same(soft)


@requires_core
def test_coarsened_grid(monkeypatch):
    # A tiny iteration budget forces the ``_solve_once`` complexity guard to widen the step, on top
    # of the 400-state ceiling ``_build_grid`` already enforces for this large pack.
    monkeypatch.setattr(dp, "MAX_ITERATIONS", 60_000)
    scenario = _scenario(
        pv=[0.0, 6.0, 0.0, 6.0, 0.0, 6.0],
        load=[0.5] * 6,
        buy=[0.45, 0.05, 0.40, 0.06, 0.50, 0.04],
        sell=[0.10] * 6,
        capacity_kwh=40.0,
        soc_step_kwh=0.005,
        power_steps=30,
    )
    reference = _python_ref(scenario)
    fast = _c_result(scenario)
    assert fast.states < 400, "the small iteration budget must have coarsened the grid"
    assert fast.effective_step_kwh > scenario.opts.soc_step_kwh
    assert fast.iterations == reference.iterations
    assert fast.powers == pytest.approx(reference.powers, abs=1e-12)
    assert fast.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)


@requires_core
def test_terminal_row_interpolation_at_a_coarsened_grid_anchor():
    """Anchor/interpolation regression: the row must be read at the exact grid entry the sweep
    evaluates, on a coarsened grid whose inverse-step index hint is inexact.

    The row is built as two plateaus (equal adjacent values) with the jump pinned to the grid entry
    slot 0's 0 kW action lands on, so the sweep's evaluation energy *is* a grid coordinate.  The
    mirrored variants prove that entry is load-bearing for the plan, so an interpolation that reads
    the wrong *region* of the row is visible in the differential rather than merely
    self-consistent.

    What this does **not** pin: the ``<=``/``<`` comparison of the upward bracket walk.  At a
    unique knot both neighbouring brackets return the knot's value (the interpolation is
    continuous), so switching that comparison would not change this plan.  ``span <= 0`` is
    likewise untouched here, and unreachable from any builder output: see
    ``test_the_state_grid_builder_cannot_produce_duplicate_coordinates``, whose strict-monotonicity
    assertion is the actual evidence for that branch being dead.
    """
    scenario = _scenario(
        pv=[0.0, 0.0, 5.0, 0.0],
        load=[0.0, 0.4, 0.4, 0.6],
        buy=[0.50, 0.06, 0.05, 0.42],
        sell=[0.02] * 4,
        capacity_kwh=40.0,
        soc_step_kwh=0.005,
        power_steps=8,
        start_kwh=20.0,
    )
    grid = _row_grid(scenario)
    assert len(grid) < 400, "the 400-state ceiling must have coarsened the grid"
    assert grid[1] - grid[0] > scenario.opts.soc_step_kwh, "the lattice really was coarsened"
    start = min(range(len(grid)), key=lambda index: abs(grid[index] - scenario.start_kwh))
    assert grid[start] == scenario.start_kwh, "the start anchor is an exact grid entry"
    lo, hi = _fast._reference_bounds(
        dp, scenario.slots[0], scenario.agg, scenario.opts, scenario.start_kwh, 1.0
    )
    assert lo <= 0.0 <= hi, "slot 0 must offer the 0 kW action that lands exactly on the anchor"
    above = [0.0] * start + [10.0] * (len(grid) - start)
    below = [10.0] * start + [0.0] * (len(grid) - start)
    _assert_same(scenario, [0.0] * len(grid))
    _assert_same(scenario, above)
    _assert_same(scenario, below)
    assert above[start] != above[start - 1] and below[start] != below[start - 1]
    assert _python_ref(scenario, above).powers != _python_ref(scenario, below).powers, (
        "the plateau edge at the evaluated entry must move the plan"
    )


def test_the_state_grid_builder_cannot_produce_duplicate_coordinates():
    """``span <= 0`` in the interpolation is a guarded-but-dead branch: the builder cannot emit a
    duplicate grid coordinate.

    ``_build_grid`` lays a lattice ``e_min + i * step`` with ``step >= 1e-4`` and at most
    ``MAX_STATES - 3`` intervals, which is strictly increasing as doubles; ``e_max`` is appended
    only when the last point is more than ``EPS_KWH`` below it; and an anchor is appended only when
    it is more than 1e-6 from every point already present (``_core.c::build_grid`` repeats that
    guard, and the returned grid must match this builder entry for entry anyway).  A flat piecewise
    interpolation over a strictly increasing grid cannot see a zero-width bracket, so the fallback
    only becomes live if a future builder change lets coordinates collide -- which this pins.
    """
    agg = dp.BatteryAggregate.from_configs([_battery()], 0.0)
    step = 0.05
    knot = agg.e_min_kwh + 8 * step
    dropped = knot + 5e-7  # inside the 1e-6 duplicate guard
    kept = knot + 1.1e-6  # just outside it
    anchors = [
        agg.e_min_kwh,
        agg.e_max_kwh,
        knot,
        dropped,
        kept,
        knot - 1.1e-6,
        agg.e_max_kwh - 5e-10,  # just below the ``e_max`` append threshold
        agg.e_max_kwh + 1.0,  # clamps onto the last point
        agg.e_min_kwh - 1.0,  # clamps onto the first point
    ]
    for requested_step in (step, 1e-4):  # the plain lattice and the 400-state coarsening path
        grid, _effective = dp._build_grid(agg, requested_step, anchors)
        assert all(low < high for low, high in zip(grid, grid[1:])), "duplicate grid coordinate"
        assert kept in grid, "an anchor just outside the guard must be kept"
        assert dropped not in grid, "an anchor inside the guard must be dropped"
    degenerate = dp.BatteryAggregate.from_configs([_battery(min_soc_pct=100.0)], 0.0)
    assert degenerate.e_min_kwh == degenerate.e_max_kwh
    grid, _effective = dp._build_grid(degenerate, step, anchors)
    assert grid == [degenerate.e_max_kwh], "a zero-span battery keeps its single state"


@requires_core
def test_ties_resolve_to_the_lowest_power():
    # Zero prices and zero wear make every action cost the same; both cores must keep the lowest
    # power (the first action in the ascending action list).
    scenario = _scenario(
        pv=[0.0, 0.0, 0.0, 0.0],
        load=[0.0, 0.0, 0.0, 0.0],
        buy=[0.0, 0.0, 0.0, 0.0],
        sell=[0.0, 0.0, 0.0, 0.0],
        wear_eur_per_kwh=0.0,
        salvage_multiplier=0.0,
    )
    reference = _python_ref(scenario)
    fast = _c_result(scenario)
    assert fast.powers == reference.powers
    assert fast.energies == reference.energies
    assert fast.iterations == reference.iterations
    # A degenerate slot whose SoC/grid windows do not intersect collapses to one power
    # (``grid_limits_infeasible``); both cores must still agree exactly.
    degenerate = _scenario(
        pv=[0.0],
        load=[1.0],
        buy=[0.2],
        sell=[0.1],
        start_kwh=3.0,
        allow_grid_charge=False,
        allow_export=False,
    )
    _assert_same(degenerate)


# --------------------------------------------------------------------------------------------
# Optimizer v2 stage 3: the continuation-value row over the same grid
# --------------------------------------------------------------------------------------------


@requires_core
def test_terminal_row_matches_the_reference():
    """The row IS the terminal condition: value[n] = row, then the ordinary interp on slot n-1."""
    scenario = _scenario(
        pv=[0.0, 4.0, 0.0, 0.0],
        load=[0.6, 0.3, 1.0, 0.5],
        buy=[0.45, 0.08, 0.42, 0.10],
        sell=[0.20, 0.20, 0.20, 0.20],
    )
    _assert_same(scenario)
    _assert_same(scenario, _tail_row(scenario))
    _assert_same(scenario, _shaped_row(scenario))
    # The row must actually reach the sweep: a row whose minimum is mid-range is not a salvage
    # line, so a core that ignored it would keep the salvage schedule.
    assert _c_result(scenario, _shaped_row(scenario)).powers != _c_result(scenario).powers


@requires_core
def test_terminal_row_carries_the_penalty_and_is_not_penalised_twice():
    """``_row_with_penalty`` bakes the target penalty in; the core must not add it again."""
    # One slot, so the target is unreachable: the penalty term is non-zero, and a second penalty
    # added on top would move both the objective and the schedule.
    unreachable = _scenario(
        pv=[0.0],
        load=[0.5],
        buy=[0.20],
        sell=[0.05],
        start_kwh=3.0,
        e_target=7.5,
    )
    for penalty in (dp.TARGET_HARD_PENALTY, dp.TARGET_SOFT_PENALTY):
        row = _tail_row(unreachable, penalty=penalty)
        reference = _python_ref(unreachable, row)
        assert reference.terminal_error_kwh > 0.0, "the penalty term must be non-zero here"
        _assert_same(unreachable, row)
        # And a row without that penalty is a different scenario (the core reads the row as given).
        assert _python_ref(unreachable, _tail_row(unreachable, penalty=0.0)).objective_eur != (
            reference.objective_eur
        )


@requires_core
@pytest.mark.parametrize("e_target", [99.0, -5.0])
def test_terminal_row_with_an_out_of_range_target(e_target):
    """The grid anchor clamps the target; the row and the penalty keep the raw value."""
    scenario = _scenario(
        pv=[0.0, 0.0],
        load=[0.5, 0.5],
        buy=[0.30, 0.10],
        sell=[0.05, 0.05],
        start_kwh=3.0,
        e_target=e_target,
    )
    row = _tail_row(scenario)
    _assert_same(scenario, row)
    assert _python_ref(scenario, row).terminal_error_kwh > 0.0


@requires_core
def test_randomized_terminal_row_differential():
    rng = random.Random(20261018)
    for _ in range(25):
        n = rng.randint(3, 10)
        slot_minutes = rng.choice([15, 30, 60])
        buy = [round(rng.uniform(-0.05, 0.60), 4) for _ in range(n)]
        sell = [
            None if rng.random() < 0.1 else round(value + rng.uniform(-0.25, 0.25), 4)
            for value in buy
        ]
        pv = [round(rng.uniform(-0.5, 6.0), 3) for _ in range(n)]
        load = [round(rng.uniform(0.0, 2.0), 3) for _ in range(n)]
        minutes = [float(slot_minutes)] * n
        if rng.random() < 0.3:
            minutes[0] = float(rng.randint(1, slot_minutes))
        e_target = None
        if rng.random() < 0.4:
            # In range and outside it: an out-of-range target only clamps the grid anchor.
            e_target = round(rng.uniform(-2.0, 12.0), 3)
        scenario = _scenario(
            pv=pv,
            load=load,
            buy=buy,
            sell=sell,
            slot_minutes=slot_minutes,
            minutes=minutes,
            start_kwh=round(rng.uniform(0.5, 9.0), 3),
            capacity_kwh=10.0,
            e_target=e_target,
            target_penalty=rng.choice([dp.TARGET_HARD_PENALTY, dp.TARGET_SOFT_PENALTY]),
            soc_step_kwh=rng.choice([0.02, 0.05, 0.2, 0.5]),
            power_steps=rng.choice([1, 2, 5, 24]),
            wear_eur_per_kwh=rng.choice([0.0, 0.02, 0.1]),
            allow_grid_charge=rng.random() < 0.8,
            allow_export=rng.random() < 0.7,
            max_import_kw=rng.choice([0.0, 0.5, 1.5]),
            max_export_kw=rng.choice([0.0, 0.4]),
            min_spread_eur_per_kwh=rng.choice([0.0, 0.01]),
            pv_charge_preference=rng.choice(["auto", "prefer_pv", "avoid_pv"]),
            sell_floor_eur_per_kwh=rng.choice([None, 0.1, 0.35]),
        )
        row = _shaped_row(scenario) if rng.random() < 0.5 else _tail_row(
            scenario, rng.choice([2, 4])
        )
        _assert_same(scenario, row)


@requires_core
def test_randomized_differential():
    rng = random.Random(20261015)
    for _ in range(40):
        n = rng.randint(3, 14)
        slot_minutes = rng.choice([15, 30, 60])
        buy = [round(rng.uniform(-0.05, 0.60), 4) for _ in range(n)]
        sell = [None if rng.random() < 0.1 else round(value + rng.uniform(-0.25, 0.25), 4) for value in buy]
        pv = [round(rng.uniform(-0.5, 6.0), 3) for _ in range(n)]
        load = [round(rng.uniform(0.0, 2.0), 3) for _ in range(n)]
        minutes = [float(slot_minutes)] * n
        if rng.random() < 0.3:
            minutes[0] = float(rng.randint(1, slot_minutes))
        e_target = None
        if rng.random() < 0.3:
            e_target = round(rng.uniform(1.0, 9.0), 3)
        scenario = _scenario(
            pv=pv,
            load=load,
            buy=buy,
            sell=sell,
            slot_minutes=slot_minutes,
            minutes=minutes,
            start_kwh=round(rng.uniform(0.5, 9.0), 3),
            capacity_kwh=10.0,
            e_target=e_target,
            target_penalty=rng.choice([dp.TARGET_HARD_PENALTY, dp.TARGET_SOFT_PENALTY]),
            soc_step_kwh=rng.choice([0.02, 0.05, 0.2, 0.5]),
            power_steps=rng.choice([1, 2, 5, 24]),
            wear_eur_per_kwh=rng.choice([0.0, 0.02, 0.1]),
            allow_grid_charge=rng.random() < 0.8,
            allow_export=rng.random() < 0.7,
            max_import_kw=rng.choice([0.0, 0.5, 1.5]),
            max_export_kw=rng.choice([0.0, 0.4]),
            min_spread_eur_per_kwh=rng.choice([0.0, 0.01]),
            pv_charge_preference=rng.choice(["auto", "prefer_pv", "avoid_pv"]),
            sell_floor_eur_per_kwh=rng.choice([None, 0.1, 0.35]),
        )
        _assert_same(scenario)


@requires_core
def test_the_core_ignoring_the_terminal_row_is_rejected(monkeypatch):
    """A core that computes the salvage plan but returns the row is rejected on the solve itself:
    the reported objective must interpolate the row, not the salvage term."""
    scenario = _fallback_scenario()
    row = _shaped_row(scenario)
    real_solve = _fast._core.solve
    # The row is dropped at the call: the returned value[n] is the salvage row, not the one the
    # caller supplied, which validation compares entry for entry.
    monkeypatch.setattr(_fast._core, "solve", lambda *args, **kwargs: real_solve(*args[:6]))
    _fast.reset()
    assert (
        _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
            terminal_row=row,
        )
        is None
    )
    assert not _fast.available(), "an ignored terminal row must disable the core for the process"
    # The wired path still returns exactly the reference plan for the same row.
    reference = _python_ref(scenario, row)
    fallback = dp._solve_once(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        minutes_of=scenario.minutes,
        terminal_row=row,
    )
    assert [slot.power_w for slot in fallback.slots] == pytest.approx(
        [power * 1000.0 for power in reference.powers], abs=1e-9
    )


@requires_core
def test_a_modified_terminal_row_is_rejected(monkeypatch):
    """The returned terminal row is verified entry for entry, not by sample."""
    scenario = _fallback_scenario()
    row = _shaped_row(scenario)
    real_solve = _fast._core.solve

    def modified(result):
        out = dict(result)
        states = int(out["states"])
        buffer = array.array("d")
        buffer.frombytes(out["value"])
        offset = len(buffer) - states
        for k in range(states):
            buffer[offset + k] = -0.123 * k  # a row the caller never supplied
        out["value"] = buffer.tobytes()
        return out

    monkeypatch.setattr(
        _fast._core, "solve", lambda *args, **kwargs: modified(real_solve(*args, **kwargs))
    )
    _fast.reset()
    assert (
        _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
            terminal_row=row,
        )
        is None
    )
    assert not _fast.available()


@requires_core
def test_a_one_ulp_terminal_row_change_is_rejected(monkeypatch, caplog):
    """The core copies the row into ``value[n]`` verbatim, so the row check is exact equality.

    A single ULP is far below ``_VALUE_ABS_TOL``; a tolerance comparison would accept it, and the
    substituted entry is exactly the kind of silent row change the stage-3 contract exists to
    catch.  The failure must come from the row check itself, before any Bellman comparison.
    """
    scenario = _fallback_scenario()
    row = _tail_row(scenario)

    def one_ulp(result):
        result = dict(result)
        values = array.array("d")
        values.frombytes(bytes(result["value"]))
        offset = len(scenario.slots) * int(result["states"]) + len(row) // 2
        values[offset] = math.nextafter(values[offset], math.inf)
        return {**result, "value": values.tobytes()}

    _patch_core_output(monkeypatch, one_ulp)
    with caplog.at_level(logging.WARNING, logger="app.optimize._fast"):
        assert (
            _fast.solve(
                scenario.slots,
                scenario.agg,
                scenario.start_kwh,
                scenario.opts,
                scenario.target_penalty,
                scenario.e_target,
                scenario.salvage,
                scenario.minutes,
                terminal_row=row,
            )
            is None
        ), "a one-ULP terminal row change must not pass as the supplied row"
    assert "terminal value row does not match the supplied row" in _disable_reason(caplog)
    assert not _fast.available()


@requires_core
def test_a_salvage_objective_with_a_terminal_row_is_rejected(monkeypatch):
    """With a row the objective's terminal term is the interpolated row, not the salvage term."""
    scenario = _fallback_scenario()
    row = _shaped_row(scenario)
    salvage_objective = _python_ref(scenario).objective_eur
    row_objective = _python_ref(scenario, row).objective_eur
    assert abs(salvage_objective - row_objective) > 1e-6, "the row must move the objective here"

    real_solve = _fast._core.solve

    def rowless_objective(*args, **kwargs):
        out = dict(real_solve(*args, **kwargs))
        out["objective_eur"] = real_solve(*args[:6])["objective_eur"]  # the salvage objective
        return out

    monkeypatch.setattr(_fast._core, "solve", rowless_objective)
    _fast.reset()
    assert (
        _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
            terminal_row=row,
        )
        is None
    )
    assert not _fast.available()


@requires_core
def test_an_unusable_terminal_row_is_declined_without_disabling_the_core():
    """A row the core cannot interpret is a *decline*: the reference solves it, then the core is
    still available.  Disabling the accelerator for the rest of the process because one caller
    passed a bad row would be a plan-level regression for every later solve."""
    scenario = _fallback_scenario()
    row = _shaped_row(scenario)
    non_finite = list(row)
    non_finite[0] = float("nan")
    for bad in (non_finite, row[:-1], row + [0.0], "not a row", object()):
        assert (
            _fast.solve(
                scenario.slots,
                scenario.agg,
                scenario.start_kwh,
                scenario.opts,
                scenario.target_penalty,
                scenario.e_target,
                scenario.salvage,
                scenario.minutes,
                terminal_row=bad,
            )
            is None
        )
        assert _fast.available(), "a caller-supplied row must never disable the accelerator"
    # The accelerator is not merely enabled: it still serves a valid row afterwards.
    assert _c_result(scenario, row).powers == _python_ref(scenario, row).powers


@requires_core
def test_a_terminal_row_that_overflows_the_conversion_is_declined(monkeypatch):
    """An int too large for ``float()`` raises ``OverflowError``, not ``ValueError``: that is the
    same *decline* as any other unusable row, never a core fault.

    ``_prepare_terminal_row`` runs before the extension, so the conversion guard must treat
    ``OverflowError`` as part of "not a sequence of floats": the extension is not called, the
    accelerator stays available, and the Python reference owns the scenario -- including the
    reference's own ``OverflowError`` from ``float(entry)``, which is the pre-acceleration
    behaviour unchanged.
    """
    scenario = _fallback_scenario()
    row = _tail_row(scenario)
    huge = 10**400  # float(huge) overflows a double
    core_calls = _count_core_calls(monkeypatch)
    for bad in (huge, [huge] * len(row), [row[0], huge] + row[2:]):
        _fast.reset()
        assert (
            _fast.solve(
                scenario.slots,
                scenario.agg,
                scenario.start_kwh,
                scenario.opts,
                scenario.target_penalty,
                scenario.e_target,
                scenario.salvage,
                scenario.minutes,
                terminal_row=bad,
            )
            is None
        )
        assert _fast.available(), "an unconvertible row must never disable the accelerator"
    assert core_calls["n"] == 0, "the row guard must decline before the extension is called"
    reference_calls = _count_reference_calls(monkeypatch)
    with pytest.raises(OverflowError):
        _python_ref(scenario, [row[0], huge] + row[2:])
    assert reference_calls["n"] == 1, "the reference is what owns an unconvertible row"


@requires_core
def test_the_cross_check_oracle_is_given_the_terminal_row(monkeypatch):
    """The periodic differential must compare against the *row* model, not the salvage one.

    Without forwarding, the oracle would solve the salvage scenario and every comparison would
    report a mismatch, disabling the core on the first row solve."""
    scenario = _fallback_scenario()
    row = _shaped_row(scenario)
    seen: list[Any] = []
    real_reference = dp._solve_once_numeric

    def recording(*args, **kwargs):
        seen.append(kwargs.get("terminal_row"))
        return real_reference(*args, **kwargs)

    monkeypatch.setattr(dp, "_solve_once_numeric", recording)
    _fast.reset()
    _fast.set_check_interval(1)  # cross-check on the very first solve
    result = _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
        terminal_row=row,
    )
    assert seen == [row], "the oracle must receive the supplied row verbatim"
    assert result is not None
    assert _fast.available(), "a correct row solve must survive the cross-check"


@requires_core
def test_wired_solve_once_prefers_the_validated_core(monkeypatch):
    """``dp._solve_once`` must return exactly what the C core produced (no plan-shape change)."""
    calls = {"n": 0}
    real_solve = _fast._core.solve

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(_fast._core, "solve", counting)
    scenario = _scenario(
        pv=[0.0, 4.0, 0.0, 0.0],
        load=[0.6, 0.3, 1.0, 0.5],
        buy=[0.45, 0.08, 0.42, 0.10],
        sell=[0.20, 0.20, 0.20, 0.20],
    )
    reference = _python_ref(scenario)
    wired = dp._solve_once(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        minutes_of=scenario.minutes,
    )
    assert calls["n"] == 1, "the wired path must have used the C core"
    assert [slot.power_w for slot in wired.slots] == pytest.approx(
        [power * 1000.0 for power in reference.powers], abs=1e-9
    )
    assert [slot.soc_kwh for slot in wired.slots] == pytest.approx(reference.energies, abs=1e-12)
    assert wired.iterations == reference.iterations
    assert wired.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
    assert wired.effective_step_kwh == pytest.approx(reference.effective_step_kwh, abs=1e-15)


@requires_core
def test_maintenance_target_plan_unchanged_by_the_core():
    scenario = _scenario(
        pv=[0.0] * 8,
        load=[0.4] * 8,
        buy=[0.50, 0.10, 0.45, 0.09, 0.40, 0.08, 0.42, 0.07],
        sell=[0.05] * 8,
        capacity_kwh=10.0,
        start_kwh=2.0,
    )
    maintenance = {"active": True, "target_soc_pct": 90.0}
    kwargs = dict(
        aggregate=scenario.agg,
        start_kwh=scenario.start_kwh,
        options=scenario.opts,
        generated_at=T0,
        maintenance=maintenance,
    )
    _fast.reset()
    with_core = dp.solve(scenario.slots, **kwargs)
    assert _fast.available()
    _fast.disable("test: compare against the reference")
    without_core = dp.solve(scenario.slots, **kwargs)
    _fast.reset()
    assert [
        (slot.power_w, slot.soc_kwh, slot.action, slot.reason) for slot in with_core.slots
    ] == [(slot.power_w, slot.soc_kwh, slot.action, slot.reason) for slot in without_core.slots]
    assert with_core.summary.objective_eur == pytest.approx(without_core.summary.objective_eur, abs=1e-9)
    assert with_core.summary.solver_iterations == without_core.summary.solver_iterations


# --------------------------------------------------------------------------------------------
# Runtime verification: tampered output must be rejected and fall back to the reference
# --------------------------------------------------------------------------------------------


def _patch_core_output(monkeypatch, corrupt):
    real_solve = _fast._core.solve
    monkeypatch.setattr(_fast._core, "solve", lambda *args, **kwargs: corrupt(real_solve(*args, **kwargs)))


def _fallback_scenario() -> Scenario:
    return _scenario(
        pv=[0.0, 3.0, 0.0, 0.0],
        load=[0.5, 0.3, 0.9, 0.4],
        buy=[0.40, 0.10, 0.38, 0.12],
        sell=[0.10, 0.10, 0.10, 0.10],
    )


@requires_core
def test_wrong_objective_disables_the_core_and_falls_back(monkeypatch):
    scenario = _fallback_scenario()

    def wrong(result):
        result = dict(result)
        result["objective_eur"] += 1.0
        return result

    _patch_core_output(monkeypatch, wrong)
    plan = dp.solve(
        scenario.slots,
        aggregate=scenario.agg,
        start_kwh=scenario.start_kwh,
        options=scenario.opts,
        generated_at=T0,
    )
    assert not _fast.available(), "a wrong objective must disable the core for the process"
    reference = _python_ref(scenario)
    assert plan.summary.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
    assert [slot.power_w for slot in plan.slots] == pytest.approx(
        [power * 1000.0 for power in reference.powers], abs=1e-9
    )


@requires_core
@pytest.mark.parametrize(
    "corrupt",
    [
        lambda result: {**result, "energies": result["energies"][:-1]},
        lambda result: {**result, "energies": [float("nan")] + result["energies"][1:]},
        lambda result: {**result, "powers": [10.0] * len(result["powers"])},
        lambda result: {**result, "terminal_error_kwh": result["terminal_error_kwh"] + 5.0},
        lambda result: {**result, "grid": result["grid"][:-1]},
        lambda result: {**result, "value": result["value"][:-8]},
        lambda result: "not a dict",
    ],
)
def test_malformed_extension_output_falls_back(monkeypatch, corrupt):
    scenario = _fallback_scenario()
    _patch_core_output(monkeypatch, corrupt)
    plan = dp.solve(
        scenario.slots,
        aggregate=scenario.agg,
        start_kwh=scenario.start_kwh,
        options=scenario.opts,
        generated_at=T0,
    )
    assert not _fast.available()
    reference = _python_ref(scenario)
    assert plan.summary.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
    assert [slot.power_w for slot in plan.slots] == pytest.approx(
        [power * 1000.0 for power in reference.powers], abs=1e-9
    )


@requires_core
def test_overpowered_action_is_rejected(monkeypatch):
    scenario = _fallback_scenario()

    def overpowered(result):
        result = dict(result)
        powers = list(result["powers"])
        powers[0] = 1000.0  # far outside p_charge and every grid window
        return {**result, "powers": powers}

    _patch_core_output(monkeypatch, overpowered)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None
    assert not _fast.available()


@requires_core
def test_soc_violating_energy_is_rejected(monkeypatch):
    scenario = _fallback_scenario()

    def violate(result):
        result = dict(result)
        energies = list(result["energies"])
        energies[-1] = scenario.agg.e_min_kwh - 1.0
        return {**result, "energies": energies}

    _patch_core_output(monkeypatch, violate)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None


@requires_core
def test_tampered_windows_are_rejected(monkeypatch):
    scenario = _fallback_scenario()

    def tamper(result):
        result = dict(result)
        windows = list(result["windows"])
        windows[0] += 0.5
        return {**result, "windows": windows}

    _patch_core_output(monkeypatch, tamper)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None


@requires_core
def test_suboptimal_but_feasible_path_is_rejected(monkeypatch):
    """Feasibility and matching money are not enough: the decision itself is verified."""
    scenario = _scenario(
        pv=[0.0] * 6,
        load=[0.3] * 6,
        buy=[0.50, 0.10, 0.45, 0.08, 0.40, 0.09],
        sell=[0.05] * 6,
        capacity_kwh=10.0,
        start_kwh=2.0,
        e_target=8.0,
        target_penalty=dp.TARGET_HARD_PENALTY,
    )

    def suboptimal(result):
        result = dict(result)
        powers = list(result["powers"])
        dt = scenario.minutes[-1] / 60.0
        lo, hi = _fast._reference_bounds(
            dp, scenario.slots[-1], scenario.agg, scenario.opts, result["energies"][-1], dt
        )
        extra = dp.exact_power_kw(result["energies"][-1], scenario.e_target, dt, scenario.agg)
        actions = dp._actions(lo, hi, scenario.opts.power_steps, extra)
        alternative = next(action for action in actions if abs(action - powers[-1]) > 1e-9)
        powers[-1] = alternative
        objective, cycle, terminal = _path_objective(powers, scenario)
        return {
            **result,
            "powers": powers,
            "objective_eur": objective,
            "cycle_cost_eur": cycle,
            "terminal_error_kwh": terminal,
        }

    _patch_core_output(monkeypatch, suboptimal)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None, "a feasible but suboptimal last action must be rejected"
    assert not _fast.available()


@requires_core
def test_corrupted_value_function_is_rejected(monkeypatch):
    scenario = _fallback_scenario()
    states = None

    def corrupt(result):
        nonlocal states
        states = result["states"]
        out = dict(result)
        values = array.array("d")
        values.frombytes(bytes(result["value"]))
        sample = _fast._sample_indices(states, 5)[2]
        values[sample] += 1.0  # value of slot 0 at a sampled state
        out["value"] = values.tobytes()
        return out

    _patch_core_output(monkeypatch, corrupt)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None, "a corrupted value function must be rejected"
    assert not _fast.available()


def _flat_tie_scenario(slots: int = 4) -> Scenario:
    """Zero prices and zero wear: every feasible action costs exactly the same."""
    return _scenario(
        pv=[0.0] * slots,
        load=[0.0] * slots,
        buy=[0.0] * slots,
        sell=[0.0] * slots,
        wear_eur_per_kwh=0.0,
        salvage_multiplier=0.0,
    )


def _corrupt_value_entry(result, row: int, index: int, entry: float):
    values = array.array("d")
    values.frombytes(bytes(result["value"]))
    values[row * result["states"] + index] = entry
    return {**result, "value": values.tobytes()}


def _count_reference_calls(monkeypatch) -> dict:
    """Record whether the Python oracle ran (it must not on a non-cross-check solve)."""
    calls = {"n": 0}
    real = dp._solve_once_numeric

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(dp, "_solve_once_numeric", counting)
    return calls


def _count_core_calls(monkeypatch) -> dict:
    """Record whether the extension was called (a declined row must never reach it)."""
    calls = {"n": 0}
    real = _fast._core.solve

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_fast._core, "solve", counting)
    return calls


def _disable_reason(caplog) -> str:
    return " ".join(record.getMessage() for record in caplog.records)


def _solve_capture(scenario: Scenario, caplog):
    with caplog.at_level(logging.WARNING, logger="app.optimize._fast"):
        return _solve_with(scenario)


def _solve_with(scenario: Scenario):
    _fast.set_check_interval(0)
    return _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    )


def _last_slot_actions(scenario: Scenario, result) -> list[float]:
    dt = scenario.minutes[-1] / 60.0
    lo, hi = _fast._reference_bounds(
        dp, scenario.slots[-1], scenario.agg, scenario.opts, result["energies"][-1], dt
    )
    return dp._actions(lo, hi, scenario.opts.power_steps, None)


def _interior_row_scenario() -> Scenario:
    """16 slots, so ``_sample_indices(16, 5)`` leaves two consecutive rows out of the sample."""
    return _scenario(
        pv=[0.0, 3.0, 0.0, 0.0, 2.0, 0.0, 0.0, 4.0, 0.0, 0.0, 1.5, 0.0, 0.0, 3.5, 0.0, 2.0],
        load=[0.6, 0.3, 1.0, 0.5, 0.4, 0.9, 0.7, 0.3, 0.5, 0.8, 0.4, 0.6, 1.1, 0.3, 0.5, 0.4],
        buy=[0.45, 0.08, 0.42, 0.10, 0.05, 0.40, 0.12, 0.38, 0.44, 0.09, 0.41, 0.11, 0.06, 0.39, 0.13, 0.37],
        sell=[0.20] * 16,
        soc_step_kwh=0.05,
        power_steps=8,
        start_kwh=3.0,
    )


def _diverge_path(
    scenario: Scenario, grid: list[float], rows: list[list[float]], terminal_row: list[float]
):
    """The forward reconstruction of a core whose own ``V_next`` rows are the ones it reports.

    ``dp._solve_once_numeric`` reuses the sweep's stored argmin when the state snaps to a grid
    point; a core with a wrong interior value instead chooses from its wrong rows, exactly what
    this mirrors.  The per-solve validation recomputes each accepted action the same way, so the
    resulting plan is *self-consistent* by construction -- that is the point of the tamper.
    """
    n = len(scenario.slots)
    e_now = dp.clamp(scenario.start_kwh, scenario.agg.e_min_kwh, scenario.agg.e_max_kwh)
    energies: list[float] = []
    powers: list[float] = []
    windows: list[float] = []
    objective = 0.0
    cycle = 0.0
    for i, slot in enumerate(scenario.slots):
        dt = scenario.minutes[i] / 60.0
        lo, hi = _fast._reference_bounds(dp, slot, scenario.agg, scenario.opts, e_now, dt)
        state = e_now
        action_lo, action_hi = lo, hi
        for point in grid:
            if abs(point - e_now) <= _fast._ENERGY_TOL:
                state = point
                action_lo, action_hi = _fast._reference_bounds(
                    dp, slot, scenario.agg, scenario.opts, state, dt
                )
                break
        extra = (
            dp.exact_power_kw(state, scenario.e_target, dt, scenario.agg)
            if i == n - 1 and scenario.e_target is not None
            else None
        )
        best_cost = math.inf
        best_power = None
        for action in dp._actions(action_lo, action_hi, scenario.opts.power_steps, extra):
            e_next = dp.next_energy_kwh(state, action, dt, scenario.agg)
            total = dp.slot_cost_eur(action, slot, dt, scenario.opts)
            total += dp.wear_cost_eur(
                scenario.opts.wear_eur_per_kwh, scenario.agg.eta_discharge, state, e_next
            )
            total += dp._interp(rows[i + 1], grid, e_next)
            if total < best_cost:
                best_cost, best_power = total, action
        power = dp.clamp(best_power, lo, hi)
        energies.append(e_now)
        powers.append(power)
        soc_lo, soc_hi = dp.soc_window_kw(e_now, scenario.agg, dt)
        grid_lo, grid_hi = dp.grid_window_kw(slot, scenario.agg, scenario.opts)
        windows.extend((soc_lo, soc_hi, grid_lo, grid_hi))
        e_next = dp.next_energy_kwh(e_now, power, dt, scenario.agg)
        objective += dp.slot_cost_eur(power, slot, dt, scenario.opts)
        cycle += dp.wear_cost_eur(
            scenario.opts.wear_eur_per_kwh, scenario.agg.eta_discharge, e_now, e_next
        )
        e_now = dp.clamp(e_next, scenario.agg.e_min_kwh, scenario.agg.e_max_kwh)
    objective += cycle + dp._interp(terminal_row, grid, e_now)
    terminal = abs(e_now - scenario.e_target) if scenario.e_target is not None else 0.0
    return energies, powers, windows, objective, cycle, terminal


def _interior_tamper(scenario: Scenario, grid: list[float], terminal_row: list[float]):
    """A core whose value is wrong at one unobserved interior cell, reporting a consistent plan.

    The cell's row *and* the row that reads it are outside the Bellman sample, so no sampled
    check touches either; the one-step validation resolves each accepted action against the same
    returned ``V_next``.  The tamper finds the runner-up action at one path state, drops that
    endpoint's continuation value by just over the cost gap so the runner-up wins, and rebuilds
    the plan the wrong value implies.
    """
    n = len(scenario.slots)
    sampled_rows = _fast._sample_indices(n, 5)
    row_index = next(
        i for i in range(1, n - 1) if i not in sampled_rows and i - 1 not in sampled_rows
    )
    captured: dict = {"row_index": row_index, "powers": None}

    def corrupt(result):
        result = dict(result)
        states = int(result["states"])
        buffer = array.array("d")
        buffer.frombytes(bytes(result["value"]))
        rows = [list(buffer[i * states : (i + 1) * states]) for i in range(n + 1)]
        slot_index = row_index - 1
        e_now = float(result["energies"][slot_index])
        dt = scenario.minutes[slot_index] / 60.0
        lo, hi = _fast._reference_bounds(
            dp, scenario.slots[slot_index], scenario.agg, scenario.opts, e_now, dt
        )
        costs = []
        for action in dp._actions(lo, hi, scenario.opts.power_steps, None):
            e_next = dp.next_energy_kwh(e_now, action, dt, scenario.agg)
            total = dp.slot_cost_eur(action, scenario.slots[slot_index], dt, scenario.opts)
            total += dp.wear_cost_eur(
                scenario.opts.wear_eur_per_kwh, scenario.agg.eta_discharge, e_now, e_next
            )
            total += dp._interp(rows[row_index], grid, e_next)
            costs.append((total, action, e_next))
        costs.sort(key=lambda item: item[0])
        gap = costs[1][0] - costs[0][0]
        cell = costs[1][2]
        probe = list(rows[row_index])
        base = dp._interp(probe, grid, cell)
        weight = 0.0
        column = 0
        for k in range(states):
            probe[k] += 1.0
            change = abs(dp._interp(probe, grid, cell) - base)
            probe[k] -= 1.0
            if change > weight:
                weight, column = change, k
        assert weight > 0.0, "the corrupted cell must be observable at the runner-up's energy"
        rows[row_index][column] -= (gap + 0.05) / weight
        energies, powers, windows, objective, cycle, terminal = _diverge_path(
            scenario, grid, rows, terminal_row
        )
        captured["powers"] = powers
        captured["column"] = column
        out = array.array("d")
        for row in rows:
            out.extend(row)
        return {
            **result,
            "value": out.tobytes(),
            "energies": energies,
            "powers": powers,
            "windows": windows,
            "objective_eur": objective,
            "cycle_cost_eur": cycle,
            "terminal_error_kwh": terminal,
        }

    return corrupt, captured


@requires_core
def test_an_unsampled_interior_value_is_only_caught_by_the_periodic_differential(
    monkeypatch, caplog
):
    """The honest bound of the per-solve contract, pinned: an unobserved interior cell can move an
    accepted action on a solve that nothing rejects, and only the periodic full differential
    catches the resulting plan afterwards.

    This is deliberate, not aspirational: the one-step check re-derives each accepted action from
    the same returned ``V_next`` and the objective from the path that value produced, so a wrong
    cell that only moves decisions the path takes still looks self-consistent.  ``0`` disables the
    periodic check (as the other per-solve tests do); ``1`` runs it on the next solve.
    """
    scenario = _interior_row_scenario()
    grid = _row_grid(scenario)
    row = _tail_row(scenario)
    corrupt, captured = _interior_tamper(scenario, grid, row)
    _patch_core_output(monkeypatch, corrupt)
    _fast.set_check_interval(0)
    accepted = _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
        terminal_row=row,
    )
    reference = _python_ref(scenario, row)
    assert accepted is not None, "the per-solve checks must not see an unobserved interior cell"
    assert captured["powers"] != reference.powers, "the corrupted cell must move an action"
    assert accepted.powers != reference.powers, "the accepted plan is the tampered one"
    assert _fast.available(), "nothing on that solve rejected the tampered result"
    _fast.reset()
    _fast.set_check_interval(1)
    with caplog.at_level(logging.WARNING, logger="app.optimize._fast"):
        assert (
            _fast.solve(
                scenario.slots,
                scenario.agg,
                scenario.start_kwh,
                scenario.opts,
                scenario.target_penalty,
                scenario.e_target,
                scenario.salvage,
                scenario.minutes,
                terminal_row=row,
            )
            is None
        ), "the periodic differential must catch the divergence"
    assert "differential self-check mismatch" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
def test_tied_higher_power_is_rejected_without_a_cross_check(monkeypatch, caplog):
    """Flat costs: a tied higher power passes money checks but not the strict argmin rule."""
    scenario = _flat_tie_scenario()
    calls = _count_reference_calls(monkeypatch)

    def tied_higher(result):
        result = dict(result)
        powers = list(result["powers"])
        actions = _last_slot_actions(scenario, result)
        assert len(actions) > 1, "the tie scenario must offer more than one action"
        powers[-1] = next(action for action in reversed(actions) if action != powers[-1])
        objective, cycle, terminal = _path_objective(powers, scenario)
        return {**result, "powers": powers, "objective_eur": objective,
                "cycle_cost_eur": cycle, "terminal_error_kwh": terminal}

    _patch_core_output(monkeypatch, tied_higher)
    assert _solve_capture(scenario, caplog) is None, "a tied-but-higher power must be rejected"
    assert calls["n"] == 0, "the rejection must not come from the periodic cross-check"
    assert "argmin" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
def test_off_action_power_is_rejected_without_a_cross_check(monkeypatch, caplog):
    """A feasible, cost-equal power that is not in the reference action set is still rejected."""
    scenario = _flat_tie_scenario()
    calls = _count_reference_calls(monkeypatch)

    def off_action(result):
        result = dict(result)
        powers = list(result["powers"])
        actions = _last_slot_actions(scenario, result)
        assert len(actions) > 1
        middle = (actions[0] + actions[1]) / 2.0
        assert all(middle != action for action in actions)
        powers[-1] = middle
        objective, cycle, terminal = _path_objective(powers, scenario)
        return {**result, "powers": powers, "objective_eur": objective,
                "cycle_cost_eur": cycle, "terminal_error_kwh": terminal}

    _patch_core_output(monkeypatch, off_action)
    assert _solve_capture(scenario, caplog) is None, "an off-action power must be rejected"
    assert calls["n"] == 0
    assert "argmin" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
@pytest.mark.parametrize("entry", [float("nan"), float("inf")])
def test_nonfinite_terminal_value_is_rejected(monkeypatch, caplog, entry):
    scenario = _fallback_scenario()
    calls = _count_reference_calls(monkeypatch)

    def bad_terminal(result):
        row = len(result["energies"])
        return _corrupt_value_entry(result, row, result["states"] // 3, entry)

    _patch_core_output(monkeypatch, bad_terminal)
    assert _solve_capture(scenario, caplog) is None
    assert calls["n"] == 0
    assert "non-finite" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
@pytest.mark.parametrize("entry", [float("nan"), float("inf")])
def test_nonfinite_sampled_value_is_rejected(monkeypatch, caplog, entry):
    scenario = _fallback_scenario()
    calls = _count_reference_calls(monkeypatch)

    def bad_sampled(result):
        # Row 0 is only read by the backward check (rows 1..n feed the decision loop).
        row = 0
        index = _fast._sample_indices(result["states"], 5)[1]
        assert row in _fast._sample_indices(len(result["powers"]), 5)
        return _corrupt_value_entry(result, row, index, entry)

    _patch_core_output(monkeypatch, bad_sampled)
    assert _solve_capture(scenario, caplog) is None
    assert calls["n"] == 0
    assert "non-finite" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
def test_nonfinite_unsampled_value_entry_is_rejected(monkeypatch, caplog):
    """The whole value buffer is checked, not only the rows/states the verifier samples."""
    scenario = _flat_tie_scenario(6)
    calls = _count_reference_calls(monkeypatch)

    def bad_unsampled(result):
        row, index = 3, 1
        assert row not in _fast._sample_indices(6, 5)
        assert index not in _fast._sample_indices(result["states"], 5)
        return _corrupt_value_entry(result, row, index, float("nan"))

    _patch_core_output(monkeypatch, bad_unsampled)
    assert _solve_capture(scenario, caplog) is None
    assert calls["n"] == 0
    assert "non-finite" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
@pytest.mark.parametrize("name", ["slot_cost_eur", "wear_cost_eur"])
def test_cost_accumulation_overflow_is_rejected(monkeypatch, caplog, name):
    """Finite operands whose sum overflows must be caught, not silently become a plan."""
    scenario = _flat_tie_scenario()
    calls = _count_reference_calls(monkeypatch)
    monkeypatch.setattr(dp, name, lambda *args, **kwargs: 1e308)
    assert _solve_capture(scenario, caplog) is None
    assert calls["n"] == 0
    assert "accumulation" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


def test_terminal_penalty_overflow_is_rejected():
    """A finite terminal penalty whose product overflows must fail closed.

    The comparison tolerance is derived from the recomputed objective, so an ``inf`` sum makes every
    tolerance infinite and ``abs(x - y) > inf`` is False: the overflow would validate itself.  A
    consistent end-to-end case cannot reach this guard, because the same product appears in the
    terminal value row and in the value-buffer finiteness check, both of which run first, so the
    guard is tested directly.
    """
    assert _fast._apply_terminal_penalty(0.5, 10.0, 1.0, 2.0) == pytest.approx(10.5)
    assert _fast._apply_terminal_penalty(0.5, 10.0, 2.0, 2.0) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="accumulation"):
        _fast._apply_terminal_penalty(-1.0, 1e308, 0.0, 8.0)


@requires_core
def test_tampered_cycle_cost_is_rejected_between_checks(monkeypatch, caplog):
    scenario = _scenario(
        pv=[0.0, 3.0, 0.0, 0.0],
        load=[0.5, 0.3, 0.9, 0.4],
        buy=[0.40, 0.10, 0.38, 0.12],
        sell=[0.10, 0.10, 0.10, 0.10],
        wear_eur_per_kwh=0.05,
    )
    calls = _count_reference_calls(monkeypatch)

    def tampered_cycle(result):
        return {**result, "cycle_cost_eur": result["cycle_cost_eur"] + 0.25}

    _patch_core_output(monkeypatch, tampered_cycle)
    assert _solve_capture(scenario, caplog) is None, "cycle cost must match the accumulated wear"
    assert calls["n"] == 0
    assert "cycle" in _disable_reason(caplog), _disable_reason(caplog)
    assert not _fast.available()


@requires_core
def test_raising_extension_call_disables_the_core(monkeypatch):
    scenario = _fallback_scenario()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated extension failure")

    monkeypatch.setattr(_fast._core, "solve", boom)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None
    assert not _fast.available()


def test_disabled_core_returns_none_without_touching_the_extension():
    _fast.disable("test")
    assert _fast.solve([], object(), 0.0, object(), 0.0, None, 0.0, []) is None


# --------------------------------------------------------------------------------------------
# A build older than its source is refused (development trap; the image always rebuilds)
# --------------------------------------------------------------------------------------------


def _write_pair(tmp_path, source_mtime: float = 1000.0, built_mtime: float = 2000.0):
    source = tmp_path / "_core.c"
    built = tmp_path / "_core.so"
    source.write_text("/* source */")
    built.write_text("binary")
    os.utime(source, (source_mtime, source_mtime))
    os.utime(built, (built_mtime, built_mtime))
    return str(built), str(source)


def _sha(text: str, length: int = _fast._BUILD_SHA_LEN) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def test_a_build_from_different_source_is_refused_by_digest(tmp_path):
    """Identity, not timestamps: a copied, restored or touched artifact must not pass as fresh."""
    built, source = _write_pair(tmp_path)
    with open(source, "w") as handle:
        handle.write("/* changed after the build */")
    reason = _fast.stale_reason(built, source, build_sha=_sha("/* source */"))
    assert reason is not None and "different" in reason
    assert _fast.stale_reason(built, source, build_sha=_sha("/* changed after the build */")) is None


def test_a_build_without_a_digest_falls_back_to_mtime(tmp_path):
    """A manual gcc line has no digest; the older-than-source check still catches the trap."""
    built, source = _write_pair(tmp_path, source_mtime=2000.0, built_mtime=1000.0)
    assert _fast.stale_reason(built, source) is not None
    newer, _ = _write_pair(tmp_path, source_mtime=1000.0, built_mtime=2000.0)
    assert _fast.stale_reason(newer, source) is None
    assert _fast.stale_reason(None, source) is None
    assert _fast.stale_reason(str(tmp_path / "absent.so"), source) is None


def test_a_missing_source_stays_permissive_and_is_logged(tmp_path, caplog):
    """Nothing to compare against must never disable a correctly built accelerator."""
    built, _ = _write_pair(tmp_path)
    with caplog.at_level(logging.INFO, logger="app.optimize._fast"):
        assert _fast.stale_reason(built, str(tmp_path / "absent.c"), build_sha="0123456789abcdef") is None
    assert "cannot be matched" in " ".join(record.getMessage() for record in caplog.records)


def test_a_built_core_on_disk_must_be_accepted():
    """A present .so the guard refuses is a build problem, not a silent skip.

    Without this, a stale artifact makes every differential/verification/budget test skip with a
    misleading reason and the suite stays green with the accelerator off.  Only the extension names
    this interpreter would actually import count: a version-tagged leftover from another interpreter
    must not fail the run (a plain ``_core.so`` is importable everywhere, so it does count).
    """
    directory = Path(_fast.__file__).parent
    importable = [directory / f"_core{suffix}" for suffix in EXTENSION_SUFFIXES]
    if not any(path.exists() for path in importable):
        pytest.skip("no importable extension on disk: nothing to check")
    assert _fast._import_error is None, _fast._import_error


@requires_core
def test_load_core_refuses_a_build_that_does_not_match_its_source(monkeypatch, tmp_path):
    """The import path itself consults the guard, using the real predicate on a real source."""
    _built, source = _write_pair(tmp_path)
    with open(source, "w") as handle:
        handle.write("/* source this build did not come from */")
    monkeypatch.setattr(_fast, "_core_source_path", lambda: source)
    module, error = _fast._load_core()
    assert module is None and error is not None
    # When the real core carries a digest the refusal must come from the digest, not from a
    # coincidental mtime ordering (which would also pass if build_sha were dropped).
    if str(getattr(_fast._core, "build_sha", "") or ""):
        assert "different" in error, error
    monkeypatch.undo()
    module, error = _fast._load_core()
    assert error is None and module is not None


# --------------------------------------------------------------------------------------------
# Periodic full differential self-check
# --------------------------------------------------------------------------------------------


@requires_core
def test_default_check_interval_is_20_and_configurable():
    assert _fast._DEFAULT_CHECK_INTERVAL == 20
    _fast.set_check_interval(7)
    assert _fast._CHECK_INTERVAL == 7
    _fast.set_check_interval(-3)
    assert _fast._CHECK_INTERVAL == 0  # negative is clamped to off


@requires_core
def test_cross_check_runs_on_first_solve_and_every_interval(monkeypatch):
    scenario = _fallback_scenario()
    calls = {"n": 0}
    real = dp._solve_once_numeric

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(dp, "_solve_once_numeric", counting)
    _fast.reset()
    _fast.set_check_interval(2)
    for expected in (1, 2, 2, 3, 3):
        assert _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
        ) is not None
        assert calls["n"] == expected


@requires_core
def test_cross_check_mismatch_disables_the_core(monkeypatch):
    scenario = _fallback_scenario()
    real = dp._solve_once_numeric

    def diverging(*args, **kwargs):
        return replace(real(*args, **kwargs), objective_eur=real(*args, **kwargs).objective_eur + 1.0)

    monkeypatch.setattr(dp, "_solve_once_numeric", diverging)
    _fast.reset()
    _fast.set_check_interval(1)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is None
    assert not _fast.available(), "a cross-check mismatch must disable the core permanently"


@requires_core
def test_cross_check_off_never_runs_the_reference(monkeypatch):
    scenario = _fallback_scenario()
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("the reference must not run when the cadence is off")

    monkeypatch.setattr(dp, "_solve_once_numeric", boom)
    _fast.reset()
    _fast.set_check_interval(0)
    assert _fast.solve(
        scenario.slots,
        scenario.agg,
        scenario.start_kwh,
        scenario.opts,
        scenario.target_penalty,
        scenario.e_target,
        scenario.salvage,
        scenario.minutes,
    ) is not None
    assert calls["n"] == 0


# --------------------------------------------------------------------------------------------
# Malformed inputs and unsupported dimensions at the C boundary
# --------------------------------------------------------------------------------------------


def _base_config(**overrides) -> dict:
    config = {
        "e_min_kwh": 0.4,
        "e_max_kwh": 8.0,
        "p_charge_kw": 4.0,
        "p_discharge_kw": 4.0,
        "eta_charge": 0.94,
        "eta_discharge": 0.92,
        "start_kwh": 3.0,
        "slot_minutes": 60.0,
        "soc_step_kwh": 0.05,
        "power_steps": 8,
        "allow_grid_charge": True,
        "allow_export": True,
        "max_import_kw": 0.0,
        "max_export_kw": 0.0,
        "min_spread_eur_per_kwh": 0.0,
        "pv_charge_preference": "auto",
        "wear_eur_per_kwh": 0.0,
        "salvage": 0.0,
        "target_penalty": 1e6,
        "max_states": 400,
        "max_iterations": 2_000_000,
    }
    config.update(overrides)
    return config


@requires_core
def test_core_rejects_malformed_inputs():
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve([0.0], [0.0, 1.0], [0.1], [0.1], [60.0], _base_config())  # length mismatch
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve([float("nan")], [0.0], [0.1], [0.1], [60.0], _base_config())  # non-finite
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve([0.0], [0.0], [0.1], [0.1], [60.0], {})  # missing config keys
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve([0.0], [0.0], [0.1], [0.1], [60.0], None)  # not a dict
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve(object(), [0.0], [0.1], [0.1], [60.0], _base_config())  # not a sequence


@requires_core
@pytest.mark.parametrize(
    "overrides",
    [
        {"max_states": 2**32},  # would narrow to a tiny int without the cap
        {"max_states": 1},  # below the supported grid
        {"max_states": -5},
        {"power_steps": 2**62},  # would overflow the action buffer size
        {"power_steps": -1},
        {"max_iterations": 0},
        {"e_min_kwh": 1.0, "e_max_kwh": -1.0},
        {"e_min_kwh": -1e308, "e_max_kwh": 1e308},  # span overflows to inf
        {"eta_charge": 0.0},
        {"eta_discharge": -0.5},
        {"slot_minutes": -1.0},
        {"soc_step_kwh": float("inf")},
        {"p_charge_kw": -1.0},
        {"p_discharge_kw": -1.0},
    ],
)
def test_core_rejects_unsupported_dimensions_and_domains(overrides):
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve([0.0], [0.0], [0.1], [0.1], [60.0], _base_config(**overrides))


@requires_core
def test_core_rejects_nonfinite_derived_ratios():
    # A finite but absurd span with a subnormal slot duration overflows the soc-window ratio, which
    # is exactly the value an unchecked integer cast used to consume.
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve(
            [0.0], [0.0], [0.1], [0.1], [1e-300], _base_config(e_min_kwh=-1e12, e_max_kwh=1e12)
        )
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve(
            [0.0], [0.0], [0.1], [0.1], [60.0],
            _base_config(e_min_kwh=-1e12, e_max_kwh=1e12, eta_charge=1e-300),
        )


@requires_core
def test_core_rejects_oversized_scenarios():
    oversized = [0.0] * (1_000_001)
    with pytest.raises((ValueError, TypeError)):
        _fast._core.solve(oversized, oversized, [0.1], [0.1], [60.0], _base_config())


@requires_core
def test_conversion_callback_cannot_shrink_the_source_list():
    """A ``__float__`` callback that clears its own list must not make the read go out of bounds."""
    values: list = []

    class Shrinker:
        def __float__(self) -> float:
            values.clear()
            return 1.0

    values.extend(Shrinker() for _ in range(4))
    result = _fast._core.solve(values, [0.0] * 4, [0.1] * 4, [0.1] * 4, [60.0] * 4, _base_config())
    assert isinstance(result, dict)
    assert len(result["energies"]) == 4
    assert values == [], "the callback did run and mutated the list during the call"


# --------------------------------------------------------------------------------------------
# The budget gate (docs/OPTIMIZER_V2.md section 6)
# --------------------------------------------------------------------------------------------


def _benchmark_scenario() -> Scenario:
    """120 slots (30 h of 15-minute slots), the default grid (<= 400 states), power_steps 24."""
    rng = random.Random(7)
    n = 120
    pv: list[float] = []
    load: list[float] = []
    buy: list[float] = []
    sell: list[float] = []
    for i in range(n):
        hour = (i // 4) % 24
        base = 0.20 + 0.25 * math.sin((hour - 6) / 24 * 2 * math.pi)
        price = round(base + 0.1155 + rng.uniform(-0.01, 0.01), 4)
        pv.append(round(max(0.0, 4.0 * math.sin((hour - 7) / 12 * math.pi)), 4) if 7 <= hour <= 19 else 0.0)
        load.append(round(0.4 + 0.3 * math.cos((hour - 18) / 24 * 2 * math.pi), 4))
        buy.append(price)
        sell.append(round(price - 0.1155, 4))
    return _scenario(
        pv=pv,
        load=load,
        buy=buy,
        sell=sell,
        slot_minutes=15,
        start_kwh=5.0,
        capacity_kwh=10.0,
        soc_step_kwh=0.02,
        power_steps=24,
        wear_eur_per_kwh=0.02,
    )


@requires_core
def test_120_slot_core_benchmark(capsys):
    scenario = _benchmark_scenario()
    _fast.set_check_interval(0)

    start = _perf_counter()
    reference = _python_ref(scenario)
    python_ms = (_perf_counter() - start) * 1000.0

    # The budget gate is the pure C sweep (the Python-side verification is layered on top and is
    # reported separately).
    config = _fast._config(
        dp, scenario.agg, scenario.opts, scenario.start_kwh, scenario.e_target,
        scenario.salvage, scenario.target_penalty,
    )
    args = (
        [float(slot.pv_kw) for slot in scenario.slots],
        [float(slot.load_kw) for slot in scenario.slots],
        [float(slot.buy) for slot in scenario.slots],
        [float(slot.sell or 0.0) for slot in scenario.slots],
        [float(value) for value in scenario.minutes],
        config,
    )
    raw_timings = []
    for _ in range(5):
        start = _perf_counter()
        raw = _fast._core.solve(*args)
        raw_timings.append((_perf_counter() - start) * 1000.0)
    assert len(raw["grid"]) <= 400

    validated_timings = []
    for _ in range(3):
        _fast.reset()
        _fast.set_check_interval(0)
        start = _perf_counter()
        result = _fast.solve(
            scenario.slots,
            scenario.agg,
            scenario.start_kwh,
            scenario.opts,
            scenario.target_penalty,
            scenario.e_target,
            scenario.salvage,
            scenario.minutes,
        )
        validated_timings.append((_perf_counter() - start) * 1000.0)
        assert result is not None
    assert result.iterations == reference.iterations
    raw_ms = min(raw_timings)
    validated_ms = min(validated_timings)
    with capsys.disabled():
        print(
            f"\n[dp-core benchmark] 120 slots / {result.states} states / power_steps 24: "
            f"Python {python_ms:.1f} ms vs C sweep {raw_ms:.2f} ms "
            f"(validated {validated_ms:.2f} ms, best of {len(raw_timings)}/{len(validated_timings)})"
        )
    assert raw_ms < 20.0, f"the C core must solve 120 slots in < 20 ms (got {raw_ms:.2f} ms)"


@requires_core
def test_terminal_row_benchmark(capsys):
    """Stage-3 shapes: a row-mode solve must keep the C sweep's budget.

    Shape A is the live-like stage-3 measurement: 120 slots of 15 minutes over <= 400 states with
    a 60-minute tail, whose continuation row comes from a real tail sweep.  Shape B is the same
    120-slot breadth (the same series, every entry kept) on 60-minute slots -- the coarse-slot
    workload, not a 30-slot slice of it.  Shape C adds a terminal target, whose hard-then-soft
    retry runs this same shape a second time per plan (the budget reserves both main sweeps).
    Python (the reference that ran when the row bypassed the core) is timed once per shape; the
    raw sweep and the validated solve are reported separately, as in the non-row gate above.
    """
    live_like = _benchmark_scenario()
    hourly = _scenario(
        pv=[slot.pv_kw for slot in live_like.slots],
        load=[slot.load_kw for slot in live_like.slots],
        buy=[slot.buy for slot in live_like.slots],
        sell=[slot.sell for slot in live_like.slots],
        slot_minutes=60,
        start_kwh=5.0,
        capacity_kwh=10.0,
        soc_step_kwh=0.02,
        power_steps=24,
        wear_eur_per_kwh=0.02,
    )
    targeted = _scenario(
        pv=[slot.pv_kw for slot in live_like.slots],
        load=[slot.load_kw for slot in live_like.slots],
        buy=[slot.buy for slot in live_like.slots],
        sell=[slot.sell for slot in live_like.slots],
        slot_minutes=15,
        start_kwh=5.0,
        capacity_kwh=10.0,
        soc_step_kwh=0.02,
        power_steps=24,
        wear_eur_per_kwh=0.02,
        e_target=9.0,
    )
    shapes = (
        ("live-like 120x15min + 60min tail", live_like),
        ("120x60min", hourly),
        ("terminal target", targeted),
    )
    for name, scenario in shapes:
        _fast.reset()
        _fast.set_check_interval(0)
        # 60 minutes of tail, in the shape's own slot length.
        row = _tail_row(scenario, length=max(1, int(60 // scenario.opts.slot_minutes)))
        start = _perf_counter()
        reference = _python_ref(scenario, row)  # what the row mode cost before the core used rows
        python_ms = (_perf_counter() - start) * 1000.0
        config = _fast._config(
            dp, scenario.agg, scenario.opts, scenario.start_kwh, scenario.e_target,
            scenario.salvage, scenario.target_penalty,
        )
        args = (
            [float(slot.pv_kw) for slot in scenario.slots],
            [float(slot.load_kw) for slot in scenario.slots],
            [float(slot.buy) for slot in scenario.slots],
            [float(slot.sell or 0.0) for slot in scenario.slots],
            [float(value) for value in scenario.minutes],
            config,
            row,
        )
        raw_timings = []
        for _ in range(3):
            start = _perf_counter()
            raw = _fast._core.solve(*args)
            raw_timings.append((_perf_counter() - start) * 1000.0)
        assert len(raw["grid"]) <= 400
        validated_timings = []
        for _ in range(2):
            _fast.reset()
            _fast.set_check_interval(0)
            start = _perf_counter()
            result = _fast.solve(
                scenario.slots,
                scenario.agg,
                scenario.start_kwh,
                scenario.opts,
                scenario.target_penalty,
                scenario.e_target,
                scenario.salvage,
                scenario.minutes,
                terminal_row=row,
            )
            validated_timings.append((_perf_counter() - start) * 1000.0)
            assert result is not None
        assert result.states == reference.states
        assert result.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
        assert result.powers == pytest.approx(reference.powers, abs=1e-12)
        raw_ms = min(raw_timings)
        validated_ms = min(validated_timings)
        with capsys.disabled():
            print(
                f"\n[dp-core terminal-row benchmark] {name} / {result.states} states / "
                f"power_steps 24: Python {python_ms:.1f} ms vs C sweep {raw_ms:.2f} ms "
                f"(validated {validated_ms:.2f} ms, best of "
                f"{len(raw_timings)}/{len(validated_timings)})"
            )
        assert raw_ms < 20.0, f"the C core must solve {name} in < 20 ms (got {raw_ms:.2f} ms)"


def test_the_core_reports_which_backend_is_live():
    """Additive (0.27.0): ``_fast.status()`` is what ``/health`` publishes.

    The accelerator is optional and fails open, so "python" is a legitimate answer -- but it has to
    be a *reported* one, including why, because a deployed image cannot otherwise be asked whether
    the compiled core is live.  Each state is exercised with the real toggles rather than stubs, so
    a change to ``disable``/``reset`` that stopped recording the reason would fail here.
    """
    _fast.reset()
    live = _fast.status()
    assert live["backend"] in ("c", "python")
    assert (live["build_sha"] is not None) == (live["backend"] == "c")
    if live["backend"] == "python":
        # No extension in this checkout: the reason must say so rather than being empty.
        assert live["reason"]
    else:
        assert live["reason"] is None

    if _fast._core is None:
        pytest.skip("the extension is not built in this checkout")

    _fast.disable("a verification mismatch")
    off = _fast.status()
    assert off == {"backend": "python", "build_sha": None, "reason": "a verification mismatch"}
    assert _fast.available() is False

    _fast.reset()
    back = _fast.status()
    assert back["backend"] == "c" and back["reason"] is None and back["build_sha"]
