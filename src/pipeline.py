"""Pipeline: collect from one or more sources, normalize, dedup, write to SQLite."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

from . import db
from .collectors import COLLECTORS

logger = logging.getLogger("pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def run_source(conn, source_id: str, since: datetime | None = None, limit: int | None = None) -> dict:
    if source_id not in COLLECTORS:
        return {"source_id": source_id, "error": f"unknown source: {source_id}"}

    collector = COLLECTORS[source_id]()
    fetched = inserted = updated = 0
    started_at = datetime.utcnow().isoformat(timespec="seconds")
    error = None
    try:
        for alert in collector.collect(since=since, limit=limit):
            if "_error" in alert:
                logger.warning(f"[{source_id}] skipped record: {alert['_error']}")
                continue
            fetched += 1
            action = db.upsert_alert(conn, alert)
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1
            if fetched % 20 == 0:
                conn.commit()
                logger.info(f"[{source_id}] processed {fetched} records")
        conn.commit()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.error(f"[{source_id}] failed: {error}")

    finished_at = datetime.utcnow().isoformat(timespec="seconds")
    db.log_run(conn, source_id, started_at, finished_at, fetched, inserted, updated, error)
    return {"source_id": source_id, "fetched": fetched, "inserted": inserted,
            "updated": updated, "error": error}


def run_all(sources: list[str], since: datetime | None = None, limit_per_source: int | None = None) -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    results = []
    for src in sources:
        logger.info(f"=== Running {src} ===")
        results.append(run_source(conn, src, since=since, limit=limit_per_source))
    conn.close()
    return results
