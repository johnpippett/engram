# Engram

A self-improving personal AI agent with four-layer memory, calendar-aware task routing, and autonomous initiative execution. Designed to run inside OpenClaw with a background cron loop for memory consolidation.

## What It Does

- **Task Management** --- Tracks tasks with priority, energy level, due dates, and auto-assigned scheduling
- **Perception Layer** --- Reads session transcripts, detects task signals (done, snoozed, blocked, new task) via regex + LLM fallback
- **Action Engine** --- LLM-powered decision loop chooses from 9 action types (nudge, check-in, anticipate, reflect, propose, surface, work, route, silence)
- **Four-Layer Memory** --- Working -> Episodic -> Semantic -> Procedural, with nightly distillation promoting patterns upward
- **Self-Directed Initiatives** --- Autonomously researches topics within cost/time budgets, surfaces findings when ready
- **Morning Briefing** --- Calendar + tasks + weather + overdue items, composed in your voice
- **Weekly Review** --- Automated progress summary with pattern analysis
- **Mechanical Reminders** --- Recurring reminders with schedule awareness and acknowledgment tracking
- **Calendar-Aware Routing** --- Integrates with Google Calendar via gog CLI to find optimal task slots
- **Guardrails** --- Rate limits, cooldowns, quiet hours, cost caps, daily Opus budget, duplicate detection

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   CRON (every 20 min)               │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐ │
│  │Perception│→ │ Triggers │→ │   Action Layer    │ │
│  │  Layer   │  │Processing│  │ (LLM Decision)    │ │
│  └──────────┘  └──────────┘  └───────┬───────────┘ │
│       │                              │              │
│       ▼                              ▼              │
│  ┌──────────┐              ┌───────────────────┐   │
│  │  Task    │              │    Telegram API    │   │
│  │  Router  │              └───────────────────┘   │
│  └──────────┘                                       │
│       │              ┌───────────────────────────┐  │
│       ▼              │     Memory Engine         │  │
│  ┌──────────┐        │  L1: Working (session)    │  │
│  │Calendar  │        │  L2: Episodic (daily md)  │  │
│  │  (gog)   │        │  L3: Semantic (MEMORY.md) │  │
│  └──────────┘        │  L4: Procedural           │  │
│                      └───────────────────────────┘  │
│  ┌──────────────────┐                               │
│  │Initiative Engine │  (autonomous research)        │
│  └──────────────────┘                               │
└─────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Clone
git clone https://github.com/johnpippett/engram.git
cd engram

# 2. Configure
bash setup.sh
# — or manually: cp config.env.example .env && edit .env

# 3. Set up your profile
mkdir -p ~/.openclaw/workspace/.state ~/.openclaw/workspace/memory/distillation-staging
cp templates/*.md ~/.openclaw/workspace/
# Edit ~/.openclaw/workspace/USER.md with your personal info

# 4. Initialize the database
python3 brain.py --init

# 5. Verify
python3 brain.py --status

# 6. Install cron (setup.sh does this interactively)
crontab -e
# Add: */20 * * * * cd /path/to/atlas-brain && python3 brain.py >> .state/cron.log 2>&1

# 7. Register OpenClaw skills
# Copy or symlink atlas-tasks/ and atlas-initiatives/ into your OpenClaw skills directory
```

See [docs/INSTALL.md](docs/INSTALL.md) for the full walkthrough.

> **Note:** Engram was originally built as "Atlas Brain." You'll see references to `atlas-brain`, `atlas-tasks`, and `atlas-initiatives` throughout the codebase — these are internal identifiers, not a separate project.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| ATLAS_BOT_TOKEN | Yes | --- | Telegram bot token from @BotFather |
| ATLAS_CHAT_ID | Yes | --- | Telegram chat ID |
| ATLAS_GATEWAY_URL | Yes | http://127.0.0.1:18789/v1/chat/completions | OpenRouter-compatible LLM endpoint |
| ATLAS_GATEWAY_TOKEN | Yes | --- | API key for LLM endpoint |
| ATLAS_USER_NAME | No | User | Your name (used in prompts) |
| ATLAS_TIMEZONE | No | UTC | IANA timezone |
| ATLAS_MODEL | No | anthropic/claude-sonnet-4-6 | Default LLM model |
| ATLAS_DEEP_MODEL | No | anthropic/claude-opus-4-6 | Model for deep work |
| ATLAS_PARENTING_DAYS | No | 2,4 | Reduced-interruption weekdays (0=Mon) |
| ATLAS_QUIET_START | No | 22 | Quiet hours start (24h) |
| ATLAS_QUIET_END | No | 6 | Quiet hours end (24h) |
| GOG_ACCOUNT | No | --- | Google account for calendar |
| GOG_KEYRING_PASSWORD | No | --- | Keyring password for gog CLI |

## How Memory Works

Atlas uses a four-layer memory system inspired by human memory consolidation:

1. **Working Memory** (SESSION-STATE.md) --- Short-term scratch pad. Cleared after each session distillation.
2. **Episodic Memory** (memory/YYYY-MM-DD.md) --- Daily event logs. Auto-cleaned after 30 days.
3. **Semantic Memory** (MEMORY.md) --- Permanent facts and preferences. Promoted from episodic by nightly distillation with duplicate detection and 8KB size cap.
4. **Procedural Memory** (PROCEDURE.md) --- Behavioral patterns: what works, what doesn't, communication style, timing preferences. Updated by distillation.

Nightly distillation (11 PM) reads the last 3 days of episodic memory, scores entries for confidence, stages them, and auto-promotes high-confidence facts. Low-confidence entries age for 3 days before promotion. Monthly verification surfaces uncertain entries for human review.

## How Initiatives Work

Atlas can pursue self-directed research within guardrails:

- **Creation:** User-requested, user-approved, or auto-created (monitoring category only)
- **Execution:** Time-boxed steps (90s max) run during the main loop when time permits
- **Budget:** Each initiative has a cost cap (default $10) and time cap (default 1 hour)
- **Surfacing:** Findings are formatted and sent via Telegram when ready
- **Safety:** Max 5 steps/day, 10 active initiatives, auto-pause after 2 empty steps, daily Opus call budget (10/day)

## CLI

```bash
python3 brain.py --add "Task title"          # Add a task
python3 brain.py --done <item-id>            # Mark done
python3 brain.py --list                      # List active items
python3 brain.py --status                    # System status
python3 brain.py --trigger                   # Manual trigger
python3 brain.py --morning                   # Send morning briefing
python3 brain.py --nightly                   # Run nightly distillation
python3 brain.py --weekly                    # Run weekly review
python3 brain.py --watchdog                  # Check if brain is alive
python3 brain.py --dry-run                   # Full loop, no messages sent
python3 brain.py --perception-only           # Debug: just run perception
python3 brain.py --init                      # Initialize fresh database
python3 brain.py --migrate                   # Migrate from cognitive-loop.db
python3 brain.py --verify                    # Verify DB integrity
```

## Requirements

- Python 3.9+
- No pip dependencies (stdlib only)
- Telegram bot (via @BotFather)
- OpenRouter-compatible LLM API endpoint
- Optional: gog CLI for Google Calendar integration

## License

MIT License. See [LICENSE](LICENSE).
