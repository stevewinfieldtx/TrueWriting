"""
Wave 2.5 - DLP (Data Loss Prevention) Scanner.

A minimal deterministic BEC-payload detector. Runs on email body + subject.

Philosophy:
  - Regex + keyword categories, weighted by severity.
  - Hard signals alone can block (wire/bank/gift-card/roster/W-2/credentials).
  - Soft signals (urgency, secrecy) never block alone but AMPLIFY hard hits.
  - Score is a continuous [0, 1] suspicion value for the risk composer.
  - Deterministic and explainable - every hit has a category and a matched snippet.

Categories:
  payment_change    : change bank details / update routing / new vendor info
  gift_cards        : gift-card purchase requests (classic BEC payload)
  wire              : wire transfer requests
  credentials       : password / credential / MFA exfil
  sensitive_data    : roster / employee list / W-2 / SSN / tax info
  crypto            : bitcoin / USDT / wallet address transfer
  financial_urgency : "invoice", "approve", "pay" in urgent framing
  urgency           : "urgent", "ASAP", "before EOD" (soft)
  secrecy           : "don't discuss", "confidential" (soft)

Usage:
  from dlp_scanner import scan
  result = scan(body, subject="")
  # result = {"score": 0.85, "hits": [{"category":..., "match":...}, ...]}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Dict

from recipient_profiler import extract_own_text


# ---------- Patterns ----------
# Each category: list of (weight, compiled_regex). Weight is the per-hit score
# contribution BEFORE combos. Multiple matches in the same category count once
# (max weight), unless noted. Patterns are case-insensitive word-boundary-aware.

def _rx(pat: str) -> re.Pattern:
    return re.compile(pat, re.IGNORECASE)


PATTERNS = {
    "payment_change": [
        (0.75, _rx(r"\bchang(e|ing)\s+(?:the\s+)?(?:bank|banking|routing|account|payment)\b")),
        (0.75, _rx(r"\bupdat(e|ing|ed)\s+(?:the\s+)?(?:bank|banking|routing|account|payment|vendor\s+payment|ach)\b")),
        (0.75, _rx(r"\bnew\s+(?:bank|banking|routing|account|payment|vendor\s+payment|ach)\b")),
        (0.7,  _rx(r"\brouting\s+number\b")),
        (0.6,  _rx(r"\bach\s+(?:details|info|information|instructions|transfer|payment)\b")),
        (0.65, _rx(r"\biban\b|\bswift\s+code\b")),
        (0.7,  _rx(r"\b(?:updated|new|revised)\s+(?:payment|wiring|banking)\s+(?:details|info|instructions)\b")),
    ],
    "gift_cards": [
        (0.85, _rx(r"\bgift\s+cards?\b")),
        (0.8,  _rx(r"\bitunes\s+(?:card|gift)\b")),
        (0.8,  _rx(r"\bamazon\s+(?:card|gift\s+card)\b")),
        (0.8,  _rx(r"\bsteam\s+card\b|\bgoogle\s+play\s+card\b")),
        (0.85, _rx(r"\bbuy\s+(?:\$?\d+\s+(?:in|worth\s+of)\s+)?(?:gift|itunes|amazon)\b")),
    ],
    "wire": [
        (0.55, _rx(r"\bwire\s+(?:transfer|payment|the\s+funds)\b")),
        (0.55, _rx(r"\bprocess\s+(?:a\s+)?wire\b")),
        (0.5,  _rx(r"\btransfer\s+(?:\$?\d|funds|the\s+money)\b")),
        (0.5,  _rx(r"\bsend\s+(?:the\s+)?(?:money|funds|payment)\b")),
    ],
    "credentials": [
        (0.8,  _rx(r"\b(?:send|share|forward)\s+(?:me\s+)?(?:your\s+|the\s+)?password\b")),
        (0.75, _rx(r"\breset\s+(?:your|the|my)\s+password\b")),
        (0.75, _rx(r"\bverify\s+(?:your|the)\s+(?:account|credentials|login)\b")),
        (0.75, _rx(r"\bmfa\s+(?:code|token)\b")),
        (0.75, _rx(r"\b(?:2fa|two[-\s]factor)\s+code\b")),
        (0.7,  _rx(r"\b(?:update|confirm)\s+(?:your\s+)?credentials\b")),
    ],
    "sensitive_data": [
        (0.7,  _rx(r"\b(?:team|employee|staff)\s+roster\b")),
        (0.7,  _rx(r"\bemployee\s+(?:list|directory|details)\b")),
        (0.8,  _rx(r"\bw[-\s]?2\b|\bw2s?\b")),
        (0.8,  _rx(r"\bsocial\s+security\s+number\b|\bssn\b")),
        (0.65, _rx(r"\btax\s+(?:info|information|forms|returns)\b")),
        (0.6,  _rx(r"\b(?:send|forward|share)\s+(?:the\s+)?(?:q\d|quarterly|annual)?\s*financials?\b")),
        (0.55, _rx(r"\bpayroll\b")),
    ],
    "crypto": [
        # Keyword-only v1. The BTC-address regex was catching email tracking IDs
        # (Craigslist/Indeed/Outlook fwds) so it's disabled until we have a real
        # Base58-checksum implementation.
        (0.8,  _rx(r"\bbitcoin\b")),
        (0.8,  _rx(r"\bbtc\s+(?:wallet|address|payment|transfer)\b")),
        (0.8,  _rx(r"\busdt\b|\busdc\b|\bethereum\b|\beth\s+wallet\b")),
        (0.85, _rx(r"\bcrypto\s+(?:wallet|address)\b")),
    ],
    "financial_urgency": [
        (0.55, _rx(r"\b(?:approve|pay|process)\s+(?:the\s+|this\s+)?invoice\b")),
        (0.5,  _rx(r"\bpayment\s+(?:today|eod|by\s+(?:end\s+of\s+day|friday|cob))\b")),
        (0.5,  _rx(r"\bfinancial\s+(?:approval|authorization)\b")),
    ],
    "urgency": [  # soft - never enough alone
        (0.2,  _rx(r"\b(?:urgent|urgently|asap)\b")),
        (0.15, _rx(r"\bbefore\s+(?:eod|end\s+of\s+day|cob|cob\s+today|close\s+of\s+business)\b")),
        (0.15, _rx(r"\bimmediately\b")),
        (0.15, _rx(r"\bright\s+(?:now|away)\b")),
    ],
    "secrecy": [  # soft - never enough alone
        (0.2,  _rx(r"\bdon'?t\s+discuss\b|\bkeep\s+(?:this\s+)?(?:quiet|confidential|between\s+us)\b")),
        (0.2,  _rx(r"\bconfidential(?:ly)?\b")),
        (0.15, _rx(r"\bdo\s+not\s+tell\b|\bdo\s+not\s+mention\b")),
        (0.2,  _rx(r"\bon\s+a\s+flight\b|\bin\s+a\s+meeting\b|\bon\s+my\s+phone\b")),  # plausible isolation
    ],
}

# Category classes
HARD_CATS = {"payment_change", "gift_cards", "wire", "credentials",
             "sensitive_data", "crypto", "financial_urgency"}
SOFT_CATS = {"urgency", "secrecy"}

# Amplification weights when a soft hit co-occurs with a hard hit.
SOFT_AMPLIFIER = 0.15  # added per soft category present (capped at 2 soft)


@dataclass
class DLPResult:
    score: float
    hits: List[Dict] = field(default_factory=list)
    category_weights: Dict[str, float] = field(default_factory=dict)

    def to_dict(self):
        return {
            "score": self.score,
            "hits": self.hits,
            "category_weights": self.category_weights,
        }


def scan(body: str, subject: str = "") -> DLPResult:
    """Run DLP scan. Returns DLPResult with score in [0, 1] and hits list.

    Scans Steve's OWN text only (forwarded/quoted content is stripped). The
    subject line is scanned as-is since it's authored by whoever sent the
    email.
    """
    own_body = extract_own_text(body) if body else ""
    text = (subject + "\n" + own_body) if subject else own_body
    if not text:
        return DLPResult(score=0.0)

    hits: List[Dict] = []
    # Track max weight seen per category (so two payment_change hits don't double-count)
    cat_max: Dict[str, float] = {}

    for cat, patterns in PATTERNS.items():
        for weight, rx in patterns:
            m = rx.search(text)
            if m:
                hits.append({
                    "category": cat,
                    "weight": weight,
                    "match": m.group(0)[:80],
                })
                if weight > cat_max.get(cat, 0.0):
                    cat_max[cat] = weight

    # Compute base score from hard categories (top-2 hard hits compose)
    hard_weights = sorted(
        [w for c, w in cat_max.items() if c in HARD_CATS],
        reverse=True,
    )
    if hard_weights:
        # Probabilistic OR of top 2 hard hits: 1 - (1-a)(1-b)
        if len(hard_weights) >= 2:
            a, b = hard_weights[0], hard_weights[1]
            base = 1 - (1 - a) * (1 - b)
        else:
            base = hard_weights[0]
    else:
        base = 0.0

    # Soft amplification: only applies if base > 0 (there's a hard hit to amplify)
    soft_count = sum(1 for c in cat_max if c in SOFT_CATS)
    if base > 0 and soft_count > 0:
        amplifier = SOFT_AMPLIFIER * min(soft_count, 2)  # cap at 2 soft cats
        base = base + amplifier * (1 - base)  # push toward 1 without exceeding
    elif base == 0 and soft_count > 0:
        # Soft-only email: tiny risk signal, but below any threshold
        base = 0.1 * min(soft_count, 2)

    score = max(0.0, min(1.0, base))
    return DLPResult(
        score=score,
        hits=hits,
        category_weights=cat_max,
    )


# ---------- Smoke test ----------

if __name__ == "__main__":
    cases = [
        ("clean real email",
         "Hey - good meeting yesterday. Let me know when you want to schedule the next one.\n\nThanks, Steve"),
        ("urgent reminder (should be low)",
         "Hey can you send me the slides for tomorrow's meeting ASAP? Urgent. Thanks."),
        ("gift card classic BEC",
         "I need you to go buy $500 in gift cards for a client appreciation gift. Do it ASAP and don't discuss with anyone."),
        ("wire to new bank",
         "Please update the banking details for our largest supplier. New routing number is 021000021. Wire the payment today before EOD."),
        ("roster request",
         "Could you send the team roster to HR? They need it for compliance reporting."),
        ("W-2 request",
         "I need copies of all employee W-2 forms forwarded to our external auditor ASAP. Confidential."),
        ("invoice approve",
         "Please approve the attached invoice and confirm payment today. I'm on a flight."),
        ("crypto scam",
         "Transfer 0.5 BTC to this wallet: bc1q9h6mlwu5jp4kk8m6lzp3c8xqt6j9s7hx3m2p5k immediately."),
        ("credential request",
         "Please reset your password and share the new one with me for audit purposes."),
    ]
    for name, text in cases:
        r = scan(text)
        cats = list(r.category_weights.keys())
        print(f"{name:30s}  score={r.score:.3f}  cats={cats}")
