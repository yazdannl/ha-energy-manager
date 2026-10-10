"""Minimal stdlib Home Assistant WebSocket client (SPEC.md section 10 prerequisites).

Contains three independent pieces:

* a WebSocket frame codec (:func:`encode_frame`, :func:`decode_frame`) with 126/127-bit payload
  lengths, client masking, ping/pong and continuation frames;
* :class:`HAWebSocket`, a text-only client implementing the HA auth handshake and the registry /
  statistics commands;
* entity-registry discovery helpers used by ``GET /api/v1/discover`` (SPEC.md section 10): candidates
  are scored from ``platform`` + ``translation_key`` first and entity-id/friendly-name text second.
  Discovered payloads never contain ``unique_id`` (it can embed device serial numbers) and never
  hardcode an instance entity id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .models import ConfigError, to_iso

log = logging.getLogger(__name__)

__all__ = [
    "Frame",
    "HAWebSocket",
    "IncompleteFrame",
    "WSError",
    "WSTimeout",
    "OPCODE_BINARY",
    "OPCODE_CLOSE",
    "OPCODE_CONTINUATION",
    "OPCODE_PING",
    "OPCODE_PONG",
    "OPCODE_TEXT",
    "decode_frame",
    "encode_frame",
    "discover_candidates",
    "registry_entries_for_integration",
    "suggested_mapping",
    "SUPERVISOR_WS_URL",
    "ZENDURE_PLATFORM",
]

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"
#: Zendure's HA integration domain (SPEC.md section 5) — identical on every install.
ZENDURE_PLATFORM = "zendure_ha"
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class WSError(Exception):
    """WebSocket protocol, transport or HA command error."""


class WSTimeout(WSError):
    """No WebSocket message arrived within the configured timeout."""


class IncompleteFrame(Exception):
    """Raised by :func:`decode_frame` when the buffer holds only part of a frame."""


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes

    def text(self) -> str:
        return self.payload.decode("utf-8")


# --------------------------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------------------------


def encode_frame(
    payload: bytes | str,
    opcode: int = OPCODE_TEXT,
    fin: bool = True,
    mask: bool = True,
    mask_key: bytes | None = None,
) -> bytes:
    """Encode one WebSocket frame. Client frames are masked by default (RFC 6455)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    mask_bit = 0x80 if mask else 0x00
    length = len(data)
    if length < 126:
        header = bytes([first, mask_bit | length])
    elif length < 0x10000:
        header = bytes([first, mask_bit | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, mask_bit | 127]) + struct.pack("!Q", length)
    if not mask:
        return header + data
    key = mask_key if mask_key is not None else os.urandom(4)
    if len(key) != 4:
        raise ValueError("mask_key must be 4 bytes")
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(data))
    return header + key + masked


def decode_frame(data: bytes | bytearray) -> tuple[Frame, int]:
    """Decode the first frame in ``data``; returns ``(frame, bytes_consumed)``.

    Raises :class:`IncompleteFrame` when more bytes are needed, :class:`WSError` on protocol errors.
    """
    buffer = bytes(data)
    if len(buffer) < 2:
        raise IncompleteFrame("need at least two header bytes")
    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    if first & 0x70:
        raise WSError("reserved bits set (compression/extensions are not supported)")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            raise IncompleteFrame("need 2 extended length bytes")
        length = struct.unpack("!H", buffer[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            raise IncompleteFrame("need 8 extended length bytes")
        length = struct.unpack("!Q", buffer[offset : offset + 8])[0]
        offset += 8
    if length > MAX_MESSAGE_BYTES:
        raise WSError(f"frame payload too large: {length} bytes")
    key = b""
    if masked:
        if len(buffer) < offset + 4:
            raise IncompleteFrame("need 4 mask bytes")
        key = buffer[offset : offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        raise IncompleteFrame("need more payload bytes")
    payload = buffer[offset : offset + length]
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return Frame(fin=fin, opcode=opcode, payload=payload), offset + length


# --------------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------------


class HAWebSocket:
    """Text-only Home Assistant WebSocket client (one command at a time)."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 20.0,
        ssl_context: ssl.SSLContext | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if env is None else env
        supervisor_token = environment.get("SUPERVISOR_TOKEN") or ""
        if url is None:
            if supervisor_token:
                url = SUPERVISOR_WS_URL
            elif environment.get("HA_URL"):
                url = _ws_url_from_ha_url(str(environment["HA_URL"]))
            else:
                url = ""
        if token is None:
            token = supervisor_token or (environment.get("HA_TOKEN") or "")
        self.url = url
        self.token = token
        self.timeout = float(timeout)
        self.ssl_context = ssl_context
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._fragment: list[bytes] | None = None
        self._next_id = 0
        self._lock = threading.RLock()
        self._closing = False

    # -- connection -------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self, timeout: float | None = None) -> None:
        if self._sock is not None:
            return
        if not self.url:
            raise ConfigError("Home Assistant WebSocket URL is not configured")
        if not self.token:
            raise ConfigError("Home Assistant token is not configured")
        wait = self.timeout if timeout is None else float(timeout)
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise ConfigError(f"unsupported WebSocket scheme: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise ConfigError("WebSocket URL has no host")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/api/websocket"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            raw = socket.create_connection((host, port), timeout=wait)
        except OSError as err:
            raise WSError(f"cannot reach Home Assistant WebSocket: {err}") from None
        if parsed.scheme == "wss":
            context = self.ssl_context or ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        raw.settimeout(wait)
        self._sock = raw
        self._buffer.clear()
        self._fragment = None
        self._closing = False
        try:
            self._handshake(host, port, path)
            self._authenticate()
        except BaseException:
            self.close()
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        assert self._sock is not None
        self._sock.sendall(request.encode("ascii"))
        header_end = self._buffer.find(b"\r\n\r\n")
        while header_end < 0:
            self._read_socket()
            header_end = self._buffer.find(b"\r\n\r\n")
        raw_headers = bytes(self._buffer[:header_end]).decode("latin-1")
        del self._buffer[: header_end + 4]
        lines = raw_headers.split("\r\n")
        if not lines or "101" not in lines[0]:
            raise WSError(f"WebSocket handshake failed: {lines[0] if lines else 'empty response'}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise WSError("WebSocket handshake returned an invalid Sec-WebSocket-Accept")

    def _authenticate(self) -> None:
        message = json.loads(self.recv_message())
        message_type = message.get("type")
        if message_type == "auth_ok":
            return
        if message_type != "auth_required":
            raise WSError(f"unexpected first WebSocket message: {message_type!r}")
        self._send_json({"type": "auth", "access_token": self.token})
        while True:
            message = json.loads(self.recv_message())
            message_type = message.get("type")
            if message_type == "auth_ok":
                return
            if message_type == "auth_invalid":
                raise WSError(f"Home Assistant rejected the token: {message.get('message', '')}")
            raise WSError(f"unexpected auth message: {message_type!r}")

    def close(self) -> None:
        self._closing = True
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(encode_frame(b"", opcode=OPCODE_CLOSE, mask=True))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        self._buffer.clear()
        self._fragment = None

    def __enter__(self) -> "HAWebSocket":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- framing -------------------------------------------------------------
    def _read_socket(self) -> None:
        if self._sock is None:
            raise WSError("WebSocket is not connected")
        try:
            chunk = self._sock.recv(65536)
        except TimeoutError:
            raise WSTimeout(f"timed out after {self.timeout:g}s waiting for Home Assistant") from None
        except OSError as err:
            raise WSError(f"WebSocket connection lost: {err}") from None
        if not chunk:
            raise WSError("WebSocket connection closed by Home Assistant")
        self._buffer.extend(chunk)

    def _pop_frame(self) -> Frame | None:
        try:
            frame, consumed = decode_frame(self._buffer)
        except IncompleteFrame:
            return None
        del self._buffer[:consumed]
        return frame

    def recv_message(self) -> str:
        """Return the next complete text message, answering pings transparently."""
        while True:
            frame = self._pop_frame()
            if frame is None:
                self._read_socket()
                continue
            if frame.opcode == OPCODE_PING:
                self._send_frame(frame.payload, OPCODE_PONG)
                continue
            if frame.opcode == OPCODE_PONG:
                continue
            if frame.opcode == OPCODE_CLOSE:
                if not self._closing:
                    self._closing = True
                    try:
                        self._send_frame(frame.payload, OPCODE_CLOSE)
                    except WSError:
                        pass
                raise WSError("WebSocket closed by Home Assistant")
            if frame.opcode == OPCODE_BINARY:
                raise WSError("binary WebSocket frames are not supported")
            if frame.opcode == OPCODE_TEXT:
                if self._fragment is not None:
                    raise WSError("new text frame inside a fragmented message")
                if frame.fin:
                    return frame.text()
                self._fragment = [frame.payload]
                continue
            if frame.opcode == OPCODE_CONTINUATION:
                if self._fragment is None:
                    raise WSError("continuation frame without a started message")
                self._fragment.append(frame.payload)
                if frame.fin:
                    payload = b"".join(self._fragment)
                    self._fragment = None
                    return payload.decode("utf-8")
                continue
            raise WSError(f"unsupported WebSocket opcode: {frame.opcode}")

    def _send_frame(self, payload: bytes, opcode: int = OPCODE_TEXT) -> None:
        if self._sock is None:
            raise WSError("WebSocket is not connected")
        try:
            self._sock.sendall(encode_frame(payload, opcode=opcode, mask=True))
        except OSError as err:
            raise WSError(f"cannot send WebSocket frame: {err}") from None

    def _send_json(self, message: Mapping[str, Any]) -> None:
        self._send_frame(json.dumps(message).encode("utf-8"), OPCODE_TEXT)

    # -- HA commands -------------------------------------------------------------
    def call(self, message_type: str, **fields: Any) -> Any:
        """Send one command and return its ``result``; raises :class:`WSError` on failure."""
        with self._lock:
            if self._sock is None:
                self.connect()
            assert self._sock is not None
            self._sock.settimeout(self.timeout)
            self._next_id += 1
            message_id = self._next_id
            self._send_json({"id": message_id, "type": message_type, **fields})
            while True:
                message = json.loads(self.recv_message())
                if message.get("id") != message_id or message.get("type") != "result":
                    continue  # event or unrelated response; this client only does request/response
                if message.get("success"):
                    return message.get("result")
                error = message.get("error") or {}
                raise WSError(
                    f"{message_type} failed: {error.get('message', 'unknown error')}",
                )

    def list_entity_registry(self) -> list[dict[str, Any]]:
        return self.call("config/entity_registry/list") or []

    def list_device_registry(self) -> list[dict[str, Any]]:
        return self.call("config/device_registry/list") or []

    def statistics_during_period(
        self,
        statistic_ids: str | Iterable[str],
        start_time: datetime | str,
        end_time: datetime | str | None = None,
        period: str = "hour",
        types: Sequence[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """``recorder/statistics_during_period`` for one or more statistics ids."""
        ids = [statistic_ids] if isinstance(statistic_ids, str) else list(statistic_ids)
        fields: dict[str, Any] = {
            "start_time": start_time if isinstance(start_time, str) else to_iso(start_time),
            "statistic_ids": ids,
            "period": period,
        }
        if end_time is not None:
            fields["end_time"] = end_time if isinstance(end_time, str) else to_iso(end_time)
        if types:
            fields["types"] = list(types)
        return self.call("recorder/statistics_during_period", **fields) or {}


def _ws_url_from_ha_url(ha_url: str) -> str:
    parsed = urllib.parse.urlsplit(ha_url.strip())
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    if path.rstrip("/").endswith("/api/websocket"):
        ws_path = path
    else:
        ws_path = path.rstrip("/") + "/api/websocket"
    return urllib.parse.urlunsplit((scheme, netloc, ws_path, "", ""))


# --------------------------------------------------------------------------------------------
# Entity discovery (SPEC.md sections 5 and 10)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RoleHint:
    role: str
    keys: tuple[str, ...]  # translation_key matches (exact match scores highest)
    text: tuple[str, ...]  # entity-id / friendly-name substrings
    device_class: str | None = None
    unit: tuple[str, ...] = ()


_ROLE_HINTS: dict[str, tuple[_RoleHint, ...]] = {
    "battery": (
        _RoleHint("charge_limit", ("input_limit", "charge_limit", "charge_power"), ("charge", "input_limit"), "power"),
        _RoleHint("discharge_limit", ("output_limit", "discharge_limit", "discharge_power"), ("discharge", "output_limit"), "power"),
        _RoleHint("power_setpoint", ("power_setpoint", "setpoint"), ("setpoint", "target_power"), "power"),
        _RoleHint("soc", ("electric_level", "battery_level", "soc", "state_of_charge"), ("soc", "battery_level", "electric_level"), "battery", ("%",)),
        _RoleHint("capacity", ("available_kwh", "total_kwh", "capacity"), ("capacity", "available_kwh", "total_kwh"), "energy", ("kWh",)),
        _RoleHint("pv_power", ("solar_input_power", "pv_power", "solar_power"), ("solar_input", "pv_power", "solar_power"), "power"),
        _RoleHint("output_power", ("output_home_power", "output_power"), ("output_home", "output_power"), "power"),
        _RoleHint("manual_mode", ("ac_mode", "manual_mode"), ("ac_mode", "manual"), None),
        _RoleHint("connection", ("connection", "connected"), ("connection", "connected"), None),
        _RoleHint("min_soc", ("min_soc",), ("min_soc",), None, ("%",)),
        _RoleHint("soc_target", ("soc_set", "soc_target"), ("soc_set", "target_soc"), None, ("%",)),
    ),
    "price": (
        _RoleHint("price_now", ("electricity_price", "current_price", "energy_price", "price"), ("price", "tariff"), "monetary", ("€/kWh", "EUR/kWh")),
        _RoleHint("price_forecast", ("electricity_price_forecast", "price_forecast", "forecast"), ("forecast", "prices"), "monetary", ("€/kWh", "EUR/kWh")),
    ),
    "solar": (
        _RoleHint("pv_power", ("solar_input_power", "pv_power", "solar_power", "dc_power"), ("pv", "solar"), "power"),
        _RoleHint("pv_energy", ("pv_energy", "solar_energy", "energy_production_today"), ("pv_energy", "solar_energy", "yield"), "energy"),
    ),
    "load": (
        _RoleHint("load_power", ("load_power", "house_power", "home_power", "consumption"), ("load", "consumption", "house_power"), "power"),
        _RoleHint("load_energy", ("load_energy", "consumption_energy"), ("load_energy", "consumption_energy"), "energy"),
        _RoleHint("temperature", ("outdoor_temperature", "temperature"), ("outdoor", "temperature"), "temperature"),
    ),
    "grid": (
        _RoleHint("grid_power", ("grid_power", "power_grid", "meter_power"), ("grid", "meter"), "power"),
        _RoleHint("grid_import", ("grid_import_power", "import_power"), ("import",), "power"),
        _RoleHint("grid_export", ("grid_export_power", "export_power"), ("export",), "power"),
    ),
}

DISCOVER_KINDS = tuple(_ROLE_HINTS)
MAX_CANDIDATES = 50


def registry_entries_for_integration(
    entries: Iterable[Mapping[str, Any]],
    platform: str | None = None,
    translation_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Entity-registry entries belonging to one integration (SPEC.md section 5).

    Matches ``platform`` exactly and, when given, one of ``translation_keys``. Unique ids are **not**
    domain-prefixed (docs/EXTERNAL_APIS.md section 5), so a unique-id prefix is deliberately not
    supported: ``registry_entries_for_integration(entries, "zendure_ha", ["input_limit"])``.
    """
    wanted = {str(key) for key in translation_keys} if translation_keys else None
    out: list[dict[str, Any]] = []
    for entry in entries:
        if platform and str(entry.get("platform") or "") != platform:
            continue
        if wanted is not None and str(entry.get("translation_key") or "") not in wanted:
            continue
        out.append(dict(entry))
    return out


def discover_candidates(
    kind: str,
    registry_entries: Iterable[Mapping[str, Any]],
    states: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    search: str | None = None,
    limit: int = MAX_CANDIDATES,
    integration: str | None = None,
) -> list[dict[str, Any]]:
    """Rank candidate entities for a discovery kind.

    ``kind`` is one of ``battery|price|solar|load|grid`` (SPEC.md section 10). Translation-key
    matches outrank entity-id text matches; the returned confidence is in ``[0, 1]`` and never 1.0
    because the user still confirms every mapping. ``limit`` is clamped to :data:`MAX_CANDIDATES`.
    """
    if kind not in _ROLE_HINTS:
        raise ConfigError(f"unknown discovery kind: {kind}")
    state_map = _normalise_states(states)
    needle = (search or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for entry in registry_entries:
        entity_id = str(entry.get("entity_id") or "")
        if not entity_id:
            continue
        if entry.get("disabled_by"):
            continue
        state = state_map.get(entity_id) or {}
        text = " ".join(
            part
            for part in (
                entity_id,
                str(entry.get("original_name") or ""),
                str(entry.get("name") or ""),
                str(state.get("attributes", {}).get("friendly_name") or ""),
            )
            if part
        ).lower()
        translation = str(entry.get("translation_key") or "").lower()
        if needle and needle not in text and needle not in translation:
            continue
        platform = str(entry.get("platform") or "")
        for hint in _ROLE_HINTS[kind]:
            confidence = _score_hint(
                hint, translation=translation, text=text, state=state, platform=platform, integration=integration
            )
            if confidence < 0.3:
                continue
            attributes = state.get("attributes") or {}
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": str(entry.get("original_name") or attributes.get("friendly_name") or entity_id),
                    "unit": attributes.get("unit_of_measurement"),
                    "device_class": attributes.get("device_class") or entry.get("device_class"),
                    "state": state.get("state"),
                    "platform": platform,
                    "suggested_role": hint.role,
                    "confidence": confidence,
                }
            )
    rows.sort(key=lambda row: (-row["confidence"], row["entity_id"], row["suggested_role"]))
    return rows[: max(1, min(int(limit), MAX_CANDIDATES))]


def suggested_mapping(
    kind: str, candidates: Iterable[Mapping[str, Any]], *, min_confidence: float = 0.5
) -> dict[str, str]:
    """Pick the best candidate per role; returns ``{role: entity_id}`` for the wizard to confirm."""
    best: dict[str, tuple[float, str]] = {}
    for row in candidates:
        role = str(row.get("suggested_role") or "")
        confidence = float(row.get("confidence") or 0.0)
        if not role or confidence < min_confidence:
            continue
        current = best.get(role)
        if current is None or confidence > current[0] or (
            confidence == current[0] and str(row.get("entity_id")) < current[1]
        ):
            best[role] = (confidence, str(row.get("entity_id")))
    return {role: entity_id for role, (_, entity_id) in best.items()}


def _normalise_states(
    states: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(states, Mapping):
        return {str(key): value for key, value in states.items()}
    return {str(item.get("entity_id")): item for item in states if item.get("entity_id")}


def _hint_unit_matches(hint: _RoleHint, unit: str) -> bool:
    if not unit:
        return False
    for expected in hint.unit:
        if unit.replace(" ", "").lower() == expected.replace(" ", "").lower():
            return True
    return False


def _score_hint(
    hint: _RoleHint,
    *,
    translation: str,
    text: str,
    state: Mapping[str, Any],
    platform: str,
    integration: str | None,
) -> float:
    score = 0.0
    if translation:
        if translation in hint.keys:
            score += 0.7
        elif any(key in translation or translation in key for key in hint.keys):
            score += 0.45
    if any(token in text for token in hint.text):
        score += 0.3
    attributes = state.get("attributes") or {}
    device_class = str(attributes.get("device_class") or "")
    if hint.device_class and device_class == hint.device_class:
        score += 0.15
    if hint.unit and _hint_unit_matches(hint, str(attributes.get("unit_of_measurement") or "")):
        score += 0.1
    if integration and platform == integration:
        score += 0.1
    if hint.role == "price_forecast" and _has_forecast_attribute(attributes):
        score += 0.25
    return round(min(score, 0.95), 2)


def _has_forecast_attribute(attributes: Mapping[str, Any]) -> bool:
    for key in ("forecast", "forecasts", "prices", "data", "price_forecast"):
        value = attributes.get(key)
        if isinstance(value, (list, tuple)) and value:
            return True
    return False
