"""Stream the raw tweet files in data/social/*.json.gz and aggregate them to a country-day table.

The raw files (G-CUT style) hold one JSON object per line and NO tweet text:
    tweet_id, created_at, country_code, country_code_alpha3, civil_unrest_score, civil_unrest_related

So everything downstream works on counts and classifier scores per country and day, not on words.

Outputs (data/social_processed/):
    country_day.csv        one row per (country, date): tweets, related tweets, score stats
    aggregate_summary.json totals, per-year / per-country counts, score histogram, flag-vs-score check
Log: logs/social_aggregate/social_aggregate_<runid>.log

Usage:
    python scripts/social_aggregate.py                      # all files, one process (about 10 minutes)
    python scripts/social_aggregate.py --workers 2          # one process per file, faster on 2+ cores
    python scripts/social_aggregate.py --max-lines 1000000  # quick test on the first 1M lines per file
"""
import argparse
import csv
import gzip
import json
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from common import DATA_DIR, log_environment, setup_logger, timed_step, write_json

MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
HIST_BINS = 20  # score histogram, bin width 0.05


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input-dir", type=Path, default=DATA_DIR / "social")
    p.add_argument("--output-dir", type=Path, default=DATA_DIR / "social_processed")
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--files", nargs="*", default=None, help="only these file names inside --input-dir")
    p.add_argument("--workers", type=int, default=1, help="processes (one file per process)")
    p.add_argument("--max-lines", type=int, default=None, help="stop after this many lines per file (testing)")
    p.add_argument("--progress-every", type=int, default=5_000_000)
    p.add_argument("--merge-parts", type=Path, default=None,
                   help="folder whose sub-folders each hold one run's country_day.csv + aggregate_summary.json")
    return p.parse_args()


def created_to_date(created: str) -> str:
    """'Wed Jan 01 05:00:02 +0000 2014' -> '2014-01-01' (all timestamps are UTC)."""
    return f"{created[-4:]}-{MONTHS[created[4:7]]}-{created[8:10]}"


def parse_line(line):
    """Return (country, date, score, related). Fast path for the fixed layout, json as the fallback."""
    try:
        parts = line.split('"')
        created, cc = parts[5], parts[9]
        tail = line.rsplit(": ", 2)
        score = float(tail[1].split(",")[0])
        related = tail[2].startswith("true")
        return cc, created_to_date(created), score, related
    except Exception:
        d = json.loads(line)
        return (d["country_code"], created_to_date(d["created_at"]),
                float(d["civil_unrest_score"]), bool(d["civil_unrest_related"]))


def check_fast_path(path, n=2000):
    """The fast parser must agree with json on the first n lines, otherwise it is not used."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= n:
                break
            d = json.loads(line)
            expected = (d["country_code"], created_to_date(d["created_at"]),
                        float(d["civil_unrest_score"]), bool(d["civil_unrest_related"]))
            if parse_line(line) != expected:
                return False
    return True


def process_file(path, max_lines=None, progress_every=0, log=None):
    """Aggregate one file. Returns a dict of partial results (merged by the caller)."""
    cells = {}                      # (country, date) -> [n, n_related, sum_score, max_score, n_high]
    per_year, per_year_rel = Counter(), Counter()
    per_country, per_country_rel = Counter(), Counter()
    hist = [0] * HIST_BINS
    min_rel, max_unrel = 1.0, 0.0
    bad, n_file = 0, 0
    t0 = time.time()
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if max_lines and n_file >= max_lines:
                break
            try:
                cc, date, score, rel = parse_line(line)
            except Exception:
                bad += 1
                continue
            n_file += 1
            key = (cc, date)
            c = cells.get(key)
            if c is None:
                c = cells[key] = [0, 0, 0.0, 0.0, 0]
            c[0] += 1
            c[2] += score
            if score > c[3]:
                c[3] = score
            if score >= 0.5:
                c[4] += 1
            year = date[:4]
            per_year[year] += 1
            per_country[cc] += 1
            hist[min(int(score * HIST_BINS), HIST_BINS - 1)] += 1
            if rel:
                c[1] += 1
                per_year_rel[year] += 1
                per_country_rel[cc] += 1
                if score < min_rel:
                    min_rel = score
            elif score > max_unrel:
                max_unrel = score
            if log and progress_every and n_file % progress_every == 0:
                log("%s: %s lines | %s cells | %.0f lines/s" % (
                    Path(path).name, f"{n_file:,}", f"{len(cells):,}", n_file / max(time.time() - t0, 1e-9)))
    return {"file": Path(path).name, "lines": n_file, "bad": bad, "cells": cells,
            "per_year": per_year, "per_year_rel": per_year_rel, "per_country": per_country,
            "per_country_rel": per_country_rel, "hist": hist, "min_rel": min_rel, "max_unrel": max_unrel,
            "seconds": time.time() - t0}


def _worker(args):
    return process_file(*args)


def merge_parts(parts_dir, output_dir, logger):
    """Combine country_day.csv / aggregate_summary.json written by separate runs (one folder per file).

    Useful when each file is processed in its own run; the result equals a single full run.
    """
    part_dirs = sorted(p for p in Path(parts_dir).iterdir() if (p / "country_day.csv").exists())
    if not part_dirs:
        raise SystemExit(f"No part folders with country_day.csv under {parts_dir}")
    cells = {}
    per_year, per_year_rel, per_country, per_country_rel = Counter(), Counter(), Counter(), Counter()
    hist = [0] * HIST_BINS
    min_rel, max_unrel, bad, total = 1.0, 0.0, 0, 0
    for pd_ in part_dirs:
        s = json.loads((pd_ / "aggregate_summary.json").read_text(encoding="utf-8"))
        logger.info("merging %s: %s lines", pd_.name, f"{s['total_valid_lines']:,}")
        total += s["total_valid_lines"]
        bad += s["malformed_lines"]
        for y, v in s["per_year"].items():
            per_year[y] += v["tweets"]
            per_year_rel[y] += v["related"]
        for c, v in s["per_country"].items():
            per_country[c] += v["tweets"]
            per_country_rel[c] += v["related"]
        hist = [a + b for a, b in zip(hist, s["score_histogram_bin_width_0.05"])]
        min_rel = min(min_rel, s["min_score_among_related"])
        max_unrel = max(max_unrel, s["max_score_among_unrelated"])
        with open(pd_ / "country_day.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                n = int(r["n_tweets"])
                v = [n, int(r["n_related"]), float(r["mean_score"]) * n, float(r["max_score"]), int(r["n_high"])]
                key = (r["country"], r["date"])
                c = cells.get(key)
                if c is None:
                    cells[key] = v
                else:
                    c[0] += v[0]; c[1] += v[1]; c[2] += v[2]; c[3] = max(c[3], v[3]); c[4] += v[4]
    write_outputs(output_dir, cells, per_year, per_year_rel, per_country, per_country_rel, hist,
                  min_rel, max_unrel, bad, total, 0.0, "merged", {"parts": [p.name for p in part_dirs]}, logger, None)


def write_outputs(output_dir, cells, per_year, per_year_rel, per_country, per_country_rel, hist,
                  min_rel, max_unrel, bad, total, elapsed, run_id, args_dict, logger, log_path):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_csv = output_dir / "country_day.csv"
    with timed_step(logger, "write country_day.csv"):
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["country", "date", "n_tweets", "n_related", "mean_score", "max_score", "n_high"])
            for (cc, date), (n, nr, s, mx, nh) in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                w.writerow([cc, date, n, nr, round(s / n, 6), round(mx, 6), nh])
    summary = {
        "run_id": run_id,
        "args": args_dict,
        "total_valid_lines": total,
        "malformed_lines": bad,
        "country_day_cells": len(cells),
        "per_year": {y: {"tweets": per_year[y], "related": per_year_rel[y]} for y in sorted(per_year)},
        "per_country": {c: {"tweets": per_country[c], "related": per_country_rel[c]}
                        for c, _ in per_country.most_common()},
        "score_histogram_bin_width_0.05": hist,
        "min_score_among_related": min_rel,
        "max_score_among_unrelated": max_unrel,
        "elapsed_seconds": round(elapsed, 1),
    }
    write_json(output_dir / "aggregate_summary.json", summary)
    if log_path is not None:
        write_json(log_path.with_name(log_path.stem + "_summary.json"), summary)
    logger.info("Total valid lines: %s | malformed: %s | country-day cells: %s",
                f"{total:,}", f"{bad:,}", f"{len(cells):,}")
    logger.info("Related share overall: %.4f", sum(per_year_rel.values()) / max(total, 1))
    logger.info("Score threshold check: min score among related=%.4f, max among unrelated=%.4f", min_rel, max_unrel)
    logger.info("Wrote %s", out_csv)


def main():
    args = parse_args()
    logger, log_path, run_id = setup_logger("social_aggregate", log_dir=args.log_dir)
    log_environment(logger, args)
    logger.info("=== SOCIAL AGGREGATE started (run %s) | log file: %s ===", run_id, log_path)

    if args.merge_parts:
        merge_parts(args.merge_parts, args.output_dir, logger)
        logger.info("=== SOCIAL AGGREGATE (merge) finished successfully ===")
        return

    files = sorted(args.input_dir.glob("*.json.gz"))
    if args.files:
        files = [f for f in files if f.name in set(args.files)]
    if not files:
        logger.error("No matching *.json.gz files in %s", args.input_dir)
        raise SystemExit(1)
    logger.info("Input files: %s", [f.name for f in files])
    logger.info("Fast parser agrees with json on the first lines of every file: %s",
                all(check_fast_path(f) for f in files))

    t0 = time.time()
    if args.workers > 1:
        logger.info("Processing %d files with %d worker processes (no per-line progress in this mode)",
                    len(files), args.workers)
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            parts = list(ex.map(_worker, [(f, args.max_lines, 0, None) for f in files]))
    else:
        parts = []
        for f in files:
            with timed_step(logger, f"read {f.name}"):
                parts.append(process_file(f, args.max_lines, args.progress_every, logger.info))

    # merge partial results
    cells = {}
    per_year, per_year_rel, per_country, per_country_rel = Counter(), Counter(), Counter(), Counter()
    hist = [0] * HIST_BINS
    min_rel, max_unrel, bad, total = 1.0, 0.0, 0, 0
    for p in parts:
        logger.info("%s: %s valid lines, %s malformed, %.0fs", p["file"], f"{p['lines']:,}", p["bad"], p["seconds"])
        total += p["lines"]
        bad += p["bad"]
        per_year.update(p["per_year"])
        per_year_rel.update(p["per_year_rel"])
        per_country.update(p["per_country"])
        per_country_rel.update(p["per_country_rel"])
        hist = [a + b for a, b in zip(hist, p["hist"])]
        min_rel = min(min_rel, p["min_rel"])
        max_unrel = max(max_unrel, p["max_unrel"])
        for key, v in p["cells"].items():
            c = cells.get(key)
            if c is None:
                cells[key] = list(v)
            else:
                c[0] += v[0]; c[1] += v[1]; c[2] += v[2]; c[3] = max(c[3], v[3]); c[4] += v[4]
    write_outputs(args.output_dir, cells, per_year, per_year_rel, per_country, per_country_rel, hist,
                  min_rel, max_unrel, bad, total, time.time() - t0, run_id,
                  {k: str(v) for k, v in vars(args).items()}, logger, log_path)
    logger.info("=== SOCIAL AGGREGATE finished successfully in %.1fs ===", time.time() - t0)


if __name__ == "__main__":
    main()
