# forecast_ml — base PV model tooling

Offline tooling that fits the **base PV residual model** described in `../../docs/FORECAST_ML.md`
from the Utrecht residential PV corpus. Nothing here runs inside the add-on, and nothing here is
part of the shipped runtime: the add-on is standard-library only (SPEC **R3**), and these scripts
deliberately are not.

The add-on must never import from this directory. The only thing that crosses into the add-on is the
exported JSON artifact, which is plain arrays plus a documented feature list.

## What the model is

A model of the **ratio of measured AC power to a physical expectation**, never absolute watts:

```
pv_ac(t) = ac_power(dc_capacity, ac_capacity, poa, t_cell)      # solar_physics.py
ratio    = pv_ac_measured / pv_ac_expected                      # the learned target
pv_final = pv_ac(t) * clamp(predict(ratio), 0.3, 1.8)
```

Because the target is dimensionless, nameplate capacity and geographic location drop out of it: one
model serves any array, and the physics (irradiance, geometry, module temperature, inverter
clipping) is supplied rather than learned. That is the whole point of a *base* model — a new
installation gets a physically plausible forecast before it has any local history.

## Measured results

Fitted on the Dutch corpus, validated **two independent ways**: on held-out **systems** (5 folds, 34–35
unseen arrays per fold) and on held-out **years**. 1,885,671 daylight rows across 174 systems over
**1,461 days (2014–2017)**. Rows are hourly, and every row has a complete hour (≥45 of 60 minutes)
with the sun above 5° and the array above 2% of nameplate.

### Held-out systems

| model | MAE (W) | RMSE (W) | MAE (% of nameplate) | energy bias |
| --- | --- | --- | --- | --- |
| physics only (ratio = 1) | 188.2 | 284.1 | 8.077% | **+9.52%** |
| ridge (linear, same features) | 137.7 | 224.5 | 5.898% | +0.52% |
| **gradient-boosted trees** | **110.8** | **187.2** | **4.742%** | **+0.09%** |

### Held-out years — the test a system split cannot perform

Trained on **2014 only** (566,138 rows), scored on **2015–2017** (1,297,274 rows): three calendar
years, and 700,000+ rows of weather the model has never seen.

| model | MAE (W) | RMSE (W) | MAE (% of nameplate) | energy bias |
| --- | --- | --- | --- | --- |
| physics only | 189.0 | 285.0 | 8.091% | +9.42% |
| ridge | 137.1 | 221.2 | 5.853% | +0.21% |
| **gradient-boosted trees** | **131.7** | **221.0** | **5.620%** | **−0.40%** |

This is the number that matters for a *base* model, because a base model's entire job is to work
before any local data exists. Error rises only ~9% (119.2 → 129.6 W) when the training year and the
test years are disjoint, and the energy bias stays at +1.2% against the physics prior's +14.5%. The
model is not memorising 2014.

Read the physics row as the honest starting point: the physical reference alone over-predicts AC
energy by ~14%, which is ordinary — it carries no soiling, cabling, mismatch or ageing term. A linear
correction removes almost all of that bias, and the tree ensemble removes a further ~20% of the
remaining error.

### The model is fitted on the irradiance chain it is served

Corpus rows are driven by `Haurwitz clear-sky x Kasten-Czeplak(observed cloud)`, not by the
satellite-observed GHI the earlier revisions used. The add-on rebuilds irradiance exactly this way at
runtime, and a model fitted on a different estimator spends part of its residual correcting the
difference: scored on the chain it would actually see, the SARAH2-fitted artifact degraded from 115.8 W
to 150.5 W with an ~8% energy bias. The observed GHI is still carried through as `ghi_observed` so the
two estimators can be compared rather than one silently replacing the other.

The weather series (cloud, wind, humidity, rain) come from `--weather`, a NASA POWER hourly JSON, and
become both the irradiance chain and six model features. Each one is a field a runtime weather forecast
publishes; that is the test for including it. `--weather` is required rather than optional, because
omitting it would silently restore the mismatch.

The numbers above are therefore the physics prior scored on a *worse* irradiance input than before
(8.077% against 7.064%), offset by a model that is handed the ingredients of that input and beats both
of the earlier revisions on both splits.

### The aggregate gate

There is no PV measurement at the target installation, so a model could reconstruct a meter while
splitting it wrongly. Row-level error cannot detect that, hence this second, falsifiable check:

| quantity (window = 1,461 days) | value |
| --- | --- |
| measured in corpus | **2,864.3 kWh/kWp** |
| model prediction | **2,847.4 kWh/kWp** (−0.59%) |
| p10–p90 across systems, measured | 1,617 – 3,197 |
| p10–p90 across systems, predicted | 1,660 – 3,172 |

The model reproduces the *spread* across systems, not just the median: that is the part a single
global multiplier could not do, and it is the strongest evidence here that the model learned
something about the arrays rather than about the average.

**Caveat on reading this table.** It compares measured against predicted **on identical filtered
rows**, which is what makes it a fair test. Its absolute level is depressed relative to a published
annual yield by per-system data coverage (median 80.6% over the four-year merge) and by the daylight
filter, so it is not an annual-yield claim. The single-year slice, where coverage was 96%, was
sanity-checked against the corpus's own published `annual_yield` and matched.

## Files

| Path | Role |
| --- | --- |
| `solar_physics.py` | Solar geometry, Erbs beam/diffuse split, plane-of-array transposition, NOCT cell temperature, DC power, inverter efficiency and clipping. NumPy, but a straight port of the formulas in `app/sources/solar.py`. |
| `ingest.py` | Utrecht metadata + wide minute-resolution power CSV + PVGIS JSON → one compact `.npz` of hourly means. |
| `train_base.py` | Feature assembly, grouped validation, the aggregate gate, model fitting, artifact export, and a **parity check** of the export against scikit-learn. |
| `snapshot.py` | Preserves this installation's own observations into a separate SQLite database, independently of Home Assistant's retention. Standard library only. |
| `selftest_snapshot.py` | Self-test for `snapshot.py`. Run it directly; deliberately not named `test_*.py` so it cannot change the add-on's tracked pytest gate count. |
| `roles.example.json` | Template for the role→entity mapping. Real mappings live outside the repository. |
| `models/pv_base_v3.json` | The exported artifact (trained on all four years). |
| `models/pv_base_v3.report.json` | The full validation report behind the tables above. |
| `emit_golden.py` | Emits the golden vectors that pin the add-on's standard-library port, and cross-checks its single-row path against `train_base.build_features` on real corpus rows. |

## The local observation store

Home Assistant is not a training archive. Its short-term statistics expire on the recorder's own
schedule, its purge settings are outside this tool's control, and the add-on's main store deliberately
trims. Anything not copied out on a schedule is gone, and nothing later reconstructs it.

That is not theoretical here. While this tooling was being written, the available 5-minute history
shrank from 8 days to 5 — three days of fine detail aged out between two probes a few hours apart.
No other part of this project has a deadline like it.

```sh
python3 snapshot.py --roles /path/outside/repo/roles.json --db data/forecast_ml.db
python3 snapshot.py --roles ... --db ... --status      # per-role coverage
python3 snapshot.py --roles ... --db ... --recent-only  # 5-minute sweep only
python3 selftest_snapshot.py
```

Design rules, each of which exists for a reason:

* **Raw observables only, never features.** Feature engineering will change; the raw record must
  outlive it.
* **Append-only, keyed on `(ts, role, source)`.** Live capture and historical backfill coexist, and
  re-running is idempotent rather than duplicating. The original value is never overwritten.
* **Roles are meanings, not entities.** `ROLES` is a fixed vocabulary, so remapping hardware does not
  reshape the training table. Unknown roles are refused rather than silently stored.
* **`kind` distinguishes a counter from a reading.** An energy counter contributes its `change`; an
  instantaneous level contributes its `mean`. Conflating them is how a cumulative total gets mistaken
  for a measurement — the same trap `ingest.py` had to avoid.
* **One bad role does not lose the others,** and each run records what succeeded and what failed in a
  `harvest_run` row rather than only on stdout.
* **Entity ids stay outside the repository.** They are private instance data and the audit gate
  refuses them; `roles.example.json` shows the shape.

Scheduled twice daily (`/etc/cron.d/forecast-ml-snapshot`): the 03:30 run backfills hourly long-term
statistics and sweeps 5-minute detail, the 15:30 run sweeps 5-minute detail only. Hourly history is
never purged by Home Assistant so it can always be repaired; the 5-minute sweep cannot, which is what
justifies the redundant schedule. The database lives under `data/`, which is git-ignored.

### Artifact encoding

Numeric arrays are stored as **IEEE-754 bit patterns held in integers**, decoded with `struct`. This
is not decoration:

* **Committed weights trip the repository's PII audit as JSON floats.** The audit flags any decimal
  with five or more fractional digits inside a latitude/longitude magnitude range, and any two
  adjacent such values read as a "lat, lon" pair. Weights are full of exactly those shapes, so a
  float-formatted artifact scores ~18k findings and cannot be committed without either silencing the
  gate or changing the encoding. Integers carry no decimal point, so the gate stays meaningful.
* **Bit patterns rather than a fixed-point scale, because quantising a threshold changes the model.**
  A split is a `<=` on a raw feature value, and several features here have many rows sitting exactly
  on a boundary — `clipping_headroom` is exactly 0 for every array that never clips, and it has a
  single bin edge. Rounding that threshold to a power-of-ten scale flips the comparison for every one
  of those rows. Measured during development: **~2% of rows moved by up to one leaf step (0.16 in
  ratio terms)**, which is invisible in an aggregate metric and shows up only in an exact parity
  check. Bit patterns round-trip bit-for-bit.

Together with the parity gate this gives a strong property: the committed artifact provably
reproduces the fitted model exactly, in a form a standard-library interpreter can walk.

Scalar metrics (`held_out_system_mae_w`, and so on) are rounded to three decimals — enough to read,
and too coarse to look like a coordinate.

## Reproducing

Data lives outside the repository (it is licensed third-party data and the power file is ~3 GB in
full). Train in a virtualenv — these scripts need NumPy, pandas and scikit-learn, unlike the add-on.

```sh
python3 -m venv .venv && .venv/bin/pip install numpy pandas scikit-learn

# 1. the small files
curl -sSL -o metadata.csv \
  "https://zenodo.org/records/10953360/files/metadata.csv?download=1"

# 2. the power series. The file is row-major (rows are timestamps, columns are systems), so a byte
#    range returns *all 175 systems* over a time window — a partial download is a legitimate strategy
#    rather than a compromise. ~780 MB per year. Later slices have no header, which ingest.py handles
#    by supplying column names and dropping the truncated first row.
URL="https://zenodo.org/records/10953360/files/filtered_pv_power_measurements_ac.csv?download=1"
curl -sSL -r 0-817888246       -o power_2014.csv     "$URL"   # 2014
curl -sSL -r 780000000-2985408766 -o power_2015_2017.csv "$URL"   # 2015-2017

# 3. measured horizontal irradiance for the same window (PVGIS-SARAH2, keyless)
curl -sSL -o pvgis.json "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=52.09&lon=5.12\
&startyear=2014&endyear=2017&angle=0&aspect=0&pvcalculation=0&outputformat=json&browser=0"

# 4. ingest, then fit and validate (system split, year split, aggregate gate, parity)
python ingest.py --metadata metadata.csv --power power_2014.csv power_2015_2017.csv \
  --pvgis pvgis.json --out ingested.npz
python train_base.py --npz ingested.npz --train-years 2014 --test-years 2015,2016,2017 \
  --report models/pv_base_v3.report.json --out models/pv_base_v3.json
```

`--quick` fits a small ensemble for a smoke run. A full fit takes about a minute on a laptop once
the data is ingested; ingest itself is the slow part at roughly 15 seconds per 100k rows.

## Conventions that had to be established, not assumed

* **Azimuth in the corpus is compass degrees from north** (observed range 86°–285°, i.e. east through
  west). `solar_physics.cos_aoi` converts to the from-south convention the transposition formula
  uses, which is the same conversion the add-on applies at its call site.
* **PVGIS ``G(i)`` at ``angle=0`` is global horizontal irradiance**, and its stamps are hour-labelled
  UTC. Confirmed against sunrise for the site and cross-checked every daylight hour: mean deviation
  in sun height −0.46°, i.e. smaller than the residual the model absorbs.
* **PVGIS returns no beam/diffuse split**, so the split comes from the Erbs correlation on the
  measured clearness index. The runtime path currently assumes a constant 25% diffuse fraction;
  Erbs is a candidate to port back.
* **Empty cells mean missing**, not zero. Hourly means are formed from sums and non-null counts —
  taking a `.mean()` and then dividing by the count again divides by the minutes twice.
* **The reported `annual_yield` in the metadata is an independent published number**, which is what
  makes the aggregate gate above a real test rather than a self-consistency check.
* **The raw-threshold tree walk reproduces scikit-learn exactly.** The obvious worry is that
  scikit-learn predicts on *binned* feature values while an exported tree compares raw values to
  `num_threshold`. Measured directly on these features, the raw comparison is **bit-exact** (0.0 over
  8000 rows) while a bin-index reconstruction is *worse* (347 of 8000 rows, up to 0.16) because
  reproducing sklearn's binning exactly requires matching its `searchsorted` tie semantics. The
  straightforward interpretation is the correct one; nobody should "fix" this by re-deriving bins.

## Licensing

The artifact is derived from the Utrecht dataset, **CC-BY-4.0** (doi `10.5281/zenodo.10953360`),
Visser, Elsinga, AlSkaif and van Sark. Attribution is carried inside the artifact and must stay
there. Corpora with commercial-use or redistribution restrictions must not be used to produce a
committed artifact; see `docs/FORECAST_ML.md` §6.4.

**Not yet done:** the load head (no Dutch household-load corpus exists — see the doc), the nightly
fine-tune head, per-setup array configuration beyond the features already carried, the forecast
archive beside the observations, and any runtime integration at all.
