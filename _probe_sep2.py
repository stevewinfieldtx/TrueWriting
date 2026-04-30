"""
Diagnostic v2: attach realistic metadata to fakes before feature extraction.

An attacker of Steve will have picked a real target. They won't send the email
with no recipient. For every fake, sample a plausible recipient from Steve's
known address book and a plausible time. Then measure how much signal remains
in each context feature.

We sample recipients from the DISTRIBUTION of Steve's real sends, weighted by
send count. This means attackers hit common targets. If our features still
separate fakes from reals after this, the signal is real.
"""
import json
import random
import statistics
from datetime import datetime, timedelta
from recipient_profiler import load_profiles
from context_features import extract_features, FEATURE_NAMES


def roc_auc(scores_pos, scores_neg):
    n_pos = len(scores_pos)
    n_neg = len(scores_neg)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    combined = [(s, 1) for s in scores_pos] + [(s, 0) for s in scores_neg]
    combined.sort(key=lambda x: x[0])
    ranks = {}
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j
    sum_pos = sum(ranks[k] for k, (s, lbl) in enumerate(combined) if lbl == 1)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def sample_recipient(profiles, weighted=True):
    items = [(k, v.get('n_sends', 0)) for k, v in profiles.items()
             if k not in ('_meta', '_global') and v.get('n_sends', 0) >= 3
             and k != 'swinfield@hotmail.com']
    if not items:
        return None
    if weighted:
        total = sum(w for _, w in items)
        pick = random.random() * total
        acc = 0
        for k, w in items:
            acc += w
            if acc >= pick:
                return k
    return random.choice(items)[0]


def sample_date(profiles, recipient, rng_days=180):
    """Sample a date within the last rng_days that fits the recipient's typical hour."""
    prof = profiles.get(recipient, profiles['_global'])
    hist = prof.get('hour_hist', [1/24]*24)
    hours = list(range(24))
    total = sum(hist) or 1.0
    weights = [h/total for h in hist]
    pick = random.random()
    acc = 0
    for h, w in zip(hours, weights):
        acc += w
        if acc >= pick:
            chosen_hour = h
            break
    else:
        chosen_hour = 18

    days_ago = random.randint(1, rng_days)
    dt = datetime.utcnow() - timedelta(days=days_ago)
    dt = dt.replace(hour=chosen_hour, minute=random.randint(0, 59), second=0)
    return dt.isoformat() + 'Z'


def attach_metadata(fakes, profiles, seed=42):
    """Attach plausible to/date/subject to each fake body. Pure, returns new list."""
    rng = random.Random(seed)
    random.seed(seed)
    out = []
    for e in fakes:
        body = e.get('body', '')
        rec = sample_recipient(profiles, weighted=True)
        date = sample_date(profiles, rec) if rec else datetime.utcnow().isoformat()+'Z'
        # Fakes are almost always NEW threads (asking for something),
        # but we randomize so some look like replies, to match Steve's 69% reply rate.
        is_reply = rng.random() < 0.5
        subj_prefix = 'Re: ' if is_reply else ''
        new = dict(e)
        new['to'] = rec or ''
        new['date'] = date
        new['subject'] = subj_prefix + e.get('topic', 'quick question')
        out.append(new)
    return out


def feature_matrix(emails, profiles):
    return [extract_features(e, profiles) for e in emails]


def main():
    profiles = load_profiles('recipient_profiles.json')
    with open('eval_splits/heldout_real.json', 'r', encoding='utf-8') as f:
        reals = json.load(f)

    real_mat = feature_matrix(reals, profiles)
    out_lines = [f"heldout reals: {len(reals)}\n"]

    for tier in ['zero_shot', 'few_shot', 'high_fidelity']:
        with open(f'eval_fakes/{tier}.json', 'r', encoding='utf-8') as f:
            fakes = json.load(f)
        fakes_md = attach_metadata(fakes, profiles, seed=42)
        out_lines.append(f"\n=== {tier}: {len(fakes)} fakes (metadata attached) ===\n")
        out_lines.append(f"sample to: {fakes_md[0].get('to')!r}  "
                         f"date: {fakes_md[0].get('date')!r}  "
                         f"subj: {fakes_md[0].get('subject')!r}\n")
        fake_mat = feature_matrix(fakes_md, profiles)

        out_lines.append(f"{'feature':<35} {'real_mean':>10} {'fake_mean':>10} {'|AUC-0.5|':>11}\n")
        rows = []
        for fi, name in enumerate(FEATURE_NAMES):
            r_vals = [row[fi] for row in real_mat]
            k_vals = [row[fi] for row in fake_mat]
            r_mean = statistics.fmean(r_vals)
            k_mean = statistics.fmean(k_vals)
            auc = roc_auc(k_vals, r_vals)
            rows.append((abs(auc-0.5), name, r_mean, k_mean, auc))
        rows.sort(reverse=True)  # strongest signal first
        for sig, name, r_mean, k_mean, auc in rows:
            out_lines.append(f"{name:<35} {r_mean:>10.4f} {k_mean:>10.4f} {sig:>11.4f} (auc={auc:.3f})\n")

    with open('_probe_out.txt', 'w', encoding='utf-8') as f:
        f.writelines(out_lines)


if __name__ == '__main__':
    main()
    print('done')
