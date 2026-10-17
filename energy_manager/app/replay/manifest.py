"""The episode manifest: the experiment contract, frozen before a replay runs.

``docs/REPLAY_HARNESS.md`` sections 1, 5 and 6 fix what a replay is: the window, the arms and the
one lever each changes, the tolerances gate 3 is judged against, and the frozen state every arm
must be paired on.  All of it is written down *before* the first step, because a tolerance chosen
after the divergence is visible is not a tolerance -- so the manifest is an input as much as an
output of a run:

* ``build_manifest(episode, ...)`` freezes a window into a :class:`Manifest` (window, arms,
  tolerances, plant ceilings, the fingerprint of every decision-relevant recorded value);
* ``load_manifest(path, ...)`` validates an existing file against the episode and the requested
  arms, and refuses when the frozen state has drifted (``fingerprint_mismatch``) instead of
  reusing stale tolerances for a different recording.

The manifest never carries recording data: the fingerprint keeps a SHA-256 per component, so
drift is detectable and the file stays small enough to read in a report.

Refusal slugs are stable and machine-readable (docs/REPLAY_HARNESS.md section 6).  A manifest
that cannot be built or validated raises :class:`ManifestError` with one of them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..config import DEFAULTS, ConfigError, coerce_setting
from ..models import from_iso, to_iso
from .episode import Episode

#: Bumped whenever the declaration's shape or the canonical arms change: a manifest written under
#: an older version is refused by name rather than reinterpreted under this contract.
#: Bumped whenever the frozen content layout changes (v2: content-hashed boundary rows; v3: the
#: window-start valuation is its own fingerprint component; v4: every row list is hashed in content
#: order, never in the reader's arrival order), because a digest computed under the old layout must
#: refuse loudly as an unsupported version, not silently as an unrelated drift.
MANIFEST_VERSION = "replay-manifest/4"

#: Stable, machine-readable refusal slugs (docs/REPLAY_HARNESS.md section 6).
MANIFEST_INVALID = "manifest_invalid"
MANIFEST_MISMATCH = "manifest_mismatch"
FINGERPRINT_MISMATCH = "fingerprint_mismatch"
ARM_UNKNOWN = "arm_unknown"
ARM_UNSUPPORTED = "arm_unsupported"
TOLERANCE_INVALID = "tolerance_invalid"
PLANT_LIMIT_INVALID = "plant_limit_invalid"
#: A loaded arm spec that differs from the canonical spec for its name: an older manifest must not
#: run with a different leg set, a price patch on the wrong leg, or a missing reference leg.
ARM_SPEC_CHANGED = "arm_spec_changed"

ARMS = ("E1", "E2", "E3", "E4")
PLANTS = ("recorded", "simulated")
POLICIES = ("production", "recorded")

#: The four disclosures accepted for stage 2c.  They travel in the manifest so every report can
#: echo them verbatim: evidence is never overstated as VSS or as a shipped comparison.
#: Limitation slugs the report echoes: one per part of the design that does not exist yet.
SAA_NOT_SHIPPED = "saa_not_shipped"
TERMINAL_VECTOR_NOT_SHIPPED = "terminal_vector_not_shipped"
LIMITATIONS: tuple[dict[str, Any], ...] = (
    {
        "slug": TERMINAL_VECTOR_NOT_SHIPPED,
        "arms": ["E3"],
        "text": "The stage-3 terminal vector does not exist; E3 pairs the accounting with and "
        "without the terminal-inventory term at the declared valuation instead.",
    },
    {
        "slug": SAA_NOT_SHIPPED,
        "arms": ["E4"],
        "text": "No expected-cost SAA or candidate policy exists in the optimizer; E4 compares "
        "the production deterministic policy against the recorded policy.",
    },
    {
        "slug": "toon_subset",
        "arms": [],
        "text": "The report's toon rendering implements a documented subset (indented mappings, "
        "- lists, tabular arrays); no in-repo dialect exists to match byte for byte.",
    },
    {
        "slug": "forecast_archive_truncated",
        "arms": ["E1", "E2", "E4"],
        "text": "Archived forecast targets past the window end are dropped by episode extraction, "
        "so the as-of recorded plan's own plan_slots is the primary vintage and a window it does "
        "not cover is refused no_vintage, never solved on a shortened horizon.",
    },
)

#: Declared assumptions the recording cannot answer (docs/REPLAY_HARNESS.md section 1).
ASSUMPTIONS = (
    "replan_backoff_at_window_start_is_min_replan_gap_s",
    "boot_hold_recovery_not_replayed_before_the_window",
)

#: The one declared valuation pair (docs/REPLAY_HARNESS.md section 4).
VALUATION = {
    "headline": "cost_basis_window_start",
    "sensitivity": "ledger_booked",
}


class ManifestError(RuntimeError):
    """A refused manifest, with a stable ``reason`` slug (never a silent default)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


# ----------------------------------------------------------------------------- the arm contract


@dataclass(frozen=True)
class LegSpec:
    """One engine pass of an arm: a plant, a policy, and the settings patch it sees."""

    name: str
    plant: str
    policy: str
    patch: tuple[tuple[str, Any], ...] = ()
    #: True for the cross-check leg that replays the recorded policy through the recorded plant,
    #: so the plant-model error inside a paired delta is made visible instead of absorbed.
    isolate: bool = False

    def patch_dict(self) -> dict[str, Any]:
        return dict(self.patch)


@dataclass(frozen=True)
class ArmSpec:
    """An arm of the experiment (docs/OPTIMIZER_V2.md section 3.1)."""

    name: str
    changes: str
    holds: tuple[str, ...]
    legs: tuple[LegSpec, ...]
    #: True when the arm's second leg is an accounting pair on the same trajectory (E3).
    terminal_pair: bool = False


def _leg(name: str, plant: str, policy: str, **kwargs: Any) -> LegSpec:
    return LegSpec(name=name, plant=plant, policy=policy, **kwargs)


def arm_spec(names: Sequence[str], contract: Mapping[str, Any] | None = None) -> tuple[ArmSpec, ...]:
    """The declared arms, or a refusal naming the lever the manifest did not fix.

    ``contract`` is E2's declared tariff model the policy must *see* (``optimizer.feed_in_wiring``
    plus a supported ``price.feed_in_mode``): without it the "new" side of the comparison does not
    exist, and inventing one would silently turn the arm into a fidelity rerun.
    """
    wanted: list[str] = []
    for raw in names:
        name = str(raw).strip().upper()
        if name not in ARMS:
            raise ManifestError(ARM_UNKNOWN, f"unknown arm {raw!r}; have {', '.join(ARMS)}")
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        raise ManifestError(MANIFEST_INVALID, "at least one arm is required")

    patch: tuple[tuple[str, Any], ...] = ()
    if "E2" in wanted:
        patch = _contract_patch(contract)

    specs: list[ArmSpec] = []
    for name in wanted:
        if name == "E1":
            specs.append(
                ArmSpec(
                    name="E1",
                    changes="none",
                    holds=("plant", "vintages", "policy", "settlement_prices", "valuation"),
                    legs=(_leg("replay", "recorded", "production"),),
                )
            )
        elif name == "E2":
            specs.append(
                ArmSpec(
                    name="E2",
                    changes="tariff_model",
                    holds=(
                        "initial_state",
                        "actuator_limits",
                        # Both legs run the SAME simulated plant: a recorded plant ignores commands
                        # entirely, so an E2 pair on it would have identical flows and a
                        # delta-profit of exactly zero -- the arm's whole output.
                        "plant",
                        "plant_physics",
                        "vintages",
                        "policy",
                        "settlement_prices",
                        "valuation",
                    ),
                    legs=(
                        _leg("recorded_model", "simulated", "production"),
                        _leg("declared_model", "simulated", "production", patch=patch),
                        # The pair answers "what did the wrong tariff cost?"; this third leg answers
                        # "is the machinery that measured it faithful?".  It is E1's leg (recorded
                        # plant, production policy, no patch), so gates 1 and 3 are measured in this
                        # arm's own report instead of borrowed from another arm's run.
                        _leg("fidelity_anchor", "recorded", "production"),
                    ),
                )
            )
        elif name == "E3":
            specs.append(
                ArmSpec(
                    name="E3",
                    changes="terminal_treatment",
                    holds=(
                        "plant",
                        "vintages",
                        "policy",
                        "settlement_prices",
                        "actuator_limits",
                    ),
                    legs=(_leg("replay", "recorded", "production"),),
                    terminal_pair=True,
                )
            )
        else:  # E4
            specs.append(
                ArmSpec(
                    name="E4",
                    changes="policy",
                    holds=(
                        "window",
                        "initial_state",
                        "actuator_limits",
                        "plant_physics",
                        "vintages",
                        "settlement_prices",
                        "valuation",
                    ),
                    legs=(
                        _leg("policy", "simulated", "production"),
                        _leg("recorded_policy", "simulated", "recorded"),
                        _leg("recorded_policy_recorded_plant", "recorded", "recorded", isolate=True),
                        # The reference leg gates 1-3 are defined against (E1's leg): without it this
                        # arm's numbers could not be reported as evidence (REPLAY_HARNESS.md 5), and
                        # the isolate leg above stays the posture cross-check rather than a reference.
                        _leg("fidelity_anchor", "recorded", "production"),
                    ),
                )
            )
    return tuple(specs)


def _contract_patch(contract: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """Validate E2's declared contract model into a settings patch the engine can consume."""
    if not contract:
        raise ManifestError(
            ARM_UNSUPPORTED,
            "E2 needs a declared contract: optimizer.feed_in_wiring and price.feed_in_mode",
        )
    patch: dict[str, Any] = {}
    for key, value in contract.items():
        if key not in DEFAULTS:
            raise ManifestError(MANIFEST_INVALID, f"unknown setting in the E2 contract: {key}")
        try:
            patch[key] = coerce_setting(key, value)
        except ConfigError as exc:
            raise ManifestError(MANIFEST_INVALID, f"E2 contract {key}: {exc}") from exc
    wiring = str(patch.get("optimizer.feed_in_wiring", "off"))
    mode = str(patch.get("price.feed_in_mode", "derived"))
    if wiring not in ("on", "shadow"):
        raise ManifestError(
            ARM_UNSUPPORTED, f"E2 contract must dispatch the new model (wiring on/shadow), got {wiring!r}"
        )
    if mode == "derived":
        raise ManifestError(
            ARM_UNSUPPORTED, "E2 contract must name a supported price.feed_in_mode, not 'derived'"
        )
    return tuple(sorted(patch.items()))


# ----------------------------------------------------------------------------------- tolerances


@dataclass(frozen=True)
class Tolerances:
    """Gate 3's declared numbers (docs/REPLAY_HARNESS.md section 5).

    ``grid_step_s`` is the planner's own slot length, so a decision is "equal" within one grid
    step.  The other three are absolute: kWh of booked energy, EUR/kWh of booked basis, and EUR/kWh
    of the recording's booked feed-in price against the contract model the run assumes for it.
    """

    grid_step_s: float
    energy_kwh: float
    basis_eur_per_kwh: float
    #: How far the recording's own booked sell price may sit from the assumed contract model before
    #: that window's export revenue is withheld.  Default 0.0005 EUR/kWh (0.05 ct/kWh).
    price_eur_per_kwh: float = 0.0005


@dataclass(frozen=True)
class PlantCeiling:
    """Declared ceilings on plant unavailability inside one decision window.

    Exceeding any of them withholds that window's delta and interval: a window whose plant could
    not be simulated must not carry a profit comparison.  ``unverified_bounds=None`` means the
    quantity is not applicable for that plant -- the recorded arm never evaluates the site export
    bound at all (``plant.py`` says so), which is a declaration, not a failure count.
    """

    energy_unknown_ticks: int
    infeasible_commands: int
    unverified_bounds: int | None


@dataclass(frozen=True)
class PlantLimits:
    recorded: PlantCeiling
    simulated: PlantCeiling

    def ceiling(self, plant: str) -> PlantCeiling:
        return self.recorded if plant == "recorded" else self.simulated


def default_tolerances(slot_minutes: int) -> Tolerances:
    """The declared defaults, derived from the recording's own planner grid."""
    return Tolerances(
        grid_step_s=float(slot_minutes) * 60.0,
        energy_kwh=0.5,
        basis_eur_per_kwh=0.005,
        price_eur_per_kwh=0.0005,
    )


def default_plant_limits() -> PlantLimits:
    return PlantLimits(
        recorded=PlantCeiling(0, 0, None),
        simulated=PlantCeiling(0, 0, 0),
    )


# ---------------------------------------------------------------------------------- fingerprint


def canonical_json(value: Any) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, no NaN/Infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
        ensure_ascii=True,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def digest_of(value: Any) -> str:
    try:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ManifestError(MANIFEST_INVALID, f"cannot fingerprint recorded data: {exc}") from exc


def _content_order(rows: Iterable[Any]) -> list[Any]:
    """Rows in a content-stable order: a digest must not depend on the query's row order.

    The episode's readers happen to be ordered (``ORDER BY ts`` and friends), but the fingerprint
    is the contract a re-run is checked against, so it sorts by the content itself rather than
    trusting a reader to stay ordered: a changed query order would otherwise drift every manifest
    while the recording is byte-identical.
    """
    materialised = list(rows)
    try:
        return sorted(materialised, key=canonical_json)
    except (TypeError, ValueError) as exc:
        # The sort key hashes every row before ``digest_of`` gets a chance to, so a non-finite
        # recorded value has to refuse as bad data here too: a raw ``ValueError`` would escape the
        # caller's refusal handling and report the run as an unexpected error (exit 1) instead of a
        # refusal (exit 2), and the exit codes are documented because tools gate on them.
        raise ManifestError(
            MANIFEST_INVALID, f"cannot fingerprint recorded data: {exc}"
        ) from exc


def _plan_payload(plan: Any) -> Any:
    if plan is None:
        return None
    return {
        "generated_at": to_iso(plan.generated_at),
        "horizon_start": to_iso(plan.horizon_start),
        "horizon_end": to_iso(plan.horizon_end),
        "slot_minutes": int(plan.slot_minutes),
        "feasible": bool(plan.feasible),
        "mode": str(plan.mode),
        "dry_run": bool(plan.dry_run),
        "batteries": list(plan.batteries),
        "slots": [
            [
                to_iso(slot.t),
                float(slot.buy),
                float(slot.sell),
                float(slot.pv_kw),
                float(slot.load_kw),
                float(slot.soc_kwh),
                float(slot.power_w),
                str(getattr(slot.action, "value", slot.action)),
                str(slot.reason),
            ]
            for slot in plan.slots
        ],
    }


def _window_start_valuation_payload(episode: Episode) -> Any:
    """The declared window-start valuation, as money-relevant as the rows it is derived from.

    The terminal term is valued on this declaration, and the rows ``update_basis`` reads to derive
    it were outside the fingerprint while the derived number was not: a changed ledger or actuals
    history with an unchanged shape could not be caught by a row count.  Fingerprinting the derived
    declaration itself is the honest unit -- it is what the accounting uses.
    """
    from .accounting import window_start_valuation

    return window_start_valuation(episode).as_dict()


def fingerprint_components(episode: Episode) -> dict[str, Any]:
    """The decision-relevant recorded state, per component.

    Every component holds *content*, not identifiers: a changed recorded value drifts the digest
    even when the row count is identical.  The scratch copy's path is deliberately absent, so two
    runs of the same recording share a fingerprint.

    Every row list is hashed in content order (see :func:`_content_order`), never in the
    reader's arrival order.

    ``window_start_valuation`` is derived by *writing* the episode's own scratch ledger (the same
    call the engine's first tick makes) -- it is not a read.  That write is idempotent at a fixed
    instant, and ``test_finding_2_deriving_the_window_start_valuation_is_idempotent`` in
    ``tests/test_replay_manifest.py`` pins it, so fingerprinting the same copy twice neither drifts
    the ledger nor the declaration; the derivation stays contained to the copy it fingerprints.  It
    has its own component so a change in it is attributed by name instead of reading as boundary
    drift.
    """
    warm = episode.warm_start
    return {
        "settings": {
            key: episode.settings_values[key] for key in sorted(episode.settings_values)
        },
        "profiles": sorted(
            ([str(kind), str(key), payload] for (kind, key), payload in episode.profiles.items()),
            key=lambda row: (row[0], row[1]),
        ),
        "batteries": _content_order([asdict(config) for config in episode.batteries]),
        "initial_batteries": _content_order(
            [asdict(state) for state in episode.initial_battery_states()]
        ),
        "warm_start": {
            "plan": _plan_payload(warm.plan),
            "last_replan_at": None if warm.last_replan_at is None else to_iso(warm.last_replan_at),
            "backoff_s": float(warm.backoff_s),
        },
        "boundary": {
            "plan_id": episode.boundary.plan_id,
            "dispatch_id": episode.boundary.dispatch_id,
            "event_id": episode.boundary.event_id,
            # Full row content per table, not a row count: a changed price or flow with the same
            # row count must drift the fingerprint, which is the whole point of freezing it.
            "rows": {
                table: digest_of(_content_order([dict(row) for row in rows]))
                for table, rows in sorted(episode.boundary.source_rows.items())
            },
            "row_counts": {
                table: len(rows) for table, rows in sorted(episode.boundary.source_rows.items())
            },
        },
        # Its own component, so money the terminal term is valued on is never read as boundary or
        # recording drift.  Deriving it writes the ledger on this copy (see the docstring above).
        "window_start_valuation": _window_start_valuation_payload(episode),
        "series": {
            name: _content_order([[to_iso(moment), value] for moment, value in points])
            for name, points in sorted(episode.series.items())
        },
        "plans": _content_order([_plan_payload(plan) for plan in episode.plans]),
        "dispatch": _content_order(
            [
                [
                    str(row.get("ts")),
                    str(row.get("battery_id")),
                    row.get("requested_w"),
                    row.get("applied_w"),
                    str(row.get("code")),
                    str(row.get("result")),
                    bool(row.get("dry_run")),
                ]
                for row in episode.dispatch
            ]
        ),
        "forecasts": _content_order(
            [
                [
                    str(row.get("kind")),
                    str(row.get("source")),
                    str(row.get("target_ts")),
                    str(row.get("issued_at")),
                    row.get("value"),
                ]
                for row in episode.forecasts
            ]
        ),
    }


def episode_fingerprint(episode: Episode) -> dict[str, Any]:
    """``{"components": {name: sha256}, "sizes": {name: n}, "digest": sha256}``."""
    components = fingerprint_components(episode)
    digests = {name: digest_of(payload) for name, payload in components.items()}
    sizes = {name: len(_flatten(payload)) for name, payload in components.items()}
    return {
        "components": digests,
        "sizes": sizes,
        "digest": digest_of(digests),
    }


def _flatten(value: Any) -> Any:
    """A cheap size proxy for the fingerprint summary (mappings/lists count their entries)."""
    if isinstance(value, Mapping):
        return list(value)
    if isinstance(value, (list, tuple)):
        return value
    return [value]


def differing_components(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str, ...]:
    """The component names whose digests disagree, plus any missing on either side."""
    left_components = dict(left.get("components") or {})
    right_components = dict(right.get("components") or {})
    names = sorted(set(left_components) | set(right_components))
    return tuple(
        name for name in names if left_components.get(name) != right_components.get(name)
    )


# ------------------------------------------------------------------------------------- manifest


@dataclass(frozen=True)
class Manifest:
    """One replay window's frozen contract."""

    start: datetime
    end: datetime
    step_s: float
    arms: tuple[ArmSpec, ...]
    tolerances: Tolerances
    plant_limits: PlantLimits
    fingerprint: Mapping[str, Any]
    seed: int = 0
    blocks: int = 4
    resamples: int = 2000
    #: How many plan horizons of windows an episode must carry before its block-bootstrap interval
    #: counts as evidence.  Declared with the other run counts, so the bar cannot be moved after an
    #: interval looks tight.
    evidence_horizons: int = 5
    limitations: tuple[dict[str, Any], ...] = LIMITATIONS
    assumptions: tuple[str, ...] = ASSUMPTIONS
    valuation: Mapping[str, str] = field(default_factory=lambda: dict(VALUATION))
    version: str = MANIFEST_VERSION

    def arm(self, name: str) -> ArmSpec:
        wanted = str(name).strip().upper()
        for spec in self.arms:
            if spec.name == wanted:
                return spec
        raise ManifestError(ARM_UNKNOWN, f"arm {name!r} is not in this manifest")

    def arm_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.arms)

    def payload(self) -> dict[str, Any]:
        """The manifest as JSON-ready data, without its own digest."""
        return {
            "version": self.version,
            "start": to_iso(self.start),
            "end": to_iso(self.end),
            "step_s": float(self.step_s),
            "seed": int(self.seed),
            "blocks": int(self.blocks),
            "resamples": int(self.resamples),
            "evidence_horizons": int(self.evidence_horizons),
            "arms": [_arm_payload(spec) for spec in self.arms],
            "tolerances": asdict(self.tolerances),
            "plant_limits": {
                "recorded": asdict(self.plant_limits.recorded),
                "simulated": asdict(self.plant_limits.simulated),
            },
            "valuation": dict(self.valuation or VALUATION),
            "fingerprint": dict(self.fingerprint),
            "limitations": [dict(item) for item in self.limitations],
            "assumptions": list(self.assumptions),
        }

    def digest(self) -> str:
        """SHA-256 over the whole declaration, tolerances included.

        The frozen state has its own :attr:`fingerprint`; this digest covers the *contract* (arms,
        tolerances, ceilings, seed).  The report prints it, so a re-run that widened a tolerance
        after seeing a divergence is visible as a different contract, not as the same experiment.

        An unkeyed self-hash is tamper-evident only against naive edits: anyone who can rewrite the
        manifest can recompute the digest too, and :func:`manifest_from_dict` recomputes it from
        the file's own content.  The guarantee needs the digest recorded *outside* the file --
        print it in the run log and pass it back via ``--expect-digest`` -- which is the only way a
        reader can tell that the file they were handed is the contract that was frozen.
        """
        return digest_of(self.payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload["manifest_digest"] = self.digest()
        return payload

    def write(self, path: str | Path) -> str:
        """Write the manifest atomically; returns the contract digest."""
        payload = self.to_dict()
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        target = Path(path).expanduser()
        if str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent or ".")
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return str(payload["manifest_digest"])


def _arm_payload(spec: ArmSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "changes": spec.changes,
        "holds": list(spec.holds),
        "terminal_pair": bool(spec.terminal_pair),
        "legs": [
            {
                "name": leg.name,
                "plant": leg.plant,
                "policy": leg.policy,
                "patch": [[key, value] for key, value in leg.patch],
                "isolate": bool(leg.isolate),
            }
            for leg in spec.legs
        ],
    }


def _arm_from_payload(payload: Mapping[str, Any]) -> ArmSpec:
    try:
        name = str(payload["name"])
        legs = tuple(
            LegSpec(
                name=str(leg["name"]),
                plant=str(leg["plant"]),
                policy=str(leg["policy"]),
                patch=tuple((str(key), value) for key, value in leg.get("patch", ())),
                isolate=bool(leg.get("isolate", False)),
            )
            for leg in payload["legs"]
        )
        spec = ArmSpec(
            name=name,
            changes=str(payload["changes"]),
            holds=tuple(str(item) for item in payload["holds"]),
            legs=legs,
            terminal_pair=bool(payload.get("terminal_pair", False)),
        )
    except (KeyError, TypeError) as exc:
        raise ManifestError(MANIFEST_INVALID, f"malformed arm entry: {exc}") from exc
    _validate_arm(spec)
    return spec


def _validate_arm(spec: ArmSpec) -> None:
    if spec.name not in ARMS:
        raise ManifestError(ARM_UNKNOWN, f"unknown arm {spec.name!r}")
    if not spec.legs:
        raise ManifestError(MANIFEST_INVALID, f"arm {spec.name} declares no legs")
    for leg in spec.legs:
        if leg.plant not in PLANTS:
            raise ManifestError(MANIFEST_INVALID, f"arm {spec.name} leg {leg.name}: plant {leg.plant!r}")
        if leg.policy not in POLICIES:
            raise ManifestError(
                MANIFEST_INVALID, f"arm {spec.name} leg {leg.name}: policy {leg.policy!r}"
            )
        patch = leg.patch_dict()
        for key, value in patch.items():
            if key not in DEFAULTS:
                raise ManifestError(MANIFEST_INVALID, f"arm {spec.name} leg {leg.name}: unknown {key}")
            try:
                coerce_setting(key, value)
            except ConfigError as exc:
                raise ManifestError(
                    MANIFEST_INVALID, f"arm {spec.name} leg {leg.name}: {key}: {exc}"
                ) from exc
    if spec.name == "E2" and not any(leg.patch for leg in spec.legs):
        raise ManifestError(ARM_UNSUPPORTED, "E2 declares no contract patch")


def _canonical_spec(spec: ArmSpec) -> ArmSpec:
    """Rebuild the canonical arm for a loaded spec's name, using its own declared contract.

    ``arm_spec`` is the one definition of an arm; a manifest must match it exactly.  E2's contract
    patch is passed back in because that is the one lever the arm declares, and it is already part
    of the spec under test.
    """
    contract = dict(next((leg.patch for leg in spec.legs if leg.patch), ())) or None
    return arm_spec([spec.name], contract)[0]


def _check_canonical_arms(specs: Sequence[ArmSpec]) -> None:
    """Refuse a loaded arm spec that differs from the canonical one in any respect.

    An older manifest (same version) could otherwise run with the old leg set: E1 with a price
    patch, E4 without its reference leg, or an E2 before ``fidelity_anchor`` existed.  Every
    field -- legs, their order, plant/policy/patch/isolate flags, holds, changes and
    ``terminal_pair`` -- is compared, because each one changes what the run measures.
    """
    for spec in specs:
        canonical = _canonical_spec(spec)
        if spec != canonical:
            raise ManifestError(
                ARM_SPEC_CHANGED,
                f"arm {spec.name} does not match the canonical specification: its legs, patches, "
                "holds or flags differ, so this manifest must not run",
            )


def _check_tolerances(tolerances: Tolerances) -> Tolerances:
    numbers = (
        ("grid_step_s", tolerances.grid_step_s),
        ("energy_kwh", tolerances.energy_kwh),
        ("basis_eur_per_kwh", tolerances.basis_eur_per_kwh),
        ("price_eur_per_kwh", tolerances.price_eur_per_kwh),
    )
    for name, value in numbers:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ManifestError(TOLERANCE_INVALID, f"{name} must be a finite number, got {value!r}")
        if float(value) < 0.0 or (name == "grid_step_s" and float(value) == 0.0):
            raise ManifestError(TOLERANCE_INVALID, f"{name} must be positive and finite, got {value!r}")
    return Tolerances(
        grid_step_s=float(tolerances.grid_step_s),
        energy_kwh=float(tolerances.energy_kwh),
        basis_eur_per_kwh=float(tolerances.basis_eur_per_kwh),
        price_eur_per_kwh=float(tolerances.price_eur_per_kwh),
    )


def _check_ceiling(plant: str, ceiling: PlantCeiling) -> PlantCeiling:
    counts = (
        ("energy_unknown_ticks", ceiling.energy_unknown_ticks, False),
        ("infeasible_commands", ceiling.infeasible_commands, False),
        ("unverified_bounds", ceiling.unverified_bounds, True),
    )
    for name, value, nullable in counts:
        if value is None:
            if not nullable:
                raise ManifestError(PLANT_LIMIT_INVALID, f"{plant}.{name} cannot be null")
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ManifestError(
                PLANT_LIMIT_INVALID, f"{plant}.{name} must be a non-negative integer, got {value!r}"
            )
    return PlantCeiling(
        energy_unknown_ticks=int(ceiling.energy_unknown_ticks),
        infeasible_commands=int(ceiling.infeasible_commands),
        unverified_bounds=(
            None if ceiling.unverified_bounds is None else int(ceiling.unverified_bounds)
        ),
    )


def _check_limits(limits: PlantLimits) -> PlantLimits:
    recorded = _check_ceiling("recorded", limits.recorded)
    simulated = _check_ceiling("simulated", limits.simulated)
    if simulated.unverified_bounds is None:
        raise ManifestError(
            PLANT_LIMIT_INVALID,
            "simulated.unverified_bounds cannot be null: the simulated plant does evaluate the "
            "site export bound, so the ceiling is a real count there",
        )
    return PlantLimits(recorded=recorded, simulated=simulated)


def _check_run_counts(seed: Any, blocks: Any, resamples: Any) -> tuple[int, int, int]:
    """Seed may be zero; a block length or a resample count of zero is not an experiment."""
    floors = (("seed", seed, 0), ("blocks", blocks, 1), ("resamples", resamples, 1))
    for name, value, minimum in floors:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ManifestError(
                MANIFEST_INVALID,
                f"{name} must be an integer >= {minimum}, got {value!r}",
            )
    return int(seed), int(blocks), int(resamples)


def _check_evidence_horizons(value: Any) -> int:
    """The publishability bar is a declared count of plan horizons, never a guessed one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(
            MANIFEST_INVALID, f"evidence_horizons must be an integer >= 1, got {value!r}"
        )
    return int(value)


def build_manifest(
    episode: Episode,
    *,
    arms: Sequence[str],
    tolerances: Tolerances | None = None,
    plant_limits: PlantLimits | None = None,
    seed: int = 0,
    blocks: int = 4,
    resamples: int = 2000,
    evidence_horizons: int = 5,
    contract: Mapping[str, Any] | None = None,
) -> Manifest:
    """Freeze one window's contract; caller writes it before the first step."""
    slot_minutes = int(episode.settings.get("optimizer.slot_minutes", 60) or 60)
    declared = _check_tolerances(tolerances or default_tolerances(slot_minutes))
    limits = _check_limits(plant_limits or default_plant_limits())
    declared_seed, declared_blocks, declared_resamples = _check_run_counts(seed, blocks, resamples)
    declared_horizons = _check_evidence_horizons(evidence_horizons)
    return Manifest(
        start=episode.start,
        end=episode.end,
        step_s=float(episode.step_s),
        arms=arm_spec(arms, contract),
        tolerances=declared,
        plant_limits=limits,
        fingerprint=episode_fingerprint(episode),
        seed=declared_seed,
        blocks=declared_blocks,
        resamples=declared_resamples,
        evidence_horizons=declared_horizons,
        valuation=dict(VALUATION),
    )


def manifest_from_dict(payload: Mapping[str, Any]) -> Manifest:
    """Rebuild a manifest from JSON data, refusing a tampered or malformed contract."""
    try:
        version = str(payload["version"])
        start = from_iso(payload["start"])
        end = from_iso(payload["end"])
        step_s = float(payload["step_s"])
        arms = tuple(_arm_from_payload(entry) for entry in payload["arms"])
        raw_tolerances = dict(payload["tolerances"])
        missing = sorted(
            key
            for key in ("grid_step_s", "energy_kwh", "basis_eur_per_kwh", "price_eur_per_kwh")
            if key not in raw_tolerances
        )
        if missing:
            # A contract written before a tolerance existed cannot be compared with it, and filling
            # the gap with a default would silently weaken the gate it pins.
            raise ManifestError(
                MANIFEST_INVALID,
                f"tolerances is missing {', '.join(missing)}: the declared contract predates the "
                "field, so it cannot be compared against this run",
            )
        tolerances = _check_tolerances(Tolerances(**raw_tolerances))
        limits_payload = dict(payload["plant_limits"])
        limits = _check_limits(
            PlantLimits(
                recorded=PlantCeiling(**dict(limits_payload["recorded"])),
                simulated=PlantCeiling(**dict(limits_payload["simulated"])),
            )
        )
        fingerprint = dict(payload["fingerprint"])
        seed, blocks, resamples = _check_run_counts(
            payload.get("seed", 0), payload.get("blocks", 4), payload.get("resamples", 2000)
        )
        evidence_horizons = _check_evidence_horizons(payload.get("evidence_horizons", 5))
        valuation = dict(payload.get("valuation", VALUATION))
        limitations = tuple(dict(item) for item in payload.get("limitations", ()))
        assumptions = tuple(str(item) for item in payload.get("assumptions", ()))
        stored_digest = str(payload["manifest_digest"])
    except ManifestError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(MANIFEST_INVALID, f"malformed manifest: {exc}") from exc

    if version != MANIFEST_VERSION:
        raise ManifestError(MANIFEST_INVALID, f"unsupported manifest version {version!r}")
    if start is None or end is None or end <= start:
        raise ManifestError(MANIFEST_INVALID, f"empty or unparsable window: {start} .. {end}")
    if step_s <= 0 or step_s != step_s:
        raise ManifestError(MANIFEST_INVALID, f"step_s must be positive and finite, got {step_s!r}")
    if not arms:
        raise ManifestError(MANIFEST_INVALID, "the manifest declares no arms")
    names = [spec.name for spec in arms]
    if len(set(names)) != len(names):
        raise ManifestError(MANIFEST_INVALID, f"duplicate arms: {names}")
    _check_canonical_arms(arms)
    if valuation != dict(VALUATION):
        raise ManifestError(
            MANIFEST_INVALID,
            f"the manifest declares the valuation {valuation} rather than the declared pair "
            f"{dict(VALUATION)}; a different valuation is a different experiment",
        )
    if not limitations:
        raise ManifestError(MANIFEST_INVALID, "the manifest carries no limitations")
    components = dict(fingerprint.get("components") or {})
    if not components or not str(fingerprint.get("digest") or ""):
        raise ManifestError(MANIFEST_INVALID, "the manifest carries no fingerprint")

    manifest = Manifest(
        start=start,
        end=end,
        step_s=step_s,
        arms=arms,
        tolerances=tolerances,
        plant_limits=limits,
        fingerprint=fingerprint,
        seed=seed,
        blocks=blocks,
        resamples=resamples,
        evidence_horizons=evidence_horizons,
        valuation=valuation,
        limitations=limitations,
        assumptions=assumptions,
        version=version,
    )
    if manifest.digest() != stored_digest:
        raise ManifestError(
            MANIFEST_INVALID,
            "the manifest's own digest does not match its content: it was edited after it was "
            "written, so its tolerances were not fixed before the run",
        )
    return manifest


def declared_contract(manifest: Manifest, arm: str = "E2") -> tuple[tuple[str, Any], ...]:
    """The contract model a manifest froze for an arm: its declared leg's settings patch.

    The manifest is authoritative -- it was written before the run -- so this is exactly what a
    re-run has to declare again: a different ``--setting`` describes a different experiment rather
    than a re-run of this one, and :func:`load_manifest` refuses it by name.
    """
    try:
        spec = manifest.arm(arm)
    except ManifestError:
        return ()
    for leg in spec.legs:
        if leg.patch:
            return tuple(leg.patch)
    return ()


def load_manifest(
    path: str | Path,
    *,
    episode: Episode,
    arms: Sequence[str] | None = None,
    contract: Mapping[str, Any] | None = None,
) -> Manifest:
    """Load and validate a manifest written before the run.

    The stored contract wins (it was fixed first): the requested arms must match it exactly, the
    window must be the episode's, and every fingerprint component must agree with the recording.
    A drifted component is named in the refusal, so the difference is attributable.  ``contract`` is
    the declared settings model a re-run is asking for; it must equal the frozen one, so re-running
    with different declared money is refused instead of quietly measuring something else.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise ManifestError(MANIFEST_INVALID, f"no manifest at {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(MANIFEST_INVALID, f"cannot read {target}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ManifestError(MANIFEST_INVALID, f"{target} is not a JSON object")
    manifest = manifest_from_dict(payload)

    if (manifest.start, manifest.end) != (episode.start, episode.end):
        raise ManifestError(
            MANIFEST_MISMATCH,
            f"window {to_iso(manifest.start)}..{to_iso(manifest.end)} does not match the episode "
            f"{to_iso(episode.start)}..{to_iso(episode.end)}",
        )
    if float(manifest.step_s) != float(episode.step_s):
        raise ManifestError(
            MANIFEST_MISMATCH, f"step {manifest.step_s} does not match the episode {episode.step_s}"
        )
    if arms is not None:
        requested = sorted({str(item).strip().upper() for item in arms})
        if requested != sorted(manifest.arm_names()):
            raise ManifestError(
                MANIFEST_MISMATCH,
                f"arms {requested} do not match the frozen manifest {list(manifest.arm_names())}",
            )
    if contract is not None:
        declared = _contract_patch(contract)
        frozen = declared_contract(manifest)
        if declared != frozen:
            raise ManifestError(
                MANIFEST_MISMATCH,
                f"the declared contract {dict(declared)} does not match the frozen manifest "
                f"{dict(frozen)}; a re-run has to declare the same settings",
            )

    current = episode_fingerprint(episode)
    if current.get("digest") != manifest.fingerprint.get("digest"):
        drifted = differing_components(manifest.fingerprint, current) or ("digest",)
        raise ManifestError(
            FINGERPRINT_MISMATCH,
            "the frozen state has drifted since the manifest was written: "
            + ", ".join(drifted),
        )
    return manifest
