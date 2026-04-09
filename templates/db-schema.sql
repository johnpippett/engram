CREATE TABLE atlas_initiatives (
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
, scheduled_after TEXT, proposal_reason TEXT, opportunity_score REAL DEFAULT 0.0, auto_approved INTEGER DEFAULT 0, proposed_at TEXT, approved_at TEXT, source_pattern TEXT);

CREATE TABLE computed_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,
    rule_value TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE daily_metrics (
    date TEXT PRIMARY KEY,
    nudges_sent INTEGER DEFAULT 0,
    checkins_sent INTEGER DEFAULT 0,
    items_completed INTEGER DEFAULT 0,
    items_created INTEGER DEFAULT 0,
    response_rate REAL,
    best_response_hour INTEGER,
    worst_response_hour INTEGER
, actions_taken TEXT, initiatives_worked INTEGER DEFAULT 0, api_failures INTEGER DEFAULT 0, total_loop_runs INTEGER DEFAULT 0);

CREATE TABLE episodic_memory (id INTEGER PRIMARY KEY, content TEXT, source TEXT, created_at TEXT);

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
    updated_at TEXT DEFAULT (datetime('now'))
, estimated_minutes INTEGER, actual_minutes INTEGER, assigned_date TEXT, energy_window TEXT, source TEXT DEFAULT 'manual', task_type TEXT, time_window TEXT, trigger_type TEXT, context_tags TEXT);

CREATE TABLE user_model (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    sample_count INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    insight TEXT NOT NULL,
    metric_type TEXT,
    metric_value TEXT
, source TEXT DEFAULT 'observation', promoted INTEGER DEFAULT 0);

CREATE TABLE mechanical_reminders (
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

CREATE TABLE memory_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    layer           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT,
    related_item_id TEXT,
    timestamp       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE nudge_ab_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id      TEXT NOT NULL,
    variant_group   TEXT NOT NULL,
    total_sent      INTEGER DEFAULT 0,
    hits            INTEGER DEFAULT 0,
    misses          INTEGER DEFAULT 0,
    pending         INTEGER DEFAULT 0,
    hit_rate        REAL DEFAULT 0.0,
    avg_latency_min REAL,
    avg_engagement  REAL DEFAULT 0.0,
    alpha           REAL DEFAULT 1.0,
    beta_param      REAL DEFAULT 1.0,
    computed_at     TEXT,
    UNIQUE(variant_id, variant_group)
);

CREATE TABLE nudge_ab_test (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- The variant being tested
    variant_id TEXT NOT NULL,           -- e.g. 'casual-emoji', 'direct-cta', 'question-hook'
    variant_group TEXT NOT NULL,        -- what dimension: 'phrasing', 'timing', 'format', 'length'
    
    -- The nudge content
    nudge_log_id INTEGER REFERENCES nudge_log(id),
    action_type TEXT NOT NULL,          -- NUDGE, CHECKIN, SURFACE, MECHANICAL, SHARE, REFLECT
    message_text TEXT NOT NULL,
    message_length INTEGER,             -- char count
    has_emoji INTEGER DEFAULT 0,
    emoji_count INTEGER DEFAULT 0,
    has_question INTEGER DEFAULT 0,     -- ends in ?
    has_urgency INTEGER DEFAULT 0,      -- words like 'overdue', 'due today', 'deadline'
    tone TEXT,                          -- 'casual', 'direct', 'playful', 'informational'
    
    -- Timing context
    sent_at TEXT NOT NULL,
    hour_sent INTEGER,                  -- 0-23
    day_of_week INTEGER,                -- 0=Mon, 6=Sun
    is_daycare_day INTEGER DEFAULT 0,   -- Mon/Tue/Thu
    energy_window TEXT,                 -- 'peak', 'fine', 'lazy', 'couch', 'off'
    nudges_sent_today INTEGER DEFAULT 0,
    minutes_since_last_interaction INTEGER,
    
    -- Response tracking (updated later)
    -- HIT = response within 30 min
    -- MISS = no response in 2 hours
    -- PENDING = still waiting
    outcome TEXT DEFAULT 'PENDING',     -- 'HIT', 'MISS', 'PENDING'
    response_at TEXT,                   -- when the user replied (if hit)
    response_latency_min REAL,          -- minutes to response
    response_text TEXT,                 -- first 200 chars of the user's reply
    
    -- Scoring
    engagement_score REAL DEFAULT 0.0,  -- 0-1: 0=miss, 0.5=ack, 1.0=full response
    
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE nudge_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nudge_id        INTEGER NOT NULL,
    nudge_timestamp TEXT NOT NULL,
    nudge_action    TEXT NOT NULL,
    nudge_category  TEXT,
    hour_sent       INTEGER NOT NULL,
    day_of_week     INTEGER NOT NULL,
    is_workday      INTEGER NOT NULL DEFAULT 1,
    is_daycare_day  INTEGER NOT NULL DEFAULT 0,
    response_detected   INTEGER DEFAULT 0,
    response_timestamp  TEXT,
    response_latency_s  INTEGER,
    response_type       TEXT,
    response_sentiment  TEXT,
    engagement_score    REAL,
    energy_window       TEXT,
    nudges_today_before INTEGER DEFAULT 0,
    minutes_since_last_nudge INTEGER,
    user_active_before  INTEGER DEFAULT 0,
    was_ignored         INTEGER DEFAULT 0,
    was_dismissed       INTEGER DEFAULT 0,
    was_actioned        INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (nudge_id) REFERENCES nudge_log(id)
);

CREATE TABLE nudge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT REFERENCES items(id),
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    message TEXT,
    thread TEXT,
    response TEXT,
    reasoning TEXT,
    forced INTEGER DEFAULT 0
);

CREATE TABLE nudge_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT NOT NULL,
    raw_message     TEXT NOT NULL,
    reasoning       TEXT,
    item_id         TEXT,
    urgency         TEXT DEFAULT 'low',
    created_at      TEXT DEFAULT (datetime('now')),
    delivered_at    TEXT,
    deferred_until  TEXT,
    expires_at      TEXT,
    nudge_log_id    INTEGER
);

CREATE TABLE nudge_variant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    variant_group TEXT NOT NULL,
    description TEXT,
    
    -- Rules (JSON arrays of keywords/patterns)
    keyword_signals TEXT,               -- JSON array of keywords that trigger this variant
    pattern_signals TEXT,               -- JSON array of regex patterns
    
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE perception_log (
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

CREATE TABLE research_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    context TEXT,
    source_session TEXT,
    source_message TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    completed_at DATETIME,
    findings TEXT,
    surfaced_at DATETIME,
    tags TEXT
);

CREATE TABLE scout_analytics_findings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at  TEXT NOT NULL DEFAULT (datetime('now')),
            anomaly_type TEXT NOT NULL,
            severity     TEXT NOT NULL,  -- 'info' | 'warn' | 'alert'
            description  TEXT NOT NULL,
            value        REAL,
            baseline     REAL,
            delta_pct    REAL,
            notified     INTEGER DEFAULT 0
        );

CREATE TABLE scout_analytics_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL DEFAULT (datetime('now')),
            snapshot    TEXT NOT NULL  -- JSON blob
        );

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE task_durations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,
    keywords        TEXT,
    estimated_minutes INTEGER NOT NULL,
    actual_minutes  INTEGER,
    sample_count    INTEGER DEFAULT 1,
    confidence      REAL DEFAULT 0.5,
    updated_at      TEXT DEFAULT (datetime('now'))
, correction_factor REAL DEFAULT 1.0);

CREATE TABLE triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    item_id TEXT,
    response_type TEXT,
    user_said TEXT,
    inferred_schedule TEXT,
    processed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

