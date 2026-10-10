"""WP3 (UI) mock-contract checks.

mock.js must be loadable without a DOM and must expose every field that SPEC §10 documents for
every endpoint the SPA consumes, so that ?mock=1 can render all seven views without a backend.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "energy_manager" / "app" / "web"
NODE = shutil.which("node")

FROZEN_SETTING_KEYS = [
    # general / add-on options
    "mode", "dry_run", "log_level", "currency", "timezone", "latitude_override", "longitude_override",
    # price (incl. the provider/contract selection and the fixed-tariff path)
    "price.provider", "price.contract", "price.source", "price.entity", "price.static_schedule",
    "price.fixed_price_eur_per_kwh", "price.fixed_day_price_eur_per_kwh", "price.fixed_night_price_eur_per_kwh",
    "price.fixed_price_entity", "price.fixed_day_price_entity", "price.fixed_night_price_entity",
    "price.fixed_day_start_hour", "price.fixed_night_start_hour",
    "price.import_fee_eur_per_kwh",
    "price.export_fee_eur_per_kwh", "price.vat_pct", "price.energy_tax_eur_per_kwh", "price.unit",
    "price.scale_divisor", "price.bzn", "price.feed_in_eur_per_kwh", "price.feed_in_entity",
    "price.feed_in_tax_inclusive",
    # solar / load / grid
    "solar.enabled", "solar.source", "solar.planes", "solar.api_key", "solar.bias_correction",
    "solar.measured_entity", "load.mode", "load.entity", "load.temperature_entity",
    "load.base_kwh_per_day", "grid.power_entity", "grid.import_entity", "grid.export_entity",
    # optimizer
    "optimizer.slot_minutes", "optimizer.horizon_hours", "optimizer.soc_step_kwh",
    "optimizer.power_steps", "optimizer.salvage_multiplier", "optimizer.cycle_cost_eur_per_kwh",
    "optimizer.allow_grid_charge", "optimizer.allow_export", "optimizer.reserve_soc_pct",
    "optimizer.max_grid_import_w", "optimizer.max_grid_export_w", "optimizer.min_spread_eur_per_kwh",
    "optimizer.target_soc_pct_end",
    # control (grid_follow_max_w is read by the dispatcher; SPEC §10 schema)
    "control.tick_seconds", "control.replan_minutes", "control.grid_follow",
    "control.grid_follow_deadband_w", "control.grid_follow_max_ratio", "control.grid_follow_max_w",
    "control.stale_state_seconds", "control.failsafe_on_error",
    # notify / batteries
    "notify.enabled", "notify.persistent", "notify.mobile_service", "notify.events",
    "batteries",
]

DOCUMENTED_ROUTES = [
    "GET /api/v1/health",
    "GET /api/v1/status",
    "GET /api/v1/plan",
    "POST /api/v1/plan/recompute",
    "GET /api/v1/forecast",
    "GET /api/v1/batteries",
    "POST /api/v1/batteries",
    "PATCH /api/v1/batteries/:id",
    "DELETE /api/v1/batteries/:id",
    "POST /api/v1/batteries/:id/test",
    "GET /api/v1/discover",
    "GET /api/v1/config",
    "PATCH /api/v1/config",
    "POST /api/v1/mode",
    "POST /api/v1/stop",
    "POST /api/v1/override",
    "DELETE /api/v1/override",
    "GET /api/v1/savings",
    "GET /api/v1/accuracy",
    "GET /api/v1/log",
    "GET /api/v1/providers",
    "POST /api/v1/notify/test",
]


@pytest.fixture(scope="module")
def fixtures() -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    script = """
    const fs = require("fs");
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    console.log(JSON.stringify({ fixtures: SEM_MOCK.fixtures(), routes: Object.keys(SEM_MOCK.routes) }));
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([NODE, script_path], capture_output=True, text=True, cwd=str(WEB))
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def assert_fields(payload: dict, fields: list[str], where: str) -> None:
    missing = [field for field in fields if field not in payload]
    assert not missing, f"{where} is missing {missing}"


def test_stop_payload_matches_section_10(fixtures):
    """POST /stop must be served by the mock router with the documented shape (SPEC §9.4/§10)."""
    if NODE is None:
        pytest.skip("node is not installed")
    script = """
    const fs = require("fs");
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    (async function () {
      const payload = await SEM_MOCK.handle("POST", "/api/v1/stop", {});
      console.log(JSON.stringify(payload));
    })().catch(function (err) { console.error(err && err.message ? err.message : err); process.exit(1); });
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([NODE, script_path], capture_output=True, text=True, cwd=str(WEB))
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert_fields(payload, ["mode", "override_cleared", "stopped"], "POST /stop")
    assert payload["mode"] == "advisory", "stop must leave the app in advisory mode"
    assert isinstance(payload["override_cleared"], bool)
    assert payload["stopped"], "stop must report per-battery results"
    for row in payload["stopped"]:
        assert_fields(row, ["battery_id", "ok", "applied_w", "code", "message", "dry_run"], "POST /stop stopped[]")
        assert isinstance(row["ok"], bool)
        assert row["applied_w"] == 0
        assert isinstance(row["dry_run"], bool)
    # the stop is a real write: it must work while dry_run is on, and still be reported as dry_run
    assert payload["stopped"][0]["dry_run"] is True, "default mock state is dry_run on; stop must still be served"


def test_baseline_note_matches_the_backend_source_of_truth(fixtures):
    """The mock and the UI fallback must carry the exact analytics.BASELINE_NOTE string."""
    source = (ROOT / "energy_manager" / "app" / "analytics.py").read_text(encoding="utf-8")
    match = re.search(r'BASELINE_NOTE = \(\s*"([^"]+)"\s*"([^"]+)"\s*\)', source)
    assert match, "analytics.BASELINE_NOTE not found in the expected form"
    expected = match.group(1) + match.group(2)
    assert fixtures["fixtures"]["savings"]["month"]["baseline_note"] == expected
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert expected in app, "the UI fallback must be the same string the API returns"


def test_round_trip_efficiency_in_mock_payloads(fixtures):
    """SPEC §5/§6/§8.1: battery pct + derived read-back, plan summary eta, efficiency_loss slot."""
    config = fixtures["fixtures"]["batteries"]["batteries"][0]["config"]
    assert "round_trip_efficiency_pct" in config, "battery read-back must expose the derived pct"
    pct = config["round_trip_efficiency_pct"]
    assert 0 < pct <= 100
    assert abs(config["eta_charge"] * config["eta_discharge"] * 100 - pct) < 1e-6, "pct must equal eta_charge x eta_discharge"

    summary = fixtures["fixtures"]["plan"]["summary"]
    for field in ["eta_charge", "eta_discharge", "round_trip_efficiency"]:
        assert field in summary, f"plan summary must carry {field}"
    assert abs(summary["eta_charge"] * summary["eta_discharge"] - summary["round_trip_efficiency"]) < 1e-9
    assert 0 < summary["round_trip_efficiency"] <= 1

    slots = fixtures["fixtures"]["plan"]["slots"]
    loss = [s for s in slots if s["reason"] == "efficiency_loss"]
    assert loss, "at least one mock plan slot must carry the efficiency_loss reason"
    for slot in loss:
        assert slot["action"] == "hold", slot
        assert slot["power_w"] == 0, slot
        # the mock rule: blocked because the sale price does not clear the round-trip break-even
        assert slot["sell"] < slot["buy"] / summary["round_trip_efficiency"], slot


def test_test_discharge_payload_matches_section_10(fixtures):
    """The mock must exercise the bounded discharge response: signed intent, exact seconds, applied_w."""
    if NODE is None:
        pytest.skip("node is not installed")
    script = """
    const fs = require("fs");
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    (async function () {
      await SEM_MOCK.handle("POST", "/api/v1/mode", { mode: "auto", dry_run: false });
      const res = await SEM_MOCK.handle("POST", "/api/v1/batteries/battery_1/test",
        { power_w: 200, seconds: 7.5, intent: "discharge" });
      console.log(JSON.stringify(res));
    })().catch(function (err) { console.error(err && err.message ? err.message : err); process.exit(1); });
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([NODE, script_path], capture_output=True, text=True, cwd=str(WEB))
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert_fields(payload, ["started", "battery_id", "power_w", "intent", "seconds", "applied_w", "ok", "dry_run"], "POST /batteries/{id}/test")
    assert payload["intent"] == "discharge" and payload["power_w"] < 0, "positive field means discharge (signed intent)"
    assert payload["seconds"] == 7.5, "the exact seconds asked for must be honoured"
    assert payload["applied_w"] == payload["power_w"]
    assert payload["applied_w"] < 0
    assert payload["dry_run"] is False and payload["ok"] is True


def test_mock_route_table_covers_the_documented_contract(fixtures):
    routes = set(fixtures["routes"])
    missing = [route for route in DOCUMENTED_ROUTES if route not in routes]
    assert not missing, f"mock.js has no fixture for {missing}"


def test_mock_is_dom_free_and_offline():
    source = (WEB / "mock.js").read_text(encoding="utf-8")
    assert "document." not in source
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source
    assert not re.search(r"Math\.random\s*\(", source), "fixtures must be deterministic"


def test_health_and_status_payloads(fixtures):
    health = fixtures["fixtures"]["health"]
    assert_fields(health, ["status", "version", "uptime_s", "commit"], "GET /health")
    assert health["status"] == "ok"

    status = fixtures["fixtures"]["status"]
    assert_fields(status, [
        "version", "commit", "uptime_s", "mode", "dry_run", "ha_connected", "ha_last_error",
        "location", "now", "price_now", "price_rank_pct", "pv_now_w", "load_now_w", "grid_now_w",
        "batteries", "active_plan", "last_plan_at", "last_tick_at", "next_cheap_window",
        "next_expensive_window", "override", "pending_holds", "mode_persisted", "alerts",
        "provider_status", "scheduler_healthy",
    ], "GET /status")
    assert status["mode"] in {"off", "advisory", "auto"}
    for alert in status["alerts"]:
        assert_fields(alert, ["ts", "level", "code", "message", "detail", "acknowledged"], "AlertRecord")
    for battery in status["batteries"]:
        assert_fields(battery, [
            "id", "available", "soc_pct", "power_w", "capacity_kwh", "energy_kwh", "pv_power_w",
            "temperature_c", "read_at", "last_error", "stale", "extra",
        ], "BatteryState")
    for provider in status["provider_status"]:
        assert_fields(provider, ["kind", "name", "active", "ok", "last_success", "last_error", "stale", "detail"], "ProviderStatus")


def test_plan_payload_matches_section_10(fixtures):
    plan = fixtures["fixtures"]["plan"]
    assert_fields(plan, ["generated_at", "horizon_start", "horizon_end", "feasible", "summary", "slots", "batteries"], "GET /plan")
    # plan.summary is a CLOSED payload: no subset check here on purpose — its exact key set is
    # asserted against app/api.py.plan_summary_dict in test_plan_summary_matches_the_api_serializer,
    # which is what catches an added/dropped field (a subset check silently missed objective_eur).
    assert plan["slots"], "plan fixture has no slots"
    slot_fields = ["t", "buy", "sell", "pv_kw", "load_kw", "load_p10", "load_p90", "soc_kwh", "soc_pct",
                   "power_w", "action", "reason", "price_stale", "pv_source", "price_source"]
    actions = {"hold", "charge", "discharge", "pv_charge", "export", "self_consumption", "reserve"}
    for slot in plan["slots"]:
        assert_fields(slot, slot_fields, "PlanSlot (GET /plan)")
        assert slot["action"] in actions, slot["action"]
        assert isinstance(slot["price_stale"], bool)
    for battery in plan["batteries"]:
        assert_fields(battery, ["id", "name", "share_w"], "plan batteries[]")


def test_plan_summary_matches_the_api_serializer(fixtures):
    """The exact key set of app/api.py:plan_summary_dict, plus the two new derived values.

    Which checks are exact and which stay subset-only:
    - EXACT (closed payload): `plan.summary` — every key here is emitted by one serializer
      (`plan_summary_dict`) and the UI renders all of them, so drift must fail loudly.
    - SUBSET on purpose (open-ended payloads): `BatteryConfig` (drivers add fields),
      `/status`, `/health`, discovery candidates, setting descriptors and forecast notes all
      accept extra provider/driver keys without the UI breaking; those keep `assert_fields`.
    """
    from energy_manager.app.api import plan_summary_dict
    from energy_manager.app.models import PlanSummary

    plan = fixtures["fixtures"]["plan"]
    summary = plan["summary"]
    expected = set(plan_summary_dict(PlanSummary()))
    assert set(summary) == expected, (
        f"mock plan.summary drifted from api.py: missing={sorted(expected - set(summary))} "
        f"extra={sorted(set(summary) - expected)}"
    )

    # The two derived values must be reproducible from the mock's own summary numbers.
    buys = sorted(slot["buy"] for slot in plan["slots"])
    mid = len(buys) // 2
    median_buy = buys[mid] if len(buys) % 2 else (buys[mid - 1] + buys[mid]) / 2
    assert summary["break_even_sell_eur_per_kwh"] == pytest.approx(
        median_buy / summary["round_trip_efficiency"] + summary["cycle_cost_eur"] / summary["eta_discharge"],
        rel=1e-12,
    )
    # objective = grid cost + cycle cost - salvage x terminal stored energy, rounded to cents
    expected_objective = round(
        summary["cost_estimate_eur"] + summary["cycle_cost_eur"]
        - summary["eta_discharge"] * median_buy * plan["slots"][-1]["soc_kwh"],
        2,
    )
    assert summary["objective_eur"] == pytest.approx(expected_objective, abs=1e-9)
    # and the objective uses the same unit the UI shows: EUR, not cents
    assert abs(summary["objective_eur"]) < 100


def test_forecast_payloads(fixtures):
    price = fixtures["fixtures"]["price"]
    assert_fields(price, ["kind", "generated_at", "source", "stale", "slots", "provider", "contract", "notes", "reason", "hint"], "GET /forecast?kind=price")
    assert price["kind"] == "price"
    # provider/contract are 0.3.0 additions; a populated panel carries no reason/hint (additive)
    assert price["provider"] and isinstance(price["provider"], str)
    assert price["contract"] and isinstance(price["contract"], str)
    assert isinstance(price["notes"], list) and price["notes"], "the populated price forecast carries notes"
    assert price["reason"] is None and price["hint"] is None
    for slot in price["slots"]:
        assert_fields(slot, ["t", "buy", "sell", "source", "is_actual", "stale"], "PriceSlot")

    solar = fixtures["fixtures"]["solar"]
    assert_fields(solar, ["kind", "generated_at", "source", "stale", "slots", "provider", "contract", "planes", "correction_applied", "notes"], "GET /forecast?kind=solar")
    for slot in solar["slots"]:
        assert_fields(slot, ["t", "pv_kw", "uncertainty_pct"], "SolarSlot")
    for plane in solar["planes"]:
        assert_fields(plane, ["kwp", "tilt_deg", "azimuth_deg", "damping"], "SolarForecast.planes")

    load = fixtures["fixtures"]["load"]
    assert_fields(load, ["kind", "generated_at", "source", "stale", "slots", "confidence", "provider", "contract", "notes"], "GET /forecast?kind=load")
    assert load["confidence"] in {"low", "medium", "high"}
    for slot in load["slots"]:
        assert_fields(slot, ["t", "load_kw", "p10_kw", "p90_kw"], "LoadSlot")


def test_forecast_empty_panel_carries_reason_and_zone_hint(fixtures):
    """The empty-panel shape: a `reason` slug and a one-sentence `hint`. For a failed day-ahead
    lookup the hint names the configured bidding zone (SPEC §4.1), rendered verbatim by the UI."""
    if NODE is None:
        pytest.skip("node is not installed")
    script = """
    const fs = require("fs");
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    (async function () {
      const empty = await SEM_MOCK.handle("GET", "/api/v1/forecast?kind=price&empty=1", {});
      const bzn = SEM_MOCK.fixtures().config.settings.filter(function (s) { return s.key === "price.bzn"; })[0].value;
      console.log(JSON.stringify({ empty: empty, bzn: bzn }));
    })().catch(function (err) { console.error(err && err.message ? err.message : err); process.exit(1); });
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([NODE, script_path], capture_output=True, text=True, cwd=str(WEB))
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    empty = data["empty"]
    assert empty["slots"] == [], "the demo panel is empty"
    assert_fields(empty, ["kind", "generated_at", "source", "provider", "contract", "notes", "reason", "hint"], "empty price forecast")
    assert empty["reason"] == "provider_failed"
    # the config provider is day_ahead, so the hint names the configured bidding zone verbatim
    assert empty["provider"] == "day_ahead"
    assert data["bzn"] in empty["hint"], "the hint names the configured bidding zone"
    assert "no day-ahead prices" in empty["hint"].lower()


def test_batteries_and_discovery_payloads(fixtures):
    payload = fixtures["fixtures"]["batteries"]
    assert_fields(payload, ["batteries"], "GET /batteries")
    config_fields = ["id", "name", "driver", "capacity_kwh", "min_soc_pct", "max_soc_pct", "max_charge_w",
                     "max_discharge_w", "eta_charge", "eta_discharge", "mapping", "enabled"]
    for record in payload["batteries"]:
        assert_fields(record, ["config", "state", "capabilities", "mapping_resolved", "discovery"], "GET /batteries[]")
        assert_fields(record["config"], config_fields, "BatteryConfig")
        assert record["config"]["mapping"], "mock battery must have a mapping"

    for kind, payload in fixtures["fixtures"]["discover"].items():
        assert_fields(payload, ["candidates"], f"GET /discover?kind={kind}")
        assert len(payload["candidates"]) <= 50
        for candidate in payload["candidates"]:
            assert_fields(candidate, ["entity_id", "name", "unit", "device_class", "state", "platform", "suggested_role", "confidence"], "DiscoveryCandidate")
            assert 0.0 <= candidate["confidence"] <= 1.0


def test_config_payload_exposes_every_frozen_setting(fixtures):
    config = fixtures["fixtures"]["config"]
    assert_fields(config, ["options", "settings", "price_warnings"], "GET /config")
    assert_fields(config["options"], ["mode", "dry_run", "log_level", "currency", "timezone", "latitude_override", "longitude_override"], "config.options")
    keys = [setting["key"] for setting in config["settings"]]
    missing = [key for key in FROZEN_SETTING_KEYS if key not in keys]
    assert not missing, f"config fixture is missing frozen settings: {missing}"
    for setting in config["settings"]:
        assert_fields(setting, ["key", "value", "type", "default", "label", "help", "group", "choices"], f"setting {setting.get('key')}")
        assert setting["type"] in {"bool", "int", "float", "str", "password", "json", "list"}, setting["type"]
        assert isinstance(setting["choices"], list), setting["key"]
    groups = {s["group"] for s in config["settings"]}
    assert {"Safety", "Price", "Solar", "Load", "Grid", "Optimizer", "Notify", "Batteries", "General"} <= groups

    by_key = {s["key"]: s for s in config["settings"]}
    assert by_key["optimizer.allow_export"]["default"] is False, "allow_export must ship off until a feed-in tariff exists"
    assert by_key["optimizer.allow_export"]["group"] == "Safety"
    for key, kind, default in [
        ("price.feed_in_eur_per_kwh", "float", None),
        ("price.feed_in_entity", "str", ""),
        ("price.feed_in_tax_inclusive", "bool", True),
        ("control.grid_follow_max_w", "float", 0),
        # provider/contract selection + the fixed-tariff path (config.DEFAULTS)
        ("price.provider", "str", "entity"),
        ("price.contract", "str", "dynamic_hourly"),
        ("price.fixed_price_eur_per_kwh", "float", None),
        ("price.fixed_price_entity", "str", ""),
        ("price.fixed_day_start_hour", "int", 7),
        ("price.fixed_night_start_hour", "int", 23),
    ]:
        assert by_key[key]["type"] == kind, key
        assert by_key[key]["default"] == default, key
    assert by_key["price.provider"]["choices"] == ["entity", "zonneplan", "day_ahead", "fixed"]
    assert by_key["price.contract"]["choices"] == ["dynamic_quarter_hourly", "dynamic_hourly", "fixed_single", "fixed_day_night"]
    # price_warnings (SPEC §4.1): [{key, message}]; the mock ships a stated mismatch so the UI path
    # is exercised (day_ahead provider + fixed_single contract keeps the provider, it is a warning).
    warnings = config["price_warnings"]
    assert isinstance(warnings, list) and warnings, "the mock must demonstrate a price_warning"
    for warning in warnings:
        assert_fields(warning, ["key", "message"], "price_warnings[]")
        assert isinstance(warning["key"], str) and isinstance(warning["message"], str)
    assert warnings[0]["key"] == "price.contract"
    # all control.* keys are flagged as Safety by the backend rule
    assert all(s["group"] in {"Safety", "General"} for s in config["settings"] if s["key"].startswith("control."))
    assert by_key["price.feed_in_eur_per_kwh"]["help"], "empty feed-in path needs explanatory help"
    assert "sell price unknown" in by_key["price.feed_in_eur_per_kwh"]["help"].lower()


def test_savings_and_accuracy_payloads(fixtures):
    savings = fixtures["fixtures"]["savings"]
    for range_name, payload in savings.items():
        assert_fields(payload, ["range", "buckets", "totals", "baseline_note"], f"GET /savings?range={range_name}")
        assert payload["buckets"], f"{range_name} has no buckets"
        assert payload["baseline_note"] == (
            "Baseline: the same measured consumption at the same prices with the battery doing nothing. "
            "Savings = baseline cost - actual cost."
        ), "mock baseline_note must match analytics.BASELINE_NOTE verbatim"
        for bucket in payload["buckets"]:
            assert_fields(bucket, ["key", "start", "end", "import_kwh", "export_kwh", "pv_kwh", "cost_eur",
                                   "revenue_eur", "baseline_cost_eur", "savings_eur", "cycles"], "SavingsBucket")
            if bucket["baseline_cost_eur"] is None:
                assert bucket["savings_eur"] is None, "unknown baseline must imply unknown savings, never 0"
        known = [b for b in payload["buckets"] if b["baseline_cost_eur"] is not None]
        if known:
            assert isinstance(payload["totals"]["baseline_cost_eur"], (int, float)), range_name
            assert isinstance(payload["totals"]["savings_eur"], (int, float)), range_name
        else:
            assert payload["totals"]["baseline_cost_eur"] is None, range_name
            assert payload["totals"]["savings_eur"] is None, range_name
    assert "arbitrage_kwh" in savings["month"]["totals"]
    assert "self_consumption_kwh" in savings["month"]["totals"]
    assert "battery_throughput_kwh" in savings["month"]["totals"]
    # the demo must exercise the unknown state, not just all-known buckets
    assert any(b["baseline_cost_eur"] is None for b in savings["month"]["buckets"])
    assert savings["all"]["totals"]["savings_eur"] is None

    accuracy = fixtures["fixtures"]["accuracy"]
    for kind in ("pv", "load", "price"):
        report = accuracy[kind]
        assert_fields(report, ["kind", "days", "mape_pct", "bias_pct", "mae", "samples", "series"], f"AccuracyReport[{kind}]")
        assert report["kind"] == kind
        for point in report["series"]:
            assert_fields(point, ["day", "mape_pct", "bias_pct"], "AccuracyReport.series[]")


def test_log_and_providers_payloads(fixtures):
    log = fixtures["fixtures"]["log"]
    assert_fields(log, ["entries"], "GET /log")
    assert log["entries"], "log fixture is empty"
    kinds = set()
    for entry in log["entries"]:
        assert_fields(entry, ["ts", "kind", "level", "code", "message", "detail"], "LogEntry")
        assert entry["level"] in {"info", "warning", "error"}
        kinds.add(entry["kind"])
    assert {"dispatch", "event"} <= kinds

    providers = fixtures["fixtures"]["providers"]
    assert_fields(providers, ["solar", "price", "load"], "GET /providers")
    for kind in ("solar", "price", "load"):
        for provider in providers[kind]:
            assert_fields(provider, ["kind", "name", "active", "ok", "last_success", "last_error", "stale", "detail"], f"ProviderStatus[{kind}]")


def test_mock_entities_use_the_reserved_namespace(fixtures):
    allowed = re.compile(r"^(sensor|binary_sensor|number|select|switch)\.example_[a-z0-9_]+$")
    discovery = fixtures["fixtures"]["discover"]
    for kind, payload in discovery.items():
        for candidate in payload["candidates"]:
            assert allowed.match(candidate["entity_id"]), f"{candidate['entity_id']} is not a reserved-namespace fake id"
    for record in fixtures["fixtures"]["batteries"]["batteries"]:
        for role, entity in record["config"]["mapping"].items():
            if role.endswith("_entity"):
                assert allowed.match(str(entity)), f"mapping {role}={entity} is not a reserved-namespace fake id"


def test_mock_contains_no_instance_values():
    source = (WEB / "mock.js").read_text(encoding="utf-8")
    for pattern in [r"\b192\.168\.", r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", r"[\w.+-]+@[\w-]+\.[a-z]{2,}"]:
        assert not re.search(pattern, source), f"mock.js contains instance-looking data matching {pattern}"
    assert "Example Street 1" in source, "the fake address label should be obviously synthetic"


def test_mock_battery_discovery_carries_the_picker_fields(fixtures):
    """?mock=1 must demonstrate the picker: device fields, min/max and a read-only look-alike."""
    if NODE is None:
        pytest.skip("node is not installed")
    rows = fixtures["fixtures"]["discover"]["battery"]["candidates"]
    for candidate in rows:
        for field in ["device_id", "device_name", "manufacturer", "model", "min", "max"]:
            assert field in candidate, f"{candidate['entity_id']} is missing {field} for the picker"
    device_ids = {row["device_id"] for row in rows}
    assert len(device_ids) >= 2, "the mock needs more than one device to show the grouping"

    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "?mock=1", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    const A = globalThis.SEM_APP;
    (async function () {
      const res = await A.get("/discover", { kind: "battery", search: "" });
      const groups = A.batteryGroupsFromCandidates(res.candidates);
      console.log(JSON.stringify({
        groups: groups.map(function (g) {
          return {
            label: g.label, deviceId: g.deviceId, manufacturer: g.manufacturer, model: g.model,
            platform: g.platform, mappable: g.mappable, writableControl: g.writableControl,
            readOnlyControl: g.readOnlyControl, controlReady: g.controlReady, skipped: g.skipped,
            selection: A.batterySelection(g, ["battery_1"]),
            colliding: A.batterySelection(g, ["battery_1", "example_battery_1"])
          };
        }),
        searchByModel: A.batterySearchGroups(res.candidates, "Model Two").groups.map(function (g) { return g.label; }),
        searchMiss: A.batterySearchGroups(res.candidates, "nothing like this").fromServer
      }));
    })().catch(function (err) { console.error(err && err.message ? err.message : err); process.exit(1); });
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run([NODE, script_path], capture_output=True, text=True, cwd=str(WEB))
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)

    labels = [group["label"] for group in data["groups"]]
    assert labels == ["Example Battery 1", "Example Sensor Bank"], labels
    battery, sensors = data["groups"]
    assert battery["deviceId"] == "example_dev_battery_1"
    assert battery["platform"] == "zendure_ha" and battery["writableControl"] is True
    assert battery["controlReady"] is True, "the mock's zendure battery must be the first picker row"
    assert battery["selection"]["id"] == "example_battery_1", "the configured mock id does not collide with the slug"
    assert battery["colliding"]["id"] == "example_battery_1_2", "a colliding id gets _2"
    assert battery["selection"]["driver"] == "zendure"
    assert battery["selection"]["mapping"]["power_setpoint_entity"] == "number.example_battery_input_limit"
    assert battery["selection"]["mapping"]["soc_entity"] == "sensor.example_battery_soc_1"
    # only numbers discovery states: capacity 12 kWh and the 800 W the signed setpoint allows
    assert battery["selection"]["numbers"] == {"capacity_kwh": 12.0, "max_charge_w": 800, "max_discharge_w": 800}
    # the look-alike keeps its measurement roles and gets no control entity at all
    assert sensors["readOnlyControl"] is True and sensors["controlReady"] is False
    assert sensors["skipped"] == ["power_setpoint"]
    assert sensors["selection"]["mapping"] == {
        "capacity_entity": "sensor.example_sensor_bank_capacity",
        "soc_entity": "sensor.example_sensor_bank_soc",
    }
    assert data["searchByModel"] == ["Example Sensor Bank"], "search reaches the model, not just entity text"
    assert data["searchMiss"] is True, "a local miss falls back to the server-side search"
