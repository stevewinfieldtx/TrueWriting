"""Score heldout reals + all 3 fake tiers. Report AUCs."""
import json, random, statistics
from datetime import datetime, timedelta
from recipient_profiler import load_profiles
from context_scorer import load_model, score_batch


def roc_auc(scores_pos, scores_neg):
    n_pos = len(scores_pos); n_neg = len(scores_neg)
    if n_pos == 0 or n_neg == 0: return 0.5
    combined = [(s, 1) for s in scores_pos] + [(s, 0) for s in scores_neg]
    combined.sort(key=lambda x: x[0])
    ranks = {}; i = 0
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


def catch_at_fpr(fake_scores, real_scores, fpr_target=0.02):
    """At the threshold producing <= fpr_target on reals, what % fakes caught?"""
    reals_sorted = sorted(real_scores, reverse=True)
    cut_idx = int(len(reals_sorted) * fpr_target)
    if cut_idx >= len(reals_sorted):
        return 0.0
    threshold = reals_sorted[cut_idx]
    caught = sum(1 for s in fake_scores if s > threshold)
    return caught / len(fake_scores)


def sample_recipient(profiles, rng):
    items = [(k, v.get('n_sends', 0)) for k, v in profiles.items()
             if k not in ('_meta', '_global') and v.get('n_sends', 0) >= 3
             and k != 'swinfield@hotmail.com']
    if not items: return None
    total = sum(w for _, w in items)
    pick = rng.random() * total
    acc = 0
    for k, w in items:
        acc += w
        if acc >= pick: return k
    return items[-1][0]


def attach_metadata(fakes, profiles, seed=42):
    """Realistic metadata: random recipient (weighted), random time in last 180 days,
    ~50% reply subjects to mirror Steve's baseline."""
    rng = random.Random(seed)
    out = []
    for e in fakes:
        rec = sample_recipient(profiles, rng)
        days_ago = rng.randint(1, 180)
        hour = rng.randint(0, 23)  # uniform hour, no bias
        dt = datetime.utcnow() - timedelta(days=days_ago)
        dt = dt.replace(hour=hour, minute=rng.randint(0, 59), second=0)
        date = dt.isoformat() + 'Z'
        is_reply = rng.random() < 0.5
        subj = ('Re: ' if is_reply else '') + e.get('topic', 'quick question')
        new = dict(e); new['to'] = rec or ''; new['date'] = date; new['subject'] = subj
        out.append(new)
    return out


def main():
    profiles = load_profiles('recipient_profiles.json')
    model = load_model()

    with open('eval_splits/heldout_real.json', 'r', encoding='utf-8') as f:
        reals = json.load(f)
    real_scores = score_batch(reals, profiles, model)

    out = [f"=== Context Scorer — Wave 3 v1 ===\n",
           f"heldout reals: n={len(real_scores)}\n",
           f"  score mean={statistics.fmean(real_scores):.4f} "
           f"median={statistics.median(real_scores):.4f} "
           f"p95={sorted(real_scores)[int(len(real_scores)*0.95)]:.4f}\n\n"]

    for tier in ['zero_shot', 'few_shot', 'high_fidelity']:
        with open(f'eval_fakes/{tier}.json', 'r', encoding='utf-8') as f:
            fakes = json.load(f)
        fakes_md = attach_metadata(fakes, profiles, seed=42)
        fake_scores = score_batch(fakes_md, profiles, model)

        auc = roc_auc(fake_scores, real_scores)
        catch2 = catch_at_fpr(fake_scores, real_scores, 0.02)
        catch5 = catch_at_fpr(fake_scores, real_scores, 0.05)

        out.append(f"--- {tier} (n={len(fakes)}) ---\n")
        out.append(f"  fake score mean={statistics.fmean(fake_scores):.4f} "
                   f"median={statistics.median(fake_scores):.4f}\n")
        out.append(f"  AUC={auc:.4f}\n")
        out.append(f"  catch@2%FPR={catch2:.3f}\n")
        out.append(f"  catch@5%FPR={catch5:.3f}\n")
        # show a few high-scoring fakes and low-scoring fakes
        pairs = sorted(zip(fake_scores, fakes_md), key=lambda x: -x[0])
        out.append(f"  TOP 2 detected fakes (highest context anomaly):\n")
        for s, f in pairs[:2]:
            out.append(f"    {s:.4f}  to={f['to']!r}  body={f['body'][:80]!r}\n")
        out.append(f"  BOTTOM 2 missed fakes (lowest context anomaly):\n")
        for s, f in pairs[-2:]:
            out.append(f"    {s:.4f}  to={f['to']!r}  body={f['body'][:80]!r}\n")
        out.append("\n")

    with open('_probe_out.txt', 'w', encoding='utf-8') as f:
        f.writelines(out)


if __name__ == '__main__':
    main()
    print('done')
