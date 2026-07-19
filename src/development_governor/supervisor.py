"""Online supervision for one binary-mode root process and its process group."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import signal
import time
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SupervisionResult:
    stop_reason: Optional[str]
    timed_out: bool
    elapsed_seconds: float
    changed_paths_at_stop: Tuple[str, ...]
    outside_paths_at_stop: Tuple[str, ...]
    product_change_observed_at_deadline: bool
    stream_truncated: bool
    token_observability_mode: str
    token_budget_exceeded: bool
    completion_event_observed: bool


def supervise_root_process(
    process,
    *,
    raw_events_path: Path,
    stderr_path: Path,
    changed_paths_probe: Callable[[], Sequence[str]],
    allowed_paths: Sequence[str],
    product_paths: Sequence[str],
    max_elapsed_seconds: int,
    product_change_deadline_seconds: Optional[int],
    max_observed_total_tokens: Optional[int],
    token_usage_from_jsonl: Callable[[str], Mapping[str, Any]],
    poll_interval_seconds: float = 0.1,
    termination_grace_seconds: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
) -> SupervisionResult:
    """Drain, observe, and if necessary stop a caller-created root process."""

    if process.stdout is None or process.stderr is None:
        raise ValueError("process stdout and stderr must both be PIPE")

    raw_events_path = Path(raw_events_path)
    stderr_path = Path(stderr_path)
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = clock()
    next_probe_at = started_at
    latest_paths: Tuple[str, ...] = ()
    latest_outside: Tuple[str, ...] = ()
    stopped_paths: Optional[Tuple[str, ...]] = None
    stopped_outside: Optional[Tuple[str, ...]] = None
    stop_reason: Optional[str] = None
    timed_out = False
    product_deadline_checked = product_change_deadline_seconds is None
    product_change_observed_at_deadline = False
    term_sent_at: Optional[float] = None
    kill_attempted = False
    root_exit_seen_at: Optional[float] = None
    final_probe_done = False
    stream_truncated = False
    token_observability_mode = "unavailable"
    token_budget_exceeded = False
    completion_event_observed = False
    stdout_observed = bytearray()
    token_checked_length = 0

    selector = selectors.DefaultSelector()
    streams = {}

    def close_stream(file_descriptor: int) -> None:
        stream = streams.pop(file_descriptor, None)
        if stream is None:
            return
        try:
            selector.unregister(file_descriptor)
        except (KeyError, ValueError):
            pass
        stream.close()

    def request_stop(reason: str, now: float, *, timeout: bool = False) -> None:
        nonlocal stop_reason, timed_out, stopped_paths, stopped_outside
        nonlocal term_sent_at
        if stop_reason is not None:
            return
        stop_reason = reason
        timed_out = timeout
        stopped_paths = latest_paths
        stopped_outside = latest_outside
        term_sent_at = now
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def drain(events) -> None:
        for key, _ in events:
            file_descriptor = key.fd
            target, capture_stdout = key.data
            try:
                chunk = os.read(file_descriptor, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                close_stream(file_descriptor)
                continue
            target.write(chunk)
            if capture_stdout:
                stdout_observed.extend(chunk)

    try:
        with raw_events_path.open("wb") as raw_events, stderr_path.open("wb") as errors:
            for stream, target, capture_stdout in (
                (process.stdout, raw_events, max_observed_total_tokens is not None),
                (process.stderr, errors, False),
            ):
                file_descriptor = stream.fileno()
                os.set_blocking(file_descriptor, False)
                streams[file_descriptor] = stream
                selector.register(
                    file_descriptor,
                    selectors.EVENT_READ,
                    (target, capture_stdout),
                )

            while True:
                now = clock()
                process_exited = process.poll() is not None
                if process_exited and root_exit_seen_at is None:
                    root_exit_seen_at = now
                if (
                    process_exited
                    and stop_reason is None
                    and root_exit_seen_at is not None
                    and now - root_exit_seen_at >= poll_interval_seconds
                    and _process_group_exists(process.pid)
                ):
                    request_stop("orphan_process_group_detected", now)

                deadline_probe_due = (
                    not product_deadline_checked
                    and product_change_deadline_seconds is not None
                    and now - started_at >= product_change_deadline_seconds
                )
                final_probe_due = process_exited and not final_probe_done
                if stop_reason is None and (
                    now >= next_probe_at or deadline_probe_due or final_probe_due
                ):
                    try:
                        latest_paths = tuple(str(path) for path in changed_paths_probe())
                    except Exception:
                        request_stop("supervision_probe_failed", clock())
                    else:
                        latest_outside = tuple(
                            path
                            for path in latest_paths
                            if not _matches_any(path, allowed_paths)
                        )
                        next_probe_at = clock() + poll_interval_seconds
                        if latest_outside:
                            request_stop("changed_path_outside_contract", clock())
                    if final_probe_due:
                        final_probe_done = True

                drain(selector.select(0))

                if (
                    stop_reason is None
                    and max_observed_total_tokens is not None
                    and len(stdout_observed) != token_checked_length
                ):
                    token_checked_length = len(stdout_observed)
                    observation = codex_stream_observation(
                        stdout_observed.decode("utf-8", errors="replace")
                    )
                    token_observability_mode = observation[
                        "token_observability_mode"
                    ]
                    completion_event_observed = observation[
                        "completion_event_observed"
                    ]
                    try:
                        token_usage = token_usage_from_jsonl(
                            stdout_observed.decode("utf-8", errors="replace")
                        )
                    except Exception:
                        request_stop("supervision_probe_failed", clock())
                    else:
                        total_tokens = token_usage.get("total_tokens")
                        if (
                            isinstance(total_tokens, (int, float))
                            and not isinstance(total_tokens, bool)
                            and total_tokens >= max_observed_total_tokens
                        ):
                            token_budget_exceeded = True
                            if token_observability_mode == "streaming":
                                request_stop(
                                    "observed_token_budget_exhausted", clock()
                                )

                now = clock()
                elapsed = now - started_at
                if stop_reason is None and elapsed >= max_elapsed_seconds:
                    request_stop("elapsed_budget_exhausted", now, timeout=True)
                elif (
                    stop_reason is None
                    and not product_deadline_checked
                    and product_change_deadline_seconds is not None
                    and elapsed >= product_change_deadline_seconds
                ):
                    product_deadline_checked = True
                    product_change_observed_at_deadline = _matches_any(
                        latest_paths, product_paths
                    )
                    if not product_change_observed_at_deadline:
                        request_stop("product_change_deadline_exhausted", now)

                if (
                    term_sent_at is not None
                    and not kill_attempted
                    and now - term_sent_at >= termination_grace_seconds
                    and (
                        process.poll() is None
                        or streams
                        or _process_group_exists(process.pid)
                    )
                ):
                    kill_attempted = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

                if process.poll() is not None:
                    if root_exit_seen_at is None:
                        root_exit_seen_at = now
                    group_exists = _process_group_exists(process.pid)
                    if not streams and not group_exists:
                        break
                    if now - root_exit_seen_at >= termination_grace_seconds:
                        stream_truncated = True
                        for file_descriptor in tuple(streams):
                            close_stream(file_descriptor)
                    if (
                        kill_attempted
                        and now - root_exit_seen_at
                        >= termination_grace_seconds * 2
                    ):
                        break

                waits = [poll_interval_seconds]
                if stop_reason is None:
                    waits.append(max_elapsed_seconds - (now - started_at))
                    waits.append(next_probe_at - now)
                    if (
                        not product_deadline_checked
                        and product_change_deadline_seconds is not None
                    ):
                        waits.append(
                            product_change_deadline_seconds - (now - started_at)
                        )
                elif term_sent_at is not None and not kill_attempted:
                    waits.append(
                        termination_grace_seconds - (now - term_sent_at)
                    )
                if root_exit_seen_at is not None and streams:
                    waits.append(
                        termination_grace_seconds - (now - root_exit_seen_at)
                    )
                timeout = max(0.0, min(waits))
                drain(selector.select(timeout))
    finally:
        for file_descriptor in tuple(streams):
            close_stream(file_descriptor)
        selector.close()

    elapsed_seconds = max(0.0, clock() - started_at)
    return SupervisionResult(
        stop_reason=stop_reason,
        timed_out=timed_out,
        elapsed_seconds=elapsed_seconds,
        changed_paths_at_stop=(
            stopped_paths if stopped_paths is not None else latest_paths
        ),
        outside_paths_at_stop=(
            stopped_outside if stopped_outside is not None else latest_outside
        ),
        product_change_observed_at_deadline=(
            product_change_observed_at_deadline
        ),
        stream_truncated=stream_truncated,
        token_observability_mode=token_observability_mode,
        token_budget_exceeded=token_budget_exceeded,
        completion_event_observed=completion_event_observed,
    )


def codex_stream_observation(raw: str) -> Mapping[str, Any]:
    """Classify whether Codex token usage was live or terminal-only."""

    streaming_usage = False
    terminal_usage = False
    completion_event_observed = False
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "turn.completed":
            completion_event_observed = True
        payload = item.get("payload")
        if (
            item_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            streaming_usage = True
        if isinstance(item.get("usage"), dict):
            if item_type == "turn.completed":
                terminal_usage = True
            else:
                streaming_usage = True
    if streaming_usage:
        mode = "streaming"
    elif terminal_usage:
        mode = "terminal_only"
    else:
        mode = "unavailable"
    return {
        "token_observability_mode": mode,
        "completion_event_observed": completion_event_observed,
    }


def _matches_any(paths, prefixes: Sequence[str]) -> bool:
    if isinstance(paths, str):
        return any(_path_matches(paths, prefix) for prefix in prefixes)
    return any(
        _path_matches(path, prefix)
        for path in paths
        for prefix in prefixes
    )


def _path_matches(path: str, prefix: str) -> bool:
    normalized_path = path.replace("\\", "/").rstrip("/")
    normalized_prefix = prefix.replace("\\", "/").rstrip("/")
    return normalized_path == normalized_prefix or normalized_path.startswith(
        normalized_prefix + "/"
    )


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
