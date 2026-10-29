CREATE TABLE IF NOT EXISTS grants (
    opp_id TEXT PRIMARY KEY,
    title TEXT,
    stage TEXT CHECK (stage IN ('concept','full')),
    opportunity_status TEXT,
    opportunity_category TEXT,
    funding_categories TEXT,
    post_date TIMESTAMP,
    close_date TIMESTAMP,
    archive_date TIMESTAMP,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_grants_stage ON grants(stage);
CREATE INDEX IF NOT EXISTS idx_grants_close_date ON grants(close_date);
CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(opportunity_status);

ALTER TABLE grants
    ADD COLUMN IF NOT EXISTS opportunity_category TEXT;

ALTER TABLE grants
    ADD COLUMN IF NOT EXISTS funding_categories TEXT;

-- Relevance scoring, provenance, and the fields the UI and digest show.
ALTER TABLE grants ADD COLUMN IF NOT EXISTS agency TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS agency_code TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS matched_keywords TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS award_ceiling NUMERIC;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS estimated_total_funding NUMERIC;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS relevance_score SMALLINT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS relevance_reason TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS relevance_model TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS relevance_profile TEXT;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS relevance_scored_at TIMESTAMP;
ALTER TABLE grants ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP NOT NULL DEFAULT NOW();
ALTER TABLE grants ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_grants_relevance_score ON grants(relevance_score);
CREATE INDEX IF NOT EXISTS idx_grants_relevance_profile ON grants(relevance_profile);

-- Grants the group has decided to pursue; their deadlines are always surfaced.
CREATE TABLE IF NOT EXISTS grant_watchlist (
    opp_id TEXT PRIMARY KEY,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- People who get the email. The team is managed with `python main.py
-- recipients`; GRANTS_NOTIFY_RECIPIENTS is merged in without undoing an
-- unsubscribe.
CREATE TABLE IF NOT EXISTS recipients (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    min_score SMALLINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    unsubscribe_token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    unsubscribed_at TIMESTAMP,
    login_sent_at TIMESTAMP
);

-- One row per thing a recipient has been told, written only after the email
-- went out. kind is new, reminder (threshold = days before the deadline),
-- changed, removed, or overview (opp_id '' and ref_date = the day it went out).
-- ref_date is the deadline the notice was about.
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    opp_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    threshold INTEGER NOT NULL DEFAULT -1,
    ref_date DATE,
    score SMALLINT,
    sent_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- A rerun can never record (or send) the same notice twice. Deadline changes
-- are exempt: a date can move back and forth.
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_once
    ON notifications (recipient_id, opp_id, kind, threshold, COALESCE(ref_date, DATE '1900-01-01'))
    WHERE kind <> 'changed';
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications (recipient_id);

-- Lease that keeps two schedulers from sending at the same time. A row lock
-- rather than an advisory lock, because poolers (Neon, PgBouncer) do not keep
-- session-level advisory locks.
CREATE TABLE IF NOT EXISTS notify_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    locked_until TIMESTAMP NOT NULL
);
