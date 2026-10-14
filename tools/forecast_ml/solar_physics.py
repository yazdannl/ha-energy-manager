"""Solar geometry and plane-of-array physics for the forecast ML tooling.

Vectorised NumPy port of the same formulas the add-on already uses, so a model trained here can be
served by a standard-library implementation of these functions without changing its inputs:

* declination, equation of time, hour angle   -> ``app/sources/solar.py::_declination_rad`` etc.
* cos(angle of incidence)                     -> ``app/sources/solar.py::_cos_aoi``
* isotropic transposition, albedo, diffuse    -> ``app/sources/solar.py::_plane_irradiance``

Two deliberate differences from the runtime path, both of which the learned residual then absorbs:

1. The runtime path drives transposition with a **clear-sky** Haurwitz estimate. Here the irradiance
   is *measured* horizontal global (PVGIS-SARAH2), which is what supervision needs.
2. The runtime path splits beam/diffuse with a constant 25 percent diffuse fraction. Here the split
   uses the Erbs correlation, which is the standard improvement and is a candidate to port back.

Everything here is a pure function of its arguments: no clock, no I/O, no module state.
"""

from __future__ import annotations

import numpy as np

#: Solar constant, W/m^2.
SOLAR_CONSTANT = 1367.0
#: Ground albedo, matching ``solar.GROUND_ALBEDO``.
GROUND_ALBEDO = 0.2
#: Nominal operating cell temperature, degrees C (a standard mounting assumption).
NOCT_C = 45.0
#: Reference cell temperature for the module temperature coefficient, degrees C.
T_REF_C = 25.0
#: Module temperature coefficient per degree C: a typical c-Si value.
GAMMA_PER_C = -0.004
#: Inverter conversion efficiency (the residual model learns whatever this gets wrong).
ETA_INV = 0.96
#: Reference irradiance for nameplate rating, W/m^2.
G_STC = 1000.0


def declination_rad(day_of_year):
    """Cooper's declination, radians."""
    return np.radians(23.45 * np.sin(np.radians(360.0 * (284.0 + day_of_year) / 365.0)))


def equation_of_time_min(day_of_year):
    """Equation of time in minutes (fractional-year form)."""
    gamma = 2.0 * np.pi * (day_of_year - 1) / 365.0
    return 229.18e-6 * (
        75.0
        + 1868.0 * np.cos(gamma)
        - 32077.0 * np.sin(gamma)
        - 14615.0 * np.cos(2.0 * gamma)
        - 40849.0 * np.sin(2.0 * gamma)
    )


def hour_angle_rad(latitude, longitude, day_of_year, hour_utc):
    """Hour angle, radians, for a UTC instant given as a fractional hour.

    The add-on derives true solar time from local clock time and the zone offset. For UTC input
    the offset is zero, which is the case here, so the same expression reduces to this.
    """
    clock_minutes = hour_utc * 60.0
    true_solar = clock_minutes + equation_of_time_min(day_of_year) + 4.0 * longitude
    return np.radians(true_solar / 4.0 - 180.0)


def sun_elevation_deg(latitude, declination, hour_angle):
    """Sun elevation above the horizon, degrees."""
    sin_elevation = (
        np.sin(np.radians(latitude)) * np.sin(declination)
        + np.cos(np.radians(latitude)) * np.cos(declination) * np.cos(hour_angle)
    )
    return np.degrees(np.arcsin(np.clip(sin_elevation, -1.0, 1.0)))


def sun_azimuth_deg(latitude, declination, hour_angle):
    """Sun azimuth measured from north, clockwise (0 = north, 90 = east, 180 = south)."""
    elevation = np.radians(sun_elevation_deg(latitude, declination, hour_angle))
    sin_lat = np.sin(np.radians(latitude))
    cos_lat = np.cos(np.radians(latitude))
    cos_azimuth = (np.sin(declination) - np.sin(elevation) * sin_lat) / (
        np.cos(elevation) * cos_lat
    )
    azimuth = np.degrees(np.arccos(np.clip(cos_azimuth, -1.0, 1.0)))
    return np.where(hour_angle > 0.0, 360.0 - azimuth, azimuth)


def cos_aoi(latitude, declination, hour_angle, tilt_deg, azimuth_deg):
    """Cosine of the angle of incidence for a plane.

    ``azimuth_deg`` is a compass azimuth (0 = north, 180 = south), matching the settings the user
    enters and the metadata in the training corpus. The transposition formula wants the plane
    azimuth measured from due south, positive toward west, which is the ``+180`` rotation below
    and is the same conversion the add-on applies at its call site.
    """
    tilt = np.radians(tilt_deg)
    azimuth_from_south = np.radians((np.asarray(azimuth_deg) + 180.0) % 360.0)
    sin_lat = np.sin(np.radians(latitude))
    cos_lat = np.cos(np.radians(latitude))
    return (
        np.sin(declination) * sin_lat * np.cos(tilt)
        - np.sin(declination) * cos_lat * np.sin(tilt) * np.cos(azimuth_from_south)
        + np.cos(declination) * cos_lat * np.cos(tilt) * np.cos(hour_angle)
        + np.cos(declination) * sin_lat * np.sin(tilt) * np.cos(azimuth_from_south) * np.cos(hour_angle)
        + np.cos(declination) * np.sin(tilt) * np.sin(azimuth_from_south) * np.sin(hour_angle)
    )


#: Kasten-Czeplak attenuation (SPEC 7.1.1 step 3), the expression the add-on applies to a cloud
#: percentage. It lives here as well as in the runtime because the corpus has to be built with the
#: same constants the forecast will use: a different pair would put a fixed offset into the residual.
CLOUD_MAX_ATTENUATION = 0.75
CLOUD_COVERAGE_EXPONENT = 3.4


def clear_sky_ghi(cos_zenith):
    """Haurwitz clear-sky horizontal global irradiance, W/m^2."""
    cos_z = np.maximum(cos_zenith, 1e-6)
    return np.where(cos_zenith > 0.0, 1098.0 * cos_z * np.exp(-0.059 / cos_z), 0.0)


def cloud_attenuation(cloud_pct):
    """Attenuation factor for a cloud coverage percentage."""
    coverage = np.clip(cloud_pct, 0.0, 100.0) / 100.0
    return 1.0 - CLOUD_MAX_ATTENUATION * coverage ** CLOUD_COVERAGE_EXPONENT


def erbs_diffuse_fraction(kt):
    """Erbs correlation: diffuse fraction of global horizontal irradiance from clearness index."""
    kt = np.clip(kt, 0.0, 1.0)
    low = 1.0 - 0.09 * kt
    mid = (
        0.9511
        - 0.1604 * kt
        + 4.388 * kt ** 2
        - 16.638 * kt ** 3
        + 12.336 * kt ** 4
    )
    return np.where(kt <= 0.22, low, np.where(kt <= 0.80, mid, 0.165))


def plane_of_array(ghi, cos_zenith, cos_incidence, tilt_deg, albedo=GROUND_ALBEDO):
    """Plane-of-array irradiance, W/m^2, from measured horizontal global irradiance.

    Returns ``(poa, kt, diffuse_fraction, dni)``. The beam term is zeroed when the sun is behind
    the plane; diffuse and ground-reflected terms are not.
    """
    cos_zenith = np.maximum(cos_zenith, 1e-6)
    kt = np.clip(ghi / (SOLAR_CONSTANT * cos_zenith), 0.0, 1.0)
    diffuse_fraction = erbs_diffuse_fraction(kt)
    dhi = diffuse_fraction * ghi
    dni = (ghi - dhi) / cos_zenith
    cos_tilt = np.cos(np.radians(tilt_deg))
    beam = dni * np.maximum(cos_incidence, 0.0)
    poa = (
        beam
        + dhi * (1.0 + cos_tilt) / 2.0
        + ghi * albedo * (1.0 - cos_tilt) / 2.0
    )
    return np.maximum(poa, 0.0), kt, diffuse_fraction, dni


def cell_temperature(t_ambient_c, poa):
    """NOCT module temperature model, degrees C."""
    return t_ambient_c + (NOCT_C - 20.0) / 800.0 * poa


def dc_power(dc_capacity_wp, poa, t_cell_c):
    """DC array power before the inverter, W."""
    return (
        dc_capacity_wp
        * (poa / G_STC)
        * (1.0 + GAMMA_PER_C * (t_cell_c - T_REF_C))
    )


def ac_power(dc_capacity_wp, ac_capacity_wp, poa, t_cell_c, eta_inv=ETA_INV):
    """AC power expectation, W, including inverter efficiency and hard AC clipping.

    ``min(dc * eta, ac_capacity)`` is what makes a DC/AC ratio above one observable: past the
    inverter's limit the curve flattens instead of following irradiance, which is exactly the
    behaviour a residual model cannot invent and must be told about.
    """
    dc = np.maximum(dc_power(dc_capacity_wp, poa, t_cell_c), 0.0) * eta_inv
    return np.minimum(dc, ac_capacity_wp)
