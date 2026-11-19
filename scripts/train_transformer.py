"""Step 2b - fine-tune a Hugging Face transformer to classify whether a crowd event is violent.

Usage (defaults: distilroberta-base, binary task, 3 epochs):
  python scripts/train_transformer.py
  python scripts/train_transformer.py --model roberta-base --epochs 3 --batch-size 16
  python scripts/train_transformer.py --task 3class
  python scripts/train_transformer.py --mask-cues                     # leakage experiment
  python scripts/train_transformer.py --max-train-samples 3000 --epochs 1   # quick sanity run

Good model choices:  distilroberta-base (fast) | roberta-base | bert-base-uncased |
                     cardiffnlp/twitter-roberta-base (pre-trained on tweets - closest to the social-media target) |
                     cardiffnlp/twitter-roberta-base-sentiment-latest (tweet sentiment pre-training)

Outputs:
  models/<run_name>/best/            best checkpoint (by validation primary metric) + tokenizer
  models/<run_name>/run_config.json
  results/<run_name>/                history.json, metrics.json, predictions_test.csv
  logs/train_transformer/train_transformer_<run_name>.log
"""
import argparse
import json
import math
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup
except ImportError as e:  # friendly message instead of a bare traceback
    sys.exit(f"Missing dependency ({e}). Install with:  pip install torch transformers scikit-learn")

from common import (MODELS_DIR, PROCESSED_DIR, RESULTS_DIR, log_environment, mask_cues, new_run_id, set_seed,
                    setup_logger, timed_step, write_json)
from metrics_utils import (LABEL_COLUMN, best_f1_threshold, class_names, compute_metrics, format_metrics,
                           predict_labels)


def parse_args():
    p = argparse.ArgumentParser(description="Fine-tune a transformer for violence prediction.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--model", default="distilroberta-base", help="HF model name or local path")
    p.add_argument("--task", choices=["binary", "3class"], default="binary")
    p.add_argument("--data-dir", type=Path, default=PROCESSED_DIR)
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--run-name", default=None)
    p.add_argument("--mask-cues", action="store_true", help="mask label-revealing keywords in the input text")
    p.add_argument("--max-length", type=int, default=128)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--grad-accum", type=int, default=1, help="gradient accumulation steps")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--weight-decay", type=float, default=0.01)
    p.add_argument("--warmup-ratio", type=float, default=0.1)
    p.add_argument("--max-grad-norm", type=float, default=1.0)
    p.add_argument("--class-weights", choices=["auto", "none"], default="auto",
                   help="auto = inverse-frequency weights from label_info.json")
    p.add_argument("--patience", type=int, default=2, help="early-stop after N epochs without improvement")
    p.add_argument("--fp16", choices=["auto", "on", "off"], default="auto", help="mixed precision (CUDA only)")
    p.add_argument("--num-workers", type=int, default=0, help="DataLoader workers (keep 0 on Windows)")
    p.add_argument("--log-every", type=int, default=50, help="log training loss every N optimizer steps")
    p.add_argument("--max-train-samples", type=int, default=None)
    p.add_argument("--max-eval-samples", type=int, default=None)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


class EncodedDataset(Dataset):
    def __init__(self, enc, labels):
        self.ids, self.mask, self.labels = enc["input_ids"], enc["attention_mask"], labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        return {"input_ids": self.ids[i], "attention_mask": self.mask[i]}, int(self.labels[i])


def make_loader(ds, tokenizer, batch_size, shuffle, num_workers, pin):
    def collate(items):
        batch = tokenizer.pad([x for x, _ in items], return_tensors="pt")
        batch["labels"] = torch.tensor([y for _, y in items], dtype=torch.long)
        return batch
    return DataLoader(ds, batch_size=batch_size, shuffle=shuffle, collate_fn=collate,
                      num_workers=num_workers, pin_memory=pin)


def load_split(path, args, tokenizer, label_col, limit, log, name):
    d = pd.read_csv(path, encoding="utf-8")
    if limit and len(d) > limit:
        d = d.sample(limit, random_state=args.seed).sort_values("event_date").reset_index(drop=True)
    texts = d["text"].fillna("").astype(str)
    if args.mask_cues:
        texts = texts.map(lambda t: mask_cues(t, tokenizer.mask_token or "[MASK]"))
    enc = tokenizer(texts.tolist(), truncation=True, max_length=args.max_length)
    lens = np.array([len(x) for x in enc["input_ids"]])
    log.info("[%s] rows=%d  tokens: mean=%.1f p95=%d max=%d  truncated=%.1f%%",
             name, len(d), lens.mean(), np.percentile(lens, 95), lens.max(), (lens >= args.max_length).mean() * 100)
    return d, enc, d[label_col].to_numpy()


@torch.no_grad()
def predict(model, loader, device, use_amp):
    model.eval()
    out = []
    for batch in loader:
        batch.pop("labels")
        batch = {k: v.to(device) for k, v in batch.items()}
        ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if use_amp else nullcontext()
        with ctx:
            logits = model(**batch).logits
        out.append(torch.softmax(logits.float(), dim=-1).cpu().numpy())
    return np.concatenate(out)


def make_scaler(enabled):
    try:
        return torch.amp.GradScaler("cuda", enabled=enabled)
    except (AttributeError, TypeError):
        return torch.cuda.amp.GradScaler(enabled=enabled)


def main():
    args = parse_args()
    run_id = new_run_id()
    short = args.model.replace("\\", "/").rstrip("/").split("/")[-1]
    run_name = args.run_name or f"{short}_{args.task}{'_masked' if args.mask_cues else ''}_{run_id}"
    log, log_path, _ = setup_logger("train_transformer", run_id=run_id, log_dir=args.log_dir,
                                    name=f"train_transformer_{run_name}")
    log.info("=== TRAIN TRANSFORMER run_name=%s ===", run_name)
    log.info("Log file: %s", log_path)
    log_environment(log, args)
    set_seed(args.seed)
    label_col = LABEL_COLUMN[args.task]
    names = class_names(args.task)

    try:
        # ---- device ----
        if torch.cuda.is_available():
            device = torch.device("cuda")
            log.info("Device: CUDA - %s (%.1f GB)", torch.cuda.get_device_name(0),
                     torch.cuda.get_device_properties(0).total_memory / 1e9)
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = torch.device("mps"); log.info("Device: Apple MPS")
        else:
            device = torch.device("cpu")
            log.warning("Device: CPU only - training will be slow. Use --max-train-samples for quick runs, "
                        "or a GPU / Google Colab for full runs.")
        use_amp = device.type == "cuda" and args.fp16 != "off"
        log.info("Mixed precision (fp16): %s", use_amp)

        # ---- data ----
        with timed_step(log, "load + tokenize data"):
            tokenizer = AutoTokenizer.from_pretrained(args.model)
            tr, enc_tr, y_tr = load_split(args.data_dir / "train.csv", args, tokenizer, label_col, args.max_train_samples, log, "train")
            va, enc_va, y_va = load_split(args.data_dir / "val.csv", args, tokenizer, label_col, args.max_eval_samples, log, "val")
            te, enc_te, y_te = load_split(args.data_dir / "test.csv", args, tokenizer, label_col, args.max_eval_samples, log, "test")
            for n, y in (("train", y_tr), ("val", y_va), ("test", y_te)):
                log.info("Label counts %-5s: %s", n, {int(k): int(v) for k, v in zip(*np.unique(y, return_counts=True))})
            if args.mask_cues:
                log.info("Masking ON, example: %s", tokenizer.decode(enc_tr["input_ids"][0]))
            pin = device.type == "cuda"
            train_loader = make_loader(EncodedDataset(enc_tr, y_tr), tokenizer, args.batch_size, True, args.num_workers, pin)
            val_loader = make_loader(EncodedDataset(enc_va, y_va), tokenizer, args.batch_size * 2, False, args.num_workers, pin)
            test_loader = make_loader(EncodedDataset(enc_te, y_te), tokenizer, args.batch_size * 2, False, args.num_workers, pin)

        # ---- model / optimiser ----
        with timed_step(log, "build model"):
            model = AutoModelForSequenceClassification.from_pretrained(
                args.model, num_labels=len(names), ignore_mismatched_sizes=True,  # e.g. sentiment models ship a 3-label head
                id2label={i: n for i, n in enumerate(names)}, label2id={n: i for i, n in enumerate(names)}).to(device)
            n_params = sum(p.numel() for p in model.parameters())
            log.info("Model: %s | parameters: %.1fM", args.model, n_params / 1e6)

            weight = None
            if args.class_weights == "auto":
                with open(args.data_dir / "label_info.json", encoding="utf-8") as f:
                    info = json.load(f)
                w = info["class_weights_binary" if args.task == "binary" else "class_weights_3class"]
                weight = torch.tensor([float(w[n]) for n in names], dtype=torch.float, device=device)
                log.info("Class weights: %s", dict(zip(names, weight.tolist())))
            loss_fn = torch.nn.CrossEntropyLoss(weight=weight)

            no_decay = ("bias", "LayerNorm.weight", "layer_norm.weight")
            groups = [
                {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], "weight_decay": args.weight_decay},
                {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0}]
            optimizer = torch.optim.AdamW(groups, lr=args.lr)
            steps_per_epoch = math.ceil(len(train_loader) / args.grad_accum)
            total_steps = steps_per_epoch * args.epochs
            scheduler = get_linear_schedule_with_warmup(optimizer, int(args.warmup_ratio * total_steps), total_steps)
            scaler = make_scaler(use_amp)
            log.info("Batches/epoch=%d | optimizer steps/epoch=%d | total steps=%d | effective batch=%d",
                     len(train_loader), steps_per_epoch, total_steps, args.batch_size * args.grad_accum)

        model_dir = args.models_dir / run_name
        best_dir = model_dir / "best"
        res_dir = args.results_dir / run_name
        model_dir.mkdir(parents=True, exist_ok=True)
        res_dir.mkdir(parents=True, exist_ok=True)
        config = {"model_type": "transformer", "run_name": run_name, "base_model": args.model, "task": args.task,
                  "mask_cues": args.mask_cues, "max_length": args.max_length, "label_names": names,
                  "threshold": None, "args": {k: str(v) for k, v in vars(args).items()}}
        write_json(model_dir / "run_config.json", config)

        # ---- training ----
        history, best_score, best_epoch, bad_epochs, best_val_probs = [], -1.0, 0, 0, None
        global_step = 0
        train_start = time.time()
        for epoch in range(1, args.epochs + 1):
            with timed_step(log, f"epoch {epoch}/{args.epochs}"):
                model.train()
                optimizer.zero_grad(set_to_none=True)
                run_loss, run_n, t0 = 0.0, 0, time.time()
                for i, batch in enumerate(train_loader, 1):
                    labels = batch.pop("labels").to(device)
                    batch = {k: v.to(device) for k, v in batch.items()}
                    ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if use_amp else nullcontext()
                    with ctx:
                        logits = model(**batch).logits
                    loss = loss_fn(logits.float(), labels)
                    scaler.scale(loss / args.grad_accum).backward()
                    run_loss += loss.item() * len(labels)
                    run_n += len(labels)
                    if i % args.grad_accum == 0 or i == len(train_loader):
                        scaler.unscale_(optimizer)
                        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                        scaler.step(optimizer)
                        scaler.update()
                        scheduler.step()
                        optimizer.zero_grad(set_to_none=True)
                        global_step += 1
                        if global_step % args.log_every == 0:
                            elapsed = time.time() - t0
                            done = i / len(train_loader)
                            log.info("epoch %d | step %d/%d | loss %.4f | lr %.2e | %.0f samples/s | epoch ETA %.0fs",
                                     epoch, global_step, total_steps, run_loss / run_n, scheduler.get_last_lr()[0],
                                     run_n / max(elapsed, 1e-9), elapsed / done - elapsed)
                train_loss = run_loss / max(run_n, 1)

                val_probs = predict(model, val_loader, device, use_amp)
                vm = compute_metrics(y_va, val_probs, args.task)
                log.info("\n%s", format_metrics(vm, f"VALIDATION after epoch {epoch}"))
                history.append({"epoch": epoch, "train_loss": train_loss, "val_primary": vm["primary"],
                                "val_macro_f1": vm["macro_f1"], "val_f1_violent": vm["f1_violent"],
                                "val_pr_auc_violent": vm["pr_auc_violent"], "epoch_seconds": time.time() - t0})
                log.info("epoch %d summary: train_loss=%.4f val_%s=%.4f", epoch, train_loss, vm["primary_name"], vm["primary"])

                if vm["primary"] > best_score:
                    best_score, best_epoch, bad_epochs, best_val_probs = vm["primary"], epoch, 0, val_probs
                    model.save_pretrained(best_dir)
                    tokenizer.save_pretrained(best_dir)
                    log.info("New best (%s=%.4f) - checkpoint saved -> %s", vm["primary_name"], best_score, best_dir)
                else:
                    bad_epochs += 1
                    log.info("No improvement for %d epoch(s) (best=%.4f at epoch %d)", bad_epochs, best_score, best_epoch)
            write_json(res_dir / "history.json", history)
            if bad_epochs >= args.patience:
                log.warning("Early stopping at epoch %d", epoch)
                break
        log.info("Training finished in %.1f min. Best epoch=%d, val primary=%.4f",
                 (time.time() - train_start) / 60, best_epoch, best_score)

        # ---- final test evaluation with the BEST checkpoint ----
        with timed_step(log, "test evaluation (best checkpoint)"):
            del model
            if device.type == "cuda":
                torch.cuda.empty_cache()
            model = AutoModelForSequenceClassification.from_pretrained(best_dir).to(device)
            test_probs = predict(model, test_loader, device, use_amp)
            metrics = {"best_epoch": best_epoch, "val": compute_metrics(y_va, best_val_probs, args.task),
                       "test": compute_metrics(y_te, test_probs, args.task)}
            log.info("\n%s", format_metrics(metrics["val"], "VALIDATION (best epoch)"))
            log.info("\n%s", format_metrics(metrics["test"], "TEST (default decision rule)"))
            if args.task == "binary":
                thr = best_f1_threshold(y_va, best_val_probs[:, 1])
                metrics["val_tuned_threshold"] = thr
                metrics["test_at_val_threshold"] = compute_metrics(y_te, test_probs, args.task, threshold=thr)
                config["threshold"] = thr
                log.info("Threshold tuned on validation: %.3f", thr)
                log.info("\n%s", format_metrics(metrics["test_at_val_threshold"], "TEST at validation-tuned threshold"))
            config.update(best_epoch=best_epoch, best_val_primary=best_score)
            write_json(model_dir / "run_config.json", config)
            write_json(res_dir / "metrics.json", metrics)
            pred = te[["event_id_cnty", "event_date", "admin1"]].copy()
            pred["y_true"] = y_te
            pred["y_pred"] = predict_labels(test_probs, args.task, None)
            for i, n in enumerate(names):
                pred[f"p_{n}"] = test_probs[:, i]
            pred.to_csv(res_dir / "predictions_test.csv", index=False)
            log.info("Model   -> %s", best_dir)
            log.info("Results -> %s", res_dir)
        log.info("=== TRAIN TRANSFORMER finished successfully ===")
        return 0
    except Exception as exc:  # full traceback already logged by timed_step where applicable
        log.exception("TRAIN TRANSFORMER aborted: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
