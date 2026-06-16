"""Step 6: Train Siamese Bi-Encoder with BCE on score_A - score_B.

Usage:
  python3 -m pip install -r requirements-ml.txt
  python3 scripts/train_bi_encoder.py --epochs 2 --max-train-pairs 5000
  python3 scripts/train_bi_encoder.py --epochs 3   # full train set (~34k pairs)
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, get_linear_schedule_with_warmup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.labeling.training_pairs import DEFAULT_MODEL, DEFAULT_SAMPLE
from src.models.bi_encoder import BiEncoderRiskModel
from src.models.pair_dataset import PairwiseRecallDataset, PairwiseRecallDbDataset

DEFAULT_TRAIN_CSV = Path("data/exports/training_pairs/train_pairs.csv")
DEFAULT_TEST_CSV = Path("data/exports/training_pairs/test_pairs.csv")
DEFAULT_DATA_JSON = Path("data/synthetic_training_pairs.json")
DEFAULT_OUT = Path("models/bi_encoder_labeling_v3")


def load_pairs_from_csv(train_csv: Path, test_csv: Path) -> dict:
    """Fast path: load pairs from step-5 CSV exports (avoids 120MB JSON parse)."""

    def read_csv(path: Path) -> list[dict]:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    train = read_csv(train_csv)
    test = read_csv(test_csv)
    for rows in (train, test):
        for p in rows:
            p["label"] = int(p["label"])
            p["composite_a"] = float(p["composite_a"])
            p["composite_b"] = float(p["composite_b"])
    return {
        "meta": {
            "train_csv": str(train_csv),
            "test_csv": str(test_csv),
            "n_train_pairs": len(train),
            "n_test_pairs": len(test),
        },
        "pairs": {"train": train, "test": test},
    }


def load_dataset(
    train_csv: Path | None,
    test_csv: Path | None,
    json_path: Path | None,
) -> dict:
    if train_csv and test_csv and train_csv.exists() and test_csv.exists():
        print(f"Loading pairs from CSV: {train_csv.name} ({train_csv.stat().st_size // 1_000_000}MB)…")
        return load_pairs_from_csv(train_csv, test_csv)
    if json_path and json_path.exists():
        print(f"Loading pairs from JSON ({json_path.stat().st_size // 1_000_000}MB) — may take several minutes…")
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(
        "Need CSV pair files or JSON. Run: python3 scripts/generate_training_pairs.py"
    )


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def pick_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


@torch.no_grad()
def evaluate(model: BiEncoderRiskModel, loader: DataLoader, device: torch.device) -> dict:
    model.eval()
    total_loss = 0.0
    correct = 0
    n = 0
    for batch in loader:
        ids_a = batch["input_ids_a"].to(device)
        mask_a = batch["attention_mask_a"].to(device)
        ids_b = batch["input_ids_b"].to(device)
        mask_b = batch["attention_mask_b"].to(device)
        labels = batch["label"].to(device)

        score_a, score_b = model(ids_a, mask_a, ids_b, mask_b)
        loss = model.pairwise_bce_loss(score_a, score_b, labels)
        total_loss += loss.item() * labels.size(0)

        pred = (score_a > score_b).float()
        correct += (pred == labels).sum().item()
        n += labels.size(0)

    return {
        "loss": total_loss / max(n, 1),
        "pairwise_accuracy": correct / max(n, 1),
        "n_pairs": n,
    }


def train_epoch(
    model: BiEncoderRiskModel,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scheduler,
    device: torch.device,
    grad_accum: int = 1,
    log_every: int = 50,
    val_loader: DataLoader | None = None,
    val_every: int = 100,
) -> tuple[float, list[dict]]:
    """Returns (mean_loss, step_log) — step_log has one entry per log_every batches.
    If val_loader is provided, also evaluates val loss every val_every steps."""
    model.train()
    total_loss = 0.0
    n = 0
    step_log: list[dict] = []
    running_loss = 0.0
    running_n = 0
    optimizer.zero_grad()
    n_steps = len(loader)

    for step, batch in enumerate(loader, start=1):
        ids_a = batch["input_ids_a"].to(device)
        mask_a = batch["attention_mask_a"].to(device)
        ids_b = batch["input_ids_b"].to(device)
        mask_b = batch["attention_mask_b"].to(device)
        labels = batch["label"].to(device)

        score_a, score_b = model(ids_a, mask_a, ids_b, mask_b)
        loss = model.pairwise_bce_loss(score_a, score_b, labels) / grad_accum
        loss.backward()

        if step % grad_accum == 0 or step == n_steps:
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()

        batch_loss = loss.item() * grad_accum
        total_loss    += batch_loss * labels.size(0)
        running_loss  += batch_loss * labels.size(0)
        running_n     += labels.size(0)
        n             += labels.size(0)

        if step % log_every == 0 or step == n_steps:
            step_loss = running_loss / max(running_n, 1)
            entry: dict = {"step": step, "train_loss": round(step_loss, 5)}

            if val_loader is not None and (step % val_every == 0 or step == n_steps):
                val_m = evaluate(model, val_loader, device)
                entry["val_loss"] = round(val_m["loss"], 5)
                model.train()

            step_log.append(entry)
            val_str = f"  val_loss={entry['val_loss']:.4f}" if "val_loss" in entry else ""
            print(f"  step {step:>4}/{n_steps}  loss={step_loss:.4f}{val_str}", flush=True)
            running_loss = running_n = 0

    return total_loss / max(n, 1), step_log


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Bi-Encoder (BCE on score difference)")
    parser.add_argument(
        "--source",
        choices=("db", "csv", "json"),
        default="db",
        help="db=SQLite pairs + alert texts (fast); csv/json=slow for full set",
    )
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    parser.add_argument("--label-model", default=DEFAULT_MODEL)
    parser.add_argument("--train-csv", type=Path, default=DEFAULT_TRAIN_CSV)
    parser.add_argument("--test-csv", type=Path, default=DEFAULT_TEST_CSV)
    parser.add_argument("--data", type=Path, default=None, help="JSON path when --source json")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--backbone", default="distilroberta-base")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--max-train-pairs", type=int, default=None, help="Subsample train pairs for faster runs")
    parser.add_argument("--max-test-pairs", type=int, default=None)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--log-every", type=int, default=50,
                        help="Log train loss every N batches within each epoch")
    parser.add_argument("--val-every", type=int, default=100,
                        help="Evaluate val loss every N batches within each epoch (0=disable)")
    args = parser.parse_args()

    set_seed(args.seed)
    device = pick_device()
    print(f"Device: {device}", flush=True)

    data_meta: dict = {}
    conn = None
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)

    if args.source == "db":
        conn = db.connect()
        n_train = conn.execute(
            """
            SELECT COUNT(*) FROM synthetic_training_pairs
            WHERE sample_name = ? AND label_model = ? AND split = 'train'
            """,
            (args.sample, args.label_model),
        ).fetchone()[0]
        if n_train == 0:
            raise SystemExit("No pairs in DB — run: python3 scripts/generate_training_pairs.py --write-db")
        has_val = conn.execute(
            "SELECT COUNT(*) FROM synthetic_training_pairs "
            "WHERE sample_name=? AND label_model=? AND split='val'",
            (args.sample, args.label_model),
        ).fetchone()[0] > 0
        print(f"Loading pairs from SQLite ({args.sample})…", flush=True)
        train_ds = PairwiseRecallDbDataset(
            conn, "train", tokenizer, max_length=args.max_length,
            sample_name=args.sample, label_model=args.label_model,
            subsample=args.max_train_pairs, seed=args.seed,
        )
        # Use val split if available (new 3-way split), else fall back to test
        val_split = "val" if has_val else "test"
        val_ds = PairwiseRecallDbDataset(
            conn, val_split, tokenizer, max_length=args.max_length,
            sample_name=args.sample, label_model=args.label_model,
            subsample=args.max_test_pairs, seed=args.seed,
        )
        data_meta = {
            "source": "db", "sample_name": args.sample,
            "label_model": args.label_model, "val_split_used": val_split,
        }
    else:
        if args.source == "csv":
            dataset = load_dataset(args.train_csv, args.test_csv, None)
        else:
            dataset = load_dataset(None, None, args.data or DEFAULT_DATA_JSON)
        data_meta = dataset.get("meta", {})
        train_ds = PairwiseRecallDataset(
            dataset["pairs"]["train"],
            tokenizer,
            max_length=args.max_length,
            subsample=args.max_train_pairs,
            seed=args.seed,
        )
        val_pairs = dataset["pairs"].get("val") or dataset["pairs"]["test"]
        val_ds = PairwiseRecallDataset(
            val_pairs, tokenizer,
            max_length=args.max_length, subsample=args.max_test_pairs, seed=args.seed,
        )

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=device.type == "cuda",
    )
    test_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
    )

    model = BiEncoderRiskModel(backbone=args.backbone).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    total_steps = max(1, len(train_loader) * args.epochs // args.grad_accum)
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=min(100, total_steps // 10),
        num_training_steps=total_steps,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    best_acc = 0.0
    t0 = time.time()

    print(f"Train pairs: {len(train_ds):,} | Val pairs: {len(val_ds):,}", flush=True)
    print(f"Backbone: {args.backbone} | Epochs: {args.epochs} | Batch: {args.batch_size}", flush=True)

    for epoch in range(1, args.epochs + 1):
        print(f"\n── Epoch {epoch}/{args.epochs} ──────────────────────────────", flush=True)
        train_loss, step_log = train_epoch(
            model, train_loader, optimizer, scheduler, device,
            grad_accum=args.grad_accum, log_every=args.log_every,
            val_loader=test_loader if args.val_every > 0 else None,
            val_every=args.val_every,
        )
        val_metrics = evaluate(model, test_loader, device)
        row = {
            "epoch":      epoch,
            "train_loss": train_loss,
            **{f"val_{k}": v for k, v in val_metrics.items()},
            "step_log":   step_log,
        }
        history.append(row)
        print(
            f"Epoch {epoch} summary | train_loss={train_loss:.4f} | "
            f"val_loss={val_metrics['loss']:.4f} | "
            f"val_acc={val_metrics['pairwise_accuracy']:.3f}",
            flush=True,
        )

        if val_metrics["pairwise_accuracy"] >= best_acc:
            best_acc = val_metrics["pairwise_accuracy"]
            torch.save(
                {
                    "model_state": model.state_dict(),
                    "backbone":    args.backbone,
                    "max_length":  args.max_length,
                    "epoch":       epoch,
                    "val_metrics": val_metrics,
                },
                args.out_dir / "best_model.pt",
            )
            print(f"  ✓ New best val_acc={best_acc:.4f} — saved best_model.pt", flush=True)

    elapsed = time.time() - t0
    meta = {
        "data_source":              data_meta,
        "backbone":                 args.backbone,
        "epochs":                   args.epochs,
        "batch_size":               args.batch_size,
        "n_train_pairs":            len(train_ds),
        "n_val_pairs":              len(val_ds),
        "max_train_pairs":          args.max_train_pairs,
        "max_test_pairs":           args.max_test_pairs,
        "device":                   str(device),
        "elapsed_sec":              round(elapsed, 1),
        "best_val_pairwise_accuracy": best_acc,
        "loss": "BCE on score_A - score_B (binary_cross_entropy_with_logits)",
        "history": history,
    }
    with open(args.out_dir / "training_metrics.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    model.encoder.save_pretrained(args.out_dir / "encoder")
    tokenizer.save_pretrained(args.out_dir / "encoder")
    torch.save(model.score_head.state_dict(), args.out_dir / "score_head.pt")

    print(f"\nDone in {elapsed:.0f}s | best test pairwise accuracy: {best_acc:.3f}", flush=True)
    print(f"Saved: {args.out_dir}/best_model.pt, training_metrics.json", flush=True)
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
