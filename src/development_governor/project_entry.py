"""Deterministic external project entry and lease state for Development Governor."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


DEFAULT_STATE_ROOT = Path(
    os.environ.get(
        "DEVELOPMENT_GOVERNOR_STATE_ROOT",
        str(Path.home() / ".codex" / "development-governor" / "v0"),
    )
).expanduser()
POLICY_SCHEMA = "development-governor-project-policy.v0"
TASK_SCHEMA = "development-governor-task-capsule.v1"


class ProjectEntryError(ValueError):
    """Raised when an external project-entry transition is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _path_set_hash(root: Path, paths: Sequence[str]) -> str:
    from development_governor.runner import hash_path_set

    return hash_path_set(root, paths)


def _load_json(path: Path) -> Mapping[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ProjectEntryError("JSON root must be an object")
    return raw


def _load_source(source: Any) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    return _load_json(Path(source))


def _require_exact_keys(raw: Mapping[str, Any], expected: Iterable[str], label: str) -> None:
    expected_set = set(expected)
    missing = sorted(expected_set.difference(raw))
    extra = sorted(set(raw).difference(expected_set))
    if missing:
        raise ProjectEntryError(f"{label} missing fields: " + ", ".join(missing))
    if extra:
        raise ProjectEntryError(f"{label} unsupported fields: " + ", ".join(extra))


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectEntryError(f"{label} must be a non-empty string")
    return value.strip()


def _relative_path(value: Any, label: str, *, directory_hint: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectEntryError(f"{label} must contain repository-relative paths")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized.startswith("./"):
        raise ProjectEntryError(f"{label} must contain repository-relative paths")
    clean = str(path)
    if clean in ("", "."):
        raise ProjectEntryError(f"{label} must contain repository-relative paths")
    if (directory_hint or normalized.endswith("/")) and not clean.endswith("/"):
        clean += "/"
    return clean


def _path_array(value: Any, label: str, *, allow_empty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        raise ProjectEntryError(f"{label} must be a non-empty array")
    result = tuple(_relative_path(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ProjectEntryError(f"{label} entries must be unique")
    return result


def _string_array(value: Any, label: str, *, allow_empty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        raise ProjectEntryError(f"{label} must be a non-empty array")
    result = tuple(_nonempty_string(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ProjectEntryError(f"{label} entries must be unique")
    return result


def _validated_evidence_inputs(value: Any, repo: Path) -> Tuple[Mapping[str, str], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ProjectEntryError("evidence_inputs must be a non-empty array")
    records = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ProjectEntryError("evidence input must be an object")
        _require_exact_keys(item, {"path", "sha256"}, "evidence input")
        path = _relative_path(item["path"], "evidence inputs")
        digest = _nonempty_string(item["sha256"], "evidence input sha256")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ProjectEntryError(
                "evidence input sha256 must be lowercase hexadecimal"
            )
        if path in seen:
            raise ProjectEntryError("evidence input paths must be unique")
        seen.add(path)
        actual = repo / path
        if not actual.is_file() or _file_sha256(actual) != digest:
            raise ProjectEntryError("evidence input hash mismatch: " + path)
        records.append({"path": path, "sha256": digest})
    return tuple(records)


def _verify_evidence_inputs(task: Mapping[str, Any]) -> None:
    repo = Path(task["project_identity"]["repo_path"])
    for item in task["evidence_inputs"]:
        path = repo / item["path"]
        if not path.is_file() or _file_sha256(path) != item["sha256"]:
            raise ProjectEntryError("evidence input hash mismatch: " + item["path"])


@contextmanager
def _isolated_repository_snapshot(repo_path: Path):
    repo = Path(repo_path).resolve()
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        shell=False,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ProjectEntryError("cannot enumerate repository snapshot")
    raw_paths = [item for item in completed.stdout.split(b"\0") if item]
    with tempfile.TemporaryDirectory(prefix="development-governor-snapshot-") as directory:
        snapshot = Path(directory) / "repo"
        snapshot.mkdir()
        for raw_path in raw_paths:
            relative = _relative_path(os.fsdecode(raw_path), "repository snapshot")
            source = repo / relative
            if not source.exists() and not source.is_symlink():
                continue
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                try:
                    source.resolve().relative_to(repo)
                except ValueError as error:
                    raise ProjectEntryError(
                        "repository snapshot contains an escaping symlink: " + relative
                    ) from error
                target.symlink_to(os.readlink(source))
            elif source.is_file():
                shutil.copy2(source, target)
            else:
                raise ProjectEntryError(
                    "repository snapshot supports files only: " + relative
                )
        yield snapshot


def _path_matches(path: str, prefix: str) -> bool:
    left = path.rstrip("/")
    right = prefix.rstrip("/")
    return left == right or left.startswith(right + "/")


def _paths_overlap(left: str, right: str) -> bool:
    return _path_matches(left, right) or _path_matches(right, left)


def _inside_any(path: str, allowed: Sequence[str]) -> bool:
    return any(_path_matches(path, item) for item in allowed)


def _positive_limits(raw: Any, label: str) -> Dict[str, int]:
    fields = {
        "max_attempts",
        "max_review_waves",
        "max_elapsed_seconds",
        "lease_seconds",
        "max_parallel_agents",
        "max_total_agents",
    }
    if not isinstance(raw, Mapping):
        raise ProjectEntryError(f"{label} must be an object")
    _require_exact_keys(raw, fields, label)
    result: Dict[str, int] = {}
    for field in sorted(fields):
        value = raw[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProjectEntryError(f"{label}.{field} must be an integer")
        if field == "max_review_waves":
            if value < 0:
                raise ProjectEntryError(f"{label}.{field} must be non-negative")
        elif value <= 0:
            raise ProjectEntryError(f"{label}.{field} must be positive")
        result[field] = value
    if result["max_total_agents"] < result["max_parallel_agents"]:
        raise ProjectEntryError("max_total_agents cannot be below max_parallel_agents")
    if result["lease_seconds"] > result["max_elapsed_seconds"]:
        raise ProjectEntryError("lease_seconds cannot exceed max_elapsed_seconds")
    return result


def canonical_project_identity(repo_path: Path) -> Mapping[str, str]:
    """Return a stable identity shared by all worktrees of one Git repository."""

    candidate = Path(repo_path).expanduser().resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        common = subprocess.run(
            [
                "git",
                "-C",
                str(candidate),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProjectEntryError(f"not a readable Git repository: {candidate}") from error
    top_path = str(Path(top).resolve())
    common_path = str(Path(common).resolve())
    project_id = hashlib.sha256(common_path.encode("utf-8")).hexdigest()
    return {
        "project_id": project_id,
        "repo_path": top_path,
        "git_common_dir": common_path,
    }


def _ensure_state_root(state_root: Path) -> Path:
    root = Path(state_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _project_dir(state_root: Path, project_id: str) -> Path:
    return _ensure_state_root(state_root) / "projects" / project_id


@contextmanager
def _project_lock(state_root: Path, project_id: str):
    directory = _project_dir(state_root, project_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / ".lock"
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = _canonical_bytes(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _validated_acceptance(raw: Any, repo: Path, protected: Sequence[str]) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ProjectEntryError("acceptance_definitions must be a non-empty array")
    results = []
    seen = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ProjectEntryError("acceptance definition must be an object")
        _require_exact_keys(item, {"acceptance_id", "argv", "files"}, "acceptance definition")
        acceptance_id = _nonempty_string(item["acceptance_id"], "acceptance_id")
        if acceptance_id in seen:
            raise ProjectEntryError("acceptance_id values must be unique")
        seen.add(acceptance_id)
        argv = item["argv"]
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(part, str) or not part for part in argv)
        ):
            raise ProjectEntryError("acceptance argv must be a non-empty string array")
        files_raw = item["files"]
        if not isinstance(files_raw, (list, tuple)) or not files_raw:
            raise ProjectEntryError("acceptance files must be a non-empty array")
        files = []
        for file_item in files_raw:
            if not isinstance(file_item, Mapping):
                raise ProjectEntryError("acceptance file must be an object")
            _require_exact_keys(file_item, {"path", "sha256"}, "acceptance file")
            path = _relative_path(file_item["path"], "acceptance files")
            digest = _nonempty_string(file_item["sha256"], "acceptance sha256")
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ProjectEntryError("acceptance sha256 must be lowercase hexadecimal")
            if not _inside_any(path, protected):
                raise ProjectEntryError("acceptance files must be inside protected_paths")
            actual_path = repo / path
            if not actual_path.is_file() or _file_sha256(actual_path) != digest:
                raise ProjectEntryError(f"acceptance file hash mismatch: {path}")
            files.append({"path": path, "sha256": digest})
        results.append(
            {
                "acceptance_id": acceptance_id,
                "argv": list(argv),
                "files": files,
            }
        )
    return tuple(results)


def _validated_policy(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = {
        "schema_version",
        "repo_path",
        "owner_authorization_ref",
        "allowed_paths",
        "protected_paths",
        "acceptance_definitions",
        "limits",
    }
    _require_exact_keys(raw, fields, "project policy")
    if raw["schema_version"] != POLICY_SCHEMA:
        raise ProjectEntryError("unsupported project policy schema_version")
    identity = canonical_project_identity(Path(_nonempty_string(raw["repo_path"], "repo_path")))
    repo = Path(identity["repo_path"])
    allowed = _path_array(raw["allowed_paths"], "allowed_paths")
    protected = _path_array(raw["protected_paths"], "protected_paths")
    if any(_paths_overlap(left, right) for left in allowed for right in protected):
        raise ProjectEntryError("allowed_paths and protected_paths must not overlap")
    acceptance = _validated_acceptance(raw["acceptance_definitions"], repo, protected)
    return {
        "schema_version": POLICY_SCHEMA,
        "project_identity": dict(identity),
        "owner_authorization_ref": _nonempty_string(
            raw["owner_authorization_ref"], "owner_authorization_ref"
        ),
        "allowed_paths": list(allowed),
        "protected_paths": list(protected),
        "acceptance_definitions": list(acceptance),
        "limits": _positive_limits(raw["limits"], "limits"),
    }


def enroll_project(policy_source: Any, *, state_root: Path = DEFAULT_STATE_ROOT) -> Mapping[str, Any]:
    policy = _validated_policy(_load_source(policy_source))
    project_id = policy["project_identity"]["project_id"]
    policy_hash = _sha256(policy)
    with _project_lock(state_root, project_id) as directory:
        policy_path = directory / "policy.json"
        if policy_path.exists():
            existing = _load_json(policy_path)
            if existing.get("policy_hash") != policy_hash:
                raise ProjectEntryError("conflicting enrolled policy requires Owner-controlled migration")
            return {
                "status": "already_enrolled",
                "project_id": project_id,
                "policy_hash": policy_hash,
                "policy_path": str(policy_path),
            }
        _atomic_json(policy_path, {"policy_hash": policy_hash, "policy": policy})
    return {
        "status": "enrolled",
        "project_id": project_id,
        "policy_hash": policy_hash,
        "policy_path": str(policy_path),
    }


def migrate_project_policy(
    policy_source: Any,
    *,
    expected_policy_hash: str,
    owner_authorization_ref: str,
    state_root: Path = DEFAULT_STATE_ROOT,
    now=None,
) -> Mapping[str, Any]:
    """Replace one enrolled policy under an exact Owner-authorized hash transition."""

    expected = _nonempty_string(expected_policy_hash, "expected policy hash")
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ProjectEntryError("expected policy hash must be lowercase hexadecimal")
    owner_ref = _nonempty_string(
        owner_authorization_ref, "owner_authorization_ref"
    )
    replacement = _validated_policy(_load_source(policy_source))
    project_id = replacement["project_identity"]["project_id"]
    new_policy_hash = _sha256(replacement)
    timestamp = _clock_value(now)
    with _project_lock(state_root, project_id) as directory:
        policy_path = directory / "policy.json"
        if not policy_path.is_file():
            raise ProjectEntryError("needs_owner_enrollment")
        enrolled = _load_json(policy_path)
        if set(enrolled) != {"policy_hash", "policy"}:
            raise ProjectEntryError("invalid enrolled project policy")
        current_policy_hash = enrolled["policy_hash"]
        if current_policy_hash != expected:
            raise ProjectEntryError("expected policy hash does not match current policy")
        if _sha256(enrolled["policy"]) != current_policy_hash:
            raise ProjectEntryError("enrolled project policy hash mismatch")
        if directory.joinpath("active-lease.json").exists():
            raise ProjectEntryError("policy migration requires no active lease")
        if new_policy_hash == current_policy_hash:
            raise ProjectEntryError("replacement policy is unchanged")
        history_path = directory / "policy-history" / (current_policy_hash + ".json")
        if history_path.exists() and _load_json(history_path) != enrolled:
            raise ProjectEntryError("policy history hash collision")
        _atomic_json(history_path, enrolled)
        receipt = {
            "schema_version": "development-governor-policy-migration-receipt.v0",
            "project_id": project_id,
            "old_policy_hash": current_policy_hash,
            "new_policy_hash": new_policy_hash,
            "owner_authorization_ref": owner_ref,
            "migrated_at": timestamp,
            "old_policy_history": str(history_path),
        }
        receipt_path = directory / "policy-migrations" / (
            f"{int(timestamp * 1000000)}-{new_policy_hash}.json"
        )
        try:
            _atomic_json(
                policy_path,
                {"policy_hash": new_policy_hash, "policy": replacement},
            )
            _atomic_json(receipt_path, receipt)
        except BaseException:
            _atomic_json(policy_path, enrolled)
            if receipt_path.exists():
                receipt_path.unlink()
            raise
    return {
        "status": "policy_migrated",
        "project_id": project_id,
        "old_policy_hash": current_policy_hash,
        "new_policy_hash": new_policy_hash,
        "migration_receipt": str(receipt_path),
    }


def _load_enrollment(identity: Mapping[str, str], state_root: Path) -> Tuple[Path, Mapping[str, Any]]:
    directory = _project_dir(state_root, identity["project_id"])
    policy_path = directory / "policy.json"
    if not policy_path.is_file():
        raise ProjectEntryError("needs_owner_enrollment")
    enrolled = _load_json(policy_path)
    if set(enrolled) != {"policy_hash", "policy"} or not isinstance(enrolled["policy"], Mapping):
        raise ProjectEntryError("invalid enrolled project policy")
    if _sha256(enrolled["policy"]) != enrolled["policy_hash"]:
        raise ProjectEntryError("enrolled project policy hash mismatch")
    if enrolled["policy"]["project_identity"]["project_id"] != identity["project_id"]:
        raise ProjectEntryError("enrolled project identity mismatch")
    return directory, enrolled


def _validated_task(raw: Mapping[str, Any], enrolled: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = {
        "schema_version",
        "repo_path",
        "owner_request_ref",
        "result",
        "constraints",
        "evidence_inputs",
        "acceptance_ids",
        "deliverable_paths",
        "limits",
        "lanes",
    }
    _require_exact_keys(raw, fields, "task capsule")
    if raw["schema_version"] != TASK_SCHEMA:
        raise ProjectEntryError("unsupported task capsule schema_version")
    identity = canonical_project_identity(Path(_nonempty_string(raw["repo_path"], "repo_path")))
    policy = enrolled["policy"]
    if identity["project_id"] != policy["project_identity"]["project_id"]:
        raise ProjectEntryError("task capsule repository is not the enrolled project")
    result = _nonempty_string(raw["result"], "result")
    constraints = _string_array(raw["constraints"], "constraints")
    evidence = _validated_evidence_inputs(raw["evidence_inputs"], Path(identity["repo_path"]))
    acceptance_ids = _string_array(raw["acceptance_ids"], "acceptance_ids")
    known_acceptance = {
        item["acceptance_id"] for item in policy["acceptance_definitions"]
    }
    unknown = sorted(set(acceptance_ids).difference(known_acceptance))
    if unknown:
        raise ProjectEntryError("unknown acceptance IDs: " + ", ".join(unknown))
    deliverables = _path_array(raw["deliverable_paths"], "deliverable_paths")
    if not all(_inside_any(path, policy["allowed_paths"]) for path in deliverables):
        raise ProjectEntryError("deliverable_paths must remain inside project allowed_paths")
    for deliverable in deliverables:
        for evidence_input in evidence:
            if _paths_overlap(deliverable, evidence_input["path"]):
                raise ProjectEntryError(
                    "invalid_capsule_mutable_deliverable_declared_as_evidence_input: "
                    + deliverable
                    + " overlaps "
                    + evidence_input["path"]
                )
    limits = _positive_limits(raw["limits"], "limits")
    for key, value in limits.items():
        if value > policy["limits"][key]:
            raise ProjectEntryError(f"task limit exceeds project policy: {key}")

    lanes_raw = raw["lanes"]
    if not isinstance(lanes_raw, (list, tuple)):
        raise ProjectEntryError("lanes must be an array")
    lanes = []
    if not lanes_raw:
        if limits["max_parallel_agents"] != 1 or limits["max_total_agents"] != 1:
            raise ProjectEntryError("serial tasks require one parallel and total agent")
    else:
        if len(lanes_raw) < 2:
            raise ProjectEntryError("parallel tasks require at least two lanes")
        if limits["max_parallel_agents"] < 2:
            raise ProjectEntryError("parallel lanes require max_parallel_agents >= 2")
        if limits["max_parallel_agents"] > len(lanes_raw) or limits["max_total_agents"] > len(lanes_raw):
            raise ProjectEntryError("agent limits cannot exceed declared lane count")
        seen_lane_ids = set()
        seen_deliverables = []
        seen_acceptance = set()
        for item in lanes_raw:
            if not isinstance(item, Mapping):
                raise ProjectEntryError("lane must be an object")
            _require_exact_keys(item, {"lane_id", "deliverable_paths", "acceptance_ids"}, "lane")
            lane_id = _nonempty_string(item["lane_id"], "lane_id")
            if lane_id in seen_lane_ids:
                raise ProjectEntryError("lane_id values must be unique")
            seen_lane_ids.add(lane_id)
            lane_deliverables = _path_array(item["deliverable_paths"], "lane deliverable_paths")
            lane_acceptance = _string_array(item["acceptance_ids"], "lane acceptance_ids")
            for path in lane_deliverables:
                if path not in deliverables or any(_paths_overlap(path, prior) for prior in seen_deliverables):
                    raise ProjectEntryError("parallel lanes require independent deliverable paths")
                seen_deliverables.append(path)
            if not set(lane_acceptance).issubset(acceptance_ids) or set(lane_acceptance).intersection(seen_acceptance):
                raise ProjectEntryError("parallel lanes require independent acceptance IDs")
            seen_acceptance.update(lane_acceptance)
            lanes.append(
                {
                    "lane_id": lane_id,
                    "deliverable_paths": list(lane_deliverables),
                    "acceptance_ids": list(lane_acceptance),
                }
            )
        if set(seen_deliverables) != set(deliverables):
            raise ProjectEntryError("every deliverable path must belong to one lane")
        if seen_acceptance != set(acceptance_ids):
            raise ProjectEntryError("every acceptance ID must belong to one lane")
    return {
        "schema_version": TASK_SCHEMA,
        "project_identity": dict(identity),
        "policy_hash": enrolled["policy_hash"],
        "owner_request_ref": _nonempty_string(raw["owner_request_ref"], "owner_request_ref"),
        "result": result,
        "constraints": list(constraints),
        "evidence_inputs": [dict(item) for item in evidence],
        "acceptance_ids": list(acceptance_ids),
        "deliverable_paths": list(deliverables),
        "baseline_product_tree_hash": _path_set_hash(
            Path(identity["repo_path"]), deliverables
        ),
        "limits": limits,
        "lanes": lanes,
    }


def prepare_task(capsule_source: Any, *, state_root: Path = DEFAULT_STATE_ROOT) -> Mapping[str, Any]:
    raw = _load_source(capsule_source)
    identity = canonical_project_identity(Path(_nonempty_string(raw.get("repo_path"), "repo_path")))
    _, enrolled = _load_enrollment(identity, state_root)
    task = _validated_task(raw, enrolled)
    task_hash = _sha256(task)
    with _project_lock(state_root, identity["project_id"]) as directory:
        task_path = directory / "tasks" / task_hash / "task.json"
        if task_path.exists():
            if _load_json(task_path) != task:
                raise ProjectEntryError("prepared task hash collision")
        else:
            _atomic_json(task_path, task)
    return {
        "status": "prepared",
        "project_id": identity["project_id"],
        "policy_hash": enrolled["policy_hash"],
        "task_hash": task_hash,
        "task_path": str(task_path),
    }


def load_prepared_task(task_ref: str, *, state_root: Path = DEFAULT_STATE_ROOT) -> Mapping[str, Any]:
    path = Path(task_ref).expanduser()
    if path.is_file():
        task = _load_json(path)
        task_hash = _sha256(task)
        try:
            project_id = task["project_identity"]["project_id"]
        except (KeyError, TypeError) as error:
            raise ProjectEntryError("prepared task has no project identity") from error
        expected = _project_dir(state_root, project_id) / "tasks" / task_hash / "task.json"
        if path.resolve() != expected.resolve():
            raise ProjectEntryError("prepared task reference is outside Governor external state")
    else:
        matches = list((_ensure_state_root(state_root) / "projects").glob(f"*/tasks/{task_ref}/task.json"))
        if len(matches) != 1:
            raise ProjectEntryError("prepared task reference not found or ambiguous")
        path = matches[0]
        task = _load_json(path)
    if _sha256(task) != path.parent.name:
        raise ProjectEntryError("prepared task hash mismatch")
    return task


def project_status(repo_path: Path, *, state_root: Path = DEFAULT_STATE_ROOT, now=None) -> Mapping[str, Any]:
    identity = canonical_project_identity(repo_path)
    directory = _project_dir(state_root, identity["project_id"])
    if not (directory / "policy.json").is_file():
        return {"status": "unregistered", "project_id": identity["project_id"], "lease_status": "none"}
    lease_path = directory / "active-lease.json"
    lease_status = "none"
    task_hash = None
    if lease_path.is_file():
        try:
            lease = _load_json(lease_path)
            task_hash = lease.get("task_hash")
            lease_status = "expired" if _clock_value(now) >= float(lease["expires_at"]) else "active"
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            lease_status = "invalid"
    return {
        "status": "enrolled",
        "project_id": identity["project_id"],
        "lease_status": lease_status,
        "task_hash": task_hash,
    }


def _clock_value(now=None) -> float:
    if now is None:
        return time.time()
    if callable(now):
        return float(now())
    return float(now)


def _task_paths(state_root: Path, task: Mapping[str, Any]) -> Tuple[Path, Path, str]:
    project_id = task["project_identity"]["project_id"]
    task_hash = _sha256(task)
    directory = _project_dir(state_root, project_id)
    return directory, directory / "tasks" / task_hash, task_hash


def _load_task_state(task_dir: Path, task_hash: str) -> Dict[str, Any]:
    path = task_dir / "state.json"
    if not path.exists():
        return {
            "task_hash": task_hash,
            "attempts_used": 0,
            "review_waves_used": 0,
            "status": "prepared",
            "last_lease_id": None,
            "verification_receipt": None,
            "first_started_at": None,
            "deadline_at": None,
        }
    raw = dict(_load_json(path))
    expected = {
        "task_hash",
        "attempts_used",
        "review_waves_used",
        "status",
        "last_lease_id",
        "verification_receipt",
        "first_started_at",
        "deadline_at",
    }
    if set(raw) != expected or raw["task_hash"] != task_hash:
        raise ProjectEntryError("invalid prepared task state")
    return raw


def _archive_lease(task_dir: Path, lease: Mapping[str, Any]) -> Path:
    lease_id = _nonempty_string(lease.get("lease_id"), "lease_id")
    archive = task_dir / "leases" / f"{lease_id}.json"
    _atomic_json(archive, lease)
    return archive


def start_task(task_ref: str, *, state_root: Path = DEFAULT_STATE_ROOT, now=None) -> Mapping[str, Any]:
    """Activate one prepared task without launching a model."""

    task = load_prepared_task(task_ref, state_root=state_root)
    directory, task_dir, task_hash = _task_paths(state_root, task)
    timestamp = _clock_value(now)
    project_id = task["project_identity"]["project_id"]
    with _project_lock(state_root, project_id):
        _, enrolled = _load_enrollment(task["project_identity"], state_root)
        if enrolled["policy_hash"] != task["policy_hash"]:
            raise ProjectEntryError("prepared task policy hash mismatch")
        _verify_evidence_inputs(task)
        task_path = task_dir / "task.json"
        if not task_path.is_file() or _sha256(_load_json(task_path)) != task_hash:
            raise ProjectEntryError("prepared task hash mismatch")
        active_path = directory / "active-lease.json"
        state = _load_task_state(task_dir, task_hash)
        if state["status"] == "closed":
            raise ProjectEntryError("verified closed task is terminal")
        if state["status"] == "aborted":
            raise ProjectEntryError("Owner-aborted task is terminal")
        if active_path.is_file():
            active = _load_json(active_path)
            try:
                active_expires = float(active["expires_at"])
                active_task = active["task_hash"]
            except (KeyError, TypeError, ValueError) as error:
                raise ProjectEntryError("invalid active project lease") from error
            if active_expires > timestamp:
                if active_task == task_hash:
                    return {
                        "status": "already_active",
                        "project_id": project_id,
                        "task_hash": task_hash,
                        "lease_id": active["lease_id"],
                        "expires_at": active_expires,
                    }
                raise ProjectEntryError("another task owns the active project lease")
            old_task_dir = directory / "tasks" / str(active_task)
            if old_task_dir.is_dir():
                _archive_lease(old_task_dir, active)
                old_state = _load_task_state(old_task_dir, str(active_task))
                old_state["status"] = "expired"
                _atomic_json(old_task_dir / "state.json", old_state)
            active_path.unlink()
        if state["attempts_used"] >= task["limits"]["max_attempts"]:
            raise ProjectEntryError("attempt budget exhausted; needs_owner_decision")
        first_started_at = (
            timestamp
            if state["first_started_at"] is None
            else float(state["first_started_at"])
        )
        deadline_at = (
            first_started_at + task["limits"]["max_elapsed_seconds"]
            if state["deadline_at"] is None
            else float(state["deadline_at"])
        )
        if timestamp >= deadline_at:
            raise ProjectEntryError("elapsed budget exhausted; needs_owner_decision")
        attempt = int(state["attempts_used"]) + 1
        expires_at = min(
            timestamp + task["limits"]["lease_seconds"], deadline_at
        )
        lease_seed = {
            "project_id": project_id,
            "policy_hash": task["policy_hash"],
            "task_hash": task_hash,
            "attempt": attempt,
            "started_at": timestamp,
            "expires_at": expires_at,
        }
        lease = {
            "schema_version": "development-governor-project-lease.v0",
            **lease_seed,
            "lease_id": _sha256(lease_seed),
            "allowed_paths": list(task["deliverable_paths"]),
            "max_review_waves": task["limits"]["max_review_waves"],
            "max_parallel_agents": task["limits"]["max_parallel_agents"],
            "max_total_agents": task["limits"]["max_total_agents"],
        }
        state.update(
            {
                "attempts_used": attempt,
                "status": "active",
                "last_lease_id": lease["lease_id"],
                "verification_receipt": None,
                "first_started_at": first_started_at,
                "deadline_at": deadline_at,
            }
        )
        _atomic_json(task_dir / "state.json", state)
        _atomic_json(active_path, lease)
    return {
        "status": "active",
        "project_id": project_id,
        "task_hash": task_hash,
        "lease_id": lease["lease_id"],
        "expires_at": expires_at,
        "attempts_used": attempt,
    }


def authorize_mutation(repo_path: Path, *, state_root: Path = DEFAULT_STATE_ROOT, now=None) -> Mapping[str, Any]:
    """Return a deterministic lease decision for one repository mutation."""

    identity = canonical_project_identity(repo_path)
    try:
        directory, enrolled = _load_enrollment(identity, state_root)
    except ProjectEntryError as error:
        if str(error) == "needs_owner_enrollment":
            return {"allowed": False, "reason": "project_not_enrolled", "project_id": identity["project_id"]}
        raise
    active_path = directory / "active-lease.json"
    if not active_path.is_file():
        return {"allowed": False, "reason": "no_active_lease", "project_id": identity["project_id"]}
    try:
        lease = _load_json(active_path)
        required = {
            "schema_version",
            "project_id",
            "policy_hash",
            "task_hash",
            "attempt",
            "started_at",
            "expires_at",
            "lease_id",
            "allowed_paths",
            "max_review_waves",
            "max_parallel_agents",
            "max_total_agents",
        }
        if set(lease) != required:
            raise ProjectEntryError("invalid active project lease")
        if lease["project_id"] != identity["project_id"]:
            raise ProjectEntryError("active lease project mismatch")
        if lease["policy_hash"] != enrolled["policy_hash"]:
            raise ProjectEntryError("active lease policy mismatch")
        if _clock_value(now) >= float(lease["expires_at"]):
            return {"allowed": False, "reason": "lease_expired", "project_id": identity["project_id"]}
        task_path = directory / "tasks" / lease["task_hash"] / "task.json"
        if not task_path.is_file() or _sha256(_load_json(task_path)) != lease["task_hash"]:
            raise ProjectEntryError("active lease task hash mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as error:
        if isinstance(error, ProjectEntryError):
            raise
        raise ProjectEntryError("invalid active project lease") from error
    return {
        "allowed": True,
        "reason": "active_matching_lease",
        "project_id": identity["project_id"],
        "task_hash": lease["task_hash"],
        "lease_id": lease["lease_id"],
        "allowed_paths": list(lease["allowed_paths"]),
        "protected_paths": list(enrolled["policy"]["protected_paths"]),
        "expires_at": lease["expires_at"],
    }


def _run_snapshot_command(
    repo_path: Path, argv: Sequence[str], *, timeout: float
) -> Mapping[str, Any]:
    if not isinstance(argv, (list, tuple)) or not argv or any(
        not isinstance(part, str) or not part for part in argv
    ):
        raise ProjectEntryError("isolated command argv must be a non-empty string array")
    with _isolated_repository_snapshot(repo_path) as snapshot:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=snapshot,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout[-20000:],
                "stderr": completed.stderr[-20000:],
                "execution_mode": "isolated_snapshot",
            }
        except subprocess.TimeoutExpired as error:
            return {
                "returncode": None,
                "stdout": (error.stdout or "")[-20000:]
                if isinstance(error.stdout, str)
                else "",
                "stderr": "isolated command timed out",
                "execution_mode": "isolated_snapshot",
            }


def run_isolated_check(
    repo_path: Path,
    argv: Sequence[str],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now=None,
) -> Mapping[str, Any]:
    """Run a non-promoting check in a disposable repository snapshot."""

    decision = authorize_mutation(repo_path, state_root=state_root, now=now)
    if not decision["allowed"]:
        raise ProjectEntryError("mutation lease not active: " + decision["reason"])
    identity = canonical_project_identity(repo_path)
    directory, enrolled = _load_enrollment(identity, state_root)
    task_dir = directory / "tasks" / decision["task_hash"]
    task = _load_json(task_dir / "task.json")
    _verify_evidence_inputs(task)
    timestamp = _clock_value(now)
    remaining = max(1.0, float(decision["expires_at"]) - timestamp)
    result = _run_snapshot_command(
        Path(identity["repo_path"]), argv, timeout=min(300.0, remaining)
    )
    receipt = {
        "schema_version": "development-governor-isolated-check-receipt.v0",
        "project_id": identity["project_id"],
        "policy_hash": enrolled["policy_hash"],
        "task_hash": decision["task_hash"],
        "lease_id": decision["lease_id"],
        "checked_at": timestamp,
        "argv": list(argv),
        "status": "check_passed" if result["returncode"] == 0 else "check_failed",
        **result,
    }
    receipt_path = task_dir / "checks" / (_sha256(receipt) + ".json")
    _atomic_json(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def verify_task(repo_path: Path, *, state_root: Path = DEFAULT_STATE_ROOT, now=None) -> Mapping[str, Any]:
    """Run frozen acceptance commands in disposable repository snapshots."""

    decision = authorize_mutation(repo_path, state_root=state_root, now=now)
    if not decision["allowed"]:
        raise ProjectEntryError("mutation lease not active: " + decision["reason"])
    identity = canonical_project_identity(repo_path)
    directory, enrolled = _load_enrollment(identity, state_root)
    task_dir = directory / "tasks" / decision["task_hash"]
    task = _load_json(task_dir / "task.json")
    _verify_evidence_inputs(task)
    definitions = {
        item["acceptance_id"]: item
        for item in enrolled["policy"]["acceptance_definitions"]
    }
    results = []
    all_passed = True
    for acceptance_id in task["acceptance_ids"]:
        definition = definitions[acceptance_id]
        for frozen in definition["files"]:
            actual = Path(identity["repo_path"]) / frozen["path"]
            if not actual.is_file() or _file_sha256(actual) != frozen["sha256"]:
                raise ProjectEntryError(
                    "acceptance file hash mismatch: " + frozen["path"]
                )
        remaining = max(1.0, float(decision["expires_at"]) - _clock_value(now))
        result = {
            "acceptance_id": acceptance_id,
            **_run_snapshot_command(
                Path(identity["repo_path"]),
                definition["argv"],
                timeout=min(300.0, remaining),
            ),
        }
        results.append(result)
        all_passed = all_passed and result["returncode"] == 0
    timestamp = _clock_value(now)
    final_product_tree_hash = _path_set_hash(
        Path(identity["repo_path"]), task["deliverable_paths"]
    )
    baseline_product_tree_hash = task.get("baseline_product_tree_hash")
    receipt = {
        "schema_version": "development-governor-verification-receipt.v0",
        "project_id": identity["project_id"],
        "policy_hash": enrolled["policy_hash"],
        "task_hash": decision["task_hash"],
        "lease_id": decision["lease_id"],
        "verified_at": timestamp,
        "status": "verification_passed" if all_passed else "verification_failed",
        "product_evidence": (
            isinstance(baseline_product_tree_hash, str)
            and baseline_product_tree_hash != final_product_tree_hash
        ),
        "repository": {
            "path": identity["repo_path"],
            "product_paths": list(task["deliverable_paths"]),
            "baseline_product_tree_hash": baseline_product_tree_hash,
            "final_product_tree_hash": final_product_tree_hash,
        },
        "results": results,
    }
    receipt_path = task_dir / "verifications" / (f"{int(timestamp * 1000000)}.json")
    state = _load_task_state(task_dir, decision["task_hash"])
    state["status"] = receipt["status"]
    state["verification_receipt"] = str(receipt_path)
    _atomic_json(receipt_path, receipt)
    _atomic_json(task_dir / "state.json", state)
    return {
        "status": receipt["status"],
        "task_hash": decision["task_hash"],
        "receipt_path": str(receipt_path),
        "results": results,
    }


def close_task(
    repo_path: Path,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    owner_abort_reason: Optional[str] = None,
    now=None,
) -> Mapping[str, Any]:
    """Close an active task after verification or an explicit Owner abort."""

    identity = canonical_project_identity(repo_path)
    project_id = identity["project_id"]
    with _project_lock(state_root, project_id) as directory:
        active_path = directory / "active-lease.json"
        if not active_path.is_file():
            return {"status": "already_closed", "project_id": project_id}
        lease = _load_json(active_path)
        task_hash = _nonempty_string(lease.get("task_hash"), "task_hash")
        task_dir = directory / "tasks" / task_hash
        state = _load_task_state(task_dir, task_hash)
        if owner_abort_reason is None:
            if state["status"] != "verification_passed":
                raise ProjectEntryError("verification has not passed; explicit Owner abort required")
            terminal_status = "closed"
        else:
            _nonempty_string(owner_abort_reason, "owner_abort_reason")
            terminal_status = "aborted"
        terminal_lease = dict(lease)
        terminal_lease.update(
            {
                "closed_at": _clock_value(now),
                "terminal_status": terminal_status,
                "owner_abort_reason": owner_abort_reason,
            }
        )
        archive = _archive_lease(task_dir, terminal_lease)
        active_path.unlink()
        state["status"] = terminal_status
        _atomic_json(task_dir / "state.json", state)
    return {
        "status": terminal_status,
        "project_id": project_id,
        "task_hash": task_hash,
        "lease_archive": str(archive),
    }
