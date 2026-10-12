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
| Test suite | `python3 -m pytest tests -q` | **694 passed** (two consecutive runs) |
| PII / secret scanner | `sh tools/audit.sh` | `audit: clean — 93 files scanned`, exit 0 |
| Add-on selfcheck | `PYTHONPATH=energy_manager python3 -m app.main --selfcheck` | **33/33 checks passed**, exit 0 |
| Syntax / imports | `python3 -m compileall -q energy_manager/app tools/make_icons.py tests` | clean, exit 0 |
| Headless UI render check | `node tools/ui_render_check.js` | **78 passed, 0 failed**, exit 0 |

Frozen digest of the verified artifact (recipe in §11, normative):

* **code + tests (79 files, documentation excluded)**:
  `29a20d627034d1634d0d529b3ba1aa2d1ad1eaab3db17d335fbff5582bd2c566`
* **path list of the same 79 files** (detects an added, removed or renamed file):
  `999c72a0ad237efad84631e1c196871fbab19aaf4e46b1e3e55f351c7c988ad5`

The 0.10.1 tree's code + tests digest was `28fdad8a…`, 0.10.0's `784dbf04…` and 0.9.1's
`fc5f9d44…`; 0.11.0 (§8.10) changed file contents only - it added no file, so the path-list digest
above is the same one, which is exactly what that recipe is for.

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
`tests/test_wp2a_haws.py` (2). The suite went from 634 to 663 passing tests at that point (668 after
§8.7); the counts in §1 are the frozen-tree rerun.

Not verified here: the add-on's own read through the container's `SUPERVISOR_TOKEN` (the live checks
used the CLI's token; neither command is admin-gated per the Core source, so the supervisor token is
more than sufficient), and the rendered UI after the change (ingress-only, owner's browser). The
prefs cache, the precedence order and the conversion are asserted with fakes; the live payload shapes
are the CLI observations above.

### 8.7 Meter-card grid provenance (0.9.1)

0.9.0 resolved the meter at runtime, but the settings panel showed only the (empty)
`grid.power_entity` field: a saved value silently disabled the fallback and could not be cleared from
the UI. Requested after the 0.9.0 deploy: "the meter info is still not coming from ha energy section".

* `GridMapping.dashboard_roles()` exposes the Energy dashboard's own roles even when an explicit
  `grid.*` setting overrides them; `Engine.grid_resolution()` combines them with the explicit keys
  and the effective source, and `GET /api/v1/config` carries the result as the additive `grid` object
  (`source`, `note`, `explicit`, `dashboard`).
* The Meter card renders it: with no `grid.*` set it names the resolved roles ("From the Home
  Assistant Energy dashboard: signed power …; import counter …; export counter …"), a saved or
  edited value is labelled as an override with the dashboard's roles beside it, and `Use the Energy
  dashboard` clears `grid.power_entity`, `grid.import_entity` and `grid.export_entity` through the
  normal dirty/Save flow (the read itself never writes a setting).
* The headless render check exercises both branches against the mock's dashboard roles and asserts
  the clear button restores the fallback text (54 checks now, was 49).

New tests: `tests/test_wp2a_grid.py` (+1, `dashboard_roles` under an override), `tests/test_wp2b_engine.py`
(+2, `grid_resolution` with and without a mapping), `tests/test_wp2b_api.py` (+2, the `grid` payload and
the older-engine `{}` fallback) and the updated route-contract assertion in
`tests/test_wp5_api_contract.py`. The suite went from 663 to 668 passing tests.

### 8.8 Per-window plan evidence: measured, dispatched, priced (0.10.0)

Requested from a phone screenshot of the 0.9.0 **Next actions** list: "enrich this section with more
data about that time slot". The list rendered time, action, power, reason and a `stale price` badge,
while the payload already carried the prices, the forecast and the SoC path, and the store already
held the measured rows and the dispatch log. Two defects surfaced while wiring it:

* **The plan's own resolution never reached the interface.** `plan_dict` omitted `slot_minutes`, so
  `planSlotMinutes()` fell back to 60 for every plan: a 15-minute plan was captioned as hourly, the
  window end was up to an hour wrong and `slotAt()` looked the "now" slot up on a 60-minute grid.
  The mock set the field, which is why no test caught it. `slot_minutes` is now part of the `/plan`
  contract (`PLAN_FIELDS`, the mock contract and the selfcheck all assert it).
* **A stale slot looked scheduled.** The dispatcher refuses a slot whose price is not fresh
  (`_stale_input_reason`), but the list showed the plan power and a badge, so a blocked window read
  like a pending one. With an hourly tariff on a 15-minute plan grid that is three quarters of every
  hour silently blocked.

What 0.10.0 adds:

* `GET /api/v1/plan/execution` (additive, SPEC 10): the plan's slot grid joined with the persisted
  5-minute `actuals` rows and the `dispatch_log`. Per slot, `dispatch` is the last attempt per battery
  (`requested_w`, `applied_w`, `ok`, `dry_run`, `clamped`, `code`, `reason`, `records`, `batteries[]`)
  and `actual` the measured aggregate (`samples`, `coverage_pct`, `pv_kw`, `load_kw`, `grid_kw`,
  `battery_kw`, `planned_kw`, `measured_kw`, `import_kwh`, `export_kwh`, `charged_kwh`,
  `discharged_kwh`, `soc_kwh_start`, `soc_kwh_end`). A slot nothing reached carries `null` for both -
  never a fabricated zero - and `progress` reports the plan so far (intended vs measured kWh and grid
  cost, `slots_elapsed`, `slots_measured`, `coverage_pct`). `floors.floor_soc_pct` is
  `max(optimizer.reserve_soc_pct, min over enabled batteries)`.
* `analytics.measured_per_slot()` (pure): rows deduplicated by `ts` (last wins, like the store's
  upsert), every surviving row standing for its own 5 measured minutes, energies returned as
  magnitudes and `coverage_pct` stating how much of the slot was actually observed.
* `Engine.plan_execution()` reads the dispatch log from the store (the whole horizon) and falls back
  to the in-memory journal ring; `_execution_progress()` counts a fully elapsed slot whole and the
  slot in progress by its measured share, so both sides cover the same span.
* The window rows now state the prices (`Buy → sell` in ct/kWh with the spread), the break-even and
  the margin over it, the planned kWh in/out, the SoC path and the floor, the planned grid flow and
  what it costs or earns, the provider's age and the window's rank - plus the execution line
  ("applied 400 W of 800 W requested (clamped: …)", "measured -0.70 kW vs -0.80 kW planned, 1 sample
  over 15 of 60 min", "not dispatchable: this slot has no fresh price (stale_input)"). One summary
  line above the list adds the windows up. The Plan view installs `state.refresh`, so returning to the
  tab re-reads the plan, the join and the providers in place; `asyncPanel.reload()` redraws a panel
  without flashing "Loading…".

Gates: the suite went from 668 to **675 passing** (analytics +2, engine +3, the API route contract +1,
the mock contract +1), the headless render check from 54 to **75 checks, 0 failed** (21 new, of which
12 are the load-bearing ones `tests/test_wp3_ui_render.py` now requires), the selfcheck from 31 to
**33/33** (the new route and the slot-grid join), `tools/audit.sh` still clean over 93 files, and
`compileall` clean. The new tests are
`tests/test_wp2b_analytics.py::test_measured_per_slot_uses_five_minute_buckets`,
`::test_measured_per_slot_ignores_unusable_rows_and_unreachable_slots`,
`tests/test_wp2b_engine.py::test_plan_execution_joins_measured_actuals_and_dispatches`,
`::test_plan_execution_progress_counts_only_elapsed_slots`,
`::test_plan_execution_is_empty_without_a_plan_or_a_store`,
`tests/test_wp5_api_contract.py::test_plan_execution_route_reports_measured_and_applied` and
`tests/test_wp3_mock_contract.py::test_plan_execution_payload_matches_section_10`.

Honest limits: a plan's slots start at the slot containing `now`, so only the current slot (and any
fully elapsed slot when the plan is older than one slot) can carry measurements - the join is not a
history of past plans. The per-window money is the plan's own cost model (`load − pv + power` at the
slot's tariff), and the measured side re-prices the measured import/export at the same tariff, so the
two are comparable but neither is a bill. `clamped` uses a 1 W tolerance (`CLAMP_TOLERANCE_W`), so an
idempotent re-write is not reported as a clamp.

### 8.9 Direct inverter control and the "commanded" dispatch state (0.10.1)

Reported from the owner's phone: "We have charging planned now but it is not charging" - the Today
view said `Planned now: charge 1.00 kW` while the pack reported 9 % SoC, 0 W and `idle`. The live
instance said why, in three layers:

* **The command was revoked before it could land.** The manager's `manual_power` number was written
  to -1000 W and to 0 W 4.6 s later, every 30 s, for hours: the driver confirmed a non-zero command
  only from the *measured* AC flow inside a 4-attempt / 1.5 s window (`readback_attempts`,
  `readback_delay_s`), and a miss was a `driver_error`, which `control.failsafe_on_error` turned into
  a fail-safe hold of 0 W. The Zendure manager applies `manual_power` **only** on its next P1-meter
  event (`manager.py _p1_changed`, throttled >=2.2 s), so the window was decided before the manager
  ever looked; the device's own `input_limit` reached 1000 W exactly once in twenty minutes and went
  back to 0 five seconds later. `grid_input_power` had not moved since 2026-10-11 12:34 - the pack had
  not charged at all, and the previous day's charging was the manager's own smart mode, before
  `operation` was switched to `manual`.
* **The house P1 feed is minutes away.** The only grid entity Home Assistant sees is the Zonneplan
  Connect Energy Meter, which `zonneplan_one` reports every five minutes (and whose reading is itself
  2-5 min old); the manager's `operation_state` flips land exactly on those `:xx:20` marks. A manual
  setpoint therefore cannot be applied - or revoked - faster than that, which makes the manager path
  structurally wrong for this house regardless of the read-back window.
* **The inverter's own entities do not wait for any of it.** The device's `ac_mode` select and its
  `input_limit` / `output_limit` numbers are written by the integration over the local
  zenSDK/MQTT link (`doCommand -> httpPost('properties/write')`) and echoed back on the device's own
  report cadence (measured on the live SF800 Plus: a property report every ~5 s).
  `input_limit = 1000` with `ac_mode = input` is a charge command the device applies itself.

What 0.10.1 changes:

* `BatteryConfig.control_path` (`auto` | `manager` | `device`, default `auto`) plus
  `ZendureDriver.validate_control_path`, wired into `driver_registry.validate_config`, so
  `POST`/`PATCH /api/v1/batteries` refuses a path that lacks its entities at save time (SPEC 5).
  `auto` keeps every existing configuration on exactly the path it used before.
* The **device** path: `_manager_quiet_write()` (the manager's `operation` select to its `off` value,
  written only when it is not already there - while it is not `off` it re-writes both limits on every
  P1 event), `_ac_mode_write()` (direction select; the option string is read from the entity's own
  `options`, because the integration localises the labels), then the two magnitude writes. A hold
  zeroes both limits and leaves the direction alone.
* `Reason.COMMANDED` + `_flow_pending`: a command whose setpoint landed but whose effect the device
  has not shown is reported as in force but unconfirmed (`applied_w` = the live setpoint,
  `readback_w` = what the device reports, message naming both). `driver_error` - and therefore the
  fail-safe hold - is now reserved for a setpoint that does not read back at all and for a manager
  that cannot be held in `manual`. `_verify_device_limits()` treats a matching `input_limit`/
  `output_limit` read-back as device evidence on its own (those entities are device reports, not our
  own echo) and falls back to the measured flow.
* Interface: a **Control path** select in the battery dialog with the trade-off spelled out, a
  `Dispatch` line on the battery card (`capabilities.control_path` plus whether it was chosen or
  automatic), the `commanded` reason label, and a Plan line that reads "commanded 1.00 kW of 1.00 kW
  requested; the device has not confirmed it yet" instead of a failure or a silent success.

Gates: 685 tests (was 675: +8 driver/device-path, +1 API contract, +1 battery-config), the headless
render check 77 passed / 0 failed (was 75), the selfcheck still **33/33**, `tools/audit.sh` clean over
93 files, `compileall` clean. New tests:
`tests/test_wp5_zendure.py::test_device_path_writes_direction_and_limits_and_quiets_the_manager`,
`::test_device_path_discharge_uses_output_mode_and_output_limit`,
`::test_device_path_hold_zeroes_both_limits_without_touching_the_direction`,
`::test_device_path_reports_commanded_until_the_inverter_echoes`,
`::test_device_path_reads_the_direction_option_from_the_entity`,
`::test_control_path_device_is_refused_without_the_limit_entities`,
`::test_a_bogus_control_path_is_rejected_by_the_battery_config`,
`tests/test_wp2b_control.py::test_zendure_reports_a_pending_command_when_the_p1_feed_is_silent`,
`::test_dispatcher_keeps_a_commanded_charge_alive`,
`tests/test_wp5_api_contract.py::test_battery_control_path_is_validated_and_round_trips`.

Honest limits: the device path was verified against the fake HA and the integration's source, not
(or not yet) on the owner's hardware - the owner declined a live write during the fix, so the first
inverter write will be the add-on's own. `ac_mode` option strings are matched case-insensitively
against the entity's own options because the integration localises them; a build that advertises
neither "input" nor "output" falls back to the raw options. The device path is per inverter, but
putting the manager `off` affects every device of that manager, so a mixed fleet must choose one path
for all of them. A `commanded` result is deliberately not a failure and not a measurement: the plan
execution line (measured vs planned) is what turns it into "the device did not follow".

### 8.10 Driving an inverter through its own MQTT bridge (0.11.0)

The owner asked for the control path the live instance actually has: "I also need an option to add the
battery/inverter and control it through its mqtt device, and we can select a zendure mqtt device to
prefill the required fields". The device is connected with its **local MQTT** feature, so it exists
twice in Home Assistant: as a `zendure_ha` device (the integration's entities) and as an `mqtt`
device carrying the raw zenSDK properties, one entity each.

Live read-only evidence (2026-10-12): the `mqtt` device reports model `solarFlow800Plus`, firmware
2.0.59, and 34 entities - `number.<serial>_inputlimit` (`power`, `[0, 1000]`), `number.<serial>_outputlimit`
(`power`, `[0, 800]`), `number.<serial>_inversemaxpower` (`[200, 800]`), `number.<serial>_socset`
(`[70, 100]` %), `number.<serial>_minsoc` (`[5, 50]` %), `select.<serial>_acmode` (options `Input
mode` / `Output mode`), `select.<serial>_gridreverse`, `switch.<serial>_smartmode` (live `off`),
`sensor.<serial>_electriclevel` (%), `sensor.<serial>_gridinputpower` / `_outputhomepower` /
`_solarinputpower` (W), plus per-pack rows (`sensor.<serial>_<pack-serial>_soclevel`, `_power`, ...).
The bridge sets **no** `translation_key`, so the property name inside the entity id is the only role
information there is.

What 0.11.0 adds:

* `ZendureMqttDriver` (`driver = "zendure_mqtt"`), a subclass of the Zendure device path: it writes
  the AC mode select, the two magnitude limits, and the device's own `smartmode` switch (on for a
  non-zero target, off for a hold), and it keeps the Zendure-HA manager `off` when the user maps its
  `operation` select. `validate_mapping` requires `charge_limit_entity`, `discharge_limit_entity` and
  `ac_mode_entity` and checks the domain of every key, so a wrong pick is refused at save time.
* `resolve_mqtt_mapping(entries, device_id|anchor_entity)`: the runtime resolver for this driver
  (registered in `driver_registry.MAPPING_RESOLVERS`), matching `<serial>_<property>` suffixes on the
  `mqtt` platform. Live check on the owner's instance: anchored on the device's SoC entity it
  resolved all ten keys (`charge_limit`, `discharge_limit`, `ac_mode`, `soc`, `soc_target`, `min_soc`,
  `output_power`, `grid_input_power`, `pv_power`, `smart_mode`) to that device's own rows, and
  nothing from the 24 other `mqtt` devices in the registry.
* Discovery vocabulary: the battery role hints learned the camelCase property names
  (`inputlimit`, `outputlimit`, `acmode`, `electriclevel`, `outputhomepower`, `gridinputpower`,
  `solarinputpower`, `socset`, `minsoc`, `smartmode`) and two new roles (`grid_input_power`,
  `smart_mode`), so those rows are in the payload at all; `ROLE_TO_MAPPING["zendure_mqtt"]` maps the
  roles onto this driver's keys (`manual_mode` -> `ac_mode_entity`, `smart_mode` ->
  `smart_mode_entity`).
* The picker identifies a Zendure bridge device by platform plus manufacturer/model and derives each
  role from the property name - no confidence floor, so `electricLevel` always wins over a pack's
  `socLevel` - and fills the whole mapping plus the `max_charge_w`/`max_discharge_w` the limit
  entities state. `Control path` is hidden for drivers with one path, and the MQTT choice says no
  other controller may manage the same inverter.
* `config.py` no longer keeps its own driver list: save-time validation asks
  `control/drivers.names()`, so a battery is checked against exactly the drivers the dispatcher can
  build.

Gates: 694 tests (was 685: +9 driver/resolver/API/mock-contract), the headless render check 78 passed
/ 0 failed (was 77), the selfcheck still **33/33**, `tools/audit.sh` clean over 93 files, `compileall`
clean. New tests: `tests/test_wp5_zendure.py::test_mqtt_charge_quiets_the_manager_switches_smart_mode_and_writes_the_limits`,
`::test_mqtt_discharge_uses_output_mode_and_output_limit`,
`::test_mqtt_hold_zeroes_both_limits_and_switches_smart_mode_off`,
`::test_mqtt_writes_only_what_would_change`,
`::test_mqtt_reports_commanded_until_the_device_echoes_the_limits`,
`::test_mqtt_driver_is_refused_without_its_control_entities`,
`::test_mqtt_roles_are_resolved_from_the_property_names_of_one_device`,
`::test_mqtt_battery_is_validated_by_the_registry`,
`tests/test_wp5_api_contract.py::test_battery_mqtt_driver_is_validated_and_round_trips`, plus the
extended picker assertions in `tests/test_wp3_mock_contract.py`.

Honest limits: the bridge entities were read live, but no write has been made to the inverter from
this side - the first real command is the add-on's own once the owner adds the battery in the UI. The
property semantics are taken from the device's own entity ranges (magnitudes from 0, so no sign
inversion) and from the integration's `smartMode` usage (source, section 6 of EXTERNAL_APIS); if a
firmware build ignores the limits while `smartMode` is off, the driver's own `commanded`/plan-execution
reporting is what will show it. `ac_mode` option strings are matched case-insensitively against the
entity's own options because the labels are localised. Putting the manager `off` affects every device
of that manager, and two batteries pointing at the same inverter would fight each other - the dialog
says so and nothing is written outside the mapped entities.

---

### 8.11 Merging several PV forecasts into one input (0.12.0)

The owner asked for several solar forecast providers to be combined into one hourly forecast instead
of a single source. The design reads the Home Assistant Energy dashboard's own providers over the
read-only, non-admin WebSocket command `energy/solar_forecast` (recorded in docs/EXTERNAL_APIS.md
section 5, verified against core `energy/websocket_api.py` and `energy/types.py`), and merges them
with a new ordered `solar.sources` list rather than changing `solar.source`, so an empty list keeps
the shipped single-source path unchanged.

What 0.12.0 adds:

* `sources/solar.py`: the pure `merge_solar_series(grid, entries, strategy)` (weighted mean, median
  or max; failed/empty/disabled sources skipped; gaps interpolated with the existing rules) and the
  `ha_energy` provider (`parse_ha_energy_wh` / `ha_energy_power_series`, summing several providers or
  selecting one by `entry_id`, converting per-period Wh to average kW). The single-source body was
  extracted into `_forecast_solar_forecast`/`_load_single_source_forecast`; the merge path falls back
  to it verbatim when no source produces a usable series.
* `models.ProviderStatus` gained additive `weight`/`state`/`note`, and `SolarForecast` a `sources`
  list; `api.provider_dict` and `_augment_forecast_payload` expose them, and `engine._fetch`
  remembers each configured source as its own `solar` provider status. `engine._explain_solar`
  names the failing merge sources (frozen `provider_failed`/`no_pv_source` slugs unchanged).
* `config.py`: `solar.sources` (validated entry by entry with a clear per-entry message) and the
  `solar.merge_strategy` choice; `docs/REFERENCE.md`, `README.md`, `CHANGELOG.md` and SPEC §7.1.3/§16
  updated.
* The Solar setup card renders one row per configured source (source select, weight, Move
  up/down, Remove, status chip) plus the strategy select, all through the ordinary dirty/Save flow;
  `tools/ui_render_check.js` asserts the two mocked rows carry a weight and a status.

Gates: 729 tests (was 694: +35 merge/config/HA-payload/status tests), the headless render check 80
passed / 0 failed (was 78), `tools/audit.sh` clean over 94 files, `compileall` clean. New tests:
`tests/test_wp2c_solar_merge.py`. Independent review: not run for this change.

---

### 8.12 Cost basis of the stored energy and the sell floor (0.18.0)

The owner asked for the energy already in the pack to be valued at what it really cost, and for an
optional floor that refuses a sale below that value. The chosen semantics are a persisted per-battery
ledger plus a floor that is **on by default** (`optimizer.sell_floor_enabled`, margin
`optimizer.sell_floor_margin_eur_per_kwh` 0..1). Nothing here reads or writes a live system: the
whole change is unit- and engine-tested against fakes.

What 0.18.0 adds:

* `app/cost_basis.py` (new, stdlib only, mirrors `capacity.py`): a per-battery ledger in
  `learning_profiles` (`kind='battery_cost_basis'`) whose intervals are **event driven, not timed
  buckets**. A slice opens on an applied dispatch change (`dispatch_log`: `mode auto`, `dry_run`
  false, `result ok`/`skipped`, `applied_w` differing from the last booked command by more than
  25 W) and closes at the next change or at the update's `now`; the slice end is stored as `last_ts`,
  so replaying an update books nothing twice. Slice energy is the *measured* battery power integral
  (piecewise-linear between persisted samples, `battery_power` or the charge/discharge pair), never
  the commanded setpoint; a gap longer than 10 minutes is never bridged. Each slice is priced at the
  plan slot it was charged in (the persisted plans, newest plan winning for a slot start, split at
  every price-slot boundary it spans): `grid_part = min(charge_ac, import_ac)` at `buy` and
  `pv_part = charge_ac - grid_part` at `sell` (the export forgone), with a missing `sell` falling
  back to `buy`, a skipped-and-counted sub-slice when neither resolves, and the plan slot's own
  reason as the attribution fallback when the meter reading is missing. The basis is
  `(basis*E + cost)/(E + charge_ac)`, a discharge reduces `E` by `discharged / round_trip`, the SoC
  floor resets to `basis=None, E=0`, `E` is clamped at `capacity_kwh / eta_charge`, a first run (or
  a gap beyond the 7-day lookback) starts clean, and the last 200 slices stay as an audit journal.
* `optimizer.sell_floor_enabled` / `optimizer.sell_floor_margin_eur_per_kwh` in `config.py`
  (validated, documented; the margin folds as advanced, the on/off stays essential). The floor per
  battery is the frozen `BatteryConfig.break_even_sell_eur_per_kwh(basis)` plus the margin; the
  fleet value is the energy-weighted mean.
* `OptimizerOptions.sell_floor_eur_per_kwh` and an additive `solve`/`solve_from_forecasts` kwarg: the
  DP removes discharge/export transitions whose value leg is below the floor (the leg is `sell` when
  exporting, `buy` when serving the house) and a slot that holds for it reports the new additive
  `Reason.SELL_FLOOR`; charging, SoC-floor holds and everything outside `auto` are untouched, and
  `None` reproduces the pre-0.18.0 schedule bit for bit. `PlanSummary.sell_floor_eur_per_kwh`
  carries the enforced floor into `/plan`.
* A dispatch backstop in `control/safety.check` (additive `sell_floor_eur_per_kwh` kwarg): a live
  *planned* discharge whose value leg is below the floor is clamped to a hold with the same reason,
  recorded like the other clamped reasons; an explicit override is never second-guessed. No new
  alerts, so a clamped tick cannot storm the log.
* Exposure: `/api/v1/status` gains per-battery `cost_basis_eur_per_kwh` and `stored_value_eur`
  (`basis * E`, defined once) plus a `cost_basis` block with the fleet values and a five-entry per-
  battery journal slice; the engine publishes `sensor.sem_battery_cost_basis` (EUR/kWh) and
  `sensor.sem_battery_value` (EUR) through the existing idempotent publisher, publishing `unknown`
  rather than a fabricated number when no ledger exists. `mock.js`, SPEC 5/16, README,
  `docs/REFERENCE.md` and `CHANGELOG.md` were updated with the settings, the formula, the sensors and
  the payload fields.

Gates: 885 tests (was 860: +25 ledger/floor/dispatch/status/sensor tests), the headless render check
97 passed / 0 failed (was 97: the settings surface changed but no rendered row did), `tools/audit.sh`
clean over 103 files, `compileall` clean. New tests: `tests/test_cost_basis.py`.

What is *not* verified live: the ledger has only ever been exercised against synthetic store rows;
the field behaviour of a real `battery_power` series across a vendor manager's own commands, the
plan-slot price resolution after a long outage, and the floor's effect on a real sale decision were
not measured on hardware. The default-on floor therefore only activates once a ledger exists (no
basis = no floor), so a fresh install behaves exactly as 0.17.1 until it has measured charges.
Independent review: not run for this change.

---

### 8.13 Measured energy on the sample grid and reconciliation with the pack (0.18.1)

The live 0.18.0 ledger on the owner's install under-reported the pack's energy. `/api/v1/status`
showed `energy_kwh 1.4094` with `intervals 250` and `skipped_no_power 142`, while the pack stood at
87 % SoC (min 10 %, capacity 1.92 kWh, `eta_charge` 0.92), whose physical AC-charged-in equivalent
is `1.92 * 0.77 / 0.92 = 1.607 kWh` — about 0.2 kWh (12 %) behind. The measurements are written on a
~5-minute sample cadence while this installation changes the applied setpoint every ~2 minutes
(grid-follow trimming), and the ledger booked each update's tail flat at the last visible sample,
so the ramp the next sample revealed was never recovered (reproduced synthetically: a 7-11 %
deficit on a rising 5-minute series after a few dozen updates; on the live cadence 142 of 250
command intervals booked no power at all).

What 0.18.1 adds:

* `app/cost_basis.py` books energy on the settled **sample grid**: the covered sample spans are
  integrated between consecutive samples, split at the applied command changes and price-slot
  boundaries they cross, and accumulated into that command interval's single journal entry, so a
  command whose whole span falls between two samples still books its measured share. `last_ts` is
  the end of the last processed sample span (never a command timestamp) and nothing beyond the
  newest settled sample is booked: an unanswered tail waits for its next sample instead of being
  extrapolated flat, a replay books nothing twice, and a settled command interval with no covered
  sample span counts in `skipped_no_power` (which drops to ~0 on a healthy series). Pricing, the
  source split and every fallback are unchanged from 0.18.0.
* Reconciliation (additive counters): `E_phys = clamp(capacity_kwh * (soc - min_soc_pct) / 100 /
  eta_charge, 0, capacity_kwh / eta_charge)` from the last SoC sample at or before the booking end;
  the difference is applied to `energy_kwh` and never to `basis`, the signed cumulative correction
  is `reconciled_kwh` plus `reconciled_last_kwh`, a stale or missing SoC sample skips the step and
  counts `skipped_no_soc`, and a single correction beyond half the pack capacity resets the ledger
  (`basis=None`, `E=0`) and counts `reconciled_resets`. All four counters travel in the `/status`
  `cost_basis` battery rows.
* `tools/audit.sh` (its own commit, `audit: allow the public PVGIS-SARAH2 dataset name`) adds one
  exact public dataset identifier to the documented serial-shape allowlist so an unrelated docs
  file can mention it; no rule is loosened and no other token is added.

Gates: 893 tests (was 886: +7 sample-grid/reconciliation tests in `tests/test_cost_basis.py`), the
headless render check 97 passed / 0 failed (no UI surface changed), `tools/audit.sh` clean over 104
files, `compileall` clean.

What is *not* verified live: booking and reconciliation were exercised against synthetic and
store-backed series, never against the live add-on; the 1.607 kWh expectation follows from the
reported SoC and config, not from a re-read of that install, and the reconciliation is trusted
whenever its correction stays within half a pack, so a badly wrong SoC sensor could move the
quantity within that band (the basis is never affected). Independent review: not run for this
change.

---

### 8.14 The sell floor must not force PV charging; the PV charge preference (0.19.0)

The live 0.18.1 plan on the owner's install charged the pack at 08:00-10:00 instead of the cheaper
11:00-13:00 hours: +303 / +849 / +727 W at sells 0.3361 / 0.2989 / 0.2797 against 11:00-13:00 at
0.2660 / 0.2620 / 0.2734, about 6 ct worse over that window, because exporting the PV earlier and
charging later was the profitable schedule. `power_bounds_kw`'s floor branch used
`grid_lo = max(grid_lo, -base)`: with `base = load - pv < 0` (a PV surplus) that is a *positive*
lower bound, so the pack was forced to swallow the whole surplus whatever the price. The floor
exists to stop *stored* energy being sold below its cost basis; PV that would go straight to the
meter has no basis, so exporting it must stay free.

Reproduced offline with the live-shaped 30 h inputs (`dp.solve_from_forecasts`: 1.92 kWh pack,
10-100 %, 1000/800 W, eta 0.92, target 100 %, buy = sell + 0.1155; the 08:00-13:00 sells are the
live ones). The bound, 1 h slot, SoC-safe:

| live-shaped slot | 0.18.1, floor 0.3997 | 0.18.1, no floor | 0.19.0, floor 0.3997 |
| --- | --- | --- | --- |
| surplus 0.30 kW, sell 0.3361 | `p in [+0.300, +1.000]` | `p in [-0.460, +1.000]` | `p in [0.000, +1.000]` |
| surplus 0.85 kW, sell 0.2989 | `p in [+0.850, +1.000]` | `p in [-0.460, +1.000]` | `p in [0.000, +1.000]` |
| deficit 0.21 kW, sell 0.3361 | `p in [-0.210, +1.000]` | `p in [-0.460, +1.000]` | `p in [-0.210, +1.000]` |

The schedule and the euro arithmetic for 08:00-13:00 (`slot.cost_eur(1.0)` of the returned plan):

| plan | 08:00 | 09:00 | 10:00 | 11:00 | 12:00 | 13:00 | 08-13 cost | 30 h cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.18.1 floored | +303.0 W | +849.0 W | +726.3 W | 0 W | 0 W | 0 W | -0.5775 | 3.5436 |
| 0.18.1 unfloored | 0 W | 0 W | 0 W | +565.2 W | +678.6 W | +634.4 W | -0.6347 | 3.4877 |
| 0.19.0 floored | 0 W | 0 W | 0 W | +565.2 W | +695.7 W | +617.4 W | -0.6348 | 3.4863 |

All three reach the 1879 Wh the pack needs (1.728 kWh usable / 0.92). The 5.7 ct is the export
revenue the early charge forgoes: early stored PV
`0.303*0.3361 + 0.849*0.2989 + 0.7263*0.2797 = 0.5587` against
`0.5652*0.2660 + 0.6786*0.2620 + 0.6344*0.2734 = 0.5016` in the 11:00-13:00 window, i.e. 0.0571
(the measured window difference -0.5775 - (-0.6347) = 0.0572; the euro figures are rounded to 4
decimals). (The 0.19.0 floored plan differs slightly from the
unfloored one after 13:00 because the floor still clamps a house-serving discharge to the house's
own demand.)

`optimizer.pv_charge_preference` (0.19.0, `auto` | `prefer_pv` | `avoid_pv`, default `auto`) sets
the price the objective assigns to PV energy a charge puts into the pack, as an internal shadow
price: for `p > 0`, `surplus = max(0, pv - load)`, `pv_kwh = min(p, surplus)`, the objective gains
`dt * (pv_price - sell) * pv_kwh` with `pv_price` the slot's `sell` (`auto`; the term is zero and
the model is exactly the true grid cost), `0` (`prefer_pv`: absorb the surplus before buying
anything) or the slot's `buy` (`avoid_pv`: export the PV and charge from the grid in the cheapest
hours). The summary money is untouched: `cost_estimate_eur`, `baseline_cost_eur` and `saved_eur`
remain the true grid cost of the returned schedule (SPEC 8.1), so no mode can invent a saving.

Tests (`tests/test_cost_basis.py`, five new; each fails on 0.18.1): the bounds table above; the
live-shaped schedule holds at 08:00-10:00 and charges 1879 Wh at 11:00-13:00; a below-floor battery
discharge is refused while the PV export in the same slot is allowed; the three modes pick the slot
their PV price points at; the summary money equals the plain grid cost of each mode's schedule (the
`prefer_pv` objective carries a -0.30 shadow credit while the reported cost stays 0.05); and
`OptimizerOptions.from_settings` normalizes a bad stored value to `auto`. The new key also
round-trips through `config.coerce_setting` (`tests/test_wp2a_config.py`, bad values rejected) and
the UI mock carries it (`tests/test_wp3_mock_contract.py` parity).

Gates: 898 tests (was 893: +5 in `tests/test_cost_basis.py`; the config and mock assertions extend
existing tests), the headless render check 97 passed / 0 failed (the new row renders, no other
surface changed), `tools/audit.sh` clean over 103 files, `compileall` clean.

What is *not* verified live: the add-on was not deployed and nothing was written to the live
instance; the live-shaped horizon is a reconstruction from the reported plan (its 08:00-13:00
prices and the PV surplus the forced powers imply), not a captured forecast, and the three
preference modes were exercised on synthetic horizons only. Independent review: not run for this
change.

---

### 8.15 Dashboard tab and manual override limits (0.20.0)

The primary nav's first tab is now **Dashboard** (`#/dashboard` unchanged; the internal
`NAV_AREAS["/dashboard"] = "today"` key is not user-visible and stays), and the Dashboard carries a
new **Manual override** panel between *Warnings and activity* and *Right now*. The panel forces
charge/discharge/hold for 5–120 min on one battery or the fleet through the unchanged
`POST/DELETE /override`, and sets per-direction `Max charge`/`Max discharge` caps per battery and
for an `All batteries` row through `POST/DELETE /limits`.

The caps are applied by lowering the **effective** config (`max_charge_w = min(configured, cap)`,
`max_discharge_w = min(configured, cap)`), which is the single number the planner's own bounds, the
aggregate safety clamp, the per-battery allocation/headroom, the dispatcher's per-battery clamp, the
HA publisher and `/status` read; the stored battery config and the battery PATCH path are never
touched. Resolution for one direction is per-battery entry → `"*"` fleet entry → the configured
maximum, and the resolver clamps a hand-edited or stale cap so no path can raise a maximum. A value
above the configured maximum (or below 0) is refused by the API with 400 `invalid_body` naming it,
never silently clamped. Limits persist like `mode`/`dry_run` (settings-store key `manual_limits`,
deliberately not a `DEFAULTS` field, so it never becomes a `/config` form field) and are journaled
(`limits_set`, `limits_cleared`); the cell-voltage guard, the SOC floor/ceiling, the reserve and the
full-charge state machine are untouched, so a limit can only narrow what those rails already allow.

Concrete scheduled-power example — the selfcheck-shaped 30 h horizon (5 kWh / 1000 W battery, eta
0.95, start 11:00, grid follow off; `dp.solve_from_forecasts`), slot powers in W:

| plan | 11:00 | 12:00 | 13:00 | 14:00 | 15:00 | 16:00 | 17:00 | 18:00 | 19:00 | 20:00 | … | cost / saved (EUR) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| uncapped | +1000 | +1000 | +368 | 0 | 0 | −436 | −333 | −800 | −800 | −800 | … | 1.4665 / 1.0735 |
| `discharge_w = 200` | +1000 | +1000 | +368 | 0 | 0 | −200 | −200 | −200 | −200 | −200 | … | 1.8679 / 0.6721 |
| `charge_w = 200` | +200 | +200 | +200 | +200 | +200 | −255 | −73 | −800 | −800 | −800 | … | — |

Every capped slot obeys the cap (never below −200 W / never above +200 W), the horizon is unchanged
(11:00 → 17:00 next day) and the summary reports the real consequence instead of the uncapped
schedule's saving: capping the discharge costs 0.40 EUR over the horizon (saved 1.0735 → 0.6721).

Tests (`tests/test_wp8_manual_limits.py`, 13 new; each behavioural one fails on 0.19.0): resolution
(per-battery beats `"*"`, `0` blocks, `None`/absent is unlimited, a cap above the maximum is clamped
by the resolver, junk is dropped on load); `effective_config` caps without touching the stored
config and a battery `PATCH` still reads back the requested maximum while the cap applies; the
planner never exceeds a discharge or a charge cap and still covers the horizon; a force above the
limit is applied at the limit; a force-discharge pin at the SOC floor still yields 0 W; a blocked
direction is never commanded; the limits survive a reload of the real settings store and clearing
restores the configured maxima; `/status` carries `manual_limits` and the resolved caps; `POST`/
`DELETE /limits` round-trip including the fleet entry and the 404/400 paths. The mock contract
(`tests/test_wp3_mock_contract.py`) asserts the mock's `/limits` round-trip and the new `/status`
keys, and the render check asserts the panel in both states (nothing set; an active override with a
per-battery limit).

Gates: 912 tests (was 898: +13 in `tests/test_wp8_manual_limits.py`, +1 mock limits round-trip; the
status/mock assertions extend existing tests), the headless render check 109 passed / 0 failed (12
new checks), `tools/audit.sh` clean over 111 files, `compileall` clean, the add-on selfcheck 33/33.

What is *not* verified live: the add-on was not deployed and nothing was written to the live
instance or any battery; the force/limit panel was exercised through the DOM shim and the mock API
only, not in a browser against the running add-on, and the concrete plan above is a synthetic
selfcheck horizon, not a captured forecast. Independent review: not run for this change.

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

# 2. Full test suite (expect: 729 passed; needs pytest and PyYAML, which only the manifest test
#    uses — the add-on itself imports nothing outside the standard library)
python3 -m pytest tests -q

# 3. R1 tripwire (expect: "audit: clean — 94 files scanned", exit 0)
sh tools/audit.sh

# 4. End-to-end selfcheck (expect: 33/33 checks passed, exit 0)
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
