"""Deterministic, external-ledger gate for governed evaluation attempts."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional, Tuple


class RerunGateError(ValueError):
    """Raised when an evaluation attempt violates the rerun contract."""


@dataclass(frozen=True)
class RerunCredit:
    credit_id: str
    owner_authority_ref: str
    reason: str


@dataclass(frozen=True)
class EvaluationPolicy:
    phase: str
    catalog_scope_ids: Tuple[str, ...]
    scope_ids: Tuple[str, ...]
    impacted_scope_ids: Tuple[str, ...]
    ledger_path: str
    ledger_sha256: str
    rerun_credit: Optional[RerunCredit]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], repo_path: Path
    ) -> "EvaluationPolicy":
        if not isinstance(raw, Mapping):
            raise RerunGateError("evaluation must be an object")
        required = {
            "phase",
            "catalog_scope_ids",
            "scope_ids",
            "impacted_scope_ids",
            "ledger_path",
            "ledger_sha256",
            "rerun_credit",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise RerunGateError(
                "evaluation missing fields: " + ", ".join(missing)
            )
        phase = raw["phase"]
        if phase not in ("red", "green"):
            raise RerunGateError("evaluation phase must be red or green")
        catalog = _validated_ids(raw["catalog_scope_ids"], "catalog_scope_ids")
        scopes = _validated_ids(raw["scope_ids"], "scope_ids")
        impacted = _validated_ids(
            raw["impacted_scope_ids"],
            "impacted_scope_ids",
            allow_empty=True,
        )
        unknown = sorted(set(scopes).difference(catalog))
        if unknown:
            raise RerunGateError("evaluation scope must be inside catalog scope")
        if phase == "red" and impacted:
            raise RerunGateError("RED evaluation cannot declare impacted scope")
        if phase == "green" and scopes != impacted:
            raise RerunGateError(
                "GREEN evaluation scope must equal its impacted scope"
            )

        ledger_value = raw["ledger_path"]
        if not isinstance(ledger_value, str) or not ledger_value:
            raise RerunGateError("ledger_path must be an absolute path")
        ledger = Path(ledger_value).expanduser()
        if not ledger.is_absolute():
            raise RerunGateError("ledger_path must be an absolute path")
        ledger = ledger.resolve()
        repo = Path(repo_path).resolve()
        if ledger == repo or repo in ledger.parents:
            raise RerunGateError("evaluation ledger must be outside repo_path")
        if ledger.exists() and (ledger.is_symlink() or not ledger.is_file()):
            raise RerunGateError("evaluation ledger must be a real file")

        expected_hash = _validated_sha256(
            raw["ledger_sha256"], "ledger_sha256"
        )
        credit_raw = raw["rerun_credit"]
        if credit_raw is None:
            credit = None
        else:
            if not isinstance(credit_raw, Mapping):
                raise RerunGateError("rerun_credit must be null or an object")
            credit_fields = ("credit_id", "owner_authority_ref", "reason")
            values = []
            for field in credit_fields:
                value = credit_raw.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise RerunGateError(
                        "rerun_credit requires non-empty " + field
                    )
                values.append(value.strip())
            credit = RerunCredit(*values)
        return cls(
            phase=phase,
            catalog_scope_ids=catalog,
            scope_ids=scopes,
            impacted_scope_ids=impacted,
            ledger_path=str(ledger),
            ledger_sha256=expected_hash,
            rerun_credit=credit,
        )


@dataclass(frozen=True)
class EvaluationRequest:
    phase: str
    control_fingerprint: str
    treatment_fingerprint: Optional[str]
    scope_ids: Tuple[str, ...]
    evaluation_fingerprint: str


def build_evaluation_request(
    policy: EvaluationPolicy,
    control_fingerprint: str,
    product_tree_hash: str,
) -> EvaluationRequest:
    control = _validated_sha256(control_fingerprint, "control_fingerprint")
    product = _validated_sha256(product_tree_hash, "product_tree_hash")
    treatment = None if policy.phase == "red" else product
    fingerprint = _canonical_hash(
        {
            "phase": policy.phase,
            "control_fingerprint": control,
            "treatment_fingerprint": treatment,
            "scope_ids": list(policy.scope_ids),
        }
    )
    return EvaluationRequest(
        phase=policy.phase,
        control_fingerprint=control,
        treatment_fingerprint=treatment,
        scope_ids=policy.scope_ids,
        evaluation_fingerprint=fingerprint,
    )


def ledger_sha256(path: Path) -> str:
    ledger = Path(path).expanduser()
    if not ledger.exists():
        return hashlib.sha256(b"").hexdigest()
    if ledger.is_symlink() or not ledger.is_file():
        raise RerunGateError("evaluation ledger must be a real file")
    return hashlib.sha256(ledger.read_bytes()).hexdigest()


def reserve_evaluation(
    policy: EvaluationPolicy,
    request: EvaluationRequest,
    contract_hash: str,
) -> dict:
    contract_hash = _validated_sha256(contract_hash, "contract_hash")
    if request.phase != policy.phase or request.scope_ids != policy.scope_ids:
        raise RerunGateError("evaluation request does not match policy")
    ledger_path = Path(policy.ledger_path)
    with _locked_ledger(ledger_path) as ledger:
        raw = _read_locked_bytes(ledger)
        current_hash = hashlib.sha256(raw).hexdigest()
        if current_hash != policy.ledger_sha256:
            raise RerunGateError("evaluation ledger hash mismatch")
        events = _parse_events(raw)
        prior_attempts = [
            event
            for event in events
            if event.get("event") == "evaluation_reserved"
            and event.get("evaluation_fingerprint")
            == request.evaluation_fingerprint
        ]
        consumed_credit_ids = {
            event.get("rerun_credit_id")
            for event in events
            if event.get("event") == "evaluation_reserved"
            and event.get("rerun_credit_consumed") is True
        }
        credit_consumed = False
        credit_id = None
        if prior_attempts:
            if policy.rerun_credit is None:
                raise RerunGateError(
                    "duplicate evaluation requires an Owner rerun credit"
                )
            credit_id = policy.rerun_credit.credit_id
            if credit_id in consumed_credit_ids:
                raise RerunGateError("rerun credit was already consumed")
            credit_consumed = True
        reservation_id = _canonical_hash(
            {
                "contract_hash": contract_hash,
                "evaluation_fingerprint": request.evaluation_fingerprint,
                "ledger_sha256": current_hash,
                "rerun_credit_id": credit_id,
            }
        )
        event = {
            "schema_version": "development-governor.evaluation-ledger.v0",
            "event": "evaluation_reserved",
            "reservation_id": reservation_id,
            "contract_hash": contract_hash,
            **asdict(request),
            "scope_ids": list(request.scope_ids),
            "prior_attempt_count": len(prior_attempts),
            "rerun_credit_consumed": credit_consumed,
            "rerun_credit_id": credit_id,
            "owner_authority_ref": (
                policy.rerun_credit.owner_authority_ref
                if credit_consumed and policy.rerun_credit
                else None
            ),
            "rerun_reason": (
                policy.rerun_credit.reason
                if credit_consumed and policy.rerun_credit
                else None
            ),
        }
        post_hash = _append_event(ledger, event)
    return {
        "status": "reserved",
        "ledger_path": str(ledger_path),
        "ledger_sha256_before": current_hash,
        "ledger_sha256_after": post_hash,
        "reservation_id": reservation_id,
        "evaluation_fingerprint": request.evaluation_fingerprint,
        "prior_attempt_count": len(prior_attempts),
        "rerun_credit_consumed": credit_consumed,
        "rerun_credit_id": credit_id,
    }


def settle_evaluation(
    ledger_path: Path, reservation_id: str, terminal_status: str
) -> dict:
    allowed_statuses = {
        "complete",
        "interrupted",
        "need_owner",
        "stopped",
        "runner_error",
    }
    if terminal_status not in allowed_statuses:
        raise RerunGateError("unsupported evaluation terminal status")
    reservation_id = _validated_sha256(reservation_id, "reservation_id")
    ledger_path = Path(ledger_path).expanduser().resolve()
    with _locked_ledger(ledger_path) as ledger:
        raw = _read_locked_bytes(ledger)
        events = _parse_events(raw)
        reservations = [
            event
            for event in events
            if event.get("event") == "evaluation_reserved"
            and event.get("reservation_id") == reservation_id
        ]
        if len(reservations) != 1:
            raise RerunGateError("evaluation reservation is missing or ambiguous")
        settled = any(
            event.get("event") == "evaluation_settled"
            and event.get("reservation_id") == reservation_id
            for event in events
        )
        if settled:
            raise RerunGateError("evaluation reservation is already settled")
        event = {
            "schema_version": "development-governor.evaluation-ledger.v0",
            "event": "evaluation_settled",
            "reservation_id": reservation_id,
            "evaluation_fingerprint": reservations[0]["evaluation_fingerprint"],
            "terminal_status": terminal_status,
        }
        post_hash = _append_event(ledger, event)
    return {
        "status": "settled",
        "reservation_id": reservation_id,
        "terminal_status": terminal_status,
        "ledger_sha256_after": post_hash,
    }


def _validated_ids(
    raw: Any, field: str, *, allow_empty: bool = False
) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise RerunGateError(field + " must be a string array")
    if not raw and not allow_empty:
        raise RerunGateError(field + " cannot be empty")
    result = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise RerunGateError(field + " must contain non-empty strings")
        result.append(value.strip())
    if len(result) != len(set(result)):
        raise RerunGateError(field + " must contain unique IDs")
    return tuple(result)


def _validated_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RerunGateError(field + " must be 64 lowercase hex characters")
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _locked_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RerunGateError("evaluation ledger must be a real file")
    with path.open("a+b") as ledger:
        fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
        try:
            yield ledger
        finally:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)


def _read_locked_bytes(ledger: BinaryIO) -> bytes:
    ledger.seek(0)
    return ledger.read()


def _parse_events(raw: bytes) -> list:
    if not raw:
        return []
    events = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RerunGateError("evaluation ledger is not UTF-8") from error
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise RerunGateError(
                "evaluation ledger contains a blank event at line %d" % line_number
            )
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise RerunGateError(
                "evaluation ledger contains invalid JSON at line %d" % line_number
            ) from error
        if not isinstance(event, dict):
            raise RerunGateError("evaluation ledger events must be objects")
        if (
            event.get("schema_version")
            != "development-governor.evaluation-ledger.v0"
        ):
            raise RerunGateError("evaluation ledger schema mismatch")
        events.append(event)
    return events


def _append_event(ledger: BinaryIO, event: Mapping[str, Any]) -> str:
    encoded = (
        json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")
    ledger.seek(0, os.SEEK_END)
    ledger.write(encoded)
    ledger.flush()
    os.fsync(ledger.fileno())
    return hashlib.sha256(_read_locked_bytes(ledger)).hexdigest()
