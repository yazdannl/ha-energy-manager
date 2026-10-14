"""Learned PV residual model, served from the standard library (SPEC.md section 7.1.4).

The committed artifact is a gradient-boosted ensemble over solar geometry, irradiance and array
parameters; its prediction is a **dimensionless multiplier on a physical estimate**, not a power in
watts. That choice is what lets one model trained on 174 Dutch arrays apply to an install it has
never seen: capacity, location and orientation enter as features, and the model outputs only the
ratio between what the physics says and what the array actually delivered.

Which physics, exactly — this is the part that has to be right. The reference is *not* the
``solar.source = capacity`` tier, and the difference is not cosmetic:

===================  ==================================  ==============================
                     ``forecast/pv.py`` (this module)    ``sources/solar.py`` capacity
===================  ==================================  ==============================
irradiance           estimated global horizontal         same input, but the split
diffuse split        Erbs, from the clearness index      constant 25 percent
module temperature   NOCT model from air temperature     not modelled
inverter             ``eta = 0.96`` plus AC clipping     folded into one ``performance_ratio``
===================  ==================================  ==============================

So the residual must multiply *this* model's output. Multiplying the capacity tier's output by a
ratio learned against a different reference would be wrong by the difference between the two, which
at low sun and high cell temperature is tens of percent. When the learned source is active,
``solar.performance_ratio`` is therefore not applied, and the forecast notes say so.

Irradiance is the honest limit of a forecast: the training corpus carried *measured* horizontal
global irradiance, while at runtime this module is handed whatever the provider could estimate —
clear-sky, attenuated by forecast cloud cover or by NASA POWER's observed local clearness. The
clearness index is normalised by extraterrestrial irradiance, so an estimated value lands inside the
range the model was trained on rather than outside it, which is the property that makes the port
viable at all. It does not make the estimate measured, and the provider keeps its clear-sky
uncertainty band rather than claiming the model's held-out accuracy.

Every function here is a pure function of its arguments: no clock, no I/O, no module state beyond the
cached artifact. The parity test (``tests/test_forecast_pv.py``) pins the numbers against golden
vectors emitted by the training pipeline, because a transcription error in this file would be
invisible in the output — it would just look like a slightly worse forecast.
"""

from __future__ import annotations

import json
import logging
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "FEATURE_NAMES",
    "LEARNED_KIND",
    "MODEL_FILENAME",
    "RESIDUAL_MAX",
    "RESIDUAL_MIN",
    "SlotEstimate",
    "PvResidualModel",
    "ac_power",
    "cell_temperature",
    "clear_sky_ghi",
    "dc_power",
    "erbs_diffuse_fraction",
    "estimate_slot",
    "feature_vector",
    "load_model",
    "model_metadata",
    "plane_of_array",
    "solar_geometry",
]

# --------------------------------------------------------------------------------------------
# Physics constants. These must stay in step with ``tools/forecast_ml/solar_physics.py``: the
# artifact's thresholds are fitted against the arithmetic these produce, so changing one without
# retraining silently invalidates the model.
# --------------------------------------------------------------------------------------------

SOLAR_CONSTANT = 1367.0
GROUND_ALBEDO = 0.2
NOCT_C = 45.0
T_REF_C = 25.0
GAMMA_PER_C = -0.004
ETA_INV = 0.96
G_STC = 1000.0

#: The residual is clamped to the range the training pipeline used, so a wild extrapolation cannot
#: become a wild power command.
RESIDUAL_MIN = 0.3
RESIDUAL_MAX = 1.8

#: The artifact's own declared kind. Refusing anything else is the cheap guard against serving a
#: file that merely looks like this one.
LEARNED_KIND = "hist_gradient_boosted_trees_exact_bits"

MODEL_FILENAME = "pv_base.json"

#: The feature contract, in order. The artifact carries the same list; the loader refuses a
#: mismatch rather than feeding a model the wrong column. A silent column swap would produce a
#: plausible-looking forecast that is simply wrong.
FEATURE_NAMES: tuple[str, ...] = (
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
    # Weather terms (0.23.0). Training used to be driven by satellite-observed irradiance while the
    # add-on rebuilds irradiance from a cloud percentage, so the residual was partly correcting the
    # difference between two estimators — measured at 111.0 W versus 150.5 W held-out MAE. These are
    # the ingredients of the serving chain itself, which is why each one is a field a weather
    # forecast publishes. ``attenuation`` and ``clear_sky_ghi_wm2`` are kept alongside ``ghi_wm2``
    # so the model can learn this site's cloud-to-irradiance mapping rather than being stuck with
    # Kasten-Czeplak's.
    "cloud_pct",
    "attenuation",
    "clear_sky_ghi_wm2",
    "wind_ms",
    "rh_pct",
    "rain_mm",
)

_BITS_FORMAT = "<d"
_BITS_UNSIGNED = "<Q"

#: Below this elevation the model has no training support: the training corpus kept rows only above
#: five degrees, because below it a tilted plane's output is dominated by diffuse light and the
#: incidence geometry stops being informative. Between zero and here the model is extrapolating
#: slightly, which the residual clamp bounds; at or below zero production is physically zero and is
#: returned without asking the model at all.
MIN_ELEVATION_DEG = 5.0


def _bits_to_float(bits: int) -> float:
    """Decode an IEEE-754 bit pattern held as an integer.

    Weights are stored this way, not as JSON decimals, for two reasons: a decimal round-trip changes
    comparisons for rows sitting exactly on a split threshold, and dense arrays of four-to-five
    decimal values are the exact shape the repository's own R1 audit hunts as coordinates.
    """
    return float(struct.unpack(_BITS_FORMAT, struct.pack(_BITS_UNSIGNED, int(bits)))[0])


# --------------------------------------------------------------------------------------------
# Solar geometry
# --------------------------------------------------------------------------------------------


def declination_rad(day_of_year: int) -> float:
    """Cooper's declination: +/-23.45 degrees at the solstices, zero at the equinoxes."""
    return math.radians(23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0)))


def equation_of_time_min(day_of_year: int) -> float:
    """Equation of time in minutes (fractional-year form; sub-hour detail is not modelled)."""
    gamma = 2.0 * math.pi * (day_of_year - 1) / 365.0
    return 229.18e-6 * (
        75.0
        + 1868.0 * math.cos(gamma)
        - 32077.0 * math.sin(gamma)
        - 14615.0 * math.cos(2.0 * gamma)
        - 40849.0 * math.sin(2.0 * gamma)
    )


def hour_angle_rad(longitude: float, day_of_year: int, fractional_hour_utc: float) -> float:
    """Hour angle, radians, from a UTC fractional hour.

    True solar time is UTC clock time corrected by the equation of time and by the longitude offset
    from the Greenwich meridian. The provider's own ``_sun_terms`` derives the same quantity from
    local clock time and the zone offset; the zone offset cancels, so the two agree for any instant
    given on a whole minute, and this module uses the UTC form because that is what training used.
    """
    true_solar_minutes = fractional_hour_utc * 60.0 + equation_of_time_min(day_of_year) + 4.0 * longitude
    return math.radians(true_solar_minutes / 4.0 - 180.0)


def sun_elevation_deg(latitude_rad: float, declination: float, hour_angle: float) -> float:
    """Sun elevation above the horizon, degrees."""
    sin_elevation = math.sin(latitude_rad) * math.sin(declination) + math.cos(latitude_rad) * math.cos(
        declination
    ) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elevation))))


def sun_azimuth_deg(latitude_rad: float, declination: float, hour_angle: float, elevation_rad: float) -> float:
    """Sun azimuth measured from north, clockwise (0 = north, 90 = east, 180 = south)."""
    cos_azimuth = (math.sin(declination) - math.sin(elevation_rad) * math.sin(latitude_rad)) / (
        math.cos(elevation_rad) * math.cos(latitude_rad)
    )
    azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cos_azimuth))))
    return 360.0 - azimuth if hour_angle > 0.0 else azimuth


def cos_aoi(
    latitude_rad: float,
    declination: float,
    hour_angle: float,
    tilt_rad: float,
    azimuth_from_south_rad: float,
) -> float:
    """Cosine of the angle of incidence between the sun and a plane (0 = grazing, 1 = normal)."""
    return (
        math.sin(declination) * math.sin(latitude_rad) * math.cos(tilt_rad)
        - math.sin(declination) * math.cos(latitude_rad) * math.sin(tilt_rad) * math.cos(azimuth_from_south_rad)
        + math.cos(declination) * math.cos(latitude_rad) * math.cos(tilt_rad) * math.cos(hour_angle)
        + math.cos(declination)
        * math.sin(latitude_rad)
        * math.sin(tilt_rad)
        * math.cos(azimuth_from_south_rad)
        * math.cos(hour_angle)
        + math.cos(declination) * math.sin(tilt_rad) * math.sin(azimuth_from_south_rad) * math.sin(hour_angle)
    )


@dataclass(frozen=True)
class SolarGeometry:
    """Sun and plane geometry for one instant, plus clear-sky horizontal irradiance."""

    elevation_deg: float
    cos_zenith: float
    cos_incidence: float
    sun_azimuth_deg: float
    clear_sky_ghi_wm2: float


def clear_sky_ghi(cos_zenith: float) -> float:
    """Haurwitz clear-sky horizontal global irradiance, W/m^2 (SPEC.md 7.1.1 step 2)."""
    if cos_zenith <= 0.0:
        return 0.0
    return 1098.0 * cos_zenith * math.exp(-0.059 / cos_zenith)


#: Kasten-Czeplak attenuation constants, the same two the training pipeline and SPEC 7.1.1 use.
#: They now only seed the ``attenuation`` feature: the model is free to learn a different mapping,
#: which is the point of handing it ``cloud_pct`` as well.
CLOUD_MAX_ATTENUATION = 0.75
CLOUD_COVERAGE_EXPONENT = 3.4


def cloud_attenuation(cloud_pct: float) -> float:
    """Attenuation factor for a cloud coverage percentage (Kasten-Czeplak).

    Non-finite or absent coverage is treated as clear. That is the documented interpretation
    everywhere else in the add-on, and it is the safe direction: an invented attenuation would
    silently cut a forecast that nothing measured.
    """
    try:
        coverage = min(max(float(cloud_pct), 0.0), 100.0) / 100.0
    except (TypeError, ValueError):
        return 1.0
    return 1.0 - CLOUD_MAX_ATTENUATION * coverage ** CLOUD_COVERAGE_EXPONENT


def cloud_pct_from_attenuation(attenuation: float) -> float:
    """The cloud coverage Kasten-Czeplak implies for an attenuation factor.

    This exists so the two weather features stay mutually consistent when the attenuation did not
    come from a cloud number — the climatological NASA clearness profile is the case that matters.
    Passing ``cloud_pct = 0`` alongside an attenuation of 0.75 would tell the model two contradictory
    things about the same sky; inverting the same expression instead keeps ``cloud_pct`` inside the
    range the model was fitted on and keeps the pair readable as one observation.
    """
    try:
        factor = min(max(float(attenuation), 1.0 - CLOUD_MAX_ATTENUATION), 1.0)
    except (TypeError, ValueError):
        return 0.0
    return 100.0 * ((1.0 - factor) / CLOUD_MAX_ATTENUATION) ** (1.0 / CLOUD_COVERAGE_EXPONENT)


def solar_geometry(
    latitude: float,
    longitude: float,
    moment: datetime,
    tilt_deg: float,
    azimuth_deg: float,
) -> SolarGeometry:
    """Geometry for one aware instant, and the clear-sky irradiance that goes with it.

    The day of year is taken in **UTC**, matching the training corpus, where rows were stamped in
    UTC. Using the local calendar day instead would shift the seasonal feature by up to a day at the
    date line and, worse, would make this module disagree with the pipeline that fitted it.
    """
    utc = moment.astimezone(timezone.utc)
    day_of_year = utc.timetuple().tm_yday
    fractional_hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    latitude_rad = math.radians(latitude)
    declination = declination_rad(day_of_year)
    hour_angle = hour_angle_rad(longitude, day_of_year, fractional_hour)
    elevation = sun_elevation_deg(latitude_rad, declination, hour_angle)
    elevation_rad = math.radians(elevation)
    cos_zenith = math.cos(math.radians(90.0 - elevation))
    incidence = cos_aoi(
        latitude_rad,
        declination,
        hour_angle,
        math.radians(tilt_deg),
        # The transposition formula measures the plane azimuth from due south, positive toward west;
        # the setting is a compass azimuth (0 = north, 180 = south).
        math.radians((azimuth_deg + 180.0) % 360.0),
    )
    azimuth = sun_azimuth_deg(latitude_rad, declination, hour_angle, elevation_rad)
    return SolarGeometry(
        elevation_deg=elevation,
        cos_zenith=cos_zenith,
        cos_incidence=incidence,
        sun_azimuth_deg=azimuth,
        clear_sky_ghi_wm2=clear_sky_ghi(cos_zenith),
    )


# --------------------------------------------------------------------------------------------
# Plane of array, module temperature, power
# --------------------------------------------------------------------------------------------


def erbs_diffuse_fraction(kt: float) -> float:
    """Erbs correlation: diffuse fraction of global horizontal irradiance from the clearness index."""
    kt = max(0.0, min(1.0, kt))
    if kt <= 0.22:
        return 1.0 - 0.09 * kt
    if kt <= 0.80:
        return (
            0.9511
            - 0.1604 * kt
            + 4.388 * kt ** 2
            - 16.638 * kt ** 3
            + 12.336 * kt ** 4
        )
    return 0.165


def plane_of_array(
    ghi: float, cos_zenith: float, cos_incidence: float, tilt_deg: float
) -> tuple[float, float, float, float]:
    """``(poa, clearness_index, diffuse_fraction, dni)`` from horizontal global irradiance, W/m^2.

    The beam term is zeroed when the sun is behind the plane; the diffuse and ground-reflected terms
    are not, which is why a north-facing array still produces something on a bright day.
    """
    cos_zenith = max(cos_zenith, 1e-6)
    kt = max(0.0, min(1.0, ghi / (SOLAR_CONSTANT * cos_zenith)))
    diffuse_fraction = erbs_diffuse_fraction(kt)
    dhi = diffuse_fraction * ghi
    dni = (ghi - dhi) / cos_zenith
    cos_tilt = math.cos(math.radians(tilt_deg))
    poa = (
        dni * max(0.0, cos_incidence)
        + dhi * (1.0 + cos_tilt) / 2.0
        + ghi * GROUND_ALBEDO * (1.0 - cos_tilt) / 2.0
    )
    return max(poa, 0.0), kt, diffuse_fraction, dni


def cell_temperature(t_ambient_c: float, poa: float) -> float:
    """NOCT module temperature model, degrees C."""
    return t_ambient_c + (NOCT_C - 20.0) / 800.0 * poa


def dc_power(dc_capacity_wp: float, poa: float, t_cell_c: float) -> float:
    """DC array power before the inverter, W."""
    return dc_capacity_wp * (poa / G_STC) * (1.0 + GAMMA_PER_C * (t_cell_c - T_REF_C))


def ac_power(dc_capacity_wp: float, ac_capacity_wp: float, poa: float, t_cell_c: float) -> float:
    """Expected AC power, W: DC derated by cell temperature and the inverter, then clipped.

    ``min(dc * eta, ac_capacity)`` is what makes a DC/AC ratio above one observable. Past the
    inverter's limit the curve flattens instead of following irradiance, which is behaviour the
    residual model cannot invent and so has to be told about.
    """
    dc = max(dc_power(dc_capacity_wp, poa, t_cell_c), 0.0) * ETA_INV
    return min(dc, ac_capacity_wp)


# --------------------------------------------------------------------------------------------
# Feature vector
# --------------------------------------------------------------------------------------------


def feature_vector(
    *,
    geometry: SolarGeometry,
    ghi_wm2: float,
    t_ambient_c: float,
    tilt_deg: float,
    dc_capacity_wp: float,
    ac_capacity_wp: float,
    moment: datetime,
    cloud_pct: float = 0.0,
    attenuation: float = 1.0,
    wind_ms: float = 0.0,
    rh_pct: float = 0.0,
    rain_mm: float = 0.0,
) -> tuple[list[float], float, float]:
    """``(features, physics_ac_w, poa_wm2)`` for one instant, in :data:`FEATURE_NAMES` order.

    The hour and day-of-year terms are the values the training rows carried: the UTC hour truncated
    to the hour, and the UTC calendar day. Keeping them coarse is deliberate — a model fitted on
    hourly samples should not be handed a smoothed-upon quantity that never appeared in training.
    """
    poa, clearness, diffuse_fraction, dni = plane_of_array(
        ghi_wm2, geometry.cos_zenith, geometry.cos_incidence, tilt_deg
    )
    t_cell = cell_temperature(t_ambient_c, poa)
    expected = ac_power(dc_capacity_wp, ac_capacity_wp, poa, t_cell)
    unclipped = dc_power(dc_capacity_wp, poa, t_cell) * ETA_INV
    clipping_headroom = max(unclipped / max(ac_capacity_wp, 1e-9) - 1.0, 0.0)

    utc = moment.astimezone(timezone.utc)
    hour_of_day = float(utc.hour)
    day_of_year = float(utc.timetuple().tm_yday)
    tilt_rad = math.radians(tilt_deg)
    azimuth_rad = math.radians(geometry.sun_azimuth_deg)

    features = [
        geometry.elevation_deg,
        geometry.cos_zenith,
        geometry.cos_incidence,
        math.sin(azimuth_rad),
        math.cos(azimuth_rad),
        tilt_deg,
        math.sin(tilt_rad),
        ghi_wm2,
        poa,
        clearness,
        diffuse_fraction,
        dni,
        t_ambient_c,
        t_cell,
        dc_capacity_wp / 1000.0,
        ac_capacity_wp / 1000.0,
        dc_capacity_wp / max(ac_capacity_wp, 1e-9),
        clipping_headroom,
        math.sin(2.0 * math.pi * hour_of_day / 24.0),
        math.cos(2.0 * math.pi * hour_of_day / 24.0),
        math.sin(2.0 * math.pi * day_of_year / 365.0),
        math.cos(2.0 * math.pi * day_of_year / 365.0),
        cloud_pct,
        # Both are passed in rather than one derived from the other here: ``ghi_wm2`` is built by the
        # caller from the attenuation that was actually applied, and re-deriving it would let the
        # two features disagree about the same slot when the attenuation came from the NASA
        # clearness profile instead of from a cloud number.
        attenuation,
        geometry.clear_sky_ghi_wm2,
        wind_ms,
        rh_pct,
        rain_mm,
    ]
    return features, expected, poa


# --------------------------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------------------------


class PvResidualModel:
    """A loaded artifact: the exact tree walk, plus enough metadata to say what it is.

    Thresholds and leaf values are decoded from their integer bit patterns once, at load, because a
    ``struct.unpack`` per comparison would dominate the walk. Decoding is exact, so this is a speed
    decision and never a numerical one.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        kind = payload.get("kind")
        if kind != LEARNED_KIND:
            raise ValueError(f"unsupported PV model kind: {kind!r}")
        names = tuple(payload.get("feature_names") or ())
        if names != FEATURE_NAMES:
            raise ValueError(
                "PV model feature contract does not match this build: "
                f"artifact has {len(names)} features, this build serves {len(FEATURE_NAMES)}"
            )
        clamp = payload.get("residual_clamp") or [RESIDUAL_MIN, RESIDUAL_MAX]
        self.clamp = (float(clamp[0]), float(clamp[1]))
        self.baseline = _bits_to_float(payload["baseline_bits"])
        self.trees = [_Tree(tree) for tree in payload["trees"]]
        self.metadata: dict[str, Any] = {
            key: payload.get(key)
            for key in (
                "trained_at",
                "training_rows",
                "training_systems",
                "training_window_days",
                "corpus",
                "attribution",
                "feature_digest",
                "held_out_system_mae_pct_nameplate",
                "held_out_year_mae_pct_nameplate",
            )
        }
        self.metadata["trees"] = len(self.trees)

    def predict_ratio(self, features: Sequence[float]) -> float:
        """Raw ensemble output: the multiplier on the physical estimate.

        The constructor applies its own shrinkage to every leaf, so there is no outer learning rate
        to multiply by here — the prediction is the baseline plus the reached leaves, which is what
        the training pipeline's parity gate pins against scikit-learn.
        """
        total = self.baseline
        for tree in self.trees:
            node = 0
            while not tree.is_leaf[node]:
                observed = features[tree.feature[node]]
                if observed != observed:  # NaN never compares equal to itself
                    node = tree.left[node] if tree.missing_left[node] else tree.right[node]
                else:
                    node = tree.left[node] if observed <= tree.thresholds[node] else tree.right[node]
            total += tree.values[node]
        return total

    def predict_clamped_ratio(self, features: Sequence[float]) -> float:
        """The ratio the provider may use: the walk, clamped to the range training used."""
        return max(self.clamp[0], min(self.clamp[1], self.predict_ratio(features)))


class _Tree:
    """One tree, decoded once: float thresholds and leaf values instead of integer bit patterns.

    Decoding up front is a speed decision, never a numerical one — the bit patterns round-trip
    exactly, and a ``struct.unpack`` per comparison would dominate the walk.

    A node's ``is_leaf`` flag is carried explicitly rather than inferred. In this encoding a leaf's
    child pointers refer back to node zero, so inferring a leaf from them would be wrong — and a
    fabricated leaf test is exactly the sort of thing that yields a plausible-looking forecast with
    no relation to the fitted model.
    """

    __slots__ = ("feature", "left", "right", "thresholds", "values", "missing_left", "is_leaf")

    def __init__(self, tree: Mapping[str, Any]) -> None:
        self.feature = [int(value) for value in tree["feature"]]
        self.left = [int(value) for value in tree["left"]]
        self.right = [int(value) for value in tree["right"]]
        self.thresholds = [_bits_to_float(value) for value in tree["threshold_bits"]]
        self.values = [_bits_to_float(value) for value in tree["value_bits"]]
        self.missing_left = [int(value) for value in tree["missing_left"]]
        self.is_leaf = [bool(value) for value in tree["is_leaf"]]


_CACHE: dict[str, PvResidualModel] = {}


def model_path(directory: str | Path | None = None) -> Path:
    """Where the committed artifact lives: beside this module, inside the add-on image."""
    base = Path(directory) if directory is not None else Path(__file__).with_name("models")
    return base / MODEL_FILENAME


def load_model(path: str | Path | None = None) -> PvResidualModel:
    """Load the artifact, cached per path.

    Lazy on purpose: an install that never selects the learned source should not pay a megabyte of
    JSON parsing at start-up. A failure is raised, never swallowed — the caller turns it into a
    provider status, so a broken artifact produces a stated reason instead of a silent zero.
    """
    target = Path(path) if path is not None else model_path()
    key = str(target)
    cached = _CACHE.get(key)
    if cached is None:
        payload = json.loads(target.read_text())
        cached = PvResidualModel(payload)
        _CACHE[key] = cached
    return cached


def model_metadata(path: str | Path | None = None) -> dict[str, Any] | None:
    """Artifact metadata without loading the trees; ``None`` when the artifact is missing."""
    target = Path(path) if path is not None else model_path()
    try:
        payload = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    clamp = payload.get("residual_clamp") or [RESIDUAL_MIN, RESIDUAL_MAX]
    return {
        "kind": payload.get("kind"),
        "features": len(payload.get("feature_names") or ()),
        "trees": len(payload.get("trees") or ()),
        "trained_at": payload.get("trained_at"),
        "corpus": payload.get("corpus"),
        "training_systems": payload.get("training_systems"),
        "training_window_days": payload.get("training_window_days"),
        "held_out_system_mae_pct_nameplate": payload.get("held_out_system_mae_pct_nameplate"),
        "held_out_year_mae_pct_nameplate": payload.get("held_out_year_mae_pct_nameplate"),
        "residual_clamp": [float(clamp[0]), float(clamp[1])],
    }


@dataclass(frozen=True)
class SlotEstimate:
    """One slot's learned estimate, with the intermediate quantities the notes and status report."""

    pv_kw: float
    ratio: float
    physics_w: float
    poa_wm2: float
    ghi_wm2: float
    clearness_index: float
    t_cell_c: float
    elevation_deg: float


def estimate_slot(
    model: PvResidualModel,
    *,
    moment: datetime,
    latitude: float,
    longitude: float,
    tilt_deg: float,
    azimuth_deg: float,
    dc_capacity_wp: float,
    ac_capacity_wp: float,
    ghi_wm2: float,
    t_ambient_c: float,
    cloud_pct: float = 0.0,
    attenuation: float = 1.0,
    wind_ms: float = 0.0,
    rh_pct: float = 0.0,
    rain_mm: float = 0.0,
) -> SlotEstimate:
    """One slot's learned PV estimate, in kW.

    Below the horizon the answer is zero without consulting the model. That is not an optimisation:
    the training corpus kept no row below five degrees of elevation, so there is nothing to
    extrapolate from, and production is physically zero there anyway.
    """
    geometry = solar_geometry(latitude, longitude, moment, tilt_deg, azimuth_deg)
    if geometry.elevation_deg <= 0.0:
        return SlotEstimate(
            pv_kw=0.0,
            ratio=0.0,
            physics_w=0.0,
            poa_wm2=0.0,
            ghi_wm2=0.0,
            clearness_index=0.0,
            t_cell_c=t_ambient_c,
            elevation_deg=geometry.elevation_deg,
        )
    features, physics_w, poa = feature_vector(
        geometry=geometry,
        ghi_wm2=ghi_wm2,
        t_ambient_c=t_ambient_c,
        tilt_deg=tilt_deg,
        dc_capacity_wp=dc_capacity_wp,
        ac_capacity_wp=ac_capacity_wp,
        moment=moment,
        cloud_pct=cloud_pct,
        attenuation=attenuation,
        wind_ms=wind_ms,
        rh_pct=rh_pct,
        rain_mm=rain_mm,
    )
    ratio = model.predict_clamped_ratio(features)
    return SlotEstimate(
        pv_kw=max(0.0, physics_w * ratio) / 1000.0,
        ratio=ratio,
        physics_w=physics_w,
        poa_wm2=poa,
        ghi_wm2=ghi_wm2,
        clearness_index=features[FEATURE_NAMES.index("clearness_index")],
        t_cell_c=features[FEATURE_NAMES.index("t_cell_c")],
        elevation_deg=geometry.elevation_deg,
    )
