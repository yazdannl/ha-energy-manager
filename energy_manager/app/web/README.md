# Smart Energy Manager — web UI

A single-page app served as static files by the add-on under its HA ingress sub-path. No build
step, no bundler, no framework and no runtime CDN: everything here is plain HTML/CSS/ES2017 loaded
with `<script defer>` in `index.html`.

## Asset map

| File | Role |
| --- | --- |
| `index.html` | Shell: compact topbar (theme control), four-item primary nav plus a contextual sub-row, `<main id="view">`, footer About disclosure, live region, confirm `<dialog>`, script tags |
| `style.css` | Design layer over Bootstrap's `--bs-*` tokens: palette, spacing/radius scales, every component, motion/focus rules. The `--chart-*` tokens live here (the frozen interface); the chart surface itself does not |
| `chart-theme.css` | The chart surface (plot box, legend, caption, hover tooltip) as its own sheet, because two consumers share it: this SPA links it, and the Lovelace card bundle inlines it |
| `sem-charts-card.js` | The Lovelace card class (`custom:sem-charts-card`). Reads only the `sensor.sem_chart_*` feeds and maps each row shape back onto `SEMCharts`; shipped inside the bundle, never loaded by this SPA |
| `sem-charts-card.css` | The card's chrome plus the design tokens `chart-theme.css` expects, defined on `:host` (the SPA gets them from `style.css` on `:root`) |
| `app.js` | State, hash router, API client, all eight views (including the dashboard-card installer), the quiet-refresh lifecycle (`state.refresh`/`refreshBlocked()`), confirm + pending/confirmed write handling |
| `charts.js` | uPlot wrappers (`SEMCharts.plan/mini/forecast/savings/accuracy`) with graceful fallbacks |
| `mock.js` | Synthetic fixtures for `?mock=1`; also loadable in Node for the static test suite |
| `vendor/bootstrap.min.css` | Vendored Bootstrap 5.3.3 CSS (MIT) - the reset, grid, utilities, form controls and design tokens. Its JavaScript is **not** vendored; interactions live in `app.js` |
| `vendor/uPlot.min.js`, `vendor/uPlot.min.css` | Vendored uPlot 1.6.32 (MIT). See `vendor/README.md` for version, source URL, checksums and refresh steps |
| `vendor/InterVariable.woff2`, `vendor/LICENSE.inter.txt` | Vendored Inter variable font (SIL OFL 1.1), served by `@font-face` in `style.css`. See `vendor/README.md` for the checksum |

## Running it

Served by the add-on at `<ingress-url>/`. To iterate without a backend, open any URL with `?mock=1`
appended, e.g. `…/?mock=1#/plan?hours=24`:

- every view renders from `mock.js`, with obviously fake values (`sensor.example_*`, round numbers)
  and **no network calls at all**;- writes (mode, override, limits, config, batteries, test discharge) are answered by the in-memory mock so
  pending → confirmed flows are visible;
- `mock.js` never touches the DOM or `fetch`, so it can be loaded and inspected in Node, which is how
  the pytest suite asserts the SPEC §10 payload fields.

For a plain static preview (`python3 -m http.server`), `index.html` works but API calls fail unless
`?mock=1` is set.

## Dashboard card (SPEC 12)

The Lovelace card runs on the Home Assistant origin, where this add-on's API is unreachable (it is
response-only behind ingress). It therefore reads the six `sensor.sem_chart_*` entities the add-on
publishes, and it is delivered as one file: `GET /api/v1/web/sem-charts-card.js` assembles
`sem-charts-card.css` + `chart-theme.css` (as a `SEM_CARD_CSS` string), the vendored uPlot, `charts.js`
and `sem-charts-card.js` into a single script (uPlot's IIFE is wrapped and assigned to the global the
other two read). The lists live in `api.py` as `CARD_BUNDLE_CSS` / `CARD_BUNDLE_SOURCES`, and
`tests/test_wp3_assets.py` fails if a listed source is missing or names a feed the engine does not
publish. Install is manual - download into `/config/www/`, register `/local/sem-charts-card.js` as a
JavaScript module, paste the snippet - because the add-on would otherwise need `map: config:rw`.

The card's palettes are restated in JS (it renders outside `:root`), and a test pins them to
charts.js's `TOKEN` map and `DEFAULT_PALETTE` so the two copies cannot drift.

## Ingress-safe URLs

The add-on is served below a tokenised path, so the app must never request `/api/...`. `app.js`
derives the base from `location.pathname` (`basePath()`) and builds
`<base>api/v1/<endpoint>`. All static asset URLs in `index.html` are relative. This is enforced by
`tests/test_wp3_assets.py`.

## Design system (SPEC 13.1)

Bootstrap supplies the primitives, `style.css` supplies the design: it maps Bootstrap's `--bs-*`
variables onto this project's palette for both `data-bs-theme="dark"` and `"light"`, and (when the
attribute is absent) for `prefers-color-scheme`. The result is this product's look, not Bootstrap's.
The `--chart-*` custom properties are a frozen interface - `charts.js` reads them through
`getComputedStyle` - so keep the names. Class vocabulary is fixed: the existing names (`panel`,
`now-card`, `chip`, `badge`, `field`, `grid`, `row`, `muted`, `stale`, `btn`, `dialog`) plus
`settings-rail`, `settings-group`, `settings-group-body`, `disclosure`, `disclosure-body`, `save-bar`,
`save-bar-actions`, `segmented`, `switch`, `unit-input`, `theme-state`, plus the setup-card and picker
vocabulary `setup-cards`, `setup-card`, `setup-list`, `picker`, `picker-current`, `picker-search`,
`picker-results`, `picker-row`, `picker-text`, `picker-name`, `picker-meta`, `all-settings`, plus the
layout/answer vocabulary `route-title`, `route-sub`, `answer-authority`, `answer-measured`,
`answer-planned`, `answer-controls`, `answer-notes`, `answer-results`, `answer-dynamic`, `card-flag`,
`supporting`, `trust-line`, `window-list`, `log-list`, `log-message`, `settings-toolbar`,
`settings-columns`, `settings-feedback`, `banner-safety`, `banner-actions`, `dialog-body`,
`dialog-actions`, `chart-card-body`, `chart-card-steps`, `chart-card-snippet`. The type scale is `--type-11` … `--type-28`; every interactive control is
≥44 px tall. The theme
control cycles auto -> dark -> light and persists the choice in `localStorage`.

## Settings information architecture (SPEC 13.2)

The Settings page has an always-visible toolbar (search + `Show all advanced`) and a Safety summary
(mode · dry run with a link to the Safety group and to Stop now), then **four dependency-complete
setup cards** - Price, Meter, Solar, Battery - each carrying every input the current selection needs,
with the full form in one collapsed **All settings** drawer below (`.all-settings`; open state in
`sem.settings.allOpen`; rail and form side by side from 720 px via `.settings-columns`). The Battery
card and the read-only `batteries` provenance line read the canonical `GET /api/v1/batteries`, not the
settings descriptor. Pending/success/server-rejected feedback lives in the always-visible save bar
(`.settings-feedback`), and a rejected key is attached to its field with `aria-describedby`. The cards own their keys for the current
selection (`SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})`, the pure table the
render check asserts) and the groups skip exactly those keys, so every setting the API sends is
rendered **exactly once**. While a search query is active the cards are hidden and the drawer lists
every match. The Meter/Solar/Price cards use `entityPicker` (a searchable `/discover` picker that
writes the candidate row's `setting_key`, falling back to the card's key) and a typed entity id can
always be used directly.

Inside the drawer, a row whose payload says `advanced: true` sits behind that group's
`Advanced (n)` disclosure (state remembered per group; `Show all advanced` opens them all). Rows in
the `Safety` group never fold, even though the wider `isSafetySetting()` (which also drives the
confirmation step for money-moving keys) would otherwise exempt them - folding follows the *server*
group. A field that cannot apply to the selected price/solar source is hidden only while it still
equals its default; once it holds a value it stays listed as a muted "not used by the current
selection" line, and a search match always wins over both rules. Three pure helpers carry this logic
and are tested without a browser: `SEM_APP.settingsGroups(items)`,
`SEM_APP.settingsVisible(item, {showAdvanced, showAll, query, priceProvider, priceContract,
solarSource})` and `SEM_APP.setupCardKeys({priceProvider, priceContract, solarSource})`.

## Conventions

- All data comes from the frozen contract in `SPEC.md` §10; views render missing/optional fields as
  `--` or omit the row instead of inventing values.
- Every number is rendered with its unit (`kW`, `kWh`, `EUR/kWh`, `%`, `cycles`, …).
- Stale data is marked with a `stale` badge, dashed borders and desaturated chart bars, plus a text
  note — never by colour alone.
- Navigation and data refresh are separate. A route change re-renders, scrolls to the top and
  focuses the route `h1`; a background poll calls the view's `state.refresh()` hook, updates values
  in place and restores focus. `refreshBlocked()` stops every background refresh while a dialog is
  open, a write is pending, the settings form is dirty or a stop result is on screen, so a poll cannot
  discard an edit or a failed-stop banner.
- Writes are never optimistic: the button shows `pending`, then the server response is shown and
  the affected data is re-fetched. `performWrite` counts in-flight writes so the refresh guard
  respects them. Dangerous actions (auto mode, dry-run off, stop-now, test
  discharge, battery enable/delete, hardware-moving settings) require an explicit confirm dialog;
  the most dangerous ones also require a checkbox acknowledgement. `Stop now` uses the atomic
  `POST /api/v1/stop` and writes to the hardware even when `dry_run` is on (dry run only suppresses
  the normal plan dispatcher; advisory mode alone would leave the last battery setpoint in place).
- The Dashboard's **Manual override** panel mirrors the safety banner with more detail. Its force
  section posts the unchanged `POST /override`/`DELETE /override` with a signed power (charge `+`,
  discharge `-`, hold `0`), a battery or `All batteries` and a 5/15/30/60/120-minute duration; the
  watt range is the resolved cap, so the form never promises a force the dispatcher will refuse. Its
  limits section has a `Max charge`/`Max discharge` row per battery plus an `All batteries` row
  (`POST /limits` with missing/`null` battery id; `DELETE /limits?battery_id=…`), showing the current
  value and its source (`this battery` vs `all batteries`) or `unlimited`. Limits cap both the
  automatic plan and manual overrides, never raise a battery's configured maximum, persist like
  `mode`/`dry_run` and never bypass the cell-voltage/SOC/reserve rails; the panel says so, and states
  when `mode` is not `auto` (nothing is dispatched) or `dry_run` is on (the override is logged, not
  written — only Stop now writes in dry run). A background poll skips the panel while focus is inside
  it, so a typed value is not discarded.
- Test discharge writes a real, bounded setpoint: the field is discharge power (W) limited by
  `max_discharge_w`, the duration is honoured to the exact second (1–60 s), and the result line shows
  the server-confirmed `applied_w` and `seconds` plus the code/message when the write did not land.
- The device has no watchdog: advisory/off and dry run stop new writes, but they do not neutralise a
  setpoint that was already written. The UI says so next to the dry-run switch instead of promising
  "no hardware writes".
- Savings come from the server: `baseline_*` and `savings_eur` may be `null` (no load forecast to
  re-price against) and are rendered as unknown (`--`), never as zero; chart series skip nulls and
  the legend says so. The baseline note is the server's text verbatim.
- Battery efficiency/wear maths is client-side from values the API already returns (per-battery
  `eta_*`, `price_eur`, `cycle_life`, `wear_eur_per_kwh`, `/status` `price_now`, and the
  `optimizer.cycle_cost_eur_per_kwh` setting as the advanced override): round trip is
  `eta_charge * eta_discharge`, derived wear is `price / (cycle_life × usable kWh × eta_discharge)`, stored kWh
  all-in is `buy / eta_charge + wear × eta_discharge`, and break-even is
  `buy / round_trip + wear`. The form's primary inputs are
  `round_trip_efficiency_pct` (split evenly into both directions) and the optional price/cycle life;
  explicit `eta_charge`/
  `eta_discharge` win and the dialog says so. The plan chart's break-even curve uses the summary's
  reported `wear_eur_per_kwh` (the configured value only as a fallback) and is omitted (with a note)
  when the summary omits the aggregate efficiency.
- The battery dialog opens with a discovery picker: `/discover?kind=battery` rows are grouped
  per device and only groups that could be a battery are listed (a known battery platform, or a
  *writable* control role plus a state role — discovery also suggests sensors for setpoint roles,
  and those are never mapped). `Select` fills the form; the mapping and the numbers are suggestions
  the user confirms before saving. The pure helpers (`batteryGroupsFromCandidates`,
  `batterySelection`, `batterySlugFor`, `batterySearchGroups`) are exported on `SEM_APP` so node can
  test them without a DOM.
- `node --check` must stay clean; the pytest suite in `tests/test_wp3_*.py` covers assets, routes
  and mock payload fields but does not need a browser.
- `node tools/ui_render_check.js` (run by `tests/test_wp3_ui_render.py`) renders the real `app.js`
  and `mock.js` through `tools/ui_dom_shim.js` and asserts the structure: every view renders from
  mock data, every applicable setting is reachable through its group, advanced rows fold and unfold,
  the safety group never folds, no rendered text says `undefined`, every emitted class exists in CSS,
  no asset is remote and `<html>` can carry an explicit theme. It exits non-zero on the first broken
  expectation, so run it after any change to the render functions.
