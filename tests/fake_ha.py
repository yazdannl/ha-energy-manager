"""WP5 independent fake Home Assistant (stdlib only, no network beyond loopback).

Two surfaces share one :class:`FakeHAState`:

* :class:`FakeHAFacade` -- an in-process object with the ``get_state`` / ``call_service`` /
  ``set_state`` surface the drivers use (SPEC 9.2).  Handy for driver tests where the request
  log matters more than the HTTP layer.
* :class:`FakeHAServer` -- a real ``ThreadingHTTPServer`` serving the HA REST subset that
  ``app.harest.HAClient`` touches (``/api/config``, ``/api/states``, ``/api/services/*``,
  ``/api/history/period/*``).  Used to boot the real app against a fake HA so the API contract
  audit exercises the actual HTTP client.

Everything here is deliberately synthetic: ``sensor.example_*`` entity ids and round numbers.
No coordinates, no real instance data.  Failure modes (500s, 429 rate limits, delayed Zendure
P1 application, unreachable server) are configurable so degradation paths can be exercised.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

EXAMPLE_CONFIG: dict[str, Any] = {
    "location_name": "Example Home",
    "time_zone": "UTC",
    "currency": "EUR",
    "version": "2026.1.0",
}


class FakeHAState:
    """Mutable HA state + request log shared by both surfaces.

    Timestamps behave like the real integration: ``last_updated`` only advances when the state or
    its attributes actually *change*.  A battery sitting idle therefore keeps its old timestamp
    forever, which is exactly the case an idle battery must not be penalised for (audit finding 1).
    """

    def __init__(
        self,
        states: Mapping[str, Mapping[str, Any]] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.config: dict[str, Any] = dict(config if config is not None else EXAMPLE_CONFIG)
        self.states: dict[str, dict[str, Any]] = {}
        for key, value in (states or {}).items():
            self.states[str(key)] = dict(value)
            self.touch(str(key))
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.state_writes: list[tuple[str, Any, dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self.auth_seen: list[bool] = []
        self.history: dict[str, list[dict[str, Any]]] = {}
        # Failure injection.
        self.service_status: dict[str, int] = {}  # "domain.entity_id" or "domain.*" -> status
        self.service_hook: Callable[[str, str, dict[str, Any]], None] | None = None
        self.rate_limit: dict[str, int] = {}  # path prefix -> remaining 429 responses
        self.service_delay_s = 0.0
        # Zendure P1 emulation: writes to these number entities become visible only after
        # ``apply_after_reads`` reads of the device's measured entities (0 = immediately).
        self.apply_after_reads: dict[str, int] = {}
        self.never_apply: set[str] = set()
        self._manager: tuple[str, str, str] | None = None
        self._device_writes: dict[str, float] = {}
        self._pending_reads: dict[str, int] = {}

    @staticmethod
    def _now_iso() -> str:
        """HA-style ISO timestamp with microseconds (two changes in a second must differ)."""
        stamp = time.time()
        return (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp))
            + f".{int((stamp % 1) * 1_000_000):06d}+00:00"
        )

    def touch(self, entity_id: str) -> None:
        """Stamp ``last_updated`` for an entity (call only when its value really changed)."""
        state = self.states.get(entity_id)
        if state is not None:
            state["last_updated"] = self._now_iso()

    def write_state(
        self, entity_id: str, value: Any, attributes: Mapping[str, Any] | None = None
    ) -> None:
        """Set a state, advancing ``last_updated`` only when value or attributes changed."""
        new_attributes = dict(attributes or {})
        current = self.states.get(entity_id) or {}
        changed = (
            not current
            or current.get("state") != value
            or dict(current.get("attributes") or {}) != new_attributes
        )
        previous_stamp = current.get("last_updated")
        self.states[entity_id] = {"state": value, "attributes": new_attributes}
        if changed or not previous_stamp:
            self.touch(entity_id)
        else:
            # An unchanged write must keep the old timestamp, exactly like HA's recorder.
            self.states[entity_id]["last_updated"] = previous_stamp

    # ---------------------------------------------------------------- device emulation

    def link_manager(self, manual_power: str, grid_input: str, output_home: str) -> None:
        """Emulate SolarFlow: negative manual_power = charge (grid_input), positive = discharge."""
        self._manager = (manual_power, grid_input, output_home)

    def _apply_manual_power(self, entity_id: str, raw: float) -> None:
        if self._manager is None or entity_id != self._manager[0]:
            return
        _, grid_input, output_home = self._manager
        self.write_state(grid_input, str(max(0.0, -raw)))
        self.write_state(output_home, str(max(0.0, raw)))

    def _measured_states(self) -> list[dict[str, Any]]:
        """Current measured pair, honouring a pending (not yet applied) setpoint."""
        if self._manager is None:
            return []
        _, grid_input, output_home = self._manager
        return [self.states[grid_input], self.states[output_home]]

    def _maybe_apply_pending(self, entity_id: str) -> None:
        """Called on every read of a measured entity: P1 semantics."""
        if self._manager is None:
            return
        manual = self._manager[0]
        if entity_id not in self._manager[1:]:
            return
        if entity_id in self.never_apply:
            return
        remaining = self._pending_reads.get(manual, 0)
        raw = self._device_writes.get(manual)
        if remaining > 0 and raw is not None:
            remaining -= 1
            self._pending_reads[manual] = remaining
            if remaining == 0:
                self._apply_manual_power(manual, raw)

    # ---------------------------------------------------------------- facade surface

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        self._maybe_apply_pending(entity_id)
        state = self.states.get(entity_id)
        return dict(state) if state is not None else None

    def get_states(self) -> list[dict[str, Any]]:
        for entity_id in self._manager[1:] if self._manager else []:
            self._maybe_apply_pending(entity_id)
        return [{"entity_id": key, **value} for key, value in sorted(self.states.items())]

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    def get_history(self, entity_ids: list[str]) -> list[list[dict[str, Any]]]:
        return [
            [dict(row) for row in self.history.get(entity_id, [])] for entity_id in entity_ids
        ]

    def call_service(
        self, domain: str, service: str, data: Mapping[str, Any] | None = None
    ) -> Any:
        payload = dict(data or {})
        self.service_calls.append((domain, service, payload))
        status = self.service_status.get(f"{domain}.{payload.get('entity_id')}") or self.service_status.get(
            f"{domain}.*"
        )
        if status:
            raise FakeHAError(f"fake HA {domain}.{service} -> HTTP {status}", status=status)
        if self.service_delay_s:
            time.sleep(self.service_delay_s)
        entity_id = str(payload.get("entity_id") or "")
        if domain in ("number", "input_number"):
            value = float(payload.get("value"))
            if entity_id in self.never_apply:
                self._device_writes[entity_id] = value
            else:
                delay = int(self.apply_after_reads.get(entity_id, 0))
                if delay > 0:
                    self._device_writes[entity_id] = value
                    self._pending_reads[entity_id] = delay
                else:
                    self.write_state(entity_id, str(value))
                    self._apply_manual_power(entity_id, value)
        elif domain in ("select", "input_select"):
            self.write_state(entity_id, str(payload.get("option")))
        elif domain in ("switch", "input_boolean", "light"):
            state = "on" if service == "turn_on" else "off"
            self.write_state(entity_id, state)
        elif domain == "notify":
            pass
        elif domain in ("persistent_notification", "logbook"):
            pass
        if self.service_hook is not None:
            self.service_hook(domain, service, payload)
        return []

    def set_state(
        self, entity_id: str, state: Any, attributes: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.state_writes.append((entity_id, state, dict(attributes or {})))
        self.write_state(entity_id, state, attributes)
        return {"entity_id": entity_id, "state": state, "attributes": dict(attributes or {})}

    # ---------------------------------------------------------------- assertions helpers

    @property
    def writes(self) -> list[tuple[str, str, dict[str, Any]]]:
        return list(self.service_calls)

    def wrote_entity(self, entity_id: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.service_calls if str(call[2].get("entity_id")) == entity_id]


class FakeHAError(RuntimeError):
    """Raised for injected 4xx/5xx service failures (mirrors ``harest.HAError``)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FakeHAFacade:
    """In-process HAFacade over a :class:`FakeHAState`."""

    def __init__(self, state: FakeHAState | None = None) -> None:
        self.state = state or FakeHAState()

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        return self.state.get_state(entity_id)

    def get_states(self) -> list[dict[str, Any]]:
        return self.state.get_states()

    def get_config(self) -> dict[str, Any]:
        return self.state.get_config()

    def call_service(
        self, domain: str, service: str, data: Mapping[str, Any] | None = None
    ) -> Any:
        return self.state.call_service(domain, service, data)

    def set_state(
        self, entity_id: str, state: Any, attributes: Mapping[str, Any] | None = None
    ) -> Any:
        return self.state.set_state(entity_id, state, attributes)


class FakeHAServer:
    """Threaded HTTP server exposing the HA REST subset on loopback."""

    def __init__(self, state: FakeHAState | None = None) -> None:
        self.state = state or FakeHAState()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self, host: str = "127.0.0.1") -> "FakeHAServer":
        state = self.state
        handler = _make_handler(state)
        self._httpd = ThreadingHTTPServer((host, 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FakeHAServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _make_handler(state: FakeHAState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FakeHA"
        protocol_version = "HTTP/1.1"

        # -- helpers --------------------------------------------------------------

        def _record(self, body: bytes) -> None:
            parsed = urlsplit(self.path)
            state.requests.append(
                {
                    "method": self.command,
                    "path": parsed.path,
                    "query": {k: v[-1] for k, v in parse_qs(parsed.query).items()},
                    "body": body.decode("utf-8", "replace") if body else "",
                }
            )
            state.auth_seen.append(bool(self.headers.get("Authorization")))

        def _send(self, status: int, payload: Any = None) -> None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _rate_limited(self) -> bool:
            for prefix, remaining in state.rate_limit.items():
                if self.path.startswith(prefix) and remaining > 0:
                    state.rate_limit[prefix] = remaining - 1
                    return True
            return False

        # -- verbs ----------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            self._record(b"")
            if self._rate_limited():
                self._send(429, {"message": "rate limited (example)"})
                return
            path = self.path.split("?", 1)[0]
            relative = path[len("/api") :] if path.startswith("/api") else path
            if relative in ("/config", "/config/"):
                self._send(200, state.get_config())
                return
            if relative.rstrip("/") == "/states":
                self._send(200, state.get_states())
                return
            if relative.startswith("/states/"):
                entity_id = unquote(relative[len("/states/") :])
                entity = state.get_state(entity_id)
                if entity is None:
                    self._send(404, {"message": "Entity not found."})
                else:
                    self._send(200, {"entity_id": entity_id, **entity})
                return
            if relative.startswith("/history/period"):
                query = parse_qs(urlsplit(self.path).query)
                ids = (query.get("filter_entity_id") or [""])[-1]
                rows = state.get_history([part for part in ids.split(",") if part])
                self._send(200, rows)
                return
            if relative.rstrip("/") == "/services":
                self._send(200, [{"domain": "number", "services": {"set_value": {}}}])
                return
            self._send(404, {"message": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_body()
            self._record(body)
            if self._rate_limited():
                self._send(429, {"message": "rate limited (example)"})
                return
            path = self.path.split("?", 1)[0]
            relative = path[len("/api") :] if path.startswith("/api") else path
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                self._send(400, {"message": "invalid JSON"})
                return
            if relative.startswith("/services/"):
                parts = relative[len("/services/") :].strip("/").split("/")
                if len(parts) < 2:
                    self._send(404, {"message": "no service"})
                    return
                domain, service = unquote(parts[0]), unquote(parts[1])
                status = state.service_status.get(
                    f"{domain}.{payload.get('entity_id')}"
                ) or state.service_status.get(f"{domain}.*")
                if status:
                    self._send(int(status), {"message": "injected failure (example)"})
                    return
                try:
                    state.call_service(domain, service, payload)
                except Exception as exc:  # noqa: BLE001 - fake server: report, never crash
                    self._send(500, {"message": str(exc)[:200]})
                    return
                self._send(200, [])
                return
            if relative.startswith("/states/"):
                entity_id = unquote(relative[len("/states/") :])
                state.set_state(entity_id, payload.get("state"), payload.get("attributes"))
                self._send(201, {"entity_id": entity_id, "state": payload.get("state")})
                return
            if relative.rstrip("/") == "/template":
                self._send(200, "example-template-output")
                return
            self._send(404, {"message": "Not found"})

        def log_message(self, fmt: str, *args: Any) -> None:  # silence the test log
            pass

    return Handler


_dead_lock = threading.Lock()
_dead_socket: socket.socket | None = None
_dead_port: int | None = None


def _refuse_connections(server: socket.socket) -> None:
    """Accept and immediately reset every connection (fast, deterministic transport failure)."""
    while True:
        try:
            conn, _ = server.accept()
        except OSError:  # pragma: no cover - socket closed at interpreter exit
            return
        try:
            # SO_LINGER 0 => RST instead of a graceful FIN, so clients fail fast without waiting
            # for the request/response cycle.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:  # pragma: no cover - platform dependent
            pass
        try:
            conn.close()
        except OSError:  # pragma: no cover
            pass


def dead_ha_url() -> str:
    """A loopback URL that always refuses (for the unreachable-HA degradation case).

    The socket is *kept open* for the rest of the run: releasing the port let a later
    ``ThreadingHTTPServer`` inside the same test reuse it, so the "dead" client silently talked to
    a live fake and the test flapped (audit finding 5).  Holding it open and resetting each
    connection keeps the failure deterministic *and* fast.
    """
    global _dead_socket, _dead_port
    with _dead_lock:
        if _dead_socket is None:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(16)
            _dead_port = int(server.getsockname()[1])
            threading.Thread(
                target=_refuse_connections, args=(server,), name="fake-ha-refusing", daemon=True
            ).start()
            _dead_socket = server
    assert _dead_port is not None
    return f"http://127.0.0.1:{_dead_port}/api"
