"""WP2A: WebSocket frame codec, mini-server integration and discovery helpers (SPEC.md 5/10)."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.haws import (  # noqa: E402
    OPCODE_CLOSE,
    OPCODE_CONTINUATION,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    Frame,
    HAWebSocket,
    IncompleteFrame,
    WSError,
    decode_frame,
    discover_candidates,
    encode_frame,
    registry_entries_for_integration,
    suggested_mapping,
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------------------------
# Mini WebSocket server built from the same codec (SPEC.md section 14)
# ---------------------------------------------------------------------------------------------


class MiniWSServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.messages: list[bytes] = []
        self.opcodes: list[int] = []
        self.error: BaseException | None = None

    def run(self):
        try:
            conn, _ = self.socket.accept()
            with conn:
                conn.settimeout(5)
                self._handshake(conn)
                conn.sendall(encode_frame(json.dumps({"type": "auth_required"}), mask=False))
                auth, buffer = self._read_frame(conn, b"")
                assert json.loads(auth.payload) == {"type": "auth", "access_token": "test-token"}
                conn.sendall(encode_frame(json.dumps({"type": "auth_ok"}), mask=False))
                command, buffer = self._read_frame(conn, buffer)
                self.messages.append(command.payload)
                request = json.loads(command.payload)
                payload = json.dumps(
                    {
                        "id": request["id"],
                        "type": "result",
                        "success": True,
                        "result": [{"entity_id": "sensor.example_pv_power"}],
                    }
                ).encode()
                conn.sendall(encode_frame(b"pong-me", opcode=OPCODE_PING, mask=False))
                split = len(payload) // 2
                conn.sendall(encode_frame(payload[:split], fin=False, mask=False))
                conn.sendall(encode_frame(payload[split:], opcode=OPCODE_CONTINUATION, mask=False))
                pong, buffer = self._read_frame(conn, buffer)
                self.opcodes.append(pong.opcode)
                self.messages.append(pong.payload)
                close, _ = self._read_frame(conn, buffer)
                self.opcodes.append(close.opcode)
        except BaseException as err:  # pragma: no cover - surfaced by the test assertions
            self.error = err
        finally:
            self.socket.close()

    def _handshake(self, conn) -> None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += conn.recv(4096)
        head = buffer.split(b"\r\n\r\n")[0].decode("latin-1")
        key = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )

    def _read_frame(self, conn, buffer: bytes) -> tuple[Frame, bytes]:
        while True:
            try:
                frame, consumed = decode_frame(buffer)
            except IncompleteFrame:
                buffer += conn.recv(65536)
                continue
            return frame, buffer[consumed:]


def test_mini_server_roundtrip_with_fragmentation_and_ping():
    server = MiniWSServer()
    server.start()
    client = HAWebSocket(f"ws://127.0.0.1:{server.port}/api/websocket", "test-token", timeout=5)
    client.connect()
    try:
        assert client.list_entity_registry() == [{"entity_id": "sensor.example_pv_power"}]
    finally:
        client.close()
    server.join(timeout=5)
    assert server.error is None
    assert json.loads(server.messages[0])["type"] == "config/entity_registry/list"
    assert server.opcodes == [OPCODE_PONG, OPCODE_CLOSE]
    assert server.messages[1] == b"pong-me"


def test_connect_requires_configuration():
    with pytest.raises(Exception):
        HAWebSocket("", "", env={}).connect()
    local = HAWebSocket(env={"HA_URL": "http://ha.local:8123", "HA_TOKEN": "t"})
    assert local.url == "ws://ha.local:8123/api/websocket"
    secure = HAWebSocket(env={"HA_URL": "https://ha.example.com/api/websocket", "HA_TOKEN": "t"})
    assert secure.url == "wss://ha.example.com/api/websocket"


# ---------------------------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------------------------


def test_encode_mask_roundtrip():
    payload = "hello".encode()
    encoded = encode_frame(payload, mask=True, mask_key=b"\x01\x02\x03\x04")
    frame, consumed = decode_frame(encoded)
    assert consumed == len(encoded)
    assert frame == Frame(fin=True, opcode=OPCODE_TEXT, payload=payload)
    assert encoded[1] & 0x80  # client frames are masked


@pytest.mark.parametrize("length", [0, 1, 125, 126, 127, 65535, 65536, 70000])
def test_payload_lengths_126_and_127(length):
    payload = b"x" * length
    masked = encode_frame(payload, mask=True)
    frame, consumed = decode_frame(masked)
    assert frame.payload == payload and consumed == len(masked)
    unmasked = encode_frame(payload, mask=False)
    assert (unmasked[1] & 0x7F) == (126 if 126 <= length < 65536 else 127 if length >= 65536 else length)
    if length >= 126:
        assert frame.opcode == OPCODE_TEXT


def test_decode_incomplete_and_protocol_errors():
    with pytest.raises(IncompleteFrame):
        decode_frame(encode_frame(b"hello", mask=False)[:3])
    with pytest.raises(IncompleteFrame):
        decode_frame(b"")
    with pytest.raises(WSError):
        decode_frame(bytes([0xC1, 0x00]))  # reserved bits set
    oversized = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 2**40)
    with pytest.raises(WSError):
        decode_frame(oversized)


class ScriptedSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent: list[bytes] = []

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, value):
        pass

    def close(self):
        pass


def test_recv_message_answers_ping_and_assembles_fragments():
    first = b'{"a": '
    second = b"1}"
    chunks = [
        encode_frame(b"ping!", opcode=OPCODE_PING, mask=False),
        encode_frame(first, opcode=OPCODE_TEXT, fin=False, mask=False),
        encode_frame(second, opcode=OPCODE_CONTINUATION, fin=True, mask=False),
    ]
    client = HAWebSocket("ws://unused", "t")
    client._sock = ScriptedSocket(chunks)
    assert client.recv_message() == '{"a": 1}'
    pong, _ = decode_frame(client._sock.sent[0])
    assert pong.opcode == OPCODE_PONG and pong.payload == b"ping!"


def test_binary_frames_rejected():
    client = HAWebSocket("ws://unused", "t")
    client._sock = ScriptedSocket([encode_frame(b"\x00\x01", opcode=0x2, mask=False)])
    with pytest.raises(WSError):
        client.recv_message()


def test_call_answers_server_ping_while_waiting_for_the_result():
    """Core PINGs every ~55 s; a late PONG means the connection is dropped (appendix A)."""
    result = json.dumps({"id": 1, "type": "result", "success": True, "result": ["ok"]}).encode()
    chunks = [encode_frame(b"heartbeat", opcode=OPCODE_PING, mask=False), encode_frame(result, mask=False)]
    sock = ScriptedSocket(chunks)
    client = HAWebSocket("ws://unused", "t")
    client._sock = sock
    assert client.call("config/entity_registry/list") == ["ok"]
    frame, _ = decode_frame(sock.sent[1])
    assert frame.opcode == OPCODE_PONG and frame.payload == b"heartbeat"


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------


def _entry(entity_id, *, platform="demo", translation_key=None, disabled_by=None, device_id=None):
    entry = {"entity_id": entity_id, "platform": platform, "original_name": entity_id}
    if translation_key:
        entry["translation_key"] = translation_key
    if disabled_by:
        entry["disabled_by"] = disabled_by
    if device_id:
        entry["device_id"] = device_id
    return entry


def _state(entity_id, value="1.0", **attributes):
    return {"entity_id": entity_id, "state": value, "attributes": attributes}


def test_translation_key_beats_text_match():
    entries = [
        _entry("sensor.example_battery_level", translation_key="electric_level"),
        _entry("sensor.example_things_battery_level"),
    ]
    states = [
        _state("sensor.example_battery_level", "42", device_class="battery", unit_of_measurement="%"),
        _state("sensor.example_things_battery_level", "43", device_class="battery", unit_of_measurement="%"),
    ]
    rows = discover_candidates("battery", entries, states)
    soc_rows = [row for row in rows if row["suggested_role"] == "soc"]
    assert soc_rows[0]["entity_id"] == "sensor.example_battery_level"
    assert soc_rows[0]["confidence"] > soc_rows[1]["confidence"]
    assert 0.0 <= soc_rows[0]["confidence"] <= 1.0
    assert "unique_id" not in soc_rows[0]


def test_discovery_filters_disabled_search_and_limit():
    entries = [
        _entry("sensor.example_pv_power", translation_key="solar_input_power", platform="zendure_ha"),
        _entry("sensor.example_hidden_pv", translation_key="solar_input_power", disabled_by="user"),
        _entry("sensor.example_output_power"),
    ]
    states = [
        _state("sensor.example_pv_power", "500", device_class="power", unit_of_measurement="W"),
        _state("sensor.example_hidden_pv", "0", device_class="power", unit_of_measurement="W"),
        _state("sensor.example_output_power", "300", device_class="power", unit_of_measurement="W"),
    ]
    rows = discover_candidates("battery", entries, states, integration="zendure_ha")
    assert {row["entity_id"] for row in rows} == {"sensor.example_pv_power", "sensor.example_output_power"}
    searched = discover_candidates("battery", entries, states, search="example_output")
    assert [row["entity_id"] for row in searched] == ["sensor.example_output_power"]
    assert len(discover_candidates("solar", entries, states, limit=500)) <= 50
    with pytest.raises(Exception):
        discover_candidates("nope", entries, states)


def test_registry_entries_for_integration_and_mapping():
    # Zendure unique ids are not domain-prefixed: discovery keys on platform + translation_key only.
    entries = [
        _entry("sensor.example_input_limit", platform="zendure_ha", translation_key="input_limit"),
        _entry("sensor.example_other_limit", platform="other", translation_key="input_limit"),
        _entry("sensor.example_plain", platform="zendure_ha"),
    ]
    by_platform = registry_entries_for_integration(entries, platform="zendure_ha")
    assert [entry["entity_id"] for entry in by_platform] == [
        "sensor.example_input_limit",
        "sensor.example_plain",
    ]
    by_key = registry_entries_for_integration(
        entries, platform="zendure_ha", translation_keys=["input_limit"]
    )
    assert [entry["entity_id"] for entry in by_key] == ["sensor.example_input_limit"]
    assert "unique_id" not in by_key[0]

    states = [
        _state("sensor.example_input_limit", "400", device_class="power", unit_of_measurement="W"),
        _state("sensor.example_electric_level", "55", device_class="battery", unit_of_measurement="%"),
    ]
    candidates = discover_candidates(
        "battery",
        [_entry("sensor.example_input_limit", translation_key="input_limit", platform="zendure_ha"),
         _entry("sensor.example_electric_level", translation_key="electric_level", platform="zendure_ha")],
        states,
        integration="zendure_ha",
    )
    mapping = suggested_mapping("battery", candidates)
    assert mapping["charge_limit"] == "sensor.example_input_limit"
    assert mapping["soc"] == "sensor.example_electric_level"
    assert suggested_mapping("battery", candidates, min_confidence=0.99) == {}


def test_candidate_rows_carry_the_device_id():
    """SPEC 10: a battery is a *device*, so a row names the device it belongs to.

    Additive contract: the published keys stay exactly as they were and ``device_id`` is the entry's
    registry device id, or null when Home Assistant has not attached one.
    """
    entries = [
        _entry("sensor.example_battery_soc", translation_key="electric_level", device_id="example_device_1"),
        _entry("sensor.example_orphan_soc", translation_key="electric_level"),
    ]
    states = [
        _state("sensor.example_battery_soc", "55", device_class="battery", unit_of_measurement="%"),
        _state("sensor.example_orphan_soc", "60", device_class="battery", unit_of_measurement="%"),
    ]
    rows = discover_candidates("battery", entries, states)
    by_entity = {row["entity_id"]: row for row in rows}
    assert by_entity["sensor.example_battery_soc"]["device_id"] == "example_device_1"
    assert by_entity["sensor.example_orphan_soc"]["device_id"] is None
    # every other key is untouched: exactly one key more than the published set
    assert set(rows[0]) == {
        "entity_id",
        "name",
        "unit",
        "device_class",
        "state",
        "platform",
        "suggested_role",
        "confidence",
        "device_id",
        "min",
        "max",
    }


def test_candidate_rows_carry_the_numeric_bounds():
    """SPEC 10: the picker needs the entity's real ``min``/``max``, never an invented bound.

    Bounds are copied from the state attributes only when they are finite numbers (HA publishes
    them as numbers or numeric strings); anything else stays null and no other key moves.
    """
    entries = [
        _entry("number.example_input_limit", translation_key="input_limit"),
        _entry("sensor.example_electric_level", translation_key="electric_level"),
        _entry("number.example_text_bounds", translation_key="input_limit"),
        _entry("sensor.example_nonfinite_bounds", translation_key="electric_level"),
    ]
    states = [
        _state("number.example_input_limit", "400", min=0, max=1200),
        _state("sensor.example_electric_level", "55", min="unavailable", max="not-a-number"),
        _state("number.example_text_bounds", "400", min="0", max="1200.5"),
        _state(
            "sensor.example_nonfinite_bounds",
            "55",
            min=float("inf"),
            max=float("nan"),
        ),
    ]
    by_entity = {row["entity_id"]: row for row in discover_candidates("battery", entries, states)}
    assert by_entity["number.example_input_limit"]["min"] == 0.0
    assert by_entity["number.example_input_limit"]["max"] == 1200.0
    assert by_entity["sensor.example_electric_level"]["min"] is None
    assert by_entity["sensor.example_electric_level"]["max"] is None
    # numeric strings are copied as numbers; non-finite values are not bounds at all
    assert by_entity["number.example_text_bounds"]["min"] == 0.0
    assert by_entity["number.example_text_bounds"]["max"] == 1200.5
    assert by_entity["sensor.example_nonfinite_bounds"]["min"] is None
    assert by_entity["sensor.example_nonfinite_bounds"]["max"] is None
    # no bounds at all in the attributes: still null, and the published keys are untouched
    plain = discover_candidates(
        "battery", [_entry("sensor.example_plain_soc", translation_key="electric_level")],
        [_state("sensor.example_plain_soc", "60", device_class="battery")],
    )
    assert plain[0]["min"] is None and plain[0]["max"] is None
    assert set(plain[0]) == {
        "entity_id",
        "name",
        "unit",
        "device_class",
        "state",
        "platform",
        "suggested_role",
        "confidence",
        "device_id",
        "min",
        "max",
    }


def test_usable_energy_is_not_offered_as_capacity():
    """SPEC 10 / appendix A: usable energy above min_soc is not the installed capacity.

    Both quantities come from the same integration, both carry kWh and both are matched by
    translation key, so they used to share the single ``capacity`` role at equal confidence. The
    wizard could then write a nearly empty battery's ``available_kwh`` reading -- which may even be
    negative -- into the driver's ``capacity_entity``, i.e. use "what is left right now" as "how big
    the pack is". They must stay distinguishable roles.
    """
    entries = [
        _entry("sensor.example_available_kwh", translation_key="available_kwh"),
        _entry("sensor.example_total_kwh", translation_key="total_kwh"),
    ]
    states = [
        _state("sensor.example_available_kwh", "-0.04", unit_of_measurement="kWh"),
        _state("sensor.example_total_kwh", "1.92", unit_of_measurement="kWh"),
    ]
    rows = discover_candidates("battery", entries, states)
    roles = {(row["entity_id"], row["suggested_role"]) for row in rows}
    assert ("sensor.example_available_kwh", "available_energy") in roles
    assert ("sensor.example_total_kwh", "capacity") in roles
    assert not any(entity.endswith("available_kwh") and role == "capacity" for entity, role in roles)
