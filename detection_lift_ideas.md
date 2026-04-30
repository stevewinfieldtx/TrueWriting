# Closing the Gap — Creative Detection Lifts for Chimera Secured

*Steve, this is my honest read after going through the code and docs. The constraint I'm holding throughout: nothing leaves the customer environment except the CPP itself. Every technique below either runs locally at scoring time or enriches the CPP during training. No email body ever crosses a boundary.*

---

## The mental model that already lives in your head

Your "7-word credit-card ask vs. 9-word lunch ask" example is doing something more precise than weighting. It's Bayesian reasoning, and it deserves to be made explicit because once you write it down this way, it tells you exactly how to spend the rest of your engineering effort.

A BEC detector is really answering one question: given everything we observe, what's the probability this message is fake?

    P(fake | signals)  ∝  P(signals | fake)  ×  P(fake | context)

The left-hand term is what stylometry, DLP patterns, perplexity, and metadata all contribute — how unusual do the signals look *if the message were fake*. That's the part you've been obsessing over, and correctly.

The right-hand term — the prior — is what DLP was trying to surface when you added it. It's the answer to "how unusual is it for an email of this kind to be fake in the first place?" And this is where your lunch/credit-card intuition lives mathematically:

| Content category                  | Prior P(fake \| category) |
|-----------------------------------|---------------------------|
| Casual conversation, scheduling   | ~0.0001                   |
| Internal status update            | ~0.001                    |
| Small invoice / routine expense   | ~0.01                     |
| Wire transfer request             | ~0.05                     |
| Banking detail change             | ~0.30                     |
| Gift-card request                 | ~0.40                     |
| Credential / password / MFA ask   | ~0.60                     |

The numbers are calibration — you'd learn them from the joint distribution of `{real corpus, known BEC corpus}` — but the structure is not a guess. These priors are genuinely how BEC distributes across content types in the wild.

Now watch what this does to Steve's two examples:

- **9-word lunch ask, style anomaly 0.80.** Prior is 0.0001. Posterior is 0.0001 × likelihood-ratio. Even with a gigantic style anomaly, the posterior stays near zero. Silent pass. This is what your gut was telling you and your tier ladder currently approximates.

- **7-word credit-card ask, style anomaly 0.20.** Prior is 0.40 (credit card is in the gift-card-adjacent cluster). Even a weak style anomaly multiplies against a strong prior. Posterior jumps above block threshold. This is also what your gut wants, and the *current* composer misses it because 0.20 stylometry alone doesn't exceed `CHIMERA_MID=0.007`.

The payoff of this reframing is not just philosophical. It gives you:

1. **Calibrated probabilities**, which means admins can set thresholds in units they understand ("block above 70% fake-probability"), instead of tuning an opaque score.
2. **One decision layer**, so you stop having to maintain if/else ladders in `risk_composer.py` as you add new signals.
3. **A principled place to put new detectors.** Every new signal contributes to the likelihood ratio in a clean way.
4. **Content-risk weighting falls out of the math**, not out of hand-coded weights. The Kimi report's arbitrary-weights critique goes away because you no longer have arbitrary weights.

Concretely: replace the current 6-tier `compose()` function with a log-posterior calculation that takes a content-category prior and a set of likelihood ratios (one per detector). Total code change: probably 80 lines. It's the single highest-leverage refactor I see.

---

## Six creative levers you haven't pulled yet

Ordered roughly by expected lift on few-shot and high-fidelity catch, which is where your ship gate is failing.

### 1. Perplexity + burstiness head (the "LLM tell" detector)

Your second-opinion brief named this as Fix B and never shipped it. It's still the single cheapest way to add meaningful catch on LLM-generated fakes — which is 100% of what your eval currently tests against.

The mechanism: LLM text has **lower sentence-level perplexity variance** than human text. Humans are bursty — we write a tight sentence, then a long rambling one, then a fragment. LLMs produce more uniform sentences. Run a small local language model (GPT-2 medium fits in ~350MB and runs in well under 500ms on commodity CPUs), compute perplexity per sentence, compute the variance, and feed that as a feature.

DetectGPT and Binoculars are the published techniques; GPT-2 XL as the reference model is standard. No API calls, fully offline, one-time model download during deploy.

**Expected lift:** few-shot catch +15 to +25 points; high-fidelity catch +10 to +15. Zero-shot lift minimal because perplexity on zero-shot fakes is already obviously off.

**Sovereign check:** ✓ model runs locally.

### 2. Relational physics — per-pair baselines

Your Wave 2 recipient-conditional features only capture *style* per recipient (length, opening patterns). The bigger signal is *relational content*: across your history of emails between Steve and Nathan, which **content categories** have ever appeared? If Steve-to-Nathan has a history of 200 emails across scheduling, deal updates, and casual back-and-forth, and a new email contains a wire-transfer ask — the *relational* prior on "Steve asks Nathan to wire money" is effectively zero. That's a massive detection signal that requires no stylometry at all.

The model: for each (sender, recipient) pair in the sent corpus, maintain a multinomial over content categories. At scoring time, P(category | this pair) gives you a relationship-specific prior that overrides the global prior. A wire-ask from Steve to his CFO is priored differently than a wire-ask from Steve to his college roommate.

The attacker defense property is exquisite here. The few-shot attacker has five of Steve's emails. They probably aren't all to the same person. Even if they are, the attacker doesn't know which conversations with which people have never included financial content. This is information the attacker cannot get from a small sample of outbound mail.

**Expected lift:** few-shot catch +10 to +20 points, especially on financial/credential tiers. Little effect on casual-topic fakes.

**Sovereign check:** ✓ per-pair histograms live in the CPP; 50 bytes per pair × a few hundred pairs = tiny.

### 3. Ask-type novelty

A specific case of relational physics worth calling out by itself because it's even simpler to implement. Classify every outbound email into one of ~15 intent categories during CPP training (`schedule_meeting`, `status_update`, `ask_favor`, `send_money`, `request_money`, `share_document`, `introduce_person`, etc.). For each user, record which intents they have *ever originated* across their entire sent corpus.

If a new outbound email has an intent the user has never originated — for example, they've never in 12 months asked anyone to send crypto — that's a categorical novelty flag. Not stylometry. Pure behavioral intent.

This is also where the DLP hard categories should be wired: if an intent is "banking_change" and the sender has never previously asked for banking changes, amplify the prior hard.

**Expected lift:** high on the first BEC an attacker sends (they're usually asking for something the user never asks for). Modest on the subsequent ones once the model has seen one.

**Sovereign check:** ✓ intent counts per user in the CPP.

### 4. Thread-coherence signal

Most BEC emails either start a new thread or hijack an existing thread with an off-topic ask. Check whether the new message **fits** the thread it claims to belong to.

Mechanism: small local sentence embedding model (MiniLM at 80MB, or even classical TF-IDF on unigrams+bigrams — fully classical works here). Embed the new message and the last 3 messages in its thread. Cosine similarity below a calibrated threshold = coherence anomaly.

A real reply continues the topic. A BEC hijack lurches into unrelated payment language. Your current detector is blind to this because it doesn't look at thread context at all.

**Expected lift:** +5 to +10 points, concentrated on "reply hijack" attacks which are a distinct attack pattern your current eval doesn't even test.

**Sovereign check:** ✓ local embedder, no external calls.

### 5. Character-trigram novelty, pushed harder

Wave 2 has 9 negative-space features. The token-level violation rates are good; the **character-trigram** version is even better for short emails because it doesn't require enough tokens for the n-gram distributions to be stable.

During CPP training, record the full set of character trigrams observed in the user's corpus (10k-30k typically). At scoring time, compute what fraction of the message's trigrams have never been observed from this user. Above ~3% is extremely unusual for a real message from this writer; LLMs routinely hit 8-15% because they default to phraseologies the user simply doesn't use ("I hope this finds you well", "kindly", "please find attached", "reaching out").

**Expected lift:** +5 to +10 points on few-shot and high-fidelity, particularly on short emails where the existing feature set is sparse.

**Sovereign check:** ✓ trigram set is a bloom filter or a hash set in the CPP. ~50KB per user.

### 6. Meta-learner ensemble instead of one-classifier tuning

You're spending effort tuning a single XGBoost classifier. The published BEC-detection literature (and frankly every mature fraud-detection team) ensembles 5-10 weak detectors with a meta-learner. Each detector is independently mediocre (AUC 0.70-0.85) but their errors are *uncorrelated*, so the ensemble is much stronger than any component.

Your detector stack becomes: (a) sovereign stylometry, (b) perplexity/burstiness, (c) character-trigram novelty, (d) DLP content score, (e) relational prior, (f) thread coherence, (g) metadata (SPF/DKIM/first-time-sender/reply-to mismatch). A logistic-regression or small gradient-boosted meta-learner trained on the seven scores produces the final calibrated fake-probability.

This is also how you **escape the single-ship-gate trap**. Instead of one overall metric that everything has to clear, the meta-learner naturally reweights detectors across tiers (perplexity dominates on zero-shot, relational priors dominate on few-shot, stylometry dominates on high-fidelity). Your catch curves become flatter across tiers, which is what MSP deployment needs.

**Expected lift:** Hard to estimate without training it, but my prior is +5 to +15 AUC points on composed score, and — more importantly — it collapses the few-shot/high-fidelity gap that's currently blocking your ship gate.

**Sovereign check:** ✓ all detector weights live in the CPP.

---

## The cold-start problem and how to finesse it

The methodology brief commits to "works with as few as 100 training emails." That's aggressive, and the ensemble above gives you a way to honor it gracefully.

New user on day 1 has a thin stylometric profile. Don't rely on it. Instead, the meta-learner automatically downweights stylometry (confidence intervals on its predictions are wide with small training sets) and upweights the detectors that don't need user-specific training: perplexity head, content-conditional priors, DLP, metadata. The user still gets meaningful protection from day 1, and the sovereign stylometry layer contributes more as their CPP matures over the first 90 days.

This also gives you a real marketing claim for Rain: "protection starts on day 1, accuracy improves continuously as the CPP learns." That's a better story than "we need 100 emails before we're useful."

---

## If I were at your keyboard, the order I'd work in

1. **Bayesian recomposer.** Rewrite `risk_composer.py` as a content-category-conditional posterior calculation. This is the foundation that everything else plugs into. ~1 week if you also learn the category priors from your current eval data.

2. **Perplexity / burstiness detector as a plug-in.** Local GPT-2 medium, feature-extracted during scoring. Register it in the meta-learner. This is the single biggest lift for your current ship-gate miss. ~1 week.

3. **Relational prior via per-pair intent histograms.** Adds to the CPP a per-recipient content-category distribution. Feeds the posterior directly. ~1 week including eval data regeneration to include recipient routing.

4. **Then ensemble.** Train the logistic-regression meta-learner on top of the seven detectors. Recalibrate thresholds. Re-run ship gate. ~1 week.

Four weeks of work, realistic. By end of that, my prediction: overall AUC 0.96-0.98, few-shot catch 0.75-0.85, high-fidelity catch 0.70-0.80. Ship gate cleared on a broader eval than you're currently running.

---

## What I want to flag explicitly

**None of the above saves you from the research-to-production gap.** The Shield service (`shield/scoring.py`) still runs the eight-feature deviation scorer with fixed weights — a completely different and much weaker algorithm than the lab stack. Whatever you build above needs to be wired through to the Shield service, not just validated in the lab. I'd personally treat the port-to-Shield task as prerequisite to any new detector work, because the cost of training Rain on one scoring behavior and then shipping a different one is very high.

**None of the above addresses the single-user proof problem either.** Everything is validated on Steve's Hotmail. Before the first MSP demo you need at least one more user's CPP built and evaluated end-to-end, even if the eval is rough. Otherwise the methodology brief is promising something you haven't demonstrated generalizes.

**None of the above helps with human-written impersonations**, which are a meaningfully different attack class than LLM fakes. Perplexity/burstiness is powerless against a human. Relational priors and intent novelty still help. This is worth a separate conversation — human impersonations are what sophisticated BEC actors *actually* deploy against high-value targets, and your eval doesn't include them yet.

---

## The sovereign constraint, one more time

Every technique above runs inside the customer's environment at scoring time. Every CPP artifact fits well under 1MB. Nothing sent externally. The perplexity head is the largest new dependency (a 350MB model download at install time), and it's a one-time deploy-side pull, not a runtime call.

This is actually a *stronger* sovereign story than you have now. You can say: "Chimera never sees customer email. Not at training, not at scoring, not even for debugging. The only thing that ever leaves is a per-user statistical fingerprint that cannot be inverted back to text." That's a real procurement weapon in legal, health, finance, EU — exactly the verticals where MSPs need ammunition.

— Claude
