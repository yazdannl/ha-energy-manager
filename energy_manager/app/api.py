"""HTTP JSON API (SPEC section 10, frozen contract) and static SPA serving.

Standard library only: ``http.server.ThreadingHTTPServer``.  No auth -- the add-on is reached
through Home Assistant ingress, which supplies it.  All GETs work with no batteries configured and
in ``mode: off``; responses never contain tokens, and instance entity ids only appear where the UI
needs them (the battery mapping).

Ponytail: one request handler with an explicit route table instead of a router framework; static
files are served from ``app/web`` with a normalised, traversal-rejecting join.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import posixpath
import re
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .models import (
    AccuracyReport,
    Plan,
    PlanSummary,
    ProviderStatus,
    SavingsBucket,
    SystemState,
    from_iso,
    to_iso,
)

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
API_PREFIX = "/api/v1"

#: The Lovelace card is served as one self-contained file assembled from committed sources: the
#: card's own CSS, uPlot's own stylesheet, the shared chart sheet, the vendored uPlot, charts.js and
#: the card class. The user downloads it once into /config/www/, so no add-on privilege or resource
#: registration is involved (SPEC 12, 0.28.0).
#:
#: uPlot.min.css MUST be inlined (0.29.1): a card renders in its own shadow root, so the SPA's
#: <link> to it does not reach the card. Without `.u-wrap { position: relative }`, the absolute
#: `.u-under`/`.u-over` canvases and `.u-axis` labels fall back into normal flow, so the plot painted
#: over its own legend, the axes rendered oversized and the chart was clipped by the card.
CARD_BUNDLE_CSS = ("vendor/uPlot.min.css", "sem-charts-card.css", "chart-theme.css")
CARD_BUNDLE_SOURCES = ("vendor/uPlot.min.js", "charts.js", "sem-charts-card.js")
SECRET_KEY_HINTS = ("key", "token", "secret", "password", "passwd")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------------------------
# Settings catalogue.  ``config.DEFAULTS``/``setting_descriptors`` is the source of truth for the
# keys, values and validation; this layer only maps the descriptor type onto the UI types of SPEC 10
# (bool|int|float|str|password|json|list), adds the enumerated choices and marks the keys that can
# move hardware as group ``Safety``.  A key that exists in DEFAULTS is always advertised, and no key
# outside DEFAULTS ever is (a regression test pins both directions, the UI form depends on it).
# ---------------------------------------------------------------------------------------------

#: Rows that can move hardware or change a hard limit stay in the never-folding Safety group (SPEC
#: 13.2). The control-loop tuning knobs live in the foldable Control group instead, so the settings
#: page can hide them like any other advanced row.
SAFETY_KEY_RE = re.compile(
    r"^(optimizer\.(reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export)"
    r"|control\.failsafe_on_error|protection\.cell_voltage_)"
)
UI_TYPES = {
    "choice": "str",
    "multi_choice": "list",
    "select": "str",
    "entity": "str",
    "float?": "float",
}
#: Keys that come from the add-on options (config.yaml) rather than the settings form.
ADDON_OPTION_KEYS = (
    "mode",
    "dry_run",
    "log_level",
    "currency",
    "timezone",
    "latitude_override",
    "longitude_override",
)


def _ui_type(key: str, raw: Any) -> str:
    """Descriptor type -> the UI type contract of SPEC 10."""
    if any(hint in key.lower() for hint in SECRET_KEY_HINTS):
        return "password"
    kind = str(raw or "str")
    if kind in ("bool", "int", "float", "json", "list"):
        return kind
    if kind == "str":
        return "str"
    return UI_TYPES.get(kind, "str")


def _infer_ui_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "json"
    return "str"


def _descriptor_row(key: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    group = "Safety" if SAFETY_KEY_RE.search(key) else str(descriptor.get("group") or "general").title()
    return {
        "key": key,
        "value": descriptor.get("value", descriptor.get("default")),
        "type": _ui_type(key, descriptor.get("type")),
        "default": descriptor.get("default"),
        "label": str(descriptor.get("label") or key),
        "help": str(descriptor.get("help") or ""),
        "group": group,
        "choices": list(descriptor.get("choices") or []),
        # additive (SPEC 13.3): the Settings UI folds advanced rows behind a disclosure
        "advanced": bool(descriptor.get("advanced", False)),
    }


def default_settings() -> dict[str, Any]:
    """``config.DEFAULTS`` when WP2A is importable, else ``{}`` (standalone api.py)."""
    try:
        from .config import DEFAULTS
    except ImportError:  # pragma: no cover - standalone api tests
        return {}
    return dict(DEFAULTS)


def _mask(key: str, value: Any) -> Any:
    if any(hint in key.lower() for hint in SECRET_KEY_HINTS):
        return "" if not value else "***"
    return value


def _redact_battery_rows(value: Any) -> Any:
    """Battery settings as a client may see them: mapping credentials masked, never echoed.

    The ``batteries`` setting is a list of battery objects whose ``mapping`` may carry a device
    token (driver ``zendure_native``); ``_mask`` only inspects the top-level key, so the nested
    credential needs its own pass (SPEC 3.3/3.4).
    """
    if not isinstance(value, list):
        return value
    try:
        from .config import redact_battery_mapping
    except ImportError:  # pragma: no cover - standalone api tests without WP2A's config module
        return value
    return [
        {**row, "mapping": redact_battery_mapping(row.get("mapping"))} if isinstance(row, Mapping) else row
        for row in value
    ]


def _coerce(meta: Mapping[str, Any], value: Any) -> Any:
    """Normalise one setting value; raises ``ApiError(400)``.  ``config`` validates for real."""
    kind = str(meta["type"])
    key = str(meta["key"])
    choices = [str(choice) for choice in meta.get("choices") or []]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and int(value) in (0, 1):
            return bool(int(value))
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "on", "off", "yes", "no"):
            return value.strip().lower() in ("true", "on", "yes")
        raise ApiError(400, "invalid_value", f"{key} expects a boolean")
    if kind in ("int", "float"):
        if isinstance(value, bool) or value is None or value == "":
            raise ApiError(400, "invalid_value", f"{key} expects a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_value", f"{key} expects a number") from None
        return int(number) if kind == "int" else number
    if kind == "list":
        if isinstance(value, list):
            items = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                items = []
            else:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    items = [part.strip() for part in text.split(",") if part.strip()]
                else:
                    items = parsed if isinstance(parsed, list) else [parsed]
        else:
            raise ApiError(400, "invalid_value", f"{key} expects a list")
        if choices:
            for item in items:
                if str(item) not in choices:
                    raise ApiError(
                        400, "invalid_value", f"{key} may only contain: {', '.join(choices)}"
                    )
        return items
    if kind == "json":
        if isinstance(value, (dict, list)):
            return value
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                raise ApiError(400, "invalid_value", f"{key} expects JSON") from None
        raise ApiError(400, "invalid_value", f"{key} expects JSON")
    if value is None:
        return "" if kind != "json" else {}
    text = str(value)
    if choices and text not in choices:
        raise ApiError(400, "invalid_value", f"{key} must be one of {', '.join(choices)}")
    return text


def _external_descriptors(settings: Any) -> list[Mapping[str, Any]]:
    """WP2A's ``config.setting_descriptors`` when the module is present, else nothing."""
    try:
        from .config import setting_descriptors
    except ImportError:  # standalone api.py without WP2A's config module
        return []
    try:
        return list(setting_descriptors(settings))
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("setting_descriptors failed: %s", exc)
        return []


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "value"):  # Enum
        return value.value
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _json_dump(payload: Any) -> bytes:
    return json.dumps(payload, default=_json_default, allow_nan=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------------------------


def plan_summary_dict(summary: PlanSummary) -> dict[str, Any]:
    payload = {
        "cost_estimate_eur": summary.cost_estimate_eur,
        "baseline_cost_eur": summary.baseline_cost_eur,
        "saved_eur": summary.saved_eur,
        "charged_kwh": summary.charged_kwh,
        "discharged_kwh": summary.discharged_kwh,
        "grid_charge_kwh": summary.grid_charge_kwh,
        "pv_charge_kwh": summary.pv_charge_kwh,
        "cycles": summary.cycles,
        "arbitrage_kwh": summary.arbitrage_kwh,
        "effective_step_kwh": summary.effective_step_kwh,
        "solver_iterations": summary.solver_iterations,
        "cycle_cost_eur": summary.cycle_cost_eur,
        "objective_eur": summary.objective_eur,
        "wear_eur_per_kwh": getattr(summary, "wear_eur_per_kwh", None),
    }
    # The efficiency the plan assumed (SPEC 5/8.1): the aggregate etas and the break-even sale price
    # at the plan's median buy.  Additive keys, read with ``getattr`` so a summary restored from
    # the store (which only keeps the declared fields) yields null instead of failing.
    payload["eta_charge"] = getattr(summary, "eta_charge", None)
    payload["eta_discharge"] = getattr(summary, "eta_discharge", None)
    payload["round_trip_efficiency"] = getattr(summary, "round_trip_efficiency", None)
    payload["break_even_sell_eur_per_kwh"] = getattr(summary, "break_even_sell_eur_per_kwh", None)
    # Additive (0.18.0): the cost-basis sell floor the DP enforced (SPEC 5). ``None`` when the
    # setting is off or no ledger exists yet; a summary restored from an older store yields null.
    payload["sell_floor_eur_per_kwh"] = getattr(summary, "sell_floor_eur_per_kwh", None)
    # Additive (0.17.0): scheduled full-charge maintenance.  ``None`` when a summary was restored
    # from a store written before the field existed, or when no battery is due in this horizon.
    payload["maintenance"] = getattr(summary, "maintenance", None)
    # Additive (stage 1b): the feed-in contract decision (mode, export_allowed, reason, floor).
    # ``reason`` is ``unpriced_export`` whenever the battery-sourced export leg was refused.  Read
    # with ``getattr`` so a summary restored from an older store yields null.
    payload["feed_in_mode"] = getattr(summary, "feed_in_mode", None)
    payload["feed_in_export_allowed"] = getattr(summary, "feed_in_export_allowed", None)
    payload["feed_in_reason"] = getattr(summary, "feed_in_reason", None)
    payload["feed_in_floor_eur_per_kwh"] = getattr(summary, "feed_in_floor_eur_per_kwh", None)
    # Additive (stage 3): the terminal-value mode, its per-plan verdict and the measured numerical
    # stability of the continuation row (docs/OPTIMIZER_V2.md section 4).  ``terminal_mode`` is
    # reported even when the mode is off, so eligibility is visible per plan; the rest are null
    # then, and a summary restored from the store (declared fields only) yields null as well.
    payload["terminal_mode"] = getattr(summary, "terminal_mode", None)
    payload["terminal_ok"] = getattr(summary, "terminal_ok", None)
    payload["terminal_reason"] = getattr(summary, "terminal_reason", None)
    payload["terminal_tail_hours"] = getattr(summary, "terminal_tail_hours", None)
    payload["terminal_stability_span_eur"] = getattr(summary, "terminal_stability_span_eur", None)
    payload["terminal_stability_tolerance_eur"] = getattr(
        summary, "terminal_stability_tolerance_eur", None
    )
    return payload


def plan_dict(plan: Plan | None, now: datetime, hours: int | None = None) -> dict[str, Any]:
    if plan is None:
        return {
            "generated_at": None,
            "horizon_start": None,
            "horizon_end": None,
            "slot_minutes": None,
            "feasible": False,
            "infeasible_reason": "no_plan",
            "summary": plan_summary_dict(PlanSummary()),
            "maintenance": None,
            "feed_in": None,
            "terminal": None,
            "slots": [],
            "batteries": [],
        }
    limit = now + timedelta(hours=hours) if hours else None
    slots = [slot for slot in plan.slots if limit is None or slot.t < limit]
    return {
        "generated_at": to_iso(plan.generated_at),
        "horizon_start": to_iso(plan.horizon_start),
        "horizon_end": to_iso(plan.horizon_end),
        # The plan's own resolution (SPEC 10): the UI draws its windows, captions and "now" slot
        # from this instead of assuming one, so a 15-minute grid is never read as an hourly one.
        "slot_minutes": int(plan.slot_minutes),
        "feasible": plan.feasible,
        "infeasible_reason": plan.infeasible_reason,
        "mode": plan.mode,
        "dry_run": plan.dry_run,
        "sources": dict(plan.sources),
        "summary": plan_summary_dict(plan.summary),
        # Additive (0.17.0): the same block as ``summary.maintenance`` at the top level, so a client
        # that reads the plan without walking its summary still sees why it charges to the ceiling.
        "maintenance": getattr(plan, "maintenance", None),
        # Additive (stage 1b): the same feed-in block as ``summary.feed_in_*`` plus the settlement
        # notes and, in shadow mode, the recorded v1-vs-v2 plan diff.
        "feed_in": getattr(plan, "feed_in", None),
        # Additive (stage 3): the prepared tail, the sufficiency verdict (``ok``, ``reason``,
        # ``detail``) and the measured stability.  Absent when the mode is off -- the summary still
        # reports ``terminal_mode`` there, so a client can tell "off" from "refused".
        "terminal": getattr(plan, "terminal", None),
        "slots": [
            {
                "t": to_iso(slot.t),
                "buy": slot.buy,
                "sell": slot.sell,
                "pv_kw": slot.pv_kw,
                "load_kw": slot.load_kw,
                "load_p10": slot.load_p10_kw,
                "load_p90": slot.load_p90_kw,
                "soc_kwh": slot.soc_kwh,
                "soc_pct": slot.soc_pct if slot.soc_pct is not None else _plan_soc_pct(plan, slot.soc_kwh),
                "power_w": slot.power_w,
                "action": slot.action.value,
                "reason": slot.reason,
                "price_stale": slot.price_stale,
                "pv_source": slot.pv_source or plan.sources.get("solar", "unknown"),
                "price_source": slot.price_source or plan.sources.get("price", "unknown"),
                # Additive (0.20.1, SPEC 8.16): the duration the solver modelled for this slot.
                # Full ``slot_minutes`` for every slot but a first one that began before the plan
                # was made, whose value is the remaining part of that clock slot.  The timestamp
                # above stays clock-aligned; clients size energy/cost windows from this field.
                "minutes": float(slot.minutes or plan.slot_minutes),
            }
            for slot in slots
        ],
        "batteries": [
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "share_w": entry.get("share_w"),
                # SPEC 5: this battery's own round trip, wear and break-even (its own etas/wear,
                # the plan's reference price).  Additive; the UI computes the rest itself.
                "round_trip_efficiency": entry.get("round_trip_efficiency"),
                "wear_eur_per_kwh": entry.get("wear_eur_per_kwh"),
                "break_even_sell_eur_per_kwh": entry.get("break_even_sell_eur_per_kwh"),
            }
            for entry in plan.batteries
        ],
    }


def _plan_soc_pct(plan: Plan, soc_kwh: float) -> float | None:
    capacity = 0.0
    for entry in plan.batteries:
        try:
            capacity += float(entry.get("capacity_kwh") or 0.0)
        except (TypeError, ValueError):
            continue
    if capacity <= 0:
        return None
    return round(max(0.0, min(100.0, soc_kwh / capacity * 100.0)), 2)


def plan_execution_dict(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Rounded, JSON-ready form of ``Engine.plan_execution`` (SPEC 10 additive route).

    Rounding happens here so the engine keeps raw arithmetic: the UI only ever formats these
    numbers, and a slot with no measurement keeps ``actual: null`` instead of a zero.
    """
    slots = [
        {
            "t": entry.get("t"),
            # Additive (0.20.1, SPEC 8.16): the modelled duration, so a client that turns planned
            # power into energy uses the same window the solver did (grid step when absent).
            "minutes": _round(entry.get("minutes") or payload.get("slot_minutes")),
            "dispatch": _rounded_dispatch(entry.get("dispatch")),
            "actual": _rounded_actual(entry.get("actual")),
        }
        for entry in payload.get("slots") or []
    ]
    progress = {
        key: (_round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        for key, value in (payload.get("progress") or {}).items()
    }
    floors = {
        key: (_round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        for key, value in (payload.get("floors") or {}).items()
    }
    return {
        "generated_at": payload.get("generated_at"),
        "slot_minutes": payload.get("slot_minutes"),
        "now": payload.get("now"),
        "floors": floors,
        "batteries": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "min_soc_pct": _round(item.get("min_soc_pct")),
                "floor_soc_pct": _round(item.get("floor_soc_pct")),
                "capacity_kwh": _round(item.get("capacity_kwh")),
            }
            for item in payload.get("batteries") or []
        ],
        "progress": progress,
        "slots": slots,
    }


def _rounded_dispatch(dispatch: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not dispatch:
        return None
    return {
        "ts": dispatch.get("ts"),
        "requested_w": _round(dispatch.get("requested_w")),
        "applied_w": _round(dispatch.get("applied_w")),
        "ok": bool(dispatch.get("ok")),
        "dry_run": bool(dispatch.get("dry_run")),
        "clamped": bool(dispatch.get("clamped")),
        "code": dispatch.get("code") or "",
        "reason": dispatch.get("reason") or "",
        "message": dispatch.get("message") or "",
        "records": int(dispatch.get("records") or 0),
        "batteries": [
            {
                "id": item.get("id"),
                "requested_w": _round(item.get("requested_w")),
                "applied_w": _round(item.get("applied_w")),
                "ok": bool(item.get("ok")),
            }
            for item in dispatch.get("batteries") or []
        ],
    }


def _rounded_actual(actual: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not actual:
        return None
    return {
        key: (_round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        for key, value in actual.items()
    }


def _round(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return value


def forecast_dict(kind: str, forecast: Any, now: datetime, hours: int | None = None) -> dict[str, Any]:
    limit = now + timedelta(hours=hours) if hours else None
    if forecast is None:
        return {
            "kind": kind,
            "generated_at": None,
            "source": "unavailable",
            "stale": True,
            "slots": [],
        }
    slots = [
        slot for slot in forecast.slots if limit is None or slot.t < limit
    ]
    if kind == "price":
        payload = [
            {"t": to_iso(slot.t), "buy": slot.buy, "sell": slot.sell, "is_actual": slot.is_actual, "stale": slot.stale}
            for slot in slots
        ]
    elif kind == "solar":
        payload = [
            {"t": to_iso(slot.t), "pv_kw": slot.pv_kw, "uncertainty_pct": slot.uncertainty_pct}
            for slot in slots
        ]
    else:
        payload = [
            {"t": to_iso(slot.t), "load_kw": slot.load_kw, "p10_kw": slot.p10_kw, "p90_kw": slot.p90_kw}
            for slot in slots
        ]
    out = {
        "kind": kind,
        "generated_at": to_iso(getattr(forecast, "generated_at", None)),
        "source": getattr(forecast, "source", "unknown"),
        "stale": bool(getattr(forecast, "stale", False)),
        "slots": payload,
    }
    if kind == "load":
        out["confidence"] = getattr(forecast, "confidence", "low")
    if kind == "solar":
        out["correction_applied"] = getattr(forecast, "correction_applied", 1.0)
        out["total_kwh"] = getattr(forecast, "total_kwh", lambda: 0.0)()
    return out


def provider_dict(status: ProviderStatus) -> dict[str, Any]:
    return {
        "kind": status.kind,
        "name": status.name,
        "active": status.active,
        "ok": status.ok,
        "last_success": to_iso(status.last_success),
        "last_error": status.last_error,
        "stale": status.stale,
        "detail": dict(status.detail),
        # Additive (0.12.0 multi-source solar): the configured weight, a coarse state and a
        # one-line note. The coarse state is derived only when a producer left it unset, so
        # existing providers keep exactly the same ok/active semantics.
        "weight": status.weight,
        "state": status.state or ("ok" if status.ok else ("disabled" if not status.active else "failed")),
        "note": status.note or (status.last_error or ""),
    }


def savings_bucket_dict(bucket: SavingsBucket) -> dict[str, Any]:
    return {
        "key": bucket.key,
        "start": to_iso(bucket.start),
        "end": to_iso(bucket.end),
        "import_kwh": bucket.import_kwh,
        "export_kwh": bucket.export_kwh,
        "pv_kwh": bucket.pv_kwh,
        "cost_eur": bucket.cost_eur,
        "revenue_eur": bucket.revenue_eur,
        "baseline_cost_eur": bucket.baseline_cost_eur,
        "savings_eur": bucket.savings_eur,
        "cycles": bucket.cycles,
        # Additive (0.13.0 measured savings): measured AC battery flows and their value at flow
        # time, plus the per-bucket coverage flags.  None = unknown, never a fabricated zero.
        "charged_kwh": bucket.charged_kwh,
        "discharged_kwh": bucket.discharged_kwh,
        "charge_cost_eur": bucket.charge_cost_eur,
        "discharge_value_eur": bucket.discharge_value_eur,
        "grid_measured": bucket.grid_measured,
        "load_measured": bucket.load_measured,
        "pv_measured": bucket.pv_measured,
        "battery_measured": bucket.battery_measured,
        # Additive (0.22.1): False = no stored row in this bucket (compared with a measured row
        # whose baseline is unknown).  Both carry None baseline/savings; only a data-bearing bucket
        # can appear in the baseline coverage counts.
        "has_data": bucket.has_data,
    }


def accuracy_dict(report: AccuracyReport) -> dict[str, Any]:
    return {
        "kind": report.kind,
        "days": report.days,
        "mape_pct": report.mape_pct,
        "bias_pct": report.bias_pct,
        "mae": report.mae,
        "samples": report.samples,
        "series": list(report.series),
    }


def status_dict(
    state: SystemState,
    plan: Plan | None,
    pending_holds: Sequence[str] = (),
    mode_persisted: bool = True,
    cell_voltage: Mapping[str, Any] | None = None,
    full_charge: Mapping[str, Any] | None = None,
    cost_basis: Mapping[str, Any] | None = None,
    manual_limit_caps: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    now = state.now or datetime.now(timezone.utc)
    override = None
    if state.override:
        override = dict(state.override)
        expires = state.override.get("expires_at")
        if isinstance(expires, str):
            try:
                moment = from_iso(expires)
                override["seconds_left"] = max(0.0, (moment - now).total_seconds()) if moment else None
            except ValueError:
                override["seconds_left"] = None
    current = plan.slot_at(now) if plan is not None else None
    basis_by_id = {
        str(row.get("id")): row
        for row in (cost_basis or {}).get("batteries", [])
        if isinstance(row, Mapping)
    }
    cap_by_id = {str(key): row for key, row in (manual_limit_caps or {}).items()}
    return {
        "version": state.version,
        "commit": state.commit,
        "uptime_s": round(state.uptime_s, 1),
        "mode": state.mode,
        "dry_run": state.dry_run,
        # Batteries whose last real setpoint is still live (SPEC 10): the UI can show that a
        # neutralising hold is outstanding.
        "pending_holds": list(pending_holds),
        # False when a mode change (e.g. Stop now) could not be written to the settings file: the
        # running process is already in the new mode, but a restart would come up in the old one
        # (audit finding 4).
        "mode_persisted": bool(mode_persisted),
        "ha_connected": state.ha_connected,
        "ha_last_error": state.ha_last_error,
        "location": state.location,
        "now": to_iso(now),
        "price_now": state.price_now,
        "price_rank_pct": state.price_rank_pct,
        "pv_now_w": state.pv_now_w,
        "load_now_w": state.load_now_w,
        "grid_now_w": state.grid_now_w,
        "batteries": [
            {
                "id": battery.id,
                "available": battery.available,
                "soc_pct": battery.soc_pct,
                "power_w": battery.power_w,
                "capacity_kwh": battery.capacity_kwh,
                "energy_kwh": battery.energy_kwh,
                "pv_power_w": battery.pv_power_w,
                "temperature_c": battery.temperature_c,
                "read_at": to_iso(battery.read_at),
                "age_s": battery.age_seconds(now),
                "stale": battery.stale,
                # Non-blocking diagnostics: a frozen integration whose timestamps stopped
                # advancing is visible here (and in the warning log) without blocking control
                # (audit finding 1).
                "timestamps_seen": bool((battery.extra or {}).get("timestamps_seen")),
                "stalled_polls": int((battery.extra or {}).get("stalled_polls") or 0),
                "frozen": bool((battery.extra or {}).get("frozen")),
                # Additive (0.16.0): the lowest cell voltage this battery reported, when a mapped
                # entity or the driver hook provides one.  None = never a guessed value.
                "cell_voltage_v": (battery.extra or {}).get("cell_voltage_v"),
                # Additive (0.18.0): this battery's cost-basis ledger (SPEC 5). ``None`` = no
                # ledger yet; the value of the held energy is defined once as ``basis * E``.
                "cost_basis_eur_per_kwh": (basis_by_id.get(battery.id) or {}).get(
                    "cost_basis_eur_per_kwh"
                ),
                "stored_value_eur": (basis_by_id.get(battery.id) or {}).get("stored_value_eur"),
                # Additive (0.20.0): the configured hardware maxima and the manual caps in force
                # (SPEC 5). ``cap_*`` is ``None`` when that direction is unlimited.
                "max_charge_w": (cap_by_id.get(battery.id) or {}).get("max_charge_w"),
                "max_discharge_w": (cap_by_id.get(battery.id) or {}).get("max_discharge_w"),
                "cap_charge_w": (cap_by_id.get(battery.id) or {}).get("cap_charge_w"),
                "cap_discharge_w": (cap_by_id.get(battery.id) or {}).get("cap_discharge_w"),
                "last_error": battery.last_error,
            }
            for battery in state.batteries
        ],
        # The full slot timeline is GET /api/v1/plan; /status carries the summary so the dashboard
        # can poll cheaply.
        "active_plan": None
        if plan is None
        else {
            "generated_at": to_iso(plan.generated_at),
            "horizon_start": to_iso(plan.horizon_start),
            "horizon_end": to_iso(plan.horizon_end),
            "feasible": plan.feasible,
            "infeasible_reason": plan.infeasible_reason,
            "summary": plan_summary_dict(plan.summary),
            "slot_count": len(plan.slots),
            "current": None
            if current is None
            else {
                "t": to_iso(current.t),
                "power_w": current.power_w,
                "action": current.action.value,
                "reason": current.reason,
                "soc_kwh": current.soc_kwh,
                # Additive (0.20.1, SPEC 8.16): the modelled duration of the current slot.
                "minutes": float(current.minutes or plan.slot_minutes),
            },
        },
        "last_plan_at": to_iso(state.last_plan_at),
        "last_tick_at": to_iso(state.last_tick_at),
        "next_cheap_window": [to_iso(state.next_cheap_window[0]), to_iso(state.next_cheap_window[1])]
        if state.next_cheap_window
        else None,
        "next_expensive_window": [
            to_iso(state.next_expensive_window[0]),
            to_iso(state.next_expensive_window[1]),
        ]
        if state.next_expensive_window
        else None,
        "override": override,
        # Additive (0.20.0): the raw per-direction manual caps (SPEC 5/10).  The resolved caps and
        # the configured maxima ride in every ``batteries[]`` row above.
        "manual_limits": {
            str(key): dict(entry)
            for key, entry in (getattr(state, "manual_limits", None) or {}).items()
        },
        # Additive (0.16.0): the cell-voltage guard's thresholds, latch and per-battery readings.
        "cell_voltage": dict(cell_voltage) if cell_voltage is not None else None,
        # Additive (0.17.0): scheduled full-charge maintenance (settings, per-battery state and the
        # plan-level block).  ``None`` only when the caller did not provide it.
        "full_charge": dict(full_charge) if full_charge is not None else None,
        # Additive (0.18.0): the cost-basis ledger and the fleet sell floor (SPEC 5/10). ``None``
        # only when the caller did not provide it.
        "cost_basis": dict(cost_basis) if cost_basis is not None else None,
        "alerts": [
            {
                "ts": to_iso(alert.ts),
                "level": alert.level,
                "code": alert.code,
                "message": alert.message,
                "detail": dict(alert.detail),
                "acknowledged": alert.acknowledged,
            }
            for alert in state.alerts
        ],
        "provider_status": [provider_dict(status) for status in state.provider_status],
        "scheduler_healthy": state.scheduler_healthy,
    }


def optimizer_core_status() -> dict[str, Any]:
    """Additive (0.27.0): the optimizer's numeric backend, for ``/health``.

    The accelerator is optional by design and fails open at build and import time, so ``"python"``
    is a legitimate answer rather than an error -- but it is a different code path, and which one
    is live is not otherwise observable from outside the container.  The import is local because
    the API must not depend on the optimizer package to serve a request.
    """
    try:
        from .optimize import _fast
    except Exception as exc:  # pragma: no cover - the optimizer is always packaged
        return {"backend": "python", "build_sha": None, "reason": f"{type(exc).__name__}: {exc}"}
    return _fast.status()


# ---------------------------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------------------------


class ApiServer:
    """Route table from SPEC 10 plus safe static serving from ``app/web``."""

    def __init__(
        self,
        engine: Any,
        *,
        web_root: str | None = None,
        host: str = "0.0.0.0",
        port: int = 8099,
        version: str = "0.1.0",
    ) -> None:
        self.engine = engine
        self.web_root = os.path.normpath(web_root or os.path.join(os.path.dirname(__file__), "web"))
        self.host = host
        self.port = int(port)
        self.version = version
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> "ApiServer":
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="sem-api", daemon=True)
        self._thread.start()
        log.info("API listening on %s:%s", self.host, self.port)
        return self

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is not None:
            return self._httpd.server_address[0], self._httpd.server_address[1]
        return self.host, self.port

    def serve_forever(self) -> None:
        self.start()
        assert self._httpd is not None
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            pass

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- request handling -------------------------------------------------------------

    def dispatch(self, method: str, target: str, body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
        """Return ``(status, headers, body)``; never raises for a client mistake."""
        method = method.upper()
        split = urlsplit(target)
        path = unquote(split.path or "/")
        query = {key: values[-1] for key, values in parse_qs(split.query, keep_blank_values=True).items()}
        payload: Any = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._error(400, "invalid_json", "request body must be valid JSON")
        try:
            status, headers, content = self._route(method, path, query, payload)
        except ApiError as exc:
            return self._error(exc.status, exc.code, exc.message)
        except KeyError as exc:
            return self._error(404, "not_found", f"unknown item: {exc}")
        except PermissionError as exc:
            return self._error(409, "conflict", str(exc))
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception as exc:  # pragma: no cover - unexpected: log, do not leak internals
            log.exception("unhandled API error")
            return self._error(
                500, "internal_error", f"internal error ({type(exc).__name__}); see the add-on log"
            )
        return status, headers, content

    def _error(self, status: int, code: str, message: str) -> tuple[int, dict[str, str], bytes]:
        return (
            status,
            {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            _json_dump({"error": {"code": code, "message": message}}),
        )

    def _json(self, payload: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
        return (
            status,
            {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            _json_dump(payload),
        )

    # -- routes -----------------------------------------------------------------------

    def _route(
        self, method: str, path: str, query: Mapping[str, str], payload: Any
    ) -> tuple[int, dict[str, str], bytes]:
        if path.startswith(API_PREFIX):
            return self._api(method, path[len(API_PREFIX):].strip("/"), query, payload)
        if method != "GET":
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed on {path}")
        return self._static(path)

    def _api(
        self, method: str, route: str, query: Mapping[str, str], payload: Any
    ) -> tuple[int, dict[str, str], bytes]:
        engine = self.engine
        segments = [segment for segment in route.split("/") if segment]
        now = self._now()

        if route == "health" and method == "GET":
            state = engine.snapshot()
            return self._json(
                {
                    "status": "ok",
                    "version": state.version or self.version,
                    "uptime_s": round(state.uptime_s, 1),
                    "commit": state.commit,
                    # Additive (0.27.0): which optimizer backend actually solves.  The accelerator
                    # is fail-open at build and import time, so without this a deployed image
                    # cannot be asked whether the compiled core is live -- and a stale build is
                    # refused at load, which is a state worth seeing rather than inferring.
                    "optimizer_core": optimizer_core_status(),
                }
            )

        if route == "status" and method == "GET":
            state = engine.snapshot()
            pending = getattr(engine, "pending_holds", None)
            persisted = getattr(engine, "mode_persisted", None)
            cell_voltage = getattr(engine, "cell_voltage_status", None)
            full_charge = getattr(engine, "full_charge_status", None)
            cost_basis = getattr(engine, "cost_basis_block", None)
            limits = getattr(engine, "manual_limits_view", None)
            return self._json(
                status_dict(
                    state,
                    engine.active_plan(),
                    pending() if callable(pending) else (),
                    persisted() if callable(persisted) else True,
                    cell_voltage() if callable(cell_voltage) else None,
                    full_charge() if callable(full_charge) else None,
                    cost_basis() if callable(cost_basis) else None,
                    limits() if callable(limits) else None,
                )
            )

        if route == "plan" and method == "GET":
            return self._json(
                plan_dict(engine.active_plan(), now, hours=_hours(query.get("hours"), self._default_hours()))
            )

        if route == "plan/recompute" and method == "POST":
            return self._json(engine.request_replan("api"))

        if route == "plan/execution" and method == "GET":
            # What the plan intended against what was measured and applied (SPEC 10, additive).
            return self._json(
                plan_execution_dict(
                    engine.plan_execution(
                        now, hours=_hours(query.get("hours"), self._default_hours())
                    )
                )
            )

        if route == "forecast" and method == "GET":
            kind = str(query.get("kind") or "price")
            if kind not in ("price", "solar", "load"):
                raise ApiError(400, "invalid_kind", "kind must be price, solar or load")
            forecast = engine.forecast(kind)
            payload = forecast_dict(kind, forecast, now, hours=_hours(query.get("hours"), self._default_hours()))
            self._augment_forecast_payload(payload, kind, forecast)
            return self._json(payload)

        if route == "batteries" and method == "GET":
            return self._json({"batteries": engine.batteries_view()})

        if route == "batteries" and method == "POST":
            config = self._body(payload).get("config")
            if not isinstance(config, Mapping):
                raise ApiError(400, "invalid_body", "body must be {config: {...}}")
            return self._json({"config": engine.create_battery(config)}, status=201)

        if len(segments) >= 2 and segments[0] == "batteries":
            battery_id = segments[1]
            if len(segments) == 2 and method == "PATCH":
                patch = self._body(payload).get("patch")
                if not isinstance(patch, Mapping):
                    raise ApiError(400, "invalid_body", "body must be {patch: {...}}")
                return self._json({"config": engine.update_battery(battery_id, patch)})
            if len(segments) == 2 and method == "DELETE":
                return self._json(engine.delete_battery(battery_id))
            if len(segments) == 3 and segments[2] == "test" and method == "POST":
                body = self._body(payload)
                return self._json(
                    engine.test_discharge(
                        battery_id,
                        float(body.get("power_w") or 0.0),
                        float(body.get("seconds") or 5.0),
                        str(body.get("intent") or "discharge"),
                    )
                )
            raise ApiError(404, "not_found", f"no route for {method} /{route}")

        if route == "discover" and method == "GET":
            kind = str(query.get("kind") or "battery")
            if kind not in ("battery", "price", "solar", "load", "grid"):
                raise ApiError(400, "invalid_kind", "kind must be battery, price, solar, load or grid")
            return self._json(
                {
                    "candidates": engine.discover(
                        kind, search=query.get("search"), limit=_limit(query.get("limit"), 50)
                    )
                }
            )

        if route == "config" and method == "GET":
            return self._json(self._config_payload())

        if route == "config" and method == "PATCH":
            body = self._body(payload)
            patch = body.get("settings")
            if not isinstance(patch, Mapping) or not patch:
                raise ApiError(400, "invalid_body", "body must be {settings: {...}} with at least one key")
            catalogue = self._setting_catalogue()
            coerced: dict[str, Any] = {}
            errors: list[dict[str, str]] = []
            for key in sorted(patch):
                meta = catalogue.get(str(key))
                if meta is None:
                    errors.append({"key": str(key), "message": "unknown setting"})
                    continue
                try:
                    coerced[str(key)] = _coerce(meta, patch[key])
                except ApiError as exc:
                    errors.append({"key": str(key), "message": exc.message})
            result = engine.apply_settings(coerced) if coerced else {"applied": [], "errors": []}
            errors.extend(result["errors"])
            # Per-key failures are data, not a request failure: the form renders them field by field.
            return self._json({"applied": result["applied"], "errors": errors})

        if route == "mode" and method == "POST":
            body = self._body(payload)
            mode = body.get("mode")
            dry_run = body.get("dry_run")
            if mode is None and dry_run is None:
                raise ApiError(400, "invalid_body", "mode and/or dry_run is required")
            if mode is not None and str(mode) not in ("off", "advisory", "auto"):
                raise ApiError(400, "invalid_mode", "mode must be off, advisory or auto")
            if dry_run is not None and not isinstance(dry_run, bool):
                raise ApiError(400, "invalid_body", "dry_run must be a boolean")
            # Partial update: an omitted field keeps its current value (SPEC 10).
            return self._json(engine.set_mode(None if mode is None else str(mode), dry_run))

        if route == "override" and method == "POST":
            body = self._body(payload)
            if body.get("power_w") is None:
                raise ApiError(400, "invalid_body", "power_w is required")
            return self._json(
                {
                    "override": engine.set_override(
                        float(body["power_w"]),
                        float(body.get("minutes") or 30),
                        body.get("battery_id"),
                    )
                }
            )

        if route == "override" and method == "DELETE":
            return self._json(engine.clear_override())

        if route == "limits" and method == "POST":
            body = self._body(payload)
            if "charge_w" not in body and "discharge_w" not in body:
                raise ApiError(400, "invalid_body", "charge_w and/or discharge_w is required")
            battery_id = body.get("battery_id")
            if battery_id is not None and not isinstance(battery_id, str):
                raise ApiError(400, "invalid_body", "battery_id must be a string or null")
            fields: dict[str, Any] = {}
            for key in ("charge_w", "discharge_w"):
                if key not in body:
                    continue
                value = body[key]
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise ApiError(400, "invalid_body", f"{key} must be a number or null")
                fields[key] = value
            try:
                limits = engine.set_manual_limits(battery_id, **fields)
            except ValueError as exc:
                # Above the configured maximum (or negative): a client mistake, never a clamp.
                raise ApiError(400, "invalid_body", str(exc)) from None
            return self._json({"manual_limits": limits})

        if route == "limits" and method == "DELETE":
            battery_id = str(query.get("battery_id") or "").strip() or None
            try:
                limits = engine.clear_manual_limits(battery_id)
            except ValueError as exc:  # pragma: no cover - defensive, mirrors POST
                raise ApiError(400, "invalid_body", str(exc)) from None
            return self._json({"manual_limits": limits})

        if route == "stop" and method == "POST":
            return self._json(engine.stop_now())

        if route == "savings" and method == "GET":
            range_key = str(query.get("range") or "day")
            buckets, totals = engine.savings(range_key)
            from .analytics import BASELINE_NOTE

            return self._json(
                {
                    "range": range_key,
                    "buckets": [savings_bucket_dict(bucket) for bucket in buckets],
                    "totals": totals,
                    "baseline_note": BASELINE_NOTE,
                }
            )

        if route == "accuracy" and method == "GET":
            days = _limit(query.get("days"), 14)
            reports = engine.accuracy(days)
            return self._json({kind: accuracy_dict(report) for kind, report in reports.items()})

        if route == "log" and method == "GET":
            level = query.get("level") or None
            kind = query.get("kind") or None
            if level not in (None, "info", "warning", "error"):
                raise ApiError(400, "invalid_level", "level must be info, warning or error")
            if kind not in (None, "dispatch", "event", "alert"):
                raise ApiError(400, "invalid_kind", "kind must be dispatch, event or alert")
            return self._json(
                {"entries": engine.log(_limit(query.get("limit"), 100), level=level, kind=kind)}
            )

        if route == "providers" and method == "GET":
            providers = engine.providers()
            return self._json(
                {kind: [provider_dict(status) for status in providers.get(kind, [])] for kind in ("solar", "price", "load")}
            )

        if route == "web/sem-charts-card.js" and method == "GET":
            return self._card_bundle()

        if route == "export" and method == "GET":
            what = str(query.get("what") or "plan")
            fmt = str(query.get("format") or "json")
            export = engine.export(what, fmt)
            headers = {
                "Content-Type": export["content_type"],
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{export["filename"]}"',
            }
            if fmt == "csv":
                buffer = io.StringIO()
                rows = export["rows"]
                if rows:
                    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
                content = buffer.getvalue().encode("utf-8")
            else:
                content = _json_dump({"what": export["what"], "rows": export["rows"]})
            return 200, headers, content

        if route == "notify/test" and method == "POST":
            return self._json({"sent": bool(engine.notify_test())})

        raise ApiError(404, "not_found", f"no route for {method} {API_PREFIX}/{route}")

    # -- helpers ----------------------------------------------------------------------

    def _now(self) -> datetime:
        """The engine's clock when it has one, so hour filters align with the plan horizon."""
        try:
            moment = self.engine.snapshot().now
        except Exception:  # pragma: no cover - defensive
            moment = None
        return moment or datetime.now(timezone.utc)

    def _default_hours(self) -> int:
        """Default forecast/plan window: the configured horizon (SPEC 3 ``optimizer.horizon_hours``)."""
        try:
            return int(self.engine.settings.get("optimizer.horizon_hours", 30) or 30)
        except Exception:  # pragma: no cover - defensive
            return 30

    @staticmethod
    def _body(payload: Any) -> Mapping[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, Mapping):
            raise ApiError(400, "invalid_body", "body must be a JSON object")
        return payload

    def _config_payload(self) -> dict[str, Any]:
        settings = self.engine.settings
        options: dict[str, Any] = {}
        raw_options = getattr(settings, "options", None)
        if callable(raw_options):
            try:
                raw_options = raw_options()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("settings.options() failed: %s", exc)
                raw_options = None
        if isinstance(raw_options, Mapping):
            for key, value in raw_options.items():
                if key in ADDON_OPTION_KEYS:
                    options[str(key)] = _mask(str(key), value)
        rows = self._setting_catalogue()
        for key, row in rows.items():
            row["value"] = _mask(key, row["value"])
        if "batteries" in rows:
            rows["batteries"]["value"] = _redact_battery_rows(rows["batteries"]["value"])
        return {
            "options": options,
            "settings": [rows[key] for key in sorted(rows)],
            # Additive: add-on options the stored settings override (mode/dry_run), so the UI can
            # warn that editing the option has no effect (WP2A ``Settings.option_conflicts``).
            "conflicts": self._option_conflicts(settings),
            # SPEC 4.1 (0.3.0): price provider/contract problems, reported but never fatal.
            "price_warnings": self._price_warnings(settings),
            # SPEC 3: what the Meter card resolves from the Energy dashboard, so the panel can
            # show the fallback and label a saved override instead of leaving both invisible.
            "grid": self._grid_resolution(),
        }

    def _grid_resolution(self) -> dict[str, Any]:
        """``engine.grid_resolution()`` when present, else ``{}`` (older engines)."""
        probe = getattr(self.engine, "grid_resolution", None)
        if not callable(probe):
            return {}
        try:
            return dict(probe() or {})
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("engine.grid_resolution() failed: %s", exc)
            return {}

    @staticmethod
    def _price_warnings(settings: Any) -> list[dict[str, Any]]:
        """``Settings.price_warnings()`` when present, else an empty list (SPEC 4.1)."""
        probe = getattr(settings, "price_warnings", None)
        if not callable(probe):
            return []
        try:
            warnings = probe()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("settings.price_warnings() failed: %s", exc)
            return []
        return [dict(warning) for warning in warnings or [] if isinstance(warning, Mapping)]

    def _augment_forecast_payload(self, payload: dict[str, Any], kind: str, forecast: Any) -> dict[str, Any]:
        """Add the SPEC 10 (0.3.0) forecast fields: provider, contract, notes, reason, hint.

        These are additive and present for every kind; ``provider``/``contract`` carry the effective
        price provider + contract for the price panel (the ``source`` key still names the concrete
        source), and ``reason``/``hint`` explain an empty or failed panel so the UI never has to say
        only "the source failed". Robust to an engine that predates these accessors.
        """
        engine = self.engine
        notes = list(getattr(forecast, "notes", []) or []) if forecast is not None else []
        provider: Any = None
        contract: Any = None
        provider_contract = getattr(engine, "price_provider_contract", None)
        if kind == "price" and callable(provider_contract):
            value = provider_contract() or {}
            provider = value.get("provider")
            contract = value.get("contract")
        else:
            provider = getattr(forecast, "source", None) if forecast is not None else "unavailable"
        explanation: dict[str, Any] = {}
        explain = getattr(engine, "explain_forecast", None)
        if callable(explain):
            explanation = explain(kind) or {}
        payload["provider"] = provider
        payload["contract"] = contract
        payload["notes"] = notes
        payload["reason"] = explanation.get("reason")
        payload["hint"] = explanation.get("hint")
        # Additive (0.12.0): one status per configured solar.sources merge entry. Absent for the
        # single-source path, so existing consumers see the same payload as before.
        source_rows = getattr(forecast, "sources", None)
        if isinstance(source_rows, (list, tuple)) and source_rows:
            payload["sources"] = [
                provider_dict(row) for row in source_rows if isinstance(row, ProviderStatus)
            ]
        return payload

    @staticmethod
    def _option_conflicts(settings: Any) -> list[dict[str, Any]]:
        """``Settings.option_conflicts()`` when present, else an empty list (SPEC 10)."""
        probe = getattr(settings, "option_conflicts", None)
        if not callable(probe):
            return []
        try:
            conflicts = probe()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("settings.option_conflicts() failed: %s", exc)
            return []
        return [dict(conflict) for conflict in conflicts or [] if isinstance(conflict, Mapping)]

    def _setting_catalogue(self) -> dict[str, dict[str, Any]]:
        """Every settings key of ``config.DEFAULTS``, ready for the generated UI form (SPEC 10)."""
        settings = self.engine.settings
        rows: dict[str, dict[str, Any]] = {}
        for descriptor in _external_descriptors(settings):
            key = str(descriptor.get("key") or "")
            if key:
                rows[key] = _descriptor_row(key, descriptor)
        for key, default in default_settings().items():
            if key in rows:
                continue
            try:
                value = settings.get(key, default)
            except Exception:  # pragma: no cover - defensive
                value = default
            rows[key] = _descriptor_row(key, {"default": default, "value": value, "type": _infer_ui_type(default)})
        return rows

    def _read_asset(self, relative: str) -> str:
        """Read one committed web asset by name, refusing anything that escapes the web root."""
        full = os.path.normpath(os.path.join(self.web_root, relative))
        if full != self.web_root and not full.startswith(self.web_root + os.sep):
            raise OSError(f"asset escapes the web root: {relative}")
        with open(full, "r", encoding="utf-8") as handle:
            return handle.read()

    def _card_bundle(self) -> tuple[int, dict[str, str], bytes]:
        """Assemble the Lovelace card as one file the user can download and register by hand.

        The card lives outside the add-on's origin, so it cannot load these files itself: everything
        is inlined here. uPlot is bundled as an ES module, so its `var uPlot = ...` IIFE is wrapped
        and the result assigned to the global the other two scripts read (SPEC 12, 0.28.0).
        """
        try:
            css = "\n".join(self._read_asset(name) for name in CARD_BUNDLE_CSS)
            uplot, charts_js, card_js = (self._read_asset(name) for name in CARD_BUNDLE_SOURCES)
        except OSError as exc:
            return self._error(500, "read_failed", str(exc))
        banner = "/* Generated by the Smart Energy Manager add-on - do not edit this file. */\n"
        payload = "".join(
            (
                banner,
                f"globalThis.SEM_CARD_CSS = {json.dumps(css)};\n",
                "(function (global) {\n// --- uPlot 1.6.32 (MIT) ---\n",
                uplot,
                "\nglobal.uPlot = uPlot;\n})(typeof globalThis !== 'undefined' ? globalThis : window);\n",
                "// --- charts.js ---\n",
                charts_js,
                "\n// --- sem-charts-card.js ---\n",
                card_js,
            )
        )
        headers = {
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "no-cache",
            "Content-Disposition": 'attachment; filename="sem-charts-card.js"',
        }
        return 200, headers, payload.encode("utf-8")

    def _static(self, path: str) -> tuple[int, dict[str, str], bytes]:
        if "\x00" in path or "\\" in path:
            return self._error(404, "not_found", "unknown path")
        relative = posixpath.normpath(path.lstrip("/"))
        if relative in (".", ""):
            relative = "index.html"
        if relative.startswith("..") or posixpath.isabs(relative):
            return self._error(404, "not_found", "unknown path")
        full = os.path.normpath(os.path.join(self.web_root, relative))
        if full != self.web_root and not full.startswith(self.web_root + os.sep):
            return self._error(404, "not_found", "unknown path")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._error(404, "not_found", f"no such asset: {relative}")
        extension = os.path.splitext(full)[1].lower()
        headers = {
            "Content-Type": CONTENT_TYPES.get(extension, "application/octet-stream"),
            # Revalidate every asset on every load: the Supervisor updates an add-on in place and the
            # asset URLs carry no version, so a long max-age leaves a browser showing the previous
            # release's UI for up to an hour after an update - a stale interface with no way to tell.
            # The files are local and small, so freshness is worth more than the saved bytes.
            "Cache-Control": "no-cache",
        }
        try:
            with open(full, "rb") as handle:
                return 200, headers, handle.read()
        except OSError as exc:  # pragma: no cover - defensive
            return self._error(500, "read_failed", str(exc))


def _hours(raw: Any, default: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(1, min(48, value))


def _limit(raw: Any, default: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(1, min(1000, value))


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=_json_default)
    return value


def _make_handler(app: ApiServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SmartEnergyManager"
        protocol_version = "HTTP/1.1"
        app_server = app

        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY_BYTES:
                status, headers, body = app._error(413, "too_large", "request body too large")
            else:
                body_in = self.rfile.read(length) if length else b""
                status, headers, body = app.dispatch(method, self.path, body_in)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._respond("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._respond("DELETE")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http %s", fmt % args)

    return Handler
