"""openFDA food enforcement reports collector.

Endpoint: https://api.fda.gov/food/enforcement.json
Docs:     https://open.fda.gov/apis/food/enforcement/
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from typing import Iterator

from .base import BaseCollector, make_retry_session

ENDPOINT = "https://api.fda.gov/food/enforcement.json"
PAGE_SIZE = 1000  # openFDA hard cap is 1000.


class FDAEnforcementCollector(BaseCollector):
    source_id = "fda_enforcement"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        skip = 0
        fetched = 0
        session = make_retry_session()
        while True:
            page_size = min(PAGE_SIZE, (limit - fetched) if limit else PAGE_SIZE)
            if page_size <= 0:
                break
            # openFDA's Lucene parser rejects URL-encoded `:` `[` `]`, so we build
            # the search query manually and only let requests encode the values it owns.
            url = f"{ENDPOINT}?limit={page_size}&skip={skip}"
            if since:
                date_str = since.strftime("%Y%m%d")
                today_str = datetime.utcnow().strftime("%Y%m%d")
                url += f"&search=report_date:[{date_str}+TO+{today_str}]"
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                # openFDA returns 404 when results exhausted.
                return
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            if not results:
                return
            for rec in results:
                yield rec
                fetched += 1
                if limit and fetched >= limit:
                    return
            if len(results) < page_size:
                return
            skip += page_size

    def normalize(self, raw: dict) -> dict:
        record_id = raw.get("recall_number", "")
        title = self._build_title(raw)
        description = raw.get("reason_for_recall", "")
        product_desc = raw.get("product_description", "")
        classify_text = f"{product_desc} {description}"

        return {
            "id": f"fda_enforcement::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(raw.get("recalling_firm"), product_desc, raw.get("country")),
            "record_url": f"https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts?search_api_fulltext={record_id}",
            "ingestion_date": datetime.utcnow().isoformat(timespec="seconds"),
            "source_published_date": _parse_fda_date(raw.get("report_date")),
            "event_initiation_date": _parse_fda_date(raw.get("recall_initiation_date")),
            "event_status": (raw.get("status") or "").lower() or None,
            "origin_country": raw.get("country"),
            "distribution_countries": json.dumps(_extract_distribution(raw.get("distribution_pattern", ""))),
            "israel_relevance_flag": _is_israel_relevant(raw),
            "recalling_firm": raw.get("recalling_firm"),
            "brand_names": json.dumps([]),
            "product_description": product_desc,
            "product_category": _infer_product_category(classify_text),
            "hazard_category": _infer_hazard_category(classify_text),
            "hazard_specific": _extract_hazard_specific(classify_text),
            "severity_raw": raw.get("classification"),
            "severity_normalized": _normalize_fda_class(raw.get("classification")),
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title,
            "description": description,
            "reason_for_recall": raw.get("reason_for_recall"),
        }

    @staticmethod
    def _build_title(raw: dict) -> str:
        firm = str(raw.get("recalling_firm") or "")
        cls = str(raw.get("classification") or "")
        product = str(raw.get("product_description") or "")[:80]
        return f"{firm} — {cls} — {product}".strip(" —")


def _parse_fda_date(date_str: str | None) -> str | None:
    """openFDA dates are YYYYMMDD strings."""
    if not date_str:
        return None
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str


def _normalize_fda_class(classification: str | None) -> str | None:
    if not classification:
        return None
    c = classification.lower()
    if "class i" in c and "ii" not in c and "iii" not in c:
        return "high"
    if "class ii" in c and "iii" not in c:
        return "medium"
    if "class iii" in c:
        return "low"
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()


def _extract_distribution(distribution_pattern: str) -> list:
    """Best-effort extraction of country/state codes from free-text distribution."""
    if not distribution_pattern:
        return []
    # The field is usually US states, sometimes "Nationwide" or country names.
    return [distribution_pattern]  # Keep full text — truncation causes Israel detection to fail in display.


def _is_israel_relevant(raw: dict) -> int:
    blob = json.dumps(raw, default=str).lower()
    return 1 if "israel" in blob else 0


# ── hazard / product classification ─────────────────────────────────────────
# openFDA's food/enforcement.json has no category or hazard-type field at all
# (its `openfda` cross-reference block, populated for drugs/devices, is always
# empty for food records) — everything below is inferred from free text in
# `product_description` + `reason_for_recall`. Same keyword-matching approach
# already used in fsa_uk.py / rasff.py / cfia_recalls.py / cdc_food_safety_rss.py;
# it will miss non-contamination recalls (labeling, process violations) the
# same way those do.

# Unambiguous on their own — safe to match without extra context.
_ALLERGEN_KW = [
    "allergen", "allergy", "allergic", "undeclared", "gluten", "sulphite",
    "sulfite", "does not declare",
]
# Only mean "allergen recall" when paired with an _ALLERGEN_KW context word above —
# bare "milk"/"egg"/"wheat" are just common ingredients, not an allergy signal on
# their own (a Listeria-in-cheese recall's product_description says "milk" too).
_ALLERGEN_FOOD_KW = [
    "peanut", "tree nut", "almond", "walnut", "cashew", "pistachio", "hazelnut",
    "pecan", "soy", "soya", "sesame", "milk", "egg", "wheat", "shellfish", "mustard",
]
_BIOLOGICAL_KW = [
    "listeria monocytogenes", "clostridium botulinum", "salmonella", "listeria",
    "l. monocytogenes", "e. coli", "e.coli", "escherichia coli", "campylobacter",
    "norovirus", "clostridium", "cronobacter", "cyclospora", "hepatitis a",
    "hepatitis", "pathogen", "bacteria", "mold", "mould", "insect", "moth",
    "larvae", "stec", "microbial", "ergot", "alternaria",
    # Mycotoxins are fungal in origin — treated as biological, matching
    # rasff.py's convention (the most complete of the 6 collectors), not
    # dropped into "chemical" just because they're detected as a chemical
    # assay result. "aflatoxin" was previously miscategorized as chemical
    # here despite rasff.py already treating it as biological.
    "aflatoxin", "ochratoxin", "zearalenone", "deoxynivalenol", "patulin",
    "mycotoxin", "muscimol", "bacillus",
    # Spoilage/hygiene-failure language — no pathogen named, but the failure
    # mode itself is the biological risk (temperature abuse and inadequate
    # pasteurization both mean surviving/growing pathogens, not a named one).
    "temperature abuse", "insanitary conditions", "unsanitary conditions",
    "not adequately pasteurized", "inadequately pasteurized",
    "pasteurization was not achieved", "spoiled", "contracaecum",
    "contracoecum", "s.infantis", "s. infantis", "inflammatory lesions",
    "toxin", "toxins",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical",
    "residue", "histamine", "nickel", "metronidazole", "fenthion",
    "monocrotophos",
    "sorbic acid", "fluid from a reach truck", "hydraulic fluid",
    # No dedicated "radiological" bucket exists in the dashboard's hazard
    # taxonomy (HAZARD_COLORS has biological/chemical/allergen/physical/
    # fraud/regulatory only) — filed under chemical as the closest fit
    # rather than left unclassified or inventing a 7th category unasked.
    "cesium-137", "cs-137",
]
# Checked last in _infer_hazard_category, same reasoning as rasff.py's
# _NONCOMPLIANCE_KW: only reached when nothing more specific matched, so a
# named hazardous substance still wins as "chemical"/"biological" over the
# generic "regulatory" bucket.
_NONCOMPLIANCE_KW = ["unapproved drug claim", "unapproved new drug"]
_PHYSICAL_KW = [
    "extraneous material", "foreign material", "foreign object", "metal",
    "glass", "plastic", "rubber", "fragment",
]

# (label, keyword list) — checked in order, first match wins. Order favors
# more specific categories (supplements, infant food, prepared/RTE dishes)
# over generic ingredient-based ones, so e.g. a chicken salad kit lands in
# "prepared dishes and salads" rather than "meat and poultry products".
_PRODUCT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("dietary supplements", [
        "dietary supplement", "capsule", "softgel", "herbal supplement",
        "vitamin", "protein powder", "moringa",
    ]),
    ("infant and toddler food", [
        "infant formula", "baby food", "infant", "toddler",
    ]),
    ("prepared dishes and salads", [
        "salad kit", "ready-to-eat", "ready to eat", "prepared meal",
        "entrée", "entree", "sandwich", "wrap", "burrito", "salad",
    ]),
    ("seafood and fish products", [
        "fish", "shrimp", "salmon", "tuna", "shellfish", "seafood", "crab",
        "lobster", "oyster", "clam", "scallop",
    ]),
    ("meat and poultry products", [
        "beef", "pork", "chicken", "turkey", "poultry", "sausage", "bacon",
        "ham", "meat",
    ]),
    ("dairy and eggs", [
        "milk", "cheese", "yogurt", "yoghurt", "dairy", "butter", "cream", "egg",
    ]),
    ("bakery and grain products", [
        "bread", "bakery", "cookie", "cracker", "cereal", "flour", "pasta",
        "oat", "rice", "tortilla", "bagel",
    ]),
    ("confectionery and snacks", [
        "chocolate", "candy", "snack", "chip", "confection", "gummy",
    ]),
    ("nuts, seeds and nut products", [
        "peanut", "almond", "walnut", "cashew", "pistachio", "sesame seed",
        "sunflower seed", "nut butter", "hazelnut", "pecan",
    ]),
    ("herbs and spices", [
        "spice", "herb", "seasoning", "cumin", "cinnamon", "paprika",
        "turmeric", "oregano", "basil",
    ]),
    ("sauces, condiments and beverages", [
        "sauce", "condiment", "dressing", "salsa", "juice", "beverage",
        "drink", "tea", "coffee",
    ]),
    ("fruits and vegetables", [
        "lettuce", "tomato", "spinach", "kale", "produce", "vegetable",
        "fruit", "berries", "apple", "onion", "potato", "mushroom",
        "cucumber", "pepper", "carrot",
    ]),
]


def _infer_product_category(text: str) -> str | None:
    t = text.lower()
    for label, keywords in _PRODUCT_CATEGORIES:
        if any(kw in t for kw in keywords):
            return label
    return None


def _infer_hazard_category(text: str) -> str | None:
    t = text.lower()
    if any(kw in t for kw in _ALLERGEN_KW):
        return "allergen"
    if "sensitiv" in t and any(kw in t for kw in _ALLERGEN_FOOD_KW):
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
    for kw in _NONCOMPLIANCE_KW:
        if kw in t:
            return "regulatory"
    return None


def _extract_hazard_specific(text: str) -> str | None:
    """Derived from the same _ALLERGEN_KW/_BIOLOGICAL_KW/_CHEMICAL_KW lists
    _infer_hazard_category uses, not a separate candidate list — see
    rasff.py's _extract_hazard_specific for why that matters."""
    t = text.lower()
    if any(kw in t for kw in _ALLERGEN_KW):
        for kw in _ALLERGEN_KW:
            if kw in t:
                return kw
    for hazard in _BIOLOGICAL_KW + _CHEMICAL_KW:
        if hazard in t:
            return hazard
    for hazard in _NONCOMPLIANCE_KW:
        if hazard in t:
            return hazard
    return None
