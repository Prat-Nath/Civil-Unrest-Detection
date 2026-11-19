"""Transfer test: does the ACLED-trained DistilRoBERTa violence classifier work on real #FarmersProtest tweets?

The model was trained on ACLED event notes (formal sentences, label = violent vs non-violent event). Here it scores a
per-day random sample of tweets (no retraining). We cannot compute precision/recall on tweets without tweet labels, so:
  1. day level: does the daily mean violence probability / flagged share track ACLED farmer-related violent events,
     riots and surge days (Spearman, ROC-AUC) - compared with a plain keyword baseline and with raw volume?
  2. tweet level: score distribution, top-scored tweets, and a label sheet (label_sample.csv) for hand-labelling
     so that real precision / recall / F1 can be computed later with scripts/farmers_transfer_eval.py.

Inputs   data/farmers_processed/{tweets_text.csv.gz, tweets_daily.csv, acled_daily.csv}, a model dir with best/
Outputs  results/farmers_transfer_<runid>/{scores.csv, daily_scores.csv, metrics.json, label_sample.csv, top_tweets.csv}
Log      logs/farmers_transfer/

Usage:
    python scripts/farmers_transfer.py --model-dir models/distilroberta-base_binary_20261117_015913
    python scripts/farmers_transfer.py --model-dir models/distilroberta-base_binary_masked_20261117_022130   # model trained with cue masking
"""
import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from common import DATA_DIR, MODELS_DIR, RESULTS_DIR, log_environment, mask_cues, set_seed, setup_logger, timed_step, write_json

URL = re.compile(r"https?://\S+|twitter\.com/\S+|pic\.twitter\.com/\S+")
MENTION = re.compile(r"@\w+")


def clean(t):
    t = URL.sub(" ", t)
    t = MENTION.sub("@user", t)
    t = t.replace("&amp;", "&").replace("#", "")
    return re.sub(r"\s+", " ", t).strip()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR / "farmers_processed")
    p.add_argument("--per-day", type=int, default=25)
    p.add_argument("--max-length", type=int, default=96)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--label-sheet-size", type=int, default=300)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def surge_flags(acled, z=2.0, min_events=15, base=28):
    x = acled["acled_farmer"]
    zs = (x - x.shift(1).rolling(base).mean()) / (x.shift(1).rolling(base).std() + 1)
    return ((zs >= z) & (x >= min_events)).astype(int)


def day_metrics(daily, name, col):
    out = {}
    for target in ("acled_farmer_violent", "acled_farmer_riot", "acled_farmer", "surge"):
        s = daily[col]
        y = daily[target]
        r = spearmanr(s, y).correlation
        entry = {"spearman": round(float(r), 3)}
        yb = (y > 0).astype(int) if target != "surge" else y
        if 0 < yb.sum() < len(yb):
            entry["roc_auc_day_has_event"] = round(float(roc_auc_score(yb, s)), 3)
        out[target] = entry
    return out


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_transfer", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS TRANSFER started (run %s) ===", run_id)
    set_seed(args.seed)
    cfg = json.loads((args.model_dir / "run_config.json").read_text())
    use_mask = bool(cfg.get("mask_cues"))
    thr = float(cfg.get("threshold", 0.5))
    logger.info("model %s | mask_cues=%s | stored threshold=%.3f | labels=%s", args.model_dir.name, use_mask, thr, cfg.get("label_names"))
    out_dir = RESULTS_DIR / f"farmers_transfer_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with timed_step(logger, "sample tweets"):
        tw = pd.read_csv(args.data_dir / "tweets_text.csv.gz", usecols=["tweet_id", "date", "text", "likes", "retweets"], dtype={"tweet_id": str})
        dt = pd.to_datetime(tw["date"], utc=True, format="ISO8601", errors="coerce")
        n_bad = int(dt.isna().sum())
        if n_bad:
            logger.warning("dropping %d rows with an unreadable date (malformed csv lines)", n_bad)
        tw = tw[dt.notna()].copy()
        tw["day"] = dt[dt.notna()].dt.tz_localize(None).dt.normalize()
        tw["text"] = tw["text"].astype(str)
        tw = tw.drop_duplicates("tweet_id")
        samp = tw.sample(frac=1.0, random_state=args.seed).groupby("day").head(args.per_day).copy()
        samp["clean"] = samp["text"].map(clean)
        samp = samp[samp["clean"].str.len() >= 15].reset_index(drop=True)
        logger.info("sampled %d tweets over %d days (<= %d per day) from %d", len(samp), samp["day"].nunique(), args.per_day, len(tw))
        model_text = samp["clean"].map(mask_cues) if use_mask else samp["clean"]

    with timed_step(logger, "score"):
        tok = AutoTokenizer.from_pretrained(args.model_dir / "best")
        model = AutoModelForSequenceClassification.from_pretrained(args.model_dir / "best").eval()
        torch.set_num_threads(2)
        order = np.argsort(model_text.str.len().values)
        probs = np.zeros(len(samp))
        t0 = time.time()
        with torch.no_grad():
            for i in range(0, len(order), args.batch_size):
                idx = order[i:i + args.batch_size]
                enc = tok(model_text.iloc[idx].tolist(), truncation=True, max_length=args.max_length, padding=True, return_tensors="pt")
                probs[idx] = torch.softmax(model(**enc).logits, dim=-1)[:, 1].numpy()
                if (i // args.batch_size) % 20 == 0:
                    logger.info("scored %d / %d (%.0fs)", i + len(idx), len(order), time.time() - t0)
        samp["p_violent"] = probs
        samp[["tweet_id", "day", "text", "p_violent", "likes", "retweets"]].to_csv(out_dir / "scores.csv", index=False)

    with timed_step(logger, "day-level comparison with ACLED"):
        t_daily = pd.read_csv(args.data_dir / "tweets_daily.csv", parse_dates=["date"]).set_index("date")
        a_daily = pd.read_csv(args.data_dir / "acled_daily.csv", parse_dates=["date"]).set_index("date")
        g = samp.groupby("day")
        d = pd.DataFrame({"n_sampled": g.size(), "mean_p": g["p_violent"].mean(), "share_p_ge_0.5": g["p_violent"].apply(lambda s: (s >= 0.5).mean()),
                          "share_p_ge_thr": g["p_violent"].apply(lambda s: (s >= thr).mean()), "p90": g["p_violent"].quantile(0.9)})
        d = d.join(t_daily[["n_tweets", "kw_violence", "kw_police", "kw_event_words"]]).join(a_daily)
        d["kw_violence_share"] = d["kw_violence"] / d["n_tweets"]
        d["log_volume"] = np.log1p(d["n_tweets"])
        d["surge"] = surge_flags(a_daily).reindex(d.index).fillna(0).astype(int)
        d.to_csv(out_dir / "daily_scores.csv")
        m = {"model": args.model_dir.name, "mask_cues": use_mask, "threshold": thr, "tweets_scored": int(len(samp)), "days": int(len(d)),
             "tweet_level": {"mean_p": round(float(probs.mean()), 4), "share_ge_0.5": round(float((probs >= 0.5).mean()), 4),
                             "share_ge_thr": round(float((probs >= thr).mean()), 4),
                             "quantiles": {str(q): round(float(np.quantile(probs, q)), 4) for q in (0.5, 0.9, 0.99)}}}
        m["day_level"] = {name: day_metrics(d, name, col) for name, col in [
            ("model_mean_p", "mean_p"), ("model_share_p_ge_0.5", "share_p_ge_0.5"), ("model_p90", "p90"),
            ("keyword_violence_share", "kw_violence_share"), ("tweet_volume", "log_volume")]}
        for k, v in m["day_level"].items():
            logger.info("%-26s %s", k, v)
        logger.info("tweet level: %s", m["tweet_level"])

    with timed_step(logger, "label sheet and top tweets"):
        top = samp.nlargest(40, "p_violent")[["tweet_id", "day", "p_violent", "text"]]
        top.to_csv(out_dir / "top_tweets.csv", index=False)
        n = args.label_sheet_size // 3
        rng = np.random.default_rng(args.seed)
        def take(df, k):
            return df.sample(min(k, len(df)), random_state=args.seed)
        parts = [take(samp.nlargest(n * 2, "p_violent"), n),
                 take(samp[(samp["p_violent"] > 0.05) & (samp["p_violent"] < 0.5)], n),
                 take(samp[samp["p_violent"] <= 0.05], n)]
        sheet = pd.concat(parts).sample(frac=1, random_state=args.seed)[["tweet_id", "day", "text"]]
        sheet["label"] = ""      # fill with 1 = describes/incites violence or clashes with police, 0 = otherwise
        sheet.to_csv(out_dir / "label_sample.csv", index=False)
        logger.info("label sheet: %d tweets (stratified by score; scores hidden)", len(sheet))

    write_json(out_dir / "metrics.json", m)
    logger.info("Wrote %s", out_dir)
    logger.info("=== FARMERS TRANSFER finished successfully ===")


if __name__ == "__main__":
    main()
