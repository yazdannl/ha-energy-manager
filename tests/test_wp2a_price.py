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
from app.sources import ProviderError, RateLimited  # noqa: E402
from app.sources.price import (  # noqa: E402
    PROVIDERS,
    PriceUnavailable,
    ZONNEPLAN_SCALE_DIVISOR,
    _locate_zonneplan_entity,
    clear_zonneplan_cache,
    effective_price_source,
    fetch_energy_charts,
    fetch_entity_forecast,
    fetch_fixed_tariff,
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
    assert set(PROVIDERS) == {
        "zonneplan_entity",
        "energy_charts",
        "entity_forecast",
        "static",
        "fixed_tariff",
    }


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


def test_derived_feed_in_is_the_buy_price_minus_the_feed_in_cost():
    configured = settings(**{"price.export_fee_eur_per_kwh": 0.005})
    forecast, statuses = load_price_forecast(
        configured,
        None,
        now=NOW,
        providers={"zonneplan_entity": slot_provider(), "static": slot_provider()},
    )
    # SPEC 4 (amended): with no explicit tariff or entity the feed-in price is the consumer price
    # you pay minus the feed-in cost; the energy tax is paid back by default.
    assert all(slot.sell == pytest.approx(slot.buy - 0.005) for slot in forecast.slots)
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is True
    assert any("derived feed-in price" in note for note in forecast.notes)


def test_derived_feed_in_subtracts_the_slot_energy_tax_when_it_is_not_refunded():
    def taxed_provider(buy=0.25, tax=0.11):
        def provider(client, settings, now=None, http=None, tz=None):
            start = (now or NOW).replace(minute=0, second=0, microsecond=0)
            return PriceForecast(
                slots=[PriceSlot(t=start, buy=buy, sell=0.0, energy_tax=tax)],
                generated_at=now or NOW,
                source="taxed",
            )

        return provider

    configured = settings(**{"price.feed_in_energy_tax_refund": False})
    forecast, statuses = load_price_forecast(
        configured, None, now=NOW, providers={"zonneplan_entity": taxed_provider()}
    )
    assert forecast.slots[0].sell == pytest.approx(0.25 - 0.11)
    assert any("minus the slot energy tax" in note for note in forecast.notes)
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is True


def test_zonneplan_payload_gives_the_per_slot_energy_tax_component():
    entries = zonneplan_entries(NOW - timedelta(minutes=15), 8)
    client = FakeClient(sensor_state(entries, state="0.1"))
    configured = settings(**{"price.entity": "sensor.example_price_now"})
    forecast = fetch_zonneplan_entity(client, configured, now=NOW)
    # entry at NOW is index 1: included 1_010_000/1e7 = 0.101, excluded 505_000/1e7 = 0.0505
    assert forecast.slots[0].energy_tax == pytest.approx(0.0505)
    assert forecast.slots[0].buy == pytest.approx(0.101)
    assert forecast.slots[0].sell == 0.0  # fetch_* leaves sell to _apply_feed_in


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
        configured, BrokenClient(), now=NOW, providers={"zonneplan_entity": slot_provider()}
    )
    assert forecast.slots and forecast.slots[0].buy == pytest.approx(0.2)
    # The broken feed-in sensor degrades to the derived price instead of failing the forecast.
    assert forecast.slots[0].sell == pytest.approx(0.2)
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].detail["sell_known"] is True
    assert any("configured feed-in source unavailable" in note for note in forecast.notes)
    # Provenance: a configured source that failed must never read as an explicit tariff.
    assert forecast.feed_in_source == "unavailable"


def test_feed_in_provenance_names_the_path_that_resolved():
    derived, _ = load_price_forecast(
        settings(), None, now=NOW, providers={"zonneplan_entity": slot_provider()}
    )
    assert derived.feed_in_source == "derived"

    flat, _ = load_price_forecast(
        settings(**{"price.feed_in_eur_per_kwh": 0.09}),
        None,
        now=NOW,
        providers={"zonneplan_entity": slot_provider()},
    )
    assert flat.feed_in_source == "flat" and flat.slots[0].sell == pytest.approx(0.09)

    client = FakeClient(
        {
            "state": "0.08",
            "attributes": {"unit_of_measurement": "€/kWh", "friendly_name": "Example feed-in"},
        }
    )
    entity, _ = load_price_forecast(
        settings(**{"price.feed_in_entity": "sensor.example_feed_in"}),
        client,
        now=NOW,
        providers={"zonneplan_entity": slot_provider()},
    )
    assert entity.feed_in_source == "entity" and entity.slots[0].sell == pytest.approx(0.08)


def test_default_allow_export_is_off_until_a_feed_in_is_confirmed():
    assert DEFAULTS["optimizer.allow_export"] is False
    assert DEFAULTS["price.feed_in_eur_per_kwh"] is None
    assert DEFAULTS["price.feed_in_entity"] == "" and DEFAULTS["price.feed_in_tax_inclusive"] is True
    assert DEFAULTS["price.feed_in_energy_tax_refund"] is True
    assert DEFAULTS["price.export_fee_eur_per_kwh"] == 0.0


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
    # SPEC 4: sell is EUR/kWh *received*; the provider itself never guesses one (the load path
    # fills it from the feed-in price)
    assert forecast.slots[0].sell == 0.0
    assert forecast.slots[0].stale is False
    # ~24 h of real coverage < the 30 h horizon: the tail is stale/degraded, not a failure.
    assert forecast.slots[-1].stale is True
    assert forecast.degraded is True
    assert any("coverage ends" in note for note in forecast.notes)
    last_amount = 1_000_000 + (4 + 24 * 4 - 1) * 10_000
    assert forecast.slots[-1].buy == pytest.approx(last_amount / 1e7 + 0.01)


def test_the_price_grid_starts_at_the_plan_slot_so_the_current_slot_is_never_stale():
    """SPEC 4 + 9.1: the price grid must cover the plan slot the dispatcher is acting on.

    An hourly feed on a 60-minute plan grid is the shipped default. If the grid started at the
    current quarter instead (16:45 for 16:50), the plan's first slot (16:00) would be filled from a
    later slot and flagged stale - and the dispatcher refuses to execute a stale current slot, so
    the battery would sit idle for most of every hour. The grid therefore starts where the plan
    grid starts, and the current slot matches a real provider entry.
    """
    now = NOW + timedelta(minutes=50)  # not on a slot boundary
    start = now.replace(minute=0, second=0, microsecond=0)
    entries = [
        {"datetime": (start + timedelta(hours=index)).isoformat(), "price": 0.20 + 0.01 * index}
        for index in range(48)
    ]
    hourly = settings(**{"price.entity": "sensor.example_price_now", "optimizer.slot_minutes": 60})
    forecast = fetch_entity_forecast(FakeClient(sensor_state(entries)), hourly, now=now)
    assert forecast.slots[0].t == start, "the price grid must start at the current plan slot"
    assert forecast.slots[0].stale is False
    assert forecast.slots[0].buy == pytest.approx(0.20)

    from app.models import LoadForecast, LoadSlot, SolarForecast, SolarSlot
    from app.optimize.dp import align_slots

    solar = SolarForecast(
        source="capacity",
        generated_at=now,
        slots=[SolarSlot(t=start + timedelta(hours=i), pv_kw=1.0) for i in range(31)],
    )
    load = LoadForecast(
        source="statistics",
        generated_at=now,
        slots=[LoadSlot(t=start + timedelta(hours=i), load_kw=0.5) for i in range(31)],
    )
    slots = align_slots(forecast, solar, load, start, start + timedelta(hours=30), 60)
    current = next(slot for slot in slots if slot.t <= now < slot.t + timedelta(hours=1))
    assert current.t == start
    assert current.price_stale is False, "the dispatcher refuses a stale current slot"
    assert not any(slot.price_stale for slot in slots)

    # A quarter-hourly plan grid still starts at the current quarter: unchanged 15-minute behaviour.
    quarter = settings(**{"price.entity": "sensor.example_price_now", "optimizer.slot_minutes": 15})
    fifteen = fetch_entity_forecast(FakeClient(sensor_state(entries)), quarter, now=now)
    assert fifteen.slots[0].t == now.replace(minute=45, second=0, microsecond=0)


def test_a_fully_covered_forecast_is_not_reported_as_a_short_tail():
    """SPEC 4: the grid ends one step before the horizon, which is not an uncovered tail.

    ``slot_range`` excludes its end, so the last slot of a fully covered forecast always *starts*
    before ``horizon_end``. Comparing that start with the horizon would report a phantom uncovered
    tail - and a note claiming stale slots that are fresh - on every complete forecast.
    """
    hours = DEFAULTS["optimizer.horizon_hours"]
    start = NOW.replace(minute=0, second=0, microsecond=0)
    entries = [
        {"datetime": (start + timedelta(minutes=15 * index)).isoformat(), "price": 0.20}
        for index in range(hours * 4 + 4)
    ]
    configured = settings(**{"price.entity": "sensor.example_price_now", "optimizer.slot_minutes": 60})
    forecast = fetch_entity_forecast(FakeClient(sensor_state(entries)), configured, now=NOW)
    assert forecast.slots[-1].stale is False, "the horizon is covered to the end"
    assert forecast.degraded is False
    assert not any("coverage ends" in note for note in forecast.notes)

    short = fetch_entity_forecast(FakeClient(sensor_state(entries[: 12 * 4])), configured, now=NOW)
    assert short.slots[-1].stale is True
    assert short.degraded is True
    assert any("coverage ends" in note for note in short.notes)


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
    # The window is today + tomorrow (0.33.0): the default window answers for today only, and a
    # 30 h plan horizon reaches past midnight.
    assert calls == [
        "https://api.energy-charts.info/price?bzn=NL&start=2026-01-05&end=2026-01-06"
    ]
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


def test_energy_charts_falls_back_to_the_today_only_window_before_the_auction():
    seconds = [int((NOW + timedelta(minutes=15 * index)).timestamp()) for index in range(8)]
    payload = {"unit": "EUR / MWh", "unix_seconds": seconds, "price": [100.0] * 8}
    calls: list[str] = []

    def fake_http(url, **kwargs):
        calls.append(url)
        if "start=" in url:
            # Tomorrow's auction is not published yet: the API answers 404.
            raise ProviderError("provider request failed with HTTP 404", status=404)
        return payload

    forecast = fetch_energy_charts(None, settings(), now=NOW, http=fake_http)
    assert calls == [
        "https://api.energy-charts.info/price?bzn=NL&start=2026-01-05&end=2026-01-06",
        "https://api.energy-charts.info/price?bzn=NL",
    ]
    assert forecast.slots and forecast.source == "energy_charts"
    # The uncovered tail is still degraded and stale, never a provider failure.
    assert forecast.slots[-1].stale is True
    assert forecast.degraded is True


def test_energy_charts_invalid_zone_is_explicit():
    def fake_http(url, **kwargs):
        raise ProviderError("provider request failed with HTTP 400", status=400)

    with pytest.raises(ProviderError) as err:
        fetch_energy_charts(None, settings(**{"price.bzn": "ZZ"}), now=NOW, http=fake_http)
    assert "bidding zone" in str(err.value) and err.value.status == 400


def test_energy_charts_falls_back_when_the_wider_window_breaks():
    # 0.33.1: only a rejected zone and a rate limit justify giving up on the provider. Any other
    # failure of the today+tomorrow window (a timeout, a 5xx, an unexpected body) degrades to the
    # today-only window instead of emptying the price chart -- live 2026-10-21, when the extended
    # window failed inside the add-on and the whole 30 h forecast disappeared without a log line.
    seconds = [int((NOW + timedelta(minutes=15 * index)).timestamp()) for index in range(8)]
    payload = {"unit": "EUR / MWh", "unix_seconds": seconds, "price": [100.0] * 8}
    calls: list[str] = []

    def fake_http(url, **kwargs):
        calls.append(url)
        if "start=" in url:
            raise ProviderError("provider request failed: URLError")
        return payload

    forecast = fetch_energy_charts(None, settings(), now=NOW, http=fake_http)
    assert len(calls) == 2
    assert forecast.slots and forecast.source == "energy_charts"


def test_energy_charts_does_not_retry_a_rate_limited_window():
    # A 429 is about the caller, not the window: the today-only call would be refused too, and a
    # second request only extends the block.
    calls: list[str] = []

    def fake_http(url, **kwargs):
        calls.append(url)
        raise RateLimited("provider rate limited (HTTP 429)", retry_after_s=30.0)

    with pytest.raises(RateLimited):
        fetch_energy_charts(None, settings(), now=NOW, http=fake_http)
    assert len(calls) == 1


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


# ----------------------------------------------------------------------------------------------
# SPEC 4.1: provider + contract selection (0.3.0). Zonneplan structural discovery, the fixed
# tariff, the day_ahead wholesale note, and the located-entity cache.
# ----------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_zonneplan_entity_cache():
    # The located-entity cache is module-level; keep tests independent.
    clear_zonneplan_cache()
    yield
    clear_zonneplan_cache()


class StateClient:
    """Fake HA client with both get_states() (discovery) and get_state(id) (the located entity)."""

    def __init__(self, states):
        self.states = states

    def get_states(self):
        return [dict(state) for state in self.states]

    def get_state(self, entity_id):
        for state in self.states:
            if state.get("entity_id") == entity_id:
                return dict(state)
        return None


def zonneplan_state(entity_id, *, minutes, count, state_price=0.15, shape="quarter", start=None, unit="€/kWh"):
    """A believable Zonneplan electricity tariff state on a regular `minutes` grid.

    `shape="quarter"` is the 15-minute list (`price_tax_included{amount}` dicts); `shape="hourly"` is
    the legacy hourly list (`electricity_price` plain ints + a `datetime` field). The state equals the
    current slot EUR/kWh so the scale check passes (both shapes, €/kWh divided by 1e7).
    """
    amount = int(round(state_price * 1e7))
    origin = start if start is not None else (NOW - timedelta(hours=1))
    entries = []
    for index in range(count):
        moment = origin + timedelta(minutes=minutes * index)
        if shape == "quarter":
            entries.append(
                {
                    "start_date": moment.isoformat(),
                    "end_date": (moment + timedelta(minutes=minutes)).isoformat(),
                    "price_tax_included": {"amount": amount},
                    "price_tax_excluded": {"amount": amount // 2},
                    "sustainability_score": 100,
                }
            )
        else:
            entries.append(
                {
                    "start_date": moment.isoformat(),
                    "datetime": moment.isoformat(),
                    "electricity_price": amount,
                    "electricity_price_excl_tax": amount // 2,
                    "sustainability_score": 100,
                    "tariff_group": "normal",
                }
            )
    return {"entity_id": entity_id, "state": f"{amount / 1e7:.6f}", "attributes": {"unit_of_measurement": unit, "forecast": entries}}


def zonneplan_hourly_entries(start: datetime, count: int, amount: int = 1_000_000, step: int = 10_000):
    """Legacy hourly Zonneplan entries: plain-int `electricity_price`, 60-minute cadence."""
    return [
        {
            "start_date": (start + timedelta(hours=index)).isoformat(),
            "datetime": (start + timedelta(hours=index)).isoformat(),
            "electricity_price": amount + index * step,
            "electricity_price_excl_tax": (amount + index * step) // 2,
            "sustainability_score": 100,
            "tariff_group": "normal",
        }
        for index in range(count)
    ]


def gas_tariff_state():
    """The gas trap: same price_tax_included fixed-point shape, EUR/m³ unit, ONE 24-hour slot.

    Its own state agrees with its single slot, so the scale check alone does NOT reject it. Only the
    EUR/kWh unit requirement and the >=2-entry regular-grid requirement do.
    """
    amount = int(round(0.9 * 1e7))  # EUR/m3
    return {
        "entity_id": "sensor.example_gas_tariff",
        "state": f"{amount / 1e7:.6f}",
        "attributes": {
            "unit_of_measurement": "€/m³",
            "forecast": [
                {
                    "start_date": NOW.isoformat(),
                    "end_date": (NOW + timedelta(hours=24)).isoformat(),
                    "price_tax_included": {"amount": amount},
                }
            ],
        },
    }


def _zonneplan_settings(contract):
    return settings(**{"price.provider": "zonneplan", "price.contract": contract})


def test_effective_price_source_maps_provider_and_contract():
    assert effective_price_source(settings(**{"price.provider": "entity"})) == "zonneplan_entity"  # price.source default
    assert effective_price_source(settings(**{"price.provider": "entity", "price.source": "static"})) == "static"
    assert effective_price_source(settings(**{"price.provider": "zonneplan"})) == "zonneplan_entity"
    assert effective_price_source(settings(**{"price.provider": "day_ahead"})) == "energy_charts"
    assert effective_price_source(settings(**{"price.provider": "fixed"})) == "fixed_tariff"


def test_zonneplan_grid_preference_matches_the_contract():
    quarter = zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=8)
    hourly = zonneplan_state("sensor.zonneplan_example_hour", minutes=60, count=8, shape="hourly")
    client = StateClient([quarter, hourly])
    quarter_id, quarter_note = _locate_zonneplan_entity(client, _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc, "dynamic_quarter_hourly")
    assert quarter_id == "sensor.zonneplan_example_quarter" and quarter_note == ""
    hourly_id, hourly_note = _locate_zonneplan_entity(client, _zonneplan_settings("dynamic_hourly"), NOW, timezone.utc, "dynamic_hourly")
    assert hourly_id == "sensor.zonneplan_example_hour" and hourly_note == ""


def test_zonneplan_falls_back_to_the_other_grid_with_a_note():
    # Only the hourly grid exists; a quarter-hourly contract must use it AND note it (never silent).
    hourly = zonneplan_state("sensor.zonneplan_example_hour", minutes=60, count=8, shape="hourly")
    entity_id, note = _locate_zonneplan_entity(StateClient([hourly]), _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc, "dynamic_quarter_hourly")
    assert entity_id == "sensor.zonneplan_example_hour"
    assert "hourly" in note.lower() and "15-minute" in note.lower()
    # Symmetric: only 15-minute exists, an hourly contract uses it, with a note naming the 15-minute grid.
    quarter = zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=8)
    entity2, note2 = _locate_zonneplan_entity(StateClient([quarter]), _zonneplan_settings("dynamic_hourly"), NOW, timezone.utc, "dynamic_hourly")
    assert entity2 == "sensor.zonneplan_example_quarter"
    assert "15-minute" in note2.lower() and "hourly" in note2.lower()


def test_zonneplan_no_candidate_is_a_clear_error():
    client = StateClient([{"entity_id": "sensor.example_temperature", "state": "20", "attributes": {"unit_of_measurement": "°C"}}])
    with pytest.raises(ProviderError) as err:
        _locate_zonneplan_entity(client, _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc, "dynamic_quarter_hourly")
    assert "no compatible electricity tariff sensor found" in str(err.value)


def test_the_gas_tariff_is_never_chosen_as_an_electricity_price():
    settings_zon = _zonneplan_settings("dynamic_quarter_hourly")
    # Gas alone: nothing qualifies (EUR/m3 unit + a single 24h slot).
    with pytest.raises(ProviderError):
        _locate_zonneplan_entity(StateClient([gas_tariff_state()]), settings_zon, NOW, timezone.utc, "dynamic_quarter_hourly")
    # Gas alongside a real electricity tariff: the electricity one wins, never the gas sensor.
    electricity = zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=8)
    entity_id, _note = _locate_zonneplan_entity(StateClient([gas_tariff_state(), electricity]), settings_zon, NOW, timezone.utc, "dynamic_quarter_hourly")
    assert entity_id == "sensor.zonneplan_example_quarter"


def test_a_non_kwh_price_list_is_rejected_even_on_a_regular_grid():
    """The unit filter must stand on its own.

    The gas sensor in ``gas_tariff_state`` is rejected by *either* filter, so removing one of them
    still fails the gas test -- that redundancy hid the fact that neither filter was pinned. This
    candidate isolates the unit rule: a coherent state, a regular 15-minute, multi-entry grid, and
    only the declared unit marks it as something other than electricity (EUR/m3, EUR/MWh, ct/m3).
    """
    for unit in ("€/m³", "EUR/MWh", "ct/m³"):
        decoy = zonneplan_state("sensor.example_not_electricity", minutes=15, count=8, unit=unit)
        with pytest.raises(ProviderError):
            _locate_zonneplan_entity(
                StateClient([decoy]), _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc,
                "dynamic_quarter_hourly",
            )


def test_a_single_daily_kwh_slot_is_rejected_by_the_grid_filter():
    """The grid filter must stand on its own: a kWh unit alone does not make a price series.

    One entry spanning 24 hours is self-consistent (the state matches its only slot, so the scale
    check passes) and is exactly the shape that must never be planned against -- the unit rule
    cannot reject it.
    """
    amount = int(round(0.15 * 1e7))
    daily = {
        "entity_id": "sensor.example_daily_kwh_tariff",
        "state": f"{amount / 1e7:.6f}",
        "attributes": {
            "unit_of_measurement": "€/kWh",
            "forecast": [
                {
                    "start_date": (NOW - timedelta(minutes=60)).isoformat(),
                    "end_date": (NOW + timedelta(hours=23)).isoformat(),
                    "price_tax_included": {"amount": amount},
                }
            ],
        },
    }
    with pytest.raises(ProviderError):
        _locate_zonneplan_entity(
            StateClient([daily]), _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc,
            "dynamic_quarter_hourly",
        )


def test_ct_per_kwh_sensor_is_accepted_and_converts_to_eur_per_kwh():
    # ct/kWh is in the EUR/kWh family; its declared unit converts the state and forecast to EUR/kWh.
    client = StateClient([zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=8, state_price=15.0, unit="ct/kWh")])
    entity_id, _note = _locate_zonneplan_entity(client, _zonneplan_settings("dynamic_quarter_hourly"), NOW, timezone.utc, "dynamic_quarter_hourly")
    assert entity_id == "sensor.zonneplan_example_quarter"
    forecast = fetch_zonneplan_entity(client, settings(**{"price.provider": "zonneplan", "price.entity": "sensor.zonneplan_example_quarter"}), now=NOW)
    assert forecast.slots[0].buy == pytest.approx(0.15)  # 15 ct/kWh -> 0.15 EUR/kWh, no adders


def test_zonneplan_hourly_legacy_shape_parses_and_divides():
    entries = zonneplan_hourly_entries(NOW - timedelta(hours=1), 4 + 30)
    client = FakeClient(sensor_state(entries, state="0.1"))
    forecast = fetch_zonneplan_entity(client, settings(**{"price.entity": "sensor.example_price_now"}), now=NOW)
    assert forecast.source == "zonneplan_entity"
    # entry at NOW is index 1: (1_000_000 + 1*10_000) / 1e7 = 0.101 EUR/kWh; no adders, upsampled.
    assert forecast.slots[0].buy == pytest.approx(0.101)
    assert forecast.slots[0].stale is False
    assert forecast.slots[1].stale is True  # hourly upsampled onto the 15-minute grid


def test_zonneplan_provider_locates_and_parses_end_to_end():
    quarter = zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=4 + 30 * 4, state_price=0.15)
    client = StateClient([quarter])

    def charts_down(client, settings, now=None, http=None, tz=None):
        raise ProviderError("energy_charts: offline")

    forecast, statuses = load_price_forecast(
        _zonneplan_settings("dynamic_quarter_hourly"),
        client,
        now=NOW,
        providers={"zonneplan_entity": fetch_zonneplan_entity, "energy_charts": charts_down, "static": fetch_static},
    )
    assert forecast.source == "zonneplan_entity"
    assert forecast.slots[0].buy == pytest.approx(0.15)  # no VAT adders on a tax-included tariff
    by_name = {status.name: status for status in statuses}
    assert by_name["zonneplan_entity"].ok is True and by_name["zonneplan_entity"].active is True
    assert by_name["zonneplan_entity"].detail["provider"] == "zonneplan"


def test_day_ahead_names_the_adders_actually_applied():
    seconds = [int((NOW + timedelta(minutes=15 * index)).timestamp()) for index in range(8)]
    payload = {"unit": "EUR / MWh", "unix_seconds": seconds, "price": [100.0] * 8}
    forecast = fetch_energy_charts(
        None,
        settings(**{"price.provider": "day_ahead", "price.vat_pct": 21.0, "price.energy_tax_eur_per_kwh": 0.05, "price.import_fee_eur_per_kwh": 0.01}),
        now=NOW,
        http=lambda url, **kw: payload,
    )
    note = " ".join(forecast.notes)
    assert "wholesale, not a consumer price" in note
    assert "VAT 21%" in note and "energy tax 0.05" in note and "import fee 0.01" in note


def test_day_ahead_404_publishes_no_prices_for_the_zone():
    def http_404(url, **kwargs):
        raise ProviderError("provider request failed with HTTP 404", status=404)

    with pytest.raises(ProviderError) as err:
        fetch_energy_charts(None, settings(**{"price.provider": "day_ahead", "price.bzn": "NL"}), now=NOW, http=http_404)
    assert "publishes no" in str(err.value) and "NL" in str(err.value) and err.value.status == 404


def test_day_ahead_empty_payload_also_names_the_zone():
    empty = {"unit": "EUR / MWh", "unix_seconds": [], "price": []}
    with pytest.raises(ProviderError) as err:
        fetch_energy_charts(None, settings(**{"price.provider": "day_ahead", "price.bzn": "NL"}), now=NOW, http=lambda url, **kw: empty)
    assert "publishes no" in str(err.value) and "NL" in str(err.value)


def test_fixed_single_tariff_is_a_flat_consumer_price_no_adders():
    configured = settings(**{"price.provider": "fixed", "price.contract": "fixed_single", "price.fixed_price_eur_per_kwh": 0.25, "price.vat_pct": 21.0})
    forecast = fetch_fixed_tariff(None, configured, now=NOW)
    assert forecast.source == "fixed_tariff"
    assert forecast.slots and all(slot.buy == 0.25 for slot in forecast.slots)  # VAT never added
    assert forecast.stale is False


def test_fixed_single_entity_wins_over_the_typed_rate():
    client = FakeClient({"entity_id": "sensor.example_tariff", "state": "30", "attributes": {"unit_of_measurement": "ct/kWh"}})
    configured = settings(**{"price.provider": "fixed", "price.contract": "fixed_single", "price.fixed_price_eur_per_kwh": 0.9, "price.fixed_price_entity": "sensor.example_tariff"})
    forecast = fetch_fixed_tariff(client, configured, now=NOW)
    assert all(slot.buy == pytest.approx(0.30) for slot in forecast.slots)


def test_fixed_single_missing_price_fails_loudly_naming_the_setting():
    with pytest.raises(ProviderError) as err:
        fetch_fixed_tariff(None, settings(**{"price.provider": "fixed", "price.contract": "fixed_single"}), now=NOW)
    assert "price.fixed_price_eur_per_kwh" in str(err.value) and "price.fixed_price_entity" in str(err.value)


def _local_window_price(slot_t, zone, day_start, night_start, day_price, night_price):
    hour = slot_t.astimezone(zone).hour
    if day_start <= night_start:
        is_day = day_start <= hour < night_start
    else:
        is_day = hour >= day_start or hour < night_start
    return day_price if is_day else night_price


def test_fixed_day_night_window_crosses_midnight_in_local_time():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # 13:00 CET
    configured = settings(
        **{
            "price.provider": "fixed",
            "price.contract": "fixed_day_night",
            "price.fixed_day_price_eur_per_kwh": 0.30,
            "price.fixed_night_price_eur_per_kwh": 0.10,
            "price.fixed_day_start_hour": 6,
            "price.fixed_night_start_hour": 22,  # night 22:00 -> 06:00 wraps midnight
        }
    )
    forecast = fetch_fixed_tariff(None, configured, now=now, tz="Europe/Amsterdam")
    price_at = {slot.t: slot.buy for slot in forecast.slots}
    assert price_at[datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)] == 0.30  # 13:00 local -> day
    assert price_at[datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)] == 0.10  # 22:00 local -> night
    assert price_at[datetime(2026, 1, 6, 4, 0, tzinfo=timezone.utc)] == 0.10  # 05:00 local -> night (post-midnight)
    assert price_at[datetime(2026, 1, 6, 5, 0, tzinfo=timezone.utc)] == 0.30  # 06:00 local -> day


def test_fixed_day_night_is_dst_correct_per_local_slot():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)  # spring forward 02:00 -> 03:00 local
    configured = settings(
        **{
            "price.provider": "fixed",
            "price.contract": "fixed_day_night",
            "price.fixed_day_price_eur_per_kwh": 0.30,
            "price.fixed_night_price_eur_per_kwh": 0.10,
            "price.fixed_day_start_hour": 2,
            "price.fixed_night_start_hour": 4,
        }
    )
    forecast = fetch_fixed_tariff(None, configured, now=now, tz="Europe/Amsterdam")
    for slot in forecast.slots:
        assert slot.buy == _local_window_price(slot.t, zone, 2, 4, 0.30, 0.10)
    # On the spring-forward day the 02:00 local hour is skipped (01:00 -> 03:00); a later day has a
    # normal 02:00, so the check is scoped to the transition day.
    transition = {slot.t.astimezone(zone).hour for slot in forecast.slots if slot.t.astimezone(zone).day == 29}
    assert 2 not in transition
    assert {1, 3} <= transition


def test_fixed_provider_never_falls_back_to_the_market():
    def charts(client, settings, now=None, http=None, tz=None):
        return PriceForecast(slots=[PriceSlot(t=NOW, buy=0.2, sell=0.1, source="energy_charts")], generated_at=NOW, source="energy_charts")

    # A missing fixed price must raise PriceUnavailable, never silently use market prices.
    with pytest.raises(PriceUnavailable):
        load_price_forecast(
            settings(**{"price.provider": "fixed", "price.contract": "fixed_single"}),
            None,
            now=NOW,
            providers={"fixed_tariff": fetch_fixed_tariff, "energy_charts": charts, "static": fetch_static},
        )


def test_fixed_provider_resolves_through_the_loader():
    forecast, statuses = load_price_forecast(
        settings(**{"price.provider": "fixed", "price.contract": "fixed_single", "price.fixed_price_eur_per_kwh": 0.25}),
        None,
        now=NOW,
        providers={"fixed_tariff": fetch_fixed_tariff},
    )
    assert forecast.source == "fixed_tariff"
    by_name = {status.name: status for status in statuses}
    assert by_name["fixed_tariff"].ok is True and by_name["fixed_tariff"].active is True
    assert by_name["fixed_tariff"].detail["provider"] == "fixed"


def test_the_located_entity_is_cached_between_replans():
    client = StateClient([zonneplan_state("sensor.zonneplan_example_quarter", minutes=15, count=8)])

    class CountingClient(StateClient):
        def __init__(self, states):
            super().__init__(states)
            self.calls = 0

        def get_states(self):
            self.calls += 1
            return super().get_states()

    counting = CountingClient(client.states)
    configured = _zonneplan_settings("dynamic_quarter_hourly")
    first = _locate_zonneplan_entity(counting, configured, NOW, timezone.utc, "dynamic_quarter_hourly")
    second = _locate_zonneplan_entity(counting, configured, NOW, timezone.utc, "dynamic_quarter_hourly")
    assert first == second and counting.calls == 1  # second call served from cache
