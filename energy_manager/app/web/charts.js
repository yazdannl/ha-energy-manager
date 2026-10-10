/* charts.js — uPlot wrappers for the Smart Energy Manager SPA.
 *
 * Depends on the vendored IIFE global `uPlot` (web/vendor/uPlot.min.js, v1.6.32).
 * No network, no build step. Every public function either draws a chart into a host element or
 * writes a plain-text fallback into it; nothing throws for missing/partial data.
 *
 * ponytail: the plan timeline is drawn as two vertically aligned uPlot panels sharing one x range
 * (price+SoC, then PV/load+battery power) instead of one plot with four y scales; four axes are
 * unreadable at 360 px. Upgrade path: one plot with `scales: {y,y2,y3,y4}` if the layout ever
 * gains the width for it.
 */
(function (root) {
  "use strict";

  var registry = typeof WeakMap === "function" ? new WeakMap() : null;
  var watchers = []; // {host, draw} for re-rendering on colour-scheme change
  var BARS = null; // uPlot.paths.bars factory, created lazily

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (err) {
      return fallback;
    }
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

  function palette() {
    return {
      price: cssVar("--chart-price", "#e0a85a"),
      sell: cssVar("--chart-sell", "#7fd1b9"),
      pv: cssVar("--chart-pv", "#4cc38a"),
      load: cssVar("--chart-load", "#58a6ff"),
      soc: cssVar("--chart-soc", "#a78bfa"),
      charge: cssVar("--chart-battery-charge", "#58a6ff"),
      discharge: cssVar("--chart-battery-discharge", "#e0a85a"),
      stale: cssVar("--chart-stale", "#6b7a8d"),
      grid: cssVar("--chart-grid", "#2e3a49"),
      axis: cssVar("--chart-axis", "#9fb0c3"),
      breakEven: cssVar("--chart-break-even", "#e08484"),
      baseline: cssVar("--chart-baseline", "#9fb0c3"),
      cost: cssVar("--chart-cost", "#e0a85a"),
      savings: cssVar("--chart-savings", "#4cc38a")
    };
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

  function mount(host, opts, data, height, drawAgain, ariaLabel) {
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
    host.style.height = height + "px";
    var width = Math.max(240, Math.floor(host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 320));
    opts.width = width;
    opts.height = height;
    var u;
    try {
      u = new root.uPlot(opts, data, host);
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

  function baseOpts(p, extra) {
    var opts = {
      scales: { x: { time: true } },
      legend: { show: true, live: true },
      cursor: { y: false, points: { show: false } },
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

  function axisFor(scale, label, side, p) {
    return {
      scale: scale, side: side, label: label,
      stroke: p.axis, font: "11px system-ui, sans-serif",
      labelFont: "10px system-ui, sans-serif", labelSize: label ? 14 : 0,
      size: 58,
      grid: scale === "y" ? { show: true, stroke: p.grid, width: 1 } : { show: false },
      ticks: { stroke: p.grid, width: 1 },
      border: { stroke: p.grid, width: 1 },
      values: function (u, vals) {
        return vals.map(function (v) { return v == null ? "" : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)); });
      }
    };
  }

  function timeAxis(p) {
    return {
      stroke: p.axis, font: "11px system-ui, sans-serif", size: 42,
      grid: { show: true, stroke: p.grid, width: 1 },
      ticks: { stroke: p.grid, width: 1 },
      border: { stroke: p.grid, width: 1 }
    };
  }

  function area(color, alpha) {
    return { stroke: color, fill: withAlpha(color, alpha), width: 1.5, points: { show: false } };
  }

  function line(color, dashed) {
    return {
      stroke: color, width: dashed ? 1 : 1.5, points: { show: false },
      dash: dashed ? [5, 4] : undefined
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
    var p = palette();
    options = options || {};
    var wear = num(options.wear) != null ? num(options.wear) : 0;
    var eff = options.efficiency;
    var showBreakEven = !!(eff && eff.round_trip > 0);
    var xs = [], buy = [], sell = [], soc = [], pv = [], load = [], charge = [], discharge = [], staleBuy = [], breakEven = [];
    var staleCount = 0;
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
      if (s.price_stale) staleCount++;
      var slotBuy = num(s.buy);
      breakEven.push(showBreakEven && slotBuy != null ? slotBuy / eff.round_trip + wear : null);
    });

    var unitFmt = function (digits, unit) {
      return function (u, v) { return fmt(v, digits, unit); };
    };

    var seriesA = [
      {},
      Object.assign(line(p.price), {
        label: "Buy price", scale: "y", paths: barsPath(),
        fill: withAlpha(p.price, 0.85), value: unitFmt(4, "EUR/kWh")
      }),
      {
        label: "Stale price (grey)", scale: "y", paths: barsPath(), stroke: p.stale,
        fill: withAlpha(p.stale, 0.9), width: 0, points: { show: false },
        value: function (u, v) { return v == null ? "not stale" : "stale"; }
      },
      Object.assign(line(p.sell, true), { label: "Sell price", scale: "y", value: unitFmt(4, "EUR/kWh") })
    ];
    var dataA = [xs, buy, staleBuy, sell];
    if (showBreakEven) {
      seriesA.push(Object.assign(line(p.breakEven, true), {
        label: "break-even after round-trip loss", scale: "y",
        value: unitFmt(4, "EUR/kWh")
      }));
      dataA.push(breakEven);
    }
    seriesA.push(Object.assign(line(p.soc), { label: "SoC", scale: "soc", value: unitFmt(1, "%") }));
    dataA.push(soc);

    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true }, soc: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "EUR/kWh", 3, p), axisFor("soc", "%", 1, p)],
      series: seriesA
    });

    var drawA = function (el) { mount(el, opts, dataA, 190, drawA, "Plan timeline: buy and sell price, the break-even price after round-trip loss, and battery state of charge over the horizon."); };
    drawA(host);

    var optsB = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "kW", 3, p)],
      series: [
        {},
        Object.assign(area(p.pv, 0.22), { label: "PV forecast", value: unitFmt(2, "kW") }),
        Object.assign(area(p.load, 0.18), { label: "Load forecast", value: unitFmt(2, "kW") }),
        { label: "Battery charging", stroke: p.charge, fill: withAlpha(p.charge, 0.85), width: 0, paths: barsPath(), points: { show: false }, value: unitFmt(2, "kW") },
        { label: "Battery discharging", stroke: p.discharge, fill: withAlpha(p.discharge, 0.85), width: 0, paths: barsPath(), points: { show: false }, value: unitFmt(2, "kW") }
      ]
    });
    var hostB = document.createElement("div");
    hostB.className = "chart";
    host.appendChild(hostB);
    var drawB = function (el) { mount(el, optsB, [xs, pv, load, charge, discharge], 165, drawB, "Plan timeline: PV and load forecast in kW and battery charging or discharging in kW."); };
    drawB(hostB);

    if (staleCount > 0) {
      var note = document.createElement("p");
      note.className = "legend-note";
      note.textContent = staleCount + " of " + slots.length + " price slots are stale (grey bars); planning treats them as flat.";
      host.appendChild(note);
    }
  }

  function miniChart(host, plan) {
    var slots = plan && Array.isArray(plan.slots) ? plan.slots.filter(function (s) { return num(s.soc_pct) != null || num(s.soc_kwh) != null || num(s.buy) != null; }) : [];
    if (!hasRows(slots)) { fallback(host, "No 24 h data yet."); return; }
    if (!chartsReady(host)) return;
    var p = palette();
    var xs = [], price = [], soc = [];
    slots.forEach(function (s) {
      xs.push(ts(s.t));
      price.push(num(s.buy));
      soc.push(num(s.soc_pct));
    });
    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true }, soc: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "EUR/kWh", 3, p), axisFor("soc", "%", 1, p)],
      series: [
        {},
        Object.assign(line(p.price), { label: "Buy price", scale: "y", fill: withAlpha(p.price, 0.16), value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } }),
        Object.assign(line(p.soc), { label: "SoC", scale: "soc", value: function (u, v) { return fmt(v, 1, "%"); } })
      ]
    });
    var draw = function (el) { mount(el, opts, [xs, price, soc], 150, draw, "Next 24 hours: energy buy price in EUR per kWh and battery state of charge in percent."); };
    draw(host);
  }

  function forecastChart(host, kind, payload) {
    var slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
    if (!hasRows(slots)) { fallback(host, "No forecast slots returned."); return; }
    if (!chartsReady(host)) return;
    var p = palette();
    var xs = slots.map(function (s) { return ts(s.t); });
    var draw, opts, data;

    if (kind === "price") {
      var buy = slots.map(function (s) { return num(s.buy); });
      var sell = slots.map(function (s) { return num(s.sell); });
      var stale = slots.map(function (s) { return s.stale ? num(s.buy) : null; });
      var staleAny = stale.some(function (v) { return v != null; });
      opts = baseOpts(p, {
        scales: { x: { time: true }, y: { auto: true } },
        axes: [timeAxis(p), axisFor("y", "EUR/kWh", 3, p)],
        series: [
          {},
          Object.assign(line(p.price), { label: "Buy price", paths: barsPath(), fill: withAlpha(p.price, 0.8), value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } }),
          { label: "Stale (grey)", paths: barsPath(), stroke: p.stale, fill: withAlpha(p.stale, 0.9), width: 0, points: { show: false }, value: function (u, v) { return v == null ? "no" : "stale"; } },
          Object.assign(line(p.sell, true), { label: "Sell price", value: function (u, v) { return fmt(v, 4, "EUR/kWh"); } })
        ]
      });
      data = [xs, buy, stale, sell];
      draw = function (el) { mount(el, opts, data, 175, draw, "Price forecast: buy and sell price per 15 minute slot, grey bars mark stale slots."); };
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
        axes: [timeAxis(p), axisFor("y", "kW", 3, p)],
        bands: [{ series: [2, 3], fill: withAlpha(p.pv, 0.14), dir: -1 }],
        series: [
          {},
          Object.assign(area(p.pv, 0.22), { label: "PV forecast", value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(line(p.pv), { label: "Upper (forecast + uncertainty)", stroke: withAlpha(p.pv, 0.5), value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(line(p.pv), { label: "Lower (forecast − uncertainty)", stroke: withAlpha(p.pv, 0.5), value: function (u, v) { return fmt(v, 2, "kW"); } })
        ]
      });
      data = [xs, pv, hi, lo];
      draw = function (el) { mount(el, opts, data, 175, draw, "Solar forecast in kW with an uncertainty band around it."); };
    } else {
      var p50 = slots.map(function (s) { return num(s.load_kw); });
      var p10 = slots.map(function (s) { return num(s.p10_kw); });
      var p90 = slots.map(function (s) { return num(s.p90_kw); });
      opts = baseOpts(p, {
        scales: { x: { time: true }, y: { auto: true } },
        axes: [timeAxis(p), axisFor("y", "kW", 3, p)],
        bands: [{ series: [2, 3], fill: withAlpha(p.load, 0.14), dir: -1 }],
        series: [
          {},
          Object.assign(line(p.load), { label: "Load P50", value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(line(p.load), { label: "Load P90", stroke: withAlpha(p.load, 0.5), value: function (u, v) { return fmt(v, 2, "kW"); } }),
          Object.assign(line(p.load), { label: "Load P10", stroke: withAlpha(p.load, 0.5), value: function (u, v) { return fmt(v, 2, "kW"); } })
        ]
      });
      data = [xs, p50, p90, p10];
      draw = function (el) { mount(el, opts, data, 175, draw, "Load forecast in kW: P50 median line with a P10 to P90 band."); };
    }
    draw(host);
  }

  function savingsChart(host, buckets) {
    if (!hasRows(buckets)) { fallback(host, "No savings buckets for this range yet."); return; }
    if (!chartsReady(host)) return;
    var p = palette();
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
        grid: { show: true, stroke: p.grid, width: 1 }, ticks: { stroke: p.grid, width: 1 },
        values: function (u, vals) { return vals.map(function (v) { return keys[v] || ""; }); } };
    }
    var opts = baseOpts(p, {
      scales: { x: { time: allTimed }, savings: { auto: true }, cost: { auto: true } },
      axes: [xAxis, axisFor("savings", "EUR saved", 3, p), axisFor("cost", "EUR cost", 1, p)],
      series: [
        {},
        { label: "Savings", scale: "savings", paths: barsPath(), stroke: p.savings, fill: withAlpha(p.savings, 0.8), width: 0, points: { show: false }, value: function (u, v) { return fmt(v, 2, "EUR"); } },
        Object.assign(line(p.cost, true), { label: "Actual net cost", scale: "cost", value: function (u, v) { return fmt(v, 2, "EUR"); } }),
        Object.assign(line(p.baseline, true), { label: "Baseline cost (no battery)", scale: "cost", value: function (u, v) { return fmt(v, 2, "EUR"); } })
      ]
    });
    var draw = function (el) { mount(el, opts, [xs, savings, cost, baseline], 175, draw, "Savings per period in EUR, with actual net cost and no-battery baseline cost lines in EUR."); };
    draw(host);
  }

  function accuracyChart(host, reports) {
    var kinds = ["pv", "load", "price"].filter(function (k) {
      return reports && reports[k] && Array.isArray(reports[k].series) && reports[k].series.length > 0;
    });
    if (!kinds.length) { fallback(host, "No accuracy series yet (needs at least one day of data)."); return; }
    if (!chartsReady(host)) return;
    var p = palette();
    var pv = reports.pv, load = reports.load, price = reports.price;
    var dayIndex = {};
    var xs = [];
    var byKind = { pv: pv, load: load, price: price };
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
    var colors = { pv: p.pv, load: p.load, price: p.price };
    var labels = { pv: "PV MAPE error", load: "Load MAPE error", price: "Price MAPE error" };
    var opts = baseOpts(p, {
      scales: { x: { time: true }, y: { auto: true } },
      axes: [timeAxis(p), axisFor("y", "% error (MAPE)", 3, p)],
      series: [{}].concat(kinds.map(function (k) {
        return Object.assign(line(colors[k]), {
          label: labels[k],
          value: function (u, v) { return fmt(v, 1, "%"); }
        });
      }))
    });
    var data = [xs].concat(kinds.map(seriesFor));
    var draw = function (el) { mount(el, opts, data, 165, draw, "Forecast accuracy trend: mean absolute percentage error per day, in percent."); };
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
    savings: savingsChart,
    accuracy: accuracyChart,
    destroy: destroy,
    destroyTree: destroyTree,
    refresh: refresh
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
