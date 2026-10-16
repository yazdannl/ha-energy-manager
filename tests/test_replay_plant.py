"""The replay plant: recorded fidelity, simulated physics, and a real engine tick (section 2)."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "energy_manager"))

from app.control.drivers.base import BatteryDriver
from app.control.safety import RAMP_LIMIT_W
from app.engine import Engine
from app.main import EXAMPLE_BATTERY, _FakeHA, _selfcheck_settings, _selfcheck_sources
from app.models import (
    Action,
    BatteryConfig,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
    quarter_floor,
    to_iso,
)
from app.replay.episode import open_episode
from app.replay.plant import (
    MODES,
    PlantDriver,
    RECORDED,
    SIMULATED,
    max_export_kw,
    plant_factory,
)
from app.store import Store

START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
BATTERY = EXAMPLE_BATTERY


def recording(
    tmp_path: Path,
    *,
    name: str = "recording.sqlite",
    settings_extra: dict | None = None,
    batteries: list[BatteryConfig] | None = None,
    soc_start: float = 55.0,
    soc_step: float = 1.0,
    power_start: float = 0.0,
    power_step: float = 100.0,
    load_kw: float = 1.2,
    pv_kw: float = 1.0,
) -> Path:
    """A recording whose per-battery samples move, so "read the recording" is observable."""
    fleet = list(batteries) if batteries is not None else [BATTERY]
    values = _selfcheck_settings().all()
    values.update(
        {
            "mode": "auto",
            "dry_run": False,
            "control.grid_follow": False,
            "batteries": [asdict(battery) for battery in fleet],
        }
    )
    values.update(settings_extra or {})
    path = tmp_path / name
    store = Store(path)
    try:
        store.set_settings(values, now=START - timedelta(hours=2))
        measurements = []
        moment = quarter_floor(START, 5)
        index = 0
        while moment < END:
            store.upsert_actuals(
                to_iso(moment),
                pv_kw=pv_kw,
                load_kw=load_kw,
                grid_kw=load_kw - pv_kw,
                battery_kw=0.0,
                soc_kwh=2.75,
                price_buy=0.25,
                price_sell=0.05,
            )
            for battery in fleet:
                measurements.append(
                    Measurement(
                        ts=moment,
                        role=f"battery_soc:{battery.id}",
                        value=soc_start + soc_step * index,
                        unit="%",
                    )
                )
                measurements.append(
                    Measurement(
                        ts=moment,
                        role=f"battery_power:{battery.id}",
                        value=power_start + power_step * index,
                        unit="W",
                    )
                )
            moment += timedelta(minutes=5)
            index += 1
        store.add_measurements(measurements)
        store.save_plan(
            Plan(
                generated_at=START - timedelta(minutes=30),
                horizon_start=START,
                horizon_end=END,
                slot_minutes=15,
                summary=PlanSummary(),
                batteries=[{"id": BATTERY.id, "share_w": 0}],
                slots=[
                    PlanSlot(
                        t=START + timedelta(minutes=15 * slot),
                        buy=0.25,
                        sell=0.05,
                        pv_kw=1.0,
                        load_kw=1.2,
                        soc_kwh=2.75,
                        power_w=0.0,
                        action=Action.HOLD,
                        reason="recorded",
                    )
                    for slot in range(4)
                ],
                mode="auto",
                dry_run=False,
            )
        )
    finally:
        store.close()
    return path


def test_the_recorded_plant_reproduces_the_recorded_samples(tmp_path: Path) -> None:
    episode = open_episode(recording(tmp_path), START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], RECORDED)
        assert isinstance(driver, BatteryDriver)
        assert driver.id() == BATTERY.id
        state = driver.read_state()
        assert state.available is True
        assert state.soc_pct == pytest.approx(55.0)
        assert state.power_w == pytest.approx(0.0)
        assert state.energy_kwh == pytest.approx(2.75)
        assert state.power_measured is True
        episode.advance_to(START + timedelta(minutes=10))
        state = driver.read_state()
        assert state.soc_pct == pytest.approx(57.0)
        assert state.power_w == pytest.approx(200.0)
        result = driver.apply(700.0, dry_run=False)
        assert result.ok is True
        assert result.requested_w == pytest.approx(700.0)
        assert result.applied_w == pytest.approx(700.0)
        assert driver.commands[-1][1] == pytest.approx(700.0)
        assert driver.commanded_power() == pytest.approx(700.0)
        # The recorded arm holds the trajectory fixed: a command never moves the samples.
        assert driver.read_state().soc_pct == pytest.approx(57.0)
        # Recorded mode reports the recording's own site measurement: rebuilding ``base + battery``
        # would derive a number the recording already contains (this fixture deliberately writes
        # ``grid_kw = load - pv``, so the two disagree and the measurement is the honest answer).
        assert driver.grid_kw() == pytest.approx(1.2 - 1.0)
        assert driver.fleet.grid_reconstruction_kw() == pytest.approx(1.2 - 1.0 + 0.2)
    finally:
        episode.close()


def test_the_simulated_plant_integrates_charging_with_efficiency(tmp_path: Path) -> None:
    episode = open_episode(recording(tmp_path), START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert isinstance(driver, BatteryDriver)
        result = driver.apply(1000.0, dry_run=False)
        assert result.applied_w == pytest.approx(1000.0)
        episode.advance_to(START + timedelta(minutes=30))
        state = driver.read_state()
        # 0.5 h at 1 kW AC, eta_charge 0.95: dp.py's own ``e += dt*p*eta_charge``.
        assert state.energy_kwh == pytest.approx(2.75 + 0.5 * 0.95)
        assert state.power_w == pytest.approx(1000.0)
        assert state.soc_pct == pytest.approx(state.energy_kwh / 5.0 * 100.0)
    finally:
        episode.close()


def test_the_simulated_plant_integrates_discharging_with_efficiency(tmp_path: Path) -> None:
    episode = open_episode(recording(tmp_path), START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert driver.apply(-1000.0, dry_run=False).applied_w == pytest.approx(-1000.0)
        episode.advance_to(START + timedelta(minutes=30))
        state = driver.read_state()
        assert state.energy_kwh == pytest.approx(2.75 - 0.5 / 0.95)
        assert state.soc_pct == pytest.approx(state.energy_kwh / 5.0 * 100.0)
    finally:
        episode.close()


def test_the_simulated_plant_ramps_between_commands(tmp_path: Path) -> None:
    episode = open_episode(recording(tmp_path), START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert driver.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        # One tick later the command cannot fall further than RAMP_LIMIT_W (1000 W).
        assert driver.apply(-1000.0, dry_run=False).applied_w == pytest.approx(0.0)
    finally:
        episode.close()


def test_the_simulated_plant_clips_at_the_power_limit(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        settings_extra={"batteries": [{**asdict(BATTERY), "max_charge_w": 2500.0}]},
        power_start=2000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        # The ramp from the recorded 2000 W would allow 3000 W; the pack's own limit is 2500 W.
        assert driver.apply(5000.0, dry_run=False).applied_w == pytest.approx(2500.0)
    finally:
        episode.close()


def test_the_simulated_plant_stops_at_the_soc_limits(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        settings_extra={"batteries": [{**asdict(BATTERY), "capacity_kwh": 1.0}]},
        soc_start=90.0,
        soc_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        charger = PlantDriver(episode, episode.batteries[0], SIMULATED)
        discharger = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert charger.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        assert discharger.apply(-1000.0, dry_run=False).applied_w == pytest.approx(-1000.0)
        episode.advance_to(END)
        state = charger.read_state()
        assert state.energy_kwh == pytest.approx(1.0)  # 0.9 kWh + a full hour, clipped at the ceiling
        assert state.soc_pct == pytest.approx(100.0)
        # The ceiling was hit inside the interval, so the flow *now* is zero -- the average of the
        # interval (0.1/0.95 kW) would be flow the pack cannot be delivering any more.
        assert state.power_w == pytest.approx(0.0)
        assert charger.realized_power() == pytest.approx(0.0)
        assert charger.grid_kw() == pytest.approx(0.2)
        # What really moved is an accounting quantity, kept separately from the power.
        assert charger.moved_kwh == pytest.approx(0.1)
        state = discharger.read_state()
        assert state.energy_kwh == pytest.approx(0.1)  # 10 % floor of 1 kWh
        assert state.soc_pct == pytest.approx(10.0)
        assert state.power_w == pytest.approx(0.0)  # at the floor: discharging has stopped
        assert discharger.moved_kwh == pytest.approx(-(0.9 - 0.1))
    finally:
        episode.close()


def test_the_settled_power_is_the_boundary_flow_not_the_interval_average(tmp_path: Path) -> None:
    """0.1 kWh of headroom charged at 1 kW for an hour: 0 W now, 0.1 kWh moved."""
    path = recording(
        tmp_path,
        settings_extra={"batteries": [{**asdict(BATTERY), "capacity_kwh": 1.0}]},
        soc_start=90.0,
        soc_step=0.0,
        power_start=1000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert driver.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        episode.advance_to(END)
        # The pack filled up mid-interval: its flow at this instant is zero, not the interval average
        # (0.1/0.95 kW) which would also poison the next ramp reference.
        assert driver.realized_power() == pytest.approx(0.0)
        assert driver.read_state().power_w == pytest.approx(0.0)
        assert driver.grid_kw() == pytest.approx(0.2)
        # The accounting quantity is the energy that actually moved, and it matches the SoC change.
        assert driver.moved_kwh == pytest.approx(0.1)
        state = driver.read_state()
        assert state.energy_kwh is not None
        assert state.energy_kwh - 0.9 == pytest.approx(driver.moved_kwh)
    finally:
        episode.close()


def test_the_fleet_settles_every_member_before_aggregating(tmp_path: Path) -> None:
    """The site flow must not depend on which driver a caller read first."""
    second = replace(BATTERY, id="example_battery_2", name="Example Battery 2")
    path = recording(
        tmp_path,
        batteries=[replace(BATTERY, capacity_kwh=1.0), replace(second, capacity_kwh=1.0)],
        soc_start=90.0,
        soc_step=0.0,
        power_start=1000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        factory = plant_factory(episode, SIMULATED)
        first = factory(episode.batteries[0])
        other = factory(episode.batteries[1])
        assert first.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        assert other.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        episode.advance_to(END)
        # Neither driver has been read since the advance, and both are full: the fleet must settle
        # them itself, so the site flow reflects the packs, not their stale commands.
        assert first.grid_kw() == pytest.approx(0.2)
        assert other.grid_kw() == pytest.approx(0.2)
        assert first.realized_power() == pytest.approx(0.0)
        assert other.realized_power() == pytest.approx(0.0)
        assert first.moved_kwh == pytest.approx(0.1)
        assert other.moved_kwh == pytest.approx(0.1)
        # Reading one member by hand before asking for the site flow changes nothing either.
        other.read_state()
        assert other.grid_kw() == pytest.approx(0.2)
        assert first.realized_power() == pytest.approx(0.0)
    finally:
        episode.close()


def test_a_full_pack_never_gets_a_charging_window(tmp_path: Path) -> None:
    """A full pack whose seeded power is above the ramp: the ramp may not open a charging floor."""
    path = recording(
        tmp_path,
        settings_extra={
            "batteries": [{**asdict(BATTERY), "capacity_kwh": 1.0, "max_charge_w": 2500.0}]
        },
        soc_start=100.0,
        soc_step=0.0,
        power_start=2000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        low, high, _, _ = driver._window()
        assert low <= high  # never an inverted interval to clamp into
        result = driver.apply(-500.0, dry_run=False)
        assert result.applied_w <= 0.0  # a full pack cannot be charged, whatever the ramp says
        assert result.applied_w <= float(driver.config.max_charge_w)
        assert result.applied_w >= -float(driver.config.max_discharge_w)
        assert driver.realized_power() <= 0.0
    finally:
        episode.close()


def test_a_pack_that_may_not_charge_never_gets_a_charging_window(tmp_path: Path) -> None:
    """The physical window caps charging at zero, so a positive ramp floor must clip into it."""
    path = recording(
        tmp_path,
        settings_extra={"batteries": [{**asdict(BATTERY), "max_charge_w": 0.0}]},
        power_start=2000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        low, high, _, _ = driver._window()
        assert low <= high  # the ramp cannot open a floor above the actuator's own ceiling
        assert high <= 0.0
        result = driver.apply(-500.0, dry_run=False)
        assert result.applied_w <= float(driver.config.max_charge_w)
        assert result.applied_w >= -float(driver.config.max_discharge_w)
        assert driver.realized_power() <= 0.0
    finally:
        episode.close()


def test_a_pack_at_its_reserve_never_gets_a_discharging_window(tmp_path: Path) -> None:
    """The mirror image: the SoC gate may not be undercut by a negative ramp-derived ceiling."""
    path = recording(
        tmp_path,
        settings_extra={
            "batteries": [{**asdict(BATTERY), "capacity_kwh": 1.0, "max_discharge_w": 2500.0}]
        },
        soc_start=10.0,  # exactly the 10 % reserve: 0.1 kWh
        soc_step=0.0,
        power_start=-2000.0,
        power_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        low, high, _, _ = driver._window()
        assert low <= high
        assert low >= 0.0  # at the reserve: charge only
        assert driver.apply(0.0, dry_run=False).applied_w >= 0.0  # a hold never discharges
        assert driver.realized_power() >= 0.0
    finally:
        episode.close()


def test_a_pack_below_its_reserve_is_never_lifted_to_it(tmp_path: Path) -> None:
    path = recording(
        tmp_path,
        settings_extra={"batteries": [{**asdict(BATTERY), "capacity_kwh": 1.0}]},
        soc_start=5.0,  # 0.05 kWh, below the 10 % (0.1 kWh) reserve
        soc_step=0.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert driver.read_state().energy_kwh == pytest.approx(0.05)
        assert driver.apply(-1000.0, dry_run=False).applied_w == pytest.approx(0.0)
        episode.advance_to(END)
        state = driver.read_state()
        assert state.energy_kwh == pytest.approx(0.05)  # kept, never created
        assert state.power_w == pytest.approx(0.0)
    finally:
        episode.close()


def test_the_export_bound_never_invents_charging_capacity(tmp_path: Path) -> None:
    # The site exports 10 kW (load - pv = -10) with a 0.5 kW export bound, so honouring the bound
    # would need 9.5 kW of charge -- far beyond this pack's 3 kW limit.
    path = recording(
        tmp_path,
        settings_extra={
            "optimizer.max_grid_export_w": 500.0,
            "batteries": [{**asdict(BATTERY), "max_charge_w": 3000.0}],
        },
        power_start=3000.0,
        power_step=0.0,
        load_kw=0.0,
        pv_kw=10.0,
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        result = driver.apply(9000.0, dry_run=False)
        assert result.applied_w == pytest.approx(3000.0)  # the charge limit, not 9.5 kW
        assert driver.last_infeasible is True
        assert driver.capabilities()["last_infeasible"] is True
        assert driver.realized_power() == pytest.approx(3000.0)
    finally:
        episode.close()


def test_two_packs_share_the_site_export_bound_and_one_grid_flow(tmp_path: Path) -> None:
    second = replace(BATTERY, id="example_battery_2", name="Example Battery 2")
    path = recording(
        tmp_path,
        batteries=[BATTERY, second],
        settings_extra={"optimizer.max_grid_export_w": 500.0},
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        factory = plant_factory(episode, SIMULATED)
        first = factory(episode.batteries[0])
        other = factory(episode.batteries[1])
        # base = +0.2 kW and a 0.5 kW bound: the site may discharge at most 0.7 kW in total.
        assert first.apply(-1000.0, dry_run=False).applied_w == pytest.approx(-700.0)
        assert other.apply(-1000.0, dry_run=False).applied_w == pytest.approx(0.0)  # budget spent
        assert first.last_infeasible is False and other.last_infeasible is False
        assert first.realized_power() + other.realized_power() == pytest.approx(-700.0)
        # Each driver reports the site's flow, including the other pack's power.
        assert first.grid_kw() == pytest.approx(-0.5)
        assert other.grid_kw() == pytest.approx(-0.5)
        assert first.capabilities()["fleet_size"] == 2
    finally:
        episode.close()


def test_the_simulated_plant_respects_the_planners_export_bound(tmp_path: Path) -> None:
    path = recording(tmp_path, settings_extra={"optimizer.max_grid_export_w": 500.0})
    episode = open_episode(path, START, END, 60.0)
    try:
        assert max_export_kw(episode.settings) == pytest.approx(0.5)
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        # load - pv = 0.2 kW, so a discharge may only reach -(0.5 + 0.2) kW = -700 W.
        assert driver.apply(-1000.0, dry_run=False).applied_w == pytest.approx(-700.0)
        assert driver.grid_kw() == pytest.approx(-0.5)
        assert driver.last_infeasible is False
    finally:
        episode.close()


@pytest.mark.parametrize("mode", MODES)
def test_each_plant_mode_drives_a_real_engine_tick(tmp_path: Path, mode: str) -> None:
    path = recording(tmp_path, name=f"{mode}.sqlite")
    episode = open_episode(path, START, END, 60.0)
    try:
        engine = Engine(
            episode.settings,
            store=episode.store(),
            ha=_FakeHA(),
            clock=lambda: episode.now,
            driver_factory=plant_factory(episode, mode),
            **_selfcheck_sources(),
        )
        outcome = engine.tick_now(episode.now)
        driver = engine._drivers[BATTERY.id]
        assert isinstance(driver, PlantDriver)
        assert driver.mode == mode
        assert [result.battery_id for result in outcome.results] == [BATTERY.id]
        assert outcome.results[0].ok is True
        # The tick wrote its own dispatch rows into the scratch copy, not into the recording.
        assert episode.store().dispatch_log(limit=5)
    finally:
        episode.close()


def test_an_impossible_site_bound_collapses_to_the_near_boundary(tmp_path: Path) -> None:
    """No power can respect the site bound: the window collapses instead of staying open."""
    path = recording(
        tmp_path, load_kw=0.2, pv_kw=2.2, settings_extra={"optimizer.max_grid_export_w": 50.0}
    )
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        # base = -2.0 kW, so even full charging exports 1.0 kW against a 50 W bound: the request of
        # 0 W cannot be honoured, and the plant charges as much as it physically may rather than
        # pretending the impossible bound left a window open.
        assert driver.apply(0.0, dry_run=False).applied_w == pytest.approx(RAMP_LIMIT_W)
        assert driver.last_infeasible is True
    finally:
        episode.close()


def test_an_unknown_pack_flow_is_not_a_zero(tmp_path: Path) -> None:
    """A missing pack sample must never be summed as a zero site flow."""
    second = replace(BATTERY, id="example_battery_2", name="Example Battery 2")
    path = recording(
        tmp_path, batteries=[BATTERY, second], settings_extra={"optimizer.max_grid_export_w": 500.0}
    )
    conn = sqlite3.connect(path)  # a power row with no value, as a recording may carry
    try:
        conn.execute(
            "INSERT OR REPLACE INTO measurements (ts, entity_role, value, unit) VALUES (?, ?, NULL, ?)",
            (to_iso(START), f"battery_power:{second.id}", "W"),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # no frames for episode extraction to refuse
    finally:
        conn.close()

    episode = open_episode(path, START, END, 60.0)
    try:
        factory = plant_factory(episode, SIMULATED)
        first = factory(episode.batteries[0])
        factory(episode.batteries[1])
        fleet = first.fleet
        assert fleet.total_power_w() is None
        assert fleet.other_power_w(first) is None
        assert fleet.grid_kw() is None
        # The site bound cannot be allocated against an invented zero, so the pack keeps its own
        # limits for this tick instead of charging an export quantity nobody measured...
        result = first.apply(-1000.0, dry_run=False)
        assert result.applied_w == pytest.approx(-1000.0)
        # ...and the unchecked constraint is stated rather than reported as satisfied.
        assert first.last_bound_unverified is True
        assert first.capabilities()["last_bound_unverified"] is True
        assert "not evaluated" in result.message
        # Recorded mode still answers with the recording's own site measurement.
        recorded = plant_factory(episode, RECORDED)(episode.batteries[0])
        assert recorded.fleet.grid_kw() == pytest.approx(0.2)
        # It cannot evaluate the bound either, so it does not claim to have cleared it.
        assert recorded.apply(0.0, dry_run=False).message == (
            "site export bound not evaluated for this command"
        )
    finally:
        episode.close()


def test_an_unknown_flow_interval_invalidates_the_stored_energy(tmp_path: Path) -> None:
    """Time passing with no known flow must not freeze energy as if nothing had moved."""
    path = recording(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO measurements (ts, entity_role, value, unit) VALUES (?, ?, NULL, ?)",
            (to_iso(START), f"battery_power:{BATTERY.id}", "W"),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    episode = open_episode(path, START, END, 60.0)
    try:
        driver = PlantDriver(episode, episode.batteries[0], SIMULATED)
        assert driver.read_state().power_w is None
        episode.advance_to(START + timedelta(minutes=10))
        state = driver.read_state()
        # The pack integrated an interval whose flow nobody measured: its energy is not an
        # observation any more, so it reports itself unavailable instead of a stale SoC.
        assert driver.energy_unknown is True
        assert state.energy_kwh is None
        assert state.soc_pct is None
        assert state.available is False
        assert state.extra["energy_unknown"] is True
        # A later command moves nothing either: there is no energy to integrate from, and the flow it
        # supposedly started is not reported as measured.
        assert driver.apply(1000.0, dry_run=False).applied_w == pytest.approx(1000.0)
        assert driver.moved_kwh == pytest.approx(0.0)
        assert driver.commanded_power() == pytest.approx(1000.0)
        assert driver.realized_power() is None
        commanded = driver.read_state()
        assert commanded.power_w is None
        assert commanded.power_measured is False
        assert commanded.energy_kwh is None
        # The site flow cannot be summed from a pack whose flow is unknown, so it is refused.
        assert driver.fleet.total_power_w() is None
        assert driver.grid_kw() is None
    finally:
        episode.close()
