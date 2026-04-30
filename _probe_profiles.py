import json
from recipient_profiler import load_profiles

profs = load_profiles('recipient_profiles.json')
with open('_probe_out.txt', 'w', encoding='utf-8') as f:
    f.write("=== META ===\n")
    f.write(json.dumps(profs['_meta'], indent=2) + "\n\n")

    # top-10 most-frequent recipients
    ranked = [(k, p.get('n_sends', 0)) for k, p in profs.items()
              if k not in ('_meta', '_global')]
    ranked.sort(key=lambda x: -x[1])
    f.write("=== TOP 10 RECIPIENTS BY SEND COUNT ===\n")
    for r, n in ranked[:10]:
        f.write(f"  {r}: {n} sends\n")
    f.write("\n")

    f.write("=== GLOBAL PROFILE (abridged) ===\n")
    g = profs['_global']
    f.write(f"n_sends={g['n_sends']} is_known={g['is_known']}\n")
    f.write(f"word_count: {g['word_count_stats']}\n")
    f.write(f"reply_rate: {g['reply_rate']:.3f}\n")
    f.write(f"top_greetings: {g['top_greetings']}\n")
    f.write(f"top_closings: {g['top_closings']}\n")
    f.write(f"em_dash_per_100w: {g['em_dash_per_100w_stats']}\n")
    f.write(f"ellipsis_per_100w: {g['ellipsis_per_100w_stats']}\n")
    f.write(f"exclaim_per_100w: {g['exclaim_per_100w_stats']}\n")
    peaks = sorted(enumerate(g['hour_hist']), key=lambda x: -x[1])[:5]
    f.write(f"top send hours (UTC): {peaks}\n")
    f.write("\n")

    # look at top recipient's profile for texture
    if ranked:
        top = ranked[0][0]
        p = profs[top]
        f.write(f"=== TOP RECIPIENT: {top} (n={p['n_sends']}) ===\n")
        f.write(f"word_count: {p['word_count_stats']}\n")
        f.write(f"top_greetings: {p['top_greetings']}\n")
        f.write(f"top_closings: {p['top_closings']}\n")
        f.write(f"reply_rate: {p['reply_rate']:.3f}\n")
        f.write(f"em_dash_per_100w: {p['em_dash_per_100w_stats']}\n")
print("done")
