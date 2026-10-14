"""Emit the golden vectors that pin the add-on's standard-library PV model to the training path.

The add-on serves the model with arithmetic only (SPEC.md R3), so its feature builder is a second
implementation of the same formulas. Two implementations of one formula is exactly the condition
under which a model silently degrades: nothing crashes, the forecast just gets quietly worse, and an
error in a seasonal term is invisible until the season returns. So the port is pinned against
vectors emitted here, and the emitted file is committed.

Two separate claims are checked, and the order matters:

1. **This file's single-row path is the training path.** ``--cross-check`` recomputes the features of
   real corpus rows through the same code that emits the golden vectors and compares them with
   ``train_base.build_features``, the function that fitted the model. If that fails, nothing else
   here means anything.
2. **The add-on reproduces these vectors.** That is the committed test's job, not this script's.

Numbers are written as IEEE-754 bit patterns in integers, for the same two reasons the artifact is:
a decimal round-trip can change a comparison against a split threshold, and dense arrays of
four-to-five-decimal values are the shape the repository's R1 audit correctly treats as coordinates.

Usage::

    python emit_golden.py --artifact models/pv_base_v3.json --npz ingested_all.npz \\
        --out ../../energy_manager/app/forecast/models/pv_golden.json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import solar_physics as sp
from train_base import (
    FEATURE_NAMES,
    WEATHER_ARRAYS,
    build_features,
    export_gbt,
    predict_artifact_scalar,
)

_BITS_FORMAT = "<d"
_BITS_UNSIGNED = "<Q"

#: Synthetic sites. Deliberately round numbers in four different hemispheres and climate zones, so
#: the geometry is exercised well away from the training corpus's own latitude: a port that is only
#: correct in the Netherlands is not a port. Round numbers also keep the fixture inside what R1
#: allows in a committed file.
SITES = [
    (52.0, 5.0),      # Netherlands, the training latitude
    (40.0, -3.0),     # Spain
    (-33.0, 151.0),   # Australia, southern hemisphere
    (60.0, 10.0),     # Norway, high latitude
    (0.0, 0.0),       # equator, the degenerate case for the hour-angle sign
]

#: (tilt, azimuth, dc_wp, ac_wp) combinations, including a clipping array and a north-facing one.
ARRAYS = [
    (35.0, 180.0, 4000.0, 4000.0),
    (0.0, 180.0, 2000.0, 2000.0),     # flat roof: tilt zero is a divide-by-ish edge
    (45.0, 135.0, 3000.0, 2500.0),    # east-south-east
    (10.0, 90.0, 5000.0, 3000.0),     # heavily clipped: dc/ac above 1.6
    (35.0, 0.0, 2500.0, 2600.0),      # north-facing, no clipping
]

#: Local hours, spanning night, both shoulders and solar noon.
HOURS = [0, 6, 12, 18]

#: Synthetic irradiance, air temperature and the weather terms. ``ghi`` at or near zero is the
#: overcast case; the training corpus's clear-sky ceiling is well under the solar constant, so the top
#: value stays inside the distribution the model was fitted on. The weather columns are varied
#: independently of ``ghi`` on purpose: a case where cloud and irradiance always agree could not
#: detect a runtime that derives one from the wrong field of the other.
CONDITIONS = [
    (0.0, -5.0, 0.0, 0.0, 0.0, 0.0),
    (50.0, 2.0, 95.0, 4.5, 92.0, 1.2),
    (250.0, 8.0, 60.0, 2.0, 78.0, 0.0),
    (600.0, 15.0, 25.0, 1.1, 55.0, 0.4),
    (850.0, 22.0, 5.0, 0.3, 38.0, 0.0),
    (1000.0, 32.0, 100.0, 8.8, 97.0, 12.5),
]

#: Days of the year: winter, the leap day, the spring equinox and the summer solstice.
DAYS_OF_YEAR = [1, 60, 172, 266]

#: One common year and one leap year, so the day-of-year path is exercised on both calendar lengths:
#: day 60 is 1 March in 2015 and 29 February in 2016.
YEARS = [2015, 2016]


def _to_bits(value: float) -> int:
    return int(struct.unpack(_BITS_UNSIGNED, struct.pack(_BITS_FORMAT, float(value)))[0])


#: See the note at the use site: this gate exists to catch a logic difference between the single-row
#: path and the vectorised training path, not float32 round-trip noise.
CROSS_CHECK_TOLERANCE = 1e-4


def _from_bits(bits: int) -> float:
    return float(struct.unpack(_BITS_FORMAT, struct.pack(_BITS_UNSIGNED, int(bits)))[0])


def single_row(
    latitude: float,
    longitude: float,
    moment: datetime,
    tilt_deg: float,
    azimuth_deg: float,
    dc_capacity_wp: float,
    ac_capacity_wp: float,
    t_ambient_c: float,
    cloud_pct: float,
    wind_ms: float,
    rh_pct: float,
    rain_mm: float,
) -> tuple[list[float], float]:
    """Features and physical AC expectation for one instant, via the training primitives.

    This mirrors ``train_base.build_features`` row by row and calls the very same
    :mod:`solar_physics` functions the corpus fit used, so the golden vectors carry the training
    arithmetic rather than a re-derivation of it. ``--cross-check`` is what holds this claim honest.
    """
    utc = moment.astimezone(timezone.utc)
    moments = int(utc.timestamp())
    seconds_into_day = moments - (moments // 86400) * 86400
    fractional_hour = seconds_into_day / 3600.0
    stamps = np.array(moments, dtype="datetime64[s]")
    day_of_year = int(
        (stamps - stamps.astype("datetime64[Y]")).astype("timedelta64[D]").astype("int64") + 1
    )
    hour_of_day = float((moments // 3600) % 24)

    declination = sp.declination_rad(day_of_year)
    hour_angle = sp.hour_angle_rad(latitude, longitude, day_of_year, fractional_hour)
    elevation = sp.sun_elevation_deg(latitude, declination, hour_angle)
    sun_azimuth = sp.sun_azimuth_deg(latitude, declination, hour_angle)
    incidence = sp.cos_aoi(latitude, declination, hour_angle, tilt_deg, azimuth_deg)
    cos_zenith = np.cos(np.radians(90.0 - elevation))

    # Built exactly as the add-on builds it and as ``build_features`` builds it: site geometry times
    # whatever the provider knows about cloud. Deriving it here rather than accepting it as an
    # argument is what makes the golden vectors pin the whole chain rather than only its tail.
    att = float(sp.cloud_attenuation(float(cloud_pct)))
    clear_sky = float(sp.clear_sky_ghi(cos_zenith))
    ghi_wm2 = clear_sky * att

    poa, clearness, diffuse_fraction, dni = sp.plane_of_array(
        ghi_wm2, cos_zenith, incidence, tilt_deg
    )
    t_cell = sp.cell_temperature(t_ambient_c, poa)
    expected = sp.ac_power(dc_capacity_wp, ac_capacity_wp, poa, t_cell)
    unclipped = sp.dc_power(dc_capacity_wp, poa, t_cell) * sp.ETA_INV
    clipping_headroom = np.maximum(unclipped / np.maximum(ac_capacity_wp, 1e-9) - 1.0, 0.0)

    features = [
        float(elevation),
        float(cos_zenith),
        float(incidence),
        float(np.sin(np.radians(sun_azimuth))),
        float(np.cos(np.radians(sun_azimuth))),
        float(tilt_deg),
        float(np.sin(np.radians(tilt_deg))),
        float(ghi_wm2),
        float(poa),
        float(clearness),
        float(diffuse_fraction),
        float(dni),
        float(t_ambient_c),
        float(t_cell),
        float(dc_capacity_wp) / 1000.0,
        float(ac_capacity_wp) / 1000.0,
        float(dc_capacity_wp) / max(float(ac_capacity_wp), 1e-9),
        float(clipping_headroom),
        float(np.sin(2.0 * np.pi * hour_of_day / 24.0)),
        float(np.cos(2.0 * np.pi * hour_of_day / 24.0)),
        float(np.sin(2.0 * np.pi * day_of_year / 365.0)),
        float(np.cos(2.0 * np.pi * day_of_year / 365.0)),
        float(cloud_pct),
        att,  # the same expression the runtime applies, so the pair cannot disagree
        float(clear_sky),
        float(wind_ms),
        float(rh_pct),
        float(rain_mm),
    ]
    return features, float(expected), ghi_wm2


def cross_check(npz_path: str, sample: int = 400) -> float:
    """Largest disagreement between :func:`single_row` and the corpus feature builder.

    Returns the max absolute difference over the sampled rows. Any value that is not floating-point
    noise means the golden vectors do not describe the model that was fitted, and the run should
    stop.
    """
    data = dict(np.load(npz_path, allow_pickle=False))
    features, _target, expected, systems, row_hours = build_features(data)
    latitude = data["latitude"].astype("float64")
    longitude = data["longitude"].astype("float64")
    tilt = data["tilt"].astype("float64")
    azimuth = data["azimuth"].astype("float64")
    dc_capacity = data["dc_capacity"].astype("float64")
    ac_capacity = data["ac_capacity"].astype("float64")
    t2m = data["t2m"].astype("float64")
    weather = {name: data[name].astype("float64") for name in WEATHER_ARRAYS}
    # ``row_hours`` carries hours-since-epoch, not a row offset: map it back to the irradiance row.
    hour_index = {int(value): position for position, value in enumerate(data["hours"])}

    stride = max(1, len(features) // sample)
    worst = 0.0
    worst_expected = 0.0
    for index in range(0, len(features), stride):
        column = int(systems[index])
        hour = hour_index[int(row_hours[index])]
        moment = datetime.fromtimestamp(int(row_hours[index]) * 3600, tz=timezone.utc)
        mine, physics, _derived_ghi = single_row(
            latitude[column],
            longitude[column],
            moment,
            tilt[column],
            azimuth[column],
            dc_capacity[column],
            ac_capacity[column],
            t2m[hour],
            weather["cloud_pct"][hour],
            weather["wind_ms"][hour],
            weather["rh_pct"][hour],
            weather["rain_mm"][hour],
        )
        reference = features[index]
        worst = max(worst, max(abs(a - b) for a, b in zip(mine, reference)))
        worst_expected = max(worst_expected, abs(physics - expected[index]))
    print(
        f"cross-check over {len(range(0, len(features), stride))} corpus rows: "
        f"features {worst:.3e}, physics {worst_expected:.3e}"
    )
    return max(worst, worst_expected)


def emit(artifact_path: str, out_path: str) -> dict:
    artifact = json.loads(Path(artifact_path).read_text())
    cases = []
    seen_hours: set[int] = set()
    for year in YEARS:
        for day_of_year in DAYS_OF_YEAR:
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            # Day 60 of a leap year is 29 February; the leap year is included on purpose so the
            # day-of-year feature is exercised on both calendar lengths.
            moment_day = start.fromordinal(start.toordinal() + day_of_year - 1)
            for hour in HOURS:
                moment = moment_day.replace(hour=hour)
                stamp = int(moment.timestamp())
                if stamp in seen_hours:
                    continue
                seen_hours.add(stamp)
                for site_index, (latitude, longitude) in enumerate(SITES):
                    for tilt, azimuth, dc_capacity, ac_capacity in ARRAYS:
                        ghi, t_ambient, cloud_pct, wind_ms, rh_pct, rain_mm = CONDITIONS[
                            (site_index + hour + day_of_year) % len(CONDITIONS)
                        ]
                        features, physics, derived_ghi = single_row(
                            latitude,
                            longitude,
                            moment,
                            tilt,
                            azimuth,
                            dc_capacity,
                            ac_capacity,
                            t_ambient,
                            cloud_pct,
                            wind_ms,
                            rh_pct,
                            rain_mm,
                        )
                        row = np.array([features], dtype="float64")
                        ratio = float(predict_artifact_scalar(artifact, row)[0])
                        clamp = artifact["residual_clamp"]
                        clamped = max(float(clamp[0]), min(float(clamp[1]), ratio))
                        cases.append(
                            {
                                "in": [
                                    _to_bits(latitude),
                                    _to_bits(longitude),
                                    _to_bits(tilt),
                                    _to_bits(azimuth),
                                    _to_bits(dc_capacity),
                                    _to_bits(ac_capacity),
                                    _to_bits(derived_ghi),
                                    _to_bits(t_ambient),
                                    _to_bits(cloud_pct),
                                    _to_bits(wind_ms),
                                    _to_bits(rh_pct),
                                    _to_bits(rain_mm),
                                    stamp,
                                ],
                                "features": [_to_bits(value) for value in features],
                                "physics_w": _to_bits(physics),
                                "ratio": _to_bits(ratio),
                                "clamped_ratio": _to_bits(clamped),
                            }
                        )
    payload = {
        "kind": "pv_golden_vectors",
        "note": (
            "Synthetic sites and conditions; emitted by tools/forecast_ml/emit_golden.py from the "
            "same arithmetic that fitted the model. Values are IEEE-754 bit patterns held in "
            "integers so the comparison is exact and no decimal array reaches the repository."
        ),
        "feature_names": list(FEATURE_NAMES),
        "model": {
            "trained_at": artifact.get("trained_at"),
            "feature_digest": artifact.get("feature_digest"),
            "trees": len(artifact.get("trees") or ()),
        },
        "cases": cases,
    }
    Path(out_path).write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {out_path}: {len(cases)} cases, {Path(out_path).stat().st_size / 1024.0:.0f} KB")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--npz", default=None, help="corpus, to cross-check the single-row path")
    parser.add_argument("--skip-cross-check", action="store_true")
    args = parser.parse_args()

    if args.npz and not args.skip_cross_check:
        worst = cross_check(args.npz)
        # The corpus stores its weather columns as float32, so the single-row path recomputes them in
        # float64 from a float32 input while the vectorised path reads the stored array. The two
        # cannot agree bit-for-bit and are not expected to; what must not happen is a *logic*
        # difference. This tolerance sits four orders below the smallest one this gate has actually
        # caught (4.0 W/m^2, for a centroid-versus-per-system clear-sky series) and four orders above
        # the float32 round-trip noise, so it neither hides a real error nor fails on arithmetic.
        if worst > CROSS_CHECK_TOLERANCE:
            # Lower-case on purpose: an upper-case word before a hyphen reads as a device serial
            # to the repository's R1 scanner, which then has to be allowlisted, which weakens it.
            print(
                f"cross-check failed: {worst:.3e} exceeds the {CROSS_CHECK_TOLERANCE:.0e} tolerance, "
                "so the single-row path is not the training path"
            )
            return 1
    emit(args.artifact, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
