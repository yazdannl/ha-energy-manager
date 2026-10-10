"""WP3 (UI) static asset checks.

These run without a browser: they verify that the SPA is self-contained, that every asset
index.html references exists, that the JavaScript parses, and that the accessibility/safety rules
from SPEC §13 are present in the source. Node-based checks are skipped when node is absent.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "energy_manager" / "app" / "web"
VENDOR = WEB / "vendor"
NODE = shutil.which("node")

VIEWS = ["dashboard", "plan", "forecasts", "batteries", "savings", "settings", "log"]

REQUIRED_FILES = [
    "index.html",
    "app.js",
    "charts.js",
    "mock.js",
    "style.css",
    "README.md",
    "vendor/uPlot.min.js",
    "vendor/uPlot.min.css",
    "vendor/README.md",
    "vendor/LICENSE.uPlot.txt",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_js_comments(text: str) -> str:
    """Remove block and line comments so pattern checks only see executable source."""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    return re.sub(r"(^|[^:])//[^\n]*", r"\1", text, flags=re.MULTILINE)


def run_node_script(body: str) -> str:
    """Run a commonjs script body in the web directory."""
    if NODE is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([NODE, "-e", body], capture_output=True, text=True, cwd=str(WEB))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_all_required_assets_exist():
    for name in REQUIRED_FILES:
        assert (WEB / name).is_file(), f"missing asset: {name}"


def test_index_references_only_existing_relative_assets():
    html = read(WEB / "index.html")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs, "index.html references no assets at all"
    for ref in refs:
        if ref.startswith("#"):
            continue
        assert "://" not in ref, f"index.html must not reference external URLs: {ref}"
        assert not ref.startswith("/"), f"ingress-safe relative URLs only, got: {ref}"
        assert (WEB / ref).is_file(), f"index.html references a missing asset: {ref}"


def test_index_has_no_inline_handlers_scripts_or_styles():
    html = read(WEB / "index.html")
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler found in index.html"
    for attrs in re.findall(r"<script\b([^>]*)>", html):
        assert "src=" in attrs, f"inline script found: <script{attrs}>"
    assert "<style" not in html.lower()


def test_index_nav_links_cover_every_view():
    html = read(WEB / "index.html")
    hrefs = set(re.findall(r'href="#(/[a-z]+)"', html))
    assert hrefs == {"/" + view for view in VIEWS}, hrefs


def test_javascript_parses():
    if NODE is None:
        pytest.skip("node is not installed")
    for path in sorted(WEB.glob("*.js")):
        proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{path.name} failed node --check:\n{proc.stderr}"


def test_no_unsafe_dom_apis_and_no_absolute_api_urls():
    for path in sorted(WEB.glob("*.js")) + [WEB / "index.html"]:
        text = read(path) if path.suffix == ".html" else strip_js_comments(read(path))
        assert "innerHTML" not in text, f"innerHTML is forbidden in {path.name}"
        assert "insertAdjacentHTML" not in text, f"insertAdjacentHTML is forbidden in {path.name}"
        assert "document.write" not in text, f"document.write is forbidden in {path.name}"
        assert not re.search(r"\beval\s*\(", text), f"eval is forbidden in {path.name}"
        assert not re.search(r"new\s+Function\s*\(", text), f"new Function is forbidden in {path.name}"
    app = strip_js_comments(read(WEB / "app.js"))
    assert "/api/" not in app, "app.js must never build an absolute /api/ URL"
    assert "basePath()" in app and "location.pathname" in app, "the API base path must be derived from location.pathname"


def test_no_inline_event_handler_assignment_in_app():
    app = read(WEB / "app.js")
    assert not re.search(r"\.on[a-z]+\s*=", app), "assign event handlers with addEventListener only"


def test_hash_routes_are_registered_and_resolve():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/api/hassio_ingress/example/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({
      routes: Object.keys(A.ROUTES),
      types: Object.keys(A.ROUTES).map(function (r) { return typeof A.ROUTES[r]; }),
      resolved: A.ROUTES && Object.keys(A.ROUTES) ? [
        "/dashboard", "/plan", "/forecasts", "/batteries", "/savings", "/settings", "/log"
      ].map(function (r) { return A.normalizeRoute(r); }) : [],
      fallback: A.normalizeRoute("/nope"),
      empty: A.normalizeRoute(""),
      ingressPath: A.apiPath("/status", {}),
      plainPath: A.apiPath("/plan", { hours: 24 }),
      split: A.splitHash("#/plan?hours=12"),
      noHash: A.splitHash(""),
      mocked: A.isMock
    }));
    """
    data = json.loads(run_node_script(script))
    assert data["routes"] == ["/" + view for view in VIEWS]
    assert set(data["types"]) == {"function"}
    assert data["resolved"] == ["/" + view for view in VIEWS]
    assert data["fallback"] == "/dashboard"
    assert data["empty"] == "/dashboard"
    assert data["ingressPath"] == "/api/hassio_ingress/example/api/v1/status"
    assert data["plainPath"] == "/api/hassio_ingress/example/api/v1/plan?hours=24"
    assert data["split"] == {"route": "/plan", "query": {"hours": "12"}}
    assert data["noHash"]["route"] == "/dashboard"
    assert data["mocked"] is False


def test_mock_flag_detected_on_any_query_string_order():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "?foo=1&mock=1", hash: "#/log" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    console.log(String(globalThis.SEM_APP.isMock));
    """
    assert run_node_script(script).strip() == "true"


def test_every_format_helper_carries_a_unit():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify([
      A.fmtPower(2450), A.fmtPower(240), A.fmtKwh(7.5), A.fmtPrice(0.214), A.fmtEur(12.3), A.fmtPct(62.5)
    ]));
    """
    values = json.loads(run_node_script(script))
    units = ["kW", "W", "kWh", "EUR/kWh", "EUR", "%"]
    for value, unit in zip(values, units):
        assert unit in value, f"{value!r} does not carry the unit {unit!r}"


def test_dangerous_actions_require_confirmation_steps():
    app = read(WEB / "app.js")
    assert "confirmAction" in app
    # auto mode, dry-run off, stop-now, test discharge, battery delete and safety settings
    assert app.count("requireCheck: true") >= 5, "dangerous actions must have explicit confirm steps"
    for needle in ["Enable automatic control?", "Turn dry run off?", "Stop now?", "I confirm these entities belong to this battery"]:
        assert needle in app, f"missing confirmation copy: {needle}"


def test_no_optimistic_ui_for_writes():
    app = read(WEB / "app.js")
    assert "pending" in app and "Confirmed by the server" in app
    # state is refreshed from the server after every successful write
    assert app.count("renderRoute();") >= 5


def test_stale_data_and_loading_states_are_handled():
    app = read(WEB / "app.js")
    for needle in ["staleBadge", "Stale prices", "Measurements are stale", "Loading…", "Could not load"]:
        assert needle in app, f"missing stale/loading handling: {needle}"


def test_discharge_result_text_shows_applied_power_and_seconds():
    """The dialog must render the server-confirmed applied_w and the exact seconds (no rounding up)."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const outcome = globalThis.SEM_APP.testDischargeOutcome;
    console.log(JSON.stringify([
      outcome({ applied_w: -200, seconds: 7.5, intent: "discharge", ok: true, code: "ok" }),
      outcome({ applied_w: -150, seconds: 10, intent: "discharge", ok: true }),
      outcome({ applied_w: 0, seconds: 5, intent: "discharge", ok: false, code: "driver_error", message: "no read-back" }),
      outcome({ applied_w: -200, seconds: 12.5 })
    ]));
    """
    texts = json.loads(run_node_script(script))
    assert "200 W" in texts[0] and "7.5 s" in texts[0] and "discharge" in texts[0]
    assert "server-confirmed" in texts[0]
    assert "10 s" in texts[1] and "10.0 s" not in texts[1], "whole seconds must not be padded or rounded"
    assert texts[2].startswith("Test discharge FAILED") and "5 s" in texts[2] and "driver_error" in texts[2]
    assert "12.5 s" in texts[3] and "discharge" in texts[3], "missing intent defaults to discharge"
    app = read(WEB / "app.js")
    assert "Discharge power (W)" in app, "the field must be labelled as discharge power"
    assert "intent: \"discharge\"" in app, "the UI must send the explicit signed intent"
    assert "no rounding up" in app or "exact seconds" in app
    assert "60 s minimum" not in app and "at least 60" not in app, "the old 1-minute floor copy must be gone"


def test_settings_flags_money_and_safety_keys():
    app = read(WEB / "app.js")
    assert re.search(r"price\.feed_in_", app), "feed-in keys must be highlighted as money/safety settings"
    assert "grid_follow_max_w" in app
    assert "money/safety" in app, "safety fields need a visible (not colour-only) marker"
    assert "No export revenue configured" in app, "an empty feed-in path must read as no export revenue"
    assert "Export is enabled without a feed-in tariff" in app


def test_dashboard_surfaces_pending_holds_and_mode_persistence():
    app = read(WEB / "app.js")
    assert "pending_holds" in app
    assert "mode_persisted" in app
    assert "Safety state" in app, "pending holds / unpersisted mode need one compact banner"
    assert "real setpoint may still be in force" in app
    assert "NOT persisted" in app
    assert "Mode persisted to the settings file" in app
    assert "data-stop-result" in app


def test_savings_unknown_state_copy():
    app = read(WEB / "app.js")
    assert "Baseline and savings are unknown for this range" in app
    assert "Unknown is not zero" in app
    assert "no load forecast for this range" in app
    assert "skipped, not plotted as zero" in app
    charts = read(WEB / "charts.js")
    assert "No baseline or savings to plot for this range" in charts
    assert "BASELINE_FALLBACK" in app
    assert "the same measured consumption at the same prices" in app, "fallback must match analytics.BASELINE_NOTE"


def test_no_watchdog_copy_matches_the_safety_model():
    app = read(WEB / "app.js")
    assert "the plan dispatcher writes nothing" in app, "dry run must not promise 'no hardware writes' at all"
    assert "no hardware writes" not in app
    assert "no watchdog" in app.lower()
    assert "use Stop now" in app or "Use Stop now" in app


def test_battery_efficiency_payload_derives_and_sends_the_right_shape():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({
      split: A.batteryEfficiencyPayload("90", "", ""),
      advanced: A.batteryEfficiencyPayload("75", "95", "92"),
      oneSide: A.batteryEfficiencyPayload("90", "95", ""),
      tooLow: A.batteryEfficiencyPayload("0", "", ""),
      tooHigh: A.batteryEfficiencyPayload("100.1", "", ""),
      garbage: A.batteryEfficiencyPayload("abc", "", ""),
      atHundred: A.batteryEfficiencyPayload("100", "", ""),
      advancedOutOfRange: A.batteryEfficiencyPayload("90", "120", "90")
    }));
    """
    data = json.loads(run_node_script(script))
    # round-trip only: the pct is sent and NO per-direction values are invented
    assert data["split"]["payload"] == {"round_trip_efficiency_pct": 90.0}
    assert data["split"]["status"]["winner"] == "round_trip"
    # Exact expectations are written as expressions: tools/audit.sh (SPEC R1) flags any decimal literal
    # with 5+ fractional digits as a plausible coordinate, so never paste full-precision constants here.
    assert data["split"]["status"]["split_pct"] == pytest.approx(100 * math.sqrt(0.9), abs=1e-4)
    # advanced: both etas are sent (as fractions) and they win over the round-trip number
    assert data["advanced"]["payload"]["round_trip_efficiency_pct"] == 75.0
    assert abs(data["advanced"]["payload"]["eta_charge"] - 0.95) < 1e-12
    assert abs(data["advanced"]["payload"]["eta_discharge"] - 0.92) < 1e-12
    assert data["advanced"]["status"]["winner"] == "advanced"
    assert abs(data["advanced"]["status"]["effective_round_trip_pct"] - 87.4) < 1e-9
    assert data["atHundred"]["payload"]["round_trip_efficiency_pct"] == 100.0
    for key in ["oneSide", "tooLow", "tooHigh", "garbage", "advancedOutOfRange"]:
        assert "error" in data[key], f"{key} should be rejected: {data[key]}"
        assert "payload" not in data[key]
    app = read(WEB / "app.js")
    assert "Round-trip efficiency (%)" in app and "Advanced: per-direction efficiency" in app
    assert "Advanced per-direction values win" in app and "Round trip split evenly" in app
    assert "25 ct is only the 80 % case" in app, "SPEC §6 worked example must appear in the copy"
    assert "20 / (result.status.effective_round_trip_pct / 100)" in app, "the example is computed, not a constant"


def test_battery_break_even_maths():
    """The card numbers: 20 ct at 75 % round trip breaks even at 26.67 ct, not 25 ct."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const cfg75 = { round_trip_efficiency_pct: 75 };
    const cfg80 = { round_trip_efficiency_pct: 80 };
    const cfg90 = { round_trip_efficiency_pct: 90 };
    const asymmetric = { eta_charge: 0.95, eta_discharge: 0.92, round_trip_efficiency_pct: 87.4 };
    console.log(JSON.stringify({
      be75: A.breakEvenSellEur(0.2, cfg75, 0),
      be80: A.breakEvenSellEur(0.2, cfg80, 0),
      be90: A.breakEvenSellEur(0.2, cfg90, 0),
      be75Wear: A.breakEvenSellEur(0.2, cfg75, 0.02),
      asymmetric: A.breakEvenSellEur(0.2, asymmetric, 0),
      stored75: A.storedKwhCostEur(0.2, cfg75, 0),
      unknownBuy: A.breakEvenSellEur(null, cfg75, 0),
      etaOnly: A.batteryEfficiency({ eta_charge: 0.9, eta_discharge: 0.9 }).round_trip_pct,
      pctOnlyEta: A.batteryEfficiency(cfg75).eta_charge
    }));
    """
    data = json.loads(run_node_script(script))
    assert round(data["be75"] * 100, 2) == 26.67, "20 ct at 75 % must break even at 26.67 ct"
    assert round(data["be80"] * 100, 2) == 25.0, "25 ct is the 80 % case"
    assert round(data["be90"] * 100, 2) == 22.22
    assert round(data["be75Wear"] * 100, 2) == 28.98, "wear adds cycle_cost / eta_discharge"
    assert round(data["asymmetric"] * 100, 2) == 22.88, "advanced override uses eta_charge x eta_discharge"
    assert round(data["stored75"] * 100, 2) == 23.09, "stored kWh all-in = buy / eta_charge at zero wear"
    assert data["unknownBuy"] is None
    assert abs(data["etaOnly"] - 81.0) < 1e-9
    assert data["pctOnlyEta"] == pytest.approx(math.sqrt(0.75), abs=1e-6), "pct-only payload splits evenly"


def test_plan_efficiency_and_break_even_helpers():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const eta90 = Math.sqrt(0.9);
    const direct = A.planEfficiency({ eta_charge: eta90, eta_discharge: eta90, round_trip_efficiency: 0.9 });
    const pctOnly = A.planEfficiency({ round_trip_efficiency_pct: 90 });
    const percentAsWhole = A.planEfficiency({ round_trip_efficiency: 90 });
    const derived = A.planEfficiency({ round_trip_efficiency: 0.81 });
    console.log(JSON.stringify({
      direct: direct, pctOnly: pctOnly, percentAsWhole: percentAsWhole, derived: derived,
      missing: A.planEfficiency({}), noSummary: A.planEfficiency(null),
      beDerived: A.planBreakEvenEur(0.2, derived, 0.02),
      beNinety: A.planBreakEvenEur(0.2, direct, 0.02),
      beNoBuy: A.planBreakEvenEur(null, derived, 0)
    }));
    """
    data = json.loads(run_node_script(script))
    assert abs(data["direct"]["round_trip"] - 0.9) < 1e-9
    assert data["direct"]["round_trip_pct"] == pytest.approx(90.0), "the plan helper reports the percentage too"
    assert data["pctOnly"]["eta_charge"] == pytest.approx(math.sqrt(0.9), abs=1e-6)
    assert abs(data["percentAsWhole"]["round_trip"] - 0.9) < 1e-9
    assert abs(data["derived"]["eta_charge"] - 0.9) < 1e-9
    assert data["missing"] is None and data["noSummary"] is None
    assert round(data["beDerived"] * 100, 2) == 26.91, "0.81 round trip: 0.2/0.81 + 0.02/0.9"
    assert round(data["beNinety"] * 100, 2) == 24.33, "0.9 round trip: 0.2/0.9 + 0.02/0.9487"
    assert data["beNoBuy"] is None
    app = read(WEB / "app.js")
    charts = read(WEB / "charts.js")
    assert "break-even after round-trip loss" in charts
    assert "break-even after round-trip loss" in app
    assert "Assumed round trip" in app


def test_efficiency_loss_reason_wording():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({ eff: A.reasonText("efficiency_loss"), spread: A.reasonText("spread_below_threshold") }));
    """
    data = json.loads(run_node_script(script))
    assert data["eff"] == "idle: sale price does not cover the round-trip loss"
    assert data["spread"] == "below the spread floor you set"
    assert data["eff"] != data["spread"]


def test_stop_now_uses_the_atomic_endpoint():
    app = read(WEB / "app.js")
    assert 'post("/stop")' in app, "Stop now must call the atomic POST /stop"
    assert 'post("/override", { power_w: 0, minutes: 1 })' not in app, "the old override+mode stop sequence must be gone"
    assert "renderStopResults" in app
    assert "did NOT confirm the stop" in app, "failed per-battery stops must be visible"
    assert "writes to the hardware for real" in app, "the stop must be labelled as a real write"
    assert "aria-busy" in app and "stop.disabled = true" in app, "the stop button must be disabled while in flight"
    assert "stopResult" in app, "stop results must be rendered and kept visible"


def test_style_is_dark_first_responsive_and_motion_aware():
    css = read(WEB / "style.css")
    assert "prefers-color-scheme: light" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "focus-visible" in css
    assert "@media" in css and "max-width" in css, "responsive breakpoints are required (360px..desktop)"
    assert "minmax(" in css, "cards must reflow on narrow screens"
    assert "color-scheme: dark" in css, "dark-first design"
    assert "@import" not in css and "url(http" not in css, "no external fonts or stylesheets"


def test_vendored_uplot_keeps_licence_and_version():
    js = read(VENDOR / "uPlot.min.js")
    css = read(VENDOR / "uPlot.min.css")
    vendor_readme = read(VENDOR / "README.md")
    licence = read(VENDOR / "LICENSE.uPlot.txt")
    assert "MIT Licensed" in js and "Leon Sorokin" in js, "upstream licence header missing from vendored JS"
    assert "1.6.32" in js and "1.6.32" in css
    assert "var uPlot" in js, "vendored file does not look like the uPlot IIFE build"
    assert "MIT" in licence and "Leon Sorokin" in licence
    for needle in ["1.6.32", "MIT", "github.com/leeoniya/uPlot", "sha256", "19c8d4c6"]:
        assert needle.lower() in vendor_readme.lower() or needle in vendor_readme, f"vendor README lacks {needle}"


def test_web_readme_documents_mock_mode_and_assets():
    readme = read(WEB / "README.md")
    for needle in ["?mock=1", "app.js", "charts.js", "mock.js", "style.css", "vendor/", "uPlot", "ingress"]:
        assert needle in readme, f"web/README.md must mention {needle}"


def test_ui_docs_describe_all_views_and_conventions():
    doc = (ROOT / "docs" / "UI.md").read_text(encoding="utf-8")
    for view in VIEWS:
        assert "#/" + view in doc, f"docs/UI.md does not document the #{view} route"
    for needle in ["?mock=1", "360 px", "uPlot", "pending", "baseline", "confirm", "ingress", "prefers-reduced-motion"]:
        assert needle.lower() in doc.lower(), f"docs/UI.md is missing {needle}"
    assert "sensor.example_" in doc, "docs/UI.md should show reserved-namespace fake ids only"


def test_no_private_or_instance_values_in_web_assets():
    forbidden = [
        re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE),
    ]
    for path in sorted(WEB.rglob("*")):
        if not path.is_file() or "vendor" in path.parts:
            continue
        text = read(path)
        for pattern in forbidden:
            match = pattern.search(text)
            assert match is None, f"{path.name} contains instance-looking data: {match.group(0)}"


# --- battery discovery picker (SPEC §5/§10) ------------------------------------------------
# Every fixture below is synthetic: reserved-namespace entity ids and obviously fake devices.


def _picker_rows():
    """One writable device, one sensor-only device, and three devices that are not batteries."""
    device_one = {"device_id": "example_dev_one", "device_name": "Example Battery One", "manufacturer": "MakerCo", "model": "Model One"}
    device_two = {"device_id": "example_dev_two", "device_name": "Example Battery Two", "manufacturer": "MakerCo", "model": "Model Two"}

    def row(entity_id, role, confidence, **extra):
        base = {
            "entity_id": entity_id,
            "name": entity_id.split(".", 1)[1].replace("_", " "),
            "unit": "W",
            "device_class": "power",
            "platform": "example_integration",
            "suggested_role": role,
            "confidence": confidence,
        }
        base.update(extra)
        return base

    return [
        # a device that can be driven and read: charge/discharge limits, manual mode, soc, capacity
        row("number.example_one_charge_limit", "charge_limit", 0.8, min=0, max=1000, **device_one),
        row("number.example_one_discharge_limit", "discharge_limit", 0.78, min=0, max=800, **device_one),
        row("select.example_one_manual_mode", "manual_mode", 0.6, **device_one),
        row("sensor.example_one_soc", "soc", 0.92, unit="%", device_class="battery", **device_one),
        row("sensor.example_one_capacity", "capacity", 0.7, unit="kWh", state="10.5", **device_one),
        # the same roles, but only read-only sensors: it looks like a battery and cannot be driven
        row("sensor.example_two_power", "power_setpoint", 0.95, **device_two),
        row("sensor.example_two_charge_limit", "charge_limit", 0.9, **device_two),
        row("sensor.example_two_soc", "soc", 0.9, unit="%", device_class="battery", **device_two),
        row("sensor.example_two_capacity", "capacity", 0.88, unit="kWh", state="6", **device_two),
    ]


def _non_battery_rows():
    device = {"device_id": "example_dev_thermostat", "device_name": "Example Thermostat", "manufacturer": "MakerCo", "model": "Model Thermostat"}
    bridge = {"device_id": "example_dev_bridge", "device_name": "Example Bridge"}
    camera = {"device_id": "example_dev_camera", "device_name": "Example Camera"}

    def row(entity_id, role, confidence, **extra):
        base = {"entity_id": entity_id, "name": entity_id, "platform": "example_integration", "suggested_role": role, "confidence": confidence}
        base.update(extra)
        return base

    return [
        row("sensor.example_thermostat_level", "soc", 0.85, unit="%", **device),          # a battery level, that is all
        row("sensor.example_bridge_connection", "connection", 0.9, **bridge),             # a bridge's connectivity sensor
        row("button.example_camera_start", "manual_mode", 0.75, **camera),                # a camera's start button
    ]


def test_battery_picker_groups_rows_per_device_and_labels_them():
    """Rows group per device_id; a row without one groups by entity id and is never dropped."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const withDevice = A.batteryGroupsFromCandidates(%s);
    const noDevice = A.batteryGroupsFromCandidates([
      { entity_id: "number.example_ha_one_setpoint", suggested_role: "power_setpoint", confidence: 0.9, platform: "zendure_ha" },
      { entity_id: "number.example_ha_two_setpoint", suggested_role: "power_setpoint", confidence: 0.8, platform: "zendure_ha" },
      { entity_id: "number.example_ha_dual", suggested_role: "charge_limit", confidence: 0.6, platform: "zendure_ha" },
      { entity_id: "number.example_ha_dual", suggested_role: "power_setpoint", confidence: 0.9, platform: "zendure_ha" }
    ]);
    const labels = A.batteryGroupsFromCandidates([
      { entity_id: "number.example_l_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_a", device_name: "Device Name Wins", manufacturer: "MakerCo", model: "Model A" },
      { entity_id: "sensor.example_l_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_a", device_name: "Device Name Wins", manufacturer: "MakerCo", model: "Model A" },
      { entity_id: "number.example_m_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_b", manufacturer: "MakerCo", model: "Model B" },
      { entity_id: "sensor.example_m_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_b", manufacturer: "MakerCo", model: "Model B" },
      { entity_id: "number.example_n_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_c", name: "Candidate Name Wins" },
      { entity_id: "sensor.example_n_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_c" },
      { entity_id: "number.example_o_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_d" },
      { entity_id: "sensor.example_o_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_d", name: "" }
    ]);
    console.log(JSON.stringify({
      withDevice: withDevice.map(function (g) { return { id: g.deviceId, rows: g.rows.length, label: g.label, manufacturer: g.manufacturer, model: g.model, platform: g.platform }; }),
      noDevice: noDevice.map(function (g) { return { id: g.deviceId, rows: g.rows.length, roles: g.mappable }; }),
      labels: labels.map(function (g) { return g.label; })
    }));
    """ % json.dumps(_picker_rows())
    data = json.loads(run_node_script(script))
    assert [group["id"] for group in data["withDevice"]] == ["example_dev_one", "example_dev_two"]
    assert [group["rows"] for group in data["withDevice"]] == [5, 4], "every row of a device belongs to its group"
    assert data["withDevice"][0]["label"] == "Example Battery One"
    assert data["withDevice"][0]["manufacturer"] == "MakerCo" and data["withDevice"][0]["model"] == "Model One"
    # no device_id: grouped by entity id (one entity can appear once per role), nothing dropped
    assert [group["id"] for group in data["noDevice"]] == [None, None, None]
    assert sorted(group["rows"] for group in data["noDevice"]) == [1, 1, 2]
    dual = [group for group in data["noDevice"] if group["rows"] == 2][0]
    assert sorted(dual["roles"]) == ["charge_limit", "power_setpoint"]
    # label: device_name, else manufacturer + model, else the best candidate's name, else entity id
    assert sorted(data["labels"]) == sorted(["Device Name Wins", "MakerCo Model B", "Candidate Name Wins", "number.example_o_setpoint"])


def test_battery_picker_skips_devices_that_are_not_batteries():
    """A lone battery-ish role is not a battery: thermostat, bridge and camera must not be listed."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const pair = A.batteryGroupsFromCandidates(%s);
    const platform = A.batteryGroupsFromCandidates(%s);
    console.log(JSON.stringify({
      pair: pair.map(function (g) { return { label: g.label, viaPlatform: g.viaPlatform, ready: g.controlReady, mappable: g.mappable, missing: g.missing }; }),
      platform: platform.map(function (g) { return { label: g.label, viaPlatform: g.viaPlatform, ready: g.controlReady, missing: g.missing }; })
    }));
    """ % (json.dumps(_non_battery_rows()), json.dumps([
        {"entity_id": "number.example_zendure_limit", "suggested_role": "charge_limit", "confidence": 0.2, "platform": "zendure_ha"}
    ]))
    data = json.loads(run_node_script(script))
    assert data["pair"] == [], "thermostat / bridge / camera rows must never become picker rows"
    assert len(data["platform"]) == 1
    assert data["platform"][0]["viaPlatform"] is True, "a known battery platform qualifies on its own"
    assert data["platform"][0]["ready"] is False
    # nothing cleared the confidence floor, so every part of a battery is reported missing
    assert data["platform"][0]["missing"] == ["a writable control entity", "soc", "capacity"]


def test_battery_picker_qualifies_a_generic_control_and_state_pair():
    """A non-zendure device with a control role and a state role is listed, labelled as role-matched."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const groups = A.batteryGroupsFromCandidates([
      { entity_id: "number.example_generic_setpoint", suggested_role: "power_setpoint", confidence: 0.9, platform: "mqtt", device_id: "example_dev_generic", device_name: "Example Generic" },
      { entity_id: "sensor.example_generic_soc", suggested_role: "soc", confidence: 0.8, unit: "%", platform: "mqtt", device_id: "example_dev_generic", device_name: "Example Generic" }
    ]);
    console.log(JSON.stringify(groups.map(function (g) { return { label: g.label, viaPlatform: g.viaPlatform, ready: g.controlReady, mappable: g.mappable }; })));
    """
    groups = json.loads(run_node_script(script))
    assert len(groups) == 1
    assert groups[0]["label"] == "Example Generic"
    assert groups[0]["viaPlatform"] is False, "it qualified by role, not by platform"
    assert groups[0]["ready"] is True, "a writable setpoint plus a soc reading is control-ready"
    assert groups[0]["mappable"] == ["power_setpoint", "soc"]


def test_battery_picker_ranks_writable_control_groups_first():
    """Read-only control matches rank below writable ones and never take a control role."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const rows = %s;
    const groups = A.batteryGroupsFromCandidates(rows);
    const every = A.batteryGroupsFromCandidates(rows).map(function (g) {
      return {
        label: g.label,
        mappable: g.mappable,
        skipped: g.skipped,
        writableControl: g.writableControl,
        readOnlyControl: g.readOnlyControl,
        controlReady: g.controlReady,
        missing: g.missing,
        mapping: A.batterySelection(g, []).mapping
      };
    });
    // measurement preference: a sensor wins over a higher-confidence number, a number is kept
    // only when no sensor matches the role
    const measured = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_pref_soc_number", suggested_role: "soc", confidence: 0.99, unit: "%%", device_id: "example_dev_pref", device_name: "Example Preference" },
      { entity_id: "sensor.example_pref_soc_sensor", suggested_role: "soc", confidence: 0.6, unit: "%%", device_id: "example_dev_pref", device_name: "Example Preference" },
      { entity_id: "number.example_pref_output", suggested_role: "output_power", confidence: 0.7, device_id: "example_dev_pref", device_name: "Example Preference" },
      { entity_id: "number.example_pref_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_pref", device_name: "Example Preference" }
    ])[0], []).mapping;
    console.log(JSON.stringify({ every: every, measured: measured }));
    """ % json.dumps(_picker_rows())
    data = json.loads(run_node_script(script))
    assert [group["label"] for group in data["every"]] == ["Example Battery One", "Example Battery Two"]
    writable, read_only = data["every"]
    assert writable["writableControl"] is True and writable["controlReady"] is True
    assert read_only["writableControl"] is False and read_only["controlReady"] is False
    assert read_only["readOnlyControl"] is True, "a sensor-only control match is reported as read-only"
    assert sorted(read_only["skipped"]) == ["charge_limit", "power_setpoint"]
    assert read_only["missing"] == ["a writable control entity"]
    # the sensor-only device keeps its measurement roles but gets no control entity at all
    assert read_only["mapping"] == {"capacity_entity": "sensor.example_two_capacity", "soc_entity": "sensor.example_two_soc"}
    assert not any(key.endswith("_entity") and "setpoint" in key or "limit" in key for key in read_only["mapping"]), \
        "no sensor may be mapped to a control role"
    assert data["measured"] == {
        "power_setpoint_entity": "number.example_pref_setpoint",
        "soc_entity": "sensor.example_pref_soc_sensor",
        "output_power_entity": "number.example_pref_output",
    }, "a sensor is preferred for a measurement role, a number only when no sensor matches"


def test_battery_slug_is_unique_against_configured_ids():
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify([
      A.batterySlugFor("Example Battery One", []),
      A.batterySlugFor("Example Battery One", ["example_battery_one"]),
      A.batterySlugFor("Example Battery One", ["example_battery_one", "example_battery_one_2"]),
      A.batterySlugFor("Example Battery One", ["example_battery_one", "example_battery_one_2", "example_battery_one_3"]),
      A.batterySlugFor("Example Battery One", ["EXAMPLE_BATTERY_ONE"]),
      A.batterySlugFor("   ", ["example_thing"])
    ]));
    """
    slugs = json.loads(run_node_script(script))
    assert slugs == [
        "example_battery_one",
        "example_battery_one_2",
        "example_battery_one_3",
        "example_battery_one_4",
        "example_battery_one_2",
        "battery",
    ], slugs


def test_battery_selection_fills_roles_numbers_and_driver():
    """One Select fills the mapping, the driver and only the numbers discovery actually states."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const limits = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_l_charge_limit", suggested_role: "charge_limit", confidence: 0.8, min: 0, max: 1000, device_id: "example_dev_l", device_name: "Example Limits" },
      { entity_id: "number.example_l_discharge_limit", suggested_role: "discharge_limit", confidence: 0.8, min: 0, max: 800, device_id: "example_dev_l", device_name: "Example Limits" },
      { entity_id: "sensor.example_l_soc_weak", suggested_role: "soc", confidence: 0.49, unit: "%%", device_id: "example_dev_l", device_name: "Example Limits" },
      { entity_id: "sensor.example_l_soc_b", suggested_role: "soc", confidence: 0.6, unit: "%%", device_id: "example_dev_l", device_name: "Example Limits" },
      { entity_id: "sensor.example_l_soc_a", suggested_role: "soc", confidence: 0.6, unit: "%%", device_id: "example_dev_l", device_name: "Example Limits" },
      { entity_id: "sensor.example_l_capacity", suggested_role: "capacity", confidence: 0.7, unit: " kWh ", state: "12.0", device_id: "example_dev_l", device_name: "Example Limits" }
    ])[0], ["example_limits"]);
    const setpoint = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_s_setpoint", suggested_role: "power_setpoint", confidence: 0.9, min: -2400, max: 2400, device_id: "example_dev_s", device_name: "Example Setpoint" },
      { entity_id: "sensor.example_s_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_s", device_name: "Example Setpoint" }
    ])[0], []);
    const socBounds = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_b_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_b", device_name: "Example Bounds" },
      { entity_id: "sensor.example_b_soc", suggested_role: "soc", confidence: 0.85, unit: "%%", device_id: "example_dev_b", device_name: "Example Bounds" },
      { entity_id: "number.example_b_min_soc", suggested_role: "min_soc", confidence: 0.8, unit: "%%", min: 0, max: 100, device_id: "example_dev_b", device_name: "Example Bounds" },
      { entity_id: "number.example_b_soc_target", suggested_role: "soc_target", confidence: 0.8, unit: "%%", min: 0, max: 100, device_id: "example_dev_b", device_name: "Example Bounds" }
    ])[0], []);
    const negativeBound = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_n_charge_limit", suggested_role: "charge_limit", confidence: 0.8, min: -900, max: 900, device_id: "example_dev_n", device_name: "Example Negative" },
      { entity_id: "sensor.example_n_soc", suggested_role: "soc", confidence: 0.8, unit: "%%", device_id: "example_dev_n", device_name: "Example Negative" }
    ])[0], []);
    const noUnit = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_u_setpoint", suggested_role: "power_setpoint", confidence: 0.9, device_id: "example_dev_u", device_name: "Example Unitless" },
      { entity_id: "sensor.example_u_capacity", suggested_role: "capacity", confidence: 0.9, unit: "Wh", state: "5000", device_id: "example_dev_u", device_name: "Example Unitless" }
    ])[0], []);
    console.log(JSON.stringify({ limits: limits, setpoint: setpoint, socBounds: socBounds, negativeBound: negativeBound, noUnit: noUnit }));
    """
    data = json.loads(run_node_script(script))
    limits = data["limits"]
    # capacity with a unit that needs trimming, power limits read per role, in its own direction
    assert limits["id"] == "example_limits_2" and limits["name"] == "Example Limits"
    assert limits["driver"] == "zendure", "charge_limit maps to a zendure-only key"
    assert limits["mapping"] == {
        "capacity_entity": "sensor.example_l_capacity",
        "charge_limit_entity": "number.example_l_charge_limit",
        "discharge_limit_entity": "number.example_l_discharge_limit",
        "soc_entity": "sensor.example_l_soc_a",   # tie on confidence -> smallest entity_id
    }
    assert limits["numbers"] == {"capacity_kwh": 12.0, "max_charge_w": 1000, "max_discharge_w": 800}, limits["numbers"]
    # the 0.49 candidate never wins a role
    assert "sensor.example_l_soc_weak" not in limits["mapping"].values()
    assert data["setpoint"]["driver"] == "generic" and data["setpoint"]["numbers"] == {"max_charge_w": 2400, "max_discharge_w": 2400}
    assert data["setpoint"]["mapping"] == {"power_setpoint_entity": "number.example_s_setpoint", "soc_entity": "sensor.example_s_soc"}
    # min_soc / soc_target state an allowed range: no SoC window and no power limit is invented
    assert data["socBounds"]["numbers"] == {}
    assert data["socBounds"]["mapping"] == {
        "power_setpoint_entity": "number.example_b_setpoint",
        "min_soc_entity": "number.example_b_min_soc",
        "soc_entity": "sensor.example_b_soc",
        "soc_target_entity": "number.example_b_soc_target",
    }
    # a negative range start is the absolute charge bound
    assert data["negativeBound"]["numbers"] == {"max_charge_w": 900}
    # a capacity in Wh is not a kWh capacity: the role is mapped, the number is left alone
    assert data["noUnit"]["numbers"] == {}
    assert data["noUnit"]["mapping"] == {
        "power_setpoint_entity": "number.example_u_setpoint",
        "capacity_entity": "sensor.example_u_capacity",
    }


def test_battery_search_matches_device_fields_then_falls_back_to_the_server():
    """Search covers device label, manufacturer, model and platform; empty results go to the server."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const rows = %s;
    const search = function (query) {
      const plan = A.batterySearchGroups(rows, query);
      return { labels: plan.groups.map(function (g) { return g.label; }), fromServer: plan.fromServer };
    };
    console.log(JSON.stringify({
      empty: search(""),
      device: search("Example Battery One"),
      manufacturer: search("MakerCo"),
      model: search("Model Two"),
      platform: search("example_integration"),
      entity: search("example_two_capacity"),
      missing: search("nothing matches this"),
      fromNoRows: A.batterySearchGroups([], "anything").fromServer
    }));
    """ % json.dumps(_picker_rows())
    data = json.loads(run_node_script(script))
    assert data["empty"]["labels"] == ["Example Battery One", "Example Battery Two"]
    assert data["empty"]["fromServer"] is False
    assert data["device"]["labels"] == ["Example Battery One"] and data["device"]["fromServer"] is False
    assert data["manufacturer"]["labels"] == ["Example Battery One", "Example Battery Two"]
    assert data["model"]["labels"] == ["Example Battery Two"]
    assert data["platform"]["labels"] == ["Example Battery One", "Example Battery Two"]
    assert data["entity"]["labels"] == ["Example Battery Two"], "search also matches the entity id"
    assert data["missing"]["labels"] == [] and data["missing"]["fromServer"] is True
    assert data["fromNoRows"] is True, "no local hit means the server-side search is the fallback"


def test_battery_dialog_wires_the_picker_into_the_form():
    """The picker is the first thing in the dialog, and the manual path stays where it was."""
    app = read(WEB / "app.js")
    # a new block at the top of the dialog, with its own search input and button
    picker_at = app.index('el("h3", { text: "Find a battery" })')
    identity_at = app.index("\n      identity,\n")   # its position in the dialog body itself
    assert picker_at < identity_at, "the discovery picker must sit above the identity fields"
    assert 'el("input", { type: "search", placeholder: "device name, model, platform or entity id"' in app
    assert '"Find batteries"' in app and "runBatterySearch(pickerSearch.value)" in app
    # it runs once on open with an empty query, and the server is only asked when nothing matches
    assert "loadBatteryCandidates();" in app, "the picker reads discovery once when the dialog opens"
    assert app.count('get("/discover", { kind: "battery", search:') == 3, "picker load, picker fallback, entity-candidate table"
    assert "batterySearchGroups(rows, query)" in app and "plan.fromServer" in app
    # the id field is only editable while adding; the Assign table stays for fine-tuning
    assert "textRow(\"id\", \"Battery id (slug)\", fields.id, editing)" in app
    assert '"Entity candidates"' in app and '"(ignore)"' in app
    # selecting fills the form and says what was mapped, never silently
    assert "batterySelection(group, configuredIds)" in app
    assert "renderMappingRows();" in app
    assert "This is only a suggestion — confirm every entity belongs to this battery before saving." in app
    assert "control entities are read-only" in app
    # the battery ids already configured are what makes a picked id unique
    assert "openBatteryDialog(record, reload, takenIds)" in app
    assert "openBatteryDialog(null, data.reload, ids)" in app


def test_battery_picker_keeps_usable_energy_out_of_the_capacity_role():
    """SPEC 10 / appendix A: ``total_kwh`` is the pack, ``available_kwh`` is what is left in it.

    The two arrive as different roles, so one Select must put them in different mapping keys and
    must prefill the capacity from the pack figure -- never from the usable reading, which on a
    nearly empty battery is a fraction of the pack (and may be negative).
    """
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const pick = A.batterySelection(A.batteryGroupsFromCandidates([
      { entity_id: "number.example_e_charge", suggested_role: "charge_limit", confidence: 0.95, min: 0, max: 1000, unit: "W", platform: "zendure_ha", device_id: "example_dev_e", device_name: "Example Energy" },
      { entity_id: "sensor.example_e_total_kwh", suggested_role: "capacity", confidence: 0.95, mapping_key: "capacity_entity", unit: "kWh", state: "1.92", platform: "zendure_ha", device_id: "example_dev_e", device_name: "Example Energy" },
      { entity_id: "sensor.example_e_available_kwh", suggested_role: "available_energy", confidence: 0.95, mapping_key: "available_kwh_entity", unit: "kWh", state: "-0.04", platform: "zendure_ha", device_id: "example_dev_e", device_name: "Example Energy" },
      { entity_id: "sensor.example_e_soc", suggested_role: "soc", confidence: 0.9, platform: "zendure_ha", device_id: "example_dev_e", device_name: "Example Energy" }
    ])[0], []);
    console.log(JSON.stringify(pick));
    """
    data = json.loads(run_node_script(script))
    assert data["driver"] == "zendure"
    assert data["mapping"]["capacity_entity"] == "sensor.example_e_total_kwh"
    assert data["mapping"]["available_kwh_entity"] == "sensor.example_e_available_kwh"
    assert data["numbers"]["capacity_kwh"] == 1.92


# --- forecast empty state + price provider/contract visibility (SPEC §4.1 / §10) -----------------


def test_forecast_empty_state_prefers_the_api_hint_verbatim():
    """An empty panel shows the server `hint` verbatim (never truncated) with the `reason` slug
    kept beside it, and only falls back to the generic sentence when the server sent no hint."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    // A failed day-ahead lookup names the configured bidding zone in its hint (SPEC §4.1).
    const zoneHint = A.forecastEmptyState({
      reason: "provider_failed",
      hint: "energy-charts published no day-ahead prices for bidding zone NL; pick a different zone or a supplier source."
    });
    const noHint = A.forecastEmptyState({ reason: "empty_horizon", hint: null });
    const noReason = A.forecastEmptyState({ hint: "Set price.provider to a source that has data." });
    const bare = A.forecastEmptyState({});
    console.log(JSON.stringify({ zoneHint: zoneHint, noHint: noHint, noReason: noReason, bare: bare }));
    """
    data = json.loads(run_node_script(script))
    # the hint is the prominent message and is passed through verbatim (equal, not truncated)
    assert data["zoneHint"]["text"] == "energy-charts published no day-ahead prices for bidding zone NL; pick a different zone or a supplier source."
    assert data["zoneHint"]["hint"] == data["zoneHint"]["text"]
    assert data["zoneHint"]["reason"] == "provider_failed", "the reason slug is kept for the small badge"
    # no hint -> the legacy fallback sentence, reason still surfaced
    assert data["noHint"]["text"] == "No slots returned; the source failed or the horizon is empty."
    assert data["noHint"]["reason"] == "empty_horizon"
    # a hint with no reason, and a totally bare payload, still produce safe text
    assert data["noReason"]["reason"] is None and data["noReason"]["text"].startswith("Set price.provider")
    assert data["bare"]["reason"] is None and data["bare"]["hint"] is None
    assert data["bare"]["text"] == "No slots returned; the source failed or the horizon is empty."
    app = read(WEB / "app.js")
    assert "forecastEmptyState" in app and "banner banner-warn" in app, "the empty panel renders the hint in a prominent banner"


def test_price_field_visibility_per_provider_and_contract():
    """priceFieldVisibility (SPEC §4.1) reveals the fields that apply to each provider/contract."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({
      entity: A.priceFieldVisibility("entity", "dynamic_hourly"),
      zonneplan: A.priceFieldVisibility("zonneplan", "dynamic_quarter_hourly"),
      dayAhead: A.priceFieldVisibility("day_ahead", "dynamic_hourly"),
      fixedSingle: A.priceFieldVisibility("fixed", "fixed_single"),
      fixedDayNight: A.priceFieldVisibility("fixed", "fixed_day_night")
    }));
    """
    data = json.loads(run_node_script(script))

    def keys(name):
        return set(data[name]["fields"])

    note = {name: data[name]["note"] for name in ("entity", "zonneplan", "dayAhead", "fixedSingle", "fixedDayNight")}
    always = {
        "price.vat_pct", "price.energy_tax_eur_per_kwh", "price.import_fee_eur_per_kwh",
        "price.export_fee_eur_per_kwh", "price.unit", "price.scale_divisor",
        "price.feed_in_eur_per_kwh", "price.feed_in_entity", "price.feed_in_tax_inclusive",
        # the selectors themselves must always be shown so the pair can be changed
        "price.provider", "price.contract",
    }
    for name in ("entity", "zonneplan", "dayAhead", "fixedSingle", "fixedDayNight"):
        assert always <= keys(name), f"{name} dropped an always-visible field: {sorted(always - keys(name))}"

    # entity: the advanced source + entity + static schedule, no bidding zone or fixed rates
    assert {"price.source", "price.entity", "price.static_schedule"} <= keys("entity")
    assert "price.bzn" not in keys("entity") and not any(k.startswith("price.fixed_") for k in keys("entity"))

    # zonneplan: NO entity field (the tariff sensor is found automatically); no bzn, no fixed rates
    assert "price.entity" not in keys("zonneplan") and "price.bzn" not in keys("zonneplan")
    assert not any(k.startswith("price.fixed_") for k in keys("zonneplan"))
    assert note["zonneplan"] and "automatically" in note["zonneplan"].lower()

    # day_ahead: the bidding zone, and a note that says wholesale + zone coverage matters (SPEC §4.1
    # addition): a valid zone may have no prices, so the user knows to pick a supplier instead.
    assert "price.bzn" in keys("dayAhead") and "price.entity" not in keys("dayAhead")
    assert not any(k.startswith("price.fixed_") for k in keys("dayAhead"))
    da = note["dayAhead"].lower()
    assert "wholesale" in da and "consumer prices" in da
    assert "zone" in da and "no prices" in da and "supplier" in da

    # fixed + fixed_single: one rate (+ its entity); not the day/night fields
    assert {"price.fixed_price_eur_per_kwh", "price.fixed_price_entity"} <= keys("fixedSingle")
    assert "price.fixed_day_price_eur_per_kwh" not in keys("fixedSingle")
    assert "price.fixed_night_price_eur_per_kwh" not in keys("fixedSingle")
    assert "price.fixed_day_start_hour" not in keys("fixedSingle")

    # fixed + fixed_day_night: day + night rates/entities and the two window hours; not the single rate
    fixed_dn = {
        "price.fixed_day_price_eur_per_kwh", "price.fixed_night_price_eur_per_kwh",
        "price.fixed_day_price_entity", "price.fixed_night_price_entity",
        "price.fixed_day_start_hour", "price.fixed_night_start_hour",
    }
    assert fixed_dn <= keys("fixedDayNight")
    assert "price.fixed_price_eur_per_kwh" not in keys("fixedDayNight")

    # contract is what switches the fixed fields: the same provider, a different contract
    assert keys("fixedSingle") != keys("fixedDayNight")
