"""Deterministic publication checks. These complement, not replace, evidence review."""
import re
from urllib.parse import urlparse


def words(body):
    return re.findall(r"\b[\w'-]+\b", body.lower(), re.UNICODE)


def check_article(article, client, existing=()):
    issues = []
    body = str(article.get("body", ""))
    title = str(article.get("title", ""))
    slug = str(article.get("slug", ""))
    tokens = words(body)
    if not title.strip() or len(title) > 200:
        issues.append("Add a descriptive title of at most 200 characters.")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 120:
        issues.append("Use a URL-safe slug, at most 120 characters.")
    if len(tokens) < 350:
        issues.append("Draft is unusually short. Expand the useful answer before automatic publishing.")
    if not re.search(r"^##\s+\S", body, re.M):
        issues.append("Add section headings so readers can navigate the answer.")
    if not article.get("description") or len(article.get("description", "")) > 320:
        issues.append("Add a concise meta description, at most 320 characters.")
    if re.search(r"\b(TODO|TBD|INSERT HERE|lorem ipsum)\b|\[(?:insert|placeholder)[^\]]*\]", body, re.I):
        issues.append("Remove placeholders from the article.")
    if re.search(r"<\s*(?:script|iframe|object|embed)|\bon\w+\s*=|javascript\s*:", body, re.I):
        issues.append("Executable HTML is not allowed in articles.")
    sources = article.get("sources", [])
    if not sources:
        issues.append("Attach research sources supporting the article.")
    for source in sources:
        url = source.get("url", "") if isinstance(source, dict) else ""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            issues.append("One research source has an invalid URL.")
            break
        if url not in body:
            issues.append("Cite every listed source in the article body.")
            break
    source_urls = {s.get("url") for s in sources if isinstance(s, dict)}
    for claim in article.get("claims", []):
        if not isinstance(claim, dict) or claim.get("source_url") not in source_urls:
            issues.append("A factual claim refers to a source missing from the evidence list.")
            break
    links = re.findall(r"\]\(([^\s)]+)", body)
    for url in links:
        if url.startswith("#"):
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
            issues.append("Use full HTTP(S) URLs for article links.")
            break
    own_host = (urlparse(client.get("url", "")).hostname or "").removeprefix("www.")
    if not any((urlparse(u).hostname or "").removeprefix("www.") == own_host for u in links):
        issues.append("Add a relevant link back to the client's website.")
    current = set(zip(tokens, tokens[1:], tokens[2:]))
    for previous in existing:
        if previous.get("id") == article.get("id"):
            continue
        if previous.get("slug") == slug:
            issues.append("Another article already uses this slug.")
            break
        prior = words(previous.get("body", ""))
        grams = set(zip(prior, prior[1:], prior[2:]))
        if current and grams and len(current & grams) / min(len(current), len(grams)) > 0.68:
            issues.append("This draft substantially overlaps an existing article.")
            break
    return {"passed": not issues, "issues": list(dict.fromkeys(issues)), "word_count": len(tokens),
            "note": "Structural checks do not prove factual accuracy. Separate AI evidence review is required."}
