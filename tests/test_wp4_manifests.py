"""Add-on manifests: the rules Supervisor applies when it lists and starts the add-on.

    python3 -m pytest tests/test_wp4_manifests.py -q

These belong in the suite, not only in the CI workflow, because the failure mode is invisible to
everything else here: a manifest can be valid YAML, lint clean and pass every application test while
Supervisor still refuses to start the add-on. Version 0.1.0 shipped exactly that — `options:` carried
explicit `null`s for the keys whose real values come from Home Assistant, and starting it failed with
"Missing required option 'currency'". Needs PyYAML, which is a test-only dependency: the add-on
itself imports nothing outside the standard library (SPEC.md R3).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "energy_manager" / "config.yaml"
BUILD = ROOT / "energy_manager" / "build.yaml"
STORE = ROOT / "repository.yaml"

#: Present in the schema, deliberately absent from `options:`: the add-on reads them from Home
#: Assistant at runtime, and any placeholder value here would silently override that.
FROM_HOME_ASSISTANT = ("currency", "timezone", "latitude_override", "longitude_override")


def config() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def test_no_option_carries_a_null_value():
    """Supervisor rejects an explicit null in `options:` and the add-on then never starts."""
    nulls = sorted(key for key, value in config()["options"].items() if value is None)
    assert not nulls, f"null option values block the add-on start: {nulls}"


def test_every_option_has_a_schema_entry():
    cfg = config()
    assert set(cfg["options"]) <= set(cfg["schema"])


def test_the_shipped_defaults_cannot_move_a_battery():
    """The published default has to be the safe one: plan and report, never write."""
    options = config()["options"]
    assert options["mode"] == "advisory"
    assert options["dry_run"] is True


def test_options_that_come_from_home_assistant_stay_optional():
    cfg = config()
    for key in FROM_HOME_ASSISTANT:
        assert cfg["schema"][key].endswith("?"), f"{key} must stay optional in the schema"
        assert key not in cfg["options"], f"{key} must be absent from options, not null"


def test_the_image_builds_every_architecture_the_manifest_advertises():
    build = yaml.safe_load(BUILD.read_text())
    assert set(config()["arch"]) == set(build["build_from"])


def test_the_local_api_port_is_never_published():
    """Ingress only: publishing 8099 would expose the local API without authentication."""
    cfg = config()
    assert cfg["ingress"] is True
    assert cfg["ingress_port"] == 8099
    assert "ports" not in cfg
    assert "host_network" not in cfg


def test_the_app_reports_the_manifest_version():
    """Run the app in a clean subprocess, the way the add-on starts, and read its version back."""
    code = (
        "from app.main import APP_VERSION; from app import __version__; "
        "print(APP_VERSION, __version__)"
    )
    environment = dict(os.environ)
    environment.pop("SEM_VERSION", None)
    environment.pop("BUILD_VERSION", None)
    environment["PYTHONPATH"] = str(ROOT / "energy_manager")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    reported, dunder = result.stdout.split()
    assert reported == dunder == config()["version"], (
        "config.yaml, app/__init__.py and the app/main.py fallback must agree"
    )


def test_the_store_descriptor_points_at_this_manifest():
    store = yaml.safe_load(STORE.read_text())
    cfg = config()
    assert store["url"] == cfg["url"]
    assert store["maintainer"]
    assert cfg["slug"].isidentifier(), cfg["slug"]
