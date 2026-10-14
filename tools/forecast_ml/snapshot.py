"""Preserve this installation's own observations, independently of Home Assistant.

Home Assistant is not a training archive. Its short-term statistics expire on the recorder's own
schedule (about ten days here), its purge settings are outside this tool's control, and the add-on's
main store deliberately trims. Anything not copied out on a schedule is simply gone, and no amount of
later effort reconstructs it.

So this writes a separate SQLite database that answers to nothing else:

* **Raw observables only, never features.** Feature engineering will change; the raw record must
  outlive it.
* **Append-only, keyed on ``(ts, role, source)``.** Live capture and historical backfill can both run
  without clobbering each other, and re-running is idempotent rather than duplicating.
* **Harvest faster than Home Assistant forgets.** Hourly backfill reaches far back from long-term
  statistics; a 5-minute sweep is the only way to keep the fine detail before it ages out.

The role vocabulary is fixed (`ROLES`) so the training table has a stable shape. Entity ids are
private and live in a mapping file **outside the repository**; see `roles.example.json` for the shape.
Transport is the `hassio` CLI, which already holds the credential, so this never touches a token.

Usage:
    python snapshot.py --roles roles.json --db data/forecast_ml.db
    python snapshot.py --roles roles.json --db data/forecast_ml.db --status
    python snapshot.py --roles roles.json --db data/forecast_ml.db --recent-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

#: The fixed role vocabulary. A role is a *meaning*, not an entity: the entity behind it can be
#: remapped when hardware changes without the training table changing shape.
ROLES: dict[str, str] = {
    "grid_net": "power",  # signed: import positive, export negative
    "grid_import": "power",
    "grid_export": "power",
    "load": "power",
    "pv": "power",
    "battery_power": "power",  # signed: charging positive where the device reports it that way
    "battery_charge": "power",
    "battery_discharge": "power",
    "battery_soc": "percent",
    "battery_available_kwh": "energy_level",
    "temp_outdoor": "temperature",
    "temp_indoor": "temperature",
}

#: Statistical types to request per kind. A **counter** has a change; an instantaneous value has a
#: mean. Conflating the two is how an energy counter's cumulative total gets mistaken for a reading,
#: so the distinction is explicit rather than inferred from a unit string.
STATISTIC_TYPES = {
    "power": ["mean"],
    "temperature": ["mean"],
    "percent": ["mean"],
    "energy_level": ["mean"],
    "energy": ["change"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS observation (
    ts         INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    value      REAL    NOT NULL,
    kind       TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (ts, role, source)
);
CREATE INDEX IF NOT EXISTS observation_role_ts ON observation (role, ts);
CREATE TABLE IF NOT EXISTS harvest_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts  INTEGER NOT NULL,
    finished_ts INTEGER,
    roles_ok    TEXT,
    roles_failed TEXT,
    notes       TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def load_roles(path: Path) -> dict[str, dict]:
    """Read the role mapping. Unknown roles are refused rather than silently stored."""
    payload = json.loads(Path(path).read_text())
    mapping = payload.get("roles", payload)
    resolved: dict[str, dict] = {}
    for role, spec in mapping.items():
        if role not in ROLES:
            raise SystemExit(f"unknown role {role!r}; known roles: {', '.join(sorted(ROLES))}")
        if isinstance(spec, str):
            spec = {"entity": spec}
        entity = str(spec.get("entity") or "").strip()
        if not entity:
            continue
        resolved[role] = {"entity": entity, "kind": str(spec.get("kind") or ROLES[role])}
    if not resolved:
        raise SystemExit(f"no usable roles in {path}")
    return resolved


def call_statistics(hassio: str, entity: str, kind: str, start: dt.datetime, end: dt.datetime, period: str) -> list[tuple[int, float]]:
    """One ``recorder/statistics_during_period`` call, returning ``(epoch_seconds, value)`` pairs.

    Goes through the raw WebSocket passthrough because the CLI's convenience command posts to a REST
    endpoint that ``--read-only`` blocks; this is a read and is not blocked there.
    """
    message = {
        "statistic_ids": [entity],
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "period": period,
        "types": STATISTIC_TYPES.get(kind, ["mean"]),
    }
    completed = subprocess.run(
        [hassio, "ws", "call", "-T", "recorder/statistics_during_period",
         "-d", json.dumps(message), "--format", "json-compact"],
        capture_output=True, text=True, timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"hassio exited {completed.returncode}: {completed.stderr.strip()[:200]}")
    payload = json.loads(completed.stdout)
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected statistics payload: {str(payload)[:160]}")
    rows = result.get(entity) or []
    primary = STATISTIC_TYPES.get(kind, ["mean"])[0]
    samples: list[tuple[int, float]] = []
    for row in rows:
        value = row.get(primary)
        if value is None:
            continue
        start_ms = row.get("start")
        if start_ms is None:
            continue
        samples.append((int(int(start_ms) // 1000), float(value)))
    return samples


def store(connection: sqlite3.Connection, samples: list[tuple[int, float]], role: str, kind: str, source: str) -> int:
    """Insert observations, ignoring anything already held for the same ``(ts, role, source)``."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    before = connection.total_changes
    connection.executemany(
        "INSERT OR IGNORE INTO observation (ts, role, value, kind, source, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(ts, role, value, kind, source, now) for ts, value in samples],
    )
    connection.commit()
    return connection.total_changes - before


def harvest(connection: sqlite3.Connection, hassio: str, roles: dict[str, dict],
            backfill_days: int, recent_days: int, recent_only: bool) -> dict:
    """Pull hourly history and a recent 5-minute sweep for every configured role."""
    now = dt.datetime.now(dt.timezone.utc)
    started = int(now.timestamp())
    cursor = connection.execute(
        "INSERT INTO harvest_run (started_ts) VALUES (?)", (started,)
    )
    run_id = cursor.lastrowid
    connection.commit()

    ok: dict[str, int] = {}
    failed: dict[str, str] = {}
    for role, spec in sorted(roles.items()):
        kind = spec["kind"]
        inserted = 0
        try:
            if not recent_only:
                rows = call_statistics(
                    hassio, spec["entity"], kind,
                    now - dt.timedelta(days=backfill_days), now, "hour",
                )
                inserted += store(connection, rows, role, kind, "lts_hourly")
            rows = call_statistics(
                hassio, spec["entity"], kind,
                now - dt.timedelta(days=recent_days), now, "5minute",
            )
            inserted += store(connection, rows, role, kind, "stats_5min")
            ok[role] = inserted
            print(f"  {role:22s} +{inserted:6d} rows  ({spec['entity']})", flush=True)
        except Exception as error:  # noqa: BLE001 - one bad role must not lose the others
            failed[role] = f"{type(error).__name__}: {error}"
            print(f"  {role:22s} FAILED  {failed[role][:120]}", flush=True)

    connection.execute(
        "UPDATE harvest_run SET finished_ts = ?, roles_ok = ?, roles_failed = ?, notes = ? WHERE id = ?",
        (
            int(dt.datetime.now(dt.timezone.utc).timestamp()),
            json.dumps(ok),
            json.dumps(failed),
            f"backfill_days={backfill_days} recent_days={recent_days} recent_only={recent_only}",
            run_id,
        ),
    )
    connection.commit()
    return {"new_rows": sum(ok.values()), "roles_ok": ok, "roles_failed": failed}


def status(connection: sqlite3.Connection) -> None:
    """Per-role coverage, so 'is this actually working' has an answer that is not a guess."""
    print(f"{'role':22s}{'source':12s}{'rows':>8s}  {'first':20s}{'last':20s}")
    rows = connection.execute(
        "SELECT role, source, COUNT(*), MIN(ts), MAX(ts) FROM observation"
        " GROUP BY role, source ORDER BY role, source"
    ).fetchall()
    if not rows:
        print("  (nothing stored yet)")
        return
    for role, source, count, first, last in rows:
        first_text = dt.datetime.fromtimestamp(first, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        last_text = dt.datetime.fromtimestamp(last, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{role:22s}{source:12s}{count:8d}  {first_text:20s}{last_text:20s}")
    span = connection.execute(
        "SELECT (MAX(ts) - MIN(ts)) / 86400.0 FROM observation WHERE source = 'lts_hourly'"
    ).fetchone()[0]
    if span:
        print(f"\nhourly history spans {span:.0f} days")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roles", required=True, help="JSON role mapping, kept outside the repository")
    parser.add_argument("--db", default="data/forecast_ml.db")
    parser.add_argument("--hassio", default=None, help="path to the hassio CLI (default: found on PATH)")
    parser.add_argument("--backfill-days", type=int, default=400)
    parser.add_argument("--recent-days", type=int, default=9, help="5-minute retention is about 10 days")
    parser.add_argument("--recent-only", action="store_true", help="skip the long backfill")
    parser.add_argument("--status", action="store_true", help="print coverage and exit")
    args = parser.parse_args()

    connection = connect(Path(args.db))
    if args.status:
        status(connection)
        return 0

    hassio = args.hassio or shutil.which("hassio")
    if not hassio:
        print("hassio CLI not found; pass --hassio", file=sys.stderr)
        return 2

    roles = load_roles(Path(args.roles))
    print(f"harvesting {len(roles)} roles into {args.db}")
    result = harvest(connection, hassio, roles, args.backfill_days, args.recent_days, args.recent_only)
    print(f"\nnew rows: {result['new_rows']}  ok: {len(result['roles_ok'])}  failed: {len(result['roles_failed'])}")
    if result["roles_failed"]:
        print("failures:")
        for role, message in result["roles_failed"].items():
            print(f"  {role}: {message}")
    print()
    status(connection)
    return 0 if not result["roles_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
