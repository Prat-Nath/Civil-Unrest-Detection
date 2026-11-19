"""Do #FarmersProtest tweet signals help predict ACLED farmer-related protest surges?

Daily data (386 days, Nov 2020 - Nov 2021) from scripts/farmers_prepare.py.

Target   (h = 0 is the same-day detection task: using today's tweets, is today a surge? ACLED features are lagged one day there.)
         surge day = ACLED farmer-related events today are >= z_threshold std above the previous 28-day mean
         (z = (x - mean) / (std + 1)) AND >= min_events.  Task: from a day with no surge today, will a surge occur
         within the next h days (h = 1, 3, 7)?
Features everything known at the end of day t (no future information):
         acled     history of ACLED farmer / violent / all-India counts
         tweets    volume, users, keyword-group shares (violence, police, event words such as bharat bandh / chakka jaam,
                   government, international, anti-protest), engagement, changes vs recent days
         calendar  day of week
Models   prevalence (expanding), persistence rule (ACLED 7-day vs 28-day ratio), logistic regression on
         acled / tweets / acled+tweets  (class-balanced, standardised, C=0.1)
Eval     walk-forward: refit every `--refit-every` days on all earlier days (only rows whose target window has fully
         closed), predict the next block. Metrics over all out-of-sample predictions: ROC-AUC, PR-AUC (lift over base rate),
         precision/recall when alerting on the top `--alert-share` of days, and a 7-day block-bootstrap interval.
         Lead time: how many surges were preceded by an alert within 1 / 3 / 7 days, and the mean lead.

Outputs  results/farmers_forecast_<runid>/{metrics.json, predictions.csv, coefficients_h*.csv, surge_days.csv, leadtime.csv}
Log      logs/farmers_forecast/farmers_forecast_<runid>.log

Usage:
    python scripts/farmers_forecast.py
"""
import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

from common import DATA_DIR, RESULTS_DIR, log_environment, setup_logger, timed_step, write_json

warnings.filterwarnings("ignore")
HORIZONS = (0, 1, 3, 7)   # h = 0 is "nowcasting": is today a surge day? (tweets of day t are known at the end of day t)
KW = ["violence", "police", "event_words", "govt", "intl", "anti"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR / "farmers_processed")
    p.add_argument("--z-threshold", type=float, default=2.0)
    p.add_argument("--min-events", type=int, default=15)
    p.add_argument("--baseline-days", type=int, default=28)
    p.add_argument("--min-train-days", type=int, default=90)
    p.add_argument("--refit-every", type=int, default=7)
    p.add_argument("--alert-share", type=float, default=0.2)
    p.add_argument("--C", type=float, default=0.1)
    p.add_argument("--bootstrap", type=int, default=500)
    p.add_argument("--signals", type=Path, default=None, help="daily_signals_<model>.csv from farmers_train_tweets.py --score-daily (adds the classifier's daily scores as features)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def build_frame(data_dir, z_threshold, min_events, baseline_days, signals=None):
    t = pd.read_csv(data_dir / "tweets_daily.csv", parse_dates=["date"]).set_index("date")
    a = pd.read_csv(data_dir / "acled_daily.csv", parse_dates=["date"]).set_index("date")
    d = t.join(a, how="inner")
    x = d["acled_farmer"]
    mean = x.shift(1).rolling(baseline_days).mean()
    std = x.shift(1).rolling(baseline_days).std()
    d["z_acled"] = (x - mean) / (std + 1)
    d["surge"] = ((d["z_acled"] >= z_threshold) & (x >= min_events)).astype(int)
    d["history_ok"] = (np.arange(len(d)) >= baseline_days).astype(int)

    f = pd.DataFrame(index=d.index)
    # tweets
    n = d["n_tweets"].clip(lower=1)
    f["tw_log_tweets"] = np.log1p(d["n_tweets"])
    f["tw_log_users"] = np.log1p(d["n_users"])
    f["tw_tweets_vs_7d"] = np.log1p(d["n_tweets"]) - np.log1p(d["n_tweets"].shift(1).rolling(7, min_periods=1).mean())
    f["tw_tweets_vs_28d"] = np.log1p(d["n_tweets"]) - np.log1p(d["n_tweets"].shift(1).rolling(28, min_periods=7).mean())
    f["tw_users_per_tweet"] = d["n_users"] / n
    f["tw_reply_share"] = d["is_reply"] / n
    f["tw_quote_share"] = d["is_quote"] / n
    f["tw_log_likes_per_tweet"] = np.log1p(d["likeCount"] / n)
    f["tw_mean_chars"] = d["mean_chars"]
    for k in KW:
        share = d[f"kw_{k}"] / n
        f[f"tw_{k}_share"] = share
        f[f"tw_{k}_share_vs_7d"] = share - share.shift(1).rolling(7, min_periods=1).mean()
    f["tw_event_words_log"] = np.log1p(d["kw_event_words"])
    f["tw_event_words_3d"] = np.log1p(d["kw_event_words"].rolling(3, min_periods=1).sum())
    # acled history
    f["ac_log_farmer"] = np.log1p(d["acled_farmer"])
    f["ac_farmer_7d"] = np.log1p(d["acled_farmer"].rolling(7, min_periods=1).mean())
    f["ac_farmer_28d"] = np.log1p(d["acled_farmer"].rolling(28, min_periods=7).mean())
    f["ac_farmer_ratio_7_28"] = f["ac_farmer_7d"] - f["ac_farmer_28d"]
    f["ac_log_violent"] = np.log1p(d["acled_farmer_violent"])
    f["ac_log_all"] = np.log1p(d["acled_all"])
    f["ac_log_riot"] = np.log1p(d["acled_farmer_riot"])
    f["ac_states"] = d["acled_farmer_states"]
    f["ac_z"] = d["z_acled"].clip(-3, 10)
    last = d.index.to_series().where(d["surge"] == 1).ffill()
    f["ac_days_since_surge"] = (d.index.to_series() - last).dt.days.fillna(60).clip(upper=60)
    # classifier signals (mean violence probability etc. from the trained tweet model, scored on a per-day sample)
    if signals is not None:
        sg = pd.read_csv(signals, parse_dates=["date"]).set_index("date").reindex(d.index)
        for c in ("mean_p", "share_flag", "p90"):
            v = sg[c].ffill().bfill()
            f[f"sg_{c}"] = v
            f[f"sg_{c}_vs_7d"] = v - v.shift(1).rolling(7, min_periods=1).mean()
    # calendar
    dow = d.index.dayofweek
    f["cal_dow_sin"], f["cal_dow_cos"] = np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)
    f = f.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    for h in HORIZONS:
        if h == 0:
            d["target_h0"] = d["surge"].astype(float)
            continue
        fut = pd.concat([d["surge"].shift(-k) for k in range(1, h + 1)], axis=1)
        d[f"target_h{h}"] = fut.max(axis=1).where(fut.notna().all(axis=1))
    d["eligible"] = ((d["surge"] == 0) & (d["history_ok"] == 1)).astype(int)
    d["eligible_h0"] = d["history_ok"]
    return d, f


def block_bootstrap(y, scores_dict, block, n_boot, rng):
    """Block-bootstrap CI for ROC-AUC of each model and for the difference acled_tweets - acled."""
    n = len(y)
    starts = np.arange(0, n, block)
    out = {k: [] for k in scores_dict}
    out["diff_acled_tweets_minus_acled"] = []
    out["diff_acled_tweets_model_minus_acled_tweets"] = []
    for _ in range(n_boot):
        idx = np.concatenate([np.arange(s, min(s + block, n)) for s in rng.choice(starts, size=len(starts), replace=True)])
        yy = y[idx]
        if yy.sum() == 0 or yy.sum() == len(yy):
            continue
        aucs = {k: roc_auc_score(yy, v[idx]) for k, v in scores_dict.items()}
        for k, v in aucs.items():
            out[k].append(v)
        if "acled_tweets" in aucs and "acled" in aucs:
            out["diff_acled_tweets_minus_acled"].append(aucs["acled_tweets"] - aucs["acled"])
        if "acled_tweets_model" in aucs and "acled_tweets" in aucs:
            out["diff_acled_tweets_model_minus_acled_tweets"].append(aucs["acled_tweets_model"] - aucs["acled_tweets"])
    return {k: [round(float(np.percentile(v, 2.5)), 3), round(float(np.percentile(v, 97.5)), 3)] for k, v in out.items() if v}


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_forecast", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS FORECAST started (run %s) ===", run_id)
    rng = np.random.default_rng(args.seed)
    out_dir = RESULTS_DIR / f"farmers_forecast_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with timed_step(logger, "build features"):
        d, f = build_frame(args.data_dir, args.z_threshold, args.min_events, args.baseline_days, args.signals)
        surge_days = d.index[d["surge"] == 1]
        logger.info("days=%d | surge days=%d: %s", len(d), len(surge_days), [str(x.date()) for x in surge_days])
        pd.DataFrame({"date": surge_days, "acled_farmer": d.loc[surge_days, "acled_farmer"], "n_tweets": d.loc[surge_days, "n_tweets"]}).to_csv(out_dir / "surge_days.csv", index=False)
    f_full = f
    cols = {
        "acled": [c for c in f.columns if c.startswith(("ac_", "cal_"))],
        "tweets": [c for c in f.columns if c.startswith(("tw_", "cal_"))],
        "acled_tweets": [c for c in f.columns if not c.startswith("sg_")],
    }
    if args.signals is not None:
        cols["model_signals"] = [c for c in f.columns if c.startswith(("sg_", "cal_"))]
        cols["acled_tweets_model"] = list(f.columns)
    for k, v in cols.items():
        logger.info("feature set %-13s: %d features", k, len(v))

    metrics, preds_all, coef_rows, lead_rows = {}, [], {}, []
    f_now = f.copy()
    ac_cols = [c for c in f.columns if c.startswith("ac_")]
    f_now[ac_cols] = f[ac_cols].shift(1).fillna(0.0)      # same-day task: ACLED of day t is not yet known
    for h in HORIZONS:
        tgt = f"target_h{h}"
        f = f_now if h == 0 else f_full
        rows = d.index[((d["eligible_h0"] if h == 0 else d["eligible"]) == 1) & d[tgt].notna()]
        y = d.loc[rows, tgt].astype(int).values
        n = len(rows)
        pos_dates = {r: i for i, r in enumerate(d.index)}
        day_idx = np.array([pos_dates[r] for r in rows])
        scores = {k: np.full(n, np.nan) for k in list(cols) + ["persistence", "prevalence"]}
        thresholds = {k: np.full(n, np.nan) for k in cols}
        first_test = args.min_train_days
        coefs = {k: [] for k in cols}
        for start in range(first_test, n, args.refit_every):
            end = min(start + args.refit_every, n)
            # only use training rows whose target window closed before the first predicted day
            train_mask = (day_idx + h) < day_idx[start]
            train_mask[start:] = False
            if train_mask.sum() < 40 or len(np.unique(y[train_mask])) < 2:
                continue
            scores["prevalence"][start:end] = y[train_mask].mean()
            for name, cs in cols.items():
                Xtr, Xte = f.loc[rows[train_mask], cs].values, f.loc[rows[start:end], cs].values
                sc = StandardScaler().fit(Xtr)
                clf = LogisticRegression(C=args.C, class_weight="balanced", max_iter=2000).fit(sc.transform(Xtr), y[train_mask])
                scores[name][start:end] = clf.predict_proba(sc.transform(Xte))[:, 1]
                thresholds[name][start:end] = np.quantile(clf.predict_proba(sc.transform(Xtr))[:, 1], 1 - args.alert_share)
                coefs[name].append(clf.coef_[0])
            scores["persistence"][start:end] = f.loc[rows[start:end], "ac_farmer_ratio_7_28"].values
        ok = ~np.isnan(scores["acled"])
        yy = y[ok]
        base = float(yy.mean())
        logger.info("h=%d: eligible rows=%d, scored=%d, positives=%d (%.1f%%)", h, n, ok.sum(), yy.sum(), 100 * base)
        m = {"n_scored": int(ok.sum()), "positives": int(yy.sum()), "base_rate": round(base, 4), "models": {}}
        names = ["prevalence", "persistence", "acled", "tweets", "acled_tweets"] + [k for k in ("model_signals", "acled_tweets_model") if k in cols]
        for name in names:
            s = scores[name][ok]
            r = {"roc_auc": round(float(roc_auc_score(yy, s)), 4) if name != "prevalence" else 0.5,
                 "pr_auc": round(float(average_precision_score(yy, s)), 4) if name != "prevalence" else round(base, 4)}
            r["pr_auc_lift"] = round(r["pr_auc"] / base, 2)
            if name in cols:
                alert = scores[name][ok] >= thresholds[name][ok]
            elif name == "persistence":
                alert = s >= np.quantile(s, 1 - args.alert_share)
            else:
                alert = np.zeros(len(yy), bool)
            tp = int((alert & (yy == 1)).sum())
            r.update({"alert_days": int(alert.sum()), "precision_at_alert": round(tp / max(alert.sum(), 1), 4), "recall_at_alert": round(tp / max(yy.sum(), 1), 4)})
            pr, rc = r["precision_at_alert"], r["recall_at_alert"]
            r["f1_at_alert"] = round(2 * pr * rc / (pr + rc), 4) if pr + rc else 0.0
            m["models"][name] = r
            logger.info("  %-13s ROC-AUC=%.3f PR-AUC=%.3f (x%.2f) | alert %d days: P=%.3f R=%.3f F1=%.3f", name, r["roc_auc"], r["pr_auc"], r["pr_auc_lift"],
                        r["alert_days"], pr, rc, r["f1_at_alert"])
        m["roc_auc_ci95_block_bootstrap"] = block_bootstrap(yy, {k: scores[k][ok] for k in ("persistence", "acled", "tweets", "acled_tweets", "model_signals", "acled_tweets_model") if k in scores}, 7, args.bootstrap, rng)
        logger.info("  95%% CI (7-day block bootstrap): %s", m["roc_auc_ci95_block_bootstrap"])
        metrics[f"h{h}"] = m

        pr = pd.DataFrame({"date": rows[ok], "horizon": h, "y": yy, **{k: scores[k][ok] for k in scores}})
        pr["alert_acled_tweets"] = (scores["acled_tweets"][ok] >= thresholds["acled_tweets"][ok]).astype(int)
        pr["alert_acled"] = (scores["acled"][ok] >= thresholds["acled"][ok]).astype(int)
        preds_all.append(pr)
        top_set = "acled_tweets_model" if "acled_tweets_model" in cols else "acled_tweets"
        coef_rows[h] = pd.DataFrame({"feature": cols[top_set], "mean_coef": np.mean(coefs[top_set], axis=0)}).sort_values("mean_coef", key=np.abs, ascending=False)
        coef_rows[h].to_csv(out_dir / f"coefficients_h{h}.csv", index=False)
        logger.info("  top coefficients (%s): %s", top_set, [(r.feature, round(r.mean_coef, 2)) for r in coef_rows[h].head(6).itertuples()])

    preds = pd.concat(preds_all)
    preds.to_csv(out_dir / "predictions.csv", index=False)

    # lead time: for each surge in the scored period, was there an alert in the preceding days?
    with timed_step(logger, "lead-time analysis"):
        h = 7
        p7 = preds[preds["horizon"] == h].set_index("date")
        for model in ("acled", "acled_tweets"):
            alerts = p7.index[p7[f"alert_{model}"] == 1]
            scored_first = p7.index.min()
            surges = [s for s in surge_days if s > scored_first]
            leads = []
            for s in surges:
                prior = [a for a in alerts if 0 < (s - a).days <= 7]
                leads.append(max((s - a).days for a in prior) if prior else 0)
            detected = [l for l in leads if l > 0]
            lead_rows.append({"model": model, "surges_after_first_prediction": len(surges), "surges_with_alert_in_prior_7d": len(detected),
                              "share_detected": round(len(detected) / max(len(surges), 1), 3), "mean_earliest_lead_days": round(float(np.mean(detected)), 2) if detected else 0.0,
                              "alert_days": int(len(alerts)), "alert_share_of_scored_days": round(len(alerts) / len(p7), 3)})
            logger.info("lead time | %s", lead_rows[-1])
        pd.DataFrame(lead_rows).to_csv(out_dir / "leadtime.csv", index=False)
        metrics["lead_time_h7"] = lead_rows

    metrics["config"] = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    metrics["surge_days"] = [str(x.date()) for x in surge_days]
    write_json(out_dir / "metrics.json", metrics)
    logger.info("Wrote %s", out_dir)
    logger.info("=== FARMERS FORECAST finished successfully ===")


if __name__ == "__main__":
    main()
