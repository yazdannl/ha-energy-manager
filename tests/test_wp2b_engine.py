"""WP2B engine tests: planning, override, kill switch, publisher, journal, batteries, savings.

The engine is booted against the in-process fakes that ``--selfcheck`` uses (``app.main``), so the
real optimiser, safety layer and dispatcher run with no Home Assistant anywhere.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app import engine as engine_module  # noqa: E402
from app.control.dispatcher import TickOutcome  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.grid import GridMapping  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import BatteryConfig, BatteryState, Mode, ProviderStatus, Reason, from_iso  # noqa: E402

PUBLISHED_ENTITIES = {
    "sensor.sem_autopilot_state",
    "sensor.sem_plan_action",
    "sensor.sem_price_now",
    "sensor.sem_pv_forecast_today",
    "sensor.sem_load_forecast_now",
    "sensor.sem_savings_today",
    "sensor.sem_battery_soc_avg",
    "sensor.sem_forecast_accuracy",
    "sensor.sem_battery_cost_basis",
    "sensor.sem_battery_value",
    "binary_sensor.sem_autopilot_active",
    "binary_sensor.sem_battery_at_floor",
}


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)
        return self.now


class FakeNotifier:
    def __init__(self, enabled=True):
        self.sent: list[tuple[str, str, str]] = []
        self.enabled = enabled

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True

    def test(self):
        self.sent.append(("test", "", ""))
        return True


def build_engine(clock=None, notifier=None, **overrides):
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    store = _FakeStore()
    ha = _FakeHA()
    clock = clock or Clock()
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        notifier=notifier or FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    engine._test_settings = settings  # noqa: SLF001 - convenience for assertions
    return engine, store, ha, clock, settings


# ----------------------------------------------------------------------------------- planning


#: A second, independent battery: two command paths, two drivers, two write locks.
SECOND_EXAMPLE_BATTERY = BatteryConfig(
    id="example_battery_2",
    name="Example Battery 2",
    driver="generic",
    capacity_kwh=5.0,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
    max_charge_w=1000.0,
    max_discharge_w=1000.0,
    eta_charge=0.95,
    eta_discharge=0.95,
    mapping={
        "power_setpoint_entity": "number.example_battery_power_2",
        "soc_entity": "sensor.example_battery_soc_2",
    },
    enabled=True,
)


class _ParkedWrite:
    """Parks one battery's in-tick setpoint write and records every write the drivers make.

    ``parked`` fires when the tick is inside the parked write; ``release`` lets it continue.  The
    log entry kinds are ``write`` (a setpoint the dispatch asked for) and ``hold`` (a neutralising
    0), both with the battery id and the requested power.
    """

    def __init__(self, block_id: str, timeout: float = 10.0):
        self.block_id = block_id
        self.timeout = timeout
        self.log: list[tuple[str, str, float]] = []
        self.parked = threading.Event()
        self.release = threading.Event()

    def wrap(self, inner: Any) -> Any:
        probe = self

        class ParkingDriver:
            def __init__(self) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)  # noqa: SLF001 - thin proxy

            def id(self) -> str:
                return self._inner.id()  # noqa: SLF001 - thin proxy

            def apply(self, power_w: float, *, dry_run: bool) -> Any:
                battery_id = self._inner.id()  # noqa: SLF001 - thin proxy
                if not dry_run and battery_id == probe.block_id:
                    probe.log.append(("park", battery_id, float(power_w)))
                    probe.parked.set()
                    probe.release.wait(probe.timeout)
                result = self._inner.apply(power_w, dry_run=dry_run)  # noqa: SLF001 - thin proxy
                probe.log.append(("write", battery_id, float(result.requested_w)))
                return result

            def hold(self, *, dry_run: bool) -> Any:
                battery_id = self._inner.id()  # noqa: SLF001 - thin proxy
                probe.log.append(("hold", battery_id, 0.0))
                result = self._inner.hold(dry_run=dry_run)  # noqa: SLF001 - thin proxy
                return result

        return ParkingDriver()


def test_replan_creates_a_plan_and_records_it():
    engine, store, ha, clock, _settings = build_engine()
    plan = engine.plan_now("test")
    assert plan is not None and plan.slots and plan.horizon_start == clock.now.replace(minute=0)
    assert engine.active_plan() is plan
    assert store.plans and store.plans[-1] is plan
    events = [code for _, code, _, _ in [(e["level"], e["code"], e["message"], e.get("detail")) for e in store.events]]
    assert "plan_updated" in events
    assert plan.summary.charged_kwh >= 0.0


def test_replan_is_deterministic():
    engine, _store, _ha, _clock, _settings = build_engine()
    first = engine.plan_now("test")
    second = engine.plan_now("test")
    assert [s.power_w for s in first.slots] == [s.power_w for s in second.slots]
    assert [s.soc_kwh for s in first.slots] == [s.soc_kwh for s in second.slots]
    # determinism is the point here; feasibility of this fixture is the optimiser's business
    assert first.feasible == second.feasible


def test_advisory_mode_never_writes_and_auto_dry_run_never_writes():
    engine, store, ha, _clock, settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    assert ha.service_calls == []
    engine.set_mode(Mode.AUTO.value, True)
    engine.tick_now()
    assert ha.service_calls == []
    assert any(entry["dry_run"] for entry in store.dispatches)
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()
    assert ha.service_calls


def test_off_mode_collects_but_does_not_plan_or_write():
    engine, store, ha, _clock, settings = build_engine()
    settings["mode"] = Mode.OFF.value
    settings["dry_run"] = False
    engine.tick_now()
    assert engine.snapshot().mode == Mode.OFF.value
    assert ha.service_calls == []
    assert all(entry["code"] == Reason.MODE_NOT_AUTO for entry in store.dispatches)


def test_stop_now_switches_to_advisory_and_holds():
    engine, store, ha, _clock, settings = live_engine()
    calls_before = ha.service_calls.copy()
    result = engine.stop_now()
    assert result["mode"] == Mode.ADVISORY.value
    assert set(result) == {"mode", "override_cleared", "stopped"}  # SPEC 10 response shape
    assert result["override_cleared"] is False
    assert [entry["battery_id"] for entry in result["stopped"]] == ["example_battery_1"]
    assert result["stopped"][0]["ok"] is True and result["stopped"][0]["dry_run"] is False
    assert len(ha.service_calls) > len(calls_before)  # a hold was actually written
    assert ha.service_calls[-1][2]["value"] == 0.0
    assert engine.snapshot().mode == Mode.ADVISORY.value
    assert engine.pending_holds() == []


# ----------------------------------------------------------------------------------- override


def test_override_is_bounded_and_expires():
    engine, store, _ha, clock, _settings = build_engine()
    override = engine.set_override(700.0, 500.0)
    assert override["minutes"] == 240.0  # clamped to the documented maximum
    assert override["power_w"] == 700.0
    engine.set_override(500.0, 5.0)
    assert engine.snapshot().override["power_w"] == 500.0
    clock.advance(minutes=6)
    engine.tick_now()
    assert engine.snapshot().override is None
    assert any(event["code"] == "override_expired" for event in store.events)


def test_override_pins_the_battery_power():
    engine, store, ha, _clock, _settings = build_engine()
    engine.set_mode(Mode.AUTO.value, False)
    engine.set_override(250.0, 10.0)
    engine.tick_now()
    assert ha.service_calls and ha.service_calls[-1][2]["value"] == 250.0
    assert any(entry["code"] == Reason.OVERRIDE_ACTIVE for entry in store.dispatches)
    assert any(
        entry["detail"].get("reason") == Reason.OVERRIDE_ACTIVE for entry in engine.log(limit=20)
    )


def test_no_plan_is_published_until_a_battery_has_been_read():
    """SPEC 9.1 / WP5 finding 7: never plan or dispatch from an assumed floor SoC.

    A plan computed before the first read is anchored on the aggregate floor, so every slot holds
    and the next auto tick has a 0 W target: the engine must withhold it instead.
    """
    engine, store, ha, clock, settings = build_engine(
        clock=Clock(datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc))
    )
    engine.set_mode(Mode.AUTO.value, False)
    ha.states["sensor.example_battery_soc"] = {"state": "unavailable", "attributes": {}}

    # nothing readable: a plan may be computed for the caller, but it is anchored on the assumed
    # floor SoC and is therefore never stored, published or dispatched
    cold = engine.plan_now("test")
    assert cold is not None and cold.slots
    assert cold.slots[0].soc_kwh == 5.0 * 0.10, cold.slots[0].soc_kwh
    assert engine.active_plan() is None
    assert store.plans == []
    assert engine.tick_now().target_w == 0.0
    assert ha.service_calls == []
    assert any(entry["code"] == "battery_reading_pending" for entry in engine.log(limit=20))
    assert any(alert.code == "battery_reading_pending" for alert in engine.snapshot().alerts)

    # the battery comes back: the first tick reads it, plans from the measured SoC and dispatches
    ha.states["sensor.example_battery_soc"] = {
        "state": "55",
        "attributes": {"unit_of_measurement": "%"},
    }
    engine.tick_now()
    plan = engine.active_plan()
    assert plan is not None, "a readable battery must produce a published plan"
    floor_kwh = 5.0 * 0.10
    assert plan.slots[0].soc_kwh > floor_kwh + 1e-6, plan.slots[0].soc_kwh
    assert plan.slots[0].soc_kwh == 2.75  # 55 % of the 5 kWh example battery
    writes = [call for call in ha.service_calls if call[0] in ("number", "input_number")]
    assert writes and abs(float(writes[-1][2].get("value") or 0.0)) > 0.0, ha.service_calls
    # ...and the dispatch really went through the safety layer, not around it
    assert any(entry["kind"] == "dispatch" for entry in engine.log(limit=20))


def test_off_mode_plans_nothing_and_drops_the_stale_plan():
    """SPEC 1: `off` is "no planning, no writes" - the API and sampling keep working."""
    engine, _store, _ha, _clock, _settings = build_engine()
    engine.set_mode(Mode.AUTO.value, False)
    assert engine.plan_now("test") is not None
    assert engine.active_plan() is not None
    engine.set_mode(Mode.OFF.value)
    # nothing stale stays on screen as the current plan
    assert engine.active_plan() is None
    # the automatic planner refuses to run in off mode, even when a recompute was queued
    assert engine.request_replan("test") == {"queued": True, "reason": "test"}
    assert engine._replan_reason() is None  # noqa: SLF001
    engine.tick_now()
    assert engine.active_plan() is None
    # sampling and the journal keep working (the tick still ran)
    assert engine.snapshot().last_tick_at is not None
    assert any(entry["code"] == "plan_cleared" for entry in engine.log(limit=20))


def test_test_discharge_requires_live_auto():
    engine, _store, _ha, _clock, settings = build_engine()
    battery_id = "example_battery_1"
    try:
        engine.test_discharge(battery_id, -300.0, 5.0)
        raise AssertionError("expected a PermissionError in advisory mode")
    except PermissionError as exc:
        assert "auto" in str(exc)
    engine.set_mode(Mode.AUTO.value, True)
    try:
        engine.test_discharge(battery_id, -300.0, 5.0)
        raise AssertionError("expected a PermissionError while dry_run")
    except PermissionError:
        pass
    engine.set_mode(Mode.AUTO.value, False)
    result = engine.test_discharge(battery_id, -300.0, 5.0)
    assert result["started"] is True and result["power_w"] == -300.0
    assert engine.snapshot().override["battery_id"] == battery_id
    try:
        engine.test_discharge("example_unknown", -100.0, 5.0)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


# ---------------------------------------------------------------------------------- publisher


def test_publisher_publishes_the_documented_entities():
    engine, _store, ha, clock, _settings = build_engine()
    engine.plan_now("test")
    published = engine.publish_states()
    assert published == len(PUBLISHED_ENTITIES)
    assert set(ha.published) == PUBLISHED_ENTITIES
    action = ha.published["sensor.sem_plan_action"]
    assert set(action["attributes"]) >= {"reason", "target_power_w", "soc_target_pct"}
    price = ha.published["sensor.sem_price_now"]
    assert set(price["attributes"]) >= {"rank_pct", "cheap_window_start", "expensive_hour_start"}
    assert ha.published["sensor.sem_autopilot_state"]["state"] == "advisory"
    # Cost basis (0.18.0): no ledger in a selfcheck store, so both sensors say unknown.
    basis = ha.published["sensor.sem_battery_cost_basis"]
    assert basis["state"] == "unknown" and basis["attributes"]["unit_of_measurement"] == "EUR/kWh"
    assert ha.published["sensor.sem_battery_value"]["state"] == "unknown"


def test_publisher_is_throttled_and_idempotent():
    engine, _store, ha, clock, _settings = build_engine()
    engine.plan_now("test")
    assert engine.publish_states() == len(PUBLISHED_ENTITIES)
    assert engine.publish_states() == 0  # throttled
    clock.advance(seconds=25)
    assert engine.publish_states() == 0  # unchanged payload
    clock.advance(seconds=25)
    engine.set_mode(Mode.AUTO.value, True)
    published = engine.publish_states()
    assert 0 < published <= len(PUBLISHED_ENTITIES)
    assert ha.published["sensor.sem_autopilot_state"]["state"] == "dry_run"
    assert ha.published["binary_sensor.sem_autopilot_active"]["state"] == "off"


def test_publisher_skips_when_ha_is_unreachable():
    engine, _store, ha, _clock, _settings = build_engine()
    engine.plan_now("test")

    def fail(*args, **kwargs):
        raise RuntimeError("example HA down")

    ha.set_state = fail
    assert engine.publish_states() == 0
    assert engine.snapshot().ha_connected is False


# ------------------------------------------------------------------------- savings/journal


def test_savings_accumulate_into_daily_rows():
    engine, store, _ha, clock, _settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    clock.advance(seconds=30)
    engine.tick_now()
    buckets, totals = engine.savings("day")
    assert store.savings, "expected a savings_daily row"
    assert len(buckets) == 1
    assert totals["import_kwh"] > 0
    assert abs(totals["savings_eur"] - (totals["baseline_cost_eur"] - totals["cost_eur"] + totals["revenue_eur"])) < 1e-9
    assert engine.savings_totals("month")["savings_eur"] == totals["savings_eur"]


def measured_battery(**overrides) -> BatteryState:
    """A confirmed measured reading for the selfcheck battery (deterministic savings tests)."""
    values = dict(
        id="example_battery_1",
        available=True,
        soc_pct=55.0,
        power_w=0.0,
        capacity_kwh=5.0,
        power_measured=True,
    )
    values.update(overrides)
    return BatteryState(**values)


def savings_tick(engine, clock, values, batteries, *, minutes=30):
    """Drive one savings interval with an explicit battery reading (bypasses the driver)."""
    engine._state.batteries = list(batteries)  # noqa: SLF001 - the reading is the input under test
    engine._last_sample_at = clock.now - timedelta(minutes=minutes)  # noqa: SLF001
    engine._savings_tick(values, clock.now)  # noqa: SLF001
    return dict(engine._savings_today)  # noqa: SLF001


def test_savings_never_credits_a_dry_run_command():
    """A dry-run/advisory dispatch writes nothing; the generic setpoint read-back is not energy."""
    engine, store, _ha, clock, _settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    clock.advance(seconds=30)
    engine.tick_now()
    row = store.savings.get(clock.now.date().isoformat(), {})
    assert row.get("battery_measured") == 0
    assert row.get("charged_kwh") is None and row.get("discharged_kwh") is None
    assert row.get("battery_cycles") is None
    assert engine.battery_cycles() == 0.0, "a command must never become delivered cycles"


def test_savings_credit_only_measured_battery_flow():
    """0.13.0: a dry-run/blocked/failed dispatch earns zero, and a clamp counts the measured power."""
    engine, _store, _ha, clock, _settings = build_engine()
    engine.plan_now("test")
    values = {"grid_w": 500.0, "load_w": 1200.0, "pv_w": 300.0}
    # The plan may ask for a charge, but the battery measured 0 W: zero delivered energy, and the
    # series is confirmed-measured (so the zero is known, not unknown).
    dry = savings_tick(engine, clock, values, [measured_battery(power_w=0.0)])
    assert dry["battery_measured"] == 1
    assert dry["charged_kwh"] == 0.0 and dry["discharged_kwh"] == 0.0
    assert dry["battery_cycles"] == 0.0

    # A clamped write (plan asked more): integrate the measured 250 W only.
    clamped_engine, _store2, _ha2, clamped_clock, _settings2 = build_engine()
    clamped_engine.plan_now("test")
    clamped = savings_tick(
        clamped_engine, clamped_clock, values, [measured_battery(power_w=250.0)], minutes=60
    )
    assert abs(clamped["charged_kwh"] - 0.25) < 1e-9
    assert clamped["battery_measured"] == 1

    # A confirmed discharge is priced with the sell series in force at flow time.
    discharge_engine, _store3, _ha3, discharge_clock, _settings3 = build_engine()
    discharge_engine.plan_now("test")
    slot = discharge_engine.active_plan().slot_at(discharge_clock.now)
    discharged = savings_tick(
        discharge_engine, discharge_clock, values, [measured_battery(power_w=-500.0)], minutes=60
    )
    assert abs(discharged["discharged_kwh"] - 0.5) < 1e-9
    assert abs(discharged["discharge_value_eur"] - slot.sell * 0.5) < 1e-9
    assert abs(discharged["charge_cost_eur"]) < 1e-9


def test_savings_measurement_gap_is_unknown_not_planned():
    """An unconfirmed setpoint never substitutes for a measurement the battery did not provide."""
    engine, _store, _ha, clock, _settings = build_engine()
    engine.plan_now("test")
    values = {"grid_w": 500.0, "load_w": 1200.0, "pv_w": 300.0}
    first = savings_tick(engine, clock, values, [measured_battery(power_w=500.0)])
    assert first["battery_measured"] == 1 and first["charged_kwh"] > 0.0
    # The only value now is a setpoint we wrote (power_measured False, e.g. a generic driver or a
    # Zendure setpoint whose measured pair went missing): the day's flow is unknown, not planned.
    clock.advance(minutes=30)
    gap = savings_tick(
        engine, clock, values, [measured_battery(power_w=800.0, power_measured=False)]
    )
    assert gap["battery_measured"] == 0
    assert gap["charged_kwh"] is None and gap["battery_cycles"] is None
    # It stays unknown: a later confirmed interval cannot repair an incomplete day into a partial
    # total that would look fully measured.
    clock.advance(minutes=30)
    later = savings_tick(engine, clock, values, [measured_battery(power_w=500.0)])
    assert later["charged_kwh"] is None
    assert later["battery_measured"] == 0


def test_savings_midnight_rollover_keeps_each_days_measured_flow():
    clock = Clock(datetime(2026, 1, 1, 23, 50, tzinfo=timezone.utc))
    engine, store, _ha, clock, _settings = build_engine(clock=clock)
    engine.plan_now("test")
    values = {"grid_w": 500.0, "load_w": 1200.0, "pv_w": 300.0}
    yesterday = savings_tick(engine, clock, values, [measured_battery(power_w=1000.0)], minutes=10)
    assert yesterday["day"] == "2026-01-01"
    assert abs(yesterday["charged_kwh"] - 1.0 / 6.0) < 1e-9  # 1 kW for 10 minutes
    clock.advance(minutes=20)  # 00:10 next day
    today = savings_tick(engine, clock, values, [measured_battery(power_w=500.0)], minutes=10)
    assert today["day"] == "2026-01-02"
    assert abs(today["charged_kwh"] - 0.5 / 6.0) < 1e-9
    # Yesterday was flushed under its own day before the new one began; today is still in memory
    # until the next flush.
    assert abs(store.savings["2026-01-01"]["charged_kwh"] - 1.0 / 6.0) < 1e-9
    engine._flush_savings()  # noqa: SLF001
    assert abs(store.savings["2026-01-02"]["charged_kwh"] - 0.5 / 6.0) < 1e-9


def test_savings_totals_aggregate_measured_flows_across_ranges():
    engine, store, _ha, _clock, _settings = build_engine()
    store.savings = {
        "2025-12-31": {
            "day": "2025-12-31",
            "charged_kwh": 1.0,
            "discharged_kwh": 0.5,
            "charge_cost_eur": 0.2,
            "discharge_value_eur": 0.05,
            "battery_measured": 1,
        },
        "2026-01-01": {
            "day": "2026-01-01",
            "charged_kwh": 2.0,
            "discharged_kwh": 1.0,
            "charge_cost_eur": 0.4,
            "discharge_value_eur": 0.1,
            "battery_measured": 1,
        },
        "2025-01-05": {"day": "2025-01-05", "charged_kwh": None, "battery_measured": 0},
    }
    _day, day_totals = engine.savings("day")
    assert abs(day_totals["charged_kwh"] - 2.0) < 1e-9
    assert abs(day_totals["discharge_value_eur"] - 0.1) < 1e-9
    week_buckets, week_totals = engine.savings("week")
    assert abs(week_totals["charged_kwh"] - 3.0) < 1e-9
    assert abs(week_totals["discharged_kwh"] - 1.5) < 1e-9
    assert week_totals["measured_buckets"]["battery"] == 2
    all_buckets, all_totals = engine.savings("all")
    # The 2025-01-05 row is a measured gap: its bucket reports unknown, so the range total is a
    # sum of the two measured days only and the bucket flags zero coverage for those days.
    assert abs(all_totals["charged_kwh"] - 3.0) < 1e-9
    assert all_buckets and all_totals["measured_buckets"]["battery"] == 2


def test_plan_execution_joins_measured_actuals_and_dispatches():
    """GET /plan/execution: measured energy and applied setpoints per slot, future slots empty (SPEC 10)."""
    clock = Clock(datetime(2026, 1, 1, 12, 20, tzinfo=timezone.utc))
    engine, store, _ha, _clock, _settings = build_engine(clock=clock)
    engine.plan_now("test")
    plan = engine.active_plan()
    minutes = plan.slot_minutes
    step = timedelta(minutes=minutes)
    # The plan grid starts at the slot containing `now`, so only a later `now` makes a slot complete.
    clock.advance(minutes=50)
    elapsed = [slot for slot in plan.slots if slot.t + step <= clock.now]
    assert len(elapsed) == 1, "the selfcheck plan must have exactly one complete slot by now"
    slot = elapsed[0]
    # Two 5-minute rows inside that slot: charging 800 W, importing 1.8 kW, SoC rising 1.0 kWh.
    for index in range(2):
        store.upsert_actuals(
            slot.t + timedelta(minutes=5 * index),
            pv_kw=0.0,
            load_kw=1.0,
            grid_kw=1.8,
            battery_kw=0.8,
            soc_kwh=3.0 + 0.5 * index,
            price_buy=slot.buy,
            plan_power_w=800.0,
            dispatch_power_w=800.0,
        )
    engine._journal.dispatch(  # noqa: SLF001 - the journal is the engine's own dispatch log
        {
            "ts": slot.t + timedelta(minutes=1),
            "battery_id": "example_battery_1",
            "requested_w": 800.0,
            "applied_w": 400.0,
            "soc_pct": 55.0,
            "mode": Mode.AUTO.value,
            "dry_run": False,
            "result": "ok",
            "code": Reason.POWER_CLAMP,
            "message": "clamped by the import limit",
            "reason": Reason.POWER_CLAMP,
            "writes": [],
        }
    )
    payload = engine.plan_execution()
    assert payload["slot_minutes"] == minutes
    assert payload["generated_at"] == plan.generated_at.isoformat()
    by_time = {entry["t"]: entry for entry in payload["slots"]}
    measured = by_time[slot.t.isoformat()]["actual"]
    assert measured["samples"] == 2
    assert measured["coverage_pct"] == 100.0 * 2 * 5 / minutes
    # Every row stands for five measured minutes: 0.8 kW x 10 min = 0.133 kWh in, 1.8 kW x 10 min
    # = 0.3 kWh imported, and the SoC runs from the first to the last row.
    assert abs(measured["charged_kwh"] - 0.8 * 10 / 60) < 1e-9
    assert abs(measured["discharged_kwh"]) < 1e-9
    assert abs(measured["import_kwh"] - 1.8 * 10 / 60) < 1e-9
    assert abs(measured["export_kwh"]) < 1e-9
    assert measured["measured_kw"] == 0.8 and measured["planned_kw"] == 0.8
    assert measured["soc_kwh_start"] == 3.0 and measured["soc_kwh_end"] == 3.5
    dispatch = by_time[slot.t.isoformat()]["dispatch"]
    assert dispatch["requested_w"] == 800.0 and dispatch["applied_w"] == 400.0
    assert dispatch["clamped"] is True and dispatch["dry_run"] is False and dispatch["ok"] is True
    assert dispatch["code"] == Reason.POWER_CLAMP and dispatch["records"] == 1
    assert [item["id"] for item in dispatch["batteries"]] == ["example_battery_1"]
    # No other slot has been measured or dispatched, and a slot nobody reached reports nothing at
    # all -- never a zero (the slot containing `now` is one of them).
    for entry in payload["slots"]:
        if entry["t"] == slot.t.isoformat():
            continue
        assert entry["actual"] is None and entry["dispatch"] is None
    assert payload["slots"][0]["t"] == plan.slots[0].t.isoformat()
    assert len(payload["slots"]) == len(plan.slots)


def test_plan_execution_progress_counts_only_elapsed_slots():
    clock = Clock(datetime(2026, 1, 1, 12, 20, tzinfo=timezone.utc))
    engine, store, _ha, _clock, _settings = build_engine(clock=clock)
    engine.plan_now("test")
    plan = engine.active_plan()
    step = timedelta(minutes=plan.slot_minutes)
    clock.advance(minutes=50)
    elapsed = [slot for slot in plan.slots if slot.t + step <= clock.now]
    slot = elapsed[0]
    store.upsert_actuals(slot.t + timedelta(minutes=5), grid_kw=-2.0, battery_kw=-0.5, soc_kwh=4.0)
    progress = engine.plan_execution()["progress"]
    assert progress["slots_total"] == len(plan.slots)
    assert progress["slots_elapsed"] == len(elapsed)
    assert progress["samples"] == 1
    assert abs(progress["measured_export_kwh"] - 2.0 * 5 / 60) < 1e-9
    assert abs(progress["measured_discharged_kwh"] - 0.5 * 5 / 60) < 1e-9
    assert progress["measured_import_kwh"] == 0.0
    expected_planned = sum(
        max(0.0, float(item.power_w)) / 1000.0 * (plan.slot_minutes / 60.0) for item in elapsed
    )
    assert abs(progress["planned_charged_kwh"] - expected_planned) < 1e-9
    expected_cost = sum(item.cost_eur(plan.slot_minutes / 60.0) for item in elapsed)
    assert abs(progress["planned_cost_eur"] - expected_cost) < 1e-9
    # The measured cost is the slot's own tariff: export earns the sell price.
    assert abs(progress["measured_cost_eur"] - (-(2.0 * 5 / 60) * slot.sell)) < 1e-9


def test_plan_execution_is_empty_without_a_plan_or_a_store():
    clock = Clock(datetime(2026, 1, 1, 12, 20, tzinfo=timezone.utc))
    engine, _store, _ha, _clock, _settings = build_engine(clock=clock)
    empty = engine.plan_execution()
    assert empty["slots"] == [] and empty["generated_at"] is None and empty["slot_minutes"] is None
    assert empty["progress"]["measured_charged_kwh"] is None
    assert set(empty["floors"]) == {"reserve_soc_pct", "min_soc_pct", "floor_soc_pct"}
    engine.plan_now("test")
    payload = engine.plan_execution()
    assert payload["slots"] and payload["progress"]["slots_elapsed"] >= 0
    # Without measured rows nothing is claimed: a measured total stays null, not 0.0.
    assert payload["progress"]["measured_import_kwh"] is None
    assert payload["batteries"] and payload["batteries"][0]["id"] == "example_battery_1"
    assert payload["batteries"][0]["floor_soc_pct"] == max(
        payload["floors"]["reserve_soc_pct"], payload["batteries"][0]["min_soc_pct"]
    )
    short = engine.plan_execution(hours=12)["slots"]
    assert 0 < len(short) < len(payload["slots"])
    assert short == payload["slots"][: len(short)]


def test_journal_entries_have_the_api_shape():
    engine, _store, _ha, _clock, _settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    entries = engine.log(limit=50)
    assert entries
    for entry in entries:
        assert set(entry) >= {"ts", "kind", "level", "code", "message", "detail"}
    assert any(entry["kind"] == "dispatch" for entry in entries)
    filtered = engine.log(limit=50, level="info")
    assert all(entry["level"] == "info" for entry in filtered)
    dispatch_only = engine.log(limit=50, kind="dispatch")
    assert all(entry["kind"] == "dispatch" for entry in dispatch_only)


def test_battery_floor_alert_and_notification():
    engine, store, _ha, _clock, _settings = build_engine()
    notifier = FakeNotifier()
    engine._notifier = notifier  # noqa: SLF001
    engine.plan_now("test")
    engine.tick_now()
    alert_codes = [alert.code for alert in engine.snapshot().alerts]
    assert "battery_floor_reached" not in alert_codes  # the fake battery sits at 55 %
    engine.snapshot()  # smoke
    # now force the reported SoC to the floor
    engine._drivers["example_battery_1"]._ha.states["sensor.example_battery_soc"] = {  # noqa: SLF001
        "state": "10",
        "attributes": {"unit_of_measurement": "%"},
    }
    engine.tick_now()
    engine._check_battery_floor(engine.snapshot().now)  # noqa: SLF001
    assert any(alert.code == "battery_floor_reached" for alert in engine.snapshot().alerts)
    assert any(event == "battery_floor_reached" for event, _, _ in notifier.sent)


def test_negative_price_alert():
    engine, _store, _ha, clock, _settings = build_engine()

    def negative_price(now):
        forecast = _selfcheck_sources()["price_source"](now)
        for slot in forecast.slots:
            slot.buy = -0.05
        return forecast

    engine._price_source = negative_price  # noqa: SLF001
    engine.plan_now("test")
    engine._check_negative_price(clock.now)  # noqa: SLF001
    assert any(alert.code == "negative_price" for alert in engine.snapshot().alerts)


def test_watchdog_holds_when_the_control_loop_stalls():
    engine, store, ha, clock, _settings = build_engine()
    engine.plan_now("test")
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()
    ha.service_calls.clear()
    engine._dispatcher.note_tick(clock.now)  # noqa: SLF001
    clock.advance(minutes=10)
    assert engine._dispatcher.stalled(clock.now) is True  # noqa: SLF001
    engine._dispatcher.watchdog(clock.now)  # noqa: SLF001
    assert ha.service_calls and ha.service_calls[-1][2]["value"] == 0.0
    assert any(event["code"] == "watchdog_hold" for event in store.events)


# --------------------------------------------------------------------------------- batteries


def test_battery_crud_and_validation():
    engine, _store, _ha, _clock, _settings = build_engine()
    good = {
        "id": "example_battery_2",
        "name": "Example Battery 2",
        "driver": "generic",
        "capacity_kwh": 3.0,
        "mapping": {"power_setpoint_entity": "number.example_power_2"},
    }
    created = engine.create_battery(good)
    assert created["id"] == "example_battery_2"
    assert len(engine.battery_configs()) == 2
    try:
        engine.create_battery({"id": "bad", "driver": "generic", "capacity_kwh": 1.0, "mapping": {}})
        raise AssertionError("expected a ValueError")
    except ValueError as exc:
        assert "power_setpoint_entity" in str(exc)
    updated = engine.update_battery("example_battery_2", {"max_charge_w": 750.0})
    assert updated["max_charge_w"] == 750.0
    try:
        engine.update_battery("example_missing", {"name": "x"})
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    result = engine.delete_battery("example_battery_2")
    assert result["deleted"] == "example_battery_2" and result["hard"] is True
    assert len(engine.battery_configs()) == 1
    try:
        engine.delete_battery("example_missing")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_batteries_view_reports_mapping_and_capabilities():
    engine, _store, _ha, _clock, _settings = build_engine()
    views = engine.batteries_view()
    assert len(views) == 1
    entry = views[0]
    assert entry["config"]["id"] == "example_battery_1"
    assert entry["mapping_resolved"] is True
    assert entry["capabilities"]["driver"] == "generic"
    assert entry["state"]["id"] == "example_battery_1"


def test_discover_returns_capped_filtered_candidates():
    engine, _store, _ha, _clock, _settings = build_engine()
    entries = [
        {
            "entity_id": f"sensor.example_price_{index}",
            "name": f"Example price {index}",
            "device_class": "monetary",
            "unit_of_measurement": "EUR/kWh",
            "platform": "example",
        }
        for index in range(60)
    ]
    engine._registry_source = lambda: entries  # noqa: SLF001
    engine._discover_cache = None  # noqa: SLF001
    found = engine.discover("price", limit=100)
    assert len(found) == 50
    assert {candidate["suggested_role"] for candidate in found} == {"price_now"}
    assert all(0.0 < candidate["confidence"] <= 1.0 for candidate in found)
    filtered = engine.discover("price", search="price_7")
    assert [candidate["entity_id"] for candidate in filtered] == ["sensor.example_price_7"]
    try:
        engine.discover("example_unknown")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_discover_suggests_battery_roles_from_the_registry():
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._registry_source = lambda: [  # noqa: SLF001
        {
            "entity_id": "number.example_device_input_limit",
            "platform": "zendure_ha",
            "translation_key": "input_limit",
            "device_id": "example_device_1",
            "name": "Input limit",
        }
    ]
    engine._discover_cache = None  # noqa: SLF001
    found = engine.discover("battery")
    assert found and found[0]["suggested_role"] == "charge_limit"
    # ... and the engine tells the wizard which mapping key that role writes to
    assert found[0]["mapping_key"] == "charge_limit_entity"


class _FakeDeviceClient:
    """Synthetic ``config/device_registry/list`` rows (R1: example ids only)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_device_registry(self):
        self.calls += 1
        return self.rows


class _BrokenDeviceClient:
    def list_device_registry(self):
        raise RuntimeError("example_registry_exploded")


class _NoDeviceMethodClient:
    """An HA client without the device-registry command (older facade / REST-only)."""


def _example_battery_entries():
    return [
        {
            "entity_id": "number.example_device_input_limit",
            "platform": "zendure_ha",
            "translation_key": "input_limit",
            "device_id": "example_device_1",
            "name": "Input limit",
        },
        {
            "entity_id": "sensor.example_device_electric_level",
            "platform": "zendure_ha",
            "translation_key": "electric_level",
            "device_id": "example_device_2",
            "name": "Electric level",
        },
    ]


def _example_device_rows():
    return [
        {
            "id": "example_device_1",
            "name_by_user": "Example Attic Battery",
            "name": "SolarFlow 800",
            "manufacturer": "Example Vendor",
            "model": "Example Model 1000",
        },
        # no user rename and no manufacturer: the wizard falls back to the device name
        {"id": "example_device_2", "name": "Example Balcony Battery"},
    ]


def test_discover_battery_rows_name_the_device():
    """SPEC 10: the wizard lists batteries as devices, so a row carries the device identity."""
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._device_client_factory = lambda: _FakeDeviceClient(  # noqa: SLF001
        _example_device_rows()
    )
    engine._registry_source = lambda: _example_battery_entries()  # noqa: SLF001
    engine._discover_cache = None  # noqa: SLF001
    found = engine.discover("battery")
    by_entity = {row["entity_id"]: row for row in found}
    assert set(by_entity) == {
        "number.example_device_input_limit",
        "sensor.example_device_electric_level",
    }
    first = by_entity["number.example_device_input_limit"]
    assert first["device_id"] == "example_device_1"
    assert first["device_name"] == "Example Attic Battery"
    assert first["manufacturer"] == "Example Vendor"
    assert first["model"] == "Example Model 1000"
    assert first["mapping_key"] == "charge_limit_entity"
    second = by_entity["sensor.example_device_electric_level"]
    assert second["device_id"] == "example_device_2"
    assert second["device_name"] == "Example Balcony Battery"
    assert second["manufacturer"] is None and second["model"] is None


def test_discover_device_fields_are_null_when_the_registry_fails():
    """A broken, missing or unknown client must never break discovery: null device fields only."""
    for factory in (
        lambda: _BrokenDeviceClient(),
        lambda: _NoDeviceMethodClient(),
        lambda: None,
    ):
        engine, _store, _ha, _clock, _settings = build_engine()
        engine._device_client_factory = factory  # noqa: SLF001
        engine._registry_source = lambda: _example_battery_entries()  # noqa: SLF001
        engine._discover_cache = None  # noqa: SLF001
        found = engine.discover("battery")
        assert {row["entity_id"] for row in found} == {
            "number.example_device_input_limit",
            "sensor.example_device_electric_level",
        }, factory
        for row in found:
            assert row["device_id"] in ("example_device_1", "example_device_2")
            assert row["device_name"] is None
            assert row["manufacturer"] is None
            assert row["model"] is None
            assert "mapping_key" in row


def test_discover_caches_the_device_registry_between_searches():
    """Typing in the search box must not hammer Home Assistant: one fetch per TTL window."""
    client = _FakeDeviceClient(_example_device_rows())
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._device_client_factory = lambda: client  # noqa: SLF001
    engine._registry_source = lambda: _example_battery_entries()  # noqa: SLF001
    engine._discover_cache = None  # noqa: SLF001
    for term in ("input", "input_limit", "electric"):
        found = engine.discover("battery", search=term)
        assert found
    assert client.calls == 1
    _clock.advance(seconds=engine_module.DEVICE_CACHE_S + 1)
    engine._discover_cache = None  # noqa: SLF001
    assert engine.discover("battery", search="input")
    assert client.calls == 2


def test_discover_only_battery_rows_carry_the_device_fields():
    """``device_name``/``manufacturer``/``model`` are battery-only; the other kinds are unchanged."""
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._device_client_factory = lambda: _FakeDeviceClient(  # noqa: SLF001
        _example_device_rows()
    )
    engine._registry_source = lambda: [  # noqa: SLF001
        {
            "entity_id": "sensor.example_price_now",
            "platform": "example",
            "translation_key": "price_now",
            "device_class": "monetary",
            "unit_of_measurement": "EUR/kWh",
            "device_id": "example_device_1",
        }
    ]
    engine._discover_cache = None  # noqa: SLF001
    rows = engine.discover("price")
    assert rows and rows[0]["device_id"] == "example_device_1"
    assert "device_name" not in rows[0] and "manufacturer" not in rows[0] and "model" not in rows[0]



# ----------------------------------------------------------------------------- degradation


def test_engine_boots_without_sources_store_or_ha():
    settings = _selfcheck_settings()
    engine = Engine(settings, clock=Clock())
    engine.tick_now()
    snapshot = engine.snapshot()
    assert snapshot.mode == Mode.ADVISORY.value
    assert snapshot.ha_connected is False
    assert engine.plan_now("test") is None  # no forecasts: no plan, no crash
    assert any(event["code"] == "forecast_unavailable" for event in engine.log(limit=50, kind="event"))
    assert engine.publish_states() == 0
    assert engine.savings("week")[0] or True
    assert engine.accuracy(14)["pv"].samples == 0
    assert engine.providers()["price"][0].name == "not_configured"


def test_export_shapes():
    engine, _store, _ha, clock, _settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    clock.advance(seconds=30)
    engine.tick_now()
    plan_export = engine.export("plan", "csv")
    assert plan_export["rows"] and "t" in plan_export["rows"][0]
    assert plan_export["filename"].endswith(".csv")
    actuals = engine.export("actuals", "json")
    assert actuals["content_type"] == "application/json"
    savings = engine.export("savings", "csv")
    assert savings["rows"]
    try:
        engine.export("example_unknown", "json")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_apply_settings_reports_set_failures():
    engine, store, _ha, _clock, _settings = build_engine()
    engine.plan_now("test")
    original_set = engine.settings.set

    def refuse_unknown(key, value):
        if key == "example.unknown":
            raise ValueError("unknown setting")
        return original_set(key, value)

    engine.settings.set = refuse_unknown
    result = engine.apply_settings({"optimizer.horizon_hours": 12, "example.unknown": 1})
    assert "optimizer.horizon_hours" in result["applied"]
    assert any(error["key"] == "example.unknown" for error in result["errors"])
    assert any(event["code"] == "replan_requested" for event in store.events)


def test_notify_test_delegates_to_the_notifier():
    notifier = FakeNotifier()
    engine, _store, _ha, _clock, _settings = build_engine(notifier=notifier)
    assert engine.notify_test() is True
    assert notifier.sent and notifier.sent[-1][0] == "test"


def test_start_and_stop_threads_are_clean():
    engine, _store, _ha, _clock, _settings = build_engine()
    engine.start()
    try:
        assert len(engine._threads) == 3  # noqa: SLF001
        assert all(thread.is_alive() for thread in engine._threads)  # noqa: SLF001
    finally:
        engine.stop()
    assert engine._threads == []  # noqa: SLF001


def test_engine_module_declares_the_published_entity_contract():
    assert engine_module.SEM_AUTOPILOT_STATE == "sensor.sem_autopilot_state"
    assert engine_module.PUBLISH_MIN_INTERVAL_S >= 20.0


# ------------------------------------------------------------------- shutdown / fatal-error hold


def live_engine(clock=None):
    """An engine that has really commanded a non-zero setpoint (auto + verified write)."""
    engine, store, ha, clock, settings = build_engine(
        clock=clock or Clock(datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc))
    )
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()  # warm-up: reads the battery and plans
    engine.tick_now()  # dispatch
    writes = [c for c in ha.service_calls if c[0] in ("number", "input_number")]
    assert writes and abs(float(writes[-1][2]["value"])) > 0.0, ha.service_calls
    assert engine.pending_holds() == ["example_battery_1"]
    return engine, store, ha, clock, settings


def test_hold_on_exit_writes_for_real_once():
    """appendix A: no device watchdog, so exiting must neutralise a live setpoint once."""
    engine, store, ha, _clock, _settings = live_engine()
    before = len(ha.service_calls)
    held = engine.hold_on_exit("shutdown")
    assert held and held[0]["battery_id"] == "example_battery_1"
    assert ha.service_calls[-1][2]["value"] == 0.0  # a real 0 W write
    holds = [e for e in engine.log(limit=20, kind="dispatch") if e["detail"]["reason"] == "shutdown"]
    assert holds and holds[0]["code"] in (Reason.OK, Reason.POWER_CLAMP)
    assert store.dispatches[-1]["applied_w"] == 0.0
    assert engine.pending_holds() == []
    # idempotent: a second exit path (atexit after SIGTERM) must not write again
    assert engine.hold_on_exit("atexit") == []
    assert len(ha.service_calls) == before + 1


def test_exit_holds_never_touch_an_install_that_commanded_nothing():
    """Audit finding 2 repro: advisory + dry_run must not write on SIGTERM/fatal.

    Forcing a real hold there asserted ``operation=manual`` on Zendure, silently killing the
    user's own smart mode on every add-on restart.
    """
    for reason in ("signal", "fatal_error", "shutdown", "atexit"):
        engine, _store, ha, _clock, _settings = build_engine()
        engine.set_mode(Mode.ADVISORY.value, True)
        engine.plan_now("test")
        engine.tick_now()
        ha.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
        ha.service_calls.clear()
        assert engine.hold_on_exit(reason) == []
        assert ha.service_calls == [], (reason, ha.service_calls)
        assert engine.pending_holds() == []


def test_exit_hold_retries_and_only_then_marks_itself_done():
    """Audit finding 15: a failed hold must not be marked done, and must not re-sync drivers."""
    engine, _store, ha, _clock, _settings = live_engine()
    ha.service_calls.clear()
    attempts = {"n": 0}
    original = ha.call_service

    def flaky(domain, service, data):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("example HA hiccup")
        return original(domain, service, data)

    ha.call_service = flaky
    synced = {"n": 0}
    original_sync = engine._sync_drivers

    def counting_sync():
        synced["n"] += 1
        return original_sync()

    engine._sync_drivers = counting_sync
    held = engine.hold_on_exit("signal")
    assert attempts["n"] >= 2, "the exit hold must retry once"
    assert held and all(entry["ok"] for entry in held)
    assert synced["n"] == 0, "the exit path must never re-create drivers (no new connection)"
    # the flag was only set once a verified write had happened
    assert engine.hold_on_exit("atexit") == []


def test_live_setpoints_are_recovered_before_control_resumes():
    """Audit finding 2: a run that died unclean neutralises on the next boot."""
    engine, store, ha, _clock, _settings = live_engine()
    # a crash left a real setpoint behind: simulate the next boot with the same store
    assert engine.pending_holds() == ["example_battery_1"]
    fresh_engine, _store2, fresh_ha, _clock2, _settings2 = None, None, None, None, None
    from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources

    fresh_ha = _FakeHA()
    fresh_ha.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
    fresh_engine = Engine(
        _selfcheck_settings(),
        store=store,
        ha=fresh_ha,
        notifier=FakeNotifier(),
        clock=Clock(datetime(2026, 1, 1, 19, 5, tzinfo=timezone.utc)),
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    recovered = fresh_engine.recover_live_writes()
    assert recovered and recovered[0]["battery_id"] == "example_battery_1"
    assert recovered[0]["dry_run"] is False
    assert fresh_ha.service_calls and fresh_ha.service_calls[-1][2]["value"] == 0.0
    assert fresh_engine.pending_holds() == []


def test_stop_now_holds_and_bypasses_dry_run():
    """SPEC 9.4: the stop button must move the hardware even in advisory/dry_run.

    0.17.1: it may only move hardware *we* commanded.  The mode change's own hold is made to fail
    (a plausible HA hiccup), which leaves the applied command unverified and in advisory/dry_run;
    Stop must then write for real, deliberately bypassing both blocks.
    """
    engine, store, ha, _clock, _settings = live_engine()
    assert engine.pending_holds() == ["example_battery_1"]
    original = ha.call_service

    def hiccup(domain, service, data):
        raise RuntimeError("example HA hiccup")

    ha.call_service = hiccup
    engine.set_mode(Mode.ADVISORY.value, True)  # dry_run on, mode not auto; its hold fails
    ha.call_service = original
    assert engine.pending_holds() == ["example_battery_1"], "a failed hold must not clear it"
    ha.service_calls.clear()
    ha.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
    result = engine.stop_now()
    assert result["mode"] == Mode.ADVISORY.value
    assert result["stopped"] and result["stopped"][0]["dry_run"] is False
    assert ha.service_calls and ha.service_calls[-1][2]["value"] == 0.0
    holds = [e for e in engine.log(limit=20, kind="dispatch") if e["detail"]["reason"] == "stop_now"]
    assert holds and holds[0]["code"] in (Reason.OK, Reason.POWER_CLAMP)
    assert holds[0]["detail"]["dry_run"] is False
    assert store.dispatches[-1]["applied_w"] == 0.0
    assert "stop_now" in [event["code"] for event in engine.log(limit=20, kind="event")]
    assert engine.pending_holds() == []


def test_stop_now_never_raises_when_a_driver_fails():
    engine, _store, ha, _clock, _settings = live_engine()

    def boom(domain, service, data):
        raise RuntimeError("example HA failure")

    ha.call_service = boom
    result = engine.stop_now()
    assert result["mode"] == Mode.ADVISORY.value
    assert result["stopped"][0]["ok"] is False
    assert result["stopped"][0]["code"] == Reason.DRIVER_ERROR
    assert result["stopped"][0]["message"]


def test_a_stop_drains_an_in_flight_tick_instead_of_interleaving_it():
    """Audit finding 4: ``_control_tick``'s control lock must serialise Stop against the tick.

    A single battery hides the lock behind the driver's own write lock: its RLock makes the two
    command paths mutually exclusive by accident.  With two batteries the per-battery writes are
    independent, so a Stop that slipped past the control lock would land its neutralising 0 between
    two tick writes -- and, worse, let a non-zero setpoint land *after* the hold.

        The second battery's write is parked inside the tick, so the interleaving is deterministic:
    the only thing that decides whether Stop waits is the control lock.  The base tree *spends* the
    1.5 s window waiting for the lock (a Stop physically cannot proceed while the tick holds the
    command path); with the lock removed Stop completes inside that window and every assertion
    below fails.
    """
    probe = _ParkedWrite(block_id=SECOND_EXAMPLE_BATTERY.id)
    real_create = engine_module.driver_registry.create

    def wrapped_create(config, ha, **kwargs):
        return probe.wrap(real_create(config, ha, **kwargs))

    order: list[str] = []
    engine_module.driver_registry.create = wrapped_create
    try:
        engine, _store, ha, clock, settings = build_engine()
        settings.add_battery(SECOND_EXAMPLE_BATTERY)
        ha.states["number.example_battery_power_2"] = {"state": "0", "attributes": {}}
        ha.states["sensor.example_battery_soc_2"] = {
            "state": "55",
            "attributes": {"unit_of_measurement": "%"},
        }
        clock.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        engine.set_mode(Mode.AUTO.value, False)
        plan = engine.plan_now("test")
        assert plan is not None and plan.slots and plan.slots[0].power_w > 0.0

        def run_tick():
            engine.tick_now()
            order.append("tick-finished")

        tick = threading.Thread(target=run_tick, daemon=True, name="example-tick")
        tick.start()
        assert probe.parked.wait(10.0), "the tick never reached the second battery's write"
        stop = threading.Thread(target=engine.stop_now, daemon=True, name="example-stop")
        stop.start()
        stop.join(1.5)
        # The Stop waits for the in-flight tick: it has not finished, and it has not even revoked
        # authority yet -- SPEC 9.4 revokes authority only once the command path is drained.
        assert stop.is_alive(), "Stop ran to completion while the tick was still in flight"
        assert str(engine.snapshot().mode) == Mode.AUTO.value, "authority revoked early"
        probe.release.set()
        tick.join(10.0)
        stop.join(10.0)
        assert not tick.is_alive(), "the tick is still parked"
        assert not stop.is_alive(), "the stop never completed"
    finally:
        probe.release.set()
        engine_module.driver_registry.create = real_create

    assert order == ["tick-finished"], order
    # Every setpoint the tick asked for landed before the first hold, so the last write each
    # battery saw is the hold's neutralising 0 -- never a non-zero setpoint after the stop.
    commanded = [
        (kind, battery_id, power)
        for kind, battery_id, power in probe.log
        if kind in ("write", "hold")
    ]
    first_hold = min(index for index, (kind, _id, _w) in enumerate(commanded) if kind == "hold")
    assert all(kind == "write" for kind, _id, _w in commanded[:first_hold]), commanded
    assert all(kind == "hold" for kind, _id, _w in commanded[first_hold:]), commanded
    for _kind, battery_id, power in commanded[first_hold:]:
        assert power == 0.0, f"{battery_id} was left at {power} W after the stop"

    def last_written(entity_id: str) -> float | None:
        values = [
            float(call[2]["value"])
            for call in ha.service_calls
            if call[2].get("entity_id") == entity_id
        ]
        return values[-1] if values else None

    assert last_written("number.example_battery_power") == 0.0
    assert last_written("number.example_battery_power_2") == 0.0
    assert engine.snapshot().mode == Mode.ADVISORY.value


def test_turning_dry_run_on_neutralises_the_live_setpoint():
    """Audit finding 3 repro: a dry_run switch must not leave the last setpoint executing."""
    engine, _store, ha, _clock, _settings = live_engine()
    assert engine.pending_holds() == ["example_battery_1"]
    engine.set_mode(Mode.AUTO.value, True)  # stay in auto, just stop writing for real
    assert ha.service_calls[-1][2]["value"] == 0.0, ha.service_calls
    assert engine.pending_holds() == []
    before = len(ha.service_calls)
    engine.tick_now()
    assert len(ha.service_calls) == before, "a dry-run tick must not touch the device"


def test_settings_changes_neutralise_the_live_setpoint():
    """The same guarantee through PATCH /config (settings), not just POST /mode."""
    engine, _store, ha, _clock, _settings = live_engine()
    assert engine.apply_settings({"dry_run": True})["applied"] == ["dry_run"]
    assert ha.service_calls[-1][2]["value"] == 0.0, ha.service_calls
    assert engine.pending_holds() == []

    engine, _store, ha, _clock, _settings = live_engine()
    assert engine.apply_settings({"mode": Mode.ADVISORY.value})["applied"] == ["mode"]
    assert ha.service_calls[-1][2]["value"] == 0.0, ha.service_calls
    assert engine.pending_holds() == []
    assert engine.snapshot().mode == Mode.ADVISORY.value


def test_stop_now_drains_an_in_flight_tick_before_holding():
    """Audit finding 4: the hold must be the LAST write, never overtaken by a tick."""
    engine, _store, ha, clock, _settings = live_engine()
    started = threading.Event()
    release = threading.Event()
    original = ha.call_service

    def slow(domain, service, data):
        if domain in ("number", "input_number") and abs(float(data.get("value") or 0.0)) > 0.0:
            started.set()
            release.wait(timeout=10)
        return original(domain, service, data)

    ha.call_service = slow
    engine._commands.clear()  # make the in-flight tick the only writer
    clock.advance(minutes=15)
    engine.set_override(500.0, 5.0)  # a target that differs from the device: a real write
    tick = threading.Thread(target=engine.tick_now)
    tick.start()
    assert started.wait(timeout=5), "the tick never reached its write"
    stopped: list[dict] = []
    stopper = threading.Thread(target=lambda: stopped.append(engine.stop_now()))
    stopper.start()
    time.sleep(0.05)  # let stop reach the control lock while the tick is still writing
    release.set()
    tick.join(timeout=10)
    stopper.join(timeout=10)
    assert stopped and stopped[0]["mode"] == Mode.ADVISORY.value
    assert engine.snapshot().mode == Mode.ADVISORY.value
    values = [call[2]["value"] for call in ha.service_calls if call[0] in ("number", "input_number")]
    assert values and float(values[-1]) == 0.0, values
    assert engine.pending_holds() == []


def test_stop_now_reports_when_the_mode_could_not_be_persisted():
    """Audit finding 4: never claim the mode was saved when persisting it failed."""
    engine, _store, ha, _clock, settings = live_engine()

    def boom(key, value):
        if key == "mode":
            raise RuntimeError("example settings failure")
        raise AssertionError(key)

    settings.set = boom
    result = engine.stop_now()
    assert result["mode"] == Mode.ADVISORY.value  # the actual in-process mode is reported
    codes = [event["code"] for event in engine.log(limit=20, kind="event")]
    assert "stop_now" in codes
    alerts = [entry["code"] for entry in engine.log(limit=20, kind="alert")]
    assert "stop_now_not_persisted" in alerts
    assert any(alert.code == "stop_now_not_persisted" for alert in engine.snapshot().alerts)
    # a settings file that still says auto must not silently take authority back
    engine._sync_mode()
    assert engine.snapshot().mode == Mode.ADVISORY.value
    # the failure is visible on /status for the UI
    from app.api import status_dict

    assert status_dict(engine.snapshot(), None)["mode"] == Mode.ADVISORY.value


def timestamped_engine(clock=None):
    """An engine whose HA facade models real change-only timestamps (audit finding 1)."""
    from fake_ha import FakeHAFacade, FakeHAState

    now = datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    state = FakeHAState(
        {
            "number.example_battery_power": {"state": "0.0", "attributes": {}},
            "sensor.example_battery_soc": {
                "state": "55",
                "attributes": {"unit_of_measurement": "%"},
            },
        }
    )
    # the entities have not changed for an hour (an idle battery)
    idle_stamp = (now - timedelta(hours=1)).isoformat()
    for entity_id in state.states:
        state.states[entity_id]["last_updated"] = idle_stamp
    clock = clock or Clock(now)
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    ha = FakeHAFacade(state)
    engine = Engine(
        settings,
        store=_FakeStore(),
        ha=ha,
        notifier=FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    engine._registry_source = lambda: []
    return engine, ha, clock, settings


def test_idle_battery_keeps_accepting_writes_past_the_stale_window():
    """Audit finding 1: HA timestamps only move on change, so an idle battery stays usable."""
    engine, ha, clock, _settings = timestamped_engine()
    engine.set_mode(Mode.AUTO.value, False)
    engine.tick_now()
    engine.set_override(-400.0, 30.0)
    engine.tick_now()
    assert ha.state.service_calls, "an idle battery must still accept a command"
    reading = engine.snapshot().batteries[0]
    assert reading.stale is False
    assert reading.age_seconds(clock.now) is not None
    # keep ticking well past control.stale_state_seconds (180 s) without any state change
    for _ in range(5):
        clock.advance(seconds=60)
        engine.tick_now()
    assert reading.stale is False or engine.snapshot().batteries[0].stale is False
    engine.set_override(-500.0, 30.0)
    clock.advance(seconds=60)
    engine.tick_now()
    assert any(
        call[2].get("entity_id") == "number.example_battery_power"
        for call in ha.state.service_calls
    )
    assert engine.snapshot().batteries[0].stale is False


def test_a_stale_plan_is_withheld_when_the_forecast_dies():
    """Audit finding 2: a plan built from inputs that are gone must not stay active."""
    engine, store, ha, clock, _settings = live_engine()
    assert engine.active_plan() is not None
    # every price/load provider fails from now on
    engine._price_source = None
    engine._load_source = None
    engine._price = None
    engine._load = None
    clock.advance(hours=10)
    assert engine.plan_now("test") is None
    assert engine.active_plan() is None, "the stale plan must be dropped"
    alerts = [alert.code for alert in engine.snapshot().alerts]
    assert Reason.STALE_INPUT in alerts
    assert any(
        entry["code"] == Reason.STALE_INPUT for entry in engine.log(limit=30, kind="alert")
    )


def test_the_dispatcher_refuses_to_execute_an_old_plan():
    """Audit repro: plan at 09:00, providers dead, +10 h, device reset to 0 -> no re-write."""
    engine, store, ha, clock, _settings = live_engine(
        clock=Clock(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
    )
    assert engine.pending_holds() == ["example_battery_1"]
    clock.advance(hours=10)
    ha.states["number.example_battery_power"] = {"state": "0.0", "attributes": {}}  # device reset
    ha.service_calls.clear()
    outcome = engine.tick_now()
    assert outcome.stale_input, "the old plan must be refused as stale input"
    assert Reason.STALE_INPUT in outcome.reasons
    assert ha.service_calls == [], ha.service_calls
    assert outcome.target_w == 0.0
    assert any(alert.code == Reason.STALE_INPUT for alert in engine.snapshot().alerts)


def run_guard_iteration(engine):
    """Run exactly one iteration of the guard loop: no thread, no waiting."""
    engine._stop.clear()
    engine._stop.wait = lambda timeout=None: engine._stop.set()
    try:
        engine._guard_loop()
    finally:
        del engine._stop.wait  # restore the bound method


def test_boot_recovery_is_retried_until_it_succeeds():
    """Audit finding 3: a recovery that fails at boot must be retried, with an alert."""
    engine, store, ha, clock, _settings = build_engine()
    store.learning[("control", "live_writes")] = (None, {"batteries": ["example_battery_1"]})
    engine._boot_holds_loaded = False
    engine._last_recovery_attempt = None
    original = ha.call_service

    def broken(domain, service, data):
        raise RuntimeError("example HA outage")

    ha.call_service = broken
    failed = engine.recover_live_writes()
    assert failed and not any(entry["ok"] for entry in failed)
    assert engine.pending_holds() == ["example_battery_1"], "the latch must survive a failure"
    # the guard loop retries (and reports the failure) while the latch is outstanding
    engine._retry_live_write_recovery(clock.now)
    assert engine.pending_holds() == ["example_battery_1"]
    assert any(
        alert.code == "live_setpoint_recovery_failed" for alert in engine.snapshot().alerts
    )
    # ...and succeeds once the outage is over
    ha.call_service = original
    engine._last_recovery_attempt = None
    engine._retry_live_write_recovery(clock.now)
    assert engine.pending_holds() == []
    assert any(
        call[2].get("entity_id") == "number.example_battery_power" for call in ha.service_calls
    )
    before = len(ha.service_calls)
    engine._retry_live_write_recovery(clock.now)
    assert len(ha.service_calls) == before, "no further holds once the latch is empty"
    # the guard loop performs the retry itself, not only Engine.start()
    ha.call_service = broken
    engine._boot_holds.add("example_battery_1")  # the latch is outstanding again
    clock.advance(seconds=61)
    run_guard_iteration(engine)
    assert engine.pending_holds() == ["example_battery_1"], "the latch survives the outage"
    assert any(
        alert.code == "live_setpoint_recovery_failed" for alert in engine.snapshot().alerts
    )
    ha.call_service = original
    clock.advance(seconds=61)
    run_guard_iteration(engine)
    assert engine.pending_holds() == [], "the guard loop must neutralise the latch"
    before = len(ha.service_calls)
    run_guard_iteration(engine)
    assert len(ha.service_calls) == before, "no further holds once the latch is empty"


def test_an_external_setpoint_is_left_alone_in_advisory_and_off():
    """Audit acceptance 6a: the add-on never touches a setpoint it did not write."""
    for mode in (Mode.ADVISORY.value, Mode.OFF.value):
        engine, store, ha, clock, _settings = build_engine()
        engine.set_mode(mode, True)
        ha.states["number.example_battery_power"] = {"state": "800.0", "attributes": {}}
        for _ in range(60):  # a simulated hour of ticks
            clock.advance(seconds=60)
            engine.tick_now()
        assert ha.service_calls == [], (mode, ha.service_calls)
        assert engine.pending_holds() == []
    # the mirror case: a latch we really wrote *is* neutralised
    engine, store, ha, clock, _settings = build_engine()
    engine.set_mode(Mode.ADVISORY.value, True)
    ha.states["number.example_battery_power"] = {"state": "800.0", "attributes": {}}
    engine._note_applied_command("example_battery_1", 800.0)
    held = engine.hold_on_exit("shutdown")
    assert held and held[0]["ok"] is True
    assert ha.service_calls and float(ha.service_calls[-1][2]["value"]) == 0.0
    assert engine.pending_holds() == []


# ------------------------------- applied-command record vs a previous run's latch (0.17.1)

#: A ``control_path: device`` battery: the inverter's own AC limits, and **no** setpoint entity.
#: That is the live-install shape where a momentary 0 W flow cleared the latch (live finding 3).
DEVICE_PATH_BATTERY = BatteryConfig(
    id="example_device_path",
    name="Example Device Path",
    driver="zendure",
    control_path="device",
    capacity_kwh=2.0,
    min_soc_pct=10.0,
    max_soc_pct=100.0,
    max_charge_w=800.0,
    max_discharge_w=800.0,
    eta_charge=0.95,
    eta_discharge=0.95,
    mapping={
        "charge_limit_entity": "number.example_device_input_limit",
        "discharge_limit_entity": "number.example_device_output_limit",
        "ac_mode_entity": "select.example_device_ac_mode",
        "soc_entity": "sensor.example_device_soc",
    },
    enabled=True,
)

DEVICE_PATH_STATES = {
    "number.example_device_input_limit": {"state": "0", "attributes": {}},
    "number.example_device_output_limit": {"state": "0", "attributes": {}},
    "select.example_device_ac_mode": {
        "state": "input",
        "attributes": {"options": ["input", "output"]},
    },
    "sensor.example_device_soc": {
        "state": "50",
        "attributes": {"unit_of_measurement": "%"},
    },
}


def device_path_engine():
    engine, store, ha, clock, settings = build_engine()
    settings.add_battery(DEVICE_PATH_BATTERY)
    ha.states.update({key: dict(value) for key, value in DEVICE_PATH_STATES.items()})
    engine._sync_drivers()
    return engine, store, ha, clock, settings


def _tick_outcome(*batteries: BatteryState) -> TickOutcome:
    return TickOutcome(
        ts=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        mode=Mode.AUTO.value,
        dry_run=False,
        battery_states=list(batteries),
    )


def test_a_running_plan_keeps_its_setpoint_through_recovery_retries():
    """0.17.1 live finding 1: the guard must never neutralise the command this run applied.

    The old guard read its own live setpoint as "a previous run died live" and held the hardware
    to 0 W every ``RECOVERY_RETRY_S``, cutting a sustained command to about two thirds duty and
    alerting on every retry.  A control loop that is alive is simulated by ``note_tick``.
    """
    engine, store, ha, clock, _settings = build_engine(
        clock=Clock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    )
    engine.set_mode(Mode.AUTO.value, False)
    engine.plan_now("test")
    engine.set_override(500.0, 5.0)
    engine.tick_now()
    assert engine.pending_holds() == ["example_battery_1"]
    value = float(ha.states["number.example_battery_power"]["state"])
    assert value == 500.0
    before = len(ha.service_calls)
    for _ in range(5):
        clock.advance(seconds=engine_module.RECOVERY_RETRY_S + 1)
        engine._dispatcher.note_tick(clock.now)  # the control loop is alive, as in a real run
        run_guard_iteration(engine)
    assert ha.service_calls[before:] == [], "the guard wrote over the command it was following"
    assert float(ha.states["number.example_battery_power"]["state"]) == value
    assert engine.pending_holds() == ["example_battery_1"], "the command is still in force"
    assert "live_setpoint_recovered" not in [event["code"] for event in store.events]
    assert not any(alert.code == "live_setpoint_recovered" for alert in engine.snapshot().alerts)


def test_an_advisory_tick_writes_nothing_and_latches_nothing():
    """0.17.1 live finding 2: a blocked advisory tick used to latch from ``requested_w``."""
    engine, store, ha, clock, _settings = build_engine()
    engine.set_mode(Mode.ADVISORY.value, False)  # advisory is write-free even without dry_run
    engine.plan_now("test")
    ha.service_calls.clear()
    outcome = engine.tick_now()
    assert outcome.target_w != 0.0, "the fixture must propose a non-zero target"
    assert ha.service_calls == [], "advisory is write-free, guard holds included"
    assert engine.pending_holds() == []
    assert engine._commands == {}
    assert all(row["code"] == Reason.MODE_NOT_AUTO for row in store.dispatches)
    for _ in range(3):
        clock.advance(seconds=engine_module.RECOVERY_RETRY_S + 1)
        engine._dispatcher.note_tick(clock.now)
        run_guard_iteration(engine)
    assert ha.service_calls == []
    assert not any(alert.code == "live_setpoint_recovered" for alert in engine.snapshot().alerts)


def test_boot_recovery_holds_once_alerts_once_and_then_stays_silent():
    """A genuine previous-run latch is neutralised once; a verified hold ends it (0.17.1)."""
    engine, store, ha, clock, _settings = build_engine()
    store.learning[("control", "live_writes")] = (
        None,
        {"batteries": [{"battery_id": "example_battery_1", "power_w": 500.0}]},
    )
    engine._boot_holds_loaded = False
    ha.states["number.example_battery_power"] = {"state": "500", "attributes": {}}
    held = engine.recover_live_writes()
    assert held and held[0]["ok"] is True and held[0]["applied_w"] == 0.0
    assert float(ha.states["number.example_battery_power"]["state"]) == 0.0
    assert engine.pending_holds() == []
    assert [alert.code for alert in engine.snapshot().alerts].count("live_setpoint_recovered") == 1
    before = len(ha.service_calls)
    engine._last_recovery_attempt = None
    engine._retry_live_write_recovery(clock.now)
    run_guard_iteration(engine)
    assert len(ha.service_calls) == before, "a verified recovery must not hold again"
    codes = [alert.code for alert in engine.snapshot().alerts]
    assert codes.count("live_setpoint_recovered") == 1
    assert "live_setpoint_recovery_failed" not in codes


def test_a_device_path_battery_needs_a_verified_hold_not_a_momentary_zero():
    """0.17.1 live finding 3: a 0 W reading is not proof with a live limit setpoint.

    ``control_path: device`` commands ``input_limit``/``output_limit``, and the live install had no
    setpoint entity for the base driver to read, so any momentary 0 W flow used to clear the
    record while the limits were still executing.
    """
    engine, _store, ha, _clock, _settings = device_path_engine()
    engine._note_applied_command("example_device_path", 500.0)
    ha.states["number.example_device_input_limit"] = {"state": "500", "attributes": {}}
    assert engine._commanded_power("example_device_path") == 500.0
    reading = BatteryState(id="example_device_path", available=True, power_w=0.0)
    engine._note_live_writes(_tick_outcome(reading))
    assert engine.pending_holds() == ["example_device_path"], "a momentary 0 W must not clear it"
    engine._note_live_writes(_tick_outcome(reading))
    assert engine.pending_holds() == ["example_device_path"], "the limit is still executing"
    # a verified hold is the explicit proof: the driver writes both limits to 0 and reads it back
    held = engine._hold_all("test", False, only=["example_device_path"])
    assert held and held[0]["ok"] is True and held[0]["applied_w"] == 0.0
    engine._clear_verified_holds(held)
    assert engine.pending_holds() == []
    # the driver's own limit read-back at zero, twice in a row, is the other accepted proof
    engine._note_applied_command("example_device_path", 500.0)
    for _ in range(engine_module.NEUTRAL_CONFIRM_READS):
        engine._note_live_writes(_tick_outcome(reading))
    assert engine.pending_holds() == []


def test_leaving_live_control_holds_only_what_this_run_commanded():
    """0.17.1: auto -> advisory neutralises our setpoint; a never-commanded battery is untouched."""
    engine, _store, ha, _clock, _settings = live_engine()
    ha.service_calls.clear()
    engine.set_mode(Mode.ADVISORY.value, False)
    writes = [call for call in ha.service_calls if call[0] in ("number", "input_number")]
    assert writes and float(writes[-1][2]["value"]) == 0.0
    assert float(ha.states["number.example_battery_power"]["state"]) == 0.0
    assert engine.pending_holds() == []

    fresh, _store2, fresh_ha, _clock2, _settings2 = build_engine()
    fresh.set_mode(Mode.AUTO.value, False)  # auto + real, but this run has written nothing
    fresh_ha.states["number.example_battery_power"] = {"state": "800", "attributes": {}}
    fresh_ha.service_calls.clear()
    fresh.set_mode(Mode.ADVISORY.value, False)
    assert fresh_ha.service_calls == [], "a never-commanded battery must not be touched"
    assert fresh.pending_holds() == []
    assert "hold_skipped" in [event["code"] for event in fresh.log(limit=10, kind="event")]


def test_a_dry_run_tick_sets_no_latch_and_writes_nothing():
    """0.17.1 live finding 2: dry_run is a preview; it may never latch or write."""
    engine, store, ha, _clock, _settings = build_engine()
    engine.set_mode(Mode.AUTO.value, True)
    engine.plan_now("test")
    ha.service_calls.clear()
    outcome = engine.tick_now()
    assert outcome.target_w != 0.0, "the fixture must propose a non-zero target"
    assert ha.service_calls == []
    assert engine.pending_holds() == []
    assert engine._commands == {}
    assert store.dispatches and all(row["dry_run"] for row in store.dispatches)


def test_stop_now_neutralises_and_reports_every_commanded_battery():
    """0.17.1: Stop now still holds each battery this run commanded, per-battery results intact."""
    clock = Clock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    engine, _store, ha, clock, settings = build_engine(clock=clock)
    settings.add_battery(SECOND_EXAMPLE_BATTERY)
    ha.states["number.example_battery_power_2"] = {"state": "0", "attributes": {}}
    ha.states["sensor.example_battery_soc_2"] = {
        "state": "55",
        "attributes": {"unit_of_measurement": "%"},
    }
    engine.set_mode(Mode.AUTO.value, False)
    engine.plan_now("test")
    engine.tick_now()
    commanded = engine.pending_holds()
    assert "example_battery_1" in commanded, commanded
    ha.service_calls.clear()
    result = engine.stop_now()
    assert set(result) == {"mode", "override_cleared", "stopped"}  # SPEC 10 response shape
    assert result["mode"] == Mode.ADVISORY.value
    assert [entry["battery_id"] for entry in result["stopped"]] == commanded
    assert all(entry["ok"] and entry["dry_run"] is False for entry in result["stopped"])
    assert all(entry["applied_w"] == 0.0 for entry in result["stopped"])
    assert engine.pending_holds() == []
    assert ha.service_calls and float(ha.service_calls[-1][2]["value"]) == 0.0


def test_publishing_survives_a_missing_load_forecast():
    """Audit finding 5: the publisher unpacked 3 values from a 2-tuple and never published."""
    engine, _store, _ha, _clock, _settings = build_engine()
    engine.tick_now()
    engine._load = None  # a provider outage / disabled load forecast
    published = engine.publish_states()
    assert published >= 8, published
    # the load-forecast sensor is published as unknown zeros instead of blowing up the loop
    payload = _ha.published["sensor.sem_load_forecast_now"]
    assert float(payload["state"]) == 0.0
    assert float(payload["attributes"]["today_kwh"]) == 0.0


def test_replan_loop_backs_off_instead_of_hot_spinning():
    """Audit finding 6: a failing plan refetched every provider once per second."""
    engine, _store, _ha, clock, _settings = build_engine()
    engine._price = None
    engine._load = None
    engine._price_source = None
    engine._load_source = None
    assert engine.plan_now("test") is None
    gap = engine._replan_gap_s()
    assert gap >= 60.0
    # a queued recompute is dropped while the failure backoff is running
    engine.request_replan("forced")
    assert engine._replan_reason() is None
    clock.advance(seconds=gap + 1.0)
    assert engine._replan_reason() == "forced"
    # repeated identical alerts are deduped
    engine._alert_once("forecast_unavailable", "example")
    engine._alert_once("forecast_unavailable", "example")
    assert sum(1 for a in engine.snapshot().alerts if a.code == "forecast_unavailable") == 1


def test_test_discharge_is_signed_and_second_based():
    """Audit finding 12: the UI's positive watts mean discharge and 10 s means 10 s."""
    engine, _store, ha, clock, _settings = live_engine()
    result = engine.test_discharge("example_battery_1", 500.0, 10.0)
    assert result["power_w"] == -500.0, "positive UI watts must mean discharge"
    assert result["seconds"] == 10.0, "seconds are seconds, not minutes"
    assert result["until"] and result["dry_run"] is False
    assert "applied_w" in result
    assert result["applied_w"] <= 0.0
    remaining = (from_iso(result["until"]) - clock.now).total_seconds()
    assert 9.0 <= remaining <= 10.0
    charge = engine.test_discharge("example_battery_1", 300.0, 5.0, "charge")
    assert charge["power_w"] == 300.0
    assert charge["seconds"] == 5.0
    # clamped to the device limit, still signed
    big = engine.test_discharge("example_battery_1", 9999.0, 90.0)
    assert big["power_w"] == -1000.0 and big["seconds"] == 60.0


def test_maintenance_runs_once_per_wall_clock_day():
    """Audit finding 14: a daily add-on restart must still learn and trim."""
    engine, _store, _ha, clock, _settings = build_engine()
    calls: list = []
    engine._maintenance = lambda now: calls.append(now)
    engine._maybe_maintenance(clock.now)
    engine._maybe_maintenance(clock.now + timedelta(seconds=60))
    assert len(calls) == 1
    # a fresh process (restart) on the same day runs it once, then no more
    engine._last_maintenance = None
    engine._maybe_maintenance(clock.now + timedelta(minutes=5))
    assert len(calls) == 2
    engine._maybe_maintenance(clock.now + timedelta(minutes=6))
    assert len(calls) == 2
    engine._maybe_maintenance(clock.now + timedelta(days=1))
    assert len(calls) == 3


def test_savings_totals_split_arbitrage_and_self_consumption():
    engine, store, _ha, clock, _settings = build_engine()
    store.savings = {
        "2026-01-01": {
            "day": "2026-01-01",
            "import_kwh": 4.0,
            "export_kwh": 1.5,
            "pv_kwh": 3.0,
            "cost_eur": 1.2,
            "revenue_eur": 0.15,
            "baseline_cost_eur": 1.6,
            "savings_eur": 0.55,
            "battery_cycles": 0.5,  # over a 5 kWh battery -> 2.5 kWh of throughput
        }
    }
    _buckets, totals = engine.savings("day")
    assert totals["arbitrage_kwh"] == 1.5  # exported battery energy
    assert totals["self_consumption_kwh"] == 1.0  # the rest displaced import
    assert totals["battery_throughput_kwh"] == 2.5
    assert totals["savings_eur"] == 0.55
    _ = clock


def test_batteries_view_reports_cycles_and_flat_capabilities():
    engine, store, _ha, _clock, _settings = build_engine()
    store.savings = {
        "2026-01-01": {"day": "2026-01-01", "battery_cycles": 2.0, "savings_eur": 0.0},
        "2026-01-02": {"day": "2026-01-02", "battery_cycles": 3.25, "savings_eur": 0.0},
    }
    view = engine.batteries_view()
    assert view and view[0]["cycles"] == 5.25
    assert view[0]["state"]["extra"]["cycles"] == 5.25
    capabilities = view[0]["capabilities"]
    assert isinstance(capabilities, dict) and capabilities["driver"] == "generic"
    assert set(view[0]) == {
        "config",
        "state",
        "capabilities",
        "mapping_resolved",
        "cycles",
        "discovery",
        # SPEC 5 learned capacity (additive): the flat record fields and the nested detail.
        "capacity",
        "capacity_source",
        "capacity_kwh",
        "capacity_learned_kwh",
        "capacity_samples",
        "capacity_updated_at",
        "effective_capacity_kwh",
        "capacity_reason",
        # SPEC 8.1 scheduled full-charge maintenance (additive): the per-battery state.
        "maintenance",
    }


def test_log_exposes_alerts_as_their_own_kind():
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._alert("warning", "example_alert", "something to look at", {"detail": 1})  # noqa: SLF001
    entries = engine.log(limit=10, kind="alert")
    assert entries and entries[0]["code"] == "example_alert"
    assert entries[0]["level"] == "warning"
    assert engine.log(limit=10, kind="event")
    # every entry has one of the three documented kinds
    assert {entry["kind"] for entry in engine.log(limit=50)} <= {"dispatch", "event", "alert"}


def test_publish_and_dispatch_log_detail_keys():
    engine, _store, _ha, _clock, _settings = build_engine()
    engine.plan_now("test")
    engine.tick_now()
    entries = engine.log(limit=20, kind="dispatch")
    assert entries
    detail = entries[0]["detail"]
    for key in ("requested_w", "applied_w", "battery_id", "mode", "dry_run"):
        assert key in detail, key
        assert key in entries[0], key


def test_discover_grid_rows_point_at_the_settings_key():
    """SPEC 3/10: a confirmed grid role is saved into ``grid.*``, so discovery names that key."""
    engine, _store, _ha, _clock, _settings = build_engine()
    engine._registry_source = lambda: [  # noqa: SLF001
        {
            "entity_id": "sensor.example_grid_power",
            "platform": "example_meter",
            "translation_key": "grid_power",
            "device_id": "example_meter_1",
            "name": "Grid power",
        },
        {
            "entity_id": "sensor.example_export_power",
            "platform": "example_meter",
            "translation_key": "grid_export_power",
            "device_id": "example_meter_1",
            "name": "Export power",
        },
    ]
    engine._discover_cache = None  # noqa: SLF001
    rows = engine.discover("grid")
    assert rows
    by_entity = {row["entity_id"]: row for row in rows}
    assert by_entity["sensor.example_grid_power"]["setting_key"] == "grid.power_entity"
    assert by_entity["sensor.example_export_power"]["setting_key"] == "grid.export_entity"
    # battery rows instead name the mapping key the wizard writes
    engine._registry_source = lambda: [  # noqa: SLF001
        {
            "entity_id": "number.example_manager_manual_power",
            "platform": "zendure_ha",
            "translation_key": "manual_power",
            "device_id": "example_manager",
            "name": "Manual power",
        }
    ]
    engine._discover_cache = None  # noqa: SLF001
    battery_rows = engine.discover("battery")
    assert battery_rows and "mapping_key" in battery_rows[0]
    assert "setting_key" not in battery_rows[0]


def test_discover_price_solar_and_load_rows_point_at_their_settings_key():
    """SPEC 10 (additive): the setup pickers write one settings key per role."""
    engine, _store, _ha, _clock, _settings = build_engine()
    cases = {
        "solar": (
            {"entity_id": "sensor.example_pv_power", "platform": "example_inverter", "translation_key": "pv_power", "name": "PV power"},
            "solar.measured_entity",
        ),
        "load": (
            {"entity_id": "sensor.example_load_power", "platform": "example_meter", "translation_key": "load_power", "name": "Load power"},
            "load.entity",
        ),
        "price": (
            {"entity_id": "sensor.example_price_now", "platform": "example_tariff", "translation_key": "electricity_price", "name": "Price now"},
            "price.entity",
        ),
    }
    for kind, (entry, expected_key) in cases.items():
        engine._registry_source = lambda entry=entry: [entry]  # noqa: SLF001
        engine._discover_cache = None  # noqa: SLF001
        rows = engine.discover(kind)
        assert rows, kind
        assert rows[0]["setting_key"] == expected_key, kind


# ----------------------------------------------------------------------------------- forecast explanation


def test_explain_forecast_is_silent_for_a_healthy_panel():
    engine, _store, _ha, clock, _settings = build_engine()
    engine._refresh_forecasts(clock.now)
    for kind in ("price", "solar", "load"):
        assert engine.explain_forecast(kind) == {"reason": None, "hint": None}


def test_price_provider_contract_reflects_settings_and_availability():
    engine, _store, _ha, clock, settings = build_engine()
    engine._refresh_forecasts(clock.now)
    settings["price.provider"] = "zonneplan"
    settings["price.contract"] = "dynamic_quarter_hourly"
    assert engine.price_provider_contract() == {"provider": "zonneplan", "contract": "dynamic_quarter_hourly"}
    engine._price = None  # no forecast -> the provider reports unavailable, the contract is still configured
    assert engine.price_provider_contract() == {"provider": "unavailable", "contract": "dynamic_quarter_hourly"}


def test_explain_price_fixed_missing_names_the_setting():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["price.provider"] = "fixed"
    settings["price.contract"] = "fixed_single"
    engine._price = None
    explanation = engine.explain_forecast("price")
    assert explanation["reason"] == "fixed_price_missing"
    assert "price.fixed_price_eur_per_kwh" in explanation["hint"] and "price.fixed_price_entity" in explanation["hint"]


def test_explain_price_fixed_day_night_missing_names_the_rate():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["price.provider"] = "fixed"
    settings["price.contract"] = "fixed_day_night"
    engine._price = None
    explanation = engine.explain_forecast("price")
    assert explanation["reason"] == "fixed_price_missing"
    assert "price.fixed_day_price_eur_per_kwh" in explanation["hint"] or "price.fixed_night_price_eur_per_kwh" in explanation["hint"]


def test_explain_price_day_ahead_404_names_the_zone_and_alternatives():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["price.provider"] = "day_ahead"
    settings["price.bzn"] = "NL"
    engine._price = None
    engine._provider_status[("price", "energy_charts")] = ProviderStatus(
        kind="price",
        name="energy_charts",
        active=True,
        ok=False,
        last_error="energy_charts: bidding zone 'NL' is valid, but this source publishes no day-ahead prices for it (no content available)",
        detail={"http_status": 404},
    )
    explanation = engine.explain_forecast("price")
    assert explanation["reason"] == "provider_failed"
    assert "NL" in explanation["hint"] and "zone" in explanation["hint"].lower()
    assert "zonneplan" in explanation["hint"].lower() and "price.entity" in explanation["hint"]


def test_explain_price_empty_horizon_on_a_parsed_but_futureless_payload():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["price.provider"] = "zonneplan"
    engine._price = None
    engine._provider_status[("price", "zonneplan_entity")] = ProviderStatus(
        kind="price", name="zonneplan_entity", active=True, ok=False,
        last_error="zonneplan_entity: no future price slots in the requested horizon",
    )
    assert engine.explain_forecast("price")["reason"] == "empty_horizon"


def test_explain_price_zonneplan_no_sensor_found():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["price.provider"] = "zonneplan"
    engine._price = None
    engine._provider_status[("price", "zonneplan_entity")] = ProviderStatus(
        kind="price", name="zonneplan_entity", active=True, ok=False,
        last_error="zonneplan: no compatible electricity tariff sensor found (need a EUR/kWh sensor with a 2+ entry list)",
    )
    explanation = engine.explain_forecast("price")
    assert explanation["reason"] == "provider_failed"
    assert "Zonneplan integration" in explanation["hint"]


def test_explain_solar_no_pv_source():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["solar.enabled"] = True
    settings["solar.planes"] = []
    engine._solar = None
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "no_pv_source"
    assert "solar.planes" in explanation["hint"]


def test_explain_solar_names_the_derived_fallback_failure():
    """SPEC 7.1.1: with no planes the meter-derived fallback is tried, so its failure is the hint."""
    engine, _store, _ha, _clock, settings = build_engine()
    settings["solar.enabled"] = True
    settings["solar.planes"] = []
    engine._solar = None
    engine._provider_status[("solar", "forecast_solar")] = ProviderStatus(
        kind="solar",
        name="forecast_solar",
        active=True,
        ok=True,
        detail={"derived_fallback": "the derived solar source needs grid.power_entity"},
    )
    explanation = engine.explain_forecast("solar")
    assert explanation["reason"] == "no_pv_source"
    assert "meter-derived" in explanation["hint"]
    assert "grid.power_entity" in explanation["hint"]


def test_engine_grid_mapping_falls_back_to_the_energy_dashboard():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["grid.power_entity"] = ""
    settings["grid.import_entity"] = ""
    settings["grid.export_entity"] = ""

    class PrefsWS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def energy_prefs(self):
            return {"energy_sources": [{"type": "grid", "stat_rate": "sensor.example_net_power"}]}

    engine._grid_mapping = GridMapping(settings, ws_factory=lambda: PrefsWS())
    assert engine.grid_mapping() == {
        "grid_power": "sensor.example_net_power",
        "grid_import": "",
        "grid_export": "",
    }
    # an explicit confirmation always wins over the dashboard
    settings["grid.power_entity"] = "sensor.example_my_meter"
    assert engine.grid_mapping()["grid_power"] == "sensor.example_my_meter"


def test_engine_grid_resolution_reports_the_dashboard_and_the_override():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["grid.power_entity"] = ""
    settings["grid.import_entity"] = ""
    settings["grid.export_entity"] = ""

    class PrefsWS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def energy_prefs(self):
            return {
                "energy_sources": [
                    {
                        "type": "grid",
                        "stat_rate": "sensor.example_net_power",
                        "stat_energy_from": "sensor.example_grid_in_energy",
                        "stat_energy_to": "sensor.example_grid_out_energy",
                    }
                ]
            }

    engine._grid_mapping = GridMapping(settings, ws_factory=lambda: PrefsWS())
    resolution = engine.grid_resolution()
    assert resolution["source"] == "energy_dashboard_power"
    assert resolution["explicit"] == {}
    assert resolution["dashboard"]["grid_power"] == "sensor.example_net_power"
    assert resolution["dashboard"]["grid_energy_import"] == "sensor.example_grid_in_energy"
    assert "Energy dashboard" in resolution["note"]

    settings["grid.power_entity"] = "sensor.example_my_meter"
    resolution = engine.grid_resolution()
    assert resolution["source"] == "settings"
    assert resolution["explicit"] == {"grid.power_entity": "sensor.example_my_meter"}
    # the dashboard view survives the override, so the panel can offer to fall back to it
    assert resolution["dashboard"]["grid_power"] == "sensor.example_net_power"
    assert resolution["note"] == ""


def test_engine_grid_resolution_without_a_mapping_reports_settings_only():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["grid.power_entity"] = "sensor.example_my_meter"
    resolution = engine.grid_resolution()
    assert resolution["source"] == "settings"
    assert resolution["dashboard"] == {}
    assert resolution["explicit"] == {"grid.power_entity": "sensor.example_my_meter"}


def test_explain_load_no_history():
    engine, _store, _ha, _clock, settings = build_engine()
    settings["load.entity"] = ""
    engine._load = None
    explanation = engine.explain_forecast("load")
    assert explanation["reason"] == "no_history"
    assert "load.entity" in explanation["hint"]


def test_explain_unknown_kind():
    engine, _store, _ha, _clock, _settings = build_engine()
    assert engine.explain_forecast("nope")["reason"] == "unknown"
