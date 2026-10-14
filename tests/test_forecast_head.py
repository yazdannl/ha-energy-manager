"""Forecast archive and the nightly fine-tune head (SPEC.md sections 7.4 and 7.5).

The head's value is that it learns from this house; its risk is that it learns something wrong and
the battery acts on it. So these tests are mostly about the parts that *stop* it: the gate that
refuses a candidate which does not beat what it would replace, the rollback that undoes a promotion,
and the closed form's behaviour when there is barely any data — where the correct answer is the base
model, not a confident fit from two cloudy afternoons.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.forecast import local_head  # noqa: E402
from app.store import Store  # noqa: E402

ZONE = ZoneInfo("Europe/Amsterdam")
NOON = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "sem.db")
    yield instance
    instance.close()


def make_sample(hour: int, forecast: float, actual: float, when: datetime = NOON) -> "local_head.Sample":
    return local_head.Sample(moment=when, hour=hour, forecast_kw=forecast, actual_kw=actual)


# ---------------------------------------------------------------------------- the closed form


def test_no_data_returns_exactly_one_not_a_division_by_zero() -> None:
    """With no evidence the estimator must be the base model, not an artefact of the algebra."""
    assert local_head.fit_factors([]) == [1.0] * 24


def test_a_single_sample_barely_moves_the_estimate() -> None:
    """The prior has to be strong enough that one afternoon cannot rewrite an hour of the day.

    With the shipped shrinkage a lone (3 kW forecast, 1.5 kW actual) pair lands near 0.87, not near
    0.5: the measurement moves the estimate without becoming it.
    """
    factors = local_head.fit_factors([make_sample(12, 3.0, 1.5)])
    assert 0.85 < factors[12] < 0.95
    assert factors[13] == 1.0


def test_plenty_of_evidence_converges_on_the_ratio() -> None:
    """Given enough consistent evidence the prior should fade out of the way."""
    samples = [make_sample(12, 3.0, 1.5) for _ in range(400)]
    assert local_head.fit_factors(samples)[12] == pytest.approx(0.5, abs=0.02)


def test_each_hour_is_fitted_independently() -> None:
    samples = [make_sample(9, 2.0, 1.0) for _ in range(200)]
    factors = local_head.fit_factors(samples)
    assert factors[9] == pytest.approx(0.5, abs=0.02)
    assert factors[15] == 1.0


def test_factors_stay_inside_the_shipped_clamp() -> None:
    """The head cannot make a larger correction than the mechanism it replaces (SPEC 7.1)."""
    factors = local_head.fit_factors([make_sample(12, 0.5, 40.0) for _ in range(500)])
    assert all(local_head.FACTOR_MIN <= value <= local_head.FACTOR_MAX for value in factors)


def test_negligible_and_negative_forecasts_are_ignored() -> None:
    """A multiplier fitted where nothing is produced is noise, and negative measurement is a fault."""
    factors = local_head.fit_factors(
        [make_sample(3, 0.0, 5.0), make_sample(4, 1e-9, 5.0), make_sample(5, 2.0, -1.0)]
    )
    assert factors == [1.0] * 24


# ---------------------------------------------------------------------------- the gate


def _seed(store: Store, *, hours: int, forecast: float, actual: float, days: int = 12) -> None:
    """Write paired archive and measured rows so a fit has something real to work with."""
    for day in range(days):
        base = NOON - timedelta(days=day)
        for hour in range(hours):
            moment = base.replace(hour=hour, minute=0, second=0, microsecond=0)
            store.upsert_actuals(moment, pv_kw=actual, load_kw=0.4)
            store.archive_forecast(
                [("solar", "learned", moment.isoformat(), (moment - timedelta(hours=1)).isoformat(),
                  forecast, 30.0)]
            )


def test_insufficient_evidence_is_refused_rather_than_promoted(store: Store) -> None:
    """Too little history must report a reason, and must not adopt anything."""
    _seed(store, hours=4, forecast=3.0, actual=1.5, days=1)
    report = local_head.update(
        store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON
    )
    assert report.status == "insufficient"
    assert report.promoted is False
    assert "need" in report.reason
    assert local_head.active_factors(store, "solar") is None


def test_a_systematically_wrong_forecast_is_corrected(store: Store) -> None:
    """The base model predicting double the truth is exactly what a local head exists to fix.

    Note which buckets move. The head is bucketed by *local* hour to match the shipped EWMA, and the
    seeded rows are written in UTC, so they land two hours later in a Dutch summer. An hour of the
    day with no scored slots keeps a factor of exactly 1.0 rather than borrowing a neighbour's — a
    correction invented for an hour nobody measured is precisely the kind of confident nonsense the
    prior exists to prevent.
    """
    _seed(store, hours=10, forecast=3.0, actual=1.5, days=12)
    report = local_head.update(
        store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON
    )
    assert report.promoted is True, report.reason
    factors = local_head.active_factors(store, "solar")
    assert factors is not None
    fitted = factors[2:12]
    assert all(value < 0.7 for value in fitted), fitted
    # Half the forecast is the truth, and enough evidence should get close to saying so outright.
    assert all(value < 0.65 for value in fitted)
    assert factors[0] == 1.0 and factors[23] == 1.0


def test_an_already_accurate_forecast_is_left_alone(store: Store) -> None:
    """Nothing to fix means nothing is promoted — the gate is not a rubber stamp."""
    _seed(store, hours=10, forecast=2.0, actual=2.0, days=12)
    report = local_head.update(store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON)
    assert report.promoted is False
    assert local_head.active_factors(store, "solar") is None


def test_a_candidate_that_loses_to_the_incumbent_is_rejected(store: Store) -> None:
    """The incumbent is whatever is in force; a head may only replace it by beating it.

    Here the incumbent already halves a forecast that is twice the truth, so it is exactly right and
    no candidate can beat it — the head must decline rather than churn.
    """
    _seed(store, hours=10, forecast=4.0, actual=2.0, days=12)
    incumbent = [0.5] * 24
    report = local_head.update(store, kind="solar", zone=ZONE, incumbent=incumbent, now=NOON)
    assert report.promoted is False
    assert local_head.active_factors(store, "solar") is None


def test_an_unmeasured_series_produces_no_samples_not_a_perfect_score(store: Store) -> None:
    """Without a measured PV series there is nothing to learn, and the head must say so."""
    for hour in range(12):
        moment = NOON.replace(hour=hour)
        store.archive_forecast(
            [("solar", "learned", moment.isoformat(), (moment - timedelta(hours=1)).isoformat(), 3.0, 30.0)]
        )
    assert local_head.collect_samples(store, start=NOON - timedelta(days=2), end=NOON, zone=ZONE) == []
    report = local_head.update(store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON)
    assert report.status == "insufficient"


def test_a_refit_is_not_due_immediately(store: Store) -> None:
    """Two planner ticks in a minute must not refit twice."""
    _seed(store, hours=10, forecast=3.0, actual=1.5, days=12)
    first = local_head.update(store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON)
    assert first.status != "fresh"
    second = local_head.update(store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON)
    assert second.status == "fresh"
    assert second.promoted is first.promoted
    forced = local_head.update(
        store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON, force=True
    )
    assert forced.status != "fresh"


# ---------------------------------------------------------------------------- rollback


def test_revert_undoes_a_promotion(store: Store) -> None:
    """The point of a gate is that the decision can be taken back without a rebuild."""
    _seed(store, hours=10, forecast=3.0, actual=1.5, days=12)
    assert local_head.update(
        store, kind="solar", zone=ZONE, incumbent=[1.0] * 24, now=NOON
    ).promoted
    assert local_head.active_factors(store, "solar") is not None
    assert local_head.revert(store, "solar", now=NOON) is True
    assert local_head.active_factors(store, "solar") is None
    payload = local_head.read_profile(store, "solar")
    assert payload is not None and payload["status"] == "reverted"


def test_revert_with_nothing_promoted_is_a_no_op(store: Store) -> None:
    assert local_head.revert(store, "solar", now=NOON) is False


def test_revert_reports_failure_on_a_store_that_cannot_write() -> None:
    """Reverting must report honestly rather than raising into the control loop."""
    assert local_head.revert(None, "solar") is False


# ---------------------------------------------------------------------------- archive


def test_archive_keeps_the_newest_issue_and_ignores_a_late_one(store: Store) -> None:
    """A provider must not be able to improve its own record by re-issuing a past slot."""
    target = (NOON + timedelta(minutes=15)).isoformat()
    store.archive_forecast([("solar", "learned", target, NOON.isoformat(), 2.0, 30.0)])
    store.archive_forecast(
        [("solar", "learned", target, (NOON + timedelta(minutes=5)).isoformat(), 2.5, 30.0)]
    )
    store.archive_forecast(
        [("solar", "learned", target, (NOON - timedelta(minutes=5)).isoformat(), 9.9, 30.0)]
    )
    rows = store.forecast_archive_range(NOON, NOON + timedelta(hours=1), "solar")
    assert len(rows) == 1 and rows[0]["value"] == 2.5


def test_archive_separates_sources_and_kinds(store: Store) -> None:
    target = (NOON + timedelta(minutes=15)).isoformat()
    store.archive_forecast([("solar", "learned", target, NOON.isoformat(), 2.0, 30.0)])
    store.archive_forecast([("solar", "capacity", target, NOON.isoformat(), 1.0, 35.0)])
    store.archive_forecast([("load", "load", target, NOON.isoformat(), 0.5, None)])
    solar = store.forecast_archive_range(NOON, NOON + timedelta(hours=1), "solar")
    assert {row["source"] for row in solar} == {"learned", "capacity"}
    assert len(store.forecast_archive_range(NOON, NOON + timedelta(hours=1), "load")) == 1


def test_accuracy_compares_hourly_means(store: Store) -> None:
    """The two sides are sampled at different cadences; the join has to survive that."""
    hour_start = NOON.replace(minute=0, second=0, microsecond=0)
    for minutes in (0, 5, 10):
        store.upsert_actuals(hour_start + timedelta(minutes=minutes), pv_kw=2.0)
    store.upsert_actuals(hour_start + timedelta(minutes=15), pv_kw=4.0)
    for minutes in (0, 15, 30, 45):
        moment = hour_start + timedelta(minutes=minutes)
        store.archive_forecast(
            [("solar", "learned", moment.isoformat(), (moment - timedelta(hours=1)).isoformat(), 2.5, 30.0)]
        )
    scores = store.forecast_accuracy(NOON - timedelta(hours=2), NOON + timedelta(hours=2), "solar")
    assert len(scores) == 1
    row = scores[0]
    assert row["hours"] == 1
    assert row["measured_mean_kw"] == pytest.approx(2.5)  # mean of 2,2,2,4
    assert row["forecast_mean_kw"] == pytest.approx(2.5)
    assert row["mae_kw"] == pytest.approx(0.0)


def test_accuracy_refuses_a_kind_with_no_measured_series(store: Store) -> None:
    """Silently scoring a series nothing measured would be the most flattering possible bug."""
    with pytest.raises(ValueError):
        store.forecast_accuracy(NOON, NOON + timedelta(hours=1), "price")


def test_archive_trim_keeps_the_recent_window(store: Store) -> None:
    old = NOON - timedelta(days=401)
    store.archive_forecast([("solar", "learned", old.isoformat(), (old - timedelta(hours=1)).isoformat(), 1.0, None)])
    store.archive_forecast([("solar", "learned", NOON.isoformat(), (NOON - timedelta(hours=1)).isoformat(), 2.0, None)])
    removed = store.trim(now=NOON)
    assert removed["forecast_archive"] == 1
    assert len(store.forecast_archive_range(NOON - timedelta(days=1), NOON + timedelta(days=1))) == 1
