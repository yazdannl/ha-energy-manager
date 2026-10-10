/* app.js — Smart Energy Manager SPA (no build step, no framework, no runtime CDN).
 *
 * Layers: state, hash router, fetch layer, view renderers, write helpers.
 * The API contract is SPEC §10; every view must render from mock.js with ?mock=1 and no network.
 * Relative URLs only: the app is served under an ingress sub-path, so the API base is derived
 * from location.pathname (never a bare "/api/...").
 *
 * Loadable in Node for static tests: it exports root.SEM_APP and skips boot() when there is no DOM.
 */
(function (root) {
  "use strict";

  var VIEW_DEFAULT = "/dashboard";
  var UNIT_EUR = "EUR";

  var MOCK = (function () {
    try {
      if (!root.location) return false;
      var search = String(root.location.search || "").replace(/^\?/, "");
      return search.split("&").some(function (pair) { return pair.split("=")[0] === "mock" && pair.split("=")[1] === "1"; });
    } catch (err) {
      return false;
    }
  })();

  /* ------------------------------------------------------------------ DOM helpers */

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") node.textContent = String(value);
        else if (key === "class") node.className = value;
        else if (key === "for") node.htmlFor = value;
        else if (key === "on") Object.keys(value).forEach(function (evt) { node.addEventListener(evt, value[evt]); });
        else if (key === "value") node.value = value;
        else if (key === "checked" || key === "disabled" || key === "hidden" || key === "selected" || key === "open") {
          if (value) node[key] = true;
        } else if (key === "dataset") Object.keys(value).forEach(function (dk) { node.dataset[dk] = String(value[dk]); });
        else node.setAttribute(key, String(value));
      });
    }
    append(node, kids);
    return node;
  }

  function append(node, kids) {
    if (kids === null || kids === undefined) return;
    (Array.isArray(kids) ? kids : [kids]).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === "object" && kid.nodeType ? kid : document.createTextNode(String(kid)));
    });
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function $(selector, scope) { return (scope || document).querySelector(selector); }

  function button(label, cls, onClick) {
    var b = el("button", { type: "button", class: "btn" + (cls ? " " + cls : ""), text: label });
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }

  function badge(text, kind) {
    return el("span", { class: "badge" + (kind ? " badge-" + kind : ""), text: text });
  }

  function table(caption, headers, rows, rowAttrs) {
    var head = el("tr", null, headers.map(function (h) { return el("th", { scope: "col", text: h }); }));
    var body = el("tbody", null, rows.map(function (cells, r) {
      var row = el("tr", rowAttrs && rowAttrs[r] ? rowAttrs[r] : null);
      cells.forEach(function (cell, i) {
        var td = el("td", i > 0 ? { class: "num" } : null);
        append(td, cell);
        row.appendChild(td);
      });
      return row;
    }));
    var node = el("table", null, [caption ? el("caption", { text: caption }) : null, el("thead", null, [head]), body]);
    return el("div", { class: "table-wrap" }, [node]);
  }

  function withUnit(text, unit) {
    return el("span", null, [String(text), " ", el("span", { class: "unit", text: unit })]);
  }

  function statCard(label, value, sub, stale) {
    return el("div", { class: "card" + (stale ? " card-stale" : "") }, [
      el("p", { class: "card-label", text: label }),
      el("p", { class: "card-value" }, value),
      sub ? el("p", { class: "card-sub" }, sub) : null
    ]);
  }

  function staleBadge(isStale) {
    return isStale ? badge("stale", "stale") : null;
  }

  /* ------------------------------------------------------------------ formatting (every number has a unit) */

  function isNum(value) { return typeof value === "number" && isFinite(value); }

  function fmtPower(watts) {
    if (!isNum(watts)) return "--";
    return Math.abs(watts) >= 1000 ? (watts / 1000).toFixed(2) + " kW" : Math.round(watts) + " W";
  }

  function fmtKwh(kwh, digits) {
    if (!isNum(kwh)) return "--";
    return kwh.toFixed(digits === undefined ? 2 : digits) + " kWh";
  }

  function fmtPrice(value) {
    if (!isNum(value)) return "--";
    return value.toFixed(4) + " " + UNIT_EUR + "/kWh";
  }

  function fmtEur(value, digits) {
    if (!isNum(value)) return "--";
    return value.toFixed(digits === undefined ? 2 : digits) + " " + UNIT_EUR;
  }

  function fmtPct(value, digits) {
    if (!isNum(value)) return "--";
    return value.toFixed(digits === undefined ? 1 : digits) + " %";
  }

  function parseDate(value) {
    if (!value) return null;
    var d = value instanceof Date ? value : new Date(value);
    return isFinite(d.getTime()) ? d : null;
  }

  function fmtTime(value) {
    var d = parseDate(value);
    if (!d) return "--";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function fmtDateTime(value) {
    var d = parseDate(value);
    if (!d) return "--";
    return d.toLocaleDateString([], { day: "2-digit", month: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function fmtDuration(seconds) {
    if (!isNum(seconds)) return "--";
    var s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + " s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " min " + (s % 60) + " s";
    var h = Math.floor(m / 60);
    return h + " h " + (m % 60) + " min";
  }

  function timeAgo(value) {
    var d = parseDate(value);
    if (!d) return "unknown time";
    var delta = (Date.now() - d.getTime()) / 1000;
    if (delta < 0) return "in " + fmtDuration(-delta);
    return fmtDuration(delta) + " ago";
  }

  function fmtWindow(pair) {
    if (!pair || pair.length < 2) return null;
    return fmtTime(pair[0]) + " – " + fmtTime(pair[1]) + " (" + timeAgo(pair[0]) + ")";
  }

  /* ------------------------------------------------------------------ reason/action labels */

  var REASONS = {
    arbitrage_charge: "Arbitrage charge",
    pv_surplus_charge: "PV surplus charge",
    peak_discharge: "Peak discharge",
    self_consumption: "Self consumption",
    reserve_hold: "Reserve hold",
    grid_limit_hold: "Grid limit hold",
    spread_below_threshold: "below the spread floor you set",
    efficiency_loss: "idle: sale price does not cover the round-trip loss",
    terminal_target: "Terminal target",
    battery_full: "Battery full",
    battery_empty: "Battery empty",
    no_forecast_slots: "No forecast slots",
    stale_input: "Stale input",
    mode_not_auto: "Blocked: mode is not auto",
    dry_run: "Dry run (no write)",
    override_active: "Blocked: manual override active",
    stale_state: "Blocked: state too old",
    ha_disconnected: "Blocked: Home Assistant disconnected",
    battery_unavailable: "Blocked: battery unavailable",
    soc_floor: "Blocked: SoC floor",
    soc_ceiling: "Blocked: SoC ceiling",
    power_clamp: "Power clamped to limits",
    grid_import_limit: "Blocked: grid import limit",
    grid_export_limit: "Blocked: grid export limit",
    ramp_limit: "Blocked: ramp limit",
    driver_error: "Driver error",
    idempotent_skip: "Skipped: already at target",
    ok: "OK"
  };

  var ACTIONS = {
    hold: "Hold",
    charge: "Charge",
    discharge: "Discharge",
    pv_charge: "PV charge",
    export: "Export",
    self_consumption: "Self consumption",
    reserve: "Reserve"
  };

  function slugText(slug, map) {
    if (!slug) return "unknown";
    return map[slug] || String(slug).replace(/_/g, " ");
  }
  function reasonText(slug) { return slugText(slug, REASONS); }
  function actionText(slug) { return slugText(slug, ACTIONS); }

  /* ------------------------------------------------------------------ API client (ingress-safe relative URLs) */

  function basePath() {
    var path = root.location && root.location.pathname ? root.location.pathname : "/";
    if (path.charAt(path.length - 1) === "/") return path;
    var cut = path.lastIndexOf("/");
    return cut >= 0 ? path.slice(0, cut + 1) : "/";
  }

  function queryString(params) {
    var keys = Object.keys(params || {});
    if (!keys.length) return "";
    return "?" + keys.map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
  }

  function apiPath(path, params) {
    return basePath() + "api/v1" + path + queryString(params);
  }

  function api(method, path, options) {
    options = options || {};
    var full = apiPath(path, options.params);
    if (MOCK) {
      if (root.SEM_MOCK && typeof root.SEM_MOCK.handle === "function") {
        return root.SEM_MOCK.handle(method, full, options.body);
      }
      return Promise.reject(new Error("?mock=1 but mock.js did not load."));
    }
    var init = { method: method, cache: "no-store", headers: { Accept: "application/json" } };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return root.fetch(full, init).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); }
          catch (err) {
            var bad = new Error("Server returned invalid JSON for " + path);
            bad.code = "invalid_json";
            throw bad;
          }
        }
        if (!response.ok) {
          var detail = data && data.error ? data.error : {};
          var error = new Error(detail.message || "HTTP " + response.status);
          error.code = detail.code || String(response.status);
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function get(path, params) { return api("GET", path, { params: params }); }
  function post(path, body) { return api("POST", path, { body: body || {} }); }
  function patch(path, body) { return api("PATCH", path, { body: body || {} }); }
  function del(path) { return api("DELETE", path); }

  /* ------------------------------------------------------------------ shared state, panels, notes */

  var state = { status: null, health: null, viewToken: 0, cleanup: null };

  function setNote(container, text, isError) {
    var live = document.getElementById("live");
    if (live) live.textContent = text;
    if (!container || !container.appendChild) return;
    var note = container.querySelector("[data-note]");
    if (!note) {
      note = el("p", { dataset: { note: "1" } });
      container.appendChild(note);
    }
    note.className = isError ? "field-error" : "muted";
    note.textContent = text;
  }

  function panel(host, title, actions) {
    var head = el("div", { class: "panel-head" }, [
      el("h2", { text: title }),
      actions && actions.length ? el("div", { class: "panel-actions" }, actions) : null
    ]);
    var body = el("div", { class: "panel-body" });
    var section = el("section", { class: "panel" }, [head, body]);
    host.appendChild(section);
    return { section: section, body: body, head: head };
  }

  function panelStatus(body, kind, message, onRetry) {
    clear(body);
    body.appendChild(el("p", { class: kind === "error" ? "panel-error" : "panel-loading", text: message }));
    if (onRetry) body.appendChild(button("Retry", "", onRetry));
  }

  function asyncPanel(host, title, options) {
    var actions = options.actions || [];
    var p = panel(host, title, actions);
    var token = state.viewToken;
    function run() {
      p.section.setAttribute("data-state", "loading");
      panelStatus(p.body, "loading", options.loadingText || "Loading…");
      options.load().then(function (data) {
        if (token !== state.viewToken || !p.body.isConnected) return;
        p.section.removeAttribute("data-state");
        clear(p.body);
        try { options.render(p.body, data); }
        catch (err) { panelStatus(p.body, "error", "Render error: " + err.message, run); }
      }).catch(function (err) {
        if (token !== state.viewToken || !p.body.isConnected) return;
        p.section.removeAttribute("data-state");
        panelStatus(p.body, "error", "Could not load " + title.toLowerCase() + ": " + (err && err.message ? err.message : err), run);
      });
    }
    run();
    return p;
  }

  /* ------------------------------------------------------------------ confirm dialog + write helpers */

  function anyDialogOpen() {
    var dlg = document.getElementById("confirm-dialog");
    return !!(dlg && dlg.open);
  }

  function confirmAction(options) {
    return new Promise(function (resolve) {
      var dlg = document.getElementById("confirm-dialog");
      if (!dlg || typeof dlg.showModal !== "function") {
        resolve(root.confirm ? root.confirm(options.title + "\n\n" + options.body) : false);
        return;
      }
      var form = document.getElementById("confirm-form");
      var checkWrap = document.getElementById("confirm-check-wrap");
      var check = document.getElementById("confirm-check");
      var checkLabel = document.getElementById("confirm-check-label");
      var hint = document.getElementById("confirm-hint");
      var okBtn = document.getElementById("confirm-ok");
      var needCheck = !!options.requireCheck;

      document.getElementById("confirm-title").textContent = options.title;
      document.getElementById("confirm-body").textContent = options.body;
      okBtn.textContent = options.confirmLabel || "Confirm";
      okBtn.className = "btn " + (options.danger === false ? "btn-primary" : "btn-danger");
      checkWrap.hidden = !needCheck;
      hint.hidden = true;
      check.checked = false;
      if (needCheck) checkLabel.textContent = options.checkLabel || "I understand";

      function onSubmit(ev) {
        var submitter = ev.submitter || document.activeElement;
        var submitting = submitter && submitter.value === "ok";
        if (submitting && needCheck && !check.checked) {
          ev.preventDefault();
          hint.hidden = false;
          check.focus();
        }
      }
      function onClose() {
        form.removeEventListener("submit", onSubmit);
        dlg.removeEventListener("close", onClose);
        resolve(dlg.returnValue === "ok");
      }
      dlg.returnValue = "";
      form.addEventListener("submit", onSubmit);
      dlg.addEventListener("close", onClose);
      if (dlg.open) dlg.close("cancel");
      dlg.showModal();
      var cancel = document.getElementById("confirm-cancel");
      if (cancel) cancel.focus();
    });
  }

  function once(factory) {
    var promise = null;
    return function () { if (!promise) promise = factory(); return promise; };
  }

  function performWrite(trigger, target, requestFn, successText, onDone) {
    trigger.disabled = true;
    trigger.setAttribute("aria-busy", "true");
    if (trigger.classList) trigger.classList.add("pending");
    setNote(target, "Pending… (waiting for the server)", false);
    requestFn().then(function (result) {
      if (trigger.classList) trigger.classList.remove("pending");
      trigger.removeAttribute("aria-busy");
      trigger.disabled = false;
      setNote(target, successText ? successText(result) : "Confirmed by the server.", false);
      if (onDone) onDone(result);
    }).catch(function (err) {
      if (trigger.classList) trigger.classList.remove("pending");
      trigger.removeAttribute("aria-busy");
      trigger.disabled = false;
      setNote(target, "Error: " + (err && err.message ? err.message : String(err)), true);
    });
  }

  function writeAction(options) {
    var btn = options.button;
    var target = options.noteContainer || btn.parentNode;
    btn.addEventListener("click", function () {
      if (!options.confirm) {
        performWrite(btn, target, options.request, options.successText, options.onDone);
        return;
      }
      confirmAction(options.confirm).then(function (ok) {
        if (ok) performWrite(btn, target, options.request, options.successText, options.onDone);
      });
    });
    return btn;
  }

  /* ------------------------------------------------------------------ router */

  var VIEWS = {
    "/dashboard": viewDashboard,
    "/plan": viewPlan,
    "/forecasts": viewForecasts,
    "/batteries": viewBatteries,
    "/savings": viewSavings,
    "/settings": viewSettings,
    "/log": viewLog
  };

  function normalizeRoute(path) {
    var p = path && path.charAt(0) === "/" ? path : "/" + String(path || "");
    return Object.prototype.hasOwnProperty.call(VIEWS, p) ? p : VIEW_DEFAULT;
  }

  function splitHash(hash) {
    var raw = String(hash || "").replace(/^#/, "");
    var cut = raw.indexOf("?");
    var path = cut >= 0 ? raw.slice(0, cut) : raw;
    var query = {};
    if (cut >= 0) {
      raw.slice(cut + 1).split("&").forEach(function (pair) {
        if (!pair) return;
        var bits = pair.split("=");
        query[decodeURIComponent(bits[0])] = decodeURIComponent(bits.slice(1).join("=") || "");
      });
    }
    return { route: normalizeRoute(path), query: query };
  }

  function setNav(route) {
    Array.prototype.forEach.call(document.querySelectorAll("#nav a"), function (link) {
      var href = link.getAttribute("href") || "";
      if (href === "#" + route) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function renderRoute() {
    var parsed = splitHash(root.location ? root.location.hash : "");
    var host = document.getElementById("view");
    if (!host) return;
    state.viewToken++;
    if (state.cleanup) {
      try { state.cleanup(); } catch (err) { /* cleanup is best effort */ }
      state.cleanup = null;
    }
    if (root.SEMCharts && root.SEMCharts.destroyTree) root.SEMCharts.destroyTree(host);
    clear(host);
    setNav(parsed.route);
    state.cleanup = VIEWS[parsed.route](host, parsed.query) || null;
    if (root.scrollTo) root.scrollTo(0, 0);
  }

  function safeRefresh() {
    if (document.hidden || anyDialogOpen()) return;
    renderRoute();
  }

  function refreshHeader() {
    var chip = document.getElementById("conn-status");
    var build = document.getElementById("build-info");
    get("/status").then(function (status) {
      state.status = status;
      if (chip) {
        chip.className = "conn " + (status.ha_connected ? "ok" : "warn");
        chip.textContent = (status.ha_connected ? "Home Assistant connected" : "Home Assistant unreachable") +
          " · " + status.mode + (status.dry_run ? " · dry run" : "");
      }
      if (build) build.textContent = "v" + (status.version || "?") + (status.commit ? " · " + status.commit : "");
    }).catch(function (err) {
      if (chip) {
        chip.className = "conn err";
        chip.textContent = "API unreachable: " + (err && err.message ? err.message : err);
      }
    });
  }

  /* ------------------------------------------------------------------ theme override */

  // auto -> dark -> light; persisted, and auto removes the attribute so prefers-color-scheme wins.
  function themeMode() {
    var mode = readStored("sem.theme", "auto");
    return mode === "dark" || mode === "light" ? mode : "auto";
  }

  function applyTheme(mode) {
    var rootEl = document.documentElement;
    if (!rootEl) return;
    if (mode === "auto") delete rootEl.dataset.bsTheme;
    else rootEl.dataset.bsTheme = mode;
  }

  function wireThemeToggle() {
    applyTheme(themeMode());
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var labels = { auto: "Theme: auto", dark: "Theme: dark", light: "Theme: light" };
    function label() {
      var text = labels[themeMode()];
      // ponytail: index.html ships a .theme-state span; update it instead of wiping the button.
      var span = btn.querySelector ? btn.querySelector(".theme-state") : null;
      if (span) span.textContent = text;
      else btn.textContent = text;
      btn.setAttribute("aria-label", text);
    }
    label();
    btn.addEventListener("click", function () {
      var mode = themeMode();
      var next = mode === "auto" ? "dark" : mode === "dark" ? "light" : "auto";
      writeStored("sem.theme", next);
      applyTheme(next);
      label();
    });
  }

  // The router asks a view's guard before it re-renders, so a cancelled navigation keeps the
  // current DOM (and its unsaved edits) instead of rebuilding it from the API.
  var skipNextHash = false;
  function onHashChange() {
    // ponytail: restoring the old hash would re-render settings and lose the edits the user kept.
    if (skipNextHash) { skipNextHash = false; return; }
    var guard = state.navGuard;
    if (guard && guard.dirty && guard.dirty()) {
      var target = root.location.hash;
      var body = typeof guard.body === "function" ? guard.body() : guard.body;
      confirmAction({ title: guard.title, body: body, confirmLabel: guard.confirmLabel }).then(function (ok) {
        if (ok) { state.navGuard = null; renderRoute(); }
        else { skipNextHash = true; root.location.hash = guard.hash; }
      });
      return;
    }
    renderRoute();
  }

  function boot() {
    wireThemeToggle();
    root.addEventListener("hashchange", onHashChange);
    if (!root.location.hash) root.location.hash = VIEW_DEFAULT;
    else renderRoute();
    refreshHeader();
    root.setInterval(function () { if (!document.hidden && !anyDialogOpen()) refreshHeader(); }, 15000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { refreshHeader(); safeRefresh(); }
    });
  }

  /* ------------------------------------------------------------------ view: dashboard */

  function slotAt(plan, when) {
    if (!plan || !Array.isArray(plan.slots)) return null;
    var now = parseDate(when || new Date());
    if (!now) return null;
    var step = (plan.slot_minutes || 15) * 60000;
    for (var i = 0; i < plan.slots.length; i++) {
      var t = parseDate(plan.slots[i].t);
      if (t && t.getTime() <= now.getTime() && now.getTime() < t.getTime() + step) return plan.slots[i];
    }
    return null;
  }

  function connectionIssues(status) {
    var out = [];
    if (!status.ha_connected) out.push({ level: "error", text: "Home Assistant unreachable" + (status.ha_last_error ? ": " + status.ha_last_error : ".") });
    if (status.scheduler_healthy === false) out.push({ level: "warning", text: "The control scheduler reports itself unhealthy." });
    var tick = parseDate(status.last_tick_at);
    if (!tick) out.push({ level: "warning", text: "No control tick recorded yet." });
    else {
      var age = (Date.now() - tick.getTime()) / 1000;
      if (age > 180) out.push({ level: "warning", text: "Measurements are stale: last control tick " + timeAgo(status.last_tick_at) + "." });
    }
    return out;
  }

  function renderStopResults(body, res, status) {
    var stopped = Array.isArray(res.stopped) ? res.stopped : [];
    var failed = stopped.filter(function (row) { return !row.ok; });
    var box = el("div", { class: "banner banner-" + (failed.length ? "error" : "warn"), dataset: { stopResult: "1" } }, [
      el("p", { class: "banner-title", text: "Stop result — mode is now " + (res.mode || "unknown") }),
      el("p", { text: (res.override_cleared ? "Manual override cleared. " : "") + (status && status.dry_run ? "Dry run was on, but the stop still wrote to the hardware: dry run only suppresses the normal plan dispatcher, it never suppresses a stop." : "The stop was written to the hardware.") })
    ]);
    if (!stopped.length) {
      box.appendChild(el("p", { class: "muted", text: "The server reported no batteries to stop. Check that batteries are configured." }));
    } else {
      if (failed.length) box.appendChild(el("p", { class: "field-error", text: failed.length + " of " + stopped.length + " batteries did NOT confirm the stop. Check them in Home Assistant before trusting the safe state." }));
      box.appendChild(table("Per-battery stop result (server response)", ["Battery", "Result", "Applied power", "Code", "Message", "Dry run"], stopped.map(function (row) {
        return [row.battery_id || "?", row.ok ? badge("stopped", "ok") : badge("FAILED", "err"), fmtPower(row.applied_w), row.code || "—", row.message || "—", row.dry_run ? "yes (still wrote)" : "no"];
      })));
    }
    box.appendChild(button("Dismiss", "", function () { box.remove(); }));
    body.appendChild(box);
  }

  function renderSafetyState(body, status) {
    var lines = [];
    var pending = Array.isArray(status.pending_holds) ? status.pending_holds : [];
    if (pending.length) {
      lines.push("A real setpoint may still be in force for: " + pending.join(", ") + ". The add-on has not confirmed a neutral hold yet — check the device before assuming it is idle.");
    }
    if (status.mode_persisted === false) {
      lines.push("The last mode change was NOT persisted: the running process is in the new mode, but after a restart the add-on would come up in the previous mode. Re-apply the mode or fix the settings file.");
    }
    if (!lines.length) return;
    var box = el("div", { class: "banner banner-warn", dataset: { safetyState: "1" } }, [
      el("p", { class: "banner-title", text: "Safety state" })
    ]);
    var list = el("ul");
    lines.forEach(function (text) { list.appendChild(el("li", { text: text })); });
    box.appendChild(list);
    body.appendChild(box);
  }

  function renderAutopilot(body, status) {
    var toolbar = el("div", { class: "toolbar" });
    var modeButtons = {};
    [["off", "Off"], ["advisory", "Advisory"], ["auto", "Auto"]].forEach(function (m) {
      var active = status.mode === m[0];
      var b = button(m[1], active ? "btn-primary" : "");
      modeButtons[m[0]] = b;
      b.setAttribute("aria-pressed", active ? "true" : "false");
      b.setAttribute("aria-label", "Set mode to " + m[1] + (active ? " (current)" : ""));
      b.addEventListener("click", function () {
        var confirmOptions = m[0] === "auto" ? {
          title: "Enable automatic control?",
          body: "Auto lets the add-on write power setpoints to the batteries. Keep dry run on to log the writes without performing them. There is no device watchdog: switching away from auto later does not neutralise a setpoint that was already written — use Stop now for that.",
          confirmLabel: "Enable auto",
          requireCheck: true,
          checkLabel: "I understand the battery can be controlled"
        } : null;
        writeAction({
          button: b, noteContainer: body, confirm: confirmOptions,
          request: function () { return post("/mode", { mode: m[0], dry_run: status.dry_run }); },
          successText: function (res) { return "Mode is now " + res.mode + " (confirmed by the server)."; },
          onDone: function () { renderRoute(); }
        });
      });
      toolbar.appendChild(b);
    });

    var dry = el("input", { type: "checkbox", id: "dry-run-toggle", checked: status.dry_run });
    dry.addEventListener("change", function () {
      var next = dry.checked;
      function apply() {
        performWrite(dry, body, function () {
          return post("/mode", { mode: status.mode, dry_run: next });
        }, function (res) {
          return "Dry run is now " + (res.dry_run ? "on" : "off") + " (confirmed by the server).";
        }, function () { renderRoute(); });
      }
      if (!next) {
        confirmAction({
          title: "Turn dry run off?",
          body: "With dry run off and mode auto, the add-on performs real writes to your batteries. The device has no watchdog, so a written setpoint stays in force until something else changes it.",
          confirmLabel: "Turn dry run off", requireCheck: true,
          checkLabel: "I understand writes will be real"
        }).then(function (ok) { if (ok) apply(); else dry.checked = true; });
      } else {
        apply();
      }
    });
    toolbar.appendChild(el("label", { class: "muted", for: "dry-run-toggle" }, [dry, " Dry run (the plan dispatcher writes nothing)"]));

    var stop = button("Stop now", "btn-danger");
    stop.addEventListener("click", function () {
      confirmAction({
        title: "Stop now?",
        body: "Sends an immediate 0 W hold to every battery and sets the mode to advisory. This writes to the hardware for real, even when dry run is on.",
        confirmLabel: "Stop now",
        requireCheck: true,
        checkLabel: "I understand this writes to the hardware for real"
      }).then(function (ok) {
        if (!ok) return;
        stop.disabled = true;
        stop.setAttribute("aria-busy", "true");
        stop.classList.add("pending");
        setNote(body, "Pending… sending a real 0 W stop to every battery", false);
        post("/stop").then(function (res) {
          stop.classList.remove("pending");
          stop.removeAttribute("aria-busy");
          stop.disabled = false;
          setNote(body, "Stop confirmed by the server: mode is now " + res.mode + ".", false);
          renderStopResults(body, res, status);
          Object.keys(modeButtons).forEach(function (mode) {
            modeButtons[mode].setAttribute("aria-pressed", mode === res.mode ? "true" : "false");
          });
          refreshHeader();
          get("/status").then(function (fresh) {
            state.status = fresh;
            var resultsBox = body.querySelector("[data-stop-result]");
            if (!resultsBox) return;
            resultsBox.appendChild(el("p", { class: fresh.mode_persisted ? "muted" : "field-error", text: fresh.mode_persisted ? "Mode persisted to the settings file." : "Mode NOT persisted: after a restart the add-on would return to the previous mode." }));
            var pending = Array.isArray(fresh.pending_holds) ? fresh.pending_holds : [];
            if (pending.length) resultsBox.appendChild(el("p", { class: "field-error", text: "Still awaiting a neutral hold: " + pending.join(", ") + " — a real setpoint may still be in force." }));
          }).catch(function () { /* the box already shows the stop response */ });
        }).catch(function (err) {
          stop.classList.remove("pending");
          stop.removeAttribute("aria-busy");
          stop.disabled = false;
          setNote(body, "Error: " + (err && err.message ? err.message : String(err)), true);
        });
      });
    });
    toolbar.appendChild(stop);
    body.appendChild(toolbar);
    renderSafetyState(body, status);

    if (status.override) {
      var until = status.override.expires_at ? fmtTime(status.override.expires_at) : "?",
          left = status.override.expires_at ? timeAgo(status.override.expires_at).replace("in ", "in ") : "unknown";
      var overrideBox = el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Manual override active" }),
        el("p", { text: fmtPower(status.override.power_w) + (status.override.battery_id ? " on " + status.override.battery_id : " on all batteries") + " until " + until + " (" + left + ")." })
      ]);
      var clear = button("Clear override", "btn-danger");
      writeAction({
        button: clear, noteContainer: overrideBox,
        confirm: { title: "Clear override?", body: "Normal plan dispatch resumes.", confirmLabel: "Clear override" },
        request: function () { return del("/override"); },
        successText: function () { return "Override cleared (confirmed by the server)."; },
        onDone: function () { renderRoute(); }
      });
      overrideBox.appendChild(clear);
      body.appendChild(overrideBox);
    }

    connectionIssues(status).forEach(function (issue) {
      body.appendChild(el("div", { class: "banner banner-" + (issue.level === "error" ? "error" : "warn") }, [
        el("p", { class: "banner-title", text: issue.level === "error" ? "Error" : "Warning" }),
        el("p", { text: issue.text })
      ]));
    });
    body.appendChild(el("p", { class: "legend-note", text: "Last plan " + timeAgo(status.last_plan_at) + ", last tick " + timeAgo(status.last_tick_at) + ". The device has no watchdog: advisory/off and dry run stop new writes, but a setpoint written earlier in a live session stays in force — use Stop now to neutralise it (Stop writes to the hardware even in dry run)." }));
  }

  function renderAlerts(body, status) {
    var alerts = Array.isArray(status.alerts) ? status.alerts : [];
    if (!alerts.length) {
      body.appendChild(el("p", { class: "muted", text: "No alerts." }));
      return;
    }
    var list = el("ul", { class: "alert-list" });
    alerts.forEach(function (alert) {
      var level = alert.level || "info";
      list.appendChild(el("li", { dataset: { level: level } }, [
        el("p", { class: "alert-meta" }, [badge(level, level === "warning" ? "warn" : level === "error" ? "err" : "info"), " ", fmtDateTime(alert.ts), " (", timeAgo(alert.ts), ")"]),
        el("p", { class: "wrap-anywhere", text: alert.message || alert.code || "(no message)" })
      ]));
    });
    body.appendChild(list);
  }

  function renderNow(body, data) {
    var status = data.status, plan = data.plan;
    var batteries = Array.isArray(status.batteries) ? status.batteries : [];
    var available = batteries.filter(function (b) { return b.available && isNum(b.soc_pct); });
    var avgSoc = available.length ? available.reduce(function (sum, b) { return sum + b.soc_pct; }, 0) / available.length : null;
    var anyStale = batteries.some(function (b) { return b.stale; });
    var slot = slotAt(plan, status.now || new Date());
    var grid = status.grid_now_w;
    var gridText = isNum(grid) ? (grid > 0 ? "importing" : grid < 0 ? "exporting" : "balanced") : "unknown";
    var rank = status.price_rank_pct;
    var rankText = isNum(rank) ? "rank " + rank.toFixed(0) + " % (" + (rank <= 33 ? "cheapest third" : rank <= 66 ? "middle third" : "most expensive third") + ")" : "rank unknown";

    var cards = el("div", { class: "cards" }, [
      statCard("Battery SoC", withUnit(isNum(avgSoc) ? avgSoc.toFixed(1) : "--", "%"), batteries.length === 1 ? "1 battery reported" : batteries.length + " batteries reported", anyStale),
      statCard("PV power", fmtPower(status.pv_now_w), "production now"),
      statCard("Load power", fmtPower(status.load_now_w), "household consumption now"),
      statCard("Grid power", fmtPower(grid), gridText + " at the meter"),
      statCard("Price now", fmtPrice(status.price_now), rankText, slot ? slot.price_stale : false),
      statCard("Plan action", slot ? actionText(slot.action) : "--", slot ? "because: " + reasonText(slot.reason) + " · target " + fmtPower(slot.power_w) : "no active plan slot"),
      statCard("Autopilot", status.mode + (status.dry_run ? " (dry run)" : ""), "last plan " + timeAgo(status.last_plan_at)),
      statCard("Next cheap window", fmtWindow(status.next_cheap_window) ? withUnit(fmtWindow(status.next_cheap_window).split(" (")[0], "local") : "--", fmtWindow(status.next_cheap_window) || "not known"),
      statCard("Next expensive window", fmtWindow(status.next_expensive_window) ? withUnit(fmtWindow(status.next_expensive_window).split(" (")[0], "local") : "--", fmtWindow(status.next_expensive_window) || "not known")
    ]);
    body.appendChild(cards);
  }

  function renderBatteryCards(body, status) {
    var batteries = Array.isArray(status.batteries) ? status.batteries : [];
    if (!batteries.length) {
      body.appendChild(el("p", { class: "muted", text: "No batteries configured. Add one in the Batteries view." }));
      return;
    }
    var grid = el("div", { class: "cards" });
    batteries.forEach(function (battery) {
      var name = (battery.extra && battery.extra.name) || battery.id;
      var direction = isNum(battery.power_w) ? (battery.power_w > 0 ? "charging" : battery.power_w < 0 ? "discharging" : "idle") : "power unknown";
      var identity = [battery.id];
      if (battery.available) identity.push(badge("available", "ok"));
      else identity.push(badge("unavailable", "err"));
      if (battery.stale) identity.push(badge("stale", "stale"));
      grid.appendChild(el("div", { class: "card" + (battery.stale ? " card-stale" : "") }, [
        el("p", { class: "card-label", text: name }),
        el("p", { class: "card-value" }, withUnit(isNum(battery.soc_pct) ? battery.soc_pct.toFixed(1) : "--", "%")),
        el("p", { class: "card-sub" }, [fmtPower(battery.power_w) + " (" + direction + ")"]),
        el("p", { class: "card-sub" }, [fmtKwh(battery.energy_kwh) + " of " + fmtKwh(battery.capacity_kwh), isNum(battery.temperature_c) ? " · " + battery.temperature_c.toFixed(1) + " °C" : ""]),
        el("p", { class: "card-sub" }, ["read " + timeAgo(battery.read_at), " ", identity.slice(1)]),
        battery.last_error ? el("p", { class: "field-error", text: battery.last_error }) : null
      ]));
    });
    body.appendChild(grid);
  }

  function viewDashboard(host) {
    var load = once(function () {
      return Promise.all([get("/status"), get("/plan", { hours: 24 })]).then(function (rows) {
        state.status = rows[0];
        return { status: rows[0], plan: rows[1] };
      });
    });

    asyncPanel(host, "Autopilot", { load: load, render: function (body, data) { renderAutopilot(body, data.status); } });
    asyncPanel(host, "Alerts", { load: load, render: function (body, data) { renderAlerts(body, data.status); } });
    asyncPanel(host, "Right now", { load: load, render: renderNow });
    asyncPanel(host, "Next 24 hours", {
      load: load,
      render: function (body, data) {
        var chart = el("div", { class: "chart chart-sm" });
        body.appendChild(chart);
        root.SEMCharts.mini(chart, data.plan);
        body.appendChild(el("p", { class: "legend-note", text: "Orange line: buy price (EUR/kWh, left axis). Purple line: battery SoC (%, right axis). Hover for per-slot values." }));
      }
    });
    asyncPanel(host, "Batteries", { load: load, render: function (body, data) { renderBatteryCards(body, data.status); } });

    var timer = root.setInterval(function () {
      if (document.hidden || anyDialogOpen()) return;
      if (document.querySelector("[data-stop-result]")) return; // keep stop results visible until dismissed
      renderRoute();
    }, 15000);
    return function () { root.clearInterval(timer); };
  }

  /* ------------------------------------------------------------------ view: plan */

  function horizonFrom(query) {
    var value = parseInt((query && query.hours) || "36", 10);
    return [12, 24, 36, 48].indexOf(value) >= 0 ? value : 36;
  }

  function planFlags(plan) {
    var slots = plan.slots || [];
    var stale = slots.filter(function (s) { return s.price_stale; }).length;
    var flags = [];
    if (!plan.feasible) flags.push({ level: "error", title: "Plan is infeasible", text: plan.infeasible_reason || "The optimiser could not satisfy the constraints; the battery is held safe." });
    if (stale) flags.push({ level: "warn", title: "Stale prices", text: stale + " of " + slots.length + " slots carry a stale price and are treated as flat by the optimiser." });
    var sources = [];
    if (slots.length) {
      if (slots[0].price_source) sources.push("price: " + slots[0].price_source);
      if (slots[0].pv_source) sources.push("PV: " + slots[0].pv_source);
    }
    return { flags: flags, sources: sources };
  }

  function renderPlanSummary(body, data) {
    var plan = data.plan;
    var cycleCost = isNum(data.cycleCost) ? data.cycleCost : 0;
    var efficiency = planEfficiency(plan.summary);
    var buyNow = state.status && isNum(state.status.price_now) ? state.status.price_now : null;
    var s = plan.summary || {};
    var stats = [
      ["Estimated cost", fmtEur(s.cost_estimate_eur)],
      ["Baseline (no battery)", fmtEur(s.baseline_cost_eur)],
      ["Saving", fmtEur(s.saved_eur)],
      ["Charged", fmtKwh(s.charged_kwh)],
      ["Discharged", fmtKwh(s.discharged_kwh)],
      ["Cycles", isNum(s.cycles) ? s.cycles.toFixed(2) + " cycles" : "--"],
      ["Arbitrage energy", fmtKwh(s.arbitrage_kwh)],
      ["Assumed round trip", efficiency ? fmtPct(efficiency.round_trip_pct) : "not reported"]
    ];
    body.appendChild(el("ul", { class: "stat-row" }, stats.map(function (row) {
      return el("li", null, [el("span", { class: "stat-label", text: row[0] }), el("span", { class: "stat-value", text: row[1] })]);
    })));
    if (isNum(s.grid_charge_kwh) || isNum(s.pv_charge_kwh)) {
      body.appendChild(el("p", { class: "legend-note", text: "Charged from grid: " + fmtKwh(s.grid_charge_kwh) + " · charged from PV: " + fmtKwh(s.pv_charge_kwh) + "." }));
    }
    var batteries = Array.isArray(plan.batteries) ? plan.batteries : [];
    if (batteries.length) {
      body.appendChild(el("p", { class: "legend-note", text: "Battery shares in this plan: " + batteries.map(function (b) {
        return (b.name || b.id) + " " + fmtPower(b.share_w);
      }).join(" · ") + "." }));
    }
    if (efficiency) {
      var example = buyNow != null ? " At the current buy price " + fmtPrice(buyNow) + " that is " + fmtPrice(planBreakEvenEur(buyNow, efficiency, cycleCost)) + " with wear " + fmtEur(cycleCost) + "/kWh." : "";
      body.appendChild(el("p", { class: "legend-note", text: "The optimiser assumed a " + fmtPct(efficiency.round_trip_pct) + " round trip (charge " + fmtPct(efficiency.eta_charge * 100) + " / discharge " + fmtPct(efficiency.eta_discharge * 100) + "). Break-even after round-trip loss = buy ÷ round trip + wear ÷ discharge efficiency, so a discharge slot only pays when the sale price clears it." + example }));
    } else {
      body.appendChild(el("p", { class: "legend-note", text: "The plan summary did not report its aggregate efficiency, so the assumed round trip and the break-even curve are unavailable; the plan was still computed with the stored per-battery efficiencies." }));
    }
    if (s.saved_eur < 0) body.appendChild(el("p", { class: "field-error", text: "The plan is more expensive than doing nothing over this horizon; check the price source and settings." }));
  }

  function renderPlanSlots(body, plan) {
    var slots = Array.isArray(plan.slots) ? plan.slots : [];
    var headers = ["Time", "Buy", "Sell", "PV", "Load", "SoC", "Battery power", "Action", "Reason", "Flags"];
    function rowsFor(list) {
      return list.map(function (slot) {
        return [
          fmtDateTime(slot.t), fmtPrice(slot.buy), fmtPrice(slot.sell), fmtKwh(slot.pv_kw, 2), fmtKwh(slot.load_kw, 2),
          fmtPct(slot.soc_pct), fmtPower(slot.power_w), actionText(slot.action), reasonText(slot.reason),
          slot.price_stale ? badge("stale price", "stale") : "—"
        ];
      });
    }
    function attrsFor(list) {
      return list.map(function (slot) { return { dataset: { stale: slot.price_stale ? "true" : "false" } }; });
    }
    function draw(list, showAll) {
      clear(body);
      body.appendChild(el("p", { class: "legend-note", text: "Actions: " + ["hold", "charge", "discharge", "pv_charge", "export", "self_consumption", "reserve"].map(actionText).join(", ") + ". Reasons explain every slot." }));
      body.appendChild(table("Plan slots, 15 minute resolution, times in local timezone", headers, rowsFor(list), attrsFor(list)));
      if (slots.length > list.length) body.appendChild(button("Show all " + slots.length + " slots", "", function () { draw(slots, true); }));
      else if (showAll && slots.length > 48) body.appendChild(button("Show first 48 slots", "", function () { draw(slots.slice(0, 48), false); }));
    }
    if (!slots.length) {
      body.appendChild(el("p", { class: "muted", text: "No plan slots yet. Recompute the plan." }));
      return;
    }
    draw(slots.slice(0, 48), false);
  }

  function viewPlan(host, query) {
    var hours = horizonFrom(query);
    var load = once(function () {
      return Promise.all([get("/plan", { hours: hours }), get("/config")]).then(function (rows) {
        return { plan: rows[0], cycleCost: cycleCostFromConfig(rows[1]) };
      });
    });
    var pollTimer = null;

    var controls = panel(host, "Plan controls");
    var select = el("select", { id: "plan-horizon" });
    [12, 24, 36, 48].forEach(function (h) {
      var option = el("option", { value: h, text: h + " hours" });
      if (h === hours) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      root.location.hash = "#/plan?hours=" + select.value;
    });
    var recompute = button("Recompute", "btn-primary");
    recompute.addEventListener("click", function () {
      performWrite(recompute, controls.body, function () { return post("/plan/recompute"); }, function () { return "Recompute queued; waiting for a new plan…"; }, null);
      var before = state.status && state.status.last_plan_at ? state.status.last_plan_at : null;
      var attempts = 0;
      if (pollTimer) root.clearInterval(pollTimer);
      pollTimer = root.setInterval(function () {
        attempts++;
        get("/status").then(function (status) {
          state.status = status;
          if ((status.last_plan_at && status.last_plan_at !== before) || attempts >= 20) {
            root.clearInterval(pollTimer);
            pollTimer = null;
            renderRoute();
          }
        }).catch(function () {
          if (attempts >= 20) { root.clearInterval(pollTimer); pollTimer = null; renderRoute(); }
        });
      }, 3000);
    });
    controls.body.appendChild(el("div", { class: "toolbar" }, [
      el("label", { for: "plan-horizon" }, ["Horizon ", select]),
      recompute,
      el("a", { class: "btn", href: apiPath("/export", { what: "plan", format: "json" }), text: "Export JSON" }),
      el("a", { class: "btn", href: apiPath("/export", { what: "plan", format: "csv" }), text: "Export CSV" })
    ]));
    controls.body.appendChild(el("p", { class: "legend-note", text: "Exports download from the add-on directly (no data leaves your instance)." }));

    asyncPanel(host, "Plan warnings", {
      load: load,
      render: function (body, data) {
        var plan = data.plan;
        var info = planFlags(plan);
        if (!info.flags.length) body.appendChild(el("p", { class: "muted", text: "No feasibility warnings. " + (info.sources.length ? "Sources — " + info.sources.join(", ") + "." : "") }));
        info.flags.forEach(function (flag) {
          body.appendChild(el("div", { class: "banner banner-" + (flag.level === "error" ? "error" : "warn") }, [
            el("p", { class: "banner-title", text: flag.title }), el("p", { text: flag.text })
          ]));
        });
        body.appendChild(el("p", { class: "legend-note", text: "Plan generated " + timeAgo(plan.generated_at) + " for " + fmtDateTime(plan.horizon_start) + " – " + fmtDateTime(plan.horizon_end) + "." }));
      }
    });

    asyncPanel(host, "Timeline", {
      load: load,
      render: function (body, data) {
        var plan = data.plan;
        var chart = el("div", { class: "chart chart-md" });
        body.appendChild(chart);
        var efficiency = planEfficiency(plan.summary);
        root.SEMCharts.plan(chart, plan, { cycleCost: isNum(data.cycleCost) ? data.cycleCost : 0, efficiency: efficiency });
        body.appendChild(el("p", { class: "legend-note", text: "Top panel: buy price bars (EUR/kWh, left axis), the dashed red break-even after round-trip loss line (buy ÷ assumed round trip + wear ÷ discharge efficiency) and the SoC line (%, right axis); grey bars are stale prices. Bottom panel: PV and load forecast (kW) and battery charging (positive, blue) or discharging (negative, orange) in kW." + (efficiency ? " Slots whose sale price sits below the break-even line would lose money, which is why they stay idle with reason “efficiency_loss”." : " The break-even line is unavailable: the plan summary carries no aggregate efficiency.") }));
      }
    });

    asyncPanel(host, "Plan summary", {
      load: load,
      render: renderPlanSummary
    });
    asyncPanel(host, "Plan slots", {
      load: load,
      render: function (body, data) { renderPlanSlots(body, data.plan); }
    });
    return function () { if (pollTimer) { root.clearInterval(pollTimer); pollTimer = null; } };
  }

  /* ------------------------------------------------------------------ view: forecasts */

  function slotHours(slots, fallbackMinutes) {
    if (slots.length >= 2) {
      var a = parseDate(slots[0].t), b = parseDate(slots[1].t);
      if (a && b) {
        var hours = (b.getTime() - a.getTime()) / 3600000;
        if (hours > 0) return hours;
      }
    }
    return (fallbackMinutes || 15) / 60;
  }

  function groupByLocalDay(slots) {
    var groups = [], index = {};
    slots.forEach(function (slot) {
      var d = parseDate(slot.t);
      var key = d ? d.toLocaleDateString() : "unknown";
      if (!(key in index)) { index[key] = groups.length; groups.push({ key: key, slots: [] }); }
      groups[index[key]].slots.push(slot);
    });
    return groups;
  }

  /** Empty-panel copy: the API `hint` (one actionable sentence, e.g. naming the bidding zone
   *  that has no day-ahead prices) is the prominent message, rendered verbatim, with the `reason`
   *  slug kept as a small badge beside it. Only when the server sent no hint do we fall back to the
   *  generic "source failed / empty horizon" sentence, so an explained empty panel is never vague.
   *  Pure (no DOM) so node tests can assert the preference directly. */
  function forecastEmptyState(payload) {
    var reason = payload && payload.reason != null && String(payload.reason) ? String(payload.reason) : null;
    var hint = payload && payload.hint != null && String(payload.hint) ? String(payload.hint) : null;
    return {
      reason: reason,
      hint: hint,
      text: hint || "No slots returned; the source failed or the horizon is empty."
    };
  }

  function renderForecastPanel(body, kind, payload) {
    var slots = Array.isArray(payload.slots) ? payload.slots : [];
    var meta = el("p", { class: "alert-meta" }, [
      badge("source: " + (payload.source || "unknown"), "info"),
      // Effective provider + configured contract (SPEC §4.1), next to the resolved source so the
      // empty-state hint below can be read against what was actually configured.
      payload.provider ? badge("provider: " + payload.provider, "info") : null,
      payload.contract ? badge("contract: " + payload.contract, "info") : null,
      " generated " + timeAgo(payload.generated_at),
      payload.stale ? badge("stale", "stale") : null,
      kind === "load" && payload.confidence ? " " : null,
      kind === "load" && payload.confidence ? badge("confidence: " + payload.confidence, payload.confidence === "low" ? "warn" : "ok") : null,
      kind === "solar" && isNum(payload.correction_applied) ? " bias correction ×" + payload.correction_applied.toFixed(2) : null
    ]);
    body.appendChild(meta);
    if (payload.stale) body.appendChild(el("p", { class: "legend-note", text: "This forecast is stale: values are re-used and greyed in the chart." }));
    (Array.isArray(payload.notes) ? payload.notes : []).forEach(function (note) {
      body.appendChild(el("p", { class: "legend-note", text: note }));
    });
    var chart = el("div", { class: "chart chart-md" });
    body.appendChild(chart);
    root.SEMCharts.forecast(chart, kind, payload);

    if (!slots.length) {
      // No slots: surface the server's `hint` verbatim and `reason` slug (the backend explains an
      // empty panel — a failed day-ahead lookup names the bidding zone), not the generic sentence.
      var empty = forecastEmptyState(payload);
      body.appendChild(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title" }, [empty.text, empty.reason ? " " : null, empty.reason ? badge(empty.reason, "warn") : null]),
        empty.hint ? null : el("p", { class: "field-help", text: "The server did not explain why this forecast is empty; check the log for the failing source." })
      ]));
      return;
    }
    var step = slotHours(slots);
    var groups = groupByLocalDay(slots);
    var headers, rows;
    if (kind === "price") {
      headers = ["Day", "Min buy", "Max buy", "Average buy", "Average sell", "Stale slots"];
      rows = groups.map(function (group) {
        var buys = group.slots.map(function (s) { return numOrNull(s.buy); }).filter(isNum);
        var sells = group.slots.map(function (s) { return numOrNull(s.sell); }).filter(isNum);
        var stale = group.slots.filter(function (s) { return s.stale; }).length;
      return [group.key,
          buys.length ? fmtPrice(Math.min.apply(null, buys)) : "--",
          buys.length ? fmtPrice(Math.max.apply(null, buys)) : "--",
          buys.length ? fmtPrice(buys.reduce(function (a, b) { return a + b; }, 0) / buys.length) : "--",
          sells.length ? fmtPrice(sells.reduce(function (a, b) { return a + b; }, 0) / sells.length) : "--",
          stale + " of " + group.slots.length];
      });
    } else if (kind === "solar") {
      headers = ["Day", "Forecast energy", "Peak power", "Peak at"];
      rows = groups.map(function (group) {
        var total = group.slots.reduce(function (sum, s) { return sum + Math.max(0, numOrNull(s.pv_kw) || 0); }, 0) * step;
        var peak = group.slots.reduce(function (best, s) {
          var value = numOrNull(s.pv_kw);
          return value != null && (!best || value > best.value) ? { value: value, t: s.t } : best;
        }, null);
        return [group.key, fmtKwh(total), peak ? fmtPower(peak.value * 1000) : "--", peak ? fmtTime(peak.t) : "--"];
      });
    } else {
      headers = ["Day", "P50 energy", "P10 energy", "P90 energy"];
      rows = groups.map(function (group) {
        var total = function (pick) {
          return group.slots.reduce(function (sum, s) { return sum + (numOrNull(pick(s)) || 0); }, 0) * step;
        };
        return [group.key, fmtKwh(total(function (s) { return s.load_kw; })), fmtKwh(total(function (s) { return s.p10_kw; })), fmtKwh(total(function (s) { return s.p90_kw; }))];
      });
    }
    body.appendChild(table("Per-day totals (times and days in local timezone)", headers, rows));
  }

  function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }

  function renderAccuracy(body, accuracy) {
    var kinds = [["pv", "PV forecast"], ["load", "Load forecast"], ["price", "Price forecast"]];
    var cards = el("div", { class: "cards" });
    kinds.forEach(function (pair) {
      var report = accuracy && accuracy[pair[0]];
      if (!report) {
        cards.appendChild(statCard(pair[1], "--", "no report"));
        return;
      }
      var value = isNum(report.mape_pct) ? fmtPct(report.mape_pct) : "--";
      var sub = ["MAPE over " + (report.days || "?") + " days", isNum(report.bias_pct) ? "bias " + fmtPct(report.bias_pct) : null, isNum(report.mae) ? "MAE " + report.mae.toFixed(3) : null, (report.samples || 0) + " samples"].filter(Boolean).join(" · ");
      cards.appendChild(statCard(pair[1], value, sub, report.samples === 0));
    });
    body.appendChild(cards);
    if (!isNum(accuracy.pv.mape_pct) && !isNum(accuracy.load.mape_pct) && !isNum(accuracy.price.mape_pct)) {
      body.appendChild(el("p", { class: "muted", text: "Not enough measured days yet to score the forecasts." }));
      return;
    }
    var chart = el("div", { class: "chart chart-sm" });
    body.appendChild(chart);
    root.SEMCharts.accuracy(chart, accuracy);
    body.appendChild(el("p", { class: "legend-note", text: "Mean absolute percentage error per day: lower is better. Green PV, blue load, orange price. Hover for exact values." }));
  }

  function viewForecasts(host) {
    var load = once(function () {
      return Promise.all([
        get("/forecast", { kind: "price", hours: 36 }),
        get("/forecast", { kind: "solar", hours: 36 }),
        get("/forecast", { kind: "load", hours: 36 }),
        get("/accuracy", { days: 14 })
      ]).then(function (rows) {
        return { price: rows[0], solar: rows[1], load: rows[2], accuracy: rows[3] };
      });
    });
    asyncPanel(host, "Price forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "price", data.price); } });
    asyncPanel(host, "Solar forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "solar", data.solar); } });
    asyncPanel(host, "Load forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "load", data.load); } });
    asyncPanel(host, "Forecast accuracy", { load: load, render: function (body, data) { renderAccuracy(body, data.accuracy); } });
    asyncPanel(host, "Providers", {
      load: once(function () { return get("/providers"); }),
      render: function (body, providers) {
        var rows = [];
        ["solar", "price", "load"].forEach(function (kind) {
          (Array.isArray(providers[kind]) ? providers[kind] : []).forEach(function (p) {
            rows.push([kind, p.name, p.active ? "yes" : "no", p.ok ? "ok" : "failing", p.stale ? "stale" : "", p.last_success ? timeAgo(p.last_success) : "never", p.last_error || "—"]);
          });
        });
        if (!rows.length) { body.appendChild(el("p", { class: "muted", text: "No provider status reported." })); return; }
        body.appendChild(table("Forecast providers", ["Kind", "Provider", "Active", "Healthy", "Freshness", "Last success", "Last error"], rows));
      }
    });
    return null;
  }

  /* ------------------------------------------------------------------ view: batteries */

  var BATTERY_ROLES = {
    generic: [
      ["power_setpoint_entity", "Signed setpoint (W, + charge / − discharge)"],
      ["charge_power_entity", "Charge power entity"],
      ["discharge_power_entity", "Discharge power entity"],
      ["mode_select_entity", "Mode select entity"],
      ["soc_entity", "State of charge sensor"],
      ["capacity_entity", "Capacity sensor"]
    ],
    zendure: [
      ["manual_mode_entity", "Manual mode select entity"],
      ["power_setpoint_entity", "Power setpoint entity"],
      ["charge_limit_entity", "Charge limit entity"],
      ["discharge_limit_entity", "Discharge limit entity"],
      ["soc_entity", "State of charge sensor"],
      ["soc_target_entity", "SoC target entity"],
      ["min_soc_entity", "Minimum SoC entity"],
      ["capacity_entity", "Capacity sensor"],
      // Usable energy above min_soc is a different quantity from the installed capacity: on a
      // nearly empty battery it reads near zero, so it must never be mapped as capacity_entity.
      ["available_kwh_entity", "Usable energy above the minimum SoC (kWh)"],
      ["pv_power_entity", "PV power sensor"],
      ["output_power_entity", "Output power sensor"],
      ["ac_mode_entity", "AC mode entity"]
    ]
  };
  var NON_ENTITY_ROLES = { mode_manual_value: "Manual mode value (e.g. manual)" };

  /* ---- battery discovery picker (SPEC §5/§10) -----------------------------------------------
   * /discover?kind=battery returns one row per (entity, role), so a battery is only obvious once
   * the rows are grouped per device — and the registry happily returns battery-ish rows for
   * devices that are not batteries (a thermostat's battery level, a bridge's `connection`
   * sensor). These helpers decide which groups are picker rows and what a Select click fills.
   * They are pure so node can run them; the dialog below only renders their result. */

  // Devices of these platforms are batteries by identity, so such a group is a picker row even
  // when discovery ranks nothing above the confidence floor.
  var BATTERY_PLATFORMS = ["zendure_ha"];
  // Only these entity domains can be written: discovery also suggests read-only SENSORS for
  // control roles, and mapping a sensor to a setpoint role produces a battery that cannot be driven.
  var BATTERY_WRITABLE_DOMAINS = ["number", "select", "input_number", "input_select", "switch"];
  var BATTERY_SENSOR_DOMAINS = ["sensor", "binary_sensor"];
  // Role slugs from haws._ROLE_HINTS(battery). Power/control = the battery can be driven,
  // manual_mode counts as control because it is how the manager is switched to manual dispatch.
  var BATTERY_POWER_ROLES = ["power_setpoint", "charge_limit", "discharge_limit"];
  var BATTERY_CONTROL_ROLES = BATTERY_POWER_ROLES.concat(["manual_mode"]);
  var BATTERY_STATE_ROLES = ["soc", "capacity"];
  var BATTERY_SETPOINT_ROLES = ["power_setpoint", "manual_mode"];
  // Roles that must come from a writable entity, and roles that are read from the device instead.
  var BATTERY_WRITABLE_ROLES = BATTERY_CONTROL_ROLES.concat(["min_soc", "soc_target"]);
  var BATTERY_MEASUREMENT_ROLES = ["soc", "capacity", "available_energy", "output_power", "pv_power", "connection"];
  // Same floor as haws.suggested_mapping: a weaker suggestion is not mapped anywhere.
  var BATTERY_MIN_CONFIDENCE = 0.5;

  function batteryDomainOf(row) {
    return String(row && row.entity_id || "").split(".")[0].toLowerCase();
  }

  function batteryIsWritable(row) {
    return BATTERY_WRITABLE_DOMAINS.indexOf(batteryDomainOf(row)) >= 0;
  }

  function batteryIsSensor(row) {
    return BATTERY_SENSOR_DOMAINS.indexOf(batteryDomainOf(row)) >= 0;
  }

  /** Mapping keys the driver's own role table offers (`driver` null = every driver).
   *  Derived from BATTERY_ROLES so the picker can never drift from the Assign dropdowns, which is
   *  also why there is no second role table to maintain. */
  function batteryRoleKeys(driver) {
    var names = driver ? [driver] : Object.keys(BATTERY_ROLES);
    var keys = {};
    names.forEach(function (name) {
      (BATTERY_ROLES[name] || []).forEach(function (role) { keys[role[0]] = true; });
    });
    return keys;
  }

  /** Role slug -> mapping key for the driver whose key table is `keys`.
   *  "<role>_entity" is only used when that key really is in BATTERY_ROLES (the server's own
   *  mapping_key first when it agrees), so a role the driver cannot store is left for the Assign
   *  table instead of being guessed at. ponytail: that leaves charge_limit/discharge_limit
   *  unmappable on a generic battery (generic stores charge_power/discharge_power); the user
   *  assigns those with the Assign dropdown, exactly as before this picker existed. */
  function batteryRoleKey(role, keys, serverKey) {
    var slug = String(role || "");
    if (!slug) return null;
    if (serverKey && keys[serverKey]) return serverKey;
    return keys[slug + "_entity"] ? slug + "_entity" : null;
  }

  function batteryRowConfidence(row) {
    var value = numeric(row && row.confidence);
    return value == null ? 0 : value;
  }

  /** Backend ordering (haws.suggested_mapping): higher confidence, then smallest entity_id. */
  function batteryRowBetter(row, other) {
    var mine = batteryRowConfidence(row);
    var theirs = batteryRowConfidence(other);
    if (mine !== theirs) return mine > theirs;
    return String(row.entity_id || "") < String(other.entity_id || "");
  }

  /** The right kind of entity for this role: control roles must be writable, so a sensor that
   *  happens to match a setpoint role is never chosen (and never silently mapped). */
  function batteryRoleAccepts(row, role) {
    if (BATTERY_WRITABLE_ROLES.indexOf(role) >= 0) return batteryIsWritable(row);
    return true;
  }

  /** Preference inside one role: the right kind of entity first, then the backend order above. */
  function batteryRowRank(row, role) {
    if (BATTERY_WRITABLE_ROLES.indexOf(role) >= 0) return batteryIsWritable(row) ? 0 : 1;
    if (BATTERY_MEASUREMENT_ROLES.indexOf(role) >= 0) return batteryIsSensor(row) ? 0 : 1;
    return 0;
  }

  function batteryRoleRowBetter(row, other, role) {
    var mine = batteryRowRank(row, role);
    var theirs = batteryRowRank(other, role);
    if (mine !== theirs) return mine < theirs;
    return batteryRowBetter(row, other);
  }

  /** Best usable candidate per role, above the confidence floor: writable for a control role,
   *  sensor-preferred for a measurement role. Control roles whose only match is read-only are
   *  reported separately so the dialog can say so instead of mapping an unusable entity. */
  function batteryGroupRoles(group) {
    var chosen = {};
    var skipped = [];
    group.rows.forEach(function (row) {
      var role = String(row.suggested_role || "");
      if (!role || batteryRowConfidence(row) < BATTERY_MIN_CONFIDENCE) return;
      if (!batteryRoleAccepts(row, role)) {
        if (skipped.indexOf(role) < 0) skipped.push(role);
        return;
      }
      if (!chosen[role] || batteryRoleRowBetter(row, chosen[role], role)) chosen[role] = row;
    });
    return { chosen: chosen, skipped: skipped };
  }

  /** device_name, else manufacturer + model, else the best candidate's name, else its entity id. */
  function batteryGroupLabel(group) {
    if (group.deviceName) return String(group.deviceName);
    if (group.manufacturer && group.model) return group.manufacturer + " " + group.model;
    var best = null;
    group.rows.forEach(function (row) { if (!best || batteryRowBetter(row, best)) best = row; });
    return best ? String(best.name || best.entity_id || "") : "";
  }

  /** Which roles this group can map, whether it can be driven at all, and why it is a picker
   *  row in the first place. Returns null when the group is not plausibly a battery. */
  function batteryGroupInfo(group) {
    var found = batteryGroupRoles(group);
    var chosen = found.chosen;
    var keys = batteryRoleKeys(null);
    var mappable = [];
    Object.keys(chosen).sort().forEach(function (role) {
      if (batteryRoleKey(role, keys, chosen[role].mapping_key)) mappable.push(role);
    });
    var viaPlatform = (group.platforms || []).some(function (platform) {
      return BATTERY_PLATFORMS.indexOf(platform) >= 0;
    });
    // Control roles are writable by construction (batteryGroupRoles drops the read-only ones).
    var controls = mappable.filter(function (role) { return BATTERY_CONTROL_ROLES.indexOf(role) >= 0; });
    var states = mappable.filter(function (role) { return BATTERY_STATE_ROLES.indexOf(role) >= 0; });
    // A group qualifies on a known battery platform, or on being able to both drive and read a
    // battery: one lone role (a thermostat's soc, a bridge's connection, a camera's manual_mode
    // button) is not a battery and must never become a picker row. Sensor-only control matches
    // still count here — such a device looks exactly like a battery, it just cannot be driven.
    if (!viaPlatform && !((controls.length || found.skipped.length) && states.length)) return null;
    var writableControl = controls.length > 0;
    var soc = mappable.indexOf("soc") >= 0;
    var capacity = mappable.indexOf("capacity") >= 0;
    var missing = [];
    if (!writableControl) missing.push("a writable control entity");
    if (!soc) missing.push("soc");
    if (!capacity) missing.push("capacity");
    return {
      roles: chosen,
      mappable: mappable,
      skipped: found.skipped,
      writableControl: writableControl,
      readOnlyControl: !writableControl && found.skipped.length > 0,
      controlReady: writableControl && (soc || capacity),
      missing: missing,
      viaPlatform: viaPlatform,
      confidence: mappable.reduce(function (sum, role) { return sum + batteryRowConfidence(chosen[role]); }, 0)
    };
  }

  /** Candidate rows -> picker groups: grouped per device, only the plausible batteries left,
   *  writable-control groups first, then most mappable roles, then summed confidence, then label. */
  function batteryGroupsFromCandidates(rows) {
    var groups = [];
    var index = {};
    (Array.isArray(rows) ? rows : []).forEach(function (row) {
      if (!row) return;
      var deviceId = row.device_id ? String(row.device_id) : "";
      // No device_id: group by entity id. One entity can appear once per role, and dropping these
      // rows would hide batteries whose registry entry lost its device.
      var key = deviceId ? "device:" + deviceId : "entity:" + String(row.entity_id || "");
      var group = index[key];
      if (!group) {
        group = {
          key: key,
          deviceId: deviceId || null,
          deviceName: row.device_name ? String(row.device_name) : "",
          manufacturer: row.manufacturer ? String(row.manufacturer) : null,
          model: row.model ? String(row.model) : null,
          platform: row.platform ? String(row.platform) : "",
          platforms: [],
          rows: []
        };
        index[key] = group;
        groups.push(group);
      }
      group.rows.push(row);
      if (!group.deviceName && row.device_name) group.deviceName = String(row.device_name);
      if (!group.manufacturer && row.manufacturer) group.manufacturer = String(row.manufacturer);
      if (!group.model && row.model) group.model = String(row.model);
      if (row.platform) {
        var platform = String(row.platform);
        if (group.platforms.indexOf(platform) < 0) group.platforms.push(platform);
        if (!group.platform) group.platform = platform;
      }
    });
    var picker = [];
    groups.forEach(function (group) {
      group.label = batteryGroupLabel(group);
      var info = batteryGroupInfo(group);
      if (!info) return;
      group.roles = info.roles;
      group.mappable = info.mappable;
      group.skipped = info.skipped;
      group.writableControl = info.writableControl;
      group.readOnlyControl = info.readOnlyControl;
      group.controlReady = info.controlReady;
      group.missing = info.missing;
      group.viaPlatform = info.viaPlatform;
      group.confidence = info.confidence;
      picker.push(group);
    });
    // Writable-control batteries first, then role coverage, then summed confidence, then label.
    return picker.sort(function (a, b) {
      if (a.writableControl !== b.writableControl) return a.writableControl ? -1 : 1;
      if (a.controlReady !== b.controlReady) return a.controlReady ? -1 : 1;
      if (a.mappable.length !== b.mappable.length) return b.mappable.length - a.mappable.length;
      if (a.confidence !== b.confidence) return b.confidence - a.confidence;
      return a.label < b.label ? -1 : a.label > b.label ? 1 : 0;
    });
  }

  /** Search text of one row: entity text plus the device fields the server does not match on. */
  function batteryRowMatchesQuery(row, query) {
    var needle = String(query == null ? "" : query).trim().toLowerCase();
    if (!needle) return true;
    var haystack = [row.entity_id, row.name, row.device_name, row.manufacturer, row.model, row.platform]
      .map(function (part) { return String(part == null ? "" : part).toLowerCase(); })
      .join(" ");
    return haystack.indexOf(needle) >= 0;
  }

  function batteryGroupMatchesQuery(group, query) {
    return group.rows.some(function (row) { return batteryRowMatchesQuery(row, query); });
  }

  /** Local search first (it sees device name, manufacturer, model and platform), the server only
   *  when nothing matches: the server matches entity text and caps at 50 rows, so it can hide a
   *  battery the user found by its model name. */
  function batterySearchGroups(rows, query) {
    var groups = batteryGroupsFromCandidates(rows);
    var needle = String(query == null ? "" : query).trim();
    if (!needle) return { groups: groups, fromServer: false };
    var matches = groups.filter(function (group) { return batteryGroupMatchesQuery(group, needle); });
    return { groups: matches, fromServer: !matches.length };
  }

  /** slugify(label) made unique against the configured battery ids (_2, _3, …). */
  function batterySlugFor(label, takenIds) {
    var taken = {};
    (Array.isArray(takenIds) ? takenIds : []).forEach(function (id) {
      var key = String(id == null ? "" : id).toLowerCase();
      if (key) taken[key] = true;
    });
    var base = slugify(label) || "battery";
    if (!taken[base]) return base;
    var suffix = 2;
    while (taken[base + "_" + suffix]) suffix += 1;
    return base + "" + "_" + suffix;
  }

  /** Roles -> mapping keys for one driver, plus the role each key came from. */
  function batteryMappingFor(group, driver) {
    var keys = batteryRoleKeys(driver);
    var roles = batteryGroupRoles(group).chosen;
    var mapping = {};
    var keyRoles = {};
    Object.keys(roles).sort().forEach(function (role) {
      var key = batteryRoleKey(role, keys, roles[role].mapping_key);
      if (!key) return;
      mapping[key] = String(roles[role].entity_id || "");
      keyRoles[key] = role;
    });
    return { mapping: mapping, roles: roles, keyRoles: keyRoles };
  }

  /** zendure when the platform says so or a role maps to a zendure-only key, else generic. */
  function batteryDriverFor(group) {
    if ((group.platforms || []).some(function (platform) { return BATTERY_PLATFORMS.indexOf(platform) >= 0; })) {
      return "zendure";
    }
    var zendure = batteryRoleKeys("zendure");
    var generic = batteryRoleKeys("generic");
    var roles = batteryGroupRoles(group).chosen;
    return Object.keys(roles).some(function (role) {
      var key = batteryRoleKey(role, zendure, roles[role].mapping_key);
      return key && !generic[key];
    }) ? "zendure" : "generic";
  }

  /** A candidate row's min/max state attribute. The backend started sending these after the rest
   *  of this file, so both shapes are accepted and anything not a finite number is ignored. */
  function batteryRowLimit(row, which) {
    var value = row ? row[which] : null;
    if (value == null && row && row.attributes) value = row.attributes[which];
    return numeric(value);
  }

  /** Absolute bound of a limit entity: abs(min) when the range starts negative, else its max. */
  function batteryAbsoluteBound(min, max) {
    var bound = min != null && min < 0 ? Math.abs(min) : max;
    return isNum(bound) && bound > 0 ? bound : null;
  }

  /** Numeric prefill, only where discovery states a number (SPEC R4: never invent one).
   *  Capacity comes from the capacity role and only when its unit is kWh. The power limits are
   *  role-aware: a charge/discharge *limit* entity is a positive range (0..N) and only ever fills
   *  its own direction, so the sign convention (negative min bounds charge, positive max bounds
   *  discharge) applies to a signed setpoint (or manual-mode) entity alone. A side with no source
   *  keeps whatever the form already had, and min_soc/soc_target state an allowed range (0..100 %)
   *  rather than the user's window, so they never prefill a SoC limit. */
  function batteryNumbersFor(bundle) {
    var numbers = {};
    var roles = bundle.roles || {};
    Object.keys(bundle.keyRoles).forEach(function (key) {
      var role = bundle.keyRoles[key];
      var row = roles[role];
      if (!row) return;
      if (role === "capacity") {
        var capacity = numeric(row.state);
        var unit = String(row.unit == null ? "" : row.unit).trim().toLowerCase();
        if (capacity != null && capacity > 0 && unit === "kwh") numbers.capacity_kwh = capacity;
        return;
      }
      if (role === "min_soc" || role === "soc_target") return;   // allowed range, not the chosen window
      var min = batteryRowLimit(row, "min");
      var max = batteryRowLimit(row, "max");
      if (role === "charge_limit") {
        var charge = batteryAbsoluteBound(min, max);
        if (charge != null) numbers.max_charge_w = charge;
        return;
      }
      if (role === "discharge_limit") {
        var discharge = batteryAbsoluteBound(min, max);
        if (discharge != null) numbers.max_discharge_w = discharge;
        return;
      }
      if (BATTERY_SETPOINT_ROLES.indexOf(role) < 0) return;
      // One signed entity states both directions; a dedicated limit entity above still wins.
      if (min != null && min < 0 && numbers.max_charge_w == null) numbers.max_charge_w = Math.abs(min);
      if (max != null && max > 0 && numbers.max_discharge_w == null) numbers.max_discharge_w = max;
    });
    return numbers;
  }

  /** What one Select click fills: id, name, driver, role mapping and the stated numbers. */
  function batterySelection(group, takenIds) {
    var driver = batteryDriverFor(group);
    var bundle = batteryMappingFor(group, driver);
    return {
      id: batterySlugFor(group.label, takenIds),
      name: group.label,
      driver: driver,
      mapping: bundle.mapping,
      numbers: batteryNumbersFor(bundle)
    };
  }

  function numeric(value) {
    if (isNum(value)) return value;
    if (typeof value === "string" && value.trim() !== "") {
      var parsed = Number(value);
      return isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  /** Effective per-direction + round-trip efficiency of a battery payload (SPEC §5).
   *  Prefers the explicit eta fields; falls back to splitting round_trip_efficiency_pct evenly. */
  function batteryEfficiency(config) {
    config = config || {};
    var etaCharge = numeric(config.eta_charge);
    var etaDischarge = numeric(config.eta_discharge);
    var pct = numeric(config.round_trip_efficiency_pct);
    if ((etaCharge == null || etaDischarge == null) && pct != null && pct > 0 && pct <= 100) {
      var split = Math.sqrt(pct / 100);
      etaCharge = split;
      etaDischarge = split;
    }
    if (etaCharge == null || etaDischarge == null || etaCharge <= 0 || etaDischarge <= 0) return null;
    var roundTrip = etaCharge * etaDischarge;
    return {
      eta_charge: etaCharge,
      eta_discharge: etaDischarge,
      round_trip: roundTrip,
      round_trip_pct: roundTrip * 100,
      asymmetric: Math.abs(etaCharge - etaDischarge) > 1e-9
    };
  }

  function roundTripPct(config) {
    var eff = batteryEfficiency(config);
    return eff ? eff.round_trip_pct : null;
  }

  /** All-in cost of one kWh put into the pack: (buy + wear) / eta_charge. */
  function storedKwhCostEur(buy, config, cycleCost) {
    var eff = batteryEfficiency(config);
    if (!eff || !isNum(buy)) return null;
    return (buy + (isNum(cycleCost) ? cycleCost : 0)) / eff.eta_charge;
  }

  /** Lowest sale price that covers buying at `buy`: buy / round_trip + wear / eta_discharge (SPEC §5). */
  function breakEvenSellEur(buy, config, cycleCost) {
    var eff = batteryEfficiency(config);
    if (!eff || !isNum(buy)) return null;
    return buy / eff.round_trip + (isNum(cycleCost) ? cycleCost : 0) / eff.eta_discharge;
  }

  /** Aggregate efficiency the plan assumed (plan.summary), or null when the server omits it. */
  function planEfficiency(summary) {
    if (!summary) return null;
    var etaCharge = numeric(summary.eta_charge);
    var etaDischarge = numeric(summary.eta_discharge);
    var roundTrip = numeric(summary.round_trip_efficiency);
    var pct = numeric(summary.round_trip_efficiency_pct);
    if (roundTrip != null && roundTrip > 1) roundTrip = roundTrip / 100; // tolerate a percent
    if (roundTrip == null && pct != null) roundTrip = pct / 100;
    if (etaCharge == null || etaDischarge == null) {
      if (roundTrip == null || roundTrip <= 0) return null;
      etaCharge = Math.sqrt(roundTrip);
      etaDischarge = etaCharge;
    }
    if (etaCharge <= 0 || etaDischarge <= 0) return null;
    var rt = roundTrip != null && roundTrip > 0 ? roundTrip : etaCharge * etaDischarge;
    return {
      eta_charge: etaCharge,
      eta_discharge: etaDischarge,
      round_trip: rt,
      round_trip_pct: rt * 100,
      asymmetric: Math.abs(etaCharge - etaDischarge) > 1e-9
    };
  }

  function planBreakEvenEur(buy, efficiency, cycleCost) {
    if (!isNum(buy) || !efficiency) return null;
    return buy / efficiency.round_trip + (isNum(cycleCost) ? cycleCost : 0) / efficiency.eta_discharge;
  }

  /** Efficiency form fields -> battery payload (SPEC §5). Returns {error} when invalid. */
  function batteryEfficiencyPayload(roundTripValue, chargeValue, dischargeValue) {
    var pct = numeric(roundTripValue);
    var charge = numeric(chargeValue);
    var discharge = numeric(dischargeValue);
    var advanced = charge != null || discharge != null;
    if (advanced && (charge == null || discharge == null)) {
      return { error: "Fill both advanced per-direction values, or leave both empty to split the round trip evenly." };
    }
    if (pct == null || !(pct > 0 && pct <= 100)) {
      return { error: "Round-trip efficiency must be a number in (0, 100]." };
    }
    if (advanced && (!(charge > 0 && charge <= 100) || !(discharge > 0 && discharge <= 100))) {
      return { error: "Advanced efficiencies must be numbers in (0, 100]." };
    }
    var payload = { round_trip_efficiency_pct: pct };
    if (advanced) {
      payload.eta_charge = charge / 100;
      payload.eta_discharge = discharge / 100;
    }
    return {
      payload: payload,
      status: {
        winner: advanced ? "advanced" : "round_trip",
        round_trip_pct: pct,
        split_pct: Math.sqrt(pct / 100) * 100,
        effective_round_trip_pct: advanced ? (charge * discharge) / 100 : pct
      }
    };
  }

  function cycleCostFromConfig(config) {
    var settings = config && Array.isArray(config.settings) ? config.settings : [];
    for (var i = 0; i < settings.length; i++) {
      if (settings[i].key === "optimizer.cycle_cost_eur_per_kwh") {
        var value = numeric(settings[i].value);
        return value != null ? value : (numeric(settings[i].default) || 0);
      }
    }
    return 0;
  }

  function batteryCycleCount(state) {
    if (state && state.extra && isNum(state.extra.cycles)) return state.extra.cycles;
    return null;
  }

  function mappingList(config, resolved) {
    var mapping = config && config.mapping ? config.mapping : {};
    var keys = Object.keys(mapping);
    if (!keys.length) return el("p", { class: "field-error", text: "No entity mapping stored: this battery cannot be dispatched until the mapping is confirmed." });
    var list = el("ul", { class: "mapping-list" }, keys.map(function (key) {
      return el("li", null, [
        el("span", { class: "mapping-key", text: key }),
        el("span", { class: "mapping-value", text: String(mapping[key]) })
      ]);
    }));
    return el("div", null, [
      el("p", { class: "card-sub" }, [resolved ? badge("mapping resolved", "ok") : badge("mapping needs confirmation", "warn")]),
      list
    ]);
  }

  function testDischargeOutcome(res) {
    var applied = Math.abs(isNum(res.applied_w) ? res.applied_w : 0);
    var seconds = isNum(res.seconds) ? (Number.isInteger(res.seconds) ? String(res.seconds) : res.seconds.toFixed(1)) : "?";
    var intent = String(res.intent || "discharge");
    var failed = res.ok === false;
    var text = (failed ? "Test discharge FAILED" : "Test discharge applied") + ": " + fmtPower(applied) + " " + intent + " for " + seconds + " s (server-confirmed)";
    if (res.code) text += " · code " + res.code;
    if (res.message) text += " · " + res.message;
    return text + ".";
  }

  function testDischargeDialog(record, status) {
    var config = record.config;
    var maxPower = isNum(config.max_discharge_w) && config.max_discharge_w > 0 ? config.max_discharge_w : 800;
    var dlg = el("dialog", { class: "dialog", "aria-label": "Test discharge for " + config.name });
    var power = el("input", { type: "number", min: "1", max: String(maxPower), step: "50", value: "200", id: "test-power" });
    var seconds = el("input", { type: "number", min: "1", max: "60", step: "1", value: "10", id: "test-seconds" });
    var note = el("p", { class: "muted" });
    var save = button("Run test discharge", "btn-danger");
    save.addEventListener("click", function () {
      confirmAction({
        title: "Test discharge " + config.name + "?",
        body: "This writes a real discharge setpoint of " + power.value + " W for " + seconds.value + " s through the configured entities. The server clamps it to the battery's limits and reports the applied power and the exact seconds.",
        confirmLabel: "Run test", requireCheck: true,
        checkLabel: "I confirm this writes a real setpoint and the battery may briefly discharge"
      }).then(function (ok) {
        if (!ok) return;
        performWrite(save, dlg, function () {
          return post("/batteries/" + encodeURIComponent(config.id) + "/test", {
            power_w: Number(power.value),
            seconds: Number(seconds.value),
            intent: "discharge"
          });
        }, testDischargeOutcome);
      });
    });
    append(dlg, [
      el("h2", { text: "Test discharge — " + config.name }),
      el("p", { class: "muted", text: "Writes a real, bounded discharge setpoint (no dry-run suppression). Discharge power up to " + fmtPower(maxPower) + ", duration 1–60 s at the exact seconds asked for. The server clamps the request to the battery's own limits and reports what was really applied. Available only when mode is auto and dry run is off." }),
      el("div", { class: "fields" }, [
        el("label", { class: "field", for: "test-power" }, [el("span", { text: "Discharge power (W)" }), el("span", { class: "field-help", text: "1 to " + fmtPower(maxPower) }), power]),
        el("label", { class: "field", for: "test-seconds" }, [el("span", { text: "Duration (s, exact)" }), el("span", { class: "field-help", text: "1 to 60 s, no rounding up" }), seconds])
      ]),
      note, save,
      el("div", { class: "dialog-actions" }, [button("Close", "", function () { dlg.close(); })])
    ]);
    document.body.appendChild(dlg);
    dlg.addEventListener("close", function () { dlg.remove(); });
    dlg.showModal();
    return dlg;
  }

  function batteryCard(body, record, status, cycleCost, reload, takenIds) {
    var config = record.config || {};
    var st = record.state || {};
    var capabilities = record.capabilities || {};
    var cycles = batteryCycleCount(st);
    var eff = batteryEfficiency(config);
    var wear = isNum(cycleCost) ? cycleCost : 0;
    var buy = isNum(status.price_now) ? status.price_now : null;
    var storedCost = storedKwhCostEur(buy, config, wear);
    var breakEven = breakEvenSellEur(buy, config, wear);
    var canTest = status.mode === "auto" && !status.dry_run;
    var actions = el("div", { class: "battery-actions" });

    var toggle = button(config.enabled ? "Disable" : "Enable", config.enabled ? "" : "btn-primary");
    toggle.addEventListener("click", function () {
      function apply() {
        performWrite(toggle, card, function () {
          return patch("/batteries/" + encodeURIComponent(config.id), { patch: { enabled: !config.enabled } });
        }, function (res) { return "Battery " + config.id + " is now " + (res && res.config && res.config.enabled ? "enabled" : "disabled") + " (confirmed by the server)."; }, reload);
      }
      if (!config.enabled) {
        confirmAction({
          title: "Enable " + config.name + "?",
          body: "An enabled battery can be dispatched when mode is auto and dry run is off.",
          confirmLabel: "Enable battery", requireCheck: true,
          checkLabel: "I understand this battery can then be controlled"
        }).then(function (ok) { if (ok) apply(); });
      } else {
        apply();
      }
    });
    actions.appendChild(toggle);

    var test = button("Test discharge", "btn-danger");
    test.disabled = !canTest;
    test.setAttribute("aria-disabled", canTest ? "false" : "true");
    if (!canTest) test.setAttribute("title", "Requires mode auto with dry run off");
    test.addEventListener("click", function () {
      if (!canTest) return;
      testDischargeDialog(record, status);
    });
    actions.appendChild(test);

    var edit = button("Edit", "", function () { openBatteryDialog(record, reload, takenIds); });
    actions.appendChild(edit);

    var remove = button("Delete", "btn-danger");
    remove.addEventListener("click", function () {
      confirmAction({
        title: "Delete " + config.name + "?",
        body: "Removes the battery configuration. Dispatch stops immediately; measurements keep working.",
        confirmLabel: "Delete battery", requireCheck: true,
        checkLabel: "I understand this cannot be undone"
      }).then(function (ok) {
        if (!ok) return;
        performWrite(remove, card, function () {
          return del("/batteries/" + encodeURIComponent(config.id));
        }, function () { return "Battery deleted (confirmed by the server)."; }, reload);
      });
    });
    actions.appendChild(remove);

    var card = el("section", { class: "panel battery-card", dataset: { battery: config.id } }, [
      el("div", { class: "panel-head" }, [
        el("h3", null, [config.name || config.id, " ", badge(config.driver || "driver?", "info"), " ", config.enabled ? badge("enabled", "ok") : badge("disabled", "warn"), " ", st.stale ? badge("stale state", "stale") : null]),
        actions
      ]),
      el("div", { class: "panel-body" }, [
        el("p", { class: "alert-meta", text: "id: " + config.id + " · last read " + timeAgo(st.read_at) + (st.available ? "" : " · unavailable") + (st.last_error ? " · last error: " + st.last_error : "") }),
        el("ul", { class: "stat-row" }, [
          el("li", null, [el("span", { class: "stat-label", text: "Power" }), el("span", { class: "stat-value", text: fmtPower(st.power_w) + (isNum(st.power_w) ? (st.power_w > 0 ? " charging" : st.power_w < 0 ? " discharging" : " idle") : "") })]),
          el("li", null, [el("span", { class: "stat-label", text: "SoC" }), el("span", { class: "stat-value", text: fmtPct(st.soc_pct) })]),
          el("li", null, [el("span", { class: "stat-label", text: "Energy" }), el("span", { class: "stat-value", text: fmtKwh(st.energy_kwh) + " of " + fmtKwh(st.capacity_kwh || config.capacity_kwh) })]),
          el("li", null, [el("span", { class: "stat-label", text: "Round trip" }), el("span", { class: "stat-value", text: eff ? fmtPct(eff.round_trip_pct) : "--" })]),
          el("li", null, [el("span", { class: "stat-label", text: "Cycles" }), el("span", { class: "stat-value", text: cycles == null ? "not reported" : cycles.toFixed(1) + " cycles" })]),
          el("li", null, [el("span", { class: "stat-label", text: "Limits" }), el("span", { class: "stat-value", text: fmtPower(config.max_charge_w) + " charge / " + fmtPower(config.max_discharge_w) + " discharge" })]),
          el("li", null, [el("span", { class: "stat-label", text: "SoC window" }), el("span", { class: "stat-value", text: fmtPct(config.min_soc_pct, 0) + " to " + fmtPct(config.max_soc_pct, 0) })]),
          el("li", null, [el("span", { class: "stat-label", text: "Stored kWh, all-in" }), el("span", { class: "stat-value", text: fmtPrice(storedCost) })]),
          el("li", null, [el("span", { class: "stat-label", text: "Break-even sale" }), el("span", { class: "stat-value", text: fmtPrice(breakEven) })])
        ]),
        eff
          ? el("p", { class: "card-sub" }, ["Charge ", fmtPct(eff.eta_charge * 100), " / discharge ", fmtPct(eff.eta_discharge * 100), eff.asymmetric ? " — advanced per-direction override" : " — split evenly from the round trip"])
          : el("p", { class: "field-error", text: "No usable efficiency on this battery: the stored-kWh and break-even maths is unavailable." }),
        el("p", { class: "legend-note", text: "Stored kWh, all-in = (buy + wear) / charge efficiency: the all-in cost of energy put into the pack. Break-even sale = the lowest sale price that covers the round trip: buy / round trip + wear / discharge efficiency. " + (buy != null ? "At the current buy price " + fmtPrice(buy) + " and wear " + fmtEur(wear) + "/kWh." : "The current buy price is unknown right now, so both show --.") }),
        mappingList(config, record.mapping_resolved),
        Object.keys(capabilities).length ? el("p", { class: "legend-note", text: "Driver capabilities: " + Object.keys(capabilities).map(function (k) { return k + "=" + String(capabilities[k]); }).join(", ") }) : null,
        Array.isArray(record.discovery) && record.discovery.length ? el("p", { class: "legend-note", text: record.discovery.length + " entity candidate(s) available for the wizard." }) : null
      ])
    ]);
    body.appendChild(card);
  }

  function renderBatteries(body, data) {
    body.appendChild(el("p", { class: "muted", text: "Batteries are confirmed by you: the wizard only suggests entities from the Home Assistant registry. Advisory/off and dry run stop new plan writes, but the device has no watchdog — a setpoint written earlier stays in force until Stop now (or the device's own app) neutralises it." }));
    var list = Array.isArray(data.batteries) ? data.batteries : [];
    var ids = list.map(function (record) { return record && record.config ? record.config.id : ""; });
    var add = button("Add battery", "btn-primary", function () { openBatteryDialog(null, data.reload, ids); });
    body.appendChild(el("div", { class: "battery-actions" }, [add]));
    if (!list.length) {
      body.appendChild(el("p", { class: "muted", text: "No batteries configured yet. Use Add battery to map one." }));
      return;
    }
    list.forEach(function (record) { batteryCard(body, record, data.status, data.cycleCost, data.reload, ids); });
  }

  function viewBatteries(host) {
    var load = once(function () {
      return Promise.all([get("/batteries"), get("/status"), get("/config")]).then(function (rows) {
        state.status = rows[1];
        return { batteries: rows[0].batteries || [], status: rows[1], cycleCost: cycleCostFromConfig(rows[2]) };
      });
    });
    asyncPanel(host, "Battery fleet", {
      load: load,
      render: function (body, data) {
        var reload = function () { renderRoute(); };
        renderBatteries(body, { batteries: data.batteries, status: data.status, cycleCost: data.cycleCost, reload: reload });
      }
    });
    return null;
  }

  function slugify(text) {
    return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
  }

  function openBatteryDialog(record, reload, takenIds) {
    var editing = !!record;
    var source = editing ? record.config || {} : {};
    // Uniqueness for the picked id: every configured battery except the one being edited.
    var configuredIds = (Array.isArray(takenIds) ? takenIds : []).map(function (id) {
      return editing && id === source.id ? "" : id;
    });
    var sourceEfficiency = batteryEfficiency(source);
    // The advanced per-direction fields are only prefilled for a stored asymmetric override; a
    // symmetric battery stays on the primary round-trip input.
    var advancedPrefill = sourceEfficiency && sourceEfficiency.asymmetric
      ? { charge: (sourceEfficiency.eta_charge * 100).toFixed(1), discharge: (sourceEfficiency.eta_discharge * 100).toFixed(1) }
      : { charge: "", discharge: "" };
    var fields = {
      id: String(source.id || ""),
      name: String(source.name || ""),
      driver: source.driver === "zendure" ? "zendure" : "generic",
      capacity_kwh: isNum(source.capacity_kwh) ? source.capacity_kwh : 5.0,
      min_soc_pct: isNum(source.min_soc_pct) ? source.min_soc_pct : 10.0,
      max_soc_pct: isNum(source.max_soc_pct) ? source.max_soc_pct : 90.0,
      max_charge_w: isNum(source.max_charge_w) ? source.max_charge_w : 800.0,
      max_discharge_w: isNum(source.max_discharge_w) ? source.max_discharge_w : 800.0,
      round_trip_efficiency_pct: sourceEfficiency ? Math.round(sourceEfficiency.round_trip_pct * 10) / 10 : 90.0,
      enabled: source.enabled !== false
    };
    var mapping = {};
    Object.keys(source.mapping || {}).forEach(function (key) { mapping[key] = String(source.mapping[key]); });
    var inputs = {};

    var dlg = el("dialog", { class: "dialog dialog-wide", "aria-label": editing ? "Edit battery" : "Add battery" });
    var body = el("div");

    function fieldRow(key, label, value, step, min, max) {
      var input = el("input", { type: "number", value: String(value), step: step, min: min, max: max });
      inputs[key] = input;
      return el("label", { class: "field" }, [el("span", { text: label }), input]);
    }
    function textRow(key, label, value, readonly) {
      var input = el("input", { type: "text", value: value });
      if (readonly) input.readOnly = true;
      inputs[key] = input;
      return el("label", { class: "field" }, [el("span", { text: label }), input]);
    }

    var driverSelect = el("select", { id: "battery-driver" });
    ["generic", "zendure"].forEach(function (driver) {
      var option = el("option", { value: driver, text: driver });
      if (driver === fields.driver) option.selected = true;
      driverSelect.appendChild(option);
    });
    inputs.driver = driverSelect;

    var identity = el("div", { class: "fields" }, [
      textRow("id", "Battery id (slug)", fields.id, editing),
      textRow("name", "Name", fields.name),
      el("label", { class: "field", for: "battery-driver" }, [el("span", { text: "Driver" }), driverSelect]),
      fieldRow("capacity_kwh", "Capacity (kWh, usable AC side)", fields.capacity_kwh, "0.1", "0.1"),
      fieldRow("min_soc_pct", "Minimum SoC (%)", fields.min_soc_pct, "1", "0", "100"),
      fieldRow("max_soc_pct", "Maximum SoC (%)", fields.max_soc_pct, "1", "0", "100"),
      fieldRow("max_charge_w", "Max charge power (W)", fields.max_charge_w, "50", "0"),
      fieldRow("max_discharge_w", "Max discharge power (W)", fields.max_discharge_w, "50", "0")
    ]);

    var roundTripInput = el("input", { type: "number", min: "0.1", max: "100", step: "0.1", value: String(fields.round_trip_efficiency_pct), id: "battery-round-trip" });
    var etaChargeInput = el("input", { type: "number", min: "0.1", max: "100", step: "0.1", value: advancedPrefill.charge, placeholder: "(empty = split evenly)", id: "battery-eta-charge" });
    var etaDischargeInput = el("input", { type: "number", min: "0.1", max: "100", step: "0.1", value: advancedPrefill.discharge, placeholder: "(empty = split evenly)", id: "battery-eta-discharge" });
    inputs.round_trip_efficiency_pct = roundTripInput;
    inputs.eta_charge = etaChargeInput;
    inputs.eta_discharge = etaDischargeInput;
    var efficiencyNote = el("p", { class: "muted" });
    function displayPct(value) { return value.toFixed(1) + " %"; }
    function updateEfficiencyNote() {
      var result = batteryEfficiencyPayload(roundTripInput.value, etaChargeInput.value, etaDischargeInput.value);
      if (result.error) {
        efficiencyNote.className = "field-error";
        efficiencyNote.textContent = result.error;
        return;
      }
      efficiencyNote.className = "muted";
      // SPEC §6 worked example, computed live so the copy can never drift from the formula.
      var example = " At a 20 ct/kWh buy price with no wear this breaks even selling at " +
        (20 / (result.status.effective_round_trip_pct / 100)).toFixed(1) + " ct/kWh — 25 ct is only the 80 % case.";
      if (result.status.winner === "advanced") {
        efficiencyNote.textContent = "Advanced per-direction values win: charge " + displayPct(result.payload.eta_charge * 100) +
          " × discharge " + displayPct(result.payload.eta_discharge * 100) + " = " + displayPct(result.status.effective_round_trip_pct) +
          " round trip. The round-trip field is ignored while these are filled." + example;
      } else {
        efficiencyNote.textContent = "Round trip split evenly: " + displayPct(result.status.round_trip_pct) + " → charge " +
          displayPct(result.status.split_pct) + " / discharge " + displayPct(result.status.split_pct) + "." + example;
      }
    }
    [roundTripInput, etaChargeInput, etaDischargeInput].forEach(function (input) { input.addEventListener("input", updateEfficiencyNote); });
    updateEfficiencyNote();
    var advancedSection = el("details", null, [
      el("summary", { text: "Advanced: per-direction efficiency (asymmetric hardware)" }),
      el("p", { class: "field-help", text: "Fill BOTH fields, in percent, only when charge and discharge efficiency differ. Per-direction values win over the round-trip field; their product becomes the round trip. Leave both empty to split the round trip evenly (charge = discharge = sqrt(pct/100))." }),
      el("div", { class: "fields" }, [
        el("label", { class: "field", for: "battery-eta-charge" }, [el("span", { text: "Charge efficiency (%) — advanced" }), etaChargeInput]),
        el("label", { class: "field", for: "battery-eta-discharge" }, [el("span", { text: "Discharge efficiency (%) — advanced" }), etaDischargeInput])
      ])
    ]);
    if (advancedPrefill.charge) advancedSection.open = true;
    var efficiencyBlock = el("div", null, [
      el("h3", { text: "Efficiency" }),
      el("label", { class: "field", for: "battery-round-trip" }, [
        el("span", { text: "Round-trip efficiency (%)" }),
        el("span", { class: "field-help", text: "Primary efficiency input; must be greater than 0 and at most 100. The backend splits it into equal charge and discharge efficiencies." }),
        roundTripInput
      ]),
      advancedSection,
      efficiencyNote
    ]);

    var enabledInput = el("input", { type: "checkbox", id: "battery-enabled", checked: fields.enabled });
    inputs.enabled = enabledInput;

    var mappingHost = el("div", { class: "fields" });
    function renderMappingRows() {
      clear(mappingHost);
      var roles = BATTERY_ROLES[driverSelect.value] || BATTERY_ROLES.generic;
      roles.forEach(function (role) {
        var input = el("input", { type: "text", value: mapping[role[0]] || "", placeholder: "entity id (empty = unused)" });
        input.addEventListener("input", function () { mapping[role[0]] = input.value.trim(); });
        inputs["map:" + role[0]] = input;
        mappingHost.appendChild(el("label", { class: "field" }, [el("span", { text: role[1] }), el("span", { class: "field-key", text: role[0] }), input]));
      });
      Object.keys(NON_ENTITY_ROLES).forEach(function (key) {
        var input = el("input", { type: "text", value: mapping[key] || "", placeholder: "e.g. manual" });
        input.addEventListener("input", function () { mapping[key] = input.value.trim(); });
        mappingHost.appendChild(el("label", { class: "field" }, [el("span", { text: NON_ENTITY_ROLES[key] }), el("span", { class: "field-key", text: key }), input]));
      });
    }
    renderMappingRows();
    driverSelect.addEventListener("change", renderMappingRows);

    var searchInput = el("input", { type: "search", placeholder: "filter entity candidates (optional)", "aria-label": "Search entity candidates" });
    var discoveryHost = el("div");
    var discoveryNote = el("p", { class: "legend-note", text: "Discovery reads the Home Assistant entity registry; at most 50 rows per request." });
    var discover = button("Find candidate entities", "", function () {
      performWrite(discover, discoveryHost, function () {
        return get("/discover", { kind: "battery", search: String(searchInput.value || "").trim() });
      }, function (res) {
        renderCandidates(res.candidates || []);
        return "Candidates loaded from the server.";
      });
    });

    function renderCandidates(candidates) {
      clear(discoveryHost);
      if (!candidates.length) {
        discoveryHost.appendChild(el("p", { class: "muted", text: "No candidates matched. You can still type entity ids manually above." }));
        return;
      }
      var roles = BATTERY_ROLES[driverSelect.value] || BATTERY_ROLES.generic;
      var rows = candidates.map(function (candidate) {
        var select = el("select", { "aria-label": "Assign role for " + candidate.entity_id });
        select.appendChild(el("option", { value: "", text: "(ignore)" }));
        var matched = false;
        roles.forEach(function (role) {
          var option = el("option", { value: role[0], text: role[0] });
          if (role[0] === candidate.suggested_role) { option.selected = true; matched = true; }
          select.appendChild(option);
        });
        select.addEventListener("change", function () {
          if (!select.value) return;
          var input = inputs["map:" + select.value];
          if (input) {
            input.value = candidate.entity_id;
            mapping[select.value] = candidate.entity_id;
            setNote(discoveryHost, candidate.entity_id + " assigned to " + select.value + ". Confirm it belongs to this battery before saving.", false);
          }
        });
        return [candidate.entity_id, candidate.name || "", candidate.platform || "", candidate.suggested_role + (matched ? " (suggested)" : ""), isNum(candidate.confidence) ? candidate.confidence.toFixed(2) : "--", select];
      });
      discoveryHost.appendChild(table("Entity candidates", ["Entity", "Name", "Platform", "Suggested role", "Confidence", "Assign"], rows));
    }

    // ---- discovery picker: find a battery and fill the form in one click ---------------------
    var pickerNotes = el("div");
    var pickerHost = el("div");
    var pickerSearch = el("input", { type: "search", placeholder: "device name, model, platform or entity id", "aria-label": "Search batteries" });
    var findBatteries = button("Find batteries", "", function () { runBatterySearch(pickerSearch.value); });
    var discoveryRows = null;   // the empty-query discovery result: the local search corpus
    var pendingQuery = "";     // a search typed before that first read came back

    function batteryStatusBadge(group) {
      if (group.controlReady) return badge("control-ready", "ok");
      if (group.readOnlyControl) return badge("control entities are read-only", "err");
      return badge("partial — missing " + group.missing.join(", "), "warn");
    }

    function renderBatteryGroups(groups, rowCount, query) {
      clear(pickerHost);
      if (!groups.length) {
        var needle = String(query || "").trim();
        var message = needle
          ? "No battery matched “" + needle + "”. The id and the entity table below are the manual path."
          : (rowCount
              ? "Discovery returned " + rowCount + " candidate row(s), but no group of them is a battery: rows are grouped per device, so a server that reports no device_id cannot be picked. Type the id and map entities by hand below."
              : "Discovery returned no candidate rows here. Type the id and map entities by hand below.");
        pickerHost.appendChild(el("p", { class: "muted", text: message }));
        return;
      }
      pickerHost.appendChild(el("div", { class: "battery-picker" }, [table("Batteries discovered in Home Assistant", ["Battery", "Model / manufacturer", "Platform", "Roles", "Use"], groups.map(function (group) {
        return [
          el("div", null, [
            el("strong", { text: group.label }),
            group.deviceId ? null : el("div", { class: "field-help", text: "entity without a device" })
          ]),
          el("span", { text: [group.model, group.manufacturer].filter(function (part) { return !!part; }).join(" · ") || "—" }),
          el("span", { text: group.platform || "—" }),
          el("div", null, [
            el("div", { text: group.mappable.length + " battery role" + (group.mappable.length === 1 ? "" : "s") + " mappable" }),
            el("div", null, [batteryStatusBadge(group)]),
            group.viaPlatform ? null : el("div", { class: "field-help", text: "matched by role, not by a known battery platform" })
          ]),
          button("Select", "btn-primary", function () { selectBattery(group); })
        ];
      }))]));
    }

    /** Selecting a battery fills id, name, driver, roles and every number discovery states. */
    function selectBattery(group) {
      var selection = batterySelection(group, configuredIds);
      if (!editing) inputs.id.value = selection.id;
      inputs.name.value = selection.name;
      driverSelect.value = selection.driver;
      // Select means "use this battery's discovery mapping": replace the entity keys and keep
      // anything typed by hand for the non-entity roles (mode_manual_value is never discovered).
      // ponytail: switching the driver dropdown afterwards re-renders the rows for that driver, so
      // keys the new driver does not use stay stored but unrendered (the pre-existing behaviour);
      // pressing Select again rebuilds the mapping.
      Object.keys(mapping).forEach(function (key) {
        if (!NON_ENTITY_ROLES[key]) delete mapping[key];
      });
      Object.keys(selection.mapping).forEach(function (key) { mapping[key] = selection.mapping[key]; });
      Object.keys(selection.numbers).forEach(function (key) {
        if (inputs[key]) inputs[key].value = String(selection.numbers[key]);
      });
      renderMappingRows();
      var mapped = Object.keys(selection.mapping).map(function (key) { return key + " = " + selection.mapping[key]; });
      var numbers = [];
      if (isNum(selection.numbers.capacity_kwh)) numbers.push(fmtKwh(selection.numbers.capacity_kwh) + " capacity");
      if (isNum(selection.numbers.max_charge_w)) numbers.push("max charge " + fmtPower(selection.numbers.max_charge_w));
      if (isNum(selection.numbers.max_discharge_w)) numbers.push("max discharge " + fmtPower(selection.numbers.max_discharge_w));
      var unmapped = group.skipped || [];
      setNote(pickerNotes, "Filled from " + selection.name + " (driver " + selection.driver + "): " +
        (mapped.length ? mapped.join(", ") : "no role mapped") +
        (numbers.length ? "; " + numbers.join(", ") : "") +
        (!editing ? "; id set to " + selection.id : "") +
        (unmapped.length ? "; left unmapped because only a read-only sensor matched: " + unmapped.join(", ") : "") +
        ". This is only a suggestion — confirm every entity belongs to this battery before saving.", false);
    }

    function renderBatterySearch(rows, query) {
      var plan = batterySearchGroups(rows, query);
      var count = Array.isArray(rows) ? rows.length : 0;
      if (!plan.fromServer) {
        renderBatteryGroups(plan.groups, count, query);
        return;
      }
      // Nothing matched locally: ask the server. It matches entity text only and caps at 50 rows,
      // so it is the fallback rather than the default path.
      performWrite(findBatteries, pickerNotes, function () {
        return get("/discover", { kind: "battery", search: String(query || "").trim() });
      }, function (res) {
        var candidates = res && Array.isArray(res.candidates) ? res.candidates : [];
        renderBatteryGroups(batteryGroupsFromCandidates(candidates), candidates.length, "");
        return "Searched the Home Assistant entity registry for “" + String(query || "").trim() + "”.";
      });
    }

    function runBatterySearch(query) {
      var needle = String(query == null ? "" : query).trim();
      if (!discoveryRows) {
        // The corpus is always the unfiltered discovery read; a query typed meanwhile is applied
        // to it as soon as it lands.
        pendingQuery = needle;
        loadBatteryCandidates();
        return;
      }
      renderBatterySearch(discoveryRows, needle);
    }

    function loadBatteryCandidates() {
      performWrite(findBatteries, pickerNotes, function () {
        return get("/discover", { kind: "battery", search: "" });
      }, function (res) {
        discoveryRows = res && Array.isArray(res.candidates) ? res.candidates : [];
        var query = pendingQuery;
        pendingQuery = "";
        renderBatterySearch(discoveryRows, query);
        return discoveryRows.length + " discovery row(s) read from Home Assistant; only groups that could be a battery are listed.";
      });
    }

    pickerSearch.addEventListener("change", function () { runBatterySearch(pickerSearch.value); });

    var errorHost = el("div");

    function collect() {
      var out = {
        id: (inputs.id.value.trim() || slugify(inputs.name.value)).toLowerCase(),
        name: inputs.name.value.trim(),
        driver: driverSelect.value,
        capacity_kwh: Number(inputs.capacity_kwh.value),
        min_soc_pct: Number(inputs.min_soc_pct.value),
        max_soc_pct: Number(inputs.max_soc_pct.value),
        max_charge_w: Number(inputs.max_charge_w.value),
        max_discharge_w: Number(inputs.max_discharge_w.value),
        mapping: {},
        enabled: !!inputs.enabled.checked
      };
      var efficiency = batteryEfficiencyPayload(inputs.round_trip_efficiency_pct.value, inputs.eta_charge.value, inputs.eta_discharge.value);
      out.__efficiency = efficiency;
      if (efficiency.payload) {
        Object.keys(efficiency.payload).forEach(function (key) { out[key] = efficiency.payload[key]; });
      }
      Object.keys(mapping).forEach(function (key) {
        var value = String(mapping[key] || "").trim();
        if (value) out.mapping[key] = value;
      });
      return out;
    }

    function validate(config) {
      var problems = [];
      if (!config.name) problems.push("Name is required.");
      if (!config.id || !/^[a-z0-9][a-z0-9_-]*$/.test(config.id)) problems.push("Id must be a lowercase slug (letters, digits, _ or -).");
      if (!(config.capacity_kwh > 0)) problems.push("Capacity must be greater than 0 kWh.");
      if (!(config.min_soc_pct >= 0 && config.min_soc_pct < config.max_soc_pct && config.max_soc_pct <= 100)) problems.push("SoC window must satisfy 0 ≤ min < max ≤ 100 %.");
      if (config.__efficiency && config.__efficiency.error) problems.push(config.__efficiency.error);
      if (!(config.max_charge_w > 0 || config.max_discharge_w > 0)) problems.push("At least one power limit must be greater than 0 W.");
      if (!Object.keys(config.mapping).length) problems.push("Map at least one entity (or the manual mode value) before saving.");
      if (config.driver === "generic" && !config.mapping.power_setpoint_entity && !(config.mapping.charge_power_entity && config.mapping.discharge_power_entity)) {
        problems.push("A generic battery needs either a signed setpoint entity or a charge + discharge entity pair; a setpoint-less battery is rejected at save time.");
      }
      return problems;
    }

    var save = button(editing ? "Review changes" : "Review and save", "btn-primary");
    save.addEventListener("click", function () {
      var config = collect();
      var problems = validate(config);
      clear(errorHost);
      if (problems.length) {
        problems.forEach(function (p) { errorHost.appendChild(el("p", { class: "field-error", text: p })); });
        setNote(errorHost, "Fix the problems above, nothing was sent to the server.", true);
        return;
      }
      var mappingText = Object.keys(config.mapping).map(function (key) { return key + " = " + config.mapping[key]; }).join("; ");
      var efficiencyText = "Efficiency: not set";
      if (config.__efficiency && config.__efficiency.payload) {
        var eff = config.__efficiency;
        efficiencyText = eff.status.winner === "advanced"
          ? "Efficiency: advanced per-direction override — charge " + displayPct(eff.payload.eta_charge * 100) + " × discharge " + displayPct(eff.payload.eta_discharge * 100) + " = " + displayPct(eff.status.effective_round_trip_pct) + " round trip (these win over the round-trip field)"
          : "Efficiency: " + displayPct(eff.status.round_trip_pct) + " round trip, split evenly (charge = discharge = " + displayPct(eff.status.split_pct) + ")";
      }
      var payloadConfig = Object.assign({}, config);
      delete payloadConfig.__efficiency;
      confirmAction({
        title: (editing ? "Save changes to " : "Add ") + config.name + "?",
        body: "Driver " + config.driver + ", " + config.capacity_kwh + " kWh, " + config.min_soc_pct + "–" + config.max_soc_pct + " % SoC. " + efficiencyText + ". Mapping: " + mappingText,
        confirmLabel: editing ? "Save changes" : "Add battery",
        requireCheck: true,
        checkLabel: "I confirm these entities belong to this battery"
      }).then(function (ok) {
        if (!ok) return;
        performWrite(save, body, function () {
          return editing
            ? patch("/batteries/" + encodeURIComponent(source.id), { patch: payloadConfig })
            : post("/batteries", { config: payloadConfig });
        }, function () { return "Battery saved (confirmed by the server)."; }, function () {
          dlg.close();
          if (reload) reload(); else renderRoute();
        });
      });
    });

    append(body, [
      el("h2", { text: editing ? "Edit battery" : "Add battery" }),
      el("p", { class: "muted", text: "Discovery only suggests; the mapping is stored after your explicit confirmation." }),
      el("h3", { text: "Find a battery" }),
      el("p", { class: "field-help", text: "Pick a discovered battery to fill the id, name, driver, roles and every number Home Assistant states. Only groups that could be a battery are listed — devices that merely mention a battery level are not — and nothing is chosen until you press Select." }),
      el("div", { class: "toolbar" }, [
        el("label", { class: "field" }, [el("span", { text: "Search batteries" }), pickerSearch]),
        findBatteries
      ]),
      pickerNotes,
      pickerHost,
      identity,
      efficiencyBlock,
      el("p", null, [enabledInput, el("label", { for: "battery-enabled", text: " Enabled for dispatch" })]),
      el("h3", { text: "Entity mapping" }),
      mappingHost,
      el("h3", { text: "Discovery" }),
      el("div", { class: "toolbar" }, [searchInput, discover]),
      discoveryNote,
      discoveryHost,
      errorHost
    ]);
    append(dlg, [body, el("div", { class: "dialog-actions" }, [button("Cancel", "", function () { dlg.close(); }), save])]);
    document.body.appendChild(dlg);
    dlg.addEventListener("close", function () { dlg.remove(); });
    loadBatteryCandidates();
    dlg.showModal();
    return dlg;
  }

  /* ------------------------------------------------------------------ view: savings */

  // Source of truth: energy_manager/app/analytics.py BASELINE_NOTE (returned as `baseline_note`).
  var BASELINE_FALLBACK = "Baseline: the same measured consumption at the same prices with the battery doing nothing. Savings = baseline cost - actual cost.";

  function renderSavingsTotals(body, payload) {
    var t = payload.totals || {};
    var savingsKnown = isNum(t.savings_eur);
    var baselineKnown = isNum(t.baseline_cost_eur);
    var reason = (!savingsKnown || !baselineKnown) ? "unknown — no load forecast for this range" : "";
    var cards = el("div", { class: "cards" }, [
      statCard("Savings", fmtEur(t.savings_eur), savingsKnown ? "baseline minus actual (net)" : reason, !savingsKnown || t.savings_eur < 0),
      statCard("Import cost", fmtEur(t.cost_eur), "grid import cost before export revenue"),
      statCard("Baseline cost", fmtEur(t.baseline_cost_eur), baselineKnown ? "same load and tariffs, battery idle (net)" : reason, !baselineKnown),
      statCard("Import", fmtKwh(t.import_kwh), "from the grid"),
      statCard("Export", fmtKwh(t.export_kwh), "to the grid"),
      statCard("PV", fmtKwh(t.pv_kwh), "produced"),
      statCard("Cycles", isNum(t.cycles) ? t.cycles.toFixed(2) + " cycles" : "--", "battery throughput")
    ]);
    body.appendChild(cards);
    if (!savingsKnown || !baselineKnown) {
      body.appendChild(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Baseline and savings are unknown for this range" }),
        el("p", { text: "The server reports null because there is no load forecast to re-price the metered intervals against. Unknown is not zero: the add-on will not guess a saving without the load. Buckets that are knowable are still summed and shown." })
      ]));
    }
    if (isNum(t.arbitrage_kwh) || isNum(t.self_consumption_kwh)) {
      body.appendChild(el("ul", { class: "stat-row" }, [
        el("li", null, [el("span", { class: "stat-label", text: "Battery energy for arbitrage" }), el("span", { class: "stat-value", text: fmtKwh(t.arbitrage_kwh) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Battery energy for self-consumption" }), el("span", { class: "stat-value", text: fmtKwh(t.self_consumption_kwh) })])
      ]));
    } else {
      body.appendChild(el("p", { class: "legend-note", text: "This backend version does not report the arbitrage / self-consumption split yet." }));
    }
    body.appendChild(el("div", { class: "banner banner-info" }, [
      el("p", { class: "banner-title", text: "How the baseline is defined" }),
      el("p", { text: payload.baseline_note || BASELINE_FALLBACK }),
      el("p", { class: "legend-note", text: "The baseline is a do-nothing net cost: import cost minus export revenue for the same realised load, PV and tariff with no battery flows. It is a counterfactual re-pricing of the same kWh, not a measured bill change." })
    ]));
  }

  function renderSavingsBuckets(body, payload) {
    var buckets = Array.isArray(payload.buckets) ? payload.buckets : [];
    if (!buckets.length) { body.appendChild(el("p", { class: "muted", text: "No buckets in this range yet." })); return; }
    var rows = buckets.map(function (b) {
      return [b.key || "", fmtDateTime(b.start) + " – " + fmtDateTime(b.end), fmtKwh(b.import_kwh), fmtKwh(b.export_kwh), fmtKwh(b.pv_kwh), fmtEur(b.cost_eur), fmtEur(b.revenue_eur), fmtEur(b.baseline_cost_eur), fmtEur(b.savings_eur), isNum(b.cycles) ? b.cycles.toFixed(2) + " cycles" : "--"];
    });
    body.appendChild(table("Buckets for range " + payload.range, ["Bucket", "Period", "Import", "Export", "PV", "Cost", "Revenue", "Baseline", "Savings", "Cycles"], rows));
  }

  function viewSavings(host, query) {
    var ranges = ["day", "week", "month", "year", "all"];
    var range = ranges.indexOf(query.range) >= 0 ? query.range : "month";
    var load = once(function () { return get("/savings", { range: range }); });

    var controls = panel(host, "Savings range");
    var select = el("select", { id: "savings-range" });
    ranges.forEach(function (r) {
      var option = el("option", { value: r, text: r });
      if (r === range) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function () { root.location.hash = "#/savings?range=" + select.value; });
    controls.body.appendChild(el("div", { class: "toolbar" }, [el("label", { for: "savings-range" }, ["Range ", select])]));
    controls.body.appendChild(el("p", { class: "legend-note", text: "Savings are computed from the stored actuals and the same prices, so trimming history never changes them. Baseline and savings are null (shown as --) when there is no load forecast to re-price against." }));

    asyncPanel(host, "Totals", { load: load, render: renderSavingsTotals });
    asyncPanel(host, "Savings vs baseline", {
      load: load,
      render: function (body, payload) {
        var chart = el("div", { class: "chart chart-md" });
        body.appendChild(chart);
        root.SEMCharts.savings(chart, payload.buckets || []);
        body.appendChild(el("p", { class: "legend-note", text: "Green bars: savings (EUR, left axis). Dashed lines: actual net cost (import cost minus export revenue) and no-battery baseline cost (EUR, right axis). Buckets with a null baseline (no load forecast) are skipped, not plotted as zero." }));
      }
    });
    asyncPanel(host, "Per-bucket detail", { load: load, render: renderSavingsBuckets });
    return null;
  }

  /* ------------------------------------------------------------------ view: settings */

  var SAFETY_KEY_RE = /(reserve_soc_pct|max_grid_import_w|max_grid_export_w|target_soc_pct_end|allow_grid_charge|allow_export|min_spread_eur_per_kwh|grid_follow_max_w|^control\.|^price\.feed_in_)/;

  // The API returns settings sorted by key, which interleaves groups; present them in a stable
  // reading order instead (unknown groups go last, alphabetically).
  var GROUP_ORDER = ["General", "Price", "Solar", "Load", "Grid", "Optimizer", "Control", "Safety", "Notify", "Batteries"];

  function isSafetySetting(descriptor) {
    return /safety|limit/i.test(String(descriptor.group || "")) || SAFETY_KEY_RE.test(descriptor.key);
  }

  // localStorage for the settings IA (active group, disclosure state, "show all advanced") and the
  // theme override. Guarded because storage can be unavailable (private mode) or absent (node).
  function readStored(key, fallback) {
    try {
      if (typeof localStorage === "undefined") return fallback;
      var raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (err) { return fallback; }
  }
  function writeStored(key, value) {
    try { if (typeof localStorage !== "undefined") localStorage.setItem(key, JSON.stringify(value)); }
    catch (err) { /* storage may be unavailable: keep the in-memory state only */ }
  }

  function settingValueEqual(a, b) {
    if (a === b) return true;
    if (a && b && typeof a === "object" && typeof b === "object") return JSON.stringify(a) === JSON.stringify(b);
    return false;
  }

  /** A descriptor's effective value: an explicit server/edited value wins over its default. */
  function settingEffectiveValue(item) {
    if (!item) return undefined;
    return item.value === undefined ? item.default : item.value;
  }

  /** Is this key switched off by the price/solar visibility tables for the current selection?
   *  Pure, and never true for a Safety row (a safety key is always applicable). */
  function settingHiddenBySource(item, opts) {
    if (!item || isSafetySetting(item)) return false;
    var group = item.group || "Other";
    if (group === "Price") return priceFieldVisibility(opts.priceProvider, opts.priceContract).fields.indexOf(item.key) < 0;
    if (group === "Solar") return solarFieldVisibility(opts.solarSource).fields.indexOf(item.key) < 0;
    return false;
  }

  /** The old filter surface: key, label, help and group name, case-insensitive. */
  function settingMatchesQuery(item, query) {
    var needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    return (item.key + " " + (item.label || "") + " " + (item.help || "") + " " + (item.group || "")).toLowerCase().indexOf(needle) >= 0;
  }

  /** Should this settings row be shown at all? Pure (no DOM): the tested surface for the settings
   *  IA. A query reveals every match (folded and source-hidden alike); otherwise a folded advanced
   *  row stays behind its group's disclosure, the price/solar tables hide fields that cannot apply
   *  unless they hold a non-default value, and Safety rows are never folded or source-hidden.
   *  opts = {showAdvanced, showAll, query, priceProvider, priceContract, solarSource}; showAdvanced
   *  is this group's disclosure state (a boolean, or a {group: bool} map for convenience). */
  function settingsVisible(item, opts) {
    opts = opts || {};
    // ponytail: a non-empty query reveals every match, folded and source-hidden rows included.
    if (opts.query) return settingMatchesQuery(item, opts.query);
    if (isSafetySetting(item)) return true;
    var open = opts.showAdvanced;
    // ponytail: an object lets one call decide for a whole rail; a scalar is this row's group.
    if (open && typeof open === "object") open = !!open[(item && item.group) || "Other"];
    if (item && item.advanced === true && !open && !opts.showAll) return false;
    if (settingHiddenBySource(item, opts)) return !settingValueEqual(settingEffectiveValue(item), item.default);
    return true;
  }

  /** One entry per server group for the settings rail: its rows, the rows that fold behind the
   *  group's "Advanced (n)" disclosure, and how many of its keys differ from their defaults.
   *  Pure (no DOM): the tested surface for the settings IA. Stable order = GROUP_ORDER, then the
   *  unknown groups alphabetically. */
  function settingsGroups(items) {
    var list = Array.isArray(items) ? items : [];
    var byName = {}, groups = [];
    list.forEach(function (item) {
      var name = (item && item.group) || "Other";
      if (!byName[name]) { byName[name] = { name: name, rows: [], advancedRows: [], changedCount: 0 }; groups.push(byName[name]); }
      var group = byName[name];
      group.rows.push(item);
      // Safety rows never fold (SPEC 13.2), so they are not part of any disclosure's count.
      if (item.advanced === true && (item.group || "") !== "Safety") group.advancedRows.push(item);
      if (!settingValueEqual(settingEffectiveValue(item), item.default)) group.changedCount++;
    });
    groups.sort(function (a, b) {
      var ia = GROUP_ORDER.indexOf(a.name), ib = GROUP_ORDER.indexOf(b.name);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    });
    return groups;
  }

  /* ---- price provider/contract field visibility (SPEC §4.1) --------------------------------
   * Which price.* fields the settings form reveals depends on price.provider (where prices come
   * from) and price.contract (their shape): asking for a bidding zone on a fixed tariff, or an
   * entity on the auto-found Zonneplan sensor, is noise. This declarative table plus its pure
   * reader decide the visible set. Visibility is a *convenience* only — renderSettings still
   * surfaces any hidden field that holds a non-default value, so nothing takes effect invisibly. */

  // Meaningful for every provider/contract: the adders/fees that turn a wholesale price into a
  // consumer price, the declared unit/scale, and the export (feed-in) tariff path.
  var PRICE_FIELDS_ALWAYS = [
    "price.vat_pct", "price.energy_tax_eur_per_kwh", "price.import_fee_eur_per_kwh",
    "price.export_fee_eur_per_kwh", "price.unit", "price.scale_divisor",
    "price.feed_in_eur_per_kwh", "price.feed_in_entity", "price.feed_in_tax_inclusive"
  ];
  // provider=entity (the pre-0.3.0 explicit path): an advanced source + entity, with a static
  // schedule as the configured->energy_charts->static tail.
  var PRICE_FIELDS_ENTITY = ["price.source", "price.entity", "price.static_schedule"];
  // provider=day_ahead: the bidding zone is the only extra field.
  var PRICE_FIELDS_DAY_AHEAD = ["price.bzn"];
  // provider=fixed, contract=fixed_single: one consumer rate (typed value, or the entity that wins).
  var PRICE_FIELDS_FIXED_SINGLE = ["price.fixed_price_eur_per_kwh", "price.fixed_price_entity"];
  // provider=fixed, contract=fixed_day_night: day + night rates and the two window start hours.
  var PRICE_FIELDS_FIXED_DAY_NIGHT = [
    "price.fixed_day_price_eur_per_kwh", "price.fixed_night_price_eur_per_kwh",
    "price.fixed_day_price_entity", "price.fixed_night_price_entity",
    "price.fixed_day_start_hour", "price.fixed_night_start_hour"
  ];
  // The one-line note under the provider select, explaining the selection so a user who later sees
  // an explained empty forecast knows where to look. zonneplan is auto-found; day_ahead is wholesale
  // and its zone coverage can legitimately be empty (hint the forecast then shows).
  var PRICE_PROVIDER_NOTES = {
    entity: null,
    zonneplan: "The Zonneplan tariff sensor is located in Home Assistant automatically (from its price-list attribute and 15-minute/hourly slot spacing) — there is no entity to pick here.",
    day_ahead: "Wholesale day-ahead prices from energy-charts for the bidding zone below. They are ex-VAT and ex-tax, so the VAT, energy-tax and fee fields turn them into consumer prices. Zone coverage matters: a valid zone may simply publish no prices for this window, and the forecast then shows a hint naming that zone — if so, pick your supplier's own source (zonneplan or a price entity) instead.",
    fixed: null
  };
  var PRICE_FIXED_NOTES = {
    fixed_single: "One consumer price (incl. VAT and tax) for the whole day. A missing fixed price fails loudly — it is never guessed from the market.",
    fixed_day_night: "Two consumer prices: the day rate runs from fixed_day_start_hour until fixed_night_start_hour, the night rate for the rest. Evaluated per slot in your timezone, so DST follows the window, not an hour count."
  };

  /** Visible price.* keys for a provider/contract pair, in render order, plus the note to show.
   *  Pure (no DOM) so node tests can assert the table. contract only changes the fixed fields. */
  function priceFieldVisibility(provider, contract) {
    var p = String(provider || "entity");
    var c = String(contract || "dynamic_hourly");
    var keys = ["price.provider", "price.contract"].concat(PRICE_FIELDS_ALWAYS);
    var note = PRICE_PROVIDER_NOTES[p] != null ? PRICE_PROVIDER_NOTES[p] : null;
    if (p === "zonneplan") {
      // No price.entity: the tariff sensor is found structurally, never by entity id.
    } else if (p === "day_ahead") {
      keys = keys.concat(PRICE_FIELDS_DAY_AHEAD);
    } else if (p === "fixed") {
      keys = keys.concat(c === "fixed_day_night" ? PRICE_FIELDS_FIXED_DAY_NIGHT : PRICE_FIELDS_FIXED_SINGLE);
      if (PRICE_FIXED_NOTES[c] != null) note = PRICE_FIXED_NOTES[c];
    } else {
      // entity (default), and the fallback for any unknown provider, so fields never vanish.
      keys = keys.concat(PRICE_FIELDS_ENTITY);
    }
    return { fields: keys, note: note || null };
  }

  /* ---- solar source field visibility (SPEC §7.1.1) --------------------------------------------
   * solar.source picks the estimation model: forecast_solar is the Forecast.Solar API and needs
   * per-plane geometry, capacity estimates from the array's watt peak alone (AC-coupled arrays whose
   * geometry the user does not want to model). Which solar.* keys the form reveals follows from it,
   * with the same two rules the price group uses: the selector and everything both sources need
   * stay visible, and renderSettings still surfaces any hidden field that holds a non-default
   * value, so a value this source ignores can never act invisibly. */

  // Meaningful for both sources: the on/off switch, the selector itself, the bias learning and the
  // measured PV entity that learning is fitted to (the accuracy of both sources improves with it).
  var SOLAR_FIELDS_ALWAYS = ["solar.source", "solar.enabled", "solar.bias_correction", "solar.measured_entity"];
  // forecast_solar: the API call — per-plane geometry, plus the optional own key (keyless is
  // single-plane hourly, which is why planes is the field that really matters).
  var SOLAR_FIELDS_FORECAST_SOLAR = ["solar.planes", "solar.api_key"];
  // capacity: the array's total watt peak, the geometry the model assumes while it is unset, the
  // performance ratio and the optional weather entity for cloud attenuation.
  var SOLAR_FIELDS_CAPACITY = [
    "solar.capacity_wp", "solar.tilt_deg", "solar.azimuth_deg", "solar.performance_ratio",
    "solar.weather_entity"
  ];
  // The one-line note under the source select, so a user who later reads an explained forecast
  // knows which model produced it. Capacity states its assumptions because they are stated (not
  // silent) in the forecast notes too (SPEC §7.1.1).
  var SOLAR_SOURCE_NOTES = {
    forecast_solar: "Per-plane geometry (kWp, tilt, azimuth): the Forecast.Solar API is asked per plane, so each plane is modelled on its own tilt and direction.",
    capacity: "Estimate from the array's total watt peak: enter the Wp and the add-on models production from the sun's position, optionally attenuated by a weather entity's cloud cover; leave tilt/azimuth unset and it assumes south-facing at 35 degrees and says so in the forecast notes.",
    derived: "Estimate from the meter: at every moment the meter exports, the array is producing at least the exported power plus whatever the battery is charging (pv = export + battery charge). The add-on learns the array's watt peak from those moments and then models it like the capacity source. Nothing to enter — it needs the Meter card and a battery whose charge/discharge power is mapped. The house load at those moments is not subtracted, so the learned size is a conservative lower bound."
  };

  /** Visible solar.* keys for a source, plus the note to show.
   *  Pure (no DOM) so node tests can assert the table. */
  function solarFieldVisibility(source) {
    var s = String(source || "forecast_solar");
    // derived runs the capacity model on a watt peak learned from the meter, so the geometry,
    // performance ratio and weather fields apply there too (an explicit solar.capacity_wp wins).
    var keys = SOLAR_FIELDS_ALWAYS.concat(
      s === "forecast_solar" ? SOLAR_FIELDS_FORECAST_SOLAR : SOLAR_FIELDS_CAPACITY
    );
    // An unknown source keeps the forecast_solar fields (never an empty group), the same fallback
    // the price table uses, so a newer backend value cannot hide every field.
    var note = SOLAR_SOURCE_NOTES[s] != null ? SOLAR_SOURCE_NOTES[s] : null;
    return { fields: keys, note: note || null };
  }

  /* ---- setup cards (SPEC 13.2, amended) -------------------------------------------------------
   * The settings page opens on four setup cards — Price, Meter, Solar, Battery — that carry only
   * the primary choice each; every other key (including the rest of a card's own group) lives in
   * the "All settings" drawer behind the existing rail. A key rendered on a card is *not* rendered
   * again in its group, so every key still appears exactly once. Pure (no DOM): the tested surface
   * for which keys the cards own at the current provider/contract/solar-source selection. */
  function setupCardKeys(opts) {
    opts = opts || {};
    var provider = String(opts.priceProvider || "entity");
    var contract = String(opts.priceContract || "dynamic_hourly");
    var source = String(opts.solarSource || "forecast_solar");
    var keys = [
      "price.provider", "price.export_fee_eur_per_kwh", "price.feed_in_energy_tax_refund",
      "grid.power_entity", "solar.enabled", "solar.source"
    ];
    if (provider === "entity") keys.push("price.entity");
    if (provider === "day_ahead") keys.push("price.bzn");
    if (provider === "fixed") {
      keys.push("price.contract");
      if (contract === "fixed_day_night") {
        keys.push("price.fixed_day_price_eur_per_kwh", "price.fixed_night_price_eur_per_kwh");
      } else {
        keys.push("price.fixed_price_eur_per_kwh");
      }
    }
    if (source !== "derived") keys.push("solar.measured_entity");
    return keys;
  }

  // Two guidance lines that live under the Solar group instead of in a field's help text, because
  // they are about the entity itself rather than the typed value: what the measured-PV learning is
  // fitted to, and what the weather entity has to support for cloud attenuation to work at all.
  var SOLAR_MEASURED_NOTE = "solar.measured_entity must be the inverter's AC output: a meter's export register under-reports, because self-consumed energy never reaches the meter, so the learned per-hour correction would come out low. It is what makes the forecast more accurate over time.";
  var SOLAR_WEATHER_NOTE = "solar.weather_entity must support the weather.get_forecasts action (Met.no's forecast entity does; some integrations, including the OpenWeatherMap entity on this install, only publish a current cloud_coverage and support no forecast type) — without a forecast type the add-on holds the current cloud cover flat and says so in the forecast notes, so the field is still worth setting.";

  function settingsField(descriptor, dirty) {
    var key = descriptor.key;
    var type = descriptor.type || "str";
    var wrap = el("div", { class: "field", dataset: { key: key } });
    var input;
    var value = descriptor.value === undefined ? descriptor.default : descriptor.value;

    if (type === "bool") {
      input = el("input", { type: "checkbox", id: "set-" + key, checked: !!value });
      input.addEventListener("change", function () { dirty(key, input.checked, wrap); });
      wrap.appendChild(el("label", { for: "set-" + key }, [input, " ", descriptor.label || key]));
    } else if (type === "list" && Array.isArray(descriptor.choices)) {
      var chosen = Array.isArray(value) ? value.slice() : [];
      input = el("div");
      descriptor.choices.forEach(function (choice) {
        var box = el("input", { type: "checkbox", checked: chosen.indexOf(choice) >= 0, id: "set-" + key + "-" + choice });
        box.addEventListener("change", function () {
          var next = (Array.isArray(chosen) ? chosen : []).filter(function (c) { return c !== choice; });
          if (box.checked) next.push(choice);
          chosen = next;
          dirty(key, next, wrap);
        });
        input.appendChild(el("label", { for: "set-" + key + "-" + choice }, [box, " ", choice]));
        input.appendChild(el("br"));
      });
      wrap.appendChild(el("span", { class: "field-label", text: descriptor.label || key }));
    } else if (type === "json") {
      input = el("textarea", { id: "set-" + key });
      try { input.value = JSON.stringify(value === undefined ? descriptor.default : value, null, 2); }
      catch (err) { input.value = "null"; }
      input.addEventListener("input", function () {
        var parsed = null, problem = null;
        try { parsed = JSON.parse(input.value); }
        catch (err) { problem = "Invalid JSON: " + err.message; }
        showFieldError(wrap, problem);
        dirty(key, parsed, wrap, !!problem);
      });
      wrap.appendChild(el("label", { for: "set-" + key, text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(input);
    } else if (type === "password") {
      input = el("input", { type: "password", id: "set-" + key, placeholder: value ? "(a value is set; leave empty to keep it)" : "" });
      input.addEventListener("input", function () { dirty(key, input.value, wrap); });
      wrap.appendChild(el("label", { for: "set-" + key, text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(input);
    } else if (type === "int" || type === "float") {
      input = el("input", { type: "number", id: "set-" + key, value: value === null || value === undefined ? "" : String(value), step: type === "int" ? "1" : "any" });
      input.addEventListener("input", function () {
        var raw = input.value.trim();
        if (raw === "") { dirty(key, null, wrap); return; }
        var number = Number(raw);
        showFieldError(wrap, isFinite(number) ? null : "Not a number");
        dirty(key, isFinite(number) ? number : undefined, wrap, !isFinite(number));
      });
      wrap.appendChild(el("label", { for: "set-" + key, text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(input);
    } else if (Array.isArray(descriptor.choices) && descriptor.choices.length) {
      // ponytail: the API always sends choices, as [] when there is no enum, so empty means free text.
      input = el("select", { id: "set-" + key });
      descriptor.choices.forEach(function (choice) {
        var option = el("option", { value: choice, text: choice });
        if (String(value) === String(choice)) option.selected = true;
        input.appendChild(option);
      });
      input.addEventListener("change", function () { dirty(key, input.value, wrap); });
      wrap.appendChild(el("label", { for: "set-" + key, text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(input);
    } else {
      input = el("input", { type: "text", id: "set-" + key, value: value === null || value === undefined ? "" : String(value) });
      input.addEventListener("input", function () { dirty(key, input.value, wrap); });
      wrap.appendChild(el("label", { for: "set-" + key, text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(input);
    }
    if (descriptor.help) wrap.appendChild(el("p", { class: "field-help", text: descriptor.help }));
    if (descriptor.key === "batteries") wrap.appendChild(el("p", { class: "field-help" }, [el("a", { href: "#/batteries", text: "Edit batteries in the Batteries view" })]));
    return wrap;
  }

  function showFieldError(wrap, message) {
    var existing = wrap.querySelector(".field-error");
    if (!message) { if (existing) existing.remove(); return; }
    if (existing) { existing.textContent = message; return; }
    wrap.appendChild(el("p", { class: "field-error", text: message }));
  }

  /* ---- entity picker (SPEC 10 discovery, used by the setup cards) -----------------------------
   * One reusable searchable picker: it asks GET /discover for the kind's candidates and writes the
   * entity id into a settings key through the card's dirty() callback. A candidate row's own
   * setting_key wins (an export-only grid sensor is saved to grid.export_entity, not to the card's
   * grid.power_entity), and a typed entity id can always be used directly, so a sensor discovery
   * ranks poorly is still reachable. */
  function entityPicker(options) {
    var kind = options.kind;
    var key = options.key;
    var label = options.label || key;
    var wrap = el("div", { class: "picker" });
    wrap.appendChild(el("p", { class: "picker-current" }, [
      el("span", { class: "field-key", text: key }), " ",
      el("span", { text: options.value ? String(options.value) : "not set" })
    ]));
    var input = el("input", { type: "search", placeholder: options.placeholder || "name, platform or entity id", "aria-label": "Search " + label });
    var find = button("Find", "");
    var results = el("div", { class: "picker-results" });

    function pick(row) {
      options.onPick(row.setting_key || key, String(row.entity_id));
    }

    function typedRow() {
      var typed = String(input.value || "").trim();
      if (!typed || typed.indexOf(".") < 0) return null;
      return el("div", { class: "picker-row" }, [
        el("div", { class: "picker-text" }, [el("div", { class: "picker-name", text: typed })]),
        button("Use typed id", "", function () { pick({ entity_id: typed }); })
      ]);
    }

    function rowFor(candidate) {
      var confidence = isNum(candidate.confidence) ? Math.round(candidate.confidence * 100) + "%" : null;
      var meta = [candidate.entity_id, candidate.platform, candidate.suggested_role, confidence].filter(Boolean).join(" · ");
      return el("div", { class: "picker-row" }, [
        el("div", { class: "picker-text" }, [
          el("div", { class: "picker-name", text: String(candidate.name || candidate.entity_id) }),
          el("div", { class: "picker-meta", text: meta })
        ]),
        button("Use", "", function () { pick(candidate); })
      ]);
    }

    function load() {
      clear(results);
      results.appendChild(el("p", { class: "muted", text: "Searching…" }));
      find.disabled = true;
      get("/discover", { kind: kind, search: String(input.value || "").trim() }).then(function (payload) {
        find.disabled = false;
        var rows = payload && Array.isArray(payload.candidates) ? payload.candidates : [];
        clear(results);
        if (!rows.length) results.appendChild(el("p", { class: "muted", text: "No candidates matched. Type a full entity id and use it directly if you know it." }));
        rows.slice(0, 12).forEach(function (candidate) { results.appendChild(rowFor(candidate)); });
        var typed = typedRow();
        if (typed) results.appendChild(typed);
      }).catch(function (err) {
        find.disabled = false;
        clear(results);
        results.appendChild(el("p", { class: "muted", text: "Discovery failed: " + (err && err.message ? err.message : err) }));
        var typed = typedRow();
        if (typed) results.appendChild(typed);
      });
    }

    find.addEventListener("click", load);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); load(); }
    });
    wrap.appendChild(el("div", { class: "picker-search" }, [input, find]));
    wrap.appendChild(results);
    return wrap;
  }

  function renderSettings(body, config) {
    var descriptors = Array.isArray(config.settings) ? config.settings : [];
    if (!descriptors.length) { body.appendChild(el("p", { class: "muted", text: "The server sent no setting descriptors." })); return; }
    var dirtyValues = {};
    var dirtyCount = 0;
    // Keys the four setup cards render this draw; their groups skip them so a key appears once.
    var cardKeys = {};

    // Remembered UI state (SPEC 13.2): active rail group, each group's disclosure, "show all
    // advanced". localStorage is the only store — the server payload is untouched by folding.
    var STORE_GROUP = "sem.settings.group";
    var STORE_ADVANCED = "sem.settings.advanced";
    var STORE_SHOW_ALL = "sem.settings.showAllAdvanced";
    var STORE_ALL_OPEN = "sem.settings.allOpen";
    var advancedOpen = readStored(STORE_ADVANCED, {});
    if (!advancedOpen || typeof advancedOpen !== "object") advancedOpen = {};
    var showAllAdvanced = readStored(STORE_SHOW_ALL, false) === true;
    var activeGroup = readStored(STORE_GROUP, null);
    var railEntries = {};

    function dirty(key, value, wrap, invalid) {
      dirtyValues[key] = { value: value, invalid: !!invalid };
      wrap.classList.add("field-dirty");
      if (invalid) wrap.setAttribute("data-invalid", "1");
      else wrap.removeAttribute("data-invalid");
      dirtyCount = Object.keys(dirtyValues).length;
      updateCounts();
      // Changing the price provider/contract or the solar source changes which fields make sense,
      // so re-render the group now (the new value is already in dirtyValues, so the preview updates
      // immediately). Editing a source-hidden key can add or drop its muted "hidden but set" line,
      // so that re-renders too.
      var descriptor = descriptorByKey(key);
      if (key === "price.provider" || key === "price.contract" || key === "solar.source" ||
          (descriptor && settingHiddenBySource(descriptor, currentOpts()))) draw();
    }

    function descriptorByKey(key) {
      return descriptors.filter(function (d) { return d.key === key; })[0] || null;
    }
    // The field's current value: an unsaved edit wins over the server value, the server value over
    // its default. Used for the visibility preview and the hidden-but-set warning.
    function settingValue(key) {
      if (Object.prototype.hasOwnProperty.call(dirtyValues, key)) return dirtyValues[key].value;
      var d = descriptorByKey(key);
      return d ? settingEffectiveValue(d) : undefined;
    }
    // Every visibility decision runs on merged rows, so an edit (or the selector it changes) is
    // reflected immediately even though the server payload is unchanged.
    function mergeItem(descriptor) {
      return Object.prototype.hasOwnProperty.call(dirtyValues, descriptor.key)
        ? Object.assign({}, descriptor, { value: dirtyValues[descriptor.key].value })
        : descriptor;
    }
    function mergedItems() { return descriptors.map(mergeItem); }
    function currentOpts() {
      return {
        showAdvanced: advancedOpen,
        showAll: showAllAdvanced,
        query: search.value,
        priceProvider: settingValue("price.provider"),
        priceContract: settingValue("price.contract"),
        solarSource: settingValue("solar.source")
      };
    }
    function settingValueText(value) {
      if (value === null || value === undefined) return "unset";
      if (typeof value === "object") return JSON.stringify(value);
      return String(value);
    }
    // One row, rendered exactly once. A source-hidden row is still listed when it holds a value
    // (muted, "not used by the current selection"); a search match also labels its group.
    function renderRow(item, opts, searching) {
      var row;
      if (settingHiddenBySource(item, opts)) {
        row = el("p", { class: "muted", dataset: { key: item.key } }, [
          "Hidden " + item.key + " = " + settingValueText(settingEffectiveValue(item)) +
            " — not used by the current " + ((item.group || "") === "Solar" ? "solar source" : "provider/contract") + ", but still set."
        ]);
      } else {
        row = settingsField(item, dirty);
        if (isSafetySetting(item)) {
          row.classList.add("field-safety");
          row.appendChild(badge("money/safety", "warn"));
        }
      }
      if (Object.prototype.hasOwnProperty.call(dirtyValues, item.key)) row.classList.add("field-dirty");
      if (searching) row.appendChild(el("span", { class: "chip", text: item.group || "Other" }));
      return row;
    }
    // Group-level guidance: the Price warnings/provider note and the Solar notes stay with their
    // fields; the Safety note explains why saving those asks a second time.
    function groupNotes(section, group, opts) {
      if (group.name === "Price") {
        (Array.isArray(config.price_warnings) ? config.price_warnings : []).forEach(function (w) {
          if (!w) return;
          section.appendChild(el("div", { class: "banner banner-warn" }, [
            badge("provider/contract", "warn"), " ",
            el("strong", { text: String(w.key || "") }),
            " — " + String(w.message || "")
          ]));
        });
        var priceVis = priceFieldVisibility(opts.priceProvider, opts.priceContract);
        if (priceVis.note) section.appendChild(el("p", { class: "field-help", text: priceVis.note }));
      } else if (group.name === "Solar") {
        var solarVis = solarFieldVisibility(opts.solarSource);
        if (solarVis.note) section.appendChild(el("p", { class: "field-help", text: solarVis.note }));
        section.appendChild(el("p", { class: "field-help", text: SOLAR_MEASURED_NOTE }));
        // Only useful when the source can actually use it: capacity is the one with cloud attenuation.
        if (solarVis.fields.indexOf("solar.weather_entity") >= 0) {
          section.appendChild(el("p", { class: "field-help", text: SOLAR_WEATHER_NOTE }));
        }
      } else if (/safety|limit/i.test(group.name)) {
        section.appendChild(el("p", { class: "field-help", text: "These settings can move hardware or change safety limits. Changes ask for confirmation." }));
      }
    }
    // One group: the rows that apply, then the folded advanced rows. Safety rows never fold, so they
    // are always in the plain set. Searching and "show all advanced" reveal folded rows in place;
    // otherwise they sit behind a disclosure whose open state is remembered per group.
    function renderGroup(group, rows, searching) {
      var opts = currentOpts();
      // A key rendered on a setup card is not rendered again here (SPEC 13.2: exactly once).
      rows = rows.filter(function (r) { return !cardKeys[r.key]; });
      var section = el("section", { class: "settings-group", dataset: { group: group.name } });
      section.appendChild(el("h3", { text: group.name }));
      var groupBody = el("div", { class: "settings-group-body" });
      if (!searching) groupNotes(groupBody, group, opts);
      // Folding follows the *server* group (SPEC 13.2: only Safety never folds). isSafetySetting is
      // wider than that - it also covers money-moving keys such as price.feed_in_* so they ask for a
      // confirmation - and those still belong behind the disclosure when the payload marks them advanced.
      var foldable = rows.filter(function (r) { return r.advanced === true && (r.group || "") !== "Safety"; });
      var plain = rows.filter(function (r) { return !(r.advanced === true && (r.group || "") !== "Safety"); });
      var fields = el("div", { class: "fields" });
      plain.forEach(function (r) { if (settingsVisible(r, opts)) fields.appendChild(renderRow(r, opts, searching)); });
      var advancedVisible = foldable.filter(function (r) {
        return settingsVisible(r, Object.assign({}, opts, { showAdvanced: true }));
      });
      if (advancedVisible.length && (searching || showAllAdvanced)) {
        // ponytail: inline, not a forced-open <details>, so the switch cannot fight the stored per-group state.
        advancedVisible.forEach(function (r) { fields.appendChild(renderRow(r, opts, searching)); });
      }
      groupBody.appendChild(fields);
      if (advancedVisible.length && !searching && !showAllAdvanced) {
        var details = el("details", { class: "disclosure", open: advancedOpen[group.name] === true });
        details.appendChild(el("summary", { text: "Advanced (" + advancedVisible.length + ")" }));
        var advFields = el("div", { class: "disclosure-body" });
        advancedVisible.forEach(function (r) { advFields.appendChild(renderRow(r, opts, searching)); });
        details.appendChild(advFields);
        details.addEventListener("toggle", function () {
          advancedOpen[group.name] = details.open;
          writeStored(STORE_ADVANCED, advancedOpen);
        });
        groupBody.appendChild(details);
      }
      section.appendChild(groupBody);
      return section;
    }
    // ---- setup cards (SPEC 13.2 amended) ------------------------------------------------------
    // Four cards carry the primary choice each; their keys are skipped by the groups below. A key
    // the card does not render (its source does not apply) falls back to the group, so nothing is
    // lost and nothing is rendered twice.
    function cardField(key) {
      var descriptor = descriptorByKey(key);
      if (!descriptor) return null;
      var item = mergeItem(descriptor);
      if (settingHiddenBySource(item, currentOpts())) return null;
      cardKeys[key] = true;
      return settingsField(item, dirty);
    }

    function cardPicker(key, kind, placeholder) {
      var descriptor = descriptorByKey(key);
      if (!descriptor) return null;
      var item = mergeItem(descriptor);
      if (settingHiddenBySource(item, currentOpts())) return null;
      cardKeys[key] = true;
      var wrap = el("div", { class: "field", dataset: { key: key } });
      wrap.appendChild(el("label", { text: descriptor.label || key }));
      wrap.appendChild(el("span", { class: "field-key", text: key }));
      wrap.appendChild(entityPicker({
        kind: kind,
        key: key,
        label: descriptor.label || key,
        placeholder: placeholder,
        value: settingValue(key),
        onPick: function (target, entityId) {
          dirty(target, entityId, wrap);
          draw();
        }
      }));
      if (descriptor.help) wrap.appendChild(el("p", { class: "field-help", text: descriptor.help }));
      return wrap;
    }

    function setupCard(title, help, children) {
      var card = el("section", { class: "setup-card" });
      card.appendChild(el("h3", { text: title }));
      if (help) card.appendChild(el("p", { class: "field-help", text: help }));
      children.forEach(function (child) { if (child) card.appendChild(child); });
      return card;
    }

    function priceCard() {
      var provider = String(settingValue("price.provider") || "entity");
      var contract = String(settingValue("price.contract") || "dynamic_hourly");
      var children = [cardField("price.provider")];
      if (provider === "entity") children.push(cardPicker("price.entity", "price", "price sensor name or entity id"));
      if (provider === "day_ahead") children.push(cardField("price.bzn"));
      if (provider === "fixed") {
        children.push(cardField("price.contract"));
        if (contract === "fixed_day_night") {
          children.push(cardField("price.fixed_day_price_eur_per_kwh"));
          children.push(cardField("price.fixed_night_price_eur_per_kwh"));
        } else {
          children.push(cardField("price.fixed_price_eur_per_kwh"));
        }
      }
      children.push(cardField("price.export_fee_eur_per_kwh"));
      children.push(cardField("price.feed_in_energy_tax_refund"));
      var note = priceFieldVisibility(provider, contract).note;
      if (note) children.push(el("p", { class: "field-help", text: note }));
      return setupCard(
        "Price",
        "Pick where prices come from; the add-on fetches the slots itself. The sell price is the buy price minus the feed-in cost.",
        children
      );
    }

    function meterCard() {
      return setupCard(
        "Meter",
        "The live power the add-on follows. The load forecast, and the meter-derived solar estimate, read this meter.",
        [cardPicker("grid.power_entity", "grid", "grid power sensor name or entity id")]
      );
    }

    function solarCard() {
      var source = String(settingValue("solar.source") || "forecast_solar");
      var children = [cardField("solar.enabled"), cardField("solar.source")];
      if (source === "derived") {
        children.push(el("p", { class: "field-help", text: "Uses the Meter card and the battery's charge/discharge power; nothing to enter here." }));
      } else {
        children.push(cardPicker("solar.measured_entity", "solar", "PV power sensor name or entity id"));
      }
      var note = solarFieldVisibility(source).note;
      if (note) children.push(el("p", { class: "field-help", text: note }));
      return setupCard("Solar", "Optional. How the add-on should know your production.", children);
    }

    function batteryCard() {
      var list = settingValue("batteries");
      var children = [];
      if (Array.isArray(list) && list.length) {
        children.push(el("ul", { class: "setup-list" }, list.map(function (battery) {
          var parts = [String((battery && battery.name) || (battery && battery.id) || "battery")];
          if (battery && isNum(battery.capacity_kwh)) parts.push(fmtKwh(battery.capacity_kwh));
          if (battery && battery.enabled === false) parts.push("disabled");
          return el("li", { text: parts.join(" · ") });
        })));
      } else {
        children.push(el("p", { class: "muted", text: "No battery configured yet." }));
      }
      children.push(el("p", {}, [
        el("a", { class: "btn btn-primary", href: "#/batteries", text: list && list.length ? "Manage batteries" : "Add a battery" }),
        " ",
        el("span", { class: "muted", text: "Batteries are searched, picked and confirmed in the Batteries view." })
      ]));
      return setupCard("Battery", "Which battery the plan may drive; the wizard finds the device and fills in the entities.", children);
    }

    function renderRail(groups, active) {
      clear(railHost);
      railEntries = {};
      groups.forEach(function (group) {
        var count = el("span", { class: "badge " + (group.changedCount ? "badge-warn" : "badge-info"), text: String(group.changedCount) });
        var entry = el("button", { type: "button", class: "chip" }, [group.name, " ", count]);
        if (group.name === active) entry.setAttribute("aria-current", "true");
        entry.addEventListener("click", function () {
          activeGroup = group.name;
          writeStored(STORE_GROUP, activeGroup);
          if (search.value) search.value = "";
          draw();
        });
        railEntries[group.name] = { button: entry, count: count };
        railHost.appendChild(entry);
      });
    }
    function updateCounts(groups) {
      groups = groups || settingsGroups(mergedItems());
      groups.forEach(function (group) {
        var entry = railEntries[group.name];
        if (!entry) return;
        entry.count.textContent = String(group.changedCount);
        entry.count.className = "badge " + (group.changedCount ? "badge-warn" : "badge-info");
      });
      saveNote.textContent = dirtyCount ? dirtyCount + " key" + (dirtyCount === 1 ? "" : "s") + " changed, not saved yet" : "No unsaved changes";
      saveButtons.forEach(function (b) { b.textContent = "Save changes (" + dirtyCount + ")"; b.disabled = dirtyCount === 0; });
    }

    var search = el("input", { type: "search", id: "settings-search", placeholder: "filter settings by key, label or help", "aria-label": "Filter settings" });
    var showAllBox = el("input", { type: "checkbox", id: "settings-show-advanced", checked: showAllAdvanced });
    var railHost = el("nav", { class: "settings-rail", "aria-label": "Settings groups" });
    var listHost = el("div");
    var cardsHost = el("div", { class: "setup-cards" });
    var saveNote = el("span", { class: "muted", text: "No unsaved changes" });
    var saveButtons = [];
    function saveButton() {
      var b = button("Save changes (0)", "btn-primary");
      b.disabled = true;
      b.addEventListener("click", submit);
      saveButtons.push(b);
      return b;
    }

    function draw() {
      clear(listHost);
      clear(cardsHost);
      cardKeys = {};
      var opts = currentOpts();
      var query = String(opts.query || "").trim();
      var groups = settingsGroups(mergedItems());
      var active = null;
      groups.forEach(function (g) { if (g.name === activeGroup) active = g; });
      if (!active && groups.length) { active = groups[0]; activeGroup = active.name; writeStored(STORE_GROUP, activeGroup); }
      // The cards are the default surface. A search hides them, so every match is listed exactly
      // once in the drawer below (the cards own their keys and the groups skip them).
      if (!query) {
        [priceCard(), meterCard(), solarCard(), batteryCard()].forEach(function (card) {
          if (card) cardsHost.appendChild(card);
        });
      }
      renderRail(groups, activeGroup);
      var note = feedInNote(mergedItems());
      if (note) listHost.appendChild(note);
      if (query) {
        // Search reveals matches everywhere: every group, folded and source-hidden rows included,
        // each match labelled with the group it lives in.
        var matched = false;
        groups.forEach(function (group) {
          var rows = group.rows.filter(function (r) { return settingsVisible(r, opts); });
          if (!rows.length) return;
          matched = true;
          listHost.appendChild(renderGroup(group, rows, true));
        });
        if (!matched) listHost.appendChild(el("p", { class: "muted", text: "No settings match that filter." }));
      } else {
        listHost.appendChild(renderGroup(active, active.rows, false));
      }
      updateCounts(groups);
    }

    function feedInNote(shown) {
      var byKey = {};
      shown.forEach(function (d) { byKey[d.key] = d; });
      var exportOn = byKey["optimizer.allow_export"] && settingEffectiveValue(byKey["optimizer.allow_export"]) === true;
      if (!exportOn) return null;
      var flat = byKey["price.feed_in_eur_per_kwh"];
      var entity = byKey["price.feed_in_entity"];
      var flatSet = flat && settingEffectiveValue(flat) !== null && settingEffectiveValue(flat) !== undefined;
      var entitySet = entity && String(settingEffectiveValue(entity) || "").length > 0;
      var refunded = !byKey["price.feed_in_energy_tax_refund"] || settingEffectiveValue(byKey["price.feed_in_energy_tax_refund"]) !== false;
      var text = flatSet || entitySet
        ? "Export is enabled: the sell price comes from price.feed_in_eur_per_kwh / price.feed_in_entity, not from the derived formula."
        : "Export is enabled: the sell price is the buy price minus the feed-in cost, with the energy tax " + (refunded ? "refunded." : "not refunded.");
      return el("div", { class: "banner banner-info" }, [
        el("p", { class: "banner-title", text: "Export is enabled" }),
        el("p", { text: text })
      ]);
    }

    function submit() {
      var settings = {};
      var reject = false;
      Object.keys(dirtyValues).forEach(function (key) {
        if (dirtyValues[key].invalid) reject = true;
        else settings[key] = dirtyValues[key].value;
      });
      if (reject) { setNote(listHost, "There are invalid values; fix them before saving.", true); return; }
      var keys = Object.keys(settings);
      if (!keys.length) return;
      var safety = keys.filter(function (key) {
        var descriptor = descriptors.filter(function (d) { return d.key === key; })[0];
        return descriptor ? isSafetySetting(descriptor) : false;
      });
      var requestFn = function () { return patch("/config", { settings: settings }); };
      var successText = function (res) {
        var applied = (res && res.applied) || [];
        var errors = (res && res.errors) || [];
        if (errors.length) return "Applied " + applied.length + " setting(s), but the server rejected " + errors.length + ".";
        return "Applied " + applied.length + " setting(s) (confirmed by the server).";
      };
      var onDone = function (res) {
        var errors = (res && res.errors) || [];
        if (errors.length) {
          renderPatchErrors(listHost, errors);
          return;
        }
        state.navGuard = null;
        root.setTimeout(function () { renderRoute(); }, 600);
      };
      if (safety.length) {
        confirmAction({
          title: "Change safety-relevant settings?",
          body: "These keys can change how the battery is controlled: " + safety.join(", ") + ".",
          confirmLabel: "Save safety settings", requireCheck: true,
          checkLabel: "I understand the effect on battery control"
        }).then(function (ok) { if (ok) performWrite(saveButtons[0], listHost, requestFn, successText, onDone); });
      } else {
        performWrite(saveButtons[0], listHost, requestFn, successText, onDone);
      }
    }

    function renderPatchErrors(host, errors) {
      var box = el("div", { class: "banner banner-error" }, [el("p", { class: "banner-title", text: "Server rejected some values" })]);
      var list = el("ul");
      errors.forEach(function (error) {
        var text = typeof error === "string" ? error : (error.key ? error.key + ": " : "") + (error.message || "invalid value");
        list.appendChild(el("li", { text: text }));
      });
      box.appendChild(list);
      host.appendChild(box);
    }

    search.addEventListener("input", draw);
    showAllBox.addEventListener("change", function () {
      showAllAdvanced = showAllBox.checked;
      writeStored(STORE_SHOW_ALL, showAllAdvanced);
      draw();
    });
    var drawerBody = el("div", { class: "disclosure-body" }, [
      el("div", { class: "toolbar" }, [
        el("label", { for: "settings-search" }, [el("span", { text: "Search" }), search]),
        el("label", { class: "switch", for: "settings-show-advanced" }, [showAllBox, " Show all advanced"])
      ]),
      railHost,
      listHost
    ]);
    var drawer = el("details", { class: "disclosure all-settings" }, [
      el("summary", { text: "All settings (" + descriptors.length + ")" }),
      drawerBody
    ]);
    if (readStored(STORE_ALL_OPEN, false) === true) drawer.open = true;
    drawer.addEventListener("toggle", function () { writeStored(STORE_ALL_OPEN, drawer.open); });
    body.appendChild(cardsHost);
    body.appendChild(drawer);
    var saveBar = el("div", { class: "save-bar" }, [
      saveNote,
      el("div", { class: "save-bar-actions" }, [
        button("Revert", "", function () { dirtyValues = {}; dirtyCount = 0; draw(); }),
        saveButton()
      ])
    ]);
    body.appendChild(saveBar);
    // Leaving with unsaved edits asks first; the router consults this guard before it re-renders.
    state.navGuard = {
      hash: root.location.hash,
      title: "Discard unsaved settings?",
      body: function () {
        return dirtyCount + " setting" + (dirtyCount === 1 ? "" : "s") + " changed here " +
          (dirtyCount === 1 ? "is" : "are") + " not saved yet. Leave and discard " + (dirtyCount === 1 ? "it" : "them") + "?";
      },
      confirmLabel: "Leave without saving",
      // ponytail: a detached settings DOM is never really dirty, so a stale guard cannot block a route.
      dirty: function () { return Object.keys(dirtyValues).length > 0 && listHost.isConnected; }
    };
    updateCounts();
    draw();
  }

  function renderOptions(body, config) {
    var options = config.options || {};
    var rows = Object.keys(options).map(function (key) {
      var value = options[key];
      return [key, value === null || value === undefined ? "(unset — read from Home Assistant)" : String(value)];
    });
    body.appendChild(el("p", { class: "muted", text: "Add-on options are set in the add-on configuration. Settings saved here override them at runtime." }));
    body.appendChild(table("Add-on options", ["Option", "Value"], rows.length ? rows : [["(none)", ""]]));
  }

  function viewSettings(host) {
    var load = once(function () { return get("/config"); });
    asyncPanel(host, "Settings", { load: load, render: renderSettings });
    asyncPanel(host, "Add-on options", { load: load, render: renderOptions });
    return null;
  }

  /* ------------------------------------------------------------------ view: log */

  var LOG_LEVELS = ["", "info", "warning", "error"];

  function renderLogEntries(host, entries) {
    clear(host);
    if (!entries.length) {
      host.appendChild(el("p", { class: "muted", text: "No entries for these filters. If something surprising happened and this stays empty, that is a bug worth reporting." }));
      return;
    }
    var rows = entries.map(function (entry) {
      var detail = entry.detail && Object.keys(entry.detail).length
        ? el("details", { class: "log-detail" }, [el("summary", { text: "detail" }), el("pre", { text: JSON.stringify(entry.detail, null, 2) })])
        : "—";
      var requested = entry.detail ? numOrNull(entry.detail.requested_w) : null;
      var applied = entry.detail ? numOrNull(entry.detail.applied_w) : null;
      var power = requested == null && applied == null ? "" : [fmtPower(requested) + " requested", fmtPower(applied) + " applied"].join(" → ");
      return [fmtDateTime(entry.ts) + " (" + timeAgo(entry.ts) + ")", entry.kind || "", badge(entry.level || "info", entry.level === "error" ? "err" : entry.level === "warning" ? "warn" : "info"), entry.code || "", entry.message || "", power, detail];
    });
    host.appendChild(table("Newest first", ["Time", "Kind", "Level", "Code", "Message", "Power", "Detail"], rows));
  }

  function viewLog(host) {
    var filters = { level: "", kind: "", limit: "200" };
    var p = panel(host, "Decision log");
    var levelSelect = el("select", { id: "log-level" }, LOG_LEVELS.map(function (level) {
      return el("option", { value: level, text: level === "" ? "all levels" : level });
    }));
    var kindInput = el("input", { type: "search", id: "log-kind", placeholder: "dispatch, event, alert…" });
    var limitSelect = el("select", { id: "log-limit" }, ["50", "100", "200", "500"].map(function (n) { return el("option", { value: n, text: n + " rows", selected: n === filters.limit }); }));
    var auto = el("input", { type: "checkbox", id: "log-auto", checked: true });
    var refresh = button("Refresh", "");
    var entriesHost = el("div");
    var timer = null;

    function reload(showLoading) {
      if (showLoading) panelStatus(entriesHost, "loading", "Loading log…");
      get("/log", { limit: filters.limit, level: filters.level, kind: filters.kind }).then(function (payload) {
        renderLogEntries(entriesHost, Array.isArray(payload.entries) ? payload.entries : []);
      }).catch(function (err) {
        panelStatus(entriesHost, "error", "Could not load the log: " + (err && err.message ? err.message : err), function () { reload(true); });
      });
    }
    levelSelect.addEventListener("change", function () { filters.level = levelSelect.value; reload(true); });
    limitSelect.addEventListener("change", function () { filters.limit = limitSelect.value; reload(true); });
    kindInput.addEventListener("change", function () { filters.kind = kindInput.value.trim(); reload(true); });
    refresh.addEventListener("click", function () { reload(false); });

    p.body.appendChild(el("div", { class: "log-filters" }, [
      el("label", { for: "log-level" }, [el("span", { class: "stat-label", text: "Level" }), levelSelect]),
      el("label", { for: "log-kind" }, [el("span", { class: "stat-label", text: "Kind" }), kindInput]),
      el("label", { for: "log-limit" }, [el("span", { class: "stat-label", text: "Rows" }), limitSelect]),
      el("label", { class: "muted", for: "log-auto" }, [auto, " auto-refresh"]),
      refresh
    ]));
    p.body.appendChild(el("p", { class: "legend-note", text: "Every dispatch attempt is logged with requested vs applied power and the blocking reason, so “why didn't it charge?” is always answerable here." }));
    p.body.appendChild(entriesHost);
    reload(true);
    timer = root.setInterval(function () {
      if (auto.checked && !document.hidden && !anyDialogOpen()) reload(false);
    }, 15000);
    return function () { root.clearInterval(timer); };
  }

  /* ==== VIEWS BELOW ==== */

  root.SEM_APP = {
    ROUTES: VIEWS,
    splitHash: splitHash,
    normalizeRoute: normalizeRoute,
    reasonText: reasonText,
    actionText: actionText,
    forecastEmptyState: forecastEmptyState,
    priceFieldVisibility: priceFieldVisibility,
    solarFieldVisibility: solarFieldVisibility,
    settingsGroups: settingsGroups,
    settingsVisible: settingsVisible,
    setupCardKeys: setupCardKeys,
    fmtPower: fmtPower,
    fmtKwh: fmtKwh,
    fmtPrice: fmtPrice,
    fmtEur: fmtEur,
    fmtPct: fmtPct,
    fmtDuration: fmtDuration,
    timeAgo: timeAgo,
    testDischargeOutcome: testDischargeOutcome,
    batteryEfficiency: batteryEfficiency,
    batteryEfficiencyPayload: batteryEfficiencyPayload,
    roundTripPct: roundTripPct,
    storedKwhCostEur: storedKwhCostEur,
    breakEvenSellEur: breakEvenSellEur,
    planEfficiency: planEfficiency,
    planBreakEvenEur: planBreakEvenEur,
    cycleCostFromConfig: cycleCostFromConfig,
    batteryGroupsFromCandidates: batteryGroupsFromCandidates,
    batteryGroupMatchesQuery: batteryGroupMatchesQuery,
    batteryRowMatchesQuery: batteryRowMatchesQuery,
    batterySearchGroups: batterySearchGroups,
    batterySlugFor: batterySlugFor,
    batterySelection: batterySelection,
    basePath: basePath,
    apiPath: apiPath,
    isMock: MOCK,
    api: api,
    get: get,
    post: post,
    patch: patch,
    del: del,
    confirmAction: confirmAction,
    state: state
  };

  if (typeof document !== "undefined" && document.getElementById("view")) boot();
})(typeof globalThis !== "undefined" ? globalThis : this);
