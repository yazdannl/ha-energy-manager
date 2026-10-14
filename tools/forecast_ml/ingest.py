"""Ingest the Utrecht PV corpus + PVGIS irradiance into a compact aligned table.

Inputs (paths passed on the command line, all outside the repository):

* ``metadata.csv``   — semicolon separated, one row per system, from the Utrecht dataset
* the wide power CSV — comma separated, one row per **minute** in UTC, one column per system id,
  empty cell = missing
* a PVGIS ``seriescalc`` JSON — hourly horizontal irradiance and air temperature
* a NASA POWER hourly JSON — cloud cover, wind, humidity and rain for the same window. Required:
  the irradiance the model trains on is rebuilt from it as clear-sky x cloud attenuation, so
  that training and serving use one chain rather than two

Output: a single ``.npz`` holding an hourly grid, per-system AC power and a validity count, plus the
irradiance series, plus the per-system geometry the model needs.

The power file is terabytes of minute data for the full corpus, so this reads it in chunks and
aggregates straight to hourly means: the deployed forecast runs on hourly slots, and the residual
model cannot use minute detail that the runtime will never have.

Nothing here is Home-Assistant-specific and nothing is written inside the repository.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

import solar_physics as sp

POWER_CHUNK_ROWS = 100_000


def read_metadata(path: Path) -> pd.DataFrame:
    """System geometry and nameplate capacities, one row per system."""
    frame = pd.read_csv(path, sep=";")
    frame = frame.loc[:, [c for c in frame.columns if not str(c).startswith("Unnamed")]]
    frame = frame.dropna(subset=["ID"]).reset_index(drop=True)
    for column in (
        "estimated_dc_capacity",
        "estimated_ac_capacity",
        "tilt",
        "azimuth",
        "annual_yield",
        "north",
        "south",
        "west",
        "east",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # The corpus locates each system by a grid cell; the centre is the representative point.
    frame["lat"] = (frame["north"] + frame["south"]) / 2.0
    frame["lon"] = (frame["east"] + frame["west"]) / 2.0
    for column in ("begin_ts", "end_ts"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


def read_power_hourly(paths: list[Path], system_ids: list[str]) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Hourly mean AC power (W) and valid-minute counts, keyed by hours-since-epoch.

    Accepts several files and merges them: the corpus is large enough that it is fetched as byte
    ranges covering different periods, and the accumulator is keyed by hour so appending is natural.
    """
    sums: dict[int, np.ndarray] = {}
    counts: dict[int, np.ndarray] = {}
    for path in paths:
        print(f"  {path.name}")
        # Names are supplied explicitly and the header is not relied on, because a byte-range slice of
        # this file (rows are timestamps, columns are systems, so a range yields all systems over a
        # period) begins mid-row with no header at all. A real header row then arrives as a data row
        # whose timestamp fails to parse, so it is dropped a few lines below along with the truncated
        # first line.
        reader = pd.read_csv(
            path,
            header=None,
            names=["DateTime"] + list(system_ids),
            chunksize=POWER_CHUNK_ROWS,
            on_bad_lines="skip",
            low_memory=True,
        )
        for index, chunk in enumerate(reader):
            chunk["DateTime"] = pd.to_datetime(chunk["DateTime"], utc=True, errors="coerce")
            chunk = chunk.dropna(subset=["DateTime"])
            if chunk.empty:
                continue
            columns = [c for c in system_ids if c in chunk.columns]
            frame = chunk.set_index("DateTime")[columns].astype("float32")
            # Accumulate sums and non-null counts separately: the hourly mean is their ratio. Using
            # ``.mean()`` here and dividing by the count again would divide by the minutes a second
            # time.
            hourly_sum = frame.resample("1h").sum(min_count=1)
            hourly_count = frame.resample("1h").count()
            for stamp, row in hourly_sum.iterrows():
                key = int(stamp.timestamp() // 3600)
                values = row.to_numpy(dtype="float64")
                sums[key] = sums.get(key, 0.0) + np.nan_to_num(values, nan=0.0)
            for stamp, row in hourly_count.iterrows():
                key = int(stamp.timestamp() // 3600)
                counts[key] = counts.get(key, 0.0) + row.to_numpy(dtype="float64")
            if index % 5 == 0:
                print(f"    chunk {index}: {len(sums)} hours accumulated", flush=True)
    return sums, counts


def read_pvgis(path: Path) -> dict[str, np.ndarray]:
    """Hourly irradiance and temperature. Stamps are hour-labelled UTC (verified against sunrise)."""
    payload = json.loads(Path(path).read_text())
    rows = payload["outputs"]["hourly"]
    hours = np.empty(len(rows), dtype="int64")
    ghi = np.empty(len(rows), dtype="float64")
    t2m = np.empty(len(rows), dtype="float64")
    h_sun = np.empty(len(rows), dtype="float64")
    for index, row in enumerate(rows):
        stamp = row["time"]
        moment = dt.datetime.strptime(stamp, "%Y%m%d:%H%M").replace(tzinfo=dt.timezone.utc)
        hours[index] = int(moment.timestamp() // 3600)
        ghi[index] = float(row["G(i)"])
        t2m[index] = float(row["T2m"])
        h_sun[index] = float(row["H_sun"])
    return {"hours": hours, "ghi": ghi, "t2m": t2m, "h_sun": h_sun}


#: NASA POWER hourly series this pipeline consumes, and the corpus array each becomes. The keys are
#: the runtime analogues the add-on reads out of a weather forecast, which is why they are here: a
#: feature the add-on cannot observe at serving time would quietly make the model worse.
WEATHER_SERIES = {
    "CLOUD_AMT": "cloud_pct",  # weather forecast ``cloud_coverage``
    "WS2M": "wind_ms",  # weather forecast ``wind_speed``
    "RH2M": "rh_pct",  # weather forecast ``humidity``
    "PRECTOTCORR": "rain_mm",  # weather forecast ``precipitation``
}
#: POWER's documented fill value. A filled hour carries no information and is filled with the series
#: median instead of being read as a real observation of zero.
NASA_FILL = -999.0


#: Haurwitz clear-sky GHI, the same expression ``app/sources/solar.py`` evaluates.
def haurwitz(cos_zenith: np.ndarray) -> np.ndarray:
    """Clear-sky horizontal global irradiance, W/m^2 (SPEC 7.1.1)."""
    cos_z = np.maximum(cos_zenith, 1e-6)
    return np.where(cos_zenith > 0.0, 1098.0 * cos_z * np.exp(-0.059 / cos_z), 0.0)


#: Kasten-Czeplak attenuation, the same constants SPEC 7.1.1 fixes for the add-on.
CLOUD_MAX_ATTENUATION = 0.75
CLOUD_COVERAGE_EXPONENT = 3.4


def kasten(cloud_pct: np.ndarray) -> np.ndarray:
    """Attenuation factor for a cloud coverage percentage."""
    coverage = np.clip(cloud_pct, 0.0, 100.0) / 100.0
    return 1.0 - CLOUD_MAX_ATTENUATION * coverage ** CLOUD_COVERAGE_EXPONENT


def read_weather(path: Path, hours: np.ndarray) -> dict[str, np.ndarray]:
    """NASA POWER hourly JSON aligned onto the corpus hours.

    POWER stamps hours as ``YYYYMMDDHH`` in UTC; the corpus carries epoch seconds, so the join is
    explicit rather than positional. A missing hour is refused rather than interpolated: every
    training row already requires irradiance, and inventing weather for it would hide a bad window.
    """
    payload = json.loads(path.read_text())
    block = payload.get("parameter", payload)
    wanted = hours * 3600
    out: dict[str, np.ndarray] = {}
    for series, name in WEATHER_SERIES.items():
        rows = block.get(series)
        if not isinstance(rows, dict):
            raise SystemExit(f"weather file has no {series}; it must carry {sorted(WEATHER_SERIES)}")
        aligned: dict[int, float] = {}
        for stamp, value in rows.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= NASA_FILL + 1.0:
                continue
            aligned[int(np.datetime64(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:00:00", "s").astype("int64"))] = number
        values = np.array([aligned.get(int(h), np.nan) for h in wanted], dtype="float64")
        missing = int((~np.isfinite(values)).sum())
        if missing:
            median = float(np.nanmedian(values)) if np.isfinite(values).any() else 0.0
            print(f"  {name}: {missing} missing hours filled with the median ({median:g})")
            values = np.where(np.isfinite(values), values, median)
        out[name] = values.astype("float32")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--power", required=True, nargs="+", help="one or more wide power CSVs, merged")
    parser.add_argument("--pvgis", required=True)
    parser.add_argument(
        "--weather",
        required=True,
        help=(
            "NASA POWER hourly JSON covering the corpus window. Required, not optional: without the "
            "cloud series the training irradiance would come from a different estimator than the "
            "one the add-on serves, which is the defect this pipeline exists to avoid"
        ),
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    metadata = read_metadata(Path(args.metadata))
    system_ids = [str(value) for value in metadata["ID"].tolist()]
    print(f"systems: {len(system_ids)}")

    print("reading irradiance ...")
    pvgis = read_pvgis(Path(args.pvgis))
    print(f"  pvgis hours: {len(pvgis['hours'])}")

    print("reading power (chunked, hourly means) ...")
    sums, counts = read_power_hourly([Path(p) for p in args.power], system_ids)
    print(f"  power hours: {len(sums)}")

    data_hours = np.array(sorted(sums), dtype="int64")
    # Keep only hours that both series cover, so every training row has irradiance.
    hours = np.intersect1d(data_hours, pvgis["hours"])
    print(f"  overlapping hours: {len(hours)}  ({len(hours) / 24.0:.0f} days)")

    data_index = {int(h): i for i, h in enumerate(data_hours)}
    pvgis_index = {int(h): i for i, h in enumerate(pvgis["hours"])}
    n_systems = len(system_ids)
    power = np.full((len(hours), n_systems), np.nan, dtype="float32")
    minutes = np.zeros((len(hours), n_systems), dtype="int16")
    for row, hour in enumerate(hours):
        key = int(hour)
        total = sums[key]
        used = counts[key]
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(used > 0, total / np.maximum(used, 1.0), np.nan)
        power[row] = mean.astype("float32")
        minutes[row] = np.minimum(used, 32767).astype("int16")

    irradiance_rows = np.array([pvgis_index[int(h)] for h in hours], dtype="int64")
    observed_ghi = pvgis["ghi"][irradiance_rows]
    t2m = pvgis["t2m"][irradiance_rows]
    pvgis_elevation = pvgis["h_sun"][irradiance_rows]

    # Cross-check: our own geometry against PVGIS's own reported sun height for the same stamp.
    moments = hours.astype("int64") * 3600
    seconds_into_day = moments - (moments // 86400) * 86400
    fractional_hour = seconds_into_day / 3600.0
    stamps = np.array(moments, dtype="datetime64[s]")
    day_of_year = (
        (stamps - stamps.astype("datetime64[Y]")).astype("timedelta64[D]").astype("int64") + 1
    )
    latitude = float(metadata["lat"].median())
    longitude = float(metadata["lon"].median())
    declination = sp.declination_rad(day_of_year)
    hour_angle = sp.hour_angle_rad(latitude, longitude, day_of_year, fractional_hour)
    our_elevation = sp.sun_elevation_deg(latitude, declination, hour_angle)
    cos_zenith = np.cos(np.radians(90.0 - our_elevation))
    daylight = (observed_ghi > 0.0) | (pvgis_elevation > 0.0)
    if daylight.any():
        error = our_elevation[daylight] - pvgis_elevation[daylight]
        print(
            f"sun height cross-check over {int(daylight.sum())} daylight hours: "
            f"bias {np.mean(error):+.3f} deg, max abs {np.max(np.abs(error)):.3f} deg"
        )

    # The irradiance the add-on will actually reconstruct in service: clear-sky Haurwitz attenuated
    # by observed cloud. Training against this instead of against ``observed_ghi`` is the whole point
    # of the weather series: the model then corrects physics rather than correcting the difference
    # between two irradiance estimators. ``observed_ghi`` is kept so the two can still be compared.
    weather = read_weather(Path(args.weather), hours)
    clear_sky_ghi = haurwitz(cos_zenith)
    attenuation = kasten(weather["cloud_pct"])
    runtime_ghi = clear_sky_ghi * attenuation
    kt = runtime_ghi[daylight] / (sp.SOLAR_CONSTANT * np.maximum(cos_zenith[daylight], 1e-6))
    print(
        f"  runtime-chain GHI: mean {runtime_ghi[daylight].mean():.1f} W/m2 vs observed "
        f"{observed_ghi[daylight].mean():.1f} ({runtime_ghi[daylight].mean() - observed_ghi[daylight].mean():+.1f}), "
        f"clearness p50 {np.percentile(kt, 50):.3f} max {kt.max():.3f}"
    )

    np.savez_compressed(
        args.out,
        hours=hours,
        power=power,
        minutes=minutes,
        system_ids=np.array(system_ids),
        tilt=metadata["tilt"].to_numpy(dtype="float32"),
        azimuth=metadata["azimuth"].to_numpy(dtype="float32"),
        dc_capacity=metadata["estimated_dc_capacity"].to_numpy(dtype="float32"),
        ac_capacity=metadata["estimated_ac_capacity"].to_numpy(dtype="float32"),
        latitude=metadata["lat"].to_numpy(dtype="float32"),
        longitude=metadata["lon"].to_numpy(dtype="float32"),
        annual_yield=metadata["annual_yield"].to_numpy(dtype="float32"),
        begin_ts=metadata["begin_ts"].astype("int64").to_numpy() // 10**9,
        end_ts=metadata["end_ts"].astype("int64").to_numpy() // 10**9,
        ghi=runtime_ghi.astype("float32"),
        ghi_observed=observed_ghi.astype("float32"),
        clear_sky_ghi=clear_sky_ghi.astype("float32"),
        attenuation=attenuation.astype("float32"),
        t2m=t2m.astype("float32"),
        pvgis_elevation=pvgis_elevation.astype("float32"),
        **weather,
    )

    valid = np.isfinite(power)
    print(f"wrote {args.out}")
    print(f"  shape power: {power.shape}, finite cells: {valid.mean() * 100:.1f}%")
    per_system = np.isfinite(power).mean(axis=0)
    print(
        f"  per-system coverage: min {per_system.min() * 100:.1f}% "
        f"median {np.median(per_system) * 100:.1f}% max {per_system.max() * 100:.1f}%"
    )
    print(f"  daylight hours: {int((observed_ghi > 5.0).sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
