"""Self-learning battery capacity (SPEC.md section 5).

Covers the pure estimator (formula direction, noise/outliers, gaps, confidence, staleness), the
runtime store round-trip through ``learning_profiles``, the engine's effective-capacity resolution
(manual wins, learned wins when confident, fallbacks) and the API/UI surface. No real entity id,
serial or instance datum appears here: every fixture is obviously fake.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_manager"))

from app.capacity import (  # noqa: E402
    MAX_KEPT_WINDOWS,
    MIN_ACCEPTED_WINDOWS,
    PROFILE_KIND,
    REASON_APPLIED,
    REASON_LOW_CONFIDENCE,
    REASON_MANUAL,
    REASON_STALE,
    REASON_UNAVAILABLE,
    capacity_status,
    estimate_capacity,
    extract_windows,
    learn_capacity,
    load_estimate,
    robust_median,
    samples_from_rows,
)
from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import BatteryConfig, Measurement, to_iso  # noqa: E402
from app.store import Store  # noqa: E402

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

EFFICIENCY = 0.95
MAX_POWER = 800.0


def _window(
    start: datetime,
    soc_from: float,
    soc_to: float,
    power_w: float,
    *,
    steps: int = 12,
    minutes: int = 5,
) -> list[tuple[datetime, float, float]]:
    """One monotonic run: ``(ts, power_w, soc_pct)`` with an even SoC ramp."""
    out: list[tuple[datetime, float, float]] = []
    for index in range(steps + 1):
        soc = soc_from + (soc_to - soc_from) * index / steps
        out.append((start + timedelta(minutes=minutes * index), power_w, soc))
    return out


def _estimated(
    samples: list[tuple[datetime, float, float]],
    *,
    eta_charge: float = EFFICIENCY,
    eta_discharge: float = EFFICIENCY,
    max_charge_w: float = MAX_POWER,
    max_discharge_w: float = MAX_POWER,
) -> dict:
    return estimate_capacity(
        samples,
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
        eta_charge=eta_charge,
        eta_discharge=eta_discharge,
        now=T0,
    )


# ----------------------------------------------------------------------------------- estimator


def test_charge_and_discharge_use_their_own_efficiency():
    """The eta belongs on the matching leg: charge multiplies, discharge divides."""
    # 500 W for 60 min = 0.5 kWh AC, 20 SoC points.
    charge = _estimated(_window(T0, 40.0, 60.0, 500.0))
    assert charge["samples"] == 1
    assert charge["windows"][0] == pytest.approx(0.5 * 0.95 / 0.20, rel=1e-6)

    discharge = _estimated(_window(T0, 70.0, 50.0, -500.0))
    assert discharge["windows"][0] == pytest.approx(0.5 / 0.95 / 0.20, abs=1e-3)

    # Swapping the efficiencies to 0.8 moves each leg in the direction its formula says.
    low_charge = _estimated(_window(T0, 40.0, 60.0, 500.0), eta_charge=0.8)
    assert low_charge["windows"][0] == pytest.approx(0.5 * 0.8 / 0.20, rel=1e-6)
    low_discharge = _estimated(_window(T0, 70.0, 50.0, -500.0), eta_discharge=0.8)
    assert low_discharge["windows"][0] == pytest.approx(0.5 / 0.8 / 0.20, rel=1e-6)


def test_outlier_is_dropped_by_the_mad_filter():
    """Three noisy-but-real windows survive; one implausible window is filtered out."""
    samples: list[tuple[datetime, float, float]] = []
    for index, power in enumerate((470.0, 500.0, 530.0)):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, power)
    samples += _window(T0 + timedelta(hours=3), 40.0, 60.0, 2000.0)  # a 9.5 kWh "pack"
    payload = _estimated(samples)
    assert payload["samples"] == 4
    assert payload["median_kwh"] == pytest.approx(0.5 * 0.95 / 0.20, rel=1e-6)
    assert payload["capacity_kwh"] == payload["median_kwh"]


def test_insufficient_windows_is_not_published():
    """Below three accepted windows the estimate is kept as evidence but never applied."""
    samples = _window(T0, 40.0, 60.0, 500.0) + _window(T0 + timedelta(hours=1), 40.0, 60.0, 500.0)
    payload = _estimated(samples)
    assert payload["samples"] == 2
    assert payload["median_kwh"] is not None
    assert payload["capacity_kwh"] is None
    status = capacity_status(payload, T0 + timedelta(hours=1))
    assert status["reason"] == REASON_LOW_CONFIDENCE
    assert status["applied"] is False


def test_too_small_soc_move_and_too_slow_power_are_rejected():
    """A 5-point move or a 10 %-of-limit trickle is not a usable cycle window."""
    small_move = _estimated(_window(T0, 40.0, 45.0, 500.0))
    assert small_move["samples"] == 0 and small_move["rejected"]["dsoc"] == 1

    slow = _estimated(_window(T0, 40.0, 60.0, 100.0))  # 12.5 % of the 800 W limit
    assert slow["samples"] == 0 and slow["rejected"]["power"] == 1


def test_gap_longer_than_one_sample_breaks_the_window():
    """A 15-minute hole (two missing 5-minute samples) is never bridged."""
    samples = _window(T0, 40.0, 80.0, 500.0)
    with_gap = []
    for index, sample in enumerate(samples):
        stamp = sample[0] + (timedelta(minutes=15) if index >= 6 else timedelta())
        with_gap.append((stamp, sample[1], sample[2]))
    accepted, rejected = extract_windows(
        with_gap,
        max_charge_w=MAX_POWER,
        max_discharge_w=MAX_POWER,
        eta_charge=EFFICIENCY,
        eta_discharge=EFFICIENCY,
    )
    assert len(accepted) == 2
    assert rejected["gaps"] == 1


def test_everything_is_clamped_and_non_finite_is_dropped():
    assert robust_median([float("nan"), float("inf"), 2.0]) == 2.0
    assert robust_median([]) is None
    # 50 kW over a 20-point move is ~237 kWh, outside the plausible 1..200 kWh envelope.
    samples: list[tuple[datetime, float, float]] = []
    for index in range(MIN_ACCEPTED_WINDOWS):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, 50000.0)
    payload = _estimated(samples, max_charge_w=100000.0, max_discharge_w=100000.0)
    assert payload["samples"] == MIN_ACCEPTED_WINDOWS
    assert payload["median_kwh"] is None
    assert payload["capacity_kwh"] is None


def test_stale_estimate_is_reported_and_not_applied():
    payload = _estimated(_window(T0, 40.0, 60.0, 500.0) + _window(T0 + timedelta(hours=1), 40.0, 60.0, 500.0)
                         + _window(T0 + timedelta(hours=2), 40.0, 60.0, 500.0))
    fresh = capacity_status(payload, T0 + timedelta(days=1))
    assert fresh["reason"] == REASON_APPLIED and fresh["applied"] is True
    stale = capacity_status(payload, T0 + timedelta(days=31))
    assert stale["reason"] == REASON_STALE and stale["applied"] is False
    assert capacity_status(None, T0)["reason"] == REASON_UNAVAILABLE


def test_windows_are_capped_and_payload_is_finite():
    samples: list[tuple[datetime, float, float]] = []
    for index in range(MAX_KEPT_WINDOWS + 5):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, 500.0)
    payload = _estimated(samples)
    assert payload["samples"] == MAX_KEPT_WINDOWS + 5
    assert len(payload["windows"]) == MAX_KEPT_WINDOWS


def test_samples_from_rows_reads_signed_or_paired_power():
    stamp = to_iso(T0)
    rows = [
        {"ts": stamp, "entity_role": "battery_power:battery_1", "value": 500.0},
        {"ts": stamp, "entity_role": "battery_soc:battery_1", "value": 42.0},
        {"ts": stamp, "entity_role": "battery_soc:battery_2", "value": 99.0},
    ]
    samples = samples_from_rows(rows, "battery_1")
    assert samples == [(T0, 500.0, 42.0)]

    paired = [
        {"ts": stamp, "entity_role": "battery_charge:battery_1", "value": 600.0},
        {"ts": stamp, "entity_role": "battery_discharge:battery_1", "value": 100.0},
        {"ts": stamp, "entity_role": "battery_soc:battery_1", "value": 42.0},
    ]
    assert samples_from_rows(paired, "battery_1") == [(T0, 500.0, 42.0)]


# ----------------------------------------------------------------------------------- runtime store


def test_learn_capacity_writes_a_profile_and_survives_a_missing_store(tmp_path):
    store = Store(tmp_path / "measure.db")
    battery = BatteryConfig(
        id="battery_1",
        name="Example battery",
        driver="generic",
        capacity_kwh=5.0,
        max_charge_w=MAX_POWER,
        max_discharge_w=MAX_POWER,
        mapping={"power_setpoint_entity": "number.example_battery_power"},
    )
    rows = []
    for index, power in enumerate((500.0, 500.0, 500.0)):
        for ts, watts, soc in _window(T0 + timedelta(hours=2 * index), 40.0, 60.0, power):
            rows.append(Measurement(ts=ts, role="battery_power:battery_1", value=watts, unit="W"))
            rows.append(Measurement(ts=ts, role="battery_soc:battery_1", value=soc, unit="%"))
    store.add_measurements(rows)

    payload = learn_capacity(store, battery, T0 + timedelta(days=1))
    assert payload is not None and payload["samples"] == 3
    assert payload["capacity_kwh"] == pytest.approx(0.5 * 0.95 / 0.20, rel=1e-6)
    stored = load_estimate(store, "battery_1")
    assert stored is not None and stored["seed_kwh"] == 5.0

    assert learn_capacity(None, battery, T0) is None
    assert load_estimate(None, "battery_1") is None
    store.close()


# ----------------------------------------------------------------------------------- engine


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakeNotifier:
    def send(self, *_args, **_kwargs):
        return True


LEARNED_KWH = 8.5
EXAMPLE_BATTERY = BatteryConfig(
    id="example_battery_1",
    name="Example Battery 1",
    driver="generic",
    capacity_kwh=5.0,
    min_soc_pct=10.0,
    max_soc_pct=90.0,
    max_charge_w=1000.0,
    max_discharge_w=1000.0,
    eta_charge=0.95,
    eta_discharge=0.95,
    mapping={
        "power_setpoint_entity": "number.example_battery_power",
        "soc_entity": "sensor.example_battery_soc",
    },
)


def build_engine(capacity_source: str = "learned"):
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    settings.add_battery(replace(EXAMPLE_BATTERY, capacity_source=capacity_source))
    store = _FakeStore()
    clock = Clock()
    engine = Engine(
        settings,
        store=store,
        ha=_FakeHA(),
        notifier=FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    return engine, store, settings, clock


def put_estimate(store, battery_id: str, *, kwh: float | None, samples: int, now: datetime) -> None:
    payload = {
        "capacity_kwh": kwh,
        "median_kwh": kwh,
        "samples": samples,
        "windows": [kwh] * samples if kwh is not None else [],
        "updated_at": to_iso(now),
    }
    store.profile_put(PROFILE_KIND, battery_id, payload, now)


def test_manual_source_always_uses_the_configured_capacity():
    engine, store, _settings, clock = build_engine(capacity_source="manual")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == 5.0
    _kwh, status = engine.effective_capacity(config)
    assert status["reason"] == REASON_MANUAL and status["applied"] is False
    assert engine.effective_config(config).capacity_kwh == 5.0


def test_learned_source_wins_when_confident_and_fresh():
    engine, store, _settings, clock = build_engine(capacity_source="learned")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == pytest.approx(LEARNED_KWH)
    assert engine.effective_battery_configs()[0].capacity_kwh == pytest.approx(LEARNED_KWH)
    codes = [event["code"] for event in store.events]
    assert "capacity_learned_applied" in codes


def test_learned_source_falls_back_when_stale_low_confidence_or_missing():
    for kwh, samples, age_days, expected in (
        (LEARNED_KWH, 6, 40, REASON_STALE),
        (LEARNED_KWH, 2, 1, REASON_LOW_CONFIDENCE),
        (None, 0, 1, REASON_LOW_CONFIDENCE),
    ):
        engine, store, _settings, clock = build_engine(capacity_source="learned")
        put_estimate(
            store,
            EXAMPLE_BATTERY.id,
            kwh=kwh,
            samples=samples,
            now=clock.now - timedelta(days=age_days),
        )
        config = engine.battery_configs()[0]
        assert engine.effective_capacity_kwh(config) == 5.0
        assert engine.capacity_status(config.id)["reason"] == expected


def test_missing_profile_is_unavailable_and_falls_back():
    engine, _store, _settings, _clock = build_engine(capacity_source="learned")
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == 5.0
    assert engine.capacity_status(config.id)["reason"] == REASON_UNAVAILABLE


def test_plan_and_allocation_use_the_learned_capacity():
    """The planner receives the effective capacity: the published plan states it."""
    engine, store, _settings, clock = build_engine(capacity_source="learned")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    plan = engine.plan_now("learned-capacity-test")
    assert plan is not None and plan.batteries
    assert plan.batteries[0]["capacity_kwh"] == pytest.approx(LEARNED_KWH)


def test_learn_capacities_updates_the_profile_from_measurements():
    engine, store, _settings, clock = build_engine(capacity_source="learned")
    rows = []
    for index in range(3):
        for ts, watts, soc in _window(clock.now - timedelta(days=1, hours=2 * index), 40.0, 60.0, 500.0):
            rows.append({"ts": to_iso(ts), "entity_role": "battery_power:example_battery_1", "value": watts})
            rows.append({"ts": to_iso(ts), "entity_role": "battery_soc:example_battery_1", "value": soc})
    store.measurements_range = lambda start, end, roles=None: rows  # noqa: ARG005
    summary = engine.learn_capacities(clock.now)
    assert summary["batteries"] == [EXAMPLE_BATTERY.id]
    assert engine.capacity_status(EXAMPLE_BATTERY.id)["reason"] == REASON_APPLIED
    assert engine.effective_capacity_kwh(engine.battery_configs()[0]) == pytest.approx(
        0.5 * 0.95 / 0.20, rel=1e-6
    )


def test_batteries_view_exposes_the_capacity_payload():
    engine, store, _settings, clock = build_engine(capacity_source="learned")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    record = engine.batteries_view()[0]
    assert record["config"]["capacity_source"] == "learned"
    assert record["config"]["capacity_kwh"] == 5.0  # configured seed stays visible
    assert record["capacity_source"] == "learned"
    assert record["capacity_kwh"] == 5.0
    assert record["capacity_learned_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["capacity_samples"] == 6
    assert record["capacity_updated_at"] == to_iso(clock.now)
    assert record["effective_capacity_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["capacity"]["applied"] is True
