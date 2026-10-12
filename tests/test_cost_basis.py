"""Cost basis of the stored energy and the sell floor (SPEC.md section 5, additive 0.18.0).

Covers the event-driven ledger (measured integration, attribution from the meter and the plan
reason, price-slot splitting, discharges, SoC-floor resets, idempotent replay, missing prices), the
fleet floor through the frozen break-even helper, the DP slot reason, the dispatch backstop and the
API/sensor surface.  Every fixture is obviously fake (SPEC R1).
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_manager"))

from app import cost_basis as ledger  # noqa: E402
from app.api import plan_summary_dict, status_dict  # noqa: E402
from app.control import safety  # noqa: E402
from app.control.dispatcher import Dispatcher  # noqa: E402
from app.control import drivers as driver_registry  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import _FakeHA, _FakeStore, _selfcheck_settings, _selfcheck_sources  # noqa: E402
from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    BatteryState,
    Measurement,
    Mode,
    Plan,
    PlanSlot,
    PlanSummary,
    Reason,
    SystemState,
    to_iso,
)
from app.optimize import dp  # noqa: E402
from app.store import Store  # noqa: E402

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def battery(**overrides) -> BatteryConfig:
    data = dict(
        id="example_battery_1",
        name="Example Battery 1",
        driver="generic",
        capacity_kwh=5.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=2000.0,
        max_discharge_w=2000.0,
        eta_charge=1.0,
        eta_discharge=1.0,
        mapping={"power_setpoint_entity": "number.example_battery_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def points(start: datetime, count: int, value: float, *, minutes: int = 5) -> tuple:
    return tuple((start + timedelta(minutes=minutes * index), value) for index in range(count))


def dispatch_row(
    row_id: int,
    ts: datetime,
    applied_w: float,
    *,
    code: str = "arbitrage_charge",
    mode: str = "auto",
    dry_run: int = 0,
    result: str = "ok",
) -> dict:
    return {
        "id": row_id,
        "ts": to_iso(ts),
        "battery_id": "example_battery_1",
        "applied_w": applied_w,
        "mode": mode,
        "dry_run": dry_run,
        "result": result,
        "code": code,
    }


def slot(start: datetime, buy: float, sell: float | None, reason: str = "arbitrage_charge", minutes: int = 60):
    return ledger.PriceSlot(t=start, minutes=minutes, buy=buy, sell=sell, reason=reason)


def book(rows, series, timeline, battery_config, *, now, state=None, reset=False):
    return ledger.book_slices(
        state,
        battery=battery_config,
        series=series,
        timeline=timeline,
        dispatch_rows=rows,
        now=now,
        reset=reset,
    )


# ------------------------------------------------------------------------------- reading rows


def test_measurement_series_reads_signed_or_paired_power():
    rows = [
        {"ts": to_iso(T0), "entity_role": "battery_power:example_battery_1", "value": 500.0},
        {"ts": to_iso(T0), "entity_role": "battery_soc:example_battery_1", "value": 42.0},
        {"ts": to_iso(T0), "entity_role": "grid_power", "value": 200.0},
        {"ts": to_iso(T0 + timedelta(minutes=5)), "entity_role": "battery_charge:example_battery_1", "value": 600.0},
        {"ts": to_iso(T0 + timedelta(minutes=5)), "entity_role": "battery_discharge:example_battery_1", "value": 200.0},
        {"ts": to_iso(T0 + timedelta(minutes=5)), "entity_role": "grid_import", "value": 300.0},
        {"ts": to_iso(T0 + timedelta(minutes=5)), "entity_role": "grid_export", "value": 100.0},
        {"ts": to_iso(T0 + timedelta(minutes=5)), "entity_role": "battery_soc:example_battery_1", "value": 44.0},
        {"ts": "not-a-time", "entity_role": "battery_power:example_battery_1", "value": 999.0},
        {"ts": to_iso(T0), "entity_role": "battery_power:someone_else", "value": 999.0},
    ]
    series = ledger.measurement_series(rows, "example_battery_1")
    assert [value for _t, value in series.power] == [500.0, 400.0]
    assert [value for _t, value in series.grid] == [200.0, 200.0]
    assert [value for _t, value in series.soc] == [42.0, 44.0]
    assert ledger.measurement_series([], "example_battery_1") == ledger.Series()


# ------------------------------------------------------------------------------- the ledger


def test_grid_and_pv_are_attributed_from_the_measured_meter():
    """0.8 kWh in, half the import grid at 0.20 and the rest PV at the 0.05 export forgone."""
    config = battery()
    series = ledger.Series(
        power=points(T0, 13, 800.0), grid=points(T0, 13, 500.0), soc=points(T0, 13, 50.0)
    )
    payload = book(
        [dispatch_row(1, T0, 800.0)],
        series,
        ledger.PriceTimeline([slot(T0, 0.20, 0.05)]),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert payload["energy_kwh"] == pytest.approx(0.8)
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.115 / 0.8)  # (0.5*0.20 + 0.3*0.05) / 0.8
    assert payload["intervals"] == 1
    entry = payload["journal"][-1]
    assert entry["grid_kwh"] == pytest.approx(0.5)
    assert entry["pv_kwh"] == pytest.approx(0.3)
    assert entry["cost_eur"] == pytest.approx(0.115)
    assert entry["reason"] == "arbitrage_charge"


def test_energy_is_measured_not_commanded():
    config = battery()
    series = ledger.Series(
        power=points(T0, 13, 200.0), grid=points(T0, 13, 200.0), soc=points(T0, 13, 50.0)
    )
    payload = book(
        [dispatch_row(1, T0, 800.0)],  # commanded 800 W, measured 200 W
        series,
        ledger.PriceTimeline([slot(T0, 0.20, 0.05)]),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert payload["energy_kwh"] == pytest.approx(0.2)
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)


def test_integration_follows_every_sample_not_just_the_range_ends():
    """A slice must be the piecewise-linear integral through its samples, never one straight line."""
    points = ledger._Points(
        [(T0, 0.0), (T0 + timedelta(minutes=5), 2000.0), (T0 + timedelta(minutes=10), 0.0)]
    )
    # (0 + 2000)/2 * 5 min + (2000 + 0)/2 * 5 min = 0.1667 kWh; a straight line between the ends
    # would be 0, and an endpoint-only integration returns exactly that.
    assert points.integrate_kwh(T0, T0 + timedelta(minutes=10))[0] == pytest.approx(2.0 * 5.0 / 60.0)
    # Clamping still splits each segment at its own zero crossing: half of 500 W over 5 minutes.
    below = ledger._Points([(T0, -500.0), (T0 + timedelta(minutes=10), 500.0)])
    assert below.integrate_kwh(T0, T0 + timedelta(minutes=10))[0] == pytest.approx(
        500.0 * (5.0 / 60.0) / 2.0 / 1000.0
    )
    # A hole wider than the bound is never bridged: two samples an hour apart contribute nothing
    # to the interval between them (the 10-minute extrapolation either side is clipped away).
    sparse = ledger._Points([(T0, 500.0), (T0 + timedelta(hours=1), 500.0)])
    assert sparse.integrate_kwh(T0, T0 + timedelta(hours=1))[0] == 0.0


def test_weighted_average_across_two_charges():
    config = battery()
    timeline = ledger.PriceTimeline(
        [
            slot(T0, 0.20, 0.05, minutes=60),
            slot(T0 + timedelta(hours=1), 0.40, 0.05, minutes=60),
        ]
    )
    rows = [dispatch_row(1, T0, 400.0)]
    series = ledger.Series(
        power=points(T0, 25, 400.0),
        grid=points(T0, 25, 400.0),
        soc=points(T0, 25, 50.0),
    )
    payload = book(rows, series, timeline, config, now=T0 + timedelta(hours=2))
    # 0.4 kWh at 0.20 in the first hour, 0.4 kWh at 0.40 in the second: (0.08 + 0.16) / 0.8 = 0.30.
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.30)
    assert payload["energy_kwh"] == pytest.approx(0.8)
    assert payload["intervals"] == 1


def test_discharge_reduces_the_held_energy_by_the_round_trip():
    config = battery(eta_charge=0.95, eta_discharge=0.95)
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=60)])
    charge = points(T0, 13, 800.0)
    series = ledger.Series(power=charge, grid=points(T0, 13, 800.0), soc=points(T0, 13, 50.0))
    first = book([dispatch_row(1, T0, 800.0)], series, timeline, config, now=T0 + timedelta(hours=1))
    assert first["energy_kwh"] == pytest.approx(0.8)
    # The discharge slice is integrated from its own measured points (a fresh update window).
    second = book(
        [dispatch_row(2, T0 + timedelta(hours=1), -500.0, code="peak_discharge")],
        ledger.Series(
            power=points(T0 + timedelta(hours=1), 13, -500.0),
            grid=points(T0 + timedelta(hours=1), 13, 0.0),
            soc=points(T0 + timedelta(hours=1), 13, 44.0),
        ),
        timeline,
        config,
        now=T0 + timedelta(hours=2),
        state=first,
    )
    assert second["basis_eur_per_kwh"] == pytest.approx(0.20)  # basis unchanged by a discharge
    assert second["energy_kwh"] == pytest.approx(0.8 - 0.5 / 0.9025, abs=1e-4)
    # A second discharge empties the book: no energy left, so no basis to floor.
    third = book(
        [dispatch_row(3, T0 + timedelta(hours=2), -800.0, code="peak_discharge")],
        ledger.Series(
            power=points(T0 + timedelta(hours=2), 13, -800.0),
            grid=points(T0 + timedelta(hours=2), 13, 0.0),
            soc=points(T0 + timedelta(hours=2), 13, 30.0),
        ),
        timeline,
        config,
        now=T0 + timedelta(hours=3),
        state=second,
    )
    assert third["energy_kwh"] == 0.0
    assert third["basis_eur_per_kwh"] is None


def test_soc_floor_resets_the_ledger():
    """At/below the floor nothing usable is held: the basis is cleared before and after the slice."""
    config = battery(min_soc_pct=10.0)
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05)])
    state = {"basis_eur_per_kwh": 0.50, "energy_kwh": 1.0, "last_ts": to_iso(T0)}
    # (a) starting at the floor: the charge is priced on its own, not blended with 1.0 kWh at 0.50.
    start_low = ledger.Series(
        power=points(T0, 13, 400.0),
        grid=points(T0, 13, 400.0),
        soc=tuple((T0 + timedelta(minutes=5 * index), 10.5 + 3.0 * index) for index in range(13)),
    )
    payload = book([], start_low, timeline, config, now=T0 + timedelta(hours=1), state=state)
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["energy_kwh"] == pytest.approx(0.4)
    # (b) dipping to the floor during the slice clears the book at the end.
    down_to_floor = ledger.Series(
        power=points(T0, 13, 400.0) + points(T0 + timedelta(hours=1), 1, 0.0),
        grid=points(T0, 13, 400.0) + points(T0 + timedelta(hours=1), 1, 0.0),
        soc=points(T0, 13, 50.0) + ((T0 + timedelta(hours=1), 10.5),),
    )
    cleared = book(
        [dispatch_row(1, T0, 400.0)],
        down_to_floor,
        timeline,
        config,
        now=T0 + timedelta(hours=1),
        state=state,
    )
    assert cleared["basis_eur_per_kwh"] is None
    assert cleared["energy_kwh"] == 0.0
    # (c) no SoC reading: the reset cannot fire, the ledger keeps accruing.
    without_soc = book(
        [],
        ledger.Series(power=points(T0, 13, 400.0), grid=points(T0, 13, 400.0)),
        timeline,
        config,
        now=T0 + timedelta(hours=1),
        state=state,
    )
    assert without_soc["basis_eur_per_kwh"] == pytest.approx((0.5 * 1.0 + 0.08) / 1.4)


def test_price_slot_boundary_splits_the_energy():
    config = battery()
    timeline = ledger.PriceTimeline(
        [
            slot(T0, 0.10, 0.10, minutes=30),
            slot(T0 + timedelta(minutes=30), 0.30, 0.30, minutes=30),
        ]
    )
    series = ledger.Series(
        power=points(T0, 13, 400.0), grid=points(T0, 13, 400.0), soc=points(T0, 13, 50.0)
    )
    payload = book(
        [dispatch_row(1, T0, 400.0)], series, timeline, config, now=T0 + timedelta(hours=1)
    )
    # 0.2 kWh at each side: (0.2*0.10 + 0.2*0.30) / 0.4 = 0.20, not the 0.10 of the opening slot.
    assert payload["energy_kwh"] == pytest.approx(0.4)
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    # The split is energy, not time: half the energy in each priced half.
    assert payload["journal"][-1]["cost_eur"] == pytest.approx(0.08)


def test_missing_price_skips_the_slice_and_counts():
    config = battery()
    series = ledger.Series(
        power=points(T0, 13, 800.0), grid=points(T0, 13, 800.0), soc=points(T0, 13, 50.0)
    )
    payload = book(
        [dispatch_row(1, T0, 800.0)],
        series,
        ledger.PriceTimeline(),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert payload["basis_eur_per_kwh"] is None
    assert payload["energy_kwh"] == 0.0
    assert payload["skipped_no_price"] == 1
    assert payload["intervals"] == 1


def test_missing_sell_falls_back_to_buy_and_missing_meter_to_the_reason():
    config = battery()
    # No sell on the slot: the PV part is priced at buy.
    payload = book(
        [dispatch_row(1, T0, 400.0)],
        ledger.Series(power=points(T0, 13, 400.0), soc=points(T0, 13, 50.0)),
        ledger.PriceTimeline([slot(T0, 0.20, None, reason="arbitrage_charge")]),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["skipped_no_grid"] == 1
    # No meter at all: the plan slot's own reason decides PV vs grid.
    pv_charge = book(
        [dispatch_row(1, T0, 400.0)],
        ledger.Series(power=points(T0, 13, 400.0), soc=points(T0, 13, 50.0)),
        ledger.PriceTimeline([slot(T0, 0.20, 0.05, reason=Reason.PV_SURPLUS_CHARGE)]),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert pv_charge["basis_eur_per_kwh"] == pytest.approx(0.05)
    grid_charge = book(
        [dispatch_row(1, T0, 400.0)],
        ledger.Series(power=points(T0, 13, 400.0), soc=points(T0, 13, 50.0)),
        ledger.PriceTimeline([slot(T0, 0.20, 0.05, reason=Reason.ARBITRAGE_CHARGE)]),
        config,
        now=T0 + timedelta(hours=1),
    )
    assert grid_charge["basis_eur_per_kwh"] == pytest.approx(0.20)


def test_only_an_applied_auto_change_opens_a_slice_and_idempotent_replay_books_once():
    config = battery()
    rows = [
        dispatch_row(1, T0, 500.0),
        dispatch_row(2, T0 + timedelta(minutes=10), 510.0),  # within tolerance: same slice
        dispatch_row(3, T0 + timedelta(minutes=20), 510.0, mode=Mode.ADVISORY.value, dry_run=1),
        dispatch_row(4, T0 + timedelta(minutes=30), 510.0, result="error"),
        dispatch_row(5, T0 + timedelta(minutes=40), 900.0),
    ]
    series = ledger.Series(
        power=points(T0, 13, 500.0), grid=points(T0, 13, 500.0), soc=points(T0, 13, 50.0)
    )
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05)])
    now = T0 + timedelta(hours=1)
    payload = book(rows, series, timeline, config, now=now)
    assert payload["intervals"] == 2
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["energy_kwh"] == pytest.approx(0.5)
    assert payload["last_applied_w"] == pytest.approx(900.0)
    # Replaying the same update with the same `now` books nothing twice.
    again = book(rows, series, timeline, config, now=now, state=payload)
    assert again["intervals"] == payload["intervals"]
    assert again["energy_kwh"] == pytest.approx(payload["energy_kwh"])
    assert again["basis_eur_per_kwh"] == pytest.approx(payload["basis_eur_per_kwh"])


def test_journal_is_bounded_to_the_last_entries():
    config = battery()
    rows = [
        dispatch_row(index + 1, T0 + timedelta(minutes=5 * index), 100.0 if index % 2 == 0 else -100.0)
        for index in range(ledger.MAX_JOURNAL + 20)
    ]
    span = len(rows) + 1
    series = ledger.Series(
        power=points(T0, span, 100.0), grid=points(T0, span, 100.0), soc=points(T0, span, 50.0)
    )
    payload = book(
        rows,
        series,
        ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=5 * span)]),
        config,
        now=T0 + timedelta(minutes=5 * span),
    )
    assert payload["intervals"] == len(rows)
    assert len(payload["journal"]) == ledger.MAX_JOURNAL


def test_covered_gap_is_not_bridged():
    config = battery()
    # Measurements at 10:00-10:10 and 10:50-11:00 with a 40-minute hole: only the covered parts book.
    series = ledger.Series(
        power=points(T0, 3, 800.0) + points(T0 + timedelta(minutes=50), 3, 800.0),
        grid=points(T0, 3, 800.0) + points(T0 + timedelta(minutes=50), 3, 800.0),
        soc=points(T0, 3, 50.0) + points(T0 + timedelta(minutes=50), 3, 50.0),
    )
    payload = book(
        [dispatch_row(1, T0, 800.0)],
        series,
        ledger.PriceTimeline([slot(T0, 0.20, 0.05)]),
        config,
        now=T0 + timedelta(hours=1),
    )
    # 10 minutes at the head + 10 at the tail = 0.2666... kWh, never the invented 40-minute bridge.
    assert payload["energy_kwh"] == pytest.approx(800.0 / 1000.0 * (20.0 / 60.0), abs=1e-4)
    assert payload["skipped_no_power"] == 0


# ------------------------------------------------------------------------------- store wiring


def _seed_store(store: Store, config: BatteryConfig, *, start: datetime, hours: int = 1) -> None:
    store.add_measurements(
        [
            Measurement(ts=start + timedelta(minutes=5 * index), role=f"battery_power:{config.id}", value=800.0, unit="W")
            for index in range(hours * 12 + 1)
        ]
        + [
            Measurement(ts=start + timedelta(minutes=5 * index), role=f"battery_soc:{config.id}", value=50.0, unit="%")
            for index in range(hours * 12 + 1)
        ]
        + [
            Measurement(ts=start + timedelta(minutes=5 * index), role="grid_power", value=800.0, unit="W")
            for index in range(hours * 12 + 1)
        ]
    )
    store.add_dispatch(
        ts=start,
        battery_id=config.id,
        requested_w=800.0,
        applied_w=800.0,
        mode=Mode.AUTO.value,
        dry_run=False,
        result="ok",
        code="arbitrage_charge",
    )
    plan = Plan(
        generated_at=start - timedelta(minutes=1),
        horizon_start=start,
        horizon_end=start + timedelta(hours=hours + 2),
        slot_minutes=60,
        slots=[
            PlanSlot(
                t=start + timedelta(hours=index),
                buy=0.20,
                sell=0.05,
                pv_kw=0.0,
                load_kw=0.5,
                soc_kwh=2.5,
                power_w=0.0,
                action=Action.HOLD,
                reason=Reason.ARBITRAGE_CHARGE,
            )
            for index in range(hours + 2)
        ],
    )
    store.save_plan(plan)


def test_update_basis_round_trips_through_the_store(tmp_path):
    store = Store(tmp_path / "cost.db")
    config = battery()
    _seed_store(store, config, start=T0)
    now = T0 + timedelta(hours=1)
    payload = ledger.update_basis(store, config, now)
    assert payload is not None
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["energy_kwh"] == pytest.approx(0.8)
    assert payload["intervals"] == 1
    stored = ledger.load_basis(store, config.id)
    assert stored is not None and stored["intervals"] == 1
    # Replaying at the same `now` books nothing new (the row is keyed by its slice end).
    replay = ledger.update_basis(store, config, now)
    assert replay is not None and replay["intervals"] == 1
    # A later update books only the new tail.
    later = ledger.update_basis(store, config, now + timedelta(hours=1))
    assert later is not None and later["intervals"] == 2
    assert later["basis_eur_per_kwh"] == pytest.approx(0.20)
    store.close()


def test_update_basis_survives_missing_store_rows_and_a_long_gap(tmp_path):
    config = battery()
    assert ledger.update_basis(None, config, T0) is None
    assert ledger.update_basis(object(), config, T0) is None
    store = Store(tmp_path / "gap.db")
    _seed_store(store, config, start=T0)
    # A stored ledger older than the lookback: its contents can no longer be attributed, so the
    # update starts clean instead of blending ten-day-old energy in.
    store.profile_put(
        ledger.PROFILE_KIND,
        config.id,
        {
            "basis_eur_per_kwh": 0.50,
            "energy_kwh": 2.0,
            "last_ts": to_iso(T0 - timedelta(days=10)),
            "intervals": 3,
        },
        T0 - timedelta(days=10),
    )
    now = T0 + timedelta(hours=1)
    payload = ledger.update_basis(store, config, now)
    assert payload is not None
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["energy_kwh"] == pytest.approx(0.8)
    assert payload["window_start"] == to_iso(T0)
    store.close()


def test_basis_status_and_the_fleet_floor_use_the_frozen_helper():
    config = battery(eta_charge=0.95, eta_discharge=0.95)
    payload = {"basis_eur_per_kwh": 0.20, "energy_kwh": 1.0, "intervals": 5}
    status = ledger.basis_status(payload, config)
    assert status["cost_basis_eur_per_kwh"] == pytest.approx(0.20)
    assert status["stored_value_eur"] == pytest.approx(0.20)
    expected = 0.20 / (0.95 * 0.95)
    assert status["sell_floor_eur_per_kwh"] == pytest.approx(expected, abs=1e-6)
    assert ledger.basis_status(payload, config, margin_eur_per_kwh=0.01)[
        "sell_floor_eur_per_kwh"
    ] == pytest.approx(expected + 0.01, abs=1e-6)
    assert ledger.basis_status(payload, config, enabled=False)["sell_floor_eur_per_kwh"] is None
    # No basis: value and floor are unknown, never a fabricated 0.
    empty = ledger.basis_status(None)
    assert empty["cost_basis_eur_per_kwh"] is None and empty["stored_value_eur"] is None
    assert empty["sell_floor_eur_per_kwh"] is None
    # Fleet floor and fleet basis are energy-weighted means of the per-battery values.
    other = battery(id="example_battery_2", eta_charge=0.95, eta_discharge=0.95)
    entries = [(config, 0.20, 1.0, None), (other, 0.40, 3.0, None)]
    assert ledger.fleet_sell_floor(entries) == pytest.approx(
        (expected * 1.0 + (0.40 / (0.95 * 0.95)) * 3.0) / 4.0, abs=1e-6
    )
    assert ledger.fleet_basis_eur_per_kwh([(config, 0.20, 1.0), (other, 0.40, 3.0)]) == pytest.approx(0.35)
    assert ledger.fleet_sell_floor([(config, None, 1.0, None)]) is None


# ------------------------------------------------------------------------------- the DP


def _slots(buy_high: float = 0.50, sell_low: float = 0.05, load_kw: float = 0.5):
    return [
        dp.SlotInput(t=T0, pv_kw=0.0, load_kw=load_kw, buy=buy_high, sell=sell_low),
        dp.SlotInput(t=T0 + timedelta(hours=1), pv_kw=0.0, load_kw=load_kw, buy=0.05, sell=sell_low),
    ]


def _solve(options: dp.OptimizerOptions, *, floor: float | None, slots=None, start_kwh: float = 3.0):
    config = battery(min_soc_pct=0.0, capacity_kwh=4.0, eta_charge=1.0, eta_discharge=1.0)
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    return dp.solve(
        slots if slots is not None else _slots(),
        aggregate=aggregate,
        start_kwh=start_kwh,
        options=options,
        generated_at=T0,
        sell_floor_eur_per_kwh=floor,
    )


def test_dp_floor_blocks_a_loss_making_discharge():
    options = dp.OptimizerOptions(slot_minutes=60, power_steps=24, wear_eur_per_kwh=0.0)
    baseline = _solve(options, floor=None)
    assert baseline.slots[0].power_w < 0  # the high slot discharges to serve the house
    assert baseline.summary.sell_floor_eur_per_kwh is None

    floored = _solve(options, floor=0.60)
    assert floored.slots[0].power_w == 0.0
    assert floored.slots[0].reason == Reason.SELL_FLOOR
    assert floored.slots[0].action == Action.HOLD
    assert floored.slots[1].power_w > 0  # charging is never blocked
    assert floored.summary.sell_floor_eur_per_kwh == pytest.approx(0.60)


def test_dp_floor_allows_a_discharge_at_or_above_it():
    options = dp.OptimizerOptions(slot_minutes=60, power_steps=24, wear_eur_per_kwh=0.0)
    # The house-serving leg is the slot's buy (0.50): a 0.40 floor leaves it alone.
    below_buy = _solve(options, floor=0.40)
    assert below_buy.slots[0].power_w == pytest.approx(-500.0, abs=1.0)  # only the house's own load
    # An export slot compares with sell (0.05): a 0.40 floor refuses the export entirely.
    export_slots = [
        dp.SlotInput(t=T0, pv_kw=0.0, load_kw=0.0, buy=0.50, sell=0.05),
        dp.SlotInput(t=T0 + timedelta(hours=1), pv_kw=0.0, load_kw=0.5, buy=0.05, sell=0.05),
    ]
    baseline = _solve(options, floor=None, slots=export_slots)
    assert baseline.slots[0].power_w < 0
    floored = _solve(options, floor=0.40, slots=export_slots)
    assert floored.slots[0].power_w == 0.0
    assert floored.slots[0].reason == Reason.SELL_FLOOR


def test_lower_bound_matches_the_value_leg():
    config = battery(capacity_kwh=4.0, min_soc_pct=0.0, eta_charge=1.0, eta_discharge=1.0)
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    house = dp.SlotInput(t=T0, pv_kw=0.0, load_kw=0.5, buy=0.50, sell=0.05)
    # No floor: the full discharge range is available.
    lo, _hi, _binding = dp.power_bounds_kw(
        house, 2.0, aggregate, dp.OptimizerOptions(slot_minutes=60, sell_floor_eur_per_kwh=None), 1.0
    )
    assert lo == pytest.approx(-2.0)
    # Floor between sell and buy: no export, but the house may still be served.
    lo, _hi, _binding = dp.power_bounds_kw(
        house, 2.0, aggregate, dp.OptimizerOptions(slot_minutes=60, sell_floor_eur_per_kwh=0.40), 1.0
    )
    assert lo == pytest.approx(-0.5)
    # Floor above buy: no discharge at all.
    lo, _hi, _binding = dp.power_bounds_kw(
        house, 2.0, aggregate, dp.OptimizerOptions(slot_minutes=60, sell_floor_eur_per_kwh=0.60), 1.0
    )
    assert lo == 0.0
    # An export slot is judged by sell.
    export = dp.SlotInput(t=T0, pv_kw=0.0, load_kw=0.0, buy=0.50, sell=0.30)
    lo, _hi, _binding = dp.power_bounds_kw(
        export, 2.0, aggregate, dp.OptimizerOptions(slot_minutes=60, sell_floor_eur_per_kwh=0.40), 1.0
    )
    assert lo == 0.0
    lo, _hi, _binding = dp.power_bounds_kw(
        export, 2.0, aggregate, dp.OptimizerOptions(slot_minutes=60, sell_floor_eur_per_kwh=0.20), 1.0
    )
    assert lo == pytest.approx(-2.0)


# ------------------------------------------------------------------------------- dispatch backstop


SAFETY_SETTINGS = {
    "control.tick_seconds": 30,
    "control.stale_state_seconds": 180,
    "control.replan_minutes": 15,
    "optimizer.reserve_soc_pct": 0.0,
    "optimizer.max_grid_import_w": 0,
    "optimizer.max_grid_export_w": 0,
}


def _safety_state(config: BatteryConfig, *, override=None) -> SystemState:
    return SystemState(
        mode=Mode.AUTO.value,
        dry_run=False,
        ha_connected=True,
        override=override,
        batteries=[
            BatteryState(
                id=config.id,
                available=True,
                soc_pct=50.0,
                power_w=-500.0,
                capacity_kwh=config.capacity_kwh,
                energy_kwh=2.5,
                read_at=T0,
            )
        ],
    )


def _plan_slot(*, power_w: float = -500.0, load_kw: float = 0.5, buy: float = 0.50, sell: float = 0.05):
    return PlanSlot(
        t=T0,
        buy=buy,
        sell=sell,
        pv_kw=0.0,
        load_kw=load_kw,
        soc_kwh=2.5,
        power_w=power_w,
        action=Action.DISCHARGE if power_w < 0 else Action.CHARGE,
        reason=Reason.PEAK_DISCHARGE if power_w < 0 else Reason.ARBITRAGE_CHARGE,
    )


def test_dispatch_backstop_clamps_a_below_floor_discharge():
    config = battery()
    state = _safety_state(config)
    slot = _plan_slot()
    decision = safety.check(
        -500.0, slot, [config], state, SAFETY_SETTINGS, T0, sell_floor_eur_per_kwh=0.60
    )
    assert decision.block is False
    assert decision.allowed_w == 0.0
    assert decision.clamped is True
    assert Reason.SELL_FLOOR in decision.reasons
    # The same plan without a floor, and a floor the sale can pay for, are untouched.
    free = safety.check(-500.0, slot, [config], state, SAFETY_SETTINGS, T0)
    assert free.allowed_w == pytest.approx(-500.0)
    allowed = safety.check(
        -500.0, slot, [config], state, SAFETY_SETTINGS, T0, sell_floor_eur_per_kwh=0.40
    )
    assert allowed.allowed_w == pytest.approx(-500.0)
    assert Reason.SELL_FLOOR not in allowed.reasons


def test_dispatch_backstop_judges_the_export_leg_by_sell():
    config = battery()
    state = _safety_state(config)
    # No load, so the planned discharge is a pure export: the leg is the feed-in price.
    export_slot = _plan_slot(power_w=-800.0, load_kw=0.0, buy=0.50, sell=0.05)
    decision = safety.check(
        -800.0, export_slot, [config], state, SAFETY_SETTINGS, T0, sell_floor_eur_per_kwh=0.40
    )
    assert decision.allowed_w == 0.0
    assert Reason.SELL_FLOOR in decision.reasons
    profitable = safety.check(
        -800.0, export_slot, [config], state, SAFETY_SETTINGS, T0, sell_floor_eur_per_kwh=0.02
    )
    assert profitable.allowed_w == pytest.approx(-800.0)


def test_dispatch_backstop_leaves_charging_and_overrides_alone():
    config = battery()
    state = _safety_state(config)
    charging = _plan_slot(power_w=500.0)
    decision = safety.check(
        500.0, charging, [config], state, SAFETY_SETTINGS, T0, sell_floor_eur_per_kwh=0.90
    )
    assert decision.allowed_w == pytest.approx(500.0)
    assert Reason.SELL_FLOOR not in decision.reasons
    pinned = safety.check(
        -500.0,
        _plan_slot(),
        [config],
        _safety_state(config, override={"power_w": -500.0, "expires_at": to_iso(T0 + timedelta(hours=1))}),
        SAFETY_SETTINGS,
        T0,
        sell_floor_eur_per_kwh=0.90,
    )
    assert pinned.allowed_w == pytest.approx(-500.0)
    assert Reason.OVERRIDE_ACTIVE in pinned.reasons
    assert Reason.SELL_FLOOR not in pinned.reasons


class _FakeJournal:
    def __init__(self) -> None:
        self.dispatches: list[dict] = []
        self.events: list[tuple] = []

    def dispatch(self, entry) -> None:
        self.dispatches.append(dict(entry))

    def event(self, level, code, message, payload=None) -> None:
        self.events.append((level, code, message, payload or {}))


class _FakeHAFacade:
    def __init__(self, states=None) -> None:
        self.states = dict(states or {})
        self.calls: list[tuple[str, str, dict]] = []
        self.published: dict[str, dict] = {}

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        if domain in ("number", "input_number"):
            self.states[str(data.get("entity_id"))] = {"state": str(data.get("value")), "attributes": {}}
        return None

    def set_state(self, entity_id, state, attributes=None):
        self.published[entity_id] = {"state": state, "attributes": attributes or {}}


def test_dispatcher_records_the_floor_as_the_dispatch_reason():
    config = battery()
    ha = _FakeHAFacade({"number.example_battery_power": {"state": "-500", "attributes": {}}})
    journal = _FakeJournal()
    dispatcher = Dispatcher(
        {**SAFETY_SETTINGS, "control.grid_follow": False},
        journal,
        {config.id: driver_registry.create(config, ha)},
    )
    plan = Plan(
        generated_at=T0,
        horizon_start=T0,
        horizon_end=T0 + timedelta(hours=2),
        slot_minutes=60,
        slots=[_plan_slot()],
    )
    state = _safety_state(config)
    outcome = dispatcher.tick(
        T0, state=state, plan=plan, batteries=[config], sell_floor_eur_per_kwh=0.60
    )
    assert outcome.allowed_w == 0.0
    assert Reason.SELL_FLOOR in outcome.reasons
    assert outcome.results[0].applied_w == 0.0
    assert journal.dispatches[0]["reason"] == Reason.SELL_FLOOR


# ------------------------------------------------------------------------------- engine + API


class _Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class _FakeNotifier:
    def send(self, *_args, **_kwargs):
        return True


def _build_engine(store=None, config: BatteryConfig | None = None):
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    settings.add_battery(config or battery())
    store = store if store is not None else _FakeStore()
    clock = _Clock()
    engine = Engine(
        settings,
        store=store,
        ha=_FakeHA(),
        notifier=_FakeNotifier(),
        clock=clock,
        version="0.1.0",
        commit="example",
        **_selfcheck_sources(),
    )
    return engine, store, settings, clock


def _seed_ledger(store, config: BatteryConfig, *, now: datetime, basis: float = 0.20, energy: float = 1.0) -> None:
    store.profile_put(
        ledger.PROFILE_KIND,
        config.id,
        {
            "version": 1,
            "basis_eur_per_kwh": basis,
            "energy_kwh": energy,
            "last_ts": to_iso(now),
            "intervals": 5,
            "window_start": to_iso(now - timedelta(hours=2)),
            "journal": [
                {
                    "ts": to_iso(now - timedelta(hours=2)),
                    "end": to_iso(now - timedelta(hours=1)),
                    "charge_kwh": energy,
                    "basis_eur_per_kwh": basis,
                    "energy_kwh": energy,
                }
            ],
            "updated_at": to_iso(now),
        },
        now,
    )


def test_engine_exposes_the_basis_the_floor_and_the_sensors():
    config = battery(eta_charge=0.95, eta_discharge=0.95)
    engine, store, settings, clock = _build_engine(config=config)
    _seed_ledger(store, config, now=clock.now)
    expected_floor = 0.20 / (0.95 * 0.95)
    block = engine.cost_basis_block()
    assert block["enabled"] is True
    row = next(item for item in block["batteries"] if item["id"] == config.id)
    assert row["cost_basis_eur_per_kwh"] == pytest.approx(0.20)
    assert row["stored_value_eur"] == pytest.approx(0.20)
    assert row["journal_entries"] == 1
    assert block["fleet_basis_eur_per_kwh"] == pytest.approx(0.20)
    assert block["sell_floor_eur_per_kwh"] == pytest.approx(expected_floor, abs=1e-6)
    assert engine.fleet_sell_floor() == pytest.approx(expected_floor, abs=1e-6)
    # The margin moves the floor; disabling the setting removes it without hiding the ledger.
    settings["optimizer.sell_floor_margin_eur_per_kwh"] = 0.02
    assert engine.fleet_sell_floor() == pytest.approx(expected_floor + 0.02, abs=1e-6)
    settings["optimizer.sell_floor_enabled"] = False
    assert engine.fleet_sell_floor() is None
    assert engine.cost_basis_block()["batteries"][0]["cost_basis_eur_per_kwh"] == pytest.approx(0.20)
    settings["optimizer.sell_floor_enabled"] = True
    settings["optimizer.sell_floor_margin_eur_per_kwh"] = 0.0
    # The two new sensors mirror the ledger and are idempotent (unchanged values are not re-sent).
    published = engine.publish_states()
    assert published > 0
    ha = engine._ha
    assert float(ha.published["sensor.sem_battery_cost_basis"]["state"]) == 0.2
    assert float(ha.published["sensor.sem_battery_value"]["state"]) == 0.2
    assert ha.published["sensor.sem_battery_cost_basis"]["attributes"]["unit_of_measurement"] == "EUR/kWh"
    assert ha.published["sensor.sem_battery_value"]["attributes"]["sell_floor_eur_per_kwh"] == pytest.approx(
        expected_floor, abs=1e-6
    )
    clock.now = clock.now + timedelta(seconds=25)
    assert engine.publish_states() == 0  # unchanged payload


def test_status_and_plan_summary_carry_the_cost_basis():
    config = battery(eta_charge=0.95, eta_discharge=0.95)
    state = SystemState(
        batteries=[
            BatteryState(
                id=config.id, available=True, soc_pct=50.0, power_w=0.0, energy_kwh=1.0, read_at=T0
            )
        ]
    )
    block = {
        "enabled": True,
        "margin_eur_per_kwh": 0.0,
        "sell_floor_eur_per_kwh": 0.2216,
        "fleet_basis_eur_per_kwh": 0.20,
        "batteries": [{"id": config.id, "cost_basis_eur_per_kwh": 0.20, "stored_value_eur": 0.20}],
    }
    payload = status_dict(state, None, cost_basis=block)
    assert payload["cost_basis"]["sell_floor_eur_per_kwh"] == pytest.approx(0.2216)
    assert payload["batteries"][0]["cost_basis_eur_per_kwh"] == pytest.approx(0.20)
    assert payload["batteries"][0]["stored_value_eur"] == pytest.approx(0.20)
    # Without the block the additive keys are present but null, never missing.
    empty = status_dict(state, None)
    assert empty["cost_basis"] is None
    assert empty["batteries"][0]["cost_basis_eur_per_kwh"] is None
    assert plan_summary_dict(PlanSummary())["sell_floor_eur_per_kwh"] is None
    assert plan_summary_dict(PlanSummary(sell_floor_eur_per_kwh=0.25))["sell_floor_eur_per_kwh"] == pytest.approx(0.25)


def test_engine_update_cost_bases_advances_the_ledger_from_the_store(tmp_path):
    store = Store(tmp_path / "engine-cost.db")
    config = battery()
    engine, _store, _settings, clock = _build_engine(store=store, config=config)
    _seed_store(store, config, start=T0)
    clock.now = T0 + timedelta(hours=1)
    summary = engine.update_cost_bases(clock.now, force=True)
    assert summary["updated"] == [config.id]
    status = engine.battery_cost_basis(config.id)
    assert status["cost_basis_eur_per_kwh"] == pytest.approx(0.20)
    assert status["energy_kwh"] == pytest.approx(0.8)
    assert engine.fleet_sell_floor() == pytest.approx(0.20 / (config.eta_charge * config.eta_discharge))
    # Replaying the same update books nothing new.
    again = engine.update_cost_bases(clock.now, force=True)
    assert again["updated"] == [config.id]
    assert engine.battery_cost_basis(config.id)["intervals"] == status["intervals"]
    store.close()
