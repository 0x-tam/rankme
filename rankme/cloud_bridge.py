"""Outbound-only, journaled bridge from the local owner app to RankMe Cloud."""

import hashlib
import http.client
import json
import os
import re
import sqlite3
import stat
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .engine import safe_error


MAX_BODY = 2_000_000
ID = r"[a-f0-9]{32}"
SENSITIVE = re.compile(r"(?i)(secret|token|password|api.?key|authorization|credential|private.?key|codex.?path|project.?path|build.?command|deploy.?command|local.?path)")
URL_SECRET = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.I)
INLINE_SECRET = re.compile(r"(?i)\b(?:client[_-]?secret|refresh[_-]?token|access[_-]?token|api[_-]?key|authorization|password|private[_-]?key)\b\s*[:=]\s*[^\s,;]+")
LOCAL_PATH = re.compile(r"(?<![\w])/(?:Users|home|private|var|tmp)/[^\s,;]+")


def allowed_command(method, path, body):
    """Validate the entire command, including fields, before local dispatch."""
    if not isinstance(body, dict) or len(json.dumps(body)) > 100_000:
        return False
    def has_sensitive_key(value):
        if isinstance(value, dict):
            return any(not isinstance(key, str) or SENSITIVE.search(key) or has_sensitive_key(item)
                       for key, item in value.items())
        return isinstance(value, list) and any(has_sensitive_key(item) for item in value)
    if has_sensitive_key(body):
        return False
    fields = set(body)
    if method == "POST" and path == "/api/clients":
        return fields <= {"url", "name"} and isinstance(body.get("url"), str)
    if method == "PATCH" and re.fullmatch(rf"/api/clients/{ID}", path):
        if not fields or not fields <= {"name", "subject", "profile", "confirmed", "automation", "next_run", "seo_connection", "visibility_settings", "image_brand", "conversion_goal"}:
            return False
        if "profile" in body and (not isinstance(body["profile"], dict) or any(SENSITIVE.search(k) for k in body["profile"])):
            return False
        if "seo_connection" in body and (not isinstance(body["seo_connection"], dict) or set(body["seo_connection"]) - {"site_url", "ga4_property", "auto_sync"}):
            return False
        if "visibility_settings" in body and (not isinstance(body["visibility_settings"], dict) or set(body["visibility_settings"]) - {"enabled", "interval_days", "max_actions", "auto_plan", "auto_refresh", "auto_research", "auto_probe"}):
            return False
        return True
    if method == "PATCH" and re.fullmatch(rf"/api/articles/{ID}", path):
        return bool(fields) and fields <= {"body", "title", "description", "scheduled_at"}
    if method == "PATCH" and re.fullmatch(rf"/api/backlinks/{ID}", path):
        return bool(fields) and fields <= {"status", "notes"}
    if method == "PATCH" and re.fullmatch(rf"/api/opportunities/{ID}", path):
        return fields == {"status"} and body["status"] in ("open", "dismissed")
    if method == "PATCH" and path == "/api/settings":
        return bool(fields) and fields <= {"paused", "model", "max_pages"}
    if method != "POST":
        return False
    patterns = (
        (rf"/api/clients/{ID}/(?:inspect|run|visibility-audit|visibility-research|visibility-probe|seo-sync|backlinks-discover)", set()),
        (rf"/api/clients/{ID}/plan", {"subject"}),
        (rf"/api/clients/{ID}/backlinks", {"source_url", "notes"}),
        (rf"/api/clients/{ID}/experiments", {"page_url", "hypothesis", "change"}),
        (rf"/api/articles/{ID}/(?:generate|review|cover|publish|verify|remove|remove-cover)", set()),
        (rf"/api/backlinks/{ID}/check", set()),
        (rf"/api/opportunities/{ID}/execute", set()),
        (rf"/api/experiments/{ID}/cancel", set()),
        (rf"/api/jobs/{ID}/(?:retry|stop|dismiss)", set()),
    )
    # Removing a website needs the typed address; the Mac checks it again before deleting anything.
    if re.fullmatch(rf"/api/clients/{ID}/remove", path):
        return fields == {"confirm"} and isinstance(body["confirm"], str) and 0 < len(body["confirm"]) <= 300
    return any(re.fullmatch(pattern, path) and fields <= permitted for pattern, permitted in patterns)


def sanitize_snapshot(value):
    """Remove local execution details and credential-shaped data recursively."""
    if isinstance(value, dict):
        output = {key: sanitize_snapshot(item) for key, item in value.items()
                  if isinstance(key, str) and not SENSITIVE.search(key) and key != "publish_result"}
        if "connection" in output and isinstance(output["connection"], dict):
            output["connection"] = {key: output["connection"][key] for key in ("mode", "format", "auto_publish", "content_dir", "public_url_template") if key in output["connection"]}
        return output
    if isinstance(value, list):
        return [sanitize_snapshot(item) for item in value]
    if isinstance(value, str):
        cleaned = URL_SECRET.sub(r"\1[redacted]@", value)
        cleaned = INLINE_SECRET.sub("[redacted]", cleaned)
        return LOCAL_PATH.sub("[local path]", cleaned)[:500_000]
    return value if value is None or isinstance(value, (int, float, bool)) else None


def load_config(config_path):
    path = Path(config_path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Cloud worker config must be an owner-only regular file")
        if info.st_size > 4096:
            raise ValueError("Cloud worker config is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            data = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(data, dict) or set(data) != {"origin", "token"}:
        raise ValueError("Cloud worker config requires origin and token")
    parsed = urlsplit(data["origin"])
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not isinstance(data["token"], str) or not 32 <= len(data["token"]) <= 512):
        raise ValueError("Cloud worker config requires a plain HTTPS origin and token")
    return parsed.hostname, data["token"]


class CloudWorker:
    def __init__(self, app, config_path, on_owner_enrolled=None, interval=15):
        self.app = app
        self.host, self.token = load_config(config_path)
        self.on_owner_enrolled = on_owner_enrolled
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.journal = sqlite3.connect(str(app.data_dir / "cloud-bridge.sqlite3"), check_same_thread=False)
        self.journal.execute("PRAGMA journal_mode=WAL")
        self.journal.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, acknowledged INTEGER NOT NULL DEFAULT 0)")
        self.journal.execute("UPDATE commands SET status='needs_review',result=? WHERE status='started'",
                             (json.dumps({"message": "Execution state ambiguous after restart; review locally."}),))
        self.journal.commit()
        self.lock = threading.Lock()
        self.enrollment_seen = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="rankme-cloud-bridge", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                return
        with self.lock:
            self.journal.close()

    def _request(self, path, body):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_BODY:
            raise ValueError("Cloud bridge payload too large")
        conn = http.client.HTTPSConnection(self.host, timeout=15)
        try:
            conn.request("POST", path, raw, {"Authorization": "Bearer " + self.token,
                       "Content-Type": "application/json", "Content-Length": str(len(raw))})
            response = conn.getresponse()
            if response.status != 200:  # Redirects are never followed with a bearer credential.
                raise RuntimeError("Cloud bridge returned HTTP " + str(response.status))
            length = response.getheader("Content-Length")
            if length and int(length) > MAX_BODY:
                raise ValueError("Cloud bridge response too large")
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError("Cloud bridge response too large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Cloud bridge response must be an object")
            return result
        finally:
            conn.close()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                # The bridge remains offline; avoid recording network failures in events.
                pass
            self.stop_event.wait(self.interval)

    def poll_once(self):
        self._flush_results()
        snapshot = sanitize_snapshot(self.app.engine.state())
        reply = self._request("/api/worker/poll", {"snapshot": snapshot})
        if reply.get("owner_enrolled") is True and not self.enrollment_seen:
            if self.on_owner_enrolled:
                self.on_owner_enrolled()
            self.enrollment_seen = True
        command = reply.get("command")
        if command:
            if reply.get("owner_enrolled") is not True:
                raise ValueError("Cloud commands require an enrolled owner")
            self.handle_command(command)

    def handle_command(self, command):
        if not self.enrollment_seen:
            raise ValueError("Cloud commands require an enrolled owner")
        if not isinstance(command, dict):
            raise ValueError("Invalid cloud command")
        ident, method, path, body = (command.get(key) for key in ("id", "method", "path", "body"))
        if not isinstance(ident, str) or not re.fullmatch(ID, ident) or not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("Invalid cloud command")
        canonical = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock:
            existing = self.journal.execute("SELECT digest,status,result FROM commands WHERE id=?", (ident,)).fetchone()
            if existing is None:
                self.journal.execute("INSERT INTO commands(id,digest,status) VALUES (?,?,?)", (ident, digest, "started"))
                self.journal.commit()  # Durable before any local side effect.
            elif existing[0] != digest:
                raise ValueError("Cloud command ID reused with different payload")
        if existing:
            status = existing[1]
            result = json.loads(existing[2]) if existing[2] else {"message": "Execution state ambiguous after restart; review locally."}
            if status == "started":
                status = "needs_review"
                with self.lock:
                    self.journal.execute("UPDATE commands SET status=?,result=? WHERE id=?", (status, json.dumps(result), ident))
                    self.journal.commit()
        else:
            try:
                if not allowed_command(method, path, body):
                    raise ValueError("Cloud command is not allowed")
                result = {"data": sanitize_snapshot(self.app.dispatch(method, path, body))}
                status = "succeeded"
            except Exception as exc:
                result = {"error": sanitize_snapshot(safe_error(exc))}
                status = "failed"
            with self.lock:
                self.journal.execute("UPDATE commands SET status=?,result=? WHERE id=?", (status, json.dumps(result), ident))
                self.journal.commit()  # Durable before acknowledgment.
        self._acknowledge(ident, status, result)

    def _acknowledge(self, ident, status, result):
        self._request("/api/worker/result", {"id": ident, "status": status, "result": result})
        with self.lock:
            self.journal.execute("UPDATE commands SET acknowledged=1 WHERE id=?", (ident,))
            self.journal.commit()

    def _flush_results(self):
        with self.lock:
            pending = self.journal.execute(
                "SELECT id,status,result FROM commands WHERE acknowledged=0 AND status!='started' LIMIT 20").fetchall()
        for ident, status, payload in pending:
            self._acknowledge(ident, status, json.loads(payload))
