# Enron Email Corpus Collector

One-shot Railway job that streams the canonical CMU Enron corpus directly into Railway Postgres. Your laptop never touches the 423 MB tarball.

## What it does

1. Opens an HTTPS stream to `https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz`
2. Walks the tarball member-by-member without extracting to disk
3. For each `maildir/<owner>/sent*/` message: parses MIME, extracts body, inserts into Postgres
4. Applies a per-writer cap (default 500 emails) so storage stays Railway-hobby-tier friendly
5. Reports eligible writers (`sent_count >= 200`) at the end for downstream CPP selection

Peak container memory: ~200–400 MB. Total DB footprint after run: ~150–300 MB depending on cap.

## Setup on Railway

1. Create a new Railway project, connect this GitHub repo
2. Add the **Postgres** plugin — `DATABASE_URL` is injected automatically
3. The service will build via NIXPACKS (no Dockerfile needed); Railway picks up `requirements.txt`
4. Railway runs `python fetch_and_ingest.py` per `railway.json`'s `startCommand`
5. `restartPolicyType: NEVER` keeps it as a one-shot — it runs once per deploy and exits

## Environment variables

| Variable         | Default                                                  | Purpose                                            |
|------------------|----------------------------------------------------------|----------------------------------------------------|
| `DATABASE_URL`   | *(auto-set by Railway Postgres plugin)*                  | Postgres connection                                |
| `ENRON_TAR_URL`  | CMU canonical URL                                        | Override for mirrors or custom tarballs            |
| `PER_WRITER_CAP` | `500`                                                    | Max sent emails ingested per mailbox               |
| `TRUNCATE_FIRST` | `false`                                                  | Wipe `emails` table before ingesting (for re-runs) |
| `LOG_EVERY`      | `500`                                                    | Progress log cadence                               |

## What lands in Postgres

Three tables (see `schema.sql`):

- **`emails`** — one row per ingested sent-folder email. Columns: `mailbox_owner`, `folder`, `message_id`, `date`, `from_name`, `from_addr`, `to_addrs[]`, `cc_addrs[]`, `subject`, `body_text`, `body_raw`, `in_reply_to`, `references_ids[]`, `char_count`, `word_count`.
- **`attacker_emails`** — populated by Wave 2+ eval swarm, not by this collector. Included here so the schema is one file.
- **`eval_runs`** — per-writer eval results, populated by the eval swarm. Same reason.

Plus a view:

- **`writer_stats`** — `mailbox_owner`, `sent_count`, `first_sent`, `last_sent`, `avg_words`, `avg_chars`, `stddev_words`, `distinct_from_addrs`. This is what the eval pipeline queries to pick the 30–50 target writers.

## Selecting target writers after ingest

```sql
-- Candidates: writers with enough volume AND stylistic consistency
SELECT mailbox_owner, sent_count, avg_words, stddev_words
FROM writer_stats
WHERE sent_count >= 200
  AND sent_count <= 500            -- adjust or remove; 500 is the cap
  AND avg_words >= 15              -- skip writers who mostly forward
ORDER BY sent_count DESC;
```

For the 40-writer eval set, we want diversity across:
- **Role** (exec / legal / trader / operations) — join against a role lookup or sample manually
- **Volume** (mix of 200-count and 500-count writers to test volume sensitivity)
- **Consistency** (mix of low-`stddev_words` and high-`stddev_words` writers)

The first eval run should pick writers at random from the eligible set, then subsequent runs can refine toward diversity targets.

## Running locally (optional, for debugging)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://localhost/enron_eval
createdb enron_eval
python fetch_and_ingest.py
```

Streaming still works over local HTTPS, so you don't need the tarball on disk for local runs either — but Railway is the intended home.

## Staging note

This folder was staged inside `Chimera_Secured/enron_collector/` during planning. For production it should live in its own Railway-connected GitHub repo (the one Steve already created). Move with:

```bash
# from Chimera_Secured
git rm -r enron_collector
# then copy the files into the separate collector repo and push
```
