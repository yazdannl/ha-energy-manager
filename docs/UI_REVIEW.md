# UI/UX review — external minimalist review (0.7.0)

**Status: addressed (see §0).** The findings below are kept verbatim as the original critique; §0
records where each one was implemented and what still needs a live browser. The deliberate
simplifications and safety rules in [`UI.md`](UI.md) and `SPEC.md` §13 remain in force and were not
relaxed by any change.

**Scope and method.** An independent external review of the 0.7.0 web UI at commit `d2f475f`, asked
to judge it as a modern minimalist product UI. The reviewer read the full source under
`energy_manager/app/web/` (`index.html`, `style.css`, `app.js`, `charts.js`, `mock.js`, vendored
assets), [`UI.md`](UI.md), [`REFERENCE.md`](REFERENCE.md) §2 and the root `README.md` UI sections,
plus a per-route render of all seven views produced by the project's own DOM-shim harness
(`tools/ui_render_check.js` over `mock.js`; charts show their text fallback because uPlot needs a
canvas, and that is a harness artifact). It re-ran the structural check (**42 passed, 0 failed**) and
reproduced two control-path defects in memory with mock data only (the Settings confirmation gap and
the Dashboard mode-button first click, §3.3). No browser, screenshot, live installation, hardware or
owner session was used — real layout, uPlot rendering, screen-reader behaviour and the HA frame are
explicitly listed as open questions in §8.

**Reviewer.** GitHub Copilot "GPT-6.1 Sol" (`github-copilot/gpt-6.1-sol`, thinking: high), run as an
independent agent through Paseo. The review is reproduced verbatim below except for the sentence
naming the temporary artifact directory.

**Findings at a glance.** Six High, five Medium, one Low.

| # | Severity | Finding |
| --- | --- | --- |
| 1 | High | Dashboard does not lead with an unambiguous operational answer |
| 2 | High | Setup cards hide inputs needed to complete the selected setup |
| 3 | High | Hardware-authority changes have inconsistent confirmation and pending behaviour |
| 4 | High | Phone support is containment, not a phone-specific task design |
| 5 | High | Refreshes discard reading context and can bypass the dirty-settings guard |
| 6 | High | Some numeric and state representations break the stated truthfulness contract |
| 7 | Medium | Explanations are valuable, but displayed at the wrong level |
| 8 | Medium | The visual system has too many equal-weight containers and too little type hierarchy |
| 9 | Medium | Minimal-looking metadata currently loses contrast and semantic consistency |
| 10 | Medium | Chart descriptions do not fully solve non-colour identification or phone inspection |
| 11 | Medium | Existing accessibility and feedback components have integration gaps |
| 12 | Low | The shell and status decoration spend attention without answering a new question |

The full ranked critique, the target-state sketch (navigation, reading hierarchy, tokens, charts,
copy policy), the quick-win/structural split with effort estimates, the do-not-touch safety and
accessibility list, and the open questions follow.

## 0. Resolution status

Every actionable finding below has been implemented in the web assets (`app.js`, `charts.js`,
`index.html`, `style.css`, `mock.js`); the review text itself is unchanged, so §3.x still reads as
the original critique. Verification at this revision: `node tools/ui_render_check.js` (44/44) and the
full pytest suite (631 passing, including `test_wp3_ui_render.py`, `test_wp3_assets.py` and
`test_wp3_mock_contract.py`).

| # | Status | Where the fix lives |
| --- | --- | --- |
| 1 | Done | `renderAutopilot()`/`paintAnswer()` lead with authority → measured → planned; warnings/errors first, info demoted to `Recent activity`; one `routeTitle()` per view |
| 2 | Done | `setupCardKeys()` + cards carry every input of the selected option (fixed day/night hours, day-ahead adders, `solar.capacity_wp`, `solar.planes`); Battery card reads the canonical `GET /batteries`; `batteries` is a read-only provenance line |
| 3 | Done | one `modeChangeConfirm()`/`dryRunOffConfirm()` policy shared by Today and Settings, confirmation strings unchanged; mode buttons bound once; dry-run restores the server state on cancel/refusal |
| 4 | Done | four primary nav destinations + contextual sub-row; Plan next-actions vertical window list; message-first Log rows; Savings period+baseline coverage first; ≥44 px targets; test-discharge prerequisite as real text |
| 5 | Done | `state.refresh()` quiet in-place updates with focus restore; `refreshBlocked()` covers dialogs, pending writes, dirty form and stop results; Log shows "New entries available" instead of rebuilding; scroll-to-top only on deliberate navigation |
| 6 | Done | `fmtKw` for PV/load power; per-kind MAE units; no zero-filled daily totals (coverage labelled); distinct Loss/Unknown/Stale flags; flattened availability badges; comparable same-period baseline |
| 7 | Done | answer → qualification → evidence; named disclosures ("Why this plan?", "Efficiency and wear", "Source details", "Entity mapping", "All slots"); `CHOICE_LABELS` display labels without changing API values |
| 8 | Done | one route title, one surface per panel, borderless metric cards, untinted panel heads, token type/spacing scale |
| 9 | Done | `--text-muted` for meaningful metadata with both themes verified ≥4.5:1; rail counts read "N customised" with a legend; confidence is neutral text; warning colour reserved for real limitations |
| 10 | Done | stable line signatures (solid/long-dash/dotted) with semantic legend names; narrow-width axis gutters that keep units; `SEMCharts.refresh()` on explicit theme change |
| 11 | Done | skip link focuses `#view` without routing; route `h1` focused on navigation; save feedback outside the drawer with `aria-describedby` field errors; `once()` clears rejected caches; explicit numeric-column metadata; consistent dialog body/actions; two-column rail ≥720 px |
| 12 | Done | compact topbar ("HA connected", no version), commit behind the footer About disclosure, one-line source metadata, quiet driver/enabled text, functional-only badges |

Still requiring the owner's browser (unchanged from §8): live HA layout and screenshots at 360 px and
zoom, real uPlot legend/cursor/theme behaviour, and screen-reader announcement. Open question 6's
dialog fallback is now safe by construction: a browser without `showModal` fails closed for
checkbox-required actions instead of resolving a bare `window.confirm`.

## 1. Verdict

SEM has the ingredients of a restrained product UI—system fonts, coherent themes, reusable primitives and unusually explicit operational safeguards—but currently reads more like an engineering console than a modern minimalist control room. The single biggest gap is **hierarchy**: the answer, the controls, the explanation and the diagnostic evidence are presented at nearly equal weight, so users must reconstruct what matters. This needs selective structural rework, not a framework migration or merely a lighter palette.

**Review basis.** Read the real `index.html`, `style.css`, `app.js`, `charts.js`, `mock.js`, both UI documents, `docs/REFERENCE.md` §2, the relevant root README sections, both rendered Markdown artifacts and all seven serialized HTML views. Source citations below refer to `energy_manager/app/web/` unless another path is given; artifact citations refer to the per-route render described in the preamble (regenerate it with `node tools/ui_render_check.js`, which renders the real `app.js`/`mock.js` through `tools/ui_dom_shim.js`). No real browser, screenshot, live installation or hardware was used. The existing read-only render check was rerun: **42 passed, 0 failed**. Additional in-memory, mock-only checks confirmed the Settings confirmation gap and Dashboard mode-button first-click problem described below; palette contrast ratios were calculated from the declared colours. Chart-library fallback messages, missing serialized input properties and run-relative timestamps are **not** treated as production defects.

## 2. What already works

- **A solid, dependency-compatible foundation.** Assets are local, the script order is explicit, and the API base derives from the ingress pathname. Keep this architecture; no design objective here requires React, a bundler, an icon package or a font download. Evidence: `index.html:9–11,73–76`; `app.js:221–241`; web `README.md:34–55`.
- **Operational honesty is a product advantage.** Dashboard copy distinguishes plan-dispatch suppression from a real Stop write; failed per-battery stops, pending holds and unpersisted modes have explicit results rather than a reassuring generic toast. Evidence: `app.js:612–648,701–738,770–771`; `docs/UI.md`, Dashboard and cross-cutting rules. Preserve the substance while shortening repeated prose.
- **Several important accessibility primitives are already present.** Labelled controls, column headers, visible focus, live write announcements, reduced-motion rules and a native confirmation dialog with Cancel initially focused are a good base. This is not a blanket accessibility pass: specific gaps appear below. Evidence: `index.html:54–70`; `app.js:73–85,349–393,402–418`; `style.css:248–263,907–914`.
- **Settings already uses progressive disclosure intelligently in places.** Source-dependent fields, search across groups, preserved non-default inactive values and a dirty-navigation guard are worth retaining. Discovery suggests rather than silently authorises a mapping. Evidence: `app.js:2543–2577,2907–2965,3220–3255,3360–3370`; web `README.md`, Settings IA.
- **Charts have an appropriate basic grammar.** Two Plan plots separate incompatible units; uncertainty bands, legends, accessible descriptions and null-preserving series are preferable to decorative gauges. Savings explicitly defines its counterfactual baseline. Evidence: `charts.js:129–164,238–317,372–416,418–455`; `app.js:2470–2474`; `view_savings.html`.

## 3. Top issues

Ranked by user impact and redesign leverage. **High** means misunderstanding, unsafe authority changes, lost work or failure of a core phone task; **medium** means persistent readability/consistency friction; **low** means secondary polish.

### 1. HIGH — Dashboard does not lead with an unambiguous operational answer

**Problem →** Autopilot controls and the entire alert list precede nine equally styled “Right now” cards; mode is repeated in the header, control panel and card grid. More importantly, “Plan action: PV charge … target 800 W” sits alongside advisory/dry-run status without saying that it is a proposal, while the battery reports actual discharging at −240 W.

**Why it matters →** The user must mentally separate intended dispatch from measured behaviour. The fixture is not proof of a control fault, but it demonstrates precisely the ambiguity a safety-adjacent dashboard must resolve. Broad grid equality also makes a future expensive window compete with present control state.

**Concrete fix →** Lead with a single operational sentence and two explicitly separate readings: **“Advisory · dry run: plan dispatcher writes nothing”**, **“Measured battery: discharging 240 W”**, **“Planned now: charge 800 W from solar”**. Put unresolved safety conditions immediately next to that answer. Follow with battery charge %, grid direction and current price as the primary metrics; put PV/load and next-window information in a quieter supporting row. Keep Stop now accessible; move routine mode editing to a labelled “Control settings” disclosure. Show actionable warnings above routine metrics, but demote “new plan published” to activity history.

**Evidence →** `app.js:497–510,789–812,841–870`; `.cards`, `.card-label`, `.card-value` in `style.css:389–408`; `rendered-outline.md`, Dashboard; `view_dashboard.html`.

### 2. HIGH — Setup cards hide inputs needed to complete the selected setup

**Problem →** Selecting Solar “capacity” produces copy saying “enter the Wp”, but no Wp field on the card. Array capacity and Forecast.Solar planes are classified advanced and live behind All settings → Solar → Advanced. Day-ahead tax/fee inputs and fixed day/night window hours are similarly separated from the primary choice. Safety is not folded *inside* its group, but the group itself is behind the outer drawer and group chooser.

**Why it matters →** “Advanced” is being used as “not on the first screen”, rather than “optional for this user's chosen source”. A user can save an apparently complete setup without the information that source needs. Seventy-seven keys are manageable only if dependency decisions, not blanket folding, determine what is exposed.

**Concrete fix →** Keep the four cards, but make them task-complete: capacity → required Wp; Forecast.Solar → plane input; fixed day/night → rates plus start times; day-ahead → a clearly visible consumer-price/tax summary and relevant inputs. Derive “Needs setup”, “Configured” and “Optional/not used” from actual requirements, not field presence. Put a compact, always-visible safety summary above the drawer, with a direct Safety link. Move Search to the Settings header so it is discoverable without first opening a drawer. Continue rendering each editable key exactly once.

**Evidence →** `app.js:2717–2741,3108–3153,3329–3359`; `mock.js:393,399,487–509`; `view_settings.html`; web `README.md:57–79`; `docs/REFERENCE.md:263–322`.

### 3. HIGH — Hardware-authority changes have inconsistent confirmation and pending behaviour

**Problem →** Settings exposes `mode` and `dry_run` in General, but `isSafetySetting()` recognises neither key. Save confirms only keys recognised by that heuristic. A mock-only check changing Settings mode to `auto` sent a PATCH with **zero confirmation calls**. Separately, the Dashboard mode handler installs `writeAction()` *inside* a click handler: the first Off click installs a listener without sending a request, and the second click sends it. The direct dry-run checkbox also shows the requested checked value immediately and does not restore server state after a failed write.

**Why it matters →** Users get different safety barriers for the same authority change depending on the route, and may interpret a dead first click as a reason to click repeatedly. The requested checkbox state is not the confirmed device-control state.

**Concrete fix →** Centralise the existing safety policy across both control surfaces: enabling Auto and turning dry run off must use the same acknowledgement regardless of entry point. Bind one write handler once per mode button. While a request is pending, keep confirmed mode/dry-run state visibly authoritative, label the requested transition separately, block conflicting submissions and restore/re-fetch on error. A local form preview is fine, but never present it as the current running state. Replace General's “advisory never writes” and unspecific dry-run help with the exact dispatcher/Stop distinction used on Dashboard.

**Evidence →** `app.js:421–435,653–676,680–700,2508–2516,3276–3315`; `mock.js:358–359`; `view_settings.html`, General. Both first-click and confirmation findings reproduced with the project's DOM shim and synthetic mock only.

### 4. HIGH — Phone support is containment, not a phone-specific task design

**Problem →** Seven navigation destinations scroll horizontally at every width; Plan and Savings have ten-column tables, Log seven. At 360 px the nine-card grid tends toward two columns and five rows. Buttons and nav links have compact padding but no minimum touch height; their ordinary line-height/padding resolves to roughly 31–35 CSS px, not a 44 px target. “Test discharge” communicates its disabled prerequisite through a hover title.

**Why it matters →** Off-screen navigation is easy to miss, wide tables split the time/action/reason relationship, and thumb users must repeatedly scroll in two directions. This is not a claim that any sub-44 px target automatically fails WCAG; it is a poor phone interaction target for this product.

**Concrete fix →** Use the four-item navigation model in §4. At ≤600 px show Plan's next action windows as a vertical list with time, action, signed power, reason and stale state together; retain the complete slot table behind “All slots”. Show Log as message-first rows with requested/applied power and code beneath, JSON collapsed. Savings needs period + saving + baseline coverage first, remaining accounting under detail. Give interactive buttons, navigation, picker actions and disclosure summaries a ≥44 px target, with 8 px separation around destructive controls. Display “Requires Auto and dry run off” as real text next to the disabled test action.

**Evidence →** `index.html:31–41`; `style.css:294–325,492–550,817–825,896–905`; `app.js:933–960,1870–1880,2483–2489,3396–3413`; all three corresponding HTML artifacts.

### 5. HIGH — Refreshes discard reading context and can bypass the dirty-settings guard

**Problem →** Dashboard polling calls `renderRoute()`, which destroys the DOM/charts and scrolls to the top. Log refresh clears and rebuilds entries, including expanded JSON. Returning to a visible tab calls `safeRefresh()` → `renderRoute()` directly, rather than consulting Settings' navigation guard. `anyDialogOpen()` checks only the shared confirmation dialog, not the battery-edit or test dialog. A stop-result banner suppresses the Dashboard interval, but not this visibility-refresh path.

**Why it matters →** A person inspecting a lower card can be pulled back to the top every 15 s. Switching to HA to find an entity and returning can destroy unsaved settings; expanded evidence and stop results can also disappear. Actual iframe/visibility behaviour needs browser verification, but the unconditional source paths are clear.

**Concrete fix →** Separate user navigation from data refresh. Navigation may deliberately move to the route heading; background updates must not scroll, replace focused controls, erase expanded detail, clear unsaved values or dismiss stop outcomes. Keep one refresh guard covering any open dialog, pending write, dirty form and unresolved result; update live values and chart data in place where feasible. For Log, pause auto-refresh while reading/opening details or show “New entries available” rather than rebuilding the current evidence. Announce significant state changes, not every unchanged poll.

**Evidence →** `app.js:344–347,476–495,560–582,863–869,3396–3413,3428–3454`; `charts.js:105–124`. These are source-backed failure paths, not a claim of completed browser testing.

### 6. HIGH — Some numeric and state representations break the stated truthfulness contract

**Problem →** Plan sends `pv_kw` and `load_kw` directly to `fmtKwh()`: power is labelled as energy. Accuracy displays MAE without a unit. Solar/load daily reducers replace missing values with zero, risking fabricated totals. Savings uses the same dashed “stale” card treatment for a negative result and missing data. Dashboard's availability badges are passed as a nested array to a helper that stringifies nested arrays; the artifact consequently shows `[object Object]` instead of the badge. Savings' partial-known fixture shows 4.04 EUR without a visible coverage count, although one bucket's baseline is unknown.

**Why it matters →** A clean interface cannot be trustworthy if a unit is wrong, missing data resembles measured zero, or a negative saving resembles a stale reading. Partial-baseline totals also need scope disclosure to avoid appearing to describe the whole range.

**Concrete fix →** Render PV/load as kW, or explicitly calculate slot kWh using the slot duration if energy is intended. Give MAE the metric's actual unit from the API contract; do not guess across kinds. Preserve unknown/partial totals and label coverage. Use separate visible labels for “Loss”, “Unknown” and “Stale”. Flatten the availability-badge children at the call site. On Savings, show “Baseline available for 9 of 10 periods” in this fixture; compare baseline with actual net cost over the *same* covered periods, keeping whole-range import/export separately labelled. Do not change accounting formulas merely to make totals look neat.

**Evidence →** `app.js:51–57,834,939,1135–1150,1172,2431–2461`; `style.css:409–410`; `mock.js:623–655`; `view_dashboard.html`, `view_plan.html`, `view_forecasts.html`, `view_savings.html`. The object-string rendering is a source defect; its exact string will differ between shim and browser.

### 7. MEDIUM — Explanations are valuable, but displayed at the wrong level

**Problem →** Plan repeats the conversion-loss/break-even explanation below both chart and summary. Batteries exposes ten statistics, a long formula paragraph, mapping keys and raw capabilities by default. Forecasts prints every Solar note before the chart; accuracy and provider health appear after all three forecasts. Technical option values (`day_ahead`, `forecast_solar`) are user-facing labels.

**Why it matters →** The product makes a simple check feel like a reading assignment. Shrinking that prose to 0.78 rem does not reduce cognitive load; it makes important content harder to read.

**Concrete fix →** Adopt three levels: **answer → short reason/qualification → evidence**. Keep active safety warnings, stale/unknown labels, material source limitations and the counterfactual savings qualification in place. Put worked examples, raw capabilities, configuration keys and full model assumptions behind named native disclosures: “Why this plan?”, “Efficiency and wear”, “Source details”, “Entity mapping”. Add display-label maps without changing API values: “Wholesale day-ahead”, “Array capacity”, “Learn from meter”, “Charge level (%)”. Show “Price does not cover conversion losses” with `efficiency_loss` available in detail; keep reserve-floor and minimum-spread explanations distinct.

**Evidence →** `app.js:177–210,877–930,1035–1049,1089–1117,1832–1922,2779–2803`; `rendered-text.md`, Plan/Forecasts/Batteries/Settings. Full reason codes remain useful diagnostic evidence, not primary prose.

### 8. MEDIUM — The visual system has too many equal-weight containers and too little type hierarchy

**Problem →** Panels have borders, tinted headers and borders under those headers; their stat cards are bordered again. Batteries nests a panel within a panel; Settings nests setup cards, drawer, group and advanced disclosure. There is no visible route-level H1 in the view renderers. The type system mixes many close sizes, uppercase card labels and small monospace metadata.

**Why it matters →** The interface looks compartmentalised rather than prioritised. A border announces “this matters” almost everywhere, so the most important information must compete using yet another badge or paragraph.

**Concrete fix →** Use one route title, one primary answer and section spacing. Retain a bordered surface for actionable forms, charts and exceptional states; turn routine metric cards into borderless blocks within one surface, and remove tinted header bands from ordinary panels. Use the token scale in §4. Keep monospace for entity IDs/raw data, not version or ordinary dates. Reduce radius variants to existing 10 px/6 px; do not introduce gradients, glass, floating shadows or decorative illustrations.

**Evidence →** `style.css:227–245,361–410,635–659,661–700`; `view_batteries.html`; `rendered-outline.md`, all routes. Rendered screenshots are still needed to judge the final optical balance.

### 9. MEDIUM — Minimal-looking metadata currently loses contrast and semantic consistency

**Problem →** `--text-dim` is used for small configuration keys, picker metadata and footer text. Calculated contrast is approximately **4.27:1** on the light white panel and **3.69:1** on light `--bg-alt`; the dark dim colour on the dark panel is approximately **4.43:1**. These miss 4.5:1 for normal-size text in the stated pairings. Settings rail badges count deviations from defaults, while the save bar counts unsaved edits; “Price 3” alongside “No unsaved changes” is therefore correct but unexplained. Medium load confidence gets a green `badge-ok`.

**Why it matters →** “Quiet” is becoming hard to read or falsely cautionary. A warning-coloured non-default count is not an error, and “medium” confidence is not synonymous with good.

**Concrete fix →** Use `--text-muted` for meaningful metadata and verify both themes, including stale badges. Keep the dim token only where contrast still meets the applicable requirement. Replace always-visible default-deviation pills with “3 customised” in group detail; reserve a clear “2 unsaved” count for draft changes. Use neutral confidence text unless the system has defined trust thresholds. Keep warning colour for real limitations, not legitimate configuration differences.

**Evidence →** `style.css:20,77,279,335–340,581,960–965`; `app.js:1101–1102,2584–2595,3176–3203`; `view_settings.html`. Contrast here is mathematical analysis of solid declared colours, not a browser-wide WCAG certification.

### 10. MEDIUM — Chart descriptions do not fully solve non-colour identification or phone inspection

**Problem →** Several series use colour as their only visible distinction: accuracy lines are all solid; Plan Sell and break-even share the same dash pattern; savings cost/baseline are both dashed. Captions hard-code colour words even though theme palettes can change. Axes allocate 58 px per y-axis and use 10–11 px type, consuming substantial plot width on a phone. Captions promise hover without a deliberate keyboard/touch inspection flow. The manual theme button changes CSS but does not call chart refresh; wrappers retain colours captured when mounted.

**Why it matters →** A labelled legend is necessary, but is not sufficient if a person cannot match same-shaped series without seeing colour. On a phone, long live legends and dual axes may take more space than the data, and a theme change can temporarily produce mismatched chart/UI colours.

**Concrete fix →** Keep the two Plan plots. Assign stable line signatures—e.g. solid forecast, long-dash sell, dotted threshold—and corresponding legend samples; use distinct signatures for the three accuracy series. Use semantic series names in copy, not “orange/purple” as the identifier. Retain every `--chart-*` token name. Provide a selected-slot text readout and previous/next controls or an equivalent accessible value list; dense tables can remain the complete alternative. Tune tick density and axis gutters at narrow widths, not by deleting units. Refresh charts on explicit theme changes. Keep assumptions and stale markers visible; keep full monetary precision in inspection.

**Evidence →** `charts.js:193–226,268–304,333–344,448–453,491–498,506–526`; `app.js:531–553,858,1049,1182`; `style.css:839–850`. Actual uPlot layout, cursor behaviour and palette rendering were not visible in the shim and require browser confirmation.

### 11. MEDIUM — Existing accessibility and feedback components have integration gaps

**Problem →** The skip link changes the hash to `#view`; the router treats that as unknown and falls back to Dashboard (`splitHash('#view')` returns `/dashboard`). Route navigation never focuses its new content. Settings sends Save feedback and rejected values into `listHost`, inside the possibly closed All settings drawer. A retried panel with a rejected `once()` promise reuses that rejection rather than making a fresh request. Shared table rendering right-aligns every cell after the first, including Log messages, reasons and provider names. Battery/test dialogs use direct children while padding targets `.dialog form`; the claimed desktop Settings “rail” is stacked before content because its parent has no two-column layout.

**Why it matters →** Keyboard users can unexpectedly change route instead of skipping chrome; a phone user saving a setup card may see only the count change while the actual error is hidden. The design vocabulary looks reusable but has divergent integration rules.

**Concrete fix →** Make Skip to content focus `#view` without changing the route hash. Focus the route H1/main on intentional navigation, never on polling. Put pending/success/error feedback in the always-visible save bar and attach field errors to their inputs with `aria-describedby`; expand/focus a rejected field when needed. Clear failed fetch caches on Retry and separate core status loading from optional Plan/chart failure. Supply explicit numeric-column metadata; keep prose left-aligned. Give every dialog a consistent padded body and actions area; lay out rail and form side by side above 720 px. Reuse existing `.dialog`, `.disclosure`, `.panel`, `.settings-rail` and `.save-bar` vocabulary rather than creating a second component system.

**Evidence →** `index.html:14,43`; `app.js:73–85,322–339,397–400,448–488,1769–1828,1960–2423,3276–3315,3336–3350`; `style.css:600–617,686,854–867`. See `view_log.html` for prose cells carrying `.num`; see `view_settings.html` for feedback-container location.

### 12. LOW — The shell and status decoration spend attention without answering a new question

**Problem →** Full product name, version/commit, long connection/mode pill and theme-cycle label occupy the topbar; a two-sentence footer repeats unit/time implementation policy on every route. Ordinary provider/source/contract identifiers get three blue badges. There is no general icon set, but absence of icons is not the core deficit.

**Why it matters →** HA already supplies application identity and navigation. Repeating app chrome and status furniture consumes phone space and makes metadata look actionable.

**Concrete fix →** Keep a compact SEM identity, short “HA connected” status and accessible theme control; move commit detail to a native About disclosure and theme preference to Manage/Settings if necessary. Reduce the footer to “Times: local” plus a help disclosure; retain units on every value. Render ordinary source metadata as a quiet text line, reserving badges for stale, unavailable, blocking or changed state. Add only functional chevrons or refresh symbols where they clarify interaction, using CSS or tiny local inline SVG with text labels; do not add an icon font or replace safety labels with symbols.

**Evidence →** `index.html:16–28,47–52`; `app.js:497–510,1091–1104`; `style.css:269–291,335–340,433–479`; `view_forecasts.html`.

## 4. Minimalist target state

### Navigation and shell

Use **Today · Plan · Savings · Manage** as the primary row, fitting at 360 px without sideways navigation. Keep all seven hash routes and deep links: Dashboard maps to Today; Plan/Forecasts share a secondary **Plan / Forecasts** row; Batteries/Settings/Log share **Batteries / Settings / Log** under Manage. Open Manage on Batteries for routine device checking, with direct setup/error links to the relevant Settings group or Log filter. These remain ordinary anchors with correct `aria-current`, not a faux tab widget needing new keyboard infrastructure.

Do **not** merge Savings into an endless Dashboard: long-range accounting is a different job. A small Today saving/unknown summary may link to it if existing data is readily available. Forecasts deserves a reachable trust surface, but not permanent equal billing with the daily operational answer. Keep a compact, text-labelled Stop now action in the operational status strip, sharing the existing confirmation/result flow; avoid a second fixed bottom navigation/control bar inside HA. Test any sticky placement against ingress scrolling before shipping.

### Reading hierarchy

| Surface | First answer | Next layer | Disclosed evidence |
| --- | --- | --- | --- |
| Today | Actual control authority, measured action versus planned action, urgent safety exception | Charge %, grid direction, price; next action/window; short 24 h chart | Mode editing, routine alerts, remaining measurements |
| Plan | Next scheduled action and timing; estimated saving/loss versus baseline for the selected horizon | The two aligned timelines and short action-window list | All 15 min slots, assumptions, efficiency/wear, exports |
| Forecasts | Source freshness/limitations for Price, Solar, Load; observed error and sample sufficiency | Selected forecast chart and daily totals | Full notes, accuracy definitions, all providers |
| Batteries | Fleet readiness and each device's measured state/mapping status | Charge %, power, freshness, limits; Add/Edit | Economics, mappings, capabilities, tests/destructive actions |
| Savings | Estimated counterfactual saving/loss, range and baseline coverage | Comparable baseline/actual net cost and chart | All accounting buckets and methodology |
| Settings | Setup completeness and current authority/safety summary | Four dependency-complete setup cards; visible Search/Save | Optional tuning groups, advanced controls, add-on option layer |
| Log | Latest blocking explanation / dispatch outcome | Time, battery, requested → confirmed applied power, mode/dry-run | Exact code, event metadata and JSON |

For a 360 px viewport, define an acceptance goal rather than claiming a measured fold: the initial app content should communicate authority, actual-versus-planned behaviour and an unresolved exception **before** any grid of secondary metrics. HA chrome and real font/layout determine the final fold. Safety exceptions are allowed to push routine charts down.

### Implementable token and component sketch

Use the existing token and class vocabulary; add a small number of type/layout tokens, not a new component library.

```css
/* Illustrative target tokens, not an implementation patch. */
:root {
  --type-meta: 0.8125rem; /* 13 px at the normal 16 px root */
  --type-body: 0.9375rem; /* 15 px */
  --type-section: 1.0625rem; /* 17 px */
  --type-title: clamp(1.25rem, 1.05rem + 1vw, 1.5rem); /* 20–24 px */
  --type-value: clamp(1.5rem, 1.2rem + 1vw, 2rem); /* 24–32 px */
  --sp-1: 0.25rem;
  --sp-2: 0.5rem;
  --sp-3: 1rem;
  --sp-4: 1.5rem;
  --sp-5: 2rem;
  --radius: 10px;
  --radius-sm: 6px;
}
```

- **Typography:** body 15 px/1.5; meaningful help at least 13 px/1.45, with danger/unknown qualifications at body size where needed. Sentence-case metric labels; medium-weight 17 px section titles; 20–24 px route title; only the leading operational values get 24–32 px. Tabular numerals for amounts; monospace only for codes/entity IDs. Do not rely on small/low-contrast text to manufacture minimalism.
- **Spacing:** 16 px phone gutters rather than the current 8 px; 24 px desktop gutters; 8 px related-item spacing, 16 px within surfaces, 24 px between task sections, 32 px between major groups. Keep `min-width:0` and wrapping rules. Use a deliberate two-column desktop overview and one-column phone story; use two columns on phone only for genuinely short, paired measurements.
- **Surfaces:** retain current dark/light palettes as the starting point. Use page background, one content surface and raised/alternate surface only for inputs, sticky save bar and dialogs. Ordinary section headings share the body surface. One subtle outer border, no nested boxes for every number; use separators for rows. No shadow except the existing sticky affordance/dialog context.
- **Colour:** blue for active navigation/primary actions, neutral text for normal facts, amber for a real degraded/blocked condition, danger for failed/destructive hardware outcomes. Green may mark confirmed success, not every configured value. Keep semantic labels and dashed/shape equivalents. Chart colours are data identities, not alert semantics; retain the frozen `--chart-*` names and their theme variants.
- **Charts:** retain both Plan plots; give the power chart a clear zero reference and explicit charge/discharge direction. Use subdued grids, 1.5–2 px primary strokes, restrained uncertainty fills and differentiated dash patterns. Do not treat SoC's auto-ranged axis as a universal 0–100 gauge; explicitly label its range or use a stable 0–100 range when useful. Short legends name series and units; a detail disclosure explains conversion-loss assumptions. Never interpolate unknown baselines into zeros.
- **Numbers:** keep one locale-aware formatting policy. Money 2 decimals, energy 2, charge level 1, adaptive W/kW; retain tariff precision of 4 decimals in inspection/detail. For prominent tariff values, separate number and visible `EUR/kWh` typographically rather than shrinking the whole number or stripping the unit. Use minute-level relative ages for routine reading, exact seconds for test duration and time-critical diagnostic detail. Make date boundaries/local timezone explicit for overnight windows.
- **Copy:** in-place text answers the question and qualifies uncertainty. One short reason precedes evidence. Technical slugs are preserved in detail/copyable diagnostics. Never collapse active safety warnings, failed Stop results, source-failure hints or missing-baseline qualifications into tooltips. Keep server `baseline_note` available verbatim; retain a short visible “counterfactual, not bill reduction” qualification beside the saving.

**Consistency strategy:** the measured class-token disparity is not proof of 134 missing components: emitted tokens include Bootstrap utilities and dynamic fragments, and the current integrity test passes. Standardise **behaviour** first—dialog body/actions, disclosure styling, state label, numeric columns, pending feedback—then remove redundant classes as those paths converge. Do not delete Bootstrap to improve a stylesheet-size ratio; CSS payload is not the primary UX bottleneck and vendoring is a hard constraint.

## 5. Per-view notes

**Dashboard.** Reorder it into a story: authority/safety → measured versus planned behaviour → three primary metrics → next action → mini-chart. Nine equal cards and a dedicated no-alerts panel should not be the default composition. A measured action different from the advisory plan is information, not automatically an alarm; say so. Correct the missing availability badge before restyling. Link to HA-mirrored state for familiar dashboard use without turning SEM into a replacement for HA. Evidence: `app.js:789–870`; `view_dashboard.html`; root `README.md:189–212`.

**Plan.** Put the horizon selector beside the route title and estimated saving/loss above the timelines; negative saving is important even when the plan is feasible. “No feasibility warnings” should become compact metadata, not an entire panel. Show action windows grouped only while action/reason/stale state remain the same, with per-slot evidence reachable; do not hide constraint changes inside a broad summary. Correct power units, expose full slot duration from the payload rather than assuming all presentations are 15 minutes, and put exports in a small disclosure. Evidence: `app.js:877–1055`; `view_plan.html`.

**Forecasts.** Answer trust before plotting: three compact summaries of source, freshness, limitations, measured error and sample availability, then Price/Solar/Load selection and the chosen chart/totals. Distinguish “fresh but model has assumptions”, “using cached data” and “no source”; never compress these into a green Healthy pill. Move ordinary geometry/provider notes after the chart, but keep stale and source-failure guidance in place. Define typical forecast error in plain language, with MAPE/bias/MAE under detail and units corrected. Evidence: `app.js:1089–1221`; `view_forecasts.html`.

**Batteries.** Prefer one un-nested device surface with a readiness header, measured charge level/power/freshness and Edit; place efficiency/wear and entity mapping in named disclosures, keeping unresolved mappings prominent. Tests and deletion belong in a clearly labelled device-actions section, not equal beside Edit on every header. The current “wizard” is one long form: retain auto-prefill and exact mapping acknowledgement, but split its reading into Select device → Review mappings/limits → Optional economics, using native disclosures before introducing a multistep state machine. Explain disabled test prerequisites in text and retain exact server-confirmed power/seconds. Evidence: `app.js:1832–1956,1960–2423`; `view_batteries.html`.

**Savings.** Make range + saving/loss + baseline coverage the hero; show comparable actual **net** cost, not import cost without export revenue, beside the baseline. Keep the brief counterfactual caveat visible and the full server definition in methodology. A loss is a result, not stale data. Put import/export/PV/cycle detail after the chart; show partial baseline coverage before the user interprets the whole-range figure. Do not equate advisory planning estimates with realised hardware savings. Evidence: `app.js:2431–2500`; `mock.js:623–655`; `view_savings.html`.

**Settings.** Preserve the four topics, schema-driven generator, one-key/one-editor principle and guarded saving. Promote source-required fields, always-visible safety summary, Search and save feedback; show configuration status instead of backend key counts. Render friendly enum names while saving original values. The artifact's “No battery configured yet” conflicts with its Battery fleet because the mock's `batteries` descriptor defaults to an empty list while `/batteries` returns one record; this proves a fixture/data-source mismatch, not necessarily a production bug. Use a canonical inventory or check consistency before treating an empty descriptor as an authoritative fleet answer. Add-on options should remain a read-only provenance disclosure, with UI-overrides clearly identified. Evidence: `app.js:2907–3389`; `mock.js:444,473–476`; `view_settings.html`, `view_batteries.html`.

**Log.** Reframe from a general event table to “Explain a dispatch”: message first, battery/time, requested → applied power, then plain-language blocking reason and exact code. Promote mode/dry-run context out of JSON so “applied” cannot imply a real hardware write when the row represents simulation; the mock demonstrates an applied message with advisory/dry-run detail, not evidence that production hardware was written. Use presets such as Dispatch/Warnings/All rather than requiring a typed kind slug for the common task, while retaining raw filters. Preserve expanded entries during updates. Replace “an empty log is a bug” with “No matching entries” plus clear filters and a separate diagnostic hint. Evidence: `app.js:3396–3457`; `mock.js:685–715`; `view_log.html`.

## 6. Quick wins (≤ a day each) and structural items

Estimates are hands-on time for one developer familiar with this plain-JS app, including a focused mock/render check. They are not independent additive commitments; browser acceptance and backend-contract questions can extend them.

### Quick wins

| Priority | Change | Rough effort |
| --- | --- | --- |
| First | Give Settings Auto/dry-run-off the shared safety confirmation; bind Dashboard mode writes once; restore confirmed checkbox state on failure | 0.5–1 day |
| First | Correct Plan kW labels, flatten availability badges, distinguish loss/unknown/stale, fix MAE units after confirming its contract | 0.5–1 day |
| First | Guard visibility refresh against dirty forms, every dialog, pending writes and stop results; stop automatic scroll-to-top | 0.5–1 day for containment, not full in-place updates |
| Next | Reorder Dashboard answer and Plan summary; demote empty warnings/informational activity; label planned versus measured explicitly | 0.5–1 day |
| Next | Lift required Wp/plane/window fields into their selected setup cards without duplicating editors | 0.5–1 day; plane-form redesign excluded |
| Next | Improve metadata contrast, set 44 px targets, show disabled-test prerequisite, left-align prose table columns | 0.5–1 day |
| Next | Put save feedback outside the drawer; fix Skip-to-content hash collision and intentional-navigation focus | 0.5–1 day |
| Polish | Apply the five-step type scale and flatten metric card/header treatment using existing selectors | 0.5–1 day for CSS, then browser tuning |
| Polish | Use standard disclosure/dialog padding; make the desktop Settings rail an actual two-column layout | 0.5 day |
| Polish | Differentiate chart dashes, refresh on manual theme change, shorten semantic legends and duplicate explanations | 0.5–1 day |

### Structural items

| Change | Rough effort | Boundary |
| --- | --- | --- |
| Four-item primary navigation with preserved routes and contextual subnavigation | 1–2 days | Small HTML/CSS/JS rework; no router replacement |
| Phone-native action-window, dispatch and savings summaries with full evidence retained | 2–4 days | New projections of existing payloads; no chart-library change |
| Dependency-complete setup/readiness model, persistent Safety summary, canonical fleet inventory and contextual field validation | 2–4 days | Mostly generator/IA work; investigate API consistency first |
| Quiet refresh model preserving scroll, focus, form drafts, opened rows and result banners; independent retryable loading | 2–4 days | Shared lifecycle rework, not cosmetic CSS |
| Savings coverage/comparable-cost presentation and forecast trust-first summaries | 1–3 days | Use existing bucket/report data where sufficient; do not invent unsupported metrics |
| Touch/keyboard chart inspection plus accessible value alternatives | 1–2 days | Plain DOM controls and vendored uPlot hooks |
| Browser acceptance in HA at 360/720/desktop, both themes, zoom and keyboard | 1–2 days | Owner/browser access required; not replaced by 44 shim checks |

A **1–2 day CSS-only pass** can improve tone, target size and contrast, but cannot solve authority ambiguity, setup dependencies or refresh data loss. A focused first redesign slice—safety/units first, then Today hierarchy, complete setup and phone navigation—is roughly **5–8 working days**; the broader structural work and browser acceptance are roughly **10–15 working days**, with overlapping work not counted twice. Stay within the existing stack throughout.

## 7. Do-not-touch list

- **No framework/build step/CDN/remote font or new runtime dependency.** Keep vendored Bootstrap CSS and uPlot, relative ingress-safe asset/API paths and the system font stack. Offline assets are required; this does not mean the live UI has no local API requests.
- **Confirmed authority is authoritative.** Preserve pending → confirmed/rejected writes, disabled/busy controls, server result details and re-fetch/read-back. A saved UI setting is not proof that a hardware setpoint was applied.
- **Keep dangerous-action dialogs and checkbox acknowledgements.** Auto, dry-run-off, Stop, real test discharge, enabling/deleting devices, money/safety settings and exact entity-mapping review must retain their appropriate barriers. Fix inconsistent entry paths; do not remove confirmations to reduce click count.
- **Stop now remains a real hardware write even in dry run.** The visible dry-run wording must remain “the plan dispatcher writes nothing”; advisory/off/dry run do not neutralise an earlier setpoint. The device has no watchdog. Keep failed-battery rows, pending holds and mode-persistence failure visible until explicitly resolved/dismissed; never say “all stopped” from HTTP success alone.
- **Retain action versus intent distinctions.** Planned, simulated/requested and server-confirmed applied power need different text. Show charge/discharge direction, not just a signed number or a coloured bar. Test duration is exact seconds, and both result power and duration come from the server.
- **Unknown is not zero.** Keep null baselines/unknown measurements as `--`, preserve gaps in charts and label partial coverage. Correctly distinguish W/kW from kWh; every quantitative value has the proper unit.
- **Staleness and limitations remain visible.** Do not hide stale readings, source-failure hints, infeasibility, unresolved mappings or counterfactual baseline qualifications behind hover-only help. Preserve each stable reason/code in accessible detail.
- **Meaning never depends on colour.** Keep text and border/shape/line equivalents in both themes; make chart signatures more distinguishable, not less. Preserve chart descriptions, legends, textual alternatives and the two-plot Plan separation on phones.
- **Keep keyboard and assistive access.** Native labelled controls/dialogs/disclosures, Cancel as the safe focus default, Escape, visible focus, correct navigation state, semantic table headers, live write feedback, appropriate contrast and reduced motion. Fix skip/focus integration rather than removing it.
- **Keep explicit theme override and auto mode persisted in localStorage.** Chart colours must follow manual and system theme changes without new network assets.
- **Keep instance data out of assets and preserve password non-echoing.** Discovery remains a suggestion requiring confirmation; manual entity entry remains possible. Do not persist live entity IDs, tokens or user data in mock files or review examples.
- **Preserve the HA-side contract.** Do not rename `sensor.sem_*` entities or confuse their reflected state with a live control guarantee. Their documented temporary disappearance after HA restart should be tolerated; keep read-only add-on options clearly separate from effective UI settings. Evidence: root `README.md:189–212`; `app.js:3375–3389`.

## 8. Open questions

1. **What does this look like inside the owner's HA layout?** Need screenshots/live browser at 360 px and representative height, with HA sidebar/header, browser bars, both themes and 200% zoom. Exact above-the-fold position, sticky boundaries, overflow and one-thumb reach cannot be established from the shim.
2. **How do real uPlot legends, axes and gestures behave?** Verify the two Plan plots' alignment, touch selection, keyboard alternative, unknown gaps, negative tariffs, monochrome series identification and immediate explicit-theme refresh. The artifact's “Chart library unavailable” is the known rendering limitation, not a finding.
3. **What are the source-of-truth contracts for derived UX?** Confirm MAE units per forecast, whether source-required inputs have machine-readable requirements, and whether `config.settings[batteries]` is guaranteed to match `/batteries`. Do not design around the mock's mismatched fleet or treat it as live installation evidence.
4. **What is the intended savings scope?** Confirm the same-period accounting semantics for partial baselines, treatment of wear and advisory/dry-run periods, and whether users most often review today or a month. Keep estimated/counterfactual language until those boundaries are established.
5. **Which visits are most frequent?** The proposed nav assumes frequent Today/Plan/Savings visits and occasional management. Owner task observation could justify keeping Batteries primary; seven permanent peers should require evidence, not habit.
6. **Are all refresh/confirmation paths exercised on supported browsers?** Test first-click mode changes, repeated activation, Settings authority transitions, API errors, leaving/returning with a dirty form, nested dialogs and failure to stop a device. The native-dialog fallback currently resolves a browser confirm without the requested checkbox (`app.js:349–356`); if a supported browser lacks `showModal`, fail closed for checkbox-required actions or supply an equivalent acknowledgement.
7. **What does the host announce to assistive technology?** Check route focus, skip behaviour, repeated `conn-status` updates, overlapping live regions, expanded detail retention and sticky Save occlusion with a screen reader. Structural labels alone do not establish this.
8. **How should forecast notes be classified?** The UI receives an untyped string array (`app.js:1108–1110`). Without severity/category metadata, it cannot reliably hide “ordinary model explanation” while always exposing “material limitation”. First keep all notes reachable with a visible assumptions/limitations summary; a later API change can support safer selective disclosure.

The recommended direction is **less simultaneous presentation, not less evidence**: one answer first, one short qualification next, exact technical and safety evidence always available.
