"""WP1 executable checks for the external contracts documented in docs/EXTERNAL_APIS.md.

Pure stdlib, no network, no imports from app/. Run with either:

    python3 tests/test_wp1_external_contracts.py
    python3 -m pytest tests/test_wp1_external_contracts.py -q

These tests pin the *normalisation* facts a provider must not get wrong; they are deliberately
tiny helpers rather than production code (WP2A owns the real providers).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# --- helpers under test -------------------------------------------------------------------


def zonneplan_price_eur_per_kwh(raw: int | float) -> float:
    """docs/EXTERNAL_APIS.md section 3: Zonneplan fixed-point integers are EUR/kWh * 10^7."""
    return float(raw) / 10**7


def energy_charts_price_eur_per_kwh(price_eur_per_mwh: float) -> float:
    """docs/EXTERNAL_APIS.md section 2: legacy and v2 publish EUR/MWh (ex-tax)."""
    return float(price_eur_per_mwh) / 1000.0


def aware_utc(value: str) -> datetime:
    """Reject timestamps without an offset.

    Forecast.Solar's default `YYYY-MM-DD HH:MM:SS` format is local-without-offset; treating it as UTC
    silently shifts the whole solar plan. The provider must pass ?time=utc or ?time=iso8601.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def zonneplan_forecast_entries(payload: dict, now: datetime) -> list[tuple[datetime, float]]:
    """Normalise both upstream Zonneplan `forecast` shapes into sorted future (start, EUR/kWh).

    Handles the 15-minute shape (`price_tax_included.amount` + `end_date`) and the legacy hourly
    shape (`electricity_price`, one hour long, no `end_date`).
    """
    out: list[tuple[datetime, float]] = []
    for entry in payload.get("forecast") or []:
        start = aware_utc(entry["start_date"])
        if "price_tax_included" in entry:
            price = zonneplan_price_eur_per_kwh(entry["price_tax_included"]["amount"])
            end = aware_utc(entry["end_date"]) if entry.get("end_date") else start + timedelta(minutes=15)
        else:
            price = zonneplan_price_eur_per_kwh(entry["electricity_price"])
            end = start + timedelta(hours=1)
        if end > now:
            out.append((start, price))
    return sorted(out)


# --- tests --------------------------------------------------------------------------------


def test_zonneplan_scale_is_10_pow_7():
    # 0.25 EUR/kWh is published as 2500000; the sensor state divides by 1e7.
    assert zonneplan_price_eur_per_kwh(2_500_000) == 0.25
    assert zonneplan_price_eur_per_kwh(2_500_000) + 1 == 1.25


def test_energy_charts_mwh_to_kwh():
    assert energy_charts_price_eur_per_kwh(250.0) == 0.25
    assert energy_charts_price_eur_per_kwh(-50.0) == -0.05


def test_forecast_solar_default_timestamp_is_rejected():
    for bad in ("2026-01-01 12:00:00", "2026-01-01T12:00:00"):
        try:
            aware_utc(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"naive timestamp accepted: {bad!r}")
    assert aware_utc("2026-01-01T12:00:00+00:00").hour == 12
    assert aware_utc("2026-01-01T13:00:00+01:00").hour == 12


def test_zonneplan_quarter_hour_shape_filters_past():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "forecast": [
            {  # past slot -> dropped
                "start_date": "2026-01-01T11:45:00+00:00",
                "end_date": "2026-01-01T12:00:00+00:00",
                "price_tax_included": {"amount": 2_000_000},
            },
            {  # current/future slot -> kept
                "start_date": "2026-01-01T12:00:00+00:00",
                "end_date": "2026-01-01T12:15:00+00:00",
                "price_tax_included": {"amount": 2_500_000},
            },
        ]
    }
    assert zonneplan_forecast_entries(payload, now) == [
        (now, 0.25)
    ]


def test_zonneplan_legacy_hourly_shape_and_missing_end_date():
    now = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
    payload = {
        "forecast": [
            {  # starts before now but its implicit 1 h ends after now -> kept
                "start_date": "2026-01-01T12:00:00+00:00",
                "electricity_price": 3_000_000,
            },
            {  # already over -> dropped
                "start_date": "2026-01-01T11:00:00+00:00",
                "electricity_price": 1_000_000,
            },
        ]
    }
    assert zonneplan_forecast_entries(payload, now) == [
        (datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), 0.3)
    ]


def test_energy_charts_legacy_shape_is_parallel_arrays():
    payload = {"unix_seconds": [1_767_225_600, 1_767_226_500], "price": [250.0, -50.0], "unit": "EUR / MWh"}
    slots = [
        (datetime.fromtimestamp(ts, tz=timezone.utc), energy_charts_price_eur_per_kwh(p))
        for ts, p in zip(payload["unix_seconds"], payload["price"], strict=True)
    ]
    assert len(slots) == 2
    assert slots[0][1] == 0.25 and slots[1][1] == -0.05


def test_statistics_start_end_are_milliseconds():
    row = {"start": 1_767_225_600_000, "end": 1_767_229_200_000, "mean": 1.0}
    start = datetime.fromtimestamp(row["start"] / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(row["end"] / 1000, tz=timezone.utc)
    assert (end - start) == timedelta(hours=1)


# --- NASA POWER hourly irradiance (docs/EXTERNAL_APIS.md section 8) -------------------------


def nasa_fill_value() -> float:
    """The POWER hourly fill value: missing data, never a 0 W/m^2 measurement."""
    return -999.0


def nasa_stamp_to_utc(stamp: str) -> datetime:
    """Hourly keys are UTC ``YYYYMMDDHH``; reading them as local time shifts the whole profile."""
    return datetime.strptime(str(stamp), "%Y%m%d%H").replace(tzinfo=timezone.utc)


def nasa_clearness(all_sky: float, clear_sky: float) -> float:
    """Observed clearness index; only daylight (a positive clear-sky value) is usable."""
    if clear_sky <= 0:
        raise ValueError("no clear-sky reference: twilight or night")
    return all_sky / clear_sky


def test_nasa_power_stamps_are_utc_hours():
    assert nasa_stamp_to_utc("2026011500") == datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    assert nasa_stamp_to_utc("2026011523").hour == 23


def test_nasa_power_fill_is_dropped_and_twilight_has_no_ratio():
    assert nasa_fill_value() < 0.0
    assert nasa_clearness(300.0, 600.0) == 0.5
    assert nasa_clearness(0.0, 600.0) == 0.0
    raised = False
    try:
        nasa_clearness(300.0, 0.0)
    except ValueError:
        raised = True
    assert raised, "a night hour has no clear-sky reference to divide by"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all WP1 contract checks passed")
