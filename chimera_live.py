"""
Chimera Secured - Live Scoring (Local, Offline)
=================================================

This is the production inference path. It is deliberately tiny and has ZERO
network dependencies. Drop this into a transport rule, an MSP container, an
on-prem server, a Lambda - anywhere that can run Python and load a pickle.

No LLM calls. No OpenRouter. No outbound traffic of any kind.

Inputs:
  - chimera_model.pkl    (produced by chimera_scorer.py --train, per user)
  - email body           (string, UTF-8)

Outputs:
  - score (float 0.0-1.0, higher = more likely impersonation)
  - verdict ("verified" / "flagged" / "blocked")
  - top reasons (human-readable)

Latency target: <100ms on a single email on commodity hardware.

Usage patterns:

  # CLI: pipe an email body in
  cat suspicious.eml | python chimera_live.py --model ./chimera_model.pkl

  # As a library in your transport-rule handler
  from chimera_live import LiveScorer
  scorer = LiveScorer("/path/to/chimera_model.pkl")
  result = scorer.score(email_body_text)
  if result.verdict == "blocked":
      hold_email()
  elif result.verdict == "flagged":
      add_banner()

  # Batch: score a folder of .txt files
  python chimera_live.py --model ./chimera_model.pkl --batch ./inbox/

Per-user thresholds can be configured by overriding the scorer's .thresholds dict.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

# NOTE: these imports are deliberately only the ones needed at inference time.
# No sklearn training deps, no network libs, no LLM SDKs.
import numpy as np
from scipy.sparse import hstack, csr_matrix

# Import the feature extractors from the training module (no circular dep -
# chimera_scorer.py doesn't import us).
from chimera_scorer import (
    handcrafted_features,
    function_word_vector,
    _strip_email_artifacts,
)


@dataclass
class ScoreResult:
    score: float              # 0.0 = clearly the user, 1.0 = clearly not
    verdict: str              # "verified" | "flagged" | "blocked"
    latency_ms: float
    reasons: List[str]
    word_count: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class LiveScorer:
    """Local, offline scorer. Load once, score many.

    Thread-safe for read-only scoring after construction.
    """

    DEFAULT_THRESHOLDS = {
        "flag": 0.55,       # score >= this -> flag with warning banner
        "block": 0.80,      # score >= this -> hold outbound / quarantine inbound
    }

    def __init__(self, model_path: str | Path, thresholds: Optional[dict] = None):
        with open(model_path, "rb") as f:
            bundle = pickle.load(f)
        self.clf = bundle["clf"]
        self.char_vec = bundle["char_vec"]
        self.dense_scaler = bundle["dense_scaler"]
        self.thresholds = thresholds or dict(self.DEFAULT_THRESHOLDS)

        # Cache handcrafted feature names for reason-giving
        self._dense_feature_names = [
            "avg_sent_len", "std_sent_len", "avg_word_len",
            "long_word_rate", "short_word_rate", "type_token", "fragment_rate",
            "em_dash", "semicolon", "exclaim", "question", "ellipsis",
            "comma", "paren", "colon", "all_caps",
            "contractions", "i_rate", "we_rate", "log_length",
        ]

    def score(self, email_body: str) -> ScoreResult:
        t0 = time.perf_counter()
        clean = _strip_email_artifacts(email_body)
        word_count = len(clean.split())

        # Degenerate case: too short to score reliably
        if word_count < 15:
            return ScoreResult(
                score=0.0,
                verdict="verified",
                latency_ms=(time.perf_counter() - t0) * 1000,
                reasons=["email too short to analyze reliably"],
                word_count=word_count,
            )

        X_char = self.char_vec.transform([email_body])
        dense = np.concatenate([handcrafted_features(email_body), function_word_vector(email_body)])
        X_dense = self.dense_scaler.transform(dense.reshape(1, -1))
        X = hstack([X_char, csr_matrix(X_dense)]).tocsr()

        p_user = float(self.clf.predict_proba(X)[0, 1])
        anomaly_score = 1.0 - p_user

        if anomaly_score >= self.thresholds["block"]:
            verdict = "blocked"
        elif anomaly_score >= self.thresholds["flag"]:
            verdict = "flagged"
        else:
            verdict = "verified"

        reasons = self._explain(email_body, dense, anomaly_score)
        latency_ms = (time.perf_counter() - t0) * 1000

        return ScoreResult(
            score=anomaly_score,
            verdict=verdict,
            latency_ms=latency_ms,
            reasons=reasons,
            word_count=word_count,
        )

    # ---- Explainability ----

    def _explain(self, email_body: str, dense_vec: np.ndarray, anomaly: float) -> List[str]:
        """Human-readable reasons. Compares the dense features against the training mean."""
        if anomaly < self.thresholds["flag"]:
            return ["matches user's established writing style"]
        # Compare this email's handcrafted features to the scaler's mean (which is the training mean)
        mean_vec = self.dense_scaler.mean_[:len(self._dense_feature_names)]
        std_vec = self.dense_scaler.scale_[:len(self._dense_feature_names)]
        this_vec = dense_vec[:len(self._dense_feature_names)]
        z = np.abs((this_vec - mean_vec) / np.maximum(std_vec, 1e-6))
        top_idx = np.argsort(z)[::-1][:3]
        reasons = []
        for i in top_idx:
            name = self._dense_feature_names[i]
            observed = this_vec[i]
            expected = mean_vec[i]
            if z[i] > 1.5:
                direction = "higher than" if observed > expected else "lower than"
                reasons.append(
                    f"{name}: {observed:.2f} ({direction} typical {expected:.2f}, z={z[i]:.1f})"
                )
        if not reasons:
            reasons.append("overall linguistic pattern deviates from user baseline")
        return reasons


# ---------- CLI ----------

def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Path to chimera_model.pkl")
    ap.add_argument("--batch", help="Folder of .txt/.eml files to score")
    ap.add_argument("--flag", type=float, default=None, help="Override flag threshold")
    ap.add_argument("--block", type=float, default=None, help="Override block threshold")
    args = ap.parse_args()

    thresholds = dict(LiveScorer.DEFAULT_THRESHOLDS)
    if args.flag is not None:
        thresholds["flag"] = args.flag
    if args.block is not None:
        thresholds["block"] = args.block

    scorer = LiveScorer(args.model, thresholds=thresholds)

    if args.batch:
        folder = Path(args.batch)
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() not in {".txt", ".eml", ".msg"}:
                continue
            body = p.read_text(encoding="utf-8", errors="ignore")
            r = scorer.score(body)
            print(f"{p.name:30s}  {r.verdict:10s}  score={r.score:.3f}  "
                  f"({r.latency_ms:.0f}ms, {r.word_count}w)")
    else:
        # Read from stdin
        body = sys.stdin.read()
        if not body.strip():
            sys.exit("No email body on stdin. Use --batch or pipe in an email.")
        r = scorer.score(body)
        print(r.to_json())


if __name__ == "__main__":
    _main()
