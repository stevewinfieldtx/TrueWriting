"""Quick diagnostic: do Wave 3 context features separate reals from fakes?
Per-feature AUC on heldout_real vs each fake tier.
"""
import json
import statistics
from recipient_profiler import load_profiles
from context_features import extract_features, FEATURE_NAMES


def roc_auc(scores_pos, scores_neg):
    """Non-lib AUC via Mann-Whitney U."""
    import itertools
    n_pos = len(scores_pos)
    n_neg = len(scores_neg)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    # rank-based
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
    auc = (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc


def load_emails(p):
    with open(p, 'r', encoding='utf-8') as f:
        d = json.load(f)
    # normalize: fakes may be list of dicts with 'body' and 'to', or lists with to missing
    out = []
    for e in d:
        if isinstance(e, dict):
            out.append(e)
        elif isinstance(e, str):
            out.append({'body': e, 'to': '', 'subject': '', 'date': ''})
    return out


def feature_matrix(emails, profiles):
    return [extract_features(e, profiles) for e in emails]


def main():
    profiles = load_profiles('recipient_profiles.json')
    reals = load_emails('eval_splits/heldout_real.json')

    tiers = {}
    for tier in ['zero_shot', 'few_shot', 'high_fidelity']:
        p = f'eval_fakes/{tier}.json'
        tiers[tier] = load_emails(p)

    real_mat = feature_matrix(reals, profiles)
    with open('_probe_out.txt', 'w', encoding='utf-8') as f:
        f.write(f"heldout reals: {len(reals)}\n")
        for tier, emails in tiers.items():
            f.write(f"\n=== {tier}: {len(emails)} fakes ===\n")
            # peek at a fake
            if emails:
                f.write(f"sample fake keys: {list(emails[0].keys())}\n")
                f.write(f"sample fake 'to': {emails[0].get('to', 'NO TO FIELD')!r}\n")
            fake_mat = feature_matrix(emails, profiles)

            f.write(f"{'feature':<35} {'real_mean':>10} {'fake_mean':>10} {'per_feat_auc':>12}\n")
            for fi, name in enumerate(FEATURE_NAMES):
                r_vals = [row[fi] for row in real_mat]
                k_vals = [row[fi] for row in fake_mat]
                r_mean = statistics.fmean(r_vals) if r_vals else 0.0
                k_mean = statistics.fmean(k_vals) if k_vals else 0.0
                # AUC of FAKE>REAL separability (label positive = fake)
                auc = roc_auc(k_vals, r_vals)
                # report how far from 0.5 this feature is
                f.write(f"{name:<35} {r_mean:>10.4f} {k_mean:>10.4f} {auc:>12.4f}\n")


if __name__ == '__main__':
    main()
    print('done')
