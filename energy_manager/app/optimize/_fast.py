"""Validated loader for the optional CPython core in ``app/optimize/_core.c`` (SPEC 8.2,
docs/OPTIMIZER_V2.md section 6).

The extension is a pure accelerator and its result is verified before a plan can use it.  The
runtime contract, stated honestly:

**Every solve** (cheap, Python-side, no re-derivation of the full value function):

* structure, lengths and finiteness of every number;
* the state grid is rebuilt independently from ``agg``/``opts`` and must match the returned grid;
* every returned power lies inside the SoC/grid power bounds derived with the Python reference
  helpers, and the returned windows equal the reference windows;
* the objective and terminal error are re-derived from the reconstructed path, and every
  *unclamped* transition -- including the terminal one -- stays inside ``[e_min, e_max]``;
* for every slot, the returned power **equals** the reference's one-step choice: the same action
  set, the same strict argmin (ties resolve to the lowest power, exactly as in
  ``dp._solve_once_numeric``), the same grid-snapped stored decision and the same reconstruction
  clamp -- an off-action power or a tied-but-higher power is rejected even though its cost ties;
* the whole value-function buffer must be finite, non-finite operands and accumulators fail closed
  before any tolerance comparison, and the last slot's terminal values and a sample of ``V_i[k]``
  entries are recomputed from ``V_next`` (backward Bellman) and compared;
* when an optimizer v2 stage-3 continuation row is supplied, its structure, length and finiteness
  are checked, the returned terminal row must equal it entry for entry (never a substituted
  salvage row), the reported objective must interpolate that row -- no salvage and no second
  target penalty -- and the *whole* last slot's row, not a sample, must be the reference one-step
  Bellman value over it, so the sweep provably consumed the row at the boundary;
* the reported cycle cost equals the wear accumulated independently over the returned path.

**Periodically** (full equivalence with the unchanged Python reference): on the first solve after
load and every ``_CHECK_INTERVAL`` solves (default 20, ``0`` = off), ``dp._solve_once_numeric`` is
run -- with the supplied row forwarded verbatim -- and powers, energies, objective, iterations and
step are compared; any mismatch disables the core permanently.  A solve that falls through to the
reference because the core *declined* (an unusable row, before the call, or a rejected result)
is a fallback, not a mismatch.

The bound this implies, stated as a limit and not hidden behind the list above: between
cross-checks the guarantee is *feasibility + one-step decision consistency + sampled backward
consistency*, never *optimality of the returned path*.  An incorrect value at an **unsampled
interior cell** -- a row and a state outside the Bellman sample, and, with a stage-3 row, outside
the fully checked last slot -- is **not** detected by the per-solve checks: the one-step check
resolves that action against the same returned ``V_next``, so a wrong cell that only moves a
decision the path takes still looks self-consistent, and the objective is then recomputed from
the (wrong) path it produced.  Detection of that case belongs to the periodic full differential,
which compares the resulting powers/objective against the Python reference -- and it therefore
does not retroactively validate the solves in between.  The row-mode last-slot check, the
symbolic grid and the sampled Bellman check establish the *boundary* the sweep consumes; they do
not turn a bad interior value into a caught one.  Any failure at any point disables the
extension for the process (one warning) and :func:`solve` returns ``None`` so the Python reference
runs.  The checks bound the risk rather than prove correctness: a wrong extension result reaches a
plan only while every per-solve check above happens to miss it -- interior optimality is exactly
that gap -- and the periodic full differential then catches the divergence after the fact, never
retroactively for the solves in between.

No third-party dependency: the extension is imported by name and everything else is the standard
library.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

#: Hex chars of the source digest the build compiles in with ``-DCORE_BUILD_SHA=``.
_BUILD_SHA_LEN = 16


def _core_source_path() -> str:
    """The ``_core.c`` this loader sits next to (the image ships it; the extension is built from it)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_core.c")


def _digest(path: str, length: int = _BUILD_SHA_LEN) -> str | None:
    """First ``length`` hex chars of the file's SHA-256, or ``None`` when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:length]
    except OSError:
        return None


def stale_reason(
    module_file: str | None, source: str | None = None, build_sha: str = ""
) -> str | None:
    """Why a built extension does not match its source, or ``None`` when it may be used.

    Identity first: the build embeds the SHA-256 prefix of the ``_core.c`` it compiled
    (``-DCORE_BUILD_SHA``), so an artifact that was copied, restored from an archive or merely
    ``touch``ed cannot pass as a build *of the current source* -- the digest covers the source, not
    the compiler flags, which are part of the documented build recipe rather than of the identity.
    A manual gcc line without that define falls back to comparing mtimes, which catches the common
    "edited the source, forgot to rebuild" case.

    A missing source, an unreadable file or an absent ``__file__`` stay permissive: this guard
    exists to catch a development checkout that forgot to rebuild, never to disable a correctly
    built accelerator.  It cannot cover import-time hazards at all -- the import has already
    happened by the time the digest can be read -- only a build that no longer matches its source.
    """
    source_path = source or _core_source_path()
    if not os.path.exists(source_path):
        log.info(
            "optimizer: %s is not present; the C core build cannot be matched against its source",
            os.path.basename(source_path),
        )
        return None
    if build_sha:
        actual = _digest(source_path, len(build_sha))
        if actual is None:
            log.info(
                "optimizer: %s is unreadable; the C core build cannot be verified against it",
                os.path.basename(source_path),
            )
            return None
        if actual != build_sha:
            return (
                f"{os.path.basename(module_file or 'the C core')} was built from different "
                f"{os.path.basename(source_path)} source (build {build_sha[:8]}, source "
                f"{actual[:8]}); rebuild it (see the Dockerfile) or remove it"
            )
        return None
    if not module_file:
        log.info("optimizer: the C core module exposes no __file__; its build cannot be verified")
        return None
    try:
        if os.path.getmtime(module_file) < os.path.getmtime(source_path):
            return (
                f"{os.path.basename(module_file)} is older than {os.path.basename(source_path)}; "
                "rebuild it (see the Dockerfile) or remove it"
            )
    except OSError:
        log.info(
            "optimizer: %s or %s is unreadable; the C core build cannot be verified",
            os.path.basename(module_file),
            os.path.basename(source_path),
        )
        return None
    return None


def _load_core() -> tuple[Any | None, str | None]:
    """Import the optional extension, refusing a build that no longer matches its source."""
    try:
        from . import _core as module
    except Exception as exc:  # noqa: BLE001 - any import failure simply means "no accelerator"
        return None, str(exc)
    reason = stale_reason(
        getattr(module, "__file__", None), build_sha=str(getattr(module, "build_sha", "") or "")
    )
    if reason is not None:
        log.warning("optimizer: ignoring the C core (%s); the Python reference stays active", reason)
        return None, reason
    return module, None


_core, _import_error = _load_core()

#: Full differential cross-check cadence (solves).  First solve after load always checks; 0 = off.
_DEFAULT_CHECK_INTERVAL = 20
_CHECK_INTERVAL = _DEFAULT_CHECK_INTERVAL

#: Set for the rest of the process once the core fails a call or any verification.
_disabled = False
#: Why it was disabled, kept so a deploy can be told rather than only a log line.
_disabled_reason: str | None = None
_warned = False
_solve_count = 0
_checks_done = 0

_ENERGY_TOL = 1e-9
#: Slack for the feasible-bounds check on a returned power, EUR/kW.
_DECISION_ABS_TOL = 1e-9
#: Objective tolerance when re-deriving the reported value from the returned path.
_OBJECTIVE_ABS_TOL = 1e-6
_OBJECTIVE_REL_TOL = 1e-9
#: Value-function / window comparison tolerance.
_VALUE_ABS_TOL = 1e-9
_VALUE_REL_TOL = 1e-12
#: Cross-check comparison tolerances.
_CROSS_ENERGY_TOL = 1e-9
_CROSS_ABS_TOL = 1e-9
_CROSS_REL_TOL = 1e-12


@dataclass(frozen=True)
class CoreResult:
    """One validated numeric solve: mirrors ``dp._NumericResult`` attribute for attribute."""

    iterations: int
    effective_step_kwh: float
    terminal_error_kwh: float
    cycle_cost_eur: float
    objective_eur: float
    internal_objective_eur: float
    states: int
    energies: list[float]
    powers: list[float]
    windows: list[tuple[float, float, float, float]]
    grid: list[float]
    value: memoryview


def available() -> bool:
    """True while the accelerator may still be used (built, imported and not disabled)."""
    return _core is not None and not _disabled


def status() -> dict[str, Any]:
    """Additive (0.27.0): which numeric backend is live, so a deploy can be verified.

    ``backend`` is ``"c"`` only while the extension is loaded, not stale and not disabled; it is
    ``"python"`` whenever the reference is what actually solves, which is the safe state and also
    what a plain checkout without a compiled extension reports.  ``build_sha`` is the digest the
    loaded extension was compiled from (``None`` when there is no extension), and ``reason`` says
    why the accelerator is unavailable, including a stale build or a verification failure -- a
    deploy that ships an accelerator it cannot prove is live is exactly the gap this closes.
    """
    if available():
        return {"backend": "c", "build_sha": _build_sha() or None, "reason": None}
    if _core is None:
        reason = _import_error or "the C core is not built"
    else:
        reason = _disabled_reason or "disabled"
    return {"backend": "python", "build_sha": None, "reason": str(reason)}


def _build_sha() -> str | None:
    """The digest the loaded extension was compiled from, when it exposes one."""
    value = getattr(_core, "build_sha", None)
    return str(value) if value else None


def set_check_interval(interval: int) -> None:
    """Set the full differential cross-check cadence (0 disables it) and reset the counters."""
    global _CHECK_INTERVAL, _solve_count, _checks_done
    _CHECK_INTERVAL = max(0, int(interval))
    _solve_count = 0
    _checks_done = 0


def reset() -> None:
    """Re-enable the accelerator after a failure (tests only)."""
    global _disabled, _warned, _disabled_reason, _solve_count, _checks_done
    _disabled = False
    _warned = False
    _disabled_reason = None
    _solve_count = 0
    _checks_done = 0


def disable(reason: str) -> None:
    """Disable the accelerator for the rest of the process, warning once."""
    global _disabled, _warned, _disabled_reason
    _disabled = True
    if _disabled_reason is None:
        _disabled_reason = str(reason)
    if not _warned:
        _warned = True
        log.warning("optimizer: C core disabled (%s); falling back to the Python reference", reason)


def _config(
    dp: Any,
    agg: Any,
    opts: Any,
    start_kwh: float,
    e_target: float | None,
    salvage: float,
    target_penalty: float,
) -> dict[str, Any]:
    return {
        "e_min_kwh": agg.e_min_kwh,
        "e_max_kwh": agg.e_max_kwh,
        "p_charge_kw": agg.p_charge_kw,
        "p_discharge_kw": agg.p_discharge_kw,
        "eta_charge": agg.eta_charge,
        "eta_discharge": agg.eta_discharge,
        "start_kwh": start_kwh,
        "e_target": e_target,
        "slot_minutes": opts.slot_minutes,
        "soc_step_kwh": opts.soc_step_kwh,
        "power_steps": int(opts.power_steps),
        "allow_grid_charge": bool(opts.allow_grid_charge),
        "allow_export": bool(opts.allow_export),
        "refuse_battery_export": bool(opts.refuse_battery_export),
        "max_import_kw": opts.max_import_kw,
        "max_export_kw": opts.max_export_kw,
        "min_spread_eur_per_kwh": opts.min_spread_eur_per_kwh,
        "sell_floor_eur_per_kwh": opts.sell_floor_eur_per_kwh,
        "pv_charge_preference": opts.pv_charge_preference,
        "wear_eur_per_kwh": opts.wear_eur_per_kwh,
        "salvage": salvage,
        "target_penalty": target_penalty,
        "max_states": dp.MAX_STATES,
        "max_iterations": dp.MAX_ITERATIONS,
    }


def _sample_indices(total: int, want: int) -> list[int]:
    if total <= 0:
        return []
    if total <= want:
        return list(range(total))
    return sorted({int(round(i * (total - 1) / (want - 1))) for i in range(want)})


def _tol(base: float, rel: float, scale: float) -> float:
    return base + rel * abs(scale)


def _apply_terminal_penalty(objective: float, penalty: float, e_now: float, e_target: float) -> float:
    """Add the terminal penalty, failing closed on a non-finite sum.

    The sum has to be checked *after* the addition: the comparison tolerance is derived from the
    recomputed objective, so an ``inf`` here would make every tolerance infinite, and
    ``abs(x - y) > inf`` is False -- an overflow would validate itself.
    """
    error = abs(e_now - e_target)
    total = objective + penalty * error
    if not (math.isfinite(total) and math.isfinite(error)):
        raise ValueError("objective accumulation is not finite")
    return total


def _reference_grid(dp: Any, slots: Sequence[Any], agg: Any, opts: Any, start_kwh: float,
                    e_target: float | None) -> tuple[list[float], float]:
    """Rebuild the state grid with the unchanged reference helpers (SPEC 8.2)."""
    anchors = [dp.clamp(start_kwh, agg.e_min_kwh, agg.e_max_kwh)]
    if e_target is not None:
        anchors.append(e_target)
    grid, step = dp._build_grid(agg, opts.soc_step_kwh, anchors)
    action_count = max(2, int(opts.power_steps) + 1)
    budget = max(2, dp.MAX_ITERATIONS // max(1, len(slots) * action_count))
    if len(grid) > budget:
        span = max(0.0, agg.e_max_kwh - agg.e_min_kwh)
        grid, step = dp._build_grid(agg, max(step, span / max(1, budget - 1)), anchors)
    return grid, step


def _reference_bounds(dp: Any, slot: Any, agg: Any, opts: Any, e: float, dt: float) -> tuple[float, float]:
    """``power_bounds_kw`` re-derived with the reference helpers (SPEC 8.2)."""
    soc_lo, soc_hi = dp.soc_window_kw(e, agg, dt)
    grid_lo, grid_hi = dp.grid_window_kw(slot, agg, opts)
    lo = soc_lo if soc_lo > grid_lo else grid_lo
    hi = soc_hi if soc_hi < grid_hi else grid_hi
    if lo > hi:
        middle = (grid_lo + grid_hi) / 2.0
        lo = hi = dp.clamp(middle, soc_lo, soc_hi)
    if hi < lo:
        lo = hi = dp.clamp(0.0, soc_lo, soc_hi)
    return lo, hi


#: Returned by :func:`_prepare_terminal_row` when a caller-supplied row cannot be used.
_ROW_UNUSABLE = object()


def _decline_terminal_row(reason: str) -> Any:
    """Log a declined row once and return the sentinel; the extension is not called."""
    log.warning("optimizer: %s; the Python reference solves this scenario", reason)
    return _ROW_UNUSABLE


def _prepare_terminal_row(
    dp: Any,
    slots: Sequence[Any],
    agg: Any,
    opts: Any,
    start_kwh: float,
    e_target: float | None,
    terminal_row: Sequence[float],
) -> Any:
    """The stage-3 row as plain floats, or :data:`_ROW_UNUSABLE` when it cannot be interpreted.

    This guard runs *before* the extension is called, so a row the core could not use is a
    **decline**, not a core fault: the Python reference then solves the scenario exactly as it did
    before this path was accelerated, and the accelerator stays available for later solves.  The
    alternative -- letting the extension raise and treating that as a broken build -- would
    disable the accelerator for the rest of the process because one caller passed a bad row.  The
    reference is never *worse* than an accelerated run here: it consumes the same row (it may
    itself produce non-finite numbers for a non-finite row, which is the caller's data, not the
    accelerator's behaviour), and the plan is the same one the pre-acceleration code produced.

    The row is checked against the grid the core will rebuild (``_reference_grid``) because the
    row and the DP must be aligned index by index; a mismatch is refused rather than guessed.
    """
    try:
        row = [float(value) for value in terminal_row]
    except (TypeError, ValueError, OverflowError) as exc:
        return _decline_terminal_row(f"the terminal row is not a sequence of floats ({exc})")
    if not all(map(math.isfinite, row)):
        return _decline_terminal_row("the terminal row has non-finite entries")
    grid, _step = _reference_grid(dp, slots, agg, opts, start_kwh, e_target)
    if len(row) != len(grid):
        return _decline_terminal_row(
            f"the terminal row has {len(row)} entries for a {len(grid)}-state grid"
        )
    return row


def _validated(
    raw: Any,
    slots: Sequence[Any],
    agg: Any,
    opts: Any,
    start_kwh: float,
    e_target: float | None,
    salvage: float,
    target_penalty: float,
    minutes: Sequence[float],
    dp: Any,
    terminal_row: Sequence[float] | None = None,
) -> CoreResult:
    """Verify every number and the decision itself; raise ``ValueError`` on any doubt.

    ``terminal_row`` is the stage-3 continuation row handed to the extension (``None`` for the
    salvage model).  With a row the checks that describe the *salvage* terminal model are replaced
    by their row equivalents: the returned terminal row must equal the supplied row entry for
    entry, the objective's terminal term is the interpolation of that row, and the whole last
    slot's row is recomputed as the reference one-step Bellman value over it.
    """
    n = len(slots)
    if not isinstance(raw, dict):
        raise ValueError("result is not a dict")
    try:
        energies = [float(value) for value in raw["energies"]]
        powers = [float(value) for value in raw["powers"]]
        windows_flat = [float(value) for value in raw["windows"]]
        grid_returned = [float(value) for value in raw["grid"]]
        value_buffer = raw["value"]
        iterations = int(raw["iterations"])
        step = float(raw["effective_step_kwh"])
        terminal = float(raw["terminal_error_kwh"])
        cycle = float(raw["cycle_cost_eur"])
        objective = float(raw["objective_eur"])
        internal = float(raw["internal_objective_eur"])
        states = int(raw["states"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"malformed result: {exc}") from exc
    if not isinstance(value_buffer, (bytes, bytearray, memoryview)):
        raise ValueError("value function is not a buffer")
    if len(energies) != n or len(powers) != n or len(windows_flat) != 4 * n:
        raise ValueError("result lengths do not match the scenario")
    if iterations < 0 or states < 1 or len(grid_returned) != states:
        raise ValueError("result state count does not match the returned grid")
    for value in (step, terminal, cycle, objective, internal, start_kwh, salvage, target_penalty):
        if not math.isfinite(value):
            raise ValueError("non-finite scalar in result")
    for value in energies + powers + windows_flat + grid_returned + [float(item) for item in minutes]:
        if not math.isfinite(value):
            raise ValueError("non-finite array value in result")

    # The grid must be exactly what the reference builds, so every window/interp below is on the
    # same discretisation the core used.
    grid, reference_step = _reference_grid(dp, slots, agg, opts, start_kwh, e_target)
    if len(grid) != states or step != reference_step:
        raise ValueError("state grid does not match the reference grid")
    for got, expected in zip(grid_returned, grid):
        if abs(got - expected) > _tol(_VALUE_ABS_TOL, _VALUE_REL_TOL, expected):
            raise ValueError("state grid values do not match the reference grid")

    if len(value_buffer) != (n + 1) * states * 8:
        raise ValueError("value function buffer has the wrong size")
    try:
        value = memoryview(value_buffer).cast("d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value function buffer is unusable: {exc}") from exc
    if len(value) != (n + 1) * states:
        raise ValueError("value function buffer has the wrong length")
    if not all(map(math.isfinite, value)):
        raise ValueError("value function buffer contains non-finite entries")

    # Terminal row: with a stage-3 row the core must have seeded ``value[n]`` with exactly that
    # row, entry for entry; without one the row is recomputed from the reference terminal value.
    # Non-finite operands fail closed: ``abs(nan - expected) > tol`` is False, so ``nan`` would
    # otherwise evade the check.  The objective below interpolates this same ``row`` object.
    row: list[float] = []
    if terminal_row is not None:
        row = [float(entry) for entry in terminal_row]
        if len(row) != states:
            raise ValueError("terminal row length does not match the state grid")
        for expected in row:
            if not math.isfinite(expected):
                raise ValueError("terminal row is not finite")
    for k in range(states):
        got = value[n * states + k]
        if terminal_row is not None:
            expected = row[k]
            # The core copies the row into the value buffer verbatim (no arithmetic at all), so a
            # correct build reproduces every entry bit for bit; this is an equality check, not a
            # tolerance check -- a one-ulp change is exactly the kind of silent substitution the
            # row contract exists to catch and must not slip under ``_VALUE_ABS_TOL``.
            if got != expected:
                raise ValueError("terminal value row does not match the supplied row")
            continue
        expected = -salvage * grid[k]
        if e_target is not None:
            expected += target_penalty * abs(grid[k] - e_target)
        if not (math.isfinite(got) and math.isfinite(expected)):
            raise ValueError("terminal value row is not finite")
        if abs(got - expected) > _tol(_VALUE_ABS_TOL, _VALUE_REL_TOL, expected):
            raise ValueError("terminal value row does not match the reference")

    e_min = agg.e_min_kwh
    e_max = agg.e_max_kwh
    tol_kwh = _ENERGY_TOL + _VALUE_REL_TOL * max(1.0, abs(e_max))
    actions_fn = dp._actions
    next_energy = dp.next_energy_kwh
    slot_cost = dp.slot_cost_eur
    wear_cost = dp.wear_cost_eur
    interp = dp._interp
    clamp = dp.clamp

    e_now = clamp(start_kwh, e_min, e_max)
    objective_recomputed = 0.0
    wear_total = 0.0

    def one_step(action: float, state: float, slot: Any, dt: float, next_row: Any) -> tuple[float, float, float]:
        """Reference one-step cost of ``action``: (slot + wear + future, slot cost, wear cost)."""
        e_next = next_energy(state, action, dt, agg)
        if not math.isfinite(e_next):
            raise ValueError("non-finite transition energy in the action set")
        step = slot_cost(action, slot, dt, opts)
        wear = wear_cost(opts.wear_eur_per_kwh, agg.eta_discharge, state, e_next)
        future = interp(next_row, grid, e_next)
        if not (math.isfinite(step) and math.isfinite(wear) and math.isfinite(future)):
            raise ValueError("non-finite one-step operand in the action set")
        total = step + wear + future
        if not math.isfinite(total):
            raise ValueError("non-finite one-step cost in the action set")
        return total, step, wear

    for i in range(n):
        dt = minutes[i] / 60.0
        slot = slots[i]
        if abs(energies[i] - e_now) > tol_kwh:
            raise ValueError("energy trajectory is not self-consistent")
        soc_lo, soc_hi = dp.soc_window_kw(e_now, agg, dt)
        grid_lo, grid_hi = dp.grid_window_kw(slot, agg, opts)
        lo, hi = _reference_bounds(dp, slot, agg, opts, e_now, dt)
        for got, expected in zip(
            windows_flat[4 * i : 4 * i + 4], (soc_lo, soc_hi, grid_lo, grid_hi)
        ):
            if abs(got - expected) > _tol(_VALUE_ABS_TOL, _VALUE_REL_TOL, expected):
                raise ValueError("window does not match the reference window")
        power = powers[i]
        if power < lo - _DECISION_ABS_TOL or power > hi + _DECISION_ABS_TOL:
            raise ValueError("power is outside the feasible bounds")
        # ``best_at`` uses the stored decision whenever the state snaps to a grid point; mirroring
        # that snap makes the recomputed argmin bit-identical to the one the sweep stored.  The
        # stored decision was chosen with the *grid point's* bounds, while the reconstruction clamp
        # below uses this slot's ``e_now`` bounds -- both are reproduced here.
        state = e_now
        action_lo, action_hi = lo, hi
        for point in grid:
            if abs(point - e_now) <= _ENERGY_TOL:
                state = point
                action_lo, action_hi = _reference_bounds(dp, slot, agg, opts, state, dt)
                break
        if i == n - 1 and e_target is not None:
            extra = dp.exact_power_kw(state, e_target, dt, agg)
        else:
            extra = None
        actions = actions_fn(action_lo, action_hi, opts.power_steps, extra)
        next_row = value[(i + 1) * states : (i + 2) * states]
        best_cost = math.inf
        best_power = None
        for action in actions:
            total, _, _ = one_step(action, state, slot, dt, next_row)
            if total < best_cost:  # strict, like the reference: ties keep the lowest power
                best_cost = total
                best_power = action
        if best_power is None or not math.isfinite(best_cost):
            raise ValueError("the reference action set is empty or non-finite")
        expected_power = clamp(best_power, lo, hi)
        if power != expected_power:
            raise ValueError("returned power is not the reference one-step argmin")
        e_next_p = next_energy(e_now, power, dt, agg)
        if not math.isfinite(e_next_p):
            raise ValueError("non-finite transition from the returned power")
        step_cost = slot_cost(power, slot, dt, opts)
        wear = wear_cost(opts.wear_eur_per_kwh, agg.eta_discharge, e_now, e_next_p)
        future = interp(next_row, grid, e_next_p)
        if not (math.isfinite(step_cost) and math.isfinite(wear) and math.isfinite(future)):
            raise ValueError("non-finite cost operand for the returned power")
        if not math.isfinite(step_cost + wear + future):
            raise ValueError("non-finite cost of the returned power")
        objective_recomputed += step_cost
        wear_total += wear
        if not (math.isfinite(objective_recomputed) and math.isfinite(wear_total)):
            raise ValueError("objective accumulation is not finite")
        if e_next_p < e_min - tol_kwh or e_next_p > e_max + tol_kwh:
            raise ValueError("transition leaves [e_min, e_max]")
        e_now = clamp(e_next_p, e_min, e_max)
    if terminal_row is not None:
        # Stage 3: the row *is* the continuation value and already carries the target penalty
        # (``dp._row_with_penalty`` baked it in before the call), so the terminal term is the same
        # interpolated row the sweep's last slot used -- no salvage, no second penalty.  The
        # grouping mirrors ``dp._solve_once_numeric``'s explicit-row branch operation for operation.
        objective_recomputed += wear_total + interp(row, grid, e_now)
    else:
        objective_recomputed += wear_total - salvage * e_now
    if not math.isfinite(objective_recomputed):
        raise ValueError("objective accumulation is not finite")
    if abs(cycle - wear_total) > _tol(_OBJECTIVE_ABS_TOL, _OBJECTIVE_REL_TOL, wear_total):
        raise ValueError("cycle cost does not match the accumulated wear")
    expected_terminal = 0.0
    if e_target is not None:
        expected_terminal = abs(e_now - e_target)
        if terminal_row is None:
            objective_recomputed = _apply_terminal_penalty(
                objective_recomputed, target_penalty, e_now, e_target
            )
    tolerance = _OBJECTIVE_ABS_TOL + _OBJECTIVE_REL_TOL * abs(objective_recomputed)
    if abs(objective_recomputed - objective) > tolerance:
        raise ValueError("objective does not match the reconstructed path")
    if abs(expected_terminal - terminal) > tolerance:
        raise ValueError("terminal error does not match the reconstructed path")

    # Sampled backward consistency: V_i[k] must be the reference one-step Bellman value of V_next.
    # With a stage-3 row the last slot's row is the whole point of the solve -- it is the only place
    # the supplied row binds the sweep -- so every state of it is recomputed, not a sample; the
    # rest of the value function keeps the sample (the periodic full differential bounds the tail).
    for i in _sample_indices(n, 5):
        dt = minutes[i] / 60.0
        slot = slots[i]
        next_row = value[(i + 1) * states : (i + 2) * states]
        states_to_check = (
            range(states)
            if terminal_row is not None and i == n - 1
            else _sample_indices(states, 5)
        )
        for k in states_to_check:
            e = grid[k]
            bounds_lo, bounds_hi = _reference_bounds(dp, slot, agg, opts, e, dt)
            if i == n - 1 and e_target is not None:
                extra = dp.exact_power_kw(e, e_target, dt, agg)
            else:
                extra = None
            best_cost = math.inf
            for action in actions_fn(bounds_lo, bounds_hi, opts.power_steps, extra):
                total, _, _ = one_step(action, e, slot, dt, next_row)
                if total < best_cost:
                    best_cost = total
            got = value[i * states + k]
            if not (math.isfinite(got) and math.isfinite(best_cost)):
                raise ValueError("value function is not finite in the sampled region")
            if abs(got - best_cost) > _tol(_VALUE_ABS_TOL, _VALUE_REL_TOL, best_cost):
                raise ValueError("value function fails backward consistency")

    windows = [tuple(windows_flat[4 * i : 4 * i + 4]) for i in range(n)]
    return CoreResult(
        iterations=iterations,
        effective_step_kwh=step,
        terminal_error_kwh=terminal,
        cycle_cost_eur=cycle,
        objective_eur=objective,
        internal_objective_eur=internal,
        states=states,
        energies=energies,
        powers=powers,
        windows=windows,
        grid=grid,
        value=value,
    )


def _differential_mismatch(
    dp: Any,
    slots: Sequence[Any],
    agg: Any,
    start_kwh: float,
    opts: Any,
    target_penalty: float,
    e_target: float | None,
    salvage: float,
    minutes: Sequence[float],
    result: CoreResult,
    terminal_row: Sequence[float] | None = None,
) -> str | None:
    """Run the unchanged Python reference and report the first divergence, or ``None``.

    ``terminal_row`` is forwarded verbatim: without it the oracle would solve the salvage model
    and every row solve would falsely report a mismatch (and then disable the core).
    """
    try:
        reference = dp._solve_once_numeric(
            slots,
            agg,
            start_kwh,
            opts,
            target_penalty,
            e_target,
            salvage,
            minutes,
            terminal_row=terminal_row,
        )
    except Exception as exc:  # noqa: BLE001 - the oracle must never fail where the core succeeded
        return f"reference raised {type(exc).__name__}: {exc}"
    if reference.iterations != result.iterations:
        return f"iterations {result.iterations} != {reference.iterations}"
    if abs(reference.effective_step_kwh - result.effective_step_kwh) > _tol(
        _CROSS_ABS_TOL, _CROSS_REL_TOL, reference.effective_step_kwh
    ):
        return "effective step differs from the reference"
    for name, fast_value, ref_value in (
        ("objective", result.objective_eur, reference.objective_eur),
        ("cycle cost", result.cycle_cost_eur, reference.cycle_cost_eur),
        ("terminal error", result.terminal_error_kwh, reference.terminal_error_kwh),
    ):
        if abs(fast_value - ref_value) > _tol(_CROSS_ABS_TOL, _CROSS_REL_TOL, ref_value):
            return f"{name} differs from the reference"
    for i, (fast_power, ref_power) in enumerate(zip(result.powers, reference.powers)):
        if abs(fast_power - ref_power) > _CROSS_ENERGY_TOL:
            return f"power[{i}] differs from the reference"
    for i, (fast_energy, ref_energy) in enumerate(zip(result.energies, reference.energies)):
        if abs(fast_energy - ref_energy) > _CROSS_ENERGY_TOL:
            return f"energy[{i}] differs from the reference"
    return None


def solve(
    slots: Sequence[Any],
    agg: Any,
    start_kwh: float,
    opts: Any,
    target_penalty: float,
    e_target: float | None,
    salvage: float,
    minutes: Sequence[float],
    terminal_row: Sequence[float] | None = None,
) -> CoreResult | None:
    """Run the C core, verify it, or return ``None`` (the Python reference then runs).

    ``terminal_row`` is the optimizer v2 stage 3 continuation-value row over the state grid
    (docs/OPTIMIZER_V2.md section 4); the extension takes it as an optional seventh argument and
    seeds ``value[n]`` with it, and the validation in :func:`_validated` then proves the boundary
    was consumed.  A row that cannot be used at all (not a sequence of finite floats, or a length
    that does not match the grid the core will rebuild) is a **decline**: ``None`` is returned
    without touching the extension or disabling it, and the Python reference solves the scenario
    exactly as it did before this path was accelerated.  The row is only ever present while
    ``optimizer.terminal_mode = vector``, which ships default-off.
    """
    global _solve_count, _checks_done
    if not available():
        return None
    from . import dp  # local import: dp imports this module at load time

    prepared_row: list[float] | None = None
    if terminal_row is not None:
        prepared = _prepare_terminal_row(dp, slots, agg, opts, start_kwh, e_target, terminal_row)
        if prepared is _ROW_UNUSABLE:
            return None
        prepared_row = prepared

    config = _config(dp, agg, opts, start_kwh, e_target, salvage, target_penalty)
    try:
        raw = _core.solve(
            [float(slot.pv_kw) for slot in slots],
            [float(slot.load_kw) for slot in slots],
            [float(slot.buy) for slot in slots],
            [float(slot.sell or 0.0) for slot in slots],
            [float(value) for value in minutes],
            config,
            prepared_row,
        )
    except Exception as exc:  # noqa: BLE001 - a broken call is a fallback, not a plan failure
        disable(f"call raised {type(exc).__name__}: {exc}")
        return None
    try:
        result = _validated(
            raw,
            slots,
            agg,
            opts,
            start_kwh,
            e_target,
            salvage,
            target_penalty,
            minutes,
            dp,
            terminal_row=prepared_row,
        )
    except Exception as exc:  # noqa: BLE001 - reject wrong output and fall back
        disable(f"validation rejected the result: {exc}")
        return None

    _solve_count += 1
    if _CHECK_INTERVAL > 0 and (_checks_done == 0 or _solve_count % _CHECK_INTERVAL == 0):
        _checks_done += 1
        mismatch = _differential_mismatch(
            dp,
            slots,
            agg,
            start_kwh,
            opts,
            target_penalty,
            e_target,
            salvage,
            minutes,
            result,
            terminal_row=prepared_row,
        )
        if mismatch:
            disable(f"differential self-check mismatch: {mismatch}")
            return None
    return result
