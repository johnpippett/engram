---
name: atlas-tasks
description: >
  Manage the user's task list — list active/today/overdue tasks, add new tasks,
  mark tasks done or blocked, snooze tasks, and filter by category. Use when
  the user asks about tasks, todos, what to work on, or mentions completing/blocking/
  deferring something. Also use proactively when task context would help the
  conversation.
---

# Atlas Tasks

Manage tasks in the Atlas Brain database via the `atlas-tasks.py` CLI.

## Commands

```bash
# List tasks
atlas-tasks.py list_active          # all non-done/cancelled, priority-ordered
atlas-tasks.py list_today           # assigned today or due today
atlas-tasks.py list_overdue         # past due date
atlas-tasks.py category work        # active tasks in a category

# Add a task
atlas-tasks.py add "Buy groceries"
atlas-tasks.py add "Fix VPN" --due 2026-04-15 --priority high --energy low --category infra --notes "Need creds"

# Update tasks
atlas-tasks.py done buy-groceries
atlas-tasks.py blocked fix-vpn --reason "Waiting on credentials"
atlas-tasks.py snooze write-report --date 2026-04-10
```

## When to use each command

| The user says... | Action |
|---|---|
| "What's on my plate?" / "What should I work on?" | `list_today`, then `list_overdue` if few results |
| "What tasks do I have?" / "Show me everything" | `list_active` |
| "I finished [X]" / "[X] is done" | `done <id>` |
| "I'm stuck on [X]" / "[X] is blocked" | `blocked <id> --reason "..."` |
| "Remind me about [X] next week" / "Push [X] to Friday" | `snooze <id> --date YYYY-MM-DD` |
| "Add a task: [title]" / "I need to [do something]" | `add "title" [--flags]` |
| "What work stuff do I have?" | `category work` |

## Output format

Each task prints as: `[slug-id] Title  |  priority  |  due: DATE  |  status`

Relay results conversationally — don't just dump the raw output. Example:
- Raw: `[fix-vpn] Fix VPN  |  high  |  due: 2026-03-30  |  blocked: waiting on credentials`
- Say: "Your VPN fix is still blocked — waiting on credentials. That was due yesterday."

## Task IDs

Tasks use slug IDs (e.g., `buy-groceries`, `fix-vpn-config`). When the user refers to a task by name, match it to the closest slug. If ambiguous, run `list_active` and pick the best match.

## Priority and energy levels

- **Priority:** critical, high, medium, low
- **Energy:** high, medium, low (matches the user's energy windows — high-energy tasks for peak hours)
