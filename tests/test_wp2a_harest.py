"""WP2A: HA REST client — URLs, retries, redaction, JSON handling (no live HA)."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.harest import HAError, HAClient, SUPERVISOR_BASE_URL  # noqa: E402
from app.models import ConfigError  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200, content_type: str = "application/json"):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(item, BaseException):
            raise item
        return item


def http_error(code: int, body: bytes = b'{"message": "boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://ha/api", code, "error", {}, io.BytesIO(body))


def test_env_resolution_supervisor_wins():
    supervisor = HAClient(env={"SUPERVISOR_TOKEN": "sup-token"}, backoff=0)
    assert supervisor.base_url == SUPERVISOR_BASE_URL
    assert supervisor.token == "sup-token"
    local = HAClient(env={"HA_URL": "http://ha.local:8123/", "HA_TOKEN": "dev-token"}, backoff=0)
    assert local.base_url == "http://ha.local:8123"
    assert local.token == "dev-token"
    assert HAClient(base_url="http://explicit:1", token="t", env={"HA_URL": "http://ignored"}).base_url == "http://explicit:1"
    with pytest.raises(ConfigError):
        HAClient(env={}).get_states()


def test_get_config_and_state():
    recorder = Recorder(FakeResponse(b'{"latitude": 0.0, "timezone": "UTC"}'), FakeResponse(b'{"state": "on"}'))
    client = HAClient("http://ha.local", "token", opener=recorder, backoff=0)
    assert client.get_config()["timezone"] == "UTC"
    assert client.get_state("sensor.example_pv_power")["state"] == "on"
    first = recorder.calls[0][0]
    assert first.full_url == "http://ha.local/config"
    assert first.get_header("Authorization") == "Bearer token"
    assert recorder.calls[0][1] == client.timeout
    assert recorder.calls[1][0].full_url == "http://ha.local/states/sensor.example_pv_power"


def test_call_service_bodies():
    recorder = Recorder(FakeResponse(b"[]"), FakeResponse(b'{"changed_states": []}'))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    client.call_service("number", "set_value", data={"value": 600}, target={"entity_id": "number.example_battery"})
    client.call_service("light", "turn_on", data={"brightness": 100}, return_response=True)
    first, second = recorder.calls[0][0], recorder.calls[1][0]
    assert first.full_url == "http://ha.local/services/number/set_value"
    # flat body: the REST API rejects a nested "target" object with 400
    assert json.loads(first.data) == {"value": 600, "entity_id": "number.example_battery"}
    assert second.full_url.endswith("/services/light/turn_on?return_response=true")
    assert json.loads(second.data) == {"brightness": 100}


def test_set_state_payload():
    recorder = Recorder(FakeResponse(b'{"entity_id": "sensor.sem_price_now"}'))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    client.set_state("sensor.sem_price_now", "0.2", attributes={"unit_of_measurement": "EUR/kWh"})
    request = recorder.calls[0][0]
    assert request.full_url == "http://ha.local/states/sensor.sem_price_now"
    assert request.method == "POST"
    assert json.loads(request.data) == {"state": "0.2", "attributes": {"unit_of_measurement": "EUR/kWh"}}


def test_get_history_and_template():
    recorder = Recorder(FakeResponse(b"[[], []]"), FakeResponse(b"2026-01-05", content_type="text/plain"))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    history = client.get_history(["sensor.example_load", "sensor.example_pv"], NOW, NOW)
    assert history == [[], []]
    url = recorder.calls[0][0].full_url
    assert url.startswith("http://ha.local/history/period/2026-01-05T12%3A00%3A00")
    assert "filter_entity_id=sensor.example_load%2Csensor.example_pv" in url
    assert "end_time=2026-01-05T12%3A00%3A00%2B00%3A00" in url
    assert client.render_template("{{ 1 + 1 }}") == "2026-01-05"


def test_retry_on_5xx_then_success():
    recorder = Recorder(http_error(500), http_error(503), FakeResponse(b'{"ok": true}'))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    assert client.get_config() == {"ok": True}
    assert len(recorder.calls) == 3


def test_no_retry_on_4xx():
    recorder = Recorder(http_error(404, b'{"message": "not found"}'))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    with pytest.raises(HAError) as err:
        client.get_state("sensor.missing")
    assert err.value.status == 404
    assert len(recorder.calls) == 1


def test_retry_on_timeout_then_raise():
    recorder = Recorder(TimeoutError(), TimeoutError(), TimeoutError())
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    with pytest.raises(HAError) as err:
        client.get_states()
    assert "timed out" in str(err.value)
    assert len(recorder.calls) == 3


def test_token_is_never_leaked_in_errors():
    recorder = Recorder(http_error(500, b"first"), http_error(500, b"second"), http_error(500, b"login failed for super-secret-token"))
    client = HAClient("http://ha.local", "super-secret-token", opener=recorder, backoff=0)
    with pytest.raises(HAError) as err:
        client.get_config()
    assert "super-secret-token" not in str(err.value)
    assert "[redacted]" in str(err.value)


def test_unreachable_is_reported_without_retry_loop():
    import urllib.error as ue

    recorder = Recorder(ue.URLError(ConnectionRefusedError(111, "refused")))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    with pytest.raises(HAError):
        client.get_config()
    assert len(recorder.calls) == 1


def test_non_json_body_falls_back_to_text():
    recorder = Recorder(FakeResponse(b"<html>oops</html>", content_type="text/html"))
    client = HAClient("http://ha.local", "t", opener=recorder, backoff=0)
    assert client.get_config() == "<html>oops</html>"
