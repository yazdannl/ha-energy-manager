"""Self-test for snapshot.py: run it directly, no pytest, no network.

    python selftest_snapshot.py

Deliberately *not* named ``test_*.py``. The add-on's pytest suite has a tracked gate count, and this
is tooling, so it must not silently change that number. Run it by hand.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "nested" / "forecast_ml.db"

        print("schema")
        connection = snapshot.connect(db)
        check(db.exists(), "creates parent directories and the database file")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        check({"observation", "harvest_run"} <= tables, "creates both tables")

        print("append-only inserts")
        first = snapshot.store(connection, [(1000, 1.5), (1060, 2.5)], "grid_net", "power", "lts_hourly")
        check(first == 2, "inserts new rows")
        again = snapshot.store(connection, [(1000, 9.9), (1120, 3.5)], "grid_net", "power", "lts_hourly")
        check(again == 1, "re-inserting an existing (ts, role, source) is ignored")
        kept = connection.execute(
            "SELECT value FROM observation WHERE ts = 1000 AND role = 'grid_net'"
        ).fetchone()[0]
        check(kept == 1.5, "the original value is not overwritten")
        other_source = snapshot.store(connection, [(1000, 7.0)], "grid_net", "power", "stats_5min")
        check(other_source == 1, "the same instant from another source coexists")
        total = connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
        check(total == 4, "four rows total")

        print("role mapping")
        good = Path(directory) / "roles.json"
        good.write_text(json.dumps({"roles": {"grid_net": "sensor.example_a",
                                              "battery_soc": {"entity": "sensor.example_b", "kind": "percent"},
                                              "pv": {"entity": ""}}}))
        resolved = snapshot.load_roles(good)
        check(set(resolved) == {"grid_net", "battery_soc"}, "string shorthand works and blank entries are dropped")
        check(resolved["battery_soc"]["kind"] == "percent", "explicit kind is kept")
        check(resolved["grid_net"]["kind"] == "power", "kind defaults from the role vocabulary")

        bad = Path(directory) / "bad.json"
        bad.write_text(json.dumps({"roles": {"not_a_role": "sensor.example_c"}}))
        try:
            snapshot.load_roles(bad)
            check(False, "an unknown role is refused")
        except SystemExit:
            check(True, "an unknown role is refused")

        print("harvest with a stubbed transport")
        original = snapshot.call_statistics
        calls: list[tuple] = []

        def stub(hassio, entity, kind, start, end, period):
            calls.append((entity, kind, period))
            if entity == "sensor.example_boom":
                raise RuntimeError("simulated provider failure")
            return [(2000, 1.0), (3600, 2.0)] if period == "hour" else [(2000, 1.1)]

        snapshot.call_statistics = stub
        try:
            roles = {
                "grid_net": {"entity": "sensor.example_ok", "kind": "power"},
                "temp_outdoor": {"entity": "sensor.example_boom", "kind": "temperature"},
            }
            result = snapshot.harvest(connection, "hassio", roles, 4, 2, False)
        finally:
            snapshot.call_statistics = original
        check(result["roles_ok"] == {"grid_net": 3}, "the healthy role stored its hourly and 5-minute rows")
        check("temp_outdoor" in result["roles_failed"], "the failing role is reported, not swallowed")
        check(len(calls) >= 3, "both periods were requested")
        run = connection.execute(
            "SELECT roles_ok, roles_failed FROM harvest_run ORDER BY id DESC LIMIT 1"
        ).fetchone()
        check(json.loads(run[0]) == {"grid_net": 3}, "the run row records what succeeded")
        check("temp_outdoor" in json.loads(run[1]), "the run row records what failed")

        print("idempotent re-run")
        snapshot.call_statistics = stub
        try:
            second = snapshot.harvest(connection, "hassio", {"grid_net": {"entity": "sensor.example_ok", "kind": "power"}}, 4, 2, False)
        finally:
            snapshot.call_statistics = original
        check(second["new_rows"] == 0, "a repeat harvest adds nothing")

        print("status output")
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            snapshot.status(connection)
        text = buffer.getvalue()
        check("grid_net" in text and "lts_hourly" in text, "status lists coverage per role and source")

        connection.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
