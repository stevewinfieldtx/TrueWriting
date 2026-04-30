import json, sys
d = json.load(open('corpus_sent.json','r',encoding='utf-8'))
with open('_probe_out.txt','w',encoding='utf-8') as f:
    f.write(f"n={len(d)}\n")
    f.write(f"keys={list(d[0].keys())}\n")
    for i in range(3):
        f.write(f"\n--- sample {i} ---\n")
        for k,v in d[i].items():
            s = str(v)[:200]
            f.write(f"{k}: {s}\n")
print("done")
