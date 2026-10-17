"""The replay report: the three fidelity gates, the arm deltas, and a deterministic rendering.

docs/REPLAY_HARNESS.md sections 4-7: the report is the evidence, so it has to be readable without
the store and identical for identical inputs.  Two rules shape everything here.

*Nothing is blended.*  Each number names its own source: an arm's flows come from its own scratch
copy, the settlement prices from the recording, the plant's coverage from the runner's tick tallies,
and the provenance of the pv/load base from the episode -- so a derived (modelled) base can never
read as a measured one, and a window whose plant could not be simulated carries no money at all.

*Nothing is overstated.*  A gate that could not be measured says ``passed: null`` with its reason; a
gate that failed withholds the arm's deltas instead of printing a number that was not earned; and
every gap the harness does not ship is labelled in the manifest's own words (``not_shipped`` /
``empirical``) rather than left to the reader's optimism.

The gates, in the order they are evaluated (:data:`GATE_SLUGS`):

* ``plant_fidelity`` -- the substitute recorded-policy leg against the real recorded plant.  It
  measures how much of a delta comes from the plant *model* rather than from a policy, which is part
  of gate 1's attribution and is deliberately *not* folded into the E4 delta.
* ``decision_fidelity`` -- the replay's own setpoints and plan slots against the recording's, one
  planner grid step apart at most, with the critical quantities fatal and ``reason``/``code``
  reported only.
* ``accounting_fidelity`` -- the arm's flows against the recording's reference rows, and the cost
  basis the engine actually booked against the one frozen at window start.

Only the recorded, production-policy leg can be judged by gates 2 and 3: the other legs exist to be
*different*, so judging them against the recording would be a category error.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..cost_basis import PROFILE_KIND as COST_BASIS_KIND
from ..models import from_iso, quarter_floor, to_iso
from .accounting import (
    EXPORT_REVENUE_UNVERIFIED,
    TARIFF_MODEL_UNAVAILABLE,
    LegAccount,
    PairedDelta,
)
from .manifest import SAA_NOT_SHIPPED, Manifest, PlantCeiling, Tolerances
from .runner import LegResult
from .statistics import EPISODE_EVIDENCE_REQUIREMENT, gap_windows, plan_horizon_windows

REPORT_SCHEMA = "replay-report/1"

#: The only keys allowed to differ between two runs of the same manifest (the determinism rule).
VOLATILE_KEYS = ("runtime_s", "generated_at")

#: Evidence labels: an arm that ran exactly as declared, or a labelled substitute for one that does
#: not exist yet (E3's terminal vector, E4's expected-cost SAA).  Never silently the same thing.
DECLARED = "declared"
EMPIRICAL = "empirical"
EMPIRICAL_ARMS = ("E3", "E4")

PLANT_FIDELITY = "plant_fidelity"
DECISION_FIDELITY = "decision_fidelity"
ACCOUNTING_FIDELITY = "accounting_fidelity"
GATE_SLUGS = (PLANT_FIDELITY, DECISION_FIDELITY, ACCOUNTING_FIDELITY)

#: What the substitute recorded-policy leg can and cannot measure, stated where the number is.
PLANT_FIDELITY_LIMITS = (
    "the substitute leg stands in only for the deciding policy, so its divergence from the real "
    "recorded plant is the plant-model term of gate 1's attribution",
    "the recorded plant never evaluates the site export bound, so its unverified-bounds ceiling is "
    "declared not applicable instead of met",
)

#: A setpoint difference below this is the same setpoint; the grid step is the tolerance that matters.
POWER_EPSILON_W = 1e-6
#: Findings are evidence, not a log: each gate keeps this many and counts the rest.
FINDING_LIMIT = 24

NO_RECORDED_LEG = "no_recorded_leg"
#: Gate 1's reference term could not cover every window: an unknown divergence is a hole, not a
#: perfect match, so the gate is not measurable instead of passing on the windows that ran.
REFERENCE_DIVERGENCE_UNKNOWN = "reference_divergence_unknown"
#: The plant-model term could not cover every window for the same reason.
PLANT_MODEL_UNKNOWN = "plant_model_unknown"
NOT_COMPARABLE = "not_comparable"
GATE_FAILED = "gate_failed"
REPORT_INVALID = "report_invalid"
FLOOR_STALL = "floor_stall"

# The declared-contract verdicts.  Derived from the refusal evidence, never asserted: a delta that
# mixes a tariff effect with a refusal to price export is not a tariff error, so the verdict travels
# with the number it produced.
CONTRACT_RESOLVED = "resolved"
CONTRACT_PARTIAL_REFUSAL = "partial_refusal"
CONTRACT_REFUSES_EXPORT = "refuses_export"
CONTRACT_UNKNOWN = "unknown"

#: One line a reader can act on whenever export was not fully priced.
UNCLEAN_CONTRACT_NOTE = (
    "not a clean tariff delta: the declared model refused to price some export, so declare the money "
    "(--setting price.feed_in_eur_per_kwh=...) for the clean run"
)

#: E4's pair is null by construction: the recording was produced by the production policy itself, so
#: the recorded-policy leg reproduces its own commands.  Zero here is an arithmetic identity, not
#: evidence that no policy gain exists.
RECORDED_POLICY_EQUALS_PRODUCTION = "recorded_policy_equals_production"
RECORDED_POLICY_NOTE = (
    "the recording was produced by the production policy, so the recorded-policy leg reproduces its "
    "own commands and this pair is 0 by construction -- not a measured policy gain, and evidence "
    "only that the command substitution works"
)

#: Why a pair can co-exist with gates: the anchor leg is what the gates judge.
ANCHOR_LEG_IS_THE_FIDELITY_REFERENCE = "anchor_leg_is_the_fidelity_reference"
REFERENCE_LEG_NOTE = (
    "the pair is a declared same-plant comparison, and the pair's own gates 1-3 remain not "
    "applicable because neither paired leg has a recorded counterpart"
)

#: A bootstrap whose interval is not evidence because the episode is shorter than one plan horizon.
ILLUSTRATIVE_INTERVAL = "illustrative_interval"

#: The comparand gate 3's basis term uses: the recording's own cost-basis row, which may sit after
#: the window end because no leg's decision can read it (:meth:`Episode.source_profile`).
SOURCE_BASIS_COMPARAND = "source_recording_profile_row"

#: Journal codes and plan-slot reasons that mean the declared contract refused to price export.
FEED_IN_REFUSALS = ("feed_in_unpriceable", "unpriced_export", "feed_in_unavailable", "feed_in_withheld")


class ReportError(RuntimeError):
    """A report could not be built or rendered; ``reason`` is the stable slug."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# ------------------------------------------------------------------------------- gates and findings


@dataclass(frozen=True)
class Finding:
    """One disagreement, with both sides named so a reader can re-check it in the store."""

    kind: str
    bucket: datetime
    label: str
    replayed: Any
    recorded: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bucket": to_iso(self.bucket),
            "label": self.label,
            "replayed": self.replayed,
            "recorded": self.recorded,
        }


@dataclass(frozen=True)
class GateVerdict:
    """One gate: passed, failed, or not measurable -- never a silent pass."""

    gate: str
    passed: bool | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return self.passed is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "measured": self.measured,
            "detail": dict(self.detail),
            "reasons": list(self.reasons),
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class LegRun:
    """One driven leg and its own account: the pair every gate and delta works on."""

    result: LegResult
    account: LegAccount


@dataclass(frozen=True)
class Isolation:
    """The E4 posture cross-check: the same plant under two policies, plus the recorded plant."""

    production: LegRun
    recorded_policy: LegRun
    recorded_plant: LegRun | None = None

    def legs(self) -> tuple[LegRun, ...]:
        parts = [self.production, self.recorded_policy]
        if self.recorded_plant is not None:
            parts.append(self.recorded_plant)
        return tuple(parts)


def _cap(findings: Sequence[Finding]) -> tuple[tuple[Finding, ...], int]:
    """Keep :data:`FINDING_LIMIT` findings of each kind and count what was dropped."""
    kept: list[Finding] = []
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
        if counts[finding.kind] <= FINDING_LIMIT:
            kept.append(finding)
    return tuple(kept), max(0, len(findings) - len(kept))


# ------------------------------------------------------------------------------------------- gates


def find_isolation(arm: Any, legs: Sequence[LegRun]) -> Isolation | None:
    """The E4 posture cross-check from a manifest arm, or ``None`` when the arm does not declare it.

    The production leg and the recorded-policy leg run on the *same* simulated plant, so their
    divergence is policy-versus-policy through a plant model; the recorded-plant leg (the third one)
    is the only leg whose flows can be compared against the recording itself.
    """
    specs = getattr(arm, "legs", ())
    if not any(getattr(spec, "isolate", False) for spec in specs) or len(specs) != len(legs):
        return None
    production = recorded_policy = recorded_plant = None
    for spec, leg in zip(specs, legs):
        if getattr(spec, "policy", "") == "production" and getattr(spec, "plant", "") == "simulated":
            production = production or leg
        elif getattr(spec, "policy", "") == "recorded" and getattr(spec, "plant", "") == "simulated":
            recorded_policy = recorded_policy or leg
        elif getattr(spec, "policy", "") == "recorded" and getattr(spec, "plant", "") == "recorded":
            recorded_plant = recorded_plant or leg
    if production is None or recorded_policy is None:
        return None
    return Isolation(
        production=production, recorded_policy=recorded_policy, recorded_plant=recorded_plant
    )


def gate_plant_fidelity(
    legs: Sequence[LegRun], isolation: Isolation | None, *, tolerance_kwh: float
) -> GateVerdict:
    """Gate 1: the plant against the recording, and the plant *model*'s own share of it.

    Two terms, each named and kept apart.  The reference term compares the recorded-plant leg's own
    flows against the recording's rows: the recorded plant answers with the recording's measurements,
    so this is the check that the replay really is on the recording's plant and not a lookalike.  The
    model term compares the substitute recorded-policy leg against that recorded plant -- same
    setpoints, same window, obeying the model instead of the recording -- which is how the plant
    model's error is attributed *inside* gate 1 instead of hiding in an arm's delta.

    Only an arm that drives a recorded plant can be judged by the reference term, so an arm without
    one (E2, E3, E4) reports the term it could measure, or says it could not measure any.
    """
    findings: list[Finding] = []
    detail: dict[str, Any] = {"limits": list(PLANT_FIDELITY_LIMITS), "tolerance_kwh": float(tolerance_kwh)}
    measured = False
    failed = False
    #: Terms that could not cover the whole window: the gate is not measurable, never a silent pass.
    incomplete: list[str] = []

    fidelity = next(
        (
            leg
            for leg in legs
            if leg.result.plant == "recorded" and leg.result.policy == "production"
        ),
        None,
    ) or next((leg for leg in legs if leg.result.plant == "recorded"), None)
    if fidelity is not None:
        unknown = [
            window for window in fidelity.account.windows if window.divergence_kwh is None
        ]
        rows = [window for window in fidelity.account.windows if window.divergence_kwh is not None]
        for window in unknown:
            findings.append(
                Finding(
                    "reference_divergence_unknown",
                    window.start,
                    window.pv_load_source,
                    None,
                    None,
                )
            )
        if unknown:
            # Gate 1 is defined against every window: a window with no measurable reference means
            # the reference term did not run, so the gate reports that instead of passing on the
            # windows that happened to be measurable.
            incomplete.append(REFERENCE_DIVERGENCE_UNKNOWN)
            detail.update(
                {
                    "reference_leg": fidelity.result.name,
                    "reference_divergence_kwh": None,
                    "reference_windows": len(rows),
                    "reference_windows_unknown": len(unknown),
                }
            )
        if rows:
            measured = True
            total = sum(abs(window.divergence_kwh or 0.0) for window in rows)
            finding = _round(total, 6) or 0.0
            detail.update(
                {
                    "reference_leg": fidelity.result.name,
                    "reference_divergence_kwh": finding,
                    "reference_windows": len(rows),
                }
            )
            for window in rows:
                divergence = abs(window.divergence_kwh or 0.0)
                if divergence > tolerance_kwh:
                    findings.append(
                        Finding(
                            "reference_divergence_kwh",
                            window.start,
                            window.pv_load_source,
                            _round(divergence, 6),
                            float(tolerance_kwh),
                        )
                    )
            failed |= total > tolerance_kwh

    if isolation is not None and isolation.recorded_plant is not None:
        modelled, observed = isolation.recorded_policy.account, isolation.recorded_plant.account
        total = 0.0
        compared = 0
        skipped = 0
        for left, right in zip(modelled.windows, observed.windows):
            if left.divergence_kwh is None or right.divergence_kwh is None:
                skipped += 1
                findings.append(
                    Finding(
                        "plant_model_unknown",
                        left.start,
                        "reconstructed_minus_recorded_grid",
                        _round(left.divergence_kwh, 6),
                        _round(right.divergence_kwh, 6),
                    )
                )
                continue
            compared += 1
            total += abs(left.divergence_kwh - right.divergence_kwh)
            findings.append(
                Finding(
                    kind="plant_model",
                    bucket=left.start,
                    label="reconstructed_minus_recorded_grid",
                    replayed=_round(left.divergence_kwh, 6),
                    recorded=_round(right.divergence_kwh, 6),
                )
            )
        if skipped:
            incomplete.append(PLANT_MODEL_UNKNOWN)
        if compared:
            measured = True
            detail.update(
                {"plant_model_error_kwh": _round(total, 6), "plant_model_windows": compared}
            )
            failed |= total > tolerance_kwh

    if not measured:
        detail["reference"] = _reference_attribution(legs)
        kept, dropped = _cap(findings)
        detail["findings_dropped"] = dropped
        # The reference term may have *run* and covered no window: naming that as a missing leg
        # would hide the reason a reviewer needs, so the terms that could not cover the window are
        # reported and ``no_recorded_leg`` is kept for the case where no reference leg exists.
        return GateVerdict(
            gate=PLANT_FIDELITY,
            passed=None,
            detail=detail,
            findings=kept,
            reasons=tuple(dict.fromkeys(incomplete or (NO_RECORDED_LEG,))),
        )
    kept, dropped = _cap(findings)
    detail["findings_dropped"] = dropped
    if incomplete:
        detail["windows_not_covered"] = len(incomplete)
        return GateVerdict(
            gate=PLANT_FIDELITY,
            passed=None,
            detail=detail,
            findings=kept,
            reasons=tuple(dict.fromkeys(incomplete)),
        )
    return GateVerdict(
        gate=PLANT_FIDELITY,
        passed=not failed,
        detail=detail,
        findings=kept,
        reasons=() if not failed else (GATE_FAILED,),
    )


def _grid_tolerance_s(slot_minutes: float) -> float:
    return float(slot_minutes) * 60.0


def _slot_minutes(leg: LegRunnerLike) -> float:
    """The planner grid this window ran on: the episode's own setting, never a fixture's."""
    settings = getattr(getattr(leg, "episode", None), "settings", None)
    value = None
    if settings is not None:
        value = settings.get("optimizer.slot_minutes", None)
    if value is None:
        value = _DEFAULT_SLOT_MINUTES
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return _DEFAULT_SLOT_MINUTES


def _replayed_plans(leg: LegRunnerLike) -> list[dict[str, Any]]:
    """The replay's own plan rows, newest first, read from the leg's scratch copy.

    Extraction cut ``plans``/``plan_slots`` at the window start, so every row left in the leg's copy
    is the replay's own; reading them is the only way to see a plan the engine wrote, which is why
    the recorded plan comes from the episode instead (its rows are the recording).
    """
    connection = leg.store.connection
    cursor = connection.execute(
        "SELECT id, created_at, summary_json FROM plans WHERE created_at >= ? ORDER BY created_at",
        (to_iso(leg.start),),
    )
    rows = []
    for plan_id, created_at, summary_json in cursor.fetchall():
        rows.append(
            {
                "id": plan_id,
                "created_at": from_iso(created_at),
                "summary": _json_object(summary_json),
            }
        )
    return rows


def _replayed_plan_slots(leg: LegRunnerLike) -> list[tuple[datetime, dict[datetime, dict[str, Any]]]]:
    """The replay's own plans in force over time: ``(created_at, {slot t: slot})``, chronological.

    The warm-start plan is the recording's own last plan (the replay seeds it rather than writing
    it), so it belongs here: it is the plan the replay actually had in force until its first own
    replan.  Everything else is read from the leg's scratch copy -- extraction cut ``plans`` at the
    window start, so those rows are the replay's own -- and a plan's slots are keyed by their exact
    ``t``, because a slot's identity on the planner grid is its timestamp.
    """
    warm = getattr(leg.episode, "warm_start", None)
    entries: list[tuple[datetime, dict[datetime, dict[str, Any]]]] = []
    if warm is not None and getattr(warm, "plan", None) is not None:
        plan = warm.plan
        entries.append(
            (
                plan.generated_at,
                {
                    slot.t: {
                        "power_w": getattr(slot, "power_w", None),
                        "action": _action_name(getattr(slot, "action", None)),
                        "reason": _reason_text(getattr(slot, "reason", None)),
                        "buy": getattr(slot, "buy", None),
                        "sell": getattr(slot, "sell", None),
                    }
                    for slot in getattr(plan, "slots", ())
                },
            )
        )
    connection = leg.store.connection
    cursor = connection.execute(
        "SELECT p.created_at, s.t, s.power_w, s.action, s.reason, s.buy, s.sell FROM plan_slots s "
        "JOIN plans p ON p.id = s.plan_id WHERE p.created_at >= ? ORDER BY p.created_at, s.t",
        (to_iso(leg.start),),
    )
    slots: dict[datetime, dict[str, Any]] = {}
    created_at: datetime | None = None
    for created, t, power_w, action, reason, buy, sell in cursor.fetchall():
        moment = from_iso(created)
        if created_at is not None and moment != created_at:
            entries.append((created_at, slots))
            slots = {}
        created_at = moment
        slots[from_iso(t)] = {
            "power_w": power_w,
            "action": _action_name(action),
            "reason": _reason_text(reason),
            "buy": buy,
            "sell": sell,
        }
    if created_at is not None:
        entries.append((created_at, slots))
    return sorted(entries, key=lambda entry: entry[0])


def _slot_in_force(
    plans: Sequence[tuple[datetime, Mapping[datetime, Mapping[str, Any]]]], instant: datetime
) -> Mapping[str, Any] | None:
    """The slot a plan in force at ``instant`` carries there: the newest plan issued at or before it.

    An absent plan is not a disagreement (a leg may not have replanned yet), and neither is a plan
    without that exact slot: the comparison only judges slots both sides actually decided.
    """
    issued = [created for created, _slots in plans if created <= instant]
    if not issued:
        return None
    wanted = max(issued)
    for created, slots in plans:
        if created == wanted:
            return slots.get(instant)
    return None


def _action_name(value: Any) -> str:
    """One action spelled the same way from either side: an enum, its value, or a stored string."""
    text = "" if value is None else str(getattr(value, "value", value))
    return text.strip().lower().rsplit(".", 1)[-1]


def _reason_text(value: Any) -> str | None:
    """A slot's reason as a plain string (attribution only, so it is never fatal)."""
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text.rsplit(".", 1)[-1]


def _replayed_dispatch(leg: LegRunnerLike) -> dict[tuple[str, datetime], dict[str, Any]]:
    """The replay's own dispatch rows by ``(battery, bucket)``: the last write in each bucket wins."""
    connection = leg.store.connection
    cursor = connection.execute(
        "SELECT ts, battery_id, requested_w, applied_w, code, result, dry_run FROM dispatch_log "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts, id",
        (to_iso(leg.start), to_iso(leg.end)),
    )
    rows: dict[tuple[str, datetime], dict[str, Any]] = {}
    for ts, battery_id, requested_w, applied_w, code, result, dry_run in cursor.fetchall():
        if dry_run:
            continue
        rows[(str(battery_id), from_iso(ts))] = {
            "requested_w": requested_w,
            "applied_w": applied_w,
            "code": code,
            "result": result,
            "ts": from_iso(ts),
        }
    return rows


def _recorded_dispatch(leg: LegRunnerLike) -> dict[tuple[str, datetime], dict[str, Any]]:
    rows: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in leg.episode.dispatch:
        if bool(row.get("dry_run")):
            continue
        moment = from_iso(row.get("ts"))
        if moment is None or moment < leg.start or moment > leg.end:
            continue
        rows[(str(row.get("battery_id")), moment)] = {
            "requested_w": row.get("requested_w"),
            "applied_w": row.get("applied_w"),
            "code": row.get("code"),
            "result": row.get("result"),
        }
    return rows


def _recorded_plan_slots(leg: LegRunnerLike) -> list[tuple[datetime, dict[datetime, dict[str, Any]]]]:
    """The recording's own plans in force over time, from the episode (never from the scratch copy)."""
    entries = []
    for plan in leg.episode.plans:
        slots = {}
        for slot in getattr(plan, "slots", ()):
            slots[slot.t] = {
                "power_w": getattr(slot, "power_w", None),
                "action": _action_name(getattr(slot, "action", None)),
                "reason": _reason_text(getattr(slot, "reason", None)),
            }
        entries.append((plan.generated_at, slots))
    return sorted(entries, key=lambda entry: entry[0])


def gate_decision_fidelity(leg: LegRun, *, tolerances: Tolerances) -> GateVerdict:
    """Gate 2: the replay's own decisions against the recording's, within one grid step.

    Setpoints and plan slots are the fidelity claim, so a mismatch there fails the gate; a dispatch
    code or a plan slot's ``reason`` is attribution (why a decision was taken), reported but never
    fatal.  Only slots the recording actually carries are compared -- an absent recorded slot is not
    a disagreement, and the replay is never asked to match a decision the recording never made.

    One behavioural divergence is attributed here explicitly rather than left inside the setpoint
    comparison: a cost-basis ledger that stalled during the replay while the recording's own ledger
    stayed live.  A stalled ledger deactivates the sell floor, so the engine stops exporting for a
    reason that has nothing to do with the policy under test, and the measurement rows that feed it
    are reported per role for the same reason.
    """
    minutes = _slot_minutes(leg.result)
    step_s = _grid_tolerance_s(minutes)
    if step_s <= 0:
        return GateVerdict(
            gate=DECISION_FIDELITY, passed=None, reasons=(REPORT_INVALID,), detail={"slot_minutes": minutes}
        )
    replayed_dispatch = _replayed_dispatch(leg.result)
    recorded_dispatch = _recorded_dispatch(leg.result)
    replayed_plans = _replayed_plan_slots(leg.result)
    recorded_plans = _recorded_plan_slots(leg.result)
    # A store write the engine lost is not a decision disagreement: it is missing evidence, and the
    # plans this gate judges are exactly the rows that went missing.
    store_failures = {
        str(name): int(count)
        for name, count in dict(getattr(leg.result, "store_failures", {}) or {}).items()
        if count
    }

    findings: list[Finding] = []
    fatal = 0
    compared = 0
    stall = _ledger_stall(leg)
    for entry in stall["packs"]:
        if not entry["replayed_stalled"]:
            continue
        fatal += 1
        findings.append(
            Finding(
                FLOOR_STALL,
                leg.result.start,
                entry["battery_id"],
                entry["replayed_note"],
                entry["recorded_note"],
            )
        )
    packs = sorted({key[0] for key in (*recorded_dispatch, *replayed_dispatch)})
    for boundary in leg.result.windows:
        bucket = quarter_floor(boundary.start, minutes)
        while bucket < boundary.end:
            compared += 1
            left = _slot_in_force(replayed_plans, bucket)
            right = _slot_in_force(recorded_plans, bucket)
            if left is not None and right is not None:
                fatal += _compare(
                    findings,
                    "slot_power_w",
                    bucket,
                    left.get("power_w"),
                    right.get("power_w"),
                    POWER_EPSILON_W,
                )[0]
                if str(left.get("action")) != str(right.get("action")):
                    fatal += 1
                    findings.append(
                        Finding("slot_action", bucket, "decision", left.get("action"), right.get("action"))
                    )
                if left.get("reason") != right.get("reason"):
                    findings.append(
                        Finding("slot_reason", bucket, "attribution", left.get("reason"), right.get("reason"))
                    )
            for pack in packs:
                arm_row = replayed_dispatch.get((pack, bucket))
                ref_row = recorded_dispatch.get((pack, bucket))
                if arm_row is None or ref_row is None:
                    continue
                for label, key in (("requested_w", "requested_w"), ("applied_w", "applied_w")):
                    fatal += _compare(
                        findings, f"dispatch_{label}", bucket, arm_row.get(key), ref_row.get(key), POWER_EPSILON_W, pack=pack
                    )[0]
                if str(arm_row.get("code")) != str(ref_row.get("code")):
                    findings.append(
                        Finding("dispatch_code", bucket, f"{pack} attribution", arm_row.get("code"), ref_row.get("code"))
                    )
            bucket += timedelta(minutes=minutes)
    if compared == 0:
        return GateVerdict(
            gate=DECISION_FIDELITY,
            passed=None,
            reasons=(NOT_COMPARABLE,),
            detail={"slot_minutes": minutes, "grid_step_s": step_s},
        )
    kept, dropped = _cap(findings)
    detail = {
        "slot_minutes": minutes,
        "grid_step_s": step_s,
        "power_tolerance_w": POWER_EPSILON_W,
        "buckets_compared": compared,
        "fatal_findings": fatal,
        "findings_dropped": dropped,
        "store_failures": store_failures,
        "measurements": _measurement_rows(leg),
        "ledger_stall": stall,
    }
    fatal += sum(store_failures.values())
    reasons = tuple(
        slug
        for slug, hit in (
            ("store_write_failed", bool(store_failures)),
            (FLOOR_STALL, any(entry["replayed_stalled"] for entry in stall["packs"])),
            (GATE_FAILED, fatal > 0),
        )
        if hit
    )
    return GateVerdict(
        gate=DECISION_FIDELITY,
        passed=fatal == 0,
        detail=detail,
        findings=kept,
        reasons=reasons,
    )


def _profile_payload(store: Any, kind: str, key: str) -> Mapping[str, Any] | None:
    """One profile row's payload from a store, however that store returns it.

    ``Store.profile_get`` answers ``(updated_at, payload)``; a stub or a JSON string answers
    something else.  A reader that only ever wants the payload should not have to care which, and a
    missing row is ``None`` -- never a crash in a gate.
    """
    try:
        row = store.profile_get(kind, str(key))
    except Exception:  # a missing table is a missing row, not a crash: the gate reports it
        return None
    if row is None:
        return None
    if isinstance(row, tuple) and len(row) == 2:
        row = row[1]
    if isinstance(row, str):
        try:
            row = json.loads(row)
        except (TypeError, ValueError):
            return None
    return row if isinstance(row, Mapping) else None


def _ledger_payload(leg: LegResult, battery_id: str) -> Mapping[str, Any] | None:
    """The ledger row the replay's own scratch copy holds for a pack, after its last update."""
    return _profile_payload(leg.store, COST_BASIS_KIND, battery_id)


def _recorded_ledger_payload(leg: LegResult, battery_id: str) -> Mapping[str, Any] | None:
    """The recording's own ledger row for a pack -- a comparand, read from the source recording."""
    return leg.episode.source_profile(COST_BASIS_KIND, battery_id)


def _ledger_stall(leg: LegRun) -> dict[str, Any]:
    """Whether the replay's ledger stalled while the recording's stayed live, per pack.

    The ledger records the fact in its own payload -- ``no_samples_since`` is the marker its own
    status API reads (``cost_basis.basis_status['stale']``) -- so this reads two rows rather than
    inferring a stall from behaviour: the replay's row in the scratch copy and the recording's own
    row as the comparand.  A pack stalled on both sides is the recording's own posture -- reported,
    not counted as a divergence; a pack live in the recording and stalled in the replay is a
    divergence, because a stalled ledger deactivates the sell floor.
    """
    packs: list[dict[str, Any]] = []
    for battery_id, _basis, _energy, _stamp in leg.account.booked_basis:
        replayed = _ledger_payload(leg.result, battery_id)
        recorded = _recorded_ledger_payload(leg.result, battery_id)
        packs.append(
            {
                "battery_id": str(battery_id),
                "replayed_stalled": _ledger_stalled(replayed),
                "recorded_stalled": _ledger_stalled(recorded),
                "replayed_note": _stall_note(replayed),
                "recorded_note": _stall_note(recorded),
            }
        )
    diverged = [
        entry
        for entry in packs
        if entry["replayed_stalled"]
        and not entry["recorded_stalled"]
        and entry["recorded_stalled"] is not None
    ]
    return {"comparand": SOURCE_BASIS_COMPARAND, "packs": packs, "diverged": [e["battery_id"] for e in diverged]}


def _ledger_stalled(payload: Mapping[str, Any] | None) -> bool | None:
    """Whether a ledger payload says the ledger stalled: the marker its own status API reports."""
    if payload is None:
        return None
    return bool(payload.get("no_samples_since"))


def _stall_note(payload: Mapping[str, Any] | None) -> str | None:
    """The stall's own explanation: since when, and how many updates it has skipped."""
    if not payload or not payload.get("no_samples_since"):
        return None
    skipped = payload.get("skipped_no_samples")
    note = f"no usable sample since {payload.get('no_samples_since')}"
    return f"{note} ({int(skipped)} update(s) skipped)" if isinstance(skipped, int) else note


def _measurement_rows(leg: LegRun) -> dict[str, Any]:
    """What this leg persisted into the ledger's own roles, per role (gate 2's evidence)."""
    result = leg.result
    failures = tuple(getattr(result, "measurement_failures", ()) or ())
    return {
        "rows_per_role": {str(role): int(count) for role, count in dict(result.measurement_rows).items()},
        "buckets": len(result.measurement_buckets),
        "sampling_s": 300,
        "failures": [
            {"instant": to_iso(refusal.instant), "reason": str(refusal.reason), "detail": str(refusal.detail)}
            for refusal in failures
        ],
        "failures_total": len(failures),
    }


def _compare(
    findings: list[Finding],
    kind: str,
    bucket: datetime,
    left: Any,
    right: Any,
    tolerance: float,
    *,
    pack: str | None = None,
) -> tuple[int, bool]:
    """Record a scalar disagreement; return ``(1, True)`` when it is fatal, ``(0, False)`` otherwise."""
    difference = _difference(left, right)
    if difference is None:
        return 0, False
    fatal = difference > tolerance
    if fatal:
        findings.append(
            Finding(kind, bucket, pack or "decision", _round(left, 3), _round(right, 3))
        )
    return (1 if fatal else 0), fatal


def _difference(left: Any, right: Any) -> float | None:
    left_value = _finite(left)
    right_value = _finite(right)
    if left_value is None or right_value is None:
        return None
    return abs(left_value - right_value)


def gate_accounting_fidelity(leg: LegRun, *, tolerances: Tolerances) -> GateVerdict:
    """Gate 3: the arm's flows against the recording, and the basis the engine actually booked.

    The energy term is the arm's own integral against the recording's rows per decision window.  The
    basis term compares the basis the replay booked for each pack against the recording's **own**
    ``battery_cost_basis`` row -- a comparand read from the source recording, which is allowed to sit
    after the window end because no leg's decision can see it.  That is the measurement of the
    approximation the harness accepts when it drops that row from the truncated copy: if the ledger's
    rebuild over its lookback diverges from what the recording held, it shows up here, per pack and
    inside the manifest's declared tolerance, instead of silently becoming a valuation nobody checked.
    """
    findings: list[Finding] = []
    worst = 0.0
    measured = 0
    for window in leg.account.windows:
        if window.divergence_kwh is None:
            continue
        measured += 1
        divergence = abs(window.divergence_kwh)
        worst = max(worst, divergence)
        if divergence > tolerances.energy_kwh:
            findings.append(
                Finding(
                    "energy_divergence_kwh",
                    window.start,
                    window.pv_load_source,
                    _round(divergence, 6),
                    float(tolerances.energy_kwh),
                )
            )
    basis_checked = 0
    worst_basis = 0.0
    comparands: list[tuple[str, float | None, float]] = []
    uncompared: list[str] = []
    for battery_id, _basis, _energy, _stamp in leg.account.booked_basis:
        payload = _recorded_ledger_payload(leg.result, battery_id)
        expected = _finite((payload or {}).get("basis_eur_per_kwh"))
        if expected is None:
            # No recorded row to compare against: not a divergence, but not a comparison either.
            uncompared.append(str(battery_id))
            continue
        comparands.append((str(battery_id), _live_basis(leg.result, battery_id), expected))
    for battery_id, actual, expected in comparands:
        basis_checked += 1
        if actual is None:
            findings.append(
                Finding("basis_missing", leg.result.start, battery_id, None, _round(expected, 6))
            )
            continue
        difference = abs(float(actual) - float(expected))
        worst_basis = max(worst_basis, difference)
        if difference > tolerances.basis_eur_per_kwh:
            findings.append(
                Finding("basis_divergence", leg.result.start, battery_id, _round(actual, 6), _round(expected, 6))
            )
    energy_failed = any(finding.kind == "energy_divergence_kwh" for finding in findings)
    basis_failed = any(finding.kind in ("basis_divergence", "basis_missing") for finding in findings)
    # The recording's booked feed-in price against the contract model this run assumes for it: the
    # settlement prices ARE the recording's columns, so a window whose columns the model cannot
    # reproduce is withheld and named here rather than quietly settled on an assumed tariff.
    tariff = getattr(leg.account, "tariff_check", None)
    # An unavailable model is not a clean check: nothing about the recording's booked feed-in price
    # was verified, so the gate fails (and the accounting withheld the windows) instead of passing
    # on an assumption.  A declared-by-construction leg is not this reference leg, and an unchecked
    # one (no tolerance declared) is named instead of passed.
    tariff_failed = bool(
        tariff is not None
        and (tariff.diverged_windows or tariff.model == TARIFF_MODEL_UNAVAILABLE)
    )
    if tariff is not None and tariff.model == TARIFF_MODEL_UNAVAILABLE:
        findings.append(
            Finding(
                EXPORT_REVENUE_UNVERIFIED,
                leg.result.start,
                str(tariff.reason or tariff.comparand),
                None,
                None,
            )
        )
    if tariff is not None and tariff.diverged_windows:
        findings.append(
            Finding(
                EXPORT_REVENUE_UNVERIFIED,
                tariff.worst_instant or leg.result.start,
                str(tariff.worst_window if tariff.worst_window is not None else ""),
                _round(tariff.worst_recorded_eur_per_kwh, 6),
                _round(tariff.worst_model_eur_per_kwh, 6),
            )
        )
    if measured == 0 and basis_checked == 0:
        return GateVerdict(
            gate=ACCOUNTING_FIDELITY,
            passed=None,
            reasons=(NOT_COMPARABLE,),
            detail={"energy_tolerance_kwh": tolerances.energy_kwh, "basis_tolerance_eur_per_kwh": tolerances.basis_eur_per_kwh},
        )
    kept, dropped = _cap(findings)
    detail = {
        "energy_tolerance_kwh": float(tolerances.energy_kwh),
        "basis_tolerance_eur_per_kwh": float(tolerances.basis_eur_per_kwh),
        "basis_comparand": SOURCE_BASIS_COMPARAND,
        "windows_compared": measured,
        "packs_compared": basis_checked,
        "packs_without_recorded_basis": uncompared,
        "worst_window_kwh": _round(worst, 6),
        "worst_basis_eur_per_kwh": _round(worst_basis, 6),
        "tariff_tolerance_eur_per_kwh": None
        if tariff is None
        else _round(tariff.tolerance_eur_per_kwh, 6),
        "tariff_comparand": None if tariff is None else tariff.comparand,
        "tariff_reason": None if tariff is None or not tariff.reason else tariff.reason,
        "tariff_model": None if tariff is None else tariff.model,
        "tariff_segments_compared": 0 if tariff is None else int(tariff.compared_segments),
        "tariff_fallback_segments": 0 if tariff is None else int(tariff.fallback_segments),
        "tariff_unchecked": False if tariff is None else bool(tariff.unchecked),
        "worst_tariff_eur_per_kwh": None if tariff is None else _round(tariff.worst_eur_per_kwh, 6),
        "tariff_windows_withheld": [] if tariff is None else list(tariff.diverged_windows),
        "tariff_note": None if tariff is None else tariff.note,
        "findings_dropped": dropped,
    }
    return GateVerdict(
        gate=ACCOUNTING_FIDELITY,
        passed=not (energy_failed or basis_failed or tariff_failed),
        detail=detail,
        findings=kept,
        reasons=()
        if not (energy_failed or basis_failed or tariff_failed)
        else tuple(
            slug
            for slug, hit in (
                ("energy_divergence", energy_failed),
                ("basis_divergence", basis_failed),
                (EXPORT_REVENUE_UNVERIFIED, tariff_failed),
            )
            if hit
        ),
    )


def _live_basis(leg: LegResult, battery_id: str) -> float | None:
    """The booked basis for a pack as the replay's own scratch copy now holds it."""
    payload = _ledger_payload(leg, battery_id)
    return None if payload is None else _finite(payload.get("basis_eur_per_kwh"))


def gates_for(
    arm: Any,
    legs: Sequence[LegRun],
    *,
    tolerances: Tolerances,
    isolation: Isolation | None,
) -> tuple[GateVerdict, ...]:
    """Gate 1 always; gates 2 and 3 on the recorded production leg when the arm declares one."""
    verdicts = [gate_plant_fidelity(legs, isolation, tolerance_kwh=tolerances.energy_kwh)]
    fidelity = next(
        (
            leg
            for leg in legs
            if leg.result.plant == "recorded" and leg.result.policy == "production"
        ),
        None,
    )
    if fidelity is None:
        for gate in (DECISION_FIDELITY, ACCOUNTING_FIDELITY):
            verdicts.append(
                GateVerdict(
                    gate=gate,
                    passed=None,
                    reasons=(NO_RECORDED_LEG,),
                    detail={
                        "note": "this arm has no recorded, production-policy leg to judge",
                        "reference": _reference_attribution(legs),
                    },
                )
            )
        return tuple(verdicts)
    verdicts.append(_with_reference_leg(gate_decision_fidelity(fidelity, tolerances=tolerances), fidelity))
    verdicts.append(_with_reference_leg(gate_accounting_fidelity(fidelity, tolerances=tolerances), fidelity))
    return tuple(verdicts)


def _with_reference_leg(verdict: GateVerdict, leg: LegRun) -> GateVerdict:
    """Name the leg a gate judged: an arm's pair and its reference leg are not the same object.

    An arm may carry a pair *and* a recorded-plant anchor (E2 does), so which leg gates 2 and 3
    describe is part of their meaning rather than something to infer from the legs list.
    """
    return replace(
        verdict,
        detail={**dict(verdict.detail), "reference_leg": str(leg.result.name)},
    )


def _reference_attribution(legs: Sequence[LegRun]) -> dict[str, Any]:
    """What gates 2 and 3 compare against, and why this run could not: named, not left to guess.

    An arm that declares no recorded-plant, production-policy leg (E3/E4 pair other things) has no
    E1 comparand, which is correct but reads as a bare ``no_recorded_leg``.  So the verdict names the
    legs the run did declare plus the recorded inputs the episode holds -- the two things a reader
    needs to tell 'this arm cannot be judged for fidelity' from 'the recording had nothing to judge'.
    """
    declared: list[dict[str, Any]] = []
    episode = None
    for leg in legs:
        result = leg.result
        declared.append(
            {
                "leg": str(result.name),
                "plant": str(result.plant),
                "policy": str(result.policy),
                "recorded_plant": bool(result.plant == "recorded"),
            }
        )
        episode = episode or getattr(result, "episode", None)
    plans = list(getattr(episode, "plans", ()) or ()) if episode is not None else []
    dispatch = list(getattr(episode, "dispatch", ()) or ()) if episode is not None else []
    return {
        "declared_legs": declared,
        "recorded_plans_in_window": len(plans),
        "recorded_plan_slots_in_window": sum(
            len(list(getattr(plan, "slots", ()) or ())) for plan in plans
        ),
        "recorded_dispatch_rows_in_window": len(dispatch),
    }


# ----------------------------------------------------------------------------------- arm deltas


def pair_arm(arm: Any, legs: Sequence[LegRun]) -> PairedDelta | None:
    """The arm's paired delta, oriented so a positive number means the declared side wins.

    E2 pairs the declared contract model against the recorded one; E4 pairs the production policy
    against the recorded policy; E1 (fidelity) and E3 (a within-leg terminal pair) have no paired
    delta of their own and say so instead of inventing one.
    """
    from .accounting import paired_delta

    selection = pair_legs(arm, legs)
    if selection is None:
        return None
    left, right = selection
    return paired_delta(left.account, right.account, change=str(getattr(arm, "changes", "")))


def pair_legs(arm: Any, legs: Sequence[LegRun]) -> tuple[LegRun, LegRun] | None:
    """Which two legs an arm pairs, by name and shape -- never the fidelity reference leg.

    An arm may carry extra legs (E2's and E4's recorded-plant reference leg, E4's posture
    cross-check); the pair is the declared same-plant comparison, so its members are selected here
    once and both the delta and the report's ``paired.legs`` read it from this function.
    """
    name = str(getattr(arm, "name", "")).upper()
    if name == "E2" and len(legs) >= 2:
        return legs[0], legs[1]
    if name == "E4" and len(legs) >= 2:
        return legs[1], legs[0]
    return None


def terminal_pair(leg: LegRun) -> dict[str, Any] | None:
    """E3's degraded comparison: the same leg's profit with and without the terminal term."""
    account = leg.account
    if account.profit_with_terminal_eur is None:
        return None
    return {
        "valuation": account.valuation_eur_per_kwh,
        "valuation_source": account.valuation_source,
        "valuation_packs": {
            "frozen": list(account.valuation_frozen),
            "derived": list(account.valuation_derived),
            "missing": list(account.valuation_missing),
        },
        "terminal_adjustment_eur": account.terminal_adjustment_eur,
        "profit_without_terminal_eur": account.profit_eur,
        "profit_with_terminal_eur": account.profit_with_terminal_eur,
        "sensitivity_valuation_eur_per_kwh": account.sensitivity_valuation_eur_per_kwh,
        "sensitivity_terminal_adjustment_eur": account.sensitivity_terminal_adjustment_eur,
    }


# ---------------------------------------------------------------------------------------- sections


def _round(value: Any, digits: int) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _run_section(manifest: Manifest, runtime_s: float, steps: int, legs: int) -> dict[str, Any]:
    return {
        "manifest_digest": manifest.digest(),
        "manifest_version": manifest.version,
        "fingerprint_digest": manifest.fingerprint.get("digest"),
        "fingerprint_components": dict(manifest.fingerprint.get("components", {})),
        "fingerprint_sizes": dict(manifest.fingerprint.get("sizes", {})),
        "window": {
            "start": to_iso(manifest.start),
            "end": to_iso(manifest.end),
            "step_s": float(manifest.step_s),
            "steps": int(steps),
        },
        "seed": int(manifest.seed),
        "blocks": int(manifest.blocks),
        "resamples": int(manifest.resamples),
        "tolerances": {
            "grid_step_s": float(manifest.tolerances.grid_step_s),
            "energy_kwh": float(manifest.tolerances.energy_kwh),
            "basis_eur_per_kwh": float(manifest.tolerances.basis_eur_per_kwh),
            "price_eur_per_kwh": float(manifest.tolerances.price_eur_per_kwh),
        },
        "plant_limits": {
            "recorded": _ceiling_dict(manifest.plant_limits.recorded),
            "simulated": _ceiling_dict(manifest.plant_limits.simulated),
        },
        "valuation": dict(manifest.valuation),
        "runtime_s": round(float(runtime_s), 3),
        # Runtime and steps are per the whole arm, so the leg count says what they were spent on
        # (an arm with a reference leg drives one more Episode and one more Engine).
        "legs": int(legs),
    }


def _ceiling_dict(ceiling: PlantCeiling) -> dict[str, Any]:
    return {
        "energy_unknown_ticks": ceiling.energy_unknown_ticks,
        "infeasible_commands": ceiling.infeasible_commands,
        "unverified_bounds": ceiling.unverified_bounds,
    }


def _drive_section(arm: Any, legs: Sequence[LegRun]) -> dict[str, Any]:
    return {
        "arm": str(getattr(arm, "name", "")),
        "change": str(getattr(arm, "changes", "")),
        "contract": _contract_state(legs),
        "legs": [_leg_drive(leg) for leg in legs],
    }


def _contract_state(legs: Sequence[LegRun]) -> dict[str, Any] | None:
    """What the declared contract model was, and whether it actually priced export.

    A declared model that refuses to price export makes the arm measure the value of export access
    rather than a tariff error, so the verdict is stated next to the number it produced instead of
    leaving a reader to infer it from a journal code.  The evidence is the arm's own plan slots (the
    settled sell they carry) and the engine's own refusal codes.
    """
    declared: dict[str, Any] = {}
    for leg in legs:
        declared.update(dict(leg.result.patch))
    if not declared:
        return None
    slots = [
        slot
        for leg in legs
        for generated, plan_slots in _replayed_plan_slots(leg.result)
        # The warm-start plan is the recording's own last plan, so its prices are the recording's,
        # not the declared contract's; only the plans these legs issued belong in the evidence.
        if generated >= leg.result.start
        for slot in plan_slots.values()
    ]
    reasons: dict[str, int] = {}
    for leg in legs:
        for event in leg.result.events:
            code = str(event.get("code", ""))
            if code in FEED_IN_REFUSALS:
                reasons[code] = reasons.get(code, 0) + 1
    for slot in slots:
        reason = str(slot.get("reason") or "")
        if reason in FEED_IN_REFUSALS:
            reasons[reason] = reasons.get(reason, 0) + 1
    settled = [float(slot["sell"]) for slot in slots if slot.get("sell") is not None]
    refused = sum(reasons.values())
    verdict = contract_verdict(len(settled), refused)
    return {
        "declared": {str(key): value for key, value in sorted(declared.items())},
        "verdict": verdict,
        "clean_tariff_delta": verdict == CONTRACT_RESOLVED,
        "evidence": {
            "refusals_by_code": dict(sorted(reasons.items())),
            "plan_slots": len(slots),
            "priced_slots": len(settled),
            "settled_export_eur_per_kwh": {
                "min": _round(min(settled), 6) if settled else None,
                "max": _round(max(settled), 6) if settled else None,
            },
        },
        "note": _contract_note(verdict, len(settled), len(slots), refused),
    }


def contract_verdict(settled_slots: int, refusals: int) -> str:
    """The verdict derived from the evidence: what was priced, and what was refused.

    ``resolved`` requires the refusal evidence to be empty -- an arm that priced some slots and
    refused others measured a tariff change mixed with lost export access, and calling that a
    tariff error would overstate it.
    """
    if refusals and not settled_slots:
        return CONTRACT_REFUSES_EXPORT
    if refusals:
        return CONTRACT_PARTIAL_REFUSAL
    return CONTRACT_RESOLVED if settled_slots else CONTRACT_UNKNOWN


def _contract_note(verdict: str, priced: int, total: int, refused: int) -> str:
    """One line a reader can act on, and never a claim the evidence contradicts."""
    if verdict == CONTRACT_RESOLVED:
        return "the declared model priced export, so the arm compares two priced contracts"
    if verdict == CONTRACT_REFUSES_EXPORT:
        return (
            "the declared model refused to price export: this arm measures export access, not a "
            f"tariff error; {UNCLEAN_CONTRACT_NOTE}"
        )
    if verdict == CONTRACT_PARTIAL_REFUSAL:
        return (
            f"the declared model priced {priced} of {total} slots and refused {refused}: "
            f"{UNCLEAN_CONTRACT_NOTE}"
        )
    return f"neither a settled export price nor a refusal was recorded; {UNCLEAN_CONTRACT_NOTE}"


def _leg_drive(leg: LegRun) -> dict[str, Any]:
    result = leg.result
    return {
        "leg": result.name,
        "plant": result.plant,
        "policy": result.policy,
        "patch": dict(sorted(dict(result.patch).items())),
        "steps": int(result.steps),
        "cadence": {
            "control_steps": int(result.control_steps),
            "replan_steps": int(result.replan_steps),
            "guard_steps": int(result.guard_steps),
        },
        "tally": {
            "control_ticks": int(result.tally.ticks),
            "commands": int(result.tally.commands),
            "unavailable_ticks": int(result.tally.unavailable_ticks),
            "infeasible_commands": int(result.tally.infeasible_commands),
            "unverified_bounds": int(result.tally.unverified_bounds),
        },
        "energy_kwh": {
            "initial": _round(result.initial_energy_kwh(), 4),
            "final": _round(result.terminal_energy_kwh(), 4),
        },
        "facade": {str(key): value for key, value in dict(result.facade).items()},
        "store_failures": {str(name): int(count) for name, count in dict(result.store_failures).items()},
        "policy_notes": {str(key): value for key, value in dict(result.policy_notes).items()},
        "measurements": _measurement_rows(leg),
        "events": _event_summary(result.events),
        "steps_with_refusals": len(result.refusals),
    }


def _event_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for event in events:
        code = str(event.get("code", ""))
        counts[code] = counts.get(code, 0) + 1
    return {"count": len(events), "by_code": {code: counts[code] for code in sorted(counts)}}


def _plant_section(manifest: Manifest, legs: Sequence[LegRun]) -> dict[str, Any]:
    rows = []
    for leg in legs:
        ceiling = manifest.plant_limits.ceiling(leg.result.plant)
        tripped = sorted({reason for window in leg.account.windows for reason in window.ceiling_tripped})
        rows.append(
            {
                "leg": leg.result.name,
                "plant": leg.result.plant,
                "ceiling": _ceiling_dict(ceiling),
                "tripped": tripped,
                "windows": [
                    {
                        "index": window.index,
                        "start": to_iso(window.start),
                        "end": to_iso(window.end),
                        "available_ticks": window.plant.unavailable_ticks,
                        "energy_unknown_ticks": window.plant.unavailable_ticks,
                        "infeasible_commands": window.plant.infeasible_commands,
                        "unverified_bounds": window.plant.unverified_bounds,
                        "withheld": window.profit_eur is None,
                        "reasons": list(window.reasons),
                    }
                    for window in leg.account.windows
                ],
            }
        )
    return {"legs": rows}


def _information_section(legs: Sequence[LegRun]) -> dict[str, Any]:
    provenance: dict[str, Any] = {}
    vintages: dict[str, Any] = {}
    for leg in legs:
        result = leg.result
        provenance[result.name] = {
            "window": result.episode.base.window(result.start, result.end),
            "buckets": dict(result.episode.base.split(result.start, result.end)),
            "refused_buckets": [to_iso(bucket) for bucket in result.episode.base.refused],
        }
        kinds: dict[str, int] = {}
        sources: set[str] = set()
        uncovered = 0
        horizon: datetime | None = None
        for call in result.vintage_calls:
            kinds[call.kind] = kinds.get(call.kind, 0) + 1
            sources.update(str(source) for source in call.sources)
            uncovered += int(call.tail_uncovered_slots)
            horizon = call.horizon_end if horizon is None else max(horizon, call.horizon_end)
        vintages[result.name] = {
            "calls": len(result.vintage_calls),
            "kinds": {kind: kinds[kind] for kind in sorted(kinds)},
            "sources": sorted(sources),
            "tail_uncovered_slots": uncovered,
            "last_horizon_end": None if horizon is None else to_iso(horizon),
        }
    return {
        "pv_load_source": provenance,
        "pv_load_source_rule": (
            "one label per window: the provenance of the covering bucket at the window start -- the "
            "base every tick of the window used -- while 'buckets' counts the buckets whose own "
            "start falls inside the window"
        ),
        "vintages": vintages,
    }


def _accounting_section(
    arm: Any, legs: Sequence[LegRun], paired: PairedDelta | None, bootstrap: Any
) -> dict[str, Any]:
    name = str(getattr(arm, "name", "")).upper()
    withheld_by_gate = None
    section: dict[str, Any] = {
        "arm": name,
        "change": str(getattr(arm, "changes", "")),
        "evidence": EMPIRICAL if name in EMPIRICAL_ARMS else DECLARED,
        "evidence_labels": [],
        "legs": [_leg_account(leg) for leg in legs],
        "paired": None
        if paired is None
        else _paired_dict(paired, pair_legs(arm, legs)),
        "bootstrap": None if bootstrap is None else _bootstrap_dict(bootstrap),
        "terminal_pair": None if name != "E3" else terminal_pair(legs[0]),        "withheld_by_gate": withheld_by_gate,
        # The declared contract's own verdict travels with the delta it produced, so a number that
        # mixes a tariff effect with a refusal to price export cannot be read as a tariff error.
        "contract": _contract_state(legs),
        "notes": [],
    }
    _label_null_by_construction(section)
    _label_anchor_leg(section, legs)
    _label_interval_evidence(section)
    return section


def _label_null_by_construction(section: dict[str, Any]) -> None:
    """E4: label its zero delta (and its interval) as an identity instead of a measurement."""
    if str(section["change"]) != "policy" or section["paired"] is None:
        return
    section["notes"].append(
        {"code": RECORDED_POLICY_EQUALS_PRODUCTION, "text": RECORDED_POLICY_NOTE}
    )
    section["evidence_labels"] = [RECORDED_POLICY_EQUALS_PRODUCTION, SAA_NOT_SHIPPED]
    for key in ("paired", "bootstrap"):
        if section.get(key) is not None:
            section[key]["by_construction"] = True
            section[key]["null_reason"] = RECORDED_POLICY_EQUALS_PRODUCTION


def _label_anchor_leg(section: dict[str, Any], legs: Sequence[LegRun]) -> None:
    """Name the leg the gates actually judge when an arm carries a pair plus a reference leg."""
    if section["paired"] is None:
        return
    anchor = [
        leg for leg in legs if leg.result.plant == "recorded" and leg.result.policy == "production"
    ]
    if not anchor:
        return
    section["notes"].append(
        {
            "code": ANCHOR_LEG_IS_THE_FIDELITY_REFERENCE,
            "leg": anchor[0].result.name,
            "text": f"the reference leg {anchor[0].result.name!r} is the gate reference; {REFERENCE_LEG_NOTE}",
        }
    )


def _label_interval_evidence(section: dict[str, Any]) -> None:
    """An interval shorter than the plan horizon is illustrative: say so where the number is."""
    bootstrap = section.get("bootstrap")
    if not isinstance(bootstrap, dict) or bool(bootstrap.get("publishable", True)):
        return
    section["notes"].append(
        {
            "code": ILLUSTRATIVE_INTERVAL,
            "text": str(bootstrap.get("note") or EPISODE_EVIDENCE_REQUIREMENT),
        }
    )
    section["evidence_labels"] = [*section["evidence_labels"], ILLUSTRATIVE_INTERVAL]


def _leg_account(leg: LegRun) -> dict[str, Any]:
    account = leg.account
    return {
        "leg": account.name,
        "plant": account.plant,
        "policy": account.policy,
        "import_kwh": _round(account.import_kwh, 4),
        "export_kwh": _round(account.export_kwh, 4),
        "cost_eur": _round(account.cost_eur, 4),
        "revenue_eur": _round(account.revenue_eur, 4),
        "profit_eur": _round(account.profit_eur, 4),
        "reference_divergence_kwh": _round(account.reference_divergence_kwh, 6),
        "energy_delta_kwh": _round(account.energy_delta_kwh, 4),
        "valuation_eur_per_kwh": _round(account.valuation_eur_per_kwh, 6),
        "terminal_adjustment_eur": _round(account.terminal_adjustment_eur, 4),
        "profit_with_terminal_eur": _round(account.profit_with_terminal_eur, 4),
        "sensitivity_valuation_eur_per_kwh": _round(account.sensitivity_valuation_eur_per_kwh, 6),
        "sensitivity_terminal_adjustment_eur": _round(account.sensitivity_terminal_adjustment_eur, 4),
        "booked_basis": [
            {
                "battery_id": str(battery_id),
                "basis_eur_per_kwh": _round(basis, 6),
                "energy_kwh": _round(energy, 4),
            }
            for battery_id, basis, energy, _stamp in account.booked_basis
        ],
        # How many windows each class of total covers: a partial sum is stated, not inferred.
        "windows_measured": int(getattr(account, "windows_measured", 0)),
        "windows_priced": int(getattr(account, "windows_priced", 0)),
        "windows_total": len(account.windows),
        "withheld_windows": list(account.withheld_windows),
        "valuation": {
            "basis_eur_per_kwh": account.valuation_eur_per_kwh,
            "source": account.valuation_source,
            "packs": {
                "frozen": list(account.valuation_frozen),
                "derived": list(account.valuation_derived),
                "missing": list(account.valuation_missing),
            },
            "terminal_adjustment_eur": _round(account.terminal_adjustment_eur, 4),
            "profit_with_terminal_eur": _round(account.profit_with_terminal_eur, 4),
            "sensitivity_valuation_eur_per_kwh": _round(
                account.sensitivity_valuation_eur_per_kwh, 6
            ),
            "sensitivity_terminal_adjustment_eur": _round(
                account.sensitivity_terminal_adjustment_eur, 4
            ),
        },
        "notes": [{"code": str(code), "text": str(text)} for code, text in account.notes],
        # The recording's booked feed-in price against the assumed contract: the comparand is named
        # the way gate 3 names ``basis_comparand``, and an unjudged model says so in one line.
        "tariff": None
        if getattr(account, "tariff_check", None) is None
        else {
            "comparand": account.tariff_check.comparand,
            "model": account.tariff_check.model,
            # Why the check could not run at all (empty when it did): a distinct slug, so a
            # fallback-priced recording is not read as "the assumed model was unavailable".
            "reason": str(getattr(account.tariff_check, "reason", "")),
            "tolerance_eur_per_kwh": _round(account.tariff_check.tolerance_eur_per_kwh, 6),
            "segments_compared": int(account.tariff_check.compared_segments),
            "fallback_segments": int(getattr(account.tariff_check, "fallback_segments", 0)),
            "independent": (int(account.tariff_check.compared_segments) > 0)
            and not bool(getattr(account.tariff_check, "unchecked", False)),
            "unchecked": bool(getattr(account.tariff_check, "unchecked", False)),
            "worst_eur_per_kwh": _round(account.tariff_check.worst_eur_per_kwh, 6),
            "worst_window": account.tariff_check.worst_window,
            "worst_segment": None
            if account.tariff_check.worst_instant is None
            else to_iso(account.tariff_check.worst_instant),
            "worst_recorded_eur_per_kwh": _round(
                account.tariff_check.worst_recorded_eur_per_kwh, 6
            ),
            "worst_model_eur_per_kwh": _round(account.tariff_check.worst_model_eur_per_kwh, 6),
            "diverged_windows": list(account.tariff_check.diverged_windows),
            "note": account.tariff_check.note,
        },
        "windows": [
            {
                "index": window.index,
                "start": to_iso(window.start),
                "end": to_iso(window.end),
                "import_kwh": _round(window.import_kwh, 4),
                "export_kwh": _round(window.export_kwh, 4),
                "cost_eur": _round(window.cost_eur, 4),
                "revenue_eur": _round(window.revenue_eur, 4),
                "profit_eur": _round(window.profit_eur, 4),
                "reference_divergence_kwh": _round(window.divergence_kwh, 6),
                "pv_load_source": window.pv_load_source,
                "price_fallbacks": int(window.price_fallbacks),
                "tariff_divergence_segments": int(window.tariff_divergence_segments),
                "reasons": list(window.reasons),
                "plant": {
                    "unavailable_ticks": window.plant.unavailable_ticks,
                    "infeasible_commands": window.plant.infeasible_commands,
                    "unverified_bounds": window.plant.unverified_bounds,
                },
            }
            for window in account.windows
        ],
    }


def _paired_dict(paired: PairedDelta, pair: tuple[LegRun, LegRun] | None = None) -> dict[str, Any]:
    return {
        "change": paired.change,
        # Which two legs this delta is between, by name: an arm that also drives a reference leg
        # must not let it drift into the comparison.
        "legs": None if pair is None else [pair[0].result.name, pair[1].result.name],
        # ``None`` when any window was withheld or no window was included: the renderer prints it
        # as null, never as a 0.0 that could be mistaken for E4's genuine null-by-construction zero.
        "delta_eur": _round(paired.delta_eur, 4),
        "delta_withheld": paired.delta_eur is None,
        "delta_with_terminal_eur": _round(paired.delta_with_terminal_eur, 4),
        "delta_with_terminal_withheld": paired.delta_with_terminal_eur is None,
        "terminal_delta_eur": _round(paired.terminal_delta_eur, 4),
        "included_windows": int(paired.included_windows),
        "withheld_windows": list(paired.withheld_windows),
        # A withheld window in the middle of the pair splits the sample into runs: each such window
        # is named (statistics.gap_windows) so the bootstrap's contiguity verdict can be checked
        # against this section's own window list instead of taken on trust.
        "gap_windows": [
            int(index) for index in gap_windows(paired.windows)
        ],
        "windows_contiguous": not gap_windows(paired.windows),
        "windows": [
            {
                "index": window.index,
                "start": to_iso(window.start),
                "end": to_iso(window.end),
                "left_profit_eur": _round(window.left_profit_eur, 4),
                "right_profit_eur": _round(window.right_profit_eur, 4),
                "delta_eur": _round(window.delta_eur, 4),
                "withheld": bool(window.withheld),
                "reasons": list(window.reasons),
            }
            for window in paired.windows
        ],
    }


def _bootstrap_dict(result: Any) -> dict[str, Any]:
    """The interval plus the parameters that judge it: blocks, seed, and the plan-horizon check.

    A block length shorter than one plan horizon cannot capture the overlap the blocks stand for, so
    the result says whether it is ``publishable`` at all, how many windows an episode would need, and
    what that means -- a tight-looking interval on a 21-minute recording is labelled illustrative.
    """
    window_seconds = getattr(result, "window_seconds", None)
    horizon_hours = getattr(result, "horizon_hours", None)
    horizons = int(getattr(result, "evidence_horizons", 5) or 5)
    implied = getattr(result, "block_length_horizon_windows", None)
    if implied is None:
        implied = plan_horizon_windows(horizon_hours, window_seconds)
    needed = None if implied is None else int(implied) * horizons
    minimum_seconds = (
        None if needed is None or not window_seconds else float(needed) * float(window_seconds)
    )
    return {
        "resamples": int(result.resamples),
        "block_length": int(result.block_length),
        "seed": int(result.seed),
        "confidence": float(result.confidence),
        "samples": int(result.samples),
        "excluded_windows": int(result.excluded_windows),
        "windows_total": int(result.windows_total),
        "mean_eur": _round(result.mean_eur, 6),
        "total_eur": _round(result.total_eur, 4),
        "interval_eur": None if result.interval_eur is None else [_round(result.interval_eur[0], 6), _round(result.interval_eur[1], 6)],
        "standard_error_eur": _round(result.standard_error_eur, 6),
        "degenerate": bool(result.degenerate),
        "reason": str(result.reason),
        # The block length against the correlation it exists to capture.  Change in the arm's own
        # settings or windows changes it: it is derived here, never assumed.
        "window_seconds": _round(window_seconds, 3),
        "horizon_hours": None if horizon_hours is None else float(horizon_hours),
        # Where that horizon came from (the recording's plan span, its settings row, or nothing):
        # a reader must be able to tell a declared horizon from one that is simply absent.
        "horizon_source": str(getattr(result, "horizon_source", "") or ""),
        "block_length_horizon_windows": implied,
        "evidence_horizons": horizons,
        "gap_count": int(getattr(result, "gap_count", 0)),
        # Named, not just counted: the report's window list can be read against these indices.
        "gap_windows": [int(index) for index in getattr(result, "gap_windows", ()) or ()],
        "windows_contiguous": int(getattr(result, "gap_count", 0)) == 0,
        "windows_needed_for_publishable_interval": needed,
        "minimum_episode_seconds": _round(minimum_seconds, 3),
        "minimum_episode_hours": _round(None if minimum_seconds is None else minimum_seconds / 3600.0, 3),
        "underpowered": bool(getattr(result, "underpowered", False)),
        "publishable": bool(getattr(result, "publishable", True)),
        "note": str(getattr(result, "note", "") or ""),
        "evidence_requirement": EPISODE_EVIDENCE_REQUIREMENT,
    }


def _refusals_section(legs: Sequence[LegRun]) -> dict[str, Any]:
    per_leg: dict[str, Any] = {}
    total = 0
    for leg in legs:
        result = leg.result
        total += len(result.refusals)
        per_leg[result.name] = {
            "refusals": [
                {"instant": to_iso(refusal.instant), "reason": refusal.reason, "detail": refusal.detail}
                for refusal in result.refusals[:FINDING_LIMIT]
            ],
            "refusals_total": len(result.refusals),
            "plant_events": [
                {"instant": to_iso(instant), "code": str(code), "detail": str(detail)}
                for instant, code, detail in result.tally.events[:FINDING_LIMIT]
            ],
        }
    return {"total": total, "legs": per_leg}


def build_report(
    *,
    manifest: Manifest,
    arm: Any,
    legs: Sequence[LegRun],
    paired: PairedDelta | None,
    bootstrap: Any,
    isolation: Isolation | None,
    gates: Sequence[GateVerdict],
    runtime_s: float,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """The whole report as JSON-ready data: no store reads, no live data, no wall-clock guesses."""
    failed = [verdict for verdict in gates if verdict.passed is False]
    unmeasured = [verdict for verdict in gates if verdict.passed is None]
    # A gate that could not be measured is not a passed gate: the paired money must not be published
    # as if the machinery behind it had been checked.  So an unmeasurable gate withholds the arm's
    # deltas exactly like a failed one, and names itself as the reason.
    withholding = failed or unmeasured
    accounting = _accounting_section(arm, legs, paired, bootstrap)
    if withholding:
        # A failed or unmeasured gate withholds the money: a delta on top of an unexplained
        # divergence -- or on top of a gate that never ran -- would be evidence of nothing, so the
        # paid numbers are dropped and the gate is named instead.
        accounting["withheld_by_gate"] = withholding[0].gate
        accounting["paired"] = None
        accounting["bootstrap"] = None
        accounting["terminal_pair"] = None
    steps = sum(int(leg.result.steps) for leg in legs)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": to_iso(generated_at) if generated_at is not None else None,
        "run": _run_section(manifest, runtime_s, steps, len(legs)),
        "drive": _drive_section(arm, legs),
        "plant": _plant_section(manifest, legs),
        "information": _information_section(legs),
        "accounting": accounting,
        "gates": [verdict.as_dict() for verdict in gates],
        "limits": {
            "status": (
                "refused_at_a_gate"
                if failed
                else ("gates_passed" if all(verdict.measured for verdict in gates) else "gates_not_measured")
            ),
            "declared": [dict(limitation) for limitation in manifest.limitations],
            "assumptions": list(manifest.assumptions),
            "plant_fidelity_limits": list(PLANT_FIDELITY_LIMITS),
            "withheld": withholding[0].gate if withholding else None,
        },
        "refusals": _refusals_section(legs),
    }


# -------------------------------------------------------------------------------------- rendering


def deterministic_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """The report with its wall-clock fields removed: two runs must be identical here."""
    copy = json.loads(json.dumps(report))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in VOLATILE_KEYS:
                node.pop(key, None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(copy)
    return copy


def render(report: Mapping[str, Any], fmt: str = "toon") -> str:
    """Render the report in its declared dialect: ``toon`` (compact) or ``json`` (exact)."""
    dialect = str(fmt).lower()
    if dialect == "toon":
        return toon(report) + "\n"
    if dialect == "json":
        return json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    raise ReportError(REPORT_INVALID, f"unsupported report format {fmt!r} (toon|json)")


def toon(payload: Mapping[str, Any]) -> str:
    """The harness's own TOON subset: nested keys, one row per array item, scalars inline.

    Deterministic by construction (insertion order, no sorting of the data itself) and JSON-exact
    for anything inline, so a reader can always recover a value without guessing at formatting.
    """
    lines: list[str] = []
    _toon_node(payload, "", lines)
    return "\n".join(lines)


def _inline(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReportError(REPORT_INVALID, "the report carries a non-finite number")
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if not value:
        return "[]" if isinstance(value, list) else "{}"
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _toon_node(value: Any, prefix: str, lines: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, Mapping):
                if not item:
                    lines.append(f"{name}: {{}}")
                else:
                    _toon_node(item, name, lines)
            elif isinstance(item, (list, tuple)):
                if not item:
                    lines.append(f"{name}: []")
                elif all(isinstance(entry, Mapping) for entry in item):
                    lines.append(f"{name}[{len(item)}]{{}}:")
                    for entry in item:
                        lines.append(f"{name}[]: {_inline(entry)}")
                else:
                    lines.append(f"{name}: {_inline(list(item))}")
            else:
                lines.append(f"{name}: {_inline(item)}")
        return
    lines.append(f"{prefix}: {_inline(value)}")


# ----------------------------------------------------------------------------------------- helpers

#: Read as the type of a leg: anything exposing the fields the gates and readers need.
LegRunnerLike = Any

_DEFAULT_SLOT_MINUTES = 60.0
