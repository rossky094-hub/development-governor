"""Command-line surface for the bounded Development Governor experiment."""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import sys

from development_governor.lineage import (
    LineageError,
    lineage_ledger_path,
    lineage_ledger_sha256,
)
from development_governor.rerun import ledger_sha256
from development_governor.runner import (
    ContractError,
    DevelopmentGovernor,
    RunContract,
    build_codex_command,
    validate_acceptance_material,
)
from development_governor.skill_candidate import (
    SkillCandidateError,
    promote_skill_candidate,
    stage_skill_candidate,
)
from development_governor.default_activation import (
    ActivationError,
    default_disable,
    default_enable,
    default_upgrade,
)
from development_governor.hook_guard import hook_main
from development_governor.project_entry import (
    DEFAULT_STATE_ROOT,
    ProjectEntryError,
    close_task,
    enroll_project,
    migrate_project_policy,
    prepare_task,
    project_status,
    run_isolated_check,
    start_task,
    verify_task,
)
from development_governor.public_demo import run_demo
from development_governor.project_review import (
    ProjectReviewContract,
    ProjectReviewError,
    ProjectReviewGovernor,
    derive_project_review_campaign_id,
    recover_project_review_receipt,
)


def _load_contract(path: str) -> RunContract:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractError("contract root must be a JSON object")
    return RunContract.from_mapping(raw)


def _load_project_review_contract(
    path: str, *, allow_legacy_lineage: bool = False
) -> ProjectReviewContract:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProjectReviewError("project review contract root must be a JSON object")
    return ProjectReviewContract.from_mapping(
        raw, allow_legacy_lineage=allow_legacy_lineage
    )


def _require_external_contract_path(path: str, contract: RunContract) -> None:
    contract_path = Path(path).resolve()
    repo = Path(contract.repo_path).resolve()
    if contract_path == repo or repo in contract_path.parents:
        raise ContractError(
            "contract file must be outside the governed repository"
        )


def _json_source(value: str = None, encoded: str = None):
    if (value is None) == (encoded is None):
        raise ProjectEntryError("provide exactly one JSON path/stdin source or --json-base64")
    if encoded is not None:
        try:
            decoded = base64.b64decode(
                encoded.encode("ascii"), altchars=b"-_", validate=True
            )
            raw = json.loads(decoded.decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
            raise ProjectEntryError("--json-base64 must contain URL-safe base64 JSON") from error
        if not isinstance(raw, dict):
            raise ProjectEntryError("base64 JSON root must be an object")
        return raw
    if value != "-":
        return Path(value)
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise ProjectEntryError("stdin JSON root must be an object")
    return raw


def _codex_home(value: str = None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="governor",
        description="Deterministic control for bounded Codex development runs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate without model use")
    validate.add_argument("contract")

    show = subparsers.add_parser("show-command", help="show the frozen Codex argv")
    show.add_argument("contract")
    show.add_argument("--codex", default="codex")

    run = subparsers.add_parser("run", help="start one governed root Codex run")
    run.add_argument("contract")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--codex", default="codex")

    review_spec = subparsers.add_parser(
        "review-spec",
        help="run one hash-bound project-aware Spec reviewer",
    )
    review_spec.add_argument("contract")
    review_spec.add_argument("--output-dir", required=True)
    review_spec.add_argument("--codex", default="codex")

    review_campaign_id = subparsers.add_parser(
        "review-campaign-id",
        help="derive the deterministic budget lineage for a review contract",
    )
    review_campaign_id.add_argument("contract")

    recover_review = subparsers.add_parser(
        "recover-review",
        help="validate a completed historical review without launching a model",
    )
    recover_review.add_argument("contract")
    recover_review.add_argument("--output-dir", required=True)

    stage = subparsers.add_parser(
        "stage-skill", help="copy an installed Skill into a new Git candidate"
    )
    stage.add_argument("--source", required=True)
    stage.add_argument("--acceptance-source", required=True)
    stage.add_argument("--candidate-repo", required=True)

    promote = subparsers.add_parser(
        "promote-skill", help="promote a hash-bound completed Skill candidate"
    )
    promote.add_argument("--candidate-repo", required=True)
    promote.add_argument("--installed-skill", required=True)
    promote.add_argument("--terminal-receipt", required=True)
    promote.add_argument(
        "--allow-new-install",
        action="store_true",
        help="explicitly authorize creation of an absent installed Skill directory",
    )

    enroll = subparsers.add_parser(
        "enroll", help="register one project policy without model use"
    )
    enroll.add_argument("policy", nargs="?", help="policy JSON path, or - for stdin")
    enroll.add_argument("--json-base64", help="URL-safe base64 policy JSON")

    migrate = subparsers.add_parser(
        "migrate-policy", help="replace an enrolled policy under exact Owner authority"
    )
    migrate.add_argument("policy", nargs="?", help="replacement policy JSON path, or - for stdin")
    migrate.add_argument("--json-base64", help="URL-safe base64 replacement policy JSON")
    migrate.add_argument("--expected-policy-hash", required=True)
    migrate.add_argument("--owner-authorization-ref", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="freeze one task capsule without issuing a lease"
    )
    prepare.add_argument("capsule", nargs="?", help="task capsule JSON path, or - for stdin")
    prepare.add_argument("--json-base64", help="URL-safe base64 task capsule JSON")

    start = subparsers.add_parser(
        "start", help="activate one prepared task lease without model use"
    )
    start.add_argument("task_ref", help="prepared task hash or task.json path")

    status = subparsers.add_parser("status", help="show enrolled project and lease state")
    status.add_argument("--repo", required=True)

    check = subparsers.add_parser(
        "check", help="run a non-promoting command in an isolated repository snapshot"
    )
    check.add_argument("--repo", required=True)
    check.add_argument("argv", nargs=argparse.REMAINDER)

    verify = subparsers.add_parser("verify", help="run frozen acceptance commands")
    verify.add_argument("--repo", required=True)

    close = subparsers.add_parser("close", help="close a verified or Owner-aborted task")
    close.add_argument("--repo", required=True)
    close.add_argument("--owner-abort-reason")

    enable = subparsers.add_parser(
        "default-enable", help="install the user-level default Governor entry"
    )
    enable.add_argument("--codex-home")
    enable.add_argument("--governor-repo")

    disable = subparsers.add_parser(
        "default-disable", help="remove the user-level default Governor entry"
    )
    disable.add_argument("--codex-home")
    disable.add_argument("--restore-backup", action="store_true")

    upgrade = subparsers.add_parser(
        "default-upgrade", help="upgrade the default runtime under explicit Owner authority"
    )
    upgrade.add_argument("--codex-home")
    upgrade.add_argument("--governor-repo")
    upgrade.add_argument("--owner-authorization-ref", required=True)

    subparsers.add_parser("hook-guard", help="evaluate one PreToolUse event from stdin")
    subparsers.add_parser("demo", help="run a self-contained zero-model control demo")

    args = parser.parse_args(argv)
    try:
        if args.command == "hook-guard":
            return hook_main()
        if args.command == "demo":
            payload = run_demo()
        elif args.command == "review-campaign-id":
            try:
                raw = json.loads(Path(args.contract).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProjectReviewError(
                    "project review contract must be valid JSON"
                ) from error
            if not isinstance(raw, dict):
                raise ProjectReviewError(
                    "project review contract root must be a JSON object"
                )
            required = {
                "repo_path",
                "candidate",
                "acceptance_target_scope_ids",
                "owner_review_authorization_ref",
            }
            if not required.issubset(raw) or not isinstance(
                raw.get("candidate"), dict
            ) or "sha256" not in raw["candidate"]:
                raise ProjectReviewError(
                    "campaign identity requires repo, candidate hash, targets, and Owner review reference"
                )
            campaign_id = derive_project_review_campaign_id(
                repo_path=Path(raw["repo_path"]),
                candidate_sha256=raw["candidate"]["sha256"],
                acceptance_target_scope_ids=raw[
                    "acceptance_target_scope_ids"
                ],
                owner_review_authorization_ref=raw[
                    "owner_review_authorization_ref"
                ],
            )
            ledger_path = lineage_ledger_path(
                DEFAULT_STATE_ROOT, Path(raw["repo_path"]), campaign_id
            )
            payload = {
                "review_campaign_id": campaign_id,
                "lineage_ledger_sha256": lineage_ledger_sha256(ledger_path),
            }
        elif args.command == "review-spec":
            review_contract = _load_project_review_contract(args.contract)
            _require_external_contract_path(args.contract, review_contract)
            payload = ProjectReviewGovernor(
                args.codex, state_root=DEFAULT_STATE_ROOT
            ).run(review_contract, Path(args.output_dir))
        elif args.command == "recover-review":
            review_contract = _load_project_review_contract(
                args.contract, allow_legacy_lineage=True
            )
            _require_external_contract_path(args.contract, review_contract)
            payload = recover_project_review_receipt(
                review_contract, Path(args.output_dir)
            )
        elif args.command == "enroll":
            payload = enroll_project(
                _json_source(args.policy, args.json_base64),
                state_root=DEFAULT_STATE_ROOT,
            )
        elif args.command == "migrate-policy":
            payload = migrate_project_policy(
                _json_source(args.policy, args.json_base64),
                expected_policy_hash=args.expected_policy_hash,
                owner_authorization_ref=args.owner_authorization_ref,
                state_root=DEFAULT_STATE_ROOT,
            )
        elif args.command == "prepare":
            payload = prepare_task(
                _json_source(args.capsule, args.json_base64),
                state_root=DEFAULT_STATE_ROOT,
            )
        elif args.command == "start":
            payload = start_task(args.task_ref, state_root=DEFAULT_STATE_ROOT)
        elif args.command == "status":
            payload = project_status(
                Path(args.repo), state_root=DEFAULT_STATE_ROOT
            )
        elif args.command == "check":
            command_argv = list(args.argv)
            if command_argv and command_argv[0] == "--":
                command_argv.pop(0)
            payload = run_isolated_check(
                Path(args.repo), command_argv, state_root=DEFAULT_STATE_ROOT
            )
        elif args.command == "verify":
            payload = verify_task(Path(args.repo), state_root=DEFAULT_STATE_ROOT)
        elif args.command == "close":
            payload = close_task(
                Path(args.repo),
                state_root=DEFAULT_STATE_ROOT,
                owner_abort_reason=args.owner_abort_reason,
            )
        elif args.command == "default-enable":
            module_path = Path(__file__).resolve()
            inferred_repo = module_path.parents[2]
            governor_repo = Path(args.governor_repo).expanduser() if args.governor_repo else None
            if governor_repo is None and (inferred_repo / ".git").exists():
                governor_repo = inferred_repo
            payload = default_enable(
                codex_home=_codex_home(args.codex_home),
                source_package=module_path.parent,
                governor_repo=governor_repo,
            )
        elif args.command == "default-disable":
            payload = default_disable(
                codex_home=_codex_home(args.codex_home),
                restore_backup=args.restore_backup,
            )
        elif args.command == "default-upgrade":
            module_path = Path(__file__).resolve()
            inferred_repo = module_path.parents[2]
            governor_repo = Path(args.governor_repo).expanduser() if args.governor_repo else None
            if governor_repo is None and (inferred_repo / ".git").exists():
                governor_repo = inferred_repo
            source_package = (
                governor_repo / "src" / "development_governor"
                if governor_repo is not None
                else module_path.parent
            )
            payload = default_upgrade(
                codex_home=_codex_home(args.codex_home),
                source_package=source_package,
                governor_repo=governor_repo,
                owner_authorization_ref=args.owner_authorization_ref,
            )
        elif args.command == "stage-skill":
            payload = stage_skill_candidate(
                Path(args.source),
                Path(args.acceptance_source),
                Path(args.candidate_repo),
            )
        elif args.command == "promote-skill":
            payload = promote_skill_candidate(
                Path(args.candidate_repo),
                Path(args.installed_skill),
                Path(args.terminal_receipt),
                allow_new_install=args.allow_new_install,
            )
        else:
            contract = _load_contract(args.contract)
            _require_external_contract_path(args.contract, contract)
            if args.command == "validate":
                acceptance = validate_acceptance_material(contract)
                if acceptance["status"] != "matched":
                    raise ContractError(
                        "acceptance material hash mismatch: "
                        + ", ".join(acceptance["mismatched_files"])
                    )
                try:
                    lineage_path = lineage_ledger_path(
                        DevelopmentGovernor().state_root,
                        Path(contract.repo_path),
                        contract.lineage.lineage_root_id,
                    )
                    current_lineage_hash = lineage_ledger_sha256(lineage_path)
                except LineageError as error:
                    raise ContractError(str(error)) from error
                if current_lineage_hash != contract.lineage.ledger_sha256:
                    raise ContractError("lineage ledger hash mismatch")
                evaluation = None
                if contract.evaluation is not None:
                    current_ledger_hash = ledger_sha256(
                        Path(contract.evaluation.ledger_path)
                    )
                    if current_ledger_hash != contract.evaluation.ledger_sha256:
                        raise ContractError("evaluation ledger hash mismatch")
                    evaluation = {
                        "phase": contract.evaluation.phase,
                        "scope_ids": list(contract.evaluation.scope_ids),
                        "impacted_scope_ids": list(
                            contract.evaluation.impacted_scope_ids
                        ),
                        "control_fingerprint": (
                            contract.acceptance_control_fingerprint
                        ),
                        "ledger_status": "matched",
                    }
                payload = {
                    "status": "valid",
                    "contract_hash": contract.contract_hash,
                    "max_parallel_agents": contract.max_parallel_agents,
                    "max_total_agents": contract.max_total_agents,
                    "execution_mode": contract.execution_mode,
                    "primary_mode": contract.primary_mode,
                    "reasoning_effort": contract.reasoning_effort,
                    "review_wave_cost": contract.review_wave_cost,
                    "stage_scope": contract.stage_control.current_scope_id,
                    "stage_admission": (
                        contract.stage_control.decision.action
                    ),
                    "runnable": (
                        contract.stage_control.decision.action
                        == "allow_current_scope"
                    ),
                    "owner_acceptance_present": (
                        contract.stage_control.owner_acceptance_ref is not None
                    ),
                    "lineage": {
                        "lineage_root_id": contract.lineage.lineage_root_id,
                        "ledger_status": "matched",
                    },
                    "acceptance_interface_hash": contract.acceptance_interface_hash,
                    "acceptance_test_bundle_hash": contract.acceptance_test_bundle_hash,
                    "evaluation": evaluation,
                    "control_boundary": "one_root_with_contract_gated_multi_agent",
                }
            elif args.command == "show-command":
                payload = {
                    "contract_hash": contract.contract_hash,
                    "argv": list(build_codex_command(contract, args.codex)),
                }
            else:
                payload = DevelopmentGovernor(args.codex).run(
                    contract, Path(args.output_dir)
                )
    except (
        ActivationError,
        ContractError,
        ProjectEntryError,
        ProjectReviewError,
        SkillCandidateError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}))
        return 2

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if args.command == "verify" and payload.get("status") != "verification_passed":
        return 1
    if args.command == "check" and payload.get("status") != "check_passed":
        return 1
    if args.command == "review-spec" and payload.get("status") != "complete":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
