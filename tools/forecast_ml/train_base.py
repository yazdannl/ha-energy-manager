"""Fit and validate the base PV residual model on the Dutch corpus.

The model predicts a **dimensionless ratio** of measured AC power to a physical expectation, not
absolute watts. That is what lets one model serve any array: capacity and location drop out of the
target, and the physics (irradiance, geometry, temperature, clipping) is supplied rather than
learned.

Validation is by **held-out systems**, never held-out rows of a system the model has already seen.
Rows from one array are strongly correlated; a random row split would report a flattering number that
says nothing about a new installation, which is the only thing a base model is for.

Two falsifiable aggregate gates are reported alongside the row-level error, because the target
installation has no PV measurement at all and a model can reconstruct a meter while splitting it
wrongly:

* **specific yield** — predicted kWh per kWp for the window against the corpus's own reported yield
* **energy bias** — total predicted energy against total measured energy over the fold

Usage:
    python train_base.py --npz ingested.npz --out pv_base_v1.json [--quick]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import time
from pathlib import Path

import numpy as np

import solar_physics as sp

#: Below this fraction of nameplate the ratio is dominated by noise and cloud-edge timing, so those
#: rows are excluded rather than fitted.
MIN_LOAD_FRACTION = 0.02
#: Sun below this elevation: long atmospheric path, heavy shading, no useful signal.
MIN_ELEVATION_DEG = 5.0
#: A full hour must be present, not a sliver either side of a dropout.
MIN_MINUTES_PER_HOUR = 45
#: Ratios beyond this are data faults, not physics.
RATIO_CLIP = 1.5
#: Clamps on model output, matching the existing bias-correction bounds in the add-on.
RESIDUAL_MIN, RESIDUAL_MAX = 0.3, 1.8

#: The artifact stores its numeric arrays as exact **IEEE-754 bit patterns held in integers**, not as
#: JSON floats. Two reasons, and the second is the binding one:
#:   1. it removes float-formatting noise, so a rebuild that changes nothing produces no diff;
#:   2. the repository's PII audit flags any decimal with five or more fractional digits inside a
#:      latitude/longitude magnitude range, and any two adjacent such values read as a "lat, lon"
#:      pair. Weights are full of those shapes, so a float-formatted artifact scores ~18k false
#:      positives and cannot be committed without either silencing the gate or changing the encoding.
#: Encoding as integers is the honest fix: the numbers were never coordinates.
#:
#: Bit patterns rather than a fixed-point scale, because **quantising a threshold changes the model**.
#: A split is a `<=` on a raw feature value, and several features here have many rows sitting exactly
#: on a boundary (``clipping_headroom`` is exactly 0 for every array that never clips, and it has a
#: single bin edge). Rounding such a threshold to a power-of-ten scale flips the comparison for every
#: one of those rows — measured at ~2% of rows and one leaf step, which is invisible in an aggregate
#: metric and obvious only in an exact parity check. Bit patterns round-trip bit-for-bit.
_BITS_FORMAT = "<d"
_BITS_UNSIGNED = "<Q"

FEATURE_NAMES = [
    "sun_elevation_deg",
    "cos_zenith",
    "cos_incidence",
    "sun_azimuth_sin",
    "sun_azimuth_cos",
    "tilt_deg",
    "tilt_sin",
    "ghi_wm2",
    "poa_wm2",
    "clearness_index",
    "diffuse_fraction",
    "dni_wm2",
    "t_ambient_c",
    "t_cell_c",
    "dc_capacity_kwp",
    "ac_capacity_kwp",
    "dc_ac_ratio",
    "clipping_headroom",
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
]


def build_features(npz: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Assemble the training matrix.

    Returns ``(features, target_ratio, expected_ac_w, system_index, row_hour)``; the row hour lets a
    caller split by calendar year, which is the only honest way to ask whether the model transfers to
    a period it has never seen.
    """
    hours = npz["hours"].astype("int64")
    power = npz["power"].astype("float64")
    minutes = npz["minutes"].astype("float64")
    n_hours, n_systems = power.shape

    ghi = npz["ghi"].astype("float64")
    t2m = npz["t2m"].astype("float64")
    latitude = npz["latitude"].astype("float64")
    longitude = npz["longitude"].astype("float64")
    tilt = npz["tilt"].astype("float64")
    azimuth = npz["azimuth"].astype("float64")
    dc_capacity = npz["dc_capacity"].astype("float64")
    ac_capacity = npz["ac_capacity"].astype("float64")

    moments = hours * 3600
    seconds_into_day = moments - (moments // 86400) * 86400
    fractional_hour = seconds_into_day / 3600.0
    # True day of year, leap-aware. This must be the real calendar day: an earlier version used
    # ``days_since_epoch % 365 + 1``, which is offset by 11-13 days *and* drifts by a day per year as
    # leap days accumulate, so the seasonal feature meant something different in every training year.
    # Retraining was cheaper than cementing that into a shipped artifact.
    stamps = np.array(moments, dtype="datetime64[s]")
    day_of_year = (
        (stamps - stamps.astype("datetime64[Y]")).astype("timedelta64[D]").astype("int64") + 1
    )
    hour_of_day = ((moments // 3600) % 24).astype("float64")

    # Geometry: declination and hour angle are per (hour, system), because longitude differs.
    declination = np.repeat(sp.declination_rad(day_of_year)[:, None], n_systems, axis=1)
    hour_angle = np.stack(
        [
            sp.hour_angle_rad(latitude[index], longitude[index], day_of_year, fractional_hour)
            for index in range(n_systems)
        ],
        axis=1,
    )
    elevation = sp.sun_elevation_deg(latitude[None, :], declination, hour_angle)
    sun_azimuth = sp.sun_azimuth_deg(latitude[None, :], declination, hour_angle)
    incidence = sp.cos_aoi(
        latitude[None, :], declination, hour_angle, tilt[None, :], azimuth[None, :]
    )
    cos_zenith = np.cos(np.radians(90.0 - elevation))

    ghi_grid = np.repeat(ghi[:, None], n_systems, axis=1)
    t2m_grid = np.repeat(t2m[:, None], n_systems, axis=1)
    tilt_grid = np.repeat(tilt[None, :], n_hours, axis=0)
    dc_grid = np.repeat(dc_capacity[None, :], n_hours, axis=0)
    ac_grid = np.repeat(ac_capacity[None, :], n_hours, axis=0)

    poa, clearness, diffuse_fraction, dni = sp.plane_of_array(
        ghi_grid, cos_zenith, incidence, tilt_grid
    )
    t_cell = sp.cell_temperature(t2m_grid, poa)
    expected = sp.ac_power(dc_grid, ac_grid, poa, t_cell)
    unclipped = sp.dc_power(dc_grid, poa, t_cell) * sp.ETA_INV
    clipping_headroom = np.maximum(unclipped / np.maximum(ac_grid, 1e-9) - 1.0, 0.0)

    usable = (
        np.isfinite(power)
        & (minutes >= MIN_MINUTES_PER_HOUR)
        & (elevation > MIN_ELEVATION_DEG)
        & (expected > MIN_LOAD_FRACTION * ac_capacity[None, :])
    )
    rows, columns = np.nonzero(usable)
    if rows.size == 0:
        raise SystemExit("no usable rows: check the ingest output")

    azimuth_from_south = np.radians((azimuth + 180.0) % 360.0)
    dc_ac_ratio = dc_capacity / np.maximum(ac_capacity, 1e-9)

    features = np.column_stack(
        [
            elevation[rows, columns],
            cos_zenith[rows, columns],
            incidence[rows, columns],
            np.sin(np.radians(sun_azimuth[rows, columns])),
            np.cos(np.radians(sun_azimuth[rows, columns])),
            tilt[columns],
            np.sin(np.radians(tilt[columns])),
            ghi[rows],
            poa[rows, columns],
            clearness[rows, columns],
            diffuse_fraction[rows, columns],
            dni[rows, columns],
            t2m[rows],
            t_cell[rows, columns],
            dc_capacity[columns] / 1000.0,
            ac_capacity[columns] / 1000.0,
            dc_ac_ratio[columns],
            clipping_headroom[rows, columns],
            np.sin(2.0 * np.pi * hour_of_day[rows] / 24.0),
            np.cos(2.0 * np.pi * hour_of_day[rows] / 24.0),
            np.sin(2.0 * np.pi * day_of_year[rows] / 365.0),
            np.cos(2.0 * np.pi * day_of_year[rows] / 365.0),
        ]
    ).astype("float64")
    _ = azimuth_from_south  # retained for parity with the runtime feature contract

    actual = power[rows, columns]
    target = np.clip(actual / expected[rows, columns], 0.0, RATIO_CLIP)
    return features, target, expected[rows, columns], columns, hours[rows]


def fit_models(x_train, y_train, quick: bool):
    """Ridge baseline and a gradient-boosted model. Both export to plain arithmetic."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    ridge = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    ridge.fit(x_train, y_train)

    # Histogram-based boosting bins each feature once and is two orders of magnitude faster than
    # exact-split boosting at this row count, which matters because every fold refits.
    gbt = HistGradientBoostingRegressor(
        max_iter=120 if quick else 400,
        max_depth=3 if quick else 5,
        learning_rate=0.1 if quick else 0.06,
        min_samples_leaf=50 if quick else 300,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=0,
    )
    gbt.fit(x_train, y_train)
    return ridge, gbt


def _to_fixed(value: float) -> int:
    """Exact IEEE-754 bit pattern of a float, as an integer."""
    return int(struct.unpack(_BITS_UNSIGNED, struct.pack(_BITS_FORMAT, float(value)))[0])


def _from_fixed(bits: int) -> float:
    """Inverse of :func:`_to_fixed`. Round-trips bit-for-bit."""
    return float(struct.unpack(_BITS_FORMAT, struct.pack(_BITS_UNSIGNED, int(bits)))[0])


def _from_fixed_array(values) -> np.ndarray:
    return np.array([_from_fixed(v) for v in values], dtype="float64")


def export_gbt(model, feature_names: list[str], extra: dict) -> dict:
    """Flatten a scikit-learn histogram gradient-boosting regressor into portable arrays.

    Each tree becomes parallel lists of split feature index, encoded threshold, left/right child and
    encoded leaf value, which a standard-library interpreter can walk without scikit-learn or NumPy.
    The constructor applies its own shrinkage to every leaf, so there is no outer learning rate to
    multiply by at inference time; the prediction is the baseline plus the reached leaves.

    Numeric arrays are emitted as IEEE-754 bit patterns held in integers (see above).
    """
    trees = []
    for stage in model._predictors:
        nodes = stage[0].nodes
        trees.append(
            {
                "feature": nodes["feature_idx"].astype(int).tolist(),
                "threshold_bits": [_to_fixed(v) for v in nodes["num_threshold"]],
                "left": nodes["left"].astype(int).tolist(),
                "right": nodes["right"].astype(int).tolist(),
                "value_bits": [_to_fixed(v) for v in nodes["value"]],
                "is_leaf": nodes["is_leaf"].astype(int).tolist(),
                "missing_left": nodes["missing_go_to_left"].astype(int).tolist(),
            }
        )
    rounded = {
        key: round(float(value), 3)
        for key, value in extra.items()
        if isinstance(value, float)
    }
    return {
        "kind": "hist_gradient_boosted_trees_exact_bits",
        "feature_names": list(feature_names),
        "baseline_bits": _to_fixed(float(np.ravel(model._baseline_prediction)[0])),
        "residual_clamp": [RESIDUAL_MIN, RESIDUAL_MAX],
        "trees": trees,
        **extra,
        **rounded,
    }


def _walk_vectorised(tree: dict, features: np.ndarray, out: np.ndarray) -> None:
    """Add one tree's contribution to ``out`` for every row. Fast path used for metrics."""
    feature = np.asarray(tree["feature"])
    threshold = _from_fixed_array(tree["threshold_bits"])
    left = np.asarray(tree["left"])
    right = np.asarray(tree["right"])
    value = _from_fixed_array(tree["value_bits"])
    is_leaf = np.asarray(tree["is_leaf"]).astype(bool)
    missing_left = np.asarray(tree["missing_left"]).astype(bool)

    node = np.zeros(len(features), dtype="int64")
    done = np.zeros(len(features), dtype=bool)
    order = np.arange(len(features))
    while not done.all():
        current = order[~done]
        leaf_here = is_leaf[node[current]]
        if leaf_here.any():
            selected = current[leaf_here]
            out[selected] += value[node[selected]]
            done[selected] = True
        remaining = current[~leaf_here]
        if remaining.size == 0:
            continue
        here = node[remaining]
        observed = features[remaining, feature[here]]
        go_left = np.where(
            np.isnan(observed), missing_left[here], observed <= threshold[here]
        )
        node[remaining] = np.where(go_left, left[here], right[here])


def predict_artifact(artifact: dict, features: np.ndarray) -> np.ndarray:
    """Evaluate the exported artifact. This defines the model the add-on will run."""
    out = np.full(len(features), _from_fixed(artifact["baseline_bits"]), dtype="float64")
    for tree in artifact["trees"]:
        _walk_vectorised(tree, features, out)
    return out


def predict_artifact_scalar(artifact: dict, features: np.ndarray) -> np.ndarray:
    """A plain scalar walk of the same artifact: the form a standard-library port would take.

    The parity gate checks this against :func:`predict_artifact`, so the fast path used to compute
    metrics is proven to agree with the simple, deployable one.
    """
    trees = artifact["trees"]
    out = np.empty(len(features), dtype="float64")
    for row in range(len(features)):
        total = _from_fixed(artifact["baseline_bits"])
        for tree in trees:
            feature = tree["feature"]
            threshold = tree["threshold_bits"]
            left = tree["left"]
            right = tree["right"]
            leaf = tree["is_leaf"]
            node = 0
            while not leaf[node]:
                observed = features[row, feature[node]]
                if observed != observed:  # NaN never compares equal to itself
                    go_left = bool(tree["missing_left"][node])
                else:
                    go_left = observed <= _from_fixed(threshold[node])
                node = left[node] if go_left else right[node]
            total += _from_fixed(tree["value_bits"][node])
        out[row] = total
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npz", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument(
        "--train-years",
        default=None,
        help="comma-separated years to train on for a temporal split, e.g. 2014",
    )
    parser.add_argument(
        "--test-years",
        default=None,
        help="comma-separated years to test on for a temporal split, e.g. 2015,2016,2017",
    )
    parser.add_argument("--quick", action="store_true", help="small ensemble, for a fast smoke run")
    args = parser.parse_args()

    started = time.time()
    npz = dict(np.load(args.npz, allow_pickle=False))
    print(f"loaded {args.npz}: power {npz['power'].shape}")

    features, target, expected, systems, row_hours = build_features(npz)
    years = (
        np.array(row_hours * 3600, dtype="datetime64[s]").astype("datetime64[Y]").astype(int) + 1970
    )
    print(f"usable rows: {len(target):,} across {len(np.unique(systems))} systems")
    print(
        "rows per year: "
        + ", ".join(
            f"{year}: {int((years == year).sum()):,}" for year in np.unique(years)
        )
    )
    print(
        f"target ratio: mean {target.mean():.4f} median {np.median(target):.4f} sd {target.std():.4f}"
    )

    from sklearn.model_selection import GroupKFold

    folds = min(args.folds, len(np.unique(systems)))
    splitter = GroupKFold(n_splits=folds)
    ac_capacity = npz["ac_capacity"].astype("float64")
    dc_capacity = npz["dc_capacity"].astype("float64")
    observed_days = len(npz["hours"]) / 24.0

    fold_report = []
    harvest: list[dict] = []
    for fold, (train_index, test_index) in enumerate(splitter.split(features, target, systems)):
        ridge, gbt = fit_models(features[train_index], target[train_index], args.quick)
        # Score the *exported artifact*, not the scikit-learn object. The artifact is what ships, and
        # evaluating anything else would be measuring code that does not run.
        fold_artifact = export_gbt(gbt, FEATURE_NAMES, {})
        candidates = {
            "physics_only": np.ones(len(test_index)),
            "ridge": ridge.predict(features[test_index]),
            "gbt": predict_artifact(fold_artifact, features[test_index]),
        }
        row = {"fold": fold, "test_systems": int(len(np.unique(systems[test_index])))}
        for name, ratio in candidates.items():
            ratio = np.clip(ratio, RESIDUAL_MIN, RESIDUAL_MAX)
            predicted_w = ratio * expected[test_index]
            actual_w = target[test_index] * expected[test_index]
            error = predicted_w - actual_w
            normalised = error / np.maximum(ac_capacity[systems[test_index]], 1e-9)
            row[name] = {
                "mae_w": float(np.mean(np.abs(error))),
                "rmse_w": float(np.sqrt(np.mean(error ** 2))),
                "mae_pct_nameplate": float(np.mean(np.abs(normalised)) * 100.0),
                "energy_bias_pct": float(
                    np.sum(error) / max(np.sum(actual_w), 1e-9) * 100.0
                ),
            }
            if name == "gbt":
                for system in np.unique(systems[test_index]):
                    mask = systems[test_index] == system
                    dc_kwp = float(dc_capacity[system]) / 1000.0
                    if dc_kwp <= 0:
                        continue
                    harvest.append(
                        {
                            "predicted_kwh": float(np.sum(predicted_w[mask])) / 1000.0,
                            "actual_kwh": float(np.sum(actual_w[mask])) / 1000.0,
                            "reported_yield": float(npz["annual_yield"][system]),
                            "dc_kwp": dc_kwp,
                        }
                    )
        fold_report.append(row)
        print(
            f"  fold {fold}: {row['test_systems']} systems | "
            + " | ".join(
                f"{n} mae={row[n]['mae_w']:.0f}W bias={row[n]['energy_bias_pct']:+.2f}%"
                for n in ("physics_only", "ridge", "gbt")
            ),
            flush=True,
        )

    summary = {
        name: {
            key: float(np.mean([fold[name][key] for fold in fold_report]))
            for key in ("mae_w", "rmse_w", "mae_pct_nameplate", "energy_bias_pct")
        }
        for name in ("physics_only", "ridge", "gbt")
    }
    print("\n=== held-out-system summary (mean over folds, daylight rows only) ===")
    print(f"{'model':<14}{'MAE W':>9}{'RMSE W':>10}{'MAE %plates':>13}{'energy bias%':>14}")
    for name in ("physics_only", "ridge", "gbt"):
        entry = summary[name]
        print(
            f"{name:<14}{entry['mae_w']:>9.1f}{entry['rmse_w']:>10.1f}"
            f"{entry['mae_pct_nameplate']:>13.3f}{entry['energy_bias_pct']:>14.3f}"
        )

    # Temporal split: the question a grouped-by-system split cannot answer. A model that only ever
    # saw one year can still look excellent across unseen arrays and fail on unfamiliar weather, so
    # this trains on one set of calendar years and scores a completely different one.
    temporal = None
    if args.train_years and args.test_years:
        train_years = {int(value) for value in args.train_years.split(",")}
        test_years = {int(value) for value in args.test_years.split(",")}
        train_mask = np.isin(years, sorted(train_years))
        test_mask = np.isin(years, sorted(test_years))
        if not train_mask.any() or not test_mask.any():
            print(
                f"\ntemporal split skipped: {int(train_mask.sum())} training rows, "
                f"{int(test_mask.sum())} test rows"
            )
        else:
            ridge, gbt = fit_models(features[train_mask], target[train_mask], args.quick)
            temporal_artifact = export_gbt(gbt, FEATURE_NAMES, {})
            candidates = {
                "physics_only": np.ones(int(test_mask.sum())),
                "ridge": ridge.predict(features[test_mask]),
                "gbt": predict_artifact(temporal_artifact, features[test_mask]),
            }
            temporal = {}
            print(
                f"\n=== temporal split: train {sorted(train_years)} ({int(train_mask.sum()):,} rows)"
                f" -> test {sorted(test_years)} ({int(test_mask.sum()):,} rows) ==="
            )
            print(f"{'model':<14}{'MAE W':>9}{'RMSE W':>10}{'MAE %plates':>13}{'energy bias%':>14}")
            for name, ratio in candidates.items():
                ratio = np.clip(ratio, RESIDUAL_MIN, RESIDUAL_MAX)
                predicted_w = ratio * expected[test_mask]
                actual_w = target[test_mask] * expected[test_mask]
                error = predicted_w - actual_w
                normalised = error / np.maximum(ac_capacity[systems[test_mask]], 1e-9)
                entry = {
                    "mae_w": float(np.mean(np.abs(error))),
                    "rmse_w": float(np.sqrt(np.mean(error ** 2))),
                    "mae_pct_nameplate": float(np.mean(np.abs(normalised)) * 100.0),
                    "energy_bias_pct": float(np.sum(error) / max(np.sum(actual_w), 1e-9) * 100.0),
                }
                temporal[name] = entry
                print(
                    f"{name:<14}{entry['mae_w']:>9.1f}{entry['rmse_w']:>10.1f}"
                    f"{entry['mae_pct_nameplate']:>13.3f}{entry['energy_bias_pct']:>14.3f}"
                )

    predicted_yield = np.array([h["predicted_kwh"] / h["dc_kwp"] for h in harvest])
    actual_yield = np.array([h["actual_kwh"] / h["dc_kwp"] for h in harvest])
    reported_yield = np.array([h["reported_yield"] for h in harvest])
    print(f"\n=== specific yield over {observed_days:.0f} observed days ===")
    print(f"  measured in corpus : {np.median(actual_yield):7.1f} kWh/kWp  (for the window)")
    print(f"  model prediction   : {np.median(predicted_yield):7.1f} kWh/kWp  (for the window)")
    print(f"  corpus metadata    : {np.median(reported_yield):7.1f} kWh/kWp  (annual, as published)")
    print(
        f"  p10-p90 measured {np.percentile(actual_yield, 10):.0f}-{np.percentile(actual_yield, 90):.0f}"
        f" vs predicted {np.percentile(predicted_yield, 10):.0f}-{np.percentile(predicted_yield, 90):.0f}"
    )

    report = {
        "corpus": "Utrecht 175 residential PV systems (CC-BY-4.0, doi 10.5281/zenodo.10953360)",
        "rows": int(len(target)),
        "systems": int(len(np.unique(systems))),
        "observed_days": float(observed_days),
        "summary": summary,
        "folds": fold_report,
        "temporal": temporal,
        "yield_gate": {
            "measured_median_kwh_per_kwp": float(np.median(actual_yield)),
            "predicted_median_kwh_per_kwp": float(np.median(predicted_yield)),
            "reported_median_annual_kwh_per_kwp": float(np.median(reported_yield)),
            "measured_p10_p90": [
                float(np.percentile(actual_yield, 10)),
                float(np.percentile(actual_yield, 90)),
            ],
            "predicted_p10_p90": [
                float(np.percentile(predicted_yield, 10)),
                float(np.percentile(predicted_yield, 90)),
            ],
        },
        "elapsed_seconds": round(time.time() - started, 1),
    }
    if args.report:
        def rounded(node):
            """Three decimals: readable, and too coarse to be mistaken for a coordinate."""
            if isinstance(node, dict):
                return {key: rounded(value) for key, value in node.items()}
            if isinstance(node, list):
                return [rounded(value) for value in node]
            if isinstance(node, float):
                return round(node, 3)
            return node

        Path(args.report).write_text(json.dumps(rounded(report), indent=1))
        print(f"wrote {args.report}")

    if args.out:
        ridge, gbt = fit_models(features, target, args.quick)
        artifact = export_gbt(
            gbt,
            FEATURE_NAMES,
            {
                "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "training_rows": int(len(target)),
                "training_systems": int(len(np.unique(systems))),
                "training_window_days": float(observed_days),
                "corpus": report["corpus"],
                "attribution": "Contains information from the Utrecht PV dataset, licensed CC-BY-4.0.",
                "feature_digest": hashlib.sha256(",".join(FEATURE_NAMES).encode()).hexdigest()[:16],
                "held_out_system_mae_w": summary["gbt"]["mae_w"],
                "held_out_system_mae_pct_nameplate": summary["gbt"]["mae_pct_nameplate"],
                # The out-of-sample-both-ways pair is what the add-on reports to the user, so both
                # halves travel with the artifact rather than only the flattering one.
                "held_out_year_mae_pct_nameplate": (
                    temporal["gbt"]["mae_pct_nameplate"] if temporal else None
                ),
                "held_out_year_energy_bias_pct": (
                    temporal["gbt"]["energy_bias_pct"] if temporal else None
                ),
            },
        )
        # Verify the export before trusting it, two ways:
        #   1. the fast vectorised evaluator used for the metrics above must agree exactly with a
        #      plain scalar walk, which is the form a standard-library port would take;
        #   2. both must agree with scikit-learn, which is the external check that the exported
        #      numbers are the fitted model and not a plausible-looking rearrangement of it.
        stride = max(1, len(features) // 4000)
        sample = features[::stride][:4000]
        reference = gbt.predict(sample)
        exported = predict_artifact(artifact, sample)
        scalar = predict_artifact_scalar(artifact, sample[:400])
        scalar_gap = float(np.max(np.abs(scalar - predict_artifact(artifact, sample[:400]))))
        drift = float(np.max(np.abs(reference - exported)))
        print(f"export parity over {len(sample)} rows: vs scikit-learn {drift:.3e}, scalar path {scalar_gap:.3e}")
        if drift > 1e-9 or scalar_gap != 0.0:
            print("  PARITY FAILED - the artifact does not reproduce the fitted model")
            return 1
        Path(args.out).write_text(json.dumps(artifact))
        print(
            f"wrote {args.out} ({Path(args.out).stat().st_size / 1024.0:.0f} KB, "
            f"{len(artifact['trees'])} trees)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
