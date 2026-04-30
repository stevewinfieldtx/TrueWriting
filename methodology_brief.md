# Chimera Secured — Technical Methodology Brief
*For inclusion in the Chimera Secured white paper. Version 1.0.*

---

## Executive Summary

Chimera Secured detects email impersonation through a layered architecture that combines per-user behavioral stylometry, content-adaptive risk scoring, length-stratified feature modeling, and metadata signal fusion. The methodology was developed iteratively, stress-tested against progressively more sophisticated LLM-generated impersonation attacks, and cross-validated by three independent AI research systems (Kimi, Z.ai, DeepSeek) before committing to the final architecture.

The product does not rely on a single signal. It fuses four orthogonal signals, weighted by the content-risk category of each message, to produce a tiered alert decision rather than a binary allow/block.

---

## 1. Core Principle: Sovereign AI for Communication Identity

Chimera Secured rejects the prevailing "crowd intelligence" model used by most behavioral AI email security vendors, which pool signals across all customers to build shared global models. That approach trades customer privacy for network effects, and it exposes every customer's communication patterns to cross-tenant analysis.

Chimera's alternative is **Sovereign AI**: each user gets their own isolated discriminative model, trained locally, stored as a per-user artifact (~250KB), never comingled with any other user's data. The product thesis is that every individual's writing style is unique enough to serve as their own verification baseline — and that the cheapest data for "what Steve's writing looks like" is Steve's own outbox, not a shared threat intelligence feed.

This architectural choice maps directly to procurement requirements in regulated verticals (legal, healthcare, finance, EU), where pooled-cloud email security solutions face compliance friction that Chimera bypasses by design.

---

## 2. Architectural Overview

Chimera operates as a four-layer defense, executed in parallel on every inbound and outbound message:

| Layer | Question It Answers | Signal Type |
|------|---------------------|-------------|
| Stylometric | Did the claimed sender write this? | Authorship confidence |
| Content-Risk (DLP) | Does this email matter from a risk standpoint? | Threat-category classification |
| Length-Stratified | How confidently can we score this at all? | Reliability gate |
| Metadata | Does the delivery envelope look normal? | Infrastructure signal |

The final risk score is a weighted composition of all four layers, producing a tiered action (silent / warning banner / quarantine) rather than a single threshold decision.

---

## 3. Layer One — Stylometric Authorship Verification

### 3.1 Per-User Discriminative Modeling

Chimera builds a Communication Personality Profile (CPP) per user using a discriminative classification framing, not anomaly detection. The positive class is the user's own sent email corpus. The negative class is a diverse population of other human writers. The classifier learns to separate "this specific user" from "all other humans" — a fundamentally different and more robust framing than "how far is this from the user's mean" anomaly detection used by most competitors.

This reframing, drawn from 20+ years of academic authorship-attribution research (Koppel et al. 2009, Stamatatos 2013), is standard in forensic linguistics but rarely deployed in commercial email security because of the operational cost of training and storing per-user models. Chimera accepts that operational cost as the price of meaningful per-user accuracy.

### 3.2 Feature Engineering

The stylometric feature space includes:

- **Character n-grams** (3-5 characters, TF-IDF weighted, top 2,000 features) — captures unconscious micro-patterns that are difficult for attackers to mimic even with LLM assistance.
- **Function-word frequency vector** (150 dimensions) — topic-independent markers of style that operate below conscious authorial control.
- **Structural features** (sentence-length mean and variance, word-length distribution, punctuation rates, type-token ratio, contraction rate, pronoun usage) — robust on short texts where sparse features fail.
- **Negative-space features** — binary markers for linguistic patterns the user has *never* exhibited in training, weighted heavily as violation signals.

### 3.3 Deliberate Exclusions

Rigorous preprocessing removes three categories of content from the feature space:

- **Signatures and sign-off blocks** — because any attacker with five sample emails can trivially copy a signature, allowing the signature to become a feature creates a shortcut that defeats the entire model.
- **Inline PII** (email addresses, phone numbers, URLs) — replaced with placeholder tokens to prevent the classifier from learning "presence of this specific phone number = this user" as a memorized shortcut.
- **Legal and compliance footers** (CONFIDENTIAL, DISCLAIMER, "This email is intended for the recipient," HIPAA, PRIVILEGED, auto-appended corporate notices) — these appear uniformly on authorized user messages and are trivially copied by attackers.

This rigorous preprocessing is what separates Chimera's stylometry from naive implementations. An initial version of the methodology that did not strip these artifacts achieved artificially high catch rates by memorizing the user's literal phone number and email address as discriminative tokens. When the preprocessing was corrected, catch rates initially collapsed — diagnostically revealing that the earlier model was not actually doing stylometry. The subsequent rebuild with genuine style-based features is what the production methodology is built on.

---

## 4. Layer Two — Adaptive DLP with Content-Weighted Risk Scoring

Stylometry alone cannot drive action decisions. A 20-word email has limited stylistic signal, and most short emails are not consequential enough for an alert to matter. Chimera's breakthrough insight — validated by the CTO of our anchor design partner — is that **content risk must modulate how aggressively stylometric anomalies are acted upon**.

### 4.1 Content Risk Categories

The DLP layer classifies every message into one of several BEC-specific risk categories:

- **Money-movement signals:** wire transfer language, banking detail changes, invoice approvals, gift card requests, cryptocurrency mentions, payroll redirects.
- **Credential access signals:** password requests, MFA code requests, VPN/SSO prompts, permission-elevation requests.
- **Data exfiltration signals:** employee roster requests, tax document requests (W-2, 1099), client list asks, HR data requests.
- **Social engineering amplifiers:** urgency markers ("today," "ASAP," "before end of day"), secrecy directives ("don't discuss with team," "confidential"), authority invocations ("I'm in a meeting, just handle it"), process deviations ("skip the usual approval").

The DLP layer does not require machine learning. It is implemented as regex-based pattern matching and curated keyword lists, tuned specifically for the BEC attack surface rather than generic compliance DLP.

### 4.2 The Adaptive Tightness Principle

Chimera scales its enforcement posture based on content risk:

- A casual 15-word email scoring moderately anomalous → silent pass. The false positive is irrelevant.
- A wire-transfer request scoring moderately anomalous → warning banner. Wire-transfer requests always deserve extra verification.
- A credential-access request scoring moderately anomalous → quarantine for MFA challenge.
- A wire-transfer request that scores normal but comes from a first-time sender domain → soft warning, because the content category alone justifies friction.

This adaptive tightness resolves the single largest operational problem in email security products: false-positive fatigue. MSPs consistently report that security tools get turned off when false positives exceed a few per user per week. By filtering alerts through content-risk weighting before applying FPR budgets, Chimera concentrates alerts on messages where the alert actually matters.

---

## 5. Layer Three — Length-Stratified Scoring and Metadata Signals

### 5.1 Why Short Emails Get Different Treatment

Academic stylometry research (Lopez-Escobedo 2013, Brocardo 2013) demonstrates that authorship attribution accuracy collapses below 100 words and is statistically unreliable below 50 words. Chimera honors this constraint explicitly rather than pretending stylometry works uniformly across all message lengths:

- **Under 50 words:** Stylometric scoring is deprioritized. The primary signals become DLP content-risk category and metadata.
- **50-150 words:** A compact feature model (handcrafted features plus negative-space violations) provides measured stylometric confidence with wider calibration bands.
- **Over 150 words:** The full feature model produces high-confidence authorship scores.

### 5.2 Metadata Signals

Layer three also incorporates infrastructure signals that function independently of content:

- First-time-sender detection at the domain level.
- Reply-to / From: mismatch.
- DMARC, SPF, and DKIM alignment status.
- Sender IP geolocation anomalies versus historical sending patterns.
- Time-of-day and day-of-week anomalies.
- Display-name spoofing detection.

Metadata signals are especially valuable for the under-50-word routing path where stylometry is unreliable, providing a meaningful risk signal without depending on writing style.

---

## 6. Scoring Integration and Tiered Action Model

Chimera rejects the binary allow/block decision model. Instead, the product produces tiered actions calibrated to FPR budgets appropriate for each action:

| Action | Composite Risk | FPR Target |
|--------|----------------|------------|
| Silent pass | Low | No FPR constraint — silent action has no user cost |
| Warning banner | Medium | < 5% FPR — banner is low-friction, high-user-tolerance |
| Quarantine with admin review | High | < 0.5% FPR — quarantine is high-friction, must be conservative |

Administrators can tune per-user and per-group sensitivity. Executives and finance staff can be placed on tighter thresholds; general staff on looser thresholds. The threshold calibration is derived from each user's own self-vs-other separation during enrollment, not from a global default.

---

## 7. Evaluation Methodology

### 7.1 Eval Harness Design

Chimera's evaluation harness departs from the industry norm of "prove it works" benchmarks by constructing adversarially difficult test sets that a naive vendor would not run against themselves.

For every per-user model, the eval harness produces:

- **300+ held-out real user emails** (never seen during training) to measure false-positive rate.
- **LLM-generated fakes at three attacker fidelity tiers** (see below) to measure catch rate against sophisticated adversaries.
- **Human-written impersonations** (recruited via contract workers with 5-10 real samples) to measure resilience against targeted human attackers, who fail in systematically different ways than LLMs.
- **Replay attacks** (real old user emails resent as if new) to explicitly document the limits of stylometric scoring.
- **Partial-copy attacks** (first portion real user text, final portion attacker-written) to test segment-level robustness.

### 7.2 Three-Tier Attacker Fidelity Model

Each model is measured against three progressively sophisticated attack tiers:

- **Zero-shot:** Attacker has no samples, only public knowledge of the target. Prompts an LLM with minimal context.
- **Few-shot:** Attacker has 5 real emails from the target (a realistic scenario after a small breach or a forwarded email chain) and prompts for style-matching imitation.
- **High-fidelity:** Attacker explicitly analyzes sentence length, punctuation habits, greeting patterns, and other stylistic markers, then prompts generation that matches each one.

Reporting catch rate at a single attacker tier is considered insufficient. The product-level metric is a tier-weighted composite, with few-shot and high-fidelity weighted more heavily than zero-shot to reflect the actual threat profile.

### 7.3 Ship Gate Criteria

Chimera uses composite-score ship gates, not raw-stylometry ship gates. The production readiness criteria are:

- Composite-score overall AUC ≥ 0.92 on the combined eval set.
- Tier-specific catch rates realistic to the length and content distribution:
  - Zero-shot: catch at 2% FPR ≥ 0.90
  - Few-shot: catch at 2% FPR ≥ 0.65
  - High-fidelity: catch at 2% FPR ≥ 0.50
- False-positive rate under 3 alerts per user per week on a real-world mixed corpus.
- Human-impersonation catch rate measured and reported, even if lower than LLM tiers, so deployment partners understand the threat model.

### 7.4 Independent Cross-Validation

The core methodology was cross-validated by three independent AI research systems (Kimi, Z.ai, DeepSeek) before being committed. Convergent critiques identified the distributional-mismatch problem in an earlier evaluation setup (synthetic LLM-generated negatives being systematically different from real human negatives), the calibration problems introduced by class-weight balancing, and the operational unrealism of a single-threshold ship gate. Each of these issues was addressed in the production methodology. The cross-validation record is retained for MSP and vendor due-diligence review.

---

## 8. Acknowledged Limitations and Complementary Defenses

Chimera Secured is deliberately transparent about what stylometric analysis cannot achieve. Three attack categories exceed the reach of style-based detection:

- **Replay attacks** — real historical emails from the user, resent by the attacker with modified links or attachments. The content is genuinely the user's writing; stylometry cannot flag it. Complementary defenses required: thread-history deduplication, link reputation checking, attachment sandboxing.
- **Account takeover** — the attacker has the user's real credentials and sends from the authentic account. Stylometry is blind to this class entirely. Complementary defenses required: MFA enforcement, anomalous-login detection, session-risk scoring.
- **Partial-copy / Franken-emails** — attacker reuses opening and signature from a real email, inserts malicious request in the middle. Segment-level scoring within Chimera addresses this partially, but deployment alongside recipient-verification protocols is recommended.

The product is positioned as a dedicated layer within a defense-in-depth security posture, not as a standalone replacement for the full spectrum of email security controls. This transparency about limitations, included in the product documentation, is itself a trust signal: customers who hear "we catch everything" lose trust faster than customers who hear "here's exactly what we catch and where you need complementary controls."

---

## 9. Privacy and Deployment Architecture

Chimera is built to a zero-knowledge content standard:

- Email content never leaves the customer's environment during scoring. All analysis happens in memory.
- The only persistent artifact per user is the stylometric model pickle (~250KB), which contains statistical feature weights and no raw text.
- PII is stripped before feature extraction, so no email addresses, phone numbers, or URLs are retained even in anonymized form.
- Model retraining happens on the same locally-deployed container, on a randomized 60-90 day cadence with drift-triggered early refresh.
- No cross-tenant data pooling. Steve's model sees Steve's emails; Jane's model sees Jane's emails; no shared gradient descent, no federated learning, no network effects that require data sharing.

The deployment model is channel-appropriate: MSP-operated containers run the scoring engine locally, per-tenant model stores are isolated, administrators tune per-user sensitivity, and the end-user experience is a badge in their email client indicating verification status — not a new app, a new account, or a manual enrollment flow.

---

## 10. Methodology Rigor as a Commercial Differentiator

Most email security vendors publish white papers that describe features without describing methodology. Chimera takes the opposite position: the methodology is the product. The willingness to publish testing protocols, acknowledged limitations, adversarial-evaluation design, and cross-validation records is intended to be a trust signal for:

- MSP technical buyers who have been burned by "AI-powered" security products that could not survive real scrutiny.
- Insurance-carrier risk advisors who evaluate controls for premium-credit programs.
- Legal-industry buyers whose procurement reviews require demonstrable controls on data handling.
- Investors and acquirers whose due diligence depends on defensible technical claims.

The claim is not that Chimera achieves perfect detection. The claim is that Chimera measures itself honestly against the attacks that actually matter, documents its limitations explicitly, and builds its product architecture around those documented limitations rather than pretending they do not exist. That posture is the commercial differentiator. The technology is the expression of it.
