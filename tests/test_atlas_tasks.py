#!/usr/bin/env python3
"""Tests for atlas-tasks.py CLI."""
import subprocess
import sqlite3
import tempfile
import os
import sys
from datetime import datetime

# -- helpers --

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "atlas-tasks", "scripts", "atlas-tasks.py")

ITEMS_SCHEMA = """
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    added TEXT NOT NULL,
    due TEXT,
    priority TEXT CHECK(priority IN ('critical','high','medium','low')) DEFAULT 'medium',
    energy TEXT CHECK(energy IN ('high','medium','low')) DEFAULT 'medium',
    category TEXT,
    status TEXT CHECK(status IN ('pending','in_progress','blocked','done','cancelled')) DEFAULT 'pending',
    block_reason TEXT,
    nudge_count INTEGER DEFAULT 0,
    last_nudge TEXT,
    last_response TEXT,
    notes TEXT,
    scheduled_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    estimated_minutes INTEGER,
    actual_minutes INTEGER,
    assigned_date TEXT,
    energy_window TEXT,
    source TEXT DEFAULT 'manual',
    task_type TEXT,
    time_window TEXT,
    trigger_type TEXT,
    context_tags TEXT
);
"""


def run_cli(db_path, *args):
    """Run atlas-tasks.py with ATLAS_DB env override and return (stdout+stderr, returncode)."""
    env = os.environ.copy()
    env["ATLAS_DB"] = db_path
    result = subprocess.run(
        [sys.executable, SCRIPT] + list(args),
        capture_output=True, text=True, env=env
    )
    return result.stdout.strip(), result.returncode, result.stderr.strip()


def make_db():
    """Create a temp DB with the items schema, return (path, connection)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(ITEMS_SCHEMA)
    return tmp.name, conn


def seed_task(conn, id, title, status="pending", priority="medium", due=None, category="general", block_reason=None, assigned_date=None):
    conn.execute(
        "INSERT INTO items (id, title, added, status, priority, due, category, block_reason, assigned_date) VALUES (?,?,datetime('now'),?,?,?,?,?,?)",
        (id, title, status, priority, due, category, block_reason, assigned_date),
    )
    conn.commit()


# -- tests --

def test_list_active_with_tasks():
    db_path, conn = make_db()
    try:
        seed_task(conn, "fix-bug", "Fix the bug", priority="high")
        seed_task(conn, "buy-milk", "Buy milk", priority="low")
        seed_task(conn, "old-task", "Old task", status="done")
        conn.close()

        out, rc, _ = run_cli(db_path, "list_active")
        assert rc == 0
        assert "[fix-bug]" in out
        assert "[buy-milk]" in out
        assert "[old-task]" not in out
        # high priority should appear before low
        assert out.index("fix-bug") < out.index("buy-milk")
    finally:
        os.unlink(db_path)


def test_list_active_empty():
    db_path, conn = make_db()
    conn.close()
    try:
        out, rc, _ = run_cli(db_path, "list_active")
        assert rc == 0
        assert "no" in out.lower()
    finally:
        os.unlink(db_path)


def test_db_not_found():
    _, rc, err = run_cli("/tmp/nonexistent_atlas_test.db", "list_active")
    assert rc == 1
    assert "not found" in err.lower() or "error" in err.lower()


def test_list_today():
    db_path, conn = make_db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        seed_task(conn, "today-task", "Today task", assigned_date=today)
        seed_task(conn, "other-task", "Other task")  # no assigned_date, no due
        conn.close()

        out, rc, _ = run_cli(db_path, "list_today")
        assert rc == 0
        assert "[today-task]" in out
        assert "[other-task]" not in out
    finally:
        os.unlink(db_path)


def test_list_overdue():
    db_path, conn = make_db()
    try:
        seed_task(conn, "overdue-task", "Overdue task", due="2020-01-01")
        seed_task(conn, "future-task", "Future task", due="2099-12-31")
        conn.close()

        out, rc, _ = run_cli(db_path, "list_overdue")
        assert rc == 0
        assert "[overdue-task]" in out
        assert "[future-task]" not in out
    finally:
        os.unlink(db_path)


def test_category_filter():
    db_path, conn = make_db()
    try:
        seed_task(conn, "work-task", "Work task", category="work")
        seed_task(conn, "personal-task", "Personal task", category="personal")
        conn.close()

        out, rc, _ = run_cli(db_path, "category", "work")
        assert rc == 0
        assert "[work-task]" in out
        assert "[personal-task]" not in out
    finally:
        os.unlink(db_path)


def test_category_empty():
    db_path, conn = make_db()
    conn.close()
    try:
        out, rc, _ = run_cli(db_path, "category", "nonexistent")
        assert rc == 0
        assert "no" in out.lower()
    finally:
        os.unlink(db_path)


def test_add_task_basic():
    db_path, conn = make_db()
    try:
        conn.close()
        out, rc, _ = run_cli(db_path, "add", "Buy groceries")
        assert rc == 0
        assert "buy-groceries" in out

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM items WHERE id = 'buy-groceries'").fetchone()
        assert row is not None
        assert row[1] == "Buy groceries"  # title
        conn.close()
    finally:
        os.unlink(db_path)


def test_add_task_with_options():
    db_path, conn = make_db()
    try:
        conn.close()
        out, rc, _ = run_cli(db_path, "add", "Fix VPN", "--due", "2026-04-15", "--priority", "high", "--energy", "low", "--category", "infra", "--notes", "Need creds first")
        assert rc == 0
        assert "fix-vpn" in out

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM items WHERE id = 'fix-vpn'").fetchone()
        assert row["priority"] == "high"
        assert row["energy"] == "low"
        assert row["due"] == "2026-04-15"
        assert row["category"] == "infra"
        assert row["notes"] == "Need creds first"
        conn.close()
    finally:
        os.unlink(db_path)


def test_add_task_slug_dedup():
    db_path, conn = make_db()
    try:
        seed_task(conn, "buy-milk", "Buy milk")
        conn.close()
        out, rc, _ = run_cli(db_path, "add", "Buy milk")
        assert rc == 0
        assert "buy-milk-2" in out
    finally:
        os.unlink(db_path)


def test_add_task_slug_special_chars():
    db_path, conn = make_db()
    try:
        conn.close()
        out, rc, _ = run_cli(db_path, "add", "Fix the VPN (urgent!!)")
        assert rc == 0
        assert "fix-the-vpn-urgent" in out
    finally:
        os.unlink(db_path)


def test_add_task_invalid_priority():
    db_path, conn = make_db()
    conn.close()
    try:
        _, rc, err = run_cli(db_path, "add", "Bad task", "--priority", "mega")
        assert rc == 1
    finally:
        os.unlink(db_path)


def test_mark_done():
    db_path, conn = make_db()
    try:
        seed_task(conn, "fix-bug", "Fix bug")
        conn.close()

        out, rc, _ = run_cli(db_path, "done", "fix-bug")
        assert rc == 0
        assert "done" in out.lower()

        conn = sqlite3.connect(db_path)
        status = conn.execute("SELECT status FROM items WHERE id = 'fix-bug'").fetchone()[0]
        assert status == "done"
        conn.close()
    finally:
        os.unlink(db_path)


def test_mark_done_not_found():
    db_path, conn = make_db()
    conn.close()
    try:
        _, rc, err = run_cli(db_path, "done", "nonexistent")
        assert rc == 1
        assert "not found" in err.lower()
    finally:
        os.unlink(db_path)


def test_mark_blocked():
    db_path, conn = make_db()
    try:
        seed_task(conn, "fix-vpn", "Fix VPN")
        conn.close()

        out, rc, _ = run_cli(db_path, "blocked", "fix-vpn", "--reason", "Waiting on credentials")
        assert rc == 0
        assert "blocked" in out.lower()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, block_reason FROM items WHERE id = 'fix-vpn'").fetchone()
        assert row["status"] == "blocked"
        assert row["block_reason"] == "Waiting on credentials"
        conn.close()
    finally:
        os.unlink(db_path)


def test_snooze():
    db_path, conn = make_db()
    try:
        seed_task(conn, "write-report", "Write report")
        conn.close()

        out, rc, _ = run_cli(db_path, "snooze", "write-report", "--date", "2026-04-10")
        assert rc == 0
        assert "snoozed" in out.lower() or "2026-04-10" in out

        conn = sqlite3.connect(db_path)
        scheduled = conn.execute("SELECT scheduled_at FROM items WHERE id = 'write-report'").fetchone()[0]
        assert scheduled == "2026-04-10"
        conn.close()
    finally:
        os.unlink(db_path)


def test_snooze_not_found():
    db_path, conn = make_db()
    conn.close()
    try:
        _, rc, err = run_cli(db_path, "snooze", "ghost-task", "--date", "2026-04-10")
        assert rc == 1
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    test_list_active_with_tasks()
    test_list_active_empty()
    test_db_not_found()
    test_list_today()
    test_list_overdue()
    test_category_filter()
    test_category_empty()
    test_add_task_basic()
    test_add_task_with_options()
    test_add_task_slug_dedup()
    test_add_task_slug_special_chars()
    test_add_task_invalid_priority()
    test_mark_done()
    test_mark_done_not_found()
    test_mark_blocked()
    test_snooze()
    test_snooze_not_found()
    print("All tests passed.")
