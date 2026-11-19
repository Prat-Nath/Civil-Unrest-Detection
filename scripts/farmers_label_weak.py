"""Build weak ("silver") tweet labels for violence-related tweets, and score the rule on the gold sample.

Rule: label 1 if the tweet has at least one STRONG physical-violence term (beating, tear gas, rammed, killed, clash, ...),
      label 0 if it has no STRONG and no WEAK term, otherwise the tweet is ambiguous and left out of the training sets.
Splits are chronological (train / val / test) and exclude every tweet in the gold sample.

Inputs   data/farmers_processed/tweets_text.csv.gz, data/farmers_labels/gold_claude.csv
Outputs  data/farmers_labels/silver_{train,val,test}.csv   (tweet_id, date, day, text, label)
         data/farmers_labels/silver_summary.json
Log      logs/farmers_label_weak/

The gold sample (300 tweets, 51 violent) was labelled by Claude, single annotator, NOT human-verified: check it, fix
the `label` column if you disagree, and re-run the evaluation. The rule words were written while reading that sample, so the
rule's own score on it is optimistic.

Usage:
    python scripts/farmers_label_weak.py
    python scripts/farmers_label_weak.py --per-day 100 --neg-ratio 3
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from common import log_environment, set_seed, setup_logger, timed_step, write_json
from farmers_common import FARMERS_DIR, GOLD_PATH, LABELS_DIR, gold_bootstrap, gold_metrics, load_gold, rule_flags


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tweets", type=Path, default=FARMERS_DIR / "tweets_text.csv.gz")
    p.add_argument("--gold", type=Path, default=GOLD_PATH)
    p.add_argument("--out-dir", type=Path, default=LABELS_DIR)
    p.add_argument("--per-day", type=int, default=150, help="max tweets kept per day and class")
    p.add_argument("--neg-ratio", type=float, default=3.0, help="negatives kept per positive (per split)")
    p.add_argument("--val-start", default="2021-07-01")
    p.add_argument("--test-start", default="2021-10-01")
    p.add_argument("--min-chars", type=int, default=25)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_label_weak", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS LABEL WEAK started (run %s) ===", run_id)
    set_seed(args.seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    with timed_step(logger, "load and flag tweets"):
        tw = pd.read_csv(args.tweets, usecols=["tweet_id", "date", "text"], dtype={"tweet_id": str})
        dt = pd.to_datetime(tw["date"], utc=True, format="ISO8601", errors="coerce")
        tw = tw[dt.notna()].copy()
        tw["day"] = dt[dt.notna()].dt.tz_localize(None).dt.normalize()
        tw["text"] = tw["text"].astype(str)
        tw = tw[tw["text"].str.len() >= args.min_chars].drop_duplicates("tweet_id")
        gold_ids = set(load_gold(args.gold)["tweet_id"])
        tw = tw[~tw["tweet_id"].isin(gold_ids)]
        # near-duplicate removal (copy-paste campaign tweets) so train/test do not share identical texts
        tw["key"] = tw["text"].str.lower().str.replace(r"https?://\S+|@\w+|\s+", " ", regex=True).str.strip()
        tw = tw.drop_duplicates("key")
        flags = rule_flags(tw["text"])
        tw["label"] = np.where(flags["strong"], 1, np.where(~flags["weak"], 0, -1))
        logger.info("tweets after filtering: %d | positives %d | negatives %d | ambiguous %d", len(tw), (tw.label == 1).sum(), (tw.label == 0).sum(), (tw.label == -1).sum())

    with timed_step(logger, "build splits"):
        tw = tw[tw["label"] >= 0]
        tw["split"] = np.where(tw["day"] >= pd.Timestamp(args.test_start), "test", np.where(tw["day"] >= pd.Timestamp(args.val_start), "val", "train"))
        parts = []
        for split, g in tw.groupby("split"):
            pos = g[g.label == 1].sample(frac=1, random_state=args.seed).groupby("day").head(args.per_day)
            neg = g[g.label == 0].sample(frac=1, random_state=args.seed).groupby("day").head(args.per_day)
            neg = neg.sample(min(len(neg), int(len(pos) * args.neg_ratio)), random_state=args.seed)
            parts.append(pd.concat([pos, neg]).sample(frac=1, random_state=args.seed).assign(split=split))
        out = pd.concat(parts)
        summary = {"run_id": run_id, "rule": "label 1 = STRONG violence term; 0 = no STRONG and no WEAK term; ambiguous removed", "splits": {}}
        for split in ("train", "val", "test"):
            d = out[out["split"] == split][["tweet_id", "date", "day", "text", "label"]]
            d.to_csv(args.out_dir / f"silver_{split}.csv", index=False)
            summary["splits"][split] = {"rows": int(len(d)), "positives": int(d["label"].sum()), "positive_rate": round(float(d["label"].mean()), 4),
                                        "day_min": str(d["day"].min())[:10], "day_max": str(d["day"].max())[:10]}
            logger.info("silver_%s: %s", split, summary["splits"][split])

    with timed_step(logger, "score the rule on the gold sample"):
        g = load_gold(args.gold)
        fl = rule_flags(g["text"])
        score = fl["strong"].astype(float) * 1.0 + (fl["weak"] & ~fl["strong"]).astype(float) * 0.0
        summary["rule_on_gold"] = gold_metrics(g["label"], score, g["weight"], 0.5)
        summary["rule_on_gold_ci95"] = gold_bootstrap(g["label"], score, g["weight"], g["stratum"], 0.5)
        logger.info("rule on gold (weighted): %s | 95%% CI %s", summary["rule_on_gold"], summary["rule_on_gold_ci95"])
    write_json(args.out_dir / "silver_summary.json", summary)
    logger.info("=== FARMERS LABEL WEAK finished successfully ===")


if __name__ == "__main__":
    main()
