"""Private bounded Chrome worker. The parent owns the lock, budget, and history."""

import json
import sys
from pathlib import Path

from .ai_overviews import MAX_RESPONSE_BYTES, _browser_session


def main():
    raw = sys.stdin.read(4097)
    if len(raw) > 4096:
        return 2
    try:
        job = json.loads(raw)
        if not isinstance(job, dict) or set(job) != {"query", "country", "language", "profile", "executable"}:
            return 2
        snapshot = _browser_session(job["query"], job["country"], job["language"],
                                    Path(job["profile"]), job["executable"])
        output = json.dumps(snapshot, ensure_ascii=False)
        if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return 2
        sys.stdout.write(output)
        return 0
    except Exception:
        # Never print browser errors: they can contain URLs or page text.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
