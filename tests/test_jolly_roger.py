"""Real subprocess conformance from only the manifest's packaged files."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "plugins/codex-session-logging"


class ManagedBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.manifest = json.loads((SOURCE / ".jollyroger.json").read_text())
        for entry in self.manifest["include"]:
            src, dst = SOURCE / entry, self.bundle / entry
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex = self.home / ".codex"
        self.claude = self.home / ".claude"
        for directory in (self.codex, self.claude):
            directory.mkdir()
            (directory / "settings.json").write_text('{"unrelated":true,"trusted":"retain"}')
        self.maps = {key: str(self.root / "legacy" / key) for key in (
            "logger-state", "logger-preferences", "forum-feedback"
        )}
        for directory in self.maps.values():
            Path(directory).mkdir(parents=True)
        self.preference = Path(self.maps["logger-preferences"]) / "preferences.json"
        self.preference.write_text('{"enabled":false}\n')
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/e3-solutions/conformance"], check=True)
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.home),
            "CODEX_HOME": str(self.codex), "CLAUDE_CONFIG_DIR": str(self.claude),
            "JOLLY_ROGER_MANAGED": "1",
            "JOLLY_ROGER_COMPONENT_ID": "codex-session-logging",
            "JOLLY_ROGER_PROVIDER": "codex",
            "JOLLY_ROGER_STATE_DIR": str(self.root / "default-state"),
            "JOLLY_ROGER_STATE_PATHS": json.dumps(self.maps),
            # Any accidental upload can only reach a local deliberately closed port.
            "CODEX_SESSION_LOG_INGEST_URL": "http://127.0.0.1:1/blocked",
        }
        poison = self.root / "poison"
        poison.mkdir()
        (poison / "sitecustomize.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(self.root / "import-poison")) + ").touch()\n")
        (poison / "session_logging.py").write_text('raise RuntimeError("wrong source")\n')
        self.env["PYTHONPATH"] = str(poison)
        self.before = self.snapshot(self.home) | self.snapshot(self.bundle)

    def snapshot(self, root):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file()}

    def invoke(self, event="SessionStart", provider="codex", guidance=False,
               payload=None, env=None, raw=None):
        if payload is None:
            payload = {"session_id": "managed-test", "source": "startup",
                       "cwd": str(self.repo), "tool_name": "exec_command",
                       "tool_input": {"cmd": "echo example"}, "prompt": "fixture text"}
        envelope = {"schema_version": 1, "provider": provider, "event": event, "payload": payload}
        variables = dict(self.env, JOLLY_ROGER_PROVIDER=provider)
        variables.update(env or {})
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.bundle / "scripts" / (
                "jolly_roger_guidance.py" if guidance else "jolly_roger.py"))],
            input=raw if raw is not None else json.dumps(envelope), text=True,
            capture_output=True, env=variables, cwd=self.root, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertLessEqual(len(result.stdout.encode()), 65536)
        self.assertTrue(set(response) <= {"status", "context"})
        self.assertEqual(self.snapshot(self.home) | self.snapshot(self.bundle), self.before)
        self.assertFalse((self.root / "default-state").exists())
        self.assertFalse((self.root / "import-poison").exists())
        return response, result

    def test_every_declared_provider_event_from_copied_bundle(self):
        for hook in self.manifest["hooks"]:
            for provider in hook["providers"]:
                with self.subTest(event=hook["event"], provider=provider):
                    response, _ = self.invoke(hook["event"], provider,
                        guidance=hook["id"] == "forum-sesh-guidance")
                    self.assertEqual(response["status"], "ok")
                    if hook["id"] != "forum-sesh-guidance":
                        self.assertNotIn("context", response)

    def test_guidance_both_providers_does_not_capture_and_preserves_debounce(self):
        for provider in ("codex", "claude"):
            first, _ = self.invoke(provider=provider, guidance=True)
            self.assertIn("Sesh prior-work", first["context"])
            self.assertIn("E3 Collective / Forum", first["context"])
            second, _ = self.invoke(provider=provider, guidance=True)
            self.assertNotIn("E3 Collective / Forum", second.get("context", ""))
        self.assertEqual(list(Path(self.maps["logger-state"]).iterdir()), [])

    def test_guidance_optouts_and_missing_fields(self):
        response, _ = self.invoke(guidance=True, env={
            "E3_SESH_CONTEXT_ENABLED": "off", "E3_COLLECTIVE_HOOK_ENABLED": "off"})
        self.assertNotIn("context", response)
        for hook in self.manifest["hooks"]:
            response, _ = self.invoke(event=hook["event"], payload={},
                guidance=hook["id"] == "forum-sesh-guidance")
            self.assertEqual(response["status"], "ok")

    def test_explicit_state_preserves_pending_records_and_upload_optout(self):
        base = Path(self.maps["logger-state"])
        pending = base / "queue/pending"
        pending.mkdir(parents=True)
        old = pending / "old.json"
        old.write_text('{"id":"old","sentinel":"retained"}\n')
        checkpoint = base / "sessions/managed-test/sequence.txt"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text("41\n")
        for _ in range(2):
            self.invoke(event="PreToolUse")
        self.assertEqual(old.read_text(), '{"id":"old","sentinel":"retained"}\n')
        self.assertEqual(checkpoint.read_text(), "43\n")
        self.assertEqual(self.preference.read_text(), '{"enabled":false}\n')
        self.assertEqual(len(list(pending.glob("*.json"))), 3)
        self.assertFalse((base / "queue/drain.lock").exists(), "opt-out must not spawn uploader")

    def test_invalid_envelopes_and_mappings_skip_without_writes(self):
        before = self.snapshot(self.root / "legacy")
        cases = [({"raw": "not JSON"}), ({"raw": "[]"}),
                 ({"raw": "x" * (4 * 1024 * 1024 + 1)}),
                 ({"provider": "claude"}), ({"event": "SessionEnd"}),
                 ({"env": {"JOLLY_ROGER_STATE_PATHS": "{}"}}),
                 ({"env": {"JOLLY_ROGER_MANAGED": "0"}})]
        for case in cases:
            with self.subTest(case=str(case)[:80]):
                response, result = self.invoke(**case)
                self.assertEqual(response, {"status": "skipped"})
                self.assertNotIn("fixture text", result.stderr)
        self.assertEqual(self.snapshot(self.root / "legacy"), before)

    def test_detached_existing_drain_cannot_take_owned_queue(self):
        base = Path(self.maps["logger-state"])
        lock = base / "queue/drain.lock"
        lock.parent.mkdir(parents=True)
        with lock.open("w") as owned:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Parent hook exits successfully after durable enqueue. Existing
            # worker is lock-excluded from consuming any newly queued record.
            for _ in range(3):
                response, _ = self.invoke(event="PreToolUse", env={"CODEX_SESSION_LOG_AUTO_UPLOAD": "1"})
                self.assertEqual(response["status"], "ok")
            # Exercise actual worker's public entrypoint under the same lock,
            # proving lock contention exits instead of creating another drainer.
            result = subprocess.run([sys.executable, "-I", "-B", str(self.bundle / "scripts/drain_queue.py")],
                env=dict(self.env, CODEX_SESSION_LOG_STATE_DIR=str(base)),
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["locked"])
            self.assertEqual(len(list((base / "queue/pending").glob("*.json"))), 3)


if __name__ == "__main__":
    unittest.main()
