"""WP5 native Zendure driver tests: the device's own local zenSDK HTTP API (SPEC 5, 9.2).

A real stdlib ``http.server`` fake device stands in for the hardware, so the whole transport --
urllib request, JSON body, ``sn`` addressing, HTTP status handling, read-back echo -- is exercised
over loopback.  Nothing here touches real hardware, the network beyond 127.0.0.1, or a real
serial/token; the driver speaks the contract recorded in docs/EXTERNAL_APIS.md section 6 (native local API).

What these tests can *not* prove (the machine has no device): live firmware behaviour, the exact
per-model maxima, HTTPS certificates on a real device, and the undocumented 401/token path.  Those
are stated as assumptions in the docs, never asserted here as hardware facts.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import MASKED_SECRET, battery_payload, merge_mapping_patch  # noqa: E402
from app.control import drivers as driver_registry  # noqa: E402
from app.control.drivers.native import (  # noqa: E402
    ZendureNativeDriver,
    validate_connection,
)
from app.models import BatteryConfig, Reason  # noqa: E402

EXAMPLE_SERIAL = "EXAMPLE0000001"
EXAMPLE_TOKEN = "example-token"


class FakeZenDevice:
    """Mutable state one fake device's handler reads and writes."""

    def __init__(self, serial: str = EXAMPLE_SERIAL) -> None:
        self.serial = serial
        self.properties: dict[str, Any] = {
            "electricLevel": 55,
            "gridInputPower": 0,
            "outputHomePower": 0,
            "inputLimit": 0,
            "outputLimit": 0,
            "acMode": 2,
            "smartMode": 0,
            "solarInputPower": 120,
            "hyperTmp": 2931,
            "chargeMaxLimit": 1000,
            "inverseMaxPower": 800,
        }
        self.packs: list[dict[str, Any]] = [
            {"sn": "EXAMPLEPACK01", "minVol": 3310, "maxVol": 3350},
            {"sn": "EXAMPLEPACK02", "minVol": 3280, "maxVol": 3345},
        ]
        self.product = "SolarFlow Example"
        self.posts: list[dict[str, Any]] = []
        self.gets = 0
        self.mode = "ok"  # ok | ignore_write | malformed | array | empty_properties | serial_mismatch | http_500 | http_401
        self.delay_s = 0.0
        self.token_header: str | None = None

    def report(self) -> dict[str, Any]:
        serial = "EXAMPLEDIFFERENT1" if self.mode == "serial_mismatch" else self.serial
        properties = {} if self.mode == "empty_properties" else dict(self.properties)
        return {
            "timestamp": 1,
            "messageId": 1,
            "sn": serial,
            "product": self.product,
            "properties": properties,
            "packData": list(self.packs),
        }


def make_handler(device: FakeZenDevice):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # keep the test output quiet
            pass

        def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error_mode(self) -> bool:
            if device.mode == "http_500":
                self._send(500, b"boom")
                return True
            if device.mode == "http_401":
                self._send(401, b"unauthorized")
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802
            if device.delay_s:
                time.sleep(device.delay_s)
            device.gets += 1
            device.token_header = self.headers.get("Authorization")
            if self._error_mode():
                return
            if device.mode == "malformed":
                self._send(200, b"{not json")
                return
            if device.mode == "array":
                self._send(200, json.dumps([1, 2, 3]).encode())
                return
            self._send(200, json.dumps(device.report()).encode())

        def do_POST(self) -> None:  # noqa: N802
            if device.delay_s:
                time.sleep(device.delay_s)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            device.token_header = self.headers.get("Authorization")
            if self._error_mode():
                return
            if device.mode == "malformed":
                self._send(200, b"{not json")
                return
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, b'{"error": "bad json"}')
                return
            device.posts.append({"path": self.path, "body": body})
            if device.mode != "ignore_write":
                device.properties.update(body.get("properties") or {})
            self._send(200, json.dumps({"success": True, "code": 200}).encode())

    return Handler


@pytest.fixture
def device():
    dev = FakeZenDevice()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dev))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dev.port = int(server.server_address[1])
    try:
        yield dev
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class NoHomeAssistant:
    """A facade that fails loudly: the native driver must never read or write HA entities."""

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        raise AssertionError(f"native driver read HA entity {entity_id!r}")

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        raise AssertionError(f"native driver called HA service {domain}.{service}")

    def set_state(self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None) -> Any:
        raise AssertionError(f"native driver set HA state {entity_id!r}")


def mapping_for(device: FakeZenDevice, **overrides: str) -> dict[str, str]:
    mapping = {"host": "127.0.0.1", "port": str(device.port), "serial": EXAMPLE_SERIAL}
    mapping.update(overrides)
    return mapping


def config_for(device: FakeZenDevice, **overrides: Any) -> BatteryConfig:
    data: dict[str, Any] = {
        "id": "example_native",
        "name": "Example Native",
        "driver": "zendure_native",
        "capacity_kwh": 2.4,
        "min_soc_pct": 10.0,
        "max_soc_pct": 100.0,
        "max_charge_w": 800.0,
        "max_discharge_w": 800.0,
        "mapping": mapping_for(device),
    }
    data.update(overrides)
    return BatteryConfig(**data)


def driver_for(device: FakeZenDevice, **overrides: Any) -> ZendureNativeDriver:
    overrides.setdefault("readback_attempts", 2)
    overrides.setdefault("readback_delay_s", 0.0)
    return ZendureNativeDriver(
        config_for(device, **overrides.pop("config", {})),
        NoHomeAssistant(),
        sleep=lambda _seconds: None,
        **overrides,
    )


def free_port() -> int:
    """A port nothing listens on (bound, then released) for the unreachable-host tests."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- reads


def test_reads_soc_measured_power_limits_temperature_and_packs(device):
    device.properties.update({"gridInputPower": 100, "outputHomePower": 700, "inputLimit": 100})
    driver = driver_for(device)

    state = driver.read_state()

    assert state.available is True and state.stale is False
    assert state.soc_pct == pytest.approx(55.0)
    # measured pair wins: 100 W drawn - 700 W to the home = 600 W discharge (models.py sign)
    assert state.power_w == pytest.approx(-600.0)
    assert state.power_measured is True
    assert state.capacity_kwh == pytest.approx(2.4)  # the user's figure, never invented
    assert state.energy_kwh == pytest.approx(2.4 * 0.55)
    assert state.pv_power_w == pytest.approx(120.0)
    assert state.temperature_c == pytest.approx(20.0)  # (2931 - 2731) / 10
    assert state.extra["packs"] == 2
    assert state.extra["measured_pair"] is True
    assert state.extra["ac_mode"] == pytest.approx(2.0)
    assert state.extra["input_limit_w"] == pytest.approx(100.0)
    assert state.last_error is None


def test_power_falls_back_to_the_limit_setpoint_when_the_measured_pair_is_incomplete(device):
    del device.properties["outputHomePower"]
    device.properties.update({"inputLimit": 300, "outputLimit": 0})
    driver = driver_for(device)

    state = driver.read_state()

    assert state.power_w == pytest.approx(300.0)  # setpoint: positive = charge
    assert state.power_measured is False  # a setpoint is not a flow measurement


def test_cell_voltage_is_the_lowest_pack_minVol(device):
    driver = driver_for(device)

    assert driver.read_state().extra["cell_voltage_v"] == pytest.approx(32.8)
    assert driver.read_cell_voltage() == pytest.approx(32.8)


def test_cell_voltage_is_none_without_pack_data(device):
    device.packs = []
    driver = driver_for(device)

    assert driver.read_cell_voltage() is None
    assert driver.read_state().extra["cell_voltage_v"] is None


# --------------------------------------------------------------------------- writes


def test_charge_writes_ac_mode_1_with_a_positive_input_limit(device):
    driver = driver_for(device)

    result = driver.apply(600.0, dry_run=False)

    assert result.ok is True and result.code == Reason.OK
    assert result.applied_w == pytest.approx(600.0)
    assert result.readback_w == pytest.approx(600.0)
    assert len(device.posts) == 1
    assert device.posts[0]["path"] == "/properties/write"
    assert device.posts[0]["body"]["sn"] == EXAMPLE_SERIAL
    assert device.posts[0]["body"]["properties"] == {
        "smartMode": 1,
        "acMode": 1,
        "inputLimit": 600,
        "outputLimit": 0,
    }


def test_discharge_writes_ac_mode_2_with_a_positive_output_limit(device):
    driver = driver_for(device)

    result = driver.apply(-500.0, dry_run=False)

    assert result.ok is True
    assert result.applied_w == pytest.approx(-500.0)
    assert device.posts[0]["body"]["properties"] == {
        "smartMode": 1,
        "acMode": 2,
        "inputLimit": 0,
        "outputLimit": 500,
    }


def test_hold_writes_zero_limits_and_smart_mode_off(device):
    device.properties.update({"inputLimit": 400, "smartMode": 1})
    driver = driver_for(device)

    result = driver.hold(dry_run=False)

    assert result.ok is True and result.applied_w == pytest.approx(0.0)
    assert device.posts[-1]["body"]["properties"] == {
        "smartMode": 0,
        "acMode": 2,
        "inputLimit": 0,
        "outputLimit": 0,
    }


def test_unreadable_limits_never_fall_back_to_the_measured_flow(device):
    """0.21.1: the limits *are* the command here, so a missing echo fails open (writes)."""
    del device.properties["inputLimit"]
    del device.properties["outputLimit"]
    device.properties.update({"gridInputPower": 0, "outputHomePower": 0})
    driver = driver_for(device)
    driver.read_state()  # one refresh, so the measured pair is in hand

    assert driver._read_power() == 0.0  # the measured pair still reads "idle"
    assert driver._idempotent_power() is None  # never the flow
    result = driver.apply(0.0, dry_run=False)
    assert result.code != Reason.IDEMPOTENT_SKIP, result.message
    assert result.ok is True, result.message


def test_holds_are_never_skipped_as_idempotent(device):
    driver = driver_for(device)
    driver.hold(dry_run=False)
    count = len(device.posts)

    driver.hold(dry_run=False)

    assert len(device.posts) == count + 1, "a live zero command must still be written"


def test_dry_run_reads_but_never_posts(device):
    driver = driver_for(device)

    result = driver.apply(300.0, dry_run=True)

    assert result.code == Reason.DRY_RUN and result.dry_run is True
    assert device.posts == []
    assert result.writes and result.writes[0]["service"] == "zen_sdk.write"


def test_idempotent_skip_within_tolerance(device):
    device.properties.update({"inputLimit": 600, "smartMode": 1})
    driver = driver_for(device)

    result = driver.apply(596.0, dry_run=False)

    assert result.code == Reason.IDEMPOTENT_SKIP
    assert device.posts == [], "a device already at the target must not be written again"


def test_a_smart_mode_change_forces_a_write_even_when_the_limits_match(device):
    # Limits read 0 while smartMode is on: a hold must still be written, or the running smart
    # mode would be left in charge.
    device.properties.update({"inputLimit": 0, "outputLimit": 0, "smartMode": 1})
    driver = driver_for(device)

    result = driver.apply(0.0, dry_run=False)

    assert result.ok is True
    assert device.posts[-1]["body"]["properties"]["smartMode"] == 0


# --------------------------------------------------------------------------- clamping


def test_clamps_to_the_devices_own_maxima(device):
    device.properties.update({"chargeMaxLimit": 600, "inverseMaxPower": 500})
    driver = driver_for(device)

    charge = driver.apply(800.0, dry_run=False)
    assert charge.requested_w == pytest.approx(600.0)
    assert device.posts[-1]["body"]["properties"]["inputLimit"] == 600

    discharge = driver.apply(-800.0, dry_run=False)
    assert discharge.requested_w == pytest.approx(-500.0)
    assert device.posts[-1]["body"]["properties"]["outputLimit"] == 500


def test_missing_device_maxima_keep_the_configured_limits(device):
    del device.properties["chargeMaxLimit"]
    del device.properties["inverseMaxPower"]
    driver = driver_for(device)

    result = driver.apply(800.0, dry_run=False)

    assert result.requested_w == pytest.approx(800.0)
    assert device.posts[-1]["body"]["properties"]["inputLimit"] == 800


# --------------------------------------------------------------------------- failures


def test_readback_mismatch_is_a_driver_error_never_a_false_success(device):
    device.mode = "ignore_write"  # accepts the POST, never applies it
    driver = driver_for(device)

    result = driver.apply(400.0, dry_run=False)

    assert result.ok is False
    assert result.code == Reason.DRIVER_ERROR
    assert result.applied_w == pytest.approx(0.0)
    assert "read-back" in result.message and "did not confirm the write" in result.message


def test_http_error_is_a_driver_error(device):
    device.mode = "http_500"
    driver = driver_for(device)

    result = driver.apply(400.0, dry_run=False)

    assert result.ok is False and result.code == Reason.DRIVER_ERROR
    assert "HTTP 500" in result.message


def test_http_401_names_the_local_api_and_never_echoes_a_token(device):
    device.mode = "http_401"
    driver = driver_for(device, config={"mapping": mapping_for(device, token=EXAMPLE_TOKEN)})

    result = driver.apply(400.0, dry_run=False)

    assert result.ok is False and result.code == Reason.DRIVER_ERROR
    assert "401" in result.message and "local API" in result.message
    assert EXAMPLE_TOKEN not in result.message


def test_malformed_json_read_degrades_to_unavailable(device):
    device.mode = "malformed"
    driver = driver_for(device)

    state = driver.read_state()

    assert state.available is False and state.stale is True
    assert "malformed JSON" in (state.last_error or "")
    assert driver.read_cell_voltage() is None


def test_non_object_report_degrades_gracefully(device):
    device.mode = "array"
    driver = driver_for(device)

    state = driver.read_state()

    assert state.available is False
    assert "non-object" in (state.last_error or "")


def test_timeout_is_a_driver_error_not_a_crash(device):
    device.delay_s = 0.6
    driver = driver_for(device, config={"mapping": mapping_for(device, timeout_s="0.2")})

    state = driver.read_state()
    assert state.available is False
    assert "no response within 0.2 s" in (state.last_error or "")

    result = driver.apply(400.0, dry_run=False)
    assert result.ok is False and result.code == Reason.DRIVER_ERROR


# --------------------------------------------------------------------------- config


def test_connection_validation_names_every_missing_or_malformed_key():
    assert validate_connection({}) == [
        "mapping: host is required (the device's IP address or hostname)",
        "mapping: serial is required (the device serial number, sn)",
    ]
    problems = validate_connection(
        {
            "host": "http://device.example.invalid",
            "serial": "EXAMPLE 0001",
            "scheme": "ftp",
            "port": "70000",
            "timeout_s": "0",
            "api_path": "properties",
        }
    )
    joined = " ".join(problems)
    for needle in ("host must be a bare", "serial must not contain", "http or https", "1..65535", "0..60", "api_path"):
        assert needle in joined, problems
    assert validate_connection({"host": "127.0.0.1", "serial": EXAMPLE_SERIAL}) == []


def test_probe_refuses_an_unreachable_device_and_names_the_settings(device):
    mapping = {"host": "127.0.0.1", "port": str(free_port()), "serial": EXAMPLE_SERIAL}

    problems = ZendureNativeDriver.probe_config(config_for(device, mapping=mapping))

    assert problems and "cannot read the Zendure device" in problems[0]


def test_probe_accepts_allow_unreachable_as_the_explicit_opt_out(device):
    mapping = {"host": "127.0.0.1", "port": str(free_port()), "serial": EXAMPLE_SERIAL, "allow_unreachable": "true"}

    assert ZendureNativeDriver.probe_config(config_for(device, mapping=mapping)) == []


def test_probe_checks_the_reported_serial_and_the_report_shape(device):
    driver_config = config_for(device)
    device.mode = "serial_mismatch"
    assert "different serial" in ZendureNativeDriver.probe_config(driver_config)[0]

    device.mode = "empty_properties"
    assert "local API" in ZendureNativeDriver.probe_config(driver_config)[0]


def test_probe_reads_the_live_device(device):
    assert ZendureNativeDriver.probe_config(config_for(device)) == []
    assert device.gets >= 1


def test_registry_validates_and_creates_the_native_driver(device):
    config = config_for(device)
    assert driver_registry.validate_config(config) == []
    assert driver_registry.probe_config(config) == []

    driver = driver_registry.create(config, NoHomeAssistant())
    assert driver.id() == "example_native"
    assert driver.capabilities()["mapping_resolved"] is True

    broken = config_for(device, mapping={"host": "127.0.0.1"})
    assert any("serial" in problem for problem in driver_registry.validate_config(broken))
    with pytest.raises(ValueError):
        driver_registry.create(broken, NoHomeAssistant())


def test_capabilities_report_connection_and_device_limits_without_entities(device):
    device.properties.update({"chargeMaxLimit": 900, "inverseMaxPower": 700})
    driver = driver_for(device)
    driver.read_state()

    caps = driver.capabilities()

    assert "entities" not in caps, "the mapping is connection data, not HA entities"
    assert caps["native"] is True and caps["driver"] == "zendure_native"
    assert caps["connection"]["host"] == "127.0.0.1"
    assert caps["connection"]["serial"] == EXAMPLE_SERIAL
    assert caps["connection"]["token_set"] is False
    assert caps["device_limits"] == {"max_charge_w": 900.0, "max_discharge_w": 700.0}
    assert caps["cell_voltage_v"] == pytest.approx(32.8)


# --------------------------------------------------------------------------- secrets


def test_a_configured_token_is_sent_as_a_bearer_header(device):
    driver = driver_for(device, config={"mapping": mapping_for(device, token=EXAMPLE_TOKEN)})

    assert driver.apply(300.0, dry_run=False).ok is True

    assert device.token_header == f"Bearer {EXAMPLE_TOKEN}"
    assert driver.capabilities()["connection"]["token_set"] is True


def test_a_token_is_never_echoed_by_a_battery_payload_or_capabilities(device):
    config = config_for(device, mapping=mapping_for(device, token=EXAMPLE_TOKEN))

    payload = battery_payload(config)
    assert payload["mapping"]["token"] == MASKED_SECRET
    assert EXAMPLE_TOKEN not in json.dumps(payload)

    caps = ZendureNativeDriver(config, NoHomeAssistant(), sleep=lambda _s: None).capabilities()
    assert EXAMPLE_TOKEN not in json.dumps(caps)


def test_a_masked_or_absent_token_keeps_the_stored_one_on_a_patch():
    stored = {"host": "127.0.0.1", "serial": EXAMPLE_SERIAL, "token": EXAMPLE_TOKEN}

    assert merge_mapping_patch(stored, {"host": "127.0.0.1", "serial": EXAMPLE_SERIAL})["token"] == EXAMPLE_TOKEN
    assert merge_mapping_patch(stored, {**stored, "token": MASKED_SECRET})["token"] == EXAMPLE_TOKEN
    assert merge_mapping_patch(stored, {**stored, "token": "example-new"})["token"] == "example-new"


# --------------------------------------------------------------------------- engine save-time


def test_engine_refuses_an_unreachable_native_battery_at_save_time():
    from test_wp2b_engine import build_engine  # the same fakes `--selfcheck` uses

    engine, _store, _ha, _clock, _settings = build_engine()
    payload = {
        "id": "example_native",
        "name": "Example Native",
        "driver": "zendure_native",
        "capacity_kwh": 2.4,
        "mapping": {"host": "127.0.0.1", "port": str(free_port()), "serial": EXAMPLE_SERIAL},
    }

    with pytest.raises(ValueError) as excinfo:
        engine.create_battery(payload)

    assert "cannot read the Zendure device" in str(excinfo.value)


def test_engine_saves_the_native_battery_and_keeps_the_token_on_a_patch(device):
    from test_wp2b_engine import build_engine

    engine, _store, _ha, _clock, _settings = build_engine()
    payload = {
        "id": "example_native",
        "name": "Example Native",
        "driver": "zendure_native",
        "capacity_kwh": 2.4,
        "mapping": mapping_for(device, token=EXAMPLE_TOKEN),
    }

    created = engine.create_battery(payload)
    assert created["driver"] == "zendure_native"
    assert created["mapping"]["token"] == MASKED_SECRET
    stored = next(c for c in engine.battery_configs() if c.id == "example_native")
    assert stored.mapping["token"] == EXAMPLE_TOKEN

    # The UI echoes the redacted map back; the stored credential must survive.
    updated = engine.update_battery("example_native", {"mapping": {**mapping_for(device), "token": MASKED_SECRET}})
    assert updated["mapping"]["token"] == MASKED_SECRET
    stored = next(c for c in engine.battery_configs() if c.id == "example_native")
    assert stored.mapping["token"] == EXAMPLE_TOKEN


def test_engine_builds_one_driver_per_native_device():
    """Two batteries on one host+serial would overwrite each other's limits; only one is driven."""
    from test_wp2b_engine import build_engine

    engine, _store, _ha, _clock, _settings = build_engine()
    base = {
        "driver": "zendure_native",
        "capacity_kwh": 2.4,
        "mapping": {"host": "127.0.0.1", "serial": EXAMPLE_SERIAL, "allow_unreachable": "true"},
    }
    engine.create_battery({**base, "id": "example_native_a", "name": "Example A"})
    engine.create_battery({**base, "id": "example_native_b", "name": "Example B"})

    assert "example_native_a" in engine._drivers
    assert "example_native_b" not in engine._drivers, "the same device must not get two controlling drivers"
    events = engine.journal.entries(limit=50) or []
    assert any(entry.get("code") == "driver_conflict" for entry in events), events
