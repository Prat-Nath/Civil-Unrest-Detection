"""Extract event types and places from the #FarmersProtest tweets and cross-check them against ACLED India.

Per tweet (rules in scripts/farmers_common.py): which event types are mentioned (bharat bandh, chakka jaam, rail roko, tractor
rally, mahapanchayat, road blockade, police force, clash, dharna) and which Indian states/sites are named (Singhu/Tikri -> Delhi,
Karnal -> Haryana, Lakhimpur -> Uttar Pradesh, ...).

Cross-check against ACLED (ground truth for the same days):
  1. event types   daily tweet mentions of a type vs ACLED events whose notes name the same type (Spearman); and
                   "do the 10 busiest tweet days for this type fall on days ACLED also reports it?" (precision@10)
  2. places        per day and state: tweet mentions vs ACLED farmer-related events (Spearman over state-days, and over
                   state-months); and "is the most-mentioned state on a day also the state with most ACLED farmer events?"
                   (days with >= 20 farmer events in ACLED)
Rules and the ACLED matching are heuristics: the numbers say how well tweets agree with ACLED, not that either one is perfect.

Inputs   data/farmers_processed/tweets_text.csv.gz, the ACLED csv in data/
Outputs  results/farmers_extract_<runid>/{metrics.json, event_type_daily.csv, state_daily.csv, event_type_top_days.csv}
Log      logs/farmers_extract/

Usage:
    python scripts/farmers_extract.py
"""
import argparse
import glob
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from common import DATA_DIR, RESULTS_DIR, log_environment, setup_logger, timed_step, write_json
from farmers_common import EVENT_TYPES, FARMERS_DIR, STATES, extract_events, load_acled_events


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tweets", type=Path, default=FARMERS_DIR / "tweets_text.csv.gz")
    p.add_argument("--acled", type=Path, default=None)
    p.add_argument("--chunk", type=int, default=150000)
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_extract", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS EXTRACT started (run %s) ===", run_id)
    out_dir = RESULTS_DIR / f"farmers_extract_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    acled_path = args.acled or Path(sorted(glob.glob(str(DATA_DIR / "ACLED Data_*.csv")))[0])

    day_parts, rows = [], 0
    with timed_step(logger, "extract from tweets"):
        for i, ch in enumerate(pd.read_csv(args.tweets, usecols=["tweet_id", "date", "text"], dtype={"tweet_id": str}, chunksize=args.chunk)):
            dt = pd.to_datetime(ch["date"], utc=True, format="ISO8601", errors="coerce")
            ch = ch[dt.notna()].copy()
            ch["day"] = dt[dt.notna()].dt.tz_localize(None).dt.normalize()
            ex = extract_events(ch["text"].astype(str))
            ex["n_tweets"] = 1
            ex["day"] = ch["day"].values
            day_parts.append(ex.groupby("day").sum())
            rows += len(ch)
            logger.info("chunk %d: %d tweets", i, rows)
            if args.max_rows and rows >= args.max_rows:
                break
        daily = pd.concat(day_parts).groupby(level=0).sum()
        daily.index.name = "date"
        start, end = daily.index.min(), daily.index.max()
        logger.info("tweets: %d | days %s..%s", rows, start.date(), end.date())

    acled = load_acled_events(acled_path, start, end)
    logger.info("ACLED India events in window: %d (farmer-related %d)", len(acled), int(acled["farmer"].sum()))
    metrics = {"run_id": run_id, "tweets": int(rows), "days": int(len(daily)), "event_types": {}, "places": {}}

    with timed_step(logger, "event types vs ACLED"):
        idx = pd.date_range(start, end, freq="D")
        daily = daily.reindex(idx, fill_value=0)
        out_rows, top_rows = [], []
        for k, pat in EVENT_TYPES.items():
            rx = re.compile(pat, re.I)
            acled_hit = acled[acled["farmer"] & acled["notes"].astype(str).str.lower().str.contains(rx)]
            a_daily = acled_hit.groupby("event_date").size().reindex(idx, fill_value=0)
            t_daily = daily[f"ev_{k}"]
            r = spearmanr(t_daily, a_daily).correlation if t_daily.sum() and a_daily.sum() else float("nan")
            top = t_daily.nlargest(10).index
            p_at_10 = float((a_daily.loc[top] > 0).mean())
            base = float((a_daily > 0).mean())
            metrics["event_types"][k] = {"tweets_mentioning": int(t_daily.sum()), "acled_events_matching": int(a_daily.sum()), "spearman_daily": None if np.isnan(r) else round(float(r), 3),
                                        "precision_at_10_days": round(p_at_10, 2), "share_of_all_days_with_acled_event": round(base, 3)}
            logger.info("%-22s tweets=%7d acled=%5d spearman=%s P@10=%.2f (chance %.2f)", k, t_daily.sum(), a_daily.sum(), metrics["event_types"][k]["spearman_daily"], p_at_10, base)
            out_rows.append(pd.DataFrame({"date": idx, "event_type": k, "tweet_mentions": t_daily.values, "acled_events": a_daily.values}))
            for d in top:
                top_rows.append({"event_type": k, "date": d.date(), "tweet_mentions": int(t_daily.loc[d]), "acled_events_naming_type": int(a_daily.loc[d])})
        pd.concat(out_rows).to_csv(out_dir / "event_type_daily.csv", index=False)
        pd.DataFrame(top_rows).to_csv(out_dir / "event_type_top_days.csv", index=False)

    with timed_step(logger, "places vs ACLED"):
        a_state = acled[acled["farmer"]].groupby(["event_date", "admin1"]).size().rename("acled_farmer").reset_index()
        rows_s = []
        for s in STATES:
            t = daily[f"st_{s}"]
            a = a_state[a_state["admin1"] == s].set_index("event_date")["acled_farmer"].reindex(idx, fill_value=0)
            rows_s.append(pd.DataFrame({"date": idx, "state": s, "tweet_mentions": t.values, "acled_farmer_events": a.values}))
        sd = pd.concat(rows_s)
        sd.to_csv(out_dir / "state_daily.csv", index=False)
        sd["month"] = sd["date"].dt.to_period("M")
        sm = sd.groupby(["state", "month"])[["tweet_mentions", "acled_farmer_events"]].sum()
        # tweet share of mentions per state-day vs ACLED farmer events
        r_day = spearmanr(sd["tweet_mentions"], sd["acled_farmer_events"]).correlation
        r_month = spearmanr(sm["tweet_mentions"], sm["acled_farmer_events"]).correlation
        # most-mentioned state vs state with most ACLED events, on busy days
        pivot_t = sd.pivot(index="date", columns="state", values="tweet_mentions")
        pivot_a = sd.pivot(index="date", columns="state", values="acled_farmer_events")
        busy = pivot_a.sum(axis=1) >= 20
        top_t, top_a = pivot_t[busy].idxmax(axis=1), pivot_a[busy].idxmax(axis=1)
        acc = float((top_t == top_a).mean()) if busy.any() else float("nan")
        in_top3 = float(np.mean([top_a.loc[d] in pivot_t.loc[d].nlargest(3).index for d in top_a.index])) if busy.any() else float("nan")
        # reference: how often a state chosen by overall mention volume is right
        fixed = pivot_t.sum().idxmax()
        base_acc = float((top_a == fixed).mean()) if busy.any() else float("nan")
        metrics["places"] = {"spearman_state_day": round(float(r_day), 3), "spearman_state_month": round(float(r_month), 3), "busy_days_n": int(busy.sum()),
                             "top_state_match": round(acc, 3), "acled_top_state_in_tweets_top3": round(in_top3, 3),
                             "always_predict_most_mentioned_state_overall": {"state": fixed, "accuracy": round(base_acc, 3)}}
        logger.info("places: %s", metrics["places"])
    write_json(out_dir / "metrics.json", metrics)
    logger.info("Wrote %s", out_dir)
    logger.info("=== FARMERS EXTRACT finished successfully ===")


if __name__ == "__main__":
    main()
