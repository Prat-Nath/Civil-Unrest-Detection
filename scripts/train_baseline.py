"""Step 2a - TF-IDF + logistic regression baseline (the score the transformer must beat).

Usage:
  python scripts/train_baseline.py                      # binary: violent vs not
  python scripts/train_baseline.py --task 3class        # peaceful / tension / violent
  python scripts/train_baseline.py --mask-cues          # hide label-revealing keywords (leakage experiment)

Outputs:
  models/<run_name>/baseline.joblib + run_config.json
  results/<run_name>/metrics.json, predictions_test.csv
  logs/train_baseline/train_baseline_<run_name>.log
"""
import argparse
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from common import (MODELS_DIR, PROCESSED_DIR, RESULTS_DIR, log_environment, mask_cues, new_run_id, set_seed,
                    setup_logger, timed_step, write_json)
from metrics_utils import LABEL_COLUMN, best_f1_threshold, class_names, compute_metrics, format_metrics, predict_labels


def parse_args():
    p = argparse.ArgumentParser(description="TF-IDF + logistic regression baseline.")
    p.add_argument("--task", choices=["binary", "3class"], default="binary")
    p.add_argument("--data-dir", type=Path, default=PROCESSED_DIR)
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--mask-cues", action="store_true", help="replace label-revealing keywords with MASKED")
    p.add_argument("--max-features", type=int, default=200000)
    p.add_argument("--ngram-max", type=int, default=2)
    p.add_argument("--C", type=float, default=4.0, help="inverse regularisation strength")
    p.add_argument("--max-train-samples", type=int, default=None, help="subsample train (quick tests)")
    p.add_argument("--max-eval-samples", type=int, default=None, help="subsample val/test (quick tests)")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def load_split(path: Path, args, label_col, limit=None):
    d = pd.read_csv(path, encoding="utf-8")
    if limit and len(d) > limit:
        d = d.sample(limit, random_state=args.seed).sort_values("event_date")
    texts = d["text"].fillna("").astype(str)
    if args.mask_cues:
        texts = texts.map(lambda t: mask_cues(t, "MASKED"))
    return d, texts.tolist(), d[label_col].to_numpy()


def main():
    args = parse_args()
    run_id = new_run_id()
    run_name = f"tfidf_lr_{args.task}{'_masked' if args.mask_cues else ''}_{run_id}"
    log, log_path, _ = setup_logger("train_baseline", run_id=run_id, log_dir=args.log_dir, name=f"train_baseline_{run_name}")
    log.info("=== TRAIN BASELINE run_name=%s ===", run_name)
    log.info("Log file: %s", log_path)
    log_environment(log, args)
    set_seed(args.seed)
    label_col = LABEL_COLUMN[args.task]

    try:
        with timed_step(log, "load data"):
            tr, x_tr, y_tr = load_split(args.data_dir / "train.csv", args, label_col, args.max_train_samples)
            va, x_va, y_va = load_split(args.data_dir / "val.csv", args, label_col, args.max_eval_samples)
            te, x_te, y_te = load_split(args.data_dir / "test.csv", args, label_col, args.max_eval_samples)
            log.info("Rows: train=%d val=%d test=%d | task=%s label_col=%s mask_cues=%s",
                     len(tr), len(va), len(te), args.task, label_col, args.mask_cues)
            for n, y in (("train", y_tr), ("val", y_va), ("test", y_te)):
                log.info("Label counts %-5s: %s", n, {int(k): int(v) for k, v in zip(*np.unique(y, return_counts=True))})
            if args.mask_cues:
                log.info("Example masked text: %s", x_tr[0][:200])

        with timed_step(log, "fit TF-IDF"):
            vec = TfidfVectorizer(ngram_range=(1, args.ngram_max), min_df=3, max_features=args.max_features,
                                  sublinear_tf=True, lowercase=True, strip_accents="unicode")
            X_tr = vec.fit_transform(x_tr)
            X_va, X_te = vec.transform(x_va), vec.transform(x_te)
            log.info("TF-IDF matrix: train=%s vocab=%d", X_tr.shape, len(vec.vocabulary_))

        with timed_step(log, "fit logistic regression"):
            clf = LogisticRegression(C=args.C, class_weight="balanced", max_iter=1000, n_jobs=None)
            clf.fit(X_tr, y_tr)

        with timed_step(log, "evaluate"):
            p_va, p_te = clf.predict_proba(X_va), clf.predict_proba(X_te)
            thr = best_f1_threshold(y_va, p_va[:, 1]) if args.task == "binary" else None
            metrics = {"val": compute_metrics(y_va, p_va, args.task),
                       "test": compute_metrics(y_te, p_te, args.task)}
            log.info("\n%s", format_metrics(metrics["val"], "VALIDATION (default decision rule)"))
            log.info("\n%s", format_metrics(metrics["test"], "TEST (default decision rule)"))
            if thr is not None:
                metrics["val_tuned_threshold"] = thr
                metrics["test_at_val_threshold"] = compute_metrics(y_te, p_te, args.task, threshold=thr)
                log.info("Threshold tuned on validation: %.3f", thr)
                log.info("\n%s", format_metrics(metrics["test_at_val_threshold"], "TEST at validation-tuned threshold"))

            # most informative words (binary: positive = violent)
            names = np.array(vec.get_feature_names_out())
            coef = clf.coef_[0] if args.task == "binary" else clf.coef_[2]
            log.info("Top words for 'violent': %s", list(names[np.argsort(coef)[-25:][::-1]]))
            log.info("Top words against 'violent': %s", list(names[np.argsort(coef)[:25]]))

        with timed_step(log, "save model + results"):
            model_dir = args.models_dir / run_name
            res_dir = args.results_dir / run_name
            model_dir.mkdir(parents=True, exist_ok=True)
            joblib.dump({"vectorizer": vec, "model": clf}, model_dir / "baseline.joblib")
            write_json(model_dir / "run_config.json", {
                "model_type": "baseline", "run_name": run_name, "task": args.task, "mask_cues": args.mask_cues,
                "label_names": class_names(args.task), "threshold": thr, "args": {k: str(v) for k, v in vars(args).items()}})
            write_json(res_dir / "metrics.json", metrics)
            pred = te[["event_id_cnty", "event_date", "admin1"]].copy()
            pred["y_true"] = y_te
            pred["y_pred"] = predict_labels(p_te, args.task, None)
            for i, n in enumerate(class_names(args.task)):
                pred[f"p_{n}"] = p_te[:, i]
            pred.to_csv(res_dir / "predictions_test.csv", index=False)
            log.info("Model   -> %s", model_dir)
            log.info("Results -> %s", res_dir)
        log.info("=== TRAIN BASELINE finished successfully ===")
        return 0
    except Exception as exc:
        log.error("TRAIN BASELINE aborted: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
