"""HA notifications and persistent notices (SPEC section 3 ``notify.*``).

Small and synchronous: the engine calls it from its own threads, it never blocks the control loop
(it is only invoked for events), and it never raises into the caller.  Messages are built by the
engine and only ever contain generic text plus numbers -- never entity ids or configuration
secrets.
"""

from __future__ import annotations

import logging
from typing import Any

from .control.drivers.base import HAFacade
from .models import utcnow

log = logging.getLogger(__name__)

DEFAULT_DEDUPE_S = 300.0


class Notifier:
    def __init__(self, ha: HAFacade | None, settings: Any, *, clock=utcnow) -> None:
        self._ha = ha
        self._settings = settings
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    def enabled(self) -> bool:
        return bool(self._settings.get("notify.enabled", True))

    def accepts(self, event: str) -> bool:
        events = self._settings.get("notify.events") or []
        try:
            listed = [str(item) for item in events]
        except TypeError:
            return False
        return event in listed

    def send(
        self,
        event: str,
        title: str,
        message: str,
        *,
        dedupe_s: float = DEFAULT_DEDUPE_S,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Send one notification for ``event``; returns True when something was sent."""
        if not self.enabled() or not self.accepts(event):
            log.debug("notification %s skipped (disabled or not in notify.events)", event)
            return False
        if self._ha is None:
            return False
        moment = self._clock().timestamp()
        last = self._last_sent.get(event)
        if last is not None and moment - last < dedupe_s:
            return False

        sent = False
        if bool(self._settings.get("notify.persistent", True)):
            sent |= self._call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": f"sem_{event}",
                },
            )
        target = str(self._settings.get("notify.mobile_service") or "").strip()
        if target:
            domain, _, service = target.partition(".")
            if not service:
                service, domain = domain, "notify"
            sent |= self._call(domain or "notify", service, {"title": title, "message": message})
        if sent:
            self._last_sent[event] = moment
        return sent

    def test(self) -> bool:
        """POST /api/v1/notify/test: one self-describing notification."""
        if not self.enabled() or self._ha is None:
            return False
        message = "Smart Energy Manager can send notifications to this device."
        sent = self._call(
            "persistent_notification",
            "create",
            {"title": "Smart Energy Manager", "message": message, "notification_id": "sem_test"},
        )
        target = str(self._settings.get("notify.mobile_service") or "").strip()
        if target:
            domain, _, service = target.partition(".")
            if not service:
                service, domain = domain, "notify"
            sent |= self._call(domain or "notify", service, {"title": "Smart Energy Manager", "message": message})
        return sent

    def _call(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        assert self._ha is not None
        try:
            self._ha.call_service(domain, service, data)
            return True
        except Exception as exc:  # notification failures must never break the caller
            log.warning("notification %s.%s failed: %s", domain, service, exc)
            return False
