"""Price forecast providers (SPEC.md section 4).

One registry :data:`PROVIDERS` maps a source name to a small fetch function. Every provider returns
a normalised :class:`~app.models.PriceForecast`: 15-minute, gap-free slots from now to
``optimizer.horizon_hours``, EUR/kWh tax included on the buy side and EUR/kWh received on the sell
side. Fixed-point providers declare their unit (or the user sets ``price.unit``/``price.scale_divisor``
via :func:`app.sources.unit_and_divisor`); magnitudes are never guessed.

:func:`load_price_forecast` implements the mandated fallback chain configured -> energy_charts ->
static and returns the :class:`ProviderStatus` list so the engine can raise the
``forecast_unavailable`` event naming the failed source.
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..models import (
    PRICE_UNITS,
    ConfigError,
    PriceForecast,
    PriceSlot,
    ProviderStatus,
    eur_per_kwh_from_raw,
    from_iso,
    quarter_floor,
    slot_range,
    to_iso,
    utcnow,
)
from ..harest import HAError
from . import ProviderError, RateLimited, horizon_hours, http_get_json, unit_and_divisor

log = logging.getLogger(__name__)

__all__ = [
    "ENERGY_CHARTS_URL",
    "PROVIDERS",
    "SELL_UNKNOWN_NOTE",
    "PriceUnavailable",
    "fetch_energy_charts",
    "fetch_entity_forecast",
    "fetch_static",
    "fetch_zonneplan_entity",
    "load_price_forecast",
]

ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"
SLOT_MINUTES = 15
STALE_ATTRIBUTE_SECONDS = 3 * 3600

#: Attribute names that may hold a price list (checked in order).
FORECAST_ATTRIBUTES = ("forecast", "prices", "forecasts", "price_forecast", "hourly", "entries", "data")
#: Entry key names, checked in order (SPEC.md section 4, docs/EXTERNAL_APIS.md section 3).
TIME_KEYS = ("start_date", "start", "datetime", "time", "from", "timestamp", "valid_from", "starts_at", "start_time")
#: ``price_tax_included`` is the consumer price (VAT + energy tax included) and is fixed-point.
PRICE_KEYS = ("electricity_price", "price_tax_included", "price", "buy", "tariff", "value", "price_eur_per_kwh", "all_in", "total")
#: Set when no feed-in tariff is configured; the loader appends it to the forecast notes.
SELL_UNKNOWN_NOTE = (
    "sell/export price unknown: set price.feed_in_eur_per_kwh or price.feed_in_entity; "
    "slots report sell = 0.00 EUR/kWh received"
)
#: Zonneplan publishes fixed-point integers with ``value_factor = 1e-7`` (docs/EXTERNAL_APIS.md section 3).
ZONNEPLAN_SCALE_DIVISOR = 1e7
#: Overall staleness floor: less than this much fresh coverage means the payload is stale.
MIN_FRESH_COVERAGE_HOURS = 1


class PriceUnavailable(ProviderError):
    """Every provider in the fallback chain failed."""


# --------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------


def _zone(name: str | None):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except (ValueError, KeyError):
        log.warning("unknown timezone %r; using UTC", name)
        return timezone.utc


def _parse_time(value: Any, tz: Any) -> datetime:
    """Parse a provider timestamp; naive values are interpreted in ``tz``."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = from_iso(text)
        return parsed.astimezone(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a price")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    return float(text)


def _nested_number(value: Any) -> Any:
    """Unwrap ``{"all_in": x}``-style price values without guessing units."""
    if isinstance(value, Mapping):
        for key in ("all_in", "total", "value", "price", "amount"):
            if key in value:
                return value[key]
        raise ValueError("price object has no recognised value key")
    return value


def _entry_price(entry: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in entry and entry[key] is not None:
            return _nested_number(entry[key])
    raise ValueError(f"no price key among {list(keys)}")


def _entry_time(entry: Any, tz: Any) -> datetime:
    if isinstance(entry, Mapping):
        for key in TIME_KEYS:
            if key in entry and entry[key] is not None:
                return _parse_time(entry[key], tz)
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return _parse_time(entry[0], tz)
    raise ValueError(f"no time key among {list(TIME_KEYS)}")


def _horizon_hours(settings: Any) -> int:
    return horizon_hours(settings)


def _fees(settings: Any) -> tuple[float, float]:
    def number(key: str) -> float:
        try:
            return float(settings.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return number("price.import_fee_eur_per_kwh"), number("price.export_fee_eur_per_kwh")


def _normalise_pairs(
    pairs: Iterable[tuple[datetime, Any]],
    *,
    unit: str,
    divisor: float,
    settings: Any,
    apply_tax: bool,
) -> list[tuple[datetime, float]]:
    """Convert raw provider buy values to ``(t, buy EUR/kWh)`` with the import fee applied.

    Providers never supply a sell price: SPEC.md section 4 defines ``sell`` as EUR/kWh *received*,
    which is filled in once by :func:`_apply_feed_in` from the configured feed-in tariff.
    """
    if unit not in PRICE_UNITS or unit == "auto":
        raise ProviderError(f"provider declared an unusable unit: {unit!r}")
    vat = float(settings.get("price.vat_pct", 0.0) or 0.0) if apply_tax else 0.0
    tax = float(settings.get("price.energy_tax_eur_per_kwh", 0.0) or 0.0) if apply_tax else 0.0
    import_fee, _ = _fees(settings)
    out: list[tuple[datetime, float]] = []
    for moment, raw_buy in pairs:
        if raw_buy is None:
            continue
        buy = eur_per_kwh_from_raw(_to_float(raw_buy), unit, divisor, vat, tax) + import_fee
        out.append((moment, buy))
    return out


def _build_forecast(
    pairs: Iterable[tuple[datetime, float]],
    *,
    source: str,
    settings: Any,
    now: datetime,
    notes: Iterable[str] = (),
) -> PriceForecast:
    """Resample normalised buy prices onto the 15-minute grid with forward/backward fill.

    Slots start with ``sell = 0.0`` (received nothing); :func:`_apply_feed_in` fills the real
    feed-in tariff on the load path. Provider lists contain past slots
    (docs/EXTERNAL_APIS.md section 3); everything before the current quarter is dropped.
    """
    horizon = _horizon_hours(settings)
    start = quarter_floor(now, SLOT_MINUTES)
    end = start + timedelta(hours=horizon)
    grid = slot_range(start, end, SLOT_MINUTES)

    table: dict[datetime, float] = {}
    saw_entries = False
    for moment, buy in pairs:
        saw_entries = True
        slot = quarter_floor(moment, SLOT_MINUTES)
        if slot < start:
            continue
        table[slot] = buy
    if not table:
        if saw_entries:
            raise ProviderError(f"{source}: no future price slots (all entries are in the past)")
        raise ProviderError(f"{source}: no price entries")
    times = sorted(table)
    if not any(moment >= start for moment in times):
        raise ProviderError(f"{source}: no future price slots")

    slots: list[PriceSlot] = []
    for slot_time in grid:
        position = bisect.bisect_right(times, slot_time)
        if position and times[position - 1] == slot_time:
            buy = table[slot_time]
            stale = False
        elif position:
            buy = table[times[position - 1]]
            stale = True
        else:
            buy = table[times[0]]
            stale = True
        slots.append(
            PriceSlot(
                t=slot_time,
                buy=buy,
                sell=0.0,
                source=source,
                is_actual=slot_time <= now < slot_time + timedelta(minutes=SLOT_MINUTES),
                stale=stale,
            )
        )
    future = [slot for slot in slots if slot.t >= start]
    forecast = PriceForecast(
        slots=slots,
        generated_at=now,
        source=source,
        stale=bool(future) and all(slot.stale for slot in future),
        notes=list(notes),
    )
    return forecast


def _apply_coverage(
    forecast: PriceForecast,
    settings: Any,
    now: datetime,
    *,
    mark_overall_stale: bool,
) -> None:
    """Mark the uncovered tail stale/degraded; a coverage gap is not a provider failure.

    ``mark_overall_stale`` is True for HA consumer sensors (zonneplan/entity_forecast), where a
    payload with almost no forward coverage means the refresh failed even though the entity still
    looks available (docs/EXTERNAL_APIS.md section 7, items 3 and 12).
    """
    start = quarter_floor(now, SLOT_MINUTES)
    horizon_end = start + timedelta(hours=_horizon_hours(settings))
    future = [slot for slot in forecast.slots if slot.t >= start]
    fresh = [slot for slot in future if not slot.stale]
    if not fresh:
        forecast.stale = True
        forecast.degraded = True
        return
    coverage_end = max(slot.t for slot in fresh)
    for slot in future:
        if slot.t > coverage_end:
            slot.stale = True
    if coverage_end < horizon_end:
        forecast.degraded = True
        forecast.notes.append(
            f"coverage ends {to_iso(coverage_end)} before the requested horizon "
            f"{to_iso(horizon_end)}; uncovered slots are stale"
        )
    if mark_overall_stale and coverage_end < start + timedelta(hours=MIN_FRESH_COVERAGE_HOURS):
        forecast.stale = True
        forecast.notes.append("less than one hour of fresh price coverage; treating the list as stale")


def _apply_feed_in(
    forecast: PriceForecast, settings: Any, client: Any, tz: Any = None
) -> bool:
    """Fill ``slot.sell`` from the configured feed-in tariff (SPEC.md section 4).

    ``sell`` is EUR/kWh *received*, so it is never derived from the buy price. Resolution order:
    ``price.feed_in_entity`` (current HA sensor state, its declared unit) then a flat
    ``price.feed_in_eur_per_kwh``; both are net of ``price.export_fee_eur_per_kwh`` and get
    ``price.vat_pct`` added when ``price.feed_in_tax_inclusive`` is off. Returns False (and leaves
    every slot at sell = 0.00) when no tariff is configured or it cannot be read; failing to price
    export must never fail the buy forecast.
    """
    entity = str(settings.get("price.feed_in_entity", "") or "")
    tariff: float | None = None
    origin = ""
    try:
        if entity:
            if client is None:
                raise ProviderError("no Home Assistant client")
            state = client.get_state(entity)
            attributes = state.get("attributes") or {}
            declared = unit_and_divisor(attributes.get("unit_of_measurement"))
            if declared is None:
                raise ProviderError(f"entity {entity} declares no recognised price unit")
            unit, divisor = declared
            tariff = eur_per_kwh_from_raw(_to_float(state.get("state")), unit, divisor)
            origin = f"{entity} ({attributes.get('unit_of_measurement')})"
        elif settings.get("price.feed_in_eur_per_kwh", None) is not None:
            tariff = float(settings.get("price.feed_in_eur_per_kwh"))
            origin = "price.feed_in_eur_per_kwh"
    except Exception as err:  # noqa: BLE001 - a broken feed-in sensor must not break the buy price
        forecast.notes.append(
            f"sell price unavailable ({type(err).__name__}: {err}); slots report sell = 0.00 EUR/kWh"
        )
        return False
    if tariff is None:
        forecast.notes.append(SELL_UNKNOWN_NOTE)
        return False
    if not bool(settings.get("price.feed_in_tax_inclusive", True)):
        tariff *= 1.0 + float(settings.get("price.vat_pct", 0.0) or 0.0) / 100.0
    export_fee = _fees(settings)[1]
    net = tariff - export_fee
    for slot in forecast.slots:
        slot.sell = net
    forecast.notes.append(
        f"sell price {net:.4f} EUR/kWh received ({origin}; export fee {export_fee:.4f} EUR/kWh)"
    )
    return True


def _declared_unit(
    attributes: Mapping[str, Any], settings: Any, source: str, default_divisor: float = 1.0
) -> tuple[str, float, list[str]]:
    """Resolve the declared unit and scale divisor; raises instead of guessing (SPEC.md section 4)."""
    notes: list[str] = []
    configured = str(settings.get("price.unit", "auto") or "auto")
    scale = float(settings.get("price.scale_divisor", 1.0) or 1.0) * float(default_divisor)
    declared = unit_and_divisor(attributes.get("unit_of_measurement"))
    if configured != "auto":
        if configured not in PRICE_UNITS:
            raise ProviderError(f"{source}: price.unit is not a known unit: {configured!r}")
        notes.append(f"unit forced to {configured} (divisor {scale:g})")
        return configured, scale, notes
    if declared is None:
        raise ProviderError(
            f"{source}: sensor declares no recognised unit; set price.unit and price.scale_divisor"
        )
    unit, divisor = declared
    divisor *= scale
    notes.append(f"unit {attributes.get('unit_of_measurement')} -> {unit} (divisor {divisor:g})")
    return unit, divisor, notes


def _first_forecast_list(attributes: Mapping[str, Any]) -> list[Any] | None:
    for key in FORECAST_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, list) and value:
            return value
    return None


def _attribute_age_seconds(attributes: Mapping[str, Any], now: datetime) -> float | None:
    for key in ("forecast_updated", "last_update", "updated_at", "last_changed"):
        value = attributes.get(key)
        if not value:
            continue
        try:
            return max(0.0, (now - _parse_time(value, timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            continue
    return None


def _entry_window(entry: Any, tz: Any, next_moment: datetime | None) -> tuple[datetime, datetime] | None:
    """Start/end of the slot an entry describes (docs/EXTERNAL_APIS.md section 3)."""
    try:
        start = _entry_time(entry, tz)
    except (ValueError, TypeError):
        return None
    end = None
    if isinstance(entry, Mapping):
        for key in ("end_date", "end", "end_time", "valid_until"):
            if entry.get(key):
                try:
                    end = _parse_time(entry[key], tz)
                    break
                except (ValueError, TypeError):
                    end = None
    if end is None:
        end = next_moment if next_moment is not None else start + timedelta(hours=1)
    if end <= start:
        end = start + timedelta(minutes=SLOT_MINUTES)
    return start, end


def _scale_check(
    source: str,
    state_value: Any,
    parsed: Sequence[tuple[datetime, Any, Any]],
    *,
    unit: str,
    divisor: float,
    tz: Any,
    now: datetime,
) -> None:
    """Compare the sensor state with the raw current slot (docs/EXTERNAL_APIS.md section 7, item 10/12).

    Zonneplan keeps serving its last payload when the upstream refresh fails, so a mismatch between
    the live state and the slot covering ``now`` means either a changed scale or a stale payload;
    both must disable the provider instead of corrupting every cost number. Fees are excluded from
    the comparison: the state is the sensor price, the fee is our own add-on markup.
    """
    try:
        state_price = float(state_value)
    except (TypeError, ValueError):
        return  # state unavailable: nothing to compare against
    current_raw = None
    for index, (moment, raw_buy, entry) in enumerate(parsed):
        next_moment = parsed[index + 1][0] if index + 1 < len(parsed) else None
        window = _entry_window(entry, tz, next_moment)
        start, end = window if window is not None else (moment, next_moment or moment)
        if start <= now < end:
            current_raw = raw_buy
            break
    if current_raw is None:
        raise ProviderError(f"{source}: no price slot covers now")
    expected = eur_per_kwh_from_raw(_to_float(current_raw), unit, divisor)
    tolerance = max(0.02, 0.25 * abs(state_price))
    if abs(expected - state_price) > tolerance:
        raise ProviderError(
            f"{source}: sensor state {state_price:g} does not match the current slot "
            f"({expected:.4f}); refusing to guess the scale or serve a stale payload"
        )


def _entity_provider(
    source: str,
    client: Any,
    settings: Any,
    now: datetime,
    tz: Any,
    *,
    default_divisor: float = 1.0,
    scale_check: bool = False,
) -> PriceForecast:
    entity_id = str(settings.get("price.entity", "") or "")
    if not entity_id:
        raise ProviderError(f"{source}: price.entity is not configured")
    if client is None:
        raise ProviderError(f"{source}: no Home Assistant client")
    state = client.get_state(entity_id)
    attributes = state.get("attributes") or {}
    entries = _first_forecast_list(attributes)
    if entries is None:
        raise ProviderError(
            f"{source}: sensor has no price list attribute (looked for {', '.join(FORECAST_ATTRIBUTES)})"
        )
    unit, divisor, notes = _declared_unit(attributes, settings, source, default_divisor)
    parsed: list[tuple[datetime, Any, Any]] = []
    for entry in entries:
        try:
            moment = _entry_time(entry, tz)
        except (ValueError, TypeError):
            continue  # drop entries with missing/None dates (docs/EXTERNAL_APIS.md item 11)
        try:
            buy = _entry_price(entry, PRICE_KEYS)
        except ValueError as err:
            raise ProviderError(f"{source}: {err}") from None
        parsed.append((moment, buy, entry))
    parsed.sort(key=lambda item: item[0])
    if scale_check:
        _scale_check(
            source,
            state.get("state"),
            parsed,
            unit=unit,
            divisor=divisor,
            tz=tz,
            now=now,
        )
    forecast = _build_forecast(
        _normalise_pairs(
            [(moment, buy) for moment, buy, _entry in parsed],
            unit=unit,
            divisor=divisor,
            settings=settings,
            apply_tax=False,
        ),
        source=source,
        settings=settings,
        now=now,
        notes=notes,
    )
    _apply_coverage(forecast, settings, now, mark_overall_stale=True)
    age = _attribute_age_seconds(attributes, now)
    if age is not None and age > STALE_ATTRIBUTE_SECONDS:
        forecast.notes.append(f"forecast attribute is {age / 3600:.1f}h old")
        forecast.stale = True
        for slot in forecast.slots:
            slot.stale = True
    return forecast


# --------------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------------


def fetch_zonneplan_entity(
    client: Any, settings: Any, now: datetime | None = None, http: Callable[..., Any] | None = None, tz: str | None = None
) -> PriceForecast:
    """Zonneplan One price sensor (docs/EXTERNAL_APIS.md section 3).

    Both forecast shapes publish fixed-point integers with ``value_factor = 1e-7`` and
    ``price_tax_included`` is already the consumer price incl. VAT and energy tax, so no adders are
    applied and the live sensor state is compared with the current slot to catch scale changes or a
    stale payload.
    """
    moment = now or utcnow()
    return _entity_provider(
        "zonneplan_entity",
        client,
        settings,
        moment,
        _zone(tz),
        default_divisor=ZONNEPLAN_SCALE_DIVISOR,
        scale_check=True,
    )


def fetch_entity_forecast(
    client: Any, settings: Any, now: datetime | None = None, http: Callable[..., Any] | None = None, tz: str | None = None
) -> PriceForecast:
    """Generic provider: any HA sensor whose attribute is a list of ``{start/datetime, price}``."""
    moment = now or utcnow()
    return _entity_provider("entity_forecast", client, settings, moment, _zone(tz))


def fetch_energy_charts(
    client: Any, settings: Any, now: datetime | None = None, http: Callable[..., Any] | None = None, tz: str | None = None
) -> PriceForecast:
    """Keyless day-ahead prices from energy-charts.info (EUR/MWh, ex-VAT, ex-tax).

    The legacy endpoint returns today only (96 quarter-hour slots); tomorrow 404s until the auction
    result is published, so the uncovered tail is marked stale/degraded rather than failing the
    provider (SPEC appendix A).
    """
    moment = now or utcnow()
    zone = str(settings.get("price.bzn", "NL") or "NL").strip()
    try:
        payload = (http or http_get_json)(f"{ENERGY_CHARTS_URL}?bzn={zone}")
    except ProviderError as err:
        if err.status == 400:
            raise ProviderError(
                f"energy_charts: bidding zone {zone!r} was rejected by the API", status=400
            ) from None
        raise
    if not isinstance(payload, Mapping):
        raise ProviderError("energy_charts: unexpected response shape")
    unit_text = payload.get("unit")
    declared = unit_and_divisor(unit_text)
    if declared is None:
        raise ProviderError(
            f"energy_charts: unrecognised unit {unit_text!r}; set price.unit/price.scale_divisor"
        )
    unit, divisor = declared
    seconds = payload.get("unix_seconds") or []
    prices = payload.get("price") or []
    pairs: list[tuple[datetime, Any]] = []
    for stamp, price in zip(seconds, prices):
        try:
            pairs.append((_parse_time(stamp, timezone.utc), price))
        except (ValueError, TypeError):
            continue
    notes = [
        f"unit {unit_text} -> {unit} (divisor {divisor:g})",
        "day-ahead spot; configured VAT and energy tax adders applied",
        "source: Energy-Charts.info (CC BY 4.0), data Bundesnetzagentur | SMARD.de",
    ]
    forecast = _build_forecast(
        _normalise_pairs(pairs, unit=unit, divisor=divisor, settings=settings, apply_tax=True),
        source="energy_charts",
        settings=settings,
        now=moment,
        notes=notes,
    )
    _apply_coverage(forecast, settings, moment, mark_overall_stale=False)
    return forecast


def fetch_static(
    client: Any, settings: Any, now: datetime | None = None, http: Callable[..., Any] | None = None, tz: str | None = None
) -> PriceForecast:
    """Last-resort provider: the configured ``price.static_schedule``."""
    moment = now or utcnow()
    schedule = settings.get("price.static_schedule") or []
    if not schedule:
        raise ProviderError("static: price.static_schedule is empty")
    zone = _zone(tz or settings.get("timezone"))
    horizon = _horizon_hours(settings)
    start = quarter_floor(moment, SLOT_MINUTES)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)
    pairs: list[tuple[datetime, Any]] = []
    for slot_time in grid:
        local = slot_time.astimezone(zone)
        price = _static_price(schedule, local.hour, local.weekday())
        if price is not None:
            pairs.append((slot_time, price))
    return _build_forecast(
        _normalise_pairs(pairs, unit="eur_per_kwh", divisor=1.0, settings=settings, apply_tax=False),
        source="static",
        settings=settings,
        now=moment,
        notes=["configured schedule; assumes the configured currency per kWh"],
    )


def _static_price(schedule: Iterable[Any], hour: int, weekday: int) -> float | None:
    for entry in schedule:
        if not isinstance(entry, Mapping):
            continue
        days = entry.get("days") or []
        if days:
            try:
                day_numbers = {int(day) for day in days}
            except (TypeError, ValueError):
                continue
            if weekday not in day_numbers:
                continue
        try:
            start = int(entry.get("from_hour", 0))
            end = int(entry.get("to_hour", 24))
            price = float(entry.get("price_eur_per_kwh"))
        except (TypeError, ValueError):
            continue
        if start <= end:
            inside = start <= hour < end
        else:  # overnight window, e.g. 23 -> 7
            inside = hour >= start or hour < end
        if inside:
            return price
    return None


#: Source registry (SPEC.md section 4). Signature: ``fetch(client, settings, now=, http=, tz=)``.
PROVIDERS: dict[str, Callable[..., PriceForecast]] = {
    "zonneplan_entity": fetch_zonneplan_entity,
    "energy_charts": fetch_energy_charts,
    "entity_forecast": fetch_entity_forecast,
    "static": fetch_static,
}

FALLBACK_ORDER = ("energy_charts", "static")


def _chain(configured: str) -> list[str]:
    chain = [configured]
    for fallback in FALLBACK_ORDER:
        if fallback not in chain:
            chain.append(fallback)
    return chain


def load_price_forecast(
    settings: Any,
    client: Any = None,
    *,
    now: datetime | None = None,
    http: Callable[..., Any] | None = None,
    tz: str | None = None,
    providers: Mapping[str, Callable[..., PriceForecast]] | None = None,
) -> tuple[PriceForecast, list[ProviderStatus]]:
    """Load a price forecast following the configured -> energy_charts -> static chain.

    Returns ``(forecast, statuses)``. Raises :class:`PriceUnavailable` when every provider failed;
    the caller is expected to raise the ``forecast_unavailable`` event with the failing names from
    the returned statuses (SPEC.md section 4). A provider failure — ``HAError`` (HA unreachable),
    ``ProviderError``/``RateLimited``, a timeout or a malformed payload — is recorded on that
    provider's status and the chain continues; a broken source never aborts the fallback. The same
    applies when a provider *returns* a forecast with no future slot in the requested horizon
    (empty, all past, or beyond the horizon): that is a failure, not an empty success.
    """
    moment = now or utcnow()
    registry = dict(providers or PROVIDERS)
    configured = str(settings.get("price.source", "zonneplan_entity") or "zonneplan_entity")
    if configured not in registry:
        raise ConfigError(f"unknown price source: {configured}")
    chain = _chain(configured)
    start = quarter_floor(moment, SLOT_MINUTES)
    end = start + timedelta(hours=horizon_hours(settings))
    statuses: dict[str, ProviderStatus] = {}
    for name in registry:
        statuses[name] = ProviderStatus(
            kind="price", name=name, active=name == configured, detail={"unit": str(settings.get("price.unit", "auto"))}
        )
    attempts: list[str] = []
    for position, name in enumerate(chain):
        func = registry.get(name)
        if func is None:
            continue
        attempts.append(name)
        status = statuses[name]
        try:
            forecast = func(client, settings, now=moment, http=http, tz=tz)
            if not any(start <= slot.t < end for slot in forecast.slots):
                # SPEC.md section 4: a forecast with no future slot in the requested horizon is a
                # provider failure, no matter how the provider signalled it (raised or returned).
                status.last_error = f"{name}: no future price slots in the requested horizon"
                continue
        except (RateLimited, ProviderError, HAError) as err:
            status.last_error = str(err)
            continue
        except Exception as err:  # noqa: BLE001 - a broken provider must never abort the chain
            status.last_error = f"{type(err).__name__}: {err}"
            continue
        status.ok = True
        status.last_success = moment
        status.stale = forecast.stale
        status.detail["slots"] = len(forecast.slots)
        # SPEC.md section 4: sell is what export actually pays, never the buy price. Apply the
        # configured feed-in tariff before the fallback note, so that note stays last.
        status.detail["sell_known"] = _apply_feed_in(forecast, settings, client, tz)
        if position > 0:
            forecast.degraded = True
            failed = [
                f"{item}: {statuses[item].last_error}" if statuses[item].last_error else item
                for item in attempts[:-1]
            ]
            forecast.notes.append("fell back to %s after: %s" % (name, "; ".join(failed)))
            status.detail["fell_back"] = True
        return forecast, [statuses[name] for name in registry]
    failures = "; ".join(f"{name}: {statuses[name].last_error}" for name in attempts)
    raise PriceUnavailable(f"price forecast unavailable ({failures})")
