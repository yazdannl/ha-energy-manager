# Verification record — Smart Energy Manager add-on

Authoritative verification record for the tree published from this repository. Every number below
was produced by a gate named in §7 on the frozen tree; nothing here is a projection. This record
supersedes the earlier WP5 report — the 292 / 359 / 398-test and 83-file figures in previous
revisions described pre-audit trees and are obsolete.

Verification was local and hardware-free: no real battery, no live provider call, no instance data.
All synthetic values use reserved namespaces (`sensor.example_*`, `number.example_*`, `sem_*`),
loopback addresses and placeholder coordinates.

---

## 1. Verification state

| Gate | Command (from the repository root) | Result |
| --- | --- | --- |
| Test suite | `python3 -m pytest tests -q` | **493 passed** (two consecutive runs) |
| PII / secret scanner | `sh tools/audit.sh` | `audit: clean — 84 files scanned`, exit 0 |
| Add-on selfcheck | `PYTHONPATH=energy_manager python3 -m app.main --selfcheck` | **31/31 checks passed**, exit 0 |
| Syntax / imports | `python3 -m compileall -q energy_manager/app tools/make_icons.py tests` | clean, exit 0 |

Frozen digest of the verified artifact (recipe in §7, normative):

* **code + tests (72 files, documentation excluded)**:
  `b095c5a57616865a4d5bbd4fbf22c95684f0aa28197dae557ec407cbe1e3347f`
* **path list of the same 72 files** (detects an added, removed or renamed file):
  `dfb3b4a9b4f6537d5929c22d6ca824fddd44d7c6b8e11894b9e35cb1ce15af63`

The recipe sorts the file list **and** the per-file manifest under `LC_ALL=C`, so the digest is
independent of `find` order and of the locale; a single `sort` is not enough. An earlier revision of
this record published digests from an order-dependent pipeline and they could not be reproduced — a
defect in the record, not in the tree: the file lists were verified byte-identical to the frozen ones,
so no source file had changed. Those values (`8f83a0a6…` all files, `c58e912c…` code + tests) are
superseded by the digest above.

The all-files digest (84 files) is deliberately **not** recorded here: this document is one of the 84,
so every documentation edit would change it while the verified code stayed identical. Compute it with
the same recipe over the full file list if you want it; the file count printed by the pipeline (84) is
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

## 5. R1 scanner (`tools/audit.sh`)

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

Documented ceilings, from the script header: MAC addresses written with hyphens, dots or no
separator; IPv6 literals; public dynamic-DNS hostnames; 3-decimal coordinate pairs; and
negative-only mixed-length pairs are **not** caught. The header also names the accepted false
positives. The scanner prints at most four characters of any match, so a scan never re-prints a
value that may be sensitive.

---

## 6. What is not verified

* **No real battery or Zendure hardware.** Driver behaviour is asserted against the documented
  integration semantics and a fake over loopback, not observed on a device.
* **No live price, PV or load provider calls.** The fallback chain and rate-limit handling were
  exercised with injected fakes; real payloads, headers and timezone edge cases remain unverified.
* **No full add-on install.** The packaged image's entrypoint was verified from a locally built
  image: `/bin/sh /run.sh` without a token exits 1 with the clear message, and with a token it execs
  the app. The Home Assistant Supervisor install path itself was not exercised.
* **Threaded timing** is asserted with deterministic barriers, not wall-clock races.
* **A persisted plan is never restored**, so the stored plan summary's efficiency fields matter only
  on the API path — no on-disk plan is resumed after a restart.

Known behaviour, accepted and deliberate:

* the safety path still blocks 0 W when the base load alone exceeds a configured grid cap
  (conservative: it never worsens the grid flow, it simply refuses to act);
* the HTTP API binds `0.0.0.0` and is unauthenticated by design, because ingress supplies the login;
  `config.yaml` publishes no port and 8099 must not be mapped.

---

## 7. How to reproduce

No network access and no hardware are needed for any command below.

```sh
# 1. Syntax / imports
python3 -m compileall -q energy_manager/app tools/make_icons.py tests

# 2. Full test suite (expect: 493 passed)
python3 -m pytest tests -q

# 3. R1 tripwire (expect: "audit: clean — 84 files scanned", exit 0)
sh tools/audit.sh

# 4. End-to-end selfcheck (expect: 31/31 checks passed, exit 0)
PYTHONPATH=energy_manager python3 -m app.main --selfcheck
```

Frozen digests — manifest digests over the file sets the two recipes select. Both stages are sorted
under `LC_ALL=C`, which makes the value independent of `find` order and of the locale (a single `sort`
produces a different value). The `wc -l` count is the cross-check (84 and 72).

```sh
# all files (84)
find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
        -o -name node_modules -o -name .mypy_cache \) -prune -o -type f -print \
  | LC_ALL=C sort | xargs sha256sum | LC_ALL=C sort | sha256sum

# code + tests only (72), documentation excluded
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
