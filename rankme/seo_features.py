"""Client-scoped settings and human-confirmed evidence for article workflows."""

import re
from urllib.parse import urlsplit

from .store import now, uid


def aio_settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - {"enabled", "interval_days", "queries", "country", "language", "location", "device"}:
        raise ValueError("Unknown AI Overview settings")
    config = {"enabled": False, "interval_days": 1, "queries": [], "country": "us", "language": "en", "location": "", "device": "desktop", **value}
    if type(config["enabled"]) is not bool or type(config["interval_days"]) is not int or not 1 <= config["interval_days"] <= 30:
        raise ValueError("Choose an AI Overview interval from 1 to 30 days")
    if not isinstance(config["queries"], list) or len(config["queries"]) > 6:
        raise ValueError("Choose up to six distinct queries for the free daily cap")
    queries = []
    for raw in config["queries"]:
        if not isinstance(raw, str) or not 2 <= len(raw.strip()) <= 200:
            raise ValueError("Each query must be 2–200 characters")
        query = " ".join(raw.split())
        if query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
    config["queries"] = queries
    for key, pattern in (("country", r"[a-zA-Z]{2}"), ("language", r"[a-zA-Z]{2}"), ("location", r"[\w .,'()-]{0,100}")):
        item = config[key]
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise ValueError("Invalid AI Overview " + key)
        config[key] = item.strip().lower() if key != "location" else item.strip()
    if config["device"] not in ("desktop", "mobile", "tablet"):
        raise ValueError("Choose a supported device")
    if config["enabled"] and not queries:
        raise ValueError("Add at least one query before enabling monitoring")
    if config["enabled"] and (config["location"] or config["device"] != "desktop" or config["language"] != "en"):
        raise ValueError("Automatic browser checks currently support English desktop with no exact location; keep these settings off for manual observations")
    return config


def growth_settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - {"enabled", "interval_days", "jev_enabled"}:
        raise ValueError("Unknown growth settings")
    result = {"enabled": False, "interval_days": 1, "jev_enabled": False, **value}
    if type(result["enabled"]) is not bool or type(result["jev_enabled"]) is not bool:
        raise ValueError("Growth switches must be true or false")
    if type(result["interval_days"]) is not int or not 1 <= result["interval_days"] <= 30:
        raise ValueError("Growth audit interval must be 1–30 days")
    return result


def experience_payload(data, client_url):
    if not isinstance(data, dict) or set(data) - {"claim", "source_url", "source_note", "confirmed"}:
        raise ValueError("Unknown experience fields")
    claim = data.get("claim")
    note = data.get("source_note", "")
    source = data.get("source_url", "")
    if not isinstance(claim, str) or not 10 <= len(claim.strip()) <= 2000:
        raise ValueError("Experience claim must be 10–2000 characters")
    if not isinstance(note, str) or len(note) > 1000 or not isinstance(source, str) or len(source) > 2000:
        raise ValueError("Invalid experience source")
    if any(re.search(r"(?i)\b(?:api[_-]?key|access[_-]?key|token|password|passwd|secret|authorization|auth|session|signature|credential|key|sid|code|sig)\b\s*[:=]", text)
           for text in (claim, note)):
        raise ValueError("Remove credentials from the experience entry")
    if source:
        parsed = urlsplit(source)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Source must be a public HTTP or HTTPS URL")
        from .evidence_audit import _public_url
        safe_source = _public_url(source)
        if not safe_source:
            raise ValueError("Source URL must be public and must not contain credentials")
        source = safe_source
    if not source and not note.strip():
        raise ValueError("Describe the source of this experience claim")
    if type(data.get("confirmed", False)) is not bool:
        raise ValueError("Confirmation must be true or false")
    from .evidence_audit import validate_experience_claim, validate_experience_entry
    entry = validate_experience_claim({"facts": [claim.strip()], "source": {"type": "public_url" if source else "owner_note",
        "url": source, "reference": note.strip()}}, client_url=client_url)
    entry.update(claim=claim.strip(), source_url=source, source_note=note.strip(),
                 user_confirmed=data.get("confirmed", False), confirmed_at=now() if data.get("confirmed") else None)
    checked = validate_experience_entry(entry)
    entry.update(verification_status=checked["verification_status"], brief_eligible=checked["brief_eligible"])
    return entry


def create_experience(store, client_id, data):
    client = store.get("clients", client_id)
    fields = experience_payload(data, client["url"])
    if sum(r.get("client_id") == client_id for r in store.all("experience_claims")) >= 20:
        raise ValueError("Keep up to 20 experience facts per website; remove an older entry first")
    return store.put("experience_claims", {"id": uid(), "client_id": client_id, **fields})


def update_experience(store, ident, data):
    old = store.get("experience_claims", ident)
    client = store.get("clients", old["client_id"])
    merged = {key: old.get(key) for key in ("claim", "source_url", "source_note")}
    merged["confirmed"] = old.get("user_confirmed", False)
    merged.update(data)
    fields = experience_payload(merged, client["url"])
    changed = any(fields[key] != old.get(key) for key in ("claim", "source_url", "source_note"))
    if changed:
        fields.update(user_confirmed=False, confirmed_at=None, verification_status="unverified", brief_eligible=False)
    elif fields["user_confirmed"] and old.get("user_confirmed"):
        fields["confirmed_at"] = old.get("confirmed_at")
    fields["id"] = ident
    return store.update("experience_claims", ident, **fields)


def confirmed_experience(store, client_id):
    from .evidence_audit import validate_experience_entry
    return [{"fact": fact, "source": valid["source"], "confirmed_at": valid["confirmed_at"]}
            for r in store.all("experience_claims") if r.get("client_id") == client_id
            for valid in [validate_experience_entry(r)] if valid["brief_eligible"] for fact in valid["facts"]][:20]
