"""
Wave 3 - Risk Composer.

Combines three signals into a single verdict + composite risk score:
  - Layer 1 (Chimera stylometry): "does this SOUND like Steve?" -- higher = more suspicious
  - Layer 2 (DLP content):         "does this contain harmful payload?" -- higher = more suspicious
  - Layer 3 (Context):             "does this FIT Steve's patterns?" -- higher = more anomalous

Outputs:
  - verdict: "pass" | "flag" | "block"
  - composite_score in [0, 1] for AUC measurement

Design principles (from wave3_contextual_analysis_spec.md):
  - Don't just sum the scores -- layers address different threats
  - Chimera high alone is enough to block
  - DLP alone is enough to block (content-based catch)
  - Mid-chimera + anomalous context = flag for review
  - Low chimera + plausible context + clean content = pass (acceptable residual risk)

Thresholds are anchored on Wave 1 real-score distribution:
  - median real score ~= 0.0022
  - p95 real score    ~= 0.0034
  - Wave 1 threshold   = 0.0071  (where the eval put the cut)
  - Some fakes reach   = 0.3+

So we use:
  CHIMERA_LOW  = 0.002   (below this, looks like a normal Steve email)
  CHIMERA_MID  = 0.007   (Wave 1 shipping threshold)
  CHIMERA_HIGH = 0.02    (very suspicious by stylometry alone)

Context scores come out of IsolationForest calibrated to [0, 1], so use:
  CONTEXT_HIGH = 0.70    (top quartile-ish of anomaly space)
  CONTEXT_MID  = 0.50

DLP is assumed binary-ish in [0, 1] (we pass 0.0 for "no DLP yet").
"""

from dataclasses import dataclass


# Chimera thresholds -- anchored to Wave 1 real-score distribution
CHIMERA_LOW = 0.002
CHIMERA_MID = 0.007
CHIMERA_HIGH = 0.02

# Context thresholds -- anomaly score from IsolationForest is calibrated to [0, 1]
CONTEXT_HIGH = 0.70
CONTEXT_MID = 0.50

# DLP threshold -- any non-trivial hit
DLP_HIT = 0.5


@dataclass
class Verdict:
    verdict: str
    composite_score: float
    reason: str


def _normalize_chimera(s: float) -> float:
    """Map raw Chimera score to [0, 1] for composite math.

    Piecewise linear:
      0             -> 0.0
      CHIMERA_LOW   -> 0.2   (normal territory)
      CHIMERA_MID   -> 0.5   (Wave 1 ship threshold)
      CHIMERA_HIGH  -> 0.9   (clearly bad)
      0.3+          -> 1.0
    """
    if s <= 0:
        return 0.0
    if s >= 0.3:
        return 1.0
    if s < CHIMERA_LOW:
        return 0.2 * (s / CHIMERA_LOW)
    if s < CHIMERA_MID:
        return 0.2 + 0.3 * (s - CHIMERA_LOW) / (CHIMERA_MID - CHIMERA_LOW)
    if s < CHIMERA_HIGH:
        return 0.5 + 0.4 * (s - CHIMERA_MID) / (CHIMERA_HIGH - CHIMERA_MID)
    return 0.9 + 0.1 * (s - CHIMERA_HIGH) / (0.3 - CHIMERA_HIGH)


def compose(chimera_score: float, context_score: float = 0.0, dlp_score: float = 0.0) -> Verdict:
    """Fuse the three signals into a verdict + composite risk score.

    All inputs are "suspicion" scores -- higher = more suspicious.
    """
    c = _normalize_chimera(chimera_score)
    ctx = max(0.0, min(1.0, context_score))
    dlp = max(0.0, min(1.0, dlp_score))

    if chimera_score >= CHIMERA_HIGH:
        verdict = "block"
        reason = "chimera_high"
        composite = max(c, 0.9)

    elif chimera_score >= CHIMERA_MID and dlp >= DLP_HIT:
        verdict = "block"
        reason = "chimera_mid+dlp_hit"
        composite = max(c, dlp, 0.9)

    elif chimera_score < CHIMERA_MID and dlp >= DLP_HIT:
        verdict = "block"
        reason = "dlp_hit"
        composite = max(dlp, 0.85)

    elif chimera_score >= CHIMERA_MID and ctx >= CONTEXT_HIGH:
        verdict = "flag"
        reason = "chimera_mid+context_high"
        composite = 0.5 * c + 0.5 * ctx

    elif chimera_score < CHIMERA_MID and ctx >= CONTEXT_HIGH:
        verdict = "flag"
        reason = "context_high"
        composite = 0.35 * c + 0.65 * ctx

    elif chimera_score >= CHIMERA_LOW and ctx >= CONTEXT_MID:
        verdict = "pass"
        reason = "grey_zone"
        composite = 0.5 * c + 0.5 * ctx

    else:
        verdict = "pass"
        reason = "clean"
        composite = 0.6 * c + 0.3 * ctx + 0.1 * dlp

    composite = max(0.0, min(1.0, composite))
    return Verdict(verdict=verdict, composite_score=composite, reason=reason)


def compose_batch(chimera_scores, context_scores, dlp_scores=None):
    """Vectorized-ish compose over parallel lists."""
    n = len(chimera_scores)
    if dlp_scores is None:
        dlp_scores = [0.0] * n
    assert len(context_scores) == n and len(dlp_scores) == n

    verdicts, composites, reasons = [], [], []
    for ch, ctx, dlp in zip(chimera_scores, context_scores, dlp_scores):
        v = compose(ch, ctx, dlp)
        verdicts.append(v.verdict)
        composites.append(v.composite_score)
        reasons.append(v.reason)
    return verdicts, composites, reasons


if __name__ == "__main__":
    cases = [
        ("real-looking, normal ctx",      0.0015, 0.20, 0.0),
        ("real-looking, high ctx",        0.0015, 0.85, 0.0),
        ("mid chimera, normal ctx",       0.0050, 0.20, 0.0),
        ("mid chimera, high ctx",         0.0050, 0.80, 0.0),
        ("high chimera alone",            0.0300, 0.20, 0.0),
        ("low chimera + DLP hit",         0.0015, 0.20, 0.9),
        ("mid chimera + DLP hit",         0.0050, 0.20, 0.9),
        ("zero-shot fake (score 0.3)",    0.3000, 0.20, 0.0),
    ]
    for name, ch, ctx, dlp in cases:
        v = compose(ch, ctx, dlp)
        print(f"{name:35s}  ch={ch:.4f}  ctx={ctx:.2f}  dlp={dlp:.2f}  ->  "
              f"{v.verdict:5s}  composite={v.composite_score:.3f}  ({v.reason})")
