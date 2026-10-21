/* sem-charts-card.js — the Smart Energy Manager Lovelace card.
 *
 * Served (never fetched from a CDN) as one self-contained bundle by
 * `GET /api/v1/web/sem-charts-card.js`: a `SEM_CARD_CSS` string, then the vendored uPlot, then
 * charts.js, then this file. The card reads only the `sensor.sem_chart_*` entities the add-on
 * publishes (SPEC 12), so it works over HTTPS, inside the mobile app, and without ever reaching the
 * add-on's ingress-only API.
 *
 * Manual install (no add-on privileges needed):
 *   1. In the add-on UI, Dashboard cards -> Download card, and save the file as
 *      /config/www/sem-charts-card.js
 *   2. Settings -> Dashboards -> Resources -> add `/local/sem-charts-card.js` as a JavaScript module
 *   3. Add a card:   type: custom:sem-charts-card   /   chart: plan
 */
(function () {
  "use strict";

  var root = typeof globalThis !== "undefined" ? globalThis : this;
  if (typeof root.HTMLElement === "undefined" || typeof root.customElements === "undefined") return;
  if (typeof root.SEMCharts === "undefined") return; // the bundle always ships charts.js first
  var charts = root.SEMCharts;
  var CSS = root.SEM_CARD_CSS || "";

  // The SPA's --chart-* tokens, restated as JS because the card renders outside the SPA (SPEC 13.1
  // keeps the names a frozen interface; these are the same values).
  var PALETTES = {
    dark: {
      price: "#e0a85a", sell: "#7fd1b9", pv: "#4cc38a", load: "#58a6ff", soc: "#a78bfa",
      charge: "#58a6ff", discharge: "#e0a85a", stale: "#6b7a8d", breakEven: "#e08484",
      baseline: "#9fb0c3", cost: "#e0a85a", savings: "#4cc38a", accent: "#58a6ff",
      grid: "#2e3a49", axis: "#9fb0c3"
    },
    light: {
      price: "#9c6414", sell: "#14705c", pv: "#1f7a4d", load: "#1c62c4", soc: "#5b46b8",
      charge: "#1c62c4", discharge: "#9c6414", stale: "#8795a6", breakEven: "#a33c3c",
      baseline: "#4c5c70", cost: "#9c6414", savings: "#1f7a4d", accent: "#1c62c4",
      grid: "#dbe3ec", axis: "#4c5c70"
    }
  };

  // Every chart the add-on publishes a feed for. `mini` draws the plan feed's price/SoC pair.
  var KINDS = {
    plan: { entity: "sensor.sem_chart_plan", title: "Plan timeline" },
    mini: { entity: "sensor.sem_chart_plan", title: "Next 24 hours" },
    price: { entity: "sensor.sem_chart_price", title: "Price forecast" },
    solar: { entity: "sensor.sem_chart_solar", title: "Solar forecast" },
    load: { entity: "sensor.sem_chart_load", title: "Load forecast" },
    savings: { entity: "sensor.sem_chart_savings", title: "Savings vs baseline" },
    accuracy: { entity: "sensor.sem_chart_accuracy", title: "Forecast accuracy" }
  };

  function epochToDate(seconds) {
    return new Date((Number(seconds) || 0) * 1000);
  }

  function planSlots(rows) {
    return (rows || []).map(function (row) {
      return {
        t: epochToDate(row[0]),
        buy: row[1],
        sell: row[2],
        soc_pct: row[3],
        pv_kw: row[4],
        load_kw: row[5],
        power_w: row[6],
        price_stale: row[7] === 1
      };
    });
  }

  function priceSlots(rows) {
    return (rows || []).map(function (row) {
      return { t: epochToDate(row[0]), buy: row[1], sell: row[2], stale: row[3] === 1 };
    });
  }

  function solarSlots(rows) {
    return (rows || []).map(function (row) {
      return { t: epochToDate(row[0]), pv_kw: row[1], uncertainty_pct: row[2] };
    });
  }

  function loadSlots(rows) {
    return (rows || []).map(function (row) {
      return { t: epochToDate(row[0]), load_kw: row[1], p10_kw: row[2], p90_kw: row[3] };
    });
  }

  function savingsBuckets(rows) {
    return (rows || []).map(function (row) {
      return {
        key: String(row[0]),
        start: epochToDate(row[0]),
        savings_eur: row[1],
        cost_eur: row[2],
        revenue_eur: row[3],
        baseline_cost_eur: row[4]
      };
    });
  }

  function accuracyReports(attributes) {
    var days = attributes.days || [];
    function seriesFor(column) {
      var values = column || [];
      return days.map(function (day, index) {
        return { day: epochToDate(day).toISOString(), mape_pct: values[index] };
      });
    }
    return {
      pv: { series: seriesFor(attributes.pv) },
      load: { series: seriesFor(attributes.load) },
      price: { series: seriesFor(attributes.price) }
    };
  }

  class SemChartsCard extends HTMLElement {
    constructor() {
      super();
      this._config = {};
      this._kind = "plan";
      this._hass = null;
      this._signature = null;
      var shadow = this.attachShadow({ mode: "open" });
      var style = document.createElement("style");
      style.textContent = CSS;
      shadow.appendChild(style);
      this._shell = root.customElements.get("ha-card")
        ? document.createElement("ha-card")
        : document.createElement("div");
      this._shell.className = "card-shell";
      this._title = document.createElement("p");
      this._title.className = "card-title";
      this._body = document.createElement("div");
      this._body.className = "chart-card-body";
      // One host for the whole life of the card: charts.js destroys the previous chart when it
      // mounts into the same element, so redrawing a feed update is a redraw and not a leak. A
      // fresh element per render would strand the old uPlot (and its cursor-sync subscription).
      this._host = document.createElement("div");
      this._host.className = "chart chart-md";
      this._shell.appendChild(this._title);
      this._shell.appendChild(this._body);
      shadow.appendChild(this._shell);
    }

    setConfig(config) {
      this._config = config || {};
      this._kind = KINDS[this._config.chart] ? this._config.chart : "plan";
      this._signature = null;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      var signature = this._fingerprint();
      if (signature !== this._signature) {
        this._signature = signature;
        this._render();
      }
    }

    // Home Assistant removes and re-inserts cards as the user edits a dashboard; drop the chart on
    // the way out rather than leaving a detached uPlot watching a resize event.
    disconnectedCallback() {
      if (charts.destroyTree) charts.destroyTree(this._host);
      this._signature = null;
    }

    getCardSize() {
      return this._kind === "accuracy" ? 5 : 7;
    }

    static getStubConfig() {
      return { chart: "plan" };
    }

    _fingerprint() {
      var state = this._state();
      return [
        this._kind,
        this._entityId(),
        state && state.last_updated,
        this._dark() ? "dark" : "light",
        this._config.range || "day",
        this._config.title || ""
      ].join("|");
    }

    _dark() {
      var themes = this._hass && this._hass.themes;
      return !themes || themes.darkMode !== false;
    }

    _entityId() {
      return this._config.entity || (KINDS[this._kind] && KINDS[this._kind].entity) || "";
    }

    _state() {
      if (!this._hass || !this._hass.states) return null;
      return this._hass.states[this._entityId()] || null;
    }

    _message(text) {
      if (charts.destroyTree) charts.destroyTree(this._host);
      this._body.textContent = "";
      var line = document.createElement("p");
      line.className = "card-empty";
      line.textContent = text;
      this._body.appendChild(line);
    }

    _render() {
      var kind = this._kind;
      this._title.textContent = this._config.title || (KINDS[kind] && KINDS[kind].title) || "Energy chart";
      this.setAttribute("data-theme", this._dark() ? "dark" : "light");
      if (!this._hass) {
        this._message("Waiting for Home Assistant\u2026");
        return;
      }
      var state = this._state();
      if (!state) {
        this._message(
          "Entity " + this._entityId() + " not found. Is the Smart Energy Manager add-on running and publishing chart feeds?"
        );
        return;
      }
      var attributes = state.attributes || {};
      if (attributes.schema !== 1) {
        this._message(
          "Unsupported chart feed schema (" + String(attributes.schema) + "). Update the add-on and the card together."
        );
        return;
      }
      // Slot-based charts only: savings and accuracy publish ranges/days, not slots, so an empty
      // `slots` there is not an empty feed.
      var slotKinds = { plan: 1, mini: 1, price: 1, solar: 1, load: 1 };
      if (slotKinds[kind] && !(attributes.slots || []).length) {
        // An empty feed is a fact (0.34.2): a missing price forecast is named as such instead of
        // drawing an empty chart that looks broken (SPEC 8.4).
        this._message(
          kind === "price"
            ? "No price forecast: no tariff provider resolved for this horizon. The plan runs zero-export self-consumption meanwhile."
            : "No data published for this chart yet."
        );
        return;
      }
      this._body.textContent = "";
      this._body.appendChild(this._host);
      var palette = this._dark() ? PALETTES.dark : PALETTES.light;
      try {
        this._draw(kind, this._host, attributes, palette);
      } catch (err) {
        this._message("Chart failed to render: " + (err && err.message ? err.message : String(err)));
        return;
      }
      var meta = document.createElement("p");
      meta.className = "card-meta";
      // 0.34.2 (SPEC 8.4): an unpriced plan feed says so, so a plan with no price line cannot be
      // mistaken for a chart that failed to load.
      var unpriced = attributes.priced === false;
      meta.textContent = "Source: " + this._entityId() + " \u00b7 " + (state.state || "unknown") +
        (unpriced ? " \u00b7 no price forecast (zero-export self-consumption)" : "");
      this._body.appendChild(meta);
    }

    _draw(kind, host, attributes, palette) {
      if (kind === "plan") {
        var efficiency = attributes.round_trip ? { round_trip: attributes.round_trip } : null;
        charts.plan(host, { slots: planSlots(attributes.slots) }, {
          wear: attributes.wear_eur_per_kwh || 0,
          efficiency: efficiency,
          palette: palette
        });
        return;
      }
      if (kind === "mini") {
        charts.mini(host, { slots: planSlots(attributes.slots) }, { palette: palette });
        return;
      }
      if (kind === "price") {
        charts.forecast(host, "price", { slots: priceSlots(attributes.slots) }, { palette: palette });
        return;
      }
      if (kind === "solar") {
        charts.forecast(host, "solar", { slots: solarSlots(attributes.slots) }, { palette: palette });
        return;
      }
      if (kind === "load") {
        charts.forecast(host, "load", { slots: loadSlots(attributes.slots) }, { palette: palette });
        return;
      }
      if (kind === "savings") {
        var range = this._config.range || "day";
        var ranges = attributes.ranges || {};
        charts.savings(host, savingsBuckets(ranges[range]), { palette: palette });
        return;
      }
      charts.accuracy(host, accuracyReports(attributes), { palette: palette });
    }
  }

  if (!root.customElements.get("sem-charts-card")) {
    root.customElements.define("sem-charts-card", SemChartsCard);
  }
  root.customCards = root.customCards || [];
  if (!root.customCards.some(function (entry) { return entry.type === "sem-charts-card"; })) {
    root.customCards.push({
      type: "sem-charts-card",
      name: "Smart Energy Manager chart",
      description: "Plan timeline, forecasts, savings or accuracy from the Smart Energy Manager add-on.",
      preview: false
    });
  }
})();
