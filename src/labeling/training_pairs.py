"""Synthetic training pairs from Bradley-Terry composite scores (step 5).

For each pair (A, B) among scored alerts in a split, the label is:
  1 if composite_A > composite_B  (A wins)
  0 if composite_A < composite_B  (B wins)
Ties are skipped by default.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.labeling.prompts import render_alert_as_text

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_SAMPLE = "labeling_v3"
DEFAULT_SEED = 42
DEFAULT_TEST_FRAC = 0.15   # held-out final test (never used for model selection)
DEFAULT_VAL_FRAC  = 0.15   # validation: used for early stopping / epoch selection


def _pair_id(alert_a_id: str, alert_b_id: str) -> str:
    lo, hi = sorted((alert_a_id, alert_b_id))
    return hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest()[:16]


def load_scored_alerts(
    conn: sqlite3.Connection,
    sample_name: str,
    label_model: str,
) -> list[dict[str, Any]]:
    """Alerts in sample with a composite BT score."""
    cur = conn.execute(
        """
        SELECT a.*, bt.score AS composite
        FROM sample_members sm
        JOIN alerts a ON a.id = sm.alert_id
        JOIN bt_scores bt ON bt.alert_id = a.id
          AND bt.sample_name = sm.sample_name
          AND bt.dimension = 'composite'
          AND bt.label_model = ?
        WHERE sm.sample_name = ?
        ORDER BY a.id
        """,
        (label_model, sample_name),
    )
    rows = [dict(r) for r in cur]
    for r in rows:
        r["text"] = render_alert_as_text(r)
    return rows


def stratified_alert_split(
    alerts: list[dict[str, Any]],
    test_frac: float = DEFAULT_TEST_FRAC,
    val_frac: float = DEFAULT_VAL_FRAC,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], list[str], list[str]]:
    """Split alert IDs into train / val / test, stratified by severity_normalized.

    Returns (train_ids, val_ids, test_ids).
    val is used for model selection (early stopping).
    test is held out and evaluated only once at the end.
    """
    by_sev: dict[str, list[str]] = defaultdict(list)
    for a in alerts:
        key = a.get("severity_normalized") or "unknown"
        by_sev[key].append(a["id"])

    rng = random.Random(seed)
    train_ids: list[str] = []
    val_ids: list[str] = []
    test_ids: list[str] = []

    for ids in by_sev.values():
        ids = list(ids)
        rng.shuffle(ids)
        n = len(ids)
        if n <= 2:
            train_ids.extend(ids)
            continue
        n_test = max(1, round(n * test_frac))
        n_val  = max(1, round(n * val_frac))
        n_test = min(n_test, n - 2)
        n_val  = min(n_val,  n - n_test - 1)
        test_ids.extend(ids[:n_test])
        val_ids.extend(ids[n_test : n_test + n_val])
        train_ids.extend(ids[n_test + n_val :])

    return train_ids, val_ids, test_ids


def generate_pairs_for_alerts(
    alerts_by_id: dict[str, dict[str, Any]],
    alert_ids: list[str],
    *,
    skip_ties: bool = True,
) -> list[dict[str, Any]]:
    """All C(n,2) pairs within alert_ids; winner from higher composite."""
    pairs: list[dict[str, Any]] = []
    for id_a, id_b in itertools.combinations(sorted(alert_ids), 2):
        a = alerts_by_id[id_a]
        b = alerts_by_id[id_b]
        comp_a = float(a["composite"])
        comp_b = float(b["composite"])
        diff = comp_a - comp_b

        if skip_ties and abs(diff) < 1e-12:
            continue

        if diff > 0:
            winner = "A"
            label = 1
        else:
            winner = "B"
            label = 0

        pairs.append(
            {
                "pair_id": _pair_id(id_a, id_b),
                "alert_a_id": id_a,
                "alert_b_id": id_b,
                "composite_a": comp_a,
                "composite_b": comp_b,
                "composite_diff": diff,
                "winner": winner,
                "label": label,
                "text_a": a["text"],
                "text_b": b["text"],
            }
        )
    return pairs


def build_training_dataset(
    conn: sqlite3.Connection,
    sample_name: str = DEFAULT_SAMPLE,
    label_model: str = DEFAULT_MODEL,
    test_frac: float = DEFAULT_TEST_FRAC,
    val_frac: float = DEFAULT_VAL_FRAC,
    seed: int = DEFAULT_SEED,
    skip_ties: bool = True,
) -> dict[str, Any]:
    alerts = load_scored_alerts(conn, sample_name, label_model)
    if len(alerts) < 3:
        raise ValueError(f"Need at least 3 scored alerts; got {len(alerts)}")

    by_id = {a["id"]: a for a in alerts}
    train_ids, val_ids, test_ids = stratified_alert_split(
        alerts, test_frac=test_frac, val_frac=val_frac, seed=seed
    )

    train_pairs = generate_pairs_for_alerts(by_id, train_ids, skip_ties=skip_ties)
    val_pairs   = generate_pairs_for_alerts(by_id, val_ids,   skip_ties=skip_ties)
    test_pairs  = generate_pairs_for_alerts(by_id, test_ids,  skip_ties=skip_ties)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {
        "meta": {
            "sample_name":    sample_name,
            "label_model":    label_model,
            "created_at":     created_at,
            "n_scored_alerts": len(alerts),
            "n_train_alerts": len(train_ids),
            "n_val_alerts":   len(val_ids),
            "n_test_alerts":  len(test_ids),
            "n_train_pairs":  len(train_pairs),
            "n_val_pairs":    len(val_pairs),
            "n_test_pairs":   len(test_pairs),
            "test_frac":      test_frac,
            "val_frac":       val_frac,
            "seed":           seed,
            "skip_ties":      skip_ties,
            "label_rule": "label=1 iff composite_A > composite_B (BCE on score_A - score_B)",
        },
        "split": {
            "train_alert_ids": train_ids,
            "val_alert_ids":   val_ids,
            "test_alert_ids":  test_ids,
        },
        "pairs": {
            "train": train_pairs,
            "val":   val_pairs,
            "test":  test_pairs,
        },
    }


def persist_split_and_pairs(
    conn: sqlite3.Connection,
    dataset: dict[str, Any],
) -> None:
    """Write training_split_members and synthetic_training_pairs tables."""
    meta = dataset["meta"]
    sample = meta["sample_name"]
    model = meta["label_model"]
    created = meta["created_at"]

    # Recreate tables with updated constraint (train | val | test).
    # DROP + CREATE is safe here because generate_training_pairs.py always
    # rebuilds the data from scratch on each run.
    conn.executescript(
        """
        DROP TABLE IF EXISTS synthetic_training_pairs;
        DROP TABLE IF EXISTS training_split_members;

        CREATE TABLE training_split_members (
            sample_name   TEXT NOT NULL,
            label_model   TEXT NOT NULL,
            alert_id      TEXT NOT NULL,
            split         TEXT NOT NULL CHECK (split IN ('train', 'val', 'test')),
            composite     REAL NOT NULL,
            created_at    TEXT NOT NULL,
            PRIMARY KEY (sample_name, label_model, alert_id),
            FOREIGN KEY (alert_id) REFERENCES alerts(id)
        );

        CREATE TABLE synthetic_training_pairs (
            pair_id        TEXT PRIMARY KEY,
            sample_name    TEXT NOT NULL,
            label_model    TEXT NOT NULL,
            split          TEXT NOT NULL CHECK (split IN ('train', 'val', 'test')),
            alert_a_id     TEXT NOT NULL,
            alert_b_id     TEXT NOT NULL,
            composite_a    REAL NOT NULL,
            composite_b    REAL NOT NULL,
            composite_diff REAL NOT NULL,
            winner         TEXT NOT NULL CHECK (winner IN ('A', 'B')),
            label          INTEGER NOT NULL CHECK (label IN (0, 1)),
            created_at     TEXT NOT NULL,
            FOREIGN KEY (alert_a_id) REFERENCES alerts(id),
            FOREIGN KEY (alert_b_id) REFERENCES alerts(id)
        );

        CREATE INDEX idx_stp_sample_split
            ON synthetic_training_pairs(sample_name, label_model, split);
        """
    )

    conn.execute(
        "DELETE FROM training_split_members WHERE sample_name = ? AND label_model = ?",
        (sample, model),
    )
    conn.execute(
        "DELETE FROM synthetic_training_pairs WHERE sample_name = ? AND label_model = ?",
        (sample, model),
    )

    by_id = {a["id"]: a for a in load_scored_alerts(conn, sample, model)}

    for split_key, ids_key in (
        ("train", "train_alert_ids"),
        ("val",   "val_alert_ids"),
        ("test",  "test_alert_ids"),
    ):
        for aid in dataset["split"][ids_key]:
            conn.execute(
                """
                INSERT INTO training_split_members
                (sample_name, label_model, alert_id, split, composite, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sample, model, aid, split_key, float(by_id[aid]["composite"]), created),
            )

    for split_key in ("train", "val", "test"):
        for p in dataset["pairs"][split_key]:
            conn.execute(
                """
                INSERT INTO synthetic_training_pairs
                (pair_id, sample_name, label_model, split,
                 alert_a_id, alert_b_id, composite_a, composite_b, composite_diff,
                 winner, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["pair_id"],
                    sample,
                    model,
                    split_key,
                    p["alert_a_id"],
                    p["alert_b_id"],
                    p["composite_a"],
                    p["composite_b"],
                    p["composite_diff"],
                    p["winner"],
                    p["label"],
                    created,
                ),
            )

    conn.commit()


def export_pairs_csv(pairs: list[dict[str, Any]], path: str) -> None:
    import csv
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "pair_id",
        "alert_a_id",
        "alert_b_id",
        "composite_a",
        "composite_b",
        "composite_diff",
        "winner",
        "label",
        "text_a",
        "text_b",
    ]
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(pairs)
