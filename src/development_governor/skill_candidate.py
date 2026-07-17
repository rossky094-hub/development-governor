"""Independent Git candidate staging for non-Git installed Skills."""

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from development_governor.runner import hash_path_set


class SkillCandidateError(ValueError):
    """Raised when a Skill candidate cannot be staged or promoted safely."""


def stage_skill_candidate(
    source_skill: Path, acceptance_source: Path, candidate_repo: Path
) -> dict:
    source_skill = _validated_source_tree(source_skill, "source_skill")
    acceptance_source = _validated_source_tree(
        acceptance_source, "acceptance_source"
    )
    candidate_repo = Path(candidate_repo).expanduser().resolve()
    _require_new_disjoint_candidate(
        source_skill, acceptance_source, candidate_repo
    )

    try:
        candidate_repo.mkdir(parents=True)
        shutil.copytree(source_skill, candidate_repo / "skill")
        shutil.copytree(acceptance_source, candidate_repo / "acceptance")
        skill_tree_hash = hash_path_set(candidate_repo, ("skill/",))
        acceptance_tree_hash = hash_path_set(candidate_repo, ("acceptance/",))
        manifest = {
            "schema_version": "development-governor.skill-candidate.v0",
            "source_skill_path": str(source_skill),
            "source_acceptance_path": str(acceptance_source),
            "source_skill_tree_hash": skill_tree_hash,
            "acceptance_tree_hash": acceptance_tree_hash,
            "governed_product_path": "skill/",
            "frozen_acceptance_path": "acceptance/",
        }
        manifest_path = candidate_repo / ".governor" / "skill-candidate.json"
        manifest_path.parent.mkdir()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _initialize_candidate_git(candidate_repo)
        baseline_commit = _git_output(candidate_repo, "rev-parse", "HEAD")
        return {
            "status": "staged",
            "candidate_repo": str(candidate_repo),
            "skill_tree_hash": skill_tree_hash,
            "acceptance_tree_hash": acceptance_tree_hash,
            "baseline_commit": baseline_commit,
            "manifest_path": str(manifest_path),
        }
    except Exception:
        if candidate_repo.exists():
            shutil.rmtree(candidate_repo)
        raise


def promote_skill_candidate(
    candidate_repo: Path,
    installed_skill: Path,
    terminal_receipt_path: Path,
    *,
    allow_new_install: bool = False,
) -> dict:
    candidate_repo = _validated_candidate_repo(candidate_repo)
    candidate_skill = _validated_source_tree(
        candidate_repo / "skill", "candidate skill"
    )
    installed_skill = Path(installed_skill).expanduser()
    receipt_path, receipt, receipt_sha256 = _validated_external_terminal_receipt(
        candidate_repo, terminal_receipt_path
    )
    candidate_hash = hash_path_set(candidate_repo, ("skill/",))
    repository = receipt.get("repository")
    if not isinstance(repository, dict):
        raise SkillCandidateError("terminal receipt lacks repository binding")
    if repository.get("path") != str(candidate_repo):
        raise SkillCandidateError("terminal receipt is bound to another candidate_repo")
    if repository.get("product_paths") != ["skill/"]:
        raise SkillCandidateError("terminal receipt must bind product path skill/")
    if repository.get("final_product_tree_hash") != candidate_hash:
        raise SkillCandidateError("terminal receipt product tree hash is stale")

    try:
        installed_parent = installed_skill.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise SkillCandidateError(
            "installed_skill parent must be an existing directory"
        ) from error
    if installed_skill.parent.is_symlink() or not installed_parent.is_dir():
        raise SkillCandidateError("installed_skill parent must be a real directory")
    installed_resolved = installed_parent / installed_skill.name
    if installed_skill.is_symlink():
        raise SkillCandidateError("installed_skill must not be a symbolic link")
    if installed_resolved.exists():
        if not installed_resolved.is_dir():
            raise SkillCandidateError("installed_skill must be a real directory")
        install_mode = "replace"
    else:
        if not allow_new_install:
            raise SkillCandidateError(
                "installed_skill does not exist; set allow_new_install explicitly"
            )
        install_mode = "new"
    if installed_resolved == candidate_repo or candidate_repo in installed_resolved.parents:
        raise SkillCandidateError("installed_skill must be outside candidate_repo")
    if installed_resolved in candidate_repo.parents:
        raise SkillCandidateError("candidate_repo must be outside installed_skill")

    source_content_hash = _directory_content_hash(candidate_skill)
    previous_hash = None
    if install_mode == "replace":
        previous_hash = hash_path_set(
            installed_resolved.parent, (installed_resolved.name + "/",)
        )
    stage_path = Path(
        tempfile.mkdtemp(
            prefix="." + installed_resolved.name + ".governor-stage-",
            dir=str(installed_resolved.parent),
        )
    )
    backup_path = None
    if install_mode == "replace":
        backup_path = installed_resolved.parent / (
            "." + installed_resolved.name + ".governor-backup-" + uuid.uuid4().hex
        )
    backup_created = False
    installed_replaced = False
    try:
        shutil.copytree(candidate_skill, stage_path, dirs_exist_ok=True)
        if _directory_content_hash(stage_path) != source_content_hash:
            raise SkillCandidateError("staged promotion copy hash mismatch")
        if install_mode == "replace":
            os.replace(str(installed_resolved), str(backup_path))
            backup_created = True
        elif installed_resolved.exists():
            raise SkillCandidateError("new installed_skill target appeared during promotion")
        os.replace(str(stage_path), str(installed_resolved))
        installed_replaced = True
        if _directory_content_hash(installed_resolved) != source_content_hash:
            raise SkillCandidateError("installed promotion copy hash mismatch")
        if backup_created:
            shutil.rmtree(backup_path)
            backup_created = False
    except Exception:
        if installed_replaced and installed_resolved.exists():
            shutil.rmtree(installed_resolved)
        if backup_created and backup_path is not None and backup_path.exists():
            os.replace(str(backup_path), str(installed_resolved))
        if stage_path.exists():
            shutil.rmtree(stage_path)
        raise

    return {
        "status": "promoted",
        "candidate_repo": str(candidate_repo),
        "installed_skill": str(installed_resolved),
        "install_mode": install_mode,
        "candidate_skill_tree_hash": candidate_hash,
        "previous_installed_tree_hash": previous_hash,
        "installed_skill_tree_hash": hash_path_set(
            installed_resolved.parent, (installed_resolved.name + "/",)
        ),
        "terminal_receipt_path": str(receipt_path),
        "terminal_receipt_sha256": receipt_sha256,
    }


def _validated_source_tree(path: Path, field: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise SkillCandidateError(field + " must be an existing directory") from error
    if candidate.is_symlink() or not resolved.is_dir():
        raise SkillCandidateError(field + " must be a real directory")
    for entry in resolved.rglob("*"):
        if entry.name == ".git":
            raise SkillCandidateError(field + " cannot contain nested Git metadata")
        if entry.is_symlink():
            raise SkillCandidateError(field + " cannot contain symbolic links")
        if not entry.is_dir() and not entry.is_file():
            raise SkillCandidateError(field + " cannot contain special files")
    return resolved


def _validated_candidate_repo(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise SkillCandidateError("candidate_repo must exist") from error
    if candidate.is_symlink() or not resolved.is_dir():
        raise SkillCandidateError("candidate_repo must be a real directory")
    inside = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise SkillCandidateError("candidate_repo must be a Git worktree")
    manifest = resolved / ".governor" / "skill-candidate.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise SkillCandidateError("candidate_repo lacks its staged manifest")
    return resolved


def _validated_external_terminal_receipt(
    candidate_repo: Path, path: Path
) -> tuple:
    receipt_path = Path(path).expanduser()
    try:
        resolved = receipt_path.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise SkillCandidateError("terminal receipt must exist") from error
    if receipt_path.is_symlink() or not resolved.is_file():
        raise SkillCandidateError("terminal receipt must be a real file")
    if resolved == candidate_repo or candidate_repo in resolved.parents:
        raise SkillCandidateError("terminal receipt must be outside candidate_repo")
    raw = resolved.read_bytes()
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillCandidateError("terminal receipt must be valid JSON") from error
    if not isinstance(receipt, dict):
        raise SkillCandidateError("terminal receipt must be a JSON object")
    acceptance = receipt.get("acceptance")
    verification = receipt.get("verification")
    run_complete = (
        receipt.get("schema_version") == "development-governor.run-receipt.v0"
        and receipt.get("status") == "complete"
        and receipt.get("product_evidence") is True
        and receipt.get("exit_code") == 0
        and receipt.get("timed_out") is False
        and receipt.get("outside_allowed_paths") == []
        and isinstance(acceptance, dict)
        and acceptance.get("preflight_status") == "matched"
        and acceptance.get("postflight_status") == "matched"
        and acceptance.get("capsule_status") == "matched"
        and isinstance(verification, dict)
        and verification.get("exit_code") == 0
    )
    results = receipt.get("results")
    project_verification_complete = (
        receipt.get("schema_version")
        == "development-governor-verification-receipt.v0"
        and receipt.get("status") == "verification_passed"
        and receipt.get("product_evidence") is True
        and isinstance(results, list)
        and bool(results)
        and all(
            isinstance(result, dict)
            and result.get("returncode") == 0
            and result.get("execution_mode") == "isolated_snapshot"
            for result in results
        )
    )
    if not (run_complete or project_verification_complete):
        raise SkillCandidateError("promotion requires a complete terminal receipt")
    return resolved, receipt, hashlib.sha256(raw).hexdigest()


def _directory_content_hash(root: Path) -> str:
    entries = []
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise SkillCandidateError("Skill trees cannot contain symbolic links")
        if item.is_dir():
            entries.append({"path": relative + "/", "kind": "directory"})
        elif item.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                }
            )
        else:
            raise SkillCandidateError("Skill trees cannot contain special files")
    encoded = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_new_disjoint_candidate(
    source_skill: Path, acceptance_source: Path, candidate_repo: Path
) -> None:
    if candidate_repo.exists():
        raise SkillCandidateError("candidate_repo must not already exist")
    for source in (source_skill, acceptance_source):
        if candidate_repo == source or source in candidate_repo.parents:
            raise SkillCandidateError("candidate_repo must be outside source trees")
        if candidate_repo in source.parents:
            raise SkillCandidateError("source trees must be outside candidate_repo")
    if source_skill == acceptance_source:
        raise SkillCandidateError("source_skill and acceptance_source must be disjoint")
    if source_skill in acceptance_source.parents or acceptance_source in source_skill.parents:
        raise SkillCandidateError("source_skill and acceptance_source must be disjoint")


def _initialize_candidate_git(candidate_repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(candidate_repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(candidate_repo),
            "config",
            "user.email",
            "development-governor@local",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(candidate_repo),
            "config",
            "user.name",
            "Development Governor",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(candidate_repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(candidate_repo),
            "commit",
            "-qm",
            "chore: stage installed skill candidate",
        ],
        check=True,
    )


def _git_output(candidate_repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(candidate_repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
