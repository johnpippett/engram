# Installation Guide

Step-by-step setup for running Engram with OpenClaw.

## Prerequisites

- Python 3.9+ (stdlib only, no pip dependencies)
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- An OpenRouter-compatible LLM API endpoint and token
- OpenClaw installed and configured
- Optional: `gog` CLI for Google Calendar integration

## Step 1: Clone the Repository

```bash
git clone https://github.com/johnpippett/engram.git
cd engram
```

## Step 2: Run Setup

```bash
bash setup.sh
```

This prompts for your Telegram bot token, LLM gateway URL and key, name, timezone, quiet hours, and scheduling preferences. It writes everything to a `.env` file (never committed).

**Manual alternative:**

```bash
cp config.env.example .env
# Edit .env with your values
```

## Step 3: Set Up Your Profile

Create the OpenClaw workspace directories and copy the template files:

```bash
mkdir -p ~/.openclaw/workspace/.state
mkdir -p ~/.openclaw/workspace/memory/distillation-staging
cp templates/*.md ~/.openclaw/workspace/
```

Now edit `~/.openclaw/workspace/USER.md` with your actual information -- name, role, schedule, family context, communication preferences. This is what Atlas reads every session to know who you are. The more detail you provide, the better Atlas adapts to you.

The other template files (`MEMORY.md`, `INFERENCES.md`, `PROCEDURE.md`) start mostly empty and get populated over time by nightly distillation. `SOUL.md` defines Atlas's personality -- customize it if you want a different tone. `AGENTS.md` defines the behavioral rules and intent detection patterns.

These files live in your local workspace and are never committed to git.

## Step 4: Initialize the Database

```bash
python3 brain.py --init
```

This creates the SQLite database at `~/.openclaw/workspace/.state/atlas-brain.db` with all required tables and seeds the mechanical reminders.

**Verify it worked:**

```bash
python3 brain.py --status
```

You should see a status summary showing 0 items, 0 initiatives, 7 mechanical reminders, and version 2.0.0.

## Step 5: Install Cron Entries

The `setup.sh` script offers to install these automatically. To do it manually:

```bash
crontab -e
```

Add these lines (replace `/path/to/engram` with your actual clone location):

```
# Atlas Brain -- main loop (every 20 min)
*/20 * * * * cd /path/to/engram && python3 brain.py >> .state/atlas-brain-cron.log 2>&1

# Morning briefing (7:15 AM)
15 7 * * * cd /path/to/engram && python3 brain.py --morning >> .state/atlas-brain-cron.log 2>&1

# Nightly distillation (11:05 PM)
5 23 * * * cd /path/to/engram && python3 brain.py --nightly >> .state/atlas-brain-cron.log 2>&1

# Weekly review (Sunday 10:05 AM)
5 10 * * 0 cd /path/to/engram && python3 brain.py --weekly >> .state/atlas-brain-cron.log 2>&1

# Watchdog (every 30 min)
*/30 * * * * cd /path/to/engram && python3 brain.py --watchdog >> .state/atlas-brain-cron.log 2>&1
```

## Step 6: Register OpenClaw Skills

Atlas Brain includes two OpenClaw skills that let the agent manage tasks and initiatives during conversation:

**Option A: Symlink (recommended)**

```bash
ln -s /path/to/engram/atlas-tasks ~/.openclaw/skills/atlas-tasks
ln -s /path/to/engram/atlas-initiatives ~/.openclaw/skills/atlas-initiatives
```

**Option B: Copy**

```bash
cp -r /path/to/engram/atlas-tasks ~/.openclaw/skills/
cp -r /path/to/engram/atlas-initiatives ~/.openclaw/skills/
```

OpenClaw discovers skills by scanning for directories containing a `SKILL.md` file with YAML frontmatter. Once the skill directories are in your skills path, OpenClaw will automatically detect them and use them when the conversation context matches.

The skills need access to the same database that `brain.py` uses. Both default to `~/.openclaw/workspace/.state/atlas-brain.db`, so no extra configuration is needed if you followed the steps above. If your DB is elsewhere, set the `ATLAS_DB` environment variable.

## Step 7: Verify Everything Works

```bash
# Check system status
python3 brain.py --status

# Add a test task via brain.py CLI
python3 brain.py --add "Test task - delete me"
python3 brain.py --list

# Verify the skill scripts can see the same data
python3 atlas-tasks/scripts/atlas-tasks.py list_active

# Clean up
python3 brain.py --done test-task-delete-me

# Test a dry run of the main loop (no messages sent)
python3 brain.py --dry-run
```

If `list_active` shows the test task from both `brain.py` and `atlas-tasks.py`, the shared database is working correctly.

To test the Telegram connection:

```bash
python3 brain.py --morning
```

A morning briefing should arrive in your Telegram chat. If it does, you're fully set up.

## What Happens Next

- **brain.py** runs every 20 minutes via cron, handling perception, reminders, action decisions, and initiative steps
- **Nightly distillation** (11 PM) reviews the last 3 days of episodic memory and promotes important facts to semantic memory (`MEMORY.md`) and behavioral patterns to procedural memory (`PROCEDURE.md`)
- **OpenClaw skills** let you manage tasks and initiatives through natural conversation ("I finished X", "research Y for me")
- **Memory grows over time** -- the more you use Atlas, the better it understands your patterns, preferences, and schedule
