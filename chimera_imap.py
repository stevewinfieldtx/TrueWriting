"""
Chimera Secured - IMAP Fallback for Quick Testing
===================================================

Zero Azure. Zero OAuth. Zero app registration.
For when you just want to pull your damn sent mail and get moving.

One-time setup (2 minutes):
1. Go to https://account.live.com/proofs/AppPassword
2. Generate an app password for "Chimera Secured"
3. Copy it (16 characters, looks like: abcdefghijklmnop)
4. Run:
     $env:CHIMERA_EMAIL = "you@hotmail.com"
     $env:CHIMERA_APP_PASSWORD = "abcdefghijklmnop"
     python chimera_imap.py --pull 2000

That's it. Writes corpus_sent.json in the same format chimera_eval.py expects.

Dependencies: just Python stdlib.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Dict, Any

IMAP_HOST = "imap-mail.outlook.com"
IMAP_PORT = 993
SENT_FOLDER_CANDIDATES = ["Sent", "Sent Items", "[Gmail]/Sent Mail", "INBOX.Sent"]
CORPUS_PATH = Path("corpus_sent.json")


# ---------- HTML -> text ----------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._skip > 0:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li"):
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip == 0:
            self._buf.append(data)

    def get_text(self) -> str:
        raw = "".join(self._buf)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    p = _HTMLStripper()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", "", html)
    return p.get_text()


# ---------- Email body extraction ----------

def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return value


def _extract_body(msg: email.message.Message) -> str:
    plain = None
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
                if msg.get_content_type() == "text/html":
                    html = text
                else:
                    plain = text
        except Exception:
            pass

    if plain:
        return plain.strip()
    if html:
        return html_to_text(html)
    return ""


# ---------- Strip quoted replies (basic) ----------

def _strip_quoted(body: str) -> str:
    lines = body.splitlines()
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^On .+(wrote|sent):$", s):
            break
        if re.match(r"^-+\s*Original Message\s*-+", s, re.IGNORECASE):
            break
        if re.match(r"^From:\s", s) and len(out) > 2:
            break
        out.append(line)
    return "\n".join(out).strip()


# ---------- Pull ----------

def pull_sent(limit: int) -> None:
    user = os.environ.get("CHIMERA_EMAIL")
    pw = os.environ.get("CHIMERA_APP_PASSWORD")
    if not user or not pw:
        sys.exit(
            "Set CHIMERA_EMAIL and CHIMERA_APP_PASSWORD env vars.\n"
            "Generate app password at: https://account.live.com/proofs/AppPassword"
        )

    print(f"Connecting to {IMAP_HOST}...")
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        M.login(user, pw)
    except imaplib.IMAP4.error as e:
        sys.exit(f"Login failed: {e}\n\nMake sure you used an APP PASSWORD, not your regular account password.")
    print("Logged in.")

    # Find the sent folder
    typ, folders = M.list()
    folder_names = []
    if typ == "OK":
        for f in folders:
            try:
                name = f.decode().split(' "/" ')[-1].strip('"')
                folder_names.append(name)
            except Exception:
                pass

    sent_folder = None
    for cand in SENT_FOLDER_CANDIDATES:
        if cand in folder_names:
            sent_folder = cand
            break
    if not sent_folder:
        print("Available folders:", folder_names)
        sys.exit("Couldn't find a Sent folder. Edit SENT_FOLDER_CANDIDATES at top of this file.")

    print(f"Selecting folder: {sent_folder}")
    typ, _ = M.select(f'"{sent_folder}"', readonly=True)
    if typ != "OK":
        sys.exit(f"Couldn't select {sent_folder}")

    typ, data = M.search(None, "ALL")
    if typ != "OK":
        sys.exit("Search failed")
    ids = data[0].split()
    print(f"Found {len(ids)} messages in {sent_folder}.")

    # Walk newest first
    ids = ids[::-1]
    out: List[Dict[str, Any]] = []
    skipped = 0

    for i, msgid in enumerate(ids):
        if len(out) >= limit:
            break
        typ, msg_data = M.fetch(msgid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            skipped += 1
            continue
        try:
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            body = _extract_body(msg)
            body = _strip_quoted(body)
            wc = len(body.split())
            if wc < 20:
                skipped += 1
                continue
            out.append({
                "id": _decode_header(msg.get("Message-ID", "")),
                "subject": _decode_header(msg.get("Subject", "")),
                "body": body,
                "to": _decode_header(msg.get("To", "")),
                "date": msg.get("Date", ""),
                "word_count": wc,
            })
        except Exception as e:
            skipped += 1
            continue

        if (i + 1) % 50 == 0:
            print(f"  processed {i+1}, kept {len(out)}, skipped {skipped}")

    try:
        M.close()
    except Exception:
        pass
    M.logout()

    CORPUS_PATH.write_text(json.dumps(out, indent=2))
    total_words = sum(m["word_count"] for m in out)
    print(f"\nSaved {len(out)} sent messages ({total_words:,} words) to {CORPUS_PATH}")
    print(f"Skipped {skipped} (short, unparseable, or drafts).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", type=int, default=2000, help="Pull N sent items")
    args = ap.parse_args()
    pull_sent(args.pull)


if __name__ == "__main__":
    main()
