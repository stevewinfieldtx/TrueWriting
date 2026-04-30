"""
Wave 3 — Recipient Profile Builder

Given a corpus of Steve's sent emails, build per-recipient profiles capturing
the patterns an impersonator would find hard to fake even with a leaked email:
send frequency, time-of-day, word-count distribution, greeting/closing
repertoire, punctuation habits.

Profiles are built fit-only, with no access to eval labels. Intended usage:

    from recipient_profiler import build_profiles, get_profile
    profiles = build_profiles(train_emails)
    save_profiles(profiles, 'recipient_profiles.json')

Cold-start policy: recipients with fewer than MIN_SENDS_KNOWN sends are flagged
`is_known=False`. This is a feature, not a score adjustment. The classifier
decides what weight to give it.
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Any, Optional

# ---- tunables ----
MIN_SENDS_KNOWN = 3          # below this, treat as cold-start
TOP_GREETINGS_K = 5          # keep top-k greetings per recipient
TOP_CLOSINGS_K = 5
GREETING_WORDS = 3           # first N words define the greeting fingerprint
CLOSING_WORDS = 3

RE_WHITESPACE = re.compile(r'\s+')
RE_WORD = re.compile(r"\b\w+\b")
RE_EM_DASH = re.compile(r'—|--')
RE_ELLIPSIS = re.compile(r'\.{3,}|…')
RE_EXCLAIM = re.compile(r'!')
RE_REPLY_SUBJECT = re.compile(r'^(re|fwd|fw)\s*:', re.IGNORECASE)

RE_FORWARD_HEADER = re.compile(
    r'^\s*(from|sent|to|cc|subject|date):\s', re.IGNORECASE)
RE_QUOTE_DIVIDER = re.compile(r'^\s*_{5,}|^\s*-{5,}|^\s*>{1,}')
RE_MOBILE_SIG = re.compile(
    r'^\s*(sent from my|get outlook for|sent from mail for|'
    r'sent from the|sent via|outlook for (ios|android))',
    re.IGNORECASE)
RE_BEGIN_FORWARDED = re.compile(
    r'begin forwarded message|forwarded message|original message',
    re.IGNORECASE)


def _parse_recipients(to_field: Any) -> List[str]:
    if not to_field:
        return []
    flat = ','.join(str(x) for x in to_field) if isinstance(to_field, list) else str(to_field)
    out = []
    for part in flat.split(','):
        r = part.strip().lower()
        if r:
            m = re.search(r'<([^>]+)>', r)
            if m:
                r = m.group(1).strip().lower()
            out.append(r)
    return out


def _parse_date(d: Any) -> Optional[datetime]:
    if not d:
        return None
    s = str(d).strip()
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _first_n_words(text: str, n: int) -> str:
    if not text:
        return ''
    words = RE_WORD.findall(text.strip())
    return ' '.join(w.lower() for w in words[:n])


def _last_n_words(text: str, n: int) -> str:
    if not text:
        return ''
    words = RE_WORD.findall(text)
    return ' '.join(w.lower() for w in words[-n:]) if words else ''


def _count_matches(regex, text: str) -> int:
    if not text:
        return 0
    return len(regex.findall(text))


def _percentile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _stats(xs: List[float]) -> Dict[str, float]:
    if not xs:
        return {'mean': 0.0, 'median': 0.0, 'p10': 0.0, 'p90': 0.0, 'std': 0.0}
    return {
        'mean': float(statistics.fmean(xs)),
        'median': float(statistics.median(xs)),
        'p10': float(_percentile(xs, 10)),
        'p90': float(_percentile(xs, 90)),
        'std': float(statistics.pstdev(xs)) if len(xs) > 1 else 0.0,
    }


def _hist(xs: List[int], nbins: int) -> List[float]:
    c = Counter(xs)
    total = sum(c.values())
    if total == 0:
        return [0.0] * nbins
    return [c.get(i, 0) / total for i in range(nbins)]


def extract_own_text(body: str) -> str:
    """
    Isolate Steve's own text. Strips:
      - Signature block (---Steve ... phone/email)
      - Mobile/auto signatures (Sent from my iPhone, Get Outlook for iOS)
      - Forwarded/quoted content (From: / Sent: / > lines / ___ dividers)
      - "Begin forwarded message" markers
    Returns only the prose Steve actually typed.
    """
    if not body:
        return ''
    lines = body.split('\n')
    out_lines: List[str] = []
    consecutive_header = 0
    for line in lines:
        stripped = line.strip()
        # Hard stops — signature / mobile sig / forwarded marker
        if re.match(r'^\s*---\s*steve\s*$', line, re.IGNORECASE):
            break
        if re.match(r'^\s*swinfield@', line, re.IGNORECASE):
            break
        if RE_MOBILE_SIG.match(line):
            break
        if RE_BEGIN_FORWARDED.search(line) and len(stripped) < 60:
            break
        # Two-or-more consecutive "From:/Sent:/To:/Subject:" lines = forwarded block
        if RE_FORWARD_HEADER.match(line):
            consecutive_header += 1
            if consecutive_header >= 2:
                while out_lines and RE_FORWARD_HEADER.match(out_lines[-1]):
                    out_lines.pop()
                break
            out_lines.append(line)
            continue
        else:
            consecutive_header = 0
        # Quote dividers / lines starting with > indicate beginning of quoted content
        if RE_QUOTE_DIVIDER.match(line):
            if len(out_lines) >= 2:
                break
            else:
                continue
        out_lines.append(line)
    return '\n'.join(out_lines).strip()


# keep legacy name for anyone importing it
_normalize_body = extract_own_text


def build_profile_for_recipient(emails: List[Dict[str, Any]]) -> Dict[str, Any]:
    word_counts: List[float] = []
    hours: List[int] = []
    dows: List[int] = []
    dates: List[datetime] = []
    greeting_bag: Counter = Counter()
    closing_bag: Counter = Counter()
    em_dash_rate: List[float] = []
    ellipsis_rate: List[float] = []
    exclaim_rate: List[float] = []
    reply_count = 0
    own_text_word_counts: List[float] = []

    for e in emails:
        raw_body = e.get('body', '') or ''
        own = extract_own_text(raw_body)
        own_wc = len(RE_WORD.findall(own))
        own_text_word_counts.append(own_wc)

        full_wc = e.get('word_count') or len(RE_WORD.findall(raw_body))
        word_counts.append(full_wc)

        dt = _parse_date(e.get('date'))
        if dt is not None:
            hours.append(dt.hour)
            dows.append(dt.weekday())
            dates.append(dt)

        # greeting/closing from own text only — must be substantial enough
        if own_wc >= 4:
            g = _first_n_words(own, GREETING_WORDS)
            if g:
                greeting_bag[g] += 1
            c = _last_n_words(own, CLOSING_WORDS)
            if c:
                closing_bag[c] += 1

        subj = e.get('subject', '') or ''
        if RE_REPLY_SUBJECT.match(subj.strip()):
            reply_count += 1

        if own_wc > 0:
            em_dash_rate.append(100.0 * _count_matches(RE_EM_DASH, own) / max(own_wc, 1))
            ellipsis_rate.append(100.0 * _count_matches(RE_ELLIPSIS, own) / max(own_wc, 1))
            exclaim_rate.append(100.0 * _count_matches(RE_EXCLAIM, own) / max(own_wc, 1))

    n = len(emails)
    dates_sorted = sorted(dates) if dates else []
    inter_send_gaps: List[float] = []
    if len(dates_sorted) >= 2:
        for i in range(1, len(dates_sorted)):
            gap = (dates_sorted[i] - dates_sorted[i - 1]).total_seconds() / 86400.0
            inter_send_gaps.append(gap)

    return {
        'n_sends': n,
        'is_known': n >= MIN_SENDS_KNOWN,
        'first_seen': dates_sorted[0].isoformat() if dates_sorted else None,
        'last_seen': dates_sorted[-1].isoformat() if dates_sorted else None,
        'word_count_stats': _stats(word_counts),
        'own_text_word_count_stats': _stats(own_text_word_counts),
        'hour_hist': _hist(hours, 24),
        'dow_hist': _hist(dows, 7),
        'inter_send_gap_days_stats': _stats(inter_send_gaps) if inter_send_gaps else _stats([]),
        'reply_rate': reply_count / n if n else 0.0,
        'top_greetings': greeting_bag.most_common(TOP_GREETINGS_K),
        'top_closings': closing_bag.most_common(TOP_CLOSINGS_K),
        'em_dash_per_100w_stats': _stats(em_dash_rate),
        'ellipsis_per_100w_stats': _stats(ellipsis_rate),
        'exclaim_per_100w_stats': _stats(exclaim_rate),
    }


def build_global_profile(emails: List[Dict[str, Any]]) -> Dict[str, Any]:
    return build_profile_for_recipient(emails)


def build_profiles(emails: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_recipient: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in emails:
        recipients = _parse_recipients(e.get('to', ''))
        for r in recipients:
            by_recipient[r].append(e)

    profiles: Dict[str, Any] = {'_global': build_global_profile(emails)}
    for r, es in by_recipient.items():
        profiles[r] = build_profile_for_recipient(es)

    profiles['_meta'] = {
        'n_emails': len(emails),
        'n_recipients': len(by_recipient),
        'n_known_recipients': sum(1 for p in profiles.values()
                                   if isinstance(p, dict) and p.get('is_known')),
        'min_sends_known': MIN_SENDS_KNOWN,
        'built_at': datetime.utcnow().isoformat() + 'Z',
    }
    return profiles


def get_profile(profiles: Dict[str, Any], recipient: str) -> Dict[str, Any]:
    r = (recipient or '').strip().lower()
    m = re.search(r'<([^>]+)>', r)
    if m:
        r = m.group(1).strip().lower()
    # When multiple recipients, caller should pick one; here just try first key
    if ',' in r:
        r = r.split(',')[0].strip()
    if r in profiles:
        return profiles[r]
    return profiles.get('_global', {})


def save_profiles(profiles: Dict[str, Any], path: str) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)


def load_profiles(path: str) -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'eval_splits/train_real.json'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'recipient_profiles.json'

    print(f'[profiler] loading {src}...')
    with open(src, 'r', encoding='utf-8') as f:
        emails = json.load(f)
    print(f'[profiler] {len(emails)} emails loaded')

    print('[profiler] building profiles...')
    profs = build_profiles(emails)
    print(f'[profiler] {len(profs) - 2} recipient profiles + 1 global + 1 meta')
    print(f'[profiler] known recipients: {profs["_meta"]["n_known_recipients"]}')

    save_profiles(profs, dst)
    print(f'[profiler] saved to {dst}')
