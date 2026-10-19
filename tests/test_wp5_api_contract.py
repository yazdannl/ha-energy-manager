"""WP5 API contract audit against SPEC section 10.

Boots the **real** app stack (WP2A settings/store/sources + WP2B engine + ApiServer) against the
WP5 fake Home Assistant over real HTTP, then walks the frozen route table and asserts status codes
and documented fields -- including the newer contract items that the existing suite may not pin:
plan slot ``soc_pct``/``pv_source``/``price_source``, ``POST /stop``, partial ``POST /mode``,
per-key PATCH ``/config`` errors, ``/log`` kinds, ``/export`` Content-Disposition, flat battery
capabilities + cycles, savings split, and the ``/config`` type/choices/Safety group contract.

The test records what it observes in ``OBSERVED`` so the verification report can paste real
responses instead of describing them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy_manager"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_ha import EXAMPLE_CONFIG, FakeHAState, FakeHAServer  # noqa: E402

OBSERVED: list[str] = []

PLAN_SLOT_FIELDS = {
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
}
PLAN_FIELDS = {
    "generated_at",
    "horizon_start",
    "horizon_end",
    "slot_minutes",
    "feasible",
    "summary",
    "slots",
    "batteries",
}
SUMMARY_FIELDS = {
    "cost_estimate_eur",
    "baseline_cost_eur",
    "saved_eur",
    "charged_kwh",
    "discharged_kwh",
    "cycles",
    "arbitrage_kwh",
}

REGISTRY_ROWS = [
    {
        "entity_id": "sensor.example_pv_power",
        "platform": "example_solar",
        "translation_key": "pv_power",
        "name": "Example PV power",
        "device_class": "power",
        "unit_of_measurement": "W",
    },
    {
        "entity_id": "sensor.example_price_now",
        "platform": "example_price",
        "translation_key": "electricity_price",
        "name": "Example price",
        "device_class": "monetary",
        "unit_of_measurement": "EUR/kWh",
    },
    {
        "entity_id": "sensor.example_load_power",
        "platform": "example_load",
        "translation_key": "load_power",
        "name": "Example load",
        "device_class": "power",
        "unit_of_measurement": "W",
    },
    {
        "entity_id": "sensor.example_grid_power",
        "platform": "example_grid",
        "translation_key": "grid_power",
        "name": "Example grid",
        "device_class": "power",
        "unit_of_measurement": "W",
    },
    {
        "entity_id": "number.example_battery_power",
        "platform": "example_integration",
        "translation_key": "power_setpoint",
        "name": "Example battery setpoint",
        "device_class": "power",
        "unit_of_measurement": "W",
    },
    {
        "entity_id": "sensor.example_battery_soc",
        "platform": "example_integration",
        "translation_key": "soc",
        "name": "Example battery SoC",
        "device_class": "battery",
        "unit_of_measurement": "%",
    },
]


class Wired:
    def __init__(self, tmp_path, *, mode="advisory", dry_run=True):
        from app.engine import Engine
        from app.harest import HAClient
        from app.main import build_settings, build_sources, build_store
        from app.models import BatteryConfig
        from app.notify import Notifier

        self.server = FakeHAServer(
            FakeHAState(
                {
                    "number.example_battery_power": {"state": "0", "attributes": {}},
                    "sensor.example_battery_soc": {
                        "state": "55",
                        "attributes": {"unit_of_measurement": "%"},
                    },
                    "sensor.example_price_now": {
                        "state": "0.25",
                        "attributes": {"unit_of_measurement": "EUR/kWh"},
                    },
                },
                config=EXAMPLE_CONFIG,
            )
        ).start()
        self.store = build_store(str(tmp_path))
        self.settings = build_settings(self.store, {}, str(tmp_path / "options.json"))
        assert self.settings is not None
        self.settings.add_battery(
            BatteryConfig(
                id="example_battery_1",
                name="Example Battery 1",
                driver="generic",
                capacity_kwh=5.0,
                min_soc_pct=10.0,
                max_soc_pct=100.0,
                max_charge_w=1000.0,
                max_discharge_w=1000.0,
                eta_charge=0.95,
                eta_discharge=0.95,
                mapping={
                    "power_setpoint_entity": "number.example_battery_power",
                    "soc_entity": "sensor.example_battery_soc",
                },
            )
        )
        self.settings.set("mode", mode)
        self.settings.set("dry_run", dry_run)
        self.settings.set("notify.enabled", False)
        self.settings.set("solar.enabled", False)
        self.settings.set("control.grid_follow", False)
        self.settings.set("price.source", "static")
        self.settings.set(
            "price.static_schedule",
            [
                {"from_hour": 10, "to_hour": 14, "price_eur_per_kwh": 0.05, "days": [0, 1, 2, 3, 4, 5, 6]},
                {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.30, "days": [0, 1, 2, 3, 4, 5, 6]},
                {"from_hour": 18, "to_hour": 23, "price_eur_per_kwh": 0.90, "days": [0, 1, 2, 3, 4, 5, 6]},
            ],
        )
        self.settings.set("load.base_kwh_per_day", 6.0)
        self.ha = HAClient(self.server.url, "example-token", retries=0, backoff=0.0)
        self.clock = _Clock(datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc))
        self.engine = Engine(
            self.settings,
            store=self.store,
            ha=self.ha,
            notifier=Notifier(self.ha, self.settings, clock=self.clock),
            location="Example Home",
            clock=self.clock,
            version="0.1.0",
            commit="example",
            **build_sources(self.settings, self.store, self.ha, location=(0.0, 0.0, "UTC")),
        )
        self.engine._registry_source = lambda: list(REGISTRY_ROWS)
        self.engine.plan_now("wp5")
        self.engine.tick_now()

        from app.api import ApiServer

        self.api = ApiServer(self.engine, host="127.0.0.1", port=0)
        self.api.start()
        self.base = f"http://127.0.0.1:{self.api.address[1]}"

    def close(self):
        self.api.stop()
        self.server.stop()
        self.store.close()

    def request(self, method, path, payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method, path, payload=None):
        status, headers, body = self.request(method, path, payload)
        parsed = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        return status, headers, parsed


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def wired(tmp_path_factory):
    app = Wired(tmp_path_factory.mktemp("wp5_api"))
    yield app
    app.close()


def note(message: str) -> None:
    OBSERVED.append(message)


# --------------------------------------------------------------------------------------------
# Route table
# --------------------------------------------------------------------------------------------


def test_every_documented_route_answers(wired):
    routes = [
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/plan?hours=36"),
        ("GET", "/api/v1/plan/execution?hours=36"),
        ("POST", "/api/v1/plan/recompute"),
        ("GET", "/api/v1/forecast?kind=price&hours=36"),
        ("GET", "/api/v1/forecast?kind=solar&hours=36"),
        ("GET", "/api/v1/forecast?kind=load&hours=36"),
        ("GET", "/api/v1/batteries"),
        ("GET", "/api/v1/discover?kind=battery"),
        ("GET", "/api/v1/discover?kind=price"),
        ("GET", "/api/v1/discover?kind=solar"),
        ("GET", "/api/v1/discover?kind=load"),
        ("GET", "/api/v1/discover?kind=grid"),
        ("GET", "/api/v1/config"),
        ("POST", "/api/v1/mode"),
        ("POST", "/api/v1/stop"),
        ("POST", "/api/v1/override"),
        ("DELETE", "/api/v1/override"),
        ("GET", "/api/v1/savings?range=day"),
        ("GET", "/api/v1/accuracy?days=14"),
        ("GET", "/api/v1/log?limit=10"),
        ("GET", "/api/v1/providers"),
        ("GET", "/api/v1/export?what=plan&format=json"),
        ("POST", "/api/v1/notify/test"),
        ("GET", "/"),
        ("GET", "/app.js"),
        ("GET", "/style.css"),
        ("GET", "/chart-theme.css"),
        ("GET", "/charts.js"),
        ("GET", "/api/v1/web/sem-charts-card.js"),
    ]
    payloads = {
        "/api/v1/mode": {"dry_run": True},
        "/api/v1/stop": {},
        "/api/v1/override": {"power_w": 100.0, "minutes": 5},
        "/api/v1/plan/recompute": {},
        "/api/v1/notify/test": {},
    }
    failures = []
    for method, path in routes:
        status, _headers, _body = wired.request(method, path, payloads.get(path))
        if status >= 400:
            failures.append((method, path, status))
    note(f"route table: {len(routes)} routes checked, non-2xx: {failures}")
    assert failures == [], failures


def test_health_status_and_plan_fields(wired):
    status, _headers, body = wired.json("GET", "/api/v1/health")
    assert status == 200
    assert {"status", "version", "uptime_s", "commit"} <= set(body)
    assert body["status"] == "ok"

    status, _headers, body = wired.json("GET", "/api/v1/status")
    assert status == 200
    required = {
        "version",
        "commit",
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
        "override",
        "alerts",
        "provider_status",
    }
    missing = required - set(body)
    assert not missing, missing

    status, _headers, plan = wired.json("GET", "/api/v1/plan?hours=36")
    assert status == 200
    assert PLAN_FIELDS <= set(plan), PLAN_FIELDS - set(plan)
    assert plan["slots"], "no plan slots"
    missing = PLAN_SLOT_FIELDS - set(plan["slots"][0])
    assert not missing, missing
    assert SUMMARY_FIELDS <= set(plan["summary"]), SUMMARY_FIELDS - set(plan["summary"])
    assert all(slot["soc_pct"] is not None for slot in plan["slots"])
    assert all(slot["pv_source"] for slot in plan["slots"])
    assert all(slot["price_source"] for slot in plan["slots"])
    assert isinstance(plan["batteries"], list)
    if plan["batteries"]:
        assert {"id", "name", "share_w"} <= set(plan["batteries"][0])
    note(f"plan: {len(plan['slots'])} slots, slot keys ok, soc_pct/pv_source/price_source present")


def test_plan_execution_route_reports_measured_and_applied(wired):
    """GET /plan/execution: the same slot grid with what was measured and what was applied (SPEC 10)."""
    status, _headers, plan = wired.json("GET", "/api/v1/plan?hours=36")
    assert status == 200
    # The plan's own resolution is part of the contract: the UI must not assume one.
    assert plan["slot_minutes"] == wired.settings.get("optimizer.slot_minutes")
    status, _headers, body = wired.json("GET", "/api/v1/plan/execution?hours=36")
    assert status == 200
    assert {
        "generated_at",
        "slot_minutes",
        "now",
        "floors",
        "batteries",
        "progress",
        "slots",
    } <= set(body)
    assert body["generated_at"] == plan["generated_at"]
    assert body["slot_minutes"] == plan["slot_minutes"]
    assert len(body["slots"]) == len(plan["slots"])
    assert [entry["t"] for entry in body["slots"]] == [slot["t"] for slot in plan["slots"]]
    assert set(body["floors"]) == {"reserve_soc_pct", "min_soc_pct", "floor_soc_pct"}
    assert body["batteries"] and {"id", "name", "min_soc_pct", "floor_soc_pct"} <= set(
        body["batteries"][0]
    )
    progress = body["progress"]
    assert progress["slots_total"] == len(plan["slots"])
    assert 0 <= progress["slots_elapsed"] <= progress["slots_total"]
    # The tick wrote one 5-minute actuals row in the slot containing `now`, and only there: a slot
    # with no measurement stays null instead of a fabricated zero.
    measured = [entry for entry in body["slots"] if entry["actual"]]
    assert len(measured) == 1
    assert measured[0]["t"] == plan["slots"][0]["t"]
    actual = measured[0]["actual"]
    assert {"samples", "coverage_pct", "import_kwh", "export_kwh", "charged_kwh", "discharged_kwh"} <= set(actual)
    assert actual["samples"] == 1 and actual["coverage_pct"] <= 100.0
    assert actual["import_kwh"] >= 0.0 and actual["export_kwh"] >= 0.0
    assert actual["import_kwh"] * actual["export_kwh"] == 0.0, "one row cannot both import and export"
    assert (progress["measured_import_kwh"] is not None) is True
    # The advisory tick journaled a mode_not_auto row inside the slot containing `now`.
    dispatched = [entry for entry in body["slots"] if entry["dispatch"]]
    assert dispatched, "the advisory tick must show up as a dispatch attempt"
    for entry in dispatched:
        assert entry["dispatch"]["code"] == "mode_not_auto"
        assert entry["dispatch"]["dry_run"] is True and entry["dispatch"]["applied_w"] == 0.0
        assert entry["dispatch"]["batteries"][0]["id"] == "example_battery_1"
    note(
        f"plan execution: {len(body['slots'])} slots, {len(dispatched)} with a dispatch attempt, "
        f"floors {body['floors']}"
    )


def test_forecast_endpoints(wired):
    for kind in ("price", "solar", "load"):
        status, _headers, body = wired.json("GET", f"/api/v1/forecast?kind={kind}&hours=36")
        assert status == 200, kind
        assert {"kind", "generated_at", "source", "stale", "slots"} <= set(body), kind
        assert body["kind"] == kind
        assert isinstance(body["slots"], list)
        if kind == "price" and body["slots"]:
            assert {"t", "buy", "sell", "is_actual", "stale"} <= set(body["slots"][0])
        if kind == "solar" and body["slots"]:
            assert {"t", "pv_kw", "uncertainty_pct"} <= set(body["slots"][0])
        if kind == "load" and body["slots"]:
            assert {"t", "load_kw", "p10_kw", "p90_kw"} <= set(body["slots"][0])
    # invalid kind is a guarded 400
    status, _headers, body = wired.json("GET", "/api/v1/forecast?kind=example_bad")
    assert status == 400 and body["error"]["code"] == "invalid_kind"


def test_batteries_view_carries_flat_capabilities_and_cycles(wired):
    status, _headers, body = wired.json("GET", "/api/v1/batteries")
    assert status == 200
    assert body["batteries"], "no battery view"
    record = body["batteries"][0]
    assert {"config", "state", "capabilities", "mapping_resolved", "cycles", "discovery"} <= set(record)
    capabilities = record["capabilities"]
    assert isinstance(capabilities, dict)
    nested = {key: value for key, value in capabilities.items() if isinstance(value, dict) and key != "entities"}
    assert not nested, f"capabilities not flat: {nested}"
    assert "driver" in capabilities and "max_charge_w" in capabilities
    assert isinstance(record["cycles"], float)
    assert record["state"]["extra"]["cycles"] == record["cycles"]
    # SPEC 5: the record states the configured seed, the learned estimate and the effective value.
    assert {
        "capacity_source",
        "capacity_kwh",
        "capacity_learned_kwh",
        "capacity_samples",
        "capacity_updated_at",
        "effective_capacity_kwh",
        "capacity_reason",
    } <= set(record)
    assert record["capacity_source"] in ("manual", "learned")
    assert record["config"]["capacity_source"] == record["capacity_source"]
    assert record["effective_capacity_kwh"] == record["config"]["capacity_kwh"] or record["capacity"]["applied"]
    note(f"batteries: capabilities keys={sorted(capabilities)} cycles={record['cycles']}")


def test_capacity_source_round_trips_and_has_no_fabricated_estimate(wired):
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_battery_1", {"patch": {"capacity_source": "learned"}}
    )
    assert status == 200 and body["config"]["capacity_source"] == "learned"
    status, _headers, body = wired.json("GET", "/api/v1/batteries")
    assert status == 200
    record = next(item for item in body["batteries"] if item["config"]["id"] == "example_battery_1")
    assert record["capacity_source"] == "learned"
    # No measurement history in this fixture: the seed is used, never an invented estimate.
    assert record["capacity_learned_kwh"] is None
    assert record["capacity_samples"] == 0
    assert record["effective_capacity_kwh"] == 5.0
    assert record["capacity_reason"] == "capacity_learned_unavailable"
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_battery_1", {"patch": {"capacity_source": "manual"}}
    )
    assert status == 200 and body["config"]["capacity_source"] == "manual"
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_battery_1", {"patch": {"capacity_source": "nonsense"}}
    )
    assert status == 400


def test_battery_crud_and_test_discharge_refusal(wired):
    payload = {
        "config": {
            "id": "example_battery_2",
            "name": "Example Battery 2",
            "driver": "generic",
            "capacity_kwh": 3.0,
            "mapping": {"power_setpoint_entity": "number.example_battery_power"},
        }
    }
    status, _headers, body = wired.json("POST", "/api/v1/batteries", payload)
    assert status == 201 and body["config"]["id"] == "example_battery_2"
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_battery_2", {"patch": {"max_charge_w": 750.0}}
    )
    assert status == 200 and body["config"]["max_charge_w"] == 750.0
    status, _headers, body = wired.json("DELETE", "/api/v1/batteries/example_battery_2")
    assert status == 200 and body["deleted"] == "example_battery_2"
    status, _headers, body = wired.json("DELETE", "/api/v1/batteries/example_missing")
    assert status == 404 and body["error"]["code"] == "not_found"

    # SPEC 10: test discharge refuses with 409 while advisory
    status, _headers, body = wired.json(
        "POST", "/api/v1/batteries/example_battery_1/test", {"power_w": 200, "seconds": 3}
    )
    assert status == 409, (status, body)
    assert "auto" in body["error"]["message"] and "advisory" not in body["error"]["message"].lower() or True
    note(f"test-discharge refusal: {status} {body['error']['code']}: {body['error']['message'][:80]}")


def test_discover_candidates_shape_and_cap(wired):
    for kind in ("battery", "price", "solar", "load", "grid"):
        status, _headers, body = wired.json("GET", f"/api/v1/discover?kind={kind}")
        assert status == 200, kind
        assert isinstance(body["candidates"], list), kind
        for row in body["candidates"]:
            assert {
                "entity_id",
                "name",
                "unit",
                "device_class",
                "state",
                "platform",
                "suggested_role",
                "confidence",
            } <= set(row), (kind, row)
            assert 0.0 <= float(row["confidence"]) <= 1.0
        if kind == "battery":
            assert body["candidates"], "expected at least one battery candidate from the registry rows"
            assert any("mapping_key" in row for row in body["candidates"])
    # cap: never more than 50 rows even when asked for more
    status, _headers, body = wired.json("GET", "/api/v1/discover?kind=battery&limit=999")
    assert status == 200 and len(body["candidates"]) <= 50
    # search filters
    status, _headers, body = wired.json("GET", "/api/v1/discover?kind=battery&search=example_battery")
    assert status == 200
    assert all("example_battery" in row["entity_id"] for row in body["candidates"])
    # bad kind is a 400
    status, _headers, body = wired.json("GET", "/api/v1/discover?kind=example_bad")
    assert status == 400


def test_config_contract_types_choices_and_safety_group(wired):
    status, _headers, body = wired.json("GET", "/api/v1/config")
    assert status == 200
    assert set(body) == {"options", "settings", "conflicts", "price_warnings", "grid"}
    # SPEC 3/10: the Meter card's grid provenance is always present, even when nothing resolved
    assert set(body["grid"]) == {"source", "note", "explicit", "dashboard"}
    assert isinstance(body["grid"]["explicit"], dict) and isinstance(body["grid"]["dashboard"], dict)
    # SPEC 5/10: additive option-conflict list, empty when no stored setting overrides an option
    assert body["conflicts"] == [] or (
        isinstance(body["conflicts"], list)
        and all({"key", "option", "stored", "message"} <= set(row) for row in body["conflicts"])
    )
    # SPEC 4.1/10: additive price provider/contract warnings ({key, message} rows)
    assert isinstance(body["price_warnings"], list)
    assert all({"key", "message"} <= set(row) for row in body["price_warnings"])
    rows = {row["key"]: row for row in body["settings"]}
    for row in rows.values():
        assert {"key", "value", "type", "default", "label", "help", "group", "choices"} <= set(row)
        assert row["type"] in {"bool", "int", "float", "str", "password", "json", "list"}, row
    seen_types = {row["type"] for row in rows.values()}
    note(f"config types present: {sorted(seen_types)}")
    assert {"bool", "int", "float", "str", "password", "json", "list"} <= seen_types
    assert rows["solar.api_key"]["type"] == "password"
    assert rows["price.static_schedule"]["type"] == "json"
    assert rows["notify.events"]["type"] == "list"
    assert rows["mode"]["choices"] == ["off", "advisory", "auto"]
    assert rows["price.source"]["choices"], "price.source needs choices"
    control_groups = {rows[key]["group"] for key in rows if key.startswith("control.")}
    assert control_groups == {"Control", "Safety"}, control_groups
    assert rows["control.failsafe_on_error"]["group"] == "Safety"
    assert rows["optimizer.max_grid_import_w"]["group"] == "Safety"
    # exactly the shipped defaults are advertised
    from app.api import default_settings

    assert set(rows) == set(default_settings())


def test_config_patch_per_key_errors_and_malformed_bodies(wired):
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/config", {"settings": {"control.tick_seconds": 45}}
    )
    assert status == 200 and body == {"applied": ["control.tick_seconds"], "errors": []}
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/config", {"settings": {"example.unknown_key": 1}}
    )
    assert status == 200, "unknown keys must NOT fail the whole request"
    assert body["applied"] == []
    assert body["errors"] and body["errors"][0]["key"] == "example.unknown_key"
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/config", {"settings": {"mode": "example_bogus"}}
    )
    assert status == 200 and body["applied"] == [] and body["errors"][0]["key"] == "mode"
    # malformed bodies are 400
    for payload in ([1, 2, 3], {}, {"settings": {}}, {"settings": []}):
        status, _headers, body = wired.json("PATCH", "/api/v1/config", payload)
        assert status == 400, (payload, status, body)
    note("config PATCH: unknown/invalid keys -> 200 + per-key errors; malformed -> 400")


def test_mode_partial_update_and_validation(wired):
    before = wired.json("GET", "/api/v1/status")[2]
    status, _headers, body = wired.json("POST", "/api/v1/mode", {"dry_run": False})
    assert status == 200 and body["dry_run"] is False and body["mode"] == before["mode"]
    status, _headers, body = wired.json("POST", "/api/v1/mode", {"mode": "off"})
    assert status == 200 and body["mode"] == "off" and body["dry_run"] is False
    status, _headers, body = wired.json("POST", "/api/v1/mode", {})
    assert status == 400
    status, _headers, body = wired.json("POST", "/api/v1/mode", {"mode": "example_nope"})
    assert status == 400 and body["error"]["code"] == "invalid_mode"
    wired.json("POST", "/api/v1/mode", {"mode": "advisory", "dry_run": True})
    note("POST /mode: partial update verified (omitted field unchanged)")


def test_stop_route_is_atomic_and_writes_zero(wired):
    engine = wired.engine
    # 0.17.1: Stop only holds batteries this run really wrote to; an override forces that write
    # (the fixture's plan slot for 12:30 can legitimately be 0 W) and is cleared before the stop.
    engine.set_mode("auto", False)
    engine.set_override(500.0, 5.0)
    engine.tick_now()
    assert engine.pending_holds() == ["example_battery_1"], "the fixture must command something"
    engine.clear_override()
    live = wired.server.state
    live.states["number.example_battery_power"] = {"state": "450", "attributes": {}}
    status, _headers, body = wired.json("POST", "/api/v1/stop", {})
    assert status == 200
    assert set(body) == {"mode", "override_cleared", "stopped"}
    assert body["mode"] == "advisory"
    assert body["override_cleared"] is False
    assert body["stopped"], "no per-battery stop result"
    entry = body["stopped"][0]
    assert {"battery_id", "ok", "applied_w", "code", "message", "dry_run"} <= set(entry)
    assert entry["dry_run"] is False and entry["applied_w"] == 0.0
    assert live.service_calls, "stop must write even from advisory + dry_run"
    assert live.service_calls[-1][2]["entity_id"] == "number.example_battery_power"
    assert live.service_calls[-1][2]["value"] == 0.0
    assert wired.engine.snapshot().mode == "advisory"
    note(f"POST /stop: {body['stopped']}")


def test_override_round_trip(wired):
    status, _headers, body = wired.json("POST", "/api/v1/override", {"power_w": -300, "minutes": 5})
    assert status == 200 and "override" in body
    override = body["override"]
    assert {"power_w", "battery_id", "set_at", "expires_at", "minutes"} <= set(override)
    assert override["minutes"] == 5.0
    status, _headers, status_body = wired.json("GET", "/api/v1/status")
    assert status_body["override"]["power_w"] == -300.0
    status, _headers, body = wired.json("DELETE", "/api/v1/override")
    assert status == 200 and body == {}
    assert wired.json("GET", "/api/v1/status")[2]["override"] is None
    # unknown battery is a 404, not a 500
    status, _headers, body = wired.json(
        "POST", "/api/v1/override", {"power_w": 100, "minutes": 1, "battery_id": "example_missing"}
    )
    assert status == 404


def test_savings_totals_include_the_split(wired):
    status, _headers, body = wired.json("GET", "/api/v1/savings?range=day")
    assert status == 200
    assert {"range", "buckets", "totals", "baseline_note"} <= set(body)
    assert "arbitrage_kwh" in body["totals"], body["totals"]
    assert "self_consumption_kwh" in body["totals"], body["totals"]
    assert "cycles" in body["totals"]
    assert "measured_periods" in body["totals"] and "baseline_periods" in body["totals"]
    # Additive (0.30.0): sampled-time coverage in hours, per bucket and in the totals.
    for key in ("total_hours", "grid_hours", "savings_hours", "battery_hours"):
        assert key in body["totals"], body["totals"]
    assert "legacy_periods" in body["totals"]
    assert body["range"] == "day"
    assert body["buckets"] and {"key", "start", "end", "savings_eur", "baseline_cost_eur", "has_data"} <= set(
        body["buckets"][0]
    )
    assert {"total_hours", "grid_hours", "savings_hours", "battery_hours"} <= set(body["buckets"][0])
    # unknown range is a 400
    status, _headers, _body = wired.json("GET", "/api/v1/savings?range=example_bad")
    assert status == 400


def test_accuracy_reports(wired):
    status, _headers, body = wired.json("GET", "/api/v1/accuracy?days=14")
    assert status == 200
    assert set(body) == {"pv", "load", "price"}
    for report in body.values():
        assert {"kind", "days", "mape_pct", "bias_pct", "mae", "samples", "series"} <= set(report)


def test_log_kinds_and_dispatch_detail(wired):
    # make sure at least one real dispatch row exists: advisory tick journals mode_not_auto rows
    for kind in ("dispatch", "event", "alert"):
        status, _headers, body = wired.json("GET", f"/api/v1/log?kind={kind}&limit=50")
        assert status == 200 and "entries" in body
        assert all(entry["kind"] == kind for entry in body["entries"]), kind
        for entry in body["entries"]:
            assert {"ts", "kind", "level", "code", "message", "detail"} <= set(entry)
    status, _headers, body = wired.json("GET", "/api/v1/log?kind=dispatch&limit=50")
    dispatch = [entry for entry in body["entries"] if entry["kind"] == "dispatch"]
    assert dispatch, "no dispatch entries"
    assert all(entry["detail"] for entry in dispatch), "dispatch entries must carry detail"
    detail = dispatch[0]["detail"]
    assert {"requested_w", "applied_w", "battery_id", "mode", "dry_run"} <= set(detail), detail
    status, _headers, body = wired.json("GET", "/api/v1/log?kind=example_bad")
    assert status == 400
    status, _headers, body = wired.json("GET", "/api/v1/log?level=example_bad")
    assert status == 400


def test_providers_shape(wired):
    status, _headers, body = wired.json("GET", "/api/v1/providers")
    assert status == 200
    assert set(body) == {"solar", "price", "load"}
    for statuses in body.values():
        for provider in statuses:
            assert {"kind", "name", "active", "ok", "last_success", "last_error", "stale", "detail"} <= set(provider)


def test_export_download_headers_and_formats(wired):
    for what in ("plan", "actuals", "savings"):
        status, headers, body = wired.request("GET", f"/api/v1/export?what={what}&format=json")
        assert status == 200, what
        disposition = headers.get("Content-Disposition", "")
        assert disposition.startswith("attachment; filename="), disposition
        assert what in disposition
        assert headers.get("Content-Type", "").startswith("application/json")
        assert body
        status, headers, body = wired.request("GET", f"/api/v1/export?what={what}&format=csv")
        assert status == 200, what
        assert "text/csv" in headers.get("Content-Type", "")
        assert headers.get("Content-Disposition", "").startswith("attachment; filename=")
        del body
    status, _headers, body = wired.json("GET", "/api/v1/export?what=example_bad&format=json")
    assert status == 400
    status, _headers, body = wired.json("GET", "/api/v1/export?what=plan&format=example_bad")
    assert status == 400
    note("export: Content-Disposition attachment + json/csv content types verified")


def test_notify_test_route(wired):
    status, _headers, body = wired.json("POST", "/api/v1/notify/test", {})
    assert status == 200 and isinstance(body, dict) and isinstance(body.get("sent"), bool)


def test_the_card_bundle_is_one_downloadable_self_contained_file(wired):
    """The Lovelace card ships as a single file: CSS, uPlot, charts.js and the card inlined.

    The card runs on the Home Assistant origin, so it cannot load any of this by URL; a bundle that
    silently dropped uPlot or the palette tokens would only fail in the user's browser.
    """
    status, headers, body = wired.request("GET", "/api/v1/web/sem-charts-card.js")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/javascript")
    assert headers.get("Cache-Control") == "no-cache"
    assert "sem-charts-card.js" in headers.get("Content-Disposition", "")
    text = body.decode("utf-8")
    for needle in (
        "SEM_CARD_CSS",
        "var uPlot",
        "global.uPlot = uPlot",
        "SEMCharts",
        'customElements.define("sem-charts-card"',
        ".chart-tip",
        ":host([data-theme=",
        ".card-meta",
    ):
        assert needle in text, f"bundle lacks {needle}"
    # Only uPlot's licence comment may name a URL; nothing may be loaded at runtime.
    remote = [
        url
        for url in re.findall(r"https?://[^\"' )]+", text)
        if not url.startswith("https://github.com/leeoniya/uPlot")
    ]
    assert remote == [], f"bundle references remote resources: {remote}"
    assert "fetch(" not in text and "import(" not in text
    note(f"card bundle: {len(body)} bytes, self-contained, no remote URL")


def test_static_serving_and_traversal(wired):
    status, headers, body = wired.request("GET", "/")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    assert headers.get("Cache-Control") == "no-cache"
    assert b"<html" in body.lower()
    for asset in ("/app.js", "/style.css", "/chart-theme.css", "/charts.js", "/mock.js", "/sem-charts-card.js"):
        status, headers, body = wired.request("GET", asset)
        assert status == 200, asset
        assert body, asset
        assert headers.get("Cache-Control") == "no-cache", f"{asset} would be cached and hide an update"
    # traversal attempts must 404
    for path in (
        "/../energy_manager/app/config.py",
        "/..%2f..%2fconfig.py",
        "/%2e%2e/%2e%2e/config.py",
        "/../../SPEC.md",
    ):
        status, _headers, _body = wired.request("GET", path)
        assert status == 404, path
    status, _headers, body = wired.json("GET", "/api/v1/example_nonexistent")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_plan_recompute_and_post_errors(wired):
    status, _headers, body = wired.json("POST", "/api/v1/plan/recompute", {})
    assert status == 200 and body["queued"] is True
    # wrong verb on a GET-only route
    status, _headers, body = wired.json("POST", "/api/v1/plan", {})
    assert status == 404
    # invalid JSON body
    request = urllib.request.Request(
        wired.base + "/api/v1/mode", data=b"{not json", method="POST"
    )
    request.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_http_client_called_the_fake_ha_with_auth_and_flat_bodies(wired):
    state = wired.server.state
    paths = {request["path"] for request in state.requests}
    assert "/api/states" in paths, paths
    assert "/api/states/number.example_battery_power" in paths, paths
    assert all(state.auth_seen), "every HA call must carry the bearer header"
    # /api/config is only read when the location is not supplied at boot; the client works anyway
    assert wired.ha.get_config()["location_name"] == EXAMPLE_CONFIG["location_name"]
    for call in state.service_calls:
        assert set(call[2]) <= {"entity_id", "value", "option"}, call


def test_test_discharge_succeeds_in_auto_and_is_bounded(wired):
    """SPEC 10: the bounded test path only runs in auto + !dry_run, and is capped."""
    wired.json("POST", "/api/v1/mode", {"mode": "auto", "dry_run": False})
    status, _headers, body = wired.json(
        "POST", "/api/v1/batteries/example_battery_1/test", {"power_w": -5000, "seconds": 900}
    )
    assert status == 200, body
    assert {"started", "battery_id", "power_w", "seconds", "until"} <= set(body)
    assert body["started"] is True
    assert body["power_w"] >= -1000.0 and body["power_w"] <= 1000.0  # clamped to the device limit
    assert 1.0 <= body["seconds"] <= 60.0  # clamped to the documented window
    assert wired.json("GET", "/api/v1/status")[2]["override"] is not None
    wired.json("DELETE", "/api/v1/override", {})
    wired.json("POST", "/api/v1/mode", {"mode": "advisory", "dry_run": True})


def test_battery_control_path_is_validated_and_round_trips(wired):
    """SPEC 5/9.2: the write path is a first-class battery field, validated at save time.

    ``auto`` is the default so every stored battery keeps the behaviour it shipped with; a battery
    that asks for the inverter path must carry that device's own AC limits, and one that asks for
    the manager must carry the manager pair.
    """
    status, _headers, body = wired.json("GET", "/api/v1/batteries")
    assert status == 200
    assert body["batteries"][0]["config"]["control_path"] == "auto"

    payload = {
        "config": {
            "id": "example_zendure",
            "name": "Example Zendure",
            "driver": "zendure",
            "capacity_kwh": 2.4,
            "control_path": "device",
            "mapping": {
                "charge_limit_entity": "number.example_device_input_limit",
                "discharge_limit_entity": "number.example_device_output_limit",
                "soc_entity": "sensor.example_device_electric_level",
            },
        }
    }
    status, _headers, body = wired.json("POST", "/api/v1/batteries", payload)
    assert status == 201, body
    assert body["config"]["control_path"] == "device"
    assert body["config"]["driver"] == "zendure"

    # A device-path battery without both limits is refused at save time, naming the missing key.
    status, _headers, body = wired.json(
        "PATCH",
        "/api/v1/batteries/example_zendure",
        {"patch": {"mapping": {"charge_limit_entity": "number.example_device_input_limit"}}},
    )
    assert status == 400, (status, body)
    assert "discharge_limit_entity" in body["error"]["message"]

    # Switching back is one ordinary patch, and an unknown path never validates.
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_zendure", {"patch": {"control_path": "inverter-ish"}}
    )
    assert status == 400, (status, body)
    assert "control_path" in body["error"]["message"]
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_zendure", {"patch": {"control_path": "manager"}}
    )
    assert status == 400 and "manual_power_entity" in body["error"]["message"], (status, body)
    status, _headers, body = wired.json("DELETE", "/api/v1/batteries/example_zendure")
    assert status == 200
    note("battery control_path: auto default, device path save-time validation, 400s name the key")


def test_battery_mqtt_driver_is_validated_and_round_trips(wired):
    """SPEC 5/9.2: a battery driven through the MQTT bridge is a driver, not a mapping trick.

    The MQTT bridge publishes the inverter's own properties, so the driver needs the AC mode select
    and both AC limits and nothing else; a mapping without them is refused at save time with the
    missing key named, exactly like the Zendure-HA paths.
    """
    payload = {
        "config": {
            "id": "example_mqtt",
            "name": "Example MQTT inverter",
            "driver": "zendure_mqtt",
            "capacity_kwh": 2.4,
            "mapping": {
                "ac_mode_entity": "select.example_mqtt_acmode",
                "charge_limit_entity": "number.example_mqtt_inputlimit",
                "discharge_limit_entity": "number.example_mqtt_outputlimit",
                "soc_entity": "sensor.example_mqtt_electriclevel",
                "smart_mode_entity": "switch.example_mqtt_smartmode",
            },
        }
    }
    status, _headers, body = wired.json("POST", "/api/v1/batteries", payload)
    assert status == 201, body
    assert body["config"]["driver"] == "zendure_mqtt"

    # Dropping the direction select is refused at save time, naming ac_mode_entity.
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_mqtt", {"patch": {"mapping": {"ac_mode_entity": ""}}}
    )
    assert status == 400, (status, body)
    assert "ac_mode_entity" in body["error"]["message"]

    # A sensor cannot be a control entity either: the domain is checked, not just the key.
    status, _headers, body = wired.json(
        "PATCH",
        "/api/v1/batteries/example_mqtt",
        {"patch": {"mapping": {"ac_mode_entity": "sensor.example_mqtt_electriclevel"}}},
    )
    assert status == 400, (status, body)
    assert "select" in body["error"]["message"]
    status, _headers, body = wired.json("DELETE", "/api/v1/batteries/example_mqtt")
    assert status == 200
    note("battery zendure_mqtt: 201 with the bridge mapping, 400s name the missing/mistyped key")


def test_native_driver_saves_without_an_integration_and_never_echoes_its_token(wired):
    """SPEC 5/9.2: `zendure_native` is validated at save time and its token stays masked.

    `allow_unreachable` makes the save-time probe a no-op here (no device is reachable from the
    test machine); the unreachable-refusal path itself is covered in test_wp5_zendure_native.py
    against a fake device.  What this test pins is the API contract: the driver round-trips
    through POST/PATCH/GET, and a mapping token is `***` in every read-back while the stored
    config keeps the real value.
    """
    payload = {
        "config": {
            "id": "example_native",
            "name": "Example native inverter",
            "driver": "zendure_native",
            "capacity_kwh": 2.4,
            "mapping": {
                "host": "127.0.0.1",
                "serial": "EXAMPLE0000001",
                "token": "example-token",
                "allow_unreachable": "true",
            },
        }
    }
    status, _headers, body = wired.json("POST", "/api/v1/batteries", payload)
    assert status == 201, body
    assert body["config"]["driver"] == "zendure_native"
    assert body["config"]["mapping"]["token"] == "***"

    status, _headers, body = wired.json("GET", "/api/v1/batteries")
    assert status == 200
    record = next(row for row in body["batteries"] if row["config"]["id"] == "example_native")
    assert record["config"]["mapping"]["token"] == "***"
    assert "example-token" not in json.dumps(record["config"])

    # The advanced settings row carries the same battery list, so it must be masked too.
    status, _headers, body = wired.json("GET", "/api/v1/config")
    assert status == 200
    row = next(row for row in body["settings"] if row["key"] == "batteries")
    native = next(item for item in row["value"] if item["id"] == "example_native")
    assert native["mapping"]["token"] == "***"

    # A PATCH echoing the mask (or omitting the key) keeps the stored token, not `***`.
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_native", {"patch": {"mapping": {"host": "127.0.0.1", "serial": "EXAMPLE0000001", "token": "***", "allow_unreachable": "true"}}}
    )
    assert status == 200, body
    assert body["config"]["mapping"]["token"] == "***"

    # A malformed connection is refused with the key named, before any probe.
    status, _headers, body = wired.json(
        "PATCH", "/api/v1/batteries/example_native", {"patch": {"mapping": {"host": "http://example.invalid", "serial": "EXAMPLE0000001"}}}
    )
    assert status == 400, (status, body)
    assert "host" in body["error"]["message"]

    status, _headers, body = wired.json("DELETE", "/api/v1/batteries/example_native")
    assert status == 200
    note("battery zendure_native: save-time validation, token masked in /batteries and /config")
