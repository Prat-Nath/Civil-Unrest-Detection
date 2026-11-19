"""Step 3 - evaluate a saved model (baseline or transformer) on a split, with breakdowns and error analysis.

Usage:
  python scripts/evaluate.py --model-dir models/<run_name>                 # test split, default decision rule
  python scripts/evaluate.py --model-dir models/<run_name> --split val
  python scripts/evaluate.py --model-dir models/<run_name> --tune-threshold  # threshold tuned on VAL, applied to the split

Outputs (results/<run_name>/eval_<split>/):
  metrics.json, predictions.csv, breakdown_by_year.csv, breakdown_by_state.csv,
  errors_false_positives.csv, errors_false_negatives.csv, confusion_matrix.png
Log: logs/evaluate/evaluate_<run_name>_<split>_<timestamp>.log
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common import (MODELS_DIR, PROCESSED_DIR, RESULTS_DIR, log_environment, mask_cues, new_run_id, setup_logger,
                    timed_step, write_json)
from metrics_utils import LABEL_COLUMN, best_f1_threshold, class_names, compute_metrics, format_metrics, predict_labels


def parse_args():
    p = argparse.ArgumentParser(description="Evaluate a saved model.")
    p.add_argument("--model-dir", type=Path, required=True, help="models/<run_name> folder")
    p.add_argument("--split", choices=["train", "val", "test"], default="test")
    p.add_argument("--data-dir", type=Path, default=PROCESSED_DIR)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--threshold", type=float, default=None, help="binary only: decision threshold on P(violent)")
    p.add_argument("--tune-threshold", action="store_true", help="binary only: tune threshold on the val split")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-samples", type=int, default=None)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def predict_probs(model_dir: Path, config: dict, texts, batch_size, log):
    if config["model_type"] == "baseline":
        import joblib
        bundle = joblib.load(model_dir / "baseline.joblib")
        return bundle["model"].predict_proba(bundle["vectorizer"].transform(texts))

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    ckpt = model_dir / "best" if (model_dir / "best").exists() else model_dir
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Loading transformer from %s on %s", ckpt, device)
    tok = AutoTokenizer.from_pretrained(ckpt)
    model = AutoModelForSequenceClassification.from_pretrained(ckpt).to(device).eval()
    out = []
    order = np.argsort([len(t) for t in texts])           # length-sorted batches = faster inference
    with torch.no_grad():
        for s in range(0, len(texts), batch_size):
            idx = order[s:s + batch_size]
            enc = tok([texts[i] for i in idx], truncation=True, max_length=config["max_length"],
                      padding=True, return_tensors="pt").to(device)
            out.append(torch.softmax(model(**enc).logits.float(), -1).cpu().numpy())
    probs = np.concatenate(out)
    result = np.zeros_like(probs)
    result[order] = probs                                  # restore original row order
    return result


def prepare_texts(df, config):
    texts = df["text"].fillna("").astype(str)
    if config.get("mask_cues"):
        repl = "MASKED"
        if config["model_type"] == "transformer":
            from transformers import AutoTokenizer
            ckpt = Path(config["_model_dir"]) / "best"
            repl = AutoTokenizer.from_pretrained(ckpt).mask_token or "[MASK]"
        texts = texts.map(lambda t: mask_cues(t, repl))
    return texts.tolist()


def group_breakdown(df, task, probs, thr, key, min_n=30):
    rows = []
    pos = 1 if task == "binary" else 2
    y_pred_all = predict_labels(probs, task, thr)
    for k, idx in df.groupby(key).groups.items():
        pos_idx = df.index.get_indexer(idx)
        yt = (df.iloc[pos_idx]["y_true"].to_numpy() == pos)
        yp = (y_pred_all[pos_idx] == pos)
        if len(pos_idx) < min_n:
            continue
        tp = (yt & yp).sum()
        rows.append({key: k, "n": len(pos_idx), "violent_rate": yt.mean(),
                     "precision": tp / max(yp.sum(), 1), "recall": tp / max(yt.sum(), 1),
                     "f1": 2 * tp / max(yt.sum() + yp.sum(), 1)})
    return pd.DataFrame(rows).sort_values("n", ascending=False)


def main():
    args = parse_args()
    run_id = new_run_id()
    model_dir = args.model_dir.resolve()
    config = json.loads((model_dir / "run_config.json").read_text(encoding="utf-8"))
    config["_model_dir"] = str(model_dir)
    run_name = config["run_name"]
    task = config["task"]
    log, log_path, _ = setup_logger("evaluate", run_id=run_id, log_dir=args.log_dir,
                                    name=f"evaluate_{run_name}_{args.split}_{run_id}")
    log.info("=== EVALUATE run_name=%s split=%s ===", run_name, args.split)
    log.info("Log file: %s", log_path)
    log_environment(log, args)
    log.info("Model config: %s", {k: v for k, v in config.items() if k != "args"})

    try:
        label_col = LABEL_COLUMN[task]
        names = class_names(task)
        with timed_step(log, "load data + predict"):
            df = pd.read_csv(args.data_dir / f"{args.split}.csv", encoding="utf-8")
            if args.max_samples and len(df) > args.max_samples:
                df = df.sample(args.max_samples, random_state=args.seed).sort_values("event_date").reset_index(drop=True)
            df["y_true"] = df[label_col].to_numpy()
            probs = predict_probs(model_dir, config, prepare_texts(df, config), args.batch_size, log)
            log.info("Predicted %d rows", len(df))

        thr = args.threshold
        if task == "binary":
            if args.tune_threshold:
                with timed_step(log, "tune threshold on val"):
                    va = pd.read_csv(args.data_dir / "val.csv", encoding="utf-8")
                    if args.max_samples and len(va) > args.max_samples:
                        va = va.sample(args.max_samples, random_state=args.seed).reset_index(drop=True)
                    pv = predict_probs(model_dir, config, prepare_texts(va, config), args.batch_size, log)
                    thr = best_f1_threshold(va[label_col].to_numpy(), pv[:, 1])
                    log.info("Tuned threshold on validation: %.3f", thr)
            log.info("Decision threshold used: %s", 0.5 if thr is None else thr)

        with timed_step(log, "metrics + breakdowns"):
            m = compute_metrics(df["y_true"], probs, task, threshold=thr)
            log.info("\n%s", format_metrics(m, f"{args.split.upper()} - {run_name}"))
            out = args.results_dir / run_name / f"eval_{args.split}"
            out.mkdir(parents=True, exist_ok=True)
            write_json(out / "metrics.json", m)

            df["y_pred"] = predict_labels(probs, task, thr)
            for i, n in enumerate(names):
                df[f"p_{n}"] = probs[:, i]
            pos = 1 if task == "binary" else 2
            df["p_violent"] = probs[:, pos]
            keep = ["event_id_cnty", "event_date", "admin1", "y_true", "y_pred", "p_violent", "text"]
            df[keep].to_csv(out / "predictions.csv", index=False)

            df["year"] = pd.to_datetime(df["event_date"]).dt.year
            by_year = group_breakdown(df, task, probs, thr, "year", min_n=1)
            by_state = group_breakdown(df, task, probs, thr, "admin1", min_n=50).head(20)
            by_year.to_csv(out / "breakdown_by_year.csv", index=False)
            by_state.to_csv(out / "breakdown_by_state.csv", index=False)
            log.info("Breakdown by year:\n%s", by_year.round(3).to_string(index=False))
            log.info("Breakdown by state (top 20 by size):\n%s", by_state.round(3).to_string(index=False))

            fp = df[(df.y_pred == pos) & (df.y_true != pos)].sort_values("p_violent", ascending=False).head(100)
            fn = df[(df.y_pred != pos) & (df.y_true == pos)].sort_values("p_violent").head(100)
            fp[keep].to_csv(out / "errors_false_positives.csv", index=False)
            fn[keep].to_csv(out / "errors_false_negatives.csv", index=False)
            log.info("Saved %d false-positive and %d false-negative examples for error analysis", len(fp), len(fn))
            for t in fn.text.head(3):
                log.info("Missed violent example: %s", t[:220])
            for t in fp.text.head(3):
                log.info("False alarm example   : %s", t[:220])

            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                cm = np.array(m["confusion_matrix"])
                fig, ax = plt.subplots(figsize=(4.5, 4))
                ax.imshow(cm, cmap="Blues")
                ax.set_xticks(range(len(names)), names, rotation=30)
                ax.set_yticks(range(len(names)), names)
                ax.set_xlabel("predicted"); ax.set_ylabel("true"); ax.set_title(f"{args.split} confusion matrix")
                for (i, j), v in np.ndenumerate(cm):
                    ax.text(j, i, str(v), ha="center", va="center", color="white" if v > cm.max() / 2 else "black")
                fig.tight_layout()
                fig.savefig(out / "confusion_matrix.png", dpi=150)
                plt.close(fig)
            except Exception as e:  # plotting must never break evaluation
                log.warning("Could not draw confusion matrix: %s", e)
            log.info("Results -> %s", out)
        log.info("=== EVALUATE finished successfully ===")
        return 0
    except Exception as exc:
        log.exception("EVALUATE aborted: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
