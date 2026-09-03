"""Base class for source-specific collectors."""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_retry_session(retries: int = 3, backoff: float = 1.5) -> requests.Session:
    """Return a requests Session that automatically retries on transient errors.

    Retries on: 429 (rate-limit), 500/502/503/504 (server errors).
    Backoff: 1.5s, 3s, 6s between attempts.
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def infer_product_category(text: str, keyword_map: dict[str, list[str]]) -> str | None:
    """First matching category from an ordered {category: [keywords]} map,
    checked most-specific-first by the caller's ordering (e.g. "seafood"
    before a generic "prepared dishes" catch-all, so a frozen squid dish
    doesn't fall through to the catch-all just because it's also frozen)."""
    for category, keywords in keyword_map.items():
        if any(kw in text for kw in keywords):
            return category
    return None


class BaseCollector(ABC):
    """A collector pulls raw records from one source and yields normalized alert dicts."""

    source_id: str = ""

    @abstractmethod
    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        """Yield raw records from the source as-is."""

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Map a raw source record onto the unified schema."""

    def collect(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        for raw in self.fetch_raw(since=since, limit=limit):
            try:
                yield self.normalize(raw)
            except Exception as e:
                # Skip malformed records, surface in pipeline log.
                yield {"_error": str(e), "_source_id": self.source_id, "_raw": raw}
