"""Health Canada / CFIA open data recall collector.

Endpoint: https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json
Docs:     none published — discovered via search results pointing at the raw
          open-data path behind recalls-rappels.canada.ca; not linked from
          open.canada.ca's own (stale, non-food) recall dataset page.

Health Canada publishes recalls/safety alerts across several programs
(medical devices, consumer products, drugs, food, ...) in a single JSON
file, refreshed daily. This collector keeps only the food program
(Organization == "CFIA" — Canadian Food Inspection Agency).

Unlike fda_enforcement.py / fsis.py, the file has no pagination and no
`since` query param — it's the entire live dataset (~34k records across
all programs, ~5k CFIA/food) in one response, so `since` filtering
happens client-side exactly like fsis.py.

CFIA's "Recall class" uses the same Class 1/2/3 severity convention as
FDA's Class I/II/III, so the classification mapping mirrors
fda_enforcement.py's. There is no company/firm field in the raw data —
the brand is embedded in Title text ("<Brand> brand <Product> recalled
due to <Issue>"), so it's extracted the same way cdc_food_safety_rss.py
extracts firm names from titles.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator



from .base import BaseCollector, make_retry_session

ENDPOINT = "https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json"

_BRAND_TITLE_RE = re.compile(r"^(.*?)\s+brand\s+", re.IGNORECASE)
_FALLBACK_FIRM_RE = re.compile(r"^(.*?)\s+recalls?\b", re.IGNORECASE)

_ALLERGEN_KW = [
    "allergen", "allergy", "allergic", "undeclared", "gluten", "peanut",
    "tree nut", "almond", "soy", "soya", "sesame", "milk", "egg", "wheat",
    "shellfish", "mustard", "sulphite", "sulfite",
]
_BIOLOGICAL_KW = [
    "salmonella", "listeria", "e. coli", "e.coli", "escherichia coli",
    "campylobacter", "norovirus", "clostridium", "cronobacter",
    "hepatitis", "pathogen", "bacteria", "mould", "mold",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical",
    "residue", "histamine", "toxin",
]
_PHYSICAL_KW = [
    "extraneous material", "metal", "glass", "plastic", "fragment",
    "foreign object", "foreign material",
]


class CFIARecallsCollector(BaseCollector):
    source_id = "cfia_recalls"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        r = session.get(ENDPOINT, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        records = r.json()

        since_str = since.strftime("%Y-%m-%d") if since else None
        count = 0
        for rec in records:
            if rec.get("Organization") != "CFIA":
                continue
            last_updated = rec.get("Last updated") or ""
            if since_str and last_updated and last_updated < since_str:
                continue
            yield rec
            count += 1
            if limit and count >= limit:
                return

    def normalize(self, raw: dict) -> dict:
        title = raw.get("Title") or ""
        product = raw.get("Product") or ""
        issue = raw.get("Issue") or ""
        category = raw.get("Category") or None
        record_id = str(raw.get("NID") or "")
        last_updated = raw.get("Last updated") or None
        firm = _extract_firm(title)
        text = f"{title} {product} {issue}"

        return {
            "id": f"cfia_recalls::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product, "Canada"),
            "record_url": raw.get("URL"),
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": last_updated,
            "event_initiation_date": last_updated,
            "event_status": "terminated" if str(raw.get("Archived")) == "1" else "ongoing",
            "origin_country": "Canada",
            "distribution_countries": json.dumps(["Canada"]),
            "israel_relevance_flag": 1 if "israel" in text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([]),
            "product_description": product or None,
            "product_category": category,
            "hazard_category": _infer_hazard_category(text),
            "hazard_specific": _extract_hazard_specific(text),
            "severity_raw": raw.get("Recall class") or None,
            "severity_normalized": _normalize_class(raw.get("Recall class")),
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": f"{title}. Category: {category}. Issue: {issue}." if issue else title or None,
            "reason_for_recall": issue or None,
        }


def _extract_firm(title: str) -> str | None:
    m = _BRAND_TITLE_RE.match(title)
    if m:
        return m.group(1).strip(" -") or None
    m = _FALLBACK_FIRM_RE.match(title)
    if m:
        return m.group(1).strip(" -") or None
    return None


def _normalize_class(cls: str | None) -> str | None:
    if not cls:
        return None
    c = cls.lower().strip()
    if not c or c == "--":
        return None
    if "class 1" in c:
        return "high"
    if "class 2" in c:
        return "medium"
    if "class 3" in c:
        return "low"
    return None


def _infer_hazard_category(text: str) -> str | None:
    t = text.lower()
    for kw in _ALLERGEN_KW:
        if kw in t:
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
        "campylobacter", "cronobacter", "norovirus", "clostridium botulinum",
        "hepatitis a", "histamine", "undeclared allergen",
    ]:
        if hazard in t:
            return hazard
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
