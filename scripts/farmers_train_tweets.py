"""Train and evaluate tweet-level violence classifiers on the weak (silver) labels, judged on the gold sample.

Models   tfidf         TF-IDF (1-2 grams) + class-balanced logistic regression
         transformer   DistilRoBERTa fine-tuned on tweets (needs torch; GPU strongly recommended, CPU is slow)
Training data   data/farmers_labels/silver_train.csv (weak labels from rules, chronological split)
Selection       decision threshold tuned for F1 on silver_val
Evaluation      silver_val / silver_test (agreement with the rules - NOT the truth) and, the number that matters, the gold
                sample (300 tweets labelled by Claude, stratum-weighted so it estimates the whole tweet population, with
                95% bootstrap intervals). The rule itself is reported on gold as the baseline to beat.
--cue-dropout  during training, strong violence words are replaced by a mask token with this probability, so the model has
               to use context instead of only memorising the rule words (helps it find violence the rules miss).
--score-daily  also score a per-day sample of all tweets and write results/<run>/daily_signals.csv for scripts/farmers_forecast.py

Outputs  results/farmers_tweets_<runid>/{metrics.json, gold_predictions.csv, silver_test_predictions.csv, daily_signals_<model>.csv}
         models/farmers_<model>_<runid>/ (best/, run_config.json)
Log      logs/farmers_train_tweets/

Usage:
    python scripts/farmers_train_tweets.py --model tfidf
    python scripts/farmers_train_tweets.py --model transformer --epochs 2 --cue-dropout 0.5 --score-daily
    python scripts/farmers_train_tweets.py --model both --max-train 4000 --epochs 1        # quick CPU test
"""
import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from common import MODELS_DIR, RESULTS_DIR, log_environment, set_seed, setup_logger, timed_step, write_json
from farmers_common import (FARMERS_DIR, LABELS_DIR, STRONG_RE, best_f1_threshold, clean_text, gold_bootstrap, gold_metrics, load_gold,
                            rule_flags, weighted_prf)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", choices=["tfidf", "transformer", "both"], default="both")
    p.add_argument("--base-model", default="distilroberta-base")
    p.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
    p.add_argument("--max-train", type=int, default=0, help="subsample the training set (0 = all)")
    p.add_argument("--max-eval", type=int, default=0, help="subsample silver val/test (0 = all); for quick CPU tests")
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--max-length", type=int, default=64)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--cue-dropout", type=float, default=0.5)
    p.add_argument("--score-daily", action="store_true")
    p.add_argument("--per-day", type=int, default=300, help="tweets scored per day for the daily signals")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def eval_block(y, p, thr):
    """Unweighted precision / recall / F1 / PR-AUC / ROC-AUC (used for the silver splits)."""
    from sklearn.metrics import average_precision_score, roc_auc_score
    pred = (p >= thr).astype(int)
    m = weighted_prf(y, pred, np.ones(len(y)))
    m.update({"pr_auc": round(float(average_precision_score(y, p)), 4), "roc_auc": round(float(roc_auc_score(y, p)), 4), "n": int(len(y)),
              "positive_rate": round(float(np.mean(y)), 4), "threshold": round(float(thr), 4)})
    return m


def mask_cues_train(texts, rate, rng, mask_token):
    out = []
    for t in texts:
        out.append(STRONG_RE.sub(mask_token, t) if rng.random() < rate else t)
    return out


def tfidf_fit_predict(train, val, test, gold, args, logger):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=200000, sublinear_tf=True, lowercase=True)
    Xtr = vec.fit_transform(train["clean"])
    clf = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000).fit(Xtr, train["label"])
    pred = lambda df: clf.predict_proba(vec.transform(df["clean"]))[:, 1]
    names = np.array(vec.get_feature_names_out())
    top = names[np.argsort(clf.coef_[0])[::-1][:25]].tolist()
    logger.info("top tf-idf terms: %s", top)
    return {"val": pred(val), "test": pred(test), "gold": pred(gold), "top_terms": top, "daily_fn": lambda s: clf.predict_proba(vec.transform(s))[:, 1], "save": None}


def transformer_fit_predict(train, val, test, gold, args, logger, run_id):
    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("device: %s%s", dev, "" if dev == "cuda" else "  (CPU: this will be slow; use --max-train for a test)")
    tok = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModelForSequenceClassification.from_pretrained(args.base_model, num_labels=2).to(dev)
    rng = np.random.default_rng(args.seed)
    mask_token = tok.mask_token or "[MASK]"

    def encode(texts):
        return tok(list(texts), truncation=True, max_length=args.max_length)

    def collate(batch):
        feats = [{k: b[k] for k in ("input_ids", "attention_mask")} for b in batch]
        enc = tok.pad(feats, return_tensors="pt")
        enc["labels"] = torch.tensor([b["labels"] for b in batch])
        return enc

    @torch.no_grad()
    def predict(texts):
        model.eval()
        texts = list(texts)
        order = np.argsort([len(t) for t in texts])
        probs = np.zeros(len(texts))
        for i in range(0, len(order), 128):
            idx = order[i:i + 128]
            enc = tok([texts[j] for j in idx], truncation=True, max_length=args.max_length, padding=True, return_tensors="pt").to(dev)
            probs[idx] = torch.softmax(model(**enc).logits.float(), dim=-1)[:, 1].cpu().numpy()
        return probs

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    steps = args.epochs * int(np.ceil(len(train) / args.batch_size))
    sched = get_linear_schedule_with_warmup(opt, int(0.06 * steps), steps)
    scaler = torch.amp.GradScaler("cuda", enabled=(dev == "cuda"))
    best_f1, best_state, history, t0 = -1.0, None, [], time.time()
    for epoch in range(1, args.epochs + 1):
        model.train()
        texts = mask_cues_train(train["clean"].tolist(), args.cue_dropout, rng, mask_token)  # fresh masking each epoch
        enc = encode(texts)
        ds = [{"input_ids": enc["input_ids"][i], "attention_mask": enc["attention_mask"][i], "labels": int(train["label"].iloc[i])} for i in range(len(texts))]
        loader = DataLoader(ds, batch_size=args.batch_size, shuffle=True, collate_fn=collate)
        total, n = 0.0, 0
        for step, batch in enumerate(loader):
            batch = {k: v.to(dev) for k, v in batch.items()}
            with torch.autocast(device_type=dev, dtype=torch.float16, enabled=(dev == "cuda")):
                loss = model(**batch).loss
            opt.zero_grad()
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt); scaler.update(); sched.step()
            total += loss.item(); n += 1
            if step % 100 == 0:
                logger.info("epoch %d step %d/%d loss %.4f (%.0fs)", epoch, step, len(loader), total / n, time.time() - t0)
        pv = predict(val["clean"])
        thr = best_f1_threshold(val["label"].values, pv)
        f1 = eval_block(val["label"].values, pv, thr)["f1"]
        history.append({"epoch": epoch, "train_loss": round(total / max(n, 1), 4), "val_f1": f1, "val_threshold": round(thr, 4)})
        logger.info("epoch %d done | train loss %.4f | val F1 %.4f (thr %.3f)", epoch, total / max(n, 1), f1, thr)
        if f1 > best_f1:
            best_f1 = f1
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            best_epoch = epoch
    model.load_state_dict(best_state)
    logger.info("best epoch %d (val F1 %.4f)", best_epoch, best_f1)

    def save(path: Path, thr: float):
        (path / "best").mkdir(parents=True, exist_ok=True)
        model.save_pretrained(path / "best")
        tok.save_pretrained(path / "best")
        write_json(path / "run_config.json", {"model_type": "transformer", "base_model": args.base_model, "task": "binary", "mask_cues": False,
                                              "cue_dropout": args.cue_dropout, "max_length": args.max_length, "label_names": ["non_violent", "violent"],
                                              "threshold": thr, "best_epoch": best_epoch, "history": history, "args": {k: str(v) for k, v in vars(args).items()}})
    return {"val": predict(val["clean"]), "test": predict(test["clean"]), "gold": predict(gold["clean"]), "history": history, "daily_fn": predict, "save": save}


def score_daily(fn, tweets_path, per_day, seed, logger):
    tw = pd.read_csv(tweets_path, usecols=["tweet_id", "date", "text"], dtype={"tweet_id": str})
    dt = pd.to_datetime(tw["date"], utc=True, format="ISO8601", errors="coerce")
    tw = tw[dt.notna()].copy()
    tw["day"] = dt[dt.notna()].dt.tz_localize(None).dt.normalize()
    tw = tw.drop_duplicates("tweet_id").sample(frac=1, random_state=seed).groupby("day").head(per_day).copy()
    tw["clean"] = tw["text"].astype(str).map(clean_text)
    tw = tw[tw["clean"].str.len() >= 15]
    logger.info("scoring %d tweets (<= %d per day, %d days)", len(tw), per_day, tw["day"].nunique())
    tw["p"] = fn(tw["clean"])
    return tw[["tweet_id", "day", "p"]]


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("farmers_train_tweets", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== FARMERS TRAIN TWEETS started (run %s) ===", run_id)
    set_seed(args.seed)
    out_dir = RESULTS_DIR / f"farmers_tweets_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with timed_step(logger, "load data"):
        sets = {}
        for s in ("train", "val", "test"):
            d = pd.read_csv(args.labels_dir / f"silver_{s}.csv", dtype={"tweet_id": str})
            d["clean"] = d["text"].map(clean_text)
            sets[s] = d
        if args.max_train and len(sets["train"]) > args.max_train:
            sets["train"] = sets["train"].sample(args.max_train, random_state=args.seed)
        if args.max_eval:
            for k in ("val", "test"):
                if len(sets[k]) > args.max_eval:
                    sets[k] = sets[k].sample(args.max_eval, random_state=args.seed)
        gold = load_gold(args.labels_dir / "gold_claude.csv")
        logger.info("train %d (%.1f%% violent) | val %d | test %d | gold %d (%d violent by Claude's labels)", len(sets["train"]),
                    100 * sets["train"]["label"].mean(), len(sets["val"]), len(sets["test"]), len(gold), gold["label"].sum())

    metrics = {"run_id": run_id, "note": "silver = agreement with weak rules; gold = Claude-labelled sample, stratum-weighted, 95% bootstrap intervals"}
    fl = rule_flags(gold["text"])
    rule_score = fl["strong"].astype(float).values
    metrics["rule_baseline_gold"] = gold_metrics(gold["label"], rule_score, gold["weight"], 0.5)
    metrics["rule_baseline_gold"]["ci95"] = gold_bootstrap(gold["label"], rule_score, gold["weight"], gold["stratum"], 0.5)
    logger.info("rule baseline on gold: %s", metrics["rule_baseline_gold"])

    runs = ["tfidf", "transformer"] if args.model == "both" else [args.model]
    for name in runs:
        with timed_step(logger, f"train + evaluate {name}"):
            if name == "tfidf":
                res = tfidf_fit_predict(sets["train"], sets["val"], sets["test"], gold, args, logger)
            else:
                res = transformer_fit_predict(sets["train"], sets["val"], sets["test"], gold, args, logger, run_id)
            thr = best_f1_threshold(sets["val"]["label"].values, res["val"])
            m = {"threshold_from_silver_val": round(thr, 4),
                 "silver_val": eval_block(sets["val"]["label"].values, res["val"], thr),
                 "silver_test": eval_block(sets["test"]["label"].values, res["test"], thr),
                 "gold": gold_metrics(gold["label"], res["gold"], gold["weight"], thr)}
            m["gold"]["ci95"] = gold_bootstrap(gold["label"], res["gold"], gold["weight"], gold["stratum"], thr)
            m["gold_at_0.5"] = gold_metrics(gold["label"], res["gold"], gold["weight"], 0.5)
            m["gold_unweighted_precision_recall_f1"] = weighted_prf(gold["label"], (res["gold"] >= thr).astype(int), np.ones(len(gold)))
            for k in ("top_terms", "history"):
                if k in res:
                    m[k] = res[k]
            metrics[name] = m
            logger.info("%s | silver test F1 %.3f | GOLD weighted P %.3f R %.3f F1 %.3f (CI %s) PR-AUC %.3f", name, m["silver_test"]["f1"], m["gold"]["precision"],
                        m["gold"]["recall"], m["gold"]["f1"], m["gold"]["ci95"]["f1"], m["gold"]["pr_auc"])
            pd.DataFrame({"tweet_id": gold["tweet_id"], "label": gold["label"], "weight": gold["weight"], "stratum": gold["stratum"],
                          "p": res["gold"], "text": gold["text"].str[:200]}).to_csv(out_dir / f"gold_predictions_{name}.csv", index=False)
            pd.DataFrame({"tweet_id": sets["test"]["tweet_id"], "label": sets["test"]["label"], "p": res["test"]}).to_csv(out_dir / f"silver_test_predictions_{name}.csv", index=False)
            if res.get("save"):
                mdir = MODELS_DIR / f"farmers_{name}_{run_id}"
                res["save"](mdir, thr)
                logger.info("model saved to %s", mdir)
            if args.score_daily:
                with timed_step(logger, f"score daily sample ({name})"):
                    sc = score_daily(res["daily_fn"], FARMERS_DIR / "tweets_text.csv.gz", args.per_day, args.seed, logger)
                    sc["flag"] = (sc["p"] >= thr).astype(int)
                    g = sc.groupby("day")
                    daily = pd.DataFrame({"n_scored": g.size(), "mean_p": g["p"].mean(), "share_flag": g["flag"].mean(), "p90": g["p"].quantile(0.9)})
                    daily.index.name = "date"
                    daily.to_csv(out_dir / f"daily_signals_{name}.csv")
                    logger.info("daily signals: %d days -> daily_signals_%s.csv", len(daily), name)
    write_json(out_dir / "metrics.json", metrics)
    logger.info("Wrote %s", out_dir)
    logger.info("=== FARMERS TRAIN TWEETS finished successfully ===")


if __name__ == "__main__":
    main()
