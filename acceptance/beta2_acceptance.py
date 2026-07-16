"""Owner-frozen acceptance for Development Governor v0.1.0-beta.2."""

import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from development_governor import cli


ROOT = Path(__file__).resolve().parents[1]


def assert_verify_exit_status() -> None:
    cases = (("verification_failed", 1), ("verification_passed", 0))
    for status, expected in cases:
        output = io.StringIO()
        with patch.object(cli, "verify_task", return_value={"status": status}):
            with redirect_stdout(output):
                actual = cli.main(["verify", "--repo", str(ROOT)])
        payload = json.loads(output.getvalue())
        assert payload["status"] == status
        assert actual == expected, (status, expected, actual)


def timeout_test_method() -> ast.FunctionDef:
    path = ROOT / "tests" / "test_development_governor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "test_timeout_stops_root_without_retry":
            return node
    raise AssertionError("timeout regression test is missing")


def assert_timeout_test_uses_controller_evidence() -> None:
    method = timeout_test_method()
    receipt_keys = {
        node.slice.value
        for node in ast.walk(method)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "receipt"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert {"status", "reason", "timed_out", "invocation_count"}.issubset(receipt_keys)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "marker"
        for node in ast.walk(method)
    ), "timeout invariant must not depend on a child-process marker"

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "test_development_governor.DevelopmentGovernorTests.test_timeout_stops_root_without_retry",
            "-v",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def assert_beta2_packaging() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.1.0b2"' in pyproject
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "@v0.1.0-beta.2" in readme
    assert "@v0.1.0-beta.1" not in readme
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.0b2 - 2026-07-16" in changelog
    notes = ROOT / "docs" / "releases" / "v0.1.0-beta.2.md"
    assert notes.is_file()
    content = notes.read_text(encoding="utf-8")
    assert "verification_failed" in content
    assert "non-zero" in content


if __name__ == "__main__":
    assert_verify_exit_status()
    assert_timeout_test_uses_controller_evidence()
    assert_beta2_packaging()
    print("BETA2_ACCEPTANCE=PASS")
