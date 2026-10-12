"""Scheduled full-charge maintenance (SPEC 8.1): due-date maths, the terminal target and the API.

The pure module is exercised directly (no clock, no store), then the optimiser proves the target
only changes the schedule while a battery is due, and finally the engine proves a completion is
recorded from a fresh measurement, an overdue cycle alerts, and the disabled feature is a no-op.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app import full_charge as fc  # noqa: E402
from app.api import ApiServer, plan_dict  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import (  # noqa: E402
    _FakeHA,
    _FakeStore,
    _selfcheck_settings,
    _selfcheck_sources,
)
from app.models import BatteryConfig  # noqa: E402
from app.optimize import dp  # noqa: E402

T0 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
AMSTERDAM = ZoneInfo("Europe/Amsterdam")


class Clock:
    def __init__(self, now=None):
        self.now = now or T0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True

    def test(self):
        return True


def settings(**overrides) -> fc.FullChargeSettings:
    values = {
        "maintenance.full_charge_enabled": True,
        "maintenance.full_charge_interval_days": 30,
        "maintenance.full_charge_target_soc_pct": 100.0,
    }
    values.update(overrides)
    return fc.FullChargeSettings.from_settings(values)


def evaluate(records, *, now, soc, cfg=None, tz=None, batteries=(("b1", True),)):
    cfg = cfg or settings()
    return fc.evaluate(
        records,
        batteries=list(batteries),
        soc_by_id=soc,
        now=now,
        settings=cfg,
        tz=tz or ZoneInfo("UTC"),
    )


# ---------------------------------------------------------------------------- due-date maths


def test_due_date_keeps_the_local_wall_clock_across_a_dst_change():
    """Both directions: the deadline stays at the same local time, its offset must not drift."""
    spring = datetime(2026, 3, 1, 12, 0, tzinfo=AMSTERDAM)
    due_spring = fc.add_days_local(spring, 30, AMSTERDAM)
    assert (due_spring.year, due_spring.month, due_spring.day, due_spring.hour, due_spring.minute) == (
        2026,
        3,
        31,
        12,
        0,
    )
    # 29 March 2026 is the spring-forward; the local offset changed and the clock did not.
    assert spring.utcoffset() != due_spring.utcoffset()
    autumn = datetime(2026, 10, 1, 12, 0, tzinfo=AMSTERDAM)
    due_autumn = fc.add_days_local(autumn, 30, AMSTERDAM)
    assert (due_autumn.month, due_autumn.day, due_autumn.hour) == (10, 31, 12)
    assert autumn.utcoffset() != due_autumn.utcoffset()


def test_due_date_rolls_over_month_and_year_boundaries():
    assert fc.add_days_local(datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc), 30, ZoneInfo("UTC")).date().isoformat() == "2026-03-02"
    assert fc.add_days_local(datetime(2026, 12, 15, 9, 0, tzinfo=timezone.utc), 30, ZoneInfo("UTC")).date().isoformat() == "2027-01-14"
    # 2028 is a leap year: 30 days from 1 February lands in March, not on the 2nd.
    assert fc.add_days_local(datetime(2028, 2, 1, 9, 0, tzinfo=timezone.utc), 30, ZoneInfo("UTC")).date().isoformat() == "2028-03-02"


def test_settings_are_clamped_and_read_from_the_keys():
    parsed = fc.FullChargeSettings.from_settings(
        {
            "maintenance.full_charge_enabled": True,
            "maintenance.full_charge_interval_days": 999,
            "maintenance.full_charge_target_soc_pct": 10.0,
        }
    )
    assert parsed.enabled is True
    assert parsed.interval_days == 365
    assert parsed.target_soc_pct == 50.0
    assert fc.FullChargeSettings.from_settings({}) == fc.FullChargeSettings(False, 30, 100.0)


def test_completion_detection_uses_the_tolerance_and_never_fakes_it():
    now = T0
    for soc, done_expected in ((98.9, False), (99.0, True), (100.0, True)):
        records = {"b1": fc.BatteryRecord("b1", next_due=now - timedelta(days=1))}
        _records, _rows, _changed, completed, _alerts = evaluate(records, now=now, soc={"b1": soc})
        assert (completed == ["b1"]) is done_expected, soc
    # A missing or non-finite measurement must never complete a cycle.
    for soc in (None, float("nan")):
        records = {"b1": fc.BatteryRecord("b1", next_due=now - timedelta(days=1))}
        _records, rows, _changed, completed, _alerts = evaluate(records, now=now, soc={"b1": soc})
        assert completed == []
        assert rows[0]["soc_pct"] is None


def test_already_at_target_completes_immediately_and_schedules_the_interval():
    now = T0
    records, rows, changed, completed, _alerts = evaluate({"b1": fc.BatteryRecord("b1")}, now=now, soc={"b1": 100.0})
    assert completed == ["b1"] and changed
    record = records["b1"]
    assert record.last_completed == now
    assert record.next_due == fc.add_days_local(now, 30, ZoneInfo("UTC"))
    assert rows[0]["state"] == "scheduled"
    assert rows[0]["at_target"] is True
    # A second reading at target while the cycle is satisfied does not re-complete (no churn).
    _records2, _rows2, _changed2, completed2, _alerts2 = evaluate(records, now=now + timedelta(hours=1), soc={"b1": 100.0})
    assert completed2 == []


def test_interval_is_respected_and_the_target_applies_only_when_due():
    now = T0
    cfg = settings()
    fresh = {"b1": fc.BatteryRecord("b1")}
    _records, rows, _changed, _completed, _alerts = evaluate(fresh, now=now, soc={"b1": 55.0})
    assert rows[0]["due"] is True, "a battery with no history is due immediately"
    block = fc.plan_block(rows, cfg, horizon_end=now + timedelta(hours=30))
    assert block["active"] is True and block["batteries"] == ["b1"]

    satisfied = {
        "b1": fc.BatteryRecord(
            "b1", last_completed=now - timedelta(days=10), next_due=now + timedelta(days=20)
        )
    }
    _records, rows, _changed, _completed, _alerts = evaluate(satisfied, now=now, soc={"b1": 55.0})
    assert rows[0]["state"] == "scheduled" and rows[0]["due"] is False
    block = fc.plan_block(rows, cfg, horizon_end=now + timedelta(hours=30))
    assert block["active"] is False and block["batteries"] == []
    # The deadline is inside a longer horizon: the plan for it is active anyway.
    assert fc.plan_block(rows, cfg, horizon_end=now + timedelta(days=21))["active"] is True


def test_overdue_alerts_once_per_repeat_window_and_keeps_trying():
    now = T0
    cfg = settings()
    overdue = {"b1": fc.BatteryRecord("b1", next_due=now - timedelta(hours=25), first_seen=now - timedelta(days=31))}
    records, rows, _changed, _completed, alerts = evaluate(overdue, now=now, soc={"b1": 40.0}, cfg=cfg)
    assert rows[0]["overdue"] is True and rows[0]["state"] == "overdue"
    assert [row["id"] for row in alerts] == ["b1"]
    assert records["b1"].overdue_alerted_at == now
    # Inside the repeat window: no second alert, and the cycle is still overdue (never fake).
    _records, _rows, _changed, _completed, alerts = evaluate(records, now=now + timedelta(hours=2), soc={"b1": 40.0}, cfg=cfg)
    assert alerts == []
    _records, _rows, _changed, _completed, alerts = evaluate(records, now=now + timedelta(hours=25), soc={"b1": 40.0}, cfg=cfg)
    assert [row["id"] for row in alerts] == ["b1"]


def test_disabled_is_a_no_op_that_writes_nothing():
    now = T0
    cfg = settings(**{"maintenance.full_charge_enabled": False})
    record = fc.BatteryRecord("b1")
    records, rows, changed, completed, alerts = evaluate({"b1": record}, now=now, soc={"b1": 100.0}, cfg=cfg)
    assert changed is False and completed == [] and alerts == []
    assert records["b1"].next_due is None and records["b1"].last_completed is None
    assert rows[0]["watching"] is False and rows[0]["state"] == "disabled"
    assert fc.plan_block(rows, cfg, horizon_end=now + timedelta(days=365))["active"] is False


def test_state_round_trips_through_the_profile_payload():
    now = T0
    records = {
        "b1": fc.BatteryRecord(
            "b1",
            last_completed=now,
            next_due=now + timedelta(days=30),
            first_seen=now - timedelta(days=60),
            overdue_alerted_at=now - timedelta(hours=1),
        )
    }
    payload = json.loads(json.dumps(fc.dump_state(records, now)))
    restored = fc.parse_state(payload)
    assert restored["b1"].last_completed == now
    assert restored["b1"].next_due == now + timedelta(days=30)
    assert restored["b1"].overdue_alerted_at == now - timedelta(hours=1)
    assert fc.parse_state(None) == {} and fc.parse_state({"batteries": "nope"}) == {}


# ---------------------------------------------------------------------------- optimiser


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
        price_eur=1000.0,
        cycle_life=4000,
    )
    data.update(overrides)
    return BatteryConfig(**data)


def slots(buys, minutes=60, pv=0.0, load=0.5):
    return [
        dp.SlotInput(
            t=T0 + timedelta(minutes=minutes * i),
            pv_kw=pv,
            load_kw=load,
            buy=buy,
            sell=0.02,
        )
        for i, buy in enumerate(buys)
    ]


def terminal_kwh(plan, agg, options):
    last = plan.slots[-1]
    return dp.next_energy_kwh(
        last.soc_kwh, last.power_w / 1000.0, options.slot_minutes / 60.0, agg
    )


def test_terminal_target_applies_only_while_maintenance_is_active():
    agg = dp.BatteryAggregate.from_configs([battery()])
    # salvage 0: without a terminal target there is no reason to charge at all, so the maintenance
    # target is the only thing that can move the schedule.
    options = dp.OptimizerOptions(
        slot_minutes=60, soc_step_kwh=0.05, power_steps=8, salvage_multiplier=0.0
    )
    horizon = slots([0.10, 0.10, 0.30, 0.40])
    plain = dp.solve(horizon, aggregate=agg, start_kwh=0.2, options=options)
    active = dp.solve(
        horizon,
        aggregate=agg,
        start_kwh=0.2,
        options=options,
        maintenance={"active": True, "due": True, "target_soc_pct": 100.0, "batteries": ["example_1"]},
    )
    assert terminal_kwh(plain, agg, options) < terminal_kwh(active, agg, options)
    assert terminal_kwh(active, agg, options) == pytest.approx(agg.e_max_kwh, abs=0.06)
    assert active.summary.maintenance["target_soc_pct"] == 100.0
    assert active.summary.maintenance["batteries"] == ["example_1"]
    assert active.maintenance == active.summary.maintenance
    # Non-due: the plan is byte-identical to the plain one, so maintenance cannot distort it.
    inactive = dp.solve(
        horizon,
        aggregate=agg,
        start_kwh=0.2,
        options=options,
        maintenance={"active": False, "due": False, "target_soc_pct": 100.0, "batteries": []},
    )
    assert inactive.summary.maintenance is None
    assert [(s.soc_kwh, s.power_w) for s in inactive.slots] == [
        (s.soc_kwh, s.power_w) for s in plain.slots
    ]


def test_higher_user_terminal_target_still_wins_and_wear_is_unchanged():
    agg = dp.BatteryAggregate.from_configs([battery()])
    options = dp.OptimizerOptions(
        slot_minutes=60,
        soc_step_kwh=0.05,
        power_steps=8,
        wear_eur_per_kwh=0.05,
        target_soc_pct_end=90.0,
        salvage_multiplier=0.0,
    )
    horizon = slots([0.10, 0.10, 0.30, 0.40])
    plan = dp.solve(
        horizon,
        aggregate=agg,
        start_kwh=0.2,
        options=options,
        maintenance={"active": True, "due": True, "target_soc_pct": 80.0, "batteries": ["example_1"]},
    )
    assert plan.summary.maintenance["effective_target_soc_pct"] == 90.0
    # The wear the plan charged is still per delivered kWh at the configured constant.
    assert plan.summary.wear_eur_per_kwh == pytest.approx(0.05)
    expected = 0.0
    for slot in plan.slots:
        e_to = dp.next_energy_kwh(slot.soc_kwh, slot.power_w / 1000.0, 1.0, agg)
        expected += dp.wear_cost_eur(0.05, agg.eta_discharge, slot.soc_kwh, e_to)
    assert plan.summary.cycle_cost_eur == pytest.approx(expected)


def test_maintenance_target_reaching_uses_the_terminal_target_reason():
    agg = dp.BatteryAggregate.from_configs([battery()])
    options = dp.OptimizerOptions(
        slot_minutes=60, soc_step_kwh=0.05, power_steps=8, salvage_multiplier=0.0
    )
    horizon = slots([0.10, 0.45, 0.45, 0.45])
    plan = dp.solve(
        horizon,
        aggregate=agg,
        start_kwh=0.2,
        options=options,
        maintenance={"active": True, "due": True, "target_soc_pct": 100.0, "batteries": ["example_1"]},
    )
    assert any(slot.reason == "terminal_target" for slot in plan.slots)


# ---------------------------------------------------------------------------- engine + API


def build_engine(store=None, **settings_overrides):
    values = _selfcheck_settings()
    values["control.grid_follow"] = False
    values.update(settings_overrides)
    store = store if store is not None else _FakeStore()
    ha = _FakeHA()
    clock = Clock()
    engine = Engine(
        values,
        store=store,
        ha=ha,
        notifier=FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    return engine, values, store, ha, clock


def api_call(api: ApiServer, method: str, target: str, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    status, headers, content = api.dispatch(method, target, payload)
    parsed = None
    if headers.get("Content-Type", "").startswith("application/json") and content:
        parsed = json.loads(content)
    return status, headers, parsed


def test_disabled_engine_no_op_plans_without_a_maintenance_block():
    engine, _settings, store, _ha, _clock = build_engine()
    plan = engine.plan_now("test")
    assert plan is not None
    assert plan.summary.maintenance is None
    assert plan.maintenance is None
    status = engine.full_charge_status()
    assert status["enabled"] is False and status["plan"]["active"] is False
    assert status["batteries"][0]["state"] == "disabled"
    # Nothing was written to the runtime profile: the feature really is a no-op.
    assert store.learning.get((fc.STATE_KIND, fc.STATE_KEY)) is None


def test_due_battery_gets_a_maintenance_plan_and_api_exposes_it():
    engine, _settings, _store, _ha, clock = build_engine(
        **{"maintenance.full_charge_enabled": True}
    )
    plan = engine.plan_now("test")
    assert plan is not None
    block = plan.summary.maintenance
    assert block and block["active"] is True and block["due"] is True
    assert block["target_soc_pct"] == 100.0
    assert block["batteries"] == ["example_battery_1"]

    payload = plan_dict(plan, clock())
    assert payload["maintenance"] == block
    assert payload["summary"]["maintenance"] == block

    batteries = engine.batteries_view()
    row = batteries[0]["maintenance"]
    assert row["watching"] is True and row["due"] is True
    assert row["target_soc_pct"] == 100.0 and row["last_completed"] is None

    api = ApiServer(engine, host="127.0.0.1", port=0)
    status, _headers, body = api_call(api, "GET", "/api/v1/plan")
    assert status == 200 and body["maintenance"]["active"] is True
    status, _headers, body = api_call(api, "GET", "/api/v1/batteries")
    assert status == 200 and body["batteries"][0]["maintenance"]["due"] is True
    status, _headers, body = api_call(api, "GET", "/api/v1/status")
    assert status == 200 and body["full_charge"]["enabled"] is True


def test_completion_is_recorded_from_a_fresh_measurement_and_drops_the_target():
    engine, _settings, store, ha, _clock = build_engine(
        **{"maintenance.full_charge_enabled": True}
    )
    engine.plan_now("test")
    assert engine.full_charge_status()["plan"]["active"] is True

    # The battery now reports the target: the next tick records the completion.
    ha.states["sensor.example_battery_soc"]["state"] = "100"
    engine.tick_now()
    status = engine.full_charge_status()
    assert status["batteries"][0]["last_completed"] is not None
    assert status["batteries"][0]["state"] == "scheduled"
    assert engine._full_charge_state["example_battery_1"].last_completed is not None
    stored = store.learning[(fc.STATE_KIND, fc.STATE_KEY)]
    assert stored[1]["batteries"]["example_battery_1"]["last_completed"] is not None
    # The next plan no longer aims for the target (the deadline moved an interval out).
    plan = engine.plan_now("test")
    assert plan is not None and plan.summary.maintenance is None


def test_overdue_battery_alerts_and_keeps_trying():
    engine, _settings, _store, _ha, clock = build_engine(
        **{"maintenance.full_charge_enabled": True}
    )
    engine._full_charge_loaded = True
    engine._full_charge_state = {
        "example_battery_1": fc.BatteryRecord(
            "example_battery_1",
            first_seen=clock() - timedelta(days=31),
            next_due=clock() - timedelta(hours=25),
        )
    }
    engine.tick_now()
    alerts = [alert for alert in engine.snapshot().alerts if alert.code == "full_charge_overdue"]
    assert alerts and alerts[-1].detail["battery_id"] == "example_battery_1"
    status = engine.full_charge_status()
    assert status["batteries"][0]["overdue"] is True and status["batteries"][0]["state"] == "overdue"
    # Still overdue: the cycle keeps trying, it never marks itself complete.
    assert status["batteries"][0]["last_completed"] is None


def test_engine_state_survives_a_restart_from_the_profile():
    engine, _settings, store, _ha, _clock = build_engine(
        **{"maintenance.full_charge_enabled": True}
    )
    engine.full_charge_status()
    assert engine._full_charge_state["example_battery_1"].next_due is not None

    # A second engine over the same store restores the due date from the persisted profile.
    rebuilt, _values, _store2, _ha2, _clock2 = build_engine(
        store=store, **{"maintenance.full_charge_enabled": True}
    )
    assert rebuilt._load_full_charge_state()["example_battery_1"].next_due is not None
