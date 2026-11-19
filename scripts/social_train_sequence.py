"""Train a neural sequence model (Transformer encoder or GRU) on 28-day windows of daily tweet signals.

Each example is one country and day: the previous 28 days of per-day features
(log tweet volume, log unrest-related tweets, unrest share, mean/max classifier score, z-score, ...) go in,
and one head per horizon predicts "a surge happens within the next h days" for h = 1, 3, 7.

Training: class-weighted binary cross-entropy, AdamW, early stopping on validation mean PR-AUC,
thresholds tuned for F1 per horizon on validation and applied unchanged to test.

Output: models/<run>/{best.pt, run_config.json}  results/<run>/{metrics.json, predictions_test.csv, history.json}
Log   : logs/social_train_sequence/<run>.log   (run = social_<arch>_<timestamp>)

Usage:
    python scripts/social_train_sequence.py
    python scripts/social_train_sequence.py --arch gru --seed 43
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import average_precision_score

from common import MODELS_DIR, RESULTS_DIR, log_environment, new_run_id, set_seed, setup_logger, timed_step, write_json
from social_common import (DAY_FEATURES, HORIZONS, SOCIAL_PROCESSED, best_f1_threshold, binary_metrics,
                           format_metrics, load_panel)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--panel", type=Path, default=SOCIAL_PROCESSED / "panel.csv.gz")
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--arch", choices=["transformer", "gru"], default="transformer")
    p.add_argument("--window", type=int, default=28)
    p.add_argument("--d-model", type=int, default=64)
    p.add_argument("--layers", type=int, default=2)
    p.add_argument("--heads", type=int, default=4)
    p.add_argument("--dropout", type=float, default=0.2)
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=1e-2)
    p.add_argument("--patience", type=int, default=6)
    p.add_argument("--horizons", type=int, nargs="+", default=list(HORIZONS))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-train-samples", type=int, default=None, help="for smoke tests")
    return p.parse_args()


def build_windows(panel, window, feats, horizons):
    """Return X (N, window, d), Y (N, H) with NaN where unlabeled, and an info frame for the N examples."""
    panel = panel.sort_values(["country", "segment", "date"]).reset_index(drop=True)
    panel["z_today"] = panel["z_today"].clip(-5, 10).fillna(0)
    values = panel[feats].to_numpy(dtype=np.float32)
    targets = panel[[f"target_h{h}" for h in horizons]].to_numpy(dtype=np.float32)
    idx = np.where((panel["eligible"] == 1).to_numpy() & ~np.isnan(targets).all(axis=1))[0]
    grp = (panel["country"].astype(str) + "_" + panel["segment"].astype(int).astype(str)).to_numpy()
    keep = [i for i in idx if i >= window - 1 and grp[i - window + 1] == grp[i]]
    keep = np.array(keep)
    X = np.stack([values[i - window + 1:i + 1] for i in keep])
    return X, targets[keep], panel.loc[keep, ["country", "date", "split"]].reset_index(drop=True), panel


class SeqModel(nn.Module):
    def __init__(self, d_in, window, args, n_out):
        super().__init__()
        self.arch = args.arch
        self.proj = nn.Linear(d_in, args.d_model)
        if args.arch == "transformer":
            self.pos = nn.Parameter(torch.zeros(window, args.d_model))
            nn.init.normal_(self.pos, std=0.02)
            layer = nn.TransformerEncoderLayer(args.d_model, args.heads, dim_feedforward=2 * args.d_model,
                                               dropout=args.dropout, batch_first=True, activation="gelu")
            self.encoder = nn.TransformerEncoder(layer, num_layers=args.layers)
        else:
            self.encoder = nn.GRU(args.d_model, args.d_model, num_layers=args.layers, batch_first=True,
                                  dropout=args.dropout if args.layers > 1 else 0.0)
        self.norm = nn.LayerNorm(args.d_model)
        self.drop = nn.Dropout(args.dropout)
        self.head = nn.Linear(args.d_model, n_out)

    def forward(self, x):
        h = self.proj(x)
        if self.arch == "transformer":
            h = self.encoder(h + self.pos)
        else:
            h, _ = self.encoder(h)
        return self.head(self.drop(self.norm(h[:, -1])))


@torch.no_grad()
def predict(model, X, device, batch=1024):
    model.eval()
    out = []
    for i in range(0, len(X), batch):
        out.append(torch.sigmoid(model(torch.from_numpy(X[i:i + batch]).to(device))).cpu().numpy())
    return np.concatenate(out)


def mean_pr_auc(Y, P):
    vals = []
    for j in range(Y.shape[1]):
        m = ~np.isnan(Y[:, j])
        if m.sum() and len(np.unique(Y[m, j])) == 2:
            vals.append(average_precision_score(Y[m, j], P[m, j]))
    return float(np.mean(vals)) if vals else 0.0


def main():
    args = parse_args()
    run_name = f"social_{args.arch}_{new_run_id()}_seed{args.seed}"
    logger, log_path, run_id = setup_logger("social_train_sequence", log_dir=args.log_dir, name=run_name)
    log_environment(logger, args)
    logger.info("=== SOCIAL TRAIN SEQUENCE started (%s) | log file: %s ===", run_name, log_path)
    set_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Device: %s", device)

    with timed_step(logger, "build windows"):
        panel = load_panel(args.panel)
        X, Y, info, _ = build_windows(panel, args.window, DAY_FEATURES, args.horizons)
        logger.info("examples: %d | window=%d | features=%d | horizons=%s", len(X), args.window, X.shape[2], args.horizons)

    split = info["split"].to_numpy()
    tr, va, te = split == "train", split == "val", split == "test"
    if args.max_train_samples:
        keep = np.where(tr)[0][:args.max_train_samples]
        tr = np.zeros_like(tr)
        tr[keep] = True
    mean = X[tr].reshape(-1, X.shape[2]).mean(0)
    std = X[tr].reshape(-1, X.shape[2]).std(0) + 1e-6
    X = (X - mean) / std
    logger.info("train/val/test examples: %d/%d/%d", tr.sum(), va.sum(), te.sum())

    pos_weight = []
    for j, h in enumerate(args.horizons):
        y = Y[tr, j]
        y = y[~np.isnan(y)]
        pos_weight.append((len(y) - y.sum()) / max(y.sum(), 1))
        logger.info("h=%d train positive rate %.4f -> pos_weight %.1f", h, y.mean(), pos_weight[-1])
    pos_weight_t = torch.tensor(pos_weight, dtype=torch.float32, device=device)

    model = SeqModel(X.shape[2], args.window, args, len(args.horizons)).to(device)
    logger.info("Model: %s | parameters: %s", args.arch, f"{sum(p.numel() for p in model.parameters()):,}")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    Xtr = torch.from_numpy(X[tr])
    Ytr = torch.from_numpy(Y[tr])

    model_dir = args.models_dir / run_name
    res_dir = args.results_dir / run_name
    model_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)
    history, best, bad_epochs = [], -1.0, 0
    t0 = time.time()
    for epoch in range(1, args.epochs + 1):
        model.train()
        perm = torch.randperm(len(Xtr))
        total, count = 0.0, 0
        for i in range(0, len(perm), args.batch_size):
            b = perm[i:i + args.batch_size]
            xb, yb = Xtr[b].to(device), Ytr[b].to(device)
            mask = ~torch.isnan(yb)
            logits = model(xb)
            loss = nn.functional.binary_cross_entropy_with_logits(
                logits, torch.nan_to_num(yb), pos_weight=pos_weight_t, reduction="none")
            loss = (loss * mask).sum() / mask.sum().clamp(min=1)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += loss.item() * len(b)
            count += len(b)
        val_pred = predict(model, X[va], device)
        val_score = mean_pr_auc(Y[va], val_pred)
        per_h = {f"h{h}": round(float(average_precision_score(Y[va][~np.isnan(Y[va][:, j]), j],
                                                              val_pred[~np.isnan(Y[va][:, j]), j])), 4)
                 for j, h in enumerate(args.horizons)}
        history.append({"epoch": epoch, "train_loss": total / count, "val_mean_pr_auc": val_score, "val_pr_auc": per_h})
        improved = val_score > best
        logger.info("epoch %2d | train_loss %.4f | val PR-AUC mean %.4f %s | %.0fs%s", epoch, total / count, val_score,
                    per_h, time.time() - t0, "  <- best, saved" if improved else "")
        if improved:
            best, bad_epochs = val_score, 0
            torch.save(model.state_dict(), model_dir / "best.pt")
        else:
            bad_epochs += 1
            if bad_epochs >= args.patience:
                logger.info("Early stopping (no improvement for %d epochs)", args.patience)
                break

    model.load_state_dict(torch.load(model_dir / "best.pt", map_location=device))
    val_pred, test_pred = predict(model, X[va], device), predict(model, X[te], device)
    metrics, frames = {}, []
    for j, h in enumerate(args.horizons):
        mv, mt = ~np.isnan(Y[va][:, j]), ~np.isnan(Y[te][:, j])
        thr = best_f1_threshold(Y[va][mv, j], val_pred[mv, j])
        res = {"val": binary_metrics(Y[va][mv, j], val_pred[mv, j], thr),
               "test": binary_metrics(Y[te][mt, j], test_pred[mt, j], thr)}
        metrics[f"seq_{args.arch}_h{h}"] = res
        logger.info(format_metrics(f"TEST seq_{args.arch} h={h}", res["test"]))
        sub = info[te].reset_index(drop=True)[mt]
        frames.append(pd.DataFrame({"model": f"seq_{args.arch}", "horizon": h, "country": sub["country"].to_numpy(),
                                    "date": sub["date"].to_numpy(), "y_true": Y[te][mt, j].astype(int),
                                    "score": test_pred[mt, j], "threshold": thr}))
    pd.concat(frames).to_csv(res_dir / "predictions_test.csv", index=False)
    write_json(res_dir / "metrics.json", {"run_name": run_name, "args": {k: str(v) for k, v in vars(args).items()},
                                          "features": DAY_FEATURES, "best_val_mean_pr_auc": best, "metrics": metrics})
    write_json(res_dir / "history.json", history)
    write_json(model_dir / "run_config.json", {"run_name": run_name, "arch": args.arch, "window": args.window,
                                               "features": DAY_FEATURES, "horizons": args.horizons,
                                               "feature_mean": mean.tolist(), "feature_std": std.tolist(),
                                               "args": {k: str(v) for k, v in vars(args).items()}})
    write_json(log_path.with_name(log_path.stem + "_summary.json"),
               {"run_name": run_name, "best_val_mean_pr_auc": best, "epochs_run": len(history),
                "test_f1": {k: v["test"]["f1"] for k, v in metrics.items()},
                "test_pr_auc": {k: v["test"]["pr_auc"] for k, v in metrics.items()}})
    logger.info("=== SOCIAL TRAIN SEQUENCE finished successfully in %.0fs ===", time.time() - t0)


if __name__ == "__main__":
    main()
