---
name: atlas-initiatives
description: >
  Manage Atlas's autonomous initiative engine — list active/pending initiatives,
  create new initiatives (with approval flow), approve or reject proposals, view
  findings, pause/resume, and execute initiative steps. Use when the user asks about
  initiatives, research projects, autonomous work, or when surfacing initiative
  findings in conversation. Also use proactively when initiative findings are
  ready and the conversation context is appropriate.
---

# Atlas Initiatives

Manage autonomous initiatives in the Atlas Brain database via `atlas-initiatives.py`.

## Commands

```bash
# List initiatives
atlas-initiatives.py list_active          # active initiatives with cost/time info
atlas-initiatives.py list_pending         # waiting for the user's approval
atlas-initiatives.py list_all             # all non-completed (active + paused + pending)

# View details
atlas-initiatives.py view 42             # full details + findings for initiative #42

# Create (non-monitoring categories require approval)
atlas-initiatives.py create "Research X" --desc "Investigate X" --category research \
    --cost-cap 5.0 --time-cap 3600 --done-condition "Summary document produced"

# Approval flow
atlas-initiatives.py approve 42          # approve pending -> active
atlas-initiatives.py reject 42           # reject pending -> cancelled

# Lifecycle
atlas-initiatives.py pause 42 --reason "Deprioritized"
atlas-initiatives.py resume 42

# Execute a step (for heartbeat/idle use)
atlas-initiatives.py step --time-budget 90
```

## When to use each command

| The user says... | Action |
|---|---|
| "What initiatives are running?" / "Any research going?" | `list_active` |
| "Any initiatives waiting for me?" | `list_pending` |
| "Show me what you found on [X]" | `view <id>` — relay findings conversationally |
| "Research [X] for me" / "Look into [X]" | `create` with appropriate params |
| "Yeah go ahead with that" (re: pending initiative) | `approve <id>` |
| "Nah drop that one" / "Don't bother" | `reject <id>` |
| "Pause the [X] initiative" | `pause <id>` |
| "Pick that back up" / "Resume [X]" | `resume <id>` |

## Surfacing findings

When an initiative has findings ready (`surface_ready` flag or substantial findings text),
mention them naturally in conversation when context is appropriate. Don't push-notify —
weave findings into the flow when the user is chatting about a related topic, or mention
them during a work session check-in.

Example: "By the way — that research initiative on X turned up something interesting:
[key finding]. Want me to pull up the full details?"

## Guardrails (preserved from brain.py)

- Max 5 initiative steps per day
- Per-initiative cost cap (default $10)
- Per-initiative time cap (default 60 min)
- Auto-pause after 2 consecutive empty steps
- Max 10 active initiatives at once
- Non-monitoring auto-creates require the user's approval
- `--done-condition` is mandatory on create

## Categories

Common categories: `research`, `monitoring`, `brain`, `product`, `meta`

Only `monitoring` initiatives can be auto-created without approval.
