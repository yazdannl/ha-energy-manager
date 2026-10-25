"""Forecast History MVP: immutable solar vintages and directly measured PV coverage."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.api import ApiServer  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.models import Measurement, ProviderStatus, SolarForecast, SolarSlot  # noqa: E402
from app.store import SCHEMA_VERSION, Store  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "history.db")
    yield instance
    instance.close()


def add_vintage(store: Store, target: datetime, energy_kwh: float, *, issued=None, model=None) -> None:
    issued = issued or target - timedelta(minutes=5)
    store.archive_forecast_vintages(
        [(target, issued, "capacity", json.dumps(model or {"version": "example-1"}, sort_keys=True), energy_kwh, 20.0, False)]
    )


def add_pv_sample(store: Store, ts: datetime, watts: float, *, entity="sensor.example_pv", observed=None) -> None:
    observed = observed or ts + timedelta(minutes=1)
    store.add_measurements(
        [Measurement(ts=ts, role="pv_power", value=watts, unit="W", entity_id=entity)],
        pv_observed_at=observed,
    )


def history_engine(store: Store) -> Engine:
    return Engine({}, store=store, clock=lambda: START)


def test_schema_v8_is_idempotent_and_adds_separate_history_tables(tmp_path):
    path = tmp_path / "migration.db"
    created = Store(path)
    _ = created.connection
    created.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_version SET version = 7")
    connection.commit()
    connection.close()

    reopened = Store(path)
    try:
        assert reopened.connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION == 9
        columns = [row[1] for row in reopened.connection.execute("PRAGMA table_info(measurements)")]
        assert columns.count("entity_id") == columns.count("observed_at") == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='forecast_vintages'"
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='forecast_archive'"
        ).fetchone()[0] == 1
    finally:
        reopened.close()


def test_engine_archives_future_solar_slots_with_model_provenance(store: Store):
    engine = history_engine(store)
    provider = ProviderStatus(
        kind="solar", name="learned", detail={"model": {"trained_at": "2026-01-01", "trees": 12}}
    )
    engine._provider_status[("solar", "learned")] = provider
    forecast = SolarForecast(
        source="learned",
        planes=[{"capacity_wp": 4200}],
        correction_applied=1.1,
        slots=[
            SolarSlot(t=START - timedelta(minutes=15), pv_kw=4.0),
            SolarSlot(t=START + timedelta(minutes=15), pv_kw=2.0, uncertainty_pct=18.0),
        ],
    )

    engine._archive_solar_history(forecast, START)

    rows = store.forecast_vintages_range(START, START + timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0]["target_ts"] == (START + timedelta(minutes=15)).isoformat()
    assert rows[0]["source"] == "learned"
    assert rows[0]["forecast_kwh"] == pytest.approx(0.5)
    assert rows[0]["uncertainty_pct"] == 18.0
    assert json.loads(rows[0]["model_json"]) == {
        "trained_at": "2026-01-01",
        "trees": 12,
        "planes": [{"capacity_wp": 4200}],
        "correction_applied": 1.1,
    }
    assert provider.detail["model"] == {"trained_at": "2026-01-01", "trees": 12}


def test_vintages_are_immutable_and_latest_as_of_target_is_selected(store: Store):
    target = START + timedelta(hours=1)
    earlier = START
    later = target - timedelta(minutes=15)
    too_late = target + timedelta(minutes=1)
    store.archive_forecast_vintages(
        [
            (target, earlier, "capacity", '{"version":"one"}', 0.25, 10.0, False),
            (target, later, "capacity", '{"version":"one"}', 0.50, 20.0, True),
            (target, too_late, "capacity", '{"version":"one"}', 9.0, 99.0, False),
        ]
    )
    # Re-offering the same identity cannot revise its earlier energy value.
    store.archive_forecast_vintages(
        [(target, later, "capacity", '{"version":"one"}', 8.0, 30.0, False)]
    )

    stored = store.connection.execute("SELECT COUNT(*) FROM forecast_vintages").fetchone()[0]
    assert stored == 2
    selected = store.forecast_vintages_range(target, target + timedelta(minutes=15))
    assert len(selected) == 1
    assert selected[0]["forecast_kwh"] == pytest.approx(0.50)
    assert selected[0]["issued_at"] == later.isoformat()
    assert selected[0]["interval_minutes"] == 15
    assert selected[0]["stale"] == 1


def test_direct_pv_samples_integrate_to_interval_energy_with_provenance(store: Store):
    target = START
    add_vintage(store, target, 0.6, model={"name": "example-model", "version": "1"})
    for offset, watts in ((0, 1000.0), (5, 2000.0), (10, 3000.0)):
        add_pv_sample(store, target + timedelta(minutes=offset), watts)

    payload = history_engine(store).forecast_history(
        target, target + timedelta(minutes=15), timezone_name="UTC", resolution="15m"
    )
    row = payload["series"][0]
    assert row["target_start"] == target.isoformat()
    assert row["target_end"] == (target + timedelta(minutes=15)).isoformat()
    assert row["forecast_kwh"] == pytest.approx(0.6)
    assert row["forecast_source"] == "capacity"
    assert row["forecast_model"] == {"name": "example-model", "version": "1"}
    assert row["issued_at"] == (target - timedelta(minutes=5)).isoformat()
    assert row["actual_source"] == "sensor.example_pv"
    assert row["actual_samples"] == row["expected_actual_samples"] == 3
    assert row["actual_kwh"] == pytest.approx(0.5)
    assert row["error_kwh"] == pytest.approx(0.1)
    assert row["comparable"] is True
    assert payload["coverage"]["comparable_intervals"] == 1


def test_missing_vintage_or_measured_samples_stays_unknown_not_zero(store: Store):
    target = START
    add_vintage(store, target, 0.5)
    # Legacy actuals and inferred PV are not direct measured five-minute PV samples.
    store.upsert_actuals(target, pv_kw=4.0, pv_derived_kw=3.0)

    payload = history_engine(store).forecast_history(
        target, target + timedelta(minutes=15), timezone_name="UTC", resolution="15m"
    )
    row = payload["series"][0]
    assert row["actual_kwh"] is None
    assert row["actual_samples"] == 0
    assert row["error_kwh"] is None
    assert row["comparable"] is False
    assert payload["actual_provenance"]["entities"] == []

    # A later target with no archived vintage is not filled using today's/current forecast.
    next_target = target + timedelta(minutes=15)
    missing = history_engine(store).forecast_history(
        next_target, next_target + timedelta(minutes=15), timezone_name="UTC", resolution="15m"
    )["series"][0]
    assert missing["forecast_kwh"] is None
    assert missing["forecast_source"] is None


def test_hourly_rollup_sums_energy_and_withholds_error_on_partial_coverage(store: Store):
    target = START
    for quarter in range(4):
        interval = target + timedelta(minutes=15 * quarter)
        add_vintage(store, interval, 0.25)
        for sample in range(3):
            if quarter == 3 and sample == 2:
                continue
            sample_ts = interval + timedelta(minutes=5 * sample)
            add_pv_sample(store, sample_ts, 1000.0)

    payload = history_engine(store).forecast_history(
        target, target + timedelta(hours=1), timezone_name="UTC", resolution="hour"
    )
    row = payload["series"][0]
    assert row["forecast_kwh"] == pytest.approx(1.0)
    assert row["forecast_intervals"] == row["expected_forecast_intervals"] == 4
    assert row["actual_kwh"] == pytest.approx(0.75)  # covered intervals only
    assert row["actual_intervals"] == 3
    assert row["actual_samples"] == 11
    assert row["actual_sample_coverage_pct"] == pytest.approx(11 / 12 * 100)
    assert row["actual_complete"] is False
    assert row["error_kwh"] is None
    assert row["comparable"] is False


def test_local_day_and_hour_groups_follow_dst_boundaries(store: Store):
    zone = ZoneInfo("Europe/Amsterdam")
    spring_day = date(2026, 3, 29)
    spring_start = datetime.combine(spring_day, time.min, tzinfo=zone)
    spring_end = datetime.combine(spring_day + timedelta(days=1), time.min, tzinfo=zone)
    spring = history_engine(store).forecast_history(
        spring_start, spring_end, timezone_name="Europe/Amsterdam", resolution="day"
    )
    assert len(spring["series"]) == 1
    assert spring["series"][0]["intervals"] == spring["coverage"]["expected_intervals"] == 92
    assert datetime.fromisoformat(spring["series"][0]["end"]) - datetime.fromisoformat(spring["series"][0]["start"]) == timedelta(hours=23)
    spring_hours = history_engine(store).forecast_history(
        spring_start, spring_end, timezone_name="Europe/Amsterdam", resolution="hour"
    )["series"]
    assert len(spring_hours) == 23
    assert not any(" 02:00 " in row["label"] for row in spring_hours)

    fall_day = date(2026, 10, 25)
    fall_start = datetime.combine(fall_day, time.min, tzinfo=zone)
    fall_end = datetime.combine(fall_day + timedelta(days=1), time.min, tzinfo=zone)
    fall_hours = history_engine(store).forecast_history(
        fall_start, fall_end, timezone_name="Europe/Amsterdam", resolution="hour"
    )["series"]
    repeated = [row["label"] for row in fall_hours if " 02:00 " in row["label"]]
    assert len(fall_hours) == 25
    assert len(repeated) == 2 and repeated[0] != repeated[1]


def test_vintage_retention_matches_the_measured_history_window(store: Store):
    old_target = START - timedelta(days=401)
    add_vintage(store, old_target, 1.0, issued=old_target - timedelta(minutes=15))
    add_vintage(store, START, 1.0)
    removed = store.trim(now=START, forecast_archive_days=400)
    assert removed["forecast_vintages"] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM forecast_vintages").fetchone()[0] == 1


class HistoryApiEngine:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return SimpleNamespace(now=datetime(2026, 3, 29, 12, 0, tzinfo=UTC))

    def forecast_history(self, start, end, *, timezone_name, resolution):
        self.calls.append((start, end, timezone_name, resolution))
        return {"series": [], "coverage": {}, "actual_provenance": {}}


def test_api_bounds_range_and_uses_dst_aware_iana_local_day():
    engine = HistoryApiEngine()
    api = ApiServer(engine)
    status, _, body = api.dispatch(
        "GET", "/api/v1/forecast-history?timezone=Europe%2FAmsterdam"
    )
    payload = json.loads(body)
    assert status == 200
    assert payload["date"] == "2026-03-29" and payload["resolution"] == "15m"
    assert payload["days"] == 1
    assert engine.calls[-1][2:] == ("Europe/Amsterdam", "15m")

    status, _, body = api.dispatch(
        "GET",
        "/api/v1/forecast-history?date=2026-03-29&timezone=Europe%2FAmsterdam&resolution=hour",
    )
    payload = json.loads(body)
    assert status == 200
    assert payload["date"] == payload["date_start"] == payload["date_end"] == "2026-03-29"
    start, end, zone_name, resolution = engine.calls[-1]
    assert zone_name == "Europe/Amsterdam" and resolution == "hour"
    assert (end.astimezone(UTC) - start.astimezone(UTC)) == timedelta(hours=23)

    status, _, body = api.dispatch(
        "GET",
        "/api/v1/forecast-history?date=2026-03-29&timezone=Europe%2FAmsterdam&resolution=day&days=99",
    )
    payload = json.loads(body)
    assert status == 200 and payload["days"] == 31
    assert payload["date_start"] == "2026-02-27"
    assert engine.calls[-1][3] == "day"


def test_api_rejects_invalid_history_timezone_date_and_resolution():
    api = ApiServer(HistoryApiEngine())
    for query, code in (
        ("timezone=Not%2FAZone", "invalid_timezone"),
        ("date=20260201", "invalid_date"),
        ("date=2026-02-30", "invalid_date"),
        ("date=0001-01-01&timezone=Europe%2FAmsterdam", "invalid_date"),
        ("resolution=week", "invalid_resolution"),
    ):
        status, _, body = api.dispatch("GET", "/api/v1/forecast-history?" + query)
        assert status == 400
        assert json.loads(body)["error"]["code"] == code
