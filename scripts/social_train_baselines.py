"""Train and evaluate tabular models that predict an unrest surge h days ahead (h = 1, 3, 7).

Models per horizon:
  prevalence      constant score (the PR-AUC of a model with no skill equals the positive rate)
  recent_rule     no training: score = how far today's unrest share is above its 28-day level
  logistic        standardised features, class-balanced logistic regression
  grad_boost      HistGradientBoostingClassifier with class-balanced sample weights

The decision threshold is tuned for F1 on the validation split and then applied to the test split.
Rows used: "eligible" days only (the day itself is not already a surge), split chronologically.

Output: results/social_baselines_<runid>/{metrics.json, predictions_test.csv, feature_importance_<h>.csv}
Log   : logs/social_train_baselines/social_train_baselines_<runid>.log

Usage:
    python scripts/social_train_baselines.py
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

from common import RESULTS_DIR, log_environment, set_seed, setup_logger, timed_step, write_json
from social_common import (DAY_FEATURES, HORIZONS, SOCIAL_PROCESSED, best_f1_threshold, binary_metrics,
                           format_metrics, load_panel)

TABULAR_FEATURES = DAY_FEATURES + [
    "log_related_lag1", "log_related_lag2", "log_related_lag3", "log_related_lag7",
    "log_related_mean7", "log_related_mean14", "log_related_mean28",
    "log_related_std7", "log_related_std14", "log_related_std28",
    "share_mean7", "share_mean14", "share_mean28", "log_tweets_mean28", "share_ratio_7_28",
    "surge_count_7", "days_since_surge",
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--panel", type=Path, default=SOCIAL_PROCESSED / "panel.csv.gz")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--horizons", type=int, nargs="+", default=list(HORIZONS))
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def make_models(seed):
    return {
        "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000)),
        "grad_boost": HistGradientBoostingClassifier(learning_rate=0.05, max_depth=4, max_iter=250,
                                                      l2_regularization=1.0, early_stopping=False, random_state=seed),
    }


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("social_train_baselines", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== SOCIAL TRAIN BASELINES started (run %s) | log file: %s ===", run_id, log_path)
    set_seed(args.seed)

    out_dir = args.results_dir / f"social_baselines_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel = load_panel(args.panel)
    panel = panel[panel["eligible"] == 1].copy()
    logger.info("eligible rows: %s | splits: %s", f"{len(panel):,}", panel["split"].value_counts().to_dict())

    all_metrics, pred_frames = {}, []
    for h in args.horizons:
        target = f"target_h{h}"
        data = panel.dropna(subset=[target] + TABULAR_FEATURES).copy()
        data[target] = data[target].astype(int)
        parts = {s: data[data["split"] == s] for s in ("train", "val", "test")}
        logger.info("--- horizon h=%d days ahead | rows train/val/test = %d/%d/%d | positive rate %.3f/%.3f/%.3f",
                    h, len(parts["train"]), len(parts["val"]), len(parts["test"]),
                    *[parts[s][target].mean() for s in ("train", "val", "test")])
        X = {s: parts[s][TABULAR_FEATURES].to_numpy(dtype=float) for s in parts}
        y = {s: parts[s][target].to_numpy() for s in parts}
        scores = {}

        scores["prevalence"] = {s: np.full(len(y[s]), y["train"].mean()) for s in parts}
        rule_col = TABULAR_FEATURES.index("share_ratio_7_28")
        scores["recent_rule"] = {s: X[s][:, rule_col] for s in parts}

        for name, model in make_models(args.seed).items():
            with timed_step(logger, f"fit {name} (h={h})"):
                if name == "grad_boost":
                    model.fit(X["train"], y["train"], sample_weight=compute_sample_weight("balanced", y["train"]))
                else:
                    model.fit(X["train"], y["train"])
            scores[name] = {s: model.predict_proba(X[s])[:, 1] for s in parts}
            if name == "grad_boost":
                with timed_step(logger, f"permutation importance on validation (h={h})"):
                    imp = permutation_importance(model, X["val"], y["val"], scoring="average_precision",
                                                 n_repeats=5, random_state=args.seed)
                    pd.DataFrame({"feature": TABULAR_FEATURES, "importance_mean": imp.importances_mean,
                                  "importance_std": imp.importances_std}
                                 ).sort_values("importance_mean", ascending=False
                                               ).to_csv(out_dir / f"feature_importance_h{h}.csv", index=False)

        for name, sc in scores.items():
            thr = best_f1_threshold(y["val"], sc["val"]) if name != "prevalence" else 0.5
            res = {"val": binary_metrics(y["val"], sc["val"], thr), "test": binary_metrics(y["test"], sc["test"], thr)}
            all_metrics[f"{name}_h{h}"] = res
            logger.info(format_metrics(f"TEST {name:<11} h={h}", res["test"]))
            if name != "prevalence":
                pred_frames.append(pd.DataFrame({
                    "model": name, "horizon": h,
                    "country": parts["test"]["country"].to_numpy(), "date": parts["test"]["date"].to_numpy(),
                    "y_true": y["test"], "score": sc["test"], "threshold": thr}))

    pd.concat(pred_frames).to_csv(out_dir / "predictions_test.csv", index=False)
    write_json(out_dir / "metrics.json", {"run_id": run_id, "args": {k: str(v) for k, v in vars(args).items()},
                                          "features": TABULAR_FEATURES, "metrics": all_metrics})
    write_json(log_path.with_name(log_path.stem + "_summary.json"),
               {"run_id": run_id, "results_dir": str(out_dir),
                "test_f1": {k: v["test"]["f1"] for k, v in all_metrics.items()},
                "test_pr_auc": {k: v["test"]["pr_auc"] for k, v in all_metrics.items()}})
    logger.info("Wrote %s", out_dir)
    logger.info("=== SOCIAL TRAIN BASELINES finished successfully ===")


if __name__ == "__main__":
    main()
