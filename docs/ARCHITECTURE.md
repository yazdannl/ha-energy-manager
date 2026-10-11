# Architecture

Short, honest map of how the add-on fits together. The frozen contract is `../SPEC.md`; the Python
code is the source of truth for behaviour, and if this file and the code disagree, this file is the
bug — please fix it in the same change.

Everything runs in one process on the Home Assistant base Python image, standard library only.
There is no build step for the UI, no message broker, no external database and no service to run
next to it.

## Component map

| Path | Responsibility |
| --- | --- |
| `app/main.py` | Bootstrap: read options, open the store, build the engine, start the HTTP server. |
| `app/config.py` | Option defaults, option/settings parsing and merging, location resolution. |
| `app/harest.py` | Home Assistant REST client (states, history, statistics, service calls), with retries and timeouts. |
| `app/haws.py` | Minimal home-grown WebSocket client (handshake, framing) for liveness/session probes and APIs that only exist over the socket. |
| `app/grid.py` | Effective grid roles: explicit `grid.*` settings first, else the Home Assistant Energy dashboard (`energy/get_prefs`), with a small cache and energy counters as the history fallback; `dashboard_roles()` exposes the dashboard's own roles even under an override, which `engine.grid_resolution()` serves to the settings panel. |
| `app/store.py` | SQLite persistence: schema migrations, plans, measurements, actuals, dispatch log, events, settings, learned profiles. |
| `app/sources/price.py` | Price forecast providers (`zonneplan_entity`, `energy_charts`, `entity_forecast`, `static`), unit normalisation and the fallback chain. |
| `app/sources/solar.py` | PV forecast provider (Forecast.Solar, keyless or with a personal key; capacity/derived model with NASA POWER observed clearness), plane summation, cache and bias correction. |
| `app/sources/load.py` | Load forecast: long-term statistics profile, level scaling, EWMA bias, optional temperature regression, flat fallback; the derived tier reads every series through statistics first (energy counters included). |
| `app/sources/measure.py` | Sampling of grid/PV/load/battery measurements into 5-minute `actuals` rows. |
| `app/optimize/dp.py` | SoC dynamic program over `optimizer.slot_minutes` slots (60 by default); deterministic, with a bounded state/action grid. |
| `app/optimize/allocate.py` | Split of the aggregate slot power across batteries, respecting per-battery limits and efficiency. |
| `app/control/safety.py` | Pure guard-rail functions: the ordered clamp/block decision for a requested power. |
| `app/control/dispatcher.py` | Tick loop: sample, replan when needed, grid-follow correction, safety, driver writes, dispatch log. |
| `app/control/drivers/` | Battery driver registry: `zendure` (translation-key discovery) and `generic` (explicit mapping), behind one small protocol. |
| `app/engine.py` | Application core: owns the threads and the shared `SystemState` the API and UI read. |
| `app/api.py` | JSON HTTP API served through Home Assistant ingress (see `../SPEC.md` section 10 for the frozen routes). |
| `app/analytics.py` | Savings and accuracy accounting against the baseline. |
| `app/notify.py` | Home Assistant notifications and persistent notices. |
| `app/web/` | Single-page UI (no build step). Vendored chart library only, with its licence kept. |

## Data flow

```
                 Home Assistant (REST + WebSocket, ingress for the UI)
                        |                              ^
    options.json ----+  |  states, history,            |  sensor.sem_* states,
    UI settings -----+  |  statistics, services        |  notifications
                        v                              |
                  +------------------------------------------+
                  |                  engine                  |
                  |                                          |
   price provider -> price forecast --+                      |
   solar provider -> solar forecast --+--> optimizer (DP) --> plan --> store (plans, plan_slots)
   load model     -> load forecast  --+          |                            |
                                                 v                            v
                                        per-battery allocation          UI / API reads
                                                 |
                                        dispatcher tick (control.tick_seconds)
                                                 |
                                  sampler -> actuals (5 min rows)
                                                 |
                                       safety.check(...)  (pure, ordered)
                                                 |
                                     driver.apply()/hold()  ---> HA service calls
                                                 |
                                        dispatch_log (incl. dry runs and rejections)
```

1. **Inputs.** Price, solar and load providers each produce gap-free, 15-minute forecasts covering
   at least the optimizer horizon. A provider that returns no future slots is a failure, not an
   empty forecast; the price chain falls back (configured → energy-charts → static) and raises a
   visible event naming the source that failed. On the sell side the price is derived by default
   (`sell = buy − price.export_fee_eur_per_kwh`, minus the slot's energy-tax component when
   `price.feed_in_energy_tax_refund` is off); an explicit `price.feed_in_eur_per_kwh` or
   `price.feed_in_entity` sensor wins, and `optimizer.allow_export` stays `false` until the user
   turns it on.
2. **Plan.** The optimizer aggregates the enabled batteries into one AC-side store (capacity-weighted
   mean efficiencies, see [Round-trip efficiency](#round-trip-efficiency-input-cost-model-and-plan-reporting))
   and runs a dynamic program over discretised energy for the horizon. The result is a `Plan` with
   per-slot power, SoC and a stable reason slug, plus a summary (cost, baseline cost, saving, cycles)
   and a per-battery snapshot of the intended split. Plans are deterministic for identical inputs and
   are stored before they are served.
3. **Dispatch.** Every tick the dispatcher samples measurements, replans when the plan is missing,
   stale or materially invalidated by new inputs, computes the current slot's power, applies the
   optional grid-follow correction, passes the request through `safety.check`, and either writes it
   through the drivers or records a dry-run entry. Nothing else in the system writes to a device.
   Sampling reads only the mapped entities with targeted state calls — never the full state snapshot
   — and upserts them into 5-minute buckets, so the stored volume is bounded (see Storage).
   A driver write is never assumed to have landed: the driver verifies by reading the device back,
   tolerates the delay until the device reports its next event (the Zendure integration only applies
   a setpoint on its next P1 event), retries, and treats a persistent mismatch as an error. Because
   the Zendure integration's own smart modes overwrite external writes, its driver also re-asserts
   the manager's manual mode while the add-on is in control, and writes a hold (0 W) setpoint on
   shutdown and on fatal error, since the integration has no watchdog of its own.
   The grid-follow correction is bounded by an independent watt budget — `max(500 W, battery
   capability × control.grid_follow_max_ratio)` — not by a fraction of the current target, which
   would be zero whenever the plan wants no battery power.
   Writes are governed by one engine control lock, the drivers' own write locks and one persisted
   per-battery **live-write latch**: ticks, mode changes and `Stop now` serialise behind the control
   lock, every driver serialises the actual device write (the exit hold included), a neutralising
   hold may only touch a battery the latch says is live, and a run that died unclean neutralises its
   outstanding setpoint at the next start, before control resumes. Advisory and dry-run installs
   therefore issue no writes at all, including on the way out, unless they really left something
   running.
4. **Feedback.** Measurements become `actuals` rows; `analytics` compares realised PV/load/price
   with what was forecast, keeps the learned corrections, and accumulates savings per day.
5. **Presentation.** The API serialises `SystemState`, plan, forecasts, savings, accuracy and log
   from the store and the engine's shared state; the UI polls it. Beside the plan itself,
   `GET /plan/execution` joins the same slot grid with the stored `actuals` rows and the
   `dispatch_log` (requested vs applied, clamped, dry run, measured kWh and SoC), so a planned window
   can be read against what was really measured and written without either side being recomputed in
   the browser. `Status` also carries
   `pending_holds` (batteries whose neutralising hold is still outstanding) and `mode_persisted`
   (false when a mode change could not be written to the settings file and a restart would come up
   in the old mode), so the UI never has to guess what is really in force. `notify.py` publishes the
   `sensor.sem_*` contract back to Home Assistant, throttled and skipped when Home Assistant is
   unreachable. Publishing uses `POST /api/states/<entity_id>` and creates no `unique_id`, so the
   mirror sensors are not part of the entity registry and disappear on a Home Assistant restart
   until the add-on republishes them on its next tick — documented behaviour, not a bug.

### Round-trip efficiency (input, cost model and plan reporting)

**Input.** A battery is configured with one number, `round_trip_efficiency_pct` (AC to AC, percent):
`eta_charge = eta_discharge = sqrt(pct/100)`. Explicit per-direction values override that split, and
the round trip is then the product, `BatteryConfig.round_trip_efficiency = eta_charge ×
eta_discharge` — not the input field. Both directions are validated to `0 < eta <= 1`.

**Cost model.** `BatteryConfig.break_even_sell_eur_per_kwh(buy, wear=None)` answers "what sale price
covers buying at `buy`?":

```
break_even_sell = buy / round_trip + wear_eur_per_kwh
```

Wear is **derived per battery**, not typed in: `wear_eur_per_kwh = price_eur /
(cycle_life × usable_kwh × eta_discharge)` (`BatteryConfig.wear_eur_per_kwh`, 0.0 while either input
is unset), the cost of one kWh **delivered** to the house. Because a full discharge delivers only
`usable_kwh × eta_discharge` AC kWh, that divisor is what makes one full cycle cost exactly
`price_eur / cycle_life`. The
advanced `optimizer.cycle_cost_eur_per_kwh` override, when non-zero, replaces the derived value and
is passed as the explicit `wear` argument. At a 20 ct buy price a 75 % round trip breaks even at
26.7 ct — not 25 ct, which is the 80 % case — before wear. A degenerate efficiency returns `None`
rather than raising.

**Planner.** The DP's energy balance already carries `eta_charge`/`eta_discharge`, so the conversion
loss is priced in the transition rather than ignored. Wear is charged on the **discharge leg only**,
per delivered AC kWh (`wear × eta_discharge × max(0, e_i − e_{i+1})`), so a charge-only schedule pays
no wear and a full cycle costs exactly `price / cycle_life`. An arbitrage charge/discharge pair is
only planned when `sell_j > buy_i / round_trip + wear_eur_per_kwh`, and a slot that
stays idle for exactly that reason reports `Reason.EFFICIENCY_LOSS` (`efficiency_loss`). That reason
is deliberately distinct from the user's own `optimizer.min_spread_eur_per_kwh` floor
(`spread_below_threshold`): one is the physics of the battery, the other is the user asking for a
wider spread. Terminal stored energy is discounted by the discharge efficiency,
`salvage = optimizer.salvage_multiplier × median(buy) × eta_discharge`, because a stored kWh delivers
only `eta_discharge` AC — at face value the pack looks more valuable than it is, and the schedule
dumps it for a trivial gain.

**Several batteries.** `BatteryAggregate.from_configs` sums capacity and the power limits, takes the
strictest SoC window and uses the **capacity-weighted mean** of `eta_charge`/`eta_discharge` for the
plan, capped at 400 DP states. The mean is a documented approximation when the packs differ
materially, so the plan reports the aggregate round trip it assumed instead of one silently invented
number. Wear is averaged over the fleet by **usable kWh**, the quantity it is charged per. Allocation
(`optimize/allocate.py`) then spends the **most efficient battery first** (ties by
battery id, deterministic) while honouring each battery's own limits. Every per-battery break-even —
the battery card, the plan's battery rows — is computed from that battery's own efficiency and wear,
so a cheap old pack and an efficient new one are never averaged into one misleading figure.

## Threads and shared state

| Thread | What it does |
| --- | --- |
| Main | Bootstrap only, then serves the HTTP API (standard-library server, one request per connection). |
| Control | The dispatcher tick: sampling, replanning, safety, driver writes, logging. Never blocks on the UI or on state publishing. |

Shared access is deliberately narrow: the engine exposes one `SystemState` snapshot (rebuilt and
replaced, not mutated in place) plus a thread-safe settings/plan interface; SQLite uses WAL with one
connection per thread, so the control thread and request threads never share a cursor. Long work
(forecast fetches, planning) happens on the control thread or in a bounded worker triggered by the
API, and the API answers from the last good snapshot while it runs — a slow provider can slow
planning, but it cannot stall the UI or duplicate a dispatch.

The WebSocket client is not on the control path: states and service calls are plain REST. It is used
for the WebSocket-only inspection commands, and it is safe to be offline for those.

## Access and authentication

Ingress is the only supported path: Home Assistant proxies the panel and supplies the login, and the
manifest publishes **no port**. The HTTP API has no authentication of its own, so publishing 8099 (a
`ports:` mapping, a reverse proxy, a host-network container) hands battery control to anyone who can
reach it — a caller could set `mode: auto` or turn `dry_run` off. That is a documented limitation,
not a configuration to encourage.

## Storage

One SQLite file in the add-on's `/data` volume, `journal_mode=WAL`, `synchronous=NORMAL`,
`busy_timeout=5000`, with a forward-only migration list and a schema version in `meta`.

Long-lived tables: `plans`/`plan_slots` (what was planned), `measurements` and `actuals` (ground
truth), `dispatch_log` (what was requested, what was applied and why not), `savings_daily` (the
accounting), `learning_profiles` (PV and load corrections, and the live-write latch), 
`forecast_cache`, `events`, `settings`.

Two changes keep the footprint small: sampling reads only the mapped entities with targeted state
calls instead of pulling the full Home Assistant state snapshot, and every tick upserts into a
5-minute bucket instead of appending a new row. At the default 30 s tick with seven mapped roles
that is about 2 016 measurement rows and roughly 20 MB of JSON per day, against about 20 160 rows and
roughly 2.4 GB per day for the snapshot-and-append approach; the volume no longer scales with the
tick rate.

Retention: measurements and actuals are trimmed to 400 days, the dispatch log to 20 000 rows, events
to 5 000 and `plans` to 200 rows (`plans_rows`), on the daily maintenance run. Savings baselines are
computed **before** trimming and stored in `savings_daily`, so the numbers stay comparable after raw
rows are gone.

### Savings baseline (the number users judge the add-on by)

`savings = baseline − actual`, where the baseline is the **do-nothing net cost** of the same realised
consumption: the import cost at the same prices minus the export revenue the same PV surplus would
have earned, with no battery flows. The export credit is included deliberately — omitting it made a
battery-less household look like it was saving money. When the load for an interval cannot be
established (no load entity and no grid sensor to derive it from), the baseline is not knowable and
both the baseline and the savings for that bucket are reported as `null` (unknown), never as zero. It
is recomputed per day from the `actuals` rows before trimming, and the same definition is shown in
the UI and in the README. It is a counterfactual re-pricing rather than a measured bill change, and
it only covers price components present in the selected price series.

## Deliberate simplifications

These are conscious trade-offs, not accidents. Each has an upgrade path.

- **No MILP / LP solver.** The optimizer is a dynamic program over a discretised SoC grid with a
  bounded action set (at most 400 states; complexity capped at roughly two million
  state-action-slot iterations, widening the step when a pack is large and logging the effective
  step). This keeps the runtime dependency count at zero and stays comfortably fast for a 36 h
  horizon. Upgrade path: a proper LP/MILP solver only if the grid has to be finer than the current
  step for correctness, which the brute-force equivalence test would have to demonstrate first.
- **AC-coupled battery model.** One aggregated AC-side store per horizon: summed power limits,
  capacity-weighted charge/discharge efficiencies and the strictest SoC window across the enabled
  batteries. There is no DC bus model, no MPPT or string model and no temperature derating. The
  per-direction efficiencies exist as an override, but one round trip per battery is the input and
  the aggregate is a single mean pair, which is exactly why it is documented as an approximation
  (see Round-trip efficiency above). Upgrade path: model each battery as its own state in the DP if
  allocation tests ever show the aggregate mis-schedules a heterogeneous fleet.
- **Only one AC power value per battery.** Allocations respect each battery's limits and prefer the
  more efficient unit when the demand does not fit everywhere, but the plan is not optimised per
  battery. The per-battery split is recorded with the plan so the log can explain it.
- **Recorder history is not enough on its own.** Load learning prefers long-term statistics, which
  Home Assistant keeps by the hour and does not purge with recorder history, so the model survives
  recorder purging. Recorder history is the fallback, and it is limited: the recorder keeps about
  ten days by default, so the "last 8 weeks" profile can only be built when statistics are
  available. Without either source the add-on falls back to a flat profile and reports
  `confidence: low` instead of pretending to know.
- **Discovery is a suggestion, not an authority.** Entity discovery ranks candidates from the
  registry and their attributes, but the user confirms the mapping and it is stored at runtime; the
  code never hardcodes a device-derived entity id. A wrong suggestion costs one confirmation
  screen, never a silent mis-write.
- **Hand-rolled WebSocket and HTTP servers.** The standard library only, so `haws.py` implements the
  frame codec itself and the API uses the standard HTTP server behind ingress. No auth is built on
  top: ingress supplies it. Upgrade path: a real ASGI server would be a new dependency for no
  user-visible gain at this scale.
- **Time resolution is a setting.** Price, PV and load providers all publish a 15-minute grid; the
  plan and dispatch grid is `optimizer.slot_minutes` (60 by default, matching the hourly dynamic
  tariffs most suppliers publish). A plan grid finer than the price feed only adds forward-filled
  slots the optimiser treats as flat, so the default matches the tariff's own slot length.
- **One process, one household.** No multi-site or multi-user support, no remote API, no MQTT.

## What it deliberately does not do

Sell your data, phone home, require an account, write YAML into your configuration, or touch
entities it was not explicitly mapped to. The only outbound requests are the enabled forecast
providers (see the privacy section of the README), and the only writes to Home Assistant are the
`input_*`/setpoint entities of batteries you mapped plus the `sensor.sem_*` contract.
