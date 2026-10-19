# Changelog

## 0.30.1

- **Fix: a stored day from a build without coverage hours is not continued.** 0.30.0 restored the
  in-progress `savings_daily` row at startup, but the row written by 0.29.1 carried the whole day's
  import (5.22 kWh) and cost (2.13 EUR) with no hours, while the new accumulator only knew the
  baseline of the minutes since the restart: `sensor.sem_savings_today` read **-2.03 EUR** for a day
  that was being accounted correctly interval by interval. A row without hour coverage is now left
  alone and today restarts its coverage at the upgrade; the earlier part of that one day reads as
  unmeasured. A restart on 0.30.x itself (the row has hours) still continues the stored day.

## 0.30.0

- **Fix: savings are counted per interval, not thrown away per day.** A single missing grid, load or
  battery poll - or one interval whose price could not be resolved on the baseline side - used to
  poison the whole day: baseline, savings and battery flows became `null` and stayed that way. On a
  live system that meant 6 of 10 measured days had no baseline, the savings sensor read `unknown`
  while the battery was actively controlled, and `year`/`all` summed to nothing because one unmeasured
  day inside a month erased the month. Baseline, actual net cost and flows now accumulate over the
  intervals that could be priced and measured (`savings_daily` migration 6 adds `total_hours`,
  `grid_hours`, `savings_hours`, `battery_hours`), `null` is reserved for "no interval could be
  priced/measured", and the UI says what share of the sampled time each figure covers instead of
  calling the range unknown.
- **Fix: `year`/`all` buckets are calendar months.** They advanced in 30-day steps from 1970-01-01,
  producing 679 buckets with visible holes and skipping February entirely; they are now contiguous
  calendar months (Jan 1970 … the current month).
- **Fix: a bucket keeps its measured part.** A month (or any bucket) with one unmeasured day used to
  erase its measured flows; a bucket now sums the rows that reported a measurement and still marks
  itself not fully measured.
- **Fix: an add-on restart no longer discards today's savings.** The in-progress `savings_daily` row
  is restored from the store at startup, so the next flush adds to it instead of overwriting it with
  the post-start partial totals. See 0.30.1 for the row written before coverage hours existed.
- **UI:** the Savings panel states `Sampled-time coverage: grid X h, battery Y h, baseline Z h of
  W h sampled`, `Savings cover N% of the sampled time`, `Measured over N% of the sampled time`, and
  the per-card scope each total covers; the per-bucket table gains the same coverage line. Legacy
  rows (written before the coverage columns) are reported as legacy periods, never as uncovered time.

## 0.29.1

- **Fix: the Lovelace card rendered mangled.** The downloaded card is one self-contained file, but it
  inlined only the add-on's own two stylesheets, not uPlot's. A card lives in its own shadow root, so
  the SPA's `<link>` to `vendor/uPlot.min.css` did not reach it, and the rules that page lays out with
  - `.u-wrap { position: relative }`, the absolutely positioned under/over canvases and `.u-axis`
  labels - were missing: the plot painted over its legend, the axis text rendered at the browser
  default size and the chart spilled past the card and was clipped. The bundle now inlines
  `vendor/uPlot.min.css` first, and `sem-charts-card.css` restates Bootstrap's `box-sizing:
  border-box`, so a card measures exactly like the SPA.
- Two tests close the class of bug rather than the instance: the assembled bundle's CSS is checked for
  uPlot's layout rules by content (a swapped or trimmed file fails), and every custom property
  `chart-theme.css` reads is checked against the card sheet (a token defined only on the SPA's `:root`
  would silently resolve to nothing).
- **Fix: a replaced card file now reaches the browser.** The `#/cards` installer and the docs register
  the resource as `/local/sem-charts-card.js?v=<add-on version>`, because Home Assistant serves
  `/local` with `max-age=2678400` and a plain URL keeps serving the old file for a month.

## 0.29.0

- **Next actions now shows the plan instead of only describing it.** The panel was a stack of sentences
  about each action window; it is now the horizon's totals, a ribbon that places every window across
  the plan (day ticks, a hatched segment for a stale window, a now marker, a legend naming each
  action), and one card per window with an action tile, a charge gauge and a glyph per fact. The
  figures are unchanged and still stated in words beside every graphic: the glyph, the tint, the
  ribbon and the gauge are additional ways in, never the only way in. The graphic comes from the plan
  the view already has (`planHorizonBounds`, `windowRail`, `socTrack`), so it disappears rather than
  guesses when the payload carries no SoC path or no second slot.
- **The icon layer is inline SVG.** Eleven glyphs (charge, discharge, hold, PV charge, export, self
  consumption, reserve, price, break-even, battery, grid) are drawn in `app.js` on one 24x24 grid,
  stroked with `currentColor`, and tinted through a single `--tint` per action so a card, its tile,
  its gauge and its ribbon segment cannot drift apart. No image, no icon font, nothing fetched; an
  unknown action still gets a bolt and a neutral tint (SPEC 13.1).
- `tools/ui_dom_shim.js` gained `createElementNS`, and `tools/ui_render_check.js` now renders the
  panel and asserts the layer: one tile and one gauge per card, a glyph per fact, a segment per window
  placed inside the track it is on, a legend that names every action present, an aria-label that reads
  the horizon out loud, and the geometry helpers' own edge cases (no slots, a level window, a window
  with no reported floor).

## 0.28.1

- **One cursor-sync key per chart family instead of one per draw.** uPlot's sync registry keeps a hub
object per key for the life of the page and never removes it (the vendored accessor only ever
creates), so minting a key each time the plan timeline was drawn retained an empty hub per redraw —
about one every 20 s on a dashboard whose card redraws whenever the add-on publishes. `charts.js` now
uses a single `SYNC_KEY`, which is also uPlot's own idiom: two plan timelines on one page share a
crosshair, which is what aligned timelines want. `tests/test_wp3_assets.py` gained the first harness
that actually executes `charts.js` (a fake uPlot records what it is constructed with) and pins two
panels per draw, three slots per panel, the 200/175 px panels, the gradient fill and the reused key
across redraws.

## 0.28.0

- **The charts can go on your dashboard now, as a card you install yourself.** The add-on's UI is
behind Supervisor ingress, so nothing in a browser could reach it: a Lovelace card had no data
source, and giving it one by publishing the plan as entity attributes would have put ~14 KB on a
single state. Six compact feeds now carry exactly the series a chart draws — plan timeline, price,
solar, load, savings and accuracy — in a row-major form that stays far below Home Assistant's ~16 KB
attribute limit, each stamped `schema: 1` so a card refuses a shape it does not understand instead of
plotting the wrong column (`feeds.py`, SPEC §12.1). On top of them ships `custom:sem-charts-card`, one
self-contained file from `GET /api/v1/web/sem-charts-card.js`: the card's CSS, the shared chart sheet,
the vendored uPlot, `charts.js` and the card class assembled into a single download. It reads only
those feeds, so it works over HTTPS, inside the mobile app, and even with the add-on stopped. Install
is manual, and deliberately so — save the file to `/config/www/sem-charts-card.js`, register
`/local/sem-charts-card.js` as a JavaScript module, paste the snippet — because the alternative was
taking `map: config:rw`, a permanent write path into the user's configuration, to register it for
them. A new **Cards** view (`#/cards`) is the installer: the download, the three steps, a chart picker
that rewrites the snippet, a copy button, a live preview drawn by the same `charts.js`, and the feed
table (SPEC §12.2).
- **The chart surface became its own sheet, and the charts got a glow-up.** The plot box, legend,
caption and hover readout moved from `style.css` into `chart-theme.css` because two consumers now
share them: this SPA and the card bundle, which inlines the same file into its shadow root (the tokens
come from `:root` here and from `:host` there). On top of that the timeline gained smooth splines,
gradient area fills, a subtle plot tint, day separators, a "now" marker, zero lines on the power
panels, cursor point markers, and a real hover readout with a time and one row per series — created
inside the plot box so it can never push the legend or the caption around. The two plan panels share a
cursor-sync key, so the price/SoC panel and the PV/load panel track the same instant. Colours are still
read from the frozen `--chart-*` tokens for the SPA, and `options.palette` (or `setPalette()`) lets the
card hand over the same values from outside `:root`, where a test pins the card's two palettes to
`charts.js`'s token map and fallback so the copies cannot drift. Nothing here carries meaning by colour
alone: every dashed line, gradient series and bar keeps its text label.
- **The design system replaced its no-decoration rule with a defined look.** SPEC §13.1 used to forbid
gradients, shadows and any font that was not already on the device; the revamp allows all three — as
*tokens*, not as free-form styling. Page, panel, panel-head, accent and danger surfaces take token
gradients (`--grad-*`), elevation comes from `--shadow-1/2/accent`, and a **locally cached** Inter
variable font (`vendor/InterVariable.woff2`, SIL OFL 1.1, checksum in `vendor/README.md`) is declared
by `@font-face` with `font-display: swap` in front of the platform UI stack. Nothing is fetched at
runtime and no image is introduced; contrast is still produced by `--text`/`--bg`, so deleting every
gradient leaves the interface readable. The rules that survived are the ones that carry meaning: one
accent colour, one radius scale, the 8 px spacing scale, tabular figures, keyboard reach and
`prefers-reduced-motion`.

## 0.27.0

- **The optimizer's C core now solves the plan in the deployed add-on, and `/health` says which
backend is live.** The DP over SoC is solved in ~17 ms by a CPython extension built from the same
source as the Python reference (`-ffp-contract=off`, so its arithmetic is bit-identical), and it now
also takes optimizer v2 stage 3's continuation row as an optional seventh argument — the row *is* the
terminal condition, the objective's terminal term is that row interpolated at the reached energy with
no salvage and no second target penalty, and `e_target` still drives the last slot's exact-power
action. Validation grew with it: the row's length and finiteness, entry-for-entry **strict** equality
of the terminal value row, a recomputed objective, and the whole last-slot Bellman row instead of a
sample; a row the core cannot use is declined before the call and the solve falls back to Python with
the accelerator still enabled, while only a row the core received and mishandled disables it. The
accelerator's runtime contract is now stated as what it is, in the core header, the loader, the core
note and the tests: structure, feasibility, one-step decision consistency, the terminal boundary and
sampled backward consistency, with the periodic full differential as a backstop — **self-consistency,
not interior optimality**, and the differential catches divergence after the fact, never
retroactively. The old claim that a wrong core result "can never reach a plan" is withdrawn. Because
the extension is optional and fails open at build and import time, a deployed image could not be asked
whether it was live at all: `/api/v1/health` now carries `optimizer_core` with `backend` (`c` or
`python`), the `build_sha` the loaded extension was compiled from, and a `reason` when the reference
is what solves (not built, a stale build refused at load, or a verification failure).
- **The replay harness ships: arms E1–E4, paired counterfactuals, and a gate that withholds.** A run
replays one recorded window through a real engine on a virtual clock (never starting the engine's
threads), freezes its tolerances before executing, and reports paired per-window deltas with
autocorrelation-aware intervals — but only once three gates pass: plant fidelity, decision fidelity
and accounting fidelity. Windows that exceed a declared plant ceiling, refuse a price, or lack an
as-of vintage have their delta **withheld** rather than published, a failing gate exits non-zero, and
`--help` says exit 0 means no gate failed and not that money was published. An interval needs a
contiguous sample spanning five plan horizons (~6.2 days) before it is evidence; below that the run
labels itself `illustrative_interval` and says the sample is shorter than a plan horizon.
- **A live window can now be captured: append-only as-of history (schema v5).** A replay is only
faithful if the settings and decision-feeding profiles in force at its start are still readable, and
the running add-on re-stamps exactly those rows on every applied command and mode change, so no
multi-day recording could ever be replayed from its start. `settings_history` and
`learning_profiles_history` are written inside the existing choke points, in the same transaction,
appending only when the value actually changes, with deletes recorded as tombstones the readers treat
as deleted-as-of; the current tables keep their exact upsert semantics. The migration seeds existing
rows at their last-write instant, which is the honest limit: a window starting before a decision
key's newest write still refuses, so a usable week-long window generally needs further uptime from
here rather than recovering what was never recorded.
- **Optimizer v2 stage 3, the terminal-value vector — default off and dormant on today's contract.**
The DP used to price the energy left at the horizon with a scalar salvage row, which cannot see what
the next horizon does with the same pack. It can now use a *continuation row* derived from a synthetic
tail instead, so the plan stops dumping energy it will refill an hour later. It ships disabled
(`optimizer.terminal_mode` defaults to `salvage`), every plan reports why it was used or refused, and
with the current sell = buy contract the cost-basis sell floor always binds, so the vector refuses by
design and plans continue to use salvage.

## 0.26.0

- **The plan is solved in C, validated per solve, with the Python reference kept.** The DP over SoC is
pure and deterministic, which makes it portable and makes it the bottleneck: a 120-slot, 30 h solve
costs ~0.4 s in Python, and any policy that compares many futures pays that per candidate. The same
solve now runs as a CPython extension in ~17 ms with the GIL released, and nothing else changes — the
core is a drop-in for one function, every solve is validated against a reference grid, bounds and
action set, a full Python-vs-C differential runs periodically and on demand, and any mismatch
permanently disables the accelerator for the process and re-solves in Python. A core that disagrees is
not a slow core, it is a wrong one, so it is refused rather than trusted.
- **The feed-in side of the contract is priced, and the optimizer can consume it — off by default.**
Sell is not always buy: a feed-in price may exclude VAT, carry redelivery costs, or be subject to
saldering or tiers, and treating `sell = buy` makes exporting stored energy look far better than it
is while making self-consumption of that charge worth far more. `sources/feed_in.py` settles the
modes (`derived`, `explicit`, `buy_minus_fee`, `saldering`, `tiered`) with the tax gate that
distinguishes a *confirmed* zero from an unstated one, and a new kill switch
(`optimizer.feed_in_wiring = off | shadow | on`, default `off`) lets the planning path consume it:
`shadow` solves a compute-only second plan and journals the difference while dispatching the old one,
`on` dispatches the settled plan, and an unpriceable contract refuses the plan (hold, then the
alert) instead of falling back to v1 pricing. With the switch off the schedule is bit-for-bit v1.
- **A recording can be replayed against the engine's own code.** `python -m energy_manager.app.replay`
re-drives the real `Engine` through its step seams on a virtual clock against a scratch copy of a
recorded store, with a simulated or recorded plant, so a policy or tariff change can be measured
against what the house actually did. Arms E1 (fidelity), E2 (tariff model), E3 (terminal inventory)
and E4 (policy) report paired per-window money with three gates — plant fidelity, decision fidelity
and accounting fidelity — whose tolerances are frozen in an episode manifest *before* the run so they
cannot be widened to fit. A window whose price, flow or reference is unknown is withheld (rendered as
absent, never as zero), as is an arm whose gate could not be measured; the bootstrap interval is
labelled illustrative unless it spans a plan horizon. The contract is `docs/REPLAY_HARNESS.md`.
- **Nothing new is on by default.** The accelerator is semantics-identical, the feed-in wiring is
`off`, and the replay harness is a CLI with no API or UI surface, so the deployed behaviour of the
live optimizer is unchanged by this release — which is exactly what makes 0.27.0's policy work
measurable rather than hopeful.

## 0.25.0

- **Named load devices: `load.devices`.** One whole-house profile and one whole-house temperature slope
meant every device landed in that single compromise term. A heat pump responds strongly to temperature,
an EV charger responds to nothing but its schedule, and averaging the two fits neither — the pump's
weather response drags the house slope up and the charger's flat draw flattens it back. Each device now
has its own meter, its own (weekend|weekday, hour) profile and its own temperature term, and the total
forecast is `base + Σ devices`.
- **The subtraction is the whole trick.** The measured total already contains the device, so a base
learned from the total and then added to the device's own profile counts it twice — and that error is
invisible, because a doubled device reads as a *steeper* temperature response rather than as a bug. An
hour therefore enters the base only if the total **and every** subtracted device have a reading there: a
missing reading is unknown, not zero, and calling it zero leaves the device's draw in the base while its
own profile is still added on top. The hours lost that way are counted and reported. A meter covering
fewer than 48 hours, or under half the house's hours, is refused whole rather than modelled from the
handful of hours the two happened to share.
- **The base records what it subtracted** (`devices_subtracted` / `devices_excluded`), and a device
profile is added back only when the current base names its key as subtracted. Re-deriving this from the
current settings instead would let a stale profile be added to a base learned without subtracting it —
and a device the base never removed *should* be inside it.
- **`kind` decides whether there is a temperature term, at serving time too.** Only `heat_pump`,
`water_heater` and `pool_pump` get a slope; an EV charger's draw is a schedule, and a slope fitted to it
would absorb whatever else varies with temperature as if it were the car. Re-configuring a device from
heat pump to EV charger drops its stored slope immediately rather than contradicting the kind until the
next learning run.
- **Devices are reported, not silently absorbed.** Every forecast names each modelled device and each
unmodelled one with the reason recorded when the base was learned ("its meter overlapped 3 of 720
hours"), because a device left inside the base changes what the base *means* and that is invisible in a
kW curve. Cold start models no device: the fallback shape is the whole house and already contains them.
- Config, a Load-card editor with a kind dropdown, and the usual validation. `kind` is a closed set that
the UI choices and the backend share, validation rejects a repeated meter and one pointing at
`load.entity` itself (subtracting the total from itself), and two devices of one kind are told apart by
their entity so a rename keeps the learning history.
- Known limits, named rather than implied away: a mostly-unreadable meter is refused entirely rather than
partly subtracted, the coverage floors are judgement calls rather than settings, each device gets one
slope for the whole week (the base model has the same limitation), the p10/p90 of the sum are the sums
of the component bands, and a removed device's stored profile is left inert rather than collected.
- 1026 tests, audit clean.

## 0.24.0

- **The load and PV forecasts can now learn on an install that measures nothing but grid and battery.**
This began as a question — we know the grid export and we know the night load, so can we not calibrate
against them? — and the blind spot was structural. `load = grid + pv − battery` is the identity that
makes both quantities recoverable, and the add-on already used it, but only in one direction and only
when *both* pv and the battery flow were already mapped. So an install whose inverter reported nothing
had no load labels, no pv labels, and a fine-tune head that could never fit anything while appearing to
be waiting for data.
- **Night-derived load (SPEC §7.2 tier 3b).** Below −6° of sun elevation production is zero for any
array — deeper than the horizon on purpose, because a tilted plane still collects diffuse light at 0° —
so `load = grid − battery` needs no pv series at all. About half of every day qualifies. A missing
location is refused rather than guessed.
- **The night tier cannot learn a day shape, and says so.** The hours it never saw are filled from the
documented default residential shape, and `assumed_daytime_shape` plus a forecast note record it,
because a level measured at night and a shape read from a table are different kinds of knowledge.
That fill has to happen *inside* profile building rather than as a patch on the finished profile: the
unobserved buckets otherwise contribute 0.0 to `profile_mean`, so the level `scale` is computed from a
mostly-empty day and then inflates every hour up to its clamp. The first version of this made a
synthetic 1 kW house forecast **2.0 kW at night** — wrong on exactly the hours it had measured, and
wrong in the direction that reads like a discovery rather than a bug. A second, subtler version
normalised by the default shape's own mean and came out 5.7% high, which `scale` then clawed back by
pushing the measured hours off their level.
- **Inferred values get their own columns** (`actuals.pv_derived_kw`, `actuals.load_derived_kw`,
schema 4). The sampler derives pv as `load − grid + battery` and load itself at night, and writes them
separately rather than into `pv_kw`/`load_kw`: writing them together would have been fewer lines and
would have destroyed, permanently and in the table the fine-tune head learns from, the ability to tell
a measurement from this add-on's own arithmetic. Existing rows keep their NULLs — nothing is
retroactively claimed to have been inferred. The head prefers the measured column and falls back only
when it is empty across the whole window, and reports which it used.
- **Refusals rather than approximations.** Inference stops when a battery is configured but its flow is
unreadable, since the cycling would be attributed to the house or to the roof; the nearest *earlier*
forecast slot is used for a lookup rather than a future one, because a value the add-on had not yet
produced cannot be an observation of the past.
- Still outstanding, and the next piece: **named load devices** (`load.devices[]`), so a heat pump's own
meter can carry its own temperature response instead of being averaged into one house-wide slope. The
design is settled in SPEC §16 — subtract each measured device from the total before learning the base
profile, learn the device's own profile and slope, add it back — and the subtraction is the part that
matters: adding a device on top of a base that already contains it double-counts it.

## 0.23.1

- **`year` and `all` savings no longer stay `--`.** 0.22.1 treated a bucket that mixed rows with and
  without a baseline as wholly unknown, which left the year/all ranges permanently unknown: their
  month buckets always contain the pre-fix NULL rows. A bucket now sums only the rows that could be
  re-priced, per row, so an unknown row's cost is never subtracted from a known baseline, and the
  UI's comparable actual net cost is `baseline − savings` so it stays exact for those buckets.

## 0.23.0

- **The learned PV model is now fitted on the irradiance chain it is actually served, and it is given
the weather.** This began as a question — are cloud cover, temperature and rain being taken into
account? — and the audit found something worse than a missing feature.
- **The defect: a train/serve mismatch.** Training drove the physics with *satellite-observed*
horizontal irradiance; at runtime the add-on reconstructs irradiance as clear-sky Haurwitz attenuated
by a forecast cloud percentage. Those are different estimators of the same quantity, so the learned
residual was spending itself correcting an irradiance model that is never used in service. Scored on
the chain it would actually see, the 0.22.0 artifact falls from 115.8 W to 150.5 W and picks up an
~8 % energy bias — meaning the accuracy validated offline was not the accuracy in service.
- **The fix.** The training corpus is now built from the serving chain, and the model is handed the
ingredients of that chain rather than only its result: cloud cover, the Kasten attenuation, clear-sky
potential, wind, humidity and rain (22 → 28 features). Wind matters physically because the NOCT module
model had no convective cooling term at all; cloud now reaches the model as a number it can learn a
mapping from, instead of being pre-mixed by a hand-tuned formula it could never correct. Every added
feature is a field a Home Assistant weather forecast publishes — a feature the add-on cannot observe
at serving time would quietly make the forecast worse.
- **Result.** Held-out *systems* 5.102 % → **4.742 %** of nameplate; held-out *years* 5.548 % →
**5.620 %** (a tie within the difference between corpora, and it now holds on both splits); energy bias
+9.5 % for the physics prior → **+0.09 %**; aggregate specific-yield gate −0.59 %. Parity against
scikit-learn stays exact at 0.000e+00.
- **What was verified rather than assumed** about the inputs that were already right: cloud cover, air
and module temperature, panel tilt/azimuth/capacity and the inverter limit were all genuinely in the
model, and PVGIS `G(i)` really is horizontal for this corpus — its clearness index never exceeds 0.80,
where plane-of-array data misread as horizontal would exceed 1.0. Corpus timestamps were checked by lag
correlation (lag 0 wins at r = 0.879) instead of trusted. The three factors that are still *not*
modelled — snow, soiling and horizon shading — are named in SPEC §7.1.4 rather than implied away.
- **Three real bugs were caught by the golden-vector cross-check, not by a metric.** It found the
clear-sky and irradiance features being built per-system on one path and at the corpus centroid on the
other (4.3 and 4.0 W/m²) — the same defect in miniature, and the kind that ships as "the forecast is a
bit off". It also caught the training pipeline still reading a stale `ghi` series for one feature after
that change. Its tolerance is now a documented 1e-4: four orders below the smallest error it has
caught, four orders above float32 round-trip noise on a float32-stored corpus.
- **Unit safety.** Wind and precipitation are converted from the weather entity's own units; a km/h
value read as m/s is a 3.6x error that still looks like a plausible number. An unrecognised unit
returns "unknown" rather than the raw value.
- The weather series must be supplied to `ingest.py` (`--weather`) and is required rather than optional,
because omitting it would silently restore the mismatch this release exists to remove.

## 0.22.1

- **The Savings view works on an install with no load or PV sensor.** The baseline was only ever
  computed from a *measured* `load_w`, so a meter + battery setup with no load/PV entity stored
  `baseline_cost_eur = NULL` for every interval and the whole section reported `--` forever. The
  do-nothing baseline now falls back to the grid identity `g0 = grid − battery` (exactly `load − pv`,
  SPEC §8.1) when there is no load/PV reading; metered `load − pv` still wins when both exist. No
  forecast is used — the baseline prices the same realised kWh. A missing buy/sell price on the side
  the baseline lands on, or a grid gap, now makes the baseline unknown rather than pricing it at 0.
- **Empty periods are no longer counted as covered.** `savings_from_daily` left buckets with no
  stored rows at their default `baseline_cost_eur = 0`, so a month with 4 days of history claimed
  "Baseline available for 25 of 30 periods" and compared 0.00 EUR with 0.00 EUR. Such buckets now
  report `null` baseline/savings plus an additive `has_data` flag, a bucket that mixes known and
  unknown rows keeps only the known rows (summed per row), and totals add
  `measured_periods`/`baseline_periods`. The
  UI counts coverage from `has_data` and shows `--` (not `0.00`) for periods with no activity.
- **`sensor.sem_savings_today` no longer fabricates a zero.** A null saving published `0.0` EUR and
  `month_eur`/`year_eur` did the same; it now publishes `unknown` (and `null` attributes). Its
  `baseline_kwh` attribute also read a `battery_cycles` key the totals never had and silently
  published `import − export` instead of the do-nothing net grid energy; it is now
  `import − export − charged + discharged`, or `null` when the baseline is unknown.
- Additive API fields: a savings bucket gains `has_data`; totals gain `measured_periods` and
  `baseline_periods`. Rows written before this release keep their stored NULL baseline (no backfill),
  so older history stays `--` and only new ticks get a baseline.

## 0.22.0

- **A learned PV model, trained offline and served from the committed artifact** (`solar.source =
  learned`, SPEC §7.1.4). It was fitted on the Utrecht 175 corpus — 174 residential arrays, four
  years, 1.86 M hourly daylight rows — and predicts a *ratio* against its own physical reference
  rather than a power in watts, which is what lets one artifact serve a roof it has never seen. That
  is the cold-start prior a fresh install otherwise has no way to build: the existing sources each
  need either an API key, a watt peak the user measured, or weeks of the meter's export moments.
- Validated on held-out **systems** (5.102 % of nameplate MAE, 5 folds) and on held-out **years**
  (5.548 %, trained on 2014 and scored on 2015-2017) — the second being the one that matters for a
  base model, because a system split cannot tell a model that generalises from one that memorised a
  year of weather. An aggregate specific-yield gate agrees to +0.15 %, and the model reproduces the
  spread across arrays, not just the median. Both error figures travel in the artifact and are
  reported in the provider status, so the number shown is the out-of-sample one.
- The reference physics is deliberately **not** the capacity tier's: Erbs diffuse split, a NOCT module
  temperature and explicit AC clipping instead of a constant 25 % diffuse fraction and one
  `performance_ratio`. The residual was fitted against that reference, so multiplying an estimate
  derived from the other would be wrong by the difference — which is why `solar.performance_ratio` is
  not applied when this source is active, and the forecast says so. New setting `solar.ac_capacity_wp`
  (the inverter's AC limit; unset = assumed equal to the watt peak, stated as an assumption).
- **Served by standard-library arithmetic only** (R3), with the port pinned by a golden-vector test
  emitted from the training pipeline itself whose `--cross-check` proves those vectors are the
  arithmetic that fitted the model. A transcription error would otherwise be invisible: the forecast
  would simply be a little worse. Weights are stored as IEEE-754 bit patterns in integers, because a
  decimal round-trip flips comparisons on split thresholds and dense decimal arrays are what R1's
  audit correctly reads as coordinates.
- **A forecast archive** (SPEC §7.4): what each provider predicted for each future slot, upserted per
  `(kind, source, slot)` keeping the newest issue made *before* the slot. Appending would store
  thousands of rows a day for one number per slot that matters, and rewriting an elapsed slot from a
  later issue would let a provider quietly improve its own record. Skill is compared on hourly means
  of both sides, and a kind with no measured series raises rather than reporting a perfect score.
- **A nightly fine-tune head** (SPEC §7.5): per-hour-of-day factors from this house's own
  forecast-versus-measured record, in closed form (`m = (Σf·a + λ)/(Σf² + λ)` — ridge against a prior
  of exactly 1.0, so no data means the base model rather than a division by zero, and a nightly refit
  still fits inside R3). A fit is only a candidate: it is adopted only when it beats whatever is in
  force on the newest 3 days by more than 2 % of that error, and the previous profile is kept so
  `revert` undoes it in one write. It **replaces** the hourly EWMA rather than stacking on it — two
  corrections fitted to the same signal would count one systematic error twice, and it would look
  like an ordinary cloudy week. Fewer than 36 scored slots across 10 hours of the day is refused with
  a reason, which is the state every install starts in.
- **Two bugs fixed rather than shipped.** The training pipeline's day-of-year was
  `days_since_epoch % 365 + 1`: offset by 11-13 days and drifting a day per year as leap days
  accumulated, so the seasonal feature meant something different in every training year. Fixing it
  improved everything, including the physics prior (176.1 → 163.4 W); retraining was cheaper than
  cementing it into an artifact. And the settings view called `hasOwnProperty` with an absent choice's
  value as the receiver, so any backend choice value the UI's label table had not yet heard of blanked
  the whole settings page — now a cosmetic fallback to the raw value.
- The clear-sky forecast notes no longer claim a hard-coded "uncertainty 35 %". The real band is 30 %
  wherever the NASA POWER clearness profile applies, and prose that contradicts the `uncertainty_pct`
  field beside it is worse than prose that omits it. `SCHEMA_VERSION` 2 → 3.

## 0.21.1

- **A hold can no longer be "confirmed" by a 0 W flow while the inverter is still armed.** On a
`device_limits` path the mapping has no setpoint entity, so `_idempotent_power` -- the pre-write
"already there" check -- fell back to the *measured* AC pair. When the pack is full, or simply idle,
that flow reads 0 W, so a hold target of 0 W was skipped as `idempotent_skip` while `input_limit`
was still armed. Live on 2026-10-14 the arbitrage charge's down-ramp ended with `input_limit = 153 W`
and `ac_mode = input`, the plan then held from 04:00Z, every 30 s tick was skipped for six hours, and
only the next process start -- which holds unconditionally -- neutralised the inverter at 05:50:56Z.
The measured flow stayed 0 W throughout (no energy was bought), but the pack was set to charge from
the grid at whatever the price was, up to 0.4378 EUR/kWh: a stale setpoint that the dispatcher
believed it had already cleared.
- The device path now follows the rule its `commanded_power()` already followed: the "already there"
  report is the `input_limit`/`output_limit` echo, never the measured flow, and an unreadable echo
  fails **open** (the write happens, never the skip). The same rule applies to the `zendure_native`
  driver, and the `zendure_mqtt` bridge now skips when its limit echo already matches the target
  instead of re-publishing the magnitudes on every tick.
- Four tests pin it: the live shape (limits armed at 153 W, measured pair 0/0, target 0 W) must write
  rather than skip; an unreadable echo must fail open on both the entity and the native driver; and
  the MQTT bridge writes nothing when the echo matches. All four fail on the 0.21.0 tree with
  `idempotent_skip`.

## 0.21.0

- **The sell floor prices only the energy above the reserve.** `optimizer.reserve_soc_pct` is a
per-battery floor above `min_soc_pct` that the plan never discharges below (SPEC 3), and the
allocator already measures discharge room from `max(min_soc, reserve)`. The cost-basis ledger did
not: it measured the held energy from `min_soc_pct` up, so the unsellable reserve band was weighted
into the per-battery basis and into the energy-weighted fleet floor the DP enforces -- a stale or
dear reserve could refuse sales of cheap energy held above it. The ledger's floor level is now
`floor_soc_pct = max(battery.min_soc_pct, optimizer.reserve_soc_pct)`, used by both the reset
(`basis = None, E = 0` when nothing sellable is held) and the SoC reconciliation
(`E_phys = capacity_kwh x (soc - floor_soc_pct) / 100 / eta_charge`). With the default reserve of 0
the level is `min_soc_pct` and the ledger is unchanged; a pack sitting at its reserve holds nothing
sellable, so its basis is cleared and the floor is inactive until energy above the reserve is
charged again.
- Two tests pin it: a 30 % reserve prices the 0.8 kWh above it where the same measured window prices
  1.8 kWh from `min_soc_pct`, and the reserve reaches the ledger through `Engine.update_cost_bases`.
  `docs/REFERENCE.md` (ledger narrative and the Reserve SoC row), the Reserve SoC setting help and
  SPEC §5 carry the rule.

## 0.20.6

- **A live safety condition no longer strands the Dashboard.** The answer panel's **Safety state**
banner (`/status.pending_holds` non-empty, or `mode_persisted: false`) was built by
`renderSafetyState`, which appended with `body.appendChild(box)` on the first argument it was given.
That argument is `renderAutopilot`'s `draw` callback, not a DOM node, so as soon as a safety line was
live the paint threw `TypeError: body.appendChild is not a function` right after the
authority/measured/planned lines. The throw aborted the rest of the `/status` success handler: the
answer stopped after its first three lines, and **Warnings and activity**, **Manual override**,
**Right now**, **Battery energy price**, **Supporting measurements** and **Batteries** never painted
and stayed on "Loading…" — the 15 s poll re-threw at the same place, so it never recovered. The
price card looked wrong in a telling way: it still held the "the server did not report a cost-basis
ledger" text painted by the earlier `/plan` handler while `data.status` was still null, so a healthy
ledger read as missing. `renderSafetyState` now appends through the same callback as every other
answer block. The render check renders the live shape (a pending hold for one battery) and asserts
the banner shows *and* every later panel still painted; on the frozen 0.20.5 tree that section aborts
with the identical `TypeError`.

## 0.20.5

- **A stall now means stale measurements, not an empty booking window.** The 0.20.3 detector
derived `stalled` from the coverage of `(last_ts, newest sample)`, which is empty by construction
whenever the ledger is already settled up to the newest sample: every update landing inside the
current 5-minute bin wrote `stale: true` / `no_samples_since` and stepped `skipped_no_samples` on a
perfectly healthy ledger (live on 2026-10-13T12:23:02Z it advanced 368 -> 371 intervals and
reported stalled *in the same update*, and at 12:28:11Z the same ledger read healthy again), which
the 0.20.4 Dashboard card rendered as "Stale reading".  A stall is now a freshness failure: the
newest usable power sample must be older than `STALL_TOLERANCE` (15 min = three sampling buckets,
one whole bucket above the 10-minute booking-gap tolerance) or missing entirely.  The 0.20.3
counter, marker, single warning and `sample_source` semantics are unchanged, and the single-battery
`actuals` fallback still fires only when the window holds no usable power point at all -- a healthy
ledger updating twice inside one sample bin books nothing extra and keeps `sample_source:
measurements`.
- Documentation: SPEC 5 amendment, `docs/REFERENCE.md` (`stale` / `no_samples_since` /
  `skipped_no_samples` semantics), `docs/VERIFICATION.md` §8.20 and the `docs/UI.md` card note.

## 0.20.4

- **The Dashboard shows what the energy in the pack cost and what a sale has to earn.** Both
  figures already existed in `GET /api/v1/status.cost_basis` — the ledger's energy-weighted
  `fleet_basis_eur_per_kwh` (the average charged price) and its `sell_floor_eur_per_kwh` (the
  break-even price the optimiser acts on) — but no view rendered them. One compact **Battery energy
  price** card now shows both in EUR/kWh, breaks the floor down into `basis ÷ round trip + wear +
  margin` (the plan's own round trip and wear, the configured margin; a term the payload does not
  carry is omitted rather than printed as a fake zero), and adds the stored energy (`energy_kwh`),
  its value (`stored_value_eur`) and the ledger's `updated_at`. Several enabled batteries add one
  row each, with the same figures and freshness. Display-only: no optimizer, ledger, dispatcher or
  sampler behaviour changes.
- **A stalled ledger is visible on the card; an empty one says so.** When 0.20.3's stall marker is
  set (`stale` / `no_samples_since`) the card marks both readings and shows a warning naming the
  battery and the moment its samples stopped, so a frozen floor cannot look healthy. A ledger with
  no basis yet, a pack that is empty, or `optimizer.sell_floor_enabled = false` each render `--`
  with the reason instead of a stale figure. The card reads `/status` and the already-fetched plan
  only; it never adds a fetch.
- Documentation: `docs/UI.md` and `docs/UI_REVIEW.md` now call the tab **Dashboard** (its label
  since 0.20.0; both still said "Today"), and `energy_manager/DOCS.md` documents the 0.20.0 manual
  override / limits panel plus the new price card.

## 0.20.3

- **The measurement sampler now reads the driver's own role vocabulary.** The sampler built its
  per-battery role map from a private list of mapping keys, so a Zendure battery whose stored
  mapping names the native measured pair (`grid_input_power_entity` / `output_power_entity`) and
  `pv_power_entity` / `soc_entity` persisted no `battery_charge` / `battery_discharge` /
  `battery_power` rows at all: the cost-basis ledger integrates that series, so it silently froze
  (18 h, 282 intervals, an unchanged 0.3197 basis and sell floor while the pack charged 0.2 kWh AC
  at 0.266), and the capacity learner lost its samples too. The measured-role table now lives on
  the driver classes (`drivers.measured_roles`, generic keys by default, overridden by the Zendure
  driver with its native pair and requiring both sides like `_power_is_measured`), and
  `sources/measure.role_entities` resolves it through the driver registry. The generic keys win
  when both vocabularies are present, the registry-resolved mapping the engine passes in counts
  (so a role the runtime resolver filled in is sampled), and an unmapped battery resolves nothing.
  `power_setpoint_entity` keeps its meaning: a signed setpoint is a command, never a measured
  energy row.
- **A stalled ledger is visible instead of silently frozen.** With no usable power samples in the
  window, `book_slices` now counts `skipped_no_samples`, sets `no_samples_since` and `stale` in the
  stored payload (they survive a restart), and logs one warning per stall; booking again clears the
  marker. `/status.cost_basis` exposes the counter, the marker and the `sample_source`, and
  `sensor.sem_battery_cost_basis` carries the per-battery `stale` / `no_samples_since` plus a fleet
  `stale` flag.
- **Measured energy that was never booked is recovered (single-battery installs).** When the
  per-battery measurement series holds no usable power point in the window and exactly one battery
  is enabled, `update_basis` falls back to the 5-minute fleet-aggregate `actuals` rows
  (`battery_kw`/`grid_kw`/`soc_kwh`, capped at the newest row); with one battery that aggregate is
  the same physical measurement, so the frozen window books with the same measured integral, price
  attribution and SoC reconciliation. With two or more enabled batteries the aggregate cannot be
  split without guessing, so no fallback happens and the stall is reported instead.

## 0.20.2

- **A plan reason now names the constraint that really bound the power.** The 0.19.0 sell floor
  sets `grid_lo = 0` in a PV-surplus slot, so `power_bounds_kw` reported `binding = True` there and
  `_classify` credited any power sitting on *a* bound to the grid: the live 12:00Z slot
  (2026-10-13, the cheapest hour: +990.3 W filling the pack to its 100 % SoC ceiling) was labelled
  `charge / grid_limit_hold` although the power sat on `soc_hi`, not on any grid limit. The window
  computation is factored into `soc_window_kw` and `grid_window_kw`, which `power_bounds_kw` still
  intersects with the same public `(lo, hi, binding)` result and the same `binding` semantics; the
  classifier now sees both windows and uses a grid reason only on a side where the grid bound is
  tighter than the SoC bound and the power really sits on it. The same conflation in the discharge
  and hold branches goes away (a discharge parked on the SoC floor reports its discharge reason, a
  hold on a zero grid bound that merely coincides with the SoC window reports the SoC reason), and
  genuine grid cases (an import-capped charge, an export cap holding the power at zero) keep
  `grid_limit_hold`. No reason slug is added. The schedule is untouched: the powers, SoC
  trajectory, energies and money are byte-identical to 0.20.1; only the action/reason strings
  change.

## 0.20.1

- **The plan now models the current (partial) slot with the time it really has.** A plan made at
  05:16Z floors its horizon to the 05:00 slot, but the solver used a full hour for every slot, so
  the 05:00 hour really covered only 05:16–06:00: the live plan sold 461.2 W in it, the dispatcher
  abandoned the slot at the clock boundary, and the pack stopped at 37 % at 06:58Z instead of
  reaching the planned 15 % at 07:00 — energy above the floor was left unsold at peak, and every
  later slot's price was applied to a window shifted by the elapsed part. `PlanSlot` now carries an
  additive `minutes` (full slot length for every slot but the first, which is the remaining part of
  the current clock slot, minimum 1 minute), and the DP transitions, power bounds, slot cost, SoC
  trajectory and summary money all use it. `solve` takes an optional `now` (absent = the previous
  uniform model, unchanged), and the execution view, `/plan`, `/plan/execution`, `/status` and the
  web action windows read the same field, so the schedule, its energy and its money can no longer
  drift from the clock. Slot timestamps and the API grid are unchanged.

## 0.20.0

- **The "Today" tab is now "Dashboard".** The primary nav, the HA-connection tooltip and the
  Settings safety summary say Dashboard; the hash route `#/dashboard` and the internal area key are
  unchanged, so existing links and deep links keep working. No other tab label changed.
- **A Manual override panel on the Dashboard: a bounded force plus per-direction limits.** The force
  section pins one battery or the whole fleet to Charge / Discharge / Hold at a chosen wattage for
  5–120 minutes through the existing `POST /override` / `DELETE /override` (signed power, unchanged
  API); an active override is shown with its direction, power, expiry and scope, and Clear confirms
  first. The limits section sets `Max charge` / `Max discharge` per battery and for an `All
  batteries` row (`POST /limits`, `DELETE /limits?battery_id=…`), showing the current value, its
  source (this battery vs all batteries) or `unlimited`, with the configured hardware maximum as the
  field's range. A limit can only lower a battery's configured maximum: a value above it is refused
  with 400 `invalid_body` naming the maximum, and the resolver clamps a hand-edited or stale cap as
  well. Limits are applied to the **effective** config (`max = min(configured, cap)`), so the
  planner's bounds, the safety aggregate, the per-battery allocation, the dispatcher clamp and
  `/status` all agree without a second enforcement path — the stored battery config and the battery
  PATCH path are untouched. A limit therefore constrains the automatic plan *and* a manual force,
  and never bypasses the cell-voltage protection, the SOC floor/ceiling, the reserve or the
  full-charge state machine. Limits persist like `mode`/`dry_run` (the settings store's
  `manual_limits` key, deliberately not a settings-form field) and are journaled (`limits_set`,
  `limits_cleared`). `/status` gains `manual_limits` and, per battery row, the configured
  `max_charge_w`/`max_discharge_w` plus the resolved `cap_charge_w`/`cap_discharge_w` (`null` =
  unlimited); the panel states when dry run or a non-auto mode means nothing is written.

## 0.19.0

- **The sell floor restricts discharge; it never forces a charge.** The 0.18.0 floor branch used
  `grid_lo = max(grid_lo, -base)`, so in a PV-surplus slot (`base = load - pv < 0`) the lower bound
  became *positive* and the pack had to swallow the whole surplus at any price: a live 30 h plan
  charged 08:00-10:00 (sell 0.34 / 0.30 / 0.28) instead of the cheaper 11:00-13:00 hours
  (0.27 / 0.26 / 0.27), about 6 ct worse over that window. The bound is now
  `max(grid_lo, -max(0.0, base))`: a deficit caps the discharge at the house's own demand, a surplus
  leaves `p >= 0` while the PV exports freely. The floor is about *stored* energy; PV that would go
  straight to the meter has no cost basis. `_sell_floor_blocks` and the dispatcher clamp already
  judged discharges only and are unchanged.
- **You choose how PV is priced: `optimizer.pv_charge_preference`.** `auto` (default) prices PV
  energy charged into the pack at the slot's sell price (the export forgone — PV then wins exactly
  when `sell < buy`), `prefer_pv` at 0 (absorb the surplus before buying anything) and `avoid_pv` at
  the slot's buy price (export the PV and charge from the grid in the cheapest hours). The chosen
  price is an internal shadow price added to the objective for the PV part of a charge; the plan
  summary keeps reporting the true grid cost of the returned schedule, so no mode can invent a
  saving. `allow_grid_charge=false` and `min_spread` keep their existing scopes.

## 0.18.1

- **The measured energy is booked on the sample grid.** On a live install whose applied setpoint
  changes every ~2 minutes while the sampler reports every 5 minutes, the 0.18.0 ledger booked each
  update's own tail flat at the last visible sample, so the ramp the next sample revealed was never
  recovered: the reported pack content ran ~12 % behind the meter (250 command intervals, 142 with
  no power booked). Energy now follows the samples and price follows the commands: the covered
  sample spans are integrated between consecutive samples and split at the command changes and
  price-slot boundaries they cross, each part is attributed to the command in force, and a command
  whose whole span falls between two samples still books its measured share. `last_ts` is the end of
  the last processed sample span (never a command timestamp) and nothing beyond the newest settled
  sample is booked, so an unanswered tail waits for its next sample instead of being extrapolated
  flat and a replay cannot double-book.
- **The quantity is reconciled with the pack.** After booking, the accounted energy is compared
  with the pack's own measured SoC content (`capacity_kwh * (soc - min_soc_pct) / 100 / eta_charge`,
  clamped to the pack) and the difference is applied to the quantity **without touching the basis**,
  kept as the signed cumulative `reconciled_kwh` plus `reconciled_last_kwh`. A stale or missing SoC
  sample skips the step (`skipped_no_soc`), and a single correction larger than half the pack
  capacity resets the ledger (`reconciled_resets`) rather than silently inventing an adjustment.
  The new counters ride along in every `/status` `cost_basis` battery row.

## 0.18.0

- **The energy in the pack is valued at what it really cost.** A per-battery cost-basis ledger
  (`app/cost_basis.py`, runtime data in the store profile, never settings) derives the weighted
  average price of the AC energy currently held from **applied** dispatch changes and the *measured*
  battery power between them: a slice opens on an applied `auto` change and closes at the next one
  (or at the update), one dispatch row books at most one slice, and the slice end is stored so a
  replay books nothing twice. Each slice is priced at the plan slot it was charged in — grid energy
  at the slot's buy price, PV energy at the export price it forgoes — splitting only at price-slot
  boundaries, falling back to buy when sell is missing and skipping (never guessing) when neither
  resolves. A discharge reduces the accounted energy by the round trip, the SoC floor resets the
  ledger, and a bounded journal keeps the provenance.
- **A sale below that value is refused by default.** `optimizer.sell_floor_enabled` (default true)
  and `optimizer.sell_floor_margin_eur_per_kwh` (0..1, default 0) turn the ledger into a per-battery
  floor through the existing break-even helper, with the fleet's energy-weighted mean enforced by the
  plan (`sell_floor` reason) and by a dispatch clamp on a live planned discharge. Charging, SoC
  holds, overrides and everything outside `auto` are untouched, and with no ledger yet the behaviour
  is exactly as before. `/status` gains the per-battery basis and stored value plus a fleet block,
  the plan summary carries the floor, and two new entities (`sensor.sem_battery_cost_basis`,
  `sensor.sem_battery_value`) mirror it into Home Assistant.

## 0.17.1

- **A running plan keeps its setpoints.** The recovery guard treated this run's own live command as
  "a previous run died with a live setpoint": in `auto` it wrote 0 W over the plan every
  `RECOVERY_RETRY_S` (about 60 s), alerted `live_setpoint_recovered` on every retry and cut a
  sustained command to roughly two thirds duty (live test 2026-10-12). The latch is now split into a
  per-battery applied-command record for what *this* run wrote and a boot latch restored from the
  store for what a *previous* process left behind. Only the boot latch is guard-recoverable, and a
  verified hold clears it, so a real recovery holds once, alerts once and then stays silent.
- **Only a real, verified non-zero write latches.** A blocked `advisory`/`off` tick used to journal
  `requested_w` and latch from it, which let the guard write `input_limit=0` while the add-on was in
  advisory. A result now latches only when it is not `dry_run`, is `ok`, really wrote, and applied a
  non-zero setpoint — so `advisory`/`off`/`dry_run` runs issue zero write calls of any kind, guard
  holds included. The only writes outside `auto` remain the documented de-escalations (mode or
  `dry_run` change, Stop now, exit) and neutralising a genuine previous-run latch at boot.
- **The latch follows what the add-on commanded, not an optional setpoint entity.** On
  `control_path: device` there is no setpoint entity, so any momentary 0 W flow used to clear the
  record while `input_limit` still executed — an `auto → advisory` change then logged
  `hold_skipped — nothing to neutralise` and left the pack charging. The record now clears only on
  verified neutral: a hold the driver confirmed in its own read-back window, or two consecutive
  readings where the driver's own setpoint and the measured flow both read ~0. The Zendure driver
  reports the `input_limit`/`output_limit` read-back as its `commanded_power()` on the device path.
- **De-escalation touches only what we wrote.** `auto → advisory/off`, `dry_run` on, Stop now and
  exit neutralise every battery this run really commanded and has not verified neutral, and nothing
  else: a fresh advisory install, or a run that never wrote, is not touched at all. The `/stop`
  response shape and per-battery results are unchanged, and `pending_holds` now means "commanded and
  not yet verified neutral".

## 0.17.0

- **A scheduled full charge keeps the pack balanced.** New `maintenance.full_charge_enabled`
  (off by default) asks the plan to reach `maintenance.full_charge_target_soc_pct` (default 100)
  every `maintenance.full_charge_interval_days` (default 30) days so the BMS can balance its cells.
  While a tracked battery is due, the optimiser reuses the existing terminal-target mechanism —
  keeping the higher of the maintenance target and `optimizer.target_soc_pct_end` — so it charges
  in the cheapest slots before the deadline instead of force-charging at peak. Charging slots keep
  the stable `terminal_target` reason, and the plan carries an additive `maintenance` block
  (`enabled`, `active`, `due`, `target_soc_pct`, the affected `batteries`) that the Plan view marks.
- **Completion is measured, never faked.** The per-battery runtime state (`last_completed`,
  `next_due`, `first_seen`, `overdue_alerted_at`) lives in the store profile, not in user settings.
  A measured SoC at or above the target minus one point records the completion and moves the
  deadline a whole interval out; the due-date arithmetic keeps the local wall-clock time across
  DST and month boundaries. A deadline past `due + 24 h` raises the rate-limited
  `full_charge_overdue` alert and the cycle keeps trying; a missing or stale reading changes
  nothing. A battery with no history is due immediately, and one already at target completes on the
  first check.
- **Disabled or off changes nothing.** With the feature off no target and no profile record are
  written; `off` plans nothing at all, and advisory still plans the charge without writing to
  hardware (the normal dispatch safety pipeline is untouched). `GET /api/v1/batteries` gains a
  per-battery `maintenance` state, `GET /api/v1/status` and `GET /api/v1/plan` gain the plan-level
  block, and the Batteries card shows `due in N days` / `due now` / `overdue` / `last completed` /
  `off`.

## 0.16.0

- **A critically low cell now charges the pack out of danger instead of letting it discharge.**
  With `protection.cell_voltage_enabled` on (off by default) the add-on watches the **lowest cell
  voltage** — a mapped `cell_voltage_entity` (read like every other measurement, with the same
  freshness rules), or the `zendure_native` driver's own `read_cell_voltage()` hook when no entity
  is mapped. Below `protection.cell_voltage_cutoff_v` the battery is **latched**: discharge is
  blocked and it is charged at `min(max_charge_w, protection.emergency_charge_power_w or
  max_charge_w)` regardless of price or plan, still respecting max SoC and the per-tick ramp.
  Between the cutoff and `protection.cell_voltage_resume_v` it is blocked from discharging only; at
  or above the resume voltage the latch clears and normal planning returns. The two stable reasons
  `cell_voltage_guard` and `cell_voltage_emergency_charge` appear in the dispatch log and as
  rate-limited alerts. `protection.cell_voltage_warning_v` is the advisory threshold inside the
  band and must sit above the cutoff (as must the resume voltage), validated at save time.
- **The guard fails safe and survives a restart.** A stale or unreadable voltage keeps the previous
  latch (and its emergency charge) and raises a `cell_voltage_stale` note; a battery whose voltage
  was never seen stays inactive, and a zero or missing reading is never treated as a voltage. The
  per-battery latch is stored in the runtime profile and restored on boot, so a restart cannot
  silently resume discharging a pack that was charging out of a low cell.
- **Advisory and dry-run still see it, without a write.** The guard is evaluated before the
  authority gate, so advisory/off and dry-run log the reason and raise the alert but issue no
  service call; `auto` with dry run off writes the emergency charge for real. The status API gains
  a `cell_voltage` block (thresholds, latch, per-battery readings) and each battery a
  `cell_voltage_v`, and the Batteries card states the voltage and marks a latched battery.
- **The guard is a pure, ordered clause of `control.safety.check`** (SPEC 9.3 clause 4b) and is
  unit-tested without hardware, alongside the existing SoC/power/grid/ramp clauses whose order and
  semantics are unchanged.

## 0.15.0

- **Zendure hardware can now be driven directly, with no `zendure_ha` integration.** The new
  `zendure_native` battery driver speaks the device's own local zenSDK HTTP API with the standard
  library alone (`urllib`/`ssl`; `requirements.txt` stays empty): no integration, no MQTT broker and
  no cloud account. Add the battery by hand in the wizard — `host` and `serial` are required, with
  optional `scheme`/`port`/`api_path`/`token`/`timeout_s`/`insecure_tls` — and enable the device's
  local API first (in the Zendure app, add HEMS and exit to apply).
- **Reads and writes follow the documented device contract** (`docs/EXTERNAL_APIS.md` section 6,
  from the vendor zenSDK docs and the reference implementation): `GET /properties/report` supplies
  SoC, the measured AC power pair, PV, the device's own limits and per-pack voltages; the write is
  `POST /properties/write` with `{sn, properties: {acMode, inputLimit, outputLimit, smartMode}}`
  using the property sign convention (magnitudes plus direction). Writes are clamped to the
  configured limit **and** the device's reported maximum, and every write is read back against the
  device's own limit echo within 5 W — a mismatch or timeout is a driver error, so the existing
  failsafe hold path decides what happens next. `read_cell_voltage()` exposes the lowest pack cell
  voltage (0.01 V) for a low-voltage guard, and the device's pack count/temperature/limits are
  reported as diagnostics. The device reports no capacity in kWh, so `capacity_kwh` stays the
  figure you entered — never a number derived from voltages.
- **Saving a `zendure_native` battery proves the connection.** The add-on reads the device once
  (bounded to 5 s) before storing: an unreachable host, a report with no properties (local API
  disabled) or a serial that does not match the device is refused at save time with the reason
  named. `allow_unreachable` in the mapping is the explicit opt-out; the battery card then shows
  the read error instead. A configured `token` is sent as `Authorization: Bearer`, stored, masked in
  every API/UI read-back (`***`, and a PATCH that omits or echoes the mask keeps the stored value)
  and never logged.
- **The local MQTT path is documented as planned work, not stubbed.** The device is an MQTT
  *client* that must be pointed at a broker (`POST /rpc`, `HA.Mqtt.SetConfig`) and publishes on
  `iot/<productKey>/<deviceId>/properties/{report,read,write}`; a stdlib-only broker/client within
  this task was not achievable, so the exact remaining work is recorded in the docs and no
  untested code ships.
- **Verified against an in-process fake device, not the hardware.** `tests/test_wp5_zendure_native.py`
  covers read/write round trips, charge-vs-discharge sign mapping, device clamping, read-back
  mismatch, HTTP errors, malformed JSON, timeouts, holds, cell-voltage reads, connection validation
  and the save-time probe; live firmware behaviour (per-model maxima, HTTPS certificates, the
  undocumented 401/token path) still needs validation on a real device and is stated as assumed in
  `docs/EXTERNAL_APIS.md` section 6.

## 0.14.0

- **Each pack's real usable capacity can now be learned from its own cycles.** A battery gains a
  `capacity_source` of `manual` (the shipped default) or `learned`. In `learned` mode the capacity
  you typed becomes the seed/fallback, and the planner uses an estimate measured from the battery's
  own 5-minute power and SoC rows. Each charge window computes
  `AC_charge_kWh * eta_charge / dSoC` and each discharge window
  `AC_discharge_kWh / eta_discharge / |dSoC|`; a window counts only with a SoC move of at least 10
  points, a mean power of at least 20 % of the battery's own limit, finite values and no gap longer
  than one sample. Accepted windows are combined with a median after a MAD outlier filter, at least
  three are required, the result is clamped to a plausible 1..200 kWh, and an estimate older than 30
  days is ignored. The effective capacity is resolved once and used everywhere capacity is consumed
  — drivers, the DP optimiser, allocation, the savings baseline and the published summaries — with a
  stable reason slug logged as the learned value is applied or rejected. The estimate is stored as
  runtime data (`learning_profiles`), never over your typed capacity. **Manual always wins**: with
  the default source, or when the estimate is stale or not yet confident, the configured value is
  used unchanged.
- **`GET /api/v1/batteries` and the Batteries card show where the capacity comes from.** Each record
  exposes `capacity_source`, the configured `capacity_kwh`, `capacity_learned_kwh`,
  `capacity_samples`, `capacity_updated_at` and the `effective_capacity_kwh`, plus a nested
  `capacity` detail. The card states the effective value, the estimate, its window count and last
  update, and the Edit dialog switches between manual and learned through the normal save flow. All
  additions are additive; `capacity_source` defaults to `manual`, so an existing configuration plans
  exactly as before.

## 0.13.0

- **Savings are now earned only by energy the battery actually moved.** The savings accounting used
  to derive battery cycles from whatever power the driver reported, and some drivers read back the
  setpoint they had written — so a dry-run, blocked or stale-refused dispatch, or a setpoint the
  battery never confirmed, could still show up as delivered energy. Battery flow is now counted only
  when the driver read a real measurement back: a setpoint-only reading (the generic driver, or a
  Zendure whose measured pair went missing) reports the day's battery flow and cycle count as
  **unknown**, never as the planned value. `BatteryState` gains `power_measured`, the Zendure driver
  sets it for the complete measured pair, and `actuals.battery_kw` is left empty rather than filled
  from a command.
- **Measured AC battery flows are accounted per day and per range.** `savings_daily` gains
  `charged_kwh`, `discharged_kwh`, `charge_cost_eur` (priced with the buy series at flow time) and
  `discharge_value_eur` (priced with the sell series), plus per-day coverage flags for the grid,
  load, PV and battery series. `/api/v1/savings` exposes them on every bucket and in the totals
  (with `measured_buckets` counts), and the Savings view shows them under a clearly labelled
  *Measured battery flows* heading together with the coverage. A measurement gap makes the flow
  unknown (`--`), never a smaller partial total that would look measured.
- **Nothing else changed shape.** New columns are added by a forward-only schema migration; rows
  written before this version keep working and report the new fields as unknown. All API additions
  are additive.

## 0.12.0

- **Several PV forecasts can be merged into one hourly input.** The planner used to take its solar
  forecast from one provider. A new `solar.sources` list combines any mix of them instead: the
  Forecast.Solar API (`forecast_solar`), **any solar-forecast provider already configured in your
  Home Assistant Energy dashboard** (`ha_energy` — read locally over the WebSocket
  `energy/solar_forecast` command, so Solcast and friends work without re-entering the array), the
  watt-peak estimate (`capacity`), and the meter-derived one (`derived`). Each entry carries a
  finite `weight > 0` and may override the matching `solar.*` setting (planes, watt peak, geometry,
  weather entity, ...) for that source only. `solar.merge_strategy` picks `weighted_mean` (default),
  `median` or `max`. Every source is aligned onto the same 15-minute grid with the existing
  interpolation, so hourly and 15-minute sources mix cleanly; a failed or empty source is skipped
  with a note and the rest still produce a forecast. **An empty `solar.sources` (the shipped
  default) leaves today's single-source path byte-for-byte unchanged**, and if no source produces a
  usable series the single-source chain runs as the fallback. Bias correction and NASA clearness
  keep working inside each source before the merge. The Solar card shows the configured sources
  with their weight and status and can add, remove and reorder them through the normal dirty/Save
  flow; the JSON field in All settings remains the fallback editor. Each configured source is
  reported with `name`, `weight`, `state` (`ok | degraded | failed | disabled`) and a short note on
  the solar provider list and in `/api/v1/forecast?kind=solar`. New settings are validated: a
  malformed entry is reported per key and never crashes the engine.

## 0.11.0

- **A battery can be added and driven through the inverter's own MQTT bridge.** A SolarFlow whose
local MQTT feature is on publishes its zenSDK properties itself, and Home Assistant's `mqtt`
integration turns each into an entity (`number.<serial>_inputlimit`, `select.<serial>_acmode`,
`sensor.<serial>_electriclevel`, `switch.<serial>_smartmode`, ...). The new `zendure_mqtt` driver
writes those directly: the AC mode select for the direction, the two magnitude limits for the power,
and the device's own `smartmode` switch (on for a live command, off for a hold) — the Zendure-HA
integration sets exactly that flag with every AC command, and the inverter only acts on the limits
while it is set. No manager is involved and nothing waits for a P1 event. If the Zendure-HA
integration still manages the same inverter, its `operation` select can be mapped so the driver keeps
it `off` — otherwise it re-writes both limits on every P1 event. Add it with **Driver → zendure_mqtt**
in the battery dialog, or pick the device in **Find a battery**.
- **The battery picker now recognises a Zendure MQTT device and prefills the whole mapping.** The
device is identified by its platform plus manufacturer/model (Zendure / solarFlow…), and each row's
role is read from the zenSDK property name it carries — no confidence score, so the device-level
`electricLevel` always wins over a pack's `socLevel`, and the pick fills `ac_mode_entity`,
`charge_limit_entity`, `discharge_limit_entity`, `soc_entity`, `soc_target_entity`, `min_soc_entity`,
`output_power_entity`, `grid_input_power_entity`, `pv_power_entity` and `smart_mode_entity` plus the
`max_charge_w`/`max_discharge_w` the limit entities state. Discovery caps at 50 rows per read
(SPEC 10) and sorts by confidence, so the bridge device — whose property names score lower than the
integration's translation-key matches — is found by searching its device name or serial; the picker
says so when a read comes back without a battery.
- The **Driver** dropdown says where the entities come from, `Control path` is hidden for drivers that
have only one path, and the MQTT choice states plainly that no other controller may manage the same
inverter at the same time. `Dispatch` on the battery card reads *Inverter over MQTT: AC mode +
limits (the device's own entities)*.
- Save-time validation follows the control layer: the driver list is no longer duplicated in
`config.py`, so a battery is checked against exactly the drivers the dispatcher can build.

## 0.10.1

- **A battery can now be told which write path to use, and a Zendure inverter can be driven
directly.** `BatteryConfig.control_path` (`auto` | `manager` | `device`, default `auto`) is picked in
the battery dialog and validated at save time: `manager` needs the Zendure manager's `manual_power`
and `operation` entities, `device` needs that inverter's own `charge_limit_entity`
(`input_limit`) and `discharge_limit_entity` (`output_limit`). The **device** path writes the
direction select (`ac_mode` → *input* while charging, *output* while discharging) plus both limits,
and puts the manager into `off` so its next P1 event cannot overwrite them. That matters because the
manager applies a setpoint **only** when its P1 meter changes: on a house whose grid feed reaches
Home Assistant once every five minutes, a 1 kW charge planned for hours was applied as a five-second
blip about once every twenty minutes (the inverter path needs no P1 event at all). The battery card
now states which path is really in use (`Dispatch`), and the dialog explains the trade-off.
- **A command the device has not confirmed yet is no longer revoked.** The driver used to require the
*measured* AC flow to match within the read-back window and otherwise report `driver_error`, which
with `control.failsafe_on_error` wrote 0 W back — cancelling the very command the device was about to
apply (live 2026-10-12: −1000 W written every 30 s and zeroed 4.6 s later, forever). Such a command is
now reported as the new dispatch code `commanded`: `applied_w` is the setpoint that is in force,
`readback_w` what the device reports, and the message names both. `driver_error` (and the fail-safe
hold) is kept for a write that really did not land, or a manager that cannot be held in `manual`. The
**Plan** view states it in words ("commanded 1.00 kW of 1.00 kW requested; the device has not
confirmed it yet") and the measured-vs-planned line remains the honest check.
- The device path confirms itself honestly: `input_limit`/`output_limit` are device properties, so a
matching read-back **is** device evidence (the inverter echoes them within a few seconds over zenSDK
or MQTT), and the AC flow is the second proof.

## 0.10.0

- **Every planned window now states the data behind it, not just its action.** The **Next actions**
  list shows, per window: the buy and sell price with their spread, the break-even
  (`buy / round_trip + wear`) and the margin above it, the planned kWh in and out, the SoC path and
  the floor a reserve hold defends, the planned grid flow and what it costs or earns, the price
  provider's age, and the window's rank by buy price in the horizon. One line above the list adds the
  windows up. The figures come from the plan payload the UI already had - they were simply never
  shown, which made a 15-minute grid and an hourly tariff look identical on screen.
- **The plan now reports its own resolution, and the interface uses it.** `GET /api/v1/plan` carries
  `slot_minutes` (SPEC 10, additive). Before this the UI assumed 60 minutes for every plan, so a
  15-minute plan was captioned as hourly, window end times were up to an hour wrong, and the "now"
  slot was looked up on the wrong grid. The offline render check now asserts the payload's own
  resolution drives the window range.
- **New additive route `GET /api/v1/plan/execution`.** It joins the plan's slot grid with the
  persisted 5-minute actuals and the dispatch log: per slot, the last dispatch attempt per battery
  (requested vs applied, clamped, dry run, mode, reason) and the measured aggregate (kWh in/out,
  import/export, average power, SoC start/end, how much of the slot was actually observed). A window
  therefore says "applied 400 W of 800 W requested (clamped)" or "measured -0.70 kW vs -0.80 kW
  planned" instead of only what was intended, and a slot nobody reached reports nothing rather than a
  fabricated zero.
- **A stale window says what it means: it is never dispatched.** The dispatcher refuses a slot whose
  price forecast is not fresh, so the window now states `not dispatchable: this slot has no fresh
  price (stale_input)` instead of presenting its plan power as if it had been applied. With an hourly
  tariff on a 15-minute plan grid this is the difference between three quarters of every hour looking
  scheduled and being visibly blocked.
- The Plan view refreshes in place when you return to the tab (`state.refresh`), so the "now" marker,
  the measured figures and the price age no longer sit on a screen that was opened hours ago. Panels
  reload without flashing "Loading…".

## 0.9.1

- **The Meter card now shows where the grid meter comes from.** With every `grid.*` key empty it
  names the roles the add-on resolved from the Home Assistant Energy dashboard ("From the Home
  Assistant Energy dashboard: signed power …, import counter …"); a saved value is labelled as an
  override of that fallback, with the dashboard's own roles beside it, and a **Use the Energy
  dashboard** button clears `grid.power_entity` / `grid.import_entity` / `grid.export_entity` back to
  the automatic path. The settings themselves stay empty - this only makes the 0.9.0 runtime default
  visible (and escapable), since a saved value previously disabled it with no way back from the UI.

## 0.9.0

- **The meter now comes from your Energy dashboard by default.** The add-on reads `energy/get_prefs`
  (read-only, no admin needed) and uses the grid source Home Assistant already has: its signed power
  sensor (positive = import) for live sampling and control, or - when no power sensor is configured -
  the import/export energy counters as a long-term-statistics history source. The **Meter** card can
  stay empty; an explicit `grid.*` value still wins, and the read is cached (5 minutes) so an
  unreachable Home Assistant costs one attempt, not one per tick.
- **The derived load and solar estimates now read history from long-term statistics**, which are
  never purged: hourly means for power sensors and hourly `change` for energy counters, converted to
  average kW. A recorder with the default 10-day purge (or an Energy dashboard with only kWh
  counters) still gives the meter-derived solar fit weeks of data, so the learned array size is not
  limited to whatever the recorder still holds. Recorder history stays the fallback.
- **A solar source with no planes now estimates from the meter instead of failing.** With
  `solar.source=forecast_solar` and an empty `solar.planes`, the meter-derived estimate runs when a
  meter resolves (an explicit mapping or the Energy dashboard) and the forecast says so; only when
  that is impossible too does the panel report `no_pv_source`, now naming the derived failure in its
  hint.

## 0.8.2

- **The plan grid now defaults to 60 minutes**, matching the hourly dynamic tariffs most suppliers
  publish (`optimizer.slot_minutes` = 60; was 15).
- **The price grid now starts where the plan grid starts.** This is the fix that makes the 60-minute
  default safe: the plan grid is floored to its own step, so a price forecast starting at the
  *current quarter* (16:45 at 16:50) left the plan's first slot - the one the dispatcher acts on -
  filled from a later slot and flagged stale, and the dispatcher never executes a stale current
  slot. With an hourly tariff on a 15-minute plan grid that meant the battery was blocked for three
  quarters of every hour, and the optimiser discarded 90 of 120 slots as flat. Now the current plan
  slot always matches a real provider entry: an hourly feed plans 30 fresh slots with no gaps, and a
  quarter-hourly feed is unchanged (its grid still starts at the current quarter). It also stops
  `sensor.sem_price_now` reporting the *next* hour's price during the last quarter of the hour.
- **A fully covered forecast is no longer reported as a short tail.** The grid ends one step before
  the horizon (`slot_range` excludes its end), and comparing that slot's *start* with the horizon
  marked every complete forecast `degraded` and added a "coverage ends …; uncovered slots are stale"
  note that was simply false. Coverage is now measured with the fresh slots' own cadence.
- Dispatch resolution follows the same setting: setpoints change once per slot, and the action
  windows, captions and slot table all report the plan's own resolution (it is never assumed to be
  15 minutes). Setting it in the add-on's Settings page overrides the new default.
- The offline demo fixtures (`?mock=1`) use the same 60-minute plan grid, so the demo's captions and
  slot table show what a default installation shows.
- Stale-price warning reworded to name plan slots rather than "price slots".

## 0.8.1

- **The chart legend no longer lands on top of the text under it.** On a phone the legend was drawn
  inside the chart's fixed height and overflowed onto the explanation below it, which made both
  unreadable. The legend and the one-line chart caption are now their own rows under the plot and
  wrap like normal text. Series names also say what they are without a colour word ("Stale price
  slot", "Charge level", "Upper uncertainty (long dashes)").
- **Axis numbers are consistent and carry their unit.** A percent axis used to mix "100" with
  "75.00"; every tick now uses one precision, percent ticks read "25 %"–"100 %" and the rotated
  percent label that collided with them is gone (the other axes keep a smaller gutter on a phone).
- **Less prose, in the right place.** The long paragraph under the plan timeline (which repeated the
  legend) is replaced by one short caption per chart plus a single line about the break-even line;
  the worked break-even maths stays in "Why this plan?". The worked example now names what it
  computes ("a break-even of 0.29 EUR/kWh, which includes 0.05 EUR of wear per kWh delivered").
- **Plainer wording in the plan views.** "Discharge · 700 W" (not "-700 W discharge"), single-slot
  action windows show one time and multi-slot windows end at the end of their last slot, the plan
  summary rows name what they are ("Estimated net cost", "Saving vs baseline", "Energy charged"),
  the stale-price warning says what staleness does to the plan, timestamps read "just now" instead
  of "0 s ago" and whole minutes read "7 min" instead of "7 min 0 s".

## 0.8.0

- **Every screen now answers first, then explains.** Today opens with the control authority, what the
  battery is actually doing and what the plan intends, instead of nine equal cards. Plan says whether
  the horizon saves or loses money before showing the timeline. Forecasts start with the measured
  error and how fresh the source is. Savings starts with the comparable saving or loss and how many
  periods actually had a baseline. Log starts with the message. Worked examples, raw capabilities,
  mappings and long provider notes moved behind named disclosures ("Why this plan?", "Efficiency and
  wear", "Source details", "Entity mapping", "All slots").
- **A short navigation instead of seven equal tabs.** Today, Plan, Savings and Manage fit one row on a
  phone; the second row is contextual (Plan → Forecasts, Manage → Batteries, Settings, Log). All seven
  pages keep their own link, and each view has one heading that gets focus when you navigate.
- **Setup cards that can actually finish their job.** The fixed day/night window hours, the day-ahead
  VAT/energy-tax/import-fee adders, the array's watt peak and the Forecast.Solar plane list are on the
  card that selects the option, instead of hidden in the advanced drawer. Settings also shows the
  current authority (mode and dry run) permanently, keeps search and "show all advanced" always
  visible, and reports saving success or a rejected value in the always-visible save bar — with the
  server's message attached to the field it rejected. The Battery card reads the battery list Home
  Assistant actually stores, so an empty or stale descriptor can no longer claim you have no batteries.
- **Nothing you are reading is thrown away by a refresh.** Polling updates values in place and keeps
  your scroll position, focus and expanded detail. It pauses while a dialog is open, a change is being
  saved, you have unsaved settings or a Stop result is on screen. The Log tells you a new entry
  arrived instead of rebuilding the list you are reading, and Retry now always makes a new request.
- **One authority, one explanation, one confirmation.** Turning on Auto or turning dry run off asks the
  same question and gives the same answer wherever you do it; the mode buttons respond on the first
  click; and a cancelled dry-run change puts the confirmed switch state back. A test discharge now
  says in plain text what it is waiting for ("Requires Auto and dry run off — currently mode is …").
- **Numbers you can trust at a glance.** Forecast solar and load are labelled kW (they used to be
  labelled as energy), forecast error is shown with the unit it is measured in, a day with no forecast
  data is left blank instead of plotted as zero, partial baselines state their coverage, and "Loss",
  "Unknown" and "Stale" are separate visible labels. Readability and touch: darker/lighter metadata
  text that meets contrast in both themes, ~44 px controls, and one type scale instead of many close
  sizes.
- **Charts are identifiable without colour.** Each series has a fixed line signature (solid, long
  dashed or dotted) named in words in the legend, the break-even threshold is dotted and the sell price
  long-dashed, axis gutters narrow on a phone without dropping their unit, and switching the theme by
  hand redraws the charts immediately.

## 0.7.0

- **Wear is derived from your battery, not typed in.** Each battery now takes an optional purchase
  price and cycle life; the add-on computes wear per kWh delivered as
  `price / (cycle life × usable kWh × discharge efficiency)`, so one full cycle costs exactly
  `price / cycle_life` (3000 EUR over 6000 cycles on 9.6 kWh usable at 95 % discharge ≈ 5.5 ct/kWh).
  Charging alone costs no wear because wear is charged on the discharge leg only. The plan uses the
  delivered-energy-weighted mean over the fleet; the advanced `optimizer.cycle_cost_eur_per_kwh`
  setting stays as an override and, when non-zero, replaces the derived value. The battery card shows
  the derivation and the Plan summary shows the wear it used.
- **The break-even is simpler: `buy / round trip + wear`.** Because wear is now stated per kWh
  delivered, the formula no longer needs the discharge efficiency; the chart line, the battery card,
  the idle-slot `efficiency_loss` reason and the docs all use the same expression.
- **Solar accuracy: NASA POWER observed irradiance (on by default).** The capacity and meter-derived
  solar sources now learn a per-hour-of-day clearness index from NASA POWER's observed all-sky and
  clear-sky hourly irradiance over the last three weeks (keyless, ~2-3 day latency, one request per
  location per day, cached). It fills in slots the weather entity has no cloud data for and refines
  the meter-derived watt-peak fit. It is an addition, not a replacement: the weather-entity cloud
  attenuation and the measured-PV bias keep priority, `forecast_solar` is unchanged, and a NASA
  failure is a note, never a failed forecast. Turn it off with `solar.nasa_irradiance`.

## 0.6.0

- **Four setup cards instead of a wall of options.** Settings now opens on **Price**, **Meter**,
  **Solar** and **Battery** cards, each with only the primary choice of its topic, and one collapsed
  **All settings** drawer holding the full grouped form. The always-visible set dropped from 55 rows
  to 21, and the `control.*` tuning knobs moved out of the never-folding *Safety* group into a
  foldable *Control* group.
- **Searchable entity pickers.** The Meter card and the Solar card search your Home Assistant
  entities (`/discover`) and save the entity you pick; a typed entity id can always be used directly.
  An export-only grid sensor is saved to `grid.export_entity` rather than to the card's key, because
  the candidate row says which setting it belongs to.
- **Solar from the meter, with nothing to enter.** `solar.source = derived` learns your array from
  the moments your meter exports: `pv = export + battery charge`. It takes the cloudless envelope of
  those samples to size the array and then runs the same sun model as the watt-peak source. The house
  load at those moments is not subtracted, so the learned size is deliberately conservative; an
  explicit `solar.capacity_wp` overrides it.
- **Feed-in price is derived now.** The sell price is `buy − feed-in cost` (the new name for
  `price.export_fee_eur_per_kwh`, default 0), minus the energy-tax component when
  `price.feed_in_energy_tax_refund` is off — the two cases a saldering contract can have. A flat
  tariff or a feed-in sensor still wins if you set one, and `optimizer.allow_export` still defaults
  to off.
- `PriceSlot` gained an additive `energy_tax` field (per slot from the Zonneplan included/excluded
  pair, otherwise the configured wholesale adder), and discovery rows for price/solar/load now carry
  the `setting_key` the pickers write.


## 0.5.1

- **Fixed: an update could take an hour to appear.** The add-on served its own assets - `style.css`,
  `app.js`, `charts.js`, `mock.js`, the vendored framework - with `Cache-Control: public,
  max-age=3600`, and the asset URLs carry no version, so a browser could keep rendering the previous
  release's interface for up to an hour after an update, with nothing on screen to say so. Every asset
  now revalidates on every load. The top bar shows the running version, so the panel itself tells you
  which release you are looking at.

## 0.5.0

- **The interface was rebuilt: same features, far less wall-of-fields.** The settings form is now a
  group rail - General, Price, Solar, Load, Grid, Optimizer, Safety, Notify, Batteries - showing one
  group at a time, with a sticky save bar that counts your unsaved edits and asks before you navigate
  away from them.
- **Every setting is reachable now.** The old form silently dropped any field that did not apply to
  the selected source *and* still held its default value, so options simply did not exist as far as
  the UI was concerned. Nothing is dropped because of its value any more: fields that do not apply to
  the current price/solar source stay listed (muted, "not used by the current selection") when they
  hold something, and everything else is behind a per-group `Advanced (n)` disclosure that remembers
  whether you opened it. `Show all advanced` opens every group at once. Safety settings are never
  folded.
- **The server decides what is advanced.** A new additive `advanced` row field carries that
  classification from the configuration metadata (SPEC 13.3), so the UI cannot drift from the backend
  and no label has to say "(advanced)" any more.
- **Bootstrap 5.3.3, vendored as CSS.** The look is a design layer over Bootstrap's tokens rather than
  a pile of bespoke rules: one accent colour, 1px borders, an 8px spacing scale, tabular figures, no
  gradients or webfonts, light *and* dark palettes, and a three-state theme control (auto / dark /
  light) that follows your operating system by default. Still no build step and still no CDN: the
  stylesheet is a pinned file in `vendor/` (checksum and refresh steps recorded) and the audit gate
  skips it only while its bytes match that record.
- Fixes found while building it: every free-text setting rendered as an empty dropdown (the API sends
  `choices: []`, which the form mistook for a select); `control.stale_plan_seconds` was missing from
  the offline mock entirely; and `price.feed_in_entity` escaped folding because a front-end safety
  regex was doing double duty as the fold rule.
- New gates: `tools/ui_render_check.js` renders the real front-end headless through a small DOM shim
  and asserts that every view renders, every applicable setting is reachable, folding and unfolding
  work, no number prints as `undefined`, every emitted class exists in CSS and no asset is remote.

## 0.4.0

- **AC-coupled solar, estimated from the array's watt peak.** `solar.source` now has two modes:
  `forecast_solar` (the API, per-plane geometry) and `capacity` (new) - state the array's total Wp and
  the add-on estimates production itself. No API key, no plane geometry, and it fits an AC-coupled
  array whose production reaches the house through the meter rather than through the battery.
- **A documented model, not a black box.** Solar elevation and azimuth from the standard declination
  and equation-of-time equations, Haurwitz clear-sky irradiance, isotropic transposition onto the
  plane, optional Kasten-Czeplak cloud attenuation from a Home Assistant weather entity, and a
  performance ratio (default 0.85). Tilt and azimuth are optional: left unset, the forecast *states*
  that it assumed south-facing at 35 degrees instead of implying geometry you never entered.
  Uncertainty is 35% without cloud data and 25% with it.
- **Your real production makes it more accurate.** Point `solar.measured_entity` at the inverter's AC
  output and the per-hour learned correction (EWMA, clamped, decaying after 14 days) narrows the
  estimate from what actually happened. Use the inverter's AC output, not a meter's export register:
  self-consumed energy never reaches the meter, which would bias the correction low.
- Cloud attenuation uses `weather.get_forecasts`. An entity that supports no forecast type at all
  (some integrations publish only a current `cloud_coverage`) is not a failure - the current cover is
  held flat and the note says so; with no cloud data the estimate is clear-sky and says that too.
  Weather never fails the forecast.
- A missing `solar.capacity_wp` fails with a hint naming the setting; a capacity is never guessed from
  history, and an empty solar panel now names whichever setting its configured source is missing.
- The settings form shows only the fields the chosen source uses (planes and API key for
  `forecast_solar`, watt peak, tilt, azimuth, performance ratio and weather entity for `capacity`),
  and names any hidden field that still holds a value.

## 0.3.0

- **You choose where prices come from, and what your contract looks like.** Two settings replace the
  need to know which sensor to paste: `price.provider` (Zonneplan / day-ahead market / fixed tariff /
  your own entity) and `price.contract` (dynamic per quarter hour, dynamic hourly, one fixed rate, or a
  day/night split). The add-on resolves the concrete source, says which one it used, and fetches from
  there.
- **Zonneplan is found in Home Assistant, structurally.** The tariff sensor is located by its payload
  (a kWh unit, at least two entries on a regular 15-minute or hourly grid, and its own state agreeing
  with the slot it is in) and never by its name. That matters: the gas tariff has the *same*
  fixed-point shape and its state agrees with its own single slot, so only the unit and the grid
  separate them. A quarter-hourly contract prefers the 15-minute sensor, an hourly contract the hourly
  one, and having to use the other grid is reported as a note rather than passing silently.
- **Day-ahead prices** come from energy-charts, keyless, for the configured bidding zone. They are
  wholesale: the forecast carries a note naming the VAT, energy tax and fees actually applied, because
  a wholesale price with no adders is not a consumer price. Zone coverage is real - a valid zone can
  simply publish nothing for the window, and the hint then names that zone and points at the
  alternatives instead of claiming a generic failure.
- **A fixed tariff** is built from one or two consumer prices (typed, or read from a sensor you point
  at) and, for the day/night split, the two window start hours. Windows are evaluated per slot in your
  timezone, so a window that crosses midnight and DST both work. A fixed contract has **no market
  fallback**: a missing price fails loudly with a hint naming the field, because silently planning a
  flat tariff against market prices would be worse than not planning.
- **An empty forecast panel now explains itself.** `GET /api/v1/forecast` carries `provider`,
  `contract`, `notes`, `reason` (a stable slug such as `provider_failed`, `fixed_price_missing`,
  `no_pv_source`, `no_history`) and `hint` - one sentence naming the setting or condition to fix. The
  forecast view shows the hint verbatim with the reason beside it, plus the source, provider and
  contract, instead of the old "the source failed or the horizon is empty".
- `GET /api/v1/config` carries `price_warnings`: provider/contract mismatches, a missing fixed price, a
  collapsed day/night window, and wholesale prices with no adders configured. Warnings are reported,
  never fatal - refusing to plan would be worse than planning with a stated mismatch. The settings
  form shows only the price fields the chosen provider and contract use, and names any hidden field
  that still holds a value, so nothing takes effect invisibly.
- The advanced `entity` provider keeps its old behaviour and remains the default, so an existing
  configuration is unaffected. `ct/kWh` sensors are now scaled correctly on the state comparison.

## 0.2.0

- **Batteries are selected, not typed.** The add-battery dialog lists the batteries discovery can see
  and filters them as you type. Each row names the device (the name you gave it), its model,
  manufacturer and platform, how many battery roles it can fill, and whether it can actually be
  driven. One *Select* fills the id (slug derived from the device name, made unique against the
  batteries you already have, still editable), the name, the driver, the whole role mapping and the
  device's own numeric limits: charge and discharge limits from the entities that carry them, and
  capacity from the pack figure.
- Read-only look-alikes are never offered as drivable. A device whose only control-role matches are
  sensors is labelled `control entities are read-only` and ranked below a real battery, and a sensor
  is never auto-mapped to a setpoint role. Discovery legitimately returns lone `soc`, `connection` or
  `manual_mode` suggestions for thermostats, bridges, printers and phones; they no longer appear as
  batteries.
- **Fixed: usable energy could be mapped as the installed capacity.** `available_kwh` (what is left
  above `min_soc`) and `total_kwh` (how big the pack is) both matched the single `capacity` role, so
  the wizard could write a nearly empty battery's reading - negative, on a live install - into
  `capacity_entity`. Usable energy now has its own role and its own driver key.
- `GET /api/v1/discover` rows gained the additive `device_id`, `device_name`, `manufacturer`, `model`,
  `min` and `max` fields (SPEC section 10). Without a reachable device registry they stay null and
  nothing else changes.

## 0.1.2

- **Fixed: the add-on still could not start — the Supervisor token never reached the entrypoint.**
  0.1.1 got past the option validation and then exited immediately: the base image's s6-overlay
  entrypoint runs the container command as a service, and s6 services do not inherit the container
  environment, so `run.sh` saw neither `SUPERVISOR_TOKEN` nor `HASSIO_TOKEN` and refused to start.
  The command now goes through `/command/with-contenv`, which imports the environment the Supervisor
  passes to the container. The fail-fast check is unchanged and still fires when no token is set.
- The CI image job now runs the built image with a Supervisor-style environment and fails if the
  token does not reach the entrypoint or the app never reaches its start banner — the two defects
  that reached a live install were both invisible to a build-only check.

## 0.1.1

- **Fixed: the add-on could not start on a fresh install.** `options:` carried explicit `null`
  values for the four keys that are read from Home Assistant at runtime (`currency`, `timezone`,
  `latitude_override`, `longitude_override`) while the schema declared them optional. Supervisor
  rejects a null option value and refused the start with `Missing required option 'currency'`.
  Those keys are now absent from `options:` (which is what "optional" means), and
  `tests/test_wp4_manifests.py` fails the suite if a null or a stray schema key returns — the
  previous check required `options` and `schema` to hold identical key sets, which is what forced
  the nulls in.
- The image now takes the manifest version from the Supervisor build (`ARG BUILD_VERSION`) instead
  of relying on a hardcoded string, so the store, the log banner and the API report one number.

## 0.1.0

First release.

- Dynamic-price aware battery planning: 15-minute slots over a 12-48 h horizon, SoC dynamic
  programming with per-battery allocation.
- Solar forecast from Forecast.Solar (keyless), price forecast from a Zonneplan price sensor,
  energy-charts day-ahead or any sensor that publishes a forecast attribute.
- Load learning from long-term statistics (median weekday/weekend hourly profile, level scaling,
  EWMA bias correction, optional temperature term) with a flat fallback on a cold start.
- Modes `off`, `advisory` and `auto`, with `dry_run` in front of every write.
- Safety guard rails: SoC floor/ceiling, power and grid limits, ramp limit, stale-state hold,
  override with a bounded timer, `Stop now` kill switch, failsafe 0 W on error.
- Own web UI served through Home Assistant ingress (no build step, no CDN assets).
- Publishes `sensor.sem_*` state back to Home Assistant over the state API.
- Savings accounting against a do-nothing baseline; accuracy reporting for PV, load and price.
- Zero third-party Python dependencies: the Python standard library only.
