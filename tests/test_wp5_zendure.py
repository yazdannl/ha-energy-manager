"""WP5 Zendure driver tests against the fake HA (SPEC 9.2, appendix A).

The SolarFlow manager is the highest-risk write path:
* ``manual_power`` uses the opposite sign of ``models.py`` (negative = charge);
* ``operation`` must be ``manual`` before the power write and re-asserted if a smart mode grabs
  it back during control;
* the setpoint only becomes visible on the next P1 event, so a delayed read-back inside the
  documented window is fine while a permanent mismatch is a driver error;
* the per-device ``input_limit``/``output_limit`` entities must never be written while the
  manager path works (they fight the manager).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.control.drivers.zendure import MANAGER_SIGN, ZendureDriver  # noqa: E402
from app.models import BatteryConfig, Reason  # noqa: E402
from fake_ha import FakeHAFacade, FakeHAState  # noqa: E402

MANUAL = "number.example_manager_manual_power"
OPERATION = "select.example_manager_operation"
GRID_IN = "sensor.example_device_grid_input"
OUTPUT = "sensor.example_device_output_home"
SOC = "sensor.example_device_electric_level"
AC_MODE = "select.example_device_ac_mode"
INPUT_LIMIT = "number.example_device_input_limit"
OUTPUT_LIMIT = "number.example_device_output_limit"

MAPPING = {
    "manual_power_entity": MANUAL,
    "operation_entity": OPERATION,
    "operation_manual_value": "manual",
    "soc_entity": SOC,
    "grid_input_power_entity": GRID_IN,
    "output_power_entity": OUTPUT,
    "charge_limit_entity": INPUT_LIMIT,
    "discharge_limit_entity": OUTPUT_LIMIT,
    "ac_mode_entity": AC_MODE,
}


def make_state() -> FakeHAState:
    state = FakeHAState(
        {
            MANUAL: {"state": "0", "attributes": {}},
            OPERATION: {"state": "smart", "attributes": {}},
            GRID_IN: {"state": "0", "attributes": {}},
            OUTPUT: {"state": "0", "attributes": {}},
            SOC: {"state": "50", "attributes": {"unit_of_measurement": "%"}},
            INPUT_LIMIT: {"state": "0", "attributes": {}},
            OUTPUT_LIMIT: {"state": "0", "attributes": {}},
            AC_MODE: {"state": "output", "attributes": {"options": ["input", "output"]}},
        }
    )
    state.link_manager(MANUAL, GRID_IN, OUTPUT)
    return state


def make_driver(state: FakeHAState, control_path: str = "auto", **overrides) -> ZendureDriver:
    config = BatteryConfig(
        id="example_zendure",
        name="Example Zendure",
        driver="zendure",
        capacity_kwh=2.4,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=800.0,
        max_discharge_w=800.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        mapping=dict(MAPPING),
        control_path=control_path,
    )
    return ZendureDriver(
        config,
        FakeHAFacade(state),
        sleep=lambda _seconds: None,
        **overrides,
    )


def test_charge_asserts_manual_before_and_writes_negated_raw():
    state = make_state()
    driver = make_driver(state)
    result = driver.apply(800.0, dry_run=False)

    assert result.ok is True, result.message
    assert result.applied_w == pytest.approx(800.0)
    assert result.code == Reason.OK
    calls = state.service_calls
    assert calls, "expected service calls"
    # manual dispatch first, the (sign-inverted, system-wide) setpoint second
    assert calls[0][0] == "select" and calls[0][2]["entity_id"] == OPERATION
    assert calls[0][2]["option"] == "manual"
    power_calls = [call for call in calls if call[2].get("entity_id") == MANUAL]
    assert power_calls, calls
    assert power_calls[0][2]["value"] == pytest.approx(MANAGER_SIGN * 800.0)
    assert power_calls[0][2]["value"] == pytest.approx(-800.0)
    # read-back resolved through the measured pair
    assert result.readback_w == pytest.approx(800.0)
    assert state.states[GRID_IN]["state"] == "800.0"
    assert state.states[OUTPUT]["state"] == "0.0"


def test_manual_assertion_is_repeated_when_a_smart_mode_grabs_the_select():
    state = make_state()
    order: list[str] = []

    def hook(domain, service, data):
        entity = data.get("entity_id")
        order.append(entity)
        if entity == MANUAL:
            # the integration's smart mode overwrites during/after the power write
            state.states[OPERATION] = {"state": "smart", "attributes": {}}

    state.service_hook = hook
    driver = make_driver(state)
    result = driver.apply(500.0, dry_run=False)

    assert result.ok is True, result.message
    # operation asserted, power written, operation re-asserted after it
    assert order == [OPERATION, MANUAL, OPERATION], order
    assert state.states[OPERATION]["state"] == "manual"
    assert state.states[MANUAL]["state"] == "-500.0"


def test_smart_mode_that_never_lets_go_reports_driver_error_never_ok():
    state = make_state()

    def hook(domain, service, data):
        if data.get("entity_id") == OPERATION:
            state.states[OPERATION] = {"state": "smart", "attributes": {}}

    state.service_hook = hook
    driver = make_driver(state)
    result = driver.apply(600.0, dry_run=False)

    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert "manual" in result.message
    assert result.code != Reason.OK


def test_delayed_p1_readback_inside_the_window_is_tolerated():
    state = make_state()
    state.apply_after_reads[MANUAL] = 2  # visible only after two measured reads
    driver = make_driver(state)
    result = driver.apply(800.0, dry_run=False)

    assert result.ok is True, result.message
    assert result.code == Reason.OK
    assert result.readback_w == pytest.approx(800.0)


def test_permanent_mismatch_is_reported_as_driver_error():
    state = make_state()
    state.never_apply.add(MANUAL)
    driver = make_driver(state)
    result = driver.apply(800.0, dry_run=False)

    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert "read-back" in result.message
    assert result.applied_w == 0.0


def test_manager_path_never_writes_the_device_limits():
    state = make_state()
    driver = make_driver(state)
    for target in (800.0, -800.0, 0.0, 250.0):
        driver.apply(target, dry_run=False)
    driver.hold(dry_run=False)
    written = {call[2].get("entity_id") for call in state.service_calls}
    assert INPUT_LIMIT not in written
    assert OUTPUT_LIMIT not in written
    # every power write went to the manager entity
    assert MANUAL in written


def test_manager_power_is_read_back_with_the_models_sign_convention():
    state = make_state()
    state.states[MANUAL] = {"state": "-640", "attributes": {}}
    state.states[GRID_IN] = {"state": "0", "attributes": {}}
    state.states[OUTPUT] = {"state": "0", "attributes": {}}
    driver = make_driver(state)
    # no measured entities this time: fall back to the manual setpoint and negate it
    state.states.pop(GRID_IN)
    state.states.pop(OUTPUT)
    assert driver.read_state().power_w == pytest.approx(640.0)

    state.states[GRID_IN] = {"state": "0", "attributes": {}}
    state.states[OUTPUT] = {"state": "0", "attributes": {}}
    state.states[MANUAL] = {"state": "0", "attributes": {}}
    discharge = driver.apply(-600.0, dry_run=False)
    assert discharge.ok, discharge.message
    assert state.states[MANUAL]["state"] == "600.0"
    assert state.states[OUTPUT]["state"] == "600.0"  # discharging shows on output_home


# -- device path: the inverter's own AC limits, no manager, no P1 event ---------------------


def test_device_path_writes_direction_and_limits_and_quiets_the_manager():
    """control_path='device': ac_mode + input_limit/output_limit, and the manager put off.

    The inverter applies those properties itself over zenSDK/MQTT, so the command does not wait
    for the manager's next P1 event -- which is the whole point of this path.  The manager is
    taken out of the loop first, because while it is not ``off`` it re-writes the same two
    properties on every P1 event (live 2026-10-12: a 5-minute P1 feed made the manager path
    unusable).
    """
    state = make_state()
    driver = make_driver(state, control_path="device")
    assert driver.capabilities()["control_path"] == "device_limits"

    result = driver.apply(800.0, dry_run=False)
    assert result.ok is True, result.message
    assert result.applied_w == pytest.approx(800.0)
    assert result.code == Reason.OK  # the limits echo back, so this is a real confirmation

    order = [call[2].get("entity_id") for call in state.service_calls]
    assert order[:4] == [OPERATION, AC_MODE, INPUT_LIMIT, OUTPUT_LIMIT], order
    assert state.states[OPERATION]["state"] == "off"
    assert state.states[AC_MODE]["state"] == "input"
    assert state.states[INPUT_LIMIT]["state"] == "800.0"
    assert state.states[OUTPUT_LIMIT]["state"] == "0.0"
    # the device-path battery never touches the manager's manual setpoint
    assert MANUAL not in order


def test_device_path_discharge_uses_output_mode_and_output_limit():
    state = make_state()
    state.states[AC_MODE] = {"state": "input", "attributes": {"options": ["input", "output"]}}
    driver = make_driver(state, control_path="device")
    result = driver.apply(-500.0, dry_run=False)
    assert result.ok is True, result.message
    assert state.states[AC_MODE]["state"] == "output"
    assert state.states[OUTPUT_LIMIT]["state"] == "500.0"
    assert state.states[INPUT_LIMIT]["state"] == "0.0"


def test_device_path_hold_zeroes_both_limits_without_touching_the_direction():
    state = make_state()
    driver = make_driver(state, control_path="device")
    driver.apply(800.0, dry_run=False)
    direction_writes = len([call for call in state.service_calls if call[2].get("entity_id") == AC_MODE])
    result = driver.hold(dry_run=False)
    assert result.ok is True, result.message
    assert result.applied_w == 0.0
    assert state.states[INPUT_LIMIT]["state"] == "0.0"
    assert state.states[OUTPUT_LIMIT]["state"] == "0.0"
    # a hold is a power write, not a direction change
    assert len([call for call in state.service_calls if call[2].get("entity_id") == AC_MODE]) == direction_writes


def test_device_path_reports_commanded_until_the_inverter_echoes():
    """A command the inverter has not reported back yet is pending, never a revoked command."""
    state = make_state()
    state.never_apply.add(INPUT_LIMIT)  # written, but the device never reports the property
    driver = make_driver(state, control_path="device")
    result = driver.apply(800.0, dry_run=False)
    assert result.ok is True, result.message
    assert result.code == Reason.COMMANDED
    assert result.applied_w == pytest.approx(800.0)
    assert result.readback_w == pytest.approx(0.0)
    assert "has not confirmed" in result.message
    # the write is still in force; only the echo is missing
    assert [call[2].get("entity_id") for call in state.service_calls][2:4] == [INPUT_LIMIT, OUTPUT_LIMIT]


def test_device_path_reads_the_direction_option_from_the_entity():
    """The integration localises the select labels, so the option comes from the entity itself."""
    state = make_state()
    state.states[AC_MODE] = {
        "state": "AC Output Mode",
        "attributes": {"options": ["AC Input Mode", "AC Output Mode"]},
    }
    driver = make_driver(state, control_path="device")
    result = driver.apply(600.0, dry_run=False)
    assert result.ok is True, result.message
    assert state.states[AC_MODE]["state"] == "AC Input Mode"


def test_control_path_device_is_refused_without_the_limit_entities():
    """SPEC 5: an unusable choice is refused at save time, not discovered at dispatch."""
    complete = BatteryConfig(
        id="example_zendure",
        name="Example Zendure",
        driver="zendure",
        capacity_kwh=2.4,
        mapping=dict(MAPPING),
        control_path="device",
    )
    assert ZendureDriver.validate_control_path(complete) == []
    missing = BatteryConfig(
        id="example_zendure",
        name="Example Zendure",
        driver="zendure",
        capacity_kwh=2.4,
        mapping={"manual_power_entity": MANUAL, "operation_entity": OPERATION},
        control_path="device",
    )
    problems = ZendureDriver.validate_control_path(missing)
    assert any("charge_limit_entity" in problem for problem in problems)
    assert any("discharge_limit_entity" in problem for problem in problems)

    manager_path = BatteryConfig(
        id="example_zendure",
        name="Example Zendure",
        driver="zendure",
        capacity_kwh=2.4,
        mapping={"charge_limit_entity": INPUT_LIMIT, "discharge_limit_entity": OUTPUT_LIMIT},
        control_path="manager",
    )
    assert any("manual_power_entity" in problem for problem in ZendureDriver.validate_control_path(manager_path))


def test_a_bogus_control_path_is_rejected_by_the_battery_config():
    config = BatteryConfig(
        id="example_zendure",
        name="Example Zendure",
        driver="zendure",
        capacity_kwh=2.4,
        mapping=dict(MAPPING),
        control_path="inverter-ish",
    )
    assert any("control_path" in problem for problem in config.validate())
