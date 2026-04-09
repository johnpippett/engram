#!/usr/bin/env python3
"""Atlas Initiatives — CLI for managing autonomous initiatives in atlas-brain.db."""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("ATLAS_DB", os.path.expanduser("~/.openclaw/workspace/.state/atlas-brain.db"))
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:18789/v1/chat/completions")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
MODEL = "openclaw"

MST = timezone(timedelta(hours=-7))

APPROVED_AUTO_CATEGORIES = ["monitoring"]
MAX_ACTIVE = 10
MAX_STEPS_PER_DAY = 5
MAX_OPUS_PER_DAY = 10
DEFAULT_COST_CAP = 10.0
DEFAULT_TIME_CAP = 3600  # seconds


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"Error: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def format_initiative(row):
    parts = [f"[{row['id']}] {row['title']}  |  {row['status']}  |  {row['category']}"]
    cost = row["estimated_cost_usd"] or 0
    cap = row["cost_cap_usd"] or DEFAULT_COST_CAP
    parts.append(f"${cost:.2f}/${cap:.2f}")
    t = row["time_invested_seconds"] or 0
    tmax = row["max_time_seconds"] or DEFAULT_TIME_CAP
    parts.append(f"{t // 60}m/{tmax // 60}m")
    if row["last_worked"]:
        parts.append(f"last: {row['last_worked'][:16]}")
    return "  |  ".join(parts)


# ── List commands ──────────────────────────────────────────────

def cmd_list_active():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM atlas_initiatives WHERE status = 'active' "
        "ORDER BY priority ASC, last_worked ASC NULLS FIRST"
    ).fetchall()
    conn.close()
    if not rows:
        print("(no active initiatives)")
        return
    for r in rows:
        print(format_initiative(r))


def cmd_list_pending():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM atlas_initiatives WHERE status = 'waiting_approval' "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    if not rows:
        print("(no initiatives pending approval)")
        return
    for r in rows:
        print(format_initiative(r))
        if r["description"]:
            print(f"    {r['description'][:120]}")


def cmd_list_all():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM atlas_initiatives WHERE status IN ('active', 'paused', 'waiting_approval') "
        "ORDER BY status, priority ASC"
    ).fetchall()
    conn.close()
    if not rows:
        print("(no initiatives)")
        return
    for r in rows:
        print(format_initiative(r))


# ── CRUD commands ──────────────────────────────────────────────

def cmd_create(args):
    conn = get_db()

    # Non-monitoring categories require approval
    status = "active"
    trigger = args.trigger or "user_request"
    if trigger == "auto" and args.category not in APPROVED_AUTO_CATEGORIES:
        status = "waiting_approval"

    if not args.done_condition:
        print("Error: --done-condition is required", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Max active check
    active_count = conn.execute(
        "SELECT COUNT(*) as c FROM atlas_initiatives WHERE status='active'"
    ).fetchone()["c"]
    if status == "active" and active_count >= MAX_ACTIVE:
        print(f"Error: max active initiatives reached ({active_count}/{MAX_ACTIVE})", file=sys.stderr)
        conn.close()
        sys.exit(1)

    notes = f"done_condition: {args.done_condition}\ntrigger: {trigger}"
    cur = conn.execute(
        "INSERT INTO atlas_initiatives (title, description, category, status, cost_cap_usd, "
        "max_time_seconds, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (args.title, args.desc, args.category, status, args.cost_cap,
         args.time_cap, notes),
    )
    conn.commit()
    init_id = cur.lastrowid
    conn.close()
    label = "Created" if status == "active" else "Queued for approval"
    print(f"{label}: [{init_id}] {args.title}")


def cmd_approve(args):
    conn = get_db()
    row = conn.execute(
        "SELECT id, status, title FROM atlas_initiatives WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        print(f"Error: initiative {args.id} not found", file=sys.stderr)
        conn.close()
        sys.exit(1)
    if row["status"] != "waiting_approval":
        print(f"Error: initiative {args.id} is '{row['status']}', not waiting_approval", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Max active check
    active_count = conn.execute(
        "SELECT COUNT(*) as c FROM atlas_initiatives WHERE status='active'"
    ).fetchone()["c"]
    if active_count >= MAX_ACTIVE:
        print(f"Error: max active initiatives reached ({active_count}/{MAX_ACTIVE})", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE atlas_initiatives SET status='active', approved_at=datetime('now'), "
        "updated_at=datetime('now') WHERE id=?", (args.id,)
    )
    conn.commit()
    conn.close()
    print(f"Approved: [{args.id}] {row['title']}")


def cmd_reject(args):
    conn = get_db()
    row = conn.execute(
        "SELECT id, status, title FROM atlas_initiatives WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        print(f"Error: initiative {args.id} not found", file=sys.stderr)
        conn.close()
        sys.exit(1)
    conn.execute(
        "UPDATE atlas_initiatives SET status='cancelled', updated_at=datetime('now') WHERE id=?",
        (args.id,),
    )
    conn.commit()
    conn.close()
    print(f"Rejected: [{args.id}] {row['title']}")


def cmd_view(args):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM atlas_initiatives WHERE id = ?", (args.id,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"Error: initiative {args.id} not found", file=sys.stderr)
        sys.exit(1)
    print(f"[{row['id']}] {row['title']}")
    print(f"Status: {row['status']}  |  Category: {row['category']}  |  Priority: {row['priority']}")
    print(f"Cost: ${(row['estimated_cost_usd'] or 0):.2f} / ${(row['cost_cap_usd'] or DEFAULT_COST_CAP):.2f}")
    t = row['time_invested_seconds'] or 0
    tmax = row['max_time_seconds'] or DEFAULT_TIME_CAP
    print(f"Time: {t // 60}m / {tmax // 60}m")
    if row['description']:
        print(f"\nDescription: {row['description']}")
    if row['notes']:
        print(f"\nNotes:\n{row['notes']}")
    if row['next_step']:
        print(f"\nNext step: {row['next_step']}")
    if row['findings']:
        print(f"\nFindings:\n{row['findings']}")


def cmd_pause(args):
    conn = get_db()
    row = conn.execute(
        "SELECT id, status, title FROM atlas_initiatives WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        print(f"Error: initiative {args.id} not found", file=sys.stderr)
        conn.close()
        sys.exit(1)
    if row["status"] != "active":
        print(f"Error: initiative {args.id} is '{row['status']}', not active", file=sys.stderr)
        conn.close()
        sys.exit(1)
    reason = args.reason or "Manual pause"
    conn.execute(
        "UPDATE atlas_initiatives SET status='paused', "
        "notes=COALESCE(notes,'')||'\n[Paused: ' || ? || ']', updated_at=datetime('now') WHERE id=?",
        (reason, args.id),
    )
    conn.commit()
    conn.close()
    print(f"Paused: [{args.id}] {row['title']}")


def cmd_resume(args):
    conn = get_db()
    row = conn.execute(
        "SELECT id, status, title FROM atlas_initiatives WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        print(f"Error: initiative {args.id} not found", file=sys.stderr)
        conn.close()
        sys.exit(1)
    if row["status"] != "paused":
        print(f"Error: initiative {args.id} is '{row['status']}', not paused", file=sys.stderr)
        conn.close()
        sys.exit(1)

    active_count = conn.execute(
        "SELECT COUNT(*) as c FROM atlas_initiatives WHERE status='active'"
    ).fetchone()["c"]
    if active_count >= MAX_ACTIVE:
        print(f"Error: max active initiatives reached ({active_count}/{MAX_ACTIVE})", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE atlas_initiatives SET status='active', updated_at=datetime('now') WHERE id=?",
        (args.id,),
    )
    conn.commit()
    conn.close()
    print(f"Resumed: [{args.id}] {row['title']}")


# ── Step execution ─────────────────────────────────────────────

def _daily_steps_used(conn):
    row = conn.execute(
        "SELECT initiatives_worked FROM daily_metrics WHERE date = date('now')"
    ).fetchone()
    return row["initiatives_worked"] if row else 0


def _empty_step_count(conn, initiative_id):
    row = conn.execute(
        "SELECT COUNT(*) as c FROM memory_events "
        "WHERE related_item_id=? AND content='empty_initiative_step' "
        "AND timestamp > datetime('now', '-48 hours')",
        (str(initiative_id),),
    ).fetchone()
    return row["c"] if row else 0


def _check_opus_budget(conn):
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'daily_opus_calls'"
    ).fetchone()
    count = int(row["value"]) if row else 0
    return count < MAX_OPUS_PER_DAY


def _record_opus_call(conn):
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'daily_opus_calls'"
    ).fetchone()
    count = int(row["value"]) if row else 0
    conn.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES ('daily_opus_calls', ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (str(count + 1),),
    )
    conn.commit()


def _gateway_chat(system_prompt, user_message, max_tokens=1200, temperature=0.7):
    if not GATEWAY_TOKEN:
        raise RuntimeError("GATEWAY_TOKEN environment variable not set")
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        GATEWAY_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Gateway error: {e}", file=sys.stderr)
        return None


def select_initiative(conn):
    """Pick best active initiative to work on. Returns row or None."""
    initiatives = conn.execute(
        "SELECT * FROM atlas_initiatives WHERE status = 'active' "
        "AND time_invested_seconds < max_time_seconds "
        "ORDER BY priority ASC, last_worked ASC NULLS FIRST"
    ).fetchall()
    if not initiatives:
        return None

    now = datetime.now(MST)
    best = None
    best_score = -1

    for init in initiatives:
        # Cost cap check
        if (init["estimated_cost_usd"] or 0) >= (init["cost_cap_usd"] or DEFAULT_COST_CAP):
            continue

        score = 6 - (init["priority"] or 3)  # Priority score

        # Staleness bonus
        if init["last_worked"]:
            try:
                last = datetime.fromisoformat(init["last_worked"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=MST)
                hours_stale = (now - last).total_seconds() / 3600
                score += min(hours_stale / 24, 3)
            except (ValueError, TypeError):
                score += 3
        else:
            score += 3  # Never worked = max staleness

        # Budget remaining bonus
        remaining = 1 - ((init["time_invested_seconds"] or 0) / max(init["max_time_seconds"] or DEFAULT_TIME_CAP, 1))
        score += remaining * 2

        if score > best_score:
            best_score = score
            best = init

    return best


def cmd_step(args):
    conn = get_db()

    # Daily budget check
    steps_used = _daily_steps_used(conn)
    if steps_used >= MAX_STEPS_PER_DAY:
        print(f"Daily initiative budget exhausted ({steps_used}/{MAX_STEPS_PER_DAY})")
        conn.close()
        return

    initiative = select_initiative(conn)
    if not initiative:
        print("No eligible initiative to work on")
        conn.close()
        return

    init_id = initiative["id"]
    cost_used = initiative["estimated_cost_usd"] or 0
    cost_cap = initiative["cost_cap_usd"] or DEFAULT_COST_CAP

    # Cost cap check (redundant safety)
    if cost_used >= cost_cap:
        conn.execute(
            "UPDATE atlas_initiatives SET status='paused', "
            "notes=COALESCE(notes,'')||'\n[Paused: cost cap reached]' WHERE id=?",
            (init_id,),
        )
        conn.commit()
        print(f"Initiative [{init_id}] paused: cost cap reached (${cost_used:.2f}/${cost_cap:.2f})")
        conn.close()
        return

    time_budget = args.time_budget
    max_tokens = min(1500, max(500, time_budget * 15))
    start = time.time()

    prompt = f"""You are Atlas, working on a self-directed initiative.

## Initiative: {initiative['title']}
{initiative['description'] or ''}

## Current findings:
{initiative['findings'] or 'None yet'}

## Next step to execute:
{initiative['next_step'] or 'Begin initial research/exploration'}

## Notes:
{initiative['notes'] or 'None'}

Execute the next step. You have {time_budget} seconds.

Return JSON:
{{
    "findings": "what you found/produced this step (append to existing)",
    "next_step": "what to do next iteration",
    "surface_ready": false,
    "summary": "one-line summary of what you did"
}}"""

    response = _gateway_chat(
        system_prompt="You are Atlas's initiative engine. Execute research/drafting/monitoring steps. Return ONLY valid JSON.",
        user_message=prompt,
        max_tokens=max_tokens,
        temperature=0.7,
    )

    elapsed = time.time() - start

    if not response:
        print(f"Gateway call failed for initiative [{init_id}]", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Parse response
    try:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {
            "findings": response[:500],
            "next_step": initiative["next_step"],
            "surface_ready": False,
            "summary": "Partial result",
        }

    # Empty step detection
    findings_delta = result.get("findings", "").strip()
    if len(findings_delta) < 20:
        conn.execute(
            "INSERT INTO memory_events (layer, event_type, content, source, related_item_id) "
            "VALUES ('procedural', 'write', 'empty_initiative_step', 'initiative', ?)",
            (str(init_id),),
        )
        conn.commit()
        empty_count = _empty_step_count(conn, init_id)
        if empty_count >= 2:
            conn.execute(
                "UPDATE atlas_initiatives SET status='paused', "
                "notes=COALESCE(notes,'')||'\n[Auto-paused: 2 consecutive empty steps]' WHERE id=?",
                (init_id,),
            )
            conn.commit()
            print(f"Initiative [{init_id}] auto-paused: 2 consecutive empty steps")
            conn.close()
            return

    # Update initiative
    now_str = datetime.now(MST).strftime("%Y-%m-%d %H:%M")
    new_findings = (initiative["findings"] or "") + f"\n\n### [{now_str}]\n{result.get('findings', '')}"
    new_time = (initiative["time_invested_seconds"] or 0) + int(elapsed)

    conn.execute(
        "UPDATE atlas_initiatives SET findings=?, next_step=?, last_worked=datetime('now'), "
        "time_invested_seconds=?, updated_at=datetime('now') WHERE id=?",
        (new_findings, result.get("next_step", ""), new_time, init_id),
    )

    # Rough cost estimate
    output_tokens = len(response) // 4 if response else 0
    step_cost = (1000 / 1000) * 0.003 + (output_tokens / 1000) * 0.015  # Sonnet pricing
    conn.execute(
        "UPDATE atlas_initiatives SET estimated_cost_usd = estimated_cost_usd + ? WHERE id=?",
        (step_cost, init_id),
    )

    # Update daily metrics
    conn.execute(
        "INSERT INTO daily_metrics (date, initiatives_worked) VALUES (date('now'), 1) "
        "ON CONFLICT(date) DO UPDATE SET initiatives_worked = initiatives_worked + 1",
    )
    conn.commit()

    # Log to memory_events
    conn.execute(
        "INSERT INTO memory_events (layer, event_type, content, source, related_item_id) "
        "VALUES ('episodic', 'write', ?, 'initiative', ?)",
        (f"Initiative '{initiative['title']}': {result.get('summary', 'step completed')}", str(init_id)),
    )
    conn.commit()
    conn.close()

    surface = result.get("surface_ready", False)
    summary = result.get("summary", "step completed")
    print(f"Step done: [{init_id}] {initiative['title']}")
    print(f"  {summary} ({elapsed:.1f}s, ~${step_cost:.4f})")
    if surface:
        print(f"  ** Findings ready to surface **")


# ── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="atlas-initiatives", description="Manage Atlas initiatives")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list_active", help="List active initiatives")
    sub.add_parser("list_pending", help="List initiatives waiting for approval")
    sub.add_parser("list_all", help="List all non-completed initiatives")

    create_p = sub.add_parser("create", help="Create a new initiative")
    create_p.add_argument("title", help="Initiative title")
    create_p.add_argument("--desc", default="", help="Description")
    create_p.add_argument("--category", default="research", help="Category (research/monitoring/brain/etc)")
    create_p.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP, help=f"Cost cap in USD (default {DEFAULT_COST_CAP})")
    create_p.add_argument("--time-cap", type=int, default=DEFAULT_TIME_CAP, help=f"Time cap in seconds (default {DEFAULT_TIME_CAP})")
    create_p.add_argument("--done-condition", required=True, help="What constitutes 'done'")
    create_p.add_argument("--trigger", default="user_request", help="Trigger type (user_request/auto/pre_approved)")

    approve_p = sub.add_parser("approve", help="Approve a pending initiative")
    approve_p.add_argument("id", type=int, help="Initiative ID")

    reject_p = sub.add_parser("reject", help="Reject a pending initiative")
    reject_p.add_argument("id", type=int, help="Initiative ID")

    view_p = sub.add_parser("view", help="View full initiative details")
    view_p.add_argument("id", type=int, help="Initiative ID")

    pause_p = sub.add_parser("pause", help="Pause an active initiative")
    pause_p.add_argument("id", type=int, help="Initiative ID")
    pause_p.add_argument("--reason", default=None, help="Pause reason")

    resume_p = sub.add_parser("resume", help="Resume a paused initiative")
    resume_p.add_argument("id", type=int, help="Initiative ID")

    step_p = sub.add_parser("step", help="Execute one initiative step")
    step_p.add_argument("--time-budget", type=int, default=90, help="Time budget in seconds (default 90)")

    args = parser.parse_args()

    if args.command == "list_active":
        cmd_list_active()
    elif args.command == "list_pending":
        cmd_list_pending()
    elif args.command == "list_all":
        cmd_list_all()
    elif args.command == "create":
        cmd_create(args)
    elif args.command == "approve":
        cmd_approve(args)
    elif args.command == "reject":
        cmd_reject(args)
    elif args.command == "view":
        cmd_view(args)
    elif args.command == "pause":
        cmd_pause(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "step":
        cmd_step(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
