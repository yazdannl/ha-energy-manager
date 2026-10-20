"""Self-learned battery round-trip efficiency (SPEC.md section 5).

A pack's datasheet round trip is rarely what the pack delivers: conversion losses, the BMS
balancing draw and an aged cell all move it, and the number is what every plan decision depends on
(the DP's charge/discharge transitions, the salvage value, the break-even sale price, the wear per
delivered kWh and the cost-basis sell floor).  So it is measured from the pack's own cycles rather
than trusted from a form field.

The estimate uses the same accepted windows as :mod:`app.capacity` (one maximal same-direction run
of 5-minute rows whose SoC moved enough and fast enough to be trusted), but it does **not** compare
the whole range's charge with the whole range's discharge: that ratio moves whenever the range
starts or ends with the pack fuller or emptier than it finished (a partial leg at either edge is not
a loss, it is stored energy), so the same pack would learn a different efficiency every day.

Only a **complete cycle** is measured: a charge window immediately followed by the discharge window
that returns the pack to where the charge started, within
:data:`FULL_CYCLE_TOLERANCE_FRACTION` of the charge's own SoC span (and
:data:`FULL_CYCLE_TOLERANCE_PCT` points, because a BMS reports whole percent), and with the pack
holding that energy between the two windows (drift at most :data:`HOLD_DRIFT_TOLERANCE_FRACTION` /
:data:`HOLD_DRIFT_TOLERANCE_PCT`).  A longer discharge than the charge, or a shorter one, is
*rejected*: extrapolating one leg onto the other's span assumes the SoC-to-energy curve is exactly
linear and silently converts a partial cycle into an invented measurement.

For a complete cycle with charge AC energy ``Ec`` over ``dSoc_c`` and discharge AC energy ``Ed`` over
``dSoc_d`` (both spans now equal to within the tolerance):

* round trip: ``eta_c * eta_d = (Ed / dSoc_d) / (Ec / dSoc_c)`` -- AC energy *per SoC point*
  delivered against AC energy per SoC point taken in.  The capacity cancels exactly, the residual
  span mismatch (whole-percent rounding, the windows' thresholds) is divided out instead of being
  extrapolated, and the state of charge the range happened to start on drops out entirely;
* capacity: ``C = sqrt(Ec * Ed / (dSoc_c * dSoc_d))`` -- reported for diagnostics (and used to
  reject a pair whose energy could not come from a plausible pack).

Pairs are weighted by ``min(dSoc_c, dSoc_d)`` (a bigger SoC move is a more precise measurement, and
the BMS reports whole percent), outliers are dropped by a median-absolute-deviation filter, and the
combination is a weighted median.  The learned round trip is then split evenly into
``eta_charge = eta_discharge = sqrt(round_trip)``, the same convention the UI and ``BatteryConfig``
use for a typed round trip, and published only after at least three accepted pairs.

Everything here is pure except :func:`learn_round_trip`, which writes the profile through the store
it is handed; a missing or unreadable store is not an error, only "no estimate yet".
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from . import capacity as capacity_learning
from .models import BatteryConfig, from_iso, to_iso, utcnow

__all__ = [
    "FULL_CYCLE_TOLERANCE_FRACTION",
    "FULL_CYCLE_TOLERANCE_PCT",
    "HOLD_DRIFT_TOLERANCE_FRACTION",
    "HOLD_DRIFT_TOLERANCE_PCT",
    "MAX_KEPT_PAIRS",
    "MAX_PAIR_GAP_DAYS",
    "MIN_ACCEPTED_PAIRS",
    "MIN_PLAUSIBLE_CAPACITY_KWH",
    "MIN_PLAUSIBLE_ROUND_TRIP",
    "MAX_PLAUSIBLE_ROUND_TRIP",
    "PROFILE_KIND",
    "SCHEMA",
    "REASON_APPLIED",
    "REASON_LOW_CONFIDENCE",
    "REASON_PINNED",
    "REASON_STALE",
    "REASON_UNAVAILABLE",
    "STALE_AFTER_DAYS",
    "estimate_round_trip",
    "pair_windows",
    "learn_round_trip",
    "load_estimate",
    "round_trip_status",
    "weighted_median",
]

#: Store ``learning_profiles`` kind for the per-battery estimate; the key is the battery id.
PROFILE_KIND = "battery_efficiency"

#: Payload schema.  Schema 1 paired *any* later discharge with a charge and scaled by the SoC-span
#: ratio, so a partial cycle was extrapolated into an efficiency; schema 2 already required a complete
#: cycle but still scored the two AC totals over differing depths; schema 3 scores AC energy per SoC
#: point on a complete cycle over windows whose energy only pays for SoC movement (see
#: :data:`app.capacity.SCHEMA`).  A payload from an older schema is reported but not trusted, and the
#: typed value stands until the next maintenance relearn rewrites it.
SCHEMA = 3

#: At least this many accepted charge/discharge pairs before the estimate is published.
MIN_ACCEPTED_PAIRS = 3
#: Accepted pairs kept in the stored payload (the combination only needs their medians).
MAX_KEPT_PAIRS = 20
#: A pair is only a cycle when the discharge immediately follows the charge; a week apart is still
#: one pack (the windows are consecutive, so this only bounds the idle time between them).
MAX_PAIR_GAP_DAYS = 7.0
#: A discharge only closes a *complete* cycle when it returns the pack to the SoC the charge started
#: on: within this fraction of the charge's own span, or this many points when the span is small.
FULL_CYCLE_TOLERANCE_FRACTION = 0.15
FULL_CYCLE_TOLERANCE_PCT = 2.0
#: Between the two windows the pack must hold the energy: this much drift is measurement, more is a
#: leak (or a cycle the extraction split), so the pair is not a measurement of the round trip.
HOLD_DRIFT_TOLERANCE_FRACTION = 0.10
HOLD_DRIFT_TOLERANCE_PCT = 1.0
#: An estimate older than this is stale and the typed round trip wins again.
STALE_AFTER_DAYS = 30.0
#: Plausible AC-to-AC round trip for a home battery; anything outside is a bad measurement.
MIN_PLAUSIBLE_ROUND_TRIP = 0.50
MAX_PLAUSIBLE_ROUND_TRIP = 1.0
#: A pair whose implied pack is outside this envelope measured something other than the pack.
MIN_PLAUSIBLE_CAPACITY_KWH = 1.0
MAX_PLAUSIBLE_CAPACITY_KWH = 200.0

#: Stable reason slugs reported wherever the effective efficiency is exposed (SPEC 5/10).
REASON_APPLIED = "efficiency_learned"
REASON_STALE = "efficiency_learned_stale"
REASON_LOW_CONFIDENCE = "efficiency_learned_low_confidence"
REASON_UNAVAILABLE = "efficiency_learned_unavailable"
REASON_PINNED = "efficiency_typed"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Weighted median of ``(value, weight)`` pairs; ``None`` when nothing usable is left.

    A zero or negative weight is ignored, and a single pair is its own median.
    """
    usable = sorted(
        (
            (float(value), float(weight))
            for value, weight in pairs
            if _finite(value) is not None and _finite(weight) is not None and float(weight) > 0.0
        ),
        key=lambda item: item[0],
    )
    if not usable:
        return None
    total = sum(weight for _, weight in usable)
    halfway = total / 2.0
    running = 0.0
    for index, (value, weight) in enumerate(usable):
        running += weight
        if running >= halfway:
            # An exact half split repeats the classic median: the mean of the two middle values.
            if running == halfway and index + 1 < len(usable):
                return (value + usable[index + 1][0]) / 2.0
            return value
    return usable[-1][0]


def pair_windows(
    windows: Sequence[Mapping[str, Any]],
    *,
    max_gap_days: float = MAX_PAIR_GAP_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Turn accepted capacity windows into *complete-cycle* pairs.

    Returns ``(accepted, rejected)``.  Each accepted pair is
    ``{round_trip, capacity_kwh, charge_dsoc_pct, discharge_dsoc_pct, deeper_pct, weight, at}``.
    Only two *consecutive* windows are ever a candidate -- a charge and the discharge that follows
    it -- and the discharge must cover the charge's whole span: it has to bring the pack back to (or
    past) the SoC the charge started from, within :data:`FULL_CYCLE_TOLERANCE_FRACTION` of the
    charge's span (or :data:`FULL_CYCLE_TOLERANCE_PCT` points), with the pack holding that energy in
    between (drift within :data:`HOLD_DRIFT_TOLERANCE_FRACTION` / :data:`HOLD_DRIFT_TOLERANCE_PCT`).
    A discharge that stops higher is a half cycle and is rejected; a deeper one is fine, because the
    comparison is per SoC point and the extra depth is never counted as energy the charge put in.

    Comparing a charge with an *unrelated* later discharge, or scaling one leg's span onto the
    other's, is what makes a partial cycle look like a measurement: the pack ends the range fuller
    or emptier than it started and the ratio absorbs stored energy as if it were a loss.  Such a
    candidate is rejected (``pairs_incomplete`` / ``pairs_drift``) rather than extrapolated, so the
    estimate does not move when a range happens to start or end mid-cycle.
    """
    accepted: list[dict[str, Any]] = []
    rejected = {"order": 0, "gap": 0, "drift": 0, "incomplete": 0, "invalid": 0}
    ordered = sorted(
        (window for window in windows if isinstance(window.get("start"), datetime)),
        key=lambda window: window["start"],
    )
    for charged, drained in zip(ordered, ordered[1:]):
        if str(charged.get("kind")) != "charge" or str(drained.get("kind")) != "discharge":
            rejected["order"] += 1
            continue
        ec = _finite(charged.get("ac_kwh"))
        ed = _finite(drained.get("ac_kwh"))
        dsoc_c = _finite(charged.get("dsoc_pct"))
        dsoc_d = _finite(drained.get("dsoc_pct"))
        soc_start_c = _finite(charged.get("soc_start_pct"))
        soc_end_c = _finite(charged.get("soc_end_pct"))
        soc_start_d = _finite(drained.get("soc_start_pct"))
        soc_end_d = _finite(drained.get("soc_end_pct"))
        if (
            ec is None
            or ed is None
            or dsoc_c is None
            or dsoc_d is None
            or soc_start_c is None
            or soc_end_c is None
            or soc_start_d is None
            or soc_end_d is None
        ):
            rejected["invalid"] += 1
            continue
        if (drained["start"] - charged["end"]).total_seconds() > max_gap_days * 86400.0:
            rejected["gap"] += 1
            continue
        if ec <= 0 or ed <= 0 or dsoc_c <= 0 or dsoc_d <= 0:
            rejected["invalid"] += 1
            continue
        # The pack must still be where the charge left it: energy that leaked away between the two
        # windows is neither charged nor discharged, so a pair that drifted measures something else.
        drift = abs(float(soc_start_d) - float(soc_end_c))
        if drift > max(HOLD_DRIFT_TOLERANCE_PCT, HOLD_DRIFT_TOLERANCE_FRACTION * dsoc_c):
            rejected["drift"] += 1
            continue
        # ...and the discharge must cover the charge's whole span: it has to take the pack back to
        # (or past) the SoC the charge started from.  A discharge that stops higher is a half cycle,
        # and measuring the round trip on it would only price the top of the pack.
        tolerance = max(FULL_CYCLE_TOLERANCE_PCT, FULL_CYCLE_TOLERANCE_FRACTION * dsoc_c)
        deeper = float(soc_start_c) - float(soc_end_d)
        if deeper < -tolerance:
            rejected["incomplete"] += 1
            continue
        fraction_c = dsoc_c / 100.0
        fraction_d = dsoc_d / 100.0
        # AC energy per SoC point, delivered against taken in: the capacity cancels, and the residual
        # span mismatch is divided out instead of being extrapolated onto the other leg.
        round_trip = (ed / fraction_d) / (ec / fraction_c)
        implied_capacity = math.sqrt(ec * ed / (fraction_c * fraction_d))
        if not (
            math.isfinite(round_trip)
            and math.isfinite(implied_capacity)
            and MIN_PLAUSIBLE_ROUND_TRIP <= round_trip <= MAX_PLAUSIBLE_ROUND_TRIP
            and MIN_PLAUSIBLE_CAPACITY_KWH
            <= implied_capacity
            <= MAX_PLAUSIBLE_CAPACITY_KWH
        ):
            rejected["invalid"] += 1
            continue
        accepted.append(
            {
                "round_trip": round_trip,
                "capacity_kwh": implied_capacity,
                "charge_dsoc_pct": round(dsoc_c, 3),
                "discharge_dsoc_pct": round(dsoc_d, 3),
                # How much deeper than the charge the discharge went (0 = a closed cycle).  The
                # per-SoC-point ratio means that extra depth is not counted as charged energy.
                "deeper_pct": round(deeper, 3),
                "weight": min(dsoc_c, dsoc_d),
                "at": to_iso(drained["start"]),
            }
        )
    return accepted, rejected


def estimate_round_trip(
    samples: Sequence[tuple[datetime, float, float]],
    *,
    max_charge_w: float,
    max_discharge_w: float,
    sample_minutes: float = capacity_learning.DEFAULT_SAMPLE_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Robust round-trip payload from measurement samples; never raises and never guesses.

    ``round_trip`` is ``None`` until at least :data:`MIN_ACCEPTED_PAIRS` pairs were accepted; the
    payload still reports how many were, so the UI can say "2 of 3" instead of "--".
    """
    windows, rejected_windows = capacity_learning.extract_windows(
        samples,
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
        # The windows' own capacity field is not used here: a pair measures the round trip without
        # believing a capacity, so a neutral efficiency keeps the extraction free of the typed one.
        eta_charge=1.0,
        eta_discharge=1.0,
        sample_minutes=sample_minutes,
    )
    pairs, rejected_pairs = pair_windows(windows)
    values = [pair["round_trip"] for pair in pairs]
    mask = capacity_learning.mad_filter(values)
    kept = [pair for pair, keep in zip(pairs, mask) if keep]
    round_trip = weighted_median(
        [(pair["round_trip"], pair["weight"]) for pair in kept]
    )
    if round_trip is not None and not (
        MIN_PLAUSIBLE_ROUND_TRIP <= round_trip <= MAX_PLAUSIBLE_ROUND_TRIP
    ):
        round_trip = None
    count = len(pairs)
    applied_value = round_trip if count >= MIN_ACCEPTED_PAIRS else None
    eta = math.sqrt(applied_value) if applied_value is not None else None
    implied_capacity = capacity_learning.robust_median(
        [pair["capacity_kwh"] for pair in kept]
    )
    rejected = {f"windows_{key}": value for key, value in rejected_windows.items()}
    rejected.update({f"pairs_{key}": value for key, value in rejected_pairs.items()})
    return {
        "round_trip": applied_value,
        "median_round_trip": round_trip,
        "eta_charge": eta,
        "eta_discharge": eta,
        "capacity_kwh": implied_capacity,
        "samples": count,
        "pairs": kept[-MAX_KEPT_PAIRS:],
        "rejected": rejected,
        "schema": SCHEMA,
        "updated_at": to_iso(now or utcnow()),
    }


def round_trip_status(
    payload: Mapping[str, Any] | None,
    now: datetime,
    *,
    stale_after_days: float = STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Classify a stored payload into ``{round_trip, eta_charge, ..., reason, applied}``.

    ``applied`` is True only for a finite estimate with enough pairs that is not stale; the
    ``reason`` slug is stable and is what the API/UI/log report when a learned value is used or
    rejected.
    """
    moment = now if now.tzinfo else now.replace(tzinfo=utcnow().tzinfo)
    if not payload:
        return _status(None, 0, None, REASON_UNAVAILABLE, moment)
    value = _finite(payload.get("round_trip"))
    try:
        count = int(payload.get("samples") or 0)
    except (TypeError, ValueError):
        count = 0
    updated = from_iso(payload.get("updated_at"))
    if _finite(payload.get("schema")) != float(SCHEMA):
        # An estimate from an older pair formula is reported but never applied.
        reason = REASON_LOW_CONFIDENCE
    elif value is None or count < MIN_ACCEPTED_PAIRS:
        reason = REASON_LOW_CONFIDENCE
    elif updated is None:
        reason = REASON_STALE
    else:
        age = (moment - updated).total_seconds()
        reason = REASON_STALE if age > stale_after_days * 86400.0 else REASON_APPLIED
    return _status(value, count, updated, reason, moment)


def _status(
    value: float | None,
    count: int,
    updated: datetime | None,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    applied = reason == REASON_APPLIED
    age_days = None if updated is None else max(0.0, (now - updated).total_seconds() / 86400.0)
    eta = math.sqrt(value) if applied and value is not None else None
    return {
        "round_trip": value if applied else None,
        "learned_round_trip": value,
        "eta_charge": eta,
        "eta_discharge": eta,
        "samples": count,
        "updated_at": to_iso(updated),
        "age_days": None if age_days is None else round(age_days, 3),
        "stale": reason == REASON_STALE,
        "confident": value is not None and count >= MIN_ACCEPTED_PAIRS,
        "applied": applied,
        "reason": reason,
    }


def load_estimate(store: Any, battery_id: str) -> dict[str, Any] | None:
    """Read the stored payload for one battery; ``None`` without a store or a saved estimate."""
    getter = getattr(store, "profile_get", None)
    if not callable(getter):
        return None
    try:
        stored = getter(PROFILE_KIND, battery_id)
    except Exception:  # noqa: BLE001 - an unreadable profile is "no estimate", never a crash
        return None
    if isinstance(stored, tuple) and len(stored) == 2:
        stored = stored[1]
    return dict(stored) if isinstance(stored, Mapping) else None


def learn_round_trip(
    store: Any,
    battery: BatteryConfig,
    now: datetime | None = None,
    *,
    days: float = 45.0,
    sample_minutes: float = capacity_learning.DEFAULT_SAMPLE_MINUTES,
) -> dict[str, Any] | None:
    """Estimate one battery's round trip from its persisted rows and store the payload.

    Returns the stored payload, or ``None`` when there is no store/rows.  A store failure is
    swallowed on purpose: learning is an optimisation, and the typed round trip stays correct.
    """
    reader = getattr(store, "measurements_range", None)
    writer = getattr(store, "profile_put", None)
    if not callable(reader) or not callable(writer):
        return None
    moment = now or utcnow()
    start = moment - timedelta(days=max(1.0, float(days)))
    roles = [
        f"{base}:{battery.id}"
        for base in (*capacity_learning.POWER_ROLES, capacity_learning.SOC_ROLE)
    ]
    try:
        rows = reader(start, moment, roles=roles) or []
    except Exception:  # noqa: BLE001 - no history is not an error
        return None
    payload = estimate_round_trip(
        capacity_learning.samples_from_rows(rows, battery.id),
        max_charge_w=battery.max_charge_w,
        max_discharge_w=battery.max_discharge_w,
        sample_minutes=sample_minutes,
        now=moment,
    )
    payload["seed_round_trip"] = float(battery.round_trip_efficiency)
    try:
        writer(PROFILE_KIND, battery.id, payload, moment)
    except Exception:  # noqa: BLE001 - a write failure leaves the previous estimate in place
        return payload
    return payload
