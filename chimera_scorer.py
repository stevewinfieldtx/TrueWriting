"""
Chimera Secured - Wave 1 Scorer
=================================

Changes in Wave 1 (addresses Kimi/Z.ai/DeepSeek convergent critiques):

  1. Classifier swapped from LogisticRegression to XGBoost (shallow trees,
     max_depth=3) wrapped in CalibratedClassifierCV with Platt scaling.
     Handles non-linear feature interactions that LR could not.

  2. class_weight='balanced' DROPPED. It was distorting probability
     calibration on the 1700:200 imbalance. Balance is now achieved by
     expanding the negative corpus (below), not by loss-weighting.

  3. Background corpus expanded 10x (200 -> 2000) and generated across
     multiple LLMs (Claude, GPT-4o, Llama 3.3, Mistral Large, Gemini)
     at varied temperatures (0.5-1.2). Fixes the "learning single-LLM
     voice vs Steve" problem that iteration 2 exposed.

  4. Length stratification added at scoring time. Emails under 50 words
     have their stylometric anomaly score capped at 0.3 — they can never
     trigger a "block" decision from stylometry alone. Short-email risk
     gets handled by the DLP content-weighting layer (Wave 3).

Usage unchanged:
  python chimera_scorer.py --build-background   # ~2000 emails, ~$3-5
  python chimera_scorer.py --train              # ~1-2 min
"""

import argparse
import json
import os
import pickle
import random
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import List, Dict, Any

import numpy as np

RANDOM_SEED = 42
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

SPLITS_DIR = Path("eval_splits")
MODEL_PATH = Path("chimera_model.pkl")
BACKGROUND_PATH = Path("background_emails.json")
SYNTH_BACKGROUND_COUNT = 2000

# Multi-LLM rotation for background generation (Wave 1 fix)
# Rotating across providers prevents the classifier from learning
# "single-LLM voice vs Steve" as a shortcut. Temperature also varies
# per call to broaden the stylistic distribution.
BACKGROUND_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-4.1",
    "meta-llama/llama-4-maverick",
    "google/gemini-2.5-flash",
    "x-ai/grok-4-fast",
]

# Length threshold below which stylometric anomaly scores get capped.
# Under this many words, there isn't enough signal for a confident
# decision; the product layer (DLP content weighting) handles risk.
LENGTH_GATE_WORDS = 50
LENGTH_GATE_SCORE_CAP = 0.3

# Top English function words - topic-independent, usage is unconscious
FUNCTION_WORDS = [
    "the", "of", "and", "to", "a", "in", "that", "is", "was", "it", "for", "on",
    "with", "as", "be", "by", "at", "this", "have", "from", "or", "had", "but",
    "not", "are", "they", "we", "you", "your", "i", "me", "my", "mine", "our",
    "ours", "he", "him", "his", "she", "her", "hers", "them", "their", "theirs",
    "who", "whom", "whose", "which", "what", "where", "when", "why", "how",
    "would", "could", "should", "will", "shall", "may", "might", "must", "can",
    "do", "does", "did", "done", "doing", "has", "having", "been", "being",
    "if", "then", "else", "than", "so", "because", "while", "although", "though",
    "after", "before", "during", "since", "until", "unless", "whereas", "yet",
    "about", "above", "across", "against", "along", "among", "around", "behind",
    "below", "beneath", "beside", "between", "beyond", "despite", "down", "into",
    "near", "off", "onto", "out", "over", "through", "throughout", "toward",
    "under", "up", "upon", "via", "within", "without", "just", "only", "also",
    "too", "very", "quite", "rather", "still", "already", "again", "always",
    "never", "often", "sometimes", "usually", "here", "there", "now", "today",
    "tomorrow", "yesterday", "soon", "later", "once", "twice", "both", "either",
    "neither", "each", "every", "any", "some", "all", "no", "none", "few",
    "many", "most", "much", "more", "less", "least", "other", "another", "same",
    "different", "such", "own", "itself", "himself", "herself", "themselves",
    "myself", "yourself", "ourselves",
]
assert len(FUNCTION_WORDS) >= 150, "need 150+ function words"
FUNCTION_WORDS = FUNCTION_WORDS[:150]


# ---------- Text preprocessing ----------

_WORD_RE = re.compile(r"[A-Za-z']+")
_SENT_RE = re.compile(r"[.!?]+\s+|\n\n+")


# Patterns that signal "everything from here on is NOT authored body content."
# The first line matching any of these ends the body. Case-insensitive.
_FOOTER_CUT_PATTERNS = [
    r"^--\s*$",                                 # RFC standard sig delimiter
    r"^---+\s*\w*\s*$",                         # ---Steve / --- style
    r"^_{5,}\s*$",                              # long underscore rule
    r"^-{5,}\s*$",                              # long dash rule
    r"^={5,}\s*$",                              # long equals rule
    r"^\*{5,}\s*$",                             # long asterisk rule
    r"^-+\s*Original Message\s*-+",             # Outlook forward/reply marker
    r"^-+\s*Forwarded message\s*-+",            # Gmail forward marker
    r"^On\s.+(wrote|sent|said):\s*$",           # Reply marker
    r"^From:\s",                                # Quoted email header block
    r"^Sent from my\s",                         # iPhone/Android auto-sig
    r"^Sent from Outlook\b",                    # Outlook mobile
    r"^Sent via\s",
    r"^Get Outlook for\s",
    r"^Download Outlook\b",

    # Legal / confidentiality footers (corporate auto-appended)
    r"^CONFIDENTIAL(ITY)?\b",
    r"^CONFIDENTIAL(ITY)?\s*NOTICE",
    r"^DISCLAIMER\b",
    r"^PRIVILEGED\b",
    r"^LEGAL\s*NOTICE\b",
    r"^IMPORTANT\s*(LEGAL\s*)?NOTICE\b",
    r"^NOTICE:\s*This\b",
    r"^This (e-?mail|message|communication|transmission)",
    r"^The information (contained |transmitted )?in this\b",
    r"^This (e-?mail|message) (and any attachments|is intended)",
    r"^If you (are|have) (not )?(the intended|received)",
    r"^Please (consider the environment|do not print)",
    r"^P\.?\s*Please consider the environment",
    r"^Any unauthorized (review|use|disclosure)",
    r"^Securities (offered|products)",           # FINRA compliance footers
    r"^HIPAA\b",
    r"^This (communication|transmission)\s+(is|may)",
]
_FOOTER_CUT_RE = re.compile("|".join(_FOOTER_CUT_PATTERNS), re.IGNORECASE)

# Inline PII/token scrubbers (remove anywhere in body, not just footer)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?\d{1,2}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def _strip_email_artifacts(text: str) -> str:
    """Aggressive scrub: drops quoted replies, signatures, legal footers, and PII.

    Ordering matters. We walk line by line, cutting the body at the first
    footer/signature marker, then strip inline PII from whatever survives.
    Over-stripping is safer than under-stripping - leakage beats signal loss.
    """
    out_lines = []
    for line in text.splitlines():
        s = line.strip()
        # Quoted reply
        if s.startswith(">"):
            continue
        # Outlook-style quoted header block (From:/Sent:/To:/Subject:)
        if re.match(r"^(From|Sent|To|Cc|Bcc|Subject|Date):\s", s, re.IGNORECASE):
            # If more than 2 headers in a row, we're inside a quoted block - stop
            if len(out_lines) >= 2 and any(
                re.match(r"^(From|Sent|To|Cc|Bcc|Subject|Date):\s", out_lines[-1].strip(), re.IGNORECASE)
                for _ in [0]
            ):
                break
            continue
        # Signature / footer cut
        if _FOOTER_CUT_RE.match(s):
            break
        out_lines.append(line)

    body = "\n".join(out_lines).strip()

    # Inline scrub: emails, phone numbers, URLs become harmless placeholder tokens.
    # We don't remove entirely because that would sometimes merge adjacent words.
    body = _EMAIL_RE.sub(" _EMAIL_ ", body)
    body = _PHONE_RE.sub(" _PHONE_ ", body)
    body = _URL_RE.sub(" _URL_ ", body)

    # Collapse extra whitespace
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _sentences(text: str) -> List[str]:
    parts = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    return parts or [text.strip()]


# ---------- Hand-crafted features (subset of your original 55) ----------

def handcrafted_features(text: str) -> np.ndarray:
    """Keep the features that actually discriminate. Drop the dialect/greeting noise."""
    clean = _strip_email_artifacts(text)
    words = _words(clean)
    sents = _sentences(clean)
    n_words = max(len(words), 1)
    n_sents = max(len(sents), 1)
    sent_lens = [len(_words(s)) for s in sents]

    def rate(needle_count: int) -> float:
        return 1000.0 * needle_count / n_words

    avg_sent_len = float(np.mean(sent_lens))
    std_sent_len = float(np.std(sent_lens))
    avg_word_len = float(np.mean([len(w) for w in words])) if words else 0.0
    long_word_rate = rate(sum(1 for w in words if len(w) > 8))
    short_word_rate = rate(sum(1 for w in words if len(w) <= 3))
    type_token = len(set(words)) / n_words
    fragment_rate = sum(1 for s in sent_lens if s <= 5) / n_sents

    em_dash = rate(clean.count("—") + clean.count("--"))
    semicolon = rate(clean.count(";"))
    exclaim = rate(clean.count("!"))
    question = rate(clean.count("?"))
    ellipsis = rate(clean.count("..."))
    comma = rate(clean.count(","))
    paren = rate(clean.count("(") + clean.count(")"))
    colon = rate(clean.count(":"))
    all_caps = rate(sum(1 for w in words if len(w) > 1 and w.upper() == w))

    contractions = rate(sum(1 for w in words if "'" in w))
    i_rate = rate(words.count("i") + words.count("i'm") + words.count("i've"))
    we_rate = rate(words.count("we") + words.count("we're") + words.count("we've"))

    return np.array([
        avg_sent_len, std_sent_len, avg_word_len,
        long_word_rate, short_word_rate, type_token, fragment_rate,
        em_dash, semicolon, exclaim, question, ellipsis, comma, paren, colon, all_caps,
        contractions, i_rate, we_rate,
        np.log1p(n_words),  # length feature
    ], dtype=np.float32)


# ---------- Function-word vector ----------

def function_word_vector(text: str) -> np.ndarray:
    clean = _strip_email_artifacts(text)
    words = _words(clean)
    n = max(len(words), 1)
    c = Counter(words)
    return np.array([c.get(fw, 0) / n for fw in FUNCTION_WORDS], dtype=np.float32)


# ---------- Data loading ----------

def _load_emails(path: Path) -> List[str]:
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("messages") or data.get("emails") or []
    return [(d.get("body") or d.get("text") or "").strip() for d in data if (d.get("body") or d.get("text"))]


# ---------- Synthetic background generator ----------

SYNTH_PERSONAS = [
    "a finance manager at a mid-size manufacturing company",
    "a software engineer at a startup",
    "a sales rep at a pharmaceutical company",
    "a paralegal at a law firm",
    "a marketing director at a SaaS company",
    "a school principal",
    "a real estate broker",
    "a hospital administrator",
    "a nonprofit executive director",
    "a freelance graphic designer",
    "a veterinarian who owns a clinic",
    "an insurance claims adjuster",
    "a construction project manager",
    "a high school teacher",
    "an accountant at a CPA firm",
]

SYNTH_TOPICS = [
    "Following up on our meeting from last week",
    "Quick update on the project timeline",
    "Need your input on the attached draft",
    "Confirming Thursday's appointment",
    "Question about the new vendor contract",
    "Thanks for your help with the issue yesterday",
    "Scheduling next quarter's review",
    "Introduction to a new team member",
    "Reminder about the upcoming training session",
    "Request for feedback on the proposal",
    "Update on the compliance audit",
    "Reschedule request for tomorrow's call",
    "Heads up on the budget approval",
    "Asking for a reference",
    "Questions about the new policy",
]


def _openrouter_call(system_prompt: str, user_prompt: str,
                     temperature: float = 0.9,
                     model_override: str = None) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    default_model = os.environ.get("OPENROUTER_MODEL_ID")
    model_id = model_override or default_model
    if not api_key or not model_id:
        raise RuntimeError("Set OPENROUTER_API_KEY and OPENROUTER_MODEL_ID env vars.")
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 600,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


def build_synthetic_background() -> None:
    """Generate ~2000 emails from varied personas + varied LLMs as 'not-Steve' background.

    Wave 1 change: was 200 emails from a single LLM. The single-LLM version
    taught the classifier "Claude voice vs Steve" rather than "any human vs Steve."
    Fixed by rotating across 5 providers at varied temperatures.

    Estimated OpenRouter cost: $3-5 for 2000 emails. Runs ~15-30 minutes.
    """
    out = []
    target = SYNTH_BACKGROUND_COUNT
    print(f"Generating {target} background emails across {len(BACKGROUND_MODELS)} models.")
    print(f"Rotation: {', '.join(BACKGROUND_MODELS)}")
    print()

    # Resume support: if background file exists, start from where we left off
    if BACKGROUND_PATH.exists():
        with open(BACKGROUND_PATH) as f:
            out = json.load(f)
        print(f"Resuming from existing file: {len(out)} already generated.")

    i = len(out)
    errors = 0
    while len(out) < target:
        persona = SYNTH_PERSONAS[i % len(SYNTH_PERSONAS)]
        topic = SYNTH_TOPICS[(i * 7) % len(SYNTH_TOPICS)]
        model = BACKGROUND_MODELS[i % len(BACKGROUND_MODELS)]
        # Temperature varies between 0.5 and 1.2, spread across the generation run.
        temperature = 0.5 + 0.7 * ((i * 13) % 100) / 100.0

        sys_p = (
            f"You are writing a short, natural business email as {persona}. "
            "Write in that person's authentic voice. Vary your style — some emails "
            "should be terse, some more formal, some casual. Do not sound like every "
            "other email you've written. Vary length from 30 to 250 words depending "
            "on what the topic calls for."
        )
        user_p = f"Write a new email about: {topic}\n\nReturn only the email body, no subject line."

        try:
            text = _openrouter_call(sys_p, user_p,
                                    temperature=temperature,
                                    model_override=model)
        except Exception as e:
            errors += 1
            print(f"  [{i}] ERROR ({model}): {e}")
            if errors > 50:
                print("Too many errors. Stopping.")
                break
            i += 1
            continue

        out.append({
            "body": text,
            "persona": persona,
            "topic": topic,
            "model": model,
            "temperature": round(temperature, 2),
        })

        # Progress + checkpoint save every 50 emails (so we don't lose progress)
        if len(out) % 50 == 0:
            with open(BACKGROUND_PATH, "w") as f:
                json.dump(out, f, indent=2)
            model_short = model.split("/")[-1][:25]
            print(f"  [{len(out)}/{target}] {model_short:25s} t={temperature:.2f}  "
                  f"{persona[:35]}  ({len(text.split())}w)")
        i += 1

    with open(BACKGROUND_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved {len(out)} synthetic background emails to {BACKGROUND_PATH}")
    # Summary stats
    from collections import Counter
    model_counts = Counter(e["model"] for e in out)
    print("\nDistribution by model:")
    for m, n in model_counts.most_common():
        print(f"  {m}: {n}")


# ---------- Training ----------

def _featurize_batch(texts: List[str]):
    """Return dense features (handcrafted + function words) and char-ngram text list."""
    dense = np.vstack([
        np.concatenate([handcrafted_features(t), function_word_vector(t)])
        for t in texts
    ])
    return dense


def train() -> None:
    try:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import StandardScaler
        from xgboost import XGBClassifier
    except ImportError as e:
        sys.exit(f"Missing dependency: {e}\nRun: pip install scikit-learn numpy xgboost")

    train_path = SPLITS_DIR / "train_real.json"
    if not train_path.exists():
        sys.exit("Run: python chimera_eval.py --stage split")
    if not BACKGROUND_PATH.exists():
        sys.exit("Missing background_emails.json. Run --build-background or provide your own.")

    pos_texts = _load_emails(train_path)
    neg_texts = _load_emails(BACKGROUND_PATH)
    print(f"Positive (Steve): {len(pos_texts)}  |  Negative (not-Steve): {len(neg_texts)}")

    texts = pos_texts + neg_texts
    # IMPORTANT: pre-clean before the vectorizer sees anything. This prevents
    # the classifier from learning "email address X = user Y" as a shortcut —
    # a trap that lets attackers pass just by copying a signature.
    cleaned_texts = [_strip_email_artifacts(t) for t in texts]
    y = np.array([1] * len(pos_texts) + [0] * len(neg_texts))

    # Char n-grams — fit on cleaned text so signatures/PII aren't features
    print("Fitting char n-gram vectorizer (3-5)...")
    char_vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=3,
        max_features=2000,
        sublinear_tf=True,
    )
    X_char = char_vec.fit_transform(cleaned_texts)

    # Dense features
    print("Extracting handcrafted + function-word features...")
    X_dense = _featurize_batch(texts)
    dense_scaler = StandardScaler()
    X_dense_scaled = dense_scaler.fit_transform(X_dense)

    # Concatenate sparse + dense features
    from scipy.sparse import hstack, csr_matrix
    X = hstack([X_char, csr_matrix(X_dense_scaled)]).tocsr()

    print(f"Feature matrix shape: {X.shape}")

    # Wave 1: XGBoost (shallow trees) wrapped in Platt calibration.
    # - Captures non-linear feature interactions that LogisticRegression missed.
    # - Platt scaling (sigmoid) produces well-calibrated probabilities so
    #   that the anomaly score maps meaningfully to a threshold decision.
    # - NO class_weight='balanced' — that was distorting calibration.
    #   Balance now comes from the expanded 2000-email background corpus.
    print("Training XGBoost base classifier...")
    base_clf = XGBClassifier(
        max_depth=3,
        n_estimators=200,
        learning_rate=0.1,
        objective="binary:logistic",
        tree_method="hist",
        n_jobs=-1,
        random_state=RANDOM_SEED,
        verbosity=0,
    )
    print("Wrapping in Platt calibration (CalibratedClassifierCV, cv=3)...")
    clf = CalibratedClassifierCV(estimator=base_clf, cv=3, method="sigmoid")
    clf.fit(X, y)
    print(f"Train accuracy: {clf.score(X, y):.3f}")

    # Save
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({
            "clf": clf,
            "char_vec": char_vec,
            "dense_scaler": dense_scaler,
            "wave": 1,
            "n_positive": len(pos_texts),
            "n_negative": len(neg_texts),
        }, f)
    print(f"Saved model to {MODEL_PATH}")


# ---------- Scoring API (used by chimera_eval.py) ----------

_SCORER_CACHE = None


def load_scorer():
    global _SCORER_CACHE
    if _SCORER_CACHE is None:
        if not MODEL_PATH.exists():
            sys.exit("No trained model. Run: python chimera_scorer.py --train")
        with open(MODEL_PATH, "rb") as f:
            _SCORER_CACHE = pickle.load(f)
    return _SCORER_CACHE


def score(scorer, email_body: str) -> float:
    """Returns anomaly score: HIGHER = more likely NOT Steve = more likely fake.

    We return 1 - P(Steve) so higher = more anomalous, matching the eval harness convention.

    Wave 1: length gate added. Emails under LENGTH_GATE_WORDS (50) words
    have their score capped at LENGTH_GATE_SCORE_CAP (0.3). Short emails
    don't have enough stylometric signal to confidently block; their risk
    gets handled by the DLP content-weighting layer (Wave 3).
    """
    from scipy.sparse import hstack, csr_matrix
    # MUST clean the same way we cleaned during training. Signature, phone,
    # email, URL, and legal footer all get stripped before the model sees it.
    cleaned = _strip_email_artifacts(email_body)
    word_count = len(cleaned.split())

    X_char = scorer["char_vec"].transform([cleaned])
    X_dense = _featurize_batch([email_body])  # handcrafted features clean internally
    X_dense_scaled = scorer["dense_scaler"].transform(X_dense)
    X = hstack([X_char, csr_matrix(X_dense_scaled)]).tocsr()
    p_steve = float(scorer["clf"].predict_proba(X)[0, 1])
    raw_anomaly = 1.0 - p_steve

    # Length gate: cap short-email anomaly scores. Stylometry on <50 words
    # is too noisy to be actionable on its own.
    if word_count < LENGTH_GATE_WORDS:
        return min(raw_anomaly, LENGTH_GATE_SCORE_CAP)
    return raw_anomaly


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-background", action="store_true",
                    help="Generate ~2000 multi-LLM synthetic background emails (~$3-5, ~15-30 min)")
    ap.add_argument("--train", action="store_true", help="Train the discriminative scorer")
    args = ap.parse_args()

    if args.build_background:
        build_synthetic_background()
    elif args.train:
        train()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
