"""Safe user-level activation of the Development Governor default entry."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Mapping, Optional, Tuple

from development_governor.project_entry import canonical_project_identity


AGENTS_BEGIN = "<!-- DEVELOPMENT-GOVERNOR:BEGIN v0 -->"
AGENTS_END = "<!-- DEVELOPMENT-GOVERNOR:END v0 -->"
ACTIVATION_SCHEMA = "development-governor-default-activation.v1"
LEGACY_ACTIVATION_SCHEMA = "development-governor-default-activation.v0"


class ActivationError(ValueError):
    """Raised when global activation cannot be applied without data loss."""


def _digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
        path.chmod(mode)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _read_file(path: Path) -> Tuple[bool, bytes, int]:
    if not path.exists():
        return False, b"", 0
    if not path.is_file():
        raise ActivationError(f"global configuration path is not a file: {path}")
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _package_hash(source_package: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in Path(source_package).rglob("*.py") if "__pycache__" not in path.parts
    )
    if not files:
        raise ActivationError("source package contains no Python modules")
    for path in files:
        relative = path.relative_to(source_package).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _install_runtime(state_root: Path, source_package: Path) -> Mapping[str, str]:
    package_hash = _package_hash(source_package)
    runtime_root = state_root / "runtime" / package_hash
    package_target = runtime_root / "development_governor"
    if not package_target.exists():
        runtime_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = runtime_root.parent / (".tmp-" + package_hash)
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary_package = temporary / "development_governor"
        shutil.copytree(
            source_package,
            temporary_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for path in temporary.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        try:
            os.replace(str(temporary), str(runtime_root))
        except OSError:
            if not runtime_root.exists():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
    bin_dir = state_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = str(Path(sys.executable).resolve())
    launcher = bin_dir / "governor"
    hook = bin_dir / "pre-tool-use"
    launcher_body = (
        "#!/bin/sh\n"
        f"DEVELOPMENT_GOVERNOR_STATE_ROOT={str(state_root)!r} PYTHONPATH={str(runtime_root)!r} "
        f"exec {python!r} -m development_governor.cli \"$@\"\n"
    ).encode("utf-8")
    hook_body = (
        "#!/bin/sh\n"
        f"DEVELOPMENT_GOVERNOR_STATE_ROOT={str(state_root)!r} PYTHONPATH={str(runtime_root)!r} "
        f"exec {python!r} -m development_governor.hook_guard\n"
    ).encode("utf-8")
    _atomic_bytes(launcher, launcher_body, 0o700)
    _atomic_bytes(hook, hook_body, 0o700)
    return {
        "package_hash": package_hash,
        "runtime_root": str(runtime_root),
        "launcher_path": str(launcher),
        "hook_command": str(hook),
        "launcher_sha256": _digest_bytes(launcher_body),
        "hook_sha256": _digest_bytes(hook_body),
    }


def _managed_agents_block(launcher_path: str) -> str:
    return "\n".join(
        (
            AGENTS_BEGIN,
            "## Development Governor default entry",
            "",
            "For mutating software-project work, use Development Governor before the first edit.",
            "Read-only inspection, explanation, and status checks remain allowed.",
            f"Launcher: `{launcher_path}`",
            "Route: enroll (first project use) -> prepare -> start -> implement -> verify -> close.",
            "Do not invent Owner authority, acceptance IDs, or acceptance evidence.",
            "Do not modify protected acceptance material. Do not claim completion before verify and close.",
            "Native multi-agent execution remains allowed only for declared independent deliverables with independent acceptance IDs.",
            "A serial TDD slice must not spawn extra reviewer or read-only probe lanes.",
            "The Hook is a guardrail for supported Codex mutation paths, not an OS security boundary.",
            AGENTS_END,
        )
    )


def _marker_counts(text: str) -> Tuple[int, int]:
    return text.count(AGENTS_BEGIN), text.count(AGENTS_END)


def _extract_agents_block(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError("AGENTS.md must be UTF-8") from error
    begin_count, end_count = _marker_counts(text)
    if begin_count != 1 or end_count != 1:
        raise ActivationError("managed AGENTS block integrity mismatch")
    start = text.index(AGENTS_BEGIN)
    end = text.index(AGENTS_END, start) + len(AGENTS_END)
    return text[start:end].encode("utf-8")


def _replace_agents_block(content: bytes, block: str) -> bytes:
    current = _extract_agents_block(content).decode("utf-8")
    text = content.decode("utf-8")
    return text.replace(current, block, 1).encode("utf-8")


def _append_agents(original: bytes, block: str) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError("AGENTS.md must be UTF-8") from error
    begin_count, end_count = _marker_counts(text)
    if begin_count or end_count:
        raise ActivationError("managed AGENTS markers already exist without an active manifest")
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return (text + separator + block + "\n").encode("utf-8")


def _parse_hooks(content: bytes) -> Mapping[str, Any]:
    if not content:
        return {}
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ActivationError("hooks.json is not valid JSON") from error
    if not isinstance(raw, Mapping):
        raise ActivationError("hooks.json root must be an object")
    return dict(raw)


def _managed_hook_group(command: str) -> Mapping[str, Any]:
    return {
        "matcher": ".*",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 2,
                "statusMessage": "Checking Development Governor lease",
            }
        ],
    }


def _hook_group_matches(group: Any, command: str) -> bool:
    if not isinstance(group, Mapping):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(isinstance(item, Mapping) and item.get("command") == command for item in handlers)


def _managed_hook_projection(group: Mapping[str, Any], handler: Mapping[str, Any]) -> bytes:
    return _canonical_bytes({"matcher": group.get("matcher"), "handler": dict(handler)})


def _find_managed_hook_projection(content: bytes, command: str) -> bytes:
    raw = _parse_hooks(content)
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, Mapping):
        raise ActivationError("managed Hook integrity mismatch")
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        raise ActivationError("managed Hook integrity mismatch")
    matches = []
    for group in pre:
        if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
            continue
        for handler in group["hooks"]:
            if isinstance(handler, Mapping) and handler.get("command") == command:
                matches.append(_managed_hook_projection(group, handler))
    if len(matches) != 1:
        raise ActivationError("managed Hook integrity mismatch")
    return matches[0]


def _replace_managed_hook_command(content: bytes, old_command: str, new_command: str) -> bytes:
    raw = dict(_parse_hooks(content))
    hooks = dict(raw.get("hooks", {}))
    pre = list(hooks.get("PreToolUse", []))
    occurrences = []
    for group_index, group in enumerate(pre):
        if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
            continue
        for handler_index, handler in enumerate(group["hooks"]):
            if isinstance(handler, Mapping) and handler.get("command") == old_command:
                occurrences.append((group_index, handler_index))
    if len(occurrences) != 1:
        raise ActivationError("managed Hook integrity mismatch")
    group_index, handler_index = occurrences[0]
    group = dict(pre[group_index])
    handlers = list(group["hooks"])
    handlers[handler_index] = _managed_hook_group(new_command)["hooks"][0]
    group["hooks"] = handlers
    group["matcher"] = ".*"
    pre[group_index] = group
    hooks["PreToolUse"] = pre
    raw["hooks"] = hooks
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _append_hook(original: bytes, command: str) -> bytes:
    raw = dict(_parse_hooks(original))
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, Mapping):
        raise ActivationError("hooks.json hooks field must be an object")
    hooks = dict(hooks)
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        raise ActivationError("hooks.json PreToolUse must be an array")
    if any(_hook_group_matches(group, command) for group in pre):
        raise ActivationError("managed Hook already exists without an active manifest")
    pre.append(_managed_hook_group(command))
    hooks["PreToolUse"] = pre
    raw["hooks"] = hooks
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _file_record(path: Path, existed: bool, content: bytes, mode: int, backup: Optional[Path]) -> Mapping[str, Any]:
    return {
        "path": str(path),
        "existed": existed,
        "sha256": _digest_bytes(content),
        "mode": mode,
        "backup_path": str(backup) if backup is not None else None,
    }


def _load_activation_manifest(current_manifest: Path) -> Mapping[str, Any]:
    try:
        manifest = json.loads(current_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ActivationError("activation manifest is unreadable") from error
    if not isinstance(manifest, Mapping):
        raise ActivationError("activation manifest root must be an object")
    if manifest.get("schema_version") not in {
        ACTIVATION_SCHEMA,
        LEGACY_ACTIVATION_SCHEMA,
    }:
        raise ActivationError("unsupported activation manifest")
    return manifest


def _validate_activation_integrity(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate only Governor-managed projections and stable runtime launchers."""

    try:
        runtime = manifest["runtime"]
        agents_record = manifest["agents"]
        hooks_record = manifest["hooks"]
        launcher_path = Path(runtime["launcher_path"])
        hook_path = Path(runtime["hook_command"])
        runtime_package = Path(runtime["runtime_root"]) / "development_governor"
        agents_path = Path(agents_record["path"])
        hooks_path = Path(hooks_record["path"])
    except (KeyError, TypeError) as error:
        raise ActivationError("activation manifest is incomplete") from error

    try:
        agents_content = agents_path.read_bytes()
        hooks_content = hooks_path.read_bytes()
        launcher_content = launcher_path.read_bytes()
        hook_content = hook_path.read_bytes()
    except OSError as error:
        raise ActivationError("managed activation integrity mismatch") from error

    agents_projection = _extract_agents_block(agents_content)
    hooks_projection = _find_managed_hook_projection(
        hooks_content, str(runtime["hook_command"])
    )
    if manifest["schema_version"] == ACTIVATION_SCHEMA:
        try:
            expected_agents = agents_record["managed_sha256"]
            expected_hooks = hooks_record["managed_sha256"]
        except KeyError as error:
            raise ActivationError("activation manifest is missing managed integrity hashes") from error
    else:
        expected_agents = _digest_bytes(
            _managed_agents_block(str(runtime["launcher_path"])).encode("utf-8")
        )
        expected_hooks = _digest_bytes(
            _managed_hook_projection(
                _managed_hook_group(str(runtime["hook_command"])),
                _managed_hook_group(str(runtime["hook_command"]))["hooks"][0],
            )
        )
    if _digest_bytes(agents_projection) != expected_agents:
        raise ActivationError("managed AGENTS block integrity mismatch")
    if _digest_bytes(hooks_projection) != expected_hooks:
        raise ActivationError("managed Hook integrity mismatch")
    if _digest_bytes(launcher_content) != runtime.get("launcher_sha256"):
        raise ActivationError("managed launcher integrity mismatch")
    if _digest_bytes(hook_content) != runtime.get("hook_sha256"):
        raise ActivationError("managed Hook launcher integrity mismatch")
    try:
        installed_package_hash = _package_hash(runtime_package)
    except (ActivationError, OSError) as error:
        raise ActivationError("managed runtime package integrity mismatch") from error
    if installed_package_hash != runtime.get("package_hash"):
        raise ActivationError("managed runtime package integrity mismatch")
    return {
        "agents_content": agents_content,
        "hooks_content": hooks_content,
        "launcher_content": launcher_content,
        "hook_content": hook_content,
        "agents_mode": stat.S_IMODE(agents_path.stat().st_mode),
        "hooks_mode": stat.S_IMODE(hooks_path.stat().st_mode),
        "launcher_mode": stat.S_IMODE(launcher_path.stat().st_mode),
        "hook_mode": stat.S_IMODE(hook_path.stat().st_mode),
    }


def default_enable(
    *,
    codex_home: Path,
    source_package: Path,
    governor_repo: Optional[Path],
) -> Mapping[str, Any]:
    """Install one global routing block and Hook without invoking a model."""

    home = Path(codex_home).expanduser().resolve()
    state_root = home / "development-governor" / "v0"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.chmod(0o700)
    current_manifest = state_root / "activation" / "current.json"
    if current_manifest.is_file():
        manifest = _load_activation_manifest(current_manifest)
        _validate_activation_integrity(manifest)
        available_hash = _package_hash(Path(source_package).resolve())
        if (
            manifest["schema_version"] == LEGACY_ACTIVATION_SCHEMA
            or available_hash != manifest["runtime"]["package_hash"]
        ):
            return {
                "status": "upgrade_required",
                "manifest_path": str(current_manifest),
                "launcher_path": manifest["runtime"]["launcher_path"],
                "hook_command": manifest["runtime"]["hook_command"],
                "current_runtime_hash": manifest["runtime"]["package_hash"],
                "available_runtime_hash": available_hash,
            }
        return {
            "status": "already_enabled",
            "manifest_path": str(current_manifest),
            "launcher_path": manifest["runtime"]["launcher_path"],
            "hook_command": manifest["runtime"]["hook_command"],
        }

    runtime = _install_runtime(state_root, Path(source_package).resolve())
    agents_path = home / "AGENTS.md"
    hooks_path = home / "hooks.json"
    agents_exists, agents_original, agents_mode = _read_file(agents_path)
    hooks_exists, hooks_original, hooks_mode = _read_file(hooks_path)
    try:
        agents_text = agents_original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError("AGENTS.md must be UTF-8") from error
    begin_count, end_count = _marker_counts(agents_text)
    if begin_count != end_count or begin_count not in (0,):
        raise ActivationError("managed AGENTS markers are broken or duplicated")
    agents_installed = _append_agents(agents_original, _managed_agents_block(runtime["launcher_path"]))
    hooks_installed = _append_hook(hooks_original, runtime["hook_command"])
    governor_identity = (
        canonical_project_identity(governor_repo)
        if governor_repo is not None
        else None
    )
    activation_seed = {
        "codex_home": str(home),
        "agents_original": _digest_bytes(agents_original),
        "hooks_original": _digest_bytes(hooks_original),
        "package_hash": runtime["package_hash"],
        "governor_project_id": (
            governor_identity["project_id"]
            if governor_identity is not None
            else None
        ),
    }
    activation_id = hashlib.sha256(_canonical_bytes(activation_seed)).hexdigest()
    backup_dir = state_root / "backups" / activation_id
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    agents_backup = backup_dir / "AGENTS.md.bin" if agents_exists else None
    hooks_backup = backup_dir / "hooks.json.bin" if hooks_exists else None
    if agents_backup is not None:
        _atomic_bytes(agents_backup, agents_original, 0o600)
    if hooks_backup is not None:
        _atomic_bytes(hooks_backup, hooks_original, 0o600)
    manifest = {
        "schema_version": ACTIVATION_SCHEMA,
        "activation_id": activation_id,
        "status": "enabled",
        "codex_home": str(home),
        "governor_project_identity": (
            dict(governor_identity) if governor_identity is not None else None
        ),
        "runtime": dict(runtime),
        "agents": {
            **_file_record(agents_path, agents_exists, agents_original, agents_mode, agents_backup),
            "installed_sha256": _digest_bytes(agents_installed),
            "managed_sha256": _digest_bytes(_extract_agents_block(agents_installed)),
            "unmanaged_edits_at_upgrade": False,
        },
        "hooks": {
            **_file_record(hooks_path, hooks_exists, hooks_original, hooks_mode, hooks_backup),
            "installed_sha256": _digest_bytes(hooks_installed),
            "managed_sha256": _digest_bytes(
                _find_managed_hook_projection(hooks_installed, runtime["hook_command"])
            ),
            "unmanaged_edits_at_upgrade": False,
        },
    }
    current_manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        _atomic_bytes(agents_path, agents_installed, agents_mode if agents_exists else 0o644)
        _atomic_bytes(hooks_path, hooks_installed, hooks_mode if hooks_exists else 0o600)
        _atomic_bytes(current_manifest, _canonical_bytes(manifest), 0o600)
    except BaseException:
        if agents_exists:
            _atomic_bytes(agents_path, agents_original, agents_mode)
        elif agents_path.exists():
            agents_path.unlink()
        if hooks_exists:
            _atomic_bytes(hooks_path, hooks_original, hooks_mode)
        elif hooks_path.exists():
            hooks_path.unlink()
        raise
    return {
        "status": "enabled",
        "activation_id": activation_id,
        "manifest_path": str(current_manifest),
        "launcher_path": runtime["launcher_path"],
        "hook_command": runtime["hook_command"],
        "runtime_hash": runtime["package_hash"],
    }


def default_upgrade(
    *,
    codex_home: Path,
    source_package: Path,
    governor_repo: Optional[Path],
    owner_authorization_ref: str,
) -> Mapping[str, Any]:
    """Upgrade one healthy activation under explicit Owner authority."""

    if not isinstance(owner_authorization_ref, str) or not owner_authorization_ref.strip():
        raise ActivationError("owner_authorization_ref must be a non-empty string")
    owner_ref = owner_authorization_ref.strip()
    home = Path(codex_home).expanduser().resolve()
    state_root = home / "development-governor" / "v0"
    current_manifest = state_root / "activation" / "current.json"
    if not current_manifest.is_file():
        raise ActivationError("default runtime is not enabled")
    manifest = _load_activation_manifest(current_manifest)
    current = _validate_activation_integrity(manifest)
    source = Path(source_package).resolve()
    available_hash = _package_hash(source)
    if (
        manifest["schema_version"] == ACTIVATION_SCHEMA
        and available_hash == manifest["runtime"]["package_hash"]
    ):
        raise ActivationError("runtime is already current")

    old_runtime = manifest["runtime"]
    agents_path = Path(manifest["agents"]["path"])
    hooks_path = Path(manifest["hooks"]["path"])
    launcher_path = Path(old_runtime["launcher_path"])
    hook_path = Path(old_runtime["hook_command"])
    history_path = None
    receipt_path = None
    runtime = None
    try:
        runtime = _install_runtime(state_root, source)
        agents_installed = _replace_agents_block(
            current["agents_content"], _managed_agents_block(runtime["launcher_path"])
        )
        hooks_installed = _replace_managed_hook_command(
            current["hooks_content"],
            str(old_runtime["hook_command"]),
            str(runtime["hook_command"]),
        )
        governor_identity = (
            canonical_project_identity(governor_repo)
            if governor_repo is not None
            else None
        )
        activation_seed = {
            "previous_activation_id": manifest["activation_id"],
            "package_hash": runtime["package_hash"],
            "owner_authorization_ref": owner_ref,
            "governor_project_id": (
                governor_identity["project_id"]
                if governor_identity is not None
                else None
            ),
        }
        activation_id = hashlib.sha256(_canonical_bytes(activation_seed)).hexdigest()
        agents_record = dict(manifest["agents"])
        hooks_record = dict(manifest["hooks"])
        agents_record.update(
            {
                "installed_sha256": _digest_bytes(agents_installed),
                "managed_sha256": _digest_bytes(_extract_agents_block(agents_installed)),
                "unmanaged_edits_at_upgrade": (
                    bool(agents_record.get("unmanaged_edits_at_upgrade"))
                    or _digest_bytes(current["agents_content"])
                    != agents_record["installed_sha256"]
                ),
            }
        )
        hooks_record.update(
            {
                "installed_sha256": _digest_bytes(hooks_installed),
                "managed_sha256": _digest_bytes(
                    _find_managed_hook_projection(
                        hooks_installed, runtime["hook_command"]
                    )
                ),
                "unmanaged_edits_at_upgrade": (
                    bool(hooks_record.get("unmanaged_edits_at_upgrade"))
                    or _digest_bytes(current["hooks_content"])
                    != hooks_record["installed_sha256"]
                ),
            }
        )
        upgraded_manifest = {
            "schema_version": ACTIVATION_SCHEMA,
            "activation_id": activation_id,
            "status": "enabled",
            "codex_home": str(home),
            "governor_project_identity": (
                dict(governor_identity) if governor_identity is not None else None
            ),
            "runtime": dict(runtime),
            "agents": agents_record,
            "hooks": hooks_record,
            "upgrade": {
                "previous_activation_id": manifest["activation_id"],
                "previous_runtime_hash": old_runtime["package_hash"],
                "owner_authorization_ref": owner_ref,
            },
        }
        superseded = dict(manifest)
        superseded["status"] = "superseded"
        superseded["superseded_by_activation_id"] = activation_id
        history_path = (
            state_root
            / "activation"
            / "history"
            / (str(manifest["activation_id"]) + ".json")
        )
        receipt_path = (
            state_root / "activation" / "upgrades" / (activation_id + ".json")
        )
        receipt = {
            "schema_version": "development-governor-runtime-upgrade-receipt.v0",
            "activation_id": activation_id,
            "previous_activation_id": manifest["activation_id"],
            "old_runtime_hash": old_runtime["package_hash"],
            "new_runtime_hash": runtime["package_hash"],
            "owner_authorization_ref": owner_ref,
        }
        _atomic_bytes(
            agents_path, agents_installed, int(current["agents_mode"])
        )
        _atomic_bytes(hooks_path, hooks_installed, int(current["hooks_mode"]))
        _atomic_bytes(history_path, _canonical_bytes(superseded), 0o600)
        _atomic_bytes(receipt_path, _canonical_bytes(receipt), 0o600)
        _atomic_bytes(current_manifest, _canonical_bytes(upgraded_manifest), 0o600)
    except BaseException:
        _atomic_bytes(
            agents_path, current["agents_content"], int(current["agents_mode"])
        )
        _atomic_bytes(hooks_path, current["hooks_content"], int(current["hooks_mode"]))
        _atomic_bytes(
            launcher_path,
            current["launcher_content"],
            int(current["launcher_mode"]),
        )
        _atomic_bytes(
            hook_path, current["hook_content"], int(current["hook_mode"])
        )
        if history_path is not None and history_path.exists():
            history_path.unlink()
        if receipt_path is not None and receipt_path.exists():
            receipt_path.unlink()
        raise
    return {
        "status": "upgraded",
        "activation_id": activation_id,
        "manifest_path": str(current_manifest),
        "history_path": str(history_path),
        "upgrade_receipt": str(receipt_path),
        "launcher_path": runtime["launcher_path"],
        "hook_command": runtime["hook_command"],
        "runtime_hash": runtime["package_hash"],
    }


def _restore_record(record: Mapping[str, Any]) -> Optional[Tuple[bytes, int]]:
    if not record["existed"]:
        return None
    backup = Path(record["backup_path"])
    content = backup.read_bytes()
    if _digest_bytes(content) != record["sha256"]:
        raise ActivationError("activation backup hash mismatch")
    return content, int(record["mode"])


def _remove_agents_block(content: bytes) -> Optional[bytes]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    begin_count, end_count = _marker_counts(text)
    if begin_count != 1 or end_count != 1:
        return None
    start = text.index(AGENTS_BEGIN)
    end = text.index(AGENTS_END, start) + len(AGENTS_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    if start > 0 and text[start - 1] == "\n" and (start < 2 or text[start - 2] == "\n"):
        start -= 1
    return (text[:start] + text[end:]).encode("utf-8")


def _remove_hook_group(content: bytes, command: str) -> Optional[bytes]:
    try:
        raw = dict(_parse_hooks(content))
    except ActivationError:
        return None
    hooks = raw.get("hooks")
    if not isinstance(hooks, Mapping):
        return None
    hooks = dict(hooks)
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        return None
    occurrences = []
    for group_index, group in enumerate(pre):
        if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
            continue
        for handler_index, handler in enumerate(group["hooks"]):
            if isinstance(handler, Mapping) and handler.get("command") == command:
                occurrences.append((group_index, handler_index))
    if len(occurrences) != 1:
        return None
    group_index, handler_index = occurrences[0]
    group = dict(pre[group_index])
    handlers = list(group["hooks"])
    del handlers[handler_index]
    if handlers:
        group["hooks"] = handlers
        pre[group_index] = group
    else:
        del pre[group_index]
    if pre:
        hooks["PreToolUse"] = pre
    else:
        hooks.pop("PreToolUse", None)
    raw["hooks"] = hooks
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _apply_restoration(path: Path, restoration: Optional[Tuple[bytes, int]]) -> None:
    if restoration is None:
        if path.exists():
            path.unlink()
        return
    content, mode = restoration
    _atomic_bytes(path, content, mode)


def default_disable(*, codex_home: Path, restore_backup: bool = False) -> Mapping[str, Any]:
    """Remove only Governor-managed global content, or explicitly restore backup."""

    home = Path(codex_home).expanduser().resolve()
    state_root = home / "development-governor" / "v0"
    current_manifest = state_root / "activation" / "current.json"
    if not current_manifest.is_file():
        return {"status": "already_disabled"}
    manifest = _load_activation_manifest(current_manifest)
    agents_path = Path(manifest["agents"]["path"])
    hooks_path = Path(manifest["hooks"]["path"])
    agents_current = agents_path.read_bytes() if agents_path.exists() else b""
    hooks_current = hooks_path.read_bytes() if hooks_path.exists() else b""
    if restore_backup:
        agents_restoration = _restore_record(manifest["agents"])
        hooks_restoration = _restore_record(manifest["hooks"])
    else:
        if (
            not manifest["agents"].get("unmanaged_edits_at_upgrade", False)
            and _digest_bytes(agents_current)
            == manifest["agents"]["installed_sha256"]
        ):
            agents_restoration = _restore_record(manifest["agents"])
        else:
            removed_agents = _remove_agents_block(agents_current)
            if removed_agents is None:
                return {"status": "owner_required", "reason": "managed AGENTS block is ambiguous"}
            agents_restoration = (removed_agents, stat.S_IMODE(agents_path.stat().st_mode))
        if (
            not manifest["hooks"].get("unmanaged_edits_at_upgrade", False)
            and _digest_bytes(hooks_current)
            == manifest["hooks"]["installed_sha256"]
        ):
            hooks_restoration = _restore_record(manifest["hooks"])
        else:
            removed_hooks = _remove_hook_group(hooks_current, manifest["runtime"]["hook_command"])
            if removed_hooks is None:
                return {"status": "owner_required", "reason": "managed Hook is ambiguous"}
            hooks_restoration = (removed_hooks, stat.S_IMODE(hooks_path.stat().st_mode))
    _apply_restoration(agents_path, agents_restoration)
    _apply_restoration(hooks_path, hooks_restoration)
    history = state_root / "activation" / "history" / (manifest["activation_id"] + ".json")
    disabled_manifest = dict(manifest)
    disabled_manifest["status"] = "disabled"
    _atomic_bytes(history, _canonical_bytes(disabled_manifest), 0o600)
    current_manifest.unlink()
    return {"status": "disabled", "activation_id": manifest["activation_id"], "history_path": str(history)}
