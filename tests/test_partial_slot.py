"""0.20.1 (SPEC 8.16): the current (partial) slot is modelled with the time it really has.

The live case (2026-10-13, plan made 05:16Z): ``solve_from_forecasts`` floors the horizon to the
slot grid, so the 05:00 slot has only 44 of its 60 minutes left, yet the pre-0.20.1 ``solve`` used
one uniform ``slot_minutes`` hour for every slot.  The plan then promised energy the dispatcher
could not move inside the clock slot -- the live pack stopped at 37 % at 06:58Z instead of reaching
the planned 15 % at 07:00, leaving energy above the floor unsold -- and every later slot's price
covered a window shifted by the elapsed 16 minutes.  These tests are shaped on the reproducer of
that plan and fail at ``6fbf4a2``.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app import api  # noqa: E402
from app.engine import _execution_progress  # noqa: E402
from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PlanSlot,
    PriceForecast,
    PriceSlot,
    Reason,
    SolarForecast,
    SolarSlot,
)
from app.optimize import dp  # noqa: E402

UTC = timezone.utc

#: The live plan's clock: made at 05:16Z, horizon floored to the 05:00 slot (44 minutes left).
LIVE_NOW = datetime(2026, 10, 13, 5, 16, tzinfo=UTC)
LIVE_START = datetime(2026, 10, 13, 5, 0, tzinfo=UTC)
CAPACITY_KWH = 1.92
ETA = 0.92

#: Battery and settings of the live instance (SolarFlow 800 Plus, 15 % floor, cost-basis floor).
SETTINGS = {
    "optimizer.slot_minutes": 60,
    "optimizer.horizon_hours": 30,
    "optimizer.soc_step_kwh": 0.02,
    "optimizer.power_steps": 24,
    "optimizer.allow_export": True,
    "optimizer.allow_grid_charge": True,
    "optimizer.cycle_cost_eur_per_kwh": 0.0,
    "optimizer.salvage_multiplier": 1.0,
    "optimizer.min_spread_eur_per_kwh": 0.0,
    "optimizer.reserve_soc_pct": 0.0,
}
#: The live plan's cost-basis sell floor and the two peak-slot prices in EUR/kWh (4 decimals, as
#: the API/UI state them).
LIVE_SELL_FLOOR = 0.3997
LIVE_BUY = (0.4095, 0.4136, 0.3564, 0.3361, 0.2989, 0.2797, 0.2660, 0.2620)
LIVE_PV = (0.0, 0.0, 0.53, 1.07, 1.50, 1.81, 2.01, 2.02)
LIVE_LOAD = (0.57, 0.71, 0.83, 0.77, 0.65, 0.59, 0.59, 0.59)


def live_battery(**overrides) -> BatteryConfig:
    data = dict(
        id="solarflow_800_plus",
        name="Solarflow",
        driver="zendure",
        capacity_kwh=CAPACITY_KWH,
        min_soc_pct=15.0,
        max_soc_pct=100.0,
        max_charge_w=1000.0,
        max_discharge_w=800.0,
        eta_charge=ETA,
        eta_discharge=ETA,
    )
    data.update(overrides)
    return BatteryConfig(**data)


def live_forecasts() -> tuple[PriceForecast, SolarForecast, LoadForecast]:
    price, solar, load = PriceForecast(source="test"), SolarForecast(source="test"), LoadForecast(source="test")
    for i, buy in enumerate(LIVE_BUY):
        moment = LIVE_START + timedelta(hours=i)
        price.slots.append(PriceSlot(t=moment, buy=buy, sell=buy))
        solar.slots.append(SolarSlot(t=moment, pv_kw=LIVE_PV[i]))
        load.slots.append(LoadSlot(t=moment, load_kw=LIVE_LOAD[i]))
    return price, solar, load


def live_plan(now: datetime = LIVE_NOW):
    price, solar, load = live_forecasts()
    return dp.solve_from_forecasts(
        price,
        solar,
        load,
        battery_configs=[live_battery()],
        start_kwh=0.85 * CAPACITY_KWH,
        settings=SETTINGS,
        now=now,
        maintenance=None,
        sell_floor_eur_per_kwh=LIVE_SELL_FLOOR,
    )


def ac_energy_kwh(slot: PlanSlot) -> float:
    """The AC energy this slot really moves: its own modelled duration, not the grid step."""
    return abs(slot.power_w) / 1000.0 * slot.minutes / 60.0


def hourly_slots(start: datetime, prices=(0.40, 0.20, 0.41, 0.10)) -> list[dp.SlotInput]:
    return [
        dp.SlotInput(t=start + timedelta(hours=i), pv_kw=0.0, load_kw=0.5, buy=price, sell=price)
        for i, price in enumerate(prices)
    ]


def test_live_first_slot_is_modelled_with_its_remaining_minutes():
    plan = live_plan()
    # The timestamp grid stays clock-aligned; only the duration shrinks to 05:16 -> 06:00.
    assert plan.slots[0].t == LIVE_START
    assert plan.slots[0].minutes == 44.0
    assert all(slot.minutes == 60.0 for slot in plan.slots[1:])
    assert plan.horizon_start == LIVE_START


def test_live_schedule_matches_what_the_real_windows_can_move():
    plan = live_plan()
    first, second = plan.slots[0], plan.slots[1]
    # The 0.20.0 plan put 461.2 W in this slot because it believed a full hour; the 44 minutes it
    # really has can carry more of the energy deferred out of the pricey window.
    assert first.power_w < -500.0
    assert second.power_w < 0.0

    # Everything above the 15 % floor is scheduled inside the real windows (one 0.02 kWh SoC step
    # of slack), so the pack can actually reach the floor at the 07:00 clock boundary.
    available = (0.85 - 0.15) * CAPACITY_KWH * ETA
    sold = ac_energy_kwh(first) + ac_energy_kwh(second)
    assert sold == pytest.approx(available, abs=0.02)

    # Recompute the SoC trajectory over the modelled minutes: it matches the plan's own soc_kwh
    # and lands on the floor at the end of the second peak slot instead of the phantom "15 % at
    # 07:00" the full-hour model reported while the clock stopped at 37 %.
    aggregate = dp.BatteryAggregate.from_configs([live_battery()])
    energy = 0.85 * CAPACITY_KWH
    for slot in (first, second):
        assert slot.soc_kwh == pytest.approx(energy, abs=0.02)
        energy = dp.next_energy_kwh(energy, slot.power_w / 1000.0, slot.minutes / 60.0, aggregate)
    assert energy == pytest.approx(aggregate.e_min_kwh, abs=0.02)
    assert plan.slots[2].soc_pct == pytest.approx(15.0, abs=1.0)


def test_live_summary_money_uses_the_modelled_minutes():
    plan = live_plan()
    first, second = plan.slots[0], plan.slots[1]
    # The summary's money is the schedule's grid cost over the same durations the solver used.
    by_modelled_minutes = sum(slot.cost_eur(slot.minutes / 60.0) for slot in plan.slots)
    by_full_slots = sum(slot.cost_eur(plan.slot_minutes / 60.0) for slot in plan.slots)
    assert plan.summary.cost_estimate_eur == pytest.approx(by_modelled_minutes, abs=1e-9)
    assert plan.summary.cost_estimate_eur != pytest.approx(by_full_slots, abs=1e-6)
    assert plan.summary.discharged_kwh == pytest.approx(
        ac_energy_kwh(first) + ac_energy_kwh(second), abs=1e-9
    )


def test_one_slot_money_uses_the_remaining_minutes():
    now = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)  # half an hour into the 12:00 slot
    slot = dp.SlotInput(
        t=datetime(2026, 1, 1, 12, 0, tzinfo=UTC), pv_kw=0.0, load_kw=0.5, buy=0.30, sell=0.20
    )
    plan = dp.solve(
        [slot],
        aggregate=dp.BatteryAggregate.from_configs([]),
        start_kwh=0.0,
        options=dp.OptimizerOptions.from_settings(SETTINGS),
        generated_at=now,
        now=now,
    )
    assert plan.slots[0].minutes == 30.0
    # Half an hour of 0.5 kW import at 0.30 EUR/kWh, hand-computed in both directions.
    assert plan.summary.cost_estimate_eur == pytest.approx(0.30 * 0.5 * 0.5, abs=1e-12)
    assert plan.summary.baseline_cost_eur == pytest.approx(0.30 * 0.5 * 0.5, abs=1e-12)
    assert plan.summary.saved_eur == pytest.approx(0.0, abs=1e-12)
    # The full-hour model would have booked twice that money for the same slot.
    assert plan.summary.cost_estimate_eur != pytest.approx(0.30 * 0.5 * 1.0, abs=1e-12)


def test_now_on_a_slot_boundary_is_unchanged():
    slots = hourly_slots(LIVE_START)
    options = dp.OptimizerOptions.from_settings(SETTINGS)
    aggregate = dp.BatteryAggregate.from_configs([live_battery()])
    uniform = dp.solve(
        slots, aggregate=aggregate, start_kwh=1.0, options=options, generated_at=LIVE_START
    )
    boundary = dp.solve(
        slots,
        aggregate=aggregate,
        start_kwh=1.0,
        options=options,
        generated_at=LIVE_START,
        now=LIVE_START,
    )
    assert [slot.minutes for slot in boundary.slots] == [60.0] * len(slots)
    assert [slot.power_w for slot in boundary.slots] == [slot.power_w for slot in uniform.slots]
    assert [slot.soc_kwh for slot in boundary.slots] == [slot.soc_kwh for slot in uniform.slots]
    assert boundary.summary.cost_estimate_eur == uniform.summary.cost_estimate_eur


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 1, 1, 12, 59, 59, tzinfo=UTC),  # a sub-minute remainder
        datetime(2026, 1, 1, 13, 0, 1, tzinfo=UTC),  # the first slot's clock window is over
    ],
)
def test_sub_minute_remainder_stays_bounded(now: datetime):
    slots = hourly_slots(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    plan = dp.solve(
        slots,
        aggregate=dp.BatteryAggregate.from_configs([live_battery()]),
        start_kwh=0.5 * CAPACITY_KWH,
        options=dp.OptimizerOptions.from_settings(SETTINGS),
        generated_at=now,
        now=now,
    )
    assert plan.slots[0].minutes == 1.0  # never zero or negative
    assert [slot.minutes for slot in plan.slots[1:]] == [60.0, 60.0, 60.0]
    assert all(math.isfinite(slot.power_w) for slot in plan.slots)
    assert all(-1000.0 - 1e-9 <= slot.power_w <= 1000.0 + 1e-9 for slot in plan.slots)


def test_execution_views_use_the_modelled_minutes():
    slot = PlanSlot(
        t=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        buy=0.40,
        sell=0.40,
        pv_kw=0.0,
        load_kw=1.0,
        soc_kwh=1.0,
        power_w=-800.0,
        action=Action.DISCHARGE,
        reason=Reason.PEAK_DISCHARGE,
        minutes=44.0,
    )
    second = replace(slot, t=slot.t + timedelta(hours=1), minutes=60.0)
    measured = [
        {
            "coverage_pct": 100.0,
            "samples": 12,
            "charged_kwh": 0.0,
            "discharged_kwh": 0.0,
            "import_kwh": 0.0,
            "export_kwh": 0.0,
        }
    ] * 2
    progress = _execution_progress(
        [slot, second], measured, second.t + timedelta(hours=1), 60.0
    )
    # 0.8 kW for the 44-minute slot and then for a full hour, and 0.2 kW of residual import in
    # each at 0.40 EUR/kWh -- the intended side must not credit the first slot with an hour.
    assert progress["planned_discharged_kwh"] == pytest.approx(0.8 * 44 / 60 + 0.8, abs=1e-9)
    assert progress["planned_cost_eur"] == pytest.approx(
        0.2 * 0.40 * 44 / 60 + 0.2 * 0.40, abs=1e-9
    )


def test_api_serialises_the_modelled_minutes():
    plan = live_plan()
    payload = api.plan_dict(plan, LIVE_NOW)
    assert payload["slots"][0]["minutes"] == 44.0
    assert payload["slots"][1]["minutes"] == 60.0
    # A plan without the field (an older stored plan) still serialises a usable duration.
    legacy = api.plan_dict(replace(plan, slots=[replace(plan.slots[0], minutes=0.0)]), LIVE_NOW)
    assert legacy["slots"][0]["minutes"] == 60.0

    execution = api.plan_execution_dict(
        {
            "generated_at": None,
            "slot_minutes": 60,
            "now": None,
            "floors": {},
            "batteries": [],
            "progress": {},
            "slots": [
                {"t": "2026-10-13T05:00:00+00:00", "minutes": 44.0, "dispatch": None, "actual": None}
            ],
        }
    )
    assert execution["slots"][0]["minutes"] == 44.0
