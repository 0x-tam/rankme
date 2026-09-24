"""Loopback-only web application. No account, API key, or cloud database required."""
import argparse
import fcntl
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

from .store import Store, now, uid
from .engine import Engine, first_run, parse_date, safe_error, brand_digest

ROOT = Path(__file__).resolve().parent.parent


class Application:
    def __init__(self, data_dir, engine_factory=Engine):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.store = Store(self.data_dir / "rankme.sqlite3")
        self.engine = engine_factory(self.store, self.data_dir)
        self.token = secrets.token_urlsafe(32)
        self.oauth_redirect = "http://127.0.0.1:8787/api/google/callback"

    def busy(self, client_id):
        return any(j["client_id"] == client_id and j["status"] in ("queued", "running") for j in self.store.all("jobs"))

    def create_client(self, data):
        from .crawler import normalize_url
        url = normalize_url(str(data.get("url", "")).strip())
        if any(c["url"].rstrip("/") == url.rstrip("/") for c in self.store.all("clients")):
            raise ValueError("This website is already in RankMe")
        client = self.store.put("clients", {"id": uid(), "url": url, "name": str(data.get("name") or urlparse(url).hostname)[:200],
                                            "status": "new", "profile": {}, "subject": "", "confirmed": False,
                                            "automation": False, "next_run": first_run(), "error": "",
                                            "visibility_settings": {"enabled": True, "interval_days": 7, "max_actions": 1,
                                                "auto_plan": False, "auto_refresh": False, "auto_research": False},
                                            "connection": {"mode": "export", "format": "md", "content_dir": "content/blog",
                                                           "auto_publish": False, "remote": "origin", "branch": "",
                                                           "project_path": "", "build_command": [], "deploy_command": [],
                                                           "public_url_template": ""}})
        self.engine.queue("inspect", client["id"])
        return client

    def update_client(self, ident, data):
        with self.engine.guard:
            return self._update_client(ident, data)

    def _update_client(self, ident, data):
        client = self.store.get("clients", ident)
        # Pause is always available; other configuration must not change mid-job.
        visibility_pause = set(data) == {"visibility_settings"} and data["visibility_settings"] == {"enabled": False}
        if self.busy(ident) and any(k != "automation" for k in data) and not visibility_pause:
            raise ValueError("Wait for the current job to finish before changing this client")
        fields = {}
        if "conversion_goal" in data:
            from .experiments import validate_goal
            fields["conversion_goal"] = validate_goal(data["conversion_goal"], client["url"])
        if "conversion_goal" in data or "seo_connection" in data:
            from .measurement import active_for
            if active_for(self.store, ident):
                raise ValueError("Finish or cancel active experiments before changing their goal or Google property")
        if "visibility_settings" in data:
            from .autopilot import settings
            value = data["visibility_settings"]
            if not isinstance(value, dict):
                raise ValueError("Visibility settings must be an object")
            config = settings({**client.get("visibility_settings", {}), **value})
            if any(config[k] for k in ("auto_plan", "auto_refresh", "auto_research", "auto_probe")) and not client.get("confirmed"):
                raise ValueError("Confirm the company profile before enabling automatic research or actions")
            fields["visibility_settings"] = config
        for key in ("name", "subject"):
            if key in data:
                fields[key] = str(data[key]).strip()[:2000]
        if "seo_connection" in data:
            connection = data["seo_connection"]
            if not isinstance(connection, dict):
                raise ValueError("SEO connection must be an object")
            site = str(connection.get("site_url", "")).strip()
            ga = str(connection.get("ga4_property", "")).strip()
            if site and not (re.fullmatch(r"sc-domain:[a-zA-Z0-9.-]+", site) or (urlparse(site).scheme in ("http", "https") and urlparse(site).hostname and not urlparse(site).username)):
                raise ValueError("Choose a valid Search Console property")
            if ga and not re.fullmatch(r"(?:properties/)?[0-9]+", ga):
                raise ValueError("Enter a numeric GA4 property ID")
            fields["seo_connection"] = {"site_url": site[:2000], "ga4_property": ga, "auto_sync": bool(connection.get("auto_sync", True))}
            if fields["seo_connection"] != client.get("seo_connection"):
                fields["seo_attempt_at"] = ""
        if "image_brand" in data:
            from .brand import normalize_brand
            fields["image_brand"] = normalize_brand(data["image_brand"])
        if "profile" in data:
            if not isinstance(data["profile"], dict):
                raise ValueError("Business profile must be an object")
            fields["profile"] = data["profile"]
            fields["confirmed"] = False
        if "confirmed" in data:
            if data["confirmed"] and not fields.get("profile", client.get("profile")):
                raise ValueError("Inspect the website before confirming its profile")
            fields["confirmed"] = bool(data["confirmed"])
        if "next_run" in data:
            fields["next_run"] = parse_date(data["next_run"]).isoformat()
        if "connection" in data:
            if not isinstance(data["connection"], dict):
                raise ValueError("Publishing connection must be an object")
            from .publisher import validate_connection
            connection = {**client.get("connection", {}), **data["connection"]}
            if connection.get("project_path"):
                checked = validate_connection(connection)
                if not checked["ok"]:
                    raise ValueError(checked["message"])
            elif connection.get("mode") == "git":
                raise ValueError("Select a local project before enabling Git publishing")
            fields["connection"] = connection
        if "automation" in data:
            enabled = bool(data["automation"])
            if enabled and not fields.get("confirmed", client.get("confirmed")):
                raise ValueError("Confirm the company profile first")
            if enabled and not fields.get("subject", client.get("subject")):
                raise ValueError("Choose a subject before enabling the weekly schedule")
            fields["automation"] = enabled
            fields["status"] = "active" if enabled else "paused"
        elif fields.get("confirmed"):
            fields["status"] = "active" if client.get("automation") else "ready"
        result = self.store.update("clients", ident, **fields)
        if "conversion_goal" in fields:
            from .measurement import observation
            self.store.put("measurements", observation(ident, "goal", {"previous": client.get("conversion_goal"),
                "current": fields["conversion_goal"]}))
        if "conversion_goal" in fields or "seo_connection" in fields:
            from .autopilot import rebuild
            rebuild(self.engine, ident)
        if "image_brand" in fields and brand_digest(client) != brand_digest(result):
            for article in self.engine.articles(ident):
                if article.get("cover") and not article.get("publish_started"):
                    self.store.update("articles", article["id"], cover_stale=True, status="held")
        if "next_run" in fields:
            due = parse_date(fields["next_run"])
            planned = [a for a in self.engine.articles(ident) if a["status"] == "planned"]
            for index, article in enumerate(planned):
                self.store.update("articles", article["id"], scheduled_at=(due + timedelta(days=index * 7)).isoformat())
        self.store.event("Client settings updated", ident)
        self.engine.wake.set()
        return result

    def dispatch(self, method, path, data):
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/api/session":
            return {"token": self.token}
        if method == "GET" and path == "/api/state":
            return self.engine.state()
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "experiments"] and parts[3] == "cancel":
            from .measurement import cancel
            return cancel(self.engine, parts[2])
        if len(parts) >= 3 and parts[:2] == ["api", "opportunities"]:
            from .autopilot import queue_action
            with self.engine.guard:
                item = self.store.get("opportunities", parts[2])
                if method == "POST" and len(parts) == 4 and parts[3] == "execute":
                    return queue_action(self.engine, item["id"])
                if method == "PATCH" and len(parts) == 3:
                    if set(data) != {"status"} or data["status"] not in ("open", "dismissed"):
                        raise ValueError("Choose open or dismissed")
                    if self.busy(item["client_id"]) or item.get("status") in ("queued", "completed", "resolved"):
                        raise ValueError("This opportunity cannot be changed while queued, completed or resolved")
                    return self.store.update("opportunities", item["id"], status=data["status"], executable=data["status"] == "open")
        if path.startswith("/api/google/"):
            google = self.engine.google
            if method == "POST" and path == "/api/google/configure":
                return google.configure(str(data.get("client_id", "")).strip(), str(data.get("client_secret", "")).strip())
            if method == "POST" and path == "/api/google/connect":
                return {"url": google.begin(self.oauth_redirect)}
            if method == "POST" and path == "/api/google/disconnect":
                return google.disconnect()
            if method == "GET" and path == "/api/google/properties":
                return google.properties()
        if len(parts) >= 3 and parts[:2] == ["api", "backlinks"]:
            link = self.store.get("backlinks", parts[2])
            if method == "POST" and len(parts) == 4 and parts[3] == "check":
                return self.engine.queue("backlink-check", link["client_id"], backlink_id=link["id"])
            if method == "PATCH" and len(parts) == 3:
                fields = {}
                if "status" in data:
                    if data["status"] not in ("prospect", "needs_review", "draft", "contacted", "dismissed") and not (data["status"] == "live" and link.get("status") == "live"):
                        raise ValueError("Invalid prospect status")
                    fields["status"] = data["status"]
                if "notes" in data:
                    fields["notes"] = str(data["notes"])[:5000]
                return self.store.update("backlinks", link["id"], **fields)
        if method == "POST" and path == "/api/clients":
            return self.create_client(data)
        if method == "PATCH" and path == "/api/settings":
            allowed = {k: data[k] for k in ("paused", "codex_path", "model", "max_pages") if k in data}
            if "paused" in allowed:
                allowed["paused"] = bool(allowed["paused"])
            for key in ("codex_path", "model"):
                if key in allowed:
                    allowed[key] = str(allowed[key]).strip()[:1000]
            if "codex_path" in allowed and not allowed["codex_path"]:
                raise ValueError("Codex executable cannot be empty")
            if "max_pages" in allowed:
                allowed["max_pages"] = max(1, min(80, int(allowed["max_pages"])))
            result = self.store.settings(allowed)
            self.engine.wake.set()
            return result
        if method == "POST" and path == "/api/settings/check":
            return self.engine.check_status()
        if method == "POST" and path == "/api/connection/check":
            from .publisher import inspect_project, validate_connection
            result = inspect_project(data.get("project_path", ""))
            if result.get("exists"):
                result["validation"] = validate_connection(data)
                if not result["validation"]["ok"]:
                    result["message"] = result["validation"]["message"]
            return result
        if len(parts) >= 3 and parts[:2] == ["api", "clients"]:
            ident = parts[2]
            if method == "GET" and len(parts) == 4 and parts[3] == "visibility-report":
                report = self.store.get("visibility", ident)
                return {**report, "opportunities": [o for o in self.store.all("opportunities") if o["client_id"] == ident],
                        "tasks": [t for t in self.store.all("tasks") if t["client_id"] == ident],
                        "measurements": self.store.history(ident),
                        "experiments": [r for r in self.store.all("experiments") if r["client_id"] == ident]}
            if method == "GET" and len(parts) == 4 and parts[3] == "measurement-history":
                self.store.get("clients", ident)
                return {"measurements": self.store.history(ident),
                        "experiments": [r for r in self.store.all("experiments") if r["client_id"] == ident]}
            if method == "PATCH" and len(parts) == 3:
                return self.update_client(ident, data)
            if method == "POST" and len(parts) == 4:
                action = parts[3]
                client = self.store.get("clients", ident)
                if action == "experiments":
                    from .measurement import start
                    with self.engine.guard:
                        if self.busy(ident):
                            raise ValueError("Wait for this client's current job before recording a page change")
                        return start(self.engine, ident, data.get("page_url", ""), data.get("hypothesis", ""), data.get("change", ""))
                if action in ("visibility-audit", "visibility-research", "visibility-probe"):
                    if action != "visibility-audit" and not client.get("confirmed"):
                        raise ValueError("Confirm the business profile before public research")
                    return self.engine.queue(action, ident)
                if action == "backlinks":
                    from .crawler import normalize_url
                    source = normalize_url(str(data.get("source_url", "")))
                    if any(b.get("client_id") == ident and b.get("source_url") == source for b in self.store.all("backlinks")):
                        raise ValueError("This source page is already tracked")
                    if self.busy(ident):
                        raise ValueError("Wait for this client's current job to finish")
                    link = self.store.put("backlinks", {"client_id": ident, "source_url": source, "target_url": client["url"], "name": urlparse(source).hostname,
                        "status": "prospect", "notes": str(data.get("notes", ""))[:5000], "draft": "", "reason": "Added manually"})
                    self.engine.queue("backlink-check", ident, backlink_id=link["id"])
                    return link
                if action in ("seo-sync", "backlinks-discover"):
                    if action == "backlinks-discover" and not client.get("confirmed"):
                        raise ValueError("Confirm the business profile before researching prospects")
                    return self.engine.queue(action, ident)
                if action == "inspect":
                    return self.engine.queue("inspect", ident)
                if action == "plan":
                    if self.busy(ident):
                        raise ValueError("This client already has work in progress")
                    subject = str(data.get("subject", client.get("subject", ""))).strip()
                    if not subject or len(subject) > 2000:
                        raise ValueError("Enter a subject, at most 2000 characters")
                    if not client.get("confirmed"):
                        raise ValueError("Confirm the company profile first")
                    self.store.update("clients", ident, subject=subject)
                    return self.engine.queue("plan", ident)
                if action == "run":
                    planned = [a for a in self.engine.articles(ident) if a["status"] == "planned"]
                    if not planned:
                        raise ValueError("Create a content plan before running an article")
                    return self.engine.queue("generate", ident, planned[0]["id"])
        if len(parts) >= 3 and parts[:2] == ["api", "articles"]:
            ident = parts[2]
            article = self.store.get("articles", ident)
            if method == "PATCH" and len(parts) == 3:
                if self.busy(article["client_id"]):
                    raise ValueError("Wait for this client's current job to finish")
                if article.get("publish_started") or article["status"] in ("published", "exported", "verification_pending"):
                    raise ValueError("Published articles are preserved. Edit their source project or create a new draft.")
                allowed = {k: data[k] for k in ("body", "title", "description", "scheduled_at") if k in data}
                if "scheduled_at" in allowed:
                    allowed["scheduled_at"] = parse_date(allowed["scheduled_at"]).isoformat()
                if any(k in allowed for k in ("body", "title", "description")):
                    self.engine._snapshot(article)
                    allowed.update(status="held", reviewed_digest="", cover_stale=bool(article.get("cover")), review={"passed": False, "issues": ["Draft edited. Run review again."], "score": 0})
                return self.store.update("articles", ident, **allowed)
            if method == "POST" and len(parts) == 4 and parts[3] in ("generate", "review", "cover", "publish", "verify"):
                return self.engine.queue(parts[3], article["client_id"], ident)
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "retry":
            return self.engine.retry(parts[2])
        raise KeyError("Endpoint not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RankMe/1.0"

        def log_message(self, fmt, *args):
            # Do not log URL query strings or request bodies.
            pass

        def trusted(self):
            host = self.headers.get("Host", "")
            allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if host not in allowed:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in allowed}:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return True

        def send(self, status, body, content_type="application/json; charset=utf-8", attachment=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def do_PATCH(self):
            self.handle_request("PATCH")

        def handle_request(self, method):
            # OAuth returns from Google as a cross-site top-level navigation. Only
            # this read-only callback bypasses the same-origin check; one-use state
            # and PKCE bind it to an authorization started in this local app.
            if method == "GET" and urlparse(self.path).path == "/api/google/callback":
                if self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port):
                    return self.send(403, {"error": "Invalid callback host"})
                query = parse_qs(urlparse(self.path).query)
                try:
                    app.engine.google.callback(query.get("code", [""])[0], query.get("state", [""])[0], app.oauth_redirect)
                    return self.send(200, '<!doctype html><title>Google connected</title><h1>Google connected</h1><p>Return to RankMe and choose your website property.</p><a href="/">Open RankMe</a>', "text/html; charset=utf-8")
                except Exception:
                    return self.send(400, '<!doctype html><title>Connection incomplete</title><h1>Google connection incomplete</h1><p>Return to RankMe and try connecting again. No metrics have been imported.</p><a href="/">Open RankMe</a>', "text/html; charset=utf-8")
            if not self.trusted():
                return self.send(403, {"error": "RankMe accepts same-origin localhost requests only"})
            path = urlparse(self.path).path
            try:
                data = {}
                if method != "GET":
                    if not secrets.compare_digest(self.headers.get("X-RankMe-Token", ""), app.token):
                        return self.send(403, {"error": "Session expired. Reload RankMe."})
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.send(415, {"error": "Send application/json"})
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 2_000_000:
                        return self.send(413, {"error": "Request is too large"})
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("Request must be a JSON object")
                if method == "GET" and path == "/api/backup":
                    return self.send(200, app.store.backup(), attachment="rankme-backup.json")
                cover_route = re.fullmatch(r"/api/articles/([a-f0-9]+)/cover", path)
                if method == "GET" and cover_route:
                    article = app.store.get("articles", cover_route.group(1))
                    cover = article.get("cover") or {}
                    image_path = Path(cover.get("path", "")).resolve()
                    try:
                        image_path.relative_to((app.data_dir / "covers").resolve())
                    except ValueError:
                        raise KeyError("Cover not found")
                    if not image_path.is_file() or image_path.stat().st_size > 20_000_000:
                        raise KeyError("Cover not found")
                    import hashlib
                    image_bytes = image_path.read_bytes()
                    if hashlib.sha256(image_bytes).hexdigest() != cover.get("sha256"):
                        raise ValueError("Cover changed on disk; generate it again")
                    mime = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}.get(cover.get("format"))
                    if not mime:
                        raise ValueError("Unsupported cover format")
                    filename = None
                    if "download=1" in urlparse(self.path).query:
                        base = re.sub(r"[^a-zA-Z0-9_-]", "", article.get("slug") or article["id"])
                        filename = base + "-cover." + cover["format"]
                    return self.send(200, image_bytes, mime, attachment=filename)
                download = re.fullmatch(r"/api/articles/([a-f0-9]+)/download", path)
                if method == "GET" and download:
                    from .publisher import export_markdown
                    article = app.store.get("articles", download.group(1))
                    filename = re.sub(r"[^a-zA-Z0-9_-]", "", article.get("slug") or article["id"]) + ".md"
                    if article.get("cover"):
                        cover = dict(article["cover"])
                        cover["url"] = article.get("publish_result", {}).get("image_url") or filename[:-3] + "-cover." + cover.get("format", "png")
                        article = {**article, "cover": cover}
                    return self.send(200, export_markdown(article), "text/markdown; charset=utf-8", filename)
                if path.startswith("/api/"):
                    return self.send(200, app.dispatch(method, path, data))
                if method != "GET":
                    raise KeyError("Endpoint not found")
                names = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
                names.update({"/static/" + name: name for name in ("app.js", "styles.css", "favicon.svg")})
                if path not in names:
                    raise KeyError("Page not found")
                file = ROOT / "static" / names[path]
                return self.send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or "application/octet-stream") + "; charset=utf-8")
            except KeyError as exc:
                self.send(404, {"error": str(exc).strip("'")})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send(400, {"error": safe_error(exc)[:1000]})
            except Exception as exc:
                self.send(400, {"error": safe_error(exc)[:1000]})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run RankMe on this computer only")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--no-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    app = Application(args.data_dir)
    app.oauth_redirect = "http://127.0.0.1:" + str(args.port) + "/api/google/callback"
    data_lock = (app.data_dir / "server.lock").open("a")
    try:
        fcntl.flock(data_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        data_lock.close()
        app.store.close()
        print("This data directory is already used by another RankMe server.", file=sys.stderr)
        return 1
    try:
        server = Server(("127.0.0.1", args.port), handler_for(app))
    except OSError as exc:
        print("Cannot start RankMe: " + str(exc), file=sys.stderr)
        data_lock.close()
        app.store.close()
        return 1
    if not args.no_worker:
        app.engine.start()
    print("RankMe is running at http://127.0.0.1:" + str(args.port), flush=True)
    print("Local data: " + str(app.data_dir), flush=True)
    def stop(*unused):
        app.engine.stop_event.set()
        app.engine.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.engine.stop_event.set()
        app.engine.wake.set()
        data_lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
