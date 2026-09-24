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
    TABLES = {"clients", "articles", "jobs", "events", "seo", "backlinks", "visibility", "opportunities", "research", "tasks", "answer_probes", "measurements", "experiments"}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        for table in self.TABLES:
            self.db.execute("CREATE TABLE IF NOT EXISTS " + table + " (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS measurements_client ON measurements(json_extract(data, '$.client_id'))")
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

    def history(self, client_id, limit=None):
        """Read history without loading every other client's retained snapshots."""
        sql = "SELECT data FROM measurements WHERE json_extract(data, '$.client_id')=? ORDER BY rowid DESC"
        values = [client_id]
        if limit is not None:
            sql += " LIMIT ?"
            values.append(max(0, int(limit)))
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(sql, values)]

    def history_counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT json_extract(data, '$.client_id'), COUNT(*) FROM measurements GROUP BY json_extract(data, '$.client_id')"))

    def put(self, table, data):
        record = dict(data)
        record.setdefault("id", uid())
        record.setdefault("created_at", now())
        record["updated_at"] = now()
        with self.lock, self.db:
            verb = "INSERT OR IGNORE" if table == "measurements" else "INSERT OR REPLACE"
            self.db.execute(verb + " INTO " + self._table(table) + " (id,data) VALUES (?,?)",
                            (record["id"], json.dumps(record, ensure_ascii=False)))
            if table == "measurements":
                return self.get(table, record["id"])
        return record

    def update(self, table, ident, **fields):
        if table == "measurements":
            raise ValueError("Measurement history is append-only")
        with self.lock:
            record = self.get(table, ident)
            record.update(fields)
            return self.put(table, record)

    def put_many(self, records):
        """Commit dependent records together, without nested per-record commits."""
        prepared = []
        for table, value in records:
            self._table(table)
            record = dict(value)
            record.setdefault("id", uid())
            record.setdefault("created_at", now())
            record["updated_at"] = now()
            prepared.append((table, record))
        with self.lock, self.db:
            for table, record in prepared:
                verb = "INSERT OR IGNORE" if table == "measurements" else "INSERT OR REPLACE"
                self.db.execute(verb + " INTO " + table + " (id,data) VALUES (?,?)",
                                (record["id"], json.dumps(record, ensure_ascii=False)))
            return [self.get(table, record["id"]) if table == "measurements" else record for table, record in prepared]

    def delete(self, table, ident):
        if table == "measurements":
            raise ValueError("Measurement history is append-only")
        with self.lock, self.db:
            if not self.db.execute("DELETE FROM " + self._table(table) + " WHERE id=?", (ident,)).rowcount:
                raise KeyError("Record not found")

    def delete_client(self, client_id):
        """Remove one client and every record that belongs to it, in a single transaction."""
        with self.lock, self.db:
            for table in sorted(self.TABLES - {"clients"}):
                self.db.execute("DELETE FROM " + table + " WHERE json_extract(data, '$.client_id')=?", (client_id,))
            # Per-client singleton reports are keyed by the client id itself.
            for table in ("seo", "visibility"):
                self.db.execute("DELETE FROM " + table + " WHERE id=?", (client_id,))
            removed = self.db.execute("DELETE FROM clients WHERE id=?", (client_id,)).rowcount
        if not removed:
            raise KeyError("Record not found")

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
