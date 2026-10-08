"""SQLite key/value cache. Every network result is stored so reruns never refetch."""

import json
import sqlite3
import threading
import time
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                " ns TEXT, key TEXT, value BLOB, ts REAL, PRIMARY KEY (ns, key))"
            )
            self._db.commit()

    def get(self, ns: str, key: str):
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        return row[0] if row else None

    def set(self, ns: str, key: str, value) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv (ns, key, value, ts) VALUES (?, ?, ?, ?)",
                (ns, key, value, time.time()))
            self._db.commit()

    def get_json(self, ns: str, key: str):
        raw = self.get(ns, key)
        return None if raw is None else json.loads(raw)

    def set_json(self, ns: str, key: str, value) -> None:
        self.set(ns, key, json.dumps(value, ensure_ascii=False))

    def delete(self, ns: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))
            self._db.commit()
