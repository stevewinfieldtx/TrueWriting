"""
Chimera Secured - Wave 3 Composed-Stack Eval (with DLP wired in)
=================================================================

Measures the COMPOSED stack (Chimera + DLP + Context + Risk Composer).
Reports per-tier and overall AUC and catch@2%FPR for four variants:

  1. Chimera alone        (Wave 1 baseline)
  2. DLP alone            (Wave 2.5 content scanner)
  3. Context alone        (Wave 3 context scorer - mostly dead weight on this eval)
  4. Composed             (all three fused via risk_composer.compose)

Also reports verdict distributions (pass/flag/block) per tier.

Run:
  py -3.13 chimera_eval_v2.py
"""

import json
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from recipient_profiler import load_profiles
from context_scorer import load_model as load_context_model, score_batch as context_score_batch
import chimera_scorer
from dlp_scanner import scan as dlp_scan
from risk_composer import compose


SPLITS_DIR = Path("eval_splits")
FAKES_DIR = Path("eval_fakes")
RESULTS_DIR = Path("eval_results")
TIERS = ["zero_shot", "few_shot", "high_fidelity"]


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


def score_chimera(emails):
    scorer = chimera_scorer.load_scorer()
    return [float(chimera_scorer.score(scorer, e["body"])) for e in emails]


def score_context(emails, profiles, model):
    return [float(s) for s in context_score_batch(emails, profiles, model)]


def score_dlp(emails):
    out = []
    for e in emails:
        r = dlp_scan(e.get("body", ""), subject=e.get("subject", ""))
        out.append(float(r.score))
    return out


def score_composed(chimera_scores, context_scores, dlp_scores):
    composites, verdicts, reasons = [], [], []
    for c, ctx, d in zip(chimera_scores, context_scores, dlp_scores):
        v = compose(c, ctx, d)
        composites.append(v.composite_score)
        verdicts.append(v.verdict)
        reasons.append(v.reason)
    return composites, verdicts, reasons


def _verdict_counts(verdicts):
    c = {"pass": 0, "flag": 0, "block": 0}
    for v in verdicts:
        c[v] = c.get(v, 0) + 1
    return c


def main():
    RESULTS_DIR.mkdir(exist_ok=True)

    profiles = load_profiles("recipient_profiles.json")
    ctx_model = load_context_model()

    with open(SPLITS_DIR / "heldout_real.json", "r", encoding="utf-8") as f:
        reals = json.load(f)

    print(f"\nScoring {len(reals)} held-out real emails...")
    real_chimera = score_chimera(reals)
    real_context = score_context(reals, profiles, ctx_model)
    real_dlp = score_dlp(reals)
    real_composed, real_verdicts, _ = score_composed(real_chimera, real_context, real_dlp)

    print(f"  Chimera   stats:  mean={statistics.fmean(real_chimera):.4f} "
          f"median={statistics.median(real_chimera):.4f} "
          f"p95={sorted(real_chimera)[int(len(real_chimera)*0.95)]:.4f}")
    print(f"  DLP       stats:  mean={statistics.fmean(real_dlp):.4f} "
          f"median={statistics.median(real_dlp):.4f} "
          f"p95={sorted(real_dlp)[int(len(real_dlp)*0.95)]:.4f}  "
          f"n_nonzero={sum(1 for x in real_dlp if x > 0)}")
    print(f"  Context   stats:  mean={statistics.fmean(real_context):.4f} "
          f"median={statistics.median(real_context):.4f} "
          f"p95={sorted(real_context)[int(len(real_context)*0.95)]:.4f}")
    print(f"  Composed  stats:  mean={statistics.fmean(real_composed):.4f} "
          f"median={statistics.median(real_composed):.4f} "
          f"p95={sorted(real_composed)[int(len(real_composed)*0.95)]:.4f}")
    print(f"  Verdicts on reals:  {_verdict_counts(real_verdicts)}  (flag/block = false-flag)")

    per_tier = {}
    all_chimera_fakes, all_context_fakes, all_dlp_fakes, all_composed_fakes = [], [], [], []

    for tier in TIERS:
        tier_path = FAKES_DIR / f"{tier}.json"
        with open(tier_path, "r", encoding="utf-8") as f:
            fakes = json.load(f)
        fakes_md = attach_metadata(fakes, profiles, seed=42)

        fake_chimera = score_chimera(fakes_md)
        fake_context = score_context(fakes_md, profiles, ctx_model)
        fake_dlp = score_dlp(fakes_md)
        fake_composed, fake_verdicts, fake_reasons = score_composed(
            fake_chimera, fake_context, fake_dlp
        )

        all_chimera_fakes.extend(fake_chimera)
        all_context_fakes.extend(fake_context)
        all_dlp_fakes.extend(fake_dlp)
        all_composed_fakes.extend(fake_composed)

        tier_result = {
            "n": len(fakes_md),
            "chimera": {
                "auc": roc_auc(fake_chimera, real_chimera),
                "catch_at_2_fpr": catch_at_fpr(fake_chimera, real_chimera, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fake_chimera, real_chimera, 0.05),
            },
            "dlp": {
                "auc": roc_auc(fake_dlp, real_dlp),
                "catch_at_2_fpr": catch_at_fpr(fake_dlp, real_dlp, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fake_dlp, real_dlp, 0.05),
                "mean": statistics.fmean(fake_dlp),
                "median": statistics.median(fake_dlp),
                "n_nonzero": sum(1 for x in fake_dlp if x > 0),
            },
            "context": {
                "auc": roc_auc(fake_context, real_context),
                "catch_at_2_fpr": catch_at_fpr(fake_context, real_context, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fake_context, real_context, 0.05),
            },
            "composed": {
                "auc": roc_auc(fake_composed, real_composed),
                "catch_at_2_fpr": catch_at_fpr(fake_composed, real_composed, 0.02),
                "catch_at_5_fpr": catch_at_fpr(fake_composed, real_composed, 0.05),
            },
            "verdicts": _verdict_counts(fake_verdicts),
            "reason_distribution": {},
        }
        for r in fake_reasons:
            tier_result["reason_distribution"][r] = tier_result["reason_distribution"].get(r, 0) + 1
        per_tier[tier] = tier_result

        print(f"\n--- {tier} (n={len(fakes_md)}) ---")
        print(f"  Chimera   AUC={tier_result['chimera']['auc']:.4f}  "
              f"catch@2%FPR={tier_result['chimera']['catch_at_2_fpr']:.3f}")
        print(f"  DLP       AUC={tier_result['dlp']['auc']:.4f}  "
              f"catch@2%FPR={tier_result['dlp']['catch_at_2_fpr']:.3f}  "
              f"(mean={tier_result['dlp']['mean']:.3f}, nonzero={tier_result['dlp']['n_nonzero']}/{len(fakes_md)})")
        print(f"  Context   AUC={tier_result['context']['auc']:.4f}  "
              f"catch@2%FPR={tier_result['context']['catch_at_2_fpr']:.3f}")
        print(f"  Composed  AUC={tier_result['composed']['auc']:.4f}  "
              f"catch@2%FPR={tier_result['composed']['catch_at_2_fpr']:.3f}")
        print(f"  Verdicts:  {tier_result['verdicts']}")
        print(f"  Reasons:   {tier_result['reason_distribution']}")

    overall = {
        "chimera": {
            "auc": roc_auc(all_chimera_fakes, real_chimera),
            "catch_at_2_fpr": catch_at_fpr(all_chimera_fakes, real_chimera, 0.02),
        },
        "dlp": {
            "auc": roc_auc(all_dlp_fakes, real_dlp),
            "catch_at_2_fpr": catch_at_fpr(all_dlp_fakes, real_dlp, 0.02),
        },
        "context": {
            "auc": roc_auc(all_context_fakes, real_context),
            "catch_at_2_fpr": catch_at_fpr(all_context_fakes, real_context, 0.02),
        },
        "composed": {
            "auc": roc_auc(all_composed_fakes, real_composed),
            "catch_at_2_fpr": catch_at_fpr(all_composed_fakes, real_composed, 0.02),
        },
    }

    print("\n=== OVERALL (all tiers combined) ===")
    print(f"  Chimera   AUC={overall['chimera']['auc']:.4f}  "
          f"catch@2%FPR={overall['chimera']['catch_at_2_fpr']:.3f}")
    print(f"  DLP       AUC={overall['dlp']['auc']:.4f}  "
          f"catch@2%FPR={overall['dlp']['catch_at_2_fpr']:.3f}")
    print(f"  Context   AUC={overall['context']['auc']:.4f}  "
          f"catch@2%FPR={overall['context']['catch_at_2_fpr']:.3f}")
    print(f"  Composed  AUC={overall['composed']['auc']:.4f}  "
          f"catch@2%FPR={overall['composed']['catch_at_2_fpr']:.3f}")

    print("\n--- Wave 3 Ship Gate ---")
    gate_rows = []
    all_pass = True
    for tier in TIERS:
        c = per_tier[tier]["composed"]["catch_at_2_fpr"]
        passed = c >= 0.85
        all_pass = all_pass and passed
        gate_rows.append((tier, c, passed))
        print(f"  {tier:15s}  composed catch@2%FPR={c:.3f}  "
              f"{'PASS' if passed else 'FAIL'} (need >= 0.85)")
    real_flag_block_pct = (len(real_verdicts) - _verdict_counts(real_verdicts)["pass"]) / len(real_verdicts)
    real_flag_pass = real_flag_block_pct <= 0.03
    print(f"  {'real-false-flag':15s}  rate={real_flag_block_pct:.3f}  "
          f"{'PASS' if real_flag_pass else 'FAIL'} (need <= 0.030)")
    gate_overall = all_pass and real_flag_pass
    print(f"  {'OVERALL':15s}  {'PASS' if gate_overall else 'FAIL'}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "timestamp": ts,
        "wave": 3,
        "eval_type": "composed_stack_with_dlp",
        "n_heldout_real": len(reals),
        "real_stats": {
            "chimera": {
                "mean": statistics.fmean(real_chimera),
                "median": statistics.median(real_chimera),
                "p95": sorted(real_chimera)[int(len(real_chimera) * 0.95)],
            },
            "dlp": {
                "mean": statistics.fmean(real_dlp),
                "median": statistics.median(real_dlp),
                "p95": sorted(real_dlp)[int(len(real_dlp) * 0.95)],
                "n_nonzero": sum(1 for x in real_dlp if x > 0),
            },
            "context": {
                "mean": statistics.fmean(real_context),
                "median": statistics.median(real_context),
                "p95": sorted(real_context)[int(len(real_context) * 0.95)],
            },
            "composed": {
                "mean": statistics.fmean(real_composed),
                "median": statistics.median(real_composed),
                "p95": sorted(real_composed)[int(len(real_composed) * 0.95)],
            },
            "verdicts": _verdict_counts(real_verdicts),
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
    out_path = RESULTS_DIR / f"wave3_dlp_run_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (RESULTS_DIR / "wave3_latest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(f"\nSaved: {out_path}")
    print(f"Always-latest: {RESULTS_DIR / 'wave3_latest.json'}")


if __name__ == "__main__":
    main()
