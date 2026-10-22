"""Plan variants (0.35.0, SPEC 5): several candidate plans, scored alike, best one kept.

The question these pin: a discharge below the pack's cost basis is only a gain when the horizon can
put that energy back more cheaply, and the *reported* money must not reward a plan for emptying the
pack. Every candidate is scored on one metric -- the cash it saves, minus the wear it spends, plus the
stored energy it ends with marked at the refill cost -- so a plan that dumps energy has to earn that
energy back in cash before it can win.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_manager"))

from app.api import plan_dict, plan_summary_dict  # noqa: E402
from app.models import (  # noqa: E402
    BatteryConfig,
    LoadForecast,
    LoadSlot,
    PriceForecast,
    PriceSlot,
    SolarForecast,
    SolarSlot,
)
from app.optimize import dp, variants  # noqa: E402

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
SLOTS = 48  # 12 h at the 15-minute slot


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _settings(**overrides):
    values = {
        "optimizer.slot_minutes": 15,
        "optimizer.horizon_hours": 12,
        "optimizer.reserve_soc_pct": 0.0,
        "optimizer.allow_export": False,
    }
    values.update(overrides)
    return _Settings(values)


def _battery(capacity_kwh: float = 10.0, **overrides) -> list[BatteryConfig]:
    values = {
        "capacity_kwh": capacity_kwh,
        "max_charge_w": 3000.0,
        "max_discharge_w": 3000.0,
        "eta_charge": 0.95,
        "eta_discharge": 0.95,
        "min_soc_pct": 10.0,
        "max_soc_pct": 100.0,
    }
    values.update(overrides)
    return [
        BatteryConfig(
            id="pack",
            name="Pack",
            driver="generic",
            enabled=True,
            mapping={"power_setpoint_entity": "number.p", "soc_entity": "sensor.s"},
            **values,
        )
    ]


def _price(profile) -> PriceForecast:
    forecast = PriceForecast(source="zonneplan_entity")
    for index in range(SLOTS):
        price = profile(index)
        forecast.slots.append(
            PriceSlot(t=NOW + timedelta(minutes=15 * index), buy=price, sell=price)
        )
    return forecast


def _solar() -> SolarForecast:
    forecast = SolarForecast(source="none")
    for index in range(SLOTS):
        forecast.slots.append(
            SolarSlot(t=NOW + timedelta(minutes=15 * index), pv_kw=0.0)
        )
    return forecast


def _load(profile=lambda index: 0.3) -> LoadForecast:
    forecast = LoadForecast(source="profile")
    for index in range(SLOTS):
        load = profile(index)
        forecast.slots.append(
            LoadSlot(
                t=NOW + timedelta(minutes=15 * index),
                load_kw=load,
                p10_kw=load * 0.8,
                p90_kw=load * 1.2,
            )
        )
    return forecast


def _plan(settings, *, start_kwh=5.0, floor=0.40, price=None, load=None, battery=None):
    return dp.solve_from_forecasts(
        price or _price(lambda index: 0.31),
        _solar(),
        load or _load(lambda index: 2.0 if 32 <= index < 40 else 0.3),
        battery_configs=battery or _battery(),
        start_kwh=start_kwh,
        settings=settings,
        now=NOW,
        sell_floor_eur_per_kwh=floor,
    )


def _arbitrage_prices(index: int) -> float:
    """Two cheap hours, then a peak the pack can serve, then mid-priced hours."""
    if 4 <= index < 12:
        return 0.22  # cheap: worth charging
    if 32 <= index < 40:
        return 0.32  # peak: worth discharging, but below the 0.40 basis
    return 0.31


def test_candidates_need_a_ledger_floor():
    """No cost basis means no policy to vary: one candidate, and the sweep is a no-op."""
    assert len(variants.candidates(None)) == 1
    assert variants.candidates(None)[0].id == "refill_floor"
    ids = [candidate.id for candidate in variants.candidates(0.40)]
    assert ids == ["refill_floor", "ledger_floor", "hold_pack"]


def test_the_sweep_takes_the_refill_aware_floor_when_the_cycle_pays():
    """Dearest case: a 0.40 basis would freeze the pack (the peak is 0.32), but the 0.22 refill
    inside the horizon pays for the round trip, so the below-basis cycle is the profitable plan."""
    plan = _plan(_settings(), price=_price(_arbitrage_prices))
    block = plan.variants
    assert block["chosen"] == "refill_floor"
    assert block["margin_eur"] > 0.0
    rows = {row["id"]: row for row in block["candidates"]}
    # The winner sells held energy below the ledger basis -- and still nets the most.
    assert rows["refill_floor"]["sold_below_basis_kwh"] > 0.0
    assert rows["refill_floor"]["net_eur"] > rows["ledger_floor"]["net_eur"]
    assert rows["ledger_floor"]["sold_below_basis_kwh"] == pytest.approx(0.0)
    assert rows["refill_floor"]["sell_floor_eur_per_kwh"] < 0.40
    assert rows["ledger_floor"]["sell_floor_eur_per_kwh"] == pytest.approx(0.40)
    # The chosen plan's slots really carry the cheaper floor, and it discharges at the peak.
    assert plan.summary.sell_floor_eur_per_kwh == pytest.approx(
        rows["refill_floor"]["sell_floor_eur_per_kwh"]
    )
    peak = [slot for slot in plan.slots if 32 <= (slot.t - NOW).total_seconds() // 900 < 40]
    assert any(slot.power_w < 0 for slot in peak), "the winning plan must use the peak"


def test_an_unprofitable_spread_is_never_sold_by_any_candidate():
    """0.30 -> 0.32 does not pay the round trip, so the refill floor refuses it too and every
    candidate agrees: nothing is discharged, and the sweep reports a tie rather than inventing a
    winner."""

    def prices(index: int) -> float:
        if 4 <= index < 12:
            return 0.30
        if 32 <= index < 40:
            return 0.32
        return 0.31

    plan = _plan(_settings(), price=_price(prices))
    block = plan.variants
    assert block["chosen"] == "refill_floor"
    assert block["tie"] is True and block["margin_eur"] == pytest.approx(0.0)
    for row in block["candidates"]:
        assert row["sold_below_basis_kwh"] == pytest.approx(0.0)
        assert row["discharged_kwh"] == pytest.approx(0.0)


def test_charge_only_is_offered_and_scored():
    plan = _plan(_settings(), price=_price(_arbitrage_prices))
    rows = {row["id"]: row for row in plan.variants["candidates"]}
    assert "hold_pack" in rows
    assert rows["hold_pack"]["discharged_kwh"] == pytest.approx(0.0)
    assert rows["hold_pack"]["rule"].startswith("charge cheap energy")
    assert rows["hold_pack"]["net_eur"] is not None


def test_doing_nothing_scores_zero_and_the_winner_beats_it():
    """The metric's reference point: a plan that changes nothing is worth exactly nothing."""
    plan = _plan(_settings(), price=_price(lambda index: 0.31))
    block = plan.variants
    assert block["chosen"] == "refill_floor"
    assert block["net_eur"] >= 0.0
    assert plan.summary.net_eur == pytest.approx(block["net_eur"])


def test_the_score_marks_the_energy_a_plan_keeps():
    """Same cash, different ending pack: the fuller one is worth more, which is what makes a dump
    have to earn its energy back instead of looking free."""
    plan = _plan(_settings(), price=_price(_arbitrage_prices))
    replacement = plan.variants["replacement_eur_per_kwh"]
    assert replacement == pytest.approx(0.22 / 0.95, rel=1e-6)
    figures = variants.score_plan(
        plan, start_kwh=5.0, replacement_eur_per_kwh=replacement, ledger_floor_eur_per_kwh=0.40
    )
    assert figures["stored_value_eur"] == pytest.approx(
        figures["energy_delta_kwh"] * replacement
    )
    # A plan that ends 1 kWh higher is worth exactly one refilled kWh more.
    class _Slots(list):
        pass

    class _Summary:
        saved_eur = 1.0
        cost_estimate_eur = 0.0
        baseline_cost_eur = 1.0
        charged_kwh = 0.0
        discharged_kwh = 0.0
        cycle_cost_eur = 0.0

    class _Plan:
        slots = [
            type("S", (), {"soc_kwh": 6.0, "power_w": 0.0, "minutes": 15.0, "buy": 0.3, "sell": 0.3})()
        ]
        summary = _Summary()

    higher = variants.score_plan(
        _Plan(), start_kwh=5.0, replacement_eur_per_kwh=replacement, ledger_floor_eur_per_kwh=None
    )
    assert higher["net_eur"] == pytest.approx(1.0 + replacement)


def test_wear_is_charged_to_the_score():
    """Cycling is not free: the plan's own wear term comes off before the comparison."""
    plan = _plan(_settings(optimizer_cycle_cost_eur_per_kwh=0.0), price=_price(_arbitrage_prices))
    figures = variants.score_plan(
        plan,
        start_kwh=5.0,
        replacement_eur_per_kwh=plan.variants["replacement_eur_per_kwh"],
        ledger_floor_eur_per_kwh=0.40,
    )
    assert figures["wear_eur"] == pytest.approx(plan.summary.cycle_cost_eur)
    assert figures["net_eur"] == pytest.approx(
        plan.summary.saved_eur - figures["wear_eur"] + figures["stored_value_eur"]
    )


def test_off_keeps_the_single_solve():
    settings = _settings(**{"optimizer.plan_variants": "off"})
    price = _price(_arbitrage_prices)
    load = _load(lambda index: 2.0 if 32 <= index < 40 else 0.3)
    plan = _plan(settings, price=price, load=load)
    assert not hasattr(plan, "variants")
    assert getattr(plan.summary, "net_eur", None) is None
    # Bit-for-bit the same schedule as the old single solve with the capped floor.
    aggregate = dp.BatteryAggregate.from_configs(_battery(), 0.0)
    start = dp.quarter_floor(NOW, 15)
    slots = dp.align_slots(price, _solar(), load, start, start + timedelta(hours=12), 15)
    options = dp.OptimizerOptions.from_settings(settings)
    reference = dp.solve(
        slots,
        aggregate=aggregate,
        start_kwh=5.0,
        options=options,
        generated_at=NOW,
        sell_floor_eur_per_kwh=0.40,
        now=NOW,
    )
    assert [slot.power_w for slot in plan.slots] == [slot.power_w for slot in reference.slots]
    assert plan.summary.saved_eur == pytest.approx(reference.summary.saved_eur)


def test_the_comparison_and_its_figures_reach_the_api():
    plan = _plan(_settings(), price=_price(_arbitrage_prices))
    payload = plan_dict(plan, NOW)
    assert payload["variants"]["chosen"] == "refill_floor"
    assert len(payload["variants"]["candidates"]) == 3
    summary = plan_summary_dict(plan.summary)
    assert summary["plan_variant"] == "refill_floor"
    assert summary["net_eur"] == pytest.approx(plan.variants["net_eur"])
    assert summary["replacement_eur_per_kwh"] == pytest.approx(
        plan.variants["replacement_eur_per_kwh"]
    )
    assert summary["plan_variant_margin_eur"] == pytest.approx(plan.variants["margin_eur"])


def test_an_infeasible_candidate_is_never_chosen():
    """A candidate the solver reports infeasible is scored but cannot win (SPEC 8.1)."""

    class _Summary:
        saved_eur = 100.0
        cost_estimate_eur = 0.0
        baseline_cost_eur = 100.0
        charged_kwh = 0.0
        discharged_kwh = 0.0
        cycle_cost_eur = 0.0

    class _Plan:
        slots = []
        summary = _Summary()

        def __init__(self, feasible):
            self.feasible = feasible
            self.infeasible_reason = None if feasible else "grid_limits_infeasible"

    good = _Plan(True)
    good.summary = type("S", (_Summary,), {"saved_eur": 0.5})()
    bad = _Plan(False)
    scored = [
        (variants.candidates(0.4)[0], bad, variants.score_plan(
            bad, start_kwh=0.0, replacement_eur_per_kwh=0.2, ledger_floor_eur_per_kwh=0.4
        )),
        (variants.candidates(0.4)[1], good, variants.score_plan(
            good, start_kwh=0.0, replacement_eur_per_kwh=0.2, ledger_floor_eur_per_kwh=0.4
        )),
    ]
    chosen, plan, _figures, block = variants.choose(scored)
    assert chosen.id == "ledger_floor" and plan is good
    assert block["chosen"] == "ledger_floor"
    assert {row["id"]: row["feasible"] for row in block["candidates"]} == {
        "refill_floor": False,
        "ledger_floor": True,
    }


def test_the_action_sensor_says_which_candidate_won():
    """The live action sensor names the winning variant and its edge (0.35.0, SPEC 5/12)."""
    from app.engine import Engine

    class _Summary:
        plan_variant = "refill_floor"
        net_eur = 4
        plan_variant_margin_eur = 2
        replacement_eur_per_kwh = 5

    plan = type("P", (), {})()
    plan.summary = _Summary()
    plan.variants = {"chosen": "refill_floor", "tie": False}
    attributes = Engine._plan_variant_attributes(plan)
    assert attributes == {
        "plan_variant": "refill_floor",
        "plan_variant_net_eur": 4.0,
        "plan_variant_margin_eur": 2.0,
        "replacement_eur_per_kwh": 5.0,
        "plan_variant_tie": False,
    }
    # A plan restored from the store keeps its slots but not the comparison: say nothing.
    restored = type("P", (), {"summary": object(), "variants": None})()
    assert Engine._plan_variant_attributes(restored) == {}
