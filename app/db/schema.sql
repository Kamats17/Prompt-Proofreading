-- PromptProofreading Sales Agent — SQLite schema
-- Tables: prospects, audits, drafts, dnc_list, events
-- Indexes that depend on columns added by migration live in migrate.py.

CREATE TABLE IF NOT EXISTS prospects (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company             TEXT NOT NULL,
    url                 TEXT NOT NULL UNIQUE,
    canonical_domain    TEXT,
    industry            TEXT,
    city                TEXT,
    state               TEXT,
    email               TEXT,
    email_source        TEXT,
    contact_name        TEXT,
    phone               TEXT,
    source              TEXT NOT NULL CHECK (source IN ('master_csv', 'xlsx', 'node_discovery', 'manual', 'osm')),
    node_rating         TEXT CHECK (node_rating IN ('A', 'B', 'C', 'Reject') OR node_rating IS NULL),
    node_issues_json    TEXT,
    blocked_reason      TEXT,
    audit_attempts      INTEGER NOT NULL DEFAULT 0,
    last_audit_error    TEXT,
    last_audit_error_at TEXT,
    last_contacted_at   TEXT,
    do_not_contact_until TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prospects_url ON prospects(url);
CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
CREATE INDEX IF NOT EXISTS idx_prospects_state ON prospects(state);

CREATE TABLE IF NOT EXISTS audits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id     INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    screenshot_path TEXT,
    html_cache_path TEXT,
    issues_json     TEXT NOT NULL,
    meta_json       TEXT,
    service_angle   TEXT,
    demo_angle      TEXT,
    icp_score       INTEGER,
    node_rating_snapshot TEXT,
    final_grade     TEXT CHECK (final_grade IN ('A', 'B', 'C', 'Reject')),
    model_id        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audits_prospect_id ON audits(prospect_id);
CREATE INDEX IF NOT EXISTS idx_audits_grade ON audits(final_grade);

CREATE TABLE IF NOT EXISTS drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id       INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    audit_id          INTEGER NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
    touch             INTEGER NOT NULL DEFAULT 1,
    parent_draft_id   INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    subject           TEXT NOT NULL,
    subject_variant   TEXT,
    finding_type      TEXT,
    evidence_json     TEXT,
    body_text         TEXT NOT NULL,
    body_html         TEXT NOT NULL,
    gmail_draft_id    TEXT,
    gmail_thread_id   TEXT,
    rfc_message_id    TEXT,
    qa_pass           INTEGER NOT NULL DEFAULT 0,
    qa_blockers_json  TEXT,
    human_edited      INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'pushed', 'error', 'sent', 'replied', 'opted_out', 'bounced')),
    reject_reason     TEXT,
    reject_category   TEXT,
    sent_at           TEXT,
    replied_at        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_prospect_id ON drafts(prospect_id);
CREATE INDEX IF NOT EXISTS idx_drafts_audit_id ON drafts(audit_id);

CREATE TABLE IF NOT EXISTS dnc_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT,
    domain      TEXT,
    email       TEXT,
    reason      TEXT,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    added_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_dnc_domain ON dnc_list(domain);
CREATE INDEX IF NOT EXISTS idx_dnc_email ON dnc_list(email);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id  INTEGER REFERENCES prospects(id) ON DELETE SET NULL,
    draft_id     INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_prospect_id ON events(prospect_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
