"""Learned round-trip efficiency (SPEC.md section 5, ``app/efficiency.py``).

The estimate must measure a *complete cycle*: a charge window and the discharge window that takes
the pack back down through the span the charge covered.  What these tests pin, in the order the bug
appeared live (2026-10-20, 88.5 % learned against a pack that delivers ~83 %):

* only a complete charge/discharge cycle counts -- a discharge that stops before it has covered the
  charge's span is rejected, never scaled into an efficiency;
* the comparison is AC energy *per SoC point*, so how deep the discharge went does not turn stored
  energy (or the state of charge the range happened to start on) into a loss: the same pack measures
  the same round trip whatever the depth;
* the pack must *hold* the energy between the two windows (a leak is not a conversion loss);
* the pair is the next window only, never any later discharge (a charge has one cycle);
* whole-percent SoC rounding is divided out instead of extrapolated.

Every synthetic leg here is physically consistent: a leg that moves ``dSoc`` points at
``eta_charge``/``eta_discharge`` takes exactly the time its AC energy needs at the given power, so
the samples never contradict the capacity the estimate implies.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app import efficiency as efficiency_learning  # noqa: E402
from app.capacity import extract_windows  # noqa: E402
from app.efficiency import estimate_round_trip, pair_windows, weighted_median  # noqa: E402

T0 = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
MAX_POWER = 1000.0
CAPACITY = 2.0
ROUND_TRIP = 0.8
ETA = ROUND_TRIP**0.5
#: The fixtures are physically consistent and rest the pack between legs, so a window's AC energy
#: pays for exactly its SoC span and the arithmetic below is exact (``extract_windows`` books no
#: energy for a sample that did not move the SoC).
REL = 1e-6


def leg(
    start: datetime,
    soc_from: float,
    soc_to: float,
    *,
    steps: int,
    capacity_kwh: float = CAPACITY,
    eta: float = ETA,
    minutes: int = 5,
) -> list[tuple[datetime, float, float]]:
    """One physically consistent run: ``(ts, power_w, soc_pct)``.

    Charging stores ``AC * eta`` and discharging delivers ``stored * eta`` (the AC-side convention of
    ``models.BatteryConfig``).  The run is ``steps`` samples long and its power is whatever moves that
    stored energy in exactly ``steps * minutes``, so the SoC ramp and the AC energy agree to the
    sample: the estimate is tested, not the sampling grid.
    """
    stored = capacity_kwh * abs(soc_to - soc_from) / 100.0
    ac_kwh = stored / eta if soc_to > soc_from else stored * eta
    hours = steps * minutes / 60.0
    power = ac_kwh / hours * 1000.0
    if soc_to < soc_from:
        power = -power
    return [
        (
            start + timedelta(minutes=minutes * index),
            power,
            soc_from + (soc_to - soc_from) * index / steps,
        )
        for index in range(steps + 1)
    ]


def idle(start: datetime, soc: float, *, minutes: int = 5) -> list[tuple[datetime, float, float]]:
    """A zero-power sample: the pack rests, which is how it really moves between a charge and a
    discharge (``extract_windows`` breaks a run on it, so no phantom energy is booked)."""
    return [(start, 0.0, soc)]


def cycle(
    start: datetime,
    *,
    low: float = 20.0,
    high: float = 80.0,
    steps: int = 16,
    hold_minutes: int = 0,
) -> list[tuple[datetime, float, float]]:
    """A full charge/discharge cycle: up from ``low`` to ``high`` and back down to ``low``."""
    charge = leg(start, low, high, steps=steps)
    rest = idle(charge[-1][0] + timedelta(minutes=5), high, minutes=5 + hold_minutes)
    discharge_start = rest[-1][0] + timedelta(minutes=5)
    discharge = leg(discharge_start, high, low, steps=steps)
    # The pack rests again afterwards, so both legs are measured with the same one-sample edge.
    return charge + rest + discharge + idle(discharge[-1][0] + timedelta(minutes=5), low)


def estimate(samples, **overrides) -> dict:
    options = dict(
        max_charge_w=MAX_POWER,
        max_discharge_w=MAX_POWER,
        now=T0 + timedelta(days=1),
    )
    options.update(overrides)
    return estimate_round_trip(samples, **options)


def windows_of(samples) -> list[dict]:
    accepted, _rejected = extract_windows(
        samples,
        max_charge_w=MAX_POWER,
        max_discharge_w=MAX_POWER,
        eta_charge=1.0,
        eta_discharge=1.0,
    )
    return accepted


# ------------------------------------------------------------------------------- pairing


def test_a_complete_cycle_measures_the_energy_it_delivered():
    """AC energy per SoC point out against AC energy per SoC point in: 0.8 for this pack."""
    pairs, rejected = pair_windows(windows_of(cycle(T0)))
    assert rejected["incomplete"] == 0 and rejected["drift"] == 0
    assert len(pairs) == 1
    assert pairs[0]["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL)
    assert pairs[0]["charge_dsoc_pct"] == pytest.approx(60.0, abs=0.1)
    assert pairs[0]["discharge_dsoc_pct"] == pytest.approx(60.0, abs=0.1)
    assert pairs[0]["deeper_pct"] == pytest.approx(0.0, abs=0.2)


def test_the_estimate_does_not_move_with_the_state_of_charge_the_range_started_on():
    """The live defect: a partial leg at either edge is stored energy, not a loss.

    The same pack is measured in two recordings -- one that is all complete cycles, one whose range
    begins mid-cycle (a charge the range cut in half) and ends mid-cycle (a discharge still running
    when the range ends).  The complete cycle must measure the same round trip in both, and neither
    partial leg may become a pair.
    """
    clean = estimate(cycle(T0) + cycle(T0 + timedelta(hours=12)) + cycle(T0 + timedelta(hours=24)))

    # A charge whose start is outside the range (40 -> 80), the discharge that covers it, then a
    # complete cycle, and finally a discharge the range cuts off before it has covered its charge.
    edge_charge = leg(T0, 40.0, 80.0, steps=10)
    edge_rest = idle(edge_charge[-1][0] + timedelta(minutes=5), 80.0)
    edge_discharge = leg(edge_rest[-1][0] + timedelta(minutes=5), 80.0, 20.0, steps=20)
    start = edge_discharge[-1][0] + timedelta(minutes=10)
    first = cycle(start)
    second = cycle(first[-1][0] + timedelta(minutes=10))
    cut_off = leg(second[-1][0] + timedelta(minutes=10), 80.0, 45.0, steps=8)
    cut_off = cut_off + idle(cut_off[-1][0] + timedelta(minutes=5), 45.0)
    noisy = estimate(
        edge_charge + edge_rest + edge_discharge + first + second + cut_off
    )

    assert clean["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL)
    assert noisy["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL)
    # The edge charge is still a measurement (its span was fully discharged); the trimmed discharge
    # at the end is not a pair at all.
    assert noisy["samples"] == 3
    assert noisy["rejected"]["pairs_incomplete"] == 0


def test_a_discharge_that_stops_before_the_charge_span_is_rejected():
    """A half discharge is not a loss measurement: it would only price the top of the pack."""
    charge = leg(T0, 20.0, 80.0, steps=16)
    partial = leg(charge[-1][0] + timedelta(minutes=10), 80.0, 45.0, steps=10)
    pairs, rejected = pair_windows(windows_of(charge + partial))
    assert pairs == []
    assert rejected["incomplete"] == 1
    # ...and the payload says so instead of inventing a number.
    payload = estimate(charge + partial)
    assert payload["samples"] == 0
    assert payload["round_trip"] is None
    assert payload["rejected"]["pairs_incomplete"] == 1


def test_a_deeper_discharge_is_a_cycle_and_its_extra_depth_is_not_charged_energy():
    """The live daily pattern: charge 30 -> 95, discharge 95 -> 10 (25 points deeper).

    The extra depth came out of energy that was in the pack before the charge, so comparing the two
    AC totals would report ``0.8 * 85/65 = 1.05`` (a pack that makes energy).  Per SoC point it is
    the same 0.8 whatever the depth.
    """
    charge = leg(T0, 30.0, 80.0, steps=12)
    deeper = leg(charge[-1][0] + timedelta(minutes=10), 80.0, 10.0, steps=18)
    pairs, rejected = pair_windows(windows_of(charge + deeper))
    assert rejected["incomplete"] == 0
    assert len(pairs) == 1
    assert pairs[0]["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL)
    assert pairs[0]["deeper_pct"] == pytest.approx(20.0, abs=0.2)


def test_a_pack_that_leaked_the_energy_between_the_windows_is_rejected():
    """The cycle has to hold its energy: a drift between the legs is not a conversion loss."""
    charge = leg(T0, 20.0, 80.0, steps=16)
    # 8 points of SoC disappear while the pack idles before discharging.
    drained = leg(charge[-1][0] + timedelta(minutes=10), 72.0, 20.0, steps=14)
    pairs, rejected = pair_windows(windows_of(charge + drained))
    assert pairs == [] and rejected["drift"] == 1


def test_only_the_next_window_is_a_cycle_partner():
    """Two cycles in a row: the first charge pairs with the first discharge, never the second."""
    samples = cycle(T0) + cycle(T0 + timedelta(hours=12))
    pairs, _rejected = pair_windows(windows_of(samples))
    assert len(pairs) == 2
    assert all(pair["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL) for pair in pairs)


def test_an_untracked_soc_window_is_invalid_not_guessed():
    """A window without its SoC endpoints (an older payload shape) cannot close a cycle."""
    charged = {
        "kind": "charge",
        "ac_kwh": 2.0,
        "dsoc_pct": 60.0,
        "start": T0,
        "end": T0 + timedelta(hours=2),
    }
    drained = {
        "kind": "discharge",
        "ac_kwh": 1.6,
        "dsoc_pct": 60.0,
        "start": T0 + timedelta(hours=2),
        "end": T0 + timedelta(hours=4),
    }
    pairs, rejected = pair_windows([charged, drained])
    assert pairs == [] and rejected["invalid"] == 1


def test_the_pair_is_energy_per_soc_point_not_two_totals():
    """Exact arithmetic, no extraction in the way: 1.2 kWh in, 0.96 kWh out, 50 points each."""
    charged = {
        "kind": "charge",
        "ac_kwh": 1.2,
        "dsoc_pct": 50.0,
        "soc_start_pct": 20.0,
        "soc_end_pct": 70.0,
        "start": T0,
        "end": T0 + timedelta(hours=2),
    }
    drained = {
        "kind": "discharge",
        "ac_kwh": 0.96,
        "dsoc_pct": 50.0,
        "soc_start_pct": 70.0,
        "soc_end_pct": 20.0,
        "start": T0 + timedelta(hours=2),
        "end": T0 + timedelta(hours=4),
    }
    pairs, _rejected = pair_windows([charged, drained])
    assert pairs[0]["round_trip"] == pytest.approx(0.8, rel=1e-12)
    # Depth does not enter the ratio: the same pack discharged 50 -> 10 (60 points) is still 0.8,
    # even though its AC total is 1.2x the charge's.
    deeper = dict(drained, ac_kwh=0.96 * 60 / 50, dsoc_pct=60.0, soc_end_pct=10.0)
    pairs, _rejected = pair_windows([charged, deeper])
    assert pairs[0]["round_trip"] == pytest.approx(0.8, rel=1e-12)
    assert pairs[0]["deeper_pct"] == pytest.approx(10.0)


# ------------------------------------------------------------------------------- payload


def test_the_estimate_needs_three_complete_cycles_before_it_is_published():
    one = estimate(cycle(T0))
    assert one["samples"] == 1 and one["round_trip"] is None
    assert one["median_round_trip"] == pytest.approx(ROUND_TRIP, rel=REL), "measured, not published"
    three = estimate(
        cycle(T0) + cycle(T0 + timedelta(hours=12)) + cycle(T0 + timedelta(hours=24))
    )
    assert three["samples"] == 3
    assert three["round_trip"] == pytest.approx(ROUND_TRIP, rel=REL)
    assert three["eta_charge"] == pytest.approx(ETA, rel=REL)
    # The diagnostic capacity: AC energy per SoC point taken in, times the span -> 2.0 kWh.
    assert three["capacity_kwh"] == pytest.approx(CAPACITY, rel=REL)


def test_an_implausible_round_trip_is_not_published():
    """1.4 kWh out of 1.0 kWh in is a broken measurement, not a >100 % pack."""
    charge = leg(T0, 20.0, 80.0, steps=16)
    # The same stored energy "delivered" at an impossible efficiency: 1.4 kWh stored out of a 2.0 kWh
    # pack as 1.68 kWh AC, i.e. a round trip of 1.2.
    impossible = leg(charge[-1][0] + timedelta(minutes=10), 80.0, 20.0, steps=16, eta=1.2)
    payload = estimate(charge + impossible, max_discharge_w=2000.0, max_charge_w=2000.0)
    assert payload["samples"] == 0 and payload["round_trip"] is None


def test_the_weighted_median_is_the_span_weighted_middle():
    assert weighted_median([(0.8, 10.0)]) == pytest.approx(0.8)
    assert weighted_median([(0.7, 1.0), (0.9, 100.0)]) == pytest.approx(0.9)
    assert weighted_median([(0.7, 1.0), (0.9, 1.0)]) == pytest.approx(0.8)
    assert weighted_median([(0.7, 0.0)]) is None
    assert weighted_median([]) is None


def test_the_payload_is_stamped_with_the_current_schema():
    """A payload is only trusted by ``round_trip_status`` when it carries the current formula."""
    payload = estimate(
        cycle(T0) + cycle(T0 + timedelta(hours=12)) + cycle(T0 + timedelta(hours=24))
    )
    assert payload["schema"] == efficiency_learning.SCHEMA
    # 3: complete cycles only, scored per SoC point, on windows that book only SoC-moving energy.
    assert efficiency_learning.SCHEMA == 3
