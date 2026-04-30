# Chimera Secured - Second Opinion Brief

I'm building an email security product called Chimera Secured that detects impersonation by analyzing writing style. Target customer is SMB-via-MSP channel. I've been working on the scoring methodology with one AI assistant and want your independent read on whether the approach is sound and what I should do next. Please push back on anything that looks wrong.

## Product concept

For each user, we build a Communication Personality Profile (CPP) from their last 12 months of sent email. When a new email is sent or received claiming to be from that user, we score it against their CPP. High deviation = likely impersonation. Runs locally per user; model artifact is a pickle file, ~250KB per user. Privacy-first: email content never leaves the customer's environment; only statistical features persist.

## Core methodology (current)

Per-user discriminative classifier. Not anomaly detection from a mean.

Positive class: user's own sent emails (currently 1,700 training examples from my real Hotmail account).

Negative class ("not-this-user"): 200 synthetic emails generated via LLM across 15 different business personas and 15 topics.

Feature set:
- TF-IDF character n-grams, 3-5 characters, top 2,000 features, sublinear TF
- 150-dimensional function-word frequency vector
- ~20 handcrafted features (sentence length mean/std, word length, punctuation rates, pronoun rates, type-token ratio, log length)

Classifier: scikit-learn logistic regression with class_weight='balanced'.

Score returned is 1 - P(user) so higher = more anomalous.

Pre-processing: aggressive stripping before vectorization. Removes quoted replies, email headers (From:/Sent:/To:/Subject:), signature blocks (-- and --- delimiters), horizontal rules, "Sent from my X" lines, legal/confidentiality footers (CONFIDENTIAL, DISCLAIMER, "This email is intended for", HIPAA, PRIVILEGED, etc.), and inline PII (email addresses, phone numbers, URLs are replaced with placeholder tokens).

## Evaluation harness

- 300 real held-out emails from the user (not seen in training)
- 50 LLM-generated fakes per tier across 3 tiers:
  - zero_shot: attacker prompt is just "write an email as Steve Winfield, channel sales at a cybersecurity startup"
  - few_shot: attacker prompt includes 5 real emails from the user and asks for style-matching imitation
  - high_fidelity: attacker prompt asks for explicit style analysis (sentence length, em-dash usage, greeting pattern, etc.) then generation matching those characteristics
- All fakes generated with claude-sonnet-4.5 via OpenRouter
- Report: ROC AUC per tier, catch rate at 2% false positive rate, overall combined metrics
- Ship gate target: overall AUC >= 0.92 AND catch at 2% FPR >= 0.90

## Iteration 1 results (raw methodology, no stripping)

```
Real held-out (n=300):  mean=0.024  median=0.00  p95=0.084

few_shot       AUC=0.868  catch@2%FPR=42.0%
high_fidelity  AUC=0.897  catch@2%FPR=44.0%
zero_shot      AUC=0.989  catch@2%FPR=92.0%
OVERALL        AUC=0.918  catch@2%FPR=59.3%
```

Diagnostic finding: worst-missed fakes ALL contained a copy of the user's literal signature:
```
---Steve
swinfield@hotmail.com
425-753-8897
```

The LLM attacker was given 5 real emails, learned the signature pattern, and appended it verbatim. The char n-gram classifier had memorized the literal email address and phone number as overwhelming Steve-markers in training, so any fake containing those exact strings was classified as Steve.

## Iteration 2 results (after aggressive stripping of signatures, legal footers, inline PII)

Hypothesis was that fixing the signature leak would close the few_shot and high_fidelity gap. It did the opposite.

```
Real held-out (n=300):  mean=0.054  median=0.0001  p95=0.390

few_shot       AUC=0.831  catch@2%FPR=14.0%
high_fidelity  AUC=0.871  catch@2%FPR=16.0%
zero_shot      AUC=0.963  catch@2%FPR=68.0%
OVERALL        AUC=0.888  catch@2%FPR=32.7%
```

Catch rates collapsed across every tier. Real email mean score moved from 0.024 to 0.054, p95 from 0.084 to 0.390 — meaning the user's own emails now score much more anomalous on average.

Worst-missed zero-shot fake (80 words, no signature, plain English business email) scored 0.000267 — essentially indistinguishable from a real user email despite containing a greeting style ("Hey there, I hope this email finds you well. I wanted to reach out...") the user does not use.

## My current diagnosis (the thing I want you to check)

The iteration 1 classifier was mostly a signature detector dressed up as stylometry. When we stripped signatures, we exposed that there wasn't much style signal underneath. Two root causes:

1. **Negative class too narrow.** 200 LLM-generated synth emails across 15 personas cover a much narrower distribution than the user's 1,700 real emails. With signatures stripped, the classifier learned "diverse writer vs narrow writer" more than "user's style vs other humans' style." This is why stripping hurt — it removed the one signal that was actually doing work.

2. **Short-email problem.** Half of real sent emails (and half of BEC attempts) are 20-40 words. After stripping, a 25-word stripped email doesn't have enough tokens for char n-grams or function-word rates to fingerprint reliably. Style is hard to detect in short bursts.

## My proposed next fixes

**Fix A: Replace synthetic background with Enron corpus.** 500K real emails from 150 real humans. The variance alone should force the classifier to learn genuine style discrimination instead of "diverse vs narrow."

**Fix B: Add an LLM-detector head.** Perplexity and burstiness-based LLM-generated-text detector (DetectGPT-style or a small pre-trained roberta-based detector). Ensemble its score with the discriminative classifier. This specifically targets the fact that ALL the fakes in the eval are LLM-generated, so we have a completely orthogonal signal available.

**Predicted result after both fixes:** overall AUC 0.94-0.96, few_shot and high_fidelity both clear 70% catch at 2% FPR, zero_shot back above 90%.

## Questions I want your honest answer on

1. Is my diagnosis correct? Was the iteration 1 model mostly a signature/PII detector, and is iteration 2 exposing a genuine lack of style signal rather than creating a new problem?

2. Are Fix A (real human background) and Fix B (LLM-detector head) the right next moves, in the right order? Or would you prioritize differently?

3. Am I missing a better approach entirely? Specifically consider:
   - Per-recipient sub-profiles (top N correspondents get their own mini-baseline)
   - Word-level TF-IDF instead of char n-grams (more interpretable, maybe more robust)
   - Sentence-level embeddings (sentence-transformers) for semantic style comparison
   - Negative-space features (things the user NEVER does, as strong signal)
   - POS-tag sequence modeling
   - Some form of length stratification so short emails get different treatment

4. Is the ship gate (AUC 0.92, catch@2%FPR 0.90) realistic given the inherent noisiness of short email stylometry? Or should I be targeting a different metric for SMB deployment?

5. What would you test against that I haven't thought of? The current eval has only LLM-generated fakes. Should I also include:
   - Human-written impersonations (asking people to mimic the target)
   - Replay attacks (real old emails from the user, resent as if new)
   - Partial-copy attacks (attacker reuses parts of real emails, writes the malicious ask in their own style)

Please be direct. If the whole approach is flawed and I should be using a different architecture entirely (e.g., abandoning stylometry and using behavioral/metadata signals, or using a fine-tuned embedding model instead of handcrafted features), say so.

## Relevant constraints

- Must run in under 500ms per scoring call, on commodity hardware, fully offline
- Training should complete in under 5 minutes per user
- Storage budget per user: under 1MB ideally
- Must work with as few as 100 training emails for new users (cold-start problem)
- Must handle users who write differently in different contexts (casual to colleagues, formal to clients)
- Must preserve privacy: no raw email content in any persistent storage outside the training pipeline; only feature vectors and model weights
- Python / numpy / scikit-learn preferred. Can use small pre-trained models. Avoid heavy deep learning frameworks at inference time.
