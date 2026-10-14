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

from app.control.drivers.zendure import (  # noqa: E402
    MANAGER_SIGN,
    ZendureDriver,
    ZendureMqttDriver,
    resolve_mqtt_mapping,
)
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


def test_power_measured_only_when_the_measured_pair_is_present():
    """0.13.0: the setpoint fallback is a command, so savings must not credit it as delivered energy."""
    state = make_state()
    driver = make_driver(state)
    state.states[GRID_IN] = {"state": "400", "attributes": {}}
    state.states[OUTPUT] = {"state": "0", "attributes": {}}
    measured = driver.read_state()
    assert measured.power_w == pytest.approx(400.0)
    assert measured.power_measured is True

    # Only the setpoint is readable (the measured pair went missing): unconfirmed, not creditable.
    state.states.pop(GRID_IN)
    state.states.pop(OUTPUT)
    state.states[MANUAL] = {"state": "-500", "attributes": {}}
    commanded = driver.read_state()
    assert commanded.power_w == pytest.approx(500.0)
    assert commanded.power_measured is False


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


def test_device_path_commanded_power_reports_the_limits_not_the_measured_flow():
    """0.17.1: a momentary 0 W measured flow is not proof that the command is neutral.

    The engine clears its applied-command record from ``commanded_power()``; on this path the
    command lives in ``input_limit``/``output_limit``, which the inverter echoes back, so those --
    never the measured pair -- are what it may report.
    """
    state = make_state()
    driver = make_driver(state, control_path="device")
    assert driver.apply(800.0, dry_run=False).ok is True
    assert driver.commanded_power() == pytest.approx(800.0)
    # the AC flow momentarily reads 0 W while the inverter's limits still execute the command
    state.states[GRID_IN] = {"state": "0", "attributes": {}}
    state.states[OUTPUT] = {"state": "0", "attributes": {}}
    assert driver._read_power() == 0.0
    assert driver.commanded_power() == pytest.approx(800.0)
    assert driver.hold(dry_run=False).ok is True
    assert driver.commanded_power() == 0.0


def test_device_path_hold_never_treats_the_measured_flow_as_proof():
    """0.21.1: a hold must neutralise the *limits*, even while the measured flow reads 0 W.

    The live 2026-10-14 shape: the arbitrage charge's down-ramp ended with ``input_limit = 153 W``
    and ``ac_mode = input``; the plan then held, and every tick was skipped as "already there"
    because a device-path mapping has no setpoint entity, so the pre-write check fell back to the
    measured pair (0 W).  The inverter stayed armed to charge from the grid for six hours and only
    the next process start -- an unconditional hold -- disarmed it.
    """
    state = make_state()
    state.states[AC_MODE] = {"state": "input", "attributes": {"options": ["input", "output"]}}
    state.states[INPUT_LIMIT] = {"state": "153", "attributes": {}}
    state.states[OUTPUT_LIMIT] = {"state": "0", "attributes": {}}
    state.states[GRID_IN] = {"state": "0", "attributes": {}}
    state.states[OUTPUT] = {"state": "0", "attributes": {}}
    driver = make_driver(state, control_path="device")
    assert driver._read_power() == 0.0  # the flow says idle
    assert driver.commanded_power() == pytest.approx(153.0)  # the armed limit says otherwise

    result = driver.apply(0.0, dry_run=False)
    assert result.code != Reason.IDEMPOTENT_SKIP, result.message
    assert result.ok is True, result.message
    assert state.states[INPUT_LIMIT]["state"] == "0.0"
    assert state.states[OUTPUT_LIMIT]["state"] == "0.0"


def test_device_path_unreadable_limits_fail_open_to_a_write():
    """An unreadable limit echo must never degrade to the measured pair (which can read 0 W)."""
    state = make_state()
    state.states[INPUT_LIMIT] = {"state": "unavailable", "attributes": {}}
    state.states[OUTPUT_LIMIT] = {"state": "unavailable", "attributes": {}}
    driver = make_driver(state, control_path="device")
    assert driver._read_power() == 0.0
    assert driver._idempotent_power() is None

    result = driver.apply(0.0, dry_run=False)
    assert result.code != Reason.IDEMPOTENT_SKIP, result.message
    written = [call[2].get("entity_id") for call in state.service_calls]
    assert INPUT_LIMIT in written and OUTPUT_LIMIT in written, written


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


# ------------------------------------------------------- MQTT bridge driver (zendure_mqtt)

MQTT_INPUT_LIMIT = "number.example_mqtt_inputlimit"
MQTT_OUTPUT_LIMIT = "number.example_mqtt_outputlimit"
MQTT_AC_MODE = "select.example_mqtt_acmode"
MQTT_SOC = "sensor.example_mqtt_electriclevel"
MQTT_GRID_IN = "sensor.example_mqtt_gridinputpower"
MQTT_OUTPUT = "sensor.example_mqtt_outputhomepower"
MQTT_PV = "sensor.example_mqtt_solarinputpower"
MQTT_SMART = "switch.example_mqtt_smartmode"
MQTT_MANAGER = "select.example_manager_operation"

MQTT_MAPPING = {
    "charge_limit_entity": MQTT_INPUT_LIMIT,
    "discharge_limit_entity": MQTT_OUTPUT_LIMIT,
    "ac_mode_entity": MQTT_AC_MODE,
    "soc_entity": MQTT_SOC,
    "grid_input_power_entity": MQTT_GRID_IN,
    "output_power_entity": MQTT_OUTPUT,
    "pv_power_entity": MQTT_PV,
    "smart_mode_entity": MQTT_SMART,
    "operation_entity": MQTT_MANAGER,
    "operation_off_value": "off",
}


def make_mqtt_state() -> FakeHAState:
    """One MQTT bridge device: the same properties the local MQTT feature publishes."""
    return FakeHAState(
        {
            MQTT_INPUT_LIMIT: {"state": "0", "attributes": {"min": 0, "max": 1000}},
            MQTT_OUTPUT_LIMIT: {"state": "0", "attributes": {"min": 0, "max": 800}},
            MQTT_AC_MODE: {
                "state": "Output mode",  # the last command was a discharge; a charge must flip it
                "attributes": {"options": ["Input mode", "Output mode"]},
            },
            MQTT_SOC: {
                "state": "40",
                "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
            },
            MQTT_GRID_IN: {"state": "0", "attributes": {"unit_of_measurement": "W"}},
            MQTT_OUTPUT: {"state": "0", "attributes": {"unit_of_measurement": "W"}},
            MQTT_PV: {"state": "120", "attributes": {"unit_of_measurement": "W"}},
            MQTT_SMART: {"state": "off", "attributes": {}},
            MQTT_MANAGER: {"state": "manual", "attributes": {"options": ["off", "manual", "smart"]}},
        }
    )


def make_mqtt_driver(state: FakeHAState, mapping: dict[str, str] | None = None) -> ZendureMqttDriver:
    config = BatteryConfig(
        id="example_mqtt",
        name="Example MQTT inverter",
        driver="zendure_mqtt",
        capacity_kwh=2.4,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_w=800.0,
        max_discharge_w=800.0,
        eta_charge=0.95,
        eta_discharge=0.95,
        mapping=dict(mapping or MQTT_MAPPING),
    )
    return ZendureMqttDriver(config, FakeHAFacade(state), sleep=lambda _seconds: None)


def test_mqtt_charge_quiets_the_manager_switches_smart_mode_and_writes_the_limits():
    """The bridge path: manager off, smartMode on, direction select, then the two magnitudes.

    The MQTT bridge publishes the device's own properties, so the limits are applied by the
    inverter itself (no P1 event involved) and its report of them is real evidence.  ``smartMode``
    joins the plan because the ``zendure_ha`` integration sets it to 1 with every non-zero AC
    command and the device only acts on the limits while it is set.
    """
    state = make_mqtt_state()
    driver = make_mqtt_driver(state)
    assert driver.capabilities()["control_path"] == "device_limits"
    assert driver.capabilities()["manager"] is None

    result = driver.apply(800.0, dry_run=False)
    assert result.ok is True, result.message
    assert result.code == Reason.OK  # the limits echo back from the device
    assert result.applied_w == pytest.approx(800.0)

    order = [call[2].get("entity_id") for call in state.service_calls]
    assert order[:4] == [MQTT_MANAGER, MQTT_SMART, MQTT_AC_MODE, MQTT_INPUT_LIMIT], order
    assert state.states[MQTT_MANAGER]["state"] == "off"
    assert state.states[MQTT_SMART]["state"] == "on"
    # the option string comes from the entity, not from a hardcoded "input"
    assert state.states[MQTT_AC_MODE]["state"] == "Input mode"
    assert float(state.states[MQTT_INPUT_LIMIT]["state"]) == pytest.approx(800.0)
    assert float(state.states[MQTT_OUTPUT_LIMIT]["state"]) == pytest.approx(0.0)
    # there is no manager setpoint on this path at all
    assert MANUAL not in order


def test_mqtt_discharge_uses_output_mode_and_output_limit():
    state = make_mqtt_state()
    driver = make_mqtt_driver(state)
    result = driver.apply(-600.0, dry_run=False)
    assert result.ok is True, result.message
    assert result.applied_w == pytest.approx(-600.0)
    assert state.states[MQTT_AC_MODE]["state"] == "Output mode"
    assert float(state.states[MQTT_OUTPUT_LIMIT]["state"]) == pytest.approx(600.0)
    assert float(state.states[MQTT_INPUT_LIMIT]["state"]) == pytest.approx(0.0)
    assert state.states[MQTT_SMART]["state"] == "on"


def test_mqtt_hold_zeroes_both_limits_and_switches_smart_mode_off():
    state = make_mqtt_state()
    state.write_state(MQTT_SMART, "on")
    state.write_state(MQTT_AC_MODE, "Output mode")
    driver = make_mqtt_driver(state)

    result = driver.hold(dry_run=False)
    assert result.ok is True, result.message
    assert float(state.states[MQTT_INPUT_LIMIT]["state"]) == pytest.approx(0.0)
    assert float(state.states[MQTT_OUTPUT_LIMIT]["state"]) == pytest.approx(0.0)
    assert state.states[MQTT_SMART]["state"] == "off"
    # a hold only zeroes the magnitudes: flipping the direction would be a pointless write
    assert MQTT_AC_MODE not in [call[2].get("entity_id") for call in state.service_calls]


def test_mqtt_writes_only_what_would_change():
    state = make_mqtt_state()
    state.write_state(MQTT_SMART, "on")
    driver = make_mqtt_driver(state)
    driver.apply(400.0, dry_run=False)
    calls = [call[2].get("entity_id") for call in state.service_calls]
    assert MQTT_SMART not in calls, "smartMode was already on"
    assert MQTT_MANAGER in calls, "the manager was still in manual"

    state.service_calls.clear()
    driver.apply(400.0, dry_run=False)
    calls = [call[2].get("entity_id") for call in state.service_calls]
    # The bridge publishes the device's own properties, and its report of the two limits is real
    # evidence (0.21.1): a target the echo already matches writes nothing at all -- neither the
    # manager/smartMode/direction nor the magnitudes are re-published.
    assert calls == [], calls


def test_mqtt_reports_commanded_until_the_device_echoes_the_limits():
    state = make_mqtt_state()
    state.never_apply.add(MQTT_INPUT_LIMIT)  # the bridge accepted nothing
    driver = make_mqtt_driver(state)

    result = driver.apply(800.0, dry_run=False)
    assert result.ok is True, result.message
    assert result.code == Reason.COMMANDED
    assert result.applied_w == pytest.approx(800.0)
    assert result.readback_w == pytest.approx(0.0)
    assert "not confirmed" in result.message


def test_mqtt_driver_is_refused_without_its_control_entities():
    problems = ZendureMqttDriver.validate_mapping({"charge_limit_entity": MQTT_INPUT_LIMIT})
    assert any("discharge_limit_entity" in problem for problem in problems)
    assert any("ac_mode_entity" in problem for problem in problems)

    problems = ZendureMqttDriver.validate_mapping({**MQTT_MAPPING, "ac_mode_entity": MQTT_SOC})
    assert any("ac_mode_entity must be a select" in problem for problem in problems)

    problems = ZendureMqttDriver.validate_mapping({**MQTT_MAPPING, "smart_mode_entity": MQTT_INPUT_LIMIT})
    assert any("smart_mode_entity must be a switch" in problem for problem in problems)

    problems = ZendureMqttDriver.validate_mapping({**MQTT_MAPPING, "charge_limit_entity": MQTT_AC_MODE})
    assert any("charge_limit_entity must be a number" in problem for problem in problems)


def test_mqtt_roles_are_resolved_from_the_property_names_of_one_device():
    """The bridge sets no translation key: the property name in the entity id is the role."""
    entries = [
        {"entity_id": "number.example_a_inputlimit", "platform": "mqtt", "device_id": "dev_a"},
        {"entity_id": "number.example_a_outputlimit", "platform": "mqtt", "device_id": "dev_a"},
        {"entity_id": "select.example_a_acmode", "platform": "mqtt", "device_id": "dev_a"},
        {"entity_id": "sensor.example_a_electriclevel", "platform": "mqtt", "device_id": "dev_a"},
        {"entity_id": "sensor.example_a_gridinputpower", "platform": "mqtt", "device_id": "dev_a"},
        {"entity_id": "switch.example_a_smartmode", "platform": "mqtt", "device_id": "dev_a"},
        # a pack-level reading carries no mapping key and must be left alone
        {"entity_id": "sensor.example_a_pack_soclevel", "platform": "mqtt", "device_id": "dev_a"},
        # another device of the same bridge, and another integration entirely
        {"entity_id": "number.example_b_inputlimit", "platform": "mqtt", "device_id": "dev_b"},
        {"entity_id": "number.example_c_input_limit", "platform": "zendure_ha", "device_id": "dev_c"},
    ]
    resolved = resolve_mqtt_mapping(entries, "dev_a")
    assert resolved == {
        "charge_limit_entity": "number.example_a_inputlimit",
        "discharge_limit_entity": "number.example_a_outputlimit",
        "ac_mode_entity": "select.example_a_acmode",
        "soc_entity": "sensor.example_a_electriclevel",
        "grid_input_power_entity": "sensor.example_a_gridinputpower",
        "smart_mode_entity": "switch.example_a_smartmode",
    }
    # without a device id the anchor entity picks the device (the stored SoC role)
    anchored = resolve_mqtt_mapping(entries, anchor_entity="sensor.example_a_electriclevel")
    assert anchored["charge_limit_entity"] == "number.example_a_inputlimit"
    # and a different device resolves only its own rows
    other = resolve_mqtt_mapping(entries, "dev_b")
    assert other == {"charge_limit_entity": "number.example_b_inputlimit"}


def test_mqtt_battery_is_validated_by_the_registry():
    from app.control import drivers as registry  # local import: the module list is what is under test

    config = BatteryConfig(
        id="example_mqtt",
        name="Example MQTT inverter",
        driver="zendure_mqtt",
        capacity_kwh=2.4,
        mapping=dict(MQTT_MAPPING),
    )
    assert registry.validate_config(config) == []
    assert registry.driver_class("zendure_mqtt") is ZendureMqttDriver
    assert registry.role_to_mapping("zendure_mqtt", "manual_mode") == "ac_mode_entity"
    assert registry.role_to_mapping("zendure_mqtt", "smart_mode") == "smart_mode_entity"

    broken = BatteryConfig(
        id="example_mqtt",
        name="Example MQTT inverter",
        driver="zendure_mqtt",
        capacity_kwh=2.4,
        mapping={"soc_entity": MQTT_SOC},
    )
    problems = registry.validate_config(broken)
    assert any("charge_limit_entity" in problem for problem in problems)
    assert any("ac_mode_entity" in problem for problem in problems)
