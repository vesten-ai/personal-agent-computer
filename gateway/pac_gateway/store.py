"""Gateway state: agents and their chat transcripts in SQLite; each agent's model history (which must be
replayed byte-exact) and latest screenshot as files next to it."""

import json
import os
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(os.path.join(data_dir, "agents"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "gateway.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, vm TEXT NOT NULL, model TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'creating', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, ts REAL NOT NULL,
                  kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
                CREATE TABLE IF NOT EXISTS placeholders (
                  agent_id TEXT NOT NULL, secret TEXT NOT NULL, value TEXT NOT NULL,
                  PRIMARY KEY (agent_id, secret));
                CREATE TABLE IF NOT EXISTS addresses (
                  agent_id TEXT PRIMARY KEY, address TEXT NOT NULL);
            """)

    # -- agents --------------------------------------------------------------------------------
    def create_agent(self, name: str, model: str) -> dict:
        aid = uuid.uuid4().hex[:12]
        row = {"id": aid, "name": name, "vm": f"pac-{aid}", "model": model, "status": "creating",
               "created_at": time.time()}
        with self.lock:
            self.db.execute("INSERT INTO agents VALUES (:id, :name, :vm, :model, :status, :created_at)", row)
            self.db.commit()
        os.makedirs(self.agent_dir(aid), exist_ok=True)
        return row

    def agents(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM agents ORDER BY created_at")]

    def agent(self, aid: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
        return dict(r) if r else None

    def set_status(self, aid: str, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET status = ? WHERE id = ?", (status, aid))
            self.db.commit()

    def delete_agent(self, aid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM events WHERE agent_id = ?", (aid,))
            self.db.execute("DELETE FROM placeholders WHERE agent_id = ?", (aid,))
            self.db.execute("DELETE FROM addresses WHERE agent_id = ?", (aid,))
            self.db.execute("DELETE FROM agents WHERE id = ?", (aid,))
            self.db.commit()

    # -- placeholders ----------------------------------------------------------------------------
    def placeholder(self, aid: str, secret: str, make) -> str:
        """The agent's placeholder for a secret, created once and stable afterwards."""
        with self.lock:
            r = self.db.execute("SELECT value FROM placeholders WHERE agent_id = ? AND secret = ?",
                                (aid, secret)).fetchone()
            if r:
                return r["value"]
            value = make()
            self.db.execute("INSERT INTO placeholders VALUES (?, ?, ?)", (aid, secret, value))
            self.db.commit()
            return value

    # -- VM addresses ----------------------------------------------------------------------------
    def address(self, aid: str, current: str | None) -> str | None:
        """The VM's address: `current` if it has one (and remember it), else the last one seen.
        A sleeping VM reports no address, but its placeholders must stay bound for when it wakes."""
        with self.lock:
            if current:
                self.db.execute("INSERT OR REPLACE INTO addresses VALUES (?, ?)", (aid, current))
                self.db.commit()
                return current
            r = self.db.execute("SELECT address FROM addresses WHERE agent_id = ?", (aid,)).fetchone()
        return r["address"] if r else None

    # -- transcript ----------------------------------------------------------------------------
    def add_event(self, aid: str, kind: str, data) -> dict:
        ev = {"agent_id": aid, "ts": time.time(), "kind": kind, "data": data}
        with self.lock:
            cur = self.db.execute("INSERT INTO events (agent_id, ts, kind, data) VALUES (?, ?, ?, ?)",
                                  (aid, ev["ts"], kind, json.dumps(data, ensure_ascii=False)))
            self.db.commit()
        ev["id"] = cur.lastrowid
        return ev

    def events(self, aid: str, after: int = 0, limit: int = 500) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE agent_id = ? AND id > ? ORDER BY id LIMIT ?",
                                   (aid, after, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def last_events(self, aid: str, n: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM (SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?)"
                                   " ORDER BY id", (aid, n)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # -- per-agent files -----------------------------------------------------------------------
    def agent_dir(self, aid: str) -> str:
        return os.path.join(self.dir, "agents", aid)

    def save_history(self, aid: str, state: dict) -> None:
        path = os.path.join(self.agent_dir(aid), "history.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)

    def load_history(self, aid: str) -> dict | None:
        try:
            with open(os.path.join(self.agent_dir(aid), "history.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save_screen(self, aid: str, png: bytes) -> None:
        path = os.path.join(self.agent_dir(aid), "screen.png")
        with open(path + ".tmp", "wb") as f:
            f.write(png)
        os.replace(path + ".tmp", path)

    def screen_path(self, aid: str) -> str:
        return os.path.join(self.agent_dir(aid), "screen.png")
