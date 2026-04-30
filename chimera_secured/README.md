# Chimera Secured — Pilot Readiness Workspace

This folder is the clean working space for getting Chimera Secured from "the lab eval looks promising" to "I honestly think this will work in a 60,000-seat Rain Networks pilot."

It is deliberately separate from the rest of `TrueWriting/` so the planning isn't tangled up with the experimental files (Wave 1/2/3 evals, legacy scorers, dropped context layer, multiple competing composers). The code under `TrueWriting/` and `TrueWriting/shield/` is the starting material — this folder is where we reason about what to keep, what to rebuild, and in what order.

## The one-line product statement

Chimera Secured detects Business Email Compromise by comparing each outbound or internal email against a per-user Communication Personality Profile (CPP) built from that user's real sending history, combined with payload-level DLP signals for content-category awareness. Only the CPP leaves the customer environment — email bodies never do.

## The one-line problem statement

We have one real training identity (Steve's Hotmail), a production Shield scorer that doesn't use any of the research-stack gains, a context layer that actively hurts us (0.57 AUC, 17% false-flag rate), and a ship gate that currently fails on the two attacker tiers that matter most (few-shot: 0.52, high-fidelity: 0.60, target: 0.85).

## What's in this folder

- **`README.md`** — this file
- **`pilot_readiness_plan.md`** — the centerpiece. Five gates we need to clear before any Rain pilot, and a phased rollout that pairs confidence with blast-radius containment
- **`eval_strategy.md`** — how we build honest confidence when we only have one real user, centered on Enron-corpus cross-writer validation and adversarial eval hardening, executed via parallel agent swarms
- **`architecture_spec.md`** — the target technical architecture: Bayesian content-conditional recomposer, detector ensemble (stylometric + LLM-detector head + DLP + ask-type novelty), and the port-in plan for Shield that respects the sovereign-data constraint

## Guiding principles

1. **Honest over optimistic.** "I think this will work" is only said when the eval supports it across writers, not just Steve.
2. **Sovereign by design.** CPPs leave the tenant. Email bodies do not. Every architectural choice gets checked against this.
3. **Detection before enforcement.** The first pilot phase ships in shadow/log-only mode. We earn blocking rights with real-world FPR data, we don't assume them.
4. **Catch the right things.** A 9-word lunch ask that doesn't sound like Steve is not a threat. A 7-word ask with a wire instruction or credit card is. Content category modulates style sensitivity.
5. **Parallel everywhere possible.** Evaluation is the bottleneck. We scale it with agent swarms, not by waiting.
