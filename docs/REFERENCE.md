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
| Top bar — connection | `#conn-status`, live region: `Connecting…` / `HA connected` / `HA unreachable` (mode and dry run are answered on the Dashboard, not repeated here) |
| Top bar — theme button | `Theme: dark` / `Theme: light`; sets `data-bs-theme` on `<html>`, persisted in localStorage, and calls `SEMCharts.refresh()` |
| Nav — primary | Four links: Dashboard (`#/dashboard`), Plan, Savings, Manage (`#/batteries`); ≥44 px tall |
| Nav — contextual sub-row | Shown only for the current area: Plan → Plan, Forecasts; Manage → Batteries, Cards, Settings, Log; all eight routes stay real links (`aria-current` marks the exact route, the area entry stays marked) |
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
| `#/cards` | `viewCards` | — |
| `#/settings` | `viewSettings` | — |
| `#/log` | `viewLog` | — |

Unknown routes fall back to `#/dashboard`. Unknown paths are normalised by
`SEM_APP.normalizeRoute()`.

### 2.3 Dashboard

Panels: **Now** (the answer strip), **Warnings and activity**, **Manual override**, **Right now**,
**Supporting measurements** (quiet), **Next 24 hours**, **Batteries**.

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

**Manual override panel** (`renderManualOverride`, additive 0.20.0) — **Force**: a battery select
(`All batteries` plus each battery), a direction select (`Charge` / `Discharge` / `Hold`), a watt
field shown for charge/discharge, a duration select (5/15/30/60/120 min, default 30) and
`Apply override` (confirmation dialog; posts `POST /api/v1/override` with a signed power). The watt
range is the resolved cap, so the form cannot promise a force the dispatcher will refuse. An active
override is shown as `Active: charge 600 W until 21:45 on <battery>` with a confirming
`Clear override` (`DELETE /api/v1/override`). **Limits**: `Max charge` / `Max discharge` inputs per
battery plus an `All batteries` row, with `Set` (`POST /api/v1/limits`; both fields are sent and an
empty field clears that direction) and `Clear` (confirmation dialog; explicit `null`s for that row).
Each row states the resolved value and its source (`this battery` vs `all batteries`) or `unlimited`,
and the configured maximum bounds the input. The panel always states whether a force writes: `mode`
not `auto` → nothing is dispatched; `dry_run` on → logged, not written (only Stop now writes in dry
run). A background poll never repaints the panel while a number field or select inside it has focus,
so a typed value is not discarded.

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
When `plan.maintenance.active` is true, a `maintenance plan` badge precedes the summary numbers with
the sentence `Maintenance plan: a due/overdue full charge for <ids> — the optimiser targets <x> %
before the deadline, in the cheapest slots, so the BMS can balance the cells.`

**Next actions** — a `.window-list` of the contiguous action windows, one card each, so a 360 px screen
never splits the time/action/reason relationship. The panel reads top to bottom as: the horizon's own
totals, the ribbon that places the windows across the horizon, then the cards.

The totals sit in `.window-summary-block` behind a `list` glyph and keep stating the sums in words
(`6 windows · 1.60 kWh out · 0.40 kWh in · costs 0.31 EUR on the grid · measured so far …`) — never a
plan figure dressed up as a measurement.

`.window-ribbon` is the panel's graphic: one `.window-ribbon-seg` per window placed by time (inline
`left`/`width` percentages off `planHorizonBounds(slots, minutes)`), a hatched segment for a stale
window, `.window-ribbon-day` hairlines at local midnight, an accented `.window-ribbon-now` marker, and
a `.window-ribbon-legend` naming every action that occurs. The track is a `role="img"` whose
`aria-label` reads the whole horizon out loud, and each segment repeats the window's own words in its
`title` — the graphic is never the only place that information exists.

Each `.window` card is tinted by its action (`window--charge`, `window--discharge`, `window--pv`,
`window--export`, `window--self`, `window--reserve`, `window--idle`) through a single `--tint`, and it
states, in this order:

| Line | Content |
| --- | --- |
| Head (`.window-head`) | `.window-icon` — the action's inline glyph on its tinted tile; `.window-action` — `Discharge 700 W`, the action word names the direction so the power is unsigned; a `now` badge with a pulsing dot marks the window containing the current time; `.window-time` — `06:45 PM – 07:15 PM` and the slot count |
| Charge gauge (`.window-soc`) | The whole battery as a track: the fill is the level the window ends on, the lighter tick is where it started, the bright tick is the floor the plan defends. `.window-soc-label` repeats it in words (`SoC 55 % → 20 % · floor 10 %`), so the bar is never the only source |
| Reason | `because: Peak discharge` (the frozen slug, rendered in words) |
| Facts (`.window-facts`, `label`/`value` pairs, one `.fact-icon` each) | `Buy → sell` (`20.0 ct/kWh → 30.0 ct/kWh (+10.0 ct/kWh)`), `Break-even` (`buy / round_trip + wear`, with the margin above it or `efficiency not reported`), `Battery` (planned kWh in/out for the window), `SoC` (first slot's SoC → last slot's SoC, plus the SoC floor a reserve hold defends), `Grid cost` (planned import/export kWh from `load − pv + power` and what that costs or earns at the slot's own tariff) |
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
| Disclosures | `Capacity learning`, `Device actions (test and delete)` (with the prerequisite as real text), `Efficiency and wear`, `Entity mapping`, `Driver details` |
| Capacity | effective kWh (marked `learned` when the estimate is applied), configured seed, the estimate with its accepted-window count and last-learning time, and the reason slug when a learned value is rejected |
| Round trip | `Round trip` in percent — the single input, split evenly unless overridden |
| Wear | `price / (cycle_life × usable kWh × η_dis)` per kWh delivered, derived from the optional price/cycle-life inputs so a full cycle costs exactly `price / cycle_life`; the advanced `optimizer.cycle_cost_eur_per_kwh` override replaces it when non-zero |
| Effective stored-kWh cost | `buy / eta_charge + wear × eta_discharge` recomputed at the current buy price |
| Break-even sale price | `buy / round_trip + wear` |
| Cycle count | from `state.extra.cycles`, else `not reported` |
| Limits & SoC window | charge/discharge limits, min/max SoC |
| Mapping | list of mapped entities plus `mapping resolved` / `needs confirmation` badge |
| Live values | power, SoC, energy, temperature, read age, **cell voltage** (lowest per-cell V, or `not reported`), a **Protection** state (`off`, `normal`, `discharge blocked` or `emergency charge active`) with a `low cell voltage` badge on a latched battery, and a **Maintenance** state (`off`, `due in N days (target x %)`, `due now`, `overdue` plus `last completed …`/`at target`), with a `full charge overdue` badge while the deadline has passed |
| Actions | enable/disable (confirm when enabling), `Test discharge` (disabled unless `mode=auto` and dry run off; confirm + checkbox), edit, delete (confirm + checkbox) |

**Add/edit wizard**

| Field | Detail |
| --- | --- |
| `Find a battery` block | Reads `/discover?kind=battery` once on open, groups rows per device, lists only devices that could be a battery (known battery platform, a Zendure device on the `mqtt` platform, or a control role plus a state role where the control entity is writable — `number`/`select`/`input_number`/`input_select`/`switch`); ranked by control-readiness, then mappable roles, then confidence. Each row shows device label, model/manufacturer, platform, mappable-role count and a `control-ready` / `partial — missing …` / `control entities are read-only` badge. `Select` fills the form below. A Zendure MQTT bridge device is identified by its platform plus manufacturer/model and its roles come from the zenSDK property names, so its pick never depends on a confidence score. |
| Search | Filters locally over device name, manufacturer, model, platform and entity id; falls back to the server's entity-text search when the local filter finds nothing. Discovery caps at 50 rows per read, so a device beyond the cap (a bridge device on a busy instance) is found by searching its name or serial — the copy says so. |
| `id` | slug, auto-slugified and made unique on selection |
| `Name` | free text |
| `Driver` | `zendure`, `zendure_mqtt`, `zendure_native` or `generic` (auto-suggested: `zendure` on `platform: zendure_ha` or a Zendure-only role, `zendure_mqtt` for a Zendure device on `platform: mqtt`; `zendure_native` is chosen by hand because no registry row identifies a device that has no integration). The `Control path` select is shown for `zendure` only; choosing `zendure_mqtt` states that the device's own entities are written and that no other controller (the Zendure-HA integration) may manage the same inverter; choosing `zendure_native` states that only the device's local API is used and that it must be enabled in the Zendure app (HEMS), and shows the connection fields instead of entity roles |
| `zendure_native` connection fields | `host` (IP address or mDNS name), `serial` (`sn` the device reports), `port` (empty = 80/443), `scheme` (`http`/`https`), `api_path` (default `/properties`) and an optional `token` (password field: stored, sent as `Authorization: Bearer`, shown masked and never echoed). Malformed values are rejected inline (bare host, 1…65535 port, http/https, `/`-prefixed path, 0…60 s timeout); the server additionally reads the device once on save and refuses an unreachable host or a mismatched serial unless `allow_unreachable` was set in the stored mapping. One battery per inverter: a second battery with the same `host`+`serial` is journaled as `driver_conflict` and not driven |
| `Capacity (kWh, usable AC side)` | step 0.1; in `learned` mode it is the seed/fallback |
| `Capacity source` | `manual` (use the typed capacity) or `learned` (measure the pack's real usable capacity); learned states the 10-point / 20 %-of-limit / 3-window / 30-day rules inline |
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
| `cell_voltage_entity` | Lowest cell voltage sensor (per cell, V) |

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
| `cell_voltage_entity` | Lowest cell voltage sensor (per cell, V) |
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
| Answer first | the saving/loss line, then `Savings cover N% of the sampled time` (omitted at 100%); the comparable baseline and actual net cost cover **the same priced time** |
| Unknown handling | `--`, a distinct `Unknown` card flag and an explicit banner when `savings_eur` / `baseline_cost_eur` are `null` (no sampled interval could be priced); `Loss` and `Stale` are separate flags; unknown is never rendered as zero |
| Split row | `Battery energy for arbitrage` and `Battery energy for self-consumption`, or a note that the backend does not report the split |
| Measured battery flows | a `Measured battery flows` banner, then `Charged (measured)`, `Discharged (measured)`, `Charge cost (measured, at buy)`, `Discharge value (measured, at sell)`; `--` only when no sampled interval had a confirmed measurement, plus `Measured over N% of the sampled time`. A dry-run, blocked or unconfirmed setpoint earns nothing |
| Coverage | `Sampled-time coverage: grid X h, battery Y h, baseline Z h of W h sampled.` — the share of sampled time each figure really covers (0.30.0); legacy rows without hours are reported as legacy periods, not as uncovered time |
| Baseline banner | `How the baseline is defined` — server `baseline_note`, plus the counterfactual re-pricing caveat |
| Chart | savings bars + solid actual net cost / long-dashed baseline cost lines; null series are skipped, not plotted as 0 |
| Per-bucket table | `Bucket`, `Period`, `Import`, `Export`, `PV`, `Cost`, `Revenue`, `Baseline`, `Savings`, `Cycles` (or `-- (unmeasured)`), `Charged (measured)`, `Discharged (measured)`, `Charge cost`, `Discharge value` |

### 2.8 Settings

Panels: **Settings** (always-visible toolbar + Safety summary, five setup cards, collapsed
**All settings** drawer) and **Add-on options** (read-only, `Option`/`Value` table).

Always visible above the cards: the `.settings-toolbar` (`settings-search` + `settings-show-advanced`)
and the **Safety summary** (`Control authority: <mode> · dry run on/off — <authorityNote>`, a
`Review safety limits` control that opens the Safety group, and `Stop now is on the Dashboard`).

**Setup cards** — the default surface; `SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})`
is the pure table of keys a card owns for the current selection (every input the selection needs is
on the card):

| Card | Keys it renders |
| --- | --- |
| Price | `price.provider`, `price.export_fee_eur_per_kwh`, `price.feed_in_energy_tax_refund`; plus `price.entity` (`provider=entity`), `price.bzn` + `price.vat_pct` + `price.energy_tax_eur_per_kwh` + `price.import_fee_eur_per_kwh` (`day_ahead`), `price.contract` + `price.fixed_price_eur_per_kwh` (`fixed`, single) or the two day/night rates **and** the two window hours (`fixed_day_night`) |
| Meter | `grid.power_entity` (searchable picker); left empty, it resolves at runtime from the Energy dashboard. The card shows the resolved roles, labels a saved value as an override, and offers `Use the Energy dashboard` to clear every explicit `grid.*` key |
| Solar | `solar.enabled`, `solar.source`, `solar.measured_entity` unless the source is `derived`, `solar.capacity_wp` (`capacity`), `solar.planes` (`forecast_solar`), plus the merge editor `solar.sources` and its `solar.merge_strategy` |
| Battery | the fleet from `GET /api/v1/batteries` (the canonical inventory) plus a link into the wizard |

Entity pickers ask `GET /api/v1/discover` and write the candidate row's `setting_key`
(`grid.export_entity` for an export-only sensor, and so on), falling back to the card's own key; a
typed entity id is always accepted.

**All settings drawer** — collapsed by default (`sem.settings.allOpen`), holds:

- the group rail (`.settings-rail`, side by side with the form above 720 px via
  `.settings-columns`), one group at a time, ordered
  `General, Price, Solar, Load, Grid, Optimizer, Control, Safety, Maintenance, Notify, Batteries`, with each
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
`reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export|control.failsafe_on_error`, and
every key the server puts in the `Safety` group (the `protection.cell_voltage_*` keys), are
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
(`price.feed_in_eur_per_kwh`, `price.feed_in_entity`, `price.feed_in_tax_inclusive`,
`price.feed_in_mode`, `price.energy_tax_confirmed`, `price.feed_in_tax_deduction_eur_per_kwh`,
`price.saldering_assume_nettable`).

Always-visible solar keys: `solar.source`, `solar.enabled`, `solar.bias_correction`,
`solar.measured_entity`, `solar.sources`, `solar.merge_strategy`. The `solar.sources` editor is a
row per entry (source select, weight input, Move up / Move down / Remove) with an `Add source`
button; it writes the plain list through the ordinary dirty/Save flow, and each row shows the
provider's reported `state` and `note` when the Settings route loaded `/api/v1/providers`.

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

### 2.10 Cards

Panels: **Add a chart to a dashboard**, **Feed entities**.

| Item | Detail |
| --- | --- |
| `Download card` | `GET /api/v1/web/sem-charts-card.js` as a download, saved as `sem-charts-card.js` |
| Steps | save to `/config/www/sem-charts-card.js`; add `/local/sem-charts-card.js?v=<add-on version>` under Settings → Dashboards → Resources as a JavaScript module (Home Assistant caches `/local` for 31 days, so the query is what makes a replaced file take effect); add a Manual card |
| Chart picker | `plan`, `mini`, `price`, `solar`, `load`, `savings` (range `day`), `accuracy` |
| `Copy snippet` | `navigator.clipboard` where available (HTTPS/localhost); otherwise selects the snippet and says `Press Ctrl+C` |
| Snippet | `type: custom:sem-charts-card`, `chart:`, `title:` and — for savings — `range:`; `entity:`/`title:` are optional overrides |
| Preview | drawn by the SPA's own `charts.js` from the same `/plan`, `/forecast`, `/savings`, `/accuracy` payloads the card's feed rows come from |
| Feed table | the six `sensor.sem_chart_*` entities and what each carries (§5.1) |

### 2.11 Cross-cutting UI rules

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
| `price.feed_in_mode` | choice `derived\|explicit\|buy_minus_fee\|saldering\|tiered` | `derived` | — | **Feed-in contract** — how export is credited. `derived` = the pre-stage-1b rule (the live forecast path). The others are priced by `sources/feed_in.py`, which is exact-or-refuse (`unpriced_export`) and **not wired into the live forecast yet**: until the optimizer consumes the complete result, the forecast keeps using the derived price |
| `price.energy_tax_confirmed` | bool | `false` | — | **Energy tax confirmed** — on = the energy-tax component is a stated value, including a confirmed `0`; with the refund off, an unconfirmed component refuses the export leg instead of pricing `sell = buy` |
| `price.feed_in_tax_deduction_eur_per_kwh` | float, nullable | `null` | 0…1 | **Feed-in tax deduction** — per-kWh tax/levy deducted from the feed-in price once saldering ends. Unset = use the slot's own energy-tax component |
| `price.saldering_assume_nettable` | bool | `false` | — | **Assume nettable** — `saldering` only: when the period import/export state is unavailable, assume this period still has nettable import (an assumption shown in the plan, never a fact) |

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
| `solar.sources` | json | `[]` | — | **Forecast sources** *(advanced)* — ordered `[{source, weight, ...}]` (SPEC §7.1.3); empty = `solar.source` alone |
| `solar.merge_strategy` | choice `weighted_mean\|median\|max` | `weighted_mean` | — | **Merge strategy** *(advanced)* — how several sources become one value per slot |

#### Load

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `load.mode` | choice `auto\|entity\|derived` | `auto` | — | **Load source** *(advanced)* — auto uses the load entity if mapped, else derived |
| `load.entity` | str | `""` | — | **Load entity** *(advanced)* |
| `load.temperature_entity` | str | `""` | — | **Outdoor temperature entity** *(advanced)* — optional regression term |
| `load.base_kwh_per_day` | float | `0.0` | 0…1000 | **Fallback daily energy** *(advanced)* |
| `load.devices` | json | `[]` | — | **Named load devices** *(advanced)* — ordered `[{entity, kind, name}]` (SPEC §7.2); `kind` is `heat_pump`, `ev_charger`, `water_heater` or `pool_pump`, `name` optional. Each meter is subtracted from the house total before the base profile is learned and added back above it with its own profile; only the heat pump, water heater and pool pump kinds get a temperature term. A device that cannot be measured stays inside the base and is named in the forecast notes |

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
| `optimizer.reserve_soc_pct` | float | `0.0` | 0…100 | **Reserve SoC** — extra floor above battery `min_soc`; the cost-basis ledger and the sell floor price only the energy above it (0.21.0) |
| `optimizer.max_grid_import_w` | float | `0` | ≥ 0 | **Max grid import W** — 0 = unlimited |
| `optimizer.max_grid_export_w` | float | `0` | ≥ 0 | **Max grid export W** — 0 = unlimited |
| `optimizer.min_spread_eur_per_kwh` | float | `0.0` | 0…10 | **Min spread** — ignore arbitrage thinner than this |
| `optimizer.pv_charge_preference` | choice `auto\|prefer_pv\|avoid_pv` | `auto` | — | **PV charge preference** — price PV energy charged into the pack at the slot's `sell` (default: the export forgone), `0` (`prefer_pv`: absorb the surplus first) or the slot's `buy` (`avoid_pv`: export the PV and charge from the grid in the cheapest hours). A planning shadow price: the reported money stays the true grid cost |
| `optimizer.target_soc_pct_end` | float, nullable | `null` | 0…100 | **End SoC target %** *(advanced)* — unset = no hard terminal target |
| `optimizer.sell_floor_enabled` | bool | `true` | — | **Sell floor** — refuse a discharge or export below the break-even of the energy actually in the pack (the cost-basis ledger, learned from measured charge intervals and the prices they were charged at). Off, or before a basis exists, behaves exactly as before |
| `optimizer.sell_floor_margin_eur_per_kwh` | float | `0.0` | 0…1 | **Sell floor margin** *(advanced)* — require this much above the break-even before a sale is allowed |
| `optimizer.feed_in_wiring` | choice `off\|shadow\|on` | `off` | — | **Feed-in wiring** — how the optimizer consumes the contract-correct settlement (`price.feed_in_mode`). `off` = today's behaviour and the settlement is never called; `shadow` = settle and record a plan diff (learning profile + journal) while v1 stays dispatched; `on` = dispatch the settlement plan. An unpriceable contract never re-prices with the derived formula: planning holds instead. Requires a supported `price.feed_in_mode` before turning it on |

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

The Safety group is never folded and is always shown. It holds the authority keys the engine will
not let a plan override — the SoC/grid limits (`optimizer.reserve_soc_pct`,
`optimizer.max_grid_import_w`, `optimizer.max_grid_export_w`, `optimizer.allow_export`),
`control.failsafe_on_error`, and the cell-voltage protection keys:

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `protection.cell_voltage_enabled` | bool | `false` | — | **Cell-voltage protection** — watch the lowest cell voltage and charge the pack out of danger; needs a mapped min-cell-voltage entity or the `zendure_native` driver's own read |
| `protection.cell_voltage_warning_v` | float | `3.10` | 0…10 | **Warning cell voltage** — volts per cell; must be above the cutoff. Informational only |
| `protection.cell_voltage_cutoff_v` | float | `3.00` | 0…10 | **Cutoff cell voltage** — below this the battery latches into an emergency charge and may not discharge |
| `protection.cell_voltage_resume_v` | float | `3.18` | 0…10 | **Resume cell voltage** — at or above this the latch clears; must be above the cutoff |
| `protection.emergency_charge_power_w` | float | `0` | 0…100000 | **Emergency charge power W** — charge power for a latched battery, capped by its own max charge; 0 = use `max_charge_w` |

`warning_v` and `resume_v` must both be **above** `cutoff_v`; the check is enforced at save time on
the patch's final state, so moving cutoff and warning together in one `PATCH /api/v1/config` is
judged as a whole. The ordering is validated even while `cell_voltage_enabled` is false.

#### Maintenance (scheduled full charge)

| Key | Type | Default | Bounds | Label — help |
| --- | --- | --- | --- | --- |
| `maintenance.full_charge_enabled` | bool | `false` | — | **Scheduled full charge** — reach the target SoC at least every interval so the BMS can balance its cells; the optimiser charges in the cheapest slots before the deadline while advisory still writes nothing |
| `maintenance.full_charge_interval_days` | int | `30` | 1…365 | **Full-charge interval (days)** *(advanced)* — how often every enabled battery must reach the target SoC |
| `maintenance.full_charge_target_soc_pct` | float | `100` | 50…100 | **Full-charge target %** *(advanced)* — terminal SoC the plan aims for while a full charge is due; a higher `optimizer.target_soc_pct_end` still wins |

Per-battery runtime state (`last_completed`, `next_due`, `first_seen`, `overdue_alerted_at`) is
never a setting: it lives in the store profile (`kind='maintenance'`, `key='full_charge'`) and is
reported on `GET /api/v1/batteries` (`maintenance`) and inside the `full_charge` block on
`GET /api/v1/status`. A battery with no history is due immediately; reaching the target
(measured SoC ≥ target − 1 point) records `last_completed` and moves the deadline an interval out;
a deadline past `due + 24 h` raises the rate-limited `full_charge_overdue` alert and keeps trying.

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

`solar.sources` — a list of merge entries (SPEC §7.1.3). Each needs `source` ∈
`forecast_solar | ha_energy | capacity | derived` and accepts `weight` (default 1, a **finite number
> 0**), an optional `name`, and the provider-specific keys for that source: `planes` / `api_key`
(`forecast_solar`), `entry_id` (`ha_energy`), and `capacity_wp` / `tilt_deg` / `azimuth_deg` /
`performance_ratio` / `weather_entity` / `nasa_irradiance` (`capacity` and `derived`). An unknown key
or a bad value fails validation with a message naming the entry (`solar.sources[0].weight must be a
finite number > 0`); `PATCH /api/v1/config` reports it per key and nothing is applied.
`solar.merge_strategy` must be `weighted_mean`, `median` or `max`.

**Group names in the API** — `SETTINGS_META.group` is lowercase; `GET /api/v1/config` title-cases it
(`general` → `General`), and moves exactly ten keys into the `Safety` group regardless of their
`SETTINGS_META` group: `optimizer.reserve_soc_pct`, `optimizer.max_grid_import_w`,
`optimizer.max_grid_export_w`, `optimizer.allow_export`, `control.failsafe_on_error`, and the five
`protection.cell_voltage_*` keys.

**Secrets are never echoed** — any settings key whose name contains `key`, `token`, `secret`,
`password` or `passwd` is reported with type `password` and its value replaced by `***` when set (for
example `solar.api_key`). The same masking applies to battery mapping credentials (a
`zendure_native` `token`): a read-back shows `***`, and a PATCH that echoes the mask or omits the key
keeps the stored value. The form shows `(a value is set; leave empty to keep it)` and only sends a
new value when the user types one.

#### Advanced (never folded) set — frozen

`mode`, `dry_run`, `notify.enabled`, `price.provider`, `price.export_fee_eur_per_kwh`,
`price.feed_in_energy_tax_refund`, `price.fixed_price_eur_per_kwh`,
`price.fixed_day_price_eur_per_kwh`, `price.fixed_night_price_eur_per_kwh`, `grid.power_entity`,
`solar.enabled`, `solar.source`, `solar.measured_entity`, `optimizer.horizon_hours`,
`optimizer.allow_grid_charge`,
`optimizer.min_spread_eur_per_kwh`, `optimizer.sell_floor_enabled`, `optimizer.pv_charge_preference`,
`maintenance.full_charge_enabled`,
plus the ten Safety keys. Everything else is `advanced: true` (this includes the advanced wear
override `optimizer.cycle_cost_eur_per_kwh`, `optimizer.sell_floor_margin_eur_per_kwh`,
`solar.nasa_irradiance` and the two `maintenance.full_charge_*` tuning keys).

### 3.4 Battery configuration fields (`models.BatteryConfig`)

| Field | Type | Default | Validation / meaning |
| --- | --- | --- | --- |
| `id` | str | required | stable slug, unique |
| `name` | str | — | display name |
| `driver` | str | required | `zendure`, `zendure_mqtt`, `zendure_native` or `generic` |
| `capacity_kwh` | float | required | must be > 0; usable AC-side capacity. In `learned` mode this is the seed/fallback |
| `capacity_source` | str | `manual` | `manual` uses `capacity_kwh`; `learned` uses the estimate measured from the pack's own cycles once it is confident and fresh (SPEC 5). Validated at save time |
| `min_soc_pct` | float | `10.0` | must be < `max_soc_pct`, both in 0…100 |
| `max_soc_pct` | float | `100.0` | — |
| `max_charge_w` | float | `800.0` | at least one of charge/discharge must be > 0 |
| `max_discharge_w` | float | `800.0` | — |
| `eta_charge` | float | `0.95` | must be in (0, 1] |
| `eta_discharge` | float | `0.95` | must be in (0, 1] |
| `price_eur` | float | `0.0` | ≥ 0; 0 = unknown purchase price, no derived wear |
| `cycle_life` | float | `0.0` | ≥ 0; 0 = unknown rated cycles, no derived wear |
| `control_path` | str | `auto` | how the driver must write: `auto` (driver preference), `manager` (the Zendure manager's system-wide `manual_power`) or `device` (the inverter's own `ac_mode` + `input_limit`/`output_limit`). Validated at save time: `manager` needs `manual_power_entity` + `operation_entity`, `device` needs `charge_limit_entity` + `discharge_limit_entity`. Ignored by `zendure_mqtt` (one path), by `zendure_native` (the device's own HTTP API is the only path) and by drivers without paths of their own |
| `mapping` | dict | `{}` | must be non-empty: "confirm the entity mapping in the UI". `zendure`, `zendure_mqtt` and `generic` map entity ids; `zendure_native` maps connection settings (`host`, `serial`, optional `scheme`/`port`/`api_path`/`token`/`timeout_s`/`insecure_tls`/`allow_unreachable`). A `token` (or any `password`/`secret`/`api_key`) value is stored for the driver, reported as `***` by every API/UI read-back, and kept when a PATCH omits it or echoes the mask |
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

**Learned capacity.** `app/capacity.py` is pure except for `learn_capacity`, which reads the
persisted 5-minute `battery_power:<id>` / `battery_charge|discharge:<id>` and `battery_soc:<id>`
rows and writes a `learning_profiles` payload of kind `battery_capacity`, keyed by battery id (never
a settings key). A *window* is a maximal run of same-direction power broken by a direction change, a
zero sample or a gap longer than one sample; it is accepted only with `|dSoC| >= 10` points and
`mean |power| >= 20 %` of that battery's own limit. The capacity is `AC_kWh * eta_charge / dSoC`
while charging and `AC_kWh / eta_discharge / |dSoC|` while discharging. Accepted windows are
combined with a median after a MAD outlier filter, kept to the last 20, clamped to 1..200 kWh and
published only with at least 3 windows; an estimate older than 30 days is stale. `manual` always
wins; `learned` falls back to the seed for a missing, stale or low-confidence estimate. The engine
resolves the effective capacity in `resolved_config` / `effective_battery_configs` — used by the
drivers, the DP optimiser, allocation, the savings baseline and the published summaries — and
journals a stable reason slug (`capacity_learned`, `capacity_learned_stale`,
`capacity_learned_low_confidence`, `capacity_learned_unavailable`). Re-learning runs in daily
maintenance.

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

**Partial first slot (0.20.1, SPEC 8.16)** — a plan made mid-slot (the engine plans through
`solve_from_forecasts`, whose horizon start is `quarter_floor(now, slot_minutes)`) keeps the
clock-aligned timestamps but models the first slot with only the minutes left in it:
`PlanSlot.minutes = clamp((slot.t + slot_minutes − now) in minutes, 1.0, slot_minutes)`, and every
later slot the full `slot_minutes`. `solve(..., now=now)` sets it; `now=None` keeps the uniform
pre-0.20.1 model, and `now` exactly on the grid reproduces the uniform schedule. The DP
transitions, `power_bounds_kw`, the slot cost, the SoC trajectory, the summary money, the execution
view's intended side and the UI action windows all use `PlanSlot.minutes / 60`, so no reported
energy or money can drift from the schedule. A sub-minute or past remainder floors at 1 minute; a
slot that carries no duration (`minutes = 0.0`, e.g. an older stored plan) falls back to
`plan.slot_minutes`.

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

The forecast records which path resolved, as the additive `feed_in_source` attribute:
`"entity"`/`"flat"` only when that source really resolved, `"derived"` when none was configured,
and `"unavailable"` when a configured source failed and the slots carry the derived price. The
optimizer wiring reads it and only hands a rate to `sources/feed_in.py` as an authoritative explicit
price when the provenance says `"entity"`/`"flat"` and the value is finite — a
configured-but-unavailable source refuses instead of dispatching on the derived number.

**Contract-correct feed-in accounting (optimizer v2 stage 1b, `sources/feed_in.py`).** The module
`settle_feed_in(settings, slots, period_state, limits)` is *exact-or-refuse*: `explicit` and
`buy_minus_fee` are exogenous and energy-only, so exact; `saldering` is exact only while the period
allowance `I − E` covers the reachable export bound; `tiered` is exact only while the reachable
volume stays below the next tier boundary. Anything else refuses the *battery-sourced* export leg
(`Reason.UNPRICED_EXPORT`) and prices forced PV export at `forced_export_price_floor`, the minimum
over every reachable settlement rate; when even that bound is unknown the result's `floor` is `None`
and no tariff-dependent optimisation may run. The reachable export bound is
`Σ min(export limit, max(0, pv − load) + max_discharge_power) · dt` and is deliberately **not**
capped by the pack's energy (PV surplus can export without the battery, and a grid recharge can feed
a later discharge); any missing, malformed or non-finite bound means *unknown*, never zero.
`saldering` settles import first inside a slot (`eligible = min(export, max(0, I + import − E))`,
excess at the post-saldering feed-in price) and needs the period state; without it, or without the
reachability limits, it refuses, and `price.saldering_assume_nettable` only applies when the state is
unavailable (supplied state wins). Malformed inputs — a non-finite or negative explicit tariff, a
confirmed but missing slot tax, an unresolved configured feed-in entity, a tier table that is not
strictly increasing with finite rates, negative PV/load samples or negative cumulative period
imports/exports/tier volumes — refuse whole rather than being repaired into a number. The derived
`buy − fee − tax` post-saldering rate is used only when no explicit source is configured; a
configured source that is unresolved or invalid refuses instead, because a derived rate could exceed
the real explicit rate and break the lower-bound guarantee.
The module rejects `price.feed_in_mode = derived` outright: that rule is the live legacy path in
`_apply_feed_in`, and re-implementing it here would risk diverging from what actually prices the
forecast. The live forecast path does **not** call the module yet: `price.feed_in_mode` is config-only
until the optimizer consumes the complete result (`export_allowed`/`reason`/`floor`), so a refusal
can never be silently degraded into zero-priced slots. The `period_state` and `limits` inputs are not
yet wired to real sensors or the optimizer — this stage ships the accounting only.

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

**Named devices** (`load.devices`, SPEC §7.2): the base profile is learned from the house total minus
every device that was actually measurable, and each of those is stored as a second profile under the
same `learning_profiles` kind, keyed `device:<entity>`. The forecast is `base + sum(devices)`: every
component is evaluated on the same clock and the bands add. An hour enters the base only when the
total and every subtracted device have a reading for it (a missing reading is unknown, not zero, and
the dropped hours are counted and noted); a device covering under half the house's hours is refused
rather than subtracted from a handful of them. The base records `devices_subtracted` and
`devices_excluded`, and a stored device profile is added back only when the current base names its
key as subtracted — so a device the base still contains can never be counted twice. Cold start adds
no device at all: the flat fallback is the whole house. The kind is authoritative at serving time too:
a device whose stored profile was learned under a different kind has its temperature term dropped
until the next run, and the forecast notes say so.

**Confidence**: `low` with no profile or fewer than `24 * 2` samples; `medium` under 14 days;
`high` at 14+ days and enough samples.

### 4.5 Measurement sampling

Every control tick, mapped roles are read with targeted per-entity state calls (never a full state
snapshot) and upserted into **5-minute** buckets (`MEASUREMENT_MINUTES = 5`).

Roles: `pv_power`, `load_power`, `price`, `grid_power`, `grid_import`, `grid_export`, and per battery
`battery_power:<id>`, `battery_charge:<id>`, `battery_discharge:<id>`, `battery_pv:<id>`,
`battery_output:<id>`, `battery_soc:<id>`.

The per-battery **measured** roles are declared by the battery's driver
(`drivers.measured_roles(mapping)`): by default the generic `charge_power_entity` /
`discharge_power_entity` pair plus `pv_power_entity` / `output_power_entity` / `soc_entity`, and on
the Zendure drivers the native `grid_input_power_entity` (charge) / `output_power_entity`
(discharge) pair — both sides required, exactly like `_power_is_measured`, with the generic keys
winning when both vocabularies are present (0.20.3). `role_entities` resolves that table through
the driver registry and also accepts the engine's driver-resolved mapping per battery
(`Engine.resolved_mappings`), so a role the registry resolver filled in is sampled exactly like a
user-confirmed one and an unmapped battery resolves nothing. `battery_power:<id>` is only the
signed `power_setpoint_entity`: a command, never measured energy.

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

**Model** — discrete slots over `optimizer.horizon_hours` (clamped 12…48 h), each with its own
modelled duration `dt_i = PlanSlot.minutes / 60` (`optimizer.slot_minutes / 60` for every slot but
a partial first one, 0.20.1/SPEC 8.16). Per slot: `pv_i` kW, `load_i` kW, `buy_i` EUR/kWh,
`sell_i` EUR/kWh.

**Objective**:

```
minimise  Σ cost_i  +  wear * Σ delivered_kwh_i  −  salvage * e_N
delivered_kwh_i = max(0, e_i − e_{i+1}) * eta_dis     # wear on the discharge leg only
cost_i  = dt_i * ( buy * max(grid,0) − sell * max(−grid,0) )
          [+ min_spread * max(0, p_i − pv_surplus) * dt_i   when min_spread > 0 and p_i > 0]
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
energy transition:  e_{i+1} = e_i + dt_i*p_i*eta_chg     (p_i >= 0)
                    e_{i+1} = e_i + dt_i*p_i/eta_dis     (p_i < 0)   # exact at p = 0
optimizer.target_soc_pct_end : hard terminal constraint when set
maintenance target        : while a tracked battery is due, the higher of
                            maintenance.full_charge_target_soc_pct and optimizer.target_soc_pct_end
                            (SPEC 8.1, 0.17.0); an inactive block changes nothing
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
  `feasible: false`, `infeasible_reason: terminal_target_unreachable`. The scheduled-full-charge
  target goes through this same path, so charging slots keep the stable `terminal_target` reason.
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

**Cost-basis sell floor (0.18.0; sample-grid booking and pack reconciliation 0.18.1)** —
`optimizer.sell_floor_enabled` (default `true`) gives the forward rule a memory of what the energy
in the pack cost. A per-battery ledger (`app/cost_basis.py`, `learning_profiles` kind
`battery_cost_basis`) is built from **applied** dispatch changes in `auto` (mode `auto`, `dry_run`
false, `result` `ok`/`skipped`, `applied_w` changed by more than 25 W): a change opens a slice and
the next change ends it. The slice's energy is the **measured** `battery_power:<id>` integral (else
`battery_charge - battery_discharge`), integrated over the covered *sample spans* and split at the
command changes and price-slot boundaries they cross (0.18.1), so a command shorter than the sample
spacing still books its share; a data gap beyond 10 minutes is never bridged, nothing beyond the
newest settled sample is booked (an unanswered tail waits for its next sample instead of being
extrapolated flat), and `last_ts` is the end of the last processed sample span — never a command
timestamp — so replaying an update books nothing twice. Each part is priced at the persisted plan
slot it was charged in, split at every price-slot boundary it spans:

```
grid_part = min(charge_ac, import_ac)   # import_ac from the measured meter import
grid_part at the slot's buy, pv_part = charge_ac - grid_part at the slot's sell
basis     = (basis * E + cost_eur) / (E + charge_ac)
E        += charge_ac
E        -= discharged_ac / (eta_charge * eta_discharge)   # a discharge leaves basis unchanged
```

A missing `sell` falls back to `buy`; when neither resolves the sub-slice is skipped and counted,
never priced by a guess. With no meter reading, the plan slot's own reason is the fallback
attribution (`pv_surplus_charge` = PV, anything else = grid). The ledger prices only the energy
above the level the plan never discharges below, `floor_soc_pct = max(min_soc_pct, reserve_soc_pct)`
(0.21.0): at or below that level plus the 1-point margin (`soc <= floor_soc_pct + 1`) it resets to
`basis = None, E = 0`; `E` is clamped at
`capacity_kwh / eta_charge` and cleared with the basis when it reaches 0. A first run, or a gap
longer than the 7-day lookback, starts clean instead of blending old energy in. The last 200 booked
slices are kept as an audit journal, and `/status` exposes the last five per battery.

After booking, the **quantity** is reconciled with the pack's own measured content (0.18.1):
`E_phys = clamp(capacity_kwh * (soc - floor_soc_pct) / 100 / eta_charge, 0, capacity_kwh / eta_charge)`
from the last SoC sample at or before the booking end (a stale or missing sample skips the step and
counts `skipped_no_soc`), with the same `floor_soc_pct = max(min_soc_pct, reserve_soc_pct)` so the
reserve band is never counted into the basis or the fleet floor (0.21.0). The difference is applied to `E` and never to `basis`; the signed
cumulative correction is `reconciled_kwh`, the last step `reconciled_last_kwh`, and a single
correction beyond half the pack capacity resets the ledger (`basis = None, E = 0`) and counts
`reconciled_resets`. All of these travel with the per-battery ledger into `/status`.

A ledger that cannot follow the pack must say so (0.20.3; the freshness rule is 0.20.5). A *stall*
is stale measurements, never an empty booking window: `book_slices` leaves the basis and the energy
exactly as they were, increments the `skipped_no_samples` counter, sets the persisted
`no_samples_since` marker (`stale` in the payload and the API) and logs one warning per stall naming
the battery when no usable power sample exists at all or the newest one is older than
`STALL_TOLERANCE` (15 min = three 5-minute buckets, one whole bucket above the 10-minute
`MAX_GAP_MINUTES` booking gap tolerance, so a healthy sampler -- or one that merely dropped a single
bucket -- can never trip it); the next successful booking with a fresh sample clears the marker.  A
ledger already settled up to the newest sample (an update inside the current sample bin) books
nothing and is **healthy**: it never sets the marker, steps the counter, logs the warning or falls
back to `actuals`.  Both fields survive a restart and are exposed per battery in
`/status.cost_basis` and the HA sensor attributes, so a frozen sell floor is visible instead of
silent. When the per-battery measurement rows hold no usable power point in the window **and
exactly one battery is enabled**, `update_basis` falls back to the fleet-aggregate 5-minute
`actuals` rows (`battery_kw`/`grid_kw`/`soc_kwh`, capped at the newest row) and books measured
energy that was never booked, under unchanged basis semantics (the payload's `sample_source` then
says `actuals`).
With two or more enabled batteries the aggregate cannot be split without guessing, so the fallback
is refused and the stall is reported instead.

**Where the Dashboard reads it (0.20.4).** The Dashboard's **Battery energy price** card is
read-only and shows exactly the ledger the optimiser acts on: `cost_basis.fleet_basis_eur_per_kwh`
(the average charged price, EUR/kWh, three decimals) and `cost_basis.sell_floor_eur_per_kwh` (the
break-even sell price), broken down as `basis ÷ round trip + wear + margin` from the plan summary's
`round_trip_efficiency` and `wear_eur_per_kwh` plus `cost_basis.margin_eur_per_kwh` (a term the
payload does not carry is omitted, never shown as a zero). The context line sums the enabled rows'
`energy_kwh` and `stored_value_eur` (only when every enabled battery reports them) and shows the
oldest `updated_at`; each enabled battery of a multi-battery fleet also gets its own row. The
per-battery `stale` / `no_samples_since` marker turns both readings into a marked stale card and a
warning naming the battery and the moment the samples stopped; a ledger with no basis yet, an empty
pack and `optimizer.sell_floor_enabled = false` each render `--` with the reason. No other endpoint
is fetched for the card.

The floor per battery is `break_even_sell_eur_per_kwh(basis, wear) + margin` (the frozen helper, the
battery's own etas, the advanced wear override when set), and the fleet floor the plan enforces is
the **energy-weighted mean** of those per-battery floors. In the DP a discharge/export transition
whose *value leg* is below the floor is removed: the leg is the slot's `sell` when the flow exports
(`grid_kw_at < 0`) and the slot's `buy` when it serves the house. A slot that holds because of the
floor reports `sell_floor`; charging, SoC-floor holds and everything outside `auto` are untouched,
and with the floor `None` the schedule is bit-for-bit the pre-0.18.0 one.

The floor restricts discharge only (0.19.0): in a slot whose `base = load - pv` is negative (a PV
surplus) the lower bound is `max(grid_lo, -max(0.0, base)) = 0`, so the surplus keeps exporting and
the pack is never forced to swallow it; only a deficit caps the discharge at the house's own demand
(`-base`) or, when `buy < floor`, at a hold. The 0.18.0 branch used `-base`, which in a surplus slot
is a *positive* bound and forced the charge at any price — the live 0.18.1 plan charged 08:00-10:00
instead of the cheaper 11:00-13:00 hours. PV that would go straight to the meter has no cost basis,
so exporting it stays free.

**How PV energy charged into the pack is priced** (0.19.0) is a separate choice:
`optimizer.pv_charge_preference` is `auto` (default), `prefer_pv` or `avoid_pv`. For a charging slot
(`p > 0`) with `surplus = max(0, pv - load)` and `pv_kwh = min(p, surplus)`, the objective adds the
internal shadow price

```
dt * (pv_price - sell) * pv_kwh
```

where `pv_price` is the slot's `sell` for `auto` (the export forgone — the addition is zero, so the
model is exactly the true grid cost and PV is preferred exactly when `sell < buy`), `0` for
`prefer_pv` (absorb the surplus before buying anything) and the slot's `buy` for `avoid_pv` (export
the PV and charge from the grid in the cheapest hours). Only the solver's objective carries the
shadow price: `cost_estimate_eur`, `baseline_cost_eur` and `saved_eur` stay the true grid cost of
the returned schedule (SPEC 8.1), so no preference can invent a saving. `allow_grid_charge=false`
keeps its PV-only bound and `min_spread` keeps charging only the grid part.

**Feed-in contract wiring (stage 1b, `optimizer.feed_in_wiring`).** With `off` (default) nothing
changes: the settlement is never called and the schedule is byte-for-byte the v1 one. With `shadow`
the engine settles the contract and solves a second, compute-only plan, recording the v1-vs-v2
per-slot diff (powers, actions, reasons) in the `feed_in_shadow` learning profile and a
`feed_in_shadow` journal event while v1 remains the plan that is persisted and dispatched; the
shadow v2 row is deliberately never written to `plans`, because the cost-basis ledger resolves the
newest plan per slot and would otherwise price energy at contract values that never ran. With `on`
the settlement's per-slot `sell` replaces the aligned slot price (a pure input change, so the C core
and its validation contract are untouched) and a refused battery-sourced export leg sets
`OptimizerOptions.refuse_battery_export`, which tightens the DP's grid window:

```
allow_export = false            -> lo = max(lo, -base)          # the user's own permission: nothing exports
else refuse_battery_export      -> lo = max(lo, -max(0, base))  # battery-sourced export refused; forced PV surplus still exports
else sell is None or sell <= 0  -> lo = max(lo, -base)          # v1's zero/unknown-price rule
```

In a deficit (`base = load - pv > 0`) the refusal bound is `-base`: the pack may serve the house,
never the meter. In a surplus (`base < 0`) it is `0`: the pack may charge or hold, never discharge,
while the forced PV surplus still exports and is priced at the settlement's justified floor -- a
negative floor is priced as a cost, which is why the refusal branch runs before the `sell <= 0` rule.
`allow_export` stays strictest, so a user who forbids export keeps forbidding it even under `on`. The
C core implements the same branch (`Config.refuse_battery_export`); the runtime validator rebuilds
the bound from `dp.grid_window_kw` and re-derives the one-step choice, so a core that ignores it is
rejected and disabled, never dispatched.

When the contract cannot price the plan at all (`floor is None`, or `price.feed_in_mode` is
`derived`/unknown), `solve_from_forecasts` raises `FeedInUnavailable` and the engine performs no
tariff-dependent optimisation: the plan is withheld (`feed_in_unpriceable`, alert code
`unpriced_export`) so the dispatcher holds at 0 W. That hold is safe on its own, and the independent
safety layer needs no plan -- the cell-voltage guard forces an emergency charge for a latched battery
and the watchdog holds every driver on a stalled loop. A refusal *with* a known floor is different
and stays a normal plan: it optimises self-consumption only, with forced PV export priced at the
floor. The plan and `/status` expose `feed_in_mode` / `feed_in_export_allowed` / `feed_in_reason` /
`feed_in_floor_eur_per_kwh`, and `cost_basis.feed_in_floor_is_bound` tells the dashboard that the
ledger's PV charge price is that conservative bound rather than the real export rate. Today the
period import/export state and the tier table have no configured source, so `saldering` prices live
only through `price.saldering_assume_nettable` and `tiered` refuses.

**Slot classification** (`_classify`) — stable action + reason slugs:

| Condition | Action | Reason |
| --- | --- | --- |
| `p > 0`, on a grid upper bound tighter than the SoC ceiling | charge | `grid_limit_hold` |
| `p > 0`, `p <= pv surplus` | pv_charge | `pv_surplus_charge` |
| `p > 0`, in the terminal tail and `buy >= median` | charge | `terminal_target` |
| `p > 0` otherwise | charge | `arbitrage_charge` |
| `p < 0`, on a grid lower bound tighter than the SoC floor | discharge | `grid_limit_hold` |
| `p < 0`, in the terminal tail and `buy <= median` | discharge | `terminal_target` |
| `p < 0`, `buy >= p75` | export / discharge | `peak_discharge` |
| `p < 0` otherwise | export / discharge | `self_consumption` |
| `p = 0`, on a zero grid bound tighter than the SoC window | hold | `grid_limit_hold` |
| `p = 0`, at `E_min` (within half a step) | hold | `reserve_hold` |
| `p = 0`, at `E_max` | hold | `battery_full` |
| `p = 0`, the cost-basis floor blocks every discharge | hold | `sell_floor` |
| `p = 0`, round trip blocks the arbitrage | hold | `efficiency_loss` |
| `p = 0` otherwise | hold | `spread_below_threshold` |

**Which constraint bound the power (0.20.2).** `power_bounds_kw` still returns the intersected
`(lo, hi, grid_binding)`, but `_classify` no longer reads `grid_binding` as "the power is on a grid
bound": it receives the SoC-safe window (`soc_window_kw`) and the grid window (`grid_window_kw`)
separately and uses a grid reason only on a side where the grid bound is tighter than the SoC bound
and the power sits on it. The 0.19.0 sell floor setting `grid_lo = 0` in a PV-surplus slot is the
case that exposed the old conflation: a charge filling the pack on its SoC ceiling stays
`pv_surplus_charge`, and a discharge parked on the SoC floor stays `peak_discharge`.

Action vocabulary: `hold`, `charge`, `discharge`, `pv_charge`, `export`, `self_consumption`,
`reserve`.

**Summary formulas** (`_summary`, all over the returned schedule):

```
cost            = Σ PlanSlot.cost_eur(minutes_i / 60)
baseline        = Σ (minutes_i / 60) * ( buy*max(load−pv,0) − sell*max(pv−load,0) )
saved           = baseline − cost
charged         = Σ max(0, power_w)/1000 * minutes_i / 60
discharged      = Σ max(0, −power_w)/1000 * minutes_i / 60
pv_charge       = Σ min(max(0,power_w)/1000, max(0, pv−load)) * minutes_i / 60
grid_charge     = max(0, charged − pv_charge)
cycles          = discharged / usable_kwh
arbitrage_kwh   = min(grid_charge, discharged)
cycle_cost_eur  = wear * discharged          # wear is per kWh delivered, charge leg pays none
objective_eur   = schedule objective (grid + min_spread + wear − terminal salvage)
wear_eur_per_kwh
eta_charge / eta_discharge / round_trip_efficiency / break_even_sell_eur_per_kwh
                = the aggregate values the plan assumed, break-even at the horizon's median buy
sell_floor_eur_per_kwh
                = the fleet cost-basis floor the plan enforced (null when off or no ledger) (0.18.0)
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
4b. **Cell-voltage guard** (`protection.cell_voltage_*`, off by default) — evaluated per battery
   before the authority gate, so advisory/off and dry-run alert and log without writing. Below
   `cell_voltage_cutoff_v` the battery latches an emergency charge at
   `min(max_charge_w, emergency_charge_power_w or max_charge_w)` and may not discharge; between the
   cutoff and `cell_voltage_resume_v` discharge is blocked and no charge is forced; at or above the
   resume voltage the latch clears. A stale reading keeps the previous latch (fail-safe, noted); a
   voltage never seen leaves the guard inactive. Reasons `cell_voltage_guard` /
   `cell_voltage_emergency_charge` / `cell_voltage_stale`; the per-battery latch is persisted and
   restored on boot. In a mixed fleet only the latched battery is blocked (the dispatcher passes
   `block_discharge_ids` to `allocate_power`); when every usable battery is latched the aggregate
   floor is raised to 0 W.
5. **SoC clamp** — ceiling: `(E_max − e) / (dt * eta_chg)`; floor: `−(e − E_min) * eta_dis / dt`,
   where `E_min` uses `max(min_soc_pct, reserve_soc_pct)` per battery.
8b. **Cost-basis sell floor** (0.18.0, on by default) — a live *planned* discharge whose value leg
   is below the fleet floor is clamped to a hold (`floor = energy-weighted mean of
   break_even(basis) + margin`); the leg is `sell` when the planned flow exports and `buy` when it
   serves the house. Charging, SoC-floor holds and an explicit override are never touched, and with
   no floor (setting off, or no ledger yet) this clause is a no-op. Reason `sell_floor`.
6. **Power clamp** — aggregated limits `Σ max_charge_w` / `Σ max_discharge_w` (the effective,
   manual-capped maxima of 0.20.0; SPEC 5).
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
discharge is an override, so it is still bounded by the battery's own floor/ceiling/reserve — and by
the per-direction manual limits below.

**Manual limits** (`POST /api/v1/limits`, `DELETE /api/v1/limits`) cap one direction of one battery,
or of the fleet, in watts: `SystemState.manual_limits` is
`{"*": {"charge_w": …, "discharge_w": …}, "<battery_id>": {…}}`, where a missing/`null` value means
no limit and `0` blocks that direction. `models.resolve_manual_limit` resolves one battery direction
as own entry → `"*"` entry → the configured maximum, and never returns more than
`BatteryConfig.max_charge_w`/`max_discharge_w`; a value above that maximum (or below 0) is refused by
the API with `400 invalid_body` naming it, never silently clamped. The resolved cap is applied by
lowering the *effective* config (`max = min(configured, cap)`), which is the number the planner's
bounds, the aggregate safety clamp, the per-battery allocation/headroom, the dispatcher's clamp and
`/status` all read — so a cap constrains the automatic plan and a manual force alike, and the stored
config is never modified. The limits persist in the settings store under `manual_limits` (like
`mode`/`dry_run`, deliberately not a `DEFAULTS` key) and are journaled (`limits_set`,
`limits_cleared`). Unknown battery ids return 404; `charge_w`/`discharge_w` are partial (absent =
unchanged, explicit `null` = cleared).

### 4.10 Drivers

Protocol: `id()`, `read_state()`, `apply(power_w, dry_run)`, `hold(dry_run)`, `capabilities()`.
Writes go through the HA service API or state-setting where no service exists.

Every driver also reads an optional mapped `cell_voltage_entity` into
`BatteryState.extra["cell_voltage_v"]` — the lowest **per-cell** voltage in V, with the same freshness
rules as every other measurement, never a pack total. That mapped entity is the primary cell-voltage
source; `read_cell_voltage()` is the optional driver hook (`zendure_native` implements it from
`packData[].minVol`) used only when no entity is mapped, and it is deliberately off the
`BatteryDriver` protocol so other drivers ignore it. A missing/unreadable reading is None, never a
guess.

- **Idempotent within a slot**: no write when the device already reports the target within tolerance
  (the SolarFlow is BLE/cloud with a finite write budget). Result code `idempotent_skip`. On a
  device-limits path the report that counts is the `input_limit`/`output_limit` echo, never the
  measured flow, which can read 0 W while the limits still execute (0.21.1); an unreadable echo
  fails open to a write.
- **Never assume a write landed**: read back, tolerate the delay until the device reports its next
  event, retry, and treat a persistent mismatch as `driver_error`. "Persistent" means the *write*
  itself never landed: a command that is in force but whose effect the device has not (yet) shown is
  reported as `commanded` — not an error, because the fail-safe hold a driver error triggers would
  revoke a command the device is about to apply. The plan execution line compares measured vs planned
  separately, so "commanded but not following" stays visible.
- **Zendure driver** (`zendure`): two write paths, chosen by `control_path` (`auto` = manager
  first). *Manager*: switch the manager into manual dispatch (`operation = manual`) before commanding
  power, express charge/discharge with the device's own semantics, re-assert manual mode while in
  control (its smart modes overwrite external writes), and write a hold on shutdown/fatal error
  because the integration has no watchdog — but note it applies a setpoint only on the manager's next
  P1 event, so a slow house P1 feed bounds how fast any of this lands. *Device*: the `ac_mode` select
  (direction) plus the `input_limit` / `output_limit` numbers (magnitudes) straight on the inverter,
  with the manager put in `off` so it cannot overwrite them; this path needs no P1 event and is
  confirmed by the device's own property echo. Either way it never writes `soc_set` or `min_soc`
  every tick — SoC floors are expressed as optimised power.
- **Zendure MQTT driver** (`zendure_mqtt`): the same device path for an inverter whose local MQTT
  feature bridges its zenSDK properties into Home Assistant's own `mqtt` integration
  (`number.<serial>_inputlimit`, `select.<serial>_acmode`, `sensor.<serial>_electriclevel`, ...).
  Roles are resolved from the property name in the entity id (the bridge sets no `translation_key`),
  `control_path` does not apply, and the device's `smartmode` switch joins the plan (on for a live
  command, off for a hold) because the device only acts on the AC limits while it is set. Mapping
  keys: `charge_limit_entity` (`inputlimit`), `discharge_limit_entity` (`outputlimit`),
  `ac_mode_entity` (`acmode`), `soc_entity` (`electriclevel`), `soc_target_entity` (`socset`),
  `min_soc_entity` (`minsoc`), `output_power_entity` (`outputhomepower`),
  `grid_input_power_entity` (`gridinputpower`), `pv_power_entity` (`solarinputpower`),
  `smart_mode_entity` (`smartmode`), plus the optional `operation_entity`/`operation_off_value` pair
  that keeps the Zendure-HA manager `off` while this driver controls the same device. The first
  three are required; a save without them is refused, naming the key.
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
battery_flows = measured AC battery_kw only (None -> unknown, never the plan)
charged_kwh   = max(0, battery_kw) * dt_h
discharged_kwh= max(0, −battery_kw) * dt_h
export_kwh  = max(0, −grid_kw) * dt_h
cost        = buy * import_kwh
revenue     = sell * export_kwh
charge_cost_eur      = buy  * charged_kwh        # priced at flow time
discharge_value_eur  = sell * discharged_kwh
# do-nothing grid power g0 (positive = import), two measured forms:
g0 = load_kw − pv_kw                       # when both are measured
   = grid_kw − battery_kw                  # else, the grid identity (SPEC 8.1)
baseline    = buy*max(0, g0)*dt_h − sell*max(0, −g0)*dt_h   # net, battery idle
cycles      = max(0, −battery_kw) * dt_h / capacity_kwh
savings     = baseline − (cost − revenue)
```

`baseline` and `savings` are `None` (unknown) when the load is not knowable — that is, when neither
``load_kw``/``pv_kw`` nor the ``grid_kw``/``battery_kw`` pair is measured — and whenever the price on
the side the baseline lands on is missing. Since 0.30.0 both sides are accumulated interval by
interval: a grid gap skips that interval's baseline *and* the matching actual cost instead of
poisoning the day. Never a fabricated zero.

**Measured-only battery credit (0.13.0).** `battery_kw` is passed only when the driver confirmed a
real flow measurement (``BatteryState.power_measured``). A setpoint read back by the generic driver,
or a Zendure reading that fell back to ``manual_power`` because the measured pair was incomplete, is
an unconfirmed command and is passed as `None`. Such an interval adds nothing to the flows; a later
confirmed interval still counts (0.30.0), and `battery_hours` says how much of the sampled time was
measured. Each day also records the fully measured series (`grid_measured`, `load_measured`,
`pv_measured`, `battery_measured`); rows written before these columns existed carry `NULL` and read
as unknown.

**Ranges** (`range_bounds`): `day` = today from midnight; `week` = 7 days; `month` = 30 days;
`year` = 365 days; `all` = since epoch. Buckets are days, except `year`/`all`, which bucket per month
(`_BUCKET_LENGTHS` = 1 day; year/all are contiguous **calendar months** since 0.30.0, so no month
is skipped and February is never lost to a 30-day step). Empty buckets are kept so the chart stays steady.

**Totals and split** (`savings_from_daily`):

```
arbitrage_kwh         = Σ min(throughput, export_kwh)                 # discharged energy that was exported
self_consumption_kwh  = Σ max(0, throughput − export_kwh)             # the rest displaced import
battery_throughput_kwh = arbitrage + self_consumption                  # measured discharge when known, else the cycle estimate
totals: import_kwh, export_kwh, pv_kwh, cost_eur, revenue_eur, baseline_cost_eur, savings_eur,
        cycles, arbitrage_kwh, self_consumption_kwh, battery_throughput_kwh, buckets,
        measured_periods, baseline_periods,                                  # 0.22.1 coverage counts
        charged_kwh, discharged_kwh, charge_cost_eur, discharge_value_eur,   # 0.13.0, None = unknown
        measured_buckets {grid, load, pv, battery}                            # buckets fully measured
```

A bucket with no stored row reports `has_data: false` and a `None` baseline/savings: it is "no
activity", not a covered 0.00 period, so it never enters the coverage counts. A bucket mixing known
and unknown rows keeps only the known rows (summed per row), so an unknown row's cost is never
subtracted from a known baseline. `measured_periods` is how many buckets have data and
`baseline_periods` how many of those could be re-priced.

A per-bucket flow is `None` only when no row in the bucket measured it; totals sum the rows that did
and the per-series `measured_buckets` counts say how many buckets were measured for *every*
interval. Buckets carry the same flags as booleans (`grid_measured`, `load_measured`, `pv_measured`,
`battery_measured`) plus the sampled-time coverage in hours (`total_hours`, `grid_hours`,
`savings_hours`, `battery_hours`) that the totals sum into `legacy_periods`-aware coverage. All of
this is additive: a legacy row with no such columns reports unknown rather than crashing.

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
| `sensor.sem_battery_cost_basis` | EUR/kWh | `stored_value_eur`, `energy_kwh`, per-battery basis plus `stale`/`no_samples_since` (additive 0.20.3); state `unknown` without a ledger |
| `sensor.sem_battery_value` | EUR | `cost_basis_eur_per_kwh`, `sell_floor_eur_per_kwh`; state `unknown` without a ledger |
| `sensor.sem_forecast_accuracy` | % | `pv_mape`, `load_mape`, `price_mae` |
| `binary_sensor.sem_autopilot_active` | on/off | — |
| `binary_sensor.sem_battery_at_floor` | on/off | — |

No `unique_id` is created, so the mirrors are not in the entity registry and disappear on an HA
restart until the add-on republishes them on its next tick — documented behaviour, not a bug.

### 5.1 Chart feeds (additive 0.28.0, schema 1)

The compact series feeds a dashboard card draws from (`feeds.py`). Every feed carries `schema: 1`,
uses Unix seconds, writes `null` for unknown (never 0), and rounds money to 2 dp, prices to 4 dp,
power to whole watts and percentages to 1 dp. Caps: 200 slots, 400 savings rows, 36 h of forecast.

| Entity | State | Payload / attributes |
| --- | --- | --- |
| `sensor.sem_chart_plan` | `ready` \| `infeasible` \| `empty` | `slots: [t, buy, sell, soc_pct, pv_kw, load_kw, power_w, stale]`, `horizon_start`/`horizon_end`, `slot_minutes`, `feasible`, `infeasible_reason`, `round_trip`, `wear_eur_per_kwh` |
| `sensor.sem_chart_price` | source \| `empty` | `slots: [t, buy, sell, stale]` |
| `sensor.sem_chart_solar` | source \| `empty` | `slots: [t, pv_kw, uncertainty_pct]` |
| `sensor.sem_chart_load` | source \| `empty` | `slots: [t, load_kw, p10_kw, p90_kw]` |
| `sensor.sem_chart_savings` | `ready` \| `empty` | `ranges: {day\|week\|month\|year\|all: [[t, savings_eur, cost_eur, revenue_eur, baseline_cost_eur], …]}` |
| `sensor.sem_chart_accuracy` | scored days \| `empty` | `days: [t, …]`, `pv`/`load`/`price`: `[mape_pct\|null, …]` |

A consumer that does not understand the schema must refuse to draw instead of reading the wrong
column. The card (`custom:sem-charts-card`) does exactly that, and is delivered as one file by
`GET /api/v1/web/sem-charts-card.js` (CSS + vendored uPlot + `charts.js` + the card class); the
install is manual — download into `/config/www/`, register the resource, paste the snippet.

---

## 6. HTTP API surface

Plain JSON over the ingress port, `Content-Type: application/json`, all timestamps ISO-8601 with
offset. Errors are `{"error": {"code": "...", "message": "..."}}` with 4xx/5xx. Every GET must work
in `mode: off` and with no batteries configured (degraded, not broken). No auth of its own — ingress
supplies it (which is why publishing a port would hand battery control to anyone who can reach it).

| Method & route | Body / query | Returns |
| --- | --- | --- |
| `GET /api/v1/health` | — | `{status, version, uptime_s, commit}` |
| `GET /api/v1/status` | — | serialised `SystemState`: `version`, `commit`, `uptime_s`, `mode`, `dry_run`, `pending_holds[]`, `mode_persisted`, `ha_connected`, `ha_last_error`, `location`, `now`, `price_now`, `price_rank_pct`, `pv_now_w`, `load_now_w`, `grid_now_w`, `batteries[]` (each with `cell_voltage_v`, additive 0.18.0 `cost_basis_eur_per_kwh`/`stored_value_eur`, and additive 0.20.0 `max_charge_w`/`max_discharge_w` plus `cap_charge_w`/`cap_discharge_w`, `null` = unlimited), `cell_voltage{enabled,cutoff_v,warning_v,resume_v,emergency_charge_power_w,latched[],batteries[{battery_id,voltage_v,fresh,seen,latched,block_discharge,force_charge_w,reason,note}]}` (additive, 0.16.0), `full_charge{enabled,interval_days,target_soc_pct,overdue_grace_hours,active,due,overdue,batteries[],plan{…}}` (additive, 0.17.0), `cost_basis{enabled,margin_eur_per_kwh,sell_floor_eur_per_kwh,fleet_basis_eur_per_kwh,batteries[{id,name,cost_basis_eur_per_kwh,energy_kwh,stored_value_eur,sell_floor_eur_per_kwh,intervals,skipped_no_price,skipped_no_power,skipped_no_grid,skipped_no_soc,skipped_no_samples,reconciled_kwh,reconciled_last_kwh,reconciled_resets,window_start,updated_at,journal_entries,journal[]}]}` (additive, 0.18.0; the `skipped_no_soc`/`reconciled_*` counters additive 0.18.1; `skipped_no_samples`/`stale`/`no_samples_since`/`sample_source`
additive 0.20.3), `manual_limits{}` (additive, 0.20.0: the raw per-direction mapping), `active_plan`, `next_cheap_window`, `next_expensive_window`, `override` (+ `seconds_left`), `alerts[]`, `provider_status[]` |
| `GET /api/v1/plan` | `hours` | `{generated_at, horizon_start, horizon_end, slot_minutes, feasible, infeasible_reason, mode, dry_run, sources{}, maintenance, summary{…+eta_charge, eta_discharge, round_trip_efficiency, wear_eur_per_kwh, break_even_sell_eur_per_kwh, maintenance{enabled,active,due,overdue,target_soc_pct,effective_target_soc_pct,interval_days,overdue_grace_hours,batteries[],due_batteries[],overdue_batteries[]} (additive, 0.17.0), sell_floor_eur_per_kwh (additive, 0.18.0)}, slots[{t, buy, sell, pv_kw, load_kw, load_p10, load_p90, soc_kwh, soc_pct, power_w, minutes (additive 0.20.1), action, reason, price_stale, pv_source, price_source}], batteries[{id, name, share_w, round_trip_efficiency, wear_eur_per_kwh, break_even_sell_eur_per_kwh}]}`; `slot_minutes` is the plan's own resolution (the UI never assumes one) and `minutes` is the duration the solver modelled for each slot (the full step, or the remaining part of the current clock slot; 0.20.1); with no plan it returns an empty payload with `feasible: false`, `infeasible_reason: "no_plan"` and `slot_minutes: null` |
| `GET /api/v1/plan/execution` | `hours` | additive (0.10.0): `{generated_at, slot_minutes, now, floors{reserve_soc_pct, min_soc_pct, floor_soc_pct}, batteries[{id, name, min_soc_pct, floor_soc_pct, capacity_kwh}], progress{…}, slots[{t, minutes (additive 0.20.1), dispatch, actual}]}` — the same slot grid as `/plan`, joined with the persisted 5-minute actuals and the dispatch log. `dispatch` is the last attempt per battery in that slot (`requested_w`, `applied_w`, `ok`, `dry_run`, `clamped`, `code`, `reason`, `message`, `records`, `batteries[]`) and `actual` the measured aggregate (`samples`, `coverage_pct`, `pv_kw`, `load_kw`, `grid_kw`, `battery_kw`, `planned_kw`, `measured_kw`, `import_kwh`, `export_kwh`, `charged_kwh`, `discharged_kwh`, `soc_kwh_start`, `soc_kwh_end`). Both are `null` for a slot nothing was measured or dispatched in — never a fabricated zero |
| `POST /api/v1/plan/recompute` | — | `{queued: true}` (async; poll status) |
| `GET /api/v1/forecast` | `kind=price\|solar\|load`, `hours` | `{kind, generated_at, source, stale, slots, provider, contract, notes[], reason, hint}` |
| `GET /api/v1/batteries` | — | `{batteries: [{config, state, capabilities, mapping_resolved, discovery[], capacity_source, capacity_kwh, capacity_learned_kwh, capacity_samples, capacity_updated_at, effective_capacity_kwh, capacity_reason, capacity{source,configured_kwh,learned_kwh,samples,updated_at,effective_kwh,reason,stale,confident,applied}, maintenance{id,watching,state,due,overdue,at_target,due_at,due_in_days,last_completed,first_seen,target_soc_pct,interval_days,soc_pct} (additive, 0.17.0)}]}` (the capacity fields are additive, SPEC 5) |
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
| `POST /api/v1/limits` | `{battery_id?, charge_w?, discharge_w?}` (`null` clears, absent = unchanged) | `{manual_limits}`; 404 unknown battery, 400 `invalid_body` below 0 or above the configured maximum |
| `DELETE /api/v1/limits` | `battery_id?` (no id clears every entry) | `{manual_limits}`; 404 unknown battery |
| `GET /api/v1/savings` | `range=day\|week\|month\|year\|all` | `{range, buckets[], totals{}, baseline_note}`; additive 0.13.0: buckets carry `charged_kwh`, `discharged_kwh`, `charge_cost_eur`, `discharge_value_eur` and `grid_measured`/`load_measured`/`pv_measured`/`battery_measured` flags, totals add the same flows plus `measured_buckets` counts (`null` = no confirmed measurement); additive 0.22.1: buckets carry `has_data` (false = no stored row) and totals add `measured_periods`/`baseline_periods` |
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
| `actuals` | per-tick ground truth (pv, load, grid, measured battery, soc, buy/sell price, plan/dispatch power) |
| `dispatch_log` | requested vs applied power, mode, dry run, result, code, message |
| `savings_daily` | the accounting: import/export kWh, cost, revenue, baseline, savings, PV, cycles, measured charged/discharged kWh and their value, per-series coverage flags, sampled hours (total/grid/savings/battery) |
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
| Device meter floors | 48 hourly readings, ≥ 50 % of the house's hours (checked per device and on their intersection) | `sources/load.py` |
| Device temperature kinds | `heat_pump`, `water_heater`, `pool_pump` (not `ev_charger`) | `sources/load.py` |
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
