"""SQLite persistence for the Smart Energy Manager (SPEC.md section 11).

Design notes (all mandated by SPEC.md section 11 unless stated otherwise):

* WAL journal, ``synchronous=NORMAL``, ``busy_timeout=5000``.
* Thread safety: **one connection per thread** (``threading.local``). SQLite serialises writers through
  the WAL + busy timeout, so no global lock is needed; every multi-statement write uses an explicit
  ``BEGIN IMMEDIATE`` transaction.
* Migrations are a forward-only ordered list; the applied version lives in ``schema_version``.
* Retention trimming is explicit and documented in :meth:`Store.trim`. Savings baselines must be
  computed *before* trimming (SPEC.md section 11); this module does not trim on its own.
* Time columns are ISO-8601 strings in UTC (``models.to_iso``).

ponytail: no ORM, no connection pool, no query builder. Ceiling: a single-process add-on reading and
writing a few tens of thousands of rows. Upgrade path: add indexes only if a query is measured slow.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .models import (
    Action,
    DispatchResult,
    Measurement,
    Plan,
    PlanSlot,
    PlanSummary,
    from_iso,
    to_iso,
    utcnow,
)

__all__ = [
    "AsOfValue",
    "DEFAULT_RETENTION",
    "SCHEMA_VERSION",
    "Store",
    "parsed_as_of_instant",
    "profile_as_of",
    "profile_deleted_as_of",
    "settings_as_of",
    "settings_deleted_as_of",
    "table_exists",
]

SCHEMA_VERSION = 6

#: One migration step: a SQL statement, or a Python step for work SQL cannot express (the history
#: seed parses the instant it copies, and a stored ``+02:00`` offset is not a string operation).
MigrationStep = str | Callable[[sqlite3.Connection], None]


def _canonical_utc(moment: datetime) -> str:
    """One canonical history stamp: UTC, at the microsecond precision a datetime really holds.

    ``to_iso`` preserves whatever offset a caller supplied, which is fine for a row's own
    ``updated_at`` but not for the history: the readers must be able to compare instants, and a
    stored ``+02:00`` must not sort like 08:00 when it means 06:00.  Fractional seconds are kept --
    truncating them to whole seconds would move the instant earlier and make a payload eligible
    inside the second it was written -- and the result is verified to re-parse to the same instant,
    so an input this form cannot represent exactly is refused instead of being rounded.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).isoformat()
    if from_iso(stamp) != moment:
        raise ValueError(f"cannot represent {moment!r} as an instant exactly")
    return stamp


def _seed_history(conn: sqlite3.Connection) -> None:
    """Migration 5, part two: give every existing row its last-write instant as history.

    Python rather than SQL because the copied ``updated_at`` may carry a non-UTC offset: the
    instant is parsed with the same parser the readers use and stored canonically.  A timestamp
    that cannot be read -- or whose fraction would lose precision -- seeds nothing: an instant we
    cannot place exactly is not one we may invent, while one we can place exactly is never dropped
    (data loss is worse than a refusal).  An existing history row for the key is never overwritten
    (the statement is idempotent).
    """
    for key, updated_at, value_json in conn.execute(
        "SELECT key, updated_at, value_json FROM settings"
    ).fetchall():
        moment = parsed_as_of_instant(updated_at)
        if moment is None or conn.execute(
            "SELECT 1 FROM settings_history WHERE key = ? LIMIT 1", (key,)
        ).fetchone() is not None:
            continue
        try:
            stamp = _canonical_utc(moment)
        except ValueError:
            continue  # an instant we cannot represent exactly seeds nothing
        conn.execute(
            "INSERT INTO settings_history(key, at, value_json, origin) VALUES(?, ?, ?, 'seed')",
            (key, stamp, value_json),
        )
    for kind, key, updated_at, payload_json in conn.execute(
        "SELECT kind, key, updated_at, payload_json FROM learning_profiles"
    ).fetchall():
        moment = parsed_as_of_instant(updated_at)
        if moment is None or conn.execute(
            "SELECT 1 FROM learning_profiles_history WHERE kind = ? AND key = ? LIMIT 1",
            (kind, key),
        ).fetchone() is not None:
            continue
        try:
            stamp = _canonical_utc(moment)
        except ValueError:
            continue  # an instant we cannot represent exactly seeds nothing
        conn.execute(
            "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
            " VALUES(?, ?, ?, ?, 'seed')",
            (kind, key, stamp, payload_json),
        )


def _add_columns_if_missing(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """Add only the columns an ``ALTER TABLE`` would duplicate.

    Plain ``ALTER TABLE … ADD COLUMN`` is not idempotent, and migration steps can legitimately run
    twice: a replay recording may claim a version older than the file really is (a scratch DB
    restored from a newer build, SPEC 14 R3), and the forward-only runner then re-applies the
    later steps against columns that already exist.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, kind in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


MIGRATIONS: list[tuple[int, tuple[MigrationStep, ...]]] = [
    # Forward-only. Append new (version, steps) entries; never edit a released entry.
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                horizon_start TEXT,
                horizon_end TEXT,
                mode TEXT,
                dry_run INTEGER,
                feasible INTEGER,
                summary_json TEXT,
                batteries_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS plan_slots (
                plan_id INTEGER,
                t TEXT,
                buy REAL,
                sell REAL,
                pv_kw REAL,
                load_kw REAL,
                soc_kwh REAL,
                power_w REAL,
                action TEXT,
                reason TEXT,
                PRIMARY KEY (plan_id, t)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS dispatch_log (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                battery_id TEXT,
                requested_w REAL,
                applied_w REAL,
                soc_pct REAL,
                mode TEXT,
                dry_run INTEGER,
                result TEXT,
                code TEXT,
                message TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS measurements (
                ts TEXT,
                entity_role TEXT,
                value REAL,
                unit TEXT,
                PRIMARY KEY (ts, entity_role)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS actuals (
                ts TEXT PRIMARY KEY,
                pv_kw REAL,
                load_kw REAL,
                grid_kw REAL,
                battery_kw REAL,
                soc_kwh REAL,
                price_buy REAL,
                plan_power_w REAL,
                dispatch_power_w REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS forecast_cache (
                kind TEXT,
                key TEXT,
                fetched_at TEXT,
                payload_json TEXT,
                PRIMARY KEY (kind, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_profiles (
                kind TEXT,
                key TEXT,
                updated_at TEXT,
                payload_json TEXT,
                PRIMARY KEY (kind, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS savings_daily (
                day TEXT PRIMARY KEY,
                import_kwh REAL,
                export_kwh REAL,
                cost_eur REAL,
                revenue_eur REAL,
                baseline_cost_eur REAL,
                savings_eur REAL,
                pv_kwh REAL,
                battery_cycles REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                level TEXT,
                code TEXT,
                message TEXT,
                payload_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT,
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_dispatch_log_ts ON dispatch_log(ts)",
            "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
            "CREATE INDEX IF NOT EXISTS idx_measurements_role_ts ON measurements(entity_role, ts)",
            "CREATE INDEX IF NOT EXISTS idx_actuals_ts ON actuals(ts)",
        ),
    ),
    (
        2,
        (
            # 0.13.0 measured savings: measured AC battery flows, their value at flow time, and the
            # per-day coverage flags for the series the accounting was built from.  Forward-only
            # ALTERs: old rows keep their NULLs, which the aggregator reports as unknown.
            "ALTER TABLE savings_daily ADD COLUMN charged_kwh REAL",
            "ALTER TABLE savings_daily ADD COLUMN discharged_kwh REAL",
            "ALTER TABLE savings_daily ADD COLUMN charge_cost_eur REAL",
            "ALTER TABLE savings_daily ADD COLUMN discharge_value_eur REAL",
            "ALTER TABLE savings_daily ADD COLUMN grid_measured INTEGER",
            "ALTER TABLE savings_daily ADD COLUMN load_measured INTEGER",
            "ALTER TABLE savings_daily ADD COLUMN pv_measured INTEGER",
            "ALTER TABLE savings_daily ADD COLUMN battery_measured INTEGER",
            # Sell price at measurement time, so a measured discharge can be priced the same way a
            # measured import already is (price_buy).
            "ALTER TABLE actuals ADD COLUMN price_sell REAL",
        ),
    ),
    (
        3,
        (
            # 0.22.0 forecast archive (SPEC.md 7.4): what each provider predicted for each slot, so
            # forecast skill becomes a measurement rather than an impression, and a change to the
            # learned correction can be accepted or rejected on evidence.
            """
            CREATE TABLE IF NOT EXISTS forecast_archive (
                kind TEXT,
                source TEXT,
                target_ts TEXT,
                issued_at TEXT,
                value REAL,
                uncertainty_pct REAL,
                PRIMARY KEY (kind, source, target_ts)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_forecast_archive_target ON forecast_archive(target_ts)",
            "CREATE INDEX IF NOT EXISTS idx_forecast_archive_issued ON forecast_archive(issued_at)",
        ),
    ),
    (
        4,
        (
            # 0.24.0 derived load and pv (SPEC.md 7.2 tier 3b): what the add-on can infer when a role
            # has no sensor at all. Kept in their own columns rather than written into ``load_kw`` and
            # ``pv_kw``, because an inferred value and a measured one must stay tellable apart: the
            # fine-tune head corrects a model with these, and a correction fitted on a number the
            # add-on produced itself is only as good as the arithmetic behind it. Rows written before
            # this release keep NULLs, so nothing is retroactively claimed to have been inferred.
            "ALTER TABLE actuals ADD COLUMN pv_derived_kw REAL",
            "ALTER TABLE actuals ADD COLUMN load_derived_kw REAL",
        ),
    ),
    (
        5,
        (
            # 0.27.0 as-of history (SPEC.md 11; docs/REPLAY_HARNESS.md section 1): the replay can
            # only freeze a value that was in force at an arbitrary past instant if the recording
            # kept more than the end state.  ``settings`` and ``learning_profiles`` stay exactly as
            # they were -- one upserted row per key -- and every write or delete through the store's
            # own writers also appends the value here, so the newest row at or before an instant is
            # the value that was in force then.  ``origin`` is 'write' for a post-migration write,
            # 'seed' for the upgrade's copy of a row that already existed, and 'delete' for a
            # tombstone.  The seed uses each row's existing ``updated_at`` as its only known 'at',
            # parsed and stored canonically here, so a window that starts before a key's newest
            # write still refuses (no_vintage): the past before this upgrade is not knowable, only
            # the last write is.  Rows with a NULL or unreadable ``updated_at`` cannot be placed and
            # seed nothing.
            """
            CREATE TABLE IF NOT EXISTS settings_history (
                id INTEGER PRIMARY KEY,
                key TEXT NOT NULL,
                at TEXT NOT NULL,
                value_json TEXT,
                origin TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_profiles_history (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                at TEXT NOT NULL,
                payload_json TEXT,
                origin TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_settings_history_key_at"
            " ON settings_history(key, at)",
            "CREATE INDEX IF NOT EXISTS idx_profiles_history_key_at"
            " ON learning_profiles_history(kind, key, at)",
            # The seed is Python (below) so the copied instant is parsed and stored canonically.
            _seed_history,
        ),
    ),
    (
        6,
        (
            # 0.30.0 gap-tolerant savings: how much sampled time each stored total actually covers.
            # A single unreadable interval used to null out a whole day's baseline and battery
            # flows, which on real hardware left most days unknowable.  The accounting now sums the
            # intervals it could price and states their coverage in hours instead of discarding the
            # day; ``savings_hours`` is the time behind ``baseline_cost_eur``/``savings_eur``,
            # ``battery_hours`` the time behind the measured flows, ``grid_hours`` the metered time
            # behind import/export, and ``total_hours`` the sampled time they are a share of.
            # Rows written before this migration keep NULLs: their coverage is unknown, and the
            # aggregator reports them as legacy rather than pretending they covered 0 hours.
            # Idempotent by inspection: a replay recording that only *claims* an older version than
            # its file really is (a scratch DB restored from a newer build, SPEC 14 R3) re-runs the
            # migration against columns that already exist, and must not fail on them.
            lambda conn: _add_columns_if_missing(
                conn,
                "savings_daily",
                {
                    "total_hours": "REAL",
                    "grid_hours": "REAL",
                    "savings_hours": "REAL",
                    "battery_hours": "REAL",
                },
            ),
        ),
    ),
]


#: Retention policy used by :meth:`Store.trim` (SPEC.md section 11).
DEFAULT_RETENTION = {
    "measurements_days": 400,
    "actuals_days": 400,
    "dispatch_rows": 20_000,
    "events_rows": 5_000,
    "plans_rows": 200,  # ~2 days at the default 15-minute replan cadence
    # Archived forecasts live only as long as the actuals they are scored against: once the measured
    # series is trimmed, an archived prediction has nothing left to be compared with, and it is the
    # comparison that is worth keeping, not the prediction.
    "forecast_archive_days": 400,
}

_ACTUAL_COLUMNS = (
    "pv_kw",
    "load_kw",
    "grid_kw",
    "battery_kw",
    "soc_kwh",
    "price_buy",
    "price_sell",
    "plan_power_w",
    "dispatch_power_w",
    # Inferred rather than measured (0.24.0). Distinct columns on purpose: the fine-tune head learns
    # from these, and it must be able to say which of the two it learned from.
    "pv_derived_kw",
    "load_derived_kw",
)

#: Which measured ``actuals`` column scores each archived forecast kind. A kind absent here is a
#: programming error, not an empty result: reporting "no error" for a series nothing measured would
#: be the most flattering possible bug.
_ARCHIVE_ACTUAL_COLUMN = {
    "solar": "pv_kw",
    "load": "load_kw",
}

_SAVINGS_COLUMNS = (
    "import_kwh",
    "export_kwh",
    "cost_eur",
    "revenue_eur",
    "baseline_cost_eur",
    "savings_eur",
    "pv_kwh",
    "battery_cycles",
    "charged_kwh",
    "discharged_kwh",
    "charge_cost_eur",
    "discharge_value_eur",
    "grid_measured",
    "load_measured",
    "pv_measured",
    "battery_measured",
    "total_hours",
    "grid_hours",
    "savings_hours",
    "battery_hours",
)


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _to_ts(value: datetime | str | None, default: datetime | None = None) -> str | None:
    if value is None:
        value = default
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return to_iso(value)


def _write_stamps(value: datetime | None) -> tuple[str, str]:
    """A write's two stamps: the current row's (unchanged) and the history's canonical UTC one.

    The current row keeps exactly the representation :func:`_to_ts` always produced, because its
    ``updated_at`` is read by production.  The history is a new column, and it is normalised here
    so its instants are comparable without a parser having to guess an offset's meaning.
    """
    moment = value if value is not None else utcnow()
    return to_iso(moment), _canonical_utc(moment)


def _json_loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


#: Where an as-of row came from.  ``history`` is a write appended after this schema existed,
#: ``seed`` the migration's copy of a row that already existed when schema v5 was applied, and
#: ``current`` a recording made before the history existed (its only row, read as before).
#: ``delete`` is a tombstone: the key is absent from ``at`` on, never present with an old payload.
_ASOF_ORIGINS = {"write": "history", "seed": "seed", "current": "current", "delete": "delete"}


@dataclass(frozen=True)
class AsOfValue:
    """One settings/profile value in force at an instant, and where that row came from."""

    at: datetime | None
    value: Any
    origin: str


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Whether a table is present: how the readers tell an upgraded store from an old recording."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _decode_as_of(text: Any, where: str) -> Any:
    """Decode one stored JSON payload; unlike :func:`_json_loads` this refuses corruption.

    The readers are the replay's only path to a recorded value, and a payload that cannot be read
    must refuse the window (``malformed_json``) rather than be read as a default the recording never
    had.  The message carries the key so an operator can name the row.
    """
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{where} is not readable JSON: {exc}") from exc


def _as_of_moment(text: Any, where: str) -> datetime | None:
    """Parse one stored instant, refusing a representation this build cannot place exactly.

    ``None`` only when there is no instant at all.  Anything else is either a proper instant or an
    error: a timestamp is compared as an instant, never as text, because ``2026-01-01T08:30:00-02:00``
    sorts before ``2026-01-01T10:00:00+00:00`` while representing a later moment.  A stored string is
    placed only when it matches the one accepted form (see ``_INSTANT``) as a whole; within that
    form the rule about precision is exactness, not digit count, so a fraction is kept however many
    digits it is written with (either separator) while every digit past microsecond precision is
    zero -- that names the instant a datetime holds, and dropping such a row would be data loss, a
    seed skipped or a row cut.  A digit that would be lost with real precision refuses instead of
    being rounded, because the round would move the instant; nothing is ever assumed.
    """
    if text is None:
        # The one legitimate absence: no instant was ever stored for this key.
        return None
    if not isinstance(text, str):
        # A number is NOT accepted. ``from_iso`` happily reads one as Unix epoch seconds, which
        # would fabricate an instant for a corrupted or mistyped row -- and a small number lands
        # decades in the past, i.e. "old enough" for every boundary, so the row would be trusted
        # instead of being refused. The accepted form is a string; anything else is unplaceable.
        raise ValueError(f"{where} is not a stamp in the accepted form: {text!r}")
    moment = _placed_moment(text, where)
    if moment is not None and from_iso(_canonical_utc(moment)) != moment:
        raise ValueError(f"{where} cannot be represented exactly as an instant: {text!r}")
    return moment


#: The one accepted form of a stored stamp, matched whole *before* anything is parsed::
#:
#:     date    := YYYY-MM-DD                        extended calendar form only
#:     time    := HH:MM[:SS[.fraction]]             fraction after "," is accepted too
#:     offset  := "Z" | [+-]HH:MM[:SS[.fraction]]
#:
#: Everything else -- week dates, ordinal dates, a basic-format date, another date/time separator,
#: a missing offset -- is unplaceable: refused for a value row, pruned for a scratch row.  The
#: alternative, inferring the structure of a stamp from its signs and parsing the pieces, reads a
#: form the writer never writes as if it were an offset: ``2026-W01-1T12:00:00Z`` splits inside its
#: week date, and ``2026-01-01+12:00:00`` (whose ``+`` the parser takes as a date/time separator,
#: i.e. midday) reads as midnight with a twelve-hour offset -- a *different instant*, which is the
#: failure this whole change exists to prevent.  The trade is stated plainly: every stamp a writer
#: produces matches (``to_iso`` writes an extended date, seconds and an offset), so only hand-written
#: or exotic rows are refused, and a refusal is safe where a guessed instant is not.
_INSTANT = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?P<fraction>[.,]\d+)?)?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)"
)

#: The offset group's own grammar, read by this build from the matched group's digits: sign, hours,
#: minutes, optional seconds and fraction.  Read here rather than trusted to ``fromisoformat``,
#: which can drop an offset fraction whole (``+00:00:00.5`` comes back with no offset at all) and
#: so place a row half a second late -- or early, for a negative offset.
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?")


def _microseconds(digits: str, where: str, text: str) -> int:
    """One fraction's digits as microseconds, by exactness rather than by digit count.

    Any number of digits is fine while every digit past microsecond precision is zero, because
    that names the instant a datetime holds and dropping such a row would be data loss (a seed
    skipped, a row cut); a nonzero digit there refuses rather than being rounded.
    """
    if any(digit != "0" for digit in digits[6:]):
        raise ValueError(
            f"{where} would lose sub-microsecond precision this build cannot place: {text!r}"
        )
    return int(digits[:6].ljust(6, "0"))


def _placed_moment(text: str, where: str) -> datetime:
    """Place one stored stamp exactly from the accepted form above, or refuse it.

    The form decides the structure; the digits then build the instant here, timezone included, so
    no piece of a stamp is ever inferred from how it sorts or which side of a sign it sits.
    """
    match = _INSTANT.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"{where} is not a stamp in the accepted form: {text!r}")
    fields = match.groupdict()
    microsecond = _microseconds((fields["fraction"] or "")[1:], where, text)
    offset_text = fields["offset"]
    offset = timedelta(0)
    if offset_text != "Z":
        written = _OFFSET.fullmatch(offset_text)
        if written is None:
            raise ValueError(f"{where} has an offset this build cannot place: {text!r}")
        sign, hours, minutes, seconds, fraction = written.groups()
        if int(minutes) > 59 or int(seconds or 0) > 59:
            raise ValueError(f"{where} has an offset this build cannot place: {text!r}")
        offset = (-1 if sign == "-" else 1) * timedelta(
            hours=int(hours),
            minutes=int(minutes),
            seconds=int(seconds or 0),
            microseconds=_microseconds(fraction or "", where, text),
        )
    try:
        zone = timezone.utc if offset_text == "Z" else timezone(offset)
        return datetime(
            int(fields["year"]),
            int(fields["month"]),
            int(fields["day"]),
            int(fields["hour"]),
            int(fields["minute"]),
            int(fields["second"] or 0),
            microsecond,
            tzinfo=zone,
        ).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        # OverflowError is reachable from astimezone() at the calendar edges: a stamp of year 1
        # with a positive offset (or year 9999 with a negative one) constructs and then overflows
        # when it is moved to UTC. Translated here, at the single place every caller goes through,
        # so a caller that guards only ValueError (a scratch prune, a seed) treats the row as
        # unplaceable -- removed or skipped -- instead of aborting the whole operation.
        raise ValueError(f"{where} is not a readable timestamp: {exc}") from exc


def parsed_as_of_instant(text: Any) -> datetime | None:
    """Exactly parse one stored instant, or answer ``None`` (the fail-closed tolerant form).

    Used where a row may legitimately be unplaceable -- a migration seed, a scratch cut: the caller
    then skips or removes the row instead of guessing a moment for it.
    """
    try:
        return _as_of_moment(text, "stored instant")
    except ValueError:
        return None


def _verified_moment(text: Any, now: datetime, where: str) -> datetime:
    """Re-read the timestamp of a row about to be trusted; refuse one unusable or in the future.

    Selection already compares parsed instants, so this is the independent backstop in front of the
    frozen window: the chosen row is verified by parsing its own timestamp, and a row later than
    the boundary (or with no readable timestamp) refuses rather than being silently dropped.
    """
    moment = _as_of_moment(text, where)
    if moment is None:
        raise ValueError(f"{where} has no timestamp")
    if moment > now:
        raise ValueError(f"{where} is later than the boundary {to_iso(now)}: {to_iso(moment)}")
    return moment


def _newest_settings_history_rows(
    conn: sqlite3.Connection, now: datetime
) -> dict[str, tuple[datetime, sqlite3.Row]]:
    """The newest history row per key at or before ``now``: by instant, ties by newest insertion.

    Chosen by ``(at, id)`` rather than by insertion id alone, because a write may legitimately be
    backdated (the writers take the write time), so insertion order is not chronological order.
    Every row's timestamp is parsed here, so an unreadable representation refuses the read.
    """
    best: dict[str, tuple[datetime, int, sqlite3.Row]] = {}
    for row in conn.execute("SELECT id, key, at, value_json, origin FROM settings_history"):
        key = str(row["key"])
        # The origin is checked before eligibility: a row we would not trust if selected must not
        # silently pass because a newer or older row happens to win (or because a tombstone is read).
        _as_of_origin(row["origin"], f"settings_history[{key}].origin")
        at = _as_of_moment(row["at"], f"settings_history[{key}].at")
        if at is None or at > now:
            continue
        row_id = int(row["id"])
        current = best.get(key)
        if current is None or (at, row_id) > (current[0], current[1]):
            best[key] = (at, row_id, row)
    winners: dict[str, tuple[datetime, sqlite3.Row]] = {}
    for key, (_at, _row_id, row) in best.items():
        winners[key] = (
            _verified_moment(row["at"], now, f"settings_history[{key}].at"), row,
        )
    return winners


def _as_of_origin(origin: Any, where: str) -> str:
    """Map a stored origin, refusing one this build does not know rather than trusting it."""
    mapped = _ASOF_ORIGINS.get(str(origin))
    if mapped is None:
        raise ValueError(f"{where} is not a known origin: {origin!r}")
    return mapped


def settings_as_of(conn: sqlite3.Connection, now: datetime) -> dict[str, AsOfValue]:
    """Newest ``settings`` row per key with ``at <= now``; a tombstoned key is absent.

    With the v5 history the answer comes from ``settings_history``: the newest row at or before
    ``now`` per key.  A key whose newest row there is a tombstone is *not* returned -- the value it
    held before the delete is never resurrected (:func:`settings_deleted_as_of` tells that apart
    from a key that has no value yet).  A key with no history rows at all -- a row written by hand,
    or a recording made before v5 in which the history table exists for other keys -- falls back to
    the current ``settings`` row, and only when *that* row is at or before ``now``; a key whose only
    row is newer is simply absent here, and the caller refuses on the current table's timestamp.
    Instants are compared as parsed datetimes, never as stored text, and the chosen row's own
    timestamp is re-checked before it is returned (``_verified_moment``).
    """
    snapshot: dict[str, AsOfValue] = {}
    if table_exists(conn, "settings_history"):
        for key, (at, row) in _newest_settings_history_rows(conn, now).items():
            where = f"settings_history[{key}]"
            origin = str(row["origin"])
            if origin == "delete":
                continue
            snapshot[key] = AsOfValue(
                at,
                _decode_as_of(row["value_json"], f"{where}.value_json"),
                _as_of_origin(origin, f"{where}.origin"),
            )
        tracked = {
            str(row[0]) for row in conn.execute("SELECT DISTINCT key FROM settings_history")
        }
        for row in conn.execute("SELECT key, updated_at, value_json FROM settings"):
            key = str(row["key"])
            if key in snapshot or key in tracked:
                continue
            at = _as_of_moment(row["updated_at"], f"settings[{key}].updated_at")
            if at is not None and at <= now:
                snapshot[key] = AsOfValue(
                    at, _decode_as_of(row["value_json"], f"settings[{key}].value_json"), "current"
                )
        return snapshot
    for row in conn.execute("SELECT key, updated_at, value_json FROM settings"):
        key = str(row["key"])
        at = _as_of_moment(row["updated_at"], f"settings[{key}].updated_at")
        if at is not None and at <= now:
            snapshot[key] = AsOfValue(
                at, _decode_as_of(row["value_json"], f"settings[{key}].value_json"), "current"
            )
    return snapshot


def settings_deleted_as_of(conn: sqlite3.Connection, now: datetime) -> set[str]:
    """Keys whose newest history row at or before ``now`` is a tombstone.

    A tombstone is a change like any write: a window starting before it must refuse the key, and
    one starting after it reads the key as absent.  Only the history can answer this, so a legacy
    recording without the table answers with the empty set.
    """
    if not table_exists(conn, "settings_history"):
        return set()
    return {
        key
        for key, (_at, row) in _newest_settings_history_rows(conn, now).items()
        if str(row["origin"]) == "delete"
    }


def profile_as_of(conn: sqlite3.Connection, kind: str, key: str, now: datetime) -> AsOfValue | None:
    """Newest ``learning_profiles`` row for one key with ``at <= now``, or ``None``.

    A tombstone at or before ``now`` returns ``None``: the key was deleted then, and the payload
    before the delete is never returned (:func:`profile_deleted_as_of` distinguishes that from a
    key that was never written).  A key with no history rows at all falls back to its current row,
    and only when that row is at or before ``now``.  Instants are compared as parsed datetimes,
    never as stored text, and the chosen row's own timestamp is re-checked before it is returned.
    """
    if table_exists(conn, "learning_profiles_history"):
        found = _newest_profile_history_row(conn, kind, key, now)
        if found is not None:
            at, row = found
            where = f"learning_profiles_history[{kind}/{key}]"
            origin = str(row["origin"])
            if origin == "delete":
                return None
            return AsOfValue(
                at,
                _decode_as_of(row["payload_json"], f"{where}.payload_json"),
                _as_of_origin(origin, f"{where}.origin"),
            )
        if _profile_history_known(conn, kind, key):
            return None
        return _current_profile_as_of(conn, kind, key, now)
    return _current_profile_as_of(conn, kind, key, now)


def _newest_profile_history_row(
    conn: sqlite3.Connection, kind: str, key: str, now: datetime
) -> tuple[datetime, sqlite3.Row] | None:
    """One key's newest history row at or before ``now``: by instant, ties by newest insertion."""
    best: tuple[datetime, int, sqlite3.Row] | None = None
    for row in conn.execute(
        "SELECT id, at, payload_json, origin FROM learning_profiles_history"
        " WHERE kind = ? AND key = ?",
        (kind, key),
    ):
        # Checked before eligibility, so both profile readers enforce exactly the same rule.
        _as_of_origin(row["origin"], f"learning_profiles_history[{kind}/{key}].origin")
        at = _as_of_moment(row["at"], f"learning_profiles_history[{kind}/{key}].at")
        if at is None or at > now:
            continue
        row_id = int(row["id"])
        if best is None or (at, row_id) > (best[0], best[1]):
            best = (at, row_id, row)
    if best is None:
        return None
    where = f"learning_profiles_history[{kind}/{key}].at"
    return _verified_moment(best[2]["at"], now, where), best[2]


def _profile_history_known(conn: sqlite3.Connection, kind: str, key: str) -> bool:
    """Whether the key has any history row at all, even one after the boundary."""
    return conn.execute(
        "SELECT 1 FROM learning_profiles_history WHERE kind = ? AND key = ? LIMIT 1",
        (kind, key),
    ).fetchone() is not None


def profile_deleted_as_of(conn: sqlite3.Connection, kind: str, key: str, now: datetime) -> bool:
    """Whether one profile key was deleted at or before ``now`` (its newest row is a tombstone)."""
    if not table_exists(conn, "learning_profiles_history"):
        return False
    found = _newest_profile_history_row(conn, kind, key, now)
    return found is not None and str(found[1]["origin"]) == "delete"


def _current_profile_as_of(
    conn: sqlite3.Connection, kind: str, key: str, now: datetime
) -> AsOfValue | None:
    """The current row as an as-of value, when it is itself at or before ``now``."""
    row = conn.execute(
        "SELECT updated_at, payload_json FROM learning_profiles WHERE kind = ? AND key = ?",
        (kind, key),
    ).fetchone()
    if row is None:
        return None
    at = _as_of_moment(row["updated_at"], f"learning_profiles[{kind}/{key}].updated_at")
    if at is None or at > now:
        return None
    return AsOfValue(
        at, _decode_as_of(row["payload_json"], f"learning_profiles[{kind}/{key}].payload_json"),
        "current",
    )


class Store:
    """Thread-safe SQLite store; one connection per thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ connections
    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened and migrated on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            _apply_migrations(conn)
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this Store opened (all threads).

        Call only during shutdown after the worker threads have stopped; other threads would need a
        fresh :meth:`connection` afterwards.
        """
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else _json_loads(row["value_json"], default)

    def get_settings(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT key, value_json FROM settings").fetchall()
        return {row["key"]: _json_loads(row["value_json"]) for row in rows}

    def set_setting(self, key: str, value: Any, now: datetime | None = None) -> None:
        self.set_settings({key: value}, now=now)

    def set_settings(self, values: Mapping[str, Any], now: datetime | None = None) -> None:
        ts, history_ts = _write_stamps(now)
        payloads = [(key, json.dumps(value)) for key, value in values.items()]
        with _transaction(self.connection) as conn:
            for key, payload in payloads:
                previous = conn.execute(
                    "SELECT value_json FROM settings WHERE key = ?", (key,)
                ).fetchone()
                # The current row keeps its exact upsert semantics; the history only records a
                # write whose serialised value actually changed, which bounds the growth of keys
                # the add-on re-stamps on every tick (SPEC.md 11).
                conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                    "updated_at = excluded.updated_at",
                    (key, payload, ts),
                )
                if previous is None or previous["value_json"] != payload:
                    conn.execute(
                        "INSERT INTO settings_history(key, at, value_json, origin)"
                        " VALUES(?, ?, ?, 'write')",
                        (key, history_ts, payload),
                    )

    def delete_setting(self, key: str, now: datetime | None = None) -> None:
        ts, history_ts = _write_stamps(now)
        with _transaction(self.connection) as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            if cursor.rowcount:
                # A tombstone, not a gap: a window starting before it still refuses the key, and a
                # window starting after it reads the key as absent, never as its old value.
                conn.execute(
                    "INSERT INTO settings_history(key, at, value_json, origin)"
                    " VALUES(?, ?, NULL, 'delete')",
                    (key, history_ts),
                )

    def settings_as_of(self, now: datetime | None = None) -> dict[str, AsOfValue]:
        """The settings in force at ``now`` (see :func:`settings_as_of`)."""
        return settings_as_of(self.connection, now or utcnow())

    def settings_deleted_as_of(self, now: datetime | None = None) -> set[str]:
        """Settings keys deleted at or before ``now`` (see :func:`settings_deleted_as_of`)."""
        return settings_deleted_as_of(self.connection, now or utcnow())

    def profile_as_of(self, kind: str, key: str, now: datetime | None = None) -> AsOfValue | None:
        """One profile in force at ``now`` (see :func:`profile_as_of`)."""
        return profile_as_of(self.connection, kind, key, now or utcnow())

    def profile_deleted_as_of(
        self, kind: str, key: str, now: datetime | None = None
    ) -> bool:
        """Whether one profile key was deleted at or before ``now`` (see the module function)."""
        return profile_deleted_as_of(self.connection, kind, key, now or utcnow())

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with _transaction(self.connection) as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # ------------------------------------------------------------------ plans
    def save_plan(self, plan: Plan) -> int:
        """Insert a plan and its slots, returning the new ``plans.id``."""
        summary = dict(plan.summary.__dict__)
        summary["infeasible_reason"] = plan.infeasible_reason
        summary["slot_minutes"] = plan.slot_minutes
        summary["sources"] = plan.sources
        with _transaction(self.connection) as conn:
            cursor = conn.execute(
                "INSERT INTO plans(created_at, horizon_start, horizon_end, mode, dry_run,"
                " feasible, summary_json, batteries_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    to_iso(plan.generated_at),
                    to_iso(plan.horizon_start),
                    to_iso(plan.horizon_end),
                    plan.mode,
                    int(bool(plan.dry_run)),
                    int(bool(plan.feasible)),
                    json.dumps(summary),
                    json.dumps(plan.batteries),
                ),
            )
            plan_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO plan_slots(plan_id, t, buy, sell, pv_kw, load_kw, soc_kwh, power_w,"
                " action, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        plan_id,
                        to_iso(slot.t),
                        slot.buy,
                        slot.sell,
                        slot.pv_kw,
                        slot.load_kw,
                        slot.soc_kwh,
                        slot.power_w,
                        slot.action.value if isinstance(slot.action, Action) else str(slot.action),
                        slot.reason,
                    )
                    for slot in plan.slots
                ],
            )
        return plan_id

    def load_plan(self, plan_id: int) -> Plan | None:
        row = self.connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def latest_plan(self) -> Plan | None:
        row = self.connection.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else self._plan_from_row(row)

    def list_plans(self, limit: int = 20) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM plans ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _plan_from_row(self, row: sqlite3.Row) -> Plan:
        summary_raw = _json_loads(row["summary_json"], {}) or {}
        infeasible_reason = summary_raw.pop("infeasible_reason", None)
        slot_minutes = int(summary_raw.pop("slot_minutes", 15) or 15)
        sources = summary_raw.pop("sources", {}) or {}
        known = {name for name in PlanSummary().__dict__}
        summary = PlanSummary(**{k: v for k, v in summary_raw.items() if k in known})
        slots = [
            PlanSlot(
                t=from_iso(s["t"]),
                buy=float(s["buy"] or 0.0),
                sell=float(s["sell"] or 0.0),
                pv_kw=float(s["pv_kw"] or 0.0),
                load_kw=float(s["load_kw"] or 0.0),
                soc_kwh=float(s["soc_kwh"] or 0.0),
                power_w=float(s["power_w"] or 0.0),
                action=Action(s["action"]) if s["action"] else Action.HOLD,
                reason=s["reason"] or "",
            )
            for s in self.connection.execute(
                "SELECT * FROM plan_slots WHERE plan_id = ? ORDER BY t", (row["id"],)
            ).fetchall()
        ]
        if slots and len(slots) > 1:
            delta = slots[1].t - slots[0].t
            if delta.total_seconds() > 0:
                slot_minutes = int(delta.total_seconds() // 60)
        return Plan(
            generated_at=from_iso(row["created_at"]),
            horizon_start=from_iso(row["horizon_start"]),
            horizon_end=from_iso(row["horizon_end"]),
            slot_minutes=slot_minutes,
            feasible=bool(row["feasible"]),
            infeasible_reason=infeasible_reason,
            slots=slots,
            summary=summary,
            batteries=_json_loads(row["batteries_json"], []) or [],
            mode=row["mode"] or "advisory",
            dry_run=bool(row["dry_run"]),
            sources={str(k): str(v) for k, v in sources.items()},
            # Additive (0.17.0): the maintenance block rides in summary_json; keep the top-level
            # field consistent for a plan restored from the store.
            maintenance=getattr(summary, "maintenance", None),
        )

    # ------------------------------------------------------------------ dispatch log
    def add_dispatch(
        self,
        *,
        ts: datetime | str | None = None,
        battery_id: str | None = None,
        requested_w: float | None = None,
        applied_w: float | None = None,
        soc_pct: float | None = None,
        mode: str = "",
        dry_run: bool = True,
        result: str = "",
        code: str = "",
        message: str = "",
    ) -> int:
        with _transaction(self.connection) as conn:
            cursor = conn.execute(
                "INSERT INTO dispatch_log(ts, battery_id, requested_w, applied_w, soc_pct, mode,"
                " dry_run, result, code, message) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _to_ts(ts, utcnow()),
                    battery_id,
                    requested_w,
                    applied_w,
                    soc_pct,
                    mode,
                    int(bool(dry_run)),
                    result,
                    code,
                    message,
                ),
            )
            return int(cursor.lastrowid or 0)

    def add_dispatch_result(
        self,
        result: DispatchResult,
        *,
        mode: str = "",
        soc_pct: float | None = None,
        outcome: str | None = None,
    ) -> int:
        return self.add_dispatch(
            ts=result.ts,
            battery_id=result.battery_id,
            requested_w=result.requested_w,
            applied_w=result.applied_w,
            soc_pct=soc_pct,
            mode=mode,
            dry_run=result.dry_run,
            result=outcome if outcome is not None else ("ok" if result.ok else "error"),
            code=result.code,
            message=result.message,
        )

    def dispatch_log(
        self, limit: int = 100, battery_id: str | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM dispatch_log"
        clauses: list[str] = []
        params: list[Any] = []
        if battery_id:
            clauses.append("battery_id = ?")
            params.append(battery_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(to_iso(since))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ measurements / actuals
    def add_measurements(self, measurements: Iterable[Measurement]) -> int:
        rows = [
            (
                to_iso(item.ts),
                item.role,
                float(item.value),
                item.unit,
            )
            for item in measurements
        ]
        if not rows:
            return 0
        with _transaction(self.connection) as conn:
            conn.executemany(
                "INSERT INTO measurements(ts, entity_role, value, unit) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(ts, entity_role) DO UPDATE SET value = excluded.value,"
                " unit = excluded.unit",
                rows,
            )
        return len(rows)

    def measurements_range(
        self,
        start: datetime,
        end: datetime,
        roles: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM measurements WHERE ts >= ? AND ts < ?"
        params: list[Any] = [to_iso(start), to_iso(end)]
        role_list = list(roles) if roles else []
        if role_list:
            query += f" AND entity_role IN ({','.join('?' * len(role_list))})"
            params.extend(role_list)
        query += " ORDER BY ts"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def upsert_actuals(self, ts: datetime | str, **fields: float | None) -> None:
        """Insert or replace one 5-minute actuals row; unknown fields are rejected."""
        unknown = set(fields) - set(_ACTUAL_COLUMNS)
        if unknown:
            raise ValueError(f"unknown actuals columns: {sorted(unknown)}")
        columns = list(fields)
        if not columns:
            with _transaction(self.connection) as conn:
                conn.execute(
                    "INSERT INTO actuals(ts) VALUES(?) ON CONFLICT(ts) DO NOTHING", (_to_ts(ts),)
                )
            return
        stamps = ["?"] * len(columns)
        updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
        sql = (
            f"INSERT INTO actuals(ts, {', '.join(columns)}) VALUES(?, {', '.join(stamps)}) "
            f"ON CONFLICT(ts) DO UPDATE SET {updates}"
        )
        with _transaction(self.connection) as conn:
            conn.execute(sql, [_to_ts(ts), *[fields[name] for name in columns]])

    def actuals_range(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM actuals WHERE ts >= ? AND ts < ? ORDER BY ts",
            (to_iso(start), to_iso(end)),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ forecast archive (SPEC 7.4)
    def archive_forecast(self, rows: Iterable[tuple[str, str, str, str, float, float | None]]) -> int:
        """Record ``(kind, source, target_ts, issued_at, value, uncertainty_pct)`` forecasts.

        Upsert rather than append, keeping the newest issue per ``(kind, source, slot)``. A provider
        refreshes every thirty minutes over a ~48 hour horizon, so appending would store thousands of
        rows a day for the one number per slot that actually matters: the most recent forecast made
        *before* the slot, which is the one the planner acted on. The ``WHERE`` clause on the conflict
        keeps an already-elapsed slot from being rewritten by a later issue that is only now
        catching up to it.

        Returns the number of rows offered; a row whose slot already has a newer issue is left alone.
        """
        payload = [
            (str(kind), str(source), _to_ts(target), _to_ts(issued), float(value), uncertainty)
            for kind, source, target, issued, value, uncertainty in rows
            if target is not None and issued is not None
        ]
        if not payload:
            return 0
        with _transaction(self.connection) as conn:
            conn.executemany(
                "INSERT INTO forecast_archive"
                "(kind, source, target_ts, issued_at, value, uncertainty_pct) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(kind, source, target_ts) DO UPDATE SET"
                " issued_at = excluded.issued_at, value = excluded.value,"
                " uncertainty_pct = excluded.uncertainty_pct"
                " WHERE excluded.issued_at >= forecast_archive.issued_at",
                payload,
            )
        return len(payload)

    def forecast_archive_range(
        self, start: datetime, end: datetime, kind: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM forecast_archive WHERE target_ts >= ? AND target_ts < ?"
        params: list[Any] = [to_iso(start), to_iso(end)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        rows = self.connection.execute(sql + " ORDER BY target_ts", params).fetchall()
        return [dict(row) for row in rows]

    def forecast_accuracy(
        self, start: datetime, end: datetime, kind: str
    ) -> list[dict[str, Any]]:
        """Per-source error of archived forecasts against the measured series, by local hour.

        Compared on hourly means of both sides rather than slot by slot, because the archive holds
        15-minute slots while ``actuals`` holds whatever the sampler wrote; joining those on an exact
        timestamp would silently match almost nothing and report a flattering empty result. An
        unmeasured series (a NULL column, which is the normal case for PV on an install with no
        inverter sensor) yields no rows, and the caller is expected to say so rather than report a
        perfect score.
        """
        column = _ARCHIVE_ACTUAL_COLUMN.get(str(kind))
        if column is None:
            raise ValueError(f"no measured series to score {kind!r} forecasts against")
        rows = self.connection.execute(
            "WITH f AS ("
            "  SELECT source AS source, substr(target_ts, 1, 13) AS hour,"
            "         AVG(value) AS forecast_kw, COUNT(*) AS slots"
            "  FROM forecast_archive WHERE kind = ? AND target_ts >= ? AND target_ts < ?"
            "  GROUP BY source, hour"
            "), m AS ("
            f"  SELECT substr(ts, 1, 13) AS hour, AVG({column}) AS measured_kw"
            f"  FROM actuals WHERE ts >= ? AND ts < ? AND {column} IS NOT NULL"
            "  GROUP BY hour"
            ")"
            " SELECT f.source AS source, COUNT(*) AS hours,"
            "        AVG(ABS(f.forecast_kw - m.measured_kw)) AS mae_kw,"
            "        AVG(f.forecast_kw - m.measured_kw) AS bias_kw,"
            "        AVG(m.measured_kw) AS measured_mean_kw,"
            "        AVG(f.forecast_kw) AS forecast_mean_kw"
            " FROM f JOIN m ON m.hour = f.hour"
            " GROUP BY f.source ORDER BY f.source",
            (str(kind), to_iso(start), to_iso(end), to_iso(start), to_iso(end)),
        ).fetchall()
        return [dict(row) for row in rows]

    def trim_forecast_archive(self, before: datetime) -> int:
        """Drop archived forecasts whose slot is older than ``before``; returns rows deleted."""
        with _transaction(self.connection) as conn:
            cursor = conn.execute(
                "DELETE FROM forecast_archive WHERE target_ts < ?", (to_iso(before),)
            )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ forecast cache / profiles
    def cache_get(self, kind: str, key: str) -> tuple[datetime, Any] | None:
        row = self.connection.execute(
            "SELECT fetched_at, payload_json FROM forecast_cache WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if row is None:
            return None
        fetched_at = from_iso(row["fetched_at"])
        return (fetched_at, _json_loads(row["payload_json"]))

    def cache_put(
        self, kind: str, key: str, payload: Any, fetched_at: datetime | None = None
    ) -> None:
        with _transaction(self.connection) as conn:
            conn.execute(
                "INSERT INTO forecast_cache(kind, key, fetched_at, payload_json)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET"
                " fetched_at = excluded.fetched_at, payload_json = excluded.payload_json",
                (kind, key, _to_ts(fetched_at, utcnow()), json.dumps(payload)),
            )

    def cache_age_seconds(self, kind: str, key: str, now: datetime | None = None) -> float | None:
        cached = self.cache_get(kind, key)
        if cached is None or cached[0] is None:
            return None
        return max(0.0, ((now or utcnow()) - cached[0]).total_seconds())

    def profile_get(self, kind: str, key: str) -> tuple[datetime, Any] | None:
        row = self.connection.execute(
            "SELECT updated_at, payload_json FROM learning_profiles WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if row is None:
            return None
        return (from_iso(row["updated_at"]), _json_loads(row["payload_json"]))

    def profile_put(
        self, kind: str, key: str, payload: Any, updated_at: datetime | None = None
    ) -> None:
        ts, history_ts = _write_stamps(updated_at)
        encoded = json.dumps(payload)
        with _transaction(self.connection) as conn:
            previous = conn.execute(
                "SELECT payload_json FROM learning_profiles WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
            conn.execute(
                "INSERT INTO learning_profiles(kind, key, updated_at, payload_json)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET"
                " updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (kind, key, ts, encoded),
            )
            if previous is None or previous["payload_json"] != encoded:
                conn.execute(
                    "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
                    " VALUES(?, ?, ?, ?, 'write')",
                    (kind, key, history_ts, encoded),
                )

    def profile_delete(self, kind: str, key: str, now: datetime | None = None) -> None:
        ts, history_ts = _write_stamps(now)
        with _transaction(self.connection) as conn:
            cursor = conn.execute(
                "DELETE FROM learning_profiles WHERE kind = ? AND key = ?", (kind, key)
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO learning_profiles_history(kind, key, at, payload_json, origin)"
                    " VALUES(?, ?, ?, NULL, 'delete')",
                    (kind, key, history_ts),
                )

    # ------------------------------------------------------------------ savings
    def upsert_savings_daily(
        self, day: str | date | datetime, *, savings_eur: float | None = None, **fields: float
    ) -> None:
        """Upsert one ``savings_daily`` row.

        ``savings_eur`` defaults to ``baseline_cost_eur - (cost_eur - revenue_eur)``, the SPEC.md
        section 11 definition (battery doing nothing vs. what actually happened).
        """
        day_key = day.isoformat() if isinstance(day, (date, datetime)) else str(day)
        values: dict[str, float | None] = {name: None for name in _SAVINGS_COLUMNS}
        unknown = set(fields) - set(_SAVINGS_COLUMNS)
        if unknown:
            raise ValueError(f"unknown savings_daily columns: {sorted(unknown)}")
        values.update(fields)
        if savings_eur is None:
            savings_eur = (values["baseline_cost_eur"] or 0.0) - (
                (values["cost_eur"] or 0.0) - (values["revenue_eur"] or 0.0)
            )
        values["savings_eur"] = savings_eur
        columns = list(values)
        placeholders = ", ".join("?" * len(columns))
        updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
        with _transaction(self.connection) as conn:
            conn.execute(
                f"INSERT INTO savings_daily(day, {', '.join(columns)}) VALUES(?, {placeholders}) "
                f"ON CONFLICT(day) DO UPDATE SET {updates}",
                [day_key, *[values[name] for name in columns]],
            )

    def savings_daily_range(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM savings_daily WHERE day >= ? AND day <= ? ORDER BY day",
            (start_day, end_day),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ events
    def add_event(
        self,
        *,
        ts: datetime | str | None = None,
        level: str = "info",
        code: str = "",
        message: str = "",
        payload: Any = None,
    ) -> int:
        with _transaction(self.connection) as conn:
            cursor = conn.execute(
                "INSERT INTO events(ts, level, code, message, payload_json) VALUES(?, ?, ?, ?, ?)",
                (
                    _to_ts(ts, utcnow()),
                    level,
                    code,
                    message,
                    None if payload is None else json.dumps(payload),
                ),
            )
            return int(cursor.lastrowid or 0)

    def events(
        self, limit: int = 100, level: str | None = None, code: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.connection.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json", None))
            out.append(item)
        return out

    # ------------------------------------------------------------------ maintenance
    def trim(
        self,
        *,
        now: datetime | None = None,
        measurements_days: int = DEFAULT_RETENTION["measurements_days"],
        actuals_days: int = DEFAULT_RETENTION["actuals_days"],
        dispatch_rows: int = DEFAULT_RETENTION["dispatch_rows"],
        events_rows: int = DEFAULT_RETENTION["events_rows"],
        plans_rows: int = DEFAULT_RETENTION["plans_rows"],
        forecast_archive_days: int = DEFAULT_RETENTION["forecast_archive_days"],
    ) -> dict[str, int]:
        """Apply SPEC.md section 11 retention. Returns per-table deleted row counts.

        Plans are trimmed by id (the N most recent survive) together with their ``plan_slots``, since
        every replan inserts ~96 slot rows per day and nothing else ever deletes them.
        Callers must persist daily savings baselines before calling this (SPEC.md section 11).
        """
        moment = now or utcnow()
        pairs = (
            ("measurements", "ts", moment - timedelta(days=measurements_days)),
            ("actuals", "ts", moment - timedelta(days=actuals_days)),
            ("forecast_archive", "target_ts", moment - timedelta(days=forecast_archive_days)),
        )
        removed: dict[str, int] = {}
        with _transaction(self.connection) as conn:
            for table, column, cutoff in pairs:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {column} < ?",
                    (to_iso(cutoff),),
                )
                removed[table] = max(0, cursor.rowcount)
            for table, keep in (("dispatch_log", dispatch_rows), ("events", events_rows), ("plans", plans_rows)):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE id <= (SELECT MAX(id) - ? FROM {table})", (keep,)
                )
                removed[table] = max(0, cursor.rowcount)
            cursor = conn.execute(
                "DELETE FROM plan_slots WHERE plan_id NOT IN (SELECT id FROM plans)"
            )
            removed["plan_slots"] = max(0, cursor.rowcount)
        return removed


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = int(row["version"]) if row is not None else 0
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES(0)")
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        with _transaction(conn) as tx:
            # Re-check under the write lock: another thread may have migrated first.
            fresh = tx.execute("SELECT version FROM schema_version").fetchone()
            if fresh is not None and int(fresh["version"]) >= version:
                continue
            for statement in statements:
                if callable(statement):
                    statement(tx)
                else:
                    tx.execute(statement)
            tx.execute("UPDATE schema_version SET version = ?", (version,))
