"""Compute precision / recall / F1 of the violence classifier on hand-labelled tweets.

1. Open results/farmers_transfer_<runid>/label_sample.csv (300 tweets, scores hidden) and fill the `label` column:
       1 = the tweet describes, reports or incites violence or a violent clash (injury, killing, beating, attack, riot,
           police force against protesters), 0 = anything else (opinion, politics, logistics, weather, support, ...)
2. Run:  python scripts/farmers_transfer_eval.py --run results/farmers_transfer_<runid>

Because the sheet over-samples high-scoring tweets (100 top-scored, 100 mid, 100 low), the script reweights each stratum by
its true share of the scored sample, so the reported precision / recall / F1 estimate the whole sampled population.
Writes <run>/hand_label_metrics.json and logs to logs/farmers_transfer_eval/.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from common import log_environment, setup_logger, write_json


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--labels", type=Path, default=None, help="labelled sheet (default: <run>/label_sample.csv)")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_transfer_eval", log_dir=args.log_dir)
    log_environment(logger, args)
    labels_path = args.labels or (args.run / "label_sample.csv")
    sheet = pd.read_csv(labels_path, dtype={"tweet_id": str})
    scores = pd.read_csv(args.run / "scores.csv", dtype={"tweet_id": str})
    sheet = sheet.drop(columns=[c for c in ("p_violent", "stratum", "weight") if c in sheet.columns]).drop_duplicates("tweet_id")
    sheet = sheet.merge(scores[["tweet_id", "p_violent"]], on="tweet_id", how="left")
    sheet = sheet[sheet["label"].isin([0, 1, "0", "1"])].copy()
    sheet["label"] = sheet["label"].astype(int)
    if len(sheet) < 30:
        raise SystemExit(f"only {len(sheet)} labelled rows - fill the `label` column first")
    p = scores["p_violent"]
    # strata as used when the sheet was drawn: top-score pool (2n best), mid (0.05-0.5), low (<=0.05)
    n = len(pd.read_csv(labels_path)) // 3
    top_cut = p.nlargest(n * 2).min()
    def stratum(v):
        return "top" if v >= top_cut else ("mid" if 0.05 < v < 0.5 else ("low" if v <= 0.05 else "other"))
    sheet["stratum"] = sheet["p_violent"].map(stratum)
    pop = scores["p_violent"].map(stratum).value_counts()
    cnt = sheet["stratum"].value_counts()
    sheet["w"] = sheet["stratum"].map(lambda s: pop.get(s, 0) / max(cnt.get(s, 1), 1))
    pred = (sheet["p_violent"] >= args.threshold).astype(int)
    y = sheet["label"]
    tp = float(sheet.loc[(pred == 1) & (y == 1), "w"].sum()); fp = float(sheet.loc[(pred == 1) & (y == 0), "w"].sum())
    fn = float(sheet.loc[(pred == 0) & (y == 1), "w"].sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    out = {"labelled": int(len(sheet)), "positives_labelled": int(y.sum()), "threshold": args.threshold,
           "precision_weighted": round(prec, 3), "recall_weighted": round(rec, 3), "f1_weighted": round(f1, 3),
           "unweighted_precision": round(float(y[pred == 1].mean()), 3) if (pred == 1).any() else None,
           "estimated_violent_share_of_tweets": round(float((tp + fn) / sheet["w"].sum()), 4)}
    logger.info("hand-label evaluation: %s", out)
    out["labels_file"] = str(labels_path.name)
    write_json(args.run / (labels_path.stem + "_metrics.json"), out)


if __name__ == "__main__":
    main()
