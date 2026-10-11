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
  const routes = ["/dashboard", "/plan", "/forecasts", "/batteries", "/savings", "/settings", "/log"];
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
  const bootstrapPath = path.join(WEB, "vendor", "bootstrap.min.css");
  const hasBootstrap = fs.existsSync(bootstrapPath);
  const bootstrapCss = hasBootstrap ? fs.readFileSync(bootstrapPath, "utf8") : "";
  check("bootstrap.min.css is vendored", hasBootstrap);
  const defined = new Set();
  [styleCss, bootstrapCss].forEach((css) => {
    const re = /\.(-?[_a-zA-Z][\w-]*)/g;
    let m;
    while ((m = re.exec(css))) defined.add(m[1]);
  });
  const emitted = classTokens(fs.readFileSync(path.join(WEB, "app.js"), "utf8"));
  dom.walk(dom.body, (node) => {
    if (node.className) node.className.split(/\s+/).filter(Boolean).forEach((c) => emitted.add(c));
  });
  const undefinedClasses = [...emitted].filter((c) => !defined.has(c)).sort();
  check("every emitted class is defined in style.css or bootstrap.min.css", undefinedClasses.length === 0, undefinedClasses.slice(0, 12).join(", "));

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

  /* ---------- 5. shell ---------- */
  console.log("\n5. shell (index.html)");
  const index = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const refs = [...index.matchAll(/(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
  check("no external URLs in index.html", refs.every((r) => !r.includes("://")), refs.filter((r) => r.includes("://")).join(", "));
  const bootIdx = refs.indexOf("vendor/bootstrap.min.css");
  const styleIdx = refs.indexOf("style.css");
  check("bootstrap is loaded before style.css", bootIdx >= 0 && styleIdx > bootIdx, `bootstrap=${bootIdx} style=${styleIdx}`);
  check("index.html still declares data-bs-theme", /data-bs-theme="(dark|light)"/.test(index));

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log("failures:\n  - " + failures.join("\n  - "));
  }
  // The app keeps timers alive (refresh + polling), so a CLI check must exit on its own.
  process.exit(failures.length ? 1 : 0);
})();
