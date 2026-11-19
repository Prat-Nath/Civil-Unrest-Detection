"""Run the whole #FarmersProtest pipeline, step by step, from the project root.

Steps (in order)
  prepare      farmers_prepare.py       daily tweet table + ACLED daily table + cleaned text   (skipped if outputs exist, unless --force)
  label        farmers_label_weak.py    weak labels (train/val/test) and rule score on the gold sample
  tfidf        farmers_train_tweets.py  TF-IDF baseline, daily signals
  transformer  farmers_train_tweets.py  DistilRoBERTa fine-tuning (GPU recommended), daily signals
  extract      farmers_extract.py       event types and places vs ACLED
  transfer     farmers_transfer.py      ACLED-trained model on tweets + evaluation on the gold sample
  forecast     farmers_forecast.py      same-day detection and 1/3/7-day surge forecasts (uses the best available daily signals)
  goldthr      farmers_gold_threshold.py  re-tune each classifier's threshold on part of the gold sample (cross-validated)
  summary      farmers_summary.py       one report: results/farmers_summary/summary.md

Usage
    python scripts/run_farmers_pipeline.py                                # everything
    python scripts/run_farmers_pipeline.py --steps label,tfidf,extract    # chosen steps
    python scripts/run_farmers_pipeline.py --skip-steps transformer,transfer      # no torch / no GPU
    python scripts/run_farmers_pipeline.py --base-model cardiffnlp/twitter-roberta-base --epochs 3
    python scripts/run_farmers_pipeline.py --acled-model-dir models/<your ACLED DistilRoBERTa run>
The log goes to logs/run_farmers_pipeline/. Each step also writes its own log in logs/<script name>/.
"""
import argparse
import glob
import subprocess
import sys
import time
from pathlib import Path

from common import DATA_DIR, MODELS_DIR, PROJECT_ROOT, RESULTS_DIR, log_environment, setup_logger

STEPS = ["prepare", "label", "tfidf", "transformer", "extract", "transfer", "forecast", "goldthr", "summary"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--steps", default=",".join(STEPS))
    p.add_argument("--skip-steps", default="")
    p.add_argument("--force", action="store_true", help="re-run prepare even if its outputs exist")
    p.add_argument("--keep-going", action="store_true", help="continue after a failed step")
    p.add_argument("--base-model", default="distilroberta-base")
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--cue-dropout", type=float, default=0.5)
    p.add_argument("--max-train", type=int, default=0, help="subsample the training set (quick test)")
    p.add_argument("--max-eval", type=int, default=0, help="subsample silver val/test (quick test)")
    p.add_argument("--per-day", type=int, default=300, help="tweets scored per day for the daily signals")
    p.add_argument("--acled-model-dir", type=Path, default=None, help="ACLED-trained model for the transfer step (default: newest distilroberta-base_binary_* without 'masked')")
    return p.parse_args()


def newest(pattern, must_exist=None):
    paths = sorted(glob.glob(str(pattern)))
    if must_exist:
        paths = [p for p in paths if Path(p, must_exist).exists()]
    return Path(paths[-1]) if paths else None


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("run_farmers_pipeline")
    log_environment(logger, args)
    steps = [s for s in args.steps.split(",") if s and s not in args.skip_steps.split(",")]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        raise SystemExit(f"unknown steps {bad}; choose from {STEPS}")
    py, sc = sys.executable, PROJECT_ROOT / "scripts"
    results = {}

    def run(name, cmd):
        logger.info("=== STEP %s: %s", name, " ".join(str(c) for c in cmd))
        t0 = time.time()
        rc = subprocess.call([str(c) for c in cmd], cwd=PROJECT_ROOT)
        results[name] = (rc, round(time.time() - t0))
        logger.info("=== STEP %s %s in %ds", name, "OK" if rc == 0 else f"FAILED (exit {rc})", time.time() - t0)
        if rc != 0 and not args.keep_going:
            raise SystemExit(f"step {name} failed; see its log (logs/) for the error")
        return rc

    for step in steps:
        if step == "prepare":
            have = all((DATA_DIR / "farmers_processed" / f).exists() for f in ("tweets_daily.csv", "acled_daily.csv", "tweets_text.csv.gz"))
            if have and not args.force:
                logger.info("=== STEP prepare skipped (outputs exist; use --force to rebuild)")
                results["prepare"] = (0, 0)
            else:
                run("prepare", [py, sc / "farmers_prepare.py"])
        elif step == "label":
            run("label", [py, sc / "farmers_label_weak.py"])
        elif step in ("tfidf", "transformer"):
            cmd = [py, sc / "farmers_train_tweets.py", "--model", step, "--score-daily", "--per-day", args.per_day, "--epochs", args.epochs,
                   "--batch-size", args.batch_size, "--cue-dropout", args.cue_dropout, "--base-model", args.base_model]
            if args.max_train:
                cmd += ["--max-train", args.max_train]
            if args.max_eval:
                cmd += ["--max-eval", args.max_eval]
            run(step, cmd)
        elif step == "extract":
            run("extract", [py, sc / "farmers_extract.py"])
        elif step == "transfer":
            mdir = args.acled_model_dir
            if mdir is None:
                cands = [p for p in sorted(glob.glob(str(MODELS_DIR / "distilroberta-base_binary_*"))) if "masked" not in p and Path(p, "best").exists()]
                mdir = Path(cands[-1]) if cands else None
            if mdir is None:
                logger.warning("transfer skipped: no ACLED-trained DistilRoBERTa found in models/ (pass --acled-model-dir)")
                continue
            if run("transfer", [py, sc / "farmers_transfer.py", "--model-dir", mdir]) == 0:
                tr = newest(RESULTS_DIR / "farmers_transfer_*", "scores.csv")
                run("transfer_eval", [py, sc / "farmers_transfer_eval.py", "--run", tr, "--labels", DATA_DIR / "farmers_labels" / "gold_claude.csv"])
        elif step == "forecast":
            sig = None
            for model in ("transformer", "tfidf"):
                f = newest(RESULTS_DIR / f"farmers_tweets_*" / f"daily_signals_{model}.csv")
                if f:
                    sig = f
                    break
            run("forecast", [py, sc / "farmers_forecast.py"] + (["--signals", sig] if sig else []))
        elif step == "goldthr":
            run("goldthr", [py, sc / "farmers_gold_threshold.py"])
        elif step == "summary":
            run("summary", [py, sc / "farmers_summary.py"])
    logger.info("pipeline finished: %s", {k: ("ok" if v[0] == 0 else "FAILED") + f" {v[1]}s" for k, v in results.items()})
    print("\nDone. Open results/farmers_summary/summary.md")


if __name__ == "__main__":
    main()
