"""WP2B analytics tests: savings buckets, accuracy accounting, PV bias learning, price windows."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app import analytics  # noqa: E402
from app.models import (  # noqa: E402
    AccuracyReport,
    PriceSlot,
    SavingsBucket,
    SolarForecast,
    SolarSlot,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def daily_row(day: str, savings: float = 0.12, **values) -> dict:
    """A consistent savings_daily row: savings = baseline - (cost - revenue)."""
    row = {
        "day": day,
        "import_kwh": 1.0,
        "export_kwh": 0.2,
        "cost_eur": 0.0,
        "revenue_eur": 0.0,
        "baseline_cost_eur": savings,
        "savings_eur": savings,
        "pv_kwh": 2.0,
        "battery_cycles": 0.5,
    }
    row.update(values)
    return row


def test_baseline_is_the_do_nothing_net_cost():
    """Audit finding 13: the baseline must credit exports and be null without a load forecast."""
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # exporting 2.5 kWh with no battery: the baseline is a net *credit*, not a cost
    row = analytics.daily_row_delta(
        None,
        day="2026-01-01",
        dt_hours=1.0,
        grid_kw=-2.0,
        load_kw=0.5,
        pv_kw=3.0,
        battery_kw=0.0,
        price_buy=0.30,
        price_sell=0.10,
        capacity_kwh=5.0,
        now=now,
    )
    assert abs(row["baseline_cost_eur"] - (-0.25)) < 1e-9
    assert abs(row["savings_eur"] - (row["baseline_cost_eur"] - (row["cost_eur"] - row["revenue_eur"]))) < 1e-9
    # no load forecast at all: the baseline and the saving are unknown, never a fabricated 0
    unknown = analytics.daily_row_delta(
        None,
        day="2026-01-01",
        dt_hours=1.0,
        grid_kw=2.0,
        load_kw=None,
        pv_kw=None,
        battery_kw=0.0,
        price_buy=0.30,
        price_sell=0.10,
        capacity_kwh=5.0,
        now=now,
    )
    assert unknown["baseline_cost_eur"] is None
    assert unknown["savings_eur"] is None
    buckets, totals = analytics.savings_from_daily([unknown], "day", now)
    assert totals["savings_eur"] is None
    assert buckets[0].savings_eur is None


def test_savings_daily_buckets_by_range():
    rows = [
        daily_row("2025-12-31", savings=1.0),
        daily_row("2026-01-01", savings=2.0),
        daily_row("2025-12-25", savings=3.0),
        daily_row("2025-01-05", savings=4.0),
    ]
    day_buckets, day_totals = analytics.savings_from_daily(rows, "day", NOW)
    assert [bucket.key for bucket in day_buckets] == ["2026-01-01"]
    assert abs(day_totals["savings_eur"] - 2.0) < 1e-9
    assert day_totals["buckets"] == 1.0

    week_buckets, _ = analytics.savings_from_daily(rows, "week", NOW)
    assert len(week_buckets) == 7  # one bucket per day, empty days included
    assert week_buckets[-1].key == "2026-01-01"
    # 7-day window = Dec 26 .. Jan 1: Dec 31 + Jan 1
    assert abs(sum(bucket.savings_eur for bucket in week_buckets) - 3.0) < 1e-9

    month_buckets, month_totals = analytics.savings_from_daily(rows, "month", NOW)
    assert len(month_buckets) == 30
    assert abs(month_totals["savings_eur"] - 6.0) < 1e-9  # 30-day window = Dec 3 .. Jan 1

    year_buckets, _ = analytics.savings_from_daily(rows, "year", NOW)
    assert all(bucket.key.startswith("2025-") or bucket.key.startswith("2026-") for bucket in year_buckets)
    assert len(year_buckets) >= 12

    all_buckets, all_totals = analytics.savings_from_daily(rows, "all", NOW)
    assert abs(all_totals["savings_eur"] - 10.0) < 1e-9
    assert all_buckets

    try:
        analytics.savings_from_daily(rows, "example", NOW)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_savings_savings_is_baseline_minus_actual():
    rows = [daily_row("2026-01-01", cost_eur=1.0, revenue_eur=0.1, baseline_cost_eur=1.5)]
    buckets, _ = analytics.savings_from_daily(rows, "day", NOW)
    assert abs(buckets[0].savings_eur - (1.5 - (1.0 - 0.1))) < 1e-9
    assert isinstance(buckets[0], SavingsBucket)
    assert analytics.BASELINE_NOTE.startswith("Baseline:")


def test_daily_row_delta_accumulates_and_defines_the_baseline():
    row = analytics.daily_row_delta(
        None,
        day="2026-01-01",
        dt_hours=0.5,
        grid_kw=1.0,
        load_kw=1.5,
        pv_kw=0.5,
        battery_kw=-1.0,
        price_buy=0.3,
        price_sell=0.05,
        capacity_kwh=5.0,
        now=NOW,
    )
    assert abs(row["import_kwh"] - 0.5) < 1e-9
    assert abs(row["export_kwh"]) < 1e-9
    assert abs(row["cost_eur"] - 0.15) < 1e-9
    assert abs(row["baseline_cost_eur"] - 0.3 * 1.0 * 0.5) < 1e-9
    assert abs(row["pv_kwh"] - 0.25) < 1e-9
    assert abs(row["battery_cycles"] - 1.0 * 0.5 / 5.0) < 1e-9
    assert abs(row["savings_eur"] - (row["baseline_cost_eur"] - row["cost_eur"] + row["revenue_eur"])) < 1e-9

    again = analytics.daily_row_delta(
        row,
        day="2026-01-01",
        dt_hours=0.5,
        grid_kw=-0.5,
        load_kw=1.0,
        pv_kw=1.5,
        battery_kw=0.0,
        price_buy=0.3,
        price_sell=0.05,
        capacity_kwh=5.0,
        now=NOW,
    )
    assert abs(again["import_kwh"] - 0.5) < 1e-9
    assert abs(again["export_kwh"] - 0.25) < 1e-9
    assert abs(again["revenue_eur"] - 0.0125) < 1e-9


def test_accuracy_reports_mape_bias_and_series():
    samples = [
        analytics.AccuracySample(ts=NOW - timedelta(days=1), forecast=2.0, actual=2.5),
        analytics.AccuracySample(ts=NOW - timedelta(days=1, hours=1), forecast=2.0, actual=1.5),
        analytics.AccuracySample(ts=NOW - timedelta(days=2), forecast=1.0, actual=1.0),
    ]
    report = analytics.accuracy_from_samples("pv", samples, 14, NOW)
    assert isinstance(report, AccuracyReport)
    assert report.samples == 3
    assert report.mae is not None and abs(report.mae - (0.5 + 0.5 + 0.0) / 3) < 1e-9
    assert report.mape_pct is not None
    assert report.bias_pct is not None
    assert [entry["day"] for entry in report.series] == sorted(entry["day"] for entry in report.series)
    assert len(report.series) == 2

    empty = analytics.accuracy_from_samples("load", [], 14, NOW)
    assert empty.samples == 0 and empty.mape_pct is None and empty.series == []

    out_of_range = analytics.accuracy_from_samples(
        "price", [analytics.AccuracySample(ts=NOW - timedelta(days=40), forecast=1.0, actual=2.0)], 14, NOW
    )
    assert out_of_range.samples == 0


def test_pv_bias_factors_are_ewma_clamped_and_per_hour():
    samples = [
        analytics.AccuracySample(ts=NOW.replace(hour=10), forecast=2.0, actual=3.0),  # ratio 1.5
        analytics.AccuracySample(ts=NOW.replace(hour=10), forecast=2.0, actual=3.0),
        analytics.AccuracySample(ts=NOW.replace(hour=11), forecast=2.0, actual=0.1),  # extreme
        analytics.AccuracySample(ts=NOW.replace(hour=12), forecast=0.0, actual=1.0),  # no forecast
    ]
    factors = analytics.update_bias_factors({}, samples)
    assert set(factors) == {"10", "11"}
    assert abs(factors["10"]["factor"] - 1.18) < 1e-9  # EWMA alpha=0.2 towards 1.5, twice
    assert factors["10"]["samples"] == 2
    assert abs(factors["11"]["factor"] - 0.86) < 1e-9  # pulled down, still inside the clamp
    assert "12" not in factors
    low = [
        analytics.AccuracySample(ts=NOW.replace(hour=13), forecast=10.0, actual=0.0)
        for _ in range(60)
    ]
    assert abs(analytics.update_bias_factors({}, low)["13"]["factor"] - 0.3) < 0.01  # clamped low
    high = [
        analytics.AccuracySample(ts=NOW.replace(hour=14), forecast=1.0, actual=10.0)
        for _ in range(60)
    ]
    assert abs(analytics.update_bias_factors({}, high)["14"]["factor"] - 1.8) < 0.01  # clamped high


def test_bias_factor_holds_then_decays_to_one():
    updated = NOW - timedelta(days=10)
    factors = {"10": {"factor": 1.5, "updated_at": updated.isoformat()}}
    at_10 = NOW.replace(hour=10)
    assert analytics.bias_factor_for(factors, at_10, NOW) == 1.5  # inside the hold window
    aged = {"10": {"factor": 1.5, "updated_at": (NOW - timedelta(days=21)).isoformat()}}
    assert abs(analytics.bias_factor_for(aged, at_10, NOW) - 1.25) < 1e-9  # half way back to 1
    stale = {"10": {"factor": 1.5, "updated_at": (NOW - timedelta(days=40)).isoformat()}}
    assert analytics.bias_factor_for(stale, at_10, NOW) == 1.0
    assert analytics.bias_factor_for({}, at_10, NOW) == 1.0


def test_apply_pv_bias_returns_a_new_corrected_forecast():
    forecast = SolarForecast(source="example")
    for hour in (10, 11):
        forecast.slots.append(SolarSlot(t=NOW.replace(hour=hour), pv_kw=2.0))
    factors = {"10": {"factor": 1.5, "updated_at": NOW.isoformat()}}
    corrected = analytics.apply_pv_bias(forecast, factors, NOW)
    assert corrected is not forecast
    assert forecast.slots[0].pv_kw == 2.0  # the input is untouched
    assert abs(corrected.slots[0].pv_kw - 3.0) < 1e-9
    assert abs(corrected.slots[1].pv_kw - 2.0) < 1e-9
    assert abs(corrected.correction_applied - 1.25) < 1e-9


def test_price_windows_include_the_running_slot():
    slots = []
    for index in range(16):
        moment = NOW.replace(minute=0) + timedelta(minutes=15 * index)
        price = 0.5 if 14 <= moment.hour < 15 else (0.1 if moment.hour == 12 else 0.3)
        slots.append(PriceSlot(t=moment, buy=price, sell=0.05))
    cheap, expensive = analytics.price_windows(slots, NOW)
    assert cheap is not None and cheap[0] == NOW.replace(minute=0)
    assert expensive is not None and expensive[0].hour == 14
    assert expensive[1] - expensive[0] >= timedelta(hours=1)
    assert analytics.price_windows([], NOW) == (None, None)


def test_percentile_rank_bounds():
    values = [0.1, 0.2, 0.3, 0.4]
    assert analytics.percentile_rank(values, 0.1) == 25.0
    assert analytics.percentile_rank(values, 0.4) == 100.0
    assert analytics.percentile_rank([], 1.0) == 0.0
    assert analytics.percentile_rank(values, -1.0) == 0.0


def test_measured_per_slot_uses_five_minute_buckets():
    """Measured actuals onto a plan slot grid: last write wins, energies are magnitudes (SPEC 10)."""
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    slots = [start, start + timedelta(minutes=15)]
    rows = [
        {
            "ts": (start + timedelta(minutes=5)).isoformat(),
            "grid_kw": 2.0,
            "battery_kw": 1.0,
            "pv_kw": 0.5,
            "load_kw": 1.5,
            "soc_kwh": 2.0,
            "plan_power_w": 1000.0,
        },
        # The same bucket written twice: the last row wins, exactly like the store's upsert.
        {
            "ts": (start + timedelta(minutes=5)).isoformat(),
            "grid_kw": 2.0,
            "battery_kw": 1.0,
            "pv_kw": 0.5,
            "load_kw": 1.5,
            "soc_kwh": 2.4,
            "plan_power_w": 1000.0,
        },
        {
            "ts": (start + timedelta(minutes=10)).isoformat(),
            "grid_kw": -1.0,
            "battery_kw": -0.5,
            "pv_kw": 1.0,
            "load_kw": 0.5,
            "soc_kwh": 2.0,
            "plan_power_w": -500.0,
        },
    ]
    first, second = analytics.measured_per_slot(slots, 15.0, rows)
    assert first["samples"] == 2
    assert first["coverage_pct"] == 100.0 * 2 * 5 / 15
    # Two distinct 5-minute buckets (the duplicate write is one of them), so each row contributes
    # exactly five measured minutes.
    assert abs(first["charged_kwh"] - 1.0 * 5 / 60) < 1e-9
    assert abs(first["discharged_kwh"] - 0.5 * 5 / 60) < 1e-9
    assert abs(first["import_kwh"] - 2.0 * 5 / 60) < 1e-9
    assert abs(first["export_kwh"] - 1.0 * 5 / 60) < 1e-9
    assert first["soc_kwh_start"] == 2.4 and first["soc_kwh_end"] == 2.0
    assert first["measured_kw"] == (1.0 + -0.5) / 2
    assert first["planned_kw"] == (1000.0 + -500.0) / 2000.0
    assert first["pv_kw"] == (0.5 + 1.0) / 2 and first["load_kw"] == (1.5 + 0.5) / 2
    # A slot with no measurement reports nothing rather than a fabricated zero.
    assert second is None


def test_measured_per_slot_ignores_unusable_rows_and_unreachable_slots():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"ts": "not-a-timestamp", "grid_kw": 1.0},
        {"ts": (start - timedelta(minutes=5)).isoformat(), "grid_kw": 1.0},
        {"ts": (start + timedelta(minutes=5)).isoformat(), "grid_kw": 1.0},
    ]
    # The future row is excluded while `now` has not reached it, so the slot stays unmeasured.
    assert analytics.measured_per_slot([start], 15.0, rows, now=start + timedelta(minutes=1)) == [None]
    measured = analytics.measured_per_slot([start], 15.0, rows, now=start + timedelta(minutes=6))[0]
    assert measured["samples"] == 1 and measured["import_kwh"] == 1.0 * 5 / 60
    assert analytics.measured_per_slot([], 15.0, rows) == []
    assert analytics.measured_per_slot([start], 0.0, rows) == [None]
    assert analytics.measured_per_slot([start], 15.0, []) == [None]
