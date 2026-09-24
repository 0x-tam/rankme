"""Thread-safe, transactional local persistence for RankMe."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class Store:
    TABLES = {"clients", "articles", "jobs", "events", "seo", "backlinks"}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        for table in self.TABLES:
            self.db.execute("CREATE TABLE IF NOT EXISTS " + table + " (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        self.db.commit()

    def _table(self, table):
        if table not in self.TABLES:
            raise ValueError("Unknown collection")
        return table

    def all(self, table):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM " + self._table(table))]

    def get(self, table, ident):
        with self.lock:
            row = self.db.execute("SELECT data FROM " + self._table(table) + " WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError("Record not found")
            return json.loads(row[0])

    def put(self, table, data):
        record = dict(data)
        record.setdefault("id", uid())
        record.setdefault("created_at", now())
        record["updated_at"] = now()
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO " + self._table(table) + " (id,data) VALUES (?,?)",
                            (record["id"], json.dumps(record, ensure_ascii=False)))
        return record

    def update(self, table, ident, **fields):
        with self.lock:
            record = self.get(table, ident)
            record.update(fields)
            return self.put(table, record)

    def settings(self, fields=None):
        defaults = {"codex_path": "codex", "model": "", "paused": False,
                    "weekly_day": 1, "weekly_hour": 9, "max_pages": 24}
        with self.lock:
            row = self.db.execute("SELECT data FROM settings WHERE id=1").fetchone()
            if row:
                defaults.update(json.loads(row[0]))
            if fields is not None:
                defaults.update(fields)
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO settings (id,data) VALUES (1,?)", (json.dumps(defaults),))
        return defaults

    def event(self, message, client_id=None, level="info"):
        event = self.put("events", {"message": str(message)[:2000], "client_id": client_id, "level": level})
        with self.lock, self.db:
            self.db.execute("DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 1000)")
        return event

    def backup(self):
        with self.lock:
            return {"format": "rankme-backup-v1", "created_at": now(), "settings": self.settings(),
                    **{table: self.all(table) for table in sorted(self.TABLES)}}

    def close(self):
        with self.lock:
            self.db.close()
