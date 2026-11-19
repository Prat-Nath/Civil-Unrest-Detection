"""
Backend for the Civil Unrest Signal Room (FastAPI).

It serves your trained models and your real result files to the web app:
  GET  /api/status       which models are loaded, thresholds, data found
  POST /api/analyze      score one post with every loaded model + word importance + rule words, protest actions, states
  POST /api/batch        score many posts (optionally with dates) + histogram + daily summary vs the 2020-21 archive
  GET  /api/timeline     the 386-day #FarmersProtest archive: tweets, ACLED events, model signal, surge days
  GET  /api/day/{date}   inspect one archive day: sampled tweets scored live, actions, states, ACLED counts
  GET  /api/lab          gold-sample scores of every model (the web app recomputes precision / recall for any threshold)
  GET  /api/evidence     extraction vs ACLED, surge forecast, G-CUT forecast results

Run from the project root:   python app/backend/server.py          (then open http://127.0.0.1:8000)
The built web app in app/frontend/dist is served at "/".
"""
from __future__ import annotations

import argparse
import glob
import html as _html
import json
import math
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("NLP_ROOT", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "scripts"))

from farmers_common import (EVENT_TYPES, STATES, STRONG_RE, WEAK_RE, best_f1_threshold, clean_text, extract_events,  # noqa: E402
                            load_gold, rule_flags)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

RESULTS, MODELS, DATA = ROOT / "results", ROOT / "models", ROOT / "data"
LABELS, FARMERS = DATA / "farmers_labels", DATA / "farmers_processed"
STRONG_I, WEAK_I = re.compile(STRONG_RE.pattern, re.I), re.compile(WEAK_RE.pattern, re.I)
URL_RE = re.compile(r"https?://\S+|twitter\.com/\S+|pic\.twitter\.com/\S+")
MENTION_RE = re.compile(r"@\w+")


# ------------------------------------------------------------------ models
class Scorer:
    def __init__(self, sid, name, kind, fn, threshold, note, thr_note):
        self.id, self.name, self.kind, self.fn = sid, name, kind, fn
        self.threshold, self.note, self.thr_note = threshold, note, thr_note
        self.lock = threading.Lock()

    def score(self, texts):
        with self.lock:
            return np.asarray(self.fn(list(texts)), float)

    def info(self):
        return {"id": self.id, "name": self.name, "kind": self.kind, "threshold": round(float(self.threshold), 4),
                "threshold_note": self.thr_note, "note": self.note}


STATE: dict = {"scorers": {}, "gold": None, "gold_scores": {}, "errors": [], "device": "cpu", "started": time.time(), "tweets": None, "loading_tweets": False}


def _torch_scorer(model_dir: Path, maxlen_default=64):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    cfg = json.loads((model_dir / "run_config.json").read_text())
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    STATE["device"] = dev
    tok = AutoTokenizer.from_pretrained(model_dir / "best")
    model = AutoModelForSequenceClassification.from_pretrained(model_dir / "best").to(dev).eval()
    maxlen = int(cfg.get("max_length", maxlen_default))

    @torch.no_grad()
    def fn(texts):
        order = np.argsort([len(t) for t in texts])
        probs = np.zeros(len(texts))
        for i in range(0, len(order), 64):
            idx = order[i:i + 64]
            enc = tok([texts[j] for j in idx], truncation=True, max_length=maxlen, padding=True, return_tensors="pt").to(dev)
            probs[idx] = torch.softmax(model(**enc).logits.float(), dim=-1)[:, 1].cpu().numpy()
        return probs

    return fn, float(cfg.get("threshold", 0.5)), cfg


def _tfidf_scorer():
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    cache = MODELS / "farmers_tfidf_app.joblib"
    if cache.exists():
        vec, clf = joblib.load(cache)
    else:
        tr = pd.read_csv(LABELS / "silver_train.csv", dtype={"tweet_id": str})
        clean = tr["text"].astype(str).map(clean_text)
        vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=200000, sublinear_tf=True, lowercase=True)
        clf = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000).fit(vec.fit_transform(clean), tr["label"])
        MODELS.mkdir(exist_ok=True)
        joblib.dump((vec, clf), cache)
    return lambda texts: clf.predict_proba(vec.transform(texts))[:, 1]


def load_models():
    gold = load_gold(LABELS / "gold_claude.csv") if (LABELS / "gold_claude.csv").exists() else None
    STATE["gold"] = gold
    sc = STATE["scorers"]

    def gold_thr(fn):
        if gold is None:
            return 0.5
        p = np.asarray(fn(gold["clean"].tolist()), float)
        return best_f1_threshold(gold["label"].values, p, gold["weight"].values), p

    # 1. tweet DistilRoBERTa
    try:
        cands = sorted(p for p in glob.glob(str(MODELS / "farmers_transformer_*")) if (Path(p) / "best").exists())
        if cands:
            fn, thr_silver, cfg = _torch_scorer(Path(cands[-1]))
            thr, pg = gold_thr(fn)
            sc["tweet_transformer"] = Scorer("tweet_transformer", "Tweet DistilRoBERTa", "tweet", fn, thr,
                                             f"fine-tuned on weak labels from #FarmersProtest tweets ({Path(cands[-1]).name})",
                                             "tuned on the 293-tweet gold sample")
            STATE["gold_scores"]["tweet_transformer"] = pg
    except Exception as e:  # noqa: BLE001
        STATE["errors"].append(f"tweet transformer not loaded: {e}")
    # 2. tweet TF-IDF
    try:
        fn = _tfidf_scorer()
        thr, pg = gold_thr(fn)
        sc["tweet_tfidf"] = Scorer("tweet_tfidf", "Tweet TF-IDF", "tweet", fn, thr, "logistic regression on word and bigram TF-IDF, trained on the same weak labels",
                                   "tuned on the 293-tweet gold sample")
        STATE["gold_scores"]["tweet_tfidf"] = pg
    except Exception as e:  # noqa: BLE001
        STATE["errors"].append(f"tweet tfidf not loaded: {e}")
    # 3. ACLED report-style model
    try:
        cands = sorted(p for p in glob.glob(str(MODELS / "distilroberta-base_binary_2*")) if "masked" not in p and (Path(p) / "best").exists())
        if cands:
            fn, thr, cfg = _torch_scorer(Path(cands[-1]), 128)
            sc["acled_report"] = Scorer("acled_report", "ACLED report model", "report", fn, thr,
                                        f"DistilRoBERTa trained on ACLED India event notes ({Path(cands[-1]).name}); made for news-style reports, does not transfer to tweets",
                                        "its own threshold from training on ACLED")
            if gold is not None:
                STATE["gold_scores"]["acled_report"] = np.asarray(fn(gold["clean"].tolist()), float)
    except Exception as e:  # noqa: BLE001
        STATE["errors"].append(f"ACLED model not loaded: {e}")
    if gold is not None:
        STATE["gold_scores"]["rule"] = rule_flags(gold["clean"])["strong"].astype(float).values


def load_tweets_bg():
    try:
        STATE["loading_tweets"] = True
        f = FARMERS / "tweets_text.csv.gz"
        tw = pd.read_csv(f, usecols=["tweet_id", "date", "text"], dtype={"tweet_id": str})
        dt = pd.to_datetime(tw["date"], utc=True, format="ISO8601", errors="coerce")
        tw = tw[dt.notna()].copy()
        tw["day"] = dt[dt.notna()].dt.tz_localize(None).dt.strftime("%Y-%m-%d")
        STATE["tweets"] = tw.drop(columns=["date"]).drop_duplicates("tweet_id").reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        STATE["errors"].append(f"archive tweets not loaded: {e}")
    finally:
        STATE["loading_tweets"] = False


@asynccontextmanager
async def lifespan(app):
    def boot():
        t0 = time.time()
        STATE["models_loading"] = True
        try:
            load_models()
        finally:
            STATE["models_loading"] = False
            STATE["load_seconds"] = round(time.time() - t0, 1)
            print(f"models ready in {STATE['load_seconds']}s: {list(STATE['scorers'])}  errors: {STATE['errors']}")
        load_tweets_bg()
    STATE["models_loading"] = True
    threading.Thread(target=boot, daemon=True).start()
    yield


app = FastAPI(title="Civil Unrest Signal Room", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ helpers
def primary_scorer(model_id=None) -> Scorer:
    sc = STATE["scorers"]
    if model_id and model_id in sc:
        return sc[model_id]
    for k in ("tweet_transformer", "tweet_tfidf", "acled_report"):
        if k in sc:
            return sc[k]
    raise HTTPException(503, "models are still loading (this can take a minute after start); try again shortly" if STATE.get("models_loading") else "no model is loaded; see /api/status")


def anonymise(t: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub("@user", URL_RE.sub("", _html.unescape(str(t))))).strip()


def logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def actions_states(texts):
    ev = extract_events(pd.Series(list(texts)))
    acts = [[c[3:] for c in ev.columns if c.startswith("ev_") and bool(ev.iloc[i][c])] for i in range(len(ev))]
    sts = [[c[3:] for c in ev.columns if c.startswith("st_") and bool(ev.iloc[i][c])] for i in range(len(ev))]
    return acts, sts


def signal_of(score, thr, acts, sts):
    if score >= thr:
        return "violent"
    if acts or sts:
        return "protest_topic"
    return "none"


def archive_reference(kind="transformer"):
    paths = sorted(glob.glob(str(RESULTS / "farmers_tweets_*" / f"daily_signals_{kind}.csv")))
    if not paths:
        return None
    return pd.read_csv(paths[-1])["mean_p"].dropna().values


def latest_dir(pattern, must):
    c = [d for d in sorted(glob.glob(str(RESULTS / pattern))) if (Path(d) / must).exists()]
    return Path(c[-1]) if c else None


def jload(p):
    return json.loads(Path(p).read_text())


def clean_nan(o):
    if isinstance(o, dict):
        return {k: clean_nan(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_nan(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


# ------------------------------------------------------------------ API
@app.get("/api/status")
def status():
    sc = STATE["scorers"]
    return {"models": [s.info() for s in sc.values()], "errors": STATE["errors"], "device": STATE["device"],
            "load_seconds": STATE.get("load_seconds"), "models_loading": bool(STATE.get("models_loading")), "archive_loaded": STATE["tweets"] is not None, "archive_loading": STATE["loading_tweets"],
            "gold_n": None if STATE["gold"] is None else int(len(STATE["gold"])), "root": str(ROOT),
            "event_types": list(EVENT_TYPES.keys()), "states": STATES}


class AnalyzeIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    explain: bool = True
    explain_model: str | None = None


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    text = clean_text(body.text)
    if len(text) < 3:
        raise HTTPException(400, "text too short")
    sc = STATE["scorers"]
    primary_scorer()
    models = []
    for s in list(sc.values()):
        p = float(s.score([text])[0])
        models.append({**s.info(), "score": round(p, 5), "logit": round(float(logit(p)), 3), "verdict": "violent" if p >= s.threshold else "not_violent"})
    acts, sts = actions_states([text])
    prim = primary_scorer(body.explain_model)
    pm = next(m for m in models if m["id"] == prim.id)
    spans = sorted([[m.start(), m.end(), "strong"] for m in STRONG_I.finditer(text)] +
                   [[m.start(), m.end(), "weak"] for m in WEAK_I.finditer(text)
                    if not any(m.start() < s[1] and m.end() > s[0] for s in [[x.start(), x.end()] for x in STRONG_I.finditer(text)])])
    out = {"text": text, "models": models, "primary": prim.id, "signal": signal_of(pm["score"], prim.threshold, acts[0], sts[0]),
           "rule": {"strong": sorted({text[a:b].lower() for a, b, t in spans if t == "strong"}), "weak": sorted({text[a:b].lower() for a, b, t in spans if t == "weak"}), "spans": spans},
           "actions": acts[0], "states": sts[0]}
    tweet_models = [m for m in models if m["kind"] == "tweet"]
    if len(tweet_models) >= 2:
        v = {m["verdict"] for m in tweet_models}
        out["agreement"] = "agree" if len(v) == 1 else "disagree"
    if body.explain:
        words = text.split()[:60]
        if len(words) >= 2:
            variants = [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
            base = logit(prim.score([" ".join(words)]))[0]
            alt = logit(prim.score(variants))
            deltas = np.clip(base - alt, -10, 10)
            out["explain"] = {"model": prim.id, "model_name": prim.name, "words": [{"w": w, "d": round(float(d), 3)} for w, d in zip(words, deltas)],
                              "rest": text.split()[60:], "base_logit": round(float(base), 3)}
    return out


class BatchIn(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=5000)
    dates: list[str | None] | None = None
    model: str | None = None


@app.post("/api/batch")
def batch(body: BatchIn):
    prim = primary_scorer(body.model)
    clean = [clean_text(t) for t in body.texts]
    ok = np.array([len(c) >= 3 for c in clean])
    p = np.full(len(clean), np.nan)
    if ok.any():
        p[ok] = prim.score([c for c, k in zip(clean, ok) if k])
    acts, sts = actions_states(clean)
    rows = []
    for i, c in enumerate(clean):
        if not ok[i]:
            rows.append({"i": i, "text": body.texts[i][:300], "score": None, "verdict": "too_short", "signal": "n/a", "actions": [], "states": [], "words": []})
            continue
        words = sorted({m.group(0).lower() for m in STRONG_I.finditer(c)})
        rows.append({"i": i, "text": c[:300], "score": round(float(p[i]), 4), "verdict": "violent" if p[i] >= prim.threshold else "not_violent",
                     "signal": signal_of(p[i], prim.threshold, acts[i], sts[i]), "actions": acts[i], "states": sts[i], "words": words})
    valid = p[ok]
    hist, edges = np.histogram(valid, bins=20, range=(0, 1)) if len(valid) else (np.zeros(20, int), np.linspace(0, 1, 21))
    summary = {"n": len(clean), "scored": int(ok.sum()), "violent": int((valid >= prim.threshold).sum()) if len(valid) else 0,
               "protest_topic": sum(1 for r in rows if r["signal"] == "protest_topic"), "mean_score": round(float(np.mean(valid)), 4) if len(valid) else None,
               "model": prim.id, "model_name": prim.name, "threshold": round(prim.threshold, 4), "hist": hist.tolist()}
    daily = None
    if body.dates:
        dt = pd.to_datetime(pd.Series(body.dates), errors="coerce", utc=True).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
        d = pd.DataFrame({"date": dt, "p": p}).dropna()
        if not d.empty:
            kind = "transformer" if prim.id == "tweet_transformer" else ("tfidf" if prim.id == "tweet_tfidf" else None)
            ref = archive_reference(kind) if kind else None
            daily = []
            for day, g in d.groupby("date"):
                m = float(g["p"].mean())
                daily.append({"date": day, "posts": int(len(g)), "mean": round(m, 4), "share_violent": round(float((g["p"] >= prim.threshold).mean()), 3),
                              "percentile": None if ref is None else round(100 * float((ref < m).mean()), 1), "noisy": bool(len(g) < 30)})
    return {"rows": rows, "summary": summary, "daily": daily}


@app.get("/api/timeline")
def timeline():
    t = pd.read_csv(FARMERS / "tweets_daily.csv", usecols=["date", "n_tweets", "kw_violence"])
    a = pd.read_csv(FARMERS / "acled_daily.csv")
    df = t.merge(a[["date", "acled_farmer", "acled_farmer_violent", "acled_farmer_riot"]], on="date", how="left").fillna(0)
    sig_kind = None
    for kind in ("transformer", "tfidf"):
        paths = sorted(glob.glob(str(RESULTS / "farmers_tweets_*" / f"daily_signals_{kind}.csv")))
        if paths:
            s = pd.read_csv(paths[-1])
            df = df.merge(s[["date", "mean_p", "share_flag"]], on="date", how="left")
            sig_kind = kind
            break
    if "mean_p" not in df:
        df["mean_p"], df["share_flag"] = np.nan, np.nan
    fd = latest_dir("farmers_forecast_*", "surge_days.csv")
    surge = set(pd.read_csv(fd / "surge_days.csv")["date"].astype(str)) if fd else set()
    df["surge"] = df["date"].astype(str).isin(surge)
    return clean_nan({"dates": df["date"].tolist(), "tweets": df["n_tweets"].astype(int).tolist(), "kw_violence": df["kw_violence"].astype(int).tolist(),
                      "acled": df["acled_farmer"].astype(int).tolist(), "acled_violent": df["acled_farmer_violent"].astype(int).tolist(),
                      "mean_p": df["mean_p"].round(4).tolist(), "share_flag": df["share_flag"].round(4).tolist(), "surge": df["surge"].tolist(), "signal_model": sig_kind})


@app.get("/api/day/{date}")
def day(date: str, n: int = 250):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    tw = STATE["tweets"]
    if tw is None:
        raise HTTPException(503, "the tweet archive is still loading (about 20 seconds after start); try again")
    d = tw[tw["day"] == date]
    if d.empty:
        raise HTTPException(404, "no tweets for that day")
    total = len(d)
    clean_all = d["text"].astype(str).map(clean_text)
    ev = extract_events(clean_all)
    action_counts = {k[3:]: int(ev[k].sum()) for k in ev.columns if k.startswith("ev_")}
    state_counts = sorted(({"state": k[3:], "tweets": int(ev[k].sum())} for k in ev.columns if k.startswith("st_") and ev[k].sum() > 0), key=lambda x: -x["tweets"])[:8]
    sample = d.sample(min(n, total), random_state=7)
    sample_clean = sample["text"].astype(str).map(clean_text)
    keep = sample_clean.str.len() >= 15
    sample, sample_clean = sample[keep], sample_clean[keep]
    prim = primary_scorer()
    p = prim.score(sample_clean.tolist())
    order = np.argsort(-p)[:8]
    top = [{"text": anonymise(sample["text"].iloc[i])[:280], "score": round(float(p[i]), 4)} for i in order]
    hist, _ = np.histogram(p, bins=20, range=(0, 1))
    a = pd.read_csv(FARMERS / "acled_daily.csv")
    arow = a[a["date"].astype(str) == date]
    ref = archive_reference("transformer" if prim.id == "tweet_transformer" else "tfidf")
    m = float(np.mean(p)) if len(p) else None
    return clean_nan({"date": date, "tweets_total": int(total), "scored": int(len(p)), "model": prim.name, "mean_score": None if m is None else round(m, 4),
                      "share_violent": round(float((p >= prim.threshold).mean()), 3) if len(p) else None, "threshold": round(prim.threshold, 4), "hist": hist.tolist(),
                      "top": top, "actions": action_counts, "states": state_counts,
                      "percentile": None if (ref is None or m is None) else round(100 * float((ref < m).mean()), 1),
                      "acled": None if arow.empty else {"farmer_events": int(arow["acled_farmer"].iloc[0]), "farmer_violent": int(arow["acled_farmer_violent"].iloc[0]),
                                                         "farmer_riots": int(arow["acled_farmer_riot"].iloc[0])}})


@app.get("/api/lab")
def lab():
    g = STATE["gold"]
    if g is None:
        raise HTTPException(503, "models are still loading; try again shortly" if STATE.get("models_loading") else "gold sample not found")
    if STATE.get("models_loading"):
        raise HTTPException(503, "models are still loading; try again shortly")
    models = {}
    names = {"tweet_transformer": "Tweet DistilRoBERTa", "tweet_tfidf": "Tweet TF-IDF", "acled_report": "ACLED report model", "rule": "Keyword rule"}
    for k, p in STATE["gold_scores"].items():
        models[k] = {"name": names.get(k, k), "scores": [round(float(x), 5) for x in p],
                     "default_threshold": 0.5 if k == "rule" else round(float(STATE["scorers"][k].threshold), 4)}
    cv = None
    gp = RESULTS / "farmers_gold_threshold" / "metrics.json"
    if gp.exists():
        cv = jload(gp)
    return clean_nan({"n": int(len(g)), "labels": g["label"].astype(int).tolist(), "weights": [round(float(x), 4) for x in g["weight"]],
                      "strata": g["stratum"].tolist(), "models": models, "cv": cv,
                      "note": "Labels: one annotator (Claude), not human-verified. Weights undo the stratified sampling."})


@app.get("/api/evidence")
def evidence():
    out = {}
    ed = latest_dir("farmers_extract_*", "metrics.json")
    if ed:
        m = jload(ed / "metrics.json")
        out["extraction"] = {"event_types": m["event_types"], "places": m["places"]}
        sd = ed / "state_daily.csv"
        if sd.exists():
            s = pd.read_csv(sd)
            g = s.groupby("state")[["tweet_mentions", "acled_farmer_events"]].sum().reset_index()
            g = g[(g["tweet_mentions"] > 0) | (g["acled_farmer_events"] > 0)].sort_values("acled_farmer_events", ascending=False).head(12)
            out["states"] = g.to_dict("records")
    fd = latest_dir("farmers_forecast_*", "metrics.json")
    if fd:
        m = jload(fd / "metrics.json")
        out["forecast"] = {h: {"positives": m[h]["positives"], "models": m[h]["models"], "ci": m[h]["roc_auc_ci95_block_bootstrap"]} for h in ("h0", "h1", "h3", "h7")}
        out["surge_days"] = pd.read_csv(fd / "surge_days.csv").to_dict("records") if (fd / "surge_days.csv").exists() else []
    sp = RESULTS / "social_summary" / "summary.csv"
    if sp.exists():
        out["social"] = pd.read_csv(sp).to_dict("records")
    td = latest_dir("farmers_transfer_*", "metrics.json")
    if td:
        t = jload(td / "metrics.json")
        out["transfer"] = {"tweets_scored": t.get("tweets_scored"), "share_ge_0.5": t.get("tweet_level", {}).get("share_ge_0.5")}
    acled = []
    for d in sorted(glob.glob(str(RESULTS / "*binary_2*"))):
        mp = Path(d) / "metrics.json"
        if mp.exists():
            m = jload(mp)
            t = m.get("test")
            if t:
                acled.append({"run": Path(d).name, "f1_violent": round(t["f1_violent"], 4), "pr_auc": round(t["pr_auc_violent"], 4), "roc_auc": round(t["roc_auc_violent"], 4), "n": t["n"]})
    out["acled_models"] = acled
    return clean_nan(out)


# ------------------------------------------------------------------ static web app
DIST = ROOT / "app" / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="web")
else:
    @app.get("/")
    def no_build():
        return JSONResponse({"message": "API is running. The web app is not built: run `npm install && npm run build` in app/frontend, or `npm run dev` for development."})


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if not a.no_browser:
        threading.Timer(3.0, lambda: __import__("webbrowser").open(f"http://{a.host}:{a.port}")).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
