"""Contract-correct feed-in accounting (SPEC.md section 4.2; optimizer v2 stage 1b).

A pure settlement and reachability module: given the configured contract (``price.feed_in_mode``),
the price slots, the period import/export state and the plan's reachability limits, it decides what
a slot's export actually earns and whether the *battery-sourced* export leg may be priced exactly.

Nothing here reads Home Assistant, the database or the clock, and nothing here actuates a battery:
the optimizer wiring is a separate stage (docs/OPTIMIZER_V2.md section 2). The rule is
**exact-or-refuse**:

* ``explicit`` / ``buy_minus_fee`` are exogenous and energy-only, so exact by construction.
* ``saldering`` is exact only while the period allowance ``I - E`` stays positive for every
  reachable path; the reachable export bound is ``Σ min(export limit, max(0, pv - load) +
  max_discharge_power) · dt`` and is deliberately **not** capped by the pack's energy (PV surplus can
  export without the battery, and a grid-recharge cycle can export more than one pack's worth).
* ``tiered`` is exact only while the reachable volume stays below the next tier boundary above the
  starting volume ``V``.
* A contract that does not refund the energy tax may price ``sell = buy - tax`` only when the
  component is confirmed (``price.energy_tax_confirmed``); otherwise it refuses the export leg
  (``Reason.UNPRICED_EXPORT``).

Unknown is never a number: a missing, malformed or non-finite input (an infinite discharge
capability, an infinite tariff, a missing slot tax) refuses instead of becoming a convenient zero,
and a malformed tier table refuses instead of being silently repaired.

A refusal never stops forced PV export from being valued: it is priced at
``forced_export_price_floor``, the minimum over every settlement rate reachable over the horizon.
When even that bound is unknown the plan must perform no tariff-dependent optimisation at all, which
the caller sees as ``floor is None``.

``derived`` is deliberately **not** implemented here: it is the live legacy path in
``sources/price.py::_apply_feed_in`` (configured explicit tariffs win, and the legacy rule always
uses the slot's own energy-tax component). Re-implementing it would risk diverging from the path
that actually prices the forecast, so :func:`settle_feed_in` raises for it.

``period_state`` and the reachability ``limits`` are inputs: wiring them to real sensors and to the
optimizer is a follow-up, and until a consumer enforces the complete result (``export_allowed``,
``reason``, ``floor``) no live forecast path calls this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Reason

__all__ = [
    "MODES",
    "FeedInLimits",
    "FeedInResult",
    "PeriodState",
    "ScheduleSettlement",
    "max_reachable_export_kwh",
    "settle_feed_in",
    "settle_saldering_schedule",
]

#: Modes this module prices. ``derived`` is the live path in ``sources/price.py`` and is rejected
#: here rather than re-implemented (see the module docstring).
MODES = ("explicit", "buy_minus_fee", "saldering", "tiered")

_EPS = 1e-9


@dataclass(frozen=True)
class PeriodState:
    """Period-to-date metered totals for the saldering allowance, in kWh.

    Both totals must be finite and non-negative; the balance ``I - E`` may be negative (an export
debt from an exhausted period is legitimate). The supported convention settles import first, then
export, inside a slot, so the allowance ``I - E`` is used raw.
    """

    import_kwh: float = 0.0
    export_kwh: float = 0.0


@dataclass(frozen=True)
class FeedInLimits:
    """Reachability inputs for the horizon; every field is optional.

    ``max_discharge_kw is None`` (or a PV/load series that does not cover every slot) means the
    battery/grid export power over the horizon is unknown, so the reachable export bound is
    unbounded and every threshold is reachable: the module refuses rather than guessing. An absent
    ``export_limit_kw`` means no grid/inverter limit is known, which only widens the bound. Every
    supplied value must be finite and non-negative; an infinite, NaN or negative capability, sample
    or volume is unknown, not zero.

    ``explicit_price_eur_per_kwh`` is the caller-resolved feed-in tariff, already net of the export
    fee and VAT-inclusive when applicable. When it is absent and ``price.feed_in_entity`` is
    configured, the pure module cannot read HA and the entity is authoritative, so the rate is
    unknown and the explicit leg is refused - it never falls back to the flat tariff behind a
    failed entity. ``tiers`` is the tiered contract as ``(upper_bound_kwh, rate_eur_per_kwh)``
    pairs: strictly increasing finite bounds, with ``+inf`` permitted only as the final bound, and
    finite rates. ``tier_volume_kwh`` is the starting cumulative volume ``V`` when the caller tracks
    it separately from the period export total.
    """

    pv_kw: Sequence[float] = ()
    load_kw: Sequence[float] = ()
    max_discharge_kw: float | None = None
    export_limit_kw: float | None = None
    slot_hours: float = 0.25
    explicit_price_eur_per_kwh: float | None = None
    tiers: Sequence[tuple[float, float]] = ()
    tier_volume_kwh: float | None = None


@dataclass(frozen=True)
class FeedInResult:
    """What the contract decided for this horizon.

    ``sells`` is the EUR/kWh received per slot. ``export_allowed`` False means the battery-sourced
    export leg is refused (``reason`` is :data:`Reason.UNPRICED_EXPORT`); forced PV export is still
    priced at ``floor`` when a justified bound exists. ``floor is None`` means no justified bound at
    all, so the caller must perform no tariff-dependent optimisation and must not read ``sells`` as
    prices.
    """

    mode: str
    sells: list[float]
    export_allowed: bool
    reason: str | None
    floor: float | None
    notes: list[str] = field(default_factory=list)
    max_export_kwh: float | None = None


@dataclass(frozen=True)
class ScheduleSettlement:
    """Whole-schedule settlement under the supported in-slot saldering convention (section 2.1)."""

    credits_eur: list[float]
    debits_eur: list[float]
    eligible_kwh: list[float]
    excess_kwh: list[float]
    end_import_kwh: float
    end_export_kwh: float


# --------------------------------------------------------------------------------------------
# Strict readers: a missing, malformed or non-finite value is unknown, never zero
# --------------------------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """A finite float, or ``None`` when the value is missing, malformed or non-finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _slot_buy(slot: Any) -> float | None:
    return _finite(getattr(slot, "buy", None))


def _slot_component(slot: Any) -> float | None:
    """The slot's energy-tax component, or ``None`` when missing, malformed or negative."""
    component = _finite(getattr(slot, "energy_tax", None))
    return component if component is not None and component >= 0.0 else None


def _export_fee(settings: Any) -> float | None:
    return _finite(settings.get("price.export_fee_eur_per_kwh", 0.0))


def _refund(settings: Any) -> bool:
    return bool(settings.get("price.feed_in_energy_tax_refund", True))


def _confirmed(settings: Any) -> bool:
    return bool(settings.get("price.energy_tax_confirmed", False))


def _stated_deduction(settings: Any) -> tuple[bool, float | None]:
    """``(is_stated, value)`` for the configured per-kWh deduction; value is ``None`` if malformed."""
    raw = settings.get("price.feed_in_tax_deduction_eur_per_kwh", None)
    if raw is None:
        return False, None
    number = _finite(raw)
    if number is None or number < 0.0:
        return True, None
    return True, number


def _tax_deduction(settings: Any, slot: Any) -> float | None:
    """The tax to deduct post-saldering, or ``None`` when it is unknown or not confirmed.

    A contract that refunds the tax deducts nothing (0.0). Otherwise the component must be
    *confirmed*: an unstated default is not zero, and a missing or non-finite slot component stays
    unknown even with the confirmation flag on (a confirmed 0.0 is a valid, finite zero).
    """
    if _refund(settings):
        return 0.0
    if not _confirmed(settings):
        return None
    stated, value = _stated_deduction(settings)
    return value if stated else _slot_component(slot)


def _explicit_rate(settings: Any, limits: FeedInLimits | None) -> tuple[bool, float | None]:
    """``(configured, rate)``: whether an explicit source is configured, and its finite net rate.

    ``configured`` is True whenever the caller supplied a resolved price, or an entity or flat
    tariff is set. A configured source that cannot be resolved or is malformed yields
    ``(True, None)`` so callers refuse instead of substituting another formula. A caller-supplied
    ``limits.explicit_price_eur_per_kwh`` is already net and wins; otherwise the flat
    ``price.feed_in_eur_per_kwh`` gets the VAT rule and the export fee applied here. A configured
    ``price.feed_in_entity`` is authoritative but unreadable from this pure module, so it makes the
    rate unknown rather than letting the flat tariff stand in for it.
    """
    if limits is not None and limits.explicit_price_eur_per_kwh is not None:
        return True, _finite(limits.explicit_price_eur_per_kwh)
    if str(settings.get("price.feed_in_entity", "") or "").strip():
        return True, None
    flat = settings.get("price.feed_in_eur_per_kwh", None)
    if flat is None:
        return False, None
    rate = _finite(flat)
    if rate is None:
        return True, None
    if not bool(settings.get("price.feed_in_tax_inclusive", True)):
        vat = _finite(settings.get("price.vat_pct", 0.0))
        if vat is None:
            return True, None
        rate *= 1.0 + vat / 100.0
    fee = _export_fee(settings)
    if fee is None:
        return True, None
    return True, rate - fee


def _post_saldering_rates(
    settings: Any, slots: Sequence[Any], limits: FeedInLimits | None
) -> list[float] | None:
    """Per-slot post-saldering feed-in price, or ``None`` when it is unknown.

    The derived ``buy - fee - deduction`` formula is used **only** when no explicit source is
    configured. A configured source that is unresolved or malformed returns ``None``: substituting
    the derived formula could exceed the actual explicit export rate and break the lower-bound
    guarantee.
    """
    configured, explicit = _explicit_rate(settings, limits)
    if explicit is not None:
        return [explicit] * len(slots)
    if configured:
        return None
    fee = _export_fee(settings)
    if fee is None:
        return None
    rates: list[float] = []
    for slot in slots:
        deduction = _tax_deduction(settings, slot)
        buy = _slot_buy(slot)
        if deduction is None or buy is None:
            return None
        rates.append(buy - fee - deduction)
    return rates


# --------------------------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------------------------


def max_reachable_export_kwh(slot_count: int, limits: FeedInLimits | None) -> float | None:
    """Bound on the cumulative metered export over the horizon, or ``None`` when it is unknown.

    ``Σ min(export limit, max(0, pv - load) + max_discharge_power) · dt``: PV surplus that never
    touches the battery can export, and a grid recharge followed by a discharge can export more than
    one pack's worth, so a pack-energy bound would be unsound. ``None`` means the inputs do not
    justify a finite bound (no discharge capability, PV/load not supplied for every slot, or any
    supplied value missing, malformed or non-finite); the caller must treat that as "every threshold
    is reachable" and refuse. A non-finite capability is unknown, never zero.
    """
    if limits is None or slot_count <= 0:
        return None
    pv = list(limits.pv_kw)
    load = list(limits.load_kw)
    if len(pv) != slot_count or len(load) != slot_count:
        return None
    hours = _finite(limits.slot_hours)
    if hours is None or hours <= 0.0:
        return None
    discharge = _finite(limits.max_discharge_kw)
    if discharge is None or discharge < 0.0:
        return None
    limit: float | None = None
    if limits.export_limit_kw is not None:
        limit = _finite(limits.export_limit_kw)
        if limit is None or limit < 0.0:
            return None
    total = 0.0
    for pv_raw, load_raw in zip(pv, load):
        pv_kw = _finite(pv_raw)
        load_kw = _finite(load_raw)
        if pv_kw is None or load_kw is None or pv_kw < 0.0 or load_kw < 0.0:
            return None
        # A negative sample is malformed, not zero surplus: it would understate reachable exports.
        capacity = max(0.0, pv_kw - load_kw) + discharge
        if limit is not None:
            capacity = min(limit, capacity)
        total += capacity * hours
    return total


def _normalise_tiers(tiers: Sequence[Any]) -> list[tuple[float, float]] | None:
    """Validate a tier table, or return ``None`` when it is malformed.

    Strictly increasing bounds, finite rates, and ``+inf`` permitted only as the final bound. A
    malformed table is refused whole: dropping an invalid middle tier would silently extend another
    rate across its interval.
    """
    rows = list(tiers or ())
    if not rows:
        return None
    out: list[tuple[float, float]] = []
    previous: float | None = None
    for index, entry in enumerate(rows):
        try:
            bound_raw, rate_raw = entry[0], entry[1]
        except (TypeError, IndexError, KeyError):
            return None
        if isinstance(bound_raw, bool) or isinstance(rate_raw, bool):
            return None
        if bound_raw == math.inf:
            if index != len(rows) - 1:
                return None
            bound = math.inf
        else:
            parsed = _finite(bound_raw)
            if parsed is None or parsed < 0.0:
                return None
            bound = parsed
        rate = _finite(rate_raw)
        if rate is None:
            return None
        if previous is not None and not bound > previous:
            return None
        previous = bound
        out.append((bound, rate))
    return out


# --------------------------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------------------------


def settle_saldering_schedule(
    slots: Sequence[Any],
    imports_kwh: Sequence[float],
    exports_kwh: Sequence[float],
    period_state: PeriodState,
    feed_in_rates: Sequence[float],
) -> ScheduleSettlement:
    """Settle a whole schedule under the supported in-slot saldering convention.

    Per slot, import is settled first and then export, so a crossing export is never credited wholly
    at the netting rate::

        eligible = min(export, max(0, I + import - E))
        excess   = export - eligible
        credit   = buy·eligible + feed_in·excess
        debit    = buy·import

    with ``I' = I + import``, ``E' = E + export``. This is the independent contract arithmetic the
    reachability pricing above must stay consistent with. The four series must have equal lengths
    and every value must be finite (flows non-negative); anything else raises :class:`ValueError`
    rather than silently truncating or carrying an infinity.
    """
    slot_list = list(slots)
    imports = [_finite(value) for value in imports_kwh]
    exports = [_finite(value) for value in exports_kwh]
    rates = [_finite(value) for value in feed_in_rates]
    lengths = {len(slot_list), len(imports), len(exports), len(rates)}
    if len(lengths) != 1:
        raise ValueError(
            "settle_saldering_schedule needs equal-length slots/imports/exports/feed_in_rates "
            f"(got {len(slot_list)}, {len(imports)}, {len(exports)}, {len(rates)})"
        )
    start_import = _finite(period_state.import_kwh)
    start_export = _finite(period_state.export_kwh)
    if start_import is None or start_export is None or start_import < 0.0 or start_export < 0.0:
        raise ValueError("settle_saldering_schedule needs non-negative finite period totals")
    for index, (slot, draw, delivered, rate) in enumerate(
        zip(slot_list, imports, exports, rates)
    ):
        buy = _slot_buy(slot)
        if buy is None:
            raise ValueError(f"slot {index} has no finite buy price")
        if draw is None or draw < 0.0 or delivered is None or delivered < 0.0:
            raise ValueError(f"slot {index} has a missing, non-finite or negative flow")
        if rate is None:
            raise ValueError(f"slot {index} has no finite feed-in rate")

    import_total = start_import
    export_total = start_export
    credits: list[float] = []
    debits: list[float] = []
    eligible: list[float] = []
    excess: list[float] = []
    for slot, draw, delivered, rate in zip(slot_list, imports, exports, rates):
        allowance = max(0.0, import_total + draw - export_total)
        netted = min(delivered, allowance)
        overflow = delivered - netted
        buy = float(getattr(slot, "buy"))
        credits.append(buy * netted + rate * overflow)
        debits.append(buy * draw)
        eligible.append(netted)
        excess.append(overflow)
        import_total += draw
        export_total += delivered
    return ScheduleSettlement(credits, debits, eligible, excess, import_total, export_total)


def _refuse(
    mode: str,
    slots: Sequence[Any],
    floor: float | None,
    note: str,
    max_export: float | None = None,
) -> FeedInResult:
    sells = [floor if floor is not None else 0.0] * len(slots)
    return FeedInResult(mode, sells, False, Reason.UNPRICED_EXPORT, floor, [note], max_export)


def _settle_saldering(
    settings: Any, slots: Sequence[Any], period_state: PeriodState | None, limits: FeedInLimits | None
) -> FeedInResult:
    buys: list[float] = []
    for slot in slots:
        buy = _slot_buy(slot)
        if buy is None:
            return _refuse(
                "saldering",
                slots,
                None,
                "saldering needs a finite buy price for every slot; battery export refused",
            )
        buys.append(buy)
    if period_state is None:
        if bool(settings.get("price.saldering_assume_nettable", False)):
            # Explicitly stated assumption, not a fact: the allowance is treated as still positive.
            return FeedInResult(
                "saldering",
                list(buys),
                True,
                None,
                min(buys),
                [
                    "saldering: assuming this period still has nettable import "
                    "(price.saldering_assume_nettable); export credited at the buy price"
                ],
                None,
            )
        return _refuse(
            "saldering",
            slots,
            None,
            "saldering needs the period import/export state; without it the battery-sourced export "
            "leg is refused (set price.saldering_assume_nettable to assume the allowance is still "
            "positive)",
        )
    # A supplied period state is authoritative: the assumption never overrides known, contradictory
    # state (an exhausted allowance with reachable excess export).
    import_total = _finite(period_state.import_kwh)
    export_total = _finite(period_state.export_kwh)
    if (
        import_total is None
        or export_total is None
        or import_total < 0.0
        or export_total < 0.0
    ):
        return _refuse(
            "saldering",
            slots,
            None,
            "saldering: the supplied period import/export state is missing, non-finite or negative; "
            "battery export refused",
        )
    allowance = import_total - export_total
    max_export = max_reachable_export_kwh(len(slots), limits)
    if max_export is not None and max_export <= allowance + _EPS:
        # The allowance stays positive for every reachable path, so the marginal rate is buy(t).
        return FeedInResult(
            "saldering",
            list(buys),
            True,
            None,
            min(buys),
            [
                f"saldering: period allowance {allowance:.3f} kWh covers the reachable export "
                f"{max_export:.3f} kWh; export credited at the buy price"
            ],
            max_export,
        )
    feed_in = _post_saldering_rates(settings, slots, limits)
    if feed_in is None:
        return _refuse(
            "saldering",
            slots,
            None,
            "saldering: the reachable export can exhaust the period allowance and the post-saldering "
            "feed-in rate is unknown (an unconfirmed or missing energy-tax component, or an "
            "unresolved explicit source); no justified floor, so no tariff-dependent optimisation",
            max_export,
        )
    floor = min(min(buys), min(feed_in))
    return _refuse(
        "saldering",
        slots,
        floor,
        f"saldering: the period allowance can be exhausted within the reachable export; battery "
        f"export refused, forced PV export priced at the {floor:.4f} EUR/kWh floor",
        max_export,
    )


def _settle_tiered(
    slots: Sequence[Any], period_state: PeriodState | None, limits: FeedInLimits | None
) -> FeedInResult:
    tiers = _normalise_tiers(limits.tiers if limits is not None else ())
    if tiers is None:
        return _refuse(
            "tiered",
            slots,
            None,
            "tiered: no valid tier table supplied (bounds must strictly increase, rates must be "
            "finite, and +inf is only allowed as the final bound); battery export refused",
        )
    rates = [rate for _bound, rate in tiers]
    complete = math.isinf(tiers[-1][0])
    # The floor spans every tier rate: any tier is reachable over the horizon for some path.
    floor = min(rates) if complete else None
    volume: float | None
    if limits is not None and limits.tier_volume_kwh is not None:
        volume = _finite(limits.tier_volume_kwh)
    elif period_state is not None:
        volume = _finite(period_state.export_kwh)
    else:
        volume = None
    if volume is None or volume < 0.0:
        return _refuse(
            "tiered",
            slots,
            floor,
            "tiered: the starting cumulative volume is unavailable, non-finite or negative; battery "
            "export refused",
        )
    current: float | None = None
    boundary = math.inf
    for bound, rate in tiers:
        if bound > volume:
            current = rate
            boundary = bound
            break
    if current is None:
        if not complete:
            return _refuse(
                "tiered",
                slots,
                None,
                "tiered: the period volume is at or beyond the last tier boundary and the tier table "
                "is not open-ended; no justified bound, so no tariff-dependent optimisation",
            )
        current = tiers[-1][1]
    max_export = max_reachable_export_kwh(len(slots), limits)
    if max_export is not None and volume + max_export <= boundary + _EPS:
        return FeedInResult(
            "tiered",
            [current] * len(slots),
            True,
            None,
            floor,
            [
                f"tiered: reachable volume {volume + max_export:.3f} kWh stays below the next "
                f"boundary {boundary:g}; export priced exactly at {current:.4f} EUR/kWh"
            ],
            max_export,
        )
    note = "tiered: the next tier boundary is reachable within the horizon; battery export refused"
    if floor is None:
        note += "; the tier table is not open-ended, so there is no justified floor"
    return _refuse("tiered", slots, floor, note, max_export)


def settle_feed_in(
    settings: Any,
    slots: Sequence[Any],
    period_state: PeriodState | None = None,
    limits: FeedInLimits | None = None,
) -> FeedInResult:
    """Price export per slot under the configured contract, exact-or-refuse (see module docstring).

    Raises :class:`ValueError` for ``derived`` and for unknown modes: ``derived`` is the live legacy
    path in :mod:`app.sources.price`, and this API deliberately does not re-implement it.
    """
    mode = str(settings.get("price.feed_in_mode", "derived") or "derived").strip() or "derived"
    if mode not in MODES:
        raise ValueError(
            f"settle_feed_in does not price {mode!r}; supported modes are {list(MODES)}. 'derived' "
            "is the live legacy path in app.sources.price::_apply_feed_in and is intentionally not "
            "re-implemented here."
        )
    slot_list = list(slots)
    if not slot_list:
        return FeedInResult(mode, [], False, None, None, ["no price slots to settle"], None)

    if mode == "explicit":
        _configured, rate = _explicit_rate(settings, limits)
        if rate is None:
            return _refuse(
                "explicit",
                slot_list,
                None,
                "explicit feed-in contract has no usable tariff (set a finite "
                "price.feed_in_eur_per_kwh, resolve price.feed_in_entity, or pass a finite "
                "explicit price); battery export refused",
            )
        return FeedInResult(
            mode,
            [rate] * len(slot_list),
            True,
            None,
            rate,
            [f"explicit feed-in contract: {rate:.4f} EUR/kWh received"],
            None,
        )

    if mode == "buy_minus_fee":
        fee = _export_fee(settings)
        if fee is None:
            return _refuse(
                "buy_minus_fee",
                slot_list,
                None,
                "buy_minus_fee: price.export_fee_eur_per_kwh is missing or non-finite; battery "
                "export refused",
            )
        sells: list[float] = []
        for slot in slot_list:
            buy = _slot_buy(slot)
            deduction = _tax_deduction(settings, slot)
            if buy is None:
                return _refuse(
                    "buy_minus_fee",
                    slot_list,
                    None,
                    "buy_minus_fee needs a finite buy price for every slot; battery export refused",
                )
            if deduction is None:
                return _refuse(
                    "buy_minus_fee",
                    slot_list,
                    None,
                    "buy_minus_fee: the energy-tax component is not confirmed (or the confirmed "
                    "component is missing or non-finite) and the contract does not refund it; "
                    "battery export refused rather than pricing sell = buy",
                )
            sells.append(buy - fee - deduction)
        return FeedInResult(
            mode,
            sells,
            True,
            None,
            min(sells),
            [
                f"buy_minus_fee feed-in contract: buy price minus feed-in cost {fee:.4f} EUR/kWh "
                "minus the confirmed energy-tax component"
            ],
            None,
        )

    if mode == "saldering":
        return _settle_saldering(settings, slot_list, period_state, limits)

    if mode == "tiered":
        return _settle_tiered(slot_list, period_state, limits)

    raise ValueError(f"unsupported price.feed_in_mode {mode!r}")  # pragma: no cover - guarded above
