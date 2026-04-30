# Chimera Secured — Evaluation Strategy

**The single biggest credibility gap in Chimera Secured today is that it has been trained, tuned, and evaluated on exactly one real human.** Every number in every internal report traces back to Steve's Hotmail. That's not a fatal problem — it's a solvable one — but it *is* the first thing a serious security buyer will ask about, and if we walk into Rain Networks without a real answer, the pilot dies in the first call.

This document is the plan for how we honestly earn the right to say "this works."

---

## The three-part eval stack

The eval has to answer three different questions, and each one needs its own data, its own method, and its own bar. If we smash them together the way the current lean eval does, we get numbers that are only trustworthy for one question.

### Part 1 — Does it work on Steve? (single-writer, in-distribution)

This is the current lean eval. It answers: can we detect impersonations of a specific, well-profiled writer whose sending history we've seen?

**Data:** Steve's Hotmail sent items (CPP training + held-out real emails) + three-tier attacker corpus (zero-shot, few-shot, high-fidelity LLM impersonation).

**Bar:** Overall AUC ≥ 0.92, catch@2%FPR ≥ 0.85 per tier, real-false-flag ≤ 3%.

**Status:** Passes on zero-shot (0.96), fails on few-shot (0.52) and high-fidelity (0.60). The fix isn't in the data — it's in the scorer. Adding the LLM-detector head (perplexity/burstiness) is the single highest-leverage change available.

### Part 2 — Does it generalize? (cross-writer, Enron corpus)

This is the gap the current eval does not close, and it is the gap Rain will ask about first. The question: if we pick a writer neither we nor the model has ever seen, build a CPP from their sent items, and run the same attacker pipeline, does the detector still work?

**Data:** The Enron email dataset. ~500,000 emails from ~150 real employees, publicly released in 2003 as part of FERC's investigation, legally cleared for research use, widely used in NLP literature (text classification, author attribution, thread reconstruction). Available from https://www.cs.cmu.edu/~enron/ or cleaned versions on Kaggle/HuggingFace.

**Method — per-writer leave-one-out eval:**

1. **Writer selection.** Pick 30–50 Enron writers with ≥ 200 sent emails. This gives enough history to build a meaningful CPP while keeping the run budget manageable. Filter out writers whose sent items are mostly forwarded chains or auto-generated content (meeting invites, status reports).

2. **CPP build per writer.** For each selected writer, train the Chimera CPP on 80% of their sent items. Hold out 20% as "real" test emails.

3. **Attacker generation per writer.** Reuse `background_generator_swarm.py` to generate the same three-tier attacker corpus for each writer: 200 zero-shot, 200 few-shot (seeded with 5 real emails from that writer), 200 high-fidelity (seeded with 20 real emails + explicit style-mimicry prompt). That's 600 attackers per writer × 40 writers = 24,000 generated emails. At current LLM costs this is approximately $50–150 end-to-end.

4. **Per-writer eval.** Run the full Chimera composite (stylometric + LLM-detector head + DLP) against each writer's held-out real + three-tier attacker set. Record AUC, catch@2%FPR per tier, real-false-flag rate.

5. **Aggregate.** Mean ± std across writers. Worst-case writer. Distribution plots by writer-volume, writer-formality, writer-consistency.

**Bar:** Mean catch@2%FPR ≥ 0.80 across writers. Worst writer ≥ 0.65. Real-false-flag rate ≤ 3% for writers with ≥ 500 training emails, ≤ 5% for writers with 200–500 (we expect less-trained CPPs to be noisier, and honesty requires us to report it).

**What we learn from this that we cannot learn any other way:**
- Which writer profiles work well and which don't (low-volume? short-email? stylistically inconsistent?)
- Whether the feature set is picking up *this writer's fingerprint* or just *English writing statistics*
- What the real-world FPR distribution looks like across a population, not just one user
- Whether the LLM-detector head helps or hurts when the target writer happens to write in an LLM-like formal register

### Part 3 — Does it survive an attacker who's trying? (adversarial hardening)

Current attacker tiers assume the attacker has a few real examples and an LLM. By the time Chimera Secured is in production, real attackers will have more. This part of the eval asks: what's the model's behavior against attacker classes we haven't defended against yet?

**Adversarial tiers to add (beyond current zero/few/high):**

1. **Harvested-mailbox RAG attacker.** Attacker has retrieval-augmented access to the target's historical sent folder. Prompt: "Write an email from [target] to [recipient] about [topic]. Here are 50 of their real emails for style reference; use them verbatim where natural." This simulates the realistic post-compromise case where the attacker has stolen the mailbox.

2. **Multi-LLM ensemble attacker.** Generate 5 candidates per attack using 5 different LLMs (Claude, GPT-4, Llama, Mistral, Gemini). Pick the candidate whose stylometric distance to the target is lowest (i.e., the attacker is using our own detector signals against us).

3. **Human impersonation.** The hardest case, and we probably can't fully beat it. A real person reads 20 of the target's emails and writes a BEC-style ask in their own phrasing, informed by the target's style. We need at least 20–50 of these from willing human collaborators. This tier is the honest floor.

4. **Signature/PII-preserving attacker.** Attacker includes the target's real signature block, real phone number, real office address (harvested from reply chains). This was the bug that inflated iteration-1 scores from 59% catch to an illusory high number — we need to test for it, not be surprised by it.

**Bar:** Catch@5%FPR ≥ 0.60 per adversarial tier. Note the FPR budget loosens — these attackers are genuinely harder, and we'd rather flag a few extra real emails than promise impossible catch rates.

---

## Parallel execution with an agent swarm

**The historical problem:** Each Enron writer eval — generate 600 attacker emails, score 750 emails through the composite, compute metrics — takes 30–60 minutes of wall time single-threaded. Running 40 writers sequentially is 20–40 hours. That's not OK for iteration velocity, and it's what caused past eval cycles to turn into multi-day waits that discouraged experimentation.

**The solution:** Parallel agent execution. Structured as follows:

### Swarm topology

- **Orchestrator agent** (1): reads the writer manifest, dispatches writer-eval jobs to workers, aggregates results, generates the final report.
- **Attacker generation workers** (N, typically 5–10): each takes one writer, runs `background_generator_swarm.py` for all three tiers, writes the attacker corpus to disk. Embarrassingly parallel. Rate-limited by the OpenRouter API budget, not by CPU.
- **Scoring workers** (N, typically 5–10): each takes one writer's attacker corpus + real held-out + CPP, runs the full composite, writes per-writer metrics JSON. Parallel by writer, CPU-bound on the stylometric feature extraction.
- **Adversarial workers** (2–4): specialized workers for the four adversarial tiers. The human-impersonation tier isn't automatable — we farm it to actual humans offline and load the corpus.
- **Aggregation agent** (1): reads all per-writer metrics, computes the distribution stats, generates plots, writes the final report to `eval_results/cross_writer_YYYYMMDD.json` + HTML dashboard.

### Practical notes

- Workers run as independent Python processes (not threads — GIL matters for the scoring workload) and coordinate via a simple `eval_jobs/` directory with JSON job manifests and lock files. No Redis/Celery needed; this is tens of jobs, not thousands.
- OpenRouter rate limits are the binding constraint on attacker generation. 10 parallel workers hit ~60 req/s which is within most OpenRouter plan limits but watch the dashboard.
- Each worker writes its own log file so a failing writer doesn't poison the run. The orchestrator skips and retries failed writers at the end.
- Expected wall time with 8 workers on a commodity machine: ~2 hours for the full 40-writer Enron cross-validation. That's the difference between "run this overnight once a week" and "run this every time I try a new feature."

### Gating step: dry-run on 3 writers before the full swarm

Every eval run starts with 3 random writers first. If the metrics look sensible (not all writers scoring 0.99 — that means a leak; not all scoring 0.5 — that means the model isn't learning), the full swarm kicks off. If the dry-run looks weird, we debug with a fast feedback loop instead of discovering the problem 2 hours later.

---

## Ablation studies — what actually matters

Once the scoring composite has 4+ components (stylometric, LLM-detector head, DLP, context, potential ask-type novelty detector), every shipped version should answer: what does each component contribute?

**Ablation matrix (run per release candidate):**

|                         | Overall AUC | Few-shot catch | High-fid catch | Real FPR |
|-------------------------|-------------|----------------|----------------|----------|
| Stylometric only        | ?           | ?              | ?              | ?        |
| LLM-detector only       | ?           | ?              | ?              | ?        |
| DLP only                | ?           | ?              | ?              | ?        |
| Stylo + LLM-det         | ?           | ?              | ?              | ?        |
| Stylo + DLP             | ?           | ?              | ?              | ?        |
| Full composite          | ?           | ?              | ?              | ?        |

**Why this matters more than it sounds.** If any single component is within 2 percentage points of the full composite, we ship the simpler thing. Complexity has a maintenance cost, and a simple model that performs within margin-of-error of a complex one wins every time. The ablation tells us the real marginal value of each piece. This is also how we avoid shipping "the context layer was broken but nobody noticed because the headline number looked fine" a second time.

---

## Calibration reliability

The composite has to emit not just rankings but *probabilities* — because the pilot phases rely on threshold-based policy (warn if ≥ 0.5, block if ≥ 0.8, etc.) and those thresholds only mean something if the probabilities are calibrated.

**Reliability diagram check (per release):**
- Bin predictions by predicted probability (10 buckets: 0.0–0.1, 0.1–0.2, …, 0.9–1.0)
- For each bucket, compute the actual fraction of attacker emails
- Plot predicted vs. actual
- Perfect calibration = diagonal line
- If the curve is systematically above the line, the model is overconfident (we're predicting 0.8 but it's actually 0.5). That bites us in FPR.
- If systematically below, we're underconfident and missing catches we should get.

**Expected result for iteration 1 of the new composite:** mild overconfidence on the high-end (0.8+ bucket actually runs at 0.65–0.70 precision). Platt scaling on the output fixes this; it's a one-line change to pipe the composite through `CalibratedClassifierCV`.

**Bar:** Expected calibration error (ECE) ≤ 0.05 across all buckets.

---

## ENV bypass accounting (Layer-0 exclusions)

When emails are excluded from scoring by envelope/address rules (internal-to-internal, known trusted partners, system-generated mail from Jira/DocuSign/Salesforce etc. — this is a Shield feature, see `architecture_spec.md`), the eval must NOT count them as "caught real emails." They were never scored.

**Two separate metrics:**
1. **Bypass rate.** What fraction of real mail flow never reaches the scorer. Should be 40–70% in a typical enterprise inbox (most mail is internal or system-generated).
2. **Score-rate FPR and catch.** Computed only on emails that passed Layer 0 and were actually scored.

Reporting catch rate on "all real mail" including bypassed mail makes Chimera look much better than it is. Don't do that. Report both numbers separately, always.

---

## What a good eval report looks like

Every eval run produces an HTML report with the following structure. If any section is missing, the run is not considered complete.

1. **Headline.** One paragraph: what changed, what we tested, overall verdict.
2. **Ship-gate table.** Per-tier metrics against the five gates, PASS/FAIL per line.
3. **Cross-writer distribution.** Box plot of catch@2%FPR across all 40 Enron writers. Callouts for best and worst.
4. **Ablation matrix.** See above.
5. **Reliability diagram.** Calibration curve + ECE.
6. **Bypass-layer stats.** Bypass rate, reason breakdown, sample bypassed vs. scored real emails.
7. **Confusion examples.** 10 highest-scored false positives (real emails that looked like attackers) and 10 lowest-scored false negatives (attackers that looked real). These are what you read to understand what the model is getting wrong.
8. **What changed from last run.** Diff against the previous eval's numbers with 95% confidence intervals. Regressions flagged in red.

The current `lean_run.out.txt` hits items 1 and 2. Everything from 3 onward is missing and needs to be built. The aggregation agent in the swarm owns building items 3–8.

---

## Honest floor: what we cannot measure

Some things the eval cannot prove, and we should say so out loud rather than hope the buyer doesn't notice:

1. **True zero-day attackers.** We can only test against attack styles we can enumerate. If attackers develop a new technique (e.g., dynamic per-recipient style adaptation using a compromised voice clone to extract intent from the recipient's own sent folder), we don't detect it until we've seen an example.

2. **Base rate variability.** Enron BEC doesn't exist in the dataset because BEC wasn't a category in 2001. Our attacker corpus is synthetic. Real-world BEC is rarer and more heterogeneous than our corpus. The Phase 1 shadow-mode pilot is the first time we see real base rates.

3. **Recipient behavior under alert fatigue.** Our eval measures detector quality, not human response. A 95% catch rate is useless if users click banners through without reading them. Phase 2 banner-useful rates are how we measure this, and it's only measurable in production.

4. **Long-tail styles.** 40 Enron writers is a sample. There are writing styles our selection missed (non-native English, heavy jargon specialist registers, senior executives who write exclusively in one-line bullets). We'll learn these in pilot.

Writing this section honestly is part of Gate 5. If the pitch promises detection confidence we don't have, the pilot eats it in month 2.
