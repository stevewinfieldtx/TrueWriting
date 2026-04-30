import json, os
with open('_probe_out.txt','w',encoding='utf-8') as f:
    for fn in ['train_real.json','heldout_real.json','attacker_seed_5.json']:
        p = os.path.join('eval_splits', fn)
        if not os.path.exists(p):
            f.write(f"{fn}: MISSING\n"); continue
        d = json.load(open(p,'r',encoding='utf-8'))
        f.write(f"\n=== {fn} ===\n")
        f.write(f"type: {type(d).__name__}, len: {len(d) if hasattr(d,'__len__') else 'n/a'}\n")
        if isinstance(d, list) and d:
            f.write(f"first item keys: {list(d[0].keys()) if isinstance(d[0], dict) else type(d[0]).__name__}\n")
            if isinstance(d[0], dict):
                dates = [e.get('date','') for e in d if isinstance(e, dict)]
                dates = [x for x in dates if x]
                if dates:
                    dates.sort()
                    f.write(f"date range: {dates[0]} .. {dates[-1]}\n")
                tos = set()
                for e in d[:500]:
                    if isinstance(e, dict):
                        t = e.get('to','')
                        for r in str(t).split(','):
                            r = r.strip().lower()
                            if r: tos.add(r)
                f.write(f"unique recipients (first 500 emails): {len(tos)}\n")
        elif isinstance(d, dict):
            f.write(f"keys: {list(d.keys())[:20]}\n")
print("done")
