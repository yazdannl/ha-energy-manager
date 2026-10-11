# Verification record — Smart Energy Manager add-on

Authoritative verification record for the tree published from this repository. Every number below
was produced by a gate named in §11 on the frozen tree; nothing here is a projection. This record
supersedes the earlier WP5 report — the 292 / 359 / 398-test and 83-file figures in previous
revisions described pre-audit trees and are obsolete.

Verification was local and hardware-free: no real battery, no live provider call, no instance data.
All synthetic values use reserved namespaces (`sensor.example_*`, `number.example_*`, `sem_*`),
loopback addresses and placeholder coordinates.

---

## 1. Verification state

| Gate | Command (from the repository root) | Result |
| --- | --- | --- |
| Test suite | `python3 -m pytest tests -q` | **663 passed** (two consecutive runs) |
| PII / secret scanner | `sh tools/audit.sh` | `audit: clean — 93 files scanned`, exit 0 |
| Add-on selfcheck | `PYTHONPATH=energy_manager python3 -m app.main --selfcheck` | **31/31 checks passed**, exit 0 |
| Syntax / imports | `python3 -m compileall -q energy_manager/app tools/make_icons.py tests` | clean, exit 0 |
| Headless UI render check | `node tools/ui_render_check.js` | **49 passed, 0 failed**, exit 0 |

Frozen digest of the verified artifact (recipe in §11, normative):

* **code + tests (79 files, documentation excluded)**:
  `581baf1fd3fd0465f639d90ccdce29e6a9d530e5015f81f86b6ff1145c5ff2f7`
* **path list of the same 79 files** (detects an added, removed or renamed file):
  `999c72a0ad237efad84631e1c196871fbab19aaf4e46b1e3e55f351c7c988ad5`

The recipe sorts the file list **and** the per-file manifest under `LC_ALL=C`, so the digest is
independent of `find` order and of the locale; a single `sort` is not enough. An earlier revision of
this record published digests from an order-dependent pipeline and they could not be reproduced — a
defect in the record, not in the tree: the file lists were verified byte-identical to the frozen ones,
so no source file had changed. Those values (`8f83a0a6…` all files, `c58e912c…` code + tests) are
superseded by the digest above.

The all-files digest (94 files) is deliberately **not** recorded here: this document is one of the 94,
so every documentation edit would change it while the verified code stayed identical. Compute it with
the same recipe over the full file list if you want it; the file count printed by the pipeline (94) is
the quick cross-check. (`tools/audit.sh` prints 93 because it removes the sha-verified vendored
stylesheet from its scan list before scanning.)

---

## 2. History: why this record exists

Three independent adversarial audits reviewed earlier revisions of the tree — **gpt-6-luna**,
**deepseek-v4.1-flash** and **step-5-preview**. All three returned **DO NOT SHIP**. Two fix waves
followed, and this record documents the state after them:

* real-and-serialised holds, with a persisted per-battery live-write latch and boot neutralisation;
* advisory installs provably writing nothing, including on exit;
* an atomic `Stop now`;
* grid caps driven by live metering;
* a stale-plan execution gate;
* the sell price no longer silently being the consumer price, with `allow_export` off by default;
* Home Assistant timezone plumbing;
* refusals for derived-load roles without the entities they need;
* plan retention;
* targeted entity reads with 5-minute measurement buckets;
* the savings baseline as net cost, reported unknown rather than zero when it cannot be known.

The defects that mattered, all reproduced by the auditors:

* `hold()` was a **silent no-op** when the measured flow happened to read zero — it even survived the
  original mutation set, because no test distinguished "already at 0 W" from "command never sent";
* an **advisory exit wrote to the hardware**;
* an **idle-battery staleness regression** stopped accepting writes after the stale window;
* a **2-tuple / 3-tuple mismatch crashed the publisher**, so no `sensor.sem_*` state was published
  at all;
* **sell price = consumer price + VAT**, which plans export at revenue that does not exist.

The pre-audit suite was green at **398 tests** while all of those existed. A green suite was not
evidence that the safety, packaging or pricing story was sound, which is why independent audits were
run instead of trusting it.

### The defect the real install found

The three audits, both fix waves and the 493-test suite all missed something that the first real
install hit within a minute of pressing start:

```
App f20c2e09_energy_manager has invalid options: Missing required option 'currency'
```

`config.yaml` listed the four options that are read from Home Assistant at runtime (`currency`,
`timezone`, `latitude_override`, `longitude_override`) in `options:` with explicit `null` values,
while the schema declared them optional. Supervisor rejects an explicit null, so the add-on built,
installed, appeared in the store — and then refused to start. The root cause was the packaging check
itself: it asserted `set(options) == set(schema)`, which *forced* those keys into `options:`, so the
only way to satisfy it was to invent values for keys whose entire purpose is to stay unset.

Fixed in **0.1.1**: the keys are absent from `options:` (which is what optional means), the check is
now a subset test plus a no-null rule, and `tests/test_wp4_manifests.py` enforces the same rules in
the suite itself rather than only in CI. The guard was proved failing-first — with the nulls
reintroduced on a copy, `test_no_option_carries_a_null_value` and
`test_options_that_come_from_home_assistant_stay_optional` fail — and the manifest was then
confirmed by running the built image locally with a Supervisor-style `/data`, where the add-on got
past option validation and failed for a second, unrelated reason (below). The 0.1.1 commit message
claimed the fix had been confirmed on the live instance; it was written before that was true, and
this record corrects it.

This is why the packaging tests now run locally: every other gate in this record was green while the
add-on could not start at all.

### The second defect the install found: the token never reached the entrypoint

Same class, one layer down. After the manifest fix the add-on still went to `error` within seconds —
no `sensor.sem_*` entity published, no notification, no plan: it died before doing anything
observable. Running the built image locally with `-e SUPERVISOR_TOKEN=...` reproduced it in one line:

```
energy_manager: neither SUPERVISOR_TOKEN nor HASSIO_TOKEN is set.
```

The base image's s6-overlay entrypoint runs the container command as a *service*, and s6 services do
not inherit the container environment (that is what `with-contenv` exists for). The token was
never visible to `run.sh`, which by design refuses to run blind, so the container exited 1 on every
start. `CMD` now goes through `/command/with-contenv`, and the fail-fast check still fires when no
token is set. Verified in the real image both ways — token present: starts, serves, logs its banner;
token absent: exits 1 with the same message — and the CI image job now runs the built image with a
Supervisor-style environment, so a build-only check can no longer pass while the entrypoint is
broken.

---

## 3. Mutation verification

The final verifier re-ran **13 mutations** against the frozen tree. All 13 are caught.

| # | Mutation | Caught by (failing test) |
| --- | --- | --- |
| 1 | `hold()` becomes a no-op | `test_zendure_hold_never_treats_a_lagging_measurement_as_idempotent`, `test_zendure_hold_always_writes_even_when_everything_reads_zero`, and the boot-recovery retry test |
| 2 | Driver freshness taken from the Home Assistant `last_updated` value instead of the read moment | `test_driver_freshness_is_the_read_moment_and_freezes_are_detected_across_polls`, `test_idle_battery_keeps_accepting_writes_past_the_stale_window` |
| 3 | The live-write latch gate removed | `test_exit_holds_never_touch_an_install_that_commanded_nothing` |
| 4 | The control lock removed | `test_a_stop_drains_an_in_flight_tick_instead_of_interleaving_it` |
| 5 | Terminal salvage computed without `eta_discharge` | `test_salvage_is_discounted_by_the_discharge_efficiency`, two oracle instances, and the objective-split test |
| 6 | The break-even gate removed | `test_a_round_trip_loss_labels_the_idle_slot_efficiency_loss`, `test_efficiency_loss_is_a_documented_plan_reason` |
| 7 | The plan-age refusal removed | `test_a_plan_older_than_the_bound_is_refused_as_stale_input` plus two more plan-age cases |
| 8 | Actuals bucketed at 15 minutes instead of the measurement resolution | `test_actuals_are_bucketed_at_the_measurement_resolution` |
| 9 | Plan retention removed | `test_trim_retention_keeps_only_the_newest_plans` |
| 10 | Derived-load role checks removed | `test_derived_tier_skips_intervals_without_battery_flow_samples`, `test_derived_tier_is_refused_without_a_pv_role` |
| 11 | The `dead_ha_url` fixture released while its accept thread still holds the port | `test_the_dead_ha_url_still_holds_its_port` |
| 12 | The override reserve removed | `test_override_pinning_respects_the_pinned_batterys_own_floor` |
| 13 | An inexact round-trip split | the `round_trip_efficiency_pct` splitting / exactness tests (`test_round_trip_efficiency_pct_splits_evenly`, `test_round_trip_pct_is_exact_for_common_values`, `test_break_even_uses_the_requested_round_trip`) |

| 14 | `available_kwh` matched the `capacity` role again (the pre-0.2.0 role hint), or its role stopped resolving to `available_kwh_entity` | `test_usable_energy_is_not_offered_as_capacity` (proved failing on the old vocabulary), `test_the_energy_roles_map_to_different_driver_keys` |

Honest notes on this table: mutations 4 and 11 were **initially survivors on the previous hash** and
were closed by adding a two-battery barrier test (mutation 4) and a port-hold assertion
(mutation 11). The verifier also corrected one of its own earlier mutants — it had released the file
descriptor while the accept thread still held the port, which is not the behaviour the test claims.
A survivor-free result for these 13 mutations does not prove the suite catches arbitrary
regressions; it proves these specific, previously demonstrated defects cannot return unnoticed.

---

## 4. The efficiency feature, independently checked

* `round_trip_efficiency_pct` is the input; the split is exact — `sqrt` with a 1-ULP correction so
  that the product of the two directions is exactly the requested round trip.
* The break-even sale price is `buy / round_trip + wear`, with wear derived per battery from
  `price_eur / (cycle_life × usable_kwh × eta_discharge)` so that one full cycle costs exactly
  `price_eur / cycle_life`; 20 ct at a 75 % round trip gives 26.666… ct exactly (not 25 ct, which
  would be the 80 % case).
* The planner holds when the sale price is below break-even and cycles above it
  (`test_a_round_trip_loss_labels_the_idle_slot_efficiency_loss` and its counterpart
  `test_the_same_slot_with_a_full_round_trip_is_not_an_efficiency_loss`).
* The DP's terminal salvage is discounted by `eta_discharge`; removing that discount measurably
  inflates a saving (about 0.14 EUR on the verifier's probe).
* The UI maths in `app/web/app.js` matches `models.py`.
* The `?mock=1` payload matches the real serializer key-for-key
  (`test_round_trip_efficiency_in_mock_payloads`).

---

## 5. The battery picker (0.2.0), checked against a real registry

The battery dialog used to ask for a slug. It now lists the batteries discovery can see, filters them
as the user types, and fills the whole form from one row. That behaviour sits on data no synthetic
test could anticipate, so it was checked twice: by the suite (node-eval of the pure helpers, plus mock
and manifest parity) and against the live instance's own registries.

Rows are grouped per device (entity-registry `device_id`, enriched from the device registry by
`Engine.discover`), and a group becomes a picker row only when it is plausibly a battery: a known
battery platform, or at least one control role together with one state role. On the real registry that
is 50 candidate rows over **13 device groups: 3 offered, 10 rejected** — a thermostat, a printer, a
toothbrush, four phones, a bridge, an add-on's `running` binary sensor and a tuya device, each of
which matched a lone `soc`, `connection` or `manual_mode` hypothesis.

Three things were visible only on real data, and each changed the result:

* **limits.** The device publishes its charge and discharge limit entities as positive-only ranges
  (`0…1000 W`, `0…800 W`). A sign-based prefill would have written the charge bound into the discharge
  field; the prefill is role-aware, so each limit comes from its own entity, and on the live rows it
  fills `max_charge_w = 1000` and `max_discharge_w = 800`.
* **usable energy versus capacity.** `available_kwh` (usable energy above `min_soc`, reading `-0.0384`
  on the live battery) and `total_kwh` (the pack, `1.92`) both matched the single `capacity` role at
  equal confidence, and the tie-break chose the first alphabetically: the wizard would have written a
  momentary reading into `capacity_entity`. Usable energy now has its own role and its own driver key,
  the picker maps `capacity_entity` to the pack figure and `available_kwh_entity` to the usable one,
  and it prefills the capacity from the former. `test_usable_energy_is_not_offered_as_capacity` was
  proved failing against the old vocabulary; mutation 14 records the regression.
* **writability.** Control roles require a writable entity (`number`/`select`/…). The live registry
  contains a device whose control-role matches are all read-only sensors, which would otherwise rank
  equal to the real battery; it is now reported as `control entities are read-only`, ranked below, and
  given no control role by the auto-mapping.

From that one click on the live battery the picker fills: driver `zendure`, **10 roles** mapped,
capacity `1.92 kWh`, charge `1000 W`, discharge `800 W`.

---

## 6. The price provider and contract selection (0.3.0)

The forecast view was empty on the live install and could only say "the source failed or the horizon
is empty". The price panel was empty for one reason: no price source was configured. That was checked
before anything was built, by feeding the instance's own Zonneplan payloads into the then-shipped
provider: both tariff sensors parse correctly (the quarter-hourly list and the hourly one; fixed-point
÷1e7; the scale check passes against the sensor state), so the source path was never broken. What was
missing was a way to *choose* a source, and an explanation when a panel cannot be filled. Solar was
empty because no PV planes are configured, and load because nothing was measured yet.

**Every claim below was checked against the instance's own `/api/states` payload** (1337 entities with
attributes), not a hand-written fixture. Three scripts, none of them part of the repo:

* provider resolution, 28 checks: the contract picks the grid it names (compared by equality against
  each live sensor parsed directly, because the two grids hold different prices), the other grid is
  never used when both exist, the **gas trap** is rejected, fixed single and day/night build the right
  slots on the local clock, a missing or `unknown` fixed price fails loudly, the warning matrix is
  right, and `explain_forecast` returns frozen slugs with hints that name something fixable.
* the frozen HTTP contract, 58 checks: `/api/v1/forecast` carries `provider`, `contract`, `notes`,
  `reason`, `hint` for a live forecast and for a missing one, `provider` is the configured provider
  while `source` names the concrete producer, a tax-inclusive source is used **verbatim** (the produced
  slots equal the payload's own `price_tax_included.amount ÷ 1e7`, so no adders are applied on top),
  and `/api/v1/config` advertises every new key with title-cased groups and `choice` rendered as
  `str` + `choices` - the exact shape the UI reads.
* the frontend helpers, under node against the shipped `app.js`: the empty panel prefers the API hint
  over the generic sentence and keeps the reason slug, and the provider/contract visibility table
  shows and hides exactly the fields it should (no entity field for `zonneplan`, bidding zone for
  `day_ahead`, day/night fields plus hours only for `fixed_day_night`), with every always-visible
  field present in every combination.

**What only real data showed, and what it changed:**

* the instance's **gas** tariff publishes the identical fixed-point shape, and its state agrees with
  its single 24 h slot, so the scale check does not reject it. Candidate selection therefore requires a
  kWh unit *and* at least two entries on a regular 15-minute or hourly grid. `ct/kWh` is accepted and
  converted; a 30-minute grid is not a contract grid and is rejected.
* **energy-charts publishes no prices for the NL zone.** Verified directly: `bzn=NL` answers HTTP 404
  "no content available" while `bzn=FR` answers 200 and an invalid code answers 400. So a valid zone
  can simply have no data; the 404 is turned into a hint naming the zone and pointing at the
  alternatives instead of a generic failure, and no third-party API was invented to paper over it.
* the day-window rule had to be defined for a window that crosses midnight (day start after night
  start) and for DST: the window is evaluated per slot in the configured timezone, not by counting
  hours.

**Mutation check on the new resolver** (tests run against a throwaway copy, mutation applied, test file
run, mutation reverted):

| Mutation | Result |
| --- | --- |
| `_select_grid` ignores the contract's preferred grid | **caught** (5 tests) |
| the fixed provider falls back to the market chain | **caught** |
| the regular-grid requirement removed (single daily slot accepted) | **caught** (after the gap below was closed) |
| the kWh-unit requirement removed | **not caught - equivalent mutant**, see below |

The two candidate filters turned out to be mutually redundant for the one gas fixture that existed, so
neither was actually pinned: removing either left the gas test green because the *other* filter still
rejected that sensor. Two tests were added, one per filter, each with a candidate that only that filter
can reject (a coherent state on a regular 15-minute grid whose unit is not in the kWh family; and a kWh
unit with a single 24-hour slot). The grid filter is now pinned. The unit filter remains an equivalent
mutant for any realistic input, because a non-kWh unit either fails to parse at all (`EUR/m³`) or is
already rejected by the state/slot scale check; the filter stays as defence in depth and the test
records the intent rather than pretending to pin an unobservable branch.

The mutation table in §3 is unchanged; the flakiness note there still applies to the whole suite.

---

## 7. The capacity-based solar estimate (0.4.0), checked against a real array

The instance's PV is AC-coupled: the battery's own PV input has never produced anything, the house
meter is where production appears, and no planes are configured - so the Forecast.Solar path could not
fill the solar panel at all. The request was to let the user state a watt peak and have the add-on
estimate production, with real production making it more accurate.

**Two independent implementations agree.** The model in SPEC 7.1.1 was implemented twice: once by the
worker, once by the orchestrator from the frozen spec text alone. On the instance's own coordinates
for 2026-10-08 both produce the *same numbers to the digit* - clear-sky peaks of 1823 W, 2025 W and
2228 W at 13:30 local (solar noon with DST) and 12.51, 13.90 and 15.29 kWh for 2700, 3000 and 3300 Wp.
Agreement between two implementations of the same text is evidence that the model was built as
specified, which "the tests pass" alone cannot give.

**Against the array's own history** (3 days of the AC-coupled production sensor, 5-minute samples):

* the measured day peaked at **1875 W at 12:45**; the model puts a 3000 Wp array at **2025 W at
  13:30** on that day - 8% above, which is what a clear-sky model must be on a partly cloudy day
  (an upper bound, never a lower one), and the peak hour is correct rather than fitted to the data;
* the measured peak implies an array of roughly **2777 Wp**, a plausible size for a residential
  string, so the model's magnitude is not off by a factor;
* the hourly profile of the clear-sky model correlates **0.903** with the measured production of that
  day over the eight hours they share.

**37 harness checks** against the shipped code (fixtures: the instance's `/api/config` and its
production history): night slots exactly zero; the peak lands near local solar noon; the model is an
upper bound on a cloudy measured day and within the same-window energy ballpark; south-facing beats
north-facing at the same capacity; explicit tilt/azimuth remove the assumption note; the cloud paths
(service response, flat current cover, clear-sky) each attenuate correctly and carry the note that
says which one applied; the learned per-hour correction from measured production scales the capacity
forecast while untouched hours stay at 1.0; a missing capacity yields an empty, explained panel rather
than a guess; and the HTTP payload carries the notes and a hint naming `solar.capacity_wp`.

**What only the live instance showed:** its weather entity supports *no* forecast type - all three
`weather.get_forecasts` types answer HTTP 400 "does not support action" - so cloud attenuation cannot
use a forecast there. That is not treated as an error: the current `cloud_coverage` is held flat with a
note, and clear-sky is the stated fallback when there is no coverage either. The service response
wrapper (`service_response`) and that 400 behaviour are recorded in `docs/EXTERNAL_APIS.md`.

**Mutation check on the new model** (throwaway copy, mutation applied, solar tests run, reverted):

| Mutation | Result |
| --- | --- |
| cloud attenuation ignored | **caught** |
| performance ratio ignored | **caught** |
| compass azimuth passed straight into a formula that measures from due south | **caught** |
| the "assumed south-facing, 35 degree tilt" note reworded to claim no assumption | **caught** |
| the missing-capacity error replaced by a guess | **caught** |
| the night gate removed (`elevation <= 0`) | **not caught - equivalent mutant**: two further guards already return 0 (a non-positive `cos_zenith`, and a `max(0.0, poa)` clamp), so no input distinguishes it |

Two defects were found by review rather than by a test, and both are recorded because they are the
kind that hides behind a green suite:

* **The whole capacity constant block was defined twice** in `sources/solar.py`. Python takes the last
  definition, so behaviour was correct and every test passed - but the dead copy masked a mutation
  (the reworded-assumption mutant initially survived because it edited the copy that had no effect).
  The duplicate was removed and the mutation re-run: it is now caught.
* **The frontend's mock fixture put capacity fields inside a `planes` entry**, which the frozen
  contract test correctly rejects (a plane has `kwp`/`tilt_deg`/`azimuth_deg`/`damping`). The mock was
  wrong, not the test: a capacity estimate models no planes, so the fixture now ships `planes: []` with
  the assumptions in its notes, and the contract test was *strengthened* to pin exactly that.

---

## 8. The interface rebuild (0.5.0), checked by rendering it headless

The request was blunt - the UI was "cluttered and not modern at all" - and it asked for a modern
minimalist look with *more* options available to whoever wants them. That second half was the real
defect: the settings form dropped any field that did not apply to the selected source **and** still
held its default value, so plenty of options simply did not exist on screen, at any point, for anyone.

**Framework.** Bootstrap 5.3.3, vendored as one CSS file and restyled through its own `--bs-*` tokens
by `style.css`. Tailwind was the alternative and was not chosen because it needs a build step, which
§13 forbids and which the add-on's offline, no-CDN, no-Node-at-runtime constraints have no room for.
The vendored file is byte-identical to the official dist (sha256 `3c8f27e6…736fc8`, 232803 bytes,
independently re-downloaded and compared, not just recorded).

**What the redesign guarantees, and the gate that holds it.** `tools/ui_render_check.js` (run by
`tests/test_wp3_ui_render.py`, skipped when node is absent) renders the real `app.js` and `mock.js`
through a dependency-free DOM shim (`tools/ui_dom_shim.js`, ~300 lines: elements, attributes,
`classList`, `dataset`, `isConnected`, closed-`<details>` semantics) and asserts **49** properties of
the result (42 at 0.7.0; §8.3 added the quiet-refresh pair, §8.4 the chart-layout five and §8.5 the
plan-grid pair). The
load-bearing ones:

* all seven views render from mock data with no `undefined` or `NaN` in the output;
* every setting the API sends is reachable: selecting each group through the rail and scanning the
  rendered rows finds all **76** keys (the **66** that apply to the current price/solar selection, plus
  the ten that are hidden-but-set and therefore still listed, muted, as "not used by the current
  selection");
* advanced rows are **not** shown by default, opening the disclosure shows them, and `Show all
  advanced` shows every applicable advanced key (**54** foldable keys exist);
* the 5 Safety rows are visible **without** unfolding anything;
* the four setup cards own the primary keys and render every one of them (the folding assertions run
  on the keys the cards do *not* own, since a card key is visible by design);
* a picker search lists discovery candidates and using one marks the setting dirty;
* the rail lists exactly the server's ten groups, the save bar exists, every class the front-end
  emits has a CSS rule, the `--chart-*` interface survived, no inline `style=` is emitted, no asset is
  external, Bootstrap loads before the design layer and the theme control pins and persists an
  explicit theme.

**The other gates this wave added**, each of which caught something real:

| Gate | What it catches | Found |
| --- | --- | --- |
| `test_mock_settings_cover_every_backend_setting` | mock/backend settings drift (the mock is the offline surface) | `control.stale_plan_seconds` was missing from the mock entirely - the exact "silently dropped option" this wave exists to stop |
| `test_mock_advanced_flags_match_the_frozen_classification` | the mock's fold flags drifting from `config.SETTINGS_META` | nothing after the fix; it pins the frozen §13.3 list in both directions |
| `test_vendored_bootstrap_matches_its_recorded_identity` | a truncated or hand-edited vendored stylesheet | nothing to fix; the file is pinned by the sha256 recorded in `vendor/README.md` |
| `test_index_loads_bootstrap_before_the_design_layer` | an override layer loaded before the framework it overrides | nothing to fix |
| `test_no_style_or_script_fetches_anything_at_runtime` | a CDN, webfont or remote image sneaking into a LAN-served UI | nothing to fix |
| `test_settings_groups_and_visibility_helpers` | the fold/visibility rules regressing (one group per key, Safety never folded, a search match always wins, a value keeps an inapplicable field listed) | nothing after the fix |
| the headless render check | a fold rule that exempts a key the payload marks advanced | `price.feed_in_entity` was rendered inline: a front-end safety regex (which exists to make money-moving keys ask for confirmation) was doing double duty as the fold rule, so a Price-group advanced key escaped the disclosure. Folding now follows the server group, as SPEC 13.2 says |
| the same check's class-coverage assertion | an emitted class with no CSS behind it | `.theme-state` had none (added) |

**One defect was found by the worker implementing it**, not by a test: `settingsField` treated the
API's `choices: []` as "this is a select", so every free-text setting rendered as an empty dropdown -
the form was unusable until that became `choices.length > 0`. It is worth recording that the new
render gate would **not** have caught it (an empty `<select>` is structurally valid); it was caught by
reading the diff against the data, which is why the diff review stays part of the process.

**The audit gate had to grow, carefully.** The vendored stylesheet is full of fractional measurements,
which is exactly the shape the R1 coordinate rule exists to catch, so the scanner reported 110
findings on it. Filtering by path would have created a hiding place; instead `tools/audit.sh` skips
the file *only while its bytes match a digest recorded in the vendor README*, names what it skipped in
its summary, and scans it normally the moment a byte changes. Both directions are tested (a verified
file is skipped; an edited file and a mismatched digest are scanned) along with the recorded digest
itself, so the exclusion cannot cover a payload.

**A stale-UI hazard found while deploying it.** The add-on served its own assets with
`Cache-Control: public, max-age=3600` and its asset URLs are unversioned, so after an in-place
Supervisor update a browser could keep rendering the previous release for up to an hour with nothing
indicating staleness - the exact "did my update apply?" trap, and one that no test in this repository
would have caught, because caching only exists between a browser and this server. Every asset now
revalidates (`no-cache`); the two static-serving tests assert it per asset rather than only for
`index.html`, which is where the old exception lived. The bug was found by reading the response
headers while checking the live deployment, not by the suite.

**What this does not prove.** There is no browser on this machine. The render check is structural: it
proves the DOM the front-end builds, not the pixels a person sees. Contrast was computed by the CSS
worker while writing the palette (>= 4.5:1 body text, >= 3:1 large text and UI), the tokens and the
accessibility rules are intact in source and asserted, and the layout is responsive by construction -
but spacing, the light palette in practice and the breakpoints have not been eyeballed here. The
click-through is the owner's, and it is the honest last step of this wave.

### 8.1 The setup cards, derived feed-in and meter-derived solar (0.6.0)

The 0.5.0 rebuild made every option reachable, but the default surface was still a rail of nine groups
with **55** always-visible rows (nine of them `control.*` tuning knobs that could never fold, because
they were grouped as Safety). The request was to *reduce* what a user has to decide: select a price
provider and fetch prices automatically, search and select the meter, optionally select a solar power
entity or estimate it from the meter, and search and add batteries - with everything else hidden but
still configurable.

The chosen shape (the user picked it from three options) is **four setup cards plus one "All settings"
drawer**: Price, Meter, Solar and Battery, each carrying only its primary choice, with the existing
rail, search, per-group `Advanced (n)` disclosures and save bar inside a collapsed drawer. Every key
is still rendered exactly once - a card key is skipped by its group, and while a search query is active
the cards are hidden so matches are listed once in the drawer. The essential set shrank from 55 rows to
**21**, and the `control.*` knobs moved to a foldable `Control` group (only `control.failsafe_on_error`
stays in `Safety`, which is now the five keys that can move hardware or change a hard limit).

Two behaviours were added to make the cards real rather than decorative:

* **Entity pickers** ask `/discover` and write the candidate row's `setting_key`, so picking an
  export-only grid sensor stores it in `grid.export_entity` instead of the card's `grid.power_entity`;
  `price`, `solar` and `load` discovery rows gained that key (additive) alongside `grid`/`battery`.
  A typed entity id can always be used directly, so a poorly ranked sensor is still reachable.
* **The sell price is derived now** (`sell = buy − price.export_fee_eur_per_kwh`, minus the slot's
  energy-tax component when `price.feed_in_energy_tax_refund` is off), because the user's contract
  pays the buy price back minus a feed-in cost. `PriceSlot` gained the additive `energy_tax` field:
  the Zonneplan payload publishes both sides per slot (their difference is the tax), wholesale sources
  carry the configured adder, and a consumer sensor carries 0.0 (unknown, stated in the note). An
  explicit flat tariff or feed-in entity still wins, and `optimizer.allow_export` still defaults to
  off. The user corrected an early misreading of this request - the toggle is the **energy tax**, not
  VAT - which is why the field is named `price.feed_in_energy_tax_refund`.
* **`solar.source = derived`** learns the array from the meter's export moments
  (`pv = export + battery net charge`), takes the 90th percentile of `pv / model-per-watt` as the
  cloudless envelope, and then runs the existing capacity model. The house load at those moments is not
  subtracted (that load is what the PV forecast is for), so the learned size is a conservative lower
  bound and the forecast says so. An explicit `solar.capacity_wp` wins; no export history, fewer than
  24 usable daylight samples or an unmappable battery flow is a status, never a crash.

What the gates prove here: the render check (49 properties at 0.8.1) asserts the cards, their key
ownership, a real picker round-trip against the mock and the chart layout contract of §8.4; `test_setup_card_keys_are_the_primary_surface` pins the pure
key table per provider/contract/solar source; `test_wp2a_price.py` covers the derived sell price, the
tax-refund switch and the per-slot tax extraction; `test_wp2a_solar.py` covers the envelope fit, the
export-derived samples, the explicit-watt-peak override and each refusal path; and the frozen
classification tests (`test_wp2a_config`, `test_wp2b_api`, `test_wp3_mock_contract`, `test_wp5_api_contract`,
`app.main --selfcheck`) were updated in both directions so the new list cannot drift.

Not verified: the pickers have never been clicked in a real browser (the ingress panel needs the
owner's session, and there is no browser on this machine), and the derived solar fit has not been
compared against a full season of a real array - only the synthetic model-recovery tests and the
existing live-instance history captured in §7.

### 8.2 Derived wear, discharge-only charging and NASA POWER clearness (0.7.0)

The review that triggered this wave came from the owner: "the pricing model uses both discharge
efficiency and round-trip efficiency", "let the user put in the cycle life of the battery and price
and calculate the wear from that", and "does the solar capacity calculation take into account the
solar irradiation data … at the specific time". The answers and the changes, in order:

* **Solar.** The capacity model already used the home's coordinates and the exact 15-minute slot time
  for its clear-sky irradiance (sun position, Haurwitz GHI, transposition) plus cloud coverage from
  `solar.weather_entity`, but no measured or forecast irradiance data. NASA POWER's observed hourly
  all-sky/clear-sky irradiance (keyless, W/m², ~2-3 day latency) is now reduced to a per-hour-of-day
  clearness index over 21 days and applied where no cloud coverage exists, and in the derived
  watt-peak fit. It is additive: cloud coverage and the measured-PV bias keep priority, and
  `forecast_solar` is untouched.
* **Wear.** `BatteryConfig` gained `price_eur` and `cycle_life`; `wear_eur_per_kwh =
  price / (cycle_life × usable_kwh × eta_discharge)` per delivered kWh (the eta divisor makes one
  full discharge cost exactly `price / cycle_life`; an independent review caught the missing term),
  delivered-energy-weighted across the fleet, with `optimizer.cycle_cost_eur_per_kwh` kept as an
  advanced override (the owner chose those two options explicitly).
* **Break-even.** `buy / round_trip + wear`, no per-direction term, and the DP charges wear on the
  discharge leg only (`wear × eta_dis × max(0, e_i − e_{i+1})`).
* **NASA robustness (review follow-up).** A failed fetch is remembered for 3 hours so an unreachable
  NASA costs one request rather than one per refresh, the local timezone is part of the profile's
  cache identity (the buckets are local hours), the profile cache rejects a future timestamp, and
  cache I/O failures are a miss, never a failed forecast. Two review findings are accepted as
  documented approximations: the DP's single delivered-energy-weighted fleet wear, and the derived
  fit scaling every historical export sample by the recent 21-day clearness median rather than the
  weather of its own date. The learned per-hour bias still applies on top of the NASA attenuation;
  it was fitted against the NASA-free model and self-corrects within days (SPEC §7.1.1a).

What the gates prove: `test_wp2a_solar.py` covers the NASA fetch shape, the fill value, the
per-hour medians (including the no-evidence default of 1.0), the weather-coverage skip (also in a
non-UTC zone), the failure path and backoff, cache keying and cache-I/O tolerance, and the derived
fit with and without clearness; `test_wp1_external_contracts.py` pins the UTC
stamp shape and the clearness definition; `test_wp2a_config.py` covers the derivation, the zero-input
cases and validation (including NaN/inf); `test_wp2b_efficiency.py` and `test_wp5_optimizer_oracle.py`
recompute the discharge-only objective independently and assert DP == brute force;
`test_wp3_assets.py` and `test_wp3_mock_contract.py` pin the UI maths and the mock contract. Two
adversarial checks were not
run this wave (no independent audit), and the NASA clearness has not been compared against a
pyranometer: the synthetic recovery tests and the live probe of the API shape are what exists.

**Review follow-up (2026-10-11).** Before the first deployment an independent review (medium tier,
separate session) was run on this wave. It found the missing `eta_discharge` in the wear divisor
(fixed, with a full-cycle test), a coverage computation that asked NASA even when the weather
forecast covered every hour of a non-UTC horizon (fixed), and NASA cache I/O outside the exception
handler (fixed). Its remaining findings are either pre-existing or accepted approximations, and are
listed in the bullet above and SPEC §7.1.1a/§7.1.2 rather than silently dropped.

### 8.3 Review-driven interface rework (0.8.0)

An external UI/UX review of the shipped 0.7.0 interface (`docs/UI_REVIEW.md`, 12 findings: 6 High,
5 Medium, 1 Low) was implemented in full; §0 of that document maps each finding to the code that
answers it. The load-bearing changes:

* **Answer-first views.** Today leads with the control authority, the measured battery behaviour and
  the planned action; Plan leads with the saving/loss against the baseline; Forecasts lead with the
  measured error and freshness; Savings leads with the comparable saving and its baseline coverage
  ("Baseline available for N of M periods"); Settings leads with a persistent Safety summary; Log
  leads with the dispatch outcome. Worked examples, raw capabilities, mappings and full notes moved
  behind named `<details>` disclosures.
* **One authority policy.** Auto and dry-run-off share one confirmation implementation and one
  explanation of the dispatcher/Stop distinction across Today and Settings; the Today mode buttons
  are built once outside the re-rendered region, so a poll can no longer swallow the first click, and
  a refused or cancelled dry-run change restores the server's confirmed state.
* **Dependency-complete setup cards.** The fixed day/night window hours, the day-ahead VAT/energy-tax/
  import-fee adders, `solar.capacity_wp` and `solar.planes` now sit on the card that selects the
  option. The Battery card and the `batteries` settings row read the canonical
  `GET /api/v1/batteries` inventory instead of the settings descriptor (open question 3 of the
  review); the descriptor is shown as read-only provenance.
* **Quiet refresh.** Navigation and data refresh are separated: a route change focuses the route
  `h1`, while polls call a view's `state.refresh()` hook and update in place, restoring focus.
  `refreshBlocked()` blocks every background refresh while a dialog is open, a write is in flight,
  the settings form is dirty or a stop result is on screen; `once()` clears a rejected cache so
  `Retry` re-requests. `renderInto()` now clears its host before drawing — without that fix each poll
  appended a second copy of the answer, which the render check now pins.
* **Truthfulness of numbers and state.** Forecast PV/load power is labelled kW (`fmtKw`), MAPE/MAE
  use the metric's own unit, daily totals are never zero-filled, Loss/Unknown/Stale are separate
  visible flags, the availability badges are flattened (they had stringified to `[object Object]`),
  and prose table columns are declared as such so only numeric columns right-align.
* **Visual and accessibility pass.** One route title and one surface per panel, borderless metric
  cards, untinted panel heads, a token type scale and 4/8/16/24/32 spacing scale; `--text-muted`
  carries meaningful metadata with contrast measured ≥4.5:1 in both themes; ≥44 px targets;
  `Skip to content` focuses `#view` without changing the route hash. A browser without
  `showModal` fails closed for checkbox-required confirms instead of resolving a bare
  `window.confirm`.
* **Charts.** Stable non-colour series signatures (solid / long dash / dotted) named in words in the
  legend, narrower axis gutters below ~480 px that keep their units, and `SEMCharts.refresh()` on an
  explicit theme change (previously only `prefers-color-scheme` changes refreshed a mounted chart).

What the gates prove: `node tools/ui_render_check.js` asserts 49 properties, including that the
dashboard answer renders exactly once and survives a visibility-triggered quiet refresh; the pytest
suite (634) holds the frozen contracts, the settings-IA pure tables and the copy rules.
`docs/UI.md` and `docs/REFERENCE.md` §2 were updated in the same change, and
`energy_manager/CHANGELOG.md` carries the user-facing entry.

Not verified (the same limit as every wave: the ingress panel needs the owner's browser session, and
this machine has none): the live HA layout above the fold at 360 px and 200 % zoom, real uPlot legend,
cursor and touch behaviour, and what assistive technology announces for route focus, repeated live
regions and sticky-bar occlusion. Those remain the review's §8 open questions 1, 2 and 7.

### 8.4 Chart legend layout and reading copy (0.8.1)

Reported from the owner's phone: on the Plan page the chart legend was drawn on top of the paragraph
underneath it, and the two overlapped into unreadable text. Cause: `charts.js` pinned the chart host
to a fixed pixel height and passed `opts.height`, so uPlot's legend table — which uPlot builds inside
the plot root — had nowhere to go and overflowed the box onto the next element.

The chart is now three flow siblings inside its host:

1. `.chart-plot` — the canvas at its explicit pixel height;
2. `.chart-legend` — uPlot's legend table, mounted there through `legend.mount` instead of inside the
   plot box, styled as one wrapping row of entries at `--type-12`;
3. `.chart-caption` — one short line saying what is plotted, on which axis, and what each line
   signature means (the per-chart paragraphs that duplicated the legend are gone; the two-sentence
   chart explanation on the Plan page is replaced by a single sentence about the break-even line, and
   the worked maths stays in "Why this plan?").

Alongside it, the reading defects the same screenshot showed:

* **Axis ticks** carry one fixed precision and their unit: percent axes read `25 %`…`100 %` (they used
  to mix `100` with `75.00`) and their rotated label is dropped, since the ticks already say it; the
  narrow-host gutter grows to 46 px only when a rotated label is still drawn.
* **Series names** avoid colour words and state their signature: "Stale price slot", "Charge level",
  "Upper uncertainty (long dashes)", "Lower uncertainty (dotted)", "Load P90 (long dashes)".
* **Plainer sentences**: "Discharge · 700 W" (not "−700 W discharge") in both the action windows and
  the Today answer strip; a one-slot action window shows a single time and a multi-slot window ends
  when its last slot ends (`06:45 PM – 07:15 PM`); plan summary rows are `Estimated net cost`,
  `Baseline net cost (no battery)`, `Saving vs baseline`, `Energy charged`, `Energy discharged`; the
  stale-price banner says what staleness does to the plan; `timeAgo()` says "just now" under 5 s
  instead of "0 s ago"; `fmtDuration()` drops a zero remainder ("7 min", not "7 min 0 s"); and
  "1 period has no baseline" instead of "1 period(s)".

New gate: `tools/ui_render_check.js` §4 installs a minimal `uPlot` stub (root element, legend table,
`legend.mount`) and asserts that each chart has a plot box with an explicit height, that the legend is
mounted **outside** that box and immediately after it, and that every chart carries a caption under
240 characters. That is a structural check, not a pixel check — the browser still decides the optical
result, and the review's open questions 1, 2 and 7 (live HA layout at 360 px and zoom, real uPlot
legend/cursor behaviour, screen-reader announcements) remain the not-verified list.

The shim itself gained `element.style` as a real CSS declaration (property writes land in the
serialized style attribute), `contains()`, and `previousElementSibling`/`nextElementSibling`, so the
stub can exercise code paths the browser runs.

---

### 8.5 The plan grid defaults to the tariff's own slot length (0.8.2)

`optimizer.slot_minutes` shipped as 15 minutes while the price feeds a household actually gets are
hourly (the Zonneplan sensor's own list is hourly; `dynamic_hourly`). The optimiser is fed by
`align_slots` onto the plan grid, so on a 15-minute grid each hour had one slot filled from a real
entry and three forward-filled and flagged `stale` — and a stale slot is treated as a flat price
(SPEC 4). The default is now **60 minutes**: 30 fresh slots over the 30 h horizon with an hourly feed.

That change alone was not enough, and the shipped tests said so. Two gates failed
(`test_a_plan_computed_without_a_live_soc_is_withheld_until_the_first_read`, `test_every_mode_boots_
serves_and_writes_only_when_live[auto-False-True]`): the plan grid is floored to its own step, so at
16:50 the plan starts at 16:00 while the price grid started at the current quarter (16:45). The
plan's first slot — the one the dispatcher acts on — was therefore *backward-filled* and stale, and
`dispatcher._stale_input_reason` refuses to execute a stale current slot (audit finding 2). With an
hourly tariff on a 15-minute plan grid that blocked the battery for three quarters of every hour; it
is a blocked *dispatch*, not merely a weaker plan. So:

1. `sources/price.py::_grid_start` starts every provider's price grid at the plan slot containing
   `now` (`quarter_floor(now, optimizer.slot_minutes)`); the current plan slot always matches a real
   provider entry, and a quarter-hourly feed is unchanged (its floor is still the current quarter).
   This also fixes `sensor.sem_price_now` reporting the *next* hour's price during the last quarter
   of the hour, and the `load_price_forecast` acceptance window deliberately stays on the current
   quarter so a provider must still have *future* coverage.
2. `_apply_coverage` measures coverage with the **fresh** slots' own cadence. It previously compared
   the last fresh slot's *start* with `horizon_end`, and `slot_range` excludes its end, so every
   fully covered forecast was `degraded` with a note claiming stale slots that were fresh.

New gates (both in `tests/test_wp2a_price.py`):
`test_the_price_grid_starts_at_the_plan_slot_so_the_current_slot_is_never_stale` (hourly feed at
16:50: the grid starts at 16:00, that slot is fresh, the 60-minute plan's current slot is fresh, no
plan slot is stale, and a 15-minute plan grid still starts at 16:45) and
`test_a_fully_covered_forecast_is_not_reported_as_a_short_tail` (covered → `degraded is False`, no
"coverage ends" note; 12 h of coverage → still stale/degraded).
`tests/test_wp2b_optimizer.py::test_the_plan_grid_defaults_to_the_price_resolution` pins the three
copies of the default (dataclass, `from_settings` fallback, `config.DEFAULTS`) and the 5…60 bounds.

Not verified here: the live instance's plan payload after the change (the add-on is ingress-only, so
only the owner's browser sees it; `sensor.sem_*` does not publish slot counts). The engine's slot
lifecycle, the dispatcher's per-slot refusal and the provider resampling are covered by the suite.

### 8.6 The Energy-dashboard grid mapping and long-term statistics history (0.9.0)

The live instance's Energy dashboard already named the house grid meter (the Connect Energy meter's
consumption and production sensors, plus Home Assistant's generated signed net-power sensor and the
Zonneplan import/export counters), but the add-on's `grid.*` settings were empty, so the derived
load and solar tiers had no meter - and with `solar.planes` empty the solar panel showed
`no_pv_source`. The request: read the grid import/export from Home Assistant's energy section by
default, which also brings history for a better solar estimate.

The payload facts were verified against the live instance (2026-10-11) with the `hassio` CLI
(`energy/get_prefs`, `recorder/get_statistics_metadata`, `recorder/statistics_during_period`):

* `energy/get_prefs` returned one grid source with `stat_energy_from`/`stat_energy_to` (the
  Zonneplan today counters), a `power_config` with `stat_rate_from`/`stat_rate_to` (the meter's
  consumption/production sensors) and Home Assistant's generated `stat_rate` net-power sensor, whose
  sign already matched `grid.power_entity` (positive = import; live state -147 W while production was
  147 W).
* `recorder/get_statistics_metadata` classified a live power statistic as `unit_class: power`,
  `has_mean: true`, unit W, and a kWh counter as `unit_class: energy`, `has_sum: true` - the two
  shapes the history reader branches on.
* Hourly long-term statistics existed back to 2026-08-01 while `GET /api/history/period` from
  2026-09-25 was already empty (the recorder's default purge), which is what makes statistics the
  history source rather than the recorder.

Three implementation changes, all covered by new tests:

1. `app/grid.py` (new) parses the prefs into role slugs - the generated signed power sensor or the
   two-sensor `power_config` for live power, the import/export energy counters for history - with an
   explicit `grid.*` setting winning as a whole and a 300 s cache that caches failures too.
   `HAWebSocket.energy_prefs` and `HAWebSocket.statistics_metadata` wrap the two commands.
2. `sources/load.py` reads each derived series from long-term statistics first (hourly `mean` for a
   power statistic, hourly `change` for an energy counter, converted to average kW over the row's own
   `start`/`end`), with recorder history as the fallback; the derived load tier and
   `collect_pv_export_samples` share that path.
3. `sources/solar.py` runs the meter-derived estimate when `solar.source=forecast_solar` has no
   planes, and the engine's `no_pv_source` hint names the derived failure
   (`status.detail.derived_fallback`) when no meter resolves either.

New tests: `tests/test_wp2a_grid.py` (13 tests: the prefs shapes, explicit-settings precedence, the
energy-counter fallback, the TTL cache and failed-read caching), the statistics/energy-counter tests
in `tests/test_wp2a_load.py` (7), the statistics-fit and no-planes-fallback tests in
`tests/test_wp2a_solar.py` (4), the engine fallback/hint tests in `tests/test_wp2b_engine.py` (2),
the wiring test in `tests/test_wp2b_integration.py` (1) and the two WebSocket wrappers in
`tests/test_wp2a_haws.py` (2). The suite went from 634 to 663 passing tests; the counts in §1 are the
frozen-tree rerun.

Not verified here: the add-on's own read through the container's `SUPERVISOR_TOKEN` (the live checks
used the CLI's token; neither command is admin-gated per the Core source, so the supervisor token is
more than sufficient), and the rendered UI after the change (ingress-only, owner's browser). The
prefs cache, the precedence order and the conversion are asserted with fakes; the live payload shapes
are the CLI observations above.

---

## 9. R1 scanner (`tools/audit.sh`)

It is a **tripwire, not a proof**. The committed tree is clean under it, and it catches:

* coordinates with 5 or more fractional digits;
* unlabelled 4-decimal latitude/longitude pairs;
* labelled 4-decimal coordinates (a location field on the same line);
* private / instance hostnames, including the FRITZ!Box default domain and `homeassistant` used as a
  host (a port or a URL authority);
* colon-separated MAC addresses;
* bare 32-character hex identifiers (the device-id / unique-id shape without quotes);
* quoted serial and unique-id values;
* bearer / JWT credential shapes;
* email addresses.

One file is skipped, and only conditionally: the vendored Bootstrap stylesheet, whose minified body
is full of fractional measurements and therefore trips the coordinate rule 110 times. It is skipped
*only while its bytes match a sha256 recorded in `vendor/README.md`*, the scan summary names what it
skipped, and the file is scanned like any other the moment a byte changes (both directions are
tested). A path-based exclusion would have been a hiding place; this one cannot hide a payload.

Documented ceilings, from the script header: MAC addresses written with hyphens, dots or no
separator; IPv6 literals; public dynamic-DNS hostnames; 3-decimal coordinate pairs; and
negative-only mixed-length pairs are **not** caught. The header also names the accepted false
positives. The scanner prints at most four characters of any match, so a scan never re-prints a
value that may be sensitive.

---

## 10. What is not verified

* **No real battery or Zendure hardware.** Driver behaviour is asserted against the documented
  integration semantics and a fake over loopback, not observed on a device.
* **No live price, PV or load provider calls.** The fallback chain and rate-limit handling were
  exercised with injected fakes; real payloads, headers and timezone edge cases remain unverified.
* **The add-on's own Energy-dashboard read.** The `energy/get_prefs` and
  `recorder/get_statistics_metadata` payload shapes were verified live with the `hassio` CLI, but the
  in-container path over the Supervisor token and the 300 s cache are asserted with fakes; the UI is
  ingress-only and was not clicked through remotely.
* **No behaviour on real hardware.** The add-on *is* installed and running on the owner's Home
  Assistant in advisory mode with `dry_run: true`, but no battery, price or solar entity is mapped
  yet, so nothing here proves it plans correctly on that instance. The packaged image's entrypoint was
  verified both ways from a locally built image (without a token it exits 1 with the clear message,
  with one it execs the app), and the two defects the real install did find are recorded in §2.
* **Threaded timing** is asserted with deterministic barriers, not wall-clock races.
* **A persisted plan is never restored**, so the stored plan summary's efficiency fields matter only
  on the API path — no on-disk plan is resumed after a restart.
* **The picker has not been clicked through in a real browser** by this author: its pure helpers are
  exercised in node and a stub DOM was driven once during development, but the suite contains no DOM
  test and no human click-through is recorded here. The live-registry check in §5 replaces what a
  synthetic fixture cannot provide, not what a browser would.
* **Saving a battery after a *Select*** (`POST /api/v1/batteries`) is covered by the API contract
  tests but has not been exercised against the live instance; nothing was added to the running
  add-on's configuration during this work.
* **The redesigned interface has been checked structurally, not visually** (see §8): no screenshot,
  no contrast measurement on rendered pixels, no real device at 360 px.
* **No clear-day validation of the capacity model.** Its clear-sky peak was checked against an almanac
  (sunrise/sunset and peak heights are physical) and against a *partly cloudy* measured day, where it
  correctly sits above the measurement; no measured clear day was available in the three days of
  history on the instance, so the model's absolute calibration rests on the model itself plus whatever
  the learned per-hour correction absorbs.
* **No real energy-charts payload was parsed.** The provider path is covered by tests, and the live
  endpoint was probed for its status codes (200/400/404), but no successful response body was seen
  from this environment: the covered zones answered 429 (rate limited) during the probe and the
  configured zone (NL) answers 404. Its parsing is therefore asserted against the documented shape
  only.
* **The fixed tariff's entity path was only exercised with missing values.** The instance's own
  day/night price sensors report `unknown`, which is exactly the case that must fail loudly and does;
  a real pair of day/night sensor values was not observed, and neither was a live tariff whose day
  window crosses midnight.
* **The provider resolution has not been exercised inside the deployed add-on.** Everything above runs
  the shipped code against captured live payloads; the running instance's settings were not touched,
  so which provider it uses is still the owner's choice to make in the UI.

Known behaviour, accepted and deliberate:

* the safety path still blocks 0 W when the base load alone exceeds a configured grid cap
  (conservative: it never worsens the grid flow, it simply refuses to act);
* the HTTP API binds `0.0.0.0` and is unauthenticated by design, because ingress supplies the login;
  `config.yaml` publishes no port and 8099 must not be mapped.

---

## 11. How to reproduce

No network access and no hardware are needed for any command below.

```sh
# 1. Syntax / imports
python3 -m compileall -q energy_manager/app tools/make_icons.py tests

# 2. Full test suite (expect: 663 passed; needs pytest and PyYAML, which only the manifest test
#    uses — the add-on itself imports nothing outside the standard library)
python3 -m pytest tests -q

# 3. R1 tripwire (expect: "audit: clean — 93 files scanned", exit 0)
sh tools/audit.sh

# 4. End-to-end selfcheck (expect: 31/31 checks passed, exit 0)
PYTHONPATH=energy_manager python3 -m app.main --selfcheck

# 5. Render the front-end headless and check its structure (expect: "0 failed", exit 0; also run by
#    tests/test_wp3_ui_render.py, which skips when node is absent)
node tools/ui_render_check.js
```

Frozen digests — manifest digests over the file sets the two recipes select. Both stages are sorted
under `LC_ALL=C`, which makes the value independent of `find` order and of the locale (a single `sort`
produces a different value). The `wc -l` count is the cross-check (94 and 79).

```sh
# all files (94)
find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
        -o -name node_modules -o -name .mypy_cache \) -prune -o -type f -print \
  | LC_ALL=C sort | xargs sha256sum | LC_ALL=C sort | sha256sum

# code + tests only (79), documentation excluded
find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
        -o -name node_modules -o -name .mypy_cache -o -name '*.md' -o -name 'LICENSE*' \) -prune \
     -o -type f -print \
  | LC_ALL=C sort | xargs sha256sum | LC_ALL=C sort | sha256sum

# code + tests path list only (the second digest in section 1)
find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
        -o -name node_modules -o -name .mypy_cache -o -name '*.md' -o -name 'LICENSE*' \) -prune \
     -o -type f -print \
  | LC_ALL=C sort | sha256sum
```

To check the selected file count on its own, run the same `find … -print | LC_ALL=C sort | wc -l`
part of either recipe: it must print 94 (all files) or 79 (code + tests only).
