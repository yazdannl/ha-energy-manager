"""The replay's uncertainty: the block bootstrap over decision windows.

The property under test is the *method*: consecutive windows drawn in blocks (never IID single
windows), one seeded draw order, parameters stated with the result, and degenerate cases reported as
such instead of being presented as an interval.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "energy_manager"))

from app.replay.accounting import PLANT_CEILING, PairedDelta, PairedWindow  # noqa: E402
from app.replay.statistics import (  # noqa: E402
    BLOCK_SHORTER_THAN_PLAN_HORIZON,
    EPISODE_EVIDENCE_REQUIREMENT,
    HORIZON_UNKNOWN,
    NO_RESAMPLES,
    NO_WINDOWS,
    SAMPLE_SHORTER_THAN_EVIDENCE_HORIZONS,
    SAMPLE_SHORTER_THAN_PLAN_HORIZON,
    SINGLE_WINDOW,
    WINDOWS_NOT_CONTIGUOUS,
    block_bootstrap,
    bootstrap_paired,
    gap_windows,
    mean_window_seconds,
    percentile,
    plan_horizon_windows,
    resample_means,
)

START = datetime(2026, 1, 2, 10, 0)
DELTAS = [1.0, -0.5, 2.0, 0.25, -1.5]


def pair(deltas, *, withheld=(), change="policies") -> PairedDelta:
    """A paired-delta record built by hand: the bootstrap only sees windows and samples."""
    windows = []
    for index, delta in enumerate(deltas):
        held = index in withheld
        windows.append(
            PairedWindow(
                index=index,
                start=START + timedelta(minutes=15 * index),
                end=START + timedelta(minutes=15 * (index + 1)),
                left_profit_eur=0.0,
                right_profit_eur=None if held else delta,
                delta_eur=None if held else delta,
                withheld=held,
                reasons=(PLANT_CEILING,) if held else (),
            )
        )
    samples = [window.delta_eur for window in windows if not window.withheld]
    return PairedDelta(
        change=change,
        windows=tuple(windows),
        delta_eur=sum(samples),
        delta_with_terminal_eur=None,
        terminal_delta_eur=None,
        withheld_windows=tuple(index for index in withheld),
        included_windows=len(samples),
    )


def test_the_draw_is_deterministic_from_the_seed() -> None:
    first = block_bootstrap(DELTAS, blocks=2, resamples=200, seed=7)
    again = block_bootstrap(DELTAS, blocks=2, resamples=200, seed=7)
    other = block_bootstrap(DELTAS, blocks=2, resamples=200, seed=8)
    assert first == again, "the same seed and inputs give the same report"
    assert first.interval_eur != other.interval_eur
    assert (
        resample_means(DELTAS, block_length=2, resamples=3, seed=7)
        == resample_means(DELTAS, block_length=2, resamples=3, seed=7)
    )


def test_blocks_are_consecutive_windows_never_single_window_draws() -> None:
    # A block covering every window contains every value, so every resample mean is the sample mean:
    # a variance of zero proves the draw takes whole consecutive blocks, not IID windows.
    whole = block_bootstrap([0.0, 0.0, 0.0, 0.0, 9.0, 9.0], blocks=6, resamples=50, seed=3)
    assert whole.interval_eur == pytest.approx((3.0, 3.0))
    assert whole.mean_eur == pytest.approx(3.0)
    assert whole.block_length == 6
    # One-window blocks are the IID draw the report never uses by default, and they do vary.
    spread = block_bootstrap([0.0, 0.0, 0.0, 0.0, 9.0, 9.0], blocks=1, resamples=50, seed=3)
    assert spread.block_length == 1
    assert spread.interval_low_eur < spread.mean_eur < spread.interval_high_eur
    means = resample_means([1.0, 2.0, 3.0], block_length=2, resamples=20, seed=1)
    assert means and all(1.0 <= mean <= 3.0 for mean in means)


def test_the_interval_is_the_percentile_of_the_resample_means() -> None:
    result = block_bootstrap(
        DELTAS, blocks=2, resamples=400, seed=11, horizon_hours=0.05, window_seconds=180.0
    )
    assert (result.samples, result.resamples, result.block_length, result.seed) == (5, 400, 2, 11)
    assert result.confidence == pytest.approx(0.95)
    assert result.block_length_horizon_windows == 1
    assert result.degenerate is False and result.reason == ""
    assert result.mean_eur == pytest.approx(sum(DELTAS) / len(DELTAS))
    assert result.total_eur == pytest.approx(sum(DELTAS))
    low, high = result.interval_eur
    assert low <= result.mean_eur <= high
    assert low < high and result.standard_error_eur > 0
    means = resample_means(DELTAS, block_length=2, resamples=400, seed=11)
    assert low == pytest.approx(percentile(means, 2.5))
    assert high == pytest.approx(percentile(means, 97.5))


def test_constant_windows_still_report_a_real_point_interval() -> None:
    result = block_bootstrap([0.5, 0.5, 0.5], blocks=2, resamples=100, seed=5)
    assert result.degenerate is False
    assert result.interval_eur == pytest.approx((0.5, 0.5))
    assert result.standard_error_eur == pytest.approx(0.0)


def test_a_single_window_is_reported_not_dressed_up_as_an_interval() -> None:
    result = block_bootstrap([3.0], blocks=4, resamples=100, seed=0)
    assert result.degenerate is True and result.reason == SINGLE_WINDOW
    assert result.samples == 1
    assert result.mean_eur == pytest.approx(3.0) and result.total_eur == pytest.approx(3.0)
    assert result.interval_eur is None and result.standard_error_eur is None
    assert result.block_length == 1


def test_no_windows_and_no_resamples_are_named() -> None:
    empty = block_bootstrap([], blocks=4, resamples=100, seed=0)
    assert empty.degenerate is True and empty.reason == NO_WINDOWS
    assert empty.samples == 0 and empty.mean_eur is None and empty.total_eur is None
    assert empty.interval_eur is None
    none_drawn = block_bootstrap(DELTAS, blocks=2, resamples=0, seed=0)
    assert none_drawn.degenerate is True and none_drawn.reason == NO_RESAMPLES
    assert none_drawn.interval_eur is None and none_drawn.mean_eur == pytest.approx(sum(DELTAS) / 5)


def test_withheld_windows_are_excluded_and_counted() -> None:
    paired = pair([1.0, 2.0, 3.0], withheld={1})
    result = bootstrap_paired(paired, blocks=2, resamples=50, seed=1)
    assert paired.bootstrap_samples == (1.0, 3.0)
    assert result.samples == 2 and result.windows_total == 3 and result.excluded_windows == 1
    assert result.degenerate is False
    assert result.mean_eur == pytest.approx(2.0), (
        "the mean is the per-window rate the interval describes, over the included windows"
    )
    assert result.total_eur is None, (
        "one excluded window makes the sum a partial total, which must never print as the arm's "
        "own (excluded_windows and windows_total keep the coverage visible)"
    )


def test_a_wholly_withheld_arm_has_no_samples_at_all() -> None:
    result = bootstrap_paired(pair([1.0, 2.0], withheld={0, 1}), blocks=2, resamples=50, seed=1)
    assert result.samples == 0 and result.excluded_windows == 2 and result.windows_total == 2
    assert result.degenerate is True and result.reason == NO_WINDOWS
    assert result.interval_eur is None and result.mean_eur is None


def test_a_one_window_arm_is_single_window_degenerate() -> None:
    result = bootstrap_paired(pair([1.5, 2.5], withheld={1}), blocks=2, resamples=50, seed=1)
    assert result.samples == 1 and result.excluded_windows == 1
    assert result.degenerate is True and result.reason == SINGLE_WINDOW
    assert result.mean_eur == pytest.approx(1.5) and result.interval_eur is None


def test_percentile_is_interpolated_and_monotone() -> None:
    assert percentile([4.0, 1.0, 2.0, 3.0], 0.0) == pytest.approx(1.0)
    assert percentile([4.0, 1.0, 2.0, 3.0], 100.0) == pytest.approx(4.0)
    assert percentile([4.0, 1.0, 2.0, 3.0], 50.0) == pytest.approx(2.5)
    assert percentile([9.0], 42.0) == pytest.approx(9.0)
    assert percentile([], 50.0) is None


# ------------------------------------------------- the block length against the plan horizon


def test_the_horizon_implied_block_length_is_derived_not_assumed() -> None:
    """The correlation the blocks stand for is the plan horizon they overlap."""
    assert plan_horizon_windows(30.0, 57.0) == 1895, "ceil(30 h / 57 s)"
    assert plan_horizon_windows(0.25, 900.0) == 1
    assert plan_horizon_windows(None, 60.0) is None
    assert plan_horizon_windows(30.0, None) is None
    assert plan_horizon_windows(0.0, 60.0) is None, "a missing horizon is not a licence to guess"


def test_an_episode_shorter_than_one_plan_horizon_is_underpowered() -> None:
    """A tight interval on a short window is reported, labelled, and never called evidence."""
    short = block_bootstrap(DELTAS, blocks=4, resamples=200, seed=0, horizon_hours=30.0, window_seconds=57.0)
    assert short.underpowered is True and short.publishable is False
    assert short.reason == SAMPLE_SHORTER_THAN_PLAN_HORIZON
    assert short.block_length_horizon_windows == 1895
    assert short.block_length == 4, "the declared knob is what is drawn, not a silent shortening"
    assert short.resamples == 200 and short.seed == 0
    assert short.mean_eur is not None and short.total_eur is not None
    assert short.interval_eur is not None and "not evidence" in short.note
    long = block_bootstrap(DELTAS, blocks=4, resamples=200, seed=0, horizon_hours=1.0, window_seconds=57.0)
    assert long.block_length_horizon_windows == 64 and long.underpowered is True
    covered = block_bootstrap(
        [0.0] * 200,
        blocks=6,
        resamples=50,
        seed=0,
        horizon_hours=0.1,
        window_seconds=60.0,
    )
    assert covered.block_length_horizon_windows == 6 and covered.publishable is True
    assert covered.reason == "" and covered.note == ""


def test_the_window_length_comes_from_the_arm_own_windows() -> None:
    """The horizon check uses the cadence the episode ran at, not a declared constant."""
    windows = [PairedWindow(0, START, START + timedelta(seconds=60), 0.0, 0.0, 0.0, False, ()),
               PairedWindow(1, START, START + timedelta(seconds=120), 0.0, 0.0, 0.0, False, ())]
    assert mean_window_seconds(windows) == pytest.approx(90.0)
    assert mean_window_seconds([]) is None
    paired = PairedDelta(
        change="tariff_model",
        windows=tuple(windows),
        delta_eur=0.0,
        delta_with_terminal_eur=None,
        terminal_delta_eur=None,
        included_windows=2,
        withheld_windows=(),
    )
    checked = bootstrap_paired(paired, blocks=2, resamples=50, seed=0, horizon_hours=1.0)
    assert checked.window_seconds == pytest.approx(90.0), "derived from the arm's own windows"
    assert checked.block_length_horizon_windows == 40


# ------------------------------------------------------------- review finding 6 (publishability)


def test_finding_6_a_short_drawn_block_is_not_publishable() -> None:
    """A 4-window block on a 300-window sample claims a correlation it cannot carry."""
    short = block_bootstrap(
        [0.0] * 300, blocks=4, resamples=50, seed=0, horizon_hours=1.0, window_seconds=60.0
    )
    assert short.block_length_horizon_windows == 60 and short.samples == 300
    assert short.publishable is False and short.reason == BLOCK_SHORTER_THAN_PLAN_HORIZON
    assert short.underpowered is False, "the sample is long enough; the drawn length is the problem"
    assert "not evidence" in short.note and short.interval_eur is not None
    enough = block_bootstrap(
        [0.0] * 300, blocks=60, resamples=50, seed=0, horizon_hours=1.0, window_seconds=60.0
    )
    assert enough.block_length == 60 and enough.publishable is True and enough.reason == ""


def test_finding_6_five_horizons_of_windows_are_the_minimum_sample() -> None:
    """299 windows at a 60-window horizon is one horizon short of the declared five."""
    few = block_bootstrap(
        [0.0] * 299, blocks=60, resamples=50, seed=0, horizon_hours=1.0, window_seconds=60.0
    )
    assert few.publishable is False and few.reason == SAMPLE_SHORTER_THAN_EVIDENCE_HORIZONS
    assert few.evidence_horizons == 5 and "horizons" in few.note
    declared = block_bootstrap(
        [0.0] * 300,
        blocks=60,
        resamples=50,
        seed=0,
        horizon_hours=1.0,
        window_seconds=60.0,
        evidence_horizons=1,
    )
    assert declared.publishable is True


def test_finding_6_a_gap_in_the_windows_is_not_a_contiguous_sample() -> None:
    """Blocks drawn across a hole in the sample do not represent the window they claim."""
    holed = block_bootstrap(
        [0.0] * 300,
        blocks=60,
        resamples=50,
        seed=0,
        horizon_hours=1.0,
        window_seconds=60.0,
        gap_count=1,
    )
    assert holed.publishable is False and holed.reason == WINDOWS_NOT_CONTIGUOUS
    assert holed.gap_count == 1


def test_finding_6_an_undeclared_horizon_is_not_publishable() -> None:
    """Without a declared plan horizon the implied block length is unknown, so nothing is claimed."""
    unknown = block_bootstrap([0.0] * 300, blocks=60, resamples=50, seed=0)
    assert unknown.publishable is False and unknown.reason == HORIZON_UNKNOWN
    assert unknown.block_length_horizon_windows is None


def test_finding_13_a_gap_between_included_windows_is_not_evidence() -> None:
    """Blocks drawn across a withheld window span time that is not in the sample."""
    result = bootstrap_paired(
        pair([1.0] * 70, withheld=(35,)),
        blocks=60,
        resamples=50,
        seed=0,
        horizon_hours=1.0,
        window_seconds=3600.0,
    )
    assert result.samples == 69 and result.excluded_windows == 1
    assert result.gap_count == 1 and result.publishable is False
    assert result.reason == WINDOWS_NOT_CONTIGUOUS
    assert "contiguous" in result.note
    # The gap is named, not only counted: a reader can find window 36 in the report's own list and
    # see that its predecessor (35) was withheld, instead of having to trust the number.
    assert result.gap_windows == (36,)
    assert gap_windows(pair([1.0] * 4, withheld=(1,)).windows) == (2,)


def test_finding_14b_the_gap_count_names_interior_breaks_only() -> None:
    """A hole the sample has is named; the circular draw's wrap is not misreported as one."""
    # A withheld *leading* or *trailing* window does not break an interior run: the wrap from the
    # last window back to the first is intrinsic to the circular draw, so naming it would make every
    # sample of two or more windows non-contiguous and the flag would carry no information.
    assert gap_windows(pair([1.0] * 5, withheld=(0,)).windows) == ()
    assert gap_windows(pair([1.0] * 5, withheld=(4,)).windows) == ()
    assert gap_windows(pair([1.0] * 5, withheld=(1, 3)).windows) == (2, 4)
    # A window that is absent from the pair altogether is a hole in the sample too: the included
    # window after it starts a new run, exactly like one after a withheld window.
    present = [window for window in pair([1.0] * 5, withheld=()).windows if window.index != 2]
    assert gap_windows(present) == (3,)
    # One included window cannot start a run, and a contiguous sample starts none.
    assert gap_windows(pair([1.0], withheld=()).windows) == ()
    assert gap_windows(pair([1.0] * 5, withheld=()).windows) == ()


def test_finding_14b_the_evidence_requirement_states_both_floors() -> None:
    """The printed bar names the declared horizons too, not only the horizon-implied length."""
    assert "evidence_horizons" in EPISODE_EVIDENCE_REQUIREMENT
    assert "5 by default" in EPISODE_EVIDENCE_REQUIREMENT
    assert "contiguous" in EPISODE_EVIDENCE_REQUIREMENT
    assert "ceil(horizon_seconds / window_seconds)" in EPISODE_EVIDENCE_REQUIREMENT
    few = block_bootstrap(
        [0.0] * 299, blocks=60, resamples=50, seed=0, horizon_hours=1.0, window_seconds=60.0
    )
    assert "5 by default" in few.note


def test_finding_13_the_interval_says_nothing_when_the_horizon_is_undeclared() -> None:
    """No declared horizon leaves the implied block length unknown, so no claim is publishable."""
    result = bootstrap_paired(
        pair([1.0] * 70, withheld=()),
        blocks=60,
        resamples=50,
        seed=0,
        horizon_hours=None,
    )
    assert result.block_length_horizon_windows is None
    assert result.evidence_horizons == 5
    assert result.publishable is False and result.reason == HORIZON_UNKNOWN
