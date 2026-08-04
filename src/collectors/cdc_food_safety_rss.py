"""CDC combined Food Safety RSS feed collector.

Endpoint: https://tools.cdc.gov/api/v2/resources/media/316422.rss
Docs:     none published — discovered via the widget embedded on
          https://www.foodsafety.gov/recalls-and-outbreaks

Captures FDA and FSIS company announcements / public health alerts
*before* they are formally classified (Class I/II/III) and published to
the FDA Enforcement Reports API or the FSIS Recall API. This is the gap
those two collectors structurally cannot see: an announcement can sit
for days to weeks (or indefinitely, e.g. outbreak-linked withdrawals)
before — if ever — it gets a formal enforcement record.

Trade-off: the feed is a rolling window (~5-6 weeks, ~20-30 items,
no pagination) with no severity classification and only a short free-text
description. It is a leading indicator, not a replacement for the
enforcement collectors — dedup.py links it to the later formal record
via fingerprint once (if) one appears.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator
from xml.etree import ElementTree

from .base import BaseCollector, make_retry_session

ENDPOINT = "https://tools.cdc.gov/api/v2/resources/media/316422.rss"

_FIRM_TITLE_RE = re.compile(
    r"^(.*?)\s+(?:"
    r"recalls|"
    r"issues?\s+(?:an?\s+)?(?:expanded\s+)?recall|"
    r"announces?\s+(?:a\s+)?(?:voluntary\s+)?recall|"
    r"expands?\s+(?:its\s+|a\s+)?recall"
    r")\b",
    re.IGNORECASE,
)

_BIOLOGICAL_KW = [
    "salmonella", "listeria", "e. coli", "e.coli", "campylobacter", "norovirus",
    "clostridium", "cyclospora", "hepatitis", "pathogen", "bacteria",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical", "residue",
]
_ALLERGEN_KW = [
    "allergen", "allergy", "allergic", "undeclared", "gluten", "peanut",
    "tree nut", "soy", "soya", "sesame", "milk", "egg", "wheat", "shellfish",
]
_PHYSICAL_KW = ["metal", "glass", "plastic", "fragment", "foreign object", "foreign matter"]


class CDCFoodSafetyRSSCollector(BaseCollector):
    source_id = "cdc_food_safety_rss"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        r = session.get(ENDPOINT, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)

        count = 0
        for item in root.findall("./channel/item"):
            pub_date = _parse_rss_date(item.findtext("pubDate"))
            if since and pub_date and pub_date < since.replace(tzinfo=pub_date.tzinfo or None):
                continue
            yield {
                "title": (item.findtext("title") or "").strip(),
                "description": (item.findtext("description") or "").strip(),
                "link": item.findtext("link"),
                "guid": item.findtext("guid"),
                "pub_date": item.findtext("pubDate"),
            }
            count += 1
            if limit and count >= limit:
                return

    def normalize(self, raw: dict) -> dict:
        title = raw.get("title") or ""
        description = raw.get("description") or ""
        text = f"{title} {description}"

        record_id = _extract_record_id(raw.get("guid") or raw.get("link") or "") or title
        firm = _extract_firm(title)
        pub_date = _parse_rss_date(raw.get("pub_date"))
        pub_date_str = pub_date.date().isoformat() if pub_date else None

        is_public_health_alert = "public health alert" in text.lower()

        return {
            "id": f"cdc_food_safety_rss::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, description, "United States"),
            "record_url": raw.get("link"),
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": pub_date_str,
            "event_initiation_date": pub_date_str,
            "event_status": None,  # pre-classification announcement — no formal status yet
            "origin_country": "United States",
            "distribution_countries": json.dumps(["United States"]),
            "israel_relevance_flag": 1 if "israel" in text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([]),
            "product_description": description or None,
            "product_category": None,
            "hazard_category": _infer_hazard_category(text),
            "hazard_specific": _extract_hazard_specific(text),
            "severity_raw": "public health alert" if is_public_health_alert else None,
            # Honest gap: FDA/FSIS haven't classified this yet, so we don't
            # invent a severity. "high" only for the one signal FSIS gives
            # pre-classification (Public Health Alert = imminent risk).
            "severity_normalized": "high" if is_public_health_alert else None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": description or None,
            "reason_for_recall": description or None,
        }


def _parse_rss_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _extract_record_id(url: str) -> str | None:
    """CDC content id, e.g. '...?m=316422&c=766042' -> '766042'."""
    m = re.search(r"[?&]c=(\d+)", url)
    return m.group(1) if m else None


def _extract_firm(title: str) -> str | None:
    m = _FIRM_TITLE_RE.match(title)
    if not m:
        return None
    firm = m.group(1).strip(" -")
    return firm or None


def _infer_hazard_category(text: str) -> str | None:
    t = text.lower()
    allergen_context = any(kw in t for kw in ["allergen", "allergy", "allergic", "undeclared"])
    if allergen_context:
        return "allergen"
    for kw in _BIOLOGICAL_KW:
        if kw in t:
            return "biological"
    for kw in _CHEMICAL_KW:
        if kw in t:
            return "chemical"
    for kw in _PHYSICAL_KW:
        if kw in t:
            return "physical"
    return None


def _extract_hazard_specific(text: str) -> str | None:
    t = text.lower()
    for hazard in [
        "salmonella", "listeria monocytogenes", "listeria", "e. coli", "e.coli",
        "campylobacter", "cyclospora", "norovirus", "clostridium botulinum",
        "hepatitis a", "undeclared allergen",
    ]:
        if hazard in t:
            return hazard
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
