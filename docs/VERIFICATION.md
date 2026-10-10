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
| Test suite | `python3 -m pytest tests -q` | **595 passed** (two consecutive runs) |
| PII / secret scanner | `sh tools/audit.sh` | `audit: clean — 89 files scanned`, exit 0 |
| Add-on selfcheck | `PYTHONPATH=energy_manager python3 -m app.main --selfcheck` | **31/31 checks passed**, exit 0 |
| Syntax / imports | `python3 -m compileall -q energy_manager/app tools/make_icons.py tests` | clean, exit 0 |

Frozen digest of the verified artifact (recipe in §11, normative):

* **code + tests (77 files, documentation excluded)**:
  `d92974e5c47f530883b394ea85bf5e7ae6e2bbceddfa31c65c94f1987e5088f6`
* **path list of the same 77 files** (detects an added, removed or renamed file):
  `63e3910878cca4ec6ac8802e98b49448c7d6655eab98076ddfcf1d0140adf88b`

The recipe sorts the file list **and** the per-file manifest under `LC_ALL=C`, so the digest is
independent of `find` order and of the locale; a single `sort` is not enough. An earlier revision of
this record published digests from an order-dependent pipeline and they could not be reproduced — a
defect in the record, not in the tree: the file lists were verified byte-identical to the frozen ones,
so no source file had changed. Those values (`8f83a0a6…` all files, `c58e912c…` code + tests) are
superseded by the digest above.

The all-files digest (90 files) is deliberately **not** recorded here: this document is one of the 90,
so every documentation edit would change it while the verified code stayed identical. Compute it with
the same recipe over the full file list if you want it; the file count printed by the pipeline (90) is
the quick cross-check.

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
* The break-even sale price is `buy / round_trip + cycle_cost / eta_discharge`; 20 ct at a 75 %
  round trip gives 26.666… ct exactly (not 25 ct, which would be the 80 % case).
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
`classList`, `dataset`, `isConnected`, closed-`<details>` semantics) and asserts **36** properties of
the result. The load-bearing ones:

* all seven views render from mock data with no `undefined` or `NaN` in the output;
* every setting the API sends is reachable: selecting each group through the rail and scanning the
  rendered rows finds all **75** keys (the **65** that apply to the current price/solar selection, plus
  the ten that are hidden-but-set and therefore still listed, muted, as "not used by the current
  selection");
* advanced rows are **not** shown by default, opening the disclosure shows them, and `Show all
  advanced` shows every applicable advanced key (**33** exist, **23** apply to the mock's selection);
* the 16 Safety rows are visible **without** unfolding anything;
* the rail lists exactly the server's nine groups, the save bar exists, every class the front-end
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

**What this does not prove.** There is no browser on this machine. The render check is structural: it
proves the DOM the front-end builds, not the pixels a person sees. Contrast was computed by the CSS
worker while writing the palette (>= 4.5:1 body text, >= 3:1 large text and UI), the tokens and the
accessibility rules are intact in source and asserted, and the layout is responsive by construction -
but spacing, the light palette in practice and the breakpoints have not been eyeballed here. The
click-through is the owner's, and it is the honest last step of this wave.

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

# 2. Full test suite (expect: 595 passed; needs pytest and PyYAML, which only the manifest test
#    uses — the add-on itself imports nothing outside the standard library)
python3 -m pytest tests -q

# 3. R1 tripwire (expect: "audit: clean — 89 files scanned", exit 0)
sh tools/audit.sh

# 4. End-to-end selfcheck (expect: 31/31 checks passed, exit 0)
PYTHONPATH=energy_manager python3 -m app.main --selfcheck

# 5. Render the front-end headless and check its structure (expect: "0 failed", exit 0; also run by
#    tests/test_wp3_ui_render.py, which skips when node is absent)
node tools/ui_render_check.js
```

Frozen digests — manifest digests over the file sets the two recipes select. Both stages are sorted
under `LC_ALL=C`, which makes the value independent of `find` order and of the locale (a single `sort`
produces a different value). The `wc -l` count is the cross-check (90 and 77).

```sh
# all files (90)
find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
        -o -name node_modules -o -name .mypy_cache \) -prune -o -type f -print \
  | LC_ALL=C sort | xargs sha256sum | LC_ALL=C sort | sha256sum

# code + tests only (73), documentation excluded
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
part of either recipe: it must print 84 (all files) or 72 (code + tests only).
