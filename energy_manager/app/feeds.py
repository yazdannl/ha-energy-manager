"""Compact HA chart feeds (SPEC 12, 0.28.0).

The add-on's own UI lives behind Supervisor ingress, so a Lovelace card running in the browser
cannot call its API. These feeds mirror exactly the series a chart needs onto six
``sensor.sem_chart_*`` entities, in a compact row-major form sized to stay well under Home
Assistant's 16 KB per-state attribute limit.

Schema 1 (frozen once shipped) - each feed carries ``schema: 1`` so a card can refuse a shape it
does not understand:

    plan      slots: [t, buy, sell, soc_pct, pv_kw, load_kw, power_w, stale(0|1)]
    price     slots: [t, buy, sell, stale(0|1)]
    solar     slots: [t, pv_kw, uncertainty_pct]
    load      slots: [t, load_kw, p10_kw, p90_kw]
    savings   ranges: {day|week|month|year|all: [[t, savings_eur, cost_eur, revenue_eur, baseline_cost_eur], ...]}
    accuracy  days: [t, ...]; pv/load/price: [mape_pct|null, ...]

``t`` is a Unix epoch in seconds (UTC); the card formats it in the browser's local timezone. A null
is unknown, never 0. Money is rounded to 2 dp, prices to 4, power to whole watts, percentages to
1 dp - enough for a chart and far short of the attribute ceiling.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import from_iso, to_iso

#: Bumped only for an incompatible row shape; the card checks it.
SCHEMA = 1

#: Per-series cap. 200 x 15-minute slots is 50 h, more than the 48 h horizon maximum, and keeps
#: every feed's attribute payload in the low single-digit KB.
MAX_SLOTS = 200

#: Cap on the savings feed (all five ranges share it): two years of days would be ~730 rows.
MAX_BUCKETS = 400

#: Forecast feeds are trimmed to this many hours; the plan feed follows the plan's own horizon.
FORECAST_HOURS = 36


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int) -> float | None:
    number = _num(value)
    return None if number is None else round(number, digits)


def _epoch(value: Any) -> int | None:
    moment = from_iso(value)
    return None if moment is None else int(moment.timestamp())


def _stale_slots(slots: Sequence[Any], now: datetime, hours: int, cap: int) -> list[Any]:
    limit = now + timedelta(hours=hours) if hours else None
    selected = []
    for slot in slots:
        moment = getattr(slot, "t", None)
        if limit is not None and moment is not None and moment > limit:
            break
        selected.append(slot)
        if len(selected) >= cap:
            break
    return selected


def plan_feed(
    plan: Any,
    now: datetime,
    *,
    round_trip: float | None = None,
    wear_eur_per_kwh: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """The plan timeline: price, break-even inputs, SoC and the planned battery power."""
    slots = list(getattr(plan, "slots", None) or [])
    if plan is None or not slots:
        return "empty", {"schema": SCHEMA, "slots": []}
    rows = [
        [
            _epoch(slot.t),
            _round(slot.buy, 4),
            _round(slot.sell, 4),
            _round(slot.soc_pct, 1),
            _round(slot.pv_kw, 3),
            _round(slot.load_kw, 3),
            _round(slot.power_w, 0),
            1 if getattr(slot, "price_stale", False) else 0,
        ]
        for slot in slots[:MAX_SLOTS]
    ]
    attributes = {
        "schema": SCHEMA,
        "generated_at": to_iso(getattr(plan, "generated_at", None)),
        "horizon_start": to_iso(getattr(plan, "horizon_start", None)),
        "horizon_end": to_iso(getattr(plan, "horizon_end", None)),
        "slot_minutes": int(getattr(plan, "slot_minutes", 0) or 0),
        "feasible": bool(getattr(plan, "feasible", False)),
        "infeasible_reason": getattr(plan, "infeasible_reason", None),
        # The aggregate the plan assumed, so a card draws the same break-even line as the SPA:
        # buy / round_trip + wear (SPEC 13).
        "round_trip": _num(round_trip),
        "wear_eur_per_kwh": _num(wear_eur_per_kwh),
        "slots": rows,
    }
    return ("ready" if attributes["feasible"] else "infeasible"), attributes


def forecast_feed(
    kind: str,
    forecast: Any,
    now: datetime,
    *,
    hours: int = FORECAST_HOURS,
) -> tuple[str, dict[str, Any]]:
    """A price, solar or load forecast panel in its compact row shape."""
    slots = list(getattr(forecast, "slots", None) or [])
    base: dict[str, Any] = {"schema": SCHEMA, "kind": kind, "slots": []}
    if forecast is None or not slots:
        return "empty", base
    rows: list[list[Any]] = []
    for slot in _stale_slots(slots, now, hours, MAX_SLOTS):
        if kind == "price":
            rows.append([_epoch(slot.t), _round(slot.buy, 4), _round(slot.sell, 4), 1 if getattr(slot, "stale", False) else 0])
        elif kind == "solar":
            rows.append([_epoch(slot.t), _round(slot.pv_kw, 3), _round(slot.uncertainty_pct, 1)])
        else:
            rows.append([_epoch(slot.t), _round(slot.load_kw, 3), _round(slot.p10_kw, 3), _round(slot.p90_kw, 3)])
    source = str(getattr(forecast, "source", "unknown") or "unknown")
    base.update(
        {
            "generated_at": to_iso(getattr(forecast, "generated_at", None)),
            "source": source,
            "stale": bool(getattr(forecast, "stale", False)),
            "slots": rows,
        }
    )
    return (source if rows else "empty"), base


def savings_row(bucket: Any) -> list[Any]:
    return [
        _epoch(getattr(bucket, "start", None)),
        _round(getattr(bucket, "savings_eur", None), 2),
        _round(getattr(bucket, "cost_eur", None), 2),
        _round(getattr(bucket, "revenue_eur", None), 2),
        _round(getattr(bucket, "baseline_cost_eur", None), 2),
    ]


def savings_feed(buckets_by_range: Mapping[str, Iterable[Any]]) -> tuple[str, dict[str, Any]]:
    """Savings-versus-baseline buckets for every range in one entity."""
    ranges: dict[str, list[list[Any]]] = {}
    remaining = MAX_BUCKETS
    for key in ("day", "week", "month", "year", "all"):
        rows = [savings_row(bucket) for bucket in buckets_by_range.get(key, [])]
        ranges[key] = rows[:remaining]
        remaining -= len(ranges[key])
    return ("ready" if any(ranges.values()) else "empty"), {"schema": SCHEMA, "ranges": ranges}


def accuracy_feed(reports: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Forecast accuracy per day, aligned on one day axis like the SPA's trend chart."""
    days: set[int] = set()
    per_kind: dict[str, list[Mapping[str, Any]]] = {}
    for kind in ("pv", "load", "price"):
        series = list(getattr(reports.get(kind), "series", None) or [])
        per_kind[kind] = series
        for entry in series:
            epoch = _epoch(entry.get("day") if isinstance(entry, Mapping) else None)
            if epoch is not None:
                days.add(epoch)
    axis = sorted(days)
    index = {epoch: position for position, epoch in enumerate(axis)}
    columns: dict[str, list[float | None]] = {}
    for kind in ("pv", "load", "price"):
        column: list[float | None] = [None] * len(axis)
        for entry in per_kind[kind]:
            if not isinstance(entry, Mapping):
                continue
            epoch = _epoch(entry.get("day"))
            position = index.get(epoch)
            if position is not None:
                column[position] = _round(entry.get("mape_pct"), 2)
        columns[kind] = column
    if not axis:
        return "empty", {"schema": SCHEMA, "days": [], "pv": [], "load": [], "price": []}
    attributes = {"schema": SCHEMA, "days": axis, **columns}
    return str(len(axis)), attributes
