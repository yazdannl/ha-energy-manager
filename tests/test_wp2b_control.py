"""WP2B control-layer tests: driver registry, generic/zendure drivers and the dispatch tick.

All Home Assistant access is a recorded in-process fake, so the tests prove exactly which service
calls are made (and that ``advisory`` and ``dry_run`` make none).
"""

from __future__ import annotations

import os
import sys

import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from fake_ha import FakeHAFacade, FakeHAState

from app.control import drivers as registry
from app.control import safety  # noqa: E402
from app.control.dispatcher import Dispatcher  # noqa: E402
from app.control.drivers.base import (  # noqa: E402
    IDEMPOTENT_TOLERANCE_W,
    DriverError,
    parse_energy_kwh,
    parse_soc_pct,
)
from app.control.drivers.zendure import (  # noqa: E402
    MANAGER_SIGN,
    resolve_mapping,
)
from app.haws import discover_candidates  # noqa: E402 - the single registry scorer (SPEC 10)
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

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

SETTINGS = {
    "control.tick_seconds": 30,
    "control.stale_state_seconds": 180,
    "control.replan_minutes": 15,
    "control.grid_follow": True,
    "control.grid_follow_deadband_w": 30,
    "control.grid_follow_max_ratio": 0.5,
    "control.failsafe_on_error": True,
    "optimizer.max_grid_import_w": 0,
    "optimizer.max_grid_export_w": 0,
    "optimizer.reserve_soc_pct": 0.0,
}


class FakeHA:
    def __init__(self, states=None):
        self.states = dict(states or {})
        self.calls: list[tuple[str, str, dict]] = []
        self.published: dict[str, dict] = {}
        self.fail_on: set[str] = set()

    @property
    def writes_done(self) -> bool:
        return bool(self.calls)

    def link_device(self, manual_power_entity, grid_input_entity, output_home_entity) -> None:
        """Emulate a SolarFlow manager: it applies the setpoint on the next P1 event.

        ``manual_power`` is negative for charging, so the measured entities follow the negated raw
        value: charging shows up on ``grid_input_power``, discharging on ``output_home_power``.
        """
        self._device = (manual_power_entity, grid_input_entity, output_home_entity)

    def _apply_device(self, data: dict) -> None:
        device = getattr(self, "_device", None)
        if device is None or str(data.get("entity_id")) != device[0]:
            return
        raw = float(self.states[device[0]]["state"])
        self.states[device[1]] = {"state": str(max(0.0, -raw)), "attributes": {}}
        self.states[device[2]] = {"state": str(max(0.0, raw)), "attributes": {}}

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def call_service(self, domain, service, data):
        call = (domain, service, dict(data))
        self.calls.append(call)
        if str(data.get("entity_id")) in self.fail_on:
            raise RuntimeError("example HA failure")
        if domain in ("number", "input_number"):
            self.states[str(data.get("entity_id"))] = {"state": str(data.get("value")), "attributes": {}}
            self._apply_device(data)
        elif domain in ("select", "input_select"):
            self.states[str(data.get("entity_id"))] = {
                "state": str(data.get("option")),
                "attributes": {},
            }
        return None

    def set_state(self, entity_id, state, attributes=None):
        self.published[entity_id] = {"state": state, "attributes": attributes or {}}


class FakeJournal:
    def __init__(self):
        self.dispatches: list[dict] = []
        self.events: list[tuple] = []

    def dispatch(self, entry):
        self.dispatches.append(dict(entry))

    def event(self, level, code, message, payload=None):
        self.events.append((level, code, message, payload or {}))


def config(**overrides) -> BatteryConfig:
    data = dict(
        id="example_battery_1",
        name="Example Battery 1",
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


def plan_slot(power_w=600.0, load_kw=0.5, pv_kw=0.0) -> PlanSlot:
    return PlanSlot(
        t=NOW,
        buy=0.2,
        sell=0.05,
        pv_kw=pv_kw,
        load_kw=load_kw,
        soc_kwh=2.5,
        power_w=power_w,
        action=Action.CHARGE if power_w > 0 else Action.DISCHARGE,
        reason=Reason.ARBITRAGE_CHARGE if power_w > 0 else Reason.SELF_CONSUMPTION,
    )


def make_plan(slots=None) -> Plan:
    chosen = slots or [plan_slot()]
    return Plan(
        generated_at=NOW,
        horizon_start=chosen[0].t,
        horizon_end=chosen[-1].t + timedelta(minutes=15),
        slot_minutes=15,
        slots=chosen,
    )


def system_state(**overrides) -> SystemState:
    data = dict(mode=Mode.AUTO.value, dry_run=False, ha_connected=True, now=NOW)
    data.update(overrides)
    return SystemState(**data)


# ------------------------------------------------------------------------------ registry


def test_registry_lists_the_shipped_drivers():
    assert registry.names() == ["generic", "zendure"]


def test_validate_config_rejects_unusable_mappings_at_save_time():
    problems = registry.validate_config(config(driver="generic", mapping={}))
    assert any("power_setpoint_entity" in problem for problem in problems)

    both = registry.validate_config(
        config(
            mapping={
                "power_setpoint_entity": "number.example_power",
                "charge_power_entity": "number.example_charge",
                "discharge_power_entity": "number.example_discharge",
            }
        )
    )
    assert any("not both" in problem for problem in both)

    wrong_domain = registry.validate_config(
        config(mapping={"power_setpoint_entity": "sensor.example_power"})
    )
    assert any("number" in problem for problem in wrong_domain)

    unknown = registry.validate_config(config(driver="example_unknown", mapping={"a": "number.b"}))
    assert any("unknown driver" in problem for problem in unknown)

    half_pair = registry.validate_config(config(mapping={"charge_power_entity": "number.example_c"}))
    assert half_pair


def test_create_refuses_bad_mappings_and_builds_good_ones():
    ha = FakeHA()
    try:
        registry.create(config(mapping={}), ha)
        raise AssertionError("expected a ValueError")
    except ValueError as exc:
        assert "power_setpoint_entity" in str(exc)
    driver = registry.create(config(), ha)
    assert driver.id() == "example_battery_1"
    assert driver.capabilities()["mapping_resolved"] is True


# ------------------------------------------------------------------------- generic driver


def test_generic_driver_dry_run_makes_no_calls_and_lists_writes():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    driver = registry.create(config(), ha)
    result = driver.apply(500.0, dry_run=True)
    assert result.ok and result.dry_run and result.code == Reason.DRY_RUN
    assert ha.calls == []
    assert result.writes and result.writes[0]["entity_id"] == "number.example_power"
    assert result.applied_w == 500.0


def test_generic_driver_writes_and_verifies_readback():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    driver = registry.create(config(), ha)
    result = driver.apply(500.0, dry_run=False)
    assert result.ok and result.code == Reason.OK
    assert ha.calls == [("number", "set_value", {"entity_id": "number.example_power", "value": 500.0})]
    assert result.readback_w == 500.0


def test_generic_driver_is_idempotent_within_tolerance():
    ha = FakeHA({"number.example_power": {"state": "500", "attributes": {}}})
    driver = registry.create(config(), ha)
    result = driver.apply(510.0, dry_run=False)
    assert result.code == Reason.IDEMPOTENT_SKIP and result.writes == []
    assert ha.calls == []
    # beyond the tolerance it writes again
    result = driver.apply(700.0, dry_run=False)
    assert result.code == Reason.OK and len(ha.calls) == 1
    assert IDEMPOTENT_TOLERANCE_W <= 30.0


def test_generic_driver_reports_readback_mismatch_as_driver_error():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    driver = registry.create(config(), ha)

    def ignore_write(domain, service, data):  # device silently refuses
        ha.calls.append((domain, service, dict(data)))

    ha.call_service = ignore_write
    result = driver.apply(500.0, dry_run=False)
    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert "read-back" in result.message


def test_generic_driver_pair_mapping_writes_both_sides():
    ha = FakeHA(
        {
            "number.example_charge": {"state": "0", "attributes": {}},
            "number.example_discharge": {"state": "0", "attributes": {}},
        }
    )
    cfg = config(
        mapping={
            "charge_power_entity": "number.example_charge",
            "discharge_power_entity": "number.example_discharge",
        }
    )
    driver = registry.create(cfg, ha)
    assert driver.apply(400.0, dry_run=False).ok
    assert ha.states["number.example_charge"]["state"] == "400.0"
    assert ha.states["number.example_discharge"]["state"] == "0.0"
    assert driver.apply(-300.0, dry_run=False).ok
    assert ha.states["number.example_charge"]["state"] == "0.0"
    assert ha.states["number.example_discharge"]["state"] == "300.0"
    state = driver.read_state()
    assert state.power_w == -300.0 and state.available


def test_generic_driver_selects_manual_mode_once():
    ha = FakeHA(
        {
            "select.example_mode": {"state": "auto", "attributes": {}},
            "number.example_power": {"state": "0", "attributes": {}},
        }
    )
    cfg = config(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "mode_select_entity": "select.example_mode",
            "mode_manual_value": "manual",
        }
    )
    driver = registry.create(cfg, ha)
    assert driver.apply(200.0, dry_run=False).ok
    domains = [(call[0], call[1]) for call in ha.calls]
    assert domains[0] == ("select", "select_option")
    assert domains[1] == ("number", "set_value")
    # the mode is already manual now: only the number is written again
    ha.calls.clear()
    assert driver.apply(300.0, dry_run=False).ok
    assert [call[0] for call in ha.calls] == ["number"]


def test_generic_driver_reads_soc_and_capacity_units():
    ha = FakeHA(
        {
            "number.example_power": {"state": "0", "attributes": {}},
            "sensor.example_soc": {"state": "0.55", "attributes": {"unit_of_measurement": "%"}},
            "sensor.example_capacity": {"state": "5200", "attributes": {"unit_of_measurement": "Wh"}},
        }
    )
    cfg = config(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "soc_entity": "sensor.example_soc",
            "capacity_entity": "sensor.example_capacity",
        }
    )
    driver = registry.create(cfg, ha)
    state = driver.read_state()
    assert state.soc_pct == 0.55
    assert state.capacity_kwh == 5.2
    assert abs(state.energy_kwh - 5.2 * 0.55 / 100.0) < 1e-9
    assert parse_soc_pct("55", {"unit_of_measurement": "%"}) == 55.0
    assert parse_soc_pct(0.42, {}) == 42.0
    assert parse_soc_pct("unavailable", {}) is None
    assert parse_energy_kwh("1.5", {"unit_of_measurement": "kWh"}) == 1.5


def test_generic_driver_unsupported_domain_is_rejected_at_save_time():
    ha = FakeHA({"sensor.example_power": {"state": "0", "attributes": {}}})
    with_sensor = battery_with_sensor_mapping()
    try:
        registry.create(with_sensor, ha)
        raise AssertionError("expected the mapping to be rejected")
    except ValueError as exc:
        assert "number" in str(exc)
    assert ha.calls == []
    # and the write helper itself refuses an unwritable domain rather than guessing
    driver = registry.create(config(), ha)
    try:
        driver.make_write("sensor.example_power", 1.0)
        raise AssertionError("expected DriverError")
    except DriverError:
        pass


def battery_with_sensor_mapping() -> BatteryConfig:
    cfg = config(mapping={"power_setpoint_entity": "number.example_power"})
    return BatteryConfig(
        id=cfg.id,
        name=cfg.name,
        driver=cfg.driver,
        capacity_kwh=cfg.capacity_kwh,
        min_soc_pct=cfg.min_soc_pct,
        max_soc_pct=cfg.max_soc_pct,
        max_charge_w=cfg.max_charge_w,
        max_discharge_w=cfg.max_discharge_w,
        eta_charge=cfg.eta_charge,
        eta_discharge=cfg.eta_discharge,
        mapping={"power_setpoint_entity": "sensor.example_power"},
    )


# ------------------------------------------------------------------------- zendure driver


def zendure_entries():
    """Synthetic registry rows: one manager plus two devices, all obviously fake values."""
    return [
        {
            "entity_id": "number.example_manager_manual_power",
            "platform": "zendure_ha",
            "translation_key": "manual_power",
            "device_id": "example_manager",
            "name": "Manual power",
        },
        {
            "entity_id": "select.example_manager_operation",
            "platform": "zendure_ha",
            "translation_key": "operation",
            "device_id": "example_manager",
            "name": "Operation",
        },
        {
            "entity_id": "number.example_device_input_limit",
            "platform": "zendure_ha",
            "translation_key": "input_limit",
            "device_id": "example_device_1",
            "name": "Input limit",
        },
        {
            "entity_id": "number.example_device_output_limit",
            "platform": "zendure_ha",
            "translation_key": "output_limit",
            "device_id": "example_device_1",
            "name": "Output limit",
        },
        {
            "entity_id": "sensor.example_device_electric_level",
            "platform": "zendure_ha",
            "translation_key": "electric_level",
            "device_id": "example_device_1",
            "name": "Electric level",
        },
        {
            "entity_id": "sensor.example_device_output_home",
            "platform": "zendure_ha",
            "translation_key": "output_home_power",
            "device_id": "example_device_1",
            "name": "Output home power",
        },
        {
            "entity_id": "sensor.example_device_grid_input",
            "platform": "zendure_ha",
            "translation_key": "grid_input_power",
            "device_id": "example_device_1",
            "name": "Grid input power",
        },
        {
            "entity_id": "sensor.example_device_total_kwh",
            "platform": "zendure_ha",
            "translation_key": "total_kwh",
            "device_id": "example_device_1",
            "name": "Total kWh",
        },
        {
            "entity_id": "sensor.example_device_soc_status",
            "platform": "zendure_ha",
            "translation_key": "soc_status",
            "device_id": "example_device_1",
            "name": "SoC status",
        },
        {
            "entity_id": "select.example_device_ac_mode",
            "platform": "zendure_ha",
            "translation_key": "ac_mode",
            "device_id": "example_device_1",
            "name": "AC mode",
        },
        {
            "entity_id": "sensor.example_other_level",
            "platform": "other_integration",
            "translation_key": "electric_level",
        },
        {
            # unique ids are NOT domain-prefixed: this row is not Zendure despite the prefix
            "entity_id": "sensor.example_unique_level",
            "unique_id": "zendure_ha.example",
            "translation_key": "electric_level",
            "device_id": "example_device_2",
        },
    ]


def manager_mapping(**extra):
    """The mapping the wizard would save for a manager install (roles resolved from the registry)."""
    mapping = resolve_mapping(zendure_entries(), anchor_entity="sensor.example_device_electric_level")
    mapping.update(extra)
    return mapping


def manager_ha(**states):
    base = {
        "select.example_manager_operation": {"state": "smart", "attributes": {}},
        "number.example_manager_manual_power": {"state": "0", "attributes": {}},
        "number.example_device_input_limit": {"state": "0", "attributes": {}},
        "number.example_device_output_limit": {"state": "0", "attributes": {}},
        "sensor.example_device_electric_level": {"state": "55", "attributes": {"unit_of_measurement": "%"}},
        "sensor.example_device_output_home": {"state": "0", "attributes": {}},
        "sensor.example_device_grid_input": {"state": "0", "attributes": {}},
        "sensor.example_device_total_kwh": {"state": "5.0", "attributes": {"unit_of_measurement": "kWh"}},
        "select.example_device_ac_mode": {"state": "output", "attributes": {}},
    }
    base.update(states)
    ha = FakeHA(base)
    ha.link_device(
        "number.example_manager_manual_power",
        "sensor.example_device_grid_input",
        "sensor.example_device_output_home",
    )
    return ha


def test_zendure_mapping_is_resolved_from_translation_keys():
    mapping = resolve_mapping(zendure_entries(), anchor_entity="sensor.example_device_electric_level")
    assert mapping == {
        "manual_power_entity": "number.example_manager_manual_power",
        "operation_entity": "select.example_manager_operation",
        "charge_limit_entity": "number.example_device_input_limit",
        "discharge_limit_entity": "number.example_device_output_limit",
        "soc_entity": "sensor.example_device_electric_level",
        "output_power_entity": "sensor.example_device_output_home",
        "grid_input_power_entity": "sensor.example_device_grid_input",
        "capacity_entity": "sensor.example_device_total_kwh",
        "ac_mode_entity": "select.example_device_ac_mode",
    }
    # soc_status is calibration bookkeeping and is never mapped as the SoC
    assert "soc_status" not in " ".join(mapping.values())


def test_zendure_mapping_device_filter_and_empty_result():
    # The manager roles stay system-wide; device-scoped roles follow the anchor's device.
    filtered = resolve_mapping(zendure_entries(), device_id="example_device_2")
    # A row whose only Zendure marker is a unique-id prefix is not Zendure (appendix A).
    assert "soc_entity" not in filtered
    assert filtered["manual_power_entity"] == "number.example_manager_manual_power"
    assert "charge_limit_entity" not in filtered
    assert resolve_mapping([], None) == {}
    assert resolve_mapping([{"entity_id": "sensor.x", "platform": "other"}]) == {}
    # a unique-id prefix alone never makes a row Zendure (appendix A)
    assert resolve_mapping([{"entity_id": "sensor.y", "unique_id": "zendure_ha.x"}]) == {}


def test_zendure_discovery_uses_the_haws_scorer():
    candidates = discover_candidates(
        "battery", zendure_entries(), {}, search="manual power", limit=5
    )
    assert [c["entity_id"] for c in candidates] == ["number.example_manager_manual_power"]
    assert candidates[0]["confidence"] > 0
    assert len(discover_candidates("battery", zendure_entries(), {}, limit=3)) <= 3
    assert len(discover_candidates("battery", zendure_entries(), {}, limit=0)) == 1  # clamped to >=1


def test_zendure_charges_with_a_negated_raw_setpoint():
    """appendix A: ``manual_power`` is negative for charging; +800 W must write -800."""
    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    assert driver.capabilities()["control_path"] == "manager_manual_power"

    result = driver.apply(800.0, dry_run=False)
    assert result.ok and result.code == Reason.OK
    assert ha.states["number.example_manager_manual_power"]["state"] == "-800.0"
    assert ha.states["select.example_manager_operation"]["state"] == "manual"
    assert result.readback_w == 800.0

    # discharge negates the other way: -400 W (our sign) writes +400 raw
    ha.states["sensor.example_device_output_home"] = {"state": "400", "attributes": {}}
    result = driver.apply(-400.0, dry_run=False)
    assert result.ok
    assert ha.states["number.example_manager_manual_power"]["state"] == "400.0"
    assert result.readback_w == -400.0
    assert MANAGER_SIGN == -1.0


def test_zendure_reads_the_authoritative_soc_and_capacity():
    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    state = driver.read_state()
    assert state.soc_pct == 55.0  # electric_level, not soc_status
    assert state.capacity_kwh == 5.0  # total_kwh
    assert state.energy_kwh == 2.75  # soc x total_kwh without available_kwh
    ha.states["sensor.example_device_available"] = {"state": "2.2", "attributes": {"unit_of_measurement": "kWh"}}
    mapping = manager_mapping(available_kwh_entity="sensor.example_device_available")
    driver = registry.create(config(driver="zendure", mapping=mapping), ha)
    # ``available_kwh`` is energy *above* min_soc (appendix A): 10 % of 5.0 kWh + 2.2 = 2.7 kWh,
    # so the plan's SoC line matches the battery card's ``electric_level`` (audit finding 9).
    state = driver.read_state()
    assert state.energy_kwh == 2.7
    assert abs(state.energy_kwh - state.capacity_kwh * state.soc_pct / 100.0) < 0.31


def offline_manager_ha(**states):
    """A manager whose device does *not* follow the setpoint (no P1 event, silent integration)."""
    ha = manager_ha(**states)
    ha._device = None  # disable the device emulation
    return ha


def test_zendure_readback_retries_instead_of_failing_on_a_slow_p1_event():
    """The setpoint lands on the next P1 event: retry inside the window, then trust the read-back."""
    sleeps: list[float] = []
    ha = offline_manager_ha()
    driver = registry.create(
        config(driver="zendure", mapping=manager_mapping()),
        ha,
        sleep=sleeps.append,
    )

    original = ha.get_state

    def slow(entity_id):
        state = original(entity_id)
        # the P1 event only lands on the third poll
        if entity_id == "sensor.example_device_grid_input" and len(sleeps) >= 2:
            return {"state": "300", "attributes": {}}
        return state

    ha.get_state = slow
    result = driver.apply(300.0, dry_run=False)
    assert result.ok, result.message
    assert result.readback_w == 300.0
    assert sleeps  # the driver waited instead of reporting a false failure


def test_zendure_reports_a_driver_error_when_the_device_never_follows():
    """Never a false success: a silent P1 sensor ends in a driver error, not in 'ok'."""
    ha = offline_manager_ha()
    driver = registry.create(
        config(driver="zendure", mapping=manager_mapping()), ha, sleep=lambda _s: None
    )
    result = driver.apply(500.0, dry_run=False)
    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert "read-back" in result.message


def test_zendure_reasserts_manual_operation_when_a_smart_mode_takes_over():
    ha = offline_manager_ha()
    driver = registry.create(
        config(driver="zendure", mapping=manager_mapping()), ha, sleep=lambda _s: None
    )
    original = ha.get_state

    def select_writes() -> int:
        return sum(1 for call in ha.calls if call[0] == "select")

    def power_writes() -> int:
        return sum(
            1
            for call in ha.calls
            if call[0] == "number" and call[2].get("entity_id") == "number.example_manager_manual_power"
        )

    def flaky(entity_id):
        state = original(entity_id)
        if entity_id == "sensor.example_device_grid_input":
            # the P1 event reports the commanded power only after the setpoint write
            return {"state": "250" if power_writes() else "0", "attributes": {}}
        if entity_id == "select.example_manager_operation":
            # A smart mode overwrites the select after our first write; the re-assert must stick.
            return {"state": "smart" if select_writes() < 2 else "manual", "attributes": {}}
        return state

    ha.get_state = flaky
    result = driver.apply(250.0, dry_run=False)
    assert result.ok, result.message
    selects = [call for call in ha.calls if call[0] == "select"]
    assert len(selects) == 2  # asserted before the write, re-asserted after the takeover


def test_zendure_reports_a_driver_error_when_manual_dispatch_cannot_be_held():
    """A smart mode that keeps grabbing the manager back must never look like success."""
    ha = offline_manager_ha()
    driver = registry.create(
        config(driver="zendure", mapping=manager_mapping()), ha, sleep=lambda _s: None
    )

    def power_writes() -> int:
        return sum(
            1
            for call in ha.calls
            if call[0] == "number" and call[2].get("entity_id") == "number.example_manager_manual_power"
        )

    def always_smart(entity_id):
        if entity_id == "select.example_manager_operation":
            return {"state": "smart", "attributes": {}}  # the smart mode never lets go
        if entity_id == "sensor.example_device_grid_input":
            return {"state": "250" if power_writes() else "0", "attributes": {}}
        return ha.states.get(entity_id)

    ha.get_state = always_smart
    result = driver.apply(250.0, dry_run=False)
    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert "manual" in result.message


def test_zendure_hold_and_dry_run_and_idempotency():
    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    result = driver.apply(300.0, dry_run=True)
    assert result.dry_run and ha.calls == []
    assert {write["entity_id"] for write in result.writes} >= {
        "number.example_manager_manual_power",
        "select.example_manager_operation",
    }
    # audit finding 1: the measured pair lags by one P1 event, so it must never justify a skip --
    # a battery *discharging* 295 W while the setpoint reads 0 must still be commanded
    ha = manager_ha(**{"sensor.example_device_output_home": {"state": "295", "attributes": {}}})
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    result = driver.apply(-295.0, dry_run=False)
    assert result.code != Reason.IDEMPOTENT_SKIP and result.ok
    assert any(
        call[2].get("entity_id") == "number.example_manager_manual_power" for call in ha.calls
    ), ha.calls
    # ...and a command that really is already on the setpoint is skipped without a write
    ha = manager_ha(**{"number.example_manager_manual_power": {"state": "-300", "attributes": {}}})
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    result = driver.apply(300.0, dry_run=False)
    assert result.code == Reason.IDEMPOTENT_SKIP and ha.calls == []

    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    held = driver.hold(dry_run=False)
    assert held.ok is True and held.applied_w == 0.0
    assert ha.states["number.example_manager_manual_power"]["state"] in ("0", "0.0")
    assert ha.states["sensor.example_device_grid_input"]["state"] in ("0", "0.0")


def test_zendure_ignores_an_incomplete_measured_pair():
    """Audit finding 4: one unavailable side must not turn an idle battery into a charging one."""
    idle = manager_ha(
        **{
            "number.example_manager_manual_power": {"state": "0", "attributes": {}},
            "sensor.example_device_grid_input": {"state": "800", "attributes": {}},  # stale leftover
            "sensor.example_device_output_home": {"state": "unavailable", "attributes": {}},
        }
    )
    idle._device = None
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), idle)
    assert driver.read_state().power_w == 0.0  # the negated setpoint, not 800 - 0

    discharging = manager_ha(
        **{
            "number.example_manager_manual_power": {"state": "500", "attributes": {}},  # discharge
            "sensor.example_device_grid_input": {"state": "0", "attributes": {}},
            "sensor.example_device_output_home": {"state": "unavailable", "attributes": {}},
        }
    )
    discharging._device = None
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), discharging)
    assert driver.read_state().power_w == -500.0  # a real discharge must not read as 0 W


def test_zendure_hold_never_treats_a_lagging_measurement_as_idempotent():
    """Audit finding 1 repro: operation=manual, manual_power=-800, measured 0/0.

    The manager applies the setpoint on the next P1 event, so the measured pair still reads 0/0
    right after a real charge command.  A hold that trusts it returns ``idempotent_skip`` without
    writing anything and the stale -800 W charge continues.
    """
    ha = manager_ha(
        **{
            "select.example_manager_operation": {"state": "manual", "attributes": {}},
            "number.example_manager_manual_power": {"state": "-800", "attributes": {}},
            "sensor.example_device_grid_input": {"state": "0", "attributes": {}},
            "sensor.example_device_output_home": {"state": "0", "attributes": {}},
        }
    )
    ha._device = None  # the P1 event has not been delivered yet
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    result = driver.hold(dry_run=False)
    assert result.code != Reason.IDEMPOTENT_SKIP, result
    assert result.ok is True, result
    assert ha.calls, "a hold must issue a real write"
    assert ha.states["number.example_manager_manual_power"]["state"] in ("0", "0.0")
    assert ha.states["select.example_manager_operation"]["state"] == "manual"
    # the raw setpoint write is the signed zero, and the commanded value is verified
    assert result.writes and result.writes[-1]["entity_id"] == "number.example_manager_manual_power"


def test_zendure_hold_always_writes_even_when_everything_reads_zero():
    """A hold is the safety path: it must never be skipped on the strength of measured flow."""
    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    result = driver.hold(dry_run=False)
    assert result.ok is True
    assert ha.calls, "hold() must issue at least one service call"
    assert any(
        call[2].get("entity_id") == "number.example_manager_manual_power" for call in ha.calls
    )


def test_zendure_serialises_the_manager_path_and_negation_in_capabilities():
    ha = manager_ha()
    driver = registry.create(config(driver="zendure", mapping=manager_mapping()), ha)
    caps = driver.capabilities()
    assert caps["manager"] == "zendure_ha"
    assert caps["control_path"] == "manager_manual_power"
    assert "positive = discharge" in caps["raw_sign_convention"]
    assert caps["operation_value"] == "manual"
    assert set(caps) and all(not isinstance(value, dict) or key == "entities" for key, value in caps.items())


def test_zendure_module_contains_no_instance_entity_ids():
    """No *string literal* in the driver may look like an entity id (comments may use placeholders)."""
    import ast
    import re

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "energy_manager",
        "app",
        "control",
        "drivers",
        "zendure.py",
    )
    source = open(path, encoding="utf-8").read()
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    pattern = re.compile(r"\b(?:sensor|binary_sensor|number|select|input_number|input_select)\.[a-z0-9_]+")
    offenders = [text for text in literals if pattern.search(text)]
    assert offenders == []
    assert any("zendure_ha" in text for text in literals)  # the platform name is generic


def test_zendure_validate_mapping_requires_power_entities():
    problems = registry.validate_config(config(driver="zendure", mapping={"soc_entity": "sensor.x"}))
    assert any("zendure" in problem for problem in problems)
    problems = registry.validate_config(
        config(
            driver="zendure",
            mapping={
                "manual_power_entity": "number.example_manager_manual_power",
                "operation_entity": "sensor.example_not_a_select",
            },
        )
    )
    assert any("operation_entity must be a select" in problem for problem in problems)
    problems = registry.validate_config(
        config(driver="zendure", mapping={"manual_power_entity": "number.example_manager_manual_power"})
    )
    assert any("operation_entity" in problem for problem in problems)
    # the generic driver still validates its own roles
    assert registry.validate_config(config(mapping={"power_setpoint_entity": "number.example_power"})) == []


# ---------------------------------------------------------------------------- dispatcher


def dispatcher_with(ha, cfg, journal=None, settings=None, driver=None):
    driver = driver or registry.create(cfg, ha)
    return Dispatcher(settings or SETTINGS, journal or FakeJournal(), {cfg.id: driver}), driver


def test_dispatcher_advisory_logs_and_never_writes():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    cfg = config()
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    state = system_state(mode=Mode.ADVISORY.value)
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan(), batteries=[cfg])
    assert outcome.block is True
    assert reason_is(outcome.reasons, Reason.MODE_NOT_AUTO)
    assert ha.calls == []
    assert len(journal.dispatches) == 1
    assert journal.dispatches[0]["code"] == Reason.MODE_NOT_AUTO
    assert journal.dispatches[0]["result"] == "ok"  # not an error, just a refusal
    assert outcome.results[0].applied_w == 0.0


def test_dispatcher_dry_run_logs_would_be_writes_without_io():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    cfg = config()
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    state = system_state(dry_run=True)
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan(), batteries=[cfg])
    assert ha.calls == []
    assert outcome.results[0].dry_run is True
    assert outcome.results[0].applied_w == 600.0
    assert journal.dispatches[0]["result"] == "dry_run"
    assert journal.dispatches[0]["writes"]


def test_dispatcher_auto_writes_the_allocated_power():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    cfg = config()
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    state = system_state(dry_run=False)
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan(), batteries=[cfg])
    assert outcome.block is False
    assert ha.calls and ha.calls[0][0] == "number"
    assert ha.calls[0][2]["value"] == 600.0
    assert journal.dispatches[0]["result"] == "ok"
    assert journal.dispatches[0]["applied_w"] == 600.0


def test_dispatcher_ramps_from_the_live_power():
    ha = FakeHA({"number.example_power": {"state": "1000", "attributes": {}}})
    cfg = config(eta_charge=1.0, eta_discharge=1.0)
    journal = FakeJournal()
    dispatcher, driver = dispatcher_with(ha, cfg, journal)
    live = driver.read_state()
    assert live.power_w == 1000.0
    state = system_state(dry_run=False, batteries=[live])
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan([plan_slot(-2000.0)]), batteries=[cfg])
    # 1000 W -> at most one ramp step, so this tick can only reach 0 W
    assert outcome.allowed_w == 0.0
    assert outcome.results[0].applied_w == 0.0
    assert ha.calls[-1][2]["value"] == 0.0


def test_dispatcher_grid_follow_deadband_ratio_and_application():
    cfg = config(max_charge_w=2000.0)
    slot = plan_slot(600.0, load_kw=0.5, pv_kw=0.0)
    plan = make_plan([slot])

    # small deviation: inside the deadband, no correction
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    dispatcher, _ = dispatcher_with(ha, cfg, FakeJournal())
    outcome = dispatcher.tick(NOW, state=system_state(), plan=plan, batteries=[cfg], measurements={"grid_w": 1120.0})
    assert abs(outcome.grid_follow_w) < 1e-9

    # large deviation: corrected up to the independent watt budget (0.5 x capability), never
    # discarded just because it is big (audit finding 11)
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    dispatcher, _ = dispatcher_with(ha, cfg, FakeJournal())
    outcome = dispatcher.tick(NOW, state=system_state(), plan=plan, batteries=[cfg], measurements={"grid_w": 3000.0})
    assert abs(outcome.grid_follow_w + 1000.0) < 1e-6  # 1900 W import: discharge at the budget

    # moderate deviation: applied
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    dispatcher, _ = dispatcher_with(ha, cfg, FakeJournal())
    outcome = dispatcher.tick(NOW, state=system_state(), plan=plan, batteries=[cfg], measurements={"grid_w": 1300.0})
    assert abs(outcome.grid_follow_w + 200.0) < 1e-6
    # planned grid = 0.5 + 0.6 = 1.1 kW; measured 1.3 kW -> charge 200 W less
    assert abs(outcome.results[0].applied_w - 400.0) < 1e-6


def test_dispatcher_grid_follow_corrects_a_zero_target():
    """Audit finding 11: with a planned 0 W the old ratio rule could never correct anything."""
    cfg = config(
        max_charge_w=2000.0,
        max_discharge_w=2000.0,
        mapping={
            "power_setpoint_entity": "number.example_power",
            "soc_entity": "sensor.example_charge",
        },
    )
    plan = make_plan([plan_slot(0.0, load_kw=0.5, pv_kw=0.0)])
    ha = FakeHA(
        {
            "number.example_power": {"state": "0", "attributes": {}},
            "sensor.example_charge": {"state": "50", "attributes": {"unit_of_measurement": "%"}},
        }
    )
    dispatcher, _ = dispatcher_with(ha, cfg, FakeJournal())
    outcome = dispatcher.tick(
        NOW, state=system_state(), plan=plan, batteries=[cfg], measurements={"grid_w": 800.0}
    )
    # planned grid 0.5 kW, measured 0.8 kW -> discharge 300 W to close the gap
    assert abs(outcome.grid_follow_w + 300.0) < 1e-6
    assert outcome.results and outcome.results[0].applied_w < 0.0
    # the explicit override wins even over a zero plan
    checked = dispatcher._grid_follow(0.0, plan.slots[0], {"grid_w": 800.0}, system_state(), 2000.0)
    assert abs(checked + 300.0) < 1e-6


def test_override_pinning_respects_the_pinned_batterys_own_floor():
    """Audit finding 8: a pinned override must not spend headroom the battery does not have."""
    full = config(
        id="a_full",
        capacity_kwh=5.0,
        max_discharge_w=2000.0,
        mapping={"power_setpoint_entity": "number.example_full"},
    )
    low = config(
        id="b_low",
        capacity_kwh=0.55,
        max_discharge_w=2000.0,
        mapping={"power_setpoint_entity": "number.example_low"},
    )

    class FixedDriver:
        """Reports a fixed live state and records the requested power."""

        def __init__(self, cfg, state):
            self.config = cfg
            self._state = state

        def id(self):
            return self.config.id

        def read_state(self):
            return self._state

        def commanded_power(self):
            return 0.0

        def hold(self, *, dry_run):
            return DispatchResult(
                battery_id=self.config.id,
                ts=NOW,
                requested_w=0.0,
                applied_w=0.0,
                ok=True,
                dry_run=dry_run,
                code=Reason.OK,
            )

        def apply(self, power_w, *, dry_run):
            return DispatchResult(
                battery_id=self.config.id,
                ts=NOW,
                requested_w=power_w,
                applied_w=power_w,
                ok=True,
                dry_run=dry_run,
                code=Reason.OK,
            )

    at_reserve = BatteryState(
        id="b_low",
        available=True,
        soc_pct=30.0,  # exactly at its own 30 % reserve floor
        capacity_kwh=0.55,
        energy_kwh=0.165,
        power_w=0.0,
        read_at=NOW,
    )
    full_state = BatteryState(
        id="a_full",
        available=True,
        soc_pct=100.0,
        capacity_kwh=5.0,
        energy_kwh=5.0,
        power_w=0.0,
        read_at=NOW,
    )
    drivers = {"a_full": FixedDriver(full, full_state), "b_low": FixedDriver(low, at_reserve)}
    settings = {**SETTINGS, "optimizer.reserve_soc_pct": 30.0}
    dispatcher = Dispatcher(settings, FakeJournal(), drivers)

    def pinned_to(battery_id):
        return system_state(
            override={
                "power_w": -2000.0,
                "battery_id": battery_id,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
        )

    outcome = dispatcher.tick(
        NOW, state=pinned_to("b_low"), plan=make_plan(), batteries=[full, low]
    )
    applied = {r.battery_id: r.requested_w for r in outcome.results}
    assert applied["b_low"] == 0.0, "the low battery is at its own reserve floor"
    assert applied["a_full"] == 0.0, "an override pins one battery; the other stays at zero"
    # pinning the full battery still works, within its own headroom
    outcome = dispatcher.tick(
        NOW, state=pinned_to("a_full"), plan=make_plan(), batteries=[full, low]
    )
    applied = {r.battery_id: r.requested_w for r in outcome.results}
    assert applied["b_low"] == 0.0
    assert applied["a_full"] < 0.0


def test_a_plan_older_than_the_bound_is_refused_as_stale_input():
    """Audit finding 2: an old plan must not keep steering the hardware (SPEC 9.1).

    The bound is ``max(2 x control.replan_minutes, control.stale_plan_seconds)``.
    """
    cfg = config()
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    journal = FakeJournal()

    def slot_at(minutes: int) -> PlanSlot:
        return PlanSlot(
            t=NOW + timedelta(minutes=minutes),
            buy=0.2,
            sell=0.05,
            pv_kw=0.0,
            load_kw=0.5,
            soc_kwh=2.5,
            power_w=600.0,
            action=Action.CHARGE,
            reason=Reason.ARBITRAGE_CHARGE,
        )

    plan = make_plan([slot_at(0), slot_at(15), slot_at(30)])
    dispatcher, _driver = dispatcher_with(ha, cfg, journal)
    # a plan from the current replan cycle still executes
    outcome = dispatcher.tick(
        NOW + timedelta(minutes=10), state=system_state(), plan=plan, batteries=[cfg]
    )
    assert outcome.stale_input is None
    assert outcome.plan_power_w == 600.0
    ha.calls.clear()
    ha.states["number.example_power"] = {"state": "0", "attributes": {}}  # device at rest
    # past the bound it is refused instead of dispatched
    outcome = dispatcher.tick(
        NOW + timedelta(minutes=31), state=system_state(), plan=plan, batteries=[cfg]
    )
    assert outcome.stale_input is not None and "min old" in outcome.stale_input
    assert Reason.STALE_INPUT in outcome.reasons
    assert outcome.plan_power_w is None, "a refused plan must not report a power to follow"
    assert ha.calls == [], "a stale plan must never reach the hardware"
    # control.stale_plan_seconds widens the bound when configured
    dispatcher, _driver = dispatcher_with(
        ha, cfg, journal, settings={**SETTINGS, "control.stale_plan_seconds": 7200}
    )
    outcome = dispatcher.tick(
        NOW + timedelta(minutes=31), state=system_state(), plan=plan, batteries=[cfg]
    )
    assert outcome.stale_input is None
    assert outcome.plan_power_w == 600.0


def test_driver_freshness_is_the_read_moment_and_freezes_are_detected_across_polls():
    """Audit finding 1: an idle battery must never be blocked, a frozen integration must be.

    HA only advances ``last_updated`` when a state changes, so a battery sitting at hold keeps an
    old timestamp forever.  ``read_at`` is therefore the moment of a successful read, and a frozen
    integration is detected across polls: only when a commanded setpoint is outstanding and the
    measurement never follows it does the reading turn stale.
    """
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    old_stamp = "2026-01-01T11:30:00.000000+00:00"
    cfg = config(
        mapping={
            "power_setpoint_entity": "number.example_power",
            "soc_entity": "sensor.example_charge",
        }
    )
    state = FakeHAState(
        {
            "number.example_power": {"state": "0", "attributes": {}},
            "sensor.example_charge": {
                "state": "50",
                "attributes": {"unit_of_measurement": "%"},
            },
        }
    )
    state.states["number.example_power"]["last_updated"] = old_stamp
    state.states["sensor.example_charge"]["last_updated"] = old_stamp
    driver = registry.create(cfg, FakeHAFacade(state), clock=lambda: now)
    for _ in range(5):  # many polls, all with the same (old) change timestamps
        reading = driver.read_state()
        assert reading.stale is False, "an idle battery must stay usable"
        assert reading.age_seconds(now) == pytest.approx(0.0, abs=1.0)
        assert reading.extra["timestamps_seen"] is True
    # a non-zero command the device never follows: the same polls now report a frozen reading
    state.never_apply.add("number.example_power")
    driver.apply(800.0, dry_run=False)
    frozen = None
    for _ in range(5):
        frozen = driver.read_state()
    assert frozen is not None and frozen.stale is True
    assert "does not follow" in str(frozen.last_error)
    assert frozen.extra["frozen"] is True
    # ...and the safety layer holds instead of commanding on a frozen measurement
    decision = safety.check(
        1000.0,
        plan_slot(),
        [cfg],
        system_state(ha_connected=True, batteries=[frozen]),
        SETTINGS,
        now,
    )
    assert decision.block is True
    assert Reason.STALE_STATE in decision.reasons
    assert decision.allowed_w == 0.0


def test_the_fake_ha_advances_timestamps_only_on_change():
    """The shared fake must model HA: ``last_updated`` moves only when the value changes."""
    entity = "number.example_power"
    state = FakeHAState({entity: {"state": "0.0", "attributes": {}}})
    first = state.get_state(entity)["last_updated"]
    state.call_service("number", "set_value", {"entity_id": entity, "value": 0.0})
    assert state.get_state(entity)["last_updated"] == first, "an unchanged value must not refresh"
    state.call_service("number", "set_value", {"entity_id": entity, "value": 500.0})
    changed = state.get_state(entity)["last_updated"]
    assert changed != first
    state.call_service("number", "set_value", {"entity_id": entity, "value": 500.0})
    assert state.get_state(entity)["last_updated"] == changed


class StubDriver:
    """Driver stand-in that reports a fixed state and refuses to be written to."""

    def __init__(self, cfg, state):
        self.config = cfg
        self._state = state

    def id(self):
        return self.config.id

    def read_state(self):
        return self._state

    def apply(self, power_w, *, dry_run):
        raise AssertionError("an excluded battery must not be written to")

    def hold(self, *, dry_run):
        raise AssertionError("an excluded battery must not be written to")

    def capabilities(self):
        return {}


def test_dispatcher_logs_unavailable_battery_in_auto():
    cfg = config()
    ha = FakeHA({"number.example_power": {"state": "unavailable", "attributes": {}}})
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    outcome = dispatcher.tick(NOW, state=system_state(), plan=make_plan(), batteries=[cfg])
    assert ha.calls == []
    assert outcome.results and outcome.results[0].code == Reason.BATTERY_UNAVAILABLE
    assert journal.dispatches and journal.dispatches[0]["code"] == Reason.BATTERY_UNAVAILABLE


def test_dispatcher_logs_excluded_battery_in_auto():
    cfg = config()
    journal = FakeJournal()
    stale = BatteryState(
        id=cfg.id,
        available=True,
        soc_pct=50.0,
        power_w=0.0,
        energy_kwh=2.5,
        read_at=NOW - timedelta(seconds=600),
    )
    dispatcher = Dispatcher(SETTINGS, journal, {cfg.id: StubDriver(cfg, stale)})
    outcome = dispatcher.tick(NOW, state=system_state(), plan=make_plan(), batteries=[cfg])
    assert outcome.results and outcome.results[0].code == Reason.STALE_STATE
    assert outcome.results[0].applied_w == 0.0
    assert journal.dispatches[0]["code"] == Reason.STALE_STATE


def test_dispatcher_watchdog_holds_everything():
    ha = FakeHA({"number.example_power": {"state": "500", "attributes": {}}})
    cfg = config()
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    dispatcher.note_tick(NOW - timedelta(seconds=600))
    assert dispatcher.stalled(NOW) is True
    results = dispatcher.watchdog(NOW)
    assert results and results[0].applied_w == 0.0
    assert ha.calls and ha.calls[-1][2]["value"] == 0.0
    assert any(code == "watchdog_hold" for _, code, _, _ in journal.events)


def test_dispatcher_needs_replan_reasons():
    cfg = config()
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    dispatcher, _ = dispatcher_with(ha, cfg)
    healthy = BatteryState(
        id=cfg.id, available=True, soc_pct=50.0, power_w=0.0, energy_kwh=2.5, read_at=NOW
    )
    state = system_state(batteries=[healthy])
    assert dispatcher.needs_replan(state, None, [cfg], NOW) == "no_plan"
    fresh = make_plan()
    assert dispatcher.needs_replan(state, fresh, [cfg], NOW) is None
    old = Plan(
        generated_at=NOW - timedelta(hours=2),
        horizon_start=NOW - timedelta(hours=2),
        horizon_end=NOW + timedelta(hours=1),
        slot_minutes=15,
        slots=[plan_slot()],
    )
    assert dispatcher.needs_replan(state, old, [cfg], NOW) == "plan_age"
    deviating = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(minutes=15),
        slot_minutes=15,
        slots=[PlanSlot(t=NOW, buy=0.2, sell=0.05, pv_kw=0.0, load_kw=0.5, soc_kwh=5.0, power_w=0.0,
                        action=Action.HOLD, reason=Reason.SPREAD_BELOW_THRESHOLD)],
    )
    state.batteries = [
        BatteryState(id=cfg.id, available=True, soc_pct=10.0, energy_kwh=0.5, read_at=NOW)
    ]
    assert dispatcher.needs_replan(state, deviating, [cfg], NOW) == "soc_deviation"


def test_dispatcher_interpolates_across_a_slot_boundary():
    cfg = config(eta_charge=1.0, eta_discharge=1.0)
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    dispatcher, _ = dispatcher_with(ha, cfg)
    previous = plan_slot(0.0)
    current = plan_slot(1000.0)
    current.t = NOW + timedelta(minutes=15)
    plan = make_plan([previous, current])
    # 60 s into the boundary change -> half way between 0 and 1000 W
    outcome = dispatcher.tick(
        current.t + timedelta(seconds=60),
        state=system_state(),
        plan=plan,
        batteries=[cfg],
    )
    assert 400.0 <= outcome.target_w <= 600.0


def test_dispatcher_two_batteries_split_and_count_writes():
    cfg_a = config(id="a", mapping={"power_setpoint_entity": "number.example_power_a"})
    cfg_b = config(id="b", mapping={"power_setpoint_entity": "number.example_power_b"})
    ha = FakeHA(
        {
            "number.example_power_a": {"state": "0", "attributes": {}},
            "number.example_power_b": {"state": "0", "attributes": {}},
        }
    )
    journal = FakeJournal()
    dispatcher = Dispatcher(
        SETTINGS,
        journal,
        {"a": registry.create(cfg_a, ha), "b": registry.create(cfg_b, ha)},
    )
    state = system_state()
    outcome = dispatcher.tick(NOW, state=state, plan=make_plan([plan_slot(1000.0)]), batteries=[cfg_a, cfg_b])
    assert len(outcome.results) == 2
    values = sorted(call[2]["value"] for call in ha.calls)
    assert values == [500.0, 500.0]
    assert len(journal.dispatches) == 2


def reason_is(reasons, expected):
    return expected in reasons


def test_driver_errors_fall_back_to_a_hold():
    cfg = config()
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    ha.fail_on = {"number.example_power"}
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    outcome = dispatcher.tick(NOW, state=system_state(), plan=make_plan(), batteries=[cfg])
    assert outcome.results[0].ok is False
    assert outcome.results[0].code == Reason.DRIVER_ERROR
    assert [entry["code"] for entry in journal.dispatches].count(Reason.DRIVER_ERROR) >= 1
    assert any(code == "driver_error" for _, code, _, _ in journal.events)


def test_driver_error_helper_is_reported_as_event():
    cfg = config()
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    ha.fail_on = {"number.example_power"}
    journal = FakeJournal()
    dispatcher, _ = dispatcher_with(ha, cfg, journal)
    dispatcher.tick(NOW, state=system_state(), plan=make_plan(), batteries=[cfg])
    events = [code for _, code, _, _ in journal.events]
    assert "driver_error" in events


def test_driver_protocol_shapes():
    ha = FakeHA({"number.example_power": {"state": "0", "attributes": {}}})
    driver = registry.create(config(), ha)
    assert callable(driver.id) and callable(driver.read_state)
    assert callable(driver.apply) and callable(driver.hold) and callable(driver.capabilities)
    caps = driver.capabilities()
    for key in ("driver", "max_charge_w", "max_discharge_w", "mapping_resolved", "signed"):
        assert key in caps
    hold = driver.hold(dry_run=True)
    assert hold.requested_w == 0.0


def test_driver_error_class_is_reachable():
    assert issubclass(DriverError, RuntimeError)
