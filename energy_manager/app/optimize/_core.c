/* SoC dynamic program core (SPEC 8.2 / docs/OPTIMIZER_V2.md section 6).
 *
 * One entry point: solve(pv, load, buy, sell, minutes, config) -> dict.
 *
 * A drop-in accelerator for ``app.optimize.dp._solve_once``: it reproduces the reference value
 * iteration and path reconstruction over flat double arrays, releases the GIL for the numeric
 * sweep and returns plain Python lists plus the state grid and the full value function (as bytes
 * of native doubles) so ``_fast.py`` can verify the decision and the sampled backward sweep.
 * Slot classification (``_classify``) and ``PlanSlot`` construction stay in Python.
 *
 * The sweep touches no Python object.  The Python loader (``_fast.py``) validates every result
 * before a plan may use it; a wrong answer here can never reach a plan.  This file must therefore
 * never rely on that validation for memory safety: dimensions are capped and every size
 * computation is checked, and numeric domains are validated here so no integer cast ever sees a
 * non-finite ratio.
 *
 * Floating-point note: the order of every expression mirrors ``dp.py`` and the Dockerfile builds
 * with ``-ffp-contract=off``, so the differential suite can compare at tight tolerances rather
 * than at a tolerance that hides a real divergence.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Mirror the dp.py constants; MAX_STATES / MAX_ITERATIONS arrive in the config so the Python
 * module stays the single source of truth for the iteration budget. */
#define EPS_KW 1e-6
#define EPS_KWH 1e-9
#define PV_AUTO 0
#define PV_PREFER 1
#define PV_AVOID 2

/*: Caller-supplied dimensions are capped so every size below fits (n+1)*m, 4*n and an int; a
 * larger value is rejected with ValueError before anything is narrowed.  The shipped solver uses
 * at most 400 states and a few hundred slots, so the caps are generous. */
#define CORE_MAX_DIMENSION 1000000L
#define CORE_MIN_STATES 8L

typedef struct {
    double e_min_kwh;
    double e_max_kwh;
    double p_charge_kw;
    double p_discharge_kw;
    double eta_charge;
    double eta_discharge;
    double start_kwh;
    int has_target;
    double e_target;
    double slot_minutes;
    double soc_step_kwh;
    long power_steps;
    int allow_grid_charge;
    int allow_export;
    double max_import_kw;
    double max_export_kw;
    double min_spread_eur_per_kwh;
    int has_sell_floor;
    double sell_floor_eur_per_kwh;
    int pv_preference;
    double wear_eur_per_kwh;
    /*: ``wear_eur_per_kwh * eta_discharge``, precomputed because the value is identical. */
    double wear_rate;
    double salvage;
    double target_penalty;
    long max_states;
    long max_iterations;
} Config;

/* Per-slot constants, hoisted out of the state loop. */
typedef struct {
    double dt;
    double buy;
    double sell;
    double base;    /* load - pv */
    double surplus; /* max(0, pv - load) */
    double pv_price;
    double grid_lo;
    double grid_hi;
} SlotCtx;

static double clampd(double value, double low, double high) {
    return value < low ? low : (value > high ? high : value);
}

/* Checked size arithmetic: returns -1 instead of wrapping. */
static int checked_mul(size_t a, size_t b, size_t *out) {
    if (a != 0 && b > (size_t)-1 / a) return -1;
    *out = a * b;
    return 0;
}

static void insertion_sort(double *values, int n) {
    for (int i = 1; i < n; i++) {
        double key = values[i];
        int j = i - 1;
        while (j >= 0 && values[j] > key) {
            values[j + 1] = values[j];
            j--;
        }
        values[j + 1] = key;
    }
}

/* _build_grid: e_min -> e_max by step_kwh plus the anchors, coarsened to the state budget.
 * Every write is bounded by ``capacity`` and every cast sees only finite values. */
static int build_grid(double e_min, double e_max, double step_kwh, long max_states,
                      const double *anchors, int n_anchors, double *out, int capacity,
                      int *out_n, double *out_step) {
    if (capacity < 4) {
        PyErr_SetString(PyExc_ValueError, "state grid capacity is too small");
        return -1;
    }
    double span = e_max - e_min;
    if (span < 0.0) span = 0.0;
    if (!isfinite(span)) {
        PyErr_SetString(PyExc_ValueError, "soc bounds do not produce a finite span");
        return -1;
    }
    double step = step_kwh > 1e-4 ? step_kwh : 1e-4;
    double budget = (double)(max_states - 3);
    if (budget < 1.0) budget = 1.0;
    double ratio = span / step;
    if (!(ratio <= budget - 1.0)) step = span / (budget - 1.0);
    if (!(step > 0.0) || !isfinite(step)) {
        PyErr_SetString(PyExc_ValueError, "soc step is not usable for these bounds");
        return -1;
    }
    double count_d = span / step;
    if (!(count_d >= 0.0)) count_d = 0.0;
    if (!(count_d <= (double)(capacity - 4))) {
        PyErr_SetString(PyExc_ValueError, "state grid exceeds the supported size");
        return -1;
    }
    int count = (int)count_d + 1;
    if (count < 1) count = 1;

    int m = 0;
    for (int i = 0; i < count; i++) {
        if (m >= capacity) {
            PyErr_SetString(PyExc_ValueError, "state grid exceeds the supported size");
            return -1;
        }
        out[m++] = e_min + (double)i * step;
    }
    if (m >= capacity) {
        PyErr_SetString(PyExc_ValueError, "state grid exceeds the supported size");
        return -1;
    }
    if (out[m - 1] < e_max - EPS_KWH) out[m++] = e_max;
    for (int a = 0; a < n_anchors; a++) {
        double value = clampd(anchors[a], e_min, e_max);
        int duplicate = 0;
        for (int k = 0; k < m; k++) {
            if (fabs(value - out[k]) <= 1e-6) {
                duplicate = 1;
                break;
            }
        }
        if (duplicate) continue;
        if (m >= capacity) {
            PyErr_SetString(PyExc_ValueError, "state grid exceeds the supported size");
            return -1;
        }
        out[m++] = value;
    }
    insertion_sort(out, m);
    *out_n = m;
    *out_step = step;
    return 0;
}

/* soc_window_kw: the SoC-safe [lo, hi] for one state. */
static void soc_window(const Config *c, double e, double dt, double *lo_out, double *hi_out) {
    double lo = -c->p_discharge_kw;
    double hi = c->p_charge_kw;
    if (dt > 0.0 && c->eta_charge > 0.0 && c->eta_discharge > 0.0) {
        double room = c->e_max_kwh - e;
        if (room < 0.0) room = 0.0;
        double reachable_hi = room / (dt * c->eta_charge);
        if (reachable_hi < hi) hi = reachable_hi;
        double above = e - c->e_min_kwh;
        if (above < 0.0) above = 0.0;
        double reachable_lo = (-above * c->eta_discharge) / dt;
        if (reachable_lo > lo) lo = reachable_lo;
    }
    if (lo > hi) lo = hi;
    *lo_out = lo;
    *hi_out = hi;
}

/* grid_window_kw: the grid-feasible [lo, hi] for one slot (state independent). */
static void grid_window(const Config *c, double base, double sell, double buy, double *lo_out,
                        double *hi_out) {
    double lo = -c->p_discharge_kw;
    double hi = c->p_charge_kw;
    if (c->max_import_kw > 0.0) {
        double cap = c->max_import_kw - base;
        if (cap < hi) hi = cap;
    }
    if (c->max_export_kw > 0.0) {
        double cap = -(c->max_export_kw + base);
        if (cap > lo) lo = cap;
    }
    if (!c->allow_export || !(sell > 0.0)) {
        if (-base > lo) lo = -base;
    }
    if (!c->allow_grid_charge) {
        double cap = -base;
        if (cap < 0.0) cap = 0.0;
        if (cap < hi) hi = cap;
    }
    if (c->has_sell_floor) {
        if (sell < c->sell_floor_eur_per_kwh) {
            double cap = base > 0.0 ? -base : 0.0;
            if (cap > lo) lo = cap;
        }
        if (base > 0.0 && buy < c->sell_floor_eur_per_kwh) {
            if (lo < 0.0) lo = 0.0;
        }
    }
    *lo_out = lo;
    *hi_out = hi;
}

static void make_ctx(const Config *c, SlotCtx *ctx, double pv, double load, double buy, double sell,
                     double minutes) {
    ctx->dt = minutes / 60.0;
    ctx->buy = buy;
    ctx->sell = sell;
    ctx->base = load - pv;
    ctx->surplus = pv - load;
    if (ctx->surplus < 0.0) ctx->surplus = 0.0;
    if (c->pv_preference == PV_PREFER) ctx->pv_price = 0.0;
    else if (c->pv_preference == PV_AVOID) ctx->pv_price = buy;
    else ctx->pv_price = sell;
    grid_window(c, ctx->base, sell, buy, &ctx->grid_lo, &ctx->grid_hi);
}

/* power_bounds_kw: the intersected window, plus the two halves for the caller's bookkeeping. */
static void bounds(const Config *c, const SlotCtx *ctx, double e, double dt, double *lo_out,
                   double *hi_out, double *soc_lo_out, double *soc_hi_out) {
    double soc_lo, soc_hi;
    soc_window(c, e, dt, &soc_lo, &soc_hi);
    double lo = soc_lo > ctx->grid_lo ? soc_lo : ctx->grid_lo;
    double hi = soc_hi < ctx->grid_hi ? soc_hi : ctx->grid_hi;
    if (lo > hi) {
        double middle = (ctx->grid_lo + ctx->grid_hi) / 2.0;
        lo = hi = clampd(middle, soc_lo, soc_hi);
    }
    if (hi < lo) lo = hi = clampd(0.0, soc_lo, soc_hi);
    *lo_out = lo;
    *hi_out = hi;
    *soc_lo_out = soc_lo;
    *soc_hi_out = soc_hi;
}

/* _actions: evenly spaced powers in [lo, hi], 0 always when feasible, sorted and deduped.
 * Returns the count, or -1 when the caller's capacity is too small (never a partial write). */
static int build_actions(double lo, double hi, long steps_in, int has_extra, double extra,
                         double *out, long capacity) {
    long steps = steps_in;
    if (steps < 1) steps = 1;
    if (steps + 2 > capacity) return -1;
    int n = 0;
    if (hi - lo <= EPS_KW) {
        out[n++] = clampd(0.0, lo, hi);
        return n;
    }
    if (steps == 1) {
        out[n++] = (lo + hi) / 2.0;
    } else {
        for (long j = 0; j < steps; j++) {
            out[n++] = lo + (hi - lo) * (double)j / (double)(steps - 1);
        }
    }
    out[n++] = clampd(0.0, lo, hi);
    if (has_extra && lo - EPS_KW <= extra && extra <= hi + EPS_KW) {
        out[n++] = clampd(extra, lo, hi);
    }
    insertion_sort(out, n);
    int w = 0;
    for (int k = 0; k < n; k++) {
        if (w == 0 || fabs(out[k] - out[w - 1]) > 1e-9) out[w++] = out[k];
    }
    return w;
}

/* _interp: value of the next slot's value function at the exact reachable energy.
 *
 * ``grid`` is ``e_min + i*step`` apart from at most a few appended anchor/ceiling points, so the
 * reciprocal-step estimate lands within a couple of positions of ``bisect_right`` and the two
 * adjust loops finish in O(1) instead of a binary search.  The estimate is only a hint: the loops
 * pick the exact ``bisect_right`` index.  The estimate is clamped in the double domain before the
 * cast, so a NaN/overflowing energy can never reach an undefined int conversion. */
static double interp(const double *values, const double *grid, int m, double inv_step, double e) {
    if (m == 0) return 0.0;
    if (!(e > grid[0])) return values[0];
    if (e >= grid[m - 1]) return values[m - 1];
    double estimate = (e - grid[0]) * inv_step;
    int i;
    if (!(estimate > 0.0)) i = 0;
    else if (estimate >= (double)(m - 2)) i = m - 2;
    else i = (int)estimate;
    while (i > 0 && grid[i] > e) i--;
    while (i < m - 2 && grid[i + 1] <= e) i++;
    double span = grid[i + 1] - grid[i];
    if (span <= 0.0) return values[i];
    return values[i] + (e - grid[i]) / span * (values[i + 1] - values[i]);
}

static double next_energy(const Config *c, double e, double p, double dt) {
    if (p >= 0.0) return e + dt * p * c->eta_charge;
    return e + dt * p / c->eta_discharge;
}

static double wear_cost(const Config *c, double e_from, double e_to) {
    double drop = e_from - e_to;
    if (drop < 0.0) drop = 0.0;
    return c->wear_rate * drop;
}

static double exact_power(const Config *c, double e, double e_target, double dt) {
    double delta = e_target - e;
    if (delta >= 0.0) return dt > 0.0 ? delta / (dt * c->eta_charge) : 0.0;
    return dt > 0.0 ? delta * c->eta_discharge / dt : 0.0;
}

/* slot_cost_eur at battery power p (positive = charging). */
static double slot_cost(const Config *c, const SlotCtx *ctx, double dt, double p) {
    double grid = ctx->base + p;
    double imported = grid > 0.0 ? grid : 0.0;
    double exported = grid < 0.0 ? -grid : 0.0;
    double cost = dt * (ctx->buy * imported - ctx->sell * exported);
    if (p > 0.0) {
        double pv_kwh = p < ctx->surplus ? p : ctx->surplus;
        double pv_price_delta = ctx->pv_price - ctx->sell;
        if (pv_kwh > 0.0 && pv_price_delta != 0.0) cost += dt * pv_price_delta * pv_kwh;
        if (c->min_spread_eur_per_kwh > 0.0) {
            double from_grid = p - ctx->surplus;
            if (from_grid < 0.0) from_grid = 0.0;
            cost += c->min_spread_eur_per_kwh * from_grid * dt;
        }
    }
    return cost;
}

/* best_at for an off-grid energy during reconstruction (the reference re-solves it, and does not
 * add to the iteration count). */
static double choose_power(const Config *c, const SlotCtx *ctx, const double *grid, int m,
                           double inv_step, const double *next_value, double e_now, double dt,
                           int last_slot, double *actions, long actions_capacity) {
    double lo, hi, soc_lo, soc_hi;
    bounds(c, ctx, e_now, dt, &lo, &hi, &soc_lo, &soc_hi);
    int has_extra = last_slot && c->has_target;
    double extra = has_extra ? exact_power(c, e_now, c->e_target, dt) : 0.0;
    int na = build_actions(lo, hi, c->power_steps, has_extra, extra, actions, actions_capacity);
    if (na < 0) return clampd(0.0, lo, hi);
    double best = INFINITY;
    double best_p = clampd(0.0, lo, hi);
    for (int j = 0; j < na; j++) {
        double p = actions[j];
        double e_next = next_energy(c, e_now, p, dt);
        double cost = slot_cost(c, ctx, dt, p);
        cost += wear_cost(c, e_now, e_next);
        cost += interp(next_value, grid, m, inv_step, e_next);
        if (cost < best) {
            best = cost;
            best_p = p;
        }
    }
    return best_p;
}

/* The value iteration + path reconstruction.  No Python object is touched below this line and the
 * GIL is released around it; every buffer arrives pre-sized and every capacity was checked. */
static int run_solve(const Config *c, const double *pv, const double *load, const double *buy,
                     const double *sell, const double *minutes, int n, const double *grid, int m,
                     double inv_step, double *value, double *argmin, double *actions,
                     long actions_capacity, double *energies, double *powers, double *windows,
                     long *iterations_out, double *terminal_error_out, double *cycle_cost_out,
                     double *objective_out, double *internal_out) {
    double *terminal = value + (size_t)n * (size_t)m;
    for (int k = 0; k < m; k++) {
        double v = -c->salvage * grid[k];
        if (c->has_target) v += c->target_penalty * fabs(grid[k] - c->e_target);
        terminal[k] = v;
    }

    long iterations = 0;
    for (int i = n - 1; i >= 0; i--) {
        SlotCtx ctx;
        make_ctx(c, &ctx, pv[i], load[i], buy[i], sell[i], minutes[i]);
        double dt = ctx.dt;
        double *row = value + (size_t)i * (size_t)m;
        double *next_row = value + (size_t)(i + 1) * (size_t)m;
        double *choices = argmin + (size_t)i * (size_t)m;
        int last_slot = (i == n - 1);
        for (int k = 0; k < m; k++) {
            double e = grid[k];
            double lo, hi, soc_lo, soc_hi;
            bounds(c, &ctx, e, dt, &lo, &hi, &soc_lo, &soc_hi);
            int has_extra = last_slot && c->has_target;
            double extra = has_extra ? exact_power(c, e, c->e_target, dt) : 0.0;
            int na = build_actions(lo, hi, c->power_steps, has_extra, extra, actions,
                                   actions_capacity);
            if (na < 0) return -1;
            double best = INFINITY;
            double best_p = clampd(0.0, lo, hi);
            for (int j = 0; j < na; j++) {
                double p = actions[j];
                double e_next = next_energy(c, e, p, dt);
                double cost = slot_cost(c, &ctx, dt, p);
                cost += wear_cost(c, e, e_next);
                cost += interp(next_row, grid, m, inv_step, e_next);
                iterations++;
                if (cost < best) { /* strict: ties resolve to the lowest power */
                    best = cost;
                    best_p = p;
                }
            }
            row[k] = best;
            choices[k] = best_p;
        }
    }

    double e_now = clampd(c->start_kwh, c->e_min_kwh, c->e_max_kwh);
    double cycle_cost = 0.0;
    double objective = 0.0;
    for (int i = 0; i < n; i++) {
        SlotCtx ctx;
        make_ctx(c, &ctx, pv[i], load[i], buy[i], sell[i], minutes[i]);
        double dt = ctx.dt;
        double lo, hi, soc_lo, soc_hi;
        bounds(c, &ctx, e_now, dt, &lo, &hi, &soc_lo, &soc_hi);
        int found = -1;
        for (int k = 0; k < m; k++) {
            if (fabs(grid[k] - e_now) <= 1e-9) {
                found = k;
                break;
            }
        }
        double chosen = found >= 0
                            ? argmin[(size_t)i * (size_t)m + (size_t)found]
                            : choose_power(c, &ctx, grid, m, inv_step,
                                           value + (size_t)(i + 1) * (size_t)m, e_now, dt,
                                           i == n - 1, actions, actions_capacity);
        double p = clampd(chosen, lo, hi);
        energies[i] = e_now;
        powers[i] = p;
        windows[(size_t)4 * (size_t)i + 0] = soc_lo;
        windows[(size_t)4 * (size_t)i + 1] = soc_hi;
        windows[(size_t)4 * (size_t)i + 2] = ctx.grid_lo;
        windows[(size_t)4 * (size_t)i + 3] = ctx.grid_hi;
        double e_next = next_energy(c, e_now, p, dt);
        cycle_cost += wear_cost(c, e_now, e_next);
        objective += slot_cost(c, &ctx, dt, p);
        e_now = clampd(e_next, c->e_min_kwh, c->e_max_kwh);
    }
    objective += cycle_cost - c->salvage * e_now;
    double terminal_error = 0.0;
    if (c->has_target) {
        objective += c->target_penalty * fabs(e_now - c->e_target);
        terminal_error = fabs(e_now - c->e_target);
    }

    double start_clamped = clampd(c->start_kwh, c->e_min_kwh, c->e_max_kwh);
    int start_index = 0;
    double best_distance = fabs(grid[0] - start_clamped);
    for (int k = 1; k < m; k++) {
        double distance = fabs(grid[k] - start_clamped);
        if (distance < best_distance) {
            best_distance = distance;
            start_index = k;
        }
    }

    *iterations_out = iterations;
    *terminal_error_out = terminal_error;
    *cycle_cost_out = cycle_cost;
    *objective_out = objective;
    *internal_out = value[start_index];
    return 0;
}

/* ---- config parsing and domain validation --------------------------------------------------- */

/* Returns 1 when present (and finite), 0 when absent or None, -1 on a bad value. */
static int cfg_double(PyObject *cfg, const char *key, double *out, int required) {
    PyObject *item = PyDict_GetItemString(cfg, key);
    if (!item || item == Py_None) {
        if (required) {
            PyErr_Format(PyExc_ValueError, "config is missing a value for %s", key);
            return -1;
        }
        return 0;
    }
    double value = PyFloat_AsDouble(item);
    if (value == -1.0 && PyErr_Occurred()) return -1;
    if (!isfinite(value)) {
        PyErr_Format(PyExc_ValueError, "config value for %s is not finite", key);
        return -1;
    }
    *out = value;
    return 1;
}

static int cfg_long(PyObject *cfg, const char *key, long *out, int required) {
    PyObject *item = PyDict_GetItemString(cfg, key);
    if (!item || item == Py_None) {
        if (required) {
            PyErr_Format(PyExc_ValueError, "config is missing a value for %s", key);
            return -1;
        }
        return 0;
    }
    long value = PyLong_AsLong(item);
    if (value == -1 && PyErr_Occurred()) return -1;
    *out = value;
    return 1;
}

static int cfg_bool(PyObject *cfg, const char *key, int *out, int default_value) {
    PyObject *item = PyDict_GetItemString(cfg, key);
    if (!item || item == Py_None) {
        *out = default_value;
        return 0;
    }
    int truth = PyObject_IsTrue(item);
    if (truth < 0) return -1;
    *out = truth;
    return 0;
}

static int parse_config(PyObject *cfg, Config *c) {
    if (!PyDict_Check(cfg)) {
        PyErr_SetString(PyExc_TypeError, "config must be a dict");
        return -1;
    }
    if (cfg_double(cfg, "e_min_kwh", &c->e_min_kwh, 1) < 0) return -1;
    if (cfg_double(cfg, "e_max_kwh", &c->e_max_kwh, 1) < 0) return -1;
    if (cfg_double(cfg, "p_charge_kw", &c->p_charge_kw, 1) < 0) return -1;
    if (cfg_double(cfg, "p_discharge_kw", &c->p_discharge_kw, 1) < 0) return -1;
    if (cfg_double(cfg, "eta_charge", &c->eta_charge, 1) < 0) return -1;
    if (cfg_double(cfg, "eta_discharge", &c->eta_discharge, 1) < 0) return -1;
    if (cfg_double(cfg, "start_kwh", &c->start_kwh, 1) < 0) return -1;
    c->has_target = cfg_double(cfg, "e_target", &c->e_target, 0) == 1;
    if (PyErr_Occurred()) return -1;
    if (cfg_double(cfg, "slot_minutes", &c->slot_minutes, 1) < 0) return -1;
    if (cfg_double(cfg, "soc_step_kwh", &c->soc_step_kwh, 1) < 0) return -1;
    if (cfg_long(cfg, "power_steps", &c->power_steps, 1) < 0) return -1;
    if (cfg_bool(cfg, "allow_grid_charge", &c->allow_grid_charge, 1) < 0) return -1;
    if (cfg_bool(cfg, "allow_export", &c->allow_export, 1) < 0) return -1;
    if (cfg_double(cfg, "max_import_kw", &c->max_import_kw, 1) < 0) return -1;
    if (cfg_double(cfg, "max_export_kw", &c->max_export_kw, 1) < 0) return -1;
    if (cfg_double(cfg, "min_spread_eur_per_kwh", &c->min_spread_eur_per_kwh, 1) < 0) return -1;
    c->has_sell_floor = cfg_double(cfg, "sell_floor_eur_per_kwh", &c->sell_floor_eur_per_kwh, 0) == 1;
    if (PyErr_Occurred()) return -1;

    PyObject *pref = PyDict_GetItemString(cfg, "pv_charge_preference");
    const char *pref_text = pref ? PyUnicode_AsUTF8(pref) : "auto";
    if (!pref_text) return -1;
    if (strcmp(pref_text, "prefer_pv") == 0) c->pv_preference = PV_PREFER;
    else if (strcmp(pref_text, "avoid_pv") == 0) c->pv_preference = PV_AVOID;
    else c->pv_preference = PV_AUTO;

    if (cfg_double(cfg, "wear_eur_per_kwh", &c->wear_eur_per_kwh, 1) < 0) return -1;
    c->wear_rate = c->wear_eur_per_kwh * c->eta_discharge;
    if (cfg_double(cfg, "salvage", &c->salvage, 1) < 0) return -1;
    if (cfg_double(cfg, "target_penalty", &c->target_penalty, 1) < 0) return -1;
    if (cfg_long(cfg, "max_states", &c->max_states, 1) < 0) return -1;
    if (cfg_long(cfg, "max_iterations", &c->max_iterations, 1) < 0) return -1;

    /* Unsupported dimensions are rejected before any narrowing. */
    if (c->power_steps < 0 || c->power_steps > CORE_MAX_DIMENSION) {
        PyErr_SetString(PyExc_ValueError, "power_steps is outside the supported range");
        return -1;
    }
    if (c->max_states < CORE_MIN_STATES || c->max_states > CORE_MAX_DIMENSION) {
        PyErr_SetString(PyExc_ValueError, "max_states is outside the supported range");
        return -1;
    }
    if (c->max_iterations < 1) {
        PyErr_SetString(PyExc_ValueError, "max_iterations must be positive");
        return -1;
    }

    /* Numeric domains: every derived ratio used by the sweep must be finite. */
    if (!(c->e_min_kwh <= c->e_max_kwh) || !isfinite(c->e_max_kwh - c->e_min_kwh)) {
        PyErr_SetString(PyExc_ValueError, "soc bounds do not produce a finite span");
        return -1;
    }
    if (!(c->eta_charge > 0.0) || !(c->eta_discharge > 0.0)) {
        PyErr_SetString(PyExc_ValueError, "battery efficiencies must be positive");
        return -1;
    }
    if (!(c->p_charge_kw >= 0.0) || !(c->p_discharge_kw >= 0.0)) {
        PyErr_SetString(PyExc_ValueError, "battery power limits must be non-negative");
        return -1;
    }
    if (!(c->slot_minutes >= 0.0)) {
        PyErr_SetString(PyExc_ValueError, "slot_minutes must be non-negative");
        return -1;
    }
    return 0;
}

/* Per-slot domain checks: reject any combination that could make a transition, a soc window or an
 * exact-power result non-finite (and therefore unsafe to interpolate or cast). */
static int validate_slots(const Config *c, const double *pv, const double *load,
                          const double *minutes, int n) {
    double span = c->e_max_kwh - c->e_min_kwh;
    for (int i = 0; i < n; i++) {
        double dt = minutes[i] / 60.0;
        if (!isfinite(dt) || !(dt >= 0.0)) {
            PyErr_Format(PyExc_ValueError, "minutes[%d] is not a finite non-negative duration", i);
            return -1;
        }
        double base = load[i] - pv[i];
        if (!isfinite(base) || !isfinite(c->max_import_kw - base) ||
            !isfinite(-(c->max_export_kw + base))) {
            PyErr_Format(PyExc_ValueError, "slot %d produces a non-finite grid flow", i);
            return -1;
        }
        if (dt <= 0.0) continue;
        double charge_factor = dt * c->eta_charge;
        double discharge_factor = dt / c->eta_discharge;
        double charge_move = dt * c->p_charge_kw * c->eta_charge;
        double discharge_move = dt * c->p_discharge_kw / c->eta_discharge;
        if (!isfinite(charge_factor) || !(charge_factor > 0.0) || !isfinite(discharge_factor) ||
            !(discharge_factor > 0.0)) {
            PyErr_Format(PyExc_ValueError, "slot %d makes the battery transition factors non-finite",
                         i);
            return -1;
        }
        if (!isfinite(charge_move) || !isfinite(discharge_move) ||
            !isfinite(c->e_max_kwh + charge_move) || !isfinite(c->e_min_kwh - discharge_move)) {
            PyErr_Format(PyExc_ValueError, "slot %d makes the battery transitions non-finite", i);
            return -1;
        }
        if (!isfinite(span / charge_factor) || !isfinite(span / discharge_factor) ||
            !isfinite(span * c->eta_discharge / dt) || !isfinite(span / (dt * c->eta_charge))) {
            PyErr_Format(PyExc_ValueError, "slot %d makes the soc window non-finite", i);
            return -1;
        }
        if (c->has_target) {
            double reach = c->e_target - c->e_min_kwh;
            if (!isfinite(reach / (dt * c->eta_charge)) ||
                !isfinite(reach * c->eta_discharge / dt)) {
                PyErr_Format(PyExc_ValueError, "slot %d makes the terminal target non-finite", i);
                return -1;
            }
        }
    }
    return 0;
}

/* ---- sequence -> double buffer -------------------------------------------------------------- */

/* Snapshot to a tuple before reading elements: ``PySequence_Fast`` may return the caller's mutable
 * list, and a ``__float__`` callback is free to shrink it while we convert (the GIL does not make
 * a sequence immutable).  The tuple pins the items, so the reads below are always in bounds. */
static double *to_doubles(PyObject *sequence, Py_ssize_t *n_out, const char *name) {
    PyObject *snapshot = PySequence_Tuple(sequence);
    if (!snapshot) {
        PyErr_Clear();
        PyErr_Format(PyExc_TypeError, "%s must be a sequence of floats", name);
        return NULL;
    }
    if (!PyTuple_Check(snapshot)) {
        Py_DECREF(snapshot);
        PyErr_Format(PyExc_TypeError, "%s must be a sequence of floats", name);
        return NULL;
    }
    Py_ssize_t n = PyTuple_GET_SIZE(snapshot);
    if (n > (Py_ssize_t)CORE_MAX_DIMENSION) {
        Py_DECREF(snapshot);
        PyErr_Format(PyExc_ValueError, "%s has more entries than the supported core accepts", name);
        return NULL;
    }
    double *buffer = (double *)malloc(sizeof(double) * (size_t)(n > 0 ? n : 1));
    if (!buffer) {
        Py_DECREF(snapshot);
        PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyTuple_GET_ITEM(snapshot, i);
        double value = PyFloat_AsDouble(item);
        if (value == -1.0 && PyErr_Occurred()) {
            free(buffer);
            Py_DECREF(snapshot);
            return NULL;
        }
        if (!isfinite(value)) {
            PyErr_Format(PyExc_ValueError, "%s[%zd] is not finite", name, (Py_ssize_t)i);
            free(buffer);
            Py_DECREF(snapshot);
            return NULL;
        }
        buffer[i] = value;
    }
    Py_DECREF(snapshot);
    *n_out = n;
    return buffer;
}

/* ---- the Python entry point ----------------------------------------------------------------- */

static int dict_set_float(PyObject *dict, const char *key, double value) {
    PyObject *item = PyFloat_FromDouble(value);
    if (!item) return -1;
    int rc = PyDict_SetItemString(dict, key, item);
    Py_DECREF(item);
    return rc;
}

static int dict_set_long(PyObject *dict, const char *key, long value) {
    PyObject *item = PyLong_FromLong(value);
    if (!item) return -1;
    int rc = PyDict_SetItemString(dict, key, item);
    Py_DECREF(item);
    return rc;
}

static PyObject *core_solve(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *pv_obj, *load_obj, *buy_obj, *sell_obj, *minutes_obj, *config_obj;
    if (!PyArg_ParseTuple(args, "OOOOOO:solve", &pv_obj, &load_obj, &buy_obj, &sell_obj,
                          &minutes_obj, &config_obj)) {
        return NULL;
    }

    Config c;
    if (parse_config(config_obj, &c) < 0) return NULL;

    Py_ssize_t n_pv = 0, n_load = 0, n_buy = 0, n_sell = 0, n_minutes = 0;
    double *pv = to_doubles(pv_obj, &n_pv, "pv");
    if (!pv) return NULL;
    double *load = to_doubles(load_obj, &n_load, "load");
    double *buy = to_doubles(buy_obj, &n_buy, "buy");
    double *sell = to_doubles(sell_obj, &n_sell, "sell");
    double *minutes = to_doubles(minutes_obj, &n_minutes, "minutes");
    if (!load || !buy || !sell || !minutes) goto fail;
    if (n_pv != n_load || n_pv != n_buy || n_pv != n_sell || n_pv != n_minutes) {
        PyErr_SetString(PyExc_ValueError,
                        "pv, load, buy, sell and minutes must all have the same length");
        goto fail;
    }
    if (n_pv > (Py_ssize_t)CORE_MAX_DIMENSION) {
        PyErr_SetString(PyExc_ValueError, "too many slots for the supported core");
        goto fail;
    }
    int n = (int)n_pv;
    if (validate_slots(&c, pv, load, minutes, n) < 0) goto fail;

    /* State grid and sizes; every product is checked before it is used. */
    size_t grid_capacity = (size_t)c.max_states + 8;
    double *grid = (double *)malloc(sizeof(double) * grid_capacity);
    if (!grid) {
        PyErr_NoMemory();
        goto fail;
    }
    double anchors[2];
    int n_anchors = 0;
    anchors[n_anchors++] = clampd(c.start_kwh, c.e_min_kwh, c.e_max_kwh);
    if (c.has_target) anchors[n_anchors++] = c.e_target;
    int m = 0;
    double step = 0.0;
    if (build_grid(c.e_min_kwh, c.e_max_kwh, c.soc_step_kwh, c.max_states, anchors, n_anchors, grid,
                   (int)grid_capacity, &m, &step) < 0) {
        free(grid);
        goto fail;
    }
    long action_count = c.power_steps + 1;
    if (action_count < 2) action_count = 2;
    long budget = c.max_iterations / (n > 0 ? (long)n * action_count : 1);
    if (budget < 2) budget = 2;
    if ((long)m > budget) {
        double span = c.e_max_kwh - c.e_min_kwh;
        if (span < 0.0) span = 0.0;
        double denom = (double)(budget - 1);
        if (denom < 1.0) denom = 1.0;
        double widened = span / denom;
        if (widened < step) widened = step;
        if (build_grid(c.e_min_kwh, c.e_max_kwh, widened, c.max_states, anchors, n_anchors, grid,
                       (int)grid_capacity, &m, &step) < 0) {
            free(grid);
            goto fail;
        }
    }
    if (!(step > 0.0) || !isfinite(step)) {
        PyErr_SetString(PyExc_ValueError, "soc step is not usable for these bounds");
        free(grid);
        goto fail;
    }

    size_t slots = (size_t)n;
    size_t rows = slots + 1;
    size_t states = (size_t)m;
    size_t value_count = 0;
    size_t argmin_count = 0;
    size_t windows_count = 0;
    size_t value_bytes = 0;
    if (checked_mul(rows, states, &value_count) < 0 ||
        checked_mul(value_count, sizeof(double), &value_bytes) < 0 ||
        checked_mul(slots, states, &argmin_count) < 0 || checked_mul(slots, 4, &windows_count) < 0 ||
        value_bytes > (size_t)PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_ValueError, "scenario dimensions exceed the supported size");
        free(grid);
        goto fail;
    }
    size_t actions_capacity = (size_t)c.power_steps + 2;
    if (actions_capacity < 3) actions_capacity = 3;

    double *value = (double *)malloc(sizeof(double) * (value_count > 0 ? value_count : 1));
    double *argmin =
        n > 0 ? (double *)malloc(sizeof(double) * (argmin_count > 0 ? argmin_count : 1)) : NULL;
    double *actions = (double *)malloc(sizeof(double) * actions_capacity);
    double *energies = (double *)malloc(sizeof(double) * (slots > 0 ? slots : 1));
    double *powers = (double *)malloc(sizeof(double) * (slots > 0 ? slots : 1));
    double *windows = (double *)malloc(sizeof(double) * (windows_count > 0 ? windows_count : 1));
    if (!value || (n > 0 && !argmin) || !actions || !energies || !powers || !windows) {
        free(grid);
        free(value);
        free(argmin);
        free(actions);
        free(energies);
        free(powers);
        free(windows);
        PyErr_NoMemory();
        goto fail;
    }

    long iterations = 0;
    double terminal_error = 0.0, cycle_cost = 0.0, objective = 0.0, internal = 0.0;
    int status;
    double inv_step = 1.0 / step;
    Py_BEGIN_ALLOW_THREADS
    status = run_solve(&c, pv, load, buy, sell, minutes, n, grid, m, inv_step, value, argmin,
                       actions, (long)actions_capacity, energies, powers, windows, &iterations,
                       &terminal_error, &cycle_cost, &objective, &internal);
    Py_END_ALLOW_THREADS
    if (status != 0) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_RuntimeError, "core sweep failed");
        free(grid);
        free(value);
        free(argmin);
        free(actions);
        free(energies);
        free(powers);
        free(windows);
        goto fail;
    }

    PyObject *result = PyDict_New();
    PyObject *energies_list = PyList_New((Py_ssize_t)slots);
    PyObject *powers_list = PyList_New((Py_ssize_t)slots);
    PyObject *windows_list = PyList_New((Py_ssize_t)windows_count);
    PyObject *grid_list = PyList_New((Py_ssize_t)states);
    PyObject *value_bytes_obj = PyBytes_FromStringAndSize((const char *)value, (Py_ssize_t)value_bytes);
    if (!result || !energies_list || !powers_list || !windows_list || !grid_list ||
        !value_bytes_obj) {
        Py_XDECREF(result);
        Py_XDECREF(energies_list);
        Py_XDECREF(powers_list);
        Py_XDECREF(windows_list);
        Py_XDECREF(grid_list);
        Py_XDECREF(value_bytes_obj);
        free(grid);
        free(value);
        free(argmin);
        free(actions);
        free(energies);
        free(powers);
        free(windows);
        goto fail;
    }
    int ok = 0;
    for (size_t i = 0; i < slots && ok == 0; i++) {
        PyObject *energy = PyFloat_FromDouble(energies[i]);
        PyObject *power = PyFloat_FromDouble(powers[i]);
        if (!energy || !power) {
            Py_XDECREF(energy);
            Py_XDECREF(power);
            ok = -1;
            break;
        }
        PyList_SET_ITEM(energies_list, (Py_ssize_t)i, energy);
        PyList_SET_ITEM(powers_list, (Py_ssize_t)i, power);
    }
    for (size_t i = 0; i < windows_count && ok == 0; i++) {
        PyObject *item = PyFloat_FromDouble(windows[i]);
        if (!item) {
            ok = -1;
            break;
        }
        PyList_SET_ITEM(windows_list, (Py_ssize_t)i, item);
    }
    for (size_t i = 0; i < states && ok == 0; i++) {
        PyObject *item = PyFloat_FromDouble(grid[i]);
        if (!item) {
            ok = -1;
            break;
        }
        PyList_SET_ITEM(grid_list, (Py_ssize_t)i, item);
    }
    free(grid);
    free(value);
    free(argmin);
    free(actions);
    free(energies);
    free(powers);
    free(windows);

    if (ok == 0) {
        ok |= PyDict_SetItemString(result, "energies", energies_list);
        ok |= PyDict_SetItemString(result, "powers", powers_list);
        ok |= PyDict_SetItemString(result, "windows", windows_list);
        ok |= PyDict_SetItemString(result, "grid", grid_list);
        ok |= PyDict_SetItemString(result, "value", value_bytes_obj);
        ok |= dict_set_float(result, "objective_eur", objective);
        ok |= dict_set_float(result, "cycle_cost_eur", cycle_cost);
        ok |= dict_set_float(result, "terminal_error_kwh", terminal_error);
        ok |= dict_set_float(result, "effective_step_kwh", step);
        ok |= dict_set_float(result, "internal_objective_eur", internal);
        ok |= dict_set_long(result, "iterations", iterations);
        ok |= dict_set_long(result, "states", (long)states);
    }
    Py_DECREF(energies_list);
    Py_DECREF(powers_list);
    Py_DECREF(windows_list);
    Py_DECREF(grid_list);
    Py_DECREF(value_bytes_obj);
    if (ok < 0) {
        Py_DECREF(result);
        goto fail;
    }
    free(pv);
    free(load);
    free(buy);
    free(sell);
    free(minutes);
    return result;

fail:
    free(pv);
    free(load);
    free(buy);
    free(sell);
    free(minutes);
    return NULL;
}

static PyMethodDef core_methods[] = {
    {"solve", core_solve, METH_VARARGS,
     "Run the SoC Bellman sweep + path reconstruction for one scenario over flat arrays."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef core_module = {
    PyModuleDef_HEAD_INIT, "_core", "CPython accelerator for app.optimize.dp (SPEC 8.2).", -1,
    core_methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__core(void) { return PyModule_Create(&core_module); }
