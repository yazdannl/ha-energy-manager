# UI map (WP3)

The SPA lives in `energy_manager/app/web/` and talks only to the frozen API in `SPEC.md` §10.
`README.md` next to the code documents the asset map; this file is the UI tour: routes, panels,
interactions and the design rules the code must keep.

- No build step, no bundler, no framework, no runtime CDN. `index.html`, `style.css`, `app.js`,
  `charts.js`, `mock.js` and the vendored uPlot 1.6.32 files are served as-is.
- Works from 360 px wide to desktop, dark-first, respects `prefers-color-scheme` and
  `prefers-reduced-motion`, keyboard navigable.
- `?mock=1` renders every view from `mock.js` with no network, so the UI can be reviewed without a
  backend.

## Routes

Hash routing (`app.js: splitHash()`), seven views, all rendered client-side:

| Route | View | Purpose |
| --- | --- | --- |
| `#/dashboard` | `viewDashboard` | Live now-cards, alerts, 24 h mini-chart, autopilot controls |
| `#/plan` | `viewPlan` | Aligned timeline, plan summary, per-slot table, recompute |
| `#/forecasts` | `viewForecasts` | Price / solar / load panels, per-day totals, accuracy trend, provider health |
| `#/batteries` | `viewBatteries` | Battery cards, discovery-based add/edit wizard, bounded test discharge |
| `#/savings` | `viewSavings` | Range totals, savings vs baseline chart, per-bucket table, baseline definition |
| `#/settings` | `viewSettings` | Four setup cards (Price, Meter, Solar, Battery) with entity pickers, plus an **All settings** drawer holding the grouped, searchable form |
| `#/log` | `viewLog` | Filterable dispatch/event log with requested vs applied power |

Unknown routes fall back to `#/dashboard`. Query options: `#/plan?hours=12|24|36|48`,
`#/savings?range=day|week|month|year|all`.

## Navigation and reading hierarchy

`index.html` renders **four primary destinations** (Today, Plan, Savings, Manage) as a row that fits
360 px, plus one contextual sub-row that `setNav()` shows only for the current area (Plan →
Plan/Forecasts; Manage → Batteries/Settings/Log). All seven hash routes stay real links, so every
view is reachable and deep-linkable; `#nav a[aria-current="page"]` marks the active entry and the
primary item stays marked while its sub-row is open. Keyboard users get ≥44 px targets.

Each view answers in the same three levels:

1. **Answer** — Today: the control authority, what was measured and what is planned. Plan: the
   saving/loss versus the baseline. Forecasts: measured error and freshness. Batteries: fleet
   readiness. Savings: the comparable saving/loss and its baseline coverage. Settings: the safety
   summary. Log: the latest dispatch outcome.
2. **Short qualification** — one line of context (assumptions, coverage, stale state, the disabled
   prerequisite text beside a test action).
3. **Evidence** — worked examples, raw capabilities, mappings, all slots and JSON live behind named
   native disclosures ("Why this plan?", "Efficiency and wear", "Source details", "Entity mapping",
   "All slots").

`routeTitle()` gives every view one `h1` and one short subtitle; panels are the single surface and
routine metric cards inside them are borderless, so a border means an actionable form, a chart or an
exceptional state.

## Refresh model

Navigation and data refresh are separate. A deliberate route change re-renders the view, scrolls to
the top and focuses the route `h1`; a background poll calls the view's `state.refresh()` hook, which
updates values in place and restores focus to the element that occupied the same position. Views that
can refresh quietly install that hook themselves (Dashboard, Plan, Savings); `asyncPanel` panels
expose `reload()`, which re-fetches and redraws a panel body in place without flashing "Loading…". Every
background refresh (15 s timer and the `visibilitychange` path) is blocked by `refreshBlocked()`
while any dialog is open, a write is pending, the settings form is dirty or a stop result is on
screen — so a poll can never discard an unsaved edit or a failed-stop banner. `once()` clears a
rejected cache slot, so a Retry always makes a fresh request.

## Panels and interactions

**Dashboard (Today)** — the `Autopilot` panel leads with the answer: the control-authority sentence,
what was **measured** and what is **planned**, then the mode Off/Advisory/Auto buttons with
`aria-pressed`, the dry-run checkbox and `Stop now`. The mode buttons are built once outside the
re-rendered area, so the first click applies (an earlier re-render could swallow it) and a refused or
cancelled dry-run change restores the server's confirmed state. Warnings and errors follow first;
informational activity is demoted behind a `Recent activity` disclosure. Then the three primary
cards (battery charge, grid power, price now), the quiet supporting row (PV, load, cheapest/next
expensive window) and the `Next 24 hours` mini-chart (price line + SoC line).

Polls itself every 15 s while the tab is visible; paused while a dialog is open, a write is pending,
the settings form is dirty or a stop-result banner is on screen, so a failed stop stays visible until
dismissed (or the result is dismissed explicitly). One compact **Safety state**
banner appears when `/status` reports a non-empty `pending_holds` (a battery whose last real setpoint
is still live, so the add-on cannot claim it is idle) or `mode_persisted: false` (the running process
has the new mode but a restart would come up in the old one). After a stop the panel re-fetches
`/status` and appends both facts to the stop-result box, so a successful HTTP 200 that did not
persist the mode is never presented as a clean stop.

`Stop now` calls the atomic `POST /api/v1/stop` (SPEC §9.4), keeps its confirm step (checkbox
acknowledgement), disables the button while the request is in flight and renders the full server
response: the resulting mode, whether a manual override was cleared, and one row per battery with
`ok`/`applied_w`/`code`/`message`/`dry_run`. A battery that failed to stop is shown as `FAILED` in
red with a count and a warning, never swallowed. **The stop writes to the hardware even when
`dry_run` is on**: dry run only suppresses the normal plan dispatcher, and advisory mode only stops
*this* add-on from writing — the Zendure integration has no watchdog, so the battery would keep its
last setpoint if the stop were skipped. The banner states this explicitly and notes that
`dry_run=yes` in a result row means the app was in dry run, not that the write was skipped.

**Plan** — horizon selector rewrites the hash; `Recompute` posts `/plan/recompute` and then polls
`/status` (3 s, max 60 s) until `last_plan_at` changes. Warnings panel (infeasible plan, stale
prices, sources), timeline chart, summary (cost, baseline, saving, charged/discharged, cycles,
arbitrage, per-battery shares, and the aggregate **round trip** the plan assumed), slot table with
action, reason and a `stale price` badge. The **Next actions** list states each window's own data:
the buy/sell prices and spread, the break-even and the margin over it, the planned kWh in/out, the
SoC path and the floor it defends, the planned grid flow and what it costs or earns, the price
provider's age and the window's rank in the horizon — plus what actually happened (applied vs
requested, clamped/dry-run/advisory, measured vs planned, or why nothing was dispatched). It reads
`GET /plan/execution` beside `/plan`; both are optional, so the rows state less rather than failing.
The view installs `state.refresh`, so returning to the tab re-reads the plan, its execution join and
the provider freshness in place — it keeps no timer of its own. The price chart draws the **break-even line**
(`buy / round_trip + wear`, from the aggregate the plan assumed) across the
sell prices, so a sell price that looks profitable but sits below the battery's conversion loss is
visibly below the line; that slot stays idle and its reason is `efficiency_loss`, which is distinct
from `spread_below_threshold` (the user's own `optimizer.min_spread_eur_per_kwh` floor). Exports are
plain links to `/api/v1/export?what=plan&format=json|csv`.

**Forecasts** — one panel per kind with source badge, `generated_at` age, `stale`/confidence
badges, notes, chart, per-day totals table. Price: buy bars + sell line, stale slots drawn grey.
Solar: PV area with a ±uncertainty band and peak per day. Load: P50 line with a P10/P90 band.
Accuracy panel shows MAPE/bias/MAE/samples per kind plus a 14-day MAPE trend. A provider table
lists active/healthy/freshness per provider.

**Batteries** — a card per battery: live power/SoC/energy, the **round trip** efficiency in percent
(the input: one number, split evenly into `eta_charge`/`eta_discharge`; explicit per-direction values
override the split and the round trip becomes their product), cycle count (from `state.extra.cycles`,
"not reported" otherwise), limits, SoC window, mapping list and `mapping resolved` / `needs
confirmation` badge. Three derived numbers sit beside the round trip: the **wear** (per kWh delivered,
`price ÷ (cycle life × usable kWh × η_dis)`, entered as the optional purchase price and cycle life in the
edit dialog; 0 when either is unset), and — recomputed from the current
buy price — the **effective cost of a stored kWh** (`buy / eta_charge + wear × eta_discharge`) and the
**break-even sale price** (`buy / round_trip + wear`), each with its formula in the field help. The
advanced `optimizer.cycle_cost_eur_per_kwh` setting replaces the derived wear when non-zero, and the
card says so. Actions:
enable/disable (confirm when enabling), `Test discharge` (disabled unless mode is `auto` and dry
run is off; confirm + checkbox; fails 409 with the server message when not allowed), edit, delete
(confirm + checkbox). The test-discharge dialog is unambiguous that it writes a **real** setpoint
(no dry-run suppression): the field is labelled *Discharge power (W)* and bounded by
`max_discharge_w`, the duration is 1–60 s **at the exact seconds asked for** (no 1-minute floor,
no rounding up), and the confirmation plus the result line render the server-confirmed `applied_w`
and the exact `seconds` from the response, with `ok: false` shown as `FAILED` plus the server code
and message. Add/edit opens a wizard that lists role inputs per driver, loads candidates
from `GET /api/v1/discover?kind=battery` (max 50 rows, optional search filter) and assigns a
candidate to a role on selection; validation rejects an incomplete generic mapping before any
request; saving shows a confirmation dialog listing the exact mapping. A **Find a battery** block
sits above the form: it reads the same discovery rows once on open, groups them per device
(`device_id`, or entity id when the server reports none) and lists only groups that could be a
battery — a known battery platform, or a control role plus a state role, where a control role must
be a *writable* entity (`number`/`select`/`input_number`/`input_select`/`switch`), because
discovery also suggests sensors for setpoint roles and a battery that cannot be driven is not a
battery. Rows are ranked by control-readiness, then mappable roles, then confidence; each row shows
the device label, model/manufacturer, platform, how many roles are mappable and a `control-ready` /
`partial — missing …` / `control entities are read-only` badge. `Select` fills id (slugified,
made unique), name, driver (`zendure` on `platform: zendure_ha` or a zendure-only role, else
`generic`), the best mapping per role at ≥ 0.5 confidence and the numbers the rows state
(capacity in kWh, charge/discharge limits from the mapped entity's `min`/`max`, never the SoC
window), and the note under it says what was filled and that it still needs confirming. Search
filters locally over device name, manufacturer, model, platform and entity id, and only falls back
to the server's entity-text search when the local filter finds nothing. The efficiency field is one
number — the **round trip** in percent, **90 % by default** (about 94.9 % charge *and* discharge,
i.e. `eta = sqrt(0.9)`) — with an advanced *charge/discharge efficiency* override that previews the
resulting round trip and shows which one wins; values that make either direction 0 or greater than
1 (100 %) are rejected inline. The help text states the money maths with the SPEC §6 worked
example: buying at 20 ct/kWh with a 75 % round trip breaks even at 26.7 ct (not 25 ct — that is the
80 % case), and 90 % with no wear gives 22.2 ct.

**Savings** — range selector, totals cards (savings, import cost, baseline cost, import, export,
PV, cycles), arbitrage vs self-consumption split when `totals.arbitrage_kwh` /
`totals.self_consumption_kwh` are present, chart (savings bars + actual net cost / baseline cost lines),
per-bucket table, and the baseline definition text from the server (`baseline_note`) with a
documented fallback. The baseline is the *do-nothing net cost* (import cost minus export revenue
under the same realised load, PV and tariff), and a bucket whose baseline is not knowable — no load
forecast for that interval — is rendered as unknown (`--`), including its savings, rather than as a
zero. When every bucket is unknown the totals show `--` and the chart shows a plain-text fallback;
chart series skip nulls instead of plotting them as 0, and the legend says so.

**Settings** — an always-visible toolbar holds the search field and the `Show all advanced` switch,
and an always-visible **Safety summary** states the authority in force (mode · dry run) with links to
the Safety group and to Today's `Stop now`. Below that the page opens on four dependency-complete
setup cards, each carrying every input the selected option needs: **Price** (`price.provider`, the
provider-specific fields — including the fixed day/night start hours and the day-ahead VAT, energy-tax
and import-fee adders — the feed-in cost and the energy-tax refund toggle), **Meter**
(`grid.power_entity`, a searchable picker), **Solar** (`solar.source` plus a measured-PV picker unless
the source is `derived`, `solar.capacity_wp` for `capacity`, `solar.planes` for `forecast_solar`) and
**Battery** (the fleet from the canonical `GET /api/v1/batteries`, plus a link into the wizard). The pickers ask `GET /api/v1/discover` and write the candidate row's
`setting_key`, falling back to the card's own key; a typed entity id can always be used directly.
`SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})` is the pure table of which keys
the cards own, and the groups below skip exactly those keys, so every key is rendered once. While a
search query is active the cards are hidden and the drawer lists every match.

Everything else lives in the collapsed **All settings** drawer, generated from the `settings` array of
`GET /api/v1/config` (`key, value, type, default, label, help, group, choices, advanced`). Types:
`bool`, `int`, `float`, `str`, `password`, `json`, `list` (+ `choices` for enums and multi-selects).
Search filters by key/label/help/group. Changes are collected and sent as one `PATCH /api/v1/config`;
the response `{applied, errors}` is displayed verbatim and the form reloads from the server on success
(no optimistic UI). Pending, success and rejected values are shown in the always-visible save bar, not
inside the drawer: a rejected key is attached to its own field with `aria-describedby` and
`aria-invalid`, the field is scrolled into view and focused. The rail and the drawer form sit side by
side from 720 px. Each rail entry counts keys that **differ from their default** ("3 customised");
the save bar counts **unsaved edits**, and a legend line says which is which. Groups are shown in a stable reading order (General, Price, Solar, Load, Grid,
Optimizer, Control, Safety, Notify, Batteries) even though the API sorts rows by key. Groups whose name
matches `safety`/`limit`, and keys matching
`reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export|control.failsafe_on_error`,
are rendered in a highlighted section (or carry a visible `money/safety` badge when the server puts
them elsewhere) and require a confirmation dialog before saving. New schema in the 0.6.0 round:
`price.feed_in_energy_tax_refund` and `solar.source=derived`; `control.*` tuning moved to the foldable
`Control` group; `price.export_fee_eur_per_kwh` is presented as the **feed-in cost**. When export is
enabled the drawer states which basis prices the sell side (explicit tariff/entity, or the derived
`buy − feed-in cost`). Password fields are never echoed: an existing value shows as "(a value is set;
leave empty to keep it)" and is only sent when the user types a new one. `batteries` is server-managed by the wizard and is rendered as a read-only provenance line linking
to the Batteries view, never as an editable JSON field. An `Add-on options` panel shows the read-only option layer (UI settings win).

**Log** — message-first rows: the message is the row, with badges (level, mode/dry-run context),
local time + relative age, kind, code, requested → applied power and a collapsible JSON detail beneath
it. A segmented preset group (All / Dispatch / Warnings and errors / Events) sits beside the
free-text `kind` filter, row limit, manual refresh and the auto-refresh checkbox (15 s). While a
detail is open a poll never rebuilds the list: it shows a "New entries available" notice instead. The empty
state explicitly says that a surprising situation with an empty log is a bug.

## Cross-cutting rules

- **No colour-only meaning**: each chart legend names its series in words ("Stale price slot",
  "Upper uncertainty (long dashes)"), price bars carry labels, stale slots show a `stale` badge plus
  dashed borders and grey bars, alert levels are written out, and every chart series has a stable line
  signature named in its legend — solid = measurement/forecast, long dashes = the sell/baseline price,
  dotted = a threshold. Legend text is semantic ("Load error (long dashes)"), never a colour word.
- **Every number has a unit**: `fmtPower` (W/kW), `fmtKw` for forecast PV/load **power** (never
  `fmtKwh`, which is energy), `fmtKwh`, `fmtPrice` (EUR/kWh), `fmtEur`, `fmtPct`, `fmtDuration`, cycle
  counts, `°C`. Forecast MAPE is shown in the metric's own unit — kW for PV/load, EUR/kWh for price.
  Unknown values render `--`, a missing daily total is left blank and labelled as uncovered rather
  than zero-filled, and partial baselines carry a visible "Baseline available for N of M periods".
- **Stale data is visibly stale**: `staleBadge()` plus `.card-stale`/`[data-stale]` styling, and
  warnings when the last control tick is older than 180 s or HA is disconnected.
- **Loading/error per panel**: `asyncPanel()` gives every panel its own loading text, error message
  and retry button; view data is fetched once per view via `once()`.
- **Writes are pending → confirmed**: `performWrite()` disables the trigger, announces "pending",
  counts the write so background refreshes wait for it, then shows the server response or the server
  error and re-renders from a fresh `GET`.
- **No watchdog, honest copy**: dry run is labelled "the plan dispatcher writes nothing", never "no
  hardware writes", and the autopilot note plus the auto/dry-run confirms say that advisory/off and
  dry run stop *new* writes but do not neutralise a setpoint already written — Stop now (a real
  write, even in dry run) is the way to make the device idle.
- **Dangerous actions are confirmed**: `confirmAction()` uses a native `<dialog>` with a safe
  default focus (Cancel), Esc to cancel, and an optional checkbox acknowledgement. Used for:
  enabling auto, turning dry-run off, Stop now, test discharge, enabling a battery, deleting a
  battery, safety settings, saving a battery mapping. `Stop now` additionally requires the checkbox
  because it is a real hardware write even in dry run. If a browser cannot show a modal `<dialog>`,
  checkbox-required actions fail closed instead of falling back to a bare `window.confirm`.
- **Contrast and type**: `--text-muted` carries meaningful metadata (labels, help, picker metadata,
  footer) and meets 4.5:1 in both themes; `--text-dim` is only used for genuinely decorative/secondary
  state and is tuned to pass as well. The token type scale (`--type-11` … `--type-28`) is
  used instead of ad-hoc sizes; spacing reuses the 4/8/16/24/32 scale.
- **Touch and focus**: primary navigation, buttons, segmented controls, picker rows and disclosure
  summaries are ≥44 px tall; destructive actions have 8 px separation. `Skip to content` focuses
  `#view` without changing the route hash, and a deliberate route change focuses the new `h1`.
- **Relative URLs only**: the API base is derived from `location.pathname`, so the app works under
  any ingress sub-path. No network at runtime.

- **No instance data in the UI files**: mock fixtures and docs use the reserved fake namespace only
  (`sensor.example_*`, `number.example_*`, `select.example_*`) and round numbers; real entity ids are
  resolved at runtime by the discovery wizard and stored in SQLite, never in the repository.

## Charts (uPlot 1.6.32)

`charts.js` wraps the vendored uPlot IIFE build. Colours are read from CSS custom properties
(`--chart-*`), so light/dark both work. Each chart is three flow siblings — `.chart-plot` (the canvas
at a fixed pixel height), `.chart-legend` (uPlot's legend table mounted there rather than inside the
plot box) and `.chart-caption` (one short line saying what is plotted and what each line signature
means). Keeping the legend out of the plot box is what stopped it painting over the caption on a
phone. Tick labels carry one fixed precision and their unit; a percent axis writes "25 %"…"100 %" in
the ticks instead of a rotated label that collided with them.
 `SEMCharts.refresh()` re-reads them, and `applyTheme()` calls
it on an explicit theme change (not only on a `prefers-color-scheme` change), so a manual theme switch
never leaves a chart in the old palette. Each series also has a non-colour signature (solid / long
dash / dotted) so it is identifiable without colour, the y-axis gutters shrink below ~480 px host
width without dropping the unit label, and the plan chart keeps its break-even threshold dotted and
its sell price long-dashed.
Charts have `role="img"` plus an `aria-label` summary; the numeric detail is always available in the
accompanying tables/cards. Empty series produce a plain-text fallback instead of an empty canvas,
and a missing `uPlot` global produces "Chart library unavailable" rather than a broken page.

## Deliberate simplifications (with upgrade paths)

- **Plan timeline is two aligned uPlot panels** (price + SoC, then PV/load + battery power) instead
  of one plot with four y scales: four axes are unreadable at 360 px. Upgrade path: one plot with
  `scales: {y, y2, y3, y4}` when the layout gains width. Marked with a `ponytail:` comment in
  `charts.js`.
- **Settings safety detection is a name/key heuristic** because the contract only gives `group`.
  Folding follows the server group (`Safety` never folds); the wider key regex only decides the
  confirmation badge and the save confirmation, so a money-moving key the server groups elsewhere is
  still flagged. The backend can make the group exact by naming it `Safety` (the mock mirrors the
  backend regex).
- **Savings arbitrage/self-consumption split is conditional** on `totals.arbitrage_kwh` /
  `totals.self_consumption_kwh`; the UI shows a note when the backend does not report them.
- **Battery cycle count** is read from `state.extra.cycles` and shows "not reported" otherwise.

## Fields the UI needs from the backend

Beyond the literal §10 listing, the UI reads these optional/extended fields; it degrades gracefully
if they are absent:

- `GET /api/v1/plan`: per-slot `soc_pct`, `pv_source`, `price_source` (now carried by
  `models.PlanSlot`; the chart and slot table read `soc_pct` straight from the payload and never
  recompute it); `summary` fields per `PlanSummary`; `batteries[].share_w`; `slot_minutes` (the
  plan's own resolution — the window ranges, the `now` marker, the slot lookup and the "All slots"
  caption all read it, and a missing value falls back to 60 rather than guessing per view).
- `GET /api/v1/plan/execution` (additive, 0.10.0): `floors.floor_soc_pct` for the reserve line,
  `progress` for the "measured so far" summary, and per slot `dispatch`/`actual` as documented in
  REFERENCE §6. A missing or failing endpoint only removes those lines.
- `GET /api/v1/providers`: the `price` list's `active` entry supplies the freshness line
  (`stale` / `last_success`) beside each window's price source.
- `GET /api/v1/config`: `settings[].type` ∈ `bool|int|float|str|password|json|list`, `choices` for
  enums/multi-selects, and a `Safety` group (or the key names listed above) for the confirm step.
- `GET /api/v1/savings`: `totals.arbitrage_kwh` and `totals.self_consumption_kwh` for the split;
  `baseline_*`/`savings_eur` may be `null` (shown as unknown, never `0`) and `baseline_note` is
  displayed verbatim (the UI fallback is the same `analytics.BASELINE_NOTE` string).
- `POST /api/v1/batteries/{id}/test`: the UI sends `{power_w, seconds, intent: "discharge"}` (a
  positive field means discharge) and renders `applied_w`, the exact `seconds`, `ok`, `code` and
  `message` from the response; the server clamps power to `max_discharge_w` and seconds to 1–60.
- `GET /api/v1/batteries` / `PATCH /batteries/{id}`: `control_path` (`auto` | `manager` |
  `device`) is the battery's chosen write path and `capabilities.control_path`
  (`manager_manual_power` | `device_setpoint` | `device_limits` | `unresolved`) the one really in use —
  the card shows both as `Dispatch`, and the dialog offers the three choices with a save-time refusal
  when the chosen path lacks its entities. Dispatch codes include `commanded` (in force, not yet
  confirmed by the device), which the Plan view states in words instead of reporting a failure.
- `driver` may be `zendure_mqtt` (a Zendure inverter bridged by Home Assistant's own MQTT
  integration): the dialog then hides `Control path` (one path only), states that the device's own AC
  mode and limits are written, and `Dispatch` reads `Inverter over MQTT: AC mode + limits (the
  device's own entities)`. `/discover?kind=battery` identifies such a device by platform plus
  manufacturer/model and derives its roles from the zenSDK property names in the entity ids, so the
  pick fills `ac_mode_entity`, `charge_limit_entity`, `discharge_limit_entity`, `soc_entity`,
  `smart_mode_entity`, `grid_input_power_entity` and the rest without a confidence score.
- `driver` may be `zendure_native` (the device's own local zenSDK HTTP API, no integration): the
  dialog then hides `Control path` (the device's own API is the only path), swaps the entity-role
  inputs for the connection fields (`host`, `serial`, `port`, `scheme`, `api_path` and an optional
  password-style `token`), validates them inline and states that the device's local API must be
  enabled in the Zendure app. Saving probes the device once: an unreachable address or a serial the
  device does not report is refused with the reason, while the confirmation names the host and
  serial and asks separately for permission to write the device's AC limits. The card's `Dispatch`
  reads `Device over zenSDK HTTP: AC mode + input/output limits (<host> over HTTP|HTTPS)`.
- `GET /api/v1/batteries`: `capabilities` as a flat object, and `state.extra.cycles` for the cycle
  count. For the efficiency/wear block: `round_trip_efficiency_pct` (the stored input), `eta_charge`
  and `eta_discharge` (the effective values, after the even split or the override), `price_eur`,
  `cycle_life` and the derived `round_trip_efficiency` plus `wear_eur_per_kwh`; the card computes
  the stored-kWh cost and break-even from those and the current buy price.
- `GET /api/v1/plan`: the aggregate `eta_charge`/`eta_discharge`/round trip and the per-delivered-kWh
  `wear_eur_per_kwh` the plan assumed (for the
  summary and the break-even line) and, in `batteries[]`, each battery's own round trip, wear and
  break-even, because per-battery values are not the aggregate mean. Slot `reason` values include
  `efficiency_loss`, which the table renders like any other reason slug.
- `GET /api/v1/accuracy`: `series[].{day, mape_pct, bias_pct}`; `null` MAPE means "no data".
- `GET /api/v1/log`: `kind` values `dispatch`, `event`, `alert`; dispatch rows in
  `detail.{requested_w, applied_w, battery_id, mode, dry_run}`.
- `POST /api/v1/mode` accepts partial `{mode?, dry_run?}`; the UI always sends both.
- `POST /api/v1/stop` is the atomic stop: it returns `{mode, override_cleared, stopped:[{battery_id,
  ok, applied_w, code, message, dry_run}]}` and must perform the 0 W hold even when `dry_run` is on.
- `GET /api/v1/status`: `pending_holds` (list of battery ids with a live setpoint) and
  `mode_persisted` drive the dashboard safety banner.
- `control.grid_follow_max_w` is read by `control/dispatcher.py` and must be present in `DEFAULTS` /
  `SETTINGS_META` for the generated form to render it; at the time of writing it is missing there
  (see the WP3 report), so the mock advertises it as a `float` with default `0` and the UI keeps it
  in the Safety group via the key regex.

## Accessibility notes

Skip link, `aria-current` nav, native `<dialog>` (focus trap + Esc), labelled inputs, tables with
`scope="col"`, `aria-live` status region for write confirmations, `:focus-visible` outlines, no
animation beyond what `prefers-reduced-motion` already disables. The palette is blue/green/orange/
purple and never encodes meaning by colour alone.
