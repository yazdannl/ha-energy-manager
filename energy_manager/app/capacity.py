"""Self-learning battery capacity (SPEC.md section 5).

A new pack rarely holds what its label promises, and an aged one holds less. Every battery
therefore carries ``capacity_source``: ``auto`` (the default) keeps ``capacity_kwh`` as the
seed/fallback and lets the planner use the value measured from the pack's own cycles once it is
confident; ``pinned`` always uses the typed value.

The estimate is derived from the persisted 5-minute measurement rows only (``sources/measure.py``),
never from user settings: since 0.38.0 a row is the **mean** of the ticks in its bucket (with the
tick count in ``measurements.samples``), and a pre-0.38 row (count NULL) is ignored because it kept
only the last tick.  Each *window* is a maximal run of same-direction AC power whose SoC moved
enough and fast enough to be trusted, and the window's capacity is the energy moved divided by the
SoC it moved

    charging:   ``capacity = AC_charge_kWh * eta_charge / dSoC``
    discharging: ``capacity = AC_discharge_kWh / eta_discharge / |dSoC|``

so the direction of each efficiency matches the AC-side convention in :mod:`app.models` (the same
formula falls out of ``stored = AC * eta`` while charging and ``AC = stored * eta`` while
discharging).

**One pack, one capacity.** The two formulas are the same statement read in opposite directions, and
they only agree when the pack's reported SoC is proportional to the energy it holds. On a real pack it
is not (the BMS compresses the top of its range, so a discharge drops a few more points than it gives
back), and mixing both directions then produces two populations that are ~5-10 % apart -- which is a
SoC-scale artifact, not a second capacity. The estimate is therefore taken from the **charging** side
alone: what the pack accepts per SoC point, with the charging loss applied, is the capacity; whatever
the discharge delivers is that capacity *less* the round-trip loss (:mod:`app.efficiency` measures it).
Discharge windows are only a fallback when too few charge windows exist to publish, and the payload
always says which side it used (``basis``). Estimates are combined with a median after a
median-absolute-deviation outlier filter, clamped to a plausible range and published only after at
least three accepted windows.

Two things are deliberately **not** used. The signed ``battery_power`` role is the setpoint *command*,
not a measurement (a pack that does not follow a taper would inflate the estimate), and rows from two
different entity ids for the same role are never mixed (a renamed mapping must not merge two sensors'
scales). The applied number is the estimate capped at the pack's own reported capacity: a measurement
can confirm that a pack holds *less* than its label (aging), but an AC-only estimate cannot prove it
holds more (the two leg efficiencies are not separately identifiable from AC energy and SoC).

Everything in this module is pure except :func:`learn_capacity`, which writes the profile through the
store it is handed; a missing or unreadable store is not an error, only "no estimate yet".
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import BatteryConfig, from_iso, to_iso, utcnow

__all__ = [
    "DEFAULT_SAMPLE_MINUTES",
    "MAX_KEPT_WINDOWS",
    "MAD_LIMIT",
    "mad_filter",
    "mad_inliers",
    "POWER_ROLES",
    "SOC_ROLE",
    "MIN_ACCEPTED_WINDOWS",
    "MIN_DSOC_PCT",
    "MIN_POWER_FRACTION",
    "PROFILE_KIND",
    "SCHEMA",
    "REASON_APPLIED",
    "REASON_LOW_CONFIDENCE",
    "REASON_MANUAL",
    "REASON_STALE",
    "REASON_UNAVAILABLE",
    "STALE_AFTER_DAYS",
    "cap_to_reported",
    "capacity_status",
    "estimate_capacity",
    "extract_windows",
    "learn_capacity",
    "load_estimate",
    "robust_median",
    "samples_from_rows",
]

#: Store ``learning_profiles`` kind for the per-battery estimate; the key is the battery id.
PROFILE_KIND = "battery_capacity"

#: Payload schema.  Schema 2 (0.31.0) reads a window's AC energy from the measured
#: charge/discharge pair instead of the signed ``battery_power`` *command*; schema 3 (0.31.1) books
#: only the SoC-moving sample pairs except a run's trailing plateau (the phantom edge sample used to
#: inflate short windows); schema 4 (0.32.0) takes the capacity from the **charging side only**, so one
#: pack reports one capacity instead of a charge population and a discharge population a few percent
#: apart; schema 5 (0.37.2) refuses windows that touch the clamped ends of the reported SoC range (a
#: pack held at 100 % keeps accepting charge the SoC cannot show, so those windows over-read); schema 6
#: (0.38.0) never learns from the signed setpoint command, keeps only the newest entity id per role
#: (a renamed mapping cannot mix two sensors' scales), and trims leading SoC plateaus exactly like
#: trailing ones.  A payload from an older schema is reported but not trusted -- the typed seed
#: stands until the next maintenance relearn rewrites it.
SCHEMA = 6

#: A window's SoC must move at least this many points to be trusted.
MIN_DSOC_PCT = 10.0
#: ...and its mean |power| at least this fraction of the battery's own limit for that direction.
MIN_POWER_FRACTION = 0.20
#: Accepted windows kept in the stored payload (the robust combine only needs the median).
MAX_KEPT_WINDOWS = 20
#: At least this many accepted windows before the estimate is published as confident.
MIN_ACCEPTED_WINDOWS = 3
#: The sampler's bucket (SPEC 7.3); a longer gap means at most one missing sample.
DEFAULT_SAMPLE_MINUTES = 5.0
#: A whole sample may be missing before a window is broken.
MAX_GAP_SAMPLES = 1.0
#: An estimate older than this is stale and the seed/fallback wins again.
STALE_AFTER_DAYS = 30.0
#: Plausible usable-capacity envelope; anything outside is a bad measurement, never a battery.
MIN_PLAUSIBLE_KWH = 1.0
MAX_PLAUSIBLE_KWH = 200.0
#: Gross-error guard above the pack's own reported capacity (SPEC 5).  AC energy and SoC cannot
#: identify the two leg efficiencies separately, so an AC-only estimate above the pack's own figure
#: is not evidence that the pack holds more: live, a stale 2.30 kWh estimate sat 20 % above a
#: 1.92 kWh pack whose AC cycles and DC flow both support the label.  The reported value is therefore
#: a hard ceiling; a measurement is always free to correct *downward* (aging).  Downward is never
#: limited.
ABOVE_REPORTED_TOLERANCE = 0.0
#: A window whose SoC touches the clamped ends of the reported range is over-read: the pack keeps
#: accepting charge (or delivering it) while the reported SoC has stopped moving.
SOC_EDGE_PCT = 1.0
#: Median-absolute-deviation outlier filter: 1.4826 turns the MAD into a sigma estimate.
MAD_SCALE = 1.4826
MAD_LIMIT = 3.5

#: Stable reason slugs reported wherever the effective capacity is exposed (SPEC 5/10).
#: ``REASON_MANUAL`` is the pinned/typed case (the pre-0.31.0 name is kept: the slug is a contract).
REASON_MANUAL = "capacity_manual"
REASON_APPLIED = "capacity_learned"
REASON_STALE = "capacity_learned_stale"
REASON_LOW_CONFIDENCE = "capacity_learned_low_confidence"
REASON_UNAVAILABLE = "capacity_learned_unavailable"

_POWER_ROLES = ("battery_power", "battery_charge", "battery_discharge")
_SOC_ROLE = "battery_soc"
#: Roles the learner reads.  The signed ``battery_power`` is the setpoint entity (a command), so it
#: is deliberately absent: a measurement is the only thing that can teach a capacity.
_LEARNED_ROLES = ("battery_charge", "battery_discharge", _SOC_ROLE)

#: Public aliases: :mod:`app.efficiency` reads the same measurement vocabulary.
POWER_ROLES = _POWER_ROLES
SOC_ROLE = _SOC_ROLE


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mad_filter(values: Iterable[float], limit: float = MAD_LIMIT) -> list[bool]:
    """Mask of the values a median-absolute-deviation filter keeps (aligned with ``values``).

    A zero MAD (more than half the values agree exactly, or a single value) keeps every value: the
    filter is never allowed to empty the set.  A non-finite value is dropped (``False``).
    ``app.efficiency`` needs the mask itself, because its weights belong to the surviving pairs.
    """
    numbers = [_finite(value) for value in values]
    finite = [value for value in numbers if value is not None]
    if not finite:
        return [False] * len(numbers)
    middle = statistics.median(finite)
    mad = statistics.median([abs(value - middle) for value in finite])
    if mad <= 0:
        return [value is not None for value in numbers]
    cutoff = float(limit) * MAD_SCALE * mad
    keep = [value is not None and abs(value - middle) <= cutoff for value in numbers]
    return keep if any(keep) else [value is not None for value in numbers]


def mad_inliers(values: Iterable[float], limit: float = MAD_LIMIT) -> list[float]:
    """The values left after a median-absolute-deviation filter (a stable order, finite only).

    Shared with :mod:`app.efficiency`, which needs the surviving set and not only its median.
    """
    numbers = list(values)
    mask = mad_filter(numbers, limit)
    return [
        value
        for value, keep in zip(numbers, mask)
        if keep and _finite(value) is not None
    ]


def robust_median(values: Iterable[float]) -> float | None:
    """Median after dropping outliers by median absolute deviation; ``None`` when empty.

    A zero MAD (more than half the windows agree exactly, or a single window) keeps every value.
    """
    inliers = mad_inliers(values)
    return statistics.median(inliers) if inliers else None


def extract_windows(
    samples: Sequence[tuple[datetime, float, float]],
    *,
    max_charge_w: float,
    max_discharge_w: float,
    eta_charge: float,
    eta_discharge: float,
    sample_minutes: float = DEFAULT_SAMPLE_MINUTES,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Turn ``(ts, ac_power_w, soc_pct)`` samples into accepted capacity windows.

    Returns ``(accepted, rejected)``. Each accepted window is
    ``{kind, capacity_kwh, dsoc_pct, mean_power_w, samples, ac_kwh, start, end, soc_start_pct,
    soc_end_pct}``. A run is broken (and never joined across the break) by a direction change, a
    zero/invalid power sample or a gap longer than one sample, so a missing measurement can only
    shorten a window, never bridge it.

    Energy is booked only for the sample pairs that moved the SoC in the run's direction.  A leading
    or trailing plateau (the pack resting at its limit, or a SoC reading that stopped ticking) is
    subtracted, so a window's ``ac_kwh`` always paid for its ``dsoc_pct``; a *mid-run* plateau stays
    booked because SoC movement on either side pays for it.  The trailing trim was 0.31.1; the
    leading mirror is 0.38.0 (a run that starts while the SoC is still clamped must not book the
    energy it moved before the reading caught up).
    """
    accepted: list[dict[str, Any]] = []
    rejected = {"gaps": 0, "direction": 0, "dsoc": 0, "power": 0, "invalid": 0, "edge": 0}
    ordered = sorted(
        (s for s in samples if s and isinstance(s[0], datetime)), key=lambda s: s[0]
    )
    max_gap = float(sample_minutes) * (1.0 + MAX_GAP_SAMPLES)
    run: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal run
        if run is None:
            return
        current, run = run, None
        sign = current["sign"]
        start_soc, end_soc = current["start_soc"], current["end_soc"]
        dsoc = (end_soc - start_soc) if sign > 0 else (start_soc - end_soc)
        if dsoc <= 0 or not math.isfinite(dsoc):
            rejected["direction"] += 1
            return
        if dsoc < MIN_DSOC_PCT:
            rejected["dsoc"] += 1
            return
        if sign > 0 and (
            start_soc <= SOC_EDGE_PCT or end_soc >= 100.0 - SOC_EDGE_PCT
        ):
            rejected["edge"] += 1
            return
        if sign < 0 and (
            end_soc <= SOC_EDGE_PCT or start_soc >= 100.0 - SOC_EDGE_PCT
        ):
            rejected["edge"] += 1
            return
        limit = float(max_charge_w if sign > 0 else max_discharge_w)
        mean_power = current["power_abs"] / max(1, current["count"])
        if limit <= 0 or mean_power < MIN_POWER_FRACTION * limit:
            rejected["power"] += 1
            return
        ac_kwh = (
            current["ac_kwh"]
            - current.get("head_ac", 0.0)
            - current.get("tail_ac", 0.0)
        )
        if ac_kwh <= 0 or not math.isfinite(ac_kwh):
            rejected["invalid"] += 1
            return
        eta = float(eta_charge if sign > 0 else eta_discharge)
        if not 0 < eta <= 1:
            rejected["invalid"] += 1
            return
        capacity = (
            ac_kwh * eta / (dsoc / 100.0)
            if sign > 0
            else ac_kwh / eta / (dsoc / 100.0)
        )
        if not math.isfinite(capacity) or capacity <= 0:
            rejected["invalid"] += 1
            return
        accepted.append(
            {
                "kind": "charge" if sign > 0 else "discharge",
                "capacity_kwh": capacity,
                "dsoc_pct": round(dsoc, 3),
                "mean_power_w": round(mean_power, 1),
                "samples": current["count"],
                # The raw facts a round-trip estimate needs (``app.efficiency``): the AC energy
                # the window really moved and when it did (the cycle order and its age).
                "ac_kwh": ac_kwh,
                "start": current["start_ts"],
                "end": current["end_ts"],
                # The SoC the window started and ended on, so a *complete cycle* can be recognised
                # (``app.efficiency`` pairs a charge with the discharge that returns the pack to
                # where it started; a partial leg is not extrapolated into an efficiency).
                "soc_start_pct": round(start_soc, 3),
                "soc_end_pct": round(end_soc, 3),
            }
        )

    for (t0, p0, s0), (t1, _p1, s1) in zip(ordered, ordered[1:]):
        power = _finite(p0)
        soc_start = _finite(s0)
        soc_end = _finite(s1)
        if power is None or soc_start is None or soc_end is None:
            flush()
            rejected["invalid"] += 1
            continue
        gap = (t1 - t0).total_seconds() / 60.0
        if not 0 < gap <= max_gap:
            flush()
            rejected["gaps"] += 1
            continue
        sign = 1 if power > 0 else (-1 if power < 0 else 0)
        if sign == 0:
            flush()
            rejected["direction"] += 1
            continue
        if run is not None and run["sign"] != sign:
            flush()
        if run is None:
            run = {
                "sign": sign,
                "start_soc": soc_start,
                "end_soc": soc_start,
                "start_ts": t0,
                "end_ts": t0,
                "ac_kwh": 0.0,
                "power_abs": 0.0,
                "count": 0,
                "head_ac": 0.0,
                "tail_ac": 0.0,
                "moved": False,
            }
        energy = abs(power) * gap / 60.0 / 1000.0
        moved = (soc_end - soc_start) * sign > 0
        if moved:
            run["end_soc"] = soc_end
            run["end_ts"] = t1
            run["moved"] = True
            run["tail_ac"] = 0.0
        elif run["moved"]:
            # A trailing plateau: the run kept drawing after the SoC stopped moving.
            run["tail_ac"] += energy
        else:
            # ...and the mirror case at the start: the pack was already accepting power before the
            # reported SoC began to move.  Booking it would credit the window with energy it never
            # moved and inflate its capacity.
            run["head_ac"] += energy
        run["ac_kwh"] += energy
        run["power_abs"] += abs(power)
        run["count"] += 1
    flush()
    return accepted, rejected


def _capacity_basis(
    accepted: Sequence[Mapping[str, Any]],
) -> tuple[list[float], str]:
    """The window values a single capacity is built from, and which side they came from.

    A pack holds one capacity, so the estimate never mixes directions: the charging side measures
    what the pack accepts (with its loss applied) and the discharge side only delivers that minus the
    round-trip loss, which :mod:`app.efficiency` measures separately.  Discharge windows are used
    only when there are too few charge windows to publish a confident estimate.
    """
    charge = [float(w["capacity_kwh"]) for w in accepted if w.get("kind") == "charge"]
    discharge = [float(w["capacity_kwh"]) for w in accepted if w.get("kind") == "discharge"]
    if len(charge) >= MIN_ACCEPTED_WINDOWS or not discharge:
        return charge, "charge"
    if len(discharge) >= MIN_ACCEPTED_WINDOWS:
        return discharge, "discharge"
    # Neither side can publish a confident estimate; report the better-evidenced one and stay
    # unapplied (``capacity_status`` refuses it below :data:`MIN_ACCEPTED_WINDOWS`).
    if len(discharge) >= len(charge):
        return discharge, "discharge"
    return charge, "charge"


def estimate_capacity(
    samples: Sequence[tuple[datetime, float, float]],
    *,
    max_charge_w: float,
    max_discharge_w: float,
    eta_charge: float,
    eta_discharge: float,
    sample_minutes: float = DEFAULT_SAMPLE_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Robust capacity payload from measurement samples; never raises and never guesses.

    ``capacity_kwh`` is ``None`` until at least :data:`MIN_ACCEPTED_WINDOWS` windows were accepted;
    the payload still reports how many were, so the UI can say "2 of 3" instead of "--".
    """
    accepted, rejected = extract_windows(
        samples,
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
        eta_charge=eta_charge,
        eta_discharge=eta_discharge,
        sample_minutes=sample_minutes,
    )
    values, basis = _capacity_basis(accepted)
    median = robust_median(values)
    if median is not None and not (MIN_PLAUSIBLE_KWH <= median <= MAX_PLAUSIBLE_KWH):
        median = None
    samples_count = len(values)
    return {
        "capacity_kwh": median if samples_count >= MIN_ACCEPTED_WINDOWS else None,
        "median_kwh": median,
        "samples": samples_count,
        # Which side of the pack the number came from: one capacity, measured while charging.
        "basis": basis,
        # Diagnostics only, never a second capacity and never applied: the same pack read from the
        # discharge side, which its SoC scale biases by a few percent (module docstring).
        "charge_kwh": robust_median(w["capacity_kwh"] for w in accepted if w["kind"] == "charge"),
        "discharge_kwh": robust_median(
            w["capacity_kwh"] for w in accepted if w["kind"] == "discharge"
        ),
        "windows": [round(value, 4) for value in values[-MAX_KEPT_WINDOWS:]],
        "rejected": rejected,
        "schema": SCHEMA,
        "updated_at": to_iso(now or utcnow()),
    }


def cap_to_reported(
    kwh: float | None,
    reported_kwh: float | None,
    *,
    tolerance: float = 0.0,
) -> float | None:
    """The estimate to plan with: never more than the pack itself reports holding.

    A measurement may legitimately read *below* the label (an aged pack) and the typed seed is only
    a guess, so a downward correction always wins.  An AC-only estimate above the pack's own figure
    is not evidence that the pack holds more: the two leg efficiencies are not separately identifiable
    from AC energy and SoC, so a higher number is a stale SoC scale, a lagging sample, or a window
    that spans two different packs.  Planning that extra energy schedules charge the pack cannot
    accept and a SoC its own counters contradict, so the reported value is a ceiling, not a target.
    ``tolerance`` exists for an explicitly measured scale and is 0 by default.  ``None``/non-positive
    reported values leave the estimate untouched (no hardware statement, no cap).
    """
    if kwh is None:
        return None
    value = float(kwh)
    if reported_kwh is None:
        return value
    reported = float(reported_kwh)
    if reported <= 0.0 or not math.isfinite(reported):
        return value
    ceiling = reported * (1.0 + max(0.0, float(tolerance)))
    return min(value, ceiling)


def capacity_status(
    payload: Mapping[str, Any] | None,
    now: datetime,
    *,
    stale_after_days: float = STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Classify a stored payload into ``{capacity_kwh, samples, ..., reason, applied}``.

    ``applied`` is True only for a finite estimate with enough windows that is not stale; the
    ``reason`` slug is stable and is what the API/UI/log report when a learned value is used or
    rejected.
    """
    moment = now if now.tzinfo else now.replace(tzinfo=utcnow().tzinfo)
    basis = (str(payload.get("basis") or "") or None) if payload else None
    if not payload:
        return _status(None, 0, None, REASON_UNAVAILABLE, moment, basis=None)
    value = _finite(payload.get("capacity_kwh"))
    try:
        count = int(payload.get("samples") or 0)
    except (TypeError, ValueError):
        count = 0
    updated = from_iso(payload.get("updated_at"))
    if _finite(payload.get("schema")) != float(SCHEMA):
        # An estimate from an older measurement formula is reported but never applied.
        reason = REASON_LOW_CONFIDENCE
    elif value is None or count < MIN_ACCEPTED_WINDOWS:
        reason = REASON_LOW_CONFIDENCE
    elif updated is None:
        reason = REASON_STALE
    else:
        age = (moment - updated).total_seconds()
        reason = REASON_STALE if age > stale_after_days * 86400.0 else REASON_APPLIED
    return _status(value, count, updated, reason, moment, basis=basis)


def _status(
    value: float | None,
    count: int,
    updated: datetime | None,
    reason: str,
    now: datetime,
    *,
    basis: str | None = None,
) -> dict[str, Any]:
    applied = reason == REASON_APPLIED
    age_days = None if updated is None else max(0.0, (now - updated).total_seconds() / 86400.0)
    return {
        "capacity_kwh": value if applied else None,
        "learned_kwh": value,
        "samples": count,
        "updated_at": to_iso(updated),
        "age_days": None if age_days is None else round(age_days, 3),
        "stale": reason == REASON_STALE,
        # A pack has one capacity; this says which side of the cycle measured it (``charge`` unless
        # only discharge windows were available).
        "basis": basis,
        "confident": value is not None and count >= MIN_ACCEPTED_WINDOWS,
        "applied": applied,
        "reason": reason,
    }


def samples_from_rows(
    rows: Iterable[Mapping[str, Any]], battery_id: str
) -> list[tuple[datetime, float, float]]:
    """Collapse measurement rows into one ``(ts, ac_power_w, soc_pct)`` sample per bucket.

    The **measured** AC pair ``battery_charge:<id> - battery_discharge:<id>`` wins (a missing side
    counts as 0, the same AC-side convention as :mod:`app.sources.measure`).  The signed
    ``battery_power:<id>`` role is **not** read: it is the ``power_setpoint_entity`` -- a command
    written to the device -- and a command the pack/BMS did not follow (a taper, a manager still
    ramping) would inflate the energy and so the learned capacity (0.38.0).  Learning integrates
    what the pack really moved, exactly like the savings ledger refuses an unconfirmed command.

    Rows from two entity ids for the same role are never mixed: only the newest entity id per role
    survives (a renamed mapping must not merge two sensors' scales into one median).  A bucket
    without both a measured power and a SoC is skipped rather than half-read.
    """
    learned = set(_LEARNED_ROLES)
    latest: dict[str, tuple[datetime, str]] = {}
    for row in rows:
        if "samples" in row and row.get("samples") is None:
            # A pre-0.38 row kept the last tick, not the bucket mean; never mix the two methods.
            continue
        role = str(row.get("entity_role") or row.get("role") or "")
        if not role.endswith(f":{battery_id}"):
            continue
        base = role.split(":", 1)[0]
        if base not in learned or _finite(row.get("value")) is None:
            continue
        moment = from_iso(row.get("ts"))
        entity = str(row.get("entity_id") or "")
        if moment is None or not entity:
            continue
        if base not in latest or moment >= latest[base][0]:
            latest[base] = (moment, entity)
    dominant = {base: entity for base, (_moment, entity) in latest.items()}

    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        if "samples" in row and row.get("samples") is None:
            continue
        role = str(row.get("entity_role") or row.get("role") or "")
        value = _finite(row.get("value"))
        if value is None or not role.endswith(f":{battery_id}"):
            continue
        base = role.split(":", 1)[0]
        if base not in learned:
            continue
        entity = str(row.get("entity_id") or "")
        if entity and dominant.get(base) and entity != dominant[base]:
            continue
        key = str(row.get("ts") or "")
        buckets.setdefault(key, {})[base] = value
    samples: list[tuple[datetime, float, float]] = []
    for key, values in buckets.items():
        soc = values.get(_SOC_ROLE)
        if soc is None:
            continue
        if "battery_charge" in values or "battery_discharge" in values:
            power = values.get("battery_charge", 0.0) - values.get("battery_discharge", 0.0)
        else:
            continue
        moment = from_iso(key)
        if moment is None:
            continue
        samples.append((moment, power, soc))
    samples.sort(key=lambda sample: sample[0])
    return samples


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


def learn_capacity(
    store: Any,
    battery: BatteryConfig,
    now: datetime | None = None,
    *,
    days: float = 45.0,
    sample_minutes: float = DEFAULT_SAMPLE_MINUTES,
    eta_charge: float | None = None,
    eta_discharge: float | None = None,
) -> dict[str, Any] | None:
    """Estimate one battery from its persisted measurement rows and store the payload.

    Returns the stored payload, or ``None`` when there is no store/rows. A store failure is
    swallowed on purpose: learning is an optimisation, and the manual capacity stays correct.
    ``eta_charge``/``eta_discharge`` default to the battery's configured values and are overridden
    with the learned round trip when one is available (SPEC 5).

    A run with too few usable windows no longer throws away a confident estimate: the stored value
    is kept (and ages out on its own timestamp) instead of being replaced by ``None``.  That is the
    difference between "this month has no clean cycle" and "the pack changed".
    """
    reader = getattr(store, "measurements_range", None)
    writer = getattr(store, "profile_put", None)
    if not callable(reader) or not callable(writer):
        return None
    moment = now or utcnow()
    start = moment - timedelta(days=max(1.0, float(days)))
    roles = [f"{base}:{battery.id}" for base in _LEARNED_ROLES]
    try:
        rows = reader(start, moment, roles=roles) or []
    except Exception:  # noqa: BLE001 - no history is not an error
        return None
    payload = estimate_capacity(
        samples_from_rows(rows, battery.id),
        max_charge_w=battery.max_charge_w,
        max_discharge_w=battery.max_discharge_w,
        eta_charge=battery.eta_charge if eta_charge is None else float(eta_charge),
        eta_discharge=battery.eta_discharge if eta_discharge is None else float(eta_discharge),
        sample_minutes=sample_minutes,
        now=moment,
    )
    payload["seed_kwh"] = float(battery.capacity_kwh)
    previous = load_estimate(store, battery.id)
    if payload.get("capacity_kwh") is None and _keepable(previous, moment):
        return {**previous, "kept_previous": True}
    try:
        writer(PROFILE_KIND, battery.id, payload, moment)
    except Exception:  # noqa: BLE001 - a write failure leaves the previous estimate in place
        return payload
    return payload


def _keepable(previous: Mapping[str, Any] | None, now: datetime) -> bool:
    """True when a confident, current-schema profile is worth keeping over an empty relearn."""
    if not previous:
        return False
    if _finite(previous.get("schema")) != float(SCHEMA):
        return False
    status = capacity_status(previous, now)
    return bool(
        status.get("learned_kwh") is not None
        and int(status.get("samples") or 0) >= MIN_ACCEPTED_WINDOWS
        and not status.get("stale")
    )
