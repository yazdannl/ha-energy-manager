"""Chart feeds (SPEC 12, 0.28.0): the compact `sensor.sem_chart_*` payloads.

The add-on's UI is ingress-only, so the Lovelace card can only read what Home Assistant holds. These
tests pin the two things that make that safe: the documented row schema, and a payload small enough
that Home Assistant neither drops the attributes nor bloats its recorder. Unknown stays null - a
missing forecast must never be drawn as a measured zero.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_manager"))

from app import feeds  # noqa: E402
from app.models import (  # noqa: E402
    AccuracyReport,
    LoadForecast,
    LoadSlot,
    Plan,
    PlanSlot,
    PlanSummary,
    PriceForecast,
    PriceSlot,
    SavingsBucket,
    SolarForecast,
    SolarSlot,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _plan(slots: int = 120, minutes: int = 15) -> Plan:
    moment = NOW
    plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=slots * minutes / 60),
        slot_minutes=minutes,
    )
    for index in range(slots):
        plan.slots.append(
            PlanSlot(
                t=moment,
                buy=0.1234,
                sell=0.0987,
                pv_kw=1.234,
                load_kw=0.567,
                soc_kwh=5.0,
                soc_pct=55.5,
                power_w=(-1234.0 if index % 2 else 1234.0),
                action="charge" if index % 2 == 0 else "discharge",
                reason="arbitrage",
                price_stale=index % 3 == 0,
                minutes=float(minutes),
            )
        )
        moment += timedelta(minutes=minutes)
    plan.summary = PlanSummary(round_trip_efficiency=0.9, wear_eur_per_kwh=0.02)
    return plan


def test_plan_feed_schema_and_size():
    state, attributes = feeds.plan_feed(_plan(), NOW, round_trip=0.9, wear_eur_per_kwh=0.02)
    assert state == "ready"
    assert attributes["schema"] == feeds.SCHEMA
    assert attributes["slot_minutes"] == 15
    assert attributes["round_trip"] == 0.9
    assert len(attributes["slots"]) == 120
    row = attributes["slots"][0]
    assert len(row) == 8, "the documented row is [t, buy, sell, soc, pv, load, power_w, stale]"
    assert row[0] == int(NOW.timestamp())
    assert row[7] == 1 and attributes["slots"][1][7] == 0
    # Home Assistant drops attributes past 16 KiB; keep every feed far below it.
    assert len(json.dumps(attributes)) < 12_000


def test_plan_feed_is_empty_without_a_plan():
    state, attributes = feeds.plan_feed(None, NOW)
    assert state == "empty" and attributes["slots"] == []


def test_an_unpriced_plan_publishes_null_prices_not_zeros():
    """0.34.2 (SPEC 8.4): no tariff means no price cell, never a 0 EUR/kWh bar."""
    plan = _plan(slots=4)
    for slot in plan.slots:
        slot.buy = 0.0
        slot.sell = 0.0
        slot.price_source = "none"
        slot.price_stale = True
    state, attributes = feeds.plan_feed(plan, NOW, round_trip=0.9, wear_eur_per_kwh=0.02)
    assert state == "ready"
    assert attributes["priced"] is False
    assert attributes["price_source"] == "none"
    assert [row[1] for row in attributes["slots"]] == [None] * 4
    assert [row[2] for row in attributes["slots"]] == [None] * 4
    # The rest of the row is unaffected: SoC and the planned power still draw.
    assert attributes["slots"][0][3] == 55.5 and attributes["slots"][0][6] == 1234.0


def test_a_priced_plan_reports_that_it_is_priced():
    _state, attributes = feeds.plan_feed(_plan(slots=4), NOW, round_trip=0.9, wear_eur_per_kwh=0.02)
    assert attributes["priced"] is True
    assert attributes["price_source"] == ""
    assert attributes["slots"][0][1] == 0.1234


def test_forecast_feeds_carry_their_own_columns():
    price = PriceForecast(source="zonneplan", generated_at=NOW)
    price.slots = [PriceSlot(t=NOW + timedelta(minutes=15 * i), buy=0.2, sell=0.1, stale=i == 1) for i in range(10)]
    state, attributes = feeds.forecast_feed("price", price, NOW)
    assert state == "zonneplan"
    assert attributes["slots"][0] == [int(NOW.timestamp()), 0.2, 0.1, 0]
    assert attributes["slots"][1][3] == 1

    solar = SolarForecast(source="forecast_solar", generated_at=NOW)
    solar.slots = [SolarSlot(t=NOW + timedelta(minutes=15 * i), pv_kw=1.0, uncertainty_pct=20.0) for i in range(10)]
    state, attributes = feeds.forecast_feed("solar", solar, NOW)
    assert state == "forecast_solar" and len(attributes["slots"][0]) == 3  # [t, pv_kw, uncertainty_pct]

    load = LoadForecast(source="learned", generated_at=NOW)
    load.slots = [LoadSlot(t=NOW + timedelta(minutes=15 * i), load_kw=0.5, p10_kw=0.3, p90_kw=0.9) for i in range(10)]
    state, attributes = feeds.forecast_feed("load", load, NOW)
    assert state == "learned" and len(attributes["slots"][0]) == 4  # [t, load, p10, p90]


def test_forecast_feed_trims_a_far_horizon():
    price = PriceForecast(source="zonneplan", generated_at=NOW)
    price.slots = [PriceSlot(t=NOW + timedelta(hours=i), buy=0.2, sell=0.1) for i in range(100)]
    _state, attributes = feeds.forecast_feed("price", price, NOW, hours=36)
    assert len(attributes["slots"]) <= 40
    assert all(row[0] <= int((NOW + timedelta(hours=36)).timestamp()) for row in attributes["slots"])


def test_forecast_feed_unknown_is_not_zero():
    state, attributes = feeds.forecast_feed("load", None, NOW)
    assert state == "empty" and attributes["slots"] == []


def test_savings_feed_keeps_rows_null_when_a_bucket_is_unknown():
    bucket = SavingsBucket(
        key="2026-10-18",
        start=NOW,
        end=NOW + timedelta(days=1),
        savings_eur=None,
        cost_eur=None,
        revenue_eur=None,
        baseline_cost_eur=None,
    )
    state, attributes = feeds.savings_feed({"day": [bucket]})
    assert state == "ready"
    assert attributes["ranges"]["day"][0] == [int(NOW.timestamp()), None, None, None, None]


def test_accuracy_feed_aligns_the_three_kinds_on_one_axis():
    pv = AccuracyReport(kind="pv", days=2)
    pv.series = [{"day": "2026-10-17", "mape_pct": 12.5}, {"day": "2026-10-18", "mape_pct": 11.0}]
    load = AccuracyReport(kind="load", days=1)
    load.series = [{"day": "2026-10-18", "mape_pct": 8.0}]
    state, attributes = feeds.accuracy_feed({"pv": pv, "load": load, "price": AccuracyReport(kind="price", days=0)})
    assert state == "2"
    assert len(attributes["days"]) == 2
    assert attributes["pv"] == [12.5, 11.0]
    # The load series is missing the first day, so the axis keeps a hole rather than shifting left.
    assert attributes["load"] == [None, 8.0]
    assert attributes["price"] == [None, None]


def test_every_feed_fits_the_attribute_ceiling():
    """A full plan plus every range of savings must stay under HA's 16 KiB per-state limit."""
    for state, attributes in (
        feeds.plan_feed(_plan(), NOW, round_trip=0.9, wear_eur_per_kwh=0.02),
        feeds.savings_feed(
            {
                "day": [SavingsBucket(key=f"d{i}", start=NOW, end=NOW) for i in range(24)],
                "month": [SavingsBucket(key=f"m{i}", start=NOW, end=NOW) for i in range(31)],
                "all": [SavingsBucket(key=f"y{i}", start=NOW, end=NOW) for i in range(60)],
            }
        ),
    ):
        assert state  # every feed has a state even when empty
        assert len(json.dumps(attributes)) < 16_384
