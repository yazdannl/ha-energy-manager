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
        "settingsVisible is exported for offline testing",
        "every settings key that applies here is reachable",
        "advanced rows are folded by default",
        "opening the disclosure reveals its advanced rows",
        "Show all advanced reveals every key that applies",
        "is never folded",
        "every emitted class is defined",
        "--chart-* variables survive the restyle",
    ):
        assert needle in proc.stdout, f"the render check no longer reports {needle!r}:\n{proc.stdout}"
