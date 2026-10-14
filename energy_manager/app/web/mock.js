/* mock.js — synthetic fixtures for ?mock=1.
 *
 * Everything here is obviously fake: reserved-namespace entity ids (`*.example_*`), round numbers,
 * and a fixed baseline ("Example Street 1", 12.0 kWh battery). No network access, ever.
 *
 * Loadable in Node (no DOM at load time) so the static test suite can assert that every payload
 * carries the SPEC §10 fields. `SEM_MOCK.fixtures()` returns one plain object with all GET payloads.
 */
(function (root) {
  "use strict";

  // The demo plan uses the shipped plan resolution (config.DEFAULTS optimizer.slot_minutes = 60),
  // so the mock's own captions, slot table and windows match a default installation.
  var SLOT_MS = 60 * 60000;
  var CAP_KWH = 12.0;
  var MIN_KWH = 4.0;
  var MAX_KWH = 10.4;
  var MOCK_ROUND_TRIP_PCT = 90.0;
  var MOCK_ETA = Math.sqrt(MOCK_ROUND_TRIP_PCT / 100);
  // SPEC 5: wear is derived per battery from its own price and cycle life, per kWh delivered.
  var MOCK_PRICE_EUR = 3000.0;
  var MOCK_CYCLE_LIFE = 6000;
  // Delivered energy per full cycle is usable kWh x eta_discharge, so a full cycle costs price/life.
  var MOCK_WEAR = round(MOCK_PRICE_EUR / (MOCK_CYCLE_LIFE * (CAP_KWH * (90.0 - 10.0) / 100.0) * MOCK_ETA), 9);
  // SPEC 8.1 scheduled full-charge maintenance: the demo plan has one due battery, so the Plan view
  // exercises the maintenance banner and the Batteries card the per-battery "due" state.
  var MOCK_MAINTENANCE = {
    enabled: true,
    active: true,
    due: true,
    overdue: false,
    target_soc_pct: 100.0,
    effective_target_soc_pct: 100.0,
    interval_days: 30,
    overdue_grace_hours: 24.0,
    batteries: ["battery_1"],
    due_batteries: ["battery_1"],
    overdue_batteries: []
  };

  /** Mirrors config.py: keep a derived round_trip_efficiency_pct and wear_eur_per_kwh on every
   *  read-back config. Both are derived, never stored (SPEC 5). */
  function withDerivedEfficiency(config) {
    if (config && typeof config.round_trip_efficiency_pct !== "number") {
      var etaCharge = typeof config.eta_charge === "number" ? config.eta_charge : null;
      var etaDischarge = typeof config.eta_discharge === "number" ? config.eta_discharge : null;
      if (etaCharge && etaDischarge) config.round_trip_efficiency_pct = round(etaCharge * etaDischarge * 100, 6);
    }
    if (config) config.wear_eur_per_kwh = derivedWearEur(config);
    return config;
  }

  /** Mirrors BatteryConfig.wear_eur_per_kwh: price / (life x usable kWh x eta_discharge); 0 when
   *  unknown. The eta term makes one full discharge cost exactly price / life (SPEC 5). */
  function derivedWearEur(config) {
    var price = Number((config || {}).price_eur);
    var life = Number((config || {}).cycle_life);
    var capacity = Number((config || {}).capacity_kwh);
    var minSoc = Number((config || {}).min_soc_pct);
    var maxSoc = Number((config || {}).max_soc_pct);
    var etaDischarge = Number((config || {}).eta_discharge);
    if (!(etaDischarge > 0) || etaDischarge > 1) etaDischarge = MOCK_ETA;
    var usable = capacity * (maxSoc - minSoc) / 100;
    if (!(price > 0) || !(life > 0) || !(usable > 0)) return 0;
    return round(price / (life * usable * etaDischarge), 9);
  }

  /** Mirrors config.py: a pct-only payload is split evenly; explicit etas win. */
  function applyEfficiencyInput(config) {
    if (config && typeof config.round_trip_efficiency_pct === "number") {
      var explicit = typeof config.eta_charge === "number" || typeof config.eta_discharge === "number";
      if (!explicit) {
        var eta = Math.sqrt(config.round_trip_efficiency_pct / 100);
        config.eta_charge = eta;
        config.eta_discharge = eta;
      }
    }
    return withDerivedEfficiency(config);
  }

  var startMs = Math.floor(Date.now() / SLOT_MS) * SLOT_MS;

  function iso(ms) { return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z"); }
  function round(value, digits) {
    var f = Math.pow(10, digits);
    return Math.round(value * f) / f;
  }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  // Deterministic pseudo noise (no Math.random, so runs are reproducible).
  function wobble(i) { return Math.sin(i * 12.9898) * 0.5 + Math.sin(i * 4.1414) * 0.5; }

  var _slots = null;
  function planSlots() {
    if (_slots) return _slots;
    var out = [];
    var soc = 6.0;
    var lastBuy = 0.21;
    var count = 48; // 48 h; views request a window and slice it
    for (var i = 0; i < count; i++) {
      var t = startMs + i * SLOT_MS;
      var d = new Date(t);
      var hour = d.getHours() + d.getMinutes() / 60;
      var daylight = Math.max(0, Math.sin(Math.PI * (hour - 6) / 12));
      var pv = round(3.2 * Math.pow(daylight, 1.4), 2);
      var load = round(clamp(0.55 + 0.25 * Math.sin((hour / 24) * 2 * Math.PI) + (hour > 17 && hour < 21 ? 0.35 : 0) + 0.05 * wobble(i), 0.1, 2.5), 2);
      var stale = i > count * 0.8;
      var buy = stale ? lastBuy : round(clamp(0.14 + 0.16 * (1 - daylight) * (0.6 + 0.4 * Math.abs(wobble(i))) + (hour >= 18 && hour < 21 ? 0.06 : 0), 0.05, 0.6), 4);
      lastBuy = buy;
      var sell = round(Math.max(0.04, buy * 0.35), 4);
      // Mock: a few evening slots see a profitable peak feed-in so both the discharge and the
      // efficiency_loss branches appear in the demo plan.
      if (hour >= 18 && hour < 21 && i % 2 === 0) sell = round(buy * 1.3, 4);

      var power = 0, action = "hold", reason = "reserve_hold";
      if (pv * 1000 > load * 1000 + 300 && soc < MAX_KWH - 0.2) {
        power = Math.min(800, Math.round((pv - load) * 1000 / 50) * 50);
        action = "pv_charge"; reason = "pv_surplus_charge";
      } else if (buy < 0.17 && soc < MAX_KWH - 0.2) {
        power = 600; action = "charge"; reason = "arbitrage_charge";
      } else if (hour >= 18 && hour < 21 && soc > MIN_KWH + 0.4) {
        var breakEven = buy / (MOCK_ETA * MOCK_ETA);
        if (sell >= breakEven) {
          power = -700; action = "discharge"; reason = "peak_discharge";
        } else {
          action = "hold"; reason = "efficiency_loss";
        }
      } else if (soc >= MAX_KWH - 0.2) {
        action = "reserve"; reason = "battery_full";
      } else if (soc <= MIN_KWH + 0.2) {
        action = "reserve"; reason = "battery_empty";
      }
      if (power > 0) soc = Math.min(MAX_KWH, soc + (power / 1000) * 1.0 * 0.95);
      else if (power < 0) soc = Math.max(MIN_KWH, soc + (power / 1000) * 1.0 / 0.95);
      soc = round(soc, 2);

      out.push({
        t: iso(t),
        buy: buy,
        sell: sell,
        pv_kw: pv,
        load_kw: load,
        load_p10: round(load * 0.7, 2),
        load_p90: round(load * 1.35, 2),
        soc_kwh: soc,
        soc_pct: round((soc / CAP_KWH) * 100, 1),
        power_w: power,
        action: action,
        reason: reason,
        price_stale: stale,
        pv_source: "capacity",
        price_source: "zonneplan_entity"
      });
    }
    _slots = out;
    return out;
  }

  // GET /api/v1/plan/execution (SPEC 10, additive): the plan grid joined with the 5-minute actuals
  // and the dispatch log. The demo plan starts at the slot containing `now`, so only that slot has
  // been reached: it carries partial measured rows and the dispatch attempt the mode would produce.
  function executionPayload(hours) {
    var plan = planPayload(hours);
    var slots = plan.slots;
    var nowMs = Date.now();
    var stepMs = plan.slot_minutes * 60000;
    var floors = { reserve_soc_pct: 10.0, min_soc_pct: 10.0, floor_soc_pct: 10.0 };
    var charged = 0.0, discharged = 0.0, imported = 0.0, exported = 0.0, cost = 0.0, samples = 0;
    var reached = 0, spanMinutes = 0.0;
    var out = slots.map(function (slot) {
      var startMs = new Date(slot.t).getTime();
      var endMs = startMs + stepMs;
      if (startMs >= nowMs) return { t: slot.t, dispatch: null, actual: null };
      // Measured minutes so far in this slot: a 5-minute grid, capped at the slot length.
      var measuredMinutes = Math.max(0, Math.min(plan.slot_minutes, Math.floor((nowMs - startMs) / 300000) * 5));
      if (measuredMinutes <= 0) return { t: slot.t, dispatch: dispatchFor(slot), actual: null };
      var coverage = round(100.0 * measuredMinutes / plan.slot_minutes, 1);
      var gridKw = slot.load_kw - slot.pv_kw + slot.power_w / 1000;
      var batteryKw = slot.power_w / 1000;
      var actual = {
        samples: measuredMinutes / 5,
        coverage_pct: coverage,
        pv_kw: slot.pv_kw,
        load_kw: slot.load_kw,
        grid_kw: round(gridKw, 4),
        battery_kw: round(batteryKw, 4),
        planned_kw: round(batteryKw, 4),
        measured_kw: round(batteryKw, 4),
        import_kwh: round(Math.max(0, gridKw) * measuredMinutes / 60, 4),
        export_kwh: round(Math.max(0, -gridKw) * measuredMinutes / 60, 4),
        charged_kwh: round(Math.max(0, batteryKw) * measuredMinutes / 60, 4),
        discharged_kwh: round(Math.max(0, -batteryKw) * measuredMinutes / 60, 4),
        soc_kwh_start: slot.soc_kwh,
        soc_kwh_end: slot.soc_kwh
      };
      reached += 1;
      samples += actual.samples;
      spanMinutes += measuredMinutes;
      charged += actual.charged_kwh;
      discharged += actual.discharged_kwh;
      imported += actual.import_kwh;
      exported += actual.export_kwh;
      cost += actual.import_kwh * slot.buy - actual.export_kwh * slot.sell;
      return { t: slot.t, dispatch: dispatchFor(slot), actual: actual };
    });
    var plannedCharged = 0.0, plannedDischarged = 0.0, plannedCost = 0.0;
    var elapsed = 0;
    slots.forEach(function (slot) {
      var startMs = new Date(slot.t).getTime();
      var share = startMs + stepMs <= nowMs ? 1.0 : 0.0;
      if (startMs + stepMs <= nowMs) elapsed += 1;
      plannedCharged += Math.max(0, slot.power_w) / 1000 * share;
      plannedDischarged += Math.max(0, -slot.power_w) / 1000 * share;
      var grid = slot.load_kw - slot.pv_kw + slot.power_w / 1000;
      plannedCost += (grid >= 0 ? grid * slot.buy : grid * slot.sell) * share;
    });
    return {
      generated_at: plan.generated_at,
      slot_minutes: plan.slot_minutes,
      now: iso(nowMs),
      floors: floors,
      batteries: [{ id: "battery_1", name: "Example Battery 1", min_soc_pct: 10.0, floor_soc_pct: 10.0, capacity_kwh: CAP_KWH }],
      progress: {
        slots_elapsed: elapsed,
        slots_measured: reached,
        slots_total: slots.length,
        samples: samples,
        coverage_pct: spanMinutes > 0 ? round(100.0 * samples * 5 / spanMinutes, 1) : 0.0,
        planned_charged_kwh: round(plannedCharged, 4),
        planned_discharged_kwh: round(plannedDischarged, 4),
        planned_cost_eur: round(plannedCost, 4),
        measured_charged_kwh: reached ? round(charged, 4) : null,
        measured_discharged_kwh: reached ? round(discharged, 4) : null,
        measured_import_kwh: reached ? round(imported, 4) : null,
        measured_export_kwh: reached ? round(exported, 4) : null,
        measured_cost_eur: reached ? round(cost, 4) : null
      },
      slots: out
    };
  }

  // What the dispatcher would have written for this slot in the demo's mode: advisory/dry-run
  // skip the write, auto applies the setpoint. A stale slot is refused, so it gets no attempt.
  function dispatchFor(slot) {
    if (slot.price_stale) return null;
    var requested = slot.power_w;
    var auto = db.mode === "auto";
    var applied = auto && !db.dry_run ? requested : 0.0;
    return {
      ts: iso(Math.min(Date.now(), new Date(slot.t).getTime() + 60000)),
      requested_w: requested,
      applied_w: applied,
      ok: true,
      dry_run: !!db.dry_run,
      clamped: false,
      code: auto ? "ok" : "mode_not_auto",
      reason: auto ? slot.reason : "mode_not_auto",
      message: auto ? "" : "Write skipped: mode is advisory",
      records: 1,
      batteries: [{ id: "battery_1", requested_w: requested, applied_w: applied, ok: true }]
    };
  }

  function windowSlots(hours) {
    var n = clamp(parseInt(hours, 10) || 36, 1, 48);
    return planSlots().slice(0, n);
  }

  function planPayload(hours) {
    var slots = windowSlots(hours);
    var cost = 0, baseline = 0;
    slots.forEach(function (s) {
      var dt = 1.0;
      var grid = s.load_kw - s.pv_kw - s.power_w / 1000;
      cost += dt * (grid >= 0 ? s.buy * grid : -s.sell * -grid);
      var base = s.load_kw - s.pv_kw;
      baseline += dt * (base >= 0 ? s.buy * base : -s.sell * -base);
    });
    // Summary extras mirror the optimiser (SPEC 8.1): the terminal salvage is
    // salvage_multiplier x median(buy) x eta_discharge per stored kWh, so the exact objective is
    // grid cost + cycle cost - salvage, and the break-even sale price is taken at the same median
    // buy. Both are computed from the rounded grid cost the summary reports so the three numbers
    // stay consistent. The real serializer emits break-even unrounded; so does this mock.
    // Wear is charged per kWh delivered: the mock computes it from its own slot powers so the
    // summary's wear total is consistent with the per-kWh constant it reports (SPEC 8.1).
    var dischargedKwh = 0.0;
    slots.forEach(function (s) {
      var power = Number(s.power_w);
      if (isFinite(power) && power < 0) dischargedKwh += -power / 1000.0 * 1.0;
    });
    var cycleCost = round(MOCK_WEAR * dischargedKwh, 6);
    var costs = round(cost, 2);
    var buys = slots.map(function (s) { return s.buy; }).sort(function (a, b) { return a - b; });
    var medianBuy = 0;
    if (buys.length) {
      var mid = Math.floor(buys.length / 2);
      medianBuy = buys.length % 2 ? buys[mid] : (buys[mid - 1] + buys[mid]) / 2;
    }
    var roundTrip = round(MOCK_ETA * MOCK_ETA, 6);
    var terminalKwh = slots.length ? slots[slots.length - 1].soc_kwh : 0;
    return {
      generated_at: iso(startMs),
      horizon_start: slots.length ? slots[0].t : iso(startMs),
      horizon_end: slots.length ? iso(startMs + slots.length * SLOT_MS) : iso(startMs),
      slot_minutes: 60,
      feasible: true,
      infeasible_reason: null,
      mode: db.mode,
      dry_run: db.dry_run,
      sources: { price: "zonneplan_entity", solar: "capacity", load: "statistics" },
      summary: {
        cost_estimate_eur: costs,
        baseline_cost_eur: round(baseline, 2),
        saved_eur: round(baseline - cost, 2),
        charged_kwh: 6.4,
        discharged_kwh: 5.9,
        grid_charge_kwh: 2.1,
        pv_charge_kwh: 4.3,
        cycles: 0.52,
        arbitrage_kwh: 2.1,
        effective_step_kwh: 0.02,
        solver_iterations: 346752,
        cycle_cost_eur: cycleCost,
        objective_eur: round(costs + cycleCost - medianBuy * MOCK_ETA * terminalKwh, 2),
        eta_charge: MOCK_ETA,
        eta_discharge: MOCK_ETA,
        round_trip_efficiency: roundTrip,
        wear_eur_per_kwh: MOCK_WEAR,
        break_even_sell_eur_per_kwh: medianBuy / roundTrip + MOCK_WEAR,
        sell_floor_eur_per_kwh: medianBuy / roundTrip + MOCK_WEAR,
        maintenance: MOCK_MAINTENANCE
      },
      maintenance: MOCK_MAINTENANCE,
      slots: slots,
      batteries: [{ id: "battery_1", name: "Example Battery 1", share_w: slots.length ? slots[0].power_w : 0, round_trip_efficiency: roundTrip, wear_eur_per_kwh: MOCK_WEAR, break_even_sell_eur_per_kwh: medianBuy / roundTrip + MOCK_WEAR }]
    };
  }

  function priceSlots(hours) {
    return windowSlots(hours).map(function (s) {
      return { t: s.t, buy: s.buy, sell: s.sell, source: "zonneplan_entity", is_actual: false, stale: s.price_stale };
    });
  }

  function solarSlots(hours) {
    return windowSlots(hours).map(function (s) {
      return { t: s.t, pv_kw: s.pv_kw, uncertainty_pct: 25.0 };
    });
  }

  // The capacity source (SPEC §7.1.1): a stated watt peak and no plane geometry at all, so
  // `planes` stays empty (the tester's four per-plane keys describe a Forecast.Solar plane) and the
  // notes carry every assumption instead. The wording mirrors sources/solar.py so the demo cannot
  // drift from what the backend actually emits.
  function capacitySolarPayload(hours) {
    return {
      kind: "solar", generated_at: iso(startMs), source: "capacity", stale: false,
      provider: "capacity", contract: null, reason: null, hint: null,
      planes: [],
      correction_applied: 1.06,
      notes: [
        "capacity estimate: 5800 Wp from solar.capacity_wp",
        "assumed south-facing, 35 degree tilt; set solar.tilt_deg / solar.azimuth_deg to refine",
        "performance ratio 0.85 (solar.performance_ratio)",
        "weather.example_home does not support weather.get_forecasts; current cloud cover held flat",
        "a measured PV entity refines this curve with a learned per-hour correction (solar.measured_entity; use the inverter's AC output, not a meter's export register)",
        MOCK_NOTE
      ],
      // Cloud cover held flat narrows the band to 25% (25.0 without, 35.0 with no cloud data).
      slots: solarSlots(hours)
    };
  }

  function loadSlots(hours) {
    return windowSlots(hours).map(function (s) {
      return { t: s.t, load_kw: s.load_kw, p10_kw: s.load_p10, p90_kw: s.load_p90 };
    });
  }

  var MOCK_NOTE = "Synthetic mock data (?mock=1): not a real installation.";

  function statePayload(batteryId) {
    var id = batteryId || "battery_1";
    return {
      id: id,
      available: true,
      soc_pct: 62.5,
      power_w: -240,
      capacity_kwh: CAP_KWH,
      energy_kwh: 7.5,
      pv_power_w: 0,
      temperature_c: 21.5,
      read_at: iso(Date.now() - 20000),
      last_error: null,
      stale: false,
      cell_voltage_v: 3.05,
      // 0.20.0 manual limits: the configured hardware maxima and the resolved caps (null =
      // unlimited), exactly the keys the real /status row carries.
      max_charge_w: 800.0,
      max_discharge_w: 800.0,
      cap_charge_w: mockManualLimit(id, "charge"),
      cap_discharge_w: mockManualLimit(id, "discharge"),
      extra: { cycles: 128.5, name: "Example Battery 1", cell_voltage_v: 3.05 }
    };
  }

  function batteryRecord() {
    return {
      config: withDerivedEfficiency({
        id: "battery_1",
        name: "Example Battery 1",
        driver: "generic",
        capacity_kwh: CAP_KWH,
        min_soc_pct: 10.0,
        max_soc_pct: 90.0,
        max_charge_w: 800.0,
        max_discharge_w: 800.0,
        eta_charge: MOCK_ETA,
        eta_discharge: MOCK_ETA,
        round_trip_efficiency_pct: MOCK_ROUND_TRIP_PCT,
        price_eur: MOCK_PRICE_EUR,
        cycle_life: MOCK_CYCLE_LIFE,
        control_path: "auto",
        capacity_source: "learned",
        mapping: {
          power_setpoint_entity: "number.example_battery_input_limit",
          mode_select_entity: "select.example_battery_mode",
          mode_manual_value: "manual",
          soc_entity: "sensor.example_battery_soc_1",
          capacity_entity: "sensor.example_battery_capacity",
          cell_voltage_entity: "sensor.example_battery_cell_voltage"
        },
        enabled: db.batteryEnabled
      }),
      state: statePayload(),
      capabilities: {
        max_charge_w: 800.0,
        max_discharge_w: 800.0,
        min_soc_pct: 10.0,
        max_soc_pct: 90.0,
        signed_setpoint: true,
        manual_mode_required: true,
        writes: ["power_setpoint_entity", "mode_select_entity"]
      },
      mapping_resolved: true,
      discovery: discoveryCandidates("battery"),
      // SPEC 5 learned capacity: the effective value the planner uses, the estimate's evidence and
      // a stable reason slug. Synthetic, so the card and the render check exercise every branch.
      capacity_source: "learned",
      capacity_kwh: CAP_KWH,
      capacity_learned_kwh: 9.6,
      capacity_samples: 5,
      capacity_updated_at: iso(Date.now() - 2 * 86400000),
      effective_capacity_kwh: 9.6,
      capacity_reason: "capacity_learned",
      capacity: {
        source: "learned",
        configured_kwh: CAP_KWH,
        learned_kwh: 9.6,
        samples: 5,
        updated_at: iso(Date.now() - 2 * 86400000),
        effective_kwh: 9.6,
        reason: "capacity_learned",
        stale: false,
        confident: true,
        applied: true
      },
      // SPEC 8.1: this battery's full-charge cycle is due, so the card shows "due now" and the
      // mock plan above targets 100 %.
      maintenance: {
        id: "battery_1",
        watching: true,
        state: "due",
        due: true,
        overdue: false,
        at_target: false,
        due_at: iso(Date.now() - 3600000),
        due_in_days: -0.042,
        last_completed: iso(Date.now() - 31 * 86400000),
        first_seen: iso(Date.now() - 61 * 86400000),
        target_soc_pct: 100.0,
        interval_days: 30,
        soc_pct: 62.5
      }
    };
  }

  function discoveryCandidates(kind) {
    // Discovery rows the picker groups: the device_* fields (device_id, device_name, manufacturer,
    // model) and min/max are the shape the entity registry adds. The second device is deliberately
    // SENSOR-only for the control roles — it looks like a battery but cannot be driven, so the
    // picker must list it as read-only and never map a sensor to a control role.
    var DEVICE_BATTERY = { device_id: "example_dev_battery_1", device_name: "Example Battery 1", manufacturer: "ExampleCorp", model: "Model One" };
    var DEVICE_SENSORS = { device_id: "example_dev_sensor_bank", device_name: "Example Sensor Bank", manufacturer: "ExampleCorp", model: "Model Two" };
    // A Zendure inverter bridged by Home Assistant's own MQTT integration: one entity per zenSDK
    // property, named after it, on the `mqtt` platform. The picker identifies the device by
    // platform + manufacturer/model and reads each row's role from the property name.
    var DEVICE_MQTT = { device_id: "example_dev_mqtt", device_name: "Example MQTT inverter", manufacturer: "Zendure", model: "solarFlow800Plus" };
    var all = {
      battery: [
        Object.assign({ entity_id: "number.example_battery_input_limit", name: "Example battery input limit", unit: "W", device_class: "power", state: "-240", platform: "zendure_ha", suggested_role: "power_setpoint", confidence: 0.86, min: -800, max: 800 }, DEVICE_BATTERY),
        Object.assign({ entity_id: "sensor.example_battery_soc_1", name: "Example battery state of charge", unit: "%", device_class: "battery", state: "62.5", platform: "zendure_ha", suggested_role: "soc", confidence: 0.92, min: null, max: null }, DEVICE_BATTERY),
        Object.assign({ entity_id: "sensor.example_battery_capacity", name: "Example battery capacity", unit: "kWh", device_class: "energy_storage", state: "12.0", platform: "zendure_ha", suggested_role: "capacity", confidence: 0.74, min: null, max: null }, DEVICE_BATTERY),
        Object.assign({ entity_id: "select.example_battery_mode", name: "Example battery mode", unit: null, device_class: null, state: "manual", platform: "zendure_ha", suggested_role: "manual_mode", confidence: 0.68, min: null, max: null }, DEVICE_BATTERY),
        Object.assign({ entity_id: "sensor.example_battery_output_home_power", name: "Example battery output power", unit: "W", device_class: "power", state: "240", platform: "zendure_ha", suggested_role: "output_power", confidence: 0.55, min: null, max: null }, DEVICE_BATTERY),
        Object.assign({ entity_id: "number.example_mqtt_inputlimit", name: "inputLimit", unit: null, device_class: "power", state: "0", platform: "mqtt", suggested_role: "charge_limit", confidence: 0.45, min: 0, max: 1000 }, DEVICE_MQTT),
        Object.assign({ entity_id: "number.example_mqtt_outputlimit", name: "outputLimit", unit: null, device_class: "power", state: "0", platform: "mqtt", suggested_role: "discharge_limit", confidence: 0.45, min: 0, max: 800 }, DEVICE_MQTT),
        Object.assign({ entity_id: "select.example_mqtt_acmode", name: "acMode", unit: null, device_class: null, state: "Output mode", platform: "mqtt", suggested_role: "manual_mode", confidence: 0.3, min: null, max: null }, DEVICE_MQTT),
        Object.assign({ entity_id: "switch.example_mqtt_smartmode", name: "smartMode", unit: null, device_class: null, state: "off", platform: "mqtt", suggested_role: "smart_mode", confidence: 0.3, min: null, max: null }, DEVICE_MQTT),
        Object.assign({ entity_id: "sensor.example_mqtt_electriclevel", name: "electricLevel", unit: "%", device_class: "battery", state: "41", platform: "mqtt", suggested_role: "soc", confidence: 0.55, min: null, max: null }, DEVICE_MQTT),
        Object.assign({ entity_id: "sensor.example_mqtt_outputhomepower", name: "outputHomePower", unit: "W", device_class: "power", state: "0", platform: "mqtt", suggested_role: "output_power", confidence: 0.45, min: null, max: null }, DEVICE_MQTT),
        Object.assign({ entity_id: "sensor.example_mqtt_gridinputpower", name: "gridInputPower", unit: "W", device_class: "power", state: "0", platform: "mqtt", suggested_role: "grid_input_power", confidence: 0.45, min: null, max: null }, DEVICE_MQTT),
        Object.assign({ entity_id: "sensor.example_sensor_bank_power", name: "Example sensor bank power", unit: "W", device_class: "power", state: "120", platform: "example_integration", suggested_role: "power_setpoint", confidence: 0.9, min: null, max: null }, DEVICE_SENSORS),
        Object.assign({ entity_id: "sensor.example_sensor_bank_soc", name: "Example sensor bank state of charge", unit: "%", device_class: "battery", state: "41.0", platform: "example_integration", suggested_role: "soc", confidence: 0.88, min: null, max: null }, DEVICE_SENSORS),
        Object.assign({ entity_id: "sensor.example_sensor_bank_capacity", name: "Example sensor bank capacity", unit: "kWh", device_class: "energy_storage", state: "5.0", platform: "example_integration", suggested_role: "capacity", confidence: 0.8, min: null, max: null }, DEVICE_SENSORS)
      ],
      price: [
        { entity_id: "sensor.example_price_now", name: "Example dynamic price forecast", unit: "EUR/kWh", device_class: "monetary", state: "0.2140", platform: "example_integration", suggested_role: "price_now", confidence: 0.81, setting_key: "price.entity" }
      ],
      solar: [
        { entity_id: "sensor.example_pv_power", name: "Example PV power", unit: "W", device_class: "power", state: "2450", platform: "example_integration", suggested_role: "pv_power", confidence: 0.88, setting_key: "solar.measured_entity" },
        { entity_id: "sensor.example_pv_energy_today", name: "Example PV energy today", unit: "kWh", device_class: "energy", state: "7.4", platform: "example_integration", suggested_role: "pv_energy", confidence: 0.7, setting_key: "solar.measured_entity" }
      ],
      load: [
        { entity_id: "sensor.example_load_power", name: "Example household load power", unit: "W", device_class: "power", state: "810", platform: "example_integration", suggested_role: "load_power", confidence: 0.9, setting_key: "load.entity" },
        { entity_id: "sensor.example_outdoor_temperature", name: "Example outdoor temperature", unit: "°C", device_class: "temperature", state: "11.5", platform: "example_integration", suggested_role: "temperature", confidence: 0.62, setting_key: "load.temperature_entity" }
      ],
      grid: [
        { entity_id: "sensor.example_grid_power", name: "Example grid power", unit: "W", device_class: "power", state: "-1640", platform: "example_integration", suggested_role: "grid_power", confidence: 0.85, setting_key: "grid.power_entity" },
        { entity_id: "sensor.example_export_power", name: "Example grid export power", unit: "W", device_class: "power", state: "1640", platform: "example_integration", suggested_role: "grid_export", confidence: 0.72, setting_key: "grid.export_entity" }
      ]
    };
    return all[kind] || [];
  }

  // -----------------------------------------------------------------------------------
  // Settings descriptors (SPEC §3 frozen keys). Types drive the generated form.
  // -----------------------------------------------------------------------------------

  // Mirrors config.DEFAULTS / SETTINGS_META: ui type, default, label, help, lowercase group.
  // The "Safety" group is derived exactly like the backend does (descriptorGroup below).
  var SETTINGS = [
    ["mode", "str", "advisory", "Mode", "off, advisory or auto; advisory never writes.", "general", ["off", "advisory", "auto"]],
    ["dry_run", "bool", true, "Dry run", "Log the writes that would happen without doing them.", "general", null],
    ["log_level", "str", "info", "Log level", "", "general", ["debug", "info", "warning", "error"]],
    ["currency", "str", null, "Currency", "Unset = read from HA.", "general", null],
    ["timezone", "str", null, "Timezone", "Unset = read from HA.", "general", null],
    ["latitude_override", "str", null, "Latitude override", "Unset = read from HA.", "general", null],
    ["longitude_override", "str", null, "Longitude override", "Unset = read from HA.", "general", null],

    ["price.provider", "str", "entity", "Price provider", "Where prices come from. entity = a sensor you pick (advanced); zonneplan = the Zonneplan integration's tariff sensor found in Home Assistant; day_ahead = energy_charts wholesale day-ahead prices for the bidding zone; fixed = a flat or day/night tariff.", "price", ["entity", "zonneplan", "day_ahead", "fixed"]],
    ["price.contract", "str", "dynamic_hourly", "Contract type", "dynamic_quarter_hourly / dynamic_hourly = market prices per quarter hour or per hour; fixed_single = one rate all day; fixed_day_night = two rates split by the day window below. Dynamic providers pick their own grid, so this only matters for a fixed tariff.", "price", ["dynamic_quarter_hourly", "dynamic_hourly", "fixed_single", "fixed_day_night"]],
    ["price.source", "str", "zonneplan_entity", "Price source", "Only used by the entity provider: configured source with fallback to energy_charts then static.", "price", ["zonneplan_entity", "energy_charts", "entity_forecast", "static"]],
    ["price.entity", "str", "", "Price entity", "Runtime-picked HA sensor entity id.", "price", null],
    ["price.fixed_price_eur_per_kwh", "float", null, "Fixed rate", "Consumer price incl. VAT and tax, EUR/kWh. Unset = read the entity below.", "price", null],
    ["price.fixed_day_price_eur_per_kwh", "float", null, "Day rate", "Consumer price incl. VAT and tax for the day window, EUR/kWh.", "price", null],
    ["price.fixed_night_price_eur_per_kwh", "float", null, "Night rate", "Consumer price incl. VAT and tax outside the day window, EUR/kWh.", "price", null],
    ["price.fixed_price_entity", "str", "", "Fixed rate entity", "HA sensor holding the consumer price; overrides the typed fixed rate.", "price", null],
    ["price.fixed_day_price_entity", "str", "", "Day rate entity", "HA sensor holding the day rate; overrides the typed day rate.", "price", null],
    ["price.fixed_night_price_entity", "str", "", "Night rate entity", "HA sensor holding the night rate; overrides the typed night rate.", "price", null],
    ["price.fixed_day_start_hour", "int", 7, "Day starts at", "Local hour the day rate starts, 0-23.", "price", null],
    ["price.fixed_night_start_hour", "int", 23, "Night starts at", "Local hour the night rate starts, 0-23.", "price", null],
    ["price.static_schedule", "json", [], "Static price schedule", "List of {from_hour,to_hour,price_eur_per_kwh,days}.", "price", null],
    ["price.import_fee_eur_per_kwh", "float", 0.0, "Import fee", "Supplier markup on buy, EUR/kWh.", "price", null],
    ["price.export_fee_eur_per_kwh", "float", 0.0, "Feed-in cost", "Deducted per kWh from the feed-in price; default 0.", "price", null],
    ["price.vat_pct", "float", 21.0, "VAT %", "Applied when a source publishes ex-VAT prices.", "price", null],
    ["price.energy_tax_eur_per_kwh", "float", 0.0, "Energy tax", "Applied when a source publishes ex-tax prices.", "price", null],
    ["price.unit", "str", "auto", "Declared unit", "auto = use the provider's declared unit.", "price", ["auto", "eur_per_kwh", "eur_per_mwh", "micro_eur_per_kwh"]],
    ["price.scale_divisor", "float", 1.0, "Scale divisor", "For providers publishing fixed-point integers.", "price", null],
    ["price.bzn", "str", "NL", "Bidding zone", "energy_charts zone, e.g. NL, DE-LU.", "price", null],
    ["price.feed_in_eur_per_kwh", "float", null, "Flat feed-in price", "Explicit EUR/kWh received; overrides the derived feed-in price.", "price", null],
    ["price.feed_in_entity", "str", "", "Feed-in price entity", "HA sensor with the current feed-in price; overrides the derived and flat feed-in prices.", "price", null],
    ["price.feed_in_tax_inclusive", "bool", true, "Feed-in incl. VAT", "For the flat tariff/entity only: off = add price.vat_pct to the value.", "price", null],
    ["price.feed_in_energy_tax_refund", "bool", true, "Energy tax refunded", "On = feed-in pays the same consumer price you buy at, minus the feed-in cost. Off = the energy-tax component is not paid back and is subtracted too.", "price", null],

    ["solar.enabled", "bool", true, "Solar forecast", "Enable the PV forecast.", "solar", null],
    ["solar.source", "str", "forecast_solar", "Solar source", "forecast_solar = the Forecast.Solar API (needs planes); capacity = estimate from the array's watt peak; derived = learn the array from the meter's export moments minus what the battery was charging, no numbers to enter; learned = a model trained on 174 other Dutch arrays that corrects its own physics for this array (needs solar.capacity_wp).", "solar", ["forecast_solar", "capacity", "derived", "learned"]],
    ["solar.planes", "json", [], "PV planes", "forecast_solar only. List of {kwp, tilt_deg, azimuth_deg (compass: 0=north), damping}.", "solar", null],
    ["solar.api_key", "password", "", "Forecast.Solar key", "forecast_solar only. Optional personal key; keyless is single-plane hourly.", "solar", null],
    ["solar.bias_correction", "bool", true, "Bias correction", "Learn a multiplicative correction from measured PV.", "solar", null],
    ["solar.measured_entity", "str", "", "Measured PV entity", "Runtime-picked PV generation sensor. Use the inverter's AC output; a meter's export register under-reports because self-consumed energy never reaches the meter.", "solar", null],
    // capacity only (SPEC 7.1.1): the array's watt peak plus the geometry the model assumes while
    // it is unset, the performance ratio and the optional weather entity for cloud attenuation.
    ["solar.capacity_wp", "float", null, "Array watt peak", "capacity/learned. Total Wp of the array (all panels added). Unset = no capacity estimate, never a guess from history.", "solar", null],
    ["solar.ac_capacity_wp", "float", null, "Inverter AC limit", "learned only. The inverter's maximum AC output in watts; when the array's DC watt peak is higher, the model clips past this. Unset = assumed equal to the watt peak (no clipping).", "solar", null],
    ["solar.tilt_deg", "float", null, "Tilt", "capacity only. Panel tilt from horizontal. Unset = assume 35 degrees, stated in the forecast notes.", "solar", null],
    ["solar.azimuth_deg", "float", null, "Azimuth", "capacity only. Compass direction the panels face: 0 = north, 90 = east, 180 = south, 270 = west. Unset = assume south.", "solar", null],
    ["solar.performance_ratio", "float", 0.85, "Performance ratio", "capacity only. Inverter, temperature and soiling losses applied to the clear-sky estimate.", "solar", null],
    ["solar.weather_entity", "str", "", "Weather entity", "capacity only. HA weather entity whose hourly cloud coverage attenuates the estimate. Unset = clear-sky, refined by the measured-PV correction.", "solar", null],
    ["solar.nasa_irradiance", "bool", true, "NASA irradiance", "capacity/derived only. NASA POWER's observed hourly irradiance (keyless, 2-3 day latency) adds a per-hour clearness factor where the weather entity has no cloud data, and refines the meter-derived array fit.", "solar", null],
    ["solar.sources", "json", [], "Forecast sources", "Merge several PV forecasts into one: an ordered list of {source, weight, ...}. source is forecast_solar, ha_energy (any solar-forecast provider the HA Energy dashboard already has), capacity, derived or learned; weight is a finite number > 0. Empty = use solar.source alone.", "solar", null],
    ["solar.merge_strategy", "str", "weighted_mean", "Merge strategy", "How several sources become one value per slot: weighted_mean = the weighted average (default), median = the middle value, max = the most optimistic source.", "solar", ["weighted_mean", "median", "max"]],

    ["load.mode", "str", "auto", "Load source", "auto uses the load entity if mapped, else derived.", "load", ["auto", "entity", "derived"]],
    ["load.entity", "str", "", "Load entity", "Runtime-picked load power sensor.", "load", null],
    ["load.temperature_entity", "str", "", "Outdoor temperature entity", "Optional regression term.", "load", null],
    ["load.base_kwh_per_day", "float", 0.0, "Fallback daily energy", "Flat profile until something is learned.", "load", null],

    ["grid.power_entity", "str", "", "Grid power entity", "Signed grid power sensor, positive = import; enables the derived load tier.", "grid", null],
    ["grid.import_entity", "str", "", "Grid import entity", "Separate import power sensor (optional).", "grid", null],
    ["grid.export_entity", "str", "", "Grid export entity", "Separate export power sensor (optional).", "grid", null],

    ["optimizer.slot_minutes", "int", 60, "Slot minutes", "Plan and dispatch resolution; match it to the tariff's slot length.", "optimizer", null],
    ["optimizer.horizon_hours", "int", 30, "Horizon hours", "Clamped to 12..48 by the optimizer. Default 30 h matches the ~+30 h Zonneplan feed.", "optimizer", null],
    ["optimizer.soc_step_kwh", "float", 0.02, "SoC step", "kWh per DP state.", "optimizer", null],
    ["optimizer.power_steps", "int", 24, "Power steps", "Discrete actions per state.", "optimizer", null],
    ["optimizer.salvage_multiplier", "float", 1.0, "Salvage multiplier", "Terminal SoC valued at multiplier x median buy price.", "optimizer", null],
    ["optimizer.cycle_cost_eur_per_kwh", "float", 0.0, "Wear override", "Advanced: a non-zero value replaces every battery's derived wear (price / (cycle life x usable kWh x discharge efficiency)) for planning and break-even. Leave 0 to use the derived per-fleet average wear.", "optimizer", null],
    ["optimizer.allow_grid_charge", "bool", true, "Allow grid charging", "", "optimizer", null],
    ["optimizer.allow_export", "bool", false, "Allow export", "Off until a feed-in tariff is configured; sell is then 0.00.", "optimizer", null],
    ["optimizer.reserve_soc_pct", "float", 0.0, "Reserve SoC", "Extra floor above battery min_soc. The cost-basis ledger and the sell floor price only the energy above it.", "optimizer", null],
    ["optimizer.max_grid_import_w", "float", 0, "Max grid import W", "0 = unlimited.", "optimizer", null],
    ["optimizer.max_grid_export_w", "float", 0, "Max grid export W", "0 = unlimited.", "optimizer", null],
    ["optimizer.min_spread_eur_per_kwh", "float", 0.0, "Min spread", "Ignore arbitrage thinner than this.", "optimizer", null],
    ["optimizer.pv_charge_preference", "str", "auto", "PV charge preference", "How the plan values PV energy it charges into the pack: auto = the export price it forgoes (default), prefer_pv = free (absorb the surplus before buying anything), avoid_pv = the slot's buy price (export the PV and charge from the grid in the cheapest hours). A shadow price for planning only: the reported plan money stays the true grid cost.", "optimizer", ["auto", "prefer_pv", "avoid_pv"]],
    ["optimizer.sell_floor_enabled", "bool", true, "Sell floor", "Refuse a discharge or export below the break-even of the energy actually in the pack (the cost-basis ledger, learned from measured charge intervals and the prices they were charged at).", "optimizer", null],
    ["optimizer.sell_floor_margin_eur_per_kwh", "float", 0.0, "Sell floor margin", "Require this much above the break-even before a sale is allowed.", "optimizer", null],
    ["optimizer.target_soc_pct_end", "float", null, "End SoC target %", "Unset = no hard terminal target.", "optimizer", null],

    ["control.tick_seconds", "int", 30, "Tick seconds", "", "control", null],
    ["control.replan_minutes", "int", 15, "Replan minutes", "", "control", null],
    ["control.grid_follow", "bool", true, "Grid follow", "Correct small grid deviations.", "control", null],
    ["control.grid_follow_deadband_w", "float", 30, "Grid deadband W", "", "control", null],
    ["control.grid_follow_max_ratio", "float", 0.5, "Grid follow max ratio", "", "control", null],
    ["control.grid_follow_max_w", "float", 0, "Grid follow max W", "Power budget for grid-follow corrections; 0 = a share of the batteries' capability.", "control", null],
    ["control.stale_state_seconds", "int", 180, "Stale state seconds", "Older measurement means hold power.", "control", null],
    ["control.stale_plan_seconds", "int", 1800, "Stale plan seconds", "Refuse to dispatch an active plan older than this.", "control", null],
    ["control.failsafe_on_error", "bool", true, "Fail safe on error", "Write 0 W on unrecoverable error.", "control", null],

    ["protection.cell_voltage_enabled", "bool", false, "Cell-voltage protection", "Watch the lowest cell voltage and charge the pack out of danger. Needs a min-cell-voltage entity mapped on the battery, or the zendure_native driver's own read; never a pack total.", "safety", null],
    ["protection.cell_voltage_warning_v", "float", 3.10, "Warning cell voltage", "Volts per cell; must be above the cutoff. Informational only.", "safety", null],
    ["protection.cell_voltage_cutoff_v", "float", 3.00, "Cutoff cell voltage", "Below this the battery is latched into an emergency charge and may not discharge.", "safety", null],
    ["protection.cell_voltage_resume_v", "float", 3.18, "Resume cell voltage", "At or above this the latch clears and normal planning resumes; must be above the cutoff.", "safety", null],
    ["protection.emergency_charge_power_w", "float", 0, "Emergency charge power W", "Charge power for a latched battery, capped by its own max charge. 0 = use max_charge_w.", "safety", null],

    ["maintenance.full_charge_enabled", "bool", false, "Scheduled full charge", "Reach the target SoC at least every interval so the BMS can balance its cells. The optimiser charges in the cheapest slots before the deadline; advisory still plans it and writes nothing.", "maintenance", null],
    ["maintenance.full_charge_interval_days", "int", 30, "Full-charge interval (days)", "How often every enabled battery must reach the target SoC.", "maintenance", null],
    ["maintenance.full_charge_target_soc_pct", "float", 100.0, "Full-charge target %", "Terminal SoC the plan aims for while a full charge is due; a higher optimizer.target_soc_pct_end still wins.", "maintenance", null],

    ["notify.enabled", "bool", true, "Notifications", "", "notify", null],
    ["notify.persistent", "bool", true, "Persistent notification", "Also create an HA persistent notification.", "notify", null],
    ["notify.mobile_service", "str", "", "Mobile service", "e.g. notify.example_mobile_app; empty = off.", "notify", null],
    ["notify.events", "list", ["plan_changed", "plan_applied", "battery_floor_reached", "negative_price", "forecast_unavailable", "driver_error"], "Notify on events", "", "notify", ["plan_changed", "plan_applied", "battery_floor_reached", "negative_price", "forecast_unavailable", "driver_error"]],

    ["batteries", "json", [], "Batteries", "Managed by the battery wizard, not the settings form.", "batteries", null]
  ];

  var db = {
    mode: "advisory",
    dry_run: true,
    override: null,
    batteryEnabled: true,
    settings: {
      // ?mock=1 demonstrates the 0.3.0 price shape (SPEC §4.1): a day_ahead provider with a
      // fixed_single contract is a *stated mismatch* — surfaced as price_warnings, never a failure
      // — and a legacy price.entity is left set but hidden by the provider, so the settings view
      // also shows the hidden-but-set warning. DEFAULTS still apply to every unset key.
      "price.provider": "day_ahead",
      "price.contract": "fixed_single",
      "price.entity": "sensor.example_legacy_price",
      // ...and the 0.4.0 solar.source choice (SPEC §7.1.1): the capacity estimate runs, while the
      // Forecast.Solar-only keys stay set but hidden by the source, so the Solar group shows the
      // same hidden-but-set line the Price group demonstrates.
      "solar.source": "capacity",
      "solar.capacity_wp": 5800,
      "solar.measured_entity": "sensor.example_pv_power",
      "solar.planes": [{ kwp: 4.0, tilt_deg: 35, azimuth_deg: 180, damping: 0.0 }],
      "solar.api_key": "example-personal-key",
      // 0.12.0 multi-source merge: the demo configures two sources so the Solar card shows the
      // weight and status columns; the single-source keys above stay set but hidden by the merge.
      "solar.sources": [
        { source: "ha_energy", weight: 2, name: "HA Energy forecast" },
        { source: "capacity", weight: 1, capacity_wp: 5800 }
      ],
      "solar.merge_strategy": "weighted_mean"
    },
    batteries: null,
    pendingHolds: [],
    modePersisted: true,
    // 0.20.0 manual limits: {"*": {...}, "<battery_id>": {...}} with charge_w/discharge_w.
    // Empty by default so ?mock=1 starts unlimited; a render-check scenario sets one.
    limits: {},
    // 0.20.4 cost-basis ledger state: "ok", "stale", "empty" or "off".
    costBasisMode: "ok"
  };

  function limitsCopy() {
    var out = {};
    Object.keys(db.limits).forEach(function (key) { out[key] = Object.assign({}, db.limits[key]); });
    return out;
  }

  // Mirrors models.resolve_manual_limit: own entry, then "*", never above the 800 W maximum.
  function mockManualLimit(batteryId, direction) {
    var key = direction === "charge" ? "charge_w" : "discharge_w";
    var own = db.limits[batteryId] || {};
    var fleet = db.limits["*"] || {};
    var value = typeof own[key] === "number" ? own[key] : (typeof fleet[key] === "number" ? fleet[key] : null);
    if (value === null || value < 0) return null;
    return Math.min(value, 800.0);
  }
  function batteryList() {
    if (!db.batteries) db.batteries = [batteryRecord()];
    return db.batteries;
  }

  // Mirrors engine.grid_resolution() (SPEC 3): the roles the add-on resolves from the Energy
  // dashboard's energy/get_prefs, plus the grid.* settings that override them. Fake namespaces
  // only (tools/audit.sh). The Meter card renders both, so an override can be told apart from the
  // fallback it replaces.
  var MOCK_ENERGY_DASHBOARD = {
    grid_power: "sensor.example_net_power",
    grid_energy_import: "sensor.example_grid_import_energy",
    grid_energy_export: "sensor.example_grid_export_energy"
  };
  function gridResolution() {
    var explicit = {};
    ["grid.power_entity", "grid.import_entity", "grid.export_entity"].forEach(function (key) {
      var value = Object.prototype.hasOwnProperty.call(db.settings, key) ? db.settings[key] : "";
      if (value) explicit[key] = value;
    });
    var source = Object.keys(explicit).length ? "settings" : "energy_dashboard_power";
    return {
      source: source,
      note: source === "settings"
        ? ""
        : "grid roles from the Home Assistant Energy dashboard (power): grid_power=sensor.example_net_power",
      explicit: explicit,
      dashboard: MOCK_ENERGY_DASHBOARD
    };
  }

  // Mirrors api.py `_descriptor_row`: keys flagged by the backend's own safety regex land in a
  // dedicated "Safety" group; the rest are Title-cased topic names.
  var MOCK_BACKEND_SAFETY_RE = /^(optimizer\.(reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export)|control\.failsafe_on_error)$/;
  function descriptorGroup(key, group) {
    if (MOCK_BACKEND_SAFETY_RE.test(key)) return "Safety";
    return group.charAt(0).toUpperCase() + group.slice(1);
  }

  // Mirrors config.py SETTINGS_META `advanced=True` (SPEC §13.3): the frozen advanced
  // classification, which the UI folds behind each group's "Advanced (n)" disclosure. The Safety
  // group is visible regardless of the flag, and a label never carries a manual "(advanced)"
  // suffix — folding is the only signal.
  var ADVANCED_KEYS = [
    "batteries", "control.grid_follow",
    "control.grid_follow_deadband_w", "control.grid_follow_max_ratio",
    "control.grid_follow_max_w", "control.replan_minutes", "control.stale_plan_seconds",
    "control.stale_state_seconds", "control.tick_seconds", "currency", "grid.export_entity",
    "grid.import_entity", "latitude_override", "load.base_kwh_per_day", "load.entity",
    "load.mode", "load.temperature_entity", "log_level", "longitude_override",
    "maintenance.full_charge_interval_days", "maintenance.full_charge_target_soc_pct",
    "notify.events",
    "notify.mobile_service", "notify.persistent", "optimizer.cycle_cost_eur_per_kwh",
    "optimizer.power_steps",
    "optimizer.salvage_multiplier", "optimizer.slot_minutes", "optimizer.soc_step_kwh",
    "optimizer.sell_floor_margin_eur_per_kwh",
    "optimizer.target_soc_pct_end", "price.bzn", "price.contract",
    "price.energy_tax_eur_per_kwh", "price.entity", "price.feed_in_entity",
    "price.feed_in_eur_per_kwh", "price.feed_in_tax_inclusive", "price.fixed_day_price_entity",
    "price.fixed_day_start_hour", "price.fixed_night_price_entity",
    "price.fixed_night_start_hour", "price.fixed_price_entity", "price.import_fee_eur_per_kwh",
    "price.scale_divisor", "price.source", "price.static_schedule", "price.unit",
    "price.vat_pct", "solar.api_key", "solar.ac_capacity_wp", "solar.azimuth_deg", "solar.bias_correction",
    "solar.capacity_wp", "solar.nasa_irradiance", "solar.performance_ratio", "solar.planes", "solar.tilt_deg",
    "solar.sources", "solar.merge_strategy",
    "solar.weather_entity", "timezone"
  ];

  function settingDescriptors() {
    return SETTINGS.slice()
      .sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; })
      .map(function (row) {
        var key = row[0];
        var value = Object.prototype.hasOwnProperty.call(db.settings, key) ? db.settings[key] : row[2];
        // The `batteries` setting is server-managed and GET /api/v1/batteries is the canonical
        // inventory, so the descriptor mirrors the fleet instead of the shipped empty default
        // (UI_REVIEW §8.3). Nothing writes this value through the settings form.
        if (key === "batteries") value = batteryList().map(function (record) { return record.config; });
        return {
          key: key,
          value: value,
          type: row[1],
          default: row[2],
          label: row[3],
          help: row[4],
          group: descriptorGroup(key, row[5]),
          choices: row[6] || [],
          advanced: ADVANCED_KEYS.indexOf(key) >= 0
        };
      });
  }

  // The effective value of a setting: an unsaved db override, else the shipped default. Used
  // by priceWarnings and by the forecast's provider/contract echo.
  function settingsValue(key) {
    if (Object.prototype.hasOwnProperty.call(db.settings, key)) return db.settings[key];
    var row = SETTINGS.filter(function (r) { return r[0] === key; })[0];
    return row ? row[2] : undefined;
  }
  // Mirrors config.py's price_warnings rule (SPEC §4.1): a provider/contract pair outside the
  // allowed table is a warning, never a failure — the engine keeps the configured provider,
  // because refusing to plan is worse than planning with a stated mismatch.
  var _PRICE_PROVIDER_CONTRACTS = {
    entity: ["dynamic_quarter_hourly", "dynamic_hourly", "fixed_single", "fixed_day_night"],
    zonneplan: ["dynamic_quarter_hourly", "dynamic_hourly"],
    day_ahead: ["dynamic_quarter_hourly", "dynamic_hourly"],
    fixed: ["fixed_single", "fixed_day_night"]
  };
  function priceWarnings() {
    var provider = settingsValue("price.provider");
    var contract = settingsValue("price.contract");
    var allowed = _PRICE_PROVIDER_CONTRACTS[provider] || [];
    if (allowed.indexOf(contract) >= 0) return [];
    return [{
      key: "price.contract",
      message: "Contract " + contract + " is not one price.provider=" + provider + " uses (" + allowed.join(", ") + "). The engine keeps the configured provider and plans with this mismatch; choose a matching contract or provider."
    }];
  }

  function validateSetting(key, value) {
    var row = SETTINGS.filter(function (r) { return r[0] === key; })[0];
    if (!row) return "unknown setting key";
    var type = row[1];
    if (type === "bool" && typeof value !== "boolean") return "expected true or false";
    if ((type === "int" || type === "float") && (typeof value !== "number" || !isFinite(value))) return "expected a number";
    if (type === "int" && typeof value === "number" && value !== Math.round(value)) return "expected a whole number";
    if (Array.isArray(row[6]) && type !== "list" && row[6].indexOf(value) === -1) return "choose one of " + row[6].join(", ");
    if (/max_grid_(import|export)_w$/.test(key) && typeof value === "number" && value < 0) return "must be 0 (unlimited) or positive";
    if (/pct$/.test(key) && typeof value === "number" && (value < 0 || value > 100)) return "must be between 0 and 100";
    return null;
  }

  // -----------------------------------------------------------------------------------
  // Savings
  // -----------------------------------------------------------------------------------

  // Source of truth: analytics.py BASELINE_NOTE, returned verbatim as `baseline_note`.
  var BASELINE_NOTE = "Baseline: the same measured consumption at the same prices with the battery doing nothing. Savings = baseline cost - actual cost.";

  // Buckets stay `null` where the backend has no load forecast to re-price against (unknown is
  // never a fabricated 0). The oldest bucket of every range and the whole `all` range demo that.
  function baselineKnown(range, i, n) {
    return range !== "all" && i < n - 1;
  }

  function savingsBuckets(range) {
    var out = [];
    var sizes = { day: 3600000, week: 86400000, month: 3 * 86400000, year: 30 * 86400000, all: 365 * 86400000 };
    var counts = { day: 24, week: 7, month: 10, year: 12, all: 3 };
    var step = sizes[range] || sizes.month;
    var n = counts[range] || counts.month;
    for (var i = n - 1; i >= 0; i--) {
      var end = startMs - i * step;
      var begin = end - step;
      var base = round(1.2 + 0.4 * Math.abs(wobble(i + n)), 2);
      var cost = round(base * 0.82, 2);
      var revenue = round(0.18 + 0.05 * Math.abs(wobble(i + 7)), 2);
      var known = baselineKnown(range, i, n);
      // A bucket is "battery measured" only when every interval was a confirmed flow reading; the
      // oldest bucket and the `all` range stay unknown so the UI always exercises the -- path.
      var batteryOk = known && i % 3 !== 0;
      var charged = batteryOk ? round(1.1 + 0.2 * Math.abs(wobble(i + 11)), 2) : null;
      var discharged = batteryOk ? round(0.9 + 0.2 * Math.abs(wobble(i + 13)), 2) : null;
      out.push({
        key: labelFor(range, begin, i),
        start: iso(begin),
        end: iso(end),
        import_kwh: round(1.6 + 0.3 * Math.abs(wobble(i)), 2),
        export_kwh: round(0.7 + 0.2 * Math.abs(wobble(i + 3)), 2),
        pv_kwh: round(4.4 + 0.6 * Math.abs(wobble(i + 5)), 2),
        cost_eur: cost,
        revenue_eur: revenue,
        baseline_cost_eur: known ? base : null,
        savings_eur: known ? round(base - (cost - revenue), 2) : null,
        cycles: round(0.4 + 0.05 * Math.abs(wobble(i + 9)), 2),
        charged_kwh: charged,
        discharged_kwh: discharged,
        charge_cost_eur: batteryOk ? round(charged * 0.18, 2) : null,
        discharge_value_eur: batteryOk ? round(discharged * 0.07, 2) : null,
        grid_measured: true,
        load_measured: known,
        pv_measured: true,
        battery_measured: batteryOk,
        has_data: true
      });
    }
    return out;
  }

  function labelFor(range, beginMs, i) {
    var d = new Date(beginMs);
    if (range === "day") return String(d.getHours()).padStart(2, "0") + ":00";
    if (range === "year") return String(d.getFullYear()) + "-" + String(d.getMonth() + 1).padStart(2, "0");
    if (range === "all") return String(d.getFullYear());
    if (range === "week") return String(d.getFullYear()) + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    return "Period " + (i + 1);
  }

  function savingsPayload(range) {
    var buckets = savingsBuckets(range || "month");
    var totals = {
      import_kwh: 0, export_kwh: 0, pv_kwh: 0, cost_eur: 0, revenue_eur: 0, cycles: 0,
      baseline_cost_eur: null, savings_eur: null,
      arbitrage_kwh: 2.1 * buckets.length, self_consumption_kwh: 3.8 * buckets.length,
      battery_throughput_kwh: 5.9 * buckets.length, buckets: buckets.length,
      charged_kwh: null, discharged_kwh: null, charge_cost_eur: null, discharge_value_eur: null,
      measured_periods: 0, baseline_periods: 0,
      measured_buckets: { grid: 0, load: 0, pv: 0, battery: 0 }
    };
    var knownBaseline = false;
    var flowSeen = { charged_kwh: false, discharged_kwh: false, charge_cost_eur: false, discharge_value_eur: false };
    buckets.forEach(function (b) {
      totals.import_kwh += b.import_kwh;
      totals.export_kwh += b.export_kwh;
      totals.pv_kwh += b.pv_kwh;
      totals.cost_eur += b.cost_eur;
      totals.revenue_eur += b.revenue_eur;
      totals.cycles += b.cycles;
      ["grid", "load", "pv", "battery"].forEach(function (name) {
        if (b[name + "_measured"]) totals.measured_buckets[name] += 1;
      });
      ["charged_kwh", "discharged_kwh", "charge_cost_eur", "discharge_value_eur"].forEach(function (key) {
        if (b[key] == null) return;
        if (!flowSeen[key]) { totals[key] = 0; flowSeen[key] = true; }
        totals[key] += b[key];
      });
      if (b.baseline_cost_eur !== null) {
        knownBaseline = true;
        totals.baseline_cost_eur += b.baseline_cost_eur;
        totals.savings_eur += b.savings_eur || 0;
      }
      if (b.has_data !== false) totals.measured_periods += 1;
      if (b.baseline_cost_eur !== null) totals.baseline_periods += 1;
    });
    if (!knownBaseline) {
      totals.baseline_cost_eur = null;
      totals.savings_eur = null;
    }
    Object.keys(totals).forEach(function (k) {
      if (typeof totals[k] === "number") totals[k] = round(totals[k], 2);
    });
    return { range: range || "month", buckets: buckets, totals: totals, baseline_note: BASELINE_NOTE };
  }

  // -----------------------------------------------------------------------------------
  // Accuracy, log, providers
  // -----------------------------------------------------------------------------------

  function accuracyReport(kind, base, spread) {
    var series = [];
    for (var i = 13; i >= 0; i--) {
      var day = new Date(Date.now() - i * 86400000);
      series.push({
        day: day.toISOString().slice(0, 10),
        mape_pct: round(base + spread * wobble(i + kind.length), 1),
        bias_pct: round(spread * 0.4 * wobble(i + 2), 1)
      });
    }
    return { kind: kind, days: 14, mape_pct: round(base + 1.0, 1), bias_pct: round(spread * 0.2, 1), mae: round(base / 8, 3), samples: 1344, series: series };
  }

  function accuracyPayload(days) {
    return {
      days: parseInt(days, 10) || 14,
      pv: accuracyReport("pv", 12.5, 3.0),
      load: accuracyReport("load", 9.0, 2.0),
      price: accuracyReport("price", 4.5, 1.0)
    };
  }

  function logEntries(limit) {
    var n = clamp(parseInt(limit, 10) || 100, 1, 500);
    var rows = [];
    var kinds = [
      { kind: "dispatch", level: "info", code: "ok", message: "Setpoint applied (synthetic)." },
      { kind: "dispatch", level: "warning", code: "soc_floor", message: "Discharge blocked by the SoC floor." },
      { kind: "dispatch", level: "warning", code: "mode_not_auto", message: "Write skipped: mode is advisory." },
      { kind: "dispatch", level: "error", code: "driver_error", message: "Read-back mismatch after write." },
      { kind: "event", level: "info", code: "plan_changed", message: "A new plan was published." },
      { kind: "event", level: "warning", code: "forecast_unavailable", message: "Solar forecast provider returned no slots." },
      { kind: "alert", level: "warning", code: "grid_import_limit", message: "Requested power would exceed the grid import limit." }
    ];
    for (var i = 0; i < Math.min(n, 14); i++) {
      var row = kinds[i % kinds.length];
      rows.push({
        ts: iso(Date.now() - i * 7 * 60000),
        kind: row.kind,
        level: row.level,
        code: row.code,
        message: row.message,
        detail: row.kind === "dispatch"
          ? { battery_id: "battery_1", requested_w: 700, applied_w: row.level === "info" ? 700 : 0, dry_run: db.dry_run, mode: db.mode }
          : { note: "synthetic mock row" }
      });
    }
    return { entries: rows };
  }

  function providersPayload() {
    return {
      solar: [{ kind: "solar", name: "capacity", active: true, ok: true, last_success: iso(Date.now() - 600000), last_error: null, stale: false, weight: 1.0, state: "ok", note: "", detail: { capacity_wp: 5800, tilt_deg: 35, azimuth_deg: 180, assumed_tilt: true, assumed_azimuth: true, performance_ratio: 0.85, cloud_attenuation: true, weather_entity: "weather.example_home", bias_correction: true, correction_applied: 1.06 } },
              { kind: "solar", name: "HA Energy forecast", active: true, ok: true, last_success: iso(Date.now() - 600000), last_error: null, stale: false, weight: 2.0, state: "ok", note: "Home Assistant solar forecast: 2 provider(s), 49 points", detail: { ha_providers: 2, ha_points: 49, entry_id: null } },
              { kind: "solar", name: "cache", active: false, ok: true, last_success: iso(Date.now() - 600000), last_error: null, stale: false, weight: null, state: "", note: "", detail: {} }],
      price: [{ kind: "price", name: "zonneplan_entity", active: true, ok: true, last_success: iso(Date.now() - 300000), last_error: null, stale: false, detail: { raw_unit: "eur_per_kwh" } },
              { kind: "price", name: "static", active: false, ok: true, last_success: null, last_error: null, stale: false, detail: {} }],
      load: [{ kind: "load", name: "statistics", active: true, ok: true, last_success: iso(Date.now() - 900000), last_error: null, stale: false, detail: { confidence: "medium" } }]
    };
  }

  // -----------------------------------------------------------------------------------
  // Routes
  // -----------------------------------------------------------------------------------

  // 0.20.4: the cost-basis ledger /status carries (SPEC 5/10): the average price paid for the AC
  // energy in the pack, the break-even sell price, the stored energy and its value, per battery.
  // Generic numbers only, never a live instance's.  db.costBasisMode selects the states the UI
  // must render distinctly: "ok" (a healthy ledger), "stale" (0.20.3's stall marker: the floor no
  // longer follows the pack), "empty" (nothing booked yet) and "off" (the sell floor is disabled).
  var MOCK_BASIS = 0.2456;
  var MOCK_BASIS_ENERGY = 5.4;
  var MOCK_BASIS_MARGIN = 0.002;
  var MOCK_BASIS_AGE_MS = 5 * 60000;
  var MOCK_BASIS_STALL_MS = 6 * 3600000;

  function costBasisRow(mode, now) {
    var stale = mode === "stale";
    var empty = mode === "empty";
    var roundTrip = round(MOCK_ETA * MOCK_ETA, 6);
    var basis = empty ? null : MOCK_BASIS;
    var energy = empty ? 0.0 : MOCK_BASIS_ENERGY;
    return {
      id: "battery_1",
      name: "Example Battery 1",
      enabled: true,
      cost_basis_eur_per_kwh: basis,
      // Floor = basis / round trip + wear + margin (models.BatteryConfig plus the configured margin).
      sell_floor_eur_per_kwh: (basis === null || mode === "off")
        ? null
        : round(basis / roundTrip + MOCK_WEAR + MOCK_BASIS_MARGIN, 6),
      energy_kwh: energy,
      stored_value_eur: basis === null ? null : round(basis * energy, 4),
      updated_at: empty ? null : iso(now - (stale ? MOCK_BASIS_STALL_MS : MOCK_BASIS_AGE_MS)),
      window_start: empty ? null : iso(now - 24 * 3600000),
      intervals: empty ? 0 : 42,
      skipped_no_price: 0,
      skipped_no_power: empty ? 0 : 1,
      skipped_no_grid: 0,
      skipped_no_soc: 0,
      skipped_no_samples: stale ? 5 : 0,
      stale: stale,
      no_samples_since: stale ? iso(now - MOCK_BASIS_STALL_MS) : null,
      sample_source: "measurements",
      reconciled_kwh: 0.0,
      reconciled_last_kwh: null,
      reconciled_resets: 0,
      journal_entries: empty ? 0 : 42,
      journal: []
    };
  }

  function costBasisPayload() {
    var mode = db.costBasisMode || "ok";
    var rows = [costBasisRow(mode, Date.now())];
    var row = rows[0];
    return {
      enabled: mode !== "off",
      margin_eur_per_kwh: MOCK_BASIS_MARGIN,
      // The fleet values are the energy-weighted means of the priced rows (engine.cost_basis_block).
      sell_floor_eur_per_kwh: mode === "off" ? null : row.sell_floor_eur_per_kwh,
      fleet_basis_eur_per_kwh: row.cost_basis_eur_per_kwh,
      batteries: rows
    };
  }

  function statusPayload() {
    var providers = providersPayload();
    return {
      version: "0.1.0",
      commit: "example00",
      uptime_s: 8412.0,
      mode: db.mode,
      dry_run: db.dry_run,
      ha_connected: true,
      ha_last_error: null,
      location: "Example Street 1",
      now: iso(Date.now()),
      price_now: 0.214,
      price_rank_pct: 33.0,
      pv_now_w: 2450.0,
      load_now_w: 810.0,
      grid_now_w: -1640.0,
      batteries: [statePayload()],
      active_plan: planPayload(36),
      last_plan_at: iso(Date.now() - 120000),
      last_tick_at: iso(Date.now() - 20000),
      next_cheap_window: [iso(Date.now() + 3 * 3600000), iso(Date.now() + 4 * 3600000)],
      next_expensive_window: [iso(Date.now() + 6 * 3600000), iso(Date.now() + 7 * 3600000)],
      override: db.override,
      manual_limits: limitsCopy(),
      cost_basis: costBasisPayload(),
      cell_voltage: {
        enabled: true,
        cutoff_v: 3.0,
        warning_v: 3.1,
        resume_v: 3.18,
        emergency_charge_power_w: 0,
        latched: ["battery_1"],
        batteries: [
          { battery_id: "battery_1", voltage_v: 3.05, fresh: true, seen: true, latched: true, block_discharge: true, force_charge_w: null, reason: "cell_voltage_guard", note: "cell voltage is below the warning threshold" }
        ]
      },
      pending_holds: db.pendingHolds.slice(),
      mode_persisted: db.modePersisted,
      alerts: [
        { ts: iso(Date.now() - 600000), level: "warning", code: "forecast_unavailable", message: "Example alert: solar forecast provider returned no slots; using cache.", detail: { source: "capacity" }, acknowledged: false },
        { ts: iso(Date.now() - 3600000), level: "info", code: "plan_changed", message: "Example alert: a new plan was published.", detail: {}, acknowledged: false }
      ],
      provider_status: providers.solar.concat(providers.price, providers.load),
      scheduler_healthy: true
    };
  }

  var routes = {
    "GET /api/v1/health": function () {
      return { status: "ok", version: "0.1.0", uptime_s: 8412.0, commit: "example00" };
    },
    "GET /api/v1/status": function () { return statusPayload(); },
    "GET /api/v1/plan": function (ctx) { return planPayload(ctx.params.hours); },
    "GET /api/v1/plan/execution": function (ctx) { return executionPayload(ctx.params.hours); },
    "POST /api/v1/plan/recompute": function () { return { queued: true }; },
    "GET /api/v1/forecast": function (ctx) {
      var kind = ctx.params.kind || "price";
      var hours = ctx.params.hours;
      if (kind === "price" && ctx.params.empty) {
        // Empty-panel demo (the frozen 0.3.0 shape): a reason slug + a one-sentence hint. With the
        // configured day_ahead provider the hint names the configured bidding zone, because
        // energy-charts publishes no prices for some valid zones (SPEC §4.1). The UI renders it
        // verbatim, so keep it a complete sentence and do not truncate it.
        var zone = settingsValue("price.bzn") || "NL";
        return {
          kind: "price", generated_at: iso(startMs), source: settingsValue("price.provider"),
          stale: true, provider: settingsValue("price.provider"), contract: settingsValue("price.contract"),
          notes: [], reason: "provider_failed",
          hint: "energy-charts published no day-ahead prices for bidding zone " + zone + "; a valid zone can simply have no data for this window — pick a different zone or a supplier source (zonneplan or a price entity).",
          slots: []
        };
      }
      if (kind === "solar") {
        // ?mock=1 shows the 0.4.0 capacity source by default (SPEC §7.1.1): an AC-coupled array
        // whose geometry is not modelled. Append `source=forecast_solar` for the per-plane API path.
        if (ctx.params.source === "forecast_solar") {
          return { kind: "solar", generated_at: iso(startMs), source: "forecast_solar", stale: false, provider: "forecast_solar", contract: null, reason: null, hint: null, planes: [{ kwp: 4.0, tilt_deg: 35, azimuth_deg: 180, damping: 0.0 }], correction_applied: 1.05, notes: [MOCK_NOTE], slots: solarSlots(hours) };
        }
        return capacitySolarPayload(hours);
      }
      if (kind === "load") {
        return { kind: "load", generated_at: iso(startMs), source: "statistics", confidence: "medium", stale: false, provider: "statistics", contract: null, reason: null, hint: null, notes: [MOCK_NOTE], slots: loadSlots(hours) };
      }
      return { kind: "price", generated_at: iso(startMs), source: "zonneplan_entity", stale: false, provider: "zonneplan", contract: "dynamic_hourly", reason: null, hint: null, notes: [MOCK_NOTE], slots: priceSlots(hours) };
    },
    "GET /api/v1/batteries": function () { return { batteries: batteryList() }; },
    "POST /api/v1/batteries": function (ctx) {
      var config = (ctx.body && ctx.body.config) || null;
      if (!config || !config.id) return { error: { code: "invalid_config", message: "config.id is required" } };
      var record = {
        config: applyEfficiencyInput(config),
        state: statePayload(config.id),
        capabilities: { max_charge_w: config.max_charge_w || 0, max_discharge_w: config.max_discharge_w || 0, signed_setpoint: true },
        mapping_resolved: true,
        discovery: discoveryCandidates("battery")
      };
      batteryList().push(record);
      return record;
    },
    "PATCH /api/v1/batteries/:id": function (ctx) {
      var found = batteryList().filter(function (b) { return b.config.id === ctx.id; })[0];
      if (!found) return { error: { code: "not_found", message: "unknown battery " + ctx.id } };
      var patch = (ctx.body && ctx.body.patch) || {};
      var pctOnly = typeof patch.round_trip_efficiency_pct === "number" &&
        typeof patch.eta_charge !== "number" && typeof patch.eta_discharge !== "number";
      Object.keys(patch).forEach(function (k) { found.config[k] = patch[k]; });
      if (pctOnly) {
        // A pct-only patch replaces inherited per-direction values with the even split.
        delete found.config.eta_charge;
        delete found.config.eta_discharge;
      }
      applyEfficiencyInput(found.config);
      if (typeof patch.enabled === "boolean") db.batteryEnabled = patch.enabled;
      return found;
    },
    "DELETE /api/v1/batteries/:id": function (ctx) {
      db.batteries = batteryList().filter(function (b) { return b.config.id !== ctx.id; });
      return {};
    },
    "POST /api/v1/batteries/:id/test": function (ctx) {
      var body = ctx.body || {};
      var intent = String(body.intent || "discharge").toLowerCase() === "charge" ? "charge" : "discharge";
      var magnitude = Math.abs(typeof body.power_w === "number" ? body.power_w : 0);
      var seconds = typeof body.seconds === "number" ? body.seconds : 0;
      if (db.mode !== "auto" || db.dry_run) {
        return { error: { code: "conflict", message: "Test discharge requires mode auto with dry run off (409)." } };
      }
      var record = batteryList().filter(function (b) { return b.config.id === ctx.id; })[0];
      if (!record) return { error: { code: "not_found", message: "unknown battery " + ctx.id } };
      var maxDischarge = typeof record.config.max_discharge_w === "number" ? record.config.max_discharge_w : 800;
      var maxCharge = typeof record.config.max_charge_w === "number" ? record.config.max_charge_w : 800;
      // Mirrors engine.test_discharge: exact seconds clamped to 1..60, explicit signed intent.
      var boundedSeconds = clamp(seconds || 5.0, 1.0, 60.0);
      var signed = clamp(intent === "charge" ? magnitude : -magnitude, -maxDischarge, maxCharge);
      return {
        started: true,
        battery_id: ctx.id,
        power_w: round(signed, 1),
        intent: intent,
        seconds: boundedSeconds,
        until: iso(Date.now() + boundedSeconds * 1000),
        applied_w: round(signed, 1),
        ok: true,
        code: "ok",
        message: "Bounded test applied (synthetic mock).",
        dry_run: false
      };
    },
    "GET /api/v1/discover": function (ctx) {
      var rows = discoveryCandidates(ctx.params.kind || "battery");
      var needle = String(ctx.params.search || "").trim().toLowerCase();
      if (needle) {
        rows = rows.filter(function (row) {
          return (row.entity_id + " " + (row.name || "") + " " + (row.platform || "") + " " + (row.suggested_role || "")).toLowerCase().indexOf(needle) >= 0;
        });
      }
      return { candidates: rows.slice(0, 50) };
    },
    "GET /api/v1/config": function () {
      return {
        options: {
          mode: "advisory", dry_run: true, log_level: "info",
          currency: null, timezone: null, latitude_override: null, longitude_override: null
        },
        settings: settingDescriptors(),
        price_warnings: priceWarnings(),
        grid: gridResolution()
      };
    },
    "PATCH /api/v1/config": function (ctx) {
      var incoming = (ctx.body && ctx.body.settings) || {};
      var applied = [], errors = [];
      Object.keys(incoming).forEach(function (key) {
        var problem = validateSetting(key, incoming[key]);
        if (problem) errors.push({ key: key, message: problem });
        else { db.settings[key] = incoming[key]; applied.push(key); }
      });
      return { applied: applied, errors: errors };
    },
    "POST /api/v1/mode": function (ctx) {
      var body = ctx.body || {};
      if (body.mode === "off" || body.mode === "advisory" || body.mode === "auto") db.mode = body.mode;
      if (typeof body.dry_run === "boolean") db.dry_run = body.dry_run;
      return { mode: db.mode, dry_run: db.dry_run };
    },
    "POST /api/v1/stop": function () {
      // Atomic stop: a real 0 W hold to every battery, even when dry_run is on, then advisory mode.
      var dry = db.dry_run;
      var cleared = !!db.override;
      db.mode = "advisory";
      db.override = null;
      var stopped = batteryList().map(function (record) {
        var enabled = record.config.enabled !== false;
        return {
          battery_id: record.config.id,
          ok: enabled,
          applied_w: 0,
          code: enabled ? "ok" : "battery_unavailable",
          message: enabled ? "Hold at 0 W written to the hardware." : "Battery is disabled; it was not stopped.",
          dry_run: dry
        };
      });
      // A battery that failed to stop keeps its last real setpoint live: report it via /status.
      db.pendingHolds = stopped.filter(function (row) { return !row.ok; }).map(function (row) { return row.battery_id; });
      db.modePersisted = true;
      return { mode: db.mode, override_cleared: cleared, stopped: stopped };
    },
    "POST /api/v1/override": function (ctx) {
      var body = ctx.body || {};
      var minutes = clamp(parseInt(body.minutes, 10) || 1, 1, 240);
      db.override = {
        power_w: typeof body.power_w === "number" ? body.power_w : 0,
        minutes: minutes,
        battery_id: body.battery_id || null,
        expires_at: iso(Date.now() + minutes * 60000)
      };
      return db.override;
    },
    "DELETE /api/v1/override": function () { db.override = null; return {}; },
    "POST /api/v1/limits": function (ctx) {
      var body = ctx.body || {};
      var target = body.battery_id ? String(body.battery_id) : "*";
      if (target !== "*" && !batteryList().some(function (record) { return record.config.id === target; })) {
        return { error: { code: "not_found", message: "unknown battery " + target } };
      }
      if (!("charge_w" in body) && !("discharge_w" in body)) {
        return { error: { code: "invalid_body", message: "charge_w and/or discharge_w is required" } };
      }
      var entry = Object.assign({}, db.limits[target] || {});
      var problem = null;
      ["charge_w", "discharge_w"].forEach(function (key) {
        if (problem || !(key in body)) return;
        var value = body[key];
        if (value === null) { delete entry[key]; return; }
        if (typeof value !== "number" || !isFinite(value) || value < 0 || value > 800) {
          problem = key + " must be 0..800 W";
          return;
        }
        entry[key] = value;
      });
      if (problem) return { error: { code: "invalid_body", message: problem } };
      if (entry.charge_w === undefined && entry.discharge_w === undefined) delete db.limits[target];
      else db.limits[target] = entry;
      return { manual_limits: limitsCopy() };
    },
    "DELETE /api/v1/limits": function (ctx) {
      var target = ctx.params && ctx.params.battery_id ? String(ctx.params.battery_id) : null;
      if (target && target !== "*") delete db.limits[target];
      else db.limits = {};
      return { manual_limits: limitsCopy() };
    },
    "GET /api/v1/savings": function (ctx) { return savingsPayload(ctx.params.range); },
    "GET /api/v1/accuracy": function (ctx) { return accuracyPayload(ctx.params.days); },
    "GET /api/v1/log": function (ctx) { return logEntries(ctx.params.limit); },
    "GET /api/v1/providers": function () { return providersPayload(); },
    "POST /api/v1/notify/test": function () { return {}; }
  };

  function splitPath(path) {
    var q = String(path || "").indexOf("?");
    var query = {};
    if (q >= 0) {
      String(path).slice(q + 1).split("&").forEach(function (pair) {
        if (!pair) return;
        var bits = pair.split("=");
        query[decodeURIComponent(bits[0])] = decodeURIComponent(bits.slice(1).join("=") || "");
      });
      path = String(path).slice(0, q);
    }
    return { path: path, query: query };
  }

  function match(method, rawPath) {
    var parts = splitPath(rawPath);
    // The SPA may be served under an ingress sub-path; strip everything before /api/v1.
    var path = parts.path;
    var marker = path.lastIndexOf("/api/v1/");
    if (marker >= 0) path = path.slice(marker);
    var direct = routes[method + " " + path];
    if (direct) return { handler: direct, id: null, params: parts.query };
    var m = /^\/api\/v1\/batteries\/([^/]+)(\/test)?$/.exec(path);
    if (m) {
      var key = method + " /api/v1/batteries/:id" + (m[2] ? "/test" : "");
      if (routes[key]) return { handler: routes[key], id: decodeURIComponent(m[1]), params: parts.query };
    }
    return null;
  }

  function handle(method, rawPath, body) {
    return new Promise(function (resolve, reject) {
      var found = match(method, rawPath);
      if (!found) {
        reject({ code: "not_found", message: "Mock has no route for " + method + " " + rawPath });
        return;
      }
      setTimeout(function () {
        var payload;
        try {
          payload = found.handler({ body: body || null, params: found.params || {}, id: found.id });
        } catch (err) {
          reject({ code: "mock_error", message: err.message });
          return;
        }
        if (payload && payload.error) {
          reject({ code: payload.error.code, message: payload.error.message });
        } else {
          resolve(payload);
        }
      }, 150);
    });
  }

  function fixtures() {
    var out = { health: routes["GET /api/v1/health"]({ params: {} }) };
    out.status = routes["GET /api/v1/status"]({ params: {} });
    out.plan = routes["GET /api/v1/plan"]({ params: { hours: "36" } });
    out.plan_execution = routes["GET /api/v1/plan/execution"]({ params: { hours: "36" } });
    out.price = routes["GET /api/v1/forecast"]({ params: { kind: "price", hours: "36" } });
    out.solar = routes["GET /api/v1/forecast"]({ params: { kind: "solar", hours: "36" } });
    out.load = routes["GET /api/v1/forecast"]({ params: { kind: "load", hours: "36" } });
    out.batteries = routes["GET /api/v1/batteries"]({ params: {} });
    out.discover = {};
    ["battery", "price", "solar", "load", "grid"].forEach(function (kind) {
      out.discover[kind] = routes["GET /api/v1/discover"]({ params: { kind: kind } });
    });
    out.config = routes["GET /api/v1/config"]({ params: {} });
    out.savings = {};
    ["day", "week", "month", "year", "all"].forEach(function (range) {
      out.savings[range] = routes["GET /api/v1/savings"]({ params: { range: range } });
    });
    out.accuracy = routes["GET /api/v1/accuracy"]({ params: { days: "14" } });
    out.log = routes["GET /api/v1/log"]({ params: { limit: "50" } });
    out.providers = routes["GET /api/v1/providers"]({ params: {} });
    return out;
  }

  root.SEM_MOCK = {
    routes: routes,
    handle: handle,
    fixtures: fixtures,
    note: MOCK_NOTE,
    // Render-check hook (0.20.4): switch the /status cost-basis fixture between the states the UI
    // must distinguish (ok/stale/empty/off).  Generic numbers only.
    setCostBasisMode: function (mode) {
      db.costBasisMode = ["ok", "stale", "empty", "off"].indexOf(mode) >= 0 ? mode : "ok";
      return costBasisPayload();
    },
    // Render-check hook (0.20.6): /status.pending_holds (a setpoint written but not yet confirmed
    // neutralised) feeds the answer's "Safety state" banner, and mode_persisted its second line.
    setSafetyState: function (pending, persisted) {
      db.pendingHolds = Array.isArray(pending) ? pending.slice() : [];
      db.modePersisted = persisted !== false;
      return statusPayload();
    }
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
