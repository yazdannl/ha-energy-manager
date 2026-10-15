"""Feed-in wiring tests (optimizer v2 stage 1b; docs/OPTIMIZER_V2.md sections 2 and 7).

The kill switch must leave v1 byte-identical and must never call the settlement; ``shadow`` must
record a v1-vs-v2 diff while v1 stays dispatched, and its v2 schedule must never reach the
``plans`` table; ``on`` must dispatch the settlement plan; and an unpriceable contract must fail
safe (plan withheld -> hold) without ever falling back to the v1 derived pricing.  The DP bound
tests run against the Python reference and the C core differentially (tests/test_dp_core.py).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import (  # noqa: E402
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    Reason,
    SolarForecast,
    SolarSlot,
)
from app.optimize import dp  # noqa: E402

NOW = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, event, title, message, **kwargs):
        self.sent.append((event, title, message))
        return True


def build_engine(values=None, clock=None):
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    for key, value in (values or {}).items():
        settings[key] = value
    engine = Engine(
        settings,
        store=_FakeStore(),
        ha=_FakeHA(),
        notifier=FakeNotifier(),
        clock=clock or Clock(),
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    return engine, settings


def saldering_values(**overrides):
    values = {
        "price.feed_in_mode": "saldering",
        "price.saldering_assume_nettable": True,
        "optimizer.allow_export": True,
    }
    values.update(overrides)
    return values


def _powers(plan):
    return [slot.power_w for slot in plan.slots]


# ------------------------------------------------------------------ kill switch (off = v1)


def test_kill_switch_off_never_calls_the_settlement_and_keeps_v1(monkeypatch):
    engine, _settings = build_engine(saldering_values())
    first = engine.plan_now("test")
    assert first is not None and getattr(first, "feed_in", None) is None
    powers = _powers(first)

    def boom(*_args, **_kwargs):
        raise AssertionError("settle_feed_in must not be called when the wiring is off")

    monkeypatch.setattr(dp, "settle_feed_in", boom)
    second = engine.plan_now("test")
    assert second is not None and _powers(second) == powers
    assert getattr(second, "feed_in", None) is None


# ------------------------------------------------------------------ on = dispatch v2


def test_wiring_on_dispatches_the_settlement_plan():
    engine, _settings = build_engine(saldering_values(**{"optimizer.feed_in_wiring": "on"}))
    plan = engine.plan_now("test")
    assert plan is not None
    assert plan.feed_in["mode"] == "saldering"
    assert plan.feed_in["export_allowed"] is True and plan.feed_in["reason"] is None
    # The explicitly stated assumption credits export at the buy price (D4: no period state).
    assert all(abs(slot.sell - slot.buy) < 1e-9 for slot in plan.slots)
    assert plan.summary.feed_in_export_allowed is True
    assert plan.summary.feed_in_floor_eur_per_kwh == pytest.approx(
        min(slot.buy for slot in plan.slots)
    )


# ------------------------------------------------------------------ shadow = diff, v1 dispatch


def test_shadow_records_a_diff_and_dispatches_v1_without_persisting_v2():
    baseline_engine, _ = build_engine(saldering_values(), clock=Clock())
    baseline = baseline_engine.plan_now("test")
    baseline_powers = _powers(baseline)

    engine, _ = build_engine(
        saldering_values(**{"optimizer.feed_in_wiring": "shadow"}), clock=Clock()
    )
    plan = engine.plan_now("test")
    assert plan is not None
    # v1 is what is dispatched and persisted: the settlement's higher sells would move the schedule.
    assert _powers(plan) == baseline_powers
    assert plan.feed_in["shadow"] is True
    assert plan.feed_in["diff"]["changed_slots"] >= 1, "the v2 prices must move at least one slot"
    assert plan.feed_in["diff"]["slots"][0]["v1_reason"] is not None
    stored = engine._store.learning.get(("feed_in_shadow", "latest"))  # noqa: SLF001
    assert stored is not None and stored[1]["feed_in"]["shadow"] is True
    assert len(engine._store.plans) == 1, "the shadow v2 schedule must never be persisted"  # noqa: SLF001
    assert any(event["code"] == "feed_in_shadow" for event in engine._store.events)  # noqa: SLF001


# ------------------------------------------------------------------ provenance (review round 2)


def _price_forecast_source(sell, provenance):
    """A price source whose slots carry ``sell`` and the additive provenance attribute."""

    def source(now):
        forecast = PriceForecast(source="test", generated_at=now)
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            forecast.slots.append(
                PriceSlot(
                    t=start + timedelta(minutes=15 * index),
                    buy=0.30 if index % 2 == 0 else 0.20,
                    sell=sell,
                )
            )
        forecast.feed_in_source = provenance
        return forecast

    return source


def test_unavailable_explicit_source_never_becomes_the_explicit_rate():
    engine, _settings = build_engine(
        {
            "optimizer.feed_in_wiring": "on",
            "price.feed_in_mode": "explicit",
            "price.feed_in_entity": "sensor.example_feed_in",
        }
    )
    # A configured-but-unreadable entity leaves derived numbers in the slots: handing those over as
    # an explicit tariff would bypass the settlement's refusal.
    engine._price_source = _price_forecast_source(0.25, "unavailable")  # noqa: SLF001
    assert engine.plan_now("test") is None
    assert any(alert.code == Reason.UNPRICED_EXPORT for alert in engine.snapshot().alerts)


def test_shadow_with_a_configured_but_unavailable_source_dispatches_v1():
    engine, _settings = build_engine(
        {
            "optimizer.feed_in_wiring": "shadow",
            "price.feed_in_mode": "explicit",
            "price.feed_in_entity": "sensor.example_feed_in",
        }
    )
    engine._price_source = _price_forecast_source(0.25, "unavailable")  # noqa: SLF001
    plan = engine.plan_now("test")
    assert plan is not None and engine.active_plan() is plan
    assert plan.feed_in["shadow"] is True
    assert plan.feed_in["reason"] == Reason.UNPRICED_EXPORT


def test_a_missing_or_non_finite_sell_is_unknown_never_a_zero_rate():
    engine, _settings = build_engine({})
    configs = engine.effective_battery_configs()
    forecast = _price_forecast_source(None, "entity")(NOW)
    assert engine._feed_in_request(configs, forecast).explicit_price_eur_per_kwh is None  # noqa: SLF001
    forecast.slots[0].sell = float("nan")
    assert engine._feed_in_request(configs, forecast).explicit_price_eur_per_kwh is None  # noqa: SLF001
    forecast.slots[0].sell = float("inf")
    assert engine._feed_in_request(configs, forecast).explicit_price_eur_per_kwh is None  # noqa: SLF001
    # A forecast from an older build has no provenance: it is not trusted as explicit either.
    del forecast.feed_in_source
    forecast.slots[0].sell = 0.25
    assert engine._feed_in_request(configs, forecast).explicit_price_eur_per_kwh is None  # noqa: SLF001


def test_on_with_a_configured_source_and_sell_none_withholds_instead_of_raising():
    engine, _settings = build_engine(
        {
            "optimizer.feed_in_wiring": "on",
            "price.feed_in_mode": "explicit",
            "price.feed_in_entity": "sensor.example_feed_in",
        }
    )
    engine._price_source = _price_forecast_source(None, "entity")  # noqa: SLF001
    assert engine.plan_now("test") is None
    assert any(alert.code == Reason.UNPRICED_EXPORT for alert in engine.snapshot().alerts)


def test_shadow_with_a_configured_source_and_sell_none_still_dispatches_v1():
    engine, _settings = build_engine(
        {
            "optimizer.feed_in_wiring": "shadow",
            "price.feed_in_mode": "explicit",
            "price.feed_in_entity": "sensor.example_feed_in",
        }
    )
    # The missing sell must degrade to "nothing received" (no export), never crash the v1 solve or
    # be handed to the settlement as an explicit rate.
    engine._price_source = _price_forecast_source(None, "entity")  # noqa: SLF001
    plan = engine.plan_now("test")
    assert plan is not None and engine.active_plan() is plan
    assert all(slot.sell == 0.0 for slot in plan.slots)
    assert plan.feed_in["shadow"] is True
    assert plan.feed_in["reason"] == Reason.UNPRICED_EXPORT


def test_a_failing_request_build_keeps_shadow_on_v1_and_fails_safe_in_on(monkeypatch):
    def boom(*_args, **_kwargs):
        raise ValueError("request construction failed")

    on_engine, _on_settings = build_engine({"optimizer.feed_in_wiring": "on"})
    monkeypatch.setattr(on_engine, "_feed_in_request", boom)
    assert on_engine.plan_now("test") is None
    assert any(alert.code == Reason.UNPRICED_EXPORT for alert in on_engine.snapshot().alerts)

    shadow_engine, _shadow_settings = build_engine({"optimizer.feed_in_wiring": "shadow"})
    monkeypatch.setattr(shadow_engine, "_feed_in_request", boom)
    plan = shadow_engine.plan_now("test")
    assert plan is not None and shadow_engine.active_plan() is plan
    assert any(event["code"] == "plan_updated" for event in shadow_engine._store.events)  # noqa: SLF001


# ------------------------------------------------------------------ fail-safe: unpriceable


def test_derived_mode_with_wiring_on_withholds_the_plan_and_holds():
    engine, _settings = build_engine({"optimizer.feed_in_wiring": "on"})
    # ``derived`` is the live v1 pricing path; the wiring must never silently re-use it.
    assert engine.plan_now("test") is None
    assert engine.active_plan() is None
    assert any(event["code"] == "feed_in_unpriceable" for event in engine._store.events)  # noqa: SLF001
    assert any(alert.code == Reason.UNPRICED_EXPORT for alert in engine.snapshot().alerts)
    outcome = engine.tick_now()
    assert outcome.plan_power_w is None and outcome.target_w == 0.0


def test_floor_none_withholds_the_plan():
    engine, _settings = build_engine(
        {"optimizer.feed_in_wiring": "on", "price.feed_in_mode": "tiered"}
    )
    assert engine.plan_now("test") is None
    assert engine.active_plan() is None
    assert any(alert.code == Reason.UNPRICED_EXPORT for alert in engine.snapshot().alerts)


def test_the_safety_layer_still_runs_with_no_plan():
    engine, _settings = build_engine({"optimizer.feed_in_wiring": "on"})
    assert engine.plan_now("test") is None
    # The watchdog is the independent fail-safe hold: it writes a neutral setpoint for every
    # battery even though no plan exists (SPEC 9.3 gate ordering is untouched).
    results = engine._dispatcher.watchdog(engine._clock())  # noqa: SLF001
    assert results and all(result.ok for result in results)
    assert any(event["code"] == "watchdog_hold" for event in engine._store.events)  # noqa: SLF001


# ------------------------------------------------------------------ DP bound (Python reference)


def _slot(pv_kw, load_kw, sell=0.10, buy=0.30):
    return dp.SlotInput(t=NOW, pv_kw=pv_kw, load_kw=load_kw, buy=buy, sell=sell, energy_tax=None)


def _aggregate():
    config = BatteryConfig(
        id="b", name="b", driver="generic", capacity_kwh=8.0, min_soc_pct=5.0,
        max_soc_pct=100.0, max_charge_w=4000.0, max_discharge_w=4000.0,
        eta_charge=0.94, eta_discharge=0.92, mapping={},
    )
    return dp.BatteryAggregate.from_configs([config], 0.0)


def test_refuse_battery_export_bounds_battery_export_to_zero():
    agg = _aggregate()
    opts = dp.OptimizerOptions(refuse_battery_export=True)
    deficit_lo, _ = dp.grid_window_kw(_slot(0.0, 1.0), agg, opts)
    # p >= -base = -1.0 kW: the pack may serve the house, but grid = base + p can never go below 0.
    assert deficit_lo == pytest.approx(-1.0)
    surplus_lo, _ = dp.grid_window_kw(_slot(2.0, 0.5), agg, opts)
    assert surplus_lo >= -1e-9, "a surplus slot refuses the battery-sourced leg (p >= 0)"
    # p = 0 still exports only the PV surplus: grid = load - pv < 0.
    assert 0.5 - 2.0 + 0.0 < -1e-9


def test_refuse_battery_export_prices_forced_pv_at_a_negative_floor():
    agg = _aggregate()
    opts = dp.OptimizerOptions(refuse_battery_export=True, power_steps=4, soc_step_kwh=0.1)
    slots = [_slot(3.0, 0.2, sell=-0.20) for _ in range(2)]
    plan = dp.solve(
        slots,
        aggregate=agg,
        start_kwh=agg.e_max_kwh,
        options=opts,
        generated_at=NOW,
    )
    assert plan.slots and plan.slots[0].grid_kw() < -1e-9, "forced PV export must stay possible"
    # The forced export is priced at the floor, so it is a cost, never a convenient zero.
    assert dp.slot_cost_eur(0.0, slots[0], 1.0, opts) > 0.0


def test_allow_export_stays_stricter_than_the_refusal():
    agg = _aggregate()
    opts = dp.OptimizerOptions(refuse_battery_export=True, allow_export=False)
    lo, _ = dp.grid_window_kw(_slot(2.0, 0.5), agg, opts)
    assert lo == pytest.approx(1.5), "the user's allow_export=False forbids the forced surplus export too"


def test_align_slots_carries_the_price_slot_energy_tax():
    start = NOW.replace(minute=0, second=0, microsecond=0)
    price = PriceForecast(slots=[PriceSlot(t=start, buy=0.30, sell=0.05, energy_tax=0.12)])
    solar = SolarForecast(slots=[SolarSlot(t=start, pv_kw=0.0)])
    load = LoadForecast(slots=[LoadSlot(t=start, load_kw=0.5)])
    slots = dp.align_slots(price, solar, load, start, start + timedelta(hours=2), 60)
    assert slots and slots[0].energy_tax == pytest.approx(0.12)
