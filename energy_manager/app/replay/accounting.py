"""Per-arm accounting: the flows an arm really produced, priced at the recording's own rates.

docs/REPLAY_HARNESS.md section 4 and the five accepted corrections fix four rules here.

**Whose flow, whose price.**  A leg's energy is its *own*: the scratch ``actuals`` rows it wrote
during the replay.  The recording's own rows (``episode.boundary.source_rows['actuals']``) are never
used as an arm's flow -- they supply the settlement prices (correction 1: section 4 names
``actuals.price_buy``/``price_sell``), the recorded-reference integral for gate 1, and nothing else.
The replay's control cadence (30 s) is finer than a bucket (300 s), so every in-window bucket's
primary key is overwritten by the arm; :data:`BUCKET_NOT_VISITED` withholds a window where that did
not happen rather than letting a recorded row stand in for the arm.

**The integral (correction 2).**  A stored row's flow is carried until the *next stored row's* own
timestamp -- never an assumed uniform bucket.  A row's value describes its own declared sampling
interval; the part of a longer gap beyond that interval has no flow to integrate, so it is reported
(:attr:`WindowAccount.gaps`) and withholds every window it touches.

**Prices (correction 1).**  Per column and per bucket: **absent** falls back to the recorded plan
slot *in force at that instant* (reported as ``plan_slot_fallback``; a slot is only ever the one
containing the instant, never a later one), **present and NULL** refuses the window (``price_null``),
and a value is used as-is.  A window with any unpriced segment has ``cost_eur``/``revenue_eur``/
``profit_eur`` ``None`` -- a partial sum is not a price.  The arm's own booked prices (its scratch
rows, which for E2 are its deliberately wrong tariff) are reported as a divergence, never settled on.

**Plant ceilings (correction 3).**  A window whose :class:`~app.replay.runner.WindowTally` exceeds a
manifest ceiling (``energy_unknown_ticks``, ``infeasible_commands``, and ``unverified_bounds`` where
the plant mode declares one) is **withheld**: :meth:`paired_delta` drops its delta-profit and its
bootstrap sample, because a window whose plant could not be simulated must not carry a profit
comparison.

**Terminal inventory.**  ``terminal_adjustment = (E_end - E_start) x valuation`` with the fleet's
window-start cost basis as the headline (one declared price, identical in every arm) and the
per-pack booked basis as the sensitivity.  The headline basis is *declared* before any leg runs
(:func:`window_start_valuation`): a pack whose ``battery_cost_basis`` row the recording already had
at the window start uses that row, and a pack without one has its basis derived by the ledger's own
``update_basis`` against the truncated scratch copy reading rows up to the window start only -- the
same call the engine's first tick makes, so the two agree by construction instead of by assumption.
The declaration carries per-pack provenance (``frozen_profile_row`` / ``derived_as_of_window_start``)
and only a pack that neither source can value is left without one.  The pack energy is DC pack kWh
valued at the ledger's booked per-kWh basis -- the only valuation the engine's ledger exposes -- and
the profit is reported with and without the term.  ``savings_daily`` is never combined with the
terminal term (double counting).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..cost_basis import PROFILE_KIND as COST_BASIS_KIND
from ..cost_basis import load_basis as load_cost_basis
from ..cost_basis import update_basis as update_cost_basis
from ..engine import ACTUALS_MINUTES
from ..models import Plan, PriceSlot, from_iso, quarter_floor
from ..sources.feed_in import settle_feed_in
from .episode import Episode
from .manifest import VALUATION, PlantCeiling, PlantLimits
from .runner import LegResult, WindowBoundary, WindowTally

#: The declared sampling interval of a stored ``actuals`` row, in seconds (production's bucket).
DECLARED_SAMPLING_S = float(ACTUALS_MINUTES) * 60.0

#: A segment longer than the declared interval is a gap: the part of it beyond the row's own
#: bucket has no flow to integrate (correction 2).
GAP_EPSILON_S = 1e-6

RECORDED_ACTUALS = "recorded_actuals"
PLAN_SLOT_FALLBACK = "plan_slot_fallback"
NO_PRICE = "no_price"

#: Withholding / note slugs; stable and machine-readable.
PRICE_NULL = "price_null"
NO_PRICE_VINTAGE = "no_price_vintage"
ENERGY_GAP = "energy_gap"
FLOW_NULL = "flow_null"
PLANT_CEILING = "plant_ceiling"
BUCKET_NOT_VISITED = "bucket_not_visited"
#: A recorded bucket has no row at all while its price column exists in the ``actuals`` schema.
#: That is a missing ROW, never a missing COLUMN: a column-less recording falls back to the
#: recorded plan slot, a row-less one refuses rather than pricing itself from the plan.
ROW_ABSENT = "recorded_row_absent"
#: The arm's own stored rows do not cover the whole window: time inside it has no flow to
#: integrate, and that is reported instead of resting on the runner's buckets_written claim.
COVERAGE_GAP = "window_coverage_gap"
#: The runner produced no plant tally for a window, so no ceiling could be evaluated for it.
#: A window without a tally is withheld, never allowed to pass every ceiling by default.
PLANT_TALLY_MISSING = "plant_tally_missing"
#: The leg's own ledger booked no basis for a pack with a non-zero energy delta, so the declared
#: ``ledger_booked`` sensitivity valuation cannot be measured for it.
NO_LEDGER_BASIS = "no_ledger_basis"
#: A window whose recorded feed-in price the assumed contract model cannot reproduce: its money is
#: withheld, because revenue that rests on an assumed tariff is not the recording's revenue.
EXPORT_REVENUE_UNVERIFIED = "export_revenue_unverified"
#: The comparand gate 3 names for that check, mirroring ``basis_comparand``: the recording's own
#: booked sell price (its ``actuals.price_sell``, or the recorded plan slot where the column is
#: absent -- the same provenance rule the settlement itself uses).
#: What gate 3's tariff term actually compares.  The recording's ``actuals.price_sell`` is very
#: likely *written* by production from the same rule this check applies to it, so a match proves the
#: recording is internally consistent with the assumed model -- never that the household was paid
#: that price.  Verification against an external invoice is out of scope.
RECORDING_SELF_CONSISTENCY = "recording_self_consistency"
#: Every comparable segment was priced from the recorded plan slot, which production derived with the
#: same rule: the fallback rule declares these segments unverifiable rather than pretending the
#: assumed model was checked.  Its own slug, so this is never read as "the model refused".
TARIFF_FALLBACK_ONLY = "tariff_unverifiable_fallback_only"
TARIFF_MODEL_DERIVED = "derived_buy_minus_fee_minus_energy_tax"
TARIFF_MODEL_SETTLED = "feed_in_settlement"
TARIFF_MODEL_DECLARED = "declared_by_construction"
TARIFF_MODEL_UNAVAILABLE = "unavailable"
NO_VALUATION = "no_valuation"
TERMINAL_UNKNOWN = "terminal_energy_unknown"
WINDOW_MISMATCH = "window_mismatch"


class AccountingError(RuntimeError):
    """Accounting could not run at all (as opposed to a withheld window)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class Gap:
    """A stretch of a window with no stored row, where a flow cannot be integrated."""

    bucket: datetime
    covered_until: datetime
    next_bucket: datetime
    gap_s: float


@dataclass(frozen=True)
class Segment:
    """One stored row's flow carried until the next stored row (clipped to a window)."""

    bucket: datetime
    start: datetime
    end: datetime
    grid_kw: float
    import_kwh: float
    export_kwh: float
    buy_eur_per_kwh: float | None
    sell_eur_per_kwh: float | None
    buy_source: str
    sell_source: str
    arm_booked_buy: float | None
    arm_booked_sell: float | None

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class WindowAccount:
    """One recorded decision window of one arm: its flows, its prices, its plant, its verdict."""

    index: int
    start: datetime
    end: datetime
    segments: tuple[Segment, ...]
    import_kwh: float | None
    export_kwh: float | None
    cost_eur: float | None
    revenue_eur: float | None
    profit_eur: float | None
    reference_import_kwh: float | None
    reference_export_kwh: float | None
    divergence_kwh: float | None
    #: Where this window's measured pv/load base came from: measured | derived | mixed | refused |
    #: absent (``Episode.base.window``).  One label per window, not a range average: it is the
    #: provenance of the covering bucket at the window *start*, the base every tick of the window
    #: actually used (a bucket's base is held until the next bucket begins) -- ``mixed`` therefore
    #: means that one bucket's pv/load pair was half measured and half derived -- while
    #: ``Episode.base.split`` counts the buckets whose own start falls inside the window.  A modelled
    #: base must never read as a measured one.
    pv_load_source: str
    price_fallbacks: int
    tariff_divergence_segments: int
    gaps: tuple[Gap, ...]
    plant: WindowTally
    ceiling_tripped: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def withheld(self) -> bool:
        return bool(self.reasons)


@dataclass(frozen=True)
class TariffCheck:
    """The recording's booked feed-in price against the contract model the run assumes for it.

    Revenue may not rest on an assumed tariff: the arm's settlement prices come from the recording's
    own columns, so those columns must agree with the model this run assumes for the old side.
    ``comparand`` names what was compared (mirroring gate 3's ``basis_comparand``) and
    ``diverged_windows`` are the windows whose money is withheld for it.

    A match is **recording self-consistency**, not contract verification: production very likely
    writes ``actuals.price_sell`` from the same derived rule this check applies to it, so 26 of 26
    buckets matching says the recording's own columns agree with the assumed model -- it does not say
    the household was paid that price.  An external invoice is the only source for that and is out
    of scope.  ``reason`` names the specific refusal when the check could not run at all (e.g.
    ``tariff_unverifiable_fallback_only`` when every segment came from the plan-slot fallback).
    """

    model: str
    comparand: str
    tolerance_eur_per_kwh: float | None
    compared_segments: int
    worst_eur_per_kwh: float | None
    worst_window: int | None
    worst_instant: datetime | None
    worst_recorded_eur_per_kwh: float | None
    worst_model_eur_per_kwh: float | None
    diverged_windows: tuple[int, ...]
    note: str
    #: The specific reason the check could not run (empty when it did): a distinct slug says *why*,
    #: so a fallback-priced recording is not read as "the assumed model was unavailable".
    reason: str = ""
    #: Segments whose recorded price came from the plan slot rather than the recorded columns: the
    #: production planner derived that slot with the same rule, so comparing it with the rule would
    #: be self-fulfilling.  They are counted here and excluded from ``compared_segments``.
    fallback_segments: int = 0
    #: No tolerance was declared, so the recording's booked price was never judged against the
    #: model: a note saying it follows is not available in that case.
    unchecked: bool = False


def recorded_tariff_check(
    windows: Sequence[WindowAccount],
    *,
    settings: Mapping[str, Any],
    tolerance_eur_per_kwh: float | None,
    declared: bool = False,
) -> tuple[TariffCheck, dict[int, float]]:
    """Check the recording's booked sell price against the assumed contract, per window.

    Deliberately about the *recording's* own tariff columns: comparing a simulated arm's settled
    prices with its own plan would be a tautology by construction.  The model is the run's assumed
    old-side contract -- the live derived rule (buy minus the feed-in fee minus its energy-tax
    component) when the recording declares ``derived``, otherwise the configured mode settled by
    :func:`app.sources.feed_in.settle_feed_in`, the same API the planner uses under feed-in wiring.
    An arm that declares its own explicit rate, or a model that cannot price, is reported as a note:
    nothing is pretended to have been verified.
    """
    if declared:
        return (
            TariffCheck(
                model=TARIFF_MODEL_DECLARED,
                comparand=RECORDING_SELF_CONSISTENCY,
                tolerance_eur_per_kwh=tolerance_eur_per_kwh,
                compared_segments=0,
                worst_eur_per_kwh=None,
                worst_window=None,
                worst_instant=None,
                worst_recorded_eur_per_kwh=None,
                worst_model_eur_per_kwh=None,
                diverged_windows=(),
                note=(
                    "this leg declares its own feed-in contract, so the recording's booked price is "
                    "not this model's output: declared by construction, not self-verified"
                ),
            ),
            {},
        )
    # One row per UNIQUE recorded bucket.  A bucket can be clipped across two neighbouring
    # windows, and a settlement is fed one slot per bucket: comparing it twice would double-count
    # the same recorded price, and feeding duplicates would let ``settle_feed_in`` misalign the
    # returned prices against the rows (correction: feed unique buckets, then assert the lengths).
    fallback_segments = 0
    by_bucket: dict[datetime, dict[str, Any]] = {}
    for window in windows:
        for segment in window.segments:
            recorded = _finite(segment.sell_eur_per_kwh)
            buy = _finite(segment.buy_eur_per_kwh)
            if recorded is None or buy is None:
                continue
            if segment.sell_source == PLAN_SLOT_FALLBACK or segment.buy_source == PLAN_SLOT_FALLBACK:
                # The plan slot was derived by production with the same rule this check applies to
                # it, so it is not an independent observation: counted, never compared.
                fallback_segments += 1
                continue
            entry = by_bucket.get(segment.bucket)
            if entry is None:
                by_bucket[segment.bucket] = entry = {
                    "recorded": recorded,
                    "buy": buy,
                    "windows": [],
                }
            if window.index not in entry["windows"]:
                entry["windows"].append(window.index)
    unique = sorted(by_bucket.items())
    slots = [
        PriceSlot(
            t=bucket,
            buy=entry["buy"],
            sell=None,
            source="replay:recorded_side",
            is_actual=True,
        )
        for bucket, entry in unique
    ]
    rows: list[tuple[int, datetime, float, PriceSlot]] = [
        (entry["windows"][0], bucket, entry["recorded"], slot)
        for (bucket, entry), slot in zip(unique, slots)
    ]
    if not rows:
        return (
            TariffCheck(
                model=TARIFF_MODEL_UNAVAILABLE,
                comparand=RECORDING_SELF_CONSISTENCY,
                tolerance_eur_per_kwh=tolerance_eur_per_kwh,
                compared_segments=0,
                worst_eur_per_kwh=None,
                worst_window=None,
                worst_instant=None,
                worst_recorded_eur_per_kwh=None,
                worst_model_eur_per_kwh=None,
                diverged_windows=(),
                note=(
                    "no recorded segment carried both a buy and a booked sell price to check"
                    + (
                        f" ({fallback_segments} segment(s) settled on the recorded plan slot, "
                        f"which production derived with the same rule): {TARIFF_FALLBACK_ONLY} -- "
                        "the fallback rule is not independent evidence, so the tariff is withheld "
                        "as unverifiable rather than checked"
                        if fallback_segments
                        else ""
                    )
                ),
                reason=TARIFF_FALLBACK_ONLY if fallback_segments else "",
                fallback_segments=fallback_segments,
            ),
            {},
        )
    mode = str(settings.get("price.feed_in_mode", "derived") or "derived").strip() or "derived"
    derived = mode == "derived"
    model = TARIFF_MODEL_DERIVED if derived else TARIFF_MODEL_SETTLED
    assumed = (
        {**dict(settings), "price.feed_in_mode": "buy_minus_fee"} if derived else dict(settings)
    )
    refused = ""
    settlement = None
    try:
        settlement = settle_feed_in(assumed, slots, None, None)
    except Exception as exc:  # noqa: BLE001 - an unusable model is reported, never guessed at
        refused = f"{type(exc).__name__}: {exc}"
    if settlement is None or not settlement.export_allowed:
        detail = refused or str(settlement.reason or "") or "; ".join(settlement.notes)
        return (
            TariffCheck(
                model=TARIFF_MODEL_UNAVAILABLE,
                comparand=RECORDING_SELF_CONSISTENCY,
                tolerance_eur_per_kwh=tolerance_eur_per_kwh,
                compared_segments=0,
                worst_eur_per_kwh=None,
                worst_window=None,
                worst_instant=None,
                worst_recorded_eur_per_kwh=None,
                worst_model_eur_per_kwh=None,
                diverged_windows=(),
                note=(
                    f"the assumed contract ({model}) could not price the recording's own intervals: "
                    f"{detail}"
                ),
            ),
            {},
        )
    if len(settlement.sells) != len(slots):
        # A model that returns a different number of prices than slots cannot be aligned against
        # the rows: comparing positionally would be a guess, so the check is refused as unavailable.
        return (
            TariffCheck(
                model=TARIFF_MODEL_UNAVAILABLE,
                comparand=RECORDING_SELF_CONSISTENCY,
                tolerance_eur_per_kwh=tolerance_eur_per_kwh,
                compared_segments=0,
                worst_eur_per_kwh=None,
                worst_window=None,
                worst_instant=None,
                worst_recorded_eur_per_kwh=None,
                worst_model_eur_per_kwh=None,
                diverged_windows=(),
                note=(
                    f"the assumed contract ({model}) returned {len(settlement.sells)} price(s) for "
                    f"{len(slots)} recorded bucket(s): the check cannot be aligned, so nothing is "
                    "verified"
                ),
                fallback_segments=fallback_segments,
                unchecked=tolerance_eur_per_kwh is None,
            ),
            {},
        )
    worst: float | None = None
    worst_window: int | None = None
    worst_instant: datetime | None = None
    worst_recorded: float | None = None
    worst_model: float | None = None
    per_window: dict[int, float] = {}
    for (index, bucket, recorded, _slot), settled in zip(rows, settlement.sells):
        divergence = abs(recorded - float(settled))
        if worst is None or divergence > worst:
            worst, worst_window, worst_instant = divergence, index, bucket
            worst_recorded, worst_model = recorded, float(settled)
        for touched in by_bucket[bucket]["windows"]:
            if divergence > per_window.get(touched, -1.0):
                per_window[touched] = divergence
    diverged = tuple(
        sorted(
            index
            for index, divergence in per_window.items()
            if tolerance_eur_per_kwh is not None and divergence > float(tolerance_eur_per_kwh)
        )
    )
    if tolerance_eur_per_kwh is None:
        note = (
            f"no tolerance was declared, so the recording's booked feed-in price was not judged "
            f"against the assumed {model}: unchecked ({len(rows)} unique bucket(s))"
        )
    elif diverged:
        note = (
            f"{len(diverged)} of {len(per_window)} window(s) booked a feed-in price that does not "
            f"agree with the assumed {model} (worst {worst:.6f} EUR/kWh against a "
            f"{float(tolerance_eur_per_kwh):.6f} tolerance): their money is withheld"
        )
    else:
        note = (
            f"the recording's booked feed-in price is self-consistent with the assumed {model} "
            f"within {float(tolerance_eur_per_kwh):.6f} EUR/kWh ({len(rows)} independent "
            f"bucket(s) compared; {fallback_segments} plan-slot fallback segment(s) excluded) -- "
            "an internal check of the recording's own columns, not evidence the tariff was billed"
        )
    return (
        TariffCheck(
            model=model,
            comparand=RECORDING_SELF_CONSISTENCY,
            tolerance_eur_per_kwh=tolerance_eur_per_kwh,
            compared_segments=len(rows),
            worst_eur_per_kwh=worst,
            worst_window=worst_window,
            worst_instant=worst_instant,
            worst_recorded_eur_per_kwh=worst_recorded,
            worst_model_eur_per_kwh=worst_model,
            diverged_windows=diverged,
            note=note,
            fallback_segments=fallback_segments,
            unchecked=tolerance_eur_per_kwh is None,
        ),
        per_window,
    )


def _recording_settings(leg: LegResult) -> Mapping[str, Any]:
    """The recording's own frozen settings: the assumed old-side contract, never the arm's patch."""
    values = getattr(getattr(leg, "episode", None), "settings_values", None)
    return values if isinstance(values, Mapping) else {}


@dataclass(frozen=True)
class LegAccount:
    """One leg's whole window: per-window accounts plus the leg's terminal-inventory term."""

    name: str
    plant: str
    policy: str
    start: datetime
    end: datetime
    step_s: float
    declared_sampling_s: float
    windows: tuple[WindowAccount, ...]
    #: Leg totals are whole-window quantities or ``None``: the kWh totals are null unless every
    #: window was measured, and the money totals (and ``profit_eur``) are null unless every window
    #: was priced.  A sum over the windows that happened to be usable would read as the leg's own
    #: number, so the coverage counters below carry the partial information instead.
    import_kwh: float | None
    export_kwh: float | None
    cost_eur: float | None
    revenue_eur: float | None
    profit_eur: float | None
    windows_unknown: int
    withheld_windows: tuple[int, ...]
    #: How many windows each class of leg total covers, so a partial total is never read as the
    #: whole window: ``windows_measured`` is the set the kWh totals cover, ``windows_priced`` the set
    #: the money totals cover, and both must equal ``len(windows)`` before those totals are non-null.
    #: ``windows_unknown`` is the remainder of the measured set.
    windows_measured: int
    windows_priced: int
    initial_energy_kwh: float | None
    terminal_energy_kwh: float | None
    energy_delta_kwh: float | None
    booked_basis: tuple[tuple[str, float | None, float | None, Any], ...]
    valuation_eur_per_kwh: float | None
    terminal_adjustment_eur: float | None
    profit_with_terminal_eur: float | None
    sensitivity_valuation_eur_per_kwh: float | None
    sensitivity_terminal_adjustment_eur: float | None
    notes: tuple[tuple[str, str], ...]
    #: Where the declared window-start valuation came from (``None`` when the caller declared none):
    #: ``frozen_profile_row``, ``derived_as_of_window_start``, ``mixed_frozen_and_derived`` or
    #: ``no_window_start_basis``, with the packs each provenance covered.
    valuation_source: str | None = None
    valuation_frozen: tuple[str, ...] = ()
    valuation_derived: tuple[str, ...] = ()
    valuation_missing: tuple[str, ...] = ()
    #: The recording's booked feed-in price against the assumed contract model (``None`` when the
    #: caller asked for no check): a note saying the model could not be judged is never the same as
    #: one that matched.
    tariff_check: TariffCheck | None = None

    @property
    def reference_divergence_kwh(self) -> float | None:
        """Gate 1's plant-fidelity number: the arm's flows against the recording's own rows.

        ``None`` unless every window contributed a measured divergence: summing only the windows
        that happened to be measurable would report an unknown reference as perfect fidelity, and
        gate 1 would pass vacuously.  An unknown window is a hole, not a zero.
        """
        if not self.windows or any(window.divergence_kwh is None for window in self.windows):
            return None
        return math.fsum(
            window.divergence_kwh
            for window in self.windows
            if window.divergence_kwh is not None
        )


@dataclass(frozen=True)
class PairedWindow:
    """One window's paired comparison; ``withheld`` means neither a delta nor a sample."""

    index: int
    start: datetime
    end: datetime
    left_profit_eur: float | None
    right_profit_eur: float | None
    delta_eur: float | None
    withheld: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PairedDelta:
    """The paired per-window delta-profit of one arm, with withheld windows excluded."""

    change: str
    windows: tuple[PairedWindow, ...]
    #: ``None`` unless every window of the pair was included: a partial sum, or a sum over zero
    #: windows, is not the arm's delta and must never print as one.
    delta_eur: float | None
    delta_with_terminal_eur: float | None
    terminal_delta_eur: float | None
    withheld_windows: tuple[int, ...]
    included_windows: int

    @property
    def bootstrap_samples(self) -> tuple[float, ...]:
        """The per-window deltas that survived withholding: the block bootstrap's population."""
        return tuple(
            window.delta_eur for window in self.windows
            if not window.withheld and window.delta_eur is not None
        )


# ------------------------------------------------------------------------------ the integral


def _arm_rows(leg: LegResult) -> list[Mapping[str, Any]]:
    """The arm's own stored rows: ``[floor(start), end)`` only.

    The scratch copy also holds the recording's rows before the window start and after its end; the
    range cut keeps them out, and the first bucket (``floor(start)``) is included because the arm's
    first control tick lands in it and rewrites it.
    """
    first = quarter_floor(leg.start, ACTUALS_MINUTES)
    return leg.store.actuals_range(first, leg.end)


def _recorded_rows(leg: LegResult) -> dict[datetime, Mapping[str, Any]]:
    out: dict[datetime, Mapping[str, Any]] = {}
    for row in leg.episode.boundary.source_rows.get("actuals", ()):
        moment = from_iso(row.get("ts"))
        if moment is not None:
            out[moment] = row
    return out


#: The source schema could not be read, so a price column's presence cannot be decided: guessing
#: from the scratch copy (migrated when it is opened) could invent a column the recording never had.
SOURCE_SCHEMA_UNKNOWN = "source_schema_unknown"


def _recorded_columns(leg: LegResult) -> frozenset[str]:
    """The ``actuals`` table's own column names, from the **source** schema rather than from a row.

    A missing recorded row must never read as a missing recorded column: the schema says which
    columns the recording has, so an absent column falls back to the recorded plan slot (as
    correction 1 declares) while an absent row refuses.  The boundary rows carry the source
    table's columns; a window with no row at all asks the source's own schema, never the scratch
    copy's, which the store migrates on open and which may therefore hold columns the recording
    never had (a migrated ``price_sell`` would turn every old recording's fallback into a refusal).
    A source that cannot be read is refused too: no column may be invented or assumed absent.
    """
    rows = leg.episode.boundary.source_rows.get("actuals", ())
    if rows:
        return frozenset(str(key) for key in rows[0])
    columns = leg.episode.source_columns("actuals")
    if columns is None:
        raise AccountingError(
            SOURCE_SCHEMA_UNKNOWN,
            "the recording holds no actuals row in this window and its own schema cannot be read, "
            "so whether it booked price columns cannot be decided from the scratch copy (which "
            "was migrated on open)",
        )
    return columns


def _segments(
    rows: Sequence[Mapping[str, Any]],
    *,
    end: datetime,
    declared_sampling_s: float,
    flow: Callable[[Mapping[str, Any]], float | None],
    price: Callable[[datetime, datetime, Mapping[str, Any] | None], tuple[float | None, float | None, str, str]],
    booked: Callable[[Mapping[str, Any]], tuple[float | None, float | None]] | None = None,
    reference: Mapping[datetime, Mapping[str, Any]] | None = None,
) -> tuple[list[Segment], list[Gap], bool, int]:
    """Carry each row's flow to the next row, refusing to stretch a value past its own bucket.

    Returns the segments, the gaps, whether any flow value was missing, and the number of fallbacks.
    """
    segments: list[Segment] = []
    gaps: list[Gap] = []
    missing = False
    fallbacks = 0
    for index, row in enumerate(rows):
        bucket = from_iso(row.get("ts"))
        if bucket is None:
            continue
        following = from_iso(rows[index + 1].get("ts")) if index + 1 < len(rows) else None
        until = min(following or end, end)
        if following is not None and (following - bucket).total_seconds() > declared_sampling_s + GAP_EPSILON_S:
            gaps.append(
                Gap(
                    bucket=bucket,
                    covered_until=bucket + timedelta(seconds=declared_sampling_s),
                    next_bucket=following,
                    gap_s=(following - bucket).total_seconds(),
                )
            )
        if until <= bucket:
            continue
        # The value of a row whose successor is late still only describes its own bucket.
        if until > bucket + timedelta(seconds=declared_sampling_s):
            until = bucket + timedelta(seconds=declared_sampling_s)
        value = _finite(flow(row))
        if value is None:
            missing = True
            continue
        buy, sell, buy_source, sell_source = price(bucket, bucket, reference.get(bucket) if reference is not None else None)
        if buy_source == PLAN_SLOT_FALLBACK or sell_source == PLAN_SLOT_FALLBACK:
            fallbacks += 1
        hours = (until - bucket).total_seconds() / 3600.0
        booked_pair = booked(row) if booked else (None, None)
        segments.append(
            Segment(
                bucket=bucket,
                start=bucket,
                end=until,
                grid_kw=value,
                import_kwh=max(value, 0.0) * hours,
                export_kwh=max(-value, 0.0) * hours,
                buy_eur_per_kwh=buy,
                sell_eur_per_kwh=sell,
                buy_source=buy_source,
                sell_source=sell_source,
                arm_booked_buy=booked_pair[0],
                arm_booked_sell=booked_pair[1],
            )
        )
    return segments, gaps, missing, fallbacks


def _clip(segment: Segment, start: datetime, end: datetime) -> Segment | None:
    """The part of a segment inside one window (a uniform value splits exactly, not proportionally)."""
    left = max(segment.start, start)
    right = min(segment.end, end)
    if right <= left:
        return None
    hours = (right - left).total_seconds() / 3600.0
    return Segment(
        bucket=segment.bucket,
        start=left,
        end=right,
        grid_kw=segment.grid_kw,
        import_kwh=max(segment.grid_kw, 0.0) * hours,
        export_kwh=max(-segment.grid_kw, 0.0) * hours,
        buy_eur_per_kwh=segment.buy_eur_per_kwh,
        sell_eur_per_kwh=segment.sell_eur_per_kwh,
        buy_source=segment.buy_source,
        sell_source=segment.sell_source,
        arm_booked_buy=segment.arm_booked_buy,
        arm_booked_sell=segment.arm_booked_sell,
    )


# ---------------------------------------------------------------------------------- the prices


def _plan_in_force(plans: Sequence[Plan], moment: datetime) -> Plan | None:
    """The newest recorded plan issued at or before ``moment`` -- never a later vintage."""
    chosen: Plan | None = None
    for plan in plans:
        if plan.generated_at <= moment:
            chosen = plan
    return chosen


def _price_in(leg: LegResult, moment: datetime) -> tuple[float, float] | None:
    """The recorded plan slot containing ``moment`` (``None`` when no vintage covers it)."""
    plan = _plan_in_force(leg.episode.plans, moment)
    if plan is None:
        return None
    slot = plan.slot_at(moment)
    if slot is None:
        return None
    buy, sell = _finite(slot.buy), _finite(slot.sell)
    if buy is None or sell is None:
        return None
    return buy, sell


def _arm_price(leg: LegResult, columns: frozenset[str]) -> Callable[..., tuple[float | None, float | None, str, str]]:
    """Correction 1: absent -> the plan slot in force; present-but-NULL -> refuse the window.

    ``columns`` is the recorded table's own schema: a column the recording does not have falls back
    to the plan slot, a column it has but a row it does not have refuses (``recorded_row_absent``),
    and a present-but-NULL value refuses as ``price_null``.
    """

    def resolve(
        bucket: datetime, moment: datetime, recorded: Mapping[str, Any] | None
    ) -> tuple[float | None, float | None, str, str]:
        buy: float | None = None
        sell: float | None = None
        buy_source = NO_PRICE
        sell_source = NO_PRICE
        for column, setter in (("price_buy", "buy"), ("price_sell", "sell")):
            value: float | None = None
            source = NO_PRICE
            if column not in columns:  # the recording has no such column: the declared fallback
                slot = _price_in(leg, moment)
                if slot is not None:
                    value = slot[0] if setter == "buy" else slot[1]
                    source = PLAN_SLOT_FALLBACK
            elif recorded is None:  # the column exists, the row does not: never the plan slot
                source = ROW_ABSENT
            else:
                value = _finite(recorded.get(column))
                source = RECORDED_ACTUALS if value is not None else PRICE_NULL
            if setter == "buy":
                buy, buy_source = value, source
            else:
                sell, sell_source = value, source
        return buy, sell, buy_source, sell_source

    return resolve


def _booked(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """The arm's own booked prices, for the tariff-model divergence finding only."""
    return _finite(row.get("price_buy")), _finite(row.get("price_sell"))


# --------------------------------------------------------------------------------- the windows


def _plant_reasons(tally: WindowTally, ceiling: PlantCeiling | None) -> tuple[tuple[str, ...], ...]:
    if ceiling is None:
        return (), ()
    tripped = []
    if tally.unavailable_ticks > ceiling.energy_unknown_ticks:
        tripped.append("energy_unknown_ticks")
    if tally.infeasible_commands > ceiling.infeasible_commands:
        tripped.append("infeasible_commands")
    if ceiling.unverified_bounds is not None and tally.unverified_bounds > ceiling.unverified_bounds:
        tripped.append("unverified_bounds")
    return tuple(tripped), ((PLANT_CEILING,) if tripped else ())


def _buckets(boundary: WindowBoundary, declared_sampling_s: float) -> tuple[datetime, ...]:
    """The settlement buckets a window must have been visited in, on production's bucket grid."""
    step = timedelta(seconds=declared_sampling_s)
    moment = quarter_floor(boundary.start, ACTUALS_MINUTES)
    moments = []
    while moment < boundary.end:
        moments.append(moment)
        moment += step
    return tuple(moments)


def _uncovered_gaps(
    boundary: WindowBoundary, clipped: Sequence[Segment], overlapped: Sequence[Gap]
) -> tuple[Gap, ...]:
    """Window time no clipped segment covers and no inter-row gap already reports.

    Correction 2 reports a gap *between* two stored rows; this reports the time inside the window
    that no row covers for any other reason (a late first row, an early last row), so leading and
    trailing uncovered stretches are named instead of resting on the runner's buckets_written
    claim.  Inter-row gaps are treated as already reported and never double-counted.
    """
    spans = sorted(
        [(piece.start, piece.end) for piece in clipped]
        + [(gap.covered_until, gap.next_bucket) for gap in overlapped]
    )
    gaps: list[Gap] = []
    cursor = boundary.start
    for start, end in spans:
        if start > cursor:
            gaps.append(
                Gap(
                    bucket=cursor,
                    covered_until=cursor,
                    next_bucket=start,
                    gap_s=(start - cursor).total_seconds(),
                )
            )
        cursor = max(cursor, min(boundary.end, end))
    if cursor < boundary.end:
        gaps.append(
            Gap(
                bucket=cursor,
                covered_until=cursor,
                next_bucket=boundary.end,
                gap_s=(boundary.end - cursor).total_seconds(),
            )
        )
    return tuple(gaps)


def _window(
    leg: LegResult,
    boundary: WindowBoundary,
    *,
    segments: Sequence[Segment],
    gaps: Sequence[Gap],
    missing_flow: bool,
    tally: WindowTally | None,
    ceiling: PlantCeiling | None,
    reference_segments: Sequence[Segment],
    reference_ok: bool,
    visits: set[datetime],
    declared_sampling_s: float,
) -> WindowAccount:
    clipped = tuple(
        piece for piece in (_clip(segment, boundary.start, boundary.end) for segment in segments) if piece
    )
    reasons: list[str] = []
    unvisited = tuple(
        bucket
        for bucket in _buckets(boundary, declared_sampling_s)
        if bucket not in visits
    )
    if unvisited:
        # A bucket the arm's own cadence never rewrote still holds the recording's row: that is not
        # this arm's flow, so the window is withheld rather than settled on it.
        reasons.append(BUCKET_NOT_VISITED)
    overlapped = tuple(
        gap
        for gap in gaps
        if gap.covered_until < boundary.end and gap.next_bucket > boundary.start
    )
    if overlapped:
        reasons.append(ENERGY_GAP)
    uncovered = _uncovered_gaps(boundary, clipped, overlapped)
    if uncovered:
        reasons.append(COVERAGE_GAP)
    if missing_flow:
        reasons.append(FLOW_NULL)
    if tally is None:
        reasons.append(PLANT_TALLY_MISSING)
    unpriced = [piece for piece in clipped if piece.buy_eur_per_kwh is None or piece.sell_eur_per_kwh is None]
    for piece in unpriced:
        sources = {piece.buy_source, piece.sell_source}
        if PRICE_NULL in sources and PRICE_NULL not in reasons:
            reasons.append(PRICE_NULL)
        if ROW_ABSENT in sources and ROW_ABSENT not in reasons:
            reasons.append(ROW_ABSENT)
        if not (sources & {PRICE_NULL, ROW_ABSENT}) and NO_PRICE_VINTAGE not in reasons:
            reasons.append(NO_PRICE_VINTAGE)
    tripped, ceiling_reasons = _plant_reasons(tally, ceiling) if tally is not None else ((), ())
    reasons.extend(reason for reason in ceiling_reasons if reason not in reasons)

    known = bool(clipped) and not unvisited and not overlapped and not uncovered and not missing_flow
    imports = sum(piece.import_kwh for piece in clipped) if known else None
    exports = sum(piece.export_kwh for piece in clipped) if known else None
    # Withholding is hard: a window the plant could not verify, or whose prices or flow cannot be
    # settled, carries no money at all -- so no consumer can pick up a number that was not earned.
    priced = known and not unpriced and not reasons
    cost = (
        sum(piece.import_kwh * float(piece.buy_eur_per_kwh) for piece in clipped) if priced else None
    )
    revenue = (
        sum(piece.export_kwh * float(piece.sell_eur_per_kwh) for piece in clipped) if priced else None
    )
    profit = None if cost is None or revenue is None else revenue - cost

    reference_clipped = tuple(
        piece
        for piece in (_clip(segment, boundary.start, boundary.end) for segment in reference_segments)
        if piece
    )
    reference_known = reference_ok and bool(reference_clipped)
    reference_import = sum(piece.import_kwh for piece in reference_clipped) if reference_known else None
    reference_export = sum(piece.export_kwh for piece in reference_clipped) if reference_known else None
    divergence = (
        abs(imports - reference_import) + abs(exports - reference_export)
        if known and reference_known and imports is not None and exports is not None
        else None
    )
    fallbacks = sum(
        1
        for piece in clipped
        if piece.buy_source == PLAN_SLOT_FALLBACK or piece.sell_source == PLAN_SLOT_FALLBACK
    )
    divergent = sum(
        1
        for piece in clipped
        if _differs(piece.arm_booked_buy, piece.buy_eur_per_kwh)
        or _differs(piece.arm_booked_sell, piece.sell_eur_per_kwh)
    )
    return WindowAccount(
        index=boundary.index,
        start=boundary.start,
        end=boundary.end,
        segments=clipped,
        import_kwh=imports,
        export_kwh=exports,
        cost_eur=cost,
        revenue_eur=revenue,
        profit_eur=profit,
        reference_import_kwh=reference_import,
        reference_export_kwh=reference_export,
        divergence_kwh=divergence,
        pv_load_source=leg.episode.base.window(boundary.start, boundary.end),
        price_fallbacks=fallbacks,
        tariff_divergence_segments=divergent,
        gaps=overlapped + uncovered,
        plant=tally if tally is not None else WindowTally(boundary.index),
        ceiling_tripped=tripped,
        reasons=tuple(reasons),
    )


def _differs(left: float | None, right: float | None, tolerance: float = 1e-9) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) > tolerance


# ------------------------------------------------------------------------- the terminal term


#: Provenance of a declared window-start basis: the recording's own row, or the ledger's rebuild.
FROZEN_PROFILE_BASIS = "frozen_profile_row"
DERIVED_WINDOW_START_BASIS = "derived_as_of_window_start"
MIXED_WINDOW_START_BASIS = "mixed_frozen_and_derived"
NO_WINDOW_START_BASIS = "no_window_start_basis"


@dataclass(frozen=True)
class WindowStartValuation:
    """Every enabled pack's cost basis at the window start, and where each number came from.

    Declared *before* any leg drives (see :func:`window_start_valuation`) because the terminal term
    must be valued on information the window start had -- not on whatever the leg's own run happened
    to produce.  ``missing`` names the packs neither source could value, which is the only case that
    still earns ``no_valuation``.
    """

    basis: Mapping[str, float] = field(default_factory=dict)
    source: str = NO_WINDOW_START_BASIS
    frozen: tuple[str, ...] = ()
    derived: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[tuple[str, str], ...] = ()

    @property
    def packs(self) -> int:
        return len(self.basis)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "packs": self.packs,
            "basis_eur_per_kwh": {key: round(value, 6) for key, value in sorted(self.basis.items())},
            "frozen": list(self.frozen),
            "derived": list(self.derived),
            "missing": list(self.missing),
            "notes": [{"battery_id": key, "text": text} for key, text in self.notes],
        }


def window_start_valuation(episode: Episode) -> WindowStartValuation:
    """Declare the window-start basis for every enabled pack, reading nothing past the window start.

    A pack whose frozen ``battery_cost_basis`` row the recording already had at the window start
    keeps that row.  For a pack without one, the ledger's own :func:`app.cost_basis.update_basis` is
    run with ``now = window start`` against the episode's truncated scratch copy: the same call the
    engine's first tick makes, the same fleet/enabled/reserve arguments, and only rows up to the
    window start exist there yet -- so the derivation cannot anticipate the window it values.  The
    scratch copy ends up holding that ledger, which is what the engine would write anyway; the
    *declaration* is what matters, because it is fixed before the run and reported with its
    provenance instead of being inferred from the run it is used to value.
    """
    configs = [config for config in episode.batteries if getattr(config, "enabled", True)]
    reserve = 0.0
    settings = getattr(episode, "settings", None)
    if settings is not None:
        value = settings.get("optimizer.reserve_soc_pct", 0.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reserve = float(value)
    basis: dict[str, float] = {}
    frozen: list[str] = []
    derived: list[str] = []
    missing: list[str] = []
    notes: list[tuple[str, str]] = []
    for config in configs:
        payload = episode.profiles.get((COST_BASIS_KIND, config.id)) or {}
        value = _finite(payload.get("basis_eur_per_kwh"))
        if value is not None:
            basis[config.id] = value
            frozen.append(config.id)
            continue
        try:
            booked = update_cost_basis(
                episode.store(),
                config,
                episode.start,
                fleet_enabled=len(configs),
                reserve_soc_pct=reserve,
            )
        except Exception as exc:  # noqa: BLE001 - an underivable basis is a missing valuation
            notes.append((config.id, f"{type(exc).__name__}: {exc}"))
            booked = None
        value = _finite((booked or {}).get("basis_eur_per_kwh"))
        if value is None:
            missing.append(config.id)
            continue
        basis[config.id] = value
        derived.append(config.id)
    if not basis:
        source = NO_WINDOW_START_BASIS
    elif not derived:
        source = FROZEN_PROFILE_BASIS
    elif not frozen:
        source = DERIVED_WINDOW_START_BASIS
    else:
        source = MIXED_WINDOW_START_BASIS
    return WindowStartValuation(
        basis=dict(sorted(basis.items())),
        source=source,
        frozen=tuple(frozen),
        derived=tuple(derived),
        missing=tuple(missing),
        notes=tuple(notes),
    )


def _booked_bases(
    leg: LegResult, window_start: WindowStartValuation | None = None
) -> tuple[tuple[str, float | None, float | None, Any], ...]:
    """Each pack's declared window-start basis and the ledger's energy, as of the window start.

    With a declaration the basis comes from it and the fourth element names that provenance; without
    one the frozen profile row stands in, keeping :func:`account_leg` usable on its own.
    """
    rows = []
    for config in leg.episode.batteries:
        payload = leg.episode.profiles.get((COST_BASIS_KIND, config.id)) or {}
        if window_start is not None:
            declared = _finite(window_start.basis.get(config.id))
            source = None
            if declared is not None:
                source = (
                    FROZEN_PROFILE_BASIS
                    if config.id in window_start.frozen
                    else DERIVED_WINDOW_START_BASIS
                )
            rows.append((config.id, declared, _finite(payload.get("energy_kwh")), source))
            continue
        rows.append(
            (
                config.id,
                _finite(payload.get("basis_eur_per_kwh")),
                _finite(payload.get("energy_kwh")),
                payload.get("updated_at"),
            )
        )
    return tuple(rows)


def _energy_by_id(states: Sequence[Any]) -> dict[str, float | None]:
    return {state.id: _finite(state.energy_kwh) for state in states}


@dataclass(frozen=True)
class TerminalValuation:
    """The terminal-inventory term, and the two declared valuations it can be booked at."""

    initial_energy_kwh: float | None
    terminal_energy_kwh: float | None
    energy_delta_kwh: float | None
    valuation_eur_per_kwh: float | None
    terminal_adjustment_eur: float | None
    sensitivity_valuation_eur_per_kwh: float | None
    sensitivity_terminal_adjustment_eur: float | None
    notes: tuple[tuple[str, str], ...]


def _ledger_booked_basis(leg: LegResult) -> dict[str, float | None]:
    """Each pack's own booked basis as the leg's scratch ledger holds it after the run.

    This is the declared ``ledger_booked`` sensitivity's source.  With a window-start declaration
    present it is a *different* number from the declared headline basis -- recomputing the declared
    basis here would collapse the declared valuation pair into one number and report it twice.
    """
    out: dict[str, float | None] = {}
    for config in leg.episode.batteries:
        payload = load_cost_basis(leg.store, config.id)
        out[config.id] = _finite((payload or {}).get("basis_eur_per_kwh"))
    return out


def _terminal(
    leg: LegResult,
    bases: Sequence[tuple[str, float | None, float | None, Any]],
    window_start: WindowStartValuation | None = None,
    ledger_basis: Mapping[str, float | None] | None = None,
) -> TerminalValuation:
    """``(E_end - E_start) x valuation``, from the declared window-start bases and the arm's energies."""
    notes: list[tuple[str, str]] = []
    start_energies = _energy_by_id(leg.initial_battery_states)
    end_energies = _energy_by_id(leg.final_battery_states)
    if (
        not start_energies
        or not end_energies
        or any(value is None for value in start_energies.values())
        or any(value is None for value in end_energies.values())
    ):
        notes.append((TERMINAL_UNKNOWN, "a pack's energy is unknown at one end of the window"))
        return TerminalValuation(None, None, None, None, None, None, None, tuple(notes))
    e_start = float(sum(value for value in start_energies.values() if value is not None))
    e_end = float(sum(value for value in end_energies.values() if value is not None))
    delta = e_end - e_start

    weighted = [
        (basis, start_energies.get(battery_id) or 0.0)
        for battery_id, basis, _ledger_energy, _updated in bases
        if basis is not None and (start_energies.get(battery_id) or 0.0) > 0
    ]
    total_weight = sum(weight for _basis, weight in weighted)
    valuation = (
        sum(basis * weight for basis, weight in weighted) / total_weight if total_weight > 0 else None
    )
    if valuation is None:
        notes.append(
            (
                NO_VALUATION,
                (
                    "neither a frozen row nor the ledger's rebuild valued the window-start inventory"
                    if window_start is not None
                    else "no frozen cost basis covers the window-start inventory"
                )
                + ("; the energy delta is zero" if abs(delta) < 1e-9 else ""),
            )
        )
    term = None if valuation is None else delta * valuation

    sensitivity = 0.0
    sensitivity_ok = True
    for battery_id, _declared, _ledger_energy, _updated in bases:
        pack_delta = (end_energies.get(battery_id) or 0.0) - (start_energies.get(battery_id) or 0.0)
        if abs(pack_delta) < 1e-9:
            continue
        booked = (ledger_basis or {}).get(battery_id)
        if booked is None:
            sensitivity_ok = False
            break
        sensitivity += pack_delta * booked
    if not sensitivity_ok:
        notes.append(
            (
                NO_LEDGER_BASIS,
                f"a pack with a non-zero energy delta has no {VALUATION['sensitivity']} basis; "
                "the declared sensitivity valuation cannot be measured",
            )
        )
        return TerminalValuation(e_start, e_end, delta, valuation, term, None, None, tuple(notes))
    rate = sensitivity / delta if abs(delta) > 1e-9 else None
    return TerminalValuation(e_start, e_end, delta, valuation, term, rate, sensitivity, tuple(notes))


# ------------------------------------------------------------------------------- the entry points


def _ceiling_for(
    limits: PlantLimits | Mapping[str, PlantCeiling] | None, plant: str
) -> PlantCeiling | None:
    """The manifest's ceiling for one plant mode: ``PlantLimits``, or a plain mapping in tests."""
    if limits is None:
        return None
    resolver = getattr(limits, "ceiling", None)
    if callable(resolver):
        return resolver(plant)
    return limits.get(plant)


def account_leg(
    leg: LegResult,
    *,
    limits: PlantLimits | Mapping[str, PlantCeiling] | None = None,
    declared_sampling_s: float = DECLARED_SAMPLING_S,
    window_start: WindowStartValuation | None = None,
    tariff_tolerance_eur_per_kwh: float | None = None,
    tariff_declared: bool = False,
) -> LegAccount:
    """Account one driven leg: its own flows, the recording's settlement prices, its terminal term.

    ``limits`` is the manifest's ``plant_limits`` mapping (plant mode -> :class:`PlantCeiling`); a
    window whose :class:`WindowTally` exceeds its ceiling is withheld.  ``window_start`` is the
    declared window-start valuation (see :func:`window_start_valuation`); without it the frozen
    profile rows stand in, so the terminal term is never inferred from the leg's own run.
    ``tariff_tolerance_eur_per_kwh`` asks for the recording's booked feed-in price to be checked
    against the assumed contract (:func:`recorded_tariff_check`): a window whose price the model
    cannot reproduce has its money withheld, and ``tariff_declared`` marks a leg that declares its
    own rate instead (nothing to check, stated as such).
    """
    if not leg.windows:
        raise AccountingError(WINDOW_MISMATCH, f"{leg.name}: the window has no decision boundary")
    visits = set(leg.buckets_written)
    recorded = _recorded_rows(leg)
    recorded_columns = _recorded_columns(leg)
    arm_segments, gaps, missing_flow, _fallbacks = _segments(
        _arm_rows(leg),
        end=leg.end,
        declared_sampling_s=declared_sampling_s,
        flow=lambda row: row.get("grid_kw"),
        price=_arm_price(leg, recorded_columns),
        booked=_booked,
        reference=recorded,
    )
    recorded_rows = [recorded[moment] for moment in sorted(recorded)]
    reference_segments, _reference_gaps, reference_missing, _reference_fallbacks = _segments(
        recorded_rows,
        end=leg.end,
        declared_sampling_s=declared_sampling_s,
        flow=lambda row: row.get("grid_kw"),
        price=lambda *_args: (None, None, NO_PRICE, NO_PRICE),
    )
    ceiling = _ceiling_for(limits, leg.plant)
    tallies = {tally.index: tally for tally in leg.window_tallies}
    windows = tuple(
        _window(
            leg,
            boundary,
            segments=arm_segments,
            gaps=gaps,
            missing_flow=missing_flow,
            tally=tallies.get(boundary.index),
            ceiling=ceiling,
            reference_segments=reference_segments,
            reference_ok=not reference_missing,
            visits=visits,
            declared_sampling_s=declared_sampling_s,
        )
        for boundary in leg.windows
    )
    # The recording's own tariff is an assumption, not a fact: check it before any window's money is
    # accepted.  A window that fails is withheld (its money nulled, exactly like a ceiling trip), so
    # neither the totals nor the pair can rest on a contract the recording did not follow.
    tariff, tariff_divergence = recorded_tariff_check(
        windows,
        settings=_recording_settings(leg),
        tolerance_eur_per_kwh=tariff_tolerance_eur_per_kwh,
        declared=tariff_declared,
    )
    if tariff.diverged_windows:
        breaching = set(tariff.diverged_windows)
    elif tariff.model == TARIFF_MODEL_UNAVAILABLE:
        # Nothing about the recording's booked feed-in price could be verified against the assumed
        # model, so the revenue settled on it is not evidence: every window is withheld.
        breaching = {window.index for window in windows}
    else:
        breaching = set()
    if breaching:
        windows = tuple(
            replace(
                window,
                reasons=tuple(dict.fromkeys((*window.reasons, EXPORT_REVENUE_UNVERIFIED))),
                cost_eur=None,
                revenue_eur=None,
                profit_eur=None,
            )
            if window.index in breaching
            else window
            for window in windows
        )
    known = [window for window in windows if window.import_kwh is not None]
    priced = [window for window in windows if window.profit_eur is not None]
    booked_bases = _booked_bases(leg, window_start)
    terminal = _terminal(leg, booked_bases, window_start, _ledger_booked_basis(leg))
    term = terminal.terminal_adjustment_eur
    # A leg's total is a *whole-window* quantity or nothing: summing only the windows that happened to
    # be priced (or measured) prints 21 of 22 windows as the leg's number, which reads as complete.
    # The coverage counters keep the partial information visible, so nothing is hidden by the null.
    complete_flow = bool(windows) and len(known) == len(windows)
    complete = bool(windows) and len(priced) == len(windows)
    flow_total = (
        sum(window.import_kwh for window in known if window.import_kwh is not None)
        if complete_flow
        else None
    )
    flow_export_total = (
        sum(window.export_kwh for window in known if window.export_kwh is not None)
        if complete_flow
        else None
    )
    cost_total = (
        sum(window.cost_eur for window in priced if window.cost_eur is not None) if complete else None
    )
    revenue_total = (
        sum(window.revenue_eur for window in priced if window.revenue_eur is not None)
        if complete
        else None
    )
    profit_total = (
        sum(window.profit_eur for window in priced if window.profit_eur is not None)
        if complete
        else None
    )
    return LegAccount(
        name=leg.name,
        plant=leg.plant,
        policy=leg.policy,
        start=leg.start,
        end=leg.end,
        step_s=leg.step_s,
        declared_sampling_s=declared_sampling_s,
        windows=windows,
        import_kwh=flow_total,
        export_kwh=flow_export_total,
        cost_eur=cost_total,
        revenue_eur=revenue_total,
        profit_eur=profit_total,
        windows_unknown=len(windows) - len(known),
        windows_measured=len(known),
        windows_priced=len(priced),
        withheld_windows=tuple(window.index for window in windows if window.withheld),
        initial_energy_kwh=terminal.initial_energy_kwh,
        terminal_energy_kwh=terminal.terminal_energy_kwh,
        energy_delta_kwh=terminal.energy_delta_kwh,
        booked_basis=booked_bases,
        valuation_eur_per_kwh=terminal.valuation_eur_per_kwh,
        terminal_adjustment_eur=term,
        profit_with_terminal_eur=(
            None if term is None or profit_total is None else profit_total + term
        ),
        sensitivity_valuation_eur_per_kwh=terminal.sensitivity_valuation_eur_per_kwh,
        sensitivity_terminal_adjustment_eur=terminal.sensitivity_terminal_adjustment_eur,
        notes=terminal.notes,
        valuation_source=None if window_start is None else window_start.source,
        valuation_frozen=() if window_start is None else window_start.frozen,
        valuation_derived=() if window_start is None else window_start.derived,
        valuation_missing=() if window_start is None else window_start.missing,
        tariff_check=tariff,
    )


def paired_delta(
    left: LegAccount,
    right: LegAccount,
    *,
    change: str = "",
    include_terminal: bool = True,
) -> PairedDelta:
    """The paired per-window delta-profit of an arm: ``right`` against ``left``.

    A window is withheld when either side is -- plant ceiling, price, gap or an unvisited bucket --
    and its delta and bootstrap sample are dropped, never averaged over.  The terminal-inventory
    delta is a run-level item: it is added to the sum of the included windows (never to a single
    window) and stays ``None`` when either side's valuation is unknown.
    """
    if len(left.windows) != len(right.windows) or any(
        (a.start, a.end) != (b.start, b.end) for a, b in zip(left.windows, right.windows)
    ):
        raise AccountingError(
            WINDOW_MISMATCH,
            f"{left.name} and {right.name} do not share the recording's decision windows",
        )
    windows = []
    for left_window, right_window in zip(left.windows, right.windows):
        reasons = tuple(dict.fromkeys((*left_window.reasons, *right_window.reasons)))
        withheld = bool(reasons) or left_window.profit_eur is None or right_window.profit_eur is None
        delta = (
            None
            if withheld or left_window.profit_eur is None or right_window.profit_eur is None
            else right_window.profit_eur - left_window.profit_eur
        )
        windows.append(
            PairedWindow(
                index=left_window.index,
                start=left_window.start,
                end=left_window.end,
                left_profit_eur=left_window.profit_eur,
                right_profit_eur=right_window.profit_eur,
                delta_eur=delta,
                withheld=withheld,
                reasons=reasons,
            )
        )
    # A plain float sum over whatever happened to be included is indistinguishable from a real zero
    # (E4's null-by-construction pair is a genuine 0.0): the total is published only when every
    # window contributed, and otherwise None, which the renderer prints as withheld.
    included = [window for window in windows if not window.withheld and window.delta_eur is not None]
    total: float | None = (
        math.fsum(window.delta_eur for window in included if window.delta_eur is not None)
        if included and len(included) == len(windows)
        else None
    )
    terminal = None
    if include_terminal:
        left_term = left.terminal_adjustment_eur
        right_term = right.terminal_adjustment_eur
        if left_term is not None and right_term is not None:
            terminal = right_term - left_term
    return PairedDelta(
        change=change,
        windows=tuple(windows),
        delta_eur=total,
        delta_with_terminal_eur=None if terminal is None or total is None else total + terminal,
        terminal_delta_eur=terminal,
        withheld_windows=tuple(window.index for window in windows if window.withheld),
        included_windows=sum(1 for window in windows if not window.withheld),
    )
