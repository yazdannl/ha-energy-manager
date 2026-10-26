/* tools/ui_render_check.js -- render the real app.js + mock.js through the DOM shim and assert the
 * structure the UI is supposed to have (SPEC 13.1/13.2): every view renders, every setting is
 * reachable, advanced rows fold and unfold, no undefined text, no class without CSS, no external URL.
 *
 * Usage: node tools/ui_render_check.js [repo-root]     (exit 0 = all checks passed)
 * Run by tests/test_wp3_ui_render.py; skipped there when node is absent.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const { makeDom } = require("./ui_dom_shim.js");

const ROOT = process.argv[2] || path.resolve(__dirname, "..");
const WEB = path.join(ROOT, "energy_manager", "app", "web");

let passed = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) {
    passed += 1;
    console.log(`  ok   ${name}`);
  } else {
    failures.push(name + (detail ? ` -- ${detail}` : ""));
    console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ""}`);
  }
}

/* ---------- globals ---------- */
const dom = makeDom();
const globalListeners = {};
const storage = new Map();

globalThis.document = dom.document;
globalThis.location = { pathname: "/", search: "?mock=1", hash: "#/settings", href: "http://x/?mock=1#/settings" };
globalThis.addEventListener = (type, fn) => {
  (globalListeners[type] = globalListeners[type] || []).push(fn);
};
globalThis.removeEventListener = () => {};
globalThis.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = () => {};
globalThis.scrollTo = () => {};
globalThis.history = { replaceState() {}, pushState() {} };
try { Object.defineProperty(globalThis, "navigator", { value: { userAgent: "node" }, configurable: true }); } catch (e) { /* node >= 21 exposes navigator as a getter */ }
globalThis.alert = () => {};
globalThis.fetch = () => Promise.reject(new Error("network disabled in the harness"));

function fire(type, detail) {
  (globalListeners[type] || []).slice().forEach((fn) => fn(detail || { type }));
}

/* ---------- load the real sources ---------- */
(0, eval)(fs.readFileSync(path.join(WEB, "mock.js"), "utf8"));
(0, eval)(fs.readFileSync(path.join(WEB, "charts.js"), "utf8"));
(0, eval)(fs.readFileSync(path.join(WEB, "app.js"), "utf8"));
const A = globalThis.SEM_APP;
const M = globalThis.SEM_MOCK;

const view = () => dom.registry.view;
const allText = (node) => node.textContent;
const byClass = (node, cls) => node.querySelectorAll("." + cls);
// "present" is not "visible": advanced rows live inside a closed <details> until the user unfolds it.
function shownLabels(node, rows) {
  const found = new Set();
  // one row container per key: match the row's own data-key, never a label substring ("Energy tax"
  // would otherwise match the visible "Energy tax refunded" row).
  const wanted = new Set(rows.map((row) => row.key));
  node.querySelectorAll("[data-key]").forEach((field) => {
    if (!dom.shown(field)) return;
    const key = field.dataset && field.dataset.key;
    if (key && wanted.has(key)) found.add(key);
  });
  return found;
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function goto(route) {
  globalThis.location.hash = "#" + route;
  fire("hashchange", { type: "hashchange" });
  // the views fetch (mock) asynchronously: wait until the text settles *and* nothing is loading
  let last = -1;
  let stable = 0;
  for (let i = 0; i < 120; i += 1) {
    await sleep(25);
    const node = view();
    const size = node.textContent.length;
    if (size === last && size > 0 && !/Loading/.test(node.textContent)) stable += 1;
    else stable = 0;
    if (stable >= 3) break;
    last = size;
  }
}

function classTokens(text) {
  const out = new Set();
  const re = /class\s*[:=]\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(text))) m[1]
      .split(/\s+/)
      .filter((c) => c && !c.endsWith("-") && !c.startsWith("-") && /^[a-z][\w-]*$/.test(c))
      .forEach((c) => out.add(c));
  return out;
}

(async function main() {
  console.log(`rendering ${path.relative(ROOT, WEB)} with the DOM shim\n`);
  check("SEM_APP and SEM_MOCK loaded", !!A && !!M);
  if (!A || !M) process.exit(1);

  /* ---------- 1. every view renders ---------- */
  console.log("1. every view renders from mock data");
  const routes = ["/dashboard", "/plan", "/forecasts", "/batteries", "/cards", "/savings", "/settings", "/log"];
  for (const route of routes) {
    let error = null;
    await goto(route).catch((err) => (error = err));
    const text = allText(view());
    check(
      `${route} renders content`,
      !error && text.trim().length > 40,
      error ? String(error.message || error) : `text length ${text.trim().length}`,
    );
    if (!error) {
      check(`${route} has no undefined/NaN text`, !/\bundefined\b|\bNaN\b/.test(text), (text.match(/\bundefined\b|\bNaN\b/) || [])[0]);
    }
  }

  /* ---------- 1f. bounded forecast-vintage history ---------- */
  console.log("\n1f. Forecast History: timezone, controls, coverage and table");
  await goto("/forecasts");
  const forecastPanels = view().querySelectorAll(".panel");
  const forecastPanelByTitle = (title) => forecastPanels.find((candidate) => {
    const heading = candidate.querySelector("h2");
    return heading && heading.textContent === title;
  });
  const priceForecastPanel = forecastPanelByTitle("Price forecast");
  const solarForecastPanel = forecastPanelByTitle("Solar forecast");
  const loadForecastPanel = forecastPanelByTitle("Load forecast");
  check(
    "live solar and load totals show the API freshness state per day",
    !!solarForecastPanel && !!loadForecastPanel
      && /Freshness/.test(allText(solarForecastPanel)) && /Not marked stale/.test(allText(solarForecastPanel))
      && /Freshness/.test(allText(loadForecastPanel)) && /Not marked stale/.test(allText(loadForecastPanel)),
  );
  check(
    "price daily totals identify stale slots and explain their freshness impact",
    !!priceForecastPanel && /Stale slots/.test(allText(priceForecastPanel))
      && /slots flagged stale/.test(allText(priceForecastPanel))
      && /may not reflect current prices/.test(allText(priceForecastPanel)),
    priceForecastPanel ? allText(priceForecastPanel).slice(-360) : "no price forecast panel",
  );
  const historyPanel = byClass(view(), "forecast-history-result")[0];
  const historyDate = view().querySelectorAll("#forecast-history-date")[0];
  const historyResolution = view().querySelectorAll("#forecast-history-resolution")[0];
  const historyDays = view().querySelectorAll("#forecast-history-days")[0];
  check(
    "history offers a local date and 15-minute/hour/day resolutions",
    !!historyDate && historyDate.getAttribute("type") === "date" && !!historyResolution
      && ["15m", "hour", "day"].every((value) => historyResolution.querySelectorAll("option").some((option) => option.value === value)),
  );
  check(
    "history states its IANA timezone and direct-measurement rule",
    /IANA timezone/.test(allText(view())) && /inferred PV is excluded/.test(allText(view())),
    allText(view()).slice(-420),
  );
  check(
    "history renders a chart fallback and an accessible provenance/coverage table",
    !!historyPanel && !!historyPanel.querySelector("table")
      && /Vintage provenance/.test(allText(historyPanel)) && /complete coverage/.test(allText(historyPanel)),
    historyPanel ? allText(historyPanel).slice(0, 360) : "no history result",
  );
  const historyRows = historyPanel && historyPanel.querySelector("table")
    ? historyPanel.querySelector("table").querySelectorAll("tbody tr") : [];
  const missingVintageText = historyRows[0] ? allText(historyRows[0]) : "";
  const staleVintageText = historyRows[1] ? allText(historyRows[1]) : "";
  const partialPvText = historyRows[5] ? allText(historyRows[5]) : "";
  check(
    "history intervals explain missing vintages without inventing a provider failure",
    /No archived PV vintage/.test(missingVintageText)
      && /cause is not retained/.test(missingVintageText)
      && /no provider failure is inferred/.test(missingVintageText)
      && !/0\.00 kWh/.test(missingVintageText),
    missingVintageText,
  );
  check(
    "history intervals expose stale vintages and incomplete direct-PV comparison limits",
    /stale forecast interval/.test(staleVintageText) && /not fresh forecast evidence/.test(staleVintageText)
      && /capacity/.test(staleVintageText)
      && /Direct PV coverage is incomplete/.test(partialPvText) && /roll-up error is withheld/.test(partialPvText),
    staleVintageText + " · " + partialPvText,
  );
  historyResolution.value = "day";
  historyResolution.dispatchEvent({ type: "change" });
  await sleep(220);
  check("daily history exposes a bounded 1–31 day selector", !!historyDays && !historyDays.hidden);

  console.log("\n1g. Daily energy timeline: local bins, provenance, coverage and plan detail");
  const dailyDate = view().querySelectorAll("#daily-timeline-date")[0];
  const dailyControls = byClass(view(), "daily-timeline-controls")[0];
  const dailyNavLabels = dailyControls ? dailyControls.querySelectorAll("button").map((button) => button.getAttribute("aria-label")) : [];
  const dailyResult = byClass(view(), "daily-timeline-result")[0];
  const dailyTable = dailyResult && dailyResult.querySelector("table");
  const dailyInspectButtons = dailyTable ? dailyTable.querySelectorAll(".daily-timeline-interval") : [];
  const dailyText = dailyResult ? allText(dailyResult) : "";
  const timelineRows = dailyTable ? dailyTable.querySelectorAll("tbody tr") : [];
  const timelineRowText = (index) => timelineRows[index] ? allText(timelineRows[index]) : "";
  check(
    "daily timeline offers a local date selector and previous/next local-day controls",
    !!dailyDate && dailyDate.getAttribute("type") === "date"
      && dailyNavLabels.includes("Previous local day") && dailyNavLabels.includes("Next local day"),
  );
  check(
    "daily timeline retains a timezone-labelled accessible interval table and explicit coverage",
    !!dailyTable && dailyInspectButtons.length >= 92 && dailyInspectButtons.length <= 100
      && dailyInspectButtons[0].getAttribute("aria-pressed") === "true"
      && dailyInspectButtons[1].getAttribute("aria-pressed") === "false"
      && /IANA timezone/.test(allText(view()))
      && /Partial bins are covered-only/.test(dailyText)
      && /unavailable/.test(dailyText) && /Data sources and limits/.test(dailyText),
    dailyText.slice(0, 420),
  );
  check(
    "daily timeline keeps direct, derived, forecast and saved-plan series separate",
    /PV direct/.test(dailyText) && /PV derived/.test(dailyText)
      && /load direct/.test(dailyText) && /load derived/.test(dailyText)
      && /PV forecast/.test(dailyText) && /Load forecast/.test(dailyText)
      && /Plan buy/.test(dailyText) && /Grid/.test(dailyText),
    dailyText.slice(0, 420),
  );
  check(
    "daily intervals show stale PV, partial actual impact, missing forecast cause and provenance",
    /marked stale; not fresh forecast evidence/.test(timelineRowText(0))
      && /PV actual: sensor\.example_pv_power/.test(timelineRowText(0))
      && /PV forecast: synthetic archived solar/.test(timelineRowText(0))
      && /issued /.test(timelineRowText(0))
      && /freshness not classified/.test(timelineRowText(0))
      && /partial \(2\/3 samples\)/.test(timelineRowText(5))
      && /no complete PV forecast comparison/.test(timelineRowText(5))
      && /no archived value; cause not retained/.test(timelineRowText(8)),
    timelineRowText(0) + " · " + timelineRowText(5) + " · " + timelineRowText(8),
  );
  check("daily timeline provides a graceful chart fallback beside the accessible table",
    !!dailyResult && !!dailyResult.querySelector(".daily-timeline-chart.chart-fallback") && !!dailyTable);
  const intervalInspect = dailyTable && dailyTable.querySelector("tbody button");
  if (intervalInspect) intervalInspect.dispatchEvent({ type: "click" });
  const planDetailButton = dailyResult && dailyResult.querySelector(".daily-timeline-selection button");
  if (planDetailButton) planDetailButton.dispatchEvent({ type: "click" });
  await sleep(240);
  check(
    "daily interval opens exact saved-plan detail without claiming causal measured flow",
    !!planDetailButton && /plan-slot timestamps only/i.test(dailyText + allText(dailyResult))
      && /does not prove plan causality/.test(allText(dailyResult)),
    dailyResult ? allText(dailyResult).slice(-420) : "no daily result",
  );

  const pvScorecard = byClass(view(), "accuracy-scorecard")[0];
  const pvScorecardText = pvScorecard ? allText(pvScorecard) : "";
  check(
    "Forecast accuracy shows PV energy error, signed bias and matched coverage",
    /Archived PV forecast scorecard/.test(pvScorecardText)
      && /PV mean absolute error/.test(pvScorecardText)
      && /PV total absolute error/.test(pvScorecardText)
      && /PV mean signed bias/.test(pvScorecardText)
      && /Matched PV intervals/.test(pvScorecardText)
      && /kWh/.test(pvScorecardText),
    pvScorecardText.slice(0, 360),
  );
  check(
    "PV scorecard defines complete direct samples and exposes period, lead-time and source breakdowns",
    /three distinct fresh 5-minute direct pv_power samples/.test(pvScorecardText)
      && /Local day/.test(pvScorecardText)
      && /Forecast lead time/.test(pvScorecardText)
      && /Forecast source/.test(pvScorecardText),
    pvScorecardText.slice(-420),
  );
  check(
    "PV scorecard does not display percentage accuracy metrics",
    !!pvScorecard && !/MAPE|%/.test(pvScorecardText),
    pvScorecardText.slice(0, 300),
  );

  /* ---------- 1z. the dashboard-card installer (SPEC 12) ---------- */
  // The card is installed by hand from these three pieces of text: a download, a resource URL and a
  // card snippet. A typo here leaves the user with a dashboard tile that never appears.
  console.log("\n1z. cards view: download, resource and snippet");
  await goto("/cards");
  const cardsView = view();
  const cardsText = allText(cardsView);
  const download = cardsView.querySelectorAll("a").find((a) => a.getAttribute("download"));
  check(
    "the download link serves the card bundle as sem-charts-card.js",
    !!download
      && /\/api\/v1\/web\/sem-charts-card\.js$/.test(download.getAttribute("href"))
      && download.getAttribute("download") === "sem-charts-card.js",
    download ? download.getAttribute("href") : "no download link",
  );
  check(
    "the steps name the config path and the Lovelace resource URL",
    /\/config\/www\/sem-charts-card\.js/.test(cardsText) && /\/local\/sem-charts-card\.js/.test(cardsText),
    cardsText.slice(0, 200),
  );
  const snippetNode = cardsView.querySelectorAll(".chart-card-snippet")[0];
  check(
    "the snippet is a manual card with the plan chart selected",
    !!snippetNode
      && /^type: custom:sem-charts-card$/m.test(snippetNode.textContent)
      && /^chart: plan$/m.test(snippetNode.textContent),
    snippetNode ? snippetNode.textContent : "no snippet",
  );
  const chartOptions = cardsView.querySelectorAll("#card-chart option").map((option) => option.value);
  check(
    "the chart picker offers all seven cards",
    ["plan", "mini", "price", "solar", "load", "savings", "accuracy"].every((id) => chartOptions.includes(id)),
    chartOptions.join(","),
  );
  check(
    "every feed entity is documented in the view",
    ["plan", "price", "solar", "load", "savings", "accuracy"].every((kind) => cardsText.includes("sensor.sem_chart_" + kind)),
  );
  const cardPicker = cardsView.querySelectorAll("#card-chart")[0];
  cardPicker.value = "savings";
  cardPicker.dispatchEvent({ type: "change" });
  await sleep(120);
  check(
    "choosing another chart rewrites the snippet with its range",
    /^chart: savings$/m.test(snippetNode.textContent) && /^range: day$/m.test(snippetNode.textContent),
    snippetNode.textContent,
  );

  /* ---------- 1a. learned battery capacity on the Batteries card (SPEC 5) ---------- */
  console.log("\n1a. batteries card: effective vs learned capacity");
  await goto("/batteries");
  const capacityText = allText(view());
  check(
    "the batteries card shows the effective capacity, marked learned",
    /Capacity/.test(capacityText) && /learned/i.test(capacityText) && /9\.60 kWh/.test(capacityText),
    capacityText.slice(0, 240),
  );
  check(
    "the batteries card states the estimate's sample count and last update",
    /accepted measurement window/i.test(capacityText) && /last learned/i.test(capacityText),
    capacityText.slice(0, 240),
  );
  check(
    "the batteries card states the configured seed next to the effective value",
    /Configured 12\.00 kWh/.test(capacityText) && /effective 9\.60 kWh/.test(capacityText),
    capacityText.slice(0, 240),
  );
  // SPEC 5 (0.31.0): the round trip is learned the same way, and the card says so with evidence.
  check(
    "the batteries card states the learned round trip and its evidence",
    /Learned from measured cycles: 81\.3 % round trip/.test(capacityText)
      && /4 complete charge\/discharge cycle/.test(capacityText),
    capacityText.slice(0, 240),
  );
  check(
    "the batteries card states the configured seed next to the effective round trip",
    /Round trip 90\.0 % configured/.test(capacityText) && /81\.3 % effective/.test(capacityText),
    capacityText.slice(0, 240),
  );

  /* ---------- 1a2. cell-voltage protection on the Batteries card (SPEC 9.3) ---------- */
  console.log("\n1a2. batteries card: cell voltage and the guard");
  const cellText = allText(view());
  check(
    "the batteries card states the lowest cell voltage",
    /Cell voltage/.test(cellText) && /3\.050 V/.test(cellText),
    cellText.slice(0, 240),
  );
  check(
    "the batteries card marks a latched battery",
    /low cell voltage/.test(cellText) && /discharge blocked/.test(cellText),
    cellText.slice(0, 240),
  );

  /* ---------- 1a3. scheduled full-charge maintenance (SPEC 8.1) ---------- */
  console.log("\n1a3. batteries card: full-charge maintenance");
  const maintenanceCardText = allText(view());
  check(
    "the batteries card states the maintenance state and its target",
    /Maintenance/.test(maintenanceCardText) && /due now/.test(maintenanceCardText)
      && /target 100 %/.test(maintenanceCardText),
    maintenanceCardText.slice(0, 240),
  );
  check(
    "the batteries card states when the last full charge completed",
    /last completed/.test(maintenanceCardText),
    maintenanceCardText.slice(0, 240),
  );
  check(
    "an overdue battery is marked, not coloured only",
    /overdue/.test(A.maintenanceText({ watching: true, overdue: true, target_soc_pct: 100 })),
  );
  check(
    "the maintenance state of an unwatched battery reads off, never an invented due date",
    A.maintenanceText({ watching: false }) === "off",
  );

  /* ---------- 1b. quiet refresh ---------- */
  // A background refresh must update in place, never duplicate the answer and never re-render the
  // route (UI_REVIEW §3.5). This runs before the settings checks so no dirty-form guard is active.
  console.log("\n1b. quiet refresh (no duplicate answer)");
  await goto("/dashboard");
  const answersBefore = byClass(view(), "answer-authority").length;
  const authorityBefore = answersBefore
    ? byClass(view(), "answer-authority")[0].textContent
    : "";
  // The visibility path is what a returning tab triggers: the one that used to re-render the route.
  fire("visibilitychange", { type: "visibilitychange" });
  await sleep(60);
  const answersAfter = byClass(view(), "answer-authority").length;
  check("the dashboard answer renders exactly once", answersBefore === 1 && answersAfter === 1,
    `before=${answersBefore} after=${answersAfter}`);
  check("a quiet refresh keeps the same answer", answersBefore === 1
    && (byClass(view(), "answer-authority")[0] || {}).textContent === authorityBefore,
    "the authority line changed or vanished");

  /* ---------- 1b1b. a live safety condition must not strand the dashboard (0.20.6) ---------- */
  // /status.pending_holds drives the answer's "Safety state" banner through renderAutopilot's
  // append callback. A mismatch there (the banner was handed the callback as if it were a DOM
  // node) threw mid-paint, aborted the rest of the /status render, and left Warnings and activity,
  // Right now, Supporting measurements and Batteries on "Loading…" for good. Render the live shape
  // and assert the banner shows *and* every later panel still painted.
  console.log("\n1b1b. a pending hold renders the safety banner without stranding the panels");
  M.setSafetyState(["battery_1"], true);
  await goto("/dashboard");
  const safetyText = allText(view());
  check("the safety banner names the battery whose hold is still unconfirmed",
    /Safety state/.test(safetyText) && /A real setpoint may still be in force for: battery_1/.test(safetyText),
    safetyText.slice(0, 200));
  check("no dashboard panel is left on Loading… while a safety condition is live",
    !/Loading/.test(safetyText), (safetyText.match(/\S*Loading\S*/) || [])[0] || "");
  check("the panels painted after the answer still rendered (alerts, readings, batteries)",
    /(No warnings or errors\.|Recent activity)/.test(safetyText) && /Grid power/.test(safetyText)
      && /PV power/.test(safetyText) && /Example Battery 1/.test(safetyText),
    safetyText.slice(0, 240));
  M.setSafetyState([], true);

  /* ---------- 1b2. the manual override panel (0.20.0, SPEC 5/9.3/10) ---------- */
  // The panel is asserted in both documented states: nothing set, then an active override with a
  // per-battery limit — the mock serves /status manual_limits plus cap_charge_w/cap_discharge_w.
  console.log("\n1b2. manual override panel (force + per-direction limits)");
  const manualPanel = () => [...view().querySelectorAll(".panel")].find((p) => {
    const h2 = p.querySelector("h2");
    return h2 && h2.textContent === "Manual override";
  }) || null;

  await goto("/dashboard");
  let panel = manualPanel();
  check("the dashboard has one Manual override panel", !!panel
    && byClass(view(), "panel").filter((p) => (p.querySelector("h2") || {}).textContent === "Manual override").length === 1);
  let panelText = panel ? panel.textContent : "";
  check("the panel says no override is active and the plan is in control",
    /No manual override is active/.test(panelText), panelText.slice(0, 140));
  check("the panel renders the force controls (battery, direction, power, duration)",
    !!panel && !!panel.querySelector("#override-battery") && !!panel.querySelector("#override-direction")
    && !!panel.querySelector("#override-power") && !!panel.querySelector("#override-minutes")
    && /Charge/.test(panelText) && /Discharge/.test(panelText) && /Hold/.test(panelText)
    && /30 min/.test(panelText));
  check("the panel states the force is bounded by the protections and the limits",
    /A manual override bypasses the plan until it expires or you clear it; it is still bounded by the cell-voltage protection, the SOC floor and the limits above\./.test(panelText),
    panelText.slice(0, 200));
  check("the panel states the limits cap both the plan and manual overrides",
    /Limits cap both the automatic plan and manual overrides\. They can never raise a battery's configured maximum\./.test(panelText),
    panelText.slice(0, 200));
  check("the panel renders a limits row per battery plus an All batteries row",
    /All batteries/.test(panelText) && /Example Battery 1/.test(panelText) && /unlimited/.test(panelText)
    && /Max charge \(W\)/.test(panelText) && /Max discharge \(W\)/.test(panelText)
    && !/undefined|NaN/.test(panelText));
  check("the limits inputs carry the configured hardware maximum as their range",
    !!panel && [...panel.querySelectorAll("input[type=number]")].some((input) => input.max === "800"),
    panel ? [...panel.querySelectorAll("input[type=number]")].map((i) => i.id + "=" + i.max).join(", ") : "no panel");

  // State 2: an active override plus a per-battery limit, with dry run on and mode auto.
  M.routes["POST /api/v1/mode"]({ body: { mode: "auto", dry_run: true }, params: {} });
  M.routes["POST /api/v1/override"]({ body: { power_w: 600, minutes: 30, battery_id: null }, params: {} });
  M.routes["POST /api/v1/limits"]({ body: { battery_id: "battery_1", charge_w: 300 }, params: {} });
  await goto("/dashboard");
  panel = manualPanel();
  panelText = panel ? panel.textContent : "";
  check("an active override is stated with its direction, power, expiry and scope",
    /Manual override active/.test(panelText) && /Active: charge 600 W until \d{1,2}:\d{2}( [AP]M)? on all batteries/.test(panelText),
    panelText.slice(0, 200));
  check("the active override offers a confirming Clear",
    !!panel && [...panel.querySelectorAll("button")].some((b) => /Clear override/.test(b.textContent)));
  check("a set per-battery limit shows its value and source",
    /Charge: 300 W \(this battery\)/.test(panelText) && /Discharge: unlimited/.test(panelText),
    panelText.slice(0, 260));
  check("the dry-run state is stated for the override",
    /Dry run is on: an override is not written to the hardware/.test(panelText), panelText.slice(0, 200));
  check("the panel still shows no duplicate or undefined text",
    (panelText.match(/No manual override is active/g) || []).length === 0
    && !/undefined|NaN/.test(panelText));

  // Restore the shared mock state for the sections that follow.
  M.routes["DELETE /api/v1/override"]({ params: {} });
  M.routes["DELETE /api/v1/limits"]({ params: {} });
  M.routes["POST /api/v1/mode"]({ body: { mode: "advisory", dry_run: true }, params: {} });

  /* ---------- 1b3. battery energy price: both ledger figures (0.20.4, SPEC 5/10) ---------- */
  // The card is display-only: it reads /status.cost_basis (the ledger the optimiser acts on) plus
  // the plan summary's round trip and wear, and must distinguish a healthy ledger from a stalled
  // one (0.20.3's marker) and from a ledger with nothing booked.
  console.log("\n1b3. battery energy price card");
  const pricePanel = () => [...view().querySelectorAll(".panel")].find((p) => {
    const h2 = p.querySelector("h2");
    return h2 && h2.textContent === "Battery energy price";
  }) || null;
  const priceText = () => (pricePanel() ? pricePanel().textContent : "");

  const costStatus = await M.handle("GET", "/api/v1/status");
  const ledger = costStatus.cost_basis;
  const expectedBasis = A.fmtPrice(ledger.fleet_basis_eur_per_kwh, 3);
  const expectedFloor = A.fmtPrice(ledger.sell_floor_eur_per_kwh, 3);
  await goto("/dashboard");
  check("the dashboard has one Battery energy price panel", !!pricePanel()
    && byClass(view(), "panel").filter((p) => (p.querySelector("h2") || {}).textContent === "Battery energy price").length === 1);
  check("the card states the average charged price in EUR/kWh (3 decimals)",
    /Average charged price/.test(priceText()) && priceText().includes(expectedBasis)
    && /^\d+\.\d{3} EUR\/kWh$/.test(expectedBasis), `${expectedBasis} :: ${priceText().slice(0, 160)}`);
  check("the card states the break-even sell price in EUR/kWh (3 decimals)",
    /Break-even sell price/.test(priceText()) && priceText().includes(expectedFloor)
    && /^\d+\.\d{3} EUR\/kWh$/.test(expectedFloor), `${expectedFloor} :: ${priceText().slice(0, 200)}`);
  check("the break-even line breaks into basis, round trip, wear and margin",
    /from basis \d+\.\d{3} EUR\/kWh ÷ 90\.0 % round trip/.test(priceText())
    && /\d+\.\d{3} EUR\/kWh wear/.test(priceText()) && /\d+\.\d{3} EUR\/kWh margin/.test(priceText()),
    priceText().slice(0, 260));
  check("the card states the stored energy, its value and the ledger's freshness",
    /Stored 5\.40 kWh worth 1\.33 EUR/.test(priceText())
    && /ledger updated .+ \(\d+ min( \d+ s)? ago\)/.test(priceText()),
    priceText().slice(-260));
  check("a healthy ledger adds no stale warning",
    !/stalled/i.test(priceText()) && !/Stale reading/.test(priceText()));

  // 0.20.3's stall marker: a frozen floor must not look healthy.
  M.setCostBasisMode("stale");
  await goto("/dashboard");
  check("a stalled ledger shows a visible warning next to the numbers",
    /The ledger is stalled/.test(priceText()) && /No usable battery power samples since/.test(priceText())
    && /still show the last booked value and no longer follow the pack/.test(priceText()),
    priceText().slice(0, 300));
  check("the stalled reading is marked, not left looking healthy",
    /Stale reading/.test(priceText()) && pricePanel().querySelectorAll(".card-stale").length === 2,
    `${pricePanel() ? pricePanel().querySelectorAll(".card-stale").length : 0} marked cards`);

  // An empty ledger says so: a missing price is a dash, never a fake number.
  M.setCostBasisMode("empty");
  await goto("/dashboard");
  check("an empty ledger says so instead of showing a stale figure",
    /No ledger yet/.test(priceText()) && !/ledger is stalled/.test(priceText()), priceText().slice(0, 240));
  check("a missing price renders as a dash, never a fake number",
    /Average charged price--/.test(priceText()) && /Break-even sell price--/.test(priceText())
    && !/\d+\.\d{3} EUR\/kWh/.test(priceText()), priceText().slice(0, 240));

  // A disabled sell floor derives no floor but the average charged price is still the ledger's.
  M.setCostBasisMode("off");
  await goto("/dashboard");
  check("a disabled sell floor says so and derives no break-even price",
    /sell floor is switched off/.test(priceText()) && /Break-even sell price--/.test(priceText())
    && priceText().includes(expectedBasis), priceText().slice(0, 300));

  // Several enabled batteries: the fleet figures plus one row per battery.
  M.setCostBasisMode("ok");
  const fleetStatus = await M.handle("GET", "/api/v1/status");
  fleetStatus.cost_basis.batteries.push(Object.assign({}, fleetStatus.cost_basis.batteries[0], {
    id: "battery_2", name: "Second battery", cost_basis_eur_per_kwh: 0.3, sell_floor_eur_per_kwh: 0.39,
    energy_kwh: 2.0, stored_value_eur: 0.6,
  }));
  const fleetRows = fleetStatus.cost_basis.batteries;
  const fleetEnergy = fleetRows.reduce((sum, row) => sum + row.energy_kwh, 0);
  const fleetBasis = fleetRows.reduce((sum, row) => sum + row.cost_basis_eur_per_kwh * row.energy_kwh, 0) / fleetEnergy;
  fleetStatus.cost_basis.fleet_basis_eur_per_kwh = fleetBasis;
  const fleetHost = document.createElement("div");
  A.renderBatteryEnergyPrice((node) => fleetHost.appendChild(node), { status: fleetStatus, plan: null });
  check("several enabled batteries render a per-battery row",
    fleetHost.querySelectorAll(".stat-row li").length === 2
    && /Second battery/.test(fleetHost.textContent)
    && /average 0\.300 EUR\/kWh · break-even 0\.390 EUR\/kWh/.test(fleetHost.textContent),
    `${fleetHost.querySelectorAll(".stat-row li").length} rows: ${fleetHost.textContent.slice(0, 200)}`);
  check("the fleet figures stay the ledger's energy-weighted mean, not one battery's",
    fleetHost.textContent.includes(A.fmtPrice(fleetBasis, 3))
    && Math.abs(A.costBasisView(fleetStatus, null).energyKwh - fleetEnergy) < 1e-9,
    A.fmtPrice(fleetBasis, 3));
  // Optimizer v2 stage 1b: a contract refusal prices PV charge at the justified floor, and the
  // status flag must say so instead of letting the floor read as the real export rate.
  const boundStatus = Object.assign({}, fleetStatus, {
    cost_basis: Object.assign({}, fleetStatus.cost_basis, { feed_in_floor_is_bound: true }),
  });
  const boundHost = document.createElement("div");
  A.renderBatteryEnergyPrice((node) => boundHost.appendChild(node), { status: boundStatus, plan: null });
  check("a contract refusal flags the floor as a conservative bound",
    /conservative bound/.test(boundHost.textContent) && /unpriced_export/.test(boundHost.textContent),
    boundHost.textContent.slice(-220));
  const stalledView = A.costBasisView({
    cost_basis: { enabled: true, batteries: [{ id: "b", name: "B", cost_basis_eur_per_kwh: 0.2, energy_kwh: 1.0, stale: true, no_samples_since: "2026-01-01T00:00:00Z" }] },
  }, null);
  check("the view marks the stalled battery and names it",
    stalledView.stale === true && stalledView.stalledNames.join(",") === "B");

  /* ---------- 1c. next actions: the data behind each time slot (SPEC 10) ---------- */
  // The window rows are the phone-first reading of the plan, so they are checked as data first
  // (pure helpers, deterministic synthetic slots) and then as rendered structure.
  console.log("\n1c. next actions: prices, energy, SoC, execution evidence");
  const HOUR = 3600000;
  const hourStart = Math.floor(Date.now() / HOUR) * HOUR;
  function slotAt(index, overrides) {
    return Object.assign({
      t: new Date(hourStart + index * HOUR).toISOString(),
      buy: 0.2, sell: 0.3, pv_kw: 0.5, load_kw: 0.8, load_p10: 0.5, load_p90: 1.1,
      soc_kwh: 5.0, soc_pct: 50.0, power_w: -800.0, action: "discharge", reason: "peak_discharge",
      price_stale: false, pv_source: "capacity", price_source: "zonneplan_entity",
    }, overrides || {});
  }
  const syntheticPlan = {
    slot_minutes: 60,
    summary: { eta_charge: 0.95, eta_discharge: 0.95, round_trip_efficiency: 0.9025, wear_eur_per_kwh: 0.02 },
    slots: [
      slotAt(0),
      slotAt(1, { soc_pct: 40.0 }),
      slotAt(2, { power_w: 0.0, action: "hold", reason: "reserve_hold", price_stale: true, soc_pct: 40.0 }),
    ],
  };
  const syntheticExecution = {
    floors: { reserve_soc_pct: 20.0, min_soc_pct: 10.0, floor_soc_pct: 20.0 },
    progress: { slots_elapsed: 0, slots_measured: 1, slots_total: 3, coverage_pct: 25.0, measured_discharged_kwh: 0.2, measured_charged_kwh: 0.0 },
    slots: [
      {
        t: syntheticPlan.slots[0].t,
        dispatch: { ts: syntheticPlan.slots[0].t, requested_w: -800.0, applied_w: -400.0, ok: true, dry_run: false, clamped: true, code: "power_clamp", reason: "power_clamp", message: "clamped", records: 1, batteries: [{ id: "battery_1", requested_w: -800.0, applied_w: -400.0, ok: true }] },
        actual: { samples: 1, coverage_pct: 25.0, pv_kw: 0.5, load_kw: 0.8, grid_kw: -1.1, battery_kw: -0.7, planned_kw: -0.8, measured_kw: -0.7, import_kwh: 0.0, export_kwh: 0.27, charged_kwh: 0.0, discharged_kwh: 0.175, soc_kwh_start: 5.0, soc_kwh_end: 4.8 },
      },
      { t: syntheticPlan.slots[1].t, dispatch: null, actual: null },
      { t: syntheticPlan.slots[2].t, dispatch: null, actual: null },
    ],
  };
  const syntheticWindows = A.planActionWindows(syntheticPlan, syntheticExecution);
  check("windows merge consecutive identical slots only", syntheticWindows.length === 2
    && syntheticWindows[0].slots === 2 && syntheticWindows[1].slots === 1,
    `${syntheticWindows.length} windows: ${syntheticWindows.map((w) => w.slots).join("+")}`);
  check("every slot belongs to exactly one window",
    syntheticWindows.reduce((sum, win) => sum + win.slots, 0) === syntheticPlan.slots.length);
  check("the plan's own resolution drives the window end",
    syntheticWindows[0].endMs === syntheticWindows[0].startMs + 2 * HOUR
    && A.planSlotMinutes({ slot_minutes: 15 }) === 15 && A.planSlotMinutes({}) === 60);
  const ctx = A.planWindowContext({
    plan: syntheticPlan, execution: syntheticExecution, cycleCost: 0.02,
    providers: { price: [{ name: "zonneplan_entity", active: true, stale: false, last_success: new Date().toISOString() }] },
  });
  const discharge = syntheticWindows[0];
  const facts = A.windowFacts(discharge, ctx);
  const labels = facts.map((fact) => fact.label).join(", ");
  check("a window states the prices behind the decision", /Buy → sell/.test(labels) && /20\.0 ct\/kWh/.test(facts[0].value) && /30\.0 ct\/kWh/.test(facts[0].value) && /\+10\.0 ct\/kWh/.test(facts[0].value), facts[0] ? facts[0].value : "no facts");
  check("a window states its break-even and the margin over it",
    /Break-even/.test(labels) && /clear/.test(facts[1].value) && /24\.2 ct\/kWh/.test(facts[1].value), facts[1] ? facts[1].value : "no break-even");
  check("a window states the battery energy and the SoC path",
    /Battery/.test(labels) && /1\.60 kWh out/.test(facts[2].value)
    && /SoC/.test(labels) && /50 % → 40 %/.test(facts[3].value) && /floor 20 %/.test(facts[3].value),
    `${facts[2].value} / ${facts[3].value}`);
  // 0.20.1 (SPEC 8.16): a partial first slot carries its own modelled duration, so its energy and
  // cost use the window the solver did; the timestamp grid (and the window end) stays clock-aligned.
  const partialPlan = {
    slot_minutes: 60,
    summary: {},
    slots: [slotAt(0, { minutes: 44.0 }), slotAt(1, { minutes: 60.0 })],
  };
  const partialWindows = A.planActionWindows(partialPlan, { slots: [] });
  check("a partial first slot's energy and cost use its modelled minutes",
    partialWindows.length === 1
    && Math.abs(partialWindows[0].minutes - 104) < 1e-9
    && Math.abs(partialWindows[0].discharged_kwh - 0.8 * 104 / 60) < 1e-9
    && Math.abs(partialWindows[0].cost_eur - (-0.5 * 0.3 * 104 / 60)) < 1e-9
    && partialWindows[0].endMs === partialWindows[0].startMs + 2 * HOUR,
    `${partialWindows.length} windows, ${partialWindows[0] && partialWindows[0].minutes} min`);
  check("a window states its grid flow and what it earns",
    /Grid cost/.test(labels) && /exports 1\.00 kWh/.test(facts[4].value) && /earns/.test(facts[4].value), facts[4] ? facts[4].value : "no grid fact");
  // 0.35.0 (SPEC 5): the plan states what it did to the pack, which candidate won and by how much.
  const variantPlan = {
    slot_minutes: 60,
    feasible: true,
    summary: {
      saved_eur: 0.4232, net_eur: 0.3356, energy_delta_kwh: -0.36,
      replacement_eur_per_kwh: 0.2435, plan_variant: "refill_floor",
      plan_variant_label: "Refill-aware floor", plan_variant_margin_eur: 0.31,
    },
    variants: {
      chosen: "refill_floor", chosen_label: "Refill-aware floor", net_eur: 0.3356,
      margin_eur: 0.31, tie: false,
      candidates: [
        { id: "refill_floor", label: "Refill-aware floor", net_eur: 0.3356, sold_below_basis_kwh: 0.42 },
        { id: "ledger_floor", label: "Never below the ledger floor", net_eur: 0.0256, sold_below_basis_kwh: 0.0 },
        { id: "hold_pack", label: "Charge only, never discharge", net_eur: -0.0058, sold_below_basis_kwh: 0.0 },
      ],
    },
    slots: [slotAt(0)],
  };
  const netText = A.planNetText(variantPlan);
  check("the plan states the net after the stored energy and which candidate won",
    /Net after the stored energy/.test(netText) && /0\.34 EUR/.test(netText)
    && /0\.36 kWh less in the pack/.test(netText)
    && /Most profitable of 3 candidates: Refill-aware floor/.test(netText)
    && /0\.31 EUR ahead of/.test(netText), netText);
  check("the plan says when it sold below the cost basis -- and when it did not",
    /discharges 0\.42 kWh below the pack's cost basis/.test(netText));
  const tiePlan = Object.assign({}, variantPlan, {
    summary: Object.assign({}, variantPlan.summary, { plan_variant_margin_eur: 0.0, energy_delta_kwh: 0.0 }),
    variants: Object.assign({}, variantPlan.variants, { tie: true, margin_eur: 0.0 }),
  });
  const tieText = A.planNetText(tiePlan);
  check("a tie is stated as a tie, and an unchanged pack as an unchanged pack",
    /tied with/.test(tieText) && /the pack ends where it started/.test(tieText)
    && !/ahead of/.test(tieText), tieText);
  check("a plan without a comparison says nothing about one",
    A.planNetText({ slots: [], summary: {} }) === "" && A.planNetText(null) === "");

  // 0.34.2 (SPEC 8.4): a horizon with no tariff must say so. "0.0 ct/kWh" would read as free
  // electricity, and "-- → --" as a rendering bug.
  const unpricedPlan = {
    slot_minutes: 60,
    feasible: true,
    summary: { eta_charge: 0.95, eta_discharge: 0.95, round_trip_efficiency: 0.9025, wear_eur_per_kwh: 0.02 },
    sources: { plan: "self_consumption", price: "none" },
    slots: [
      slotAt(0, { buy: 0.0, sell: 0.0, price_source: "none", price_stale: true, action: "pv_charge", reason: "pv_surplus_charge" }),
      slotAt(1, { buy: 0.0, sell: 0.0, price_source: "none", price_stale: true, soc_pct: 60.0 }),
    ],
  };
  const unpricedWindows = A.planActionWindows(unpricedPlan, { slots: [] });
  const unpricedFacts = A.windowFacts(unpricedWindows[0], ctx);
  check("an unpriced window says it has no price instead of 0.0 ct/kWh",
    /no price forecast/.test(unpricedFacts[0].value) && /needs a price/.test(unpricedFacts[1].value),
    `${unpricedFacts[0].value} / ${unpricedFacts[1].value}`);
  const unpricedFlags = A.planFlags(unpricedPlan);
  check("an unpriced plan is flagged as such, not as stale prices",
    unpricedFlags.flags.length === 1
      && unpricedFlags.flags[0].title === "No price forecast"
      && /zero-export self-consumption/.test(unpricedFlags.flags[0].text)
      && !unpricedFlags.flags.some((flag) => /stale/i.test(flag.title)),
    unpricedFlags.flags.map((flag) => flag.title).join(","));
  check("an unpriced plan is recognised from its marker or its slots",
    A.planIsUnpriced(unpricedPlan) === true
      && A.planIsUnpriced({ sources: { plan: "self_consumption" }, slots: [] }) === true
      && A.planIsUnpriced(syntheticPlan) === false
      && A.planIsUnpriced(null) === false);
  check("the join lands the dispatch and the measurement on the right window",
    discharge.dispatch && discharge.dispatch.clamped === true && discharge.dispatchSlots === 1
    && discharge.actual && Math.abs(discharge.measured_discharged_kwh - 0.175) < 1e-9
    && syntheticWindows[1].dispatch === null && syntheticWindows[1].actual === null);
  check("a clamped dispatch states applied vs requested",
    /applied 400 W of 800 W requested \(clamped: /.test(A.windowStatusText(discharge, ctx)), A.windowStatusText(discharge, ctx));
  check("a measured window compares measured with planned",
    /measured -0\.70 kW vs -0\.80 kW planned/.test(A.windowStatusText(discharge, ctx)), A.windowStatusText(discharge, ctx));
  const staleWindow = syntheticWindows[1];
  check("a stale window says the dispatcher refuses it (stale_input)",
    /not dispatchable/.test(A.windowStatusText(staleWindow, ctx)) && /stale_input/.test(A.windowStatusText(staleWindow, ctx)));
  // A command the device has not confirmed yet is stated as commanded, never as measured or as a
  // failure: the Zendure manager applies manual power on its next P1 event, which can be minutes.
  const commandedWindow = Object.assign({}, discharge, {
    dispatch: Object.assign({}, discharge.dispatch, { code: "commanded", clamped: false })
  });
  check("a commanded dispatch says the device has not confirmed it yet",
    /commanded 400 W of 800 W requested/.test(A.windowStatusText(commandedWindow, ctx))
    && /not confirmed/.test(A.windowStatusText(commandedWindow, ctx))
    && !/failed/.test(A.windowStatusText(commandedWindow, ctx)),
    A.windowStatusText(commandedWindow, ctx));
  check("the battery card names the write path that is really in use",
    A.dispatchPathText({ control_path: "device" }, { control_path: "device_limits" }) === "Inverter: AC mode + limits (chosen)"
    && A.dispatchPathText({ control_path: "auto" }, { control_path: "manager_manual_power" }) === "Zendure manager: manual power (auto)"
    && A.dispatchPathText({}, { driver: "zendure_mqtt", control_path: "device_limits" }) === "Inverter over MQTT: AC mode + limits (the device's own entities)"
    && A.dispatchPathText({}, { driver: "zendure_native", connection: { host: "example.invalid", scheme: "http" } }) === "Device over zenSDK HTTP: AC mode + input/output limits (example.invalid over HTTP)"
    && A.dispatchPathText({}, {}) === "unknown (auto)",
    A.dispatchPathText({ control_path: "device" }, { control_path: "device_limits" }));
  // The native driver has no entities: its wizard fields are the connection settings, and the
  // client refuses a malformed host/serial before the server's own save-time probe would.
  const nativeProblems = A.batteryNativeConnectionProblems({ host: "http://example.invalid", serial: "", scheme: "ftp", port: "70000" });
  check("the native driver's connection settings are validated in the wizard",
    nativeProblems.length >= 4
    && A.batteryNativeConnectionProblems({ host: "device.example.invalid", serial: "EXAMPLE0000001" }).length === 0,
    JSON.stringify(nativeProblems));
  // A Zendure inverter bridged by Home Assistant's own MQTT integration is a driver of its own:
  // the device is identified by platform + manufacturer/model, and each row's role follows from
  // the zenSDK property name it carries (no confidence score is involved).
  const mqttGroup = A.batteryGroupsFromCandidates(M.fixtures().discover.battery.candidates)
    .filter((g) => A.batteryIsZendureMqtt(g))[0];
  const mqttPick = mqttGroup ? A.batterySelection(mqttGroup, []) : { mapping: {}, numbers: {} };
  check("a Zendure MQTT bridge device is picked as its own driver",
    !!mqttGroup && A.batteryDriverFor(mqttGroup) === "zendure_mqtt"
    && mqttPick.mapping.charge_limit_entity === "number.example_mqtt_inputlimit"
    && mqttPick.mapping.discharge_limit_entity === "number.example_mqtt_outputlimit"
    && mqttPick.mapping.ac_mode_entity === "select.example_mqtt_acmode"
    && mqttPick.mapping.soc_entity === "sensor.example_mqtt_electriclevel"
    && mqttPick.mapping.smart_mode_entity === "switch.example_mqtt_smartmode"
    && mqttPick.numbers.max_charge_w === 1000 && mqttPick.numbers.max_discharge_w === 800,
    JSON.stringify(mqttPick.mapping));
  check("a stale window's price is not presented as a fresh one",
    /no fresh price/.test(A.windowPriceText(staleWindow, ctx)));
  check("a fresh window names its price source and rank",
    /zonneplan_entity/.test(A.windowPriceText(discharge, ctx)) && /rank 1 of 3/.test(A.windowPriceText(discharge, ctx)),
    A.windowPriceText(discharge, ctx));
  check("the horizon summary adds the windows up",
    /2 windows/.test(A.windowSummary(syntheticWindows, ctx)) && /1\.60 kWh out/.test(A.windowSummary(syntheticWindows, ctx))
    && /measured so far/.test(A.windowSummary(syntheticWindows, ctx)),
    A.windowSummary(syntheticWindows, ctx));
  check("a missing measurement is never reported as a zero",
    /nothing measured yet/.test(A.windowSummary(syntheticWindows, { progress: { slots_elapsed: 0, slots_total: 3, coverage_pct: 0 } })));

  await goto("/plan");
  const historyText = allText(view());
  check("Plan history renders separated dispatch and measured evidence, including unmatched rows",
    historyText.includes("Plan history") && historyText.includes("Dispatcher requested")
      && historyText.includes("Driver-reported applied") && historyText.includes("Measured battery")
      && historyText.includes("Measured grid") && historyText.includes("Unmatched dispatch rows"),
    historyText.slice(-800));
  const livePlanFixture = await M.handle("GET", "/api/v1/plan?hours=36");
  const executionFixture = await M.handle("GET", "/api/v1/plan/execution?hours=36");
  check("the plan payload carries its own resolution",
    livePlanFixture.slot_minutes > 0 && A.planSlotMinutes(livePlanFixture) === livePlanFixture.slot_minutes);
  const mockWindows = A.planActionWindows(livePlanFixture, executionFixture);
  check("the rendered window list matches the payload's windows",
    byClass(view(), "window-list").length === 1 && byClass(view(), "window-facts").length === mockWindows.length,
    `${byClass(view(), "window-facts").length} rows for ${mockWindows.length} windows`);
  check("the rendered rows carry facts, context, status and price provenance",
    byClass(view(), "window-context").length === mockWindows.length
    && byClass(view(), "window-status").length === mockWindows.length
    && byClass(view(), "window-price").length === mockWindows.length
    && byClass(view(), "window-summary").length === 1);
  const factText = byClass(view(), "window-facts").map((node) => node.textContent).join(" | ");
  check("the rendered facts name the prices, break-even, energy, SoC and grid cost",
    /Buy → sell/.test(factText) && /Break-even/.test(factText) && /Battery/.test(factText)
    && /SoC/.test(factText) && /Grid cost/.test(factText), factText.slice(0, 160));
  check("the rendered rows mark the window that is running now",
    byClass(view(), "badge-info").length >= 1, `${byClass(view(), "badge-info").length} now badges`);
  check("the plan view marks a maintenance plan from its payload",
    /Maintenance plan/.test(allText(view())) && A.maintenancePlanText(livePlanFixture.maintenance).length > 0,
    allText(view()).slice(0, 240));
  check("a non-active maintenance block adds no banner",
    A.maintenancePlanText({ active: false, target_soc_pct: 100, batteries: ["battery_1"] }) === "");
  check("the rendered execution line states what happened",
    mockWindows.some((win) => win.dispatch || win.actual)
    && /(applied|not written|measured|not dispatchable|no dispatch recorded)/.test(byClass(view(), "window-status").map((n) => n.textContent).join(" | ")),
    byClass(view(), "window-status").map((n) => n.textContent).join(" | ").slice(0, 160));

  /* ---------- 1c2. next actions: the icon and graphic layer (SPEC 13.1) ---------- */
  // Every visual cue in this panel has to survive being read aloud or seen without colour: a shape on
  // every action and fact, a place on the horizon for every window, and the same words beside both.
  console.log("\n1c2. next actions: action icons, charge graphic and the horizon ribbon");
  const iconCount = (node) => node.querySelectorAll("svg").length;
  const cards = byClass(view(), "window");
  check("the panel opens with the horizon totals beside an icon",
    byClass(view(), "window-summary-block").length === 1
    && byClass(view(), "window-summary-icon").length === 1
    && iconCount(byClass(view(), "window-summary-icon")[0]) === 1,
    `${byClass(view(), "window-summary-icon").length} summary icons`);
  check("every window is one card with an action tile and a charge graphic",
    cards.length === mockWindows.length
    && byClass(view(), "window-icon").length === mockWindows.length
    && byClass(view(), "window-icon").every((node) => iconCount(node) === 1)
    && byClass(view(), "window-soc-track").length === mockWindows.length,
    `${cards.length} cards, ${byClass(view(), "window-icon").length} tiles, ${byClass(view(), "window-soc-track").length} tracks`);
  check("every window card carries a fact glyph per fact",
    byClass(view(), "window-facts").every((list) => list.querySelectorAll(".fact-icon").length === list.querySelectorAll("li").length)
    && byClass(view(), "fact-icon").every((node) => iconCount(node) === 1),
    `${byClass(view(), "fact-icon").length} fact glyphs`);
  check("the charge graphic fills to the level and marks the floor",
    byClass(view(), "window-soc-track").every((track) => {
      const fill = track.querySelector(".window-soc-fill");
      if (!fill) return false;
      const width = parseFloat(fill.style.width);
      return width > 0 && width <= 100.5;
    })
    && byClass(view(), "window-soc-floor").length === mockWindows.length
    && byClass(view(), "window-soc-floor").every((tick) => {
      const left = parseFloat(tick.style.left);
      return left >= 0 && left <= 100;
    }),
    byClass(view(), "window-soc-fill").map((n) => n.style.width).join(" ").slice(0, 120));
  check("the horizon ribbon has a segment per window, a now marker and day ticks",
    byClass(view(), "window-ribbon-track").length === 1
    && byClass(view(), "window-ribbon-seg").length === mockWindows.length
    && byClass(view(), "window-ribbon-now").length === 1
    && byClass(view(), "window-ribbon-day").length >= 1,
    `${byClass(view(), "window-ribbon-seg").length} segments, ${byClass(view(), "window-ribbon-day").length} day ticks`);
  check("every ribbon segment sits inside the track it is placed on",
    byClass(view(), "window-ribbon-seg").every((seg) => {
      const left = parseFloat(seg.style.left);
      const width = parseFloat(seg.style.width);
      return left >= 0 && width > 0 && left + width <= 100.5;
    }),
    byClass(view(), "window-ribbon-seg").map((n) => `${n.style.left}+${n.style.width}`).join(" ").slice(0, 140));
  const legendText = byClass(view(), "window-ribbon-legend").map((n) => n.textContent).join(" | ");
  const windowActions = new Set(mockWindows.map((win) => A.actionText(win.action)));
  check("the ribbon legend names every action the window list uses",
    byClass(view(), "window-ribbon-legend").length === 1
    && [...windowActions].every((label) => legendText.includes(label))
    && byClass(view(), "window-legend-swatch").length === windowActions.size,
    `legend "${legendText.slice(0, 120)}" for ${[...windowActions].join(", ")}`);
  check("the ribbon describes itself for a reader who cannot see it",
    /The plan's next actions across its horizon:/.test(byClass(view(), "window-ribbon-track")[0].getAttribute("aria-label") || ""),
    byClass(view(), "window-ribbon-track")[0].getAttribute("aria-label"));
  check("the charge graphic repeats its numbers as text",
    byClass(view(), "window-soc-label").every((node) => /SoC \d+ % → \d+ %/.test(node.textContent))
    && byClass(view(), "window-soc-label").length === mockWindows.length,
    byClass(view(), "window-soc-label").map((n) => n.textContent).join(" | ").slice(0, 120));
  check("every window card is tinted by its own action, not by a fixed colour",
    cards.filter((card, i) => card.className.includes(A.actionClass(mockWindows[i].action))).length === mockWindows.length,
    cards.map((c) => c.className).join(" | ").slice(0, 160));
  check("an unknown action still draws a glyph and a neutral tint",
    A.actionIcon("invented_action") === "bolt" && A.actionClass("invented_action") === "window--idle"
    && A.factIcon("invented fact") === "bolt");
  check("the horizon geometry is derived from the plan, not assumed",
    (() => {
      const bounds = A.planHorizonBounds(livePlanFixture.slots, livePlanFixture.slot_minutes);
      if (!bounds || !(bounds.spanMs > 0)) return false;
      const first = A.windowRail({ startMs: bounds.startMs, endMs: bounds.startMs + bounds.spanMs / 2 }, bounds);
      const late = A.windowRail({ startMs: bounds.endMs, endMs: bounds.endMs + 1000 }, bounds);
      return first.left === 0 && Math.abs(first.width - 50) < 0.001 && late.left === 100 && A.planHorizonBounds([], 15) === null;
    })());
  check("the charge track keeps a single-slot window meaningful and names no floor it was not given",
    A.socTrack({ socStart: 40, socEnd: 40 }, 10).fill === 40
    && A.socTrack({ socStart: 40, socEnd: 40 }, 10).floor === 10
    && A.socTrack({ socStart: 40, socEnd: 40 }, 10).moved === false
    && A.socTrack({ socStart: 55, socEnd: 20 }, null).floor === null
    && A.socTrack({ socStart: 55, socEnd: 20 }, 10).moved === true
    && A.socTrack({ socStart: null, socEnd: 20 }, 10) === null
    && A.socTrack({ socStart: 55, socEnd: 20 }, 10).rising === false);

  /* ---------- 1d. savings: measured battery flows only (0.13.0) ---------- */
  console.log("\n1d. savings measured battery flows");
  await goto("/savings");
  const savingsMonth = await M.handle("GET", "/api/v1/savings?range=month");
  const monthBuckets = savingsMonth.buckets || [];
  check("the savings payload carries measured battery flows and per-bucket coverage",
    monthBuckets.length > 0
    && monthBuckets.every((b) => "charged_kwh" in b && "discharged_kwh" in b && "battery_measured" in b)
    && monthBuckets.some((b) => b.charged_kwh != null)
    && monthBuckets.some((b) => b.battery_measured === false),
    `${monthBuckets.length} buckets`);
  const savingsText = allText(view());
  check("the savings view labels measured battery flows",
    /Measured battery flows/.test(savingsText) && /Charged \(measured\)/.test(savingsText)
    && /Discharged \(measured\)/.test(savingsText) && /Charge cost/.test(savingsText),
    savingsText.slice(0, 160));
  check("the savings view states sampled-time coverage",
    /Sampled-time coverage: grid [\d.]+ h, battery [\d.]+ h, baseline [\d.]+ h of [\d.]+ h sampled\./.test(savingsText)
    && /Savings cover \d+% of the sampled time/.test(savingsText)
    && /Measured over \d+% of the sampled time/.test(savingsText),
    savingsText.slice(0, 240));
  check("the savings cards state the scope of each figure",
    /metered intervals, before export revenue/.test(savingsText)
    && /measured discharge ÷ pack capacity/.test(savingsText)
    && /baseline minus actual \(net\)/.test(savingsText),
    savingsText.slice(0, 240));
  check("the time-coverage figure is formatted as hours", A.fmtHours(0.5) === "0.50 h"
    && A.fmtHours(12) === "12.0 h" && A.fmtHours(null) === "--");
  const coverage = A.savingsCoverage([
    { has_data: false, baseline_cost_eur: null },
    { has_data: true, baseline_cost_eur: null },
    { has_data: true, baseline_cost_eur: 1.2 }
  ]);
  check("savings coverage counts data-bearing buckets only, never empty periods",
    coverage.withData.length === 2 && coverage.covered.length === 1,
    JSON.stringify({ withData: coverage.withData.length, covered: coverage.covered.length }));
  const savingsCells = view().querySelectorAll("td").map((node) => node.textContent);
  check("an unmeasured bucket reports its cycle energy as unknown, not planned",
    savingsCells.indexOf("-- (unmeasured)") >= 0);
  check("a measured bucket renders its charged and discharged energy",
    /Charged \(measured\)[\s\S]*?kWh/.test(savingsText)
    && /Discharged \(measured\)[\s\S]*?kWh/.test(savingsText),
    savingsText.slice(0, 200));

  /* ---------- 2. settings structure ---------- */
  console.log("\n2. settings IA (every key present, groups, folding)");
  await goto("/settings");
  const cfg = await M.handle("GET", "/api/v1/config");
  const settings = cfg.settings || [];
  const html = view().outerHTML;
  // The settings page opens on the four setup cards; the rail and the groups live inside the "All
  // settings" drawer, so the structural checks unfold it first.
  const drawer = view().querySelector(".all-settings");
  check("the all-settings drawer holds the full form", !!drawer, "no .all-settings");
  if (drawer) { drawer.open = true; await sleep(20); }

  const railOf = () => (byClass(view(), "settings-rail")[0] || view()).querySelectorAll("button");
  const value = (key) => { const row = settings.find((r) => r.key === key); return row ? row.value : null; };
  const selection = {
    priceProvider: value("price.provider"),
    priceContract: value("price.contract"),
    solarSource: value("solar.source"),
  };
  const canAsk = typeof A.settingsVisible === "function";
  check("settingsVisible is exported for offline testing", canAsk);
  const isVisible = (row) => (canAsk
    ? A.settingsVisible(row, Object.assign({ showAdvanced: true, showAll: true, query: "" }, selection))
    : true);

  // A key is reachable when selecting any group renders it. Hidden-because-inapplicable keys are
  // still required when they hold a non-default value (the muted "not used by the current selection"
  // line), which is what "nothing is invisible" means for this design.
  const expected = settings.filter((row) => isVisible(row) || row.value !== row.default);
  const deferred = settings.filter((row) => !expected.includes(row));

  const railButtons = railOf();
  const groupNames = [...new Set(settings.map((r) => r.group))];
  check(`settings rail offers a control per group`, railButtons.length >= groupNames.length,
    `${railButtons.length} buttons for ${groupNames.length} groups`);
  const railText = (byClass(view(), "settings-rail")[0] || view()).textContent;
  const missingGroups = groupNames.filter((g) => !railText.includes(g));
  check(`settings rail lists every group (${groupNames.length})`, missingGroups.length === 0, missingGroups.join(", "));

  async function walk(labelsOnly) {
    const found = new Set();
    for (const button of railOf()) {
      button.dispatchEvent({ type: "click" });
      await sleep(25);
      const html = view().outerHTML;
      const text = view().textContent;
      expected.forEach((row) => {
        if (html.includes(row.key) || text.includes(row.label)) found.add(row.key);
      });
    }
    return found;
  }

  const reachable = await walk();
  const unreachable = expected.filter((row) => !reachable.has(row.key));
  check(
    `every settings key that applies here is reachable (${expected.length} required, ${deferred.length} inapplicable)`,
    unreachable.length === 0,
    unreachable.length ? `unreachable ${unreachable.length}: ${unreachable.slice(0, 6).map((r) => r.key).join(", ")}` : "",
  );

  const advancedRows = settings.filter((r) => r.advanced && r.group !== "Safety");
  // Keys the setup cards own are the primary surface: they are meant to be visible outside any
  // disclosure, and the groups skip them (exactly once). Folding is asserted for the rest.
  const cardKeys = new Set(typeof A.setupCardKeys === "function" ? A.setupCardKeys(selection) : []);
  const foldedRows = advancedRows.filter((r) => !cardKeys.has(r.key));
  check(`payload classifies ${advancedRows.length} foldable advanced keys`, advancedRows.length > 0);
  check("the setup cards own the primary keys", cardKeys.size >= 6 && cardKeys.has("price.provider") && cardKeys.has("grid.power_entity"), [...cardKeys].join(", "));
  const cardFields = [...byClass(view(), "setup-cards")[0]?.querySelectorAll("[data-key]") || []].map((f) => f.dataset.key);
  check("every card key is rendered on a card", [...cardKeys].every((key) => cardFields.includes(key)), `cards render ${cardFields.join(", ")}`);

  // SPEC 7.1.3: the Solar card owns the merge list. The mock configures two sources, so the editor
  // must render one row per source with its weight input and its status chip.
  const solarCardNode = [...(byClass(view(), "setup-cards")[0]?.querySelectorAll(".setup-card") || [])]
    .find((card) => /How the add-on should know your production/.test(card.textContent)) || null;
  const sourceRows = solarCardNode ? solarCardNode.querySelectorAll(".source-row") : [];
  const sourceWeights = solarCardNode ? solarCardNode.querySelectorAll(".source-weight") : [];
  const sourceStatuses = solarCardNode ? solarCardNode.querySelectorAll(".source-status") : [];
  check("the Solar card renders the merge-source editor", !!solarCardNode && sourceRows.length === 2 && sourceWeights.length === 2,
    `${sourceRows.length} rows, ${sourceWeights.length} weights`);
  check("each merge source shows a weight and a status", sourceStatuses.length === 2
    && [...sourceStatuses].every((n) => /(ok|degraded|failed|disabled|no status)/.test(n.textContent)),
    [...sourceStatuses].map((n) => n.textContent).join(" | "));

  // SPEC 7.2: the Load card owns the named-device editor. The mock configures two devices, so the
  // editor must render one row per device with its kind dropdown, its display name and its meter
  // picker; a row without a meter must be refused client-side, never sent for the server to reject.
  function loadCardNode() {
    const host = byClass(view(), "setup-cards")[0];
    if (!host) return null;
    return [...host.querySelectorAll(".setup-card")].find((card) => /What the house draws/.test(card.textContent)) || null;
  }
  const deviceRows = () => (loadCardNode() ? loadCardNode().querySelectorAll(".device-row") : []);
  const deviceKinds = () => (loadCardNode() ? loadCardNode().querySelectorAll(".device-kind") : []);
  const deviceNames = () => (loadCardNode() ? loadCardNode().querySelectorAll(".device-name") : []);
  check("the Load card renders the named-device editor",
    !!loadCardNode() && deviceRows().length === 2 && deviceKinds().length === 2 && deviceNames().length === 2
      && (loadCardNode().querySelectorAll(".picker").length >= 2),
    `${deviceRows().length} rows, ${deviceKinds().length} kinds, ${deviceNames().length} names`);
  const kindOptions = deviceKinds().length ? [...deviceKinds()[0].querySelectorAll("option")].map((o) => o.value) : [];
  check("the device kind dropdown offers the closed kind set",
    ["heat_pump", "ev_charger", "water_heater", "pool_pump"].every((kind) => kindOptions.includes(kind)),
    kindOptions.join(", "));
  const addDevice = loadCardNode()
    ? [...loadCardNode().querySelectorAll("button")].find((b) => /Add device/.test(b.textContent))
    : null;
  check("the device editor can add a row", !!addDevice);
  if (addDevice) {
    addDevice.dispatchEvent({ type: "click" });
    await sleep(20);
    check("an added device row without a meter is refused before saving",
      deviceRows().length === 3 && /has no meter/.test(loadCardNode().textContent),
      `${deviceRows().length} rows`);
  }
  const deviceProblems = A.loadDeviceProblems([{ entity: "sensor.example_a", kind: "heat_pump" }, { entity: "sensor.example_a", kind: "ev_charger" }], ["heat_pump", "ev_charger"]);
  check("a repeated device meter is refused client-side",
    deviceProblems.length === 1 && /repeats/.test(deviceProblems[0]), deviceProblems.join(" | "));
  check("an unknown device kind is refused client-side",
    /unknown kind/.test(A.loadDeviceProblems([{ entity: "sensor.example_b", kind: "boiler" }], ["heat_pump"])[0] || ""));

  // SPEC 3: the Meter card names the Energy-dashboard fallback and lets a saved override go back
  // to it. The mock serves the dashboard roles and mirrors the engine's grid_resolution().
  function meterCardNode() {
    const host = byClass(view(), "setup-cards")[0];
    if (!host) return null;
    return [...host.querySelectorAll(".setup-card")].find((card) => /The live power the add-on follows/.test(card.textContent)) || null;
  }
  const meterText = () => (meterCardNode() ? meterCardNode().textContent : "");
  check(
    "the Meter card shows the Energy-dashboard fallback",
    /From the Home Assistant Energy dashboard: signed power sensor\.example_net_power/.test(meterText()),
    meterText().slice(0, 180),
  );

  // The pickers must really work against /discover: Find lists candidates, Use marks the key dirty.
  const picker = byClass(view(), "picker")[0];
  check("the meter/solar cards render searchable pickers", byClass(view(), "picker").length >= 2, `${byClass(view(), "picker").length} pickers`);
  if (picker) {
    const find = picker.querySelector("button");
    find.dispatchEvent({ type: "click" });
    await sleep(260); // the mock answers after a 150 ms simulated latency
    const rows = picker.querySelectorAll(".picker-row");
    check("a picker search lists discovery candidates", rows.length > 0, `${rows.length} rows`);
    const use = rows.length ? rows[rows.length - 1].querySelector("button") : null;
    if (use) {
      use.dispatchEvent({ type: "click" });
      await sleep(40);
      const note = view().querySelector(".save-bar .muted");
      check("using a candidate marks the setting dirty", !!note && /unsaved change/.test(note.textContent), note ? note.textContent : "no save note");
      // The candidate made a grid.* key dirty: the card must switch to the override branch, name
      // the fallback it replaces, and offer to go back to it (SPEC 3).
      check(
        "a dirty grid value is labelled as an override",
        /overrides the Home Assistant Energy dashboard/.test(meterText()) && /The Energy dashboard has signed power sensor\.example_net_power/.test(meterText()),
        meterText().slice(0, 220),
      );
      const useDashboard = meterCardNode() ? [...meterCardNode().querySelectorAll("button")].find((b) => /Use the Energy dashboard/.test(b.textContent)) : null;
      check("the override offers a way back to the Energy dashboard", !!useDashboard);
      if (useDashboard) {
        useDashboard.dispatchEvent({ type: "click" });
        await sleep(40);
        check("clearing the override restores the fallback text", /From the Home Assistant Energy dashboard/.test(meterText()), meterText().slice(0, 180));
        const cleared = view().querySelector(".save-bar .muted");
        check("clearing the override is an unsaved change", !!cleared && /unsaved change/.test(cleared.textContent), cleared ? cleared.textContent : "no save note");
      }
    }
  }
  check("advanced rows sit in a disclosure container", byClass(view(), "disclosure").length > 0 || view().querySelectorAll("details").length > 0);

  // distinct advanced keys visible per state, walking every group
  async function advancedVisible() {
    const found = new Set();
    for (const button of railOf()) {
      button.dispatchEvent({ type: "click" });
      await sleep(25);
      shownLabels(view(), foldedRows).forEach((key) => found.add(key));
    }
    return found;
  }

  const rowByKey = {};
  settings.forEach((row) => { rowByKey[row.key] = row; });
  // Does the row apply to the currently selected price/solar sources at all? (the visibility table)
  const applicable = (row) => (canAsk
    ? A.settingsVisible(
        Object.assign({}, row, { value: row.default }),   // ignore the "hidden but set" rule
        Object.assign({ showAdvanced: true, showAll: true, query: "" }, selection))
    : true);
  const notApplicableButSet = (row) => !applicable(row) && row.value !== row.default;
  const expectedWithShowAll = foldedRows.filter((row) => applicable(row));

  const folded = await advancedVisible();
  const wronglyShown = [...folded].filter((key) => applicable(rowByKey[key]));
  check("advanced rows are folded by default", wronglyShown.length === 0,
    `shown by default: ${[...folded].map((k) => k + (applicable(rowByKey[k]) ? "" : "(n/a)")).slice(0, 6).join(", ")} -> wrongly shown: ${wronglyShown.join(", ")}`);

  // unfolding: the folded rows must really live inside the disclosure container, since opening it
  // is what reveals them (a click on <summary> is native browser behaviour, not app logic)
  const disclosure = view().querySelector(".settings-group .disclosure") || byClass(view(), "disclosure")[0];
  if (disclosure) {
    const inside = [[...shownLabels(disclosure, foldedRows)].length, disclosure.querySelectorAll(".field").length];
    disclosure.open = true;
    await sleep(20);
    const revealed = [...shownLabels(view(), foldedRows)].length;
    check("opening the disclosure reveals its advanced rows", revealed > 0,
      `fields inside the disclosure: ${inside[1]}, shown after opening: ${revealed}`);
  } else {
    check("folded rows sit in an openable disclosure", false, "no .disclosure / <details> found");
  }

  const showAllSwitch = () => dom.document.getElementById("settings-show-advanced") ||
    (byClass(view(), "switch")[0] && byClass(view(), "switch")[0].querySelector("input"));
  let showAll = showAllSwitch();
  if (showAll) {
    const box = showAllSwitch();
    box.checked = true;
    box.dispatchEvent({ type: "change" });
    await sleep(30);
    const revealed = await advancedVisible();
    const missingShown = expectedWithShowAll.filter((row) => !revealed.has(row.key));
    check("Show all advanced reveals every key that applies", missingShown.length === 0,
      `showed ${revealed.size}, expected ${expectedWithShowAll.length}; missing ${missingShown.slice(0, 5).map((r) => r.key).join(", ")}`);
  } else {
    check("Show all advanced control exists", false, "no #settings-show-advanced and no .switch input");
  }

  // safety keys are never folded: visible with the switch off and folded advanced elsewhere
  showAll = showAllSwitch();
  if (showAll) {
    showAll.checked = false;
    showAll.dispatchEvent({ type: "change" });
    await sleep(30);
  }
  const safety = settings.filter((r) => r.group === "Safety");
  const safetySeen = new Set();
  for (const button of railOf()) {
    button.dispatchEvent({ type: "click" });
    await sleep(25);
    const text = view().textContent;
    safety.forEach((row) => { if (text.includes(row.label)) safetySeen.add(row.key); });
  }
  check(`safety group (${safety.length} keys) is never folded`, safetySeen.size === safety.length,
    `${safetySeen.size} of ${safety.length} visible`);

  check("save bar exists", byClass(view(), "save-bar").length > 0);

  /* ---------- 3. style integrity ---------- */
  console.log("\n3. style integrity");
  const styleCss = fs.readFileSync(path.join(WEB, "style.css"), "utf8");
  // The chart surface ships in its own sheet because the Lovelace card bundle inlines the same file.
  const chartCssPath = path.join(WEB, "chart-theme.css");
  const chartCss = fs.existsSync(chartCssPath) ? fs.readFileSync(chartCssPath, "utf8") : "";
  const bootstrapPath = path.join(WEB, "vendor", "bootstrap.min.css");
  const hasBootstrap = fs.existsSync(bootstrapPath);
  const bootstrapCss = hasBootstrap ? fs.readFileSync(bootstrapPath, "utf8") : "";
  check("bootstrap.min.css is vendored", hasBootstrap);
  const defined = new Set();
  [styleCss, chartCss, bootstrapCss].forEach((css) => {
    const re = /\.(-?[_a-zA-Z][\w-]*)/g;
    let m;
    while ((m = re.exec(css))) defined.add(m[1]);
  });
  const emitted = classTokens(fs.readFileSync(path.join(WEB, "app.js"), "utf8"));
  dom.walk(dom.body, (node) => {
    if (node.className) node.className.split(/\s+/).filter(Boolean).forEach((c) => emitted.add(c));
  });
  const undefinedClasses = [...emitted].filter((c) => !defined.has(c)).sort();
  check("every emitted class is defined in style.css, chart-theme.css or bootstrap.min.css", undefinedClasses.length === 0, undefinedClasses.slice(0, 12).join(", "));

  const chartVars = ["--chart-price", "--chart-sell", "--chart-pv", "--chart-load", "--chart-soc", "--chart-grid"];
  const lostVars = chartVars.filter((v) => !styleCss.includes(v));
  check("--chart-* variables survive the restyle", lostVars.length === 0, lostVars.join(", "));

  const inlineStyles = (html.match(/ style="/g) || []).length;
  check("no inline style attributes in rendered output", inlineStyles === 0, `${inlineStyles} found`);

  // In auto mode the attribute is deliberately absent so prefers-color-scheme applies; clicking the
  // control must pin an explicit theme.
  const themeBefore = dom.documentElement.getAttribute("data-bs-theme");
  const toggleBtn = dom.document.getElementById("theme-toggle");
  check("theme control exists in the shell", !!toggleBtn);
  if (toggleBtn) {
    toggleBtn.dispatchEvent({ type: "click" });
    await sleep(20);
    const themeAfter = dom.documentElement.getAttribute("data-bs-theme");
    check("theme control pins an explicit theme", themeAfter === "dark" || themeAfter === "light",
      `before=${themeBefore} after=${themeAfter}`);
    check("theme choice is persisted", [...storage.keys()].some((k) => /theme/i.test(k)), [...storage.keys()].join(","));
  }

  /* ---------- 4. chart structure ---------- */
  // The reported defect: uPlot's legend table shared the plot's fixed pixel height, so it overflowed
  // and painted over the caption. The stub below is the minimum uPlot surface charts.js uses, which
  // lets the harness assert the layout contract (plot, then legend, then caption, as siblings).
  console.log("\n4. charts keep the legend out of the plot box");
  if (!globalThis.uPlot) {
    globalThis.uPlot = function (opts, data, target) {
      this.opts = opts;
      this.data = data;
      this.root = target;
      const plot = document.createElement("div");
      plot.className = "uplot";
      target.appendChild(plot);
      if (opts.legend && opts.legend.show && typeof opts.legend.mount === "function") {
        const table = document.createElement("table");
        table.className = "u-legend";
        const tbody = document.createElement("tbody");
        (opts.series || []).slice(1).forEach((series) => {
          const tr = document.createElement("tr");
          const th = document.createElement("th");
          th.textContent = series.label || "";
          tr.appendChild(th);
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        opts.legend.mount(this, table);
      }
      this.destroy = () => { target.textContent = ""; };
      this.setSize = () => {};
    };
    globalThis.uPlot.paths = { bars: () => () => null };
    globalThis.ResizeObserver = undefined;
  }
  const planFixture = await M.handle("GET", "/api/v1/plan?hours=36");
  const chartHost = document.createElement("div");
  chartHost.className = "chart chart-md";
  view().appendChild(chartHost);
  A && globalThis.SEMCharts.plan(chartHost, planFixture, { wear: 0.05, efficiency: null });
  const plotBoxes = [...chartHost.querySelectorAll(".chart-plot")];
  const legendBoxes = [...chartHost.querySelectorAll(".chart-legend")];
  const captions = [...chartHost.querySelectorAll(".chart-caption")];
  check("every chart has its own plot box", plotBoxes.length === 2, `plot boxes ${plotBoxes.length}`);
  check("the legend is mounted outside the plot box", legendBoxes.length === 2
    && legendBoxes.every((box) => box.querySelector(".u-legend"))
    && legendBoxes.every((box) => !box.closest(".chart-plot")),
    `legend boxes ${legendBoxes.length}`);
  check("the legend follows its plot, in that order", legendBoxes.every((box) => {
    const prev = box.previousElementSibling;
    return prev && prev.classList.contains("chart-plot");
  }), "legend is not a sibling right after the plot");
  check("each chart carries a one-line caption", captions.length >= 2
    && captions.every((c) => c.textContent.trim().length > 10 && c.textContent.length < 240),
    captions.map((c) => c.textContent.length).join(","));
  check("the plot box keeps an explicit height", plotBoxes.every((box) => /height/.test(box.getAttribute("style") || "")),
    plotBoxes.map((b) => b.getAttribute("style")).join(" | "));
  chartHost.remove();

  /* ---------- 5. shell ---------- */
  console.log("\n5. shell (index.html)");
  const index = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const refs = [...index.matchAll(/(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
  check("no external URLs in index.html", refs.every((r) => !r.includes("://")), refs.filter((r) => r.includes("://")).join(", "));
  const bootIdx = refs.indexOf("vendor/bootstrap.min.css");
  const styleIdx = refs.indexOf("style.css");
  check("bootstrap is loaded before style.css", bootIdx >= 0 && styleIdx > bootIdx, `bootstrap=${bootIdx} style=${styleIdx}`);
  check("the shared chart sheet is loaded", refs.indexOf("chart-theme.css") > styleIdx, refs.join(", "));
  check("index.html still declares data-bs-theme", /data-bs-theme="(dark|light)"/.test(index));

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log("failures:\n  - " + failures.join("\n  - "));
  }
  // The app keeps timers alive (refresh + polling), so a CLI check must exit on its own.
  process.exit(failures.length ? 1 : 0);
})();
