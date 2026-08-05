"""FSA UK Food Alerts API collector.

Endpoint: https://data.food.gov.uk/food-alerts/id
Docs:     https://data.food.gov.uk/food-alerts/ui/reference

Covers Allergy Alerts (AA), Product Recall Information Notices (PRIN),
and Food Alerts for Action (FAFA).
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from typing import Iterator

from .base import BaseCollector, make_retry_session

ENDPOINT = "https://data.food.gov.uk/food-alerts/id"
PAGE_SIZE = 200


class FSAUKCollector(BaseCollector):
    source_id = "fsa_uk"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        offset = 0
        fetched = 0
        session = make_retry_session()
        session.headers.update({"Accept": "application/json"})
        while True:
            params = {"_limit": PAGE_SIZE, "_offset": offset}
            r = session.get(ENDPOINT, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
            if not items:
                return
            for rec in items:
                if since:
                    created = _parse_iso_date(rec.get("created"))
                    if created and created < since.strftime("%Y-%m-%d"):
                        continue
                yield rec
                fetched += 1
                if limit and fetched >= limit:
                    return
            if len(items) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def normalize(self, raw: dict) -> dict:
        record_id = raw.get("notation", "")
        alert_types = raw.get("type", []) or []
        # The 'type' field is a list of URIs; extract short codes.
        type_codes = [t.rsplit("/", 1)[-1] for t in alert_types if isinstance(t, str)]
        is_prin = "PRIN" in type_codes
        is_aa = "AA" in type_codes
        is_fafa = "FAFA" in type_codes

        firm = (raw.get("reportingBusiness") or {}).get("commonName")
        problems = raw.get("problem", []) or []
        product_details = raw.get("productDetails", []) or []

        risk_statement = "; ".join(p.get("riskStatement", "") for p in problems if p.get("riskStatement"))
        product_names = [p.get("productName", "") for p in product_details if p.get("productName")]
        product_text = "; ".join(product_names)

        # FSA UK does not expose FDA-style severity; we map alert type heuristically.
        if is_fafa:
            severity = "high"
        elif is_prin:
            severity = "medium"
        elif is_aa:
            severity = "medium"  # Allergy alerts are serious for affected individuals.
        else:
            severity = None

        return {
            "id": f"fsa_uk::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product_text, "United Kingdom"),
            "record_url": raw.get("alertURL") or raw.get("@id"),
            "ingestion_date": datetime.utcnow().isoformat(timespec="seconds"),
            "source_published_date": _parse_iso_date(raw.get("created")),
            "event_initiation_date": _parse_iso_date(raw.get("created")),
            "event_status": (raw.get("status") or {}).get("label", "").lower() or None,
            "origin_country": "United Kingdom",
            "distribution_countries": json.dumps(["United Kingdom"]),
            "israel_relevance_flag": 1 if "israel" in json.dumps(raw, default=str).lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps(product_names[:5]),
            "product_description": product_text or None,
            "product_category": None,
            "hazard_category": _infer_hazard_category(type_codes, risk_statement),
            "hazard_specific": _extract_hazard_specific(risk_statement),
            "severity_raw": "/".join(type_codes) or None,
            "severity_normalized": severity,
            "population_at_risk": "allergic" if is_aa else None,
            "illness_count_reported": None,
            "title": raw.get("title") or raw.get("shortTitle"),
            "description": risk_statement or None,
            "reason_for_recall": risk_statement or None,
        }


def _parse_iso_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        # Handle both date-only and full timestamp formats.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10] if len(s) >= 10 else s


_BIOLOGICAL_KW = ["salmonella", "listeria monocytogenes", "listeria", "l. monocytogenes",
                  "e. coli", "e.coli", "campylobacter", "norovirus",
                  "clostridium botulinum", "clostridium", "bacterial", "pathogen",
                  "mould", "mold", "insect", "moth", "larvae", "stec", "microbial",
                  "bacteria", "ergot", "alternaria",
                  # Mycotoxins are fungal in origin — biological, matching
                  # rasff.py's convention, not "chemical" just because
                  # they're detected as a chemical assay result.
                  "aflatoxin", "ochratoxin", "zearalenone", "deoxynivalenol",
                  "patulin", "mycotoxin", "muscimol", "bacillus",
                  "temperature abuse", "insanitary conditions",
                  "unsanitary conditions", "contracaecum", "contracoecum",
                  "s.infantis", "s. infantis", "inflammatory lesions",
                  "toxin", "toxins"]
_CHEMICAL_KW = ["pesticide", "lead", "cadmium", "mercury", "arsenic",
                "fenthion", "monocrotophos",
                "ethylene oxide", "chemical", "residue", "histamine",
                "nickel", "metronidazole", "sorbic acid"]
_PHYSICAL_KW = ["metal", "glass", "plastic piece", "foreign body", "fragment"]


def _infer_hazard_category(type_codes: list, risk_text: str) -> str | None:
    if "AA" in type_codes:
        return "allergen"
    text = (risk_text or "").lower()
    for kw in _BIOLOGICAL_KW:
        if kw in text:
            return "biological"
    for kw in _CHEMICAL_KW:
        if kw in text:
            return "chemical"
    for kw in _PHYSICAL_KW:
        if kw in text:
            return "physical"
    return None


def _extract_hazard_specific(risk_text: str) -> str | None:
    """Derived from the same _BIOLOGICAL_KW/_CHEMICAL_KW lists
    _infer_hazard_category uses, not a separate candidate list — see
    rasff.py's _extract_hazard_specific for why that matters."""
    if not risk_text:
        return None
    text = risk_text.lower()
    for kw in _BIOLOGICAL_KW + _CHEMICAL_KW:
        if kw in text:
            return kw
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
