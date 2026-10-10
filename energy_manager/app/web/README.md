# Smart Energy Manager — web UI

A single-page app served as static files by the add-on under its HA ingress sub-path. No build
step, no bundler, no framework and no runtime CDN: everything here is plain HTML/CSS/ES2017 loaded
with `<script defer>` in `index.html`.

## Asset map

| File | Role |
| --- | --- |
| `index.html` | Shell: header, nav, `<main id="view">`, live region, confirm `<dialog>`, script tags |
| `style.css` | Dark-first design tokens, responsive layout, colour-blind-safe palette, motion/focus rules |
| `app.js` | State, hash router, API client, all seven views, confirm + pending/confirmed write handling |
| `charts.js` | uPlot wrappers (`SEMCharts.plan/mini/forecast/savings/accuracy`) with graceful fallbacks |
| `mock.js` | Synthetic fixtures for `?mock=1`; also loadable in Node for the static test suite |
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
