"""
Chimera Secured - Live Hotmail / Outlook.com Connector
========================================================

Pulls your live Sent Items via Microsoft Graph, converts to plain text,
and writes corpus_sent.json so the rest of the pipeline can use it.

Also provides a "watch" mode that monitors your Inbox in real time and
scores new incoming emails against a trained model.

One-time setup (5 minutes):
---------------------------
1. Go to: https://entra.microsoft.com/  ->  Applications  ->  App registrations
   -> New registration
2. Name: "Chimera Secured Local"
3. Supported account types: "Personal Microsoft accounts only"
   (this is the one that works for Hotmail/Outlook.com)
4. Redirect URI: leave blank
5. Register.
6. Copy the "Application (client) ID" from the overview page.
7. Under "Authentication" -> "Advanced settings":
      "Allow public client flows" -> YES  (save)
8. Under "API permissions" -> "Add a permission" -> "Microsoft Graph"
      -> "Delegated permissions":
         Mail.Read
         offline_access
      -> Add permissions. (No admin consent needed for personal accounts.)

Then set env vars:
   export CHIMERA_CLIENT_ID=<paste client ID from step 6>

Usage:
  # One-time: pull the last 2000 sent emails into corpus_sent.json
  python chimera_hotmail.py --pull 2000

  # Watch mode: every 30s, score any new inbox messages
  python chimera_hotmail.py --watch --model chimera_model.pkl

Dependencies: pip install msal
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Dict, Any, Optional

TOKEN_CACHE_PATH = Path(".chimera_token_cache.json")
CORPUS_PATH = Path("corpus_sent.json")
WATCH_STATE_PATH = Path(".chimera_watch_state.json")
GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/common"  # accepts both work and personal accounts
SCOPES = ["Mail.Read"]


# ---------- HTML → plain text ----------

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


# ---------- Auth (MSAL device code flow) ----------

def _get_access_token() -> str:
    try:
        import msal
    except ImportError:
        sys.exit("pip install msal")

    client_id = os.environ.get("CHIMERA_CLIENT_ID")
    if not client_id:
        sys.exit(
            "Set CHIMERA_CLIENT_ID env var to your Azure app registration's client ID. "
            "See header of this file for one-time setup instructions."
        )

    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())

    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=AUTHORITY,
        token_cache=cache,
    )

    # Try silent first (uses refresh token if we have one)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        # Device code flow - user opens browser, enters code
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            sys.exit(f"Failed to start device flow: {flow}")
        print("\n" + "=" * 60)
        print(flow["message"])
        print("=" * 60 + "\n")
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        sys.exit(f"Auth failed: {result.get('error_description', result)}")

    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())

    return result["access_token"]


# ---------- Graph API ----------

def _graph_get(url: str, token: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # Prefer text bodies to cut HTML noise when available
            "Prefer": 'outlook.body-content-type="text"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Graph {e.code}: {err}") from e


def _extract_body(msg: Dict[str, Any]) -> str:
    body = msg.get("body", {}) or {}
    content = body.get("content", "") or ""
    ctype = (body.get("contentType") or "").lower()
    if ctype == "html":
        return html_to_text(content)
    return content.strip()


def _recipients(msg: Dict[str, Any]) -> str:
    rec = msg.get("toRecipients") or []
    emails = []
    for r in rec:
        addr = (r.get("emailAddress") or {}).get("address")
        if addr:
            emails.append(addr)
    return ", ".join(emails)


# ---------- Pull sent items ----------

def pull_sent(limit: int) -> None:
    token = _get_access_token()
    url = (
        f"{GRAPH}/me/mailFolders/SentItems/messages"
        f"?$select=id,subject,body,toRecipients,sentDateTime,isDraft"
        f"&$orderby=sentDateTime%20desc"
        f"&$top=50"
    )

    out: List[Dict[str, Any]] = []
    fetched = 0
    skipped = 0

    while url and fetched < limit:
        data = _graph_get(url, token)
        for msg in data.get("value", []):
            if fetched >= limit:
                break
            if msg.get("isDraft"):
                skipped += 1
                continue
            body = _extract_body(msg)
            wc = len(body.split())
            if wc < 20:
                skipped += 1
                continue
            out.append({
                "id": msg.get("id"),
                "subject": msg.get("subject", ""),
                "body": body,
                "to": _recipients(msg),
                "date": msg.get("sentDateTime", ""),
                "word_count": wc,
            })
            fetched += 1
            if fetched % 50 == 0:
                print(f"  pulled {fetched} ({skipped} skipped)")
        url = data.get("@odata.nextLink")

    CORPUS_PATH.write_text(json.dumps(out, indent=2))
    total_words = sum(m["word_count"] for m in out)
    print(f"\nSaved {len(out)} sent messages ({total_words:,} words) to {CORPUS_PATH}")
    print(f"Skipped {skipped} (drafts or <20 words).")
    if len(out) < 100:
        print("\nWARNING: fewer than 100 usable emails. Model quality will be limited.")
        print("Try --pull with a higher number, or use an account with more sent history.")


# ---------- Watch mode (live scoring of new inbox messages) ----------

def watch(model_path: str, interval_sec: int = 30) -> None:
    from chimera_live import LiveScorer
    scorer = LiveScorer(model_path)
    print(f"Loaded model. Watching inbox every {interval_sec}s. Ctrl-C to stop.\n")

    # Track already-seen message IDs so we don't re-score
    if WATCH_STATE_PATH.exists():
        seen = set(json.loads(WATCH_STATE_PATH.read_text()))
    else:
        seen = set()

    while True:
        try:
            token = _get_access_token()
            url = (
                f"{GRAPH}/me/mailFolders/Inbox/messages"
                f"?$select=id,subject,from,body,receivedDateTime"
                f"&$orderby=receivedDateTime%20desc"
                f"&$top=20"
            )
            data = _graph_get(url, token)
            new_msgs = [m for m in data.get("value", []) if m.get("id") not in seen]
            new_msgs.reverse()  # oldest first for readability

            for msg in new_msgs:
                body = _extract_body(msg)
                frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "?")
                subj = msg.get("subject", "(no subject)")[:60]
                r = scorer.score(body)
                tag = {
                    "verified": "OK ",
                    "flagged": "!!",
                    "blocked": "XX",
                }.get(r.verdict, "?")
                print(f"[{tag}] {r.score:.2f}  from={frm[:35]:35s}  {subj}")
                if r.verdict != "verified":
                    for reason in r.reasons[:2]:
                        print(f"       - {reason}")
                seen.add(msg["id"])

            # Persist seen set (cap size so it doesn't grow forever)
            if len(seen) > 5000:
                seen = set(list(seen)[-2500:])
            WATCH_STATE_PATH.write_text(json.dumps(list(seen)))
        except Exception as e:
            print(f"[watch] error: {e}")

        time.sleep(interval_sec)


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", type=int, help="Pull N sent items into corpus_sent.json")
    ap.add_argument("--watch", action="store_true", help="Live-score new inbox messages")
    ap.add_argument("--model", default="chimera_model.pkl", help="Path to trained model (watch mode)")
    ap.add_argument("--interval", type=int, default=30, help="Watch poll interval (seconds)")
    args = ap.parse_args()

    if args.pull:
        pull_sent(args.pull)
    elif args.watch:
        watch(args.model, args.interval)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
