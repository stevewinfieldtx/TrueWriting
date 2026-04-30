"""
Chimera Secured — Wave 2 Feature Modules
----------------------------------------
Two drop-in feature extractors that plug into chimera_scorer.py:

  1) NegativeSpaceExtractor
     Learns "things Steve never does" from the real corpus and flags violations.
     Rationale: a single hit on a hard-negative pattern is a stronger signal
     than a distributional deviation — this is the Kimi/Z.ai/DeepSeek
     "negative space" recommendation (items 5 of the 9-item convergent list).

  2) RecipientConditionalProfiler
     Per-recipient baselines for Steve's top-N correspondents. Short emails
     to frequent correspondents are scored against a *tight* per-recipient
     baseline; short emails to rare recipients fall back to the global
     baseline with added skepticism (item 6 of the 9-item list).

Both return numpy vectors and expose .feature_names() for XGBoost interpretability.

Integration points are at the bottom of this file (FeatureBundle).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

# ----------------------------- shared utils ---------------------------------

WORD_RE = re.compile(r"[A-Za-z']+")

def tokenize(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(text or "")]

def first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if text else ""

def last_nonblank_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""

def opening_phrase(text: str, n_words: int = 4) -> str:
    toks = tokenize(first_line(text))
    return " ".join(toks[:n_words])

def closing_phrase(text: str, n_words: int = 3) -> str:
    toks = tokenize(last_nonblank_line(text))
    return " ".join(toks[-n_words:]) if toks else ""

def ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


# ===========================================================================
# 1) Negative-Space Extractor
# ===========================================================================

@dataclass
class NegativeSpaceExtractor:
    """
    Learns what Steve *never* does. At score time, any hit on one of these
    "never" patterns is a high-weight anomaly signal.

    Patterns tracked:
      - opening phrases (first 2-4 words)
      - closing/sign-off phrases (last 2-3 words before signature)
      - specific word n-grams (2-grams + 3-grams) from the full body
      - punctuation/whitespace tells (em-dash, ellipsis, double-space etc.)

    "Never" means: absent from Steve's real corpus above a min-support floor
    on the positive side, i.e. patterns that are *common* in fakes but zero
    in real. We harden that by requiring the real-side count to be exactly 0
    across min_real_corpus_size emails.
    """

    opening_topk: int = 50
    closing_topk: int = 40
    ngram_topk: int = 200
    min_real_corpus_size: int = 200

    # learned state
    real_openings_: set = field(default_factory=set)
    real_closings_: set = field(default_factory=set)
    real_2grams_: set = field(default_factory=set)
    real_3grams_: set = field(default_factory=set)
    real_punct_tells_: dict = field(default_factory=dict)
    fitted_: bool = False

    # --- fit ----------------------------------------------------------------
    def fit(self, real_bodies: Iterable[str]) -> "NegativeSpaceExtractor":
        real_bodies = list(real_bodies)
        if len(real_bodies) < self.min_real_corpus_size:
            # still fit, but mark so scorer can warn
            pass

        open_counter: Counter[str] = Counter()
        close_counter: Counter[str] = Counter()
        g2: Counter[str] = Counter()
        g3: Counter[str] = Counter()
        punct: Counter[str] = Counter()

        for body in real_bodies:
            op = opening_phrase(body, 4)
            cl = closing_phrase(body, 3)
            if op:
                open_counter[op] += 1
                # also bank 2-word opener as a softer signal
                open_counter[" ".join(op.split()[:2])] += 1
            if cl:
                close_counter[cl] += 1

            toks = tokenize(body)
            g2.update(ngrams(toks, 2))
            g3.update(ngrams(toks, 3))

            # punctuation / whitespace signals
            if "—" in body: punct["em_dash"] += 1
            if "…" in body: punct["ellipsis_unicode"] += 1
            if "..." in body: punct["ellipsis_ascii"] += 1
            if "  " in body: punct["double_space"] += 1
            if re.search(r"\s!\s|\s\?\s", body): punct["spaced_punct"] += 1

        # "real set" = patterns Steve ACTUALLY uses (with support)
        # negative-space violation = an incoming pattern NOT in real set
        self.real_openings_ = {k for k, c in open_counter.items() if c >= 1}
        self.real_closings_ = {k for k, c in close_counter.items() if c >= 1}
        self.real_2grams_   = {k for k, c in g2.items() if c >= 2}
        self.real_3grams_   = {k for k, c in g3.items() if c >= 2}
        # punct tells: note which tells Steve uses at all
        self.real_punct_tells_ = {k: c for k, c in punct.items()}

        self.fitted_ = True
        return self

    # --- transform ----------------------------------------------------------
    FEATURES = [
        "neg_opening_violation",        # 0/1 — opening phrase never seen in real
        "neg_opening_2w_violation",     # 0/1 — 2-word opener never seen in real
        "neg_closing_violation",        # 0/1 — closing phrase never seen in real
        "neg_2gram_violation_rate",     # fraction of body 2-grams not in real set
        "neg_3gram_violation_rate",     # fraction of body 3-grams not in real set
        "neg_2gram_violation_count",    # raw count (helps short-email signal)
        "neg_punct_em_dash_mismatch",   # 1 if body uses em-dash and Steve never does, else 0
        "neg_punct_ellipsis_mismatch",
        "neg_violation_score",          # weighted composite
    ]

    def feature_names(self) -> list[str]:
        return list(self.FEATURES)

    def transform_one(self, body: str) -> np.ndarray:
        if not self.fitted_:
            raise RuntimeError("NegativeSpaceExtractor not fitted")

        op4 = opening_phrase(body, 4)
        op2 = " ".join(op4.split()[:2])
        cl  = closing_phrase(body, 3)

        op_viol   = 1.0 if op4 and op4 not in self.real_openings_ else 0.0
        op2_viol  = 1.0 if op2 and op2 not in self.real_openings_ else 0.0
        cl_viol   = 1.0 if cl  and cl  not in self.real_closings_ else 0.0

        toks = tokenize(body)
        b2 = ngrams(toks, 2)
        b3 = ngrams(toks, 3)
        g2_viol = [g for g in b2 if g not in self.real_2grams_]
        g3_viol = [g for g in b3 if g not in self.real_3grams_]

        g2_rate = len(g2_viol) / max(1, len(b2))
        g3_rate = len(g3_viol) / max(1, len(b3))
        g2_cnt  = float(len(g2_viol))

        em_mismatch = 1.0 if ("—" in body and "em_dash" not in self.real_punct_tells_) else 0.0
        el_mismatch = 1.0 if (("…" in body or "..." in body)
                              and "ellipsis_unicode" not in self.real_punct_tells_
                              and "ellipsis_ascii"   not in self.real_punct_tells_) else 0.0

        # Composite: opening/closing violations weight highest because they're
        # the most structural tells; n-gram rate rounds out for longer bodies.
        composite = (
            3.0 * op_viol +
            1.0 * op2_viol +
            2.0 * cl_viol +
            2.0 * g3_rate +
            1.0 * g2_rate +
            0.5 * em_mismatch +
            0.5 * el_mismatch
        )

        return np.array([
            op_viol, op2_viol, cl_viol,
            g2_rate, g3_rate, g2_cnt,
            em_mismatch, el_mismatch,
            composite,
        ], dtype=np.float32)

    def transform(self, bodies: Iterable[str]) -> np.ndarray:
        return np.vstack([self.transform_one(b) for b in bodies])


# ===========================================================================
# 2) Recipient-Conditional Profiler
# ===========================================================================

@dataclass
class RecipientStats:
    n: int = 0
    mean_len: float = 0.0
    std_len: float = 1.0
    mean_sent_len: float = 0.0
    std_sent_len: float = 1.0
    top_openings: set = field(default_factory=set)
    uses_em_dash: bool = False


@dataclass
class RecipientConditionalProfiler:
    """
    Builds per-recipient baselines for the top-N correspondents. At score time
    the incoming email's recipient selects which baseline to use; emails to
    rare recipients fall through to the global baseline with a configurable
    skepticism bonus.

    Handles: To + Cc addresses. Primary key is the lowercased email address of
    the first recipient. You can override by passing recipient_key explicitly.
    """

    top_n: int = 5
    min_samples: int = 8
    skepticism_bonus_rare: float = 1.0  # added to anomaly score for rare recipients

    # learned state
    per_recipient_: dict = field(default_factory=dict)  # addr -> RecipientStats
    global_: Optional[RecipientStats] = None
    known_recipients_: set = field(default_factory=set)
    fitted_: bool = False

    # --- fit ----------------------------------------------------------------
    def _stats_from(self, bodies: list[str]) -> RecipientStats:
        if not bodies:
            return RecipientStats()
        lens = np.array([len(tokenize(b)) for b in bodies], dtype=np.float32)
        sent_lens = []
        openers: Counter[str] = Counter()
        em = 0
        for b in bodies:
            sents = re.split(r"[.!?\n]+", b)
            sents = [s for s in sents if s.strip()]
            if sents:
                sent_lens.extend([len(tokenize(s)) for s in sents])
            op = opening_phrase(b, 3)
            if op: openers[op] += 1
            if "—" in b: em += 1
        sent_lens_arr = np.array(sent_lens, dtype=np.float32) if sent_lens else np.array([0.0])
        return RecipientStats(
            n=len(bodies),
            mean_len=float(lens.mean()),
            std_len=float(lens.std() or 1.0),
            mean_sent_len=float(sent_lens_arr.mean()),
            std_sent_len=float(sent_lens_arr.std() or 1.0),
            top_openings={k for k, _ in openers.most_common(10)},
            uses_em_dash=em >= max(1, 0.1 * len(bodies)),
        )

    def fit(self, samples: Iterable[dict]) -> "RecipientConditionalProfiler":
        """
        samples: iterable of {"body": str, "recipient": str} dicts.
        Recipient should be lowercased email. If unknown, pass "" and it'll
        only contribute to the global profile.
        """
        by_recip: dict[str, list[str]] = defaultdict(list)
        all_bodies: list[str] = []
        for s in samples:
            body = s.get("body", "")
            rec = (s.get("recipient") or "").strip().lower()
            all_bodies.append(body)
            if rec:
                by_recip[rec].append(body)

        # global baseline
        self.global_ = self._stats_from(all_bodies)

        # pick top-N by frequency, requiring min_samples
        eligible = [(r, bs) for r, bs in by_recip.items() if len(bs) >= self.min_samples]
        eligible.sort(key=lambda x: -len(x[1]))
        for r, bs in eligible[: self.top_n]:
            self.per_recipient_[r] = self._stats_from(bs)
            self.known_recipients_.add(r)

        self.fitted_ = True
        return self

    # --- transform ----------------------------------------------------------
    FEATURES = [
        "rec_is_known",                  # 0/1
        "rec_len_z",                     # z-score of word count vs. chosen baseline
        "rec_sent_len_z",                # z-score of avg sentence length
        "rec_opening_known",             # 0/1 — opening seen in baseline's top set
        "rec_em_dash_mismatch",          # 0/1 — body uses em-dash but baseline doesn't
        "rec_skepticism",                # constant bonus if rare-recipient fallback used
    ]

    def feature_names(self) -> list[str]:
        return list(self.FEATURES)

    def _baseline_for(self, recipient: str) -> tuple[RecipientStats, bool]:
        r = (recipient or "").strip().lower()
        if r in self.per_recipient_:
            return self.per_recipient_[r], True
        return self.global_ or RecipientStats(), False

    def transform_one(self, body: str, recipient: str = "") -> np.ndarray:
        if not self.fitted_:
            raise RuntimeError("RecipientConditionalProfiler not fitted")

        base, known = self._baseline_for(recipient)

        toks = tokenize(body)
        wc = len(toks)
        sents = [s for s in re.split(r"[.!?\n]+", body) if s.strip()]
        sl = np.mean([len(tokenize(s)) for s in sents]) if sents else 0.0

        len_z = (wc - base.mean_len) / (base.std_len or 1.0)
        sent_z = (sl - base.mean_sent_len) / (base.std_sent_len or 1.0)

        op3 = opening_phrase(body, 3)
        op_known = 1.0 if op3 in base.top_openings else 0.0

        em_mismatch = 1.0 if ("—" in body and not base.uses_em_dash) else 0.0

        skepticism = 0.0 if known else self.skepticism_bonus_rare

        return np.array([
            1.0 if known else 0.0,
            float(len_z),
            float(sent_z),
            op_known,
            em_mismatch,
            skepticism,
        ], dtype=np.float32)

    def transform(self, bodies: Iterable[str], recipients: Iterable[str]) -> np.ndarray:
        return np.vstack([
            self.transform_one(b, r) for b, r in zip(bodies, recipients)
        ])


# ===========================================================================
# 3) FeatureBundle — the one-line wire-in
# ===========================================================================

@dataclass
class Wave2FeatureBundle:
    """
    Convenience wrapper so chimera_scorer.py gets exactly one object to call.

    In chimera_scorer.py:

        from wave2_features import Wave2FeatureBundle

        # during training:
        wave2 = Wave2FeatureBundle()
        wave2.fit(real_samples)   # list of {"body": str, "recipient": str}
        X_wave2 = wave2.transform(real_bodies, real_recipients)
        # hstack with your existing feature matrix:
        X_train = np.hstack([X_existing, X_wave2])

        # during scoring:
        x_wave2 = wave2.transform_one(body, recipient)
        x_full  = np.hstack([x_existing, x_wave2])

    Save/load with joblib like the rest of the scorer.
    """
    negspace: NegativeSpaceExtractor = field(default_factory=NegativeSpaceExtractor)
    recprof:  RecipientConditionalProfiler = field(default_factory=RecipientConditionalProfiler)

    def fit(self, real_samples: list[dict]) -> "Wave2FeatureBundle":
        bodies = [s["body"] for s in real_samples]
        recips = [s.get("recipient", "") for s in real_samples]
        self.negspace.fit(bodies)
        self.recprof.fit(real_samples)
        return self

    def feature_names(self) -> list[str]:
        return self.negspace.feature_names() + self.recprof.feature_names()

    def transform_one(self, body: str, recipient: str = "") -> np.ndarray:
        return np.concatenate([
            self.negspace.transform_one(body),
            self.recprof.transform_one(body, recipient),
        ])

    def transform(self, bodies: list[str], recipients: Optional[list[str]] = None) -> np.ndarray:
        if recipients is None:
            recipients = [""] * len(bodies)
        ns = self.negspace.transform(bodies)
        rp = self.recprof.transform(bodies, recipients)
        return np.hstack([ns, rp])
