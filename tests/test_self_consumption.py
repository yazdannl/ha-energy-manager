"""Zero-export self-consumption planning when no price resolves (SPEC 8.31, 0.34.0).

Two layers are proved here:

* the planner itself (``app.optimize.selfconsumption``) -- no export while the pack has room, the
  SoC floor and ceiling are respected, and the horizon is reported as *unpriced* rather than free;
* the engine path -- a price provider that cannot resolve still yields a dispatched plan, and the
  dispatcher executes it even though every slot's price is marked stale.
"""

from __future__ import annotations

import os
import sys

import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager")
)

from app.control.dispatcher import Dispatcher  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import (  # noqa: E402
    LoadSlot,
    PriceForecast,
    SolarSlot,
    _FakeHA,
    _FakeStore,
    _selfcheck_settings,
    _selfcheck_sources,
)
from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    LoadForecast,
    Plan,
    PlanSlot,
    Reason,
    SolarForecast,
    utcnow,
)
from app.optimize import selfconsumption  # noqa: E402

NOW = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

EXAMPLE_BATTERY = BatteryConfig(
    id="example_battery",
    name="Example Battery",
    driver="generic",
    capacity_kwh=5.0,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
    max_charge_w=2000.0,
    max_discharge_w=2000.0,
    eta_charge=0.95,
    eta_discharge=0.95,
    mapping={
        "power_setpoint_entity": "number.example_battery_power",
        "soc_entity": "sensor.example_battery_soc",
    },
    enabled=True,
)


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _settings(**overrides):
    values = {
        "optimizer.slot_minutes": 15,
        "optimizer.horizon_hours": 12,
        "optimizer.reserve_soc_pct": 0.0,
    }
    values.update(overrides)
    return _Settings(values)


def _solar(profile, now=NOW):
    """``profile(index) -> kW`` over 96 quarter-hour slots starting at ``now``."""
    forecast = SolarForecast(source="test", generated_at=now)
    start = now.replace(minute=0, second=0, microsecond=0)
    for index in range(96):
        forecast.slots.append(
            SolarSlot(t=start + timedelta(minutes=15 * index), pv_kw=profile(index))
        )
    return forecast


def _load(value=0.4, now=NOW, slots=96):
    forecast = LoadForecast(source="test", generated_at=now, confidence="high")
    start = now.replace(minute=0, second=0, microsecond=0)
    for index in range(slots):
        forecast.slots.append(
            LoadSlot(
                t=start + timedelta(minutes=15 * index),
                load_kw=value,
                p10_kw=value * 0.7,
                p90_kw=value * 1.3,
            )
        )
    return forecast


def _plan(solar, load, start_kwh=2.5, **overrides):
    return selfconsumption.plan_self_consumption(
        solar=solar,
        load=load,
        battery_configs=[EXAMPLE_BATTERY],
        start_kwh=start_kwh,
        settings=_settings(**overrides),
        now=NOW,
    )


# --------------------------------------------------------------------------------- the planner


def test_plan_preserves_a_per_slot_live_pv_source():
    solar = _solar(lambda i: 1.0)
    solar.slots[0].source = "live_measurement"
    plan = _plan(solar, _load())
    assert plan.slots[0].pv_source == "live_measurement"
    assert plan.slots[1].pv_source == "test"


def test_every_surplus_kwh_is_caught_and_nothing_is_exported():
    """A short 2 kW rooftop burst against 0.4 kW of load: charge 1.6 kW, never reach the meter."""
    plan = _plan(_solar(lambda i: 2.0 if 8 <= i < 12 else 0.0), _load(0.4))
    charging = [slot for slot in plan.slots if slot.action == Action.PV_CHARGE]
    assert charging, "a surplus must charge the pack"
    assert charging[0].reason == Reason.PV_SURPLUS_CHARGE
    assert charging[0].power_w == 1600  # the surplus, not the 2000 W charge limit
    # Zero export while the pack has room, and zero import to charge (SPEC 8.31).
    assert min(slot.grid_kw() for slot in plan.slots) >= -1e-6
    assert plan.summary.grid_charge_kwh == 0.0
    assert plan.summary.pv_charge_kwh == pytest.approx(plan.summary.charged_kwh)


def test_a_surplus_only_leaves_once_the_pack_is_full():
    """Long 2 kW surplus against a 5 kWh pack: after the ceiling there is nothing left to catch."""
    plan = _plan(_solar(lambda i: 2.0 if 8 <= i < 40 else 0.0), _load(0.4))
    ceiling = EXAMPLE_BATTERY.capacity_kwh
    for slot in plan.slots:
        if slot.grid_kw() < -1e-6:
            assert slot.soc_kwh == pytest.approx(ceiling), slot.t
    # ... and everything the pack *could* hold was taken: it reaches its ceiling.
    assert max(slot.soc_kwh for slot in plan.slots) == pytest.approx(ceiling)


def test_the_pack_serves_the_house_without_going_below_the_floor():
    """No sun: discharge at the load, stop at ``min_soc`` instead of emptying the pack."""
    plan = _plan(_solar(lambda i: 0.0), _load(1.0), start_kwh=5.0)
    discharging = [slot for slot in plan.slots if slot.action == Action.SELF_CONSUMPTION]
    assert discharging, "a deficit must be served from the pack"
    assert discharging[0].power_w == -1000  # the load, not the 2000 W discharge limit
    # The pack drains to its floor and then holds: no discharge below ``min_soc_pct`` (0.34.0).
    assert min(slot.soc_kwh for slot in plan.slots) == pytest.approx(
        EXAMPLE_BATTERY.capacity_kwh * 0.10
    )
    assert any(slot.reason == Reason.RESERVE_HOLD for slot in plan.slots)


def test_a_pack_at_its_floor_delivers_only_what_it_may():
    """0.05 kWh above the floor is 190 W over a quarter hour, not the whole 1 kW of load."""
    plan = _plan(_solar(lambda i: 0.0), _load(1.0), start_kwh=0.55)
    assert plan.slots[0].power_w == -190
    assert plan.slots[1].reason == Reason.RESERVE_HOLD
    assert all(slot.soc_pct is None or slot.soc_pct >= 10.0 for slot in plan.slots)


def test_a_full_pack_stops_charging_and_lets_the_surplus_go():
    """At the ceiling there is nothing left to catch: hold, and the export is unavoidable."""
    plan = _plan(_solar(lambda i: 3.0), _load(0.2), start_kwh=5.0)
    assert plan.slots[0].action == Action.HOLD
    assert plan.slots[0].reason == Reason.FULL
    assert all(slot.power_w == 0 for slot in plan.slots)
    assert plan.summary.charged_kwh == 0.0


def test_the_horizon_is_reported_as_unpriced_never_as_free():
    plan = _plan(_solar(lambda i: 1.0), _load(0.4))
    assert plan.sources["plan"] == selfconsumption.PLAN_SOURCE
    assert plan.sources["price"] == "none"
    assert selfconsumption.is_self_consumption_plan(plan) is True
    assert all(slot.price_source == "none" for slot in plan.slots)
    assert all(slot.price_stale for slot in plan.slots)
    # 0.0 here means "not priced"; the plan claims no cost and no saving (SPEC 8.31).
    assert plan.summary.cost_estimate_eur == 0.0
    assert plan.summary.saved_eur == 0.0
    assert plan.summary.sell_floor_eur_per_kwh is None


def test_the_plan_needs_both_forecasts():
    with pytest.raises(ValueError):
        _plan(_solar(lambda i: 1.0), None)
    with pytest.raises(ValueError):
        _plan(None, _load())


def test_the_horizon_follows_the_optimizer_grid():
    plan = _plan(_solar(lambda i: 1.0), _load(), **{"optimizer.slot_minutes": 60})
    assert plan.slot_minutes == 60
    assert len(plan.slots) == 12  # 12 h horizon at one slot per hour
    assert plan.horizon_start == NOW.replace(minute=0, second=0, microsecond=0)


# -------------------------------------------------------------------------------- the dispatcher


def _slot(t, power_w=0.0, action=Action.HOLD, reason=Reason.SELF_CONSUMPTION, **kwargs):
    return PlanSlot(
        t=t,
        buy=kwargs.pop("buy", 0.2),
        sell=kwargs.pop("sell", 0.05),
        pv_kw=kwargs.pop("pv_kw", 0.0),
        load_kw=kwargs.pop("load_kw", 0.0),
        soc_kwh=kwargs.pop("soc_kwh", 2.5),
        power_w=power_w,
        action=action,
        reason=reason,
        **kwargs,
    )


def _plan_at(t, price_stale=False, sources=None, power_w=800.0):
    return Plan(
        generated_at=t,
        horizon_start=t,
        horizon_end=t + timedelta(hours=1),
        slot_minutes=15,
        slots=[_slot(t, power_w=power_w, price_stale=price_stale)],
        sources=dict(sources or {}),
    )


def _dispatcher():
    journal = _FakeStore()
    return Dispatcher({"control.tick_seconds": 30, "control.replan_minutes": 15}, journal, {})


def test_a_stale_price_refuses_a_priced_plan():
    plan = _plan_at(NOW, price_stale=True)
    assert _dispatcher()._stale_input_reason(plan, plan.slots[0], NOW + timedelta(minutes=1))


def test_a_self_consumption_plan_is_executed_despite_its_unpriced_slots():
    plan = _plan_at(
        NOW, price_stale=True, sources={"plan": selfconsumption.PLAN_SOURCE, "price": "none"}
    )
    assert _dispatcher()._stale_input_reason(plan, plan.slots[0], NOW + timedelta(minutes=1)) is None


# ------------------------------------------------------------------------------------ the engine


class _Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True

    def test(self):
        self.sent.append(("test", "", ""))
        return True


def _unpriced_engine():
    """An engine whose price provider cannot resolve, but whose PV and load forecasts are fine."""
    sources = _selfcheck_sources()

    def failing_price(now):
        raise RuntimeError("price forecast unavailable (zonneplan_entity: no tariff sensor)")

    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    store = _FakeStore()
    ha = _FakeHA()
    ha.states["sensor.example_battery_soc"] = {
        "state": "50",
        "attributes": {"unit_of_measurement": "%"},
    }
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=_Notifier(),
        clock=_Clock(),
        version="test",
        commit="test",
        price_source=failing_price,
        solar_source=sources["solar_source"],
        load_source=sources["load_source"],
        sampler=sources["sampler"],
    )
    return engine, store, ha


def test_the_engine_plans_zero_export_when_no_price_resolves():
    engine, _store, _ha = _unpriced_engine()
    plan = engine.plan_now("test")
    assert plan is not None, "no price must not mean no plan any more"
    assert selfconsumption.is_self_consumption_plan(plan)
    assert plan.feasible and plan.slots
    assert any(slot.action == Action.PV_CHARGE for slot in plan.slots)
    # Published (0.34.0): the schedule is dispatched, not merely computed for the caller.
    active = engine.active_plan()
    assert active is not None and selfconsumption.is_self_consumption_plan(active)
    codes = [entry["code"] for entry in engine.log(limit=50, kind="event")]
    assert "plan_updated" in codes
    assert any(alert.code == Reason.NO_PRICE_SELF_CONSUMPTION for alert in engine.snapshot().alerts)


def test_the_engine_dispatch_follows_the_self_consumption_target():
    engine, _store, ha = _unpriced_engine()
    tick = engine.tick_now()
    # 11:00 on the selfcheck clock: 2 kW of PV against 0.8 kW of load -> a 1.2 kW charge target.
    assert tick.target_w > 500, tick
    assert not any(call[0] == "number" for call in ha.service_calls)  # advisory: never writes


def test_a_priced_engine_is_unaffected():
    """The fallback only takes over when the price is missing (no price, no costed plan)."""
    sources = _selfcheck_sources()
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    ha = _FakeHA()
    ha.states["sensor.example_battery_soc"] = {
        "state": "50",
        "attributes": {"unit_of_measurement": "%"},
    }
    engine = Engine(
        settings,
        store=_FakeStore(),
        ha=ha,
        notifier=_Notifier(),
        clock=_Clock(),
        version="test",
        commit="test",
        **_selfcheck_sources(),
    )
    plan = engine.plan_now("test")
    assert plan is not None
    assert not selfconsumption.is_self_consumption_plan(plan)
    assert plan.summary.cost_estimate_eur != 0.0
