"""Night-only load learning (SPEC.md section 7.2 tier 3b, 0.23.0).

The tier exists to break a circular dependency: the ordinary derived path needs a pv series, pv can
only be inferred from load, and an install whose inverter reports nothing has neither. Once the sun is
down, pv is zero by geometry, so grid and battery alone give exact consumption.

Two failure modes were hit while building it, and both are silent, so both are pinned here:

* treating the sun as "down" at the horizon rather than below it, which lets twilight diffuse light in
  as if it were consumption;
* filling the unobserved daytime with a shape normalised against the wrong mean, which makes the level
  wrong -- and, worse, makes the forecast *worse* on the hours that were actually measured, because the
  recent-window ``scale`` then corrects the error away by pulling the observed hours off their level.
"""

from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.sources.load import (  # noqa: E402
    DEFAULT_SHAPE,
    NIGHT_ELEVATION_MAX_DEG,
    _fill_assumed_shape,
    _hourly_values,
    _night_load_samples,
    _zone,
    build_profile,
    night_hours,
)

LATITUDE = 52.09
LONGITUDE = 5.12


def _hours(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(hours=index) for index in range(count)]


def _series(start: datetime, days: int, level: float, battery_at: int | None = None):
    """A house drawing ``level`` kW, with the battery charging 0.5 kW at one hour of the day."""
    grid: dict[datetime, float] = {}
    battery: dict[datetime, float] = {}
    for stamp in _hours(start, 24 * days):
        charging = 0.5 if battery_at is not None and stamp.hour == battery_at else 0.0
        grid[stamp] = level + charging
        battery[stamp] = charging
    return {"grid_power": grid, "battery": battery}


def test_night_is_below_the_horizon_not_at_it() -> None:
    """A tilted roof still collects diffuse light at zero elevation, so night must sit well below it.

    Pinned because the mistake is invisible: admitting an hour of twilight as "pv is zero" simply makes
    the learned consumption a little too high, at every hour of the day, forever.
    """
    from app.forecast import pv

    assert NIGHT_ELEVATION_MAX_DEG < 0.0
    for day in (datetime(2026, 6, 21, tzinfo=timezone.utc), datetime(2026, 12, 21, tzinfo=timezone.utc)):
        for stamp in _hours(day, 24):
            utc = stamp.astimezone(timezone.utc)
            day_of_year = utc.timetuple().tm_yday
            elevation = pv.sun_elevation_deg(
                math.radians(LATITUDE),
                pv.declination_rad(day_of_year),
                pv.hour_angle_rad(LONGITUDE, day_of_year, float(utc.hour)),
            )
            if stamp in night_hours([stamp], LATITUDE, LONGITUDE):
                assert elevation <= NIGHT_ELEVATION_MAX_DEG
            else:
                # Note what is *not* asserted: elevation > 0. The band between the horizon and the
                # threshold is deliberately excluded from night, and that exclusion is the point.
                assert elevation > NIGHT_ELEVATION_MAX_DEG


def test_night_hours_shrink_in_summer_and_grow_in_winter() -> None:
    """The window is real geometry, so it must respond to the season at a mid-latitude site."""
    june = datetime(2026, 6, 21, tzinfo=timezone.utc)
    december = datetime(2026, 12, 21, tzinfo=timezone.utc)
    summer = night_hours(_hours(june, 24), LATITUDE, LONGITUDE)
    winter = night_hours(_hours(december, 24), LATITUDE, LONGITUDE)
    assert 0 < len(summer) < len(winter) < 24
    assert 4 <= len(summer) <= 8, len(summer)
    assert 12 <= len(winter) <= 18, len(winter)


def test_night_samples_exclude_the_day_and_the_battery() -> None:
    """``load = grid - battery``, night hours only: the battery's charging is not consumption."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    series = _series(start, 14, level=1.0, battery_at=23)
    samples = _night_load_samples(series, LATITUDE, LONGITUDE)
    nights = night_hours(series["grid_power"], LATITUDE, LONGITUDE)
    assert samples
    assert {stamp for stamp, _ in samples} <= nights, "a daylight hour was admitted as pv-free"
    assert all(abs(value - 1.0) < 1e-9 for _, value in samples), (
        "the battery's charging leaked into the learned consumption"
    )


def test_night_samples_refuse_without_a_battery_flow() -> None:
    """Without a flow sample the grid reading includes cycling, which is not household load."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    series = _series(start, 14, level=1.0)
    assert _night_load_samples({"grid_power": series["grid_power"]}, LATITUDE, LONGITUDE) == []
    # And a night hour that has a grid sample but no battery sample is skipped, not read as zero.
    series["battery"].pop(next(iter(series["battery"])))
    samples = dict(_night_load_samples(series, LATITUDE, LONGITUDE))
    assert series["grid_power"].keys() - samples.keys()


def test_the_filled_day_averages_the_observed_level() -> None:
    """The assumption has to preserve the magnitude that was actually measured."""
    medians = [0.0] * 48
    p10s = [0.0] * 48
    p90s = [0.0] * 48
    for index in (0, 1, 2, 21, 22, 23, 24, 25, 26, 45, 46, 47):
        medians[index] = 1.0
        p10s[index] = 0.7
        p90s[index] = 1.4
    filled, low, high = _fill_assumed_shape(list(medians), list(p10s), list(p90s))
    assert not [value for value in filled if value <= 0.0], "an empty bucket survived as zero"
    assert all(filled[index] == 1.0 for index in (0, 21, 24, 47)), "an observed bucket was overwritten"
    assert all(low[index] == 0.7 for index in (0, 24)), "an observed p10 was overwritten"
    assert statistics.fmean(filled) == pytest.approx(1.0, abs=5e-3)
    assert all(value > 0.0 for value in low) and all(value > 0.0 for value in high)


def test_a_night_learned_profile_reproduces_the_measured_hours() -> None:
    """End to end: the level, and the hours that were observed, both come back unchanged.

    This is the assertion that caught the normalisation bug. The first version of the fill made a
    synthetic 1 kW house forecast 2.0 kW at night, because the scale factor was derived from a
    mostly-empty day and then clamped.
    """
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    series = _series(start, 14, level=1.0, battery_at=23)
    samples = _night_load_samples(series, LATITUDE, LONGITUDE)
    profile = build_profile(
        samples, now=start + timedelta(days=14), tz="UTC", assumed_daytime_shape=True
    )
    assert profile["assumed_daytime_shape"] is True
    assert profile["scale"] == pytest.approx(1.0, abs=0.02)
    assert profile["profile_mean"] == pytest.approx(1.0, abs=0.02)
    nights = night_hours(series["grid_power"], LATITUDE, LONGITUDE)
    values = _hourly_values(
        profile,
        first_hour=start,
        last_hour=start + timedelta(hours=23),
        zone=_zone("UTC"),
        base_kwh_per_day=0.0,
        temperature_c=None,
    )
    for stamp in nights:
        if stamp in values:
            assert values[stamp] == pytest.approx(1.0, abs=0.02), stamp
    # And the daytime is a plausible residential shape, not a flat copy of the night level.
    daytime = [values[start + timedelta(hours=hour)] for hour in range(3, 21)]
    assert max(daytime) > 1.2
    assert min(daytime) < 0.8
    assert DEFAULT_SHAPE[18] > DEFAULT_SHAPE[4], "the default shape should peak in the evening"


def test_the_flag_is_opt_in_and_leaves_every_other_tier_alone() -> None:
    """Without the flag nothing changes: a genuinely measured zero bucket stays a zero bucket."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    series = _series(start, 14, level=1.0)
    samples = _night_load_samples(series, LATITUDE, LONGITUDE)
    profile = build_profile(samples, now=start + timedelta(days=14), tz="UTC")
    assert profile["assumed_daytime_shape"] is False
    assert sum(1 for entry in profile["buckets"] if entry["median"] == 0.0) > 0
    assert "assumed_shape" not in profile
