"""USDA FSIS Recall API collector.

Endpoint: https://www.fsis.usda.gov/fsis/api/recall/v/1
Docs:     https://www.fsis.usda.gov/science-data/developer-resources/recall-api

The endpoint sits behind Akamai bot protection that fingerprints TLS handshakes.
Plain `requests`, `urllib`, and even modern `curl` calls get HTTP 403. The fix
is `curl_cffi` with `impersonate="chrome120"`, which spoofs Chrome's TLS
fingerprint. Verified working from an Israeli IP on 2026-05-11.

The API returns the entire historical recall list (~2,000 records) in one
response. No pagination is needed; we filter client-side by `since`.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from typing import Iterator

from curl_cffi import requests as cf_requests

from .base import BaseCollector

ENDPOINT = "https://www.fsis.usda.gov/fsis/api/recall/v/1"


class FSISCollector(BaseCollector):
    source_id = "fsis"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        r = cf_requests.get(ENDPOINT, impersonate="chrome120", timeout=120)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            import sys
            print(f"FSIS: unexpected response type {type(data).__name__} — expected list. "
                  f"API may have changed format.", file=sys.stderr)
            return
        count = 0
        for rec in data:
            init_date = _parse_fsis_date(rec.get("field_recall_date"))
            if since and init_date and init_date < since.strftime("%Y-%m-%d"):
                continue
            yield rec
            count += 1
            if limit and count >= limit:
                return

    def normalize(self, raw: dict) -> dict:
        record_id = str(raw.get("field_recall_number") or "")
        title = _as_str(raw.get("field_title", ""))
        firm = _as_str(raw.get("field_establishment", ""))
        summary = _as_str(raw.get("field_summary") or raw.get("field_recall_reason"))
        states_raw = raw.get("field_states", "")
        product_items = _as_str(raw.get("field_product_items", ""))
        classification = _as_str(raw.get("field_recall_classification") or raw.get("field_risk_level", ""))
        reason = _as_str(raw.get("field_recall_reason"))

        if isinstance(states_raw, list):
            states_list = [s.strip() for s in states_raw if s]
        else:
            states_list = [s.strip() for s in states_raw.split(",") if s.strip()]

        return {
            "id": f"fsis::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product_items, "United States"),
            "record_url": raw.get("field_recall_url") or None,
            "ingestion_date": datetime.utcnow().isoformat(timespec="seconds"),
            "source_published_date": _parse_fsis_date(raw.get("field_recall_date")),
            "event_initiation_date": _parse_fsis_date(raw.get("field_recall_date")),
            "event_status": _normalize_status(raw),
            "origin_country": "United States",
            "distribution_countries": json.dumps(states_list),
            "israel_relevance_flag": 1 if "israel" in json.dumps(raw, default=str).lower() else 0,
            "recalling_firm": firm or None,
            "brand_names": json.dumps([]),
            "product_description": product_items or None,
            "product_category": "Meat & Poultry",
            "hazard_category": _infer_hazard_category(reason, summary),
            "hazard_specific": _extract_hazard_specific(reason, summary),
            "severity_raw": classification or None,
            "severity_normalized": _normalize_fsis_class(classification, reason),
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": _strip_html(summary) or None,
            "reason_for_recall": reason or None,
        }


def _as_str(value) -> str:
    """Coerce a string-or-list field to a plain string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return str(value)


def _parse_fsis_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        return date_str


def _normalize_fsis_class(cls: str | None, reason: str | None = None) -> str | None:
    """Map FSIS classification to our high/medium/low scale.

    FSIS values seen in practice: 'Class I', 'Class II', 'Class III',
    'Public Health Alert' (treated as high — pre-recall warning of serious risk).
    """
    if not cls:
        return None
    c = cls.lower()
    if "class i" in c and "ii" not in c and "iii" not in c:
        return "high"
    if "public health alert" in c:
        return "high"
    if "class ii" in c and "iii" not in c:
        return "medium"
    if "class iii" in c:
        return "low"
    if "high" in c:
        return "high"
    if "low" in c:
        return "low"
    return "medium"


def _normalize_status(raw: dict) -> str | None:
    if str(raw.get("field_active_notice", "")).lower() in ("true", "1"):
        return "ongoing"
    if str(raw.get("field_archive_recall", "")).lower() in ("true", "1"):
        return "terminated"
    if raw.get("field_closed_date"):
        return "completed"
    return None


# hazard_category is derived FROM this table rather than a separately
# maintained keyword scan, so the two fields can never disagree — this
# collector previously had hazard_category hardcoded to None for every
# record (100% unclassified) despite hazard_specific already working.
_SPECIFIC_TO_CATEGORY: dict[str, str] = {
    "listeria monocytogenes": "biological",
    "listeria": "biological",
    "l. monocytogenes": "biological",
    "salmonella": "biological",
    "e. coli": "biological",
    "e.coli": "biological",
    "escherichia coli": "biological",
    "campylobacter": "biological",
    "clostridium botulinum": "biological",
    "clostridium": "biological",
    "staphylococcus": "biological",
    "norovirus": "biological",
    "hepatitis a": "biological",
    "insect": "biological",
    "moth": "biological",
    "undeclared allergen": "allergen",
    "extraneous material": "physical",
    "foreign material": "physical",
    "foreign object": "physical",
    # FSIS's own generic reason tags, not free text — no pathogen name to
    # find, but the tag itself says enough. "Product Contamination" alone
    # is genuinely ambiguous (any of biological/chemical/physical) and is
    # deliberately left unmapped rather than guessed.
    "misbranding": "fraud",
    "import violation": "regulatory",
    "produced without benefit of inspection": "regulatory",
}


def _extract_hazard_specific(reason: str | None, summary: str | None = "") -> str | None:
    text = ((reason or "") + " " + (summary or "")).lower()
    for hazard in _SPECIFIC_TO_CATEGORY:
        if hazard in text:
            return hazard
    return None


def _infer_hazard_category(reason: str | None, summary: str | None = "") -> str | None:
    specific = _extract_hazard_specific(reason, summary)
    return _SPECIFIC_TO_CATEGORY[specific] if specific else None


def _strip_html(text: str | None) -> str | None:
    """Light HTML strip — FSIS summary field comes with <p>, <strong>, etc."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
