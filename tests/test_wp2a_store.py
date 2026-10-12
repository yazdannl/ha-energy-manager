"""WP2A: SQLite store schema, migrations, retention and helpers (SPEC.md section 11)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.models import (  # noqa: E402
    Action,
    DispatchResult,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
)
from app.store import MIGRATIONS, SCHEMA_VERSION, Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

EXPECTED_TABLES = {
    "schema_version",
    "plans",
    "plan_slots",
    "dispatch_log",
    "measurements",
    "actuals",
    "forecast_cache",
    "learning_profiles",
    "savings_daily",
    "events",
    "settings",
    "meta",
}


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "sem.db")
    yield instance
    instance.close()


def test_schema_matches_spec(store):
    rows = store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row["name"] for row in rows}
    assert tables == EXPECTED_TABLES
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store.get_meta("schema") is None
    version = store.connection.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_settings_and_meta(store):
    assert store.get_setting("price.source") is None
    store.set_setting("price.source", "static")
    store.set_settings({"mode": "auto", "dry_run": False})
    assert store.get_setting("price.source") == "static"
    assert store.get_settings() == {"price.source": "static", "mode": "auto", "dry_run": False}
    store.delete_setting("mode")
    assert store.get_setting("mode", "advisory") == "advisory"
    store.set_meta("last_maintenance", "2026-01-05")
    assert store.get_meta("last_maintenance") == "2026-01-05"


def test_plan_roundtrip(store):
    plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=36),
        slot_minutes=15,
        feasible=False,
        infeasible_reason="terminal target unreachable",
        slots=[
            PlanSlot(
                t=NOW,
                buy=0.2,
                sell=0.05,
                pv_kw=1.5,
                load_kw=0.4,
                soc_kwh=4.0,
                power_w=-800.0,
                action=Action.DISCHARGE,
                reason="peak_discharge",
            ),
            PlanSlot(
                t=NOW + timedelta(minutes=15),
                buy=0.25,
                sell=0.05,
                pv_kw=1.7,
                load_kw=0.4,
                soc_kwh=3.8,
                power_w=0.0,
                action=Action.HOLD,
                reason="reserve_hold",
            ),
        ],
        summary=PlanSummary(cost_estimate_eur=1.5, baseline_cost_eur=2.0, saved_eur=0.5, cycles=0.1),
        batteries=[{"id": "battery_1", "share_w": -800.0}],
        mode="auto",
        dry_run=False,
        sources={"price": "static", "solar": "forecast_solar"},
    )
    plan_id = store.save_plan(plan)
    assert plan_id > 0
    loaded = store.latest_plan()
    assert loaded is not None
    assert loaded.feasible is False
    assert loaded.infeasible_reason == "terminal target unreachable"
    assert loaded.dry_run is False
    assert loaded.mode == "auto"
    assert loaded.sources == {"price": "static", "solar": "forecast_solar"}
    assert loaded.summary.saved_eur == pytest.approx(0.5)
    assert [slot.reason for slot in loaded.slots] == ["peak_discharge", "reserve_hold"]
    assert loaded.slots[0].action is Action.DISCHARGE
    assert loaded.slot_minutes == 15
    assert store.load_plan(999) is None
    assert store.list_plans() == [plan_id]


def test_measurements_and_actuals_upsert(store):
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1000.0, unit="W")])
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1200.0, unit="W")])
    rows = store.measurements_range(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert len(rows) == 1 and rows[0]["value"] == 1200.0
    assert store.measurements_range(NOW, NOW + timedelta(minutes=1), roles=["grid_power"]) == []

    store.upsert_actuals(NOW, pv_kw=1.2, load_kw=0.4)
    store.upsert_actuals(NOW, pv_kw=1.3, grid_kw=-0.5, plan_power_w=200.0)
    rows = store.actuals_range(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert len(rows) == 1
    assert rows[0]["pv_kw"] == 1.3 and rows[0]["grid_kw"] == -0.5 and rows[0]["plan_power_w"] == 200.0
    with pytest.raises(ValueError):
        store.upsert_actuals(NOW, temperature=12.0)
    store.upsert_actuals(NOW + timedelta(hours=1))
    assert len(store.actuals_range(NOW, NOW + timedelta(hours=2))) == 2


def test_dispatch_log(store):
    store.add_dispatch_result(
        DispatchResult(
            battery_id="battery_1",
            ts=NOW,
            requested_w=800.0,
            applied_w=500.0,
            ok=False,
            dry_run=True,
            code="power_clamp",
            message="clamped",
        ),
        mode="advisory",
        soc_pct=42.0,
    )
    rows = store.dispatch_log(limit=10)
    assert rows[0]["battery_id"] == "battery_1"
    assert rows[0]["requested_w"] == 800.0 and rows[0]["applied_w"] == 500.0
    assert rows[0]["dry_run"] == 1 and rows[0]["result"] == "error"
    assert store.dispatch_log(battery_id="other") == []


def test_cache_profiles_and_events(store):
    assert store.cache_get("solar", "k") is None
    store.cache_put("solar", "k", {"result": {"watts": {}}}, NOW)
    cached = store.cache_get("solar", "k")
    assert cached[0] == NOW and cached[1]["result"]["watts"] == {}
    assert store.cache_age_seconds("solar", "k", NOW + timedelta(minutes=10)) == 600

    store.profile_put("load", "profile", {"samples": 3}, NOW)
    assert store.profile_get("load", "profile")[1] == {"samples": 3}
    store.profile_delete("load", "profile")
    assert store.profile_get("load", "profile") is None

    store.add_event(ts=NOW, level="warning", code="forecast_unavailable", message="static only", payload={"src": "energy_charts"})
    events = store.events(limit=5, level="warning")
    assert events[0]["code"] == "forecast_unavailable"
    assert events[0]["payload"] == {"src": "energy_charts"}
    assert store.events(code="other") == []


def test_savings_upsert_and_range(store):
    store.upsert_savings_daily("2026-01-04", import_kwh=10.0, cost_eur=2.5, revenue_eur=0.5, baseline_cost_eur=3.5)
    row = store.savings_daily_range("2026-01-01", "2026-01-05")[0]
    assert row["savings_eur"] == pytest.approx(1.5)  # 3.5 - (2.5 - 0.5)
    store.upsert_savings_daily("2026-01-04", import_kwh=11.0, cost_eur=2.6, revenue_eur=0.5, baseline_cost_eur=3.5)
    assert len(store.savings_daily_range("2026-01-01", "2026-01-05")) == 1
    with pytest.raises(ValueError):
        store.upsert_savings_daily("2026-01-05", nonsense=1.0)


def test_savings_measured_flow_columns_round_trip(store):
    """0.13.0 additions are additive: an old-style call leaves them None, a new one stores them."""
    store.upsert_savings_daily("2026-01-01", import_kwh=1.0)
    legacy = store.savings_daily_range("2026-01-01", "2026-01-01")[0]
    assert legacy["charged_kwh"] is None and legacy["battery_measured"] is None
    store.upsert_savings_daily(
        "2026-01-02",
        charged_kwh=1.5,
        discharged_kwh=1.0,
        charge_cost_eur=0.3,
        discharge_value_eur=0.05,
        grid_measured=1,
        load_measured=1,
        pv_measured=0,
        battery_measured=1,
    )
    fresh = store.savings_daily_range("2026-01-02", "2026-01-02")[0]
    assert fresh["charged_kwh"] == 1.5 and fresh["discharged_kwh"] == 1.0
    assert fresh["charge_cost_eur"] == 0.3 and fresh["discharge_value_eur"] == 0.05
    assert fresh["battery_measured"] == 1 and fresh["pv_measured"] == 0


def test_schema_v2_migrates_a_v1_database_and_keeps_its_rows(tmp_path):
    """A database written by 0.12.0 opens, migrates forward and reports old rows as unknown."""
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES(1)")
    for statement in MIGRATIONS[0][1]:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO savings_daily(day, import_kwh, cost_eur, baseline_cost_eur, savings_eur, battery_cycles)"
        " VALUES('2026-01-01', 2.0, 0.4, 0.6, 0.2, 0.5)"
    )
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        row = store.savings_daily_range("2026-01-01", "2026-01-01")[0]
        assert row["savings_eur"] == 0.2 and row["battery_cycles"] == 0.5
        assert row["charged_kwh"] is None and row["battery_measured"] is None  # unknown, not 0
        store.upsert_savings_daily("2026-01-02", charged_kwh=1.5, battery_measured=1)
        assert store.savings_daily_range("2026-01-02", "2026-01-02")[0]["charged_kwh"] == 1.5
        version = store.connection.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION == 2
    finally:
        store.close()


def make_plan(moment, slots=2):
    return Plan(
        generated_at=moment,
        horizon_start=moment,
        horizon_end=moment + timedelta(hours=36),
        slot_minutes=15,
        feasible=True,
        slots=[
            PlanSlot(
                t=moment + timedelta(minutes=15 * index),
                buy=0.2,
                sell=0.0,
                pv_kw=0.0,
                load_kw=0.4,
                soc_kwh=4.0,
                power_w=0.0,
                action=Action.HOLD,
                reason="reserve_hold",
            )
            for index in range(slots)
        ],
        summary=PlanSummary(cost_estimate_eur=1.0, baseline_cost_eur=1.5, saved_eur=0.5, cycles=0.0),
        batteries=[],
        mode="auto",
        dry_run=False,
        sources={"price": "static"},
    )


def test_trim_retention_keeps_only_the_newest_plans(store):
    """Every replan inserts a plan plus ~120 slot rows; retention must bound both."""
    ids = [store.save_plan(make_plan(NOW + timedelta(minutes=15 * index))) for index in range(5)]
    removed = store.trim(now=NOW, plans_rows=2)
    assert removed["plans"] == 3 and removed["plan_slots"] == 6  # 3 plans x 2 slots
    assert store.list_plans() == [ids[-1], ids[-2]]
    assert store.connection.execute("SELECT COUNT(*) FROM plan_slots").fetchone()[0] == 4
    assert store.load_plan(ids[0]) is None and store.load_plan(ids[-1]) is not None
    # a second trim with the same budget deletes nothing
    assert store.trim(now=NOW, plans_rows=2)["plans"] == 0


def test_trim_retention(store):
    store.add_measurements([Measurement(ts=NOW - timedelta(days=500), role="pv_power", value=1.0, unit="W")])
    store.add_measurements([Measurement(ts=NOW, role="pv_power", value=1.0, unit="W")])
    store.upsert_actuals(NOW - timedelta(days=500), pv_kw=1.0)
    store.upsert_actuals(NOW, pv_kw=1.0)
    for _ in range(5):
        store.add_dispatch()
        store.add_event(code="x")
    removed = store.trim(now=NOW, dispatch_rows=2, events_rows=2)
    assert removed["measurements"] == 1 and removed["actuals"] == 1
    assert removed["dispatch_log"] == 3 and removed["events"] == 3
    assert len(store.dispatch_log(limit=100)) == 2
    assert len(store.events(limit=100)) == 2
