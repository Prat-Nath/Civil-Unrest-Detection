"""Collect every social-signal experiment into one table and one lead-time chart.

Reads  results/social_baselines_*/metrics.json (latest run) and results/social_*_seed*/metrics.json (all seeds).
Writes results/social_summary/summary.csv   one row per model and horizon (test metrics; sequence models averaged over seeds)
       results/social_summary/lead_time.png PR-AUC lift and F1 against forecast horizon
Log    logs/social_summary/social_summary_<runid>.log

Usage:
    python scripts/social_summary.py
"""
import argparse
import glob
import json
from pathlib import Path

import pandas as pd

from common import RESULTS_DIR, log_environment, setup_logger, write_json


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("social_summary", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== SOCIAL SUMMARY started (run %s) ===", run_id)
    rows = []

    base = sorted(glob.glob(str(args.results_dir / "social_baselines_*" / "metrics.json")))
    if base:
        logger.info("baseline run: %s", Path(base[-1]).parent.name)
        for key, v in json.loads(Path(base[-1]).read_text())["metrics"].items():
            name, h = key.rsplit("_h", 1)
            t = v["test"]
            rows.append({"model": name, "horizon": int(h), "seed": 0, **{k: t[k] for k in (
                "f1", "precision", "recall", "pr_auc", "pr_auc_lift_over_prevalence", "roc_auc", "prevalence", "n", "positives")}})
    for f in sorted(glob.glob(str(args.results_dir / "social_*_seed*" / "metrics.json"))):
        j = json.loads(Path(f).read_text())
        for key, v in j["metrics"].items():
            h = int(key.rsplit("_h", 1)[1])
            t = v["test"]
            rows.append({"model": key.rsplit("_h", 1)[0], "horizon": h, "seed": int(j["args"]["seed"]),
                         **{k: t[k] for k in ("f1", "precision", "recall", "pr_auc", "pr_auc_lift_over_prevalence",
                                              "roc_auc", "prevalence", "n", "positives")}})
    if not rows:
        logger.error("no results found under %s", args.results_dir)
        raise SystemExit(1)

    df = pd.DataFrame(rows)
    metric_cols = ["f1", "precision", "recall", "pr_auc", "pr_auc_lift_over_prevalence", "roc_auc"]
    g = df.groupby(["model", "horizon"])
    summary = g[metric_cols].mean().round(4).add_suffix("_mean")
    summary = summary.join(g[["f1", "pr_auc"]].std().round(4).add_suffix("_std"))
    summary["seeds"] = g["seed"].nunique()
    summary["prevalence"] = g["prevalence"].first().round(4)
    summary = summary.reset_index().sort_values(["horizon", "pr_auc_mean"], ascending=[True, False])
    out = args.results_dir / "social_summary"
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "summary.csv", index=False)
    for h, sub in summary.groupby("horizon"):
        logger.info("--- h=%d days ahead (test)", h)
        for _, r in sub.iterrows():
            logger.info("  %-18s F1=%.3f PR-AUC=%.3f (x%.1f over base %.3f) ROC-AUC=%.3f seeds=%d", r["model"], r["f1_mean"],
                        r["pr_auc_mean"], r["pr_auc_lift_over_prevalence_mean"], r["prevalence"], r["roc_auc_mean"], r["seeds"])

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    for model, sub in summary[summary["model"] != "prevalence"].groupby("model"):
        sub = sub.sort_values("horizon")
        ax[0].plot(sub["horizon"], sub["pr_auc_lift_over_prevalence_mean"], marker="o", label=model)
        ax[1].plot(sub["horizon"], sub["f1_mean"], marker="o", label=model)
    ax[0].axhline(1, color="grey", ls=":")
    ax[0].set_title("PR-AUC lift over base rate (test)")
    ax[1].set_title("F1 of the surge class (test)")
    for a in ax:
        a.set_xlabel("forecast horizon (days ahead)")
        a.set_xticks(sorted(summary["horizon"].unique()))
        a.grid(alpha=0.25)
    ax[1].legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "lead_time.png", dpi=150)
    write_json(log_path.with_name(log_path.stem + "_summary.json"), {"run_id": run_id, "rows": len(summary), "output": str(out)})
    logger.info("Wrote %s and %s", out / "summary.csv", out / "lead_time.png")
    logger.info("=== SOCIAL SUMMARY finished successfully ===")


if __name__ == "__main__":
    main()
