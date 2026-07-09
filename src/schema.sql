-- FoodSafe Intelligence — Core schema
-- Unit of analysis: one recall event per row.
-- Cross-source duplicates are linked via fingerprint, not merged.

CREATE TABLE IF NOT EXISTS alerts (
    -- Identity & dedup
    id                       TEXT PRIMARY KEY,
    source_id                TEXT NOT NULL,
    source_record_id         TEXT NOT NULL,
    fingerprint              TEXT NOT NULL,

    -- Provenance
    record_url               TEXT,
    ingestion_date           TEXT NOT NULL,
    source_published_date    TEXT,

    -- Time
    event_initiation_date    TEXT,
    event_status             TEXT,

    -- Geography
    origin_country           TEXT,
    distribution_countries   TEXT,
    israel_relevance_flag    INTEGER DEFAULT 0,

    -- Company / supply chain
    recalling_firm           TEXT,
    brand_names              TEXT,

    -- Product
    product_description      TEXT,
    product_category         TEXT,

    -- Hazard
    hazard_category          TEXT,
    hazard_specific          TEXT,

    -- Severity
    severity_raw             TEXT,
    severity_normalized      TEXT,

    -- Population & impact
    population_at_risk       TEXT,
    illness_count_reported   INTEGER,

    -- Free text
    title                    TEXT,
    description              TEXT,
    reason_for_recall        TEXT,

    -- Notification tracking
    notified_at              TEXT,

    UNIQUE(source_id, source_record_id)
);

CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts(fingerprint);
CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts(source_id);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity_normalized);
CREATE INDEX IF NOT EXISTS idx_alerts_initiation ON alerts(event_initiation_date);
CREATE INDEX IF NOT EXISTS idx_alerts_hazard ON alerts(hazard_category);

-- Tracks ingestion runs for observability and idempotency.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    records_fetched   INTEGER DEFAULT 0,
    records_inserted  INTEGER DEFAULT 0,
    records_updated   INTEGER DEFAULT 0,
    error             TEXT
);

-- Stratified sample membership. Many-to-one link from sample row to alert.
CREATE TABLE IF NOT EXISTS sample_members (
    sample_name      TEXT NOT NULL,
    alert_id         TEXT NOT NULL,
    stratum          TEXT,
    PRIMARY KEY (sample_name, alert_id),
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

-- Pairs selected for LLM labeling.
CREATE TABLE IF NOT EXISTS labeling_pairs (
    pair_id          TEXT PRIMARY KEY,
    sample_name      TEXT NOT NULL,
    alert_a_id       TEXT NOT NULL,
    alert_b_id       TEXT NOT NULL,
    pair_category    TEXT,
    created_at       TEXT NOT NULL,
    FOREIGN KEY (alert_a_id) REFERENCES alerts(id),
    FOREIGN KEY (alert_b_id) REFERENCES alerts(id)
);

-- Raw labels from LLM judge for each (pair, dimension).
CREATE TABLE IF NOT EXISTS llm_labels (
    pair_id            TEXT NOT NULL,
    dimension          TEXT NOT NULL,
    order_variant      TEXT NOT NULL,
    winner             TEXT NOT NULL,
    reasoning          TEXT,
    model              TEXT NOT NULL,
    labeled_at         TEXT NOT NULL,
    PRIMARY KEY (pair_id, dimension, order_variant),
    FOREIGN KEY (pair_id) REFERENCES labeling_pairs(pair_id)
);

-- Expert verification labels (subset of pairs).
CREATE TABLE IF NOT EXISTS expert_labels (
    pair_id            TEXT NOT NULL,
    dimension          TEXT NOT NULL,
    expert_name        TEXT NOT NULL,
    winner             TEXT NOT NULL,
    notes              TEXT,
    labeled_at         TEXT NOT NULL,
    PRIMARY KEY (pair_id, dimension, expert_name),
    FOREIGN KEY (pair_id) REFERENCES labeling_pairs(pair_id)
);

-- Bradley-Terry strength per alert and dimension (from LLM pairwise labels).
CREATE TABLE IF NOT EXISTS bt_scores (
    alert_id              TEXT NOT NULL,
    sample_name           TEXT NOT NULL,
    dimension             TEXT NOT NULL,
    score                 REAL NOT NULL,
    label_model           TEXT NOT NULL,
    n_comparisons_in_fit  INTEGER,
    fitted_at             TEXT NOT NULL,
    PRIMARY KEY (alert_id, sample_name, dimension, label_model),
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

CREATE INDEX IF NOT EXISTS idx_bt_scores_sample ON bt_scores(sample_name, dimension);

-- Train/test split at alert level (step 5 — synthetic pairs for Bi-Encoder).
CREATE TABLE IF NOT EXISTS training_split_members (
    sample_name   TEXT NOT NULL,
    label_model   TEXT NOT NULL,
    alert_id      TEXT NOT NULL,
    split         TEXT NOT NULL CHECK (split IN ('train', 'test')),
    composite     REAL NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (sample_name, label_model, alert_id),
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

-- Synthetic pairs: winner = higher composite BT score (step 5).
CREATE TABLE IF NOT EXISTS synthetic_training_pairs (
    pair_id        TEXT PRIMARY KEY,
    sample_name    TEXT NOT NULL,
    label_model    TEXT NOT NULL,
    split          TEXT NOT NULL CHECK (split IN ('train', 'test')),
    alert_a_id     TEXT NOT NULL,
    alert_b_id     TEXT NOT NULL,
    composite_a    REAL NOT NULL,
    composite_b    REAL NOT NULL,
    composite_diff REAL NOT NULL,
    winner         TEXT NOT NULL CHECK (winner IN ('A', 'B')),
    label          INTEGER NOT NULL CHECK (label IN (0, 1)),
    created_at     TEXT NOT NULL,
    FOREIGN KEY (alert_a_id) REFERENCES alerts(id),
    FOREIGN KEY (alert_b_id) REFERENCES alerts(id)
);

CREATE INDEX IF NOT EXISTS idx_stp_sample_split
    ON synthetic_training_pairs(sample_name, label_model, split);

CREATE INDEX IF NOT EXISTS idx_pairs_sample ON labeling_pairs(sample_name);
CREATE INDEX IF NOT EXISTS idx_pairs_category ON labeling_pairs(pair_category);
