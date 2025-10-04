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

CREATE TABLE IF NOT EXISTS grant_subscriptions (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    field TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_email_field
    ON grant_subscriptions (email, field);

CREATE INDEX IF NOT EXISTS idx_subscriptions_field
    ON grant_subscriptions (field);
