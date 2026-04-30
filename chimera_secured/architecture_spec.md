# Chimera Secured — Target Architecture

**Scope of this document.** What Chimera Secured looks like when the pilot-readiness gates clear. Not a description of current code; that's the `TrueWriting/` lab stack (experimental, XGBoost + Wave 2 features) and `TrueWriting/shield/` (production, 8-feature hand-weighted deviation scorer — currently unconnected to the lab gains). This document is the target both converge on.

**Three design commitments that override everything else:**

1. **Sovereignty is architectural, not marketing.** The CPP is the only thing that leaves the customer tenant. Email bodies are scored in-place inside customer infrastructure and discarded after scoring. Every component below either respects this or isn't shipped.
2. **Ensembles, not silver bullets.** No single detector has ever cleared the bar on the few-shot and high-fidelity tiers. The design is an ensemble of weak detectors composed Bayesianly by content category.
3. **Detection before enforcement.** Every component ships in log-only mode first, earns its way to warn mode, and only gets enforcement rights after sustained real-world calibration. This isn't slow — it's how you avoid the false-positive death spiral that kills every enterprise security tool.

---

## The layered pipeline

Every inbound (or internal) email flows through these layers in order. Each layer can short-circuit the pipeline or hand off to the next. All layers log their decisions to the audit trail.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Email arrives at Shield (via M365 Graph or SMTP hook)              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 0 — Envelope & Address Bypass                                │
│  Fast-path skip for mail that doesn't need scoring:                 │
│    • Internal-to-internal (same tenant, both sides known)           │
│    • Trusted-partner sender (allowlist managed at tenant level)     │
│    • System-generated (Jira, DocuSign, Salesforce, etc.)            │
│    • Replies inside a thread originated by the recipient            │
│  → Bypassed mail: verdict = "allow", reason = "bypass/<rule>"       │
│  → Non-bypassed mail continues to Layer 1                           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1 — Detector Ensemble                                        │
│  Seven detectors, each emits probability ∈ [0,1] + confidence:      │
│    D1. Stylometric distance (XGBoost + Wave2 features)              │
│    D2. LLM-detector head (perplexity/burstiness, DetectGPT-style)   │
│    D3. DLP / payload category (current dlp_scanner.py)              │
│    D4. Ask-type novelty (has this sender ever asked this before?)   │
│    D5. Recipient-conditional style (does sender write this way to   │
│        this recipient specifically? current Wave2 RCP feature pack) │
│    D6. Thread coherence (is this a plausible reply or a topic jump?)│
│    D7. Metadata anomaly (send time, client, geo-IP, headers)        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 2 — Bayesian Content-Conditional Composer                    │
│  Posterior P(fake | signals, content_category)                      │
│    = P(signals | fake) · P(fake | content_category) / P(signals)    │
│  Uses DLP category output to set the prior:                         │
│    "wire transfer" category → high prior                            │
│    "scheduling/lunch" category → low prior                          │
│    → 7-word style anomaly + wire payload = HIGH (what gut says)     │
│    → 9-word style anomaly + lunch payload = LOW (what gut says)     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 3 — Policy Cascade & Verdict                                 │
│  Distributor → Reseller → Tenant → Security Group → User policy     │
│  Applies thresholds and routes to:                                  │
│    • allow (no action)                                              │
│    • warn (banner + optional verbal alert via ElevenLabs)           │
│    • quarantine (hold for admin review)                             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 4 — Audit & Explain                                          │
│    • /explain endpoint: per-email signal breakdown for admin/user   │
│    • Feedback logging: mark FP / FN without storing body            │
│    • Threshold telemetry feed back to composer for recalibration    │
└─────────────────────────────────────────────────────────────────────┘
```

### Where the email body lives

At Layer 1 entry, the body is in memory inside Shield's address space, running on customer infrastructure. Layers 1–3 operate on features extracted from the body + the CPP. The body is destroyed after Layer 1 feature extraction. Only derived signals (per-detector probabilities, per-category DLP flags, metadata) continue to Layer 2. **Nothing from Layer 1 onward contains reconstructible email content.** This is the sovereign-data contract and it's enforced by the pipeline shape, not by policy.

### Where the CPP lives

CPPs are built in-tenant from the user's historical sent items, then packaged as a ~250KB artifact. The CPP is the only object that is allowed to leave the tenant — it's uploaded to the TrueWriting CPP service for storage, versioning, and optional cross-tenant analytics (aggregate stats only, never per-user content). If a tenant opts out of CPP upload, the CPP stays in-tenant; Shield still works, it just runs without cross-tenant aggregate features.

---

## The seven detectors in detail

Each detector has three responsibilities: produce a calibrated probability, produce a confidence, and produce a human-readable reason string for `/explain`.

### D1 — Stylometric distance (the current lab stack)

- **Input:** email body + CPP
- **Features:** character 3/4/5-grams, function-word frequencies, structural (greeting/closing/length), Wave 2 negative-space features (9), Wave 2 recipient-conditional (6)
- **Model:** XGBoost (max_depth=3) + CalibratedClassifierCV (Platt scaling)
- **Training:** positive = real user emails; negative = multi-LLM rotated background (Claude, GPT-4, Llama, Mistral, Gemini at varied temperatures)
- **Known strengths:** Zero-shot catch 0.96. Best component on clearly-not-the-user emails.
- **Known weaknesses:** Plateaus on few-shot (0.52) and high-fidelity (0.60) attackers. Does not discriminate well when the attacker's LLM has seen examples.

### D2 — LLM-detector head (the missing piece)

- **Input:** email body (no CPP needed; this detector is sender-agnostic)
- **Features:**
  - Token-level perplexity (GPT-2 or small open model as reference)
  - Burstiness (std of per-sentence perplexity; humans vary more than LLMs)
  - Token-rank distribution (DetectGPT / Binoculars / GLTR signatures)
  - Sentence-length entropy
- **Model:** Logistic regression calibrated on known-LLM-generated vs. known-human corpora
- **Why this closes the few-shot / high-fidelity gap:** An attacker using an LLM, *however cleverly prompted*, still produces text with distinguishable statistical signatures at the token distribution level. D1 asks "does this look like Steve?" — D2 asks "does this look machine-generated at all?" They fail differently. Ensembling them captures both signals.
- **Known weakness:** Humans writing formally (lawyers, HR, formal execs) look LLM-like. Don't run D2 as a standalone high-confidence signal; compose it with D1 and D7.
- **Status:** Not implemented. Highest-priority new work.

### D3 — DLP / payload category

- **Input:** email body
- **Features:** Current `dlp_scanner.py` — hard categories (payment_change, gift_cards, wire, credentials, sensitive_data, crypto, financial_urgency) and soft categories (urgency, secrecy). Probabilistic-OR top-2 hard + soft amplification.
- **Output:** per-category scores + overall DLP probability + top category label
- **Role in the composer:** D3's category label feeds Layer 2's Bayesian prior. This is the "7-word credit card vs. 9-word lunch" mechanism in code form.
- **Status:** Well-designed, already in the lab stack, needs to be ported into Shield's path.

### D4 — Ask-type novelty

- **Input:** email body + the sender's historical sent items (in-tenant query)
- **Features:** semantic embedding of the email's "ask" (extracted via small LLM prompt or pattern matching). Distance to nearest past ask of the same sender.
- **Example:** Sender has never asked for a wire transfer in 3 years of sent items. This email asks for a wire transfer. That's a novelty score of ~1.0.
- **Why this matters:** Stylometric match can be perfect and the email still be fake if the *type of request* has never come from this sender before. Account takeover attackers frequently introduce ask types the user has never used.
- **Status:** Not implemented. Moderate-priority new work. Depends on a tenant-side semantic index of past sent items.

### D5 — Recipient-conditional style

- **Input:** email body + CPP with per-recipient substyle + recipient identity
- **Features:** Wave 2 recipient-conditional profiler (RCP). Does the sender write to *this specific recipient* in this formality / length / greeting pattern?
- **Why:** Steve writes differently to his accountant than to his best friend. A BEC email from "Steve" to his accountant that reads like his emails to his best friend is a style mismatch that D1 might miss.
- **Status:** Implemented in Wave 2 feature pack. Needs porting into Shield.

### D6 — Thread coherence

- **Input:** email body + thread history (in-tenant)
- **Features:** Semantic continuity between this email and the prior 2–3 messages in the thread. LLM-powered coherence scoring (small model, in-tenant).
- **Example:** Thread is about scheduling a lunch. Reply suddenly asks for wire transfer. Coherence: 0.05. This is the "sudden topic jump" signal that real BEC attackers produce when they hijack an existing thread.
- **Status:** Not implemented. Moderate-priority new work.

### D7 — Metadata anomaly

- **Input:** email headers + send metadata + CPP metadata profile
- **Features:** send time-of-day deviation, sending client (Outlook web vs. mobile vs. API), geo-IP of authenticated session, reply-to manipulation, display-name spoofing.
- **Why:** The cheapest, highest-precision signals live in the envelope. An email "from Steve" sent at 3am from a Lagos IP on a client Steve has never used is almost certainly not Steve, regardless of what the body says.
- **Status:** Partially implemented in Shield's existing infrastructure. Needs a scoring wrapper.

---

## The Bayesian content-conditional composer (Layer 2)

The core idea, expressed in one line:

> **The confidence required in the "this isn't really from you" signal should depend on what the email is asking for.**

A 7-word "what's for lunch?" with a 0.20 stylometric anomaly is ignorable. A 7-word "wire $50k to attached account" with a 0.20 stylometric anomaly is blocked. The *same* stylometric signal produces opposite verdicts because the content prior is different.

### Formally

Let `S` = vector of detector outputs (D1..D7) and `C` = DLP content category (from D3).

The current composer uses thresholds on a piecewise-linear blend of signals: if D1 ≥ 0.02 → high; else if D1 ≥ 0.007 AND D3 ≥ 0.5 → mid; etc. This is a hand-coded if-ladder that the Kimi report partially addresses but fundamentally treats as a weighting problem.

The Bayesian composer instead computes:

```
P(fake | S, C) ∝ P(S | fake) · P(fake | C)
```

`P(fake | C)` is a lookup table by content category, informed by BEC incident-rate literature and adjustable per-tenant:

| Content category              | P(fake \| C) |
|------------------------------|-------------|
| Wire/payment instruction     | 0.40        |
| Credentials request          | 0.35        |
| Gift card request            | 0.50        |
| Invoice/payment change       | 0.30        |
| Sensitive data request       | 0.20        |
| Urgent / secrecy framing     | 0.10        |
| Scheduling / social          | 0.003       |
| Status / informational       | 0.002       |

`P(S | fake)` is learned from the attacker corpus. The composer multiplies these and normalizes. The result is a posterior P(fake | S, C) that correctly handles the lunch vs. wire asymmetry without any hand-tuned threshold ladder.

### Worked example — the user's own intuition

**Case 1: 9-word lunch ask, style anomaly 0.20.**
- Content category: scheduling/social (prior 0.003)
- Posterior ≈ 0.003 × (style likelihood ratio ~3) ≈ 0.009
- Verdict: allow. Matches gut.

**Case 2: 7-word credit-card ask, style anomaly 0.20.**
- Content category: sensitive_data (prior 0.20)
- Posterior ≈ 0.20 × (style likelihood ratio ~3) ≈ 0.60
- Verdict: warn/block at 0.5 threshold. Matches gut.

The composer is ~80 lines of Python. It replaces `risk_composer.py`'s if-ladder.

---

## ElevenLabs verbal warning (novel creative lever)

On HIGH-confidence verdicts (composite ≥ 0.85), trigger a verbal warning synthesized via ElevenLabs and played to the recipient through the Outlook add-in or an OS-level notification.

**Copy examples:**
- Wire transfer case: *"Heads up — the email from Steve asking for a wire transfer doesn't match his usual writing style. Please verify with him directly before acting."*
- Credential case: *"Careful — this password-reset email from IT doesn't look like it came from your IT team. Pause and check with them in person or on Slack."*

**Why this is worth building:**
1. **Memorability.** A voice alert lodges in working memory in a way a banner does not. The recipient remembers the pattern for next time.
2. **Differentiation.** No commercial BEC tool does this. Rain Networks can sell this as a feature no incumbent has.
3. **Cost.** ElevenLabs API is cheap at HIGH-confidence rate (probably <1% of flagged mail → <0.01% of all mail). $10–50/month per tenant ceiling.
4. **Opt-in.** Configurable per user, per security group. Admins can disable if annoying.

**Why it's not the first thing built:** It's a Phase 2+ feature. It only matters after Layer 1–3 are solid and the HIGH-confidence tier is well-calibrated. Shipping it before the composer is stable produces voice alerts on false positives, which is the worst possible alert fatigue.

**Config:** `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env.example`, already wired.

---

## Shield port-in plan

The current production Shield uses `shield/scoring.py` — an 8-feature hand-weighted deviation scorer unconnected to anything in this document. The port-in plan:

**Step 1 — Wrap the lab composite as a service.**
The lab stack (`chimera_scorer.py` + Wave 2 features + DLP + the new Bayesian composer) gets wrapped in a FastAPI service at `shield_scorer_service.py`. Input: email body + CPP + thread history. Output: per-detector scores + composite + reason + explain payload.

**Step 2 — Replace Shield's scorer call.**
`shield/service.py` currently calls `scoring.score_email(...)`. Change the call to hit the new service. Shield's database, policy cascade, and M365 integration stay unchanged.

**Step 3 — Preserve the current scorer behind a feature flag.**
During the pilot's shadow phase, both scorers run in parallel. Verdicts are logged but the old scorer's output drives any (disabled) enforcement. Once confidence is established, the flag flips.

**Step 4 — Retire the old scorer.**
After Phase 2 of the pilot, `shield/scoring.py` becomes dead code and is removed. The 8-feature hand-weighted scorer was a bootstrap; it's served its purpose.

---

## Model artifacts & versioning

Every deployed scorer version produces a signed manifest:

```json
{
  "version": "chimera-secured/2026.04.1",
  "components": {
    "stylometric": {"model_sha256": "...", "features": 28},
    "llm_detector": {"model_sha256": "...", "reference_model": "gpt2-small"},
    "dlp": {"ruleset_sha256": "...", "version": "2.1"},
    "composer": {"prior_table_sha256": "...", "version": "bayesian-v1"}
  },
  "trained_at": "2026-04-12T00:00:00Z",
  "eval_metrics": { /* gate-passing numbers */ },
  "ship_gate_passed": true
}
```

This is what Shield loads at startup and what the `/version` endpoint returns. Pilots always know exactly which model version is in their tenant.

---

## What's NOT in this spec (deliberate)

1. **A new UI.** Shield's M365 add-in stays. Verdicts render through existing banner/quarantine infrastructure. No re-skin.
2. **A new pricing model.** The architecture doesn't care about pricing; that's Rain's work.
3. **A re-invented policy engine.** The distributor → reseller → tenant → group cascade in `shield/` is fine. Layer 3 consumes it as-is.
4. **Cross-tenant learning.** Not allowed under the sovereignty constraint. Each tenant's CPPs stay separate. Aggregate anonymous statistics (e.g., baseline LLM-detector scores for "normal" English) can be learned cross-tenant without violating sovereignty, but per-user signals never mix.

---

## Sequencing

When all five pilot-readiness gates (see `pilot_readiness_plan.md`) clear, the architecture above is what ships. Until then, the sequence inside this architecture is:

1. **Stylometric + DLP port** into Shield (replace 8-feature scorer). Ship with existing threshold ladder.
2. **LLM-detector head (D2)** added to the ensemble. Ablation: does few-shot/high-fidelity catch jump?
3. **Bayesian composer (Layer 2)** replaces the if-ladder. Ablation: does real-false-flag drop while catch holds?
4. **Ask-type novelty (D4) and thread coherence (D6)**. Ablation: do they add incremental catch or are they redundant with D1+D2?
5. **Verbal warning (ElevenLabs)** on HIGH-confidence only, opt-in per user.
6. **Cross-tenant aggregate statistics** (anonymous, opt-in) as polish.

Steps 1–3 are pre-pilot. Steps 4–5 are early-pilot enhancements. Step 6 is post-GA.
