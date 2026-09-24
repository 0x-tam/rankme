"""Persisted queue, weekly scheduling and the inspection-to-publication workflow."""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .store import now, uid
from .quality import check_article


def parse_date(value):
    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Dates must include a timezone")
    return date.astimezone(timezone.utc)


def first_run():
    return (datetime.now().astimezone() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def content_digest(article):
    fields = {k: article.get(k) for k in ("title", "slug", "description", "body", "sources", "claims")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def profile_digest(client):
    return hashlib.sha256(json.dumps({"profile": client.get("profile"), "url": client.get("url")}, sort_keys=True).encode()).hexdigest()


def brand_digest(client):
    brand = client.get("image_brand") or {}
    fields = {"colors": brand.get("colors", []), "style": brand.get("style", ""), "audience": client.get("profile", {}).get("audience", ""), "tone": client.get("profile", {}).get("tone", "")}
    # Direction fields join the digest only once set, so covers reviewed before they existed stay valid.
    # Learned consistency aids (references, style_spec) refine the house style without invalidating approved covers.
    fields.update({key: brand[key] for key in ("mode", "mood", "subjects", "avoid") if brand.get(key)})
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def safe_error(error):
    message = str(error)[:1500] or type(error).__name__
    message = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", message)
    message = re.sub(r"(?i)(token|api[_-]?key|password|authorization)([=: ]+)[^\s,;]+", r"\1\2[redacted]", message)
    return message


class Engine:
    def __init__(self, store, data_dir, runner_factory=None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runner_factory = runner_factory
        from .google_data import GoogleData
        from .vercel import Vercel
        self.google = GoogleData(self.data_dir)
        self.vercel = Vercel(self.data_dir)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.RLock()
        from .measurement import backfill
        backfill(self.store)
        self.worker = None
        self.codex_status = {"available": False, "authenticated": False, "method": "", "message": "Checking Codex sign-in…"}

    def runner(self):
        settings = self.store.settings()
        if self.runner_factory:
            return self.runner_factory()
        from .ai import CodexRunner
        return CodexRunner(self.data_dir / "runs", executable=settings["codex_path"], model=settings["model"])

    def check_status(self):
        try:
            self.codex_status = self.runner().status()
        except Exception as exc:
            self.codex_status = {"available": False, "authenticated": False, "method": "", "message": str(exc)[:500]}
        return self.codex_status

    def start(self):
        # Never silently repeat a possibly completed external publication after a crash.
        for job in self.store.all("jobs"):
            if job["status"] == "running":
                self.store.update("jobs", job["id"], status="failed", error="RankMe stopped during this job. Review its state and retry.")
                if job.get("opportunity_id"):
                    opportunity = self.store.get("opportunities", job["opportunity_id"])
                    if opportunity["client_id"] == job["client_id"] and opportunity.get("status") != "completed":
                        self.store.update("opportunities", opportunity["id"], status="failed", executable=True,
                                          error="Interrupted job; retry to continue without duplicating completed work.")
                self.store.event("Interrupted job needs attention: " + job["kind"], job.get("client_id"), "warning")
        self.worker = threading.Thread(target=self.loop, name="rankme-worker", daemon=True)
        self.worker.start()
        threading.Thread(target=self.check_status, name="rankme-codex-status", daemon=True).start()

    def queue(self, kind, client_id, article_id=None, **extra):
        if kind not in ("inspect", "plan", "generate", "review", "cover", "publish", "verify", "seo-sync", "backlinks-discover", "backlink-check", "visibility-audit", "visibility-research", "visibility-action", "visibility-probe", "style", "brand-guide"):
            raise ValueError("Unknown job type")
        self.store.get("clients", client_id)
        with self.guard:
            pending = [j for j in self.store.all("jobs") if j["status"] in ("queued", "running") and j["client_id"] == client_id]
            if pending:
                raise ValueError("This client already has a queued or running job. Wait for it to finish.")
            if article_id:
                article = self.store.get("articles", article_id)
                if article["client_id"] != client_id:
                    raise ValueError("Article does not belong to this client")
                if kind in ("generate", "publish") and article.get("refresh_of"):
                    from .measurement import active_for
                    original = self.store.get("articles", article["refresh_of"])
                    page = original.get("publish_result", {}).get("live_url")
                    if any(r.get("article_id") != article_id for r in active_for(self.store, client_id, page)):
                        raise ValueError("This page has an active experiment; wait before another refresh")
            if kind == "visibility-action":
                opportunity = self.store.get("opportunities", extra.get("opportunity_id"))
                if opportunity["client_id"] != client_id:
                    raise ValueError("Opportunity does not belong to this client")
            job = self.store.put("jobs", {"kind": kind, "client_id": client_id, "article_id": article_id,
                                         "status": "queued", "stage": "Waiting", "error": "", "attempts": 0, **extra})
            for previous in self.store.all("jobs"):
                same_work = previous["kind"] == kind or (kind == "generate" and previous["kind"] in ("cover", "review")) or (kind in ("publish", "cover") and previous["kind"] == "generate" and article_id)
                if kind == "backlink-check" and previous.get("backlink_id") != extra.get("backlink_id"):
                    same_work = False
                if kind == "visibility-action" and previous.get("opportunity_id") != extra.get("opportunity_id"):
                    same_work = False
                if previous["status"] == "failed" and previous["client_id"] == client_id and previous.get("article_id") == article_id and same_work:
                    self.store.update("jobs", previous["id"], status="retried")
            self.store.event(kind.capitalize() + " queued", client_id)
            self.wake.set()
            return job

    STOPPABLE_WHILE_RUNNING = ("inspect", "plan", "generate", "review", "cover", "seo-sync", "backlinks-discover", "backlink-check",
                               "visibility-audit", "visibility-research", "visibility-action", "visibility-probe", "style", "brand-guide")

    def stop(self, ident):
        """Stop a queued or running job and return its article or website to a state the user can act on."""
        from . import cancel
        with self.guard:
            job = self.store.get("jobs", ident)
            if job["status"] == "queued":
                self.unwind(job)
                return self.store.get("jobs", ident)
            if job["status"] != "running":
                raise ValueError("Only queued or running work can be stopped")
            if job["kind"] not in self.STOPPABLE_WHILE_RUNNING:
                raise ValueError("Publishing and live checks cannot be stopped midway. Wait a moment for them to finish.")
            if not cancel.request():
                raise ValueError("This article is being published to the live website right now and cannot be stopped safely.")
            self.store.update("jobs", ident, stage="Stopping…")
            return self.store.get("jobs", ident)

    def unwind(self, job):
        """Mark a job stopped and undo the in-progress states it set."""
        self.store.update("jobs", job["id"], status="cancelled", stage="Stopped by you", finished_at=now(), error="")
        client = self.store.get("clients", job["client_id"])
        if job.get("article_id"):
            try:
                article = self.store.get("articles", job["article_id"])
            except KeyError:
                article = None
            if article and article["status"] in ("writing", "reviewing", "generating_cover"):
                # Written drafts wait for review again; unwritten topics return to the plan.
                self.store.update("articles", article["id"], status="held" if article.get("body") else "planned", error="")
        if job["kind"] in ("inspect", "plan") and client.get("status") in ("inspecting", "planning", "new"):
            status = ("active" if client.get("automation") else "ready") if client.get("confirmed") else "needs_confirmation" if client.get("profile") else "new"
            self.store.update("clients", client["id"], status=status, error="")
        if job.get("scheduled") and job["kind"] in ("plan", "generate") and client.get("automation"):
            self.advance(client["id"])  # Otherwise the schedule would restart the same work on its next tick.
        self.store.event("Stopped: " + job["kind"].replace("-", " "), client["id"], "warning")

    def dismiss(self, ident):
        """Clear a failed job so it no longer blocks this website's weekly schedule."""
        with self.guard:
            job = self.store.get("jobs", ident)
            if job["status"] != "failed":
                raise ValueError("Only failed jobs can be dismissed")
            return self.store.update("jobs", ident, status="dismissed", stage="Dismissed")

    def retry(self, ident):
        job = self.store.get("jobs", ident)
        if job["status"] != "failed":
            raise ValueError("Only failed jobs can be retried")
        kind = job["kind"]
        resume = job.get("resume_generation", False)
        if job.get("article_id"):
            article = self.store.get("articles", job["article_id"])
            if kind == "generate" and article.get("publish_started"):
                kind = "publish"
            elif kind == "generate" and article.get("reviewed_digest") == content_digest(article):
                kind, resume = "cover", True
        result = self.queue(kind, job["client_id"], job.get("article_id"), scheduled=job.get("scheduled", False), resume_generation=resume, backlink_id=job.get("backlink_id"), opportunity_id=job.get("opportunity_id"))
        if kind == "visibility-action":
            self.store.update("opportunities", job["opportunity_id"], status="queued", job_id=result["id"], executable=False)
        self.store.update("jobs", ident, status="retried")
        return result

    def articles(self, client_id):
        return sorted([a for a in self.store.all("articles") if a["client_id"] == client_id], key=lambda a: a.get("scheduled_at", ""))

    def comparison_articles(self, client_id, article):
        """Exclude only validated ancestors of a refresh from duplicate checks."""
        excluded, current = set(), article
        while current.get("refresh_of"):
            ident = current["refresh_of"]
            if ident in excluded or len(excluded) >= 100:
                raise ValueError("Invalid refresh ancestry")
            current = self.store.get("articles", ident)
            if current["client_id"] != client_id:
                raise ValueError("Refresh source does not belong to this client")
            excluded.add(ident)
        return [a for a in self.articles(client_id) if a["id"] not in excluded]

    def schedule_tick(self):
        if self.store.settings()["paused"]:
            return
        current = datetime.now(timezone.utc)
        for article in self.store.all("articles"):
            if article["status"] != "verification_pending" or article.get("verification_attempts", 0) >= 12:
                continue
            client = self.store.get("clients", article["client_id"])
            on_vercel = client.get("connection", {}).get("provider") == "vercel"
            if not client.get("automation") and not on_vercel:
                continue
            if (article.get("publish_result") or {}).get("deployment", {}).get("state") in ("ERROR", "CANCELED"):
                continue  # A failed build needs the user; rechecking cannot fix it.
            last = article.get("last_verified_at") or article.get("updated_at")
            if last and (current - parse_date(last)).total_seconds() < (90 if on_vercel else 300):
                continue
            try:
                self.queue("verify", client["id"], article["id"])
            except ValueError:
                pass
        for client in self.store.all("clients"):
            if not client.get("automation") or not client.get("confirmed") or not client.get("subject"):
                continue
            if client["status"] not in ("active", "ready"):
                continue
            try:
                due = parse_date(client.get("next_run", first_run()))
            except (ValueError, TypeError):
                continue
            if due > current:
                continue
            if any(j["client_id"] == client["id"] and (j["status"] in ("queued", "running") or (j["status"] == "failed" and j["kind"] not in ("seo-sync", "backlinks-discover", "backlink-check", "visibility-audit", "visibility-research", "visibility-action", "visibility-probe", "style", "brand-guide"))) for j in self.store.all("jobs")):
                continue
            planned = [a for a in self.articles(client["id"]) if a["status"] == "planned"]
            if planned:
                from .measurement import active_for
                eligible = []
                for draft in planned:
                    if draft.get("refresh_of"):
                        original = self.store.get("articles", draft["refresh_of"])
                        if active_for(self.store, client["id"], original.get("publish_result", {}).get("live_url")):
                            continue
                    eligible.append(draft)
                if not eligible:
                    continue
                planned = eligible
            try:
                if planned:
                    if planned[0].get("scheduled_at") and parse_date(planned[0]["scheduled_at"]) > current:
                        continue
                    self.queue("generate", client["id"], planned[0]["id"], scheduled=True)
                else:
                    self.queue("plan", client["id"], scheduled=True)
            except ValueError:
                pass

    def data_schedule_tick(self):
        """Refresh connected metrics daily and known links weekly while RankMe runs."""
        if self.store.settings()["paused"]:
            return
        current = datetime.now(timezone.utc)
        for client in self.store.all("clients"):
            if any(j["client_id"] == client["id"] and j["status"] in ("queued", "running") for j in self.store.all("jobs")):
                continue
            connection = client.get("seo_connection", {})
            last = client.get("seo_attempt_at")
            if connection.get("auto_sync", True) and connection.get("site_url") and self.google.status().get("connected") and (not last or (current - parse_date(last)).total_seconds() >= 86400):
                self.queue("seo-sync", client["id"])
                continue
            for link in self.store.all("backlinks"):
                if link.get("client_id") != client["id"] or link.get("status") == "dismissed":
                    continue
                checked = link.get("last_check_attempt") or link.get("created_at")
                if checked and (current - parse_date(checked)).total_seconds() >= 7 * 86400:
                    self.queue("backlink-check", client["id"], backlink_id=link["id"])
                    break

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.schedule_tick()
                self.data_schedule_tick()
                from .autopilot import schedule
                schedule(self)
                queued = sorted([j for j in self.store.all("jobs") if j["status"] == "queued"], key=lambda j: j["created_at"])
                if queued and not self.store.settings()["paused"]:
                    self.execute(queued[0])
                    continue
            except Exception as exc:
                self.store.event("Worker error: " + safe_error(exc), level="error")
            self.wake.wait(15)
            self.wake.clear()

    def advance(self, client_id):
        # One catch-up article only. No flood of old publications after a long shutdown.
        client = self.store.get("clients", client_id)
        current = datetime.now(timezone.utc)
        try:
            due = parse_date(client["next_run"])
        except (ValueError, KeyError):
            due = current
        while due <= current:
            due += timedelta(days=7)
        self.store.update("clients", client_id, next_run=due.isoformat())

    def execute(self, job):
        from . import ai, crawler
        client_id = job["client_id"]
        article_id = job.get("article_id")
        if job.get("scheduled") and job["kind"] in ("plan", "generate", "cover", "publish", "verify"):
            current = self.store.get("clients", client_id)
            if not current.get("automation") or current.get("status") == "paused" or self.store.settings()["paused"]:
                self.store.update("jobs", job["id"], status="cancelled", stage="Paused before execution", finished_at=now(), error="")
                return
        from . import cancel
        cancel.reset()
        self.store.update("jobs", job["id"], status="running", started_at=now(), attempts=job.get("attempts", 0) + 1)

        def progress(message):
            cancel.check()  # Every progress step is a safe point to stop at.
            self.store.update("jobs", job["id"], stage=str(message)[:400])

        try:
            client = self.store.get("clients", client_id)
            if job["kind"].startswith("visibility-"):
                from .autopilot import execute as visibility_execute
                visibility_execute(self, job, progress)
            elif job["kind"] == "seo-sync":
                self.store.update("clients", client_id, seo_attempt_at=now())
                connection = client.get("seo_connection", {})
                if not connection.get("site_url"):
                    raise ValueError("Select a Search Console property first")
                progress("Reading Search Console and analytics performance")
                goal = client.get("conversion_goal") or {}
                snapshot = self.google.sync(connection["site_url"], connection.get("ga4_property", ""),
                                            goal_config={k: goal[k] for k in ("event_name", "landing_page", "goal_type") if k in goal})
                from .measurement import save_snapshot, evaluate_all
                observed = save_snapshot(self.store, "seo", client_id, snapshot, job["id"])
                evaluate_all(self, client_id, observed["snapshot"], observed["id"])
                self.store.update("clients", client_id, seo_error="")
            elif job["kind"] == "backlinks-discover":
                from .backlinks import discover_prospects
                progress("Researching relevant backlink opportunities")
                prospects = discover_prospects(self.runner(), client, progress=progress)
                existing = {b["source_url"] for b in self.store.all("backlinks") if b["client_id"] == client_id}
                for prospect in prospects:
                    if prospect["source_url"] not in existing:
                        self.store.put("backlinks", {**prospect, "id": uid(), "client_id": client_id, "target_url": client["url"]})
                        existing.add(prospect["source_url"])
                self.store.event(str(len(prospects)) + " backlink prospects researched. Outreach remains drafts.", client_id)
            elif job["kind"] == "backlink-check":
                from .backlinks import verify_backlink
                link = self.store.get("backlinks", job["backlink_id"])
                if link["client_id"] != client_id:
                    raise ValueError("Link does not belong to this client")
                self.store.update("backlinks", link["id"], last_check_attempt=now())
                progress("Checking the public page for the backlink")
                result = verify_backlink(link["source_url"], link.get("target_url") or client["url"])
                fields = {"verification": result}
                if result.get("status") == "missing" and link.get("status") == "live":
                    fields["status"] = "prospect"
                if result.get("live"):
                    fields["first_seen_at"] = link.get("first_seen_at") or result["checked_at"]
                    fields["last_seen_at"] = result["checked_at"]
                self.store.update("backlinks", link["id"], **fields)
            elif job["kind"] == "inspect":
                self.store.update("clients", client_id, status="inspecting", error="")
                progress("Inspecting public website pages")
                crawl = crawler.crawl_site(client["url"], max_pages=self.store.settings()["max_pages"], progress=progress)
                if not crawl.get("pages"):
                    raise ValueError("No readable pages found. Check the URL, robots policy, or website access.")
                self.store.update("clients", client_id, crawl=crawl, crawl_observed_at=now(), visibility_attempt_at=now())
                from .autopilot import rebuild
                rebuild(self, client_id)
                progress("Building the company profile and suggested subjects")
                profile = ai.inspect_business(self.runner(), crawl, progress=progress)
                if not client.get("image_brand", {}).get("colors"):
                    from .brand import extract_brand
                    try:
                        self.store.update("clients", client_id, image_brand=extract_brand(client["url"]))
                    except (ValueError, OSError):
                        self.store.event("Brand colors could not be detected. Add them in the business profile before generating a cover.", client_id, "warning")
                self.store.update("clients", client_id, profile=profile, name=profile.get("name") or client["name"],
                                  status="needs_confirmation", confirmed=False, automation=False, error="")
            elif job["kind"] == "plan":
                if not client.get("confirmed") or not client.get("subject"):
                    raise ValueError("Confirm the business profile and choose a subject first")
                progress("Researching a connected 12-week content plan")
                self.store.update("clients", client_id, status="planning", error="")
                existing = self.articles(client_id)
                try:
                    metrics = self.store.get("seo", client_id)
                    search = metrics.get("search_console") or {}
                    evidence = {"periods": metrics.get("periods"), "updated_at": metrics.get("updated_at"), "recommendations": metrics.get("recommendations", [])[:6],
                        "note": "Historical connected-site data; check dates for freshness. Top rows are not exhaustive or market search volumes.",
                        "search_console": {period: {"totals": search.get(period, {}).get("totals"),
                            "queries": search.get(period, {}).get("queries", [])[:50], "pages": search.get(period, {}).get("pages", [])[:50]}
                            for period in ("current", "previous")}}
                    client = {**client, "seo_evidence": evidence}
                except KeyError:
                    pass
                plan = ai.plan_articles(self.runner(), client, existing, progress=progress)
                items = plan.get("articles", [])
                if not items or len(items) > 12:
                    raise ValueError("The planner did not return a valid content plan")
                due = parse_date(client.get("next_run") or first_run())
                old_topics = [a for a in existing if a["status"] == "planned" and a.get("subject") != client["subject"]]
                existing_dates = [parse_date(a["scheduled_at"]) for a in existing if a.get("scheduled_at") and a["status"] == "planned" and a not in old_topics]
                if existing_dates:
                    due = max(due, max(existing_dates) + timedelta(days=7))
                titles = {a.get("title", "").lower().strip() for a in existing}
                count = 0
                for item in items:
                    if not isinstance(item, dict) or not item.get("title") or item["title"].lower().strip() in titles:
                        continue
                    titles.add(item["title"].lower().strip())
                    self.store.put("articles", {**item, "id": uid(), "client_id": client_id, "status": "planned", "subject": client["subject"],
                                                "scheduled_at": (due + timedelta(days=count * 7)).isoformat(), "body": "", "error": ""})
                    count += 1
                if not count:
                    raise ValueError("No distinct article topics found. Choose a more specific subject.")
                for old in old_topics:
                    self.store.update("articles", old["id"], status="superseded")
                self.store.update("clients", client_id, status="active" if self.store.get("clients", client_id).get("automation") else "ready", error="")
                self.store.event("Content plan ready: " + str(count) + " articles", client_id)
            elif job["kind"] in ("generate", "review"):
                if not client.get("confirmed"):
                    raise ValueError("Confirm the business profile before producing content")
                article = self.store.get("articles", article_id)
                if article.get("publish_started") or article["status"] in ("published", "verification_pending", "exported"):
                    raise ValueError("Published/exported articles cannot be regenerated in place. Create a new draft.")
                existing = self.comparison_articles(client_id, article)
                if article.get("refresh_of"):
                    original = self.store.get("articles", article["refresh_of"])
                    if original["client_id"] != client_id:
                        raise ValueError("Refresh source does not belong to this client")
                runner = self.runner()
                feedback = ""
                tries = 2 if job["kind"] == "generate" else 1
                for attempt in range(tries):
                    if job["kind"] == "generate":
                        self.store.update("articles", article_id, status="writing", error="")
                        progress("Researching and writing" if not attempt else "Repairing review findings")
                        draft = ai.write_article(runner, client, article, existing, feedback=feedback, progress=progress)
                        allowed = {k: draft[k] for k in ("title", "slug", "description", "body", "sources", "claims", "internal_links") if k in draft}
                        if article.get("refresh_of"):
                            allowed["slug"] = original["slug"]
                        article = self.store.update("articles", article_id, **allowed)
                        self._snapshot(article)
                    self.store.update("articles", article_id, status="reviewing")
                    progress("Checking claims, sources, relevance, and article structure")
                    checks = check_article(article, client, existing)
                    review = ai.review_article(runner, client, article, progress=progress)
                    passed = checks["passed"] and review.get("passed") is True and not review.get("issues")
                    article = self.store.update("articles", article_id, checks=checks, review=review,
                                                reviewed_digest=content_digest(article) if passed else "",
                                                reviewed_profile=profile_digest(client) if passed else "",
                                                status="ready" if passed else "held")
                    if passed:
                        break
                    feedback = json.dumps({"structural": checks["issues"], "review": review}, ensure_ascii=False)
                if article["status"] == "held":
                    self.store.event("Article held: " + article["title"] + ". Review its findings before retrying.", client_id, "warning")
                else:
                    latest = self.store.get("clients", client_id)
                    article = self.prepare_cover(latest, article, progress)
                    latest = self.store.get("clients", client_id)
                    if article["status"] == "ready" and latest.get("connection", {}).get("auto_publish") and latest.get("status") != "paused" and not self.store.settings()["paused"]:
                        self.publish(latest, article, progress)
                if job.get("scheduled"):
                    self.advance(client_id)
            elif job["kind"] == "brand-guide":
                from .brand_guide import scan
                from .covers import analyze_brand
                brand = client.get("image_brand") or {}
                url = (brand.get("site") or {}).get("url") or client["url"]
                progress("Scanning " + url + " for brand images and colors")
                site = scan(url, self.data_dir, client_id, progress)
                progress("Studying the brand's imagery to build the cover guide")
                from .brand_guide import image_path
                images = [image_path(self.data_dir, client_id, item) for item in site["images"]]
                analysis = analyze_brand(self.runner(), client, [path for path in images if path], site["css_colors"], url)
                latest = self.store.get("clients", client_id).get("image_brand") or {}
                guide = {**site, "scanned_at": now(), "analysis": analysis}
                # The guide fills gaps; anything the user already wrote stays theirs.
                filled = {key: analysis[key] for key in ("mood", "subjects", "avoid") if analysis.get(key) and not latest.get(key)}
                if analysis["colors"] and not latest.get("colors"):
                    filled["colors"] = analysis["colors"]
                self.store.update("clients", client_id, image_brand={**latest, **filled, "site": guide})
                self.store.event("Brand guide ready from " + url, client_id)
            elif job["kind"] == "style":
                from .covers import describe_style, reference_paths
                progress("Learning this website's cover style from its approved references")
                brand = client.get("image_brand") or {}
                wanted = [r.get("sha256") for r in brand.get("references", [])]
                spec = describe_style(self.runner(), client, reference_paths(client, self.data_dir))
                latest = self.store.get("clients", client_id).get("image_brand") or {}
                # References may change while the job runs; only a spec for the current set is kept.
                if [r.get("sha256") for r in latest.get("references", [])] == wanted:
                    self.store.update("clients", client_id, image_brand={**latest, "style_spec": spec})
            elif job["kind"] == "cover":
                article = self.store.get("articles", article_id)
                if not client.get("confirmed") or not article.get("body"):
                    raise ValueError("Confirm the profile and write an article before generating its cover")
                if article.get("publish_started") or article["status"] in ("published", "exported", "verification_pending"):
                    raise ValueError("Published article covers are preserved")
                article = self.prepare_cover(client, article, progress, force=not job.get("resume_generation"))
                if job.get("resume_generation"):
                    latest = self.store.get("clients", client_id)
                    if article["status"] == "ready" and latest.get("connection", {}).get("auto_publish") and latest.get("status") != "paused" and not self.store.settings()["paused"]:
                        self.publish(latest, article, progress)
                    if job.get("scheduled"):
                        self.advance(client_id)
            elif job["kind"] == "publish":
                self.publish(client, self.store.get("articles", article_id), progress)
                if job.get("scheduled"):
                    self.advance(client_id)
            elif job["kind"] == "verify":
                from .publisher import verify_live
                article = self.store.get("articles", article_id)
                result = article.get("publish_result", {})
                if not result.get("live_url"):
                    raise ValueError("No live article URL is configured")
                connection = client.get("connection", {})
                if connection.get("provider") == "vercel" and result.get("commit"):
                    link = connection.get("vercel", {})
                    progress("Checking the Vercel deployment")
                    deployment = self.vercel.deployment(link.get("team_id", ""), link.get("project_id", ""), result["commit"])
                    result = {**result, "deployment": deployment or {"state": "WAITING"}}
                    state = (deployment or {}).get("state", "WAITING")
                    if state != "READY":
                        failed = state in ("ERROR", "CANCELED")
                        message = ("Vercel build failed. Open the deployment in Vercel, fix the site build, redeploy, then check again." if failed
                                   else "Vercel is building the site." if deployment else "Waiting for Vercel to start the build.")
                        self.store.update("articles", article_id, publish_result={**result, "message": message}, last_verified_at=now(),
                                          verification_attempts=article.get("verification_attempts", 0) + (0 if failed else 1))
                        self.store.update("jobs", job["id"], status="completed", stage="Complete", finished_at=now(), error="")
                        if failed:
                            self.store.event("Vercel build failed for " + article["title"], client_id, "error")
                        return
                progress("Verifying the live article")
                verified = verify_live(result["live_url"], article["title"])
                result = {**result, "status": "published" if verified.get("ok") else "verification_pending", "message": verified.get("message", "")}
                self.store.update("articles", article_id, verification=verified,
                                  publish_result=result, last_verified_at=now(), verification_attempts=article.get("verification_attempts", 0) + 1,
                                  status="published" if verified.get("ok") else "verification_pending")
                if verified.get("ok"):
                    self.store.update("articles", article_id, published_at=article.get("published_at") or now())
                from .measurement import record_publication
                record_publication(self, client, article, result)
            self.store.update("jobs", job["id"], status="completed", stage="Complete", finished_at=now(), error="")
            if job["kind"] in ("seo-sync", "backlinks-discover", "backlink-check", "inspect"):
                from .autopilot import rebuild
                rebuild(self, client_id)
            self.store.event(job["kind"].capitalize() + " completed", client_id)
        except cancel.JobCancelled:
            self.unwind(self.store.get("jobs", job["id"]))
        except Exception as exc:
            message = safe_error(exc)
            self.store.update("jobs", job["id"], status="failed", stage="Needs attention", finished_at=now(), error=message)
            if job["kind"] == "seo-sync":
                self.store.update("clients", client_id, seo_error=message)
            if job["kind"].startswith("visibility-"):
                self.store.update("clients", client_id, visibility_error=message)
                if job.get("opportunity_id"):
                    item = self.store.get("opportunities", job["opportunity_id"])
                    if item["client_id"] == client_id and item.get("status") != "completed":
                        self.store.update("opportunities", item["id"], status="failed", executable=True, error=message)
            if job["kind"] in ("inspect", "plan"):
                self.store.update("clients", client_id, status="error", error=message)
            elif article_id:
                article = self.store.get("articles", article_id)
                if article["status"] not in ("published", "exported", "verification_pending"):
                    self.store.update("articles", article_id, status="error", error=message)
            self.store.event(message, client_id, "error")

    def _snapshot(self, article):
        folder = self.data_dir / "revisions" / article["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (str(time.time_ns()) + ".json")).write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")

    def cover_valid(self, client, article):
        cover = article.get("cover") or {}
        if not cover.get("review", {}).get("passed") or cover.get("review", {}).get("issues"):
            return False
        if cover.get("reviewed_article_digest") != content_digest(article) or cover.get("reviewed_brand_digest") != brand_digest(client):
            return False
        try:
            path = Path(cover["path"])
            path.resolve().relative_to((self.data_dir / "covers").resolve())
            if path.is_symlink() or not 0 < path.stat().st_size <= 20_000_000:
                return False
            return hashlib.sha256(path.read_bytes()).hexdigest() == cover.get("sha256")
        except (ValueError, KeyError, OSError):
            return False

    def earlier_covers(self, client, article):
        """Covers of this website's other articles, newest first, for the one-image-per-article guardrail."""
        from .covers import fingerprint
        root = (self.data_dir / "covers").resolve()
        found = []
        for other in self.articles(client["id"]):
            cover = other.get("cover") or {}
            if other["id"] == article["id"] or other.get("status") == "superseded" or not cover.get("path"):
                continue
            path = Path(cover["path"]).resolve()
            if root not in path.parents or path.is_symlink() or not path.is_file():
                continue
            value = cover.get("fingerprint")
            if not value:
                value = fingerprint(path)
                if value:
                    self.store.update("articles", other["id"], cover={**cover, "fingerprint": value})
            found.append({"title": other.get("title", ""), "alt": cover.get("alt", ""), "fingerprint": value,
                          "path": str(path), "at": other.get("updated_at", "")})
        return sorted(found, key=lambda item: item["at"], reverse=True)

    def prepare_cover(self, client, article, progress, force=False):
        from .covers import generate_cover
        if not client.get("image_brand", {}).get("colors"):
            from .brand import extract_brand
            try:
                detected = extract_brand(client["url"])
            except (ValueError, OSError):
                detected = {}
            if not detected.get("colors"):
                raise ValueError("Add brand colors in the business profile before generating the cover. No reliable palette was detected.")
            client = self.store.update("clients", client["id"], image_brand=detected)
        if not force and self.cover_valid(client, article):
            return article
        self.store.update("articles", article["id"], status="generating_cover", error="")
        progress("Creating a cover in this website’s house style")
        cover = generate_cover(self.runner(), client, article, self.data_dir, progress=progress, previous=self.earlier_covers(client, article))
        cover.update(reviewed_article_digest=content_digest(article), reviewed_brand_digest=brand_digest(client))
        article = self.store.update("articles", article["id"], cover=cover, cover_stale=False)
        text_passed = article.get("review", {}).get("passed") and not article.get("review", {}).get("issues") and article.get("reviewed_digest") == content_digest(article) and article.get("reviewed_profile") == profile_digest(client)
        ready = bool(text_passed and self.cover_valid(client, article))
        article = self.store.update("articles", article["id"], status="ready" if ready else "held")
        self._snapshot(article)
        if not ready:
            self.store.event("Article held: its text or cover needs review.", client["id"], "warning")
        return article

    def publish(self, client, article, progress):
        from .publisher import publish_article
        from .measurement import active_for, record_publication
        if not client.get("confirmed") or not article.get("review", {}).get("passed") or article.get("reviewed_digest") != content_digest(article):
            raise ValueError("This article needs a successful review before publishing")
        if article.get("reviewed_profile") != profile_digest(client):
            raise ValueError("Business profile changed. Review the article against the current profile before publishing.")
        if not self.cover_valid(client, article):
            raise ValueError("Generate a reviewed cover matching the current article and brand before publishing")
        existing = self.comparison_articles(client["id"], article)
        if article.get("refresh_of"):
            original = self.store.get("articles", article["refresh_of"])
            if original["client_id"] != client["id"] or original.get("status") not in ("published", "refreshed"):
                raise ValueError("Refresh source is not a published article for this website")
            existing = [a for a in existing if a["id"] != original["id"]]
            page = original.get("publish_result", {}).get("live_url")
            if any(r.get("article_id") != article["id"] for r in active_for(self.store, client["id"], page)):
                raise ValueError("This page has an active experiment. Finish its observation before another refresh.")
        checks = check_article(article, client, existing)
        if not checks["passed"]:
            raise ValueError("Publication checks failed: " + "; ".join(checks["issues"]))
        connection = dict(client.get("connection") or {})
        if not connection.get("project_path"):
            path = self.data_dir / "exports" / client["id"]
            path.mkdir(parents=True, exist_ok=True)
            connection.update(project_path=str(path), content_dir="articles", mode="export", format="md", auto_publish=False, deploy_command=[], build_command=[])
        publish_client = {**client, "connection": connection}
        if not article.get("change_observation"):
            from .measurement import matching_measurement
            try:
                seo = self.store.get("seo", client["id"])
            except KeyError:
                seo = {}
            article = self.store.update("articles", article["id"], change_observation={
                "started_at": now(), "snapshot": {"conversion_measurement": matching_measurement(client, seo),
                    "ga4_property": seo.get("ga4_property")}})
        else:
            # A failed build may have rolled back the first attempt. Preserve the
            # pre-change baseline but exclude days before this later attempt too.
            article = self.store.update("articles", article["id"], change_observation={
                **article["change_observation"], "started_at": now()})
        self.store.update("articles", article["id"], status="publishing", publish_started=True, error="")
        from .measurement import observation
        self.store.put("measurements", observation(client["id"], "publication_attempt", {
            "article_id": article["id"], "refresh_of": article.get("refresh_of"),
            "content_digest": content_digest(article), "baseline": article.get("change_observation"),
            "note": "Publication started; inspect the later result before assuming the site changed."},
            article["id"] + ":" + article["change_observation"]["started_at"]))
        if connection.get("provider") == "vercel":
            from .vercel import sync_site
            progress("Updating RankMe’s copy of the website from GitHub")
            link = connection.get("vercel", {})
            sync_site(self.data_dir, client["id"], link.get("repo", ""), connection.get("branch", ""))
        progress("Publishing and verifying the article")
        from . import cancel
        with cancel.protected():
            # A half-finished push or deploy is worse than waiting: publishing cannot be stopped midway.
            result = publish_article(publish_client, article, progress=progress, data_dir=self.data_dir)
        self.store.update("articles", article["id"], status=result["status"], publish_result=result, error="")
        if result["status"] == "published":
            self.store.update("articles", article["id"], published_at=article.get("published_at") or now())
        if article.get("refresh_of"):
            self.store.update("articles", article["refresh_of"], status="refreshed", replaced_by=article["id"])
        record_publication(self, client, article, result)

    def state(self):
        from .measurement import matching_measurement
        articles = self.store.all("articles")
        clients = self.store.all("clients")
        seo = self.store.all("seo")
        by_client = {c["id"]: c for c in clients}
        seo_by_client = {r["id"]: r for r in seo}
        visibility = [{**r, "conversion_measurement": matching_measurement(by_client.get(r["client_id"], {}),
                            seo_by_client.get(r["client_id"], {}))} for r in self.store.all("visibility")]
        jobs = sorted(self.store.all("jobs"), key=lambda j: j["created_at"], reverse=True)[:150]
        events = sorted(self.store.all("events"), key=lambda j: j["created_at"], reverse=True)[:200]
        return {"clients": clients, "articles": articles, "jobs": jobs, "events": events,
                "settings": self.store.settings(), "status": self.codex_status,
                "google": self.google.status(), "vercel": self.vercel.status(), "seo": seo, "backlinks": self.store.all("backlinks"),
                "visibility": visibility, "opportunities": self.store.all("opportunities"),
                "tasks": self.store.all("tasks"), "research": self.store.all("research"),
                "measurements": [r for c in clients for r in self.store.history(c["id"], 20)],
                "measurement_counts": self.store.history_counts(), "experiments": self.store.all("experiments"),
                "server": {"version": "1.0.0", "local": True}}
