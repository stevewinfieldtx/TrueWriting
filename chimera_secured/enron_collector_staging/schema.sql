-- Enron Email Corpus — Chimera Secured eval store
-- Target: Railway Postgres (or any Postgres 13+)

CREATE TABLE IF NOT EXISTS emails (
    id              BIGSERIAL PRIMARY KEY,
    mailbox_owner   TEXT NOT NULL,              -- e.g. "allen-p", "lay-k"
    folder          TEXT NOT NULL,              -- "sent", "sent_items", "_sent_mail"
    message_id      TEXT,
    date            TIMESTAMPTZ,
    from_name       TEXT,
    from_addr       TEXT,
    to_addrs        TEXT[],
    cc_addrs        TEXT[],
    subject         TEXT,
    body_text       TEXT,                       -- decoded plain-text payload
    body_raw        TEXT,                       -- original bytes (as UTF-8 with replace)
    in_reply_to     TEXT,
    references_ids  TEXT[],
    char_count      INT,
    word_count      INT,
    ingested_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_emails_owner         ON emails(mailbox_owner);
CREATE INDEX IF NOT EXISTS idx_emails_owner_folder  ON emails(mailbox_owner, folder);
CREATE INDEX IF NOT EXISTS idx_emails_date          ON emails(date);
CREATE INDEX IF NOT EXISTS idx_emails_message_id    ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_emails_from_addr     ON emails(from_addr);

-- Convenience view: writers eligible for CPP training.
-- Used by the eval pipeline to select 30–50 target writers post-ingest.
CREATE OR REPLACE VIEW writer_stats AS
SELECT
    mailbox_owner,
    COUNT(*)                        AS sent_count,
    MIN(date)                       AS first_sent,
    MAX(date)                       AS last_sent,
    ROUND(AVG(word_count)::numeric, 1)  AS avg_words,
    ROUND(AVG(char_count)::numeric, 1)  AS avg_chars,
    ROUND(STDDEV(word_count)::numeric, 1) AS stddev_words,
    COUNT(DISTINCT from_addr)       AS distinct_from_addrs
FROM emails
WHERE folder IN ('sent', 'sent_items', '_sent_mail', 'sent items')
GROUP BY mailbox_owner;

-- Attacker corpora land here later (Wave 2+ eval): generated emails tagged with
-- target writer, tier, generator model. Kept in the same DB for easy joins.
CREATE TABLE IF NOT EXISTS attacker_emails (
    id              BIGSERIAL PRIMARY KEY,
    target_owner    TEXT NOT NULL,              -- the writer being impersonated
    tier            TEXT NOT NULL,              -- "zero_shot", "few_shot", "high_fidelity", "rag", "multi_llm"
    generator_model TEXT,                       -- e.g. "anthropic/claude-sonnet-4.6"
    prompt_hash     TEXT,                       -- hash of the generation prompt
    subject         TEXT,
    body_text       TEXT NOT NULL,
    word_count      INT,
    generated_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_attacker_target_tier ON attacker_emails(target_owner, tier);

-- Per-run eval results: one row per (writer, tier, model_version).
CREATE TABLE IF NOT EXISTS eval_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,              -- groups all rows from one eval run
    model_version   TEXT NOT NULL,              -- e.g. "chimera-secured/2026.04.1"
    target_owner    TEXT NOT NULL,
    tier            TEXT NOT NULL,
    auc             REAL,
    catch_at_2_fpr  REAL,
    catch_at_5_fpr  REAL,
    false_flag_rate REAL,
    n_real          INT,
    n_attacker      INT,
    extra_json      JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_run_id ON eval_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_version ON eval_runs(model_version);
