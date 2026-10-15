# Learned load and PV forecasting

**Status: the PV half is shipped; the load half is still a plan.** `solar.source = learned` (SPEC.md
§7.1.4), the forecast archive (§7.4) and the nightly fine-tune head (§7.5) shipped in 0.22.0, with
measured results in §1.1. The load model below has not been built: there is no Dutch household-load
corpus, and the existing `sources/load.py` tiers already learn from this installation's own meter. One
structural piece of it did ship — **named load devices** (`load.devices`, SPEC.md §7.2), which split
one house-wide profile into `base + sum(devices)` with a per-device temperature term — because it needs
no corpus: it is still this installation's own meter, in two or three pieces instead of one. Any
further change to frozen contracts (`../SPEC.md`) still needs the usual amendment plus the gate set in
`VERIFICATION.md`.

The goal is narrow: replace two hand-tuned estimators with learned ones.

| Today | File | Replacement |
| --- | --- | --- |
| 48-bucket median profile + level scale + EWMA bias + one global temperature slope (now per named device with its own subtracted profile) | `app/sources/load.py` | conditional-quantile load forecast |
| 24 hourly scalar bias factors over a physics base | `app/sources/solar.py` | feature-conditioned PV residual model |

Neither the optimizer (`app/optimize/dp.py`) nor the control path changes. A forecast is an input;
the DP stays exact for the inputs it is given.

## 1.1 Measured result: the base PV model

**Shipped in 0.22.0 as `solar.source = learned`** (SPEC.md §7.1.4), served from the committed artifact
by standard-library arithmetic only. Fitted on the Utrecht corpus and validated **two independent
ways**: on held-out **systems** (5 folds, 34–35 unseen arrays per fold) and on held-out **years**.
1,863,412 hourly daylight rows over 174 systems and 1,461 days (2014–2017). Full detail and
reproduction steps in `tools/forecast_ml/README.md`.

| model | system split: MAE (% nameplate) | year split: MAE (% nameplate) | system split: energy bias |
| --- | --- | --- | --- |
| physics only (ratio = 1) | 7.064% | 7.099% | **+14.44%** |
| ridge, same features | 5.669% | 5.708% | +0.41% |
| **gradient-boosted trees** | **5.102%** | **5.548%** | **+0.11%** |

The year split is the one that matters for a *base* model, whose entire job is to work before any
local data exists: trained on 2014 alone and scored on 2015–2017, error rises only ~9% (119.2 →
129.6 W) and the energy bias stays at +1.19% against the physics prior's +14.52%. A system split
cannot show that — it can look excellent while the model has memorised one year of weather.

Aggregate gate — the check that matters, because a model cannot be scored against a PV measurement
that does not exist: over the same rows the model predicts **3,043.5 kWh/kWp** against **3,036.7
measured** (+0.22%), and reproduces the cross-system spread (predicted p10–p90 1,808–3,381 against
1,750–3,434 measured). Reproducing the *spread* is what a single global multiplier cannot do. The
absolute level carries a coverage caveat stated in the tooling README; the comparison itself is on
identical rows.

One trap found while making the numbers above trustworthy, recorded because it is the kind of thing
that silently invalidates a model rather than crashing it: the pipeline's day-of-year feature was
`days_since_epoch % 365 + 1`, which is offset by 11–13 days and drifts by a day per year as leap days
accumulate. It made the seasonal feature mean something different in every training year. Retraining
was cheaper than embodying it in a shipped artifact, and fixing it improved everything — including the
physics prior, from 176.1 W to 163.4 W.

The §8.1 requirement that aggregate gates are mandatory rather than optional is therefore not
theoretical: it is the only reason the numbers above can be trusted at all. The row-level error alone
would have looked fine for a model that had learned the average array and nothing else.

Weights are stored as **IEEE-754 bit patterns in integers**, which is what lets the artifact be
committed at all: as JSON floats it scores ~18k findings from the repository's own PII audit, because
dense arrays of 4–5 decimal values are exactly the shape that tripwire hunts. Integers are also what
makes the export exact — a power-of-ten fixed-point scale silently changed ~2% of predictions. See
`tools/forecast_ml/README.md`.

---

## 1. The observables (measured, 2026)

Facts established by read-only `recorder/statistics_during_period` and `recorder/list_statistic_ids`
calls. Role names only, per the `EXTERNAL_APIS.md` convention.

| Series (role) | Depth | Resolution | Note |
| --- | --- | --- | --- |
| Signed net grid power | **≈138 days (~3.3k hourly rows)** | hour | the only real observable |
| Grid import / export registers | same | hour | redundant with the net reading |
| Outdoor temperature (heat-pump probe) | ≈156 days | hour | ⚠️ max hourly mean implausible; needs validating |
| Battery AC flows | ≈6 days | hour | the confounder; only recently installed |
| Battery PV inputs | ≈5 days | hour | all exactly zero — array is not connected there |
| Recorder short-term statistics | ≈10 days | 5 min | expires on the recorder's own schedule |
| NASA POWER (irradiance, T2M, cloud) | decades → ~3 days ago | hour | keyless |

Three conclusions the design is built on:

1. **The clean window is the pre-battery period (~133 days).** With no battery installed,
   `grid = load − pv` holds exactly and there is no confounder to subtract. This is the training set.
2. **Only one half of the year is observed.** The window is a summer half-year. No winter, no
   heating season, no low-sun PV. A free-form model would have to *extrapolate* through a season it
   has never seen, so the physics prior must carry the seasonal structure.
3. **5-minute data is already expiring.** If any high-resolution training is wanted, the snapshot
   has to start before those rows age out. Hourly long-term statistics are not purged.

## 2. Why one model, not "derive PV, then learn load"

Deriving PV first and then training load on the residual is circular: the load labels would come
from a model that already assumed a load. Instead, train one model with two heads through the single
signal that is actually measured:

```
y_hat(t) = load_theta(x_t) - pv_phi(x_t) - p_battery(t)
loss     = sum_t w_t * huber(y_obs(t) - y_hat(t))  +  prior terms
```

`p_battery` is known telemetry, not learned. The model is scored only on what can be observed.

### 2.1 Identifiability, honestly

* **Night: `pv = 0` exactly, so `load = net` exactly.** With `p_battery` known, night hours give
  *exact* load labels for roughly half of all hours. That is the anchor that pins the load model.
* **Daytime: one equation, two unknowns.** The split is resolved by the PV physics prior plus
  `0 <= load` and a positive base load. Example: net reads −4 kW and physics says PV is 4.5 kW, so
  load is ~0.5 kW.
* **Non-negativity is a real constraint, not a formality.** It is what makes the daytime split
  tractable at all.

### 2.2 The dominant-error problem on this array

Array nameplate is several kWp against a household average under 1 kW. Near midday, PV is roughly
**five times** the load, so a 15–20 % PV modelling error is larger than the entire load signal.

| Quantity | Identifiability |
| --- | --- |
| PV, daytime | **good** — it dominates the meter |
| PV, annual/monthly energy | **good** — physics-constrained |
| Load, night | **exact** |
| Load, daytime high-PV hours | **poor** — expect large relative error |

Consequences, which must be stated in the docs and the UI rather than hidden:

* The load head is learned from night hours plus low-PV daytime (winter, overcast, dawn/dusk) and
  must **extrapolate the day shape parametrically**. A free-form daytime load model would be fitting
  mostly PV error.
* Aggregate accuracy will look far better than hourly accuracy. Both are reported.
* This is still a large improvement on the current flat fallback, which is a constant base level.

## 3. Model shape

**Physics first, learned residual second.** The residual is bounded by the same clamps the current
bias correction uses, so a bad model cannot produce a forecast that drives the battery somewhere
strange.

```
pv_ac(t) = min( sum_planes Wp_p * (G_poa,p / G_stc) * eta_loss * eta_temp(T) * aoi_p(t) ,
                P_inv_limit )

pv_final(t) = pv_ac(t) * clamp(residual_head(x_t), RESIDUAL_MIN, RESIDUAL_MAX)
```

* `G_poa` comes from the existing astronomy + transposition code, driven by *observed* irradiance
  for training and *forecast* cloud/irradiance for deployment. These two roles must not be mixed.
* `eta_temp` is the module temperature coefficient (typically −0.35…−0.45 %/°C). **Panel efficiency
  as a module percentage is redundant** given `Wp` (efficiency × area = Wp) and must not be a
  parameter; the temperature coefficient is the term that actually buys accuracy.
* `P_inv_limit` and the clipping shape are **configurable per setup** (see §4). A DC/AC ratio above
  one makes midday clipping a real nonlinearity that would otherwise be misattributed to load.
* The load head predicts conditional quantiles at 0.1 / 0.5 / 0.9, which map directly onto the
  existing `LoadSlot.load_kw` / `p10_kw` / `p90_kw` fields — no `models.py` change required for the
  shape.

### 3.1 Suggested estimator

Gradient-boosted trees with pinball loss, one ensemble per head. Rationale: robust at ~3k samples,
handles the temperature × hour interaction the current single global slope cannot, needs no feature
scaling, and infers in pure Python as array comparisons (see §6, rule **R3**). A ridge model on the
same engineered features is the mandatory baseline that the trees must beat.

A neural formulation is not justified at this sample size, and would need a third-party runtime.

## 4. Array and inverter configuration (per setup)

Because topology is configurable per install, the config is both a physics input and a feature block.
Changing it re-derives the physics and re-conditions the residual head; it does not silently
invalidate the model.

| Key | Meaning |
| --- | --- |
| plane list | per plane: `Wp`, tilt, azimuth, optional weight |
| inverter | per plane or shared: max AC power, DC/AC ratio, clipping behaviour |
| losses | soiling/snow multiplier, cabling/mismatch `eta_loss` |
| temperature | module temperature coefficient |
| location | latitude, longitude, timezone |

The trained artifact records a **config hash**. When the hash changes, the artifact is marked stale,
the physics + online residual path keeps working, and a note is published — the forecast never
disappears because a setting changed.

## 5. Feature contract

Load head: hour and minute-of-slot cyclic; **day-of-week one-hot** (not the current two day-types);
day-of-year cyclic; holiday and DST flags; **lag terms (last 1/2/3 h, same hour yesterday, rolling
24 h mean)**; temperature, temperature at −24 h, heating- and cooling-degree terms, temperature ×
hour interactions; day length; recent level.

PV residual head: sun elevation and azimuth (per plane), cos(AOI) per plane, forecast cloud
coverage, *observed* clearness index (training only), ambient temperature, hour and day-of-year
cyclic, plane geometry, configured inverter limit and clipping headroom, the external provider's own
raw output, and recent residual history.

Shared: nothing that is not available at forecast time may appear in the deployed feature set. Lags
of the *live meter* are the one exception, and only for the nowcast path in §7.

## 6. Two-tier training

The model is deliberately split so that adaptation is cheap and stable, and so that a bad night of
data cannot damage the physics.

| Tier | What it is | Retrained | Runs where |
| --- | --- | --- | --- |
| **Base** | physics + a residual head trained on public multi-house data | rarely, offline | repo tooling (third-party libraries allowed) |
| **Fine-tune** | a small correction head on top of the frozen base | **nightly** | in the add-on (stdlib only, rule **R3**) |

```
tools/forecast_ml/
  snapshot.py   pull aligned history from HA statistics + NASA POWER -> local training table
  corpus.py     fetch/normalise the public corpora (section 6.2)
  dataset.py    feature assembly, quality filters, season tagging
  train_base.py fit the base model, export JSON artifact
  backtest.py   rolling-origin evaluation and reporting
app/forecast/   (runtime) feature builder, pure-Python inference, nightly fine-tune
```

Artifact: `app/forecast/models/<name>_v<n>.json` — feature names, tree arrays, clamp bounds, training
window, config hash, digest. Versioned in git so a model change is reviewable like any other change.
The nightly head is **never** written into the artifact; it lives in the local store and is applied
on top, so deleting it restores exactly the base behaviour.

### 6.1 Why the split is load-bearing

About 24 new hourly rows arrive per day (288 at 5-minute resolution). Retraining a model of that size
nightly on that little new data would learn noise. Three rules follow, and they are not optional:

1. **Freeze the base; fit only a small head.** A rolling window of ~21–28 days (~500–670 hourly rows)
   is enough to fit a head of ~20–40 features and far too little to fit a representation.
2. **Regularise toward the base:** `loss = data_loss + lambda * ||theta - theta_base||^2`. Without this
   the head overfits one household's unusual fortnight and forgets the physics it was given.
3. **Promotion gate with automatic rollback.** A nightly head is adopted only if it beats the current
   one on a held-out recent window (last ~7 days) *and* stays inside its clamps; otherwise the
   previous model is kept and a reason is logged. Slow drift into nonsense is the failure mode to
   design against, not a bad single night.

A ridge head is a small normal-equation solve, so the nightly job is closed-form, deterministic and
pure Python — no optimiser, no third-party runtime. Full base retrains stay offline and manual.

### 6.2 Nightly cadence: night is when the labels are exact

The fine-tune window is chosen deliberately. At night `pv = 0` exactly (§2.1), so the previous night
contributes **exact** load labels; the previous day contributes PV residuals. A nightly job therefore
sees the two label types it needs, at the moment both are freshest.

### 6.3 Local observation store (independent of the Home Assistant recorder)

**Status: built.** `tools/forecast_ml/snapshot.py` implements this section, with a self-test and a
twice-daily cron entry. See the tooling README for the schema rules and how to run it.

Home Assistant is not a training archive. Its short-term statistics expire on the recorder's own
schedule (~10 days observed), its purge settings are outside this add-on's control, and the main
`store.py` deliberately trims (plans to 200 rows). The training record must not live in either.

The deadline is real rather than theoretical: while this tooling was written, the available 5-minute
history shrank from 8 days to 5 between two probes a few hours apart.

| Rule | Why |
| --- | --- |
| **Separate SQLite file** on the `/data` volume (e.g. `forecast_ml.db`), own schema/migrations | never subject to the main store's trims; can be purged independently |
| **Store raw observables, never features** | feature engineering will change; raw data must survive it |
| **Append-only, unique on `(ts, role, source)`** | live capture and historical backfill can both write without clobbering each other |
| **Record what was forecast, next to what happened** | the scoring archive that proves (or disproves) improvement, and the input to the promotion gate |
| **Harvest at a cadence faster than the recorder expires** | twice daily; a 5-minute sweep is the only way to hold on to short-term statistics |
| **Roles are meanings, not entities** | remapping hardware must not reshape the training table |
| **Monthly compacted export** (e.g. `csv.gz`) | survives beyond Home Assistant snapshots and can be pulled into the repo tooling for offline training |

Columns are the observables of §1 plus battery power/SoC, ambient temperature, cloud, observed
irradiance and sun geometry, each row carrying `source` (live / backfill) and a quality flag.

Storage is not a concern: 5-minute sampling is ~105k rows/year and hourly backfill is ~3.3k rows for
the observed window. A decade fits comfortably.

**Privacy:** this is live household data. It stays under the ignored `data/` directory and out of the
public repository; the entity-id mapping lives outside the repository entirely. Only base-model
artifacts — trained on public corpora — are ever committed.

The forecast archive is **built** (SPEC.md §7.4, `forecast_archive`, `SCHEMA_VERSION` 3). It is the
add-on's own table rather than a harvest of `/api/v1/forecast`: rows are written where the planner
already has the corrected forecast in hand, which is both simpler and more accurate than reading it
back off an HTTP surface, and it works without the ingress-only round trip. Skill is read with
`Store.forecast_accuracy` (hourly means of both sides, per source).

**What is not done here:** there is no HTTP route exposing that score, so today the numbers live in the
provider status (`model`, `fine_tune_head`) and in the database. That is deliberate for this release —
the archive starts accumulating the moment the add-on runs, and delaying the deploy to build a view
would have cost days of exactly the record that cannot be reconstructed afterwards. On this
installation the solar series will stay empty until a `solar.measured_entity` is mapped, because
scoring PV against a column nothing measured would be the most flattering possible bug.

### 6.4 Public corpora for the base model

A base model is only useful for what a single installation cannot observe: cold start, and the
season it has never seen (§1). It has to be trained on data where **PV and load are separately
metered**, because that separation is the label this whole design is trying to reconstruct.

| Corpus | Households | Resolution | Has PV separately? | Cost / access |
| --- | --- | --- | --- | --- |
| **Utrecht 175 residential PV systems** (Utrecht University, 2014–2017) | 175 systems | **1 min** | PV only (no load) — but **per-system DC *and* AC capacity, tilt, azimuth, yield, location** | open, **CC-BY-4.0** |
| Ausgrid Solar Home Electricity Data | 300 homes, 3 years (half-hourly) + 2,657 monthly | 30 min | **yes — gross metered generation**, plus consumption, plus generator capacity and postcode | free, open (data.gov.au) |
| PVOutput.org | thousands of real systems | 5/15 min | PV only (no load) | free account + API key; **check terms** |
| NREL PVDAQ | ~100 systems | ~15 min | PV only; some sites carry **co-located irradiance** | free (open data catalog) |
| Pecan Street Dataport | 1,000+ homes | 1 min | **yes — separately metered solar**, circuit-level | gated, paid tiers |
| Load-only corpora (e.g. Low Carbon London ~5.5k homes, REFIT, UK-DALE, UCI) | large | 1–30 min | no | free |
| Dutch Residential Energy Dataset (DRED) | **1** home | 1 s–1 min | no | free |
| Synthetic profile sets (e.g. ResStock-style) | very large | sub-hourly | simulated | free |

Irradiance, a different role from household data: **PVGIS-SARAH2** (EC JRC) serves hourly satellite irradiance
for any European point from ~2005, free and scriptable, and is a better physics input for a Dutch site
than the coarser reanalysis behind NASA POWER. **National** validation series for the Netherlands
exist too (the Nationaal Energie Dashboard, and TenneT via ENTSO-E: 15-minute solar outturn, 2019→).
They are aggregate behind-the-meter solar for the whole country — useful to check the modelled
national shape, useless for household-level disaggregation.

Per head:

* **PV head — train the base on Dutch data.** The Utrecht set is the closest match in the list to
  this installation's situation: real Dutch rooftop systems, 1-minute AC power, and metadata giving
  **DC and AC capacity separately**, which makes the **DC/AC ratio and inverter clipping observable**
  rather than assumed. Clipping is exactly the per-setup axis of §4, so this corpus is what turns
  §4's configurability into something the model can actually learn. Pair it with PVGIS irradiance to
  drive the physics and fit the residual. Two caveats: AC only (no DC-side or irradiance sensor), and
  2014–2017.
* **Load head — no substantial Dutch corpus exists.** DRED is a single household and the load-only
  corpora are UK/US. The base therefore supplies *shape* and weather response from a foreign climate,
  and everything Dutch-specific (heat pump, gas boiler replacement, occupancy) has to come from this
  installation's own meter and the nightly fine-tune of §6.1.

**The gap that shapes the whole design: no public corpus pairs separately metered PV *and* household
load for the Netherlands.** The PV half of this installation can be trained on genuinely local data;
the load half cannot, and must lean on local measurement.

**Normalise away what differs, learn what transfers.** PV is predicted as a *dimensionless ratio* to
the physics expectation, never as absolute kW, so capacity and location drop out. Load is predicted
as a *shape* normalised by a level scalar, with the level calibrated locally — magnitude varies by an
order of magnitude between households and must not be baked into the base.

Honest expectation: the base model's largest win is **cold start**. Today a fresh install with no
usable history runs a flat `fallback` profile; a base model replaces it with a physically plausible
forecast on day one. For this installation specifically, where the observed window is a single summer
half-year, the base also supplies the seasonal structure the local data cannot.

**Licensing decides what may ship.** The add-on is public, and model weights are derived from their
training data, so corpus choice is a distribution decision, not a technical one. The Utrecht set is
**CC-BY-4.0** — shippable with attribution. PVOutput carries commercial-use terms and Pecan Street
is under a redistribution-restricted agreement; those may inform local experiments but must stay out
of any committed artifact. Ausgrid is open. The artifact should record corpus provenance so this stays
checkable.

## 7. Deployment

* Config flags default **off**; the add-on ships inert as it does today.
* Missing, corrupt, or stale artifact → the exact current code path, byte-for-byte. No new failure
  mode is introduced by the model.
* Deterministic: identical inputs produce byte-identical forecasts; asserted by test.
* Output bounded: `0 <= pv <= P_inv_limit`, residual ratio within its clamps, load non-negative.
* `ProviderStatus.detail` gains `model_version`, `model_trained_at`, `model_age_days`,
  `config_hash_match`. Existing reason slugs are frozen; any new slug is a SPEC amendment.
* **Live nowcast (the main operational win).** The same model run against the live meter at sampling
  cadence yields a continuously updated estimate of *today's* actual load level and PV output. That
  directly replaces the 7-day level `scale` in `load.py` and the 24-hour EWMA factors in `solar.py`,
  and current-slot accuracy is where the DP's decisions are actually made.

## 8. Evaluation and gates

Rolling-origin walk-forward over whole days; never random splits. `backtest.py` reports against the
**current implementation** as the baseline, not against a naive one.

Metrics: MAE and RMSE; pinball loss at 0.1/0.5/0.9; and the decision-relevant one — euros saved in a
replay of the historical window through the existing DP.

Shipping gates, all of which must pass:

1. Beats the current implementation on pinball loss on held-out days **and** on the held-out season
   edge (the observed window is summer-half; performance in its spring/autumn shoulders is the
   extrapolation test that matters).
2. Beats it on euros saved in replay.
3. Determinism, bounds, and fail-safe-fallback tests above.
4. Audit clean, plus the existing pytest/UI gates, before any release.

### 8.1 Ground truth does not exist, so aggregate gates carry the weight

There is no PV measurement on this installation, so a model can reconstruct the meter perfectly while
splitting it wrongly. Hourly error cannot detect that. The following falsifiable aggregate checks are
therefore **required**, not optional:

| Check | Expectation |
| --- | --- |
| Implied specific yield (kWh per kWp per year) | within a plausible range for the site's latitude and climate |
| Implied daily load energy | consistent with the configured base level |
| Monthly energy balance | closes, with no systematic PV↔load leakage |
| Implied clipping hours | consistent with the configured DC/AC ratio and physics model |
| Night load labels | reproduce the meter exactly (a hard invariant, not a metric) |

A model that passes hourly error but fails any of these is rejected. One caveat learned the hard way
while building the PV model: the yield check must compare measured and predicted **on identical
filtered rows**, because raw per-system data coverage varies (median 80.6% over the four-year merge)
and a naive ratio against a published annual figure silently measures coverage rather than skill.

## 9. Known limits

* Winter behaviour is unobserved; PV seasonality rests on the physics prior, and the load's heating
  response is fitted from a mild-temperature tail only. Expect the first full-year retrain to move
  both models materially.
* Daytime load under high PV is weakly identified by construction (§2.2).
* Broken-cloud days are ambiguous hour-by-hour; only the daily aggregate is trustworthy.
* Unusual-load days (EV, guests) are partly absorbed into the PV residual. A robust loss and
  day-level level terms mitigate but do not remove this.
* The outdoor-temperature series needs validation before use; NASA POWER `T2M` is the fallback and
  has full history.

## 10. Rollout

1. ~~**`tools/forecast_ml/snapshot.py` first.**~~ **Built** (§6.3). Data was expiring while this was
   written — 5-minute history went from 8 days to 5 between two probes hours apart.
2. ~~Base model from public corpora, validated on held-out *houses*.~~ **Built and shipped** (§1.1,
   SPEC §7.1.4): 5.102% of nameplate on held-out systems, 5.548% on held-out years.
3. ~~Backtest harness.~~ **Built** (§7.4 — the archive measures it in service rather than in advance).
4. ~~PV residual model behind its own flag.~~ **Built and shipped**, opt-in by construction: it is a
   `solar.source` value, so no install changes behaviour until someone selects it.
5. ~~Nightly fine-tune head, with the promotion gate and rollback.~~ **Built** (§7.5). It will report
   `insufficient` until roughly three days of scored slots exist, which is the honest starting state.
6. **Load model — not started**, and now the only substantial piece left. It needs a household-load
   corpus for the cold start (Ausgrid is the free one that meters PV and consumption separately), or
   an argument that the §7.2 tiers make a learned load head unnecessary. **Named load devices are the
   local half of this and are shipped**: they do not give the base profile a corpus, but they stop a
   heat pump and an EV charger from being averaged into one compromise slope, which is the part of
   the load error a corpus could not have fixed either. The subtraction is what makes them safe — the
   base is learned from the total minus every measurable device, and an unmeasurable device is left
   inside the base and reported rather than added on top of itself.
7. **A scoring surface** — an HTTP route over `Store.forecast_accuracy`, plus the UI card that shows
   the user whether the learned model is actually beating the physics it replaced.

Steps 1 and 3 are useful even if every model in this document is abandoned, and step 1 is the only
one that is time-critical.
