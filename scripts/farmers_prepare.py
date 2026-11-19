"""Prepare the #FarmersProtest tweet archive and link it to ACLED India by day.

Reads  data/tweets/archive.zip       (tweets.csv: tweetUrl, date, renderedContent, counts, ...)
       data/ACLED Data_*.csv         (ACLED India export)
Writes data/farmers_processed/tweets_text.csv.gz   tweet_id, date, user_id, text, likes, retweets, replies, is_reply, is_quote
       data/farmers_processed/tweets_daily.csv     one row per day: volume, users, engagement, keyword-group counts
       data/farmers_processed/acled_daily.csv      one row per day: ACLED India counts (all, farmer-related, violent, regional)
       data/farmers_processed/prepare_summary.json
Log    logs/farmers_prepare/

Notes
- tweetId in the CSV was exported as a float and has lost precision, so the real id is taken from tweetUrl.
- "Farmer-related" ACLED events: notes mention farm*, or actor/associated actor mentions Farm/Kisan/SKM.
- "Violent" ACLED events: sub_event_type in {Mob violence, Violent demonstration, Protest with intervention, Looting/property destruction, Armed clash, Attack}.

Usage:
    python scripts/farmers_prepare.py
    python scripts/farmers_prepare.py --max-rows 200000      # quick test
"""
import argparse
import glob
import time
import zipfile
from pathlib import Path

import pandas as pd

from common import DATA_DIR, log_environment, setup_logger, timed_step, write_json

KEYWORD_GROUPS = {
    "violence": r"violen|lathi|tear ?gas|clash|brutal|attack|beaten|beat up|killed|murder|died|dead|death|martyr|injur|shot|firing|water ?cannon|barricade|stone|pelt",
    "police": r"police|cops?\b|crpf|paramilitary|detain|arrest|jail|tihar",
    "event_words": r"chakka ?jaam|bharat ?bandh|rail ?roko|tractor ?(rally|parade|march)|red ?fort|republic day|mahapanchayat|kisan (parade|mahapanchayat)",
    "govt": r"modi|bjp|government|govt|farm ?laws?|msp|amit shah|tomar|parliament",
    "intl": r"rihanna|greta|thunberg|meena harris|canada|trudeau|un\b|uk parliament",
    "anti": r"khalistan|anti.?national|terror|urban naxal|paid|fake farmers?|andolanjeevi",
}
VIOLENT_SUBTYPES = {"Mob violence", "Violent demonstration", "Protest with intervention", "Looting/property destruction",
                    "Armed clash", "Attack"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", type=Path, default=DATA_DIR / "tweets" / "archive.zip")
    p.add_argument("--acled", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=DATA_DIR / "farmers_processed")
    p.add_argument("--chunk", type=int, default=200000)
    p.add_argument("--max-rows", type=int, default=0, help="stop after this many rows (0 = all)")
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def acled_daily(path, start, end, logger):
    a = pd.read_csv(path, low_memory=False)
    a["event_date"] = pd.to_datetime(a["event_date"])
    a = a[(a["event_date"] >= start) & (a["event_date"] <= end)].copy()
    farm = (a["notes"].str.contains("farm", case=False, na=False)
            | a["actor1"].astype(str).str.contains("Farm|Kisan", case=False)
            | a["assoc_actor_1"].astype(str).str.contains("Farm|Kisan|SKM", case=False))
    a["farmer"] = farm
    a["violent"] = a["sub_event_type"].isin(VIOLENT_SUBTYPES)
    a["is_protest"] = a["event_type"].isin(["Protests", "Riots"])
    a["fatal"] = a["fatalities"].fillna(0) > 0
    g = a.groupby("event_date")
    d = pd.DataFrame({
        "acled_all": g.size(),
        "acled_protest_riot": g["is_protest"].sum(),
        "acled_violent": g["violent"].sum(),
        "acled_farmer": g["farmer"].sum(),
        "acled_farmer_violent": g.apply(lambda x: int((x["farmer"] & x["violent"]).sum())),
        "acled_farmer_riot": g.apply(lambda x: int((x["farmer"] & (x["event_type"] == "Riots")).sum())),
        "acled_farmer_punjab_haryana_delhi": g.apply(lambda x: int((x["farmer"] & x["admin1"].isin(["Punjab", "Haryana", "Delhi"])).sum())),
        "acled_farmer_states": g.apply(lambda x: x.loc[x["farmer"], "admin1"].nunique()),
    })
    d = d.reindex(pd.date_range(start, end, freq="D"), fill_value=0)
    d.index.name = "date"
    logger.info("ACLED India %s..%s: %d events, %d farmer-related (%d violent, %d riots)", start.date(), end.date(), len(a),
                int(a["farmer"].sum()), int((a["farmer"] & a["violent"]).sum()), int((a["farmer"] & (a["event_type"] == "Riots")).sum()))
    return d.reset_index()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_prepare", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS PREPARE started (run %s) ===", run_id)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    acled_path = args.acled or Path(sorted(glob.glob(str(DATA_DIR / "ACLED Data_*.csv")))[0])
    logger.info("ACLED file: %s", acled_path.name)

    daily_parts, rows, t0 = [], 0, time.time()
    text_out = args.out_dir / "tweets_text.csv.gz"
    if text_out.exists():
        text_out.unlink()
    with timed_step(logger, "stream tweets"):
        with zipfile.ZipFile(args.zip) as z, z.open("tweets.csv") as f:
            reader = pd.read_csv(f, chunksize=args.chunk, dtype=str, usecols=[
                "tweetUrl", "date", "renderedContent", "userId", "replyCount", "retweetCount", "likeCount", "quoteCount", "quotedTweet"])
            for i, ch in enumerate(reader):
                ch = ch.dropna(subset=["date", "renderedContent"])
                ch["date_ts"] = pd.to_datetime(ch["date"], utc=True, errors="coerce", format="ISO8601")
                ch = ch.dropna(subset=["date_ts"])
                ch["tweet_id"] = ch["tweetUrl"].str.rsplit("/", n=1).str[-1]
                for c in ("replyCount", "retweetCount", "likeCount", "quoteCount"):
                    ch[c] = pd.to_numeric(ch[c], errors="coerce").fillna(0)
                text = ch["renderedContent"]
                ch["day"] = ch["date_ts"].dt.tz_localize(None).dt.normalize()
                ch["is_reply"] = text.str.startswith("@").astype(int)
                ch["is_quote"] = ch["quotedTweet"].notna().astype(int)
                low = text.str.lower()
                for name, pat in KEYWORD_GROUPS.items():
                    ch["kw_" + name] = low.str.contains(pat, regex=True).astype(int)
                kw = ["kw_" + k for k in KEYWORD_GROUPS]
                g = ch.groupby("day")
                part = g[kw + ["is_reply", "is_quote", "likeCount", "retweetCount", "replyCount"]].sum()
                part["n_tweets"] = g.size()
                part["chars_sum"] = g["renderedContent"].apply(lambda s: s.str.len().sum())
                part["users_list"] = g["userId"].apply(lambda s: set(s.dropna()))
                daily_parts.append(part)
                out = ch[["tweet_id", "date_ts", "userId", "renderedContent", "likeCount", "retweetCount", "replyCount", "is_reply", "is_quote"]]
                out.columns = ["tweet_id", "date", "user_id", "text", "likes", "retweets", "replies", "is_reply", "is_quote"]
                out.to_csv(text_out, mode="a", header=(i == 0), index=False, compression="gzip")
                rows += len(ch)
                logger.info("chunk %d: %d rows total (%.0fs)", i, rows, time.time() - t0)
                if args.max_rows and rows >= args.max_rows:
                    break

    with timed_step(logger, "daily aggregate"):
        big = pd.concat(daily_parts)
        sums = big.drop(columns=["users_list"]).groupby(level=0).sum()
        users = big["users_list"].groupby(level=0).apply(lambda s: len(set().union(*s)))
        sums["n_users"] = users
        sums["mean_chars"] = sums["chars_sum"] / sums["n_tweets"]
        sums = sums.drop(columns=["chars_sum"])
        start, end = sums.index.min(), sums.index.max()
        sums = sums.reindex(pd.date_range(start, end, freq="D"), fill_value=0)
        sums.index.name = "date"
        sums.reset_index().to_csv(args.out_dir / "tweets_daily.csv", index=False)
        acled = acled_daily(acled_path, start, end, logger)
        acled.to_csv(args.out_dir / "acled_daily.csv", index=False)

    summary = {"run_id": run_id, "tweets": int(rows), "date_min": str(start.date()), "date_max": str(end.date()),
               "days": int(len(sums)), "days_with_tweets": int((sums["n_tweets"] > 0).sum()),
               "acled_events_farmer": int(acled["acled_farmer"].sum()), "acled_events_all": int(acled["acled_all"].sum()),
               "keyword_groups": {k: v for k, v in KEYWORD_GROUPS.items()}}
    write_json(args.out_dir / "prepare_summary.json", summary)
    logger.info("tweets=%d days=%d..%s | wrote %s", rows, len(sums), end.date(), args.out_dir)
    logger.info("=== FARMERS PREPARE finished successfully ===")


if __name__ == "__main__":
    main()
