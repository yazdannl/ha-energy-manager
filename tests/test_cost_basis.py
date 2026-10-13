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
    LoadForecast,
    LoadSlot,
    Measurement,
    Mode,
    Plan,
    PlanSlot,
    PlanSummary,
    PriceForecast,
    PriceSlot,
    Reason,
    SolarForecast,
    SolarSlot,
    SystemState,
    to_iso,
)
from app.optimize import dp  # noqa: E402
from app.sources.measure import role_entities  # noqa: E402
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


def soc_for(energy_kwh: float, config: BatteryConfig) -> float:
    """The measured SoC whose physical content equals ``energy_kwh`` (0.18.1 reconciliation).

    Existing fixtures asserted the measured arithmetic with a flat 50 % SoC; the reconciliation
    now anchors the quantity to the pack, so a fixture that wants the booked value must measure
    the SoC that implies it.
    """
    return config.min_soc_pct + energy_kwh * config.eta_charge * 100.0 / config.capacity_kwh


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
        power=points(T0, 13, 800.0), grid=points(T0, 13, 500.0), soc=points(T0, 13, soc_for(0.8, config))
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
        power=points(T0, 13, 200.0), grid=points(T0, 13, 200.0), soc=points(T0, 13, soc_for(0.2, config))
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
        soc=points(T0, 25, soc_for(0.8, config)),
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
    series = ledger.Series(
        power=charge, grid=points(T0, 13, 800.0), soc=points(T0, 13, soc_for(0.8, config))
    )
    first = book([dispatch_row(1, T0, 800.0)], series, timeline, config, now=T0 + timedelta(hours=1))
    assert first["energy_kwh"] == pytest.approx(0.8)
    # The discharge slice is integrated from its own measured points (a fresh update window), and
    # the pack's own SoC measures the post-discharge content the ledger keeps.
    after_discharge = 0.8 - 0.5 / (config.eta_charge * config.eta_discharge)
    second = book(
        [dispatch_row(2, T0 + timedelta(hours=1), -500.0, code="peak_discharge")],
        ledger.Series(
            power=points(T0 + timedelta(hours=1), 13, -500.0),
            grid=points(T0 + timedelta(hours=1), 13, 0.0),
            soc=points(T0 + timedelta(hours=1), 13, soc_for(after_discharge, config)),
        ),
        timeline,
        config,
        now=T0 + timedelta(hours=2),
        state=first,
    )
    assert second["basis_eur_per_kwh"] == pytest.approx(0.20)  # basis unchanged by a discharge
    assert second["energy_kwh"] == pytest.approx(after_discharge, abs=1e-4)
    # A second discharge empties the book: no energy left, so no basis to floor.
    third = book(
        [dispatch_row(3, T0 + timedelta(hours=2), -800.0, code="peak_discharge")],
        ledger.Series(
            power=points(T0 + timedelta(hours=2), 13, -800.0),
            grid=points(T0 + timedelta(hours=2), 13, 0.0),
            soc=points(T0 + timedelta(hours=2), 13, config.min_soc_pct),
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
        soc=tuple(
            (T0 + timedelta(minutes=5 * index), 10.5 + (soc_for(0.4, config) - 10.5) / 12.0 * index)
            for index in range(13)
        ),
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
        power=points(T0, 13, 400.0), grid=points(T0, 13, 400.0), soc=points(T0, 13, soc_for(0.4, config))
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
        power=points(T0, 13, 500.0), grid=points(T0, 13, 500.0), soc=points(T0, 13, soc_for(0.5, config))
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
        soc=points(T0, 3, soc_for(800.0 / 1000.0 * (20.0 / 60.0), config))
        + points(T0 + timedelta(minutes=50), 3, soc_for(800.0 / 1000.0 * (20.0 / 60.0), config)),
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
    # The measured SoC the seeded charge physically implies, so the 0.18.1 reconciliation agrees
    # with the measured integral instead of pulling the quantity to a flat 50 % fixture.
    seeded_soc = soc_for(0.8 * hours, config)
    store.add_measurements(
        [
            Measurement(ts=start + timedelta(minutes=5 * index), role=f"battery_power:{config.id}", value=800.0, unit="W")
            for index in range(hours * 12 + 1)
        ]
        + [
            Measurement(ts=start + timedelta(minutes=5 * index), role=f"battery_soc:{config.id}", value=seeded_soc, unit="%")
            for index in range(hours * 12 + 1)
        ]
        + [
            Measurement(ts=start + timedelta(minutes=5 * index), role="grid_power", value=800.0, unit="W")
            for index in range(hours * 12 + 1)
        ]
    )
    _seed_dispatch_and_plan(store, config, start=start, hours=hours)


def _seed_dispatch_and_plan(
    store: Store, config: BatteryConfig, *, start: datetime, hours: int = 1
) -> None:
    """The applied charge and its plan, with no measurement or actuals rows (0.20.3 tests)."""
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


# --------------------------------------------------------- the floor restricts discharge (0.19.0)

#: The live-shaped 30 h horizon behind F9 (SPEC 8.14): a 1.92 kWh pack at its 10 % floor must reach
#: 100 % by the end of the horizon.  The 08:00-13:00 sell prices are the ones the live plan
#: reported; buy = sell + a fixed fee, so the price order is the live order.
_LIVE_START = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
_LIVE_SELL = {8: 0.3361, 9: 0.2989, 10: 0.2797, 11: 0.2660, 12: 0.2620, 13: 0.2734}
_LIVE_SHAPE = {
    0: 0.2900, 1: 0.2800, 2: 0.2700, 3: 0.2650, 4: 0.2680, 5: 0.2790,
    6: 0.2950, 7: 0.3150, 14: 0.2900, 15: 0.3050, 16: 0.3350, 17: 0.3650,
    18: 0.3900, 19: 0.3750, 20: 0.3500, 21: 0.3300, 22: 0.3150, 23: 0.3050,
    24: 0.2950, 25: 0.2850, 26: 0.2750, 27: 0.2700, 28: 0.2800, 29: 0.2950,
}
#: The reported forced charges were +303 / +849 / +727 W at 08:00-10:00 over a 0.5 kW house load.
_LIVE_PV = {7: 0.35, 8: 0.803, 9: 1.349, 10: 1.40, 11: 1.10, 12: 1.20, 13: 1.18, 14: 1.00, 15: 0.60, 16: 0.30}
_LIVE_FLOOR = 0.3997


def _live_slots() -> list[dp.SlotInput]:
    slots = []
    for i in range(30):
        sell = _LIVE_SELL[i] if i in _LIVE_SELL else _LIVE_SHAPE[i]
        slots.append(
            dp.SlotInput(
                t=_LIVE_START + timedelta(hours=i),
                pv_kw=_LIVE_PV.get(i, 0.0),
                load_kw=0.5,
                buy=sell + 0.1155,
                sell=sell,
            )
        )
    return slots


def _live_plan(floor: float | None, preference: str = "auto") -> Plan:
    config = battery(
        capacity_kwh=1.92,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=1000.0,
        max_discharge_w=800.0,
        eta_charge=0.92,
        eta_discharge=0.92,
    )
    price = PriceForecast(source="example_price")
    solar = SolarForecast(source="example_solar")
    load = LoadForecast(source="example_load")
    for slot in _live_slots():
        price.slots.append(PriceSlot(t=slot.t, buy=slot.buy, sell=slot.sell))
        solar.slots.append(SolarSlot(t=slot.t, pv_kw=slot.pv_kw))
        load.slots.append(LoadSlot(t=slot.t, load_kw=slot.load_kw))
    # Through ``settings``, the live path: on 0.18.1 the unknown preference key is ignored and the
    # plan is exactly the 0.18.1 one the live finding reported (+303 / +849 / +727 W at 08:00-10:00).
    settings = {
        "optimizer.slot_minutes": 60,
        "optimizer.horizon_hours": 30,
        "optimizer.power_steps": 24,
        "optimizer.allow_export": True,
        "optimizer.target_soc_pct_end": 100.0,
        "optimizer.pv_charge_preference": preference,
    }
    return dp.solve_from_forecasts(
        price,
        solar,
        load,
        battery_configs=[config],
        start_kwh=1.92 * 0.10,
        settings=settings,
        now=_LIVE_START,
        sell_floor_eur_per_kwh=floor,
    )


def test_power_bounds_floor_never_forces_a_pv_charge():
    """0.18.1 used ``max(grid_lo, -base)``, so a PV surplus (``base < 0``) became a positive lower
    bound and the pack had to swallow the whole surplus at any price.  The floor may only refuse a
    discharge; PV that would go straight to the meter has no cost basis and exports freely."""
    config = battery(
        capacity_kwh=4.0, min_soc_pct=0.0, max_charge_w=1000.0, max_discharge_w=800.0
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    options = dp.OptimizerOptions(slot_minutes=60, allow_export=True, sell_floor_eur_per_kwh=_LIVE_FLOOR)

    surplus_30 = dp.SlotInput(t=T0, pv_kw=0.80, load_kw=0.50, buy=0.4516, sell=0.3361)
    lo, hi, _binding = dp.power_bounds_kw(surplus_30, 2.0, aggregate, options, 1.0)
    assert lo == 0.0  # 0.18.1: +0.300 -- the whole surplus was forced into the pack
    assert lo > -0.30  # a below-floor battery discharge/export is still refused (p >= 0)
    assert hi == pytest.approx(1.0)
    assert dp.grid_kw_at(surplus_30, 0.0) < 0.0  # the surplus still exports while the pack holds

    surplus_85 = dp.SlotInput(t=T0, pv_kw=1.35, load_kw=0.50, buy=0.4144, sell=0.2989)
    lo, _hi, _binding = dp.power_bounds_kw(surplus_85, 2.0, aggregate, options, 1.0)
    assert lo == 0.0  # 0.18.1: +0.850

    # A deficit keeps the intended scope: discharge up to the house's own demand, no export.
    deficit = dp.SlotInput(t=T0, pv_kw=0.29, load_kw=0.50, buy=0.4516, sell=0.3361)
    lo, _hi, _binding = dp.power_bounds_kw(deficit, 2.0, aggregate, options, 1.0)
    assert lo == pytest.approx(-0.21)


def test_live_shaped_plan_keeps_the_pv_surplus_for_the_cheaper_hours():
    """The live finding (SPEC 8.14): with the fleet floor ~0.3997 the 0.18.1 plan charged
    +303 / +849 / +727 W at 08:00-10:00 instead of the cheaper 11:00-13:00 hours, because the floor
    forced the PV surplus into the pack.  The floor must leave this horizon to the price signal."""
    floored = _live_plan(_LIVE_FLOOR)
    powers = [slot.power_w for slot in floored.slots]
    assert floored.summary.sell_floor_eur_per_kwh == pytest.approx(_LIVE_FLOOR)
    # 08:00-10:00: the pack holds (0.18.1: +303 / +849 / +726 W) and the PV exports below the floor.
    assert powers[8] == 0.0 and powers[9] == 0.0 and powers[10] == 0.0
    assert floored.slots[8].action is Action.HOLD
    assert floored.slots[8].grid_kw() < 0.0
    # 11:00-13:00 carry the 1879 Wh the pack needs to reach 100 %, exactly as the unfloored plan did.
    assert powers[11] > 0.0 and powers[12] > 0.0 and powers[13] > 0.0
    assert sum(powers[8:14]) / 1000.0 == pytest.approx(1.879, abs=0.002)
    # The 13:00 slot's 617.4 W is exactly ``(e_max - e) / (1 * eta)``: the pack's ceiling, not the
    # 1000 W grid ceiling the 0.20.1 label claimed (0.20.2, SPEC 8.17).
    assert powers[11:14] == pytest.approx([565.217391304, 695.652173913, 617.391304348], abs=1e-6)
    assert [slot.reason for slot in floored.slots[11:14]] == [Reason.PV_SURPLUS_CHARGE] * 3
    unfloored = _live_plan(None)
    # The unfloored plan was already price-driven (0.18.1: 0 early, 1879 Wh late), and the floor no
    # longer touches this window; the two plans may still differ later where the floor clamps a
    # house-serving discharge, so only the window is compared.
    assert unfloored.slots[8].power_w == 0.0 and unfloored.slots[11].power_w > 0.0
    assert sum(powers[8:14]) / 1000.0 == pytest.approx(
        sum(slot.power_w for slot in unfloored.slots[8:14]) / 1000.0, abs=0.02
    )
    # The terminal target is still met exactly (the floor never blocks charging).
    aggregate = dp.BatteryAggregate.from_configs([battery(capacity_kwh=1.92, min_soc_pct=10.0)], 0.0)
    last = floored.slots[-1]
    final = dp.next_energy_kwh(last.soc_kwh, last.power_w / 1000.0, 1.0, aggregate)
    assert final == pytest.approx(1.92, abs=0.01)


def test_live_shaped_ceiling_charge_is_a_pv_charge_not_a_grid_hold():
    """0.20.1 mislabelled the live 12:00 slot ``charge / grid_limit_hold``: the sell floor sets
    ``grid_lo = 0`` in a PV-surplus hour, so ``power_bounds_kw`` reported ``binding``, and a charge
    that really sits on the pack's 100 % SoC ceiling was credited to the grid.  The reason must
    name the window the power sits on; the schedule itself is the 0.20.1 one, byte for byte."""
    config = battery(
        capacity_kwh=1.92, min_soc_pct=15.0, max_charge_w=1000.0, max_discharge_w=800.0,
        eta_charge=0.92, eta_discharge=0.92,
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    # The live plan (2026-10-13 11:04Z) reached 12:00 with e = 1.92 - 0.92 * 0.9903 kWh (52.5 %):
    # filling to 100 % in the hour takes exactly (e_max - e) / (1 h * eta) = 990.3 W, not a limit.
    e_start = 1.92 - 0.92 * 0.9903
    slot = dp.SlotInput(t=T0, pv_kw=2.02, load_kw=0.59, buy=0.2620, sell=0.2620)
    options = dp.OptimizerOptions(
        slot_minutes=60, power_steps=24, allow_export=True, target_soc_pct_end=100.0
    )
    plan = dp.solve(
        [slot], aggregate=aggregate, start_kwh=e_start, options=options, generated_at=T0,
        sell_floor_eur_per_kwh=0.3997,
    )
    s = plan.slots[0]
    # The schedule is the 0.20.1 one (pinned from the pre-fix solve): power, SoC and money unmoved.
    assert s.power_w == pytest.approx(990.3, abs=1e-9)
    assert s.soc_kwh == pytest.approx(e_start, abs=1e-12)
    assert s.soc_pct == pytest.approx(52.55, abs=1e-9)
    assert dp.next_energy_kwh(s.soc_kwh, s.power_w / 1000.0, 1.0, aggregate) == pytest.approx(
        1.92, abs=1e-12
    )
    # The money, from the slot's own inputs (grid = load - pv + p).
    expected_cost = -0.2620 * (2.02 - 0.59 - 0.9903)
    assert plan.summary.cost_estimate_eur == pytest.approx(expected_cost, abs=1e-9)
    assert plan.summary.baseline_cost_eur == pytest.approx(-0.2620 * (2.02 - 0.59), abs=1e-9)
    assert plan.summary.charged_kwh == pytest.approx(0.9903, abs=1e-9)
    assert plan.summary.pv_charge_kwh == pytest.approx(0.9903, abs=1e-9)
    assert plan.summary.grid_charge_kwh == 0.0
    assert plan.summary.objective_eur == pytest.approx(
        expected_cost - 0.2620 * 0.92 * 1.92, abs=1e-9
    )
    # The floor tightened the *lower* bound while the power sits on the SoC ceiling: the grid
    # windows and the binding flag are as before, but the label follows the window really hit.
    floored = replace(options, sell_floor_eur_per_kwh=0.3997)
    soc_lo, soc_hi = dp.soc_window_kw(e_start, aggregate, 1.0)
    grid_lo, grid_hi = dp.grid_window_kw(slot, aggregate, floored)
    assert (grid_lo, grid_hi) == (0.0, 1.0)
    assert soc_hi == pytest.approx(0.9903, abs=1e-9)
    assert soc_lo < grid_lo < soc_hi < grid_hi
    assert dp.power_bounds_kw(slot, e_start, aggregate, floored, 1.0)[2] is True
    assert s.action is Action.PV_CHARGE
    assert s.reason == Reason.PV_SURPLUS_CHARGE


def test_a_grid_capped_charge_still_names_the_grid_limit():
    """Preservation (0.20.2, SPEC 8.17): a charge parked on a grid import cap that is tighter than
    the SoC ceiling keeps ``charge / grid_limit_hold`` after the window split."""
    config = battery(
        capacity_kwh=2.0, min_soc_pct=0.0, max_charge_w=1000.0, max_discharge_w=1000.0,
        eta_charge=1.0, eta_discharge=1.0,
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    slot = dp.SlotInput(t=T0, pv_kw=0.0, load_kw=0.55, buy=0.20, sell=0.05)
    options = dp.OptimizerOptions(
        slot_minutes=60, power_steps=24, allow_export=True, max_import_kw=1.05,
        target_soc_pct_end=25.0,
    )
    plan = dp.solve([slot], aggregate=aggregate, start_kwh=0.0, options=options, generated_at=T0)
    s = plan.slots[0]
    assert s.power_w == pytest.approx(500.0, abs=1e-9)  # the import cap, not the 1000 W ceiling
    assert plan.summary.cost_estimate_eur == pytest.approx(0.21, abs=1e-9)
    assert s.action is Action.CHARGE
    assert s.reason == Reason.GRID_LIMIT_HOLD


def test_a_soc_floor_discharge_in_a_floor_slot_keeps_its_discharge_reason():
    """0.20.1 called a discharge parked on the SoC floor ``grid_limit_hold`` whenever some grid
    rule (the sell floor and the import cap here) made ``binding`` true.  The power sits on the
    pack's own 10 % floor, so the discharge reason must stand (0.20.2, SPEC 8.17)."""
    config = battery(
        capacity_kwh=2.0, min_soc_pct=10.0, max_charge_w=1000.0, max_discharge_w=1000.0,
        eta_charge=1.0, eta_discharge=1.0,
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    slot = dp.SlotInput(t=T0, pv_kw=0.0, load_kw=2.0, buy=0.55, sell=0.10)
    options = dp.OptimizerOptions(
        slot_minutes=60, power_steps=24, allow_export=True, max_import_kw=2.5
    )
    plan = dp.solve(
        [slot], aggregate=aggregate, start_kwh=0.3, options=options, generated_at=T0,
        sell_floor_eur_per_kwh=0.30,
    )
    s = plan.slots[0]
    assert s.power_w == pytest.approx(-100.0, abs=1e-9)  # 0.3 -> 0.2 kWh = the 10 % floor
    assert plan.summary.cost_estimate_eur == pytest.approx(1.045, abs=1e-9)
    assert s.action is Action.DISCHARGE
    assert s.reason == Reason.PEAK_DISCHARGE


def test_a_grid_limited_hold_still_names_the_grid_limit():
    """Preservation (0.20.2, SPEC 8.17): an export cap equal to the PV surplus pins the power at 0
    on the grid side of the window -- a genuine grid hold, still ``hold / grid_limit_hold``."""
    config = battery(
        capacity_kwh=4.0, min_soc_pct=10.0, max_charge_w=1000.0, max_discharge_w=1000.0,
        eta_charge=1.0, eta_discharge=1.0,
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    slot = dp.SlotInput(t=T0, pv_kw=1.5, load_kw=0.5, buy=0.50, sell=0.30)
    options = dp.OptimizerOptions(
        slot_minutes=60, power_steps=24, allow_export=True, max_export_kw=1.0,
        salvage_multiplier=0.0,
    )
    plan = dp.solve([slot], aggregate=aggregate, start_kwh=2.0, options=options, generated_at=T0)
    s = plan.slots[0]
    assert s.power_w == 0.0
    assert s.action is Action.HOLD
    assert s.reason == Reason.GRID_LIMIT_HOLD


def _preference_plan(preference: str, later_buy: float) -> Plan:
    """One 1.0 kW PV surplus hour (sell 0.30) and one later grid hour at ``later_buy``; the pack must
    charge exactly 1.0 kWh by the end, so every mode picks the slot its PV price points at and no
    later spread can be arbitraged."""
    config = battery(
        capacity_kwh=4.0, min_soc_pct=0.0, max_charge_w=1000.0, max_discharge_w=1000.0
    )
    aggregate = dp.BatteryAggregate.from_configs([config], 0.0)
    slots = [
        dp.SlotInput(t=T0, pv_kw=1.5, load_kw=0.5, buy=0.50, sell=0.30),
        dp.SlotInput(t=T0 + timedelta(hours=1), pv_kw=0.0, load_kw=0.5, buy=later_buy, sell=0.05),
    ]
    options = dp.OptimizerOptions(
        slot_minutes=60,
        power_steps=24,
        allow_export=True,
        target_soc_pct_end=25.0,  # 1.0 kWh of the 4 kWh pack
        pv_charge_preference=preference,
    )
    return dp.solve(slots, aggregate=aggregate, start_kwh=0.0, options=options, generated_at=T0)


def test_pv_charge_preference_modes():
    """``auto`` prices PV at the export it forgoes, ``prefer_pv`` at zero and ``avoid_pv`` at the
    slot's buy (SPEC 8.1, 0.19.0)."""
    # auto: PV is cheaper than the 0.35 grid hour, so it charges the surplus at 08:00.
    auto_pv = _preference_plan("auto", 0.35)
    assert auto_pv.slots[0].power_w == pytest.approx(1000.0)
    # auto: a 0.10 grid hour beats the 0.30 export it would forgo -> the PV exports.
    auto_grid = _preference_plan("auto", 0.10)
    assert auto_grid.slots[0].power_w == 0.0
    assert auto_grid.slots[1].power_w == pytest.approx(1000.0)
    # prefer_pv: the surplus is free, so it is soaked even though 0.10 is cheaper than the 0.30 sell.
    prefer = _preference_plan("prefer_pv", 0.10)
    assert prefer.slots[0].power_w == pytest.approx(1000.0)
    assert prefer.slots[1].power_w == 0.0
    # avoid_pv: PV is priced at this slot's 0.50 buy, so it exports and buys in the cheaper hour.
    avoid = _preference_plan("avoid_pv", 0.35)
    assert avoid.slots[0].power_w == 0.0
    assert avoid.slots[1].power_w == pytest.approx(1000.0)


def test_pv_charge_preference_never_changes_the_reported_money():
    """The PV price is an internal shadow price: the summary keeps reporting the true grid cost of
    the returned schedule (SPEC 8.1), so no preference can invent a saving."""
    for preference in ("auto", "prefer_pv", "avoid_pv"):
        plan = _preference_plan(preference, 0.10)
        true_cost = sum(slot.cost_eur(1.0) for slot in plan.slots)
        assert plan.summary.cost_estimate_eur == pytest.approx(true_cost, abs=1e-9), preference
        assert plan.summary.saved_eur == pytest.approx(
            plan.summary.baseline_cost_eur - true_cost, abs=1e-9
        ), preference
    prefer = _preference_plan("prefer_pv", 0.10)
    # The true grid cost of the prefer_pv schedule: the 1.0 kWh PV charge at 08:00 (grid 0), then the
    # 0.5 kW house hour at 0.10 -- while the objective carries the shadow credit of the forgone
    # export (-0.30) and the terminal salvage, so it must never be reported as money.
    assert prefer.summary.cost_estimate_eur == pytest.approx(0.05, abs=1e-9)
    assert prefer.summary.objective_eur == pytest.approx(-0.55, abs=1e-9)


def test_optimizer_options_validate_the_pv_preference():
    assert dp.OptimizerOptions.from_settings({}).pv_charge_preference == "auto"
    for value in ("auto", "prefer_pv", "avoid_pv"):
        assert dp.OptimizerOptions.from_settings(
            {"optimizer.pv_charge_preference": value}
        ).pv_charge_preference == value
    # A stored value that is not one of the three is a typo, never a crash in the solver.
    assert dp.OptimizerOptions.from_settings(
        {"optimizer.pv_charge_preference": "always"}
    ).pv_charge_preference == "auto"


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
            "reconciled_kwh": -0.05,
            "reconciled_last_kwh": -0.05,
            "reconciled_resets": 1,
            "skipped_no_soc": 2,
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
    # Additive (0.18.1): the reconciliation provenance travels with the block into /status.
    assert row["reconciled_kwh"] == pytest.approx(-0.05)
    assert row["reconciled_last_kwh"] == pytest.approx(-0.05)
    assert row["reconciled_resets"] == 1
    assert row["skipped_no_soc"] == 2
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


# ------------------------------------------- the sample grid and the pack reconciliation (0.18.1)


def _visible(samples, now: datetime):
    """The measurement rows a store read at ``now`` returns (``ts < now``), never the future."""
    return tuple((t, v) for t, v in samples if t < now)


def test_energy_follows_the_sample_grid_when_commands_outpace_the_samples():
    """A ~2-minute command cadence over 5-minute samples must book the whole measured integral.

    Before 0.18.1 every update booked its own tail flat at the last visible sample, so the ramp
    the next sample revealed was never recovered -- the live 2026-10-12 ledger ran ~12 % behind
    the pack (250 intervals, 142 skipped_no_power) on exactly this cadence.
    """
    config = battery(capacity_kwh=50.0)
    samples = [(T0 + timedelta(minutes=5 * index), 1000.0 * index) for index in range(9)]
    rows = [
        dispatch_row(index + 1, T0 + timedelta(minutes=2 * index), 1000.0 + 50.0 * index)
        for index in range(20)
    ]
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=120)])
    state = None
    for minute in range(1, 42):
        now = T0 + timedelta(minutes=minute)
        visible = _visible(samples, now)
        state = book(
            rows, ledger.Series(power=visible, grid=visible), timeline, config, now=now, state=state
        )
    expected = ledger._Points(samples).integrate_kwh(T0, T0 + timedelta(minutes=40))[0]
    assert state["energy_kwh"] == pytest.approx(expected, rel=0.01)
    assert state["skipped_no_power"] == 0
    # The idempotency key is the end of the settled sample span, never a command timestamp.
    assert state["last_ts"] == to_iso(T0 + timedelta(minutes=40))


def test_a_command_interval_between_two_samples_books_its_measured_share():
    """A command change faster than the sampler still books the measured integral over its span."""
    config = battery()
    samples = ((T0, 0.0), (T0 + timedelta(minutes=5), 1000.0))
    rows = [
        dispatch_row(1, T0, 1000.0),
        dispatch_row(2, T0 + timedelta(minutes=2), 500.0),
        dispatch_row(3, T0 + timedelta(minutes=4), 1000.0),
    ]
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05)])
    payload = book(
        rows,
        ledger.Series(power=samples, grid=samples),
        timeline,
        config,
        now=T0 + timedelta(minutes=5),
    )
    # The ramp 0 -> 1000 W over 5 minutes: 2500 W*min, split 400/1200/900 over the commands.
    assert payload["energy_kwh"] == pytest.approx(2500.0 / 60.0 / 1000.0, abs=1e-4)
    assert payload["skipped_no_power"] == 0
    middle = payload["journal"][1]
    assert middle["ts"] == to_iso(T0 + timedelta(minutes=2))
    assert middle["end"] == to_iso(T0 + timedelta(minutes=4))
    assert middle["charge_kwh"] == pytest.approx(1200.0 / 60.0 / 1000.0, abs=1e-6)


def test_sample_grid_booking_is_idempotent_and_never_double_books():
    config = battery()
    samples = [(T0 + timedelta(minutes=5 * index), 1000.0 * index) for index in range(5)]
    rows = [
        dispatch_row(index + 1, T0 + timedelta(minutes=2 * index), 1000.0 + 50.0 * index)
        for index in range(10)
    ]
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=60)])
    first_now = T0 + timedelta(minutes=11)
    visible = _visible(samples, first_now)
    first = book(
        rows, ledger.Series(power=visible, grid=visible), timeline, config, now=first_now
    )
    assert first["last_ts"] == to_iso(T0 + timedelta(minutes=10))
    assert first["energy_kwh"] == pytest.approx(
        ledger._Points(samples).integrate_kwh(T0, T0 + timedelta(minutes=10))[0], abs=1e-4
    )
    replay = book(
        rows,
        ledger.Series(power=visible, grid=visible),
        timeline,
        config,
        now=first_now,
        state=first,
    )
    assert replay["intervals"] == first["intervals"]
    assert replay["energy_kwh"] == pytest.approx(first["energy_kwh"])
    later_now = T0 + timedelta(minutes=21)
    visible_later = _visible(samples, later_now)
    later = book(
        rows,
        ledger.Series(power=visible_later, grid=visible_later),
        timeline,
        config,
        now=later_now,
        state=replay,
    )
    assert later["energy_kwh"] == pytest.approx(
        ledger._Points(samples).integrate_kwh(T0, T0 + timedelta(minutes=20))[0], abs=1e-4
    )


def test_reconciliation_pulls_the_quantity_to_the_pack_content_never_the_basis():
    config = battery(capacity_kwh=4.0)
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=60)])
    state = {"basis_eur_per_kwh": 0.20, "energy_kwh": 1.4, "last_ts": to_iso(T0)}
    now = T0 + timedelta(minutes=20)
    series = ledger.Series(
        power=points(T0, 5, 600.0), grid=points(T0, 5, 600.0), soc=points(T0, 5, 60.0)
    )
    payload = book([dispatch_row(1, T0, 600.0)], series, timeline, config, now=now, state=state)
    # E_phys = 4 kWh * (60 - 10) / 100 = 2.0 kWh; the measured booking was 1.4 + 0.2 = 1.6.
    assert payload["energy_kwh"] == pytest.approx(2.0)
    assert payload["basis_eur_per_kwh"] == pytest.approx(0.20)  # never touched
    assert payload["reconciled_kwh"] == pytest.approx(0.4)
    assert payload["reconciled_last_kwh"] == pytest.approx(0.4)
    assert payload["reconciled_resets"] == 0
    # A replay at the same end applies nothing twice; the value follows basis * E.
    again = book([dispatch_row(1, T0, 600.0)], series, timeline, config, now=now, state=payload)
    assert again["energy_kwh"] == pytest.approx(2.0)
    assert again["reconciled_kwh"] == pytest.approx(0.4)
    status = ledger.basis_status(payload, config)
    assert status["stored_value_eur"] == pytest.approx(0.40)


def test_reconciliation_is_capped_and_resets_beyond_half_a_pack():
    # The clamp: a glitched SoC above 100 % cannot push more than capacity / eta_charge in.
    config = battery(capacity_kwh=1.0, min_soc_pct=0.0)
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=60)])
    state = {"basis_eur_per_kwh": 0.20, "energy_kwh": 0.5, "last_ts": to_iso(T0)}
    capped = book(
        [dispatch_row(1, T0, 300.0)],
        ledger.Series(
            power=points(T0, 13, 300.0), grid=points(T0, 13, 300.0), soc=points(T0, 13, 120.0)
        ),
        timeline,
        config,
        now=T0 + timedelta(hours=1),
        state=state,
    )
    # E_phys would be 1.2 kWh but max_energy is 1.0: the correction is 1.0 - 0.8 = 0.2, not 0.4.
    assert capped["energy_kwh"] == pytest.approx(1.0)
    assert capped["reconciled_last_kwh"] == pytest.approx(0.2)
    assert capped["basis_eur_per_kwh"] == pytest.approx(0.20)
    assert capped["reconciled_resets"] == 0
    # Beyond half a pack in one step the ledger is unattributable: reset and count it.
    reset = book(
        [dispatch_row(1, T0, 100.0)],
        ledger.Series(
            power=points(T0, 5, 100.0), grid=points(T0, 5, 100.0), soc=points(T0, 5, 90.0)
        ),
        timeline,
        config,
        now=T0 + timedelta(minutes=20),
        state={"basis_eur_per_kwh": 0.20, "energy_kwh": 0.1, "last_ts": to_iso(T0)},
    )
    assert reset["basis_eur_per_kwh"] is None
    assert reset["energy_kwh"] == 0.0
    assert reset["reconciled_resets"] == 1
    assert reset["reconciled_kwh"] == 0.0


def test_stale_or_missing_soc_skips_reconciliation_and_counts_it():
    config = battery(capacity_kwh=4.0)
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=60)])
    state = {"basis_eur_per_kwh": 0.20, "energy_kwh": 1.0, "last_ts": to_iso(T0)}
    now = T0 + timedelta(minutes=30)
    # The last SoC sample is 20 minutes old: past the 10-minute bound, so it is not used.
    stale = ledger.Series(
        power=points(T0, 7, 600.0), grid=points(T0, 7, 600.0), soc=points(T0, 3, 60.0)
    )
    payload = book([dispatch_row(1, T0, 600.0)], stale, timeline, config, now=now, state=state)
    assert payload["energy_kwh"] == pytest.approx(1.0 + 600.0 * 30 / 60 / 1000.0)
    assert payload["skipped_no_soc"] == 1
    assert payload["reconciled_kwh"] == 0.0
    assert payload["reconciled_last_kwh"] is None
    # No SoC series at all: still skipped and counted, never a fabricated correction.
    without = ledger.Series(power=points(T0, 7, 600.0), grid=points(T0, 7, 600.0))
    payload2 = book([dispatch_row(1, T0, 600.0)], without, timeline, config, now=now, state=state)
    assert payload2["energy_kwh"] == pytest.approx(1.3)
    assert payload2["skipped_no_soc"] == 1
    assert payload2["reconciled_resets"] == 0


def test_basis_status_carries_the_reconciliation_counters():
    payload = {
        "basis_eur_per_kwh": 0.2,
        "energy_kwh": 1.0,
        "reconciled_kwh": -0.05,
        "reconciled_last_kwh": 0.02,
        "reconciled_resets": 2,
        "skipped_no_soc": 3,
    }
    status = ledger.basis_status(payload)
    assert status["reconciled_kwh"] == pytest.approx(-0.05)
    assert status["reconciled_last_kwh"] == pytest.approx(0.02)
    assert status["reconciled_resets"] == 2
    assert status["skipped_no_soc"] == 3
    # No ledger yet: the counters are present and null/zero, never invented.
    empty = ledger.basis_status(None)
    assert empty["reconciled_kwh"] == 0.0
    assert empty["reconciled_last_kwh"] is None
    assert empty["reconciled_resets"] == 0
    assert empty["skipped_no_soc"] == 0


# ------------------------------------------- a stalled ledger is never silent (0.20.3)


def test_stalled_ledger_counts_marks_and_warns_once_then_recovers(tmp_path, caplog):
    """The live 0.20.2 defect: no power rows froze the ledger with no trace at all."""
    store = Store(tmp_path / "stall.db")
    config = battery()
    _seed_dispatch_and_plan(store, config, start=T0)
    seeded_soc = soc_for(0.8, config)
    store.add_measurements(
        [
            Measurement(
                ts=T0 + timedelta(minutes=5 * index),
                role=f"battery_soc:{config.id}",
                value=seeded_soc,
                unit="%",
            )
            for index in range(13)
        ]
    )
    # The frozen live ledger: 282 intervals, basis 0.3197, last booked 18 h before the incident.
    store.profile_put(
        ledger.PROFILE_KIND,
        config.id,
        {
            "basis_eur_per_kwh": 0.3197,
            "energy_kwh": 1.0,
            "last_ts": to_iso(T0),
            "intervals": 282,
        },
        T0,
    )
    timeline = ledger.PriceTimeline([slot(T0, 0.20, 0.05, minutes=120)])
    now = T0 + timedelta(hours=1)
    with caplog.at_level("WARNING", logger="app.cost_basis"):
        first = ledger.update_basis(store, config, now, timeline=timeline)
        second = ledger.update_basis(store, config, now, timeline=timeline)
    assert first is not None and second is not None
    # Freezing: the basis is left exactly as it was, never re-guessed from nothing.
    assert second["basis_eur_per_kwh"] == pytest.approx(0.3197)
    assert second["energy_kwh"] == pytest.approx(1.0)
    assert second["intervals"] == 282
    # Visible: one counter step per update, a persisted marker and exactly one warning.
    assert second["skipped_no_samples"] == 2
    assert second["stale"] is True
    assert second["no_samples_since"] == to_iso(T0)
    warnings = [record for record in caplog.records if "stalled" in record.getMessage()]
    assert len(warnings) == 1
    stored = ledger.load_basis(store, config.id)
    assert stored is not None and stored["stale"] is True
    assert stored["skipped_no_samples"] == 2

    # The next successful booking clears the marker.
    store.add_measurements(
        [
            Measurement(
                ts=T0 + timedelta(minutes=5 * index),
                role=f"battery_power:{config.id}",
                value=800.0,
                unit="W",
            )
            for index in range(13)
        ]
    )
    third = ledger.update_basis(store, config, now, timeline=timeline)
    assert third is not None
    assert third["stale"] is False
    assert third["no_samples_since"] is None
    assert third["intervals"] == 283
    assert third["skipped_no_samples"] == 2  # the counter is cumulative
    store.close()


def test_basis_status_exposes_the_stall_marker():
    status = ledger.basis_status(
        {"basis_eur_per_kwh": 0.3197, "energy_kwh": 1.0, "no_samples_since": to_iso(T0),
         "skipped_no_samples": 9}
    )
    assert status["stale"] is True
    assert status["no_samples_since"] == to_iso(T0)
    assert status["skipped_no_samples"] == 9
    assert status["sample_source"] is None
    empty = ledger.basis_status(None)
    assert empty["stale"] is False and empty["no_samples_since"] is None
    assert empty["skipped_no_samples"] == 0


# ------------------------------------------- recovering unbooked measured energy (0.20.3)


def test_single_battery_actuals_fallback_recovers_the_unbooked_window(tmp_path):
    """With one battery the actuals row is the same measurement, so it can recover the window."""
    config = battery()
    now = T0 + timedelta(hours=1)
    measured_store = Store(tmp_path / "measured.db")
    _seed_store(measured_store, config, start=T0)
    measured = ledger.update_basis(measured_store, config, now)

    fallback_store = Store(tmp_path / "fallback.db")
    _seed_dispatch_and_plan(fallback_store, config, start=T0)
    seeded_soc = soc_for(0.8, config)
    for index in range(13):
        fallback_store.upsert_actuals(
            T0 + timedelta(minutes=5 * index),
            battery_kw=0.8,
            grid_kw=0.8,
            soc_kwh=seeded_soc / 100.0 * config.capacity_kwh,
        )
    recovered = ledger.update_basis(fallback_store, config, now)

    assert measured["sample_source"] == "measurements"
    assert recovered["sample_source"] == "actuals"
    assert recovered["stale"] is False
    # The recovered booking is the same arithmetic as the measurement-role booking: the measured
    # integral, the plan's buy/sell attribution, and the pack reconciliation.
    assert recovered["basis_eur_per_kwh"] == pytest.approx(measured["basis_eur_per_kwh"])
    assert recovered["energy_kwh"] == pytest.approx(measured["energy_kwh"])
    assert recovered["intervals"] == measured["intervals"]
    assert recovered["journal"][-1]["cost_eur"] == pytest.approx(
        measured["journal"][-1]["cost_eur"]
    )
    assert recovered["journal"][-1]["grid_kwh"] > 0.0  # attributed through the measured meter
    assert ledger.basis_status(recovered, config)["sell_floor_eur_per_kwh"] == pytest.approx(
        ledger.basis_status(measured, config)["sell_floor_eur_per_kwh"]
    )
    measured_store.close()
    fallback_store.close()


def test_actuals_fallback_never_splits_the_fleet_aggregate(tmp_path):
    """Two enabled batteries: the aggregate cannot be split, so the stall is reported instead."""
    config = battery()
    store = Store(tmp_path / "two-batteries.db")
    _seed_dispatch_and_plan(store, config, start=T0)
    seeded_soc = soc_for(0.8, config)
    for index in range(13):
        store.upsert_actuals(
            T0 + timedelta(minutes=5 * index),
            battery_kw=1.6,  # both packs charging: the fleet aggregate, not one battery
            grid_kw=1.6,
            soc_kwh=2.0 * seeded_soc / 100.0 * config.capacity_kwh,
        )
    payload = ledger.update_basis(store, config, T0 + timedelta(hours=1), fleet_enabled=2)
    assert payload is not None
    assert payload["sample_source"] == "measurements"
    assert payload["stale"] is True
    assert payload["skipped_no_samples"] == 1
    assert payload["no_samples_since"] == to_iso(T0)
    assert payload["basis_eur_per_kwh"] is None  # never a guessed split
    assert payload["intervals"] == 0
    store.close()


def test_engine_recovers_the_ledger_and_reports_a_stall_without_a_guessed_split(tmp_path):
    """The engine passes the enabled-fleet size, so the fallback stays single-battery only."""
    config = battery()
    single = Store(tmp_path / "engine-single.db")
    engine, _store, _settings, clock = _build_engine(store=single, config=config)
    _seed_dispatch_and_plan(single, config, start=T0)
    seeded_soc = soc_for(0.8, config)
    for index in range(13):
        single.upsert_actuals(
            T0 + timedelta(minutes=5 * index),
            battery_kw=0.8,
            grid_kw=0.8,
            soc_kwh=seeded_soc / 100.0 * config.capacity_kwh,
        )
    clock.now = T0 + timedelta(hours=1)
    engine.update_cost_bases(clock.now, force=True)
    row = engine.battery_cost_basis(config.id)
    assert row["sample_source"] == "actuals" and row["stale"] is False
    assert row["skipped_no_samples"] == 0
    assert engine.fleet_sell_floor() is not None
    single.close()

    fleet = Store(tmp_path / "engine-fleet.db")
    engine2, _store2, settings2, clock2 = _build_engine(store=fleet, config=config)
    settings2.add_battery(battery(id="example_battery_2", name="Example Battery 2"))
    _seed_dispatch_and_plan(fleet, config, start=T0)
    for index in range(13):
        fleet.upsert_actuals(
            T0 + timedelta(minutes=5 * index),
            battery_kw=1.6,
            grid_kw=1.6,
            soc_kwh=2.0 * seeded_soc / 100.0 * config.capacity_kwh,
        )
    clock2.now = T0 + timedelta(hours=1)
    engine2.update_cost_bases(clock2.now, force=True)
    row2 = engine2.battery_cost_basis(config.id)
    assert row2["stale"] is True and row2["skipped_no_samples"] == 1
    assert row2["cost_basis_eur_per_kwh"] is None
    assert row2["intervals"] == 0
    fleet.close()


def test_engine_resolved_mappings_carry_registry_roles_for_the_sampler():
    """The sampler's mapping override is the driver-resolved one, registry roles included."""
    config = BatteryConfig(
        id="example_battery_1",
        name="Example Battery 1",
        driver="zendure",
        capacity_kwh=5.0,
        mapping={
            "manual_power_entity": "number.example_manual_power",
            "operation_entity": "select.example_operation",
        },
    )
    settings = _selfcheck_settings()
    settings["control.grid_follow"] = False
    settings.add_battery(config)
    entries = [
        {
            "platform": "zendure_ha",
            "translation_key": key,
            "entity_id": entity,
            "device_id": "example_device",
        }
        for key, entity in (
            ("output_home_power", "sensor.example_output_home_power"),
            ("grid_input_power", "sensor.example_grid_input_power"),
            ("electric_level", "sensor.example_electric_level"),
        )
    ]
    engine = Engine(
        settings,
        store=_FakeStore(),
        ha=_FakeHA(),
        notifier=_FakeNotifier(),
        clock=_Clock(),
        **{**_selfcheck_sources(), "registry_source": lambda: entries},
    )
    mappings = engine.resolved_mappings()
    resolved = mappings[config.id]
    assert resolved["output_power_entity"] == "sensor.example_output_home_power"
    assert resolved["grid_input_power_entity"] == "sensor.example_grid_input_power"
    assert resolved["soc_entity"] == "sensor.example_electric_level"
    # ...and the sampler reads those roles even though the stored mapping never names them.
    roles = role_entities(settings, [config], mappings=mappings)
    assert roles["battery_charge:example_battery_1"] == "sensor.example_grid_input_power"
    assert roles["battery_discharge:example_battery_1"] == "sensor.example_output_home_power"
    assert roles["battery_soc:example_battery_1"] == "sensor.example_electric_level"
