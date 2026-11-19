"""Shared helpers for every script in scripts/: project paths, logging, timing, JSON output.

Logs are written to   logs/<stage>/<stage>_<YYYYmmdd_HHMMSS>.log
so every run of a stage (preprocess, train, evaluate, ...) gets its own, clearly labelled file.
"""
import json
import logging
import platform
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
LOGS_DIR = PROJECT_ROOT / "logs"
MODELS_DIR = PROJECT_ROOT / "models"
RESULTS_DIR = PROJECT_ROOT / "results"

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def setup_logger(stage: str, run_id: str = None, log_dir=None, level=logging.INFO, name: str = None):
    """Create a logger that writes to the console and to logs/<stage>/<name>.log.

    `name` defaults to "<stage>_<run_id>"; training scripts pass the full run name
    (model + task + timestamp) so each log file says exactly which experiment it belongs to.
    Returns (logger, log_file_path, run_id).
    """
    run_id = run_id or new_run_id()
    stage_dir = Path(log_dir if log_dir else LOGS_DIR) / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / f"{name or f'{stage}_{run_id}'}.log"

    # Windows consoles often use cp1252; avoid crashes on non-ASCII text in log messages.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    logger = logging.getLogger(stage)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path, run_id


def log_environment(logger, args=None):
    """Record the exact environment and arguments of a run (for reproducibility)."""
    logger.info("Python        : %s", sys.version.replace("\n", " "))
    logger.info("Platform      : %s", platform.platform())
    logger.info("Executable    : %s", sys.executable)
    logger.info("Working dir   : %s", Path.cwd())
    logger.info("Project root  : %s", PROJECT_ROOT)
    for pkg in ("numpy", "pandas", "sklearn", "torch", "transformers"):
        try:
            mod = __import__(pkg)
            logger.info("Package       : %s %s", pkg, getattr(mod, "__version__", "?"))
        except Exception:
            logger.info("Package       : %s not installed", pkg)
    if args is not None:
        for key, value in sorted(vars(args).items()):
            logger.info("Arg           : %s = %s", key, value)


@contextmanager
def timed_step(logger, name: str):
    """Log start / end / duration of a pipeline step, and log (then re-raise) any failure."""
    logger.info("START  %s", name)
    start = time.time()
    try:
        yield
    except Exception:
        logger.exception("FAILED %s after %.1fs", name, time.time() - start)
        raise
    logger.info("DONE   %s (%.1fs)", name, time.time() - start)


# Words that (almost) spell out the label. Used by the optional --mask-cues experiment, which
# replaces them so the model has to rely on context instead of keyword matching.
CUE_WORDS = ["clash", "stone", "pelt", "violen", "riot", "arson", "torch", "vandal", "set fire", "set ablaze",
             "lynch", "injur", "killed", "ransack", "attack", "beat", "thrash"]
_CUE_RE = re.compile(r"\b\w*(?:" + "|".join(re.escape(w) for w in CUE_WORDS) + r")\w*\b", re.IGNORECASE)


def mask_cues(text: str, replacement: str = "[MASK]") -> str:
    return _CUE_RE.sub(replacement, text)


def set_seed(seed: int):
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return path
