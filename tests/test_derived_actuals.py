"""Derived load and pv written from what *is* measured (SPEC.md 7.2 tier 3b, 0.24.0).

An install can measure nothing but grid and battery. Without these columns the fine-tune head has no
labels at all on such a site -- ``pv_kw`` and ``load_kw`` stay empty forever -- which is the state that
makes a learned correction look perpetually "insufficient".

The sign convention is the part worth pinning. ``load = grid + pv - battery`` (grid positive = import,
battery positive = charging) rearranges to ``pv = load - grid + battery``, and getting a sign wrong
produces a forecast that is wrong by exactly twice the export, which looks like a plausible number.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app import engine as engine_module  # noqa: E402
from app.sources import load as load_module  # noqa: E402
from app.sources import solar as solar_module  # noqa: E402

MOMENT = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_the_engine_slot_cadence_matches_both_providers() -> None:
    """``_slot_forecast_at`` floors to the engine's slot minutes and looks the key up in a provider's.

    A silent divergence would not raise: every exact lookup would miss and fall back to the previous
    slot, so an inferred load would be an hour stale and nothing would say so.
    """
    assert engine_module.SLOT_MINUTES == load_module.SLOT_MINUTES
    assert engine_module.SLOT_MINUTES == solar_module.SLOT_MINUTES


class _FakeEngine:
    """Just enough of the engine to exercise ``_derived_actuals`` without a Home Assistant."""

    _derived_actuals = engine_module.Engine._derived_actuals
    _slot_forecast_at = engine_module.Engine._slot_forecast_at

    def __init__(self, load_kw: float | None, has_batteries: bool = True) -> None:
        import threading

        self._lock = threading.Lock()
        self._state = type("State", (), {"batteries": [object()] if has_batteries else []})()
        self._slot_forecasts = {
            "pv": {},
            "load": {},
            "price": {},
        }
        if load_kw is not None:
            self._slot_forecasts["load"][MOMENT] = load_kw

    # ``_derived_actuals`` only touches the lock, the battery list and the slot forecasts.
    def __getattr__(self, name):  # pragma: no cover - guards against a wrong assumption in a test
        raise AssertionError(f"the engine helper needed {name!r}, which this fake does not provide")


def _row(grid_kw, battery_kw, load_kw=None, pv_kw=None):
    return {
        "grid_kw": grid_kw,
        "battery_kw": battery_kw,
        "load_kw": load_kw,
        "pv_kw": pv_kw,
    }


def test_load_is_derived_only_when_pv_is_provably_zero() -> None:
    """The engine cannot know the sun is down from here; it takes the provider's word for the load.

    What it must not do is invent a load when the sampler *did* produce one.
    """
    engine = _FakeEngine(load_kw=0.4)
    out = engine._derived_actuals(_row(grid_kw=0.4, battery_kw=0.0, load_kw=0.41), MOMENT)
    assert out == {"pv_derived_kw": None, "load_derived_kw": None}, "a measured value was overridden"


def test_export_implies_production_above_the_house_load() -> None:
    """The identity, at the sign that matters: exporting 1 kW with a 0.5 kW house and a flat battery."""
    engine = _FakeEngine(load_kw=0.5)
    out = engine._derived_actuals(_row(grid_kw=-1.0, battery_kw=0.0), MOMENT)
    assert out["load_derived_kw"] == 0.5
    assert out["pv_derived_kw"] == 1.5, "pv = load - grid + battery, and grid is negative when exporting"


def test_charging_is_paid_for_by_the_roof_or_the_grid_not_counted_as_load() -> None:
    """A battery taking 1 kW while the house draws 0.5 kW and the grid imports nothing: pv = 1.5 kW."""
    engine = _FakeEngine(load_kw=0.5)
    out = engine._derived_actuals(_row(grid_kw=0.0, battery_kw=1.0), MOMENT)
    assert out["pv_derived_kw"] == 1.5, "the charge was attributed to the house instead of the roof"


def test_night_production_is_never_negative() -> None:
    """Importing at night with a known load must floor at zero rather than report negative pv.

    The consistent night row is import = load + charge: a 0.5 kW house, a 1 kW charge and 1.5 kW of
    import give ``pv = 0.5 - 1.5 + 1.0 = 0``. Asking for more import than that is over-determined, and
    the identity then goes negative, which is the case the floor exists for.
    """
    engine = _FakeEngine(load_kw=0.5)
    assert engine._derived_actuals(_row(grid_kw=1.5, battery_kw=1.0), MOMENT)["pv_derived_kw"] == 0.0
    out = engine._derived_actuals(_row(grid_kw=2.0, battery_kw=1.0), MOMENT)
    assert out["pv_derived_kw"] == 0.0, "an over-determined row produced negative production"


def test_an_unmeasured_battery_flow_refuses_instead_of_guessing() -> None:
    """With batteries configured but no flow reading, the cycling would land on the house or the roof."""
    engine = _FakeEngine(load_kw=0.5)
    out = engine._derived_actuals(_row(grid_kw=-1.0, battery_kw=None), MOMENT)
    assert out == {"pv_derived_kw": None, "load_derived_kw": None}
    # With no batteries configured at all, zero is the truth rather than an assumption.
    engine = _FakeEngine(load_kw=0.5, has_batteries=False)
    out = engine._derived_actuals(_row(grid_kw=-1.0, battery_kw=None), MOMENT)
    assert out["pv_derived_kw"] == 1.5


def test_a_missing_grid_reading_derives_nothing() -> None:
    """Nothing can be inferred without the one series that anchors the identity."""
    engine = _FakeEngine(load_kw=0.5)
    out = engine._derived_actuals(_row(grid_kw=None, battery_kw=0.0), MOMENT)
    assert out == {"pv_derived_kw": None, "load_derived_kw": None}


def test_the_nearest_earlier_slot_is_used_and_never_a_later_one() -> None:
    """A past observation may be built from an earlier forecast, never from a future one."""
    engine = _FakeEngine(load_kw=None)
    earlier = MOMENT - timedelta(minutes=30)
    engine._slot_forecasts["load"][earlier] = 0.7
    engine._slot_forecasts["load"][MOMENT + timedelta(minutes=15)] = 9.9
    assert engine._slot_forecast_at("load", MOMENT) == 0.7
    assert engine._slot_forecast_at("load", MOMENT - timedelta(hours=2)) is None


def test_measured_labels_win_over_derived_ones_in_the_head() -> None:
    """A fit must prefer the real measurement; the inferred series is only a fallback."""
    from app.forecast import local_head

    assert local_head._measured_column("solar") == "pv_kw"
    assert local_head._derived_column("solar") == "pv_derived_kw"
    assert local_head._derived_column("load") == "load_derived_kw"
    # An unknown kind has neither, so nothing is silently scored against an unrelated column.
    assert local_head._measured_column("price") is None
    assert local_head._derived_column("price") is None
