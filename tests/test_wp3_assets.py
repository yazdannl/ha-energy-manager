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
