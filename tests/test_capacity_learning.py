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
    ABOVE_REPORTED_TOLERANCE,
    MAX_KEPT_WINDOWS,
    MIN_ACCEPTED_WINDOWS,
    PROFILE_KIND,
    REASON_APPLIED,
    REASON_LOW_CONFIDENCE,
    REASON_MANUAL,
    REASON_STALE,
    REASON_UNAVAILABLE,
    SCHEMA,
    capacity_status,
    estimate_capacity,
    extract_windows,
    learn_capacity,
    load_estimate,
    robust_median,
    samples_from_rows,
)
from app import efficiency as efficiency_learning  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app import models  # noqa: E402
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


def test_one_pack_reports_one_capacity_from_its_charging_side():
    """Charge and discharge windows are one capacity, not two: the charging side defines it.

    A pack holds one number of kWh.  If the discharge number comes out higher it is the SoC scale
    (a discharge drops more points than the energy it gives back), not extra capacity -- so the
    estimate is taken from charging alone and the discharge side only ever discounts it.
    """
    samples: list[tuple[datetime, float, float]] = []
    for index in range(4):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, 500.0)
    # The same 20-point swing discharging a touch more energy per point (0.5 kWh vs 0.47).
    for index in range(4):
        samples += _window(T0 + timedelta(hours=6 + index), 70.0, 50.0, -470.0)
    payload = _estimated(samples)
    charge_kwh = 0.5 * EFFICIENCY / 0.20
    assert payload["basis"] == "charge"
    assert payload["samples"] == 4  # the four charge windows only
    assert payload["capacity_kwh"] == pytest.approx(charge_kwh, rel=1e-6)
    assert payload["median_kwh"] == pytest.approx(charge_kwh, rel=1e-6)
    # The other side is reported as evidence, and is never what the pack is run with.
    assert payload["discharge_kwh"] == pytest.approx(0.47 / EFFICIENCY / 0.20, rel=1e-6)
    assert payload["discharge_kwh"] != pytest.approx(payload["capacity_kwh"], rel=1e-3)
    status = capacity_status(payload, T0 + timedelta(hours=12))
    assert status["applied"] is True and status["basis"] == "charge"


def test_discharge_is_a_fallback_only_when_charging_is_too_thin_to_publish():
    """Two charge windows cannot publish a confident estimate, so the discharge side may speak."""
    samples: list[tuple[datetime, float, float]] = []
    for index in range(2):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, 500.0)
    for index in range(4):
        samples += _window(T0 + timedelta(hours=6 + index), 70.0, 50.0, -500.0)
    payload = _estimated(samples)
    assert payload["basis"] == "discharge"
    assert payload["samples"] == 4
    assert payload["capacity_kwh"] == pytest.approx(0.5 / EFFICIENCY / 0.20, rel=1e-6)


def test_a_discharge_side_that_reads_low_does_not_lower_the_capacity():
    """The same pack, one capacity: a biased discharge SoC cannot drag the estimate with it."""
    samples: list[tuple[datetime, float, float]] = []
    for index in range(3):
        samples += _window(T0 + timedelta(hours=index), 40.0, 60.0, 500.0)
    # Discharges that appear to give back 9 % more energy per SoC point (a compressed top range).
    for index in range(6):
        samples += _window(T0 + timedelta(hours=6 + index), 70.0, 50.0, -545.0)
    payload = _estimated(samples)
    assert payload["basis"] == "charge"
    assert payload["capacity_kwh"] == pytest.approx(0.5 * EFFICIENCY / 0.20, rel=1e-6)
    mixed_median = robust_median(
        [0.5 * EFFICIENCY / 0.20] * 3 + [0.545 / EFFICIENCY / 0.20] * 6
    )
    assert payload["capacity_kwh"] < mixed_median  # mixing would have inflated it


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

    # The measured pair wins over the command: ``battery_power`` is the power_setpoint_entity, and
    # a pack that did not follow the setpoint (a taper) would otherwise inflate the AC energy.
    both = paired + [{"ts": stamp, "entity_role": "battery_power:battery_1", "value": 900.0}]
    assert samples_from_rows(both, "battery_1") == [(T0, 500.0, 42.0)]


def test_a_payload_from_the_old_formula_is_reported_but_never_applied():
    """0.31.0 changed what a window measures (flow, not setpoint): an old estimate is not trusted."""
    now = T0 + timedelta(days=1)
    legacy = {
        "capacity_kwh": LEARNED_KWH,
        "median_kwh": LEARNED_KWH,
        "samples": 6,
        "windows": [LEARNED_KWH] * 6,
        "updated_at": to_iso(now),
    }
    status = capacity_status(legacy, now)
    assert status["applied"] is False
    assert status["capacity_kwh"] is None, "the effective capacity must stay the typed seed"
    assert status["learned_kwh"] == pytest.approx(LEARNED_KWH), "the evidence is still reported"
    assert status["reason"] == REASON_LOW_CONFIDENCE
    assert capacity_status({**legacy, "schema": SCHEMA}, now)["applied"] is True


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
TOLERANCE = ABOVE_REPORTED_TOLERANCE
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


def build_engine(capacity_source: str = "auto"):
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
        "basis": "charge",
        "schema": SCHEMA,
        "updated_at": to_iso(now),
    }
    store.profile_put(PROFILE_KIND, battery_id, payload, now)


def test_pinned_source_always_uses_the_configured_capacity():
    engine, store, _settings, clock = build_engine(capacity_source="pinned")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == 5.0
    _kwh, status = engine.effective_capacity(config)
    assert status["reason"] == REASON_MANUAL and status["applied"] is False
    assert engine.effective_config(config).capacity_kwh == 5.0


def test_legacy_manual_source_now_learns_like_auto():
    """0.31.0: ``manual``/``learned`` both mean auto, so a typed capacity is only a seed."""
    for legacy in ("manual", "learned"):
        engine, store, _settings, clock = build_engine(capacity_source=legacy)
        put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
        config = engine.battery_configs()[0]
        assert config.learns_from_measurements is True
        assert engine.effective_capacity_kwh(config) == pytest.approx(LEARNED_KWH)


def test_auto_is_the_shipped_default():
    assert BatteryConfig(
        id="b", name="B", driver="generic", capacity_kwh=5.0,
        mapping={"power_setpoint_entity": "number.example_battery_power"},
    ).capacity_source == "auto"
    assert "pinned" in models.CAPACITY_SOURCES


def test_learned_source_wins_when_confident_and_fresh():
    engine, store, _settings, clock = build_engine(capacity_source="auto")
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
        engine, store, _settings, clock = build_engine(capacity_source="auto")
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
    engine, _store, _settings, _clock = build_engine(capacity_source="auto")
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == 5.0
    assert engine.capacity_status(config.id)["reason"] == REASON_UNAVAILABLE


def test_plan_and_allocation_use_the_learned_capacity():
    """The planner receives the effective capacity: the published plan states it."""
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    plan = engine.plan_now("learned-capacity-test")
    assert plan is not None and plan.batteries
    assert plan.batteries[0]["capacity_kwh"] == pytest.approx(LEARNED_KWH)


def put_round_trip(store, battery_id: str, *, round_trip: float, samples: int, now: datetime) -> None:
    eta = round_trip**0.5
    store.profile_put(
        efficiency_learning.PROFILE_KIND,
        battery_id,
        {
            "round_trip": round_trip,
            "median_round_trip": round_trip,
            "eta_charge": eta,
            "eta_discharge": eta,
            "samples": samples,
            "pairs": [],
            "schema": efficiency_learning.SCHEMA,
            "updated_at": to_iso(now),
        },
        now,
    )


def test_the_learned_round_trip_reaches_the_plan():
    """SPEC 5: the DP, the published summary and the wear read the learned round trip."""
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_round_trip(store, EXAMPLE_BATTERY.id, round_trip=0.75, samples=4, now=clock.now)
    config = engine.battery_configs()[0]
    (eta_charge, eta_discharge), status = engine.effective_efficiency(config)
    assert eta_charge == pytest.approx(0.75**0.5)
    assert eta_discharge == pytest.approx(0.75**0.5)
    assert status["applied"] is True and status["reason"] == efficiency_learning.REASON_APPLIED
    effective = engine.effective_config(config)
    assert effective.round_trip_efficiency == pytest.approx(0.75)
    plan = engine.plan_now("learned-efficiency-test")
    assert plan is not None and plan.batteries
    assert plan.batteries[0]["round_trip_efficiency"] == pytest.approx(0.75)
    assert plan.summary.round_trip_efficiency == pytest.approx(0.75)


def test_pinned_ignores_the_learned_round_trip_too():
    engine, store, _settings, clock = build_engine(capacity_source="pinned")
    put_round_trip(store, EXAMPLE_BATTERY.id, round_trip=0.75, samples=4, now=clock.now)
    config = engine.battery_configs()[0]
    (eta_charge, _eta_discharge), status = engine.effective_efficiency(config)
    assert eta_charge == pytest.approx(EXAMPLE_BATTERY.eta_charge)
    assert status["applied"] is False and status["reason"] == efficiency_learning.REASON_PINNED


def test_learn_capacities_updates_the_profile_from_measurements():
    engine, store, _settings, clock = build_engine(capacity_source="auto")
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
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    record = engine.batteries_view()[0]
    assert record["config"]["capacity_source"] == "auto"
    assert record["config"]["capacity_kwh"] == 5.0  # configured seed stays visible
    assert record["capacity_source"] == "auto"
    assert record["capacity_kwh"] == 5.0
    assert record["capacity_learned_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["capacity_samples"] == 6
    assert record["capacity_updated_at"] == to_iso(clock.now)
    assert record["effective_capacity_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["capacity"]["applied"] is True
    # One pack, one capacity: the record says which side of the cycle measured it.
    assert record["capacity"]["basis"] == "charge"


# ------------------------------------------------------------------ reported-capacity ceiling


def test_cap_to_reported_only_ever_lowers_the_estimate():
    """SPEC 5: the pack's own reported capacity is a ceiling, never a target."""
    from app.capacity import ABOVE_REPORTED_TOLERANCE, cap_to_reported  # noqa: PLC0415

    assert cap_to_reported(8.5, 6.0) == 6.0
    assert cap_to_reported(5.0, 6.0) == 5.0
    assert cap_to_reported(8.5, None) == 8.5
    assert cap_to_reported(8.5, 0.0) == 8.5
    assert cap_to_reported(8.5, -1.0) == 8.5
    assert cap_to_reported(None, 6.0) is None
    # 0.37.2: a small measured overshoot is planned (the charge-side estimate carries the unknown
    # efficiency split), only the excess is trimmed.
    ceiling = 6.0 * (1.0 + ABOVE_REPORTED_TOLERANCE)
    assert cap_to_reported(6.2, 6.0, tolerance=ABOVE_REPORTED_TOLERANCE) == 6.2
    assert cap_to_reported(8.5, 6.0, tolerance=ABOVE_REPORTED_TOLERANCE) == pytest.approx(ceiling)


def test_a_learned_estimate_above_the_reported_capacity_is_capped():
    """0.37.x live: 2.30 kWh learned on a 1.92 kWh pack; planning must use the hardware's 1.92."""
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    engine._note_ha_read(  # noqa: SLF001 - the hardware reading is the input under test
        [models.BatteryState(id=EXAMPLE_BATTERY.id, available=True, capacity_kwh=6.0)]
    )
    ceiling = 6.0 * (1.0 + TOLERANCE)
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == pytest.approx(ceiling)
    _value, status = engine.effective_capacity(config)
    assert status["applied"] is True and status["reason"] == REASON_APPLIED
    assert status["capped"] is True
    assert status["capped_kwh"] == pytest.approx(LEARNED_KWH)  # the measurement stays visible
    assert status["cap_kwh"] == pytest.approx(ceiling)
    assert status["reported_kwh"] == pytest.approx(6.0)
    # The planner, and therefore the published plan, sees the bounded value.
    assert engine.effective_battery_configs()[0].capacity_kwh == pytest.approx(ceiling)
    plan = engine.plan_now("capped-capacity-test")
    assert plan is not None and plan.batteries
    assert plan.batteries[0]["capacity_kwh"] == pytest.approx(ceiling)
    # The journal says both numbers instead of silently dropping the estimate.
    applied = [e for e in store.events if e["code"] == "capacity_learned_applied"]
    assert applied and applied[-1]["payload"]["reported_kwh"] == pytest.approx(6.0)
    assert applied[-1]["payload"]["ceiling_kwh"] == pytest.approx(ceiling)
    assert applied[-1]["payload"]["learned_kwh"] == pytest.approx(LEARNED_KWH)


def test_the_reported_capacity_survives_the_plan_scale_rescale():
    """0.37.3: the driver's kWh becomes the plan's scale, so the ceiling must read the saved one."""
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    state = models.BatteryState(
        id=EXAMPLE_BATTERY.id,
        available=True,
        soc_pct=58.0,
        capacity_kwh=3.0,  # already rescaled by the dispatcher
        energy_kwh=1.74,
        extra={"capacity_reported": True, "reported_capacity_kwh": 1.92},
    )
    engine._note_ha_read([state])
    status = engine.capacity_status(EXAMPLE_BATTERY.id)
    assert status["reported_kwh"] == pytest.approx(1.92)
    assert status["capped"] is True  # 8.5 is trimmed by the 25 % band around the *reported* 1.92
    assert status["cap_kwh"] == pytest.approx(1.92 * 1.25)
    assert engine.effective_capacity_kwh(EXAMPLE_BATTERY) == pytest.approx(1.92 * 1.25)


def test_batteries_view_exposes_the_cap_and_keeps_the_raw_estimate():
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=LEARNED_KWH, samples=6, now=clock.now)
    engine._note_ha_read(  # noqa: SLF001 - see above
        [models.BatteryState(id=EXAMPLE_BATTERY.id, available=True, capacity_kwh=6.0)]
    )
    ceiling = 6.0 * (1.0 + TOLERANCE)
    record = engine.batteries_view()[0]
    assert record["capacity_learned_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["effective_capacity_kwh"] == pytest.approx(ceiling)
    assert record["capacity"]["capped"] is True
    assert record["capacity"]["cap_kwh"] == pytest.approx(ceiling)
    assert record["capacity"]["reported_kwh"] == pytest.approx(6.0)
    assert record["capacity"]["learned_kwh"] == pytest.approx(LEARNED_KWH)
    assert record["capacity"]["effective_kwh"] == pytest.approx(ceiling)


def test_an_estimate_below_the_reported_capacity_is_untouched():
    engine, store, _settings, clock = build_engine(capacity_source="auto")
    put_estimate(store, EXAMPLE_BATTERY.id, kwh=4.2, samples=6, now=clock.now)
    engine._note_ha_read(  # noqa: SLF001 - see above
        [models.BatteryState(id=EXAMPLE_BATTERY.id, available=True, capacity_kwh=6.0)]
    )
    config = engine.battery_configs()[0]
    assert engine.effective_capacity_kwh(config) == pytest.approx(4.2)
    assert "capped" not in engine.capacity_status(EXAMPLE_BATTERY.id)


def test_a_window_that_ends_at_the_clamped_soc_ceiling_is_rejected():
    """0.37.2: a pack held at 100 % keeps accepting charge the reported SoC cannot show."""
    at_top = _window(T0, 20.0, 100.0, 500.0, steps=20)
    payload = _estimated(at_top)
    assert payload["capacity_kwh"] is None
    assert payload["rejected"]["edge"] == 1

    just_below = _window(T0, 20.0, 98.0, 500.0, steps=20)
    payload = _estimated(just_below)
    assert payload["samples"] == 1  # accepted (one window is not yet a published estimate)
    assert payload["rejected"]["edge"] == 0
