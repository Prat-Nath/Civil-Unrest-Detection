"""Shared helpers for the social-media (tweet signal) experiments.

The tweet files carry no text, only counts and a civil-unrest classifier score per country and day, so
this part of the project is a *signal forecasting* problem, not a text classification problem.
Labels ("surge" days) are derived from the dataset's own classifier flags. They are NOT human ground truth.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA_DIR

SOCIAL_PROCESSED = DATA_DIR / "social_processed"
HORIZONS = (1, 3, 7)

# Per-day input features (used by both the tabular and the sequence models)
DAY_FEATURES = ["log_tweets", "log_related", "share", "high_share", "mean_score", "max_score",
                "z_today", "missing", "dow_sin", "dow_cos"]


def read_country_day(path):
    """Read country_day.csv. keep_default_na=False matters: 'NA' is Namibia, not a missing value."""
    return pd.read_csv(path, parse_dates=["date"], keep_default_na=False, na_values=[""])


def load_panel(path=None):
    path = Path(path) if path else SOCIAL_PROCESSED / "panel.csv.gz"
    return pd.read_csv(path, parse_dates=["date"], keep_default_na=False, na_values=[""])


def load_panel_info(path=None):
    path = Path(path) if path else SOCIAL_PROCESSED / "panel_info.json"
    return json.loads(path.read_text(encoding="utf-8"))


def best_f1_threshold(y_true, scores):
    """Threshold on `scores` that maximises F1 of the positive class."""
    from sklearn.metrics import precision_recall_curve
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = 2 * precision[:-1] * recall[:-1] / np.clip(precision[:-1] + recall[:-1], 1e-12, None)
    if len(f1) == 0:
        return 0.5
    return float(thresholds[int(np.nanargmax(f1))])


def binary_metrics(y_true, scores, threshold):
    """Accuracy, precision, recall, F1 for the positive (surge) class, plus threshold-free PR-AUC / ROC-AUC."""
    from sklearn.metrics import (accuracy_score, average_precision_score, confusion_matrix,
                                 precision_recall_fscore_support, roc_auc_score)
    y_true = np.asarray(y_true).astype(int)
    scores = np.asarray(scores, dtype=float)
    pred = (scores >= threshold).astype(int)
    p, r, f1, _ = precision_recall_fscore_support(y_true, pred, average="binary", zero_division=0)
    both = len(np.unique(y_true)) == 2
    prevalence = float(y_true.mean())
    pr_auc = float(average_precision_score(y_true, scores)) if both else float("nan")
    return {
        "n": int(len(y_true)),
        "positives": int(y_true.sum()),
        "prevalence": prevalence,
        "threshold": float(threshold),
        "accuracy": float(accuracy_score(y_true, pred)),
        "precision": float(p),
        "recall": float(r),
        "f1": float(f1),
        "pr_auc": pr_auc,
        "pr_auc_lift_over_prevalence": pr_auc / prevalence if both and prevalence > 0 else float("nan"),
        "roc_auc": float(roc_auc_score(y_true, scores)) if both else float("nan"),
        "confusion_matrix": confusion_matrix(y_true, pred, labels=[0, 1]).tolist(),
    }


def format_metrics(name, m):
    return ("%s | n=%d pos=%d (%.1f%%) | P=%.3f R=%.3f F1=%.3f | PR-AUC=%.3f (x%.1f over base) ROC-AUC=%.3f | thr=%.3f | CM=%s"
            % (name, m["n"], m["positives"], 100 * m["prevalence"], m["precision"], m["recall"], m["f1"],
               m["pr_auc"], m["pr_auc_lift_over_prevalence"], m["roc_auc"], m["threshold"], m["confusion_matrix"]))
