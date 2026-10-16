# C core (`app/optimize/_core.c`) — runtime verification contract

Short note for reviewers; `_fast.py`'s module docstring is the authoritative description and
`docs/OPTIMIZER_V2.md` section 6 states the same contract.

The core is a drop-in accelerator for `dp._solve_once`: same state grid, powers, energies,
objective, iterations, effective step and terminal error as the Python reference
(`dp._solve_once_numeric`). `_fast.py` decides whether a returned result may become a plan.

**Every solve** (Python side, before the result is returned):

1. structure, lengths, finiteness (including **every entry of the value-function buffer**) and the
   returned state grid compared against an independently rebuilt reference grid (same step and
   values); non-finite operands and accumulators fail closed *before* any tolerance comparison, so
   a `NaN` can never slip through `abs(a - b) > tol`;
2. feasibility: every returned power inside the SoC/grid bounds derived with the reference
   helpers, every returned window equal to the reference window, every *unclamped* transition
   (including the terminal one) inside `[e_min, e_max]`;
3. self-consistency: the objective and terminal error re-derived from the reconstructed path, the
   energy trajectory recomputed slot by slot, and the reported cycle cost compared against the
   wear accumulated independently over the returned path;
4. decision consistency: for every slot the returned power **equals** the reference's own choice -
   the same action set, the same strict argmin (ties resolve to the lowest power), the same
   grid-snapped stored decision and the same reconstruction clamp. A feasible power outside the
   action set, or a tied-but-higher action, is rejected even though its cost ties;
5. backward consistency: the terminal value row is recomputed in full and a sample of `V_i[k]`
   entries is recomputed from `V_next`, with non-finite results failing closed.

**Periodically** (full equivalence): on the first solve after load and every `_CHECK_INTERVAL`
solves (default 20; `_fast.set_check_interval(n)`, 0 = off) the unchanged Python reference runs
and powers, energies, objective, iterations and step are compared. Any mismatch disables the core
for the process permanently.

**Bound:** between cross-checks the guarantee is feasibility + strict decision consistency +
self-consistency + sampled backward consistency (whole value buffer finite), not a full
re-derivation of the value function; the periodic full differential bounds the residual risk, and
every layer above falls back to the Python reference rather than shipping an unverified number.

The core also refuses memory-unsafe or numerically pathological inputs itself (dimension caps,
checked size arithmetic, explicit rejection of non-finite spans/ratios/efficiencies before any
integer cast, input sequences snapshotted to an immutable tuple before conversion) so the loader's
verification is never the only thing standing between a bad call and a corrupted process.

## Build identity (why the loader can refuse a stale artifact)

The image compiles the extension in place and removes any carried-over `.so` first; a development
checkout does not, so a `.so` built before the last `_core.c` edit used to load silently and then
either fail verification (disabling the accelerator for the process) or, worse, keep producing
numbers that pass while the source had moved on. The build therefore embeds the SHA-256 prefix of
the source it compiled, and `_fast._load_core()` refuses a build whose digest no longer matches the
adjacent `_core.c` (one warning, Python reference stays active):

```sh
cd energy_manager/app/optimize && \
gcc -O3 -ffp-contract=off -fPIC -shared \
    -DCORE_BUILD_SHA="\"$(sha256sum _core.c | cut -c1-16)\"" \
    $(python3-config --includes) \
    -o "_core$(python3-config --extension-suffix)" _core.c
```

A manual `gcc` line that omits `-DCORE_BUILD_SHA` falls back to comparing mtimes, which still
catches "edited the source, forgot to rebuild". The guard is deliberately one-sided: a missing
`_core.c`, an unreadable file or an absent `__file__` (zipapp, frozen bundle, a packaged build
that ships only the `.so`) is treated as "cannot check" and keeps the core active, because the
accelerator is optional and disabling it is a performance loss. It cannot cover import-time
hazards at all -- the import has already succeeded by the time the digest is readable -- only a
build that no longer matches its source. `-ffp-contract=off` remains mandatory: without it GCC's
FMA contraction changes the last bits and flips near-tie argmins, which the differential suite
detects.
