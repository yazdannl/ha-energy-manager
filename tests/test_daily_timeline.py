"""The selected-local-day timeline reuses durable actuals, measurements and saved plans."""

from __future__ import annotations

import json
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
from app.models import Action, Measurement, Plan, PlanSlot, PlanSummary, to_iso  # noqa: E402
from app.store import Store  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "timeline.db")
    yield instance
    instance.close()


def test_timeline_keeps_direct_derived_forecast_and_plan_data_distinct(store: Store):
    store.archive_forecast_vintages(
        [
            (START, START - timedelta(minutes=5), "capacity", '{"version":"1"}', 0.5, 10.0, False),
            (START + timedelta(minutes=15), START, "capacity", '{"version":"1"}', 0.25, None, False),
        ]
    )
    for offset in (0, 5, 10, 15):
        sample = START + timedelta(minutes=offset)
        store.add_measurements(
            [Measurement(ts=sample, role="pv_power", value=1000.0, unit="W", entity_id="sensor.example_pv")],
            pv_observed_at=sample + timedelta(minutes=1),
        )

    store.archive_forecast(
        [
            ("load", "older", to_iso(START), to_iso(START - timedelta(minutes=10)), 0.9, None),
            ("load", "newer", to_iso(START), to_iso(START - timedelta(minutes=5)), 1.25, None),
            ("load", "newer", to_iso(START + timedelta(minutes=15)), to_iso(START), 1.5, None),
        ]
    )
    # A price-like value in live actuals is deliberately not treated as a historical price series.
    for offset, load, grid, battery, pv_derived, load_derived in (
        (0, 0.6, 0.5, 0.1, None, None),
        (5, 1.0, -0.1, -0.2, None, None),
        (10, 1.4, 0.2, 0.3, None, None),
        (15, 0.5, 0.2, None, 1.5, 0.8),
        (20, 0.7, None, None, 1.6, 0.9),
        (25, None, 0.4, None, 1.7, 1.0),
    ):
        store.upsert_actuals(
            START + timedelta(minutes=offset), load_kw=load, grid_kw=grid, battery_kw=battery,
            pv_derived_kw=pv_derived, load_derived_kw=load_derived,
            price_buy=9.99 if offset == 0 else None,
        )

    store.save_plan(
        Plan(
            generated_at=START - timedelta(minutes=10),
            horizon_start=START,
            horizon_end=START + timedelta(minutes=15),
            slot_minutes=15,
            feasible=True,
            slots=[PlanSlot(
                t=START, buy=0.21, sell=0.08, pv_kw=0.5, load_kw=0.9,
                soc_kwh=5.0, power_w=600.0, action=Action.CHARGE,
                reason="arbitrage_charge", minutes=15.0,
            )],
            summary=PlanSummary(),
            sources={"price": "saved_price", "solar": "saved_solar", "load": "saved_load"},
            mode="auto",
            dry_run=False,
        )
    )

    engine = Engine({}, store=store, clock=lambda: START + timedelta(hours=1))
    payload = engine.daily_energy_timeline(START, START + timedelta(minutes=45), timezone_name="UTC")
    assert payload["interval_minutes"] == 15
    assert payload["expected_intervals"] == 3

    direct, partial, missing = payload["series"]
    assert direct["solar"]["actual_kw"] == pytest.approx(1.0)
    assert direct["solar"]["actual_source"] == "sensor.example_pv"
    assert direct["solar"]["forecast_kw"] == pytest.approx(2.0)
    assert direct["load"]["actual_kw"] == pytest.approx(1.0)
    assert direct["load"]["forecast_kw"] == pytest.approx(1.25)
    assert direct["load"]["forecast_source"] == "newer"
    assert direct["grid"]["actual_kw"] == pytest.approx(0.2)
    assert direct["battery"]["actual_kw"] == pytest.approx(0.2 / 3.0, abs=1e-6)
    assert direct["plan"]["planned_power_kw"] == pytest.approx(0.6)
    assert direct["plan"]["planned_buy_eur_per_kwh"] == pytest.approx(0.21)
    assert direct["plan"]["planned_sell_eur_per_kwh"] == pytest.approx(0.08)
    assert direct["plan"]["buy_coverage_pct"] == direct["plan"]["sell_coverage_pct"] == 100.0
    assert direct["plan"]["segments"][0]["action"] == "charge"

    assert partial["solar"]["actual_kw"] is None
    assert partial["solar"]["actual_status"] == "partial"
    assert partial["solar"]["derived"]["kw"] == pytest.approx(1.6)
    assert partial["load"]["actual_kw"] is None
    assert partial["load"]["covered_kw"] == pytest.approx(0.6)
    assert partial["load"]["status"] == "partial"
    assert partial["load"]["derived"]["kw"] == pytest.approx(0.9)
    assert partial["grid"]["actual_kw"] is None
    assert partial["grid"]["covered_kw"] == pytest.approx(0.3)
    assert partial["battery"]["actual_kw"] is None
    assert partial["plan"]["plan_id"] is None
    assert partial["load"]["forecast_kw"] == pytest.approx(1.5)

    assert missing["load"]["actual_kw"] is None and missing["load"]["forecast_kw"] is None
    assert missing["solar"]["actual_kw"] is None and missing["solar"]["derived"]["kw"] is None
    assert missing["plan"]["planned_buy_eur_per_kwh"] is None
    assert "price_actual" not in direct and "actual_price" not in direct["plan"]
    assert "direct pv_power" in payload["provenance"]["solar_actual"]
    assert "shown separately" in payload["provenance"]["load_derived"]
    assert "Independent historical actual-price data is unavailable" in payload["provenance"]["price"]
    assert payload["coverage"]["solar_derived"]["complete"] == 1
    assert payload["coverage"]["load_derived"]["complete"] == 1


def test_engine_bins_normal_dst_days_by_utc_instants(tmp_path):
    store = Store(tmp_path / "dst.db")
    engine = Engine({}, store=store)
    zone = ZoneInfo("Europe/Amsterdam")
    try:
        for selected, expected_hours, expected_bins in (
            (date(2026, 3, 29), 23, 92),
            (date(2026, 10, 25), 25, 100),
        ):
            local_start = datetime.combine(selected, time.min, tzinfo=zone)
            local_end = datetime.combine(selected + timedelta(days=1), time.min, tzinfo=zone)
            start, end = local_start.astimezone(UTC), local_end.astimezone(UTC)
            payload = engine.daily_energy_timeline(start, end, timezone_name=zone.key)
            assert end - start == timedelta(hours=expected_hours)
            assert payload["expected_intervals"] == expected_bins
            assert payload["series"][0]["label"].startswith(selected.isoformat())
        repeated = [row["label"] for row in payload["series"] if " 02:15 " in row["label"]]
        assert len(repeated) == 2 and repeated[0] != repeated[1]
    finally:
        store.close()


class TimelineApiEngine:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return SimpleNamespace(now=datetime(2026, 3, 29, 12, 0, tzinfo=UTC))

    def daily_energy_timeline(self, start, end, *, timezone_name):
        self.calls.append((start, end, timezone_name))
        count = int((end - start).total_seconds() // 900)
        return {"timezone": timezone_name, "interval_minutes": 15, "expected_intervals": count, "series": []}


def test_daily_timeline_api_validates_and_uses_selected_local_day():
    engine = TimelineApiEngine()
    api = ApiServer(engine)
    status, _, body = api.dispatch(
        "GET", "/api/v1/daily-timeline?date=2026-03-29&timezone=Europe%2FAmsterdam"
    )
    payload = json.loads(body)
    assert status == 200 and payload["date"] == "2026-03-29"
    assert payload["expected_intervals"] == 92
    assert payload["timezone"] == "Europe/Amsterdam"
    assert engine.calls[-1][1] - engine.calls[-1][0] == timedelta(hours=23)

    status, _, body = api.dispatch(
        "GET", "/api/v1/daily-timeline?date=2026-10-25&timezone=Europe%2FAmsterdam"
    )
    payload = json.loads(body)
    assert status == 200 and payload["expected_intervals"] == 100
    assert engine.calls[-1][1] - engine.calls[-1][0] == timedelta(hours=25)

    for query, code in (
        ("timezone=Not%2FAZone", "invalid_timezone"),
        ("date=2026-02-30", "invalid_date"),
        ("date=20260201", "invalid_date"),
    ):
        status, _, body = api.dispatch("GET", "/api/v1/daily-timeline?" + query)
        assert status == 400 and json.loads(body)["error"]["code"] == code
