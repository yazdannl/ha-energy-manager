"""Cost basis of the energy in the pack (SPEC.md section 5, additive 0.18.0).

The optimiser only knows a *forward* rule: what a sale has to earn back from here on
(``dp._round_trip_blocks_arbitrage`` plus ``BatteryConfig.break_even_sell_eur_per_kwh``).  It has no
memory of what the energy already in the pack cost.  This module keeps that memory: a per-battery
ledger of the **weighted average price paid for the AC energy currently held**, derived from what
the add-on really did and what the battery really measured.

Basis unit is EUR per kWh charged *into* the pack (AC).  That makes the sell floor identical to the
frozen break-even helper with ``buy := basis``:
``floor = battery.break_even_sell_eur_per_kwh(basis) + margin`` (SPEC 5).

Intervals are **event driven, never timed buckets**.  A slice opens on an applied change the add-on
made for that battery -- a ``store.dispatch_log`` row for ``mode == auto`` that was really applied
(``dry_run`` false, ``result`` ``ok`` or ``skipped``) and whose ``applied_w`` differs from the last
booked command by more than :data:`APPLIED_CHANGE_TOLERANCE_W`.  It closes at the next such change;
the still-open tail is booked up to the newest settled measurement sample (0.18.1).  One row books
at most one slice, and the processed span's end is stored as ``last_ts``, so re-running an update
books nothing twice.

The energy booked per slice is **measured**, not the commanded setpoint: the persisted
``battery_power:<id>`` rows (or the ``battery_charge``/``battery_discharge`` pair) are integrated
over the *settled sample grid* (0.18.1), piecewise-linearly between samples, so uncommanded device
flow, ramps and a vendor manager moving the pack are all accounted.  A data gap longer than
:data:`MAX_GAP_MINUTES` is never bridged: only the covered part is booked.  A command interval that
is settled but has no covered sample span is counted in ``skipped_no_power``, never price-guessed.

Energy follows the samples, price follows the commands (0.18.1).  The covered sample spans are
split at the applied command changes and the price-slot boundaries they cross, each part is
attributed to the command in force, and the parts belonging to one command interval accumulate
into its single journal entry -- so a command whose whole span falls between two samples still
books its share.  ``last_ts`` is the end of the last processed sample span, never a command
timestamp, and nothing beyond the newest sample is booked: a ramp the next sample reveals is never
swallowed by a flat extrapolation at an update boundary.

After booking, the *quantity* is reconciled with the pack's own measured content (0.18.1):
``E_phys = capacity_kwh * (soc - floor_soc_pct) / 100 / eta_charge`` (clamped to ``[0, max_energy]``)
from the last SoC sample at or before the booking end (a stale/missing one skips the step and is
counted in ``skipped_no_soc``).  ``floor_soc_pct = max(battery.min_soc_pct,
optimizer.reserve_soc_pct)`` is the level the plan never discharges below, so the ledger prices only
the energy above it -- the reserve band is never sellable and its cost must not set the floor
(0.21.0).  The difference is applied to the energy and never to the basis
(the basis stays the tracked average price of the energy we priced), the signed cumulative
correction is kept as ``reconciled_kwh`` plus ``reconciled_last_kwh``, and a single correction
larger than half the pack capacity is treated as unattributable: the ledger resets and
``reconciled_resets`` counts it.

Pricing follows the plan slot the energy was charged in (the persisted plans, one slot's
``buy``/``sell``/``reason`` per slot start; the newest plan wins for a slot), splitting the energy
at price-slot boundaries when a slice spans one:
``grid_part = min(charge_ac, import_ac)`` at the slot's ``buy`` (``import_ac`` from the measured
meter import) and ``pv_part = charge_ac - grid_part`` at the slot's ``sell`` (the export forgone --
the DP's own salvage convention).  When the slot's ``sell`` is missing it falls back to ``buy``;
when both are missing the sub-slice is skipped and counted.  When the meter reading is missing, the
plan slot's own reason is the fallback attribution (``pv_surplus_charge`` means PV, anything else
means grid).  A bounded audit journal keeps the last :data:`MAX_JOURNAL` booked slices.

The ledger is derived data in the ``learning_profiles`` store (``kind = PROFILE_KIND``), never user
settings.  Nothing here may crash or block: no store, no rows, an unknown battery or a missing price
degrades to ``basis=None`` (the floor is then inactive) and the control loop carries on.

A ledger that cannot advance must never do so *silently* (0.20.3): when the newest usable power
sample is older than :data:`STALL_TOLERANCE`, ``book_slices`` counts ``skipped_no_samples``, sets
``no_samples_since``/``stale`` in the stored payload (visible in ``/status.cost_basis`` and the HA
sensors) and logs one warning per stall, cleared when booking resumes.  A stall is a *freshness*
failure, never an empty booking window (0.20.5): an update that lands inside the current sample bin
finds the ledger already settled up to the newest sample, and an empty booking window there is
healthy, not a stall.  To recover measured energy that was never booked, an update whose window
has no usable measurement power point falls back to the fleet-aggregate ``actuals`` rows -- but only
when exactly one battery is enabled, because the aggregate cannot be split across a fleet without
guessing; with several batteries the stall is reported instead.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import BatteryConfig, Mode, Reason, from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

__all__ = [
    "APPLIED_CHANGE_TOLERANCE_W",
    "DISPATCH_LIMIT",
    "JOURNAL_EXPOSED",
    "LOOKBACK_DAYS",
    "MAX_GAP_MINUTES",
    "MAX_JOURNAL",
    "PLAN_LIMIT",
    "PROFILE_KIND",
    "RECONCILE_RESET_FRACTION",
    "RESET_SOC_MARGIN_PCT",
    "SAMPLE_MINUTES",
    "STALL_TOLERANCE",
    "STATE_VERSION",
    "PriceSlot",
    "PriceTimeline",
    "Series",
    "basis_status",
    "book_slices",
    "fleet_basis_eur_per_kwh",
    "fleet_sell_floor",
    "load_basis",
    "measurement_roles",
    "measurement_series",
    "update_basis",
]

#: Store ``learning_profiles`` kind for the per-battery ledger; the key is the battery id.
PROFILE_KIND = "battery_cost_basis"
STATE_VERSION = 1
#: The sampler's bucket; a longer gap means at most one missing sample.
SAMPLE_MINUTES = 5.0
#: A whole sample may be missing before a measurement gap is treated as unknown.
MAX_GAP_SAMPLES = 1.0
MAX_GAP_MINUTES = SAMPLE_MINUTES * (1.0 + MAX_GAP_SAMPLES)
#: A *stall* is a freshness failure: the ledger cannot follow the pack when the newest usable
#: sample is older than this.  ``MAX_GAP_MINUTES`` is the booking gap tolerance (how wide a hole
#: the integral may bridge); the stall tolerance sits one whole sample bucket above it -- 15 min =
#: the 10-min gap bound + one missing 5-min bucket -- so a healthy sampler, or one that merely
#: dropped a single bucket and is due its next sample, can never trip it, while a real outage is
#: caught well inside one extra bucket.
STALL_TOLERANCE = timedelta(minutes=3 * SAMPLE_MINUTES)
#: A command change smaller than this does not open a new slice (grid-follow micro corrections).
APPLIED_CHANGE_TOLERANCE_W = 25.0
#: At or below ``max(min_soc_pct, reserve_soc_pct) + this`` the pack holds nothing sellable: the
#: ledger resets.
RESET_SOC_MARGIN_PCT = 1.0
#: A single SoC reconciliation correction beyond this fraction of the pack capacity means the
#: ledger can no longer be attributed to the measured grid: it resets instead (0.18.1).
RECONCILE_RESET_FRACTION = 0.5
#: A first run (or a gap between updates) never looks further back than this.
LOOKBACK_DAYS = 7.0
#: Audit journal: the last N booked slices are kept (and at most ``JOURNAL_EXPOSED`` exposed).
MAX_JOURNAL = 200
JOURNAL_EXPOSED = 5
#: Bounds for the persisted-row reads (the ledger never blocks on an unbounded query).
DISPATCH_LIMIT = 5000
PLAN_LIMIT = 800
PLAN_SCAN_MIN = 12

_APPLIED_RESULTS = ("ok", "skipped")
_POWER_ROLES = ("battery_power", "battery_charge", "battery_discharge")
_GRID_ROLES = ("grid_power", "grid_import", "grid_export")
_SOC_ROLE = "battery_soc"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse(value: Any) -> datetime | None:
    """A parsed timestamp, or ``None`` for anything unparsable (never a crash on stored data)."""
    try:
        return from_iso(value)
    except (TypeError, ValueError):
        return None


def measurement_roles(battery_id: str) -> list[str]:
    """The persisted roles the ledger reads for one battery (grid is instance-wide, unsuffixed)."""
    return [f"{base}:{battery_id}" for base in (*_POWER_ROLES, _SOC_ROLE)] + list(_GRID_ROLES)


@dataclass(frozen=True)
class Series:
    """Measured points for one battery: ``(datetime, value)``, ascending, battery AC W signed."""

    power: tuple[tuple[datetime, float], ...] = ()
    grid: tuple[tuple[datetime, float], ...] = ()
    soc: tuple[tuple[datetime, float], ...] = ()


def measurement_series(rows: Iterable[Mapping[str, Any]], battery_id: str) -> Series:
    """Collapse persisted measurement rows into the three series the ledger integrates.

    The signed ``battery_power:<id>`` role wins, else the measured AC pair
    ``battery_charge:<id> - battery_discharge:<id>`` (a missing side counts as 0), the same
    convention as :mod:`app.sources.measure` and :mod:`app.capacity`.  Grid is instance-wide
    ``grid_power`` (positive = import), else ``grid_import - grid_export``; SoC is per battery.
    A row with an unparsable timestamp or a non-finite value is skipped, never guessed.
    """
    buckets: dict[datetime, dict[str, float]] = {}
    for row in rows:
        role = str(row.get("entity_role") or row.get("role") or "")
        value = _finite(row.get("value"))
        if value is None:
            continue
        if role in _GRID_ROLES:
            base = role
        elif role.endswith(f":{battery_id}"):
            base = role.split(":", 1)[0]
            if base not in _POWER_ROLES and base != _SOC_ROLE:
                continue
        else:
            continue
        moment = _parse(row.get("ts"))
        if moment is None:
            continue
        buckets.setdefault(moment, {})[base] = value
    power: list[tuple[datetime, float]] = []
    grid: list[tuple[datetime, float]] = []
    soc: list[tuple[datetime, float]] = []
    for moment in sorted(buckets):
        values = buckets[moment]
        if "battery_power" in values:
            battery_w: float | None = values["battery_power"]
        elif "battery_charge" in values or "battery_discharge" in values:
            battery_w = values.get("battery_charge", 0.0) - values.get("battery_discharge", 0.0)
        else:
            battery_w = None
        if battery_w is not None and math.isfinite(battery_w):
            power.append((moment, battery_w))
        if "grid_power" in values:
            grid_w: float | None = values["grid_power"]
        elif "grid_import" in values or "grid_export" in values:
            grid_w = values.get("grid_import", 0.0) - values.get("grid_export", 0.0)
        else:
            grid_w = None
        if grid_w is not None and math.isfinite(grid_w):
            grid.append((moment, grid_w))
        if _SOC_ROLE in values:
            soc.append((moment, values[_SOC_ROLE]))
    return Series(tuple(power), tuple(grid), tuple(soc))


def _has_window_power(series: Series, start: datetime) -> bool:
    """True when the battery series holds a usable power point at or after ``start``.

    ``measurement_series`` returns its points ascending, so the newest point answers it: a series
    whose last battery sample predates the window has nothing to integrate there.
    """
    if not series.power:
        return False
    return max(t for t, _ in series.power) >= start


def _actuals_series(store: Any, battery: BatteryConfig, start: datetime, end: datetime) -> Series:
    """The single-battery fallback series from the fleet-aggregate ``actuals`` rows (0.20.3).

    ``actuals`` carries one aggregated row per :data:`SAMPLE_MINUTES` bucket: ``battery_kw`` the sum
    of the confirmed per-battery flows, ``soc_kwh`` the sum of their stored energy and ``grid_kw``
    the meter.  With exactly one enabled battery that aggregate *is* that battery's measurement, so
    it can stand in for missing ``measurements`` rows without inventing a split; the caller may not
    use it with several batteries.  SoC is reconstructed as a percentage from ``capacity_kwh``.
    A row without a finite value is skipped, and the series never extends beyond its newest row.
    """
    reader = getattr(store, "actuals_range", None)
    if not callable(reader):
        return Series()
    try:
        rows = reader(start - timedelta(hours=1), end) or []
    except Exception:  # noqa: BLE001 - no history is not an error
        return Series()
    capacity = float(battery.capacity_kwh or 0.0)
    power: list[tuple[datetime, float]] = []
    grid: list[tuple[datetime, float]] = []
    soc: list[tuple[datetime, float]] = []
    for row in rows:
        moment = _parse(row.get("ts"))
        if moment is None:
            continue
        battery_kw = _finite(row.get("battery_kw"))
        if battery_kw is not None:
            power.append((moment, battery_kw * 1000.0))
        grid_kw = _finite(row.get("grid_kw"))
        if grid_kw is not None:
            grid.append((moment, grid_kw * 1000.0))
        soc_kwh = _finite(row.get("soc_kwh"))
        if soc_kwh is not None and capacity > 0.0:
            soc.append((moment, max(0.0, soc_kwh) / capacity * 100.0))
    return Series(tuple(power), tuple(grid), tuple(soc))


def _linear_clamped_area(v0: float, v1: float, length: float) -> float:
    """Integral of ``max(0, linear(v0 -> v1))`` over ``length`` (same units as ``v * length``)."""
    if v0 >= 0.0 and v1 >= 0.0:
        return (v0 + v1) / 2.0 * length
    if v0 <= 0.0 and v1 <= 0.0:
        return 0.0
    if v0 < 0.0 < v1:
        return (v1 * v1) / (2.0 * (v1 - v0)) * length
    return (v0 * v0) / (2.0 * (v0 - v1)) * length


class _Points:
    """A sorted measured series with bisect-backed integration, so booking stays O(log n)."""

    __slots__ = ("_points", "_times")

    def __init__(self, points: Sequence[tuple[datetime, float]]) -> None:
        ordered = tuple(sorted(((t, float(v)) for t, v in points), key=lambda point: point[0]))
        self._points = ordered
        self._times = tuple(point[0] for point in ordered)

    def __bool__(self) -> bool:
        return bool(self._points)

    def __len__(self) -> int:
        return len(self._points)

    @property
    def last_time(self) -> datetime | None:
        """The newest sample's timestamp, or ``None`` when nothing was measured."""
        return self._times[-1] if self._times else None

    def value_at(self, moment: datetime) -> float:
        """Piecewise-linear value with flat extrapolation either side of the sampled range."""
        index = bisect_right(self._times, moment) - 1
        if index < 0:
            return self._points[0][1]
        if index >= len(self._points) - 1:
            return self._points[-1][1]
        t0, v0 = self._points[index]
        t1, v1 = self._points[index + 1]
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return v1
        return v0 + (v1 - v0) * ((moment - t0).total_seconds() / span)

    def soc_at(self, moment: datetime) -> float | None:
        """The measured value in force at ``moment`` (last at or before, else the first after)."""
        if not self._points:
            return None
        gap = timedelta(minutes=MAX_GAP_MINUTES)
        index = bisect_right(self._times, moment) - 1
        if index >= 0:
            return self._points[index][1] if moment - self._points[index][0] <= gap else None
        return self._points[0][1] if self._points[0][0] - moment <= gap else None

    def value_at_or_before(self, moment: datetime) -> float | None:
        """The last measured value at or before ``moment``, or ``None`` when none is fresh enough.

        Unlike :meth:`soc_at` a sample *after* ``moment`` never counts: reconciliation must use
        what the pack measured up to the booking end, never a later value.
        """
        if not self._points:
            return None
        index = bisect_right(self._times, moment) - 1
        if index < 0:
            return None
        last = self._points[index]
        return last[1] if moment - last[0] <= timedelta(minutes=MAX_GAP_MINUTES) else None

    def min_between(self, start: datetime, end: datetime) -> float | None:
        """Lowest measured value in ``(start, end]``, or ``None`` when nothing was measured."""
        if not self._points:
            return None
        i0 = bisect_right(self._times, start)
        i1 = bisect_right(self._times, end)
        window = self._points[i0:i1]
        return min(value for _t, value in window) if window else None

    def covered_ranges(self, a: datetime, b: datetime) -> list[tuple[datetime, datetime]]:
        """Sub-ranges of ``[a, b)`` where interpolation is trustworthy (no gap beyond the bound).

        The value is known at every sample, between two samples closer than the bound, and for the
        bound either side of the outermost sample; everything else is a hole.  A hole is removed,
        never bridged (SPEC 5).
        """
        if not self._points or b <= a:
            return []
        gap = timedelta(minutes=MAX_GAP_MINUTES)
        i0 = bisect_left(self._times, a - gap)
        i1 = bisect_right(self._times, b + gap)
        times = self._times[i0:i1]
        if not times:
            return []
        raw: list[tuple[datetime, datetime]] = [(times[0] - gap, times[0])]
        raw.extend((t0, t1) for t0, t1 in zip(times, times[1:]) if t1 - t0 <= gap)
        raw.append((times[-1], times[-1] + gap))
        merged: list[tuple[datetime, datetime]] = []
        for start, end in raw:
            start, end = max(start, a), min(end, b)
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def integrate_kwh(self, a: datetime, b: datetime, *, sign: float = 1.0) -> tuple[float, float]:
        """``(kWh, covered_hours)`` of ``max(0, sign * value)`` over ``[a, b)``.

        The integrand is the piecewise-linear curve *through the measured samples*, so every
        sample inside the range is a breakpoint: a slice must never be reduced to a straight line
        between its own endpoints (that would turn a step at the first sample into a full-slice
        ramp).
        """
        total_wh = 0.0
        covered_hours = 0.0
        for start, end in self.covered_ranges(a, b):
            covered_hours += (end - start).total_seconds() / 3600.0
            inner = self._times[bisect_right(self._times, start) : bisect_left(self._times, end)]
            stamps = (start, *inner, end)
            previous = stamps[0]
            v_previous = self.value_at(previous) * sign
            for moment in stamps[1:]:
                v_next = self.value_at(moment) * sign
                hours = (moment - previous).total_seconds() / 3600.0
                total_wh += _linear_clamped_area(v_previous, v_next, hours)
                previous, v_previous = moment, v_next
        return total_wh / 1000.0, covered_hours


@dataclass(frozen=True)
class PriceSlot:
    """One plan slot's price and reason, as persisted with the plan that steered that slot."""

    t: datetime
    minutes: int
    buy: float | None
    sell: float | None
    reason: str = ""

    @property
    def ends(self) -> datetime:
        return self.t + timedelta(minutes=max(1, int(self.minutes)))


class PriceTimeline:
    """``moment -> (buy, sell, plan reason)`` from the persisted plans, newest plan winning."""

    def __init__(self, slots: Sequence[PriceSlot] = ()) -> None:
        ordered = sorted(slots, key=lambda slot: slot.t)
        self._slots: tuple[PriceSlot, ...] = tuple(ordered)
        self._starts: tuple[datetime, ...] = tuple(slot.t for slot in ordered)

    def __bool__(self) -> bool:
        return bool(self._slots)

    @classmethod
    def from_store(
        cls, store: Any, start: datetime, end: datetime, *, limit: int | None = None
    ) -> "PriceTimeline":
        """Read the persisted plans covering ``[start, end]``; no store is an empty timeline."""
        lister = getattr(store, "list_plans", None)
        loader = getattr(store, "load_plan", None)
        if not callable(lister) or not callable(loader):
            return cls()
        window_hours = max(0.0, (end - start).total_seconds() / 3600.0)
        scan = int(limit) if limit else min(
            PLAN_LIMIT, max(PLAN_SCAN_MIN, int(window_hours / 0.25) + 2)
        )
        try:
            plan_ids = list(lister(limit=scan) or [])
        except Exception:  # noqa: BLE001 - no plans is "no price", never a crash
            return cls()
        by_start: dict[datetime, PriceSlot] = {}
        for plan_id in plan_ids:  # newest first: the first plan for a slot wins
            try:
                plan = loader(plan_id)
            except Exception:  # noqa: BLE001
                continue
            if plan is None:
                continue
            horizon_end = getattr(plan, "horizon_end", None)
            if horizon_end is not None and horizon_end <= start:
                # Older plans only ever end earlier, so the rest of the history is out of range.
                break
            minutes = int(getattr(plan, "slot_minutes", 15) or 15)
            for slot in getattr(plan, "slots", ()) or ():
                moment = getattr(slot, "t", None)
                if moment is None or moment < start - timedelta(hours=2) or moment > end:
                    continue
                if moment in by_start:
                    continue
                by_start[moment] = PriceSlot(
                    t=moment,
                    minutes=minutes,
                    buy=_finite(getattr(slot, "buy", None)),
                    sell=_finite(getattr(slot, "sell", None)),
                    reason=str(getattr(slot, "reason", "") or ""),
                )
        return cls(list(by_start.values()))

    def at(self, moment: datetime) -> PriceSlot | None:
        """The slot covering ``moment`` exactly, else ``None`` (never a neighbouring price)."""
        if not self._starts:
            return None
        index = bisect_right(self._starts, moment) - 1
        if index < 0:
            return None
        slot = self._slots[index]
        return slot if moment < slot.ends else None

    def starts_between(self, a: datetime, b: datetime) -> list[datetime]:
        """Slot starts strictly inside ``(a, b)``: the price boundaries a slice must be split at."""
        if not self._starts:
            return []
        i0 = bisect_right(self._starts, a)
        i1 = bisect_left(self._starts, b)
        return list(self._starts[i0:i1])


def _effective(pair: tuple[float | None, float | None]) -> tuple[float | None, float | None]:
    """``(buy, sell)`` with the documented fallbacks; ``(None, None)`` when neither resolved."""
    buy, sell = pair
    if buy is None and sell is None:
        return None, None
    if buy is None:
        return sell, sell
    return buy, buy if sell is None else sell


def _battery_floor(
    battery: BatteryConfig,
    basis: float,
    wear_eur_per_kwh: float | None,
    margin_eur_per_kwh: float,
) -> float | None:
    """The frozen break-even helper with ``buy := basis``, plus the configured margin."""
    break_even = battery.break_even_sell_eur_per_kwh(basis, wear_eur_per_kwh)
    if break_even is None:
        return None
    return break_even + max(0.0, float(margin_eur_per_kwh or 0.0))


def book_slices(
    state: Mapping[str, Any] | None,
    *,
    battery: BatteryConfig,
    series: Series,
    timeline: PriceTimeline | None = None,
    dispatch_rows: Sequence[Mapping[str, Any]] = (),
    now: datetime,
    reset: bool = False,
    source: str = "measurements",
    reserve_soc_pct: float = 0.0,
) -> dict[str, Any]:
    """Advance the ledger over the settled measured sample grid since ``state['last_ts']``.

    Pure: no store, no clock.  Returns the new JSON-safe payload.  ``reset`` starts from an empty
    ledger (a first run with no usable history, or a gap beyond the lookback).

    Energy follows the samples and price follows the commands (0.18.1): the covered sample spans
    are integrated (piecewise-linear through every sample), split at the applied command changes
    and price-slot boundaries they cross, and attributed to the command in force.  ``last_ts`` is
    the end of the last processed sample span, never a command timestamp, and nothing beyond the
    newest sample is booked, so a ramp the next sample reveals is never swallowed by a flat
    extrapolation at an update boundary.  After booking the quantity is reconciled with the pack's
    measured SoC content, never touching the basis (SPEC 5, 0.18.1).
    """
    stored = dict(state) if isinstance(state, Mapping) else {}
    timeline = timeline if timeline is not None else PriceTimeline()
    power = _Points(series.power)
    grid = _Points(series.grid)
    soc = _Points(series.soc)
    basis = _finite(stored.get("basis_eur_per_kwh"))
    energy = max(0.0, _finite(stored.get("energy_kwh")) or 0.0)
    if basis is None:
        energy = 0.0
    last_ts = _parse(stored.get("last_ts"))
    last_applied = _finite(stored.get("last_applied_w"))
    window_start = _parse(stored.get("window_start"))
    journal: list[dict[str, Any]] = [
        dict(entry) for entry in (stored.get("journal") or []) if isinstance(entry, Mapping)
    ][-MAX_JOURNAL:]
    intervals = _count(stored.get("intervals"))
    skipped_no_price = _count(stored.get("skipped_no_price"))
    skipped_no_power = _count(stored.get("skipped_no_power"))
    skipped_no_grid = _count(stored.get("skipped_no_grid"))
    reconciled = _finite(stored.get("reconciled_kwh")) or 0.0
    reconciled_last = _finite(stored.get("reconciled_last_kwh"))
    reconciled_resets = _count(stored.get("reconciled_resets"))
    skipped_no_soc = _count(stored.get("skipped_no_soc"))
    skipped_no_samples = _count(stored.get("skipped_no_samples"))
    no_samples_since = _parse(stored.get("no_samples_since"))
    if reset or (last_ts is not None and last_ts > now):
        basis, energy, last_ts, window_start = None, 0.0, None, None
        reconciled, reconciled_last = 0.0, None

    round_trip = float(battery.round_trip_efficiency)
    max_energy = None
    if battery.capacity_kwh > 0 and battery.eta_charge > 0:
        max_energy = battery.capacity_kwh / battery.eta_charge
    # The level the plan never discharges below: ``optimizer.reserve_soc_pct`` is a per-battery floor
    # above ``min_soc_pct`` (SPEC 3).  The ledger prices only the energy above it, because the
    # reserve band is never sellable and its cost must not set the floor (SPEC 5, 0.21.0).
    floor_soc = max(float(battery.min_soc_pct), float(reserve_soc_pct or 0.0))
    reset_soc = floor_soc + RESET_SOC_MARGIN_PCT

    # --- one boundary per applied change the add-on really made -------------------------------
    boundaries: list[tuple[datetime, float, str, str, Any]] = []
    previous_applied = last_applied
    for row in dispatch_rows:
        if int(row.get("dry_run") or 0) != 0:
            continue
        if str(row.get("mode") or "") != Mode.AUTO.value:
            continue
        if str(row.get("result") or "") not in _APPLIED_RESULTS:
            continue
        moment = _parse(row.get("ts"))
        if moment is None or moment > now:
            continue
        if last_ts is not None and moment < last_ts:
            continue
        applied = _finite(row.get("applied_w"))
        applied = 0.0 if applied is None else applied
        if (
            previous_applied is not None
            and abs(applied - previous_applied) <= APPLIED_CHANGE_TOLERANCE_W
        ):
            continue
        boundaries.append(
            (moment, applied, str(row.get("code") or ""), str(row.get("mode") or ""), row.get("id"))
        )
        previous_applied = applied
    boundaries.sort(key=lambda item: item[0])

    slices: list[tuple[datetime, datetime, float, str, str, Any]] = []
    cursor = last_ts
    command: tuple[float, str, str, Any] = (
        last_applied if last_applied is not None else 0.0,
        str(stored.get("last_reason") or ""),
        str(stored.get("last_mode") or ""),
        stored.get("last_dispatch_id"),
    )
    for moment, applied, code, mode, row_id in boundaries:
        if cursor is None:
            cursor, command = moment, (applied, code, mode, row_id)
            continue
        if moment <= cursor:
            command = (applied, code, mode, row_id)
            continue
        slices.append((cursor, moment, command[0], command[1], command[2], command[3]))
        cursor, command = moment, (applied, code, mode, row_id)
    if cursor is not None and now > cursor:
        slices.append((cursor, now, command[0], command[1], command[2], command[3]))

    booked = 0
    coverage_index = 0
    processed_end = last_ts
    # The settled sample grid: the covered ranges of the measured series, clipped to the newest
    # sample.  Booking never runs past it, so a partial command interval waits for its next sample
    # instead of being extrapolated flat (0.18.1).
    settled_end = power.last_time
    if settled_end is not None and settled_end > now:
        settled_end = now
    window_from = slices[0][0] if slices else None
    coverage: list[tuple[datetime, datetime]] = []
    if window_from is not None and settled_end is not None:
        coverage = [
            (a, min(b, settled_end))
            for a, b in power.covered_ranges(window_from, now)
            if min(b, settled_end) > a
        ]
    # A stall is a *freshness* failure, never an empty booking window (0.20.5).  An update that
    # lands inside the current sample bin finds the ledger already settled up to the newest sample,
    # so `coverage` above is empty by construction there and says nothing about the sampler; the
    # 0.20.3 counter, marker and warning below are kept, but what they report is the age of the
    # newest usable sample against the sample grid instead of the emptiness of the window.
    stalled = bool(slices) and (settled_end is None or (now - settled_end) > STALL_TOLERANCE)
    for start, end, applied, dispatch_reason, mode, dispatch_id in slices:
        if settled_end is None or start >= settled_end:
            # Not bounded by samples on both sides yet: leave it for the next update.
            break
        processed_end = min(end, settled_end)
        if window_start is None:
            window_start = start
        # Nothing usable is held when the pack sits at its floor: start with a clean ledger.
        start_soc = soc.soc_at(start)
        if start_soc is not None and start_soc <= reset_soc:
            basis, energy = None, 0.0
        charge_kwh = 0.0
        discharge_kwh = 0.0
        grid_kwh = 0.0
        pv_kwh = 0.0
        cost_eur = 0.0
        skipped_price = False
        covered_power_hours = 0.0
        while coverage_index < len(coverage) and coverage[coverage_index][1] <= start:
            coverage_index += 1
        for covered_start, covered_end in coverage[coverage_index:]:
            if covered_start >= processed_end:
                break
            span_start = max(covered_start, start)
            span_end = min(covered_end, processed_end)
            if span_end <= span_start:
                continue
            boundaries_t = [span_start, *timeline.starts_between(span_start, span_end), span_end]
            for a, b in zip(boundaries_t, boundaries_t[1:]):
                if b <= a:
                    continue
                charged, power_hours = power.integrate_kwh(a, b)
                covered_power_hours += power_hours
                discharged, _ = power.integrate_kwh(a, b, sign=-1.0)
                discharge_kwh += discharged
                if charged <= 0.0 and discharged <= 0.0:
                    continue
                slot = timeline.at(a)
                buy, sell = _effective((slot.buy, slot.sell)) if slot is not None else (None, None)
                if charged > 0.0:
                    if buy is None and sell is None:
                        # Skip the sub-slice and count it: never invent a price (SPEC 5).
                        skipped_price = True
                        skipped_no_price += 1
                        continue
                    imported, _grid_hours = grid.integrate_kwh(a, b)
                    if not grid:
                        # No meter reading: the plan slot's own reason is the fallback attribution.
                        pv_only = slot is not None and slot.reason == Reason.PV_SURPLUS_CHARGE
                        grid_part = 0.0 if pv_only else charged
                        skipped_no_grid += 1
                    else:
                        grid_part = min(charged, max(0.0, imported))
                    pv_part = charged - grid_part
                    cost = grid_part * float(buy) + pv_part * float(sell)
                    grid_kwh += grid_part
                    pv_kwh += pv_part
                    cost_eur += cost
                    total = energy + charged
                    basis = (
                        (basis * energy + cost) / total
                        if basis is not None and energy > 0.0
                        else cost / charged
                    )
                    energy = total
                    charge_kwh += charged
                if discharged > 0.0 and round_trip > 0.0:
                    energy = max(0.0, energy - discharged / round_trip)
                    if energy <= 0.0:
                        basis = None
        if max_energy is not None and energy > max_energy:
            energy = max_energy
        end_soc_low = soc.min_between(start, processed_end)
        if end_soc_low is not None and end_soc_low <= reset_soc:
            basis, energy = None, 0.0
        if max_energy is not None and energy > max_energy:
            energy = max_energy
        if covered_power_hours <= 0.0:
            skipped_no_power += 1
        intervals += 1
        booked += 1
        if charge_kwh > 0.0 or discharge_kwh > 0.0:
            journal.append(
                {
                    "ts": to_iso(start),
                    "end": to_iso(processed_end),
                    "applied_w": None if applied is None else round(float(applied), 1),
                    "reason": dispatch_reason,
                    "mode": mode,
                    "dispatch_id": dispatch_id,
                    "charge_kwh": round(charge_kwh, 4),
                    "discharge_kwh": round(discharge_kwh, 4),
                    "grid_kwh": round(grid_kwh, 4),
                    "pv_kwh": round(pv_kwh, 4),
                    "cost_eur": round(cost_eur, 4),
                    "skipped_no_price": skipped_price,
                    "basis_eur_per_kwh": None if basis is None else round(basis, 6),
                    "energy_kwh": round(energy, 4),
                }
            )
    if booked:
        last_ts = processed_end
    if stalled:
        skipped_no_samples += 1
        if stored.get("no_samples_since") is None:
            # One warning per stall, not one per tick: the stored marker (which survives a
            # restart) is what says the stall was already reported.
            log.warning(
                "cost-basis ledger for %s stalled: the newest usable battery power sample is %s, "
                "older than the %s stall tolerance (window since %s); the stored basis and sell "
                "floor no longer follow the pack",
                battery.id,
                to_iso(settled_end) if settled_end is not None else "missing",
                STALL_TOLERANCE,
                to_iso(window_from or now),
            )
        if no_samples_since is None:
            no_samples_since = window_from or now
    else:
        no_samples_since = None

    # --- reconcile the quantity with the pack's measured content (0.18.1) ----------------------
    if booked and basis is not None and max_energy is not None and last_ts is not None:
        soc_now = soc.value_at_or_before(last_ts)
        if soc_now is None:
            skipped_no_soc += 1
        else:
            physical = battery.capacity_kwh * (soc_now - floor_soc) / 100.0
            physical = max(0.0, min(physical / float(battery.eta_charge), max_energy))
            correction = physical - energy
            if abs(correction) > battery.capacity_kwh * RECONCILE_RESET_FRACTION:
                # More than half a pack in one step is not a measurement lag: the ledger is
                # unattributable, so start clean instead of silently adjusting it (0.18.1).
                basis, energy = None, 0.0
                reconciled, reconciled_last = 0.0, None
                reconciled_resets += 1
            else:
                energy = physical
                reconciled += correction
                reconciled_last = correction
    if len(journal) > MAX_JOURNAL:
        journal = journal[-MAX_JOURNAL:]

    return {
        "version": STATE_VERSION,
        "sample_source": source,
        "basis_eur_per_kwh": None if basis is None else round(basis, 6),
        "energy_kwh": round(energy, 4),
        "last_ts": to_iso(last_ts),
        "last_dispatch_id": command[3],
        "last_applied_w": None if command[0] is None else round(float(command[0]), 3),
        "last_reason": command[1],
        "last_mode": command[2],
        "intervals": intervals,
        "skipped_no_samples": skipped_no_samples,
        "no_samples_since": to_iso(no_samples_since),
        "stale": no_samples_since is not None,
        "skipped_no_price": skipped_no_price,
        "skipped_no_power": skipped_no_power,
        "skipped_no_grid": skipped_no_grid,
        "skipped_no_soc": skipped_no_soc,
        "reconciled_kwh": round(reconciled, 4),
        "reconciled_last_kwh": None if reconciled_last is None else round(reconciled_last, 4),
        "reconciled_resets": reconciled_resets,
        "window_start": to_iso(window_start),
        "journal": journal,
        "updated_at": to_iso(now),
    }


def load_basis(store: Any, battery_id: str) -> dict[str, Any] | None:
    """Read the stored ledger for one battery; ``None`` without a store or a saved ledger."""
    getter = getattr(store, "profile_get", None)
    if not callable(getter):
        return None
    try:
        stored = getter(PROFILE_KIND, battery_id)
    except Exception:  # noqa: BLE001 - an unreadable profile is "no ledger", never a crash
        return None
    if isinstance(stored, tuple) and len(stored) == 2:
        stored = stored[1]
    return dict(stored) if isinstance(stored, Mapping) else None


def update_basis(
    store: Any,
    battery: BatteryConfig,
    now: datetime | None = None,
    *,
    days: float = LOOKBACK_DAYS,
    timeline: PriceTimeline | None = None,
    fleet_enabled: int = 1,
    reserve_soc_pct: float = 0.0,
) -> dict[str, Any] | None:
    """Advance one battery's ledger from its dispatch log, measurements and persisted plans.

    Returns the stored payload (or the previous one when nothing changed).  A missing store, missing
    rows or an unreadable profile is not an error, only "no change".

    ``reserve_soc_pct`` is ``optimizer.reserve_soc_pct``: the per-battery level the plan never
    discharges below.  The ledger prices only the energy above it (0.21.0), so the basis and the
    fleet floor never include energy that can never be sold.

    When the measurement series holds no usable power point in the window, the fleet-aggregate
    ``actuals`` rows stand in for it -- but only with ``fleet_enabled == 1``, because with several
    batteries the aggregate cannot be split without guessing (0.20.3).  A stall is persisted (the
    ``skipped_no_samples`` counter and the ``no_samples_since`` marker) instead of being dropped, so
    it survives a restart and shows up in ``/status``.
    """
    moment = now or utcnow()
    if not callable(getattr(store, "measurements_range", None)) or not callable(
        getattr(store, "dispatch_log", None)
    ):
        return load_basis(store, battery.id)
    stored = load_basis(store, battery.id)
    last_ts = _parse((stored or {}).get("last_ts"))
    lookback = moment - timedelta(days=max(0.1, float(days)))
    reset = False
    start = lookback
    if last_ts is not None:
        if lookback <= last_ts <= moment:
            start = last_ts
        else:
            # A gap beyond the lookback: the contents can no longer be attributed, start clean.
            reset = True
    read_start = start - timedelta(hours=1)  # one preceding sample, for interpolation at `start`
    try:
        rows = store.measurements_range(read_start, moment, roles=measurement_roles(battery.id)) or []
    except Exception:  # noqa: BLE001 - no history is not an error
        rows = []
    try:
        dispatch_rows = store.dispatch_log(
            limit=DISPATCH_LIMIT, battery_id=battery.id, since=start
        ) or []
    except Exception:  # noqa: BLE001
        dispatch_rows = []
    if timeline is None:
        timeline = PriceTimeline.from_store(store, start, moment)
    series = measurement_series(rows, battery.id)
    source = "measurements"
    if _count(fleet_enabled) == 1 and not _has_window_power(series, start):
        # The persisted measurement rows do not cover the window: the actuals row is the same
        # physical measurement aggregated once per bucket, and with a single enabled battery it
        # belongs to that battery.  Recovering it books energy that was measured but never booked.
        actuals = _actuals_series(store, battery, start, moment)
        if actuals.power:
            series, source = actuals, "actuals"
    booked = book_slices(
        stored,
        battery=battery,
        series=series,
        timeline=timeline,
        dispatch_rows=dispatch_rows,
        now=moment,
        reset=reset,
        source=source,
        reserve_soc_pct=reserve_soc_pct,
    )
    previous = stored or {}
    grew = _count(booked.get("intervals")) > _count(previous.get("intervals"))
    # A stall must be persisted even though it books nothing: the counter and the marker are the
    # only trace until booking resumes, and clearing the marker is itself a change to store.
    stall_changed = (
        _count(booked.get("skipped_no_samples"))
        != _count(previous.get("skipped_no_samples"))
        or booked.get("no_samples_since") != previous.get("no_samples_since")
    )
    if not grew and not stall_changed:
        return stored
    writer = getattr(store, "profile_put", None)
    if callable(writer):
        try:
            writer(PROFILE_KIND, battery.id, booked, moment)
        except Exception:  # noqa: BLE001 - a write failure leaves the previous ledger in place
            return booked
    return booked


def basis_status(
    payload: Mapping[str, Any] | None,
    battery: BatteryConfig | None = None,
    *,
    enabled: bool = True,
    margin_eur_per_kwh: float = 0.0,
    wear_eur_per_kwh: float | None = None,
    expose_journal: int = JOURNAL_EXPOSED,
) -> dict[str, Any]:
    """Classify a stored payload for the API/UI: basis, held value, sell floor and provenance.

    ``battery=None`` (unknown id) still reports the ledger; only the derived floor needs a battery.
    The value of the AC-equivalent energy held is defined once: ``basis * energy_kwh``.
    """
    basis = _finite((payload or {}).get("basis_eur_per_kwh"))
    energy = max(0.0, _finite((payload or {}).get("energy_kwh")) or 0.0)
    if basis is None:
        energy = 0.0
    value = basis * energy if basis is not None else None
    floor = None
    if enabled and basis is not None and battery is not None:
        floor = _battery_floor(battery, basis, wear_eur_per_kwh, margin_eur_per_kwh)
    journal = [
        dict(entry) for entry in ((payload or {}).get("journal") or []) if isinstance(entry, Mapping)
    ]
    exposed = max(0, int(expose_journal))
    return {
        "cost_basis_eur_per_kwh": None if basis is None else round(basis, 6),
        "energy_kwh": round(energy, 4),
        "stored_value_eur": None if value is None else round(value, 4),
        "sell_floor_eur_per_kwh": None if floor is None else round(floor, 6),
        "enabled": bool(enabled),
        "intervals": _count((payload or {}).get("intervals")),
        "skipped_no_price": _count((payload or {}).get("skipped_no_price")),
        "skipped_no_power": _count((payload or {}).get("skipped_no_power")),
        "skipped_no_grid": _count((payload or {}).get("skipped_no_grid")),
        "skipped_no_samples": _count((payload or {}).get("skipped_no_samples")),
        # Additive (0.20.3): a stalled ledger says so instead of just freezing (SPEC 5).
        "stale": bool((payload or {}).get("no_samples_since")),
        "no_samples_since": (payload or {}).get("no_samples_since"),
        "sample_source": (payload or {}).get("sample_source"),
        # Additive (0.18.1): how far the quantity was pulled onto the pack's own measured SoC
        # content, and how often a correction was too large to attribute (a reset).
        "skipped_no_soc": _count((payload or {}).get("skipped_no_soc")),
        "reconciled_kwh": round(_finite((payload or {}).get("reconciled_kwh")) or 0.0, 4),
        "reconciled_last_kwh": (
            None
            if _finite((payload or {}).get("reconciled_last_kwh")) is None
            else round(_finite((payload or {}).get("reconciled_last_kwh")), 4)
        ),
        "reconciled_resets": _count((payload or {}).get("reconciled_resets")),
        "window_start": (payload or {}).get("window_start"),
        "updated_at": (payload or {}).get("updated_at"),
        "journal_entries": len(journal),
        "journal": journal[-exposed:] if exposed else [],
    }


def fleet_sell_floor(
    entries: Iterable[tuple[BatteryConfig, float | None, float | None, float | None]],
    *,
    margin_eur_per_kwh: float = 0.0,
) -> float | None:
    """Energy-weighted mean of the per-battery sell floors; ``None`` when nothing is accounted.

    ``entries`` are ``(battery, basis, energy_kwh, wear_eur_per_kwh)``; a battery with no basis or
    no accounted energy contributes nothing (its money is not at stake) and never a fabricated 0.
    """
    total_energy = 0.0
    weighted = 0.0
    for battery, basis, energy, wear in entries:
        basis_value = _finite(basis)
        energy_value = _finite(energy) or 0.0
        if basis_value is None or energy_value <= 0.0:
            continue
        floor = _battery_floor(battery, basis_value, wear, margin_eur_per_kwh)
        if floor is None:
            continue
        weighted += floor * energy_value
        total_energy += energy_value
    return weighted / total_energy if total_energy > 0.0 else None


def fleet_basis_eur_per_kwh(
    entries: Iterable[tuple[BatteryConfig, float | None, float | None]],
) -> float | None:
    """Energy-weighted mean cost basis of the fleet, ``None`` when nothing is accounted."""
    total_energy = 0.0
    weighted = 0.0
    for _battery, basis, energy in entries:
        basis_value = _finite(basis)
        energy_value = _finite(energy) or 0.0
        if basis_value is None or energy_value <= 0.0:
            continue
        weighted += basis_value * energy_value
        total_energy += energy_value
    return weighted / total_energy if total_energy > 0.0 else None
