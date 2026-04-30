# Chimera Secured — Wave 2 Drop-In Kit

Two files:

1. `background_generator_swarm.py` — parallelized OpenRouter email generator. Fixes the 3-hour serial bottleneck.
2. `wave2_features.py` — `NegativeSpaceExtractor` + `RecipientConditionalProfiler` + `Wave2FeatureBundle`. This is the Kimi/Z.ai/DeepSeek convergent recommendation items 5 and 6, surgically scoped at the few-shot failure mode we saw in the Wave 1 eval.

Copy both files into your `chimera_secured/` working folder (same folder as `chimera_scorer.py` and `chimera_eval.py`).

---

## Run order

```powershell
# 0. install deps if not already present
pip install xgboost scikit-learn numpy requests joblib

# 1. set your OpenRouter key
$env:OPENROUTER_API_KEY = "sk-or-..."

# 2. regenerate the background at 10x speed (swap workers to 15 if you want to push it)
python background_generator_swarm.py --n 2000 --workers 10
# expected wall time: ~15-20 minutes (was ~3 hours serial)
# Ctrl+C is safe — it checkpoints every 25 completions; re-run with --resume.

# 3. retrain with Wave 2 features wired in (see wire-in below)
python chimera_scorer.py --train

# 4. score the eval set
python chimera_eval.py --stage score
```

---

## Wire-in for `chimera_scorer.py`

The bundle is designed to slot next to your existing feature block with minimal disruption. Two places to change:

### Training path

```python
from wave2_features import Wave2FeatureBundle

# ... after you've built your existing feature matrix X_existing from real + fake ...

# real_samples should be a list of {"body": str, "recipient": str}
# recipient = first To: address, lowercased. If you don't have it handy yet,
# pass "" and the module falls back to the global baseline cleanly.
wave2 = Wave2FeatureBundle()
wave2.fit(real_samples)

X_real_w2 = wave2.transform(real_bodies, real_recipients)
X_fake_w2 = wave2.transform(fake_bodies, fake_recipients)  # fakes: recipient="" is fine

X_train = np.hstack([X_existing_train, np.vstack([X_real_w2, X_fake_w2])])

# persist alongside the scorer
import joblib
joblib.dump(wave2, "wave2_bundle.joblib")
```

### Scoring path

```python
import joblib
from wave2_features import Wave2FeatureBundle

wave2: Wave2FeatureBundle = joblib.load("wave2_bundle.joblib")

def score(body: str, recipient: str = "") -> float:
    x_existing = existing_feature_vec(body)           # your current extractor
    x_wave2    = wave2.transform_one(body, recipient)
    x_full     = np.concatenate([x_existing, x_wave2])
    return classifier.predict_proba(x_full.reshape(1, -1))[0, 1]
```

That's the whole integration. The feature dim grows by **15** (9 negative-space + 6 recipient-conditional). XGBoost handles the extra columns without retuning.

---

## What each feature is actually doing

### Negative-space (9 features)
- `neg_opening_violation` / `neg_opening_2w_violation` — opening phrase never seen in your real corpus. Hits hard on fakes that start with "Hi," or "Hello," or "I hope this finds you well" when you don't.
- `neg_closing_violation` — same idea on the sign-off tail.
- `neg_2gram_violation_rate` / `neg_3gram_violation_rate` / `neg_2gram_violation_count` — n-gram fraction/count of the body that doesn't appear in your real 2-/3-gram vocabulary. Short emails lean on the count; long emails lean on the rate.
- `neg_punct_em_dash_mismatch` / `neg_punct_ellipsis_mismatch` — body uses punctuation tells you never use.
- `neg_violation_score` — weighted composite so XGBoost gets a strong single-signal column to split on early.

### Recipient-conditional (6 features)
- `rec_is_known` — recipient is in your top-N (default top-5).
- `rec_len_z` / `rec_sent_len_z` — z-scores against the *recipient-specific* length and sentence-length baseline (tighter than global).
- `rec_opening_known` — opening phrase is one you typically use with that specific person.
- `rec_em_dash_mismatch` — body uses em-dash but baseline for this recipient doesn't.
- `rec_skepticism` — constant bonus applied when the recipient is rare / unknown and we had to fall through to the global baseline. This is the "treat short emails to rare recipients with more suspicion" behavior.

---

## Why this matches the Wave 1 failure mode

The Wave 1 few-shot misses all scored ~0.0022 — **at the median of real-Steve scores**. The classifier was confidently labeling them as Steve, not uncertain-and-unlucky. The reason: those emails matched Steve's positive patterns (short, direct, Hi-ask-Thanks, ---Steve signature) closely enough that a distribution-based classifier saw them as indistinguishable.

Negative space flips the test: instead of asking "does this look like Steve?", it asks "does this do anything Steve has never done?" — which is a much cheaper signal for an attacker to violate accidentally. Recipient-conditional then tightens the test further by saying "does this look like Steve writing to *this specific person*?", which is where short-email impersonation breaks down for few-shot attackers who don't know the relationship's conversational norms.

Expected Wave 2 delta on few-shot catch @ 2% FPR:
- **Baseline Wave 1**: 46%
- **Wave 2 target**: 65-75%

If we land below 60%, the three reviewers (Kimi/Z.ai/DeepSeek) get re-engaged and we add Wave 2.5 work. If we land at 65%+, we clear the ship gate on all four criteria and the conversation shifts to Wave 3 (tiered actions + DLP content-weighting + expanded eval set).

---

## Swarm tuning notes

| Workers | 2000 emails ETA | OpenRouter behavior |
|---|---|---|
| 1 (serial) | ~170 min | baseline |
| 5 | ~35 min | very safe |
| 10 (default) | ~18 min | sweet spot |
| 15 | ~12 min | some 429s, retries handle it |
| 25+ | ~8 min | diminishing returns, more rate-limit noise |

Stick with 10 unless you have a reason to push harder. Checkpoints save every 25 completions so crashes are cheap.

If OpenRouter rate-limits, you'll see retry messages — the backoff is exponential per-task, so the swarm keeps moving. After the run, any permanently failed ids are listed in `background_emails.ckpt.json.failed_ids`. Re-run with `--retry-failed` to re-queue them.
