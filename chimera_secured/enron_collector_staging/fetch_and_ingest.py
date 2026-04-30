#!/usr/bin/env python3
"""
Enron Email Corpus → Railway Postgres ingester.

Streams the CMU canonical tarball (~423 MB) directly from HTTPS into memory,
parses maildir entries on the fly, and inserts each mailbox's sent folders
into Postgres. Never writes the tarball or extracted maildir to disk.

Design choices:
  * Wide net, not pre-selection. We ingest ALL mailboxes' sent folders so
    writer selection can be a post-ingest SQL query instead of a hardcoded
    list we'd have to update. Per-writer cap (default 500) keeps the total
    size manageable for Railway hobby-tier Postgres.
  * Idempotent-ish. Re-runs overwrite via message_id natural key where present;
    emails without Message-ID are inserted fresh each run (rare in Enron).
  * Streaming only. Peak memory: one email at a time, plus a small batch for
    INSERTs. Works on a 512MB Railway container.

Environment:
    DATABASE_URL       Postgres connection string (auto-set by Railway addon)
    ENRON_TAR_URL      Override source URL (default: CMU canonical)
    PER_WRITER_CAP     Max sent emails per mailbox (default: 500)
    TRUNCATE_FIRST     If "true", TRUNCATE emails table before ingest
    LOG_EVERY          Progress log cadence (default: 500 emails)

Run:
    python fetch_and_ingest.py
"""
from __future__ import annotations

import email
import os
import sys
import tarfile
import time
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

TAR_URL = os.environ.get(
    "ENRON_TAR_URL",
    "https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz",
)
DATABASE_URL = os.environ["DATABASE_URL"]
PER_WRITER_CAP = int(os.environ.get("PER_WRITER_CAP", "500"))
TRUNCATE_FIRST = os.environ.get("TRUNCATE_FIRST", "false").lower() == "true"
LOG_EVERY = int(os.environ.get("LOG_EVERY", "500"))
BATCH_SIZE = 200

SENT_FOLDER_NAMES = {"sent", "sent_items", "_sent_mail", "sent items"}


# ---------- helpers ----------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_maildir_path(name: str) -> tuple[Optional[str], Optional[str]]:
    """
    Maildir entries look like:
        maildir/allen-p/sent/1.
        maildir/allen-p/_sent_mail/10.
    Returns (owner, folder) or (None, None) if not a maildir message.
    """
    parts = name.split("/")
    if len(parts) < 4 or parts[0] != "maildir":
        return None, None
    return parts[1], parts[2].lower().strip()


def extract_body(msg: Message) -> str:
    """Walk MIME parts, return first text/plain payload. Enron is almost all plaintext."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace",
                    )
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True)
        if payload is None:
            return msg.get_payload() or ""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return msg.get_payload() or ""


def split_addrs(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        return [addr for _, addr in getaddresses([raw]) if addr]
    except Exception:
        return []


def first_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        parsed = getaddresses([raw])
        if parsed and parsed[0][0]:
            return parsed[0][0]
    except Exception:
        pass
    return None


def parse_date(raw: Optional[str]):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        return None


def build_row(owner: str, folder: str, raw_bytes: bytes) -> Optional[dict]:
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return None
    body_text = extract_body(msg)
    body_raw = raw_bytes.decode("utf-8", errors="replace")
    from_raw = msg.get("From", "")
    from_addrs = split_addrs(from_raw)
    return {
        "mailbox_owner": owner,
        "folder": folder,
        "message_id": msg.get("Message-ID"),
        "date": parse_date(msg.get("Date")),
        "from_name": first_name(from_raw),
        "from_addr": from_addrs[0] if from_addrs else None,
        "to_addrs": split_addrs(msg.get("To")),
        "cc_addrs": split_addrs(msg.get("Cc")),
        "subject": msg.get("Subject"),
        "body_text": body_text,
        "body_raw": body_raw,
        "in_reply_to": msg.get("In-Reply-To"),
        "references_ids": split_addrs(msg.get("References")),
        "char_count": len(body_text),
        "word_count": len(body_text.split()),
    }


INSERT_SQL = """
INSERT INTO emails (
    mailbox_owner, folder, message_id, date, from_name, from_addr,
    to_addrs, cc_addrs, subject, body_text, body_raw,
    in_reply_to, references_ids, char_count, word_count
) VALUES (
    %(mailbox_owner)s, %(folder)s, %(message_id)s, %(date)s,
    %(from_name)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s,
    %(subject)s, %(body_text)s, %(body_raw)s,
    %(in_reply_to)s, %(references_ids)s, %(char_count)s, %(word_count)s
)
"""


# ---------- main ----------

def main() -> int:
    log(f"Connecting to Postgres...")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    log("Applying schema...")
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_path) as f:
        cur.execute(f.read())
    conn.commit()

    if TRUNCATE_FIRST:
        log("TRUNCATE_FIRST=true -> clearing existing rows")
        cur.execute("TRUNCATE TABLE emails RESTART IDENTITY")
        conn.commit()

    log(f"Streaming tarball from {TAR_URL}")
    per_writer: dict[str, int] = {}
    batch: list[dict] = []
    ingested = 0
    skipped_cap = 0
    started = time.time()

    with requests.get(TAR_URL, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        # Keep gzipped bytes intact; tarfile handles the gzip layer via mode="r|gz".
        resp.raw.decode_content = False
        with tarfile.open(fileobj=resp.raw, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                owner, folder = parse_maildir_path(member.name)
                if owner is None or folder not in SENT_FOLDER_NAMES:
                    continue
                if per_writer.get(owner, 0) >= PER_WRITER_CAP:
                    skipped_cap += 1
                    continue
                try:
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        continue
                    raw = fobj.read()
                except Exception:
                    continue

                row = build_row(owner, folder, raw)
                if row is None:
                    continue
                batch.append(row)
                per_writer[owner] = per_writer.get(owner, 0) + 1
                ingested += 1

                if len(batch) >= BATCH_SIZE:
                    psycopg2.extras.execute_batch(cur, INSERT_SQL, batch)
                    conn.commit()
                    batch.clear()

                if ingested % LOG_EVERY == 0:
                    elapsed = time.time() - started
                    rate = ingested / max(elapsed, 0.01)
                    log(f"  ingested={ingested} writers={len(per_writer)} rate={rate:.1f}/s")

    if batch:
        psycopg2.extras.execute_batch(cur, INSERT_SQL, batch)
        conn.commit()

    elapsed = time.time() - started
    log("")
    log(f"Done. ingested={ingested} skipped_by_cap={skipped_cap} writers={len(per_writer)} time={elapsed:.1f}s")
    log("Top 20 writers by sent count:")
    for owner, cnt in sorted(per_writer.items(), key=lambda kv: -kv[1])[:20]:
        log(f"  {owner:30s} {cnt:>5d}")

    log("")
    log("Eligible writers (>=200 sent) per writer_stats view:")
    cur.execute("SELECT COUNT(*) FROM writer_stats WHERE sent_count >= 200")
    eligible = cur.fetchone()[0]
    log(f"  eligible_writers={eligible}")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
