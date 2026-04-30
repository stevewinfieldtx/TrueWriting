"""
Chimera Secured - Lean (Chimera + DLP only) Eval
=================================================

Tests the TWO-LAYER stack: Chimera stylometry + DLP payload scan.
Context layer is dropped entirely (fed as 0.0) because Wave 3 eval showed
it has ~0.57 AUC and drives ~17% false-flag rate on real emails.

This is the Option-C stack from wave3_results_honest_debrief.md.

Run:
  py -3.13 chimera_eval_lean.py
"""

import json
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from recipient_profiler import load_profiles
import chimera_scorer
from dlp_scanner import scan as dlp_scan
from risk_composer import compose


SPLITS_DIR = Path("eval_splits")
FAKES_DIR = Path("eval_fakes")
RESULTS_DIR = Path("eval_results")
TIERS = ["zero_shot", "few_shot", "high_fidelity"]


# ---------- Metrics ----------

def roc_auc(scores_pos, scores_neg):
    n_pos, n_neg = len(scores_pos), len(scores_neg)
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
    sum_pos = sum(ranks[k] for k, (_, lbl) in enumerate(combined) if lbl == 1)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def catch_at_fpr(fake_scores, real_scores, fpr_target=0.02):
    reals_sorted = sorted(real_scores, reverse=True)
    cut_idx = int(len(reals_sorted) * fpr_target)
    if cut_idx >= len(reals_sorted):
        return 0.0
    threshold = reals_sorted[cut_idx]
    caught = sum(1 for s in fake_scores if s > threshold)
    return caught / len(fake_scores) if fake_scores else 0.0


# ---------- Metadata attachment (same seed as v2 for comparability) ----------

def sample_recipient(profiles, rng):
    items = [
        (k, v.get("n_sends", 0))
        for k, v in profiles.items()
        if k not in ("_meta", "_global")
        and v.get("n_sends", 0) >= 3
        and k != "swinfield@hotmail.com"
    ]
    if not items:
        return None
    total = sum(w for _, w in items)
    pick = rng.random() * total
    acc = 0
    for k, w in items:
        acc += w
        if acc >= pick:
            return k
    return items[-1][0]


def attach_metadata(fakes, profiles, seed=42):
    rng = random.Random(seed)
    out = []
    for e in fakes:
        rec = sample_recipient(profiles, rng)
        days_ago = rng.randint(1, 180)
        hour = rng.randint(0, 23)
        dt = datetime.utcnow() - timedelta(days=days_ago)
        dt = dt.replace(hour=hour, minute=rng.randint(0, 59), second=0)
        is_reply = rng.random() < 0.5
        subj = ("Re: " if is_reply else "") + e.get("topic", "quick question")
        new = dict(e)
        new["to"] = rec or ""
        new["date"] = dt.isoformat() + "Z"
        new["subject"] = subj
        out.append(new)
    return out


# ---------- Scoring ----------

def score_chimera(emails):
    scorer = chimera_scorer.load_scorer()
    return [float(chimera_scorer.score(scorer, e["body"])) for e in emails]


def score_dlp(emails):
    return [float(dlp_scan(e.get("body", ""), subject=e.get("subject", "")).score) for e in emails]


def score_composed_lean(chimera_scores, dlp_scores):
    """Compose with context=0 - pure Chimera + DLP."""
    composites, verdicts, reasons = [], [], []
    for c, d in zip(chimera_scores, dlp_scores):
        v = compose(c, context_score=0.0, dlp_score=d)
        composites.append(v.composite_score)
        verdicts.append(v.verdict)
        reasons.append(v.reason)
    return composites, verdicts, reasons


def _verdict_counts(verdicts):
    c = {"pass": 0, "flag": 0, "block": 0}
    for v in verdicts:
        c[v] = c.get(v, 0) + 1
    return c


# ---------- Main ----------

def main():
    RESULTS_DIR.mkdir(exist_ok=True)
    profiles = load_profiles("recipient_profiles.json")

    with open(SPLITS_DIR / "heldout_real.json", "r", encoding="utf-8") as f:
        reals = json.load(f)

    print(f"\nScoring {len(reals)} held-out real emails (Chimera + DLP only)...")
    real_chimera = score_chimera(reals)
    real_dlp = score_dlp(reals)
    real_lean, real_verdicts, real_reasons = score_composed_lean(real_chimera, real_dlp)

    print(f"  Chimera  mean={statistics.fmean(real_chimera):.4f} "
          f"median={statistics.median(real_chimera):.4f} "
          f"p95={sorted(real_chimera)[int(len(real_chimera)*0.95)]:.4f}")
    print(f"  DLP      mean={statistics.fmean(real_dlp):.4f} "
          f"nonzero={sum(1 for x in real_dlp if x > 0)} "
          f">=0.5={sum(1 for x in real_dlp if x >= 0.5)}")
    print(f"  Lean     mean={statistics.fmean(real_lean):.4f} "
          f"median={statistics.median(real_lean):.4f} "
          f"p95={sorted(real_lean)[int(len(real_lean)*0.95)]:.4f}")
    print(f"  Verdicts on reals:  {_verdict_counts(real_verdicts)}")
    reason_counts_real = {}
    for r in real_reasons:
        reason_counts_real[r] = reason_counts_real.get(r, 0) + 1
    print(f"  Reasons on reals:   {reason_counts_real}")

    per_tier = {}
    all_chimera_fakes, all_dlp_fakes, all_lean_fakes = [], [], []

    for tier in TIERS:
        with open(FAKES_DIR / f"{tier}.json", "r", encoding="utf-8") as f:
            fakes = json.load(f)
        fakes_md = attach_metadata(fakes, profiles, seed=42)

        fc = score_chimera(fakes_md)
        fd = score_dlp(fakes_md)
        fl, fv, fr = score_composed_lean(fc, fd)

        all_chimera_fakes.extend(fc)
        all_dlp_fakes.extend(fd)
        all_lean_fakes.extend(fl)

        tier_result = {
            "n": len(fakes_md),
            "chimera": {
                "auc": roc_auc(fc, real_chimera),
                "catch_at_2_fpr": catch_at_fpr(fc, real_chimera, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fc, real_chimera, 0.05),
            },
            "dlp": {
                "auc": roc_auc(fd, real_dlp),
                "catch_at_2_fpr": catch_at_fpr(fd, real_dlp, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fd, real_dlp, 0.05),
            },
            "lean": {
                "auc": roc_auc(fl, real_lean),
                "catch_at_2_fpr": catch_at_fpr(fl, real_lean, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fl, real_lean, 0.05),
            },
            "verdicts": _verdict_counts(fv),
            "reason_distribution": {},
        }
        for r in fr:
            tier_result["reason_distribution"][r] = tier_result["reason_distribution"].get(r, 0) + 1
        per_tier[tier] = tier_result

        print(f"\n--- {tier} (n={len(fakes_md)}) ---")
        print(f"  Chimera  AUC={tier_result['chimera']['auc']:.4f}  catch@2%FPR={tier_result['chimera']['catch_at_2_fpr']:.3f}")
        print(f"  DLP      AUC={tier_result['dlp']['auc']:.4f}  catch@2%FPR={tier_result['dlp']['catch_at_2_fpr']:.3f}")
        print(f"  Lean     AUC={tier_result['lean']['auc']:.4f}  catch@2%FPR={tier_result['lean']['catch_at_2_fpr']:.3f}")
        print(f"  Verdicts: {tier_result['verdicts']}")
        print(f"  Reasons:  {tier_result['reason_distribution']}")

    overall = {
        "chimera": {
            "auc": roc_auc(all_chimera_fakes, real_chimera),
            "catch_at_2_fpr": catch_at_fpr(all_chimera_fakes, real_chimera, 0.02),
        },
        "dlp": {
            "auc": roc_auc(all_dlp_fakes, real_dlp),
            "catch_at_2_fpr": catch_at_fpr(all_dlp_fakes, real_dlp, 0.02),
        },
        "lean": {
            "auc": roc_auc(all_lean_fakes, real_lean),
            "catch_at_2_fpr": catch_at_fpr(all_lean_fakes, real_lean, 0.02),
        },
    }

    print("\n=== OVERALL (all tiers combined) ===")
    print(f"  Chimera  AUC={overall['chimera']['auc']:.4f}  catch@2%FPR={overall['chimera']['catch_at_2_fpr']:.3f}")
    print(f"  DLP      AUC={overall['dlp']['auc']:.4f}  catch@2%FPR={overall['dlp']['catch_at_2_fpr']:.3f}")
    print(f"  Lean     AUC={overall['lean']['auc']:.4f}  catch@2%FPR={overall['lean']['catch_at_2_fpr']:.3f}")

    print("\n--- Ship Gate (composed catch@2%FPR >= 0.85 per tier, false-flag <= 3%) ---")
    gate_rows = []
    all_pass = True
    for tier in TIERS:
        c = per_tier[tier]["lean"]["catch_at_2_fpr"]
        passed = c >= 0.85
        all_pass = all_pass and passed
        gate_rows.append((tier, c, passed))
        print(f"  {tier:15s}  lean catch@2%FPR={c:.3f}  {'PASS' if passed else 'FAIL'} (need >= 0.85)")
    real_flag_block_pct = (len(real_verdicts) - _verdict_counts(real_verdicts)["pass"]) / len(real_verdicts)
    real_flag_pass = real_flag_block_pct <= 0.03
    print(f"  {'real-false-flag':15s}  rate={real_flag_block_pct:.3f}  {'PASS' if real_flag_pass else 'FAIL'} (need <= 0.030)")
    gate_overall = all_pass and real_flag_pass
    print(f"  {'OVERALL':15s}  {'PASS' if gate_overall else 'FAIL'}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "timestamp": ts,
        "eval_type": "lean_chimera_plus_dlp",
        "n_heldout_real": len(reals),
        "real_stats": {
            "verdicts": _verdict_counts(real_verdicts),
            "reasons": reason_counts_real,
            "false_flag_rate": real_flag_block_pct,
        },
        "per_tier": per_tier,
        "overall": overall,
        "ship_gate": {
            "per_tier_pass": {t: p for t, _, p in gate_rows},
            "real_false_flag_pass": real_flag_pass,
            "overall_pass": gate_overall,
        },
    }
    out_path = RESULTS_DIR / f"lean_run_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (RESULTS_DIR / "lean_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
