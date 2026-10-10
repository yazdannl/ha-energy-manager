"""WP2A: price providers, unit normalisation and the fallback chain (SPEC §4, appendix A)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "energy_manager") not in sys.path:
    sys.path.insert(0, str(ROOT / "energy_manager"))

from app.config import DEFAULTS, Settings  # noqa: E402
from app.harest import HAError  # noqa: E402
from app.models import PriceForecast, PriceSlot  # noqa: E402
from app.sources import ProviderError  # noqa: E402
from app.sources.price import (  # noqa: E402
    PROVIDERS,
    SELL_UNKNOWN_NOTE,
    PriceUnavailable,
    ZONNEPLAN_SCALE_DIVISOR,
    fetch_energy_charts,
    fetch_entity_forecast,
    fetch_static,
    fetch_zonneplan_entity,
    load_price_forecast,
)

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # Monday


class FakeClient:
    def __init__(self, state):
        self.state = state

    def get_state(self, entity_id):
        return self.state


def sensor_state(entries, unit="€/kWh", extra=None, state="0.1"):
    attributes = {"unit_of_measurement": unit, "friendly_name": "Example price"}
    if entries is not None:
        attributes["forecast"] = entries
    if extra:
        attributes.update(extra)
    return {"entity_id": "sensor.example_price_now", "state": state, "attributes": attributes}


def fifteen_minute_entries(start: datetime, count: int, price=0.10):
    return [
        {"datetime": (start + timedelta(minutes=15 * index)).isoformat(), "price": price + index * 0.001}
        for index in range(count)
    ]


def zonneplan_entries(start: datetime, count: int, amount: int = 1_000_000, step: int = 10_000):
    """Zonneplan shapes: fixed-point integers, both legacy and quarter-hourly keys."""
    return [
        {
            "start_date": (start + timedelta(minutes=15 * index)).isoformat(),
            "end_date": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
            "price_tax_included": {"amount": amount + index * step},
            "price_tax_excluded": {"amount": (amount + index * step) // 2},
            "sustainability_score": 100,
        }
        for index in range(count)
    ]


def settings(**options):
    return Settings(None, options=options)


def test_registry_covers_spec_sources():
    assert set(PROVIDERS) == {"zonneplan_entity", "energy_charts", "entity_forecast", "static"}


def slot_provider(buy=0.2, sell=0.0, slots=4):
    """Custom provider for the sell/feed-in tests; no fabricated sell price."""

    def provider(client, settings, now=None, http=None, tz=None):
        moment = now or NOW
        start = moment.replace(minute=0, second=0, microsecond=0)
        return PriceForecast(
            slots=[
                PriceSlot(t=start + timedelta(minutes=15 * index), buy=buy, sell=sell)
                for index in range(slots)
            ],
            generated_at=moment,
            source="example_provider",
        )

    return provider


def test_never_derives_sell_from_buy_without_a_feed_in_tariff():
    configured = settings(**{"price.export_fee_eur_per_kwh": 0.005})
    forecast, statuses = load_price_forecast(
        configured,
        None,
        now=NOW,
        providers={"zonneplan_entity": slot_provider(sell=0.05), "static": slot_provider(sell=0.05)},
    )
    assert forecast.slots[0].sell == 0.05  # untouched: this provider states its own sell series
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is False
    assert SELL_UNKNOWN_NOTE in forecast.notes


def test_flat_feed_in_tariff_fills_sell_net_of_the_export_fee():
    configured = settings(
        **{
            "price.feed_in_eur_per_kwh": 0.05,
            "price.export_fee_eur_per_kwh": 0.005,
            "price.vat_pct": 21.0,
            "price.feed_in_tax_inclusive": True,
        }
    )
    forecast, statuses = load_price_forecast(
        configured, None, now=NOW, providers={"zonneplan_entity": slot_provider()}
    )
    assert all(slot.sell == pytest.approx(0.045) for slot in forecast.slots)
    assert all(slot.buy == pytest.approx(0.2) for slot in forecast.slots)  # buy untouched
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is True
    assert SELL_UNKNOWN_NOTE not in forecast.notes
    assert any("0.0450 EUR/kWh received" in note for note in forecast.notes)


def test_feed_in_entity_overrides_flat_and_adds_vat_when_exclusive():
    client = FakeClient(
        {
            "entity_id": "sensor.example_feed_in",
            "state": "9",
            "attributes": {"unit_of_measurement": "ct/kWh"},
        }
    )
    configured = settings(
        **{
            "price.feed_in_entity": "sensor.example_feed_in",
            "price.feed_in_eur_per_kwh": 0.99,  # ignored: the entity wins
            "price.feed_in_tax_inclusive": False,
            "price.vat_pct": 21.0,
        }
    )
    forecast, statuses = load_price_forecast(
        configured, client, now=NOW, providers={"zonneplan_entity": slot_provider()}
    )
    # 9 ct/kWh = 0.09, exclusive -> 1.21x = 0.1089. No energy tax on feed-in.
    assert all(slot.sell == pytest.approx(0.1089) for slot in forecast.slots)
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is True


def test_broken_feed_in_sensor_does_not_fail_the_buy_forecast():
    class BrokenClient:
        def get_state(self, entity_id):
            raise HAError("HA GET /states/sensor.example_feed_in failed: connection refused")

    configured = settings(**{"price.feed_in_entity": "sensor.example_feed_in"})
    forecast, statuses = load_price_forecast(
        configured, BrokenClient(), now=NOW, providers={"zonneplan_entity": slot_provider(sell=0.0)}
    )
    assert forecast.slots and forecast.slots[0].buy == pytest.approx(0.2)
    assert forecast.slots[0].sell == 0.0
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is False
    assert any("sell price unavailable" in note for note in forecast.notes)
    assert SELL_UNKNOWN_NOTE not in forecast.notes


def test_default_allow_export_is_off_until_a_feed_in_is_confirmed():
    assert DEFAULTS["optimizer.allow_export"] is False
    assert DEFAULTS["price.feed_in_eur_per_kwh"] is None
    assert DEFAULTS["price.feed_in_entity"] == "" and DEFAULTS["price.feed_in_tax_inclusive"] is True


def test_zonneplan_entity_divides_by_1e7_drops_past_and_forward_fills():
    # ~24 h of 15-minute entries plus 1 h of past slots; the state matches the current slot.
    entries = zonneplan_entries(NOW - timedelta(hours=1), 4 + 24 * 4)
    client = FakeClient(sensor_state(entries, state="0.1"))
    configured = settings(
        **{
            "price.entity": "sensor.example_price_now",
            "price.import_fee_eur_per_kwh": 0.01,
            "price.export_fee_eur_per_kwh": 0.005,
        }
    )
    forecast = fetch_zonneplan_entity(client, configured, now=NOW)
    hours = configured.get("optimizer.horizon_hours")
    assert ZONNEPLAN_SCALE_DIVISOR == 1e7
    assert forecast.source == "zonneplan_entity"
    assert len(forecast.slots) == hours * 4
    assert forecast.slots[0].t == NOW
    # entry at NOW is index 4: (1_000_000 + 4*10_000) / 1e7 = 0.104 EUR/kWh; no VAT/tax adders.
    assert forecast.slots[0].buy == pytest.approx(0.114)
    # SPEC 4: sell is EUR/kWh *received*; a provider with no feed-in series never guesses one
    assert forecast.slots[0].sell == 0.0
    assert forecast.slots[0].stale is False
    # ~24 h of real coverage < the 30 h horizon: the tail is stale/degraded, not a failure.
    assert forecast.slots[-1].stale is True
    assert forecast.degraded is True
    assert any("coverage ends" in note for note in forecast.notes)
    last_amount = 1_000_000 + (4 + 24 * 4 - 1) * 10_000
    assert forecast.slots[-1].buy == pytest.approx(last_amount / 1e7 + 0.01)


def test_zonneplan_scale_mismatch_disables_the_provider():
    entries = zonneplan_entries(NOW - timedelta(minutes=15), 8)
    bad_state = sensor_state(entries, state="0.55")  # stale payload or changed scale
    with pytest.raises(ProviderError) as err:
        fetch_zonneplan_entity(FakeClient(bad_state), settings(**{"price.entity": "x"}), now=NOW)
    assert "does not match the current slot" in str(err.value)
    # no VAT adders anywhere in the expected value: 0.1 is the whole consumer price
    assert "refusing to guess" in str(err.value)


def test_zonneplan_requires_a_slot_covering_now():
    past = zonneplan_entries(NOW - timedelta(hours=2), 4)
    with pytest.raises(ProviderError) as err:
        fetch_zonneplan_entity(FakeClient(sensor_state(past)), settings(**{"price.entity": "x"}), now=NOW)
    assert "covers now" in str(err.value)


def test_entity_forecast_accepts_generic_keys_and_cents():
    entries = [
        {"start": "2026-01-05T12:00:00Z", "price": 21.0},
        {"start": "2026-01-05T12:15:00Z", "price": 22.0},
    ]
    client = FakeClient(sensor_state(entries, unit="ct/kWh"))
    forecast = fetch_entity_forecast(client, settings(**{"price.entity": "sensor.example_price_now"}), now=NOW)
    assert forecast.slots[0].buy == pytest.approx(0.21)
    assert forecast.slots[1].buy == pytest.approx(0.22)
    # generic provider: no zonneplan fixed-point scale, no state self-check
    assert forecast.slots[0].stale is False


def test_unknown_unit_is_an_error_not_a_guess():
    client = FakeClient(sensor_state(fifteen_minute_entries(NOW, 2), unit=None))
    with pytest.raises(ProviderError):
        fetch_entity_forecast(client, settings(**{"price.entity": "x"}), now=NOW)
    forced = fetch_entity_forecast(
        client, settings(**{"price.entity": "x", "price.unit": "eur_per_mwh"}), now=NOW
    )
    assert forced.slots[0].buy == pytest.approx(0.0001)


def test_no_future_slots_is_a_failure():
    past = fifteen_minute_entries(NOW - timedelta(hours=2), 3)
    client = FakeClient(sensor_state(past))
    with pytest.raises(ProviderError) as err:
        fetch_entity_forecast(client, settings(**{"price.entity": "x"}), now=NOW)
    assert "future" in str(err.value)


def test_stale_attribute_detection():
    entries = zonneplan_entries(NOW - timedelta(minutes=15), 40)
    client = FakeClient(
        sensor_state(entries, state="0.1", extra={"forecast_updated": (NOW - timedelta(hours=5)).isoformat()})
    )
    forecast = fetch_zonneplan_entity(client, settings(**{"price.entity": "x"}), now=NOW)
    assert forecast.stale is True
    assert all(slot.stale for slot in forecast.slots)
    assert any("old" in note for note in forecast.notes)


def test_missing_entity_is_a_clean_failure():
    state = {"entity_id": "sensor.example_price_now", "state": "0.2", "attributes": {"unit_of_measurement": "€/kWh"}}
    with pytest.raises(ProviderError):
        fetch_zonneplan_entity(FakeClient(state), settings(**{"price.entity": "x"}), now=NOW)
    with pytest.raises(ProviderError):
        fetch_entity_forecast(FakeClient(state), settings(**{"price.entity": ""}), now=NOW)


def test_energy_charts_parsing_vat_and_coverage_gap():
    seconds = [int((NOW + timedelta(minutes=15 * index)).timestamp()) for index in range(8)]
    payload = {
        "unit": "EUR / MWh",
        "unix_seconds": seconds,
        "price": [100.0] * 8,
        "license_info": "CC BY 4.0 example attribution",
    }
    calls = []

    def fake_http(url, **kwargs):
        calls.append(url)
        return payload

    forecast = fetch_energy_charts(None, settings(**{"price.vat_pct": 21.0}), now=NOW, http=fake_http)
    assert calls == ["https://api.energy-charts.info/price?bzn=NL"]
    assert forecast.slots[0].buy == pytest.approx(0.1 * 1.21)
    assert forecast.source == "energy_charts"
    # today only (8 slots = 2 h): coverage gap is degraded + stale tail, never a provider failure
    assert forecast.degraded is True
    assert forecast.stale is False
    assert forecast.slots[-1].stale is True
    assert any("coverage ends" in note for note in forecast.notes)
    assert any("Energy-Charts" in note for note in forecast.notes)

    with pytest.raises(ProviderError):
        fetch_energy_charts(None, settings(), now=NOW, http=lambda url, **kw: {"unix_seconds": seconds, "price": [1.0] * 8})


def test_energy_charts_invalid_zone_is_explicit():
    def fake_http(url, **kwargs):
        raise ProviderError("provider request failed with HTTP 400", status=400)

    with pytest.raises(ProviderError) as err:
        fetch_energy_charts(None, settings(**{"price.bzn": "ZZ"}), now=NOW, http=fake_http)
    assert "bidding zone" in str(err.value) and err.value.status == 400


def test_static_schedule_uses_the_resolved_timezone():
    windows = [
        {"from_hour": 23, "to_hour": 7, "price_eur_per_kwh": 0.10, "days": []},
        {"from_hour": 7, "to_hour": 23, "price_eur_per_kwh": 0.30, "days": []},
    ]
    configured = settings(**{"price.static_schedule": windows})
    moment = datetime(2026, 1, 5, 6, 30, tzinfo=timezone.utc)  # 07:30 in Europe/Amsterdam (CET)
    assert fetch_static(None, configured, now=moment).slots[0].buy == pytest.approx(0.10)  # 06:30 UTC = night
    assert fetch_static(None, configured, now=moment, tz="Europe/Amsterdam").slots[0].buy == pytest.approx(0.30)
    # settings.timezone is the fallback when the caller has no resolved zone yet
    mixed = settings(**{"price.static_schedule": windows, "timezone": "Europe/Amsterdam"})
    assert fetch_static(None, mixed, now=moment).slots[0].buy == pytest.approx(0.30)


def test_static_schedule_day_and_overnight_windows():
    configured = settings(
        **{
            "price.static_schedule": [
                {"from_hour": 23, "to_hour": 7, "price_eur_per_kwh": 0.05, "days": [0]},
                {"from_hour": 0, "to_hour": 6, "price_eur_per_kwh": 0.10, "days": []},
                {"from_hour": 6, "to_hour": 24, "price_eur_per_kwh": 0.30, "days": []},
            ]
        }
    )
    forecast = fetch_static(None, configured, now=NOW)
    price_at = {slot.t: slot.buy for slot in forecast.slots}
    assert price_at[NOW] == pytest.approx(0.30)  # Monday noon
    assert price_at[NOW + timedelta(hours=11)] == pytest.approx(0.05)  # Monday 23:00 overnight
    assert price_at[NOW + timedelta(hours=15)] == pytest.approx(0.10)  # Tuesday 03:00
    assert forecast.stale is False
    with pytest.raises(ProviderError):
        fetch_static(None, settings(), now=NOW)


def test_fallback_chain_configured_then_energy_charts_then_static():
    def failing(client, settings, now=None, http=None, tz=None):
        raise ProviderError("configured source down")

    def good(client, settings, now=None, http=None, tz=None):
        return PriceForecast(
            slots=[PriceSlot(t=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), buy=0.2, sell=0.1, source="good")],
            generated_at=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
            source="good",
        )

    forecast, statuses = load_price_forecast(
        settings(), None, now=NOW, providers={"zonneplan_entity": failing, "energy_charts": good}
    )
    assert forecast.source == "good" and forecast.degraded is True
    assert "fell back" in " ".join(forecast.notes)
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].ok is False and by_name["zonneplan_entity"].active is True
    assert "configured source down" in by_name["zonneplan_entity"].last_error
    assert by_name["energy_charts"].ok is True

    with pytest.raises(PriceUnavailable) as err:
        load_price_forecast(
            settings(), None, now=NOW, providers={"zonneplan_entity": failing, "energy_charts": failing, "static": failing}
        )
    assert "zonneplan_entity" in str(err.value) and "static" in str(err.value)


def test_chain_survives_an_ha_outage_and_reaches_static():
    """A transport error from the configured entity provider must not abort the chain (WP5 major)."""

    class UnreachableClient:
        def get_state(self, entity_id):
            raise HAError("HA GET /states/sensor.example_price_now failed: connection refused")

    def charts_down(client, settings, now=None, http=None, tz=None):
        raise ProviderError("energy_charts: provider request failed")

    configured = settings(
        **{
            "price.entity": "sensor.example_price_now",
            "price.static_schedule": [
                {"from_hour": 0, "to_hour": 24, "price_eur_per_kwh": 0.25, "days": []}
            ],
        }
    )
    forecast, statuses = load_price_forecast(
        configured,
        UnreachableClient(),
        now=NOW,
        providers={
            "zonneplan_entity": fetch_zonneplan_entity,
            "energy_charts": charts_down,
            "static": fetch_static,
        },
    )
    assert forecast.source == "static"
    assert len(forecast.slots) == configured.get("optimizer.horizon_hours") * 4
    assert forecast.degraded is True and forecast.stale is False
    note = " ".join(forecast.notes)
    assert "fell back" in note and "zonneplan_entity" in note and "energy_charts" in note
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].ok is False and by_name["zonneplan_entity"].active is True
    assert "connection refused" in by_name["zonneplan_entity"].last_error
    assert by_name["energy_charts"].ok is False and by_name["energy_charts"].last_error
    assert by_name["static"].ok is True and by_name["static"].active is False

    # an unexpected provider error (not HAError/ProviderError) also falls through, recorded with its type
    def broken(client, settings, now=None, http=None, tz=None):
        raise TypeError("malformed payload")

    forecast, statuses = load_price_forecast(
        configured,
        None,
        now=NOW,
        providers={"zonneplan_entity": broken, "energy_charts": charts_down, "static": fetch_static},
    )
    assert forecast.source == "static"
    assert {status.name: status for status in statuses}["zonneplan_entity"].last_error == "TypeError: malformed payload"


def test_chain_treats_a_returned_empty_or_all_past_forecast_as_a_failure():
    """SPEC.md section 4: no future slot in the horizon is a failure, even when it is returned."""
    past = PriceForecast(
        slots=[PriceSlot(t=NOW - timedelta(hours=1), buy=0.1, sell=0.05, source="example_past")],
        generated_at=NOW,
        source="example_past",
    )
    empty = PriceForecast(slots=[], generated_at=NOW, source="example_empty")
    good = PriceForecast(
        slots=[PriceSlot(t=NOW, buy=0.2, sell=0.1, source="example_next")],
        generated_at=NOW,
        source="example_next",
    )

    def good_provider(client, settings, now=None, http=None, tz=None):
        return good

    for dead in (empty, past):
        forecast, statuses = load_price_forecast(
            settings(),
            None,
            now=NOW,
            providers={
                "zonneplan_entity": lambda *args, _f=dead, **kwargs: _f,
                "energy_charts": good_provider,
                "static": fetch_static,
            },
        )
        assert forecast.source == "example_next"  # never stops on the empty/all-past forecast
        assert forecast.degraded is True
        note = " ".join(forecast.notes)
        assert "fell back" in note and "zonneplan_entity" in note
        assert "no future price slots" in note
        by_name = {status.name: status for status in statuses}
        assert by_name["zonneplan_entity"].ok is False
        assert "no future price slots" in by_name["zonneplan_entity"].last_error
        assert by_name["energy_charts"].ok is True and by_name["energy_charts"].active is False


def test_chain_raises_when_every_source_returns_no_usable_slots():
    def empty(client, settings, now=None, http=None, tz=None):
        return PriceForecast(slots=[], generated_at=now, source="example_empty")

    with pytest.raises(PriceUnavailable) as err:
        load_price_forecast(
            settings(),
            None,
            now=NOW,
            providers={"zonneplan_entity": empty, "energy_charts": empty, "static": empty},
        )
    message = str(err.value)
    assert "no future price slots" in message
    assert "zonneplan_entity" in message and "energy_charts" in message and "static" in message
