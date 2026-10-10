"""WP2A: Forecast.Solar hourly/single-plane contract, cache, 429 back-off, bias (SPEC §7.1, App. A)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import Settings  # noqa: E402
from app.sources import ProviderError, RateLimited  # noqa: E402
from app.sources.solar import (  # noqa: E402
    BACKOFF_KEY,
    BACKOFF_KIND,
    _interpolate,
    _parse_watts,
    fetch_plane,
    load_solar_forecast,
    solar_bias_factors,
    update_solar_bias,
)
from app.store import Store  # noqa: E402

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
PLANE = {"kwp": 5.0, "tilt_deg": 35.0, "azimuth_deg": 180.0, "damping": 0.4}


def watts_payload(watts: float, *, hours: int = 6, start: datetime | None = None, ramp: bool = True) -> dict:
    """Hourly rows in the ``?time=utc`` shape, with the private ``message.info`` block included."""
    origin = (start or NOW).replace(minute=0, second=0, microsecond=0)
    table = {
        (origin + timedelta(hours=index)).isoformat(): watts + (index if ramp else 0)
        for index in range(hours + 1)
    }
    return {
        "result": {
            "watts": table,
            "watt_hours_period": {key: value for key, value in table.items()},
            "watt_hours_day": {origin.date().isoformat(): watts * hours},
        },
        "message": {
            "code": 0,
            "text": "",
            "info": {
                "latitude": 52.0,
                "longitude": 5.0,
                "place": "Example Street 1",
                "timezone": "Europe/Amsterdam",
            },
            "ratelimit": {"period": 3600, "limit": 12, "remaining": 10},
        },
    }


class HttpStub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else watts_payload(0.0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "solar.db")
    yield instance
    instance.close()


def solar_settings(store=None, **overrides):
    options = {"solar.enabled": True, "solar.planes": [PLANE], "solar.bias_correction": False}
    options.update(overrides)
    return Settings(store, options=options)


def test_interpolate_hourly_curve_onto_quarter_grid():
    series = [(NOW, 0.0), (NOW + timedelta(hours=1), 1000.0), (NOW + timedelta(hours=2), 400.0)]
    assert _interpolate(series, NOW - timedelta(hours=1)) == 0.0  # before the series
    assert _interpolate(series, NOW) == pytest.approx(0.0)
    assert _interpolate(series, NOW + timedelta(minutes=15)) == pytest.approx(250.0)
    assert _interpolate(series, NOW + timedelta(minutes=30)) == pytest.approx(500.0)
    assert _interpolate(series, NOW + timedelta(hours=1, minutes=30)) == pytest.approx(700.0)
    assert _interpolate(series, NOW + timedelta(hours=5)) == pytest.approx(400.0)  # after the series


def test_interpolate_keeps_first_partial_sample_anchored():
    # sunrise fill rows are irregular: 07:29 then 08:00; the ramp must start at 07:29, not at 08:00.
    first = NOW.replace(hour=7, minute=29, second=0, microsecond=0)
    series = [(first, 0.0), (first + timedelta(minutes=31), 1550.0)]
    assert _interpolate(series, first) == pytest.approx(0.0)
    assert _interpolate(series, first + timedelta(minutes=15, seconds=30)) == pytest.approx(775.0)
    assert _interpolate(series, first + timedelta(minutes=31)) == pytest.approx(1550.0)


def test_parse_watts_tolerates_offsetless_keys_as_utc():
    payload = {"result": {"watts": {"2026-01-05 12:00:00": 100, "2026-01-05T13:00:00+01:00": 200}}}
    series = _parse_watts(payload)
    assert series == [
        (datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), 200.0),
    ]


def test_fetch_plane_url_time_utc_azimuth_and_privacy():
    http = HttpStub(watts_payload(1234.0))
    payload = fetch_plane(52.0, 5.0, PLANE, api_key="personal-key", http=http)
    url = http.urls[0]
    assert url.startswith("https://api.forecast.solar/personal-key/estimate/52.0000/5.0000/35/0/5?")
    assert "time=utc" in url
    assert "damping_morning=0.4" in url and "damping_evening=0.4" in url
    assert payload["result"]["watts"]
    assert "info" not in payload["message"]  # coordinates/place are never returned or cached
    assert "Example Street 1" not in repr(payload)

    # compass azimuth: 0 = north -> -180; 90 = east -> -90; 270 = west -> +90
    for compass, expected in ((0.0, "-180"), (90.0, "-90"), (270.0, "90"), (180.0, "0")):
        probe = HttpStub(watts_payload(1.0))
        fetch_plane(52.0, 5.0, {**PLANE, "azimuth_deg": compass}, http=probe)
        assert f"/{expected}/5?" in probe.urls[0]


def test_fetch_plane_validates_and_removes_bad_damping():
    bad = HttpStub({"result": {}, "message": {"code": 1, "text": "invalid key"}})
    with pytest.raises(ProviderError) as err:
        fetch_plane(52.0, 5.0, PLANE, http=bad)
    assert "invalid key" in str(err.value)
    with pytest.raises(ProviderError):
        fetch_plane(52.0, 5.0, {**PLANE, "kwp": 0}, http=HttpStub({}))
    # a rejected damping value is dropped and the request retried once (appendix A item 4)
    retry = HttpStub(ProviderError("bad damping", status=400), watts_payload(10.0))
    fetch_plane(52.0, 5.0, {**PLANE, "damping": 5.0}, http=retry)
    assert len(retry.urls) == 2
    assert "damping" not in retry.urls[1] and "time=utc" in retry.urls[1]


def test_load_solar_keyless_single_plane_warns(store):
    http = HttpStub(watts_payload(1000.0), watts_payload(500.0))
    settings = solar_settings(store, **{"solar.planes": [PLANE, {**PLANE, "kwp": 3.0}]})
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert status.ok is True
    assert len(http.urls) == 1  # keyless: never one request per plane
    assert status.detail["planes_used"] == 1 and status.detail["planes_ignored"] == 1
    assert any("single-plane" in note for note in forecast.notes)
    assert len(forecast.planes) == 1
    assert len(forecast.slots) == settings.get("optimizer.horizon_hours") * 4
    assert forecast.slots[0].t == NOW
    assert forecast.slots[0].pv_kw == pytest.approx(1.0)
    assert forecast.slots[0].uncertainty_pct == pytest.approx(25.0)


def test_load_solar_keyed_sums_planes(store):
    http = HttpStub(watts_payload(1000.0), watts_payload(500.0))
    settings = solar_settings(
        store, **{"solar.planes": [PLANE, {**PLANE, "kwp": 3.0}], "solar.api_key": "personal-key"}
    )
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert len(http.urls) == 2 and all(url.startswith("https://api.forecast.solar/personal-key/") for url in http.urls)
    assert forecast.slots[0].pv_kw == pytest.approx(1.5)
    assert status.detail["planes_used"] == 2


def test_load_solar_caches_on_the_15_minute_grid(store):
    http = HttpStub(watts_payload(1000.0))
    settings = solar_settings(store)
    first, _ = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    cached, _ = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW + timedelta(minutes=29), http=http
    )
    assert len(http.urls) == 1
    assert first.slots[0].pv_kw == pytest.approx(1.0)
    # hourly 1000 -> 1001 W is a linear ramp: +0.25 W at 12:15
    assert first.slots[1].pv_kw == pytest.approx(1000.25 / 1000.0)
    assert cached.slots[0].pv_kw == pytest.approx(1000.25 / 1000.0)


def test_rate_limit_backoff_serves_cache(store):
    settings = solar_settings(store)
    cache_http = HttpStub(watts_payload(800.0, ramp=False))
    first, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=cache_http)
    assert status.ok and first.slots[0].pv_kw == pytest.approx(0.8)

    limited = HttpStub(RateLimited("too many requests"))
    later = NOW + timedelta(minutes=31)
    served, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=later, http=limited)
    assert len(limited.urls) == 1
    assert served.slots[0].pv_kw == pytest.approx(0.8)
    assert any("429" in note for note in served.notes)
    backoff = store.profile_get(BACKOFF_KIND, BACKOFF_KEY)[1]
    assert backoff["failures"] == 1 and backoff["until"] is not None

    def explode(url, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("HTTP call during back-off")

    served, _ = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=later + timedelta(seconds=30), http=explode
    )
    assert served.slots[0].pv_kw == pytest.approx(0.8)
    assert any("back-off" in note for note in served.notes)


def test_rate_limit_without_cache_reports_failure(store):
    settings = solar_settings(store)
    forecast, status = load_solar_forecast(
        settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=HttpStub(RateLimited("429"))
    )
    assert status.ok is False and status.stale is True
    assert forecast.slots == []


def test_bias_correction_is_applied_and_clamped(store):
    http = HttpStub(watts_payload(1000.0))
    settings = solar_settings(store, **{"solar.bias_correction": True})
    update_solar_bias(store, [(NOW, 1.0, 0.5)], now=NOW, tz="UTC")
    factors = solar_bias_factors(store, now=NOW)
    assert factors[12] == pytest.approx(0.9)
    assert factors[3] == pytest.approx(1.0)
    forecast, status = load_solar_forecast(settings, store, None, location=(52.0, 5.0, "UTC"), now=NOW, http=http)
    assert status.ok
    assert forecast.slots[0].pv_kw == pytest.approx(1.0 * 0.9)
    total = settings.get("optimizer.horizon_hours") * 4
    assert forecast.correction_applied == pytest.approx((8 * 0.9 + (total - 8) * 1.0) / total)
    assert status.detail["bias_correction"] is True

    update_solar_bias(store, [(NOW, 1.0, 1000.0)], now=NOW, tz="UTC")
    assert all(0.3 <= factor <= 1.8 for factor in solar_bias_factors(store, now=NOW))


def test_disabled_missing_location_and_no_planes(store):
    disabled = solar_settings(store, **{"solar.enabled": False})
    forecast, status = load_solar_forecast(disabled, store, None, location=(52.0, 5.0, "UTC"), now=NOW)
    assert status.active is False and status.ok is True and forecast.slots == []
    assert "disabled" in " ".join(forecast.notes)

    no_planes = solar_settings(store, **{"solar.planes": []})
    _, status = load_solar_forecast(no_planes, store, None, location=(52.0, 5.0, "UTC"), now=NOW)
    assert status.ok is True and status.detail["reason"] == "no planes configured"

    forecast, status = load_solar_forecast(solar_settings(store), store, None, location=None, now=NOW)
    assert status.ok is False and forecast.slots == []
    assert "location unavailable" in status.last_error
