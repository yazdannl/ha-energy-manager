"""The replay's uncertainty: a circular moving-block bootstrap over decision windows.

docs/REPLAY_HARNESS.md section 3 and docs/OPTIMIZER_V2.md section 3.2: loss attribution is paired per
decision window, and uncertainty is a block bootstrap over those windows -- never an IID resampling
of prices or of independent intervals, because consecutive windows share what makes them correlate
(an overlapping plan horizon, the battery's state of charge, the shape of the tariff's day).  The
windows are the recording's own decision cadence (``LegResult.windows``, the intervals between its
plan vintages), so a block of consecutive windows is a real stretch of the recording.

The draw is a circular moving-block bootstrap: ``blocks`` consecutive windows per block, wrapped at
the end so every window can start a block, ``resamples`` draws, each truncated to the sample count so
every resample carries the same weight.  The interval is the percentile interval of the resample
means and the point estimate is the plain mean of the samples.  Every number comes from
``random.Random(seed)`` in one documented draw order, so the same manifest and inputs give the same
report.

Degenerate cases are stated, never dressed up: fewer than two samples (or no resamples) reports
``degenerate=True`` with a ``reason`` and ``interval=None`` while still reporting the mean and total,
so a single-window day says what it measured instead of implying an interval.  Withheld windows are
not samples at all: :func:`bootstrap_paired` takes only the windows that survived the accounting
refusals and counts the rest, because a window the plant could not simulate must not widen or narrow
an interval.

A block length is only as good as the correlation it claims to capture.  What makes consecutive
windows correlate here is the plan horizon they overlap, so the length that captures it is
``ceil(horizon_seconds / window_seconds)`` -- the number of decision windows one plan covers
(:func:`plan_horizon_windows`).  When the episode is shorter than that, the interval is not evidence
about the horizon-overlapping process: it is reported, but marked ``underpowered`` with
``SAMPLE_SHORTER_THAN_PLAN_HORIZON`` and a one-line note, and the report states how long an episode
must be for a publishable interval instead of pretending the recording supported one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from statistics import fmean, stdev
from typing import Sequence

from .accounting import PairedDelta

#: The default percentile interval of the resample means (95 %).
DEFAULT_CONFIDENCE = 0.95

NO_WINDOWS = "no_windows"
SINGLE_WINDOW = "single_window"
NO_RESAMPLES = "no_resamples"
#: The sample is shorter than one plan horizon, so its interval cannot speak for the process the
#: blocks are supposed to capture.  The interval is still computed and reported -- it is just not
#: evidence, which is what this slug says.
SAMPLE_SHORTER_THAN_PLAN_HORIZON = "sample_shorter_than_plan_horizon"
#: The drawn block length is shorter than one plan horizon: the blocks cannot span the overlap they
#: exist to capture, so a long episode does not make the interval evidence by itself.
BLOCK_SHORTER_THAN_PLAN_HORIZON = "block_shorter_than_plan_horizon"
#: Fewer than the manifest's declared number of plan horizons of comparable windows.
SAMPLE_SHORTER_THAN_EVIDENCE_HORIZONS = "sample_shorter_than_evidence_horizons"
#: Withheld windows split the sample into non-contiguous runs, so some drawn blocks span a gap.
WINDOWS_NOT_CONTIGUOUS = "windows_not_contiguous"
#: The plan horizon is unknown, so no block length can be checked against it: not evidence.
HORIZON_UNKNOWN = "horizon_unknown"
#: Where the judged horizon came from, so the interval's parameter can be traced: the recording's
#: own plan span (what the engine actually planned over), its ``optimizer.horizon_hours`` setting
#: row (written only when the value changed), or nowhere -- and "nowhere" refuses the claim instead
#: of resting it on the engine's default, which the replay applies for fidelity but the recording
#: does not state.
HORIZON_FROM_PLAN_SPAN = "recording_plan_span"
HORIZON_FROM_SETTING = "recording_setting"
HORIZON_SOURCE_UNKNOWN = "unknown"
UNDERPOWERED_NOTE = (
    "the episode is shorter than one plan horizon, so this interval is not evidence (it cannot "
    "capture the overlap the blocks stand for); the window must span many plan horizons before a "
    "one-line uncertainty claim is quoted"
)
BLOCK_SHORTER_NOTE = (
    "the drawn block length is shorter than one plan horizon, so the blocks cannot span the "
    "overlap they exist to capture; the interval is reported but is not evidence until --blocks "
    "reaches the horizon-implied length"
)
EVIDENCE_HORIZONS_NOTE = (
    "the episode carries fewer than the declared number of plan horizons (5 by default, the "
    "manifest's evidence_horizons) of comparable windows, so the interval is reported but is not "
    "evidence"
)
NOT_CONTIGUOUS_NOTE = (
    "withheld windows break the sample into non-contiguous runs, so some drawn blocks span a gap "
    "in the recording; the interval is reported but is not evidence"
)
HORIZON_UNKNOWN_NOTE = (
    "the recording declares no plan horizon -- neither a plan span nor an "
    "optimizer.horizon_hours setting -- so the block length cannot be checked against the "
    "correlation it stands for and no horizon-implied length may be published; the interval is "
    "reported but is not evidence"
)
#: What a publishable interval requires, in the report's own words.  Both floors are named: the
#: horizon-implied block length and the declared number of horizons of windows (5 by default),
#: because stating only the first understates the bar by that factor.
EPISODE_EVIDENCE_REQUIREMENT = (
    "an interval is publishable only when the drawn block length reaches the horizon-implied number "
    "of comparable decision windows (ceil(horizon_seconds / window_seconds)) AND the episode "
    "carries the declared number of plan horizons of such windows (evidence_horizons, 5 by "
    "default) in one contiguous run -- i.e. at least evidence_horizons x ceil(horizon_seconds / "
    "window_seconds) windows; a tuned-policy section additionally needs enough windows for the "
    "chronological tune/holdout split, so a window shorter than one plan horizon is illustrative "
    "evidence, never a gated number"
)


@dataclass(frozen=True)
class BootstrapResult:
    """A block-bootstrap interval over decision windows, with every parameter stated.

    ``mean_eur`` is the per-window mean delta, ``total_eur`` the sum over the same windows, and the
    interval describes the *mean* (the per-window rate), which is what a paired comparison of
    policies estimates; the total's own uncertainty scales with the window count the report prints.
    ``total_eur`` is ``None`` when any window was excluded: the sum over the included windows is a
    partial total, and printing it would read as the arm's own number (``excluded_windows`` and
    ``windows_total`` keep that coverage visible, exactly as the accounting's leg totals do).
    """

    samples: int
    excluded_windows: int
    windows_total: int
    mean_eur: float | None
    total_eur: float | None
    interval_low_eur: float | None
    interval_high_eur: float | None
    standard_error_eur: float | None
    resamples: int
    block_length: int
    seed: int
    confidence: float
    degenerate: bool
    reason: str
    #: The window length actually used (mean over the arm's own decision windows, in seconds) and
    #: the horizon it was checked against, so the block length can be re-derived from the report.
    window_seconds: float | None = None
    horizon_hours: float | None = None
    #: Which of :data:`HORIZON_FROM_PLAN_SPAN`/:data:`HORIZON_FROM_SETTING`/
    #: :data:`HORIZON_SOURCE_UNKNOWN` supplied ``horizon_hours``, so a reader can tell a declared
    #: horizon from a missing one (and from the engine default the replay applies but does not claim).
    horizon_source: str = HORIZON_SOURCE_UNKNOWN
    block_length_horizon_windows: int | None = None
    underpowered: bool = False
    publishable: bool = True
    note: str = ""
    #: The manifest's declared publishability bar: this many plan horizons of windows are needed.
    evidence_horizons: int = 5
    #: How many included windows follow a withheld one: each is a place a drawn block can span a
    #: gap in the recording rather than consecutive time.
    gap_count: int = 0
    #: The included windows that start a new run (see :func:`gap_windows`): the count named, so a
    #: reader can check it against the pair's own window list instead of trusting the number.
    gap_windows: tuple[int, ...] = ()

    @property
    def interval_eur(self) -> tuple[float, float] | None:
        if self.interval_low_eur is None or self.interval_high_eur is None:
            return None
        return (self.interval_low_eur, self.interval_high_eur)


def plan_horizon_windows(horizon_hours: float | None, window_seconds: float | None) -> int | None:
    """How many decision windows one plan horizon spans: the block length that captures it.

    ``None`` when either input is unknown or non-positive, because a block length derived from a
    missing horizon would be exactly the invented precision this check exists to prevent.
    """
    if horizon_hours is None or window_seconds is None:
        return None
    seconds = float(horizon_hours) * 3600.0
    window = float(window_seconds)
    if not (seconds > 0.0) or not (window > 0.0):
        return None
    return max(1, int(math.ceil(seconds / window)))


def mean_window_seconds(windows: Sequence[Any]) -> float | None:
    """The mean length of the decision windows actually used, in seconds (``None`` if none)."""
    lengths = [
        (window.end - window.start).total_seconds()
        for window in windows
        if getattr(window, "end", None) is not None
        and getattr(window, "start", None) is not None
        and window.end > window.start
    ]
    if not lengths:
        return None
    return fmean(lengths)


def _judge_interval(
    samples: int, block_length: int, implied: int | None, horizons: int, gap_count: int
) -> tuple[bool, str, str, bool]:
    """Publishability and its reason: drawn block length, sample size and sample contiguity.

    A block length of the manifest's ``blocks`` says nothing about the correlation the interval
    claims to capture: the drawn length itself must reach the horizon-implied number of windows,
    and the sample must carry the declared number of horizons, before the interval is evidence.
    """
    if implied is None:
        return False, HORIZON_UNKNOWN, HORIZON_UNKNOWN_NOTE, False
    if samples < implied:
        return False, SAMPLE_SHORTER_THAN_PLAN_HORIZON, UNDERPOWERED_NOTE, True
    if block_length < implied:
        return False, BLOCK_SHORTER_THAN_PLAN_HORIZON, BLOCK_SHORTER_NOTE, False
    if samples < horizons * implied:
        return False, SAMPLE_SHORTER_THAN_EVIDENCE_HORIZONS, EVIDENCE_HORIZONS_NOTE, False
    if gap_count > 0:
        return False, WINDOWS_NOT_CONTIGUOUS, NOT_CONTIGUOUS_NOTE, False
    return True, "", "", False


def percentile(values: Sequence[float], pct: float) -> float | None:
    """A linearly interpolated percentile; ``None`` for an empty sample."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * float(pct) / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(low)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def resample_means(
    samples: Sequence[float], *, block_length: int, resamples: int, seed: int
) -> list[float]:
    """The bootstrap's resample means, in draw order -- the algorithm, not just its summary.

    One draw takes ``block_length`` consecutive windows starting at a uniformly drawn window,
    wrapping at the end, and repeats until it has ``samples`` values; the draw's mean is one
    observation.  A block length of one is an IID window draw (available for comparison, never the
    default) and a block length covering every window reproduces the sample mean exactly.
    """
    values = [float(value) for value in samples]
    count = len(values)
    if count == 0 or int(resamples) <= 0:
        return []
    length = max(1, min(int(block_length), count))
    rng = random.Random(int(seed))
    means: list[float] = []
    for _ in range(int(resamples)):
        draw: list[float] = []
        while len(draw) < count:
            start = rng.randrange(count)
            for offset in range(length):
                draw.append(values[(start + offset) % count])
                if len(draw) == count:
                    break
        means.append(fmean(draw))
    return means


def block_bootstrap(
    samples: Sequence[float],
    *,
    blocks: int,
    resamples: int,
    seed: int,
    confidence: float = DEFAULT_CONFIDENCE,
    horizon_hours: float | None = None,
    horizon_source: str = HORIZON_SOURCE_UNKNOWN,
    window_seconds: float | None = None,
    evidence_horizons: int = 5,
    gap_count: int = 0,
    gap_windows: Sequence[int] = (),
) -> BootstrapResult:
    """The block bootstrap of per-window deltas: point estimate, interval and its parameters.

    ``horizon_hours``, ``window_seconds`` and ``evidence_horizons`` *judge* the draw: the declared
    ``blocks`` is what is drawn, and the interval is publishable only when that drawn length reaches
    the horizon-implied number of windows and the sample carries the declared number of horizons.
    Anything short of that is still reported -- labelled ``underpowered``/not publishable with its
    reason, never silently shortened or trusted.  ``horizon_source`` records where the horizon came
    from: an unknown one refuses the implied-length claim (see :data:`HORIZON_UNKNOWN_NOTE`).
    ``gap_windows`` names the sample's interior breaks when the caller has them (see
    :func:`gap_windows`); ``gap_count`` alone is the count-only path, used by callers that know the
    number without the indices.
    """
    gaps = tuple(sorted(int(index) for index in gap_windows)) if gap_windows else ()
    breaks = len(gaps) if gaps else int(gap_count)
    values = [float(value) for value in samples]
    count = len(values)
    length = max(1, min(int(blocks), count)) if count else int(blocks)
    implied = plan_horizon_windows(horizon_hours, window_seconds)
    horizons = max(1, int(evidence_horizons))
    publishable, judged_reason, judged_note, underpowered = _judge_interval(
        count, length, implied, horizons, int(breaks)
    )
    if count < 2 or int(resamples) < 1:
        reason = NO_WINDOWS if count == 0 else SINGLE_WINDOW
        if int(resamples) < 1:
            reason = NO_RESAMPLES
        return BootstrapResult(
            samples=count,
            excluded_windows=0,
            windows_total=count,
            mean_eur=fmean(values) if count else None,
            total_eur=math.fsum(values) if count else None,
            interval_low_eur=None,
            interval_high_eur=None,
            standard_error_eur=None,
            resamples=int(resamples),
            block_length=length,
            seed=int(seed),
            confidence=float(confidence),
            degenerate=True,
            reason=reason,
            window_seconds=None if window_seconds is None else round(float(window_seconds), 3),
            horizon_hours=None if horizon_hours is None else float(horizon_hours),
            horizon_source=str(horizon_source) or HORIZON_SOURCE_UNKNOWN,
            block_length_horizon_windows=implied,
            underpowered=underpowered,
            publishable=False,
            note=judged_note,
            evidence_horizons=horizons,
            gap_count=int(breaks),
            gap_windows=gaps,
        )
    low_pct = (1.0 - float(confidence)) / 2.0 * 100.0
    means = resample_means(values, block_length=length, resamples=resamples, seed=seed)
    return BootstrapResult(
        samples=count,
        excluded_windows=0,
        windows_total=count,
        mean_eur=fmean(values),
        total_eur=math.fsum(values),
        interval_low_eur=percentile(means, low_pct),
        interval_high_eur=percentile(means, 100.0 - low_pct),
        standard_error_eur=stdev(means) if len(means) > 1 else None,
        resamples=int(resamples),
        block_length=length,
        seed=int(seed),
        confidence=float(confidence),
        degenerate=False,
        reason="" if publishable else judged_reason,
        window_seconds=None if window_seconds is None else round(float(window_seconds), 3),
        horizon_hours=None if horizon_hours is None else float(horizon_hours),
        horizon_source=str(horizon_source) or HORIZON_SOURCE_UNKNOWN,
        block_length_horizon_windows=implied,
        underpowered=underpowered,
        publishable=publishable,
        note=judged_note,
        evidence_horizons=horizons,
        gap_count=int(breaks),
        gap_windows=gaps,
    )


def gap_windows(windows: Sequence[Any]) -> tuple[int, ...]:
    """The included windows that start a new run: each is a hole a drawn block can span.

    A window counts when it is included but is not the successor of the previously included window
    (in the pair's own ascending order), which is exactly a place the sample stops covering
    consecutive time.  Only *interior* breaks are named: the circular draw's wrap from the last
    window back to the first is intrinsic to the method, not a hole in the recording, so counting it
    would mark every sample of two or more windows as non-contiguous and the flag would say nothing.
    """
    previous: int | None = None
    starts: list[int] = []
    for window in windows:
        if getattr(window, "withheld", False) or getattr(window, "delta_eur", None) is None:
            continue
        index = int(window.index)
        if previous is not None and index != previous + 1:
            starts.append(index)
        previous = index
    return tuple(starts)


def bootstrap_paired(
    paired: PairedDelta,
    *,
    blocks: int,
    resamples: int,
    seed: int,
    confidence: float = DEFAULT_CONFIDENCE,
    horizon_hours: float | None = None,
    horizon_source: str = HORIZON_SOURCE_UNKNOWN,
    window_seconds: float | None = None,
    evidence_horizons: int = 5,
) -> BootstrapResult:
    """One arm's paired deltas, bootstrapped over the windows that could be compared.

    Withheld windows are excluded from the sample and counted in ``excluded_windows``, so the
    interval is never widened by a window whose plant could not be simulated and never silently
    narrowed by dropping one.  The window length defaults to the mean over the *included* decision
    windows, and the windows that start a new run are named (``gap_windows``): with gaps present,
    some drawn blocks span time that is not in the sample, so the interval is not evidence for the
    contiguous process the method claims to describe.
    """
    samples = paired.bootstrap_samples
    included_windows = [
        window
        for window in paired.windows
        if not getattr(window, "withheld", False) and getattr(window, "delta_eur", None) is not None
    ]
    result = block_bootstrap(
        samples,
        blocks=blocks,
        resamples=resamples,
        seed=seed,
        confidence=confidence,
        horizon_hours=horizon_hours,
        horizon_source=horizon_source,
        window_seconds=(
            mean_window_seconds(included_windows) if window_seconds is None else window_seconds
        ),
        evidence_horizons=evidence_horizons,
        gap_windows=gap_windows(paired.windows),
    )
    excluded = len(paired.windows) - len(samples)
    if excluded == 0:
        return result
    # The sum over the included windows is not this arm's total: withholding a window must null the
    # total, never quietly shrink it.  The mean and its interval are still the per-window rate the
    # comparison estimates, and the count that was dropped stays visible next to them.
    return replace(
        result,
        excluded_windows=excluded,
        windows_total=len(paired.windows),
        total_eur=None,
    )
