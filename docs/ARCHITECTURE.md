# Architecture

## Runtime Model

Engram has a single entry point: `brain.py`. It runs as a cron job every 20 minutes for its main loop, and is also invoked directly for scheduled tasks (`--morning`, `--nightly`, `--weekly`) and CLI operations (`--add`, `--list`, `--status`, etc.).

There is no long-running daemon. Each invocation is a short-lived process that reads state from a shared SQLite database and markdown files, performs its work, and exits.

## Cron Loop Flow

When `brain.py` runs without flags (the default cron invocation), it executes these stages in order:

1. **Perception** --- Reads recent session transcripts and scans for task signals (completed, snoozed, blocked, new task). Uses regex pattern matching first, falling back to an LLM call for ambiguous signals.
2. **Trigger Processing** --- Evaluates pending triggers (time-based, event-based) and marks those that have fired.
3. **Auto-Assign Dates** --- Tasks without due dates are assigned optimal dates based on priority, energy requirements, and calendar availability.
4. **Mechanical Reminders** --- Checks recurring reminders against their schedules, sends any that are due, and tracks acknowledgments.
5. **Action Layer** --- The LLM decision engine. Receives current context (tasks, memory, recent activity) and chooses from 9 action types: nudge, check-in, anticipate, reflect, propose, surface, work, route, or silence. Most runs result in silence.
6. **Initiative Step** --- If time permits and budget allows, executes one step of an active initiative (autonomous research). Each step is time-boxed to 90 seconds.
7. **Nightly Distillation** --- Runs only at 11 PM. Promotes episodic memory entries to semantic/procedural memory.
8. **Weekly Review** --- Runs only on Sunday at 10 AM. Generates a progress summary with pattern analysis and sends it via Telegram.

## OpenClaw Integration

Atlas Brain is designed to run inside OpenClaw, an AI agent runtime. Two companion scripts serve as OpenClaw skills:

- **atlas-tasks** (`atlas-tasks/`) --- CLI for task CRUD operations. Reads and writes the shared SQLite database directly. Registered as an OpenClaw skill so the conversational agent can manage tasks during sessions.
- **atlas-initiatives** (`atlas-initiatives/`) --- CLI for initiative management. Same pattern: direct DB access, registered as an OpenClaw skill.

Both skills and `brain.py` share the same SQLite database as their source of truth. There is no API layer between them --- they use the same schema and access patterns. SQLite's file-level locking handles concurrency between the cron job and interactive OpenClaw sessions.

## Four-Layer Memory Model

Memory is organized in four layers, modeled after human memory consolidation:

| Layer | Storage | Lifespan | Purpose |
|---|---|---|---|
| L1: Working | SESSION-STATE.md | Single session | Scratch pad for current conversation context |
| L2: Episodic | memory/YYYY-MM-DD.md | 30 days | Daily event logs, observations, interactions |
| L3: Semantic | MEMORY.md | Permanent | Facts, preferences, relationships (8KB cap) |
| L4: Procedural | PROCEDURE.md | Permanent | Behavioral patterns, communication style, timing |

Promotion flows upward. Nightly distillation reads the last 3 days of episodic entries, scores each for confidence, and stages candidates. High-confidence entries are auto-promoted to semantic or procedural memory. Low-confidence entries age for 3 days before promotion. Duplicate detection prevents redundant entries in MEMORY.md. Monthly verification surfaces uncertain entries for human review.

## Bootstrap File Load Order

When brain.py starts (or when OpenClaw loads context), it assembles its working context from these files in order:

1. **SOUL.md** --- Core identity, values, behavioral guidelines
2. **USER.md** --- User profile, preferences, communication style
3. **Today's episodic** (memory/YYYY-MM-DD.md) --- What happened today
4. **Yesterday's episodic** (memory/YYYY-MM-DD.md) --- Recent context
5. **handoff.md** (if exists) --- Inter-session notes left by a previous run
6. **MEMORY.md** --- Long-term semantic memory
7. **INFERENCES.md** --- Derived conclusions and predictions
8. **AGENTS.md** --- Definitions of sub-agent personas and behaviors

This load order ensures the agent always has identity context first, then recent events, then long-term knowledge.

## Database as Shared State

The SQLite database (`.state/atlas.db`) is the single source of truth for structured data: tasks, initiatives, reminders, triggers, action history, and rate-limiting state. Both `brain.py` (running via cron) and the OpenClaw skills (running interactively) read and write this database directly. Markdown files handle unstructured memory; the database handles everything else.
