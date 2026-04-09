#!/usr/bin/env python3
"""
Atlas Brain v2 — A self-improving personal AI agent with four-layer memory.
Single entry point replacing cognitive-loop.py + absorbed cron scripts.

Usage:
    python3 atlas-brain.py                    # Standard cron loop (every 20 min)
    python3 atlas-brain.py --morning          # Morning briefing mode
    python3 atlas-brain.py --nightly          # Nightly distillation mode (also runs inline at 11 PM)
    python3 atlas-brain.py --weekly           # Weekly review mode
    python3 atlas-brain.py --perception-only  # Just run perception (debug)
    python3 atlas-brain.py --dry-run          # Full loop, no messages sent
    python3 atlas-brain.py --watchdog         # Check if brain is alive, alert if stuck
    python3 atlas-brain.py --add "title"      # CLI: add task
    python3 atlas-brain.py --done ID          # CLI: mark done
    python3 atlas-brain.py --list             # CLI: list active items
    python3 atlas-brain.py --status           # CLI: system status
    python3 atlas-brain.py --trigger          # CLI: manual trigger
    python3 atlas-brain.py --migrate          # Run DB migration from cognitive-loop.db
    python3 atlas-brain.py --verify           # Verify migration integrity
"""

import argparse
import datetime
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

# ============================================================
# CONSTANTS
# ============================================================

WORKSPACE = Path.home() / ".openclaw" / "workspace"
STATE_DIR = WORKSPACE / ".state"
DB_PATH = STATE_DIR / "atlas-brain.db"
OLD_DB_PATH = STATE_DIR / "cognitive-loop.db"
LOCK_PATH = STATE_DIR / "atlas-brain.lock"
LOG_PATH = STATE_DIR / "atlas-brain.log"
SESSIONS_DIR = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
WATERMARK_PATH = STATE_DIR / "perception-watermark.json"
BRIEFING_STATE_PATH = STATE_DIR / "briefing-state.json"

MEMORY_DIR = WORKSPACE / "memory"
MEMORY_MD = WORKSPACE / "MEMORY.md"
PROCEDURE_MD = WORKSPACE / "PROCEDURE.md"
SESSION_STATE_MD = WORKSPACE / "SESSION-STATE.md"
PROACTIVE_TRACKER = WORKSPACE / "proactive-tracker.md"

MST = datetime.timezone(datetime.timedelta(hours=-7))

QUIET_START = 22  # 10 PM MST
QUIET_END = 6     # 6 AM MST
ENERGY_WINDOWS = {
    "morning_peak": (8, 11),
    "midday":       (11, 15),
    "afternoon":    (15, 19),
    "evening":      (19, 22),
}
PARENTING_DAYS = {2, 4}  # Wednesday=2, Friday=4 (weekday())
MAX_DAILY_NUDGES = 15
MAX_DAILY_FAILURES = 10
COOLDOWN_MINUTES = 40
GLOBAL_COOLDOWN_MINUTES = 15
LOCK_MAX_AGE_SECONDS = 25 * 60
DEFAULT_MODEL = os.environ.get("ATLAS_MODEL", "anthropic/claude-sonnet-4-6")
DEEP_MODEL = os.environ.get("ATLAS_DEEP_MODEL", "anthropic/claude-opus-4-6")
BRAIN_VERSION = "2.0.0"
USER_NAME = os.environ.get("ATLAS_USER_NAME", "User")

BOT_TOKEN = os.environ.get('ATLAS_BOT_TOKEN', '')
CHAT_ID = os.environ.get('ATLAS_CHAT_ID', '')
THREAD_MAIN = int(os.environ.get('ATLAS_THREAD_ID', '0'))

GATEWAY_URL = os.environ.get('ATLAS_GATEWAY_URL', 'http://127.0.0.1:18789/v1/chat/completions')
GATEWAY_TOKEN = os.environ.get('ATLAS_GATEWAY_TOKEN', '')

# ============================================================
# DATABASE SCHEMA
# ============================================================

SCHEMA_V2 = """
-- Items: {user_name}'s tasks (preserves TEXT primary key from v1)
CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    added           TEXT,
    due             TEXT,
    priority        TEXT DEFAULT 'medium',
    energy          TEXT DEFAULT 'medium',
    category        TEXT DEFAULT 'general',
    status          TEXT DEFAULT 'pending',
    block_reason    TEXT,
    nudge_count     INTEGER DEFAULT 0,
    last_nudge      TEXT,
    last_response   TEXT,
    notes           TEXT,
    scheduled_at    TEXT,
    estimated_minutes INTEGER,
    actual_minutes  INTEGER,
    assigned_date   TEXT,
    energy_window   TEXT,
    source          TEXT DEFAULT 'manual',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(due);
CREATE INDEX IF NOT EXISTS idx_items_assigned_date ON items(assigned_date);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);

-- Nudge log: all actions taken by Atlas
CREATE TABLE IF NOT EXISTS nudge_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT REFERENCES items(id),
    timestamp       TEXT DEFAULT (datetime('now')),
    action          TEXT NOT NULL,
    message         TEXT,
    thread          TEXT,
    response        TEXT,
    reasoning       TEXT,
    forced          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nudge_log_item ON nudge_log(item_id);
CREATE INDEX IF NOT EXISTS idx_nudge_log_timestamp ON nudge_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_nudge_log_action ON nudge_log(action);

-- Learnings: behavioral patterns observed
CREATE TABLE IF NOT EXISTS learnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT DEFAULT (date('now')),
    insight         TEXT NOT NULL,
    metric_type     TEXT,
    metric_value    TEXT,
    source          TEXT DEFAULT 'observation',
    promoted        INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learnings_type ON learnings(metric_type);
CREATE INDEX IF NOT EXISTS idx_learnings_promoted ON learnings(promoted);

-- Triggers: task signals detected from {user_name}'s messages
CREATE TABLE IF NOT EXISTS triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT DEFAULT (datetime('now')),
    item_id         TEXT REFERENCES items(id),
    response_type   TEXT NOT NULL,
    user_said       TEXT,
    inferred_schedule TEXT,
    processed       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_triggers_processed ON triggers(processed);
CREATE INDEX IF NOT EXISTS idx_triggers_item ON triggers(item_id);

-- System state: key-value store for brain state
CREATE TABLE IF NOT EXISTS system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- Computed behavioral rules
CREATE TABLE IF NOT EXISTS computed_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type       TEXT NOT NULL,
    rule_value      TEXT NOT NULL,
    computed_at     TEXT DEFAULT (datetime('now')),
    expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_type ON computed_rules(rule_type);

-- Daily metrics
CREATE TABLE IF NOT EXISTS daily_metrics (
    date            TEXT PRIMARY KEY,
    nudges_sent     INTEGER DEFAULT 0,
    checkins_sent   INTEGER DEFAULT 0,
    items_completed INTEGER DEFAULT 0,
    items_created   INTEGER DEFAULT 0,
    response_rate   REAL DEFAULT 0.0,
    best_response_hour  INTEGER,
    worst_response_hour INTEGER,
    actions_taken   TEXT,
    initiatives_worked INTEGER DEFAULT 0,
    api_failures    INTEGER DEFAULT 0,
    total_loop_runs INTEGER DEFAULT 0
);

-- ============================================================
-- NEW TABLES (v2)
-- ============================================================

-- Atlas's self-directed autonomous work
CREATE TABLE IF NOT EXISTS atlas_initiatives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT DEFAULT 'active',
    priority        INTEGER DEFAULT 3,
    category        TEXT DEFAULT 'research',
    last_worked     TEXT,
    next_step       TEXT,
    findings        TEXT,
    notes           TEXT,
    surface_when_ready INTEGER DEFAULT 1,
    time_invested_seconds INTEGER DEFAULT 0,
    max_time_seconds INTEGER DEFAULT 3600,
    estimated_cost_usd REAL DEFAULT 0.0,
    cost_cap_usd    REAL DEFAULT 10.0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_initiatives_status ON atlas_initiatives(status);
CREATE INDEX IF NOT EXISTS idx_initiatives_priority ON atlas_initiatives(priority);

-- Memory event log (tracks all memory writes/promotions/corrections)
CREATE TABLE IF NOT EXISTS memory_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    layer           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT,
    related_item_id TEXT,
    timestamp       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_layer ON memory_events(layer);
CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_events(event_type);

-- Task duration estimates (learned over time)
CREATE TABLE IF NOT EXISTS task_durations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,
    keywords        TEXT,
    estimated_minutes INTEGER NOT NULL,
    actual_minutes  INTEGER,
    sample_count    INTEGER DEFAULT 1,
    confidence      REAL DEFAULT 0.5,
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_durations_category ON task_durations(category);

-- Perception log (raw signal detection log)
CREATE TABLE IF NOT EXISTS perception_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT DEFAULT (datetime('now')),
    session_file    TEXT,
    message_id      TEXT,
    raw_message     TEXT NOT NULL,
    signal_type     TEXT,
    item_id         TEXT REFERENCES items(id),
    extracted_value TEXT,
    acted_on        INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_perception_signal ON perception_log(signal_type);
CREATE INDEX IF NOT EXISTS idx_perception_acted ON perception_log(acted_on);

-- Mechanical reminders (absorbed from cron scripts)
CREATE TABLE IF NOT EXISTS mechanical_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_type   TEXT NOT NULL UNIQUE,
    schedule_cron   TEXT NOT NULL,
    last_fired      TEXT,
    last_acknowledged TEXT,
    suppressed_until TEXT,
    context_rules   TEXT,
    message_template TEXT,
    enabled         INTEGER DEFAULT 1
);

-- ============================================================
-- VIEWS
-- ============================================================

CREATE VIEW IF NOT EXISTS v_active_items AS
    SELECT * FROM items
    WHERE status IN ('pending', 'in_progress', 'blocked', 'active')
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END,
        due ASC;

CREATE VIEW IF NOT EXISTS v_today_items AS
    SELECT * FROM items
    WHERE status IN ('pending', 'active')
    AND (assigned_date = date('now') OR (due <= date('now') AND assigned_date IS NULL))
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END;

CREATE VIEW IF NOT EXISTS v_overdue AS
    SELECT * FROM items
    WHERE status IN ('pending', 'active') AND due < date('now')
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END,
        due ASC;

CREATE VIEW IF NOT EXISTS v_recent_nudges AS
    SELECT * FROM nudge_log
    WHERE timestamp > datetime('now', '-24 hours')
    ORDER BY timestamp DESC;

CREATE VIEW IF NOT EXISTS v_unprocessed_triggers AS
    SELECT * FROM triggers WHERE processed = 0 ORDER BY timestamp ASC;
"""

SEED_MECHANICAL_REMINDERS = """
INSERT OR IGNORE INTO mechanical_reminders (reminder_type, schedule_cron, message_template, context_rules) VALUES
    ('meds_morning', '0 7 * * *', '💊 Morning meds', '{"min_gap_hours": 12}'),
    ('cat_food', '30 7 * * 1-5', '🐱 Feed the cats', '{"min_gap_hours": 20}'),
    ('dishes', '0 16 * * 1-5', '🍽️ Dishes', '{"skip_if_away": true, "min_gap_hours": 20}'),
    ('litter_scoop', '30 16 * * 2,4', '🐱 Litter scoop', '{"min_gap_hours": 48}'),
    ('litter_full', '0 11 * * 0', '🐱 Full litter clean', '{"min_gap_hours": 168}'),
    ('pool_lamp', '0 20 * * *', '🏊 Pool lamp check', '{"skip_if_away": true}'),
    ('humidifier', '0 10 * * *', '💧 Humidifier water', '{"min_gap_hours": 20}');
"""


# ============================================================
# CLASS: Database
# ============================================================

class Database:
    """SQLite connection manager with WAL mode and busy timeout."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        return self.conn

    def ensure_schema(self):
        """Create all tables, indexes, views if they don't exist. Idempotent."""
        self.conn.executescript(SCHEMA_V2)
        self.conn.executescript(SEED_MECHANICAL_REMINDERS)
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        try:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logging.error(f"DB query error: {e} | SQL: {sql[:100]}")
            return []

    def execute(self, sql: str, params: tuple = ()) -> int:
        try:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            logging.error(f"DB execute error: {e} | SQL: {sql[:100]}")
            return -1

    def get_state(self, key: str, default: str = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value)
        )
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def migrate_from_v1(self):
        """Migrate from cognitive-loop.db to atlas-brain.db.
        
        Strategy: Copy old DB, add new columns to existing tables, create new tables.
        Preserves ALL existing data. No rows deleted.
        """
        if not OLD_DB_PATH.exists():
            logging.error(f"Old DB not found at {OLD_DB_PATH}")
            return False

        # Backup old DB
        backup_name = f"cognitive-loop.db.bak.pre-brain-{datetime.datetime.now().strftime('%Y%m%d%H%M')}"
        backup_path = STATE_DIR / backup_name
        shutil.copy2(OLD_DB_PATH, backup_path)
        logging.info(f"Backed up old DB to {backup_path}")

        # Copy old DB to new location
        if self.db_path.exists():
            # Back up existing new DB too
            shutil.copy2(self.db_path, self.db_path.with_suffix('.db.bak'))

        shutil.copy2(OLD_DB_PATH, self.db_path)
        logging.info(f"Copied {OLD_DB_PATH} → {self.db_path}")

        # Reconnect to the copied DB
        self.close()
        self.connect()

        # Add new columns to existing tables (ALTER TABLE ADD COLUMN is safe — no-ops if exists)
        alter_statements = [
            # items table — new columns
            "ALTER TABLE items ADD COLUMN estimated_minutes INTEGER",
            "ALTER TABLE items ADD COLUMN actual_minutes INTEGER",
            "ALTER TABLE items ADD COLUMN assigned_date TEXT",
            "ALTER TABLE items ADD COLUMN energy_window TEXT",
            "ALTER TABLE items ADD COLUMN source TEXT DEFAULT 'manual'",
            # learnings table — new columns
            "ALTER TABLE learnings ADD COLUMN source TEXT DEFAULT 'observation'",
            "ALTER TABLE learnings ADD COLUMN promoted INTEGER DEFAULT 0",
            "ALTER TABLE learnings ADD COLUMN created_at TEXT DEFAULT (datetime('now'))",
            # daily_metrics — new columns
            "ALTER TABLE daily_metrics ADD COLUMN actions_taken TEXT",
            "ALTER TABLE daily_metrics ADD COLUMN initiatives_worked INTEGER DEFAULT 0",
            "ALTER TABLE daily_metrics ADD COLUMN api_failures INTEGER DEFAULT 0",
            "ALTER TABLE daily_metrics ADD COLUMN total_loop_runs INTEGER DEFAULT 0",
        ]

        for stmt in alter_statements:
            try:
                self.conn.execute(stmt)
                logging.info(f"Migration: {stmt[:60]}... OK")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    logging.info(f"Migration: column already exists, skipping: {stmt[:60]}")
                else:
                    logging.warning(f"Migration warning: {e} | {stmt[:60]}")

        self.conn.commit()

        # Create all new tables + indexes + views (idempotent via IF NOT EXISTS)
        self.ensure_schema()

        # Set brain version
        self.set_state('brain_version', BRAIN_VERSION)

        # Verify migration
        return self.verify_migration()

    def verify_migration(self) -> bool:
        """Verify all tables exist and data is intact."""
        required_tables = [
            'items', 'nudge_log', 'learnings', 'triggers', 'system_state',
            'computed_rules', 'daily_metrics',
            'atlas_initiatives', 'memory_events', 'task_durations',
            'perception_log', 'mechanical_reminders'
        ]

        actual_tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]

        missing = [t for t in required_tables if t not in actual_tables]
        if missing:
            logging.error(f"VERIFY FAIL: Missing tables: {missing}")
            return False

        # Verify views
        required_views = ['v_active_items', 'v_today_items', 'v_overdue', 'v_recent_nudges', 'v_unprocessed_triggers']
        actual_views = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()]
        missing_views = [v for v in required_views if v not in actual_views]
        if missing_views:
            logging.error(f"VERIFY FAIL: Missing views: {missing_views}")
            return False

        # Verify items count matches original
        new_count = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        logging.info(f"VERIFY: items count = {new_count}")

        # Verify new columns exist on items
        cols = [c[1] for c in self.conn.execute("PRAGMA table_info(items)").fetchall()]
        required_cols = ['estimated_minutes', 'assigned_date', 'energy_window', 'source']
        missing_cols = [c for c in required_cols if c not in cols]
        if missing_cols:
            logging.error(f"VERIFY FAIL: items missing columns: {missing_cols}")
            return False

        # Verify mechanical reminders seeded
        mech_count = self.conn.execute("SELECT COUNT(*) FROM mechanical_reminders").fetchone()[0]
        if mech_count == 0:
            logging.error("VERIFY FAIL: mechanical_reminders not seeded")
            return False

        # Verify brain version
        version = self.get_state('brain_version')
        if version != BRAIN_VERSION:
            logging.warning(f"VERIFY WARN: brain_version = {version} (expected {BRAIN_VERSION})")

        logging.info(f"VERIFY PASS: {len(required_tables)} tables, {len(required_views)} views, {new_count} items, {mech_count} mechanical reminders, version {version}")
        return True


# ============================================================
# LOCK MANAGEMENT (with stale detection — Amendment 1)
# ============================================================

def acquire_lock() -> Optional[object]:
    """Acquire exclusive file lock with stale lock detection."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Check for stale lock before attempting
    if LOCK_PATH.exists():
        try:
            content = LOCK_PATH.read_text().strip()
            if '|' in content:
                pid_str, ts_str = content.split('|', 1)
                pid = int(pid_str)
                ts = float(ts_str)

                pid_alive = True
                try:
                    os.kill(pid, 0)
                except OSError:
                    pid_alive = False

                age = time.time() - ts
                if not pid_alive or age > LOCK_MAX_AGE_SECONDS:
                    logging.warning(f"Breaking stale lock: pid={pid} alive={pid_alive} age={age:.0f}s")
                    LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, IOError):
            LOCK_PATH.unlink(missing_ok=True)

    fd = open(str(LOCK_PATH), 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(f"{os.getpid()}|{time.time()}")
        fd.flush()
        return fd
    except IOError:
        fd.close()
        return None


def release_lock(lock_fd):
    """Release file lock."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass


# ============================================================
# LOGGING
# ============================================================

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.FileHandler(str(LOG_PATH), encoding='utf-8')]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )


# ============================================================
# CLASS: GatewayClient (HTTP to local OpenRouter-compatible gateway)
# ============================================================

OUTBOX_PATH = STATE_DIR / "loop-outbox.jsonl"


class GatewayClient:
    """LLM gateway client using direct HTTP to localhost."""

    def __init__(self, url: str = GATEWAY_URL, token: str = GATEWAY_TOKEN):
        self.url = url
        self.token = token

    def chat(self, system_prompt: str, user_message: str,
             model: str = DEFAULT_MODEL, temperature: float = 0.3,
             max_tokens: int = 1200) -> Optional[str]:
        """Send a chat completion request to the gateway. Returns content string or None."""
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode('utf-8')

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    logging.error(f"Gateway returned {resp.status}")
                    return None
                body = json.loads(resp.read().decode('utf-8'))
            return body["choices"][0]["message"]["content"]
        except Exception as e:
            logging.error(f"Gateway call failed: {e}")
            return None

    def send_telegram(self, text: str, thread_id: int = THREAD_MAIN) -> bool:
        """Send a message via Telegram Bot API. Log to outbox file."""
        payload = json.dumps({
            "chat_id": CHAT_ID,
            "message_thread_id": thread_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode('utf-8')

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                success = resp.status == 200
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")
            success = False

        # Log to outbox
        outbox_entry = {
            "timestamp": datetime.datetime.now(MST).isoformat(),
            "text": text,
            "thread_id": thread_id,
            "success": success,
        }
        try:
            OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTBOX_PATH, 'a') as f:
                f.write(json.dumps(outbox_entry) + '\n')
        except IOError as e:
            logging.error(f"Outbox write failed: {e}")

        return success


# ============================================================
# CLASS: MemoryEngine
# ============================================================

class MemoryEngine:
    """Four-layer memory system: working → episodic → semantic → procedural."""

    PROCEDURE_SECTIONS = ["Communication", "Timing", "Task Routing", "What Works", "What Doesn't"]
    MEMORY_MD_SIZE_CAP = 8192  # 8KB cap (Amendment 4)

    def __init__(self, db: Database, gateway: GatewayClient):
        self.db = db
        self.gateway = gateway
        self._ensure_procedure_md()

    def _ensure_procedure_md(self):
        """Create PROCEDURE.md template if it doesn't exist."""
        if PROCEDURE_MD.exists():
            return
        lines = ["# Procedural Memory", "",
                 "_How Atlas works with {user_name}. Updated by nightly distillation._", ""]
        for section in self.PROCEDURE_SECTIONS:
            lines.append(f"## {section}")
            lines.append("")
        PROCEDURE_MD.write_text("\n".join(lines) + "\n")
        logging.info("Created PROCEDURE.md template")

    # ── Layer 1: Working Memory ─────────────────────────────────

    def write_working(self, content: str, source: str):
        """Append to SESSION-STATE.md + memory_events table."""
        timestamp = datetime.datetime.now(MST).strftime("%H:%M")
        entry = f"- [{timestamp}] ({source}) {content}\n"

        SESSION_STATE_MD.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_STATE_MD, 'a') as f:
            f.write(entry)

        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('working', 'write', content, source)
        )

    # ── Layer 2: Episodic Memory ────────────────────────────────

    def write_episodic(self, event: str, source: str):
        """Append to memory/YYYY-MM-DD.md + memory_events table."""
        now = datetime.datetime.now(MST)
        today_file = MEMORY_DIR / f"{now.strftime('%Y-%m-%d')}.md"

        entry = f"- [{now.strftime('%H:%M')}] ({source}) {event}\n"

        today_file.parent.mkdir(parents=True, exist_ok=True)
        with open(today_file, 'a') as f:
            f.write(entry)

        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('episodic', 'write', event, source)
        )

    def distill_session(self):
        """Extract structured data from SESSION-STATE.md, write to episodic, clear working memory."""
        if not SESSION_STATE_MD.exists():
            return

        content = SESSION_STATE_MD.read_text()
        if len(content.strip()) < 50:
            return

        lines = [l for l in content.split('\n') if l.strip().startswith('- [')]
        if not lines:
            return

        now = datetime.datetime.now(MST)
        today_file = MEMORY_DIR / f"{now.strftime('%Y-%m-%d')}.md"

        summary = f"\n### Session Distilled @ {now.strftime('%H:%M')}\n"
        for line in lines:
            summary += f"{line}\n"
        summary += "\n"

        today_file.parent.mkdir(parents=True, exist_ok=True)
        with open(today_file, 'a') as f:
            f.write(summary)

        # Clear working memory
        SESSION_STATE_MD.write_text(f"# Session State\n_Last cleared: {now.isoformat()}_\n")

        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('working', 'distill', f"Distilled {len(lines)} entries to episodic", 'system')
        )
        logging.info(f"Session distilled: {len(lines)} entries → episodic")

    def nightly_distillation(self):
        """Delegate to NightlyDistillation class (kept for backward compat)."""
        logging.info("nightly_distillation: use NightlyDistillation.run() instead")

    # ── Layer 3: Semantic Memory ────────────────────────────────

    def promote_to_semantic(self, insight: str):
        """Append to MEMORY.md with duplicate detection and 8KB size cap (Amendment 4)."""
        content = MEMORY_MD.read_text() if MEMORY_MD.exists() else "# Memory\n"

        # Duplicate detection (jaccard similarity on tokenized words)
        for line in content.split('\n'):
            if line.strip() and self._jaccard_similarity(insight.lower(), line.lower()) > 0.7:
                logging.info(f"Duplicate semantic memory skipped: {insight[:80]}")
                return

        # Size cap check
        if len(content.encode('utf-8')) >= self.MEMORY_MD_SIZE_CAP:
            logging.warning(f"MEMORY.md at size cap ({self.MEMORY_MD_SIZE_CAP}B). Skipping promotion.")
            self.db.execute(
                "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
                ('semantic', 'cap_reached', insight, 'distillation')
            )
            return

        # Ensure Observations section exists
        if "## Observations" not in content:
            content += "\n## Observations\n"

        content += f"- {insight}\n"
        MEMORY_MD.write_text(content)

        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('semantic', 'promote', insight, 'distillation')
        )
        logging.info(f"Promoted to semantic: {insight[:80]}")

    @staticmethod
    def _jaccard_similarity(a: str, b: str) -> float:
        """Jaccard similarity on word tokens."""
        set_a = set(a.split())
        set_b = set(b.split())
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)

    # ── Layer 4: Procedural Memory ──────────────────────────────

    def update_procedural(self, pattern: str):
        """Append to PROCEDURE.md under appropriate section."""
        content = PROCEDURE_MD.read_text() if PROCEDURE_MD.exists() else ""
        if not content.strip():
            self._ensure_procedure_md()
            content = PROCEDURE_MD.read_text()

        section = self._classify_procedure_section(pattern)
        section_header = f"## {section}"

        if section_header in content:
            idx = content.index(section_header) + len(section_header)
            next_section = content.find("\n## ", idx)
            insert_point = next_section if next_section != -1 else len(content)
            content = content[:insert_point] + f"- {pattern}\n" + content[insert_point:]
        else:
            content += f"\n{section_header}\n- {pattern}\n"

        PROCEDURE_MD.write_text(content)
        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('procedural', 'update', pattern, 'distillation')
        )
        logging.info(f"Procedural update [{section}]: {pattern[:80]}")

    @staticmethod
    def _classify_procedure_section(pattern: str) -> str:
        """Determine which PROCEDURE.md section a pattern belongs to."""
        p = pattern.lower()
        if any(w in p for w in ['message', 'tone', 'say', 'ask', 'tell', 'word', 'phrase', 'emoji']):
            return "Communication"
        if any(w in p for w in ['time', 'hour', 'morning', 'evening', 'night', 'window', 'when', 'schedule']):
            return "Timing"
        if any(w in p for w in ['route', 'assign', 'category', 'priority', 'energy', 'slot']):
            return "Task Routing"
        if any(w in p for w in ['works', 'effective', 'respond', 'likes', 'prefers', 'good']):
            return "What Works"
        if any(w in p for w in ['ignore', 'annoy', 'bad', 'fail', 'avoid', 'hate', 'doesn']):
            return "What Doesn't"
        return "What Works"  # default

    # ── Corrections ─────────────────────────────────────────────

    def handle_correction(self, old_fact: str, new_fact: str, user_message: str):
        """Amendment 11: update MEMORY.md, log to corrections.jsonl, cancel stale nudges."""
        # 1. Update MEMORY.md
        if MEMORY_MD.exists():
            content = MEMORY_MD.read_text()
            if old_fact and old_fact in content:
                content = content.replace(old_fact, new_fact)
                MEMORY_MD.write_text(content)
                logging.info(f"MEMORY.md corrected: '{old_fact[:50]}' → '{new_fact[:50]}'")
            else:
                self.promote_to_semantic(f"{new_fact} (corrected {datetime.date.today().isoformat()})")

        # 2. Log to corrections.jsonl
        corrections_file = MEMORY_DIR / "corrections.jsonl"
        correction_entry = {
            "timestamp": datetime.datetime.now(MST).isoformat(),
            "old": old_fact,
            "new": new_fact,
            "user_said": user_message,
            "source": "explicit_correction"
        }
        corrections_file.parent.mkdir(parents=True, exist_ok=True)
        with open(corrections_file, 'a') as f:
            f.write(json.dumps(correction_entry) + '\n')

        # 3. Cancel stale nudges referencing corrected fact
        if old_fact:
            stale_nudges = self.db.query(
                "SELECT id, item_id, message FROM nudge_log "
                "WHERE timestamp > datetime('now', '-48 hours') AND message LIKE ?",
                (f"%{old_fact[:30]}%",)
            )
            for nudge in stale_nudges:
                logging.info(f"Cancelling stale nudge #{nudge['id']} referencing corrected fact")
                self.db.execute(
                    "UPDATE triggers SET processed=1 WHERE item_id=? AND processed=0",
                    (nudge['item_id'],)
                )

        # 4. Log memory event
        self.db.execute(
            "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
            ('semantic', 'correct', f"'{old_fact}' → '{new_fact}'", 'explicit_correction')
        )

    # ── Cleanup ─────────────────────────────────────────────────

    def cleanup_episodic(self, retention_days: int = 30):
        """Delete memory files older than retention_days."""
        cutoff = datetime.date.today() - datetime.timedelta(days=retention_days)
        removed = 0
        for f in MEMORY_DIR.glob("????-??-??.md"):
            try:
                file_date = datetime.date.fromisoformat(f.stem)
                if file_date < cutoff:
                    f.unlink()
                    removed += 1
            except ValueError:
                continue
        if removed:
            logging.info(f"Episodic cleanup: removed {removed} files older than {retention_days}d")


# ============================================================
# CLASS: PerceptionLayer
# ============================================================

class PerceptionLayer:
    """Reads {user_name}'s OpenClaw session transcripts and detects task signals."""

    INTENT_PATTERNS = {
        'done': [
            r'\b(done|finished|completed|took care of|wrapped up|handled|did that|checked off)\b',
            r'[✅☑️]',
        ],
        'snoozed': [
            r'\b(later|tonight|tomorrow|remind me|do it (at|on|in)|next week|after)\b',
            r'\b(not now|can wait|push it|move it)\b',
        ],
        'blocked': [
            r"\b(can't|cannot|waiting on|blocked by|depends on|need .+ first|stuck)\b",
        ],
        'cancelled': [
            r'\b(not doing|cancel|drop it|forget it|irrelevant|nah|nvm|never mind)\b',
        ],
        'acknowledged': [
            r'^(ok|okay|on it|noted|will do|yeah|yep|got it|bet|aight|copy)\s*[.!]?$',
        ],
        'hard_no': [
            r'\b(absolutely not|hell no|fuck off|stop asking|do not|never)\b',
        ],
        'new_task': [
            r'\b(need to|gotta|should|have to|todo|task|remind me to|add)\b.*',
        ],
        'mood_positive': [
            r"\b(great|awesome|perfect|stoked|pumped|crushing it|fired up|let's go)\b",
        ],
        'mood_negative': [
            r'\b(frustrated|stressed|exhausted|overwhelmed|pissed|annoyed|ugh|fuck)\b',
        ],
        'energy_low': [
            r'\b(tired|exhausted|burnt out|drained|low energy|wiped|dead)\b',
        ],
    }

    def __init__(self, db: Database, gateway: GatewayClient, memory: MemoryEngine):
        self.db = db
        self.gateway = gateway
        self.memory = memory

    # ── Watermark Management ────────────────────────────────────

    def _load_watermark(self) -> dict:
        """Load watermark or initialize on first run (Amendment 2)."""
        if WATERMARK_PATH.exists():
            try:
                wm = json.loads(WATERMARK_PATH.read_text())
                if wm.get('initialized'):
                    return wm
            except (json.JSONDecodeError, KeyError):
                pass
        return self._initialize_watermark()

    def _initialize_watermark(self) -> dict:
        """First run: skip all historical messages. Set watermark to end of latest file (Amendment 2)."""
        session_files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if session_files:
            latest = session_files[-1]
            stat = latest.stat()
            watermark = {
                "last_file": latest.name,
                "last_inode": stat.st_ino,
                "last_size": stat.st_size,
                "last_offset": stat.st_size,
                "last_message_id": None,
                "last_scan": datetime.datetime.now(MST).isoformat(),
                "initialized": True
            }
        else:
            watermark = {
                "last_file": None, "last_inode": 0, "last_size": 0,
                "last_offset": 0, "last_message_id": None,
                "last_scan": datetime.datetime.now(MST).isoformat(),
                "initialized": True
            }
        self._save_watermark(watermark)
        logging.info(f"Perception watermark initialized (first run)")
        return watermark

    def _save_watermark(self, watermark: dict):
        """Persist watermark to disk."""
        watermark['last_scan'] = datetime.datetime.now(MST).isoformat()
        WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATERMARK_PATH.write_text(json.dumps(watermark, indent=2))

    def _check_file_continuity(self, fpath: Path, watermark: dict) -> int:
        """Return correct starting offset, handling rotation (Amendment 2)."""
        stat = fpath.stat()
        if fpath.name != watermark.get('last_file'):
            return 0
        if stat.st_ino != watermark.get('last_inode', 0):
            logging.info(f"File {fpath.name} rotated (inode changed). Resetting offset.")
            return 0
        if stat.st_size < watermark.get('last_size', 0):
            logging.info(f"File {fpath.name} truncated. Resetting offset.")
            return 0
        return watermark.get('last_offset', 0)

    # ── Session File Scanning ───────────────────────────────────

    def scan_session_files(self) -> List[Dict]:
        """Read new messages from OpenClaw session JSONL files with inode-aware watermark."""
        if not SESSIONS_DIR.exists():
            logging.debug(f"Sessions dir not found: {SESSIONS_DIR}")
            return []

        watermark = self._load_watermark()
        messages: List[Dict] = []

        session_files = sorted(
            SESSIONS_DIR.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime
        )

        if not session_files:
            return []

        past_watermark = watermark['last_file'] is None
        for fpath in session_files:
            fname = fpath.name
            if not past_watermark:
                if fname == watermark['last_file']:
                    past_watermark = True
                else:
                    continue

            offset = self._check_file_continuity(fpath, watermark)

            try:
                with open(fpath, 'rb') as f:
                    f.seek(offset)
                    last_good_offset = offset
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        line_stripped = line.decode('utf-8', errors='replace').strip()
                        if not line_stripped:
                            last_good_offset = f.tell()
                            continue
                        try:
                            entry = json.loads(line_stripped)
                            last_good_offset = f.tell()
                        except json.JSONDecodeError:
                            # Partial write — stop here, never advance past failed parse
                            break

                        if entry.get('type') != 'message':
                            continue
                        msg = entry.get('message', {})
                        if msg.get('role') != 'user':
                            continue

                        msg_id = entry.get('id', '')
                        if msg_id == watermark.get('last_message_id'):
                            continue

                        # Extract text from content blocks, strip metadata
                        text = self._extract_text(msg.get('content', []))
                        if text.strip():
                            messages.append({
                                'id': msg_id,
                                'text': text.strip(),
                                'timestamp': entry.get('timestamp', ''),
                                'session_file': fname
                            })
            except (IOError, OSError) as e:
                logging.error(f"Error reading session file {fname}: {e}")
                continue

            # Update watermark for this file
            stat = fpath.stat()
            watermark['last_file'] = fname
            watermark['last_inode'] = stat.st_ino
            watermark['last_size'] = stat.st_size
            watermark['last_offset'] = last_good_offset
            if messages:
                watermark['last_message_id'] = messages[-1]['id']

        self._save_watermark(watermark)
        return messages

    @staticmethod
    def _extract_text(content) -> str:
        """Extract {user_name}'s actual text from OpenClaw message wrapper, stripping metadata blocks."""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = ' '.join(
                block.get('text', '')
                for block in content
                if isinstance(block, dict) and block.get('type') == 'text'
            )
        else:
            return ''
        # Strip common metadata/system blocks
        text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.DOTALL)
        text = re.sub(r'<available-deferred-tools>.*?</available-deferred-tools>', '', text, flags=re.DOTALL)
        return text.strip()

    # ── Intent Classification ───────────────────────────────────

    def classify_intent(self, text: str, recent_messages: List[Dict] = None) -> Dict:
        """Regex-first classification with LLM fallback. Sliding window of 5 messages (Amendment 6)."""
        # Regex pass
        regex_result = self._regex_classify(text)
        if regex_result['confidence'] >= 0.9:
            return regex_result

        # Build context window from last 5 messages for LLM
        context_window = ""
        if recent_messages and len(recent_messages) > 1:
            window = recent_messages[-5:]
            context_window = "\n".join(
                [f"[{m.get('timestamp', '?')}] {m['text'][:200]}" for m in window]
            )

        # LLM fallback — only if message seems potentially task-related
        if len(text.strip()) > 10 and any(word in text.lower() for word in
                ['task', 'item', 'remind', 'that thing', 'the', 'it', 'done', 'later']):
            if context_window:
                return self._llm_classify_with_context(text, context_window)
            return self._llm_classify(text)

        # If regex matched with lower confidence, return that
        if regex_result['signal_type'] != 'none':
            return regex_result

        return {'signal_type': 'none', 'confidence': 1.0, 'method': 'default', 'extracted': {}}

    def _regex_classify(self, text: str) -> Dict:
        """Fast regex-based intent classification."""
        text_lower = text.lower().strip()
        for signal_type, patterns in self.INTENT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    return {
                        'signal_type': signal_type,
                        'confidence': 0.8,
                        'method': 'regex',
                        'extracted': {}
                    }
        return {'signal_type': 'none', 'confidence': 1.0, 'method': 'regex', 'extracted': {}}

    def _llm_classify(self, text: str) -> Dict:
        """LLM fallback for ambiguous intent classification."""
        prompt = (
            f'Classify this message from {user_name} into ONE signal type.\n'
            f'Signal types: done, snoozed, blocked, new_task, acknowledged, cancelled, hard_no, mood_positive, mood_negative, energy_low, none\n\n'
            f'Message: "{text}"\n\n'
            f'Active tasks for context:\n{self._get_active_items_summary()}\n\n'
            f'Return JSON: {{"signal_type": "...", "item_id": null, "confidence": 0.0-1.0}}'
        )
        response = self.gateway.chat(
            system_prompt="You classify user messages into task signal types. Return ONLY valid JSON.",
            user_message=prompt,
            temperature=0.1,
            max_tokens=200
        )
        if response:
            try:
                result = json.loads(response)
                result.setdefault('confidence', 0.6)
                result['method'] = 'llm'
                result.setdefault('extracted', {})
                return result
            except json.JSONDecodeError:
                pass
        return {'signal_type': 'none', 'confidence': 0.5, 'method': 'llm_failed', 'extracted': {}}

    def _llm_classify_with_context(self, text: str, context: str) -> Dict:
        """LLM classification with sliding window context (Amendment 6)."""
        prompt = (
            f'Classify {user_name}\'s LATEST message as a task signal. Consider the conversation context.\n\n'
            f'Recent conversation:\n{context}\n\n'
            f'Latest message: "{text}"\n\n'
            f'Active tasks:\n{self._get_active_items_summary()}\n\n'
            f'Is the latest message a task signal (done/snoozed/blocked/cancelled/new_task/acknowledged) '
            f'or just conversation (none)?\n\n'
            f'Return JSON: {{"signal_type": "...", "item_id": null, "confidence": 0.0-1.0, "reasoning": "..."}}'
        )
        response = self.gateway.chat(
            system_prompt="You classify user messages into task signal types. Return ONLY valid JSON.",
            user_message=prompt,
            temperature=0.1,
            max_tokens=300
        )
        if response:
            try:
                result = json.loads(response)
                result.setdefault('confidence', 0.6)
                result['method'] = 'llm_context'
                result.setdefault('extracted', {})
                return result
            except json.JSONDecodeError:
                pass
        return {'signal_type': 'none', 'confidence': 0.5, 'method': 'llm_context_failed', 'extracted': {}}

    def _get_active_items_summary(self) -> str:
        """Get a short summary of active items for LLM context."""
        items = self.db.query("SELECT id, title, status FROM v_active_items LIMIT 10")
        if not items:
            return "(no active items)"
        return "\n".join(f"- [{i['id']}] {i['title']} ({i['status']})" for i in items)

    # ── Task Signal Extraction (Amendment 6) ────────────────────

    def extract_task_signal(self, message: Dict, recent_messages: List[Dict]) -> Optional[Dict]:
        """Extract task signal with false-positive prevention (Amendment 6)."""
        classification = self.classify_intent(message['text'], recent_messages)
        signal_type = classification['signal_type']
        confidence = classification['confidence']

        if signal_type == 'none':
            self.db.execute(
                "INSERT INTO perception_log (session_file, message_id, raw_message, signal_type, acted_on) "
                "VALUES (?, ?, ?, 'none', 0)",
                (message['session_file'], message['id'], message['text'][:500])
            )
            return None

        # Gate 1: Confidence threshold — only task signals need >= 0.7
        if signal_type in ('done', 'snoozed', 'blocked', 'cancelled', 'new_task', 'acknowledged', 'hard_no'):
            if confidence < 0.7:
                self._log_perception(message, signal_type, confidence, acted=False, reason="low_confidence")
                return None

        # Gate 2: Context scoping — task signals need recent nudge or item reference
        if signal_type in ('done', 'snoozed', 'blocked', 'cancelled'):
            is_addressed = self._was_atlas_nudge_recent(hours=2)
            has_ref = self._references_active_item(message['text'])
            if not (is_addressed or has_ref):
                self._log_perception(message, signal_type, confidence, acted=False, reason="not_addressed")
                return None

        # Gate 3: For 'done', item must have been nudged recently (2h), else downgrade
        item_id = self._fuzzy_match_item(message['text'])
        if signal_type == 'done' and item_id:
            last_nudge = self.db.query(
                "SELECT timestamp FROM nudge_log WHERE item_id=? AND action='NUDGE' "
                "ORDER BY timestamp DESC LIMIT 1",
                (item_id,)
            )
            if last_nudge:
                try:
                    nudge_ts = datetime.datetime.fromisoformat(last_nudge[0]['timestamp'])
                    if nudge_ts.tzinfo is None:
                        nudge_ts = nudge_ts.replace(tzinfo=MST)
                    age_hours = (datetime.datetime.now(MST) - nudge_ts).total_seconds() / 3600
                    if age_hours > 2:
                        signal_type = 'acknowledged'
                except (ValueError, TypeError):
                    pass

        # Extract temporal signals for snooze / task title for new_task
        extracted_value = {}
        if signal_type == 'snoozed':
            extracted_value['snooze_until'] = self._parse_temporal_reference(message['text'])
        elif signal_type == 'new_task':
            extracted_value['task_title'] = self._extract_task_title(message['text'])

        # Write to triggers table
        trigger_id = self.db.execute(
            "INSERT INTO triggers (item_id, response_type, user_said, inferred_schedule) "
            "VALUES (?, ?, ?, ?)",
            (item_id, signal_type, message['text'][:500],
             extracted_value.get('snooze_until'))
        )

        # Write to perception_log
        self.db.execute(
            "INSERT INTO perception_log (session_file, message_id, raw_message, signal_type, "
            "item_id, extracted_value, acted_on) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (message['session_file'], message['id'], message['text'][:500],
             signal_type, item_id, json.dumps(extracted_value))
        )

        # Update last_message_from_user
        self.db.set_state('last_message_from_user',
                          message.get('timestamp', datetime.datetime.now(MST).isoformat()))

        logging.info(f"Task signal: {signal_type} (conf={confidence:.2f}) item={item_id} | {message['text'][:80]}")
        return {
            'trigger_id': trigger_id,
            'signal_type': signal_type,
            'item_id': item_id,
            'extracted': extracted_value
        }

    # ── Context State ───────────────────────────────────────────

    def update_context_state(self, message: Dict):
        """Detect mood/energy from message, update system_state."""
        classification = self.classify_intent(message['text'])
        signal = classification['signal_type']

        if signal == 'mood_positive':
            self.db.set_state('user_current_mood', 'positive')
            self.memory.write_working("{user_name} seems in good mood", 'perception')
        elif signal == 'mood_negative':
            self.db.set_state('user_current_mood', 'negative')
            self.memory.write_working(
                f"{user_name} seems frustrated/stressed: {message['text'][:100]}", 'perception')
        elif signal == 'energy_low':
            self.db.set_state('user_energy_level', 'low')
            self.memory.write_working("{user_name} reports low energy", 'perception')

        self.db.set_state('last_message_from_user',
                          message.get('timestamp', datetime.datetime.now(MST).isoformat()))

    # ── Orchestrator ────────────────────────────────────────────

    def process_new_messages(self) -> int:
        """Main orchestrator: scan → classify → extract → update. Returns count of signals found."""
        messages = self.scan_session_files()
        if not messages:
            logging.debug("Perception: no new messages")
            return 0

        logging.info(f"Perception: processing {len(messages)} new message(s)")
        signals_found = 0

        for i, message in enumerate(messages):
            # Build recent_messages window (up to 5 prior messages including this one)
            start = max(0, i - 4)
            recent = messages[start:i + 1]

            # Extract task signal (handles classification internally)
            signal = self.extract_task_signal(message, recent)
            if signal:
                signals_found += 1

            # Update mood/energy context (separate from task signals)
            self.update_context_state(message)

        logging.info(f"Perception complete: {len(messages)} messages, {signals_found} signals")
        return signals_found

    # ── Helper Methods ──────────────────────────────────────────

    def _log_perception(self, message: Dict, signal_type: str, confidence: float,
                        acted: bool, reason: str = ""):
        """Log a perception event to the perception_log table."""
        self.db.execute(
            "INSERT INTO perception_log (session_file, message_id, raw_message, signal_type, "
            "extracted_value, acted_on) VALUES (?, ?, ?, ?, ?, ?)",
            (message['session_file'], message['id'], message['text'][:500],
             signal_type, json.dumps({'confidence': confidence, 'reason': reason}),
             1 if acted else 0)
        )

    def _was_atlas_nudge_recent(self, hours: int = 2) -> bool:
        """Check if Atlas sent a nudge recently."""
        rows = self.db.query(
            "SELECT COUNT(*) as n FROM nudge_log "
            "WHERE timestamp > datetime('now', ? || ' hours')",
            (f"-{hours}",)
        )
        return rows[0]['n'] > 0 if rows else False

    def _references_active_item(self, text: str) -> bool:
        """Check if text references any active item by title keywords."""
        items = self.db.query("SELECT id, title FROM v_active_items LIMIT 20")
        text_lower = text.lower()
        for item in items:
            # Check if significant words from title appear in message
            title_words = [w for w in item['title'].lower().split() if len(w) > 3]
            if title_words and any(w in text_lower for w in title_words):
                return True
        return False

    def _fuzzy_match_item(self, text: str) -> Optional[str]:
        """Try to match message text to an active item. Returns item_id or None."""
        items = self.db.query("SELECT id, title FROM v_active_items LIMIT 20")
        text_lower = text.lower()
        best_match = None
        best_score = 0

        for item in items:
            title_words = set(w for w in item['title'].lower().split() if len(w) > 3)
            if not title_words:
                continue
            text_words = set(text_lower.split())
            overlap = len(title_words & text_words)
            score = overlap / len(title_words)
            if score > best_score and score >= 0.3:
                best_score = score
                best_match = item['id']

        return best_match

    @staticmethod
    def _parse_temporal_reference(text: str) -> Optional[str]:
        """Extract temporal reference from text (tomorrow, tonight, next week, etc.)."""
        text_lower = text.lower()
        now = datetime.datetime.now(MST)

        if 'tomorrow' in text_lower:
            return (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        if 'tonight' in text_lower:
            return now.strftime('%Y-%m-%d') + 'T20:00'
        if 'next week' in text_lower:
            return (now + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        if 'later' in text_lower:
            return (now + datetime.timedelta(hours=3)).isoformat()

        # Try to find "at HH" or "at HH:MM"
        time_match = re.search(r'at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text_lower)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            ampm = time_match.group(3)
            if ampm == 'pm' and hour < 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            return target.isoformat()

        return None

    @staticmethod
    def _extract_task_title(text: str) -> str:
        """Extract a task title from a new_task message."""
        # Strip common prefixes
        cleaned = re.sub(
            r'^(i\s+)?(need to|gotta|should|have to|todo|task|remind me to|add)\s+',
            '', text.strip(), flags=re.IGNORECASE
        )
        # Truncate to reasonable length
        return cleaned[:120].strip()


# ============================================================
# CLASS: TaskRouter
# ============================================================

class TaskRouter:
    """Calendar-and-energy-aware task routing and scheduling."""

    CATEGORY_DEFAULTS = {
        'work': 45,
        'personal': 20,
        'home': 30,
        'health': 30,
        'capstone': 90,
        'admin': 15,
        'general': 25,
    }

    def __init__(self, db: Database, gateway: GatewayClient):
        self.db = db
        self.gateway = gateway

    # ── Duration Estimation ────────────────────────────────────

    def estimate_duration(self, item) -> int:
        """Estimate task duration in minutes from historical data + category defaults."""
        # Step 1: Check historical data
        category = item['category'] or 'general'
        matches = self.db.query(
            "SELECT * FROM task_durations WHERE category = ? ORDER BY confidence DESC",
            (category,)
        )
        for match in matches:
            keywords = json.loads(match['keywords']) if match['keywords'] else []
            title_words = set((item['title'] or '').lower().split())
            if keywords:
                overlap = len(set(keywords) & title_words) / max(len(keywords), 1)
                if overlap > 0.3 and match['confidence'] > 0.5:
                    return match['estimated_minutes']

        # Step 2: Category defaults
        base = self.CATEGORY_DEFAULTS.get(category, 25)

        # Step 3: Priority adjustment (high/critical get 1.3x)
        priority = item['priority'] or 'medium'
        if priority in ('high', 'critical'):
            base = int(base * 1.3)

        # Step 4: Prefer existing estimate
        if item.get('estimated_minutes'):
            return item['estimated_minutes']

        return base

    # ── Energy & Day Type ──────────────────────────────────────

    def get_energy_window(self) -> str:
        """Return current energy window name based on MST hour."""
        hour = datetime.datetime.now(MST).hour
        for name, (start, end) in ENERGY_WINDOWS.items():
            if start <= hour < end:
                return name
        return 'off_hours'

    def get_day_type(self) -> str:
        """Return 'parenting_day' (Wed/Fri), 'weekend' (Sat/Sun), or 'work_day'."""
        today = datetime.datetime.now(MST).date()
        return self._get_day_type_for_date(today)

    @staticmethod
    def _get_day_type_for_date(d: datetime.date) -> str:
        wd = d.weekday()  # Mon=0 … Sun=6
        if wd in PARENTING_DAYS:
            return 'parenting_day'
        if wd >= 5:
            return 'weekend'
        return 'work_day'

    # ── Calendar Gaps (with gog fallback — Amendment 7) ────────

    _calendar_cache: Dict[str, List[Tuple[int, int]]] = {}

    def get_calendar_gaps(self, date_str: str) -> List[Tuple[int, int]]:
        # Cache per date per run — avoid hammering gog for every item
        if date_str in self._calendar_cache:
            return self._calendar_cache[date_str]
        gaps = self._fetch_calendar_gaps(date_str)
        self._calendar_cache[date_str] = gaps
        return gaps

    def _fetch_calendar_gaps(self, date_str: str) -> List[Tuple[int, int]]:
        """Call gog CLI to get calendar events, return free windows as (start_hour, end_hour) tuples."""
        try:
            result = subprocess.run(
                ["gog", "calendar", "list", "--from", date_str, "--to", date_str, "--all", "--json"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ,
                     "GOG_KEYRING_PASSWORD": os.environ.get("GOG_KEYRING_PASSWORD", ""),
                     "GOG_ACCOUNT": os.environ.get("GOG_ACCOUNT", "")}
            )
            if result.returncode != 0:
                self._handle_gog_failure(f"exit code {result.returncode}: {result.stderr[:200]}")
                return self._energy_window_fallback(date_str)

            raw = json.loads(result.stdout) if result.stdout.strip() else []
            # gog wraps events in {"events": [...]} dict
            if isinstance(raw, dict):
                events = raw.get('events', []) or []
            elif isinstance(raw, list):
                events = raw
            else:
                events = []
        except subprocess.TimeoutExpired:
            self._handle_gog_failure("timeout after 15s")
            return self._energy_window_fallback(date_str)
        except Exception as e:
            self._handle_gog_failure(str(e))
            return self._energy_window_fallback(date_str)

        # Track success
        self.db.set_state('gog_last_success', datetime.datetime.now(MST).isoformat())

        # Parse events into busy blocks
        busy = []
        for event in events:
            if not isinstance(event, dict):
                continue
            start_val = event.get('start', {})
            end_val = event.get('end', {})
            # Handle nested format: {"dateTime": "..."} or {"date": "..."}
            if isinstance(start_val, dict):
                start_str = start_val.get('dateTime', start_val.get('date', ''))
            else:
                start_str = str(start_val)
            if isinstance(end_val, dict):
                end_str = end_val.get('dateTime', end_val.get('date', ''))
            else:
                end_str = str(end_val)
            start = self._parse_event_time(start_str)
            end = self._parse_event_time(end_str)
            if start is not None and end is not None:
                busy.append((start, end))
        busy.sort()

        # Compute gaps between 6 AM and 10 PM
        gaps = []
        cursor = 6
        for bstart, bend in busy:
            if bstart > cursor:
                gaps.append((cursor, bstart))
            cursor = max(cursor, bend)
        if cursor < 22:
            gaps.append((cursor, 22))

        return gaps if gaps else [(6, 22)]

    def _energy_window_fallback(self, date_str: str) -> List[Tuple[int, int]]:
        """Fallback: use energy windows only, no calendar data."""
        logging.warning("Using energy-window-only scheduling (no calendar)")
        try:
            day = datetime.date.fromisoformat(date_str)
        except ValueError:
            return [(8, 11), (11, 15), (15, 19)]
        if day.weekday() in PARENTING_DAYS:
            return [(8, 11), (20, 22)]
        return [(8, 11), (11, 15), (15, 19)]

    def _handle_gog_failure(self, reason: str):
        """Track gog failures for diagnostics."""
        logging.warning(f"gog CLI unavailable: {reason}")
        self.db.set_state('gog_last_failure', f"{datetime.datetime.now(MST).isoformat()}|{reason}")
        last_success = self.db.get_state('gog_last_success')
        if last_success:
            try:
                hours_since = (datetime.datetime.now(MST) -
                               datetime.datetime.fromisoformat(last_success)).total_seconds() / 3600
                if hours_since > 24:
                    logging.error("gog CLI has been unavailable for >24 hours")
            except (ValueError, TypeError):
                pass

    @staticmethod
    def _parse_event_time(time_str: str) -> Optional[int]:
        """Parse an event time string to an hour integer."""
        if not time_str:
            return None
        try:
            # Try ISO datetime
            dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            return dt.hour + (1 if dt.minute >= 30 else 0)
        except (ValueError, TypeError):
            pass
        # Try "HH:MM" format
        m = re.match(r'(\d{1,2}):(\d{2})', str(time_str))
        if m:
            return int(m.group(1))
        return None

    # ── Optimal Slot Finding ───────────────────────────────────

    def find_optimal_slot(self, item, duration: int) -> Optional[str]:
        """Find best date for a task by scoring energy match + freshness + gap size."""
        energy_required = item.get('energy') or 'medium'
        preferred_windows = self._energy_to_windows(energy_required)

        today = datetime.date.today()
        best_score = -1
        best_date = None

        for day_offset in range(0, 8):
            candidate = today + datetime.timedelta(days=day_offset)
            day_type = self._get_day_type_for_date(candidate)

            # Skip parenting days for high-energy work
            if day_type == 'parenting_day' and energy_required == 'high':
                continue

            gaps = self.get_calendar_gaps(candidate.isoformat())

            for gap_start, gap_end in gaps:
                gap_duration = (gap_end - gap_start) * 60  # minutes
                if gap_duration < duration:
                    continue

                window_name = self._hour_to_window(gap_start)
                energy_score = 3 if window_name in preferred_windows else 1
                freshness_score = max(0, 7 - day_offset)
                gap_score = min(gap_duration / duration, 3)

                total_score = energy_score + freshness_score + gap_score
                if total_score > best_score:
                    best_score = total_score
                    best_date = candidate.isoformat()

        return best_date

    @staticmethod
    def _energy_to_windows(energy: str) -> List[str]:
        if energy == 'high':
            return ['morning_peak']
        elif energy == 'medium':
            return ['morning_peak', 'midday']
        else:
            return ['afternoon', 'evening']

    @staticmethod
    def _hour_to_window(hour: int) -> str:
        for name, (start, end) in ENERGY_WINDOWS.items():
            if start <= hour < end:
                return name
        return 'off_hours'

    # ── Auto-Assign Date ───────────────────────────────────────

    def auto_assign_date(self, item) -> str:
        """Full decision tree for auto-assigning a work date."""
        today = datetime.date.today()

        # Rule 1: Urgent due date (within 2 days)
        if item.get('due'):
            try:
                due = datetime.date.fromisoformat(item['due'][:10])
                if (due - today).days <= 2:
                    return today.isoformat()
            except (ValueError, TypeError):
                pass

        # Rule 2: Critical priority
        if item.get('priority') == 'critical':
            return today.isoformat()

        # Rule 3: Explicit schedule
        if item.get('scheduled_at'):
            return item['scheduled_at'][:10]

        # Rule 4-5: Smart routing
        duration = self.estimate_duration(item)
        optimal = self.find_optimal_slot(item, duration)
        if optimal:
            return optimal

        # Rule 6: Fallback — today + 3
        fallback = today + datetime.timedelta(days=3)
        return fallback.isoformat()

    # ── Mechanical Reminders ───────────────────────────────────

    def route_mechanical_reminder(self, reminder_type: str, context: Dict) -> Optional[str]:
        """Decide whether to surface a mechanical reminder right now."""
        reminders = self.db.query(
            "SELECT * FROM mechanical_reminders WHERE reminder_type = ? AND enabled = 1",
            (reminder_type,)
        )
        if not reminders:
            return None
        reminder = reminders[0]

        now = datetime.datetime.now(MST)
        rules = json.loads(reminder['context_rules']) if reminder['context_rules'] else {}

        # Check schedule window (±15 min)
        if not self._in_schedule_window(reminder['schedule_cron'], now, window_minutes=15):
            return None

        # Check acknowledgment gap
        if reminder.get('last_acknowledged'):
            try:
                last_ack = datetime.datetime.fromisoformat(reminder['last_acknowledged'])
                if last_ack.tzinfo is None:
                    last_ack = last_ack.replace(tzinfo=MST)
                min_gap = rules.get('min_gap_hours', 20)
                if (now - last_ack).total_seconds() < min_gap * 3600:
                    return None
            except (ValueError, TypeError):
                pass

        # Check suppression
        if reminder.get('suppressed_until'):
            try:
                suppress_until = datetime.datetime.fromisoformat(reminder['suppressed_until'])
                if suppress_until.tzinfo is None:
                    suppress_until = suppress_until.replace(tzinfo=MST)
                if now < suppress_until:
                    return None
            except (ValueError, TypeError):
                pass

        # Meeting check
        if context.get('in_meeting'):
            return None

        # Quiet hours (meds exempt)
        if self._is_quiet_hours() and 'meds' not in reminder_type:
            return None

        # Parenting day filter
        if context.get('day_type') == 'parenting_day' and reminder_type in ('dishes', 'pool_lamp'):
            return None

        return reminder['message_template']

    @staticmethod
    def _in_schedule_window(cron_expr: str, now: datetime.datetime, window_minutes: int = 15) -> bool:
        """Check if current time is within ±window_minutes of a cron schedule.
        Supports simple cron: 'M H * * DOW' format."""
        try:
            parts = cron_expr.strip().split()
            if len(parts) < 5:
                return False
            cron_minute = int(parts[0])
            cron_hour = int(parts[1])
            dow_spec = parts[4]

            # Check day-of-week
            if dow_spec != '*':
                allowed_days = set()
                for chunk in dow_spec.split(','):
                    if '-' in chunk:
                        lo, hi = chunk.split('-', 1)
                        allowed_days.update(range(int(lo), int(hi) + 1))
                    else:
                        allowed_days.add(int(chunk))
                # cron: 0=Sun, python isoweekday: 1=Mon..7=Sun → convert
                py_dow = now.isoweekday() % 7  # Sun=0, Mon=1..Sat=6
                if py_dow not in allowed_days:
                    return False

            # Check time window
            sched = now.replace(hour=cron_hour, minute=cron_minute, second=0, microsecond=0)
            diff = abs((now - sched).total_seconds()) / 60
            return diff <= window_minutes
        except (ValueError, IndexError):
            return False

    @staticmethod
    def _is_quiet_hours() -> bool:
        hour = datetime.datetime.now(MST).hour
        return hour >= QUIET_START or hour < QUIET_END

    def check_pending_reminders(self, context: Dict) -> List[str]:
        """Check all enabled mechanical reminders and return messages for those that should fire."""
        reminders = self.db.query("SELECT reminder_type FROM mechanical_reminders WHERE enabled = 1")
        pending = []
        for r in reminders:
            msg = self.route_mechanical_reminder(r['reminder_type'], context)
            if msg:
                pending.append(msg)
                # Update last_fired
                self.db.execute(
                    "UPDATE mechanical_reminders SET last_fired = ? WHERE reminder_type = ?",
                    (datetime.datetime.now(MST).isoformat(), r['reminder_type'])
                )
        return pending


# ============================================================
# CLASS: ActionLayer
# ============================================================

ACTION_SYSTEM_PROMPT = """You are Atlas, {user_name}'s personal AI agent. You're deciding what action to take right now.

## About {user_name}
- Read USER.md for personal details, role, and context.
- Check schedule for peak hours and reduced-interruption days.
- Communication style: match the user's energy. No AI-isms.
- Hard "no" = drop it forever. Lazy ignore = persist intelligently.

## Available Actions
- NUDGE: Remind {user_name} about a specific task. Must reference item_id. Casual tone.
- CHECKIN: Ask how {user_name}'s doing / if they need anything. No task reference.
- ANTICIPATE: Proactively do something {user_name} hasn't asked for yet. Requires reasoning.
- REFLECT: Share an observation about patterns or progress. Brief, not preachy.
- PROPOSE: Suggest a new approach, tool, or plan. Actionable.
- SURFACE: Share initiative findings. Must be ready. Don't surface half-baked work.
- WORK: Atlas does background work (no message to {user_name}). Log what was done.
- ROUTE: Route a task to a better time/day. Explain why briefly.
- SILENCE: Do nothing. This is ALWAYS valid. Prefer silence when:
  - {user_name} is in quiet hours or a meeting
  - Last message was < 15 min ago
  - Nothing meaningful to say
  - {user_name} is clearly busy / parenting
  - Daily nudge count is high

## Guardrails (HARD RULES)
- NEVER send external messages (email, social) without explicit approval
- NEVER deploy to production
- NEVER spend money
- NEVER surface incomplete initiative findings
- NEVER nudge the same item within {cooldown} minutes
- NEVER send more than {max_daily} messages per day
- SILENCE is always the safest option

## Output Format
Return ONLY valid JSON:
{{
    "action": "NUDGE|CHECKIN|ANTICIPATE|REFLECT|PROPOSE|SURFACE|WORK|ROUTE|SILENCE",
    "item_id": null,
    "message": "the message to send {user_name} (null for WORK/SILENCE)",
    "reasoning": "why this action right now (internal, not shown to {user_name})",
    "urgency": "low|medium|high"
}}"""

ACTION_USER_TEMPLATE = """## Current Context
- Time: {current_time} MST ({day_name})
- Day type: {day_type}
- Energy window: {energy_window}
- {user_name}'s mood: {user_mood}
- {user_name}'s energy: {user_energy}
- Last message from {user_name}: {last_message_time} ({minutes_ago} min ago)
- In meeting: {in_meeting}
- Messages sent today: {daily_nudge_count}/{max_daily}
- Last action: {last_action} ({last_action_time})

## Today's Tasks (priority order)
{today_tasks}

## Overdue Tasks
{overdue_tasks}

## Recent Actions (last 6 hours)
{recent_actions}

## Pending Mechanical Reminders
{mechanical_pending}

## Active Initiatives with Findings Ready
{ready_initiatives}

## Triggers to Act On
{pending_triggers}

## Procedural Memory (How to Work with {user_name})
{procedure_summary}

What should Atlas do right now?"""


class ActionLayer:
    """Context-aware action decision engine."""

    def __init__(self, db: Database, gateway: GatewayClient, memory: MemoryEngine):
        self.db = db
        self.gateway = gateway
        self.memory = memory
        self.dry_run = False

    # ── Context Gathering ──────────────────────────────────────

    def gather_context(self, task_router: 'TaskRouter') -> Dict:
        """Build full context dict for the decision call."""
        now = datetime.datetime.now(MST)

        # Active items (top 15)
        active_items = self.db.query("SELECT * FROM v_active_items LIMIT 15")

        # Today's items
        today_items = self.db.query("SELECT * FROM v_today_items LIMIT 10")

        # Overdue items
        overdue_items = self.db.query("SELECT * FROM v_overdue LIMIT 10")

        # Recent nudges (24h)
        recent_nudges = self.db.query("SELECT * FROM v_recent_nudges LIMIT 20")

        # Recent actions (6h)
        recent_actions = self.db.query(
            "SELECT action, item_id, message, timestamp FROM nudge_log "
            "WHERE timestamp > datetime('now', '-6 hours') ORDER BY timestamp DESC LIMIT 10"
        )

        # Daily metrics
        metrics = self.db.query("SELECT * FROM daily_metrics WHERE date = date('now')")
        daily_nudge_count = metrics[0]['nudges_sent'] if metrics else 0

        # Last action
        last_action_row = self.db.query(
            "SELECT action, timestamp FROM nudge_log ORDER BY timestamp DESC LIMIT 1"
        )
        last_action = last_action_row[0]['action'] if last_action_row else 'none'
        last_action_time = last_action_row[0]['timestamp'] if last_action_row else 'never'

        # Last message from {user_name}
        last_msg_ts = self.db.get_state('last_message_from_user', 'never')
        minutes_ago = 9999
        if last_msg_ts and last_msg_ts != 'never':
            try:
                last_msg_dt = datetime.datetime.fromisoformat(last_msg_ts)
                if last_msg_dt.tzinfo is None:
                    last_msg_dt = last_msg_dt.replace(tzinfo=MST)
                minutes_ago = int((now - last_msg_dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass

        # Pending mechanical reminders
        day_type = task_router.get_day_type()
        energy_window = task_router.get_energy_window()
        reminder_context = {'day_type': day_type, 'in_meeting': False}
        mechanical_pending = task_router.check_pending_reminders(reminder_context)

        # Pending triggers
        pending_triggers = self.db.query("SELECT * FROM v_unprocessed_triggers LIMIT 10")

        # Ready initiatives
        ready_initiatives = self.db.query(
            "SELECT id, title, findings FROM atlas_initiatives "
            "WHERE status = 'active' AND surface_when_ready = 1 AND findings IS NOT NULL "
            "AND length(findings) > 100 LIMIT 5"
        )

        # Procedural memory summary
        procedure_summary = ""
        if PROCEDURE_MD.exists():
            try:
                procedure_summary = PROCEDURE_MD.read_text()[:1500]
            except IOError:
                pass

        # Format for template
        def fmt_items(items, limit=10):
            if not items:
                return "(none)"
            lines = []
            for it in items[:limit]:
                line = f"- [{it['id']}] {it['title']} (pri={it.get('priority','?')}, energy={it.get('energy','?')})"
                if it.get('due'):
                    line += f" due={it['due']}"
                if it.get('assigned_date'):
                    line += f" assigned={it['assigned_date']}"
                lines.append(line)
            return "\n".join(lines)

        def fmt_actions(actions):
            if not actions:
                return "(none)"
            return "\n".join(
                f"- [{a['timestamp']}] {a['action']}: {(a.get('message') or '')[:80]}"
                for a in actions
            )

        def fmt_triggers(triggers):
            if not triggers:
                return "(none)"
            return "\n".join(
                f"- [{t['response_type']}] item={t.get('item_id','?')}: {(t.get('user_said') or '')[:80]}"
                for t in triggers
            )

        def fmt_initiatives(inits):
            if not inits:
                return "(none)"
            return "\n".join(f"- [{i['id']}] {i['title']}" for i in inits)

        context = {
            'current_time': now.strftime('%H:%M'),
            'day_name': now.strftime('%A'),
            'day_type': day_type,
            'energy_window': energy_window,
            'user_mood': self.db.get_state('user_current_mood', 'unknown'),
            'user_energy': self.db.get_state('user_energy_level', 'unknown'),
            'last_message_time': last_msg_ts,
            'minutes_ago': minutes_ago,
            'in_meeting': False,
            'daily_nudge_count': daily_nudge_count,
            'max_daily': MAX_DAILY_NUDGES,
            'last_action': last_action,
            'last_action_time': last_action_time,
            'today_tasks': fmt_items(today_items),
            'overdue_tasks': fmt_items(overdue_items),
            'recent_actions': fmt_actions(recent_actions),
            'mechanical_pending': "\n".join(f"- {m}" for m in mechanical_pending) if mechanical_pending else "(none)",
            'ready_initiatives': fmt_initiatives(ready_initiatives),
            'pending_triggers': fmt_triggers(pending_triggers),
            'procedure_summary': procedure_summary[:1500] if procedure_summary else "(none)",
            # Raw data for validators
            '_active_items': active_items,
            '_recent_nudges': recent_nudges,
            '_mechanical_pending': mechanical_pending,
        }
        return context

    # ── Decision ───────────────────────────────────────────────

    def decide(self, context: Dict) -> Dict:
        """Call Sonnet to decide what action to take."""
        system = ACTION_SYSTEM_PROMPT.format(
            cooldown=COOLDOWN_MINUTES,
            max_daily=MAX_DAILY_NUDGES,
        )
        user_msg = ACTION_USER_TEMPLATE.format(**{
            k: v for k, v in context.items() if not k.startswith('_')
        })

        response = self.gateway.chat(
            system_prompt=system,
            user_message=user_msg,
            temperature=0.4,
            max_tokens=500,
        )

        if not response:
            logging.warning("Action decision call failed — defaulting to SILENCE")
            return {'action': 'SILENCE', 'reasoning': 'gateway_failure'}

        # Parse JSON response
        try:
            # Strip markdown fences if present
            cleaned = response.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            decision = json.loads(cleaned)
            decision.setdefault('action', 'SILENCE')
            decision.setdefault('item_id', None)
            decision.setdefault('message', None)
            decision.setdefault('reasoning', '')
            decision.setdefault('urgency', 'low')
            return decision
        except json.JSONDecodeError:
            logging.warning(f"Malformed action response: {response[:200]}")
            return {'action': 'SILENCE', 'reasoning': f'parse_error: {response[:100]}'}

    # ── Validation ─────────────────────────────────────────────

    def validate(self, decision: Dict, context: Dict) -> bool:
        """Check rate limits, cooldowns, quiet hours, action-specific validators."""
        action = decision.get('action', 'SILENCE')

        # SILENCE and WORK always valid
        if action in ('SILENCE', 'WORK'):
            return True

        # Rate limit
        if not self.check_rate_limit():
            logging.info(f"Rate limit hit. Forcing SILENCE instead of {action}")
            return False

        # Global cooldown
        if not self.check_cooldown():
            logging.info(f"Global cooldown active. Forcing SILENCE instead of {action}")
            return False

        # Quiet hours
        if self.check_quiet_hours():
            logging.info(f"Quiet hours. Forcing SILENCE instead of {action}")
            return False

        # Per-item cooldown for NUDGE
        if action == 'NUDGE':
            if not decision.get('item_id'):
                logging.info("NUDGE requires item_id. Rejecting.")
                return False
            if not self.check_cooldown(decision['item_id']):
                logging.info(f"Item cooldown active for {decision['item_id']}. Forcing SILENCE.")
                return False

        # Dedup check — Jaccard against last 6 hours
        if decision.get('message') and action in ('NUDGE', 'CHECKIN'):
            if self._is_duplicate_message(decision['message']):
                logging.info(f"Duplicate message detected for {action}. Rejecting.")
                return False

        # Action-specific validators
        if action == 'CHECKIN':
            if not decision.get('message'):
                return False
        elif action in ('ANTICIPATE', 'PROPOSE'):
            if not decision.get('message') or not decision.get('reasoning'):
                return False
        elif action == 'REFLECT':
            if not decision.get('message'):
                return False
        elif action == 'SURFACE':
            if not decision.get('message'):
                return False
        elif action == 'ROUTE':
            if not decision.get('item_id') or not decision.get('message'):
                return False

        return True

    # ── Execution ──────────────────────────────────────────────

    def execute(self, decision: Dict, dry_run: bool = False) -> bool:
        """Send Telegram, log to nudge_log, update metrics."""
        action = decision.get('action', 'SILENCE')
        message = decision.get('message')
        item_id = decision.get('item_id')

        if action == 'SILENCE':
            logging.info(f"SILENCE: {decision.get('reasoning', 'no reason')}")
            return True

        if action == 'WORK':
            logging.info(f"WORK: {decision.get('reasoning', 'background work')}")
            self.memory.write_episodic(f"Background work: {decision.get('reasoning', '')}", 'action')
            return True

        if not message:
            logging.error(f"Action {action} has no message. Aborting.")
            return False

        if dry_run or self.dry_run:
            logging.info(f"DRY RUN [{action}]: {message}")
        else:
            success = self.gateway.send_telegram(message)
            if not success:
                logging.error(f"Telegram send failed for {action}")
                # Track failure
                self.db.execute(
                    "INSERT INTO daily_metrics (date, api_failures) VALUES (date('now'), 1) "
                    "ON CONFLICT(date) DO UPDATE SET api_failures = api_failures + 1"
                )
                return False

        # Log to nudge_log
        self.db.execute(
            "INSERT INTO nudge_log (item_id, action, message, reasoning) VALUES (?, ?, ?, ?)",
            (item_id, action, message, decision.get('reasoning'))
        )

        # Update item nudge count
        if item_id:
            self.db.execute(
                "UPDATE items SET nudge_count = nudge_count + 1, last_nudge = datetime('now'), "
                "updated_at = datetime('now') WHERE id = ?",
                (item_id,)
            )

        # Write to episodic
        self.memory.write_episodic(f"Sent {action} to {user_name}: {message[:100]}", 'action')

        # Update daily metrics
        self.db.execute(
            "INSERT INTO daily_metrics (date, nudges_sent) VALUES (date('now'), 1) "
            "ON CONFLICT(date) DO UPDATE SET nudges_sent = nudges_sent + 1"
        )

        return True

    # ── Cooldown / Rate Limit / Quiet Hours ────────────────────

    def check_cooldown(self, item_id=None) -> bool:
        """Per-item and global cooldown checks. Returns True if OK to proceed."""
        if item_id:
            last = self.db.query(
                "SELECT timestamp FROM nudge_log WHERE item_id = ? ORDER BY timestamp DESC LIMIT 1",
                (item_id,)
            )
            if last:
                try:
                    elapsed = (datetime.datetime.now(MST) -
                               datetime.datetime.fromisoformat(last[0]['timestamp']).replace(tzinfo=MST)
                               ).total_seconds() / 60
                    return elapsed >= COOLDOWN_MINUTES
                except (ValueError, TypeError):
                    pass
        else:
            last = self.db.query(
                "SELECT timestamp FROM nudge_log ORDER BY timestamp DESC LIMIT 1"
            )
            if last:
                try:
                    elapsed = (datetime.datetime.now(MST) -
                               datetime.datetime.fromisoformat(last[0]['timestamp']).replace(tzinfo=MST)
                               ).total_seconds() / 60
                    return elapsed >= GLOBAL_COOLDOWN_MINUTES
                except (ValueError, TypeError):
                    pass
        return True

    def check_rate_limit(self) -> bool:
        """Check daily message count against MAX_DAILY_NUDGES."""
        metrics = self.db.query("SELECT nudges_sent FROM daily_metrics WHERE date = date('now')")
        if not metrics:
            return True
        return metrics[0]['nudges_sent'] < MAX_DAILY_NUDGES

    @staticmethod
    def check_quiet_hours() -> bool:
        """Return True if currently in quiet hours (10PM-6AM MST)."""
        hour = datetime.datetime.now(MST).hour
        return hour >= QUIET_START or hour < QUIET_END

    def _is_duplicate_message(self, message: str) -> bool:
        """Check if message is too similar to recent nudges (last 6 hours)."""
        recent = self.db.query(
            "SELECT message FROM nudge_log "
            "WHERE timestamp > datetime('now', '-6 hours') AND message IS NOT NULL"
        )
        msg_words = set(message.lower().split())
        for row in recent:
            if not row['message']:
                continue
            # Exact match
            if row['message'] == message:
                return True
            # Jaccard similarity
            row_words = set(row['message'].lower().split())
            if msg_words and row_words:
                intersection = msg_words & row_words
                union = msg_words | row_words
                if len(intersection) / len(union) > 0.7:
                    return True
        return False


# ============================================================
# CLASS: InitiativeEngine
# ============================================================

APPROVED_AUTO_CATEGORIES = ['monitoring']

COST_PER_1K_INPUT = {"anthropic/claude-sonnet-4-6": 0.003, "anthropic/claude-opus-4-6": 0.015}
COST_PER_1K_OUTPUT = {"anthropic/claude-sonnet-4-6": 0.015, "anthropic/claude-opus-4-6": 0.075}
DEFAULT_INITIATIVE_CAP = 10.0  # $10


class InitiativeEngine:
    """Atlas's self-directed autonomous work engine with guardrails."""

    def __init__(self, db: Database, gateway: GatewayClient, memory: MemoryEngine):
        self.db = db
        self.gateway = gateway
        self.memory = memory

    # ── Create ────────────────────────────────────────────────

    def create_initiative(self, title: str, description: str,
                          category: str = "research", trigger: str = "auto",
                          done_condition: str = None,
                          cost_cap_usd: float = DEFAULT_INITIATIVE_CAP) -> int:
        """Create initiative with guardrails (Amendment 5).

        trigger: 'user_request', 'user_approved', 'auto' (monitoring only), 'pre_approved'
        done_condition: required — reject without one.
        """
        # Auto-create only allowed for monitoring category
        if trigger == 'auto' and category not in APPROVED_AUTO_CATEGORIES:
            logging.info(f"Initiative '{title}' blocked: auto-create only for monitoring. Queuing for approval.")
            self.db.execute(
                "INSERT INTO atlas_initiatives (title, description, category, status, notes) "
                "VALUES (?, ?, ?, 'waiting_approval', ?)",
                (title, description, category, f"trigger={trigger}")
            )
            return -1

        # Require done_condition
        if not done_condition:
            logging.warning(f"Initiative '{title}' has no done condition. Rejecting.")
            return -1

        # Max 10 active initiatives
        active_count = self.db.query(
            "SELECT COUNT(*) as c FROM atlas_initiatives WHERE status='active'"
        )[0]['c']
        if active_count >= 10:
            logging.warning(f"Initiative cap reached ({active_count}). Skipping: {title}")
            return -1

        init_id = self.db.execute(
            "INSERT INTO atlas_initiatives (title, description, category, cost_cap_usd, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, category, cost_cap_usd,
             f"done_condition: {done_condition}\ntrigger: {trigger}")
        )

        self.memory.write_episodic(f"Created initiative: {title}", 'initiative')
        logging.info(f"New initiative #{init_id}: {title}")
        return init_id

    # ── Select ────────────────────────────────────────────────

    def select_initiative(self, context: Dict = None) -> Optional[Dict]:
        """Pick best initiative to work on. Score by priority + staleness + remaining budget.
        Max 5 steps/day (Amendment 5)."""
        metrics = self.db.query(
            "SELECT initiatives_worked FROM daily_metrics WHERE date = date('now')"
        )
        if metrics and metrics[0]['initiatives_worked'] >= 5:
            logging.info("Daily initiative budget exhausted (5/5)")
            return None

        initiatives = self.db.query(
            "SELECT * FROM atlas_initiatives WHERE status = 'active' "
            "AND time_invested_seconds < max_time_seconds "
            "ORDER BY priority ASC, last_worked ASC NULLS FIRST"
        )
        if not initiatives:
            return None

        best = None
        best_score = -1
        now = datetime.datetime.now(MST)

        for init in initiatives:
            # Cost cap check
            if init['estimated_cost_usd'] >= init['cost_cap_usd']:
                continue

            score = (6 - init['priority'])  # Priority score (1→5, 5→1)

            # Staleness bonus
            if init['last_worked']:
                try:
                    last = datetime.datetime.fromisoformat(init['last_worked'])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=MST)
                    hours_stale = (now - last).total_seconds() / 3600
                    score += min(hours_stale / 24, 3)
                except (ValueError, TypeError):
                    score += 3
            else:
                score += 3  # Never worked = maximum staleness

            # Budget remaining bonus
            remaining_ratio = 1 - (init['time_invested_seconds'] / max(init['max_time_seconds'], 1))
            score += remaining_ratio * 2

            if score > best_score:
                best_score = score
                best = init

        return best

    # ── Execute Step ──────────────────────────────────────────

    def execute_initiative_step(self, initiative: Dict,
                                time_budget_seconds: int = 90) -> Dict:
        """Execute one time-boxed step. Uses Sonnet unless deep_work=True (Amendment 9)."""
        # Cost cap check
        if initiative['estimated_cost_usd'] >= initiative['cost_cap_usd']:
            logging.info(
                f"Initiative #{initiative['id']} hit cost cap "
                f"(${initiative['estimated_cost_usd']:.2f}/${initiative['cost_cap_usd']:.2f})"
            )
            self.db.execute(
                "UPDATE atlas_initiatives SET status='paused', "
                "notes=COALESCE(notes,'')||'\n[Paused: cost cap reached]' WHERE id=?",
                (initiative['id'],)
            )
            return {"success": False, "cost_capped": True, "surface_ready": False, "result": {}}

        start = time.time()
        max_tokens = min(1500, max(500, time_budget_seconds * 15))

        # Force Sonnet unless deep_work=True in notes (Amendment 9)
        model = DEFAULT_MODEL
        if 'deep_work=True' in (initiative['notes'] or ''):
            # Check Opus budget before using
            if self._check_opus_budget():
                model = DEEP_MODEL
                self._record_opus_call()
            else:
                logging.info("Opus budget exhausted, falling back to Sonnet for initiative step")

        prompt = f"""You are Atlas, working on a self-directed initiative.

## Initiative: {initiative['title']}
{initiative['description']}

## Current findings:
{initiative['findings'] or 'None yet'}

## Next step to execute:
{initiative['next_step'] or 'Begin initial research/exploration'}

## Notes:
{initiative['notes'] or 'None'}

Execute the next step. You have {time_budget_seconds} seconds.

Return JSON:
{{
    "findings": "what you found/produced this step (append to existing)",
    "next_step": "what to do next iteration",
    "surface_ready": false,
    "summary": "one-line summary of what you did"
}}"""

        response = self.gateway.chat(
            system_prompt="You are Atlas's initiative engine. Execute research/drafting/monitoring steps. Return ONLY valid JSON.",
            user_message=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=0.7
        )

        elapsed = time.time() - start

        if not response:
            return {"success": False, "surface_ready": False, "result": {}, "error": "gateway_failure"}

        try:
            cleaned = response.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            result = {"findings": response[:500], "next_step": initiative['next_step'],
                      "surface_ready": False, "summary": "Partial result"}

        # Empty step detection (Amendment 5)
        findings_delta = result.get('findings', '').strip()
        if len(findings_delta) < 20:
            empty_count = self._get_empty_step_count(initiative['id'])
            self.db.execute(
                "INSERT INTO memory_events (layer, event_type, content, source, related_item_id) "
                "VALUES ('procedural', 'write', 'empty_initiative_step', 'initiative', ?)",
                (str(initiative['id']),)
            )
            if empty_count >= 2:
                logging.info(f"Initiative #{initiative['id']} paused: 2 consecutive empty steps")
                self.db.execute(
                    "UPDATE atlas_initiatives SET status='paused', "
                    "notes=COALESCE(notes,'')||'\n[Auto-paused: 2 empty steps]' WHERE id=?",
                    (initiative['id'],)
                )
                return {"success": False, "paused": True, "surface_ready": False, "result": result}

        # Update initiative record
        now_str = datetime.datetime.now(MST).strftime('%Y-%m-%d %H:%M')
        new_findings = (initiative['findings'] or '') + f"\n\n### [{now_str}]\n{result.get('findings', '')}"
        new_time = initiative['time_invested_seconds'] + int(elapsed)

        self.db.execute(
            "UPDATE atlas_initiatives SET findings=?, next_step=?, last_worked=datetime('now'), "
            "time_invested_seconds=?, updated_at=datetime('now') WHERE id=?",
            (new_findings, result.get('next_step', ''), new_time, initiative['id'])
        )

        # Cost estimate update
        estimated_tokens = len(response) // 4 if response else 0
        step_cost = self._estimate_step_cost(model, 1000, estimated_tokens)
        self.db.execute(
            "UPDATE atlas_initiatives SET estimated_cost_usd = estimated_cost_usd + ? WHERE id=?",
            (step_cost, initiative['id'])
        )

        # Update daily metrics
        self.db.execute(
            "INSERT INTO daily_metrics (date, initiatives_worked) VALUES (date('now'), 1) "
            "ON CONFLICT(date) DO UPDATE SET initiatives_worked = initiatives_worked + 1"
        )

        # Log to episodic
        self.memory.write_episodic(
            f"Initiative '{initiative['title']}': {result.get('summary', 'step completed')}",
            'initiative'
        )

        logging.info(
            f"Initiative #{initiative['id']} step done in {elapsed:.1f}s "
            f"(model={model.split('/')[-1]}, cost~${step_cost:.4f})"
        )
        return {"success": True, "surface_ready": result.get('surface_ready', False), "result": result}

    # ── Surface Findings ──────────────────────────────────────

    def surface_findings(self, initiative: Dict) -> Optional[str]:
        """Format findings for {user_name} via Sonnet, mark as completed."""
        if not initiative.get('findings'):
            return None

        prompt = f"""Summarize these research findings for {user_name} in casual, direct language.
Keep it under 200 words. Lead with the key insight. No preamble.

Initiative: {initiative['title']}
Description: {initiative['description']}

Findings:
{initiative['findings']}

Write the message {user_name} will see on Telegram."""

        message = self.gateway.chat(
            system_prompt="You are Atlas. Write casually for {user_name}. No AI-isms. Lead with the answer.",
            user_message=prompt,
            max_tokens=400,
            temperature=0.7
        )

        if message:
            self.db.execute(
                "UPDATE atlas_initiatives SET status='completed', updated_at=datetime('now') WHERE id=?",
                (initiative['id'],)
            )
            logging.info(f"Initiative #{initiative['id']} findings surfaced and marked completed")
        return message

    # ── Helpers ────────────────────────────────────────────────

    def _get_empty_step_count(self, initiative_id: int) -> int:
        """Count consecutive empty steps for an initiative."""
        rows = self.db.query(
            "SELECT COUNT(*) as c FROM memory_events "
            "WHERE related_item_id=? AND content='empty_initiative_step' "
            "AND timestamp > datetime('now', '-48 hours')",
            (str(initiative_id),)
        )
        return rows[0]['c'] if rows else 0

    @staticmethod
    def _estimate_step_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        input_cost = (input_tokens / 1000) * COST_PER_1K_INPUT.get(model, 0.003)
        output_cost = (output_tokens / 1000) * COST_PER_1K_OUTPUT.get(model, 0.015)
        return input_cost + output_cost

    def _check_opus_budget(self) -> bool:
        """Return True if Opus call is allowed (max 10/day, Amendment 9)."""
        count_str = self.db.get_state('daily_opus_calls')
        count = int(count_str) if count_str else 0
        if count >= 10:
            logging.warning(f"Daily Opus cap reached ({count}/10). Forcing Sonnet.")
            return False
        return True

    def _record_opus_call(self):
        count_str = self.db.get_state('daily_opus_calls')
        count = int(count_str) if count_str else 0
        self.db.set_state('daily_opus_calls', str(count + 1))
        if count + 1 == 8:
            logging.warning("Opus daily budget at 80% (8/10 calls)")


# ============================================================
# CLASS: MorningBriefing
# ============================================================

BRIEFING_SYSTEM_PROMPT = """You are Atlas composing a morning briefing for {user_name}.

Rules:
- Write like {user_name} talks: casual, emoji, direct. Gen-z energy.
- Lead with the most important thing
- Be casual, use emoji sparingly but naturally
- If there's a birthday/anniversary, mention it with personality
- For work meetings, include the time and a one-word vibe ("boring", "important", "skip if you can")
- For overdue tasks, be direct but not nagging — they know
- Keep it under 300 words
- If it's a reduced-interruption day, acknowledge that and adjust expectations
- Don't include items that can clearly wait
- End with energy — not "have a great day!" but something real

Example tone:
"Hey {user_name} — you got a 2pm meeting, your SSO cert expires in 8 days,
and you've actually got time for that project if you stop doomscrolling. Weather's
73° and clear — solid gym weather after 5."

NEVER start with "Good morning!" or "Here's your briefing for today." or any variant of those.
"""

BRIEFING_USER_TEMPLATE = """## Today: {day_name}, {date}
Day type: {day_type}

## Calendar (next 12h)
{calendar_formatted}

## Due Today
{due_today_formatted}

## Overdue
{overdue_formatted}

## Blocked Items
{blocked_formatted}

## Weather
{weather}

## Yesterday's Stats
{yesterday_stats}

## Initiative Findings Ready
{initiative_findings}

## Notes
{extra_notes}

Write the briefing now. Keep it tight."""


class MorningBriefing:
    """Composes and sends {user_name}'s morning briefing with fatigue filtering."""

    def __init__(self, db: Database, gateway: GatewayClient, task_router: TaskRouter):
        self.db = db
        self.gateway = gateway
        self.task_router = task_router

    # ── Trigger ───────────────────────────────────────────────

    def should_fire(self) -> bool:
        """Check all trigger conditions for morning briefing."""
        now = datetime.datetime.now(MST)

        # Time window: 7:00-8:30 AM MST
        if not (7 <= now.hour <= 8 and (now.hour < 8 or now.minute <= 30)):
            return False

        # Already sent today
        state = self._load_state()
        if state.get('last_briefing_date') == now.strftime('%Y-%m-%d'):
            return False

        # {user_name} recently active — skip if messaged in last 30 min
        last_msg = self.db.get_state('last_message_from_user')
        if last_msg and last_msg != 'never':
            try:
                last_time = datetime.datetime.fromisoformat(last_msg)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=MST)
                if (now - last_time).total_seconds() < 1800:
                    logging.info("Morning briefing skipped: {user_name} active in last 30 min")
                    return False
            except (ValueError, TypeError):
                pass

        # Content check — at least 1 piece of content
        has_content = (
            self._has_due_items_today() or
            self._has_overdue_items() or
            self._has_calendar_events_soon(hours=4) or
            self._has_initiative_findings() or
            now.weekday() == 0  # Monday reset
        )

        # Weekend: skip unless content exists
        if now.weekday() in (5, 6) and not has_content:
            return False

        return has_content

    # ── Context Gathering ─────────────────────────────────────

    def gather_briefing_context(self) -> Dict:
        """Collect everything for briefing with fatigue filtering (Amendment 8)."""
        today = datetime.date.today()
        today_iso = today.isoformat()

        due_today = self.db.query(
            "SELECT * FROM items WHERE status IN ('pending','active') AND "
            "(due = ? OR assigned_date = ?) ORDER BY "
            "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END",
            (today_iso, today_iso)
        )

        overdue_raw = self.db.query(
            "SELECT * FROM items WHERE status IN ('pending','active') AND due < ? "
            "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END, due ASC",
            (today_iso,)
        )

        # Fatigue filtering on overdue (Amendment 8) — max 3 consecutive appearances
        filtered_overdue = []
        suppressed = []
        for item in overdue_raw:
            fatigue = self._get_briefing_fatigue(item['id'])
            if fatigue < 3:
                filtered_overdue.append(item)
            else:
                suppressed.append(item)

        blocked = self.db.query("SELECT * FROM items WHERE status='blocked'")

        ready_initiatives = self.db.query(
            "SELECT * FROM atlas_initiatives WHERE status='active' AND surface_when_ready=1 "
            "AND findings IS NOT NULL AND length(findings) > 100"
        )

        yesterday_metrics = self.db.query(
            "SELECT * FROM daily_metrics WHERE date = date('now', '-1 day')"
        )

        weather = self._get_weather()

        extra_notes = ""
        if suppressed:
            extra_notes += f"{len(suppressed)} overdue items suppressed from briefing (fatigue > 3 days). Will nudge separately.\n"
        if today.weekday() == 0:
            extra_notes += "Monday — weekly context reset.\n"

        return {
            'date': today_iso,
            'day_name': today.strftime('%A'),
            'day_type': self.task_router.get_day_type(),
            'due_today': due_today,
            'overdue': filtered_overdue[:2],  # Max 2 overdue in briefing
            'blocked': blocked,
            'calendar_events': self._get_calendar_events(today_iso),
            'ready_initiatives': ready_initiatives,
            'yesterday_metrics': yesterday_metrics,
            'weather': weather,
            'extra_notes': extra_notes,
            'suppressed_overdue_count': len(suppressed),
        }

    # ── Compose ───────────────────────────────────────────────

    def compose(self, context: Dict) -> Optional[str]:
        """Call Sonnet with BRIEFING_SYSTEM_PROMPT to compose the briefing."""
        # Format context for template
        def fmt_items(items):
            if not items:
                return "(none)"
            lines = []
            for it in items:
                line = f"- {it['title']} (pri={it.get('priority','?')})"
                if it.get('due'):
                    line += f" due={it['due']}"
                lines.append(line)
            return "\n".join(lines)

        def fmt_calendar(events):
            if not events:
                return "(no events)"
            return "\n".join(f"- {e}" for e in events)

        def fmt_yesterday(metrics):
            if not metrics:
                return "(no data)"
            m = metrics[0]
            return (f"Nudges: {m.get('nudges_sent', 0)}, "
                    f"Items completed: {m.get('items_completed', 0)}, "
                    f"Loop runs: {m.get('total_loop_runs', 0)}")

        def fmt_initiatives(inits):
            if not inits:
                return "(none)"
            return "\n".join(f"- {i['title']}: {(i.get('findings') or '')[:150]}" for i in inits)

        user_msg = BRIEFING_USER_TEMPLATE.format(
            day_name=context['day_name'],
            date=context['date'],
            day_type=context['day_type'],
            calendar_formatted=fmt_calendar(context['calendar_events']),
            due_today_formatted=fmt_items(context['due_today']),
            overdue_formatted=fmt_items(context['overdue']),
            blocked_formatted=fmt_items(context['blocked']),
            weather=context['weather'] or '(unavailable)',
            yesterday_stats=fmt_yesterday(context['yesterday_metrics']),
            initiative_findings=fmt_initiatives(context['ready_initiatives']),
            extra_notes=context['extra_notes'] or '(none)',
        )

        message = self.gateway.chat(
            system_prompt=BRIEFING_SYSTEM_PROMPT,
            user_message=user_msg,
            max_tokens=600,
            temperature=0.7
        )
        return message

    # ── Send ──────────────────────────────────────────────────

    def send(self, dry_run: bool = False) -> bool:
        """Orchestrator: should_fire → gather → compose → send."""
        if not self.should_fire():
            logging.info("Morning briefing: conditions not met, skipping")
            return False

        context = self.gather_briefing_context()
        message = self.compose(context)

        if not message:
            logging.error("Morning briefing compose failed")
            return False

        if dry_run:
            logging.info(f"DRY RUN [BRIEFING]: {message}")
        else:
            success = self.gateway.send_telegram(message)
            if not success:
                logging.error("Morning briefing Telegram send failed")
                return False

        # Log each included item for fatigue tracking (Amendment 8)
        today_iso = datetime.date.today().isoformat()
        for item in context.get('overdue', []) + context.get('due_today', []):
            self.db.execute(
                "INSERT INTO nudge_log (item_id, action, message, reasoning) "
                "VALUES (?, 'BRIEFING', ?, 'morning briefing inclusion')",
                (item['id'], f"Included in briefing for {today_iso}")
            )

        # Update briefing state
        self._save_state({'last_briefing_date': today_iso})

        # Update daily metrics
        self.db.execute(
            "INSERT INTO daily_metrics (date, nudges_sent) VALUES (date('now'), 1) "
            "ON CONFLICT(date) DO UPDATE SET nudges_sent = nudges_sent + 1"
        )

        logging.info(f"Morning briefing sent ({len(message)} chars)")
        return True

    # ── Helpers ────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if BRIEFING_STATE_PATH.exists():
            try:
                return json.loads(BRIEFING_STATE_PATH.read_text())
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_state(self, state: dict):
        existing = self._load_state()
        existing.update(state)
        BRIEFING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRIEFING_STATE_PATH.write_text(json.dumps(existing, indent=2))

    def _has_due_items_today(self) -> bool:
        today_iso = datetime.date.today().isoformat()
        rows = self.db.query(
            "SELECT COUNT(*) as c FROM items WHERE status IN ('pending','active') "
            "AND (due = ? OR assigned_date = ?)",
            (today_iso, today_iso)
        )
        return rows[0]['c'] > 0 if rows else False

    def _has_overdue_items(self) -> bool:
        rows = self.db.query(
            "SELECT COUNT(*) as c FROM items WHERE status IN ('pending','active') "
            "AND due < date('now')"
        )
        return rows[0]['c'] > 0 if rows else False

    def _has_calendar_events_soon(self, hours: int = 4) -> bool:
        today_iso = datetime.date.today().isoformat()
        events = self._get_calendar_events(today_iso)
        return len(events) > 0

    def _has_initiative_findings(self) -> bool:
        rows = self.db.query(
            "SELECT COUNT(*) as c FROM atlas_initiatives WHERE status='active' "
            "AND surface_when_ready=1 AND findings IS NOT NULL AND length(findings) > 100"
        )
        return rows[0]['c'] > 0 if rows else False

    def _get_briefing_fatigue(self, item_id: str) -> int:
        """Count consecutive briefing appearances for an item (Amendment 8)."""
        appearances = self.db.query(
            "SELECT COUNT(*) as c FROM nudge_log WHERE item_id=? AND action='BRIEFING' "
            "AND timestamp > date('now', '-7 days')",
            (item_id,)
        )
        return appearances[0]['c'] if appearances else 0

    def _get_calendar_events(self, date_str: str) -> List[str]:
        """Get calendar events for a date via gog CLI."""
        try:
            result = subprocess.run(
                ["gog", "calendar", "list", "--from", date_str, "--to", date_str, "--all", "--json"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ,
                     "GOG_KEYRING_PASSWORD": os.environ.get("GOG_KEYRING_PASSWORD", ""),
                     "GOG_ACCOUNT": os.environ.get("GOG_ACCOUNT", "")}
            )
            if result.returncode != 0:
                return []
            raw = json.loads(result.stdout) if result.stdout.strip() else []
            if isinstance(raw, dict):
                events = raw.get('events', []) or []
            elif isinstance(raw, list):
                events = raw
            else:
                return []
            formatted = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                summary = event.get('summary', 'Untitled')
                start_val = event.get('start', {})
                if isinstance(start_val, dict):
                    start_str = start_val.get('dateTime', start_val.get('date', ''))
                else:
                    start_str = str(start_val)
                formatted.append(f"{start_str} — {summary}")
            return formatted
        except Exception:
            return []

    @staticmethod
    def _get_weather() -> Optional[str]:
        """Get weather summary via wttr.in."""
        try:
            req = urllib.request.Request(
                "https://wttr.in/?format=%C+%t+%h+%w",
                headers={"User-Agent": "atlas-brain/2.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode('utf-8').strip()
        except Exception:
            return None


# ============================================================
# CLASS: NightlyDistillation (Phase 5)
# ============================================================

DISTILLATION_SYSTEM_PROMPT = """You are Atlas's memory distillation engine. Your job is to read through \
recent episodic memories (daily logs) and decide what deserves promotion to permanent memory.

You have two output targets:

## SEMANTIC (MEMORY.md) — Facts about {user_name}
Permanent facts, preferences, relationships, patterns. Things that would be true next month.
Examples: "{user_name}'s sibling's birthday is March 22", "{user_name} hates unclear requirements",
"Child started daycare Mon/Tue/Thu", "Partner handles dinner most nights"

Only add NEW facts not already in MEMORY.md. Never duplicate. Never delete existing entries
unless explicitly contradicted by new information (in which case output a CORRECT action).

## PROCEDURAL (PROCEDURE.md) — How to work with {user_name}
Behavioral patterns, communication strategies, timing insights, what works/doesn't work.
Examples: "Nudging about gym before 10 AM gets ignored", "{user_name} responds better to casual
reminders than formal ones", "Don't bring up projects on reduced-interruption days"

## Output Format
Return ONLY valid JSON:
{
    "semantic_additions": ["fact 1", "fact 2"],
    "semantic_corrections": [{"old": "wrong fact", "new": "correct fact"}],
    "procedural_updates": ["pattern 1", "pattern 2"],
    "procedural_removals": ["outdated pattern"],
    "learnings_to_promote": [1, 2],
    "summary": "One sentence summarizing what was distilled today"
}

If nothing qualifies for promotion, return empty arrays. Be selective — only promote
things that are genuinely persistent/useful. Err on the side of KEEPING more than less.
{user_name} said: "Forget nothing that matters."
"""

DISTILLATION_USER_TEMPLATE = """## Current MEMORY.md
{memory_md_content}

## Current PROCEDURE.md
{procedure_md_content}

## Episodic Memory (Last 3 Days)
{episodic_content}

## Unpromoted Learnings
{learnings_json}

## This Week's Metrics
{metrics_summary}

Now analyze and produce your distillation output."""


class NightlyDistillation:
    """Nightly distillation of episodic memory into semantic/procedural layers."""

    STAGING_DIR = MEMORY_DIR / "distillation-staging"

    def __init__(self, db: Database, gateway: GatewayClient, memory: MemoryEngine):
        self.db = db
        self.gateway = gateway
        self.memory = memory

    def run(self):
        logging.info("=== Nightly distillation start ===")

        # 1. Check if already run today
        last_run = self.db.get_state('last_nightly_distillation')
        today_iso = datetime.date.today().isoformat()
        if last_run == today_iso:
            logging.info("Nightly distillation already run today. Skipping.")
            return

        try:
            # 2. Gather episodic data — last 3 days
            episodic_content = self._gather_episodic(days=3)

            # 3. Read current MEMORY.md
            memory_md_content = MEMORY_MD.read_text() if MEMORY_MD.exists() else "# Memory\n(empty)"

            # 4. Read current PROCEDURE.md
            procedure_md_content = PROCEDURE_MD.read_text() if PROCEDURE_MD.exists() else "(empty)"

            # 5. Query unpromoted learnings
            learnings = self.db.query(
                "SELECT * FROM learnings WHERE promoted = 0 ORDER BY date DESC LIMIT 50"
            )
            learnings_json = json.dumps(
                [{"id": l["id"], "date": l["date"], "insight": l["insight"],
                  "metric_type": l.get("metric_type", ""), "source": l.get("source", "")}
                 for l in learnings],
                indent=2
            ) if learnings else "[]"

            # 6. Query daily_metrics for last 7 days
            metrics = self.db.query(
                "SELECT * FROM daily_metrics WHERE date >= date('now', '-7 days') ORDER BY date DESC"
            )
            metrics_summary = self._format_metrics(metrics)

            # 7. Call Sonnet
            user_message = DISTILLATION_USER_TEMPLATE.format(
                memory_md_content=memory_md_content[:3000],
                procedure_md_content=procedure_md_content[:2000],
                episodic_content=episodic_content[:4000],
                learnings_json=learnings_json[:2000],
                metrics_summary=metrics_summary[:1000],
            )

            raw_response = self.gateway.chat(
                system_prompt=DISTILLATION_SYSTEM_PROMPT,
                user_message=user_message,
                max_tokens=2000,
                temperature=0.2,
            )

            if not raw_response:
                logging.error("Distillation: Sonnet returned empty response")
                return

            # 8. Validate output
            result = self._validate_distillation_output(raw_response)
            if result is None:
                logging.error("Distillation output failed validation. Skipping tonight.")
                return

            # 9. Write to staging area with confidence scores
            staged_entries = self._write_staging(result, today_iso)

            # 10. Auto-promote high-confidence entries (>=0.8)
            promoted_semantic = 0
            for entry_obj in staged_entries:
                if entry_obj["confidence"] >= 0.8:
                    self.memory.promote_to_semantic(entry_obj["text"])
                    entry_obj["promoted"] = True
                    promoted_semantic += 1

            # Re-write staging file with promotion flags
            self._update_staging_file(today_iso, staged_entries)

            # 11. Promote aged staging entries (3+ days old)
            aged_promoted = self._promote_aged_staging(days=3)

            # 12. Apply semantic corrections
            corrections_applied = 0
            for correction in result.get("semantic_corrections", []):
                old = correction.get("old", "")
                new = correction.get("new", "")
                if old and new and MEMORY_MD.exists():
                    content = MEMORY_MD.read_text()
                    if old in content:
                        content = content.replace(old, new)
                        MEMORY_MD.write_text(content)
                        corrections_applied += 1
                        logging.info(f"Semantic correction: '{old[:50]}' → '{new[:50]}'")

            # 13. Apply procedural updates
            procedural_count = 0
            for pattern in result.get("procedural_updates", []):
                if pattern.strip():
                    self.memory.update_procedural(pattern)
                    procedural_count += 1

            # Handle procedural removals (log but don't auto-remove — too risky)
            for removal in result.get("procedural_removals", []):
                logging.info(f"Procedural removal suggested (manual review): {removal[:80]}")

            # 14. Mark learnings as promoted
            learning_ids = result.get("learnings_to_promote", [])
            if learning_ids:
                placeholders = ",".join("?" * len(learning_ids))
                self.db.execute(
                    f"UPDATE learnings SET promoted = 1 WHERE id IN ({placeholders})",
                    tuple(learning_ids)
                )
                logging.info(f"Marked {len(learning_ids)} learnings as promoted")

            # 15. Clean up episodic files older than 30 days
            self.memory.cleanup_episodic(retention_days=30)

            # 16. Log the distillation to memory_events
            summary = result.get("summary", "Nightly distillation completed")
            self.db.execute(
                "INSERT INTO memory_events (layer, event_type, content, source) VALUES (?, ?, ?, ?)",
                ('semantic', 'distillation', summary, 'nightly')
            )

            # 17. Update system_state
            self.db.set_state('last_nightly_distillation', today_iso)

            # 18. Write brief summary to episodic
            total_promoted = promoted_semantic + aged_promoted
            episodic_summary = (
                f"Nightly distillation: promoted {total_promoted} semantic, "
                f"{procedural_count} procedural entries. "
                f"{corrections_applied} corrections applied. {summary}"
            )
            self.memory.write_episodic(episodic_summary, 'nightly_distillation')

            logging.info(
                f"=== Nightly distillation complete: {total_promoted} semantic, "
                f"{procedural_count} procedural, {corrections_applied} corrections ==="
            )

        except Exception as e:
            logging.exception(f"Nightly distillation failed: {e}")

    def _gather_episodic(self, days: int = 3) -> str:
        """Read memory/YYYY-MM-DD.md files from last N days."""
        content_parts = []
        today = datetime.date.today()
        for i in range(days):
            d = today - datetime.timedelta(days=i)
            f = MEMORY_DIR / f"{d.isoformat()}.md"
            if f.exists():
                text = f.read_text()
                if text.strip():
                    content_parts.append(f"### {d.isoformat()}\n{text}")
        return "\n\n".join(content_parts) if content_parts else "(no episodic data)"

    def _format_metrics(self, metrics: List[dict]) -> str:
        """Format daily_metrics rows into a readable summary."""
        if not metrics:
            return "(no metrics data)"
        lines = []
        for m in metrics:
            lines.append(
                f"{m['date']}: nudges={m.get('nudges_sent', 0)}, "
                f"completed={m.get('items_completed', 0)}, "
                f"created={m.get('items_created', 0)}, "
                f"response_rate={m.get('response_rate', 0):.0%}, "
                f"loops={m.get('total_loop_runs', 0)}"
            )
        return "\n".join(lines)

    def _validate_distillation_output(self, raw: str) -> Optional[dict]:
        """Parse and validate distillation output. Returns dict or None on failure."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        # Validate and coerce structure
        if not isinstance(data.get('semantic_additions'), list):
            data['semantic_additions'] = []
        if not isinstance(data.get('semantic_corrections'), list):
            data['semantic_corrections'] = []
        if not isinstance(data.get('procedural_updates'), list):
            data['procedural_updates'] = []
        if not isinstance(data.get('procedural_removals'), list):
            data['procedural_removals'] = []
        if not isinstance(data.get('learnings_to_promote'), list):
            data['learnings_to_promote'] = []
        if not isinstance(data.get('summary'), str):
            data['summary'] = ''

        # Enforce max entries
        data['semantic_additions'] = data['semantic_additions'][:20]
        data['semantic_corrections'] = data['semantic_corrections'][:5]
        data['procedural_updates'] = data['procedural_updates'][:10]
        data['procedural_removals'] = data['procedural_removals'][:5]
        data['learnings_to_promote'] = data['learnings_to_promote'][:20]

        return data

    def _score_confidence(self, entry: str) -> float:
        """Score confidence of a distillation entry. 0.0-1.0 scale."""
        score = 0.5  # baseline

        # Proper nouns boost
        if re.search(r'\b[A-Z][a-z]+\b', entry):
            score += 0.1
        # Date/number boost
        if re.search(r'\b\d{4}-\d{2}-\d{2}\b|\b\d+\b', entry):
            score += 0.1
        # "{user_name} said" or direct quote boost
        if USER_NAME.lower() in entry.lower() and any(w in entry.lower() for w in ['said', 'mentioned', 'told', 'confirmed']):
            score += 0.15
        # Hedging penalty
        if any(w in entry.lower() for w in ['probably', 'might', 'seems like', 'possibly', 'appears to']):
            score -= 0.2

        return max(0.0, min(1.0, score))

    def _write_staging(self, result: dict, today_iso: str) -> list:
        """Write semantic additions to staging file with confidence scores. Returns entry list."""
        self.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        staging_file = self.STAGING_DIR / f"{today_iso}.json"

        entries = []
        for text in result.get('semantic_additions', []):
            if not text.strip():
                continue
            entries.append({
                "text": text,
                "confidence": self._score_confidence(text),
                "staged_date": today_iso,
                "promoted": False,
            })

        staging_data = {"date": today_iso, "entries": entries, "summary": result.get("summary", "")}
        staging_file.write_text(json.dumps(staging_data, indent=2))
        logging.info(f"Staging: wrote {len(entries)} entries to {staging_file.name}")
        return entries

    def _update_staging_file(self, today_iso: str, entries: list):
        """Update staging file with promotion flags."""
        staging_file = self.STAGING_DIR / f"{today_iso}.json"
        if staging_file.exists():
            data = json.loads(staging_file.read_text())
            data["entries"] = entries
            staging_file.write_text(json.dumps(data, indent=2))

    def _promote_aged_staging(self, days: int = 3) -> int:
        """Promote staged entries older than N days that haven't been promoted yet."""
        if not self.STAGING_DIR.exists():
            return 0

        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        promoted_count = 0

        for f in sorted(self.STAGING_DIR.glob("????-??-??.json")):
            try:
                file_date = datetime.date.fromisoformat(f.stem)
            except ValueError:
                continue

            if file_date > cutoff:
                continue  # too recent

            try:
                data = json.loads(f.read_text())
            except (json.JSONDecodeError, IOError):
                continue

            modified = False
            for entry in data.get("entries", []):
                if entry.get("promoted"):
                    continue
                # Auto-promote aged entries regardless of confidence
                self.memory.promote_to_semantic(entry["text"])
                entry["promoted"] = True
                promoted_count += 1
                modified = True

            if modified:
                f.write_text(json.dumps(data, indent=2))

        if promoted_count:
            logging.info(f"Aged staging: promoted {promoted_count} entries older than {days}d")
        return promoted_count


# ============================================================
# CLASS: WeeklyReview (Phase 5)
# ============================================================

WEEKLY_REVIEW_SYSTEM_PROMPT = """You are Atlas, {user_name}'s AI assistant. Write a casual weekly review \
message for {user_name}. Keep it under 400 words, actionable, and in {user_name}'s casual tone — like a friend \
giving a quick status update, not a formal report.

Structure:
1. What got done this week (celebrate wins)
2. What's still stuck/blocked
3. Active initiatives and their progress
4. Key learnings/patterns observed
5. Top 3 priorities for next week

Be direct, skip fluff. Use bullet points. If something is blocked, say why and suggest next steps.
Output the review message as plain text (Telegram markdown OK)."""

WEEKLY_REVIEW_USER_TEMPLATE = """## This Week's Metrics (last 7 days)
{metrics_summary}

## Active Items
{active_items}

## Blocked Items
{blocked_items}

## Active Initiatives
{initiatives}

## This Week's Learnings
{learnings}

{monthly_section}

Compose the weekly review now."""


class WeeklyReview:
    """Weekly review and planning session."""

    def __init__(self, db: Database, gateway: GatewayClient, memory: MemoryEngine):
        self.db = db
        self.gateway = gateway
        self.memory = memory

    def run(self):
        logging.info("=== Weekly review start ===")

        try:
            # 1. Gather last 7 days of daily_metrics
            metrics = self.db.query(
                "SELECT * FROM daily_metrics WHERE date >= date('now', '-7 days') ORDER BY date DESC"
            )

            # 2. Gather active + blocked items
            active_items = self.db.query(
                "SELECT id, title, status, priority, category, due, assigned_date "
                "FROM items WHERE status IN ('pending', 'active') ORDER BY priority LIMIT 30"
            )
            blocked_items = self.db.query(
                "SELECT id, title, block_reason, updated_at "
                "FROM items WHERE status = 'blocked' ORDER BY updated_at DESC LIMIT 15"
            )

            # 3. Gather active initiatives
            initiatives = self.db.query(
                "SELECT id, title, status, next_step, findings, time_invested_seconds "
                "FROM atlas_initiatives WHERE status = 'active' ORDER BY priority LIMIT 10"
            )

            # 4. Query learnings from last 7 days
            learnings = self.db.query(
                "SELECT * FROM learnings WHERE date >= date('now', '-7 days') ORDER BY date DESC LIMIT 30"
            )

            # 5. Calculate summary stats
            total_nudges = sum(m.get('nudges_sent', 0) for m in metrics)
            total_completed = sum(m.get('items_completed', 0) for m in metrics)
            total_created = sum(m.get('items_created', 0) for m in metrics)
            avg_response_rate = (
                sum(m.get('response_rate', 0) for m in metrics) / len(metrics)
                if metrics else 0
            )
            best_hours = [m.get('best_response_hour') for m in metrics if m.get('best_response_hour') is not None]
            worst_hours = [m.get('worst_response_hour') for m in metrics if m.get('worst_response_hour') is not None]

            metrics_summary = (
                f"Nudges sent: {total_nudges}\n"
                f"Response rate: {avg_response_rate:.0%}\n"
                f"Items completed: {total_completed}\n"
                f"Items created: {total_created}\n"
                f"Best response hours: {best_hours}\n"
                f"Worst response hours: {worst_hours}"
            )

            # Format items
            active_str = "\n".join(
                f"- [{i['id']}] {i['title']} (P{i.get('priority', '?')}, {i.get('status', '?')}, due: {i.get('due', 'none')})"
                for i in active_items
            ) or "(none)"

            blocked_str = "\n".join(
                f"- [{i['id']}] {i['title']} — blocked: {i.get('block_reason', '?')}"
                for i in blocked_items
            ) or "(none)"

            init_str = "\n".join(
                f"- [{i['id']}] {i['title']} — next: {i.get('next_step', '?')} "
                f"({i.get('time_invested_seconds', 0) // 60}min invested)"
                for i in initiatives
            ) or "(none)"

            learnings_str = "\n".join(
                f"- [{l['date']}] {l['insight']}"
                for l in learnings
            ) or "(none)"

            # 9. Monthly: every 4th week, include low-confidence staged entries
            monthly_section = ""
            week_num = datetime.date.today().isocalendar()[1]
            if week_num % 4 == 0:
                monthly_section = self._get_monthly_verification_section()

            # 6. Call Sonnet to compose the review
            user_message = WEEKLY_REVIEW_USER_TEMPLATE.format(
                metrics_summary=metrics_summary,
                active_items=active_str[:2000],
                blocked_items=blocked_str[:1000],
                initiatives=init_str[:1000],
                learnings=learnings_str[:1500],
                monthly_section=monthly_section,
            )

            review_text = self.gateway.chat(
                system_prompt=WEEKLY_REVIEW_SYSTEM_PROMPT,
                user_message=user_message,
                max_tokens=1500,
                temperature=0.4,
            )

            if not review_text:
                logging.error("Weekly review: Sonnet returned empty response")
                return

            # 7. Send via Telegram
            self.gateway.send_telegram(review_text)
            logging.info("Weekly review sent to Telegram")

            # 8. Update system_state
            self.db.set_state('last_weekly_review', today_iso := datetime.date.today().isoformat())

            # Log to episodic
            self.memory.write_episodic(
                f"Weekly review sent: {total_completed} completed, {len(blocked_items)} blocked, "
                f"{len(initiatives)} active initiatives",
                'weekly_review'
            )

            logging.info("=== Weekly review complete ===")

        except Exception as e:
            logging.exception(f"Weekly review failed: {e}")

    def _get_monthly_verification_section(self) -> str:
        """Every 4th week: include 5 lowest-confidence staged entries for {user_name} to verify."""
        staging_dir = MEMORY_DIR / "distillation-staging"
        if not staging_dir.exists():
            return ""

        low_conf_entries = []
        for f in sorted(staging_dir.glob("????-??-??.json")):
            try:
                data = json.loads(f.read_text())
            except (json.JSONDecodeError, IOError):
                continue
            for entry in data.get("entries", []):
                if not entry.get("promoted") and entry.get("confidence", 1.0) < 0.8:
                    low_conf_entries.append(entry)

        if not low_conf_entries:
            return ""

        # Sort by confidence ascending, take 5 lowest
        low_conf_entries.sort(key=lambda e: e.get("confidence", 0))
        top5 = low_conf_entries[:5]

        lines = ["## Monthly Verification (please confirm or correct these)"]
        for e in top5:
            lines.append(f"- [{e.get('confidence', 0):.2f}] {e['text']}")

        return "\n".join(lines)


# ============================================================
# TRIGGER PROCESSING
# ============================================================

def process_triggers(db: Database) -> int:
    """Process unprocessed triggers from v_unprocessed_triggers. Returns count processed."""
    triggers = db.query("SELECT * FROM v_unprocessed_triggers")
    if not triggers:
        return 0

    processed = 0
    for trigger in triggers:
        t_id = trigger['id']
        item_id = trigger.get('item_id')
        response_type = trigger['response_type']
        now_iso = datetime.datetime.now(MST).isoformat()

        if response_type == 'done' and item_id:
            db.execute("UPDATE items SET status='done', updated_at=? WHERE id=?", (now_iso, item_id))
            logging.info(f"Trigger #{t_id}: item {item_id} → done")

        elif response_type == 'snoozed' and item_id:
            new_schedule = trigger.get('inferred_schedule')
            if new_schedule:
                db.execute(
                    "UPDATE items SET scheduled_at=?, status='active', updated_at=? WHERE id=?",
                    (new_schedule, now_iso, item_id)
                )
            logging.info(f"Trigger #{t_id}: item {item_id} → snoozed until {new_schedule}")

        elif response_type == 'blocked' and item_id:
            block_reason = trigger.get('user_said', '')[:200]
            db.execute(
                "UPDATE items SET status='blocked', block_reason=?, updated_at=? WHERE id=?",
                (block_reason, now_iso, item_id)
            )
            logging.info(f"Trigger #{t_id}: item {item_id} → blocked")

        elif response_type == 'acknowledged' and item_id:
            db.execute(
                "UPDATE items SET last_response='acknowledged', updated_at=? WHERE id=?",
                (now_iso, item_id)
            )
            logging.info(f"Trigger #{t_id}: item {item_id} → acknowledged")

        elif response_type == 'cancelled' and item_id:
            db.execute(
                "UPDATE items SET status='cancelled', updated_at=? WHERE id=?",
                (now_iso, item_id)
            )
            logging.info(f"Trigger #{t_id}: item {item_id} → cancelled")

        elif response_type == 'hard_no' and item_id:
            db.execute(
                "UPDATE items SET status='cancelled', updated_at=? WHERE id=?",
                (now_iso, item_id)
            )
            # Log learning
            user_said = trigger.get('user_said', '')[:300]
            db.execute(
                "INSERT INTO learnings (insight, metric_type, metric_value, source) "
                "VALUES (?, 'task_routing', ?, 'correction')",
                (f"Hard no on item {item_id}: {user_said[:100]}",
                 json.dumps({'item_id': item_id, 'user_said': user_said}))
            )
            logging.info(f"Trigger #{t_id}: item {item_id} → hard_no (cancelled + learning)")

        elif response_type == 'new_task':
            # New tasks are created by PerceptionLayer already; just mark processed
            logging.info(f"Trigger #{t_id}: new_task signal (handled by perception)")

        else:
            logging.debug(f"Trigger #{t_id}: unhandled type '{response_type}'")

        # Mark processed
        db.execute("UPDATE triggers SET processed=1 WHERE id=?", (t_id,))
        processed += 1

    logging.info(f"Processed {processed} trigger(s)")
    return processed


# ============================================================
# CLI COMMANDS
# ============================================================

def cmd_list(db: Database):
    """Print all active items."""
    items = db.query(
        "SELECT * FROM v_active_items"
    )
    if not items:
        print("No active items.")
        return

    for item in items:
        status_icon = {"pending": "⏳", "in_progress": "🔄", "blocked": "🚧", "active": "⏳"}.get(item["status"], "?")
        line = f"{status_icon} [{item['priority']}] {item['id']}: {item['title']}"
        if item.get("due"):
            line += f" (due: {item['due']})"
        if item.get("assigned_date"):
            line += f" [assigned: {item['assigned_date']}]"
        if item["status"] == "blocked":
            line += f" [blocked: {item.get('block_reason', '?')}]"
        line += f" | nudges: {item['nudge_count']}"
        print(line)


def cmd_status(db: Database):
    """Print current system state."""
    now = datetime.datetime.now(MST)
    print(f"=== Atlas Brain v2 Status ===")
    print(f"Version: {db.get_state('brain_version', 'unknown')}")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"DB: {db.db_path}")
    print()

    # Table counts
    for table in ['items', 'nudge_log', 'learnings', 'triggers', 'atlas_initiatives',
                   'memory_events', 'perception_log', 'mechanical_reminders']:
        count = db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count}")

    # Items by status
    print("\nItems by status:")
    rows = db.query("SELECT status, COUNT(*) as n FROM items GROUP BY status ORDER BY n DESC")
    for r in rows:
        print(f"  {r['status']}: {r['n']}")

    # System state
    print(f"\nLast run: {db.get_state('last_run_timestamp', 'never')}")
    print(f"Last message from {USER_NAME}: {db.get_state('last_message_from_user', 'never')}")
    print(f"Daily Opus calls: {db.get_state('daily_opus_calls', '0')}")
    print(f"Brain version: {db.get_state('brain_version', 'unknown')}")


def cmd_done(db: Database, item_id: str):
    """Mark an item as done."""
    # Try exact match first, then prefix
    row = db.conn.execute("SELECT id, title FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        rows = db.conn.execute("SELECT id, title FROM items WHERE id LIKE ?", (f"%{item_id}%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            print(f"Ambiguous match for '{item_id}':")
            for r in rows:
                print(f"  {r['id']}: {r['title']}")
            return
        else:
            print(f"Item not found: {item_id}")
            return

    now_iso = datetime.datetime.now(MST).isoformat()
    db.execute("UPDATE items SET status='done', updated_at=? WHERE id=?", (now_iso, row['id']))
    print(f"Done: {row['id']} — {row['title']}")


def cmd_add(db: Database, title: str, priority: str = "medium", due: str = None,
            category: str = None, energy: str = "medium", notes: str = None):
    """Add a new task."""
    # Generate slug ID
    slug = re.sub(r'[^a-z0-9\s-]', '', title.lower().strip())
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)[:60].strip('-')

    # Check for duplicate
    existing = db.query("SELECT id FROM items WHERE id = ?", (slug,))
    if existing:
        for i in range(2, 100):
            candidate = f"{slug}-{i}"
            if not db.query("SELECT id FROM items WHERE id = ?", (candidate,)):
                slug = candidate
                break

    now_iso = datetime.datetime.now(MST).isoformat()
    db.execute(
        "INSERT INTO items (id, title, added, due, priority, energy, category, status, notes, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 'manual')",
        (slug, title, now_iso, due, priority, energy, category, notes)
    )
    print(f"Added: {slug} — {title} [{priority}]")


def cmd_watchdog(db: Database):
    """Check if brain is running. Alert if stuck."""
    import urllib.request
    import urllib.error

    last_run = db.get_state('last_run_timestamp')
    if not last_run:
        print("No last run recorded — brain may not have started yet")
        return

    try:
        last_dt = datetime.datetime.fromisoformat(last_run)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=MST)
        now = datetime.datetime.now(MST)
        age_minutes = (now - last_dt).total_seconds() / 60

        if age_minutes > 40:
            message = f"⚠️ Atlas brain appears stuck. Last successful run: {last_run} ({int(age_minutes)} min ago). Check .state/atlas-brain.log"
            # Send alert via raw Telegram API
            payload = json.dumps({
                "chat_id": CHAT_ID,
                "message_thread_id": THREAD_MAIN,
                "text": message
            }).encode('utf-8')
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(req, timeout=15)
                print(f"ALERT SENT: brain stuck for {int(age_minutes)} min")
            except Exception as e:
                print(f"Failed to send alert: {e}")
        else:
            print(f"Brain OK — last run {int(age_minutes)} min ago")
    except Exception as e:
        print(f"Watchdog error: {e}")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Atlas Brain v2")
    parser.add_argument("--morning", action="store_true")
    parser.add_argument("--nightly", action="store_true")
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--perception-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--add", type=str)
    parser.add_argument("--done", type=str)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--trigger", action="store_true")
    parser.add_argument("--init", action="store_true", help="Initialize a fresh database (new install)")
    parser.add_argument("--migrate", action="store_true", help="Migrate from cognitive-loop.db (upgrades only)")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--priority", type=str, default="medium")
    parser.add_argument("--due", type=str, default=None)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--energy", type=str, default="medium")
    parser.add_argument("--notes", type=str, default=None)
    parser.add_argument("--response", type=str)
    parser.add_argument("--item-id", type=str)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Commands that don't need the lock
    if args.init:
        db = Database()
        db.connect()
        db.ensure_schema()
        db.set_state('brain_version', BRAIN_VERSION)
        db.close()
        print(f"✅ Database initialized at {DB_PATH}")
        print("Run --status to verify.")
        return

    if args.migrate:
        db = Database()
        db.connect()
        success = db.migrate_from_v1()
        if success:
            print("✅ Migration complete. Run --verify to check.")
        else:
            print("❌ Migration failed. Check logs.")
        db.close()
        return

    if args.watchdog:
        db = Database()
        db.connect()
        cmd_watchdog(db)
        db.close()
        return

    # Commands that need DB but not lock
    if args.verify or args.list or args.status or args.done or args.add:
        db = Database()
        db.connect()
        if args.verify:
            success = db.verify_migration()
            print("✅ Verification PASSED" if success else "❌ Verification FAILED")
        elif args.list:
            cmd_list(db)
        elif args.status:
            cmd_status(db)
        elif args.done:
            cmd_done(db, args.done)
        elif args.add:
            cmd_add(db, args.add, args.priority, args.due, args.category, args.energy, args.notes)
        db.close()
        return

    # Main loop and scheduled tasks need the lock
    lock_fd = acquire_lock()
    if lock_fd is None:
        logging.info("Another instance running. Exiting.")
        sys.exit(0)

    try:
        db = Database()
        db.connect()
        db.ensure_schema()

        # Record run timestamp
        db.set_state('last_run_timestamp', datetime.datetime.now(MST).isoformat())

        gateway = GatewayClient()
        memory = MemoryEngine(db, gateway)
        perception = PerceptionLayer(db, gateway, memory)
        task_router = TaskRouter(db, gateway)
        action_layer = ActionLayer(db, gateway, memory)
        initiative_engine = InitiativeEngine(db, gateway, memory)
        morning_briefing = MorningBriefing(db, gateway, task_router)
        nightly = NightlyDistillation(db, gateway, memory)
        weekly = WeeklyReview(db, gateway, memory)

        # Reset daily Opus counter if new day (Amendment 9)
        if db.get_state('opus_counter_reset_date') != datetime.date.today().isoformat():
            db.set_state('daily_opus_calls', '0')
            db.set_state('opus_counter_reset_date', datetime.date.today().isoformat())

        if args.dry_run:
            action_layer.dry_run = True

        if args.perception_only:
            signals = perception.process_new_messages()
            logging.info(f"Perception-only: {signals} signal(s) detected")
        elif args.morning:
            morning_briefing.send(dry_run=args.dry_run)
        elif args.nightly:
            nightly.run()
        elif args.weekly:
            weekly.run()
        else:
            # Standard cron loop
            loop_start = time.time()
            logging.info(f"=== Atlas Brain v{BRAIN_VERSION} loop start ===")
            items_count = db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            active_count = db.conn.execute("SELECT COUNT(*) FROM items WHERE status IN ('pending','active','blocked')").fetchone()[0]
            logging.info(f"Items: {items_count} total, {active_count} active")

            # Step 1: Perception — scan for new messages and extract signals
            signals = perception.process_new_messages()
            if signals:
                logging.info(f"Perception detected {signals} signal(s)")

            # Step 2: Process unprocessed triggers
            trigger_count = process_triggers(db)

            # Step 3: Auto-assign dates for dateless active items
            dateless = db.query(
                "SELECT * FROM items WHERE status IN ('pending', 'active') "
                "AND assigned_date IS NULL LIMIT 20"
            )
            for item in dateless:
                assigned = task_router.auto_assign_date(item)
                db.execute(
                    "UPDATE items SET assigned_date=?, updated_at=datetime('now') WHERE id=?",
                    (assigned, item['id'])
                )
                logging.info(f"Auto-assigned date {assigned} to item {item['id']}")

            # Step 4: Check mechanical reminders
            day_type = task_router.get_day_type()
            reminder_context = {'day_type': day_type, 'in_meeting': False}
            mechanical_msgs = task_router.check_pending_reminders(reminder_context)
            for msg in mechanical_msgs:
                if args.dry_run:
                    logging.info(f"DRY RUN [MECHANICAL]: {msg}")
                else:
                    gateway.send_telegram(msg)
                    db.execute(
                        "INSERT INTO nudge_log (action, message, reasoning) "
                        "VALUES ('MECHANICAL', ?, 'scheduled reminder')",
                        (msg,)
                    )
                logging.info(f"Mechanical reminder sent: {msg}")

            # Step 5: ActionLayer — gather context → decide → validate → execute
            context = action_layer.gather_context(task_router)
            decision = action_layer.decide(context)
            logging.info(f"Action decision: {decision.get('action', '?')} — {decision.get('reasoning', '')[:120]}")

            if action_layer.validate(decision, context):
                action_layer.execute(decision, dry_run=args.dry_run)
            else:
                logging.info(f"Action {decision.get('action')} failed validation — SILENCE")

            # Step 6: Initiative step — if time permits (<90s elapsed) and not parenting afternoon
            elapsed_so_far = time.time() - loop_start
            now_hour = datetime.datetime.now(MST).hour
            is_parenting_afternoon = (
                day_type == 'parenting_day' and now_hour >= 12
            )
            if elapsed_so_far < 90 and not is_parenting_afternoon:
                initiative = initiative_engine.select_initiative()
                if initiative:
                    remaining_budget = max(30, int(90 - elapsed_so_far))
                    step_result = initiative_engine.execute_initiative_step(
                        initiative, time_budget_seconds=remaining_budget
                    )
                    if step_result.get('surface_ready'):
                        surfaced = initiative_engine.surface_findings(initiative)
                        if surfaced and not args.dry_run:
                            gateway.send_telegram(surfaced)
                            logging.info(f"Surfaced initiative #{initiative['id']} findings")
                        elif surfaced:
                            logging.info(f"DRY RUN [SURFACE]: {surfaced[:200]}")

            # Step 7: Nightly distillation at hour 23
            if now_hour == 23:
                nightly_run_today = db.get_state('nightly_run_date')
                if nightly_run_today != datetime.date.today().isoformat():
                    nightly.run()
                    db.set_state('nightly_run_date', datetime.date.today().isoformat())

            # Step 7b: Weekly review — auto-detect Sunday 10 AM
            now_dt = datetime.datetime.now(MST)
            if now_dt.weekday() == 6 and now_hour == 10:  # Sunday, 10 AM
                last_weekly = db.get_state('last_weekly_review')
                if last_weekly != datetime.date.today().isoformat():
                    weekly.run()

            # Step 8: Update daily metrics
            db.execute(
                "INSERT INTO daily_metrics (date, total_loop_runs) VALUES (date('now'), 1) "
                "ON CONFLICT(date) DO UPDATE SET total_loop_runs = total_loop_runs + 1"
            )

            elapsed = time.time() - loop_start
            logging.info(f"=== Atlas Brain v{BRAIN_VERSION} loop complete in {elapsed:.1f}s ===")

        db.close()
    except Exception as e:
        logging.exception(f"Brain crash: {e}")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
