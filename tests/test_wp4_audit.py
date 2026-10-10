"""WP4: the PII/secret tripwire must catch every shape SPEC.md R1 forbids, and must stay quiet on
the real tree (SPEC.md section 14).

The planted values are assembled from pieces on purpose: a test file about a secret scanner has to
pass that scanner itself.

    python3 -m pytest tests/test_wp4_audit.py -q

Note for the other workers: ``test_repository_tree_is_clean`` is the acceptance gate, so a leak
planted in any committed file is meant to turn this test red.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# plant file name -> (text written to the file, rule label it must be reported under)
PLANTS = {
    "coordinate": ("latitude: 52." + "12345", "coordinate"),
    "private-ip": ("broker host: 192." + "168.7." + "21", "private-ip"),
    "entity-sensor": ("watch " + "sensor." + "kitchen_boiler_power", "entity-sensor"),
    "entity-domain": ("watch " + "light." + "hall_ceiling_lamp", "entity-domain"),
    # The domains a battery driver actually writes to (WP5 finding 3): these must be caught too.
    "entity-domain-number": (
        "setpoint " + "number." + "zendure_battery_manual_power",
        "entity-domain",
    ),
    "entity-domain-select": (
        "manager " + "select." + "zendure_manager_operation",
        "entity-domain",
    ),
    "token-assignment": ("api" + "_key = " + "sk-abc123456", "token-assignment"),
    "credential": (
        "Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789",
        "credential",
    ),
    "email": ("mail me at " + "someone" + "@" + "example.org", "email"),
    "device-serial": ("serial: " + "HOB" + "-1234567", "device-serial"),
    # Final verifier round: shapes the scanner used to miss. The first two are the verifier's
    # demonstrated misses (unlabelled four-decimal coordinate pairs). Every value is assembled from
    # pieces so this test file itself keeps passing the scanner, and the plants only ever exist in
    # the temporary tree, never in the repository.
    "coordinate-pair-latlon": ("52." + "3676, " + "4." + "9041", "coordinate-pair"),
    "coordinate-pair-second": ("48." + "8566, " + "2." + "3522", "coordinate-pair"),
    "private-host-tld": ("http://" + "sem-test." + "local" + ":8123/api", "private-host"),
    # An allowlist must not swallow a longer label that merely ends in the placeholder
    # (regression control for the substring bug: this is not ha.local).
    "private-host-not-placeholder": (
        "http://" + "my" + "ha." + "local" + ":8123",
        "private-host",
    ),
    "private-host-homeassistant": (
        "http://" + "homeassistant" + ":8123" + "/api/states",
        "private-host",
    ),
    "mac": ("bssid " + "de:" + "ad:" + "be:" + "ef:" + "00:" + "11", "mac"),
    "mac-upper": ("mac " + "AA:" + "BB:" + "CC:" + "DD:" + "EE:" + "FF", "mac"),
    "private-host-fritz": ("router http://" + "fritz" + "." + "box/", "private-host"),
    "private-host-homeassistant-url": (
        "http://" + "homeassistant" + "/api",
        "private-host",
    ),
    "hex-id": ("device_id: " + "0123456789abcdef" + "0123456789abcdef", "hex-id"),
    "hex-id-upper": (
        "unique_id: " + "0123456789ABCDEF" + "0123456789ABCDEF",
        "hex-id",
    ),
}


def _run_scanner(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "tools/audit.sh"], cwd=directory, capture_output=True, text=True, check=False
    )


def _copy_scanner(directory: Path) -> None:
    (directory / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "audit.sh", directory / "tools" / "audit.sh")


def test_scanner_reports_each_forbidden_shape():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _copy_scanner(root)
        for name, (text, _label) in PLANTS.items():
            (root / f"{name}.txt").write_text(text + "\n")
        result = _run_scanner(root)

    assert result.returncode == 1, f"scanner did not fail:\n{result.stdout}{result.stderr}"
    lines = result.stdout.splitlines()
    for name, (_, label) in PLANTS.items():
        hits = [line for line in lines if line.startswith(f"  ./{name}.txt:")]
        assert hits, f"{name} slipped through:\n{result.stdout}"
        assert any(line.endswith(f"[{label}]") for line in hits), (
            f"{name} should be [{label}], got {hits}"
        )
    assert "findings:" in result.stdout
    print(result.stdout)


def test_scanner_ignores_redaction_safe_namespaces():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _copy_scanner(root)
        (root / "examples.md").write_text(
            "\n".join(
                [
                    "sensor.example_pv_power and sensor.sem_price_now are fine",
                    "binary_sensor.sem_battery_at_floor is fine",
                    "latitude: 52.0 with only one decimal is a documented placeholder",
                    "a local check may talk to 127.0.0.1",
                    'solcast_api_key: "" is empty on purpose',
                    "notify.mobile_service is a settings key, not an entity",
                    # The allowlists the new hostname rule has to keep working.
                    "HA_URL=http://ha.local:8123/ is the documented dev placeholder",
                    "threading.local() is a Python API, not a hostname",
                    "0.0.0.0, localhost and example.com are reserved",
                    "internal.internal_objective_eur is a code attribute, not a hostname",
                    "api.forecast.solar and api.energy-charts.info are the public providers",
                    "github.com/homeassistant/core is a public repository path",
                    # Ordinary four-decimal test values are not coordinates.
                    "an ordinary value like pytest.approx(0.9500) stays quiet",
                    "a normalised pair (0.9500, 0.9900) is ordinary test data",
                    # A time-like group and longer hex runs are not MACs/identifiers.
                    "a duration of 12:34:56 is not a MAC address",
                    "a digest " + "deadbeef" * 8 + " is longer than a bare 32-hex id",
                    "an all-letter run " + "abcdef" * 5 + "ab is a placeholder, not an id",
                ]
            )
            + "\n"
        )
        result = _run_scanner(root)

    assert result.returncode == 0, f"false positives:\n{result.stdout}{result.stderr}"


def test_scanner_ignores_runtime_and_binary_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _copy_scanner(root)
        for ignored in (".git", "__pycache__", ".venv"):
            directory = root / ignored
            directory.mkdir()
            (directory / "leak.txt").write_text("latitude: 52." + "12345\n")
        (root / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"latitude: 52." + b"12345\x00\xff")
        result = _run_scanner(root)

    assert result.returncode == 0, f"ignored paths were scanned:\n{result.stdout}{result.stderr}"


def test_repository_tree_is_clean():
    result = _run_scanner(ROOT)
    assert result.returncode == 0, f"the committed tree has findings:\n{result.stdout}"

def test_vendored_asset_is_skipped_only_while_its_digest_is_recorded():
    """A vendored third-party file is skipped from the content rules, but never unconditionally.

    The exclusion exists because a minified stylesheet is full of fractional measurements, and the
    coordinate rule is meant to catch fractions. It stops applying the moment the bytes change, so it
    cannot become a place to hide a coordinate: the same tree that scans clean is scanned *and*
    flagged one byte later. A missing sha256sum also means no exclusion.
    """
    shape = "{width:33." + "33333333px}\n"  # exactly what the coordinate rule flags
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _copy_scanner(root)
        vendor = root / "energy_manager" / "app" / "web" / "vendor"
        vendor.mkdir(parents=True)
        css = vendor / "bootstrap.min.css"
        css.write_text(shape)
        digest = hashlib.sha256(css.read_bytes()).hexdigest()
        (vendor / "README.md").write_text(
            "# Vendored browser assets\n\n## Bootstrap 5.3.3\n\n"
            f"source https://example.invalid/bootstrap.min.css\nsha256 {digest}\n"
        )

        verified = _run_scanner(root)
        assert verified.returncode == 0, f"a verified vendored asset must be skipped:\n{verified.stdout}"
        assert "skipped" in verified.stdout and "bootstrap.min.css" in verified.stdout, (
            "the scan summary must name the file it skipped:\n" + verified.stdout
        )

        # The recorded digest no longer matches: the file is scanned again and the shape is reported.
        css.write_text(shape.rstrip("\n") + "/* edited */\n")
        modified = _run_scanner(root)
        assert modified.returncode == 1, (
            "a modified vendored file must be scanned like any other:\n" + modified.stdout
        )
        assert "bootstrap.min.css" in modified.stdout and "[coordinate]" in modified.stdout, modified.stdout

        # A substitution with an unrecorded digest is a finding for the same reason.
        css.write_text(shape)
        (vendor / "README.md").write_text(
            "# Vendored browser assets\n\n## Bootstrap 5.3.3\n\n"
            f"source https://example.invalid/bootstrap.min.css\nsha256 {'0' * 64}\n"
        )
        mismatched = _run_scanner(root)
        assert mismatched.returncode == 1, (
            "a digest that does not match the file must not buy an exclusion:\n" + mismatched.stdout
        )


def test_vendored_asset_exclusion_is_visible_in_the_real_tree():
    """The committed tree must be clean *and* the summary must say what was skipped."""
    result = _run_scanner(ROOT)
    assert result.returncode == 0, result.stdout
    assert "vendor/bootstrap.min.css" in result.stdout, result.stdout
    assert "sha256 verified" in result.stdout, result.stdout
