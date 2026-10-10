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

## Panels and interactions

**Dashboard** — `Autopilot` panel (mode Off/Advisory/Auto buttons with `aria-pressed`, dry-run checkbox, `Stop now`), alert list (level badge + shape cue + text), `Right now` cards (SoC, PV,
load, grid, price + rank, current action + reason, autopilot state, next cheap and next expensive
window), `Next 24 hours` mini-chart (price line + SoC line), battery summary cards. Polls itself
every 15 s while the tab is visible; paused while a dialog is open, and paused while a stop-result
banner is on screen so a failed stop stays visible until dismissed. One compact **Safety state**
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
action, reason and a `stale price` badge. The price chart draws the **break-even line**
(`buy / round_trip + cycle_cost / eta_discharge`, from the aggregate the plan assumed) across the
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
confirmation` badge. Two derived numbers sit beside the round trip, both recomputed from the current
buy price: the **effective cost of a stored kWh** (`buy / eta_charge`, the buy price plus the
charging loss) and the **break-even sale price**
(`buy / round_trip + cycle_cost / eta_discharge`), each with its formula in the field help and the
wear term shown only when `optimizer.cycle_cost_eur_per_kwh` is non-zero. Actions:
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

**Settings** — the page opens on four setup cards, each carrying only the primary choice of its
topic: **Price** (`price.provider`, the provider-specific fields, the feed-in cost and the energy-tax
refund toggle), **Meter** (`grid.power_entity`, a searchable picker), **Solar** (`solar.source` plus a
measured-PV picker unless the source is `derived`) and **Battery** (the configured batteries plus a
link into the wizard). The pickers ask `GET /api/v1/discover` and write the candidate row's
`setting_key`, falling back to the card's own key; a typed entity id can always be used directly.
`SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})` is the pure table of which keys
the cards own, and the groups below skip exactly those keys, so every key is rendered once. While a
search query is active the cards are hidden and the drawer lists every match.

Everything else lives in the collapsed **All settings** drawer, generated from the `settings` array of
`GET /api/v1/config` (`key, value, type, default, label, help, group, choices, advanced`). Types:
`bool`, `int`, `float`, `str`, `password`, `json`, `list` (+ `choices` for enums and multi-selects).
Search filters by key/label/help/group. Changes are collected and sent as one `PATCH /api/v1/config`;
the response `{applied, errors}` is displayed verbatim and the form reloads from the server on success
(no optimistic UI). Groups are shown in a stable reading order (General, Price, Solar, Load, Grid,
Optimizer, Control, Safety, Notify, Batteries) even though the API sorts rows by key. Groups whose name
matches `safety`/`limit`, and keys matching
`reserve_soc_pct|max_grid_import_w|max_grid_export_w|allow_export|control.failsafe_on_error`,
are rendered in a highlighted section (or carry a visible `money/safety` badge when the server puts
them elsewhere) and require a confirmation dialog before saving. New schema in the 0.6.0 round:
`price.feed_in_energy_tax_refund` and `solar.source=derived`; `control.*` tuning moved to the foldable
`Control` group; `price.export_fee_eur_per_kwh` is presented as the **feed-in cost**. When export is
enabled the drawer states which basis prices the sell side (explicit tariff/entity, or the derived
`buy − feed-in cost`). Password fields are never echoed: an existing value shows as "(a value is set;
leave empty to keep it)" and is only sent when the user types a new one. `batteries` is edited in the
Batteries view via a link. An `Add-on options` panel shows the read-only option layer (UI settings win).

**Log** — level filter, free-text `kind` filter (e.g. `dispatch`, `event`, `alert`), row limit,
manual refresh and an auto-refresh checkbox (15 s). Rows show local time + relative age, kind,
level badge, code, message, requested → applied power and a collapsible JSON detail. The empty
state explicitly says that a surprising situation with an empty log is a bug.

## Cross-cutting rules

- **No colour-only meaning**: price bars/legend carry labels, stale slots show a `stale` badge plus
  dashed borders and grey bars, alert levels are written out, chart series use distinct colours and
  each has a legend label. SoC/price lines are distinguishable by position and legend text.
- **Every number has a unit**: `fmtPower` (W/kW), `fmtKwh`, `fmtPrice` (EUR/kWh), `fmtEur`,
  `fmtPct`, `fmtDuration`, cycle counts, `°C`. Unknown values render `--`.
- **Stale data is visibly stale**: `staleBadge()` plus `.card-stale`/`[data-stale]` styling, and
  warnings when the last control tick is older than 180 s or HA is disconnected.
- **Loading/error per panel**: `asyncPanel()` gives every panel its own loading text, error message
  and retry button; view data is fetched once per view via `once()`.
- **Writes are pending → confirmed**: `performWrite()` disables the trigger, announces "pending",
  then shows the server response or the server error and re-renders from a fresh `GET`.
- **No watchdog, honest copy**: dry run is labelled "the plan dispatcher writes nothing", never "no
  hardware writes", and the autopilot note plus the auto/dry-run confirms say that advisory/off and
  dry run stop *new* writes but do not neutralise a setpoint already written — Stop now (a real
  write, even in dry run) is the way to make the device idle.
- **Dangerous actions are confirmed**: `confirmAction()` uses a native `<dialog>` with a safe
  default focus (Cancel), Esc to cancel, and an optional checkbox acknowledgement. Used for:
  enabling auto, turning dry-run off, Stop now, test discharge, enabling a battery, deleting a
  battery, safety settings, saving a battery mapping. `Stop now` additionally requires the checkbox
  because it is a real hardware write even in dry run.
- **Relative URLs only**: the API base is derived from `location.pathname`, so the app works under
  any ingress sub-path. No network at runtime.

- **No instance data in the UI files**: mock fixtures and docs use the reserved fake namespace only
  (`sensor.example_*`, `number.example_*`, `select.example_*`) and round numbers; real entity ids are
  resolved at runtime by the discovery wizard and stored in SQLite, never in the repository.

## Charts (uPlot 1.6.32)

`charts.js` wraps the vendored uPlot IIFE build. Colours are read from CSS custom properties
(`--chart-*`), so light/dark both work; a `prefers-color-scheme` change re-renders live charts.
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
  recompute it); `summary` fields per `PlanSummary`; `batteries[].share_w`.
- `GET /api/v1/config`: `settings[].type` ∈ `bool|int|float|str|password|json|list`, `choices` for
  enums/multi-selects, and a `Safety` group (or the key names listed above) for the confirm step.
- `GET /api/v1/savings`: `totals.arbitrage_kwh` and `totals.self_consumption_kwh` for the split;
  `baseline_*`/`savings_eur` may be `null` (shown as unknown, never `0`) and `baseline_note` is
  displayed verbatim (the UI fallback is the same `analytics.BASELINE_NOTE` string).
- `POST /api/v1/batteries/{id}/test`: the UI sends `{power_w, seconds, intent: "discharge"}` (a
  positive field means discharge) and renders `applied_w`, the exact `seconds`, `ok`, `code` and
  `message` from the response; the server clamps power to `max_discharge_w` and seconds to 1–60.
- `GET /api/v1/batteries`: `capabilities` as a flat object, and `state.extra.cycles` for the cycle
  count. For the efficiency block: `round_trip_efficiency_pct` (the stored input), `eta_charge` and
  `eta_discharge` (the effective values, after the even split or the override) and the derived
  `round_trip_efficiency` plus `break_even_sell_eur_per_kwh`, recomputed by the server from the
  current buy price and `optimizer.cycle_cost_eur_per_kwh` so the card never does the maths itself.
- `GET /api/v1/plan`: the aggregate `eta_charge`/`eta_discharge`/round trip the plan assumed (for the
  summary and the break-even line) and, in `batteries[]`, each battery's own round trip and
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
