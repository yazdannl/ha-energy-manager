"""Calendar-edge stamps must come back unplaceable, not as an escaping OverflowError.

The readers' tolerant form (``parsed_as_of_instant``) is what a migration seed and a scratch prune
use, and every caller guards only ``ValueError``. A stamp that constructs and then overflows while
being moved to UTC -- year 1 with a positive offset, year 9999 with a negative one -- must
therefore answer ``None``, never raise, or it aborts the whole truncation instead of treating one
row as unplaceable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "energy_manager"))

from app import store as store_module  # noqa: E402
from app.store import parsed_as_of_instant  # noqa: E402

import pytest  # noqa: E402


def test_a_calendar_edge_stamp_is_unplaceable_rather_than_an_overflow():
    assert parsed_as_of_instant("0001-01-01T00:00:00+00:01") is None
    assert parsed_as_of_instant("9999-12-31T23:59:59-00:01") is None


def test_the_same_instants_in_utc_are_still_placeable():
    """The refusal above is about the overflow, not about extreme years as such."""
    assert parsed_as_of_instant("0001-01-01T00:00:00+00:00") is not None
    assert parsed_as_of_instant("9999-12-31T23:59:59+00:00") is not None


def test_a_stamp_that_is_not_a_string_is_unplaceable_rather_than_a_fabricated_epoch():
    """A numeric stamp must not be read as Unix epoch seconds.

    The parser this code replaced fell back to ``from_iso``, which accepts a number as epoch
    seconds -- so a corrupted or mistyped row became an instant decades in the PAST and was
    therefore "old enough" for every boundary, i.e. trusted instead of refused. Nothing but the
    accepted string form is placeable, and a genuine absence (``NULL``) is still an absence.
    """
    assert parsed_as_of_instant(None) is None
    for value in (123, 0, 1_700_000_000, 12.5, b"2026-01-01T00:00:00+00:00", ["x"]):
        assert parsed_as_of_instant(value) is None, value
    with pytest.raises(ValueError):
        store_module._as_of_moment(1_700_000_000, "stored instant")


def test_the_strict_path_uses_the_same_contextual_valueerror_as_other_unreadable_stamps():
    try:
        store_module._as_of_moment("0001-01-01T00:00:00+00:01", "stored instant")
    except ValueError as exc:
        assert "not a readable timestamp" in str(exc)
    else:  # pragma: no cover - the explicit failure below is the point
        raise AssertionError("an overflowing stamp must be refused, not returned")
