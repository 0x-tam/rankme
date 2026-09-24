"""Loopback-only web application with a local passkey owner."""
import argparse
from http import cookies
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
from urllib.parse import urlparse, parse_qs

from .auth import AuthError, AuthService, issue_bootstrap, reset_for_recovery
from .store import Store, uid
from .engine import Engine, first_run, parse_date, safe_error, brand_digest

ROOT = Path(__file__).resolve().parent.parent


class Application:
    def __init__(self, data_dir, engine_factory=Engine, port=8787):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.auth = AuthService(self.data_dir, port)
        self.store = Store(self.data_dir / "rankme.sqlite3")
        self.engine = engine_factory(self.store, self.data_dir)
        self.instance = secrets.token_urlsafe(24)
        self.oauth_redirect = f"http://localhost:{port}/api/google/callback"
        self.start_worker_on_enroll = False
        self._engine_start_lock = threading.Lock()
        self._engine_started = False

    def start_engine(self):
        """Start the local engine once, after local or cloud owner enrollment."""
        with self._engine_start_lock:
            if not self._engine_started:
                self.engine.start()
                self._engine_started = True
            self.start_worker_on_enroll = False

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
            brand = normalize_brand(data["image_brand"])
            previous = client.get("image_brand") or {}
            # Learned references only stay valid while the rendering mode stays the same.
            if previous.get("mode", "illustration_3d") == brand.get("mode", "illustration_3d"):
                brand.update({k: previous[k] for k in ("references", "style_spec") if previous.get(k)})
            if previous.get("site"):
                brand["site"] = previous["site"]  # The brand website guide is managed by its own endpoint and job.
            fields["image_brand"] = brand
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
            managed = ("provider", "vercel", "deploy_on_publish")
            connection = {**client.get("connection", {}), **{k: v for k, v in data["connection"].items() if k not in managed}}
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

    def link_vercel(self, ident, data):
        """Connect one website to a Vercel project and prepare RankMe's own copy of its repository."""
        from .publisher import validate_connection
        from .store import now
        from .vercel import github_access, readiness, sync_site
        client = self.store.get("clients", ident)
        if self.busy(ident):
            raise ValueError("Wait for this website's current job to finish")
        project = self.engine.vercel.project(str(data.get("team_id", "")), str(data.get("project_id", "")))
        if not project["repo"]:
            raise ValueError("This Vercel project is not linked to a GitHub repository. Connect it in Vercel under Project → Settings → Git.")
        access = github_access(project["repo"])
        if not access["ok"]:
            raise ValueError(access["message"])
        path = sync_site(self.data_dir, ident, project["repo"], project["branch"])
        ready = readiness(path)
        domains = [d for d in project["domains"] if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d)]
        domain = str(data.get("domain") or "").strip().lower()
        if domain not in domains:
            domain = domains[0] if domains else ""
        if not domain:
            raise ValueError("This Vercel project has no domain. Add one in Vercel first.")
        blog_path = "/" + str(data.get("blog_path") or "blog").strip().strip("/")
        if not re.fullmatch(r"(?:/[a-z0-9-]+)+", blog_path):
            raise ValueError("Blog path must look like /blog or /resources/articles")
        content_dir = str(data.get("content_dir") or ready["content_dir"] or "content/blog").strip().strip("/")
        previous = client.get("connection", {})
        connection = {**previous, "provider": "vercel", "mode": "git", "project_path": str(path), "branch": project["branch"],
                      "remote": "origin", "content_dir": content_dir, "format": previous.get("format") or "md",
                      "build_command": [], "deploy_command": [], "public_url_template": f"https://{domain}{blog_path}/{{slug}}",
                      "deploy_on_publish": True, "auto_publish": bool(previous.get("auto_publish")) and previous.get("provider") == "vercel",
                      "vercel": {"team_id": project["team_id"], "project_id": project["id"], "name": project["name"], "repo": project["repo"],
                                 "branch": project["branch"], "domain": domain, "domains": domains[:20], "framework": ready["framework"],
                                 "readiness": ready, "checked_at": now()}}
        checked = validate_connection(connection)
        if not checked["ok"]:
            raise ValueError(checked["message"])
        with self.engine.guard:
            if self.busy(ident):
                raise ValueError("Wait for this website's current job to finish")
            result = self.store.update("clients", ident, connection=connection)
        self.store.event(f"Connected to Vercel project {project['name']} ({project['repo']})", ident)
        return result

    def check_vercel(self, ident):
        from .store import now
        from .vercel import readiness, sync_site
        client = self.store.get("clients", ident)
        connection = client.get("connection", {})
        if connection.get("provider") != "vercel":
            raise ValueError("This website is not connected to Vercel")
        if self.busy(ident):
            raise ValueError("Wait for this website's current job to finish")
        link = connection["vercel"]
        path = sync_site(self.data_dir, ident, link["repo"], connection["branch"])
        link = {**link, "readiness": readiness(path), "checked_at": now()}
        with self.engine.guard:
            return self.store.update("clients", ident, connection={**connection, "vercel": link})

    def unlink_vercel(self, ident):
        client = self.store.get("clients", ident)
        with self.engine.guard:
            if self.busy(ident):
                raise ValueError("Wait for this website's current job to finish")
            # RankMe's repository copy stays on disk so reconnecting is fast; nothing is deleted remotely.
            connection = {"mode": "export", "format": client.get("connection", {}).get("format", "md"), "content_dir": "content/blog",
                          "auto_publish": False, "remote": "origin", "branch": "", "project_path": "", "build_command": [],
                          "deploy_command": [], "public_url_template": ""}
            result = self.store.update("clients", ident, connection=connection)
        self.store.event("Disconnected the Vercel project", ident)
        return result

    def _drop_cover_files(self, article, client):
        """Delete an article's cover files and forget it as a style reference."""
        import shutil
        folder = self.data_dir.resolve() / "covers" / article["id"]
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", article["id"]) and not folder.is_symlink() and folder.resolve().parent == self.data_dir.resolve() / "covers":
            shutil.rmtree(folder, ignore_errors=True)
        brand = client.get("image_brand") or {}
        if any(r.get("article_id") == article["id"] for r in brand.get("references", [])):
            references = [r for r in brand["references"] if r.get("article_id") != article["id"]]
            brand = {k: v for k, v in {**brand, "references": references}.items() if k != "style_spec"}
            self.store.update("clients", client["id"], image_brand=brand)

    def remove_article(self, article):
        """Delete one article from RankMe. A published copy on the live website is not touched."""
        import shutil
        with self.engine.guard:
            if any(j.get("article_id") == article["id"] and j["status"] in ("queued", "running") for j in self.store.all("jobs")):
                raise ValueError("Stop this article's current job before deleting it")
            if any(a.get("refresh_of") == article["id"] for a in self.store.all("articles")):
                raise ValueError("Delete this article's refresh draft first")
            client = self.store.get("clients", article["client_id"])
            self._drop_cover_files(article, client)
            revisions = self.data_dir.resolve() / "revisions" / article["id"]
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", article["id"]) and not revisions.is_symlink():
                shutil.rmtree(revisions, ignore_errors=True)
            self.store.delete("articles", article["id"])
        live = article.get("status") in ("published", "refreshed", "verification_pending")
        self.store.event("Deleted article “%s”%s" % (article.get("title", "Untitled"), " (it stays live on the website)" if live else ""), client["id"], "warning")
        return {"ok": True, "removed": article["id"]}

    def remove_cover(self, article):
        """Discard an unpublished article's cover; the article waits for a new one before it can publish."""
        with self.engine.guard:
            if any(j.get("article_id") == article["id"] and j["status"] in ("queued", "running") for j in self.store.all("jobs")):
                raise ValueError("Stop this article's current job first")
            if article.get("publish_started") or article["status"] in ("published", "exported", "verification_pending", "refreshed"):
                raise ValueError("Published covers are preserved")
            if not article.get("cover"):
                raise ValueError("This article has no cover")
            client = self.store.get("clients", article["client_id"])
            self._drop_cover_files(article, client)
            status = "held" if article["status"] == "ready" else article["status"]
            return self.store.update("articles", article["id"], cover=None, cover_stale=False, status=status)

    def remove_client(self, ident, data):
        """Permanently remove a website from RankMe. Nothing on the live website, GitHub, or Vercel changes."""
        import shutil
        client = self.store.get("clients", ident)
        host = (urlparse(client["url"]).hostname or "").removeprefix("www.")
        typed = str(data.get("confirm", "")).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        if not host or typed != host:
            raise ValueError("Type " + host + " to confirm removing this website")
        with self.engine.guard:
            if self.busy(ident):
                raise ValueError("Wait for this website's current job to finish, or pause it, before removing the website")
            article_ids = [a["id"] for a in self.store.all("articles") if a.get("client_id") == ident]
            self.store.delete_client(ident)
        root = self.data_dir.resolve()
        folders = [root / "brand" / ident, root / "sites" / ident, root / "exports" / ident]
        folders += [root / kind / article for article in article_ids for kind in ("covers", "revisions")]
        for folder in folders:
            # Only RankMe-owned folders inside the data directory; never follow a symbolic link out of it.
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", folder.name) and not folder.is_symlink() and root in folder.resolve().parents:
                shutil.rmtree(folder, ignore_errors=True)
        self.store.event("Removed website " + (client.get("name") or host) + " and its RankMe data", None, "warning")
        return {"ok": True, "removed": ident}

    def brand_guide(self, ident, data):
        """Set the brand reference website for covers and queue a scan of its imagery and colors."""
        from .crawler import normalize_url
        client = self.store.get("clients", ident)
        url = normalize_url(str(data.get("url") or client["url"]).strip())
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https") or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", parsed.hostname or "")
                or parsed.username or parsed.password):
            raise ValueError("Enter the public website address, such as https://example.com")
        with self.engine.guard:
            if self.busy(ident):
                raise ValueError("Wait for this website's current job to finish")
            brand = dict(client.get("image_brand") or {})
            previous = brand.get("site") or {}
            # A different website starts a fresh guide; rescanning the same one keeps the current guide until the new one is ready.
            brand["site"] = {**previous, "url": url} if previous.get("url") == url else {"url": url}
            self.store.update("clients", ident, image_brand=brand)
            return self.engine.queue("brand-guide", ident)

    def style_reference(self, article, use):
        """Add or remove an approved cover as one of its website's style references (at most three, newest kept)."""
        from .covers import MAX_REFERENCES, cover_mode
        client = self.store.get("clients", article["client_id"])
        with self.engine.guard:
            if self.busy(client["id"]):
                raise ValueError("Wait for this website's current job to finish")
            brand = dict(client.get("image_brand") or {})
            cover = article.get("cover") or {}
            references = [r for r in brand.get("references", []) if r.get("article_id") != article["id"]]
            if use:
                if not cover.get("review", {}).get("passed") or not self.engine.cover_valid(client, article):
                    raise ValueError("Only a cover that passed review can become a style reference")
                if cover.get("mode", "illustration_3d") != cover_mode(client):
                    raise ValueError("This cover was made in a different style than the website now uses")
                references = (references + [{"article_id": article["id"], "sha256": cover["sha256"]}])[-MAX_REFERENCES:]
            brand["references"] = references
            brand.pop("style_spec", None)
            result = self.store.update("clients", client["id"], image_brand=brand)
            if references:
                self.engine.queue("style", client["id"])
        return result

    def dispatch(self, method, path, data):
        parts = path.strip("/").split("/")
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
        if path.startswith("/api/vercel/"):
            vercel = self.engine.vercel
            if method == "POST" and path == "/api/vercel/connect":
                return vercel.connect(str(data.get("token", "")))
            if method == "POST" and path == "/api/vercel/disconnect":
                return vercel.disconnect()
            if method == "GET" and path == "/api/vercel/projects":
                return vercel.projects()
        if method == "POST" and len(parts) in (4, 5) and parts[:2] == ["api", "clients"] and parts[3] == "vercel":
            action = parts[4] if len(parts) == 5 else "link"
            if action == "link":
                return self.link_vercel(parts[2], data)
            if action == "check":
                return self.check_vercel(parts[2])
            if action == "unlink":
                return self.unlink_vercel(parts[2])
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
                if action == "brand-guide":
                    return self.brand_guide(ident, data)
                if action == "remove":
                    return self.remove_client(ident, data)
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
            if method == "POST" and len(parts) == 4 and parts[3] == "remove":
                return self.remove_article(article)
            if method == "POST" and len(parts) == 4 and parts[3] == "remove-cover":
                return self.remove_cover(article)
            if method == "POST" and len(parts) == 4 and parts[3] == "style-reference":
                return self.style_reference(article, bool(data.get("use")))
            if method == "POST" and len(parts) == 4 and parts[3] in ("generate", "review", "cover", "publish", "verify"):
                return self.engine.queue(parts[3], article["client_id"], ident)
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "retry":
            return self.engine.retry(parts[2])
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop":
            return self.engine.stop(parts[2])
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "dismiss":
            return self.engine.dismiss(parts[2])
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

        def cookie(self, name):
            try:
                jar = cookies.SimpleCookie()
                jar.load(self.headers.get("Cookie", "")[:4096])
                return jar[name].value if name in jar else ""
            except cookies.CookieError:
                return ""

        def session(self, touch=False):
            return app.auth.session(self.cookie("__Host-rankme-session"), touch=touch)

        def csrf(self, expected):
            supplied = self.headers.get("X-RankMe-CSRF", "") or self.headers.get("X-RankMe-Token", "")
            return bool(supplied) and len(supplied) <= 256 and secrets.compare_digest(supplied, expected)

        def trusted(self, method):
            canonical = "localhost:" + str(self.server.server_port)
            if self.headers.get("Host") != canonical:
                return False
            origin = self.headers.get("Origin")
            if method != "GET" and origin != "http://" + canonical:
                return False
            if origin and origin != "http://" + canonical:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return True

        def send(self, status, body, content_type="application/json; charset=utf-8", attachment=None, set_cookies=()):
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
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", 'camera=(), microphone=(), geolocation=(), payment=(), publickey-credentials-get=(self)')
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            for cookie in set_cookies:
                self.send_header("Set-Cookie", cookie)
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

        def do_DELETE(self):
            self.handle_request("DELETE")

        def handle_request(self, method):
            path = urlparse(self.path).path
            port = self.server.server_port
            host = self.headers.get("Host", "")
            if method == "GET" and path == "/api/health" and host in (f"localhost:{port}", f"127.0.0.1:{port}"):
                busy = any(j["status"] == "running" for j in app.store.all("jobs")) if app.auth.enrolled() else False
                return self.send(200, {"service": "rankme", "version": "1", "instance": app.instance, "busy": busy})
            if method == "GET" and host == f"127.0.0.1:{port}" and path in ("/", "/index.html", "/app.js", "/styles.css", "/favicon.svg"):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}{self.path}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Google returns by top-level cross-site navigation. SameSite=Lax
            # supplies the initiating session; state and PKCE remain one-use.
            if method == "GET" and path == "/api/google/callback":
                if host != f"localhost:{port}":
                    return self.send(403, {"error": "Connection failed"})
                query = parse_qs(urlparse(self.path).query)
                session = self.session()
                try:
                    app.auth.consume_oauth(session[0] if session else "", query.get("state", [""])[0])
                    app.engine.google.callback(query.get("code", [""])[0], query.get("state", [""])[0], app.oauth_redirect)
                    return self.send(200, '<!doctype html><title>Google connected</title><h1>Google connected</h1><p>Return to RankMe and choose your website property.</p><a href="/">Open RankMe</a>', "text/html; charset=utf-8")
                except Exception:
                    return self.send(400, '<!doctype html><title>Connection incomplete</title><h1>Google connection incomplete</h1><p>Return to RankMe and try connecting again.</p><a href="/">Open RankMe</a>', "text/html; charset=utf-8")
            if not self.trusted(method):
                return self.send(403, {"error": "Request denied"})
            try:
                session = self.session()
                if method == "GET" and path == "/api/auth/status":
                    if session:
                        return self.send(200, {"enrolled": app.auth.enrolled(), "authenticated": True,
                                               "csrf": session[1]["csrf"]})
                    raw, csrf = app.auth.preauth_status(self.cookie("__Host-rankme-preauth"))
                    return self.send(200, {"enrolled": app.auth.enrolled(), "authenticated": False, "csrf": csrf},
                                     set_cookies=(f"__Host-rankme-preauth={raw}; Max-Age=600; Path=/; HttpOnly; Secure; SameSite=Lax",))
                if method == "GET" and path == "/api/session":
                    if not session:
                        return self.send(401, {"error": "Authentication required"})
                    csrf = session[1]["csrf"]
                    return self.send(200, {"csrf": csrf, "token": csrf})
                data = {}
                if method != "GET":
                    if path.startswith("/api/auth/") and path in (
                            "/api/auth/enroll/options", "/api/auth/enroll/verify",
                            "/api/auth/login/options", "/api/auth/login/verify"):
                        binding = app.auth.preauth_check(self.cookie("__Host-rankme-preauth"),
                            self.headers.get("X-RankMe-CSRF", ""))
                    else:
                        if not session:
                            return self.send(401, {"error": "Authentication required"})
                        if not self.csrf(session[1]["csrf"]):
                            return self.send(403, {"error": "Request denied"})
                        session = self.session(touch=True)
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.send(415, {"error": "Send application/json"})
                    length = int(self.headers.get("Content-Length", "0"))
                    limit = 64_000 if path.startswith("/api/auth/") else 2_000_000
                    if not 0 <= length <= limit:
                        return self.send(413, {"error": "Request is too large"})
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("Request must be a JSON object")
                if path.startswith("/api/auth/"):
                    if method == "POST" and path == "/api/auth/enroll/options":
                        return self.send(200, app.auth.enroll_options(binding, data.get("bootstrap_secret"), data.get("name", "")))
                    if method == "POST" and path == "/api/auth/enroll/verify":
                        raw = app.auth.enroll_verify(binding, data.get("credential"))
                        if app.start_worker_on_enroll:
                            app.start_engine()
                        return self.send(200, {"ok": True}, set_cookies=(
                            f"__Host-rankme-session={raw}; Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax",
                            "__Host-rankme-preauth=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"))
                    if method == "POST" and path == "/api/auth/login/options":
                        return self.send(200, app.auth.login_options(binding))
                    if method == "POST" and path == "/api/auth/login/verify":
                        raw = app.auth.login_verify(binding, data.get("credential"))
                        return self.send(200, {"ok": True}, set_cookies=(
                            f"__Host-rankme-session={raw}; Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax",
                            "__Host-rankme-preauth=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"))
                    if not session:
                        return self.send(401, {"error": "Authentication required"})
                    if method == "POST" and path == "/api/auth/logout":
                        app.auth.logout(self.cookie("__Host-rankme-session"))
                        return self.send(200, {"ok": True}, set_cookies=(
                            "__Host-rankme-session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax",))
                    if method == "POST" and path == "/api/auth/touch":
                        return self.send(200, {"ok": True})
                    if method == "GET" and path == "/api/auth/credentials":
                        return self.send(200, {"credentials": app.auth.list_credentials()})
                    if method == "POST" and path == "/api/auth/step-up/options":
                        return self.send(200, app.auth.step_up_options(session[0]))
                    if method == "POST" and path == "/api/auth/step-up/verify":
                        app.auth.step_up_verify(session[0], data.get("credential"))
                        return self.send(200, {"ok": True})
                    if method == "POST" and path == "/api/auth/credentials/options":
                        return self.send(200, app.auth.add_options(session[0], data.get("name", "")))
                    if method == "POST" and path == "/api/auth/credentials/verify":
                        app.auth.add_verify(session[0], data.get("credential"))
                        return self.send(200, {"ok": True})
                    match = re.fullmatch(r"/api/auth/credentials/([A-Za-z0-9_-]{1,8192})", path)
                    if method == "DELETE" and match:
                        app.auth.remove(session[0], match.group(1))
                        return self.send(200, {"ok": True})
                    raise KeyError("Endpoint not found")
                if path.startswith("/api/") and not session:
                    return self.send(401, {"error": "Authentication required"})
                if method == "POST" and path == "/api/google/connect":
                    result = app.dispatch(method, path, data)
                    state = parse_qs(urlparse(result["url"]).query).get("state", [""])[0]
                    app.auth.begin_oauth(session[0], state)
                    return self.send(200, result)
                if method == "GET" and path == "/api/backup":
                    return self.send(200, app.store.backup(), attachment="rankme-backup.json")
                brand_image = re.fullmatch(r"/api/clients/([a-zA-Z0-9_-]+)/brand-images/([0-9a-f]{16}\.(?:png|jpg|webp))", path)
                if method == "GET" and brand_image:
                    from .brand_guide import image_path
                    client = app.store.get("clients", brand_image.group(1))
                    items = ((client.get("image_brand") or {}).get("site") or {}).get("images") or []
                    item = next((i for i in items if i.get("file") == brand_image.group(2)), None)
                    image_file = item and image_path(app.data_dir, client["id"], item)
                    if not image_file:
                        raise KeyError("Brand image not found")
                    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[image_file.suffix[1:]]
                    return self.send(200, image_file.read_bytes(), mime)
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
                names = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/auth.js": "auth.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
                names.update({"/static/" + name: name for name in ("app.js", "auth.js", "styles.css", "favicon.svg")})
                if path not in names:
                    raise KeyError("Page not found")
                file = ROOT / "static" / names[path]
                return self.send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or "application/octet-stream") + "; charset=utf-8")
            except AuthError as exc:
                self.send(exc.status, {"error": str(exc)})
            except KeyError as exc:
                self.send(404, {"error": str(exc).strip("'")})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send(400, {"error": safe_error(exc)[:1000]})
            except Exception:
                self.send(500, {"error": "Request failed"})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run RankMe on this computer only")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--no-worker", action="store_true", help=argparse.SUPPRESS)
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--bootstrap-secret", action="store_true", help="Issue a short-lived first-owner enrollment secret in this terminal")
    command.add_argument("--recover-passkeys", action="store_true", help="Reset lost passkeys with local OS access and a stopped server")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    if args.bootstrap_secret:
        if not sys.stdout.isatty():
            parser.error("Enrollment secret requires an interactive local terminal")
        print(issue_bootstrap(args.data_dir))
        return 0
    if args.recover_passkeys:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("Passkey recovery requires an interactive local terminal")
        data_dir = Path(args.data_dir).expanduser().resolve()
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(data_dir / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            parser.error("Stop RankMe before passkey recovery")
        try:
            print("This removes every RankMe passkey. Existing sessions end when the server restarts.")
            if input("Type RESET RANKME PASSKEYS to continue: ") != "RESET RANKME PASSKEYS":
                print("Recovery canceled")
                return 1
            reset_for_recovery(data_dir)
            print("Passkeys reset. Start RankMe, then run the launcher's explicit --enroll command.")
            return 0
        finally:
            os.close(descriptor)
    app = Application(args.data_dir, port=args.port)
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
    cloud_worker = None
    if not args.no_worker:
        cloud_config = app.data_dir / "cloud-bridge.json"
        try:
            if cloud_config.exists() or cloud_config.is_symlink():
                from .cloud_bridge import CloudWorker
                cloud_worker = CloudWorker(app, cloud_config, on_owner_enrolled=app.start_engine)
        except Exception:
            server.server_close()
            data_lock.close()
            app.store.close()
            print("Cloud worker configuration is invalid; check the private cloud-bridge.json file.", file=sys.stderr)
            return 1
        if app.auth.enrolled():
            app.start_engine()
        else:
            app.start_worker_on_enroll = True
        if cloud_worker:
            cloud_worker.start()
    print("RankMe is running at http://localhost:" + str(args.port), flush=True)
    print("Local data: " + str(app.data_dir), flush=True)
    def stop(*unused):
        if cloud_worker:
            cloud_worker.stop_event.set()
        app.engine.stop_event.set()
        app.engine.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if cloud_worker:
            cloud_worker.stop()
        server.server_close()
        app.engine.stop_event.set()
        app.engine.wake.set()
        data_lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
