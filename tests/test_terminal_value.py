"""Optimizer v2 stage 3 tests: the terminal-value vector (docs/OPTIMIZER_V2.md section 4).

Three layers, in increasing distance from the maths:

* ``optimize/terminal.py`` on its own -- tail construction, the two sufficiency conditions and the
  offset-invariant convergence test.
* ``dp`` wiring -- a vector row must reproduce the *concatenated* DP (horizon + tail solved as one
  long problem) exactly, mode-off must stay bit-for-bit salvage, and every disabled path must fall
  back to the salvage row with a stable reason.
* the guards -- the C core must never see a terminal row, and the fetcher's ``slots`` must not move.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager")
)

from app.models import (  # noqa: E402
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    SolarForecast,
    SolarSlot,
)
from app.optimize import _fast, dp  # noqa: E402
from app.optimize import terminal as tv  # noqa: E402
from app.sources import price as price_source  # noqa: E402
from app.sources.feed_in import FeedInRequest, PeriodState  # noqa: E402
import math  # noqa: E402
from dataclasses import replace  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    )
    data.update(overrides)
    return BatteryConfig(**data)


def make_slots(count, minutes=60, *, pv=0.0, load=0.5, buy=0.2, sell=0.05, start=T0, tail=False):
    def pick(value, index):
        if isinstance(value, (list, tuple)):
            return value[index % len(value)]
        return value

    return [
        dp.SlotInput(
            t=start + timedelta(minutes=minutes * i),
            pv_kw=pick(pv, i),
            load_kw=pick(load, i),
            buy=pick(buy, i),
            sell=pick(sell, i),
        )
        for i in range(count)
    ]


def agg_for(configs=None, reserve=0.0):
    return dp.BatteryAggregate.from_configs(configs or [battery()], reserve)


def options(**overrides) -> dp.OptimizerOptions:
    data = dict(slot_minutes=60, soc_step_kwh=0.05, power_steps=8, wear_eur_per_kwh=0.01)
    data.update(overrides)
    return dp.OptimizerOptions(**data)


def request(basis=0.33, *, rt=0.9, wear=0.01, margin=0.0) -> tv.TerminalRequest:
    entries = () if basis is None else (tv.BasisEntry(basis, rt, wear),)
    return tv.TerminalRequest(margin_eur_per_kwh=margin, basis=entries)


def plan_for(window, *, agg, opts, tail_price=(), feed_in=None, req=None, start=None,
             main_max_export_kwh=None, main_max_import_kwh=None, sell_pricing=None):
    return tv.plan_terminal(
        options=opts,
        aggregate=agg,
        window_slots=window,
        price_tail=list(tail_price),
        start=start or window[-1].t + timedelta(minutes=opts.slot_minutes),
        request=req,
        feed_in=feed_in,
        main_max_export_kwh=main_max_export_kwh,
        main_max_import_kwh=main_max_import_kwh,
        sell_pricing=sell_pricing,
        slot_minutes=float(opts.slot_minutes),
    )


def pricing_for(window, settings, *, tail_price=()):
    """The window's real pricing decision, through the production entry point."""
    return price_source.tail_sell_pricing(
        PriceForecast(
            source="test",
            slots=[
                PriceSlot(t=slot.t, buy=slot.buy, sell=slot.sell, source="test")
                for slot in window
            ],
            tail_slots=list(tail_price),
        ),
        settings,
    )


def sell_pricing(rule=None, *, ambiguous=False, unsupported=False, evidence=None):
    return price_source.TailSellPricing(rule, ambiguous, unsupported, evidence or {})


# --------------------------------------------------------------------------------------
# Tail construction
# --------------------------------------------------------------------------------------


def test_tail_slots_prefers_published_prices_and_repeats_the_window():
    window = make_slots(4, 60, pv=0.0, load=0.5, buy=[0.10, 0.20, 0.30, 0.40], sell=0.05)
    start = window[-1].t + timedelta(minutes=60)
    published = [
        PriceSlot(t=start, buy=0.99, sell=0.0, source="test"),
        PriceSlot(t=start + timedelta(minutes=60), buy=0.98, sell=0.0, source="test"),
    ]
    tail = tv.tail_slots(published, window, start=start, hours=3.0, slot_minutes=60.0)
    assert [slot.t for slot in tail] == [start + timedelta(minutes=60 * i) for i in range(3)]
    # The two published entries win; the third repeats the window's own time-of-day profile.
    assert [slot.buy for slot in tail] == [0.99, 0.98, 0.40]
    assert [slot.price_stale for slot in tail] == [False, False, True]
    # pv/load always come from the fetched window by time-of-day, and the sell price is the
    # horizon's own derived per-slot price (the contract settlement replaces it when wired).
    assert [slot.load_kw for slot in tail] == [0.5, 0.5, 0.5]
    assert [slot.sell for slot in tail] == [0.05, 0.05, 0.05]
    assert [slot.pv_source for slot in tail] == ["periodic"] * 3


def test_a_published_tail_price_gets_its_own_derived_sell():
    """A published entry's sell must come from *its* buy, not from the clock-time reference.

    The live contract derives ``sell = buy - fee``, so a 0.99 EUR/kWh tail slot must carry 0.99 - fee:
    reusing the reference slot's sell would price two different buys identically, and on a derived
    contract that (buy, sell) pair does not exist.
    """
    window = make_slots(4, 60, buy=[0.10, 0.20, 0.30, 0.40], sell=[0.08, 0.18, 0.28, 0.38])
    start = window[-1].t + timedelta(minutes=60)
    published = [PriceSlot(t=start, buy=0.99, sell=0.0, source="test")]
    rule = price_source.derived_sell_rule({"price.export_fee_eur_per_kwh": 0.02})
    tail = tv.tail_slots(
        published, window, start=start, hours=2.0, slot_minutes=60.0, derive_sell=rule
    )
    assert [slot.buy for slot in tail] == [0.99, 0.40]
    assert [slot.sell for slot in tail] == [pytest.approx(0.97), pytest.approx(0.38)]
    # Without the rule the pre-F3 behaviour stands (the reference's own sell): a caller with no
    # settings keeps pricing an explicit-sell window exactly as it did.
    plain = tv.tail_slots(published, window, start=start, hours=2.0, slot_minutes=60.0)
    assert [slot.sell for slot in plain] == [0.38, 0.38]


def test_the_tail_sell_pricing_follows_the_windows_own_provenance():
    """R1: the tail's sell prices reuse the rule the *fetched window* went through.

    The settlement replaces the tail's sells whenever it prices it, but with the feed-in wiring off
    the materialised sells *are* the contract: deriving ``buy - fee`` for a published tail entry
    would price the tail against a contract the plan does not have, while reusing the window's rate
    would do the reverse on a derived contract.  The forecast's own provenance is the fact that
    decides, not the settlement mode -- and with no provenance at all the choice is *ambiguous*,
    never guessed (the tail itself is then priced both ways; see the refusal test below).
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}

    def forecast(sells, provenance=None, buy=0.20):
        def pick(value, index):
            return value[index % len(value)] if isinstance(value, (list, tuple)) else value

        item = PriceForecast(
            source="test",
            slots=[
                PriceSlot(t=T0 + timedelta(hours=i), buy=pick(buy, i), sell=sell, source="test")
                for i, sell in enumerate(sells)
            ],
        )
        if provenance is not None:
            item.feed_in_source = provenance
        return item

    # An explicit rate really resolved: the tail keeps it, whatever its own buy price is.
    for provenance in ("flat", "entity"):
        pricing = price_source.tail_sell_pricing(forecast([0.14, 0.14], provenance), settings)
        assert pricing.rule is None
        assert pricing.ambiguous is False and pricing.unsupported is False
    # A derived window derives per slot -- a configured source that failed falls back to it too.
    for provenance in ("derived", "unavailable"):
        pricing = price_source.tail_sell_pricing(forecast([0.18, 0.18], provenance), settings)
        assert pricing.ambiguous is False and pricing.unsupported is False
        assert pricing.rule is not None
        assert pricing.rule(0.40, 0.0) == pytest.approx(0.38)
        assert pricing.rule(0.40, 0.05) == pytest.approx(0.38)  # refunded by default
    # No provenance: the window must fit at least one supported model, and even then the matrix does
    # not pick a rule -- which model priced the window is unidentified, so both readings of the *tail*
    # are compared (see the plan-level tests below).
    for fitting in (
        forecast([0.14, 0.14], buy=0.16),          # both models fit
        forecast([0.14, 0.18], buy=[0.16, 0.20]),  # derived only (0.20 and 0.22 sells)
        forecast([0.14, 0.14], buy=0.30),          # flat only: one rate, not buy - fee
    ):
        pricing = price_source.tail_sell_pricing(fitting, settings)
        assert pricing.ambiguous is True and pricing.unsupported is False
        assert pricing.rule is not None
    # Neither fitting is its own refusal: a variable explicit tariff (or garbage) justifies no
    # reading of the window at all.
    variable = price_source.tail_sell_pricing(forecast([0.14, 0.20]), settings)
    assert variable.unsupported is True and variable.ambiguous is False
    assert variable.evidence["first_unexplained_slot"]["slot"] == 1
    for garbage in ([float("nan"), 0.14], [float("inf"), 0.14]):
        assert price_source.tail_sell_pricing(forecast(garbage), settings).unsupported is True
    # A window with no slots carries no evidence either: both models fit vacuously, so the tail
    # comparison still decides (and no tail means nothing to disagree about).
    empty = price_source.tail_sell_pricing(PriceForecast(source="test", slots=[]), settings)
    assert empty.ambiguous is True and empty.unsupported is False
    # A non-numeric *buy* breaks only the derived model, so a flat window still fits it -- and a
    # non-numeric sell breaks both.
    malformed_buy = price_source.tail_sell_pricing(
        PriceForecast(source="test", slots=[PriceSlot(t=T0, buy="not a price", sell=0.14, source="test")]),
        settings,
    )
    assert malformed_buy.ambiguous is True and malformed_buy.unsupported is False
    malformed_sell = price_source.tail_sell_pricing(
        PriceForecast(source="test", slots=[PriceSlot(t=T0, buy=0.16, sell="not a price", source="test")]),
        settings,
    )
    assert malformed_sell.unsupported is True


def test_the_model_evidence_names_the_slot_that_is_actually_corrupt():
    """R5/D: a non-finite rate is evidence about *its own* slot, never about an earlier one.

    Here slot 1 breaks the derived model but still fits the flat one, and only slot 2 is garbage: the
    flat and "fits neither" evidence must both name slot 2, not inherit slot 1 from the derived break.
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}
    window = [
        PriceSlot(t=T0, buy=0.16, sell=0.14, source="test"),  # fits both (0.16 - 0.02 == 0.14)
        PriceSlot(t=T0 + timedelta(hours=1), buy=0.20, sell=0.14, source="test"),  # flat only
        PriceSlot(t=T0 + timedelta(hours=2), buy=0.30, sell=float("nan"), source="test"),
    ]
    pricing = price_source.tail_sell_pricing(PriceForecast(source="test", slots=window), settings)
    assert pricing.unsupported is True and pricing.ambiguous is False
    evidence = pricing.evidence
    assert evidence["derived_first_break"]["slot"] == 1
    assert evidence["flat_first_break"]["slot"] == 2
    assert evidence["first_unexplained_slot"]["slot"] == 2
    assert evidence["flat_first_break"]["sell_eur_per_kwh"] is None
    assert evidence["first_unexplained_slot"]["buy"] == pytest.approx(0.30)


def test_solve_from_forecasts_prices_the_tail_by_the_windows_own_contract(monkeypatch):
    """R1 integration: with the settlement off, the tail follows the *window's* pricing rule."""
    calls: list[dict] = []
    real = tv.tail_slots

    def spy(price_tail, window_slots, **kwargs):
        slots = real(price_tail, window_slots, **kwargs)
        calls.append({"rule": kwargs.get("derive_sell"), "slots": slots})
        return slots

    monkeypatch.setattr(tv, "tail_slots", spy)
    horizon = 12
    solar = SolarForecast(
        source="test",
        slots=[SolarSlot(t=T0 + timedelta(hours=i), pv_kw=0.0) for i in range(horizon)],
    )
    load = LoadForecast(
        source="test",
        slots=[LoadSlot(t=T0 + timedelta(hours=i), load_kw=0.5) for i in range(horizon)],
    )
    settings = {
        "optimizer.terminal_mode": "vector",
        "optimizer.terminal_tail_hours": 24.0,
        "optimizer.horizon_hours": horizon,
        "price.feed_in_mode": "off",
        "price.export_fee_eur_per_kwh": 0.02,
    }

    def run(window_sell, provenance, published_buy=0.99):
        calls.clear()
        item = PriceForecast(
            source="test",
            slots=[
                PriceSlot(t=T0 + timedelta(hours=i), buy=0.20, sell=window_sell, source="test")
                for i in range(horizon)
            ],
            tail_slots=[
                PriceSlot(t=T0 + timedelta(hours=horizon + i), buy=published_buy, sell=0.0,
                          source="test")
                for i in range(2)
            ],
        )
        if provenance is not None:
            item.feed_in_source = provenance
        plan = dp.solve_from_forecasts(
            item, solar, load, battery_configs=[battery()], start_kwh=1.0, settings=settings,
            now=T0,
        )
        native = calls[0]["slots"]
        return plan, [slot for slot in native if slot.price_source == "tail"]

    # Explicit (flat/entity): one rate for the whole contract, so the published tail entries keep
    # it even though their own buy price (0.99) differs from their time-of-day reference (0.20).
    plan, flat_tail = run(0.14, "flat")
    assert calls[0]["rule"] is None and len(calls) == 1
    assert flat_tail and all(slot.sell == pytest.approx(0.14) for slot in flat_tail)
    # Derived: the window's sells follow buy - fee, so each published entry derives from its own buy.
    plan, derived_tail = run(0.18, "derived")
    assert calls[0]["rule"] is not None and len(calls) == 1
    assert derived_tail and all(slot.sell == pytest.approx(0.97) for slot in derived_tail)
    # No provenance and both models fit: the tail is priced *both* ways and compared on its own
    # entries.  Here the published buy derives to 0.97 while the window's rate is 0.18, so the plan
    # refuses rather than pick a contract (R3).
    plan, _ = run(0.18, None)
    assert len(calls) == 2 and calls[1]["rule"] is None
    assert plan.terminal["reason"] == tv.REASON_TAIL_PRICING_AMBIGUOUS
    # ... and when both readings agree on every entry being priced the vector proceeds: a published
    # buy of 0.20 derives to the window's own 0.18, so the choice cannot matter here.
    plan, agreeing = run(0.18, None, published_buy=0.20)
    assert len(calls) == 2
    assert plan.terminal["reason"] != tv.REASON_TAIL_PRICING_AMBIGUOUS
    assert agreeing and all(slot.sell == pytest.approx(0.18) for slot in agreeing)
    # No provenance and only the *flat* model fits the window (one rate that is not buy - fee): the
    # rule is still not identified, so the tail is compared too -- and here the published buy would
    # derive to 0.97 while the window's rate is 0.14, so the plan refuses (R5/A).
    plan, _ = run(0.14, None)
    assert len(calls) == 2 and calls[1]["rule"] is None
    assert plan.terminal["reason"] == tv.REASON_TAIL_PRICING_AMBIGUOUS
    # ... and when the published buy derives to the reference rate the readings agree, so the vector
    # runs on with the flat window's own rate.
    plan, flat_agreeing = run(0.14, None, published_buy=0.16)
    assert len(calls) == 2
    assert plan.terminal["reason"] != tv.REASON_TAIL_PRICING_AMBIGUOUS
    assert flat_agreeing and all(slot.sell == pytest.approx(0.14) for slot in flat_agreeing)


def test_an_unprovenanced_window_prices_the_tail_both_ways_and_refuses_on_a_disagreement():
    """Round-3: matching observed prices does not identify the contract.

    A flat 0.14 tariff on a window of 0.16 buys *is* ``buy - 0.02``, so the window's own sells cannot
    say which contract priced them; defaulting either way silently re-prices the tail.  The tail is
    therefore priced under both readings and a disagreement refuses the vector
    (``tail_pricing_ambiguous``) instead of picking one.
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}
    derive = price_source.derived_sell_rule(settings)
    window = make_slots(6, 60, buy=0.16, sell=0.14)
    start = window[-1].t + timedelta(minutes=60)
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)
    published = [PriceSlot(t=start, buy=0.99, sell=0.0, source="test")]
    # This window fits *both* models (one rate, and buy - fee gives the same rate), which is exactly
    # the case the tail comparison exists for.
    pricing = pricing_for(window, settings, tail_price=published)
    assert pricing.ambiguous is True and pricing.unsupported is False
    common = dict(
        agg=agg_for(), opts=opts, tail_price=published, req=request(), start=start,
        sell_pricing=pricing,
    )

    verdict = plan_for(window, **common)
    assert verdict.reason == tv.REASON_TAIL_PRICING_AMBIGUOUS == "tail_pricing_ambiguous"
    assert verdict.ok is False
    conflict = verdict.detail["tail_pricing_ambiguous"]
    assert conflict["slot"] == 0
    assert conflict["t"] == start.isoformat()
    assert conflict["price_source"] == "tail"
    assert conflict["window_rate_eur_per_kwh"] == pytest.approx(0.14)
    assert conflict["derived_eur_per_kwh"] == pytest.approx(0.97)
    assert conflict["count"] == 1 and conflict["of_slots"] == 72
    assert "no feed-in provenance" in verdict.detail["sell_provenance"]
    # The tail itself exists -- the refusal is about its pricing, not its availability.  (It is the
    # 3L native tail the solve and the checks use: 3 x 24 h at the plan's own resolution.)
    assert len(verdict.slots) == 72

    # A published buy whose derived price *is* the window's rate: both readings agree on the entry
    # being priced, so the interpretation cannot matter and the vector runs on.
    agreeing = dict(common, tail_price=[PriceSlot(t=start, buy=0.16, sell=0.0, source="test")])
    settled = plan_for(window, **agreeing)
    assert "tail_pricing_ambiguous" not in settled.detail
    assert settled.reason != tv.REASON_TAIL_PRICING_AMBIGUOUS
    assert settled.detail["native_tail_slots"] == 72

    # With no published entry at all the two readings are identical by construction.
    filled = plan_for(window, **dict(common, tail_price=()))
    assert filled.reason != tv.REASON_TAIL_PRICING_AMBIGUOUS

    # An *unknown* window rate (``None``) is skipped by the model validation, so this stays a
    # comparison -- and a disagreement -- while a non-finite one is garbage that fits neither model
    # and refuses even earlier (R4's own slug, decided on the window alone).
    broken = [replace(window[0], sell=None), *window[1:]]
    unknown = pricing_for(broken, settings, tail_price=published)
    assert unknown.ambiguous is True and unknown.unsupported is False
    assert plan_for(broken, **dict(common, sell_pricing=unknown)).reason == (
        tv.REASON_TAIL_PRICING_AMBIGUOUS
    )
    for garbage in (float("nan"), float("inf")):
        broken = [replace(window[0], sell=garbage), *window[1:]]
        garbage_pricing = pricing_for(broken, settings, tail_price=published)
        assert garbage_pricing.unsupported is True
        assert plan_for(broken, **dict(common, sell_pricing=garbage_pricing)).reason == (
            tv.REASON_TAIL_PRICING_UNSUPPORTED
        )

    # A wired settlement replaces every tail sell with the contract's own price, so the ambiguity
    # cannot reach the solve or the floor check and the plan is decided on its merits instead.
    wired = dict(common, feed_in=_tiered_feed_in([(100.0, 0.05)]))
    assert plan_for(window, **wired).reason != tv.REASON_TAIL_PRICING_AMBIGUOUS
    assert derive(0.16, 0.0) == pytest.approx(0.14)  # the coincidence the test is built on


def test_a_window_that_fits_neither_model_refuses_before_the_tail_is_priced():
    """R4: agreement between the two readings does not establish that either is the contract.

    A variable explicit tariff (0.16 buys with 0.14/0.20 sells) fits neither supported model, yet a
    tail whose published buys derive to those same rates makes both readings agree -- so round 3's
    comparison alone would have let it through.  The window is validated first, and a window that
    fits neither model refuses with its own slug instead of falling back to one of the readings.
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}
    derive = price_source.derived_sell_rule(settings)
    window = make_slots(4, 60, buy=0.16, sell=[0.14, 0.20, 0.14, 0.20])
    start = window[-1].t + timedelta(minutes=60)
    # The tail's published buys derive to the reference rates (0.16 -> 0.14, 0.22 -> 0.20), so the
    # two readings agree on every entry the round-3 check would have compared.  The window covers
    # clock hours 0..3, so the two published moments are placed on the tail's own 0:00 and 1:00.
    published = [
        PriceSlot(t=start + timedelta(hours=20 + i), buy=buy, sell=0.0, source="test")
        for i, buy in enumerate((0.16, 0.22))
    ]
    pricing = pricing_for(window, settings, tail_price=published)
    assert pricing.unsupported is True and pricing.ambiguous is False
    evidence = pricing.evidence
    assert evidence["priced_slots"] == 4
    assert evidence["derived_fits"] is False and evidence["flat_fits"] is False
    assert evidence["flat_rate_eur_per_kwh"] == pytest.approx(0.14)
    assert evidence["derived_first_break"]["slot"] == 1
    assert evidence["flat_first_break"]["slot"] == 1
    unexplained = evidence["first_unexplained_slot"]
    assert unexplained["slot"] == 1 and unexplained["t"] == window[1].t.isoformat()
    assert unexplained["buy"] == pytest.approx(0.16)
    assert unexplained["sell_eur_per_kwh"] == pytest.approx(0.20)
    assert unexplained["derived_eur_per_kwh"] == pytest.approx(0.14)

    # Both readings of the tail agree, so only the model validation stands between this window and a
    # vector built on a contract the plan does not have.
    derived_side = tv.tail_slots(
        published, window, start=start, hours=24.0, slot_minutes=60.0, derive_sell=derive
    )
    window_side = tv.tail_slots(published, window, start=start, hours=24.0, slot_minutes=60.0)
    assert [slot.sell for slot in derived_side] == [slot.sell for slot in window_side]
    assert [slot.sell for slot in derived_side if slot.price_source == "tail"] == [
        pytest.approx(0.14), pytest.approx(0.20)
    ]

    # The plan refuses on the window alone: no tail is built, so there are no slots and no row.
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_mode="vector", terminal_tail_hours=24.0),
        tail_price=published, req=request(), start=start, sell_pricing=pricing,
    )
    assert verdict.reason == tv.REASON_TAIL_PRICING_UNSUPPORTED == "tail_pricing_unsupported"
    assert verdict.ok is False and verdict.slots == () and verdict.minutes == ()
    assert verdict.detail["tail_pricing_unsupported"] == evidence
    assert "no feed-in provenance" in verdict.detail["sell_provenance"]
    assert verdict.detail["native_tail_slots"] == 0

    # A wired settlement prices the *entire* tail from the contract itself, so the window's own
    # pricing is not the plan's contract and neither pricing refusal applies (R5/C): the tail is built
    # and the plan is decided by the settlement's own sufficiency checks instead.
    settled = plan_for(
        window, agg=agg_for(), opts=options(terminal_mode="vector", terminal_tail_hours=24.0),
        tail_price=published, req=request(), start=start, sell_pricing=pricing,
        feed_in=_tiered_feed_in([(100.0, 0.05)]), main_max_export_kwh=1.0,
    )
    assert settled.reason not in (tv.REASON_TAIL_PRICING_UNSUPPORTED, tv.REASON_TAIL_PRICING_AMBIGUOUS)
    assert settled.detail["native_tail_slots"] == 72 and settled.slots


def test_a_window_with_one_fitting_model_still_compares_both_readings():
    """R5/A: fitting one model does not identify the contract, so the tail decides either way.

    Buys 0.16/0.20 with sells 0.14/0.18 fit the derived rule exactly, and are equally an entity
    publishing those two rates -- so a later tail buy of 0.99 may not simply be priced 0.97.  Both
    readings are compared on the tail, and only agreement lets the vector through (the same for a
    flat-only window, whose tail entries would otherwise be re-priced from their own buys).
    """
    settings = {"price.export_fee_eur_per_kwh": 0.02}
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)

    # Derived-only window (0.16/0.14, 0.20/0.18): the derived reading says 0.99 - 0.02 = 0.97 for a
    # published 0.99 entry, the explicit one keeps its clock-time reference (0.14) -- refuse.
    derived_window = make_slots(4, 60, buy=[0.16, 0.20, 0.16, 0.20], sell=[0.14, 0.18, 0.14, 0.18])
    start = derived_window[-1].t + timedelta(minutes=60)
    published = [PriceSlot(t=start, buy=0.99, sell=0.0, source="test")]
    pricing = pricing_for(derived_window, settings, tail_price=published)
    assert pricing.ambiguous is True and pricing.unsupported is False
    assert pricing.evidence["derived_fits"] is True and pricing.evidence["flat_fits"] is False
    verdict = plan_for(
        derived_window, agg=agg_for(), opts=opts, tail_price=published, req=request(),
        start=start, sell_pricing=pricing,
    )
    assert verdict.reason == tv.REASON_TAIL_PRICING_AMBIGUOUS
    conflict = verdict.detail["tail_pricing_ambiguous"]
    assert conflict["derived_eur_per_kwh"] == pytest.approx(0.97)
    # The clock-time reference for the tail's first slot is the window's last slot (hour 4 is past it).
    assert conflict["window_rate_eur_per_kwh"] == pytest.approx(0.18)

    # ... and a published buy whose two readings agree (0.20 - 0.02 == the clock-time 0.18) lets the
    # vector through, priced the same either way.
    agreeing = [PriceSlot(t=start, buy=0.20, sell=0.0, source="test")]
    pricing = pricing_for(derived_window, settings, tail_price=agreeing)
    verdict = plan_for(
        derived_window, agg=agg_for(), opts=opts, tail_price=agreeing, req=request(),
        start=start, sell_pricing=pricing,
    )
    assert "tail_pricing_ambiguous" not in verdict.detail
    assert "tail_pricing_unsupported" not in verdict.detail
    assert [slot.sell for slot in verdict.slots if slot.price_source == "tail"] == [pytest.approx(0.18)]

    # Flat-only window (one explicit rate over varying buys): a 0.30 tail buy would derive to 0.28
    # while the explicit rate is 0.14 -- refuse; a 0.16 buy derives to the same 0.14 -- proceed.
    flat_window = make_slots(4, 60, buy=[0.16, 0.20, 0.24, 0.28], sell=0.14)
    start = flat_window[-1].t + timedelta(minutes=60)
    published = [PriceSlot(t=start, buy=0.30, sell=0.0, source="test")]
    pricing = pricing_for(flat_window, settings, tail_price=published)
    assert pricing.ambiguous is True and pricing.unsupported is False
    assert pricing.evidence["derived_fits"] is False and pricing.evidence["flat_fits"] is True
    verdict = plan_for(
        flat_window, agg=agg_for(), opts=opts, tail_price=published, req=request(),
        start=start, sell_pricing=pricing,
    )
    assert verdict.reason == tv.REASON_TAIL_PRICING_AMBIGUOUS
    assert verdict.detail["tail_pricing_ambiguous"]["derived_eur_per_kwh"] == pytest.approx(0.28)
    assert verdict.detail["tail_pricing_ambiguous"]["window_rate_eur_per_kwh"] == pytest.approx(0.14)
    agreeing = [PriceSlot(t=start, buy=0.16, sell=0.0, source="test")]
    pricing = pricing_for(flat_window, settings, tail_price=agreeing)
    verdict = plan_for(
        flat_window, agg=agg_for(), opts=opts, tail_price=agreeing, req=request(),
        start=start, sell_pricing=pricing,
    )
    assert "tail_pricing_ambiguous" not in verdict.detail
    assert [slot.sell for slot in verdict.slots if slot.price_source == "tail"] == [pytest.approx(0.14)]


def test_tail_slots_count_and_empty_cases():
    window = make_slots(4, 15)
    start = window[-1].t + timedelta(minutes=15)
    assert len(tv.tail_slots((), window, start=start, hours=24.0, slot_minutes=15.0)) == 96


def test_the_tail_resolution_averages_native_slots_within_each_period():
    """A coarse tail slot is the mean of its native sub-slots, and the bucket restarts per period.

    The restart is what keeps the phase alignment exact when the period is not a multiple of the
    ratio (4 + 2 native slots per period here): the duration pattern repeats, ``sum(minutes)`` is
    still exactly the native tail's span, and the DP sees the same energy over a coarser step.
    """
    window = make_slots(6, 15, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    start = window[-1].t + timedelta(minutes=15)
    published = [
        PriceSlot(t=start + timedelta(minutes=15 * i), buy=0.10 + 0.01 * i, sell=0.001 * i,
                  source="test")
        for i in range(24)
    ]
    native = tv.tail_slots(published, window, start=start, hours=6.0, slot_minutes=15.0)
    coarse, minutes = tv.aggregate_tail(
        native, native_slots_per_period=6, ratio=4, native_minutes=15.0
    )
    assert len(coarse) == 8  # (4 + 2) native slots per period, four periods
    assert minutes == [60.0, 30.0] * 4
    assert sum(minutes) == 24 * 15.0  # the coarse tail spans exactly the native tail
    first, second = coarse[0], coarse[1]
    # The bucket means its native sub-slots -- price *and* the pv/load that drive the energy.
    assert first.buy == pytest.approx(0.115)  # (0.10 + 0.11 + 0.12 + 0.13) / 4
    assert second.buy == pytest.approx(0.145)  # (0.14 + 0.15) / 2
    assert first.sell == pytest.approx(0.05)  # the horizon's own sell price, averaged too
    assert first.pv_kw == pytest.approx(sum(slot.pv_kw for slot in native[0:4]) / 4.0)
    assert first.load_kw == pytest.approx(sum(slot.load_kw for slot in native[0:4]) / 4.0)
    # Price is linear in the slot, so the coarse and native tails must price identically.
    assert sum(m / 60.0 * slot.buy for slot, m in zip(coarse, minutes)) == pytest.approx(
        sum(15.0 / 60.0 * slot.buy for slot in native)
    )
    # ratio <= 1 is the identity: the native tail and its native per-slot minutes.
    same, same_minutes = tv.aggregate_tail(
        native, native_slots_per_period=6, ratio=1, native_minutes=15.0
    )
    assert same == list(native)
    assert same_minutes == [15.0] * len(native)
    assert tv.tail_slots((), window, start=start, hours=0.0, slot_minutes=15.0) == []
    assert tv.tail_slots((), [], start=start, hours=24.0, slot_minutes=15.0) == []


def test_price_slots_are_unchanged_by_the_tail():
    """Stage 3 is additive: the fetcher's ``slots`` must not move (live path/settlement)."""
    pairs = [(T0 + timedelta(hours=i), 0.20 + 0.01 * i) for i in range(12)]
    with_tail = price_source._build_forecast(
        pairs,
        source="test",
        settings={"optimizer.horizon_hours": 6, "optimizer.terminal_tail_hours": 24},
        now=T0,
    )
    without = price_source._build_forecast(
        pairs,
        source="test",
        settings={"optimizer.horizon_hours": 6, "optimizer.terminal_tail_hours": 0},
        now=T0,
    )
    assert with_tail.slots == without.slots
    assert [slot.t for slot in with_tail.slots] == [slot.t for slot in without.slots]
    assert without.tail_slots == []
    assert len(with_tail.tail_slots) == 96
    assert with_tail.tail_slots[0].t == with_tail.slots[-1].t + timedelta(minutes=15)


# --------------------------------------------------------------------------------------
# The continuation row is the true tail value
# --------------------------------------------------------------------------------------


def _tiny_case():
    """A family where every reachable energy and every action lands exactly on the grid."""
    config = battery(capacity_kwh=4.0, min_soc_pct=25.0, max_charge_w=1000.0, max_discharge_w=1000.0,
                     eta_charge=1.0, eta_discharge=1.0)
    agg = dp.BatteryAggregate.from_configs([config], 0.0)
    opts = options(soc_step_kwh=1.0, power_steps=1, wear_eur_per_kwh=0.0)
    return agg, opts


def _brute_force(slots, agg, opts, start_kwh, salvage):
    """Exhaustive enumeration of every grid-aligned path (tiny instances only)."""
    grid, _step = dp._build_grid(agg, opts.soc_step_kwh, dp._anchors(start_kwh, agg, None))
    dt = opts.slot_minutes / 60.0
    best = float("inf")

    def walk(index, energy, cost):
        nonlocal best
        if index == len(slots):
            best = min(best, cost - salvage * energy)
            return
        slot = slots[index]
        lo, hi, _flags = dp.power_bounds_kw(slot, energy, agg, opts, dt)
        for power in dp._actions(lo, hi, opts.power_steps, None):
            nxt = dp.next_energy_kwh(energy, power, dt, agg)
            if not any(abs(point - nxt) <= 1e-9 for point in grid):
                raise AssertionError("the tiny family must stay on the grid")
            step_cost = dp.slot_cost_eur(power, slot, dt, opts) + dp.wear_cost_eur(
                opts.wear_eur_per_kwh, agg.eta_discharge, energy, nxt
            )
            walk(index + 1, nxt, cost + step_cost)

    walk(0, start_kwh, 0.0)
    return best


def test_vector_row_equals_concatenated_dp_and_brute_force():
    """The vector must equal solving horizon+tail as one DP -- and that DP must equal brute force."""
    agg, opts = _tiny_case()
    start = 2.0
    horizon = make_slots(2, 60, pv=0.0, load=0.0, buy=[0.30, 0.10], sell=0.05)
    tail = make_slots(2, 60, pv=0.0, load=0.0, buy=[0.40, 0.05], sell=0.05,
                      start=horizon[-1].t + timedelta(minutes=60))
    salvage = 0.2 * agg.eta_discharge
    minutes = [60.0, 60.0]

    concatenated = dp._solve_once_numeric(
        horizon + tail, agg, start, opts, dp.TARGET_HARD_PENALTY, None, salvage, minutes * 2
    )
    grid = dp._main_grid(agg, opts, 2, dp._anchors(start, agg, None)).points
    row = tv.continuation_row(grid, tail, minutes, agg, opts, salvage)
    vector = dp._solve_once_numeric(
        horizon, agg, start, opts, dp.TARGET_HARD_PENALTY, None, salvage, minutes, terminal_row=row
    )

    brute = _brute_force(horizon + tail, agg, opts, start, salvage)
    assert concatenated.objective_eur == pytest.approx(brute, abs=1e-9)
    assert vector.objective_eur == pytest.approx(concatenated.objective_eur, abs=1e-9)
    assert vector.powers == concatenated.powers[: len(horizon)]
    assert vector.energies == concatenated.energies[: len(horizon)]


def test_vector_row_equals_the_concatenated_dp_on_a_larger_instance():
    """The vector row must equal the boundary value function of the concatenated DP, exactly.

    The *decisions* then match slot for slot.  The reported objective is not comparable to the
    concatenated run's: the DP's value at an off-grid energy is the linear interpolation of the
    grid row (an upper bound of the greedy path's own cost), and only the vector run reports the
    value it actually minimised.
    """
    agg = agg_for()
    opts = options(soc_step_kwh=0.02, power_steps=12, wear_eur_per_kwh=0.02)
    start = 1.2
    horizon = make_slots(6, 60, pv=[0.0, 1.5, 0.4, 0.0, 0.0, 0.0], load=0.6,
                         buy=[0.28, 0.12, 0.09, 0.31, 0.35, 0.22], sell=0.05)
    tail = make_slots(
        6, 60, pv=[0.0, 1.2, 0.3, 0.0, 0.0, 0.0], load=0.6,
        buy=[0.30, 0.11, 0.08, 0.33, 0.36, 0.24], sell=0.05,
        start=horizon[-1].t + timedelta(minutes=60),
    )
    salvage = 0.15
    minutes = [60.0] * 6
    grid = dp._main_grid(agg, opts, 6, dp._anchors(start, agg, None)).points
    row = tv.continuation_row(grid, tail, minutes, agg, opts, salvage)
    # The concatenated DP's value function at the horizon boundary is *the* row, bit for bit.
    value, _argmin, _iterations = dp._backward_sweep(
        grid, horizon + tail, minutes * 2, agg, opts,
        dp._salvage_terminal_row(grid, salvage, 0.0, None),
    )
    assert value[len(horizon)] == row
    concatenated = dp._solve_once_numeric(
        horizon + tail, agg, start, opts, dp.TARGET_HARD_PENALTY, None, salvage, minutes * 2
    )
    vector = dp._solve_once_numeric(
        horizon, agg, start, opts, dp.TARGET_HARD_PENALTY, None, salvage, minutes, terminal_row=row
    )
    assert vector.powers == concatenated.powers[: len(horizon)]
    assert vector.energies == concatenated.energies[: len(horizon)]
    # Site 3: the reported objective's terminal term is the same interpolated row.
    e_now = vector.energies[-1]
    dt = 1.0
    e_end = dp.clamp(
        dp.next_energy_kwh(e_now, vector.powers[-1], dt, agg), agg.e_min_kwh, agg.e_max_kwh
    )
    prefix = sum(
        dp.slot_cost_eur(power, slot, dt, opts)
        for power, slot in zip(vector.powers, horizon)
    )
    assert vector.objective_eur == pytest.approx(
        prefix + vector.cycle_cost_eur + dp._interp(row, grid, e_end), abs=1e-9
    )


def test_stability_span_is_offset_invariant():
    short = [0.0, -0.1, -0.3, -0.6]
    shifted = [value + 12.5 for value in short]
    assert tv.stability_span(short, shifted) == pytest.approx(0.0, abs=1e-12)
    drifting = [0.0, -0.1, -0.3, -0.75]
    assert tv.stability_span(short, drifting) == pytest.approx(0.15, abs=1e-12)
    assert tv.stability_span(short, short[:2]) == float("inf")
    assert tv.stability_span([], []) == float("inf")


# --------------------------------------------------------------------------------------
# dp wiring: mode off, disabled, and the plan block
# --------------------------------------------------------------------------------------


def _solve(window, opts, *, agg=None, terminal=None, start=1.2):
    agg = agg or agg_for()
    return dp.solve(
        window, aggregate=agg, start_kwh=start, options=opts, generated_at=T0, terminal=terminal
    )


def test_mode_off_is_bit_for_bit_salvage():
    window = make_slots(4, 60, pv=[0.0, 1.0, 0.0, 0.0], buy=[0.25, 0.10, 0.30, 0.20])
    off = _solve(window, options())
    salvage = _solve(window, options(terminal_mode="salvage"))
    assert off.summary.objective_eur == salvage.summary.objective_eur
    assert [slot.power_w for slot in off.slots] == [slot.power_w for slot in salvage.slots]
    assert [slot.soc_kwh for slot in off.slots] == [slot.soc_kwh for slot in salvage.slots]
    assert getattr(off, "terminal", None) is None


def test_a_disabled_vector_plan_is_bit_for_bit_salvage():
    window = make_slots(4, 60, pv=[0.0, 1.0, 0.0, 0.0], buy=[0.25, 0.10, 0.30, 0.20])
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.3767)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=request())
    assert verdict.ok is False
    plan = _solve(window, opts, terminal=verdict)
    baseline = _solve(window, options(terminal_mode="salvage", sell_floor_eur_per_kwh=0.3767))
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]
    assert plan.summary.objective_eur == baseline.summary.objective_eur
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_FLOOR_BINDS
    assert plan.summary.terminal_reason == tv.REASON_FLOOR_BINDS
    assert plan.summary.terminal_mode == "vector"


def test_live_shaped_tail_disables_with_floor_binds():
    """The live contract's shape: floor on, basis ~0.33, sell = buy ranging below the floor.

    The vector must be *disabled* here, and the plan must equal the salvage arm.  This is the
    honest outcome of section 4's sufficiency check, not a bug.
    """
    window = make_slots(6, 60, pv=0.0, load=0.5, buy=[0.18, 0.21, 0.25, 0.28, 0.24, 0.20],
                        sell=[0.18, 0.21, 0.25, 0.28, 0.24, 0.20])
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.33, terminal_tail_hours=6.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=request(basis=0.33, rt=0.9, wear=0.02))
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_FLOOR_BINDS
    assert verdict.detail["floor_binds"]["leg"] == "export"
    assert verdict.detail["floor_hi_eur_per_kwh"] > 0.33
    plan = _solve(window, opts, terminal=verdict)
    baseline = _solve(window, options(terminal_mode="salvage", sell_floor_eur_per_kwh=0.33))
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]
    assert plan.summary.objective_eur == baseline.summary.objective_eur


def test_floor_binds_on_the_house_leg_too():
    window = make_slots(3, 60, pv=0.0, load=0.5, buy=0.10, sell=0.50)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.33, terminal_tail_hours=3.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=request(basis=0.33, rt=1.0, wear=0.0))
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_FLOOR_BINDS
    assert verdict.detail["floor_binds"]["leg"] == "house"


def test_floor_without_a_ledger_is_unprovable():
    window = make_slots(3, 60, buy=0.10, sell=0.50)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.33, terminal_tail_hours=3.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=None)
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BASIS_UNAVAILABLE


def test_a_floor_that_provably_never_binds_is_sufficient():
    window = make_slots(3, 60, buy=0.10, sell=0.50)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.05, terminal_tail_hours=3.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=request(basis=0.01, rt=1.0, wear=0.0))
    assert verdict.ok is True
    assert verdict.detail["floor"] == "provably never binds over the tail"


def test_one_battery_without_a_basis_disables_the_vector():
    """The bound is the *fleet's*: one valid battery plus one unknown one is not provable.

    Skipping the unknown entry would report the valid battery's bound and approve the vector, but
    the unknown battery's basis may be a past charge price above every price in this window, or it
    may acquire one by charging during the tail -- either way it then enters the fleet mean with its
    own efficiency and wear.  The refusal stays distinguishable from a genuine floor refusal.
    """
    window = make_slots(3, 60, buy=0.10, sell=0.50)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.30, terminal_tail_hours=24.0)
    known = tv.BasisEntry(0.01, 1.0, 0.0)
    unknown = tv.BasisEntry(None, 1.0, 0.0)
    verdict = plan_for(
        window, agg=agg_for(), opts=opts, req=tv.TerminalRequest(basis=(known, unknown))
    )
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BASIS_UNAVAILABLE
    assert verdict.reason != tv.REASON_FLOOR_BINDS
    assert verdict.detail["basis_unprovable"] == [1]
    assert "floor_binds" not in verdict.detail
    # A fleet whose batteries can all state their bound still passes on the same shape.
    ok = plan_for(window, agg=agg_for(), opts=opts, req=tv.TerminalRequest(basis=(known, known)))
    assert ok.ok is True
    # Non-finite or non-positive facts are unprovable too, one entry at a time.
    for entry in (
        tv.BasisEntry(0.01, 0.0, 0.0),
        tv.BasisEntry(0.01, float("nan"), 0.0),
        tv.BasisEntry(float("inf"), 1.0, 0.0),
        tv.BasisEntry(0.01, 1.0, float("nan")),
    ):
        bad = plan_for(window, agg=agg_for(), opts=opts, req=tv.TerminalRequest(basis=(entry,)))
        assert bad.reason == tv.REASON_BASIS_UNAVAILABLE
        assert bad.detail["basis_unprovable"] == [0]


def test_an_unknown_reachable_export_bound_disables_the_vector():
    window = make_slots(3, 60)
    feed_in = FeedInRequest(settings={"price.feed_in_mode": "tiered"}, period_state=PeriodState())
    verdict = plan_for(window, agg=agg_for(), opts=options(terminal_tail_hours=3.0), feed_in=feed_in)
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BILLING_REACHABLE
    assert verdict.detail["reachability"]


def test_a_refusing_contract_disables_the_vector():
    window = make_slots(3, 60)
    feed_in = FeedInRequest(
        settings={"price.feed_in_mode": "derived"},
        period_state=PeriodState(),
    )
    verdict = plan_for(
        window,
        agg=agg_for(),
        opts=options(terminal_tail_hours=3.0),
        feed_in=feed_in,
    )
    assert verdict.ok is False
    assert verdict.reason in (tv.REASON_BILLING_REACHABLE, tv.REASON_CONTRACT_UNPRICEABLE)


def test_an_unpriceable_contract_disables_the_vector():
    window = make_slots(3, 60)
    feed_in = FeedInRequest(settings={"price.feed_in_mode": "not-a-mode"}, period_state=PeriodState())
    verdict = plan_for(window, agg=agg_for(), opts=options(terminal_tail_hours=3.0), feed_in=feed_in)
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_CONTRACT_UNPRICEABLE


def test_a_settlement_that_does_not_price_every_tail_slot_refuses(monkeypatch):
    """R6/C: the settlement carve-out is sound only while *every* tail slot really is priced.

    A settlement that returns fewer prices than slots (or a non-finite one) must refuse the vector:
    it must never keep the materialised, guessed sell for a slot the settlement left out, and it must
    not crash on a short list either.  ``settle_feed_in`` is exact-or-refuse by contract; this pins
    the boundary that no longer has to trust that convention.  The refused plan is a report, not a
    decision -- the solve below stays bit-for-bit salvage, exactly as for any other refusal.
    """
    window = make_slots(4, 60, pv=0.0, load=0.5, buy=0.20, sell=0.14)
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)
    feed_in = _tiered_feed_in([(100.0, 0.05)])
    real = tv.settle_feed_in
    lengths: list[int] = []

    def short(settings, slots, state, limits):
        result = real(settings, slots, state, limits)
        lengths.append(len(result.sells))
        return replace(result, sells=result.sells[:-1])

    monkeypatch.setattr(tv, "settle_feed_in", short)
    verdict = plan_for(window, agg=agg_for(), opts=opts, feed_in=feed_in, main_max_export_kwh=1.0)
    assert lengths, "the settlement must have been asked to price the tail"
    assert lengths[0] == verdict.detail["native_tail_slots"]
    assert verdict.ok is False and verdict.reason == tv.REASON_CONTRACT_UNPRICEABLE
    assert verdict.detail["contract_error"] == (
        f"the settlement priced {lengths[0] - 1} of {lengths[0]} tail slots; the vector needs a "
        "price for every tail slot"
    )
    plan = _solve(window, opts, terminal=verdict)
    baseline = _solve(window, options(terminal_mode="salvage"))
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]
    assert plan.summary.objective_eur == baseline.summary.objective_eur
    assert plan.terminal["ok"] is False and plan.terminal["reason"] == tv.REASON_CONTRACT_UNPRICEABLE

    def unpriced(settings, slots, state, limits):
        result = real(settings, slots, state, limits)
        return replace(result, sells=[float("nan")] + list(result.sells[1:]))

    monkeypatch.setattr(tv, "settle_feed_in", unpriced)
    verdict = plan_for(window, agg=agg_for(), opts=opts, feed_in=feed_in, main_max_export_kwh=1.0)
    assert verdict.ok is False and verdict.reason == tv.REASON_CONTRACT_UNPRICEABLE
    assert "left tail slot 0 unpriced (nan)" in verdict.detail["contract_error"]


def test_a_tail_that_would_exceed_the_iteration_budget_is_refused(monkeypatch):
    monkeypatch.setattr(dp, "MAX_ITERATIONS", 1000)
    window = make_slots(4, 15, pv=0.0, load=0.5, buy=0.2, sell=0.05)
    opts = options(slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts)
    assert verdict.ok is True  # the tail itself is fine; the *budget* is what fails
    plan = _solve(window, opts, terminal=verdict)
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_TAIL_TOO_LONG
    assert plan.terminal["detail"]["solve_iterations"] > 1000


def test_a_spike_tail_fails_the_stability_test():
    """A one-slot opportunity in each period makes the per-period value state-dependent.

    A block of cheap or expensive slots repeats its state at the period boundary and converges; a
    single spike does not, because how much energy was carried into it changes the value of the
    next period.  Failing closed to ``salvage`` is the designed outcome.
    """
    window = make_slots(6, 60, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    start = window[-1].t + timedelta(minutes=60)
    spiky = [
        PriceSlot(t=start + timedelta(hours=i), buy=0.05 if i % 24 == 3 else 0.20, sell=0.0,
                  source="test")
        for i in range(72)
    ]
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, tail_price=spiky)
    assert verdict.ok is True
    plan = _solve(window, opts, terminal=verdict)
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_TAIL_UNSTABLE
    assert plan.terminal["stability_span_eur"] > plan.terminal["stability_tolerance_eur"]
    baseline = _solve(window, options(terminal_mode="salvage"))
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]


def test_a_blocky_tail_is_stable_and_the_row_changes_the_schedule():
    """A cheap block in each period: the vector converges *and* is not decision-neutral."""
    window = make_slots(6, 60, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    start = window[-1].t + timedelta(minutes=60)
    blocky = [
        PriceSlot(t=start + timedelta(hours=i), buy=0.02 if i % 24 < 6 else 0.20, sell=0.0,
                  source="test")
        for i in range(72)
    ]
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, tail_price=blocky)
    assert verdict.ok is True
    plan = _solve(window, opts, terminal=verdict)
    assert plan.terminal["ok"] is True
    assert plan.terminal["reason"] is None
    assert plan.terminal["stability_span_eur"] < plan.terminal["stability_tolerance_eur"]
    assert plan.summary.terminal_mode == "vector"
    assert plan.summary.terminal_reason is None
    baseline = _solve(window, options(terminal_mode="salvage"))
    assert [slot.power_w for slot in plan.slots] != [slot.power_w for slot in baseline.slots]


def test_the_target_penalty_stays_additive_on_the_vector_row():
    window = make_slots(6, 60, pv=0.0, load=0.4, buy=0.25, sell=0.05)
    opts = options(terminal_mode="vector", terminal_tail_hours=6.0, target_soc_pct_end=80.0)
    agg = agg_for()
    verdict = plan_for(window, agg=agg, opts=opts)
    assert verdict.ok is True
    plan = _solve(window, opts, terminal=verdict)
    target = agg.capacity_kwh * 0.80
    last = plan.slots[-1]
    e_end = dp.next_energy_kwh(
        last.soc_kwh, last.power_w / 1000.0, last.minutes / 60.0, agg
    )
    assert abs(e_end - target) <= max(plan.summary.effective_step_kwh / 2.0, 1e-6)
    assert plan.infeasible_reason != "terminal_target_unreachable"


def test_a_supplied_terminal_plan_that_is_disabled_keeps_the_salvage_row():
    window = make_slots(4, 60, pv=0.0, load=0.5, buy=0.2, sell=0.05)
    disabled = tv.TerminalPlan(False, tv.REASON_FLOOR_BINDS, {"floor": "test"})
    plan = _solve(window, options(terminal_mode="vector"), terminal=disabled)
    baseline = _solve(window, options(terminal_mode="salvage"))
    assert plan.summary.objective_eur == baseline.summary.objective_eur
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]
    assert plan.terminal["reason"] == tv.REASON_FLOOR_BINDS


def test_vector_mode_without_a_prepared_tail_stays_on_salvage():
    """``solve()`` called directly (an engine recompute) must report why it fell back."""
    window = make_slots(4, 60, pv=0.0, load=0.5, buy=0.2, sell=0.05)
    plan = _solve(window, options(terminal_mode="vector"))
    baseline = _solve(window, options(terminal_mode="salvage"))
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_TAIL_UNAVAILABLE
    assert plan.summary.terminal_reason == tv.REASON_TAIL_UNAVAILABLE
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in baseline.slots]
    assert plan.summary.objective_eur == baseline.summary.objective_eur


def test_solve_from_forecasts_threads_the_tail():
    horizon = 12
    prices = [
        PriceSlot(t=T0 + timedelta(hours=i), buy=0.18 + 0.01 * (i % 6), sell=0.05, source="test")
        for i in range(horizon)
    ]
    tail = [
        PriceSlot(t=T0 + timedelta(hours=horizon + i), buy=0.16 + 0.01 * (i % 6), sell=0.0,
                  source="test")
        for i in range(horizon)
    ]
    solar = SolarForecast(
        source="test",
        slots=[SolarSlot(t=T0 + timedelta(hours=i), pv_kw=1.0 if 8 <= i % 24 <= 16 else 0.0)
               for i in range(horizon)],
    )
    load = LoadForecast(
        source="test",
        slots=[LoadSlot(t=T0 + timedelta(hours=i), load_kw=0.5) for i in range(horizon)],
    )
    plan = dp.solve_from_forecasts(
        PriceForecast(source="test", slots=prices, tail_slots=tail),
        solar,
        load,
        battery_configs=[battery()],
        start_kwh=1.0,
        settings={
            "optimizer.terminal_mode": "vector",
            "optimizer.terminal_tail_hours": 12.0,
            "optimizer.horizon_hours": 12,
        },
        now=T0,
    )
    assert plan.terminal["mode"] == "vector"
    # A 12 h request is rounded up to one whole 24 h profile period: the extension increment must be
    # a whole number of synthetic-profile days (see the phase-alignment tests below).
    assert plan.terminal["tail_hours"] == 24.0
    assert plan.terminal["periods"] == 1
    assert plan.terminal["detail"]["tail_hours_requested"] == 12.0
    assert plan.terminal["stability_tolerance_eur"] > 0.0
    assert plan.summary.terminal_mode == "vector"


def test_solve_from_forecasts_hands_the_tail_the_window_derivation(monkeypatch):
    """The production path must price tail slots with the rule the *fetched window* used.

    R1: that rule is read from the forecast's own pricing provenance, so this window is a derived
    one (``sell = buy - fee``, provenance ``derived``) -- an explicit window keeps its rate instead.
    """
    seen: dict = {}
    real = tv.tail_slots

    def spy(price_tail, window_slots, **kwargs):
        seen.update(kwargs)
        return real(price_tail, window_slots, **kwargs)

    monkeypatch.setattr(tv, "tail_slots", spy)
    horizon = 12
    prices = [
        PriceSlot(t=T0 + timedelta(hours=i), buy=0.18 + 0.01 * (i % 6),
                  sell=0.16 + 0.01 * (i % 6), source="test")
        for i in range(horizon)
    ]
    tail = [
        PriceSlot(t=T0 + timedelta(hours=horizon + i), buy=0.40, sell=0.0, source="test")
        for i in range(horizon)
    ]
    price_forecast = PriceForecast(source="test", slots=prices, tail_slots=tail)
    price_forecast.feed_in_source = "derived"
    solar = SolarForecast(
        source="test",
        slots=[SolarSlot(t=T0 + timedelta(hours=i), pv_kw=0.0) for i in range(horizon)],
    )
    load = LoadForecast(
        source="test",
        slots=[LoadSlot(t=T0 + timedelta(hours=i), load_kw=0.5) for i in range(horizon)],
    )
    dp.solve_from_forecasts(
        price_forecast,
        solar,
        load,
        battery_configs=[battery()],
        start_kwh=1.0,
        settings={
            "optimizer.terminal_mode": "vector",
            "optimizer.terminal_tail_hours": 24.0,
            "optimizer.horizon_hours": 12,
            "price.export_fee_eur_per_kwh": 0.02,
        },
        now=T0,
    )
    derive = seen["derive_sell"]
    assert derive is not None
    assert derive(0.40, 0.0) == pytest.approx(0.38)
    # Refunded by default, so a known energy-tax component is not deducted (the fetch loop agrees).
    assert derive(0.40, 0.05) == pytest.approx(0.38)


# --------------------------------------------------------------------------------------
# C1: the basis bound and the billing-state set
# --------------------------------------------------------------------------------------


def test_the_basis_bound_includes_the_main_window():
    """A high charge price in the MAIN window raises the basis the tail inherits.

    The tail's own prices would clear this plan (basis 0.01, tail sell 0.10), but the pack may have
    been charged at 0.60 during the planning window, so the floor really binds.
    """
    window = make_slots(4, 60, pv=0.0, load=0.5, buy=[0.60, 0.10, 0.10, 0.10], sell=0.10)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.30, terminal_tail_hours=4.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts, req=request(basis=0.01, rt=1.0, wear=0.0))
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_FLOOR_BINDS
    assert verdict.detail["main_max_buy_eur_per_kwh"] == pytest.approx(0.60)
    assert verdict.detail["floor_binds"]["leg"] == "export"
    assert verdict.detail["floor_binds"]["slot"] == 0


def test_the_basis_bound_is_running_not_a_global_maximum():
    """A charge *inside* the tail raises the bound only for the slots after it.

    A single global maximum (0.60, in the last tail slot) would fail every slot here; the running
    bound must accept, because no discharge can be restricted by a charge price that has not been
    paid yet.
    """
    window = make_slots(3, 60, pv=0.0, load=0.5, buy=0.10, sell=0.30)
    start = window[-1].t + timedelta(minutes=60)
    prices = [
        PriceSlot(t=start + timedelta(hours=i), buy=0.60 if i == 71 else 0.10, sell=0.0,
                  source="test")
        for i in range(72)
    ]
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.30, terminal_tail_hours=6.0)
    verdict = plan_for(
        window, agg=agg_for(), opts=opts, tail_price=prices,
        req=request(basis=0.10, rt=1.0, wear=0.0),
    )
    assert verdict.ok is True
    assert verdict.detail["floor"] == "provably never binds over the tail"
    # The 6 h request is one 24 h profile period, so the tail is 72 native slots and the 0.60 charge
    # price is the *last* slot: no discharge can be restricted by it, and the plan still passes.
    assert len(verdict.slots) == 72


def _tiered_feed_in(tiers, *, discharge=1.0):
    return FeedInRequest(
        settings={"price.feed_in_mode": "tiered"},
        period_state=PeriodState(import_kwh=0.0, export_kwh=0.0),
        max_discharge_kw=discharge,
        tiers=tiers,
    )


def _saldering_feed_in(import_now, export_now, *, discharge=1.0):
    return FeedInRequest(
        settings={"price.feed_in_mode": "saldering"},
        period_state=PeriodState(import_kwh=import_now, export_kwh=export_now),
        max_discharge_kw=discharge,
    )


def test_the_billing_check_settles_every_reachable_state(monkeypatch):
    """Both endpoints and every tier boundary the reachable interval can cross are settled."""
    window = make_slots(1, 60, pv=0.0, load=0.5, buy=0.20, sell=0.20)
    # No discharge and no PV surplus: the tail's own reachable export is 0, so every candidate's
    # tier stays priceable and the candidate *set* is what this test observes.
    feed_in = _tiered_feed_in(((5.0, 0.10), (math.inf, 0.10)), discharge=0.0)
    seen = []
    real = tv.settle_feed_in

    def spy(settings, slots, period_state, limits):
        seen.append(limits.tier_volume_kwh)
        return real(settings, slots, period_state, limits)

    monkeypatch.setattr(tv, "settle_feed_in", spy)
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_tail_hours=1.0), feed_in=feed_in,
        main_max_export_kwh=12.0,
    )
    # The reachable volume spans [0, 12] and crosses the 5 kWh boundary; every rate agrees, so the
    # vector stays eligible.
    assert sorted(seen) == [0.0, 5.0, 12.0]
    assert verdict.ok is True


def test_a_rate_change_across_reachable_states_disables_the_vector(monkeypatch):
    """A priced tail that differs between reachable states is not a function of stored energy."""
    window = make_slots(1, 60, pv=0.0, load=0.5, buy=0.20, sell=0.20)
    feed_in = _tiered_feed_in(((5.0, 0.10), (math.inf, 0.02)), discharge=0.0)
    real = tv.settle_feed_in
    seen = []

    def spy(settings, slots, period_state, limits):
        seen.append(limits.tier_volume_kwh)
        return real(settings, slots, period_state, limits)

    monkeypatch.setattr(tv, "settle_feed_in", spy)
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_tail_hours=1.0), feed_in=feed_in,
        main_max_export_kwh=12.0,
    )
    assert sorted(seen) == [0.0, 5.0]  # stops at the first disagreement
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BILLING_REACHABLE
    assert "differs across reachable" in verdict.detail["billing_state"]["reachable"]


def test_the_billing_interval_spans_the_import_side_too(monkeypatch):
    """Both period totals are reachable: imports only grow, so they restore the allowance.

    The allowance at the tail's start is ``import - export``.  A candidate set that moved only the
    export total would never settle the state where the household *imported* during the horizon --
    which restores an allowance the tail's pricing depends on.  The reachable interval is
    ``[import_now - (export_now + max_export), (import_now + max_import) - export_now]``.
    """
    window = make_slots(1, 60, pv=0.0, load=0.5, buy=0.20, sell=0.20)
    # No discharge and no PV surplus: the tail's own reachable export is 0, so both ends of the
    # allowance interval stay in the netting branch and the candidate *set* is what this observes.
    feed_in = _saldering_feed_in(50.0, 10.0, discharge=0.0)  # allowance +40 today
    seen = []
    real = tv.settle_feed_in

    def spy(settings, slots, period_state, limits):
        seen.append((period_state.import_kwh, period_state.export_kwh))
        return real(settings, slots, period_state, limits)

    monkeypatch.setattr(tv, "settle_feed_in", spy)
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_tail_hours=24.0), feed_in=feed_in,
        main_max_export_kwh=4.0, main_max_import_kwh=20.0,
    )
    assert verdict.detail["allowance_lo_kwh"] == pytest.approx(36.0)
    assert verdict.detail["allowance_hi_kwh"] == pytest.approx(60.0)
    # The export end (allowance 36) and the import-restored end (allowance 60) are both settled; the
    # second state exists only because the main horizon's imports are reachable.
    assert sorted(seen) == [(50.0, 14.0), (70.0, 10.0)]
    assert verdict.ok is True


def test_a_restored_allowance_still_disables_the_vector_when_the_exhausted_state_is_reachable():
    """A currently net-exporting household can import its way back to a positive allowance.

    Both ends of the reachable allowance interval then matter: the exhausted end refuses the battery
    export leg while the restored end nets the tail at the buy price, so the priced tail is not a
    function of stored energy alone.  The reason is the billing state, never the floor.
    """
    window = make_slots(1, 60, pv=0.0, load=0.5, buy=0.20, sell=0.20)
    feed_in = _saldering_feed_in(5.0, 10.0, discharge=0.0)  # allowance -5: exhausted today
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_tail_hours=24.0), feed_in=feed_in,
        main_max_export_kwh=4.0, main_max_import_kwh=20.0,
    )
    assert verdict.detail["allowance_lo_kwh"] == pytest.approx(-9.0)
    assert verdict.detail["allowance_hi_kwh"] == pytest.approx(15.0)
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BILLING_REACHABLE
    assert verdict.reason != tv.REASON_FLOOR_BINDS
    assert verdict.detail["max_export_main_kwh"] == 4.0
    assert verdict.detail["max_import_main_kwh"] == 20.0


def test_an_unknown_import_bound_disables_the_vector_for_saldering():
    """The allowance interval cannot be bounded from the export side alone: refuse, never guess."""
    window = make_slots(1, 60, pv=0.0, load=0.5, buy=0.20, sell=0.20)
    feed_in = _saldering_feed_in(5.0, 10.0, discharge=0.0)
    verdict = plan_for(
        window, agg=agg_for(), opts=options(terminal_tail_hours=24.0), feed_in=feed_in,
        main_max_export_kwh=4.0,
    )
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_BILLING_REACHABLE
    assert "reachable-import bound is unknown" in verdict.detail["reachability"]


# --------------------------------------------------------------------------------------
# C2: the tail's non-price inputs
# --------------------------------------------------------------------------------------


def test_the_tail_carries_the_window_load_and_pv():
    """``base = load - pv`` decides the house leg, so the tail must repeat both by time-of-day."""
    surplus = make_slots(2, 60, pv=1.5, load=0.2, buy=0.10, sell=0.50)
    opts = options(terminal_mode="vector", sell_floor_eur_per_kwh=0.33, terminal_tail_hours=2.0)
    request_ = request(basis=0.33, rt=1.0, wear=0.0)
    assert plan_for(surplus, agg=agg_for(), opts=opts, req=request_).ok is True
    verdict = plan_for(surplus, agg=agg_for(), opts=opts, req=request_)
    assert [slot.pv_kw for slot in verdict.slots] == [1.5] * 72
    assert [slot.load_kw for slot in verdict.slots] == [0.2] * 72
    # The same prices with a deficit: the house leg fires and the vector is disabled.
    deficit = make_slots(2, 60, pv=0.0, load=1.0, buy=0.10, sell=0.50)
    bound = plan_for(deficit, agg=agg_for(), opts=opts, req=request_)
    assert bound.ok is False
    assert bound.reason == tv.REASON_FLOOR_BINDS
    assert bound.detail["floor_binds"]["leg"] == "house"


# --------------------------------------------------------------------------------------
# C3: numerical stability, not convergence
# --------------------------------------------------------------------------------------


def test_the_extension_increment_is_a_whole_number_of_profile_periods():
    """L, 2L and 3L must end at the same daily phase: the synthetic profile repeats by time of day.

    The increment used to be the *main horizon's* length, so a 30 h horizon put 2L and 3L six and
    twelve hours further into the day than L, and the stability spans measured the profile's daily
    shape instead of the tail's stability.  A request shorter than one period is rounded *up*.
    """
    window = make_slots(30, 60, buy=0.20, sell=0.05)  # a 30 h horizon: not a whole number of days
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts)
    assert verdict.detail["profile_hours"] == 24.0
    assert verdict.periods == 1
    assert verdict.period_slots == 24  # 24 native 60-minute slots per profile day
    assert verdict.tail_hours == 24.0
    assert len(verdict.slots) == 72
    unit = verdict.periods * verdict.period_slots
    ends = [
        verdict.slots[length - 1].t + timedelta(minutes=opts.slot_minutes)
        for length in (unit, 2 * unit, 3 * unit)
    ]
    # The tail starts at 06:00 (a 30 h window from midnight), so every compared length ends at the
    # same time of day -- one whole profile period apart, not one horizon apart.
    assert {end.hour for end in ends} == {6}
    short = plan_for(
        window, agg=agg_for(), opts=options(terminal_mode="vector", terminal_tail_hours=2.0)
    )
    assert (short.periods, short.tail_hours) == (1, 24.0)
    assert short.detail["tail_hours_requested"] == 2.0
    assert len(short.slots) == 72


def test_a_native_slot_that_does_not_divide_the_profile_period_refuses_the_vector():
    """R2: whole-period alignment needs the native slot to divide the 24 h profile period.

    A 50-minute plan slot gives 28.8 slots per day, so no integer number of extensions can end at
    the same daily phase and the stability spans would drift against the profile.  Fail closed.
    """
    window = make_slots(6, 50, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    opts = options(slot_minutes=50, terminal_mode="vector", terminal_tail_hours=24.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts)
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_TAIL_MISALIGNED
    assert verdict.reason not in (tv.REASON_TAIL_TOO_LONG, tv.REASON_TAIL_UNSTABLE)
    assert verdict.detail["profile_slots"] == pytest.approx(28.8)
    assert "does not divide" in verdict.detail["tail_misaligned"]
    assert verdict.slots == () and verdict.minutes == ()
    plan = _solve(window, opts, terminal=verdict)
    assert plan.terminal["reason"] == tv.REASON_TAIL_MISALIGNED
    # A resolution that does divide the period is unaffected (96, 48, 24 and 12 slots per day).
    for minutes in (15, 30, 60, 120):
        ok = plan_for(
            make_slots(6, minutes, pv=0.0, load=0.5, buy=0.20, sell=0.05),
            agg=agg_for(),
            opts=options(slot_minutes=minutes, terminal_mode="vector", terminal_tail_hours=24.0),
        )
        assert ok.reason != tv.REASON_TAIL_MISALIGNED, minutes


def test_the_coarse_tail_restarts_its_buckets_on_the_profile_period():
    """The bucket pattern must repeat with the profile period, not with the main horizon's length.

    A 6 h horizon with a 4-slot tail resolution: the buckets restart every 24 native slots (one
    day), so each period yields the same duration pattern and the increment is a whole profile
    period at the tail resolution too.  A ratio that does not divide the period still restarts per
    period -- the last bucket of each period is short -- so the pattern repeats instead of drifting.
    """
    window = make_slots(6, 60, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    opts = options(terminal_mode="vector", terminal_tail_hours=24.0, terminal_tail_minutes=240.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts)
    assert verdict.detail["tail_ratio"] == 4
    assert (verdict.periods, verdict.period_slots) == (1, 6)  # 24 native slots / ratio 4
    assert len(verdict.slots) == 3 * 6 == 18
    assert list(verdict.minutes) == [240.0] * 18
    assert sum(verdict.minutes) == 72 * 60.0  # the native 3L span, exactly
    assert verdict.detail["native_tail_slots"] == 4 * len(verdict.slots)

    fifteen = make_slots(96, 15, pv=0.0, load=0.5, buy=0.20, sell=0.05)  # one whole day
    coarse = plan_for(
        fifteen,
        agg=agg_for(),
        opts=options(
            slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0,
            terminal_tail_minutes=75.0,
        ),
    )
    assert coarse.detail["tail_ratio"] == 5
    assert (coarse.periods, coarse.period_slots) == (1, 20)  # 96 native slots / ratio 5, rounded up
    assert len(coarse.slots) == 3 * 20 == 60
    assert list(coarse.minutes[:20]) == [75.0] * 19 + [15.0]
    assert coarse.minutes[20:40] == coarse.minutes[:20]  # the duration pattern repeats per period
    assert sum(coarse.minutes) == 3 * 24 * 60.0


def test_the_stability_tolerance_is_the_per_kwh_bound_over_the_state_span():
    agg = agg_for()
    expected = tv.TERMINAL_STABILITY_EUR_PER_KWH * (agg.e_max_kwh - agg.e_min_kwh)
    assert tv.stability_tolerance_eur(agg) == pytest.approx(expected)
    window = make_slots(3, 60, buy=0.20, sell=0.05)
    opts = options(terminal_mode="vector", terminal_tail_hours=3.0)
    plan = _solve(window, opts, agg=agg, terminal=plan_for(window, agg=agg, opts=opts))
    assert plan.terminal["stability_tolerance_eur"] == pytest.approx(expected)


def test_stability_needs_more_than_one_extension(monkeypatch):
    """Agreement at L vs 2L is not enough: the 2L vs 3L extension must agree too."""
    window = make_slots(3, 60, buy=0.20, sell=0.05)
    opts = options(terminal_mode="vector", terminal_tail_hours=3.0)
    verdict = plan_for(window, agg=agg_for(), opts=opts)
    assert verdict.ok is True
    real = tv.continuation_row
    calls = {"n": 0}

    def fake(grid, slots, minutes, agg, options_, salvage):
        calls["n"] += 1
        row = real(grid, slots, minutes, agg, options_, salvage)
        if calls["n"] == 3:  # the 3L row drifts
            row = [value - 0.01 * index for index, value in enumerate(row)]
        return row

    monkeypatch.setattr(tv, "continuation_row", fake)
    plan = _solve(window, opts, terminal=verdict)
    assert calls["n"] == 3
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_TAIL_UNSTABLE
    assert len(plan.terminal["stability_spans_eur"]) == 2


# --------------------------------------------------------------------------------------
# C4: the whole-solve budget
# --------------------------------------------------------------------------------------


def test_the_whole_solve_budget_covers_the_main_sweep_and_every_tail():
    """The guard is not just the tail: main + L + 2L + 3L must fit the iteration budget.

    At the plan's *own* resolution (``terminal_tail_minutes`` == the 15-minute window) a 24 h tail
    on a 10 kWh pack is budget-disabled: the tail is never coarsened on its own, so the mode fails
    closed instead of silently changing the main plan's discretisation.  The tail resolution is an
    explicit opt-in -- see the next test for the same shape at 60-minute tail slots.
    """
    window = make_slots(8, 15, pv=0.0, load=0.5, buy=0.2, sell=0.05)
    agg = agg_for([battery(capacity_kwh=10.0, min_soc_pct=5.0)])
    opts = options(slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0,
                   terminal_tail_minutes=15.0, power_steps=24)
    verdict = plan_for(window, agg=agg, opts=opts)
    assert verdict.ok is True
    assert verdict.detail["tail_ratio"] == 1
    states = len(dp._main_grid(agg, opts, len(window), dp._anchors(1.2, agg, None)).points)
    actions = max(2, int(opts.power_steps) + 1) + 1  # worst case: a target's exact-power action
    unit = verdict.periods * verdict.period_slots
    plan = _solve(window, opts, agg=agg, terminal=verdict)
    assert plan.terminal["reason"] == tv.REASON_TAIL_TOO_LONG
    detail = plan.terminal["detail"]
    # Every sweep is run: the main one, then L, 2L and 3L.
    assert detail["solve_iterations"] == (len(window) + 6 * unit) * states * actions
    assert detail["iteration_budget"] == dp.MAX_ITERATIONS
    assert detail["solve_iterations"] > dp.MAX_ITERATIONS


def test_the_tail_resolution_brings_the_live_like_shape_under_the_budget():
    """120 native 15-minute slots with a 24 h tail at 60-minute tail slots: the budget fits.

    Four native slots fold into one tail slot, so the whole-solve guard passes, and the refusal
    this shape gets is the *floor*: ``floor_binds`` (eligibility), not ``tail_too_long`` (budget).
    The two must stay distinguishable, which is why the budget is reported even on a refused plan.
    """
    window = make_slots(120, 15, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    agg = agg_for([battery(capacity_kwh=10.0, min_soc_pct=5.0)])
    opts = options(slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0,
                   terminal_tail_minutes=60.0, power_steps=24, sell_floor_eur_per_kwh=0.3767)
    verdict = plan_for(window, agg=agg, opts=opts, req=request())
    assert verdict.ok is False
    assert verdict.reason == tv.REASON_FLOOR_BINDS
    assert verdict.detail["tail_ratio"] == 4
    assert verdict.detail["native_tail_slots"] == 4 * len(verdict.slots)
    states = len(dp._main_grid(agg, opts, len(window), dp._anchors(1.2, agg, None)).points)
    actions = max(2, int(opts.power_steps) + 1) + 1
    unit = verdict.periods * verdict.period_slots
    plan = _solve(window, opts, agg=agg, terminal=verdict)
    assert plan.terminal["ok"] is False
    assert plan.terminal["reason"] == tv.REASON_FLOOR_BINDS
    assert plan.terminal["reason"] != tv.REASON_TAIL_TOO_LONG
    detail = plan.terminal["detail"]
    assert detail["solve_iterations"] == (len(window) + 6 * unit) * states * actions
    assert detail["iteration_budget"] == dp.MAX_ITERATIONS
    assert detail["solve_iterations"] < dp.MAX_ITERATIONS


def test_the_budget_reserves_the_second_main_sweep_when_a_target_is_configured():
    """An unreachable terminal target retries the whole main sweep, so the guard must reserve it.

    The same shape fits the budget without a target and does not fit with one: the retry is not
    optional once a target is set, so "fits" has to mean "fits with the retry".
    """
    window = make_slots(120, 15, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    agg = agg_for([battery(capacity_kwh=10.0, min_soc_pct=5.0)])
    opts = options(
        slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0,
        terminal_tail_minutes=60.0, power_steps=24, soc_step_kwh=0.04,
    )
    verdict = plan_for(window, agg=agg, opts=opts)
    assert verdict.ok is True
    unit = verdict.periods * verdict.period_slots
    actions = max(2, int(opts.power_steps) + 1) + 1  # worst case: a target's exact-power action
    without_states = len(dp._main_grid(agg, opts, len(window), dp._anchors(1.2, agg, None)).points)
    assert (len(window) + 6 * unit) * without_states * actions < dp.MAX_ITERATIONS
    target = options(
        slot_minutes=15, terminal_mode="vector", terminal_tail_hours=24.0,
        terminal_tail_minutes=60.0, power_steps=24, soc_step_kwh=0.04, target_soc_pct_end=90.0,
    )
    e_target = agg.capacity_kwh * 0.9
    states = len(dp._main_grid(agg, target, len(window), dp._anchors(1.2, agg, e_target)).points)
    assert (2 * len(window) + 6 * unit) * states * actions > dp.MAX_ITERATIONS
    plan = _solve(window, target, agg=agg, terminal=verdict)
    assert plan.terminal["reason"] == tv.REASON_TAIL_TOO_LONG
    detail = plan.terminal["detail"]
    assert detail["solve_iterations"] == (2 * len(window) + 6 * unit) * states * actions
    assert detail["iteration_budget"] == dp.MAX_ITERATIONS


# --------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def solve(self, *args, **kwargs):
        self.calls.append(args)
        return None


def test_a_terminal_row_never_reaches_the_c_core(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(_fast, "_core", recorder)
    monkeypatch.setattr(_fast, "_disabled", False)
    monkeypatch.setattr(_fast, "_checks_done", 0)
    monkeypatch.setattr(_fast, "_solve_count", 0)
    agg = agg_for()
    opts = options()
    window = make_slots(4, 60)
    minutes = [60.0] * 4
    start = 1.2

    # Control: without a row the extension is reached (so the recorder proves something).  The
    # stub returns ``None``, which validation rejects and disables the core.
    assert _fast.solve(window, agg, start, opts, dp.TARGET_HARD_PENALTY, None, 0.1, minutes) is None
    assert len(recorder.calls) == 1

    monkeypatch.setattr(_fast, "_disabled", False)
    assert (
        _fast.solve(
            window,
            agg,
            start,
            opts,
            dp.TARGET_HARD_PENALTY,
            None,
            0.1,
            minutes,
            terminal_row=[0.0] * 4,
        )
        is None
    )
    assert len(recorder.calls) == 1, "a terminal row must never be handed to the C core"


def test_the_reference_produces_the_plan_when_a_row_is_supplied(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(_fast, "_core", recorder)
    monkeypatch.setattr(_fast, "_disabled", False)
    monkeypatch.setattr(_fast, "_checks_done", 0)
    monkeypatch.setattr(_fast, "_solve_count", 0)
    agg = agg_for()
    opts = options(terminal_mode="vector", terminal_tail_hours=6.0)
    # A flat window keeps the 24 h tail numerically stable (the vector has to be eligible before a
    # row can be supplied at all); this test is about the row's mechanics, not the tail's shape.
    window = make_slots(6, 60, pv=0.0, load=0.5, buy=0.20, sell=0.05)
    verdict = plan_for(window, agg=agg, opts=opts)
    assert verdict.ok is True
    plan = _solve(window, opts, agg=agg, terminal=verdict)
    assert recorder.calls == []
    assert plan.terminal["ok"] is True

    grid = dp._main_grid(agg, opts, len(window), dp._anchors(1.2, agg, None)).points
    row = tv.continuation_row(
        grid, verdict.slots[: verdict.periods * verdict.period_slots],
        verdict.minutes[: verdict.periods * verdict.period_slots],
        agg, opts, verdict.salvage_eur_per_kwh,
    )
    reference = dp._solve_once_numeric(
        window, agg, 1.2, opts, dp.TARGET_HARD_PENALTY, None, verdict.salvage_eur_per_kwh,
        [60.0] * 6, terminal_row=row,
    )
    assert [slot.power_w / 1000.0 for slot in plan.slots] == reference.powers
    assert plan.summary.objective_eur == pytest.approx(reference.objective_eur, abs=1e-9)
