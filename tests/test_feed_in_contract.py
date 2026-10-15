"""Optimizer v2 stage 1b: contract-correct feed-in accounting (SPEC.md 4.2, docs/OPTIMIZER_V2.md 2).

Two layers are tested:

* the pure module :mod:`app.sources.feed_in` — settlement arithmetic, the reachability bound and
  exact-or-refuse, against independently written contract arithmetic, including the refusal paths
  for malformed/unknown inputs;
* the live forecast path in :mod:`app.sources.price` — stays on the legacy ``derived`` resolution
  (non-derived modes are config-only until a consumer enforces the complete module result), with
  exact parity where the module's supported modes share the legacy arithmetic.

No network, no HA instance: every input is a plain price slot, period state or reachability limit.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import DEFAULTS, SETTINGS_META, Settings  # noqa: E402
from app.models import ConfigError, PriceForecast, PriceSlot, Reason  # noqa: E402
from app.sources.feed_in import (  # noqa: E402
    MODES,
    FeedInLimits,
    PeriodState,
    max_reachable_export_kwh,
    settle_feed_in,
    settle_saldering_schedule,
)
from app.sources.price import _apply_feed_in  # noqa: E402

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def settings(**options):
    return Settings(None, options=options)


def slots(count, buy=0.30, energy_tax=0.0):
    return [
        PriceSlot(t=T0 + timedelta(minutes=15 * index), buy=buy, sell=0.0, energy_tax=energy_tax)
        for index in range(count)
    ]


class BareSlot:
    """A duck-typed slot carrying only the attributes given (missing ones stay missing)."""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)


# --------------------------------------------------------------------------------------------
# Settings: defaults preserve today's behaviour, and derived stays on the live path
# --------------------------------------------------------------------------------------------


def test_new_settings_default_to_the_pre_stage_1b_behaviour():
    assert DEFAULTS["price.feed_in_mode"] == "derived"
    assert DEFAULTS["price.energy_tax_confirmed"] is False
    assert DEFAULTS["price.feed_in_tax_deduction_eur_per_kwh"] is None
    assert DEFAULTS["price.saldering_assume_nettable"] is False
    for key in (
        "price.feed_in_mode",
        "price.energy_tax_confirmed",
        "price.feed_in_tax_deduction_eur_per_kwh",
        "price.saldering_assume_nettable",
    ):
        assert key in SETTINGS_META, key
        assert key in DEFAULTS, key


def test_feed_in_mode_is_validated_as_a_choice():
    configured = settings()
    assert configured.set("price.feed_in_mode", "saldering") == "saldering"
    with pytest.raises(ConfigError):
        configured.set("price.feed_in_mode", "nonsense")
    with pytest.raises(ConfigError):
        configured.set("price.feed_in_tax_deduction_eur_per_kwh", -0.5)


def test_module_excludes_derived_and_unknown_modes():
    assert "derived" not in MODES
    with pytest.raises(ValueError):
        settle_feed_in(settings(), slots(1))  # default mode is derived
    with pytest.raises(ValueError):
        settle_feed_in(settings(**{"price.feed_in_mode": "nonsense"}), slots(1))


# --------------------------------------------------------------------------------------------
# Reachability bound
# --------------------------------------------------------------------------------------------


def test_max_reachable_export_includes_pv_surplus_plus_discharge_and_grid_recharge():
    # 2 slots, 0.25 h each: PV surplus 2 kW plus 5 kW discharge capability = 7 kW per slot.
    # A pack-energy bound is unsound: 7 kW * 0.25 h = 1.75 kWh per slot, 3.5 kWh over the horizon,
    # which already exceeds a 1 kWh pack because grid charging can feed a later discharge.
    limits = FeedInLimits(
        pv_kw=(3.0, 3.0),
        load_kw=(1.0, 1.0),
        max_discharge_kw=5.0,
        export_limit_kw=8.0,
        slot_hours=0.25,
    )
    assert max_reachable_export_kwh(2, limits) == pytest.approx(3.5)


def test_max_reachable_export_applies_the_grid_export_limit_per_slot():
    limits = FeedInLimits(pv_kw=(3.0,), load_kw=(1.0,), max_discharge_kw=5.0, export_limit_kw=4.0,
                          slot_hours=0.25)
    assert max_reachable_export_kwh(1, limits) == pytest.approx(1.0)  # min(4, 7) * 0.25


def test_unknown_discharge_capability_is_unbounded_not_zero():
    assert max_reachable_export_kwh(1, None) is None
    assert max_reachable_export_kwh(
        1, FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=None)
    ) is None
    # A PV/load series that does not cover every slot is unknown, not zero.
    assert max_reachable_export_kwh(
        2, FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0)
    ) is None


@pytest.mark.parametrize(
    "limits",
    [
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=math.inf),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=math.nan),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=-1.0),
        FeedInLimits(pv_kw=(math.inf,), load_kw=(0.0,), max_discharge_kw=1.0),
        FeedInLimits(pv_kw=(0.0,), load_kw=("x",), max_discharge_kw=1.0),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=math.inf),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.0),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, export_limit_kw=math.inf),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, export_limit_kw=-1.0),
    ],
)
def test_non_finite_or_malformed_reachability_inputs_are_unbounded(limits):
    # An infinite capability must never become zero capability.
    assert max_reachable_export_kwh(1, limits) is None


@pytest.mark.parametrize(
    "pv_kw,load_kw",
    [
        ((-1.0,), (0.0,)),  # negative PV sample
        ((0.0,), (-1.0,)),  # negative load sample
        ((-1.0,), (-1.0,)),
    ],
)
def test_negative_pv_or_load_samples_are_unbounded(pv_kw, load_kw):
    # A negative sample is malformed, not zero surplus: accepting it would suppress surplus and
    # understate the reachable export. Independent of the discharge/export-limit validation.
    limits = FeedInLimits(pv_kw=pv_kw, load_kw=load_kw, max_discharge_kw=5.0, slot_hours=0.25)
    assert max_reachable_export_kwh(1, limits) is None


def test_non_finite_capability_keeps_a_threshold_contract_from_being_declared_exact():
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=math.inf, slot_hours=0.25)
    result = settle_feed_in(
        configured, slots(1, buy=0.30), period_state=PeriodState(1000.0, 0.0), limits=limits
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor == pytest.approx(0.30)


# --------------------------------------------------------------------------------------------
# Explicit and buy_minus_fee (energy-only, exact, strict about unknown inputs)
# --------------------------------------------------------------------------------------------


def test_explicit_contract_uses_the_flat_tariff_net_of_fee():
    configured = settings(
        **{
            "price.feed_in_mode": "explicit",
            "price.feed_in_eur_per_kwh": 0.05,
            "price.export_fee_eur_per_kwh": 0.005,
            "price.feed_in_tax_inclusive": True,
        }
    )
    result = settle_feed_in(configured, slots(2, buy=0.99))
    assert result.export_allowed is True and result.reason is None
    assert result.sells == pytest.approx([0.045, 0.045])
    assert result.floor == pytest.approx(0.045)


def test_explicit_contract_adds_vat_when_the_tariff_is_exclusive():
    configured = settings(
        **{
            "price.feed_in_mode": "explicit",
            "price.feed_in_eur_per_kwh": 0.05,
            "price.feed_in_tax_inclusive": False,
            "price.vat_pct": 21.0,
        }
    )
    result = settle_feed_in(configured, slots(1))
    assert result.sells[0] == pytest.approx(0.05 * 1.21)


def test_explicit_contract_without_a_tariff_refuses():
    result = settle_feed_in(settings(**{"price.feed_in_mode": "explicit"}), slots(2))
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor is None
    assert result.sells == pytest.approx([0.0, 0.0])


@pytest.mark.parametrize("flat", [math.inf, math.nan, "x", True])
def test_explicit_contract_with_a_malformed_flat_tariff_refuses(flat):
    configured = settings(**{"price.feed_in_mode": "explicit", "price.feed_in_eur_per_kwh": flat})
    result = settle_feed_in(configured, slots(1))
    assert result.export_allowed is False
    assert result.floor is None


def test_explicit_contract_with_a_malformed_resolved_price_refuses_without_falling_back():
    configured = settings(
        **{"price.feed_in_mode": "explicit", "price.feed_in_eur_per_kwh": 0.05}
    )
    result = settle_feed_in(
        configured, slots(1), limits=FeedInLimits(explicit_price_eur_per_kwh=math.nan)
    )
    assert result.export_allowed is False and result.floor is None


def test_unresolved_authoritative_entity_does_not_fall_back_to_the_flat_tariff():
    configured = settings(
        **{
            "price.feed_in_mode": "explicit",
            "price.feed_in_entity": "sensor.example_feed_in",
            "price.feed_in_eur_per_kwh": 0.05,  # must not stand in for the failed entity
        }
    )
    result = settle_feed_in(configured, slots(1))
    assert result.export_allowed is False and result.floor is None
    # A caller that resolved the entity supplies the finite rate and is priced exactly.
    resolved = settle_feed_in(
        configured, slots(1), limits=FeedInLimits(explicit_price_eur_per_kwh=0.06)
    )
    assert resolved.export_allowed is True and resolved.sells == pytest.approx([0.06])


def test_buy_minus_fee_confirmed_zero_tax_prices_buy_minus_fee():
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
            "price.feed_in_tax_deduction_eur_per_kwh": 0.0,
            "price.export_fee_eur_per_kwh": 0.01,
        }
    )
    result = settle_feed_in(configured, slots(2, buy=0.40, energy_tax=0.11))
    assert result.export_allowed is True and result.reason is None
    assert result.sells == pytest.approx([0.39, 0.39])


def test_buy_minus_fee_confirmed_non_zero_deduction_prices_buy_minus_tax():
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
            "price.feed_in_tax_deduction_eur_per_kwh": 0.11,
            "price.export_fee_eur_per_kwh": 0.01,
        }
    )
    result = settle_feed_in(configured, slots(2, buy=0.40, energy_tax=0.13))
    assert result.sells == pytest.approx([0.28, 0.28])


def test_buy_minus_fee_confirmed_uses_the_slot_component_when_no_deduction_is_stated():
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
            "price.export_fee_eur_per_kwh": 0.005,
        }
    )
    result = settle_feed_in(configured, slots(1, buy=0.40, energy_tax=0.11))
    assert result.sells == pytest.approx([0.285])


def test_buy_minus_fee_unconfirmed_tax_refuses_the_battery_export_leg():
    configured = settings(
        **{"price.feed_in_mode": "buy_minus_fee", "price.feed_in_energy_tax_refund": False}
    )
    result = settle_feed_in(configured, slots(2, buy=0.40, energy_tax=0.11))
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor is None  # no justified bound: sell = buy would be a guess


def test_buy_minus_fee_with_the_tax_refunded_deducts_nothing():
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "buy_minus_fee"}),
        slots(2, buy=0.40, energy_tax=0.11),
    )
    assert result.export_allowed is True
    assert result.sells == pytest.approx([0.40, 0.40])


@pytest.mark.parametrize("energy_tax", [None, math.inf, math.nan, -0.01, "x"])
def test_confirmed_but_missing_or_malformed_slot_tax_refuses(energy_tax):
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
        }
    )
    slot = BareSlot(buy=0.40)
    if energy_tax is not None:
        slot.energy_tax = energy_tax
    result = settle_feed_in(configured, [slot])
    assert result.export_allowed is False and result.floor is None


def test_confirmed_zero_slot_tax_is_a_valid_zero_not_a_refusal():
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
        }
    )
    result = settle_feed_in(configured, slots(1, buy=0.40, energy_tax=0.0))
    assert result.export_allowed is True
    assert result.sells == pytest.approx([0.40])


@pytest.mark.parametrize("deduction", [math.inf, math.nan, -0.01, "x"])
def test_confirmed_but_malformed_stated_deduction_refuses(deduction):
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
            "price.feed_in_tax_deduction_eur_per_kwh": deduction,
        }
    )
    result = settle_feed_in(configured, slots(1, buy=0.40, energy_tax=0.0))
    assert result.export_allowed is False and result.floor is None


def test_malformed_export_fee_refuses():
    configured = settings(
        **{"price.feed_in_mode": "buy_minus_fee", "price.export_fee_eur_per_kwh": math.inf}
    )
    result = settle_feed_in(configured, slots(1, buy=0.40))
    assert result.export_allowed is False and result.floor is None


@pytest.mark.parametrize("buy", [None, math.inf, math.nan, "x"])
def test_missing_or_malformed_buy_price_refuses(buy):
    slot = BareSlot(energy_tax=0.0)
    if buy is not None:
        slot.buy = buy
    for mode in ("buy_minus_fee", "saldering"):
        result = settle_feed_in(
            settings(**{"price.feed_in_mode": mode}),
            [slot],
            period_state=PeriodState(10.0, 0.0),
            limits=FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0),
        )
        assert result.export_allowed is False and result.floor is None


# --------------------------------------------------------------------------------------------
# Tiered
# --------------------------------------------------------------------------------------------


def test_tiered_is_exact_when_the_reachable_volume_stays_inside_the_current_tier():
    limits = FeedInLimits(
        pv_kw=(0.0,),
        load_kw=(0.0,),
        max_discharge_kw=1.0,
        slot_hours=0.25,
        tiers=((10.0, 0.10), (float("inf"), 0.05)),
        tier_volume_kwh=2.0,
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1, buy=0.40), limits=limits
    )
    assert result.export_allowed is True and result.reason is None
    assert result.sells == pytest.approx([0.10])
    assert result.floor == pytest.approx(0.05)  # min over every tier rate, not the current one


def test_tiered_floor_uses_the_lowest_tier_rate_below_the_single_formula_value():
    # The next boundary is reachable (10 kWh capability vs a 1 kWh boundary), so the battery export
    # is refused and forced PV export is priced at the lowest reachable rate, 0.02 - well below the
    # buy price a single-formula sale would have used.
    limits = FeedInLimits(
        pv_kw=(0.0,),
        load_kw=(0.0,),
        max_discharge_kw=40.0,
        slot_hours=0.25,
        tiers=((1.0, 0.02), (float("inf"), 0.30)),
        tier_volume_kwh=0.0,
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1, buy=0.30), limits=limits
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor == pytest.approx(0.02)
    assert result.sells == pytest.approx([0.02])


def test_tiered_without_a_tier_table_refuses():
    result = settle_feed_in(settings(**{"price.feed_in_mode": "tiered"}), slots(2))
    assert result.export_allowed is False and result.reason == Reason.UNPRICED_EXPORT
    assert result.floor is None


def test_tiered_without_a_starting_volume_refuses_but_keeps_the_floor():
    limits = FeedInLimits(
        pv_kw=(0.0,),
        load_kw=(0.0,),
        max_discharge_kw=1.0,
        slot_hours=0.25,
        tiers=((1.0, 0.08), (float("inf"), 0.12)),
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1), limits=limits
    )
    assert result.export_allowed is False
    assert result.floor == pytest.approx(0.08)


def test_tiered_negative_starting_volume_refuses():
    # A negative volume would manufacture distance to the next boundary and could declare the
    # contract exact.
    limits = FeedInLimits(
        pv_kw=(0.0,),
        load_kw=(0.0,),
        max_discharge_kw=1.0,
        slot_hours=0.25,
        tiers=((1.0, 0.08), (float("inf"), 0.12)),
        tier_volume_kwh=-5.0,
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1), limits=limits
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor == pytest.approx(0.08)


def test_tiered_incomplete_table_beyond_the_last_boundary_has_no_justified_floor():
    limits = FeedInLimits(
        pv_kw=(0.0,),
        load_kw=(0.0,),
        max_discharge_kw=1.0,
        slot_hours=0.25,
        tiers=((1.0, 0.05),),
        tier_volume_kwh=2.0,
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1), limits=limits
    )
    assert result.export_allowed is False and result.floor is None


@pytest.mark.parametrize(
    "tiers",
    [
        ((10.0, 0.10), (5.0, 0.20), (math.inf, 0.05)),  # bounds not strictly increasing
        ((10.0, 0.10), (10.0, 0.20), (math.inf, 0.05)),  # duplicate boundary
        ((10.0, 0.10), (math.inf, 0.05), (20.0, 0.02)),  # +inf is not the final bound
        ((10.0, math.inf), (math.inf, 0.05)),  # non-finite rate
        ((10.0, math.nan), (math.inf, 0.05)),  # NaN rate
        ((-1.0, 0.10), (math.inf, 0.05)),  # negative boundary
        ((10.0, "x"), (math.inf, 0.05)),  # malformed rate
        ((10.0,), (math.inf, 0.05)),  # malformed row
    ],
)
def test_malformed_tier_tables_are_refused_whole(tiers):
    limits = FeedInLimits(
        pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.25, tiers=tiers
    )
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "tiered"}), slots(1), limits=limits
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor is None  # a reparsed table must never provide a "floor"


# --------------------------------------------------------------------------------------------
# Saldering
# --------------------------------------------------------------------------------------------


def test_saldering_is_exact_when_the_allowance_covers_the_reachable_export():
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0, 0.0), load_kw=(0.0, 0.0), max_discharge_kw=1.0, slot_hours=0.25)
    result = settle_feed_in(
        configured,
        slots(2, buy=0.30),
        period_state=PeriodState(import_kwh=10.0, export_kwh=0.0),
        limits=limits,
    )
    assert result.export_allowed is True and result.reason is None
    assert result.sells == pytest.approx([0.30, 0.30])  # netting rate = buy
    assert result.floor == pytest.approx(0.30)
    assert result.max_export_kwh == pytest.approx(0.5)


def test_saldering_refuses_and_prices_forced_pv_at_the_floor_when_the_allowance_can_exhaust():
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(
        pv_kw=(3.0, 3.0),
        load_kw=(1.0, 1.0),
        max_discharge_kw=5.0,
        slot_hours=0.25,
    )
    result = settle_feed_in(
        configured,
        slots(2, buy=0.30),
        period_state=PeriodState(import_kwh=2.0, export_kwh=1.0),  # allowance 1.0 < 3.5 reachable
        limits=limits,
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    # floor = min over {buy, feed_in}; feed_in = buy - export fee = buy here.
    assert result.floor == pytest.approx(0.30)
    assert result.sells == pytest.approx([0.30, 0.30])


def test_saldering_floor_follows_a_negative_buy_price():
    # buy = -0.20, zero tax/fee: the floor is -0.20, never a VAT-scaled -0.158.
    configured = settings(**{"price.feed_in_mode": "saldering", "price.vat_pct": 21.0})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, slot_hours=0.25)
    result = settle_feed_in(
        configured,
        slots(1, buy=-0.20),
        period_state=PeriodState(import_kwh=1.0, export_kwh=1.0),  # allowance 0
        limits=limits,
    )
    assert result.export_allowed is False
    assert result.floor == pytest.approx(-0.20)
    assert result.sells == pytest.approx([-0.20])


def test_saldering_without_period_state_refuses():
    result = settle_feed_in(settings(**{"price.feed_in_mode": "saldering"}), slots(2))
    assert result.export_allowed is False and result.reason == Reason.UNPRICED_EXPORT
    assert result.floor is None


def test_saldering_with_state_but_unknown_reachability_refuses_at_the_buy_only_floor():
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "saldering"}),
        slots(1, buy=0.30),
        period_state=PeriodState(import_kwh=10.0, export_kwh=0.0),
    )
    assert result.export_allowed is False
    assert result.floor == pytest.approx(0.30)  # min(buy, feed_in) with feed_in = buy - fee


def test_saldering_assume_nettable_only_applies_when_the_state_is_unavailable():
    configured = settings(
        **{"price.feed_in_mode": "saldering", "price.saldering_assume_nettable": True}
    )
    assumed = settle_feed_in(configured, slots(2, buy=0.30))
    assert assumed.export_allowed is True and assumed.reason is None
    assert assumed.sells == pytest.approx([0.30, 0.30])
    assert any("assuming this period still has nettable import" in note for note in assumed.notes)


def test_saldering_assume_nettable_never_overrides_a_supplied_exhausted_state():
    configured = settings(
        **{"price.feed_in_mode": "saldering", "price.saldering_assume_nettable": True}
    )
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, slot_hours=0.25)
    result = settle_feed_in(
        configured,
        slots(1, buy=0.30),
        period_state=PeriodState(import_kwh=10.0, export_kwh=10.0),  # exhausted, known
        limits=limits,
    )
    assert result.export_allowed is False
    assert result.reason == Reason.UNPRICED_EXPORT
    assert result.floor == pytest.approx(0.30)
    assert not any("assuming this period" in note for note in result.notes)


def test_saldering_zero_allowance_state_is_not_treated_as_missing():
    # A real (0, 0) state means the allowance is already exhausted: refuse, and the note must not
    # claim the state was unavailable.
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.25)
    result = settle_feed_in(
        configured, slots(1, buy=0.30), period_state=PeriodState(0.0, 0.0), limits=limits
    )
    assert result.export_allowed is False
    assert not any("state" in note and "unavailable" in note for note in result.notes)


@pytest.mark.parametrize("state", [PeriodState(math.inf, 0.0), PeriodState(0.0, math.nan)])
def test_saldering_non_finite_period_state_refuses(state):
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.25)
    result = settle_feed_in(configured, slots(1, buy=0.30), period_state=state, limits=limits)
    assert result.export_allowed is False and result.floor is None


@pytest.mark.parametrize(
    "state",
    [
        PeriodState(0.0, -10.0),  # negative export would manufacture a 10 kWh allowance
        PeriodState(-1.0, 0.0),
        PeriodState(-1.0, -10.0),
    ],
)
def test_saldering_negative_period_totals_refuse(state):
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.25)
    result = settle_feed_in(configured, slots(1, buy=0.30), period_state=state, limits=limits)
    assert result.export_allowed is False and result.floor is None


def test_saldering_negative_balance_is_legitimate_and_does_not_refuse_the_state():
    # I - E may be negative (an export deficit); only the individual totals must be non-negative.
    configured = settings(**{"price.feed_in_mode": "saldering"})
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=1.0, slot_hours=0.25)
    result = settle_feed_in(
        configured,
        slots(1, buy=0.30),
        period_state=PeriodState(import_kwh=0.0, export_kwh=10.0),  # balance -10, both totals >= 0
        limits=limits,
    )
    assert result.export_allowed is False
    assert result.floor == pytest.approx(0.30)  # rates are known; only the allowance is exhausted


def test_saldering_unresolved_entity_never_substitutes_the_derived_formula():
    configured = settings(
        **{
            "price.feed_in_mode": "saldering",
            "price.feed_in_entity": "sensor.example_feed_in",
            "price.feed_in_eur_per_kwh": 0.05,  # must not stand in for the failed entity
        }
    )
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, slot_hours=0.25)
    result = settle_feed_in(
        configured,
        slots(1, buy=0.30),
        period_state=PeriodState(0.0, 0.0),
        limits=limits,
    )
    assert result.export_allowed is False
    # A configured but unresolved source refuses outright: the derived buy-based rate could exceed
    # the real explicit rate and break the lower-bound guarantee.
    assert result.floor is None


@pytest.mark.parametrize(
    "limits",
    [
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, explicit_price_eur_per_kwh=math.inf),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, explicit_price_eur_per_kwh=math.nan),
        FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, explicit_price_eur_per_kwh="x"),
    ],
)
def test_saldering_invalid_explicit_rate_refuses(limits):
    configured = settings(
        **{"price.feed_in_mode": "saldering", "price.feed_in_eur_per_kwh": 0.05}
    )
    result = settle_feed_in(
        configured,
        slots(1, buy=0.30),
        period_state=PeriodState(0.0, 0.0),
        limits=limits,
    )
    assert result.export_allowed is False and result.floor is None


def test_saldering_malformed_flat_tariff_refuses_instead_of_deriving():
    configured = settings(
        **{"price.feed_in_mode": "saldering", "price.feed_in_eur_per_kwh": math.inf}
    )
    limits = FeedInLimits(pv_kw=(0.0,), load_kw=(0.0,), max_discharge_kw=10.0, slot_hours=0.25)
    result = settle_feed_in(
        configured, slots(1, buy=0.30), period_state=PeriodState(0.0, 0.0), limits=limits
    )
    assert result.export_allowed is False and result.floor is None


# --------------------------------------------------------------------------------------------
# Whole-schedule settlement against independent contract arithmetic
# --------------------------------------------------------------------------------------------


def test_whole_schedule_settlement_matches_independent_contract_arithmetic():
    slot_list = slots(3, buy=0.30)
    imports = [3.0, 2.0, 0.0]
    exports = [0.5, 1.0, 0.5]
    feed_in = [0.10, 0.10, 0.10]
    settlement = settle_saldering_schedule(
        slot_list, imports, exports, PeriodState(import_kwh=10.0, export_kwh=0.0), feed_in
    )
    # Independently: I starts at 10, E at 0, so the allowance covers every export here.
    assert settlement.eligible_kwh == pytest.approx([0.5, 1.0, 0.5])
    assert settlement.excess_kwh == pytest.approx([0.0, 0.0, 0.0])
    assert settlement.credits_eur == pytest.approx([0.15, 0.30, 0.15])
    assert settlement.debits_eur == pytest.approx([0.90, 0.60, 0.0])
    assert settlement.end_import_kwh == pytest.approx(15.0)
    assert settlement.end_export_kwh == pytest.approx(2.0)


def test_saldering_import_after_allowance_exhaustion_credits_one_unit_at_buy():
    # docs/OPTIMIZER_V2.md section 2.1: I = 10, E = 12, import = 3, export = 2 credits exactly one
    # unit at buy and the rest at the feed-in price.
    settlement = settle_saldering_schedule(
        slots(1, buy=0.40), [3.0], [2.0], PeriodState(import_kwh=10.0, export_kwh=12.0), [0.05]
    )
    assert settlement.eligible_kwh == pytest.approx([1.0])
    assert settlement.excess_kwh == pytest.approx([1.0])
    assert settlement.credits_eur == pytest.approx([0.40 * 1.0 + 0.05 * 1.0])
    assert settlement.debits_eur == pytest.approx([0.40 * 3.0])


def test_saldering_credits_the_excess_at_the_feed_in_price():
    settlement = settle_saldering_schedule(
        slots(1, buy=0.40), [0.0], [2.0], PeriodState(0.0, 0.0), [0.05]
    )
    assert settlement.eligible_kwh == pytest.approx([0.0])
    assert settlement.excess_kwh == pytest.approx([2.0])
    assert settlement.credits_eur == pytest.approx([0.10])
    assert settlement.debits_eur == pytest.approx([0.0])


def test_saldering_transitions_carry_the_allowance_into_the_next_slot():
    settlement = settle_saldering_schedule(
        slots(2, buy=0.40), [4.0, 0.0], [5.0, 2.0], PeriodState(0.0, 0.0), [0.05, 0.05]
    )
    # slot 0: allowance 4 -> eligible 4, excess 1; then I=4, E=5.
    # slot 1: allowance max(0, 4 + 0 - 5) = 0 -> eligible 0, excess 2.
    assert settlement.eligible_kwh == pytest.approx([4.0, 0.0])
    assert settlement.excess_kwh == pytest.approx([1.0, 2.0])
    assert settlement.credits_eur == pytest.approx([0.40 * 4 + 0.05 * 1, 0.05 * 2])


@pytest.mark.parametrize("short", ["slots", "imports", "exports", "rates"])
def test_schedule_settlement_rejects_unequal_series(short):
    slot_list = slots(2)
    imports = [1.0, 1.0]
    exports = [0.5, 0.5]
    rates = [0.05, 0.05]
    if short == "slots":
        slot_list = slot_list[:1]
    elif short == "imports":
        imports = imports[:1]
    elif short == "exports":
        exports = exports[:1]
    else:
        rates = rates[:1]
    with pytest.raises(ValueError):
        settle_saldering_schedule(slot_list, imports, exports, PeriodState(), rates)


@pytest.mark.parametrize(
    "imports,exports,rates",
    [
        ([math.inf, 0.0], [0.0, 0.0], [0.05, 0.05]),
        ([0.0, 0.0], [math.nan, 0.0], [0.05, 0.05]),
        ([-1.0, 0.0], [0.0, 0.0], [0.05, 0.05]),
        ([0.0, 0.0], [0.0, -1.0], [0.05, 0.05]),
        ([0.0, 0.0], [0.0, 0.0], [math.inf, 0.05]),
    ],
)
def test_schedule_settlement_rejects_malformed_values(imports, exports, rates):
    with pytest.raises(ValueError):
        settle_saldering_schedule(slots(2), imports, exports, PeriodState(), rates)


@pytest.mark.parametrize(
    "state",
    [
        PeriodState(0.0, -10.0),
        PeriodState(-1.0, 0.0),
        PeriodState(math.inf, 0.0),
        PeriodState(0.0, math.nan),
    ],
)
def test_schedule_settlement_rejects_negative_or_non_finite_starting_totals(state):
    with pytest.raises(ValueError):
        settle_saldering_schedule(slots(1), [0.0], [0.5], state, [0.05])


# --------------------------------------------------------------------------------------------
# Negative feed-in
# --------------------------------------------------------------------------------------------


def test_negative_feed_in_price_is_preserved():
    explicit = settle_feed_in(
        settings(**{"price.feed_in_mode": "explicit", "price.feed_in_eur_per_kwh": -0.05}),
        slots(1),
    )
    assert explicit.export_allowed is True
    assert explicit.sells == pytest.approx([-0.05])

    # buy = -0.20 with the tax refunded: sell = buy.
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "buy_minus_fee"}), slots(1, buy=-0.20)
    )
    assert result.sells == pytest.approx([-0.20])


# --------------------------------------------------------------------------------------------
# Live path: non-derived modes stay out until a consumer enforces the complete result
# --------------------------------------------------------------------------------------------


def test_default_mode_keeps_the_legacy_derived_path():
    forecast = PriceForecast(slots=slots(2, buy=0.30, energy_tax=0.11))
    configured = settings(
        **{"price.export_fee_eur_per_kwh": 0.005, "price.feed_in_energy_tax_refund": False}
    )
    assert _apply_feed_in(forecast, configured, None) is True
    assert all(slot.sell == pytest.approx(0.30 - 0.005 - 0.11) for slot in forecast.slots)
    assert any("derived feed-in price" in note for note in forecast.notes)


def test_non_derived_modes_are_inert_in_the_live_forecast_path():
    # The live path has no channel for export_allowed/reason/floor, so a configured contract mode
    # must not reach it: a refusal would otherwise become ordinary zero-priced slots. Everything
    # else in the legacy resolution order (a flat tariff here) still applies exactly as today.
    base = {
        "price.feed_in_eur_per_kwh": 0.06,
        "price.export_fee_eur_per_kwh": 0.005,
        "price.feed_in_energy_tax_refund": False,
    }
    with_mode = PriceForecast(slots=slots(2, buy=0.30, energy_tax=0.11))
    _apply_feed_in(with_mode, settings(price_feed_in_mode="explicit", **base), None)
    without_mode = PriceForecast(slots=slots(2, buy=0.30, energy_tax=0.11))
    _apply_feed_in(without_mode, settings(**base), None)
    assert [slot.sell for slot in with_mode.slots] == pytest.approx(
        [slot.sell for slot in without_mode.slots]
    )
    assert all(slot.sell == pytest.approx(0.06 - 0.005) for slot in with_mode.slots)
    assert not any("contract" in note for note in with_mode.notes)


# --------------------------------------------------------------------------------------------
# Parity between the module's supported modes and the legacy forecast arithmetic
# --------------------------------------------------------------------------------------------


def test_module_explicit_rate_matches_the_legacy_flat_tariff_path():
    configured = settings(
        **{
            "price.feed_in_mode": "explicit",
            "price.feed_in_eur_per_kwh": 0.06,
            "price.export_fee_eur_per_kwh": 0.005,
            "price.feed_in_tax_inclusive": True,
        }
    )
    forecast = PriceForecast(slots=slots(2, buy=0.40))
    _apply_feed_in(forecast, configured, None)
    result = settle_feed_in(configured, forecast.slots)
    assert result.sells == pytest.approx([slot.sell for slot in forecast.slots])


def test_module_explicit_limit_price_overrides_the_flat_tariff():
    configured = settings(**{"price.feed_in_mode": "explicit", "price.feed_in_eur_per_kwh": 0.05})
    result = settle_feed_in(
        configured, slots(1), limits=FeedInLimits(explicit_price_eur_per_kwh=0.06)
    )
    assert result.sells == pytest.approx([0.06])


def test_module_buy_minus_fee_matches_legacy_derived_when_the_tax_is_refunded():
    legacy = settings(**{"price.export_fee_eur_per_kwh": 0.005})
    forecast = PriceForecast(slots=slots(2, buy=0.40, energy_tax=0.11))
    _apply_feed_in(forecast, legacy, None)
    result = settle_feed_in(
        settings(**{"price.feed_in_mode": "buy_minus_fee", "price.export_fee_eur_per_kwh": 0.005}),
        forecast.slots,
    )
    assert result.sells == pytest.approx([slot.sell for slot in forecast.slots])


def test_module_buy_minus_fee_matches_legacy_derived_for_a_confirmed_component():
    legacy = settings(
        **{"price.export_fee_eur_per_kwh": 0.005, "price.feed_in_energy_tax_refund": False}
    )
    forecast = PriceForecast(slots=slots(2, buy=0.40, energy_tax=0.11))
    _apply_feed_in(forecast, legacy, None)
    result = settle_feed_in(
        settings(
            **{
                "price.feed_in_mode": "buy_minus_fee",
                "price.export_fee_eur_per_kwh": 0.005,
                "price.feed_in_energy_tax_refund": False,
                "price.energy_tax_confirmed": True,
            }
        ),
        forecast.slots,
    )
    assert result.sells == pytest.approx([slot.sell for slot in forecast.slots])


def test_module_deduction_override_is_a_stated_replacement_the_legacy_path_ignores():
    configured = settings(
        **{
            "price.feed_in_mode": "buy_minus_fee",
            "price.feed_in_energy_tax_refund": False,
            "price.energy_tax_confirmed": True,
            "price.feed_in_tax_deduction_eur_per_kwh": 0.02,
            "price.export_fee_eur_per_kwh": 0.005,
        }
    )
    result = settle_feed_in(configured, slots(1, buy=0.40, energy_tax=0.11))
    assert result.sells == pytest.approx([0.375])

    # The legacy derived path never reads the override: it still subtracts the slot component.
    forecast = PriceForecast(slots=slots(1, buy=0.40, energy_tax=0.11))
    _apply_feed_in(
        forecast,
        settings(**{"price.export_fee_eur_per_kwh": 0.005, "price.feed_in_energy_tax_refund": False}),
        None,
    )
    assert forecast.slots[0].sell == pytest.approx(0.285)


def test_empty_forecast_settles_to_no_slots():
    result = settle_feed_in(settings(**{"price.feed_in_mode": "explicit"}), [])
    assert result.sells == [] and result.export_allowed is False and result.floor is None
