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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

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

__all__ = ["DEFAULT_RETENTION", "SCHEMA_VERSION", "Store"]

SCHEMA_VERSION = 3

# Forward-only. Append new (version, statements) entries; never edit a released entry.
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
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


def _json_loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


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
        ts = _to_ts(now, utcnow())
        with _transaction(self.connection) as conn:
            conn.executemany(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                [(key, json.dumps(value), ts) for key, value in values.items()],
            )

    def delete_setting(self, key: str) -> None:
        with _transaction(self.connection) as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

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
        with _transaction(self.connection) as conn:
            conn.execute(
                "INSERT INTO learning_profiles(kind, key, updated_at, payload_json)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET"
                " updated_at = excluded.updated_at, payload_json = excluded.payload_json",
                (kind, key, _to_ts(updated_at, utcnow()), json.dumps(payload)),
            )

    def profile_delete(self, kind: str, key: str) -> None:
        with _transaction(self.connection) as conn:
            conn.execute(
                "DELETE FROM learning_profiles WHERE kind = ? AND key = ?", (kind, key)
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
                tx.execute(statement)
            tx.execute("UPDATE schema_version SET version = ?", (version,))
