# Chimera Secured - Day 1 Runbook

Two files. Four commands. Real number by end of day.

## Setup (one time, ~2 min)

```bash
pip install scikit-learn scipy numpy

# OpenRouter for LLM fake generation + synthetic background
export OPENROUTER_API_KEY=sk-or-...
export OPENROUTER_MODEL_ID=anthropic/claude-sonnet-4.5

# Drop your corpus in this directory as corpus_sent.json
# (or edit CORPUS_PATH in chimera_eval.py if it's elsewhere)
```

## Run it

```bash
# 1. Split your corpus into train / held-out real. Instant.
python chimera_eval.py --stage split

# 2. Build a not-Steve background for the classifier. ~2 min, ~$1.
python chimera_scorer.py --build-background

# 3. Train the discriminative scorer. ~30 sec.
python chimera_scorer.py --train

# 4. Generate LLM fakes at 3 fidelity tiers. ~5 min, ~$2.
python chimera_eval.py --stage fakes

# 5. Score. This is the number that tells you where you stand.
python chimera_eval.py --stage score
```

## What you're looking for

The final output prints AUC and catch rate @ 2% FPR for:
- zero_shot fakes (naive attacker)
- few_shot fakes (attacker with 5 of Steve's emails)
- high_fidelity fakes (sophisticated attacker doing explicit style analysis)
- OVERALL

**Ship gate:** overall AUC >= 0.92 AND catch @ 2% FPR >= 0.90.

Anything below that, do not demo to Rain Networks yet.

## What "good" looks like after Day 1

Expected Day 1 result (methodology fix only, no fancy stuff):
- zero_shot: AUC 0.90-0.97
- few_shot: AUC 0.75-0.85
- high_fidelity: AUC 0.65-0.78
- OVERALL: AUC 0.78-0.87

If you're in that range, the framing fix worked. Day 2 is where we add the LLM-detector head and recipient conditioning to push few_shot and high_fidelity up.

If you're below that range, something is off - likely the background corpus is too
small or too similar to Steve. We'll iterate.

## Troubleshooting

- **"Positive class accuracy is 100%, AUC is 0.5"** — corpus file loaded wrong. Check schema in `_load_corpus` inside `chimera_eval.py`.
- **Background emails look too generic/boring** — swap the synth background for a real
  multi-user corpus (Enron is the standard: `https://www.cs.cmu.edu/~enron/`). Drop
  any list of `{"body": "..."}` dicts into `background_emails.json`.
- **Few-shot fakes scoring too similar to real** — that's not a bug, that's the
  attacker actually beating shallow stylometry. It's why Day 2 adds the LLM-detector
  head and char n-gram work.
