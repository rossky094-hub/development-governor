"""Self-contained, zero-model demonstration of the public control boundary."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional

from development_governor.hook_guard import evaluate_hook_event
from development_governor.project_entry import (
    close_task,
    enroll_project,
    prepare_task,
    start_task,
    verify_task,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _permission(result: Dict[str, Any]) -> str:
    try:
        return result["hookSpecificOutput"]["permissionDecision"]
    except (KeyError, TypeError):
        return "allow" if not result else "unknown"


def _run_demo(root: Path) -> Dict[str, Any]:
    demo_root = root / "development-governor-demo"
    repo = demo_root / "project"
    state_root = demo_root / "state"
    repo.mkdir(parents=True)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Development Governor Demo")
    _git(repo, "config", "user.email", "demo@example.invalid")
    _write(repo / "src" / "value.txt", "before\n")
    _write(
        repo / "acceptance" / "check.py",
        "from pathlib import Path\n"
        "if Path('src/value.txt').read_text(encoding='utf-8') != 'after\\n':\n"
        "    raise SystemExit('expected the governed product change')\n"
        "print('demo acceptance passed')\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "demo baseline")

    acceptance_path = repo / "acceptance" / "check.py"
    acceptance_sha256 = hashlib.sha256(acceptance_path.read_bytes()).hexdigest()
    limits = {
        "max_attempts": 1,
        "max_review_waves": 0,
        "max_elapsed_seconds": 120,
        "lease_seconds": 120,
        "max_parallel_agents": 1,
        "max_total_agents": 1,
    }
    enrollment = enroll_project(
        {
            "schema_version": "development-governor-project-policy.v0",
            "repo_path": str(repo),
            "owner_authorization_ref": "demo:synthetic-owner-authorization",
            "allowed_paths": ["src/"],
            "protected_paths": ["acceptance/"],
            "acceptance_definitions": [
                {
                    "acceptance_id": "demo-acceptance",
                    "argv": [sys.executable, "acceptance/check.py"],
                    "files": [
                        {
                            "path": "acceptance/check.py",
                            "sha256": acceptance_sha256,
                        }
                    ],
                }
            ],
            "limits": limits,
        },
        state_root=state_root,
    )
    prepared = prepare_task(
        {
            "schema_version": "development-governor-task-capsule.v2",
            "repo_path": str(repo),
            "owner_request_ref": "demo:bounded-product-slice",
            "result": "Change the demo product value from before to after.",
            "primary_mode": "product",
            "capability_transition": {
                "capability_id": "demo-product-value",
                "from_state": "before",
                "to_state": "after",
            },
            "constraints": ["Do not modify the frozen acceptance script."],
            "evidence_inputs": [
                {"path": "acceptance/check.py", "sha256": acceptance_sha256}
            ],
            "acceptance_ids": ["demo-acceptance"],
            "deliverable_paths": ["src/value.txt"],
            "product_evidence_paths": ["src/value.txt"],
            "limits": limits,
            "lanes": [],
        },
        state_root=state_root,
    )
    lease = start_task(prepared["task_hash"], state_root=state_root)

    allowed = evaluate_hook_event(
        {
            "cwd": str(repo),
            "tool_name": "apply_patch",
            "tool_input": "*** Begin Patch\n*** Update File: src/value.txt\n*** End Patch",
        },
        state_root=state_root,
    )
    blocked = evaluate_hook_event(
        {
            "cwd": str(repo),
            "tool_name": "apply_patch",
            "tool_input": "*** Begin Patch\n*** Update File: README.md\n*** End Patch",
        },
        state_root=state_root,
    )
    if _permission(allowed) != "allow" or _permission(blocked) != "deny":
        raise RuntimeError("demo mutation guard did not produce the expected decisions")

    _write(repo / "src" / "value.txt", "after\n")
    verification = verify_task(repo, state_root=state_root)
    closure = close_task(repo, state_root=state_root)
    acceptance_stdout = verification["results"][0]["stdout"]
    return {
        "status": "demo_passed",
        "model_invocations": 0,
        "enrollment": enrollment["status"],
        "lease": lease["status"],
        "allowed_mutation": "allowed",
        "blocked_mutation": "denied_outside_scope",
        "verification": verification["status"],
        "closure": closure["status"],
        "acceptance_stdout": acceptance_stdout,
    }


def run_demo(work_root: Optional[Path] = None) -> Dict[str, Any]:
    """Run the deterministic demo without reading the user's Governor state."""

    if work_root is not None:
        return _run_demo(Path(work_root))
    with tempfile.TemporaryDirectory(prefix="development-governor-demo-") as directory:
        return _run_demo(Path(directory))
