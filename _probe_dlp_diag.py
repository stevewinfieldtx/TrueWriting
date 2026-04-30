"""Diagnose DLP false positives on real emails."""
import json
from dlp_scanner import scan

with open('eval_splits/heldout_real.json', 'r', encoding='utf-8') as f:
    reals = json.load(f)

scored = []
for i, e in enumerate(reals):
    r = scan(e.get('body', ''), subject=e.get('subject', ''))
    if r.score > 0:
        scored.append((r.score, i, e, r))

scored.sort(reverse=True)

lines = []
lines.append(f"DLP fired on {len(scored)}/{len(reals)} real emails ({100*len(scored)/len(reals):.1f}%)\n")
lines.append(f"Score distribution: >=0.9: {sum(1 for s,_,_,_ in scored if s>=0.9)}, "
             f">=0.7: {sum(1 for s,_,_,_ in scored if s>=0.7)}, "
             f">=0.5: {sum(1 for s,_,_,_ in scored if s>=0.5)}, "
             f"any: {len(scored)}\n\n")

# Top 20 highest-scoring reals
lines.append("=== TOP 20 REAL EMAILS FIRING DLP (FALSE POSITIVES) ===\n\n")
for score, idx, email, result in scored[:20]:
    body = email.get('body', '')[:250]
    subj = email.get('subject', '')[:80]
    cats = list(result.category_weights.keys())
    hits_str = "; ".join(f"{h['category']}:'{h['match']}'" for h in result.hits[:4])
    lines.append(f"[{idx}] score={score:.3f}  cats={cats}\n")
    lines.append(f"    subj: {subj!r}\n")
    lines.append(f"    hits: {hits_str}\n")
    lines.append(f"    body: {body!r}\n\n")

# Tally category hits on reals
cat_counts = {}
for _, _, _, r in scored:
    for c in r.category_weights:
        cat_counts[c] = cat_counts.get(c, 0) + 1
lines.append(f"\nCategory hit counts on reals:\n")
for c, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
    lines.append(f"  {c:25s}  {n}\n")

with open('_probe_dlp_fp.txt', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print(f"Wrote _probe_dlp_fp.txt  ({len(scored)} reals fired)")
