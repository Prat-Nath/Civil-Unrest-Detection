"""Collect the #FarmersProtest results into one report: results/farmers_summary/{summary.md, summary.json}.

Uses the latest run in each results folder:
  farmers_tweets_*      tweet classifiers (rule baseline, tf-idf, transformer) on the gold sample
  farmers_transfer_*    ACLED-trained model on the gold sample (needs gold_claude_metrics.json from farmers_transfer_eval.py)
  farmers_extract_*     event/place extraction vs ACLED
  farmers_forecast_*    same-day detection and 1/3/7-day forecasts of ACLED surges
Missing parts are skipped. All gold numbers use labels written by Claude (single annotator, not human-verified).

Usage:
    python scripts/farmers_summary.py
"""
import argparse
import glob
import json
from pathlib import Path

import pandas as pd

from common import DATA_DIR, RESULTS_DIR, log_environment, setup_logger, write_json


def latest(pattern, must_contain=None):
    paths = sorted(glob.glob(str(RESULTS_DIR / pattern)))
    if must_contain:
        paths = [p for p in paths if Path(p, must_contain).exists()]
    return Path(paths[-1]) if paths else None


def load(p):
    return json.loads(Path(p).read_text())


def ci(x):
    return f"[{x[0]:.2f}, {x[1]:.2f}]" if x else ""


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    logger, log_path, run_id = setup_logger("farmers_summary")
    log_environment(logger, argparse.Namespace())
    out = {}
    md = ["# #FarmersProtest results summary", "",
          "Gold labels: 300 tweets labelled by Claude (single annotator, not human-verified); metrics are weighted to estimate the whole tweet population and carry wide intervals.", ""]

    prep = DATA_DIR / "farmers_processed" / "prepare_summary.json"
    if prep.exists():
        s = load(prep)
        out["data"] = {k: s[k] for k in ("tweets", "date_min", "date_max", "days", "acled_events_farmer", "acled_events_all")}
        md += ["## Data", f"- {s['tweets']:,} tweets, {s['date_min']} to {s['date_max']} ({s['days']} days); ACLED India events in window: {s['acled_events_all']:,}, farmer-related: {s['acled_events_farmer']:,}", ""]

    d = latest("farmers_tweets_*", "metrics.json")
    rows = []
    gt = {}
    gp = RESULTS_DIR / "farmers_gold_threshold" / "metrics.json"
    if gp.exists():
        gt = load(gp).get("models", {})
    if d:
        out["tweet_classifiers"] = {}
        m = load(d / "metrics.json")
        r = m["rule_baseline_gold"]
        rows.append(["rule (keywords)", r["precision"], r["recall"], r["f1"], ci(r["ci95"]["f1"]), r["pr_auc"], "-", "-"])
        # each model comes from the latest run that contains it (a transformer run does not hide the tf-idf row)
        for name in ("tfidf", "transformer"):
            paths = [p for p in sorted(glob.glob(str(RESULTS_DIR / "farmers_tweets_*" / "metrics.json")))
                     if name in load(Path(p))]
            if not paths:
                continue
            g_all = load(Path(paths[-1]))[name]
            g = g_all["gold"]
            tuned = f"{gt[name]['f1']:.2f}" if name in gt else "-"
            rows.append([name, g["precision"], g["recall"], g["f1"], ci(g["ci95"]["f1"]), g["pr_auc"], g_all["silver_test"]["f1"], tuned])
            out["tweet_classifiers"][name] = {"run": Path(paths[-1]).parent.name, "gold": g, "silver_test_f1": g_all["silver_test"]["f1"],
                                              "gold_tuned_threshold": gt.get(name)}
    t = latest("farmers_transfer_*", "gold_claude_metrics.json")
    if t:
        g = load(t / "gold_claude_metrics.json")
        rows.append(["ACLED-trained DistilRoBERTa (no retraining)", g["precision_weighted"], g["recall_weighted"], g["f1_weighted"], "", "", "-", "-"])
        out.setdefault("tweet_classifiers", {})["acled_model_transfer"] = g
    if rows:
        md += ["## Tweet-level violence detection (gold sample, weighted)", "",
               "| model | precision | recall | F1 | F1 95% CI | PR-AUC | silver-test F1 (agreement with rules) | F1 with threshold tuned on gold (5-fold CV) |", "|---|---|---|---|---|---|---|---|"]
        md += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
        md += ["", "The rule list was written while reading the gold sample, so the rule's own row is optimistic. The silver-test column measures agreement with the rules, not the truth. The threshold of each model is tuned on silver validation data (no ambiguous tweets, about 25% positive), which is not like the real tweet mix (about 7% violent), so precision on the gold sample is low and recall high; the last column re-tunes the threshold on part of the gold sample and scores the rest.", ""]

    e = latest("farmers_extract_*", "metrics.json")
    if e:
        m = load(e / "metrics.json")
        out["extraction"] = m
        md += ["## Event and place extraction vs ACLED", "", "| event type | tweets | ACLED events naming it | Spearman (daily) | precision@10 busiest days | chance |", "|---|---|---|---|---|---|"]
        for k, v in m["event_types"].items():
            md.append(f"| {k} | {v['tweets_mentioning']:,} | {v['acled_events_matching']} | {v['spearman_daily']} | {v['precision_at_10_days']} | {v['share_of_all_days_with_acled_event']} |")
        p = m["places"]
        md += ["", f"Places: Spearman between tweet mentions and ACLED farmer events is {p['spearman_state_day']} per state-day and {p['spearman_state_month']} per state-month. "
               f"On {p['busy_days_n']} busy days (20+ ACLED farmer events) the most-mentioned state matches ACLED's top state {p['top_state_match']:.0%} of the time "
               f"(always guessing {p['always_predict_most_mentioned_state_overall']['state']}: {p['always_predict_most_mentioned_state_overall']['accuracy']:.0%}); "
               f"ACLED's top state is among the tweets' top 3 in {p['acled_top_state_in_tweets_top3']:.0%} of them.", ""]

    f = latest("farmers_forecast_*", "metrics.json")
    if f:
        m = load(f / "metrics.json")
        out["forecast"] = {k: m[k] for k in ("h0", "h1", "h3", "h7")}
        names = [n for n in ("persistence", "acled", "tweets", "acled_tweets", "model_signals", "acled_tweets_model") if n in m["h0"]["models"]]
        md += ["## Predicting ACLED farmer-protest surges (walk-forward, ROC-AUC with 95% interval; 0.5 = chance)", "",
               "| task | surges | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
        for h, label in (("h0", "same day"), ("h1", "next 1 day"), ("h3", "next 3 days"), ("h7", "next 7 days")):
            c = m[h]["roc_auc_ci95_block_bootstrap"]
            md.append(f"| {label} | {m[h]['positives']} | " + " | ".join(f"{m[h]['models'][n]['roc_auc']:.2f} {ci(c.get(n))}" for n in names) + " |")
        md += ["", "Difference in ROC-AUC, (ACLED history + tweets) minus (ACLED history only), 95% interval: " +
               "; ".join(f"{l} {m[h]['roc_auc_ci95_block_bootstrap']['diff_acled_tweets_minus_acled']}" for h, l in (("h0", "same day"), ("h1", "1 day"), ("h3", "3 days"), ("h7", "7 days"))), ""]
    (RESULTS_DIR / "farmers_summary").mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "farmers_summary" / "summary.md").write_text("\n".join(md), encoding="utf-8")
    write_json(RESULTS_DIR / "farmers_summary" / "summary.json", out)
    logger.info("Wrote %s", RESULTS_DIR / "farmers_summary")
    print("\n".join(md))


if __name__ == "__main__":
    main()
