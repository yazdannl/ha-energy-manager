"""Scheduled full-charge maintenance so the BMS can balance its cells (SPEC 8.1/11/13).

A battery management system usually balances only near the top of the pack, so a pack that never
reaches its ceiling slowly drifts out of balance.  ``maintenance.full_charge_enabled`` (off by
default) asks the optimiser to reach ``maintenance.full_charge_target_soc_pct`` every
``maintenance.full_charge_interval_days`` days, in the cheapest slots before the deadline.

This module is deliberately pure: the due date, the completion check and the status/plan blocks are
functions of the persisted per-battery record, the measured SoC and the settings.  The engine owns
the store, the lock and the alerts, so nothing here writes anything or fabricates a number.

No instance-specific value may appear here (SPEC.md R1): the module has no default entity ids,
coordinates or secrets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .models import from_iso, to_iso

__all__ = [
    "TOLERANCE_PCT",
    "OVERDUE_GRACE_HOURS",
    "OVERDUE_ALERT_REPEAT_S",
    "STATE_KIND",
    "STATE_KEY",
    "STATE_VERSION",
    "FullChargeSettings",
    "BatteryRecord",
    "resolve_zone",
    "add_days_local",
    "parse_state",
    "dump_state",
    "evaluate",
    "plan_block",
]

#: Measured SoC within this many percentage points of the target counts as "reached" (SPEC 8.1):
#: a pack rarely sits at exactly 100 % and a 1-point tolerance still means the balance window.
TOLERANCE_PCT = 1.0
#: A missed deadline is only *overdue* after this grace period, so a late cheap window still
#: completes the charge without an alert (SPEC 8.1).
OVERDUE_GRACE_HOURS = 24.0
#: How often an overdue battery may re-raise its alert: a rate limit, never silence.
OVERDUE_ALERT_REPEAT_S = 24 * 3600.0

#: Where the per-battery runtime state lives (``store.profile_get``/``profile_put``), never in the
#: user's settings (SPEC 11).
STATE_KIND = "maintenance"
STATE_KEY = "full_charge"
STATE_VERSION = 1


@dataclass(frozen=True)
class FullChargeSettings:
    """The ``maintenance.full_charge_*`` settings, validated and clamped once."""

    enabled: bool = False
    interval_days: int = 30
    target_soc_pct: float = 100.0

    @classmethod
    def from_settings(cls, settings: Any) -> "FullChargeSettings":
        try:
            interval = int(settings.get("maintenance.full_charge_interval_days", 30) or 30)
        except (TypeError, ValueError):
            interval = 30
        try:
            target = float(settings.get("maintenance.full_charge_target_soc_pct", 100.0))
        except (TypeError, ValueError):
            target = 100.0
        if not math.isfinite(target):
            target = 100.0
        return cls(
            enabled=bool(settings.get("maintenance.full_charge_enabled", False)),
            interval_days=max(1, min(365, interval)),
            target_soc_pct=max(50.0, min(100.0, target)),
        )


@dataclass(frozen=True)
class BatteryRecord:
    """Persisted runtime state of one battery's maintenance cycle."""

    battery_id: str
    last_completed: datetime | None = None
    next_due: datetime | None = None
    first_seen: datetime | None = None
    overdue_alerted_at: datetime | None = None


def resolve_zone(name: Any) -> ZoneInfo:
    """A usable timezone: the configured name, else UTC.  Never raises."""
    if isinstance(name, ZoneInfo):
        return name
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ValueError, KeyError, TypeError):
        return ZoneInfo("UTC")


def add_days_local(moment: datetime, days: int, tz: Any = None) -> datetime:
    """Add whole days keeping the *local* wall-clock time, so DST never shifts the deadline.

    ``datetime + timedelta`` is absolute-time arithmetic: across a DST change it lands an hour
    earlier or later on the clock, which would drift the deadline every spring and autumn.  Adding
    the days to the local naive value and re-attaching the zone preserves the local time of day
    (SPEC 8.1); month boundaries are just calendar arithmetic and need no special case.
    """
    zone = resolve_zone(tz)
    local = moment.astimezone(zone)
    shifted = local.replace(tzinfo=None) + timedelta(days=int(days))
    return shifted.replace(tzinfo=zone)


def parse_state(payload: Any) -> dict[str, BatteryRecord]:
    """Restore the per-battery records from a stored profile payload (never raises)."""
    out: dict[str, BatteryRecord] = {}
    if not isinstance(payload, Mapping):
        return out
    entries = payload.get("batteries")
    if not isinstance(entries, Mapping):
        return out
    for battery_id, raw in entries.items():
        if not isinstance(raw, Mapping):
            continue
        out[str(battery_id)] = BatteryRecord(
            battery_id=str(battery_id),
            last_completed=from_iso(raw.get("last_completed")),
            next_due=from_iso(raw.get("next_due")),
            first_seen=from_iso(raw.get("first_seen")),
            overdue_alerted_at=from_iso(raw.get("overdue_alerted_at")),
        )
    return out


def dump_state(records: Mapping[str, BatteryRecord], now: datetime) -> dict[str, Any]:
    """The JSON-safe profile payload for the runtime records."""

    def row(record: BatteryRecord) -> dict[str, Any]:
        return {
            "last_completed": to_iso(record.last_completed),
            "next_due": to_iso(record.next_due),
            "first_seen": to_iso(record.first_seen),
            "overdue_alerted_at": to_iso(record.overdue_alerted_at),
        }

    return {
        "version": STATE_VERSION,
        "batteries": {key: row(record) for key, record in sorted(records.items())},
        "updated_at": to_iso(now),
    }


def _at_target(soc_pct: float | None, target_soc_pct: float, tolerance_pct: float) -> bool:
    if soc_pct is None:
        return False
    try:
        value = float(soc_pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    return value >= target_soc_pct - tolerance_pct


def _advance(
    record: BatteryRecord,
    *,
    now: datetime,
    soc_pct: float | None,
    settings: FullChargeSettings,
    zone: ZoneInfo,
    tolerance_pct: float,
) -> tuple[BatteryRecord, bool, bool]:
    """Return ``(record, completed_now, changed)`` for one enabled, watched battery."""
    touched = False
    if record.next_due is None:
        # A battery with no history is due immediately: the first full charge should not wait a
        # whole interval from the moment the feature was switched on.
        record = replace(record, first_seen=record.first_seen or now, next_due=now)
        touched = True
    if _at_target(soc_pct, settings.target_soc_pct, tolerance_pct):
        already_satisfied = (
            record.last_completed is not None
            and record.next_due is not None
            and now < record.next_due
        )
        if not already_satisfied:
            record = replace(
                record,
                last_completed=now,
                next_due=add_days_local(now, settings.interval_days, zone),
                overdue_alerted_at=None,
            )
            return record, True, True
    return record, False, touched


def _row(
    record: BatteryRecord,
    *,
    now: datetime,
    soc_pct: float | None,
    settings: FullChargeSettings,
    watching: bool,
) -> dict[str, Any]:
    due_at = record.next_due
    at_target = watching and _at_target(soc_pct, settings.target_soc_pct, TOLERANCE_PCT)
    due = bool(watching and due_at is not None and now >= due_at)
    overdue = bool(due and due_at is not None and now >= due_at + timedelta(hours=OVERDUE_GRACE_HOURS))
    if not watching:
        state = "disabled"
    elif overdue:
        state = "overdue"
    elif due:
        state = "due"
    else:
        state = "scheduled"
    due_in_days = None
    if due_at is not None:
        due_in_days = round((due_at - now).total_seconds() / 86400.0, 3)
    soc_value: float | None = None
    if soc_pct is not None:
        try:
            candidate = float(soc_pct)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and math.isfinite(candidate):
            soc_value = round(candidate, 3)
    return {
        "id": record.battery_id,
        "watching": watching,
        "state": state,
        "due": due,
        "overdue": overdue,
        "at_target": at_target,
        "due_at": to_iso(due_at),
        "due_in_days": due_in_days,
        "last_completed": to_iso(record.last_completed),
        "first_seen": to_iso(record.first_seen),
        "target_soc_pct": settings.target_soc_pct,
        "interval_days": settings.interval_days,
        "soc_pct": soc_value,
    }


def evaluate(
    records: Mapping[str, BatteryRecord],
    *,
    batteries: Sequence[tuple[str, bool]],
    soc_by_id: Mapping[str, float | None],
    now: datetime,
    settings: FullChargeSettings,
    tz: Any = None,
    tolerance_pct: float = TOLERANCE_PCT,
    alert_repeat_s: float = OVERDUE_ALERT_REPEAT_S,
) -> tuple[dict[str, BatteryRecord], list[dict[str, Any]], bool, list[str], list[dict[str, Any]]]:
    """Evaluate every configured battery.

    Returns ``(records, rows, changed, completed_ids, overdue_alerts)``.  ``records`` keeps one
    entry per configured battery; ``rows`` is the per-battery status the API/UI renders; a battery
    is watched only while it is enabled *and* the feature is on.  A missing or non-finite SoC never
    completes a cycle and never enters ``rows`` as a number (SPEC 8.1: never fake completion).
    """
    zone = resolve_zone(tz)
    updated: dict[str, BatteryRecord] = {}
    rows: list[dict[str, Any]] = []
    completed: list[str] = []
    alerts: list[dict[str, Any]] = []
    changed = False
    for battery_id, enabled in batteries:
        record = records.get(battery_id) or BatteryRecord(battery_id)
        watching = bool(enabled and settings.enabled)
        soc = soc_by_id.get(battery_id)
        if watching:
            record, done, touched = _advance(
                record,
                now=now,
                soc_pct=soc,
                settings=settings,
                zone=zone,
                tolerance_pct=tolerance_pct,
            )
            changed = changed or touched
            if done:
                completed.append(battery_id)
        row = _row(record, now=now, soc_pct=soc, settings=settings, watching=watching)
        if row["overdue"]:
            last = record.overdue_alerted_at
            if last is None or (now - last).total_seconds() >= alert_repeat_s:
                record = replace(record, overdue_alerted_at=now)
                changed = True
                alerts.append(row)
        updated[battery_id] = record
        rows.append(row)
    return updated, rows, changed, completed, alerts


def plan_block(
    rows: Sequence[Mapping[str, Any]],
    settings: FullChargeSettings,
    *,
    horizon_end: datetime,
) -> dict[str, Any]:
    """The compact block a plan carries (SPEC 8.1): ``due``, ``target`` and the batteries.

    ``active`` is true when a watched battery's deadline falls at or before ``horizon_end``, which
    is exactly when the optimiser must aim for the target inside this horizon.  ``enabled`` and
    ``due``/``overdue`` stay informative when it is not active, so the UI can still say "due in
    N days" without implying the current plan already charges for it.
    """
    active: list[str] = []
    due: list[str] = []
    overdue: list[str] = []
    for row in rows:
        if not row.get("watching"):
            continue
        battery_id = str(row.get("id"))
        due_at = from_iso(row.get("due_at"))
        if due_at is not None and due_at <= horizon_end:
            active.append(battery_id)
        if row.get("due"):
            due.append(battery_id)
        if row.get("overdue"):
            overdue.append(battery_id)
    return {
        "enabled": bool(settings.enabled),
        "active": bool(active),
        "due": bool(due),
        "overdue": bool(overdue),
        "target_soc_pct": settings.target_soc_pct,
        "interval_days": settings.interval_days,
        "overdue_grace_hours": OVERDUE_GRACE_HOURS,
        "batteries": active,
        "due_batteries": due,
        "overdue_batteries": overdue,
    }
