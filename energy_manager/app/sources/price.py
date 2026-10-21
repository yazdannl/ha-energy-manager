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
import math
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence
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
    "PriceUnavailable",
    "clear_zonneplan_cache",
    "effective_price_source",
    "fetch_energy_charts",
    "fetch_entity_forecast",
    "fetch_fixed_tariff",
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
#: The two Zonneplan electricity shapes (docs/EXTERNAL_APIS.md section 3): the 15-minute list
#: carries ``price_tax_included``/``price_tax_excluded`` as ``{amount}`` dicts, the legacy hourly list
#: carries ``electricity_price``/``electricity_price_excl_tax`` as plain ints. Candidate detection
#: accepts only these, so a generic ``price`` key alone never masquerades as a Zonneplan tariff.
ZONNEPLAN_PRICE_KEYS = ("price_tax_included", "electricity_price")
#: Energy-tax-excluded counterparts in the same two shapes; their difference is the per-slot tax.
TAX_EXCLUDED_KEYS = ("electricity_price_excl_tax", "price_tax_excluded")
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


def plan_slot_minutes(settings: Any) -> int:
    """The plan/dispatch grid the optimiser will use (``optimizer.slot_minutes``, SPEC 3).

    Providers need this because the price grid has to cover every plan slot: the plan grid is
    floored to this step, so a price grid that started later (at the current quarter, say) would
    leave the plan's first slot forward/backward-filled and *stale*, and the dispatcher refuses to
    execute a stale current slot (SPEC 9.1, audit finding 2).
    """
    try:
        minutes = int(settings.get("optimizer.slot_minutes", 60) or 60)
    except (TypeError, ValueError):
        return 60
    return minutes if minutes > 0 else 60


def _grid_start(now: datetime, settings: Any) -> datetime:
    """Start of the plan slot containing ``now``: where the price grid must begin."""
    return quarter_floor(now, plan_slot_minutes(settings))


def _fees(settings: Any) -> tuple[float, float]:
    def number(key: str) -> float:
        try:
            return float(settings.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return number("price.import_fee_eur_per_kwh"), number("price.export_fee_eur_per_kwh")


def derived_sell_rule(settings: Any) -> Callable[[float, float], float]:
    """The derived feed-in price as a function of one slot's own buy price and energy tax.

    ``_build_forecast``'s derived branch applies exactly this rule to every slot it builds, so a
    caller that materialises *more* slots -- the stage-3 tail, whose published provider entries
    carry their own buy price -- prices them the same way instead of reusing another slot's sell
    price.  The rule takes the already-resolved tax *component* in EUR/kWh (``0.0`` when the source
    publishes none), because that is the quantity the fetch loop has.
    """
    export_fee = _fees(settings)[1]
    refund = bool(settings.get("price.feed_in_energy_tax_refund", True))

    def derive(buy: float, component: float) -> float:
        return float(buy) - export_fee - (0.0 if refund else float(component))

    return derive


#: ``forecast.feed_in_source`` values that mean the fetched window's ``sell`` prices are the
#: contract's own explicit rate (one resolved entity or flat tariff) rather than a per-slot
#: derivation from each slot's buy price.
EXPLICIT_FEED_IN_SOURCES = ("entity", "flat")

#: ... and the values that mean the window's slots carry the *derived* price: ``derived`` when no
#: explicit source is configured, ``unavailable`` when a configured one failed and the slots fell
#: back to the derived price.
DERIVED_FEED_IN_SOURCES = ("derived", "unavailable")


#: Tolerance for matching a window slot's ``sell`` against a pricing model, EUR/kWh.
_SELL_MODEL_EPS = 1e-9


class TailSellPricing(NamedTuple):
    """Which pricing model the fetched window's ``sell`` prices came from (see
    :func:`tail_sell_pricing`).

    ``rule`` is the derivation to reuse on the tail's published entries, or ``None`` to keep the
    window's own clock-time rate.  ``ambiguous`` is set when the window carries **no** provenance, so
    which supported model priced it is unidentified: both candidate readings of the tail are then
    computed and compared, and any disagreement refuses the vector.  ``unsupported`` is set when the
    window fits **neither** supported model: no reading of it can be justified and the vector must
    refuse.  ``evidence`` records what was checked (``rule`` is unused when ``unsupported``).
    """

    rule: Callable[[float, float], float] | None
    ambiguous: bool
    unsupported: bool
    evidence: dict[str, Any]


def _finite_float(value: Any) -> float | None:
    """A finite float, else ``None`` (never a fabricated zero)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _slot_evidence(index: int, slot: Any, sell: float | None, derived: float | None) -> dict[str, Any]:
    moment = getattr(slot, "t", None)
    return {
        "slot": index,
        "t": moment.isoformat() if hasattr(moment, "isoformat") else None,
        "buy": _finite_float(getattr(slot, "buy", None)),
        "sell_eur_per_kwh": sell,
        "derived_eur_per_kwh": derived,
    }


def _derived_value(derive: Callable[[float, float], float], slot: Any) -> float | None:
    """``derive(buy, tax)`` for one slot, exactly as the tail materialisation would compute it."""
    try:
        buy = float(getattr(slot, "buy", None))
        tax = float(getattr(slot, "energy_tax", 0.0) or 0.0)
        value = float(derive(buy, tax))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _window_sell_models(forecast: Any, derive: Callable[[float, float], float]) -> dict[str, Any]:
    """Which of the two supported models the window's own *priced* slots are consistent with.

    *derived*: every priced slot satisfies ``sell = buy - fee`` (minus the energy tax unless the
    tariff is tax-inclusive).  *flat*: every priced slot shares one finite rate.  An unpriced slot
    (``sell is None``, which means unknown and never a zero rate) carries no evidence either way; a
    non-finite one fits neither model.

    Returns the evidence for the decision: both verdicts, the first slot that breaks each model, the
    flat rate the window would have, and the first slot that fits *neither* model.
    """
    flat_rate: float | None = None
    derived_break: dict[str, Any] | None = None
    flat_break: dict[str, Any] | None = None
    unexplained: dict[str, Any] | None = None
    priced = 0
    for index, slot in enumerate(getattr(forecast, "slots", None) or ()):
        raw = getattr(slot, "sell", None)
        if raw is None:
            continue
        priced += 1
        sell = _finite_float(raw)
        derived = _derived_value(derive, slot) if sell is not None else None
        if sell is None:
            # A non-finite rate is garbage: it fits neither model, and *this* slot is the evidence for
            # each failure field that is still empty (never an earlier slot's evidence).
            broken = _slot_evidence(index, slot, None, None)
            if derived_break is None:
                derived_break = broken
            if flat_break is None:
                flat_break = broken
            if unexplained is None:
                unexplained = broken
            continue
        if flat_rate is None:
            flat_rate = sell
        flat_ok = abs(sell - flat_rate) <= _SELL_MODEL_EPS
        derived_ok = derived is not None and abs(sell - derived) <= _SELL_MODEL_EPS
        if not derived_ok and derived_break is None:
            derived_break = _slot_evidence(index, slot, sell, derived)
        if not flat_ok and flat_break is None:
            flat_break = _slot_evidence(index, slot, sell, None)
        if not derived_ok and not flat_ok and unexplained is None:
            unexplained = _slot_evidence(index, slot, sell, derived)
    return {
        "priced_slots": priced,
        "derived_fits": derived_break is None,
        "flat_fits": flat_break is None,
        "flat_rate_eur_per_kwh": flat_rate,
        "derived_first_break": derived_break,
        "flat_first_break": flat_break,
        "first_unexplained_slot": unexplained,
    }


def tail_sell_pricing(forecast: Any, settings: Any) -> TailSellPricing:
    """Which pricing model the **fetched window**'s ``sell`` prices came from, for the stage-3 tail.

    A published tail entry carries its own buy price, so its sell price must come from the same rule
    the window's slots went through, or the continuation row optimises against a different contract
    than the plan: on a derived contract ``sell = buy - fee`` follows *each slot's own* buy, while an
    explicit flat contract pays one rate that the tail keeps (the window slot at the same clock time
    carries it, which is what :func:`optimize.terminal.tail_slots` reuses when ``rule`` is ``None``).
    Returning the derived rule unconditionally would re-price the tail of an explicit contract;
    returning ``None`` unconditionally would do the reverse.

    Three models are supported, and the forecast's own provenance is authoritative when it is there:

    * ``feed_in_source`` in ``entity``/``flat`` -- one explicit rate really priced the window, so the
      tail keeps that rate (``rule is None``).
    * ``feed_in_source`` in ``derived``/``unavailable`` -- the slots carry the derived price
      (``unavailable`` *did* fall back to it), so each published tail entry derives from its own buy.
    * no provenance at all -- a caller-built or replayed forecast.  Exactly two things happen:

      1. the window must fit at least one supported model.  If it fits neither -- a variable explicit
         tariff, or garbage -- no reading of it can be justified, so the vector refuses
         (``unsupported``), and clock-time repetition does not resolve a variable explicit tariff;
      2. even when it fits one, the *window* fitting a model does not identify the contract: 0.16
         and 0.22 buys with 0.14 and 0.20 sells fit the derived rule exactly and are equally an
         entity publishing those two rates, so a later tail buy of 0.30 would be priced 0.28 by the
         derived reading and 0.14 by the explicit one.  The matrix therefore never picks a rule: the
         tail is priced under **both** candidate readings -- each published entry derived from its
         own buy, versus the clock-time reference rate the materialisation already uses -- and any
         disagreement refuses (``ambiguous``).  It proceeds only when the two agree everywhere,
         where the choice cannot matter.

    Those window sells are what the sweep prices the plan's own slots at either way, so reusing them
    is consistency with the plan's input, not trust: the settlement's separate rule -- never hand an
    unprovenanced window to the contract as an authoritative explicit tariff -- answers a different
    question.
    """
    derive = derived_sell_rule(settings)
    provenance = getattr(forecast, "feed_in_source", None)
    if provenance in EXPLICIT_FEED_IN_SOURCES:
        return TailSellPricing(None, False, False, {"provenance": provenance, "models": ["flat"]})
    if provenance in DERIVED_FEED_IN_SOURCES:
        return TailSellPricing(
            derive, False, False, {"provenance": provenance, "models": ["derived"]}
        )
    evidence = _window_sell_models(forecast, derive)
    if not (evidence["derived_fits"] or evidence["flat_fits"]):
        return TailSellPricing(derive, False, True, evidence)
    return TailSellPricing(derive, True, False, evidence)


def _normalise_pairs(
    pairs: Iterable[tuple[datetime, Any]],
    *,
    unit: str,
    divisor: float,
    settings: Any,
    apply_tax: bool,
    taxes: Mapping[datetime, float] | None = None,
) -> list[tuple[datetime, float, float]]:
    """Convert raw provider buy values to ``(t, buy EUR/kWh, energy_tax EUR/kWh)``.

    The consumer price carries VAT, the energy tax and the import fee. ``taxes`` may supply a
    per-slot energy-tax component for providers that publish a tax-excluded price next to the
    included one (Zonneplan); otherwise the component is the configured
    ``price.energy_tax_eur_per_kwh`` for wholesale sources (exactly the adder applied here) and 0.0
    for consumer sources, where the tax is already inside the published number. The derived feed-in
    price uses the component when the contract does not pay the energy tax back (SPEC.md section 4).
    """
    if unit not in PRICE_UNITS or unit == "auto":
        raise ProviderError(f"provider declared an unusable unit: {unit!r}")
    vat = float(settings.get("price.vat_pct", 0.0) or 0.0) if apply_tax else 0.0
    tax = float(settings.get("price.energy_tax_eur_per_kwh", 0.0) or 0.0) if apply_tax else 0.0
    import_fee, _ = _fees(settings)
    out: list[tuple[datetime, float, float]] = []
    for moment, raw_buy in pairs:
        if raw_buy is None:
            continue
        buy = eur_per_kwh_from_raw(_to_float(raw_buy), unit, divisor, vat, tax) + import_fee
        slot_tax = tax
        if taxes is not None and moment in taxes:
            slot_tax = float(taxes[moment])
        out.append((moment, buy, slot_tax))
    return out


def _entry_tax_component(
    entry: Mapping[str, Any], unit: str, divisor: float
) -> float | None:
    """Per-slot energy tax = (tax-included - tax-excluded price) in EUR/kWh, else None.

    Both Zonneplan shapes publish both sides (``price_tax_included``/``price_tax_excluded`` and
    ``electricity_price``/``electricity_price_excl_tax``); a generic consumer sensor publishes only
    one, and the derived feed-in price then falls back to ``price.energy_tax_eur_per_kwh``.
    """
    try:
        included = _entry_price(entry, ZONNEPLAN_PRICE_KEYS)
        excluded = _entry_price(entry, TAX_EXCLUDED_KEYS)
    except ValueError:
        return None
    try:
        # A negative component would mean the excluded price is above the included one (bad data);
        # clamping keeps it from *adding* to the sell price when the tax is not refunded.
        return max(
            0.0,
            eur_per_kwh_from_raw(_to_float(included), unit, divisor)
            - eur_per_kwh_from_raw(_to_float(excluded), unit, divisor),
        )
    except (TypeError, ValueError):
        return None


def _build_forecast(
    pairs: Iterable[tuple],
    *,
    source: str,
    settings: Any,
    now: datetime,
    notes: Iterable[str] = (),
) -> PriceForecast:
    """Resample normalised buy prices onto the 15-minute grid with forward/backward fill.

    Pairs are ``(t, buy)`` or ``(t, buy, energy_tax)``; slots start with ``sell = 0.0`` (received
    nothing) and :func:`_apply_feed_in` fills the feed-in price on the load path. The grid starts at
    the plan slot containing ``now`` (:func:`_grid_start`), so every plan slot - including the one
    the dispatcher acts on right now - has an exact price when the provider publishes the cadence
    the plan uses; provider entries older than that slot are dropped (docs/EXTERNAL_APIS.md 3).
    """
    horizon = _horizon_hours(settings)
    start = _grid_start(now, settings)
    end = start + timedelta(hours=horizon)
    grid = slot_range(start, end, SLOT_MINUTES)

    table: dict[datetime, tuple[float, float]] = {}
    saw_entries = False
    for pair in pairs:
        saw_entries = True
        moment, buy = pair[0], pair[1]
        slot_tax = float(pair[2]) if len(pair) > 2 else 0.0
        slot = quarter_floor(moment, SLOT_MINUTES)
        if slot < start:
            continue
        table[slot] = (buy, slot_tax)
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
            buy, slot_tax = table[slot_time]
            stale = False
        elif position:
            buy, slot_tax = table[times[position - 1]]
            stale = True
        else:
            buy, slot_tax = table[times[0]]
            stale = True
        slots.append(
            PriceSlot(
                t=slot_time,
                buy=buy,
                sell=0.0,
                source=source,
                is_actual=slot_time <= now < slot_time + timedelta(minutes=SLOT_MINUTES),
                stale=stale,
                energy_tax=slot_tax,
            )
        )
    future = [slot for slot in slots if slot.t >= start]
    forecast = PriceForecast(
        slots=slots,
        generated_at=now,
        source=source,
        stale=bool(future) and all(slot.stale for slot in future),
        notes=list(notes),
        tail_slots=_tail_slots(
            table,
            times,
            slots,
            source=source,
            end=end,
            hours=_terminal_tail_hours(settings),
        ),
    )
    return forecast


def _terminal_tail_hours(settings: Any) -> float:
    """``optimizer.terminal_tail_hours``: the stage-3 tail length, 0 when unusable (no tail)."""
    try:
        value = float(settings.get("optimizer.terminal_tail_hours", 24.0))
    except (TypeError, ValueError):
        return 24.0
    if value != value:  # NaN
        return 24.0
    return 0.0 if value <= 0.0 else min(value, 96.0)


def _tail_slots(
    table: dict[datetime, tuple[float, float]],
    times: Sequence[datetime],
    window: Sequence[PriceSlot],
    *,
    source: str,
    end: datetime,
    hours: float,
) -> list[PriceSlot]:
    """The stage-3 tail: provider entries past the plan horizon, then a periodic profile.

    Additive, and read only by ``optimize/terminal.py`` (docs/OPTIMIZER_V2.md section 4).  The plan
    horizon and the fetched window are the same window -- ``optimizer.horizon_hours`` sizes both --
    so section 4's "beyond the fetched window" is exactly this: the real prices the provider
    already published past the horizon, and the fetched window repeated by time-of-day beyond the
    last real entry.  ``slots`` is never touched, so the live path, the feed-in settlement and the
    saldering period volumes cannot move.
    """
    if hours <= 0.0 or not window:
        return []
    profile: dict[tuple[int, int], PriceSlot] = {}
    for slot in window:
        profile.setdefault((slot.t.hour, slot.t.minute), slot)
    tail: list[PriceSlot] = []
    for slot_time in slot_range(end, end + timedelta(hours=hours), SLOT_MINUTES):
        position = bisect.bisect_right(times, slot_time)
        if position and times[position - 1] == slot_time:
            buy, slot_tax = table[slot_time]
            stale = False
        else:
            # Beyond the last real entry: the same clock time from the fetched window (the last
            # fetched slot when the horizon is shorter than a day and that clock time never occurs).
            reference = profile.get((slot_time.hour, slot_time.minute), window[-1])
            buy, slot_tax = reference.buy, reference.energy_tax
            stale = True
        tail.append(
            PriceSlot(
                t=slot_time,
                buy=buy,
                sell=0.0,
                source=source,
                stale=stale,
                energy_tax=slot_tax,
            )
        )
    return tail


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
    start = _grid_start(now, settings)
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
    # ``slot_range`` excludes its end, so the grid's last slot always starts one step before
    # ``horizon_end``: coverage reaches the horizon when the *fresh* slots' own cadence carries the
    # last one up to it. Measure with that cadence (60 min for an hourly feed, 15 for a
    # quarter-hourly one), or a fully covered forecast would report a phantom uncovered tail and a
    # note claiming stale slots that are fresh.
    step = _grid_minutes([slot.t for slot in fresh]) or SLOT_MINUTES
    covered_until = coverage_end + timedelta(minutes=step)
    if covered_until < horizon_end:
        forecast.degraded = True
        forecast.notes.append(
            f"coverage ends {to_iso(covered_until)} before the requested horizon "
            f"{to_iso(horizon_end)}; uncovered slots are stale"
        )
    if mark_overall_stale and coverage_end < start + timedelta(hours=MIN_FRESH_COVERAGE_HOURS):
        forecast.stale = True
        forecast.notes.append("less than one hour of fresh price coverage; treating the list as stale")


def _apply_feed_in(
    forecast: PriceForecast, settings: Any, client: Any, tz: Any = None
) -> bool:
    """Fill ``slot.sell`` (SPEC.md section 4).

    ``sell`` is EUR/kWh *received*. Resolution order:

    1. ``price.feed_in_entity`` (current HA sensor state, its declared unit).
    2. ``price.feed_in_eur_per_kwh`` (flat tariff).
       Both are net of ``price.export_fee_eur_per_kwh`` and get ``price.vat_pct`` added when
       ``price.feed_in_tax_inclusive`` is off.
    3. Derived, when neither is configured: the consumer buy price you pay minus
       ``price.export_fee_eur_per_kwh`` (the feed-in cost, default 0), and minus the slot's
       energy-tax component when ``price.feed_in_energy_tax_refund`` is off (a contract that does
       not pay the energy tax back on redelivery).

    A configured feed-in source that cannot be read degrades to the derived price with a note:
    failing to price export must never fail the buy forecast. Returns False only when there is
    nothing to price (no slots).

    Provenance is recorded on the forecast as the additive ``forecast.feed_in_source`` attribute
    (the orchestrator-owned ``PriceForecast`` is not edited): ``"entity"`` or ``"flat"`` only when
    that explicit source really resolved, ``"derived"`` when no explicit source is configured, and
    ``"unavailable"`` when a configured source failed and the slots carry the derived price.  The
    optimizer wiring must never hand ``"unavailable"`` slots to the settlement as if they were an
    authoritative explicit tariff.
    """
    entity = str(settings.get("price.feed_in_entity", "") or "")
    tariff: float | None = None
    provenance = "derived"
    origin = ""
    try:
        if entity:
            provenance = "entity"
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
            provenance = "flat"
            tariff = float(settings.get("price.feed_in_eur_per_kwh"))
            origin = "price.feed_in_eur_per_kwh"
    except Exception as err:  # noqa: BLE001 - a broken feed-in sensor must not break the buy price
        provenance = "unavailable"
        forecast.notes.append(
            f"configured feed-in source unavailable ({type(err).__name__}: {err}); "
            "using the derived feed-in price"
        )
    if tariff is None and provenance in ("entity", "flat"):
        # The configured source resolved to nothing usable (a malformed state that did not raise):
        # the slots below carry the derived price, so the provenance must say so.
        provenance = "unavailable"
    export_fee = _fees(settings)[1]
    if tariff is not None:
        if not bool(settings.get("price.feed_in_tax_inclusive", True)):
            tariff *= 1.0 + float(settings.get("price.vat_pct", 0.0) or 0.0) / 100.0
        net = tariff - export_fee
        for slot in forecast.slots:
            slot.sell = net
        forecast.notes.append(
            f"sell price {net:.4f} EUR/kWh received ({origin}; feed-in cost {export_fee:.4f} EUR/kWh)"
        )
        forecast.feed_in_source = provenance
        return bool(forecast.slots)

    # Derived: the consumer price you pay, minus the feed-in cost, minus the energy-tax component
    # when the contract does not pay it back. The component is per-slot where the provider publishes
    # a tax-excluded price and otherwise the configured wholesale adder (0.0 when unknown).
    refund = bool(settings.get("price.feed_in_energy_tax_refund", True))
    derive = derived_sell_rule(settings)
    taxed = 0
    for slot in forecast.slots:
        component = float(getattr(slot, "energy_tax", 0.0) or 0.0)
        if not refund and component:
            taxed += 1
        slot.sell = derive(slot.buy, component)
    detail = f"derived feed-in price: buy price minus feed-in cost {export_fee:.4f} EUR/kWh"
    if not refund:
        if taxed:
            detail += f" minus the slot energy tax (known for {taxed}/{len(forecast.slots)} slots)"
        else:
            detail += (
                "; no energy-tax component is known for this source, so none was subtracted "
                "(set price.energy_tax_eur_per_kwh to state it)"
            )
    forecast.notes.append(detail)
    forecast.feed_in_source = provenance
    return bool(forecast.slots)


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
    state_divisor: float = 1.0,
) -> None:
    """Compare the sensor state with the raw current slot (docs/EXTERNAL_APIS.md section 7, item 10/12).

    Zonneplan keeps serving its last payload when the upstream refresh fails, so a mismatch between
    the live state and the slot covering ``now`` means either a changed scale or a stale payload;
    both must disable the provider instead of corrupting every cost number. Fees are excluded from
    the comparison: the state is the sensor price, the fee is our own add-on markup.

    The state is stored in the sensor's *declared* unit (not fixed-point), so ``state_divisor``
    converts it to EUR/kWh before comparing (``1.0`` for €/kWh, ``100.0`` for ct/kWh); the raw slot
    value keeps the full fixed-point ``divisor``. Without this a ct/kWh state would be compared
    against a EUR/kWh slot and always look like a scale change.
    """
    try:
        state_price = eur_per_kwh_from_raw(float(state_value), "eur_per_kwh", state_divisor)
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
    # The sensor state is stored in its declared unit, not fixed-point; convert it to EUR/kWh with
    # the declared divisor alone (the raw slot value keeps the full fixed-point ``divisor``).
    declared_now = unit_and_divisor(attributes.get("unit_of_measurement"))
    state_divisor = declared_now[1] if declared_now is not None else 1.0
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
    # Sources that publish a tax-excluded price next to the included one give the energy-tax
    # component per slot (Zonneplan); the derived feed-in price needs it when the contract does not
    # pay the energy tax back.
    taxes = {
        moment: component
        for moment, _buy, entry in parsed
        if (component := _entry_tax_component(entry, unit, divisor)) is not None
    }
    if scale_check:
        _scale_check(
            source,
            state.get("state"),
            parsed,
            unit=unit,
            divisor=divisor,
            tz=tz,
            now=now,
            state_divisor=state_divisor,
        )
    forecast = _build_forecast(
        _normalise_pairs(
            [(moment, buy) for moment, buy, _entry in parsed],
            unit=unit,
            divisor=divisor,
            settings=settings,
            apply_tax=False,
            taxes=taxes or None,
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
# Provider/contract resolution (SPEC.md section 4.1)
# --------------------------------------------------------------------------------------------

#: Bounded module-level cache of the located Zonneplan entity, so a replan does not pull every
#: state. Keyed by (provider, contract); the stored client id keeps a different HA client (or a
#: test) from reusing another's pick. A monotonic timestamp enforces the documented TTL. Every
#: cache access is wrapped so a cache fault can never raise into the caller.
_ZONNEPLAN_ENTITY_CACHE: dict[tuple[str, str], tuple[float, int, str, str]] = {}
_ZONNEPLAN_CACHE_TTL_S = 3600.0
_ZONNEPLAN_CACHE_MAX = 16


def clear_zonneplan_cache() -> None:
    """Drop the located-Zonneplan cache (settings/client changes and tests)."""
    _ZONNEPLAN_ENTITY_CACHE.clear()


def _zonneplan_cache_get(client: Any, contract: str) -> tuple[str, str] | None:
    try:
        entry = _ZONNEPLAN_ENTITY_CACHE.get(("zonneplan", contract))
        if not entry:
            return None
        stamp, client_id, entity_id, note = entry
        if client_id != id(client) or (time.monotonic() - stamp) > _ZONNEPLAN_CACHE_TTL_S:
            return None
        return entity_id, note
    except Exception:  # noqa: BLE001 - cache access must never raise
        return None


def _zonneplan_cache_set(client: Any, contract: str, entity_id: str, note: str) -> None:
    try:
        if len(_ZONNEPLAN_ENTITY_CACHE) >= _ZONNEPLAN_CACHE_MAX:
            _ZONNEPLAN_ENTITY_CACHE.clear()
        _ZONNEPLAN_ENTITY_CACHE[("zonneplan", contract)] = (time.monotonic(), id(client), entity_id, note)
    except Exception:  # noqa: BLE001 - cache access must never raise
        pass


class _SettingsOverlay:
    """Read-through settings wrapper that overrides a few keys (e.g. the located entity id).

    Everything else delegates to the wrapped :class:`~app.config.Settings`, so injecting the
    located Zonneplan entity into ``price.entity`` never disturbs any other setting read.
    """

    def __init__(self, base: Any, overrides: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides))

    def get(self, key: str, default: Any = None) -> Any:
        overrides = self._overrides
        if key in overrides:
            return overrides[key]
        return self._base.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return self._base[key]

    def __contains__(self, key: str) -> bool:
        return key in self._overrides or key in self._base


def _grid_minutes(moments: Sequence[datetime]) -> int | None:
    """Detect a regular 15-minute or hourly grid from the entries' own start times.

    The cadence is the most common spacing, so one dropped or mis-timed entry does not hide an
    otherwise regular grid. Fewer than two entries have no spacing and therefore no grid.
    """
    if len(moments) < 2:
        return None
    spaced = sorted(moments)
    spacings = [
        int(round((spaced[index + 1] - spaced[index]).total_seconds() / 60.0))
        for index in range(len(spaced) - 1)
    ]
    cadence = Counter(gap for gap in spacings if gap > 0).most_common(1)
    if not cadence:
        return None
    minutes = cadence[0][0]
    return minutes if minutes in (15, 60) else None


def _zonneplan_candidate(
    state: Any, settings: Any, now: datetime, tz: Any
) -> dict[str, Any] | None:
    """Return a Zonneplan electricity tariff candidate from an HA state, or ``None``.

    Identification is STRUCTURAL and never uses the entity name. Two hard filters separate a real
    electricity tariff from look-alikes (SPEC.md 4.1 + live-instance refinement):

    * **A kWh unit.** The instance's *gas* tariff publishes the exact same ``price_tax_included``
    fixed-point shape as the electricity tariff, and its own state agrees with its single slot, so
      the scale check does not reject it. Only the unit differs (€/m³, not €/kWh). A candidate whose
      declared unit is not in the EUR/kWh family (€/kWh, EUR/kWh, ct/kWh) is rejected outright; a
    ct/kWh sensor is accepted and converts to EUR/kWh through its declared divisor.
    * **At least two entries on a regular 15-minute or hourly grid.** The gas tariff has a single
      24-hour slot, so this grid check alone also rejects it.

    On top of the two filters, the candidate's own state must still agree with its current slot (the
    same :func:`_scale_check` the provider uses), which catches stale payloads and scale changes.
    """
    try:
        if not isinstance(state, Mapping):
            return None
        attributes = state.get("attributes")
        if not isinstance(attributes, Mapping):
            return None
        # (a) kWh unit: reject the gas tariff (€/m³), EUR/MWh and other non-electricity sensors.
        declared = unit_and_divisor(attributes.get("unit_of_measurement"))
        if declared is None or declared[0] != "eur_per_kwh":
            return None
        entries = _first_forecast_list(attributes)
        if entries is None:
            return None
        parsed: list[tuple[datetime, Any, Any]] = []
        for entry in entries:
            try:
                moment = _entry_time(entry, tz)
            except (ValueError, TypeError):
                continue  # drop entries with missing/None dates (docs/EXTERNAL_APIS.md item 11)
            try:
                buy = _entry_price(entry, ZONNEPLAN_PRICE_KEYS)
            except ValueError:
                continue
            parsed.append((moment, buy, entry))
        parsed.sort(key=lambda item: item[0])
        # (b) a regular 15-minute or hourly grid with at least two entries (rejects the 1-slot gas).
        grid = _grid_minutes([moment for moment, _buy, _entry in parsed])
        if grid is None:
            return None
        scale = float(settings.get("price.scale_divisor", 1.0) or 1.0) * ZONNEPLAN_SCALE_DIVISOR
        divisor = declared[1] * scale
        try:
            _scale_check(
                "zonneplan_entity",
                state.get("state"),
                parsed,
                unit=declared[0],
                divisor=divisor,
                tz=tz,
                now=now,
                state_divisor=declared[1],
            )
        except ProviderError:
            return None  # a stale payload, changed scale or no slot covering now is not a tariff.
        return {
            "entity_id": str(state.get("entity_id") or ""),
            "grid": grid,
            "unit": declared[0],
            "divisor": divisor,
        }
    except Exception:  # noqa: BLE001 - one malformed state never aborts the scan.
        return None


def _preferred_grid(contract: str) -> int | None:
    return {"dynamic_quarter_hourly": 15, "dynamic_hourly": 60}.get(contract)


def _select_grid(
    candidates: list[dict[str, Any]], preferred: int | None
) -> tuple[dict[str, Any] | None, str]:
    """Pick the candidate matching the contract's preferred grid, else the other grid with a note.

    ``dynamic_quarter_hourly`` prefers the 15-minute grid, ``dynamic_hourly`` the hourly one. When
    only the other grid exists it is used **with a note**, never silently (SPEC.md 4.1).
    """
    matching = sorted((c for c in candidates if c["grid"] == preferred), key=lambda c: c["entity_id"])
    if matching:
        return matching[0], ""
    others = sorted((c for c in candidates if c["grid"] != preferred), key=lambda c: c["entity_id"])
    if not others:
        return None, ""
    chosen = others[0]
    want = {15: "15-minute", 60: "hourly"}.get(preferred, "requested")
    got = "15-minute" if chosen["grid"] == 15 else "hourly"
    return chosen, (
        f"no {want} Zonneplan tariff found; used the {got} Zonneplan tariff instead "
        "(grid preference comes from price.contract)"
    )


def _locate_zonneplan_entity(
    client: Any, settings: Any, now: datetime, tz: Any, contract: str
) -> tuple[str, str]:
    """Find the Zonneplan electricity tariff entity in Home Assistant (SPEC.md 4.1).

    Returns ``(entity_id, note)`` where ``note`` is non-empty only when a non-preferred grid had to
    be used. Raises :class:`ProviderError` (naming the provider) when no candidate qualifies.
    """
    cached = _zonneplan_cache_get(client, contract)
    if cached is not None and cached[0]:
        return cached
    if client is None:
        raise ProviderError(
            "zonneplan: no Home Assistant client available to search for a tariff sensor"
        )
    try:
        states = client.get_states()
    except HAError as err:
        raise ProviderError(f"zonneplan: cannot read Home Assistant states: {err}") from None
    candidates = [
        found
        for state in states
        if (found := _zonneplan_candidate(state, settings, now, tz)) is not None
    ]
    chosen, note = _select_grid(candidates, _preferred_grid(contract))
    if chosen is None or not chosen["entity_id"]:
        raise ProviderError(
            "zonneplan: no compatible electricity tariff sensor found (need a EUR/kWh sensor with a "
            "2+ entry 15-minute or hourly price list whose state matches its current slot); enable "
            "the Zonneplan integration, or pick another price provider"
        )
    _zonneplan_cache_set(client, contract, chosen["entity_id"], note)
    return chosen["entity_id"], note


def effective_price_source(settings: Any) -> str:
    """The concrete source name ``price.provider`` resolves to (no client, no network)."""
    provider = str(settings.get("price.provider", "entity") or "entity").strip()
    if provider == "zonneplan":
        return "zonneplan_entity"
    if provider == "day_ahead":
        return "energy_charts"
    if provider == "fixed":
        return "fixed_tariff"
    return str(settings.get("price.source", "zonneplan_entity") or "zonneplan_entity")


def _resolve_source(
    settings: Any, client: Any, now: datetime, tz: Any
) -> tuple[str, Any, list[str], bool]:
    """Resolve ``price.provider`` + ``price.contract`` to ``(source, settings, notes, no_fallback)``.

    ``settings`` is the view the providers read (an overlay injects the located Zonneplan entity id),
    ``notes`` are resolution notes to attach when the resolved provider succeeds, and
    ``no_fallback`` forces a single-element chain (the fixed provider must never fall back to the
    market). The default ``entity`` provider keeps the pre-0.3.0 path exactly.
    """
    provider = str(settings.get("price.provider", "entity") or "entity").strip()
    contract = str(settings.get("price.contract", "dynamic_hourly") or "dynamic_hourly").strip()
    if provider == "zonneplan":
        entity_id, note = _locate_zonneplan_entity(client, settings, now, tz, contract)
        overlay = _SettingsOverlay(settings, {"price.entity": entity_id})
        return "zonneplan_entity", overlay, ([note] if note else []), False
    if provider == "day_ahead":
        return "energy_charts", settings, [], False
    if provider == "fixed":
        return "fixed_tariff", settings, [], True
    return str(settings.get("price.source", "zonneplan_entity") or "zonneplan_entity"), settings, [], False


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

    The window is **today + tomorrow** (``start``/``end``): a plan horizon of 30 h reaches past
    midnight, and the default window answers for today only, so asking for one day left the whole
    second day filled flat from the last known price (`price_stale`), which hides every overnight
    cheap slot and every next-evening peak from the optimiser (live 2026-10-21). Tomorrow 404s
    until the auction result is published, so the today-only window is the fallback and an
    uncovered tail is still marked stale/degraded rather than failing the provider (SPEC appendix A).
    """
    moment = now or utcnow()
    zone = str(settings.get("price.bzn", "NL") or "NL").strip()
    day = moment.astimezone(_zone(tz or settings.get("timezone"))).date()
    urls = (
        f"{ENERGY_CHARTS_URL}?bzn={zone}&start={day.isoformat()}"
        f"&end={(day + timedelta(days=1)).isoformat()}",
        f"{ENERGY_CHARTS_URL}?bzn={zone}",
    )
    http_get = http or http_get_json
    payload: Any = None
    missing: ProviderError | None = None
    for url in urls:
        try:
            candidate = http_get(url)
        except ProviderError as err:
            if err.status == 400:
                raise ProviderError(
                    f"energy_charts: bidding zone {zone!r} was rejected by the API", status=400
                ) from None
            if err.status != 404:
                raise
            # 404: tomorrow's auction is not published yet (or, on the today-only window, this
            # zone has no prices at all). Either way the next window is the fallback.
            missing = err
            continue
        if not isinstance(candidate, Mapping):
            raise ProviderError("energy_charts: unexpected response shape")
        if not (candidate.get("unix_seconds") or candidate.get("price")):
            missing = None
            continue
        payload = candidate
        break
    if payload is None:
        if missing is not None:
            # A valid zone for which this source publishes nothing (e.g. NL returns 404 "no
            # content available"). Keep the legible message on the provider status so
            # explain_forecast can name the zone and point at the alternatives.
            raise ProviderError(
                f"energy_charts: bidding zone {zone!r} is valid, but this source publishes no "
                "day-ahead prices for it (no content available)",
                status=404,
            ) from None
        raise ProviderError(
            f"energy_charts: publishes no day-ahead prices for bidding zone {zone!r} (empty payload)"
        )
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
    if not pairs:
        # An empty (but well-formed) payload means this source has nothing for the zone; keep the
        # message zone-shaped so explain_forecast can name price.bzn and the alternatives.
        raise ProviderError(
            f"energy_charts: publishes no day-ahead prices for bidding zone {zone!r} (empty payload)"
        )
    # day_ahead is ex-VAT/ex-tax, so name the adders actually applied (SPEC 4.1): a wholesale price
    # with no adders configured is not a consumer price, and the UI must be able to say so.
    vat = float(settings.get("price.vat_pct", 0.0) or 0.0)
    tax = float(settings.get("price.energy_tax_eur_per_kwh", 0.0) or 0.0)
    import_fee = _fees(settings)[0]
    notes = [
        f"unit {unit_text} -> {unit} (divisor {divisor:g})",
        (
            f"day-ahead spot (wholesale, not a consumer price); adders applied: VAT {vat:g}%, "
            f"energy tax {tax:g} EUR/kWh, import fee {import_fee:g} EUR/kWh"
        ),
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
    start = _grid_start(moment, settings)
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


def _int_setting(settings: Any, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def _fixed_rate(
    settings: Any, client: Any, typed_key: str, entity_key: str, label: str
) -> float:
    """Resolve one fixed consumer price (EUR/kWh, tax included, no adders).

    The entity, when set, wins over the typed value. A missing, non-numeric or unavailable price is
    a hard :class:`ProviderError` naming the exact setting to fill -- a fixed contract never gets a
    guessed or market-derived number (SPEC.md 4.1).
    """
    entity = str(settings.get(entity_key, "") or "").strip()
    if entity:
        if client is None:
            raise ProviderError(
                f"fixed: {entity_key} is set but no Home Assistant client is available to read it"
            )
        try:
            state = client.get_state(entity)
        except HAError as err:
            raise ProviderError(f"fixed: cannot read {entity_key} {entity!r}: {err}") from None
        attributes = (state.get("attributes") if isinstance(state, Mapping) else None) or {}
        raw = state.get("state") if isinstance(state, Mapping) else None
        declared = unit_and_divisor(attributes.get("unit_of_measurement"))
        try:
            if declared is None:
                return _to_float(raw)  # no declared unit: assume the sensor already reports EUR/kWh
            unit, divisor = declared
            return eur_per_kwh_from_raw(_to_float(raw), unit, divisor)
        except (ValueError, TypeError):
            raise ProviderError(
                f"fixed: {entity_key} {entity!r} has a non-numeric or unavailable price {raw!r}; "
                f"set {typed_key} as a fallback"
            ) from None
    typed = settings.get(typed_key)
    if typed is None:
        raise ProviderError(
            f"fixed: a {label} price is required; set {typed_key} or {entity_key}"
        )
    try:
        return float(typed)
    except (TypeError, ValueError):
        raise ProviderError(f"fixed: {typed_key} is not a number ({typed!r})") from None


def fetch_fixed_tariff(
    client: Any, settings: Any, now: datetime | None = None, http: Callable[..., Any] | None = None, tz: str | None = None
) -> PriceForecast:
    """Fixed consumer tariff (provider=fixed). SPEC.md 4.1: prices incl. VAT, NO adders.

    ``fixed_single`` uses one rate all day; ``fixed_day_night`` splits by the configured day window
    evaluated per slot in the configured timezone, so DST is handled by the local wall-clock hour
    (the window), never by an elapsed hour count. This provider has no market fallback: the loader
    puts it on a single-element chain, so a missing price fails loudly rather than silently planning
    against market prices.
    """
    moment = now or utcnow()
    zone = _zone(tz or settings.get("timezone"))
    contract = str(settings.get("price.contract", "fixed_single") or "fixed_single")
    horizon = _horizon_hours(settings)
    start = _grid_start(moment, settings)
    grid = slot_range(start, start + timedelta(hours=horizon), SLOT_MINUTES)
    if contract == "fixed_day_night":
        day_price = _fixed_rate(
            settings, client, "price.fixed_day_price_eur_per_kwh", "price.fixed_day_price_entity", "day"
        )
        night_price = _fixed_rate(
            settings, client, "price.fixed_night_price_eur_per_kwh", "price.fixed_night_price_entity", "night"
        )
        day_start = _int_setting(settings, "price.fixed_day_start_hour", 0)
        night_start = _int_setting(settings, "price.fixed_night_start_hour", 24)

        def price_for(local_hour: int) -> float:
            # day = [day_start, night_start); night = the rest. A wrap (day_start > night_start)
            # makes the day window span midnight, exactly like the static schedule's overnight case.
            if day_start <= night_start:
                is_day = day_start <= local_hour < night_start
            else:
                is_day = local_hour >= day_start or local_hour < night_start
            return day_price if is_day else night_price

        note = (
            f"fixed day/night tariff: day {day_price:g} EUR/kWh, night {night_price:g} EUR/kWh; "
            f"day window {day_start:02d}:00-{night_start:02d}:00 local; no adders applied"
        )
    else:  # fixed_single (and any non-day-night contract): one rate all day.
        single = _fixed_rate(
            settings, client, "price.fixed_price_eur_per_kwh", "price.fixed_price_entity", "single"
        )
        price_for = lambda _hour: single  # noqa: E731 - one flat rate all day
        note = f"fixed tariff: {single:g} EUR/kWh (consumer price, no adders)"
    pairs: list[tuple[datetime, float]] = []
    for slot_time in grid:
        pairs.append((slot_time, price_for(slot_time.astimezone(zone).hour)))
    return _build_forecast(
        pairs, source="fixed_tariff", settings=settings, now=moment, notes=[note]
    )


#: Source registry (SPEC.md section 4). Signature: ``fetch(client, settings, now=, http=, tz=)``.
PROVIDERS: dict[str, Callable[..., PriceForecast]] = {
    "zonneplan_entity": fetch_zonneplan_entity,
    "energy_charts": fetch_energy_charts,
    "entity_forecast": fetch_entity_forecast,
    "static": fetch_static,
    "fixed_tariff": fetch_fixed_tariff,
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
    """Load a price forecast for the effective provider (SPEC.md section 4.1).

    ``price.provider`` + ``price.contract`` resolve to a concrete source and chain:

    * ``entity`` (default) keeps the pre-0.3.0 path ``price.source -> energy_charts -> static``.
    * ``zonneplan``/``day_ahead`` resolve first, then fall back to energy_charts (if not already the
      resolved source) and static.
    * ``fixed`` uses ``fixed_tariff`` **only** — a fixed contract never falls back to the market.

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
    configured, effective_settings, resolve_notes, no_fallback = _resolve_source(
        settings, client, moment, tz
    )
    if configured not in registry:
        raise ConfigError(f"unknown price source: {configured}")
    # ``fixed`` must never fall back to the market (SPEC 4.1): a single-element chain.
    chain = [configured] if no_fallback else _chain(configured)
    # The acceptance window is about *future* coverage, so it starts at the current quarter - not at
    # the (coarser) plan slot start the providers themselves fill from.
    start = quarter_floor(moment, SLOT_MINUTES)
    end = start + timedelta(hours=horizon_hours(settings))
    statuses: dict[str, ProviderStatus] = {}
    for name in registry:
        statuses[name] = ProviderStatus(
            kind="price",
            name=name,
            active=name == configured,
            detail={
                "unit": str(settings.get("price.unit", "auto")),
                "provider": str(settings.get("price.provider", "entity") or "entity"),
                "contract": str(settings.get("price.contract", "dynamic_hourly") or "dynamic_hourly"),
            },
        )
    attempts: list[str] = []
    for position, name in enumerate(chain):
        func = registry.get(name)
        if func is None:
            continue
        attempts.append(name)
        status = statuses[name]
        try:
            forecast = func(client, effective_settings, now=moment, http=http, tz=tz)
            if not any(start <= slot.t < end for slot in forecast.slots):
                # SPEC.md section 4: a forecast with no future slot in the requested horizon is a
                # provider failure, no matter how the provider signalled it (raised or returned).
                status.last_error = f"{name}: no future price slots in the requested horizon"
                continue
        except (RateLimited, ProviderError, HAError) as err:
            status.last_error = str(err)
            http_status = getattr(err, "status", None)
            if isinstance(http_status, int):
                status.detail["http_status"] = http_status
            continue
        except Exception as err:  # noqa: BLE001 - a broken provider must never abort the chain
            status.last_error = f"{type(err).__name__}: {err}"
            continue
        status.ok = True
        status.last_success = moment
        status.stale = forecast.stale
        status.detail["slots"] = len(forecast.slots)
        # A resolution note (e.g. the Zonneplan grid fallback) is the source's own explanation and
        # belongs before the feed-in note.
        if position == 0 and resolve_notes:
            forecast.notes.extend(resolve_notes)
        # SPEC.md section 4: sell is what export actually pays, never the buy price. Apply the
        # configured feed-in tariff before the fallback note, so that note stays last.
        status.detail["sell_known"] = _apply_feed_in(forecast, effective_settings, client, tz)
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
