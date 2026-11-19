"""Test the tweet violence models on your freshly labelled unseen sample.

Thresholds are fixed BEFORE looking at the unseen labels: (a) 0.5, (b) the threshold tuned on silver validation data and (c) the
threshold tuned on the gold sample. Metrics are weighted back to the whole tweet population (stratum weights from farmers_make_unseen.py)
with 95% stratified bootstrap intervals.

Models: keyword rule; TF-IDF logistic regression (retrained from silver_train in about 30 s); DistilRoBERTa from models/farmers_transformer_*
(the latest, or --transformer-dir). Use --skip-transformer without PyTorch.

Inputs   data/farmers_unseen/unseen_to_label.csv (your labels), unseen_key.csv
Outputs  results/farmers_unseen_<runid>/{metrics.json, summary.md, predictions.csv}
Log      logs/farmers_eval_unseen/

Usage:  python scripts/farmers_eval_unseen.py
        python scripts/farmers_eval_unseen.py --skip-transformer
"""
import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA_DIR, MODELS_DIR, RESULTS_DIR, log_environment, setup_logger, timed_step, write_json
from farmers_common import LABELS_DIR, best_f1_threshold, clean_text, gold_bootstrap, gold_metrics, load_gold, rule_flags
from farmers_train_tweets import tfidf_fit_predict


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--labels", type=Path, default=DATA_DIR / "farmers_unseen" / "unseen_to_label.csv")
    p.add_argument("--key", type=Path, default=DATA_DIR / "farmers_unseen" / "unseen_key.csv")
    p.add_argument("--transformer-dir", type=Path, default=None)
    p.add_argument("--skip-transformer", action="store_true")
    p.add_argument("--max-length", type=int, default=64)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def load_unseen(args, logger):
    s = pd.read_csv(args.labels, encoding="utf-8-sig")
    lab = pd.to_numeric(s["label"], errors="coerce")
    n_all = len(s)
    s = s[lab.isin([0, 1])].copy()
    s["label"] = lab[lab.isin([0, 1])].astype(int)
    key = pd.read_csv(args.key, dtype={"tweet_id": str})
    s = s.merge(key, on="row_id", how="left")
    s["clean"] = s["text"].astype(str).map(clean_text)
    logger.info("labelled %d of %d rows; %d violent, %d not", len(s), n_all, int(s["label"].sum()), int((1 - s["label"]).sum()))
    if len(s) < 100 or s["label"].sum() < 20:
        logger.warning("few labels (%d rows, %d violent): intervals will be very wide; label more tweets if you can", len(s), int(s["label"].sum()))
    return s


def evaluate(name, y, score, w, strata, thresholds, logger):
    out = {}
    for tname, thr in thresholds.items():
        m = gold_metrics(y, score, w, thr)
        m["ci95"] = gold_bootstrap(y, score, w, strata, thr)
        out[tname] = m
        logger.info("%-12s %-22s thr %.3f | weighted P %.3f R %.3f F1 %.3f (CI %s) PR-AUC %.3f", name, tname, thr, m["precision"], m["recall"], m["f1"],
                    m["ci95"]["f1"], m["pr_auc"])
    return out


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_eval_unseen", log_dir=args.log_dir)
    log_environment(logger, args)
    un = load_unseen(args, logger)
    gold = load_gold(LABELS_DIR / "gold_claude.csv")
    out_dir = RESULTS_DIR / f"farmers_unseen_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    y, w, strata = un["label"].values, un["weight"].values, un["stratum"].values
    metrics = {"run_id": run_id, "n_labelled": int(len(un)), "n_violent": int(y.sum()), "models": {}}
    preds = un[["row_id", "tweet_id", "stratum", "weight", "label", "text"]].copy()

    rule = rule_flags(un["clean"])["strong"].astype(float).values
    metrics["models"]["rule"] = evaluate("rule", y, rule, w, strata, {"rule": 0.5}, logger)

    with timed_step(logger, "tfidf (retrain from silver_train)"):
        silver = {k: pd.read_csv(LABELS_DIR / f"silver_{k}.csv", dtype={"tweet_id": str}) for k in ("train", "val", "test")}
        for d in silver.values():
            d["clean"] = d["text"].astype(str).map(clean_text)
        both = pd.concat([gold[["clean"]], un[["clean"]]], ignore_index=True)
        res = tfidf_fit_predict(silver["train"], silver["val"], silver["test"].head(10), both, args, logger)
        p_gold, p_un = res["gold"][:len(gold)], res["gold"][len(gold):]
        thr = {"threshold_0.5": 0.5, "silver_tuned": best_f1_threshold(silver["val"]["label"].values, res["val"]),
               "gold_tuned": best_f1_threshold(gold["label"].values, p_gold, gold["weight"].values)}
        metrics["models"]["tfidf"] = evaluate("tfidf", y, p_un, w, strata, thr, logger)
        preds["p_tfidf"] = p_un

    if not args.skip_transformer:
        mdir = args.transformer_dir
        if mdir is None:
            cands = sorted(p for p in glob.glob(str(MODELS_DIR / "farmers_transformer_*")) if (Path(p) / "best").exists())
            mdir = Path(cands[-1]) if cands else None
        if mdir is None:
            logger.warning("no transformer model found in models/; skipping (train one with farmers_train_tweets.py --model transformer)")
        else:
            import json
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            cfg = json.loads((mdir / "run_config.json").read_text())
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            tok = AutoTokenizer.from_pretrained(mdir / "best")
            model = AutoModelForSequenceClassification.from_pretrained(mdir / "best").to(dev).eval()
            maxlen = int(cfg.get("max_length", args.max_length))

            @torch.no_grad()
            def predict(texts):
                texts = list(texts)
                order = np.argsort([len(t) for t in texts])
                probs = np.zeros(len(texts))
                for i in range(0, len(order), 128):
                    idx = order[i:i + 128]
                    enc = tok([texts[j] for j in idx], truncation=True, max_length=maxlen, padding=True, return_tensors="pt").to(dev)
                    probs[idx] = torch.softmax(model(**enc).logits.float(), dim=-1)[:, 1].cpu().numpy()
                return probs

            with timed_step(logger, f"transformer {mdir.name}"):
                pg, pu = predict(gold["clean"]), predict(un["clean"])
                thr = {"threshold_0.5": 0.5, "silver_tuned": float(cfg["threshold"]),
                       "gold_tuned": best_f1_threshold(gold["label"].values, pg, gold["weight"].values)}
                metrics["models"]["transformer"] = evaluate("transformer", y, pu, w, strata, thr, logger)
                metrics["transformer_dir"] = mdir.name
                preds["p_transformer"] = pu

    preds.to_csv(out_dir / "predictions.csv", index=False, encoding="utf-8-sig")
    write_json(out_dir / "metrics.json", metrics)
    md = [f"# Unseen-data test ({metrics['n_labelled']} labelled tweets, {metrics['n_violent']} violent)", "",
          "Weighted to the whole tweet population; thresholds were fixed before looking at these labels.", "",
          "| model | threshold | precision | recall | F1 | F1 95% CI | PR-AUC |", "|---|---|---|---|---|---|---|"]
    for mname, d in metrics["models"].items():
        for tname, m in d.items():
            md.append(f"| {mname} | {tname} ({m['threshold']}) | {m['precision']} | {m['recall']} | {m['f1']} | {m['ci95']['f1']} | {m['pr_auc']} |")
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    logger.info("Wrote %s", out_dir)
    print("\n".join(md))
    print(f"\nDone. Open {out_dir / 'summary.md'}")


if __name__ == "__main__":
    main()
