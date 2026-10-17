"""The replay CLI: drive one recorded window and print the report.

    python -m app.replay --store /path/energy_manager.db \
        --start 2026-10-16T11:43:57+00:00 --end 2026-10-16T12:05:00+00:00 --arm E1

Exit codes (stable, because tools gate on them):

* ``0`` -- every gate that could be measured passed, and none failed (``0`` means no gate failed,
  not that money was published: a run whose money was withheld on an unmeasured gate still exits
  ``0``, and its withheld numbers render as ``null``);
* ``2`` -- a gate failed, a precondition was refused, or the stored manifest disagrees with this run
  (the report is still printed in the requested dialect, and a failed gate withholds the arm's money
  rather than printing an unearned delta);
* ``1`` -- an unexpected error: a bug, never a refusal, and nothing is claimed about the run.

``--episode`` is both input and output: an existing manifest is loaded and validated against this
window and the frozen state, and otherwise the manifest is written once *before* the first step,
because gate-3 tolerances have to be fixed before anything is measured.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import SETTINGS_META
from ..models import from_iso, to_iso, utcnow
from .accounting import AccountingError, account_leg, window_start_valuation
from .episode import Episode, EpisodeError, open_episode
from .manifest import (
    ARMS,
    FINGERPRINT_MISMATCH,
    MANIFEST_INVALID,
    MANIFEST_MISMATCH,
    ManifestError,
    build_manifest,
    differing_components,
    episode_fingerprint,
    load_manifest,
)
from .report import (
    REPORT_SCHEMA,
    GateVerdict,
    LegRun,
    ReportError,
    build_report,
    find_isolation,
    gates_for,
    pair_arm,
    render,
)
from .runner import RunnerError, recorded_policy_source, run_leg
from .statistics import (
    HORIZON_FROM_PLAN_SPAN,
    HORIZON_FROM_SETTING,
    HORIZON_SOURCE_UNKNOWN,
    bootstrap_paired,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAILED = 2

EPILOG = """exit codes:
  0  every measured gate passed (gates that could not be measured are reported as null and their
     money is withheld: 0 means no gate failed, not that money was published)
  2  a gate failed, or a precondition was refused (the report names the refusal slug)
  1  an unexpected error -- nothing is claimed about the run

The report is written to stdout in --format (toon by default, json for the exact form), and a
re-run with the same manifest and inputs produces an identical report apart from the runtime and
the generated_at stamp."""  # noqa: E501

#: E2's declared contract model: the keys the harness's own settings machinery validates.
CONTRACT_KEYS = ("optimizer.feed_in_wiring", "price.feed_in_mode")
WIRING_CHOICES = tuple(SETTINGS_META["optimizer.feed_in_wiring"].choices or ("on", "shadow", "off"))
MODE_CHOICES = tuple(SETTINGS_META["price.feed_in_mode"].choices or ("explicit",))


def _instant(text: str) -> datetime:
    value = from_iso(text)
    if value is None:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO-8601 instant")
    return value


def _positive(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
    if not value > 0 or value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("the step must be a finite number of seconds above zero")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc
    return value


def parser() -> argparse.ArgumentParser:
    """The CLI surface, kept in step with docs/REPLAY_HARNESS.md's flag list."""
    built = argparse.ArgumentParser(
        prog="python -m app.replay",
        description="Replay one recorded window through the real engine and report the gates.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    built.add_argument("--store", required=True, help="recording (SQLite) to replay from")
    built.add_argument("--start", required=True, type=_instant, help="window start (ISO-8601)")
    built.add_argument("--end", required=True, type=_instant, help="window end (ISO-8601)")
    built.add_argument("--step", type=_positive, default=1.0, help="virtual seconds per step (default 1)")
    built.add_argument("--arm", default="E1", choices=tuple(ARMS), help="arm to run (default E1)")
    built.add_argument(
        "--episode",
        default=None,
        help="manifest file: validated when it exists, written before the first step when it does not",
    )
    built.add_argument("--format", default="toon", choices=("toon", "json"), help="report dialect")
    built.add_argument("--seed", type=_count, default=None, help="bootstrap seed (default 0; a stored manifest's frozen seed wins)")
    built.add_argument("--blocks", type=_count, default=None, help="block bootstrap length in windows (default 4; the stored manifest's frozen value wins)")
    built.add_argument("--resamples", type=_count, default=None, help="bootstrap resamples (default 2000; the stored manifest's frozen value wins)")
    built.add_argument(
        "--expect-digest",
        default=None,
        help="the contract digest recorded outside the manifest file: refused unless the loaded or "
        "newly written manifest carries exactly it",
    )
    built.add_argument(
        "--feed-in-wiring",
        default="on",
        choices=WIRING_CHOICES,
        help="E2's declared contract model: how the optimizer consumes the settlement",
    )
    built.add_argument(
        "--feed-in-mode",
        default="explicit",
        choices=MODE_CHOICES,
        help="E2's declared contract model: the feed-in contract to settle with",
    )
    built.add_argument(
        "--setting",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="E2's declared contract: any config default (validated by the same DEFAULTS + "
        "coerce_setting path as the two flags above), e.g. price.feed_in_eur_per_kwh=0.065. "
        "Repeat for several keys; a key declared here and by a flag is refused, never merged",
    )
    built.add_argument("--verbose", action="store_true", help="progress and timing on stderr")
    return built


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (EpisodeError, ManifestError, RunnerError, AccountingError, ReportError) as error:
        print(render(_refusal(args, error), args.format), end="")
        return EXIT_FAILED
    except (KeyboardInterrupt, BrokenPipeError):
        return EXIT_ERROR
    except Exception as error:  # noqa: BLE001 - an unexpected failure is a bug, not a refusal
        print(f"replay failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_ERROR


def run(args: argparse.Namespace) -> int:
    """Drive the arm's legs and print the report; the exit code reflects the gates.

    Every leg runs on its **own** scratch copy of the recording.  A running engine writes plans,
    actuals, measurements, dispatch rows and the cost-basis ledger, so a leg that started from
    another leg's scratch would read its neighbour's writes as if they were the recording's -- and
    an arm's gates would then depend on which legs ran before it.  The copies stay alive until the
    report is built, because the accounting and the gates read each leg's own episode.
    """
    if args.end <= args.start:
        raise ManifestError(MANIFEST_INVALID, "the window end must be after its start")
    store = str(Path(args.store).expanduser())
    with ExitStack() as stack:

        def open_copy() -> Episode:
            """A pristine scratch copy, released when the run is over."""
            return stack.enter_context(
                open_episode(store, args.start, args.end, float(args.step))
            )

        # The manifest is fingerprinted against a pristine copy, before any leg has written.
        manifest = _manifest(args, open_copy())
        # The window-start valuation is declared ONCE, on a dedicated copy, and every leg is
        # required to derive the same declaration: two legs that silently valued the same window
        # differently would make the arm's terminal terms incomparable.
        declared_valuation = window_start_valuation(open_copy())
        if args.verbose:
            print(
                f"window-start valuation declared: {declared_valuation.source}, "
                f"{declared_valuation.packs} pack(s)"
                + (
                    f", {len(declared_valuation.missing)} unvalued"
                    if declared_valuation.missing
                    else ""
                ),
                file=sys.stderr,
            )
        return _drive(args, manifest, open_copy, declared_valuation)


def _drive(
    args: argparse.Namespace,
    manifest: Any,
    open_copy: Callable[[], Episode],
    declared_valuation: Any,
) -> int:
    """Run the arm's legs -- each on its own scratch copy -- and print the report."""
    arm = manifest.arm(args.arm)
    started = time.monotonic()
    legs: list[LegRun] = []
    for spec in arm.legs:
        episode = open_copy()
        # Every leg's copy is fingerprinted against the frozen contract, not only the first: a leg
        # must never run on a recording that has drifted since the manifest was written.
        current = episode_fingerprint(episode)
        if current.get("digest") != manifest.fingerprint.get("digest"):
            drifted = differing_components(manifest.fingerprint, current) or ("digest",)
            raise ManifestError(
                FINGERPRINT_MISMATCH,
                f"leg {spec.name}: the frozen state has drifted since the manifest was written: "
                + ", ".join(drifted),
            )
        valuation = window_start_valuation(episode)
        if valuation != declared_valuation:
            raise ManifestError(
                MANIFEST_MISMATCH,
                f"leg {spec.name}: its window-start valuation differs from the declaration made "
                f"before the run ({valuation.source} vs {declared_valuation.source}), so the "
                "terminal terms would not be valued on the same declaration",
            )
        if args.verbose:
            print(
                f"window-start valuation ({spec.name}): {valuation.source}, "
                f"{valuation.packs} pack(s)"
                + (f", {len(valuation.missing)} unvalued" if valuation.missing else ""),
                file=sys.stderr,
            )
        source = recorded_policy_source(episode) if spec.policy == "recorded" else None
        result = run_leg(
            episode,
            name=spec.name,
            plant=spec.plant,
            policy=spec.policy,
            settings_patch=spec.patch_dict(),
            command_source=source,
        )
        patch = spec.patch_dict()
        legs.append(
            LegRun(
                result=result,
                account=account_leg(
                    result,
                    limits=manifest.plant_limits,
                    window_start=valuation,
                    tariff_tolerance_eur_per_kwh=manifest.tolerances.price_eur_per_kwh,
                    # An arm that declares its own feed-in contract is declared by construction:
                    # there is no old-side model to check the recording's price against.
                    tariff_declared=any(
                        key.startswith(("price.feed_in_", "price.export_fee", "price.energy_tax"))
                        for key in patch
                    ),
                ),
            )
        )
        if args.verbose:
            print(
                f"{result.name}: {result.steps} steps, {result.control_steps} control ticks, "
                f"{len(result.refusals)} refusal(s)",
                file=sys.stderr,
            )
    runtime_s = time.monotonic() - started
    isolation = find_isolation(arm, legs)
    gates = gates_for(arm, legs, tolerances=manifest.tolerances, isolation=isolation)
    paired = pair_arm(arm, legs)
    horizon_hours, horizon_source = _plan_horizon_hours(legs[0].result.episode)
    bootstrap = (
        None
        if paired is None
        else bootstrap_paired(
            paired,
            blocks=manifest.blocks,
            resamples=manifest.resamples,
            seed=manifest.seed,
            evidence_horizons=int(manifest.evidence_horizons),
            # The block length is judged against the horizon the recording's own plans ran under,
            # so a short window says its interval is not evidence instead of looking tight. The
            # warm start's plan carries the engine's own horizon_end - horizon_start; a recording
            # without plans falls back to the effective setting the engine would have used, and a
            # horizon that neither can supply stays None (with its provenance recorded): inventing
            # 30 h here would invent the very horizon plan_horizon_windows refuses to guess, so no
            # horizon-implied claim is published for such a recording.
            horizon_hours=horizon_hours,
            horizon_source=horizon_source,
        )
    )
    report = build_report(
        manifest=manifest,
        arm=arm,
        legs=legs,
        paired=paired,
        bootstrap=bootstrap,
        isolation=isolation,
        gates=gates,
        runtime_s=runtime_s,
        generated_at=utcnow(),
    )
    if args.verbose:
        print(f"runtime {runtime_s:.1f}s, arm {arm.name}", file=sys.stderr)
    print(render(report, args.format), end="")
    return EXIT_FAILED if _failed(gates) else EXIT_OK


def _failed(gates: Sequence[GateVerdict]) -> bool:
    return any(verdict.passed is False for verdict in gates)


def _optional_float(value: Any) -> float | None:
    """A finite float, or ``None`` when the recording does not declare one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _plan_horizon_hours(episode: Episode) -> tuple[float | None, str]:
    """The horizon the recording's own plans ran under, and where that number came from.

    The plan's own span is the horizon the engine actually planned over, so it needs no setting
    to be present (a settings row is only written when a value changes, so ``horizon_hours`` is
    usually absent from a recording even though every plan ran with one).  A recording with no plan
    falls back to the settings row the recording itself wrote (``settings_values``, never the
    resolved settings: a value that came from ``DEFAULTS`` is the engine's default, not a
    declaration by the recording); one that declares neither returns ``(None, unknown)`` rather
    than the engine's 30 h default, because the replay applies that default to reproduce the
    engine's decisions but must not publish a horizon-implied claim the recording never stated.
    """
    plan = episode.warm_start.plan
    if plan is not None:
        hours = (plan.horizon_end - plan.horizon_start).total_seconds() / 3600.0
        if hours > 0.0:
            return hours, HORIZON_FROM_PLAN_SPAN
    declared = _optional_float(episode.settings_values.get("optimizer.horizon_hours", None))
    if declared is not None and declared > 0.0:
        return declared, HORIZON_FROM_SETTING
    return None, HORIZON_SOURCE_UNKNOWN


def _check_requested_counts(args: argparse.Namespace, manifest: Any) -> None:
    """Refuse an explicit ``--seed/--blocks/--resamples`` that disagrees with the frozen manifest.

    Silently ignoring them would report the stored run counts while the caller believed they had
    asked for different ones -- the same failure mode ``--setting`` is refused for.  The label is
    ``manifest_mismatch``, not ``manifest_invalid``: the frozen manifest is well formed, it is the
    caller's request that is out of step with the contract being re-run.
    """
    for name in ("seed", "blocks", "resamples"):
        requested = getattr(args, name, None)
        if requested is None:
            continue
        frozen = int(getattr(manifest, name))
        if int(requested) != frozen:
            raise ManifestError(
                MANIFEST_MISMATCH,
                f"--{name} {requested} does not match the frozen manifest ({frozen}): the manifest "
                "is authoritative, so a re-run cannot use different run counts",
            )


def _check_expected_digest(args: argparse.Namespace, manifest: Any) -> None:
    """The one check an unkeyed self-hash cannot make for itself: an externally recorded digest."""
    expected = getattr(args, "expect_digest", None)
    if not expected:
        return
    digest = manifest.digest()
    if str(expected).strip().lower() != digest.lower():
        raise ManifestError(
            MANIFEST_MISMATCH,
            f"--expect-digest {expected} does not match the contract digest {digest}: this "
            "manifest is not the contract that was recorded outside the file",
        )


def _declared_settings(args: argparse.Namespace) -> dict[str, Any]:
    """E2's declared contract model: its two flags plus every ``--setting KEY=VALUE``.

    All of it goes through the manifest's own ``_contract_patch`` validation (a key must be a config
    default and its value must coerce), and all of it lands in the arm specification -- so the
    declared money is written into the manifest before the first step and a later re-run declaring
    different values is refused instead of silently measuring something else.
    """
    declared: dict[str, Any] = {
        "optimizer.feed_in_wiring": args.feed_in_wiring,
        "price.feed_in_mode": args.feed_in_mode,
    }
    for item in args.setting or ():
        key, separator, raw = str(item).partition("=")
        key = key.strip()
        if not separator or not key:
            raise ManifestError(MANIFEST_INVALID, f"--setting needs KEY=VALUE, got {item!r}")
        if key in declared:
            raise ManifestError(
                MANIFEST_INVALID, f"{key} is already declared by a flag; declare it once"
            )
        declared[key] = raw.strip()
    return declared


def _manifest(args: argparse.Namespace, episode: Any) -> Any:
    """Load the stored contract, or freeze and write it before the first step."""
    path = None if args.episode is None else Path(args.episode).expanduser()
    declared = _declared_settings(args) if str(args.arm).upper() == "E2" else None
    if declared is None and args.setting:
        raise ManifestError(
            MANIFEST_INVALID,
            "--setting declares E2's contract model; the other arms compare the recording with "
            "itself and have no from/to pair to declare",
        )
    if path is not None and path.is_file():
        if args.verbose:
            print(f"validating {path} against the frozen state", file=sys.stderr)
        manifest = load_manifest(path, episode=episode, arms=[args.arm], contract=declared)
        _check_requested_counts(args, manifest)
        _check_expected_digest(args, manifest)
        return manifest
    contract: Mapping[str, Any] | None = declared
    manifest = build_manifest(
        episode,
        arms=[args.arm],
        seed=0 if args.seed is None else args.seed,
        blocks=4 if args.blocks is None else args.blocks,
        resamples=2000 if args.resamples is None else args.resamples,
        contract=contract,
    )
    _check_expected_digest(args, manifest)
    if path is not None:
        manifest.write(path)
        if args.verbose:
            print(f"wrote {path}", file=sys.stderr)
    return manifest


def _refusal(args: argparse.Namespace, error: BaseException) -> dict[str, Any]:
    """A refusal is a result too: the report names the slug instead of implying a clean run."""
    reason = str(getattr(error, "reason", "error") or "error")
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": to_iso(utcnow()),
        "run": {
            "arm": str(getattr(args, "arm", "")),
            "window": {
                "start": to_iso(getattr(args, "start", None)),
                "end": to_iso(getattr(args, "end", None)),
                "step_s": getattr(args, "step", None),
                "steps": 0,
            },
        },
        "refusal": {"reason": reason, "kind": type(error).__name__, "message": str(error)},
        "limits": {
            "status": "refused",
            "note": "no leg ran, so nothing is measured and nothing is claimed",
        },
    }


if __name__ == "__main__":  # pragma: no cover - the console entry point
    raise SystemExit(main(sys.argv[1:]))
