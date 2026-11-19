"""Check new, unseen posts for violence and unrest signals using the trained models.

For every post you give it, it prints:
  violence score   probability (0-1) from the model that the post describes, reports or incites violence
  verdict          VIOLENT if the score is at or above the decision threshold (tuned on the gold sample), else not violent
  signal           VIOLENT UNREST SIGNAL / protest topic, no violence / no unrest signal
  details          violence words found, protest actions (bharat bandh, chakka jaam, rail roko ...) and states named

If you give dates, it also summarises each day and compares the day's average violence score with the days in the Nov 2020 - Nov 2021
#FarmersProtest archive (percentile).

IMPORTANT: this scores the text of each post. It cannot tell whether a post will lead to unrest: in this project, forecasting unrest days
ahead from tweets showed no skill, and same-day detection from tweets was only a weak signal. Use it to screen text, not to predict events.

Input (choose one)
  --text "post one" --text "post two"
  --file posts.csv  [--text-col text] [--date-col date]     (CSV with a column of post text; the date column is optional)
  --interactive                                            (type or paste one post per line, empty line to quit)

Model   --model auto (default: the latest DistilRoBERTa in models/farmers_transformer_*, else TF-IDF) | transformer | tfidf
Output  results/predict_<runid>/predictions.csv (and daily.csv when dates are given); log in logs/predict_unrest/

Examples
  python scripts/predict_unrest.py --text "Police used tear gas on farmers at the border"
  python scripts/predict_unrest.py --file data/examples/new_posts_example.csv --date-col date
  python scripts/predict_unrest.py --interactive
"""
import argparse
import glob
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from common import MODELS_DIR, RESULTS_DIR, log_environment, setup_logger
from farmers_common import LABELS_DIR, STATES, STRONG_RE, best_f1_threshold, clean_text, extract_events, load_gold, rule_flags


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text", action="append", default=[], help="a post to check (repeat for several)")
    p.add_argument("--file", type=Path, default=None)
    p.add_argument("--text-col", default="text")
    p.add_argument("--date-col", default=None)
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--model", choices=["auto", "transformer", "tfidf"], default="auto")
    p.add_argument("--transformer-dir", type=Path, default=None)
    p.add_argument("--threshold", type=float, default=None, help="override the decision threshold")
    p.add_argument("--max-length", type=int, default=64)
    p.add_argument("--log-dir", type=Path, default=None)
    return p.parse_args()


def load_scorer(args, logger):
    """Return (name, score_fn(list_of_clean_texts) -> probabilities, threshold)."""
    gold = load_gold(LABELS_DIR / "gold_claude.csv")
    mdir = args.transformer_dir
    if mdir is None and args.model in ("auto", "transformer"):
        cands = sorted(p for p in glob.glob(str(MODELS_DIR / "farmers_transformer_*")) if (Path(p) / "best").exists())
        mdir = Path(cands[-1]) if cands else None
    if args.model == "transformer" and mdir is None:
        raise SystemExit("no trained transformer found in models/. Train one: python scripts/farmers_train_tweets.py --model transformer --epochs 2")
    if mdir is not None and args.model != "tfidf":
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        cfg = json.loads((mdir / "run_config.json").read_text())
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        tok = AutoTokenizer.from_pretrained(mdir / "best")
        model = AutoModelForSequenceClassification.from_pretrained(mdir / "best").to(dev).eval()
        maxlen = int(cfg.get("max_length", args.max_length))

        @torch.no_grad()
        def score(texts):
            texts = list(texts)
            order = np.argsort([len(t) for t in texts])
            probs = np.zeros(len(texts))
            for i in range(0, len(order), 128):
                idx = order[i:i + 128]
                enc = tok([texts[j] for j in idx], truncation=True, max_length=maxlen, padding=True, return_tensors="pt").to(dev)
                probs[idx] = torch.softmax(model(**enc).logits.float(), dim=-1)[:, 1].cpu().numpy()
            return probs

        name = f"transformer ({mdir.name})"
        thr = best_f1_threshold(gold["label"].values, score(gold["clean"]), gold["weight"].values)
        return "transformer", name, score, thr
    # TF-IDF: retrain from the silver training set (about 30 s)
    from farmers_train_tweets import tfidf_fit_predict
    silver = {k: pd.read_csv(LABELS_DIR / f"silver_{k}.csv", dtype={"tweet_id": str}) for k in ("train", "val", "test")}
    for d in silver.values():
        d["clean"] = d["text"].astype(str).map(clean_text)
    res = tfidf_fit_predict(silver["train"], silver["val"], silver["test"].head(10), gold, args, logger)
    thr = best_f1_threshold(gold["label"].values, res["gold"], gold["weight"].values)
    return "tfidf", "tfidf (retrained from the silver training set)", res["daily_fn"], thr


def analyse(texts, score_fn, thr):
    clean = [clean_text(t) for t in texts]
    ok = [len(c) >= 3 for c in clean]
    p = np.full(len(clean), np.nan)
    if any(ok):
        p[np.array(ok)] = score_fn([c for c, k in zip(clean, ok) if k])
    ser = pd.Series(clean)
    ev, strong_terms = extract_events(ser), []
    for c in clean:
        low = c.lower()
        strong_terms.append(sorted({m.group(0).strip() for m in STRONG_RE.finditer(low)}))
    actions = [", ".join(k[3:].replace("_", " ") for k in ev.columns if k.startswith("ev_") and ev.iloc[i][k]) for i in range(len(clean))]
    states = [", ".join(s for s in STATES if ev.iloc[i].get(f"st_{s}", False)) for i in range(len(clean))]
    out = pd.DataFrame({"text": texts, "violence_score": np.round(p, 4)})
    out["verdict"] = np.where(np.isnan(p), "too short", np.where(p >= thr, "VIOLENT", "not violent"))
    sig = []
    for i in range(len(clean)):
        if np.isnan(p[i]):
            sig.append("n/a")
        elif p[i] >= thr:
            sig.append("VIOLENT UNREST SIGNAL")
        elif actions[i] or states[i]:
            sig.append("protest topic, no violence")
        else:
            sig.append("no unrest signal")
    out["signal"] = sig
    out["violence_words"] = [", ".join(t) for t in strong_terms]
    out["protest_actions"] = actions
    out["states_named"] = states
    return out


def history_reference(model_kind):
    paths = sorted(glob.glob(str(RESULTS_DIR / "farmers_tweets_*" / f"daily_signals_{model_kind}.csv")))
    if not paths:
        return None
    return pd.read_csv(paths[-1])["mean_p"].dropna().values


def daily_summary(out, dates, model_kind, thr):
    dt = pd.to_datetime(pd.Series(dates).reset_index(drop=True), errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    d = pd.DataFrame({"date": dt, "p": out["violence_score"].values})
    d = d.dropna()
    if d.empty:
        return None
    g = d.groupby("date")["p"]
    day = pd.DataFrame({"posts": g.size(), "mean_violence_score": g.mean().round(4), "share_violent": g.apply(lambda s: float((s >= thr).mean())).round(3)})
    ref = history_reference(model_kind)
    if ref is not None:
        day["percentile_vs_archive_days"] = [round(100 * float((ref < v).mean()), 1) for v in day["mean_violence_score"]]
    day["note"] = np.where(day["posts"] < 30, "few posts: treat as noisy", "")
    return day


def show(out):
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 70)
    for i, r in out.iterrows():
        print(f"\n[{i + 1}] {str(r['text'])[:140]}")
        print(f"    violence score {r['violence_score']}   verdict: {r['verdict']}   signal: {r['signal']}")
        extra = []
        if r["violence_words"]:
            extra.append(f"violence words: {r['violence_words']}")
        if r["protest_actions"]:
            extra.append(f"protest actions: {r['protest_actions']}")
        if r["states_named"]:
            extra.append(f"states: {r['states_named']}")
        if extra:
            print("    " + "   |   ".join(extra))


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("predict_unrest", log_dir=args.log_dir)
    log_environment(logger, args)
    kind, name, score_fn, thr = load_scorer(args, logger)
    if args.threshold is not None:
        thr = args.threshold
    print(f"\nModel: {name}\nDecision threshold: {thr:.3f}  (tuned on the gold sample; change with --threshold)")
    print("Note: this scores each post's text. It cannot predict whether a post will lead to unrest.\n")

    out_dir = RESULTS_DIR / f"predict_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.interactive:
        rows = []
        print("Type or paste one post per line. Empty line to finish.")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                break
            o = analyse([line], score_fn, thr)
            show(o)
            rows.append(o)
        if rows:
            pd.concat(rows, ignore_index=True).to_csv(out_dir / "predictions.csv", index=False, encoding="utf-8-sig")
            print(f"\nSaved {out_dir / 'predictions.csv'}")
        return

    dates = None
    if args.file:
        df = pd.read_csv(args.file, encoding="utf-8-sig")
        if args.text_col not in df.columns:
            raise SystemExit(f"column '{args.text_col}' not found in {args.file}; columns are {list(df.columns)}. Use --text-col.")
        texts = df[args.text_col].astype(str).tolist()
        if args.date_col:
            if args.date_col not in df.columns:
                raise SystemExit(f"column '{args.date_col}' not found; columns are {list(df.columns)}")
            dates = df[args.date_col]
    else:
        texts = args.text
    if not texts:
        raise SystemExit("nothing to check: give --text, --file or --interactive (see --help)")

    out = analyse(texts, score_fn, thr)
    if args.file:
        for c in df.columns:
            if c != args.text_col and c not in out.columns:
                out[c] = df[c].values
    if len(out) <= 25:
        show(out)
    else:
        print(out["signal"].value_counts().to_string())
        print("\nTop 10 by violence score:")
        show(out.sort_values("violence_score", ascending=False).head(10).reset_index(drop=True))
    out.to_csv(out_dir / "predictions.csv", index=False, encoding="utf-8-sig")
    print(f"\nSaved all {len(out)} rows to {out_dir / 'predictions.csv'}")
    if dates is not None:
        day = daily_summary(out, dates, kind, thr)
        if day is not None:
            day.to_csv(out_dir / "daily.csv", encoding="utf-8-sig")
            print("\nDaily summary (percentile = how this day's average violence score ranks against the days in the 2020-21 archive):")
            print(day.to_string())
            print(f"Saved {out_dir / 'daily.csv'}")


if __name__ == "__main__":
    main()
