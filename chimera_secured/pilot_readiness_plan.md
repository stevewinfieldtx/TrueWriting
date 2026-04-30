# Chimera Secured — Pilot Readiness Plan

**Audience:** Steve (TrueWriting / Chimera Secured founder), as the person who has to honestly decide whether to take this to Rain Networks' 60,000 seats.

**Bottom line up front:** We are not pilot-ready today. The lab stack's own ship gate says so (few-shot catch 0.52, high-fidelity catch 0.60, need 0.85). But the distance between here and honestly-ready is measurable and the work is sequenceable. This plan describes the five gates that need to clear, and the phased pilot rollout that follows. The phased rollout is structured so the first customer-facing phase ships in log-only mode — which means we can start getting real-world signal from Rain's environment *before* we've earned full blocking rights.

---

## The five gates

These gates are ordered by dependency. Gate 1 has to pass before Gate 2 is meaningful. Gates 3, 4, and 5 can overlap once Gate 2 is clearing.

### Gate 1 — Single-writer ship gate (Steve's Hotmail)

**Definition.** On held-out real emails from Steve's Hotmail plus the three-tier attacker set (zero-shot, few-shot, high-fidelity), the production scorer achieves:
- Overall composite AUC ≥ 0.92
- Catch rate at 2% FPR ≥ 0.85 on every tier individually
- Real-email false-flag rate ≤ 3%

**Current state.** Lean eval (Chimera + DLP, context dropped): overall AUC 0.9503, zero-shot catch 0.96, few-shot 0.52, high-fidelity 0.60, real-false-flag 2.3%. Two tiers fail the bar.

**What has to happen.** The two failing tiers fail for a known reason: the stylometric scorer is the only thing carrying weight there, and it plateaus when the attacker has seen even a handful of the target's real emails. The fix is the LLM-detector head — perplexity and burstiness features that flag LLM-generated text independently of style similarity. This is the single biggest lift available and it's been on the list for months without landing. It goes in first.

**Adjacent fix.** The production Shield scorer at `shield/scoring.py` is an 8-feature hand-weighted deviation model with no connection to the lab stack. It will not clear this gate no matter what we do in the lab. Either Shield's scorer gets replaced with the lab composite, or the lab composite gets wrapped in a service that Shield calls. Both are fine; what's not fine is shipping a pilot where the thing that passed the eval is not the thing making decisions.

**Exit criterion.** A single eval script runs the production scorer (not a notebook version) against held-out data and emits a JSON report showing all three tiers clearing 0.85 catch@2%FPR.

### Gate 2 — Cross-writer generalization (Enron)

**Definition.** Same bar as Gate 1, but the model is trained and evaluated on 30–50 distinct Enron writers in a leave-one-writer-out fashion. Mean catch@2%FPR across writers ≥ 0.80, worst writer ≥ 0.65.

**Why this matters more than anything else.** Right now we have one real identity. That is the single biggest credibility hole in the pitch to any partner. If the model works on Steve because the CPP happened to separate cleanly, we have no way to know that until it fails in someone else's inbox. Enron is the answer. 150+ distinct writers, ~500K emails, public, legally cleared for research. It costs nothing and it's the difference between "it works on me" and "it works across genuinely different writing styles."

**What has to happen.** See `eval_strategy.md` for the full plan. Briefly: pick 30–50 Enron writers who have ≥ 200 sent messages; build a CPP for each; generate attacker corpora against each (reusing the tiered generation pipeline); run the full three-tier eval per writer using a parallel agent swarm; aggregate.

**Exit criterion.** Per-writer metrics table plus distribution plots. Mean clears the bar. Worst-case is documented — we need to know which writers the model struggles with and have a hypothesis for why (short emails? low send volume? highly variable style?).

### Gate 3 — Adversarial hardening

**Definition.** The attacker-generation pipeline includes at least three distinct adversarial strategies beyond the current three tiers:
1. **Human impersonation** — a real person rewriting target emails in their own phrasing with target-specific intent (the hardest case, and the one we probably can't perfectly beat but need to measure)
2. **Multi-LLM ensemble attacker** — an attacker that uses three different LLMs to generate candidates and picks the one closest to the target style
3. **Signature/PII injection** — an attacker that copies legitimate signatures, reply chains, and real phone numbers from harvested real emails

**Why.** Our current "high-fidelity" tier is still a single LLM given few-shot examples. That's a 2024-era threat. By the time Chimera Secured is in a real enterprise, attackers will be using retrieval-augmented generation over stolen mailboxes. If we can't beat those, we need to know now.

**Exit criterion.** Catch@5%FPR ≥ 0.60 on each of the three adversarial tiers. Yes, we're loosening the FPR budget for the nastiest attackers — that's honest, because these attacks are by construction harder and we'd rather flag a few more real emails than promise unachievable catch on impossible attackers.

### Gate 4 — Operational readiness

**Definition.** Shield can do the following without code changes:
1. **Run in shadow mode** — score every email, log the verdict, never modify delivery. This is Phase 1 of the pilot.
2. **Run in warning mode** — add a banner to flagged emails without blocking them. Phase 2.
3. **Run in enforcement mode** — quarantine held emails per policy. Phase 3.
4. **Emit an `/explain` response** for any scored email that shows which signals fired at what strength, so Steve or a Rain admin can answer the "why was this flagged?" question for any decision.
5. **Log every verdict with a `feedback_id`** so admins can mark false positives and false negatives, and we can compute real-world performance without ever storing email bodies.

**Why.** Pilots die not when the model is wrong but when the customer can't understand why the model was wrong. Explainability and feedback loops are the difference between "turn it off, it's annoying" and "this keeps saving us, let me configure it better."

**Exit criterion.** End-to-end smoke test in a real M365 tenant (Steve's, Rain Direct's sandbox) with all three modes exercised and the explain/feedback path verified.

### Gate 5 — Honest pilot pitch to Rain

**Definition.** A three-page document Steve can read out loud to Nathan at Rain Networks that covers:
1. What Chimera Secured catches and at what confidence, grounded in Gates 1–4's numbers
2. What it does not catch (e.g. a sophisticated human writing from the target's own account in perfect style with a benign ask — we just don't detect that, and we shouldn't pretend to)
3. What the first 30 days look like (shadow mode, weekly review of what would-have-been-flagged, joint calibration of thresholds)
4. The sovereign-data story, in plain English: CPPs leave the tenant, email bodies do not, here's what's in the CPP and here's the code path that proves it
5. The stop conditions: "We'll pause the pilot if real-world FPR exceeds X, or if user-reported false positives exceed Y per week, or if we discover an attacker class we can't detect"

**Why.** Steve asked for this directly. "We have to have something that we honestly think is going to solve the problem." The pitch is where that honesty gets tested. If I can't write Gate 5 without hedging, we're not ready.

**Exit criterion.** Steve reads it, pushes back on anything that feels oversold, the revision passes his gut check.

---

## Phased pilot rollout

Once the five gates clear, the pilot is staged so early phases gather signal without customer risk.

### Phase 0 — Internal (weeks 0–2, pre-pilot)

Run Chimera Secured against Steve's Hotmail and Rain Direct's sandbox tenant in shadow mode. No user-visible changes. Collect two weeks of real-email scoring data. Compute real-world FPR. Adjust thresholds if needed. Ship the `/explain` and feedback endpoints.

**Success signal:** Real-world FPR within 2x of the eval estimate. Explainability output is readable by a non-engineer (Nathan at Rain tests this).

### Phase 1 — Detection-only at Rain (weeks 3–6)

Roll out to a selected Rain customer tenant — ideally one with 50–500 users and a willing admin. Shadow mode, no user-visible changes, but admin sees a weekly dashboard of "would have been flagged" emails with verdicts and reasons.

**Why start here.** Rain's admin can tell us — for the specific attackers they're actually seeing in their environment — whether our flags look right. We get real-world attacker diversity without risking a single delayed legitimate email. This is the phase where we learn which detectors matter in the wild.

**Success signal:** Admin-confirmed catch rate ≥ 0.70 on real incidents logged in the window. Would-have-been-flagged real emails that the admin marks "fine" ≤ 3/week per 100 users.

### Phase 2 — Warning banners (weeks 7–10)

Same tenant, same detection, but flagged emails now get an in-client banner ("This email's style is unusual for the sender. Verify before acting on any requests."). No blocking, no quarantine, no sender friction. Recipients can mark banners as correct or incorrect with one click.

**Why.** This is where we find out whether the warnings are *useful* to humans, not just statistically valid. A banner the recipient ignores is no better than no banner. A banner the recipient uses to ask the sender "did you really send this?" is exactly what BEC prevention looks like.

**Success signal:** Banner-useful rate (recipient-marked correct) ≥ 0.60. Banner-annoying rate ≤ 0.15. Admin retention intent ≥ "would renew."

### Phase 3 — Full enforcement (weeks 11–14)

Quarantine verdict = hold at the policy threshold for flagged security groups (finance, executives). Sender and admin notified. Recipient sees nothing until release.

**Why phased by group.** Finance and executives are both the highest-value targets and the groups most tolerant of a hold-and-review flow. Everyone else stays on banners.

**Success signal:** Zero confirmed missed BEC incidents. Zero confirmed wrongful holds that materially delayed business. Admin-reported net-positive NPS from the security-group users.

### Phase 4 — Multi-tenant expansion (months 4–6)

Roll out to 5–10 additional Rain customers. Start with the same shadow → warn → enforce phasing, compressed to 2 weeks per phase based on Phase 0–3 learnings. Introduce the distributor/reseller policy cascade in anger for the first time.

**Success signal:** Per-tenant onboarding time ≤ 4 hours. Per-tenant policy-tuning cycles ≤ 2. Shared false-positive patterns across tenants get codified into default policies automatically.

### Phase 5 — GA (month 7+)

Chimera Secured is a product, not a pilot. Rain Networks sells it across their 60,000-seat base. Pricing, packaging, SLAs, incident response, customer success all exist as functions. This plan does not go that far — but it's the target that Gate 5 is pointing at.

---

## What this plan explicitly does not promise

1. **A single-signal silver bullet.** Stylometry plateaus. Perplexity/burstiness is a strong addition but has its own blind spots (humans writing formally read as low-perplexity, like LLMs). The right answer is an ensemble of weak detectors composed Bayesianly by content category.
2. **Zero false positives.** A 2% FPR on a 200-user tenant sending 100 emails per user per day is 400 false flags a day. We mitigate with warning mode, explainability, and per-security-group policy tuning. We do not promise zero.
3. **Defeat of the human-from-your-own-machine attacker.** If someone sits at your compromised laptop and types a benign-looking request using your own phrasing and no payload, we will not catch it. We will catch it only when they start showing the tells: unusual send time, unusual recipient, payload words, LLM-generated phrasing, etc. Chimera Secured is a behavioral security tool, not a possession-proof tool.
4. **One-month pilot readiness.** The gates are sequenceable but honest. My estimate is 6–10 weeks of focused work to clear all five gates with a small team, longer solo.

---

## The honest self-assessment that motivates this plan

Three things Chimera Secured has going for it that most BEC tools don't: a per-user behavioral fingerprint instead of per-tenant heuristics; a sovereign-data story that's actually structurally true (the CPP architecture forces it, not a marketing claim); and a content-conditional composer idea (the lunch/credit-card insight) that no commercial tool I've seen implements properly.

Three things we're weak on: a single-user training set is a credibility gap no amount of clever math closes — Enron fixes this for free and we should have done it already; the production Shield scorer and the research stack have diverged into unrelated codebases, and the gap keeps widening the longer it stays unreconciled; the context layer was shipped before it was validated, performed at random (0.57 AUC), and got quietly disabled — we should not repeat that pattern with the LLM-detector head, which means every new detector needs its own ship gate before it's composed.

One thing that's genuinely uncertain: whether per-user CPPs transfer well across writers who have very different sending volumes and style consistencies. Enron will tell us. If the answer is "only well for consistent writers," then the product scopes to those users and we're honest about it.
