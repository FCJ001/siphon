"""SQLite 存储。单连接 + 线程锁, WAL 模式; 异步路径一律用 asyncio.to_thread 包。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT NOT NULL,
  key_tail      TEXT NOT NULL,
  alias         TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  primary_flag  INTEGER NOT NULL DEFAULT 0,
  weight        INTEGER NOT NULL DEFAULT 100,
  enabled       INTEGER NOT NULL DEFAULT 1,
  inflight_cap  INTEGER NOT NULL DEFAULT 24,
  session_id    TEXT NOT NULL,
  allowlist     TEXT NOT NULL DEFAULT '[]',
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              REAL NOT NULL,
  account_id      INTEGER,
  affinity_key    TEXT NOT NULL DEFAULT '',
  requested_model TEXT NOT NULL DEFAULT '',
  model           TEXT NOT NULL DEFAULT '',
  effort_in       TEXT,
  effort_out      TEXT,
  prompt_tokens   INTEGER NOT NULL DEFAULT 0,
  cached_tokens   INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  saved_tokens    INTEGER NOT NULL DEFAULT 0,
  tool_rounds     INTEGER NOT NULL DEFAULT 0,
  latency_ms      INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'ok',
  retried         INTEGER NOT NULL DEFAULT 0,
  replay_hit      INTEGER NOT NULL DEFAULT 0,
  cost_baseline   REAL NOT NULL DEFAULT 0,
  cost_actual     REAL NOT NULL DEFAULT 0,
  save_cache      REAL NOT NULL DEFAULT 0,
  save_free       REAL NOT NULL DEFAULT 0,
  save_transform  REAL NOT NULL DEFAULT 0,
  save_replay     REAL NOT NULL DEFAULT 0,
  error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);

CREATE TABLE IF NOT EXISTS model_usage (
  account_id INTEGER NOT NULL,
  model      TEXT NOT NULL,
  month      TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd   REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, model, month)
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  account_id INTEGER NOT NULL,
  rolling REAL, weekly REAL, monthly REAL,
  resets TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  before TEXT NOT NULL DEFAULT '{}',
  after TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path = Path(config.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def execute(sql: str, params: tuple | list = ()) -> int:
    """写操作, 返回 lastrowid。"""
    with _lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or 0


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock:
        conn = connect()
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------------- kv(全局策略等) ----------------

def kv_get(key: str, default=None):
    row = query_one("SELECT value FROM kv WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def kv_set(key: str, value) -> None:
    execute("INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)))


def log_event(etype: str, payload: dict) -> None:
    execute("INSERT INTO events(ts,type,payload) VALUES(?,?,?)",
            (_now(), etype, json.dumps(payload, ensure_ascii=False)))


def log_audit(action: str, target: str = "", before: dict | None = None,
              after: dict | None = None) -> None:
    execute("INSERT INTO audit(ts,action,target,before,after) VALUES(?,?,?,?,?)",
            (_now(), action, target,
             json.dumps(before or {}, ensure_ascii=False),
             json.dumps(after or {}, ensure_ascii=False)))
