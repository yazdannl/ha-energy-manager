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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import capacity as capacity_learning
from .. import cost_basis as cost_basis_ledger
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
from ..store import Store

#: Refusal reasons; stable, machine-readable slugs (docs/REPLAY_HARNESS.md section 6).
LIVE_STORE = "live_store"
NOT_SQLITE = "not_sqlite"
UNFLUSHED_WAL = "unflushed_wal"
NO_VINTAGE = "no_vintage"
INCOMPLETE_ACTUALS = "incomplete_actuals"
MALFORMED_JSON = "malformed_json"
WINDOW = "window"
NON_ANTICIPATION = "non_anticipation"

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

#: Tables a recording must have; anything else is not this application's store.
_REQUIRED_TABLES = frozenset(("plan_slots", *APPEND_ONLY_TABLES, *UPSERT_BY_KEY_TABLES))


class EpisodeError(RuntimeError):
    """A refused episode, with a stable ``reason`` slug (never a silent default)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


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

    The scratch copy is the engine's store; every recorded value is read from the source before the
    copy is created and is exposed afterwards only through :meth:`at` snapshots.
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
        self.now = start
        self._keep_scratch = keep_scratch
        self._store: Store | None = None

    # -- lifecycle --------------------------------------------------------------

    def store(self) -> Store:
        """The engine's writable :class:`~app.store.Store`, on the scratch copy."""
        if self._store is None:
            store = Store(self.scratch_path)
            store.connection  # open and migrate now: a schema failure is an episode error
            self._store = store
        return self._store

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
        settings_values = _freeze_settings(conn, start)
        settings = Settings(_FrozenValues(settings_values), {}, options_path="")
        profiles = _freeze_profiles(conn, start)
        batteries = _effective_configs(settings, profiles, start)
        series, battery_samples = _read_series(conn, start, end, batteries)
        plans, dispatch, forecasts = _read_recorded(conn, start, end)
        warm_start = _warm_start(plans, forecasts, start)
        scratch_path = _backup(conn, scratch_dir)
        boundary = _read_boundary(conn, start, end)
    except BaseException:
        conn.close()
        if scratch_path is not None:
            _remove(scratch_path)
        raise
    conn.close()

    episode = Episode(
        source_path=str(source_path),
        scratch_path=scratch_path,
        start=start,
        end=end,
        step_s=float(step_s),
        settings=settings,
        settings_values=settings_values,
        profiles=profiles,
        batteries=batteries,
        series=series,
        battery_samples=battery_samples,
        plans=plans,
        dispatch=dispatch,
        forecasts=forecasts,
        warm_start=warm_start,
        boundary=boundary,
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


def _freeze_settings(conn: sqlite3.Connection, start: datetime) -> dict[str, Any]:
    """Every settings value with ``updated_at <= start``; anything newer is refused.

    The table keeps one row per key, so a newer row is the recording's *end* state and there is no
    older value to fall back to: using it would give the replay knowledge from after the window, and
    dropping it silently would change what the engine plans on (section 1).
    """
    values: dict[str, Any] = {}
    newer: list[str] = []
    for row in _fetch(conn, "SELECT key, value_json, updated_at FROM settings"):
        updated = from_iso(row["updated_at"])
        if updated is not None and updated <= start:
            key = str(row["key"])
            values[key] = _json_value(row["value_json"], where=f"settings[{key}].value_json")
        else:
            newer.append(f"{row['key']}@{row['updated_at']}")
    if newer:
        raise EpisodeError(
            NO_VINTAGE,
            f"{len(newer)} settings row(s) newer than {to_iso(start)}: "
            + ", ".join(sorted(newer)[:5]),
        )
    return values


def _freeze_profiles(conn: sqlite3.Connection, start: datetime) -> dict[tuple[str, str], Any]:
    """Accepted decision-feeding ``learning_profiles`` payloads; newer rows are refused."""
    accepted: dict[tuple[str, str], Any] = {}
    newer: list[str] = []
    for row in _fetch(conn, "SELECT kind, key, updated_at, payload_json FROM learning_profiles"):
        kind = str(row["kind"])
        if kind not in DECISION_PROFILE_KINDS:
            continue
        key = str(row["key"])
        updated = from_iso(row["updated_at"])
        if updated is not None and updated <= start:
            accepted[(kind, key)] = _json_value(
                row["payload_json"], where=f"learning_profiles[{kind}/{key}].payload_json"
            )
        else:
            newer.append(f"{kind}/{key}@{row['updated_at']}")
    if newer:
        raise EpisodeError(
            NO_VINTAGE,
            f"{len(newer)} decision-feeding profile row(s) newer than {to_iso(start)}: "
            + ", ".join(sorted(newer)[:5]),
        )
    return accepted


def _effective_configs(
    settings: Settings, profiles: Mapping[tuple[str, str], Any], start: datetime
) -> tuple[BatteryConfig, ...]:
    """The battery configs as of ``start``: learned capacity and manual caps, never today's."""
    limits = clean_manual_limits(settings.get("manual_limits", {}))
    out: list[BatteryConfig] = []
    for config in settings.battery_list():
        updates: dict[str, Any] = {}
        if config.capacity_source == "learned":
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


def _read_series(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    batteries: Sequence[BatteryConfig],
) -> tuple[
    dict[str, tuple[tuple[datetime, float | None], ...]],
    dict[str, dict[str, tuple[tuple[datetime, float | None], ...]]],
]:
    """The recorded exogenous series and the per-battery samples of the window.

    ``actuals`` is a five-minute table, so coverage is demanded per bucket: a missing row or a NULL
    physical column is refused, because a gap would otherwise be read as zero import.
    """
    first = quarter_floor(start, ACTUALS_MINUTES)
    rows = _fetch(
        conn,
        "SELECT * FROM actuals WHERE ts >= ? AND ts < ? ORDER BY ts",
        (to_iso(first), to_iso(end)),
    )
    index = {str(row["ts"]): row for row in rows}
    missing: list[str] = []
    bucket = first
    while bucket < end:
        key = to_iso(bucket)
        row = index.get(key)
        if row is None or any(row[column] is None for column in REQUIRED_SERIES):
            missing.append(key)
        bucket += timedelta(minutes=ACTUALS_MINUTES)
    if missing:
        raise EpisodeError(
            INCOMPLETE_ACTUALS,
            f"{len(missing)} incomplete {ACTUALS_MINUTES}-minute bucket(s) in the window; "
            f"first {missing[0]}",
        )
    series = {
        name: tuple((from_iso(row["ts"]), row[name]) for row in rows)
        for name in RECORDED_SERIES
    }
    return series, _read_battery_samples(conn, first, end, batteries)


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
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> tuple[
    tuple[Plan, ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    """The recorded plan, dispatch and forecast history of the window.

    Plans are ordered by ``(created_at, id)`` -- the accepted as-of rule is *newest
    ``created_at``*, and the row id is only a tie-breaker; sorting by insertion order would pick an
    older plan whenever a later insert carries an earlier timestamp.
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
    dispatch = tuple(
        _fetch(
            conn,
            "SELECT * FROM dispatch_log WHERE ts >= ? AND ts < ? ORDER BY id",
            (to_iso(start), to_iso(end)),
        )
    )
    forecasts = tuple(
        _fetch(
            conn,
            "SELECT * FROM forecast_archive WHERE target_ts >= ? AND target_ts < ?"
            " ORDER BY kind, source, target_ts",
            (to_iso(start), to_iso(end)),
        )
    )
    return plans, dispatch, forecasts


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
