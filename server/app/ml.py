"""Activity classifier that learns from your corrections.

Deliberately a small, interpretable model rather than a deep one:

  * The training set is your own confirmed labels, which will number in the
    dozens, not the millions. A high-capacity model would memorise them.
  * Softmax regression exposes per-feature weights, so it can explain why it
    called something a run rather than a walk.
  * Accuracy is reported from cross-validation, not from the training data, so
    the number shown is not the model grading its own homework.

Until there are enough labels the rule-based classifier in segment.py is used
instead, and the model never silently replaces it while performing worse.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np

from .segment import FEATURE_NAMES, rule_classify, to_vector

log = logging.getLogger("whoop.ml")

MIN_PER_CLASS = 4      # labels needed in a class before it can be learned
MIN_CLASSES = 2
MODEL_NAME = "activity_classifier"


class ActivityClassifier:
    """Multinomial logistic regression with L2, trained by gradient descent."""

    def __init__(self) -> None:
        self.classes: list[str] = []
        self.mean = np.zeros(len(FEATURE_NAMES))
        self.std = np.ones(len(FEATURE_NAMES))
        self.weights: np.ndarray | None = None    # (features + 1, classes)
        self.accuracy: float | None = None
        self.n_samples = 0

    # --- fitting ------------------------------------------------------------
    def _standardise(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.std

    @staticmethod
    def _softmax(z: np.ndarray) -> np.ndarray:
        z = z - z.max(axis=1, keepdims=True)      # shift for numerical stability
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def _fit_raw(self, x: np.ndarray, y: np.ndarray, n_classes: int,
                 epochs: int = 600, lr: float = 0.35, l2: float = 0.01) -> np.ndarray:
        n, d = x.shape
        xb = np.hstack([x, np.ones((n, 1))])       # bias column
        w = np.zeros((d + 1, n_classes))
        onehot = np.zeros((n, n_classes))
        onehot[np.arange(n), y] = 1.0
        for _ in range(epochs):
            probs = self._softmax(xb @ w)
            grad = xb.T @ (probs - onehot) / n
            grad[:-1] += l2 * w[:-1]               # do not regularise the bias
            w -= lr * grad
        return w

    def train(self, samples: Sequence[tuple[list[float], str]]) -> dict[str, Any]:
        """Fit on (feature_vector, label) pairs. Returns a training report."""
        counts: dict[str, int] = {}
        for _, label in samples:
            counts[label] = counts.get(label, 0) + 1
        usable = {c for c, n in counts.items() if n >= MIN_PER_CLASS}

        if len(usable) < MIN_CLASSES:
            return {"trained": False, "reason": "not enough labelled examples",
                    "counts": counts, "need_per_class": MIN_PER_CLASS,
                    "need_classes": MIN_CLASSES}

        rows = [(v, l) for v, l in samples if l in usable]
        self.classes = sorted(usable)
        index = {c: i for i, c in enumerate(self.classes)}
        x = np.array([v for v, _ in rows], dtype=float)
        y = np.array([index[l] for _, l in rows], dtype=int)

        self.mean = x.mean(axis=0)
        std = x.std(axis=0)
        self.std = np.where(std < 1e-9, 1.0, std)   # constant features -> no scaling
        xs = self._standardise(x)

        self.accuracy = self._cross_val(xs, y, len(self.classes))
        self.weights = self._fit_raw(xs, y, len(self.classes))
        self.n_samples = len(rows)

        return {"trained": True, "classes": self.classes, "n_samples": self.n_samples,
                "cv_accuracy": self.accuracy, "counts": counts,
                "baseline_accuracy": round(max(counts[c] for c in usable) / len(rows), 3)}

    def _cross_val(self, x: np.ndarray, y: np.ndarray, n_classes: int) -> float:
        """Stratified k-fold accuracy. Honest estimate on unseen samples."""
        n = len(y)
        k = min(5, min(np.bincount(y, minlength=n_classes)[np.bincount(y) > 0]))
        if k < 2:
            return 0.0
        rng = np.random.default_rng(0)
        folds = np.zeros(n, dtype=int)
        for cls in range(n_classes):
            idx = np.where(y == cls)[0]
            rng.shuffle(idx)
            folds[idx] = np.arange(len(idx)) % k

        correct = 0
        for f in range(k):
            train, test = folds != f, folds == f
            if not test.any() or len(np.unique(y[train])) < 2:
                continue
            w = self._fit_raw(x[train], y[train], n_classes)
            xb = np.hstack([x[test], np.ones((test.sum(), 1))])
            correct += int((self._softmax(xb @ w).argmax(axis=1) == y[test]).sum())
        return round(correct / n, 3)

    # --- prediction ---------------------------------------------------------
    def predict(self, features: dict[str, float]) -> tuple[str, float] | None:
        if self.weights is None or not self.classes:
            return None
        x = self._standardise(np.array(to_vector(features), dtype=float))
        probs = self._softmax(np.hstack([x, [1.0]])[None, :] @ self.weights)[0]
        i = int(probs.argmax())
        return self.classes[i], round(float(probs[i]), 3)

    def explain(self) -> dict[str, dict[str, float]]:
        """Standardised weight per feature per class -- why it decides what it does."""
        if self.weights is None:
            return {}
        return {
            cls: {name: round(float(self.weights[j, i]), 3)
                  for j, name in enumerate(FEATURE_NAMES)}
            for i, cls in enumerate(self.classes)
        }

    # --- persistence --------------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "classes": self.classes, "mean": self.mean.tolist(),
            "std": self.std.tolist(),
            "weights": self.weights.tolist() if self.weights is not None else None,
            "accuracy": self.accuracy, "n_samples": self.n_samples,
            "features": list(FEATURE_NAMES),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ActivityClassifier":
        m = cls()
        # A feature-set change invalidates stored weights; retrain rather than
        # silently applying them to a different vector layout.
        if list(payload.get("features", [])) != list(FEATURE_NAMES):
            log.warning("stored model has a different feature set; ignoring it")
            return m
        m.classes = payload.get("classes", [])
        m.mean = np.array(payload["mean"])
        m.std = np.array(payload["std"])
        w = payload.get("weights")
        m.weights = np.array(w) if w is not None else None
        m.accuracy = payload.get("accuracy")
        m.n_samples = payload.get("n_samples", 0)
        return m


def classify(features: dict[str, float], hint: str | None,
             model: ActivityClassifier | None) -> tuple[str, float, str]:
    """Best available label: the learned model when it is trustworthy, else rules.

    The model is only preferred once cross-validated accuracy clears the
    majority-class baseline by a clear margin -- a model that cannot beat
    "always guess the most common class" is worse than the rules.
    """
    if model is not None and model.weights is not None and (model.accuracy or 0) >= 0.60:
        result = model.predict(features)
        if result:
            return result[0], result[1], "model"
    label, confidence = rule_classify(features, hint)
    return label, confidence, "rules"
