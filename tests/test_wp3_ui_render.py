"""WP3 (UI) render check: the SPA is rendered through a DOM shim and its structure is asserted.

The static checks in test_wp3_assets.py prove the assets are self-contained and that the source
contains the right patterns. They cannot prove that the settings form actually *renders* every
setting, that the advanced rows fold, or that a view does not print `undefined`. ``tools/
ui_render_check.js`` runs the real ``app.js`` and ``mock.js`` against a dependency-free DOM shim
(``tools/ui_dom_shim.js``) and checks exactly that, so the settings information architecture from
SPEC 13.2 has a gate that fails when it regresses rather than only a review pass.

    python3 -m pytest tests/test_wp3_ui_render.py -q

Skipped when node is not installed, like the other node-based checks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "tools" / "ui_render_check.js"
NODE = shutil.which("node")


def test_ui_renders_and_settings_ia_holds():
    if NODE is None:
        pytest.skip("node is not installed")
    assert CHECK.is_file(), f"missing {CHECK}"
    proc = subprocess.run(
        [NODE, str(CHECK), str(ROOT)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"the rendered UI failed its checks:\n{proc.stdout}\n{proc.stderr}"
    assert "0 failed" in proc.stdout, proc.stdout
    # A render check that silently checks nothing is worse than no check: assert the load-bearing ones.
    for needle in (
        "every view renders from mock data",
        "history offers a local date and 15-minute/hour/day resolutions",
        "history states its IANA timezone and direct-measurement rule",
        "history renders a chart fallback and an accessible provenance/coverage table",
        "daily history exposes a bounded 1–31 day selector",
        "settingsVisible is exported for offline testing",
        "the all-settings drawer holds the full form",
        "the setup cards own the primary keys",
        "every card key is rendered on a card",
        "every settings key that applies here is reachable",
        "advanced rows are folded by default",
        "opening the disclosure reveals its advanced rows",
        "Show all advanced reveals every key that applies",
        "is never folded",
        "every emitted class is defined",
        "--chart-* variables survive the restyle",
        # SPEC 10 plan execution: the window rows must keep stating the slot's own data, not just
        # its action, and a stale or clamped slot must keep saying so.
        "windows merge consecutive identical slots only",
        "the plan payload carries its own resolution",
        "a window states the prices behind the decision",
        "a window states its break-even and the margin over it",
        "a window states the battery energy and the SoC path",
        "a window states its grid flow and what it earns",
        "the join lands the dispatch and the measurement on the right window",
        "a clamped dispatch states applied vs requested",
        "a measured window compares measured with planned",
        "a stale window says the dispatcher refuses it (stale_input)",
        "the rendered rows carry facts, context, status and price provenance",
        # SPEC 11 measured savings: only confirmed battery flow earns credit, and an unmeasured
        # bucket stays unknown.
        "the savings payload carries measured battery flows and per-bucket coverage",
        "the savings view labels measured battery flows",
        "the savings view states sampled-time coverage",
        "the savings cards state the scope of each figure",
        "the time-coverage figure is formatted as hours",
        "an unmeasured bucket reports its cycle energy as unknown, not planned",
        "a measured bucket renders its charged and discharged energy",
        # SPEC 5 learned capacity: the Batteries card must state the effective value and where it
        # came from, never bury a learned estimate without its evidence.
        "the batteries card shows the effective capacity, marked learned",
        # SPEC 7.2 named load devices: the Load card's editor must render one row per configured
        # device, offer the closed kind set, and refuse an unsaveable row before a save is attempted.
        "the Load card renders the named-device editor",
        "the device kind dropdown offers the closed kind set",
        "the device editor can add a row",
        "an added device row without a meter is refused before saving",
        "a repeated device meter is refused client-side",
        "an unknown device kind is refused client-side",
        "the batteries card states the estimate's sample count and last update",
        "the batteries card states the configured seed next to the effective value",
        "the batteries card states the lowest cell voltage",
        "the batteries card marks a latched battery",
        # SPEC 8.1 scheduled full-charge maintenance: the card states its due/overdue state and the
        # Plan view marks a plan that charges to the ceiling for it.
        "the batteries card states the maintenance state and its target",
        "the batteries card states when the last full charge completed",
        "an overdue battery is marked, not coloured only",
        "the maintenance state of an unwatched battery reads off, never an invented due date",
        "the plan view marks a maintenance plan from its payload",
        "a non-active maintenance block adds no banner",
        "the native driver's connection settings are validated in the wizard",
        # SPEC 5/10 cost-basis ledger (0.20.4): the Dashboard must render the average charged price
        # and the break-even sell price, mark a stalled ledger and never fake a missing one.
        "the dashboard has one Battery energy price panel",
        "the card states the average charged price in EUR/kWh (3 decimals)",
        "the card states the break-even sell price in EUR/kWh (3 decimals)",
        "the break-even line breaks into basis, round trip, wear and margin",
        "the card states the stored energy, its value and the ledger's freshness",
        "a stalled ledger shows a visible warning next to the numbers",
        "an empty ledger says so instead of showing a stale figure",
        "a missing price renders as a dash, never a fake number",
        "several enabled batteries render a per-battery row",
    ):
        assert needle in proc.stdout, f"the render check no longer reports {needle!r}:\n{proc.stdout}"
