import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rankme.cloud_bridge import CloudWorker, allowed_command, load_config, sanitize_snapshot


class FakeEngine:
    def state(self):
        return {"clients": [{"id": "a" * 32, "connection": {"mode": "git", "project_path": "/Users/owner/site",
                 "build_command": ["npm", "build"], "auto_publish": True}}],
                "settings": {"codex_path": "/Users/owner/bin/codex", "paused": False},
                "events": [{"message": "client_secret=hidden"}]}


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.engine = FakeEngine()
        self.calls = []

    def dispatch(self, method, path, body):
        self.calls.append((method, path, body))
        return {"id": "b" * 32, "connection": {"project_path": "/Users/owner/site"}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "worker.json"
        self.config.write_text(json.dumps({"origin": "https://example.test", "token": "x" * 40}))
        self.config.chmod(0o600)
        self.app = FakeApp(self.temp.name)

    def test_config_requires_owner_only_regular_file(self):
        self.assertEqual(load_config(self.config)[0], "example.test")
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            load_config(self.config)
        self.config.chmod(0o600)
        self.config.write_text(json.dumps({"origin": "http://example.test", "token": "x" * 40}))
        with self.assertRaises(ValueError):
            load_config(self.config)
        self.config.write_text(json.dumps({"origin": "https://example.test", "token": "x" * 40}))
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.config)
        with self.assertRaises(OSError):
            load_config(link)

    def test_snapshot_redacts_execution_and_secrets(self):
        snapshot = sanitize_snapshot(self.app.engine.state())
        encoded = json.dumps(snapshot)
        self.assertIn('"client', encoded)
        self.assertIn('"auto_publish": true', encoded)
        for secret in ("hidden", "project_path", "build_command", "codex_path", "/Users/owner"):
            self.assertNotIn(secret, encoded)

    def test_allowlist_rejects_execution_controls(self):
        ident = "a" * 32
        self.assertTrue(allowed_command("POST", f"/api/articles/{ident}/publish", {}))
        self.assertTrue(allowed_command("PATCH", "/api/settings", {"paused": True}))
        self.assertFalse(allowed_command("PATCH", "/api/settings", {"codex_path": "/tmp/tool"}))
        self.assertFalse(allowed_command("PATCH", f"/api/clients/{ident}", {"connection": {"deploy_command": ["sh"]}}))
        self.assertFalse(allowed_command("POST", f"/api/articles/{ident}/publish", {"force": True}))
        self.assertFalse(allowed_command("POST", "/api/google/configure", {"client_secret": "x"}))

    def test_command_journal_prevents_reexecution_and_retries_ack(self):
        worker = CloudWorker(self.app, self.config)
        self.addCleanup(worker.stop)
        worker.enrollment_seen = True
        command = {"id": "a" * 32, "method": "POST", "path": "/api/clients", "body": {"url": "https://example.com"}}
        replies = []
        worker._request = lambda path, body: replies.append((path, body)) or {"ok": True}
        worker.handle_command(command)
        worker.handle_command(command)
        self.assertEqual(len(self.app.calls), 1)
        self.assertEqual(replies[0][1]["status"], "succeeded")
        self.assertEqual(replies[1][1]["status"], "succeeded")

    def test_ambiguous_restart_marks_review_without_reexecution(self):
        command = {"id": "a" * 32, "method": "POST", "path": "/api/clients", "body": {"url": "https://example.com"}}
        worker = CloudWorker(self.app, self.config)
        worker.enrollment_seen = True
        worker.journal.execute("INSERT INTO commands(id,digest,status) VALUES (?,?,?)", (command["id"], "old", "started"))
        worker.journal.commit()
        worker.stop()
        restarted = CloudWorker(self.app, self.config)
        self.addCleanup(restarted.stop)
        recorded = []
        restarted._request = lambda path, body: recorded.append(body) or {"ok": True}
        restarted._flush_results()
        self.assertEqual(recorded[0]["status"], "needs_review")
        self.assertEqual(self.app.calls, [])

    def test_poll_requires_enrollment_before_dispatch(self):
        worker = CloudWorker(self.app, self.config)
        self.addCleanup(worker.stop)
        command = {"id": "a" * 32, "method": "POST", "path": "/api/clients", "body": {"url": "https://example.com"}}
        worker._request = lambda path, body: {"owner_enrolled": False, "command": command}
        with self.assertRaises(ValueError):
            worker.poll_once()
        self.assertEqual(self.app.calls, [])


if __name__ == "__main__":
    unittest.main()
