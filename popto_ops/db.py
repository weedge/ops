"""SQLite：hub.db（事件 / 任务 / 运行 / 卡片）与 ops.db（业务数据）。WAL 模式。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

HUB_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
  subject TEXT, summary TEXT, link TEXT, payload_ref TEXT, data TEXT,
  dedup_key TEXT UNIQUE, occurred_at TEXT, received_at TEXT,
  seen_count INTEGER DEFAULT 1, last_seen_at TEXT,
  rule TEXT, routed TEXT                          -- routed: task|none|internal|unmatched|paused
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, rule TEXT NOT NULL, grp TEXT, source TEXT,
  state TEXT NOT NULL,     -- queued|leased|waiting_decision|done|failed|cancelled
  priority INTEGER DEFAULT 2, round INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT, not_before TEXT,
  lease_owner TEXT, lease_until TEXT, lease_losses INTEGER DEFAULT 0,
  agent TEXT, attempts INTEGER DEFAULT 0, tried TEXT DEFAULT '[]',
  producer TEXT,           -- 被审对象的产出 Agent（审查任务用，强制审查者≠生产者）
  parent_task TEXT, summary TEXT, output TEXT, card_id TEXT, error TEXT, forced_agent TEXT
);
CREATE TABLE IF NOT EXISTS task_events (task_id TEXT, event_id TEXT, PRIMARY KEY (task_id, event_id));
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent TEXT NOT NULL,
  is_fallback INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT,
  status TEXT, error_kind TEXT, error_detail TEXT, session_id TEXT, cost_usd REAL,
  duration_s REAL, output_path TEXT
);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, kind TEXT, title TEXT, payload TEXT, task_ids TEXT,
  priority INTEGER DEFAULT 2, created_at TEXT, sent_at TEXT, expires_at TEXT,
  decided_at TEXT, decided_by TEXT, choice TEXT, reason TEXT, status TEXT  -- pending|sent|decided|expired
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, priority INTEGER DEFAULT 2,
  title TEXT, body TEXT, card_id TEXT, created_at TEXT, send_after TEXT,
  sent_at TEXT, status TEXT DEFAULT 'pending', error TEXT, ref TEXT
);
CREATE TABLE IF NOT EXISTS agent_state (
  agent TEXT PRIMARY KEY, state TEXT, open_until TEXT, last_error TEXT, updated_at TEXT,
  timeout_strikes INTEGER DEFAULT 0, usage_ratio REAL, unavailable_since TEXT
);
CREATE TABLE IF NOT EXISTS schedule_state (type TEXT PRIMARY KEY, last_due TEXT, fired_at TEXT);
CREATE TABLE IF NOT EXISTS paused (key TEXT PRIMARY KEY, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS counters (day TEXT, name TEXT, value REAL DEFAULT 0, PRIMARY KEY (day, name));
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_grp ON tasks(grp, state);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, received_at);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
"""

OPS_DDL = """
CREATE TABLE IF NOT EXISTS contents (
  id TEXT PRIMARY KEY, parent_id TEXT, pillar TEXT, persona TEXT, topic TEXT,
  script_path TEXT, popto_features TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, content_id TEXT, platform TEXT, account TEXT,
  platform_post_id TEXT, url TEXT, title TEXT, pillar TEXT, published_at TEXT, channel_code TEXT,
  publish_mode TEXT,
  UNIQUE (platform, platform_post_id)
);
CREATE TABLE IF NOT EXISTS metric_jobs (
  post_id TEXT, checkpoint TEXT, due_at TEXT, done_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT,
  PRIMARY KEY (post_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS post_metrics (
  post_id TEXT, captured_at TEXT, checkpoint TEXT, source TEXT,
  views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, favorites INTEGER,
  follows_gained INTEGER, completion_rate REAL, avg_watch_sec REAL, profile_visits INTEGER,
  raw_ref TEXT, PRIMARY KEY (post_id, captured_at)
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, post_id TEXT, author_hash TEXT, text TEXT, likes INTEGER,
  created_at TEXT, category TEXT, sentiment TEXT, reply_draft TEXT, reply_status TEXT,
  purge_after TEXT, collected_at TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, source_type TEXT, source_ref TEXT, quote TEXT,
  kind TEXT, module TEXT, persona TEXT, intensity INTEGER, demand_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS demands (
  id TEXT PRIMARY KEY, title TEXT, kind TEXT, module TEXT, signal_count INTEGER,
  persona_dist TEXT, value_factor REAL, cost_size TEXT, score REAL,
  status TEXT, prd_path TEXT, mr_links TEXT, decided_ref TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, platform TEXT, profile_url TEXT, nickname TEXT, industry TEXT,
  lead_type TEXT, followers INTEGER, score REAL, stage TEXT, why TEXT, outreach_draft TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS funnel_daily (
  date TEXT, channel_code TEXT, clicks INTEGER, registrations INTEGER,
  first_done INTEGER, second_done INTEGER, paid INTEGER,
  PRIMARY KEY (date, channel_code)
);
CREATE TABLE IF NOT EXISTS publish_packs (
  id TEXT PRIMARY KEY, content_id TEXT, platform TEXT, account TEXT, title TEXT, scheduled_at TEXT,
  pack_dir TEXT, publish_mode TEXT, status TEXT,  -- ready|carded|approved|published|rejected|failed
  card_id TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, kind TEXT, ref TEXT, approved_at TEXT, status TEXT,
  result TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS listening_terms (
  term TEXT PRIMARY KEY, platform TEXT, industry TEXT, active INTEGER DEFAULT 1,
  hits_7d INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback_inbox (
  id TEXT PRIMARY KEY, channel TEXT, link TEXT, received_at TEXT, read_at TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def hub_db_path() -> Path:
    return paths.ops_data() / "hub.db"


def ops_db_path() -> Path:
    return paths.ops_data() / "ops.db"


def open_hub(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path or hub_db_path())
    conn.executescript(HUB_DDL)
    return conn


# 已有库补列（CREATE TABLE IF NOT EXISTS 不会给老表加新列）
OPS_MIGRATIONS = [("comments", "collected_at", "TEXT")]


def open_ops(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path or ops_db_path())
    conn.executescript(OPS_DDL)
    for table, col, typ in OPS_MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
