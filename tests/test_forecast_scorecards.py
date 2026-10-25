"""PV accuracy scorecards built from archived forecast vintages and direct measured outcomes."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.api import ApiServer
from app.engine import Engine
from app.models import Measurement
from app.store import Store

UTC = timezone.utc
NOW = datetime(2026, 1, 5, 1, 0, tzinfo=UTC)


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "scorecards.db")
    yield instance
    instance.close()


def add_forecast(
    store: Store,
    target: datetime,
    forecast_kwh: float,
    source: str,
    *,
    lead: timedelta = timedelta(minutes=5),
    stale: bool = False,
) -> None:
    store.archive_forecast_vintages(
        [
            (
                target,
                target - lead,
                source,
                json.dumps({"version": "test"}, sort_keys=True),
                forecast_kwh,
                None,
                stale,
            )
        ]
    )


def add_pv_samples(
    store: Store,
    target: datetime,
    watts: float,
    *,
    offsets: tuple[int, ...] = (0, 5, 10),
    entity: str = "sensor.example_pv",
) -> None:
    for offset in offsets:
        ts = target + timedelta(minutes=offset)
        store.add_measurements(
            [Measurement(ts=ts, role="pv_power", value=watts, unit="W", entity_id=entity)],
            pv_observed_at=ts + timedelta(minutes=1),
        )


def score_engine(store: Store) -> Engine:
    return Engine({}, store=store, clock=lambda: NOW)


def test_scorecard_matches_only_complete_vintages_and_breaks_down_supported_dimensions(store):
    # Cross local midnight in Europe/Amsterdam, where the target period labels should move at 23Z.
    targets = [NOW - timedelta(minutes=135 - 15 * index) for index in range(4)]
    add_forecast(store, targets[0], 0.50, "capacity", lead=timedelta(minutes=15), stale=True)
    add_forecast(store, targets[1], 0.10, "learned", lead=timedelta(hours=2))
    add_forecast(store, targets[2], 0.00, "capacity", lead=timedelta(minutes=30))
    add_forecast(store, targets[3], 0.40, "learned", lead=timedelta(hours=30))

    add_pv_samples(store, targets[0], 1000.0)
    add_pv_samples(store, targets[1], 1000.0)
    add_pv_samples(store, targets[2], 0.0)  # a measured zero is a valid outcome, not missing data
    add_pv_samples(store, targets[3], 1000.0, offsets=(0, 5))  # partial outcome is not a match

    report = score_engine(store).forecast_vintage_scorecard(
        days=1, timezone_name="Europe/Amsterdam"
    )
    assert report["interval_minutes"] == 15
    assert report["timezone"] == "Europe/Amsterdam"
    assert "three distinct fresh 5-minute direct pv_power samples" in report["sample_definition"]
    assert report["matched_samples"] == 3
    assert report["bias_kwh"] == pytest.approx((0.25 - 0.15 + 0.0) / 3)
    assert report["mae_kwh"] == pytest.approx(0.40 / 3)
    assert report["total_absolute_error_kwh"] == pytest.approx(0.40)
    assert report["coverage"] == {
        "expected_intervals": 96,
        "forecast_intervals": 4,
        "actual_complete_intervals": 3,
        "matched_intervals": 3,
        "direct_samples": 11,
        "expected_direct_samples": 288,
        "stale_forecast_intervals": 1,
    }

    periods = {row["local_day"]: row for row in report["periods"]}
    assert list(periods) == ["2026-01-04", "2026-01-05"]
    assert periods["2026-01-04"]["coverage"]["expected_intervals"] == 88
    assert periods["2026-01-04"]["matched_samples"] == 1
    assert periods["2026-01-05"]["coverage"]["expected_intervals"] == 8
    assert periods["2026-01-05"]["matched_samples"] == 2

    sources = {row["source"]: row for row in report["by_source"]}
    assert sources["capacity"]["coverage"]["forecast_intervals"] == 2
    assert sources["capacity"]["matched_samples"] == 2
    assert sources["learned"]["coverage"]["forecast_intervals"] == 2
    assert sources["learned"]["matched_samples"] == 1

    lead_times = {row["lead_time_band"]: row for row in report["by_lead_time"]}
    assert lead_times["0–1 h"]["matched_samples"] == 2
    assert lead_times["1–6 h"]["matched_samples"] == 1
    assert lead_times["24 h+"]["matched_samples"] == 0
    assert lead_times["24 h+"]["bias_kwh"] is None
    assert lead_times["24 h+"]["coverage"]["direct_samples"] == 2


def test_scorecard_keeps_unmatched_error_unknown_not_zero(store):
    target = NOW - timedelta(minutes=15)
    add_forecast(store, target, 0.0, "capacity")

    report = score_engine(store).forecast_vintage_scorecard(days=1)
    assert report["matched_samples"] == 0
    assert report["bias_kwh"] is None
    assert report["mae_kwh"] is None
    assert report["total_absolute_error_kwh"] is None
    assert report["coverage"]["forecast_intervals"] == 1
    assert report["coverage"]["actual_complete_intervals"] == 0
    assert report["coverage"]["matched_intervals"] == 0
    assert report["coverage"]["expected_intervals"] == 96


def test_accuracy_api_adds_scorecard_and_validates_timezone(store):
    api = ApiServer(score_engine(store))
    status, _, body = api.dispatch(
        "GET", "/api/v1/accuracy?days=1&timezone=Europe%2FAmsterdam"
    )
    payload = json.loads(body)
    assert status == 200
    assert set(payload) == {"pv", "load", "price", "pv_vintage"}
    assert payload["pv_vintage"]["days"] == 1
    assert payload["pv_vintage"]["timezone"] == "Europe/Amsterdam"

    status, _, body = api.dispatch("GET", "/api/v1/accuracy?timezone=Not%2FAZone")
    assert status == 400
    assert json.loads(body)["error"]["code"] == "invalid_timezone"
