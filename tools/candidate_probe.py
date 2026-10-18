#!/usr/bin/env python3
"""Probe (stage 4, gating question): do candidate input transforms differ at all?

Reads a recorded store read-only (``--store``, required; no default).  For a set of real decision
points (recorded plan rows, sampled with ``--stride``/``--limit``) it re-solves the *same* DP with
different input transforms and compares the resulting plans:

    det              the recorded plan's own vintage, unmodified (baseline)
    no_export        the same vintage with optimizer.allow_export = false
    pv_lo            pv_kw * 0.7
    load_hi          load_kw * 1.3
    stress           pv_kw * 0.7 and load_kw * 1.3
    sell_lo          sell * 0.85 (breaks sell == buy)
    buy_hi           buy * 1.15 and sell * 1.15 (uniform price level shift)
    noexport_pv_lo   allow_export = false and pv_kw * 0.7 (PV-dependent feasibility)
    noexport_stress  allow_export = false and pv_kw * 0.7, load_kw * 1.3
    floor_hi         sell floor raised to the window's 90th-percentile sell (binds)
    floor_pv_lo      floor_hi and pv_kw * 0.7 (floor-dependent feasibility)
    floor_stress     floor_hi and pv_kw * 0.7, load_kw * 1.3

``--sweep`` additionally grades pv down to 0.0 and load up to 5x (downward PV, upward load only).

Exactness: each transform solve is compared on the *whole* slot grid (equal length and identical
timestamps, no silent truncation).  ``exact_max_abs_diff_w`` is the unrounded maximum |Δpower| and
``exact_equal`` means grid match, zero power difference, zero action differences and equal
feasibility.  The ``no_diff_gt_1w`` view alone is thresholded at 1 W and is labelled as such.

As-of rule: every solve input comes from the decision's own `plans`/`plan_slots` row (the plan's
own forward price/pv/load vintage) plus `actuals` rows with ``ts <= plan.created_at`` (the start
state only).  A run whose `settings` rows postdate the earliest selected plan is NOT as-of and
refuses to run unless ``--allow-non-as-of`` is given (the summary then says ``"as_of": false``).
Settings keys absent from the store fall back to ``DEFAULTS`` and are listed in
``settings_defaults_used``.  Realized prices are used only for the *ex-post partial-prefix*
re-pricing, which is an open-loop pinned-candidate comparison, never an input to a solve, and is
not a value per horizon (windows overlap and no terminal energy is valued).

This is a probe, not evidence for a selector: no scenario set, no risk objective, no selection.
Each run prints its own sample description and can write a compact result artifact
(``--results``) recording the revision, the C-core build identity and the exact invocation.
Findings: docs/STAGE4_FINDINGS.md. Write ``--results`` outside the repository: the repo's PII audit
(tools/audit.sh) scans untracked files and reads precise power/price floats as coordinates, so an
artifact left inside the tree trips that gate (the probe warns and the file stays out of the commit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from energy_manager.app.config import DEFAULTS  # noqa: E402
from energy_manager.app.optimize import dp  # noqa: E402
from energy_manager.app.optimize import _fast  # noqa: E402

SLOT_MINUTES = 15
DISPATCH_EPS_W = 1.0
CHANGED_EPS_W = 25.0


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


class SettingsShim:
    """Store settings over DEFAULTS, tracking every key that had to fall back to a default."""

    def __init__(self, values: dict, updated: dict) -> None:
        self.values = values
        self.updated = updated
        self.used_defaults: set[str] = set()

    def get(self, key: str, default=None):
        if key in self.values:
            return self.values[key]
        if key in DEFAULTS:
            self.used_defaults.add(key)
            return DEFAULTS[key]
        return default


def load_store(path: str):
    import sqlite3

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    settings: dict = {}
    updated: dict = {}
    for row in cur.execute("select key, value_json, updated_at from settings"):
        settings[row["key"]] = json.loads(row["value_json"])
        updated[row["key"]] = parse_iso(row["updated_at"])
    plans = [dict(r) for r in cur.execute(
        "select id, created_at, horizon_start, horizon_end, feasible, summary_json, batteries_json"
        " from plans order by created_at, id"
    )]
    slots: dict[int, list[dict]] = {}
    for row in cur.execute("select * from plan_slots order by plan_id, t"):
        slots.setdefault(row["plan_id"], []).append(dict(row))
    actuals = [dict(r) for r in cur.execute("select * from actuals order by ts")]
    return con, settings, updated, plans, slots, actuals


def battery_aggregate(plan_batteries: list[dict], settings: dict) -> dp.BatteryAggregate:
    configured = {b["id"]: b for b in settings.get("batteries", [])}
    enabled = [b for b in plan_batteries if b.get("id") in configured]
    if not enabled:  # no settings entry: treat the plan snapshot as the whole story
        enabled = plan_batteries
    capacity = sum(float(b["capacity_kwh"]) for b in enabled)
    e_min = sum(
        float(b["capacity_kwh"]) * float(configured.get(b["id"], {}).get("min_soc_pct", 0.0)) / 100.0
        for b in enabled
    )
    e_max = sum(
        float(b["capacity_kwh"]) * float(configured.get(b["id"], {}).get("max_soc_pct", 100.0)) / 100.0
        for b in enabled
    )
    etas = [
        (float(b["capacity_kwh"]), float(configured.get(b["id"], {}).get("eta_charge", 1.0)),
         float(configured.get(b["id"], {}).get("eta_discharge", 1.0)))
        for b in enabled
    ]
    eta_charge = sum(c * ec for c, ec, _ in etas) / capacity if capacity else 1.0
    eta_discharge = sum(c * ed for c, _, ed in etas) / capacity if capacity else 1.0
    wear_w = sum(
        max(0.0, float(b["capacity_kwh"]) * (
            float(configured.get(b["id"], {}).get("max_soc_pct", 100.0)) / 100.0
            - float(configured.get(b["id"], {}).get("min_soc_pct", 0.0)) / 100.0))
        * float(configured.get(b["id"], {}).get("eta_discharge", 1.0))
        for b in enabled
    )
    wear = (
        sum(
            max(0.0, float(b["capacity_kwh"]) * (
                float(configured.get(b["id"], {}).get("max_soc_pct", 100.0)) / 100.0
                - float(configured.get(b["id"], {}).get("min_soc_pct", 0.0)) / 100.0))
            * float(configured.get(b["id"], {}).get("eta_discharge", 1.0))
            * float(b.get("wear_eur_per_kwh", 0.0))
            for b in enabled
        )
        / wear_w
        if wear_w > 0
        else 0.0
    )
    return dp.BatteryAggregate(
        capacity_kwh=capacity,
        e_min_kwh=e_min,
        e_max_kwh=e_max,
        p_charge_kw=sum(float(b["max_charge_w"]) for b in enabled) / 1000.0,
        p_discharge_kw=sum(float(b["max_discharge_w"]) for b in enabled) / 1000.0,
        eta_charge=eta_charge,
        eta_discharge=eta_discharge,
        usable_kwh=max(0.0, e_max - e_min),
        batteries=len(enabled),
        wear_eur_per_kwh=wear,
    )


def start_energy(actuals: list[dict], created_at: datetime, agg: dp.BatteryAggregate):
    """The latest measured fleet energy at or before the decision (as-of, never later)."""
    best = None
    for row in actuals:
        if row["soc_kwh"] is None:
            continue
        if parse_iso(row["ts"]) <= created_at:
            best = row
        else:
            break
    if best is None:
        return None, None
    return min(max(float(best["soc_kwh"]), agg.e_min_kwh), agg.e_max_kwh), parse_iso(best["ts"])


def build_slots(rows: list[dict]) -> list[dp.SlotInput]:
    return [
        dp.SlotInput(
            t=parse_iso(r["t"]),
            pv_kw=float(r["pv_kw"]),
            load_kw=float(r["load_kw"]),
            buy=float(r["buy"]),
            sell=float(r["sell"] if r["sell"] is not None else r["buy"]),
        )
        for r in rows
    ]


def window_floor(slots: list[dp.SlotInput]) -> float:
    """The window's 90th-percentile sell price: a floor that binds in the tail of the window."""
    sells = sorted(s.sell for s in slots)
    return float(sells[min(len(sells) - 1, int(round(0.9 * (len(sells) - 1))))])


def transform(name: str, slots: list[dp.SlotInput], options: dp.OptimizerOptions):
    if name == "det":
        return slots, options
    if name == "no_export":
        return slots, dp.replace(options, allow_export=False)
    if name == "pv_lo":
        return scale_slots(slots, pv=0.7), options
    if name == "load_hi":
        return scale_slots(slots, load=1.3), options
    if name == "stress":
        return scale_slots(slots, pv=0.7, load=1.3), options
    if name == "sell_lo":
        return scale_slots(slots, sell=0.85), options
    if name == "buy_hi":
        return scale_slots(slots, buy=1.15, sell=1.15), options
    if name == "noexport_pv_lo":
        return scale_slots(slots, pv=0.7), dp.replace(options, allow_export=False)
    if name == "noexport_stress":
        return scale_slots(slots, pv=0.7, load=1.3), dp.replace(options, allow_export=False)
    if name == "floor_hi":
        return slots, dp.replace(options, sell_floor_eur_per_kwh=window_floor(slots))
    if name == "floor_pv_lo":
        return scale_slots(slots, pv=0.7), dp.replace(
            options, sell_floor_eur_per_kwh=window_floor(slots)
        )
    if name == "floor_stress":
        return scale_slots(slots, pv=0.7, load=1.3), dp.replace(
            options, sell_floor_eur_per_kwh=window_floor(slots)
        )
    raise ValueError(name)


def scale_slots(slots: list[dp.SlotInput], pv: float = 1.0, load: float = 1.0,
                buy: float = 1.0, sell: float = 1.0):
    if pv == 1.0 and load == 1.0 and buy == 1.0 and sell == 1.0:
        return slots
    return [
        dp.replace(s, pv_kw=s.pv_kw * pv, load_kw=s.load_kw * load,
                   buy=s.buy * buy, sell=s.sell * sell)
        for s in slots
    ]


PV_SCALES = (0.9, 0.8, 0.7, 0.5, 0.3, 0.1, 0.0)
LOAD_SCALES = (1.1, 1.2, 1.3, 1.5, 2.0, 3.0, 5.0)


TRANSFORMS = (
    "det",
    "no_export",
    "pv_lo",
    "load_hi",
    "stress",
    "sell_lo",
    "buy_hi",
    "noexport_pv_lo",
    "noexport_stress",
    "floor_hi",
    "floor_pv_lo",
    "floor_stress",
)

#: Sweep solves per window: 7 PV levels + 7 load levels (downward PV, upward load only).
SWEEP_LEVELS = len(PV_SCALES) + len(LOAD_SCALES)

#: Options the recorded plan summary exposes per plan; everything else comes from `settings`/
#: `DEFAULTS` and is *not* attested by any plan row (named in every run's verification block).
OPTION_FIELDS_IN_SUMMARY = frozenset(
    {"slot_minutes", "wear_eur_per_kwh", "sell_floor_eur_per_kwh", "soc_step_kwh"}
)
OPTIONS_NOT_IN_SUMMARY = tuple(
    sorted(set(dp.OptimizerOptions.__dataclass_fields__) - OPTION_FIELDS_IN_SUMMARY)
)

#: The documented way the snapshot was taken (docs/REPLAY_HARNESS.md 'The store'): a read-only
#: source copied with SQLite's `.backup`, never a bare file copy -- an unflushed WAL truncates it.
STORE_RETRIEVAL = (
    'sqlite3 -readonly <live add-on store> ".backup <snapshot path>"  '
    "(docs/REPLAY_HARNESS.md 'The store': never a bare file copy; an unflushed WAL truncates it)"
)


def slot_bucket(t: datetime) -> datetime:
    return t - timedelta(
        minutes=t.minute % SLOT_MINUTES, seconds=t.second, microseconds=t.microsecond
    )


def realized_index(actuals: list[dict]) -> dict[datetime, dict]:
    """One realized row per 15-minute slot (the last row with a price), for ex-post re-pricing."""
    index: dict[datetime, dict] = {}
    for row in actuals:
        if row["price_buy"] is None:
            continue
        index[slot_bucket(parse_iso(row["ts"]))] = row
    return index


def realized_bucket(index: dict[datetime, dict], t: datetime):
    return index.get(t)


def _opt_match(used, recorded, tol: float = 1e-9):
    """True/False when the plan summary attests a value, None when it records nothing."""
    if recorded is None:
        return None
    try:
        return abs(float(used) - float(recorded)) <= tol
    except (TypeError, ValueError):
        return bool(used == recorded)


def git_revision() -> str | None:
    """The probe's own source revision, recorded so a later reader can reconcile the numbers."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, help="path to energy_manager.db (opened mode=ro)")
    ap.add_argument("--stride", "--step", dest="stride", type=int, default=4,
                    help="take every Nth plan row (default 4)")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of windows")
    ap.add_argument("--sweep", action="store_true", help="severity sweep on pv/load scales")
    ap.add_argument(
        "--results",
        default="",
        help="write a compact, privacy-safe result JSON here; keep it outside the repo "
        "(tools/audit.sh scans untracked files and flags precise floats)",
    )
    ap.add_argument(
        "--allow-non-as-of",
        action="store_true",
        help="run even when settings rows postdate the earliest selected plan (result is marked)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not _fast.available():
        print("FATAL: the C core is not available; build it first", file=sys.stderr)
        return 2
    store_path = Path(args.store).expanduser()
    if not store_path.is_file():
        print(f"FATAL: no store at {store_path}", file=sys.stderr)
        return 2

    con, settings_values, settings_updated, plans, plan_slots, actuals = load_store(str(store_path))
    chosen = [plans[i] for i in range(0, len(plans), max(1, args.stride))]
    if args.limit:
        chosen = chosen[: args.limit]
    if not chosen:
        print(f"FATAL: {store_path} has no plan rows", file=sys.stderr)
        return 2
    # As-of gate: the store keeps one row per settings key, so a row newer than a plan means that
    # plan's options cannot be reconstructed as-of.  Refuse rather than silently mix in the future.
    earliest_selected = min(parse_iso(p["created_at"]) for p in chosen)
    late_settings = {
        k: v.isoformat() for k, v in settings_updated.items() if v > earliest_selected
    }
    as_of = not late_settings
    if not as_of and not args.allow_non_as_of:
        print(
            "FATAL: not as-of: settings rows postdate the earliest selected plan"
            f" ({earliest_selected.isoformat()}): {', '.join(sorted(late_settings))};"
            " pass --allow-non-as-of to run anyway (the result will be marked as not as-of)",
            file=sys.stderr,
        )
        return 2

    invocation = "python3 " + " ".join(shlex.quote(a) for a in sys.argv)
    revision = git_revision()
    try:
        core_source_sha16 = hashlib.sha256(
            (ROOT / "energy_manager/app/optimize/_core.c").read_bytes()
        ).hexdigest()[:16]
    except OSError:
        core_source_sha16 = None
    core_build_sha = str(getattr(_fast._core, "build_sha", "") or "") or None
    try:
        probe_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        probe_sha256 = None

    settings = SettingsShim(settings_values, settings_updated)
    base_options = dp.OptimizerOptions.from_settings(settings)
    expost_index = realized_index(actuals)

    rows = []
    skipped = {"no_slots": 0, "no_start": 0, "no_price": 0}
    t0 = perf_counter()
    for plan in chosen:
        slots_rows = plan_slots.get(plan["id"], [])
        created_at = parse_iso(plan["created_at"])
        if len(slots_rows) < 8:
            skipped["no_slots"] += 1
            continue
        summary = json.loads(plan["summary_json"])
        aggregate = battery_aggregate(json.loads(plan["batteries_json"]), settings_values)
        e0, e0_at = start_energy(actuals, created_at, aggregate)
        if e0 is None:
            skipped["no_start"] += 1
            continue
        slots = build_slots(slots_rows)
        horizon_start = parse_iso(plan["horizon_start"])
        assert all(s.t >= horizon_start for s in slots), "slot before horizon start"
        floor = summary.get("sell_floor_eur_per_kwh")
        options = dp.replace(
            base_options,
            slot_minutes=SLOT_MINUTES,
            wear_eur_per_kwh=float(summary.get("wear_eur_per_kwh") or aggregate.wear_eur_per_kwh),
            sell_floor_eur_per_kwh=None if floor is None else float(floor),
        )
        result: dict[str, object] = {
            "plan_id": plan["id"],
            "created_at": plan["created_at"],
            "horizon_start": plan["horizon_start"],
        }
        plans_out = {}
        for name in TRANSFORMS:
            s_t, o_t = transform(name, slots, options)
            started = perf_counter()
            out = dp.solve(
                s_t,
                aggregate=aggregate,
                start_kwh=e0,
                options=o_t,
                generated_at=created_at,
                now=created_at,
                mode="advisory",
                dry_run=True,
            )
            plans_out[name] = out
            result[f"feasible_{name}"] = out.feasible
            result[f"ms_{name}"] = round((perf_counter() - started) * 1000.0, 2)
        det = plans_out["det"]
        det_power = [s.power_w for s in det.slots]
        det_actions = [s.action for s in det.slots]
        det_times = [s.t for s in det.slots]
        # first slot in force at the decision instant
        first = next(
            (i for i, s in enumerate(det.slots)
             if s.t <= created_at < s.t + timedelta(minutes=SLOT_MINUTES)),
            0,
        )
        per_transform = {}
        for name in TRANSFORMS[1:]:
            out = plans_out[name]
            power = [s.power_w for s in out.slots]
            actions = [s.action for s in out.slots]
            times = [s.t for s in out.slots]
            # Exactness is only meaningful on the same slot grid: equal length and timestamps.
            grid_match = len(power) == len(det_power) and times == det_times
            n = min(len(power), len(det_power))
            diffs = [abs(power[i] - det_power[i]) for i in range(n)]
            exact_max = max(diffs) if diffs else None
            action_diff = sum(1 for i in range(n) if actions[i] != det_actions[i])
            moved = [d for d in diffs if d > DISPATCH_EPS_W]
            first_ok = grid_match and first < n
            per_transform[name] = {
                "slots": n,
                "grid_match": grid_match,
                "feasible": bool(out.feasible),
                "infeasible_reason": out.infeasible_reason,
                "action_diff_slots": action_diff,
                "exact_max_abs_diff_w": None if exact_max is None else exact_max,
                "exact_equal": bool(
                    grid_match and exact_max == 0.0 and action_diff == 0
                    and bool(out.feasible) == bool(det.feasible)
                ),
                "power_exact_zero": bool(grid_match and exact_max == 0.0),
                "no_diff_gt_1w": bool(
                    grid_match and exact_max is not None and exact_max <= DISPATCH_EPS_W
                ),
                "power_diff_frac": round(len(moved) / n, 4) if n else 0.0,
                "max_abs_diff_w": round(max(diffs), 2) if diffs else 0.0,
                "median_abs_diff_w": round(statistics.median(diffs), 2) if diffs else 0.0,
                "median_moved_diff_w": round(statistics.median(moved), 2) if moved else 0.0,
                "first_slot_differs": bool(
                    first_ok
                    and (
                        actions[first] != det_actions[first]
                        or abs(power[first] - det_power[first]) > DISPATCH_EPS_W
                    )
                ),
                "first_slot_power_differs": bool(
                    first_ok and abs(power[first] - det_power[first]) > DISPATCH_EPS_W
                ),
                "first_slot_action_only": bool(
                    first_ok
                    and actions[first] != det_actions[first]
                    and abs(power[first] - det_power[first]) <= DISPATCH_EPS_W
                ),
                "first_slot_changed_25w": bool(
                    first_ok
                    and (
                        actions[first] != det_actions[first]
                        or abs(power[first] - det_power[first]) > CHANGED_EPS_W
                    )
                ),
                "first_slot_delta_w": (
                    round(power[first] - det_power[first], 2) if first_ok else None
                ),
            }
        # ex-post re-pricing (open-loop pinned-candidate comparison; never a solve input)
        expost = {}
        for name in TRANSFORMS:
            out = plans_out[name]
            ex_sum = 0.0
            ex_first = None
            covered = 0
            for i, s in enumerate(out.slots):
                bucket = realized_bucket(expost_index, s.t)
                if bucket is None:
                    continue
                pv = bucket["pv_derived_kw"] if bucket["pv_derived_kw"] is not None else bucket["pv_kw"]
                load = bucket["load_derived_kw"] if bucket["load_derived_kw"] is not None else bucket["load_kw"]
                if pv is None or load is None:
                    pv, load = s.pv_kw, s.load_kw
                buy = float(bucket["price_buy"])
                sell = float(bucket["price_sell"] if bucket["price_sell"] is not None else buy)
                grid = float(load) - float(pv) + s.power_w / 1000.0
                cost = (SLOT_MINUTES / 60.0) * (buy * max(grid, 0.0) - sell * max(-grid, 0.0))
                ex_sum += cost
                covered += 1
                if i == first:
                    ex_first = cost
            expost[name] = {
                "covered_slots": covered,
                "horizon_cost_eur": round(ex_sum, 4) if covered >= 4 else None,
                "first_slot_cost_eur": None if ex_first is None else round(ex_first, 4),
            }
        result["per_transform"] = per_transform
        result["expost"] = expost
        # fidelity: how close is the det re-solve to the recorded plan (exact, on the same grid)?
        rec_times = [parse_iso(r["t"]) for r in slots_rows]
        rec_actions = [r["action"] for r in slots_rows]
        rec_power = [float(r["power_w"]) for r in slots_rows]
        rec_grid_match = len(rec_times) == len(det_times) and rec_times == det_times
        n = min(len(rec_power), len(det_power))
        slot_errs = [abs(rec_power[i] - det_power[i]) for i in range(n)]
        result["det_vs_recorded"] = {
            "slots_recorded": len(rec_power),
            "slots_solved": len(det_power),
            "grid_match": rec_grid_match,
            "action_mismatches": sum(1 for i in range(n) if rec_actions[i] != det_actions[i]),
            "mean_abs_error_w": (sum(slot_errs) / n) if n else None,
            "max_abs_slot_error_w": max(slot_errs) if slot_errs else None,
            "exact_reproduction": bool(
                rec_grid_match
                and slot_errs
                and max(slot_errs) == 0.0
                and all(rec_actions[i] == det_actions[i] for i in range(n))
            ),
        }
        result["options_audit"] = {
            "checks": {
                "slot_minutes": {
                    "used": SLOT_MINUTES,
                    "recorded": summary.get("slot_minutes"),
                    "matches": _opt_match(SLOT_MINUTES, summary.get("slot_minutes")),
                },
                "wear_eur_per_kwh": {
                    "used": options.wear_eur_per_kwh,
                    "recorded": summary.get("wear_eur_per_kwh"),
                    "matches": _opt_match(options.wear_eur_per_kwh, summary.get("wear_eur_per_kwh")),
                },
                "sell_floor_eur_per_kwh": {
                    "used": options.sell_floor_eur_per_kwh,
                    "recorded": summary.get("sell_floor_eur_per_kwh"),
                    "matches": _opt_match(
                        options.sell_floor_eur_per_kwh, summary.get("sell_floor_eur_per_kwh")
                    ),
                },
                "soc_step_kwh": {
                    "used": options.soc_step_kwh,
                    "recorded_field": "effective_step_kwh",
                    "recorded": summary.get("effective_step_kwh"),
                    "matches": _opt_match(options.soc_step_kwh, summary.get("effective_step_kwh")),
                },
                "eta_charge": {
                    "used": aggregate.eta_charge,
                    "recorded": summary.get("eta_charge"),
                    "matches": _opt_match(aggregate.eta_charge, summary.get("eta_charge")),
                },
                "eta_discharge": {
                    "used": aggregate.eta_discharge,
                    "recorded": summary.get("eta_discharge"),
                    "matches": _opt_match(aggregate.eta_discharge, summary.get("eta_discharge")),
                },
            },
            "mismatched": [],
            "not_attested_by_summary": list(OPTIONS_NOT_IN_SUMMARY),
            "det_feasible": bool(det.feasible),
            "det_infeasible_reason": det.infeasible_reason,
            "start_lag_s": (created_at - e0_at).total_seconds() if e0_at else None,
        }
        result["options_audit"]["mismatched"] = sorted(
            name
            for name, check in result["options_audit"]["checks"].items()
            if check["matches"] is False
        )
        # The conditional-separability test: does a PV/load perturbation change the plan when the
        # *feasible set* is PV/load-dependent (export off, or a binding floor)?  Pairwise, so the
        # floor's own effect is not mistaken for the perturbation's.
        interactions = {}
        for label, base_name, perturbed_name in (
            ("noexport_pv_lo", "no_export", "noexport_pv_lo"),
            ("noexport_stress", "no_export", "noexport_stress"),
            ("floor_pv_lo", "floor_hi", "floor_pv_lo"),
            ("floor_stress", "floor_hi", "floor_stress"),
        ):
            pairs_base = plans_out[base_name]
            pairs_pert = plans_out[perturbed_name]
            base_slots = pairs_base.slots
            pert_slots = pairs_pert.slots
            power_a = [s.power_w for s in base_slots]
            power_b = [s.power_w for s in pert_slots]
            times_a = [s.t for s in base_slots]
            times_b = [s.t for s in pert_slots]
            grid_ok = len(power_a) == len(power_b) and times_a == times_b
            m = min(len(power_a), len(power_b))
            pair_diffs = [abs(power_a[i] - power_b[i]) for i in range(m)]
            first_ok = grid_ok and first < m
            interactions[label] = {
                "grid_match": grid_ok,
                "feasible_base": bool(pairs_base.feasible),
                "feasible_perturbed": bool(pairs_pert.feasible),
                "exact_max_abs_diff_w": max(pair_diffs) if pair_diffs else None,
                "power_exact_equal": bool(grid_ok and pair_diffs and max(pair_diffs) == 0.0),
                "exact_equal": bool(
                    grid_ok
                    and pair_diffs
                    and max(pair_diffs) == 0.0
                    and all(base_slots[i].action == pert_slots[i].action for i in range(m))
                ),
                "first_slot_power_differs": bool(
                    first_ok and abs(power_a[first] - power_b[first]) > DISPATCH_EPS_W
                ),
                "power_diff_frac": (
                    round(sum(1 for d in pair_diffs if d > DISPATCH_EPS_W) / m, 4) if m else 0.0
                ),
            }
        result["interactions"] = interactions
        result["start_kwh"] = round(e0, 4)
        if args.sweep:
            sensitivity: dict[str, dict] = {"pv": {}, "load": {}}
            for label, scales, key in (
                ("pv", PV_SCALES, "pv"),
                ("load", LOAD_SCALES, "load"),
            ):
                for scale in scales:
                    s_t = scale_slots(
                        slots,
                        pv=scale if key == "pv" else 1.0,
                        load=scale if key == "load" else 1.0,
                    )
                    out = dp.solve(
                        s_t,
                        aggregate=aggregate,
                        start_kwh=e0,
                        options=options,
                        generated_at=created_at,
                        now=created_at,
                        mode="advisory",
                        dry_run=True,
                    )
                    power = [s.power_w for s in out.slots]
                    times = [s.t for s in out.slots]
                    actions = [s.action for s in out.slots]
                    # The same grid/length/feasibility qualification as the main transforms: a
                    # truncated or infeasible sweep solve must not count as "unchanged".
                    grid_ok = len(power) == len(det_power) and times == det_times
                    n = min(len(power), len(det_power))
                    diffs = [abs(power[i] - det_power[i]) for i in range(n)]
                    exact_max = max(diffs) if diffs else None
                    action_diff = sum(1 for i in range(n) if actions[i] != det_actions[i])
                    feasible = bool(out.feasible)
                    first_ok = grid_ok and first < n
                    sensitivity[label][str(scale)] = {
                        "grid_match": grid_ok,
                        "slots": n,
                        "feasible": feasible,
                        "infeasible_reason": out.infeasible_reason,
                        "power_exact_zero": bool(grid_ok and exact_max == 0.0),
                        "exact_equal": bool(
                            grid_ok and exact_max == 0.0 and action_diff == 0
                            and feasible == bool(det.feasible)
                        ),
                        "action_diff_slots": action_diff,
                        "first_slot_power_differs": bool(
                            first_ok and abs(power[first] - det_power[first]) > DISPATCH_EPS_W
                        ),
                        "any_slot_power_differs": any(
                            abs(power[i] - det_power[i]) > DISPATCH_EPS_W for i in range(n)
                        ),
                        "power_diff_frac": (
                            round(
                                sum(
                                    1
                                    for i in range(n)
                                    if abs(power[i] - det_power[i]) > DISPATCH_EPS_W
                                )
                                / n,
                                4,
                            )
                            if n
                            else 0.0
                        ),
                        "exact_max_abs_diff_w": exact_max,
                    }
            result["sensitivity"] = sensitivity
        result["start_at"] = e0_at.isoformat() if e0_at else None
        rows.append(result)

    elapsed = perf_counter() - t0

    def median(values):
        vals = [v for v in values if v is not None]
        return round(statistics.median(vals), 4) if vals else None

    summary_out = {
        "sample": {
            "store": str(store_path),
            "plans_in_store": len(plans),
            "store_span": [plans[0]["created_at"], plans[-1]["created_at"]] if plans else None,
            "stride": args.stride,
            "limit": args.limit or None,
            "windows_selected": len(chosen),
            "windows_scored": len(rows),
            "skipped": skipped,
            "distinct_vintages": len({r["horizon_start"] for r in rows}),
            "distinct_start_readings": len({r["start_at"] for r in rows}),
            "slot_minutes": SLOT_MINUTES,
            "c_core": _fast.available(),
            "elapsed_s": round(elapsed, 2),
        },
        "store": str(store_path),
        "windows_selected": len(chosen),
        "windows_scored": len(rows),
        "distinct_horizon_starts_scored": len(
            {r["horizon_start"] for r in rows}
        ),
        "skipped": skipped,
        "step": args.stride,
        "transforms": list(TRANSFORMS),
        "slot_minutes": SLOT_MINUTES,
        "settings_after_first_plan": late_settings,
        "c_core": _fast.available(),
        "elapsed_s": round(elapsed, 2),
        "det_vs_recorded_exact_windows": sum(
            1 for r in rows if r["det_vs_recorded"]["exact_reproduction"]
        ),
        "det_vs_recorded_max_abs_slot_error_w_over_windows": max(
            (
                r["det_vs_recorded"]["max_abs_slot_error_w"]
                for r in rows
                if r["det_vs_recorded"]["max_abs_slot_error_w"] is not None
            ),
            default=None,
        ),
        "any_plan_difference_windows": {
            name: sum(1 for r in rows if r["per_transform"][name]["power_diff_frac"] > 0.0)
            for name in TRANSFORMS[1:]
        },
        "any_action_difference_windows": {
            name: sum(
                1 for r in rows if r["per_transform"][name]["action_diff_slots"] > 0
            )
            for name in TRANSFORMS[1:]
        },
        "first_slot_differs_windows": {
            name: sum(1 for r in rows if r["per_transform"][name]["first_slot_differs"])
            for name in TRANSFORMS[1:]
        },
        "first_slot_power_differs_windows": {
            name: sum(1 for r in rows if r["per_transform"][name]["first_slot_power_differs"])
            for name in TRANSFORMS[1:]
        },
        "first_slot_action_only_windows": {
            name: sum(1 for r in rows if r["per_transform"][name]["first_slot_action_only"])
            for name in TRANSFORMS[1:]
        },
        "action_diff_slots_median": {
            name: median([r["per_transform"][name]["action_diff_slots"] for r in rows])
            for name in TRANSFORMS[1:]
        },
        "first_slot_changed_25w_windows": {
            name: sum(1 for r in rows if r["per_transform"][name]["first_slot_changed_25w"])
            for name in TRANSFORMS[1:]
        },
        "power_diff_frac_median": {
            name: median([r["per_transform"][name]["power_diff_frac"] for r in rows])
            for name in TRANSFORMS[1:]
        },
        "max_abs_diff_w_median": {
            name: median([r["per_transform"][name]["max_abs_diff_w"] for r in rows])
            for name in TRANSFORMS[1:]
        },
        "max_abs_diff_w_over_windows": {
            name: max((r["per_transform"][name]["max_abs_diff_w"] for r in rows), default=None)
            for name in TRANSFORMS[1:]
        },
        "expost_partial_prefix_delta_eur_median": {
            name: median(
                [
                    r["expost"][name]["horizon_cost_eur"] - r["expost"]["det"]["horizon_cost_eur"]
                    for r in rows
                    if r["expost"][name]["horizon_cost_eur"] is not None
                    and r["expost"]["det"]["horizon_cost_eur"] is not None
                ]
            )
            for name in TRANSFORMS[1:]
        },
        "expost_partial_prefix_windows": {
            name: sum(
                1
                for r in rows
                if r["expost"][name]["horizon_cost_eur"] is not None
                and r["expost"]["det"]["horizon_cost_eur"] is not None
            )
            for name in TRANSFORMS[1:]
        },
        "expost_partial_prefix_delta_eur_total": {
            name: round(
                sum(
                    r["expost"][name]["horizon_cost_eur"] - r["expost"]["det"]["horizon_cost_eur"]
                    for r in rows
                    if r["expost"][name]["horizon_cost_eur"] is not None
                    and r["expost"]["det"]["horizon_cost_eur"] is not None
                ),
                4,
            )
            for name in TRANSFORMS[1:]
        },
        "expost_partial_prefix_first_slot_delta_eur_total": {
            name: round(
                sum(
                    r["expost"][name]["first_slot_cost_eur"] - r["expost"]["det"]["first_slot_cost_eur"]
                    for r in rows
                    if r["expost"][name]["first_slot_cost_eur"] is not None
                    and r["expost"]["det"]["first_slot_cost_eur"] is not None
                ),
                4,
            )
            for name in TRANSFORMS[1:]
        },
        "expost_partial_prefix_first_slot_windows": {
            name: sum(
                1
                for r in rows
                if r["expost"][name]["first_slot_cost_eur"] is not None
                and r["expost"]["det"]["first_slot_cost_eur"] is not None
            )
            for name in TRANSFORMS[1:]
        },
        "solve_ms_median": {
            name: median([r[f"ms_{name}"] for r in rows]) for name in TRANSFORMS
        },
    }

    def invariance(block_rows):
        out = {}
        for name in TRANSFORMS[1:]:
            ft = [r["per_transform"][name] for r in block_rows]
            out[name] = {
                "windows": len(ft),
                "exact_equal_windows": sum(1 for v in ft if v["exact_equal"]),
                "no_diff_gt_1w_windows": sum(1 for v in ft if v["no_diff_gt_1w"]),
                "any_power_diff_windows": sum(1 for v in ft if v["power_diff_frac"] > 0),
                "first_slot_power_differs_windows": sum(
                    1 for v in ft if v["first_slot_power_differs"]
                ),
                "first_slot_action_only_windows": sum(
                    1 for v in ft if v["first_slot_action_only"]
                ),
                "grid_mismatch_windows": sum(1 for v in ft if not v["grid_match"]),
                "infeasible_windows": sum(1 for v in ft if not v["feasible"]),
                "infeasible_reasons": sorted(
                    {v["infeasible_reason"] for v in ft if not v["feasible"]}
                ),
                "power_exact_zero_windows": sum(
                    1 for v in ft if v["grid_match"] and v["exact_max_abs_diff_w"] == 0.0
                ),
                "label_mismatch_windows": sum(1 for v in ft if v["action_diff_slots"] > 0),
                "max_abs_diff_exact_w_over_windows": max(
                    (
                        v["exact_max_abs_diff_w"]
                        for v in ft
                        if v["exact_max_abs_diff_w"] is not None
                    ),
                    default=None,
                ),
                "power_diff_frac_median": median([v["power_diff_frac"] for v in ft]),
                "median_moved_diff_w": median(
                    [v["median_moved_diff_w"] for v in ft if v["median_moved_diff_w"] > 0]
                ),
            }
        return out

    exact_rows = [r for r in rows if r["det_vs_recorded"]["exact_reproduction"]]
    # Pin the measurement input: the snapshot's bytes, not just its path (it lives outside the
    # repo).  A bare copy can truncate an unflushed WAL, so the WAL size is recorded too.
    store_stat = store_path.stat()
    store_sha256 = hashlib.sha256()
    with store_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            store_sha256.update(chunk)
    wal_path = store_path.with_name(store_path.name + "-wal")
    store_bytes = store_stat.st_size
    store_mtime = datetime.fromtimestamp(store_stat.st_mtime).astimezone().isoformat()
    store_wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    inexact_errors = [
        r["det_vs_recorded"]["max_abs_slot_error_w"]
        for r in rows
        if not r["det_vs_recorded"]["exact_reproduction"]
        and r["det_vs_recorded"]["max_abs_slot_error_w"] is not None
    ]
    summary_out["verification"] = {
        "probe_revision": revision,
        "probe_sha256": probe_sha256,
        "store_sha256": store_sha256.hexdigest(),
        "store_bytes": store_bytes,
        "store_mtime": store_mtime,
        "store_wal_bytes": store_wal_bytes,
        "store_retrieval": STORE_RETRIEVAL,
        "core": {
            "available": _fast.available(),
            "build_sha": core_build_sha,
            "source_sha16": core_source_sha16,
        },
        "invocation": invocation,
        "as_of": as_of,
        "settings_defaults_used": sorted(settings.used_defaults),
        "store": str(store_path),
        "plans_in_store": len(plans),
    }
    summary_out["fidelity"] = {
        "windows": len(rows),
        "exact_reproduction_windows": len(exact_rows),
        "grid_mismatch_windows": sum(1 for r in rows if not r["det_vs_recorded"]["grid_match"]),
        "max_abs_slot_error_w_over_windows": summary_out[
            "det_vs_recorded_max_abs_slot_error_w_over_windows"
        ],
        "max_abs_slot_error_w_over_inexact_windows": max(inexact_errors, default=None),
        "max_action_mismatches": max(
            (r["det_vs_recorded"]["action_mismatches"] for r in rows), default=None
        ),
        "windows_with_option_mismatches": sum(1 for r in rows if r["options_audit"]["mismatched"]),
        "option_checks": (
            {
                name: {
                    "recorded_windows": sum(
                        1
                        for r in rows
                        if r["options_audit"]["checks"][name]["matches"] is not None
                    ),
                    "mismatch_windows": sum(
                        1
                        for r in rows
                        if r["options_audit"]["checks"][name]["matches"] is False
                    ),
                }
                for name in sorted(rows[0]["options_audit"]["checks"])
            }
            if rows
            else {}
        ),
        "options_not_attested_by_summary": list(OPTIONS_NOT_IN_SUMMARY),
    }
    summary_out["per_transform_all"] = invariance(rows)
    summary_out["per_transform_exact_windows"] = invariance(exact_rows)

    def interaction_summary(block_rows):
        out = {}
        for label in ("noexport_pv_lo", "noexport_stress", "floor_pv_lo", "floor_stress"):
            ft = [r["interactions"][label] for r in block_rows]
            out[label] = {
                "windows": len(ft),
                "power_exact_equal_windows": sum(1 for v in ft if v["power_exact_equal"]),
                "any_power_diff_windows": sum(1 for v in ft if v["power_diff_frac"] > 0),
                "first_slot_power_differs_windows": sum(
                    1 for v in ft if v["first_slot_power_differs"]
                ),
                "feasible_windows": sum(
                    1 for v in ft if v["feasible_base"] and v["feasible_perturbed"]
                ),
                "max_abs_diff_exact_w_over_windows": max(
                    (
                        v["exact_max_abs_diff_w"]
                        for v in ft
                        if v["exact_max_abs_diff_w"] is not None
                    ),
                    default=None,
                ),
                "power_diff_frac_median": median([v["power_diff_frac"] for v in ft]),
            }
        return out

    summary_out["interaction_all"] = interaction_summary(rows)
    summary_out["interaction_exact_windows"] = interaction_summary(exact_rows)
    summary_out["det_infeasible_windows"] = sum(
        1 for r in rows if not r["options_audit"]["det_feasible"]
    )
    summary_out["solve_counts"] = {
        "windows_scored": len(rows),
        "transforms_per_window": len(TRANSFORMS),
        "sweep_solves_per_window": SWEEP_LEVELS if args.sweep else 0,
        "total_solves": len(rows) * (len(TRANSFORMS) + (SWEEP_LEVELS if args.sweep else 0)),
    }
    if args.sweep:
        for label, scales in (("pv", PV_SCALES), ("load", LOAD_SCALES)):
            level_summary = {}
            for scale in scales:
                scored = [
                    r["sensitivity"][label][str(scale)]
                    for r in rows
                    if "sensitivity" in r
                ]
                level_summary[str(scale)] = {
                    "windows": len(scored),
                    "power_exact_zero_windows": sum(
                        1 for v in scored if v["power_exact_zero"]
                    ),
                    "exact_equal_windows": sum(1 for v in scored if v["exact_equal"]),
                    "any_power_diff_windows": sum(
                        1 for v in scored if v["any_slot_power_differs"]
                    ),
                    "first_slot_power_differs_windows": sum(
                        1 for v in scored if v["first_slot_power_differs"]
                    ),
                    "grid_mismatch_windows": sum(1 for v in scored if not v["grid_match"]),
                    "infeasible_windows": sum(1 for v in scored if not v["feasible"]),
                    "infeasible_reasons": sorted(
                        {v["infeasible_reason"] for v in scored if not v["feasible"]}
                    ),
                    "action_diff_slots_median": median(
                        [v["action_diff_slots"] for v in scored]
                    ),
                    "power_diff_frac_median": median(
                        [v["power_diff_frac"] for v in scored]
                    ),
                    "max_abs_diff_exact_w": max(
                        (
                            v["exact_max_abs_diff_w"]
                            for v in scored
                            if v["exact_max_abs_diff_w"] is not None
                        ),
                        default=None,
                    ),
                }
            summary_out[f"{label}_scale_sensitivity"] = level_summary
    if args.results:
        result_summary = {
            "per_transform_all": summary_out["per_transform_all"],
            "per_transform_exact_windows": summary_out["per_transform_exact_windows"],
            "expost_partial_prefix_delta_eur_total": summary_out.get(
                "expost_partial_prefix_delta_eur_total"
            ),
            "expost_partial_prefix_windows": summary_out.get("expost_partial_prefix_windows"),
        }
        for key in ("pv_scale_sensitivity", "load_scale_sensitivity"):
            if key in summary_out:
                result_summary[key] = summary_out[key]
        result_summary["interaction_all"] = summary_out["interaction_all"]
        result_summary["interaction_exact_windows"] = summary_out["interaction_exact_windows"]
        payload = {
            "meta": summary_out["verification"],
            "sample": summary_out["sample"],
            "fidelity": summary_out["fidelity"],
            "solve_counts": summary_out["solve_counts"],
            "summary": result_summary,
            "windows": [
                {
                    "plan_id": r["plan_id"],
                    "created_at": r["created_at"],
                    "horizon_start": r["horizon_start"],
                    "start_kwh": r["start_kwh"],
                    "start_at": r["start_at"],
                    "det_vs_recorded": r["det_vs_recorded"],
                    "options_audit": r["options_audit"],
                    "per_transform": {
                        name: {
                            k: v
                            for k, v in r["per_transform"][name].items()
                            if k
                            in (
                                "grid_match",
                                "feasible",
                                "infeasible_reason",
                                "exact_equal",
                                "no_diff_gt_1w",
                                "exact_max_abs_diff_w",
                                "action_diff_slots",
                                "power_diff_frac",
                                "first_slot_power_differs",
                                "first_slot_delta_w",
                            )
                        }
                        for name in TRANSFORMS[1:]
                    },
                    "expost_partial_prefix": {
                        name: r["expost"][name]
                        for name in ("det", "no_export")
                        if name in r["expost"]
                    },
                    "interactions": r["interactions"],
                    "sweep": {
                        label: {
                            str(scale): {
                                "grid_match": v["grid_match"],
                                "feasible": v["feasible"],
                                "power_exact_zero": v["power_exact_zero"],
                                "exact_max_abs_diff_w": v["exact_max_abs_diff_w"],
                            }
                            for scale, v in levels.items()
                        }
                        for label, levels in r.get("sensitivity", {}).items()
                    },
                }
                for r in rows
            ],
        }
        out = Path(args.results).resolve()
        repo_root = Path(__file__).resolve().parent.parent
        if repo_root in out.parents:
            print(
                f"warning: {args.results} is inside the repository; tools/audit.sh scans untracked "
                "files and reads precise power/price floats as coordinates — keep the artifact out "
                "of the tree or it breaks the audit gate",
                file=sys.stderr,
            )
        out.write_text(json.dumps(payload, default=str) + "\n")
        print(f"results: {out}", file=sys.stderr)

    if args.json:
        print(json.dumps({"summary": summary_out, "windows": rows}, indent=1, default=str))
    else:
        print(json.dumps(summary_out, indent=1, default=str))
    print(
        f"sample: {len(rows)} windows ({len({r['horizon_start'] for r in rows})} distinct vintages)"
        f" of {len(chosen)} selected from {len(plans)} plans in {store_path}",
        file=sys.stderr,
    )
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
