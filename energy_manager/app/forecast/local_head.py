"""The nightly fine-tune head (SPEC.md section 7.5).

The base model is trained on other people's arrays, so it knows what a Dutch rooftop *in general*
does and nothing about what this one does. There is one correction that fixes a lot of that
cheaply: a per-hour-of-day multiplier learned from this house's own forecast-versus-measured record.

Two properties make it safe to run unattended.

**It solves in closed form.** ``m_h = (Σ f·a + λ) / (Σ f² + λ)`` is ridge regression against a prior
of exactly 1.0, written as one division. With no data the estimate *is* 1.0, not a division by zero;
with a little data it barely moves; with a lot it becomes the least-squares fit. That is what lets a
nightly refit live inside the add-on's standard-library constraint (R3) and be read in one sitting.

**It can only be adopted by beating what it replaces.** A fit is a candidate until it wins on
samples it was not fitted on, and the previous profile is kept so a bad night is one write away from
being undone. Learning from your own house is worth doing; doing it to a battery controller without
a gate is not, so the gate is part of the feature rather than a later addition.

This module never applies itself. :func:`active_factors` answers "is there a promoted head, and what
are its factors", and the provider decides what to do with that — which keeps the failure modes
separate: a broken fit is a learned profile that fails to promote, never a broken forecast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ..models import clamp, from_iso, to_iso

log = logging.getLogger(__name__)

__all__ = [
    "FACTOR_MAX",
    "FACTOR_MIN",
    "PROMOTION_MARGIN",
    "PROFILE_KIND",
    "HeadReport",
    "active_factors",
    "collect_samples",
    "fit_factors",
    "read_profile",
    "revert",
    "update",
]

#: ``learning_profiles`` kind. The key is the forecast kind being corrected (``solar``).
PROFILE_KIND = "forecast_head"

#: Ridge prior strength, in squared kilowatts. A bucket needs on the order of this much squared
#: forecast before its own fit outweighs the base model — roughly three hours of a 3 kW array.
#: Deliberately over-strength: a correction fitted from a single afternoon should stay near the base
#: model, because the alternative is a large swing at one hour of the day on almost no evidence. The
#: promotion gate is the second line of defence, not the first.
SHRINKAGE = 25.0

#: The same clamp the shipped EWMA uses, so the head cannot make a larger correction than the
#: mechanism it replaces (SPEC 7.1).
FACTOR_MIN = 0.3
FACTOR_MAX = 1.8

#: Evidence required before a fit is even a candidate.
MIN_SAMPLES = 36
MIN_HOURS = 10

#: Fit window and the part of it held back for the gate. The holdout is the newest slice, because
#: that is the regime the head is about to be used in.
FIT_DAYS = 14.0
HOLDOUT_DAYS = 3.0

#: Refit at most this often. The archive advances every planner tick; the correction does not need
#: to.
REFIT_HOURS = 20.0

#: The candidate must beat the incumbent by this fraction of its error. A bare improvement would
#: promote on noise, and a promotion that is wrong is felt on a battery.
PROMOTION_MARGIN = 0.02


@dataclass(frozen=True)
class Sample:
    """One scored slot: when it was for, its local hour bucket, what was forecast, what happened."""

    moment: datetime
    hour: int
    forecast_kw: float
    actual_kw: float


@dataclass(frozen=True)
class HeadReport:
    """What the last refit decided, for the status payload and the tests."""

    status: str
    promoted: bool
    samples: int
    fit_samples: int
    holdout_samples: int
    candidate_mae: float | None
    incumbent_mae: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "promoted": self.promoted,
            "samples": self.samples,
            "fit_samples": self.fit_samples,
            "holdout_samples": self.holdout_samples,
            "candidate_mae_kw": None if self.candidate_mae is None else round(self.candidate_mae, 4),
            "incumbent_mae_kw": None if self.incumbent_mae is None else round(self.incumbent_mae, 4),
            "reason": self.reason,
        }


def _measured_column(kind: str) -> str | None:
    """The measured series a forecast kind is scored against, or ``None`` when there is none."""
    return {"solar": "pv_kw", "load": "load_kw"}.get(str(kind))


def fit_factors(samples: Sequence[Sample], *, shrinkage: float = SHRINKAGE) -> list[float]:
    """Closed-form per-hour multipliers, shrunk toward 1.0.

    Negative or negligible forecasts are skipped: a multiplier fitted where almost nothing is being
    produced carries no information and would be dominated by rounding.
    """
    numerator = [shrinkage] * 24
    denominator = [shrinkage] * 24
    for sample in samples:
        if sample.forecast_kw <= 1e-6 or sample.actual_kw < 0.0:
            continue
        bucket = int(sample.hour) % 24
        numerator[bucket] += sample.forecast_kw * sample.actual_kw
        denominator[bucket] += sample.forecast_kw * sample.forecast_kw
    return [clamp(numerator[hour] / denominator[hour], FACTOR_MIN, FACTOR_MAX) for hour in range(24)]


def collect_samples(
    store: Any,
    *,
    start: datetime,
    end: datetime,
    zone: timezone | ZoneInfo,
    kind: str = "solar",
) -> list[Sample]:
    """Archived forecasts joined to what was measured, bucketed by local hour.

    Both sides are reduced to hourly means first. The archive holds 15-minute slots while the sampler
    writes at its own cadence, so an exact timestamp join would match almost nothing and report a
    flattering empty result rather than an honest small one.
    """
    column = _measured_column(kind)
    if store is None or column is None:
        return []
    measured: dict[str, list[float]] = {}
    for row in store.actuals_range(start, end):
        value = row.get(column)
        if value is None:
            continue
        measured.setdefault(str(row["ts"])[:13], []).append(float(value))
    if not measured:
        return []
    means = {hour: sum(values) / len(values) for hour, values in measured.items()}
    samples: list[Sample] = []
    for row in store.forecast_archive_range(start, end, kind):
        mean = means.get(str(row["target_ts"])[:13])
        if mean is None:
            continue
        moment = from_iso(row["target_ts"])
        if moment is None:
            continue
        samples.append(
            Sample(
                moment=moment,
                hour=moment.astimezone(zone).hour,
                forecast_kw=float(row["value"]),
                actual_kw=mean,
            )
        )
    samples.sort(key=lambda sample: sample.moment)
    return samples


def _mean_absolute_error(samples: Sequence[Sample], factors: Sequence[float]) -> float | None:
    """MAE of ``forecast × factor[local hour]`` against the measurement, kW; ``None`` with no data."""
    total = 0.0
    count = 0
    for sample in samples:
        if sample.forecast_kw <= 1e-6:
            continue
        factor = float(factors[int(sample.hour) % 24])
        total += abs(sample.forecast_kw * factor - sample.actual_kw)
        count += 1
    return None if count == 0 else total / count


def read_profile(store: Any, kind: str = "solar") -> dict[str, Any] | None:
    """The stored head payload for a forecast kind, or ``None``."""
    if store is None:
        return None
    got = store.profile_get(PROFILE_KIND, str(kind))
    if got is None:
        return None
    payload = got[1]
    return payload if isinstance(payload, dict) else None


def active_factors(store: Any, kind: str = "solar") -> list[float] | None:
    """The factors a promoted head wants applied, or ``None`` when nothing is promoted.

    Returning ``None`` rather than ones is deliberate: the caller must be able to tell "no head" from
    "a head that says the forecast is already right", because only the first should fall back to the
    shipped EWMA correction.
    """
    payload = read_profile(store, kind)
    if not payload or not payload.get("promoted"):
        return None
    factors = payload.get("factors")
    if not isinstance(factors, list) or len(factors) != 24:
        return None
    try:
        return [clamp(float(value), FACTOR_MIN, FACTOR_MAX) for value in factors]
    except (TypeError, ValueError):
        return None


def revert(store: Any, kind: str = "solar", *, now: datetime | None = None) -> bool:
    """Undo the last promotion, restoring the previous profile. ``True`` when something changed.

    The escape hatch the promotion gate implies: a head that looked better on the holdout and turns
    out worse in service must be removable without a rebuild and without waiting a night.
    """
    payload = read_profile(store, kind)
    if not payload:
        return False
    previous = payload.get("previous")
    if isinstance(previous, dict):
        previous = dict(previous)
        previous["promoted"] = False
        previous["reverted_at"] = to_iso(now or datetime.now(timezone.utc))
        previous["status"] = "reverted"
        store.profile_put(PROFILE_KIND, str(kind), previous, updated_at=now)
        return True
    if payload.get("promoted"):
        payload = dict(payload)
        payload["promoted"] = False
        payload["status"] = "reverted"
        payload["reverted_at"] = to_iso(now or datetime.now(timezone.utc))
        store.profile_put(PROFILE_KIND, str(kind), payload, updated_at=now)
        return True
    return False


def update(
    store: Any,
    *,
    kind: str,
    zone: timezone | ZoneInfo,
    incumbent: Sequence[float],
    now: datetime | None = None,
    force: bool = False,
    refit_hours: float = REFIT_HOURS,
) -> HeadReport:
    """Fit, gate and adopt — at most once per ``refit_hours``. Never raises.

    ``incumbent`` is whatever correction is in force now: the shipped EWMA factors, or an already
    promoted head. The candidate has to beat *that*, on samples it was not fitted on, by
    :data:`PROMOTION_MARGIN` of its error. A candidate that loses is not stored as promoted; the
    incumbent stays exactly as it was.
    """
    moment = now or datetime.now(timezone.utc)
    previous = read_profile(store, kind)
    if not force and previous is not None:
        last = from_iso(previous.get("fitted_at"))
        if last is not None and (moment - last).total_seconds() < refit_hours * 3600.0:
            return HeadReport("fresh", bool(previous.get("promoted")), 0, 0, 0, None, None,
                              "refit not due")

    end = moment
    start = moment - timedelta(days=FIT_DAYS)
    holdout_start = moment - timedelta(days=HOLDOUT_DAYS)
    samples = collect_samples(store, start=start, end=end, zone=zone, kind=kind)
    fit_samples = [sample for sample in samples if sample.moment < holdout_start]
    holdout = [sample for sample in samples if sample.moment >= holdout_start]
    if len(fit_samples) < MIN_SAMPLES or len({sample.hour for sample in fit_samples}) < MIN_HOURS:
        return HeadReport(
            "insufficient", bool(previous and previous.get("promoted")), len(samples),
            len(fit_samples), len(holdout), None, None,
            f"need {MIN_SAMPLES} scored slots across {MIN_HOURS} hours of the day",
        )

    candidate = fit_factors(fit_samples)
    candidate_mae = _mean_absolute_error(holdout, candidate)
    incumbent_mae = _mean_absolute_error(holdout, list(incumbent) if incumbent else [1.0] * 24)
    promoted = False
    reason = ""
    if candidate_mae is None or incumbent_mae is None:
        reason = "nothing scored in the holdout window"
    elif incumbent_mae <= 0.0:
        # An incumbent with no error cannot be improved on; adopting anything would be a regression.
        reason = "the incumbent is already exact over the holdout"
    elif candidate_mae < incumbent_mae * (1.0 - PROMOTION_MARGIN):
        promoted = True
        reason = (
            f"holdout MAE {candidate_mae:.3f} kW beats {incumbent_mae:.3f} kW "
            f"by more than {PROMOTION_MARGIN:.0%}"
        )
    else:
        reason = (
            f"holdout MAE {candidate_mae:.3f} kW does not beat {incumbent_mae:.3f} kW "
            f"by {PROMOTION_MARGIN:.0%}; keeping the incumbent"
        )

    payload: dict[str, Any] = {
        "status": "promoted" if promoted else "rejected",
        "promoted": promoted,
        "factors": candidate,
        "fitted_at": to_iso(moment),
        "kind": str(kind),
        "samples": len(samples),
        "fit_samples": len(fit_samples),
        "holdout_samples": len(holdout),
        "candidate_mae_kw": candidate_mae,
        "incumbent_mae_kw": incumbent_mae,
        "shrinkage": SHRINKAGE,
        "reason": reason,
        "previous": previous,
    }
    try:
        store.profile_put(PROFILE_KIND, str(kind), payload, updated_at=moment)
    except Exception as exc:  # noqa: BLE001 - a fit that cannot be stored is simply not adopted
        log.warning("storing the fine-tune head for %s failed: %s", kind, exc)
        return HeadReport("store_failed", False, len(samples), len(fit_samples), len(holdout),
                          candidate_mae, incumbent_mae, str(exc))
    return HeadReport(
        payload["status"], promoted, len(samples), len(fit_samples), len(holdout),
        candidate_mae, incumbent_mae, reason,
    )
