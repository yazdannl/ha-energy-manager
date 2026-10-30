"""WP2B API contract tests (SPEC section 10).

The engine is a duck-typed fake that records calls, so every route, status code and payload shape
in the frozen table is exercised without threads, Home Assistant or a database.  One real socket
round-trip proves the handler wiring, and the static-file tests cover traversal rejection.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))

from app.api import ApiServer, default_settings  # noqa: E402
from app.models import (  # noqa: E402
    Action,
    AccuracyReport,
    AlertRecord,
    BatteryState,
    LoadForecast,
    LoadSlot,
    Mode,
    Plan,
    PlanSlot,
    PlanSummary,
    PriceForecast,
    PriceSlot,
    ProviderStatus,
    Reason,
    SavingsBucket,
    SolarForecast,
    SolarSlot,
    SystemState,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeSettings(dict):
    def __init__(self, **values):
        super().__init__(**values)

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value

    def all(self):
        return dict(self)

    def options(self):
        return {
            "mode": self.get("mode", "advisory"),
            "dry_run": self.get("dry_run", True),
            "log_level": "info",
            "solar.api_key": "example-secret-value",
        }


def make_plan(hours: int = 4) -> Plan:
    slots = []
    for index in range(hours * 4):
        moment = NOW + timedelta(minutes=15 * index)
        slots.append(
            PlanSlot(
                t=moment,
                buy=0.2 + 0.01 * index,
                sell=0.05,
                pv_kw=0.5,
                load_kw=0.4,
                soc_kwh=2.0,
                power_w=500.0 if index % 4 == 0 else -250.0,
                action=Action.CHARGE if index % 4 == 0 else Action.SELF_CONSUMPTION,
                reason=Reason.ARBITRAGE_CHARGE if index % 4 == 0 else Reason.SELF_CONSUMPTION,
                load_p10_kw=0.2,
                load_p90_kw=0.6,
            )
        )
    return Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=slots[-1].t + timedelta(minutes=15),
        slot_minutes=15,
        slots=slots,
        summary=PlanSummary(cost_estimate_eur=1.0, baseline_cost_eur=1.2, saved_eur=0.2),
        batteries=[{"id": "example_battery_1", "name": "Example Battery 1", "capacity_kwh": 5.0, "share_w": 500.0}],
        sources={"price": "example_price", "solar": "example_solar", "load": "example_load"},
    )


def make_status(**overrides) -> SystemState:
    state = SystemState(
        version="0.1.0",
        commit="example",
        uptime_s=12.5,
        mode=Mode.ADVISORY.value,
        dry_run=True,
        ha_connected=True,
        location="Example Street 1",
        now=NOW,
        price_now=0.21,
        price_rank_pct=42.0,
        pv_now_w=100.0,
        load_now_w=400.0,
        grid_now_w=300.0,
        batteries=[
            BatteryState(
                id="example_battery_1",
                available=True,
                soc_pct=55.0,
                power_w=0.0,
                capacity_kwh=5.0,
                energy_kwh=2.75,
                read_at=NOW,
            )
        ],
        next_cheap_window=(NOW, NOW + timedelta(hours=2)),
        next_expensive_window=(NOW + timedelta(hours=6), NOW + timedelta(hours=7)),
        alerts=[AlertRecord(ts=NOW, level="info", code="example_alert", message="example")],
        provider_status=[
            ProviderStatus(kind="solar", name="example_solar", active=True, ok=True),
            ProviderStatus(kind="price", name="example_price", active=True, ok=True),
            ProviderStatus(kind="load", name="not_configured", active=False, ok=False),
        ],
        scheduler_healthy=True,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class FakeEngine:
    """Records everything the API asks for; raises the documented errors on demand."""

    def __init__(self, *, plan=None, status=None, batteries=True):
        self.settings = FakeSettings(mode=Mode.ADVISORY.value, dry_run=True)
        self.mode_calls: list[tuple] = []
        self.override = None
        self.plan = plan if plan is not None else make_plan()
        self.status = status if status is not None else make_status()
        self.calls: list[tuple] = []
        self.batteries = (
            [
                {
                    "config": {
                        "id": "example_battery_1",
                        "name": "Example Battery 1",
                        "driver": "generic",
                        "mapping": {"power_setpoint_entity": "number.example_power"},
                    },
                    "state": {
                        "id": "example_battery_1",
                        "available": True,
                        "soc_pct": 55.0,
                        "extra": {"cycles": 3.5},
                    },
                    "capabilities": {"driver": "generic", "mapping_resolved": True},
                    "mapping_resolved": True,
                    "cycles": 3.5,
                    "discovery": [],
                }
            ]
            if batteries
            else []
        )

    def snapshot(self):
        return self.status

    def active_plan(self):
        return self.plan

    def request_replan(self, reason="api"):
        self.calls.append(("replan", reason))
        return {"queued": True, "reason": reason}

    def forecast(self, kind):
        if kind == "price":
            forecast = PriceForecast(source="example_price", generated_at=NOW)
            for index in range(8):
                forecast.slots.append(
                    PriceSlot(t=NOW + timedelta(minutes=15 * index), buy=0.2, sell=0.05, is_actual=index == 0)
                )
            return forecast
        if kind == "solar":
            forecast = SolarForecast(source="example_solar", generated_at=NOW)
            forecast.slots = [SolarSlot(t=NOW + timedelta(minutes=15 * i), pv_kw=0.5) for i in range(8)]
            return forecast
        forecast = LoadForecast(source="example_load", generated_at=NOW, confidence="medium")
        forecast.slots = [
            LoadSlot(t=NOW + timedelta(minutes=15 * i), load_kw=0.4, p10_kw=0.2, p90_kw=0.7) for i in range(8)
        ]
        return forecast

    def batteries_view(self):
        return self.batteries

    def create_battery(self, config):
        if "id" not in config:
            raise ValueError("id is required")
        self.calls.append(("create_battery", dict(config)))
        return dict(config)

    def update_battery(self, battery_id, patch):
        if battery_id != "example_battery_1":
            raise KeyError(battery_id)
        return {"id": battery_id, **patch}

    def delete_battery(self, battery_id):
        if battery_id != "example_battery_1":
            raise KeyError(battery_id)
        return {"deleted": battery_id, "hard": True}

    def test_discharge(self, battery_id, power_w, seconds, intent="discharge"):
        if battery_id != "example_battery_1":
            raise KeyError(battery_id)
        if self.status.mode != Mode.AUTO.value or self.status.dry_run:
            raise PermissionError("test discharge requires mode: auto and dry_run: false")
        return {"started": True, "battery_id": battery_id, "power_w": power_w, "seconds": seconds}

    def discover(self, kind, search=None, limit=50):
        if kind not in ("battery", "price", "solar", "load", "grid"):
            raise ValueError("unknown kind")
        rows = [
            {
                "entity_id": f"sensor.example_price_{index}",
                "name": f"Example {index}",
                "unit": "EUR/kWh",
                "device_class": "monetary",
                "state": "0.21",
                "platform": "example",
                "suggested_role": "price.entity",
                "confidence": 0.9,
            }
            for index in range(60)
        ]
        return rows[: max(1, min(50, int(limit)))]

    def apply_settings(self, patch):
        for key, value in patch.items():
            self.settings.set(key, value)
        return {"applied": sorted(patch), "errors": []}

    def set_mode(self, mode=None, dry_run=None):
        self.mode_calls.append((mode, dry_run))
        if mode is not None:
            self.status.mode = mode
        if dry_run is not None:
            self.status.dry_run = dry_run
        return {"mode": self.status.mode, "dry_run": self.status.dry_run}

    def set_override(self, power_w, minutes, battery_id=None):
        return {
            "power_w": power_w,
            "minutes": minutes,
            "battery_id": battery_id,
            "expires_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        }

    def clear_override(self):
        return {}

    def stop_now(self):
        self.status.mode = Mode.ADVISORY.value
        self.override = None
        return {
            "mode": Mode.ADVISORY.value,
            "override_cleared": True,
            "stopped": [
                {
                    "battery_id": "example_battery_1",
                    "ok": True,
                    "applied_w": 0.0,
                    "code": Reason.OK,
                    "message": "stop now",
                    "dry_run": False,
                }
            ],
        }

    def savings(self, range_key):
        if range_key not in ("day", "week", "month", "year", "all"):
            raise ValueError(f"unknown range: {range_key!r}")
        bucket = SavingsBucket(
            key="2026-01-01",
            start=NOW,
            end=NOW + timedelta(days=1),
            import_kwh=3.0,
            export_kwh=1.0,
            pv_kwh=4.0,
            cost_eur=0.6,
            revenue_eur=0.05,
            baseline_cost_eur=0.8,
            savings_eur=0.25,
            cycles=0.5,
            charged_kwh=2.0,
            discharged_kwh=1.5,
            charge_cost_eur=0.3,
            discharge_value_eur=0.08,
            grid_measured=True,
            load_measured=True,
            pv_measured=True,
            battery_measured=True,
        )
        return [bucket], {
            "import_kwh": 3.0,
            "savings_eur": 0.25,
            "buckets": 1.0,
            "cycles": 0.5,
            "arbitrage_kwh": 0.75,
            "self_consumption_kwh": 1.25,
            "charged_kwh": 2.0,
            "discharged_kwh": 1.5,
            "charge_cost_eur": 0.3,
            "discharge_value_eur": 0.08,
            "measured_buckets": {"grid": 1, "load": 1, "pv": 1, "battery": 1},
        }

    def accuracy(self, days):
        return {
            "pv": AccuracyReport(kind="pv", days=days, mape_pct=12.0, samples=10),
            "load": AccuracyReport(kind="load", days=days, samples=0),
            "price": AccuracyReport(kind="price", days=days, mae=0.01, samples=5),
        }

    def forecast_vintage_scorecard(self, days, *, timezone_name="UTC"):
        return {"days": days, "timezone": timezone_name, "matched_samples": 0}

    def log(self, limit, level=None, kind=None):
        def entry(index):
            kind = ("dispatch", "event", "alert")[index % 3]
            detail = {"index": index}
            if kind == "dispatch":
                detail.update(
                    {
                        "requested_w": 500.0,
                        "applied_w": 0.0,
                        "battery_id": "example_battery_1",
                        "mode": "advisory",
                        "dry_run": True,
                    }
                )
            return {
                "ts": "2026-01-01T12:00:00+00:00",
                "kind": kind,
                "level": "info",
                "code": Reason.OK,
                "message": "example",
                "detail": detail,
            }

        entries = [entry(index) for index in range(min(limit, 120))]
        if level:
            entries = [entry for entry in entries if entry["level"] == level]
        if kind:
            entries = [entry for entry in entries if entry["kind"] == kind]
        return entries

    def providers(self):
        return {
            "solar": [ProviderStatus(kind="solar", name="example_solar", active=True, ok=True)],
            "price": [ProviderStatus(kind="price", name="example_price", active=True, ok=True)],
            "load": [ProviderStatus(kind="load", name="not_configured", active=False, ok=False)],
        }

    def export(self, what, fmt):
        if what not in ("plan", "actuals", "savings"):
            raise ValueError(f"unknown export: {what!r}")
        if fmt not in ("json", "csv"):
            raise ValueError(f"unknown format: {fmt!r}")
        return {
            "what": what,
            "rows": [{"t": "2026-01-01T12:00:00+00:00", "power_w": 500.0, "note": "a, b"}],
            "filename": f"energy-manager-{what}-20260101-120000.{fmt}",
            "content_type": "application/json" if fmt == "json" else "text/csv",
        }

    def notify_test(self):
        self.calls.append(("notify_test",))
        return True


def server(engine=None, web_root=None) -> ApiServer:
    return ApiServer(engine or FakeEngine(), web_root=web_root, host="127.0.0.1", port=0)


def call(app: ApiServer, method: str, target: str, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    status, headers, content = app.dispatch(method, target, payload)
    parsed = None
    if headers.get("Content-Type", "").startswith("application/json") and content:
        parsed = json.loads(content)
    return status, headers, content, parsed


def test_health_and_status_shapes():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/health")
    assert status == 200 and body["status"] == "ok" and body["version"] == "0.1.0"
    # Additive (0.27.0): the health payload names the optimizer's numeric backend, because the
    # accelerator is optional and fails open, so nothing else outside the container can tell
    # which code path is live.  The digest is reported only when the accelerator really is live.
    core = body["optimizer_core"]
    assert core["backend"] in ("c", "python")
    assert (core["build_sha"] is not None) == (core["backend"] == "c")
    assert isinstance(core["reason"], str) or core["reason"] is None
    if core["backend"] == "python":
        assert core["reason"], "a backend that is not the accelerator must say why"

    status, _headers, _content, body = call(app, "GET", "/api/v1/status")
    assert status == 200
    for key in (
        "version",
        "uptime_s",
        "mode",
        "dry_run",
        "ha_connected",
        "location",
        "now",
        "price_now",
        "price_rank_pct",
        "pv_now_w",
        "load_now_w",
        "grid_now_w",
        "batteries",
        "active_plan",
        "last_plan_at",
        "last_tick_at",
        "next_cheap_window",
        "next_expensive_window",
        "override",
        "alerts",
        "provider_status",
        "scheduler_healthy",
    ):
        assert key in body, key
    assert body["batteries"][0]["id"] == "example_battery_1"
    assert "number.example_power" not in json.dumps(body)  # never leak instance entity ids


def test_plan_route_shape_and_hour_filter():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan?hours=36")
    assert status == 200
    assert set(body) >= {
        "generated_at",
        "horizon_start",
        "horizon_end",
        "feasible",
        "summary",
        "slots",
        "batteries",
    }
    assert len(body["slots"]) == 16
    slot = body["slots"][0]
    for key in (
        "t",
        "buy",
        "sell",
        "pv_kw",
        "load_kw",
        "load_p10",
        "load_p90",
        "soc_kwh",
        "soc_pct",
        "power_w",
        "action",
        "reason",
        "price_stale",
        "pv_source",
        "price_source",
    ):
        assert key in slot, key
    assert slot["soc_pct"] == 40.0  # 2.0 kWh of 5.0 kWh capacity
    assert body["summary"]["saved_eur"] == 0.2
    assert body["batteries"] == [
        {
            "id": "example_battery_1",
            "name": "Example Battery 1",
            "share_w": 500.0,
            # SPEC 5: additive per-battery efficiency/wear keys (null for a hand-built plan)
            "round_trip_efficiency": None,
            "wear_eur_per_kwh": None,
            "break_even_sell_eur_per_kwh": None,
        }
    ]

    filtered = call(app, "GET", "/api/v1/plan?hours=1")[3]
    assert len(filtered["slots"]) == 4
    clamped = call(app, "GET", "/api/v1/plan?hours=999")[3]
    assert len(clamped["slots"]) == 16


def test_plan_route_works_with_no_plan():
    engine = FakeEngine(plan=None)
    engine.plan = None
    app = server(engine)
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan")
    assert status == 200
    assert body["feasible"] is False and body["slots"] == [] and body["summary"]["saved_eur"] == 0.0
    assert body["feed_in"] is None
    assert body["terminal"] is None


def test_plan_and_summary_expose_the_feed_in_decision():
    from app.api import plan_dict, plan_summary_dict

    summary = PlanSummary()
    summary.feed_in_mode = "saldering"
    summary.feed_in_export_allowed = False
    summary.feed_in_reason = Reason.UNPRICED_EXPORT
    summary.feed_in_floor_eur_per_kwh = 0.1234
    payload = plan_summary_dict(summary)
    assert payload["feed_in_mode"] == "saldering"
    assert payload["feed_in_export_allowed"] is False
    assert payload["feed_in_reason"] == Reason.UNPRICED_EXPORT
    assert payload["feed_in_floor_eur_per_kwh"] == 0.1234
    plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=1),
        slot_minutes=60,
    )
    plan.feed_in = {
        "mode": "saldering",
        "export_allowed": False,
        "reason": Reason.UNPRICED_EXPORT,
        "floor_eur_per_kwh": 0.1234,
    }
    assert plan_dict(plan, NOW)["feed_in"]["reason"] == Reason.UNPRICED_EXPORT
    # A summary restored from an older store has no feed-in keys: they read null, never a guess.
    assert plan_summary_dict(PlanSummary())["feed_in_reason"] is None


def test_plan_and_summary_expose_the_terminal_verdict():
    from app.api import plan_dict, plan_summary_dict

    summary = PlanSummary()
    summary.terminal_mode = "vector"
    summary.terminal_ok = False
    summary.terminal_reason = "floor_binds"
    summary.terminal_tail_hours = 24.0
    summary.terminal_stability_span_eur = 1e-15
    summary.terminal_stability_tolerance_eur = 0.0045
    payload = plan_summary_dict(summary)
    assert payload["terminal_mode"] == "vector"
    assert payload["terminal_ok"] is False
    assert payload["terminal_reason"] == "floor_binds"
    assert payload["terminal_tail_hours"] == 24.0
    assert payload["terminal_stability_span_eur"] == 1e-15
    assert payload["terminal_stability_tolerance_eur"] == 0.0045
    plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=1),
        slot_minutes=60,
    )
    plan.terminal = {
        "mode": "vector",
        "ok": False,
        "reason": "floor_binds",
        "detail": {"floor": "binds"},
    }
    assert plan_dict(plan, NOW)["terminal"]["reason"] == "floor_binds"
    assert plan_dict(plan, NOW)["terminal"]["detail"] == {"floor": "binds"}
    # The mode-off plan carries no block, and a summary restored from an older store has no
    # terminal keys: both read null, never a guess.
    other = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW + timedelta(hours=1),
        slot_minutes=60,
    )
    assert plan_dict(other, NOW)["terminal"] is None
    assert plan_summary_dict(PlanSummary())["terminal_mode"] is None
    assert plan_summary_dict(PlanSummary())["terminal_ok"] is None


def test_recompute_route():
    app = server()
    status, _headers, _content, body = call(app, "POST", "/api/v1/plan/recompute")
    assert status == 200 and body == {"queued": True, "reason": "api"}


def test_forecast_routes():
    app = server()
    for kind in ("price", "solar", "load"):
        status, _headers, _content, body = call(app, "GET", f"/api/v1/forecast?kind={kind}&hours=36")
        assert status == 200
        assert body["kind"] == kind
        assert set(body) >= {"kind", "generated_at", "source", "stale", "slots"}
        assert body["slots"]
    status, _headers, _content, body = call(app, "GET", "/api/v1/forecast?kind=example")
    assert status == 400 and body["error"]["code"] == "invalid_kind"


def test_battery_routes():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/batteries")
    assert status == 200
    entry = body["batteries"][0]
    assert set(entry) == {"config", "state", "capabilities", "mapping_resolved", "discovery", "cycles"}

    status, _headers, _content, body = call(
        app,
        "POST",
        "/api/v1/batteries",
        {"config": {"id": "example_battery_2", "name": "Example Battery 2", "driver": "generic"}},
    )
    assert status == 201 and body["config"]["id"] == "example_battery_2"

    status, _headers, _content, body = call(app, "POST", "/api/v1/batteries", {})
    assert status == 400 and body["error"]["code"] == "invalid_body"

    status, _headers, _content, body = call(app, "POST", "/api/v1/batteries", {"config": {"name": "x"}})
    assert status == 400

    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/batteries/example_battery_1", {"patch": {"max_charge_w": 900.0}}
    )
    assert status == 200 and body["config"]["max_charge_w"] == 900.0

    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/batteries/example_missing", {"patch": {"max_charge_w": 1.0}}
    )
    assert status == 404 and body["error"]["code"] == "not_found"

    status, _headers, _content, body = call(app, "DELETE", "/api/v1/batteries/example_battery_1")
    assert status == 200 and body["deleted"] == "example_battery_1"

    status, _headers, _content, body = call(app, "DELETE", "/api/v1/batteries/example_missing")
    assert status == 404


def test_test_discharge_conflict_and_success():
    app = server()
    status, _headers, _content, body = call(
        app, "POST", "/api/v1/batteries/example_battery_1/test", {"power_w": -300, "seconds": 5}
    )
    assert status == 409
    assert "auto" in body["error"]["message"]
    assert body["error"]["code"] == "conflict"

    engine = FakeEngine()
    engine.status.mode = Mode.AUTO.value
    engine.status.dry_run = False
    app = server(engine)
    status, _headers, _content, body = call(
        app, "POST", "/api/v1/batteries/example_battery_1/test", {"power_w": -300, "seconds": 5}
    )
    assert status == 200 and body["started"] is True and body["seconds"] == 5.0

    status, _headers, _content, body = call(
        app, "POST", "/api/v1/batteries/example_missing/test", {"power_w": -300}
    )
    assert status == 404


def test_discover_route_is_capped_and_filtered():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/discover?kind=price")
    assert status == 200 and len(body["candidates"]) == 50
    candidate = body["candidates"][0]
    assert set(candidate) == {
        "entity_id",
        "name",
        "unit",
        "device_class",
        "state",
        "platform",
        "suggested_role",
        "confidence",
    }
    tiny = call(app, "GET", "/api/v1/discover?kind=price&limit=3")[3]
    assert len(tiny["candidates"]) == 3
    assert call(app, "GET", "/api/v1/discover?kind=example")[0] == 400


def test_config_get_masks_secrets_and_publishes_metadata():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    assert status == 200
    # options only ever expose the add-on option keys (SPEC 3)
    assert set(body["options"]) <= {
        "mode",
        "dry_run",
        "log_level",
        "currency",
        "timezone",
        "latitude_override",
        "longitude_override",
    }
    for key in ("mode", "dry_run", "log_level"):
        assert key in body["options"]  # the keys this fake actually supplies
    keys = {row["key"] for row in body["settings"]}
    for expected in (
        "price.source",
        "price.static_schedule",
        "solar.planes",
        "solar.api_key",
        "load.mode",
        "grid.power_entity",
        "grid.import_entity",
        "grid.export_entity",
        "optimizer.soc_step_kwh",
        "optimizer.target_soc_pct_end",
        "control.stale_state_seconds",
        "notify.events",
        "batteries",
        "mode",
        "dry_run",
    ):
        assert expected in keys, expected
    allowed_types = {"bool", "int", "float", "str", "password", "json", "list"}
    for row in body["settings"]:
        assert set(row) == {
            "key",
            "value",
            "type",
            "default",
            "label",
            "help",
            "group",
            "choices",
            "advanced",
        }
        assert row["type"] in allowed_types, row
        assert row["group"]
        if row["type"] == "list" and row["choices"]:
            assert isinstance(row["choices"], list)
    # a password field is masked and typed as such
    secret = next(row for row in body["settings"] if row["key"] == "solar.api_key")
    assert secret["type"] == "password" and secret["value"] in ("", "***")
    # the enumerated settings carry their choices
    assert "off" in next(r for r in body["settings"] if r["key"] == "mode")["choices"]
    assert next(r for r in body["settings"] if r["key"] == "notify.events")["type"] == "list"
    # hardware-moving keys are grouped for the confirmation step (SPEC 10); the control-loop tuning
    # knobs live in the foldable Control group, the fail-safe stays in the never-folding Safety group
    security_group = {
        r["key"]: r["group"] for r in body["settings"] if r["key"].startswith("control.")
    }
    assert set(security_group.values()) == {"Control", "Safety"}
    assert security_group["control.failsafe_on_error"] == "Safety"
    assert security_group["control.tick_seconds"] == "Control"
    for key in (
        "optimizer.max_grid_import_w",
        "optimizer.max_grid_export_w",
        "optimizer.reserve_soc_pct",
        "optimizer.allow_export",
    ):
        assert next(r for r in body["settings"] if r["key"] == key)["group"] == "Safety"


def test_config_rows_carry_the_advanced_folding_flag():
    """SPEC 13.2/13.3: advanced rows are folded in the UI, essential ones are not."""
    app = server()
    _status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    rows = {row["key"]: row for row in body["settings"]}
    assert rows["price.source"]["advanced"] is True
    assert rows["batteries"]["advanced"] is True
    assert rows["grid.import_entity"]["advanced"] is True
    assert rows["price.contract"]["advanced"] is True
    assert rows["mode"]["advanced"] is False
    assert rows["price.provider"]["advanced"] is False
    assert rows["grid.power_entity"]["advanced"] is False
    assert rows["price.feed_in_energy_tax_refund"]["advanced"] is False
    # every row is classified and the group override is untouched by the flag
    assert all(isinstance(row["advanced"], bool) for row in rows.values())
    assert rows["price.source"]["group"] == "Price" and rows["price.source"]["label"] == "Price source"
    assert rows["control.failsafe_on_error"]["group"] == "Safety"
    assert all(
        row["group"] == "Control" for row in rows.values()
        if row["key"].startswith("control.") and row["key"] != "control.failsafe_on_error"
    )


def test_config_catalogue_matches_defaults_exactly():
    """The UI form is generated from GET /config: it must be exactly the DEFAULTS key set."""
    defaults = default_settings()
    if not defaults:  # pragma: no cover - WP2A absent
        return
    app = server()
    _status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    advertised = {row["key"] for row in body["settings"]}
    assert advertised == set(defaults), {
        "missing": sorted(set(defaults) - advertised),
        "extra": sorted(advertised - set(defaults)),
    }


def test_config_patch_validates_keys_and_types():
    app = server()
    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"optimizer.horizon_hours": "24"}}
    )
    assert status == 200 and body["applied"] == ["optimizer.horizon_hours"]

    # Unknown/invalid keys are reported per key with a 200 so the form can render them (SPEC 10).
    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"example.unknown": 1}}
    )
    assert status == 200 and body["applied"] == []
    assert body["errors"] == [{"key": "example.unknown", "message": "unknown setting"}]

    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"dry_run": "maybe"}}
    )
    assert status == 200 and body["applied"] == [] and body["errors"][0]["key"] == "dry_run"

    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"mode": "example"}}
    )
    assert status == 200 and body["errors"][0]["key"] == "mode"

    # a good key next to a bad one still applies
    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"mode": "example", "dry_run": False}}
    )
    assert status == 200 and body["applied"] == ["dry_run"]
    assert [error["key"] for error in body["errors"]] == ["mode"]

    status, _headers, _content, body = call(app, "PATCH", "/api/v1/config", {"settings": "nope"})
    assert status == 400 and body["error"]["code"] == "invalid_body"
    status, _headers, _content, body = call(app, "PATCH", "/api/v1/config", {"settings": {}})
    assert status == 400 and body["error"]["code"] == "invalid_body"

    status, _headers, _content, body = call(
        app, "PATCH", "/api/v1/config", {"settings": {"optimizer.target_soc_pct_end": None}}
    )
    assert status == 200


def test_mode_override_and_stop_routes():
    app = server()
    status, _headers, _content, body = call(app, "POST", "/api/v1/mode", {"mode": "auto", "dry_run": True})
    assert status == 200 and body == {"mode": "auto", "dry_run": True}
    # partial update: an omitted field stays unchanged (SPEC 10)
    status, _headers, _content, body = call(app, "POST", "/api/v1/mode", {"dry_run": False})
    assert status == 200 and body == {"mode": "auto", "dry_run": False}
    assert app.engine.mode_calls[-1] == (None, False)
    status, _headers, _content, body = call(app, "POST", "/api/v1/mode", {"mode": "advisory"})
    assert status == 200 and body == {"mode": "advisory", "dry_run": False}
    assert app.engine.mode_calls[-1] == ("advisory", None)
    assert call(app, "POST", "/api/v1/mode", {"mode": "example"})[0] == 400
    assert call(app, "POST", "/api/v1/mode", {})[0] == 400

    status, _headers, _content, body = call(app, "POST", "/api/v1/override", {"power_w": 500, "minutes": 30})
    assert status == 200 and body["override"]["power_w"] == 500.0
    assert call(app, "POST", "/api/v1/override", {"minutes": 30})[0] == 400
    status, _headers, _content, body = call(app, "DELETE", "/api/v1/override")
    assert status == 200 and body == {}

    status, _headers, _content, body = call(app, "POST", "/api/v1/stop")
    assert status == 200 and body["mode"] == "advisory"
    assert body["override_cleared"] is True
    assert set(body) == {"mode", "override_cleared", "stopped"}
    entry = body["stopped"][0]
    assert set(entry) == {"battery_id", "ok", "applied_w", "code", "message", "dry_run"}
    assert entry["ok"] is True and entry["applied_w"] == 0.0 and entry["dry_run"] is False


def test_savings_accuracy_log_and_providers_routes():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/savings?range=week")
    assert status == 200
    assert body["range"] == "week"
    assert body["baseline_note"].startswith("Baseline:")
    assert body["buckets"][0]["key"] == "2026-01-01"
    assert body["totals"]["savings_eur"] == 0.25
    # SPEC 11 (0.13.0): measured battery flows and per-bucket coverage travel additively.
    assert body["buckets"][0]["charged_kwh"] == 2.0
    assert body["buckets"][0]["discharged_kwh"] == 1.5
    assert body["buckets"][0]["battery_measured"] is True
    assert body["buckets"][0]["grid_measured"] is True
    assert body["totals"]["discharge_value_eur"] == 0.08
    assert body["totals"]["measured_buckets"]["battery"] == 1
    # SPEC 10: the split the UI explains the savings with
    assert body["totals"]["arbitrage_kwh"] == 0.75
    assert body["totals"]["self_consumption_kwh"] == 1.25
    assert call(app, "GET", "/api/v1/savings?range=example")[0] == 400

    status, _headers, _content, body = call(app, "GET", "/api/v1/accuracy?days=14")
    assert status == 200 and set(body) == {"pv", "load", "price", "pv_vintage"}
    assert body["pv"]["mape_pct"] == 12.0 and body["load"]["samples"] == 0
    assert body["pv_vintage"] == {"days": 14, "timezone": "UTC", "matched_samples": 0}

    status, _headers, _content, body = call(app, "GET", "/api/v1/log?limit=10")
    assert status == 200 and len(body["entries"]) == 10
    assert set(body["entries"][0]) >= {"ts", "kind", "level", "code", "message", "detail"}
    for kind in ("dispatch", "event", "alert"):
        status, _headers, _content, body = call(app, "GET", f"/api/v1/log?kind={kind}")
        assert status == 200
    assert call(app, "GET", "/api/v1/log?level=example")[0] == 400
    assert call(app, "GET", "/api/v1/log?kind=example")[0] == 400

    status, _headers, _content, body = call(app, "GET", "/api/v1/providers")
    assert status == 200 and set(body) == {"solar", "price", "load"}
    assert body["load"][0]["active"] is False


def test_plan_slots_carry_the_documented_fields():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan?hours=12")
    assert status == 200 and body["slots"]
    slot = body["slots"][0]
    for key in (
        "t",
        "buy",
        "sell",
        "pv_kw",
        "load_kw",
        "load_p10",
        "load_p90",
        "soc_kwh",
        "soc_pct",
        "power_w",
        "action",
        "reason",
        "price_stale",
        "pv_source",
        "price_source",
    ):
        assert key in slot, key
    assert slot["soc_pct"] is not None and 0.0 <= slot["soc_pct"] <= 100.0
    assert slot["pv_source"] and slot["price_source"]
    assert body["batteries"] and {"id", "name", "share_w"} <= set(body["batteries"][0])


def test_plan_horizon_default_comes_from_the_settings():
    """SPEC 3/10: no hardcoded horizon -- the default follows ``optimizer.horizon_hours``."""
    engine = FakeEngine()
    engine.settings.set("optimizer.horizon_hours", 12)
    app = server(engine)
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan")
    assert status == 200 and len(body["slots"]) == 16  # the 4 h fake plan is not truncated
    engine.settings.set("optimizer.horizon_hours", 1)
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan")
    assert status == 200 and len(body["slots"]) == 4  # ... but a 1 h window is
    status, _headers, _content, body = call(app, "GET", "/api/v1/plan?hours=2")
    assert status == 200 and len(body["slots"]) == 8  # explicit query still wins


def test_batteries_route_reports_flat_capabilities_and_cycles():
    app = server()
    status, _headers, _content, body = call(app, "GET", "/api/v1/batteries")
    assert status == 200
    record = body["batteries"][0]
    assert set(record) >= {"config", "state", "capabilities", "mapping_resolved"}
    capabilities = record["capabilities"]
    assert isinstance(capabilities, dict) and capabilities
    assert all(not isinstance(value, dict) for key, value in capabilities.items() if key != "entities")
    assert record["mapping_resolved"] is True
    # the cycle count is reported (SPEC 10)
    assert record["cycles"] == 3.5
    assert record["state"]["extra"]["cycles"] == 3.5


def test_export_route_downloads_csv_and_json():
    app = server()
    status, headers, content, _body = call(app, "GET", "/api/v1/export?what=plan&format=csv")
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers["Content-Disposition"]
    text = content.decode("utf-8")
    assert text.splitlines()[0].startswith("t,")
    assert '"a, b"' in text  # csv quoting

    status, headers, content, _body = call(app, "GET", "/api/v1/export?what=actuals&format=json")
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert json.loads(content)["rows"]

    assert call(app, "GET", "/api/v1/export?what=example&format=json")[0] == 400
    assert call(app, "GET", "/api/v1/export?what=plan&format=example")[0] == 400


def test_notify_test_and_error_paths():
    app = server()
    status, _headers, _content, body = call(app, "POST", "/api/v1/notify/test")
    assert status == 200 and body == {"sent": True}

    status, _headers, _content, body = call(app, "GET", "/api/v1/example")
    assert status == 404 and set(body["error"]) == {"code", "message"}

    status, _headers, _content, body = call(app, "GET", "/api/v1/health", body=None)
    assert status == 200
    status, _headers, content = app.dispatch("POST", "/api/v1/health", b"{not json")
    assert status == 400 and json.loads(content)["error"]["code"] == "invalid_json"

    # unknown method on a known route
    assert call(app, "DELETE", "/api/v1/health")[0] == 404

    # an unexpected engine failure must not leak internals
    engine = FakeEngine()

    def boom(*args, **kwargs):
        raise RuntimeError("example internal failure")

    engine.savings = boom
    app = server(engine)
    status, _headers, _content, body = call(app, "GET", "/api/v1/savings")
    assert status == 500 and "example internal failure" not in body["error"]["message"]


def test_all_gets_work_with_no_batteries_and_mode_off():
    engine = FakeEngine(batteries=False)
    engine.status.mode = Mode.OFF.value
    engine.status.batteries = []
    engine.plan = Plan(
        generated_at=NOW,
        horizon_start=NOW,
        horizon_end=NOW,
        slot_minutes=15,
        feasible=True,
        slots=[],
    )
    app = server(engine)
    for target in (
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/plan",
        "/api/v1/forecast?kind=price",
        "/api/v1/batteries",
        "/api/v1/discover",
        "/api/v1/config",
        "/api/v1/savings",
        "/api/v1/accuracy",
        "/api/v1/log",
        "/api/v1/providers",
        "/api/v1/export?what=plan&format=json",
    ):
        status, _headers, _content, body = call(app, "GET", target)
        assert status == 200, (target, status, body)


# ---------------------------------------------------------------------------------- static


def test_static_serving_and_traversal_rejection(tmp_path):
    (tmp_path / "index.html").write_text("<html>example</html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log('example')", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "style.css").write_text("body{}", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("example secret", encoding="utf-8")
    app = server(web_root=str(tmp_path))

    status, headers, content, _body = call(app, "GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-cache"
    assert b"example" in content

    status, headers, _content, _body = call(app, "GET", "/app.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    # Every asset revalidates, not just the shell: an add-on is updated in place and its asset URLs
    # are unversioned, so a cached stylesheet would keep showing the previous release's UI.
    assert headers["Cache-Control"] == "no-cache", "a cached asset would hide an update"

    status, headers, _content, _body = call(app, "GET", "/sub/style.css")
    assert status == 200 and headers["Content-Type"].startswith("text/css")
    assert headers["Cache-Control"] == "no-cache"

    for attack in (
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/sub/../../secret.txt",
        "/..%2fsecret.txt",
        "/sub/%2e%2e/%2e%2e/secret.txt",
        "/.\x00/index.html",
    ):
        status, _headers, content, body = call(app, "GET", attack)
        assert status == 404, attack
        assert b"example secret" not in content

    status, _headers, _content, body = call(app, "GET", "/missing.js")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_static_serving_reports_missing_ui_clearly(tmp_path):
    app = server(web_root=str(tmp_path / "does-not-exist"))
    status, _headers, _content, body = call(app, "GET", "/")
    assert status == 404 and body["error"]["message"]


def test_real_http_round_trip():
    app = server()
    app.start()
    try:
        base = f"http://127.0.0.1:{app.address[1]}"
        with urllib.request.urlopen(base + "/api/v1/health", timeout=10) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        request = urllib.request.Request(
            base + "/api/v1/mode",
            data=json.dumps({"mode": "auto", "dry_run": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
            assert json.loads(response.read())["mode"] == "auto"
        try:
            urllib.request.urlopen(base + "/api/v1/example", timeout=10)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        app.stop()


def test_request_threads_do_not_leak_store_connections(tmp_path):
    """A per-connection API thread must hand its SQLite connection back (EMFILE, 0.37.4).

    ``ThreadingHTTPServer`` runs one thread per client connection while the store caches one
    connection per thread, so an unreleased cache entry leaks the database/WAL descriptors: the
    live add-on hit ``EMFILE`` after a few hundred requests, which took the UI down and made every
    unguarded store read fail.  30 requests here are enough to show growth of a descriptor each.
    """
    if not os.path.isdir("/proc/self/fd"):  # pragma: no cover - the add-on runs on Linux
        pytest.skip("needs /proc/self/fd")
    from app.store import Store

    store = Store(tmp_path / "store.db")

    class StoreEngine(FakeEngine):
        @property
        def store(self):
            return store

        def snapshot(self):
            store.connection.execute("SELECT 1").fetchone()  # a store read on every request
            return super().snapshot()

    store.release_thread()
    app = ApiServer(StoreEngine(), host="127.0.0.1", port=0)
    before = len(os.listdir("/proc/self/fd"))
    app.start()
    try:
        base = f"http://127.0.0.1:{app.address[1]}"
        for _ in range(30):
            with urllib.request.urlopen(base + "/api/v1/health", timeout=10) as response:
                assert response.status == 200
    finally:
        app.stop()
    leaked = len(os.listdir("/proc/self/fd")) - before
    assert leaked <= 5, f"{leaked} descriptors leaked over 30 requests"
    assert len(store._connections) == 0, "a finished request thread kept its SQLite connection"


def test_settings_meta_matches_the_frozen_spec_keys():
    """Every frozen SPEC 3 key is advertised (and nothing else is, see the DEFAULTS test)."""
    defaults = default_settings()
    if not defaults:  # pragma: no cover - WP2A absent
        return
    app = server()
    _status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    rows = {row["key"]: row for row in body["settings"]}
    for key in (
        "price.unit",
        "price.bzn",
        "solar.planes",
        "grid.power_entity",
        "grid.import_entity",
        "grid.export_entity",
        "optimizer.target_soc_pct_end",
        "control.stale_state_seconds",
        "notify.events",
    ):
        assert key in rows, key
    assert rows["price.unit"]["choices"] == [
        "auto",
        "eur_per_kwh",
        "eur_per_mwh",
        "micro_eur_per_kwh",
    ]
    assert rows["optimizer.target_soc_pct_end"]["default"] is None
    assert rows["mode"]["choices"] == ["off", "advisory", "auto"]
    assert rows["solar.api_key"]["type"] == "password"
    # the removed keys must never be advertised again
    assert "solar.declination_correction" not in rows


# ----------------------------------------------------------------------------------- forecast + config (0.3.0)


class WarnSettings(FakeSettings):
    """FakeSettings that also answers price_warnings(), like the real Settings (SPEC 4.1)."""

    def __init__(self, warnings, **values):
        super().__init__(**values)
        self._warnings = warnings

    def price_warnings(self):
        return self._warnings


class ExplainedFakeEngine(FakeEngine):
    """FakeEngine that also exposes the price provider/contract + forecast explanation accessors."""

    def __init__(self, *, provider_contract=None, explanation=None, price_forecast="default", price_warnings=None):
        super().__init__()
        self._provider_contract = provider_contract or {"provider": "fixed", "contract": "fixed_single"}
        self._explanation = explanation if explanation is not None else {"reason": None, "hint": None}
        self._price_forecast = price_forecast
        self.settings = WarnSettings(
            price_warnings or [],
            mode=self.settings.get("mode"),
            dry_run=self.settings.get("dry_run"),
        )

    def forecast(self, kind):
        if kind == "price" and self._price_forecast == "none":
            return None
        return super().forecast(kind)

    def price_provider_contract(self):
        return dict(self._provider_contract)

    def explain_forecast(self, kind):
        return dict(self._explanation)


def test_forecast_price_payload_adds_provider_contract_notes_reason_hint():
    app = server(ExplainedFakeEngine(provider_contract={"provider": "fixed", "contract": "fixed_single"}))
    status, _headers, _content, body = call(app, "GET", "/api/v1/forecast?kind=price&hours=36")
    assert status == 200
    # additive: every pre-existing key is still there
    assert {"kind", "generated_at", "source", "stale", "slots"} <= set(body)
    # the 0.3.0 additions
    assert body["provider"] == "fixed"
    assert body["contract"] == "fixed_single"
    assert isinstance(body["notes"], list)
    assert body["reason"] is None and body["hint"] is None


def test_forecast_empty_price_panel_still_carries_the_new_fields_with_a_reason():
    engine = ExplainedFakeEngine(
        price_forecast="none",
        provider_contract={"provider": "unavailable", "contract": "fixed_single"},
        explanation={"reason": "fixed_price_missing", "hint": "set price.fixed_price_eur_per_kwh or price.fixed_price_entity"},
    )
    app = server(engine)
    status, _headers, _content, body = call(app, "GET", "/api/v1/forecast?kind=price")
    assert status == 200
    assert {"kind", "generated_at", "source", "stale", "slots"} <= set(body)
    assert body["slots"] == [] and body["source"] == "unavailable"
    assert body["provider"] == "unavailable" and body["contract"] == "fixed_single"
    assert body["notes"] == []
    assert body["reason"] == "fixed_price_missing"
    assert "price.fixed_price_eur_per_kwh" in body["hint"]


def test_forecast_solar_payload_adds_provider_and_notes():
    app = server(ExplainedFakeEngine())
    status, _headers, _content, body = call(app, "GET", "/api/v1/forecast?kind=solar")
    assert status == 200
    assert body["provider"] == "example_solar" and body["contract"] is None
    assert isinstance(body["notes"], list)


def test_config_payload_carries_price_warnings():
    engine = ExplainedFakeEngine(price_warnings=[{"key": "price.contract", "message": "provider/contract mismatch example"}])
    app = server(engine)
    status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    assert status == 200
    assert {"options", "settings", "conflicts", "price_warnings"} <= set(body)
    assert body["price_warnings"] == [{"key": "price.contract", "message": "provider/contract mismatch example"}]


class GridFakeEngine(FakeEngine):
    """FakeEngine that answers the Meter card's provenance accessor (SPEC 3)."""

    def grid_resolution(self):
        return {
            "source": "energy_dashboard_power",
            "note": "grid roles from the Home Assistant Energy dashboard (power): grid_power=sensor.example_net_power",
            "explicit": {},
            "dashboard": {
                "grid_power": "sensor.example_net_power",
                "grid_energy_import": "sensor.example_grid_in_energy",
            },
        }


def test_config_payload_carries_the_grid_resolution():
    app = server(GridFakeEngine())
    status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    assert status == 200
    assert body["grid"]["source"] == "energy_dashboard_power"
    assert body["grid"]["dashboard"]["grid_power"] == "sensor.example_net_power"
    assert body["grid"]["explicit"] == {}


def test_config_payload_without_a_grid_accessor_still_answers():
    app = server(FakeEngine())
    status, _headers, _content, body = call(app, "GET", "/api/v1/config")
    assert status == 200
    assert body["grid"] == {}


def test_health_reports_the_backend_the_optimizer_module_reports():
    """The health field must follow the optimizer's state, not be a constant.

    Checking only the response shape would pass just as happily against a hard-coded
    ``{"backend": "python", "build_sha": None, "reason": ...}``, which would silently remove the one
    guarantee this field exists for -- being able to ask a deployed container whether the compiled
    accelerator is actually live.
    """
    from app.optimize import _fast

    app = server()
    try:
        # The state TRANSITIONS are the assertion that matters, because they cannot be satisfied by
        # a constant, and they do not depend on this test and the API importing the same module
        # object (the package is importable as both ``app`` and ``energy_manager.app``, so a test
        # that imports the other name sees a second ``_fast`` and compares the wrong instance).
        _fast.reset()
        live = call(app, "GET", "/api/v1/health")[3]["optimizer_core"]
        assert live["backend"] in ("c", "python")
        assert (live["build_sha"] is not None) == (live["backend"] == "c")
        assert live["reason"] is None or live["backend"] == "python"

        _fast.disable("a verification mismatch")
        if _fast._core is None:
            # Without the extension the accelerator is unavailable for a more fundamental reason --
            # it is not built -- which ``status()`` reports ahead of any disable, so the disable and
            # reset transitions are not observable in this configuration (the CI and plain-checkout
            # path).  The shape of the field is still asserted above, and the transitions are
            # exercised whenever the extension is built.
            pytest.skip("the extension is not built in this checkout")
        off = call(app, "GET", "/api/v1/health")[3]["optimizer_core"]
        assert off["backend"] == "python" and off["build_sha"] is None
        assert off["reason"] == "a verification mismatch"
        assert off == _fast.status()

        _fast.reset()
        back = call(app, "GET", "/api/v1/health")[3]["optimizer_core"]
        assert back == live, "a reset must put the endpoint back where it started"
        assert back == _fast.status()
    finally:
        _fast.reset()
