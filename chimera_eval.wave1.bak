"""
Chimera Secured - Evaluation Harness
=====================================

This is the measurement tool. Nothing else you build matters until this exists.
It does four things:
  1. Loads your corpus and splits it into train / held-out real emails
  2. Loads (or fetches) a background corpus of other people's emails (not-Steve)
  3. Generates LLM-crafted fake emails at three fidelity levels via OpenRouter
  4. Produces a proper ROC curve, AUC, and catch rate @ 2% FPR

Run order:
  python chimera_eval.py --stage split       # One-time, creates data splits
  python chimera_eval.py --stage fakes       # Generates LLM fakes (costs a few $)
  python chimera_eval.py --stage score       # Scores a trained model and prints metrics

Assumptions about your corpus_sent.json:
  - It's a list of dicts
  - Each dict has at least a "body" field (string) and optionally "subject", "to", "date"
  - If your schema differs, edit _load_corpus() below

Environment variables required:
  OPENROUTER_API_KEY      - for LLM fake generation
  OPENROUTER_MODEL_ID     - e.g. "anthropic/claude-sonnet-4.5" or "openai/gpt-4o"
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import List, Dict, Any

import numpy as np

RANDOM_SEED = 42
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

# ---------- Configuration ----------
CORPUS_PATH = Path("corpus_sent.json")
SPLITS_DIR = Path("eval_splits")
FAKES_DIR = Path("eval_fakes")
HELDOUT_REAL_COUNT = 300          # Real Steve emails held out for eval
TRAIN_REAL_COUNT = None            # None = use all remaining
FAKES_PER_TIER = 50                # zero-shot, few-shot, fine-tuned-sim
MIN_WORDS = 20                     # Skip emails shorter than this

# ---------- Corpus loading ----------

def _load_corpus(path: Path) -> List[Dict[str, Any]]:
    """Load corpus. Edit this if your schema differs."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Accept either a list or {"messages": [...]}
    if isinstance(data, dict):
        data = data.get("messages") or data.get("emails") or []
    # Normalize
    out = []
    for row in data:
        body = row.get("body") or row.get("body_text") or row.get("text") or ""
        if not isinstance(body, str):
            continue
        word_count = len(body.split())
        if word_count < MIN_WORDS:
            continue
        out.append({
            "body": body,
            "subject": row.get("subject", ""),
            "to": row.get("to", row.get("recipient", "")),
            "date": row.get("date", row.get("sent_date", "")),
            "word_count": word_count,
        })
    return out


# ---------- Stage 1: Split ----------

def stage_split() -> None:
    SPLITS_DIR.mkdir(exist_ok=True)
    corpus = _load_corpus(CORPUS_PATH)
    print(f"Loaded {len(corpus)} usable Steve emails (>= {MIN_WORDS} words).")

    random.shuffle(corpus)
    heldout = corpus[:HELDOUT_REAL_COUNT]
    train = corpus[HELDOUT_REAL_COUNT:]
    if TRAIN_REAL_COUNT is not None:
        train = train[:TRAIN_REAL_COUNT]

    with open(SPLITS_DIR / "train_real.json", "w") as f:
        json.dump(train, f, indent=2)
    with open(SPLITS_DIR / "heldout_real.json", "w") as f:
        json.dump(heldout, f, indent=2)

    # Seed emails for few-shot LLM mimicry (the attacker's reconnaissance)
    seed = random.sample(train, min(5, len(train)))
    with open(SPLITS_DIR / "attacker_seed_5.json", "w") as f:
        json.dump(seed, f, indent=2)

    print(f"Train real: {len(train)}  |  Held-out real: {len(heldout)}")
    print(f"Attacker seed (5 samples the attacker 'has'): saved.")
    print("Next: python chimera_eval.py --stage fakes")


# ---------- Stage 2: LLM fake generation ----------

FAKE_TIERS = {
    "zero_shot": {
        "description": "Attacker knows almost nothing. Just 'write an email as Steve Winfield.'",
        "use_seed": False,
        "system_prompt": (
            "You are writing a business email as Steve Winfield. "
            "Steve works in channel sales for a cybersecurity startup and communicates "
            "with MSPs and vendors. Write naturally, as he would."
        ),
    },
    "few_shot": {
        "description": "Attacker has 5 real emails from Steve (e.g., from a breach or forwarded chain).",
        "use_seed": True,
        "system_prompt": (
            "You are impersonating Steve Winfield in an email. You have access to 5 real emails "
            "he sent. Study his writing style carefully - sentence length, punctuation, greeting/closing "
            "patterns, vocabulary, contractions, tone - and write a new email that would be "
            "indistinguishable from his real writing. The email's topic is different from the samples "
            "but the voice must match exactly."
        ),
    },
    "high_fidelity": {
        "description": "Attacker has studied Steve deeply and uses explicit style analysis.",
        "use_seed": True,
        "system_prompt": (
            "You are an expert forensic impersonator. You have 5 emails from Steve Winfield. "
            "Before writing, silently analyze: his mean sentence length, his use of em-dashes vs "
            "semicolons, his greeting style (Hi/Hey/Hello/none), his sign-off pattern, his use of "
            "contractions, his passive/active voice ratio, his typical paragraph length, and any "
            "idiosyncratic phrases or errors. Then write a new email of similar length and formality "
            "that matches every one of these characteristics. Do not reveal your analysis, just write "
            "the email."
        ),
    },
}

# Topics the attacker would plausibly write about (BEC scenarios)
FAKE_TOPICS = [
    "Urgent request to update vendor payment details before Friday's payroll run",
    "Quick favor - can you process a wire transfer for the Q4 acquisition?",
    "Need you to buy $500 in gift cards for client appreciation ASAP",
    "Please review the attached invoice and confirm approval today",
    "Change in banking details for our largest supplier, effective immediately",
    "Confidential project kickoff - please don't discuss with the team yet",
    "Following up on the pricing proposal I sent last week",
    "Can you send me the contact info for the MSP lead from yesterday's call?",
    "I need the Q3 financials forwarded to an external auditor by EOD",
    "Approve this expense report on my behalf - I'm on a flight",
    "Moving the Tuesday meeting - please update the calendar and let the team know",
    "Heads up - expect a call from our legal team tomorrow about the contract",
    "Can you get the partner agreement signed and back to vendor by Thursday?",
    "Draft a response to the channel manager proposal I'll forward shortly",
    "Please send the team roster to HR - they need it for compliance reporting",
]


def _openrouter_call(system_prompt: str, user_prompt: str) -> str:
    """Make one call to OpenRouter. Returns generated text."""
    import urllib.request
    import urllib.error

    api_key = os.environ.get("OPENROUTER_API_KEY")
    model_id = os.environ.get("OPENROUTER_MODEL_ID")
    if not api_key or not model_id:
        raise RuntimeError(
            "Set OPENROUTER_API_KEY and OPENROUTER_MODEL_ID env vars before running 'fakes' stage."
        )

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


def stage_fakes() -> None:
    FAKES_DIR.mkdir(exist_ok=True)
    seed_path = SPLITS_DIR / "attacker_seed_5.json"
    if not seed_path.exists():
        sys.exit("Run --stage split first.")
    with open(seed_path) as f:
        seed = json.load(f)
    seed_text = "\n\n---\n\n".join(
        f"From: Steve\nSubject: {s.get('subject','(no subject)')}\n\n{s['body']}" for s in seed
    )

    for tier_name, tier in FAKE_TIERS.items():
        print(f"\n=== Generating {FAKES_PER_TIER} fakes: tier={tier_name} ===")
        print(f"    {tier['description']}")
        out = []
        for i in range(FAKES_PER_TIER):
            topic = FAKE_TOPICS[i % len(FAKE_TOPICS)]
            if tier["use_seed"]:
                user_prompt = (
                    f"Reference emails from Steve:\n\n{seed_text}\n\n"
                    f"Now write a new email from Steve on this topic: {topic}\n\n"
                    f"Return ONLY the email body, no subject line, no 'From:' header."
                )
            else:
                user_prompt = (
                    f"Write a new email from Steve on this topic: {topic}\n\n"
                    f"Return ONLY the email body, no subject line, no 'From:' header."
                )
            try:
                text = _openrouter_call(tier["system_prompt"], user_prompt)
            except Exception as e:
                print(f"    [{i+1}/{FAKES_PER_TIER}] ERROR: {e}")
                continue
            out.append({"body": text, "topic": topic, "tier": tier_name})
            print(f"    [{i+1}/{FAKES_PER_TIER}] {len(text.split())} words")
        with open(FAKES_DIR / f"{tier_name}.json", "w") as f:
            json.dump(out, f, indent=2)
        print(f"    Saved {len(out)} fakes to {FAKES_DIR / (tier_name + '.json')}")

    print("\nNext: train a scorer, then: python chimera_eval.py --stage score")


# ---------- Stage 3: Score a trained model ----------

RESULTS_DIR = Path("eval_results")


def stage_score(scorer_module: str = "chimera_scorer") -> None:
    """
    Loads a trained scorer and evaluates it on held-out real + all fake tiers.
    Writes a timestamped JSON to eval_results/ AND prints to terminal.
    """
    try:
        from sklearn.metrics import roc_auc_score, roc_curve
    except ImportError:
        sys.exit("pip install scikit-learn numpy")
    from datetime import datetime

    RESULTS_DIR.mkdir(exist_ok=True)
    mod = __import__(scorer_module)
    scorer = mod.load_scorer()

    # Load eval data
    with open(SPLITS_DIR / "heldout_real.json") as f:
        heldout_real = json.load(f)

    tier_files = list(FAKES_DIR.glob("*.json"))
    if not tier_files:
        sys.exit("No fakes found. Run --stage fakes first.")

    # Score real
    real_scores = np.array([mod.score(scorer, e["body"]) for e in heldout_real])
    print(f"\nReal held-out (n={len(real_scores)}):  "
          f"mean={real_scores.mean():.3f}  median={np.median(real_scores):.3f}  "
          f"p95={np.percentile(real_scores, 95):.3f}")

    # Per-tier + combined fake scoring
    all_fake_scores = []
    print("\nPer-tier fake scoring:")
    per_tier_results = {}
    worst_examples = {}
    for tf in sorted(tier_files):
        with open(tf) as f:
            fakes = json.load(f)
        fs = np.array([mod.score(scorer, e["body"]) for e in fakes])
        all_fake_scores.extend(fs.tolist())

        y = np.concatenate([np.zeros(len(real_scores)), np.ones(len(fs))])
        s = np.concatenate([real_scores, fs])
        auc = float(roc_auc_score(y, s))
        fpr, tpr, thr = roc_curve(y, s)
        idx = np.searchsorted(fpr, 0.02, side="right") - 1
        idx = max(0, min(idx, len(tpr) - 1))
        catch_at_2 = float(tpr[idx])
        per_tier_results[tf.stem] = {"auc": auc, "catch_at_2_fpr": catch_at_2, "n": len(fs)}
        print(f"  {tf.stem:20s}  n={len(fs):3d}  AUC={auc:.3f}  catch@2%FPR={catch_at_2:.1%}")

        # Capture the 3 fakes that scored LOWEST (the ones that fooled us most)
        sorted_idx = np.argsort(fs)
        worst_examples[tf.stem] = [
            {"score": float(fs[i]), "body": fakes[i]["body"][:400]}
            for i in sorted_idx[:3]
        ]

    # Overall
    all_fake_scores = np.array(all_fake_scores)
    y = np.concatenate([np.zeros(len(real_scores)), np.ones(len(all_fake_scores))])
    s = np.concatenate([real_scores, all_fake_scores])
    overall_auc = float(roc_auc_score(y, s))
    fpr, tpr, thr = roc_curve(y, s)
    idx = np.searchsorted(fpr, 0.02, side="right") - 1
    idx = max(0, min(idx, len(tpr) - 1))
    overall_catch = float(tpr[idx])
    overall_thr = float(thr[idx])
    print(f"\nOVERALL  AUC={overall_auc:.3f}  catch@2%FPR={overall_catch:.1%}  "
          f"(threshold={overall_thr:.3f})")

    # Ship gate
    print("\n--- Ship gate ---")
    gate_pass = overall_auc >= 0.92 and overall_catch >= 0.90
    tier_pass_map = {}
    for tier, r in per_tier_results.items():
        tp = r["auc"] >= 0.85 and r["catch_at_2_fpr"] >= 0.80
        tier_pass_map[tier] = tp
        print(f"  {tier:20s}  {'PASS' if tp else 'FAIL'}")
    print(f"  {'OVERALL':20s}  {'PASS' if gate_pass else 'FAIL'}")
    if not gate_pass:
        print("\nDo not demo to MSPs yet. Keep iterating.")
    else:
        print("\nThis is a demo-worthy number. Ship it to Rain Networks.")

    # ---- Save full results ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = {
        "timestamp": ts,
        "scorer_module": scorer_module,
        "n_heldout_real": len(real_scores),
        "real_score_stats": {
            "mean": float(real_scores.mean()),
            "median": float(np.median(real_scores)),
            "std": float(real_scores.std()),
            "p95": float(np.percentile(real_scores, 95)),
        },
        "per_tier": per_tier_results,
        "overall": {
            "auc": overall_auc,
            "catch_at_2_fpr": overall_catch,
            "suggested_threshold": overall_thr,
        },
        "ship_gate": {
            "overall_pass": gate_pass,
            "per_tier_pass": tier_pass_map,
        },
        "worst_fakes_we_missed": worst_examples,
    }
    out_path = RESULTS_DIR / f"run_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2))
    # Also keep a pointer to the most recent run
    (RESULTS_DIR / "latest.json").write_text(json.dumps(results, indent=2))
    print(f"\nResults saved: {out_path}")
    print(f"Always-latest: {RESULTS_DIR / 'latest.json'}")


# ---------- Main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["split", "fakes", "score"])
    ap.add_argument("--scorer", default="chimera_scorer",
                    help="Python module exposing load_scorer() and score()")
    args = ap.parse_args()

    if args.stage == "split":
        stage_split()
    elif args.stage == "fakes":
        stage_fakes()
    elif args.stage == "score":
        stage_score(args.scorer)


if __name__ == "__main__":
    main()
