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
  //: Mirrors ``models.BatteryConfig.eta_discharge`` for a battery whose config carries no eta.
  var DEFAULT_ETA_DISCHARGE = 0.95;

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

  /* ------------------------------------------------------------------ inline icons
   * SPEC 13.1 keeps the UI free of images and of anything fetched at runtime, so every glyph here is
   * drawn as inline SVG: one 24x24 grid, stroked with currentColor at 1.7, no glyph that can only be
   * told apart by colour. An icon names a shape; the action or fact it belongs to is always spelled
   * out in the text beside it, so a monochrome or read-aloud rendering loses nothing (SPEC 13.1).
   */
  var SVG_NS = "http://www.w3.org/2000/svg";

  var ICONS = {
    // Battery, arrow in: energy going into the battery.
    charge: ["M3 9h12v6H3Z", "M17.5 11v2", "M9 10.4v3.2", "M7.4 12l1.6 1.6 1.6-1.6"],
    // Battery, arrow out: energy coming out of it.
    discharge: ["M3 9h12v6H3Z", "M17.5 11v2", "M9 13.6v-3.2", "M7.4 12l1.6-1.6 1.6 1.6"],
    // Battery, two bars: nothing moves.
    hold: ["M3 9h12v6H3Z", "M17.5 11v2", "M7.7 10.6v2.8", "M10.3 10.6v2.8"],
    // Sun plus a down arrow: PV surplus charging.
    pv_charge: [
      "M12 13a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
      "M12 2.4v1.6", "M5.4 9.5H7", "M16.9 9.5h1.7",
      "M7.4 4.9 8.5 6", "M16.6 4.9 15.5 6",
      "M12 16.4v4.7", "M10.2 18.6 12 20.4l1.8-1.8"
    ],
    // Arrow up out of a tray: exporting to the grid.
    export: ["M12 16.6V5.2", "M8.2 9 12 5.2 15.8 9", "M4.6 14.6V18a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3.4"],
    // A house: the load served from the battery instead of the grid.
    self_consumption: ["M4 11 12 4.2 20 11", "M6.5 10v9.6h11V10", "M10 19.6V15h4v4.6"],
    // A shield over a line: the floor this window defends.
    reserve: ["M12 3.6 5.5 6.1v5.6c0 3.9 2.7 7.2 6.5 8.7 3.8-1.5 6.5-4.8 6.5-8.7V6.1Z", "M8.6 12.4h6.8"],
    // Coin: the price a window buys and sells at.
    price: ["M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17Z", "M14.8 9.7a3.3 3.3 0 1 0 0 4.6", "M7.7 11.2h5.1", "M7.7 13h5.1"],
    // A flagged threshold above a dashed baseline: the break-even a sale has to clear.
    breakeven: [
      { d: "M3.5 17.6h17", dash: "2 2" },
      "M13.6 17.6V9.2", "M13.6 9.2h5.6l-1.8 2.3 1.8 2.3h-5.6"
    ],
    // Battery with the level it holds.
    battery: ["M3 9h12v6H3Z", "M17.5 11v2", { d: "M4.6 10.2h3.2v3.6H4.6Z", fill: true }],
    // Plug: the grid the window imports from or exports to.
    grid: ["M9 3v3.8", "M15 3v3.8", "M6.4 6.8h11.2v4.6a5.6 5.6 0 0 1-11.2 0Z", "M12 17v3.6"],
    // Three lines: the horizon the windows were planned across.
    list: ["M4 7h16", "M4 12h16", "M4 17h16"],
    // Fallback for an action this UI does not know yet.
    bolt: [{ d: "M13.2 2.5 5.6 13.1h5.3l-1 8.4L18.4 11h-5.3Z", fill: true }]
  };

  /** Build one inline icon. Unknown names fall back to a bolt, so a new engine action still shows a
   *  glyph instead of a gap. `cls` adds a size/colour class; the glyph itself never carries meaning. */
  function icon(name, cls) {
    var parts = ICONS[name] || ICONS.bolt;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    parts.forEach(function (part) {
      var shape = typeof part === "string" ? { d: part } : part;
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", shape.d);
      if (shape.fill) path.setAttribute("fill", "currentColor");
      if (shape.dash) path.setAttribute("stroke-dasharray", shape.dash);
      svg.appendChild(path);
    });
    return svg;
  }

  // One shape per action the engine can put in a slot. The fallback keeps an unknown action visible.
  var ACTION_ICON = {
    charge: "charge",
    discharge: "discharge",
    hold: "hold",
    pv_charge: "pv_charge",
    export: "export",
    self_consumption: "self_consumption",
    reserve: "reserve"
  };

  function actionIcon(action) { return ACTION_ICON[action] || "bolt"; }

  // The tint a row and its ribbon segment are drawn in; unknown actions share the neutral one.
  var ACTION_TINT = { charge: "charge", discharge: "discharge", pv_charge: "pv", export: "export", self_consumption: "self", reserve: "reserve" };

  function actionClass(action) { return "window--" + (ACTION_TINT[action] || "idle"); }

  // Facts carry a glyph as well as a label, never a colour-only cue: the label spells the meaning out.
  var FACT_ICON = {
    "Buy → sell": "price",
    "Break-even": "breakeven",
    "Battery": "battery",
    "SoC": "battery",
    "Grid cost": "grid"
  };

  function factIcon(label) { return FACT_ICON[label] || "bolt"; }

  function clampPct(value) { return Math.max(0, Math.min(100, isNum(value) ? value : 0)); }

  /** The horizon the plan spans, in ms. A plan with fewer than two slots has no span, so callers
   *  leave the graphic out rather than drawing an arbitrary bar. */
  function planHorizonBounds(slots, minutes) {
    var times = (slots || []).map(function (slot) { return parseDate(slot.t); })
      .filter(function (d) { return d instanceof Date && isFinite(d.getTime()); })
      .map(function (d) { return d.getTime(); });
    if (times.length < 2) return null;
    var step = (isNum(minutes) && minutes > 0 ? minutes : 15) * 60000;
    var startMs = Math.min.apply(null, times);
    var endMs = Math.max.apply(null, times) + step;
    if (!(endMs > startMs)) return null;
    return { startMs: startMs, endMs: endMs, spanMs: endMs - startMs };
  }

  /** Where a timestamp sits on the horizon, as a percentage. Null when there is nothing to place it on. */
  function horizonPct(ms, bounds) {
    if (!bounds || !isNum(ms)) return null;
    return clampPct((100 * (ms - bounds.startMs)) / bounds.spanMs);
  }

  /** One window's span on the horizon, in percent. A single-slot window keeps at least 1% so it stays
   *  visible instead of collapsing to a hairline. */
  function windowRail(win, bounds) {
    if (!bounds || !win || !isNum(win.startMs) || !isNum(win.endMs)) return null;
    var left = horizonPct(win.startMs, bounds);
    var right = horizonPct(win.endMs, bounds);
    if (left === null || right === null) return null;
    return { left: left, width: Math.max(1, right - left) };
  }

  /** The charge a window operates in, as percentages of the battery: the level it ends on, where it
   *  started and the floor it defends. A gauge rather than a delta bar, so a single-slot window still
   *  shows where in the battery it acts. Null when the plan reported no SoC path at all. */
  function socTrack(win, floorPct) {
    if (!win || !isNum(win.socStart) || !isNum(win.socEnd)) return null;
    var from = clampPct(win.socStart);
    var end = clampPct(win.socEnd);
    return {
      fill: Math.max(0.8, end),
      from: from,
      floor: isNum(floorPct) ? clampPct(floorPct) : null,
      rising: end >= from,
      moved: Math.abs(end - from) >= 0.5
    };
  }

  /** Local midnights inside the horizon: the ribbon's day ticks, so a 36 h plan reads as two days. */
  function horizonDayTicks(bounds) {
    var ticks = [];
    if (!bounds) return ticks;
    var cursor = new Date(bounds.startMs);
    cursor.setHours(0, 0, 0, 0);
    cursor.setDate(cursor.getDate() + 1);
    while (cursor.getTime() < bounds.endMs && ticks.length < 6) {
      ticks.push({ pct: horizonPct(cursor.getTime(), bounds) });
      cursor.setDate(cursor.getDate() + 1);
      cursor.setHours(0, 0, 0, 0);
    }
    return ticks;
  }

  // Numeric columns are declared metadata, never a blanket "everything after the first
  // column": prose columns (messages, reasons, names) must stay left-aligned (UI_REVIEW §3.11).
  // opts = {rowAttrs, numeric}; numeric = null (default) = every column after the first.
  function table(caption, headers, rows, opts) {
    opts = opts || {};
    var numeric = opts.numeric === undefined ? null : opts.numeric;
    var head = el("tr", null, headers.map(function (h) { return el("th", { scope: "col", text: h }); }));
    var body = el("tbody", null, rows.map(function (cells, r) {
      var row = el("tr", opts.rowAttrs && opts.rowAttrs[r] ? opts.rowAttrs[r] : null);
      cells.forEach(function (cell, i) {
        var numericColumn = numeric ? numeric.indexOf(i) >= 0 : i > 0;
        var td = el("td", numericColumn ? { class: "num" } : null);
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

  // A metric card. `state` is false (a normal reading), "stale", "loss" or "unknown": each
  // gets its own visible flag, so a negative result, missing data and a stale reading never share
  // one treatment (UI_REVIEW §3.6). A plain `true` means "stale", which is what the dashboard
  // cards pass.
  function statCard(label, value, sub, state) {
    var kind = state === true ? "stale" : typeof state === "string" && state ? state : false;
    var flags = { stale: "Stale reading", loss: "Loss", unknown: "Unknown" };
    return el("div", { class: "card" + (kind ? " card-" + kind : "") }, [
      el("p", { class: "card-label", text: label }),
      el("p", { class: "card-value" }, value),
      sub ? el("p", { class: "card-sub" }, sub) : null,
      kind ? el("p", { class: "card-flag", text: flags[kind] || kind }) : null
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

  // Sampled-time coverage (0.30.0) is hours, never an energy or a percentage on its own.
  function fmtHours(hours) {
    if (!isNum(hours)) return "--";
    return hours >= 10 ? hours.toFixed(1) + " h" : hours.toFixed(2) + " h";
  }

  // Power in kW, so a kW payload is never labelled as an energy (UI_REVIEW §3.6).
  function fmtKw(kw, digits) {
    if (!isNum(kw)) return "--";
    return kw.toFixed(digits === undefined ? 2 : digits) + " kW";
  }

  function fmtPrice(value, digits) {
    if (!isNum(value)) return "--";
    return value.toFixed(digits === undefined ? 4 : digits) + " " + UNIT_EUR + "/kWh";
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
    if (m < 60) return m < 60 && s % 60 === 0 ? m + " min" : m + " min " + (s % 60) + " s";
    var h = Math.floor(m / 60);
    return m % 60 === 0 ? h + " h" : h + " h " + (m % 60) + " min";
  }

  function timeAgo(value) {
    var d = parseDate(value);
    if (!d) return "unknown time";
    var delta = (Date.now() - d.getTime()) / 1000;
    if (delta < 0) return "in " + fmtDuration(-delta);
    // A fresh reading reads "just now", never "0 s ago".
    if (delta < 5) return "just now";
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
    // The command is in force but the device (or the Zendure manager, which only applies a
    // setpoint on its next P1 event) has not confirmed it yet.  Not an error, and not a claim
    // that the flow was measured: the plan execution line reports measured vs planned.
    commanded: "Commanded: the device has not confirmed it yet",
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

  var state = { status: null, health: null, viewToken: 0, cleanup: null, refresh: null };

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

  // The route title: one visible H1 per view, and the element intentional navigation focuses.
  function routeTitle(host, title, sub) {
    host.appendChild(el("h1", { class: "route-title", tabindex: "-1", text: title }));
    if (sub) host.appendChild(el("p", { class: "route-sub", text: sub }));
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
    // A quiet reload (background refresh) must not flash "Loading…" over figures the user is
    // reading; the panel body is redrawn in place when the new data arrives (UI_REVIEW §3.5).
    function run(quiet) {
      if (!quiet) {
        p.section.setAttribute("data-state", "loading");
        panelStatus(p.body, "loading", options.loadingText || "Loading…");
      }
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
    p.reload = function () { run(true); };
    return p;
  }

  /* ------------------------------------------------------------------ confirm dialog + write helpers */

  function anyDialogOpen() {
    var dialogs = document.querySelectorAll ? document.querySelectorAll("dialog") : [];
    for (var i = 0; i < dialogs.length; i++) {
      if (dialogs[i] && dialogs[i].open) return true;
    }
    return false;
  }

  // In-flight writes and unresolved stop results are evidence, not stale data: a background
  // refresh must never discard them (UI_REVIEW §3.5).
  var pendingWrites = 0;

  /* One guard for every background refresh: an open dialog, a pending write, a dirty form or an
   * unresolved stop result blocks it. Deliberate navigation does not use this guard. */
  function refreshBlocked() {
    if (anyDialogOpen()) return true;
    if (pendingWrites > 0) return true;
    if (state.navGuard && typeof state.navGuard.dirty === "function" && state.navGuard.dirty()) return true;
    if (document.querySelector && document.querySelector("[data-stop-result]")) return true;
    return false;
  }

  /* ------------------------------------------------------------------ quiet refresh helpers
   * A background data refresh must not scroll, destroy the focused control, or discard rows the
   * reader expanded (UI_REVIEW §3.5): re-render into the existing host and then put focus back
   * on the element that occupied the same position before the update. */

  function within(node, container) {
    var current = node;
    while (current) {
      if (current === container) return true;
      current = current.parentNode;
    }
    return false;
  }

  function childElements(node) {
    if (!node) return [];
    if (node.children) return Array.prototype.slice.call(node.children);
    return Array.prototype.filter.call(node.childNodes || [], function (kid) { return kid.nodeType === 1; });
  }

  function focusPath(node, stop) {
    var steps = [];
    var current = node;
    while (current && current !== stop && current.tagName) {
      var parent = current.parentNode;
      if (!parent) return null;
      var same = childElements(parent).filter(function (kid) { return kid.tagName === current.tagName; });
      steps.unshift(current.tagName + ":" + same.indexOf(current) + "/" + same.length);
      current = parent;
    }
    return steps.length ? steps : null;
  }

  function elementAtPath(root, steps) {
    var node = root;
    for (var i = 0; i < steps.length; i++) {
      if (!node) return null;
      var match = /^([A-Z]+):(\d+)\/(\d+)$/.exec(steps[i]);
      if (!match) return null;
      var same = childElements(node).filter(function (kid) { return kid.tagName === match[1]; });
      node = same.length === Number(match[3]) ? same[Number(match[2])] : null;
    }
    return node || null;
  }

  // Draw into `target` (which draw() fills itself) and restore focus to the equivalent element.
  function renderInto(target, draw) {
    var active = document.activeElement;
    var path = active && within(active, target) ? focusPath(active, null) : null;
    clear(target);
    draw(target);
    if (path) {
      var again = elementAtPath(document.body || document.documentElement, path);
      if (again && typeof again.focus === "function") again.focus();
    }
  }

  function confirmAction(options) {
    return new Promise(function (resolve) {
      var dlg = document.getElementById("confirm-dialog");
      if (!dlg || typeof dlg.showModal !== "function") {
        // Fail closed: a bare window.confirm cannot carry the acknowledgement checkbox, so an
        // action that needs a barrier must not proceed without one (UI_REVIEW §8.6).
        if (options.requireCheck) {
          setNote(null, "This browser cannot show the acknowledgement dialog, so nothing was applied.");
          resolve(false);
          return;
        }
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

  // A cached failure must not poison a Retry: clear the slot so the next call re-requests
  // (UI_REVIEW §3.11).
  function once(factory) {
    var promise = null;
    return function () {
      if (!promise) {
        promise = factory().catch(function (err) { promise = null; throw err; });
      }
      return promise;
    };
  }

  // Every dialog counts, not only the shared confirmation one: an open battery wizard or test
  // dialog must also pause background refresh (UI_REVIEW §3.11).
  function performWrite(trigger, target, requestFn, successText, onDone, onError) {
    trigger.disabled = true;
    trigger.setAttribute("aria-busy", "true");
    if (trigger.classList) trigger.classList.add("pending");
    setNote(target, "Pending… (waiting for the server)", false);
    pendingWrites++;
    requestFn().then(function (result) {
      pendingWrites--;
      if (trigger.classList) trigger.classList.remove("pending");
      trigger.removeAttribute("aria-busy");
      trigger.disabled = false;
      setNote(target, successText ? successText(result) : "Confirmed by the server.", false);
      if (onDone) onDone(result);
    }).catch(function (err) {
      pendingWrites--;
      if (trigger.classList) trigger.classList.remove("pending");
      trigger.removeAttribute("aria-busy");
      trigger.disabled = false;
      setNote(target, "Error: " + (err && err.message ? err.message : String(err)), true);
      if (onError) onError(err);
    });
  }

  function writeAction(options) {
    var btn = options.button;
    var target = options.noteContainer || btn.parentNode;
    btn.addEventListener("click", function () {
      if (!options.confirm) {
        performWrite(btn, target, options.request, options.successText, options.onDone, options.onError);
        return;
      }
      confirmAction(options.confirm).then(function (ok) {
        if (ok) performWrite(btn, target, options.request, options.successText, options.onDone, options.onError);
      });
    });
    return btn;
  }

  /* ---- control-authority policy (UI_REVIEW §3.3) --------------------------------------------
   * The two transitions that can let the plan dispatcher write to the batteries get the same
   * acknowledgement wherever they are triggered from: the Dashboard control settings and the
   * Settings General group share these descriptors, so the safety barrier cannot depend on the
   * route the user happened to take. */

  function modeChangeConfirm(mode) {
    if (mode === "auto") {
      return {
        title: "Enable automatic control?",
        body: "Auto lets the add-on write power setpoints to the batteries. Keep dry run on to log the writes without performing them. There is no device watchdog: switching away from auto later does not neutralise a setpoint that was already written — use Stop now for that.",
        confirmLabel: "Enable auto",
        requireCheck: true,
        checkLabel: "I understand the battery can be controlled"
      };
    }
    return null;
  }

  function dryRunOffConfirm() {
    return {
      title: "Turn dry run off?",
      body: "With dry run off and mode auto, the add-on performs real writes to your batteries. The device has no watchdog, so a written setpoint stays in force until something else changes it.",
      confirmLabel: "Turn dry run off",
      requireCheck: true,
      checkLabel: "I understand writes will be real"
    };
  }

  // The exact dispatcher/Stop distinction, shared by the answer strip and the Settings help text.
  function authorityNote(dryRun) {
    return dryRun
      ? "Dry run on: the plan dispatcher writes nothing — it logs what it would send. Stop now still writes 0 W to the hardware, even in dry run."
      : "Dry run off: the plan dispatcher writes setpoints to the batteries. Stop now still writes 0 W to the hardware.";
  }

  /* ------------------------------------------------------------------ router */

  var VIEWS = {
    "/dashboard": viewDashboard,
    "/plan": viewPlan,
    "/forecasts": viewForecasts,
    "/batteries": viewBatteries,
    "/cards": viewCards,
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
    var area = NAV_AREAS[route];
    var subVisible = area === "plan" || area === "manage";
    Array.prototype.forEach.call(document.querySelectorAll("#nav a"), function (link) {
      var href = (link.getAttribute("href") || "").replace(/^#/, "");
      var group = link.closest ? link.closest("[data-sub]") : null;
      var current;
      if (group) {
        // The contextual row marks the exact route; the primary pill stays on the area entry.
        group.hidden = !subVisible || group.getAttribute("data-sub") !== area;
        current = href === route;
      } else {
        current = NAV_AREAS[href] === area && (!subVisible || href !== route);
      }
      if (current) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    Array.prototype.forEach.call(document.querySelectorAll("#nav .nav-sub"), function (list) {
      list.hidden = !subVisible;
    });
  }

  // A route render is always intentional here (background updates use state.refresh instead),
  // so scrolling to the top and focusing the route title is the deliberate behaviour. The H1 each
  // view renders is the focus target; without one the <main> takes focus.
  function renderRoute() {
    var parsed = splitHash(root.location ? root.location.hash : "");
    var host = document.getElementById("view");
    if (!host) return;
    state.viewToken++;
    state.refresh = null;
    if (state.cleanup) {
      try { state.cleanup(); } catch (err) { /* cleanup is best effort */ }
      state.cleanup = null;
    }
    if (root.SEMCharts && root.SEMCharts.destroyTree) root.SEMCharts.destroyTree(host);
    clear(host);
    setNav(parsed.route);
    state.cleanup = VIEWS[parsed.route](host, parsed.query) || null;
    if (root.scrollTo) root.scrollTo(0, 0);
    var target = host.querySelector("h1") || host;
    if (typeof target.focus === "function") target.focus({ preventScroll: true });
  }

  /* Deliberate navigation is the only thing that re-renders a route; background updates go through
   * a view's quiet refresh() instead, and are blocked while evidence is on screen. */

  function refreshHeader() {
    var chip = document.getElementById("conn-status");
    var build = document.getElementById("build-info");
    get("/status").then(function (status) {
      state.status = status;
      if (chip) {
        chip.className = "conn " + (status.ha_connected ? "ok" : "warn");
        chip.textContent = status.ha_connected ? "HA connected" : "HA unreachable";
        chip.title = status.ha_connected
          ? "Home Assistant connected; control authority is on the Dashboard"
          : "Home Assistant is unreachable, so no reading here is live";
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
    // Charts read their colours from CSS custom properties, so an explicit theme change has to
    // re-render them or the UI and the plot disagree until the next mount (UI_REVIEW §3.10).
    if (root.SEMCharts && typeof root.SEMCharts.refresh === "function") root.SEMCharts.refresh();
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

  /* ------------------------------------------------------------------ navigation and refresh
   * Intentional navigation (a hash change or a view link) may scroll to the route heading and
   * focus it; a background data update may not. Views that can refresh quietly install
   * state.refresh, so returning to a visible tab updates values in place instead of rebuilding
   * the route — which is what protects unsaved settings, expanded rows and stop results
   * (UI_REVIEW §3.5). */

  var NAV_AREAS = {
    "/dashboard": "today",
    "/plan": "plan",
    "/forecasts": "plan",
    "/savings": "savings",
    "/batteries": "manage",
    "/cards": "manage",
    "/settings": "manage",
    "/log": "manage"
  };

  function refreshCurrentView() {
    if (!state.refresh) return;
    state.refresh();
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
    // The footer's About disclosure replaces the two-sentence policy that every route repeated
    // (UI_REVIEW §3.12); "Times: local" stays visible.
    var helpToggle = document.getElementById("help-toggle");
    var help = document.getElementById("help");
    if (helpToggle && help) {
      helpToggle.addEventListener("click", function () {
        var open = help.hidden;
        help.hidden = !open;
        helpToggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
    // Skip to content must move focus, not the route: a hash of #view used to fall through the
    // router to the Dashboard (UI_REVIEW §3.11).
    var skip = document.querySelector(".skip-link");
    if (skip) {
      skip.addEventListener("click", function (event) {
        if (event && event.preventDefault) event.preventDefault();
        var host = document.getElementById("view");
        if (!host) return;
        if (!host.getAttribute("tabindex")) host.setAttribute("tabindex", "-1");
        host.focus();
      });
    }
    if (!root.location.hash) root.location.hash = VIEW_DEFAULT;
    else renderRoute();
    refreshHeader();
    root.setInterval(function () { if (!document.hidden && !anyDialogOpen()) refreshHeader(); }, 15000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { refreshHeader(); refreshCurrentView(); }
    });
  }

  /* ------------------------------------------------------------------ view: dashboard */

  function slotAt(plan, when) {
    if (!plan || !Array.isArray(plan.slots)) return null;
    var now = parseDate(when || new Date());
    if (!now) return null;
    var step = (plan.slot_minutes || 60) * 60000;
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
      }), { numeric: [2] }));
    }
    box.appendChild(button("Dismiss", "", function () { box.remove(); }));
    body.appendChild(box);
  }

  // Rendered through renderAutopilot's append callback like every other answer block, so it must
  // take that callback (`draw`), never a DOM node: passing the callback to appendChild threw
  // "body.appendChild is not a function" whenever a safety condition was live (a pending hold or an
  // unpersisted mode), which aborted the rest of the /status paint and left five panels on
  // "Loading…" for good (0.20.6).
  function renderSafetyState(draw, status) {
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
    draw(box);
  }

  // Who is in control, and whether the dispatcher writes (UI_REVIEW §3.1). The wording is the
  // same one the confirmation dialogs use, so the answer strip and the acknowledgement can never
  // disagree.
  function authorityText(status) {
    var mode = { off: "Off", advisory: "Advisory", auto: "Auto" }[status.mode] || String(status.mode || "unknown");
    return mode + " · " + (status.dry_run
      ? "dry run: the plan dispatcher writes nothing"
      : "dry run off: the plan dispatcher writes to the batteries");
  }

  function batteryName(battery) {
    return (battery.extra && battery.extra.name) || battery.id || "battery";
  }

  // Which write path a Zendure battery really dispatches through (driver capabilities) and how it
  // was chosen (config.control_path), so a switch to the inverter path is visible on the card.
  var DISPATCH_PATHS = {
    manager_manual_power: "Zendure manager: manual power",
    device_limits: "Inverter: AC mode + limits",
    device_setpoint: "Device setpoint",
    unresolved: "unmapped"
  };

  function dispatchPathText(config, capabilities) {
    var path = capabilities && capabilities.control_path ? String(capabilities.control_path) : "";
    var label = DISPATCH_PATHS[path] || (path || "unknown");
    if (capabilities && capabilities.driver === MQTT_ZENDURE_DRIVER) {
      // One path only: the device's own AC mode and limits, through the MQTT bridge's entities.
      return "Inverter over MQTT: AC mode + limits (the device's own entities)";
    }
    if (capabilities && capabilities.driver === NATIVE_ZENDURE_DRIVER) {
      // The add-on speaks the device's own zenSDK HTTP API: no HA entity is involved.
      var connection = capabilities.connection || {};
      var where = connection.host
        ? " (" + connection.host + (String(connection.scheme) === "https" ? " over HTTPS" : " over HTTP") + ")"
        : "";
      return "Device over zenSDK HTTP: AC mode + input/output limits" + where;
    }
    var chosen = String(config && config.control_path || "auto");
    return label + (["manager", "device"].indexOf(chosen) >= 0 ? " (chosen)" : " (auto)");
  }

  // Measured behaviour, stated separately from the plan, so a proposal can never be read as what
  // the battery actually did.
  function measuredText(batteries) {
    var rows = (Array.isArray(batteries) ? batteries : []).filter(function (b) { return isNum(b.power_w); });
    if (!rows.length) return "Measured battery: power unknown";
    var total = rows.reduce(function (sum, b) { return sum + b.power_w; }, 0);
    var who = rows.length === 1 ? batteryName(rows[0]) : rows.length + " batteries";
    var direction = total > 0 ? "charging" : total < 0 ? "discharging" : "idle";
    return "Measured " + who + ": " + direction + " " + fmtPower(Math.abs(total));
  }

  function plannedText(slot) {
    if (!slot) return "Planned now: no active plan slot";
    // The action word carries the direction, so the number beside it is unsigned.
    var power = isNum(slot.power_w) ? fmtPower(Math.abs(slot.power_w)) : "power unknown";
    return "Planned now: " + String(actionText(slot.action)).toLowerCase() + " " + power +
      " — because: " + reasonText(slot.reason) + " (a proposal, not the measured behaviour above)";
  }

  // A dashboard panel body that can be re-drawn in place: quiet refresh re-renders it without
  // touching the rest of the route, so scroll, focus and the stop-result banner survive.
  function dashboardPanel(host, title, cls) {
    var section = el("section", { class: "panel" + (cls ? " " + cls : "") });
    section.appendChild(el("div", { class: "panel-head" }, [el("h2", { text: title })]));
    var body = el("div", { class: "panel-body" });
    section.appendChild(body);
    host.appendChild(section);
    return body;
  }

  function renderAutopilot(draw, status, plan, handles) {
    // 1) the operational answer: authority, measured behaviour, planned behaviour.
    draw(el("p", { class: "answer-authority", text: authorityText(status) }));
    draw(el("p", { class: "answer-measured", text: measuredText(status.batteries) }));
    draw(el("p", { class: "answer-planned", text: plannedText(slotAt(plan, status.now || new Date())) }));
    // 2) unresolved safety conditions directly beside the answer, never below routine metrics.
    renderSafetyState(draw, status);
    if (status.override) {
      var until = status.override.expires_at ? fmtTime(status.override.expires_at) : "?";
      var overrideBox = el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Manual override active" }),
        el("p", { text: fmtPower(status.override.power_w) + (status.override.battery_id ? " on " + status.override.battery_id : " on all batteries") + " until " + until + "." })
      ]);
      var clear = button("Clear override", "btn-danger");
      writeAction({
        button: clear, noteContainer: handles.notes,
        confirm: { title: "Clear override?", body: "Normal plan dispatch resumes.", confirmLabel: "Clear override" },
        request: function () { return del("/override"); },
        successText: function () { return "Override cleared (confirmed by the server)."; },
        onDone: function () { handles.refresh(); }
      });
      overrideBox.appendChild(clear);
      draw(overrideBox);
    }
    connectionIssues(status).forEach(function (issue) {
      draw(el("div", { class: "banner banner-" + (issue.level === "error" ? "error" : "warn") }, [
        el("p", { class: "banner-title", text: issue.level === "error" ? "Error" : "Warning" }),
        el("p", { text: issue.text })
      ]));
    });
    draw(el("p", { class: "legend-note", text: "Last plan " + timeAgo(status.last_plan_at) + ", last tick " + timeAgo(status.last_tick_at) + ". The device has no watchdog: advisory/off and dry run stop new writes, but a setpoint written earlier in a live session stays in force — use Stop now to neutralise it (Stop writes to the hardware even in dry run)." }));
  }

  function renderAlerts(body, status) {
    var alerts = Array.isArray(status.alerts) ? status.alerts : [];
    var actionable = alerts.filter(function (alert) { return (alert.level || "info") !== "info"; });
    var activity = alerts.filter(function (alert) { return (alert.level || "info") === "info"; });
    if (!actionable.length) {
      body.appendChild(el("p", { class: "muted", text: "No warnings or errors." }));
    } else {
      var list = el("ul", { class: "alert-list" });
      actionable.forEach(function (alert) {
        var level = alert.level || "warning";
        list.appendChild(el("li", { dataset: { level: level } }, [
          el("p", { class: "alert-meta" }, [badge(level, level === "warning" ? "warn" : "err"), " ", fmtDateTime(alert.ts), " (", timeAgo(alert.ts), ")"]),
          el("p", { class: "wrap-anywhere", text: alert.message || alert.code || "(no message)" })
        ]));
      });
      body.appendChild(list);
    }
    // Routine informational events ("a new plan was published") are activity history, not alerts.
    if (activity.length) {
      var details = el("details", { class: "disclosure" }, [el("summary", { text: "Recent activity (" + activity.length + ")" })]);
      var items = el("ul", { class: "alert-list" });
      activity.forEach(function (alert) {
        items.appendChild(el("li", { dataset: { level: "info" } }, [
          el("p", { class: "alert-meta" }, [badge("info", "info"), " ", fmtDateTime(alert.ts), " (", timeAgo(alert.ts), ")"]),
          el("p", { class: "wrap-anywhere", text: alert.message || alert.code || "(no message)" })
        ]));
      });
      details.appendChild(items);
      body.appendChild(details);
    }
  }

  // Primary metrics only: charge, grid direction, price. PV/load and the next windows are the
  // quieter supporting row, so a future expensive window cannot compete with the control state.
  function renderNow(body, data) {
    var status = data.status, plan = data.plan;
    var batteries = Array.isArray(status.batteries) ? status.batteries : [];
    var available = batteries.filter(function (b) { return b.available && isNum(b.soc_pct); });
    var avgSoc = available.length ? available.reduce(function (sum, b) { return sum + b.soc_pct; }, 0) / available.length : null;
    var anyStale = batteries.some(function (b) { return b.stale; });
    var grid = status.grid_now_w;
    var gridText = isNum(grid) ? (grid > 0 ? "importing" : grid < 0 ? "exporting" : "balanced") : "unknown";
    var rank = status.price_rank_pct;
    var rankText = isNum(rank) ? "rank " + rank.toFixed(0) + " % (" + (rank <= 33 ? "cheapest third" : rank <= 66 ? "middle third" : "most expensive third") + ")" : "rank unknown";
    var slot = slotAt(plan, status.now || new Date());
    body.appendChild(el("div", { class: "cards" }, [
      statCard("Battery charge", withUnit(isNum(avgSoc) ? avgSoc.toFixed(1) : "--", "%"), batteries.length === 1 ? "1 battery reported" : batteries.length + " batteries reported", anyStale),
      statCard("Grid power", fmtPower(grid), gridText + " at the meter"),
      statCard("Price now", fmtPrice(status.price_now), rankText, slot ? slot.price_stale : false)
    ]));
  }

  function renderSupporting(body, data) {
    var status = data.status;
    var cheap = fmtWindow(status.next_cheap_window);
    var expensive = fmtWindow(status.next_expensive_window);
    body.appendChild(el("div", { class: "cards supporting" }, [
      statCard("PV power", fmtPower(status.pv_now_w), "production now"),
      statCard("Load power", fmtPower(status.load_now_w), "household consumption now"),
      statCard("Next cheap window", cheap ? cheap.split(" (")[0] : "--", cheap ? cheap.split(" (")[1] || "" : "not known", false),
      statCard("Next expensive window", expensive ? expensive.split(" (")[0] : "--", expensive ? expensive.split(" (")[1] || "" : "not known", false)
    ]));
  }

  // ------------------------------------------------------------------ battery energy price (0.20.4)

  // The cost-basis ledger's two money figures (SPEC 5/10, additive 0.20.4): the average price paid
  // for the AC energy in the pack and the break-even sell price the optimiser acts on.  The values
  // come from /status.cost_basis -- the ledger the optimizer reads -- with the round trip and wear
  // the plan assumed; a missing field is null (rendered "--"), never a guessed zero.  Pure data,
  // no DOM, so the offline render check can assert every state directly.
  function costBasisView(status, plan) {
    var block = status && status.cost_basis && typeof status.cost_basis === "object" ? status.cost_basis : null;
    var summary = (plan && plan.summary) || (status && status.active_plan && status.active_plan.summary) || {};
    var rows = block && Array.isArray(block.batteries) ? block.batteries : [];
    var roundTrip = isNum(summary.round_trip_efficiency) && summary.round_trip_efficiency > 0 ? summary.round_trip_efficiency : null;
    var batteries = rows.map(function (row) {
      row = row || {};
      return {
        id: row.id || null,
        name: row.name || row.id || "battery",
        enabled: row.enabled !== false,
        basis: isNum(row.cost_basis_eur_per_kwh) ? row.cost_basis_eur_per_kwh : null,
        floor: isNum(row.sell_floor_eur_per_kwh) ? row.sell_floor_eur_per_kwh : null,
        energyKwh: isNum(row.energy_kwh) ? row.energy_kwh : null,
        valueEur: isNum(row.stored_value_eur) ? row.stored_value_eur : null,
        updatedAt: row.updated_at || null,
        stale: row.stale === true,
        stalledSince: row.no_samples_since || null,
        bookings: isNum(row.intervals) ? row.intervals : 0
      };
    });
    var enabledRows = batteries.filter(function (row) { return row.enabled; });
    var stalled = enabledRows.filter(function (row) { return row.stale; });
    // Only a complete sum is a fleet total: one battery without the field would make the sum read
    // as a smaller number rather than as unknown ("never a fabricated figure").
    var energy = enabledRows.length && enabledRows.every(function (row) { return row.energyKwh !== null; })
      ? enabledRows.reduce(function (sum, row) { return sum + row.energyKwh; }, 0) : null;
    var value = enabledRows.length && enabledRows.every(function (row) { return row.valueEur !== null; })
      ? enabledRows.reduce(function (sum, row) { return sum + row.valueEur; }, 0) : null;
    var stamps = enabledRows.map(function (row) { return row.updatedAt; }).filter(Boolean).sort();
    return {
      present: block !== null,
      enabled: !!(block && block.enabled),
      basis: block && isNum(block.fleet_basis_eur_per_kwh) ? block.fleet_basis_eur_per_kwh : null,
      floor: block && isNum(block.sell_floor_eur_per_kwh) ? block.sell_floor_eur_per_kwh : null,
      roundTrip: roundTrip,
      wear: isNum(summary.wear_eur_per_kwh) ? summary.wear_eur_per_kwh : null,
      margin: block && isNum(block.margin_eur_per_kwh) ? block.margin_eur_per_kwh : null,
      batteries: batteries,
      enabledBatteries: enabledRows,
      stale: stalled.length > 0,
      stalledSince: stalled.length ? stalled[0].stalledSince : null,
      stalledNames: stalled.map(function (row) { return row.name; }),
      // The oldest update is the fleet's freshness: it is what a stalled battery freezes.
      updatedAt: stamps.length ? stamps[0] : null,
      energyKwh: energy,
      storedValueEur: value,
      bookings: enabledRows.reduce(function (sum, row) { return sum + row.bookings; }, 0),
      // "Empty" is a ledger with nothing priced: a fresh install, a pack emptied to its floor, or
      // every battery's ledger reset.  It is never the same state as a stale figure.
      empty: enabledRows.length > 0 && enabledRows.every(function (row) { return row.basis === null; }),
      // Optimizer v2 stage 1b: the active plan prices export at the feed-in contract's justified
      // floor (a refusal plan), so the ledger's PV charge price is a conservative bound.  The
      // server sets this only for a dispatched refusal plan, never for a shadow record.
      floorIsBound: !!(block && block.feed_in_floor_is_bound === true),
      feedInReason: summary.feed_in_reason || null
    };
  }

  // "basis ÷ round trip + wear + margin" with only the terms the payload really carries, so a
  // missing round trip, wear or margin is omitted instead of printed as a fake zero (SPEC 5).
  function costBasisBreakdown(model) {
    var parts = [];
    if (isNum(model.basis) && isNum(model.roundTrip) && model.roundTrip > 0) {
      parts.push(fmtPrice(model.basis / model.roundTrip, 3) + " from basis " + fmtPrice(model.basis, 3) +
        " ÷ " + fmtPct(model.roundTrip * 100, 1) + " round trip");
    }
    if (isNum(model.wear)) parts.push(fmtPrice(model.wear, 3) + " wear");
    if (isNum(model.margin)) parts.push(fmtPrice(model.margin, 3) + " margin");
    return parts.join(" + ");
  }

  function costBasisContextText(model) {
    var stored = isNum(model.energyKwh) && isNum(model.storedValueEur)
      ? "Stored " + fmtKwh(model.energyKwh) + " worth " + fmtEur(model.storedValueEur)
      : isNum(model.energyKwh) ? "Stored " + fmtKwh(model.energyKwh)
      : isNum(model.storedValueEur) ? "Stored value " + fmtEur(model.storedValueEur)
      : "Stored energy not reported";
    var updated = model.updatedAt
      ? "ledger updated " + fmtDateTime(model.updatedAt) + " (" + timeAgo(model.updatedAt) + ")"
      : "ledger update time not reported";
    return stored + " · " + updated + ".";
  }

  // One per-battery row: both values, the stored energy, its value and this battery's freshness.
  function batteryPriceRowText(row) {
    var parts = ["average " + fmtPrice(row.basis, 3), "break-even " + fmtPrice(row.floor, 3)];
    if (isNum(row.energyKwh)) parts.push(fmtKwh(row.energyKwh));
    if (isNum(row.valueEur)) parts.push(fmtEur(row.valueEur));
    parts.push(row.updatedAt ? "updated " + timeAgo(row.updatedAt) : "never updated");
    return parts.join(" · ");
  }

  // One compact "Battery energy price" card (0.20.4): both ledger figures, the stored energy and
  // its value, the ledger's freshness, and a visible warning when 0.20.3's stall marker is set --
  // a frozen floor must not look healthy.  A value the ledger cannot report renders "--".
  function renderBatteryEnergyPrice(draw, data) {
    var model = costBasisView(data.status, data.plan);
    if (!model.present) {
      draw(el("p", { class: "muted", text: "The server did not report a cost-basis ledger, so the average charged price and the break-even sell price are unknown." }));
      return;
    }
    var state = model.stale ? "stale" : null;
    draw(el("div", { class: "cards" }, [
      statCard("Average charged price", fmtPrice(model.basis, 3), "weighted average paid for the energy in the pack", isNum(model.basis) ? state : (state || "unknown")),
      statCard("Break-even sell price", fmtPrice(model.floor, 3), isNum(model.basis) ? (costBasisBreakdown(model) || "the plan did not report the round trip and wear") : "no basis yet", isNum(model.floor) ? state : (state || "unknown"))
    ]));
    if (!model.enabled) {
      draw(el("p", { class: "field-help", text: "The sell floor is switched off (optimizer.sell_floor_enabled = false), so no break-even price is derived; the average charged price is still the ledger's." }));
    } else if (!model.enabledBatteries.length) {
      draw(el("p", { class: "muted", text: "No enabled battery has a cost-basis ledger yet." }));
    } else if (model.empty) {
      draw(el("p", { class: "muted", text: model.bookings > 0 ? "The pack is empty: the ledger holds no priced energy, so there is no break-even price yet." : "No ledger yet: no charging interval has been booked, so there is no break-even price yet." }));
    }
    draw(el("p", { class: "legend-note", text: costBasisContextText(model) }));
    if (model.floorIsBound) {
      draw(el("p", { class: "field-help", text: "Feed-in contract: the battery-sourced export leg is refused (unpriced_export), so PV charging is priced at the contract's justified floor. That floor is a conservative bound, not the real export rate." }));
    }
    if (model.stale) {
      draw(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "The ledger is stalled" }),
        el("p", { text: "No usable battery power samples since " + fmtDateTime(model.stalledSince) + " (" + timeAgo(model.stalledSince) + ") for " + model.stalledNames.join(", ") + ": the average charged price and the break-even sell price still show the last booked value and no longer follow the pack." })
      ]));
    }
    if (model.enabledBatteries.length > 1) {
      draw(el("ul", { class: "stat-row" }, model.enabledBatteries.map(function (row) {
        return el("li", null, [
          el("span", { class: "stat-label", text: row.name }),
          el("span", { class: "stat-value", text: batteryPriceRowText(row) }),
          row.stale ? el("span", null, [" ", badge("stale", "stale")]) : null
        ]);
      })));
    }
  }

  function renderBatteryCards(body, status) {
    var batteries = Array.isArray(status.batteries) ? status.batteries : [];
    if (!batteries.length) {
      body.appendChild(el("p", { class: "muted", text: "No batteries configured. Add one in the Batteries view." }));
      return;
    }
    var grid = el("div", { class: "cards" });
    batteries.forEach(function (battery) {
      var name = batteryName(battery);
      var direction = isNum(battery.power_w) ? (battery.power_w > 0 ? "charging" : battery.power_w < 0 ? "discharging" : "idle") : "power unknown";
      // Badges are flattened at the call site: a nested array stringified to [object Object]
      // (UI_REVIEW §3.6).
      var badges = [];
      if (battery.available) badges.push(badge("available", "ok"));
      else badges.push(badge("unavailable", "err"));
      if (battery.stale) badges.push(badge("stale", "stale"));
      var guard = status && status.cell_voltage && Array.isArray(status.cell_voltage.latched)
        ? status.cell_voltage.latched.indexOf(battery.id) >= 0
        : false;
      var guardRow = null;
      if (status && status.cell_voltage && Array.isArray(status.cell_voltage.batteries)) {
        status.cell_voltage.batteries.forEach(function (row) {
          if (row && row.battery_id === battery.id) guardRow = row;
        });
      }
      if (guard) badges.push(badge("low cell voltage", "err"));
      grid.appendChild(el("div", { class: "card" + (battery.stale ? " card-stale" : "") }, [
        el("p", { class: "card-label", text: name }),
        el("p", { class: "card-value" }, withUnit(isNum(battery.soc_pct) ? battery.soc_pct.toFixed(1) : "--", "%")),
        el("p", { class: "card-sub" }, [fmtPower(battery.power_w) + " (" + direction + ")"]),
        el("p", { class: "card-sub" }, [fmtKwh(battery.energy_kwh) + " of " + fmtKwh(battery.capacity_kwh), isNum(battery.temperature_c) ? " · " + battery.temperature_c.toFixed(1) + " °C" : ""]),
        el("p", { class: "card-sub" }, [
          isNum(battery.cell_voltage_v) ? "cell " + Number(battery.cell_voltage_v).toFixed(3) + " V" : "cell voltage unknown",
          guard ? (guardRow && guardRow.force_charge_w ? " · emergency charge active" : " · discharge blocked") : ""
        ]),
        el("p", { class: "card-sub" }, ["read " + timeAgo(battery.read_at), " "].concat(badges)),
        battery.last_error ? el("p", { class: "field-error", text: battery.last_error }) : null
      ]));
    });
    body.appendChild(grid);
  }

  // One line for an active manual override, shared by the safety banner and the panel.
  function manualOverrideText(override) {
    if (!override) return "no override";
    var power = isNum(override.power_w) ? override.power_w : 0;
    var what = power > 0 ? "charge " + fmtPower(power) : power < 0 ? "discharge " + fmtPower(-power) : "hold";
    return what + " until " + fmtTime(override.expires_at) + (override.battery_id ? " on " + override.battery_id : " on all batteries");
  }

  // Manual override (SPEC 5/9.3/10, 0.20.0): a bounded force plus the per-direction manual limits.
  // The force goes through POST/DELETE /override, the limits through POST/DELETE /limits; the copy
  // states what the server really does, so the panel never promises a write the rails will refuse.
  function renderManualOverride(draw, data, handles) {
    var status = data.status || {};
    var batteries = (Array.isArray(status.batteries) ? status.batteries : []).filter(function (b) { return b && b.id; });
    var limits = status.manual_limits && typeof status.manual_limits === "object" ? status.manual_limits : {};
    var override = status.override || null;

    // ---- what is active now, with the same confirmed Clear as the safety banner above
    if (override) {
      var overrideBox = el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Manual override active" }),
        el("p", { text: "Active: " + manualOverrideText(override) + "." })
      ]);
      var clearOverride = button("Clear override", "btn-danger");
      writeAction({
        button: clearOverride, noteContainer: handles.notes,
        confirm: { title: "Clear override?", body: "Normal plan dispatch resumes.", confirmLabel: "Clear override" },
        request: function () { return del("/override"); },
        successText: function () { return "Override cleared (confirmed by the server)."; },
        onDone: function () { handles.refresh(); }
      });
      overrideBox.appendChild(clearOverride);
      draw(overrideBox);
    } else {
      draw(el("p", { class: "muted", text: "No manual override is active; the plan is in control." }));
    }

    // ---- the watts this selection may really command, never above the limit or the maximum
    function configuredMax(battery, direction) {
      var capKey = direction === "charge" ? "cap_charge_w" : "cap_discharge_w";
      var maxKey = direction === "charge" ? "max_charge_w" : "max_discharge_w";
      var rows = battery ? [battery] : batteries;
      return rows.reduce(function (sum, row) {
        if (isNum(row[capKey])) return sum + row[capKey];
        return sum + (isNum(row[maxKey]) ? row[maxKey] : 0);
      }, 0);
    }

    function resolvedLimitText(batteryId, direction) {
      var key = direction === "charge" ? "charge_w" : "discharge_w";
      var own = batteryId ? limits[batteryId] : null;
      var fleet = limits["*"];
      if (own && isNum(own[key])) return fmtPower(own[key]) + " (this battery)";
      if (fleet && isNum(fleet[key])) return fmtPower(fleet[key]) + " (all batteries)";
      return "unlimited";
    }

    // A limit is a per-battery cap, so the all-batteries field is bounded by the largest configured
    // maximum (the API refuses more), never by the sum a fleet-wide *force* may use.
    function limitRangeMax(direction) {
      var maxKey = direction === "charge" ? "max_charge_w" : "max_discharge_w";
      return batteries.reduce(function (highest, row) {
        return Math.max(highest, isNum(row[maxKey]) ? row[maxKey] : 0);
      }, 0);
    }

    // ---- force
    draw(el("h3", { text: "Force" }));
    if (!batteries.length) {
      draw(el("p", { class: "muted", text: "No batteries are configured, so there is nothing to force." }));
    }
    var batterySelect = el("select", { id: "override-battery" }, [el("option", { value: "", text: "All batteries" })].concat(
      batteries.map(function (b) { return el("option", { value: b.id, text: batteryName(b) }); })
    ));
    var directionSelect = el("select", { id: "override-direction" }, [
      el("option", { value: "charge", text: "Charge" }),
      el("option", { value: "discharge", text: "Discharge" }),
      el("option", { value: "hold", text: "Hold" })
    ]);
    directionSelect.value = "charge";
    var powerInput = el("input", { type: "number", id: "override-power", min: "1", step: "50", value: "500" });
    var powerHelp = el("span", { class: "field-help" });
    var powerField = el("label", { class: "field", for: "override-power" }, [
      el("span", { text: "Power (W)" }), powerHelp, powerInput
    ]);
    var minutesSelect = el("select", { id: "override-minutes" }, [5, 15, 30, 60, 120].map(function (minutes) {
      return el("option", { value: String(minutes), text: minutes + " min" });
    }));
    minutesSelect.value = "30";

    function selectedBattery() {
      for (var i = 0; i < batteries.length; i += 1) {
        if (batteries[i].id === batterySelect.value) return batteries[i];
      }
      return null;
    }

    function syncPowerField() {
      var direction = directionSelect.value;
      powerField.hidden = direction === "hold";
      if (direction === "hold") return;
      var max = configuredMax(selectedBattery(), direction);
      powerInput.max = String(Math.max(0, Math.round(max)));
      powerHelp.textContent = max >= 1
        ? "1 to " + fmtPower(max) + " (the configured maximum, lowered by any limit)"
        : "No capacity in this direction: a 0 W limit blocks it.";
    }
    batterySelect.addEventListener("change", syncPowerField);
    directionSelect.addEventListener("change", syncPowerField);
    syncPowerField();

    var apply = button("Apply override", "btn-primary");
    apply.addEventListener("click", function () {
      var direction = directionSelect.value;
      var battery = selectedBattery();
      var hold = direction === "hold";
      var max = hold ? 0 : configuredMax(battery, direction);
      if (!hold && max < 1) {
        setNote(handles.notes, "Nothing to apply: " + direction + " is blocked by a 0 W limit or has no capacity.", true);
        return;
      }
      var asked = Number(powerInput.value);
      var watts = hold ? 0 : Math.min(Math.max(1, isFinite(asked) ? Math.round(asked) : 1), Math.round(max));
      var signed = direction === "charge" ? watts : direction === "discharge" ? -watts : 0;
      var minutes = Number(minutesSelect.value) || 30;
      var who = battery ? batteryName(battery) : "all batteries";
      var what = hold ? "a hold at 0 W" : direction + " " + fmtPower(watts);
      confirmAction({
        title: "Apply manual override?",
        body: "Sets " + what + " on " + who + " for " + minutes + " min. The plan is bypassed until it expires or you clear it; the cell-voltage protection, the SOC floor and the manual limits still apply." + (status.dry_run ? " Dry run is on, so this is logged and not written to the hardware." : ""),
        confirmLabel: "Apply override"
      }).then(function (ok) {
        if (!ok) return;
        performWrite(apply, handles.notes, function () {
          return post("/override", { power_w: signed, minutes: minutes, battery_id: battery ? battery.id : null });
        }, function () { return "Manual override applied (confirmed by the server)."; }, function () { handles.refresh(); });
      });
    });
    draw(el("div", { class: "fields" }, [
      el("label", { class: "field", for: "override-battery" }, [el("span", { text: "Battery" }), batterySelect]),
      el("label", { class: "field", for: "override-direction" }, [el("span", { text: "Direction" }), directionSelect]),
      powerField,
      el("label", { class: "field", for: "override-minutes" }, [el("span", { text: "Duration" }), minutesSelect])
    ]));
    draw(el("div", { class: "toolbar" }, [apply]));
    draw(el("p", {
      class: "field-help",
      text: String(status.mode) === "auto"
        ? (status.dry_run
          ? "Dry run is on: an override is not written to the hardware. Only Stop now writes in dry run."
          : "Mode auto with dry run off: an applied override writes to the hardware.")
        : "Mode is " + (String(status.mode) || "unknown") + ": nothing is dispatched at all."
    }));

    // ---- limits
    draw(el("h3", { text: "Limits" }));
    var limitRows = el("div", { class: "fields" });

    function limitRow(battery, label) {
      var batteryId = battery ? battery.id : null;
      var own = batteryId ? (limits[batteryId] || {}) : (limits["*"] || {});
      var chargeMax = battery ? (isNum(battery.max_charge_w) ? battery.max_charge_w : 0) : limitRangeMax("charge");
      var dischargeMax = battery ? (isNum(battery.max_discharge_w) ? battery.max_discharge_w : 0) : limitRangeMax("discharge");
      var chargeInput = el("input", {
        type: "number", id: (batteryId || "fleet") + "-limit-charge", min: "0",
        max: String(Math.max(0, Math.round(chargeMax))), step: "50", placeholder: "unlimited",
        value: isNum(own.charge_w) ? String(own.charge_w) : ""
      });
      var dischargeInput = el("input", {
        type: "number", id: (batteryId || "fleet") + "-limit-discharge", min: "0",
        max: String(Math.max(0, Math.round(dischargeMax))), step: "50", placeholder: "unlimited",
        value: isNum(own.discharge_w) ? String(own.discharge_w) : ""
      });
      var set = button("Set", "btn-primary");
      set.addEventListener("click", function () {
        var payload = { battery_id: batteryId };
        ["charge", "discharge"].forEach(function (direction) {
          var input = direction === "charge" ? chargeInput : dischargeInput;
          var raw = String(input.value === undefined || input.value === null ? "" : input.value).trim();
          payload[direction + "_w"] = raw === "" ? null : Number(raw);
        });
        if (payload.charge_w !== null && !isFinite(payload.charge_w)) {
          setNote(handles.notes, "Max charge must be a number or empty.", true);
          return;
        }
        if (payload.discharge_w !== null && !isFinite(payload.discharge_w)) {
          setNote(handles.notes, "Max discharge must be a number or empty.", true);
          return;
        }
        performWrite(set, handles.notes, function () {
          return post("/limits", payload);
        }, function () {
          return "Limits set for " + label + " (confirmed by the server).";
        }, function () { handles.refresh(); });
      });
      var clearLimits = button("Clear", "");
      clearLimits.addEventListener("click", function () {
        confirmAction({
          title: "Clear limits for " + label + "?",
          body: "The configured maximum applies again in both directions for " + (battery ? "this battery" : "the all-batteries entry") + ".",
          confirmLabel: "Clear limits"
        }).then(function (ok) {
          if (!ok) return;
          performWrite(clearLimits, handles.notes, function () {
            return post("/limits", { battery_id: batteryId, charge_w: null, discharge_w: null });
          }, function () { return "Limits cleared for " + label + " (confirmed by the server)."; }, function () { handles.refresh(); });
        });
      });
      return el("div", { class: "field" }, [
        el("span", { class: "field-label", text: label }),
        el("p", { class: "field-help", text: "Charge: " + resolvedLimitText(batteryId, "charge") + " · Discharge: " + resolvedLimitText(batteryId, "discharge") + ". Configured maximum " + fmtPower(chargeMax) + " charge / " + fmtPower(dischargeMax) + " discharge. An empty field clears that direction; a 0 W value blocks it." }),
        el("div", { class: "row" }, [
          el("label", { class: "field", for: chargeInput.id }, [el("span", { text: "Max charge (W)" }), chargeInput]),
          el("label", { class: "field", for: dischargeInput.id }, [el("span", { text: "Max discharge (W)" }), dischargeInput]),
          set,
          clearLimits
        ])
      ]);
    }

    limitRows.appendChild(limitRow(null, "All batteries"));
    batteries.forEach(function (battery) { limitRows.appendChild(limitRow(battery, batteryName(battery))); });
    draw(limitRows);
    draw(el("p", { class: "field-help", text: "Limits cap both the automatic plan and manual overrides. They can never raise a battery's configured maximum." }));
    draw(el("p", { class: "field-help", text: "A manual override bypasses the plan until it expires or you clear it; it is still bounded by the cell-voltage protection, the SOC floor and the limits above." }));
  }

  function viewDashboard(host) {
    var data = { status: null, plan: null };
    var modeButtons = {};
    var confirmed = el("p", { class: "control-state" });
    var notes = el("div", { class: "answer-notes" });
    var results = el("div", { class: "answer-results", dataset: { stopResultHost: "1" } });

    function setConfirmed(status) {
      confirmed.textContent = "Confirmed by the server: mode " + status.mode + (status.dry_run ? " · dry run on" : " · dry run off") + ".";
    }
    function modeLabel(mode) { return { off: "Off", advisory: "Advisory", auto: "Auto" }[mode] || mode; }
    function syncButton(b, active, mode) {
      b.setAttribute("aria-pressed", active ? "true" : "false");
      b.setAttribute("aria-label", "Set mode to " + modeLabel(mode) + (active ? " (current)" : ""));
      if (active) b.classList.add("btn-primary");
      else b.classList.remove("btn-primary");
    }
    function syncModeButtons(status) {
      Object.keys(modeButtons).forEach(function (mode) { syncButton(modeButtons[mode], status.mode === mode, mode); });
    }

    // ---- the panels ------------------------------------------------------------------------
    var answerDynamic = el("div", { class: "answer-dynamic" });
    var controls = el("div", { class: "answer-controls" });
    var answerBody = dashboardPanel(host, "Now", "panel-answer");
    answerBody.appendChild(answerDynamic);
    answerBody.appendChild(notes);

    function drawAnswer() {
      renderInto(answerDynamic, function (target) {
        renderAutopilot(function (node) { target.appendChild(node); }, data.status, data.plan, { notes: notes, refresh: refresh });
      });
    }
    function paintAnswer() { if (data.status) { drawAnswer(); setConfirmed(data.status); } }

    var alertsBody = dashboardPanel(host, "Warnings and activity");
    // Manual override (0.20.0): after the Now/alerts area, before the plan chart. Its own note and
    // dynamic hosts stay outside the redrawn area, so a write result survives a quiet refresh.
    var overrideBody = dashboardPanel(host, "Manual override");
    var overrideDynamic = el("div", { class: "answer-dynamic" });
    var overrideNotes = el("div", { class: "answer-notes" });
    overrideBody.appendChild(overrideDynamic);
    overrideBody.appendChild(overrideNotes);
    var nowBody = dashboardPanel(host, "Right now");
    // Battery energy price (0.20.4): the ledger's two money figures sit with the live readings.
    var priceBody = dashboardPanel(host, "Battery energy price");
    var supportBody = dashboardPanel(host, "Supporting measurements", "panel-quiet");
    var chartBody = dashboardPanel(host, "Next 24 hours");
    var batteryBody = dashboardPanel(host, "Batteries");
    [alertsBody, nowBody, priceBody, supportBody, chartBody, batteryBody].forEach(function (body) {
      panelStatus(body, "loading", "Loading…");
    });

    // The controls are built once and survive every quiet refresh, so a poll can never replace a
    // button mid-write; each button binds exactly one handler (UI_REVIEW §3.3: the old code bound
    // writeAction inside the click, so the first Off/Advisory/Auto press only installed it).
    ["off", "advisory", "auto"].forEach(function (mode) {
      var b = button(modeLabel(mode), "");
      modeButtons[mode] = b;
      syncButton(b, false, mode);
      b.addEventListener("click", function () {
        writeAction({
          button: b, noteContainer: notes, confirm: modeChangeConfirm(mode),
          request: function () { return post("/mode", { mode: mode, dry_run: data.status ? data.status.dry_run : true }); },
          successText: function (res) { return "Mode is now " + res.mode + " (confirmed by the server)."; },
          onDone: function (res) {
            var fresh = data.status || {};
            fresh.mode = res.mode;
            if (typeof res.dry_run === "boolean") fresh.dry_run = res.dry_run;
            data.status = fresh;
            state.status = fresh;
            syncModeButtons(fresh);
            if (dryBox) dryBox.checked = !!fresh.dry_run;
            setConfirmed(fresh);
            paintAnswer();
            refreshHeader();
          }
        });
      });
      controls.appendChild(b);
    });

    var dryBox = null;
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
        setNote(notes, "Pending… sending a real 0 W stop to every battery", false);
        post("/stop").then(function (res) {
          stop.classList.remove("pending");
          stop.removeAttribute("aria-busy");
          stop.disabled = false;
          setNote(notes, "Stop confirmed by the server: mode is now " + res.mode + ".", false);
          renderStopResults(results, res, data.status || {});
          var fresh = data.status || {};
          fresh.mode = res.mode;
          data.status = fresh;
          syncModeButtons(fresh);
          if (dryBox) dryBox.checked = !!fresh.dry_run;
          refreshHeader();
          get("/status").then(function (status) {
            data.status = status;
            state.status = status;
            var resultsBox = results.querySelector("[data-stop-result]");
            if (!resultsBox) return;
            resultsBox.appendChild(el("p", { class: status.mode_persisted ? "muted" : "field-error", text: status.mode_persisted ? "Mode persisted to the settings file." : "Mode NOT persisted: after a restart the add-on would return to the previous mode." }));
            var pending = Array.isArray(status.pending_holds) ? status.pending_holds : [];
            if (pending.length) resultsBox.appendChild(el("p", { class: "field-error", text: "Still awaiting a neutral hold: " + pending.join(", ") + " — a real setpoint may still be in force." }));
            setConfirmed(status);
            syncModeButtons(status);
            if (dryBox) dryBox.checked = !!status.dry_run;
            paintAnswer();
          }).catch(function () { /* the box already shows the stop response */ });
        }).catch(function (err) {
          stop.classList.remove("pending");
          stop.removeAttribute("aria-busy");
          stop.disabled = false;
          setNote(notes, "Error: " + (err && err.message ? err.message : String(err)), true);
        });
      });
    });
    controls.appendChild(stop);

    dryBox = el("input", { type: "checkbox", id: "dry-run-toggle", checked: true });
    var dryLabel = el("label", { class: "switch-label", for: "dry-run-toggle" }, [dryBox, " Dry run (the plan dispatcher writes nothing)"]);
    dryBox.addEventListener("change", function () {
      var next = dryBox.checked;
      function apply() {
        performWrite(dryBox, notes, function () {
          return post("/mode", { mode: data.status ? data.status.mode : "advisory", dry_run: next });
        }, function (res) {
          return "Dry run is now " + (res.dry_run ? "on" : "off") + " (confirmed by the server).";
        }, function (res) {
          var fresh = data.status || {};
          fresh.dry_run = res.dry_run;
          if (res.mode) fresh.mode = res.mode;
          data.status = fresh;
          setConfirmed(fresh);
          paintAnswer();
          refreshHeader();
        }, function () {
          // The requested checkbox state is not the confirmed control state: undo the preview
          // (UI_REVIEW §3.3).
          dryBox.checked = !!(data.status && data.status.dry_run);
        });
      }
      if (!next) {
        confirmAction(dryRunOffConfirm()).then(function (ok) {
          if (ok) apply();
          else dryBox.checked = !!(data.status && data.status.dry_run);
        });
      } else {
        apply();
      }
    });
    var modeToolbar = el("div", { class: "toolbar" }, Object.keys(modeButtons).map(function (mode) { return modeButtons[mode]; }).concat([dryLabel]));
    var controlDisclosure = el("details", { class: "disclosure", dataset: { controlSettings: "1" } }, [
      el("summary", { text: "Control settings (mode and dry run)" }),
      el("div", { class: "disclosure-body" }, [
        modeToolbar,
        confirmed,
        el("p", { class: "field-help", text: "Routine mode editing lives here; Stop now stays in the status strip above." }),
        el("p", { class: "field-help", text: authorityNote(data.status ? data.status.dry_run : true) })
      ])
    ]);
    controls.appendChild(controlDisclosure);
    answerBody.appendChild(controls);
    answerBody.appendChild(results);

    function paintAlerts() {
      renderInto(alertsBody, function (target) { clear(target); renderAlerts(target, data.status); });
    }
    function paintNow() {
      renderInto(nowBody, function (target) { clear(target); renderNow(target, data); });
    }
    function paintPrice() {
      renderInto(priceBody, function (target) { renderBatteryEnergyPrice(function (node) { target.appendChild(node); }, data); });
    }
    function paintSupporting() {
      renderInto(supportBody, function (target) { clear(target); renderSupporting(target, data); });
    }
    function paintBatteries() {
      renderInto(batteryBody, function (target) { renderBatteryCards(target, data.status); });
    }
    function paintManualOverride() {
      if (!data.status) {
        panelStatus(overrideDynamic, "loading", "Loading…");
        return;
      }
      // A poll must never clobber a value being typed: while a number field or select inside the
      // panel has focus the redraw waits, and the confirmed state is painted as soon as focus
      // leaves (UI_REVIEW §3.5). A focused button does not hold it back.
      var active = document.activeElement;
      var editing = active && within(active, overrideDynamic)
        && ["INPUT", "SELECT", "TEXTAREA"].indexOf(active.tagName) >= 0;
      if (editing) return;
      renderInto(overrideDynamic, function (target) {
        renderManualOverride(function (node) { target.appendChild(node); }, data, { notes: overrideNotes, refresh: refresh });
      });
    }
    function paintChart() {
      renderInto(chartBody, function (target) {
        clear(target);
        if (!data.plan) {
          panelStatus(target, "error", "The next-24-hours chart needs the plan, which could not be loaded. The live readings above still update.", function () { refresh(); });
          return;
        }
        var chart = el("div", { class: "chart chart-sm" });
        target.appendChild(chart);
        root.SEMCharts.mini(chart, data.plan);
        target.appendChild(el("p", { class: "legend-note", text: "Every slot value is also listed in the Plan view; the legend follows the pointer or touch." }));
      });
    }

    // ---- quiet refresh: data updates in place, never a route rebuild (UI_REVIEW §3.5) --------
    function refresh() {
      get("/status").then(function (status) {
        data.status = status;
        state.status = status;
        paintAnswer();
        paintAlerts();
        paintManualOverride();
        paintNow();
        paintPrice();
        paintSupporting();
        paintBatteries();
        setConfirmed(status);
        syncModeButtons(status);
        if (dryBox) dryBox.checked = !!status.dry_run;
      }, function () { /* keep the last confirmed readings on screen; the header reports the error */ });
      get("/plan", { hours: 24 }).then(function (plan) {
        data.plan = plan;
        paintAnswer();
        paintChart();
        paintPrice();
      }, function () { /* the chart panel keeps its own retry; live readings still update */ });
    }

    get("/status").then(function (status) {
      data.status = status;
      state.status = status;
      paintAnswer();
      setConfirmed(status);
      paintAlerts();
      paintManualOverride();
      paintNow();
      paintPrice();
      paintSupporting();
      paintBatteries();
      syncModeButtons(status);
      dryBox.checked = !!status.dry_run;
    }, function (err) {
      panelStatus(answerDynamic, "error", "Could not load the live status: " + (err && err.message ? err.message : err), function () { refresh(); });
      panelStatus(overrideDynamic, "error", "The manual override controls need the live status, which could not be loaded.", function () { refresh(); });
    });
    get("/plan", { hours: 24 }).then(function (plan) {
      data.plan = plan;
      paintAnswer();
      paintChart();
      paintPrice();
    }, function (err) {
      panelStatus(chartBody, "error", "Could not load the 24-hour plan: " + (err && err.message ? err.message : err), function () { refresh(); });
    });

    state.refresh = function () {
      if (refreshBlocked()) return;
      refresh();
    };
    var timer = root.setInterval(function () {
      if (document.hidden || refreshBlocked()) return;
      refresh();
    }, 15000);
    return function () {
      root.clearInterval(timer);
      if (state.refresh) state.refresh = null;
    };
  }

  /* ------------------------------------------------------------------ view: plan */

  function horizonFrom(query) {
    var value = parseInt((query && query.hours) || "36", 10);
    return [12, 24, 36, 48].indexOf(value) >= 0 ? value : 36;
  }

  // Scheduled full-charge maintenance (SPEC 8.1): the plan-level block as one plain sentence.
  function maintenancePlanText(maintenance) {
    if (!maintenance || maintenance.active !== true) return "";
    var target = isNum(maintenance.target_soc_pct) ? fmtPct(maintenance.target_soc_pct, 0) : "the configured target";
    var ids = Array.isArray(maintenance.batteries) ? maintenance.batteries : [];
    var who = ids.length ? ids.join(", ") : "the fleet";
    var when = maintenance.overdue === true ? "overdue" : (maintenance.due === true ? "due" : "scheduled within this horizon");
    return "Maintenance plan: a " + when + " full charge for " + who + " — the optimiser targets " + target +
      " before the deadline, in the cheapest slots, so the BMS can balance the cells.";
  }

  // The per-battery maintenance state as one plain sentence ("due in N days / active / last
  // completed / overdue"), never colour alone (SPEC 13.1).
  function maintenanceText(row) {
    if (!row || row.watching !== true) return "off";
    var target = isNum(row.target_soc_pct) ? fmtPct(row.target_soc_pct, 0) : "the target";
    var parts = [];
    if (row.overdue) parts.push("overdue — " + target + " not reached by the deadline");
    else if (row.due) parts.push("due now — target " + target);
    else if (isNum(row.due_in_days)) parts.push("due in " + Math.max(0, row.due_in_days).toFixed(row.due_in_days < 10 ? 1 : 0) + " days (target " + target + ")");
    else parts.push("scheduled — target " + target);
    if (row.at_target) parts.push("at target");
    if (row.last_completed) parts.push("last completed " + timeAgo(row.last_completed));
    return parts.join(" · ");
  }

  function planFlags(plan) {
    var slots = plan.slots || [];
    var stale = slots.filter(function (s) { return s.price_stale; }).length;
    var flags = [];
    if (!plan.feasible) flags.push({ level: "error", title: "Plan is infeasible", text: plan.infeasible_reason || "The optimiser could not satisfy the constraints; the battery is held safe." });
    if (stale) {
      var fresh = slots.length - stale;
      flags.push({
        level: "warn",
        title: "Stale prices",
        text: stale + " of " + slots.length + " slots carry no fresh price (grey in the timeline). " +
          "The optimiser treats a stale price as flat, so those slots cannot argue for or against a " +
          "charge; this plan leans on the " + fresh + " slot" + (fresh === 1 ? "" : "s") + " with a fresh price."
      });
    }
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
    // The plan reports the wear it actually assumed; the configured value is only a fallback for
    // older payloads (the advanced override is already baked into the reported value).
    var wear = isNum(s.wear_eur_per_kwh) ? s.wear_eur_per_kwh : cycleCost;
    // The horizon's answer first: saving (or loss) against the do-nothing baseline. A loss is a
    // result, never a stale reading (UI_REVIEW §3.6/§3.7).
    var savingKnown = isNum(s.saved_eur);
    var savingText = savingKnown
      ? (s.saved_eur < 0 ? "a loss of " + fmtEur(Math.abs(s.saved_eur)) : fmtEur(s.saved_eur) + " saved")
      : "not reported for this horizon";
    body.appendChild(el("p", { class: "plan-answer" }, [
      "Estimated over this horizon: ",
      el("strong", { class: savingKnown && s.saved_eur < 0 ? "answer-loss" : "answer-gain", text: savingText }),
      " against doing nothing (the same consumption and tariffs, battery idle)."
    ]));
    // A maintenance plan says why it charges to the ceiling before the summary numbers (SPEC 8.1).
    var maintenance = maintenancePlanText(plan.maintenance || (plan.summary && plan.summary.maintenance));
    if (maintenance) {
      body.appendChild(el("p", { class: "legend-note" }, [
        badge("maintenance plan", "info"), " ", maintenance
      ]));
    }
    var stats = [
      // "Net" because it can be negative: an export-heavy horizon earns more than it spends.
      ["Estimated net cost", fmtEur(s.cost_estimate_eur)],
      ["Baseline net cost (no battery)", fmtEur(s.baseline_cost_eur)],
      ["Saving vs baseline", fmtEur(s.saved_eur)],
      ["Energy charged", fmtKwh(s.charged_kwh)],
      ["Energy discharged", fmtKwh(s.discharged_kwh)],
      ["Cycles", isNum(s.cycles) ? s.cycles.toFixed(2) + " cycles" : "--"],
      ["Arbitrage energy", fmtKwh(s.arbitrage_kwh)],
      ["Assumed round trip", efficiency ? fmtPct(efficiency.round_trip_pct) : "not reported"],
      ["Assumed wear", isNum(wear) ? fmtEur(wear) + "/kWh delivered" : "not reported"]
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
    if (s.saved_eur < 0) {
      body.appendChild(el("p", { class: "field-error", text: "The plan is more expensive than doing nothing over this horizon; check the price source and settings." }));
    }
    // Worked examples and model assumptions are evidence behind a named disclosure, not the first
    // reading (UI_REVIEW §3.7).
    var why = el("details", { class: "disclosure" }, [el("summary", { text: "Why this plan?" })]);
    var whyBody = el("div", { class: "disclosure-body" });
    if (efficiency) {
      var example = buyNow != null ? " At the current buy price of " + fmtPrice(buyNow) + " that is a break-even of " + fmtPrice(planBreakEvenEur(buyNow, efficiency, wear)) + ", which includes " + fmtEur(wear) + " of wear per kWh delivered." : "";
      whyBody.appendChild(el("p", { class: "field-help", text: "The dotted line on the timeline is the break-even after round-trip loss: buy ÷ round trip + wear, so a discharge slot only pays when the sale price clears it. The optimiser assumed a " + fmtPct(efficiency.round_trip_pct) + " round trip (charge " + fmtPct(efficiency.eta_charge * 100) + " / discharge " + fmtPct(efficiency.eta_discharge * 100) + "). Wear is charged per kWh delivered, so charging alone costs none." + example }));
    } else {
      whyBody.appendChild(el("p", { class: "field-help", text: "The plan summary did not report its aggregate efficiency, so the assumed round trip and the break-even curve are unavailable; the plan was still computed with the stored per-battery efficiencies." }));
    }
    whyBody.appendChild(el("p", { class: "field-help", text: "Every slot carries its action and reason in the tables below; a slot that would lose money to the round trip stays idle with reason “efficiency_loss”, which is distinct from “below the spread floor you set” (your own minimum spread)." }));
    why.appendChild(whyBody);
    body.appendChild(why);
  }

  // Slot length comes from the payload, never assumed: a 5-minute plan must not be captioned as
  // 15-minute resolution (UI_REVIEW §3.5/§3.6).
  function planSlotMinutes(plan) {
    var minutes = plan && isNum(plan.slot_minutes) && plan.slot_minutes > 0 ? plan.slot_minutes : 60;
    return Math.round(minutes);
  }

  // Cents per kWh, the vocabulary the battery cards already use ("20 ct/kWh"): "0.2810 EUR/kWh"
  // beside "0.5120 EUR/kWh" is unreadable on a 360 px screen.
  function fmtCt(value, digits) {
    if (!isNum(value)) return "--";
    return (value * 100).toFixed(digits === undefined ? 1 : digits) + " ct/kWh";
  }

  // Rank of one buy price inside the plan horizon: 1 = the most expensive slot. `/status` reports
  // the same idea for "now" as `price_rank_pct` (SPEC 4.13); this is its per-window form.
  function priceRank(values, value) {
    if (!isNum(value) || !Array.isArray(values) || !values.length) return null;
    var above = 0;
    values.forEach(function (candidate) { if (isNum(candidate) && candidate > value) above += 1; });
    return { rank: above + 1, total: values.length };
  }

  // Action windows: consecutive slots with the same action, reason, power and staleness become one
  // row, so a constraint change is never hidden inside a broad summary (UI_REVIEW §3.4).
  //
  // The window also carries everything the row states about that time slot: the prices behind the
  // decision, the energy and SoC it moves, the planned grid flow, and the per-slot execution
  // evidence joined from GET /plan/execution (what was dispatched and what was measured). Pure
  // data: no DOM, so the offline render check can assert the numbers directly.
  function planActionWindows(plan, execution) {
    var slots = Array.isArray(plan && plan.slots) ? plan.slots : [];
    var minutes = planSlotMinutes(plan);
    var stepMs = minutes * 60000;
    var byTime = {};
    ((execution && execution.slots) || []).forEach(function (entry) {
      if (entry && entry.t) byTime[entry.t] = entry;
    });
    var windows = [];

    function add(win, slot) {
      var exec = byTime[slot.t] || {};
      var power = isNum(slot.power_w) ? slot.power_w : 0;
      // The slot's own modelled duration (0.20.1, SPEC 8.16): a partial first slot is not a full
      // clock hour, so its energy and cost use the window the solver did.  The timestamp grid (and
      // therefore the window end below) stays clock-aligned.
      var slotMinutes = isNum(slot.minutes) && slot.minutes > 0 ? slot.minutes : minutes;
      var slotHours = slotMinutes / 60;
      win.windowMinutes += slotMinutes;
      win.last = slot;
      win.buySum += isNum(slot.buy) ? slot.buy : 0;
      win.sellSum += isNum(slot.sell) ? slot.sell : 0;
      win.pvSum += isNum(slot.pv_kw) ? slot.pv_kw : 0;
      win.loadSum += isNum(slot.load_kw) ? slot.load_kw : 0;
      win.charged_kwh += Math.max(0, power) / 1000 * slotHours;
      win.discharged_kwh += Math.max(0, -power) / 1000 * slotHours;
      // The plan's own grid model (models.PlanSlot.grid_kw): positive = importing.
      var grid = (isNum(slot.load_kw) ? slot.load_kw : 0) - (isNum(slot.pv_kw) ? slot.pv_kw : 0) + power / 1000;
      win.grid_import_kwh += Math.max(0, grid) * slotHours;
      win.grid_export_kwh += Math.max(0, -grid) * slotHours;
      win.cost_eur += (grid >= 0 ? grid * (slot.buy || 0) : grid * (slot.sell || 0)) * slotHours;
      if (isNum(slot.load_p10)) win.bandLow = win.bandLow == null ? slot.load_p10 : Math.min(win.bandLow, slot.load_p10);
      if (isNum(slot.load_p90)) win.bandHigh = win.bandHigh == null ? slot.load_p90 : Math.max(win.bandHigh, slot.load_p90);
      if (isNum(slot.soc_pct)) {
        if (win.socStart == null) win.socStart = slot.soc_pct;
        win.socEnd = slot.soc_pct;
      }
      if (exec.dispatch) {
        win.dispatch = exec.dispatch;   // the most recent attempt in the window
        win.dispatchSlots += 1;
        if (!exec.dispatch.ok || exec.dispatch.clamped) win.dispatchProblems += 1;
      }
      if (exec.actual) {
        win.actual = exec.actual;
        win.actualSlots += 1;
        win.actualSamples += exec.actual.samples || 0;
        win.actualMinutes += (exec.actual.coverage_pct || 0) / 100 * minutes;
        win.measured_charged_kwh += exec.actual.charged_kwh || 0;
        win.measured_discharged_kwh += exec.actual.discharged_kwh || 0;
        win.measured_import_kwh += exec.actual.import_kwh || 0;
        win.measured_export_kwh += exec.actual.export_kwh || 0;
        win.measured_cost_eur += (exec.actual.import_kwh || 0) * (slot.buy || 0)
          - (exec.actual.export_kwh || 0) * (slot.sell || 0);
      }
    }

    slots.forEach(function (slot) {
      var previous = windows[windows.length - 1];
      var same = previous && previous.action === slot.action && previous.reason === slot.reason &&
        !!previous.stale === !!slot.price_stale &&
        (isNum(previous.power_w) && isNum(slot.power_w) ? previous.power_w === slot.power_w : true);
      if (same) {
        // `end` is the moment the last slot ends, so a two-slot window reads 06:45–07:15 rather
        // than stopping at the last slot's start.
        previous.end = slot.t;
        previous.endMs = parseDate(slot.t) ? parseDate(slot.t).getTime() + stepMs : null;
        previous.slots += 1;
        add(previous, slot);
        return;
      }
      var startMs = parseDate(slot.t) ? parseDate(slot.t).getTime() : null;
      var win = {
        start: slot.t,
        end: slot.t,
        startMs: startMs,
        endMs: startMs === null ? null : startMs + stepMs,
        slots: 1,
        action: slot.action,
        reason: slot.reason,
        power_w: slot.power_w,
        stale: !!slot.price_stale,
        first: slot,
        last: slot,
        buySum: 0,
        sellSum: 0,
        pvSum: 0,
        loadSum: 0,
        bandLow: null,
        bandHigh: null,
        charged_kwh: 0,
        discharged_kwh: 0,
        grid_import_kwh: 0,
        grid_export_kwh: 0,
        cost_eur: 0,
        socStart: null,
        socEnd: null,
        dispatch: null,
        dispatchSlots: 0,
        dispatchProblems: 0,
        actual: null,
        actualSlots: 0,
        actualSamples: 0,
        actualMinutes: 0,
        windowMinutes: 0,
        measured_charged_kwh: 0,
        measured_discharged_kwh: 0,
        measured_import_kwh: 0,
        measured_export_kwh: 0,
        measured_cost_eur: 0
      };
      add(win, slot);
      windows.push(win);
    });
    windows.forEach(function (win) {
      win.buy = win.buySum / win.slots;
      win.sell = win.sellSum / win.slots;
      win.pv_kw = win.pvSum / win.slots;
      win.load_kw = win.loadSum / win.slots;
      win.spread = win.sell - win.buy;
      win.minutes = win.windowMinutes;
      win.hours = win.minutes / 60;
    });
    return windows;
  }

  // A window's numbers as label/value facts. Pure: the renderer only wraps these in elements, and
  // the offline check asserts them without a browser.
  function windowFacts(win, ctx) {
    ctx = ctx || {};
    var facts = [];
    var margin = null;
    var breakEven = planBreakEvenEur(win.buy, ctx.efficiency, ctx.wear);
    if (isNum(breakEven) && isNum(win.sell)) margin = win.sell - breakEven;
    var energy = [];
    if (win.charged_kwh > 0) energy.push(fmtKwh(win.charged_kwh) + " in");
    if (win.discharged_kwh > 0) energy.push(fmtKwh(win.discharged_kwh) + " out");
    var cost = win.cost_eur;
    var grid = win.grid_export_kwh > 0 && win.grid_import_kwh <= 0
      ? "exports " + fmtKwh(win.grid_export_kwh)
      : win.grid_import_kwh > 0 && win.grid_export_kwh <= 0
        ? "imports " + fmtKwh(win.grid_import_kwh)
        : "imports " + fmtKwh(win.grid_import_kwh) + ", exports " + fmtKwh(win.grid_export_kwh);
    facts.push({
      label: "Buy → sell",
      value: fmtCt(win.buy) + " → " + fmtCt(win.sell) +
        (isNum(win.spread) ? " (" + (win.spread >= 0 ? "+" : "−") + fmtCt(Math.abs(win.spread)) + ")" : "")
    });
    facts.push({
      label: "Break-even",
      value: isNum(breakEven)
        ? fmtCt(breakEven) + (isNum(margin) ? " · " + (margin >= 0 ? "+" : "−") + fmtCt(Math.abs(margin)) + " clear" : "")
        : "efficiency not reported"
    });
    facts.push({ label: "Battery", value: energy.length ? energy.join(" · ") : "no battery energy" });
    facts.push({
      label: "SoC",
      value: isNum(win.socStart) && isNum(win.socEnd)
        ? fmtPct(win.socStart, 0) + " → " + fmtPct(win.socEnd, 0) + (isNum(ctx.floorPct) ? " · floor " + fmtPct(ctx.floorPct, 0) : "")
        : "not reported"
    });
    facts.push({
      label: "Grid cost",
      value: grid + " · " + (cost < 0 ? "earns " + fmtEur(Math.abs(cost)) : "costs " + fmtEur(cost))
    });
    return facts;
  }

  // What actually happened in the window: dispatched vs applied vs measured, or why nothing was.
  // Pure text so the same sentence can be asserted offline (SPEC 10 plan execution).
  function windowStatusText(win, ctx) {
    ctx = ctx || {};
    var nowMs = isNum(ctx.nowMs) ? ctx.nowMs : Date.now();
    var parts = [];
    // A stale slot is refused before anything else is said about it: the dispatcher never executes
    // a slot without a fresh price, so "why 0 W?" is answered by the staleness, not by the row it
    // leaves behind (dispatcher._stale_input_reason).
    if (win.stale) parts.push("not dispatchable: this slot has no fresh price, so the dispatcher refuses it (stale_input)");
    var dispatch = win.dispatch;
    if (dispatch) {
      var applied = fmtPower(Math.abs(isNum(dispatch.applied_w) ? dispatch.applied_w : 0));
      var requested = fmtPower(Math.abs(isNum(dispatch.requested_w) ? dispatch.requested_w : 0));
      var who = (dispatch.batteries || []).map(function (item) {
        return (item.id || "battery") + " " + (item.applied_w < 0 ? "−" : "") + fmtPower(Math.abs(item.applied_w || 0));
      }).join(", ");
      if (dispatch.code === "mode_not_auto") parts.push("not written: the mode is not auto (mode_not_auto)");
      else if (dispatch.dry_run) parts.push("dry run: simulated " + applied + " of " + requested);
      else if (!dispatch.ok) parts.push("dispatch failed (" + reasonText(dispatch.code) + ")");
      else if (dispatch.code === "commanded") parts.push("commanded " + applied + " of " + requested + " requested; the device has not confirmed it yet");
      else if (dispatch.clamped) parts.push("applied " + applied + " of " + requested + " requested (clamped: " + reasonText(dispatch.code) + ")");
      else parts.push("applied " + applied + " of " + requested + " requested");
      if (who && dispatch.code !== "mode_not_auto") parts.push(who);
      parts.push("last attempt " + timeAgo(dispatch.ts));
      if (win.dispatchProblems && win.dispatchSlots > 1) parts.push(win.dispatchProblems + " of " + win.dispatchSlots + " slots clamped or failed");
    }
    if (win.actual) {
      var measuredKw = win.actualMinutes > 0
        ? (win.measured_charged_kwh - win.measured_discharged_kwh) / (win.actualMinutes / 60)
        : null;
      parts.push("measured " + fmtKw(measuredKw) + " vs " + fmtKw(isNum(win.power_w) ? win.power_w / 1000 : null) + " planned, " +
        win.actualSamples + (win.actualSamples === 1 ? " sample" : " samples") + " over " +
        Math.round(win.actualMinutes) + " of " + win.minutes + " min");
    }
    if (!parts.length) {
      if (isNum(ctx.nowMs) && isNum(win.startMs) && win.startMs > ctx.nowMs) parts.push("not reached yet, starts in " + fmtDuration((win.startMs - ctx.nowMs) / 1000));
      else if (ctx.mode && ctx.mode !== "auto") parts.push("nothing was dispatched: the mode is " + ctx.mode);
      else parts.push("no dispatch recorded for this slot");
    }
    return parts.join(" · ");
  }

  // The provenance of the price behind the window: which provider, how old, and where the window
  // sits in the horizon's price order.
  function windowPriceText(win, ctx) {
    ctx = ctx || {};
    var parts = [];
    if (win.stale) parts.push("no fresh price for this slot: the optimiser treats it as flat, and it is never dispatched");
    else parts.push("price from " + (win.first && win.first.price_source ? win.first.price_source : "unknown source"));
    var provider = ctx.priceProvider;
    if (provider) {
      parts.push(provider.stale ? "provider stale" : "updated " + timeAgo(provider.last_success));
    }
    var rank = priceRank(ctx.buyValues, win.buy);
    if (rank) parts.push("rank " + rank.rank + " of " + rank.total + " by buy price" + (rank.rank === 1 ? " (most expensive)" : ""));
    return parts.join(" · ");
  }

  // The horizon's own answer above the window list: what the windows do in total, and how much of
  // it has already been measured (never a plan figure dressed up as a measurement).
  function windowSummary(windows, ctx) {
    ctx = ctx || {};
    var charged = 0;
    var discharged = 0;
    var cost = 0;
    windows.forEach(function (win) {
      charged += win.charged_kwh;
      discharged += win.discharged_kwh;
      cost += win.cost_eur;
    });
    var parts = [
      windows.length + (windows.length === 1 ? " window" : " windows"),
      fmtKwh(discharged) + " out",
      fmtKwh(charged) + " in",
      cost < 0 ? "earns " + fmtEur(Math.abs(cost)) + " on the grid" : "costs " + fmtEur(cost) + " on the grid"
    ];
    var progress = ctx.progress;
    if (progress && isNum(progress.measured_discharged_kwh)) {
      parts.push("measured so far " + fmtKwh(progress.measured_discharged_kwh) + " out, " +
        fmtKwh(progress.measured_charged_kwh) + " in over " + progress.slots_measured + " of " +
        progress.slots_total + " slots (" + fmtPct(progress.coverage_pct, 0) + " of the reached grid observed)");
    } else if (progress) {
      parts.push("nothing measured yet over the " + progress.slots_elapsed + " fully elapsed slots");
    }
    return parts.join(" · ");
  }

  // Phone-first action windows: one card per window with an action tile, the times and the power, a
  // charge graphic for the SoC path it moves through, then the reason and the figures behind the
  // decision -- so a 360 px screen never splits the time/action/reason relationship (UI_REVIEW §3.4,
  // SPEC 10 plan execution).
  function renderPlanWindows(body, plan, execution, ctx) {
    var windows = planActionWindows(plan, execution);
    if (!windows.length) {
      body.appendChild(el("p", { class: "muted", text: "No plan slots yet. Recompute the plan." }));
      return;
    }
    ctx = ctx || {};
    ctx.minutes = planSlotMinutes(plan);
    ctx.buyValues = ctx.buyValues || (plan.slots || []).map(function (slot) { return slot.buy; });
    ctx.nowMs = Date.now();
    ctx.horizon = ctx.horizon || planHorizonBounds(plan.slots, ctx.minutes);
    body.appendChild(windowsSummary(windows, ctx));
    var ribbon = windowsRibbon(windows, ctx);
    if (ribbon) body.appendChild(ribbon);
    var list = el("ul", { class: "window-list" });
    windows.forEach(function (win) { list.appendChild(windowCard(win, ctx)); });
    body.appendChild(list);
    body.appendChild(el("p", { class: "legend-note", text: "Windows merge only while the action, reason, power and staleness stay the same, so every change is visible; each slot's full figures are in All slots. A stale slot has no fresh price, so the dispatcher refuses it rather than acting on a flat price." }));
  }

  // The panel's headline: the icon states that this is the horizon's own answer, and the sentence
  // keeps stating the totals in words (never a plan figure dressed up as a measurement).
  function windowsSummary(windows, ctx) {
    return el("div", { class: "window-summary-block" }, [
      el("span", { class: "window-summary-icon" }, [icon("list")]),
      el("p", { class: "window-summary", text: windowSummary(windows, ctx) })
    ]);
  }

  // The horizon at a glance: one segment per window placed by time, with the day ticks and the now
  // marker, then a legend naming every action that occurs. It is the panel's graphic and the key to
  // the row tints below it; the rows themselves still state every figure in text.
  function windowsRibbon(windows, ctx) {
    var bounds = ctx.horizon;
    if (!bounds) return null;
    var track = el("div", { class: "window-ribbon-track", role: "img", "aria-label": windowRibbonLabel(windows, ctx) });
    windows.forEach(function (win) {
      var rail = windowRail(win, bounds);
      if (!rail) return;
      var seg = el("span", {
        class: "window-ribbon-seg " + actionClass(win.action) + (win.stale ? " is-stale" : ""),
        title: windowRibbonSegment(win)
      });
      seg.style.left = rail.left.toFixed(2) + "%";
      seg.style.width = rail.width.toFixed(2) + "%";
      track.appendChild(seg);
    });
    horizonDayTicks(bounds).forEach(function (tick) {
      var mark = el("span", { class: "window-ribbon-day" });
      mark.style.left = tick.pct.toFixed(2) + "%";
      track.appendChild(mark);
    });
    var nowPct = horizonPct(ctx.nowMs, bounds);
    if (nowPct !== null) {
      var nowMark = el("span", { class: "window-ribbon-now" });
      nowMark.style.left = nowPct.toFixed(2) + "%";
      track.appendChild(nowMark);
    }
    var seen = {};
    var legend = el("div", { class: "window-ribbon-legend" });
    windows.forEach(function (win) {
      var key = win.action || "unknown";
      if (seen[key]) return;
      seen[key] = true;
      legend.appendChild(el("span", { class: "window-legend-item " + actionClass(key) }, [
        el("span", { class: "window-legend-swatch" }, [icon(actionIcon(key), "icon-xs")]),
        el("span", { text: actionText(key) })
      ]));
    });
    return el("div", { class: "window-ribbon" }, [
      track,
      el("p", { class: "window-ribbon-axis" }, [
        el("span", { text: fmtTime(new Date(bounds.startMs).toISOString()) }),
        el("span", { text: fmtTime(new Date(bounds.endMs).toISOString()) })
      ]),
      legend
    ]);
  }

  // What the ribbon segment says when a pointer rests on it, and what a screen reader hears instead
  // of the graphic: the same words the rows carry.
  function windowRibbonSegment(win) {
    var power = isNum(win.power_w) ? fmtPower(Math.abs(win.power_w)) : "power unknown";
    var when = isNum(win.startMs) && isNum(win.endMs)
      ? fmtTime(win.start) + " – " + fmtTime(new Date(win.endMs).toISOString())
      : fmtTime(win.start);
    return when + ": " + actionText(win.action) + " " + power + (win.stale ? " (stale price)" : "");
  }

  function windowRibbonLabel(windows, ctx) {
    var parts = windows.map(function (win) {
      var power = isNum(win.power_w) ? " " + fmtPower(Math.abs(win.power_w)) : "";
      return fmtTime(win.start) + " " + actionText(win.action).toLowerCase() + power;
    });
    return "The plan's next actions across its horizon: " + parts.join("; ") + ".";
  }

  // One window as a card: which action, when, how hard, what it does to the charge, why, and what it
  // is worth. The action tile and the charge track are the graphics; every figure they show is also
  // written out beside them.
  function windowCard(win, ctx) {
    var current = isNum(ctx.nowMs) && isNum(win.startMs) && isNum(win.endMs) &&
      ctx.nowMs >= win.startMs && ctx.nowMs < win.endMs;
    var power = isNum(win.power_w) ? fmtPower(Math.abs(win.power_w)) : "power unknown";
    var when = isNum(win.startMs) && isNum(win.endMs) && win.endMs > win.startMs
      ? fmtTime(win.start) + " – " + fmtTime(new Date(win.endMs).toISOString())
      : fmtTime(win.start);
    var track = socTrack(win, ctx.floorPct);
    return el("li", { class: "window " + actionClass(win.action) + (current ? " is-current" : ""), dataset: { stale: win.stale ? "true" : "false" } }, [
      el("div", { class: "window-head" }, [
        el("span", { class: "window-icon" }, [icon(actionIcon(win.action))]),
        el("div", { class: "window-headline" }, [
          el("p", { class: "window-action" }, [
            el("span", { class: "window-action-text", text: actionText(win.action) }),
            el("span", { class: "window-power num", text: power }),
            current ? el("span", { class: "badge badge-info" }, [el("span", { class: "now-dot" }), "now"]) : null
          ]),
          el("p", { class: "window-time" }, [
            when,
            el("span", { class: "window-slot-count", text: win.slots + (win.slots === 1 ? " slot" : " slots") })
          ])
        ])
      ]),
      track ? windowSocGraphic(track, win, ctx.floorPct) : null,
      el("p", { class: "window-reason" }, ["because: ", reasonText(win.reason)]),
      el("ul", { class: "stat-row window-facts" }, windowFacts(win, ctx).map(function (fact) {
        return el("li", null, [
          el("span", { class: "fact-icon" }, [icon(factIcon(fact.label), "icon-xs")]),
          el("span", { class: "stat-label", text: fact.label }),
          el("span", { class: "stat-value", text: fact.value })
        ]);
      })),
      el("p", { class: "window-context" }, [
        "PV " + fmtKw(win.pv_kw) + " · load " + fmtKw(win.load_kw) + (isNum(win.bandLow) && isNum(win.bandHigh)
          ? " (" + fmtKw(win.bandLow) + "–" + fmtKw(win.bandHigh) + " band)"
          : "")
      ]),
      el("p", { class: "window-status" }, [windowStatusText(win, ctx)]),
      el("p", { class: "window-price" }, [windowPriceText(win, ctx)]),
      win.stale ? el("p", { class: "window-flag" }, [badge("stale price", "stale")]) : null
    ]);
  }

  // The charge graphic: the track is the whole battery, the fill is the level this window ends on, the
  // lighter tick is where it started and the bright one is the floor the plan defends. The sentence
  // below repeats all three in numbers, because a bar alone is not readable out loud.
  function windowSocGraphic(track, win, floorPct) {
    var bar = el("span", { class: "window-soc-track" });
    var fill = el("span", { class: "window-soc-fill" + (track.rising ? " is-rising" : " is-falling") });
    fill.style.width = track.fill.toFixed(1) + "%";
    bar.appendChild(fill);
    if (track.moved) {
      var from = el("span", { class: "window-soc-from" });
      from.style.left = track.from.toFixed(1) + "%";
      bar.appendChild(from);
    }
    if (track.floor !== null) {
      var floor = el("span", { class: "window-soc-floor" });
      floor.style.left = track.floor.toFixed(1) + "%";
      bar.appendChild(floor);
    }
    return el("div", { class: "window-soc" }, [
      bar,
      el("p", { class: "window-soc-label" }, [
        "SoC " + fmtPct(win.socStart, 0) + " → " + fmtPct(win.socEnd, 0),
        track.floor !== null ? " · floor " + fmtPct(isNum(floorPct) ? floorPct : null, 0) : ""
      ])
    ]);
  }

  // Everything the window rows state about a time slot, gathered once: the efficiency and wear the
  // optimiser assumed, the SoC floor a reserve hold defends, the price provider's freshness and the
  // plan-so-far progress. Missing pieces stay null so a fact is omitted, never invented.
  function planWindowContext(data) {
    var plan = (data && data.plan) || {};
    var summary = plan.summary || {};
    var execution = (data && data.execution) || {};
    var providers = ((data && data.providers) || {}).price || [];
    var provider = null;
    providers.forEach(function (item) { if (item && item.active) provider = item; });
    var floors = execution.floors || {};
    return {
      efficiency: planEfficiency(summary),
      wear: isNum(summary.wear_eur_per_kwh) ? summary.wear_eur_per_kwh : (isNum(data && data.cycleCost) ? data.cycleCost : 0),
      floorPct: isNum(floors.floor_soc_pct) ? floors.floor_soc_pct : null,
      // Every buy price in the horizon, so a window can say where it ranks (SPEC 4.13).
      buyValues: (plan.slots || []).map(function (slot) { return slot.buy; }),
      mode: plan.mode,
      dry_run: plan.dry_run,
      priceProvider: provider,
      progress: execution.progress || null
    };
  }

  function renderPlanSlots(body, plan) {
    var slots = Array.isArray(plan.slots) ? plan.slots : [];
    var minutes = planSlotMinutes(plan);
    var headers = ["Time", "Buy", "Sell", "PV", "Load", "SoC", "Battery power", "Action", "Reason", "Flags"];
    function rowsFor(list) {
      return list.map(function (slot) {
        return [
          fmtDateTime(slot.t), fmtPrice(slot.buy), fmtPrice(slot.sell), fmtKw(slot.pv_kw, 2), fmtKw(slot.load_kw, 2),
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
      body.appendChild(table("Plan slots, " + minutes + "-minute resolution, times in local timezone", headers, rowsFor(list), { rowAttrs: attrsFor(list) }));
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
    var cached = null;
    function fetchAll() {
      // The execution join and the provider freshness are supporting evidence: if either is
      // unavailable the plan still renders and the rows simply state less.
      return Promise.all([
        get("/plan", { hours: hours }),
        get("/config"),
        get("/plan/execution", { hours: hours }).catch(function () { return null; }),
        get("/providers").catch(function () { return null; })
      ]).then(function (rows) {
        return {
          plan: rows[0],
          config: rows[1],
          execution: rows[2],
          providers: rows[3],
          cycleCost: cycleCostFromConfig(rows[1])
        };
      });
    }
    function load() {
      if (!cached) cached = fetchAll();
      return cached;
    }
    var pollTimer = null;
    var panels = [];
    function addPanel(title, render) {
      panels.push(asyncPanel(host, title, { load: load, render: render }));
    }

    routeTitle(host, "Plan", "What the optimiser intends to do with the batteries, and why.");
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

    addPanel("Plan warnings", function (body, data) {
        var plan = data.plan;
        var info = planFlags(plan);
        if (!info.flags.length) body.appendChild(el("p", { class: "muted", text: "No feasibility warnings. " + (info.sources.length ? "Sources — " + info.sources.join(", ") + "." : "") }));
        info.flags.forEach(function (flag) {
          body.appendChild(el("div", { class: "banner banner-" + (flag.level === "error" ? "error" : "warn") }, [
            el("p", { class: "banner-title", text: flag.title }), el("p", { text: flag.text })
          ]));
        });
        body.appendChild(el("p", { class: "legend-note", text: "Plan generated " + timeAgo(plan.generated_at) + " for " + fmtDateTime(plan.horizon_start) + " – " + fmtDateTime(plan.horizon_end) + "." }));
    });

    addPanel("Timeline", function (body, data) {
        var plan = data.plan;
        var chart = el("div", { class: "chart chart-md" });
        body.appendChild(chart);
        var efficiency = planEfficiency(plan.summary);
        var planWear = isNum(plan.summary && plan.summary.wear_eur_per_kwh)
          ? plan.summary.wear_eur_per_kwh
          : (isNum(data.cycleCost) ? data.cycleCost : 0);
        root.SEMCharts.plan(chart, plan, { wear: planWear, efficiency: efficiency });
        body.appendChild(el("p", { class: "legend-note", text: efficiency
          ? "A slot whose sale price sits below the dotted break-even line would lose money, which is why it stays idle with reason “efficiency_loss”; the exact figures are in All slots."
          : "The break-even after round-trip loss line is unavailable for this plan: the summary carries no aggregate efficiency." }));
    });

    addPanel("Plan summary", renderPlanSummary);
    addPanel("Next actions", function (body, data) {
      renderPlanWindows(body, data.plan, data.execution, planWindowContext(data));
    });
    addPanel("All slots", function (body, data) { renderPlanSlots(body, data.plan); });

    // Returning to the tab re-reads the plan, its execution join and the providers in place: the
    // "now" marker, the measured figures and the price age must not sit on a screen opened hours
    // ago, and a background refresh must never scroll or rebuild the route (UI_REVIEW §3.5).
    state.refresh = function () {
      if (refreshBlocked()) return;
      cached = null;
      panels.forEach(function (p) { p.reload(); });
    };
    return function () {
      if (pollTimer) { root.clearInterval(pollTimer); pollTimer = null; }
      if (state.refresh) state.refresh = null;
    };
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

  // Measured error of one forecast kind, or null when there is not enough measured data. MAE
  // carries the kind's own unit — the contract does not name one (UI_REVIEW §3.6): pv/load are kW,
  // price is EUR/kWh, so guessing across kinds is never done.
  function forecastAccuracyLine(report) {
    if (!report) return "not measured yet";
    if (!(report.samples > 0)) return "not enough measured days yet";
    var parts = ["MAPE " + (isNum(report.mape_pct) ? fmtPct(report.mape_pct) : "--") + " over " + (report.days || "?") + " days"];
    if (isNum(report.bias_pct)) parts.push("bias " + fmtPct(report.bias_pct));
    if (isNum(report.mae)) parts.push("MAE " + report.mae.toFixed(2) + " " + (report.kind === "price" ? "EUR/kWh" : "kW"));
    parts.push(report.samples + " samples");
    return parts.join(" · ");
  }

  function renderForecastPanel(body, kind, payload, accuracy) {
    var slots = Array.isArray(payload.slots) ? payload.slots : [];
    // Source metadata is one quiet line; only a limitation (stale) keeps a badge, so ordinary
    // provider names do not read as actionable state (UI_REVIEW §3.12).
    var meta = el("p", { class: "alert-meta" }, [
      "Source: " + (payload.source || "unknown"),
      // Effective provider + configured contract (SPEC §4.1), next to the resolved source so the
      // empty-state hint below can be read against what was actually configured.
      payload.provider ? " · provider: " + payload.provider : null,
      payload.contract ? " · contract: " + payload.contract : null,
      " · generated " + timeAgo(payload.generated_at),
      payload.stale ? " " : null,
      payload.stale ? badge("stale", "stale") : null,
      kind === "load" && payload.confidence ? " · confidence: " + payload.confidence : null,
      kind === "solar" && isNum(payload.correction_applied) ? " · bias correction ×" + payload.correction_applied.toFixed(2) : null
    ]);
    body.appendChild(meta);
    body.appendChild(el("p", { class: "trust-line" }, ["Measured error: ", forecastAccuracyLine(accuracy)]));
    // A material limitation stays visible; ordinary model explanation moves behind the chart's
    // Source details disclosure (UI_REVIEW §3.7).
    if (payload.stale) {
      body.appendChild(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Using cached data" }),
        el("p", { text: "This forecast is stale: values are re-used and greyed in the chart." })
      ]));
    }
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
      body.appendChild(forecastNotes(payload));
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
        // Missing values are never summed as zero: the total states how many slots it covers
        // (UI_REVIEW §3.6).
        var have = 0, missing = 0, total = 0;
        var peak = null;
        group.slots.forEach(function (s) {
          var value = numOrNull(s.pv_kw);
          if (value == null) { missing++; return; }
          have++;
          total += Math.max(0, value);
          if (!peak || value > peak.value) peak = { value: value, t: s.t };
        });
        return [group.key,
          have ? fmtKwh(total * step) + (missing ? " (over " + have + " of " + group.slots.length + " slots)" : "") : "--",
          peak ? fmtPower(peak.value * 1000) : "--",
          peak ? fmtTime(peak.t) : "--"];
      });
    } else {
      headers = ["Day", "P50 energy", "P10 energy", "P90 energy"];
      rows = groups.map(function (group) {
        function totalFor(pick) {
          var have = 0, missing = 0, sum = 0;
          group.slots.forEach(function (s) {
            var value = numOrNull(pick(s));
            if (value == null) { missing++; return; }
            have++;
            sum += value;
          });
          return have ? fmtKwh(sum * step) + (missing ? " (over " + have + " of " + group.slots.length + " slots)" : "") : "--";
        }
        return [group.key,
          totalFor(function (s) { return s.load_kw; }),
          totalFor(function (s) { return s.p10_kw; }),
          totalFor(function (s) { return s.p90_kw; })];
      });
    }
    body.appendChild(table("Per-day totals (times and days in local timezone)", headers, rows));
    body.appendChild(forecastNotes(payload));
  }

  function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }

  // Ordinary model explanation and geometry notes are evidence: reachable, but not the first
  // reading. The server sends an untyped string array, so nothing here is classified as
  // non-material by the UI (UI_REVIEW §3.7 / open question 8).
  function forecastNotes(payload) {
    var notes = Array.isArray(payload.notes) ? payload.notes.filter(function (note) { return !!note; }) : [];
    if (!notes.length) return null;
    var details = el("details", { class: "disclosure" }, [el("summary", { text: "Source details (" + notes.length + " note" + (notes.length === 1 ? "" : "s") + ")" })]);
    var noteBody = el("div", { class: "disclosure-body" });
    notes.forEach(function (note) { noteBody.appendChild(el("p", { class: "field-help", text: String(note) })); });
    details.appendChild(noteBody);
    return details;
  }

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
      // MAE carries the kind's own unit (UI_REVIEW §3.6): power forecasts in kW, price in EUR/kWh.
      var maeUnit = pair[0] === "price" ? "EUR/kWh" : "kW";
      var sub = [
        "MAPE over " + (report.days || "?") + " days",
        isNum(report.bias_pct) ? "bias " + fmtPct(report.bias_pct) : null,
        isNum(report.mae) ? "MAE " + report.mae.toFixed(3) + " " + maeUnit : null,
        (report.samples || 0) + " samples"
      ].filter(Boolean).join(" · ");
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
    body.appendChild(el("p", { class: "legend-note", text: "Lower is better; every value is also in the per-kind cards above." }));
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
    routeTitle(host, "Forecasts", "How much to trust each source, then what it expects.");
    asyncPanel(host, "Price forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "price", data.price, data.accuracy.price); } });
    asyncPanel(host, "Solar forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "solar", data.solar, data.accuracy.pv); } });
    asyncPanel(host, "Load forecast", { load: load, render: function (body, data) { renderForecastPanel(body, "load", data.load, data.accuracy.load); } });
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
        body.appendChild(table("Forecast providers", ["Kind", "Provider", "Active", "Healthy", "Freshness", "Last success", "Last error"], rows, { numeric: [] }));
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
      ["capacity_entity", "Capacity sensor"],
      ["cell_voltage_entity", "Lowest cell voltage sensor (per cell, V)"]
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
      ["grid_input_power_entity", "Grid input power sensor"],
      ["cell_voltage_entity", "Lowest cell voltage sensor (per cell, V)"],
      ["smart_mode_entity", "Device smart mode switch"],
      ["ac_mode_entity", "AC mode entity"]
    ],
    // A Zendure device bridged by Home Assistant's own MQTT integration publishes one entity per
    // zenSDK property (`<serial>_inputlimit`, `<serial>_acmode`, ...): there is no manager to pick
    // and no control path, the device's own AC limits *are* the control path.
    zendure_mqtt: [
      ["ac_mode_entity", "AC mode select (Input/Output mode)"],
      ["charge_limit_entity", "AC charge limit (inputLimit)"],
      ["discharge_limit_entity", "AC output limit (outputLimit)"],
      ["soc_entity", "State of charge sensor (electricLevel)"],
      ["soc_target_entity", "SoC target (socSet)"],
      ["min_soc_entity", "Minimum SoC (minSoc)"],
      ["output_power_entity", "Output power sensor (outputHomePower)"],
      ["grid_input_power_entity", "Grid input power sensor (gridInputPower)"],
      ["pv_power_entity", "PV input power sensor (solarInputPower)"],
      ["cell_voltage_entity", "Lowest cell voltage sensor (per cell, V)"],
      ["smart_mode_entity", "Device smart mode (written with every command)"],
      ["operation_entity", "Zendure manager operation select (kept off, optional)"]
    ],
    // The device's own local zenSDK HTTP API: no HA entities at all, so the "roles" are the
    // connection settings the driver posts to and reads the device identity from.
    zendure_native: []
  };
  var NON_ENTITY_ROLES = {
    mode_manual_value: "Manual mode value (e.g. manual)",
    operation_off_value: "Manager off value (e.g. off)"
  };
  // Driver ids as the dropdown shows them: the id is what the server validates, the label says
  // where the entities come from.
  var BATTERY_DRIVER_LABELS = {
    generic: "generic (a setpoint or charge/discharge pair)",
    zendure: "zendure (the Zendure-HA integration)",
    zendure_mqtt: "zendure_mqtt (a device bridged by MQTT)",
    zendure_native: "zendure_native (the device's own zenSDK HTTP API, no integration)"
  };

  // The native driver's mapping keys are connection settings; they mirror
  // control/drivers/native.py so a typed value and a validated value can never disagree.
  var NATIVE_CONNECTION_FIELDS = [
    ["host", "Device IP address or mDNS name", "the device's own address on the LAN"],
    ["serial", "Device serial number (sn)", "the serial the device reports"],
    ["port", "HTTP port (empty = 80 for http, 443 for https)", "80"],
    ["scheme", "Scheme (http or https)", "http"],
    ["api_path", "API base path", "/properties"],
    ["token", "API token (optional)", "empty = no Authorization header"]
  ];

  /** Connection problems of a zendure_native mapping (mirrors control/drivers/native.py). */
  function batteryNativeConnectionProblems(mapping) {
    mapping = mapping || {};
    var problems = [];
    var host = String(mapping.host || "").trim();
    if (!host) problems.push("A native Zendure battery needs the device's IP address or hostname.");
    else if (/:\/\//.test(host) || host.indexOf("/") >= 0 || /\s/.test(host)) {
      problems.push("The device host must be a bare IP address or hostname (no scheme, port or path).");
    }
    var serial = String(mapping.serial || "").trim();
    if (!serial) problems.push("A native Zendure battery needs the device serial number (sn).");
    else if (/\s/.test(serial)) problems.push("The device serial must not contain spaces.");
    var scheme = String(mapping.scheme || "http").trim().toLowerCase();
    if (scheme !== "http" && scheme !== "https") problems.push("The scheme must be http or https.");
    var port = String(mapping.port || "").trim();
    if (port) {
      var portValue = numeric(port);
      if (portValue == null || portValue < 1 || portValue > 65535) problems.push("The HTTP port must be a whole number between 1 and 65535.");
    }
    var apiPath = String(mapping.api_path || "/properties").trim();
    if (apiPath.charAt(0) !== "/" || /\s/.test(apiPath)) problems.push("The API base path must start with / and contain no spaces.");
    var timeout = String(mapping.timeout_s || "").trim();
    if (timeout) {
      var timeoutValue = numeric(timeout);
      if (timeoutValue == null || timeoutValue <= 0 || timeoutValue > 60) problems.push("The HTTP timeout must be between 0 and 60 seconds.");
    }
    return problems;
  }

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

  /* A Zendure device bridged by Home Assistant's MQTT integration (local MQTT feature) publishes
   * its own zenSDK properties, one entity per property, named `<serial>_<property>` in camelCase
   * (`number.<serial>_inputlimit`, `select.<serial>_acmode`, `sensor.<serial>_electriclevel`).
   * There is nothing to score: the device is identified by its platform plus manufacturer/model,
   * and each row's role follows from the property name it carries. The suffix table below is the
   * same vocabulary the server driver resolves (`control/drivers/zendure.py`), so a pick here and a
   * runtime resolve agree. */
  var MQTT_ZENDURE_DRIVER = "zendure_mqtt";
  // The native driver talks to the inverter's own local zenSDK HTTP API.  No entity registry row
  // can identify it (there is no integration), so it is only ever chosen manually in the wizard.
  var NATIVE_ZENDURE_DRIVER = "zendure_native";
  // [entity-id suffix, role slug, mapping key]
  var MQTT_ZENDURE_PROPERTIES = [
    ["inputlimit", "charge_limit", "charge_limit_entity"],
    ["outputlimit", "discharge_limit", "discharge_limit_entity"],
    ["acmode", "manual_mode", "ac_mode_entity"],
    ["electriclevel", "soc", "soc_entity"],
    ["outputhomepower", "output_power", "output_power_entity"],
    ["gridinputpower", "grid_input_power", "grid_input_power_entity"],
    ["solarinputpower", "pv_power", "pv_power_entity"],
    ["socset", "soc_target", "soc_target_entity"],
    ["minsoc", "min_soc", "min_soc_entity"],
    ["smartmode", "smart_mode", "smart_mode_entity"]
  ];
  // `mqtt` alone is every device on the broker, so the *device* has to look like a Zendure
  // inverter: manufacturer or model text (case-insensitive).
  var MQTT_ZENDURE_IDENTITY = /zendure|solarflow|hyper|superbase|aio/i;

  /** True for a group that is a Zendure inverter seen through the MQTT bridge. */
  function batteryIsZendureMqtt(group) {
    var platforms = (group && group.platforms) || [];
    if (platforms.indexOf("mqtt") < 0) return false;
    var text = [group.manufacturer, group.model, group.deviceName]
      .filter(function (part) { return !!part; })
      .join(" ");
    return MQTT_ZENDURE_IDENTITY.test(text);
  }

  /** The zenSDK property suffix of an MQTT bridge entity id (`..._inputlimit` -> `inputlimit`). */
  function mqttPropertySuffix(entityId) {
    var objectId = String(entityId || "").toLowerCase().split(".").pop();
    var found = "";
    MQTT_ZENDURE_PROPERTIES.forEach(function (entry) {
      if (found) return;
      var suffix = "_" + entry[0];
      if (objectId.slice(-suffix.length) === suffix) found = entry[0];
    });
    return found;
  }

  /** Deterministic roles, mapping keys and numbers of one MQTT bridge device.
   *  No confidence is involved: the property name says which role an entity carries, and only the
   *  first row per property is taken (so a pack's `socLevel` can never displace `electricLevel`). */
  function mqttZendureGroup(group) {
    var roles = {};
    var mapping = {};
    var keyRoles = {};
    var skipped = [];
    var rows = ((group && group.rows) || []).slice().sort(function (a, b) {
      return String(a.entity_id || "") < String(b.entity_id || "") ? -1 : 1;
    });
    rows.forEach(function (row) {
      var suffix = mqttPropertySuffix(row.entity_id);
      if (!suffix) return;
      var match = null;
      MQTT_ZENDURE_PROPERTIES.forEach(function (entry) {
        if (!match && entry[0] === suffix) match = entry;
      });
      if (!match) return;
      var role = match[1];
      if (roles[role]) return;
      if (BATTERY_WRITABLE_ROLES.indexOf(role) >= 0 && !batteryIsWritable(row)) {
        if (skipped.indexOf(role) < 0) skipped.push(role);
        return;
      }
      roles[role] = row;
      mapping[match[2]] = String(row.entity_id || "");
      keyRoles[match[2]] = role;
    });
    var missing = [];
    ["charge_limit", "discharge_limit", "manual_mode", "soc"].forEach(function (role) {
      if (!roles[role]) missing.push(role);
    });
    return { roles: roles, mapping: mapping, keyRoles: keyRoles, skipped: skipped, missing: missing };
  }

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
    if (batteryIsZendureMqtt(group)) {
      var found = mqttZendureGroup(group);
      return { chosen: found.roles, skipped: found.skipped };
    }
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
    if (batteryIsZendureMqtt(group)) {
      // Identity comes from the device, roles from the property names: a bridge group is a picker
      // row on its own terms, never on a confidence score, and a device without a capacity sensor
      // is not "partial" -- capacity_kwh is a form field.
      var mqtt = mqttZendureGroup(group);
      var mqttRoles = Object.keys(mqtt.roles).sort();
      return {
        roles: mqtt.roles,
        mappable: mqttRoles,
        skipped: mqtt.skipped,
        writableControl: mqttRoles.indexOf("charge_limit") >= 0 && mqttRoles.indexOf("discharge_limit") >= 0,
        readOnlyControl: false,
        controlReady: mqtt.missing.length === 0,
        missing: mqtt.missing,
        viaPlatform: true,
        confidence: mqttRoles.length
      };
    }
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
    if (driver === MQTT_ZENDURE_DRIVER) {
      var mqtt = mqttZendureGroup(group);
      return { mapping: mqtt.mapping, roles: mqtt.roles, keyRoles: mqtt.keyRoles };
    }
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
    if (batteryIsZendureMqtt(group)) return MQTT_ZENDURE_DRIVER;
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

  /** The wear constant a battery is priced with, EUR per kWh delivered (SPEC §5).
   *  The advanced override wins when it is a positive number; else the server-derived value. */
  function batteryWear(config, override) {
    if (isNum(override) && override > 0) return override;
    var derived = numeric((config || {}).wear_eur_per_kwh);
    return derived != null && derived >= 0 ? derived : 0;
  }

  /** All-in cost of one kWh put into the pack: buy / eta_charge + wear x eta_discharge. */
  function storedKwhCostEur(buy, config, wear) {
    var eff = batteryEfficiency(config);
    if (!eff || !isNum(buy)) return null;
    return buy / eff.eta_charge + (isNum(wear) ? wear : 0) * eff.eta_discharge;
  }

  /** Lowest sale price that covers buying at `buy`: buy / round_trip + wear (SPEC §5). */
  function breakEvenSellEur(buy, config, wear) {
    var eff = batteryEfficiency(config);
    if (!eff || !isNum(buy)) return null;
    return buy / eff.round_trip + (isNum(wear) ? wear : 0);
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

  function planBreakEvenEur(buy, efficiency, wear) {
    if (!isNum(buy) || !efficiency) return null;
    return buy / efficiency.round_trip + (isNum(wear) ? wear : 0);
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

  /** Derived wear of a battery in the dialog: price / (cycle life x usable kWh x eta_discharge),
   *  EUR per AC kWh delivered, or null. A full cycle therefore costs exactly price / cycle life. */
  function derivedWearEur(config) {
    var price = numeric((config || {}).price_eur);
    var life = numeric((config || {}).cycle_life);
    var capacity = numeric((config || {}).capacity_kwh);
    var minSoc = numeric((config || {}).min_soc_pct);
    var maxSoc = numeric((config || {}).max_soc_pct);
    if (capacity == null) capacity = 0;
    if (minSoc == null) minSoc = 0;
    if (maxSoc == null) maxSoc = 100;
    var usable = capacity * (maxSoc - minSoc) / 100;
    var eff = batteryEfficiency(config || {});
    var etaDischarge = eff && eff.eta_discharge > 0 ? eff.eta_discharge : DEFAULT_ETA_DISCHARGE;
    if (price == null || life == null || price <= 0 || life <= 0 || usable <= 0) return null;
    return price / (life * usable * etaDischarge);
  }

  function batteryCycleCount(state) {
    if (state && state.extra && isNum(state.extra.cycles)) return state.extra.cycles;
    return null;
  }

  /** SPEC 5 learned capacity: what the card may state, and never a fabricated estimate. */
  function capacitySummary(record) {
    var config = (record && record.config) || {};
    var info = (record && record.capacity) || {};
    var source = String(config.capacity_source || info.source || "manual");
    function pick(infoKey, recordKey) {
      if (isNum(info[infoKey])) return info[infoKey];
      if (record && isNum(record[recordKey])) return record[recordKey];
      return null;
    }
    var effective = pick("effective_kwh", "effective_capacity_kwh");
    if (effective == null && isNum(config.capacity_kwh)) effective = config.capacity_kwh;
    var samples = pick("samples", "capacity_samples");
    return {
      source: source,
      effective: effective,
      configured: isNum(config.capacity_kwh) ? config.capacity_kwh : null,
      learned: pick("learned_kwh", "capacity_learned_kwh"),
      samples: samples == null ? 0 : samples,
      updated: info.updated_at || (record && record.capacity_updated_at) || null,
      reason: String((record && record.capacity_reason) || info.reason || ""),
      stale: !!info.stale,
      applied: source !== "learned" || !!info.applied
    };
  }

  /** One truthful sentence about where a battery's capacity comes from (SPEC 5). */
  function capacityLearningText(cap) {
    if (cap.source !== "learned") {
      return "Capacity source: manual — the configured " + fmtKwh(cap.configured) + " kWh is used everywhere.";
    }
    if (cap.learned == null) {
      return "Learned capacity: no published estimate yet (" + cap.samples + " of 3 accepted windows). The configured " + fmtKwh(cap.configured) + " kWh seed is used meanwhile.";
    }
    if (!cap.applied) {
      return "Learned capacity ignored (" + (cap.reason || "rejected") + "); the configured " + fmtKwh(cap.configured) + " kWh seed is used. Estimate was " + fmtKwh(cap.learned) + " kWh from " + cap.samples + " window(s).";
    }
    return "Learned from measured cycles: " + fmtKwh(cap.learned) + " kWh from " + cap.samples + " accepted window(s), last learned " + timeAgo(cap.updated) + ". Effective capacity " + fmtKwh(cap.effective) + " kWh.";
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
      el("div", { class: "dialog-body" }, [
        el("h2", { text: "Test discharge — " + config.name }),
        el("p", { class: "muted", text: "Writes a real, bounded discharge setpoint (no dry-run suppression). Discharge power up to " + fmtPower(maxPower) + ", duration 1–60 s at the exact seconds asked for. The server clamps the request to the battery's own limits and reports what was really applied. Available only when mode is auto and dry run is off." }),
        el("div", { class: "fields" }, [
          el("label", { class: "field", for: "test-power" }, [el("span", { text: "Discharge power (W)" }), el("span", { class: "field-help", text: "1 to " + fmtPower(maxPower) }), power]),
          el("label", { class: "field", for: "test-seconds" }, [el("span", { text: "Duration (s, exact)" }), el("span", { class: "field-help", text: "1 to 60 s, no rounding up" }), seconds])
        ]),
        note
      ]),
      save,
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
    var cap = capacitySummary(record);
    var eff = batteryEfficiency(config);
    var wearOverride = isNum(cycleCost) ? cycleCost : 0;
    var wear = batteryWear(config, wearOverride);
    var buy = isNum(status.price_now) ? status.price_now : null;
    var storedCost = storedKwhCostEur(buy, config, wear);
    var breakEven = breakEvenSellEur(buy, config, wear);
    var canTest = status.mode === "auto" && !status.dry_run;
    var cellVoltage = st.extra && isNum(st.extra.cell_voltage_v) ? st.extra.cell_voltage_v : null;
    var guardRow = null;
    if (status && status.cell_voltage && Array.isArray(status.cell_voltage.batteries)) {
      status.cell_voltage.batteries.forEach(function (row) {
        if (row && row.battery_id === config.id) guardRow = row;
      });
    }
    var guardText = !status || !status.cell_voltage || !status.cell_voltage.enabled
      ? "off"
      : guardRow
        ? (guardRow.force_charge_w ? "emergency charge active" : "discharge blocked")
        : "normal";
    // The disabled prerequisite is real text next to the action, never a hover-only title
    // (UI_REVIEW §3.4).
    var testBlockedReason = "Requires Auto and dry run off — currently mode is " + status.mode +
      " and dry run is " + (status.dry_run ? "on" : "off") + ".";

    var edit = button("Edit", "", function () { openBatteryDialog(record, reload, takenIds); });
    var toggle = button(config.enabled ? "Disable" : "Enable", config.enabled ? "" : "btn-primary");
    var note = el("div", { class: "battery-note" });

    // Device actions sit in one labelled section, so tests and deletion are not equal peers of Edit
    // on every header (UI_REVIEW §3.7 / per-view notes).
    var actions = el("div", { class: "battery-actions" }, [edit, toggle, note]);

    var test = button("Test discharge", "btn-danger");
    test.disabled = !canTest;
    test.setAttribute("aria-disabled", canTest ? "false" : "true");
    if (!canTest) test.setAttribute("title", "Requires mode auto with dry run off");
    test.addEventListener("click", function () {
      if (!canTest) return;
      testDischargeDialog(record, status);
    });
    var remove = button("Delete", "btn-danger");
    remove.addEventListener("click", function () {
      confirmAction({
        title: "Delete " + config.name + "?",
        body: "Removes the battery configuration. Dispatch stops immediately; measurements keep working.",
        confirmLabel: "Delete battery", requireCheck: true,
        checkLabel: "I understand this cannot be undone"
      }).then(function (ok) {
        if (!ok) return;
        performWrite(remove, note, function () {
          return del("/batteries/" + encodeURIComponent(config.id));
        }, function () { return "Battery deleted (confirmed by the server)."; }, reload);
      });
    });

    toggle.addEventListener("click", function () {
      function apply() {
        performWrite(toggle, note, function () {
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

    // Economics and mappings are evidence behind named disclosures; unresolved mappings and stale
    // state stay in the header block (UI_REVIEW §3.7).
    var economics = el("details", { class: "disclosure" }, [
      el("summary", { text: "Efficiency and wear" }),
      el("div", { class: "disclosure-body" }, [
        eff
          ? el("p", { class: "field-help", text: "Charge " + fmtPct(eff.eta_charge * 100) + " / discharge " + fmtPct(eff.eta_discharge * 100) + (eff.asymmetric ? " — advanced per-direction override" : " — split evenly from the round trip") + "." })
          : el("p", { class: "field-error", text: "No usable efficiency on this battery: the stored-kWh and break-even maths is unavailable." }),
        el("p", { class: "field-help", text: "Stored kWh, all-in = buy ÷ charge efficiency + wear × discharge efficiency: the all-in cost of energy put into the pack. Break-even sale = the lowest sale price that covers the round trip: buy ÷ round trip + wear per delivered kWh. Wear is derived per battery from purchase price ÷ (cycle life × usable kWh × discharge efficiency), so a full cycle costs price ÷ cycle life" + (isNum(config.price_eur) && config.price_eur > 0 ? " (" + fmtEur(config.price_eur) + " ÷ (" + (isNum(config.cycle_life) ? config.cycle_life : 0) + " cycles × " + fmtKwh(config.capacity_kwh * (config.max_soc_pct - config.min_soc_pct) / 100) + " usable × " + fmtPct((batteryEfficiency(config) || { eta_discharge: DEFAULT_ETA_DISCHARGE }).eta_discharge * 100) + " discharge)" : "") + "; the advanced wear override replaces it when set." + (buy != null ? " At the current buy price " + fmtPrice(buy) + " and wear " + fmtEur(wear) + "/kWh." : " The current buy price is unknown right now, so both show --.") }),
        el("p", { class: "field-help", text: "Cost of a stored kWh: " + fmtPrice(storedCost) + " · lowest sale price that covers the round trip: " + fmtPrice(breakEven) + "." })
      ])
    ]);

    var detailsBlocks = [
      // SPEC 5: where the effective capacity comes from, with the estimate's own evidence.
      el("details", { class: "disclosure" }, [
        el("summary", { text: "Capacity learning" }),
        el("div", { class: "disclosure-body" }, [
          el("p", { class: "field-help", text: capacityLearningText(cap) }),
          el("p", { class: "field-help", text: "Configured " + fmtKwh(cap.configured) + " kWh · effective " + fmtKwh(cap.effective) + " kWh" + (cap.samples ? " · " + cap.samples + " accepted measurement window(s)" : "") + (cap.updated ? " · last learned " + timeAgo(cap.updated) : "") + ". Switch between manual and learned in Edit → Capacity source." })
        ])
      ]),
      economics,
      el("details", { class: "disclosure" }, [
        el("summary", { text: "Entity mapping" }),
        el("div", { class: "disclosure-body" }, [mappingList(config, record.mapping_resolved)])
      ]),
      Object.keys(capabilities).length ? el("details", { class: "disclosure" }, [
        el("summary", { text: "Driver details" }),
        el("div", { class: "disclosure-body" }, [
          el("p", { class: "field-help", text: "Driver capabilities: " + Object.keys(capabilities).map(function (k) { return k + "=" + String(capabilities[k]); }).join(", ") }),
          Array.isArray(record.discovery) && record.discovery.length ? el("p", { class: "field-help", text: record.discovery.length + " entity candidate(s) available for the wizard." }) : null
        ])
      ]) : null
    ];

    var card = el("section", { class: "battery-card", dataset: { battery: config.id } }, [
      el("div", { class: "battery-head" }, [
        el("h3", null, [
          config.name || config.id, " ",
          el("span", { class: "battery-driver", text: config.driver || "driver?" }), " ",
          config.enabled ? el("span", { class: "muted", text: "enabled" }) : badge("disabled", "warn"),
          " ",
          st.stale ? badge("stale state", "stale") : null,
          guardRow && guardRow.block_discharge ? badge("low cell voltage", "err") : null,
          record.maintenance && record.maintenance.overdue ? badge("full charge overdue", "warn") : null,
          st.available ? null : badge("unavailable", "err")
        ]),
        actions
      ]),
      el("p", { class: "alert-meta", text: "id: " + config.id + " · last read " + timeAgo(st.read_at) + (st.available ? "" : " · unavailable") + (st.last_error ? " · last error: " + st.last_error : "") }),
      el("ul", { class: "stat-row" }, [
        el("li", null, [el("span", { class: "stat-label", text: "Power" }), el("span", { class: "stat-value", text: (isNum(st.power_w) ? fmtPower(Math.abs(st.power_w)) : fmtPower(st.power_w)) + (isNum(st.power_w) ? (st.power_w > 0 ? " charging" : st.power_w < 0 ? " discharging" : " idle") : "") })]),
        el("li", null, [el("span", { class: "stat-label", text: "Charge level" }), el("span", { class: "stat-value", text: fmtPct(st.soc_pct) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Energy" }), el("span", { class: "stat-value", text: fmtKwh(st.energy_kwh) + " of " + fmtKwh(st.capacity_kwh || config.capacity_kwh) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Capacity" }), el("span", { class: "stat-value", text: fmtKwh(cap.effective) + (cap.source === "learned" ? " learned" : "") })]),
        el("li", null, [el("span", { class: "stat-label", text: "Cell voltage" }), el("span", { class: "stat-value", text: cellVoltage == null ? "not reported" : cellVoltage.toFixed(3) + " V" })]),
        el("li", null, [el("span", { class: "stat-label", text: "Protection" }), el("span", { class: "stat-value", text: guardText })]),
        el("li", null, [el("span", { class: "stat-label", text: "Maintenance" }), el("span", { class: "stat-value", text: maintenanceText(record.maintenance) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Round trip" }), el("span", { class: "stat-value", text: eff ? fmtPct(eff.round_trip_pct) : "--" })]),
        el("li", null, [el("span", { class: "stat-label", text: "Cycles" }), el("span", { class: "stat-value", text: cycles == null ? "not reported" : cycles.toFixed(1) + " cycles" })]),
        el("li", null, [el("span", { class: "stat-label", text: "Limits" }), el("span", { class: "stat-value", text: fmtPower(config.max_charge_w) + " charge / " + fmtPower(config.max_discharge_w) + " discharge" })]),
        el("li", null, [el("span", { class: "stat-label", text: "Dispatch" }), el("span", { class: "stat-value", text: dispatchPathText(config, capabilities) })]),
        el("li", null, [el("span", { class: "stat-label", text: "SoC window" }), el("span", { class: "stat-value", text: fmtPct(config.min_soc_pct, 0) + " to " + fmtPct(config.max_soc_pct, 0) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Wear" }), el("span", { class: "stat-value", text: fmtEur(wear) + "/kWh delivered" + (wearOverride > 0 ? " (override)" : "") })])
      ]),
      el("details", { class: "disclosure" }, [
        el("summary", { text: "Device actions (test and delete)" }),
        el("div", { class: "disclosure-body" }, [
          el("p", { class: "field-help", text: canTest ? "A test discharge writes a real, bounded setpoint (no dry-run suppression) and the server reports what was really applied." : "Test discharge is unavailable: " + testBlockedReason }),
          el("div", { class: "battery-actions" }, [test, remove])
        ])
      ])
    ].concat(detailsBlocks));
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
    routeTitle(host, "Batteries", "Fleet readiness, measured state and the mapping each device dispatches through.");
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
      driver: ["generic", "zendure", MQTT_ZENDURE_DRIVER, NATIVE_ZENDURE_DRIVER].indexOf(String(source.driver)) >= 0
        ? String(source.driver) : "generic",
      capacity_kwh: isNum(source.capacity_kwh) ? source.capacity_kwh : 5.0,
      min_soc_pct: isNum(source.min_soc_pct) ? source.min_soc_pct : 10.0,
      max_soc_pct: isNum(source.max_soc_pct) ? source.max_soc_pct : 90.0,
      max_charge_w: isNum(source.max_charge_w) ? source.max_charge_w : 800.0,
      max_discharge_w: isNum(source.max_discharge_w) ? source.max_discharge_w : 800.0,
      round_trip_efficiency_pct: sourceEfficiency ? Math.round(sourceEfficiency.round_trip_pct * 10) / 10 : 90.0,
      price_eur: isNum(source.price_eur) ? source.price_eur : 0.0,
      cycle_life: isNum(source.cycle_life) ? source.cycle_life : 0.0,
      enabled: source.enabled !== false,
      // Which write path the driver must use.  "auto" keeps the shipped behaviour (the Zendure
      // manager when its entities are mapped), so an older saved battery is unaffected.
      control_path: ["auto", "manager", "device"].indexOf(String(source.control_path)) >= 0 ? String(source.control_path) : "auto",
      // SPEC 5: manual (default) uses the typed capacity; learned uses the measured estimate and
      // keeps the typed value as the seed/fallback.
      capacity_source: String(source.capacity_source) === "learned" ? "learned" : "manual"
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
    ["generic", "zendure", MQTT_ZENDURE_DRIVER, NATIVE_ZENDURE_DRIVER].forEach(function (driver) {
      var option = el("option", { value: driver, text: BATTERY_DRIVER_LABELS[driver] || driver });
      if (driver === fields.driver) option.selected = true;
      driverSelect.appendChild(option);
    });
    inputs.driver = driverSelect;

    var controlPathSelect = el("select", { id: "battery-control-path", "aria-label": "Control path" });
    [
      ["auto", "Automatic (Zendure manager when its entities are mapped)"],
      ["manager", "Zendure manager: manual power (needs a fast P1 meter)"],
      ["device", "Inverter itself: AC mode + input/output limits"]
    ].forEach(function (option) {
      controlPathSelect.appendChild(el("option", { value: option[0], text: option[1] }));
    });
    controlPathSelect.value = fields.control_path;
    // The manager only applies its setpoint on the next P1 event and re-writes the inverter's own
    // limits on every one of those events, so the device path takes the manager out of the loop.
    var controlPathNote = el("p", { class: "field-help", text: "Manager: one system-wide setpoint, applied on the manager's next meter (P1) update — needs a fast grid feed. Inverter: the device's own AC direction and input/output limits, applied immediately, with the Zendure manager kept switched off. Both are bounded by this battery's SoC window and power limits." });
    var controlPathField = el("label", { class: "field", for: "battery-control-path" }, [el("span", { text: "Control path" }), controlPathSelect]);
    // The MQTT bridge driver needs a warning of its own: it drives the *device*, so a second
    // battery pointing at the same inverter (or a manager still managing it) would fight it.
    var mqttDriverNote = el("p", { class: "field-help", text: "The MQTT bridge's own entities: the add-on writes the device's AC mode and input/output limits, so the Zendure-HA integration must not manage the same inverter at the same time — disable this battery's twin there first, and map the Zendure manager's operation select above if that integration stays installed." });
    // The native driver needs neither the integration nor a broker; it does need the device's own
    // local API enabled, and the server proves the connection (and the serial) before saving.
    var nativeDriverNote = el("p", { class: "field-help", text: "The device's own local HTTP (zenSDK) API: no Zendure-HA entities and no MQTT broker are needed. Enable the device's local API first (in the Zendure app, add HEMS and exit to apply), then save — the add-on reads the device and checks the serial before the battery is stored. Every write is read back from the device before it is reported as applied." });
    // Only the Zendure-HA driver has two paths to choose between. A Zendure MQTT battery has one:
    // the device's own AC mode and limits, written through the bridge's own entities.
    function updateControlPathVisibility() {
      var zendureHa = driverSelect.value === "zendure";
      var native = driverSelect.value === NATIVE_ZENDURE_DRIVER;
      controlPathField.style.display = zendureHa ? "" : "none";
      controlPathNote.style.display = zendureHa ? "" : "none";
      mqttDriverNote.style.display = driverSelect.value === MQTT_ZENDURE_DRIVER ? "" : "none";
      nativeDriverNote.style.display = native ? "" : "none";
    }
    driverSelect.addEventListener("change", updateControlPathVisibility);

    var capacitySourceSelect = el("select", { id: "battery-capacity-source" });
    [
      ["manual", "Manual — use the capacity I typed"],
      ["learned", "Learned — measure the pack's real usable capacity"]
    ].forEach(function (option) {
      var node = el("option", { value: option[0], text: option[1] });
      if (option[0] === fields.capacity_source) node.selected = true;
      capacitySourceSelect.appendChild(node);
    });
    inputs.capacity_source = capacitySourceSelect;
    var capacitySourceNote = el("p", { class: "field-help", text: "Learned keeps the typed capacity as the seed/fallback and uses the estimate measured from this battery's own 5-minute power and SoC history: only windows with at least a 10-point SoC move and at least 20 % of the battery's own power limit count, and at least three accepted windows are needed before it is used. The estimate is ignored once it is older than 30 days or while it is not confident." });

    var identity = el("div", { class: "fields" }, [
      textRow("id", "Battery id (slug)", fields.id, editing),
      textRow("name", "Name", fields.name),
      el("label", { class: "field", for: "battery-driver" }, [el("span", { text: "Driver" }), driverSelect]),
      controlPathField,
      controlPathNote,
      mqttDriverNote,
      nativeDriverNote,
      fieldRow("capacity_kwh", "Capacity (kWh, usable AC side)", fields.capacity_kwh, "0.1", "0.1"),
      el("label", { class: "field", for: "battery-capacity-source" }, [el("span", { text: "Capacity source" }), capacitySourceSelect]),
      capacitySourceNote,
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

    // SPEC §5: wear comes from the pack's own price and rated cycle life; the old global
    // optimizer.cycle_cost_eur_per_kwh stays as an advanced override (server-side).
    var priceInput = el("input", { type: "number", min: "0", step: "1", value: String(fields.price_eur), id: "battery-price" });
    var cycleLifeInput = el("input", { type: "number", min: "0", step: "100", value: String(fields.cycle_life), id: "battery-cycle-life" });
    inputs.price_eur = priceInput;
    inputs.cycle_life = cycleLifeInput;
    var wearNote = el("p", { class: "muted" });
    function economicsInputs() {
      var eff = batteryEfficiencyPayload(roundTripInput.value, etaChargeInput.value, etaDischargeInput.value);
      return {
        price_eur: numeric(priceInput.value),
        cycle_life: numeric(cycleLifeInput.value),
        capacity_kwh: numeric(inputs.capacity_kwh.value),
        min_soc_pct: numeric(inputs.min_soc_pct.value),
        max_soc_pct: numeric(inputs.max_soc_pct.value),
        eta_charge: eff.payload ? eff.payload.eta_charge : null,
        eta_discharge: eff.payload ? eff.payload.eta_discharge : null
      };
    }
    function updateWearNote() {
      var inputsNow = economicsInputs();
      var wear = derivedWearEur(inputsNow);
      if (wear == null) {
        var oneSided = (inputsNow.price_eur > 0) !== (inputsNow.cycle_life > 0);
        wearNote.className = "muted";
        wearNote.textContent = oneSided
          ? "Fill in both the purchase price and the cycle life to derive wear; until then plans treat this battery as wear-free."
          : "Optional. With a purchase price and a cycle life the add-on derives wear = price ÷ (cycle life × usable kWh × discharge efficiency) per kWh delivered — a full cycle costs price ÷ cycle life.";
        return;
      }
      var usable = inputsNow.capacity_kwh * (inputsNow.max_soc_pct - inputsNow.min_soc_pct) / 100;
      var etaDischarge = isNum(inputsNow.eta_discharge) && inputsNow.eta_discharge > 0 ? inputsNow.eta_discharge : DEFAULT_ETA_DISCHARGE;
      wearNote.className = "muted";
      wearNote.textContent = "Derived wear " + fmtEur(wear) + "/kWh delivered = " + fmtEur(inputsNow.price_eur) +
        " ÷ (" + inputsNow.cycle_life + " cycles × " + fmtKwh(usable) + " usable × " + fmtPct(etaDischarge * 100) + " discharge efficiency). A full cycle delivers " +
        fmtKwh(usable * etaDischarge) + " and costs " + fmtEur(inputsNow.price_eur / inputsNow.cycle_life) + ". The advanced wear override in All settings replaces this when set.";
    }
    [priceInput, cycleLifeInput, inputs.capacity_kwh, inputs.min_soc_pct, inputs.max_soc_pct,
     roundTripInput, etaChargeInput, etaDischargeInput].forEach(function (input) {
      input.addEventListener("input", updateWearNote);
    });
    updateWearNote();
    var economicsBlock = el("div", null, [
      el("h3", { text: "Wear (optional)" }),
      el("div", { class: "fields" }, [
        el("label", { class: "field", for: "battery-price" }, [el("span", { text: "Purchase price (EUR)" }), priceInput]),
        el("label", { class: "field", for: "battery-cycle-life" }, [el("span", { text: "Cycle life (cycles)" }), cycleLifeInput])
      ]),
      wearNote
    ]);

    var mappingHost = el("div", { class: "fields" });
    function renderMappingRows() {
      clear(mappingHost);
      if (driverSelect.value === NATIVE_ZENDURE_DRIVER) {
        // A stored credential is read back masked; the field starts empty so an untouched form
        // never re-sends it, and the server keeps the stored value when the key is absent.
        if (mapping.token === "***") delete mapping.token;
        NATIVE_CONNECTION_FIELDS.forEach(function (field) {
          var key = field[0];
          var input = key === "token"
            ? el("input", { type: "password", value: "", placeholder: field[2], autocomplete: "off" })
            : el("input", { type: "text", value: mapping[key] || "", placeholder: field[2] });
          input.addEventListener("input", function () {
            var text = input.value.trim();
            if (text) mapping[key] = text; else delete mapping[key];
          });
          inputs["map:" + key] = input;
          mappingHost.appendChild(el("label", { class: "field" }, [el("span", { text: field[1] }), el("span", { class: "field-key", text: key }), input]));
        });
        mappingHost.appendChild(el("p", { class: "field-help", text: "The driver reads SoC, the measured AC power pair, PV, the device's own limits and pack voltages, and writes the AC direction plus both limits. Capacity in kWh is not reported by the device, so the value above stays the user's figure." }));
        return;
      }
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
    updateControlPathVisibility();
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
      discoveryHost.appendChild(table("Entity candidates", ["Entity", "Name", "Platform", "Suggested role", "Confidence", "Assign"], rows, { numeric: [4] }));
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
              ? "Discovery returned " + rowCount + " candidate row(s), but no group of them is a battery: rows are grouped per device, so a server that reports no device_id cannot be picked. Discovery caps at 50 rows per read, so a device may sit beyond them — search for its name or serial (the box above) to ask the server for it. Type the id and map entities by hand below."
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
      })), { numeric: [] }]));
    }

    /** Selecting a battery fills id, name, driver, roles and every number discovery states. */
    function selectBattery(group) {
      var selection = batterySelection(group, configuredIds);
      if (!editing) inputs.id.value = selection.id;
      inputs.name.value = selection.name;
      driverSelect.value = selection.driver;
      updateControlPathVisibility();
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
        price_eur: Number(inputs.price_eur.value),
        cycle_life: Number(inputs.cycle_life.value),
        capacity_source: capacitySourceSelect.value === "learned" ? "learned" : "manual",
        mapping: {},
        enabled: !!inputs.enabled.checked
      };
      // Only the Zendure driver has two write paths to choose between.
      if (driverSelect.value === "zendure") out.control_path = controlPathSelect.value;
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
      if (!(config.price_eur >= 0) || !(config.cycle_life >= 0)) problems.push("Price and cycle life must be numbers >= 0 (0 = unknown).");
      if (!(config.max_charge_w > 0 || config.max_discharge_w > 0)) problems.push("At least one power limit must be greater than 0 W.");
      if (!Object.keys(config.mapping).length) problems.push("Map at least one entity (or the manual mode value) before saving.");
      if (config.driver === "generic" && !config.mapping.power_setpoint_entity && !(config.mapping.charge_power_entity && config.mapping.discharge_power_entity)) {
        problems.push("A generic battery needs either a signed setpoint entity or a charge + discharge entity pair; a setpoint-less battery is rejected at save time.");
      }
      // The server refuses these too; catching them here explains the choice instead of echoing it.
      if (config.driver === "zendure" && config.control_path === "device" && !(config.mapping.charge_limit_entity && config.mapping.discharge_limit_entity)) {
        problems.push("Control path 'inverter itself' needs both the charge limit (input_limit) and the discharge limit (output_limit) entity mapped.");
      }
      if (config.driver === "zendure" && config.control_path === "manager" && !(config.mapping.manual_power_entity && config.mapping.operation_entity)) {
        problems.push("Control path 'Zendure manager' needs both the manager manual_power entity and its operation select.");
      }
      if (config.driver === MQTT_ZENDURE_DRIVER && !(config.mapping.charge_limit_entity && config.mapping.discharge_limit_entity)) {
        problems.push("A Zendure MQTT battery needs both the AC charge limit (inputLimit) and the AC output limit (outputLimit) entity: the device applies them itself and reports them back.");
      }
      if (config.driver === MQTT_ZENDURE_DRIVER && !config.mapping.ac_mode_entity) {
        problems.push("A Zendure MQTT battery needs the AC mode select (acMode) so the driver can pick the direction (Input/Output mode).");
      }
      if (config.driver === NATIVE_ZENDURE_DRIVER) {
        batteryNativeConnectionProblems(config.mapping).forEach(function (problem) { problems.push(problem); });
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
      var nativeDriver = config.driver === NATIVE_ZENDURE_DRIVER;
      var connectionText = nativeDriver
        ? "Device " + String(config.mapping.host || "") + ", serial " + String(config.mapping.serial || "") +
          ", over " + String(config.mapping.scheme || "http") + ":" + String(config.mapping.port || "(default)") + ". "
        : "";
      confirmAction({
        title: (editing ? "Save changes to " : "Add ") + config.name + "?",
        body: nativeDriver
          ? "Driver " + config.driver + ", " + config.capacity_kwh + " kWh, " + config.min_soc_pct + "–" + config.max_soc_pct + " % SoC. " + efficiencyText + ". " + connectionText + "The add-on reads the device and checks its serial before saving, so an unreachable device or a wrong serial is refused here."
          : "Driver " + config.driver + ", " + config.capacity_kwh + " kWh, " + config.min_soc_pct + "–" + config.max_soc_pct + " % SoC. " + efficiencyText + ". Mapping: " + mappingText,
        confirmLabel: editing ? "Save changes" : "Add battery",
        requireCheck: true,
        checkLabel: nativeDriver
          ? "I confirm the add-on may write this device's AC limits"
          : "I confirm these entities belong to this battery"
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
      el("p", { class: "muted", text: "Discovery only suggests; the mapping is stored after your explicit confirmation. Read it as: find a battery, review the device and its mapping, then add the optional economics." }),
      el("h3", { text: "Find a battery" }),
      el("p", { class: "field-help", text: "Pick a discovered battery to fill the id, name, driver, roles and every number Home Assistant states. Only groups that could be a battery are listed — devices that merely mention a battery level are not — and nothing is chosen until you press Select." }),
      el("div", { class: "toolbar" }, [
        el("label", { class: "field" }, [el("span", { text: "Search batteries" }), pickerSearch]),
        findBatteries
      ]),
      pickerNotes,
      pickerHost,
      identity,
      el("details", { class: "disclosure", open: true }, [
        el("summary", { text: editing ? "Review charge limits, efficiency and mapping" : "Review the mapping" }),
        el("div", { class: "disclosure-body" }, [
          efficiencyBlock,
          el("h3", { text: "Entity mapping" }),
          mappingHost
        ])
      ]),
      el("details", { class: "disclosure" }, [
        el("summary", { text: "Optional economics (wear)" }),
        el("div", { class: "disclosure-body" }, [economicsBlock])
      ]),
      el("details", { class: "disclosure" }, [
        el("summary", { text: "Discovery" }),
        el("div", { class: "disclosure-body" }, [
          el("div", { class: "toolbar" }, [searchInput, discover]),
          discoveryNote,
          discoveryHost
        ])
      ]),
      el("p", null, [enabledInput, el("label", { for: "battery-enabled", text: " Enabled for dispatch" })]),
      errorHost
    ]);
    append(dlg, [el("div", { class: "dialog-body" }, [body]), el("div", { class: "dialog-actions" }, [button("Cancel", "", function () { dlg.close(); }), save])]);
    document.body.appendChild(dlg);
    dlg.addEventListener("close", function () { dlg.remove(); });
    loadBatteryCandidates();
    dlg.showModal();
    return dlg;
  }

  /* ------------------------------------------------------------------ view: savings */

  // Source of truth: energy_manager/app/analytics.py BASELINE_NOTE (returned as `baseline_note`).
  var BASELINE_FALLBACK = "Baseline: the same measured consumption at the same prices with the battery doing nothing. Savings = baseline cost - actual cost.";

  // Baseline coverage must count only buckets that carry measured rows.  An empty bucket has no
  // activity to re-price, so it must never look like a covered period (the bug that made a month
  // of empty days display "Baseline available for 25 of 30 periods" with a 0.00 EUR total).
  // `has_data` is additive; an older API without it keeps its buckets treated as measured.
  function savingsCoverage(buckets) {
    var withData = buckets.filter(function (b) { return b.has_data !== false; });
    return {
      withData: withData,
      covered: withData.filter(function (b) { return b.baseline_cost_eur != null; })
    };
  }

  function renderSavingsTotals(body, payload) {
    var t = payload.totals || {};
    var buckets = Array.isArray(payload.buckets) ? payload.buckets : [];
    var cov = savingsCoverage(buckets);
    var covered = cov.covered;
    var withData = cov.withData;
    var savingsKnown = isNum(t.savings_eur);
    var baselineKnown = isNum(t.baseline_cost_eur);
    // 0.30.0: a missing interval is excluded from the comparison instead of discarding the period,
    // so the honest scope of the saving is the share of *sampled time* it covers.
    var sampledHours = isNum(t.total_hours) ? t.total_hours : 0;
    var coveredHours = isNum(t.savings_hours) ? t.savings_hours : 0;
    var gridHours = isNum(t.grid_hours) ? t.grid_hours : 0;
    var batteryHours = isNum(t.battery_hours) ? t.battery_hours : 0;
    var legacyPeriods = isNum(t.legacy_periods) ? t.legacy_periods : 0;
    var coveragePct = sampledHours > 0 ? Math.max(0, Math.min(100, Math.round(coveredHours / sampledHours * 100))) : null;
    var batteryPct = sampledHours > 0 ? Math.max(0, Math.min(100, Math.round(batteryHours / sampledHours * 100))) : null;
    function legacyNote() {
      if (!legacyPeriods) return "";
      return " " + legacyPeriods + " earlier period" + (legacyPeriods === 1 ? " predates" : "s predate") + " hour coverage; its stored totals still count.";
    }
    var reason = (!savingsKnown || !baselineKnown)
      ? (withData.length
          ? "unknown — no sampled interval in these periods could be re-priced against a baseline"
          : "unknown — no measured activity in this range yet")
      : "";
    // Loss, unknown and stale are three different states and never share one visual treatment
    // (UI_REVIEW §3.6).
    function savingsCard() {
      if (!savingsKnown) return statCard("Savings", "--", reason, "unknown");
      if (t.savings_eur < 0) return statCard("Savings", fmtEur(t.savings_eur), coveragePct == null ? "a loss: the battery cost more than doing nothing" : "a loss over " + coveragePct + "% of the sampled time: the battery cost more than doing nothing", "loss");
      return statCard("Savings", fmtEur(t.savings_eur), coveragePct == null ? "baseline minus actual (net)" : "baseline minus actual (net), over " + coveragePct + "% of the sampled time", false);
    }
    var cards = el("div", { class: "cards" }, [
      savingsCard(),
      statCard("Import cost", fmtEur(t.cost_eur), "metered intervals, before export revenue"),
      statCard("Baseline cost", fmtEur(t.baseline_cost_eur), baselineKnown ? "same load and tariffs, battery idle (net)" : reason, baselineKnown ? false : "unknown"),
      statCard("Import", fmtKwh(t.import_kwh), "from the grid"),
      statCard("Export", fmtKwh(t.export_kwh), "to the grid"),
      statCard("PV", fmtKwh(t.pv_kwh), "produced"),
      statCard(
        "Cycles",
        isNum(t.discharged_kwh) && isNum(t.cycles) ? t.cycles.toFixed(2) + " cycles" : "--",
        isNum(t.discharged_kwh) ? "measured discharge ÷ pack capacity" : "unknown: no confirmed battery measurement",
        isNum(t.discharged_kwh) ? false : "unknown"
      )
    ]);
    body.appendChild(cards);
    // Time coverage first: it is the scope of the number, not a caveat to hide at the bottom.
    if (savingsKnown && coveragePct != null && coveragePct < 100) {
      body.appendChild(el("div", { class: "banner banner-info" }, [
        el("p", { class: "banner-title", text: "Savings cover " + coveragePct + "% of the sampled time" }),
        el("p", { text: "The baseline and the saving add up interval by interval: " + fmtHours(coveredHours) + " of " + fmtHours(sampledHours) + " could be re-priced. An interval without a confirmed meter reading, battery flow or price is left out of both sides of the comparison instead of guessed, so the figure stays exact for the time it covers." + legacyNote() })
      ]));
    }
    // Coverage first: a partial baseline is scope, not a total (UI_REVIEW §3.6).
    var missing = withData.length - covered.length;
    if (withData.length && covered.length && covered.length < withData.length) {
      body.appendChild(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Baseline available for " + covered.length + " of " + withData.length + " measured periods" }),
        el("p", { text: missing + " measured period" + (missing === 1 ? " has" : "s have") + " no sampled interval that could be re-priced against a baseline (the meter, the realized load or the price was missing), so " + (missing === 1 ? "its" : "their") + " cost is not part of the comparison — the whole-range import/export/PV figures below still cover every metered interval." })
      ]));
    }
    if (buckets.length && withData.length < buckets.length) {
      var empty = buckets.length - withData.length;
      body.appendChild(el("p", { class: "legend-note", text: empty + " of " + buckets.length + " periods in this range have no recorded activity, so they carry no baseline and no saving." }));
    }
    if (!savingsKnown || !baselineKnown) {
      body.appendChild(el("div", { class: "banner banner-warn" }, [
        el("p", { class: "banner-title", text: "Baseline and savings are unknown for this range" }),
        el("p", { text: "The server reports null because the realised load for these periods could not be re-priced against a do-nothing baseline. Unknown is not zero: the add-on will not guess a saving. Periods that are knowable are still summed and shown." })
      ]));
    }
    // Comparable figures: baseline and actual net cost over the same covered periods.  The actual
    // net cost of those periods is baseline − savings, which stays exact even when a bucket also
    // contains rows that could not be re-priced (a month in the year/all range).
    if (covered.length) {
      var baselineSum = covered.reduce(function (sum, b) { return sum + (b.baseline_cost_eur || 0); }, 0);
      var netSum = covered.reduce(function (sum, b) { return sum + (b.baseline_cost_eur - (b.savings_eur || 0)); }, 0);
      body.appendChild(el("ul", { class: "stat-row" }, [
        el("li", null, [el("span", { class: "stat-label", text: "Baseline cost (covered periods)" }), el("span", { class: "stat-value", text: fmtEur(baselineSum) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Actual net cost (same periods)" }), el("span", { class: "stat-value", text: fmtEur(netSum) })])
      ]));
      body.appendChild(el("p", { class: "legend-note", text: "Actual net cost is import cost minus export revenue over the same covered periods, so the two figures are comparable; the whole-range import/export/PV figures above are kept separately labelled." }));
    }
    if (isNum(t.arbitrage_kwh) || isNum(t.self_consumption_kwh)) {
      body.appendChild(el("ul", { class: "stat-row" }, [
        el("li", null, [el("span", { class: "stat-label", text: "Battery energy for arbitrage" }), el("span", { class: "stat-value", text: fmtKwh(t.arbitrage_kwh) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Battery energy for self-consumption" }), el("span", { class: "stat-value", text: fmtKwh(t.self_consumption_kwh) })])
      ]));
    } else {
      body.appendChild(el("p", { class: "legend-note", text: "This backend version does not report the arbitrage / self-consumption split yet." }));
    }
    // Measured battery flows (0.13.0): only energy the battery really moved earns credit, so these
    // are integrated from confirmed power readings and priced at flow time.  Null means no bucket
    // in the range had a complete measurement: unknown, never a planned value.
    var batteryFlowsKnown = isNum(t.charged_kwh) || isNum(t.discharged_kwh);
    body.appendChild(el("div", { class: "banner banner-info" }, [
      el("p", { class: "banner-title", text: "Measured battery flows" }),
      el("p", { text: "These are energy flows the battery actually moved: measured AC power integrated over time, not what the plan asked for. A dry-run, blocked, stale-refused or unconfirmed setpoint earns no flow credit; a clamped write counts only the measured power." })
    ]));
    if (batteryFlowsKnown) {
      body.appendChild(el("ul", { class: "stat-row" }, [
        el("li", null, [el("span", { class: "stat-label", text: "Charged (measured)" }), el("span", { class: "stat-value", text: fmtKwh(t.charged_kwh) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Discharged (measured)" }), el("span", { class: "stat-value", text: fmtKwh(t.discharged_kwh) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Charge cost (measured, at buy)" }), el("span", { class: "stat-value", text: fmtEur(t.charge_cost_eur) })]),
        el("li", null, [el("span", { class: "stat-label", text: "Discharge value (measured, at sell)" }), el("span", { class: "stat-value", text: fmtEur(t.discharge_value_eur) })])
      ]));
      if (batteryPct != null) {
        body.appendChild(el("p", { class: "legend-note", text: "Measured over " + batteryPct + "% of the sampled time (" + fmtHours(batteryHours) + " of " + fmtHours(sampledHours) + "). A gap is not counted as a flow and not counted as a zero." }));
      }
    } else {
      body.appendChild(el("p", { class: "legend-note", text: "No confirmed battery measurement in this range, so the measured battery flows are unknown (--): a planned or requested setpoint is not counted as delivered energy." }));
    }
    // Sampled-time coverage (0.30.0).  The series-completeness counts are kept for an older API
    // payload, but hours are what a partially measured period can honestly state.
    if (sampledHours > 0) {
      body.appendChild(el("p", { class: "legend-note", text: "Sampled-time coverage: grid " + fmtHours(gridHours) + ", battery " + fmtHours(batteryHours) + ", baseline " + fmtHours(coveredHours) + " of " + fmtHours(sampledHours) + " sampled." + legacyNote() }));
    } else if (t.measured_buckets) {
      var measured = t.measured_buckets;
      var names = [["grid", "grid"], ["load", "load"], ["pv", "PV"], ["battery", "battery"]];
      var parts = names.map(function (pair) {
        return pair[1] + " " + (measured[pair[0]] || 0) + " of " + buckets.length;
      });
      body.appendChild(el("p", { class: "legend-note", text: "Measured-series coverage (buckets with every interval measured): " + parts.join(", ") + ". A bucket missing a series reports its flows as unknown, not as zero." }));
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
    var cov = savingsCoverage(buckets);
    if (cov.covered.length < cov.withData.length) {
      body.appendChild(el("p", { class: "legend-note", text: cov.covered.length + " of " + cov.withData.length + " measured buckets have a baseline (a load the meter and battery could be re-priced against). A bucket without one shows -- for baseline and savings: unknown, not zero." }));
    }
    var rows = buckets.map(function (b) {
      var flow = function (key, fmt) { return b[key] == null ? "--" : fmt(b[key]); };
      // A bucket with no stored row is "no recorded activity", not a measured zero.
      var val = function (key, fmt) {
        if (b.has_data === false || b[key] == null) return "--";
        return fmt(b[key]);
      };
      return [
        b.key || "",
        fmtDateTime(b.start) + " – " + fmtDateTime(b.end),
        val("import_kwh", fmtKwh),
        val("export_kwh", fmtKwh),
        val("pv_kwh", fmtKwh),
        val("cost_eur", fmtEur),
        val("revenue_eur", fmtEur),
        b.baseline_cost_eur == null ? "--" : fmtEur(b.baseline_cost_eur),
        b.savings_eur == null ? "--" : fmtEur(b.savings_eur),
        b.battery_measured ? (isNum(b.cycles) ? b.cycles.toFixed(2) + " cycles" : "--") : "-- (unmeasured)",
        flow("charged_kwh", fmtKwh),
        flow("discharged_kwh", fmtKwh),
        flow("charge_cost_eur", fmtEur),
        flow("discharge_value_eur", fmtEur)
      ];
    });
    body.appendChild(table("Buckets for range " + payload.range, ["Bucket", "Period", "Import", "Export", "PV", "Cost", "Revenue", "Baseline", "Savings", "Cycles", "Charged (measured)", "Discharged (measured)", "Charge cost", "Discharge value"], rows));
  }

  function viewSavings(host, query) {
    var ranges = ["day", "week", "month", "year", "all"];
    var range = ranges.indexOf(query.range) >= 0 ? query.range : "month";
    var load = once(function () { return get("/savings", { range: range }); });

    routeTitle(host, "Savings", "Counterfactual saving against doing nothing — an estimate, not a bill change.");
    var controls = panel(host, "Savings range");
    var select = el("select", { id: "savings-range" });
    ranges.forEach(function (r) {
      var option = el("option", { value: r, text: r });
      if (r === range) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function () { root.location.hash = "#/savings?range=" + select.value; });
    controls.body.appendChild(el("div", { class: "toolbar" }, [el("label", { for: "savings-range" }, ["Range ", select])]));
    controls.body.appendChild(el("p", { class: "legend-note", text: "Savings are computed from the stored actuals and the same prices, so trimming history never changes them. Baseline and savings are null (shown as --) when the realised load cannot be re-priced: there is no load/PV reading and no meter + battery pair to derive it from. Measured battery flows count only confirmed measurements — a dry-run, blocked or unconfirmed setpoint earns nothing." }));

    asyncPanel(host, "Totals", { load: load, render: renderSavingsTotals });
    asyncPanel(host, "Savings vs baseline", {
      load: load,
      render: function (body, payload) {
        var chart = el("div", { class: "chart chart-md" });
        body.appendChild(chart);
        root.SEMCharts.savings(chart, payload.buckets || []);
        body.appendChild(el("p", { class: "legend-note", text: "Buckets with a null baseline (no load forecast) are skipped, not plotted as zero." }));
      }
    });
    asyncPanel(host, "Per-bucket detail", { load: load, render: renderSavingsBuckets });
    return null;
  }

  /* ------------------------------------------------------------------ view: settings */

  var SAFETY_KEY_RE = /(reserve_soc_pct|max_grid_import_w|max_grid_export_w|target_soc_pct_end|allow_grid_charge|allow_export|min_spread_eur_per_kwh|grid_follow_max_w|^control\.|^price\.feed_in_)/;

  // Control authority is safety-relevant wherever it is edited: enabling auto, and turning dry run
  // off, are acknowledged exactly like any other barrier-bearing change (UI_REVIEW §3.3).
  var AUTHORITY_KEYS = ["mode", "dry_run"];

  // Friendly text for technical enum values; the value saved to the API is unchanged (UI_REVIEW §3.7).
  var CHOICE_LABELS = {
    "price.provider": {
      entity: "Your own price entity",
      zonneplan: "Zonneplan (found automatically)",
      day_ahead: "Wholesale day-ahead",
      fixed: "Fixed tariff"
    },
    "price.contract": {
      dynamic_quarter_hourly: "Dynamic (quarter-hourly)",
      dynamic_hourly: "Dynamic (hourly)",
      fixed_single: "One fixed price",
      fixed_day_night: "Day and night prices"
    },
    "solar.source": {
      forecast_solar: "Forecast.Solar (per plane)",
      capacity: "Array capacity (watt peak)",
      derived: "Learn from the meter",
      learned: "Learned model (trained on other arrays)"
    },
    mode: { off: "Off", advisory: "Advisory", auto: "Auto" },
    log_level: { debug: "Debug", info: "Info", warning: "Warning", error: "Error" }
  };

  function choiceText(key, value) {
    var map = CHOICE_LABELS[key];
    // Ownership is asked of the *map*, not of ``map[value]``. Calling hasOwnProperty with an
    // absent key's value passes ``undefined`` as the receiver and throws, which takes down the whole
    // settings view the moment the backend gains a choice value this table has not heard of yet.
    // A missing label is a cosmetic gap; a blank settings page is not.
    return map && Object.prototype.hasOwnProperty.call(map, value) ? map[value] : String(value);
  }

  // The API returns settings sorted by key, which interleaves groups; present them in a stable
  // reading order instead (unknown groups go last, alphabetically).
  var GROUP_ORDER = ["General", "Price", "Solar", "Load", "Grid", "Optimizer", "Control", "Safety", "Maintenance", "Notify", "Batteries"];

  function isSafetySetting(descriptor) {
    if (descriptor && AUTHORITY_KEYS.indexOf(descriptor.key) >= 0) return true;
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
  var SOLAR_FIELDS_ALWAYS = ["solar.source", "solar.enabled", "solar.bias_correction", "solar.measured_entity", "solar.sources", "solar.merge_strategy"];
  // forecast_solar: the API call — per-plane geometry, plus the optional own key (keyless is
  // single-plane hourly, which is why planes is the field that really matters).
  var SOLAR_FIELDS_FORECAST_SOLAR = ["solar.planes", "solar.api_key"];
  // capacity: the array's total watt peak, the geometry the model assumes while it is unset, the
  // performance ratio and the optional weather entity for cloud attenuation.
  var SOLAR_FIELDS_CAPACITY = [
    "solar.capacity_wp", "solar.tilt_deg", "solar.azimuth_deg", "solar.performance_ratio",
    "solar.weather_entity", "solar.nasa_irradiance"
  ];
  // learned (SPEC §7.1.4): the same array geometry and weather inputs as the capacity estimate, plus
  // the inverter's AC limit so the model can clip. solar.performance_ratio is deliberately absent —
  // the artifact carries its own inverter, module-temperature and clipping terms, so a ratio applied
  // on top of it would count those losses twice.
  var SOLAR_FIELDS_LEARNED = [
    "solar.capacity_wp", "solar.ac_capacity_wp", "solar.tilt_deg", "solar.azimuth_deg",
    "solar.weather_entity", "solar.nasa_irradiance"
  ];
  // The one-line note under the source select, so a user who later reads an explained forecast
  // knows which model produced it. Capacity states its assumptions because they are stated (not
  // silent) in the forecast notes too (SPEC §7.1.1).
  var SOLAR_SOURCE_NOTES = {
    forecast_solar: "Per-plane geometry (kWp, tilt, azimuth): the Forecast.Solar API is asked per plane, so each plane is modelled on its own tilt and direction.",
    capacity: "Estimate from the array's total watt peak: enter the Wp and the add-on models production from the sun's position, optionally attenuated by a weather entity's cloud cover; leave tilt/azimuth unset and it assumes south-facing at 35 degrees and says so in the forecast notes.",
    derived: "Estimate from the meter: at every moment the meter exports, the array is producing at least the exported power plus whatever the battery is charging (pv = export + battery charge). The add-on learns the array's watt peak from those moments and then models it like the capacity source. Nothing to enter — it needs the Meter card and a battery whose charge/discharge power is mapped. The house load at those moments is not subtracted, so the learned size is a conservative lower bound.",
    learned: "A model trained on 174 other Dutch rooftop arrays, applied to yours. It predicts the ratio between what physics says an array of your size should make and what an array actually makes, so one model can serve a roof it has never seen — and it needs only the array's watt peak to start, before this house has any history of its own. Set the inverter's AC limit when it is smaller than the array: past that limit the DC curve flattens and the model has to be told, or it will keep forecasting output the inverter cannot deliver. The performance ratio is not used here — the model carries its own inverter, module-temperature and soiling terms."
  };

  /** Visible solar.* keys for a source, plus the note to show.
   *  Pure (no DOM) so node tests can assert the table. */
  function solarFieldVisibility(source) {
    var s = String(source || "forecast_solar");
    // derived runs the capacity model on a watt peak learned from the meter, so the geometry,
    // performance ratio and weather fields apply there too (an explicit solar.capacity_wp wins).
    var specific = s === "forecast_solar"
      ? SOLAR_FIELDS_FORECAST_SOLAR
      : (s === "learned" ? SOLAR_FIELDS_LEARNED : SOLAR_FIELDS_CAPACITY);
    var keys = SOLAR_FIELDS_ALWAYS.concat(specific);
    // An unknown source keeps the forecast_solar fields (never an empty group), the same fallback
    // the price table uses, so a newer backend value cannot hide every field.
    var note = SOLAR_SOURCE_NOTES[s] != null ? SOLAR_SOURCE_NOTES[s] : null;
    return { fields: keys, note: note || null };
  }

  /* ---- named load devices (SPEC §7.2) -----------------------------------------------------------
   * Each device has its own meter, its own (weekend|weekday, hour) profile and — for the kinds that
   * respond to weather — its own temperature term. The kind is a closed set because it decides
   * whether the model fits such a term at all; the local list mirrors load.py's kind table and the
   * server's descriptor carries the same set as choices, which wins when present so a newer backend
   * kind needs no UI release. Pure (no DOM), so the row rules have a node-testable surface. */
  var LOAD_DEVICE_KINDS = ["heat_pump", "ev_charger", "water_heater", "pool_pump"];
  var LOAD_DEVICE_KIND_LABELS = {
    heat_pump: "Heat pump",
    ev_charger: "EV charger",
    water_heater: "Water heater",
    pool_pump: "Pool pump"
  };

  /** Client-side problems of a load.devices list: what must be fixed before a save can succeed.
   *  The meter is the one field the add-on cannot infer, and a repeated meter would be subtracted
   *  twice — a double count that hides inside the temperature slope — so both are refused here
   *  rather than left to the server's 400. */
  function loadDeviceProblems(entries, kinds) {
    var known = Array.isArray(kinds) && kinds.length ? kinds : LOAD_DEVICE_KINDS;
    var out = [];
    var seen = {};
    (Array.isArray(entries) ? entries : []).forEach(function (entry, index) {
      var where = "Device " + (index + 1);
      var entity = entry && entry.entity != null ? String(entry.entity).trim() : "";
      if (!entity) { out.push(where + " has no meter"); return; }
      if (seen[entity]) out.push(where + " repeats " + entity + "; one meter cannot be two devices");
      seen[entity] = true;
      var kind = entry && entry.kind != null ? String(entry.kind) : "";
      if (known.indexOf(kind) < 0) out.push(where + " has an unknown kind " + (kind || "(none)"));
    });
    return out;
  }


  /* ---- setup cards (SPEC 13.2, amended; the Load card was added with named devices) -------------
   * The settings page opens on five setup cards that carry the primary choice of each topic *and
   * the fields that choice needs to be complete*: the capacity estimate needs the array's watt
   * peak, Forecast.Solar needs its plane, a fixed day/night tariff needs both rates and both window
   * start hours, and wholesale day-ahead prices need the adders that turn them into a consumer
   * price. "Advanced" therefore means "optional for this user's chosen source", not "not on the
   * first screen" (UI_REVIEW §3.2). Every other key (including the rest of a card's own group)
   * lives in the "All settings" drawer behind the existing rail, and a key rendered on a card is
   * *not* rendered again in its group, so every key still appears exactly once. Pure (no DOM): the
   * tested surface for which keys the cards own at the current provider/contract/solar-source
   * selection. */
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
        keys.push("price.fixed_day_price_eur_per_kwh", "price.fixed_night_price_eur_per_kwh",
          "price.fixed_day_start_hour", "price.fixed_night_start_hour");
      } else {
        keys.push("price.fixed_price_eur_per_kwh");
      }
    }
    // Day-ahead prices are wholesale: without VAT, energy tax or a fee they are not a consumer
    // price, so those adders belong on the card that chose the source, not behind a disclosure.
    if (provider === "day_ahead") {
      keys.push("price.vat_pct", "price.energy_tax_eur_per_kwh", "price.import_fee_eur_per_kwh");
    }
    if (source === "capacity") keys.push("solar.capacity_wp");
    // The learned source is unusable without a watt peak, and the inverter limit is the one number
    // that decides whether an afternoon clip shows up in the forecast at all, so both are on the card.
    if (source === "learned") keys.push("solar.capacity_wp", "solar.ac_capacity_wp");
    if (source === "forecast_solar") keys.push("solar.planes");
    if (source !== "derived") keys.push("solar.measured_entity");
    // The merge list and its strategy are part of the Solar card whatever the single source is.
    keys.push("solar.sources", "solar.merge_strategy");
    // The Load card carries the house meter, its optional outdoor-temperature sensor and the
    // named-device editor (SPEC §7.2); load.mode and the fallback daily energy stay in the drawer.
    keys.push("load.entity", "load.temperature_entity", "load.devices");
    return keys;
  }

  // Two guidance lines that live under the Solar group instead of in a field's help text, because
  // they are about the entity itself rather than the typed value: what the measured-PV learning is
  // fitted to, and what the weather entity has to support for cloud attenuation to work at all.
  var SOLAR_MEASURED_NOTE = "solar.measured_entity must be the inverter's AC output: a meter's export register under-reports, because self-consumed energy never reaches the meter, so the learned per-hour correction would come out low. It is what makes the forecast more accurate over time.";
  var SOLAR_WEATHER_NOTE = "solar.weather_entity must support the weather.get_forecasts action (Met.no's forecast entity does; some integrations, including the OpenWeatherMap entity on this install, only publish a current cloud_coverage and support no forecast type) — without a forecast type the add-on holds the current cloud cover flat and says so in the forecast notes, so the field is still worth setting.";

  // Two keys whose backend help understates the safety model: the UI states the exact
  // dispatcher/Stop distinction here — the same wording the Dashboard answer strip uses
  // (UI_REVIEW §3.3).
  var KEY_HELP = {
    mode: "off = no dispatch; advisory = plan and log but never write; auto = the plan dispatcher writes setpoints. Advisory and off stop new writes, but a setpoint already written is only neutralised by Stop now (a real write, even in dry run).",
    dry_run: "On: the plan dispatcher writes nothing — it logs what it would send. Stop now still writes 0 W to the hardware, even in dry run."
  };

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
        var option = el("option", { value: choice, text: choiceText(key, choice) });
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
    if (KEY_HELP[key] || descriptor.help) wrap.appendChild(el("p", { class: "field-help", text: KEY_HELP[key] || descriptor.help }));
    if (descriptor.key === "batteries") wrap.appendChild(el("p", { class: "field-help" }, [el("a", { href: "#/batteries", text: "Edit batteries in the Batteries view" })]));
    return wrap;
  }

  function showFieldError(wrap, message) {
    var existing = wrap.querySelector(".field-error");
    var input = wrap.querySelector("input, select, textarea");
    if (!message) {
      if (existing) existing.remove();
      if (input) {
        input.removeAttribute("aria-describedby");
        input.setAttribute("aria-invalid", "false");
      }
      return;
    }
    if (existing) existing.textContent = message;
    else {
      existing = el("p", { class: "field-error", text: message });
      wrap.appendChild(existing);
    }
    // A field error is announced with its own control, not only somewhere else on the page
    // (UI_REVIEW §3.11).
    if (input) {
      var errorId = "err-" + String(wrap.dataset && wrap.dataset.key ? wrap.dataset.key : "field").replace(/[^a-z0-9_-]+/gi, "-");
      existing.id = errorId;
      input.setAttribute("aria-describedby", errorId);
      input.setAttribute("aria-invalid", "true");
    }
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

  function renderSettings(body, data) {
    var config = data.config || {};
    // The canonical inventory is GET /batteries, not the `batteries` settings descriptor: the two
    // can disagree, and a descriptor that reads as an empty fleet must never answer the question
    // (UI_REVIEW §3.2 / open question 3).
    var fleet = Array.isArray(data.fleet) ? data.fleet : [];
    // Provider status is optional on the Settings route: the Solar card shows each configured merge
    // source's state when it is available, and never invents one when the read failed.
    var providers = (data && data.providers) || null;
    var solarProviders = (providers && Array.isArray(providers.solar)) ? providers.solar : [];
    var descriptors = Array.isArray(config.settings) ? config.settings : [];
    if (!descriptors.length) { body.appendChild(el("p", { class: "muted", text: "The server sent no setting descriptors." })); return; }
    var dirtyValues = {};
    var dirtyCount = 0;
    // Keys the four setup cards render this draw; their groups skip them so a key appears once.
    var cardKeys = {};
    // SPEC 3: the Meter card's provenance — what the Energy dashboard resolves, whether an
    // explicit grid.* setting overrides it, and the fallback the panel can offer to restore.
    var grid = (config.grid && typeof config.grid === "object") ? config.grid : {};

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
      // A card may clear several keys at once; the fields themselves re-render from settingValue.
      if (wrap) {
        wrap.classList.add("field-dirty");
        if (invalid) wrap.setAttribute("data-invalid", "1");
        else wrap.removeAttribute("data-invalid");
      }
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
      if (item.key === "batteries") {
        // Server-managed inventory (the battery wizard owns it): showing an editable JSON field
        // here could only ever be rejected by the server, so it is a provenance line instead.
        row = el("p", { class: "muted", dataset: { key: item.key } }, [
          item.key + " — managed in the Batteries view (", fleet.length, " battery", fleet.length === 1 ? "" : "s", " configured). ",
          el("a", { href: "#/batteries", text: "Open the Batteries view" })
        ]);
      } else if (settingHiddenBySource(item, opts)) {
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
    // Five cards carry the primary choice each; their keys are skipped by the groups below. A key
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
          children.push(cardField("price.fixed_day_start_hour"));
          children.push(cardField("price.fixed_night_start_hour"));
        } else {
          children.push(cardField("price.fixed_price_eur_per_kwh"));
        }
      }
      children.push(cardField("price.export_fee_eur_per_kwh"));
      children.push(cardField("price.feed_in_energy_tax_refund"));
      // Wholesale day-ahead prices only become consumer prices through these adders, so they are
      // on the card that chose the source (UI_REVIEW §3.2).
      if (provider === "day_ahead") {
        children.push(cardField("price.vat_pct"));
        children.push(cardField("price.energy_tax_eur_per_kwh"));
        children.push(cardField("price.import_fee_eur_per_kwh"));
      }
      var note = priceFieldVisibility(provider, contract).note;
      if (note) children.push(el("p", { class: "field-help", text: note }));
      return setupCard(
        "Price",
        "Pick where prices come from; the add-on fetches the slots itself. The sell price is the buy price minus the feed-in cost.",
        children
      );
    }

    // SPEC 3: the grid role -> settings key map the Meter card clears, and the Energy-dashboard
    // description it shows. The dashboard's own roles stay visible under an override so the user
    // can tell the fallback from the value replacing it (and go back to it).
    var GRID_ROLE_KEYS = {
      grid_power: "grid.power_entity",
      grid_import: "grid.import_entity",
      grid_export: "grid.export_entity"
    };

    function gridExplicitKeys() {
      return Object.keys(GRID_ROLE_KEYS).map(function (role) { return GRID_ROLE_KEYS[role]; })
        .filter(function (key) { return String(settingValue(key) || "").length > 0; });
    }

    function gridDashboardText() {
      var dash = grid.dashboard || {};
      var parts = [];
      if (dash.grid_power) parts.push("signed power " + dash.grid_power);
      if (dash.grid_import) parts.push("import power " + dash.grid_import);
      if (dash.grid_export) parts.push("export power " + dash.grid_export);
      if (dash.grid_energy_import) parts.push("import counter " + dash.grid_energy_import);
      if (dash.grid_energy_export) parts.push("export counter " + dash.grid_energy_export);
      return parts.join("; ");
    }

    function clearGridRoles() {
      gridExplicitKeys().forEach(function (key) { dirty(key, ""); });
      draw();
    }

    function meterCard() {
      var children = [cardPicker("grid.power_entity", "grid", "grid power sensor name or entity id")];
      var dashboardText = gridDashboardText();
      if (gridExplicitKeys().length) {
        children.push(el("p", {
          class: "field-help",
          text: dashboardText
            ? "Set here; this overrides the Home Assistant Energy dashboard."
            : "Set here; the Home Assistant Energy dashboard has no grid meter."
        }));
        if (dashboardText) {
          children.push(el("p", { class: "field-help muted", text: "The Energy dashboard has " + dashboardText + "." }));
          children.push(button("Use the Energy dashboard", "", clearGridRoles));
        }
      } else if (dashboardText) {
        children.push(el("p", { class: "field-help", text: "From the Home Assistant Energy dashboard: " + dashboardText + "." }));
      } else {
        children.push(el("p", { class: "field-help muted", text: "Not set, and the Home Assistant Energy dashboard has no grid meter either." }));
      }
      return setupCard(
        "Meter",
        "The live power the add-on follows. The load forecast, and the meter-derived solar estimate, read this meter.",
        children
      );
    }

    // The merge list and its strategy (SPEC §7.1.3). The All-settings JSON row for solar.sources
    // remains the fallback path; this editor is the normal add/remove/reorder surface and commits
    // through the same dirty/Save flow.
    var SOLAR_SOURCE_KINDS = ["forecast_solar", "ha_energy", "capacity", "derived", "learned"];
    var SOLAR_SOURCE_KIND_LABELS = {
      forecast_solar: "Forecast.Solar (API)",
      ha_energy: "Home Assistant Energy dashboard",
      capacity: "Array capacity (watt peak)",
      derived: "Learn from the meter",
      learned: "Learned model (SPEC 7.1.4)"
    };

    function solarSourceSelect(value, onChange) {
      var select = el("select", { class: "source-kind" });
      SOLAR_SOURCE_KINDS.forEach(function (option) {
        var node = el("option", { value: option, text: SOLAR_SOURCE_KIND_LABELS[option] });
        if (String(value) === option) node.selected = true;
        select.appendChild(node);
      });
      select.addEventListener("change", function () { onChange(select.value); });
      return select;
    }

    function solarSourcesEditor() {
      var value = settingValue("solar.sources");
      var list = Array.isArray(value) ? value.map(function (entry) { return Object.assign({}, entry); }) : [];
      var wrap = el("div", { class: "field", dataset: { key: "solar.sources" } });
      wrap.appendChild(el("label", { text: "Forecast sources (merge)" }));
      wrap.appendChild(el("span", { class: "field-key", text: "solar.sources" }));
      wrap.appendChild(el("p", { class: "field-help", text: "Merge several PV forecasts into one hourly series. Empty = the single source above is used unchanged. Each entry is {source, weight, ...}; a per-source key overrides the matching solar.* setting for that entry only." }));
      function commit(next) { dirty("solar.sources", next, wrap); draw(); }
      function update(index, patch) {
        commit(list.map(function (entry, i) { return i === index ? Object.assign({}, entry, patch) : entry; }));
      }
      function move(index, delta) {
        var target = index + delta;
        if (target < 0 || target >= list.length) return;
        var next = list.slice();
        var item = next.splice(index, 1)[0];
        next.splice(target, 0, item);
        commit(next);
      }
      function removeAt(index) {
        commit(list.filter(function (_entry, i) { return i !== index; }));
      }
      function statusFor(entry) {
        var want = String(entry.name || entry.source || "");
        var i;
        for (i = 0; i < solarProviders.length; i += 1) {
          if (String(solarProviders[i].name || "") === want) return solarProviders[i];
        }
        for (i = 0; i < solarProviders.length; i += 1) {
          if (String(solarProviders[i].name || "").indexOf(want + ":") === 0) return solarProviders[i];
        }
        return null;
      }
      var rows = el("div", { class: "source-list" });
      if (!list.length) {
        rows.appendChild(el("p", { class: "muted", text: "No merge sources configured: the single source above is used." }));
      }
      list.forEach(function (entry, index) {
        var row = el("div", { class: "source-row" });
        row.appendChild(solarSourceSelect(entry.source, function (source) { update(index, { source: source }); }));
        var weight = el("input", {
          type: "number",
          class: "source-weight",
          min: "0",
          step: "any",
          value: entry.weight === undefined || entry.weight === null ? "1" : String(entry.weight)
        });
        weight.addEventListener("input", function () {
          var raw = weight.value.trim();
          var number = raw === "" ? 1 : Number(raw);
          if (!isFinite(number) || number <= 0) return;
          update(index, { weight: number });
        });
        row.appendChild(el("label", { class: "source-weight-label" }, ["Weight", weight]));
        row.appendChild(button("Move up", "", function () { move(index, -1); }));
        row.appendChild(button("Move down", "", function () { move(index, 1); }));
        row.appendChild(button("Remove", "", function () { removeAt(index); }));
        var status = statusFor(entry);
        row.appendChild(el("span", {
          class: "source-status badge badge-info",
          text: status
            ? (status.state || (status.ok ? "ok" : "failed")) + (status.note || status.last_error ? " - " + (status.note || status.last_error) : "")
            : "no status reported yet"
        }));
        rows.appendChild(row);
      });
      wrap.appendChild(rows);
      wrap.appendChild(button("Add source", "", function () {
        commit(list.concat([{ source: "ha_energy", weight: 1 }]));
      }));
      return wrap;
    }

    function solarCard() {
      var source = String(settingValue("solar.source") || "forecast_solar");
      var children = [cardField("solar.enabled"), cardField("solar.source")];
      if (source === "derived") {
        children.push(el("p", { class: "field-help", text: "Uses the Meter card and the battery's charge/discharge power; nothing to enter here." }));
      } else {
        children.push(cardPicker("solar.measured_entity", "solar", "PV power sensor name or entity id"));
      }
      // The input the chosen source needs to be complete: the array's watt peak for the capacity
      // estimate, the plane list for Forecast.Solar (UI_REVIEW §3.2).
      if (source === "capacity") children.push(cardField("solar.capacity_wp"));
      if (source === "forecast_solar") children.push(cardField("solar.planes"));
      var note = solarFieldVisibility(source).note;
      if (note) children.push(el("p", { class: "field-help", text: note }));
      // The multi-source merge editor (SPEC §7.1.3): carries solar.sources on the card so the
      // groups skip it, and owns the strategy select beside it.
      cardKeys["solar.sources"] = true;
      children.push(solarSourcesEditor());
      var strategy = cardField("solar.merge_strategy");
      if (strategy) children.push(strategy);
      return setupCard("Solar", "Optional. How the add-on should know your production — one source, or several merged.", children);
    }

    // The named-device list (SPEC §7.2). Unlike the solar merge list there is no single-entry
    // fallback, so an empty list is the normal state and means one house-wide profile. Every row is
    // a meter picker, a kind dropdown and an optional display name: the kind decides whether the
    // device gets its own temperature term, and the meter is the one thing the add-on cannot infer.
    // Rows that cannot be saved (no meter, a repeated meter) are marked invalid by dirty(), so a
    // save is never attempted with a list the server has to reject.
    function loadDeviceKindSelect(value, kinds, onChange) {
      var select = el("select", { class: "device-kind" });
      kinds.forEach(function (option) {
        var node = el("option", { value: option, text: LOAD_DEVICE_KIND_LABELS[option] || option });
        if (String(value) === option) node.selected = true;
        select.appendChild(node);
      });
      if (value && kinds.indexOf(String(value)) < 0) {
        // A kind this UI does not know (a newer backend) must still be shown, not silently changed
        // to the first option the moment the user touches another field.
        var extra = el("option", { value: String(value), text: String(value) + " (unknown)" });
        extra.selected = true;
        select.appendChild(extra);
      }
      select.addEventListener("change", function () { onChange(select.value); });
      return select;
    }

    function loadDeviceKinds() {
      // The backend's descriptor carries the closed kind set (config.LOAD_DEVICE_KINDS) as choices;
      // the local table is the fallback for a mock or an older payload, so never an empty dropdown.
      var descriptor = descriptorByKey("load.devices");
      var choices = descriptor && Array.isArray(descriptor.choices)
        ? descriptor.choices.filter(function (choice) { return !!choice; })
        : [];
      return choices.length ? choices : LOAD_DEVICE_KINDS;
    }

    function loadDevicesEditor() {
      var value = settingValue("load.devices");
      var list = Array.isArray(value) ? value.map(function (entry) { return Object.assign({}, entry); }) : [];
      var kinds = loadDeviceKinds();
      var wrap = el("div", { class: "field", dataset: { key: "load.devices" } });
      wrap.appendChild(el("label", { text: "Named load devices" }));
      wrap.appendChild(el("span", { class: "field-key", text: "load.devices" }));
      wrap.appendChild(el("p", { class: "field-help", text: "A device meter is subtracted from the house total before the base profile is learned, and the device is added back above it with its own (weekend|weekday, hour) profile and, for a heat pump, water heater or pool pump, its own temperature term. An EV charger draws on its schedule, so it gets no temperature term. A device that cannot be measured is not subtracted: it stays inside the base profile and the forecast says so, because adding it back on top of a base that already contains it would count it twice." }));
      function commit(next, redraw) {
        dirty("load.devices", next, wrap, loadDeviceProblems(next, kinds).length > 0);
        if (redraw) draw();
      }
      function update(index, patch, redraw) {
        commit(list.map(function (entry, i) { return i === index ? Object.assign({}, entry, patch) : entry; }), redraw);
      }
      function move(index, delta) {
        var target = index + delta;
        if (target < 0 || target >= list.length) return;
        var next = list.slice();
        var item = next.splice(index, 1)[0];
        next.splice(target, 0, item);
        commit(next, true);
      }
      function removeAt(index) {
        commit(list.filter(function (_entry, i) { return i !== index; }), true);
      }
      var rows = el("div", { class: "device-list" });
      if (!list.length) {
        rows.appendChild(el("p", { class: "muted", text: "No named devices: the house is one profile." }));
      }
      list.forEach(function (entry, index) {
        var row = el("div", { class: "device-row" });
        row.appendChild(loadDeviceKindSelect(entry.kind, kinds, function (kind) { update(index, { kind: kind }, true); }));
        var name = el("input", {
          type: "text",
          class: "device-name",
          placeholder: "display name (optional)",
          value: entry.name == null ? "" : String(entry.name)
        });
        // No redraw while typing: draw() rebuilds the form and the caret would be lost mid-name.
        name.addEventListener("input", function () { update(index, { name: name.value.trim() }, false); });
        row.appendChild(el("label", { class: "device-name-label" }, ["Name", name]));
        row.appendChild(entityPicker({
          kind: "load",
          key: "load.devices[" + index + "].entity",
          label: "device meter",
          placeholder: "device power or energy meter",
          value: entry.entity,
          onPick: function (_target, entityId) { update(index, { entity: entityId }, true); }
        }));
        row.appendChild(button("Move up", "", function () { move(index, -1); }));
        row.appendChild(button("Move down", "", function () { move(index, 1); }));
        row.appendChild(button("Remove", "", function () { removeAt(index); }));
        rows.appendChild(row);
      });
      wrap.appendChild(rows);
      var problems = loadDeviceProblems(list, kinds);
      if (problems.length) wrap.appendChild(el("p", { class: "field-help field-error", text: problems.join("; ") }));
      wrap.appendChild(button("Add device", "", function () {
        commit(list.concat([{ entity: "", kind: kinds[0] || "heat_pump" }]), true);
      }));
      return wrap;
    }

    function loadCard() {
      var children = [
        cardPicker("load.entity", "load", "household load power sensor name or entity id"),
        cardPicker("load.temperature_entity", "load", "outdoor temperature sensor name or entity id")
      ];
      // The device editor carries load.devices itself (like the Solar card owns solar.sources), so
      // the Load group skips the key and it appears exactly once.
      cardKeys["load.devices"] = true;
      children.push(loadDevicesEditor());
      return setupCard(
        "Load",
        "What the house draws, and any named device that deserves its own profile. The base profile is learned from the total minus the device meters, and every modelled device is added back above it; a device that cannot be measured stays inside the base and is reported in the forecast notes.",
        children
      );
    }

    function batteryCard() {
      // The canonical inventory is the /batteries endpoint, so a stale or empty descriptor can
      // never answer the question (UI_REVIEW §3.2 / open question 3).
      var list = fleet.map(function (record) { return record && record.config ? record.config : record; });
      var children = [];
      if (list.length) {
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
        el("a", { class: "btn btn-primary", href: "#/batteries", text: list.length ? "Manage batteries" : "Add a battery" }),
        " ",
        el("span", { class: "muted", text: "Batteries are searched, picked and confirmed in the Batteries view." })
      ]));
      return setupCard("Battery", "Which battery the plan may drive; the wizard finds the device and fills in the entities.", children);
    }

    // A rail count is how many keys differ from their default — a configuration fact, not a warning
    // and not the unsaved-edit count, which the save bar owns (UI_REVIEW §3.9).
    function renderRail(groups, active) {
      clear(railHost);
      railEntries = {};
      groups.forEach(function (group) {
        var count = el("span", { class: "badge badge-info", text: group.changedCount ? group.changedCount + " customised" : "defaults" });
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
      railHost.appendChild(el("p", { class: "legend-note", text: "Counts are keys that differ from their default; unsaved edits are counted in the save bar below." }));
    }
    function updateCounts(groups) {
      groups = groups || settingsGroups(mergedItems());
      groups.forEach(function (group) {
        var entry = railEntries[group.name];
        if (!entry) return;
        entry.count.textContent = group.changedCount ? group.changedCount + " customised" : "defaults";
      });
      saveNote.textContent = dirtyCount
        ? dirtyCount + " unsaved change" + (dirtyCount === 1 ? "" : "s") + " — not saved yet"
        : "No unsaved changes";
      saveButtons.forEach(function (b) { b.textContent = "Save changes (" + dirtyCount + ")"; b.disabled = dirtyCount === 0; });
    }

    // An always-visible safety summary: the authority in force right now, and direct links to the
    // Safety group and to Stop now (UI_REVIEW §3.2). Stop now lives on the Dashboard, not here.
    function authoritySummary() {
      var mode = settingValue("mode");
      var dry = settingValue("dry_run");
      var modeText = { off: "Off", advisory: "Advisory", auto: "Auto" }[mode] || String(mode || "unknown");
      var groups = settingsGroups(mergedItems());
      var hasSafety = groups.some(function (group) { return group.name === "Safety"; });
      return el("div", { class: "banner banner-safety" }, [
        el("p", { class: "banner-title", text: "Safety summary" }),
        el("p", {}, [
          "Control authority: ",
          el("strong", { text: modeText + (dry ? " · dry run on" : " · dry run off") }),
          " — " + authorityNote(!!dry)
        ]),
        el("p", { class: "banner-actions" }, [
          hasSafety ? (function () {
            var link = button("Review safety limits", "");
            link.addEventListener("click", function () {
              activeGroup = "Safety";
              writeStored(STORE_GROUP, activeGroup);
              if (search.value) search.value = "";
              if (drawer && !drawer.open) drawer.open = true;
              draw();
            });
            return link;
          })() : null,
          el("a", { class: "btn", href: "#/dashboard", text: "Stop now is on the Dashboard" })
        ])
      ]);
    }

    var search = el("input", { type: "search", id: "settings-search", placeholder: "filter settings by key, label or help", "aria-label": "Filter settings" });
    var showAllBox = el("input", { type: "checkbox", id: "settings-show-advanced", checked: showAllAdvanced });
    var railHost = el("nav", { class: "settings-rail", "aria-label": "Settings groups" });
    var listHost = el("div");
    var cardsHost = el("div", { class: "setup-cards" });
    // Feedback is always visible: the drawer may be closed, so pending/success/error and rejected
    // values never live inside it (UI_REVIEW §3.11).
    var feedback = el("div", { class: "settings-feedback" });
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
        [priceCard(), meterCard(), solarCard(), loadCard(), batteryCard()].forEach(function (card) {
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
      if (reject) { setNote(feedback, "There are invalid values; fix them before saving.", true); return; }
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
          renderPatchErrors(feedback, errors);
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
        }).then(function (ok) { if (ok) performWrite(saveButtons[0], feedback, requestFn, successText, onDone); });
      } else {
        performWrite(saveButtons[0], feedback, requestFn, successText, onDone);
      }
    }

    function renderPatchErrors(host, errors) {
      var box = el("div", { class: "banner banner-error" }, [el("p", { class: "banner-title", text: "Server rejected some values" })]);
      var list = el("ul");
      var focused = false;
      errors.forEach(function (error) {
        var text = typeof error === "string" ? error : (error.key ? error.key + ": " : "") + (error.message || "invalid value");
        list.appendChild(el("li", { text: text }));
        var key = typeof error === "string" ? null : error.key;
        if (!key) return;
        var field = listHost.querySelector('[data-key="' + key + '"]') || cardsHost.querySelector('[data-key="' + key + '"]');
        if (!field) return;
        var message = typeof error === "string" ? null : error.message;
        showFieldError(field, message || "The server rejected this value.");
        var input = field.querySelector("input, select, textarea");
        if (input && !focused) { input.focus(); focused = true; }
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
    var columns = el("div", { class: "settings-columns" }, [railHost, listHost]);
    var drawerBody = el("div", { class: "disclosure-body" }, [columns]);
    var drawer = el("details", { class: "disclosure all-settings" }, [
      el("summary", { text: "All settings (" + descriptors.length + ")" }),
      drawerBody
    ]);
    if (readStored(STORE_ALL_OPEN, false) === true) drawer.open = true;
    drawer.addEventListener("toggle", function () { writeStored(STORE_ALL_OPEN, drawer.open); });
    // Search and the advanced switch sit in the settings header, so filtering never requires
    // opening a drawer first (UI_REVIEW §3.2).
    body.appendChild(el("div", { class: "settings-toolbar" }, [
      el("label", { class: "field", for: "settings-search" }, [el("span", { text: "Search settings" }), search]),
      el("label", { class: "switch", for: "settings-show-advanced" }, [showAllBox, " Show all advanced"])
    ]));
    body.appendChild(authoritySummary());
    body.appendChild(cardsHost);
    body.appendChild(drawer);
    body.appendChild(feedback);
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
    body.appendChild(table("Add-on options", ["Option", "Value"], rows.length ? rows : [["(none)", ""]], { numeric: [] }));
  }

  function viewSettings(host) {
    var load = once(function () {
      return Promise.all([get("/config"), get("/batteries"), get("/providers").catch(function () { return null; })]).then(function (rows) {
        return { config: rows[0], fleet: (rows[1] && Array.isArray(rows[1].batteries) ? rows[1].batteries : []), providers: rows[2] };
      }, function (err) {
        // The config is the required read; without the fleet the Battery card says so rather than
        // guessing an empty inventory.
        return get("/config").then(function (config) {
          return { config: config, fleet: [], fleetError: err, providers: null };
        });
      });
    });
    routeTitle(host, "Settings", "Four setup cards for the choices that matter, then every key behind them.");
    asyncPanel(host, "Settings", { load: load, render: renderSettings });
    asyncPanel(host, "Add-on options", { load: load, render: renderOptions });
    return null;
  }

  /* ------------------------------------------------------------------ view: log */

  var LOG_LEVELS = ["", "info", "warning", "error"];
  // Presets answer the common question without typing a kind slug; the raw kind filter stays
  // for everything else (UI_REVIEW §5, per-view notes).
  var LOG_PRESETS = [
    { id: "all", text: "All", kind: "", level: "" },
    { id: "dispatch", text: "Dispatch", kind: "dispatch", level: "" },
    { id: "warnings", text: "Warnings and errors", kind: "", level: "warning" },
    { id: "events", text: "Events", kind: "event", level: "" }
  ];

  // Message first: what happened, then battery/time, requested → applied power, the plain-language
  // blocking reason and the exact code. Mode/dry-run context is promoted out of the JSON so an
  // "applied" row can never imply a real hardware write when it was a simulation (UI_REVIEW §5).
  function logRowMessage(entry) {
    var detail = entry.detail || {};
    var context = [];
    if (detail.mode) context.push("mode " + detail.mode);
    if (detail.dry_run === true) context.push("dry run");
    var requested = numOrNull(detail.requested_w);
    var applied = numOrNull(detail.applied_w);
    var power = requested == null && applied == null ? "" : [fmtPower(requested) + " requested", fmtPower(applied) + " applied"].join(" → ");
    return el("li", { dataset: { level: entry.level || "info" } }, [
      el("p", { class: "log-message", text: entry.message || entry.code || "(no message)" }),
      el("p", { class: "alert-meta" }, [
        badge(entry.level || "info", entry.level === "error" ? "err" : entry.level === "warning" ? "warn" : "info"),
        " ", fmtDateTime(entry.ts), " (", timeAgo(entry.ts), ")",
        detail.battery_id ? " · " + detail.battery_id : "",
        entry.kind ? " · " + entry.kind : "",
        entry.code ? " · code " + entry.code : "",
        power ? " · " + power : "",
        context.length ? " · " + context.join(" · ") : ""
      ]),
      detail && Object.keys(detail).length
        ? el("details", { class: "log-detail" }, [el("summary", { text: "detail" }), el("pre", { text: JSON.stringify(detail, null, 2) })])
        : null
    ]);
  }

  function renderLogEntries(host, entries) {
    clear(host);
    if (!entries.length) {
      host.appendChild(el("p", { class: "muted", text: "No matching entries. Clear the filters or widen the preset to see more." }));
      host.appendChild(el("p", { class: "legend-note", text: "If something surprising happened and every filter shows nothing, that is a bug worth reporting." }));
      return;
    }
    host.appendChild(el("ul", { class: "log-list" }, entries.map(logRowMessage)));
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
    var notice = el("p", { class: "legend-note", hidden: true });
    var timer = null;

    function reload(showLoading) {
      if (showLoading) panelStatus(entriesHost, "loading", "Loading log…");
      get("/log", { limit: filters.limit, level: filters.level, kind: filters.kind }).then(function (payload) {
        renderLogEntries(entriesHost, Array.isArray(payload.entries) ? payload.entries : []);
        notice.hidden = true;
      }).catch(function (err) {
        panelStatus(entriesHost, "error", "Could not load the log: " + (err && err.message ? err.message : err), function () { reload(true); });
      });
    }
    // Auto-refresh preserves what the reader is looking at: while a detail is open, or the reader
    // has focus inside the list, it offers a "new entries available" action instead of rebuilding
    // the evidence on screen (UI_REVIEW §3.5).
    function readingDetail() {
      return !!entriesHost.querySelector("details[open]");
    }
    function autoRefresh() {
      if (!auto.checked || document.hidden || anyDialogOpen()) return;
      if (readingDetail()) {
        notice.textContent = "New entries are available — Refresh to show them, or close the open detail to keep updating automatically.";
        notice.hidden = false;
        return;
      }
      reload(false);
    }
    function applyPreset(preset) {
      filters.kind = preset.kind;
      filters.level = preset.level;
      levelSelect.value = preset.level;
      kindInput.value = preset.kind;
      reload(true);
    }
    var presetButtons = LOG_PRESETS.map(function (preset) {
      var b = button(preset.text, preset.id === "all" ? "btn-primary" : "");
      b.setAttribute("aria-pressed", preset.id === "all" ? "true" : "false");
      b.addEventListener("click", function () {
        presetButtons.forEach(function (other) {
          other.classList.remove("btn-primary");
          other.setAttribute("aria-pressed", other === b ? "true" : "false");
        });
        b.classList.add("btn-primary");
        applyPreset(preset);
      });
      return b;
    });
    levelSelect.addEventListener("change", function () { filters.level = levelSelect.value; reload(true); });
    limitSelect.addEventListener("change", function () { filters.limit = limitSelect.value; reload(true); });
    kindInput.addEventListener("change", function () { filters.kind = kindInput.value.trim(); reload(true); });
    refresh.addEventListener("click", function () { reload(false); });

    p.body.appendChild(el("div", { class: "log-filters" }, [
      el("div", { class: "segmented", role: "group", "aria-label": "Log presets" }, presetButtons),
      el("label", { for: "log-level" }, [el("span", { class: "stat-label", text: "Level" }), levelSelect]),
      el("label", { for: "log-kind" }, [el("span", { class: "stat-label", text: "Kind" }), kindInput]),
      el("label", { for: "log-limit" }, [el("span", { class: "stat-label", text: "Rows" }), limitSelect]),
      el("label", { class: "switch", for: "log-auto" }, [auto, " auto-refresh"]),
      refresh
    ]));
    p.body.appendChild(el("p", { class: "legend-note", text: "Every dispatch attempt is logged with requested vs applied power and the blocking reason, so “why didn't it charge?” is always answerable here. An applied row in dry run was simulated: the dispatcher wrote nothing." }));
    p.body.appendChild(notice);
    p.body.appendChild(entriesHost);
    reload(true);
    timer = root.setInterval(autoRefresh, 15000);
    return function () { root.clearInterval(timer); };
  }

  /* ---- dashboard cards (SPEC 12) ---------------------------------------------------------
     The add-on's UI sits behind Supervisor ingress, so a Lovelace card in the browser cannot call
     its API. The card therefore reads the six `sensor.sem_chart_*` feeds the add-on publishes, and
     is installed by hand: the file is downloaded here, saved into /config/www and registered as a
     Lovelace resource. A bundle the add-on could register itself would need `map: config:rw`.
     The preview is drawn by the same charts.js the card carries, so what is shown here is what the
     dashboard renders. ---- */

  var CARD_KINDS = [
    { id: "plan", label: "Plan timeline", title: "Plan timeline" },
    { id: "mini", label: "Next 24 hours", title: "Next 24 hours" },
    { id: "price", label: "Price forecast", title: "Price forecast" },
    { id: "solar", label: "Solar forecast", title: "Solar forecast" },
    { id: "load", label: "Load forecast", title: "Load forecast" },
    { id: "savings", label: "Savings vs baseline", title: "Savings vs baseline", range: "day" },
    { id: "accuracy", label: "Forecast accuracy", title: "Forecast accuracy" }
  ];

  var CARD_FEEDS = [
    ["sensor.sem_chart_plan", "Plan timeline, ready or infeasible"],
    ["sensor.sem_chart_price", "Price forecast (buy, sell, stale)"],
    ["sensor.sem_chart_solar", "Solar forecast with its uncertainty"],
    ["sensor.sem_chart_load", "Load forecast with its p10–p90 band"],
    ["sensor.sem_chart_savings", "Savings, cost and baseline per bucket"],
    ["sensor.sem_chart_accuracy", "Daily forecast error per kind"]
  ];

  function cardLoad(kind) {
    if (kind === "plan" || kind === "mini") return get("/plan");
    if (kind === "savings") return get("/savings", { range: "day" });
    if (kind === "accuracy") return get("/accuracy", { days: 30 });
    return get("/forecast", { kind: kind, hours: 36 });
  }

  // The card's own mapper turns a feed row back into these shapes, so the preview and the card draw
  // the same series from the same numbers.
  function cardDraw(host, kind, payload) {
    var chart = el("div", { class: "chart chart-md" });
    host.appendChild(chart);
    if (kind === "plan") {
      var summary = payload && payload.summary ? payload.summary : {};
      var efficiency = planEfficiency(summary);
      root.SEMCharts.plan(chart, payload, {
        wear: isNum(summary.wear_eur_per_kwh) ? summary.wear_eur_per_kwh : 0,
        efficiency: efficiency
      });
    } else if (kind === "mini") {
      root.SEMCharts.mini(chart, payload);
    } else if (kind === "savings") {
      root.SEMCharts.savings(chart, (payload && payload.buckets) || []);
    } else if (kind === "accuracy") {
      root.SEMCharts.accuracy(chart, payload);
    } else {
      root.SEMCharts.forecast(chart, kind, payload);
    }
  }

  function cardSnippet(kind) {
    var lines = ["type: custom:sem-charts-card", "chart: " + kind.id, "title: " + kind.title];
    if (kind.range) lines.push("range: " + kind.range);
    return lines.join("\n");
  }

  function selectNode(node) {
    if (!root.getSelection || !document.createRange) return;
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = root.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function copyButton(node, label) {
    var b = button(label, "", function () {
      var clipboard = root.navigator && root.navigator.clipboard;
      if (clipboard && clipboard.writeText) {
        clipboard.writeText(node.textContent).then(
          function () { b.textContent = "Copied"; },
          function () { b.textContent = "Select and copy"; }
        );
        return;
      }
      // The clipboard API needs a secure context, and a LAN install over plain HTTP does not have
      // one: select the snippet instead of pretending to copy it.
      selectNode(node);
      b.textContent = "Press Ctrl+C";
    });
    return b;
  }

  function viewCards(host) {
    routeTitle(host, "Dashboard cards", "Put these charts on a Home Assistant dashboard without exposing the add-on's own API.");
    var kind = CARD_KINDS[0];
    var p = panel(host, "Add a chart to a dashboard", [
      el("a", {
        class: "btn btn-primary",
        href: apiPath("/web/sem-charts-card.js"),
        download: "sem-charts-card.js",
        text: "Download card"
      })
    ]);
    var snippet = el("pre", { class: "chart-card-snippet" });
    var preview = el("div", { class: "chart-card-body" });
    function paintSnippet() { snippet.textContent = cardSnippet(kind); }
    function paint() {
      paintSnippet();
      // This preview builds a fresh host per repaint, so release the previous chart instead of
      // stranding its canvas (charts.js only destroys the old chart when a host is reused).
      if (root.SEMCharts.destroyTree) root.SEMCharts.destroyTree(preview);
      panelStatus(preview, "loading", "Loading the " + kind.label + " chart…");
      cardLoad(kind.id).then(function (payload) {
        if (!preview.isConnected) return; // the reader moved on while this was in flight
        clear(preview);
        cardDraw(preview, kind.id, payload);
      }).catch(function (err) {
        if (!preview.isConnected) return;
        panelStatus(preview, "error", "Could not load the chart data: " + (err && err.message ? err.message : err), paint);
      });
    }
    var select = el("select", { id: "card-chart" }, CARD_KINDS.map(function (item) {
      return el("option", { value: item.id, text: item.label });
    }));
    select.addEventListener("change", function () {
      kind = CARD_KINDS.filter(function (item) { return item.id === select.value; })[0] || CARD_KINDS[0];
      paint();
    });
    p.body.appendChild(el("p", { class: "legend-note", text: "One card, seven charts. The file carries uPlot, the chart code and its styles, so the dashboard needs no other resource and the card never calls this add-on: it reads the sensor.sem_chart_* entities the add-on already publishes (SPEC 12)." }));
    p.body.appendChild(el("ol", { class: "chart-card-steps" }, [
      el("li", null, ["Download the card and save it in your Home Assistant config share as ", el("code", { text: "/config/www/sem-charts-card.js" }), ". Saving over an older copy is the whole update."]),
      el("li", null, ["In Home Assistant open ", el("strong", { text: "Settings → Dashboards → Resources" }), ", add ", el("code", { text: "/local/sem-charts-card.js?v=" + ((state.status && state.status.version) || "1") }), " and choose ", el("strong", { text: "JavaScript module" }), ". The ", el("code", { text: "?v=" }), " part matters: Home Assistant caches ", el("code", { text: "/local" }), " for 31 days, so bump it after you replace the file."]),
      el("li", null, ["Edit the dashboard, choose ", el("strong", { text: "Add card → Manual" }), ", and paste the snippet below. ", el("code", { text: "entity:" }), " and ", el("code", { text: "title:" }), " are optional overrides."])
    ]));
    p.body.appendChild(el("div", { class: "toolbar" }, [
      el("label", { for: "card-chart" }, [el("span", { class: "stat-label", text: "Chart" }), select]),
      copyButton(snippet, "Copy snippet")
    ]));
    p.body.appendChild(snippet);
    p.body.appendChild(el("p", { class: "legend-note", text: "Preview, drawn live from the data this card would read:" }));
    p.body.appendChild(preview);
    paint();

    var feeds = panel(host, "Feed entities");
    feeds.body.appendChild(table(
      "The add-on publishes one entity per chart. They are ordinary sensors: any dashboard card, template or automation can read them.",
      ["Entity", "What it carries"],
      CARD_FEEDS.map(function (row) { return [el("code", { text: row[0] }), row[1]]; }),
      { numeric: [] }
    ));
    feeds.body.appendChild(el("p", { class: "legend-note", text: "Every feed carries schema: 1 and refuses to draw if the shape changes; a card older or newer than the add-on says so instead of plotting the wrong column." }));
    return null;
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
    loadDeviceProblems: loadDeviceProblems,
    settingsGroups: settingsGroups,
    settingsVisible: settingsVisible,
    setupCardKeys: setupCardKeys,
    fmtPower: fmtPower,
    fmtKwh: fmtKwh,
    fmtPrice: fmtPrice,
    fmtEur: fmtEur,
    savingsCoverage: savingsCoverage,
    fmtHours: fmtHours,
    fmtPct: fmtPct,
    fmtDuration: fmtDuration,
    timeAgo: timeAgo,
    testDischargeOutcome: testDischargeOutcome,
    batteryEfficiency: batteryEfficiency,
    batteryEfficiencyPayload: batteryEfficiencyPayload,
    batteryWear: batteryWear,
    dispatchPathText: dispatchPathText,
    batteryNativeConnectionProblems: batteryNativeConnectionProblems,
    derivedWearEur: derivedWearEur,
    roundTripPct: roundTripPct,
    storedKwhCostEur: storedKwhCostEur,
    breakEvenSellEur: breakEvenSellEur,
    planEfficiency: planEfficiency,
    planBreakEvenEur: planBreakEvenEur,
    maintenancePlanText: maintenancePlanText,
    maintenanceText: maintenanceText,
    planSlotMinutes: planSlotMinutes,
    planActionWindows: planActionWindows,
    windowFacts: windowFacts,
    windowStatusText: windowStatusText,
    windowPriceText: windowPriceText,
    windowSummary: windowSummary,
    planWindowContext: planWindowContext,
    actionIcon: actionIcon,
    factIcon: factIcon,
    actionClass: actionClass,
    planHorizonBounds: planHorizonBounds,
    windowRail: windowRail,
    horizonPct: horizonPct,
    horizonDayTicks: horizonDayTicks,
    socTrack: socTrack,
    costBasisView: costBasisView,
    costBasisBreakdown: costBasisBreakdown,
    batteryPriceRowText: batteryPriceRowText,
    renderBatteryEnergyPrice: renderBatteryEnergyPrice,
    priceRank: priceRank,
    fmtCt: fmtCt,
    cycleCostFromConfig: cycleCostFromConfig,
    batteryGroupsFromCandidates: batteryGroupsFromCandidates,
    batteryGroupMatchesQuery: batteryGroupMatchesQuery,
    batteryRowMatchesQuery: batteryRowMatchesQuery,
    batterySearchGroups: batterySearchGroups,
    batterySlugFor: batterySlugFor,
    batterySelection: batterySelection,
    batteryIsZendureMqtt: batteryIsZendureMqtt,
    mqttZendureGroup: mqttZendureGroup,
    batteryDriverFor: batteryDriverFor,
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
