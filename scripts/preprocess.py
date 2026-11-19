"""Step 1 - preprocessing: raw ACLED CSV(s) -> cleaned, labelled, time-ordered train/val/test files.

What it does
  1. load every *.csv in data/ and validate required columns
  2. keep crowd events only (Protests + Riots)
  3. clean text (unicode normalise, collapse whitespace), drop empty/very short notes
  4. drop exact duplicates (same notes + date + location)
  5. build labels:  crowd_label 0=peaceful 1=tension 2=violent,  is_violent (binary),  has_fatality
  6. extract crowd_size from the tags column
  7. split BY TIME (train = oldest, test = newest) so the model is never tested on the past
  8. write data/processed/{train,val,test}.csv + label_info.json
  9. write logs/preprocess/preprocess_<run_id>.log and ..._summary.json

Usage:  python scripts/preprocess.py            (all defaults)
        python scripts/preprocess.py --help
"""
import argparse
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

from common import (DATA_DIR, PROCESSED_DIR, log_environment, setup_logger, timed_step, write_json)

REQUIRED_COLUMNS = ["event_id_cnty", "event_date", "event_type", "sub_event_type", "notes", "fatalities",
                    "admin1", "location", "actor1", "tags"]
KEEP_COLUMNS = ["event_id_cnty", "event_date", "admin1", "admin2", "location", "latitude", "longitude",
                "event_type", "sub_event_type", "actor1", "actor2", "interaction", "fatalities", "crowd_size",
                "text", "crowd_label", "is_violent", "has_fatality"]
LABEL_NAMES = {0: "peaceful", 1: "tension", 2: "violent"}
PEACEFUL = {"Peaceful protest"}
TENSION = {"Protest with intervention", "Excessive force against protesters"}


def parse_args():
    p = argparse.ArgumentParser(description="Preprocess ACLED data for civil-unrest violence prediction.")
    p.add_argument("--input-dir", type=Path, default=DATA_DIR, help="folder containing the raw *.csv files")
    p.add_argument("--output-dir", type=Path, default=PROCESSED_DIR, help="where train/val/test.csv are written")
    p.add_argument("--log-dir", type=Path, default=None, help="root log folder (default: <project>/logs)")
    p.add_argument("--val-frac", type=float, default=0.15, help="fraction of events (newest-before-test) used for validation")
    p.add_argument("--test-frac", type=float, default=0.15, help="fraction of newest events used for testing")
    p.add_argument("--min-chars", type=int, default=20, help="drop notes shorter than this many characters")
    p.add_argument("--seed", type=int, default=42, help="random seed (recorded for reproducibility)")
    return p.parse_args()


def load_raw(input_dir: Path, log) -> pd.DataFrame:
    files = sorted(f for f in Path(input_dir).glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {input_dir}")
    frames = []
    for f in files:
        d = pd.read_csv(f, low_memory=False, encoding="utf-8-sig")
        missing = [c for c in REQUIRED_COLUMNS if c not in d.columns]
        if missing:
            log.warning("Skipping %s - missing required columns: %s", f.name, missing)
            continue
        log.info("Loaded %-80s rows=%d cols=%d", f.name, len(d), d.shape[1])
        d["_source_file"] = f.name
        frames.append(d)
    if not frames:
        raise ValueError("No CSV had the required ACLED columns.")
    df = pd.concat(frames, ignore_index=True)
    log.info("Combined raw data: %d rows", len(df))
    return df


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", " ", s).strip()


def extract_crowd_size(tags) -> object:
    if pd.isna(tags):
        return np.nan
    for t in str(tags).split(";"):
        t = t.strip()
        if t.startswith("crowd size="):
            return t.split("=", 1)[1]
    return np.nan


def label_events(sub_event_type: pd.Series) -> pd.Series:
    return sub_event_type.map(lambda s: 0 if s in PEACEFUL else 1 if s in TENSION else 2)


def time_split(df: pd.DataFrame, val_frac: float, test_frac: float, log):
    """Chronological split. Events on one calendar date never straddle two splits."""
    dates = df["event_date"].sort_values().reset_index(drop=True)
    n = len(dates)
    cut_train = dates.iloc[int(n * (1 - val_frac - test_frac)) - 1]
    cut_val = dates.iloc[int(n * (1 - test_frac)) - 1]
    train = df[df.event_date <= cut_train]
    val = df[(df.event_date > cut_train) & (df.event_date <= cut_val)]
    test = df[df.event_date > cut_val]
    log.info("Split cut dates: train <= %s < val <= %s < test", cut_train.date(), cut_val.date())
    return train, val, test


def describe_split(name: str, d: pd.DataFrame, log) -> dict:
    counts = d["crowd_label"].value_counts().reindex([0, 1, 2], fill_value=0)
    info = {
        "rows": int(len(d)),
        "date_min": str(d.event_date.min().date()), "date_max": str(d.event_date.max().date()),
        "label_counts": {LABEL_NAMES[k]: int(v) for k, v in counts.items()},
        "violent_rate": round(float(d.is_violent.mean()), 4),
        "fatality_rate": round(float(d.has_fatality.mean()), 4),
        "median_words": float(d.text.str.split().str.len().median()),
    }
    log.info("[%-5s] rows=%-7d dates=%s..%s  peaceful=%d tension=%d violent=%d  violent_rate=%.2f%%",
             name, info["rows"], info["date_min"], info["date_max"], counts[0], counts[1], counts[2],
             info["violent_rate"] * 100)
    return info


def main():
    args = parse_args()
    log, log_path, run_id = setup_logger("preprocess", log_dir=args.log_dir)
    log.info("=== PREPROCESS run_id=%s ===", run_id)
    log.info("Log file: %s", log_path)
    log_environment(log, args)
    np.random.seed(args.seed)
    summary = {"run_id": run_id, "args": {k: str(v) for k, v in vars(args).items()}, "steps": {}}

    try:
        with timed_step(log, "load raw CSVs"):
            df = load_raw(args.input_dir, log)
            summary["steps"]["raw_rows"] = int(len(df))

        with timed_step(log, "parse dates + filter crowd events (Protests/Riots)"):
            df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
            bad_dates = int(df.event_date.isna().sum())
            if bad_dates:
                log.warning("Dropping %d rows with unparseable event_date", bad_dates)
                df = df.dropna(subset=["event_date"])
            log.info("Event types before filter: %s", df.event_type.value_counts().to_dict())
            df = df[df.event_type.isin(["Protests", "Riots"])].copy()
            log.info("Rows after crowd filter: %d", len(df))
            summary["steps"]["crowd_rows"] = int(len(df))

        with timed_step(log, "clean text"):
            df["text"] = df["notes"].fillna("").map(clean_text)
            short = df.text.str.len() < args.min_chars
            log.info("Rows with text shorter than %d chars (dropped): %d", args.min_chars, int(short.sum()))
            df = df[~short]
            log.info("Rows after text cleaning: %d", len(df))
            log.info("Text length (chars): %s", df.text.str.len().describe().round(1).to_dict())

        with timed_step(log, "drop duplicates"):
            before = len(df)
            id_dups = int(df.event_id_cnty.duplicated().sum())
            df = df.drop_duplicates(subset="event_id_cnty")
            dup_mask = df.duplicated(subset=["text", "event_date", "location"])
            df = df[~dup_mask]
            log.info("Dropped %d duplicate event ids and %d duplicate (text,date,location) rows; %d -> %d rows",
                     id_dups, int(dup_mask.sum()), before, len(df))
            summary["steps"]["after_dedup_rows"] = int(len(df))

        with timed_step(log, "build labels + features"):
            df["crowd_label"] = label_events(df.sub_event_type)
            df["is_violent"] = (df.crowd_label == 2).astype(int)
            df["has_fatality"] = (df.fatalities > 0).astype(int)
            df["crowd_size"] = df["tags"].map(extract_crowd_size)
            log.info("Label distribution: %s",
                     {LABEL_NAMES[k]: int(v) for k, v in df.crowd_label.value_counts().sort_index().items()})
            log.info("Violent rate overall: %.2f%%  | fatality rate: %.2f%%",
                     df.is_violent.mean() * 100, df.has_fatality.mean() * 100)
            log.info("crowd_size coverage: %.1f%%", df.crowd_size.notna().mean() * 100)
            for col in ("admin2", "actor2", "interaction", "latitude", "longitude"):
                if col not in df.columns:
                    df[col] = np.nan
                    log.warning("Column %s not found in raw data - filled with NaN", col)

        with timed_step(log, "chronological train/val/test split"):
            df = df.sort_values(["event_date", "event_id_cnty"]).reset_index(drop=True)
            train, val, test = time_split(df, args.val_frac, args.test_frac, log)
            splits = {"train": train, "val": val, "test": test}
            summary["splits"] = {k: describe_split(k, v, log) for k, v in splits.items()}
            rates = [s["violent_rate"] for s in summary["splits"].values()]
            if max(rates) - min(rates) > 0.05:
                log.warning("Violent rate differs by >5 points across splits (%s) - temporal drift, "
                            "expect a train/test performance gap.", rates)
            if any(len(v) == 0 for v in splits.values()):
                raise ValueError("A split is empty - check --val-frac/--test-frac")

        with timed_step(log, "class weights + write outputs"):
            counts = train.crowd_label.value_counts().reindex([0, 1, 2], fill_value=0)
            weights3 = (len(train) / (3 * counts.clip(lower=1))).round(4)
            pos = int(train.is_violent.sum()); neg = len(train) - pos
            label_info = {
                "label_names": LABEL_NAMES,
                "class_weights_3class": {LABEL_NAMES[k]: float(v) for k, v in weights3.items()},
                "class_weights_binary": {"non_violent": round(len(train) / (2 * neg), 4),
                                         "violent": round(len(train) / (2 * max(pos, 1)), 4)},
                "train_label_counts": {LABEL_NAMES[k]: int(v) for k, v in counts.items()},
            }
            log.info("Class weights (3-class, from train): %s", label_info["class_weights_3class"])
            log.info("Class weights (binary,  from train): %s", label_info["class_weights_binary"])

            args.output_dir.mkdir(parents=True, exist_ok=True)
            for name, d in splits.items():
                out = args.output_dir / f"{name}.csv"
                d[KEEP_COLUMNS].to_csv(out, index=False, encoding="utf-8")
                log.info("Wrote %-6s %d rows -> %s", name, len(d), out)
            write_json(args.output_dir / "label_info.json", label_info)
            log.info("Wrote label_info.json -> %s", args.output_dir)

        summary["status"] = "success"
        summary_path = write_json(Path(log_path).with_name(f"preprocess_{run_id}_summary.json"), summary)
        log.info("Summary written -> %s", summary_path)
        log.info("=== PREPROCESS finished successfully ===")
        return 0

    except Exception as exc:  # already logged with traceback by timed_step
        log.error("PREPROCESS aborted: %s: %s", type(exc).__name__, exc)
        summary["status"] = f"failed: {type(exc).__name__}: {exc}"
        write_json(Path(log_path).with_name(f"preprocess_{run_id}_summary.json"), summary)
        return 1


if __name__ == "__main__":
    sys.exit(main())
