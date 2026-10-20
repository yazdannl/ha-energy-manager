"""Episode extraction for the optimizer v2 replay harness (docs/REPLAY_HARNESS.md section 1).

A recording is opened read-only and copied with SQLite's backup API into a writable scratch store:
the source handle is the only read path for recorded data, so a table the engine also writes can
never be read back as if it were the recording.  Everything the replay may see is frozen here, and
every missing or ambiguous window is refused with a machine-readable reason instead of defaulted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import capacity as capacity_learning
from .. import cost_basis as cost_basis_ledger
from .. import efficiency as efficiency_learning
from .. import full_charge
from ..config import Settings
from ..engine import ACTUALS_MINUTES, MIN_REPLAN_GAP_S
from ..models import (
    Action,
    BatteryConfig,
    BatteryState,
    Mode,
    Plan,
    PlanSlot,
    PlanSummary,
    clean_manual_limits,
    from_iso,
    quarter_floor,
    resolve_manual_limit,
    to_iso,
)
from ..sources import load as load_source
from ..sources import solar as solar_source
from ..sources.measure import MEASUREMENT_MINUTES
from ..store import (
    Store,
    parsed_as_of_instant,
    profile_as_of,
    profile_deleted_as_of,
    settings_as_of,
    settings_deleted_as_of,
    table_exists,
)

#: Refusal reasons; stable, machine-readable slugs (docs/REPLAY_HARNESS.md section 6).
LIVE_STORE = "live_store"
NOT_SQLITE = "not_sqlite"
UNFLUSHED_WAL = "unflushed_wal"
NO_VINTAGE = "no_vintage"
INCOMPLETE_ACTUALS = "incomplete_actuals"
MALFORMED_JSON = "malformed_json"
WINDOW = "window"
NON_ANTICIPATION = "non_anticipation"

#: Distinguishes "this comparand was read and is absent" from "not read yet" in the source cache.
_UNREAD: Any = object()

#: ``actuals`` columns the replay reads; the physical five must be present in every window bucket.
RECORDED_SERIES = (
    "pv_kw",
    "load_kw",
    "grid_kw",
    "battery_kw",
    "soc_kwh",
    "price_buy",
    "price_sell",
)
REQUIRED_SERIES = ("pv_kw", "load_kw", "grid_kw", "battery_kw", "soc_kwh")
#: The required series that have no derived stand-in; ``pv_kw``/``load_kw`` are covered by
#: :func:`_effective_base` instead.
DIRECT_REQUIRED_SERIES = tuple(
    name for name in REQUIRED_SERIES if name not in ("pv_kw", "load_kw")
)

#: Where a bucket's pv/load base came from: two measured columns, two derived ones, or one of each.
BASE_MEASURED = "measured"
BASE_DERIVED = "derived"
BASE_MIXED = "mixed"
#: A bucket whose derived pair failed the recording's own identity check: it has no usable base.
BASE_REFUSED = "refused"
#: A window that covers no recorded bucket at all.
BASE_ABSENT = "absent"
BASE_LABELS = (BASE_MEASURED, BASE_DERIVED, BASE_MIXED)

#: The rounding tolerance for the recording's own identity
#: ``pv_derived_kw == max(0, load_derived_kw - grid_kw + battery_kw)``: it holds by construction, so
#: this only absorbs the rounding of a row written in stages.
BASE_IDENTITY_TOLERANCE_KW = 1e-3

#: Append-only tables are cut by ``max(id)`` right after the copy; upsert-by-key tables are
#: compared by key between the source and the scratch copy (docs/REPLAY_HARNESS.md section 1).
APPEND_ONLY_TABLES = ("plans", "dispatch_log", "events")
UPSERT_BY_KEY_TABLES = (
    "actuals",
    "measurements",
    "savings_daily",
    "learning_profiles",
    "forecast_archive",
    "settings",
)
#: The columns that identify a row of each upsert-by-key table.
UPSERT_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "actuals": ("ts",),
    "measurements": ("ts", "entity_role"),
    "savings_daily": ("day",),
    "learning_profiles": ("kind", "key"),
    "forecast_archive": ("kind", "source", "target_ts"),
    "settings": ("key",),
}

#: ``learning_profiles`` kinds whose value feeds a decision (docs/REPLAY_HARNESS.md section 1).
DECISION_PROFILE_KINDS = (
    "control",
    full_charge.STATE_KIND,
    capacity_learning.PROFILE_KIND,
    cost_basis_ledger.PROFILE_KIND,
    load_source.PROFILE_KIND,
    solar_source.BIAS_KIND,
)

#: Profile kinds whose newer row may be DROPPED instead of refusing the window, because the replay
#: can rebuild the value from rows of the past alone: ``cost_basis.update_basis`` resets and rebuilds
#: the ledger over its own lookback when the stored watermark is gone, and a learned capacity falls
#: back to the configured ``capacity_kwh``.  Everything else in :data:`DECISION_PROFILE_KINDS` has no
#: rebuild path inside a replay, so a newer row still refuses the window (`no_vintage`).
REBUILDABLE_PROFILE_KINDS = (cost_basis_ledger.PROFILE_KIND, capacity_learning.PROFILE_KIND)

#: Every table the scratch copy is cut at the window start, on that table's own time column.
#:
#: Production's store never holds a row newer than "now", and several production readers rely on
#: that: ``Store.dispatch_log(limit, battery_id, since)`` has no upper bound and orders newest-first,
#: and the cost-basis ledger feeds exactly those rows to ``update_basis`` before every solve.  The
#: replay's copy does hold the recording's future, so the boundary is enforced in the data -- the
#: scratch store is the recording up to ``start`` plus whatever the replay writes -- instead of
#: trusting every call site to remember an upper bound.
BOUNDARY_COLUMNS: dict[str, str] = {
    "actuals": "ts",
    "measurements": "ts",
    "dispatch_log": "ts",
    "events": "ts",
    "savings_daily": "day",
    "forecast_archive": "issued_at",
    "forecast_cache": "fetched_at",
    "plans": "created_at",
    "plan_slots": "t",
}

#: Tables a recording must have; anything else is not this application's store.
_REQUIRED_TABLES = frozenset(("plan_slots", *APPEND_ONLY_TABLES, *UPSERT_BY_KEY_TABLES))


class EpisodeError(RuntimeError):
    """A refused episode, with a stable ``reason`` slug (never a silent default)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@dataclass(frozen=True)
class Truncation:
    """What the boundary cut removed from the scratch copy, so the report can state it.

    ``rows`` counts the deleted rows per table, ``dropped_profiles`` names the decision-feeding
    profile keys whose only row was newer than the window start (their value is rebuilt from the
    recording's past, or falls back to the configured value), and ``cut_at`` is the boundary the cut
    used.  An empty truncation means nothing had to be removed: the recording's tables stop at or
    before the window start.
    """

    cut_at: datetime | None = None
    rows: Mapping[str, int] = field(default_factory=dict)
    dropped_profiles: tuple[tuple[str, str], ...] = ()

    @property
    def rows_removed(self) -> int:
        return sum(int(count) for count in self.rows.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "cut_at": None if self.cut_at is None else to_iso(self.cut_at),
            "rows_removed": self.rows_removed,
            "tables": {table: int(count) for table, count in sorted(self.rows.items())},
            "dropped_profiles": [[kind, key] for kind, key in self.dropped_profiles],
        }


@dataclass(frozen=True)
class _AsOfSettings:
    """Every settings value in force at the window start, with its instant and provenance."""

    as_of: Mapping[str, tuple[datetime, Any]]
    origins: Mapping[str, str]

    @property
    def values(self) -> dict[str, Any]:
        return {key: value for key, (_at, value) in self.as_of.items()}


@dataclass(frozen=True)
class _AsOfProfiles:
    """Accepted decision-feeding profiles, and every kind's value the scratch must be given.

    ``values``/``origins`` cover the decision-feeding kinds only -- they are what the episode
    declares as frozen.  ``as_of`` covers every kind the recording knows, because the engine reads
    the scratch's ``learning_profiles`` through ``profile_get`` for other kinds too (rate-limit
    backoff, load/pv bias), and only the as-of row may survive the boundary.
    """

    as_of: Mapping[tuple[str, str], tuple[datetime, Any]]
    values: Mapping[tuple[str, str], Any]
    origins: Mapping[tuple[str, str], str]


@dataclass(frozen=True)
class BaseReading:
    """The recording's own pv/load at one instant, with the label of the bucket it came from."""

    load_kw: float
    pv_kw: float
    source: str

    @property
    def base_kw(self) -> float:
        return self.load_kw - self.pv_kw

    def as_dict(self) -> dict[str, Any]:
        return {"load_kw": self.load_kw, "pv_kw": self.pv_kw, "base_kw": self.base_kw,
                "source": self.source}


@dataclass(frozen=True)
class BaseProvenance:
    """Where each five-minute bucket's pv/load base came from (REPLAY_HARNESS sections 2-3).

    An instance without pv/load sensors never fills ``pv_kw``/``load_kw``, and the engine's own
    same-tick estimates (``pv_derived_kw``/``load_derived_kw``) are then the only value the planner
    and the sampler ever saw -- as-of information, not invention.  They are still *modelled* values,
    so every bucket is labelled and a reader can never mistake a modelled base for a measured one.

    ``refused`` names the buckets whose derived pair failed the recording's own identity check
    (:data:`BASE_IDENTITY_TOLERANCE_KW`); such a bucket has no base at all, so the plant refuses its
    site flow rather than reconstructing one from a half-written row.  Completeness is unchanged:
    a bucket with neither measured nor derived values still refuses the whole window at extraction.
    """

    source: Mapping[datetime, str] = field(default_factory=dict)
    refused: tuple[datetime, ...] = ()

    def at(self, moment: datetime) -> str | None:
        """The label of the bucket covering ``moment`` (the newest bucket at or before it)."""
        known = [bucket for bucket in self.source if bucket <= moment]
        return self.source[max(known)] if known else None

    def window(self, start: datetime, end: datetime) -> str:
        """One label for the window: the provenance in force at ``start``.

        The label is the covering bucket at ``start`` -- the newest bucket at or before it -- because
        a bucket's base is held until the next bucket begins, so that single label describes the value
        every tick of the window used.  It therefore reads as one bucket's own source (``mixed``
        means that bucket's pair was half measured and half derived, not that the window contains
        several kinds); :meth:`split` counts the buckets whose own start falls inside
        ``[start, end)``, which is why a window can begin before the first bucket inside it.
        ``refused`` means the covering bucket failed the identity check (no usable base) and
        ``absent`` that no recorded bucket covers ``start`` at all.
        """
        label = self.at(start)
        if label is not None:
            return label
        covered = any(bucket <= start for bucket in self.refused)
        return BASE_REFUSED if covered else BASE_ABSENT

    def split(self, start: datetime, end: datetime) -> dict[str, int]:
        """Bucket counts per label in ``[start, end)``, ``refused`` included."""
        counts = {label: 0 for label in (*BASE_LABELS, BASE_REFUSED)}
        for bucket, label in self.source.items():
            if start <= bucket < end:
                counts[label] = counts.get(label, 0) + 1
        counts[BASE_REFUSED] += sum(1 for bucket in self.refused if start <= bucket < end)
        return counts


@dataclass(frozen=True)
class WarmStart:
    """The state the engine should start from, read from the recording at ``start``.

    ``plan`` is the newest recorded ``plans`` row with ``created_at <= start`` (less than or equal,
    so the boundary is defined even without history to interpolate), and ``last_replan_at`` is that
    row's own ``created_at`` -- the moment the last replan happened, not the moment of the copy.
    """

    plan: Plan | None
    last_replan_at: datetime | None
    backoff_s: float


@dataclass(frozen=True)
class Boundary:
    """The recording/replay cut taken from the source right after the scratch copy."""

    plan_id: int | None
    dispatch_id: int | None
    event_id: int | None
    #: The source's rows of the upsert-by-key tables, so a key can be compared source-vs-scratch.
    source_rows: Mapping[str, tuple[Mapping[str, Any], ...]]

    def key(self, table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
        """The upsert key of one row, from that table's declared key columns."""
        return tuple(row[column] for column in UPSERT_KEY_COLUMNS[table])

    def source_index(self, table: str) -> dict[tuple[Any, ...], Mapping[str, Any]]:
        """The source rows of one table, indexed by their upsert key."""
        return {self.key(table, row): row for row in self.source_rows.get(table, ())}


class AsOfSnapshot:
    """The recorded data visible at one instant, and nothing after it (section 3).

    Every accessor that names an instant refuses one beyond :attr:`now`: a replay that can see the
    future is not a replay, so non-anticipation is an error, never a documented convention.
    """

    def __init__(self, episode: "Episode", now: datetime) -> None:
        self._episode = episode
        self.now = now

    def series(self, name: str) -> tuple[tuple[datetime, float | None], ...]:
        """The recorded points of one series up to ``now`` (inclusive)."""
        points = self._episode.series[name]
        return tuple(point for point in points if point[0] <= self.now)

    def latest(self, name: str) -> tuple[datetime, float | None] | None:
        """The most recent point at or before ``now``, or ``None`` when none exists."""
        points = [point for point in self._episode.series[name] if point[0] <= self.now]
        return points[-1] if points else None

    def value_at(self, name: str, when: datetime) -> float | None:
        """The recorded point exactly at ``when``; refuses ``when > now``."""
        self._guard(name, when)
        for point in self._episode.series[name]:
            if point[0] == when:
                return point[1]
        return None

    def range(
        self, name: str, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, float | None], ...]:
        """The recorded points in ``[start, end)``; refuses an ``end`` beyond ``now``."""
        self._guard(name, end)
        return tuple(
            point for point in self._episode.series[name] if start <= point[0] < end
        )

    def plan(self) -> Plan | None:
        """The recorded plan in force at ``now``: the newest with ``created_at <= now``.

        ``Episode.plans`` is already ordered by ``(created_at, id)``, so the last qualifying entry
        is the newest one (the id only breaks ties).
        """
        active: Plan | None = None
        for plan in self._episode.plans:
            if plan.generated_at <= self.now:
                active = plan
        return active

    def forecasts(self) -> tuple[Mapping[str, Any], ...]:
        """Archived forecasts issued at or before ``now`` (section 3)."""
        return tuple(
            row
            for row in self._episode.forecasts
            if (issued := from_iso(row.get("issued_at"))) is not None and issued <= self.now
        )

    def battery_state(self, config: BatteryConfig) -> BatteryState:
        """The recorded state of one battery at ``now``.

        Per-battery ``measurements`` roles (``battery_soc:<id>`` in percent, ``battery_power:<id>``
        in W, positive = charging) are the recording's own samples.  ``read_at`` is the *oldest*
        contributing sample's timestamp -- never refreshed to ``now`` -- so the dispatcher's
        staleness rule sees a measurement outage exactly as the live run did, and each quantity's
        age is in ``extra``.  ``power_measured`` is True only when the power value comes from a
        sample as fresh as the state itself: an old power sample next to a fresh SoC is a power
        telemetry outage, not a flow.  Every quantity is judged against the cadence it was recorded
        at, as that cadence stood at ``now`` -- so an arrival after ``now`` can never change how an
        earlier snapshot was judged -- and a quantity older than its own spacing is named in
        ``extra['stale_quantities']`` and marks the state ``stale``, exactly as an aged live read is
        treated.

        When a pack has no sample at all the recording only has the fleet row in ``actuals``: a
        one-battery install is exact, and several packs are split by capacity share.  That fallback
        is not silent: it is marked in ``extra['sample_source'] = 'fleet_share'`` and reported as
        not measured, because ``actuals.soc_kwh`` is a fleet sum that cannot recover the split.
        """
        samples = self._episode.battery_samples.get(config.id, {})
        soc_series = samples.get("soc_pct")
        power_series = samples.get("power_w")
        soc_point = self._latest_point(soc_series)
        power_point = self._latest_point(power_series)
        # Each quantity is judged against the cadence it was recorded at, not a constant of today.
        soc_spacing = _sample_spacing_s(soc_series, self.now, MEASUREMENT_MINUTES * 60.0)
        power_spacing = _sample_spacing_s(power_series, self.now, MEASUREMENT_MINUTES * 60.0)
        sample_source = "measurement"
        if soc_point is None:
            fleet_soc = self.latest("soc_kwh")
            fleet_power = self.latest("battery_kw")
            total_kwh = sum(float(entry.capacity_kwh) for entry in self._episode.batteries)
            capacity = float(config.capacity_kwh)
            share = capacity / total_kwh if total_kwh > 0 else 0.0
            if fleet_soc is not None and fleet_soc[1] is not None and capacity > 0:
                soc_point = (fleet_soc[0], max(0.0, float(fleet_soc[1]) * share / capacity * 100.0))
                soc_spacing = ACTUALS_MINUTES * 60.0
                sample_source = "fleet_share"
            if power_point is None and fleet_power is not None and fleet_power[1] is not None:
                power_point = (fleet_power[0], float(fleet_power[1]) * 1000.0 * share)
                power_spacing = ACTUALS_MINUTES * 60.0
        contributing = [
            (point, spacing)
            for point, spacing in ((soc_point, soc_spacing), (power_point, power_spacing))
            if point is not None
        ]
        soc_pct = soc_point[1] if soc_point is not None else None
        power_w = power_point[1] if power_point is not None else None
        extra: dict[str, Any] = {}
        read_at: datetime | None = None
        stale_quantities: list[str] = []
        # Every quantity is judged against its OWN cadence: a SoC still inside its sampling interval
        # next to a power sample that has outlived its own is a telemetry outage for that quantity,
        # not a healthy read.
        for name, point, spacing in (
            ("soc", soc_point, soc_spacing),
            ("power", power_point, power_spacing),
        ):
            if point is None:
                continue
            age = (self.now - point[0]).total_seconds()
            extra[f"{name}_age_s"] = age
            if age > spacing:
                stale_quantities.append(name)
        if contributing:
            # The oldest contributor is what a caller may rely on, so it is ``read_at``.
            read_at = min(point[0] for point, _ in contributing)
        stale = bool(stale_quantities)
        if stale_quantities:
            extra["stale_quantities"] = stale_quantities
        measured = (
            sample_source == "measurement"
            and power_w is not None
            and "power" not in stale_quantities
        )
        if sample_source != "measurement":
            extra["sample_source"] = sample_source
        return BatteryState(
            id=config.id,
            available=soc_pct is not None,
            soc_pct=soc_pct,
            power_w=power_w,
            capacity_kwh=float(config.capacity_kwh),
            energy_kwh=(
                None if soc_pct is None else float(soc_pct) / 100.0 * float(config.capacity_kwh)
            ),
            read_at=read_at,
            stale=stale,
            power_measured=measured,
            extra=extra,
        )

    def _latest_point(
        self, points: tuple[tuple[datetime, float | None], ...] | None
    ) -> tuple[datetime, float | None] | None:
        """The latest recorded point at or before ``now``, with its own timestamp."""
        if not points:
            return None
        visible = [point for point in points if point[0] <= self.now]
        return visible[-1] if visible else None

    def _guard(self, name: str, when: datetime) -> None:
        if when > self.now:
            raise EpisodeError(
                NON_ANTICIPATION,
                f"{name} asked for {to_iso(when)}, past the replay instant {to_iso(self.now)}",
            )


class Episode:
    """One replay window and the frozen recording data it may read (section 1).

    The scratch copy is the engine's store, cut at the window start (see :class:`Truncation`), so it
    holds the recording up to the boundary and nothing from after it; every recorded value is read
    from the source before the copy is created and is exposed afterwards only through :meth:`at`
    snapshots.
    """

    def __init__(
        self,
        *,
        source_path: str,
        scratch_path: str,
        start: datetime,
        end: datetime,
        step_s: float,
        settings: Settings,
        settings_values: Mapping[str, Any],
        profiles: Mapping[tuple[str, str], Any],
        batteries: Sequence[BatteryConfig],
        series: Mapping[str, tuple[tuple[datetime, float | None], ...]],
        battery_samples: Mapping[str, Mapping[str, tuple[tuple[datetime, float | None], ...]]],
        plans: Sequence[Plan],
        dispatch: Sequence[Mapping[str, Any]],
        forecasts: Sequence[Mapping[str, Any]],
        warm_start: WarmStart,
        boundary: Boundary,
        truncation: Truncation | None = None,
        base: BaseProvenance | None = None,
        asof_sources: Mapping[str, str] | None = None,
        keep_scratch: bool = False,
    ) -> None:
        self.source_path = source_path
        self.scratch_path = scratch_path
        self.start = start
        self.end = end
        self.step_s = float(step_s)
        self.settings = settings
        self.settings_values = dict(settings_values)
        self.profiles = dict(profiles)
        self.batteries = tuple(batteries)
        self.series = dict(series)
        self.battery_samples = {
            battery_id: dict(by_name) for battery_id, by_name in battery_samples.items()
        }
        self.plans = tuple(plans)
        self.dispatch = tuple(dispatch)
        self.forecasts = tuple(forecasts)
        self.warm_start = warm_start
        self.boundary = boundary
        self.truncation = truncation if truncation is not None else Truncation()
        self.base = base if base is not None else BaseProvenance()
        #: Per frozen decision value, whether it came from the as-of history, the upgrade's seed,
        #: or (for a recording made before the history existed) the current row.  Reporting only.
        self.asof_sources = dict(asof_sources or {})
        self.now = start
        self._keep_scratch = keep_scratch
        self._store: Store | None = None
        self._source_profiles: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._source_columns: dict[str, frozenset[str] | None] = {}

    # -- lifecycle --------------------------------------------------------------

    def store(self) -> Store:
        """The engine's writable :class:`~app.store.Store`, on the scratch copy."""
        if self._store is None:
            store = Store(self.scratch_path)
            store.connection  # open and migrate now: a schema failure is an episode error
            self._store = store
        return self._store

    def source_profile(self, kind: str, key: str) -> Mapping[str, Any] | None:
        """One profile row read from the **source** recording, whatever its ``updated_at``.

        A comparand, never a policy input.  Gate 3 checks what the replay booked against what the
        recording's own ledger held, and a comparand is allowed to sit after the window end because
        no leg's decision reads it: nothing here is visible to a tick, and the source is opened
        read-only and immutable and is never written to.  Absent rows are cached as ``None`` so a
        missing comparand cannot be retried into existence mid-run.
        """
        cached = self._source_profiles.get((kind, key), _UNREAD)
        if cached is not _UNREAD:
            return cached
        payload: dict[str, Any] | None = None
        try:
            connection = sqlite3.connect(
                f"file:{self.source_path}?mode=ro&immutable=1", uri=True
            )
            try:
                row = connection.execute(
                    "SELECT payload_json FROM learning_profiles WHERE kind = ? AND key = ?",
                    (kind, key),
                ).fetchone()
            finally:
                connection.close()
            if row is not None and row[0]:
                loaded = json.loads(row[0])
                payload = loaded if isinstance(loaded, dict) else None
        except (sqlite3.Error, ValueError, OSError):
            payload = None
        self._source_profiles[(kind, key)] = payload
        return payload

    def source_columns(self, table: str) -> frozenset[str] | None:
        """One **source** table's column names, or ``None`` when the source cannot be read.

        The scratch copy is opened through :class:`~app.store.Store`, which migrates it, so the
        copy's schema can hold columns the recording never had.  A check about what the recording
        itself stored (for example: did this recording book a ``price_sell`` column?) has to ask
        the source, never the copy.  An absent table is an empty set, which is a fact about the
        recording; ``None`` means the source itself could not be read, which callers must treat as
        a refusal rather than as "no columns".
        """
        cached = self._source_columns.get(table, _UNREAD)
        if cached is not _UNREAD:
            return cached
        columns: frozenset[str] | None
        try:
            connection = sqlite3.connect(
                f"file:{self.source_path}?mode=ro&immutable=1", uri=True
            )
            try:
                rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            finally:
                connection.close()
            columns = frozenset(str(row[1]) for row in rows)
        except (sqlite3.Error, OSError):
            columns = None
        self._source_columns[table] = columns
        return columns

    def close(self) -> None:
        """Close the scratch store and remove the copy unless ``keep_scratch`` was requested."""
        if self._store is not None:
            self._store.close()
            self._store = None
        if not self._keep_scratch:
            _remove(self.scratch_path)

    def __enter__(self) -> "Episode":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- replay clock and as-of reads ------------------------------------------

    def rewind(self) -> None:
        """Put the replay instant back at the window start so another leg can drive this recording.

        Legs share one recording but each drives its own scratch copy, so a second leg has to begin
        from the same instant as the first.  The episode holds no other mutable state -- nothing but
        the clock -- so rewinding cannot un-see a future row or resurrect one the cut removed.
        """
        self.now = self.start

    def advance_to(self, now: datetime) -> datetime:
        """Move the replay instant forward; a replay never runs backwards or past its window."""
        moment = _aware(now)
        if moment < self.now:
            raise EpisodeError(
                NON_ANTICIPATION,
                f"replay instant cannot move backwards: {to_iso(moment)} < {to_iso(self.now)}",
            )
        if moment > self.end:
            raise EpisodeError(
                WINDOW, f"replay instant {to_iso(moment)} is past the window end {to_iso(self.end)}"
            )
        self.now = moment
        return moment

    def at(self, now: datetime) -> AsOfSnapshot:
        """A view of the recording at ``now``, refusing data from any later instant."""
        moment = _aware(now)
        if moment > self.end:
            raise EpisodeError(
                WINDOW, f"snapshot {to_iso(moment)} is past the window end {to_iso(self.end)}"
            )
        return AsOfSnapshot(self, moment)

    def base_at(self, now: datetime) -> BaseReading | None:
        """The recording's own pv/load base at ``now``, labelled with its provenance.

        This is the sanctioned way to read the site base: it is the only accessor that knows which
        bucket a value came from, so a plant can refuse a bucket whose derived pair failed the
        recording's identity check instead of reconstructing a flow from a half-written row.  The
        value is held constant across the bucket exactly as ``AsOfSnapshot.latest`` holds it, and
        ``now`` past the window end is refused like every other as-of read.
        """
        moment = _aware(now)
        if moment > self.end:
            raise EpisodeError(
                WINDOW, f"snapshot {to_iso(moment)} is past the window end {to_iso(self.end)}"
            )
        loads = [point for point in self.series.get("load_kw", ()) if point[0] <= moment]
        pvs = {
            point[0]: point[1]
            for point in self.series.get("pv_kw", ())
            if point[0] <= moment
        }
        for bucket, load in reversed(loads):
            pv = pvs.get(bucket)
            if load is None or pv is None:
                continue
            source = self.base.at(bucket)
            if source is None:
                return None  # a refused bucket: no base, and never an older one either
            return BaseReading(load_kw=float(load), pv_kw=float(pv), source=source)
        return None

    def initial_battery_states(self) -> list[BatteryState]:
        """The as-of battery view at ``start`` (the warm-start seed, section 1)."""
        snapshot = self.at(self.start)
        return [snapshot.battery_state(config) for config in self.batteries]


def open_episode(
    source_path: str | Path,
    start: datetime,
    end: datetime,
    step_s: float = 1.0,
    *,
    live_store_paths: Sequence[str | Path] = (),
    scratch_dir: str | Path | None = None,
    keep_scratch: bool = False,
) -> Episode:
    """Freeze one recording window into an :class:`Episode`.

    Refusals (all raise :class:`EpisodeError` with a ``reason``): the source is a known live store
    or not a readable SQLite store, the source has an unflushed WAL, the settings/profiles have no
    as-of value at ``start``, ``actuals`` coverage is incomplete, or there is neither a plan nor an
    archived forecast from which to plan.
    """
    start = _aware(start)
    end = _aware(end)
    if not isinstance(step_s, (int, float)) or float(step_s) <= 0:
        raise EpisodeError(WINDOW, f"step_s must be positive, got {step_s!r}")
    if end <= start:
        raise EpisodeError(WINDOW, f"empty window: {to_iso(start)} .. {to_iso(end)}")

    conn = _open_source(source_path, live_store_paths)
    scratch_path: str | None = None
    try:
        _check_tables(conn)
        frozen_settings = _freeze_settings(conn, start)
        settings = Settings(_FrozenValues(frozen_settings.values), {}, options_path="")
        frozen_profiles, dropped_profiles = _freeze_profiles(conn, start)
        batteries = _effective_configs(settings, frozen_profiles.values, start)
        series, battery_samples, base = _read_series(conn, start, end, batteries)
        plans, dispatch, forecasts = _read_recorded(
            conn, start, end, [config.id for config in batteries]
        )
        warm_start = _warm_start(plans, forecasts, start)
        scratch_path = _backup(conn, scratch_dir)
        truncation = _truncate(
            scratch_path,
            start,
            dropped_profiles=dropped_profiles,
            settings_as_of=frozen_settings.as_of,
            profiles_as_of=frozen_profiles.as_of,
        )
        boundary = _read_boundary(conn, start, end)
    except BaseException:
        conn.close()
        if scratch_path is not None:
            _remove(scratch_path)
        raise
    conn.close()

    asof_sources = {
        **{f"settings:{key}": origin for key, origin in frozen_settings.origins.items()},
        **{
            f"profiles:{kind}/{key}": origin
            for (kind, key), origin in frozen_profiles.origins.items()
        },
    }
    episode = Episode(
        source_path=str(source_path),
        scratch_path=scratch_path,
        start=start,
        end=end,
        step_s=float(step_s),
        settings=settings,
        settings_values=frozen_settings.values,
        profiles=frozen_profiles.values,
        batteries=batteries,
        series=series,
        battery_samples=battery_samples,
        plans=plans,
        dispatch=dispatch,
        forecasts=forecasts,
        warm_start=warm_start,
        boundary=boundary,
        truncation=truncation,
        base=base,
        asof_sources=asof_sources,
        keep_scratch=keep_scratch,
    )
    try:
        episode.store()  # fail early if the scratch copy is not writable
    except Exception:
        episode.close()
        raise
    return episode


# --------------------------------------------------------------------------------- helpers


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _remove(path: str) -> None:
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass


def _identity(path: Path) -> tuple[int, int] | None:
    """The file identity (device, inode), or ``None`` when the path cannot be stat'ed."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _open_source(
    source_path: str | Path, live_store_paths: Sequence[str | Path]
) -> sqlite3.Connection:
    """Open the recording read-only, refusing a live path, a non-store and an unflushed WAL.

    The path is resolved once and every check below uses that resolution: a symlink must not hide
    the target's ``-wal``, and live stores are compared by file identity (``st_dev``/``st_ino``),
    because a hard link -- or a differently spelled path -- defeats a string comparison.
    """
    resolved = Path(source_path).expanduser().resolve()
    live: set[tuple[int, int]] = set()
    for item in live_store_paths:
        identity = _identity(Path(item).expanduser().resolve())
        if identity is not None:
            live.add(identity)
    if not resolved.exists():
        raise EpisodeError(NOT_SQLITE, f"{resolved} does not exist")
    if _identity(resolved) in live:
        raise EpisodeError(LIVE_STORE, f"{resolved} is a live store path")
    wal = resolved.with_name(resolved.name + "-wal")
    if wal.exists() and wal.stat().st_size > 32:
        # A read-only connection cannot checkpoint, and a bare copy of the main file would silently
        # drop these committed frames; refusing is the only honest answer (section 1).
        raise EpisodeError(
            UNFLUSHED_WAL,
            f"{wal} holds frames a read-only connection cannot checkpoint; "
            "copy the store with sqlite3's backup API instead",
        )
    try:
        # ``immutable``: a recording is frozen by definition, and a plain read-only open of a WAL
        # database creates ``-wal``/``-shm`` sidecars next to it -- the "read-only" source would
        # still be touched.  Refusing live paths and unflushed WALs above is what makes this safe.
        # ``as_uri()`` percent-encodes the name, so a ``?``, ``#`` or percent escape in a recording's
        # filename is data rather than URI syntax and cannot make SQLite open a different file than
        # the one that passed the checks above.
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise EpisodeError(NOT_SQLITE, f"cannot open {resolved} read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise EpisodeError(NOT_SQLITE, f"{resolved} is not a readable SQLite store: {exc}") from exc
    return conn


def _check_tables(conn: sqlite3.Connection) -> None:
    names = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing = sorted(_REQUIRED_TABLES - names)
    if missing:
        raise EpisodeError(
            NOT_SQLITE, f"not this application's store; missing: {', '.join(missing)}"
        )


def _fetch(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _json_value(text: Any, *, where: str) -> Any:
    """Decode one stored JSON column, refusing a corrupt payload.

    A valid JSON ``null`` is a recorded value (``None``); unreadable JSON is not, because accepting
    it as ``None`` would silently plan on a default the recording never had.
    """
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise EpisodeError(MALFORMED_JSON, f"{where} is not readable JSON: {exc}") from exc


def _tolerant_moment(text: Any) -> datetime | None:
    """A stored instant for the refusal message; unreadable text still marks the key as known.

    Uses the readers' own exact parse (``parsed_as_of_instant``), so a value that would be refused
    as an instant cannot be quietly rotated backward by a looser parser here.
    """
    return parsed_as_of_instant(text)


#: Rows removed per DELETE statement.  A long append-only history can hold far more rows newer than
#: a window start than a connection has bind variables (999 on many builds), and one placeholder per
#: doomed row would fail the whole scratch copy -- precisely on the long windows this cut exists
#: for.  Deleting in bounded batches keeps the count of placeholders fixed however long the history.
_DELETE_BATCH = 500


def _delete_rowids(connection: sqlite3.Connection, table: str, rowids: list[int]) -> None:
    """Delete rows by their own ids, in batches small enough for any connection to bind."""
    for offset in range(0, len(rowids), _DELETE_BATCH):
        batch = rowids[offset : offset + _DELETE_BATCH]
        connection.execute(
            f"DELETE FROM {table} WHERE rowid IN ({','.join('?' * len(batch))})", batch
        )


def _freeze_settings(conn: sqlite3.Connection, start: datetime) -> _AsOfSettings:
    """Every settings value in force at ``start``, with its origin (SPEC.md 11).

    The table keeps one row per key -- the recording's *end* state -- so a value newer than the
    window is no longer readable at ``start`` from the table alone.  The append-only as-of history
    is where the value in force at ``start`` now comes from.  A key the recording knows only from
    after ``start`` (a first write, or a tombstone) is still refused with the key named: the past is
    never back-filled, and the current row is only used when it is itself at or before ``start``
    (a pre-history recording, or a row written around the choke points).  Within a key the newest
    instant at or before ``start`` wins, ties broken by the newest insertion (a write may be
    backdated, so insertion order is not chronological order), and the chosen row's own timestamp
    is re-read and re-checked against the boundary before it is trusted.
    """
    try:
        snapshot = settings_as_of(conn, start)
        deleted = settings_deleted_as_of(conn, start)
    except ValueError as exc:
        raise EpisodeError(MALFORMED_JSON, str(exc)) from exc
    seen: dict[str, datetime | None] = {}
    if table_exists(conn, "settings_history"):
        for row in _fetch(conn, "SELECT key, at FROM settings_history"):
            key = str(row["key"])
            moment = _tolerant_moment(row["at"])
            if key not in seen or (
                moment is not None and (seen[key] is None or moment > seen[key])
            ):
                seen[key] = moment
    for row in _fetch(conn, "SELECT key, updated_at FROM settings"):
        key = str(row["key"])
        moment = _tolerant_moment(row["updated_at"])
        if key not in seen or (
            moment is not None and (seen[key] is None or moment > seen[key])
        ):
            seen[key] = moment
    as_of: dict[str, tuple[datetime, Any]] = {}
    origins: dict[str, str] = {}
    for key, row in snapshot.items():
        as_of[key] = (row.at, row.value)
        origins[key] = row.origin
    newer = sorted(
        f"{key}@{to_iso(moment) if moment is not None else 'null'}"
        for key, moment in seen.items()
        if key not in snapshot and key not in deleted
    )
    if newer:
        raise EpisodeError(
            NO_VINTAGE,
            f"{len(newer)} settings row(s) newer than {to_iso(start)}: "
            + ", ".join(newer[:5]),
        )
    return _AsOfSettings(as_of, origins)


def _freeze_profiles(
    conn: sqlite3.Connection, start: datetime
) -> tuple[_AsOfProfiles, tuple[tuple[str, str], ...]]:
    """Accepted decision-feeding ``learning_profiles`` payloads, plus the dropped ones.

    Each key's value is its newest as-of history row at or before ``start``; a key with no history
    rows at all falls back to its current row, and then only when that row is itself at or before
    ``start``.  A key whose only rows (writes or tombstones) sit after ``start`` is refused, exactly
    as before.  Two kinds can still be replayed without a value, because the engine rebuilds them
    from rows the replay is allowed to see: the cost-basis ledger resets and rebuilds over its own
    lookback when the stored watermark is gone, and a learned capacity falls back to the configured
    ``capacity_kwh``.  Those keys are DROPPED and reported as a declared approximation; every other
    decision-feeding kind has no rebuild path inside a replay, so it refuses the window rather than
    silently planning on a value from after it.

    The snapshot returned covers every profile kind, not only the decision-feeding ones, so the
    scratch copy can be given exactly the values the engine's own ``profile_get`` reads would have
    seen at ``start`` (the loads' bias, the fetch backoff, the maintenance state).
    """
    keys: dict[tuple[str, str], datetime | None] = {}
    if table_exists(conn, "learning_profiles_history"):
        for row in _fetch(conn, "SELECT kind, key, at FROM learning_profiles_history"):
            found = (str(row["kind"]), str(row["key"]))
            moment = _tolerant_moment(row["at"])
            if found not in keys or (
                moment is not None and (keys[found] is None or moment > keys[found])
            ):
                keys[found] = moment
    for row in _fetch(conn, "SELECT kind, key, updated_at FROM learning_profiles"):
        found = (str(row["kind"]), str(row["key"]))
        moment = _tolerant_moment(row["updated_at"])
        if found not in keys or (
            moment is not None and (keys[found] is None or moment > keys[found])
        ):
            keys[found] = moment
    as_of: dict[tuple[str, str], tuple[datetime, Any]] = {}
    accepted: dict[tuple[str, str], Any] = {}
    origins: dict[tuple[str, str], str] = {}
    dropped: list[tuple[str, str]] = []
    newer: list[str] = []
    for (kind, key), newest in keys.items():
        try:
            row = profile_as_of(conn, kind, key, start)
        except ValueError as exc:
            raise EpisodeError(MALFORMED_JSON, str(exc)) from exc
        if row is not None:
            as_of[(kind, key)] = (row.at, row.value)
            if kind in DECISION_PROFILE_KINDS:
                accepted[(kind, key)] = row.value
                origins[(kind, key)] = row.origin
            continue
        if profile_deleted_as_of(conn, kind, key, start):
            continue  # a tombstone in force at start: the key is absent, not late
        if kind not in DECISION_PROFILE_KINDS:
            continue
        if kind in REBUILDABLE_PROFILE_KINDS:
            dropped.append((kind, key))
        else:
            newer.append(f"{kind}/{key}@{to_iso(newest) if newest is not None else 'null'}")
    if newer:
        raise EpisodeError(
            NO_VINTAGE,
            f"{len(newer)} decision-feeding profile row(s) newer than {to_iso(start)} and with no "
            "rebuild path inside a replay: " + ", ".join(sorted(newer)[:5]),
        )
    return _AsOfProfiles(as_of, accepted, origins), tuple(sorted(dropped))


def _truncate(
    scratch_path: str,
    start: datetime,
    *,
    dropped_profiles: Sequence[tuple[str, str]] = (),
    settings_as_of: Mapping[str, tuple[datetime, Any]] | None = None,
    profiles_as_of: Mapping[tuple[str, str], tuple[datetime, Any]] | None = None,
) -> Truncation:
    """Cut every row the recording wrote after the window start out of the scratch copy.

    The boundary is enforced in the data rather than by trusting each reader to pass an upper bound,
    because production's store never holds a row newer than "now" and several production readers
    depend on that (see :data:`BOUNDARY_COLUMNS`).  A row whose time column is NULL cannot be proven
    to be at or before the boundary, so it is removed too; the source is untouched, so the recording
    stays complete for the episode's own in-memory reads.

    ``settings`` and ``learning_profiles`` need more than a cut.  The engine reads them through the
    scratch store's own ``profile_get`` (the load bias, the fetch backoff, the maintenance and
    control state), so a row the recording wrote after ``start`` may not simply vanish: the as-of
    row must be written back, or the engine would see a key that was in force at ``start`` as
    absent.  The cut compares parsed instants (never stored text), and a row whose timestamp cannot
    be placed exactly is removed rather than left visible.  This is true of a pre-history recording
    too: the same cut and the same write-back of the frozen rows run, and only the history tables'
    own cuts are conditional on the history existing.  History rows after ``start`` are cut as well
    when the tables are present.
    """
    counts: dict[str, int] = {}
    connection = sqlite3.connect(scratch_path, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        present = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        history_path = "settings_history" in present and "learning_profiles_history" in present
        for table, column in BOUNDARY_COLUMNS.items():
            if table not in present:
                continue
            if table in ("settings", "learning_profiles"):
                # The strict cut below is the single owner of these two tables, history or no
                # history, so their cut semantics stay in one place and at the right boundary.
                continue
            bound = start.date().isoformat() if table == "savings_daily" else to_iso(start)
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {column} IS NULL OR {column} >= ?", (bound,)
            )
            removed = max(0, int(cursor.rowcount or 0))
            if removed:
                counts[table] = removed
        # ``settings`` and ``learning_profiles`` are cut by parsed instants rather than by a SQL
        # string comparison, because a stored ``+02:00`` offset sorts on the wrong side of the
        # boundary; an unplaceable timestamp cannot be proven old enough, so it is removed too.
        # A row is kept only when its instant is exactly placeable and not after ``start``, so a
        # fraction that names a real microsecond instant to more digits than that is never lost.
        def prune_after_boundary(table: str, column: str) -> None:
            # Keyed on ``rowid``: the two key tables have no ``id`` column, but every one of the
            # four is an ordinary rowid table (on the history tables ``id`` is that same rowid).
            doomed = [
                int(row["rid"])
                for row in connection.execute(f"SELECT rowid AS rid, {column} FROM {table}")
                if (moment := _tolerant_moment(row[column])) is None or moment > start
            ]
            if not doomed:
                return
            _delete_rowids(connection, table, doomed)
            counts[table] = counts.get(table, 0) + len(doomed)

        for table, column in (
            ("settings", "updated_at"),
            ("learning_profiles", "updated_at"),
        ):
            prune_after_boundary(table, column)
        if history_path:
            for table, column in (
                ("settings_history", "at"),
                ("learning_profiles_history", "at"),
            ):
                prune_after_boundary(table, column)
        for key, (at, value) in (settings_as_of or {}).items():
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                (key, json.dumps(value), to_iso(at)),
            )
        for (kind, key), (at, value) in (profiles_as_of or {}).items():
            connection.execute(
                "INSERT INTO learning_profiles(kind, key, updated_at, payload_json)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET"
                " updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (kind, key, to_iso(at), json.dumps(value)),
            )
        # A declared-dropped key must not survive the cut even if some row of it is old enough:
        # the episode reports it as dropped, so the scratch the engine reads must not hold it.
        for kind, key in dropped_profiles:
            cursor = connection.execute(
                "DELETE FROM learning_profiles WHERE kind = ? AND key = ?", (kind, key)
            )
            removed = max(0, int(cursor.rowcount or 0))
            if removed:
                counts["learning_profiles"] = counts.get("learning_profiles", 0) + removed
        # ``plan_slots`` follows its plan: a surviving pre-window plan may reach past the boundary
        # (its horizon started before the window), and cutting slots only by their own ``t`` would
        # leave orphaned rows of the plans that were just removed -- which then collide with a new
        # plan, because row ids are reused after a delete.  So every slot of a removed plan goes.
        cursor = connection.execute(
            "DELETE FROM plan_slots WHERE plan_id NOT IN (SELECT id FROM plans)"
        )
        removed = max(0, int(cursor.rowcount or 0))
        if removed:
            counts["plan_slots"] = counts.get("plan_slots", 0) + removed
    except sqlite3.Error as exc:
        raise EpisodeError(
            NOT_SQLITE, f"cannot cut the scratch copy at the boundary: {exc}"
        ) from exc
    finally:
        connection.close()
    return Truncation(
        cut_at=start, rows=dict(sorted(counts.items())), dropped_profiles=tuple(dropped_profiles)
    )


def _effective_configs(
    settings: Settings, profiles: Mapping[tuple[str, str], Any], start: datetime
) -> tuple[BatteryConfig, ...]:
    """The battery configs as of ``start``: learned values and manual caps, never today's."""
    limits = clean_manual_limits(settings.get("manual_limits", {}))
    out: list[BatteryConfig] = []
    for config in settings.battery_list():
        updates: dict[str, Any] = {}
        if config.learns_from_measurements:
            # The round trip first: it needs no capacity, and the capacity learner used it too.
            efficiency_status = efficiency_learning.round_trip_status(
                profiles.get((efficiency_learning.PROFILE_KIND, config.id)), start
            )
            eta = efficiency_status.get("eta_charge")
            if eta is not None and 0.0 < float(eta) <= 1.0:
                updates["eta_charge"] = float(eta)
                updates["eta_discharge"] = float(eta)
            status = capacity_learning.capacity_status(
                profiles.get((capacity_learning.PROFILE_KIND, config.id)), start
            )
            learned = status.get("capacity_kwh")
            if learned is not None:
                updates["capacity_kwh"] = float(learned)
        for direction, attribute in (("charge", "max_charge_w"), ("discharge", "max_discharge_w")):
            cap = resolve_manual_limit(limits, config.id, direction, getattr(config, attribute))
            if cap is not None and cap < float(getattr(config, attribute)):
                updates[attribute] = cap
        out.append(replace(config, **updates) if updates else config)
    return tuple(out)


def _finite_kw(value: Any) -> float | None:
    """One recorded number, or ``None`` when the column carries nothing usable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _effective_base(row: Mapping[str, Any]) -> tuple[float, float, str, bool | None] | None:
    """One bucket's pv/load as the planner would have seen it, with its provenance label.

    The measured columns win when both are present; otherwise the engine's own same-tick estimates
    stand in and the bucket is labelled ``derived`` (both) or ``mixed`` (one of each).  The fourth
    element is the recording's own identity check
    ``pv_derived_kw == max(0, load_derived_kw - grid_kw + battery_kw)``: ``True``/``False`` when a
    derived value is used and the whole derived pair is present, ``None`` when the check does not
    apply.  ``None`` as the whole result means the bucket has no base at all.
    """
    pv_measured = _finite_kw(row.get("pv_kw"))
    load_measured = _finite_kw(row.get("load_kw"))
    pv_derived = _finite_kw(row.get("pv_derived_kw"))
    load_derived = _finite_kw(row.get("load_derived_kw"))
    grid = _finite_kw(row.get("grid_kw"))
    battery = _finite_kw(row.get("battery_kw"))
    pv = pv_measured if pv_measured is not None else pv_derived
    load = load_measured if load_measured is not None else load_derived
    if pv is None or load is None:
        return None
    if pv_measured is not None and load_measured is not None:
        return float(pv), float(load), BASE_MEASURED, None
    label = BASE_DERIVED if pv_measured is None and load_measured is None else BASE_MIXED
    check: bool | None = None
    if None not in (pv_derived, load_derived, grid, battery):
        expected = max(0.0, float(load_derived) - float(grid) + float(battery))
        check = abs(float(pv_derived) - expected) <= BASE_IDENTITY_TOLERANCE_KW
    return float(pv), float(load), label, check


def _read_series(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    batteries: Sequence[BatteryConfig],
) -> tuple[
    dict[str, tuple[tuple[datetime, float | None], ...]],
    dict[str, dict[str, tuple[tuple[datetime, float | None], ...]]],
    BaseProvenance,
]:
    """The recorded exogenous series, the per-battery samples and the base's provenance.

    ``actuals`` is a five-minute table, so coverage is demanded per bucket: a missing row or a NULL
    physical column is refused, because a gap would otherwise be read as zero import.  ``pv_kw`` and
    ``load_kw`` may be answered by the derived columns (:func:`_effective_base`), and every bucket
    records which of the two was used; their values are the *effective* ones, so a reader that goes
    through :meth:`Episode.base_at` sees the same numbers the planner did.  A bucket with neither a
    measured nor a derived base still refuses the window.
    """
    first = quarter_floor(start, ACTUALS_MINUTES)
    rows = _fetch(
        conn,
        "SELECT * FROM actuals WHERE ts >= ? AND ts < ? ORDER BY ts",
        (to_iso(first), to_iso(end)),
    )
    index = {str(row["ts"]): row for row in rows}
    effective = {
        str(row["ts"]): _effective_base(row) for row in rows
    }
    missing: list[str] = []
    source: dict[datetime, str] = {}
    refused: list[datetime] = []
    bucket = first
    while bucket < end:
        key = to_iso(bucket)
        row = index.get(key)
        base = effective.get(key)
        # ``row.get``: a recording that predates a required column is incomplete for the same reason
        # a NULL one is, and must refuse the window with that slug instead of raising a KeyError.
        if row is None or base is None or any(
            row.get(column) is None for column in DIRECT_REQUIRED_SERIES
        ):
            missing.append(key)
        else:
            label, check = base[2], base[3]
            if check is False:
                refused.append(bucket)
            else:
                source[bucket] = label
        bucket += timedelta(minutes=ACTUALS_MINUTES)
    if missing:
        raise EpisodeError(
            INCOMPLETE_ACTUALS,
            f"{len(missing)} incomplete {ACTUALS_MINUTES}-minute bucket(s) in the window; "
            f"first {missing[0]}",
        )
    series = {
        name: tuple(
            (from_iso(row["ts"]), _series_value(name, row, effective[str(row["ts"])]))
            for row in rows
        )
        for name in RECORDED_SERIES
    }
    provenance = BaseProvenance(source=dict(source), refused=tuple(refused))
    return series, _read_battery_samples(conn, first, end, batteries), provenance


def _series_value(
    name: str, row: Mapping[str, Any], base: tuple[float, float, str, bool | None] | None
) -> Any:
    """One series point: the effective pv/load value, or the recorded column for anything else.

    A column the recording does not have (an ``actuals`` table from before ``price_sell`` existed)
    has no point to report, exactly as the accounting's schema-aware price resolution treats it: no
    error, and no value invented -- the recorded plan slot is the documented fallback there, and the
    engine's own pricing never reads these columns.  A NULL value reads the same way, because a
    series point cannot carry "column absent" separately from "value absent".
    """
    if name not in ("pv_kw", "load_kw"):
        return row.get(name)
    if base is None:
        return None
    return base[0] if name == "pv_kw" else base[1]


def _sample_spacing_s(
    points: tuple[tuple[datetime, float | None], ...] | None, now: datetime, fallback_s: float
) -> float:
    """The recording's own sample spacing as it stood at ``now``: the median gap between the
    consecutive samples visible then.

    Judging a sample against the cadence it was recorded at -- rather than a constant from today's
    settings -- keeps a stale reading stale for the right reason.  Only samples at or before ``now``
    count, so a replay can never let an arrival it could not yet have seen change the verdict on an
    earlier snapshot (the as-of contract, section 1).  A visible series with fewer than two points
    has no measurable cadence, so the recording's own bucket grid is the honest fallback.
    """
    visible = [point for point in (points or ()) if point[0] <= now]
    if len(visible) < 2:
        return fallback_s
    gaps = sorted(
        (later[0] - earlier[0]).total_seconds()
        for earlier, later in zip(visible, visible[1:])
    )
    middle = gaps[len(gaps) // 2]
    return middle if middle > 0 else fallback_s


def _read_battery_samples(
    conn: sqlite3.Connection,
    first: datetime,
    end: datetime,
    batteries: Sequence[BatteryConfig],
) -> dict[str, dict[str, tuple[tuple[datetime, float | None], ...]]]:
    """The latest pre-window sample plus the window's samples, per battery and role.

    Loading the last row before the window matters: restricting extraction to the first window
    bucket would lose the sample the recording already had, and the first replay instant would look
    like the pack appeared from nowhere.  The sample keeps its own timestamp either way.
    """
    samples: dict[str, dict[str, tuple[tuple[datetime, float | None], ...]]] = {}
    for config in batteries:
        for name, role in (
            ("soc_pct", f"battery_soc:{config.id}"),
            ("power_w", f"battery_power:{config.id}"),
        ):
            rows = _fetch(
                conn,
                "SELECT ts, value FROM measurements WHERE entity_role = ? AND ts < ?"
                " ORDER BY ts DESC LIMIT 1",
                (role, to_iso(first)),
            )
            rows += _fetch(
                conn,
                "SELECT ts, value FROM measurements WHERE ts >= ? AND ts < ? AND entity_role = ?"
                " ORDER BY ts",
                (to_iso(first), to_iso(end), role),
            )
            if rows:
                rows.sort(key=lambda row: from_iso(row["ts"]))
                samples.setdefault(config.id, {})[name] = tuple(
                    (from_iso(row["ts"]), None if row["value"] is None else float(row["value"]))
                    for row in rows
                )
    return samples


def _read_recorded(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    battery_ids: Sequence[str] = (),
) -> tuple[
    tuple[Plan, ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    """The recorded plan, dispatch and forecast history of the window.

    Plans are ordered by ``(created_at, id)`` -- the accepted as-of rule is *newest
    ``created_at``*, and the row id is only a tie-breaker; sorting by insertion order would pick an
    older plan whenever a later insert carries an earlier timestamp.

    The dispatch history is ordered by ``(ts, id)`` and carries, per battery, the newest row at or
    before the window start as well: the setpoint in force when the window opens was written before
    it, and the recorded-policy leg must read the recording's own value rather than hold one the
    recording never re-issued.
    """
    plan_rows: list[tuple[datetime, int, dict[str, Any]]] = []
    for row in _fetch(conn, "SELECT * FROM plans ORDER BY id"):
        created = from_iso(row["created_at"])
        if created is not None and created <= end:
            plan_rows.append((created, int(row["id"]), row))
    plan_rows.sort(key=lambda item: (item[0], item[1]))
    slots: dict[int, list[dict[str, Any]]] = {}
    if plan_rows:
        ids = [plan_id for _, plan_id, _ in plan_rows]
        marks = ",".join("?" * len(ids))
        for row in _fetch(
            conn, f"SELECT * FROM plan_slots WHERE plan_id IN ({marks}) ORDER BY plan_id, t", ids
        ):
            slots.setdefault(int(row["plan_id"]), []).append(row)
    plans = tuple(
        _plan_from_rows(row, slots.get(plan_id, [])) for _, plan_id, row in plan_rows
    )
    dispatch = _recorded_dispatch(conn, start, end, battery_ids)
    forecasts = tuple(
        _fetch(
            conn,
            "SELECT * FROM forecast_archive WHERE target_ts >= ? AND target_ts < ?"
            " ORDER BY kind, source, target_ts",
            (to_iso(start), to_iso(end)),
        )
    )
    return plans, dispatch, forecasts


def _dispatch_order(row: Mapping[str, Any]) -> tuple[datetime, int]:
    """As-of order: a row takes effect at its own timestamp, its id only breaking ties."""
    moment = from_iso(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)
    try:
        identifier = int(row.get("id") or 0)
    except (TypeError, ValueError):
        identifier = 0
    return moment, identifier


def _recorded_dispatch(
    conn: sqlite3.Connection, start: datetime, end: datetime, battery_ids: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    """The window's dispatch rows plus each battery's newest row at or before the start."""
    in_window = tuple(
        _fetch(
            conn,
            "SELECT * FROM dispatch_log WHERE ts >= ? AND ts < ? ORDER BY id",
            (to_iso(start), to_iso(end)),
        )
    )
    known = {int(row["id"]) for row in in_window if row.get("id") is not None}
    seeds: list[dict[str, Any]] = []
    for battery_id in sorted({str(item) for item in battery_ids if item}):
        row = conn.execute(
            "SELECT * FROM dispatch_log WHERE battery_id = ? AND ts <= ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (battery_id, to_iso(start)),
        ).fetchone()
        if row is not None and int(row["id"]) not in known:
            seeds.append(dict(row))
    return tuple(sorted((*seeds, *in_window), key=_dispatch_order))


def _warm_start(
    plans: Sequence[Plan], forecasts: Sequence[Mapping[str, Any]], start: datetime
) -> WarmStart:
    active: Plan | None = None
    # ``plans`` is ordered by (created_at, id), so the last one at or before ``start`` is the
    # accepted as-of pick; iterating in that order keeps the rule visible.
    for plan in plans:
        if plan.generated_at <= start:
            active = plan
    if active is None:
        # A forecast only seeds the window when it was issued before the start *and* targets a slot
        # the window can still use; an issue for an elapsed slot is not a vintage for this replay.
        issued = [
            from_iso(row.get("issued_at"))
            for row in forecasts
            if (target := from_iso(row.get("target_ts"))) is not None and target >= start
        ]
        if not any(moment is not None and moment <= start for moment in issued):
            raise EpisodeError(
                NO_VINTAGE,
                f"no plans row created at or before {to_iso(start)} and no archived forecast "
                "issued at or before it",
            )
    return WarmStart(
        plan=active,
        last_replan_at=active.generated_at if active is not None else None,
        backoff_s=float(MIN_REPLAN_GAP_S),
    )


def _backup(conn: sqlite3.Connection, scratch_dir: str | Path | None) -> str:
    """Copy the source into a writable scratch file with SQLite's own backup API."""
    handle, path = tempfile.mkstemp(prefix="replay-", suffix=".sqlite", dir=scratch_dir)
    os.close(handle)
    try:
        target = sqlite3.connect(path)
        try:
            conn.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        _remove(path)
        raise EpisodeError(NOT_SQLITE, f"cannot copy the recording: {exc}") from exc
    return path


def _read_boundary(conn: sqlite3.Connection, start: datetime, end: datetime) -> Boundary:
    """The cut taken after the copy: max ids, and the source's upsert-by-key rows.

    The upsert rows are scoped to the window (and the day range for ``savings_daily``) because only
    keys the replay can touch need a source value to compare against; ``settings`` and
    ``learning_profiles`` are whole-table, since the engine reads those everywhere.
    """
    first = quarter_floor(start, ACTUALS_MINUTES)
    day_start = start.date().isoformat()
    day_end = end.date().isoformat()
    source_rows: dict[str, tuple[Mapping[str, Any], ...]] = {
        "actuals": tuple(
            _fetch(
                conn,
                "SELECT * FROM actuals WHERE ts >= ? AND ts < ? ORDER BY ts",
                (to_iso(first), to_iso(end)),
            )
        ),
        "measurements": tuple(
            _fetch(
                conn,
                "SELECT * FROM measurements WHERE ts >= ? AND ts < ? ORDER BY ts",
                (to_iso(first), to_iso(end)),
            )
        ),
        "savings_daily": tuple(
            _fetch(
                conn,
                "SELECT * FROM savings_daily WHERE day >= ? AND day <= ? ORDER BY day",
                (day_start, day_end),
            )
        ),
        "learning_profiles": tuple(
            _fetch(conn, "SELECT * FROM learning_profiles ORDER BY kind, key")
        ),
        "forecast_archive": tuple(
            _fetch(
                conn,
                "SELECT * FROM forecast_archive WHERE target_ts >= ? AND target_ts < ?"
                " ORDER BY kind, source, target_ts",
                (to_iso(first), to_iso(end)),
            )
        ),
        "settings": tuple(_fetch(conn, "SELECT * FROM settings ORDER BY key")),
    }
    return Boundary(
        plan_id=_max_id(conn, "plans"),
        dispatch_id=_max_id(conn, "dispatch_log"),
        event_id=_max_id(conn, "events"),
        source_rows=source_rows,
    )


def _max_id(conn: sqlite3.Connection, table: str) -> int | None:
    row = conn.execute(f"SELECT MAX(id) AS max_id FROM {table}").fetchone()
    if row is None or row["max_id"] is None:
        return None
    return int(row["max_id"])


def _plan_from_rows(row: Mapping[str, Any], slots: Sequence[Mapping[str, Any]]) -> Plan:
    """Rebuild one recorded ``plans`` row, mirroring ``Store._plan_from_row``.

    Duplicated on purpose: reading the plan out of the scratch copy would make the engine's own
    writes part of the recorded history it is compared against (section 1).
    """
    summary_raw = dict(
        _json_value(row["summary_json"], where=f"plans[{row['id']}].summary_json") or {}
    )
    infeasible_reason = summary_raw.pop("infeasible_reason", None)
    slot_minutes = int(summary_raw.pop("slot_minutes", 15) or 15)
    sources = summary_raw.pop("sources", {}) or {}
    known = {name for name in PlanSummary().__dict__}
    summary = PlanSummary(**{key: value for key, value in summary_raw.items() if key in known})
    plan_slots = [
        PlanSlot(
            t=from_iso(item["t"]),
            buy=float(item["buy"] or 0.0),
            sell=float(item["sell"] or 0.0),
            pv_kw=float(item["pv_kw"] or 0.0),
            load_kw=float(item["load_kw"] or 0.0),
            soc_kwh=float(item["soc_kwh"] or 0.0),
            power_w=float(item["power_w"] or 0.0),
            action=Action(item["action"]) if item["action"] else Action.HOLD,
            reason=item["reason"] or "",
        )
        for item in slots
    ]
    if len(plan_slots) > 1:
        delta = plan_slots[1].t - plan_slots[0].t
        if delta.total_seconds() > 0:
            slot_minutes = int(delta.total_seconds() // 60)
    return Plan(
        generated_at=from_iso(row["created_at"]),
        horizon_start=from_iso(row["horizon_start"]),
        horizon_end=from_iso(row["horizon_end"]),
        mode=row["mode"] or Mode.ADVISORY.value,
        dry_run=bool(row["dry_run"]),
        feasible=bool(row["feasible"]),
        summary=summary,
        batteries=_json_value(row["batteries_json"], where=f"plans[{row['id']}].batteries_json")
        or [],
        slots=plan_slots,
        infeasible_reason=infeasible_reason,
        slot_minutes=slot_minutes,
        sources=sources,
        maintenance=getattr(summary, "maintenance", None),
    )


class _FrozenValues:
    """A ``Store``-shaped read view over the episode's frozen settings.

    ``Settings`` only ever calls ``get_setting``/``get_settings`` on its store, so a plain mapping
    is enough -- and it keeps the frozen values independent of the scratch copy, which the engine
    writes.
    """

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def get_settings(self) -> dict[str, Any]:
        return dict(self._values)
