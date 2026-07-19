import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest

from development_governor.supervisor import supervise_root_process


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)

    def spawn(self, source, *arguments):
        process = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(source), *map(str, arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def supervise(self, process, changed_paths_probe, **overrides):
        arguments = {
            "raw_events_path": self.root / "raw-events.jsonl",
            "stderr_path": self.root / "stderr.txt",
            "changed_paths_probe": changed_paths_probe,
            "allowed_paths": ("src/", "README.md"),
            "product_paths": ("src/",),
            "max_elapsed_seconds": 3,
            "product_change_deadline_seconds": None,
            "max_observed_total_tokens": None,
            "token_usage_from_jsonl": token_usage_from_jsonl,
            "poll_interval_seconds": 0.005,
            "termination_grace_seconds": 0.1,
        }
        arguments.update(overrides)
        return supervise_root_process(process, **arguments)

    def test_docs_only_change_is_stopped_at_product_deadline(self):
        process = self.spawn(
            """
            import time
            time.sleep(10)
            """
        )

        result = self.supervise(
            process,
            lambda: ("README.md",),
            max_elapsed_seconds=2,
            product_change_deadline_seconds=0.05,
        )

        self.assertEqual(result.stop_reason, "product_change_deadline_exhausted")
        self.assertFalse(result.timed_out)
        self.assertEqual(result.changed_paths_at_stop, ("README.md",))
        self.assertEqual(result.outside_paths_at_stop, ())
        self.assertFalse(result.product_change_observed_at_deadline)
        self.assertFalse(result.stream_truncated)

    def test_product_deadline_takes_a_current_path_snapshot(self):
        process = self.spawn(
            """
            import time
            time.sleep(0.08)
            """
        )
        probe_calls = 0

        def changed_paths_probe():
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                return ()
            return ("src/product.py",)

        result = self.supervise(
            process,
            changed_paths_probe,
            max_elapsed_seconds=1,
            product_change_deadline_seconds=0.03,
            poll_interval_seconds=0.2,
        )

        self.assertIsNone(result.stop_reason)
        self.assertTrue(result.product_change_observed_at_deadline)
        self.assertEqual(result.changed_paths_at_stop, ("src/product.py",))

    def test_outside_path_snapshot_survives_process_cleanup(self):
        changed_file = self.root / "outside.txt"
        ready_file = self.root / "ready"
        process = self.spawn(
            """
            from pathlib import Path
            import signal
            import sys
            import time

            changed_file = Path(sys.argv[1])
            ready_file = Path(sys.argv[2])
            changed_file.write_text("forbidden", encoding="utf-8")

            def cleanup_and_exit(signum, frame):
                changed_file.unlink(missing_ok=True)
                raise SystemExit(9)

            signal.signal(signal.SIGTERM, cleanup_and_exit)
            ready_file.write_text("ready", encoding="utf-8")
            time.sleep(10)
            """,
            changed_file,
            ready_file,
        )

        def changed_paths_probe():
            if ready_file.exists() and changed_file.exists():
                return ("outside.txt", "src/allowed.py")
            return ()

        result = self.supervise(process, changed_paths_probe)

        self.assertEqual(result.stop_reason, "changed_path_outside_contract")
        self.assertEqual(
            result.changed_paths_at_stop,
            ("outside.txt", "src/allowed.py"),
        )
        self.assertEqual(result.outside_paths_at_stop, ("outside.txt",))
        self.assertFalse(changed_file.exists())
        self.assertEqual(process.returncode, 9)

    def test_observed_token_cap_stops_process(self):
        event = {
            "usage": {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
            }
        }
        encoded_event = json.dumps(event, separators=(",", ":")).encode() + b"\n"
        process = self.spawn(
            """
            import os
            import time
            os.write(1, bytes.fromhex(__import__("sys").argv[1]))
            time.sleep(10)
            """,
            encoded_event.hex(),
        )

        result = self.supervise(
            process,
            lambda: (),
            max_observed_total_tokens=90,
        )

        self.assertEqual(result.stop_reason, "observed_token_budget_exhausted")
        self.assertFalse(result.timed_out)
        self.assertEqual((self.root / "raw-events.jsonl").read_bytes(), encoded_event)

    def test_terminal_only_token_usage_is_accounting_not_a_live_stop(self):
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 80,
                "cached_input_tokens": 70,
                "output_tokens": 20,
                "total_tokens": 100,
            },
        }
        encoded_event = json.dumps(event, separators=(",", ":")).encode() + b"\n"
        process = self.spawn(
            """
            import os
            import time
            os.write(1, bytes.fromhex(__import__("sys").argv[1]))
            time.sleep(0.05)
            """,
            encoded_event.hex(),
        )

        result = self.supervise(
            process,
            lambda: (),
            max_observed_total_tokens=90,
        )

        self.assertIsNone(result.stop_reason)
        self.assertEqual(result.token_observability_mode, "terminal_only")
        self.assertTrue(result.token_budget_exceeded)
        self.assertTrue(result.completion_event_observed)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.root / "raw-events.jsonl").read_bytes(), encoded_event)

    def test_large_stdout_and_stderr_are_drained_without_deadlock(self):
        stdout_bytes = bytes(range(256)) * 4096
        stderr_bytes = bytes(reversed(range(256))) * 4096
        process = self.spawn(
            """
            import os

            stdout_bytes = bytes(range(256)) * 4096
            stderr_bytes = bytes(reversed(range(256))) * 4096
            chunk_size = 16384
            for offset in range(0, len(stdout_bytes), chunk_size):
                os.write(1, stdout_bytes[offset:offset + chunk_size])
                os.write(2, stderr_bytes[offset:offset + chunk_size])
            """
        )

        result = self.supervise(process, lambda: (), max_elapsed_seconds=5)

        self.assertIsNone(result.stop_reason)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.stream_truncated)
        self.assertEqual((self.root / "raw-events.jsonl").read_bytes(), stdout_bytes)
        self.assertEqual((self.root / "stderr.txt").read_bytes(), stderr_bytes)
        self.assertEqual(process.returncode, 0)

    def test_process_ignoring_sigterm_is_killed_after_grace(self):
        ready_file = self.root / "ignoring-term"
        process = self.spawn(
            """
            from pathlib import Path
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(sys.argv[1]).write_text("ready", encoding="utf-8")
            time.sleep(10)
            """,
            ready_file,
        )

        result = self.supervise(
            process,
            lambda: ("outside.txt",) if ready_file.exists() else (),
            termination_grace_seconds=0.05,
        )

        self.assertEqual(result.stop_reason, "changed_path_outside_contract")
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertGreaterEqual(result.elapsed_seconds, 0.05)

    def test_probe_failure_stops_process_fail_closed(self):
        process = self.spawn(
            """
            import time
            time.sleep(10)
            """
        )

        def failing_probe():
            raise RuntimeError("git probe unavailable")

        result = self.supervise(process, failing_probe)

        self.assertEqual(result.stop_reason, "supervision_probe_failed")
        self.assertFalse(result.timed_out)
        self.assertEqual(result.changed_paths_at_stop, ())
        self.assertEqual(result.outside_paths_at_stop, ())
        self.assertIsNotNone(process.returncode)

    def test_root_exit_terminates_lingering_process_group(self):
        ready_file = self.root / "orphan-ready"
        stopped_file = self.root / "orphan-stopped"
        process = self.spawn(
            """
            import subprocess
            import sys
            import time

            child = r'''
            from pathlib import Path
            import signal
            import sys
            import time

            ready = Path(sys.argv[1])
            stopped = Path(sys.argv[2])
            def stop(signum, frame):
                stopped.write_text("stopped", encoding="utf-8")
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            ready.write_text("ready", encoding="utf-8")
            time.sleep(1)
            '''
            subprocess.Popen([sys.executable, "-c", child, sys.argv[1], sys.argv[2]])
            while not __import__("pathlib").Path(sys.argv[1]).exists():
                time.sleep(0.005)
            """,
            ready_file,
            stopped_file,
        )

        result = self.supervise(
            process,
            lambda: (),
            termination_grace_seconds=0.1,
        )

        self.assertEqual(result.stop_reason, "orphan_process_group_detected")
        self.assertTrue(stopped_file.exists())
        self.assertEqual(process.returncode, 0)


def token_usage_from_jsonl(raw):
    observed = None
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = item.get("usage") if isinstance(item, dict) else None
        if isinstance(usage, dict) and "total_tokens" in usage:
            observed = usage
    if observed is None:
        return {"status": "unavailable"}
    return {"status": "observed", **observed}


if __name__ == "__main__":
    unittest.main()
