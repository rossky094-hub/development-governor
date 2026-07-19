"""Hash-bound contracts for project-aware, read-only Spec review runs."""

from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Any, Mapping, Optional, Tuple

from development_governor.lineage import (
    LineageError,
    LineagePolicy,
    lineage_ledger_path,
    lineage_projection,
    lineage_ledger_sha256,
    reserve_lineage,
    settle_lineage,
)
from development_governor.runner import (
    _git_changed_paths,
    _git_head,
    _require_clean_git_worktree,
    _session_id_from_jsonl,
    _terminate_process_group,
    _token_usage_from_jsonl,
)
from development_governor.supervisor import (
    codex_stream_observation,
    supervise_root_process,
)


_CONTEXT_ROLES = {
    "project_goal",
    "parent_baseline",
    "contract",
    "decision_record",
    "authority_record",
    "evidence",
    "open_obligations",
    "prior_review_receipt",
    "trusted_diff",
    "dependency_map",
    "prior_finding_map",
}


class ProjectReviewError(ValueError):
    """Raised when a project review contract cannot be enforced."""


@dataclass(frozen=True)
class ReviewFile:
    path: str
    sha256: str
    role: Optional[str] = None


@dataclass(frozen=True)
class ReviewerSkill:
    root: str
    files: Tuple[ReviewFile, ...]


@dataclass(frozen=True)
class ReviewScope:
    kind: str
    scope_id: str
    objective: str
    acceptance_id: str
    context_paths: Tuple[str, ...]
    depends_on: Tuple[str, ...]
    max_elapsed_seconds: int
    max_observed_total_tokens: Optional[int]


@dataclass(frozen=True)
class ReviewWorkspace:
    output_dir: Path
    context_root: Path
    manifest_path: Path
    output_schema_path: Path
    review_receipt_path: Path
    review_batch_id: str
    context_tree_sha256: str
    output_schema_sha256: str


@dataclass(frozen=True)
class SegmentWorkspace:
    output_dir: Path
    context_root: Path
    manifest_path: Path
    output_schema_path: Path
    review_receipt_path: Path
    context_tree_sha256: str
    output_schema_sha256: str


@dataclass(frozen=True)
class ProjectReviewContract:
    schema_version: str
    objective: str
    repo_path: str
    model: str
    reasoning_effort: str
    max_elapsed_seconds: int
    max_observed_total_tokens: Optional[int]
    max_parallel_agents: int
    max_total_agents: int
    max_spawn_depth: int
    review_mode: str
    review_scope_id: str
    owner_review_authorization_ref: str
    owner_revision_ref: Optional[str]
    candidate: ReviewFile
    context_inputs: Tuple[ReviewFile, ...]
    reviewer_skill: ReviewerSkill
    acceptance_target_scope_ids: Tuple[str, ...]
    review_scopes: Tuple[ReviewScope, ...]
    lineage: LineagePolicy
    _legacy_lineage_read_only: bool = False

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        allow_legacy_lineage: bool = False,
    ) -> "ProjectReviewContract":
        if not isinstance(raw, Mapping):
            raise ProjectReviewError("project review contract must be an object")
        required = {
            "schema_version",
            "objective",
            "repo_path",
            "model",
            "reasoning_effort",
            "max_elapsed_seconds",
            "max_observed_total_tokens",
            "max_parallel_agents",
            "max_total_agents",
            "max_spawn_depth",
            "review_mode",
            "review_scope_id",
            "owner_review_authorization_ref",
            "owner_revision_ref",
            "candidate",
            "context_inputs",
            "reviewer_skill",
            "acceptance_target_scope_ids",
            "review_scopes",
            "lineage",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise ProjectReviewError(
                "project review contract missing fields: " + ", ".join(missing)
            )
        unsupported = sorted(set(raw).difference(required))
        if unsupported:
            raise ProjectReviewError(
                "project review contract contains unsupported fields: "
                + ", ".join(unsupported)
            )
        if raw["schema_version"] != "development-governor.project-review-contract.v0":
            raise ProjectReviewError("unsupported project review schema_version")
        repo = Path(_nonempty(raw["repo_path"], "repo_path")).expanduser().resolve()
        if not repo.is_dir():
            raise ProjectReviewError("repo_path must be an existing directory")

        candidate = _project_file(raw["candidate"], repo, role=None)
        context_inputs = _context_files(raw["context_inputs"], repo)
        context_paths = [item.path for item in context_inputs]
        if candidate.path in context_paths or len(set(context_paths)) != len(context_paths):
            raise ProjectReviewError(
                "candidate and context input paths must be unique"
            )
        reviewer_skill = _reviewer_skill(raw["reviewer_skill"], repo)
        review_mode = _nonempty(raw["review_mode"], "review_mode")
        if review_mode not in {"full", "incremental"}:
            raise ProjectReviewError("review_mode must be full or incremental")
        reasoning_effort = _nonempty(raw["reasoning_effort"], "reasoning_effort")
        if reasoning_effort not in {
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }:
            raise ProjectReviewError("reasoning_effort is not supported")
        owner_revision_ref = _optional_string(
            raw["owner_revision_ref"], "owner_revision_ref"
        )
        if review_mode == "incremental":
            roles = {item.role for item in context_inputs}
            required_impact_roles = {
                "prior_review_receipt",
                "trusted_diff",
                "dependency_map",
                "prior_finding_map",
            }
            if (
                not required_impact_roles.issubset(roles)
                or owner_revision_ref is None
            ):
                raise ProjectReviewError(
                    "incremental review requires prior receipt, trusted diff, "
                    "dependency map, prior finding map, and owner_revision_ref"
                )
        targets = _unique_strings(
            raw["acceptance_target_scope_ids"], "acceptance_target_scope_ids"
        )
        review_scopes = _review_scopes(
            raw["review_scopes"], context_inputs=context_inputs
        )
        max_elapsed_seconds = _positive_int(
            raw["max_elapsed_seconds"], "max_elapsed_seconds"
        )
        max_observed_total_tokens = _optional_positive_int(
            raw["max_observed_total_tokens"], "max_observed_total_tokens"
        )
        max_parallel_agents = _positive_int(
            raw["max_parallel_agents"], "max_parallel_agents"
        )
        max_total_agents = _positive_int(
            raw["max_total_agents"], "max_total_agents"
        )
        max_spawn_depth = _positive_int(
            raw["max_spawn_depth"], "max_spawn_depth"
        )
        if max_spawn_depth != 1:
            raise ProjectReviewError("max_spawn_depth must be exactly one")
        if not review_scopes:
            if max_parallel_agents != 1 or max_total_agents != 1:
                raise ProjectReviewError(
                    "serial review requires one active and total agent"
                )
        else:
            independent = [
                item for item in review_scopes if item.kind == "independent"
            ]
            joins = [item for item in review_scopes if item.kind == "join"]
            if len(independent) < 2:
                raise ProjectReviewError(
                    "segmented review requires at least two independent scopes"
                )
            if len(joins) != 1:
                raise ProjectReviewError(
                    "segmented review requires exactly one cross-scope join"
                )
            if any(item.depends_on for item in independent):
                raise ProjectReviewError(
                    "independent review scopes cannot declare dependencies"
                )
            independent_ids = {item.scope_id for item in independent}
            if set(joins[0].depends_on) != independent_ids:
                raise ProjectReviewError(
                    "cross-scope join must depend on every independent scope"
                )
            if (
                max_parallel_agents < 2
                or max_parallel_agents > len(independent)
            ):
                raise ProjectReviewError(
                    "max_parallel_agents must be between two and independent scope count"
                )
            if max_total_agents != len(review_scopes):
                raise ProjectReviewError(
                    "max_total_agents must equal the declared segment count"
                )
            if (
                sum(item.max_elapsed_seconds for item in review_scopes)
                > max_elapsed_seconds
            ):
                raise ProjectReviewError(
                    "segment elapsed budgets exceed the contract cap"
                )
            segment_token_budgets = [
                item.max_observed_total_tokens for item in review_scopes
            ]
            if max_observed_total_tokens is not None and (
                any(value is None for value in segment_token_budgets)
                or sum(value for value in segment_token_budgets if value is not None)
                > max_observed_total_tokens
            ):
                raise ProjectReviewError(
                    "segment token budgets must be closed within the contract cap"
                )
        try:
            lineage = LineagePolicy.from_mapping(raw["lineage"])
        except LineageError as error:
            raise ProjectReviewError(str(error)) from error
        if lineage.max_review_waves != 1:
            raise ProjectReviewError(
                "project review lineage must default to exactly one review wave"
            )
        if review_scopes and lineage.resume_session_id is not None:
            raise ProjectReviewError(
                "segmented review uses checkpoint recovery, not session resume"
            )
        campaign_id = derive_project_review_campaign_id(
            repo_path=repo,
            candidate_sha256=candidate.sha256,
            acceptance_target_scope_ids=targets,
            owner_review_authorization_ref=raw[
                "owner_review_authorization_ref"
            ],
        )
        lineage_mismatch = lineage.lineage_root_id != campaign_id
        if not allow_legacy_lineage and lineage_mismatch:
            raise ProjectReviewError(
                "lineage_root_id must equal the deterministic review campaign identity "
                + campaign_id
            )

        return cls(
            schema_version=raw["schema_version"],
            objective=_nonempty(raw["objective"], "objective"),
            repo_path=str(repo),
            model=_nonempty(raw["model"], "model"),
            reasoning_effort=reasoning_effort,
            max_elapsed_seconds=max_elapsed_seconds,
            max_observed_total_tokens=max_observed_total_tokens,
            max_parallel_agents=max_parallel_agents,
            max_total_agents=max_total_agents,
            max_spawn_depth=max_spawn_depth,
            review_mode=review_mode,
            review_scope_id=_nonempty(raw["review_scope_id"], "review_scope_id"),
            owner_review_authorization_ref=_nonempty(
                raw["owner_review_authorization_ref"],
                "owner_review_authorization_ref",
            ),
            owner_revision_ref=owner_revision_ref,
            candidate=candidate,
            context_inputs=context_inputs,
            reviewer_skill=reviewer_skill,
            acceptance_target_scope_ids=targets,
            review_scopes=review_scopes,
            lineage=lineage,
            _legacy_lineage_read_only=(
                bool(allow_legacy_lineage) and lineage_mismatch
            ),
        )

    @property
    def context_hash(self) -> str:
        return _canonical_hash([asdict(item) for item in self.context_inputs])

    @property
    def skill_bundle_hash(self) -> str:
        return _canonical_hash(
            {
                "root": self.reviewer_skill.root,
                "files": [asdict(item) for item in self.reviewer_skill.files],
            }
        )

    @property
    def review_campaign_id(self) -> str:
        return derive_project_review_campaign_id(
            repo_path=self.repo_path,
            candidate_sha256=self.candidate.sha256,
            acceptance_target_scope_ids=self.acceptance_target_scope_ids,
            owner_review_authorization_ref=(
                self.owner_review_authorization_ref
            ),
        )

    @property
    def review_identity_hash(self) -> str:
        return _canonical_hash(
            {
                "schema_version": self.schema_version,
                "objective": self.objective,
                "repo_path": self.repo_path,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "max_elapsed_seconds": self.max_elapsed_seconds,
                "max_observed_total_tokens": self.max_observed_total_tokens,
                "max_parallel_agents": self.max_parallel_agents,
                "max_total_agents": self.max_total_agents,
                "max_spawn_depth": self.max_spawn_depth,
                "candidate": asdict(self.candidate),
                "context_hash": self.context_hash,
                "skill_bundle_hash": self.skill_bundle_hash,
                "review_mode": self.review_mode,
                "review_scope_id": self.review_scope_id,
                "owner_review_authorization_ref": (
                    self.owner_review_authorization_ref
                ),
                "owner_revision_ref": self.owner_revision_ref,
                "acceptance_target_scope_ids": list(
                    self.acceptance_target_scope_ids
                ),
                "review_scopes": [asdict(item) for item in self.review_scopes],
                "lineage": {
                    "lineage_root_id": self.lineage.lineage_root_id,
                    "max_elapsed_seconds": self.lineage.max_elapsed_seconds,
                    "max_invocations": self.lineage.max_invocations,
                    "max_review_waves": self.lineage.max_review_waves,
                },
            }
        )

    @property
    def contract_hash(self) -> str:
        payload = asdict(self)
        payload.pop("_legacy_lineage_read_only", None)
        return _canonical_hash(payload)

    def validate_material(self) -> dict:
        mismatched = []
        repo = Path(self.repo_path)
        for item in (self.candidate,) + self.context_inputs:
            if not _matches(repo / item.path, item.sha256, root=repo):
                mismatched.append(item.path)
        skill_root = Path(self.reviewer_skill.root)
        for item in self.reviewer_skill.files:
            if not _matches(skill_root / item.path, item.sha256, root=skill_root):
                mismatched.append("reviewer_skill/" + item.path)
        return {
            "status": "matched" if not mismatched else "mismatch",
            "mismatched_files": mismatched,
        }


class ProjectReviewGovernor:
    """Launch one content-bound reviewer without acquiring its semantic authority."""

    def __init__(self, codex_executable: str = "codex", *, state_root: Path):
        self.codex_executable = str(codex_executable)
        self.state_root = Path(state_root).expanduser().resolve()

    def run(
        self, contract: ProjectReviewContract, output_dir: Path
    ) -> Mapping[str, Any]:
        if not isinstance(contract, ProjectReviewContract):
            raise ProjectReviewError("contract must be a ProjectReviewContract")
        if contract._legacy_lineage_read_only:
            raise ProjectReviewError(
                "legacy lineage contracts are recovery-only and cannot launch a model"
            )
        if contract.review_scopes:
            return _run_segmented_project_review(
                self.codex_executable,
                self.state_root,
                contract,
                output_dir,
            )
        material = contract.validate_material()
        if material["status"] != "matched":
            raise ProjectReviewError(
                "review material hash mismatch: "
                + ", ".join(material["mismatched_files"])
            )
        repo = Path(contract.repo_path)
        try:
            _require_clean_git_worktree(repo)
        except Exception as error:
            raise ProjectReviewError(str(error)) from error
        baseline_head = _git_head(repo)
        lineage_path = lineage_ledger_path(
            self.state_root,
            repo,
            contract.lineage.lineage_root_id,
        )
        is_resume = contract.lineage.resume_session_id is not None
        if is_resume:
            workspace = _resume_workspace(contract, output_dir)
        else:
            _preflight_new_workspace(contract, output_dir)
            workspace = None
        try:
            reservation = reserve_lineage(
                contract.lineage,
                ledger_path=lineage_path,
                contract_hash=contract.contract_hash,
                candidate_hash=contract.candidate.sha256,
                requested_elapsed_seconds=contract.max_elapsed_seconds,
                requested_review_waves=0 if is_resume else 1,
                current_scope_id=contract.review_scope_id,
                primary_mode="governance",
                owner_acceptance_ref=None,
                owner_revision_ref=contract.owner_revision_ref,
            )
        except LineageError as error:
            raise ProjectReviewError(str(error)) from error

        if workspace is None:
            try:
                workspace = materialize_review_context(
                    contract,
                    output_dir,
                    review_batch_id=reservation["reservation_id"],
                )
            except Exception as error:
                try:
                    settle_lineage(
                        lineage_path,
                        reservation["reservation_id"],
                        terminal_status="runner_error",
                        model_started=False,
                        actual_elapsed_seconds=0,
                        session_id=None,
                    )
                except LineageError as settlement_error:
                    raise ProjectReviewError(
                        "failed to settle unstarted review reservation"
                    ) from settlement_error
                if isinstance(error, ProjectReviewError):
                    raise
                raise ProjectReviewError(
                    "failed to materialize review workspace"
                ) from error
        command = build_project_review_command(
            contract,
            workspace,
            codex_executable=self.codex_executable,
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            settle_lineage(
                lineage_path,
                reservation["reservation_id"],
                terminal_status="runner_error",
                model_started=False,
                actual_elapsed_seconds=0,
                session_id=None,
            )
            raise ProjectReviewError("failed to start reviewer agent") from error

        supervision_started_at = time.monotonic()
        try:
            supervision = supervise_root_process(
                process,
                raw_events_path=workspace.output_dir / "raw-events.jsonl",
                stderr_path=workspace.output_dir / "stderr.txt",
                changed_paths_probe=lambda: _git_changed_paths(repo, baseline_head),
                allowed_paths=(),
                product_paths=(),
                max_elapsed_seconds=contract.max_elapsed_seconds,
                product_change_deadline_seconds=None,
                max_observed_total_tokens=contract.max_observed_total_tokens,
                token_usage_from_jsonl=_token_usage_from_jsonl,
            )
        except Exception as error:
            _terminate_process_group(process)
            try:
                settle_lineage(
                    lineage_path,
                    reservation["reservation_id"],
                    terminal_status="runner_error",
                    model_started=True,
                    actual_elapsed_seconds=max(
                        0.0, time.monotonic() - supervision_started_at
                    ),
                    session_id=None,
                )
            except LineageError as settlement_error:
                raise ProjectReviewError(
                    "online review supervision failed and settlement failed"
                ) from settlement_error
            raise ProjectReviewError(
                "online review supervision failed: " + str(error)
            ) from error
        raw_events = (workspace.output_dir / "raw-events.jsonl").read_text(
            encoding="utf-8", errors="replace"
        )
        session_id = _session_id_from_jsonl(raw_events)
        token_usage = _token_usage_from_jsonl(raw_events)
        stream_observation = codex_stream_observation(raw_events)
        final_changes = _git_changed_paths(repo, baseline_head)
        context_unchanged = (
            _directory_hash(workspace.context_root)
            == workspace.context_tree_sha256
            and _file_hash(workspace.output_schema_path)
            == workspace.output_schema_sha256
        )
        review = None
        receipt_error = None
        if (
            supervision.stop_reason is None
            and process.returncode == 0
            and not final_changes
            and context_unchanged
        ):
            try:
                review = _load_and_validate_review_receipt(contract, workspace)
            except ProjectReviewError as error:
                receipt_error = str(error)

        if final_changes:
            status = "stopped"
            reason = "reviewer_modified_governed_repository"
        elif not context_unchanged:
            status = "stopped"
            reason = "review_context_changed"
        elif supervision.stop_reason is not None:
            status = "interrupted" if session_id else "runner_error"
            reason = supervision.stop_reason
        elif process.returncode != 0:
            status = "interrupted" if session_id else "runner_error"
            reason = "reviewer_process_failed"
        elif receipt_error is not None:
            status = "stopped"
            reason = "review_receipt_invalid"
        else:
            status = "complete"
            reason = "review_receipt_validated"

        try:
            settlement = settle_lineage(
                lineage_path,
                reservation["reservation_id"],
                terminal_status=status,
                model_started=True,
                actual_elapsed_seconds=supervision.elapsed_seconds,
                session_id=session_id,
            )
        except LineageError as error:
            raise ProjectReviewError(str(error)) from error

        hard_controls = [
            "hash_bound_project_context",
            "external_hash_bound_reviewer_skill",
            "read_only_reviewer_sandbox",
            "governed_repository_nonmutation_probe",
            "one_lineage_review_wave",
            "machine_validated_review_receipt",
        ]
        soft_controls = []
        hard_controls.append("serial_multi_agent_disabled")
        if contract.max_observed_total_tokens is not None:
            if stream_observation["token_observability_mode"] == "streaming":
                hard_controls.append("observed_token_cap")
            elif stream_observation["token_observability_mode"] == "terminal_only":
                soft_controls.append("terminal_token_accounting")
            else:
                soft_controls.append("observed_token_cap_unavailable")

        observed_total = token_usage.get("total_tokens")
        token_limit = contract.max_observed_total_tokens
        token_overrun = (
            token_limit is not None
            and isinstance(observed_total, int)
            and not isinstance(observed_total, bool)
            and observed_total >= token_limit
        )
        if token_limit is None:
            budget_state = "not_configured"
            budget_enforcement = "not_configured"
        elif stream_observation["token_observability_mode"] == "unavailable":
            budget_state = "unavailable"
            budget_enforcement = "unavailable"
        else:
            budget_state = "overrun" if token_overrun else "within_limit"
            budget_enforcement = (
                "live_hard_cap"
                if stream_observation["token_observability_mode"] == "streaming"
                else "terminal_accounting_only"
            )

        receipt = {
            "schema_version": "development-governor.project-review-run-receipt.v0",
            "status": status,
            "reason": reason,
            "contract_hash": contract.contract_hash,
            "review_identity_hash": contract.review_identity_hash,
            "review_batch_id": workspace.review_batch_id,
            "session_id": session_id,
            "token_usage": token_usage,
            "exit_code": process.returncode,
            "elapsed_seconds": supervision.elapsed_seconds,
            "candidate": {
                "path": contract.candidate.path,
                "sha256": contract.candidate.sha256,
            },
            "context_hash": contract.context_hash,
            "skill_bundle_hash": contract.skill_bundle_hash,
            "materialized_context_sha256": workspace.context_tree_sha256,
            "output_schema_sha256": workspace.output_schema_sha256,
            "repository": {
                "path": str(repo),
                "baseline_head": baseline_head,
                "changed_paths": final_changes,
            },
            "review": review,
            "review_receipt_error": receipt_error,
            "artifact_status": {
                "review_receipt_present": workspace.review_receipt_path.is_file(),
                "turn_completed": stream_observation[
                    "completion_event_observed"
                ],
            },
            "review_validation_status": {
                "status": (
                    "valid"
                    if review is not None
                    else "invalid"
                    if receipt_error is not None
                    else "not_validated"
                ),
                "error": receipt_error,
            },
            "budget_status": {
                "status": budget_state,
                "configured_token_limit": token_limit,
                "observed_total_tokens": (
                    observed_total if isinstance(observed_total, int) else None
                ),
                "token_observability_mode": stream_observation[
                    "token_observability_mode"
                ],
                "enforcement": budget_enforcement,
            },
            "lineage": {
                "ledger_path": str(lineage_path),
                "reservation": reservation,
                "settlement": settlement,
                **settlement["projection"],
            },
            "authority_boundary": {
                "governor_semantic_verdict": False,
                "owner_acceptance": "pending",
                "implementation_authorized": False,
            },
            "hard_controls": hard_controls,
            "soft_controls": soft_controls,
        }
        _atomic_json_write(workspace.output_dir / "terminal-receipt.json", receipt)
        return receipt


def derive_project_review_campaign_id(
    *,
    repo_path: Path,
    candidate_sha256: str,
    acceptance_target_scope_ids,
    owner_review_authorization_ref: str,
) -> str:
    """Derive the non-user-selectable budget lineage for one frozen review."""

    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise ProjectReviewError("repo_path must be an existing directory")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProjectReviewError("repo_path must identify a Git worktree") from error
    common_value = result.stdout.strip()
    if not common_value:
        raise ProjectReviewError("Git common dir cannot be empty")
    common = Path(common_value)
    if not common.is_absolute():
        common = repo / common
    common = common.resolve()
    if not common.is_dir():
        raise ProjectReviewError("Git common dir must be an existing directory")
    candidate = _sha256(candidate_sha256, "candidate_sha256")
    targets = sorted(
        _unique_strings(
            acceptance_target_scope_ids,
            "acceptance_target_scope_ids",
        )
    )
    owner_ref = _nonempty(
        owner_review_authorization_ref,
        "owner_review_authorization_ref",
    )
    return _canonical_hash(
        {
            "schema_version": "development-governor.review-campaign-identity.v0",
            "git_common_dir": str(common),
            "candidate_sha256": candidate,
            "acceptance_target_scope_ids": targets,
            "owner_review_authorization_ref": owner_ref,
        }
    )


def _run_segmented_project_review(
    codex_executable: str,
    state_root: Path,
    contract: ProjectReviewContract,
    output_dir: Path,
) -> Mapping[str, Any]:
    material = contract.validate_material()
    if material["status"] != "matched":
        raise ProjectReviewError(
            "review material hash mismatch: "
            + ", ".join(material["mismatched_files"])
        )
    repo = Path(contract.repo_path)
    try:
        _require_clean_git_worktree(repo)
    except Exception as error:
        raise ProjectReviewError(str(error)) from error
    baseline_head = _git_head(repo)
    campaign_dir = _ensure_segment_campaign(contract, output_dir)
    checkpoints = _load_segment_checkpoints(contract, campaign_dir)
    if len(checkpoints) == len(contract.review_scopes):
        aggregate = _aggregate_segment_checkpoints(
            contract, campaign_dir, checkpoints
        )
        return _segmented_run_receipt(
            contract,
            checkpoints,
            aggregate,
            model_invocations_started=0,
            segment_results=(),
            lineage=None,
            reason="all_segment_checkpoints_reused",
        )

    missing = [
        scope for scope in contract.review_scopes if scope.scope_id not in checkpoints
    ]
    requested_elapsed = sum(scope.max_elapsed_seconds for scope in missing)
    lineage_path = lineage_ledger_path(
        state_root, repo, contract.review_campaign_id
    )
    current_lineage = lineage_projection(lineage_path)
    try:
        reservation = reserve_lineage(
            contract.lineage,
            ledger_path=lineage_path,
            contract_hash=contract.contract_hash,
            candidate_hash=contract.candidate.sha256,
            requested_elapsed_seconds=requested_elapsed,
            requested_invocations=len(missing),
            requested_review_waves=(
                0 if current_lineage["review_waves_spent"] > 0 else 1
            ),
            current_scope_id=contract.review_scope_id,
            primary_mode="governance",
            owner_acceptance_ref=None,
            owner_revision_ref=contract.owner_revision_ref,
        )
    except LineageError as error:
        raise ProjectReviewError(str(error)) from error

    attempted = set()
    segment_results = []
    total_elapsed = 0.0
    started_count = 0
    while len(checkpoints) < len(contract.review_scopes):
        ready = [
            scope
            for scope in contract.review_scopes
            if scope.scope_id not in checkpoints
            and scope.scope_id not in attempted
            and all(dependency in checkpoints for dependency in scope.depends_on)
        ]
        if not ready:
            break
        workers = min(contract.max_parallel_agents, len(ready))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_project_review_segment,
                    codex_executable,
                    contract,
                    scope,
                    campaign_dir,
                    reservation["reservation_id"],
                    baseline_head,
                    {
                        dependency: checkpoints[dependency]
                        for dependency in scope.depends_on
                    },
                ): scope
                for scope in ready
            }
            for future in as_completed(futures):
                scope = futures[future]
                attempted.add(scope.scope_id)
                try:
                    result = future.result()
                except Exception as error:
                    result = {
                        "schema_version": (
                            "development-governor.segment-run-receipt.v0"
                        ),
                        "status": "runner_error",
                        "reason": "segment_runner_exception",
                        "segment_id": scope.scope_id,
                        "model_started": False,
                        "elapsed_seconds": 0.0,
                        "error": str(error),
                    }
                segment_results.append(result)
                if result.get("model_started") is True:
                    started_count += 1
                    elapsed = result.get("elapsed_seconds")
                    if isinstance(elapsed, (int, float)) and not isinstance(
                        elapsed, bool
                    ):
                        total_elapsed += max(0.0, float(elapsed))
                if result.get("status") == "complete":
                    checkpoint = _write_segment_checkpoint(
                        contract, scope, campaign_dir, result
                    )
                    checkpoints[scope.scope_id] = checkpoint

    all_complete = len(checkpoints) == len(contract.review_scopes)
    terminal_status = "complete" if all_complete else "stopped"
    try:
        settlement = settle_lineage(
            lineage_path,
            reservation["reservation_id"],
            terminal_status=terminal_status,
            model_started=started_count > 0,
            actual_elapsed_seconds=total_elapsed,
            actual_invocations=started_count,
            session_id=None,
        )
    except LineageError as error:
        raise ProjectReviewError(str(error)) from error

    aggregate = None
    if all_complete:
        aggregate = _aggregate_segment_checkpoints(
            contract, campaign_dir, checkpoints
        )
    reason = (
        "all_segment_checkpoints_validated"
        if all_complete
        else "one_or_more_segments_incomplete"
    )
    receipt = _segmented_run_receipt(
        contract,
        checkpoints,
        aggregate,
        model_invocations_started=started_count,
        segment_results=tuple(segment_results),
        lineage={
            "ledger_path": str(lineage_path),
            "reservation": reservation,
            "settlement": settlement,
        },
        reason=reason,
    )
    attempt_path = (
        campaign_dir / "attempts" / (reservation["reservation_id"] + ".json")
    )
    _atomic_json_write(attempt_path, receipt)
    return receipt


def _ensure_segment_campaign(
    contract: ProjectReviewContract, output_dir: Path
) -> Path:
    destination = Path(output_dir).expanduser().resolve()
    repo = Path(contract.repo_path)
    if destination == repo or repo in destination.parents:
        raise ProjectReviewError("review output_dir must be outside governed repository")
    manifest = {
        "schema_version": "development-governor.segment-campaign-manifest.v0",
        "campaign_id": contract.review_campaign_id,
        "review_identity_hash": contract.review_identity_hash,
        "candidate": asdict(contract.candidate),
        "context_hash": contract.context_hash,
        "skill_bundle_hash": contract.skill_bundle_hash,
        "acceptance_target_scope_ids": list(
            contract.acceptance_target_scope_ids
        ),
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
        "review_mode": contract.review_mode,
        "segments": [_json_normalized(asdict(scope)) for scope in contract.review_scopes],
    }
    manifest_path = destination / "campaign-manifest.json"
    if destination.exists():
        if not destination.is_dir() or not manifest_path.is_file():
            raise ProjectReviewError(
                "segmented review output must contain its campaign manifest"
            )
        existing = _read_json_object(manifest_path, "segment campaign manifest")
        if existing != manifest:
            raise ProjectReviewError("segment campaign manifest mismatch")
    else:
        destination.mkdir(parents=True)
        _atomic_json_write(manifest_path, manifest)
    (destination / "checkpoints").mkdir(exist_ok=True)
    (destination / "attempts").mkdir(exist_ok=True)
    return destination


def _run_project_review_segment(
    codex_executable: str,
    contract: ProjectReviewContract,
    scope: ReviewScope,
    campaign_dir: Path,
    attempt_id: str,
    baseline_head: str,
    dependency_checkpoints: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    workspace = _materialize_segment_workspace(
        contract,
        scope,
        campaign_dir,
        attempt_id,
        dependency_checkpoints,
    )
    command = _build_segment_review_command(
        contract,
        scope,
        workspace,
        codex_executable=codex_executable,
    )
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=Path(contract.repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        receipt = {
            "schema_version": "development-governor.segment-run-receipt.v0",
            "status": "runner_error",
            "reason": "segment_process_start_failed",
            "campaign_id": contract.review_campaign_id,
            "segment_id": scope.scope_id,
            "attempt_id": attempt_id,
            "model_started": False,
            "elapsed_seconds": 0.0,
            "error": str(error),
        }
        _atomic_json_write(workspace.output_dir / "terminal-receipt.json", receipt)
        return receipt
    try:
        supervision = supervise_root_process(
            process,
            raw_events_path=workspace.output_dir / "raw-events.jsonl",
            stderr_path=workspace.output_dir / "stderr.txt",
            changed_paths_probe=lambda: _git_changed_paths(
                Path(contract.repo_path), baseline_head
            ),
            allowed_paths=(),
            product_paths=(),
            max_elapsed_seconds=scope.max_elapsed_seconds,
            product_change_deadline_seconds=None,
            max_observed_total_tokens=scope.max_observed_total_tokens,
            token_usage_from_jsonl=_token_usage_from_jsonl,
        )
    except Exception as error:
        _terminate_process_group(process)
        receipt = {
            "schema_version": "development-governor.segment-run-receipt.v0",
            "status": "runner_error",
            "reason": "segment_supervision_failed",
            "campaign_id": contract.review_campaign_id,
            "segment_id": scope.scope_id,
            "attempt_id": attempt_id,
            "model_started": True,
            "elapsed_seconds": max(0.0, time.monotonic() - started_at),
            "error": str(error),
        }
        _atomic_json_write(workspace.output_dir / "terminal-receipt.json", receipt)
        return receipt

    raw_events_path = workspace.output_dir / "raw-events.jsonl"
    raw_events = raw_events_path.read_text(encoding="utf-8", errors="replace")
    token_usage = _token_usage_from_jsonl(raw_events)
    stream_observation = codex_stream_observation(raw_events)
    final_changes = _git_changed_paths(Path(contract.repo_path), baseline_head)
    context_unchanged = (
        _directory_hash(workspace.context_root) == workspace.context_tree_sha256
        and _file_hash(workspace.output_schema_path)
        == workspace.output_schema_sha256
    )
    review = None
    review_error = None
    dependency_hashes = {
        dependency: _file_hash(
            campaign_dir / "checkpoints" / (dependency + ".json")
        )
        for dependency in scope.depends_on
    }
    if (
        supervision.stop_reason is None
        and process.returncode == 0
        and not final_changes
        and context_unchanged
    ):
        try:
            review = _load_and_validate_segment_review(
                contract,
                scope,
                workspace.review_receipt_path,
                dependency_hashes,
            )
        except ProjectReviewError as error:
            review_error = str(error)
    if final_changes:
        status, reason = "stopped", "reviewer_modified_governed_repository"
    elif not context_unchanged:
        status, reason = "stopped", "review_context_changed"
    elif supervision.stop_reason is not None:
        status, reason = "interrupted", supervision.stop_reason
    elif process.returncode != 0:
        status, reason = "interrupted", "reviewer_process_failed"
    elif review_error is not None:
        status, reason = "stopped", "segment_review_receipt_invalid"
    else:
        status, reason = "complete", "segment_review_receipt_validated"
    receipt = {
        "schema_version": "development-governor.segment-run-receipt.v0",
        "status": status,
        "reason": reason,
        "campaign_id": contract.review_campaign_id,
        "review_identity_hash": contract.review_identity_hash,
        "segment_definition_hash": _canonical_hash(asdict(scope)),
        "segment_id": scope.scope_id,
        "acceptance_id": scope.acceptance_id,
        "attempt_id": attempt_id,
        "model_started": True,
        "elapsed_seconds": supervision.elapsed_seconds,
        "exit_code": process.returncode,
        "session_id": _session_id_from_jsonl(raw_events),
        "repository_changed_paths": list(final_changes),
        "context_tree_sha256": workspace.context_tree_sha256,
        "output_schema_sha256": workspace.output_schema_sha256,
        "raw_events_sha256": _file_hash(raw_events_path),
        "review_receipt_sha256": (
            _file_hash(workspace.review_receipt_path)
            if workspace.review_receipt_path.is_file()
            else None
        ),
        "token_usage": token_usage,
        "token_observability_mode": stream_observation[
            "token_observability_mode"
        ],
        "token_budget_exceeded": supervision.token_budget_exceeded,
        "review_receipt_error": review_error,
        "review": review,
    }
    _atomic_json_write(workspace.output_dir / "terminal-receipt.json", receipt)
    return receipt


def _materialize_segment_workspace(
    contract: ProjectReviewContract,
    scope: ReviewScope,
    campaign_dir: Path,
    attempt_id: str,
    dependency_checkpoints: Mapping[str, Mapping[str, Any]],
) -> SegmentWorkspace:
    attempt = _sha256(attempt_id, "segment attempt_id")
    destination = (
        campaign_dir
        / "segments"
        / scope.scope_id
        / "attempts"
        / attempt
    )
    if destination.exists():
        raise ProjectReviewError("segment attempt output already exists")
    context_root = destination / "review-context"
    context_root.mkdir(parents=True)
    project_root = context_root / "project"
    repo = Path(contract.repo_path)
    _copy_bound_file(
        repo / contract.candidate.path,
        project_root / contract.candidate.path,
    )
    context_by_path = {item.path: item for item in contract.context_inputs}
    selected_context = [context_by_path[path] for path in scope.context_paths]
    for item in selected_context:
        _copy_bound_file(repo / item.path, project_root / item.path)
    reviewer_root = context_root / "reviewer"
    skill_root = Path(contract.reviewer_skill.root)
    for item in contract.reviewer_skill.files:
        _copy_bound_file(skill_root / item.path, reviewer_root / item.path)
    dependency_hashes = {}
    for dependency in scope.depends_on:
        source = campaign_dir / "checkpoints" / (dependency + ".json")
        expected = dependency_checkpoints.get(dependency)
        if expected is None or not source.is_file():
            raise ProjectReviewError("segment dependency checkpoint is missing")
        dependency_hashes[dependency] = _file_hash(source)
        _copy_bound_file(
            source,
            context_root / "dependencies" / (dependency + ".json"),
        )
    manifest = {
        "schema_version": "development-governor.segment-manifest.v0",
        "campaign_id": contract.review_campaign_id,
        "review_identity_hash": contract.review_identity_hash,
        "candidate": asdict(contract.candidate),
        "context_inputs": [asdict(item) for item in selected_context],
        "segment": _json_normalized(asdict(scope)),
        "dependency_checkpoint_sha256s": dependency_hashes,
        "acceptance_target_scope_ids": list(
            contract.acceptance_target_scope_ids
        ),
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
    }
    manifest_path = context_root / "SEGMENT-MANIFEST.json"
    _write_json(manifest_path, manifest)
    output_schema_path = destination / "segment-review-receipt.schema.json"
    _write_json(
        output_schema_path,
        _segment_review_receipt_schema(
            contract, scope, dependency_hashes=dependency_hashes
        ),
    )
    context_hash = _directory_hash(context_root)
    schema_hash = _file_hash(output_schema_path)
    _write_json(
        destination / "frozen-segment-material.json",
        {
            "schema_version": "development-governor.frozen-segment-material.v0",
            "campaign_id": contract.review_campaign_id,
            "segment_definition_hash": _canonical_hash(asdict(scope)),
            "context_tree_sha256": context_hash,
            "output_schema_sha256": schema_hash,
        },
    )
    return SegmentWorkspace(
        output_dir=destination,
        context_root=context_root,
        manifest_path=manifest_path,
        output_schema_path=output_schema_path,
        review_receipt_path=destination / "segment-review-receipt.json",
        context_tree_sha256=context_hash,
        output_schema_sha256=schema_hash,
    )


def _build_segment_review_command(
    contract: ProjectReviewContract,
    scope: ReviewScope,
    workspace: SegmentWorkspace,
    *,
    codex_executable: str,
) -> Tuple[str, ...]:
    prompt = f"""{scope.objective}

You are one dedicated read-only reviewer for segment {scope.scope_id} of campaign
{contract.review_campaign_id}. Read SEGMENT-MANIFEST.json and reviewer/SKILL.md.
Use only this materialized context. Do not spawn subagents, read conversation history,
edit the governed repository, create Owner authority, or review another segment.
If this is the join segment, treat files under dependencies/ as the only authoritative
upstream segment results and test contradictions across them.

Return only the JSON object required by the supplied schema. The next allowed move
must remain owner_decision.
"""
    return (
        codex_executable,
        "--disable",
        "multi_agent",
        "--strict-config",
        "-c",
        'model_reasoning_effort="' + contract.reasoning_effort + '"',
        "--model",
        contract.model,
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace.context_root),
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        str(workspace.output_schema_path),
        "--output-last-message",
        str(workspace.review_receipt_path),
        prompt,
    )


def _segment_review_receipt_schema(
    contract: ProjectReviewContract,
    scope: ReviewScope,
    *,
    dependency_hashes: Mapping[str, str],
) -> dict:
    finding_properties = {
        "finding_id": {"type": "string"},
        "severity": {
            "type": "string",
            "enum": ["critical", "important", "minor"],
        },
        "title": {"type": "string"},
        "location": {"type": "string"},
        "trigger": {"type": "string"},
        "consequence": {"type": "string"},
        "minimum_repair": {"type": "string"},
    }
    counterexample_properties = {
        "applicable_counterexamples": {"type": "integer"},
        "counterexample_blocked": {"type": "integer"},
        "counterexample_succeeded": {"type": "integer"},
        "not_run": {"type": "integer"},
        "not_applicable": {"type": "integer"},
    }
    dependency_properties = {
        key: {"type": "string", "enum": [value]}
        for key, value in dependency_hashes.items()
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate",
            "campaign_id",
            "segment_id",
            "acceptance_id",
            "dependency_checkpoint_sha256s",
            "owner_review_authorization_ref",
            "counterexample_summary",
            "findings",
            "verdict",
            "next_allowed_move",
            "can_claim",
            "cannot_claim",
        ],
        "properties": {
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "hash"],
                "properties": {
                    "path": {
                        "type": "string",
                        "enum": [contract.candidate.path],
                    },
                    "hash": {
                        "type": "string",
                        "enum": [contract.candidate.sha256],
                    },
                },
            },
            "campaign_id": {
                "type": "string",
                "enum": [contract.review_campaign_id],
            },
            "segment_id": {"type": "string", "enum": [scope.scope_id]},
            "acceptance_id": {
                "type": "string",
                "enum": [scope.acceptance_id],
            },
            "dependency_checkpoint_sha256s": {
                "type": "object",
                "additionalProperties": False,
                "required": list(dependency_properties),
                "properties": dependency_properties,
            },
            "owner_review_authorization_ref": {
                "type": "string",
                "enum": [contract.owner_review_authorization_ref],
            },
            "counterexample_summary": {
                "type": "object",
                "additionalProperties": False,
                "required": list(counterexample_properties),
                "properties": counterexample_properties,
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(finding_properties),
                    "properties": finding_properties,
                },
            },
            "verdict": {
                "type": "string",
                "enum": [
                    "accepted_for_owner_review",
                    "targeted_revision_required",
                    "major_revision_required",
                ],
            },
            "next_allowed_move": {
                "type": "string",
                "enum": ["owner_decision"],
            },
            "can_claim": {"type": "array", "items": {"type": "string"}},
            "cannot_claim": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _load_and_validate_segment_review(
    contract: ProjectReviewContract,
    scope: ReviewScope,
    path: Path,
    dependency_hashes: Mapping[str, str],
) -> dict:
    raw = _read_json_object(path, "segment review receipt")
    required = {
        "candidate",
        "campaign_id",
        "segment_id",
        "acceptance_id",
        "dependency_checkpoint_sha256s",
        "owner_review_authorization_ref",
        "counterexample_summary",
        "findings",
        "verdict",
        "next_allowed_move",
        "can_claim",
        "cannot_claim",
    }
    if set(raw) != required:
        raise ProjectReviewError("segment review receipt has invalid fields")
    exact = {
        "candidate": {
            "path": contract.candidate.path,
            "hash": contract.candidate.sha256,
        },
        "campaign_id": contract.review_campaign_id,
        "segment_id": scope.scope_id,
        "acceptance_id": scope.acceptance_id,
        "dependency_checkpoint_sha256s": dict(dependency_hashes),
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
        "next_allowed_move": "owner_decision",
    }
    for field, expected in exact.items():
        if raw[field] != expected:
            raise ProjectReviewError("segment review receipt " + field + " mismatch")
    if raw["verdict"] not in {
        "accepted_for_owner_review",
        "targeted_revision_required",
        "major_revision_required",
    }:
        raise ProjectReviewError("segment review verdict is unsupported")
    counterexample_fields = {
        "applicable_counterexamples",
        "counterexample_blocked",
        "counterexample_succeeded",
        "not_run",
        "not_applicable",
    }
    summary = raw["counterexample_summary"]
    if (
        not isinstance(summary, dict)
        or set(summary) != counterexample_fields
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in summary.values()
        )
    ):
        raise ProjectReviewError("segment counterexample summary is invalid")
    finding_fields = {
        "finding_id",
        "severity",
        "title",
        "location",
        "trigger",
        "consequence",
        "minimum_repair",
    }
    findings = raw["findings"]
    if not isinstance(findings, list):
        raise ProjectReviewError("segment findings must be an array")
    finding_ids = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != finding_fields
            or finding.get("severity") not in {"critical", "important", "minor"}
            or any(
                not isinstance(value, str) or not value
                for value in finding.values()
            )
        ):
            raise ProjectReviewError("segment finding is invalid")
        finding_ids.append(finding["finding_id"])
    if len(set(finding_ids)) != len(finding_ids):
        raise ProjectReviewError("segment finding IDs must be unique")
    for field in ("can_claim", "cannot_claim"):
        if not isinstance(raw[field], list) or any(
            not isinstance(value, str) or not value for value in raw[field]
        ):
            raise ProjectReviewError(field + " must be a string array")
    return raw


def _write_segment_checkpoint(
    contract: ProjectReviewContract,
    scope: ReviewScope,
    campaign_dir: Path,
    terminal: Mapping[str, Any],
) -> Mapping[str, Any]:
    if terminal.get("status") != "complete" or not isinstance(
        terminal.get("review"), dict
    ):
        raise ProjectReviewError("only complete segment runs can checkpoint")
    attempt_id = _sha256(terminal.get("attempt_id"), "segment attempt_id")
    attempt_dir = (
        campaign_dir / "segments" / scope.scope_id / "attempts" / attempt_id
    )
    terminal_path = attempt_dir / "terminal-receipt.json"
    review_path = attempt_dir / "segment-review-receipt.json"
    checkpoint = {
        "schema_version": "development-governor.segment-checkpoint.v0",
        "status": "complete",
        "campaign_id": contract.review_campaign_id,
        "review_identity_hash": contract.review_identity_hash,
        "segment_definition_hash": _canonical_hash(asdict(scope)),
        "segment_id": scope.scope_id,
        "acceptance_id": scope.acceptance_id,
        "attempt_id": attempt_id,
        "terminal_receipt_sha256": _file_hash(terminal_path),
        "review_receipt_sha256": _file_hash(review_path),
        "context_tree_sha256": terminal["context_tree_sha256"],
        "output_schema_sha256": terminal["output_schema_sha256"],
        "review": terminal["review"],
    }
    path = campaign_dir / "checkpoints" / (scope.scope_id + ".json")
    if path.exists():
        existing = _read_json_object(path, "segment checkpoint")
        if existing != checkpoint:
            raise ProjectReviewError("conflicting segment checkpoint exists")
        return existing
    _atomic_json_write(path, checkpoint)
    return checkpoint


def _load_segment_checkpoints(
    contract: ProjectReviewContract, campaign_dir: Path
) -> dict:
    loaded = {}
    ordered = sorted(
        contract.review_scopes, key=lambda scope: 1 if scope.kind == "join" else 0
    )
    for scope in ordered:
        path = campaign_dir / "checkpoints" / (scope.scope_id + ".json")
        if not path.exists():
            continue
        checkpoint = _read_json_object(path, "segment checkpoint")
        expected_fields = {
            "schema_version",
            "status",
            "campaign_id",
            "review_identity_hash",
            "segment_definition_hash",
            "segment_id",
            "acceptance_id",
            "attempt_id",
            "terminal_receipt_sha256",
            "review_receipt_sha256",
            "context_tree_sha256",
            "output_schema_sha256",
            "review",
        }
        if set(checkpoint) != expected_fields:
            raise ProjectReviewError("segment checkpoint has invalid fields")
        exact = {
            "schema_version": "development-governor.segment-checkpoint.v0",
            "status": "complete",
            "campaign_id": contract.review_campaign_id,
            "review_identity_hash": contract.review_identity_hash,
            "segment_definition_hash": _canonical_hash(asdict(scope)),
            "segment_id": scope.scope_id,
            "acceptance_id": scope.acceptance_id,
        }
        if any(checkpoint.get(key) != value for key, value in exact.items()):
            raise ProjectReviewError("segment checkpoint identity mismatch")
        attempt_id = _sha256(checkpoint.get("attempt_id"), "segment attempt_id")
        attempt_dir = (
            campaign_dir / "segments" / scope.scope_id / "attempts" / attempt_id
        )
        terminal_path = attempt_dir / "terminal-receipt.json"
        review_path = attempt_dir / "segment-review-receipt.json"
        schema_path = attempt_dir / "segment-review-receipt.schema.json"
        context_root = attempt_dir / "review-context"
        if (
            not terminal_path.is_file()
            or not review_path.is_file()
            or not schema_path.is_file()
            or not context_root.is_dir()
            or checkpoint["terminal_receipt_sha256"] != _file_hash(terminal_path)
            or checkpoint["review_receipt_sha256"] != _file_hash(review_path)
            or checkpoint["context_tree_sha256"] != _directory_hash(context_root)
            or checkpoint["output_schema_sha256"] != _file_hash(schema_path)
        ):
            raise ProjectReviewError("segment checkpoint artifact mismatch")
        dependency_hashes = {
            dependency: _file_hash(
                campaign_dir / "checkpoints" / (dependency + ".json")
            )
            for dependency in scope.depends_on
            if dependency in loaded
        }
        if set(dependency_hashes) != set(scope.depends_on):
            raise ProjectReviewError("segment checkpoint dependency is missing")
        review = _load_and_validate_segment_review(
            contract, scope, review_path, dependency_hashes
        )
        terminal = _read_json_object(terminal_path, "segment terminal receipt")
        if terminal.get("status") != "complete" or terminal.get("review") != review:
            raise ProjectReviewError("segment checkpoint terminal mismatch")
        if checkpoint["review"] != review:
            raise ProjectReviewError("segment checkpoint review mismatch")
        loaded[scope.scope_id] = checkpoint
    return loaded


def _aggregate_segment_checkpoints(
    contract: ProjectReviewContract,
    campaign_dir: Path,
    checkpoints: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    if set(checkpoints) != {scope.scope_id for scope in contract.review_scopes}:
        raise ProjectReviewError("cannot aggregate incomplete segment checkpoints")
    precedence = {
        "accepted_for_owner_review": 0,
        "targeted_revision_required": 1,
        "major_revision_required": 2,
    }
    verdict = "accepted_for_owner_review"
    findings = []
    summary = {
        "applicable_counterexamples": 0,
        "counterexample_blocked": 0,
        "counterexample_succeeded": 0,
        "not_run": 0,
        "not_applicable": 0,
    }
    segments = []
    cannot_claim = []
    for scope in contract.review_scopes:
        checkpoint = checkpoints[scope.scope_id]
        review = checkpoint["review"]
        if precedence[review["verdict"]] > precedence[verdict]:
            verdict = review["verdict"]
        for key, value in review["counterexample_summary"].items():
            summary[key] += value
        for finding in review["findings"]:
            findings.append(dict(finding, segment_id=scope.scope_id))
        for claim in review["cannot_claim"]:
            if claim not in cannot_claim:
                cannot_claim.append(claim)
        segments.append(
            {
                "kind": scope.kind,
                "scope_id": scope.scope_id,
                "acceptance_id": scope.acceptance_id,
                "checkpoint_sha256": _file_hash(
                    campaign_dir / "checkpoints" / (scope.scope_id + ".json")
                ),
                "verdict": review["verdict"],
                "finding_ids": [
                    finding["finding_id"] for finding in review["findings"]
                ],
            }
        )
    if "Owner acceptance" not in cannot_claim:
        cannot_claim.append("Owner acceptance")
    aggregate = {
        "schema_version": "development-governor.segmented-review-aggregate.v0",
        "candidate": {
            "path": contract.candidate.path,
            "hash": contract.candidate.sha256,
        },
        "campaign_id": contract.review_campaign_id,
        "acceptance_target_scope_ids": list(
            contract.acceptance_target_scope_ids
        ),
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
        "counterexample_summary": summary,
        "findings": findings,
        "segments": segments,
        "verdict": verdict,
        "next_allowed_move": "owner_decision",
        "can_claim": [
            "all declared review segments completed",
            "deterministic aggregation completed",
        ],
        "cannot_claim": cannot_claim,
    }
    path = campaign_dir / "review-aggregate.json"
    if path.exists():
        existing = _read_json_object(path, "segmented review aggregate")
        if existing != aggregate:
            raise ProjectReviewError("conflicting segmented review aggregate exists")
        return existing
    _atomic_json_write(path, aggregate)
    return aggregate


def _segmented_run_receipt(
    contract: ProjectReviewContract,
    checkpoints: Mapping[str, Mapping[str, Any]],
    aggregate: Optional[Mapping[str, Any]],
    *,
    model_invocations_started: int,
    segment_results,
    lineage: Optional[Mapping[str, Any]],
    reason: str,
) -> Mapping[str, Any]:
    return {
        "schema_version": "development-governor.segmented-review-run-receipt.v0",
        "status": "complete" if aggregate is not None else "incomplete",
        "reason": reason,
        "campaign_id": contract.review_campaign_id,
        "review_identity_hash": contract.review_identity_hash,
        "candidate": {
            "path": contract.candidate.path,
            "sha256": contract.candidate.sha256,
        },
        "model_invocations_started": model_invocations_started,
        "checkpoint_count": len(checkpoints),
        "checkpoint_sha256s": {
            scope_id: _canonical_hash(checkpoint)
            for scope_id, checkpoint in sorted(checkpoints.items())
        },
        "segment_results": list(segment_results),
        "review": aggregate,
        "lineage": lineage,
        "hard_controls": [
            "deterministic_review_campaign_identity",
            "controller_managed_segment_processes",
            "hash_bound_segment_context",
            "append_only_segment_checkpoints",
            "governed_repository_nonmutation_probe",
            "deterministic_nonsemantic_aggregation",
        ],
        "soft_controls": [
            "semantic_findings_owned_by_reviewer_agents",
            "owner_reference_preserved_not_authenticated",
        ],
        "authority_boundary": {
            "owner_acceptance": False,
            "implementation_authorized": False,
            "publication_authorized": False,
        },
    }


def recover_project_review_receipt(
    contract: ProjectReviewContract, output_dir: Path
) -> Mapping[str, Any]:
    """Validate and append a recovery receipt for the terminal-usage race."""

    if not isinstance(contract, ProjectReviewContract):
        raise ProjectReviewError("contract must be a ProjectReviewContract")
    workspace = _resume_workspace(contract, output_dir)
    terminal_path = workspace.output_dir / "terminal-receipt.json"
    raw_events_path = workspace.output_dir / "raw-events.jsonl"
    if not terminal_path.is_file() or not raw_events_path.is_file():
        raise ProjectReviewError(
            "recovery requires the original terminal receipt and raw events"
        )
    terminal = _read_json_object(terminal_path, "terminal receipt")
    if (
        terminal.get("schema_version")
        != "development-governor.project-review-run-receipt.v0"
        or terminal.get("status") != "interrupted"
        or terminal.get("reason") != "observed_token_budget_exhausted"
        or terminal.get("review") is not None
    ):
        raise ProjectReviewError(
            "recovery requires an interrupted terminal-budget review without a verdict"
        )
    exact_terminal_fields = {
        "contract_hash": contract.contract_hash,
        "review_identity_hash": contract.review_identity_hash,
        "review_batch_id": workspace.review_batch_id,
        "context_hash": contract.context_hash,
        "skill_bundle_hash": contract.skill_bundle_hash,
        "materialized_context_sha256": workspace.context_tree_sha256,
        "output_schema_sha256": workspace.output_schema_sha256,
    }
    for field, expected in exact_terminal_fields.items():
        if terminal.get(field) != expected:
            raise ProjectReviewError("recovery terminal " + field + " mismatch")
    if terminal.get("candidate") != {
        "path": contract.candidate.path,
        "sha256": contract.candidate.sha256,
    }:
        raise ProjectReviewError("recovery terminal candidate mismatch")
    repository = terminal.get("repository")
    if (
        not isinstance(repository, dict)
        or Path(str(repository.get("path"))).expanduser().resolve()
        != Path(contract.repo_path)
        or repository.get("changed_paths") != []
    ):
        raise ProjectReviewError(
            "recovery requires bound governed-repository nonmutation evidence"
        )
    session_id = terminal.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProjectReviewError("recovery terminal session_id is missing")
    lineage = terminal.get("lineage")
    if not isinstance(lineage, dict):
        raise ProjectReviewError("recovery terminal lineage is missing")
    ledger_path_raw = lineage.get("ledger_path")
    settlement = lineage.get("settlement")
    if (
        not isinstance(ledger_path_raw, str)
        or not isinstance(settlement, dict)
        or settlement.get("terminal_status") != "interrupted"
        or settlement.get("ledger_sha256_after")
        != lineage_ledger_sha256(Path(ledger_path_raw))
    ):
        raise ProjectReviewError("recovery lineage settlement mismatch")

    raw_events = raw_events_path.read_text(encoding="utf-8", errors="replace")
    if _session_id_from_jsonl(raw_events) != session_id:
        raise ProjectReviewError("recovery raw-event session mismatch")
    token_usage = _token_usage_from_jsonl(raw_events)
    if terminal.get("token_usage") != token_usage:
        raise ProjectReviewError("recovery raw-event token usage mismatch")
    observation = codex_stream_observation(raw_events)
    if observation != {
        "token_observability_mode": "terminal_only",
        "completion_event_observed": True,
    }:
        raise ProjectReviewError(
            "recovery requires terminal-only usage after a completed turn"
        )
    review = _load_and_validate_review_receipt(contract, workspace)
    if _last_agent_message_json(raw_events) != review:
        raise ProjectReviewError(
            "recovery final agent message does not match review receipt"
        )

    token_limit = contract.max_observed_total_tokens
    observed_total = token_usage.get("total_tokens")
    if (
        token_limit is None
        or not isinstance(observed_total, int)
        or isinstance(observed_total, bool)
        or observed_total < token_limit
    ):
        raise ProjectReviewError("recovery terminal token budget was not exceeded")
    recovery = {
        "schema_version": (
            "development-governor.project-review-recovery-receipt.v0"
        ),
        "status": "recovered",
        "reason": "valid_review_preceded_terminal_budget_observation",
        "contract_hash": contract.contract_hash,
        "review_identity_hash": contract.review_identity_hash,
        "review_batch_id": workspace.review_batch_id,
        "session_id": session_id,
        "original_terminal_receipt_sha256": _file_hash(terminal_path),
        "raw_events_sha256": _file_hash(raw_events_path),
        "review_receipt_sha256": _file_hash(workspace.review_receipt_path),
        "materialized_context_sha256": workspace.context_tree_sha256,
        "output_schema_sha256": workspace.output_schema_sha256,
        "lineage_ledger_sha256": lineage_ledger_sha256(Path(ledger_path_raw)),
        "artifact_status": {
            "review_receipt_present": True,
            "turn_completed": True,
        },
        "review_validation_status": {"status": "valid", "error": None},
        "budget_status": {
            "status": "overrun",
            "configured_token_limit": token_limit,
            "observed_total_tokens": observed_total,
            "token_observability_mode": "terminal_only",
            "enforcement": "terminal_accounting_only",
        },
        "review": review,
        "authority_boundary": terminal.get("authority_boundary"),
        "history_mutation": {
            "terminal_receipt_modified": False,
            "lineage_ledger_modified": False,
        },
    }
    recovery_path = workspace.output_dir / "review-recovery-receipt.json"
    if recovery_path.exists():
        existing = _read_json_object(recovery_path, "review recovery receipt")
        if existing != recovery:
            raise ProjectReviewError("conflicting review recovery receipt exists")
        return existing
    _atomic_json_write(recovery_path, recovery)
    return recovery


def materialize_review_context(
    contract: ProjectReviewContract,
    output_dir: Path,
    *,
    review_batch_id: str,
) -> ReviewWorkspace:
    """Copy only hash-bound project and Skill inputs into one review workspace."""

    if not isinstance(contract, ProjectReviewContract):
        raise ProjectReviewError("contract must be a ProjectReviewContract")
    material = contract.validate_material()
    if material["status"] != "matched":
        raise ProjectReviewError(
            "review material hash mismatch: "
            + ", ".join(material["mismatched_files"])
        )
    batch_id = _sha256(review_batch_id, "review_batch_id")
    destination = _preflight_new_workspace(contract, output_dir)
    repo = Path(contract.repo_path)
    context_root = destination / "review-context"
    context_root.mkdir(parents=True)

    project_root = context_root / "project"
    for item in (contract.candidate,) + contract.context_inputs:
        _copy_bound_file(repo / item.path, project_root / item.path)

    reviewer_root = context_root / "reviewer"
    skill_root = Path(contract.reviewer_skill.root)
    for item in contract.reviewer_skill.files:
        _copy_bound_file(skill_root / item.path, reviewer_root / item.path)

    manifest = {
        "schema_version": "development-governor.project-review-manifest.v0",
        "review_identity_hash": contract.review_identity_hash,
        "review_batch_id": batch_id,
        "candidate": asdict(contract.candidate),
        "context_hash": contract.context_hash,
        "context_inputs": [asdict(item) for item in contract.context_inputs],
        "skill_bundle_hash": contract.skill_bundle_hash,
        "reviewer_skill_files": [
            asdict(item) for item in contract.reviewer_skill.files
        ],
        "review_mode": contract.review_mode,
        "review_scope_id": contract.review_scope_id,
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
        "owner_revision_ref": contract.owner_revision_ref,
        "acceptance_target_scope_ids": list(
            contract.acceptance_target_scope_ids
        ),
        "review_scopes": [asdict(item) for item in contract.review_scopes],
    }
    manifest_path = context_root / "REVIEW-MANIFEST.json"
    _write_json(manifest_path, manifest)
    output_schema_path = destination / "review-receipt.schema.json"
    _write_json(
        output_schema_path,
        _review_receipt_schema(contract, review_batch_id=batch_id),
    )
    review_receipt_path = destination / "review-receipt.json"
    context_tree_sha256 = _directory_hash(context_root)
    output_schema_sha256 = _file_hash(output_schema_path)
    _write_json(
        destination / "frozen-review-material.json",
        {
            "schema_version": "development-governor.frozen-review-material.v0",
            "review_identity_hash": contract.review_identity_hash,
            "review_batch_id": batch_id,
            "context_tree_sha256": context_tree_sha256,
            "output_schema_sha256": output_schema_sha256,
        },
    )
    return ReviewWorkspace(
        output_dir=destination,
        context_root=context_root,
        manifest_path=manifest_path,
        output_schema_path=output_schema_path,
        review_receipt_path=review_receipt_path,
        review_batch_id=batch_id,
        context_tree_sha256=context_tree_sha256,
        output_schema_sha256=output_schema_sha256,
    )


def _preflight_new_workspace(
    contract: ProjectReviewContract, output_dir: Path
) -> Path:
    destination = Path(output_dir).expanduser().resolve()
    repo = Path(contract.repo_path)
    if destination == repo or repo in destination.parents:
        raise ProjectReviewError("review output_dir must be outside governed repository")
    if destination.exists():
        raise ProjectReviewError("review output_dir already exists")
    return destination


def build_project_review_command(
    contract: ProjectReviewContract,
    workspace: ReviewWorkspace,
    *,
    codex_executable: str = "codex",
) -> Tuple[str, ...]:
    """Build one isolated Codex command for the frozen review identity."""

    if contract.review_scopes:
        raise ProjectReviewError(
            "segmented review requires controller-managed segment processes"
        )
    command = [
        codex_executable,
        "--disable",
        "multi_agent",
        "--strict-config",
        "-c",
        'model_reasoning_effort="' + contract.reasoning_effort + '"',
        "--model",
        contract.model,
    ]
    if contract.lineage.resume_session_id is None:
        command.extend(
            [
                "--sandbox",
                "read-only",
                "--cd",
                str(workspace.context_root),
                "exec",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
            ]
        )
        command.extend(
            [
                "--json",
                "--output-schema",
                str(workspace.output_schema_path),
                "--output-last-message",
                str(workspace.review_receipt_path),
                _review_prompt(contract, workspace),
            ]
        )
    else:
        command.extend(
            [
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--output-schema",
                str(workspace.output_schema_path),
                "--output-last-message",
                str(workspace.review_receipt_path),
                contract.lineage.resume_session_id,
                _review_prompt(contract, workspace),
            ]
        )
    return tuple(command)


def _load_and_validate_review_receipt(
    contract: ProjectReviewContract, workspace: ReviewWorkspace
) -> dict:
    try:
        raw = json.loads(workspace.review_receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectReviewError("review receipt must be valid JSON") from error
    required = {
        "candidate",
        "batch_id",
        "acceptance_target_scope_ids",
        "owner_review_authorization_ref",
        "review_budget_reservation_ref",
        "review_mode",
        "counterexample_summary",
        "findings",
        "independent_scopes",
        "verdict",
        "next_allowed_move",
        "can_claim",
        "cannot_claim",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ProjectReviewError("review receipt has invalid top-level fields")
    if raw["candidate"] != {
        "path": contract.candidate.path,
        "hash": contract.candidate.sha256,
    }:
        raise ProjectReviewError("review receipt candidate identity mismatch")
    exact_fields = {
        "batch_id": workspace.review_batch_id,
        "acceptance_target_scope_ids": list(
            contract.acceptance_target_scope_ids
        ),
        "owner_review_authorization_ref": (
            contract.owner_review_authorization_ref
        ),
        "review_budget_reservation_ref": workspace.review_batch_id,
        "review_mode": contract.review_mode,
        "next_allowed_move": "owner_decision",
    }
    for field, expected in exact_fields.items():
        if raw[field] != expected:
            raise ProjectReviewError("review receipt " + field + " mismatch")
    verdicts = {
        "accepted_for_owner_review",
        "targeted_revision_required",
        "major_revision_required",
        "blocked_independent_review",
    }
    if raw["verdict"] not in verdicts:
        raise ProjectReviewError("review receipt verdict is unsupported")
    if not isinstance(raw["counterexample_summary"], dict):
        raise ProjectReviewError("counterexample_summary must be an object")
    if not isinstance(raw["findings"], list) or any(
        not isinstance(item, dict) for item in raw["findings"]
    ):
        raise ProjectReviewError("findings must be an object array")
    if not isinstance(raw["independent_scopes"], list) or any(
        not isinstance(item, dict) for item in raw["independent_scopes"]
    ):
        raise ProjectReviewError("independent_scopes must be an object array")
    for field in ("can_claim", "cannot_claim"):
        if not isinstance(raw[field], list) or any(
            not isinstance(item, str) or not item for item in raw[field]
        ):
            raise ProjectReviewError(field + " must be a string array")
    if not contract.review_scopes and raw["independent_scopes"]:
        raise ProjectReviewError(
            "serial review receipt cannot invent independent scopes"
        )
    if contract.review_scopes:
        expected_scopes = {
            (item.scope_id, item.acceptance_id) for item in contract.review_scopes
        }
        observed_scopes = set()
        statuses = []
        for item in raw["independent_scopes"]:
            if set(item) != {"scope_id", "acceptance_id", "status", "findings"}:
                raise ProjectReviewError(
                    "declared review scopes require closed scope receipts"
                )
            pair = (item["scope_id"], item["acceptance_id"])
            status = item["status"]
            if (
                pair not in expected_scopes
                or pair in observed_scopes
                or status
                not in {"complete", "skipped_due_known_blocker", "failed"}
                or not isinstance(item["findings"], list)
            ):
                raise ProjectReviewError(
                    "declared review scopes do not match receipt scopes"
                )
            observed_scopes.add(pair)
            statuses.append(status)
        if observed_scopes != expected_scopes:
            raise ProjectReviewError(
                "declared review scopes are incomplete in receipt"
            )
        if raw["verdict"] in {
            "accepted_for_owner_review",
            "targeted_revision_required",
        } and any(status != "complete" for status in statuses):
            raise ProjectReviewError(
                "declared review scopes must complete for this verdict"
            )
        if raw["verdict"] == "blocked_independent_review" and all(
            status == "complete" for status in statuses
        ):
            raise ProjectReviewError(
                "blocked independent review requires an incomplete scope"
            )
    return raw


def _read_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectReviewError(label + " must be valid JSON") from error
    if not isinstance(value, dict):
        raise ProjectReviewError(label + " must be an object")
    return value


def _last_agent_message_json(raw_events: str) -> dict:
    found = None
    for line in raw_events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            found = candidate
    if found is None:
        raise ProjectReviewError("recovery raw events lack a final JSON agent message")
    return found


def _resume_workspace(
    contract: ProjectReviewContract, output_dir: Path
) -> ReviewWorkspace:
    destination = Path(output_dir).expanduser().resolve()
    context_root = destination / "review-context"
    manifest_path = context_root / "REVIEW-MANIFEST.json"
    output_schema_path = destination / "review-receipt.schema.json"
    frozen_path = destination / "frozen-review-material.json"
    if (
        not manifest_path.is_file()
        or not output_schema_path.is_file()
        or not frozen_path.is_file()
    ):
        raise ProjectReviewError("resume requires the original review workspace")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectReviewError("resume review manifest is invalid") from error
    if manifest.get("review_identity_hash") != contract.review_identity_hash:
        raise ProjectReviewError("resume review identity mismatch")
    batch_id = _sha256(manifest.get("review_batch_id"), "review_batch_id")
    try:
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectReviewError("resume frozen review material is invalid") from error
    context_tree_sha256 = _sha256(
        frozen.get("context_tree_sha256"), "context_tree_sha256"
    )
    output_schema_sha256 = _sha256(
        frozen.get("output_schema_sha256"), "output_schema_sha256"
    )
    if (
        frozen.get("review_identity_hash") != contract.review_identity_hash
        or frozen.get("review_batch_id") != batch_id
        or _directory_hash(context_root) != context_tree_sha256
        or _file_hash(output_schema_path) != output_schema_sha256
    ):
        raise ProjectReviewError("resume frozen review material mismatch")
    return ReviewWorkspace(
        output_dir=destination,
        context_root=context_root,
        manifest_path=manifest_path,
        output_schema_path=output_schema_path,
        review_receipt_path=destination / "review-receipt.json",
        review_batch_id=batch_id,
        context_tree_sha256=context_tree_sha256,
        output_schema_sha256=output_schema_sha256,
    )


def _review_prompt(
    contract: ProjectReviewContract, workspace: ReviewWorkspace
) -> str:
    return f"""{contract.objective}

You are the dedicated project-aware Spec reviewer for one frozen review batch.
The Development Governor controls execution and receipt identity; you own only the
semantic review. Read REVIEW-MANIFEST.json, reviewer/SKILL.md, the referenced gate
catalog and receipt template completely before reviewing
project/{contract.candidate.path}.

Frozen review identity:
- review_identity_hash: {contract.review_identity_hash}
- review_budget_reservation_ref: {workspace.review_batch_id}
- owner_review_authorization_ref: {contract.owner_review_authorization_ref}
- acceptance_target_scope_ids: {json.dumps(list(contract.acceptance_target_scope_ids))}
- review_mode: {contract.review_mode}
- maximum active logical agents: {contract.max_parallel_agents}
- maximum total logical agents: {contract.max_total_agents}
- maximum spawn depth: {contract.max_spawn_depth}

Use only files listed in REVIEW-MANIFEST.json. Do not read files outside this
materialized context and do not use remembered conversation history. Do not edit the
candidate, create authority, authorize implementation, or start another review.
Do not spawn subagents; perform the one direct review yourself.

Return only the single JSON object required by the supplied output schema. Findings
and the verdict are your review evidence, not a Governor decision. The next allowed
move must remain owner_decision.
"""


def _review_receipt_schema(
    contract: ProjectReviewContract, *, review_batch_id: str
) -> dict:
    verdicts = [
        "accepted_for_owner_review",
        "targeted_revision_required",
        "major_revision_required",
        "blocked_independent_review",
    ]
    finding_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "finding_id",
            "severity",
            "title",
            "location",
            "trigger",
            "consequence",
            "minimum_repair",
        ],
        "properties": {
            "finding_id": {"type": "string"},
            "severity": {
                "type": "string",
                "enum": ["critical", "important", "minor"],
            },
            "title": {"type": "string"},
            "location": {"type": "string"},
            "trigger": {"type": "string"},
            "consequence": {"type": "string"},
            "minimum_repair": {"type": "string"},
        },
    }
    counterexample_properties = {
        "applicable_counterexamples": {"type": "integer"},
        "counterexample_blocked": {"type": "integer"},
        "counterexample_succeeded": {"type": "integer"},
        "not_run": {"type": "integer"},
        "not_applicable": {"type": "integer"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate",
            "batch_id",
            "acceptance_target_scope_ids",
            "owner_review_authorization_ref",
            "review_budget_reservation_ref",
            "review_mode",
            "counterexample_summary",
            "findings",
            "independent_scopes",
            "verdict",
            "next_allowed_move",
            "can_claim",
            "cannot_claim",
        ],
        "properties": {
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "hash"],
                "properties": {
                    "path": {
                        "type": "string",
                        "enum": [contract.candidate.path],
                    },
                    "hash": {
                        "type": "string",
                        "enum": [contract.candidate.sha256],
                    },
                },
            },
            "batch_id": {"type": "string", "enum": [review_batch_id]},
            "acceptance_target_scope_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": len(contract.acceptance_target_scope_ids),
                "maxItems": len(contract.acceptance_target_scope_ids),
            },
            "owner_review_authorization_ref": {
                "type": "string",
                "enum": [contract.owner_review_authorization_ref],
            },
            "review_budget_reservation_ref": {
                "type": "string",
                "enum": [review_batch_id],
            },
            "review_mode": {
                "type": "string",
                "enum": [contract.review_mode],
            },
            "counterexample_summary": {
                "type": "object",
                "additionalProperties": False,
                "required": list(counterexample_properties),
                "properties": counterexample_properties,
            },
            "findings": {"type": "array", "items": finding_schema},
            "independent_scopes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scope_id",
                        "acceptance_id",
                        "status",
                        "findings",
                    ],
                    "properties": {
                        "scope_id": {"type": "string"},
                        "acceptance_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "complete",
                                "skipped_due_known_blocker",
                                "failed",
                            ],
                        },
                        "findings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "verdict": {"type": "string", "enum": verdicts},
            "next_allowed_move": {
                "type": "string",
                "enum": ["owner_decision"],
            },
            "can_claim": {"type": "array", "items": {"type": "string"}},
            "cannot_claim": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _copy_bound_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o444)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _directory_hash(root: Path) -> str:
    root = Path(root).resolve()
    entries = []
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            entries.append(
                {"path": relative, "kind": "symlink", "target": os.readlink(item)}
            )
        elif item.is_dir():
            entries.append({"path": relative + "/", "kind": "directory"})
        elif item.is_file():
            entries.append(
                {"path": relative, "kind": "file", "sha256": _file_hash(item)}
            )
        else:
            entries.append({"path": relative, "kind": "special"})
    return _canonical_hash(entries)


def _context_files(raw: Any, repo: Path) -> Tuple[ReviewFile, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ProjectReviewError("context_inputs must be a non-empty array")
    result = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"role", "path", "sha256"}:
            raise ProjectReviewError(
                "context input requires role, path, and sha256"
            )
        role = _nonempty(item["role"], "context input role")
        if role not in _CONTEXT_ROLES:
            raise ProjectReviewError("unsupported context input role: " + role)
        result.append(_project_file(item, repo, role=role))
    roles = {item.role for item in result}
    for required_role in ("project_goal", "parent_baseline"):
        if required_role not in roles:
            raise ProjectReviewError(
                "context_inputs require role " + required_role
            )
    return tuple(result)


def _project_file(raw: Any, repo: Path, role: Optional[str]) -> ReviewFile:
    expected = {"path", "sha256"} if role is None else {"role", "path", "sha256"}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ProjectReviewError("review file has invalid fields")
    path = _relative_path(raw["path"], "path")
    digest = _sha256(raw["sha256"], "sha256")
    if not _matches(repo / path, digest, root=repo):
        raise ProjectReviewError("review file hash mismatch: " + path)
    return ReviewFile(path=path, sha256=digest, role=role)


def _reviewer_skill(raw: Any, repo: Path) -> ReviewerSkill:
    if not isinstance(raw, Mapping) or set(raw) != {"root", "files"}:
        raise ProjectReviewError("reviewer_skill requires root and files")
    root = Path(_nonempty(raw["root"], "reviewer_skill.root")).expanduser().resolve()
    if not root.is_dir():
        raise ProjectReviewError("reviewer_skill.root must be an existing directory")
    if root == repo or repo in root.parents or root in repo.parents:
        raise ProjectReviewError(
            "reviewer skill root must be disjoint from governed repository"
        )
    files_raw = raw["files"]
    if not isinstance(files_raw, (list, tuple)) or not files_raw:
        raise ProjectReviewError("reviewer_skill.files must be a non-empty array")
    files = []
    for item in files_raw:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            raise ProjectReviewError("reviewer skill file requires path and sha256")
        path = _relative_path(item["path"], "reviewer skill path")
        digest = _sha256(item["sha256"], "reviewer skill sha256")
        if not _matches(root / path, digest, root=root):
            raise ProjectReviewError("reviewer skill hash mismatch: " + path)
        files.append(ReviewFile(path=path, sha256=digest))
    file_paths = {item.path for item in files}
    required_files = {
        "SKILL.md",
        "references/gate-catalog.md",
        "templates/spec-review-receipt.md",
    }
    if len(file_paths) != len(files) or not required_files.issubset(file_paths):
        raise ProjectReviewError(
            "reviewer_skill.files must include unique required reviewer skill files"
        )
    return ReviewerSkill(root=str(root), files=tuple(files))


def _review_scopes(
    raw: Any, *, context_inputs: Tuple[ReviewFile, ...]
) -> Tuple[ReviewScope, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ProjectReviewError("review_scopes must be an array")
    result = []
    for item in raw:
        required = {
            "kind",
            "scope_id",
            "objective",
            "acceptance_id",
            "context_paths",
            "depends_on",
            "max_elapsed_seconds",
            "max_observed_total_tokens",
        }
        if not isinstance(item, Mapping) or set(item) != required:
            raise ProjectReviewError(
                "review scope requires kind, identity, context, dependencies, and budgets"
            )
        kind = _nonempty(item["kind"], "review scope kind")
        if kind not in {"independent", "join"}:
            raise ProjectReviewError(
                "review scope kind must be independent or join"
            )
        context_paths = _unique_strings(
            item["context_paths"], "review scope context_paths"
        )
        known_context = {context.path: context for context in context_inputs}
        if not set(context_paths).issubset(known_context):
            raise ProjectReviewError(
                "review scope context_paths must reference bound context inputs"
            )
        selected_roles = {
            known_context[path].role for path in context_paths
        }
        if not {"project_goal", "parent_baseline"}.issubset(selected_roles):
            raise ProjectReviewError(
                "every review scope requires project_goal and parent_baseline context"
            )
        scope_id = _closed_identifier(
            item["scope_id"], "review scope identifier"
        )
        dependencies = tuple(
            _closed_identifier(value, "review dependency identifier")
            for value in _unique_strings(
                item["depends_on"],
                "review scope depends_on",
                allow_empty=True,
            )
        )
        result.append(
            ReviewScope(
                kind=kind,
                scope_id=scope_id,
                objective=_nonempty(item["objective"], "review objective"),
                acceptance_id=_nonempty(
                    item["acceptance_id"], "review acceptance_id"
                ),
                context_paths=context_paths,
                depends_on=dependencies,
                max_elapsed_seconds=_positive_int(
                    item["max_elapsed_seconds"],
                    "review scope max_elapsed_seconds",
                ),
                max_observed_total_tokens=_optional_positive_int(
                    item["max_observed_total_tokens"],
                    "review scope max_observed_total_tokens",
                ),
            )
        )
    scope_ids = [item.scope_id for item in result]
    acceptance_ids = [item.acceptance_id for item in result]
    if len(set(scope_ids)) != len(scope_ids) or len(set(acceptance_ids)) != len(
        acceptance_ids
    ):
        raise ProjectReviewError(
            "review scope IDs and acceptance IDs must be unique"
        )
    known_scope_ids = set(scope_ids)
    if any(
        dependency not in known_scope_ids or dependency == item.scope_id
        for item in result
        for dependency in item.depends_on
    ):
        raise ProjectReviewError(
            "review scope dependencies must name other declared scopes"
        )
    return tuple(result)


def _matches(path: Path, expected: str, *, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return False
    root = root.resolve()
    return (
        not path.is_symlink()
        and resolved.is_file()
        and (resolved == root or root in resolved.parents)
        and hashlib.sha256(resolved.read_bytes()).hexdigest() == expected
    )


def _relative_path(value: Any, field: str) -> str:
    text = _nonempty(value, field).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or text.startswith("./"):
        raise ProjectReviewError(field + " must be repository-relative")
    normalized = str(path)
    if normalized in ("", "."):
        raise ProjectReviewError(field + " must name a file")
    return normalized


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectReviewError(field + " must be a non-empty string")
    return value.strip()


def _closed_identifier(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
    )
    if len(text) > 128 or any(character not in allowed for character in text):
        raise ProjectReviewError(
            field + " must use only ASCII letters, digits, '-', '_', '.', or ':'"
        )
    return text


def _optional_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty(value, field)


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProjectReviewError(field + " must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    return _positive_int(value, field)


def _sha256(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProjectReviewError(field + " must be a lowercase sha256")
    return text


def _unique_strings(
    raw: Any, field: str, *, allow_empty: bool = False
) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or (not raw and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        raise ProjectReviewError(field + " must be " + qualifier)
    result = tuple(_nonempty(value, field) for value in raw)
    if len(set(result)) != len(result):
        raise ProjectReviewError(field + " entries must be unique")
    return result


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_normalized(value: Any) -> Any:
    return json.loads(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
