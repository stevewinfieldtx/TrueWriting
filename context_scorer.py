"""
Wave 3 — Context Anomaly Scorer

Trains an IsolationForest on real emails only (no fakes needed) and emits an
anomaly score in [0, 1] for any email. Higher = more anomalous relative to
Steve's patterns.

Why IsolationForest: we don't have a reliable labeled pool of "bad context"
emails. The correct framing is one-class anomaly detection — fit the manifold
of Steve's real sends, then anything far from it is suspicious. This avoids
having to generate synthetic attack examples for training (which could teach
the model the wrong thing).

Model dict:
    {
        'iforest': IsolationForest,
        'scaler': StandardScaler,
        'feature_names': list[str],
        'decision_p01': float,  # 1st percentile of real decision scores (calibration)
        'decision_p99': float,  # 99th percentile
        'wave': 3,
    }
"""

import json
import pickle
from typing import Dict, List, Any

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from context_features import extract_features_batch, FEATURE_NAMES
from recipient_profiler import load_profiles

MODEL_PATH = 'context_model.pkl'
_CACHE: Dict[str, Any] = {}


def train(train_emails_path: str = 'eval_splits/train_real.json',
          profiles_path: str = 'recipient_profiles.json',
          out_path: str = MODEL_PATH,
          n_estimators: int = 200,
          contamination: float = 'auto',
          random_state: int = 42) -> Dict[str, Any]:
    print(f'[ctx-scorer] loading train emails from {train_emails_path}...')
    with open(train_emails_path, 'r', encoding='utf-8') as f:
        emails = json.load(f)
    print(f'[ctx-scorer] {len(emails)} real emails')

    profiles = load_profiles(profiles_path)
    print(f'[ctx-scorer] profiles loaded: {profiles["_meta"]}')

    print(f'[ctx-scorer] extracting features...')
    X = np.array(extract_features_batch(emails, profiles), dtype=float)
    print(f'[ctx-scorer] X shape: {X.shape}')

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    print(f'[ctx-scorer] fitting IsolationForest (n={n_estimators}, contam={contamination})...')
    iforest = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=random_state,
        n_jobs=-1,
    )
    iforest.fit(Xs)

    # calibration range from training score distribution
    d_train = iforest.decision_function(Xs)
    p01 = float(np.percentile(d_train, 1))
    p99 = float(np.percentile(d_train, 99))

    model = {
        'iforest': iforest,
        'scaler': scaler,
        'feature_names': list(FEATURE_NAMES),
        'decision_p01': p01,
        'decision_p99': p99,
        'wave': 3,
    }

    with open(out_path, 'wb') as f:
        pickle.dump(model, f)
    print(f'[ctx-scorer] saved to {out_path}')
    print(f'[ctx-scorer] decision score range (train): p01={p01:.4f} p99={p99:.4f}')
    return model


def load_model(path: str = MODEL_PATH) -> Dict[str, Any]:
    if 'model' not in _CACHE:
        with open(path, 'rb') as f:
            _CACHE['model'] = pickle.load(f)
    return _CACHE['model']


def score_batch(emails: List[Dict[str, Any]],
                profiles: Dict[str, Any],
                model: Dict[str, Any] = None) -> List[float]:
    """
    Returns anomaly scores in [0, 1]. Higher = more anomalous.
    Calibrated so real training emails sit near 0.
    """
    if model is None:
        model = load_model()
    X = np.array(extract_features_batch(emails, profiles), dtype=float)
    Xs = model['scaler'].transform(X)
    raw = model['iforest'].decision_function(Xs)  # higher = more normal
    # map raw to [0,1] anomaly score using training quantiles
    p01 = model['decision_p01']
    p99 = model['decision_p99']
    rng = max(p99 - p01, 1e-6)
    # invert so higher = more anomalous
    anom = (p99 - raw) / rng
    anom = np.clip(anom, 0.0, 1.0)
    return anom.tolist()


def score_one(email: Dict[str, Any],
              profiles: Dict[str, Any],
              model: Dict[str, Any] = None) -> float:
    return score_batch([email], profiles, model)[0]


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'train':
        train()
    else:
        print('USAGE: python context_scorer.py train')
        print('       (or import and call score_batch(emails, profiles))')
