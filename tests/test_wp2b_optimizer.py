"""WP2B optimiser tests (SPEC sections 8 and 14).

The brute-force comparison uses a family of instances whose reachable energies all land on the
DP's state grid (one full charge window per slot, one action step per grid cell), so the DP must
equal exhaustive search to machine precision.  A second, harder random family checks the
quantisation bound instead of equality.
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    Reason,
    SolarForecast,
    SolarSlot,
    clamp,
)
from app.optimize import dp  # noqa: E402
from app.optimize.allocate import allocate_power, headroom_w  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DOCUMENTED_REASONS = {
    Reason.ARBITRAGE_CHARGE,
    Reason.PV_SURPLUS_CHARGE,
    Reason.PEAK_DISCHARGE,
    Reason.SELF_CONSUMPTION,
    Reason.RESERVE_HOLD,
    Reason.GRID_LIMIT_HOLD,
    Reason.SPREAD_BELOW_THRESHOLD,
    Reason.TERMINAL_TARGET,
    Reason.FULL,
    Reason.EMPTY,
}


def battery(**overrides) -> BatteryConfig:
    data = dict(
        id="example_1",
        name="Example Battery 1",
        driver="generic",
        capacity_kwh=2.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=1000.0,
        max_discharge_w=1000.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        mapping={"power_setpoint_entity": "number.example_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def make_slots(count: int, minutes: int, pv=0.0, load=0.5, buy=0.2, sell=0.05, stale=False):
    return [
        dp.SlotInput(
            t=T0 + timedelta(minutes=minutes * i),
            pv_kw=pv[i] if isinstance(pv, list) else pv,
            load_kw=load[i] if isinstance(load, list) else load,
            buy=buy[i] if isinstance(buy, list) else buy,
            sell=sell[i] if isinstance(sell, list) else sell,
            price_stale=stale,
        )
        for i in range(count)
    ]


def plan_objective(plan, opts: dp.OptimizerOptions, salvage: float, agg: dp.BatteryAggregate) -> float:
    """Exact objective of a reconstructed plan: costs + cycle cost - salvage*e_N."""
    dt = opts.slot_minutes / 60.0
    energies = [slot.soc_kwh for slot in plan.slots]
    total = 0.0
    for slot in plan.slots:
        source = dp.SlotInput(t=slot.t, pv_kw=slot.pv_kw, load_kw=slot.load_kw, buy=slot.buy, sell=slot.sell)
        total += dp.slot_cost_eur(slot.power_w / 1000.0, source, dt, opts)
    final = dp.next_energy_kwh(energies[-1], plan.slots[-1].power_w / 1000.0, dt, agg)
    for index, energy in enumerate(energies):
        nxt = energies[index + 1] if index + 1 < len(energies) else final
        total += dp.wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, energy, nxt)
    return total - salvage * final


def brute_force(slots, agg, start_kwh, opts, salvage) -> float:
    """Exhaustive search over the same action sets; the reference optimum."""
    dt = opts.slot_minutes / 60.0
    best = [float("inf")]

    def walk(index: int, energy: float, cost: float) -> None:
        if index == len(slots):
            best[0] = min(best[0], cost - salvage * energy)
            return
        lo, hi, _ = dp.power_bounds_kw(slots[index], energy, agg, opts, dt)
        for power in dp._actions(lo, hi, opts.power_steps):
            nxt = dp.next_energy_kwh(energy, power, dt, agg)
            step = dp.slot_cost_eur(power, slots[index], dt, opts)
            step += dp.wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, energy, nxt)
            walk(index + 1, clamp(nxt, agg.e_min_kwh, agg.e_max_kwh), cost + step)

    walk(0, start_kwh, 0.0)
    return best[0]


def grid_aligned_instance(rng: random.Random, count: int):
    minutes = rng.choice([60, 30])
    dt = minutes / 60.0
    power = rng.choice([1.0, 2.0, 3.0])
    capacity = power * dt  # a full charge window fits exactly in one slot
    config = battery(
        capacity_kwh=capacity,
        max_charge_w=power * 1000.0,
        max_discharge_w=power * 1000.0,
        eta_charge=1.0,
        eta_discharge=1.0,
        min_soc_pct=0.0,
    )
    opts = dp.OptimizerOptions(
        slot_minutes=minutes,
        soc_step_kwh=capacity / 2.0,
        power_steps=3,
        salvage_multiplier=rng.choice([0.0, 1.0]),
        wear_eur_per_kwh=rng.choice([0.0, 0.05]),
    )
    slots = make_slots(
        count,
        minutes,
        pv=[round(rng.uniform(0, 3), 2) for _ in range(count)],
        load=[round(rng.uniform(0, 3), 2) for _ in range(count)],
        buy=[round(rng.uniform(-0.05, 0.6), 3) for _ in range(count)],
        sell=[round(rng.uniform(0.0, 0.3), 3) for _ in range(count)],
    )
    start = rng.choice([0.0, capacity / 2.0, capacity])
    return config, opts, slots, start


def test_dp_matches_brute_force_on_grid_aligned_instances():
    rng = random.Random(3)
    for _ in range(60):
        config, opts, slots, start = grid_aligned_instance(rng, rng.randint(4, 8))
        agg = dp.BatteryAggregate.from_configs([config], 0.0)
        salvage = opts.salvage_multiplier * dp._median([slot.buy for slot in slots]) * agg.eta_discharge
        plan = dp.solve(slots, aggregate=agg, start_kwh=start, options=opts, generated_at=T0)
        grid, _step = dp._build_grid(agg, opts.soc_step_kwh, [start])
        for slot in plan.slots:  # every visited state is a grid point, so interpolation is exact
            assert any(abs(slot.soc_kwh - point) < 1e-9 for point in grid)
        difference = plan_objective(plan, opts, salvage, agg) - brute_force(slots, agg, start, opts, salvage)
        assert abs(difference) < 1e-6, difference


def test_dp_is_within_quantisation_bound_of_brute_force_on_random_instances():
    rng = random.Random(11)
    for _ in range(25):
        minutes = rng.choice([15, 30])
        dt = minutes / 60.0
        power = rng.choice([0.5, 1.0, 1.5])
        config = battery(
            capacity_kwh=rng.choice([1.0, 1.5, 3.0]),
            max_charge_w=power * 1000.0,
            max_discharge_w=power * 1000.0,
            eta_charge=rng.choice([0.9, 0.95]),
            eta_discharge=rng.choice([0.9, 0.95]),
        )
        opts = dp.OptimizerOptions(
            slot_minutes=minutes,
            soc_step_kwh=rng.choice([0.1, 0.25]),
            power_steps=rng.choice([3, 5]),
            salvage_multiplier=rng.choice([0.0, 1.0]),
            wear_eur_per_kwh=rng.choice([0.0, 0.02]),
            allow_grid_charge=rng.choice([True, False]),
            allow_export=rng.choice([True, False]),
            max_import_kw=rng.choice([0.0, 2.0]),
            max_export_kw=rng.choice([0.0, 2.0]),
            min_spread_eur_per_kwh=rng.choice([0.0, 0.05]),
        )
        agg = dp.BatteryAggregate.from_configs([config], 0.0)
        count = rng.randint(4, 8)
        slots = make_slots(
            count,
            minutes,
            pv=[round(rng.uniform(0, 2), 2) for _ in range(count)],
            load=[round(rng.uniform(0, 2), 2) for _ in range(count)],
            buy=[round(rng.uniform(-0.05, 0.5), 3) for _ in range(count)],
            sell=[round(rng.uniform(0, 0.2), 3) for _ in range(count)],
        )
        start = clamp(rng.uniform(agg.e_min_kwh, agg.e_max_kwh), agg.e_min_kwh, agg.e_max_kwh)
        salvage = opts.salvage_multiplier * dp._median([slot.buy for slot in slots]) * agg.eta_discharge
        plan = dp.solve(slots, aggregate=agg, start_kwh=start, options=opts, generated_at=T0)
        # 0.25 EUR = one grid cell's worth of the widest plausible price swing; the DP may pay
        # that much for having a discretised state space, and never more.
        tolerance = 0.25 + 1e-6
        assert plan_objective(plan, opts, salvage, agg) <= brute_force(slots, agg, start, opts, salvage) + tolerance


def test_plan_respects_physical_limits():
    rng = random.Random(5)
    for _ in range(20):
        config, opts, slots, start = grid_aligned_instance(rng, rng.randint(4, 8))
        opts = dp.OptimizerOptions(**{**opts.__dict__, "allow_grid_charge": True, "allow_export": True})
        agg = dp.BatteryAggregate.from_configs([config], 0.0)
        plan = dp.solve(slots, aggregate=agg, start_kwh=start, options=opts, generated_at=T0)
        dt = opts.slot_minutes / 60.0
        energies = [slot.soc_kwh for slot in plan.slots]
        assert all(agg.e_min_kwh - 1e-9 <= e <= agg.e_max_kwh + 1e-9 for e in energies)
        for slot in plan.slots:
            assert -config.max_discharge_w - 1e-6 <= slot.power_w <= config.max_charge_w + 1e-6
        for index, slot in enumerate(plan.slots[:-1]):
            expected = dp.next_energy_kwh(energies[index], slot.power_w / 1000.0, dt, agg)
            assert abs(expected - energies[index + 1]) < 1e-9


def test_dp_deterministic_across_three_runs():
    config, opts, slots, start = grid_aligned_instance(random.Random(21), 6)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    runs = []
    for _ in range(3):
        plan = dp.solve(slots, aggregate=agg, start_kwh=start, options=opts, generated_at=T0)
        runs.append(
            [
                (slot.t, slot.power_w, slot.soc_kwh, slot.action.value, slot.reason)
                for slot in plan.slots
            ]
        )
    assert runs[0] == runs[1] == runs[2]


def test_high_pv_window_charges_then_discharges():
    minutes = 15
    config = battery(
        capacity_kwh=8.0, max_charge_w=2000.0, max_discharge_w=2000.0, min_soc_pct=5.0
    )
    opts = dp.OptimizerOptions(slot_minutes=minutes, soc_step_kwh=0.1, power_steps=12)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    count = 96
    slots = []
    for index in range(count):
        hour = index / 4
        pv = 4.0 if 11 <= hour < 15 else 0.0  # a strong midday PV window
        buy = 0.45 if hour >= 18 else 0.2
        slots.append(
            dp.SlotInput(
                t=T0 + timedelta(minutes=minutes * index),
                pv_kw=pv,
                load_kw=0.5,
                buy=buy,
                sell=0.05,
            )
        )
    plan = dp.solve(slots, aggregate=agg, start_kwh=agg.e_min_kwh, options=opts, generated_at=T0)
    actions = [slot.action for slot in plan.slots]
    assert Action.PV_CHARGE in actions
    assert Action.DISCHARGE in actions or Action.EXPORT in actions
    charge_indices = [i for i, a in enumerate(actions) if a == Action.PV_CHARGE]
    discharge_indices = [i for i, a in enumerate(actions) if a in (Action.DISCHARGE, Action.EXPORT)]
    assert max(charge_indices) < min(discharge_indices)
    assert plan.summary.charged_kwh > 0 and plan.summary.discharged_kwh > 0
    assert plan.summary.saved_eur > 0


def test_flat_prices_do_not_arbitrage_below_min_spread():
    minutes = 15
    config = battery(capacity_kwh=5.0, max_charge_w=1000.0, max_discharge_w=1000.0)
    opts = dp.OptimizerOptions(
        slot_minutes=minutes, soc_step_kwh=0.1, power_steps=8, min_spread_eur_per_kwh=0.05
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = make_slots(24, minutes, pv=0.0, load=0.4, buy=0.25, sell=0.25)
    plan = dp.solve(slots, aggregate=agg, start_kwh=2.0, options=opts, generated_at=T0)
    # A round trip at flat prices is break-even at best and a loss once the user's 0.05 spread
    # floor is charged, so the pack is never charged from the grid.  (Discharging is break-even
    # too, now that the terminal value is discounted by eta_dis -- the point is no arbitrage.)
    assert all(slot.power_w <= 30.0 for slot in plan.slots), "flat prices must not charge the pack"
    assert plan.summary.charged_kwh < 0.05


def test_terminal_target_is_reached_when_feasible():
    minutes = 15
    config = battery(capacity_kwh=5.0, max_charge_w=2000.0, max_discharge_w=2000.0, min_soc_pct=0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=minutes,
        soc_step_kwh=0.05,
        power_steps=12,
        target_soc_pct_end=80.0,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = make_slots(12, minutes, pv=0.0, load=0.4, buy=0.3, sell=0.05)
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
    assert plan.feasible and plan.infeasible_reason is None
    final = dp.next_energy_kwh(
        plan.slots[-1].soc_kwh, plan.slots[-1].power_w / 1000.0, opts.slot_minutes / 60.0, agg
    )
    assert abs(final - 4.0) < 1e-6
    assert any(slot.reason == Reason.TERMINAL_TARGET for slot in plan.slots)


def test_infeasible_terminal_target_degrades_to_soft_penalty():
    minutes = 15
    config = battery(capacity_kwh=5.0, max_charge_w=500.0, max_discharge_w=500.0, min_soc_pct=0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=minutes,
        soc_step_kwh=0.05,
        power_steps=8,
        target_soc_pct_end=100.0,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = make_slots(2, minutes, pv=0.0, load=0.4, buy=0.3, sell=0.05)  # 0.25 kWh reachable
    plan = dp.solve(slots, aggregate=agg, start_kwh=0.0, options=opts, generated_at=T0)
    assert plan.feasible is False
    assert plan.infeasible_reason == "terminal_target_unreachable"
    assert plan.slots  # still produces a plan, never an exception
    final = dp.next_energy_kwh(
        plan.slots[-1].soc_kwh, plan.slots[-1].power_w / 1000.0, opts.slot_minutes / 60.0, agg
    )
    assert final <= 0.25 + 1e-9


def test_stale_price_forecast_is_flat():
    minutes = 15
    config = battery(capacity_kwh=5.0, max_charge_w=1000.0, max_discharge_w=1000.0)
    price = PriceForecast(source="example_price", stale=True)
    solar = SolarForecast(source="example_solar")
    load = LoadForecast(source="example_load")
    for index in range(48):
        moment = T0 + timedelta(minutes=minutes * index)
        price.slots.append(PriceSlot(t=moment, buy=0.1 + index * 0.01, sell=0.02, stale=True))
        solar.slots.append(SolarSlot(t=moment, pv_kw=0.0))
        load.slots.append(LoadSlot(t=moment, load_kw=0.5))
    settings = {
        "optimizer.slot_minutes": minutes,
        "optimizer.horizon_hours": 12,
        "optimizer.soc_step_kwh": 0.1,
        "optimizer.power_steps": 8,
    }
    plan = dp.solve_from_forecasts(
        price, solar, load, battery_configs=[config], start_kwh=2.0, settings=settings, now=T0
    )
    buys = {slot.buy for slot in plan.slots}
    assert len(buys) == 1  # flattened to the median
    assert all(slot.price_stale for slot in plan.slots)
    assert plan.sources.get("price_stale") == "flat"
    # A dead forecast must not invent arbitrage: nothing is charged.  (A break-even discharge is
    # allowed now that the terminal value is discounted by eta_dis; only charging invents value.)
    assert all(slot.power_w <= 1e-6 for slot in plan.slots), plan.slots[0]


def test_no_batteries_returns_hold_schedule():
    price = PriceForecast(source="example")
    solar = SolarForecast(source="example")
    load = LoadForecast(source="example")
    for index in range(8):
        moment = T0 + timedelta(minutes=15 * index)
        price.slots.append(PriceSlot(t=moment, buy=0.2, sell=0.05))
        solar.slots.append(SolarSlot(t=moment, pv_kw=0.0))
        load.slots.append(LoadSlot(t=moment, load_kw=0.5))
    plan = dp.solve_from_forecasts(
        price, solar, load, battery_configs=[], start_kwh=0.0, settings={}, now=T0
    )
    assert plan.feasible is True
    assert all(slot.action is Action.HOLD for slot in plan.slots)
    assert abs(plan.summary.cost_estimate_eur - plan.summary.baseline_cost_eur) < 1e-9


def test_align_slots_gap_fills_and_flags_stale():
    price = PriceForecast(source="example")
    solar = SolarForecast(source="example")
    load = LoadForecast(source="example")
    price.slots.append(PriceSlot(t=T0, buy=0.2, sell=0.05))
    price.slots.append(PriceSlot(t=T0 + timedelta(hours=3), buy=0.4, sell=0.05))  # a 3 h gap
    solar.slots.append(SolarSlot(t=T0, pv_kw=1.0))
    for index in range(8):
        load.slots.append(LoadSlot(t=T0 + timedelta(minutes=15 * index), load_kw=0.5, p10_kw=0.3, p90_kw=0.8))
    slots = dp.align_slots(price, solar, load, T0, T0 + timedelta(hours=2), 15)
    assert slots[0].buy == 0.2 and slots[0].price_stale is False
    assert slots[1].buy == 0.2 and slots[1].price_stale is True  # filled from the 15 min old slot
    assert all(slot.price_stale for slot in slots[1:])
    assert slots[0].load_p10_kw == 0.3 and slots[0].load_p90_kw == 0.8
    # a gap beyond the requested window still back-fills the first slot
    later = dp.align_slots(price, solar, load, T0 + timedelta(hours=4), T0 + timedelta(hours=5), 15)
    assert all(slot.buy == 0.4 for slot in later)


def test_reason_slugs_are_documented():
    minutes = 15
    config = battery(capacity_kwh=6.0, max_charge_w=1500.0, max_discharge_w=1500.0)
    opts = dp.OptimizerOptions(slot_minutes=minutes, soc_step_kwh=0.1, power_steps=10)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = []
    for index in range(48):
        hour = index / 4
        slots.append(
            dp.SlotInput(
                t=T0 + timedelta(minutes=minutes * index),
                pv_kw=3.0 if 10 <= hour < 15 else 0.0,
                load_kw=0.6,
                buy=0.4 if hour >= 18 else (0.12 if hour < 8 else 0.25),
                sell=0.05,
            )
        )
    plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
    assert all(slot.reason in DOCUMENTED_REASONS for slot in plan.slots)
    assert all(isinstance(slot.action, Action) for slot in plan.slots)
    assert plan.summary.effective_step_kwh > 0
    assert plan.summary.solver_iterations > 0


# ---------------------------------------------------------------------------- allocation


def test_allocate_splits_proportionally_to_headroom():
    big = battery(id="b_big", capacity_kwh=10.0, max_charge_w=2000.0)
    small = battery(id="b_small", capacity_kwh=5.0, max_charge_w=1000.0)
    entries = allocate_power(
        1500.0, [big, small], {"b_big": 5.0, "b_small": 2.5}, slot_hours=0.25
    )
    by_id = {config.id: power for config, power in entries}
    assert abs(sum(by_id.values()) - 1500.0) < 1e-6
    assert by_id["b_big"] > by_id["b_small"] > 0.0
    # id order is stable and disabled batteries are absent
    assert [config.id for config, _ in entries] == ["b_big", "b_small"]
    disabled = battery(id="b_off", enabled=False)
    assert [c.id for c, _ in allocate_power(500.0, [disabled], {})] == []


def test_allocate_clamps_to_headroom_and_redistributes_once():
    a = battery(id="a", capacity_kwh=2.0, max_charge_w=1000.0)
    b = battery(id="b", capacity_kwh=2.0, max_charge_w=1000.0)
    # both batteries are nearly full: only a little room in total
    energies = {"a": 1.9, "b": 1.9}
    entries = allocate_power(2000.0, [a, b], energies, slot_hours=0.25)
    total = sum(power for _, power in entries)
    room_each = headroom_w(a, 1.9, charging=True, slot_hours=0.25)
    assert 0.0 < room_each < 1000.0
    assert abs(total - 2 * room_each) < 1e-6
    assert all(power >= 0 for _, power in entries)


def test_allocate_prefers_better_efficiency_for_partial_demand():
    slow = battery(id="a_slow", capacity_kwh=5.0, max_charge_w=1000.0, eta_charge=0.8)
    fast = battery(id="b_fast", capacity_kwh=5.0, max_charge_w=1000.0, eta_charge=1.0)
    entries = dict(
        (config.id, power)
        for config, power in allocate_power(400.0, [slow, fast], {}, slot_hours=0.25)
    )
    assert abs(entries["b_fast"] - entries["a_slow"]) < 1e-9  # room-limited split while both fit
    # with little room left in the slow battery the leftover goes to the efficient one
    entries = dict(
        (config.id, power)
        for config, power in allocate_power(
            1000.0, [slow, fast], {"a_slow": 4.95, "b_fast": 3.0}, slot_hours=0.25
        )
    )
    assert entries["b_fast"] > entries["a_slow"] > 0
    assert abs(sum(entries.values()) - 1000.0) < 1e-6


def test_allocate_discharge_respects_floor():
    config = battery(id="a", capacity_kwh=4.0, min_soc_pct=25.0, max_discharge_w=1000.0)
    entries = allocate_power(-1000.0, [config], {"a": 1.02}, slot_hours=0.25)
    power = entries[0][1]
    expected_max = (1.02 - 4.0 * 0.25) * config.eta_discharge / 0.25 * 1000.0
    assert power >= -expected_max - 1e-6
    assert power <= 0.0
    assert entries[0][0].id == "a"


def test_allocate_enforces_the_per_battery_reserve_floor():
    """WP5 repro: 5.0 / 0.55 kWh, -2000 W, reserve 30% (1.5 kWh on the 5.0 kWh battery).

    The reserve is an extra floor above ``min_soc`` (SPEC 3) and must hold *per battery*: with the
    old min_soc-only headroom the low battery was drained to 0.157 kWh, below its own 0.165 kWh
    floor, while the full battery stayed untouched.
    """
    reserve = 30.0
    full = battery(id="a_full", capacity_kwh=5.0, min_soc_pct=10.0, max_discharge_w=2000.0)
    low = battery(id="b_low", capacity_kwh=0.55, min_soc_pct=10.0, max_discharge_w=2000.0)
    energies = {"a_full": 5.0, "b_low": 0.166}
    hours = 30 / 3600
    entries = allocate_power(
        -2000.0, [full, low], energies, slot_hours=hours, reserve_soc_pct=reserve
    )
    after = {}
    for config, power in entries:
        after[config.id] = energies[config.id] + (power / 1000.0) * hours / config.eta_discharge
        floor = config.capacity_kwh * max(config.min_soc_pct, reserve) / 100.0
        assert after[config.id] >= floor - 1e-9, (config.id, after[config.id], floor)
    # the low battery is stopped at its own floor (it may give up 1 Wh, not 8.8 Wh)
    assert 0.165 - 1e-9 <= after["b_low"] <= 0.166
    assert abs(after["b_low"] - 0.165) < 1e-3
    assert abs(sum(power for _, power in entries) + 2000.0) < 1e-6
    # an impossible request exhausts every battery's own room and stops exactly at the floors
    pinned = allocate_power(
        -5000.0, [full, low], energies, slot_hours=hours, reserve_soc_pct=reserve
    )
    after_pinned = {
        config.id: energies[config.id] + (power / 1000.0) * hours / config.eta_discharge
        for config, power in pinned
    }
    assert abs(after_pinned["b_low"] - 0.165) < 1e-9
    assert after_pinned["a_full"] >= 1.5 - 1e-9
    # both batteries gave every watt of their own room (2000 W + 114 W), and no more
    assert abs(sum(power for _, power in pinned) + 2114.0) < 1e-6
    # and the floor is not a tax on charging: the same fleet may still be filled up
    charged = allocate_power(
        500.0, [full, low], energies, slot_hours=hours, reserve_soc_pct=reserve
    )
    assert abs(sum(power for _, power in charged) - 500.0) < 1e-6


def test_allocation_never_drives_any_battery_below_its_own_floor():
    """Randomised property: the reserve floor holds for every battery, not just the aggregate."""
    rng = random.Random(20261009)
    hours = 0.25
    checked = 0
    for _ in range(400):
        reserve = rng.choice([0.0, 10.0, 30.0, 60.0])
        configs = []
        energies: dict[str, float] = {}
        for index in range(rng.randint(1, 4)):
            capacity = round(rng.uniform(0.5, 8.0), 3)
            min_soc = rng.choice([0.0, 5.0, 10.0, 25.0])
            config = battery(
                id=f"b{index}",
                capacity_kwh=capacity,
                min_soc_pct=min_soc,
                max_charge_w=rng.choice([500.0, 1000.0, 3000.0]),
                max_discharge_w=rng.choice([500.0, 1000.0, 3000.0]),
            )
            configs.append(config)
            floor = capacity * max(min_soc, reserve) / 100.0
            ceiling = capacity * config.max_soc_pct / 100.0
            # some batteries start *below* their floor (they must not be pushed further down)
            energies[config.id] = rng.uniform(max(0.0, floor - 0.3 * capacity), ceiling)
        request = rng.choice([0.0, 250.0, -250.0, 1000.0, -1000.0, 5000.0, -5000.0])
        entries = allocate_power(
            request, configs, energies, slot_hours=hours, reserve_soc_pct=reserve
        )
        assert len(entries) == len(configs)
        assert abs(sum(power for _, power in entries)) <= abs(request) + 1e-6
        for config, power in entries:
            assert -config.max_discharge_w - 1e-9 <= power <= config.max_charge_w + 1e-9
            if power < 0:
                after = energies[config.id] + (power / 1000.0) * hours / config.eta_discharge
            else:
                after = energies[config.id] + (power / 1000.0) * hours * config.eta_charge
            floor = config.capacity_kwh * max(config.min_soc_pct, reserve) / 100.0
            ceiling = config.capacity_kwh * config.max_soc_pct / 100.0
            assert after <= ceiling + 1e-9, (config.id, after, ceiling)
            if energies[config.id] >= floor - 1e-9:
                assert after >= floor - 1e-9, (request, config.id, energies[config.id], after, floor)
                checked += 1
            else:
                assert after >= energies[config.id] - 1e-9, (config.id, after, energies[config.id])
    assert checked > 300, f"too few batteries started above their floor: {checked}"


def test_allocate_zero_and_unknown_sum_are_safe():
    config = battery(id="a", capacity_kwh=4.0, max_charge_w=1000.0)
    assert allocate_power(0.0, [config], {"a": 2.0})[0][1] == 0.0
    power = allocate_power(500.0, [config], {})[0][1]  # unknown SoC -> floor, may still charge
    assert 0.0 <= power <= 500.0


def test_summary_reports_the_dp_objective_with_cycle_and_spread_costs():
    """WP5 nit: cost/baseline/saved stay grid-only; objective_eur is what the DP minimised.

    The instance is grid-representable (1 kWh steps, 0.25 kWh state grid, eta 1.0), so the DP's
    internal value function at the start state and the reconstructed schedule agree exactly.
    """
    config = battery(
        capacity_kwh=4.0,
        min_soc_pct=0.0,
        max_charge_w=1000.0,
        max_discharge_w=1000.0,
        eta_charge=1.0,
        eta_discharge=1.0,
    )
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15,
        soc_step_kwh=0.25,
        power_steps=2,
        salvage_multiplier=1.0,
        wear_eur_per_kwh=0.05,
        min_spread_eur_per_kwh=0.05,
    )
    slots = make_slots(8, 15, pv=0.0, load=1.2, buy=[0.05] * 4 + [0.40] * 4, sell=0.02)
    plan = dp.solve(slots, aggregate=agg, start_kwh=2.0, options=opts, generated_at=T0)
    assert plan.summary.cycle_cost_eur > 0.0, "expected the schedule to cycle the battery"
    assert plan.summary.charged_kwh > 0.0 and plan.summary.discharged_kwh > 0.0

    # Independent recomputation from the returned schedule: grid + spread + cycle - salvage.
    dt = opts.slot_minutes / 60.0
    grid_cost = sum(slot.cost_eur(dt) for slot in plan.slots)
    spread_cost = 0.0
    cycle_cost = 0.0
    energy = None
    for slot in plan.slots:
        power = slot.power_w / 1000.0
        energy = dp.next_energy_kwh(slot.soc_kwh, power, dt, agg)
        cycle_cost += dp.wear_cost_eur(opts.wear_eur_per_kwh, agg.eta_discharge, slot.soc_kwh, energy)
        surplus = max(0.0, slot.pv_kw - slot.load_kw)
        if power > 0:
            spread_cost += opts.min_spread_eur_per_kwh * max(0.0, power - surplus) * dt
    assert energy is not None
    ordered_buy = sorted(s.buy for s in slots)
    middle = len(ordered_buy) // 2
    salvage = (
        opts.salvage_multiplier
        * (ordered_buy[middle - 1] + ordered_buy[middle]) / 2.0
        * agg.eta_discharge  # stored energy only delivers eta_dis AC (SPEC 8.1)
    )
    expected_objective = grid_cost + spread_cost + cycle_cost - salvage * energy

    # grid-only money figures are unchanged by the wear/spread terms
    assert abs(plan.summary.cost_estimate_eur - grid_cost) < 1e-9
    assert abs(plan.summary.saved_eur - (plan.summary.baseline_cost_eur - grid_cost)) < 1e-9
    assert plan.summary.cycle_cost_eur == pytest.approx(cycle_cost, abs=1e-9)
    # wear is per kWh delivered, so the total is exactly wear x discharged AC energy
    assert plan.summary.cycle_cost_eur == pytest.approx(
        opts.wear_eur_per_kwh * plan.summary.discharged_kwh, abs=1e-9
    )
    assert plan.summary.objective_eur == pytest.approx(expected_objective, abs=1e-6)
    assert abs(plan.summary.saved_eur - plan.summary.objective_eur) > 1e-3  # never conflated

    # ...and it is the DP's own minimised value, not a number re-derived from the money fields
    internal = dp._solve_once(slots, agg, 2.0, opts, dp.TARGET_HARD_PENALTY, None, salvage)
    assert internal.objective_eur == pytest.approx(plan.summary.objective_eur, abs=1e-9)
    assert internal.internal_objective_eur == pytest.approx(plan.summary.objective_eur, abs=1e-9)


def test_a_binding_grid_limit_reports_the_real_direction():
    """Audit minor: ``grid_limit_hold`` must not label a non-zero power as ``hold``."""
    config = battery(capacity_kwh=4.0, min_soc_pct=0.0, max_charge_w=2000.0, max_discharge_w=2000.0)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.25, power_steps=4, max_import_kw=0.5, allow_export=True
    )
    slots = make_slots(4, 15, pv=0.0, load=2.0, buy=0.4)
    plan = dp.solve(slots, aggregate=agg, start_kwh=3.0, options=opts, generated_at=T0)
    for slot in plan.slots:
        if slot.reason == Reason.GRID_LIMIT_HOLD:
            assert slot.action is not Action.HOLD, (slot.action, slot.power_w)
            if slot.power_w > 0:
                assert slot.action is Action.CHARGE
            elif slot.power_w < 0:
                assert slot.action is Action.DISCHARGE


def test_no_export_driven_discharge_without_a_sell_price():
    """Audit minor: a zero/unknown sell price must never justify exporting energy."""
    config = battery(capacity_kwh=4.0, min_soc_pct=0.0, max_charge_w=2000.0, max_discharge_w=2000.0)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = dp.OptimizerOptions(
        slot_minutes=15, soc_step_kwh=0.25, power_steps=4, allow_export=True, salvage_multiplier=0.0
    )
    slots = make_slots(4, 15, pv=0.0, load=0.0, buy=0.4, sell=0.0)
    plan = dp.solve(slots, aggregate=agg, start_kwh=3.0, options=opts, generated_at=T0)
    for slot in plan.slots:
        assert slot.grid_kw() >= -1e-6, (slot.power_w, slot.grid_kw())
    # an unknown sell price behaves the same way
    slots = [
        replace(slot, sell=None) for slot in make_slots(4, 15, pv=0.0, load=0.0, buy=0.4, sell=0.0)
    ]
    plan = dp.solve(slots, aggregate=agg, start_kwh=3.0, options=opts, generated_at=T0)
    for slot in plan.slots:
        assert slot.grid_kw() >= -1e-6


def test_complexity_guard_coarsens_the_state_grid(monkeypatch):
    """SPEC 8.2: slots x states x actions stays inside the iteration budget."""
    minutes = 15
    config = battery(capacity_kwh=20.0, max_charge_w=5000.0, max_discharge_w=5000.0)
    opts = dp.OptimizerOptions(slot_minutes=minutes, soc_step_kwh=0.01, power_steps=30)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = make_slots(96, minutes, pv=0.0, load=0.5, buy=0.3, sell=0.05)
    monkeypatch.setattr(dp, "MAX_ITERATIONS", 200_000)
    plan = dp.solve(slots, aggregate=agg, start_kwh=2.0, options=opts, generated_at=T0)
    assert plan.summary.solver_iterations <= 200_000
    states = max(1, round(agg.usable_kwh / plan.summary.effective_step_kwh)) + 1
    assert states * 31 * len(slots) >= plan.summary.solver_iterations
    # with the default budget the same instance still fits without coarsening below 400 states
    monkeypatch.setattr(dp, "MAX_ITERATIONS", 2_000_000)
    detailed = dp.solve(slots, aggregate=agg, start_kwh=2.0, options=opts, generated_at=T0)
    assert detailed.summary.effective_step_kwh <= plan.summary.effective_step_kwh
    assert detailed.summary.solver_iterations <= 2_000_000


def test_frozen_plan_slot_cost_matches_the_optimizer():
    """SPEC 8.1 regression: the frozen ``PlanSlot.cost_eur`` and the DP must never diverge.

    The helper used to carry the inverted sign of the old spec text; if either side changes its
    convention again, the reported ``cost_estimate_eur`` and the shipped helper disagree and this
    test fails instead of silently corrupting every savings number.
    """
    rng = random.Random(7)
    for _ in range(5):
        minutes = 60
        opts = dp.OptimizerOptions(slot_minutes=minutes, horizon_hours=12, soc_step_kwh=0.5, power_steps=4)
        config = battery()
        agg = dp.BatteryAggregate.from_configs([config], 0.0)
        slots = make_slots(
            8,
            minutes,
            pv=[round(rng.uniform(0.0, 1.5), 3) for _ in range(8)],
            load=[round(rng.uniform(0.2, 1.2), 3) for _ in range(8)],
            buy=[round(rng.uniform(0.1, 0.5), 3) for _ in range(8)],
            sell=[round(rng.uniform(0.02, 0.2), 3) for _ in range(8)],
        )
        plan = dp.solve(slots, aggregate=agg, start_kwh=1.0, options=opts, generated_at=T0)
        dt = minutes / 60.0
        assert abs(sum(slot.cost_eur(dt) for slot in plan.slots) - plan.summary.cost_estimate_eur) < 1e-12
        # the grid flow sign: charging must *raise* grid import, discharging lowers it
        for slot in plan.slots:
            assert abs(slot.grid_kw() - (slot.load_kw - slot.pv_kw + slot.power_w / 1000.0)) < 1e-12
        for slot in plan.slots:
            grid = slot.grid_kw()
            expected = dt * (slot.buy * max(grid, 0.0) - slot.sell * max(-grid, 0.0))
            assert abs(slot.cost_eur(dt) - expected) < 1e-12
        charging = [slot for slot in plan.slots if slot.power_w > 0]
        if charging:
            slot = charging[0]
            assert slot.grid_kw() > slot.load_kw - slot.pv_kw  # charging raises grid import
        discharging = [slot for slot in plan.slots if slot.power_w < 0]
        if discharging:
            slot = discharging[0]
            assert slot.grid_kw() < slot.load_kw - slot.pv_kw  # discharging lowers it


def test_plan_slots_carry_soc_pct_and_sources():
    """SPEC 10: the optimiser fills ``soc_pct``, ``pv_source`` and ``price_source`` itself."""
    minutes = 15
    opts = dp.OptimizerOptions(slot_minutes=minutes, horizon_hours=12)
    config = battery(capacity_kwh=4.0, min_soc_pct=25.0, max_soc_pct=100.0)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = [
        dp.SlotInput(
            t=T0 + timedelta(minutes=minutes * i),
            pv_kw=0.5,
            load_kw=0.4,
            buy=0.2,
            sell=0.05,
            price_source="example_price",
            pv_source="example_solar",
        )
        for i in range(8)
    ]
    plan = dp.solve(slots, aggregate=agg, start_kwh=2.0, options=opts, generated_at=T0)
    for slot in plan.slots:
        assert slot.price_source == "example_price"
        assert slot.pv_source == "example_solar"
        assert slot.soc_pct is not None
        assert abs(slot.soc_pct - slot.soc_kwh / 4.0 * 100.0) < 0.02  # capacity-weighted mean
    assert plan.slots[-1].soc_pct == round(plan.slots[-1].soc_kwh / 4.0 * 100.0, 2)
    # a battery-less plan has no meaningful SoC percentage
    empty = dp.solve(slots, aggregate=dp.BatteryAggregate.from_configs([], 0.0), start_kwh=0.0, options=opts)
    assert all(slot.soc_pct is None for slot in empty.slots)


def test_align_slots_propagates_the_provider_names():
    price = PriceForecast(
        slots=[PriceSlot(t=T0 + timedelta(minutes=15 * i), buy=0.2, sell=0.05, source="example_price") for i in range(8)],
        generated_at=T0,
        source="example_price",
    )
    solar = SolarForecast(
        slots=[SolarSlot(t=T0 + timedelta(minutes=15 * i), pv_kw=0.3) for i in range(8)],
        generated_at=T0,
        source="example_solar",
    )
    load = LoadForecast(
        slots=[LoadSlot(t=T0 + timedelta(minutes=15 * i), load_kw=0.4) for i in range(8)],
        generated_at=T0,
        source="example_load",
    )
    aligned = dp.align_slots(price, solar, load, T0, T0 + timedelta(hours=2), 15)
    assert all(slot.price_source == "example_price" for slot in aligned)
    assert all(slot.pv_source == "example_solar" for slot in aligned)


def test_optimizer_options_default_the_horizon_to_the_shipped_setting():
    """SPEC 3: ``optimizer.horizon_hours`` defaults to 30 h, so the dataclass default may not say 36.

    Settings always supply the key in production, so a stale 36 only bites an install (or a test)
    that builds options without it -- exactly the kind of dead default that silently diverges from
    SPEC.  The setting still wins whenever it is present.
    """
    assert dp.OptimizerOptions().horizon_hours == 30
    assert dp.OptimizerOptions.from_settings({}).horizon_hours == 30
    assert dp.OptimizerOptions.from_settings({"optimizer.horizon_hours": 24}).horizon_hours == 24
    assert dp.OptimizerOptions.from_settings({"optimizer.horizon_hours": 48}).horizon_hours == 48
    # and the shipped default (config.DEFAULTS, SPEC 3) agrees with the optimiser fallback
    from app.config import DEFAULTS

    assert DEFAULTS["optimizer.horizon_hours"] == dp.OptimizerOptions().horizon_hours


def test_the_plan_grid_defaults_to_the_price_resolution():
    """SPEC 3/4: the plan grid defaults to 60 min, and every copy of that default agrees.

    An hourly tariff on a 15-minute plan grid leaves three of every four slots forward-filled and
    flagged stale, and the optimiser treats a stale price as flat (SPEC 4), so the shipped default
    must match the tariff granularity a household actually has. The dataclass default, the
    ``from_settings`` fallback and ``config.DEFAULTS`` are the three places that could drift.
    """
    from app.config import DEFAULTS

    assert dp.OptimizerOptions().slot_minutes == 60
    assert dp.OptimizerOptions.from_settings({}).slot_minutes == 60
    assert DEFAULTS["optimizer.slot_minutes"] == dp.OptimizerOptions().slot_minutes
    # the setting still wins when present, and the documented bounds still hold
    assert dp.OptimizerOptions.from_settings({"optimizer.slot_minutes": 15}).slot_minutes == 15
    assert dp.OptimizerOptions.from_settings({"optimizer.slot_minutes": 5}).slot_minutes == 5
    metadata = _setting_metadata("optimizer.slot_minutes")
    assert (metadata.minimum, metadata.maximum) == (5, 60)
    assert metadata.advanced is True


def _setting_metadata(key: str):
    from app.config import SETTINGS_META

    return SETTINGS_META[key]
