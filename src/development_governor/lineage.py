"""Append-only, lineage-scoped budget reservations."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, BinaryIO, Mapping, Optional


SCHEMA_VERSION = "development-governor.lineage-ledger.v0"


class LineageError(ValueError):
    """Raised when lineage policy or ledger state is invalid."""


@dataclass(frozen=True)
class OwnerReviewCredit:
    credit_id: str
    owner_authority_ref: str
    reason: str


@dataclass(frozen=True)
class LineagePolicy:
    lineage_root_id: str
    ledger_sha256: str
    max_elapsed_seconds: int
    max_invocations: int
    max_review_waves: int
    resume_from_reservation_id: Optional[str]
    resume_session_id: Optional[str]
    owner_review_credit: Optional[OwnerReviewCredit]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LineagePolicy":
        if not isinstance(raw, Mapping):
            raise LineageError("lineage must be an object")
        required = {
            "lineage_root_id",
            "ledger_sha256",
            "max_elapsed_seconds",
            "max_invocations",
            "max_review_waves",
            "resume_from_reservation_id",
            "resume_session_id",
            "owner_review_credit",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise LineageError("lineage missing fields: " + ", ".join(missing))
        unsupported = sorted(set(raw).difference(required))
        if unsupported:
            raise LineageError(
                "lineage contains unsupported fields: " + ", ".join(unsupported)
            )

        lineage_root_id = _nonempty_string(raw["lineage_root_id"], "lineage_root_id")
        ledger_hash = _sha256(raw["ledger_sha256"], "ledger_sha256")
        max_elapsed = _positive_int(
            raw["max_elapsed_seconds"], "max_elapsed_seconds"
        )
        max_invocations = _positive_int(raw["max_invocations"], "max_invocations")
        max_review = _nonnegative_int(
            raw["max_review_waves"], "max_review_waves"
        )

        reservation_raw = raw["resume_from_reservation_id"]
        session_raw = raw["resume_session_id"]
        if (reservation_raw is None) != (session_raw is None):
            raise LineageError(
                "resume_from_reservation_id and resume_session_id must both be null or both be set"
            )
        if reservation_raw is None:
            resume_reservation = None
            resume_session = None
        else:
            resume_reservation = _sha256(
                reservation_raw, "resume_from_reservation_id"
            )
            resume_session = _nonempty_string(session_raw, "resume_session_id")

        credit_raw = raw["owner_review_credit"]
        if credit_raw is None:
            credit = None
        else:
            if not isinstance(credit_raw, Mapping):
                raise LineageError("owner_review_credit must be null or an object")
            fields = {"credit_id", "owner_authority_ref", "reason"}
            if set(credit_raw) != fields:
                raise LineageError(
                    "owner_review_credit requires credit_id, owner_authority_ref, and reason"
                )
            credit = OwnerReviewCredit(
                _nonempty_string(credit_raw["credit_id"], "credit_id"),
                _nonempty_string(
                    credit_raw["owner_authority_ref"], "owner_authority_ref"
                ),
                _nonempty_string(credit_raw["reason"], "reason"),
            )
        return cls(
            lineage_root_id=lineage_root_id,
            ledger_sha256=ledger_hash,
            max_elapsed_seconds=max_elapsed,
            max_invocations=max_invocations,
            max_review_waves=max_review,
            resume_from_reservation_id=resume_reservation,
            resume_session_id=resume_session,
            owner_review_credit=credit,
        )


def lineage_ledger_path(
    state_root: Path, repo_path: Path, lineage_root_id: str
) -> Path:
    """Return a stable external ledger path for a Git common dir and lineage."""
    root = Path(state_root).expanduser().resolve()
    repo = Path(repo_path).expanduser().resolve()
    lineage_id = _nonempty_string(lineage_root_id, "lineage_root_id")
    if not repo.is_dir():
        raise LineageError("repo_path must be an existing directory")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise LineageError("repo_path must identify a Git worktree") from error
    value = result.stdout.strip()
    if not value:
        raise LineageError("Git common dir cannot be empty")
    common = Path(value)
    if not common.is_absolute():
        common = repo / common
    common = common.resolve()
    if not common.is_dir():
        raise LineageError("Git common dir must be an existing directory")
    identity = _canonical_hash(
        {"git_common_dir": str(common), "lineage_root_id": lineage_id}
    )
    return root / "lineage-ledgers" / (identity + ".jsonl")


def lineage_ledger_sha256(path: Path) -> str:
    ledger = Path(path).expanduser()
    if not ledger.exists():
        return hashlib.sha256(b"").hexdigest()
    if ledger.is_symlink() or not ledger.is_file():
        raise LineageError("lineage ledger must be a real file")
    return hashlib.sha256(ledger.read_bytes()).hexdigest()


def reserve_lineage(
    policy: LineagePolicy,
    *,
    ledger_path: Path,
    contract_hash: str,
    candidate_hash: str,
    requested_elapsed_seconds: int,
    requested_review_waves: int,
    current_scope_id: Optional[str] = None,
    primary_mode: Optional[str] = None,
    owner_acceptance_ref: Optional[str] = None,
    owner_revision_ref: Optional[str] = None,
) -> dict:
    if not isinstance(policy, LineagePolicy):
        raise LineageError("policy must be a LineagePolicy")
    contract = _sha256(contract_hash, "contract_hash")
    candidate = _candidate_hash(candidate_hash)
    elapsed = _positive_int(
        requested_elapsed_seconds, "requested_elapsed_seconds"
    )
    review_waves = _nonnegative_int(
        requested_review_waves, "requested_review_waves"
    )
    scope_id, mode, acceptance_ref, revision_ref = _reservation_context(
        current_scope_id=current_scope_id,
        primary_mode=primary_mode,
        owner_acceptance_ref=owner_acceptance_ref,
        owner_revision_ref=owner_revision_ref,
    )
    path = _ledger_path(ledger_path)

    with _locked_ledger(path) as ledger:
        raw = _read_locked_bytes(ledger)
        before_hash = hashlib.sha256(raw).hexdigest()
        if before_hash != policy.ledger_sha256:
            raise LineageError("lineage ledger hash mismatch")
        state = _rebuild(raw)
        metadata = state["metadata"]
        if metadata is not None:
            if metadata["lineage_root_id"] != policy.lineage_root_id:
                raise LineageError("lineage root does not match ledger")
            maxima = (
                metadata["max_elapsed_seconds"],
                metadata["max_invocations"],
                metadata["max_review_waves"],
            )
            supplied = (
                policy.max_elapsed_seconds,
                policy.max_invocations,
                policy.max_review_waves,
            )
            if maxima != supplied:
                raise LineageError("lineage ledger has frozen budgets")
        if state["active_reservation_id"] is not None:
            raise LineageError("lineage already has an active reservation")

        resume_target = policy.resume_from_reservation_id
        if resume_target is None:
            if state["unresumed_interruptions"]:
                raise LineageError(
                    "interrupted reservation requires explicit resume"
                )
        else:
            target = state["settlements"].get(resume_target)
            if target is None:
                raise LineageError("resume reservation is missing")
            if target["terminal_status"] != "interrupted":
                raise LineageError("resume reservation must be interrupted")
            if target["session_id"] != policy.resume_session_id:
                raise LineageError("resume session does not match interruption")
            if resume_target not in state["unresumed_interruptions"]:
                raise LineageError("interrupted reservation was already resumed")

        _enforce_scope_invariants(
            state,
            current_scope_id=scope_id,
            primary_mode=mode,
            owner_acceptance_ref=acceptance_ref,
            owner_revision_ref=revision_ref,
            candidate_hash=candidate,
        )

        if state["elapsed_seconds_spent"] + elapsed > policy.max_elapsed_seconds:
            raise LineageError("insufficient elapsed time budget")
        if state["invocations_spent"] + 1 > policy.max_invocations:
            raise LineageError("insufficient invocation budget")

        base_review_limit = (
            policy.max_review_waves + state["spent_review_credit_count"]
        )
        projected_review = state["review_waves_spent"] + review_waves
        review_excess = projected_review - base_review_limit
        credit_consumed = False
        credit = policy.owner_review_credit
        if review_excess > 0:
            if review_excess != 1:
                raise LineageError("review wave budget exhausted")
            if credit is None:
                raise LineageError("review wave budget exhausted")
            if credit.credit_id in state["consumed_credit_ids"]:
                raise LineageError("owner review credit was already consumed")
            credit_consumed = True

        reservation_id = _canonical_hash(
            {
                "candidate_hash": candidate,
                "contract_hash": contract,
                "current_scope_id": scope_id,
                "ledger_sha256": before_hash,
                "lineage_root_id": policy.lineage_root_id,
                "owner_acceptance_ref": acceptance_ref,
                "owner_review_credit_id": (
                    credit.credit_id if credit_consumed and credit else None
                ),
                "owner_revision_ref": revision_ref,
                "primary_mode": mode,
                "requested_elapsed_seconds": elapsed,
                "requested_review_waves": review_waves,
                "resume_from_reservation_id": resume_target,
                "resume_session_id": policy.resume_session_id,
            }
        )
        if metadata is None:
            _append_event(
                ledger,
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": "lineage_initialized",
                    "lineage_root_id": policy.lineage_root_id,
                    "max_elapsed_seconds": policy.max_elapsed_seconds,
                    "max_invocations": policy.max_invocations,
                    "max_review_waves": policy.max_review_waves,
                },
            )
        _append_event(
            ledger,
            {
                "schema_version": SCHEMA_VERSION,
                "event": "lineage_reserved",
                "reservation_id": reservation_id,
                "contract_hash": contract,
                "candidate_hash": candidate,
                "current_scope_id": scope_id,
                "primary_mode": mode,
                "owner_acceptance_ref": acceptance_ref,
                "owner_revision_ref": revision_ref,
                "requested_elapsed_seconds": elapsed,
                "requested_invocations": 1,
                "requested_review_waves": review_waves,
                "resume_from_reservation_id": resume_target,
                "resume_session_id": policy.resume_session_id,
                "owner_review_credit_consumed": credit_consumed,
                "owner_review_credit_id": (
                    credit.credit_id if credit_consumed and credit else None
                ),
                "owner_authority_ref": (
                    credit.owner_authority_ref if credit_consumed and credit else None
                ),
                "owner_review_reason": (
                    credit.reason if credit_consumed and credit else None
                ),
            },
        )
        after_raw = _read_locked_bytes(ledger)
        after_state = _rebuild(after_raw)
        after_hash = hashlib.sha256(after_raw).hexdigest()
    return {
        "status": "reserved",
        "ledger_path": str(path),
        "ledger_sha256_before": before_hash,
        "ledger_sha256_after": after_hash,
        "reservation_id": reservation_id,
        "owner_review_credit_consumed": credit_consumed,
        "owner_revision_ref": revision_ref,
        "projection": _projection(after_state),
    }


def settle_lineage(
    ledger_path: Path,
    reservation_id: str,
    *,
    terminal_status: str,
    model_started: bool,
    actual_elapsed_seconds: float,
    session_id: Optional[str],
) -> dict:
    reservation_id = _sha256(reservation_id, "reservation_id")
    allowed = {"complete", "interrupted", "need_owner", "stopped", "runner_error"}
    if terminal_status not in allowed:
        raise LineageError("unsupported lineage terminal status")
    if not isinstance(model_started, bool):
        raise LineageError("model_started must be boolean")
    actual = _nonnegative_number(
        actual_elapsed_seconds, "actual_elapsed_seconds"
    )
    if session_id is None:
        normalized_session = None
    else:
        normalized_session = _nonempty_string(session_id, "session_id")
    if not model_started and normalized_session is not None:
        raise LineageError("unstarted model cannot have session_id")
    if terminal_status == "interrupted" and (
        not model_started or normalized_session is None
    ):
        raise LineageError(
            "interrupted settlement requires a started model and session_id"
        )

    path = _ledger_path(ledger_path)
    with _locked_ledger(path) as ledger:
        raw = _read_locked_bytes(ledger)
        state = _rebuild(raw)
        reservation = state["reservations"].get(reservation_id)
        if reservation is None:
            raise LineageError("lineage reservation is missing")
        existing = state["settlements"].get(reservation_id)
        if existing is not None:
            same = (
                existing["terminal_status"] == terminal_status
                and existing["model_started"] is model_started
                and existing["actual_elapsed_seconds"] == actual
                and existing["session_id"] == normalized_session
            )
            if not same:
                raise LineageError("conflicting settlement retry")
            return {
                "status": "settled",
                "reservation_id": reservation_id,
                "terminal_status": terminal_status,
                "ledger_sha256_after": hashlib.sha256(raw).hexdigest(),
                "idempotent": True,
                "projection": _projection(state),
            }
        if state["active_reservation_id"] != reservation_id:
            raise LineageError("lineage reservation is not active")
        charged_elapsed = math.ceil(actual) if model_started else 0
        event = {
            "schema_version": SCHEMA_VERSION,
            "event": "lineage_settled",
            "reservation_id": reservation_id,
            "terminal_status": terminal_status,
            "model_started": model_started,
            "actual_elapsed_seconds": actual,
            "charged_elapsed_seconds": charged_elapsed,
            "charged_invocations": 1 if model_started else 0,
            "charged_review_waves": (
                reservation["requested_review_waves"] if model_started else 0
            ),
            "session_id": normalized_session,
        }
        _append_event(ledger, event)
        after_raw = _read_locked_bytes(ledger)
        after_state = _rebuild(after_raw)
        after_hash = hashlib.sha256(after_raw).hexdigest()
    return {
        "status": "settled",
        "reservation_id": reservation_id,
        "terminal_status": terminal_status,
        "ledger_sha256_after": after_hash,
        "idempotent": False,
        "projection": _projection(after_state),
    }


def _projection(state: dict) -> dict:
    metadata = state["metadata"]
    if metadata is None:
        raise LineageError("lineage ledger is not initialized")
    active = None
    if state["active_reservation_id"] is not None:
        active = state["reservations"][state["active_reservation_id"]]
    active_elapsed = active["requested_elapsed_seconds"] if active else 0
    active_invocations = active["requested_invocations"] if active else 0
    active_reviews = active["requested_review_waves"] if active else 0
    active_credit = int(
        bool(active and active["owner_review_credit_consumed"])
    )
    elapsed_reserved = state["elapsed_seconds_spent"] + active_elapsed
    invocations_reserved = state["invocations_spent"] + active_invocations
    review_reserved = state["review_waves_spent"] + active_reviews
    review_limit = (
        metadata["max_review_waves"]
        + state["spent_review_credit_count"]
        + active_credit
    )
    return {
        "elapsed_seconds_spent": state["elapsed_seconds_spent"],
        "elapsed_seconds_reserved": elapsed_reserved,
        "elapsed_seconds_remaining": metadata["max_elapsed_seconds"] - elapsed_reserved,
        "invocations_spent": state["invocations_spent"],
        "invocations_reserved": invocations_reserved,
        "invocations_remaining": metadata["max_invocations"] - invocations_reserved,
        "review_waves_spent": state["review_waves_spent"],
        "review_waves_reserved": review_reserved,
        "review_waves_remaining": review_limit - review_reserved,
    }


def _rebuild(raw: bytes) -> dict:
    events = _parse_events(raw)
    state = {
        "metadata": None,
        "reservations": {},
        "settlements": {},
        "active_reservation_id": None,
        "elapsed_seconds_spent": 0,
        "invocations_spent": 0,
        "review_waves_spent": 0,
        "spent_review_credit_count": 0,
        "consumed_credit_ids": set(),
        "unresumed_interruptions": set(),
        "owner_acceptance_refs": {},
        "reviewed_candidate_hashes": {},
    }
    for index, event in enumerate(events):
        kind = event.get("event")
        if kind == "lineage_initialized":
            if index != 0 or state["metadata"] is not None:
                raise LineageError("lineage initialization must be the first event")
            state["metadata"] = {
                "lineage_root_id": _nonempty_string(
                    event.get("lineage_root_id"), "lineage_root_id"
                ),
                "max_elapsed_seconds": _positive_int(
                    event.get("max_elapsed_seconds"), "max_elapsed_seconds"
                ),
                "max_invocations": _positive_int(
                    event.get("max_invocations"), "max_invocations"
                ),
                "max_review_waves": _nonnegative_int(
                    event.get("max_review_waves"), "max_review_waves"
                ),
            }
            continue
        if state["metadata"] is None:
            raise LineageError("lineage ledger is missing initialization")
        if kind == "lineage_reserved":
            if state["active_reservation_id"] is not None:
                raise LineageError("lineage ledger contains concurrent reservations")
            reservation_id = _sha256(event.get("reservation_id"), "reservation_id")
            if reservation_id in state["reservations"]:
                raise LineageError("lineage ledger repeats a reservation")
            _sha256(event.get("contract_hash"), "contract_hash")
            candidate = _candidate_hash(event.get("candidate_hash"))
            context_fields = (
                "current_scope_id",
                "primary_mode",
                "owner_acceptance_ref",
                "owner_revision_ref",
            )
            context_presence = [field in event for field in context_fields]
            if any(context_presence) and not all(context_presence):
                raise LineageError(
                    "lineage reservation has incomplete scope binding"
                )
            if any(context_presence):
                scope_id, mode, acceptance_ref, revision_ref = (
                    _reservation_context(
                        current_scope_id=event.get("current_scope_id"),
                        primary_mode=event.get("primary_mode"),
                        owner_acceptance_ref=event.get("owner_acceptance_ref"),
                        owner_revision_ref=event.get("owner_revision_ref"),
                    )
                )
            else:
                # Ledgers created before scope bindings remain replayable.
                scope_id = mode = acceptance_ref = revision_ref = None
            _enforce_scope_invariants(
                state,
                current_scope_id=scope_id,
                primary_mode=mode,
                owner_acceptance_ref=acceptance_ref,
                owner_revision_ref=revision_ref,
                candidate_hash=candidate,
            )
            elapsed = _positive_int(
                event.get("requested_elapsed_seconds"), "requested_elapsed_seconds"
            )
            if event.get("requested_invocations") != 1:
                raise LineageError("lineage reservation must reserve one invocation")
            reviews = _nonnegative_int(
                event.get("requested_review_waves"), "requested_review_waves"
            )
            resume_target = event.get("resume_from_reservation_id")
            resume_session = event.get("resume_session_id")
            if (resume_target is None) != (resume_session is None):
                raise LineageError("lineage reservation has incomplete resume binding")
            if resume_target is not None:
                resume_target = _sha256(resume_target, "resume_from_reservation_id")
                resume_session = _nonempty_string(resume_session, "resume_session_id")
                target = state["settlements"].get(resume_target)
                if target is None or target["terminal_status"] != "interrupted":
                    raise LineageError("lineage ledger has invalid resume reservation")
                if target["session_id"] != resume_session:
                    raise LineageError("lineage ledger has mismatched resume session")
                if resume_target not in state["unresumed_interruptions"]:
                    raise LineageError("lineage ledger repeats a resume reservation")
                state["unresumed_interruptions"].remove(resume_target)
            elif state["unresumed_interruptions"]:
                raise LineageError("lineage ledger bypasses interrupted reservation")

            credit_consumed = event.get("owner_review_credit_consumed")
            if not isinstance(credit_consumed, bool):
                raise LineageError("owner review credit consumption must be boolean")
            credit_id = event.get("owner_review_credit_id")
            if credit_consumed:
                credit_id = _nonempty_string(credit_id, "owner_review_credit_id")
                _nonempty_string(event.get("owner_authority_ref"), "owner_authority_ref")
                _nonempty_string(event.get("owner_review_reason"), "owner_review_reason")
                if credit_id in state["consumed_credit_ids"]:
                    raise LineageError("owner review credit was already consumed")
                state["consumed_credit_ids"].add(credit_id)
            elif any(
                event.get(field) is not None
                for field in (
                    "owner_review_credit_id",
                    "owner_authority_ref",
                    "owner_review_reason",
                )
            ):
                raise LineageError("unused owner review credit metadata must be null")

            metadata = state["metadata"]
            if state["elapsed_seconds_spent"] + elapsed > metadata["max_elapsed_seconds"]:
                raise LineageError("lineage ledger exceeds elapsed budget")
            if state["invocations_spent"] + 1 > metadata["max_invocations"]:
                raise LineageError("lineage ledger exceeds invocation budget")
            review_limit = (
                metadata["max_review_waves"]
                + state["spent_review_credit_count"]
                + int(credit_consumed)
            )
            if state["review_waves_spent"] + reviews > review_limit:
                raise LineageError("lineage ledger exceeds review wave budget")
            if (
                credit_consumed
                and state["review_waves_spent"] + reviews <= review_limit - 1
            ):
                raise LineageError("lineage ledger consumed an unnecessary owner credit")
            normalized = dict(event)
            normalized["requested_elapsed_seconds"] = elapsed
            normalized["requested_invocations"] = 1
            normalized["requested_review_waves"] = reviews
            normalized["owner_review_credit_consumed"] = credit_consumed
            normalized["current_scope_id"] = scope_id
            normalized["primary_mode"] = mode
            normalized["owner_acceptance_ref"] = acceptance_ref
            normalized["owner_revision_ref"] = revision_ref
            state["reservations"][reservation_id] = normalized
            if acceptance_ref is not None:
                state["owner_acceptance_refs"][scope_id] = acceptance_ref
            state["active_reservation_id"] = reservation_id
            continue
        if kind == "lineage_settled":
            reservation_id = _sha256(event.get("reservation_id"), "reservation_id")
            reservation = state["reservations"].get(reservation_id)
            if reservation is None:
                raise LineageError("lineage settlement has no reservation")
            if reservation_id in state["settlements"]:
                raise LineageError("lineage ledger repeats a settlement")
            if state["active_reservation_id"] != reservation_id:
                raise LineageError("lineage settlement is out of order")
            terminal_status = event.get("terminal_status")
            if terminal_status not in {
                "complete", "interrupted", "need_owner", "stopped", "runner_error"
            }:
                raise LineageError("lineage ledger has unsupported terminal status")
            started = event.get("model_started")
            if not isinstance(started, bool):
                raise LineageError("lineage settlement model_started must be boolean")
            actual = _nonnegative_number(
                event.get("actual_elapsed_seconds"), "actual_elapsed_seconds"
            )
            session = event.get("session_id")
            if started:
                if session is not None:
                    session = _nonempty_string(session, "session_id")
                elif terminal_status == "interrupted":
                    raise LineageError(
                        "interrupted lineage settlement requires session_id"
                    )
                charged_elapsed = math.ceil(actual)
                charged_invocations = 1
                charged_reviews = reservation["requested_review_waves"]
            else:
                if session is not None:
                    raise LineageError("unstarted lineage settlement has session_id")
                if terminal_status == "interrupted":
                    raise LineageError("unstarted lineage settlement cannot be interrupted")
                charged_elapsed = 0
                charged_invocations = 0
                charged_reviews = 0
            if (
                event.get("charged_elapsed_seconds") != charged_elapsed
                or event.get("charged_invocations") != charged_invocations
                or event.get("charged_review_waves") != charged_reviews
            ):
                raise LineageError("lineage settlement charges do not match reservation")
            normalized = dict(event)
            normalized["actual_elapsed_seconds"] = actual
            normalized["session_id"] = session
            state["settlements"][reservation_id] = normalized
            state["active_reservation_id"] = None
            if started:
                state["elapsed_seconds_spent"] += charged_elapsed
                state["invocations_spent"] += 1
                state["review_waves_spent"] += charged_reviews
                if reservation["owner_review_credit_consumed"]:
                    state["spent_review_credit_count"] += 1
                scope_id = reservation["current_scope_id"]
                if scope_id is not None:
                    if charged_reviews > 0:
                        state["reviewed_candidate_hashes"][scope_id] = (
                            reservation["candidate_hash"]
                        )
                    elif (
                        reservation["owner_revision_ref"] is not None
                        and state["reviewed_candidate_hashes"].get(scope_id)
                        != reservation["candidate_hash"]
                    ):
                        state["reviewed_candidate_hashes"].pop(scope_id, None)
            if terminal_status == "interrupted":
                state["unresumed_interruptions"].add(reservation_id)
            continue
        raise LineageError("lineage ledger contains unsupported event")
    return state


def _parse_events(raw: bytes) -> list:
    if not raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LineageError("lineage ledger is not UTF-8") from error
    events = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise LineageError(
                "lineage ledger contains a blank event at line %d" % line_number
            )
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise LineageError(
                "lineage ledger contains invalid JSON at line %d" % line_number
            ) from error
        if not isinstance(event, dict):
            raise LineageError("lineage ledger events must be objects")
        if event.get("schema_version") != SCHEMA_VERSION:
            raise LineageError("lineage ledger schema mismatch")
        events.append(event)
    return events


def _ledger_path(path: Path) -> Path:
    ledger = Path(path).expanduser()
    if not ledger.is_absolute():
        ledger = ledger.resolve()
    return ledger


@contextmanager
def _locked_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise LineageError("lineage ledger must be a real file")
    with path.open("a+b") as ledger:
        fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
        try:
            yield ledger
        finally:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)


def _read_locked_bytes(ledger: BinaryIO) -> bytes:
    ledger.seek(0)
    return ledger.read()


def _append_event(ledger: BinaryIO, event: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    ledger.seek(0, os.SEEK_END)
    ledger.write(encoded)
    ledger.flush()
    os.fsync(ledger.fileno())


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LineageError(field + " must be a non-empty string")
    return value.strip()


def _optional_nonempty_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _reservation_context(
    *,
    current_scope_id: Any,
    primary_mode: Any,
    owner_acceptance_ref: Any,
    owner_revision_ref: Any,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    scope_id = _optional_nonempty_string(current_scope_id, "current_scope_id")
    mode = _optional_nonempty_string(primary_mode, "primary_mode")
    acceptance_ref = _optional_nonempty_string(
        owner_acceptance_ref, "owner_acceptance_ref"
    )
    revision_ref = _optional_nonempty_string(
        owner_revision_ref, "owner_revision_ref"
    )
    if (scope_id is None) != (mode is None):
        raise LineageError(
            "current_scope_id and primary_mode must both be null or both be set"
        )
    if mode is not None and mode not in {"research", "product", "governance"}:
        raise LineageError("primary_mode must be research, product, or governance")
    if scope_id is None and (acceptance_ref is not None or revision_ref is not None):
        raise LineageError("Owner references require a current scope binding")
    if acceptance_ref is not None and mode != "product":
        raise LineageError("Owner acceptance requires product mode")
    return scope_id, mode, acceptance_ref, revision_ref


def _enforce_scope_invariants(
    state: Mapping[str, Any],
    *,
    current_scope_id: Optional[str],
    primary_mode: Optional[str],
    owner_acceptance_ref: Optional[str],
    owner_revision_ref: Optional[str],
    candidate_hash: str,
) -> None:
    if current_scope_id is None:
        return
    accepted_ref = state["owner_acceptance_refs"].get(current_scope_id)
    if accepted_ref is not None:
        if primary_mode != "product":
            raise LineageError("accepted scope must remain in product mode")
        if owner_acceptance_ref != accepted_ref:
            raise LineageError(
                "accepted scope cannot change its Owner acceptance reference"
            )
    reviewed_candidate = state["reviewed_candidate_hashes"].get(
        current_scope_id
    )
    if (
        reviewed_candidate is not None
        and reviewed_candidate != candidate_hash
        and owner_revision_ref is None
    ):
        raise LineageError(
            "post-review revision requires an Owner authorization reference"
        )


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LineageError(field + " must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LineageError(field + " must be a non-negative integer")
    return value


def _nonnegative_number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise LineageError(field + " must be a finite non-negative number")
    return value


def _sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LineageError(field + " must be 64 lowercase hex characters")
    return value


def _candidate_hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in (40, 64)
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LineageError(
            "candidate_hash must be 40 or 64 lowercase hex characters"
        )
    return value
