"""Observed Google AI Overviews through SerpApi, with a local request ledger.

No inferred overview is recorded as a search observation. Provider text and URLs
are untrusted data. This module never fetches cited URLs or follows API redirects.
"""

from __future__ import annotations

import ipaddress
import importlib.util
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import fcntl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .crawler import _public_address

PROVIDER = "SerpApi Google Search"
CONFIG_NAME = "ai_overviews_config.json"
DB_NAME = "ai_overviews.sqlite3"
MAX_RESPONSE_BYTES = 2_000_000
MAX_TEXT = 20_000
BROWSER_DAILY_CAP = 6
BROWSER_PROFILE_NAME = "aio-browser-profile"
BROWSER_LOCK_NAME = "aio-browser.lock"
_STOP = set("a an and are as at be by for from has have how in is it of on or that the their these this to was were what when where which who why with you your".split())


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _utc(value).isoformat().replace("+00:00", "Z")


def _private_file(path):
    """Reject swapped links, shared credentials, and multi-link files."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0)


def _validate_cohort(client_id, query, country, language, location, device):
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id):
        raise ValueError("Invalid client ID")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or any(ord(c) < 32 for c in query):
        raise ValueError("Query must contain 1–500 printable characters")
    if not isinstance(country, str) or not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Country must be a two-letter lowercase code")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", language):
        raise ValueError("Language must be a lowercase language code")
    if not isinstance(location, str) or len(location) > 200 or any(ord(c) < 32 for c in location):
        raise ValueError("Invalid location")
    if device not in ("desktop", "mobile", "tablet"):
        raise ValueError("Invalid device")
    return {"client_id": client_id, "query": query.strip(), "country": country,
            "language": language, "location": location.strip(), "device": device}


def _public_url(raw):
    if not isinstance(raw, str) or len(raw) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return ""
    try:
        p = urllib.parse.urlsplit(raw.strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")) or "%" in host:
            return ""
        try:
            if not _public_address(ipaddress.ip_address(host)):
                return ""
        except ValueError:
            pass
        if p.port not in (None, 80, 443):
            return ""
        netloc = host if ":" not in host else "[" + host + "]"
        if p.port and p.port != (443 if p.scheme == "https" else 80):
            netloc += ":" + str(p.port)
        parameters = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        sensitive = {"token", "secret", "password", "passwd", "api_key", "apikey", "access_key",
                     "accesskey", "authorization", "auth", "session", "signature", "credential",
                     "key", "sid", "code", "sig", "private_key", "awsaccesskeyid"}
        def sensitive_name(key):
            snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
            normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
            parts = normalized.split("_")
            return normalized in sensitive or any(part in sensitive for part in parts)
        if any(sensitive_name(key) for key, _ in parameters):
            return ""
        query = urllib.parse.urlencode([(k, v) for k, v in parameters
                                        if not k.lower().startswith("utm_")], doseq=True)
        return urllib.parse.urlunsplit((p.scheme, netloc, p.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return ""


def _text(value, limit=MAX_TEXT):
    return str(value)[:limit].strip() if isinstance(value, str) else ""


def _blocks(value):
    snippets = []
    pending = [(item, 0) for item in reversed(value[:100])] if isinstance(value, list) else []
    visited = 0
    while pending and visited < 200:
        item, depth = pending.pop()
        visited += 1
        if not isinstance(item, dict):
            continue
        for key in ("title", "snippet"):
            part = _text(item.get(key), 2000)
            if part:
                snippets.append(part)
        nested = item.get("list")
        if isinstance(nested, list) and depth < 8:
            pending.extend((child, depth + 1) for child in reversed(nested[:100]))
    return "\n".join(snippets)[:MAX_TEXT]


def _references(value, verification="provider_reported"):
    found, seen = [], set()
    for ref in value[:50] if isinstance(value, list) else []:
        if not isinstance(ref, dict):
            continue
        url = _public_url(ref.get("link"))
        if not url or url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "domain": urllib.parse.urlsplit(url).hostname,
                      "title": _text(ref.get("title"), 300), "snippet": _text(ref.get("snippet"), 1000),
                      "source": _text(ref.get("source"), 200),
                      "provider_index": ref.get("index") if type(ref.get("index")) is int else None,
                      "verification": verification})
    return found


def _request_json(api_key, params):
    """Fixed HTTPS destination; no redirects; one bounded request, no automatic retry."""
    query = urllib.parse.urlencode({**params, "api_key": api_key, "output": "json"})
    request = urllib.request.Request("https://serpapi.com/search.json?" + query,
                                     headers={"Accept": "application/json", "User-Agent": "RankMe/1.0"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError("provider_http_error")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("provider_response_too_large")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("provider_invalid_response")
            return value
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        # urllib exception strings can contain the URL and therefore the key.
        raise ValueError("provider_transport_error") from None


def _browser_runtime():
    if importlib.util.find_spec("playwright") is None:
        return None, "playwright_not_installed"
    candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path), "ready"
    return None, "chrome_not_installed"


_BROWSER_SNAPSHOT_JS = r"""() => {
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const root = document.querySelector('#search') || document.querySelector('main');
  const headings = root ? [...root.querySelectorAll('[role="heading"],h1,h2,h3')]
    .filter(e => visible(e) && (e.innerText || '').trim() === 'AI Overview') : [];
  const heading = headings[0];
  let container = heading && heading.closest('div.CKgc1d');
  const anchors = container ? [...container.querySelectorAll('a[href]')]
    .filter(visible).slice(0, 80).map(a => ({url:a.href.slice(0,2001), title:(a.innerText || a.getAttribute('aria-label') || '').trim().slice(0,300)})) : [];
  const body = (document.body.innerText || '').slice(0,30000);
  return {
    title: document.title.slice(0,300), final_url: location.href.slice(0,3000),
    returned_query: [...document.querySelectorAll('textarea[name="q"],input[name="q"]')]
      .find(visible)?.value || '',
    heading_count: headings.length, container_verified: !!container,
    container_text: container ? (container.innerText || '').slice(0,20000) : '',
    anchors, organic_result_count: root ? root.querySelectorAll('a h3').length : 0,
    challenge_text: /unusual traffic|not a robot|verify you are human|before you continue|captcha/i.test(body.slice(0,3000)),
    generating: /generating(?: an)? ai overview|ai overview is loading/i.test(body),
    body_mentions_overview: /AI Overview/i.test(body),
    search_root_present: !!root
  };
}"""


def _browser_environment():
    return {key: os.environ[key] for key in ("HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "DISPLAY")
            if key in os.environ}


def _browse_google(query, country, language, profile, executable):
    """Bound the entire browser session and keep query text out of process argv."""
    payload = json.dumps({"query": query, "country": country, "language": language,
                          "profile": str(profile), "executable": executable})
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "rankme.aio_browser_worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True, cwd=Path(__file__).resolve().parent.parent,
            env=_browser_environment())
        try:
            output, _ = process.communicate(payload, timeout=55)
        except subprocess.TimeoutExpired:
            # The worker spawns Playwright's driver and Chrome; kill the group,
            # not just the Python parent, before releasing the profile lock.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
            raise ValueError("browser_worker_timeout") from None
        if process.returncode != 0 or len(output) > MAX_RESPONSE_BYTES:
            raise ValueError("browser_worker_failed")
        snapshot = json.loads(output)
        if not isinstance(snapshot, dict):
            raise ValueError("browser_worker_invalid_result")
        return snapshot
    except (subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError):
        raise ValueError("browser_worker_failed") from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


def _browser_session(query, country, language, profile, executable):
    """One ordinary Google Search in isolated stock Chrome; no CAPTCHA automation."""
    from playwright.sync_api import sync_playwright

    search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"q": query, "gl": country, "hl": language, "pws": "0"})
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), executable_path=executable, headless=True, chromium_sandbox=True,
            accept_downloads=False, service_workers="block", viewport={"width": 1280, "height": 900},
            timeout=15000, env=_browser_environment(),
            args=["--disable-extensions", "--disable-sync", "--disable-background-networking", "--no-first-run"])
        try:
            def route_request(route):
                try:
                    host = urllib.parse.urlsplit(route.request.url).hostname or ""
                    if host in ("google.com", "gstatic.com") or host.endswith((".google.com", ".gstatic.com")):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    route.abort()
            context.route("**/*", route_request)
            page = context.new_page()
            page.set_default_timeout(5000)
            response = page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            more = page.get_by_role("button", name="Show more AI Overview", exact=True)
            if more.count():
                try:
                    more.first.click(timeout=3000)
                    page.wait_for_timeout(500)
                except Exception:
                    return {"browser_error": "overview_expansion_failed", "http_status": response.status if response else None}
            snapshot = page.evaluate(_BROWSER_SNAPSHOT_JS)
            snapshot["http_status"] = response.status if response else None
            return snapshot
        finally:
            context.close()


def _parse_browser_snapshot(snapshot, query):
    if not isinstance(snapshot, dict):
        return "error", "invalid_browser_snapshot", "", [], {}
    status = snapshot.get("http_status")
    url = snapshot.get("final_url") or ""
    parsed = urllib.parse.urlsplit(url)
    evidence = {"http_status": status, "heading_count": snapshot.get("heading_count", 0),
                "container_verified": snapshot.get("container_verified") is True,
                "organic_result_count": snapshot.get("organic_result_count", 0),
                "final_host": parsed.hostname if parsed.hostname in ("www.google.com", "consent.google.com") else "other",
                "final_page": "search" if parsed.path == "/search" else ("challenge" if "/sorry/" in parsed.path else "other"),
                "extraction": "visible_google_search_ai_overview_container_v1"}
    if snapshot.get("browser_error"):
        return "error", "browser_extraction_error", "", [], evidence
    if status in (403, 429) or snapshot.get("challenge_text") or "/sorry/" in parsed.path or "consent.google.com" == parsed.hostname:
        return "blocked", "google_challenge_or_consent", "", [], evidence
    if status != 200 or parsed.hostname != "www.google.com" or parsed.path != "/search":
        return "error", "unexpected_google_search_page", "", [], evidence
    if snapshot.get("returned_query") != query:
        return "error", "search_query_mismatch", "", [], evidence
    if snapshot.get("generating"):
        return "error", "overview_still_generating", "", [], evidence
    heading_count = snapshot.get("heading_count")
    if type(heading_count) is not int or heading_count < 0 or heading_count > 1:
        return "error", "ambiguous_overview_heading", "", [], evidence
    if heading_count == 0:
        if snapshot.get("body_mentions_overview"):
            return "error", "overview_heading_unrecognized", "", [], evidence
        if snapshot.get("search_root_present") and type(snapshot.get("organic_result_count")) is int and snapshot["organic_result_count"] > 0:
            return "absent", "no_visible_ai_overview_on_loaded_results", "", [], evidence
        return "error", "search_results_not_confirmed", "", [], evidence
    if snapshot.get("container_verified") is not True:
        return "error", "overview_container_unrecognized", "", [], evidence
    raw = _text(snapshot.get("container_text"), MAX_TEXT)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if lines and lines[0] == "AI Overview":
        lines.pop(0)
    lines = [line for line in lines if line not in ("Show more", "Show more AI Overview", "Show less", "AI Overview")]
    answer = "\n".join(lines)[:MAX_TEXT]
    if len(answer) < 40 or len(answer.split()) < 8:
        return "error", "overview_container_incomplete", "", [], evidence
    links = []
    for item in snapshot.get("anchors", [])[:80] if isinstance(snapshot.get("anchors"), list) else []:
        if not isinstance(item, dict):
            continue
        link = _public_url(item.get("url"))
        host = urllib.parse.urlsplit(link).hostname if link else ""
        if host in ("google.com", "www.google.com") and urllib.parse.urlsplit(link).path == "/url":
            redirect_params = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
            link = _public_url((redirect_params.get("url") or redirect_params.get("q") or [""])[0])
            host = urllib.parse.urlsplit(link).hostname if link else ""
        if not link or host == "google.com" or host.endswith((".google.com", ".gstatic.com")):
            continue
        links.append({"link": link, "title": item.get("title", "")})
    citations = _references(links, verification="browser_observed")
    return "present", "", answer, citations, evidence


class AIOCollector:
    def __init__(self, data_dir, clock=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / CONFIG_NAME
        self.db_path = self.data_dir / DB_NAME
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _config(self):
        if not _private_file(self.config_path):
            return None, "missing_or_insecure_config"
        try:
            fd = os.open(self.config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    return None, "missing_or_insecure_config"
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    fd = -1
                    raw = stream.read(4097)
                    if len(raw) > 4096:
                        return None, "invalid_or_disabled_config"
                    config = json.loads(raw)
            finally:
                if fd >= 0:
                    os.close(fd)
            if (not isinstance(config, dict) or config.get("provider") != "serpapi"
                    or not isinstance(config.get("api_key"), str) or not config["api_key"].strip()
                    or type(config.get("daily_call_cap")) is not int
                    or not 1 <= config["daily_call_cap"] <= 10000
                    or config.get("enabled") is not True):
                return None, "invalid_or_disabled_config"
            return config, "ready"
        except (OSError, UnicodeError, ValueError):
            return None, "invalid_or_disabled_config"

    def _db(self):
        if self.db_path.exists() and not _private_file(self.db_path):
            raise ValueError("Insecure AI Overview history file")
        if not self.db_path.exists():
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc_value, traceback):
                try:
                    return super().__exit__(exc_type, exc_value, traceback)
                finally:
                    self.close()
        db = sqlite3.connect(self.db_path, timeout=5, factory=ClosingConnection)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, day TEXT NOT NULL, engine TEXT NOT NULL, state TEXT NOT NULL, recorded_at TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS requests_day ON requests(day)")
        db.execute("CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, query TEXT NOT NULL, country TEXT NOT NULL, language TEXT NOT NULL, location TEXT NOT NULL, device TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS observations_cohort ON observations(client_id, query, country, language, location, device, observed_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS observations_exact_manual_sample ON observations(client_id, query, country, language, location, device, observed_at) WHERE source='manual'")
        db.commit()
        return db

    def readiness(self):
        config, reason = self._config()
        today = _utc(self.clock()).date().isoformat()
        try:
            with self._db() as db:
                used = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND engine IN ('google','google_ai_overview')", (today,)).fetchone()[0]
        except (OSError, sqlite3.Error, ValueError):
            return {"configured": False, "status": "blocked", "reason": "history_unavailable", "calls_today": None,
                    "daily_call_cap": config["daily_call_cap"] if config else None}
        cap = config["daily_call_cap"] if config else None
        return {"configured": bool(config), "status": "ready" if config and used < cap else "blocked",
                "reason": "daily_cap_reached" if config and used >= cap else reason,
                "calls_today": used, "daily_call_cap": cap}

    def browser_readiness(self):
        executable, reason = _browser_runtime()
        today = _utc(self.clock()).date().isoformat()
        try:
            with self._db() as db:
                used = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND engine='browser_google'", (today,)).fetchone()[0]
        except (OSError, sqlite3.Error, ValueError):
            return {"configured": False, "status": "blocked", "reason": "history_unavailable",
                    "calls_today": None, "daily_call_cap": BROWSER_DAILY_CAP,
                    "provider": "local_chrome", "cost": "free"}
        available = bool(executable) and used < BROWSER_DAILY_CAP
        return {"configured": bool(executable), "status": "ready" if available else "blocked",
                "reason": "daily_cap_reached" if used >= BROWSER_DAILY_CAP else reason,
                "calls_today": used, "daily_call_cap": BROWSER_DAILY_CAP,
                "provider": "local_chrome", "cost": "free"}

    def _reserve(self, engine, cap):
        timestamp = _stamp(self.clock())
        day = timestamp[:10]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if engine == "browser_google":
                used = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND engine='browser_google'", (day,)).fetchone()[0]
            else:
                used = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND engine IN ('google','google_ai_overview')", (day,)).fetchone()[0]
            if used >= cap:
                return None
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (request_id, day, engine, "reserved", timestamp))
            return request_id

    def _request(self, config, engine, params, request_ids, provenance):
        ident = self._reserve(engine, config["daily_call_cap"])
        if ident is None:
            return None
        request_ids.append(ident)
        provenance.append({"request_id": ident, "engine": engine, "requested_at": _stamp(self.clock()),
                           "endpoint": "https://serpapi.com/search.json"})
        state = "error"
        try:
            result = _request_json(config["api_key"], params)
            state = "completed"
            return result
        finally:
            with self._db() as db:
                db.execute("UPDATE requests SET state=? WHERE id=?", (state, ident))

    def _save(self, record):
        with self._db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (record["id"], record["client_id"], record["query"], record["country"], record["language"],
                                 record["location"], record["device"], record["observed_at"], record["source"],
                                 json.dumps(record, ensure_ascii=False)))
            if cursor.rowcount == 0:
                row = db.execute("SELECT payload FROM observations WHERE client_id=? AND query=? AND country=? AND language=? AND location=? AND device=? AND observed_at=? AND source=?",
                                 (record["client_id"], record["query"], record["country"], record["language"],
                                  record["location"], record["device"], record["observed_at"], record["source"])).fetchone()
                if row:
                    return json.loads(row[0])
        return record

    def collect_browser(self, client_id, query, country="us", language="en", location="", device="desktop"):
        """Collect one free Google Search browser observation, never AI Mode."""
        cohort = _validate_cohort(client_id, query, country, language, location, device)
        record = {"id": uuid.uuid4().hex, **cohort, "observed_at": _stamp(self.clock()),
                  "source": "browser", "provider": "Google Search local Chrome", "status": "blocked",
                  "reason": "", "answer": "", "citations": [], "request_ids": [], "request_provenance": [],
                  "provider_search_ids": [], "browser_evidence": {},
                  "locale_verification": "gl/hl requested; actual physical location unverified"}
        if location or device != "desktop" or language != "en":
            record["reason"] = "unsupported_browser_cohort"
            return self._save(record)
        executable, reason = _browser_runtime()
        if not executable:
            record["reason"] = reason
            return self._save(record)
        profile = self.data_dir / BROWSER_PROFILE_NAME
        try:
            profile.mkdir(mode=0o700, exist_ok=True)
            profile_info = profile.lstat()
            if (not stat.S_ISDIR(profile_info.st_mode) or profile_info.st_uid != os.getuid()
                    or profile_info.st_mode & 0o077):
                record["reason"] = "insecure_browser_profile"
                return self._save(record)
            lock_path = self.data_dir / BROWSER_LOCK_NAME
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                lock_info = os.fstat(lock_fd)
                if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                        or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077):
                    record["reason"] = "insecure_browser_lock"
                    return self._save(record)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    record["reason"] = "browser_busy"
                    return self._save(record)
                request_id = self._reserve("browser_google", BROWSER_DAILY_CAP)
                if request_id is None:
                    record["reason"] = "daily_cap_reached"
                    return self._save(record)
                record["request_ids"].append(request_id)
                record["request_provenance"].append({"request_id": request_id, "engine": "browser_google",
                    "requested_at": _stamp(self.clock()), "endpoint": "https://www.google.com/search"})
                state = "error"
                try:
                    snapshot = _browse_google(cohort["query"], country, language, profile, executable)
                    status, reason, answer, citations, evidence = _parse_browser_snapshot(snapshot, cohort["query"])
                    record.update(status=status, reason=reason, answer=answer, citations=citations,
                                  browser_evidence=evidence)
                    state = "completed"
                except Exception:
                    # Browser exceptions may include URLs, page text, or local profile paths.
                    record.update(status="error", reason="browser_navigation_or_runtime_error")
                finally:
                    with self._db() as db:
                        db.execute("UPDATE requests SET state=? WHERE id=?", (state, request_id))
                    record["observed_at"] = _stamp(self.clock())
            finally:
                os.close(lock_fd)
        except (OSError, sqlite3.Error, ValueError):
            record.update(status="error" if record["request_ids"] else "blocked", reason="browser_storage_unavailable")
        return self._save(record)

    def collect(self, client_id, query, country="us", language="en", location="", device="desktop"):
        cohort = _validate_cohort(client_id, query, country, language, location, device)
        timestamp = _stamp(self.clock())
        config, reason = self._config()
        record = {"id": uuid.uuid4().hex, **cohort, "observed_at": timestamp, "source": "provider",
                  "provider": PROVIDER, "status": "blocked", "reason": reason, "answer": "", "citations": [],
                  "request_ids": [], "request_provenance": [], "provider_search_ids": [], "provider_location_used": ""}
        if not config:
            return self._save(record)
        params = {"engine": "google", "q": cohort["query"], "gl": country, "hl": language, "device": device,
                  "no_cache": "true", "async": "false"}
        if location:
            params["location"] = location
        try:
            first = self._request(config, "google", params, record["request_ids"], record["request_provenance"])
            if first is None:
                record["reason"] = "daily_cap_reached"
                return self._save(record)
            first_meta = first.get("search_metadata")
            if (not isinstance(first_meta, dict) or first_meta.get("status") != "Success"
                    or first.get("error")):
                raise ValueError("provider_search_not_successful")
            used = first.get("search_parameters")
            if not isinstance(used, dict) or any(used.get(key) != expected for key, expected in
                                                  (("q", cohort["query"]), ("gl", country), ("hl", language),
                                                   ("device", device))):
                raise ValueError("provider_cohort_mismatch")
            responses = [first]
            overview = first.get("ai_overview")
            if isinstance(overview, dict) and isinstance(overview.get("page_token"), str) and not overview.get("text_blocks"):
                token = overview["page_token"]
                if not 1 <= len(token) <= 8000:
                    raise ValueError("invalid_page_token")
                second = self._request(config, "google_ai_overview",
                                       {"engine": "google_ai_overview", "page_token": token,
                                        "no_cache": "true", "async": "false"}, record["request_ids"], record["request_provenance"])
                if second is None:
                    record["reason"] = "daily_cap_reached_before_followup"
                    return self._save(record)
                responses.append(second)
                overview = second.get("ai_overview")
            for response in responses:
                meta = response.get("search_metadata")
                if not isinstance(meta, dict) or meta.get("status") != "Success":
                    raise ValueError("provider_search_not_successful")
                ident = _text(meta.get("id"), 100)
                if ident:
                    record["provider_search_ids"].append(ident)
                if response.get("error"):
                    raise ValueError("provider_reported_error")
            search_parameters = first.get("search_parameters")
            if isinstance(search_parameters, dict):
                record["provider_location_used"] = _text(search_parameters.get("location_used"), 200)
            if isinstance(overview, dict) and overview.get("error"):
                raise ValueError("overview_unavailable")
            if overview is None:
                if len(responses) > 1:
                    raise ValueError("missing_followup_overview")
                record.update(status="absent", reason="no_ai_overview_in_successful_search")
            elif not isinstance(overview, dict):
                raise ValueError("invalid_ai_overview")
            else:
                answer = _blocks(overview.get("text_blocks")) or _text(overview.get("snippet"))
                if not answer:
                    raise ValueError("empty_ai_overview")
                record.update(status="present", reason="", answer=answer,
                              citations=_references(overview.get("references")))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            record.update(status="error", reason="provider_or_response_error")
        record["observed_at"] = _stamp(self.clock())
        return self._save(record)

    def import_observation(self, client_id, payload, source="manual"):
        """Store an explicitly attributed browser observation without claiming API provenance."""
        if source != "manual" or not isinstance(payload, dict):
            raise ValueError("Only explicit manual observations may be imported")
        cohort = _validate_cohort(client_id, payload.get("query"), payload.get("country"),
                                  payload.get("language"), payload.get("location", ""), payload.get("device"))
        timestamp = payload.get("observed_at")
        try:
            moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = _stamp(moment)
        except (AttributeError, ValueError):
            raise ValueError("Manual observation needs a dated time") from None
        if moment.astimezone(timezone.utc) > _utc(self.clock()) + timedelta(minutes=5):
            raise ValueError("Manual observation time is in the future")
        if payload.get("status") not in ("present", "absent"):
            raise ValueError("Manual observation must explicitly report present or absent")
        evidence_url = _public_url(payload.get("evidence_url"))
        evidence_note = _text(payload.get("evidence_note"), 1000)
        if not evidence_url and not evidence_note:
            raise ValueError("Manual observation needs evidence provenance")
        answer = _text(payload.get("answer"))
        if payload["status"] == "present" and not answer:
            raise ValueError("Present manual observation needs observed answer text")
        citations = _references([{"link": c.get("url"), "title": c.get("title"), "snippet": c.get("snippet"),
                                 "source": c.get("source")} for c in payload.get("citations", []) if isinstance(c, dict)],
                                verification="manually_reported")
        record = {"id": uuid.uuid4().hex, **cohort, "observed_at": timestamp, "source": "manual",
                  "provider": "Google Search manual browser observation", "status": payload["status"], "reason": "",
                  "answer": answer if payload["status"] == "present" else "", "citations": citations if payload["status"] == "present" else [],
                  "evidence_url": evidence_url, "evidence_note": evidence_note, "request_ids": [],
                  "request_provenance": [], "provider_search_ids": []}
        with self._db() as db:
            row = db.execute("SELECT payload FROM observations WHERE client_id=? AND query=? AND country=? AND language=? AND location=? AND device=? AND observed_at=? AND source=?",
                             (*cohort.values(), timestamp, source)).fetchone()
            if row:
                return json.loads(row[0])
        return self._save(record)

    def history(self, client_id, query, country="us", language="en", location="", device="desktop", limit=90):
        cohort = _validate_cohort(client_id, query, country, language, location, device)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid history limit")
        with self._db() as db:
            rows = db.execute("SELECT payload FROM observations WHERE client_id=? AND query=? AND country=? AND language=? AND location=? AND device=? ORDER BY observed_at DESC LIMIT ?",
                              (*cohort.values(), limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete_client(self, client_id):
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id):
            raise ValueError("Invalid client ID")
        with self._db() as db:
            db.execute("DELETE FROM observations WHERE client_id=?", (client_id,))

    def summarize(self, client_id, query, country="us", language="en", location="", device="desktop", days=30):
        cohort = _validate_cohort(client_id, query, country, language, location, device)
        if type(days) is not int or not 7 <= days <= 365:
            raise ValueError("Summary window must be 7–365 days")
        current = _utc(self.clock())
        cutoff = (current - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        latest = (current + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        with self._db() as db:
            rows = db.execute("SELECT payload FROM observations WHERE client_id=? AND query=? AND country=? AND language=? AND location=? AND device=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at DESC",
                              (*cohort.values(), cutoff, latest)).fetchall()
        observations = [json.loads(row[0]) for row in rows]
        valid = [item for item in observations if item["status"] in ("present", "absent")]
        by_source_day = {"browser": {}, "provider": {}, "manual": {}}
        for item in valid:
            if item.get("source") in by_source_day:
                by_source_day[item["source"]].setdefault(item["observed_at"][:10], item)
        source_breakdown = {}
        for source, samples in by_source_day.items():
            source_present = sum(item["status"] == "present" for item in samples.values())
            source_breakdown[source] = {"sample_days": len(samples), "present_days": source_present,
                                        "absent_days": len(samples) - source_present,
                                        "appearance_rate": round(source_present / len(samples), 3) if samples else None}
        if len(by_source_day["browser"]) >= 7:
            basis_source = "browser"
        elif len(by_source_day["provider"]) >= 7:
            basis_source = "provider"
        elif len(by_source_day["manual"]) >= 7:
            basis_source = "manual"
        else:
            basis_source = max(by_source_day, key=lambda source: len(by_source_day[source]))
        series = list(reversed(list(by_source_day[basis_source].values())))
        present = [item for item in series if item["status"] == "present"]
        counts = Counter(c["url"] for item in present for c in item["citations"])
        citation_meta = {c["url"]: c for item in present for c in item["citations"]}
        citations = [{**citation_meta[url], "days_cited": count, "frequency": round(count / len(present), 3)}
                     for url, count in counts.most_common()]
        transitions = []
        for previous, current in zip(series, series[1:]):
            before = {c["url"] for c in previous["citations"]} if previous["status"] == "present" else set()
            after = {c["url"] for c in current["citations"]} if current["status"] == "present" else set()
            transitions.append({"from_date": previous["observed_at"][:10], "to_date": current["observed_at"][:10],
                                "retained": sorted(before & after), "added": sorted(after - before), "lost": sorted(before - after)})
        terms = Counter(word.lower() for item in present for word in set(re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", item["answer"]))
                        if word.lower() not in _STOP)
        common_terms = [{"term": term, "days_seen": count} for term, count in terms.most_common(15) if count >= 2]
        return {"cohort": cohort, "window_days": days, "basis_source": basis_source,
                "source_breakdown": source_breakdown,
                "total_observed_days": len({item["observed_at"][:10] for item in valid}),
                "sample_days": len(series), "enough_history": len(series) >= 7,
                "present_days": len(present), "absent_days": len(series) - len(present),
                "error_samples": sum(item["status"] == "error" for item in observations),
                "blocked_samples": sum(item["status"] == "blocked" for item in observations),
                "appearance_rate": round(len(present) / len(series), 3) if series else None,
                "citations": citations, "citation_changes": transitions, "common_terms": common_terms,
                "daily_evidence": [{"date": item["observed_at"][:10], "status": item["status"], "evidence_id": item["id"],
                                    "source": item["source"], "answer": item["answer"]} for item in series],
                "limitations": ["One latest valid sample per UTC day for this exact query, locale and source cohort.",
                                "Browser, provider and manual observations are kept separate for history comparisons.",
                                "Provider-reported citations have not been fetched or checked for factual support.",
                                "Common terms are deterministic word counts, not topic inference."]}


def build_ai_overview_brief(summary):
    """Editorial leads backed by observed text and citations; no invented claims."""
    if not isinstance(summary, dict) or not summary.get("daily_evidence"):
        return {"status": "insufficient_evidence", "observed_sources": [], "terms": [], "suggestions": []}
    sources = [{"url": c["url"], "title": c["title"], "days_cited": c["days_cited"]}
               for c in summary.get("citations", [])[:10]]
    terms = summary.get("common_terms", [])[:10]
    suggestions = []
    if summary.get("enough_history") and sources:
        suggestions.append({"type": "source_review", "label": "Review repeatedly cited sources for coverage and attribution",
                            "evidence_urls": [c["url"] for c in sources[:5]],
                            "inference": True})
    return {"status": "observed_history" if summary.get("enough_history") else "early_sample",
            "observed_sources": sources, "terms": terms, "suggestions": suggestions,
            "disclaimer": "Editorial suggestions are labeled inference from the cited observations; source claims require independent review."}
