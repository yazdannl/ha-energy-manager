# Smart Energy Manager — web UI

A single-page app served as static files by the add-on under its HA ingress sub-path. No build
step, no bundler, no framework and no runtime CDN: everything here is plain HTML/CSS/ES2017 loaded
with `<script defer>` in `index.html`.

## Asset map

| File | Role |
| --- | --- |
| `index.html` | Shell: topbar (with the theme control), nav, `<main id="view">`, live region, confirm `<dialog>`, script tags |
| `style.css` | Design layer over Bootstrap's `--bs-*` tokens: palette, spacing/radius scales, every component, motion/focus rules |
| `app.js` | State, hash router, API client, all seven views, confirm + pending/confirmed write handling |
| `charts.js` | uPlot wrappers (`SEMCharts.plan/mini/forecast/savings/accuracy`) with graceful fallbacks |
| `mock.js` | Synthetic fixtures for `?mock=1`; also loadable in Node for the static test suite |
| `vendor/bootstrap.min.css` | Vendored Bootstrap 5.3.3 CSS (MIT) - the reset, grid, utilities, form controls and design tokens. Its JavaScript is **not** vendored; interactions live in `app.js` |
| `vendor/uPlot.min.js`, `vendor/uPlot.min.css` | Vendored uPlot 1.6.32 (MIT). See `vendor/README.md` for version, source URL, checksums and refresh steps |

## Running it

Served by the add-on at `<ingress-url>/`. To iterate without a backend, open any URL with `?mock=1`
appended, e.g. `…/?mock=1#/plan?hours=24`:

- every view renders from `mock.js`, with obviously fake values (`sensor.example_*`, round numbers)
  and **no network calls at all**;
- writes (mode, override, config, batteries, test discharge) are answered by the in-memory mock so
  pending → confirmed flows are visible;
- `mock.js` never touches the DOM or `fetch`, so it can be loaded and inspected in Node, which is how
  the pytest suite asserts the SPEC §10 payload fields.

For a plain static preview (`python3 -m http.server`), `index.html` works but API calls fail unless
`?mock=1` is set.

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
`picker-results`, `picker-row`, `picker-text`, `picker-name`, `picker-meta`, `all-settings`. The theme
control cycles auto -> dark -> light and persists the choice in `localStorage`.

## Settings information architecture (SPEC 13.2)

The Settings page opens on **four setup cards** - Price, Meter, Solar, Battery - carrying only the
primary choice of each topic, with the full form in one collapsed **All settings** drawer below
(`.all-settings`; open state in `sem.settings.allOpen`). The cards own their keys for the current
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
- Writes are never optimistic: the button shows `pending`, then the server response is shown and
  the affected data is re-fetched. Dangerous actions (auto mode, dry-run off, stop-now, test
  discharge, battery enable/delete, hardware-moving settings) require an explicit confirm dialog;
  the most dangerous ones also require a checkbox acknowledgement. `Stop now` uses the atomic
  `POST /api/v1/stop` and writes to the hardware even when `dry_run` is on (dry run only suppresses
  the normal plan dispatcher; advisory mode alone would leave the last battery setpoint in place).
- Test discharge writes a real, bounded setpoint: the field is discharge power (W) limited by
  `max_discharge_w`, the duration is honoured to the exact second (1–60 s), and the result line shows
  the server-confirmed `applied_w` and `seconds` plus the code/message when the write did not land.
- The device has no watchdog: advisory/off and dry run stop new writes, but they do not neutralise a
  setpoint that was already written. The UI says so next to the dry-run switch instead of promising
  "no hardware writes".
- Savings come from the server: `baseline_*` and `savings_eur` may be `null` (no load forecast to
  re-price against) and are rendered as unknown (`--`), never as zero; chart series skip nulls and
  the legend says so. The baseline note is the server's text verbatim.
- Battery efficiency maths is client-side from values the API already returns (per-battery `eta_*`,
  `/status` `price_now`, the `optimizer.cycle_cost_eur_per_kwh` setting): round trip is
  `eta_charge * eta_discharge`, stored kWh all-in is `(buy + wear) / eta_charge`, and break-even is
  `buy / round_trip + wear / eta_discharge`. The form's primary input is
  `round_trip_efficiency_pct` (split evenly into both directions); explicit `eta_charge`/
  `eta_discharge` win and the dialog says so. The plan chart's break-even curve uses the aggregate
  the summary reports and is omitted (with a note) when the summary omits it.
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
