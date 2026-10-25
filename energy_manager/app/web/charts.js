/* charts.js — uPlot wrappers for the Smart Energy Manager charts.
 *
 * Shared by two consumers, which is why nothing here reads the document for anything but colours:
 *   1. the SPA in this directory (index.html loads it as `SEMCharts`), and
 *   2. the dashboard card bundle served at /api/v1/web/sem-charts-card.js, which sets an explicit
 *      palette with `SEMCharts.setPalette()` because it renders outside the SPA's token scope.
 *
 * Depends on the vendored IIFE global `uPlot` (web/vendor/uPlot.min.js, v1.6.32). No network, no
 * build step. Every public function either draws a chart into a host element or writes a
 * plain-text fallback into it; nothing throws for missing/partial data.
 *
 * Design (SPEC 13.1, 0.28.0): a chart is the same three flow siblings it always was - a plot box,
 * a static legend and a one-line caption - now with smooth splines, gradient area fills, a hover
 * readout, a subtle plot tint, day separators and a "now" marker. Colour alone still never carries
 * meaning: every dashed line and every bar keeps its text label.
 *
 * ponytail: the plan timeline stays two vertically aligned uPlot panels sharing one x range
 * (price+SoC, then PV/load+battery power) instead of one plot with four y scales; four axes are
 * unreadable at 360 px. Upgrade path: one plot with `scales: {y,y2,y3,y4}` if the layout ever gains
 * the width for it.
 */
(function (root) {
  "use strict";

  var registry = typeof WeakMap === "function" ? new WeakMap() : null;
  var watchers = []; // {host, draw} for re-rendering on colour-scheme change
  var BARS = null; // uPlot.paths.bars factory, created lazily
  var SPLINE = null; // uPlot.paths.spline factory, created lazily
  // uPlot's sync registry never forgets a key (its `dt` accessor only ever creates, never deletes),
  // so every distinct key a page has ever mounted keeps a hub object alive for the life of that page.
  // A key per draw would therefore grow for as long as the page is open - and the Lovelace card
  // redraws every time the add-on publishes. One key per chart family is also uPlot's own idiom: two
  // plan timelines on one page then share a crosshair, which is what aligned timelines want anyway.
  var SYNC_KEY = "sem-plan"; // the two plan panels (price/SoC and PV/load/battery) track together

  // Fallbacks when the CSS custom properties are missing (the dashboard card passes its own set).
  var DEFAULT_PALETTE = {
    price: "#e0a85a",
    sell: "#7fd1b9",
    pv: "#4cc38a",
    load: "#58a6ff",
    soc: "#a78bfa",
    charge: "#58a6ff",
    discharge: "#e0a85a",
    stale: "#6b7a8d",
    grid: "#2e3a49",
    axis: "#9fb0c3",
    breakEven: "#e08484",
    baseline: "#9fb0c3",
    cost: "#e0a85a",
    savings: "#4cc38a",
    accent: "#58a6ff"
  };

  // palette key -> the frozen `--chart-*` custom property charts.js reads (SPEC 13.1).
  var TOKEN = {
    price: "--chart-price",
    sell: "--chart-sell",
    pv: "--chart-pv",
    load: "--chart-load",
    soc: "--chart-soc",
    charge: "--chart-battery-charge",
    discharge: "--chart-battery-discharge",
    stale: "--chart-stale",
    grid: "--chart-grid",
    axis: "--chart-axis",
    breakEven: "--chart-break-even",
    baseline: "--chart-baseline",
    cost: "--chart-cost",
    savings: "--chart-savings",
    accent: "--accent"
  };

  var activePalette = null; // set by the dashboard card so charts render outside the SPA tokens

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (err) {
      return fallback;
    }
  }

  function palette(overrides) {
    var out = {};
    Object.keys(DEFAULT_PALETTE).forEach(function (key) {
      if (activePalette) out[key] = activePalette[key] || DEFAULT_PALETTE[key];
      else out[key] = cssVar(TOKEN[key], DEFAULT_PALETTE[key]);
    });
    if (overrides) {
      Object.keys(overrides).forEach(function (key) {
        if (overrides[key]) out[key] = overrides[key];
      });
    }
    return out;
  }

  function setPalette(next) {
    activePalette = next || null;
  }

  function withAlpha(color, alpha) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
    if (m) {
      var hex = m[1];
      if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      var n = parseInt(hex, 16);
      return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
    }
    if (/^rgba?\(/.test(color)) {
      return color.replace(/^rgba?\(([^)]+)\)$/, function (all, inner) {
        var parts = inner.split(",").slice(0, 3).join(",");
        return "rgba(" + parts + "," + alpha + ")";
      });
    }
    return color;
  }

  function ts(value) {
    if (value == null) return null;
    var d = value instanceof Date ? value : new Date(value);
    var t = d.getTime();
    return isFinite(t) ? t / 1000 : null;
  }

  function num(value) {
    return typeof value === "number" && isFinite(value) ? value : null;
  }

  function fmt(value, digits, unit) {
    if (value == null || !isFinite(value)) return "--";
    var text = Math.abs(value) >= 1000 ? value.toFixed(0) : value.toFixed(digits);
    return unit ? text + " " + unit : text;
  }

  function hasRows(rows) {
    return Array.isArray(rows) && rows.length > 0;
  }

  function fallback(host, message) {
    destroy(host);
    host.classList.add("chart-fallback");
    host.textContent = message;
    host.setAttribute("role", "note");
  }

  function chartsReady(host) {
    if (root.uPlot && typeof root.uPlot === "function" && root.uPlot.paths && root.uPlot.paths.bars) return true;
    fallback(host, "Chart library unavailable (vendored uPlot failed to load).");
    return false;
  }

  function destroy(host) {
    host.classList.remove("chart-fallback");
    if (!registry || !registry.has(host)) {
      host.textContent = "";
      return;
    }
    var entry = registry.get(host);
    registry.delete(host);
    if (entry.ro) entry.ro.disconnect();
    if (entry.onResize) root.removeEventListener("resize", entry.onResize);
    try { entry.u.destroy(); } catch (err) { /* already gone */ }
    host.textContent = "";
    for (var i = watchers.length - 1; i >= 0; i--) {
      if (watchers[i].host === host) watchers.splice(i, 1);
    }
  }

  function destroyTree(host) {
    watchers.slice().forEach(function (w) {
      if (w.host && (w.host === host || host.contains(w.host))) destroy(w.host);
    });
    destroy(host);
  }

  function resize(entry, host) {
    var width = Math.max(240, Math.floor(host.clientWidth || 320));
    if (width === entry.width) return;
    entry.width = width;
    try { entry.u.setSize({ width: width, height: entry.height }); } catch (err) { /* detached */ }
  }

  /* uPlot builds its legend as a <table> inside the plot root. With the plot pinned to an explicit
   * pixel height that table overflowed the box and painted over the paragraph below it (the mangled
   * chart on a phone). The plot, the legend and the caption are three flow siblings, and the hover
   * readout lives inside the plot box so it can never push the caption around. */
  function mount(host, opts, data, height, drawAgain, ariaLabel, caption) {
    destroy(host);
    if (!root.uPlot || typeof root.uPlot !== "function") {
      fallback(host, "Chart library unavailable (vendored uPlot failed to load).");
      return null;
    }
    if (!data || !data[0] || data[0].length === 0) {
      fallback(host, "No data to draw.");
      return null;
    }
    host.textContent = "";
    host.setAttribute("role", "img");
    if (ariaLabel) host.setAttribute("aria-label", ariaLabel);
    var plot = document.createElement("div");
    plot.className = "chart-plot";
    plot.style.height = height + "px";
    host.appendChild(plot);
    var legendBox = document.createElement("div");
    legendBox.className = "chart-legend";
    host.appendChild(legendBox);
    if (caption) {
      var cap = document.createElement("p");
      cap.className = "chart-caption";
      cap.textContent = caption;
      host.appendChild(cap);
    }
    var width = Math.max(240, Math.floor(host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 320));
    opts.width = width;
    opts.height = height;
    var baseLegend = opts.legend || {};
    opts.legend = {
      show: true,
      // Static: every figure is also in the tables beside the chart (All slots for the plan), so the
      // legend never implies a pointer readout uPlot would only draw when a series supplies `values`.
      // The hover readout is our own tooltip below, which also works where uPlot's live legend does
      // not (touch, series without a `value` function).
      live: false,
      markers: baseLegend.markers,
      mount: function (u, legendEl) {
        legendEl.classList.add("chart-legend-table");
        legendBox.appendChild(legendEl);
      }
    };
    var u;
    try {
      u = new root.uPlot(opts, data, plot);
    } catch (err) {
      fallback(host, "Chart failed to render: " + err.message);
      return null;
    }
    var entry = { u: u, height: height, width: width, ro: null, onResize: null };
    if (typeof root.ResizeObserver === "function") {
      entry.ro = new root.ResizeObserver(function () { resize(entry, host); });
      entry.ro.observe(host);
    } else {
      entry.onResize = function () { resize(entry, host); };
      root.addEventListener("resize", entry.onResize);
    }
    if (registry) registry.set(host, entry);
    if (typeof drawAgain === "function") watchers.push({ host: host, draw: drawAgain });
    return entry;
  }

  function barsPath() {
    if (!BARS) {
      if (!(root.uPlot && root.uPlot.paths && root.uPlot.paths.bars)) return undefined;
      BARS = root.uPlot.paths.bars({ size: [0.72, 1000] });
    }
    return BARS;
  }

  function splinePath() {
    // undefined when the stub/uPlot has no spline factory: uPlot falls back to straight segments.
    if (SPLINE === null) {
      SPLINE = (root.uPlot && root.uPlot.paths && root.uPlot.paths.spline)
        ? root.uPlot.paths.spline()
        : undefined;
    }
    return SPLINE;
  }

  function baseOpts(p, extra) {
    var opts = {
      scales: { x: { time: true } },
      legend: { show: true, live: false },
      cursor: { y: false, x: true, points: { show: true, size: 5 } },
      axes: [
        {
          stroke: p.axis, font: "11px system-ui, sans-serif", size: 44,
          grid: { show: true, stroke: p.grid, width: 1 },
          ticks: { stroke: p.grid, width: 1 },
          border: { stroke: p.grid, width: 1 }
        }
      ],
      series: [{}]
    };
    Object.keys(extra || {}).forEach(function (key) { opts[key] = extra[key]; });
    return opts;
  }

  // A chart host narrower than ~480 px (a phone in the HA sidebar) gets tighter axis gutters.
  function narrowHost(host) {
    var width = host && host.clientWidth ? host.clientWidth : (host.parentNode && host.parentNode.clientWidth) || 0;
    return width > 0 && width < 480;
  }

  /* One precision and one unit per axis kind, so two ticks never disagree with each other
   * ("75.00" beside "100"). A percent axis carries the unit in every tick, which also removes the
   * rotated axis label that collided with the ticks at 360 px. */
  var TICK_FMT = {
    price: function (v) { return v.toFixed(2); },
    eur: function (v) { return v.toFixed(2); },
    kw: function (v) { return v.toFixed(2); },
    kwh: function (v) { return v.toFixed(2); },
    pct: function (v) { return Math.round(v) + "%"; },
    pctError: function (v) { return Math.round(v) + "%"; }
  };

  function axisFor(scale, label, side, p, narrow, kind) {
    var fmtTick = TICK_FMT[kind] || function (v) { return String(v); };
    // A charge axis already carries "%" on every tick, so its rotated label would only repeat it.
    var showLabel = !!label && kind !== "pct";
    return {
      scale: scale, side: side, label: showLabel ? label : null,
      stroke: p.axis, font: narrow ? "10px system-ui, sans-serif" : "11px system-ui, sans-serif",
      labelFont: "10px system-ui, sans-serif", labelSize: showLabel ? (narrow ? 11 : 14) : 0,
      // A 360 px viewport cannot afford 58 px per y axis; the gutter shrinks, the units stay
      // (UI_REVIEW §3.10).
      size: narrow ? (showLabel ? 46 : 40) : 58,
      grid: scale === "y"
        ? { show: true, stroke: p.grid, width: 1, dash: [2, 4] }
        : { show: false },
      ticks: { stroke: p.grid, width: 1 },
      border: { stroke: p.grid, width: 1 },
      values: function (u, vals) {
        return vals.map(function (v) { return v == null ? "" : fmtTick(v); });
      }
    };
  }

  function timeAxis(p) {
    return {
      stroke: p.axis, font: "11px system-ui, sans-serif", size: 42,
      grid: { show: false },
      ticks: { stroke: p.grid, width: 1 },
      border: { stroke: p.grid, width: 1 }
    };
  }

  // ------------------------------------------------------------------------------------
  // Visual decorations: gradients, splines, plot tint, day/now markers and the hover readout
  // ------------------------------------------------------------------------------------

  /* A vertical gradient in plot coordinates. Tall price bars therefore read brighter than short
   * ones, and an area fill fades into the plot so the line stays the dominant edge. */
  function gradientFill(color, topAlpha, bottomAlpha) {
    return function (u) {
      try {
        var g = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
        g.addColorStop(0, withAlpha(color, topAlpha));
        g.addColorStop(1, withAlpha(color, bottomAlpha));
        return g;
      } catch (err) {
        return withAlpha(color, bottomAlpha);
      }
    };
  }

  function area(color, topAlpha, bottomAlpha) {
    return {
      stroke: color, width: 1.5, points: { show: false },
      paths: splinePath(),
      fill: gradientFill(color, topAlpha, bottomAlpha)
    };
  }

  /* Line signatures are the non-colour identification: a solid line is a measurement/forecast, a
   * long dash is a price series that may sit beside it, and a dotted line is a threshold
   * (UI_REVIEW §3.10). Colour alone never carries meaning. */
  var DASH = { long: [8, 4], dotted: [2, 3] };

  function line(color, dashed) {
    return {
      stroke: color, width: dashed ? 1.25 : 1.75, points: { show: false },
      paths: splinePath(),
      dash: dashed ? DASH.long : undefined
    };
  }

  function lineDashed(color) { return line(color, true); }

  // Thresholds (break-even, P10/P90 bounds) are not smoothed: a threshold that curves is a lie.
  function lineDotted(color) {
    return {
      stroke: color, width: 1, points: { show: false },
      dash: DASH.dotted
    };
  }

  function thresholdLine(color) {
    return {
      stroke: color, width: 1.5, points: { show: false },
      dash: DASH.dotted
    };
  }

  function bars(color, topAlpha, bottomAlpha) {
    return {
      paths: barsPath(), stroke: color, width: 0, points: { show: false },
      fill: gradientFill(color, topAlpha, bottomAlpha)
    };
  }

  function nextMidnight(moment) {
    moment.setDate(moment.getDate() + 1);
    moment.setHours(0, 0, 0, 0);
    return moment;
  }

  function drawDecor(u, decor) {
    if (!u.ctx || !u.bbox) return;
    var p = decor.palette || {};
    var ctx = u.ctx;
    var L = u.bbox.left, T = u.bbox.top, W = u.bbox.width, H = u.bbox.height;
    var xs = u.scales && u.scales.x;
    ctx.save();
    if (xs && xs.min != null && xs.max != null && xs.max > xs.min) {
      // Faint separator per local midnight; the time axis labels the day, the line just divides it.
      ctx.strokeStyle = withAlpha(p.axis || "#9fb0c3", 0.22);
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 4]);
      var cursor = new Date(xs.min * 1000);
      nextMidnight(cursor);
      for (var guard = 0; guard < 64 && cursor.getTime() / 1000 < xs.max; guard++) {
        var x = u.valToPos(cursor.getTime() / 1000, "x");
        if (x > L + 1 && x < L + W - 1) {
          ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + H); ctx.stroke();
        }
        nextMidnight(cursor);
      }
      if (decor.now != null) {
        var marker = u.valToPos(decor.now, "x");
        if (marker > L + 1 && marker < L + W - 1) {
          ctx.setLineDash([]);
          ctx.strokeStyle = withAlpha(p.accent || "#58a6ff", 0.85);
          ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.moveTo(marker, T); ctx.lineTo(marker, T + H); ctx.stroke();
        }
      }
    }
    ctx.restore();
  }

  function drawZero(u, decor) {
    if (!u.ctx || !u.bbox) return;
    var y = u.scales && u.scales.y;
    if (!y || y.min == null || y.max == null || y.min > 0 || y.max < 0) return;
    var p = decor.palette || {};
    var ctx = u.ctx;
    var yy = u.valToPos(0, "y");
    ctx.save();
    ctx.strokeStyle = withAlpha(p.axis || "#9fb0c3", 0.5);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(u.bbox.left, yy);
    ctx.lineTo(u.bbox.left + u.bbox.width, yy);
    ctx.stroke();
    ctx.restore();
  }

  /* Subtle accent tint behind the grid, drawn right after the canvas is cleared. It gives the plot
   * a surface without a bordered box (a border would eat 2 px off the canvas uPlot sizes itself). */
  function drawTint(u, decor) {
    if (!u.ctx || !u.bbox) return;
    var p = decor.palette || {};
    var ctx = u.ctx;
    var top = u.bbox.top, height = u.bbox.height;
    try {
      var g = ctx.createLinearGradient(0, top, 0, top + height);
      g.addColorStop(0, withAlpha(p.accent || "#58a6ff", 0.06));
      g.addColorStop(1, withAlpha(p.accent || "#58a6ff", 0));
      ctx.fillStyle = g;
      ctx.fillRect(u.bbox.left, top, u.bbox.width, height);
    } catch (err) { /* tint is cosmetic */ }
  }

  function fmtHoverTime(seconds, spanSeconds) {
    var d = new Date(seconds * 1000);
    try {
      if (spanSeconds != null && spanSeconds <= 26 * 3600) {
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
      return d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (err) {
      return d.toISOString();
    }
  }

  function onCursor(u) {
    var tip = u.__semTip;
    if (!tip) return;
    var idx = u.cursor && u.cursor.idx;
    if (!tip.over || idx == null || !u.data || !u.data[0]) { tip.box.hidden = true; return; }
    var rows = [];
    u.series.forEach(function (s, i) {
      if (i === 0 || s.show === false) return;
      var value = u.data[i] ? u.data[i][idx] : null;
      if (value == null) return;
      var text = typeof s.value === "function" ? s.value(u, value, i, idx) : String(value);
      var swatch = typeof s.stroke === "string" ? s.stroke : (typeof s.fill === "string" ? s.fill : "");
      rows.push({ label: s.label || "", color: swatch, text: text });
    });
    if (!rows.length) { tip.box.hidden = true; return; }
    tip.box.textContent = "";
    var head = document.createElement("p");
    head.className = "chart-tip-time";
    head.textContent = fmtHoverTime(u.data[0][idx], u.scales.x ? u.scales.x.max - u.scales.x.min : null);
    tip.box.appendChild(head);
    rows.forEach(function (row) {
      var el = document.createElement("p");
      el.className = "chart-tip-row";
      var swatch = document.createElement("span");
      swatch.className = "chart-tip-swatch";
      if (row.color) swatch.style.background = row.color;
      var name = document.createElement("span");
      name.className = "chart-tip-name";
      name.textContent = row.label;
      var val = document.createElement("span");
      val.className = "chart-tip-value";
      val.textContent = row.text;
      el.appendChild(swatch); el.appendChild(name); el.appendChild(val);
      tip.box.appendChild(el);
    });
    tip.box.hidden = false;
    var nearRight = u.cursor.left > u.bbox.width / 2;
    tip.box.style.left = Math.max(0, u.cursor.left) + "px";
    tip.box.style.top = Math.max(0, u.cursor.top == null ? 6 : u.cursor.top) + "px";
    tip.box.style.transform = nearRight ? "translate(-100%,0) translateX(-12px)" : "translate(12px,0)";
  }

  function decorPlugin(decor) {
    return {
      hooks: {
        drawClear: function (u) { drawTint(u, decor); },
        draw: function (u) { drawDecor(u, decor); if (decor.zeroLine) drawZero(u, decor); }
      }
    };
  }

  function tooltipPlugin() {
    return {
      hooks: {
        ready: function (u) {
          try {
            if (!u.over || !u.over.parentNode || !document.createElement) return;
            var box = document.createElement("div");
            box.className = "chart-tip";
            box.hidden = true;
            u.over.parentNode.appendChild(box);
            var state = { box: box, over: false };
            u.__semTip = state;
            u.over.addEventListener("pointerenter", function () { state.over = true; });
            u.over.addEventListener("pointerleave", function () { state.over = false; box.hidden = true; });
          } catch (err) { /* cosmetic */ }
        },
        setCursor: onCursor
      }
    };
  }

  // ------------------------------------------------------------------------------------
  // Public charts
  // ------------------------------------------------------------------------------------

  function planChart(host, plan, options) {
    var slots = plan && Array.isArray(plan.slots) ? plan.slots : [];
    if (!hasRows(slots)) { fallback(host, "No plan available yet."); return; }
    if (!chartsReady(host)) return;
    destroyTree(host);
    host.textContent = "";
    var p = palette(options && options.palette);
    options = options || {};
    var wear = num(options.wear) != null ? num(options.wear) : 0;
    var eff = options.efficiency;
    var showBreakEven = !!(eff && eff.round_trip > 0);
    var now = Date.now() / 1000;
    var xs = [], buy = [], sell = [], soc = [], pv = [], load = [], charge = [], discharge = [], staleBuy = [], breakEven = [];
    slots.forEach(function (s) {
      xs.push(ts(s.t));
      buy.push(num(s.buy));
      sell.push(num(s.sell));
      soc.push(num(s.soc_pct));
      pv.push(num(s.pv_kw));
      load.push(num(s.load_kw));
      var power = num(s.power_w);
      charge.push(power != null && power > 0 ? power / 1000 : null);
      discharge.push(power != null && power < 0 ? power / 1000 : null);
      staleBuy.push(s.price_stale ? num(s.buy) : null);
      var slotBuy = num(s.buy);
      breakEven.push(showBreakEven && slotBuy != null ? slotBuy / eff.round_trip + wear : null);
    });
    // 0.34.2 (SPEC 8.4): a zero-export self-consumption plan carries no tariff at all. Drawing an
    // empty price axis (or a 0 EUR/kWh bar) would claim a price that does not exist, so the price
    // series and its axis are left out entirely and the caption says why.
    var priced = buy.some(function (value) { return value != null; });

    var unitFmt = function (digits, unit) {
      return function (u, v) { return fmt(v, digits, unit); };
    };

    var seriesA = [
      {},
      Object.assign(line(p.price), {
        label: "Buy price", scale: "y", paths: barsPath(),
        fill: gradientFill(p.price, 0.95, 0.45), value: unitFmt(4, "EUR/kWh")
      }),
      {
        label: "Stale price slot", scale: "y", paths: barsPath(), stroke: p.stale,
        fill: withAlpha(p.stale, 0.9), width: 0, points: { show: false },
        value: function (u, v) { return v == null ? "fresh" : "stale"; }
      },
      Object.assign(line(p.sell, true), { label: "Sell price (long dashes)", scale: "y", value: unitFmt(4, "EUR/kWh") })
    ];
    var dataA = [xs, buy, staleBuy, sell];
    if (showBreakEven) {
      seriesA.push(Object.assign(thresholdLine(p.breakEven), {
        label: "Break-even (dotted)", scale: "y",
        value: unitFmt(4, "EUR/kWh")
      }));
      dataA.push(breakEven);
    }
    seriesA.push(Object.assign(line(p.soc), { label: "Charge level", scale: "soc", value: unitFmt(1, "%") }));
    dataA.push(soc);
    if (!priced) {
      seriesA = [seriesA[0], seriesA[seriesA.length - 1]];
      dataA = [xs, soc];
    }

    var narrow = narrowHost(host);
    var optsA = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true }, soc: { auto: true } },
      axes: priced
        ? [timeAxis(p), axisFor("y", "EUR/kWh", 3, p, narrow, "price"), axisFor("soc", "%", 1, p, narrow, "pct")]
        : [timeAxis(p), axisFor("soc", "%", 1, p, narrow, "pct")],
      series: seriesA,
      cursor: { y: false, x: true, points: { show: true, size: 5 }, sync: { key: SYNC_KEY, setSeries: false } },
      plugins: [decorPlugin({ palette: p, now: now }), tooltipPlugin()]
    });

    var drawA = function (el) {
      mount(el, optsA, dataA, 200, drawA,
        priced
          ? "Plan timeline: buy and sell price, the break-even price after round-trip loss, and battery charge over the horizon."
          : "Plan timeline without prices: battery charge over the horizon for a zero-export self-consumption plan (no price forecast).",
        priced
          ? "Price in EUR/kWh (left axis) and battery charge in % (right axis). The dotted line is the break-even after round-trip loss; below it, discharging loses money."
          : "Battery charge in %. No price forecast is available, so this plan is not priced: the battery catches the PV surplus and serves the load down to its floor.");
    };
    drawA(host);

    var optsB = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "kW", 3, p, narrow, "kw")],
      series: [
        {},
        Object.assign(area(p.pv, 0.34, 0.02), { label: "PV forecast", value: unitFmt(2, "kW") }),
        Object.assign(area(p.load, 0.28, 0.02), { label: "Load forecast", value: unitFmt(2, "kW") }),
        Object.assign(bars(p.charge, 0.9, 0.4), { label: "Battery charging", value: unitFmt(2, "kW") }),
        Object.assign(bars(p.discharge, 0.9, 0.4), { label: "Battery discharging", value: unitFmt(2, "kW") })
      ],
      cursor: { y: false, x: true, points: { show: true, size: 5 }, sync: { key: SYNC_KEY, setSeries: false } },
      plugins: [decorPlugin({ palette: p, now: now, zeroLine: true }), tooltipPlugin()]
    });
    var hostB = document.createElement("div");
    hostB.className = "chart";
    host.appendChild(hostB);
    var drawB = function (el) {
      mount(el, optsB, [xs, pv, load, charge, discharge], 175, drawB,
        "Plan timeline: PV and load forecast in kW and battery charging or discharging in kW.",
        "PV and load forecast in kW, with battery charging (+) and discharging (−) in kW.");
    };
    drawB(hostB);

  }

  function miniChart(host, plan, options) {
    var slots = plan && Array.isArray(plan.slots) ? plan.slots.filter(function (s) { return num(s.soc_pct) != null || num(s.soc_kwh) != null || num(s.buy) != null; }) : [];
    if (!hasRows(slots)) { fallback(host, "No 24 h data yet."); return; }
    if (!chartsReady(host)) return;
    var p = palette(options && options.palette);
    var xs = [], price = [], soc = [];
    slots.forEach(function (s) {
      xs.push(ts(s.t));
      price.push(num(s.buy));
      soc.push(num(s.soc_pct));
    });
    // No tariff anywhere in the horizon (0.34.2): draw the charge level alone rather than an empty
    // price axis (SPEC 8.4).
    var priced = price.some(function (value) { return value != null; });
    var narrow = narrowHost(host);
    var series = priced
      ? [
          {},
          Object.assign(line(p.price), { label: "Buy price", scale: "y", fill: gradientFill(p.price, 0.22, 0.02), value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } }),
          Object.assign(area(p.soc, 0.3, 0.02), { label: "Charge level", scale: "soc", value: function (u, v) { return fmt(v, 1, "%"); } })
        ]
      : [
          {},
          Object.assign(area(p.soc, 0.3, 0.02), { label: "Charge level", scale: "soc", value: function (u, v) { return fmt(v, 1, "%"); } })
        ];
    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true }, soc: { auto: true } },
      axes: priced
        ? [timeAxis(p), axisFor("y", "EUR/kWh", 3, p, narrow, "price"), axisFor("soc", "%", 1, p, narrow, "pct")]
        : [timeAxis(p), axisFor("soc", "%", 1, p, narrow, "pct")],
      series: series,
      plugins: [decorPlugin({ palette: p, now: Date.now() / 1000 }), tooltipPlugin()]
    });
    var draw = function (el) {
      mount(el, opts, priced ? [xs, price, soc] : [xs, soc], 150, draw,
        priced
          ? "Next 24 hours: energy buy price in EUR per kWh and battery charge in percent."
          : "Next 24 hours: battery charge in percent (no price forecast).",
        priced
          ? "Buy price in EUR/kWh (left axis) and battery charge in % (right axis)."
          : "Battery charge in %. No price forecast is available, so no price is drawn.");
    };
    draw(host);
  }

  function forecastChart(host, kind, payload, options) {
    var slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
    if (!hasRows(slots)) {
      // An empty price feed is a fact, not a rendering gap (0.34.2): say so, so a missing tariff
      // reads as "the provider has not resolved" instead of "the app is broken" (SPEC 8.4).
      fallback(host, kind === "price"
        ? "No price forecast: no tariff provider resolved for this horizon."
        : "No forecast slots returned.");
      return;
    }
    if (!chartsReady(host)) return;
    var p = palette(options && options.palette);
    var now = Date.now() / 1000;
    var xs = slots.map(function (s) { return ts(s.t); });
    var draw, opts, data;

    if (kind === "price") {
      var buy = slots.map(function (s) { return num(s.buy); });
      var sell = slots.map(function (s) { return num(s.sell); });
      var stale = slots.map(function (s) { return s.stale ? num(s.buy) : null; });
      var staleAny = stale.some(function (v) { return v != null; });
      var narrowP = narrowHost(host);
      opts = baseOpts(p, {
        scales: { x: { time: true }, y: { auto: true } },
        axes: [timeAxis(p), axisFor("y", "EUR/kWh", 3, p, narrowP, "price")],
        series: [
          {},
          Object.assign(line(p.price), { label: "Buy price", paths: barsPath(), fill: gradientFill(p.price, 0.95, 0.4), value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } }),
          { label: "Stale price slot", paths: barsPath(), stroke: p.stale, fill: withAlpha(p.stale, 0.9), width: 0, points: { show: false }, value: function (u, v) { return v == null ? "fresh" : "stale"; } },
          Object.assign(line(p.sell, true), { label: "Sell price (long dashes)", value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } })
        ],
        plugins: [decorPlugin({ palette: p, now: now }), tooltipPlugin()]
      });
      data = [xs, buy, stale, sell];
      draw = function (el) {
        mount(el, opts, data, 185, draw,
          "Price forecast: buy and sell price per slot, grey bars mark stale slots.",
          "Buy price per slot in EUR/kWh (bars) and the sell price (long dashes). Grey bars are stale slots.");
      };
      if (!staleAny) opts.series[2].show = false;
    } else if (kind === "solar") {
      var pv = slots.map(function (s) { return num(s.pv_kw); });
      var lo = [], hi = [];
      slots.forEach(function (s, i) {
        var u = num(s.uncertainty_pct);
        var v = pv[i];
        var spread = v != null ? Math.abs(v) * ((u == null ? 25 : u) / 100) : null;
        lo.push(spread == null ? null : Math.max(0, v - spread));
        hi.push(spread == null ? null : v + spread);
      });
      opts = baseOpts(p, {
        scales: { x: { time: true }, y: { auto: true } },
        axes: [timeAxis(p), axisFor("y", "kW", 3, p, narrowHost(host), "kw")],
        bands: [{ series: [2, 3], fill: withAlpha(p.pv, 0.14), dir: -1 }],
        series: [
          {},
          Object.assign(area(p.pv, 0.32, 0.02), { label: "PV forecast (solid)", value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(lineDashed(p.pv), { label: "Upper uncertainty (long dashes)", stroke: withAlpha(p.pv, 0.55), value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(lineDotted(p.pv), { label: "Lower uncertainty (dotted)", stroke: withAlpha(p.pv, 0.55), value: function (u, v) { return fmt(v, 2, "kW"); } })
        ],
        plugins: [decorPlugin({ palette: p, now: now, zeroLine: true }), tooltipPlugin()]
      });
      data = [xs, pv, hi, lo];
      draw = function (el) {
        mount(el, opts, data, 185, draw,
          "Solar forecast in kW with an uncertainty band around it.",
          "PV forecast in kW: the solid line is the expected output; the band is its uncertainty (long dashes = upper, dotted = lower).");
      };
    } else {
      var p50 = slots.map(function (s) { return num(s.load_kw); });
      var p10 = slots.map(function (s) { return num(s.p10_kw); });
      var p90 = slots.map(function (s) { return num(s.p90_kw); });
      opts = baseOpts(p, {
        scales: { x: { time: true }, y: { auto: true } },
        axes: [timeAxis(p), axisFor("y", "kW", 3, p, narrowHost(host), "kw")],
        bands: [{ series: [2, 3], fill: withAlpha(p.load, 0.14), dir: -1 }],
        series: [
          {},
          Object.assign(area(p.load, 0.3, 0.02), { label: "Load P50 (solid)", value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(lineDashed(p.load), { label: "Load P90 (long dashes)", stroke: withAlpha(p.load, 0.55), value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(lineDotted(p.load), { label: "Load P10 (dotted)", stroke: withAlpha(p.load, 0.55), value: function (u, v) { return fmt(v, 2, "kW"); } })
        ],
        plugins: [decorPlugin({ palette: p, now: now, zeroLine: true }), tooltipPlugin()]
      });
      data = [xs, p50, p90, p10];
      draw = function (el) {
        mount(el, opts, data, 185, draw,
          "Load forecast in kW: P50 median line with a P10 to P90 band.",
          "Load forecast in kW: P50 solid, P90 long-dashed, P10 dotted.");
      };
    }
    draw(host);
  }

  function forecastHistoryChart(host, payload, options) {
    var rows = payload && Array.isArray(payload.series) ? payload.series : [];
    if (!hasRows(rows)) { fallback(host, "No forecast history intervals for this range."); return; }
    var xs = rows.map(function (row) { return ts(row.start); });
    var forecast = rows.map(function (row) {
      return row.forecast_complete ? num(row.forecast_kwh) : null;
    });
    var actual = rows.map(function (row) {
      return row.actual_complete ? num(row.actual_kwh) : null;
    });
    if (!forecast.concat(actual).some(function (value) { return value !== null; })) {
      fallback(host, "No complete forecast or measured-PV intervals to plot; see the table for coverage.");
      return;
    }
    if (!chartsReady(host)) return;
    var p = palette(options && options.palette);
    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "kWh", 3, p, narrowHost(host), "kwh")],
      series: [
        {},
        Object.assign(line(p.pv), {
          label: "Forecast energy (complete coverage)",
          value: function (u, v) { return fmt(v, 2, "kWh"); }
        }),
        Object.assign(lineDashed(p.load), {
          label: "Measured PV energy (complete coverage, dashed)",
          value: function (u, v) { return fmt(v, 2, "kWh"); }
        })
      ],
      plugins: [decorPlugin({ palette: p, zeroLine: true }), tooltipPlugin()]
    });
    var draw = function (el) {
      mount(el, opts, [xs, forecast, actual], 185, draw,
        "Forecast and direct measured PV energy per interval in kWh; incomplete intervals are gaps.",
        "Forecast energy (solid) and measured PV energy (long dashes), in kWh. Incomplete coverage is not plotted.");
    };
    draw(host);
  }

  function savingsChart(host, buckets, options) {
    if (!hasRows(buckets)) { fallback(host, "No savings buckets for this range yet."); return; }
    if (!chartsReady(host)) return;
    var p = palette(options && options.palette);
    var xs = [], savings = [], cost = [], baseline = [], keys = [];
    var allTimed = true;
    buckets.forEach(function (b, i) {
      var t = ts(b.start);
      if (t == null) allTimed = false;
      xs.push(t == null ? i : t);
      keys.push(typeof b.key === "string" ? b.key : String(i + 1));
      savings.push(num(b.savings_eur));
      cost.push(num(b.cost_eur) != null ? num(b.cost_eur) - (num(b.revenue_eur) || 0) : null);
      baseline.push(num(b.baseline_cost_eur));
    });
    var xAxis = timeAxis(p);
    var anyValue = savings.concat(cost, baseline).some(function (value) { return value !== null; });
    if (!anyValue) {
      fallback(host, "No baseline or savings to plot for this range: the server reports null without a load forecast (unknown is not zero).");
      return;
    }
    if (!allTimed) {
      xAxis = { stroke: p.axis, font: "11px system-ui, sans-serif", size: 42,
        grid: { show: false }, ticks: { stroke: p.grid, width: 1 },
        values: function (u, vals) { return vals.map(function (v) { return keys[v] || ""; }); } };
    }
    var narrow = narrowHost(host);
    var opts = baseOpts(p, {
      scales: { x: { time: allTimed }, savings: { auto: true }, cost: { auto: true } },
      axes: [xAxis, axisFor("savings", "EUR saved", 3, p, narrow, "eur"), axisFor("cost", "EUR cost", 1, p, narrow, "eur")],
      series: [
        {},
        Object.assign(bars(p.savings, 0.95, 0.4), { label: "Savings", scale: "savings", value: function (u, v) { return fmt(v, 2, "EUR"); } }),
        Object.assign(line(p.cost), { label: "Actual net cost (solid)", scale: "cost", value: function (u, v) { return fmt(v, 2, "EUR"); } }),
        Object.assign(line(p.baseline, true), { label: "Baseline cost, no battery (long dashes)", scale: "cost", value: function (u, v) { return fmt(v, 2, "EUR"); } })
      ],
      plugins: [decorPlugin({ palette: p, zeroLine: true }), tooltipPlugin()]
    });
    var draw = function (el) {
      mount(el, opts, [xs, savings, cost, baseline], 185, draw,
        "Savings per period in EUR, with actual net cost and no-battery baseline cost lines in EUR.",
        "Savings per period in EUR (left axis); actual net cost (solid) and the no-battery baseline (long-dashed) are both in EUR on the right axis.");
    };
    draw(host);
  }

  function accuracyChart(host, reports, options) {
    var kinds = ["load", "price"].filter(function (k) {
      return reports && reports[k] && Array.isArray(reports[k].series) && reports[k].series.length > 0;
    });
    if (!kinds.length) { fallback(host, "No load/price accuracy series yet (needs at least one day of data)."); return; }
    if (!chartsReady(host)) return;
    var p = palette(options && options.palette);
    var load = reports.load, price = reports.price;
    var dayIndex = {};
    var xs = [];
    var byKind = { load: load, price: price };
    kinds.forEach(function (k) {
      byKind[k].series.forEach(function (d) {
        var t = ts(d.day);
        if (t == null) return;
        var key = String(t);
        if (!(key in dayIndex)) { dayIndex[key] = xs.length; xs.push(t); }
      });
    });
    xs.sort(function (a, b) { return a - b; });
    var indexOf = {};
    xs.forEach(function (t, i) { indexOf[String(t)] = i; });
    function seriesFor(k) {
      var out = new Array(xs.length).fill(null);
      byKind[k].series.forEach(function (d) {
        var t = ts(d.day);
        if (t == null) return;
        var i = indexOf[String(t)];
        if (i != null) out[i] = num(d.mape_pct);
      });
      return out;
    }
    var colors = { load: p.load, price: p.price };
    var signatures = { load: lineDashed, price: lineDotted };
    var labels = {
      load: "Load error (long dashes)",
      price: "Price error (dotted)"
    };
    var narrow = narrowHost(host);
    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "% error (MAPE)", 3, p, narrow, "pctError")],
      series: [{}].concat(kinds.map(function (k) {
        return Object.assign(signatures[k](colors[k]), {
          label: labels[k],
          value: function (u, v) { return fmt(v, 1, "%"); }
        });
      })),
      plugins: [decorPlugin({ palette: p, zeroLine: true }), tooltipPlugin()]
    });
    var data = [xs].concat(kinds.map(seriesFor));
    var draw = function (el) {
      mount(el, opts, data, 175, draw,
        "Forecast accuracy trend: daily MAPE for load and price; PV uses the archived energy scorecard.",
        "Mean absolute percentage error per day (%) for load and price, lower is better. PV is scored in kWh on the Forecasts view.");
    };
    draw(host);
  }

  function refresh() {
    watchers = watchers.filter(function (w) {
      if (!w.host || !w.host.isConnected) return false;
      w.draw(w.host);
      return true;
    });
  }

  if (root.matchMedia) {
    try {
      var mq = root.matchMedia("(prefers-color-scheme: light)");
      var onChange = function () { refresh(); };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (err) { /* colour change is cosmetic */ }
  }

  root.SEMCharts = {
    plan: planChart,
    mini: miniChart,
    forecast: forecastChart,
    forecastHistory: forecastHistoryChart,
    savings: savingsChart,
    accuracy: accuracyChart,
    destroy: destroy,
    destroyTree: destroyTree,
    refresh: refresh,
    setPalette: setPalette,
    palette: palette
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
