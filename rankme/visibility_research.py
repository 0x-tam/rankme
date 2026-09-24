"""Bounded, source-checked public research through the existing signed-in runner.

Research observations are not keyword-volume data or measurements of consumer AI
answers. External text never configures jobs, commands, destinations, or accounts.
"""
import hashlib
import http.client
import json
import re

from .ai import _client_context, _data, _object, STR, _validate
from .crawler import normalize_url, fetch_public, _Page
from .store import now


CATEGORIES = {"customer_question", "content_gap", "comparison", "statistics_page",
              "free_tool", "infographic", "outdated_guide", "competitor_link", "unlinked_mention",
              "broken_link", "citation_source"}
RESEARCH = _object({"findings": {"type": "array", "maxItems": 8, "items": _object({
    key: STR for key in ("category", "title", "query", "reason", "source_url", "quote", "suggested_action")})},
    "limitations": {"type": "array", "maxItems": 12, "items": STR}})


def research(runner, client, progress=None):
    prompt = (
        "Research useful visibility opportunities for this business with live web research. "
        "Start with actual customer wording in public reviews/forums, competitor weaknesses, "
        "relevant search questions, and existing site coverage. Return at most eight strong "
        "findings, fewer or zero when evidence is weak. Diversify only where relevant: "
        "comparison pages, dated guides, statistics pages, useful free tools, infographic "
        "ideas, unlinked mentions, competitor referring pages, and broken references. "
        "Outreach is out of scope: do not research contacts, reporter requests, partnerships, "
        "email pitches, or outreach campaigns. Focus on analysis and owned-site improvements. "
        "Never invent demand, traffic, volumes, authority scores, quotes, statistics, email "
        "addresses, relationships, or AI mentions/citations. Citation_source means a useful "
        "source to investigate, NOT proof any AI engine cites it. Report deadlines and "
        "uncertainty; a link loss/broken link/mention is a candidate until verified. "
        "Every finding requires a real public source URL and an exact quotation of 12-25 "
        "words from that page. Use each source page once. Propose a concrete next action. "
        "No sending, publishing, account access, reciprocal-link schemes, or execution. "
        "Categories allowed: " + ", ".join(sorted(CATEGORIES)) + ". "
        "Use the client language. " + _data(_client_context(client)))
    result = runner.run(prompt, RESEARCH, progress=progress, search=True, require_research=True)
    _validate(result, RESEARCH)
    accepted, rejected, seen, final_seen = [], 0, set(), set()
    for item in result["findings"]:
        try:
            if item["category"] not in CATEGORIES:
                raise ValueError("Unknown research category")
            source = normalize_url(item["source_url"])
            if source in seen:
                continue
            seen.add(source)
            quote = " ".join(item["quote"].split())
            if not 12 <= len(quote.split()) <= 25:
                raise ValueError("Evidence quotation outside bounds")
            if progress:
                progress("Verifying public research evidence: " + source)
            response = fetch_public(source, max_bytes=750000)
            if response.get("status", 200) != 200:
                raise ValueError("Source unavailable")
            parser = _Page(response["url"])
            parser.feed(response["text"])
            text = " ".join(" ".join(parser.parts).split())
            if quote.casefold() not in text.casefold():
                raise ValueError("Quote not present on source")
            final = normalize_url(response["url"])
            if final in final_seen:
                continue
            final_seen.add(final)
            key = hashlib.sha256((item["category"] + "|" + final + "|" + item["query"].casefold().strip()).encode()).hexdigest()[:24]
            accepted.append({"key": key, "category": item["category"], "title": item["title"][:200],
                "query": item["query"][:200], "reason": item["reason"][:1500],
                "suggested_action": item["suggested_action"][:2000], "source_url": final,
                "evidence": {"url": final, "quote": quote, "checked_at": now(), "verified": True},
                "note": "Quotation verified; relevance and proposed action remain research judgments."})
        except (ValueError, OSError, KeyError, http.client.HTTPException):
            rejected += 1
    return {"findings": accepted, "rejected": rejected, "observed_at": now(),
            "limitations": [str(x)[:1000] for x in result["limitations"]],
            "provider": "Codex public web research", "consumer_ai_measurement": False}
