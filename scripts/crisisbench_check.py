"""Check whether QCRI/CrisisBench-english is usable for the civil-unrest project.

Downloads the dataset from Hugging Face (needs internet and `pip install datasets`),
then reports: columns, split sizes, label counts, tweet length, and how many tweets
mention civil-unrest vocabulary (protest, riot, strike, curfew, ...), overall and by label.

Outputs: results/crisisbench_check/report.json, unrest_keyword_examples.csv,
         data/crisisbench/<config>_<split>.csv (local copies for later experiments)
Log:     logs/crisisbench_check/crisisbench_check_<runid>.log

Usage:
    python scripts/crisisbench_check.py
    python scripts/crisisbench_check.py --configs informativeness humanitarian
"""
import argparse
import re
from pathlib import Path

import pandas as pd

from common import PROJECT_ROOT, RESULTS_DIR, log_environment, setup_logger, write_json

UNREST_PATTERN = re.compile(
    r"\b(protest\w*|riot\w*|demonstrat(?:ion|ions|or|ors)|unrest|curfew|clash(?:es|ed)?|"
    r"tear ?gas|looting|strike[sd]?|rall(?:y|ies)|march(?:ers)?|uprising|insurgen\w*|"
    r"police (?:fired|opened fire)|mob|violence)\b",
    re.IGNORECASE,
)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default="QCRI/CrisisBench-english")
    p.add_argument("--configs", nargs="+", default=["informativeness", "humanitarian"])
    p.add_argument("--save-dir", type=Path, default=PROJECT_ROOT / "data" / "crisisbench")
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def pick_columns(df):
    text_col = next((c for c in ("text", "tweet_text", "tweet", "content") if c in df.columns), None)
    if text_col is None:
        str_cols = [c for c in df.columns if df[c].dtype == object]
        text_col = max(str_cols, key=lambda c: df[c].astype(str).str.len().mean())
    label_col = next((c for c in ("class_label", "label", "labels", "category") if c in df.columns), None)
    return text_col, label_col


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("crisisbench_check", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== CRISISBENCH CHECK started (run %s) ===", run_id)
    from datasets import load_dataset

    out = RESULTS_DIR / "crisisbench_check"
    out.mkdir(parents=True, exist_ok=True)
    args.save_dir.mkdir(parents=True, exist_ok=True)
    report, examples = {}, []

    for config in args.configs:
        logger.info("loading %s / %s", args.dataset, config)
        ds = load_dataset(args.dataset, config)
        report[config] = {}
        for split, d in ds.items():
            df = d.to_pandas()
            text_col, label_col = pick_columns(df)
            df.to_csv(args.save_dir / f"{config}_{split}.csv", index=False)
            texts = df[text_col].astype(str)
            hit = texts.str.contains(UNREST_PATTERN)
            info = {"rows": int(len(df)), "columns": list(df.columns), "text_column": text_col, "label_column": label_col,
                    "mean_chars": round(float(texts.str.len().mean()), 1), "unrest_keyword_tweets": int(hit.sum()),
                    "unrest_keyword_share": round(float(hit.mean()), 4)}
            if label_col is not None:
                info["label_counts"] = {str(k): int(v) for k, v in df[label_col].value_counts().items()}
                info["unrest_keyword_share_by_label"] = {str(k): round(float(v), 4) for k, v in hit.groupby(df[label_col]).mean().items()}
            report[config][split] = info
            logger.info("%s/%s: %d rows | columns=%s | label=%s", config, split, len(df), list(df.columns), label_col)
            logger.info("   unrest-keyword tweets: %d (%.2f%%)", hit.sum(), 100 * hit.mean())
            if label_col is not None:
                logger.info("   label counts: %s", info["label_counts"])
            if split == "test":
                sample = df[hit].head(40).copy()
                sample.insert(0, "config", config)
                examples.append(sample)

    write_json(out / "report.json", report)
    if examples:
        pd.concat(examples).to_csv(out / "unrest_keyword_examples.csv", index=False)
    logger.info("Wrote %s", out)
    logger.info("=== CRISISBENCH CHECK finished successfully ===")


if __name__ == "__main__":
    main()
