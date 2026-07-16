"""Deterministic stage-local authorization for governed development runs."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional, Tuple


class StageControlError(ValueError):
    """Raised when stage-local control cannot be derived deterministically."""


@dataclass(frozen=True)
class AuthorizationScope:
    scope_id: str
    capability_id: str
    stage_id: str
    status: str
    authorized_product_paths: Tuple[str, ...]


@dataclass(frozen=True)
class BlockerScopeProof:
    blocker_id: str
    affected_scope_ids: Tuple[str, ...]
    required_predicate: str
    failure_if_ignored: str
    earliest_stage: str
    safe_work_remaining_scope_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Gate:
    gate_id: str
    affected_scope_ids: Tuple[str, ...]
    status: str
    owner_decision_ref: Optional[str]


@dataclass(frozen=True)
class StageControlDecision:
    action: str
    current_scope_id: str
    safe_work_remaining_scope_ids: Tuple[str, ...]
    blocked_scope_ids: Tuple[str, ...]
    active_gate_ids: Tuple[str, ...]
    proposed_gate_ids: Tuple[str, ...]

    def as_mapping(self) -> dict:
        return {
            "action": self.action,
            "current_scope_id": self.current_scope_id,
            "safe_work_remaining_scope_ids": list(
                self.safe_work_remaining_scope_ids
            ),
            "blocked_scope_ids": list(self.blocked_scope_ids),
            "active_gate_ids": list(self.active_gate_ids),
            "proposed_gate_ids": list(self.proposed_gate_ids),
        }


@dataclass(frozen=True)
class StageControlPolicy:
    current_scope_id: str
    authorization_scopes: Tuple[AuthorizationScope, ...]
    blockers: Tuple[BlockerScopeProof, ...]
    gates: Tuple[Gate, ...]
    owner_acceptance_ref: Optional[str]
    owner_revision_ref: Optional[str]
    max_review_batches_without_owner: int
    automatic_post_review_revisions: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StageControlPolicy":
        if not isinstance(raw, Mapping):
            raise StageControlError("stage_control must be an object")
        required = {
            "current_scope_id",
            "authorization_scopes",
            "blockers",
            "gates",
            "owner_acceptance_ref",
            "owner_revision_ref",
            "max_review_batches_without_owner",
            "automatic_post_review_revisions",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise StageControlError(
                "stage_control missing fields: " + ", ".join(missing)
            )
        unsupported = sorted(set(raw).difference(required))
        if unsupported:
            raise StageControlError(
                "stage_control contains unsupported fields: "
                + ", ".join(unsupported)
            )

        current_scope_id = _nonempty_string(
            raw["current_scope_id"], "current_scope_id"
        )
        scopes = _authorization_scopes(raw["authorization_scopes"])
        scope_ids = tuple(scope.scope_id for scope in scopes)
        scope_id_set = set(scope_ids)
        if current_scope_id not in scope_id_set:
            raise StageControlError(
                "current_scope_id must reference authorization_scopes"
            )

        blockers = _blockers(raw["blockers"], scope_id_set)
        gates = _gates(raw["gates"], scope_id_set)
        owner_acceptance_ref = _optional_string(
            raw["owner_acceptance_ref"], "owner_acceptance_ref"
        )
        owner_revision_ref = _optional_string(
            raw["owner_revision_ref"], "owner_revision_ref"
        )

        max_review_batches = raw["max_review_batches_without_owner"]
        if (
            not isinstance(max_review_batches, int)
            or isinstance(max_review_batches, bool)
            or max_review_batches != 1
        ):
            raise StageControlError(
                "max_review_batches_without_owner must be exactly one batch"
            )
        automatic_revisions = raw["automatic_post_review_revisions"]
        if (
            not isinstance(automatic_revisions, int)
            or isinstance(automatic_revisions, bool)
            or automatic_revisions != 0
        ):
            raise StageControlError(
                "automatic post-review revisions must be zero"
            )

        safe_scope_ids, affected_scope_ids, _, _ = _scope_projection(
            scopes, blockers, gates
        )
        expected_safe = set(safe_scope_ids)
        for blocker in blockers:
            if set(blocker.safe_work_remaining_scope_ids) != expected_safe:
                raise StageControlError(
                    "each blocker must provide a complete safe work proof"
                )
        covered_blocked = affected_scope_ids
        for scope in scopes:
            if scope.status != "authorized" and scope.scope_id not in covered_blocked:
                raise StageControlError(
                    "blocked or pending scope requires a blocker or owner-activated gate"
                )

        return cls(
            current_scope_id=current_scope_id,
            authorization_scopes=scopes,
            blockers=blockers,
            gates=gates,
            owner_acceptance_ref=owner_acceptance_ref,
            owner_revision_ref=owner_revision_ref,
            max_review_batches_without_owner=max_review_batches,
            automatic_post_review_revisions=automatic_revisions,
        )

    @property
    def current_scope(self) -> AuthorizationScope:
        return next(
            scope
            for scope in self.authorization_scopes
            if scope.scope_id == self.current_scope_id
        )

    @property
    def decision(self) -> StageControlDecision:
        (
            safe_scope_ids,
            affected_scope_ids,
            active_gates,
            proposed_gates,
        ) = _scope_projection(
            self.authorization_scopes,
            self.blockers,
            self.gates,
        )
        blocked_scope_ids = tuple(
            scope.scope_id
            for scope in self.authorization_scopes
            if scope.scope_id not in safe_scope_ids
        )
        if self.current_scope_id in safe_scope_ids:
            action = "allow_current_scope"
        elif safe_scope_ids:
            action = "route_to_safe_scope"
        else:
            action = "terminal_owner_decision_required"
        return StageControlDecision(
            action=action,
            current_scope_id=self.current_scope_id,
            safe_work_remaining_scope_ids=safe_scope_ids,
            blocked_scope_ids=blocked_scope_ids,
            active_gate_ids=tuple(gate.gate_id for gate in active_gates),
            proposed_gate_ids=tuple(gate.gate_id for gate in proposed_gates),
        )

    def product_evidence_fuse(self, product_evidence: bool) -> str:
        if self.owner_acceptance_ref is None:
            return "not_applicable"
        return "satisfied" if product_evidence else "tripped"

    def as_mapping(self, product_evidence: Optional[bool] = None) -> dict:
        current = self.current_scope
        result = {
            "current_scope_id": self.current_scope_id,
            "claim_scope": {
                "capability_id": current.capability_id,
                "stage_id": current.stage_id,
            },
            "authorization_scopes": [
                {
                    "scope_id": scope.scope_id,
                    "capability_id": scope.capability_id,
                    "stage_id": scope.stage_id,
                    "status": scope.status,
                    "authorized_product_paths": list(
                        scope.authorized_product_paths
                    ),
                }
                for scope in self.authorization_scopes
            ],
            "blockers": [
                {
                    "blocker_id": blocker.blocker_id,
                    "affected_scope_ids": list(blocker.affected_scope_ids),
                    "required_predicate": blocker.required_predicate,
                    "failure_if_ignored": blocker.failure_if_ignored,
                    "earliest_stage": blocker.earliest_stage,
                    "safe_work_remaining_scope_ids": list(
                        blocker.safe_work_remaining_scope_ids
                    ),
                }
                for blocker in self.blockers
            ],
            "gates": [
                {
                    "gate_id": gate.gate_id,
                    "affected_scope_ids": list(gate.affected_scope_ids),
                    "status": gate.status,
                    "owner_decision_ref": gate.owner_decision_ref,
                }
                for gate in self.gates
            ],
            "owner_acceptance_ref": self.owner_acceptance_ref,
            "owner_revision_ref": self.owner_revision_ref,
            "max_review_batches_without_owner": (
                self.max_review_batches_without_owner
            ),
            "automatic_post_review_revisions": (
                self.automatic_post_review_revisions
            ),
            "decision": self.decision.as_mapping(),
        }
        if product_evidence is not None:
            result["product_evidence_fuse"] = self.product_evidence_fuse(
                product_evidence
            )
        return result


def _authorization_scopes(raw: Any) -> Tuple[AuthorizationScope, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise StageControlError("authorization_scopes must be a non-empty array")
    result = []
    seen_ids = set()
    seen_pairs = set()
    fields = {
        "scope_id",
        "capability_id",
        "stage_id",
        "status",
        "authorized_product_paths",
    }
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise StageControlError(
                "authorization_scopes entries require scope_id, capability_id, "
                "stage_id, status, and authorized_product_paths"
            )
        scope_id = _nonempty_string(item["scope_id"], "scope_id")
        capability_id = _nonempty_string(
            item["capability_id"], "capability_id"
        )
        stage_id = _nonempty_string(item["stage_id"], "stage_id")
        authorized_product_paths = _product_paths(
            item["authorized_product_paths"], "authorized_product_paths"
        )
        status = item["status"]
        if status not in ("authorized", "blocked", "pending_owner"):
            raise StageControlError(
                "authorization scope status must be authorized, blocked, or pending_owner"
            )
        pair = (capability_id, stage_id)
        if scope_id in seen_ids or pair in seen_pairs:
            raise StageControlError(
                "authorization scope IDs and capability/stage pairs must be unique"
            )
        seen_ids.add(scope_id)
        seen_pairs.add(pair)
        result.append(
            AuthorizationScope(
                scope_id,
                capability_id,
                stage_id,
                status,
                authorized_product_paths,
            )
        )
    return tuple(result)


def _blockers(
    raw: Any, scope_ids: set
) -> Tuple[BlockerScopeProof, ...]:
    if not isinstance(raw, (list, tuple)):
        raise StageControlError("blockers must be an array")
    fields = {
        "blocker_id",
        "affected_scope_ids",
        "required_predicate",
        "failure_if_ignored",
        "earliest_stage",
        "safe_work_remaining_scope_ids",
    }
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise StageControlError(
                "blocker entries require affected and safe-work scope proof"
            )
        blocker_id = _nonempty_string(item["blocker_id"], "blocker_id")
        if blocker_id in seen:
            raise StageControlError("blocker_id must be unique")
        seen.add(blocker_id)
        affected = _scope_id_array(
            item["affected_scope_ids"],
            "affected_scope_ids",
            allow_empty=False,
        )
        safe = _scope_id_array(
            item["safe_work_remaining_scope_ids"],
            "safe_work_remaining_scope_ids",
            allow_empty=True,
        )
        _require_known_scopes(affected + safe, scope_ids)
        if set(affected).intersection(safe):
            raise StageControlError(
                "affected scopes cannot also be safe work remaining"
            )
        result.append(
            BlockerScopeProof(
                blocker_id=blocker_id,
                affected_scope_ids=affected,
                required_predicate=_nonempty_string(
                    item["required_predicate"], "required_predicate"
                ),
                failure_if_ignored=_nonempty_string(
                    item["failure_if_ignored"], "failure_if_ignored"
                ),
                earliest_stage=_nonempty_string(
                    item["earliest_stage"], "earliest_stage"
                ),
                safe_work_remaining_scope_ids=safe,
            )
        )
    return tuple(result)


def _gates(raw: Any, scope_ids: set) -> Tuple[Gate, ...]:
    if not isinstance(raw, (list, tuple)):
        raise StageControlError("gates must be an array")
    fields = {
        "gate_id",
        "affected_scope_ids",
        "status",
        "owner_decision_ref",
    }
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise StageControlError(
                "gate entries require gate_id, affected_scope_ids, status, and owner_decision_ref"
            )
        gate_id = _nonempty_string(item["gate_id"], "gate_id")
        if gate_id in seen:
            raise StageControlError("gate_id must be unique")
        seen.add(gate_id)
        affected = _scope_id_array(
            item["affected_scope_ids"],
            "affected_scope_ids",
            allow_empty=False,
        )
        _require_known_scopes(affected, scope_ids)
        status = item["status"]
        if status not in ("proposed_nonblocking", "owner_activated"):
            raise StageControlError(
                "gate status must be proposed_nonblocking or owner_activated"
            )
        owner_decision_ref = _optional_string(
            item["owner_decision_ref"], "owner_decision_ref"
        )
        if status == "owner_activated" and owner_decision_ref is None:
            raise StageControlError(
                "owner-activated gate requires owner_decision_ref"
            )
        if status == "proposed_nonblocking" and owner_decision_ref is not None:
            raise StageControlError(
                "proposed nonblocking gate cannot claim owner_decision_ref"
            )
        result.append(Gate(gate_id, affected, status, owner_decision_ref))
    return tuple(result)


def _scope_id_array(
    raw: Any, field: str, *, allow_empty: bool
) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or (not raw and not allow_empty):
        suffix = "an array" if allow_empty else "a non-empty array"
        raise StageControlError(field + " must be " + suffix)
    result = []
    for value in raw:
        result.append(_nonempty_string(value, field))
    if len(set(result)) != len(result):
        raise StageControlError(field + " must not contain duplicates")
    return tuple(result)


def _product_paths(raw: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise StageControlError(field + " must be a non-empty array")
    result = []
    for value in raw:
        if not isinstance(value, str) or not value:
            raise StageControlError(field + " must contain non-empty strings")
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or normalized.startswith("./"):
            raise StageControlError(
                field + " entries must be repository-relative without '..'"
            )
        clean = str(path)
        if normalized.endswith("/"):
            clean += "/"
        if clean in ("", "."):
            raise StageControlError(field + " entries must name a repository path")
        result.append(clean)
    if len(set(result)) != len(result):
        raise StageControlError(field + " must not contain duplicates")
    return tuple(result)


def _scope_projection(
    scopes: Tuple[AuthorizationScope, ...],
    blockers: Tuple[BlockerScopeProof, ...],
    gates: Tuple[Gate, ...],
) -> Tuple[Tuple[str, ...], set, Tuple[Gate, ...], Tuple[Gate, ...]]:
    active_gates = tuple(
        gate for gate in gates if gate.status == "owner_activated"
    )
    proposed_gates = tuple(
        gate for gate in gates if gate.status == "proposed_nonblocking"
    )
    affected_scope_ids = {
        scope_id
        for blocker in blockers
        for scope_id in blocker.affected_scope_ids
    }
    affected_scope_ids.update(
        scope_id
        for gate in active_gates
        for scope_id in gate.affected_scope_ids
    )
    safe_scope_ids = tuple(
        scope.scope_id
        for scope in scopes
        if scope.status == "authorized"
        and scope.scope_id not in affected_scope_ids
    )
    return safe_scope_ids, affected_scope_ids, active_gates, proposed_gates


def _require_known_scopes(values: Tuple[str, ...], scope_ids: set) -> None:
    unknown = sorted(set(values).difference(scope_ids))
    if unknown:
        raise StageControlError(
            "stage control references unknown scopes: " + ", ".join(unknown)
        )


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageControlError(field + " must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, field)
