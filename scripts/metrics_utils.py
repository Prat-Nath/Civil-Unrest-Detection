"""Metrics shared by the baseline, transformer and evaluation scripts."""
import numpy as np
from sklearn.metrics import (accuracy_score, average_precision_score, confusion_matrix, f1_score,
                             precision_recall_curve, precision_recall_fscore_support, roc_auc_score)

LABEL_COLUMN = {"binary": "is_violent", "3class": "crowd_label"}


def class_names(task: str):
    return ["non_violent", "violent"] if task == "binary" else ["peaceful", "tension", "violent"]


def best_f1_threshold(y_true, p_pos):
    """Threshold on P(violent) that maximises F1 of the violent class (tune on VALIDATION data only)."""
    prec, rec, thr = precision_recall_curve(y_true, p_pos)
    f1 = 2 * prec * rec / (prec + rec + 1e-12)
    if len(thr) == 0:
        return 0.5
    return float(thr[int(np.argmax(f1[:-1]))])


def predict_labels(probs, task: str, threshold=None):
    probs = np.asarray(probs)
    if task == "binary":
        return (probs[:, 1] >= (0.5 if threshold is None else threshold)).astype(int)
    return probs.argmax(1)


def compute_metrics(y_true, probs, task: str, threshold=None) -> dict:
    y_true = np.asarray(y_true)
    probs = np.asarray(probs)
    names = class_names(task)
    labels = list(range(len(names)))
    y_pred = predict_labels(probs, task, threshold)

    p, r, f, s = precision_recall_fscore_support(y_true, y_pred, labels=labels, zero_division=0)
    m = {
        "n": int(len(y_true)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)),
        "weighted_f1": float(f1_score(y_true, y_pred, labels=labels, average="weighted", zero_division=0)),
        "per_class": {n: {"precision": float(p[i]), "recall": float(r[i]), "f1": float(f[i]), "support": int(s[i])}
                      for i, n in enumerate(names)},
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
        "labels": names,
    }
    pos = 1 if task == "binary" else 2          # the "violent" class index
    y_pos = (y_true == pos).astype(int)
    if y_pos.sum() > 0 and y_pos.sum() < len(y_pos):
        m["pr_auc_violent"] = float(average_precision_score(y_pos, probs[:, pos]))
        m["roc_auc_violent"] = float(roc_auc_score(y_pos, probs[:, pos]))
    else:
        m["pr_auc_violent"] = m["roc_auc_violent"] = None
    m["f1_violent"] = float(f[pos])
    if task == "binary":
        m["threshold"] = 0.5 if threshold is None else float(threshold)
        m["best_f1_threshold_on_this_split"] = best_f1_threshold(y_true, probs[:, 1]) if m["pr_auc_violent"] is not None else None
    # one number used for model selection / early stopping
    m["primary"] = m["f1_violent"] if task == "binary" else m["macro_f1"]
    m["primary_name"] = "f1_violent" if task == "binary" else "macro_f1"
    return m


def format_metrics(m: dict, title: str = "") -> str:
    lines = [f"--- {title} (n={m['n']}) ---"]
    lines.append(f"accuracy={m['accuracy']:.4f}  macro_f1={m['macro_f1']:.4f}  weighted_f1={m['weighted_f1']:.4f}")
    if m.get("pr_auc_violent") is not None:
        lines.append(f"PR-AUC(violent)={m['pr_auc_violent']:.4f}  ROC-AUC(violent)={m['roc_auc_violent']:.4f}")
    if "threshold" in m:
        lines.append(f"threshold={m['threshold']:.3f}")
    for name, c in m["per_class"].items():
        lines.append(f"  {name:<12} P={c['precision']:.3f}  R={c['recall']:.3f}  F1={c['f1']:.3f}  support={c['support']}")
    lines.append(f"confusion matrix (rows=true, cols=pred; order={m['labels']}): {m['confusion_matrix']}")
    return "\n".join(lines)
