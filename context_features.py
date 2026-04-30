"""
Wave 3 — Context Feature Extractor

Given an email and a set of recipient profiles, extract numeric features
answering: "does this email fit Steve's historical patterns for this recipient?"

These are signals Chimera stylometry cannot use — by design, they don't touch
the prose content. An attacker with a leaked email can copy the writing style;
they can't easily copy the interaction history.

Features dropped during diagnostics:
  - rec_days_since_last_send: leaked via random train/heldout split (profiles
    "see the future"). Would need temporal split or out-of-fold profiling to
    be honest, which isn't worth it for this dataset.

Usage:
    from context_features import extract_features, FEATURE_NAMES
    vec = extract_features(email_dict, profiles)
"""

import math
import re
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional

from recipient_profiler import (
    _parse_recipients, _parse_date, _first_n_words, _last_n_words,
    _count_matches, extract_own_text,
    RE_WORD, RE_EM_DASH, RE_ELLIPSIS, RE_EXCLAIM, RE_REPLY_SUBJECT,
)

FEATURE_NAMES: List[str] = [
    'rec_is_known',                  # recipient has >= MIN_SENDS_KNOWN prior sends
    'rec_n_sends_log1p',             # log(1 + prior sends to this recipient)
    'wc_z_vs_recipient',             # (email wc - rec mean) / rec std
    'wc_z_vs_global',                # same vs global baseline
    'hour_fit_recipient',            # rec.hour_hist[email.hour]
    'hour_fit_global',               # global.hour_hist[email.hour]
    'dow_fit_recipient',             # rec.dow_hist[email.dow]
    'dow_fit_global',                # global.dow_hist[email.dow]
    'reply_rate_recipient',          # rec reply rate (% of emails that are Re:/Fwd:)
    'is_reply',                      # 1 if subject starts with Re:/Fwd:
    'reply_expected_mismatch',       # |is_reply - rec reply rate|
    'greeting_in_top_k_recipient',   # does first 3 words match rec top greetings?
    'greeting_in_top_k_global',      # same vs global
    'closing_in_top_k_global',       # does last 3 words of own text match global closings?
    'em_dash_z_vs_recipient',
    'ellipsis_z_vs_recipient',
    'exclaim_z_vs_recipient',
    'own_wc_over_full_wc',           # ratio of own text to full body (quoted content)
    'n_recipients',                  # how many addresses in "to"
    'rec_is_self',                   # sent to swinfield@hotmail.com (self-send)
]

_Z_CLIP = 10.0


def _safe_z(x: float, mean: float, std: float) -> float:
    if std <= 1e-9:
        return 0.0
    z = (x - mean) / std
    return max(-_Z_CLIP, min(_Z_CLIP, z))


def _pick_primary_profile(profiles: Dict[str, Any],
                          recipients: List[str]) -> Tuple[Dict[str, Any], str]:
    """
    Given a list of recipients, pick the one with the highest n_sends.
    If none are known, return _global and the first recipient.
    """
    best: Optional[str] = None
    best_n = -1
    for r in recipients:
        p = profiles.get(r)
        if p and p.get('n_sends', 0) > best_n:
            best = r
            best_n = p['n_sends']
    if best is not None:
        return profiles[best], best
    first = recipients[0] if recipients else ''
    return profiles.get('_global', {}), first


def _hist_fit(hist: List[float], idx: int) -> float:
    if not hist or idx is None or idx < 0 or idx >= len(hist):
        return 0.0
    return float(hist[idx])


def _greeting_match(own_text: str, top_pairs: List[List[Any]]) -> float:
    if not top_pairs:
        return 0.0
    g = _first_n_words(own_text, 3)
    if not g:
        return 0.0
    for pair in top_pairs:
        phrase = pair[0] if isinstance(pair, (list, tuple)) else pair
        if phrase and g == phrase:
            return 1.0
    return 0.0


def _closing_match(own_text: str, top_pairs: List[List[Any]]) -> float:
    if not top_pairs:
        return 0.0
    c = _last_n_words(own_text, 3)
    if not c:
        return 0.0
    for pair in top_pairs:
        phrase = pair[0] if isinstance(pair, (list, tuple)) else pair
        if phrase and c == phrase:
            return 1.0
    return 0.0


def _resolve_recipient_override(email: Dict[str, Any]) -> str:
    for k in ('recipient', 'to_override'):
        if email.get(k):
            return str(email[k])
    return str(email.get('to', '') or '')


def extract_features(email: Dict[str, Any],
                     profiles: Dict[str, Any]) -> List[float]:
    body = email.get('body', '') or ''
    subject = email.get('subject', '') or ''
    to_field = _resolve_recipient_override(email)
    date_raw = email.get('date')

    own_text = extract_own_text(body)
    own_wc = len(RE_WORD.findall(own_text))
    full_wc = email.get('word_count') or len(RE_WORD.findall(body))
    own_over_full = (own_wc / full_wc) if full_wc > 0 else 0.0

    recipients = _parse_recipients(to_field)
    prof, primary = _pick_primary_profile(profiles, recipients)
    glob = profiles.get('_global', {}) or {}

    rec_is_known = 1.0 if prof.get('is_known') else 0.0
    n_sends = prof.get('n_sends', 0) or 0
    rec_n_sends_log1p = math.log1p(n_sends)

    dt = _parse_date(date_raw) or datetime.utcnow()

    wc_stats_rec = prof.get('word_count_stats', {})
    wc_stats_glob = glob.get('word_count_stats', {})
    wc_z_rec = _safe_z(full_wc, wc_stats_rec.get('mean', 0.0), wc_stats_rec.get('std', 1.0))
    wc_z_glob = _safe_z(full_wc, wc_stats_glob.get('mean', 0.0), wc_stats_glob.get('std', 1.0))

    hour = dt.hour
    dow = dt.weekday()
    hour_fit_rec = _hist_fit(prof.get('hour_hist', []), hour)
    hour_fit_glob = _hist_fit(glob.get('hour_hist', []), hour)
    dow_fit_rec = _hist_fit(prof.get('dow_hist', []), dow)
    dow_fit_glob = _hist_fit(glob.get('dow_hist', []), dow)

    rec_reply_rate = float(prof.get('reply_rate', 0.0) or 0.0)
    is_reply = 1.0 if RE_REPLY_SUBJECT.match(subject.strip()) else 0.0
    reply_mismatch = abs(is_reply - rec_reply_rate)

    greet_match_rec = _greeting_match(own_text, prof.get('top_greetings', []))
    greet_match_glob = _greeting_match(own_text, glob.get('top_greetings', []))
    close_match_glob = _closing_match(own_text, glob.get('top_closings', []))

    em_stats = prof.get('em_dash_per_100w_stats', {})
    el_stats = prof.get('ellipsis_per_100w_stats', {})
    ex_stats = prof.get('exclaim_per_100w_stats', {})
    if own_wc > 0:
        em_rate = 100.0 * _count_matches(RE_EM_DASH, own_text) / max(own_wc, 1)
        el_rate = 100.0 * _count_matches(RE_ELLIPSIS, own_text) / max(own_wc, 1)
        ex_rate = 100.0 * _count_matches(RE_EXCLAIM, own_text) / max(own_wc, 1)
    else:
        em_rate = el_rate = ex_rate = 0.0
    em_z = _safe_z(em_rate, em_stats.get('mean', 0.0), em_stats.get('std', 1.0))
    el_z = _safe_z(el_rate, el_stats.get('mean', 0.0), el_stats.get('std', 1.0))
    ex_z = _safe_z(ex_rate, ex_stats.get('mean', 0.0), ex_stats.get('std', 1.0))

    n_recipients = float(len(recipients))
    rec_is_self = 1.0 if any(r.strip() == 'swinfield@hotmail.com' for r in recipients) else 0.0

    vec = [
        rec_is_known,
        rec_n_sends_log1p,
        wc_z_rec,
        wc_z_glob,
        hour_fit_rec,
        hour_fit_glob,
        dow_fit_rec,
        dow_fit_glob,
        rec_reply_rate,
        is_reply,
        reply_mismatch,
        greet_match_rec,
        greet_match_glob,
        close_match_glob,
        em_z,
        el_z,
        ex_z,
        own_over_full,
        n_recipients,
        rec_is_self,
    ]
    assert len(vec) == len(FEATURE_NAMES), f'{len(vec)} vs {len(FEATURE_NAMES)}'
    return vec


def extract_features_batch(emails: List[Dict[str, Any]],
                           profiles: Dict[str, Any]) -> List[List[float]]:
    return [extract_features(e, profiles) for e in emails]


if __name__ == '__main__':
    import json
    from recipient_profiler import load_profiles

    profiles = load_profiles('recipient_profiles.json')
    with open('eval_splits/heldout_real.json', 'r', encoding='utf-8') as f:
        heldout = json.load(f)

    print(f'extracting {len(FEATURE_NAMES)} features for {len(heldout)} held-out reals...')
    vecs = extract_features_batch(heldout, profiles)
    print(f'done. example feature vector for email 0:')
    for name, val in zip(FEATURE_NAMES, vecs[0]):
        print(f'  {name}: {val:.4f}')
