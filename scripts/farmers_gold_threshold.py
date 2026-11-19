"""
farmers_gold_threshold.py - how much of a tweet classifier's gold-sample error is just the threshold?

The decision threshold is tuned on silver_val, where ambiguous tweets were removed and about 25% of tweets are positive. The real
tweet population has about 6-7% violent tweets and many rhetoric-only tweets, so that threshold can flag far too many tweets on the gold sample.
This script re-tunes the threshold on part of the gold sample and scores it on the held-out part (repeated stratified k-fold), so the
result is an honest estimate of "calibrate on a small labelled sample, then deploy". PR-AUC does not depend on the threshold.

Usage:  python scripts/farmers_gold_threshold.py            (uses the latest run that holds each model's gold predictions)
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RESULTS_DIR, log_environment, setup_logger, write_json  # noqa: E402
from farmers_common import best_f1_threshold, weighted_prf  # noqa: E402

MODELS = ("tfidf", "transformer")


def cv_threshold(y, p, w, folds, repeats, seed):
    y, p, w = np.asarray(y), np.asarray(p, float), np.asarray(w, float)
    reps = []
    for r in range(repeats):
        pred = np.zeros(len(y), int)
        thrs = []
        for tr, te in StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed + r).split(p.reshape(-1, 1), y):
            t = best_f1_threshold(y[tr], p[tr], w[tr])
            thrs.append(t)
            pred[te] = (p[te] >= t).astype(int)
        m = weighted_prf(y, pred, w)
        m["mean_threshold"] = round(float(np.mean(thrs)), 4)
        reps.append(m)
    keys = ("precision", "recall", "f1", "mean_threshold")
    return {k: round(float(np.mean([r[k] for r in reps])), 4) for k in keys} | {"f1_sd_over_repeats": round(float(np.std([r["f1"] for r in reps])), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--log-dir", default=None)
    args = ap.parse_args()
    logger, log_path, run_id = setup_logger("farmers_gold_threshold", log_dir=args.log_dir)
    log_environment(logger, args)

    out = {"folds": args.folds, "repeats": args.repeats, "models": {}}
    for name in MODELS:
        files = sorted(glob.glob(str(RESULTS_DIR / "farmers_tweets_*" / f"gold_predictions_{name}.csv")))
        if not files:
            logger.info("no gold predictions for %s", name)
            continue
        f = Path(files[-1])
        g = pd.read_csv(f)
        m = cv_threshold(g["label"], g["p"], g["weight"], args.folds, args.repeats, args.seed)
        m["run"] = f.parent.name
        m["n_gold"] = int(len(g))
        out["models"][name] = m
        logger.info("%s (%s): gold-tuned threshold, held-out weighted P %.3f R %.3f F1 %.3f (sd %.3f)", name, f.parent.name,
                    m["precision"], m["recall"], m["f1"], m["f1_sd_over_repeats"])
    d = RESULTS_DIR / "farmers_gold_threshold"
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "metrics.json", out)
    logger.info("Wrote %s", d)


if __name__ == "__main__":
    main()
