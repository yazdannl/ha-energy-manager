"""Bounded historical plan/dispatch evidence; unmatched rows are never guessed into a plan."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.api import ApiServer  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.models import Action, Plan, PlanSlot, PlanSummary  # noqa: E402
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _plan(generated: datetime, start: datetime) -> Plan:
    return Plan(
        generated_at=generated,
        horizon_start=start,
        horizon_end=start + timedelta(minutes=30),
        slot_minutes=15,
        feasible=True,
        slots=[
            PlanSlot(
                t=start,
                buy=0.1,
                sell=0.05,
                pv_kw=0.2,
                load_kw=0.8,
                soc_kwh=5.0,
                power_w=600.0,
                action=Action.CHARGE,
                reason="arbitrage_charge",
                minutes=15.0,
            ),
            PlanSlot(
                t=start + timedelta(minutes=15),
                buy=0.2,
                sell=0.05,
                pv_kw=0.1,
                load_kw=0.8,
                soc_kwh=5.1,
                power_w=0.0,
                action=Action.HOLD,
                reason="reserve_hold",
                minutes=15.0,
            ),
        ],
        summary=PlanSummary(),
        sources={"price": "example_price", "solar": "example_solar", "load": "example_load"},
        mode="auto",
        dry_run=False,
    )


def _history_fixture(path: Path):
    store = Store(path)
    start = NOW - timedelta(minutes=30)
    historical_id = store.save_plan(_plan(NOW - timedelta(minutes=35), start))
    # A newer replan covers the same interval. Its dispatch row must not be joined to the old plan.
    active_id = store.save_plan(_plan(NOW - timedelta(minutes=20), start))
    engine = Engine({"mode": "advisory", "dry_run": True}, store=store, clock=lambda: NOW)
    engine._plan = store.load_plan(active_id)  # noqa: SLF001 - establish the active snapshot for history filtering

    store.add_dispatch(
        ts=start + timedelta(minutes=1), battery_id="battery_1", requested_w=600.0,
        applied_w=500.0, result="ok", code="ramp_limit", reason="ramp_limit",
        message="Limited by the per-tick ramp.", mode="auto", dry_run=False,
        plan_id=historical_id, plan_slot_ts=start,
    )
    store.add_dispatch(
        ts=start + timedelta(minutes=2), battery_id="battery_1", requested_w=600.0,
        applied_w=550.0, result="ok", code="ok", reason="arbitrage_charge",
        message="Newer overlapping replan.", mode="auto", dry_run=False,
        plan_id=active_id, plan_slot_ts=start,
    )
    # An inconsistent stored timestamp is not accepted as an exact slot link either.
    store.add_dispatch(
        ts=start + timedelta(minutes=4), battery_id="battery_1", requested_w=600.0,
        applied_w=0.0, result="ok", code="bad_link", reason="bad_link",
        message="Slot timestamp does not contain this dispatch time.", mode="auto", dry_run=False,
        plan_id=historical_id, plan_slot_ts=start + timedelta(minutes=15),
    )
    # A legacy event has the same timestamp relationship but no persisted plan identity.
    store.add_dispatch(
        ts=start + timedelta(minutes=3), battery_id="battery_1", requested_w=600.0,
        applied_w=0.0, result="ok", code="legacy", reason="legacy",
        message="Legacy row without plan/slot identity.", mode="advisory", dry_run=True,
    )
    for minute, battery_kw, grid_kw in (
        (2, 0.4, 1.0),
        (7, None, 0.9),
        (12, 0.6, None),
    ):
        store.upsert_actuals(
            start + timedelta(minutes=minute), battery_kw=battery_kw, grid_kw=grid_kw,
            soc_kwh=5.0 + minute / 100.0,
        )
    return store, engine, historical_id, active_id, start


def test_history_is_bounded_and_keeps_exact_links_separate_from_measurements(tmp_path):
    store, engine, historical_id, active_id, start = _history_fixture(tmp_path / "history.db")
    try:
        index = engine.plan_history(limit=1)
        assert index["limit"] == 1
        assert [row["plan_id"] for row in index["plans"]] == [historical_id]
        assert active_id not in [row["plan_id"] for row in index["plans"]]

        report = engine.plan_history_detail(historical_id, NOW)
        assert report is not None
        first, second = report["slots"]
        assert first["planned_setpoint_w"] == 600.0
        assert first["dispatch"]["records"] == 1
        battery = first["dispatch"]["batteries"][0]
        assert battery["attempts"] == 1
        assert battery["last"]["requested_w"] == 600.0
        assert battery["last"]["applied_w"] == 500.0
        assert first["actual"]["battery_kw"] == 0.5
        assert first["actual"]["grid_kw"] == 0.95
        assert first["actual"]["battery_coverage_pct"] == 100.0 * 2 * 5 / 15
        assert first["actual"]["grid_coverage_pct"] == 100.0 * 2 * 5 / 15
        assert second["dispatch"] is None and second["actual"] is None
        assert report["linkage"]["unmatched_dispatch_count"] == 2
        assert all(row["attribution"] == "unmatched" for row in report["linkage"]["unmatched_dispatches"])
        assert {row["plan_id"] for row in report["linkage"]["unmatched_dispatches"]} == {None, historical_id}
        assert report["linkage"]["unassigned_dispatches"] == 1
        assert report["linkage"]["measurement_note"].startswith("Measured actuals are aligned")
    finally:
        store.close()


def test_old_plan_with_unknown_slot_duration_does_not_claim_percentage_coverage(tmp_path):
    store = Store(tmp_path / "legacy-duration.db")
    start = NOW - timedelta(minutes=25)
    plan = _plan(NOW - timedelta(minutes=30), start)
    plan.slots[0].minutes = 0.0
    plan_id = store.save_plan(plan)
    store.upsert_actuals(start + timedelta(minutes=1), battery_kw=0.5, grid_kw=0.8)
    engine = Engine({"mode": "advisory", "dry_run": True}, store=store, clock=lambda: NOW)
    try:
        report = engine.plan_history_detail(plan_id, NOW)
        assert report is not None
        actual = report["slots"][0]["actual"]
        assert actual["battery_samples"] == 1 and actual["grid_samples"] == 1
        assert actual["battery_coverage_pct"] is None
        assert actual["grid_coverage_pct"] is None
    finally:
        store.close()


def test_history_api_exposes_bounded_index_and_one_plan_detail(tmp_path):
    store, engine, historical_id, _active_id, _start = _history_fixture(tmp_path / "api.db")
    try:
        api = ApiServer(engine, host="127.0.0.1", port=0)
        status, _headers, content = api.dispatch("GET", "/api/v1/plan/history?limit=1")
        assert status == 200
        index = json.loads(content)
        assert index["limit"] == 1
        assert [row["plan_id"] for row in index["plans"]] == [historical_id]

        status, _headers, content = api.dispatch(
            "GET", f"/api/v1/plan/history?plan_id={historical_id}"
        )
        assert status == 200
        report = json.loads(content)
        slot = report["slots"][0]
        assert slot["planned_setpoint_w"] == 600.0
        assert slot["dispatch"]["batteries"][0]["last"]["requested_w"] == 600.0
        assert slot["dispatch"]["batteries"][0]["last"]["applied_w"] == 500.0
        assert slot["actual"]["battery_kw"] == 0.5
        assert report["linkage"]["unmatched_dispatches"][0]["attribution"] == "unmatched"

        status, _headers, content = api.dispatch("GET", "/api/v1/plan/history?plan_id=0")
        assert status == 400
        status, _headers, content = api.dispatch("GET", "/api/v1/plan/history?plan_id=9999")
        assert status == 404
    finally:
        store.close()
