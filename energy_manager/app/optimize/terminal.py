"""Terminal value: the stage-3 continuation row (docs/OPTIMIZER_V2.md section 4).

The DP's terminal row is ``-salvage * e`` today.  Stage 3 replaces it, for a plan that satisfies the
**structural sufficiency condition**, with a continuation-cost vector over the stored-energy grid:
the value of the tail, solved on the *main* state grid so the row aligns index-by-index, ending in
the same ``salvage`` row.

Three windows, deliberately distinct (docs/OPTIMIZER_V2.md section 4):

* the **planning/accounting window** -- ``[horizon_start, + horizon_hours)``: the plan, the feed-in
  settlement and the ledger accounting all live here, and ``price.slots`` is its price series;
* the **available published-price window** -- the provider entries past the planning window, gridded
  onto the same slot grid out to ``optimizer.terminal_tail_hours`` and carried additively on
  ``PriceForecast.tail_slots``.  They never enter ``slots``, so the settlement and the period
  volumes cannot move;
* the **synthetic extension** -- past the last published entry, the fetched window repeated by
  time-of-day at the same slot resolution.  The tail solved here can be longer than the published
  window (``L``, ``2L`` and ``3L``), and everything past the published entries is filled by this
  rule.

Two resolutions, and the one place an approximation enters.  The **sufficiency checks and the
pricing run at the plan's own (native) resolution** -- every published price, every PV/load sample
and every contract settlement is native -- because the sell floor and the billing state bind per
native slot.  The **tail solve runs at ``optimizer.terminal_tail_minutes``** (default 60, never
finer than the plan): ``aggregate_tail`` folds whole native sub-slots into one tail slot, taking
``buy``/``sell`` as the mean of the sub-slots (a slot's cost is linear in the price) and
``pv_kw``/``load_kw`` as the mean with the summed duration, so the energy over the coarse ``dt`` is
the same quantity.  A coarse tail can therefore **understate a spike** that lasts less than one tail
slot, and the stability result below is a statement at the tail resolution, not a claim about the
native optimum.  The tail's **extension increment is a whole number of synthetic-profile periods**
(24 h): the profile repeats by time of day, so a horizon-length increment (30 h in the live shape)
would leave ``L``, ``2L`` and ``3L`` ending at different daily phases and the stability spans would
measure the profile rather than the tail.  The requested ``optimizer.terminal_tail_hours`` is
rounded **up** to a whole period, and the same period drives the coarse bucket restart, so the
increment stays a whole profile period at the tail resolution too.  The state grid is always the
plan's own grid -- never coarsened for the tail --
because a continuation value on a different grid is not a continuation value of the plan being
optimised.

Structural sufficiency (docs/OPTIMIZER_V2.md section 4).  The vector may replace ``salvage`` only
when the omitted state cannot change the tail's optimal value for any reachable path:

* **the sell floor is disabled, or provably never binds.**  ``dp.grid_window_kw`` tightens a bound
  in exactly two branches -- ``sell(t) < floor`` (no flow below the house's own demand may export)
  and ``base(t) > 0 and buy(t) < floor`` (even displacing import cannot pay for the held energy),
  with ``base = load - pv``.  The floor the DP receives is the fleet's energy-weighted mean
  break-even, ``mean_i(basis_i / rt_i + wear_i) + margin`` (SPEC 5), and the *basis* is an
  energy-weighted average of the current basis and the prices of the energy charged since -- a
  discharge leaves it, only the quantity is reconciled -- so at any moment it stays inside the
  convex hull of ``{basis_now}`` and every **charge price since the plan started**, which includes
  the whole **main window**, not just the tail.  That horizon-inclusive part is the soundness fix:
  a high charge price during the planning window raises the basis the tail inherits, so a bound
  built only from the tail's own prices would clear a plan whose floor really binds.

  The bound is therefore a **running** one per tail slot: before slot ``j`` the reachable basis is
  at most ``buy_hi(j) = max(basis_now, max main-window buy, max tail buy before j)`` -- slot ``j``'s
  own charge price is excluded because a discharge (the leg the floor restricts) does not change the
  basis, and including it would make the house leg unsatisfiable by construction.  With
  ``basis_hi_i(j) = max(basis_now_i, buy_hi(j))`` the fleet floor is at most
  ``max_i(basis_hi_i(j) / rt_i + wear_i + margin)``, and the condition holds when that bound is at
  or below every tail slot's ``sell`` and every deficit slot's ``buy``.  PV-surplus charging cannot
  raise it: the supported contracts pay at most the buy price for the forgone export, so a PV charge
  never prices the basis above ``max tail buy``.
* **the billing state cannot change the tail's rewards** (section 2.3 reachability).  The tail is
  settled from **every candidate state spanning the reachable horizon-end set** -- not from one
  endpoint -- by reusing :func:`app.sources.feed_in.settle_feed_in`, so the section 2.3 rule has one
  implementation.  Both period totals are reachable inputs: imports only grow during the horizon, so
  a household that is net-exporting now can become net-importing and *restore* a saldering
  allowance that the tail's pricing depends on.  The reachable set is therefore the interval of the
  quantity the settlement branches on -- the allowance ``import - export`` for ``saldering``,
  bounded by **both** the export and the import reachability, and the cumulative export volume for
  ``tiered`` -- and the candidates are its two endpoints plus every threshold strictly inside it
  (the tail's own reachable export, and every tier boundary).  A refusal, an unpriceable contract,
  an unknown reachable bound, or a *priced tail that differs between candidates* disables the
  vector.

Failing either condition disables the vector for that plan: ``salvage`` is used and the reason is
reported on the plan and its summary.  No sampling of the observed basis is evidence of sufficiency.

Numerical stability, not convergence.  The DP has no discount factor, so each added period shifts
the whole row; only the offset-invariant **span** of the difference between two phase-aligned tail
lengths is decision-relevant, and the main DP's argmin is invariant to an additive constant on its
terminal row.  A tail whose period repeats the *state* at the period boundary -- a block of cheap or
expensive slots -- has a nearly state-independent per-period value and passes; a single spike does
not, because how much energy was carried into it changes the next period's value.  Passing is
evidence of numerical stability at the tested lengths **and at the tail resolution**, never a
convergence guarantee about the native 15-minute optimum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Callable, Sequence

from ..models import PriceSlot
from ..sources.feed_in import (
    FeedInLimits,
    FeedInRequest,
    PeriodState,
    max_reachable_export_kwh,
    settle_feed_in,
)

#: The tail-stability tolerance, EUR per stored kWh (docs/OPTIMIZER_V2.md section 4).  Converted to
#: the monetary tolerance actually compared by multiplying by the state span (``e_max - e_min``),
#: because the compared quantity is a **span in EUR**, not a per-kWh slope.
TERMINAL_STABILITY_EUR_PER_KWH = 1e-3

#: How many phase-aligned tail lengths the stability test compares (``L``, ``2L``, ``3L``).
EXTENSION_COUNT = 3

#: The synthetic profile's own period, in hours: past the published entries the tail repeats the
#: fetched window **by time of day**, so every extension increment must be a whole number of these
#: or ``L``, ``2L`` and ``3L`` end at different daily phases and the spans measure the profile.
PROFILE_PERIOD_HOURS = 24.0

#: Stable reason slugs for a plan that could not use the vector (``None`` means it did).
REASON_TAIL_UNAVAILABLE = "tail_unavailable"
REASON_FLOOR_BINDS = "floor_binds"
REASON_BASIS_UNAVAILABLE = "basis_unavailable"
REASON_BILLING_REACHABLE = "billing_state_reachable"
REASON_CONTRACT_UNPRICEABLE = "contract_unpriceable"
REASON_TAIL_TOO_LONG = "tail_too_long"
REASON_TAIL_UNSTABLE = "tail_unstable"
#: The plan's native slot does not divide the synthetic profile's 24 h period, so ``L``, ``2L`` and
#: ``3L`` cannot all end at the same daily phase and the stability spans would measure the profile.
REASON_TAIL_MISALIGNED = "tail_misaligned"
#: The fetched window carries no feed-in provenance and the tail's own published entries would be
#: priced differently by the two possible interpretations (the window's rate vs the derived rule), so
#: no pricing is known to be the contract's and the vector refuses rather than silently picking one.
REASON_TAIL_PRICING_AMBIGUOUS = "tail_pricing_ambiguous"
#: The fetched window carries no feed-in provenance and its own priced slots fit **neither** supported
#: model (a variable explicit tariff, or garbage), so no reading of it can be justified at all.
REASON_TAIL_PRICING_UNSUPPORTED = "tail_pricing_unsupported"

_EPS = 1e-9


@dataclass(frozen=True)
class BasisEntry:
    """One battery's cost-basis facts, as the ledger knows them (pure data, no store access)."""

    basis_eur_per_kwh: float | None
    round_trip_efficiency: float
    wear_eur_per_kwh: float


@dataclass(frozen=True)
class TerminalRequest:
    """The engine's cost-basis facts for the sufficiency check (the DP never reads the ledger)."""

    margin_eur_per_kwh: float = 0.0
    basis: tuple[BasisEntry, ...] = ()


@dataclass(frozen=True)
class TerminalPlan:
    """A prepared, priced tail and the sufficiency verdict (docs/OPTIMIZER_V2.md section 4).

    ``slots`` covers ``EXTENSION_COUNT * L``: the stability test needs ``L``, ``2L`` and ``3L``, and
    one materialisation serves all three.  ``periods`` and ``period_slots`` describe the phase
    alignment (``L = periods * period_slots``), and ``minutes`` is the per-slot modelled duration.
    """

    ok: bool
    reason: str | None
    detail: dict[str, Any]
    slots: tuple[Any, ...] = ()
    minutes: tuple[float, ...] = ()
    tail_hours: float = 0.0
    periods: int = 1
    period_slots: int = 1
    salvage_eur_per_kwh: float = 0.0


def tail_slots(
    price_tail: Sequence[PriceSlot],
    window_slots: Sequence[Any],
    *,
    start: Any,
    hours: float,
    slot_minutes: float,
    derive_sell: Callable[[float, float], float] | None = None,
) -> list[Any]:
    """Materialise the tail as ``SlotInput``s: prices from the feed, pv/load by time-of-day.

    The plan horizon and the fetched window are the same window, so the tail takes the fetcher's
    ``PriceForecast.tail_slots`` (real provider entries plus its periodic fill) where they exist,
    starting at ``start`` (the plan horizon's end), and past the fetcher's own end repeats the
    fetched window by time-of-day -- the same rule the fetcher used, so the two agree where they
    overlap.  pv and load always come from the fetched window by time-of-day (docs/OPTIMIZER_V2.md
    section 4), and the *same* ``SlotInput`` objects feed the tail solve and the sufficiency checks,
    so ``base = load - pv`` cannot disagree between them.

    ``derive_sell`` is the window's own derived feed-in rule (``price.derived_sell_rule``).  A
    **published** entry carries its own buy price, so it must carry its own derived sell too:
    reusing the time-of-day reference's sell would price a 0.40 EUR/kWh slot with the 0.20 slot's
    sell price, and on a derived (``sell = buy - fee``) contract that pair does not exist.  Slots
    filled from the window keep the reference's sell, and a wired contract's settlement replaces
    every sell anyway (:func:`_priced_tail`), so the explicit/tiered paths are untouched.  When the
    fetched window carries no feed-in provenance the choice between the two rules is itself unknown
    (:func:`sources.price.tail_sell_pricing`), and :func:`plan_terminal` materialises the tail both
    ways and refuses if the two disagree on an entry it is pricing.

    ``slot_minutes`` is the plan's (native) resolution: this returns the **native** tail, which is
    what the sufficiency checks and the contract settlement see.  The solve runs on
    :func:`aggregate_tail` of it.
    """
    from . import dp

    if hours <= 0.0 or not window_slots:
        return []
    step = timedelta(minutes=max(1.0, float(slot_minutes)))
    count = int(round(hours / (step.total_seconds() / 3600.0)))
    if count <= 0:
        return []
    profile: dict[tuple[int, int], Any] = {}
    for slot in window_slots:
        profile.setdefault((slot.t.hour, slot.t.minute), slot)
    published = {slot.t: slot for slot in price_tail}
    out: list[Any] = []
    for index in range(count):
        moment = start + step * index
        reference = profile.get((moment.hour, moment.minute), window_slots[-1])
        price = published.get(moment)
        if price is not None:
            buy, energy_tax, stale = float(price.buy), price.energy_tax, bool(price.stale)
        else:
            buy, energy_tax, stale = float(reference.buy), reference.energy_tax, True
        if price is not None and derive_sell is not None:
            sell = float(derive_sell(buy, float(energy_tax or 0.0)))
        else:
            sell = float(reference.sell or 0.0)
        out.append(
            dp.SlotInput(
                t=moment,
                pv_kw=float(reference.pv_kw),
                load_kw=float(reference.load_kw),
                buy=buy,
                sell=sell,
                load_p10_kw=reference.load_p10_kw,
                load_p90_kw=reference.load_p90_kw,
                price_stale=stale,
                pv_source="periodic",
                price_source="tail" if price is not None else "periodic",
                energy_tax=energy_tax,
            )
        )
    return out


def _sell_interpretation_conflict(
    *, derived: Sequence[Any], window_rate: Sequence[Any]
) -> dict[str, Any] | None:
    """The first tail sell the two pricing interpretations disagree on, or ``None`` when they agree.

    Both sequences are :func:`tail_slots` output for the same tail, ``derived`` priced with the
    window's derived rule and ``window_rate`` with its own clock-time rate.  Only published entries
    can differ (a window-filled slot reuses the reference's sell either way), but every pair is
    compared.  A non-finite rate on either side is a disagreement, never a silent pick, so the check
    fails closed; a non-numeric one never reaches here (the materialisation itself rejects it).
    """
    first: dict[str, Any] | None = None
    differing = 0
    for index, (left, right) in enumerate(zip(derived, window_rate)):
        derived_rate = _finite(getattr(left, "sell", None))
        window_rate_value = _finite(getattr(right, "sell", None))
        if (
            derived_rate is not None
            and window_rate_value is not None
            and abs(derived_rate - window_rate_value) <= _EPS
        ):
            continue
        differing += 1
        if first is None:
            moment = getattr(left, "t", None)
            first = {
                "slot": index,
                "t": moment.isoformat() if hasattr(moment, "isoformat") else None,
                "price_source": getattr(left, "price_source", None),
                "window_rate_eur_per_kwh": window_rate_value,
                "derived_eur_per_kwh": derived_rate,
            }
    if first is None:
        return None
    first["count"] = differing
    first["of_slots"] = len(derived)
    return first


def aggregate_tail(
    slots: Sequence[Any],
    *,
    native_slots_per_period: int,
    ratio: int,
    native_minutes: float,
) -> tuple[list[Any], list[float]]:
    """Fold a native tail into ``ratio``-sized slots, restarting at every period boundary.

    Restarting at the period boundary is what keeps the phase alignment exact when the window's
    native length is not a multiple of ``ratio``: the last bucket of such a period is shorter, and
    its own duration travels in the returned ``minutes`` (in **minutes**, the DP's own unit), so the
    duration pattern repeats with the period and ``sum(minutes)`` is still exactly the tail's span.
    ``ratio <= 1`` is the identity (the tail stays native), which is what every pre-resolution
    caller gets.
    """
    if ratio <= 1 or not slots:
        return list(slots), [float(native_minutes) for _ in slots]
    span = max(1, int(native_slots_per_period))
    out: list[Any] = []
    minutes: list[float] = []
    for period_start in range(0, len(slots), span):
        block = slots[period_start : period_start + span]
        for index in range(0, len(block), ratio):
            group = block[index : index + ratio]
            out.append(_mean_slot(group))
            minutes.append(len(group) * float(native_minutes))
    return out, minutes


def _mean_slot(group: Sequence[Any]) -> Any:
    """One coarse tail slot: the sub-slots' means with their summed duration.

    A slot's cost is linear in ``buy``/``sell`` and its energy is linear in ``pv_kw``/``load_kw``,
    so the mean of the sub-slots with the summed ``dt`` preserves both quantities over the coarse
    slot.  An optional field that is unknown (``None``) in any sub-slot stays unknown -- a mean
    over the known ones would fabricate a value the recording never had.
    """
    from . import dp

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    def optional(name: str) -> float | None:
        values = [getattr(slot, name, None) for slot in group]
        if any(value is None for value in values):
            return None
        return mean([float(value) for value in values])

    taxes = [getattr(slot, "energy_tax", None) for slot in group]
    price_sources = {str(getattr(slot, "price_source", "")) for slot in group}
    pv_sources = {str(getattr(slot, "pv_source", "")) for slot in group}
    return dp.SlotInput(
        t=group[0].t,
        pv_kw=mean([float(slot.pv_kw) for slot in group]),
        load_kw=mean([float(slot.load_kw) for slot in group]),
        buy=mean([float(slot.buy) for slot in group]),
        sell=mean([float(slot.sell or 0.0) for slot in group]),
        load_p10_kw=optional("load_p10_kw"),
        load_p90_kw=optional("load_p90_kw"),
        price_stale=any(bool(getattr(slot, "price_stale", False)) for slot in group),
        pv_source=pv_sources.pop() if len(pv_sources) == 1 else "mixed",
        price_source=price_sources.pop() if len(price_sources) == 1 else "mixed",
        energy_tax=None if any(value is None for value in taxes) else mean(taxes),
    )


def plan_terminal(
    *,
    options: Any,
    aggregate: Any,
    window_slots: Sequence[Any],
    price_tail: Sequence[PriceSlot],
    start: Any,
    request: TerminalRequest | None,
    feed_in: FeedInRequest | None,
    main_max_export_kwh: float | None,
    slot_minutes: float,
    main_max_import_kwh: float | None = None,
    sell_pricing: Any | None = None,
) -> TerminalPlan:
    """Build the priced ``3L`` tail and decide whether the vector may replace ``salvage``.

    ``L`` is a whole number of **synthetic-profile periods** (24 h, rounded up from the requested
    ``optimizer.terminal_tail_hours``): the profile repeats by time of day, so comparing two tail
    lengths that start at a different daily phase would measure the price profile, not the tail's
    stability.  ``main_max_export_kwh``/``main_max_import_kwh`` are the main horizon's reachable
    export/import bounds (section 2.3): the billing state at the tail's start is reachable from
    ``sell_pricing`` is the window's own pricing decision (:func:`sources.price.tail_sell_pricing`):
    its ``rule`` prices the tail's published entries (``None`` keeps the window's clock-time rate), its
    ``ambiguous`` flag marks a window whose model provenance cannot identify -- the tail is then priced
    under both supported readings and the plan refuses with ``tail_pricing_ambiguous`` if they disagree
    on an entry it is pricing -- and its ``unsupported`` flag marks a window that fits neither model,
    which refuses with ``tail_pricing_unsupported`` before any tail is built.  Both refusals are scoped
    to the case where no wired settlement prices the tail: a settlement replaces every tail sell from
    the contract itself, so the materialised rate is not the plan's contract and neither applies.
    ``None`` means the caller declared the rule itself and no provenance check applies.

    The checks (contract settlement, floor, billing state) run on the **native** tail;
    ``slots``/``minutes`` returned here are the **tail resolution**
    (``optimizer.terminal_tail_minutes``) the solve and the stability test use.
    """
    hours = max(0.0, float(getattr(options, "terminal_tail_hours", 24.0) or 0.0))
    native_minutes = max(1.0, float(slot_minutes))
    # R2: the whole-period alignment is only sound when the native slot divides the profile period:
    # a 50-minute slot gives 28.8 slots per day, so no integer number of extensions can end at the
    # same daily phase and the periodic profile would drift against the compared lengths.  Fail
    # closed (a declared-resolution refusal) instead of silently misaligning.
    slots_per_period = PROFILE_PERIOD_HOURS * 60.0 / native_minutes
    if abs(slots_per_period - round(slots_per_period)) > _EPS * max(1.0, slots_per_period):
        return TerminalPlan(
            False,
            REASON_TAIL_MISALIGNED,
            {
                "check_minutes": native_minutes,
                "profile_hours": PROFILE_PERIOD_HOURS,
                "profile_slots": slots_per_period,
                "tail_misaligned": (
                    f"a {native_minutes:g}-minute plan slot does not divide the "
                    f"{PROFILE_PERIOD_HOURS:g} h profile period ({slots_per_period:.6g} slots)"
                ),
            },
            (),
            (),
            0.0,
            1,
            1,
            0.0,
        )
    requested = _finite(getattr(options, "terminal_tail_minutes", None))
    tail_minutes = max(native_minutes, float(requested) if requested else native_minutes)
    ratio = max(1, int(round(tail_minutes / native_minutes)))
    # The increment must be whole profile periods (never a fraction of one), so the requested length
    # is rounded *up*: a shorter tail could omit the spike it was asked to model.  The same period
    # drives the coarse bucket restart below, so ``unit`` coarse slots cover exactly
    # ``periods * profile_slots_native`` native slots and the increment is phase-aligned at both
    # resolutions.
    profile_slots_native = max(1, int(round(PROFILE_PERIOD_HOURS * 60.0 / native_minutes)))
    periods = max(1, int(math.ceil(hours / PROFILE_PERIOD_HOURS - _EPS))) if hours > 0.0 else 1
    tail_hours = periods * PROFILE_PERIOD_HOURS
    period_slots = max(1, -(-profile_slots_native // ratio))
    unit = periods * period_slots
    detail: dict[str, Any] = {
        # ``tail_slots`` is the *solved* tail, which is the coarse one; it is filled in after
        # aggregation, and stays 0 when there is no tail at all.  The native count is reported
        # separately, so the ratio's own effect is visible rather than inferred.
        "tail_slots": 0,
        "unit_slots": unit,
        "periods": periods,
        "period_slots": period_slots,
        "profile_hours": PROFILE_PERIOD_HOURS,
        "tail_hours_requested": hours,
        "tail_minutes": native_minutes * ratio,
        "tail_ratio": ratio,
        "native_tail_slots": 0,
        "check_minutes": native_minutes,
    }
    derive_sell = None if sell_pricing is None else getattr(sell_pricing, "rule", None)
    ambiguous = bool(sell_pricing is not None and getattr(sell_pricing, "ambiguous", False))
    # R5/C: both pricing refusals apply only while the materialised rates *are* the plan's contract.
    # A wired settlement prices the entire tail from the contract itself (``_priced_tail`` below), so
    # refusing here would disable the vector for the very escape hatch the design provides.
    settled = feed_in is not None
    if bool(sell_pricing is not None and getattr(sell_pricing, "unsupported", False)) and not settled:
        # R4: the window's own sells fit neither supported model (a variable explicit tariff, or
        # garbage), so no reading of it can be justified -- and clock-time repetition does not
        # resolve a variable explicit tariff either.  Refuse before building any tail, rather than
        # falling back to one of the readings.
        detail["tail_pricing_unsupported"] = getattr(sell_pricing, "evidence", {})
        detail["sell_provenance"] = "the fetched window carries no feed-in provenance"
        return TerminalPlan(
            False,
            REASON_TAIL_PRICING_UNSUPPORTED,
            detail,
            (),
            (),
            0.0,
            periods,
            period_slots,
            0.0,
        )
    native = tail_slots(
        price_tail,
        window_slots,
        start=start,
        hours=tail_hours * EXTENSION_COUNT,
        slot_minutes=native_minutes,
        derive_sell=derive_sell,
    )
    detail["native_tail_slots"] = len(native)
    if not native:
        return TerminalPlan(
            False, REASON_TAIL_UNAVAILABLE, detail, (), (), 0.0, periods, period_slots, 0.0
        )

    priced, price_detail, price_reason = _priced_tail(
        native, feed_in, main_max_export_kwh, main_max_import_kwh, native_minutes
    )
    detail.update(price_detail)
    slots, minutes = aggregate_tail(
        priced,
        native_slots_per_period=profile_slots_native,
        ratio=ratio,
        native_minutes=native_minutes,
    )
    detail["tail_slots"] = len(slots)
    salvage = (
        float(getattr(options, "salvage_multiplier", 1.0))
        * _median_buy(slots[:unit])
        * float(aggregate.eta_discharge)
    )
    detail["salvage_eur_per_kwh"] = salvage
    if price_reason is not None:
        return TerminalPlan(
            False, price_reason, detail, tuple(slots), tuple(minutes), tail_hours, periods,
            period_slots, salvage,
        )

    if ambiguous and not settled:
        # Round-3 finding: with no provenance, matching observed prices cannot identify the
        # contract -- an explicit flat tariff can coincide with the derived price across the whole
        # fetched window (0.16 buys with an explicit 0.14 sell *are* ``buy - 0.02``).  Price the
        # tail under both interpretations and refuse when they disagree on the entries actually
        # being priced, instead of silently picking one.  With a wired settlement the contract's
        # own price replaces every tail sell (:func:`_priced_tail`), so the ambiguity cannot reach
        # the solve or the floor check and the materialised rate is not the plan's contract.
        alternative = tail_slots(
            price_tail,
            window_slots,
            start=start,
            hours=tail_hours * EXTENSION_COUNT,
            slot_minutes=native_minutes,
            derive_sell=None,
        )
        conflict: dict[str, Any] | None = _sell_interpretation_conflict(
            derived=native, window_rate=alternative
        )
        if conflict is not None:
            detail["tail_pricing_ambiguous"] = conflict
            detail["sell_provenance"] = "the fetched window carries no feed-in provenance"
            return TerminalPlan(
                False,
                REASON_TAIL_PRICING_AMBIGUOUS,
                detail,
                tuple(slots),
                tuple(minutes),
                tail_hours,
                periods,
                period_slots,
                salvage,
            )

    floor_ok, floor_detail, floor_reason = _floor_ok(priced, options, request, window_slots)
    detail.update(floor_detail)
    if not floor_ok:
        return TerminalPlan(
            False, floor_reason, detail, tuple(slots), tuple(minutes), tail_hours, periods,
            period_slots, salvage,
        )
    return TerminalPlan(
        True, None, detail, tuple(slots), tuple(minutes), tail_hours, periods, period_slots, salvage
    )


def continuation_row(
    grid: Sequence[float],
    slots: Sequence[Any],
    minutes: Sequence[float],
    aggregate: Any,
    options: Any,
    salvage: float,
) -> list[float]:
    """The tail's value row at ``t = 0`` over ``grid``, in the main DP's cost units."""
    from . import dp

    terminal_row = dp._salvage_terminal_row(grid, salvage, 0.0, None)
    value, _argmin, _iterations = dp._backward_sweep(
        grid, slots, minutes, aggregate, options, terminal_row, e_target=None
    )
    return list(value[0])


def stability_span(short: Sequence[float], long: Sequence[float]) -> float:
    """The offset-invariant span of ``V_long - V_short``, in EUR (not per kWh).

    ``max_e(V_long(e) - V_short(e)) - min_e(...)``: an additive constant on either row cancels, so
    the measured quantity is exactly the decision-relevant shape difference between two tail
    lengths.  A length mismatch or an empty row is ``inf`` (never "stable").
    """
    if not short or len(short) != len(long):
        return float("inf")
    diffs = [b - a for a, b in zip(short, long)]
    return max(diffs) - min(diffs)


def stability_tolerance_eur(aggregate: Any) -> float:
    """``TERMINAL_STABILITY_EUR_PER_KWH`` converted to the monetary tolerance actually compared."""
    span = float(getattr(aggregate, "e_max_kwh", 0.0)) - float(getattr(aggregate, "e_min_kwh", 0.0))
    return TERMINAL_STABILITY_EUR_PER_KWH * max(0.0, span)


def _median_buy(slots: Sequence[Any]) -> float:
    """Median buy price of the given slots; ``0.0`` when there are none."""
    values = sorted(float(slot.buy) for slot in slots)
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def _priced_tail(
    tail: Sequence[Any],
    feed_in: FeedInRequest | None,
    main_max_export_kwh: float | None,
    main_max_import_kwh: float | None,
    slot_minutes: float,
) -> tuple[list[Any], dict[str, Any], str | None]:
    """Price the tail under the contract, over the whole reachable billing-state set."""
    detail: dict[str, Any] = {}
    if feed_in is None:
        # No contract wiring: the horizon's own derived per-slot sell is exogenous, and
        # ``tail_slots`` already applied the window's own derivation to each published price.
        detail["contract"] = "none (derived per-slot price)"
        return list(tail), detail, None

    mode = str(feed_in.settings.get("price.feed_in_mode", "derived") or "derived")
    detail["contract"] = mode
    detail["max_export_main_kwh"] = main_max_export_kwh
    detail["max_import_main_kwh"] = main_max_import_kwh
    if mode in ("tiered", "saldering") and main_max_export_kwh is None:
        # Section 2.3: an unknown reachable-export bound means every threshold is reachable.
        detail["reachability"] = "the main horizon's reachable-export bound is unknown"
        return list(tail), detail, REASON_BILLING_REACHABLE

    base = FeedInLimits(
        pv_kw=[float(slot.pv_kw) for slot in tail],
        load_kw=[float(slot.load_kw) for slot in tail],
        max_discharge_kw=feed_in.max_discharge_kw,
        export_limit_kw=feed_in.export_limit_kw,
        slot_hours=max(1.0, float(slot_minutes)) / 60.0,
        explicit_price_eur_per_kwh=feed_in.explicit_price_eur_per_kwh,
        tiers=feed_in.tiers,
    )
    candidates, reach_detail, reach_reason = _reachable_billing_states(
        feed_in,
        main_max_export_kwh,
        main_max_import_kwh,
        mode=mode,
        # The allowance threshold the settlement branches on: the tail's own reachable export.
        tail_max_export=max_reachable_export_kwh(len(tail), base),
    )
    detail.update(reach_detail)
    if reach_reason is not None:
        return list(tail), detail, reach_reason
    detail["billing_candidates"] = len(candidates)
    if not candidates:
        detail["reachability"] = "no reachable billing state could be enumerated"
        return list(tail), detail, REASON_BILLING_REACHABLE

    sells_seen: list[float] | None = None
    max_export_tail: float | None = None
    for state, volume in candidates:
        limits = replace(base, tier_volume_kwh=volume)
        try:
            result = settle_feed_in(feed_in.settings, tail, state, limits)
        except ValueError as exc:
            detail["contract_error"] = str(exc)[:200]
            return list(tail), detail, REASON_CONTRACT_UNPRICEABLE
        if not result.export_allowed:
            detail["contract_reason"] = result.reason
            detail["billing_state"] = {
                "volume_kwh": volume,
                "reachable": "a reachable billing state refuses the battery export leg",
            }
            return list(tail), detail, REASON_BILLING_REACHABLE
        if max_export_tail is None or (
            result.max_export_kwh is not None and result.max_export_kwh < max_export_tail
        ):
            max_export_tail = result.max_export_kwh
        # R6/C: the settlement carve-out is sound only while the settlement really prices *every*
        # tail slot -- a short or non-finite answer must refuse the vector, never leave one slot on
        # the materialised (guessed) rate for the solve or the floor check to read.  ``settle_feed_in``
        # is exact-or-refuse by contract, but this boundary must not depend on a convention it cannot
        # enforce.
        sells: list[float] = []
        for index, value in enumerate(result.sells):
            price = _finite(value)
            if price is None:
                detail["contract_error"] = (
                    f"the settlement left tail slot {index} unpriced ({str(value)[:32]}); the vector "
                    "needs a finite price for every tail slot"
                )
                return list(tail), detail, REASON_CONTRACT_UNPRICEABLE
            sells.append(price)
        if len(sells) != len(tail):
            detail["contract_error"] = (
                f"the settlement priced {len(sells)} of {len(tail)} tail slots; the vector needs a "
                "price for every tail slot"
            )
            return list(tail), detail, REASON_CONTRACT_UNPRICEABLE
        if sells_seen is None:
            sells_seen = sells
        elif any(abs(a - b) > _EPS for a, b in zip(sells_seen, sells)):
            # The tail's rewards depend on the omitted billing state: not a function of stored
            # energy alone, so no continuation row can be right for every reachable path.
            detail["billing_state"] = {
                "volume_kwh": volume,
                "reachable": "the priced tail differs across reachable billing states",
            }
            return list(tail), detail, REASON_BILLING_REACHABLE

    detail["max_export_tail_kwh"] = max_export_tail
    priced = [replace(slot, sell=sells_seen[i]) for i, slot in enumerate(tail)]
    return priced, detail, None


def _reachable_billing_states(
    feed_in: FeedInRequest,
    max_export_main: float | None,
    max_import_main: float | None,
    *,
    mode: str,
    tail_max_export: float | None,
) -> tuple[tuple[tuple[PeriodState | None, float | None], ...], dict[str, Any], str | None]:
    """Candidate billing states spanning the reachable horizon-end set (section 2.3).

    The settlement is piecewise in the period state: ``saldering`` branches on the allowance
    ``import - export`` (netting versus refusal or a post-saldering rate), ``tiered`` on the
    cumulative export volume crossing a tier bound.  **Both** period totals are reachable inputs --
    a household that is net-exporting now can import during the horizon and *restore* the allowance
    the tail's pricing depends on -- so the candidates span the reachable interval of the quantity
    the settlement branches on: **both endpoints, plus every threshold strictly inside it** (the
    tail's own reachable export for ``saldering``, every tier bound for ``tiered``).  Every
    candidate is a period state that really is reachable: imports and exports only grow over the
    horizon, so the allowance interval is
    ``[import_now - (export_now + max_export), (import_now + max_import) - export_now]`` and any
    point inside it is realised by moving whichever side the interval needs.

    ``tiered``'s volume is a cumulative *export* total, so its interval is bounded by the export
    side alone; ``saldering``'s allowance needs **both** bounds, and an unknown import bound is
    refused rather than sampled one-sided.  A non-``None`` reason means the interval cannot be
    bounded for this contract: the caller must refuse the vector instead of guessing an endpoint.
    """
    state = feed_in.period_state
    volume = _finite(feed_in.tier_volume_kwh)
    if volume is None and state is not None:
        volume = _finite(getattr(state, "export_kwh", None))
    if mode not in ("saldering", "tiered"):
        # No supported mode branches on the period state: one candidate keeps the settlement call
        # exactly as it is (and never refuses a mode that does not need the state).
        return ((state, volume),), {}, None

    if mode == "tiered":
        if volume is None or volume < 0.0 or max_export_main is None:
            # The settlement refuses an unavailable volume itself; one candidate carries that.
            return ((state, None),), {}, None
        top = volume + max(0.0, float(max_export_main))
        bounds = [
            float(bound)
            for bound, _rate in feed_in.tiers or ()
            if _finite(bound) is not None
        ]
        samples = _interval_samples(volume, top, bounds)
        detail = {
            "volume_lo_kwh": volume,
            "volume_hi_kwh": top,
            "thresholds_kwh": [value for value in sorted(bounds) if volume < value < top],
        }
        if state is None:
            return tuple((None, value) for value in samples), detail, None
        return (
            tuple(
                (PeriodState(import_kwh=state.import_kwh, export_kwh=value), value)
                for value in samples
            ),
            detail,
            None,
        )

    # saldering: the allowance ``import - export``, reachable from both sides.
    if state is None:
        # The module's documented assume-or-refuse path (no state to propagate): one candidate.
        return ((None, volume),), {}, None
    import_now = _finite(getattr(state, "import_kwh", None))
    export_now = _finite(getattr(state, "export_kwh", None))
    if import_now is None or export_now is None or import_now < 0.0 or export_now < 0.0:
        # The settlement refuses this state itself; one candidate carries that refusal through.
        return ((state, volume),), {}, None
    if max_export_main is None or max_import_main is None:
        return (
            (),
            {
                "reachability": (
                    "the main horizon's reachable-import bound is unknown"
                    if max_import_main is None
                    else "the main horizon's reachable-export bound is unknown"
                )
            },
            REASON_BILLING_REACHABLE,
        )
    allowance_now = import_now - export_now
    low = allowance_now - max(0.0, float(max_export_main))
    high = allowance_now + max(0.0, float(max_import_main))
    thresholds = [] if tail_max_export is None else [float(tail_max_export)]
    samples = _interval_samples(low, high, thresholds)
    detail = {
        "allowance_lo_kwh": low,
        "allowance_hi_kwh": high,
        "thresholds_kwh": [value for value in sorted(thresholds) if low < value < high],
    }
    out: list[tuple[PeriodState | None, float | None]] = []
    for value in samples:
        delta = value - allowance_now
        if delta >= 0.0:
            # Restore the allowance by importing more during the horizon (the household becomes
            # less net-exporting); the export total stays where it is.
            out.append(
                (PeriodState(import_kwh=import_now + delta, export_kwh=export_now), export_now)
            )
        else:
            # Draw the allowance down by exporting more; the import total stays where it is.
            out.append(
                (
                    PeriodState(import_kwh=import_now, export_kwh=export_now - delta),
                    export_now - delta,
                )
            )
    return tuple(out), detail, None


def _interval_samples(low: float, high: float, thresholds: Sequence[float]) -> list[float]:
    """The interval's endpoints plus every threshold strictly inside it, ascending.

    A threshold within ``_EPS`` of an endpoint *is* that endpoint: sampling it twice would settle
    the same state twice without adding information.  Ascending order makes the scan deterministic
    and puts the strictest reachable state (the lowest allowance / volume) first.
    """
    samples = {float(low), float(high)}
    for value in thresholds:
        number = _finite(value)
        if number is not None and float(low) + _EPS < number < float(high) - _EPS:
            samples.add(number)
    return sorted(samples)


def _floor_ok(
    tail: Sequence[Any],
    options: Any,
    request: TerminalRequest | None,
    window_slots: Sequence[Any],
) -> tuple[bool, dict[str, Any], str | None]:
    """Condition (a): the cost-basis floor is disabled or provably never binds over the tail.

    The bound is horizon-inclusive and running: the basis the tail inherits already contains every
    charge price of the **main window**, and each tail slot adds its own charge price to the bound
    only *after* it has been checked (a discharge, the leg the floor restricts, does not change the
    basis).

    The check runs on the **native** tail -- ``tail`` here is the priced native tail, and the
    reported ``slot`` is a native index -- because the floor binds per native slot: a coarse tail
    whose mean sale clears the bound must not hide a single native slot the sweep would refuse.

    Every battery in the fleet must be able to state its own bound (:func:`_floor_upper_bound`):
    an unprovable entry refuses here (``basis_unavailable``, with the offending indexes in
    ``basis_unprovable``), never silently drops out of the max.  That refusal stays distinguishable
    from a genuine floor refusal (``floor_binds``): one says the bound could not be established,
    the other that it was established and the tail crosses it.
    """
    detail: dict[str, Any] = {}
    floor = getattr(options, "sell_floor_eur_per_kwh", None)
    if floor is None:
        detail["floor"] = "disabled"
        return True, detail, None
    detail["floor_current_eur_per_kwh"] = float(floor)
    detail["floor_check_minutes"] = "native"
    if request is None or not request.basis:
        detail["floor"] = "the reachable basis bound is unavailable"
        return False, detail, REASON_BASIS_UNAVAILABLE
    window_buys = [float(slot.buy) for slot in window_slots]
    if not window_buys:
        detail["floor"] = "the main window's charge prices are unavailable"
        return False, detail, REASON_BASIS_UNAVAILABLE
    running = max(window_buys)
    detail["main_max_buy_eur_per_kwh"] = running
    bound_max: float | None = None
    for index, slot in enumerate(tail):
        bound = _floor_upper_bound(request, running)
        if bound is None:
            detail["floor"] = "the reachable basis bound is unavailable"
            detail["basis_unprovable"] = [
                position
                for position, entry in enumerate(request.basis)
                if not _entry_provable(entry)
            ]
            return False, detail, REASON_BASIS_UNAVAILABLE
        bound_max = bound if bound_max is None else max(bound_max, bound)
        sell = float(slot.sell or 0.0)
        base = float(slot.load_kw) - float(slot.pv_kw)
        if sell < bound:
            detail["floor_hi_eur_per_kwh"] = bound_max
            detail["floor_binds"] = {
                "slot": index,
                "leg": "export",
                "sell_eur_per_kwh": sell,
                "bound_eur_per_kwh": bound,
            }
            return False, detail, REASON_FLOOR_BINDS
        if base > 0.0 and float(slot.buy) < bound:
            detail["floor_hi_eur_per_kwh"] = bound_max
            detail["floor_binds"] = {
                "slot": index,
                "leg": "house",
                "buy_eur_per_kwh": float(slot.buy),
                "bound_eur_per_kwh": bound,
            }
            return False, detail, REASON_FLOOR_BINDS
        running = max(running, float(slot.buy))
    detail["floor_hi_eur_per_kwh"] = bound_max
    detail["floor"] = "provably never binds over the tail"
    return True, detail, None


def _entry_provable(entry: BasisEntry) -> bool:
    """Whether one battery can state its own floor bound: finite basis, positive finite rt, wear."""
    round_trip = _finite(entry.round_trip_efficiency)
    return (
        _finite(entry.basis_eur_per_kwh) is not None
        and round_trip is not None
        and round_trip > 0.0
        and _finite(entry.wear_eur_per_kwh) is not None
    )


def _floor_upper_bound(request: TerminalRequest, buy_hi: float) -> float | None:
    """``max_i(max(basis_i, buy_hi) / rt_i + wear_i + margin)``; ``None`` when unprovable.

    ``buy_hi`` is the running upper bound on the charge prices the basis can contain, so this is an
    upper bound on the *fleet* floor for every reachable path (a weighted mean is at most a max).

    **Every** battery has to state its own bound.  One with no basis cannot be skipped: its basis
    may already be a *past* charge price above every price in this window, or it may acquire one by
    charging during the tail, and either way it then enters the fleet mean with its own efficiency
    and wear -- so dropping it understates the fleet floor.  The engine's promise is to refuse a
    missing ledger fact rather than guess it, so any entry that cannot state its bound (missing or
    non-finite basis/wear, non-positive or non-finite round-trip efficiency, non-finite margin)
    makes the whole bound unprovable.
    """
    margin = _finite(request.margin_eur_per_kwh)
    if margin is None:
        return None
    best: float | None = None
    for entry in request.basis:
        if not _entry_provable(entry):
            return None
        basis = float(entry.basis_eur_per_kwh)
        round_trip = float(entry.round_trip_efficiency)
        floor = (
            max(basis, float(buy_hi)) / round_trip
            + float(entry.wear_eur_per_kwh)
            + margin
        )
        best = floor if best is None else max(best, floor)
    return best


def _finite(value: Any) -> float | None:
    """A finite float, else ``None`` (never a fabricated zero)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
