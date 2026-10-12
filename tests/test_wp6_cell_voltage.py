"""WP6 cell-voltage protection tests (SPEC 9.3 clause 4b, 0.16.0).

Covers the whole feature without hardware: the settings and their cross-key validation, the pure
per-battery guard (thresholds, hysteresis, latch, stale readings, per-battery mapping), its place in
the ordered safety table, the dispatcher's advisory/dry-run/write behaviour and dispatch-log reasons,
the engine latch/alerts/status surface, the API fields and the allocate helper.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.api import status_dict  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULTS,
    SETTINGS_META,
    ConfigError,
    Settings,
    coerce_setting,
    protection_patch_errors,
)
from app.control import drivers as registry  # noqa: E402
from app.control import safety  # noqa: E402
from app.control.dispatcher import Dispatcher  # noqa: E402
from app.models import (  # noqa: E402
    Action,
    BatteryConfig,
    BatteryState,
    DispatchResult,
    Mode,
    Plan,
    PlanSlot,
    Reason,
    SystemState,
)
from app.optimize.allocate import allocate_power  # noqa: E402
from fake_ha import FakeHAFacade, FakeHAState  # noqa: E402

NOW = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)


class DictSettings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def protection_settings(**overrides) -> DictSettings:
    values = {
        "protection.cell_voltage_enabled": True,
        "protection.cell_voltage_warning_v": 3.10,
        "protection.cell_voltage_cutoff_v": 3.00,
        "protection.cell_voltage_resume_v": 3.18,
        "protection.emergency_charge_power_w": 0.0,
        "control.tick_seconds": 30,
        "control.stale_state_seconds": 180,
        "optimizer.reserve_soc_pct": 0.0,
        "optimizer.max_grid_import_w": 0,
        "optimizer.max_grid_export_w": 0,
    }
    values.update(overrides)
    return DictSettings(**values)


def battery(battery_id: str = "example_battery_1", **overrides) -> BatteryConfig:
    data = dict(
        id=battery_id,
        name=f"Example {battery_id}",
        driver="generic",
        capacity_kwh=5.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=1000.0,
        max_discharge_w=1000.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        mapping={"power_setpoint_entity": "number.example_power"},
    )
    data.update(overrides)
    return BatteryConfig(**data)


def live(
    voltage: float | None = None,
    *,
    battery_id: str = "example_battery_1",
    age_s: float = 5.0,
    available: bool = True,
    stale: bool = False,
    soc_pct: float = 50.0,
) -> BatteryState:
    state = BatteryState(
        id=battery_id,
        available=available,
        soc_pct=soc_pct,
        power_w=0.0,
        capacity_kwh=5.0,
        energy_kwh=5.0 * soc_pct / 100.0,
        read_at=NOW - timedelta(seconds=age_s),
        stale=stale,
    )
    if voltage is not None:
        state.extra["cell_voltage_v"] = voltage
    return state


# ---------------------------------------------------------------------------------------------
# 1. Settings
# ---------------------------------------------------------------------------------------------


def test_cell_voltage_settings_ship_disabled_with_safe_defaults():
    assert DEFAULTS["protection.cell_voltage_enabled"] is False
    assert DEFAULTS["protection.cell_voltage_warning_v"] == 3.10
    assert DEFAULTS["protection.cell_voltage_cutoff_v"] == 3.00
    assert DEFAULTS["protection.cell_voltage_resume_v"] == 3.18
    assert DEFAULTS["protection.emergency_charge_power_w"] == 0.0
    for key in (
        "protection.cell_voltage_enabled",
        "protection.cell_voltage_warning_v",
        "protection.cell_voltage_cutoff_v",
        "protection.cell_voltage_resume_v",
        "protection.emergency_charge_power_w",
    ):
        assert key in SETTINGS_META


def test_cell_voltage_settings_round_trip_and_range_validate(tmp_path):
    store_path = str(tmp_path)
    from app.main import build_settings, build_store

    store = build_store(store_path)
    try:
        settings = build_settings(store, {}, str(tmp_path / "options.json"))
        settings.set("protection.cell_voltage_enabled", True)
        settings.set("protection.cell_voltage_cutoff_v", 2.95)
        settings.set("protection.cell_voltage_warning_v", 3.05)
        settings.set("protection.cell_voltage_resume_v", 3.20)
        settings.set("protection.emergency_charge_power_w", 700)
        assert settings.get("protection.cell_voltage_enabled") is True
        assert settings.get("protection.emergency_charge_power_w") == 700.0
    finally:
        store.close()
    with pytest.raises(ConfigError):
        coerce_setting("protection.cell_voltage_cutoff_v", -0.1)
    with pytest.raises(ConfigError):
        coerce_setting("protection.cell_voltage_warning_v", "not a number")
    with pytest.raises(ConfigError):
        coerce_setting("protection.emergency_charge_power_w", -5)


def test_settings_reject_warning_or_resume_at_or_below_cutoff(tmp_path):
    from app.main import build_settings, build_store

    store = build_store(str(tmp_path))
    try:
        settings = build_settings(store, {}, str(tmp_path / "options.json"))
        with pytest.raises(ConfigError):
            settings.set("protection.cell_voltage_cutoff_v", 3.10)  # warning 3.10 <= cutoff
        with pytest.raises(ConfigError):
            settings.set("protection.cell_voltage_cutoff_v", 3.50)  # both warning and resume below
        with pytest.raises(ConfigError):
            settings.set("protection.cell_voltage_resume_v", 2.99)
        # the invalid ordering is rejected even while the guard is disabled
        assert settings.get("protection.cell_voltage_enabled") is False
        assert settings.get("protection.cell_voltage_cutoff_v") == 3.00
    finally:
        store.close()


def test_whole_patch_can_move_cutoff_and_warning_together(tmp_path):
    """A save that fixes both keys must be judged on its final state, not a half-applied one."""
    from app.main import build_settings, build_store

    store = build_store(str(tmp_path))
    try:
        settings = build_settings(store, {}, str(tmp_path / "options.json"))
        applied, errors = settings.update(
            {
                "protection.cell_voltage_cutoff_v": 3.20,
                "protection.cell_voltage_warning_v": 3.30,
                "protection.cell_voltage_resume_v": 3.35,
            }
        )
        assert sorted(applied) == [
            "protection.cell_voltage_cutoff_v",
            "protection.cell_voltage_resume_v",
            "protection.cell_voltage_warning_v",
        ]
        assert errors == []
        # and an invalid pair in one patch is rejected as a whole, leaving the stored values alone
        applied, errors = settings.update(
            {
                "protection.cell_voltage_cutoff_v": 3.40,
                "protection.cell_voltage_warning_v": 3.30,
            }
        )
        assert applied == []
        assert {row["key"] for row in errors} == {
            "protection.cell_voltage_cutoff_v",
            "protection.cell_voltage_warning_v",
        }
        assert settings.get("protection.cell_voltage_cutoff_v") == 3.20
    finally:
        store.close()


def test_protection_patch_errors_judges_the_final_state():
    current = {key: DEFAULTS[key] for key in DEFAULTS if key.startswith("protection.")}
    assert protection_patch_errors({"protection.cell_voltage_cutoff_v": 3.20}, current)
    assert protection_patch_errors(
        {
            "protection.cell_voltage_cutoff_v": 3.20,
            "protection.cell_voltage_warning_v": 3.30,
            "protection.cell_voltage_resume_v": 3.40,
        },
        current,
    ) == {}


# ---------------------------------------------------------------------------------------------
# 2. The pure guard
# ---------------------------------------------------------------------------------------------


def guard_reading(result, battery_id: str = "example_battery_1"):
    reading = result.reading(battery_id)
    assert reading is not None
    return reading


def test_guard_is_inactive_when_disabled():
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(2.50)}, protection_settings(
            **{"protection.cell_voltage_enabled": False}
        ), NOW
    )
    assert result.enabled is False
    assert result.active is False
    assert result.readings == ()


def test_below_cutoff_latches_and_commands_the_emergency_charge():
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(2.90)}, protection_settings(), NOW
    )
    reading = guard_reading(result)
    assert reading.fresh and reading.seen
    assert reading.latched and reading.block_discharge and reading.force
    assert reading.force_charge_w == 1000.0  # 0 = the battery's own max_charge_w
    assert reading.reason == Reason.CELL_VOLTAGE_EMERGENCY_CHARGE
    assert result.latch_state()["example_battery_1"] == {
        "latched": True,
        "force": True,
        "seen": True,
    }


def test_emergency_charge_is_capped_by_the_battery_max_charge():
    result = safety.cell_voltage_guard(
        [battery(max_charge_w=600.0)],
        {"example_battery_1": live(2.90)},
        protection_settings(**{"protection.emergency_charge_power_w": 900.0}),
        NOW,
    )
    assert guard_reading(result).force_charge_w == 600.0


def test_emergency_charge_uses_the_configured_power_when_lower():
    result = safety.cell_voltage_guard(
        [battery(max_charge_w=2000.0)],
        {"example_battery_1": live(2.90)},
        protection_settings(**{"protection.emergency_charge_power_w": 700.0}),
        NOW,
    )
    assert guard_reading(result).force_charge_w == 700.0


def test_between_cutoff_and_resume_blocks_discharge_without_forcing_charge():
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(3.05)}, protection_settings(), NOW
    )
    reading = guard_reading(result)
    assert reading.latched and reading.block_discharge
    assert reading.force is False and reading.force_charge_w is None
    assert reading.reason == Reason.CELL_VOLTAGE_GUARD
    assert "warning" in (reading.note or "")


def test_at_or_above_resume_clears_the_latch():
    latched = {"example_battery_1": {"latched": True, "force": True, "seen": True}}
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(3.20)}, protection_settings(), NOW, latched
    )
    reading = guard_reading(result)
    assert reading.latched is False and reading.block_discharge is False
    assert reading.force_charge_w is None and reading.reason is None
    assert result.latch_state()["example_battery_1"]["latched"] is False


def test_hysteresis_keeps_the_latch_between_cutoff_and_resume():
    latched = {"example_battery_1": {"latched": True, "force": True, "seen": True}}
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(3.12)}, protection_settings(), NOW, latched
    )
    reading = guard_reading(result)
    assert reading.latched and reading.block_discharge
    assert reading.force is False  # the forced charge stops once above the cutoff


def test_stale_reading_keeps_the_previous_latch_and_notes_it():
    latched = {"example_battery_1": {"latched": True, "force": True, "seen": True}}
    result = safety.cell_voltage_guard(
        [battery()],
        {"example_battery_1": live(2.90, age_s=10_000)},
        protection_settings(),
        NOW,
        latched,
    )
    reading = guard_reading(result)
    assert reading.fresh is False
    assert reading.latched and reading.block_discharge and reading.force_charge_w == 1000.0
    assert reading.reason == Reason.CELL_VOLTAGE_STALE
    assert "stale" in (reading.note or "")


def test_a_battery_whose_voltage_was_never_seen_stays_inactive():
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(None)}, protection_settings(), NOW
    )
    reading = guard_reading(result)
    assert reading.seen is False and reading.latched is False
    assert reading.block_discharge is False and reading.reason is None
    assert "no cell voltage" in (reading.note or "")


def test_a_zero_reading_is_not_a_voltage():
    """0 V is not a plausible cell voltage, so it must never latch the guard."""
    result = safety.cell_voltage_guard(
        [battery()], {"example_battery_1": live(0.0)}, protection_settings(), NOW
    )
    reading = guard_reading(result)
    assert reading.fresh is False and reading.latched is False


def test_stale_battery_state_is_not_a_fresh_voltage():
    result = safety.cell_voltage_guard(
        [battery()],
        {"example_battery_1": live(2.90, stale=True)},
        protection_settings(),
        NOW,
    )
    assert guard_reading(result).fresh is False


def test_guard_is_per_battery():
    result = safety.cell_voltage_guard(
        [battery("a_low"), battery("b_ok")],
        {"a_low": live(2.95, battery_id="a_low"), "b_ok": live(3.30, battery_id="b_ok")},
        protection_settings(),
        NOW,
    )
    low = guard_reading(result, "a_low")
    ok = guard_reading(result, "b_ok")
    assert low.force_charge_w == 1000.0 and low.block_discharge
    assert ok.latched is False and ok.force_charge_w is None
    assert result.latched_ids == ("a_low",)


def test_guard_does_not_mutate_its_inputs():
    configs = [battery()]
    states = {"example_battery_1": live(2.90)}
    before = dict(states["example_battery_1"].extra)
    safety.cell_voltage_guard(configs, states, protection_settings(), NOW)
    assert states["example_battery_1"].extra == before
    assert configs[0].mapping == {"power_setpoint_entity": "number.example_power"}


# ---------------------------------------------------------------------------------------------
# 3. Safety-check integration and ordering
# ---------------------------------------------------------------------------------------------


def plan_slot(power_w: float) -> PlanSlot:
    return PlanSlot(
        t=NOW,
        buy=0.2,
        sell=0.05,
        pv_kw=0.0,
        load_kw=0.5,
        soc_kwh=2.5,
        power_w=power_w,
        action=Action.CHARGE if power_w > 0 else Action.DISCHARGE,
        reason=Reason.ARBITRAGE_CHARGE if power_w > 0 else Reason.SELF_CONSUMPTION,
    )


def system_state(batteries, **overrides) -> SystemState:
    data = dict(mode=Mode.AUTO.value, dry_run=False, ha_connected=True, now=NOW, batteries=batteries)
    data.update(overrides)
    return SystemState(**data)


def test_check_returns_the_guard_and_blocks_a_lone_latched_discharge():
    config = battery()
    state = system_state([live(2.95)])
    decision = safety.check(-800.0, plan_slot(-800.0), [config], state, protection_settings(), NOW)
    assert decision.guard is not None and decision.guard.enabled
    assert decision.block is False
    assert decision.allowed_w >= 0.0
    assert Reason.CELL_VOLTAGE_GUARD in decision.reasons
    assert Reason.CELL_VOLTAGE_EMERGENCY_CHARGE in decision.reasons


def test_check_keeps_its_order_when_the_guard_is_disabled():
    """The existing clauses are untouched when protection is off."""
    config = battery()
    state = system_state([live(2.95)])
    settings = protection_settings(**{"protection.cell_voltage_enabled": False})
    decision = safety.check(-800.0, plan_slot(-800.0), [config], state, settings, NOW)
    assert decision.allowed_w == -800.0
    assert decision.guard is not None and decision.guard.enabled is False
    assert Reason.CELL_VOLTAGE_GUARD not in decision.reasons


def test_check_advisory_still_evaluates_the_guard_without_allowing_a_write():
    config = battery()
    state = system_state([live(2.95)], mode=Mode.ADVISORY.value)
    decision = safety.check(-800.0, plan_slot(-800.0), [config], state, protection_settings(), NOW)
    assert decision.block is True
    assert decision.reasons[0] == Reason.MODE_NOT_AUTO
    assert decision.guard is not None
    assert guard_reading(decision.guard).force_charge_w == 1000.0


def test_check_mixed_fleet_leaves_the_aggregate_alone():
    configs = [battery("a_low"), battery("b_ok")]
    state = system_state([live(2.95, battery_id="a_low"), live(3.30, battery_id="b_ok")])
    decision = safety.check(
        -1500.0, plan_slot(-1500.0), configs, state, protection_settings(), NOW
    )
    # b_ok can still discharge; the dispatcher blocks only a_low per battery.
    assert decision.allowed_w == -1500.0
    assert guard_reading(decision.guard, "a_low").block_discharge is True
    assert guard_reading(decision.guard, "b_ok").block_discharge is False


def test_check_passes_the_latch_in_and_out():
    config = battery()
    latched = {"example_battery_1": {"latched": True, "force": True, "seen": True}}
    state = system_state([live(None)])  # stale, but the latch is remembered
    decision = safety.check(
        -800.0, plan_slot(-800.0), [config], state, protection_settings(), NOW,
        cell_voltage_latch=latched,
    )
    reading = guard_reading(decision.guard)
    assert reading.latched and reading.force_charge_w == 1000.0
    assert decision.guard.latch_state()["example_battery_1"]["latched"] is True


# ---------------------------------------------------------------------------------------------
# 4. Allocation
# ---------------------------------------------------------------------------------------------


def test_allocate_power_never_discharges_a_blocked_battery():
    configs = [battery("a_low"), battery("b_ok")]
    entries = allocate_power(
        -1500.0,
        configs,
        {"a_low": 2.5, "b_ok": 2.5},
        slot_hours=30 / 3600,
        block_discharge_ids={"a_low"},
    )
    by_id = {config.id: power for config, power in entries}
    assert by_id["a_low"] == 0.0
    assert by_id["b_ok"] < 0.0


# ---------------------------------------------------------------------------------------------
# 5. Dispatcher: advisory / dry-run / writes / reasons
# ---------------------------------------------------------------------------------------------


class RecordingDriver:
    """A driver that records what it was asked to write, without touching Home Assistant."""

    def __init__(self, config: BatteryConfig, state: BatteryState) -> None:
        self.config = config
        self._state = state
        self.applied: list[tuple[float, bool]] = []
        self.tolerance_w = 5.0

    def id(self) -> str:
        return self.config.id

    def read_state(self) -> BatteryState:
        return self._state

    def apply(self, power_w: float, *, dry_run: bool) -> DispatchResult:
        self.applied.append((float(power_w), bool(dry_run)))
        return DispatchResult(
            battery_id=self.config.id,
            ts=NOW,
            requested_w=float(power_w),
            applied_w=float(power_w),
            ok=True,
            dry_run=dry_run,
            code=Reason.OK,
        )

    def hold(self, *, dry_run: bool) -> DispatchResult:
        return self.apply(0.0, dry_run=dry_run)

    def capabilities(self):
        return {}

    def commanded_power(self):
        return None


class FakeJournal:
    def __init__(self) -> None:
        self.dispatches: list[dict] = []
        self.events: list[tuple] = []

    def dispatch(self, entry):
        self.dispatches.append(dict(entry))

    def event(self, level, code, message, payload=None):
        self.events.append((level, code, message, payload or {}))


def make_plan(power_w: float) -> Plan:
    slot = plan_slot(power_w)
    return Plan(
        generated_at=NOW,
        horizon_start=slot.t,
        horizon_end=slot.t + timedelta(minutes=15),
        slot_minutes=15,
        slots=[slot],
    )


def test_dispatcher_advisory_logs_the_guard_and_never_writes():
    fake = FakeHAState({"number.example_power": {"state": "0", "attributes": {}}})
    ha = FakeHAFacade(fake)
    config = battery(mapping={"power_setpoint_entity": "number.example_power"})
    driver = registry.create(config, ha)
    journal = FakeJournal()
    dispatcher = Dispatcher(protection_settings(), journal, {config.id: driver})
    state = system_state([live(2.90)], mode=Mode.ADVISORY.value)
    outcome = dispatcher.tick(
        NOW, state=state, plan=make_plan(-600.0), batteries=[config], battery_states=[live(2.90)]
    )
    assert fake.service_calls == []
    assert outcome.guard is not None
    assert journal.dispatches and journal.dispatches[0]["code"] == Reason.CELL_VOLTAGE_EMERGENCY_CHARGE
    assert outcome.results[0].applied_w == 0.0


def test_dispatcher_dry_run_reports_the_charge_without_a_write():
    fake = FakeHAState({"number.example_power": {"state": "0", "attributes": {}}})
    ha = FakeHAFacade(fake)
    config = battery(mapping={"power_setpoint_entity": "number.example_power"})
    driver = registry.create(config, ha)
    journal = FakeJournal()
    dispatcher = Dispatcher(protection_settings(), journal, {config.id: driver})
    state = system_state([live(2.90)], dry_run=True)
    outcome = dispatcher.tick(
        NOW, state=state, plan=make_plan(-600.0), batteries=[config], battery_states=[live(2.90)]
    )
    assert fake.service_calls == []
    assert outcome.results[0].dry_run is True
    assert outcome.results[0].code == Reason.DRY_RUN
    assert journal.dispatches[0]["reason"] == Reason.CELL_VOLTAGE_EMERGENCY_CHARGE
    assert journal.dispatches[0]["result"] == "dry_run"


def test_dispatcher_auto_commands_the_emergency_charge():
    config = battery(mapping={"power_setpoint_entity": "number.example_power"})
    driver = RecordingDriver(config, live(2.90))
    journal = FakeJournal()
    dispatcher = Dispatcher(protection_settings(), journal, {config.id: driver})
    state = system_state([live(2.90)])
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan(-600.0), batteries=[config])
    assert driver.applied and driver.applied[0][0] == 1000.0
    assert outcome.results[0].applied_w == 1000.0
    assert journal.dispatches[0]["reason"] == Reason.CELL_VOLTAGE_EMERGENCY_CHARGE


def test_dispatcher_blocks_a_latched_battery_but_still_uses_the_healthy_one():
    low = battery("a_low")
    ok = battery("b_ok")
    low_driver = RecordingDriver(low, live(3.05, battery_id="a_low"))
    ok_driver = RecordingDriver(ok, live(3.30, battery_id="b_ok"))
    journal = FakeJournal()
    dispatcher = Dispatcher(protection_settings(), journal, {"a_low": low_driver, "b_ok": ok_driver})
    state = system_state([live(3.05, battery_id="a_low"), live(3.30, battery_id="b_ok")])
    outcome = dispatcher.tick(
        NOW, state=state, plan=make_plan(-1500.0), batteries=[low, ok]
    )
    applied = {result.battery_id: result.applied_w for result in outcome.results}
    assert applied["a_low"] >= 0.0
    assert applied["b_ok"] < 0.0
    reasons = {entry["battery_id"]: entry["reason"] for entry in journal.dispatches}
    assert reasons["a_low"] == Reason.CELL_VOLTAGE_GUARD
    assert reasons["b_ok"] != Reason.CELL_VOLTAGE_GUARD


def test_dispatcher_forced_charge_respects_max_soc():
    config = battery(mapping={"power_setpoint_entity": "number.example_power"})
    driver = RecordingDriver(config, live(2.90, soc_pct=100.0))
    dispatcher = Dispatcher(protection_settings(), FakeJournal(), {config.id: driver})
    state = system_state([live(2.90, soc_pct=100.0)])
    dispatcher.tick(NOW, state=state, plan=make_plan(-600.0), batteries=[config])
    assert driver.applied and driver.applied[0][0] == 0.0


def test_dispatcher_forced_charge_still_respects_the_ramp():
    config = battery()
    driver = RecordingDriver(config, live(2.90))
    driver._state.power_w = -900.0  # currently discharging: the ramp caps the swing
    dispatcher = Dispatcher(protection_settings(), FakeJournal(), {config.id: driver})
    state = system_state([live(2.90)])
    dispatcher.tick(NOW, state=state, plan=make_plan(-900.0), batteries=[config])
    assert driver.applied and driver.applied[0][0] == pytest.approx(100.0)


def test_dispatcher_reason_for_a_warning_band_battery_is_the_guard_slug():
    config = battery()
    driver = RecordingDriver(config, live(3.05))
    journal = FakeJournal()
    dispatcher = Dispatcher(protection_settings(), journal, {config.id: driver})
    state = system_state([live(3.05)])
    dispatcher.tick(NOW, state=state, plan=make_plan(-600.0), batteries=[config])
    assert journal.dispatches[0]["reason"] == Reason.CELL_VOLTAGE_GUARD
    assert driver.applied[0][0] >= 0.0


# ---------------------------------------------------------------------------------------------
# 6. Drivers: the mapped entity is primary, the hook the fallback
# ---------------------------------------------------------------------------------------------


def test_generic_driver_reads_the_mapped_cell_voltage():
    states = {
        "number.example_power": {"state": "0", "attributes": {}},
        "sensor.example_soc": {"state": "50", "attributes": {"unit_of_measurement": "%"}},
        "sensor.example_cell": {"state": "3.05", "attributes": {"unit_of_measurement": "V"}},
    }
    ha = FakeHAFacade(FakeHAState(states))
    config = battery(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "soc_entity": "sensor.example_soc",
            "cell_voltage_entity": "sensor.example_cell",
        }
    )
    driver = registry.create(config, ha)
    assert driver.read_state().extra["cell_voltage_v"] == pytest.approx(3.05)


def test_missing_cell_voltage_entity_yields_none():
    states = {"number.example_power": {"state": "0", "attributes": {}}}
    ha = FakeHAFacade(FakeHAState(states))
    config = battery(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "cell_voltage_entity": "sensor.example_missing_cell",
        }
    )
    driver = registry.create(config, ha)
    assert driver.read_state().extra["cell_voltage_v"] is None


def test_mapping_validation_rejects_a_bad_cell_voltage_entity():
    problems = registry.validate_config(
        battery(
            mapping={
                "power_setpoint_entity": "number.example_power",
                "cell_voltage_entity": "not an entity id",
            }
        )
    )
    assert any("cell_voltage_entity" in problem for problem in problems)


class _HookDriver(RecordingDriver):
    def read_cell_voltage(self):
        return 3.20


def test_dispatcher_uses_the_driver_hook_only_without_a_mapped_entity():
    config = battery()
    state = live(None)  # read_state carries no voltage
    hook = _HookDriver(config, state)
    dispatcher = Dispatcher(protection_settings(), FakeJournal(), {config.id: hook})
    dispatcher.read_batteries(NOW, [config])
    assert state.extra["cell_voltage_v"] == pytest.approx(3.20)

    # with a mapped entity the hook must not be consulted (the entity is primary)
    calls = []

    class _CountingHook(_HookDriver):
        def read_cell_voltage(self):
            calls.append(1)
            return 3.20

    mapped = battery(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "cell_voltage_entity": "sensor.example_cell",
        }
    )
    mapped_state = live(None)
    dispatcher2 = Dispatcher(
        protection_settings(), FakeJournal(), {mapped.id: _CountingHook(mapped, mapped_state)}
    )
    dispatcher2.read_batteries(NOW, [mapped])
    assert calls == []


# ---------------------------------------------------------------------------------------------
# 7. API surface
# ---------------------------------------------------------------------------------------------


def test_status_dict_exposes_the_cell_voltage_fields():
    config = battery()
    battery_state = live(3.05)
    state = system_state([battery_state])
    payload = status_dict(
        state,
        None,
        cell_voltage={
            "enabled": True,
            "cutoff_v": 3.0,
            "warning_v": 3.1,
            "resume_v": 3.18,
            "emergency_charge_power_w": 0.0,
            "latched": ["example_battery_1"],
            "batteries": [{"battery_id": "example_battery_1", "voltage_v": 3.05}],
        },
    )
    assert payload["batteries"][0]["cell_voltage_v"] == pytest.approx(3.05)
    assert payload["cell_voltage"]["enabled"] is True
    assert payload["cell_voltage"]["latched"] == ["example_battery_1"]


def test_status_dict_cell_voltage_is_null_without_a_guard():
    state = system_state([live(3.05)])
    payload = status_dict(state, None)
    assert payload["cell_voltage"] is None


# ---------------------------------------------------------------------------------------------
# 8. Engine end to end: latch, alerts, status and restart
# ---------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


def build_engine(tmp_path, *, mode: str = Mode.AUTO.value, dry_run: bool = True, voltage: str = "2.90"):
    from app.engine import Engine
    from app.main import build_settings, build_store
    from app.models import LoadForecast, LoadSlot, PriceForecast, PriceSlot, SolarForecast

    store = build_store(str(tmp_path))
    settings = build_settings(store, {}, os.path.join(str(tmp_path), "options.json"))
    states = {
        "number.example_battery_power": {"state": "0", "attributes": {}},
        "sensor.example_battery_soc": {"state": "50", "attributes": {"unit_of_measurement": "%"}},
        "sensor.example_battery_cell": {"state": voltage, "attributes": {"unit_of_measurement": "V"}},
    }
    if settings.battery("example_battery_1") is None:
        settings.add_battery(
            battery(
                "example_battery_1",
                capacity_kwh=5.0,
                mapping={
                    "power_setpoint_entity": "number.example_battery_power",
                    "soc_entity": "sensor.example_battery_soc",
                    "cell_voltage_entity": "sensor.example_battery_cell",
                },
            )
        )
    settings.set("mode", mode)
    settings.set("dry_run", dry_run)
    settings.set("protection.cell_voltage_enabled", True)
    settings.set("solar.enabled", False)
    settings.set("notify.enabled", False)
    settings.set("control.grid_follow", False)
    settings.set("price.source", "static")
    settings.set("price.static_schedule", [])

    def price_source(now):
        forecast = PriceForecast(source="example_static", generated_at=now)
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            moment = start + timedelta(minutes=15 * index)
            forecast.slots.append(PriceSlot(t=moment, buy=0.30, sell=0.01, source="example_static"))
        return forecast

    def solar_source(now):
        return SolarForecast(source="example_solar", generated_at=now)

    def load_source(now):
        forecast = LoadForecast(source="example_load", generated_at=now, confidence="low")
        start = now.replace(minute=0, second=0, microsecond=0)
        for index in range(96):
            forecast.slots.append(LoadSlot(t=start + timedelta(minutes=15 * index), load_kw=0.6))
        return forecast

    live = FakeHAState(states)
    ha = FakeHAFacade(live)
    clock = Clock(datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc))
    engine = Engine(
        settings,
        store=store,
        ha=ha,
        price_source=price_source,
        solar_source=solar_source,
        load_source=load_source,
        sampler=lambda now: {"grid_w": 300.0, "pv_w": 0.0, "load_w": 600.0},
        registry_source=lambda: [],
        location="Example Home",
        clock=clock,
        version="0.1.0",
    )
    return engine, settings, store, live, clock


def test_engine_latches_alerts_and_reports_the_guard():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine, settings, store, live, clock = build_engine(tmp, mode=Mode.ADVISORY.value)
        try:
            engine.tick_now()
            status = engine.cell_voltage_status()
            assert status["enabled"] is True
            assert status["latched"] == ["example_battery_1"]
            assert status["batteries"][0]["voltage_v"] == pytest.approx(2.90)
            assert live.service_calls == []  # advisory never writes
            codes = [alert.code for alert in engine.snapshot().alerts]
            assert Reason.CELL_VOLTAGE_EMERGENCY_CHARGE in codes
        finally:
            store.close()


def test_engine_auto_writes_the_emergency_charge():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine, settings, store, live, clock = build_engine(tmp, mode=Mode.AUTO.value, dry_run=False)
        try:
            engine.tick_now()
            writes = [
                call for call in live.service_calls if call[2].get("entity_id") == "number.example_battery_power"
            ]
            assert writes, live.service_calls
            assert max(float(call[2]["value"]) for call in writes) == pytest.approx(1000.0)
        finally:
            store.close()


def test_engine_resume_clears_the_latch():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine, settings, store, live, clock = build_engine(tmp, mode=Mode.ADVISORY.value)
        try:
            engine.tick_now()
            assert engine.cell_voltage_status()["latched"] == ["example_battery_1"]
            live.write_state("sensor.example_battery_cell", "3.30")
            engine.tick_now()
            assert engine.cell_voltage_status()["latched"] == []
            assert not any(
                item.get("block_discharge") for item in engine.cell_voltage_status()["batteries"]
            )
        finally:
            store.close()


def test_engine_stale_voltage_keeps_the_latch_and_alerts():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine, settings, store, live, clock = build_engine(tmp, mode=Mode.ADVISORY.value)
        try:
            engine.tick_now()
            live.write_state("sensor.example_battery_cell", "unavailable")
            engine.tick_now()
            status = engine.cell_voltage_status()
            assert status["latched"] == ["example_battery_1"]
            assert status["batteries"][0]["fresh"] is False
            codes = [alert.code for alert in engine.snapshot().alerts]
            assert Reason.CELL_VOLTAGE_STALE in codes
        finally:
            store.close()


def test_engine_applies_a_multi_key_protection_patch_atomically(tmp_path):
    engine, settings, store, live, clock = build_engine(str(tmp_path), mode=Mode.ADVISORY.value)
    try:
        result = engine.apply_settings(
            {
                "protection.cell_voltage_cutoff_v": 3.20,
                "protection.cell_voltage_warning_v": 3.30,
                "protection.cell_voltage_resume_v": 3.40,
            }
        )
        assert result["errors"] == [], result["errors"]
        assert settings.get("protection.cell_voltage_cutoff_v") == 3.20
        bad = engine.apply_settings(
            {
                "protection.cell_voltage_cutoff_v": 3.50,
                "protection.cell_voltage_warning_v": 3.30,
            }
        )
        assert {row["key"] for row in bad["errors"]} == {
            "protection.cell_voltage_cutoff_v",
            "protection.cell_voltage_warning_v",
        }
        assert settings.get("protection.cell_voltage_cutoff_v") == 3.20
    finally:
        store.close()


def test_engine_restart_restores_the_latch():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine, settings, store, live, clock = build_engine(tmp, mode=Mode.ADVISORY.value)
        engine.tick_now()
        assert engine.cell_voltage_status()["latched"] == ["example_battery_1"]
        store.close()

        # A fresh engine over the same store, with the voltage now unreadable: the latch survives.
        engine2, settings2, store2, live2, clock2 = build_engine(tmp, mode=Mode.ADVISORY.value, voltage="unavailable")
        try:
            engine2.tick_now()
            assert engine2.cell_voltage_status()["latched"] == ["example_battery_1"]
        finally:
            store2.close()
