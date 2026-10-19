"""Savings and accuracy accounting (SPEC sections 7.1 and 11).

Pure functions over plain rows/pairs so they can be tested without a database.  The savings
baseline is the definition the user is judged by: what the same realised consumption would have
cost with the battery doing nothing, at the same prices (SPEC 11).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .models import AccuracyReport, PriceSlot, SavingsBucket, SolarForecast, clamp, from_iso, to_iso

RANGES = ("day", "week", "month", "year", "all")

BASELINE_NOTE = (
    "Baseline: the same measured consumption at the same prices with the battery doing nothing. "
    "Savings = baseline cost - actual cost."
)

#: Google-style "one bucket per X" for each range.
_BUCKET_LENGTHS = {"day": 1, "week": 1, "month": 1, "year": 30, "all": 30}

#: Measured series whose presence a savings bucket reports (0.13.0).  Battery flow is "measured"
#: only when the driver read a real flow back, never merely because a setpoint was written.
MEASURED_SERIES = ("grid", "load", "pv", "battery")

#: Per-day measured battery flow fields.  ``None`` means "not fully measured today", so the bucket
#: reports unknown instead of a smaller number that looks measured (0.13.0).
MEASURED_FLOWS = ("charged_kwh", "discharged_kwh", "charge_cost_eur", "discharge_value_eur")

#: Sampled-time coverage behind the stored totals (0.30.0), in hours.  ``savings_hours`` is the
#: time behind ``baseline_cost_eur``/``savings_eur``, ``battery_hours`` the time behind the measured
#: flows, ``grid_hours`` the metered time behind import/export, and ``total_hours`` the sampled time
#: they are a share of.  Rows written before these columns existed report 0 and are counted as
#: legacy periods.
COVERAGE_HOURS = ("total_hours", "grid_hours", "savings_hours", "battery_hours")


def range_bounds(range_key: str, now: datetime) -> tuple[datetime, datetime]:
    """Start/end of a savings range, both inclusive of ``now``."""
    if range_key not in RANGES:
        raise ValueError(f"unknown range: {range_key!r}")
    moment = now.astimezone(timezone.utc)
    today = moment.date()
    midnight = datetime.combine(today, time.min, tzinfo=timezone.utc)
    if range_key == "day":
        return midnight, moment
    if range_key == "week":
        return midnight - timedelta(days=6), moment
    if range_key == "month":
        return midnight - timedelta(days=29), moment
    if range_key == "year":
        return midnight - timedelta(days=364), moment
    return datetime(1970, 1, 1, tzinfo=timezone.utc), moment


def _bucket_key(day: date, range_key: str) -> str:
    if range_key in ("year", "all"):
        return f"{day.year:04d}-{day.month:02d}"
    return day.isoformat()


def _bucket_start(key: str, range_key: str) -> datetime:
    if range_key in ("year", "all"):
        year, month = key.split("-")
        return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    year, month, day = (int(part) for part in key.split("-"))
    return datetime(year, month, day, tzinfo=timezone.utc)


def _bucket_end(key: str, range_key: str) -> datetime:
    start = _bucket_start(key, range_key)
    if range_key in ("year", "all"):
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return datetime(year, month, 1, tzinfo=timezone.utc)
    return start + timedelta(days=1)


def _parse_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _empty_bucket(key: str, range_key: str) -> SavingsBucket:
    return SavingsBucket(key=key, start=_bucket_start(key, range_key), end=_bucket_end(key, range_key))


def _bucket_days(start: datetime, end: datetime, range_key: str):
    """Ordered bucket keys covering the range (empty buckets included for a steady chart).

    Year and all buckets are *calendar* months, not 30-day steps: stepping 30 days from the first
    day of a month drifts out of the calendar (1970-01-01 + 30d = 1970-01-31, +30d = 1970-03-02,
    so February was skipped for the whole ``all`` axis and every later month could be missing or
    duplicated).
    """
    if range_key in ("year", "all"):
        keys: list[str] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            keys.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return keys
    keys = []
    cursor = start.date()
    last = end.date()
    while cursor <= last:
        key = _bucket_key(cursor, range_key)
        if key not in keys:
            keys.append(key)
        cursor += timedelta(days=_BUCKET_LENGTHS.get(range_key, 1))
    return keys


def savings_from_daily(
    rows: Iterable[Mapping[str, Any]],
    range_key: str,
    now: datetime,
    *,
    capacity_kwh: float = 0.0,
) -> tuple[list[SavingsBucket], dict[str, Any]]:
    """Aggregate ``savings_daily`` rows into the range's buckets plus totals (SPEC 11).

    ``arbitrage_kwh``/``self_consumption_kwh`` (SPEC 10) split the realised battery throughput by
    where it went: discharged energy that was exported earned arbitrage, the rest displaced import
    (self-consumption).  The stored daily rows carry cycles, not throughput, so the split uses
    ``cycles * capacity_kwh``; pass the capacity of the batteries in use and treat the split as the
    documented estimate it is (0.0 when no capacity is known).

    ``baseline_cost_eur``/``savings_eur`` are known when the bucket has at least one row that could
    be re-priced, and are summed per row: an unknown row's cost is never subtracted from a known
    baseline (a month bucket can span days from before and after a gap).  A bucket with no such row
    reports ``None`` for both, never a fabricated 0, and exposes ``has_data`` so a bucket with no
    stored rows is never counted as a covered period (SPEC 11).

    Coverage is summed in *hours* (0.30.0): ``savings_hours`` is the sampled time behind the
    baseline/savings, ``battery_hours`` the time behind the measured flows, ``grid_hours`` the
    metered time behind import/export and ``total_hours`` the sampled time they are a share of.  A
    row written before the hour columns existed contributes 0 hours and is counted once in
    ``legacy_periods``; its stored totals still contribute, so old history is not lost.
    """
    start, end = range_bounds(range_key, now)
    keys = _bucket_days(start, end, range_key)
    buckets = {key: _empty_bucket(key, range_key) for key in keys}
    start_day, end_day = start.date(), end.date()
    arbitrage_kwh = 0.0
    self_consumption_kwh = 0.0
    # Buckets whose rows carried no usable baseline: their savings are unknown, not zero.  An
    # empty bucket is also unknown: it has no activity to re-price, so it must not look like a
    # covered 0.00 period (SPEC 11).
    baseline_known: set[str] = set()
    bucket_rows: set[str] = set()
    # Per-bucket coverage of each measured series (only a fully measured bucket keeps the flag) and
    # per-bucket sums of the measured battery flows.  Flows are summed whenever a row carries one:
    # since 0.30.0 a row's flow is the part that *was* measured, not an all-or-nothing day, so one
    # earlier gap must not erase the measured part (that is what hid the year/all flows entirely).
    measured_series: dict[str, set[str]] = {}
    flow_sums: dict[str, dict[str, float]] = {}
    flow_seen: dict[str, set[str]] = {}
    hour_sums: dict[str, dict[str, float]] = {}

    for row in rows:
        day = _parse_day(row.get("day") or row.get("ts"))
        if day is None or day < start_day or day > end_day:
            continue
        key = _bucket_key(day, range_key)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _empty_bucket(key, range_key)
            buckets[key] = bucket
        series = measured_series.setdefault(key, set(MEASURED_SERIES))
        for name in MEASURED_SERIES:
            if not row.get(f"{name}_measured"):
                series.discard(name)
        seen = flow_seen.setdefault(key, set())
        sums = flow_sums.setdefault(key, {name: 0.0 for name in MEASURED_FLOWS})
        for name in MEASURED_FLOWS:
            value = row.get(name)
            if value is not None:
                seen.add(name)
                sums[name] += float(value)
        hours = hour_sums.setdefault(key, {name: 0.0 for name in COVERAGE_HOURS})
        for name in COVERAGE_HOURS:
            hours[name] += float(row.get(name) or 0.0)
        # The realised battery throughput is measured discharge when the flow is known; legacy
        # rows (and pre-0.13 rows) fall back to the stored cycle estimate.
        discharged = row.get("discharged_kwh")
        if discharged is not None:
            throughput = max(0.0, float(discharged))
        else:
            cycles = float(row.get("battery_cycles") or row.get("cycles") or 0.0)
            throughput = max(0.0, cycles) * max(0.0, float(capacity_kwh))
        export_kwh = float(row.get("export_kwh") or 0.0)
        arbitrage_kwh += min(throughput, export_kwh)
        self_consumption_kwh += max(0.0, throughput - export_kwh)
        bucket.import_kwh += float(row.get("import_kwh") or 0.0)
        bucket.export_kwh += export_kwh
        bucket.pv_kwh += float(row.get("pv_kwh") or 0.0)
        bucket.cost_eur += float(row.get("cost_eur") or 0.0)
        bucket.revenue_eur += float(row.get("revenue_eur") or 0.0)
        bucket_rows.add(key)
        baseline = row.get("baseline_cost_eur")
        if baseline is not None:
            # Sum the rows that *could* be re-priced, per row.  A bucket spanning rows from before
            # and after a gap (a month in the year/all range) keeps its known portion; its savings
            # is accumulated per row so an unknown row's cost can never be subtracted from a known
            # baseline (which is what made old all-range totals look like a large loss).
            baseline_known.add(key)
            bucket.baseline_cost_eur += float(baseline)
            row_savings = row.get("savings_eur")
            if row_savings is None:
                row_savings = float(baseline) - (
                    float(row.get("cost_eur") or 0.0) - float(row.get("revenue_eur") or 0.0)
                )
            bucket.savings_eur += float(row_savings)
        bucket.cycles += float(row.get("battery_cycles") or 0.0)

    ordered = []
    for key in sorted(buckets):
        bucket = buckets[key]
        has_data = key in bucket_rows
        if not (has_data and key in baseline_known):
            # No rows at all, or no row with a knowable baseline: unknown, never a fabricated
            # zero.  An empty bucket therefore has no baseline to be "covered" by, so the UI can
            # no longer mislabel empty days as covered periods (SPEC 11).
            bucket = replace(bucket, baseline_cost_eur=None, savings_eur=None)
        if has_data:
            seen = flow_seen.get(key, set())
            sums = flow_sums.get(key, {})
            hours = hour_sums.get(key, {})
            bucket = replace(
                bucket,
                **{name: (float(sums.get(name, 0.0)) if name in seen else None) for name in MEASURED_FLOWS},
                **{f"{name}_measured": name in measured_series.get(key, set()) for name in MEASURED_SERIES},
                **{name: float(hours.get(name, 0.0)) for name in COVERAGE_HOURS},
            )
        bucket = replace(bucket, has_data=has_data)
        ordered.append(bucket)

    def _total_flow(name: str) -> float | None:
        values = [getattr(b, name) for b in ordered if getattr(b, name) is not None]
        return sum(values) if values else None

    def _total_hours(name: str) -> float:
        return sum(float(getattr(b, name) or 0.0) for b in ordered)

    # Only buckets with a known baseline contribute a total; an empty bucket must not be silently
    # added as zero (SPEC 11).
    known_buckets = [b for b in ordered if b.baseline_cost_eur is not None]
    measured_buckets = [b for b in ordered if b.has_data]
    totals = {
        "import_kwh": sum(b.import_kwh for b in ordered),
        "export_kwh": sum(b.export_kwh for b in ordered),
        "pv_kwh": sum(b.pv_kwh for b in ordered),
        "cost_eur": sum(b.cost_eur for b in ordered),
        "revenue_eur": sum(b.revenue_eur for b in ordered),
        "baseline_cost_eur": (
            sum(float(b.baseline_cost_eur) for b in known_buckets) if known_buckets else None
        ),
        "savings_eur": (
            sum(float(b.savings_eur) for b in known_buckets) if known_buckets else None
        ),
        "cycles": sum(b.cycles for b in ordered),
        "arbitrage_kwh": arbitrage_kwh,
        "self_consumption_kwh": self_consumption_kwh,
        "battery_throughput_kwh": arbitrage_kwh + self_consumption_kwh,
        "buckets": float(len(ordered)),
        # Additive (0.22.1): how much of the range actually has measured rows, and how much of
        # that could be re-priced.  The UI states coverage from these counts instead of guessing
        # from a baseline that empty buckets used to carry as 0.
        "measured_periods": float(len(measured_buckets)),
        "baseline_periods": float(len(known_buckets)),
        # Additive (0.13.0): measured battery flows and how many buckets actually measured each
        # series.  ``None`` for a flow means no bucket in this range carried one.
        "charged_kwh": _total_flow("charged_kwh"),
        "discharged_kwh": _total_flow("discharged_kwh"),
        "charge_cost_eur": _total_flow("charge_cost_eur"),
        "discharge_value_eur": _total_flow("discharge_value_eur"),
        "measured_buckets": {
            name: sum(1 for b in ordered if getattr(b, f"{name}_measured")) for name in MEASURED_SERIES
        },
        # Additive (0.30.0): the sampled time behind each total, so a partial day is presented as
        # "savings over X of Y sampled hours" instead of being reported as unknown.  A data-bearing
        # bucket with no hours at all predates the coverage columns (legacy_periods); its stored
        # totals still count, but it cannot claim a coverage share.
        **{name: _total_hours(name) for name in COVERAGE_HOURS},
        "legacy_periods": float(
            sum(1 for b in measured_buckets if float(b.total_hours or 0.0) <= 0.0)
        ),
    }
    return ordered, totals


def daily_row_delta(
    previous: Mapping[str, Any] | None,
    *,
    day: str,
    dt_hours: float,
    grid_kw: float | None,
    load_kw: float | None,
    pv_kw: float | None,
    battery_kw: float | None,
    price_buy: float | None,
    price_sell: float | None,
    capacity_kwh: float,
    now: datetime,
) -> dict[str, Any]:
    """Accumulate one tick into a ``savings_daily`` row (SPEC 11).

    ``battery_kw`` must be a *measured* flow; ``None`` means no confirmed measurement for this
    interval, which adds nothing to the day's battery flows or cycle count rather than a planned
    value (0.13.0, interval-level since 0.30.0).  The grid/load/PV coverage flags record which
    measured series were present; the ``*_hours`` counters record how much sampled time the totals
    really cover.

    The do-nothing baseline needs the realised ``load - pv`` for the interval.  It is taken from
    the metered load and PV when both exist, and otherwise from the grid identity
    ``load - pv = grid - p`` (SPEC 8.1), so a meter + battery installation with no load or PV
    sensor still gets a baseline.  No forecast is used: the baseline prices the same *realised*
    kWh, which a forecast would not.
    """
    row = dict(previous or {})
    row["day"] = day
    hours = clamp(float(dt_hours), 0.0, 1.0)
    import_kwh = max(0.0, float(grid_kw)) * hours if grid_kw is not None else 0.0
    export_kwh = max(0.0, -float(grid_kw)) * hours if grid_kw is not None else 0.0
    buy = float(price_buy or 0.0)
    sell = float(price_sell or 0.0)
    cost = buy * import_kwh
    revenue = sell * export_kwh
    # Do-nothing baseline: the *net* cost the household would have paid with the same realized
    # load/PV and tariff (import cost minus export revenue).  Omitting the export credit made a
    # no-battery household look like it was saving money, and a missing load forecast silently
    # turned the whole bill into "negative savings" (audit finding 13).
    #
    # The realised do-nothing *grid* power ``g0`` (kW, positive = import) has two measured forms:
    #   1. metered load and PV: ``g0 = load - pv`` (SPEC 11);
    #   2. the grid identity ``g0 = grid - p`` (SPEC 8.1: ``grid = load - pv + p``): exact for the
    #      realised interval and needing neither a load nor a PV sensor.  Its only extra reading is
    #      the measured battery flow, which the day already needs to earn any credit at all.
    # Without either, the load is not knowable and the baseline stays unknown -- never a guess.
    baseline_grid: float | None = None
    if load_kw is not None and pv_kw is not None:
        baseline_grid = float(load_kw) - float(pv_kw)
    elif grid_kw is not None and battery_kw is not None:
        baseline_grid = float(grid_kw) - float(battery_kw)
    baseline: float | None = None
    if baseline_grid is not None and grid_kw is not None:
        # The side the baseline lands on needs its price: pricing an unknown buy or sell at 0 would
        # make the counterfactual look like a real number (audit finding 13).
        if (baseline_grid > 0.0 and price_buy is None) or (
            baseline_grid < 0.0 and price_sell is None
        ):
            baseline = None
        else:
            baseline = (
                buy * max(0.0, baseline_grid) * hours - sell * max(0.0, -baseline_grid) * hours
            )

    def add(key: str, value: float) -> None:
        row[key] = float(row.get(key) or 0.0) + value

    def add_known(key: str, value: float | None) -> None:
        # Sticky unknown for a *value* (not for delivered energy): a partially priced total must
        # never look like a measured number.  Since 0.30.0 only the two flow values use this; the
        # energies and the coverage hours keep accumulating through a gap.
        if value is None or (key in row and row[key] is None):
            row[key] = None
        else:
            add(key, value)

    def cover(key: str, present: bool) -> None:
        # Coverage can only be lost: one unmeasured interval makes the day's series incomplete.
        # This flag stays "the whole period was measured"; the hour counters below say how much
        # of it the totals actually cover.
        if present:
            if row.get(key) is None:
                row[key] = 1
        else:
            row[key] = 0

    add("total_hours", hours)
    if grid_kw is not None:
        add("grid_hours", hours)
        add("import_kwh", import_kwh)
        add("export_kwh", export_kwh)
        add("cost_eur", cost)
        add("revenue_eur", revenue)
    if pv_kw is not None:
        add("pv_kwh", max(0.0, float(pv_kw)) * hours)
    # The do-nothing baseline and the actual net cost are accumulated for the same interval, so an
    # interval whose price or reading is missing is excluded from both and the saving stays exact
    # for the time it covers.  A single unreadable interval no longer discards the whole day
    # (0.30.0); ``savings_hours`` states how much sampled time the figure covers.
    if baseline is not None and grid_kw is not None:
        add("baseline_cost_eur", baseline)
        add("savings_eur", baseline - (cost - revenue))
        add("savings_hours", hours)
    # Measured AC battery flows: only real, confirmed flow earns credit.  This covers charging and
    # discharging (a clamped or ramp-limited write counts what was measured, not what was asked)
    # and prices each leg with the price series in force at flow time.  The energies survive a gap;
    # the two priced values stay unknown if any measured interval could not be priced.
    if battery_kw is not None:
        charged = max(0.0, float(battery_kw)) * hours
        discharged = max(0.0, -float(battery_kw)) * hours
        add("battery_hours", hours)
        add("charged_kwh", charged)
        add("discharged_kwh", discharged)
        add_known("charge_cost_eur", buy * charged if price_buy is not None else None)
        add_known("discharge_value_eur", sell * discharged if price_sell is not None else None)
        add("battery_cycles", discharged / capacity_kwh if capacity_kwh > 0 else 0.0)
    cover("grid_measured", grid_kw is not None)
    cover("load_measured", load_kw is not None)
    cover("pv_measured", pv_kw is not None)
    cover("battery_measured", battery_kw is not None)
    # Unknown is still not zero: a day that never priced an interval reports None for its baseline
    # and saving, and a day that never measured the battery reports None for its flows.  This keys
    # off "nothing was ever accumulated" rather than the hour counters, so a stored row from a
    # build without coverage hours keeps its value when a later interval happens to be missing.
    if row.get("baseline_cost_eur") is None:
        row["baseline_cost_eur"] = None
        row["savings_eur"] = None
    if row.get("charged_kwh") is None:
        for key in (
            "charged_kwh",
            "discharged_kwh",
            "charge_cost_eur",
            "discharge_value_eur",
            "battery_cycles",
        ):
            row[key] = None
    row["updated_at"] = to_iso(now)
    return row


@dataclass(frozen=True)
class AccuracySample:
    ts: datetime
    forecast: float
    actual: float


def accuracy_from_samples(
    kind: str, samples: Iterable[AccuracySample], days: int, now: datetime
) -> AccuracyReport:
    """MAPE/bias/MAE for one kind over the last ``days`` (SPEC 10 /api/v1/accuracy)."""
    cutoff = now - timedelta(days=max(1, int(days)))
    usable = [s for s in samples if s.ts >= cutoff]
    report = AccuracyReport(kind=kind, days=int(days), samples=len(usable))
    if not usable:
        return report
    errors = [s.actual - s.forecast for s in usable]
    abs_errors = [abs(e) for e in errors]
    denominators = [abs(s.actual) for s in usable if abs(s.actual) > 1e-6]
    report.mae = sum(abs_errors) / len(abs_errors)
    if denominators:
        report.mape_pct = (
            sum(abs(e) / abs(s.actual) * 100.0 for e, s in zip(errors, usable) if abs(s.actual) > 1e-6)
            / len(denominators)
        )
    mean_actual = sum(s.actual for s in usable) / len(usable)
    if abs(mean_actual) > 1e-6:
        report.bias_pct = sum(errors) / len(errors) / abs(mean_actual) * 100.0

    by_day: dict[date, list[AccuracySample]] = {}
    for sample in usable:
        by_day.setdefault(sample.ts.date(), []).append(sample)
    for day in sorted(by_day):
        group = by_day[day]
        day_abs = sum(abs(s.actual - s.forecast) for s in group)
        day_den = [abs(s.actual) for s in group if abs(s.actual) > 1e-6]
        mape = (day_abs / len(day_den) * 100.0) if day_den else None
        mean = sum(s.actual for s in group) / len(group)
        bias = (
            sum(s.actual - s.forecast for s in group) / len(group) / abs(mean) * 100.0
            if abs(mean) > 1e-6
            else None
        )
        report.series.append(
            {"day": day.isoformat(), "mape_pct": mape, "bias_pct": bias, "samples": len(group)}
        )
    return report


def update_bias_factors(
    factors: Mapping[str, Any],
    samples: Iterable[AccuracySample],
    *,
    alpha: float = 0.2,
    low: float = 0.3,
    high: float = 1.8,
) -> dict[str, dict[str, Any]]:
    """EWMA PV correction per hour-of-day bucket (SPEC 7.1)."""
    out: dict[str, dict[str, Any]] = {str(k): dict(v) for k, v in factors.items()}
    for sample in sorted(samples, key=lambda s: s.ts):
        if sample.forecast <= 1e-6 or sample.actual < 0:
            continue
        ratio = clamp(sample.actual / sample.forecast, low, high)
        key = str(sample.ts.hour)
        current = float(out.get(key, {}).get("factor", 1.0))
        updated = clamp(current + alpha * (ratio - current), low, high)
        out[key] = {
            "factor": updated,
            "updated_at": to_iso(sample.ts),
            "samples": int(out.get(key, {}).get("samples") or 0) + 1,
        }
    return out


def bias_factor_for(
    factors: Mapping[str, Any], when: datetime, now: datetime, *, hold_days: float = 14.0
) -> float:
    """Learned factor for an hour bucket, decaying to 1.0 after ``hold_days`` (SPEC 7.1)."""
    entry = factors.get(str(when.hour))
    if not entry:
        return 1.0
    factor = clamp(float(entry.get("factor", 1.0)), 0.0, 5.0)
    updated = from_iso(entry.get("updated_at"))
    if updated is None:
        return factor
    age_days = (now - updated).total_seconds() / 86400.0
    if age_days <= hold_days:
        return factor
    remaining = 1.0 - (age_days - hold_days) / hold_days
    if remaining <= 0:
        return 1.0
    return 1.0 + (factor - 1.0) * remaining


def apply_pv_bias(
    forecast: SolarForecast, factors: Mapping[str, Any], now: datetime
) -> SolarForecast:
    """Return a copy of ``forecast`` with the learned per-hour correction applied (SPEC 7.1)."""
    if not forecast.slots:
        return forecast
    applied = [bias_factor_for(factors, slot.t, now) for slot in forecast.slots]
    slots = [
        replace(slot, pv_kw=max(0.0, slot.pv_kw * factor))
        for slot, factor in zip(forecast.slots, applied)
    ]
    return replace(
        forecast,
        slots=slots,
        correction_applied=sum(applied) / len(applied),
    )


def price_windows(
    slots: Sequence[PriceSlot], now: datetime
) -> tuple[tuple[datetime, datetime] | None, tuple[datetime, datetime] | None]:
    """Next cheap window and next expensive run in the forecast (SPEC 12 attributes)."""
    # The slot running right now counts as future too.
    future = sorted(
        (slot for slot in slots if slot.t + timedelta(minutes=15) > now), key=lambda s: s.t
    )
    if not future:
        return None, None
    prices = [s.buy for s in future]
    cheap_threshold = _percentile(prices, 0.25)
    expensive_threshold = _percentile(prices, 0.75)

    cheap: tuple[datetime, datetime] | None = None
    for index, slot in enumerate(future):
        if slot.buy <= cheap_threshold:
            end = slot.t + timedelta(minutes=15)
            cursor = index + 1
            while cursor < len(future) and future[cursor].buy <= cheap_threshold:
                end = future[cursor].t + timedelta(minutes=15)
                cursor += 1
            cheap = (slot.t, end)
            break

    expensive: tuple[datetime, datetime] | None = None
    peak = max(range(len(future)), key=lambda i: (future[i].buy, -i))
    start = future[peak].t
    end = start + timedelta(minutes=15)
    cursor = peak + 1
    while cursor < len(future) and future[cursor].buy >= expensive_threshold:
        end = future[cursor].t + timedelta(minutes=15)
        cursor += 1
    if end - start < timedelta(hours=1):
        end = start + timedelta(hours=1)
    expensive = (start, end)
    return cheap, expensive


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Percentage of values at or below ``value`` (0..100)."""
    if not values:
        return 0.0
    below = sum(1 for candidate in values if candidate <= value)
    return clamp(below / len(values) * 100.0, 0.0, 100.0)


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = clamp(fraction, 0.0, 1.0) * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (index - low) * (ordered[high] - ordered[low])


# ---------------------------------------------------------------------------------------------
# Measured actuals onto the plan grid (SPEC 10, additive: GET /api/v1/plan/execution)
# ---------------------------------------------------------------------------------------------

#: Bucket length of the persisted ``actuals`` rows (``engine.ACTUALS_MINUTES``).  Passed in by the
#: caller rather than imported, so this module keeps no dependency on the engine.
ACTUALS_BUCKET_MINUTES = 5.0

#: Columns aggregated per slot.  ``signed`` fields are averaged, the rest are turned into energy.
_SIGNED_ACTUALS = ("pv_kw", "load_kw", "grid_kw", "battery_kw")


def measured_per_slot(
    slot_times: Sequence[datetime],
    slot_minutes: float,
    rows: Iterable[Mapping[str, Any]],
    *,
    bucket_minutes: float = ACTUALS_BUCKET_MINUTES,
    now: datetime | None = None,
) -> list[dict[str, Any] | None]:
    """5-minute ``actuals`` rows aggregated onto a plan's slot grid, one entry per slot time.

    ``None`` for a slot with no measurement (a future slot reports nothing, never a zero).  Rows are
    deduplicated by ``ts`` with the last row winning -- the store's ``ON CONFLICT(ts) DO UPDATE``
    keeps one row per bucket, while the in-memory ring keeps every tick.  Every surviving row stands
    for one ``bucket_minutes`` of measured time, so ``coverage_pct`` states how much of the slot was
    observed instead of counting the gaps as zero power.

    Signs follow the engine's actuals convention: ``battery_kw`` and ``plan_power_w`` positive =
    charging, ``grid_kw`` positive = importing.  Energy is returned as positive magnitudes
    (``charged_kwh``/``discharged_kwh``/``import_kwh``/``export_kwh``).
    """
    step = float(slot_minutes)
    empty: list[dict[str, Any] | None] = [None] * len(slot_times)
    if step <= 0 or not slot_times:
        return empty
    hours = float(bucket_minutes) / 60.0
    latest: dict[datetime, Mapping[str, Any]] = {}
    for row in rows:
        try:
            stamp = from_iso(row.get("ts"))
        except ValueError:  # a corrupt row must not take the whole panel down
            continue
        if stamp is None:
            continue
        if now is not None and stamp > now:
            continue
        latest[stamp] = row  # last write wins
    if not latest:
        return empty
    ordered = sorted(latest.items())
    out: list[dict[str, Any] | None] = []
    for start in slot_times:
        end = start + timedelta(minutes=step)
        selected = [row for stamp, row in ordered if start <= stamp < end]
        if not selected:
            out.append(None)
            continue
        entry: dict[str, Any] = {
            "samples": len(selected),
            "coverage_pct": min(100.0, 100.0 * len(selected) * float(bucket_minutes) / step),
        }
        for field in _SIGNED_ACTUALS:
            entry[field] = _mean([row.get(field) for row in selected])
        power = _mean([row.get("plan_power_w") for row in selected])
        entry["planned_kw"] = None if power is None else power / 1000.0
        entry["measured_kw"] = entry["battery_kw"]
        entry["import_kwh"] = _positive_energy(selected, "grid_kw", hours)
        entry["export_kwh"] = _negative_energy(selected, "grid_kw", hours)
        entry["charged_kwh"] = _positive_energy(selected, "battery_kw", hours)
        entry["discharged_kwh"] = _negative_energy(selected, "battery_kw", hours)
        soc = [row.get("soc_kwh") for row in selected if row.get("soc_kwh") is not None]
        entry["soc_kwh_start"] = float(soc[0]) if soc else None
        entry["soc_kwh_end"] = float(soc[-1]) if soc else None
        out.append(entry)
    return out


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def _positive_energy(rows: Sequence[Mapping[str, Any]], field: str, hours: float) -> float:
    return sum(max(0.0, float(row[field])) for row in rows if row.get(field) is not None) * hours


def _negative_energy(rows: Sequence[Mapping[str, Any]], field: str, hours: float) -> float:
    return sum(max(0.0, -float(row[field])) for row in rows if row.get(field) is not None) * hours
