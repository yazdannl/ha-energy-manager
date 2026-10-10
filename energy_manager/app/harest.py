"""Minimal Home Assistant REST client (SPEC.md section 10 prerequisites).

``urllib.request`` only (SPEC.md R3). Base URL and token come from the add-on environment:

* ``SUPERVISOR_TOKEN`` set -> ``http://supervisor/core/api`` with that token (add-on runtime).
* otherwise ``HA_URL`` + ``HA_TOKEN`` (local development against a real or fake HA).

Retries: at most :attr:`HAClient.retries` extra attempts (default 2), exponential backoff, and only
for 5xx responses or timeouts. 4xx errors are raised immediately. No code path logs or raises a
message containing the token.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .models import ConfigError, to_iso

log = logging.getLogger(__name__)

__all__ = ["HAError", "HAClient", "SUPERVISOR_BASE_URL"]

SUPERVISOR_BASE_URL = "http://supervisor/core/api"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_RETRIES = 2


class HAError(Exception):
    """A Home Assistant REST call failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HAClient:
    """Small synchronous Home Assistant REST client."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
        opener: Callable[..., Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if env is None else env
        supervisor_token = environment.get("SUPERVISOR_TOKEN") or ""
        if base_url is None:
            base_url = SUPERVISOR_BASE_URL if supervisor_token else (environment.get("HA_URL") or "")
        if token is None:
            token = supervisor_token or (environment.get("HA_TOKEN") or "")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT_S)
        self.retries = int(retries if retries is not None else DEFAULT_RETRIES)
        self.backoff = float(backoff)
        self._opener = opener or urllib.request.urlopen

    # ------------------------------------------------------------------ low level
    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        if not self.base_url:
            raise ConfigError("Home Assistant URL is not configured (HA_URL or SUPERVISOR_TOKEN)")
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        return url

    def _redact(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "[redacted]")
        return text

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: Mapping[str, Any] | None = None,
        expect: str | None = None,
    ) -> Any:
        """Perform one HA REST call with bounded retries; returns parsed JSON, text or ``None``."""
        url = self._url(path, params)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                response = self._opener(request, timeout=self.timeout)
                try:
                    status = getattr(response, "status", None) or response.getcode()
                    raw = response.read()
                    content_type = str(response.headers.get("Content-Type", ""))
                finally:
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                if status and status >= 400:  # defensive: rarer openers return instead of raising
                    if status >= 500 and attempt < self.retries:
                        self._sleep(attempt)
                        attempt += 1
                        continue
                    raise HAError(f"HA {method} {path} failed with HTTP {status}", status=status)
                return _decode(raw, content_type, expect)
            except urllib.error.HTTPError as err:
                detail = self._redact(_safe_read(err))
                if err.code >= 500 and attempt < self.retries:
                    self._sleep(attempt)
                    attempt += 1
                    continue
                raise HAError(
                    f"HA {method} {path} failed with HTTP {err.code}: {detail}", status=err.code
                ) from None
            except TimeoutError:
                if attempt < self.retries:
                    self._sleep(attempt)
                    attempt += 1
                    continue
                raise HAError(f"HA {method} {path} timed out after {self.timeout:g}s") from None
            except urllib.error.URLError as err:
                if isinstance(err.reason, TimeoutError) and attempt < self.retries:
                    self._sleep(attempt)
                    attempt += 1
                    continue
                raise HAError(self._redact(f"HA {method} {path} unavailable: {err.reason}")) from None

    def _sleep(self, attempt: int) -> None:
        delay = self.backoff * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    # ------------------------------------------------------------------ API
    def get_config(self) -> dict[str, Any]:
        return self.request("GET", "/config") or {}

    def get_state(self, entity_id: str) -> dict[str, Any]:
        return self.request("GET", f"/states/{urllib.parse.quote(entity_id, safe='')}") or {}

    def get_states(self) -> list[dict[str, Any]]:
        return self.request("GET", "/states") or []

    def call_service(
        self,
        domain: str,
        service: str,
        data: Mapping[str, Any] | None = None,
        target: Mapping[str, Any] | None = None,
        return_response: bool = False,
    ) -> Any:
        """``POST /api/services/<domain>/<service>`` with a FLAT body.

        The REST API reads target selectors at the top level of the service data
        (``entity_id``/``device_id``/``area_id``/``floor_id``/``label_id``); a nested ``"target"``
        object is rejected with 400 (docs/EXTERNAL_APIS.md section 4). ``target`` is therefore
        merged into ``data``, with the explicit target winning on conflicts.
        """
        body: dict[str, Any] = dict(data or {})
        if target:
            body.update(target)
        return self.request(
            "POST",
            f"/services/{urllib.parse.quote(domain, safe='')}/{urllib.parse.quote(service, safe='')}",
            payload=body,
            params={"return_response": "true"} if return_response else None,
        )

    def set_state(
        self, entity_id: str, state: str, attributes: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": state}
        if attributes:
            payload["attributes"] = dict(attributes)
        return self.request(
            "POST", f"/states/{urllib.parse.quote(entity_id, safe='')}", payload=payload
        ) or {}

    def get_history(
        self,
        entity_ids: str | Iterable[str],
        start_time: datetime | str,
        end_time: datetime | str | None = None,
        minimal: bool = False,
    ) -> list[list[dict[str, Any]]]:
        """``GET /api/history/period/<start>`` for one or more entities."""
        ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids)
        start = start_time if isinstance(start_time, str) else to_iso(start_time)
        params: dict[str, Any] = {
            "filter_entity_id": ",".join(ids),
            "minimal_response": 1 if minimal else None,
        }
        if end_time is not None:
            params["end_time"] = end_time if isinstance(end_time, str) else to_iso(end_time)
        return (
            self.request(
                "GET",
                f"/history/period/{urllib.parse.quote(start, safe='')}",
                params=params,
            )
            or []
        )

    def render_template(
        self, template: str, variables: Mapping[str, Any] | None = None
    ) -> str:
        """``POST /api/template``; returns the rendered text."""
        payload: dict[str, Any] = {"template": template}
        if variables:
            payload["variables"] = dict(variables)
        result = self.request("POST", "/template", payload=payload, expect="text")
        return "" if result is None else str(result)


def _safe_read(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", "replace")[:500]
    except Exception:  # pragma: no cover - body already consumed or unreadable
        return ""


def _decode(raw: bytes, content_type: str, expect: str | None) -> Any:
    if not raw:
        return None
    if expect == "text" or (expect is None and "json" not in content_type.lower()):
        text = raw.decode("utf-8", "replace")
        if expect == "text":
            return text
        try:
            return json.loads(text)
        except ValueError:
            return text
    return json.loads(raw.decode("utf-8"))
