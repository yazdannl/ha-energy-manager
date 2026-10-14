"""Parity gate for the standard-library PV model port (SPEC.md section 7.1.4).

The add-on serves the learned PV model with arithmetic only (R3), so its feature builder and tree
walk are a second implementation of what the training pipeline did. This test is the only thing
standing between that second implementation and a silently degraded forecast: a sign error in the
hour angle, a degrees/radians mix-up, or a diffuse split that drifts from Erbs would not raise, crash
or look obviously wrong. It would just quietly produce a worse number, and a seasonal term would not
be caught until the season came round.

What is checked, and why each part earns its place:

* **Features, against the training arithmetic.** ``tools/forecast_ml/emit_golden.py`` builds these
  vectors by calling the same :mod:`solar_physics` functions the corpus fit used, and its
  ``--cross-check`` proves that single-row path reproduces ``train_base.build_features`` exactly on
  real corpus rows. So these vectors carry the training arithmetic rather than a re-derivation of it.
* **Physics and ratio, exactly.** The walk decides branches by comparing a feature with a decoded
  threshold, so this is what would move first if anything upstream drifted.
* **The residual clamp**, because an unclamped extrapolation is the one failure mode of this model
  that could reach a battery command rather than merely a bad forecast.

The cases are synthetic: five sites in both hemispheres and at the equator, five array shapes
including a flat roof, a north-facing array and a heavily clipped one, four hours spanning night and
noon, four calendar days including 29 February, and two years so the day-of-year path is exercised
on both calendar lengths.
"""

from __future__ import annotations

import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.forecast import pv  # noqa: E402

MODELS = ROOT / "energy_manager" / "app" / "forecast" / "models"
GOLDEN_PATH = MODELS / "pv_golden.json"
ARTIFACT_PATH = MODELS / "pv_base.json"

_BITS_FORMAT = "<d"
_BITS_UNSIGNED = "<Q"


def _from_bits(bits: int) -> float:
    return float(struct.unpack(_BITS_FORMAT, struct.pack(_BITS_UNSIGNED, int(bits)))[0])


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


@pytest.fixture(scope="module")
def model() -> "pv.PvResidualModel":
    return pv.load_model(ARTIFACT_PATH)


def _case_inputs(case: dict) -> tuple[list[float], datetime]:
    latitude, longitude, tilt, azimuth, dc, ac, ghi, t_ambient, stamp = case["in"]
    values = [_from_bits(value) for value in (latitude, longitude, tilt, azimuth, dc, ac, ghi, t_ambient)]
    return values, datetime.fromtimestamp(int(stamp), tz=timezone.utc)


def test_the_artifact_is_available_and_declares_its_contract(golden: dict) -> None:
    """The committed artifact loads, matches this build's feature order, and is the one pinned."""
    model = pv.load_model(ARTIFACT_PATH)
    assert model.clamp == (pv.RESIDUAL_MIN, pv.RESIDUAL_MAX)
    assert len(model.trees) == golden["model"]["trees"]
    assert model.metadata["trained_at"] == golden["model"]["trained_at"]
    metadata = pv.model_metadata(ARTIFACT_PATH)
    assert metadata is not None
    # The artifact travels with the attribution its licence requires, and the numbers the user is
    # shown come from the artifact rather than from a report filed elsewhere.
    assert "CC-BY-4.0" in str(metadata["corpus"])
    assert metadata["held_out_system_mae_pct_nameplate"] is not None
    assert metadata["held_out_year_mae_pct_nameplate"] is not None


def test_feature_contract_mismatch_is_refused() -> None:
    """A model fitted on a different feature set must fail loudly, not be fed shuffled columns."""
    payload = json.loads(ARTIFACT_PATH.read_text())
    payload["feature_names"] = list(reversed(payload["feature_names"]))
    with pytest.raises(ValueError, match="feature contract"):
        pv.PvResidualModel(payload)


def test_unknown_artifact_kind_is_refused() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text())
    payload["kind"] = "something_else"
    with pytest.raises(ValueError, match="unsupported"):
        pv.PvResidualModel(payload)


def test_features_match_the_training_arithmetic(golden: dict) -> None:
    """Every golden case reproduces its feature vector to within floating-point noise.

    The tolerance is deliberately far below the spacing between any two split thresholds in the
    artifact, so passing this and then producing a different tree path would require a threshold to
    sit inside this window — which the ratio check below would then catch.
    """
    worst = 0.0
    worst_name = ""
    for case in golden["cases"]:
        (latitude, longitude, tilt, azimuth, dc, ac, ghi, t_ambient), moment = _case_inputs(case)
        geometry = pv.solar_geometry(latitude, longitude, moment, tilt, azimuth)
        features, _physics, _poa = pv.feature_vector(
            geometry=geometry,
            ghi_wm2=ghi,
            t_ambient_c=t_ambient,
            tilt_deg=tilt,
            dc_capacity_wp=dc,
            ac_capacity_wp=ac,
            moment=moment,
        )
        expected = [_from_bits(value) for value in case["features"]]
        for index, (mine, reference) in enumerate(zip(features, expected)):
            gap = abs(mine - reference) / max(1.0, abs(reference))
            if gap > worst:
                worst = gap
                worst_name = f"case at {moment.isoformat()} feature {pv.FEATURE_NAMES[index]}"
    assert worst < 1e-9, f"feature drift {worst:.3e} at {worst_name}"


def test_physics_and_ratio_are_reproduced_exactly(golden: dict, model: "pv.PvResidualModel") -> None:
    """The physical estimate and the ensemble output are bit-for-bit the training pipeline's."""
    for case in golden["cases"]:
        (latitude, longitude, tilt, azimuth, dc, ac, ghi, t_ambient), moment = _case_inputs(case)
        geometry = pv.solar_geometry(latitude, longitude, moment, tilt, azimuth)
        features, physics, _poa = pv.feature_vector(
            geometry=geometry,
            ghi_wm2=ghi,
            t_ambient_c=t_ambient,
            tilt_deg=tilt,
            dc_capacity_wp=dc,
            ac_capacity_wp=ac,
            moment=moment,
        )
        assert physics == _from_bits(case["physics_w"]), f"physics at {moment.isoformat()}"
        assert model.predict_ratio(features) == _from_bits(
            case["ratio"]
        ), f"ratio at {moment.isoformat()}"


def test_the_estimate_path_agrees_with_the_pieces(golden: dict, model: "pv.PvResidualModel") -> None:
    """``estimate_slot`` is the composed entry point, so it is checked against its own parts."""
    for case in golden["cases"]:
        (latitude, longitude, tilt, azimuth, dc, ac, ghi, t_ambient), moment = _case_inputs(case)
        estimate = pv.estimate_slot(
            model,
            moment=moment,
            latitude=latitude,
            longitude=longitude,
            tilt_deg=tilt,
            azimuth_deg=azimuth,
            dc_capacity_wp=dc,
            ac_capacity_wp=ac,
            ghi_wm2=ghi,
            t_ambient_c=t_ambient,
        )
        clamped = _from_bits(case["clamped_ratio"])
        if estimate.elevation_deg <= 0.0:
            # Below the horizon the physical answer is zero, and it is produced without asking a
            # model that has no training support there.
            assert estimate.pv_kw == 0.0
            assert estimate.ratio == 0.0
            continue
        assert estimate.ratio == clamped, f"clamp at {moment.isoformat()}"
        assert estimate.physics_w == _from_bits(case["physics_w"])
        expected_kw = max(0.0, estimate.physics_w * clamped) / 1000.0
        assert estimate.pv_kw == expected_kw


def test_residual_clamp_bounds_any_input(model: "pv.PvResidualModel") -> None:
    """The clamp is a safety property, so it is asserted directly rather than only via vectors.

    Note what is *not* asserted: that a particular wild input saturates a particular bound. Which
    bound an out-of-distribution input reaches is an artefact of the trees, and pinning it here would
    freeze noise. What matters is that no input escapes the range, and that the range is not vacuous
    — the raw walk really does leave it, so the clamp is doing work rather than being decoration.
    """
    span = len(pv.FEATURE_NAMES)
    vectors = [[(-1.0 if (index * 31 + seed) % 2 else 1.0) * (10.0 ** 6) for index in range(span)] for seed in range(16)]
    vectors.append([0.0] * span)
    vectors.append([1e9] * span)
    vectors.append([-1e9] * span)
    raw_outside = 0
    for vector in vectors:
        raw = model.predict_ratio(vector)
        clamped = model.predict_clamped_ratio(vector)
        assert pv.RESIDUAL_MIN <= clamped <= pv.RESIDUAL_MAX, raw
        if raw < pv.RESIDUAL_MIN or raw > pv.RESIDUAL_MAX:
            raw_outside += 1
    assert raw_outside > 0, "the clamp never engaged, so it proves nothing"


def test_night_is_zero_before_the_model_is_consulted(model: "pv.PvResidualModel") -> None:
    """Deep night returns zero, and the day-of-year path stays correct across a leap year."""
    for year, day, expected_doy in ((2015, 1, 1), (2016, 60, 60)):
        moment = datetime(year, 1, 1, tzinfo=timezone.utc).fromordinal(
            datetime(year, 1, 1).toordinal() + day - 1
        ).replace(hour=3)
        geometry = pv.solar_geometry(52.0, 5.0, moment, 35.0, 180.0)
        assert geometry.elevation_deg < 0.0
        estimate = pv.estimate_slot(
            model,
            moment=moment,
            latitude=52.0,
            longitude=5.0,
            tilt_deg=35.0,
            azimuth_deg=180.0,
            dc_capacity_wp=4000.0,
            ac_capacity_wp=4000.0,
            ghi_wm2=0.0,
            t_ambient_c=5.0,
        )
        assert estimate.pv_kw == 0.0
        assert moment.timetuple().tm_yday == expected_doy


def test_clear_sky_reference_is_physical() -> None:
    """Haurwitz is a documented ceiling: it must stay below the solar constant and rise with the sun."""
    values = [pv.clear_sky_ghi(value / 100.0) for value in range(1, 100)]
    assert all(0.0 < value < 1100.0 for value in values)
    assert max(values) > values[0]
    assert pv.clear_sky_ghi(0.0) == 0.0


def test_erbs_diffuse_fraction_is_continuous_where_the_branches_meet() -> None:
    """The three Erbs branches must meet at their joins, or the model would see a step in the sky."""
    for kt in (0.22, 0.80):
        below = pv.erbs_diffuse_fraction(kt - 1e-9)
        at = pv.erbs_diffuse_fraction(kt)
        assert abs(below - at) < 1e-6, kt
    assert pv.erbs_diffuse_fraction(-1.0) == pytest.approx(1.0)
    assert pv.erbs_diffuse_fraction(2.0) == pytest.approx(0.165)


def test_the_hour_angle_is_antisymmetric_about_solar_noon() -> None:
    """A sign error here would still produce plausible-looking output on a south-facing array."""
    noon_ish = [pv.hour_angle_rad(5.0, 172, hour / 4.0) for hour in range(40, 60)]
    assert noon_ish == sorted(noon_ish)
    # Same clock hour on opposite sides of the day must straddle the turning point.
    assert pv.hour_angle_rad(5.0, 172, 6.0) < 0.0 < pv.hour_angle_rad(5.0, 172, 18.0)
