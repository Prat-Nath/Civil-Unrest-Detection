"""Build the country-day panel, the unrest "surge" targets and the chronological splits.

Input : data/social_processed/country_day.csv   (from social_aggregate.py)
Output: data/social_processed/panel.csv.gz      one row per country and day with features and targets
        data/social_processed/panel_info.json   definitions, cut dates, counts and positive rates
Log   : logs/social_panel/social_panel_<runid>.log

Definitions (all computed from data up to and including the current day, except the targets):
  z_today   = (related tweets today - mean of the previous 28 days) / (std of the previous 28 days + 1)
  surge(t)  = z_today >= --z-threshold  AND  related tweets >= --min-related
  target_h  = 1 if a surge happens on any of the next h days (h = 1, 3, 7), else 0
  eligible  = the day itself is NOT a surge day (the task is the *emergence* of a surge from a calm day)
              and at least --baseline-days of history exist inside the same continuous stretch of data.

The surge label comes from the dataset's own civil-unrest classifier (the `civil_unrest_related` flag),
so it measures a spike in unrest-related tweet volume. It is not ACLED-style ground truth.

Usage:
    python scripts/social_panel.py
    python scripts/social_panel.py --z-threshold 3 --min-related 50
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from common import log_environment, setup_logger, timed_step, write_json
from social_common import HORIZONS, SOCIAL_PROCESSED, read_country_day


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, default=SOCIAL_PROCESSED / "country_day.csv")
    p.add_argument("--output-dir", type=Path, default=SOCIAL_PROCESSED)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--baseline-days", type=int, default=28)
    p.add_argument("--z-threshold", type=float, default=2.0)
    p.add_argument("--min-related", type=int, default=20)
    p.add_argument("--horizons", type=int, nargs="+", default=list(HORIZONS))
    p.add_argument("--gap-days", type=int, default=30, help="a gap longer than this between observed dates starts a new segment")
    p.add_argument("--val-frac", type=float, default=0.15)
    p.add_argument("--test-frac", type=float, default=0.15)
    return p.parse_args()


def assign_segments(dates, gap_days):
    """Segment id per date: a new segment starts after a gap longer than gap_days (e.g. the missing year 2016)."""
    uniq = np.array(sorted(pd.to_datetime(dates).unique()))
    seg = np.zeros(len(uniq), dtype=int)
    gaps = np.diff(uniq).astype("timedelta64[D]").astype(int)
    seg[1:] = np.cumsum(gaps > gap_days)
    return dict(zip(uniq, seg))


def build_country_segment(g, args):
    """Daily features and targets for one country inside one continuous stretch of dates."""
    full = pd.date_range(g["date"].min(), g["date"].max(), freq="D")
    g = g.set_index("date").reindex(full)
    g["missing"] = g["n_tweets"].isna().astype(int)
    for c in ("n_tweets", "n_related", "n_high", "mean_score", "max_score"):
        g[c] = g[c].fillna(0)
    g["country"] = g["country"].ffill().bfill()
    g["segment"] = g["segment"].ffill().bfill()
    g.index.name = "date"
    g = g.reset_index()

    rel = g["n_related"].astype(float)
    prev = rel.shift(1)
    mu = prev.rolling(args.baseline_days, min_periods=args.baseline_days).mean()
    sd = prev.rolling(args.baseline_days, min_periods=args.baseline_days).std()
    g["baseline_mean"] = mu
    g["z_today"] = (rel - mu) / (sd + 1.0)
    g["history_ok"] = mu.notna().astype(int)
    g["surge"] = ((g["z_today"] >= args.z_threshold) & (rel >= args.min_related) & (g["history_ok"] == 1)).astype(int)

    tw = g["n_tweets"].astype(float)
    g["log_tweets"] = np.log1p(tw)
    g["log_related"] = np.log1p(rel)
    g["share"] = rel / tw.clip(lower=1)
    g["high_share"] = g["n_high"] / tw.clip(lower=1)
    dow = g["date"].dt.dayofweek
    g["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    g["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # tabular history features (past and present only)
    for k in (1, 2, 3, 7):
        g[f"log_related_lag{k}"] = g["log_related"].shift(k)
    for w in (7, 14, 28):
        g[f"log_related_mean{w}"] = g["log_related"].rolling(w, min_periods=w).mean()
        g[f"log_related_std{w}"] = g["log_related"].rolling(w, min_periods=w).std()
        g[f"share_mean{w}"] = g["share"].rolling(w, min_periods=w).mean()
    g["log_tweets_mean28"] = g["log_tweets"].rolling(28, min_periods=28).mean()
    g["share_ratio_7_28"] = g["share_mean7"] / (g["share_mean28"] + 1e-6)
    g["surge_count_7"] = g["surge"].rolling(7, min_periods=1).sum()
    last = g["date"].where(g["surge"] == 1).ffill()
    g["days_since_surge"] = (g["date"] - last).dt.days.fillna(60).clip(upper=60)

    # targets: any surge in the next h days
    for h in args.horizons:
        fut = pd.concat([g["surge"].shift(-k) for k in range(1, h + 1)], axis=1)
        valid = fut.notna().all(axis=1)
        g[f"target_h{h}"] = np.where(valid, fut.max(axis=1), np.nan)
    return g


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("social_panel", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== SOCIAL PANEL started (run %s) | log file: %s ===", run_id, log_path)

    with timed_step(logger, "load country_day.csv"):
        df = read_country_day(args.input)
        logger.info("rows=%s | countries=%d | dates %s .. %s", f"{len(df):,}", df["country"].nunique(),
                    df["date"].min().date(), df["date"].max().date())
        seg_map = assign_segments(df["date"], args.gap_days)
        df["segment"] = df["date"].map(seg_map)
        logger.info("segments (continuous stretches of data): %s",
                    {int(s): (str(g["date"].min().date()), str(g["date"].max().date())) for s, g in df.groupby("segment")})

    with timed_step(logger, "build features and targets per country and segment"):
        parts = [build_country_segment(g, args) for _, g in df.groupby(["country", "segment"])]
        panel = pd.concat(parts, ignore_index=True)
        logger.info("panel rows (complete daily grid): %s", f"{len(panel):,}")

    panel["eligible"] = ((panel["history_ok"] == 1) & (panel["surge"] == 0)).astype(int)
    elig = panel[panel["eligible"] == 1]
    dates = np.array(sorted(elig["date"].unique()))
    n = len(dates)
    test_start = dates[int(n * (1 - args.test_frac))]
    val_start = dates[int(n * (1 - args.test_frac - args.val_frac))]
    panel["split"] = np.where(panel["date"] >= test_start, "test", np.where(panel["date"] >= val_start, "val", "train"))
    logger.info("chronological cut dates: val starts %s, test starts %s", pd.Timestamp(val_start).date(),
                pd.Timestamp(test_start).date())

    info = {
        "run_id": run_id,
        "args": {k: str(v) for k, v in vars(args).items()},
        "definition": "surge = z_today >= z_threshold and related tweets >= min_related; label is classifier-derived, not ground truth",
        "val_start": str(pd.Timestamp(val_start).date()),
        "test_start": str(pd.Timestamp(test_start).date()),
        "rows_total": int(len(panel)),
        "rows_eligible": int(len(elig)),
        "surge_days_total": int(panel["surge"].sum()),
        "surge_rate_among_history_ok": float(panel.loc[panel["history_ok"] == 1, "surge"].mean()),
        "countries": int(panel["country"].nunique()),
        "splits": {},
    }
    for split in ("train", "val", "test"):
        s = panel[(panel["split"] == split) & (panel["eligible"] == 1)]
        info["splits"][split] = {
            "rows": int(len(s)),
            "date_min": str(s["date"].min().date()),
            "date_max": str(s["date"].max().date()),
            "positive_rate": {f"h{h}": float(s[f"target_h{h}"].dropna().mean()) for h in args.horizons},
            "labelled_rows": {f"h{h}": int(s[f"target_h{h}"].notna().sum()) for h in args.horizons},
        }
        logger.info("%-5s rows=%s %s..%s | positive rate by horizon %s", split, f"{len(s):,}", s["date"].min().date(),
                    s["date"].max().date(), {k: round(v, 4) for k, v in info["splits"][split]["positive_rate"].items()})

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with timed_step(logger, "write panel.csv.gz"):
        panel.to_csv(args.output_dir / "panel.csv.gz", index=False)
    write_json(args.output_dir / "panel_info.json", info)
    write_json(log_path.with_name(log_path.stem + "_summary.json"), info)
    logger.info("=== SOCIAL PANEL finished successfully ===")


if __name__ == "__main__":
    main()
