"""Codex PreToolUse guard for externally governed project mutations."""

import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import sys
from typing import Any, Mapping, Optional

from development_governor.default_activation import bound_source_runtime_status
from development_governor.project_entry import (
    DEFAULT_STATE_ROOT,
    ProjectEntryError,
    authorize_mutation,
    canonical_project_identity,
)


_READ_ONLY_PROGRAMS = {"pwd", "ls", "rg", "grep", "stat", "head", "tail", "wc", "which"}
_READ_ONLY_GIT = {"status", "diff", "show", "log", "rev-parse", "branch", "ls-files", "worktree"}
_GOVERNOR_COMMANDS = {
    "enroll",
    "migrate-policy",
    "prepare",
    "review-spec",
    "start",
    "status",
    "check",
    "verify",
    "close",
    "default-enable",
    "default-disable",
    "default-upgrade",
    "hook-guard",
}
_SHELL_TOOL_NAMES = {"bash", "shell", "exec_command", "unified_exec", "functions.exec"}
_PATCH_TOOL_NAMES = {"apply_patch", "edit", "write"}
_SHELL_META = re.compile(r"[|;&><`\n]|\$\(")
_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
    re.MULTILINE,
)


def _deny(reason: str) -> Mapping[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _diagnostic(message: str) -> Mapping[str, Any]:
    return {"systemMessage": "Development Governor guard fail-open: " + message}


def _is_trusted_governor_command(command: str, state_root: Path) -> bool:
    if not command.strip() or _SHELL_META.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 2 or tokens[1] not in _GOVERNOR_COMMANDS:
        return False
    activation_path = Path(state_root) / "activation" / "current.json"
    if not activation_path.is_file():
        return False
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    runtime = activation.get("runtime")
    if not isinstance(runtime, Mapping):
        return False
    launcher = runtime.get("launcher_path")
    return isinstance(launcher, str) and Path(tokens[0]).expanduser() == Path(launcher)


def _shell_is_read_only(command: str) -> bool:
    if not command.strip() or _SHELL_META.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    program = Path(tokens[0]).name
    if program in _READ_ONLY_PROGRAMS:
        if program == "grep" and any(token.startswith("--include=") for token in tokens):
            return True
        return True
    if program == "git" and len(tokens) >= 2:
        index = 1
        while index < len(tokens) and tokens[index] in {"-C", "--git-dir", "--work-tree"}:
            index += 2
        if index >= len(tokens) or tokens[index] not in _READ_ONLY_GIT:
            return False
        arguments = tokens[index + 1 :]
        if any(
            value == "-o" or value == "--output" or value.startswith("--output=")
            for value in arguments
        ):
            return False
        if tokens[index] == "worktree":
            return bool(arguments) and arguments[0] == "list"
        if tokens[index] == "branch":
            if not arguments:
                return True
            return all(
                value in {"--list", "--show-current", "--no-color"}
                or value.startswith("--format=")
                for value in arguments
            )
        return True
    return False


def _tool_source(tool_input: Any) -> Optional[str]:
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, Mapping):
        for key in ("command", "cmd", "input", "source", "code", "script"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return None


def _mutation_capable(tool_name: str, tool_input: Any) -> Optional[bool]:
    normalized = tool_name.lower()
    if normalized in _PATCH_TOOL_NAMES:
        return True
    if normalized in _SHELL_TOOL_NAMES:
        command = _tool_source(tool_input)
        if command is None:
            return True
        return not _shell_is_read_only(command)
    return None


def _path_matches(path: str, prefix: str) -> bool:
    left = path.rstrip("/")
    right = prefix.rstrip("/")
    return left == right or left.startswith(right + "/")


def _normalized_repo_path(value: str, repo: Path) -> Optional[str]:
    candidate = value.strip().strip("'\"")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return "__outside_repository__"
    normalized = candidate.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if ".." in pure.parts:
        return "__outside_repository__"
    clean = str(pure)
    return None if clean in ("", ".") else clean


def _patch_paths(source: str, repo: Path):
    expanded = source.replace("\\n", "\n")
    paths = []
    for match in _PATCH_PATH.finditer(expanded):
        value = match.group(1) or match.group(2)
        normalized = _normalized_repo_path(value, repo)
        if normalized is not None:
            paths.append(normalized)
    return tuple(paths)


def _explicit_shell_paths(source: str, repo: Path):
    if _SHELL_META.search(source):
        return ()
    try:
        tokens = shlex.split(source)
    except ValueError:
        return ()
    executable_index = 0
    while executable_index < len(tokens) and "=" in tokens[executable_index]:
        executable_index += 1
    program = (
        Path(tokens[executable_index]).name
        if executable_index < len(tokens)
        else ""
    )
    direct_path_mutators = {"touch", "rm", "mv", "cp", "mkdir", "rmdir", "tee"}
    paths = []
    for index, token in enumerate(tokens):
        if index <= executable_index or token.startswith("-") or "=" in token:
            continue
        exists_in_repo = (repo / token).exists()
        looks_like_path = (
            token.startswith(("./", "../", "/"))
            or "/" in token
            or exists_in_repo
            or program in direct_path_mutators
        )
        if not looks_like_path:
            continue
        normalized = _normalized_repo_path(token, repo)
        if normalized is not None:
            paths.append(normalized)
    return tuple(paths)


def _scope_denial(
    tool_name: str,
    tool_input: Any,
    *,
    repo: Path,
    decision: Mapping[str, Any],
) -> Optional[str]:
    source = _tool_source(tool_input) or ""
    normalized_name = tool_name.lower()
    patch_capable = normalized_name in _PATCH_TOOL_NAMES or (
        normalized_name in _SHELL_TOOL_NAMES
        and ("apply_patch" in source or "*** Begin Patch" in source)
    )
    paths = _patch_paths(source, repo) if patch_capable else _explicit_shell_paths(source, repo)
    if patch_capable and not paths:
        return "Governor could not establish patch paths inside the active task scope."
    if normalized_name in _SHELL_TOOL_NAMES and not patch_capable and not paths:
        return (
            "Opaque shell command has no provable write set; run it through the "
            "Governor isolated check entry."
        )
    allowed = tuple(decision.get("allowed_paths", ()))
    protected = tuple(decision.get("protected_paths", ()))
    for path in paths:
        if path == "__outside_repository__" or not any(
            _path_matches(path, prefix) for prefix in allowed
        ):
            return "Mutation path is outside the active Governor task scope."
        if any(_path_matches(path, prefix) for prefix in protected):
            return "Mutation path targets protected Governor acceptance material."
    if any(prefix.rstrip("/") in source for prefix in protected):
        return "Mutation command references protected Governor acceptance material."
    return None


def evaluate_hook_event(
    event: Mapping[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now=None,
) -> Mapping[str, Any]:
    """Return one Codex-compatible allow/deny result for a PreToolUse event."""

    try:
        if not isinstance(event, Mapping):
            raise ValueError("event is not an object")
        cwd = event.get("cwd")
        tool_name = event.get("tool_name")
        tool_input = event.get("tool_input")
        if not isinstance(cwd, str) or not cwd or not isinstance(tool_name, str) or tool_input is None:
            raise ValueError("event is missing cwd, tool_name, or tool_input")
        if tool_name.lower() in _SHELL_TOOL_NAMES:
            command = _tool_source(tool_input)
            if isinstance(command, str) and _is_trusted_governor_command(
                command, Path(state_root)
            ):
                return {}
        mutation = _mutation_capable(tool_name, tool_input)
        if mutation is None or mutation is False:
            return {}
        try:
            identity = canonical_project_identity(Path(cwd))
        except ProjectEntryError:
            return {}
        activation_path = Path(state_root) / "activation" / "current.json"
        if activation_path.is_file():
            activation = json.loads(activation_path.read_text(encoding="utf-8"))
            governor_identity = activation.get("governor_project_identity")
            if (
                isinstance(governor_identity, Mapping)
                and governor_identity.get("project_id") == identity["project_id"]
            ):
                return {}
            source_status = bound_source_runtime_status(activation)
            if source_status["status"] == "upgrade_required":
                return _deny(
                    "Runtime upgrade required: the active Development Governor runtime "
                    "is stale relative to its bound source. Run default-upgrade from the "
                    "approved Governor source checkout with explicit Owner authority "
                    "before external project mutation."
                )
            if source_status["status"] == "source_unavailable":
                return _deny(
                    "Bound Development Governor source is unavailable; restore the "
                    "approved source checkout or explicitly rebind and upgrade before "
                    "external project mutation."
                )
        decision = authorize_mutation(Path(cwd), state_root=state_root, now=now)
        if decision["allowed"]:
            scope_denial = _scope_denial(
                tool_name,
                tool_input,
                repo=Path(identity["repo_path"]),
                decision=decision,
            )
            if scope_denial is not None:
                return _deny(scope_denial)
            return {}
        if decision["reason"] == "project_not_enrolled":
            return _deny("Project mutation requires Development Governor enroll before editing.")
        if decision["reason"] == "lease_expired":
            return _deny("Development Governor lease expired; start a valid bounded task before editing.")
        return _deny("No valid Development Governor lease; run prepare and start before editing.")
    except (ProjectEntryError, ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as error:
        return _diagnostic(str(error))


def hook_main(stdin=None, stdout=None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        event = json.load(source)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        result = _diagnostic(str(error))
    else:
        result = evaluate_hook_event(event)
    json.dump(result, target, ensure_ascii=False, sort_keys=True)
    target.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
