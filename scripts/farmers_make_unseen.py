"""Draw a fresh, unseen tweet sample for you to label, so the models can be tested on data nobody trained or tuned on.

Tweets come from the test period (default 2021-10-01 onward, after the silver train and validation periods) and exclude every tweet in
silver_train / silver_val / silver_test and in the gold sample. The sample is stratified by the keyword rule (strong violence term /
rhetoric-only term / no term) and each stratum gets a weight, so the evaluation can estimate accuracy on the whole tweet population.

Outputs  data/farmers_unseen/unseen_to_label.csv   row_id, text, label   <- fill the `label` column (1 = violent, 0 = not)
         data/farmers_unseen/unseen_key.csv         row_id, tweet_id, stratum, weight   <- do not edit

Label 1 = the tweet describes, reports or incites a specific violent act or clash (beating, attack, killing, tear gas, riot).
Label 0 = anything else, including angry political talk, insults and slogans. Leave `label` blank to skip a tweet you cannot judge.
Label before looking at any model score (the sheet has none). Label at least 150 tweets; 300 is better.

Usage:  python scripts/farmers_make_unseen.py
        python scripts/farmers_make_unseen.py --n-strong 120 --n-weak 80 --n-none 100 --seed 11
"""
import argparse
from pathlib import Path

import pandas as pd

from common import DATA_DIR, log_environment, set_seed, setup_logger
from farmers_common import FARMERS_DIR, LABELS_DIR, clean_text, load_gold, rule_flags


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-date", default="2021-10-01")
    p.add_argument("--n-strong", type=int, default=100)
    p.add_argument("--n-weak", type=int, default=100, help="tweets with only rhetoric-prone terms")
    p.add_argument("--n-none", type=int, default=100)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out-dir", type=Path, default=DATA_DIR / "farmers_unseen")
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_make_unseen", log_dir=args.log_dir)
    log_environment(logger, args)
    set_seed(args.seed)

    used = set(load_gold(LABELS_DIR / "gold_claude.csv")["tweet_id"].astype(str))
    for name in ("silver_train", "silver_val", "silver_test"):
        f = LABELS_DIR / f"{name}.csv"
        if f.exists():
            used |= set(pd.read_csv(f, usecols=["tweet_id"], dtype={"tweet_id": str})["tweet_id"])
    logger.info("excluding %d tweets already used for training, validation, testing or the gold sample", len(used))

    tw = pd.read_csv(FARMERS_DIR / "tweets_text.csv.gz", usecols=["tweet_id", "date", "text"], dtype={"tweet_id": str})
    dt = pd.to_datetime(tw["date"], utc=True, format="ISO8601", errors="coerce")
    tw = tw[dt.notna()].copy()
    tw["day"] = dt[dt.notna()].dt.tz_localize(None).dt.normalize()
    tw = tw[(tw["day"] >= pd.Timestamp(args.from_date)) & ~tw["tweet_id"].isin(used)].drop_duplicates("tweet_id")
    tw["clean"] = tw["text"].astype(str).map(clean_text)
    tw = tw[tw["clean"].str.len() >= 25].drop_duplicates("clean")
    fl = rule_flags(tw["clean"])
    tw["stratum"] = ["strong" if s else ("weak_only" if w else "none") for s, w in zip(fl["strong"], fl["weak"])]
    pop = tw["stratum"].value_counts().to_dict()
    logger.info("eligible tweets from %s: %d  (%s)", args.from_date, len(tw), pop)

    parts = []
    for stratum, n in (("strong", args.n_strong), ("weak_only", args.n_weak), ("none", args.n_none)):
        pool = tw[tw["stratum"] == stratum]
        k = min(n, len(pool))
        s = pool.sample(k, random_state=args.seed).copy()
        s["weight"] = len(pool) / max(k, 1)
        parts.append(s)
    s = pd.concat(parts).sample(frac=1, random_state=args.seed).reset_index(drop=True)
    s["row_id"] = range(1, len(s) + 1)
    s["text_out"] = s["clean"].str.slice(0, 400)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    s[["row_id", "text_out"]].rename(columns={"text_out": "text"}).assign(label="").to_csv(args.out_dir / "unseen_to_label.csv", index=False, encoding="utf-8-sig")
    s[["row_id", "tweet_id", "stratum", "weight"]].to_csv(args.out_dir / "unseen_key.csv", index=False)
    logger.info("wrote %d tweets to %s", len(s), args.out_dir)
    print(f"\nLabel the `label` column in {args.out_dir / 'unseen_to_label.csv'} (1 = violent, 0 = not; blank = skip).")
    print("Then run: python scripts/farmers_eval_unseen.py")


if __name__ == "__main__":
    main()
