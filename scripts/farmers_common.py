"""Shared helpers for the #FarmersProtest pipeline: text cleaning, rule lexicons, gold-label evaluation, extraction gazetteers."""
import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from common import DATA_DIR

warnings.filterwarnings("ignore", message="This pattern is interpreted as a regular expression")

FARMERS_DIR = DATA_DIR / "farmers_processed"
LABELS_DIR = DATA_DIR / "farmers_labels"
GOLD_PATH = LABELS_DIR / "gold_claude.csv"

_URL = re.compile(r"https?://\S+|twitter\.com/\S+|pic\.twitter\.com/\S+")
_MENTION = re.compile(r"@\w+")


def clean_text(t: str) -> str:
    t = _URL.sub(" ", str(t))
    t = _MENTION.sub("@user", t)
    t = t.replace("&amp;", "&").replace("#", "")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- weak-label rules
# STRONG = words for concrete physical violence or force. WEAK = words that often appear in rhetoric.
# Note: these lists were written while reading the 300-tweet gold sample, so the rule's score on that sample is optimistic.
STRONG = (r"beaten|beating|beat up|beats up|thrash|lathi ?charg|lathicharg|tear ?gas|water ?cannon|rammed(?! through)|mowed|run ?over|ran over|"
          r"run down|crushed|shot dead|shot at|firing|opened fire|bullet|stabb|lynch|chopp|murder|killed|massacre|slaughter|attacked|attacking|"
          r"attack on|brutal|assault|clash|riot|stone ?pelt|pelted|arson|set fire|set on fire|injur|wounded|hacked|rape|molest|violen|goons|"
          r"manhandl|stripped|burnt alive|shelling|dead body|hit by|lathi")
WEAK = r"kill|die\b|died|dead|death|blood|terror|fight|force|crush|cruel|hit\b|hurt|torture|martyr|shaheed|weapon|army|police|cops?\b|barricad|jail|arrest|detain"
STRONG_RE, WEAK_RE = re.compile(STRONG), re.compile(WEAK)


def rule_flags(texts: pd.Series) -> pd.DataFrame:
    low = texts.astype(str).str.lower().map(lambda t: _URL.sub(" ", t))
    return pd.DataFrame({"strong": low.str.contains(STRONG_RE), "weak": low.str.contains(WEAK_RE)}, index=texts.index)


# ---------------------------------------------------------------- gold evaluation
def load_gold(path: Path = GOLD_PATH) -> pd.DataFrame:
    g = pd.read_csv(path, dtype={"tweet_id": str})
    g["clean"] = g["text"].map(clean_text)
    return g


def weighted_prf(y, pred, w) -> dict:
    y, pred, w = np.asarray(y), np.asarray(pred), np.asarray(w, float)
    tp = w[(pred == 1) & (y == 1)].sum()
    fp = w[(pred == 1) & (y == 0)].sum()
    fn = w[(pred == 0) & (y == 1)].sum()
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(float(p), 4), "recall": round(float(r), 4), "f1": round(float(f), 4)}


def gold_metrics(y, score, w, threshold=0.5) -> dict:
    """Weighted precision/recall/F1 at `threshold` plus weighted PR-AUC / ROC-AUC. The weights undo the stratified sampling."""
    y, score = np.asarray(y), np.asarray(score, float)
    out = weighted_prf(y, (score >= threshold).astype(int), w)
    out["pr_auc"] = round(float(average_precision_score(y, score, sample_weight=w)), 4)
    out["roc_auc"] = round(float(roc_auc_score(y, score, sample_weight=w)), 4)
    out["prevalence_est"] = round(float((np.asarray(w)[y == 1].sum()) / np.sum(w)), 4)
    out["threshold"] = round(float(threshold), 4)
    out["n_gold"] = int(len(y))
    return out


def gold_bootstrap(y, score, w, strata, threshold=0.5, n_boot=300, seed=0) -> dict:
    """95% bootstrap interval for weighted precision / recall / F1, resampling inside each sampling stratum."""
    rng = np.random.default_rng(seed)
    y, score, w, strata = np.asarray(y), np.asarray(score, float), np.asarray(w, float), np.asarray(strata)
    groups = [np.where(strata == s)[0] for s in np.unique(strata)]
    res = {"precision": [], "recall": [], "f1": []}
    for _ in range(n_boot):
        idx = np.concatenate([rng.choice(g, size=len(g), replace=True) for g in groups])
        # keep each stratum's total weight fixed: weight = stratum population / stratum sample size
        m = weighted_prf(y[idx], (score[idx] >= threshold).astype(int), w[idx])
        for k in res:
            res[k].append(m[k])
    return {k: [round(float(np.percentile(v, 2.5)), 3), round(float(np.percentile(v, 97.5)), 3)] for k, v in res.items()}


def best_f1_threshold(y, score, w=None) -> float:
    """Threshold maximising (weighted) F1 on the given labelled data (use a validation set, not the gold sample)."""
    y, score = np.asarray(y), np.asarray(score, float)
    w = np.ones(len(y)) if w is None else np.asarray(w, float)
    best, thr = -1.0, 0.5
    for t in np.unique(np.quantile(score, np.linspace(0.5, 0.999, 120))):
        f = weighted_prf(y, (score >= t).astype(int), w)["f1"]
        if f > best:
            best, thr = f, float(t)
    return thr


# ---------------------------------------------------------------- extraction gazetteers
EVENT_TYPES = {
    "bharat_bandh": r"bharat ?bandh|\bbandh\b",
    "chakka_jaam": r"chakka ?ja+m",
    "rail_roko": r"rail ?roko|rail ?blockade|blocked? (the )?rail|railway track",
    "tractor_rally": r"tractor ?(rally|parade|march)|kisan ?(gantantra )?parade|tractor2twitter",
    "mahapanchayat": r"mahapanchayat|maha ?panchayat",
    "road_blockade": r"highway (block|jam)|road (block|jam)|border (block|seal)|blocked? (the )?(road|highway|border)",
    "police_force": r"lathi ?charg|tear ?gas|water ?cannon|baton|rubber bullet",
    "clash": r"clash|stone ?pelt|violence|riot|scuffle",
    "hunger_strike_dharna": r"hunger ?strike|dharna|sit-?in|\bmorcha\b|\bdharne\b",
}
# location -> ACLED admin1 state ("" = a site that is not a state)
PLACES = {
    "singhu": "Delhi", "tikri": "Delhi", "ghazipur": "Uttar Pradesh", "gazipur": "Uttar Pradesh", "shahjahanpur": "Rajasthan",
    "red fort": "Delhi", "delhi": "Delhi", "ito": "Delhi",
    "punjab": "Punjab", "amritsar": "Punjab", "ludhiana": "Punjab", "patiala": "Punjab", "chandigarh": "Chandigarh", "bathinda": "Punjab",
    "sangrur": "Punjab", "jalandhar": "Punjab", "ferozepur": "Punjab", "moga": "Punjab", "mansa": "Punjab",
    "haryana": "Haryana", "karnal": "Haryana", "sonipat": "Haryana", "rohtak": "Haryana", "hisar": "Haryana", "sirsa": "Haryana",
    "ambala": "Haryana", "kurukshetra": "Haryana", "jind": "Haryana", "bahadurgarh": "Haryana", "tohana": "Haryana", "panipat": "Haryana",
    "uttar pradesh": "Uttar Pradesh", "lakhimpur": "Uttar Pradesh", "muzaffarnagar": "Uttar Pradesh", "meerut": "Uttar Pradesh",
    "lucknow": "Uttar Pradesh", "bijnor": "Uttar Pradesh", "mathura": "Uttar Pradesh", "bareilly": "Uttar Pradesh",
    "rajasthan": "Rajasthan", "jaipur": "Rajasthan", "sikar": "Rajasthan", "alwar": "Rajasthan",
    "maharashtra": "Maharashtra", "mumbai": "Maharashtra", "nashik": "Maharashtra", "pune": "Maharashtra",
    "tamil nadu": "Tamil Nadu", "chennai": "Tamil Nadu", "karnataka": "Karnataka", "bengaluru": "Karnataka", "bangalore": "Karnataka",
    "kerala": "Kerala", "bihar": "Bihar", "patna": "Bihar", "west bengal": "West Bengal", "kolkata": "West Bengal", "siliguri": "West Bengal",
    "madhya pradesh": "Madhya Pradesh", "bhopal": "Madhya Pradesh", "gujarat": "Gujarat", "odisha": "Odisha", "telangana": "Telangana",
    "hyderabad": "Telangana", "andhra": "Andhra Pradesh", "assam": "Assam", "jharkhand": "Jharkhand", "ranchi": "Jharkhand",
    "uttarakhand": "Uttarakhand", "himachal": "Himachal Pradesh", "chhattisgarh": "Chhattisgarh", "goa": "Goa",
}
STATES = sorted(set(PLACES.values()))
_EVENT_RE = {k: re.compile(v) for k, v in EVENT_TYPES.items()}
_STATE_PLACES = {s: [k for k, v in PLACES.items() if v == s] for s in STATES}
_STATE_RE = {s: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + r")\b") for s, keys in _STATE_PLACES.items()}


def extract_events(texts: pd.Series) -> pd.DataFrame:
    """Per tweet: boolean column per event type (ev_*) and per ACLED state (st_*; a state counts if any of its places is named)."""
    low = texts.astype(str).str.lower().map(lambda t: _URL.sub(" ", t.replace("_", " ")))
    out = {f"ev_{k}": low.str.contains(r) for k, r in _EVENT_RE.items()}
    out.update({f"st_{s}": low.str.contains(r) for s, r in _STATE_RE.items()})
    return pd.DataFrame(out, index=texts.index)


def load_acled_events(path, start, end) -> pd.DataFrame:
    """ACLED India rows in [start, end] with the same 'farmer-related' / 'violent' flags as scripts/farmers_prepare.py."""
    a = pd.read_csv(path, low_memory=False)
    a["event_date"] = pd.to_datetime(a["event_date"])
    a = a[(a["event_date"] >= start) & (a["event_date"] <= end)].copy()
    a["farmer"] = (a["notes"].str.contains("farm", case=False, na=False) | a["actor1"].astype(str).str.contains("Farm|Kisan", case=False)
                   | a["assoc_actor_1"].astype(str).str.contains("Farm|Kisan|SKM", case=False))
    a["violent"] = a["sub_event_type"].isin({"Mob violence", "Violent demonstration", "Protest with intervention", "Looting/property destruction", "Armed clash", "Attack"})
    return a
