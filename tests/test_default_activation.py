import json
import os
from pathlib import Path
import tempfile
import unittest

from development_governor.default_activation import (
    ActivationError,
    AGENTS_BEGIN,
    AGENTS_END,
    default_disable,
    default_enable,
)


class DefaultActivationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        self.source_package = Path(__file__).resolve().parents[1] / "src" / "development_governor"
        self.governor_repo = Path(__file__).resolve().parents[1]
        self.agents_path = self.codex_home / "AGENTS.md"
        self.hooks_path = self.codex_home / "hooks.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def enable(self):
        return default_enable(
            codex_home=self.codex_home,
            source_package=self.source_package,
            governor_repo=self.governor_repo,
        )

    def test_enable_preserves_existing_content_installs_stable_runtime_and_is_idempotent(self):
        self.agents_path.write_text("# Existing rules\n\nKeep this.\n", encoding="utf-8")
        existing_hook = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/tmp/existing"}]}
                ]
            },
            "custom": {"preserve": True},
        }
        self.hooks_path.write_text(json.dumps(existing_hook), encoding="utf-8")

        enabled = self.enable()
        repeated = self.enable()

        agents = self.agents_path.read_text(encoding="utf-8")
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(enabled["status"], "enabled")
        self.assertEqual(repeated["status"], "already_enabled")
        self.assertIn("# Existing rules", agents)
        self.assertEqual(agents.count(AGENTS_BEGIN), 1)
        self.assertEqual(agents.count(AGENTS_END), 1)
        self.assertEqual(hooks["custom"], {"preserve": True})
        self.assertIn("PostToolUse", hooks["hooks"])
        self.assertEqual(len(hooks["hooks"]["PreToolUse"]), 1)
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(command, enabled["hook_command"])
        self.assertNotIn(".worktrees", command)
        self.assertTrue(Path(enabled["launcher_path"]).is_file())
        self.assertTrue(Path(enabled["manifest_path"]).is_file())
        self.assertEqual(os.stat(enabled["manifest_path"]).st_mode & 0o777, 0o600)

    def test_disable_removes_only_managed_content_after_unrelated_later_edits(self):
        self.agents_path.write_text("Original\n", encoding="utf-8")
        self.hooks_path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        self.enable()

        self.agents_path.write_text(
            self.agents_path.read_text(encoding="utf-8") + "Later owner rule\n",
            encoding="utf-8",
        )
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks["hooks"]["Stop"] = [
            {"hooks": [{"type": "command", "command": "/tmp/later"}]}
        ]
        self.hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

        disabled = default_disable(codex_home=self.codex_home)
        repeated = default_disable(codex_home=self.codex_home)

        agents = self.agents_path.read_text(encoding="utf-8")
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(repeated["status"], "already_disabled")
        self.assertIn("Original", agents)
        self.assertIn("Later owner rule", agents)
        self.assertNotIn(AGENTS_BEGIN, agents)
        self.assertNotIn("PreToolUse", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])

    def test_restore_backup_returns_exact_original_bytes_and_modes(self):
        original_agents = b"original agents\n"
        original_hooks = b'{"hooks":{}}\n'
        self.agents_path.write_bytes(original_agents)
        self.hooks_path.write_bytes(original_hooks)
        self.agents_path.chmod(0o640)
        self.hooks_path.chmod(0o600)
        self.enable()
        self.agents_path.write_text("owner changed after enable\n", encoding="utf-8")

        restored = default_disable(codex_home=self.codex_home, restore_backup=True)

        self.assertEqual(restored["status"], "disabled")
        self.assertEqual(self.agents_path.read_bytes(), original_agents)
        self.assertEqual(self.hooks_path.read_bytes(), original_hooks)
        self.assertEqual(self.agents_path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.hooks_path.stat().st_mode & 0o777, 0o600)

    def test_enable_rejects_invalid_json_and_broken_or_duplicate_markers(self):
        self.hooks_path.write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(ActivationError, "hooks.json is not valid JSON"):
            self.enable()

        self.hooks_path.unlink()
        for index, content in enumerate(
            (
                AGENTS_BEGIN + "\nbroken\n",
                AGENTS_BEGIN + "\none\n" + AGENTS_END + "\n" + AGENTS_BEGIN + "\ntwo\n" + AGENTS_END,
            )
        ):
            with self.subTest(index=index):
                self.agents_path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ActivationError, "managed AGENTS markers"):
                    self.enable()

    def test_disable_returns_owner_required_without_writing_ambiguous_files(self):
        self.agents_path.write_text("Original\n", encoding="utf-8")
        self.enable()
        broken = self.agents_path.read_text(encoding="utf-8").replace(AGENTS_END, "")
        self.agents_path.write_text(broken, encoding="utf-8")
        before_agents = self.agents_path.read_bytes()
        before_hooks = self.hooks_path.read_bytes()

        result = default_disable(codex_home=self.codex_home)

        self.assertEqual(result["status"], "owner_required")
        self.assertEqual(self.agents_path.read_bytes(), before_agents)
        self.assertEqual(self.hooks_path.read_bytes(), before_hooks)

    def test_disable_preserves_handler_added_to_the_managed_hook_group(self):
        self.enable()
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": "/tmp/later-handler"}
        )
        self.hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

        result = default_disable(codex_home=self.codex_home)

        self.assertEqual(result["status"], "disabled")
        remaining = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(
            remaining["hooks"]["PreToolUse"][0]["hooks"],
            [{"type": "command", "command": "/tmp/later-handler"}],
        )


if __name__ == "__main__":
    unittest.main()
