#!/usr/bin/env python3
"""Atlas Tasks — CLI for managing tasks in atlas-brain.db."""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("ATLAS_DB", os.path.expanduser("~/.openclaw/workspace/.state/atlas-brain.db"))

MST = timezone(timedelta(hours=-7))

# -- Query constants (inline, not dependent on views existing) --

Q_ACTIVE = """
    SELECT * FROM items
    WHERE status IN ('pending', 'in_progress', 'blocked')
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END,
        due ASC
"""

Q_TODAY = """
    SELECT * FROM items
    WHERE status IN ('pending', 'in_progress')
    AND (assigned_date = date('now', 'localtime') OR (due <= date('now', 'localtime') AND assigned_date IS NULL))
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END
"""

Q_OVERDUE = """
    SELECT * FROM items
    WHERE status IN ('pending', 'in_progress') AND due < date('now', 'localtime')
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END,
        due ASC
"""


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"Error: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def format_task(row):
    parts = [f"[{row['id']}] {row['title']}  |  {row['priority']}"]
    if row["due"]:
        parts.append(f"due: {row['due']}")
    status = row["status"]
    if status == "blocked" and row["block_reason"]:
        parts.append(f"blocked: {row['block_reason']}")
    else:
        parts.append(status)
    return "  |  ".join(parts)


def cmd_list(query):
    conn = get_db()
    rows = conn.execute(query).fetchall()
    conn.close()
    if not rows:
        print("(no tasks)")
        return
    for row in rows:
        print(format_task(row))


Q_CATEGORY = """
    SELECT * FROM items
    WHERE status IN ('pending', 'in_progress', 'blocked')
    AND category = ? COLLATE NOCASE
    ORDER BY
        CASE priority
            WHEN 'critical' THEN 0 WHEN 'high' THEN 1
            WHEN 'medium' THEN 2 WHEN 'low' THEN 3
        END,
        due ASC
"""


VALID_PRIORITIES = ("critical", "high", "medium", "low")
VALID_ENERGIES = ("high", "medium", "low")


def generate_slug(title, conn):
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)[:60].strip("-")
    existing = conn.execute("SELECT id FROM items WHERE id = ?", (slug,)).fetchone()
    if existing:
        for i in range(2, 100):
            candidate = f"{slug}-{i}"
            if not conn.execute("SELECT id FROM items WHERE id = ?", (candidate,)).fetchone():
                slug = candidate
                break
    return slug


def update_task(task_id, updates):
    """Update a task by ID. updates is a dict of column=value pairs. Exits 1 if not found."""
    conn = get_db()
    row = conn.execute("SELECT id FROM items WHERE id = ?", (task_id,)).fetchone()
    if not row:
        print(f"Error: task '{task_id}' not found", file=sys.stderr)
        conn.close()
        sys.exit(1)
    updates["updated_at"] = datetime.now(MST).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]
    conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def cmd_add(args):
    if args.priority not in VALID_PRIORITIES:
        print(f"Error: invalid priority '{args.priority}'. Valid: {', '.join(VALID_PRIORITIES)}", file=sys.stderr)
        sys.exit(1)
    if args.energy not in VALID_ENERGIES:
        print(f"Error: invalid energy '{args.energy}'. Valid: {', '.join(VALID_ENERGIES)}", file=sys.stderr)
        sys.exit(1)

    conn = get_db()
    slug = generate_slug(args.title, conn)
    now_iso = datetime.now(MST).isoformat()
    conn.execute(
        "INSERT INTO items (id, title, added, due, priority, energy, category, status, notes, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 'manual')",
        (slug, args.title, now_iso, args.due, args.priority, args.energy, args.category, args.notes),
    )
    conn.commit()
    conn.close()
    print(f"Added: [{slug}] {args.title}")


def main():
    parser = argparse.ArgumentParser(prog="atlas-tasks", description="Manage Atlas Brain tasks")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list_active", help="List all active tasks")
    sub.add_parser("list_today", help="List today's tasks")
    sub.add_parser("list_overdue", help="List overdue tasks")

    cat_parser = sub.add_parser("category", help="List active tasks by category")
    cat_parser.add_argument("name", help="Category name to filter by")

    add_parser = sub.add_parser("add", help="Add a new task")
    add_parser.add_argument("title", help="Task title")
    add_parser.add_argument("--due", default=None, help="Due date (YYYY-MM-DD)")
    add_parser.add_argument("--priority", default="medium", help="critical/high/medium/low")
    add_parser.add_argument("--energy", default="medium", help="high/medium/low")
    add_parser.add_argument("--category", default="general", help="Task category")
    add_parser.add_argument("--notes", default=None, help="Additional notes")

    done_parser = sub.add_parser("done", help="Mark a task as done")
    done_parser.add_argument("id", help="Task ID (slug)")

    blocked_parser = sub.add_parser("blocked", help="Mark a task as blocked")
    blocked_parser.add_argument("id", help="Task ID (slug)")
    blocked_parser.add_argument("--reason", required=True, help="Why it's blocked")

    snooze_parser = sub.add_parser("snooze", help="Snooze a task to a later date")
    snooze_parser.add_argument("id", help="Task ID (slug)")
    snooze_parser.add_argument("--date", required=True, help="New date (YYYY-MM-DD or datetime)")

    args = parser.parse_args()

    if args.command == "list_active":
        cmd_list(Q_ACTIVE)
    elif args.command == "list_today":
        cmd_list(Q_TODAY)
    elif args.command == "list_overdue":
        cmd_list(Q_OVERDUE)
    elif args.command == "category":
        conn = get_db()
        rows = conn.execute(Q_CATEGORY, (args.name,)).fetchall()
        conn.close()
        if not rows:
            print("(no tasks)")
        else:
            for row in rows:
                print(format_task(row))
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "done":
        update_task(args.id, {"status": "done"})
        print(f"Marked [{args.id}] as done")
    elif args.command == "blocked":
        update_task(args.id, {"status": "blocked", "block_reason": args.reason})
        print(f"Marked [{args.id}] as blocked: {args.reason}")
    elif args.command == "snooze":
        update_task(args.id, {"scheduled_at": args.date})
        print(f"Snoozed [{args.id}] until {args.date}")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
