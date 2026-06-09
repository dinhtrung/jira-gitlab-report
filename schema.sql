-- schema.sql
-- Deterministic Sprint Reporting Engine — Database Schema

CREATE TABLE IF NOT EXISTS raw_jira_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    field TEXT NOT NULL,
    from_value TEXT,
    to_value TEXT,
    timestamp TEXT NOT NULL,
    author TEXT,
    sprint_name TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_raw_jira_issue_key ON raw_jira_events(issue_key);
CREATE INDEX IF NOT EXISTS idx_raw_jira_timestamp ON raw_jira_events(timestamp);

CREATE TABLE IF NOT EXISTS raw_gitlab_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mr_iid INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    timestamp TEXT NOT NULL,
    author TEXT,
    milestone TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_raw_gitlab_mr ON raw_gitlab_events(mr_iid);
CREATE INDEX IF NOT EXISTS idx_raw_gitlab_timestamp ON raw_gitlab_events(timestamp);

-- Normalized timeline — one row per meaningful state transition per ticket
CREATE TABLE IF NOT EXISTS ticket_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,          -- e.g. "PROJ-123" or "!45"
    source TEXT NOT NULL CHECK(source IN ('jira', 'gitlab')),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'created',
        'status_change',
        'reopened',
        'returned_to_progress',
        'code_review_approved',
        'mr_merged',
        'points_assigned'
    )),
    old_status TEXT,
    new_status TEXT,
    points REAL DEFAULT 0,
    sprint_name TEXT,
    author TEXT,
    event_ts TEXT NOT NULL,           -- ISO-8601 timestamp
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_history_ticket ON ticket_history(ticket_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_history_sprint ON ticket_history(sprint_name);

-- Reconciliation results (cached so re-runs are deterministic)
CREATE TABLE IF NOT EXISTS sprint_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    source TEXT NOT NULL,
    final_status TEXT NOT NULL,
    awarded_points REAL DEFAULT 0,
    flag TEXT,                        -- 'in-flight', 'reopened', 'valid', etc.
    generated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(sprint_name, ticket_id, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_report_sprint ON sprint_report(sprint_name);
