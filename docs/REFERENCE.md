# Smart Energy Manager — complete reference

One document listing **every field, setting, tab/item and formula** of the `ha-energy-manager`
add-on (shipped version **0.8.0**), as implemented in `energy_manager/app/`.

Sources, in order of authority:

| Source | What it fixes |
| --- | --- |
| `energy_manager/app/*.py` | actual behaviour (this file describes that) |
| `SPEC.md` | the frozen contract |
| `docs/UI.md`, `docs/ARCHITECTURE.md`, `docs/EXTERNAL_APIS.md` | UI tour, architecture, external API facts |

If this file and the code disagree, the code wins — please fix this file in the same change.

Contents:

1. [Product in one page](#1-product-in-one-page)
2. [UI reference (tabs, panels, cards, fields, items)](#2-ui-reference)
3. [Settings and field reference](#3-settings-and-field-reference)
4. [Logic and formulas](#4-logic-and-formulas)
5. [Published Home Assistant entities](#5-published-home-assistant-entities)
6. [HTTP API surface](#6-http-api-surface)
7. [Persistence and retention](#7-persistence-and-retention)
8. [Notifications](#8-notifications)
9. [Constant reference](#9-constant-reference)

---

## 1. Product in one page

An ingress-only Home Assistant add-on that plans battery dispatch from dynamic prices, a PV
forecast and a learned load profile, and — only when explicitly authorised — executes that plan on
configured batteries.

- **One process**, Python standard library only, one SQLite database in `/data`, one control thread
  plus the HTTP server. No build step for the UI, no MQTT, no external service.
- **Three modes** (`mode`): `off` (no planning, no writes), `advisory` (default: plans and logs, no
  writes), `auto` (writes allowed).
- **`dry_run`** (default `true`) suppresses the plan dispatcher's writes even in `auto`.
- **The only write paths** are the mapped battery setpoint entities and the `sensor.sem_*` state
  contract. `POST /api/v1/stop` is the one deliberate exception that writes while not in `auto`.
- **Horizon** default 30 h of `optimizer.slot_minutes` slots (60 min by default, so 30 slots).
  Prices, PV and load are all resampled onto that grid.

---

## 2. UI reference

Single-page app in `energy_manager/app/web/` (`index.html`, `app.js`, `charts.js`, `mock.js`,
`style.css`, vendored Bootstrap + uPlot). Hash routing, no framework, no runtime CDN.
`?mock=1` renders every view from synthetic data with no network.

### 2.1 Shell (present on every view)

| Item | Detail |
| --- | --- |
| Skip link | `Skip to content` — focuses `#view` directly (no hash change, so it never changes route) |
| Top bar — brand | `Smart Energy Manager` (compact; the build/version line now lives in the footer About disclosure as `#build-info`) |
| Top bar — connection | `#conn-status`, live region: `Connecting…` / `HA connected` / `HA unreachable` (mode and dry run are answered on Today, not repeated here) |
| Top bar — theme button | `Theme: dark` / `Theme: light`; sets `data-bs-theme` on `<html>`, persisted in localStorage, and calls `SEMCharts.refresh()` |
| Nav — primary | Four links: Today (`#/dashboard`), Plan, Savings, Manage (`#/batteries`); ≥44 px tall |
| Nav — contextual sub-row | Shown only for the current area: Plan → Plan, Forecasts; Manage → Batteries, Settings, Log; all seven routes stay real links (`aria-current` marks the exact route, the area entry stays marked) |
| Main | `#view`, `tabindex="-1"`; every view renders one `h1.route-title` + `.route-sub` via `routeTitle()` |
| Footer | `Times: local` plus an About disclosure (`#help-toggle`/`#help`): units, ISO-8601 timestamps and the build line |
| Live region | `#live`, `role="status"`, used by `performWrite()` to announce pending/confirmed writes |
| Confirm dialog | Native `<dialog>` `#confirm-dialog` — title, body, optional checkbox acknowledgement, Cancel (safe default) / Confirm |

### 2.2 Routes

| Route | View function | Query options |
| --- | --- | --- |
| `#/dashboard` | `viewDashboard` | — |
| `#/plan` | `viewPlan` | `?hours=12\|24\|36\|48` |
| `#/forecasts` | `viewForecasts` | — |
| `#/batteries` | `viewBatteries` | — |
| `#/savings` | `viewSavings` | `?range=day\|week\|month\|year\|all` |
| `#/settings` | `viewSettings` | — |
| `#/log` | `viewLog` | — |

Unknown routes fall back to `#/dashboard`. Unknown paths are normalised by
`SEM_APP.normalizeRoute()`.

### 2.3 Dashboard (Today)

Panels: **Now** (the answer strip), **Warnings and activity**, **Right now**, **Supporting
measurements** (quiet), **Next 24 hours**, **Batteries**.

**Now panel — the answer first**, in `.answer-dynamic` (re-rendered in place on every refresh):

| Line | Content |
| --- | --- |
| `.answer-authority` | `authorityText(status)` — the control authority in force (`Off` / `Advisory` / `Auto`, dry run on/off) with the dispatcher/Stop distinction |
| `.answer-measured` | `Measured <battery>: charging/discharging/idle <power>` (or "unknown"), i.e. what the device is actually doing |
| `.answer-planned` | `Planned now: <action> <power> — because: <reason>` from the current slot, or `no active plan slot`; the power is unsigned because the action names the direction, and the line ends with "(a proposal, not the measured behaviour above)" |

Then, still near the answer: the **Safety-state banner** (`pending_holds`,
`mode_persisted: false`), the override banner with `Clear override`, and any connection issue
(HA unreachable, unhealthy scheduler, missing/stale control tick).

**Controls** (`.answer-controls`, built once outside the re-rendered area so a poll cannot replace a
button mid-write):

| Item | Detail |
| --- | --- |
| Mode buttons | `off`, `advisory`, `auto` — `aria-pressed` marks the current one; `auto` asks for confirmation |
| Dry-run checkbox | `Dry run (the plan dispatcher writes nothing)`; turning it off asks for confirmation, and a refused/cancelled change restores the server's confirmed state |
| Stop now | `btn-danger`; confirmation dialog **with checkbox acknowledgement**; posts `POST /api/v1/stop` |
| Confirmed line | `.control-state`: `Confirmed by the server: mode <m> · dry run on/off` |
| Notes / results | `.answer-notes` (pending, errors, clear-override feedback) and `.answer-results` (`data-stop-result-host`, holds the stop result until dismissed) |
| Polling | Every 15 s while the tab is visible; `refreshBlocked()` pauses it while a dialog is open, a write is pending, the settings form is dirty or a stop-result banner is on screen |

Stop-result box — server response rendered verbatim: resulting mode, mode-persisted line,
outstanding holds, plus a per-battery `Battery`, `Result`, `Applied power`, `Code`, `Message`,
`Dry run` table and a `Dismiss` button.

**Warnings and activity panel** — warning/error alerts first, each with a level badge and text;
informational entries are demoted behind a `Recent activity` disclosure.

**Right now — three primary cards**: `Battery charge` (mean SoC of available batteries),
`Grid power` (`fmtPower(grid_now_w)` + importing/exporting/balanced/unknown), `Price now`
(`fmtPrice(price_now)` + the rank line). Each card can carry a visible `.card-flag` label
(`Loss` / `Unknown` / `Stale`) instead of relying on colour.

**Supporting measurements — quiet cards** (`.cards.supporting`, `panel-quiet`): PV power, load power,
next cheap window, next expensive window; `Stale prices` / `Measurements are stale` stay visible.

**Next 24 hours** — mini chart: price line + SoC line.

**Batteries panel** — one card per battery: name, SoC %, power and direction
(charging/discharging/idle/unknown), energy of capacity, temperature (°C), read age, and
`available` / `unavailable` / `stale` badges (flattened individually at the call site, stale also
greys the card).

Warnings surfaced when the last control tick is older than **180 s**
("Measurements are stale") or HA is disconnected.

### 2.4 Plan

Panels: **Plan controls**, **Plan warnings**, **Timeline**, **Plan summary**, **Next actions**,
**All slots**.

**Plan controls**

| Item | Detail |
| --- | --- |
| Horizon select | `12 hours`, `24 hours`, `36 hours`, `48 hours` — rewrites the hash (`#/plan?hours=…`) |
| Recompute | `POST /api/v1/plan/recompute`, then polls `/status` every 3 s (max 20 attempts) until `last_plan_at` changes |
| Export JSON | link to `/api/v1/export?what=plan&format=json` |
| Export CSV | link to `/api/v1/export?what=plan&format=csv` |
| Note | "Exports download from the add-on directly (no data leaves your instance)." |

**Plan warnings** — flags: `Plan is infeasible` (with `infeasible_reason`), `Stale prices`
("n of m slots carry a stale price and are treated as flat by the optimiser"), source warnings.

**Timeline** — two aligned uPlot panels: (a) price bars + long-dashed sell line with the **dotted
break-even line** `buy / round_trip + wear`; (b) PV/load areas and battery power, plus the solid SoC
line. Stale slots are dashed/grey and labelled. The legend names each signature in words, never by
colour, and a slot whose sale price sits below break-even is explained by `efficiency_loss`.

**Plan summary** leads with the answer: one line naming the saving or the loss against the
baseline (`answer-gain` / `answer-loss`), with the worked explanation behind a **Why this plan?**
disclosure. Rows: `Estimated net cost`, `Baseline net cost (no battery)`, `Saving vs baseline`,
`Energy charged`, `Energy discharged`, `Cycles`, `Arbitrage energy`, `Assumed round trip` (or `not reported`),
`Assumed wear` per kWh delivered.
Extra lines: `Charged from grid: … · charged from PV: …`, `Battery shares in this plan: <name> <W>…`,
and the efficiency note with the break-even explanation and worked example. A negative saving adds
`The plan is more expensive than doing nothing over this horizon; check the price source and settings.`

**Next actions** — a vertical `.window-list` of the contiguous action windows, so a 360 px screen
never splits the time/action/reason relationship. One summary line above the list adds the windows
up (`6 windows · 1.60 kWh out · 0.40 kWh in · costs 0.31 EUR on the grid · measured so far …`), then
each window states, in this order:

| Line | Content |
| --- | --- |
| Time | `06:45 PM – 07:15 PM (2 slots)` — the range ends when the window's last slot ends; a `now` badge marks the window containing the current time |
| Action | `Discharge · 800 W` — the action word names the direction, so the power is unsigned |
| Reason | `because: Peak discharge` (the frozen slug, rendered in words) |
| Facts (`.window-facts`, `label`/`value` pairs) | `Buy → sell` (`20.0 ct/kWh → 30.0 ct/kWh (+10.0 ct/kWh)`), `Break-even` (`buy / round_trip + wear`, with the margin above it or `efficiency not reported`), `Battery` (planned kWh in/out for the window), `SoC` (first slot's SoC → last slot's SoC, plus the SoC floor a reserve hold defends), `Grid cost` (planned import/export kWh from `load − pv + power` and what that costs or earns at the slot's own tariff) |
| Context | `PV 0.50 kW · load 0.80 kW (0.50 kW–1.10 kW band)` — the slot's forecast and the load band the optimiser saw |
| Status | what actually happened: `applied 400 W of 800 W requested (clamped: Power clamped to limits) · battery_1 −400 W · last attempt 12 min ago`, `not written: the mode is not auto`, `dry run: simulated …`, `measured −0.70 kW vs −0.80 kW planned, 1 sample over 15 of 60 min`, `not reached yet, starts in …`, or `not dispatchable: this slot has no fresh price, so the dispatcher refuses it (stale_input)` |
| Price provenance | `price from zonneplan_entity · updated 4 min ago · rank 1 of 36 by buy price (most expensive)`, or `no fresh price for this slot: the optimiser treats it as flat, and it is never dispatched` |

A stale window keeps its `stale price` badge and is never presented as dispatchable: the dispatcher
refuses a slot whose price is not fresh (`dispatcher._stale_input_reason`), so the row says so
instead of showing a plan power as if it had been applied. Windows merge only while the action,
reason, power and staleness stay identical, so a constraint change is never hidden inside a broad
summary; each slot's full figures remain in **All slots**.

**All slots** — headers: `Time`, `Buy`, `Sell`, `PV`, `Load`, `SoC`, `Battery power`,
`Action`, `Reason`, `Flags` (`stale price` badge); PV/load are kW (`fmtKw`), never labelled as
energy. Rows carry `data-stale` for styling. Long plans
render the first 48 rows with `Show all n slots` / `Show first 48 slots` toggles. Legend lists the
action vocabulary: hold, charge, discharge, pv_charge, export, self_consumption, reserve.

### 2.5 Forecasts

Panels: **Price forecast**, **Solar forecast**, **Load forecast**, **Forecast accuracy**,
**Providers**. Each forecast panel leads with a **trust line** (`Measured error: MAPE … · bias … ·
MAE … (unit) · N samples`, the MAE carrying the metric's own unit), then one quiet metadata line
(`Source: … · provider: … · contract: … · generated …`) — badges are reserved for stale/limitation
state — then the chart and a per-day totals table. A stale source is promoted to a visible banner;
load confidence is neutral text; the provider's full `notes` sit behind a **Source details**
disclosure. A day with no coverage is left blank and labelled rather than zero-filled.

| Panel | Chart | Per-day table headers |
| --- | --- | --- |
| Price forecast | buy bars + sell line, stale slots grey | `Day`, `Min buy`, `Max buy`, `Average buy`, `Average sell`, `Stale slots` |
| Solar forecast | PV area with ±uncertainty band, peak per day | `Day`, `Forecast energy`, `Peak power`, `Peak at` |
| Load forecast | P50 line with P10/P90 band | `Day`, `P50 energy`, `P10 energy`, `P90 energy` |

Empty forecasts render the server's `reason` slug plus its `hint` sentence; if the server gave no
explanation the panel says so instead of pretending the source returned nothing.

**Forecast accuracy** — three cards (`PV forecast`, `Load forecast`, `Price forecast`) each showing
MAPE over N days, `bias`, `MAE` **in that kind's unit** (kW for PV/load, EUR/kWh for price),
`samples`; plus a 14-day MAPE trend chart with one signature per series (PV solid, load long dashes,
price dotted) and the "not enough measured days" empty state.

**Providers table** — headers: `Kind`, `Provider`, `Active`, `Healthy`, `Freshness`, `Last success`,
`Last error`.

### 2.6 Batteries

Panel: **Battery fleet**. Per battery card:

| Element | Detail |
| --- | --- |
| Heading | name, driver as quiet monospace text, `enabled` as quiet text (a `disabled` badge), `stale state` / `unavailable` badges |
| Card surface | one separator-separated `.battery-card` inside the fleet panel — no nested panel; `Edit`/`Disable`/`Enable` sit in the heading actions |
| Disclosures | `Device actions (test and delete)` (with the prerequisite as real text), `Efficiency and wear`, `Entity mapping`, `Driver details` |
| Round trip | `Round trip` in percent — the single input, split evenly unless overridden |
| Wear | `price / (cycle_life × usable kWh × η_dis)` per kWh delivered, derived from the optional price/cycle-life inputs so a full cycle costs exactly `price / cycle_life`; the advanced `optimizer.cycle_cost_eur_per_kwh` override replaces it when non-zero |
| Effective stored-kWh cost | `buy / eta_charge + wear × eta_discharge` recomputed at the current buy price |
| Break-even sale price | `buy / round_trip + wear` |
| Cycle count | from `state.extra.cycles`, else `not reported` |
| Limits & SoC window | charge/discharge limits, min/max SoC |
| Mapping | list of mapped entities plus `mapping resolved` / `needs confirmation` badge |
| Live values | power, SoC, energy, temperature, read age |
| Actions | enable/disable (confirm when enabling), `Test discharge` (disabled unless `mode=auto` and dry run off; confirm + checkbox), edit, delete (confirm + checkbox) |

**Add/edit wizard**

| Field | Detail |
| --- | --- |
| `Find a battery` block | Reads `/discover?kind=battery` once on open, groups rows per device, lists only devices that could be a battery (known battery platform, or a control role plus a state role where the control entity is writable — `number`/`select`/`input_number`/`input_select`/`switch`); ranked by control-readiness, then mappable roles, then confidence. Each row shows device label, model/manufacturer, platform, mappable-role count and a `control-ready` / `partial — missing …` / `control entities are read-only` badge. `Select` fills the form below. |
| Search | Filters locally over device name, manufacturer, model, platform and entity id; falls back to the server's entity-text search when the local filter finds nothing. |
| `id` | slug, auto-slugified and made unique on selection |
| `Name` | free text |
| `Driver` | `zendure` or `generic` (auto-suggested: `zendure` on `platform: zendure_ha` or a Zendure-only role) |
| `Capacity (kWh, usable AC side)` | step 0.1 |
| `Minimum SoC (%)` / `Maximum SoC (%)` | 0–100 |
| `Max charge power (W)` / `Max discharge power (W)` | step 50 |
| Round trip efficiency (%) | one number, **90 % default**; advanced per-direction override that previews the resulting round trip and states which value wins; values making either direction 0 or > 1 are rejected inline |
| Mapping roles | one input per role in the driver's table (below); candidate rows from discovery are assignable with one click; an explicit mapping is validated before any request |
| Save | confirmation dialog listing the exact mapping |
| Candidates table | headers `Entity`, `Name`, `Platform`, `Suggested role`, `Confidence`, `Assign` |
| Discovery table (battery groups) | headers `Battery`, `Model / manufacturer`, `Platform`, `Roles`, `Use` |

**Role inputs per driver** (`BATTERY_ROLES` in `app.js`, mapping keys in
`control/drivers/__init__.py`):

| `generic` | Label |
| --- | --- |
| `power_setpoint_entity` | Signed setpoint (W, + charge / − discharge) |
| `charge_power_entity` | Charge power entity |
| `discharge_power_entity` | Discharge power entity |
| `mode_select_entity` | Mode select entity |
| `soc_entity` | State of charge sensor |
| `capacity_entity` | Capacity sensor |

| `zendure` | Label |
| --- | --- |
| `manual_mode_entity` | Manual mode select entity |
| `power_setpoint_entity` | Power setpoint entity |
| `charge_limit_entity` | Charge limit entity |
| `discharge_limit_entity` | Discharge limit entity |
| `soc_entity` | State of charge sensor |
| `soc_target_entity` | SoC target entity |
| `min_soc_entity` | Minimum SoC entity |
| `capacity_entity` | Capacity sensor |
| `available_kwh_entity` | Usable energy above the minimum SoC (kWh) |
| `pv_power_entity` | PV power sensor |
| `output_power_entity` | Output power sensor |
| `ac_mode_entity` | AC mode entity |

Non-entity role: `mode_manual_value` — `Manual mode value (e.g. manual)`.

**Test-discharge dialog** — `Discharge power (W)` (bounded by `max_discharge_w`), duration
`1–60 s` honoured at the exact seconds asked for, confirmation checkbox, and a result line rendering
the server's `applied_w`, `seconds`, `ok`, `code`, `message`. Advisory mode or dry run returns 409
with the server's message.

### 2.7 Savings

Panels: **Savings range** (controls), **Totals**, **Savings vs baseline**, **Per-bucket detail**.

| Item | Detail |
| --- | --- |
| Range select | `day`, `week`, `month`, `year`, `all` |
| Totals cards | `Savings` (baseline minus actual, net), `Import cost`, `Baseline cost`, `Import`, `Export`, `PV`, `Cycles` |
| Answer first | the saving/loss line, then `Baseline available for N of M periods`; the comparable baseline and actual net cost cover **the same periods** |
| Unknown handling | `--`, a distinct `Unknown` card flag and an explicit banner when `savings_eur` / `baseline_cost_eur` are `null`; `Loss` and `Stale` are separate flags; unknown is never rendered as zero |
| Split row | `Battery energy for arbitrage` and `Battery energy for self-consumption`, or a note that the backend does not report the split |
| Baseline banner | `How the baseline is defined` — server `baseline_note`, plus the counterfactual re-pricing caveat |
| Chart | savings bars + solid actual net cost / long-dashed baseline cost lines; null series are skipped, not plotted as 0 |
| Per-bucket table | `Bucket`, `Period`, `Import`, `Export`, `PV`, `Cost`, `Revenue`, `Baseline`, `Savings`, `Cycles` |

### 2.8 Settings

Panels: **Settings** (always-visible toolbar + Safety summary, four setup cards, collapsed
**All settings** drawer) and **Add-on options** (read-only, `Option`/`Value` table).

Always visible above the cards: the `.settings-toolbar` (`settings-search` + `settings-show-advanced`)
and the **Safety summary** (`Control authority: <mode> · dry run on/off — <authorityNote>`, a
`Review safety limits` control that opens the Safety group, and `Stop now is on Today`).

**Setup cards** — the default surface; `SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})`
is the pure table of keys a card owns for the current selection (every input the selection needs is
on the card):

| Card | Keys it renders |
| --- | --- |
| Price | `price.provider`, `price.export_fee_eur_per_kwh`, `price.feed_in_energy_tax_refund`; plus `price.entity` (`provider=entity`), `price.bzn` + `price.vat_pct` + `price.energy_tax_eur_per_kwh` + `price.import_fee_eur_per_kwh` (`day_ahead`), `price.contract` + `price.fixed_price_eur_per_kwh` (`fixed`, single) or the two day/night rates **and** the two window hours (`fixed_day_night`) |
| Meter | `grid.power_entity` (searchable picker); left empty, it resolves at runtime from the Energy dashboard. The card shows the resolved roles, labels a saved value as an override, and offers `Use the Energy dashboard` to clear every explicit `grid.*` key |
| Solar | `solar.enabled`, `solar.source`, `solar.measured_entity` unless the source is `derived`, `solar.capacity_wp` (`capacity`), `solar.planes` (`forecast_solar`) |
| Battery | the fleet from `GET /api/v1/batteries` (the canonical inventory) plus a link into the wizard |

Entity pickers ask `GET /api/v1/discover` and write the candidate row's `setting_key`
(`grid.export_entity` for an export-only sensor, and so on), falling back to the card's own key; a
typed entity id is always accepted.

**All settings drawer** — collapsed by default (`sem.settings.allOpen`), holds:

- the group rail (`.settings-rail`, side by side with the form above 720 px via
  `.settings-columns`), one group at a time, ordered
  `General, Price, Solar, Load, Grid, Optimizer, Control, Safety, Notify, Batteries`, with each
  entry counting keys that **differ from their default** (`n customised` / `defaults`) and a legend
  line saying that unsaved edits are counted in the save bar instead;
- `search` (matches key, label, help, group); while a query is active the cards hide and every match
  is listed once;
- per-group `Advanced (n)` disclosure, collapsed by default, state remembered per group;
- `Show all advanced` switch (remembered);
- a sticky `save-bar` with the **unsaved** change count, `Save` and `Revert`;
- `.settings-feedback` beside the save bar (outside the drawer) for pending/success/server-rejected
  results; a rejected key is attached to its field with `aria-describedby` + `aria-invalid` and
  focused, the field message coming from the server;
- navigation-away with unsaved changes asks first.

**Safety handling** — the `Safety` group never folds. Keys matching
`reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export|control.failsafe_on_error` are
flagged with a `money/safety` badge and require a confirmation dialog before saving. Password fields
are never echoed: an existing value shows `(a value is set; leave empty to keep it)` and is only sent
when a new value is typed. `batteries` is server-managed by the wizard: the drawer renders it as a
read-only provenance line (with the fleet count) linking to the Batteries view, never as an editable
JSON field.

**Provider/source-dependent visibility** (pure tables, so hidden fields never take effect silently —
a hidden field holding a non-default value is still surfaced):

| Selector | Visible extra keys | Note shown |
| --- | --- | --- |
| `price.provider=entity` | `price.source`, `price.entity`, `price.static_schedule` | — |
| `price.provider=zonneplan` | — (source found structurally) | tariff sensor located automatically from its price-list attribute and slot spacing |
| `price.provider=day_ahead` | `price.bzn` | wholesale, ex-VAT/ex-tax; zone coverage may legitimately be empty |
| `price.provider=fixed` | single: `price.fixed_price_eur_per_kwh`(entity); day/night: day+night rates/entities and the two window hours | fixed prices fail loudly, never guessed from the market; DST follows the window |
| `solar.source=forecast_solar` | `solar.planes`, `solar.api_key` | per-plane geometry; keyless is single-plane hourly |
| `solar.source=capacity` and `derived` | `solar.capacity_wp`, `solar.tilt_deg`, `solar.azimuth_deg`, `solar.performance_ratio`, `solar.weather_entity`, `solar.nasa_irradiance` | capacity: assumptions stated in the forecast notes; derived: nothing to enter (except the optional geometry/performance overrides), learned from the meter, conservative lower bound; NASA POWER observed clearness fills in where the weather entity has no cloud data |

Always-visible price keys: the provider/contract selectors, the adders and fees
(`price.vat_pct`, `price.energy_tax_eur_per_kwh`, `price.import_fee_eur_per_kwh`,
`price.export_fee_eur_per_kwh`, `price.unit`, `price.scale_divisor`) and the feed-in path
(`price.feed_in_eur_per_kwh`, `price.feed_in_entity`, `price.feed_in_tax_inclusive`).

Always-visible solar keys: `solar.source`, `solar.enabled`, `solar.bias_correction`,
`solar.measured_entity`.

Two guidance lines under the Solar group: `SOLAR_MEASURED_NOTE` (the measured entity must be the
inverter's AC output — a meter's export register under-reports self-consumed energy) and
`SOLAR_WEATHER_NOTE` (the weather entity must support `weather.get_forecasts`; otherwise the current
cloud cover is held flat and the notes say so).

### 2.9 Log

Panel: **Decision log**.

| Item | Detail |
| --- | --- |
| Level filter | `all levels`, `debug`, `info`, `warning`, `error` |
| Kind filter | free text, e.g. `dispatch`, `event`, `alert` |
| Rows | `50`, `100`, `200`, `500` |
| Auto-refresh | checkbox, 15 s, paused while the tab is hidden or a dialog is open |
| `Refresh` | manual reload |
| Note | "Every dispatch attempt is logged with requested vs applied power and the blocking reason, so 'why didn't it charge?' is always answerable here." |
| Presets | segmented group `All` / `Dispatch` / `Warnings and errors` / `Events` (`aria-pressed`) |
| Rows | message-first `.log-list`: the message, then badges (level, mode/dry-run context), local time + relative age (`just now` under 5 s), kind, code, `requested → applied` power and a collapsed JSON detail, newest first |
| While reading | an open detail is never rebuilt by a poll; a `New entries available` notice appears instead |

### 2.10 Cross-cutting UI rules

- **No colour-only meaning**: badges carry text, stale slots carry a `stale` badge plus dashed
  borders, alerts write the level out, and every chart series has a stable line signature named in
  words in its legend (solid = measurement/forecast, long dashes = sell/baseline, dotted = threshold).
- **Every number has a unit**: `fmtPower` (W/kW), `fmtKw` (forecast PV/load **power**), `fmtKwh`
  (energy), `fmtPrice` (EUR/kWh), `fmtEur`, `fmtPct`, `fmtDuration`, cycles, `°C`. Unknown values
  render `--`; a partial baseline states its coverage; forecast MAE uses the metric's own unit.
- **Stale data is visibly stale**: `staleBadge()` plus `.card-stale` / `[data-stale]` styling, and
  banner warnings for a control tick older than 180 s or a disconnected HA.
- **Loading and error per panel**: `asyncPanel()` gives every panel its own loading text, error
  message and retry button; each view fetches its data once via `once()`.
- **Writes are pending → confirmed**: `performWrite()` disables the trigger, announces "pending",
  counts the in-flight write, then renders the server response or the server error and re-renders
  from a fresh `GET`. No optimistic UI.
- **Charts**: `.chart-plot` + `.chart-legend` + `.chart-caption` as flow siblings (the legend is
  mounted outside the plot box so it cannot overlap what follows); semantic series names with a line
  signature in words; one tick precision and unit per axis.
- **Navigation ≠ refresh**: a route change re-renders, scrolls to the top and focuses the route
  `h1`; background polls call the view's `state.refresh()` hook and update in place, and
  `refreshBlocked()` stops them while a dialog is open, a write is pending, the form is dirty or a
  stop result is on screen. `once()` clears a rejected cache so `Retry` re-requests.
- **Dangerous actions are confirmed** via `confirmAction()` (native `<dialog>`, focus on Cancel, Esc
  cancels, optional checkbox): enabling `auto`, turning dry run off, Stop now, test discharge,
  enabling a battery, deleting a battery, safety settings, saving a battery mapping. A browser
  without `showModal` **fails closed** for checkbox-required actions instead of falling back to a
  bare `window.confirm`.
- **Honest copy about writes**: dry run is described as "the plan dispatcher writes nothing", never
  as "no hardware writes"; the autopilot note and the auto/dry-run confirms state that advisory/off
  and dry run stop *new* writes but do not neutralise a setpoint already written — Stop now is the
  way to make a device idle.
- **Relative URLs only**: the API base derives from `location.pathname`, so ingress sub-paths work.
- **No instance data in UI files**: mocks and docs use the reserved fake namespace and round numbers;
  real entity ids are resolved at runtime and stored in SQLite.

---

## 3. Settings and field reference

### 3.1 Layers and precedence

1. `DEFAULTS` (shipped values for every option and setting key).
2. Add-on options from `/data/options.json` (top-level keys only).
3. UI settings persisted in the SQLite `settings` table (dotted keys).

Later layers win. `OPTION_OVERRIDE_KEYS = ("mode", "dry_run")`: the stored value wins over the
add-on option and a mismatch is warned about, never silently flipped.

### 3.2 Add-on options (Supervisor layer)

Declared in `energy_manager/config.yaml`; every value must be non-null (Supervisor rejects explicit
nulls), while `schema:` alone carries `?` optional keys.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `list(off\|advisory\|auto)` | `advisory` | Control authority |
| `dry_run` | `bool` | `true` | Suppress dispatcher writes |
| `log_level` | `list(debug\|info\|warning\|error)` | `info` | Log verbosity |
| `currency` | `str?` | unset → read from HA | Display currency |
| `timezone` | `str?` | unset → read from HA | Timezone for windows and profiles |
| `latitude_override` | `float?` | unset → read from HA | Solar geometry |
| `longitude_override` | `float?` | unset → read from HA | Solar geometry |

Manifest facts: ingress-only (`ingress: true`, `ingress_port: 8099`, no published port),
`homeassistant_api: true`, `hassio_api: false`, startup `application`, boot `auto`, panel
`Energy` with icon `mdi:home-battery`.

### 3.3 Settings keys

Type/group/label/help/min/max/nullable/advanced come from `SETTINGS_META` in `app/config.py`, which
is the authority for the `advanced` classification (the test suite asserts the exact set both ways).

#### General

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `mode` | choice `off\|advisory\|auto` | `advisory` | — | **Mode** — off, advisory or auto; advisory never writes. |
| `dry_run` | bool | `true` | — | **Dry run** — log the writes that would happen without doing them. |
| `log_level` | choice `debug\|info\|warning\|error` | `info` | — | **Log level** *(advanced)* |
| `currency` | str, nullable | `null` | — | **Currency** *(advanced)* — unset = read from HA. |
| `timezone` | str, nullable | `null` | — | **Timezone** *(advanced)* — unset = read from HA. |
| `latitude_override` | float, nullable | `null` | −90…90 | **Latitude override** *(advanced)* |
| `longitude_override` | float, nullable | `null` | −180…180 | **Longitude override** *(advanced)* |

#### Price

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `price.provider` | choice `entity\|zonneplan\|day_ahead\|fixed` | `entity` | — | **Price provider** — where prices come from |
| `price.contract` | choice `dynamic_quarter_hourly\|dynamic_hourly\|fixed_single\|fixed_day_night` | `dynamic_hourly` | — | **Contract type** *(advanced)* — slot granularity; day/night windows for fixed |
| `price.source` | choice `zonneplan_entity\|energy_charts\|entity_forecast\|static` | `zonneplan_entity` | — | **Price source** *(advanced)* — entity provider only, with energy_charts then static fallback |
| `price.entity` | str | `""` | — | **Price entity** *(advanced)* — runtime-picked HA sensor |
| `price.static_schedule` | json | `[]` | — | **Static price schedule** *(advanced)* — list of `{from_hour, to_hour, price_eur_per_kwh, days}` |
| `price.import_fee_eur_per_kwh` | float | `0.0` | −10…10 | **Import fee** *(advanced)* — supplier markup on buy |
| `price.export_fee_eur_per_kwh` | float | `0.0` | −10…10 | **Feed-in cost** — deducted per kWh from the feed-in price |
| `price.vat_pct` | float | `21.0` | 0…100 | **VAT %** *(advanced)* — applied when a source publishes ex-VAT prices |
| `price.energy_tax_eur_per_kwh` | float | `0.0` | 0…10 | **Energy tax** *(advanced)* — applied when a source publishes ex-tax prices |
| `price.unit` | choice `auto\|eur_per_kwh\|eur_per_mwh\|micro_eur_per_kwh` | `auto` | — | **Declared unit** *(advanced)* — auto = use the provider's declared unit |
| `price.scale_divisor` | float | `1.0` | 1e-9…1e9 | **Scale divisor** *(advanced)* — for fixed-point integer providers |
| `price.bzn` | str | `"NL"` | — | **Bidding zone** *(advanced)* — energy-charts zone |
| `price.fixed_price_eur_per_kwh` | float, nullable | `null` | −1…5 | **Fixed rate** — consumer price incl. VAT and tax |
| `price.fixed_day_price_eur_per_kwh` | float, nullable | `null` | −1…5 | **Day rate** |
| `price.fixed_night_price_eur_per_kwh` | float, nullable | `null` | −1…5 | **Night rate** |
| `price.fixed_price_entity` | str | `""` | — | **Fixed rate entity** *(advanced)* — overrides the typed rate |
| `price.fixed_day_price_entity` | str | `""` | — | **Day rate entity** *(advanced)* |
| `price.fixed_night_price_entity` | str | `""` | — | **Night rate entity** *(advanced)* |
| `price.fixed_day_start_hour` | int | `7` | 0…23 | **Day starts at** *(advanced)* |
| `price.fixed_night_start_hour` | int | `23` | 0…23 | **Night starts at** *(advanced)* |
| `price.feed_in_eur_per_kwh` | float, nullable | `null` | −1…2 | **Flat feed-in price** *(advanced)* — explicit EUR/kWh received |
| `price.feed_in_entity` | str | `""` | — | **Feed-in price entity** *(advanced)* |
| `price.feed_in_tax_inclusive` | bool | `true` | — | **Feed-in incl. VAT** *(advanced)* — off = add `price.vat_pct` |
| `price.feed_in_energy_tax_refund` | bool | `true` | — | **Energy tax refunded** — on = feed-in pays the same consumer price minus the feed-in cost; off = the energy-tax component is not paid back and is subtracted too |

#### Solar

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `solar.enabled` | bool | `true` | — | **Solar forecast** |
| `solar.source` | choice `forecast_solar\|capacity\|derived` | `forecast_solar` | — | **Solar source** |
| `solar.planes` | json | `[]` | — | **PV planes** *(advanced)* — `{kwp, tilt_deg, azimuth_deg (compass: 0=north), damping}` |
| `solar.api_key` | str, nullable | `""` | — | **Forecast.Solar key** *(advanced)* — optional; keyless is single-plane hourly |
| `solar.bias_correction` | bool | `true` | — | **Bias correction** *(advanced)* |
| `solar.measured_entity` | str | `""` | — | **Measured PV entity** — inverter AC output, not a meter export register |
| `solar.capacity_wp` | float, nullable | `null` | 0…200000 | **Array watt peak** *(advanced)* |
| `solar.tilt_deg` | float, nullable | `null` | 0…90 | **Tilt** *(advanced)* — unset assumes 35° and says so |
| `solar.azimuth_deg` | float, nullable | `null` | 0…360 | **Azimuth** *(advanced)* — compass, unset assumes south |
| `solar.performance_ratio` | float | `0.85` | 0.5…1.1 | **Performance ratio** *(advanced)* |
| `solar.weather_entity` | str | `""` | — | **Weather entity** *(advanced)* — hourly cloud coverage |
| `solar.nasa_irradiance` | bool | `true` | — | **NASA irradiance** *(advanced)* — observed NASA POWER clearness per hour-of-day where the weather entity has no cloud data, and in the derived fit; cached daily per location **and timezone**, with a 3-hour backoff after a failure |

#### Load

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `load.mode` | choice `auto\|entity\|derived` | `auto` | — | **Load source** *(advanced)* — auto uses the load entity if mapped, else derived |
| `load.entity` | str | `""` | — | **Load entity** *(advanced)* |
| `load.temperature_entity` | str | `""` | — | **Outdoor temperature entity** *(advanced)* — optional regression term |
| `load.base_kwh_per_day` | float | `0.0` | 0…1000 | **Fallback daily energy** *(advanced)* |

#### Grid

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `grid.power_entity` | str | `""` | — | **Grid power entity** — signed, positive = import; enables the derived load tier |
| `grid.import_entity` | str | `""` | — | **Grid import entity** *(advanced)* |
| `grid.export_entity` | str | `""` | — | **Grid export entity** *(advanced)* |

All three default to empty. Left empty, the runtime fallback (`app/grid.py`) resolves them from the
Home Assistant **Energy dashboard** (`energy/get_prefs`, no admin, cached 300 s): the grid source's
generated signed `stat_rate` (positive = import) → `grid.power_entity`, else the two-sensor
`power_config` → `grid.import_entity`/`grid.export_entity`, else the import/export energy counters →
`grid_energy_import`/`grid_energy_export` (history only: a kWh counter cannot be sampled as live
power). Any explicit `grid.*` value replaces the fallback as a whole, and an `stat_rate_inverted`
without Home Assistant's generated net sensor is ignored rather than guessed.

The Meter card renders this provenance from `GET /api/v1/config`'s additive `grid` object
(`engine.grid_resolution()`): with no `grid.*` set it names the resolved roles ("From the Home
Assistant Energy dashboard: …"), a saved value is labelled "Set here; this overrides …" with the
dashboard's own roles beside it, and a `Use the Energy dashboard` button clears `grid.power_entity`,
`grid.import_entity` and `grid.export_entity` through the normal dirty/Save flow.

#### Optimizer

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `optimizer.slot_minutes` | int | `60` | 5…60 | **Slot minutes** *(advanced)* |
| `optimizer.horizon_hours` | int | `30` | 1…72 (clamped 12…48 by the optimizer) | **Horizon hours** |
| `optimizer.soc_step_kwh` | float | `0.02` | 0.001…5 | **SoC step** *(advanced)* — kWh per DP state |
| `optimizer.power_steps` | int | `24` | 3…200 | **Power steps** *(advanced)* — discrete actions per state |
| `optimizer.salvage_multiplier` | float | `1.0` | 0…3 | **Salvage multiplier** *(advanced)* — terminal SoC valued at multiplier × median buy price |
| `optimizer.cycle_cost_eur_per_kwh` | float | `0.0` | 0…1 | **Wear override** *(advanced)* — non-zero = EUR per kWh delivered, replaces every battery's derived wear; 0 = derive it |
| `optimizer.allow_grid_charge` | bool | `true` | — | **Allow grid charging** |
| `optimizer.allow_export` | bool | `false` | — | **Allow export** — off = never export on purpose |
| `optimizer.reserve_soc_pct` | float | `0.0` | 0…100 | **Reserve SoC** — extra floor above battery `min_soc` |
| `optimizer.max_grid_import_w` | float | `0` | ≥ 0 | **Max grid import W** — 0 = unlimited |
| `optimizer.max_grid_export_w` | float | `0` | ≥ 0 | **Max grid export W** — 0 = unlimited |
| `optimizer.min_spread_eur_per_kwh` | float | `0.0` | 0…10 | **Min spread** — ignore arbitrage thinner than this |
| `optimizer.target_soc_pct_end` | float, nullable | `null` | 0…100 | **End SoC target %** *(advanced)* — unset = no hard terminal target |

#### Control

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `control.tick_seconds` | int | `30` | 5…3600 | **Tick seconds** *(advanced)* |
| `control.replan_minutes` | int | `15` | 1…1440 | **Replan minutes** *(advanced)* |
| `control.grid_follow` | bool | `true` | — | **Grid follow** *(advanced)* |
| `control.grid_follow_deadband_w` | float | `30` | 0…10000 | **Grid deadband W** *(advanced)* |
| `control.grid_follow_max_ratio` | float | `0.5` | 0…1 | **Grid follow max ratio** *(advanced)* |
| `control.grid_follow_max_w` | float | `0` | ≥ 0 | **Grid follow max W** *(advanced)* — 0 = derive from capability × ratio (min 500 W) |
| `control.stale_state_seconds` | int | `180` | 5…3600 | **Stale state seconds** *(advanced)* — older measurement means hold power |
| `control.stale_plan_seconds` | int | `1800` | 60…86400 | **Stale plan seconds** *(advanced)* — refuse to dispatch an older active plan |
| `control.failsafe_on_error` | bool | `true` | — | **Fail safe on error** — write 0 W on unrecoverable error |

#### Safety (never folded)

`control.failsafe_on_error` is the one `control.*` key that lives in the Safety group. The five safety
keys the UI never hides:

`optimizer.reserve_soc_pct`, `optimizer.max_grid_import_w`, `optimizer.max_grid_export_w`,
`optimizer.allow_export`, `control.failsafe_on_error`.

#### Notify

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `notify.enabled` | bool | `true` | — | **Notifications** |
| `notify.persistent` | bool | `true` | — | **Persistent notification** *(advanced)* |
| `notify.mobile_service` | str | `""` | — | **Mobile service** *(advanced)* — e.g. `notify.example_mobile_app`; empty = off |
| `notify.events` | multi_choice | all six (below) | — | **Notify on events** *(advanced)* |

`notify.events` choices: `plan_changed`, `plan_applied`, `battery_floor_reached`, `negative_price`,
`forecast_unavailable`, `driver_error`.

#### Batteries

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `batteries` | json | `[]` | *(advanced)* Managed by the battery wizard, not the settings form |

#### JSON-typed settings (validation rules)

`price.static_schedule` — a list of objects; each needs `price_eur_per_kwh` and accepts
`from_hour` (default 0), `to_hour` (default 24) and `days`. Hours must be within 0…24; a window with
`from_hour > to_hour` wraps overnight (e.g. 23 → 7). `days` is a list of either integers 0…6 or
three-letter names (`mon`…`sun`); an empty/absent list means every day. The first matching entry wins.

`solar.planes` — a list of objects; each needs `kwp` (> 0) and accepts `tilt_deg` (default 35, 0…90)
and `azimuth_deg` (default 0, 0…360, **compass bearing**: 0 = north, 90 = east, 180 = south,
270 = west) plus an optional `damping`. For the Forecast.Solar API, azimuth is converted by
`azimuth % 360 − 180` (0 = south, −90 = east, +90 = west) and tilt is sent as 0 = horizontal,
90 = vertical; `damping` may be a single value, a `[morning, evening]` pair, or
`{morning, evening}`, each clamped to 0…1.

`batteries` — a list of battery objects, validated per §3.4 and edited through the Batteries view.

**Group names in the API** — `SETTINGS_META.group` is lowercase; `GET /api/v1/config` title-cases it
(`general` → `General`), and moves exactly five keys into the `Safety` group regardless of their
`SETTINGS_META` group: `optimizer.reserve_soc_pct`, `optimizer.max_grid_import_w`,
`optimizer.max_grid_export_w`, `optimizer.allow_export`, `control.failsafe_on_error`.

**Secrets are never echoed** — any settings key whose name contains `key`, `token`, `secret`,
`password` or `passwd` is reported with type `password` and its value replaced by `***` when set (for
example `solar.api_key`). The form shows `(a value is set; leave empty to keep it)` and only sends a
new value when the user types one.

#### Advanced (never folded) set — frozen

`mode`, `dry_run`, `notify.enabled`, `price.provider`, `price.export_fee_eur_per_kwh`,
`price.feed_in_energy_tax_refund`, `price.fixed_price_eur_per_kwh`,
`price.fixed_day_price_eur_per_kwh`, `price.fixed_night_price_eur_per_kwh`, `grid.power_entity`,
`solar.enabled`, `solar.source`, `solar.measured_entity`, `optimizer.horizon_hours`,
`optimizer.allow_grid_charge`,
`optimizer.min_spread_eur_per_kwh`, plus the five Safety keys. Everything else is `advanced: true`
(this includes the advanced wear override `optimizer.cycle_cost_eur_per_kwh` and
`solar.nasa_irradiance`).

### 3.4 Battery configuration fields (`models.BatteryConfig`)

| Field | Type | Default | Validation / meaning |
| --- | --- | --- | --- |
| `id` | str | required | stable slug, unique |
| `name` | str | — | display name |
| `driver` | str | required | `zendure` or `generic` |
| `capacity_kwh` | float | required | must be > 0; usable AC-side capacity |
| `min_soc_pct` | float | `10.0` | must be < `max_soc_pct`, both in 0…100 |
| `max_soc_pct` | float | `100.0` | — |
| `max_charge_w` | float | `800.0` | at least one of charge/discharge must be > 0 |
| `max_discharge_w` | float | `800.0` | — |
| `eta_charge` | float | `0.95` | must be in (0, 1] |
| `eta_discharge` | float | `0.95` | must be in (0, 1] |
| `price_eur` | float | `0.0` | ≥ 0; 0 = unknown purchase price, no derived wear |
| `cycle_life` | float | `0.0` | ≥ 0; 0 = unknown rated cycles, no derived wear |
| `control_path` | str | `auto` | how the driver must write: `auto` (driver preference), `manager` (the Zendure manager's system-wide `manual_power`) or `device` (the inverter's own `ac_mode` + `input_limit`/`output_limit`). Validated at save time: `manager` needs `manual_power_entity` + `operation_entity`, `device` needs `charge_limit_entity` + `discharge_limit_entity`. Ignored by drivers without paths of their own |
| `mapping` | dict | `{}` | must be non-empty: "confirm the entity mapping in the UI" |
| `enabled` | bool | `true` | disabled batteries are excluded from aggregate, allocation and safety |

Derived/validated helpers:

```
usable_kwh()        = max(0, capacity_kwh * (max_soc_pct - min_soc_pct) / 100)
energy_bounds_kwh() = (capacity_kwh * min_soc_pct / 100, capacity_kwh * max_soc_pct / 100)
round_trip_efficiency = eta_charge * eta_discharge
wear_eur_per_kwh    = price_eur / (cycle_life * usable_kwh * eta_discharge)
                                                              (0.0 when either input is unset)
break_even_sell_eur_per_kwh(buy, wear=None)
                    = buy / round_trip + (wear or wear_eur_per_kwh)   (None if degenerate)
validate()          -> list of human-readable problems; empty means usable
```

Input convenience (`config.py`): `round_trip_efficiency_pct` is split evenly —
`eta_charge = eta_discharge = sqrt(pct/100)`. Explicit per-direction values override the split, and
then the round trip is their product (not the input field). `wear_eur_per_kwh` is likewise derived on
every read-back (`battery_payload`) and accepted-but-ignored on a patch, like
`round_trip_efficiency_pct`.

### 3.5 Discovery API fields

`GET /api/v1/discover?kind=battery|price|solar|load|grid` returns rows with
`entity_id`, `name`, `unit`, `device_class`, `state`, `platform`, `suggested_role`, `confidence`,
plus:

- `min` / `max` when the state attributes hold finite numbers;
- `setting_key` — the settings key a confirmed role is saved to (`grid.*` for `grid`,
  `price.entity`, `solar.measured_entity`, `load.entity` / `load.temperature_entity`; `null` when a
  role has no key);
- for `kind=battery`: `device_id` and, when the device registry is reachable, `device_name`,
  `manufacturer`, `model` (all nullable).

Rows are capped at **50** per request with an optional `search` filter; results are cached for
`DISCOVER_CACHE_S = 60 s`.

---

## 4. Logic and formulas

### 4.1 Conventions

- **Sign**: AC-side battery power `p` is **positive = charging**, negative = discharging. Grid is
  positive = import.
- **Prices** are EUR/kWh, tax included, at the meter; `sell` is EUR/kWh **received**.
- **Energy** is kWh AC-side unless stated; power W everywhere in the API/UI, kW in the optimizer.
- **Time**: `optimizer.slot_minutes` slots (60 min by default), floored to that step; all
  timestamps ISO-8601 with offset.
- **Determinism**: identical inputs must produce byte-identical plans — no dict/set iteration order
  may leak into results.

Core identity used everywhere:

```
grid_kw = load_kw − pv_kw + p_kw                     # p > 0 raises import
cost_i  = dt * ( buy * max(grid, 0) − sell * max(−grid, 0) )
```

`models.PlanSlot.grid_kw()` and `PlanSlot.cost_eur(dt)` are the frozen helpers; the optimizer's
`_summary` calls them so a reported plan cost can never diverge from the shipped helper.

### 4.2 Price formulas

**Unit normalisation** — `models.eur_per_kwh_from_raw(value, unit, scale_divisor, vat_pct, adder)`:

```
scaled = value / scale_divisor
eur_per_mwh     -> scaled /= 1000
micro_eur_per_kwh -> scaled /= 1_000_000
if vat_pct: scaled *= 1 + vat_pct/100
result = scaled + adder
```

Magnitude-based guessing is forbidden; `unit="auto"` means "the provider declared its own unit", and
callers must pass a concrete unit.

**Buy price assembly** (`sources/price.py::_normalise_pairs`):

```
buy = eur_per_kwh_from_raw(raw, unit, divisor, vat, tax) + price.import_fee_eur_per_kwh
slot.energy_tax = per-slot tax component when the provider publishes a tax-excluded price,
                  else price.energy_tax_eur_per_kwh for wholesale sources, else 0
```

Per-slot tax component (`_entry_tax_component`) =
`max(0, price_incl_tax − price_excl_tax)` in EUR/kWh (Zonneplan publishes both sides).

**Resampling** (`_build_forecast`): provider entries are floored to the plan grid step
(`_grid_start`, i.e. `quarter_floor(now, optimizer.slot_minutes)` - the plan's own first slot, so the
dispatcher never sees a filled *current* slot); entries before that start are dropped; the 15-minute
grid `[start, start + horizon_hours)` is filled by exact match, else the last known earlier value
marked `stale=True`. A source that yields **no future slots is a
failure**, not an empty forecast (`ProviderError`). An empty entry list where every entry is in the
past is reported as `no future price slots`.

The fallback chain's acceptance window is the *current quarter* onwards (`load_price_forecast`): a
provider must still have something in the future, whatever the plan grid's step is.

**Fallback chain** (`_chain(configured)`): `configured → energy_charts → static`; a failure raises a
`forecast_unavailable` event naming the source. When every future slot is stale, the optimizer treats
the price as flat (equal prices) rather than guessing.

**Coverage** (`_apply_coverage`): the uncovered tail is marked stale and the forecast `degraded`,
with a note naming the coverage end; a gap is *not* a provider failure. Coverage is measured with the
*fresh* slots' own cadence (60 min for an hourly feed), so a forecast that reaches the horizon is not
reported as a short tail. For HA consumer sensors
(`zonneplan`, `entity_forecast`), less than one hour of fresh coverage marks the whole list stale.

**Provider → source resolution** (`_resolve_source`, `_locate_zonneplan_entity`,
`_select_grid`, `_grid_minutes`, `_preferred_grid`):

| `price.provider` | Resolves to | Allowed contracts |
| --- | --- | --- |
| `entity` | `price.source` + `price.entity`, chain configured → energy_charts → static | any |
| `zonneplan` | a Zonneplan tariff sensor found structurally: an attribute holding a fixed-point price list (`price_tax_included` / `price_tax_excluded`, `value_factor 1e-7`) whose own state agrees with its current slot | `dynamic_*` |
| `day_ahead` | `energy_charts` for `price.bzn` (keyless) | `dynamic_*` |
| `fixed` | the configured flat or day/night tariff; **no market fallback** | `fixed_*` |

Zonneplan grid detection is structural from slot spacing (15-minute vs hourly), never from the entity
name; falling back to the other grid is recorded as a note. A provider/contract combination outside
the table is a **warning** (`price_warnings` in `/api/v1/config`), not a failure.

**Fixed tariff** (`_fixed_rate`, `fetch_fixed_tariff`): consumer prices incl. VAT and tax, no adders.
An entity (`price.fixed_*_entity`) wins over the typed value. Day window =
`price.fixed_day_start_hour` until `price.fixed_night_start_hour`, evaluated per slot in the
configured timezone, so DST follows the window and not an hour count. A missing fixed price is a
`forecast_unavailable` failure with a hint naming the field.

**Static schedule** (`_static_price`): the first entry whose `days` (when given) contain the slot's
local weekday and whose 0…24 hour window contains the slot's local hour; an inverted window wraps
overnight. Returns `None` when nothing matches.

**Feed-in (sell) resolution** (`_apply_feed_in`), once per forecast:

1. `price.feed_in_entity` — live HA sensor, its declared unit.
2. `price.feed_in_eur_per_kwh` — flat tariff.
   Both: `sell = tariff − price.export_fee_eur_per_kwh`, and `tariff *= 1 + price.vat_pct/100` when
   `price.feed_in_tax_inclusive` is off.
3. Derived (default): `sell = buy − price.export_fee_eur_per_kwh`, and additionally
   `− slot.energy_tax` when `price.feed_in_energy_tax_refund` is off.

A configured feed-in source that cannot be read degrades to the derived price with a note — pricing
export must never fail the buy forecast. Only an explicit `price.feed_in_eur_per_kwh = 0` forces a
zero sell price. `optimizer.allow_export` defaults to off, so nothing exports until the user asks.

### 4.3 Solar formulas

#### 4.3.1 `solar.source = forecast_solar` (Forecast.Solar API)

- Per configured plane, request the keyless v1 estimate endpoint with `?time=utc`; sum planes on the
  common 15-minute grid.
- **Keyless = hourly, single plane, ~2-day horizon** → the hourly curve is linearly interpolated onto
  the 15-minute grid (the first partial sample stays anchored at its own timestamp). A personal key
  in the URL path enables multiple planes (`MAX_PLANES = 4`).
- Raw responses are cached in `forecast_cache` with `CACHE_TTL_S = 30 min`; on HTTP 429 the provider
  backs off exponentially (`RATE_LIMIT_BASE_S = 60 s` up to `RATE_LIMIT_MAX_S = 1800 s`, keyless limit
  12 requests/hour/IP) and serves cache.
- Output: `SolarSlot(pv_kw, uncertainty_pct)`, default uncertainty **25 %** until learned.
- Azimuth conversion: API azimuth = compass − 180 (0 = south); declination 0–90 (90 = vertical).
- The response `message.info` field must never be logged (it echoes coordinates).

#### 4.3.2 `solar.source = capacity` (astronomy model)

Per 15-minute slot, with latitude/longitude/timezone:

**1. Solar position** (Cooper's declination, equation of time, longitude correction):

```
declination = 23.45° * sin( 2π * (284 + doy) / 365 )
gamma = 2π * (doy − 1) / 365
# the code writes the fractional coefficients as integers over _EOT_SCALE = 1e6
EoT_min = (229.18 / 1e6) * ( 75 + 1868*cos(gamma) − 32077*sin(gamma)
                             − 14615*cos(2*gamma) − 40849*sin(2*gamma) )
true_solar_minutes = clock_minutes + EoT_min + 4*longitude − utc_offset_minutes
hour_angle = radians(true_solar_minutes / 4 − 180)
sin(elevation) = sin(lat)*sin(decl) + cos(lat)*cos(decl)*cos(hour_angle)
elevation <= 0  ->  pv_kw = 0
```

**2. Clear-sky GHI (Haurwitz) and transposition to the plane:**

```
GHI = 1098 * cos(z) * exp(−0.059 / cos(z))                      # W/m², z = zenith
diffuse = 0.25 * GHI                                            # constant diffuse fraction
beam    = (GHI − diffuse) * max(0, cos(AOI)) / cos(z)
POA     = beam
        + diffuse * (1 + cos(tilt)) / 2                         # isotropic sky
        + GHI * 0.2 * (1 − cos(tilt)) / 2                       # ground albedo 0.2
```

`cos(AOI)` is the standard transposition formula; the setting is a compass azimuth, converted by
`azimuth_from_south = (azimuth_deg + 180) mod 360`.

**3. Cloud attenuation (Kasten-Czeplak)** when a weather entity yields coverage for the slot:

```
k = 1 − 0.75 * (cloud_pct / 100) ^ 3.4
```

Hourly forecast values cover the four slots of their hour. Without cloud data the estimate is
clear-sky and the forecast says so.

**4. Power and the learned correction:**

```
pv_kw = (capacity_wp / 1000) * (POA * k / 1000) * performance_ratio * bias_factor[local_hour]
```

`bias_factor` comes from the shared solar bias learning: EWMA with `alpha = 0.2`, clamped to
`[0.3, 1.8]`, 24 hourly buckets, stored in `learning_profiles` (`solar_bias`/`hourly`), decaying
toward 1.0 over `BIAS_DECAY_DAYS = 14`.

Uncertainty: **35 %** without cloud data, **25 %** with it. Notes always state the capacity used, the
tilt/azimuth actually assumed when left unset, the performance ratio, whether cloud attenuation was
applied and from which entity, and that a measured PV entity refines the curve. A missing capacity is
a failure with a hint naming `solar.capacity_wp` — never a guess.

#### 4.3.3 `solar.source = derived` (meter-learned capacity)

Because `grid = load − pv + p`, whenever the signed grid power is negative the array is producing at
least `pv = −grid + p` (`p > 0` = charging). Samples come from the same roles the derived load tier
uses (`grid.power_entity` or the import/export pair, else the Energy-dashboard roles — its power
sensor, or the import/export energy counters as `grid_energy_import`/`grid_energy_export` — plus the
battery's AC flow); a battery whose flow
cannot be read refuses the tier rather than letting its discharge read as PV. With a WebSocket client
each series comes from long-term statistics (hourly `mean` for a power statistic, hourly `change` for
an energy counter, converted to average kW), so the window is the never-purged statistics table and
not the recorder's purge window; recorder history is the fallback. Stamps without export
carry no information and are skipped; the house load at those moments is **not** subtracted, so every
sample is a lower bound.

```
ratio_i   = pv_sample_kw / ((POA_i / 1000) * performance_ratio / 1000)
capacity  = 90th percentile of ratio_i            # the cloudless envelope
```

Requirements: at least **24** usable daylight samples (`DERIVED_MIN_SAMPLES`) and a non-negligible
model output per installed watt (`> 1e-5 kW/Wp`); fewer samples is a status naming the meter, never a
crash. An explicit `solar.capacity_wp > 0` wins over the learned value, which is stated in the notes
and status (`capacity_source`, `derived_samples`) but never written back to settings. The fit is
cached in `learning_profiles` (`solar_derived`/`capacity`) keyed by the entity-mapping fingerprint and
refit at most once per `DERIVED_REFIT_HOURS = 24 h`. The forecast itself is then the capacity model,
so tilt/azimuth/performance ratio/weather apply unchanged.

`solar.source=forecast_solar` with an empty `solar.planes` runs this estimate as a fallback (source
selection, §4.3.1): the returned forecast's source becomes `derived` and a note says the planes are
empty. When no meter resolves either, the status stays `no_pv_source` and
`status.detail.derived_fallback` carries the derived reason for the hint.

### 4.4 Load formulas

Priority order: **long-term statistics** (60 days, `hour`, `haws.statistics_during_period`) →
**recorder history** (`GET /api/history/period`) → **derived** → **fallback flat profile**.

The derived tier reads each series through long-term statistics when a WebSocket client is available
(an energy counter's hourly `change` is converted to average kW), so a recorder with the default
10-day purge still yields weeks of samples — and an Energy dashboard with only kWh counters still
learns.

**Derived tier** (no load entity mapped):

```
load = grid_import − grid_export + pv + discharge − charge        # kW, AC side
```

An interval is learned only when every *required* series has a sample for that stamp: a missing PV or
battery-flow stamp is skipped, never read as zero. The tier is refused when PV is unmapped (net grid
import is not household load), and also when a configured battery's flow could not be mapped.

**Profile learning** (`build_profile`, `WINDOW_DAYS = 56`, 48 buckets = weekday/weekend × hour):

```
bucket[weekend|weekday, hour] : median, p10, p90            # medians are the profile
profile_mean = (5 * weekday_median + 2 * weekend_median) / 7
scale        = clamp(recent_7d_mean / profile_mean, 0.5, 2.0)
bias[bucket] = EWMA(alpha 0.2) over the last 7 days of (day mean / bucket median),
               each ratio clamped to [0.5, 2.0]
temperature  : least-squares fit  load = a + b*T  (applied only when r² >= 0.05)
```

Prediction per hour:

```
value = median[bucket] * scale * bias[bucket]
if slope and r2 >= 0.05 and T is known: value += b * (T − t_mean)
value = max(0, value)
```

Hourly values are linearly interpolated onto the 15-minute slot grid (`p10`/`p90` become the band).
Fallback when nothing is learned: `load.base_kwh_per_day` spread by `DEFAULT_SHAPE`
(24 relative weights, normalised) and `confidence: low`.

**Confidence**: `low` with no profile or fewer than `24 * 2` samples; `medium` under 14 days;
`high` at 14+ days and enough samples.

### 4.5 Measurement sampling

Every control tick, mapped roles are read with targeted per-entity state calls (never a full state
snapshot) and upserted into **5-minute** buckets (`MEASUREMENT_MINUTES = 5`).

Roles: `pv_power`, `load_power`, `price`, `grid_power`, `grid_import`, `grid_export`, and per battery
`battery_power:<id>`, `battery_charge:<id>`, `battery_discharge:<id>`, `battery_pv:<id>`,
`battery_output:<id>`, `battery_soc:<id>`.

Unit handling: power → W (`W`, `kW`, `MW`), SoC → %, price → EUR/kWh. The tick's `actuals` row is:

```
grid_kw    = grid_power / 1000, or (grid_import − grid_export)/1000 when only the pair is mapped
pv_kw      = pv_power / 1000
load_kw    = load_power / 1000
battery_kw = sum(battery power) / 1000
soc_kwh    = Σ (soc_pct/100 * capacity_kwh)
price_buy  = price role
```

Sampling must never be skipped because a write failed — these rows are the ground truth for savings
and accuracy.

### 4.6 Battery aggregation

`BatteryAggregate.from_configs` (DP horizon model, at most `MAX_STATES = 400` states):

```
E_cap   = Σ capacity_kwh * (max_soc − min_soc)/100
E_min   = strictest floor across enabled batteries, E_max = strictest ceiling
P_chg   = Σ max_charge_w / 1000            P_dis = Σ max_discharge_w / 1000
eta_chg, eta_dis = capacity-weighted means
wear, EUR/kWh delivered = delivered-energy-weighted mean (usable x eta_discharge) of each battery's wear
round_trip = eta_chg * eta_dis             # the aggregate mean pair is a documented approximation
soc_pct(e) = clamp(e / capacity_kwh * 100, 0, 100)
```

Disabled batteries are excluded everywhere. Zero enabled batteries still yield a do-nothing plan.

### 4.7 Optimizer (dynamic program)

**Model** — discrete slots `dt = optimizer.slot_minutes / 60` over `optimizer.horizon_hours`
(clamped 12…48 h). Per slot: `pv_i` kW, `load_i` kW, `buy_i` EUR/kWh, `sell_i` EUR/kWh.

**Objective**:

```
minimise  Σ cost_i  +  wear * Σ delivered_kwh_i  −  salvage * e_N
delivered_kwh_i = max(0, e_i − e_{i+1}) * eta_dis     # wear on the discharge leg only
cost_i  = dt * ( buy * max(grid,0) − sell * max(−grid,0) )
          [+ min_spread * max(0, p_i − pv_surplus) * dt   when min_spread > 0 and p_i > 0]
grid_i  = load_i − pv_i + p_i
salvage = optimizer.salvage_multiplier * median(buy over horizon) * eta_dis
```

`wear` is the {aggregate|override} per-delivered-kWh wear defined in §3.4; a charge-only schedule
pays none and a full cycle costs `price / cycle_life`.

`min_spread` is charged only to energy that has to come from the grid (charging above the PV surplus).

**Constraints**:

```
E_min <= e_i <= E_max
−P_dis <= p_i <= P_chg                       (p_hi additionally capped to PV surplus when
                                              allow_grid_charge is false)
grid_i <= max_grid_import_w/1000             (when set)
−grid_i <= max_grid_export_w/1000            (when set)
export only when allow_export and sell > 0   (exporting at an unknown/zero sell price is never planned)
energy transition:  e_{i+1} = e_i + dt*p_i*eta_chg     (p_i >= 0)
                    e_{i+1} = e_i + dt*p_i/eta_dis     (p_i < 0)   # exact at p = 0
optimizer.target_soc_pct_end : hard terminal constraint when set
```

**DP mechanics**:

- State grid: `E_min → E_max` step `optimizer.soc_step_kwh`, anchored on the current and target
  energy, coarsened until `states <= 400` (the effective step is logged and reported).
- Action set per state: `optimizer.power_steps` evenly spaced values in the feasible `[p_lo, p_hi]`
  after clipping by SoC reachability and grid limits; `0` is always included when feasible, and the
  exact reachable power is added as an extra action where valid.
- Transition cost uses the interpolated value function at the exact reachable energy, so
  quantisation does not bias the decision; the path is reconstructed from per-(slot, state) argmins.
- Complexity target `slots * states * actions <= 2 000 000`; exceeding it widens the step.
- Terminal target: tried hard (`TARGET_HARD_PENALTY = 1e6`); if the terminal error exceeds half the
  effective step it is retried soft (`TARGET_SOFT_PENALTY = 2.0`) and the plan is marked
  `feasible: false`, `infeasible_reason: terminal_target_unreachable`.
- Grid-limit violations found in the reconstructed plan yield
  `infeasible_reason: grid_limits_infeasible`. No slots yields `no_forecast_slots`.

**Arbitrage feasibility** — an arbitrage pair is planned only when the sale clears the round-trip
break-even:

```
sell_j > buy_i / round_trip + wear
```

where `buy_i` is the cheapest buy price at or before the slot. A slot left idle for exactly that
reason reports `efficiency_loss`; a slot blocked by the user's own floor
(`optimizer.min_spread_eur_per_kwh`) reports `spread_below_threshold`. The two are deliberately
distinct: one is physics, the other is a preference.

**Slot classification** (`_classify`) — stable action + reason slugs:

| Condition | Action | Reason |
| --- | --- | --- |
| `p > 0`, binding grid limit at bound | charge | `grid_limit_hold` |
| `p > 0`, `p <= pv surplus` | pv_charge | `pv_surplus_charge` |
| `p > 0`, in the terminal tail and `buy >= median` | charge | `terminal_target` |
| `p > 0` otherwise | charge | `arbitrage_charge` |
| `p < 0`, binding grid limit at bound | discharge | `grid_limit_hold` |
| `p < 0`, in the terminal tail and `buy <= median` | discharge | `terminal_target` |
| `p < 0`, `buy >= p75` | export / discharge | `peak_discharge` |
| `p < 0` otherwise | export / discharge | `self_consumption` |
| `p = 0`, grid limit binding at a zero bound | hold | `grid_limit_hold` |
| `p = 0`, at `E_min` (within half a step) | hold | `reserve_hold` |
| `p = 0`, at `E_max` | hold | `battery_full` |
| `p = 0`, round trip blocks the arbitrage | hold | `efficiency_loss` |
| `p = 0` otherwise | hold | `spread_below_threshold` |

Action vocabulary: `hold`, `charge`, `discharge`, `pv_charge`, `export`, `self_consumption`,
`reserve`.

**Summary formulas** (`_summary`, all over the returned schedule):

```
cost            = Σ PlanSlot.cost_eur(dt)
baseline        = Σ dt * ( buy*max(load−pv,0) − sell*max(pv−load,0) )
saved           = baseline − cost
charged         = Σ max(0, power_w)/1000 * dt
discharged      = Σ max(0, −power_w)/1000 * dt
pv_charge       = Σ min(max(0,power_w)/1000, max(0, pv−load)) * dt
grid_charge     = max(0, charged − pv_charge)
cycles          = discharged / usable_kwh
arbitrage_kwh   = min(grid_charge, discharged)
cycle_cost_eur  = wear * discharged          # wear is per kWh delivered, charge leg pays none
objective_eur   = schedule objective (grid + min_spread + wear − terminal salvage)
wear_eur_per_kwh
eta_charge / eta_discharge / round_trip_efficiency / break_even_sell_eur_per_kwh
                = the aggregate values the plan assumed, break-even at the horizon's median buy
effective_step_kwh, solver_iterations
```

Note that `baseline` and `saved` stay **grid-only** (they do not include cycle cost or salvage), so
savings are never inflated by ignoring wear.

### 4.8 Allocation

`optimize/allocate.py` splits the aggregate slot power across enabled batteries — greedy,
deterministic, one redistribution pass.

```
floor_energy_kwh(b)   = capacity_kwh * max(min_soc_pct, reserve_soc_pct) / 100
headroom(charge)      = min(max_charge_w,    max(0, E_max − e) / (dt * eta_charge) * 1000)
headroom(discharge)   = min(max_discharge_w, max(0, e − floor) * eta_discharge / dt * 1000)
```

Algorithm: proportional split to remaining headroom, clamp per battery, then redistribute the
shortfall once, preferring the **better efficiency** (ties by battery id, stable). Returns one entry
per enabled battery in id order, including zeros; the sum equals the requested total minus whatever
is physically impossible. An unknown live energy falls back to the configured floor (safe direction:
may charge, will not be asked to discharge below its floor). The reserve is enforced **per battery**,
so a fleet-wide reserve cannot be spent entirely out of the smallest pack.

### 4.9 Control loop

**Tick** (`control.tick_seconds`, default 30 s):

1. Sample measurements (§4.5).
2. Replan if needed (§ below).
3. Take the current slot's target power (interpolating the slot boundary change).
4. Optional grid-follow correction.
5. `control.safety.check(...)`.
6. In `auto` **and not** `dry_run`: write through the drivers. Otherwise log the would-be write as a
   `dry_run` dispatch entry (visible in the UI log).
7. Per-battery allocation, per-battery ramp clamp, then driver writes with read-back verification.

Every step is recorded in `dispatch_log`, including rejections with their reason — "why didn't it
charge?" must always be answerable.

**Replan triggers** (`needs_replan` plus the engine's input fingerprint):

| Trigger | Condition |
| --- | --- |
| `no_plan` | no plan exists |
| `plan_age` | plan older than `control.replan_minutes` |
| `plan_expired` | `plan.slot_at(now)` is None |
| `soc_deviation` | live SoC differs from the planned slot by more than **3 %** of total capacity |
| `inputs_changed` | price vector (first 96 slots, `t`/`buy`/`sell` rounded), PV total or load total changed since the plan was built |
| — | a forced replan from the API (`POST /api/v1/plan/recompute`, reason `api`) |

An active plan older than `control.stale_plan_seconds` is refused for dispatch (hold + reason).
Replanning needs a live SoC; a missing reading blocks replanning without churn, and a new plan is
published/dispatched only after live readings succeed. `MIN_REPLAN_GAP_S = 60 s` throttles churn, with
backoff up to `MAX_REPLAN_BACKOFF_S = 900 s`. `off` never auto-plans (explicit `plan_now` still works).

**Grid-follow correction** (`_grid_follow`):

```
planned_grid_kw = slot.load_kw − slot.pv_kw + target_w/1000
delta           = −(measured_grid_kw − planned_grid_kw) * 1000        # W
if |delta| <= control.grid_follow_deadband_w:      no correction
budget = control.grid_follow_max_w  if non-zero
         else max(500 W, battery_capability * control.grid_follow_max_ratio)
correction = clamp(delta, −budget, +budget)
```

The budget is independent of the planned power, so a slot planning 0 W can still be corrected. The
result is then clipped by every limit inside `safety.check`.

**Safety decision** (`control.safety.check`, pure, no I/O) — clauses in order:

1. `mode != auto` → block (`mode_not_auto`).
2. Active override → replace the requested power with the override power (`override_active`), still
   subject to every limit; the decision is otherwise unchanged.
3. / 4. Batteries usable? Disabled, unavailable, missing or stale readings are excluded and named in
   `blocked_ids` (`battery_unavailable`, `stale_state`); HA disconnected blocks everything
   (`ha_disconnected`). No usable battery blocks (`battery_unavailable`).
5. **SoC clamp** — ceiling: `(E_max − e) / (dt * eta_chg)`; floor: `−(e − E_min) * eta_dis / dt`,
   where `E_min` uses `max(min_soc_pct, reserve_soc_pct)` per battery.
6. **Power clamp** — aggregated limits `Σ max_charge_w` / `Σ max_discharge_w`.
7. **Grid limits** — with the requested power, predicted import/export must stay inside
   `optimizer.max_grid_import_w` / `optimizer.max_grid_export_w`. The live meter is ground truth: the
   measured grid flow is corrected back to the "battery idle" base before clamping, and the more
   conservative of forecast/measurement basis is used. Telemetry missing entirely ⇒ fail closed by
   never planning more than the cap itself. Still violating after the clamp ⇒ block
   (`grid_import_limit` / `grid_export_limit`).
8. **Ramp** — a battery's AC power may not change by more than `RAMP_LIMIT_W = 1000 W` per tick
   (per-battery in the dispatcher; the aggregate budget is `1000 W × usable batteries`).
9. Clamp to the intersection; if it is empty, block and name every violated constraint. Otherwise
   return `allowed_w`, `clamped` and the reason slugs the result sits on.

Blocked actions are visible in three places: an alert, a `dispatch_log` row, and the UI banner.

**Stop now** (`POST /api/v1/stop`) — the atomic safety action; it is the only write path allowed to
run outside `auto`:

1. clears any active override;
2. writes `hold()` (0 W / neutral) **for real** to every battery whose last non-zero setpoint is
   still live (per-battery live-write latch), deliberately bypassing the `mode != auto` and `dry_run`
   blocks — de-escalating to zero is the safe direction. A battery the add-on never commanded is not
   touched;
3. sets `mode: advisory`;
4. records an event plus one dispatch row per battery and returns per-battery results.

It must never raise: a failing battery is reported in the response. `control.safety` stays pure and is
not involved in this path. A run that died unclean neutralises its outstanding setpoint at the next
start, before control resumes; the exit/shutdown hold is serialised behind the same locks.

**Override** (`POST /api/v1/override`) pins a battery (or all) to a fixed power for a bounded time
(max 240 min, auto-expires, visible countdown). `mode: off` is the global kill switch. A UI test
discharge is an override, so it is still bounded by the battery's own floor/ceiling/reserve.

### 4.10 Drivers

Protocol: `id()`, `read_state()`, `apply(power_w, dry_run)`, `hold(dry_run)`, `capabilities()`.
Writes go through the HA service API or state-setting where no service exists.

- **Idempotent within a slot**: no write when the device already reports the target within tolerance
  (the SolarFlow is BLE/cloud with a finite write budget). Result code `idempotent_skip`.
- **Never assume a write landed**: read back, tolerate the delay until the device reports its next
  event, retry, and treat a persistent mismatch as `driver_error`. "Persistent" means the *write*
  itself never landed: a command that is in force but whose effect the device has not (yet) shown is
  reported as `commanded` — not an error, because the fail-safe hold a driver error triggers would
  revoke a command the device is about to apply. The plan execution line compares measured vs planned
  separately, so "commanded but not following" stays visible.
- **Zendure driver**: two write paths, chosen by `control_path` (`auto` = manager first). *Manager*:
  switch the manager into manual dispatch (`operation = manual`) before commanding power, express
  charge/discharge with the device's own semantics, re-assert manual mode while in control (its smart
  modes overwrite external writes), and write a hold on shutdown/fatal error because the integration
  has no watchdog — but note it applies a setpoint only on the manager's next P1 event, so a slow
  house P1 feed bounds how fast any of this lands. *Device*: the `ac_mode` select (direction) plus
  the `input_limit` / `output_limit` numbers (magnitudes) straight on the inverter, with the manager
  put in `off` so it cannot overwrite them; this path needs no P1 event and is confirmed by the
  device's own property echo. Either way it never writes `soc_set` or `min_soc` every tick — SoC
  floors are expressed as optimised power.
- **Generic driver**: `power_setpoint_entity` (signed W, + charge / − discharge) or the pair
  `charge_power_entity` + `discharge_power_entity`, plus optional mode select/manual value, SoC and
  capacity entities. A generic battery with no usable setpoint is rejected at **save** time, not at
  dispatch time.
- **Fail-safe**: on an unrecoverable error with `control.failsafe_on_error`, a neutral write is issued
  once.

### 4.11 Savings and baseline

**Per tick** (`analytics.daily_row_delta`) into `savings_daily`:

```
import_kwh  = max(0, grid_kw) * dt_h
export_kwh  = max(0, −grid_kw) * dt_h
cost        = buy * import_kwh
revenue     = sell * export_kwh
do_nothing_import = max(0, load_kw − pv_kw)
do_nothing_export = max(0, pv_kw − load_kw)
baseline    = buy*do_nothing_import*dt_h − sell*do_nothing_export*dt_h      # net, battery idle
cycles      = max(0, −battery_kw) * dt_h / capacity_kwh
savings     = baseline − (cost − revenue)
```

`baseline` and `savings` are `None` (unknown) when the load is not knowable — never a fabricated
zero. `dt_h` is clamped to 1 h.

**Ranges** (`range_bounds`): `day` = today from midnight; `week` = 7 days; `month` = 30 days;
`year` = 365 days; `all` = since epoch. Buckets are days, except `year`/`all`, which bucket per month
(`_BUCKET_LENGTHS` = 1 day, 30 days for year/all). Empty buckets are kept so the chart stays steady.

**Totals and split** (`savings_from_daily`):

```
arbitrage_kwh         = Σ min(cycles * capacity_kwh, export_kwh)      # discharged energy that was exported
self_consumption_kwh  = Σ max(0, cycles*capacity_kwh − export_kwh)    # the rest displaced import
battery_throughput_kwh = arbitrage + self_consumption                  # documented estimate
totals: import_kwh, export_kwh, pv_kwh, cost_eur, revenue_eur, baseline_cost_eur, savings_eur,
        cycles, arbitrage_kwh, self_consumption_kwh, battery_throughput_kwh, buckets
```

Baselines are computed **before** the daily maintenance trim and stored in `savings_daily`, so the
numbers stay comparable after raw rows are gone.

`BASELINE_NOTE` (shown verbatim in the UI): *"Baseline: the same measured consumption at the same
prices with the battery doing nothing. Savings = baseline cost - actual cost."*

### 4.12 Forecast accuracy

`accuracy_from_samples` over the last N days (default 14):

```
MAPE = mean( |forecast − actual| / |actual| ) * 100      (only samples with a usable actual)
bias = mean( (forecast − actual) / |actual| ) * 100
MAE  = mean( |forecast − actual| )
samples = number of usable samples
```

`null` MAPE means "no data" and renders `--` (never as 0). Per-day series
`{day, mape_pct, bias_pct}` feeds the 14-day trend chart.

### 4.13 Price windows and rank

```
percentile_rank(values, value) = clamp(count(v <= value) / len(values) * 100, 0, 100)
```

Used for `price_rank_pct` in `/status`, rendered as the cheapest / middle / most expensive third.

`price_windows(slots, now)` (future slots include the one running now):

```
cheap_threshold     = 25th percentile of future buy prices
expensive_threshold = 75th percentile of future buy prices
next cheap window     = first run with buy <= cheap_threshold
next expensive window = run around the max-price slot, extended while buy >= expensive_threshold,
                        padded to at least one hour
```

Used for the two dashboard window cards and the `sensor.sem_price_now` attributes.

### 4.14 Efficiency, wear and break-even (worked examples)

```
eta_charge = eta_discharge = sqrt(round_trip_pct / 100)      # unless overridden
round_trip = eta_charge * eta_discharge
wear = price_eur / (cycle_life * usable_kwh * eta_discharge)  # EUR per kWh delivered; 0 if unset
break_even_sell(buy, wear=None) = buy / round_trip + (wear or derived wear)
```

Wear is stated per kWh **delivered** to the house, so the break-even needs no per-direction
efficiency. The advanced `optimizer.cycle_cost_eur_per_kwh` override, when non-zero, replaces the
derived value; the fleet plans with the delivered-energy-weighted mean.

| Buy price | Round trip | Wear (EUR/kWh delivered) | Break-even sell |
| --- | --- | --- | --- |
| 0.20 | 0.90 | 0 | 0.2222 |
| 0.20 | 0.75 | 0 | 0.2667 |
| 0.20 | 0.80 | 0 | 0.2500 |
| 0.20 | 0.75 | 0.02 | 0.2867 |

Without wear the formula collapses to `buy / round_trip`. The effective cost of a stored kWh shown on
the battery card is `buy / eta_charge + wear × eta_discharge` — the buy price plus the charging loss
plus the wear of the energy that will be delivered. A degenerate
efficiency returns `None` rather than raising. Every per-battery break-even is computed from that
battery's own efficiency and wear, so a cheap old pack and an efficient new one are never averaged
into one misleading figure, while the *plan* reports the aggregate round trip and wear it actually
assumed.

### 4.15 Measured actuals onto the plan grid (`/plan/execution`, 0.10.0)

The plan says what was intended; `GET /api/v1/plan/execution` joins the *same* slot grid with what
was measured and what was applied, so a window can state both without the UI inventing anything.

```
bucket         = 5 min (engine.ACTUALS_MINUTES, the actuals row's own resolution)
rows           = actuals rows in [plan.horizon_start, now + 5 min), deduplicated by ts, last row wins
                 (the store upserts one row per bucket; the in-memory ring keeps every tick)
slot aggregate = every surviving row in [slot.t, slot.t + slot_minutes) counts as 5 measured minutes
coverage_pct   = min(100, 100 x samples x 5 / slot_minutes)
energy_kwh     = sum(kW) x 5/60, split into magnitudes:
                 charged/discharged from battery_kw (positive = charging),
                 import/export from grid_kw (positive = importing)
progress       = a fully elapsed slot counts whole; the slot in progress counts by coverage_pct,
                 so the intended and measured sides cover the same span
                 planned_cost  = sum of slot.cost_eur over that span
                 measured_cost = sum(import_kwh x buy − export_kwh x sell) at the slot's own tariff
                 measured_* stays null until something was measured
clamped        = any battery with |applied_w| < |requested_w| − 1 W (engine.CLAMP_TOLERANCE_W)
floor_soc_pct  = max(optimizer.reserve_soc_pct, every enabled battery's min_soc_pct)
```

A slot with no measurement reports `actual: null` and a window with no dispatch row reports
`dispatch: null`; the UI renders "not reached yet", "no dispatch recorded" or the stale-price refusal
from that absence rather than reading it as 0 W applied. The dispatch log is read from the store
(everything since the plan was generated) and falls back to the in-memory journal ring, which only
holds a few hours of ticks.

---

## 5. Published Home Assistant entities

Published with `POST /api/states/<entity_id>` (no MQTT, no YAML). Publishing is idempotent, throttled
to **at least 20 s** between writes per entity, skipped entirely when HA is unreachable, and never
blocks the control loop. Unchanged state and attributes are not rewritten.

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.sem_autopilot_state` | `off` \| `advisory` \| `auto` \| `dry_run` | — |
| `sensor.sem_plan_action` | action slug | `reason`, `target_power_w`, `soc_target_pct` |
| `sensor.sem_price_now` | EUR/kWh | `rank_pct`, `cheap_window_start`, `expensive_hour_start` |
| `sensor.sem_pv_forecast_today` | kWh | `tomorrow_kwh`, `peak_w`, `peak_at` |
| `sensor.sem_load_forecast_now` | kW | `today_kwh`, `confidence` |
| `sensor.sem_savings_today` | EUR | `month_eur`, `year_eur`, `baseline_kwh` |
| `sensor.sem_battery_soc_avg` | % | — |
| `sensor.sem_forecast_accuracy` | % | `pv_mape`, `load_mape`, `price_mae` |
| `binary_sensor.sem_autopilot_active` | on/off | — |
| `binary_sensor.sem_battery_at_floor` | on/off | — |

No `unique_id` is created, so the mirrors are not in the entity registry and disappear on an HA
restart until the add-on republishes them on its next tick — documented behaviour, not a bug.

---

## 6. HTTP API surface

Plain JSON over the ingress port, `Content-Type: application/json`, all timestamps ISO-8601 with
offset. Errors are `{"error": {"code": "...", "message": "..."}}` with 4xx/5xx. Every GET must work
in `mode: off` and with no batteries configured (degraded, not broken). No auth of its own — ingress
supplies it (which is why publishing a port would hand battery control to anyone who can reach it).

| Method & route | Body / query | Returns |
| --- | --- | --- |
| `GET /api/v1/health` | — | `{status, version, uptime_s, commit}` |
| `GET /api/v1/status` | — | serialised `SystemState`: `version`, `commit`, `uptime_s`, `mode`, `dry_run`, `pending_holds[]`, `mode_persisted`, `ha_connected`, `ha_last_error`, `location`, `now`, `price_now`, `price_rank_pct`, `pv_now_w`, `load_now_w`, `grid_now_w`, `batteries[]`, `active_plan`, `next_cheap_window`, `next_expensive_window`, `override` (+ `seconds_left`), `alerts[]`, `provider_status[]` |
| `GET /api/v1/plan` | `hours` | `{generated_at, horizon_start, horizon_end, slot_minutes, feasible, infeasible_reason, mode, dry_run, sources{}, summary{…+eta_charge, eta_discharge, round_trip_efficiency, wear_eur_per_kwh, break_even_sell_eur_per_kwh}, slots[{t, buy, sell, pv_kw, load_kw, load_p10, load_p90, soc_kwh, soc_pct, power_w, action, reason, price_stale, pv_source, price_source}], batteries[{id, name, share_w, round_trip_efficiency, wear_eur_per_kwh, break_even_sell_eur_per_kwh}]}`; `slot_minutes` is the plan's own resolution (the UI never assumes one); with no plan it returns an empty payload with `feasible: false`, `infeasible_reason: "no_plan"` and `slot_minutes: null` |
| `GET /api/v1/plan/execution` | `hours` | additive (0.10.0): `{generated_at, slot_minutes, now, floors{reserve_soc_pct, min_soc_pct, floor_soc_pct}, batteries[{id, name, min_soc_pct, floor_soc_pct, capacity_kwh}], progress{…}, slots[{t, dispatch, actual}]}` — the same slot grid as `/plan`, joined with the persisted 5-minute actuals and the dispatch log. `dispatch` is the last attempt per battery in that slot (`requested_w`, `applied_w`, `ok`, `dry_run`, `clamped`, `code`, `reason`, `message`, `records`, `batteries[]`) and `actual` the measured aggregate (`samples`, `coverage_pct`, `pv_kw`, `load_kw`, `grid_kw`, `battery_kw`, `planned_kw`, `measured_kw`, `import_kwh`, `export_kwh`, `charged_kwh`, `discharged_kwh`, `soc_kwh_start`, `soc_kwh_end`). Both are `null` for a slot nothing was measured or dispatched in — never a fabricated zero |
| `POST /api/v1/plan/recompute` | — | `{queued: true}` (async; poll status) |
| `GET /api/v1/forecast` | `kind=price\|solar\|load`, `hours` | `{kind, generated_at, source, stale, slots, provider, contract, notes[], reason, hint}` |
| `GET /api/v1/batteries` | — | `{batteries: [{config, state, capabilities, mapping_resolved, discovery[]}]}` |
| `POST /api/v1/batteries` | `{config}` | 201 `{config}` |
| `PATCH /api/v1/batteries/{id}` | `{patch}` | `{config}` |
| `DELETE /api/v1/batteries/{id}` | — | result |
| `POST /api/v1/batteries/{id}/test` | `{power_w, seconds, intent}` | `{ok, applied_w, seconds, code, message}`; 409 when not `auto`/dry-run |
| `GET /api/v1/discover` | `kind`, `search`, `limit` (≤ 50) | `{candidates[]}` |
| `GET /api/v1/config` | — | `{options, settings[{key, value, type, default, label, help, group, choices, advanced}], price_warnings[]}` |
| `PATCH /api/v1/config` | `{settings: {key: value}}` | `{applied[], errors[]}`; per-key errors, 400 only for a malformed/empty body |
| `POST /api/v1/mode` | `{mode?, dry_run?}` (partial) | updated state |
| `POST /api/v1/stop` | — | `{mode, override_cleared, stopped[{battery_id, ok, applied_w, code, message, dry_run}]}` |
| `POST /api/v1/override` | `{power_w, minutes, battery_id?}` | `{override}` |
| `DELETE /api/v1/override` | — | `{}` |
| `GET /api/v1/savings` | `range=day\|week\|month\|year\|all` | `{range, buckets[], totals{}, baseline_note}` |
| `GET /api/v1/accuracy` | `days=14` | `{pv, load, price: AccuracyReport}` |
| `GET /api/v1/log` | `limit`, `level`, `kind` | `{entries[{ts, kind, level, code, message, detail}]}` |
| `GET /api/v1/providers` | — | `{solar[], price[], load[]}` |
| `GET /api/v1/export` | `what=plan\|actuals\|savings`, `format=json\|csv` | file download (`Content-Disposition`) |
| `POST /api/v1/notify/test` | — | `{}` |
| `GET /` and static assets | — | SPA (`Cache-Control: no-cache`; path traversal rejected) |

**Frozen reason slugs** (used by the UI, log and notifications): plan reasons `arbitrage_charge`,
`pv_surplus_charge`, `peak_discharge`, `self_consumption`, `reserve_hold`, `grid_limit_hold`,
`spread_below_threshold`, `efficiency_loss`, `terminal_target`, `battery_full`, `battery_empty`,
`no_forecast_slots`, `stale_input`; dispatch blocks `mode_not_auto`, `dry_run`, `override_active`,
`stale_state`, `ha_disconnected`, `battery_unavailable`, `soc_floor`, `soc_ceiling`, `power_clamp`,
`grid_import_limit`, `grid_export_limit`, `ramp_limit`, `driver_error`, `idempotent_skip`,
`commanded` (the command is in force, the device has not confirmed it yet — `applied_w` is the
setpoint that is live, `readback_w` what the device reports), `ok`;
empty-forecast slugs `not_configured`, `provider_failed`, `fixed_price_missing`, `no_pv_source`,
`no_history`, `empty_horizon`, `unknown`.

---

## 7. Persistence and retention

One SQLite file in the add-on's `/data` volume: `journal_mode=WAL`, `synchronous=NORMAL`,
`busy_timeout=5000`, a forward-only migration list and a schema version in `meta`. Thread-safe via one
connection per thread.

| Table | Contents |
| --- | --- |
| `plans`, `plan_slots` | what was planned (per-slot timestamp, prices, PV, load, SoC, power, action, reason) |
| `measurements` | 5-minute bucketed raw samples per role |
| `actuals` | per-tick ground truth (pv, load, grid, battery, soc, price, plan/dispatch power) |
| `dispatch_log` | requested vs applied power, mode, dry run, result, code, message |
| `savings_daily` | the accounting: import/export kWh, cost, revenue, baseline, savings, PV, cycles |
| `learning_profiles` | PV and load corrections, derived solar capacity, and the live-write latch |
| `forecast_cache` | cached provider payloads with fetch timestamps |
| `events` | events and alerts |
| `settings` | runtime settings (dotted keys) |
| `meta` | schema version and small facts |

Daily maintenance trims `measurements`/`actuals` to **400 days**, `dispatch_log` to **20 000** rows,
`events` to **5 000**, and `plans` to `plans_rows` (**200**) most recent, with their `plan_slots`.
Sampling reads only mapped entities and upserts into 5-minute buckets, so volume does not scale with
the tick rate. Savings baselines are computed before trimming.

---

## 8. Notifications

`notify.py` sends to `persistent_notification.create` (when `notify.persistent`) with id
`sem_<event>` and/or to `notify.mobile_service`. It never blocks the control loop and never raises
into the caller. Messages contain generic text plus numbers — never entity ids or secrets.

- Gated by `notify.enabled` and membership in `notify.events`.
- Deduplicated per event: `DEFAULT_DEDUPE_S = 300 s`.
- Events: `plan_changed`, `plan_applied`, `battery_floor_reached`, `negative_price`,
  `forecast_unavailable`, `driver_error`.
- `POST /api/v1/notify/test` sends one self-describing test notification.
- Alerts additionally dedupe at `ALERT_DEDUPE_S = 900 s`.

---

## 9. Constant reference

| Constant | Value | Where |
| --- | --- | --- |
| Slot length | 60 min (`optimizer.slot_minutes`; providers still feed a 15 min grid) | price/solar/load/optimizer |
| Measurement bucket | 5 min | `sources/measure.py` |
| Actuals bucket | 5 min | `engine.py` (`ACTUALS_MINUTES`) |
| DP states / iterations | 400 / 2 000 000 | `optimize/dp.py` |
| DP terminal penalties | `1e6` hard, `2.0` soft | `optimize/dp.py` |
| Ramp limit | 1000 W per battery per tick | `control/safety.py` |
| Grid-follow budget | `max(500 W, capability × 0.5)` | `control/dispatcher.py` |
| Publish throttle | 20 s per entity | `engine.py` |
| Min replan gap / backoff cap | 60 s / 900 s | `engine.py` |
| Alert dedupe | 900 s | `engine.py` |
| Savings flush | 300 s | `engine.py` |
| Discovery cache / rows | 60 s / 50 | `engine.py` |
| Load window / samples / EWMA | 56 days / 24 / 0.2 | `sources/load.py` |
| Load scale and bias clamps | 0.5…2.0 both | `sources/load.py` |
| Temperature term threshold | r² ≥ 0.05 | `sources/load.py` |
| Solar bias | α 0.2, clamp 0.3…1.8, decay 14 days | `sources/solar.py` |
| Solar cache / rate limit | 30 min / 12 per hour, backoff 60→1800 s | `sources/solar.py` |
| Solar planes cap | 4 | `sources/solar.py` |
| Solar uncertainty | 35 % clear-sky, 25 % with cloud | `sources/solar.py` |
| Haurwitz | A 1098, B 0.059 | `sources/solar.py` |
| Diffuse fraction / albedo | 0.25 / 0.2 | `sources/solar.py` |
| Cloud attenuation | `1 − 0.75·cc^3.4` | `sources/solar.py` |
| Assumed geometry | tilt 35°, azimuth south (180°) | `sources/solar.py` |
| Derived capacity | 24 samples, 90th percentile, 24 h refit | `sources/solar.py` |
| Zonneplan value factor | `1e-7` (EUR/kWh) | `sources/price.py` |
| Energy-charts unit | EUR/MWh | `sources/price.py` |
| Price horizon | `optimizer.horizon_hours` (30 default, 12…48) | `sources/price.py` |
| Notification dedupe | 300 s | `notify.py` |
| Savings retention | 400 days raw, 20 000 log rows, 5 000 events, 200 plans | `store.py` |

---

*Generated from the 0.8.0 source tree. Contains no instance-specific values: entity ids, zones and
prices in this document are settings keys, placeholders or worked examples.*
