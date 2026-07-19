"""One-root Codex supervisor with contract-gated logical-agent parallelism."""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import time
from typing import Any, Mapping, Optional, Sequence, Tuple

from development_governor.rerun import (
    EvaluationPolicy,
    RerunGateError,
    build_evaluation_request,
    reserve_evaluation,
    settle_evaluation,
)
from development_governor.lineage import (
    LineageError,
    LineagePolicy,
    lineage_ledger_path,
    reserve_lineage,
    settle_lineage,
)
from development_governor.supervisor import supervise_root_process
from development_governor.stage_control import (
    StageControlError,
    StageControlPolicy,
)


class ContractError(ValueError):
    """Raised when a run contract cannot be enforced by the experimental runner."""


@dataclass(frozen=True)
class AcceptanceFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class ParallelUnit:
    kind: str
    task_id: str
    objective: str
    deliverable_paths: Tuple[str, ...]
    acceptance_command: Tuple[str, ...]
    acceptance_files: Tuple[str, ...]


@dataclass(frozen=True)
class RunContract:
    objective: str
    repo_path: str
    model: str
    primary_mode: str
    reasoning_effort: str
    max_elapsed_seconds: int
    product_change_deadline_seconds: Optional[int]
    max_observed_total_tokens: Optional[int]
    max_parallel_agents: int
    max_total_agents: int
    max_spawn_depth: int
    review_credits: int
    allowed_paths: Tuple[str, ...]
    product_paths: Tuple[str, ...]
    verification_command: Tuple[str, ...]
    acceptance_files: Tuple[AcceptanceFile, ...]
    parallel_units: Tuple[ParallelUnit, ...]
    evaluation: Optional[EvaluationPolicy]
    lineage: LineagePolicy
    stage_control: StageControlPolicy

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RunContract":
        required = {
            "objective",
            "repo_path",
            "model",
            "primary_mode",
            "reasoning_effort",
            "max_elapsed_seconds",
            "product_change_deadline_seconds",
            "max_observed_total_tokens",
            "max_parallel_agents",
            "max_total_agents",
            "max_spawn_depth",
            "review_credits",
            "allowed_paths",
            "product_paths",
            "verification_command",
            "acceptance_files",
            "parallel_units",
            "lineage",
            "stage_control",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise ContractError("missing contract fields: " + ", ".join(missing))
        unsupported = sorted(set(raw).difference(required | {"evaluation"}))
        if unsupported:
            raise ContractError(
                "unsupported contract fields: " + ", ".join(unsupported)
            )

        objective = raw["objective"]
        repo_path = raw["repo_path"]
        model = raw["model"]
        if not isinstance(objective, str) or not objective.strip():
            raise ContractError("objective must be a non-empty string")
        if not isinstance(repo_path, str) or not Path(repo_path).is_absolute():
            raise ContractError("repo_path must be absolute")
        if not isinstance(model, str) or not model.strip():
            raise ContractError("model must be a non-empty string")
        primary_mode = raw["primary_mode"]
        if primary_mode not in ("research", "product", "governance"):
            raise ContractError(
                "primary_mode must be research, product, or governance"
            )
        reasoning_effort = raw["reasoning_effort"]
        if reasoning_effort not in (
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        ):
            raise ContractError("reasoning_effort is not supported")

        positive_fields = (
            "max_elapsed_seconds",
            "max_parallel_agents",
            "max_total_agents",
            "max_spawn_depth",
        )
        for field in positive_fields:
            value = raw[field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ContractError(field + " must be a positive integer")

        product_deadline = raw["product_change_deadline_seconds"]
        if primary_mode == "product":
            if (
                not isinstance(product_deadline, int)
                or isinstance(product_deadline, bool)
                or product_deadline <= 0
                or product_deadline > raw["max_elapsed_seconds"]
            ):
                raise ContractError(
                    "product_change_deadline_seconds must be a positive integer "
                    "within max_elapsed_seconds for product mode"
                )
        elif product_deadline is not None:
            raise ContractError(
                "product_change_deadline_seconds must be null outside product mode"
            )

        token_cap = raw["max_observed_total_tokens"]
        if token_cap is not None and (
            not isinstance(token_cap, int)
            or isinstance(token_cap, bool)
            or token_cap <= 0
        ):
            raise ContractError(
                "max_observed_total_tokens must be null or a positive integer"
            )

        review_credits = raw["review_credits"]
        if (
            not isinstance(review_credits, int)
            or isinstance(review_credits, bool)
            or review_credits < 0
        ):
            raise ContractError("review_credits must be a non-negative integer")
        if raw["max_total_agents"] < raw["max_parallel_agents"]:
            raise ContractError("max_total_agents cannot be below max_parallel_agents")

        allowed_paths = _validated_paths(raw["allowed_paths"], "allowed_paths")
        product_paths = _validated_paths(raw["product_paths"], "product_paths")
        if not product_paths:
            raise ContractError("product_paths cannot be empty")
        if primary_mode == "product":
            for product_path in product_paths:
                if not any(
                    _path_matches(product_path.rstrip("/"), allowed)
                    for allowed in allowed_paths
                ):
                    raise ContractError(
                        "every product path must be inside allowed_paths in product mode"
                    )

        verification = raw["verification_command"]
        if (
            not isinstance(verification, (list, tuple))
            or not verification
            or any(not isinstance(part, str) or not part for part in verification)
        ):
            raise ContractError("verification_command must be a non-empty string array")

        acceptance_files = _validated_acceptance_files(raw["acceptance_files"])
        frozen_acceptance_paths = {item.path for item in acceptance_files}
        if not set(verification).intersection(frozen_acceptance_paths):
            raise ContractError(
                "verification_command must reference a frozen acceptance file"
            )
        for acceptance_file in acceptance_files:
            if any(
                _paths_overlap(acceptance_file.path, allowed)
                for allowed in allowed_paths
            ):
                raise ContractError(
                    "acceptance_files must be outside allowed_paths: "
                    + acceptance_file.path
                )

        parallel_units = _validated_parallel_units(
            raw["parallel_units"],
            allowed_paths=allowed_paths,
            product_paths=product_paths,
            acceptance_files=acceptance_files,
        )
        if raw["max_spawn_depth"] != 1:
            raise ContractError("max_spawn_depth must be 1")
        if not parallel_units:
            if raw["max_parallel_agents"] != 1 or raw["max_total_agents"] != 1:
                raise ContractError(
                    "serial contracts require max_parallel_agents and max_total_agents to be 1"
                )
        else:
            unit_count = len(parallel_units)
            if raw["max_parallel_agents"] < 2:
                raise ContractError(
                    "parallel contracts require at least 2 active agents"
                )
            if raw["max_parallel_agents"] > unit_count:
                raise ContractError(
                    "max_parallel_agents cannot exceed declared parallel units"
                )
            if raw["max_total_agents"] > unit_count:
                raise ContractError(
                    "max_total_agents cannot exceed declared parallel units"
                )

        review_wave_cost = int(
            primary_mode == "governance"
            or any(unit.kind == "review" for unit in parallel_units)
        )
        if review_credits != review_wave_cost:
            raise ContractError(
                "review_credits must equal the derived review wave cost"
            )

        evaluation_raw = raw.get("evaluation")
        if evaluation_raw is None:
            evaluation = None
        else:
            try:
                evaluation = EvaluationPolicy.from_mapping(
                    evaluation_raw, Path(repo_path)
                )
            except RerunGateError as error:
                raise ContractError(str(error)) from error
        _validate_skill_candidate_contract(
            Path(repo_path).resolve(), product_paths, evaluation
        )
        try:
            lineage = LineagePolicy.from_mapping(raw["lineage"])
        except LineageError as error:
            raise ContractError(str(error)) from error
        try:
            stage_control = StageControlPolicy.from_mapping(raw["stage_control"])
        except StageControlError as error:
            raise ContractError(str(error)) from error
        if set(stage_control.current_scope.authorized_product_paths) != set(
            product_paths
        ):
            raise ContractError(
                "current scope authorized_product_paths must equal product_paths"
            )
        if primary_mode == "product" and stage_control.owner_acceptance_ref is None:
            raise ContractError(
                "product mode requires an Owner-accepted slice reference"
            )
        if primary_mode != "product" and stage_control.owner_acceptance_ref is not None:
            raise ContractError(
                "Owner-accepted slice must run in product mode"
            )
        if (
            lineage.max_review_waves
            > stage_control.max_review_batches_without_owner
        ):
            raise ContractError(
                "lineage may allocate at most one review batch without Owner credit"
            )

        return cls(
            objective=objective.strip(),
            repo_path=str(Path(repo_path).resolve()),
            model=model.strip(),
            primary_mode=primary_mode,
            reasoning_effort=reasoning_effort,
            max_elapsed_seconds=raw["max_elapsed_seconds"],
            product_change_deadline_seconds=product_deadline,
            max_observed_total_tokens=token_cap,
            max_parallel_agents=raw["max_parallel_agents"],
            max_total_agents=raw["max_total_agents"],
            max_spawn_depth=raw["max_spawn_depth"],
            review_credits=review_credits,
            allowed_paths=allowed_paths,
            product_paths=product_paths,
            verification_command=tuple(verification),
            acceptance_files=acceptance_files,
            parallel_units=parallel_units,
            evaluation=evaluation,
            lineage=lineage,
            stage_control=stage_control,
        )

    @property
    def contract_hash(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def execution_mode(self) -> str:
        if self.parallel_units:
            return "declared_parallel_units"
        return "serial_root_only"

    @property
    def product_units(self) -> Tuple[ParallelUnit, ...]:
        return tuple(unit for unit in self.parallel_units if unit.kind == "product")

    @property
    def review_units(self) -> Tuple[ParallelUnit, ...]:
        return tuple(unit for unit in self.parallel_units if unit.kind == "review")

    @property
    def review_wave_cost(self) -> int:
        return int(self.primary_mode == "governance" or bool(self.review_units))

    @property
    def acceptance_interface_hash(self) -> str:
        payload = {
            "verification_command": list(self.verification_command),
            "parallel_unit_commands": [
                {
                    "task_id": unit.task_id,
                    "command": list(unit.acceptance_command),
                    "acceptance_files": list(unit.acceptance_files),
                }
                for unit in self.parallel_units
            ],
        }
        return _canonical_hash(payload)

    @property
    def acceptance_test_bundle_hash(self) -> str:
        return _canonical_hash(
            [asdict(acceptance_file) for acceptance_file in self.acceptance_files]
        )

    @property
    def acceptance_control_fingerprint(self) -> str:
        return _canonical_hash(
            {
                "acceptance_interface_hash": self.acceptance_interface_hash,
                "acceptance_test_bundle_hash": self.acceptance_test_bundle_hash,
            }
        )


def _validated_paths(raw: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ContractError(field + " must be a non-empty string array")
    result = []
    for value in raw:
        if not isinstance(value, str) or not value:
            raise ContractError(field + " must contain non-empty strings")
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or normalized.startswith("./"):
            raise ContractError(field + " entries must be repository-relative without '..'")
        clean = str(path)
        if normalized.endswith("/"):
            clean += "/"
        if clean in ("", "."):
            raise ContractError(field + " entries must name a repository path")
        result.append(clean)
    return tuple(dict.fromkeys(result))


def _path_matches(path: str, prefix: str) -> bool:
    base = prefix.rstrip("/")
    return path == base or path.startswith(base + "/")


def _paths_overlap(left: str, right: str) -> bool:
    return _path_matches(left.rstrip("/"), right) or _path_matches(
        right.rstrip("/"), left
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def hash_path_set(root: Path, paths: Sequence[str]) -> str:
    """Hash selected repository paths without following symbolic links."""
    root = Path(root).resolve()
    entries = []
    for selected in dict.fromkeys(paths):
        normalized = selected.rstrip("/")
        candidate = root / normalized
        if not candidate.exists() and not candidate.is_symlink():
            entries.append({"path": normalized, "kind": "missing"})
            continue
        selected_paths = [candidate]
        if candidate.is_dir() and not candidate.is_symlink():
            selected_paths.extend(sorted(candidate.rglob("*")))
        for item in selected_paths:
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                entries.append(
                    {"path": relative, "kind": "symlink", "target": os.readlink(item)}
                )
            elif item.is_dir():
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
                entries.append({"path": relative, "kind": "special"})
    return _canonical_hash(entries)


def _validate_skill_candidate_contract(
    repo: Path,
    product_paths: Tuple[str, ...],
    evaluation: Optional[EvaluationPolicy],
) -> None:
    manifest_path = repo / ".governor" / "skill-candidate.json"
    if not manifest_path.exists():
        return
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ContractError("Skill candidate manifest must be a real file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("Skill candidate manifest must be valid JSON") from error
    valid_manifest = (
        isinstance(manifest, dict)
        and manifest.get("schema_version")
        == "development-governor.skill-candidate.v0"
        and manifest.get("governed_product_path") == "skill/"
        and manifest.get("frozen_acceptance_path") == "acceptance/"
        and manifest.get("acceptance_tree_hash")
        == hash_path_set(repo, ("acceptance/",))
    )
    if not valid_manifest:
        raise ContractError("Skill candidate manifest or acceptance tree is stale")
    if product_paths != ("skill/",):
        raise ContractError("Skill candidate product_paths must equal skill/")
    if evaluation is None:
        raise ContractError("Skill candidate runs require evaluation rerun gate")


def _validated_command(raw: Any, field: str) -> Tuple[str, ...]:
    if (
        not isinstance(raw, (list, tuple))
        or not raw
        or any(not isinstance(part, str) or not part for part in raw)
    ):
        raise ContractError(field + " must be a non-empty string array")
    return tuple(raw)


def _validated_acceptance_files(raw: Any) -> Tuple[AcceptanceFile, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ContractError("acceptance_files must be a non-empty array")
    result = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ContractError("acceptance_files entries must be objects")
        if "path" not in item or "sha256" not in item:
            raise ContractError("acceptance_files entries require path and sha256")
        path = _validated_paths([item["path"]], "acceptance_files.path")[0]
        if path.endswith("/"):
            raise ContractError("acceptance_files entries must name files")
        digest = item["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContractError(
                "acceptance_files[%d].sha256 must be 64 lowercase hex characters"
                % index
            )
        if path in seen:
            raise ContractError("acceptance_files paths must be unique")
        seen.add(path)
        result.append(AcceptanceFile(path=path, sha256=digest))
    return tuple(result)


def _validated_parallel_units(
    raw: Any,
    *,
    allowed_paths: Tuple[str, ...],
    product_paths: Tuple[str, ...],
    acceptance_files: Tuple[AcceptanceFile, ...],
) -> Tuple[ParallelUnit, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ContractError("parallel_units must be an array")
    if len(raw) == 1:
        raise ContractError("parallel_units must contain zero or at least two entries")

    frozen_acceptance_paths = {item.path for item in acceptance_files}
    units = []
    task_ids = set()
    commands = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractError("parallel_units entries must be objects")
        required = {
            "kind",
            "task_id",
            "objective",
            "deliverable_paths",
            "acceptance_command",
            "acceptance_files",
        }
        missing = sorted(required.difference(item))
        if missing:
            raise ContractError(
                "parallel unit missing fields: " + ", ".join(missing)
            )
        task_id = item["task_id"]
        objective = item["objective"]
        kind = item["kind"]
        if kind not in ("product", "review"):
            raise ContractError("parallel unit kind must be product or review")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ContractError("parallel unit task_id must be a non-empty string")
        if task_id in task_ids:
            raise ContractError("parallel unit task_id must be unique")
        if not isinstance(objective, str) or not objective.strip():
            raise ContractError("parallel unit objective must be a non-empty string")
        task_ids.add(task_id)

        deliverable_paths = _validated_paths(
            item["deliverable_paths"], "parallel_units.deliverable_paths"
        )
        for deliverable_path in deliverable_paths:
            if not any(
                _path_matches(deliverable_path.rstrip("/"), allowed)
                for allowed in allowed_paths
            ):
                raise ContractError(
                    "parallel unit deliverable paths must be inside allowed_paths"
                )
            if kind == "product" and not any(
                _path_matches(deliverable_path.rstrip("/"), product)
                for product in product_paths
            ):
                raise ContractError(
                    "product unit deliverable paths must be inside product_paths"
                )

        command = _validated_command(
            item["acceptance_command"], "parallel_units.acceptance_command"
        )
        if command in commands:
            raise ContractError("parallel unit acceptance commands must be distinct")
        commands.add(command)

        acceptance_refs = _validated_paths(
            item["acceptance_files"], "parallel_units.acceptance_files"
        )
        unknown_refs = sorted(set(acceptance_refs).difference(frozen_acceptance_paths))
        if unknown_refs:
            raise ContractError(
                "parallel unit acceptance files must reference frozen acceptance_files"
            )
        if not set(command).intersection(acceptance_refs):
            raise ContractError(
                "parallel unit acceptance_command must reference its acceptance_files"
            )
        units.append(
            ParallelUnit(
                kind=kind,
                task_id=task_id.strip(),
                objective=objective.strip(),
                deliverable_paths=deliverable_paths,
                acceptance_command=command,
                acceptance_files=acceptance_refs,
            )
        )

    for index, left in enumerate(units):
        for right in units[index + 1 :]:
            if any(
                _paths_overlap(left_path, right_path)
                for left_path in left.deliverable_paths
                for right_path in right.deliverable_paths
            ):
                raise ContractError("parallel unit deliverable paths must be disjoint")
            if set(left.acceptance_files).intersection(right.acceptance_files):
                raise ContractError("parallel unit acceptance files must be disjoint")
    return tuple(units)


def validate_acceptance_material(contract: RunContract) -> dict:
    return _acceptance_material_status(
        Path(contract.repo_path).resolve(), contract.acceptance_files
    )


def _acceptance_material_status(
    root: Path, acceptance_files: Sequence[AcceptanceFile]
) -> dict:
    root = Path(root).resolve()
    mismatches = []
    actual_files = []
    for expected in acceptance_files:
        candidate = root / expected.path
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            mismatches.append(expected.path)
            actual_files.append({"path": expected.path, "sha256": None})
            continue
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or (resolved != root and root not in resolved.parents)
        ):
            mismatches.append(expected.path)
            actual_files.append({"path": expected.path, "sha256": None})
            continue
        actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        actual_files.append({"path": expected.path, "sha256": actual_hash})
        if actual_hash != expected.sha256:
            mismatches.append(expected.path)
    return {
        "status": "matched" if not mismatches else "mismatch",
        "mismatched_files": mismatches,
        "actual_test_bundle_hash": _canonical_hash(actual_files),
    }


def _create_acceptance_capsule(contract: RunContract, output_dir: Path) -> Path:
    capsule_root = Path(output_dir).resolve() / "acceptance-capsule"
    if capsule_root.exists():
        raise ContractError("output_dir acceptance capsule already exists")
    repo = Path(contract.repo_path).resolve()
    created_directories = {capsule_root}
    for acceptance_file in contract.acceptance_files:
        source = repo / acceptance_file.path
        destination = capsule_root / acceptance_file.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        created_directories.update(
            parent
            for parent in destination.parents
            if parent == capsule_root or capsule_root in parent.parents
        )
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o444)
    status = _acceptance_material_status(capsule_root, contract.acceptance_files)
    if status["status"] != "matched":
        raise ContractError("failed to create verified acceptance capsule")
    for directory in sorted(
        created_directories, key=lambda path: len(path.parts), reverse=True
    ):
        directory.chmod(0o555)
    return capsule_root


def _capsule_command(command: Sequence[str], capsule_root: Path) -> Tuple[str, ...]:
    replacements = {
        part: str(capsule_root / part)
        for part in command
        if (capsule_root / part).is_file()
    }
    return tuple(replacements.get(part, part) for part in command)


def build_coordinator_prompt(contract: RunContract) -> str:
    _require_stage_admission(contract)
    allowed = ", ".join(contract.allowed_paths)
    products = ", ".join(contract.product_paths)
    stage_decision = contract.stage_control.decision
    proposed_gates = ", ".join(stage_decision.proposed_gate_ids) or "none"
    active_gates = ", ".join(stage_decision.active_gate_ids) or "none"
    frozen_header = f"""{contract.objective}

You are the single root coordinator for this governed development run. Apply the
installed karpathy-guidelines skill as the coding-behavior baseline.

Frozen coordination contract:
- execution mode: {contract.execution_mode}
- primary mode: {contract.primary_mode}
- model reasoning effort: {contract.reasoning_effort}
- maximum active logical agents: {contract.max_parallel_agents}
- maximum total logical agents: {contract.max_total_agents}
- maximum spawn depth: {contract.max_spawn_depth}
- review wave credits requested: {contract.review_credits}
- review wave cost: {contract.review_wave_cost}
- allowed paths: {allowed}
- product paths: {products}
- current authorized scope: {contract.stage_control.current_scope_id}
- stage admission: {stage_decision.action}
- proposed nonblocking Gates: {proposed_gates}
- Owner-activated Gates: {active_gates}
- Owner acceptance reference: {contract.stage_control.owner_acceptance_ref or "none"}
- Owner revision reference: {contract.stage_control.owner_revision_ref or "none"}
- acceptance interface hash: {contract.acceptance_interface_hash}
- acceptance test bundle hash: {contract.acceptance_test_bundle_hash}
"""
    final_rule = """
The final response must report changed product paths and the verification result.
Prose, plans, reviews, or successful model exit alone are not Product Evidence.
The frozen acceptance interface is external authority. Never edit its files,
replace its command, or define a substitute success criterion.
Authorization and progress claims apply only to the current capability/stage
scope. Never label a scoped result as global acceptance. Newly proposed Gates
are nonblocking until an Owner decision explicitly activates them. Automatic
post-review revision or re-review is forbidden; a revision verdict returns to
the Owner instead of starting another review loop.
"""
    if contract.primary_mode != "product":
        final_rule += """
This is a non-product mode. Report its artifacts and verification, but never
classify them as Product Evidence or Product Progress.
"""
    evaluation_rule = ""
    if contract.evaluation is not None:
        scopes = ", ".join(contract.evaluation.scope_ids)
        impacted = ", ".join(contract.evaluation.impacted_scope_ids) or "none"
        evaluation_rule = f"""

External evaluation gate:
- external evaluation phase: {contract.evaluation.phase}
- catalog-bound scope: {scopes}
- impacted GREEN scope: {impacted}
- control fingerprint: {contract.acceptance_control_fingerprint}

The launcher reserves this exact evaluation identity once before Codex starts.
RED identity ignores the product tree. GREEN identity binds the pre-run product
tree and may cover only the declared impacted scope. Do not execute or repeat the
frozen external acceptance yourself. Use focused local tests after a relevant
code change; an unchanged command rerun is not Product Evidence.
"""
    if not contract.parallel_units:
        return frozen_header + """

This is a serial TDD slice and multi_agent is disabled by the launcher. Keep the
entire red-green-refactor loop at the root. Do not spawn workers, reviewers, or
read-only probes. Read-only status is not an independent deliverable.
""" + evaluation_rule + final_rule

    units = json.dumps(
        [asdict(unit) for unit in contract.parallel_units],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return frozen_header + f"""

Native multi-agent execution is enabled only for these declared parallel units:
{units}

For every worker spawn, set fork_turns="none"; never omit it and never use `all`.
The spawn message must be a self-contained task envelope containing the task ID,
objective, dependencies, repository path, role, read/write scope, allowed paths,
expected evidence, and stop conditions. Do not copy broad conversation or project
history into the worker message.

Spawn at most one worker for each declared parallel unit. Its write scope is the
unit's deliverable_paths and its success criterion is the unit's frozen
acceptance_command plus acceptance_files. Unlisted workers, reviewers, and
read-only probes are forbidden. A read-only probe is not independent merely
because it does not write. Workers must not spawn descendants. Integrate the
parallel wave once. If one worker fails, preserve completed sibling results and
handle only the failed slice. Do not restart the full parallel wave. Review
credits never authorize an unlisted reviewer.
""" + evaluation_rule + final_rule


def build_codex_command(
    contract: RunContract, codex_executable: str = "codex"
) -> Sequence[str]:
    _require_stage_admission(contract)
    feature_control = (
        ["--enable", "multi_agent"]
        if contract.parallel_units
        else ["--disable", "multi_agent"]
    )
    command = [
        codex_executable,
        *feature_control,
        "--strict-config",
        "-c",
        'model_reasoning_effort="' + contract.reasoning_effort + '"',
        "-m",
        contract.model,
        "-s",
        "workspace-write",
        "-a",
        "never",
        "-C",
        contract.repo_path,
        "exec",
    ]
    if contract.lineage.resume_session_id is not None:
        command.extend(
            [
                "resume",
                "--json",
                contract.lineage.resume_session_id,
                build_coordinator_prompt(contract),
            ]
        )
    else:
        command.extend(["--json", build_coordinator_prompt(contract)])
    return command


class DevelopmentGovernor:
    def __init__(
        self,
        codex_executable: str = "codex",
        *,
        state_root: Optional[Path] = None,
    ):
        self.codex_executable = codex_executable
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else (Path.home() / ".codex" / "development-governor" / "v0").resolve()
        )

    def run(self, contract: RunContract, output_dir: Path) -> dict:
        repo = Path(contract.repo_path).resolve()
        output_dir = Path(output_dir).resolve()
        if output_dir == repo or repo in output_dir.parents:
            raise ContractError("output_dir must be outside the governed repository")
        if self.state_root == repo or repo in self.state_root.parents:
            raise ContractError("state_root must be outside the governed repository")
        _require_stage_admission(contract)
        preflight_acceptance = validate_acceptance_material(contract)
        if preflight_acceptance["status"] != "matched":
            raise ContractError(
                "acceptance material hash mismatch: "
                + ", ".join(preflight_acceptance["mismatched_files"])
            )
        _require_clean_git_worktree(repo)
        baseline_head = _git_head(repo)
        baseline_product_tree_hash = hash_path_set(repo, contract.product_paths)
        try:
            lineage_path = lineage_ledger_path(
                self.state_root, repo, contract.lineage.lineage_root_id
            )
        except LineageError as error:
            raise ContractError(str(error)) from error
        if lineage_path == output_dir or output_dir in lineage_path.parents:
            raise ContractError("lineage ledger must be outside output_dir")
        try:
            lineage_reservation = reserve_lineage(
                contract.lineage,
                ledger_path=lineage_path,
                contract_hash=contract.contract_hash,
                candidate_hash=baseline_head,
                requested_elapsed_seconds=contract.max_elapsed_seconds,
                requested_review_waves=contract.review_credits,
                current_scope_id=contract.stage_control.current_scope_id,
                primary_mode=contract.primary_mode,
                owner_acceptance_ref=contract.stage_control.owner_acceptance_ref,
                owner_revision_ref=contract.stage_control.owner_revision_ref,
            )
        except LineageError as error:
            raise ContractError(str(error)) from error
        evaluation_request = None
        evaluation_reservation = None
        process = None
        try:
            if contract.evaluation is not None:
                ledger_path = Path(contract.evaluation.ledger_path)
                if ledger_path == output_dir or output_dir in ledger_path.parents:
                    raise ContractError("evaluation ledger must be outside output_dir")
                evaluation_request = build_evaluation_request(
                    contract.evaluation,
                    contract.acceptance_control_fingerprint,
                    baseline_product_tree_hash,
                )
                evaluation_reservation = reserve_evaluation(
                    contract.evaluation,
                    evaluation_request,
                    contract.contract_hash,
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            acceptance_capsule = _create_acceptance_capsule(contract, output_dir)
            command = list(build_codex_command(contract, self.codex_executable))
            process = subprocess.Popen(
                command,
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
        except (ContractError, LineageError, RerunGateError, OSError) as error:
            if evaluation_reservation is not None:
                settle_evaluation(
                    Path(evaluation_reservation["ledger_path"]),
                    evaluation_reservation["reservation_id"],
                    "runner_error",
                )
            settle_lineage(
                lineage_path,
                lineage_reservation["reservation_id"],
                terminal_status="runner_error",
                model_started=False,
                actual_elapsed_seconds=0,
                session_id=None,
            )
            if isinstance(error, ContractError):
                raise
            raise ContractError(str(error)) from error

        supervision_started_at = time.monotonic()
        try:
            supervision = supervise_root_process(
                process,
                raw_events_path=output_dir / "raw-events.jsonl",
                stderr_path=output_dir / "stderr.txt",
                changed_paths_probe=lambda: _git_changed_paths(repo, baseline_head),
                allowed_paths=contract.allowed_paths,
                product_paths=contract.product_paths,
                max_elapsed_seconds=contract.max_elapsed_seconds,
                product_change_deadline_seconds=(
                    contract.product_change_deadline_seconds
                    if contract.primary_mode == "product"
                    else None
                ),
                max_observed_total_tokens=contract.max_observed_total_tokens,
                token_usage_from_jsonl=_token_usage_from_jsonl,
            )
        except Exception as error:
            _terminate_process_group(process)
            settlement_errors = []
            try:
                settle_lineage(
                    lineage_path,
                    lineage_reservation["reservation_id"],
                    terminal_status="runner_error",
                    model_started=True,
                    actual_elapsed_seconds=(
                        max(0.0, time.monotonic() - supervision_started_at)
                    ),
                    session_id=None,
                )
            except LineageError as settlement_error:
                settlement_errors.append(str(settlement_error))
            if evaluation_reservation is not None:
                try:
                    settle_evaluation(
                        Path(evaluation_reservation["ledger_path"]),
                        evaluation_reservation["reservation_id"],
                        "runner_error",
                    )
                except RerunGateError as settlement_error:
                    settlement_errors.append(str(settlement_error))
            message = "online supervision failed: " + str(error)
            if settlement_errors:
                message += "; settlement failed: " + "; ".join(
                    settlement_errors
                )
            raise ContractError(message) from error
        stdout = (output_dir / "raw-events.jsonl").read_text(
            encoding="utf-8", errors="replace"
        )
        session_id = _session_id_from_jsonl(stdout)
        token_usage = _token_usage_from_jsonl(stdout)
        changed_paths = _git_changed_paths(repo, baseline_head)
        postflight_acceptance = validate_acceptance_material(contract)
        postflight_capsule = _acceptance_material_status(
            acceptance_capsule, contract.acceptance_files
        )
        final_outside_scope = [
            path
            for path in changed_paths
            if not any(_path_matches(path, prefix) for prefix in contract.allowed_paths)
        ]
        outside_scope = sorted(
            set(final_outside_scope).union(supervision.outside_paths_at_stop)
        )

        verification = None
        parallel_unit_acceptance = []
        product_evidence = False
        mode_evidence = False
        acceptance_unchanged = (
            postflight_acceptance["status"] == "matched"
            and postflight_capsule["status"] == "matched"
        )
        if (
            supervision.stop_reason is None
            and process.returncode == 0
            and not outside_scope
            and acceptance_unchanged
        ):
            verification = _run_verification(contract, acceptance_capsule)
            parallel_unit_acceptance = [
                {
                    "task_id": unit.task_id,
                    **_run_command(
                        _capsule_command(unit.acceptance_command, acceptance_capsule),
                        repo_path=contract.repo_path,
                        timeout_seconds=min(contract.max_elapsed_seconds, 300),
                    ),
                }
                for unit in contract.parallel_units
            ]
            product_change = any(
                _path_matches(path, prefix)
                for path in changed_paths
                for prefix in contract.product_paths
            )
            unit_acceptance_closed = all(
                result["exit_code"] == 0 for result in parallel_unit_acceptance
            )
            mode_evidence = (
                bool(changed_paths)
                and verification["exit_code"] == 0
                and unit_acceptance_closed
            )
            product_evidence = (
                contract.primary_mode == "product"
                and mode_evidence
                and product_change
            )

        status, reason, next_action = _terminal_decision(
            timed_out=supervision.timed_out,
            supervisor_stop_reason=supervision.stop_reason,
            exit_code=process.returncode,
            session_id=session_id,
            outside_scope=outside_scope,
            acceptance_material_unchanged=acceptance_unchanged,
            primary_mode=contract.primary_mode,
            mode_evidence=mode_evidence,
            product_evidence=product_evidence,
            product_evidence_fuse=(
                contract.stage_control.product_evidence_fuse(product_evidence)
            ),
        )
        try:
            lineage_settlement = settle_lineage(
                lineage_path,
                lineage_reservation["reservation_id"],
                terminal_status=status,
                model_started=True,
                actual_elapsed_seconds=supervision.elapsed_seconds,
                session_id=session_id,
            )
        except LineageError as error:
            raise ContractError(str(error)) from error
        evaluation_settlement = None
        if evaluation_reservation is not None:
            try:
                evaluation_settlement = settle_evaluation(
                    Path(evaluation_reservation["ledger_path"]),
                    evaluation_reservation["reservation_id"],
                    status,
                )
            except RerunGateError as error:
                raise ContractError(str(error)) from error
        lineage_projection = lineage_settlement["projection"]
        hard_controls = [
            "single_root_invocation",
            "root_elapsed_cap",
            "root_process_group_termination",
            "workspace_write_sandbox",
            "frozen_contract_and_verification",
            "frozen_acceptance_interface_and_test_hashes",
            "periodic_git_scope_probe",
            "lineage_budget_ledger",
            "review_wave_admission_gate",
            "stage_capability_local_admission",
            "owner_activated_gate_admission",
            "single_default_review_batch_per_lineage",
            "lineage_owner_acceptance_persistence",
            "post_review_revision_owner_gate",
        ]
        if contract.primary_mode == "product":
            hard_controls.append("product_change_deadline")
            hard_controls.append("post_acceptance_product_evidence_fuse")
        soft_controls = []
        if contract.max_observed_total_tokens is not None:
            if supervision.token_observability_mode == "streaming":
                hard_controls.append("observed_token_cap")
            elif supervision.token_observability_mode == "terminal_only":
                soft_controls.append("terminal_token_accounting")
            else:
                soft_controls.append("observed_token_cap_unavailable")
        if contract.parallel_units:
            hard_controls.append("declared_parallel_unit_schema_gate")
            soft_controls.extend(
                [
                    "native_logical_agent_limits",
                    "logical_worker_resource_scopes",
                    "isolated_worker_context_prompt",
                ]
            )
        else:
            hard_controls.append("serial_multi_agent_disabled")
        if evaluation_reservation is not None:
            hard_controls.append("external_hash_bound_rerun_ledger")
        receipt = {
            "schema_version": "development-governor.run-receipt.v0",
            "status": status,
            "reason": reason,
            "next_action": next_action,
            "contract_hash": contract.contract_hash,
            "execution_mode": contract.execution_mode,
            "primary_mode": contract.primary_mode,
            "reasoning_effort": contract.reasoning_effort,
            "session_id": session_id,
            "token_usage": token_usage,
            "exit_code": process.returncode,
            "timed_out": supervision.timed_out,
            "elapsed_seconds": supervision.elapsed_seconds,
            "invocation_count": lineage_projection["invocations_spent"],
            "repository": {
                "path": str(repo),
                "baseline_head": baseline_head,
                "product_paths": list(contract.product_paths),
                "baseline_product_tree_hash": baseline_product_tree_hash,
                "final_product_tree_hash": hash_path_set(
                    repo, contract.product_paths
                ),
            },
            "changed_paths": changed_paths,
            "outside_allowed_paths": outside_scope,
            "acceptance": {
                "interface_hash": contract.acceptance_interface_hash,
                "test_bundle_hash": contract.acceptance_test_bundle_hash,
                "preflight_status": preflight_acceptance["status"],
                "postflight_status": postflight_acceptance["status"],
                "postflight_actual_test_bundle_hash": postflight_acceptance[
                    "actual_test_bundle_hash"
                ],
                "mismatched_files": postflight_acceptance["mismatched_files"],
                "capsule_status": postflight_capsule["status"],
                "capsule_mismatched_files": postflight_capsule["mismatched_files"],
            },
            "verification": verification,
            "parallel_unit_acceptance": parallel_unit_acceptance,
            "mode_evidence": mode_evidence,
            "product_evidence": product_evidence,
            "stage_control": contract.stage_control.as_mapping(
                product_evidence=product_evidence
            ),
            "supervision": {
                "stop_reason": supervision.stop_reason,
                "changed_paths_at_stop": list(
                    supervision.changed_paths_at_stop
                ),
                "outside_paths_at_stop": list(
                    supervision.outside_paths_at_stop
                ),
                "product_change_observed_at_deadline": (
                    supervision.product_change_observed_at_deadline
                ),
                "stream_truncated": supervision.stream_truncated,
                "product_change_deadline_seconds": (
                    contract.product_change_deadline_seconds
                ),
                "max_observed_total_tokens": (
                    contract.max_observed_total_tokens
                ),
                "token_observability_mode": (
                    supervision.token_observability_mode
                ),
                "token_budget_exceeded": supervision.token_budget_exceeded,
                "completion_event_observed": (
                    supervision.completion_event_observed
                ),
            },
            "lineage": {
                "lineage_root_id": contract.lineage.lineage_root_id,
                "ledger_path": str(lineage_path),
                "reservation": lineage_reservation,
                "settlement": lineage_settlement,
                **lineage_projection,
            },
            "review_budget": {
                "requested": contract.review_credits,
                "review_unit_count": len(contract.review_units),
                "waves_required": contract.review_wave_cost,
                "waves_spent": lineage_projection["review_waves_spent"],
                "remaining": lineage_projection["review_waves_remaining"],
            },
            "evaluation": (
                None
                if evaluation_request is None
                else {
                    "phase": evaluation_request.phase,
                    "catalog_scope_ids": list(
                        contract.evaluation.catalog_scope_ids
                    ),
                    "scope_ids": list(evaluation_request.scope_ids),
                    "impacted_scope_ids": list(
                        contract.evaluation.impacted_scope_ids
                    ),
                    "control_fingerprint": (
                        evaluation_request.control_fingerprint
                    ),
                    "treatment_fingerprint": (
                        evaluation_request.treatment_fingerprint
                    ),
                    "evaluation_fingerprint": (
                        evaluation_request.evaluation_fingerprint
                    ),
                    "reservation": evaluation_reservation,
                    "settlement": evaluation_settlement,
                }
            ),
            "hard_controls": hard_controls,
            "soft_controls": soft_controls,
        }
        _atomic_json_write(output_dir / "terminal-receipt.json", receipt)
        return receipt


def _require_clean_git_worktree(repo: Path) -> None:
    if not repo.is_dir():
        raise ContractError("repo_path does not exist")
    inside = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ContractError("repo_path must be a Git worktree")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ContractError("experimental v0 requires a clean isolated worktree")


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_changed_paths(repo: Path, baseline_head: str) -> list:
    committed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", baseline_head + "..HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    working = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return sorted(set(committed + working + untracked))


def _run_verification(contract: RunContract, capsule_root: Path) -> dict:
    return _run_command(
        _capsule_command(contract.verification_command, capsule_root),
        repo_path=contract.repo_path,
        timeout_seconds=min(contract.max_elapsed_seconds, 300),
    )


def _terminate_process_group(process, grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    if not _wait_for_process_group_exit(process, grace_seconds):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _wait_for_process_group_exit(process, grace_seconds)
    if process.poll() is None:
        process.wait(timeout=grace_seconds)


def _wait_for_process_group_exit(process, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return process.returncode is not None
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _run_command(
    command: Sequence[str], *, repo_path: str, timeout_seconds: int
) -> dict:
    try:
        result = subprocess.run(
            list(command),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "command": list(command),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "command": list(command),
            "exit_code": None,
            "stdout": error.stdout or "",
            "stderr": error.stderr or "",
            "timed_out": True,
        }


def _session_id_from_jsonl(raw: str) -> Optional[str]:
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_session_id(item)
        if found:
            return found
    return None


def _token_usage_from_jsonl(raw: str) -> dict:
    observed = None
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue

        candidate = None
        payload = item.get("payload")
        if (
            item.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            info = payload.get("info")
            if isinstance(info, dict):
                candidate = info.get("total_token_usage")
        elif isinstance(item.get("usage"), dict):
            candidate = item["usage"]

        normalized = _normalized_token_usage(candidate)
        if normalized is not None and (
            observed is None
            or normalized.get("total_tokens", -1)
            >= observed.get("total_tokens", -1)
        ):
            observed = normalized

    if observed is None:
        return {"status": "unavailable"}
    return {"status": "observed", **observed}


def _normalized_token_usage(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    result = {}
    for field in fields:
        if field not in value:
            continue
        token_count = value[field]
        if (
            not isinstance(token_count, int)
            or isinstance(token_count, bool)
            or token_count < 0
        ):
            return None
        result[field] = token_count
    if (
        "total_tokens" not in result
        and "input_tokens" in result
        and "output_tokens" in result
    ):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result or None


def _find_session_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("thread_id", "session_id", "threadId", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = _find_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_session_id(child)
            if found:
                return found
    return None


def _terminal_decision(
    timed_out: bool,
    supervisor_stop_reason: Optional[str],
    exit_code: Optional[int],
    session_id: Optional[str],
    outside_scope: Sequence[str],
    acceptance_material_unchanged: bool,
    primary_mode: str,
    mode_evidence: bool,
    product_evidence: bool,
    product_evidence_fuse: str,
) -> Tuple[str, str, str]:
    if timed_out:
        return "stopped", "elapsed_budget_exhausted", "inspect terminal receipt"
    if not acceptance_material_unchanged:
        return "stopped", "acceptance_material_changed", "inspect and preserve worktree"
    if outside_scope:
        return "stopped", "changed_path_outside_contract", "inspect and preserve worktree"
    if supervisor_stop_reason is not None:
        return "stopped", supervisor_stop_reason, "inspect terminal receipt"
    if exit_code != 0:
        if session_id:
            return (
                "interrupted",
                "root_session_interrupted",
                "codex exec resume " + session_id + " after Owner invocation allowance",
            )
        return "stopped", "root_failed_without_session", "inspect stderr"
    if primary_mode != "product":
        if mode_evidence:
            return "complete", "non_product_mode_verification_closed", "none"
        return "need_owner", "non_product_mode_evidence_missing", "inspect changed paths and verification"
    if product_evidence:
        return "complete", "product_evidence_closed", "none"
    if product_evidence_fuse == "tripped":
        return (
            "stopped",
            "post_acceptance_product_evidence_fuse_tripped",
            "inspect the product slice; do not start spec repair or re-review automatically",
        )
    return "need_owner", "product_evidence_missing", "inspect changed paths and verification"


def _require_stage_admission(contract: RunContract) -> None:
    decision = contract.stage_control.decision
    if decision.action == "allow_current_scope":
        return
    safe = ", ".join(decision.safe_work_remaining_scope_ids) or "none"
    raise ContractError(
        "stage admission is "
        + decision.action
        + "; current scope is not authorized; safe work remaining: "
        + safe
    )


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(path))
