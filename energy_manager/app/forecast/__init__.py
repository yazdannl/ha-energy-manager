"""Learned forecasting (SPEC.md section 7).

Two halves, deliberately separated by what they need at runtime:

* :mod:`forecast.pv` — the committed base model. Pure standard-library arithmetic over a frozen
  artifact, no state, no I/O beyond reading the artifact once. This is what serves a forecast on a
  fresh install, before the install has any history of its own.
* :mod:`forecast.local_head` — the part that learns from *this* house. Small, regularised toward the
  base model, refitted from the store on a slow cadence, and adopted only when it demonstrably beats
  what it replaces.

The split matters because the two fail differently. A bad base artifact is a build-time mistake, and
the parity test catches it before it ships. A bad local fit is a *data* accident — a heat pump
cycling, a holiday, a meter swap — so it is gated at adoption time and reversible without a rebuild.
"""

from __future__ import annotations

__all__ = ["local_head", "pv"]
