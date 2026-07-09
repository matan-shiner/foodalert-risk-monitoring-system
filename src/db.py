"""SQLite connection and schema bootstrap."""
from __future__ import annotations
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "alerts.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def upsert_alert(conn: sqlite3.Connection, alert: dict) -> str:
    """Insert or replace an alert. Returns 'inserted', 'updated', or 'unchanged'."""
    columns = list(alert.keys())
    placeholders = ",".join("?" for _ in columns)
    col_names = ",".join(columns)

    cur = conn.execute(
        f"SELECT id FROM alerts WHERE source_id = ? AND source_record_id = ?",
        (alert["source_id"], alert["source_record_id"]),
    )
    existing = cur.fetchone()

    if existing is None:
        conn.execute(f"INSERT INTO alerts ({col_names}) VALUES ({placeholders})", [alert[c] for c in columns])
        return "inserted"

    skip = {"id", "ingestion_date"}  # keep original ingestion_date on updates
    update_set = ",".join(f"{c} = ?" for c in columns if c not in skip)
    values = [alert[c] for c in columns if c not in skip] + [existing["id"]]
    conn.execute(f"UPDATE alerts SET {update_set} WHERE id = ?", values)
    return "updated"


def log_run(conn, source_id: str, started_at: str, finished_at: str,
            fetched: int, inserted: int, updated: int, error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO ingestion_runs (source_id, started_at, finished_at,
           records_fetched, records_inserted, records_updated, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_id, started_at, finished_at, fetched, inserted, updated, error),
    )
    conn.commit()
