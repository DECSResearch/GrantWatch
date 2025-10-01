CREATE TABLE IF NOT EXISTS grants (
    opp_id TEXT PRIMARY KEY,
    title TEXT,
    stage TEXT CHECK (stage IN ('concept','full')),
    opportunity_status TEXT,
    post_date TIMESTAMP,
    close_date TIMESTAMP,
    archive_date TIMESTAMP,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_grants_stage ON grants(stage);
CREATE INDEX IF NOT EXISTS idx_grants_close_date ON grants(close_date);
CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(opportunity_status);
