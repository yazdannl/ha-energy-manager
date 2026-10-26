"""WP3 (UI) static asset checks.

These run without a browser: they verify that the SPA is self-contained, that every asset
index.html references exists, that the JavaScript parses, and that the accessibility/safety rules
from SPEC §13 are present in the source. Node-based checks are skipped when node is absent.
"""

from __future__ import annotations

import hashlib
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

VIEWS = ["dashboard", "plan", "forecasts", "batteries", "cards", "savings", "settings", "log"]

REQUIRED_FILES = [
    "index.html",
    "app.js",
    "charts.js",
    "mock.js",
    "style.css",
    "chart-theme.css",
    "sem-charts-card.js",
    "sem-charts-card.css",
    "README.md",
    "vendor/uPlot.min.js",
    "vendor/uPlot.min.css",
    "vendor/bootstrap.min.css",
    "vendor/InterVariable.woff2",
    "vendor/README.md",
    "vendor/LICENSE.uPlot.txt",
    "vendor/LICENSE.bootstrap.txt",
    "vendor/LICENSE.inter.txt",
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


def test_vendored_bootstrap_matches_its_recorded_identity():
    """The vendored framework must be the official build, pinned by the checksum in vendor/README.md.

    A truncated or hand-edited copy of a 230 KB stylesheet is otherwise invisible until something
    looks wrong in the browser, so the README is the record of what was vendored and this test keeps
    the file and the record in step. Refreshing the file means updating the README (and nothing else).
    """
    css = read(VENDOR / "bootstrap.min.css")
    assert len(css) > 100_000, f"bootstrap.min.css looks truncated: {len(css)} bytes"
    assert "Bootstrap  v5.3.3" in css, "not the expected Bootstrap build"
    assert "Licensed under MIT" in css

    record = read(VENDOR / "README.md")
    digest = hashlib.sha256(css.encode("utf-8")).hexdigest()
    assert digest in record, f"vendor/README.md does not record the file's sha256 ({digest})"
    assert str(len(css.encode("utf-8"))) in record, "vendor/README.md does not record the byte size"
    assert "5.3.3" in record and "bootstrap" in record.lower()
    assert "://" in record  # the source URL must be recorded for a refresh

    license_text = read(VENDOR / "LICENSE.bootstrap.txt")
    assert "MIT License" in license_text and "Bootstrap Authors" in license_text


def test_index_loads_bootstrap_before_the_design_layer():
    """Bootstrap first, then the design layer that overrides its tokens (SPEC 13.1)."""
    html = read(WEB / "index.html")
    refs = re.findall(r'(?:href|src)="([^"]+)"', html)
    assert "vendor/bootstrap.min.css" in refs, "the vendored framework is not loaded"
    assert refs.index("vendor/bootstrap.min.css") < refs.index("style.css"), (
        "style.css must come after bootstrap.min.css or its overrides lose"
    )
    assert 'data-bs-theme' in html, "the theme attribute must be declared on <html>"


def test_design_layer_keeps_the_chart_token_interface():
    """charts.js reads the chart colours through getComputedStyle, so the names are a frozen interface."""
    css = read(WEB / "style.css")
    for token in (
        "--chart-price", "--chart-sell", "--chart-pv", "--chart-load", "--chart-soc",
        "--chart-battery-charge", "--chart-battery-discharge", "--chart-stale",
        "--chart-break-even", "--chart-grid", "--chart-axis", "--chart-baseline",
        "--chart-cost", "--chart-savings", "--chart-pv-accuracy", "--chart-load-accuracy",
        "--chart-price-accuracy",
    ):
        assert token in css, f"the design layer dropped {token}, which charts.js reads"
    charts = read(WEB / "charts.js")
    assert "getComputedStyle" in charts and "--chart-" in charts


def test_no_style_or_script_fetches_anything_at_runtime():
    """No CDN, no webfont, no remote image: the add-on is served from a LAN and may be offline."""
    for name in ("index.html", "style.css", "app.js", "charts.js", "mock.js"):
        text = read(WEB / name)
        assert "@import" not in text, f"{name} uses @import"
        assert "url(http" not in text and "url(//" not in text, f"{name} loads a remote asset"


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
    # The route list is injected from VIEWS: a hard-coded copy here would pass while a route was
    # missing from the router or the other way round, which is the thing this test exists to catch.
    routes = json.dumps(["/" + view for view in VIEWS])
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/api/hassio_ingress/example/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({
      routes: Object.keys(A.ROUTES),
      types: Object.keys(A.ROUTES).map(function (r) { return typeof A.ROUTES[r]; }),
      resolved: __ROUTES__.map(function (r) { return A.normalizeRoute(r); }),
      fallback: A.normalizeRoute("/nope"),
      empty: A.normalizeRoute(""),
      ingressPath: A.apiPath("/status", {}),
      plainPath: A.apiPath("/plan", { hours: 24 }),
      split: A.splitHash("#/plan?hours=12"),
      noHash: A.splitHash(""),
      mocked: A.isMock
    }));
    """.replace("__ROUTES__", routes)
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
    # SPEC 4 (amended): with no explicit tariff the sell price is derived (buy minus feed-in cost),
    # so the old "no export revenue" warning is gone; enabling export states the basis instead.
    assert "Export is enabled" in app
    assert "the sell price is the buy price minus the feed-in cost" in app
    assert "price.feed_in_eur_per_kwh / price.feed_in_entity" in app


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
    assert "no sampled interval in these periods could be re-priced against a baseline" in app
    assert "Savings cover " in app and "of the sampled time" in app
    assert "Sampled-time coverage" in app
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
      wearFromConfig: A.batteryWear({ wear_eur_per_kwh: 0.05 }, 0),
      wearOverride: A.batteryWear({ wear_eur_per_kwh: 0.05 }, 0.02),
      derivedWear: A.derivedWearEur({ price_eur: 3000, cycle_life: 6000, capacity_kwh: 10, min_soc_pct: 10, max_soc_pct: 90 }),
      derivedWearMissing: A.derivedWearEur({ price_eur: 3000, cycle_life: 0, capacity_kwh: 10, min_soc_pct: 10, max_soc_pct: 90 }),
      unknownBuy: A.breakEvenSellEur(null, cfg75, 0),
      etaOnly: A.batteryEfficiency({ eta_charge: 0.9, eta_discharge: 0.9 }).round_trip_pct,
      pctOnlyEta: A.batteryEfficiency(cfg75).eta_charge
    }));
    """
    data = json.loads(run_node_script(script))
    assert round(data["be75"] * 100, 2) == 26.67, "20 ct at 75 % must break even at 26.67 ct"
    assert round(data["be80"] * 100, 2) == 25.0, "25 ct is the 80 % case"
    assert round(data["be90"] * 100, 2) == 22.22
    assert round(data["be75Wear"] * 100, 2) == 28.67, "wear adds the per-delivered-kWh constant directly"
    assert round(data["asymmetric"] * 100, 2) == 22.88, "advanced override uses eta_charge x eta_discharge"
    assert round(data["stored75"] * 100, 2) == 23.09, "stored kWh all-in = buy / eta_charge at zero wear"
    assert data["wearFromConfig"] == pytest.approx(0.05), "the derived per-battery wear is used"
    assert data["wearOverride"] == pytest.approx(0.02), "a positive override wins over derived wear"
    assert data["derivedWear"] == pytest.approx(3000.0 / (6000 * 8.0 * 0.95)), "price / (cycles x usable kWh x eta_discharge)"
    assert data["derivedWearMissing"] is None, "unknown cycle life means no derived wear"
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
    assert round(data["beDerived"] * 100, 2) == 26.69, "0.81 round trip: 0.2/0.81 + 0.02"
    assert round(data["beNinety"] * 100, 2) == 24.22, "0.9 round trip: 0.2/0.9 + 0.02"
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


def test_vendored_inter_font_matches_its_recorded_identity():
    """The UI font is served locally, so the README is the record of what was vendored.

    SPEC 13.1 (0.28.0) allows a locally cached webfont; this keeps the binary, the licence and the
    recorded sha256 in step, exactly like Bootstrap. The file is binary, so the PII tripwire skips
    it - the identity check is what proves it is the official build.
    """
    font = (VENDOR / "InterVariable.woff2").read_bytes()
    assert font[:4] == b"wOF2", "not a WOFF2 font"
    assert len(font) > 100_000, f"InterVariable.woff2 looks truncated: {len(font)} bytes"

    record = read(VENDOR / "README.md")
    digest = hashlib.sha256(font).hexdigest()
    assert digest in record, f"vendor/README.md does not record the font's sha256 ({digest})"
    assert "InterVariable" in record and "rsms.me/inter/font-files" in record
    assert "OFL" in record or "Open Font License" in record

    licence = read(VENDOR / "LICENSE.inter.txt")
    assert "SIL Open Font License" in licence and "The Inter Project Authors" in licence

    css = read(WEB / "style.css")
    assert '@font-face' in css and 'Inter var' in css and 'vendor/InterVariable.woff2' in css
    assert 'font-display: swap' in css, "a local font must not block text rendering"


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
    """An empty panel shows the server `hint` verbatim and never guesses a failure without a reason."""
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
    const failedReason = A.forecastEmptyState({ reason: "provider_failed" });
    const noReason = A.forecastEmptyState({ hint: "Set price.provider to a source that has data." });
    const bare = A.forecastEmptyState({});
    console.log(JSON.stringify({ zoneHint: zoneHint, noHint: noHint, failedReason: failedReason, noReason: noReason, bare: bare }));
    """
    data = json.loads(run_node_script(script))
    # the hint is the prominent message and is passed through verbatim (equal, not truncated)
    assert data["zoneHint"]["text"] == "energy-charts published no day-ahead prices for bidding zone NL; pick a different zone or a supplier source."
    assert data["zoneHint"]["hint"] == data["zoneHint"]["text"]
    assert data["zoneHint"]["reason"] == "provider_failed", "the reason slug is kept for the small badge"
    # Without a hint, retain a supplied reason; without either, do not guess the cause.
    assert data["noHint"]["text"] == "No forecast slots returned; reason: empty horizon."
    assert data["noHint"]["reason"] == "empty_horizon"
    assert data["failedReason"]["text"] == "No forecast slots returned; reason: provider failed."
    assert data["failedReason"]["reason"] == "provider_failed"
    # A hint with no reason, and a totally bare payload, still produce safe text.
    assert data["noReason"]["reason"] is None and data["noReason"]["text"].startswith("Set price.provider")
    assert data["bare"]["reason"] is None and data["bare"]["hint"] is None
    assert data["bare"]["text"] == "No forecast slots returned; no reason was reported."
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


def test_solar_field_visibility_per_source():
    """solarFieldVisibility (SPEC §7.1.1) shows only the fields the chosen solar source uses."""
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    console.log(JSON.stringify({
      api: A.solarFieldVisibility("forecast_solar"),
      capacity: A.solarFieldVisibility("capacity"),
      unknown: A.solarFieldVisibility("")
    }));
    """
    data = json.loads(run_node_script(script))

    def keys(name):
        return set(data[name]["fields"])

    always = {"solar.source", "solar.enabled", "solar.bias_correction", "solar.measured_entity"}
    for name in ("api", "capacity", "unknown"):
        assert always <= keys(name), f"{name} dropped an always-visible field: {sorted(always - keys(name))}"

    # forecast_solar: per-plane geometry plus the optional personal key, and nothing capacity-shaped
    assert {"solar.planes", "solar.api_key"} <= keys("api")
    assert not any(k in keys("api") for k in ("solar.capacity_wp", "solar.tilt_deg", "solar.azimuth_deg",
                                              "solar.performance_ratio", "solar.weather_entity"))

    # capacity: the watt peak, the optional geometry and the optional weather entity; no planes
    assert {"solar.capacity_wp", "solar.tilt_deg", "solar.azimuth_deg", "solar.performance_ratio",
            "solar.weather_entity"} <= keys("capacity")
    assert "solar.planes" not in keys("capacity") and "solar.api_key" not in keys("capacity")
    # the note must say the estimate comes from the watt peak, so the field is understood
    assert "watt peak" in (data["capacity"]["note"] or "").lower()

    # an empty/unknown source must not make fields vanish: fall back to the API source's set
    assert keys("unknown") == keys("api")


def test_load_device_problems_mirror_the_config_rules():
    """The Load card's client-side list rules (SPEC §7.2), so a save is never sent to be rejected.

    The meter is the one field the add-on cannot infer and the one whose repetition would be
    subtracted twice — a double count that hides inside the temperature slope — so both are refused
    in the editor as well as by the server validator.
    """
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "", hash: "" };
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    const kinds = ["heat_pump", "ev_charger", "water_heater", "pool_pump"];
    console.log(JSON.stringify({
      ok: A.loadDeviceProblems([
        { entity: "sensor.example_heat_pump_power", kind: "heat_pump" },
        { entity: "sensor.example_ev_charger_power", kind: "ev_charger" }
      ], kinds),
      missing: A.loadDeviceProblems([{ entity: "", kind: "heat_pump" }], kinds),
      repeated: A.loadDeviceProblems([
        { entity: "sensor.example_heat_pump_power", kind: "heat_pump" },
        { entity: "sensor.example_heat_pump_power", kind: "ev_charger" }
      ], kinds),
      unknown: A.loadDeviceProblems([{ entity: "sensor.example_boiler_power", kind: "boiler" }], kinds),
      empty: A.loadDeviceProblems(null, kinds)
    }));
    """
    data = json.loads(run_node_script(script))
    assert data["ok"] == [], data["ok"]
    assert data["empty"] == []
    assert any("no meter" in problem for problem in data["missing"])
    assert any("repeats" in problem for problem in data["repeated"])
    assert any("unknown kind" in problem for problem in data["unknown"])


def test_settings_groups_and_visibility_helpers():
    """The settings IA is tested through two pure helpers (SPEC 13.4), so node needs no DOM.

    Every key must land in exactly one group, the foldable set must be the advanced rows outside the
    Safety group, a source-inapplicable field may hide only while it holds its default, and a search
    match must win over both rules - that is what keeps "nothing is invisible" true.
    """
    script = """
    const fs = require("fs");
    globalThis.location = { pathname: "/x/", search: "?mock=1", hash: "#/settings" };
    (0, eval)(fs.readFileSync("mock.js", "utf8"));
    (0, eval)(fs.readFileSync("app.js", "utf8"));
    const A = globalThis.SEM_APP;
    (async () => {
      const cfg = await globalThis.SEM_MOCK.handle("GET", "/api/v1/config");
      const rows = cfg.settings;
      const value = (key) => (rows.find((r) => r.key === key) || {}).value;
      const selection = {
        priceProvider: value("price.provider"),
        priceContract: value("price.contract"),
        solarSource: value("solar.source")
      };
      const groups = A.settingsGroups(rows);
      const seen = groups.flatMap((g) => g.rows.map((r) => r.key));
      const pick = (key) => rows.find((r) => r.key === key);
      const asDefault = (key) => Object.assign({}, pick(key), { value: pick(key).default });
      console.log(JSON.stringify({
        groupNames: groups.map((g) => g.name),
        seen: seen.length,
        unique: new Set(seen).size,
        total: rows.length,
        advancedInGroups: groups.flatMap((g) => g.advancedRows.map((r) => r.key)).length,
        expectedAdvanced: rows.filter((r) => r.advanced && r.group !== "Safety").length,
        safetyFolded: groups.filter((g) => g.name === "Safety").flatMap((g) => g.advancedRows).length,
        hiddenWhenDefault: A.settingsVisible(asDefault("solar.planes"), Object.assign({}, selection)),
        shownWhenSet: A.settingsVisible(pick("solar.planes"), Object.assign({}, selection)),
        queryWins: A.settingsVisible(asDefault("solar.planes"), Object.assign({ query: "solar.planes" }, selection)),
        safetyAlwaysVisible: A.settingsVisible(pick("control.tick_seconds"), Object.assign({}, selection)),
        unknownGroupStable: groups.length > 0 && groups.every((g) => typeof g.changedCount === "number")
      }));
    })();
    """
    data = json.loads(run_node_script(script))

    assert data["seen"] == data["total"], "every setting must appear in exactly one group"
    assert data["unique"] == data["total"], "a setting must not appear in two groups"
    assert len(data["groupNames"]) == len(set(data["groupNames"])), data["groupNames"]
    assert data["advancedInGroups"] == data["expectedAdvanced"], (
        "the foldable set must be the advanced rows outside Safety: "
        f"{data['advancedInGroups']} != {data['expectedAdvanced']}"
    )
    assert data["safetyFolded"] == 0, "Safety rows must never be folded"
    assert data["hiddenWhenDefault"] is False, "a source-inapplicable field may hide while at its default"
    # settingsVisible is the visibility table, not the "hidden but set" rule: a source-inapplicable
    # field stays hidden here (the renderer lists it separately, as a muted line, and
    # tools/ui_render_check.js asserts that every such key is still reachable).
    assert data["shownWhenSet"] is False, "the visibility table itself must stay independent of the value"
    assert data["queryWins"] is True, "a search match must reveal a hidden field"
    assert data["safetyAlwaysVisible"] is True, "safety settings are never hidden by a source table"
    assert data["unknownGroupStable"] is True


def test_setup_card_keys_are_the_primary_surface():
    """SPEC 13.2 (amended): the four setup cards own the primary keys, per provider/solar source.

    Pure helper (no DOM), so the table is asserted without a browser: the cards must carry the
    provider selector, the meter picker, the solar selector and the feed-in controls, and must not
    offer an entity picker where the provider is found automatically or the solar estimate comes
    from the meter.
    """
    script = """
        const fs = require("fs");
        globalThis.location = { pathname: "/x/", search: "?mock=1", hash: "#/settings" };
        (0, eval)(fs.readFileSync("mock.js", "utf8"));
        (0, eval)(fs.readFileSync("app.js", "utf8"));
        (() => {
          const A = globalThis.SEM_APP;
          const keys = (opts) => A.setupCardKeys(opts);
          console.log(JSON.stringify({
            entity: keys({ priceProvider: "entity", solarSource: "forecast_solar" }),
            zonneplan: keys({ priceProvider: "zonneplan", solarSource: "capacity" }),
            dayAhead: keys({ priceProvider: "day_ahead", solarSource: "derived" }),
            fixedSingle: keys({ priceProvider: "fixed", priceContract: "fixed_single", solarSource: "forecast_solar" }),
            fixedDayNight: keys({ priceProvider: "fixed", priceContract: "fixed_day_night", solarSource: "forecast_solar" })
          }));
        })();
    """
    data = json.loads(run_node_script(script))
    for name, keys in data.items():
        for required in ("price.provider", "price.export_fee_eur_per_kwh", "price.feed_in_energy_tax_refund", "grid.power_entity", "solar.enabled", "solar.source"):
            assert required in keys, f"{name} card set is missing {required}: {keys}"
    assert "price.entity" in data["entity"]
    assert "price.entity" not in data["zonneplan"], "the zonneplan tariff sensor is found automatically"
    assert "price.bzn" in data["dayAhead"]
    assert "price.bzn" not in data["entity"]
    assert "price.contract" in data["fixedSingle"] and "price.fixed_price_eur_per_kwh" in data["fixedSingle"]
    assert "price.fixed_day_price_eur_per_kwh" in data["fixedDayNight"]
    assert "price.fixed_night_price_eur_per_kwh" in data["fixedDayNight"]
    # The measured-PV picker is independent of the forecast source, including meter-derived models.
    assert "solar.measured_entity" in data["dayAhead"]
    assert "solar.measured_entity" in data["entity"] and "solar.measured_entity" in data["zonneplan"]


# --------------------------------------------------------------------------------------------
# Lovelace card (SPEC 12)
# --------------------------------------------------------------------------------------------

CHART_FEED_ENTITIES = [
    "sensor.sem_chart_plan",
    "sensor.sem_chart_price",
    "sensor.sem_chart_solar",
    "sensor.sem_chart_load",
    "sensor.sem_chart_savings",
    "sensor.sem_chart_accuracy",
]


def test_the_card_only_reads_feeds_the_engine_actually_publishes():
    """A typo in an entity id would leave the user with a permanently empty card.

    The card is installed by hand on a dashboard, so nothing else would catch it: the add-on's own
    tests never load the card, and the failure appears only in the browser.
    """
    source = read(WEB / "sem-charts-card.js")
    engine = read(ROOT / "energy_manager" / "app" / "engine.py")
    published = set(re.findall(r'"(sensor\.sem_chart_[a-z_]+)"', engine))
    referenced = set(re.findall(r'"(sensor\.sem_chart_[a-z_]+)"', source))
    assert referenced, "the card references no chart feed entity"
    assert referenced <= published, f"card reads feeds the engine never publishes: {sorted(referenced - published)}"
    assert set(CHART_FEED_ENTITIES) <= published, "the engine no longer publishes a documented feed"


def test_the_card_restates_every_token_the_shared_chart_sheet_reads():
    """chart-theme.css is shared verbatim; every custom property it reads must exist in the card.

    The SPA gets them from `:root` in style.css, the card from `:host` in sem-charts-card.css. A
    property that is only in style.css silently resolves to nothing inside the shadow root (a gap
    becomes 0, a colour becomes inherit), which is invisible to a test that only greps for names.
    """
    theme = read(WEB / "chart-theme.css")
    card = read(WEB / "sem-charts-card.css")
    used = set(re.findall(r"var\(\s*(--[\w-]+)", theme))
    defined = set(re.findall(r"^\s*(--[\w-]+):", card, re.M))
    assert used, "chart-theme.css reads no tokens at all any more"
    assert not used - defined, f"the card sheet does not define {sorted(used - defined)}"


def _js_color_object(text: str, declaration: str) -> dict[str, str]:
    match = re.search(re.escape(declaration) + r"\s*=\s*\{([^}]*)\}", text)
    assert match, f"{declaration} not found"
    return dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", match.group(1)))


def test_the_card_palette_keeps_the_frozen_chart_token_names():
    """SPEC 13.1 freezes the --chart-* names; the card restates them in JS, so the three must agree.

    charts.js reads its colours from CSS custom properties and accepts an override object from the
    card. Two hand-written copies of the same colour set (the dark palette here and charts.js's
    fallback) drift silently, so the card is pinned to both the token map and the fallback values.
    """
    css = read(WEB / "style.css")
    charts = read(WEB / "charts.js")
    card = read(WEB / "sem-charts-card.js")
    tokens = _js_color_object(charts, "var TOKEN")
    fallback = _js_color_object(charts, "var DEFAULT_PALETTE")
    assert tokens and fallback, "charts.js must still expose a token map and a fallback palette"
    for name in tokens.values():
        assert f"{name}:" in css, f"charts.js reads {name}, which style.css no longer defines"

    palettes = {
        name: dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", body))
        for name, body in re.findall(r"(dark|light): \{(.*?)\}", card, re.S)
    }
    assert set(palettes) == {"dark", "light"}, "the card must define both palettes"
    for name, colors in palettes.items():
        assert set(colors) == set(tokens), f"the {name} palette keys do not match charts.js's token map: {sorted(set(colors) ^ set(tokens))}"
    for key, value in fallback.items():
        assert palettes["dark"][key] == value, f"the card's dark {key} drifted from charts.js's fallback"
    assert palettes["dark"] != palettes["light"], "the light palette must actually differ"


CARD_HARNESS = r"""
const calls = [];
const CHART_FNS = ["plan", "mini", "forecast", "savings", "accuracy"];
let uid = 0;
globalThis.SEM_CARD_CSS = "";
globalThis.SEMCharts = new Proxy({}, {
  get: (_target, name) => (...args) => calls.push([String(name), args])
});
class FakeNode {
  constructor(tag) { this.tagName = tag; this.uid = ++uid; this.className = ""; this._text = ""; this.children = []; this.attrs = {}; }
  // Setting textContent replaces the children, exactly like the real DOM: _message() and _render()
  // clear the body with it, and a stub that ignored that would hide a double render.
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this.children.length ? this.children.map((child) => child.textContent).join(" ") : this._text; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attrs[key] = value; }
}
globalThis.document = { createElement: (tag) => new FakeNode(tag) };
globalThis.HTMLElement = class {
  constructor() { this.attrs = {}; }
  attachShadow() { return { appendChild: () => {} }; }
  setAttribute(key, value) { this.attrs[key] = value; }
};
globalThis.customElements = {
  defined: {},
  get: () => undefined,
  define: (name, klass) => { globalThis.customElements.defined[name] = klass; }
};
globalThis.customCards = [];
require("./sem-charts-card.js");
const Card = globalThis.customElements.defined["sem-charts-card"];

function clone(value) {
  return JSON.parse(JSON.stringify(value, (_k, item) => item instanceof Date ? item.toISOString() : item));
}
function opts(options) {
  return options ? { wear: options.wear, roundTrip: options.efficiency && options.efficiency.round_trip, hasPalette: !!options.palette, price: options.palette && options.palette.price } : null;
}
function summary([fn, args]) {
  // forecast() takes the kind as a second positional argument, so its data and options shift.
  if (fn === "forecast") return { fn, kind: args[1], host: args[0].uid, data: clone(args[2]), options: opts(args[3]) };
  return { fn, host: args[0].uid, data: clone(args[1]), options: opts(args[2]) };
}
// The card is expected to reuse one host element; destroying that host's chart is a separate call
// (charts.destroyTree) and must not be mistaken for a draw.
function chartCalls() {
  return calls.splice(0).filter(([fn]) => CHART_FNS.includes(fn)).map(summary);
}
function destroyCalls() {
  return calls.splice(0).filter(([fn]) => fn === "destroyTree").length;
}
function passes() {
  return calls.splice(0).filter(([fn]) => CHART_FNS.includes(fn) || fn === "destroyTree");
}

function feed(entity, attributes, updated) {
  const states = {};
  states[entity] = { state: "ready", last_updated: updated || "2026-10-18T12:00:00+00:00", attributes };
  return { states, themes: { darkMode: true } };
}

const results = {};
const scenarios = {
  plan: ["sensor.sem_chart_plan", { schema: 1, slots: [[1700000000, 0.1145, 0.0912, 55.5, 1.234, 0.876, 421, 1]], round_trip: 0.85, wear_eur_per_kwh: 0.02 }],
  mini: ["sensor.sem_chart_plan", { schema: 1, slots: [[1700000000, 0.1145, 0.0912, 55.5, 1.234, 0.876, 421, 0]] }],
  price: ["sensor.sem_chart_price", { schema: 1, slots: [[1700000000, 0.1145, 0.0912, 1]] }],
  solar: ["sensor.sem_chart_solar", { schema: 1, slots: [[1700000000, 1.234, 12.5]] }],
  load: ["sensor.sem_chart_load", { schema: 1, slots: [[1700000000, 0.876, 0.5, 1.4]] }],
  savings: ["sensor.sem_chart_savings", { schema: 1, ranges: { day: [[1700000000, 1.25, 2.5, 0.4, 3.75]], week: [[1700086400, 4.5, 9.0, 1.1, 13.5]] } }],
  accuracy: ["sensor.sem_chart_accuracy", { schema: 1, days: [1700000000, 1700086400], pv: [12.5, null], load: [8.25, 9.5], price: [null, 15.75] }]
};
for (const [name, [entity, attributes]] of Object.entries(scenarios)) {
  const card = new Card();
  card.setConfig(name === "savings" ? { chart: name, range: "week" } : { chart: name });
  card.hass = feed(entity, attributes);
  const first = chartCalls();
  const firstMessage = card._body.textContent;
  // The same feed republished (a new plan, a refreshed forecast): the card must redraw the chart it
  // already has, not build a second one on a fresh element and strand the first.
  card.hass = feed(entity, attributes, "2026-10-18T12:20:00+00:00");
  const redraw = chartCalls();
  // Then the feed disappears (the add-on stops, the entity is renamed): the chart must be released
  // rather than left drawing behind the message.
  card.hass = { states: {}, themes: { darkMode: true } };
  results[name] = {
    calls: first,
    firstMessage: firstMessage,
    redraw: redraw,
    released: name === "plan" ? { destroys: destroyCalls(), message: card._body.textContent } : null,
    message: card._body.textContent,
    theme: card.attrs["data-theme"]
  };
}
const missing = new Card();
missing.setConfig({ chart: "plan" });
missing.hass = { states: {}, themes: { darkMode: false } };
results.missing = { calls: [], message: missing._body.textContent, theme: missing.attrs["data-theme"] };
const wrongSchema = new Card();
wrongSchema.setConfig({ chart: "solar" });
wrongSchema.hass = feed("sensor.sem_chart_solar", { schema: 2, slots: [] });
results.wrongSchema = { calls: [], message: wrongSchema._body.textContent, theme: wrongSchema.attrs["data-theme"] };
// 0.34.2 (SPEC 8.4): an unpriced plan publishes null prices with priced:false, and an empty price
// feed must be named rather than drawn as an empty chart.
const unpriced = new Card();
unpriced.setConfig({ chart: "plan" });
unpriced.hass = feed("sensor.sem_chart_plan", { schema: 1, priced: false, price_source: "none", slots: [[1700000000, null, null, 55.5, 1.234, 0.876, 421, 1]] });
results.unpriced = { calls: chartCalls(), message: unpriced._body.textContent };
const emptyPrice = new Card();
emptyPrice.setConfig({ chart: "price" });
emptyPrice.hass = feed("sensor.sem_chart_price", { schema: 1, slots: [] });
results.emptyPrice = { calls: chartCalls(), message: emptyPrice._body.textContent };
console.log(JSON.stringify(results));
"""


def test_the_card_maps_every_feed_row_into_the_chart_api():
    """Drive the card against real feed payloads and assert the chart call it makes.

    The card is the one part of the add-on that runs outside this repo's Python and JS tests, so the
    row adapters (frozen row shapes from feeds.py -> charts.js series objects) are exercised here.
    """
    data = json.loads(run_node_script(CARD_HARNESS))
    plan = data["plan"]
    assert [entry["fn"] for entry in plan["calls"]] == ["plan"]
    call = plan["calls"][0]
    assert call["host"] == plan["redraw"][0]["host"], "the card must redraw on the chart it already has"
    assert all(entry["host"] == call["host"] for entry in plan["redraw"])
    assert plan["released"]["destroys"] >= 1, "a card that loses its feed must release the chart"
    assert "not found" in plan["released"]["message"]
    assert call["data"]["slots"] == [
        {
            "t": "2023-11-14T22:13:20.000Z",
            "buy": 0.1145,
            "sell": 0.0912,
            "soc_pct": 55.5,
            "pv_kw": 1.234,
            "load_kw": 0.876,
            "power_w": 421,
            "price_stale": True,
        }
    ]
    assert call["options"]["wear"] == 0.02
    assert call["options"]["roundTrip"] == 0.85
    assert call["options"]["hasPalette"] is True
    assert plan["theme"] == "dark" and "Source: sensor.sem_chart_plan" in plan["firstMessage"]

    assert data["mini"]["calls"][0]["fn"] == "mini"
    assert data["price"]["calls"][0]["fn"] == "forecast"
    assert [data[name]["calls"][0]["kind"] for name in ("price", "solar", "load")] == ["price", "solar", "load"]
    assert data["price"]["calls"][0]["data"]["slots"] == [
        {"t": "2023-11-14T22:13:20.000Z", "buy": 0.1145, "sell": 0.0912, "stale": True}
    ]
    assert data["solar"]["calls"][0]["data"]["slots"] == [
        {"t": "2023-11-14T22:13:20.000Z", "pv_kw": 1.234, "uncertainty_pct": 12.5}
    ]
    assert data["load"]["calls"][0]["data"]["slots"] == [
        {"t": "2023-11-14T22:13:20.000Z", "load_kw": 0.876, "p10_kw": 0.5, "p90_kw": 1.4}
    ]
    # range=week picks the weekly buckets, and the bucket key stays the epoch the SPA uses
    savings = data["savings"]["calls"][0]
    assert savings["fn"] == "savings" and len(savings["data"]) == 1
    assert savings["data"][0] == {
        "key": "1700086400",
        "start": "2023-11-15T22:13:20.000Z",
        "savings_eur": 4.5,
        "cost_eur": 9.0,
        "revenue_eur": 1.1,
        "baseline_cost_eur": 13.5,
    }
    accuracy = data["accuracy"]["calls"][0]
    # 0.34.2: a null price reaches charts.js as null (a gap), never as 0.0, and the meta line says
    # why there is no price line at all.
    unpriced = data["unpriced"]
    assert [entry["fn"] for entry in unpriced["calls"]] == ["plan"]
    assert unpriced["calls"][0]["data"]["slots"][0]["buy"] is None
    assert unpriced["calls"][0]["data"]["slots"][0]["sell"] is None
    assert unpriced["calls"][0]["data"]["slots"][0]["soc_pct"] == 55.5
    assert "no price forecast" in unpriced["message"]
    empty_price = data["emptyPrice"]
    assert empty_price["calls"] == [], "an empty price feed must not draw a chart"
    assert "No price forecast" in empty_price["message"]
    assert accuracy["fn"] == "accuracy"
    assert accuracy["data"]["pv"]["series"] == [
        {"day": "2023-11-14T22:13:20.000Z", "mape_pct": 12.5},
        {"day": "2023-11-15T22:13:20.000Z", "mape_pct": None},
    ]
    assert [entry["mape_pct"] for entry in accuracy["data"]["price"]["series"]] == [None, 15.75]

    # A card on a dashboard whose add-on is stopped, or whose feed is a newer schema, must explain
    # itself instead of drawing an empty plot.
    assert data["missing"]["calls"] == [] and "not found" in data["missing"]["message"]
    assert data["missing"]["theme"] == "light"
    assert data["wrongSchema"]["calls"] == [] and "schema" in data["wrongSchema"]["message"]


def test_the_plan_timeline_reuses_one_cursor_sync_key():
    """Two aligned panels, drawn again and again, must not mint a sync key per draw.

    uPlot's sync registry keeps a hub per key for the life of the page, and the card redraws the plan
    every time the add-on publishes (~20 s), so a fresh key per draw retains an empty hub per redraw
    for as long as the dashboard tab is open.
    """
    data = json.loads(run_node_script(CHARTS_HARNESS))
    assert data["panelsPerDraw"] == [2, 2], data
    assert data["firstKeys"] == data["secondKeys"] == ["sem-plan"], data
    assert data["slotsPerPanel"] == [3, 3], data
    assert data["heights"] == [200, 175], data
    # Panel A: an empty x series, buy, stale, sell, break-even and SoC. Panel B: an empty x series
    # plus PV, load, charging and discharging.
    assert data["seriesPerPanel"] == [6, 5], data
    assert data["gradientFills"] >= 1, "the price bars lost their gradient fill"


def test_the_card_types_no_html_and_reaches_no_remote_asset():
    """The card is injected into a user's dashboard: it must build DOM, never parse HTML."""
    source = strip_js_comments(read(WEB / "sem-charts-card.js"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
        assert forbidden not in source, f"sem-charts-card.js uses {forbidden}"
    assert "fetch(" not in source and "XMLHttpRequest" not in source
    assert "http://" not in source and "https://" not in source


def test_the_card_bundle_has_no_duplicate_or_missing_source():
    """The served bundle is an assembly of committed files; keep the two lists in one place."""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "energy_manager"))
    from app.api import CARD_BUNDLE_CSS, CARD_BUNDLE_SOURCES  # noqa: E402

    for name in CARD_BUNDLE_CSS + CARD_BUNDLE_SOURCES:
        assert (WEB / name).is_file(), f"bundle source {name} is missing"
    assert "charts.js" in CARD_BUNDLE_SOURCES and "sem-charts-card.js" in CARD_BUNDLE_SOURCES


def test_the_card_bundle_inlines_every_stylesheet_its_charts_need():
    """A card cannot inherit the SPA's <link>s: whatever uPlot needs to lay out must be inlined.

    Regression for the mangled 0.29.0 card: only the two product sheets were inlined, so uPlot's own
    `.u-wrap { position: relative }` and `.uplot canvas { width/height: 100% }` never reached the shadow
    root - the absolute canvases and `.u-axis` labels fell into normal flow, the plot painted over its
    legend and the chart was clipped. The list is checked by content, not by order alone, so swapping
    the file for something smaller cannot pass.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "energy_manager"))
    from app.api import CARD_BUNDLE_CSS  # noqa: E402

    assert CARD_BUNDLE_CSS[0] == "vendor/uPlot.min.css", "uPlot's own sheet must be inlined first"
    assert CARD_BUNDLE_CSS.index("vendor/uPlot.min.css") < CARD_BUNDLE_CSS.index("chart-theme.css")
    uplot_css = (WEB / "vendor" / "uPlot.min.css").read_text(encoding="utf-8")
    for rule in (
        ".u-wrap {position: relative",
        ".u-over, .u-under {position: absolute",
        ".u-axis {position: absolute",
        ".uplot canvas {display: block",
    ):
        assert rule in uplot_css, f"vendored uPlot sheet no longer carries {rule}"
    # The card's own sheet must not restate uPlot's layout: it defines tokens and chrome only.
    card_css = (WEB / "sem-charts-card.css").read_text(encoding="utf-8")
    assert ".u-wrap" not in card_css and ".u-axis" not in card_css


CHARTS_HARNESS = r"""
/* charts.js only ever runs in a browser, so a fake uPlot is the only way to execute its option
   building at all. What this harness is really for: the cursor-sync key. uPlot's sync registry keeps
   a hub per key for the life of the page (its accessor only ever creates), so a key minted per draw
   would retain one empty hub per redraw - and the dashboard card redraws every time the add-on
   publishes. One key per chart family is also what gives two aligned timelines a shared crosshair. */
const mounts = [];
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
function fakeNode(tag) {
  return {
    tagName: tag, className: "", style: {}, textContent: "", children: [], attrs: {}, hidden: false,
    clientWidth: 640,
    classList: { add() {}, remove() {} },
    setAttribute(key, value) { this.attrs[key] = value; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {}, removeEventListener() {},
    contains: () => false,
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 640, height: 200, top: 0, left: 0 })
  };
}
globalThis.document = { createElement: fakeNode, documentElement: fakeNode("html"), body: fakeNode("body") };
globalThis.uPlot = function (opts, data, el) {
  mounts.push({
    sync: opts.cursor && opts.cursor.sync ? opts.cursor.sync.key : null,
    series: opts.series.length,
    slots: data[0].length,
    height: opts.height,
    gradientFill: typeof opts.series[1].fill
  });
  this.destroy = function () {};
  this.setSize = function () {};
};
globalThis.uPlot.paths = { bars: () => () => {}, spline: () => () => {} };
require("./charts.js");

const slot = (t, power) => ({
  t: new Date(t * 1000).toISOString(), buy: 0.11, sell: 0.09, soc_pct: 55, pv_kw: 1.2, load_kw: 0.6,
  power_w: power, price_stale: false
});
const plan = { slots: [slot(1700000000, 300), slot(1700000900, -200), slot(1700001800, 0)] };
const host = fakeNode("div");
const options = { wear: 0.02, efficiency: { round_trip: 0.85 } };
globalThis.SEMCharts.plan(host, plan, options);
const first = mounts.splice(0);
globalThis.SEMCharts.plan(host, plan, options);
const second = mounts.splice(0);
const keys = (list) => [...new Set(list.map((entry) => entry.sync))];
console.log(JSON.stringify({
  panelsPerDraw: [first.length, second.length],
  firstKeys: keys(first),
  secondKeys: keys(second),
  seriesPerPanel: first.map((entry) => entry.series),
  slotsPerPanel: first.map((entry) => entry.slots),
  heights: first.map((entry) => entry.height),
  gradientFills: first.filter((entry) => entry.gradientFill === "function").length
}));
"""


BUNDLE_LOADER = r"""
/* Load the served card bundle the way a browser would, with the DOM stubs that cannot exist in node.
   The bundle is an ES-module download whose uPlot source is an IIFE: if the wrapper or the global it
   assigns ever broke, the card would silently render nothing on a dashboard. */
const path = process.argv[2];
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => {};
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = () => {};
globalThis.devicePixelRatio = 1;
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
function fakeElement() {
  return {
    style: {},
    classList: { add() {}, remove() {} },
    appendChild() {}, setAttribute() {}, getContext: () => null,
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ width: 320, height: 180, top: 0, left: 0 })
  };
}
globalThis.document = {
  createElement: fakeElement, documentElement: {}, body: fakeElement(),
  addEventListener() {}, removeEventListener() {}, visibilityState: "visible"
};
globalThis.HTMLElement = class { attachShadow() { return { appendChild() {} }; } };
globalThis.customElements = { get: () => undefined, define() {} };
globalThis.customCards = [];
require(path);
console.log(JSON.stringify({
  charts: typeof globalThis.SEMCharts,
  plan: typeof (globalThis.SEMCharts && globalThis.SEMCharts.plan),
  uplot: typeof globalThis.uPlot,
  cssBytes: (globalThis.SEM_CARD_CSS || "").length,
  styleTag: /sem-charts-card|chart-tip/.test(globalThis.SEM_CARD_CSS || "")
}));
"""


def test_the_served_bundle_runs_and_registers_its_globals(tmp_path):
    """Assemble the bundle through the real API method, then execute it with DOM stubs.

    Every other check on the card looks at the text of the files. This one is the only place the
    assembled artifact is executed, which is where a broken uPlot wrapper or an unescaped CSS string
    would actually show up.
    """
    if NODE is None:
        pytest.skip("node is not installed")
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "energy_manager"))
    from app.api import ApiServer  # noqa: E402

    server = ApiServer.__new__(ApiServer)  # _card_bundle only needs web_root
    server.web_root = str(WEB)
    status, headers, body = server._card_bundle()
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    bundle = tmp_path / "sem-charts-card.bundle.js"
    bundle.write_bytes(body)
    loader = tmp_path / "load_bundle.js"
    loader.write_text(BUNDLE_LOADER, encoding="utf-8")
    proc = subprocess.run([NODE, str(loader), str(bundle)], capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["charts"] == "object" and result["plan"] == "function", result
    assert result["uplot"] == "function", "the wrapped uPlot build did not reach globalThis"
    assert result["cssBytes"] > 4000 and result["styleTag"], result
    # The assembled CSS must actually carry uPlot's layout rules, not just the two product sheets.
    from app.api import CARD_BUNDLE_CSS as _bundle_css  # noqa: E402

    css = "\n".join((WEB / name).read_text(encoding="utf-8") for name in _bundle_css)
    for rule in (".u-wrap {position: relative", ".uplot canvas {display: block", ".u-axis {position: absolute"):
        assert rule in css, f"the assembled card CSS is missing uPlot rule {rule}"
