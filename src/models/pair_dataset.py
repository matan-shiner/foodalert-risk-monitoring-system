"""PyTorch dataset for pairwise recall training."""
from __future__ import annotations

import random
import sqlite3
from typing import Any

import torch
from torch.utils.data import Dataset

from src.labeling.prompts import render_alert_as_text
from src.labeling.training_pairs import DEFAULT_MODEL, DEFAULT_SAMPLE


def load_alert_text_map(
    conn: sqlite3.Connection,
    alert_ids: set[str],
) -> dict[str, str]:
    """One text string per alert (shared across all pairs)."""
    placeholders = ",".join("?" for _ in alert_ids)
    cur = conn.execute(
        f"SELECT * FROM alerts WHERE id IN ({placeholders})",
        list(alert_ids),
    )
    return {row["id"]: render_alert_as_text(dict(row)) for row in cur}


def load_pair_rows_from_db(
    conn: sqlite3.Connection,
    split: str,
    sample_name: str,
    label_model: str,
) -> list[tuple[str, str, int]]:
    cur = conn.execute(
        """
        SELECT alert_a_id, alert_b_id, label
        FROM synthetic_training_pairs
        WHERE sample_name = ? AND label_model = ? AND split = ?
        ORDER BY pair_id
        """,
        (sample_name, label_model, split),
    )
    return [(row[0], row[1], row[2]) for row in cur]


def _tokenize_alerts(
    tokenizer,
    text_by_id: dict[str, str],
    max_length: int,
) -> dict[str, dict[str, torch.Tensor]]:
    """Tokenize each alert once; reused for every pair in the split."""
    ids = list(text_by_id.keys())
    texts = [text_by_id[aid] for aid in ids]
    enc = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    cache: dict[str, dict[str, torch.Tensor]] = {}
    for i, alert_id in enumerate(ids):
        cache[alert_id] = {
            "input_ids": enc["input_ids"][i].clone(),
            "attention_mask": enc["attention_mask"][i].clone(),
        }
    return cache


class PairwiseRecallDataset(Dataset):
    """Dataset from in-memory list of pair dicts (CSV / JSON source)."""

    def __init__(
        self,
        pairs: list[dict[str, Any]],
        tokenizer,
        max_length: int = 256,
        subsample: int | None = None,
        seed: int = 42,
    ) -> None:
        if subsample and subsample < len(pairs):
            rng = random.Random(seed)
            pairs = rng.sample(pairs, subsample)
        self.pairs = pairs
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        p = self.pairs[idx]
        enc_a = self.tokenizer(
            p["text_a"],
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
            return_tensors="pt",
        )
        enc_b = self.tokenizer(
            p["text_b"],
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
            return_tensors="pt",
        )
        return {
            "input_ids_a":      enc_a["input_ids"].squeeze(0),
            "attention_mask_a": enc_a["attention_mask"].squeeze(0),
            "input_ids_b":      enc_b["input_ids"].squeeze(0),
            "attention_mask_b": enc_b["attention_mask"].squeeze(0),
            "label":            torch.tensor(p["label"], dtype=torch.float),
        }


class PairwiseRecallDbDataset(Dataset):
    """Pairs from SQLite; alert text loaded once per alert (not duplicated per pair)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        split: str,
        tokenizer,
        max_length: int = 256,
        sample_name: str = DEFAULT_SAMPLE,
        label_model: str = DEFAULT_MODEL,
        subsample: int | None = None,
        seed: int = 42,
    ) -> None:
        all_pairs = load_pair_rows_from_db(conn, split, sample_name, label_model)
        all_alert_ids: set[str] = set()
        for a, b, _ in all_pairs:
            all_alert_ids.add(a)
            all_alert_ids.add(b)

        print(
            f"  {split}: {len(all_pairs):,} pairs, {len(all_alert_ids):,} unique alerts"
            " — loading texts…",
            flush=True,
        )

        text_by_id = load_alert_text_map(conn, all_alert_ids)

        # Some alerts may have been removed from the DB after training pair generation.
        # Keep only pairs where both alerts still exist.
        pairs = [
            (a, b, lbl)
            for a, b, lbl in all_pairs
            if a in text_by_id and b in text_by_id
        ]
        if len(pairs) < len(all_pairs):
            print(
                f"  {split}: filtered {len(all_pairs) - len(pairs):,} pairs"
                " whose alerts were removed from the DB",
                flush=True,
            )

        if subsample and subsample < len(pairs):
            rng = random.Random(seed)
            pairs = rng.sample(pairs, subsample)

        present_ids = set()
        for a, b, _ in pairs:
            present_ids.add(a)
            present_ids.add(b)

        print(
            f"  {split}: tokenizing {len(present_ids):,} alerts (once)…",
            flush=True,
        )
        self.token_cache = _tokenize_alerts(
            tokenizer,
            {aid: text_by_id[aid] for aid in present_ids},
            max_length,
        )
        self.pairs = pairs

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        alert_a_id, alert_b_id, label = self.pairs[idx]
        enc_a = self.token_cache[alert_a_id]
        enc_b = self.token_cache[alert_b_id]
        return {
            "input_ids_a":      enc_a["input_ids"],
            "attention_mask_a": enc_a["attention_mask"],
            "input_ids_b":      enc_b["input_ids"],
            "attention_mask_b": enc_b["attention_mask"],
            "label":            torch.tensor(label, dtype=torch.float),
        }
