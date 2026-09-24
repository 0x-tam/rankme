"""Persistent visibility reports and bounded, explicitly configured follow-up work."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .store import now, uid

DEFAULTS = {"enabled": True, "interval_days": 7, "max_actions": 1,
            "auto_plan": False, "auto_refresh": False, "auto_research": False, "auto_probe": False}
CONTENT = {"customer_question", "content_gap", "comparison", "statistics_page"}
JOBS = {"visibility-audit", "visibility-research", "visibility-action", "visibility-probe"}


def settings(value=None):
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Unknown visibility settings")
    result = {**DEFAULTS, **value}
    for key in ("enabled", "auto_plan", "auto_refresh", "auto_research", "auto_probe"):
        if type(result[key]) is not bool:
            raise ValueError(key + " must be true or false")
    for key, maximum in (("interval_days", 30), ("max_actions", 5)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(key + " is outside the allowed range")
    return result


def _canonical(value):
    try:
        p = urlsplit(value or "")
        return (p.scheme, (p.hostname or "").removeprefix("www."), p.port, p.path.rstrip("/"), p.query)
    except ValueError:
        return ("", "")


def owned_article(engine, client, url):
    if not url:
        return None
    matches = []
    for article in engine.articles(client["id"]):
        live = article.get("publish_result", {}).get("live_url")
        if article.get("status") == "published" and live and _canonical(live) == _canonical(url):
            matches.append(article)
    return max(matches, key=lambda a: a.get("updated_at", "")) if matches else None


def _optional(store, table, ident):
    try:
        return store.get(table, ident)
    except KeyError:
        return {}


def rebuild(engine, client_id):
    with engine.guard:
        return _rebuild(engine, client_id)


def _rebuild(engine, client_id):
    from .visibility import analyze
    store = engine.store
    client = store.get("clients", client_id)
    report = analyze(client, client.get("crawl", {}), _optional(store, "seo", client_id),
                     store.all("backlinks"), engine.articles(client_id))
    research = _optional(store, "research", client_id)
    probes = _optional(store, "answer_probes", client_id)
    report["answer_probes"] = probes
    if probes.get("records"):
        report["missing_data"] = [message for message in report["missing_data"] if not message.startswith("AI mentions")]
        report["missing_data"].append("AI results are sampled Codex web answers; consumer ChatGPT, Claude, Gemini and Perplexity remain unmeasured.")
        for capability in report["capabilities"]:
            if capability["name"] in ("AI Mentions", "Citation Gaps"):
                capability.update(status="partial", detail="Actual sampled Codex web answers and checked sources; other AI platforms need their own integrations.")
        for sample in probes["records"][:3]:
            if sample.get("target_cited"):
                continue
            for citation in sample.get("citations", [])[:5]:
                if not citation.get("verified") or citation.get("target"):
                    continue
                source = citation.get("final_url") or citation.get("url")
                identity = hashlib.sha256((sample["prompt"] + source).encode()).hexdigest()[:20]
                report["opportunities"].append({"id": "citation-" + identity, "kind": "citation_gap",
                    "title": "Review a source cited for “" + sample["prompt"][:150] + "”", "url": source,
                    "score": 48, "confidence": .6, "query": sample["prompt"],
                    "evidence": {"provider": sample["provider"], "observed_at": sample["observed_at"],
                                 "prompt": sample["prompt"], "source_url": source, "target_cited": False,
                                 "note": "One actual Codex answer sample. Reachability is checked; citation support still requires editorial review."},
                    "suggested_action": "Compare this cited source against existing coverage; identify useful missing evidence and answer structure.",
                    "score_explanation": "Sampled citation gap priority 48/100; not a prediction of citation probability."})
    for item in research.get("findings", [])[:8]:
        from .visibility_research import CATEGORIES
        if item.get("category") not in CATEGORIES:
            continue
        report["opportunities"].append({"id": "research-" + item["key"], "kind": item["category"],
            "title": item["title"], "url": item["source_url"], "score": 45, "confidence": .6,
            "evidence": item["evidence"], "query": item["query"], "reason": item["reason"],
            "suggested_action": item["suggested_action"],
            "score_explanation": "Research candidate: source quotation checked; demand, fit and feasibility still need research. Priority 45/100 is a heuristic."})
    report["research"] = {"observed_at": research.get("observed_at"), "count": len(research.get("findings", [])),
                          "limitations": research.get("limitations", []), "rejected": research.get("rejected", 0)}
    report["crawl_observed_at"] = client.get("crawl_observed_at")
    report["automation"] = settings(client.get("visibility_settings"))
    existing = {o["id"]: o for o in store.all("opportunities") if o["client_id"] == client_id}
    records, active = [], set()
    timestamp = now()
    for item in report["opportunities"]:
        local_id = item["id"]
        original = owned_article(engine, client, item.get("url")) if item["kind"] == "refresh" else None
        if original:
            # A new owned revision can be refreshed again only with a later data window.
            if original.get("refresh_of"):
                end = item.get("evidence", {}).get("periods", {}).get("current", {}).get("end", "")
                published_day = (original.get("published_at") or original.get("updated_at", ""))[:10]
                if not end or end <= published_day:
                    continue
            local_id += ":version:" + original["id"]
        ident = hashlib.sha256((client_id + ":" + local_id).encode()).hexdigest()[:32]
        previous = existing.get(ident, {})
        status = previous.get("status", "open")
        if status == "resolved":
            status = "open"
        mode = "refresh" if original else "article" if item["kind"] in CONTENT else "task"
        record = {**item, "id": ident, "source_id": local_id, "client_id": client_id,
            "status": status, "first_seen_at": previous.get("first_seen_at", timestamp),
            "last_seen_at": timestamp, "created_at": previous.get("created_at", timestamp),
            "execution_mode": mode, "executable": status in ("open", "failed") and item["kind"] != "editorial_prospect",
            "execution_label": {"refresh": "Prepare safe refresh", "article": "Create content brief", "task": "Prepare action brief"}[mode]}
        if original:
            record["source_article_id"] = original["id"]
        for key in ("article_id", "task_id", "job_id", "error", "completed_at"):
            if key in previous:
                record[key] = previous[key]
        active.add(ident)
        records.append(("opportunities", record))
    for ident, previous in existing.items():
        if ident not in active and previous.get("status") in ("open", "failed"):
            records.append(("opportunities", {**previous, "status": "resolved", "executable": False}))
    report.update(id=client_id, client_id=client_id, observed_at=timestamp,
                  opportunities=[r for table, r in records if r["id"] in active])
    report["summary"] = {"opportunities": len(active), "pages": report["coverage"]["pages"],
                         "technical_findings": len(report["technical_findings"])}
    records.append(("visibility", report))
    with engine.guard:
        store.put_many(records)
    return report


def queue_action(engine, ident, scheduled=False):
    with engine.guard:
        item = engine.store.get("opportunities", ident)
        if item.get("kind") in ("editorial_prospect", "listicle_outreach", "reporter_outreach", "editorial_partnership"):
            raise ValueError("Outreach is outside the configured visibility scope")
        if item.get("status") not in ("open", "failed"):
            raise ValueError("This opportunity is already queued, completed, dismissed or resolved")
        client = engine.store.get("clients", item["client_id"])
        if not client.get("confirmed"):
            raise ValueError("Confirm the business profile before acting on recommendations")
        job = engine.queue("visibility-action", client["id"], opportunity_id=ident, scheduled=scheduled)
        engine.store.update("opportunities", ident, status="queued", job_id=job["id"], executable=False, error="")
        return job


def _task_body(item, client):
    return ("# " + item["title"] + "\n\n" + item.get("reason", "") + "\n\n"
        "## Proposed action\n" + item.get("suggested_action", "Review the observed evidence and verify intent before changing this page.") +
        "\n\n## Evidence\n```json\n" + json.dumps(item.get("evidence", {}), ensure_ascii=False, indent=2) +
        "\n```\n\n## Completion checks\n"
        "- Confirm the evidence is current and relevant to " + client["name"] + ".\n"
        "- Preserve useful existing content and verify supporting sources.\n"
        "- Check internal destinations, factual claims, and reader value.\n"
        "- Publishing requires a supported configured adapter. Outreach is out of scope.\n"
        "- Record the result and compare later observations; do not assume causation.\n\n"
        "This is an action brief. It does not mean the website was changed or a message was sent.\n")


def execute_action(engine, job, progress):
    store = engine.store
    item = store.get("opportunities", job["opportunity_id"])
    if item["client_id"] != job["client_id"]:
        raise ValueError("Opportunity does not belong to this client")
    client = store.get("clients", job["client_id"])
    if not client.get("confirmed"):
        raise ValueError("Confirm the business profile before preparing work")
    if item.get("status") == "completed":
        return  # A retry after a crash must not create another draft.
    if item.get("status") not in ("queued", "failed", "open"):
        raise ValueError("Opportunity is no longer actionable")
    if job.get("scheduled"):
        config = settings(client.get("visibility_settings"))
        key = "auto_refresh" if item.get("execution_mode") == "refresh" else "auto_plan"
        if not config["enabled"] or not config[key] or client.get("status") == "paused":
            raise ValueError("Automatic follow-up was paused before this job started")
    progress("Preparing an evidence-backed next action")
    task_id = hashlib.sha256(("task:" + item["id"]).encode()).hexdigest()[:32]
    task = {"id": task_id, "client_id": client["id"], "title": item["title"], "kind": item["kind"],
            "body": _task_body(item, client), "sources": [item["url"]] if item.get("url") else [],
            "status": "ready", "opportunity_id": item["id"]}
    writes = [("tasks", task)]
    updates = {"status": "completed", "task_id": task_id, "completed_at": now(), "executable": False, "error": ""}
    if item.get("execution_mode") in ("article", "refresh"):
        from .engine import first_run, parse_date
        existing = engine.articles(client["id"])
        article_id = hashlib.sha256(("article:" + item["id"]).encode()).hexdigest()[:32]
        article = _optional(store, "articles", article_id)
        if not article:
            query = item.get("query") or item.get("evidence", {}).get("query") or item["title"]
            due = max([datetime.now(timezone.utc), parse_date(client.get("next_run") or first_run())] +
                      [parse_date(a["scheduled_at"]) + timedelta(days=7) for a in existing if a.get("status") == "planned" and a.get("scheduled_at")])
            article = {"id": article_id, "client_id": client["id"], "title": item["title"],
                "keyword": str(query)[:200], "intent": "Answer the verified customer need", "subject": client.get("subject") or str(query)[:200],
                "angle": (item.get("suggested_action", "") + "\n" + _task_body(item, client))[:12000],
                "cta_url": client["url"], "status": "planned", "scheduled_at": due.isoformat(), "body": "", "error": "",
                "opportunity_id": item["id"], "visibility_generated": True}
            if item["execution_mode"] == "refresh":
                original = store.get("articles", item["source_article_id"])
                if original["client_id"] != client["id"] or original.get("status") != "published":
                    raise ValueError("Refresh requires a currently published article owned by this website")
                from .publisher import refresh_baseline
                baseline = refresh_baseline(client, original)
                article.update(title=original["title"], slug=original["slug"], keyword=original.get("keyword", query),
                    refresh_of=original["id"], refresh_baseline=baseline,
                    angle=article["angle"] + "\nRefresh this existing article while preserving its intent and URL. Existing body:\n" + original.get("body", "")[:20000])
            elif any(a.get("keyword", "").casefold().strip() == str(query).casefold().strip() and a.get("status") != "superseded" for a in existing):
                raise ValueError("An article already covers this query; review existing coverage first")
            writes.append(("articles", article))
        updates["article_id"] = article_id
    writes.append(("opportunities", {**item, **updates}))
    with engine.guard:
        store.put_many(writes)
    store.event("Visibility action prepared: " + item["title"], client["id"])


def schedule(engine):
    if engine.store.settings()["paused"]:
        return
    from .engine import parse_date
    current = datetime.now(timezone.utc)
    for client in engine.store.all("clients"):
        config = settings(client.get("visibility_settings"))
        if not config["enabled"] or client.get("status") in ("paused", "error", "new", "inspecting"):
            continue
        if not client.get("crawl"):
            continue
        pending = [j for j in engine.store.all("jobs") if j["client_id"] == client["id"] and j["status"] in ("queued", "running")]
        if pending:
            continue
        last = client.get("visibility_attempt_at") or client.get("crawl_observed_at") or client.get("created_at")
        try:
            due = not last or (current - parse_date(last)).total_seconds() >= config["interval_days"] * 86400
        except (TypeError, ValueError):
            due = True
        if due:
            engine.queue("visibility-audit", client["id"], scheduled=True)
            continue
        if not client.get("confirmed"):
            continue
        research_at = client.get("research_attempt_at")
        try:
            research_due = not research_at or (current - parse_date(research_at)).total_seconds() >= config["interval_days"] * 86400
        except (ValueError, TypeError):
            research_due = True
        if config["auto_research"] and research_due:
            engine.queue("visibility-research", client["id"], scheduled=True)
            continue
        probe_at = client.get("probe_attempt_at")
        try:
            probe_due = not probe_at or (current - parse_date(probe_at)).total_seconds() >= config["interval_days"] * 86400
        except (ValueError, TypeError):
            probe_due = True
        if config["auto_probe"] and probe_due:
            engine.queue("visibility-probe", client["id"], scheduled=True)
            continue
        cycle = client.get("visibility_attempt_at") or client.get("crawl_observed_at") or client.get("created_at", "")
        used = sum(1 for j in engine.store.all("jobs") if j["client_id"] == client["id"] and
                   j["kind"] == "visibility-action" and j.get("scheduled") and j.get("created_at", "") >= cycle)
        if used >= config["max_actions"]:
            continue
        candidates = [o for o in engine.store.all("opportunities") if o["client_id"] == client["id"] and
                      o.get("status") == "open" and ((o.get("execution_mode") == "article" and config["auto_plan"]) or
                      (o.get("execution_mode") == "refresh" and config["auto_refresh"]))]
        if candidates:
            item = max(candidates, key=lambda o: o.get("score", 0))
            queue_action(engine, item["id"], scheduled=True)


def execute(engine, job, progress):
    client_id = job["client_id"]
    client = engine.store.get("clients", client_id)
    if job.get("scheduled"):
        config = settings(client.get("visibility_settings"))
        if not config["enabled"] or client.get("status") == "paused" or engine.store.settings()["paused"]:
            raise ValueError("Visibility automation was paused before this job started")
        if job["kind"] == "visibility-research" and not config["auto_research"]:
            raise ValueError("Automatic research was disabled before this job started")
        if job["kind"] == "visibility-probe" and not config["auto_probe"]:
            raise ValueError("Automatic answer sampling was disabled before this job started")
    if job["kind"] == "visibility-action":
        return execute_action(engine, job, progress)
    if job["kind"] == "visibility-probe":
        if not client.get("confirmed"):
            raise ValueError("Confirm the business profile before sampling AI answers")
        engine.store.update("clients", client_id, probe_attempt_at=now())
        from .answer_probes import probe
        seo = _optional(engine.store, "seo", client_id)
        candidates = [a.get("keyword") for a in engine.articles(client_id)]
        candidates.extend(r.get("query") for r in (seo.get("search_console") or {}).get("current", {}).get("queries", [])[:20])
        candidates.append(client.get("subject"))
        queries = list(dict.fromkeys(str(q).strip()[:200] for q in candidates if q and str(q).strip()))[:3]
        if not queries:
            raise ValueError("Choose a subject or sync Search Console before sampling answers")
        result = probe(engine.runner(), client, queries, progress)
        engine.store.put("answer_probes", {**result, "id": client_id, "client_id": client_id})
    elif job["kind"] == "visibility-research":
        if not client.get("confirmed"):
            raise ValueError("Confirm the business profile before public research")
        engine.store.update("clients", client_id, research_attempt_at=now())
        from .visibility_research import research
        result = research(engine.runner(), client, progress)
        engine.store.put("research", {**result, "id": client_id, "client_id": client_id})
    else:
        engine.store.update("clients", client_id, visibility_attempt_at=now())
        from .crawler import crawl_site
        progress("Auditing public pages and rebuilding the visibility priorities")
        crawl = crawl_site(client["url"], max_pages=engine.store.settings()["max_pages"], progress=progress)
        if not crawl.get("pages"):
            raise ValueError("No readable pages found; previous visibility evidence was preserved")
        engine.store.update("clients", client_id, crawl=crawl, crawl_observed_at=now())
    rebuild(engine, client_id)
    engine.store.update("clients", client_id, visibility_error="")
