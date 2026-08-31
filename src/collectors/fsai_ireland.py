"""Food Safety Authority of Ireland (FSAI) collector — HTML scrape, no RSS/API.

Endpoints (Kentico CMS, server-rendered HTML, checked directly — no feed
tags, no JSON behind the listing):
  Food alerts:     https://www.fsai.ie/news-alerts/food?page=N
  Allergen alerts: https://www.fsai.ie/news-alerts/allergens?page=N
Listing pages link to detail pages under /news-and-alerts/food-alerts/<slug>
and /news-and-alerts/allergen-alerts/<slug> respectively.

This is the first HTML-scraping collector in the project (the other 7 all
consume RSS or JSON). Justified because the underlying detail-page markup
is a simple, stable `<table><td><strong>Label:</strong></td><td>Value</td>`
structure (verified against several live pages) rather than free-flowing
prose — regex extraction is as reliable here as the RSS-label parsing in
fsanz_australia.py.

FSAI splits recalls into two separate registers, each with its own ID
sequence and page:
  - Food Alerts (`Alert Notification: 2026.53`) cover general food-safety
    hazards (microbiological, chemical, physical, mislabelling, THC/CBD
    products, etc.) and carry a real severity classification:
    "Category 1: For Action" (recall/withdrawal action required) vs.
    "Category 2: For Information" (lower urgency). This means, unlike
    cdc_food_safety_rss.py / fsanz_australia.py, FSAI records do NOT hit
    the Bi-Encoder score-suppression issue — there's a genuine classification
    field to feed the model, the same shape as FDA's Class I/II/III.
  - Food Allergen Alerts (`Allergy Alert Notification: 2026.A24`) cover
    undeclared/incorrectly-declared allergens specifically and carry an
    explicit `Allergen(s):` field (e.g. "Nuts and soybeans") instead of a
    Category field. Allergen-labelling issues are handled as a distinct
    workstream under EU FIC Regulation 1169/2011 obligations rather than
    general food-safety hazard rules, and only pose a risk to a specific
    allergic/intolerant subpopulation rather than the general public —
    which is presumably why FSAI (like FSA UK, with its own separate
    "Allergy Alert" notices) tracks and numbers them separately from
    everything else. Because the allergen is already named in a structured
    field here, hazard_category/hazard_specific for this register don't
    need keyword guessing at all.

Backfill depth verified directly: Food Alerts pagination bottoms out at
page 34 (page 35 empty) at ~10 items/page, reaching back to at least
January 2022 (`Alert Notification: 2022.07`) — roughly 4+ years of history,
comparable to what cfia_recalls.py / fsanz_australia.py provide.

Default listing sort is "Updated (newest)" — pages are consumed oldest-
after-newest, so `since` filtering can stop paginating a register entirely
once an item older than the cutoff is seen.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator

from .base import BaseCollector, make_retry_session

BASE_URL = "https://www.fsai.ie"
FOOD_LIST_URL = BASE_URL + "/news-alerts/food?page={page}"
ALLERGEN_LIST_URL = BASE_URL + "/news-alerts/allergens?page={page}"
MAX_PAGES = 200

_LISTING_LINK_RE = re.compile(r'<a class="feature-card[^"]*"\s+href="(/news-and-alerts/[^"]+)"')
_DATE_RE = re.compile(r'<p class="date">([^<]+)</p>')
_TITLE_RE = re.compile(r'<h2>([^<]+)</h2>')
_TABLE_ROW_RE = re.compile(
    r'<td><strong>([^<]+?):?</strong></td>\s*<td>(.*?)</td>', re.DOTALL
)
_SECTION_RE = re.compile(
    r'<p><strong>([^<]+?):?</strong>\s*<br\s*/?>\s*(.*?)</p>\s*(?:</p>)?', re.DOTALL
)

# Same mature keyword lists used by the other 6 collectors (last synced from
# cfia_recalls.py) — applied only to Food Alerts; Allergen Alerts get their
# hazard fields straight from the structured "Allergen(s):" field instead.
_ALLERGEN_KW = ["allergen", "allergy", "allergic", "undeclared", "gluten", "sulphite",
                "sulfite", "improperly declared", "does not declare", "mislabel"]
_ALLERGEN_FOOD_KW = [
    "peanut", "tree nut", "almond", "soy", "soya", "sesame", "milk", "egg",
    "wheat", "shellfish", "mustard",
]
_BIOLOGICAL_KW = [
    "listeria monocytogenes", "clostridium botulinum", "salmonella", "listeria",
    "l. monocytogenes", "e. coli", "e.coli", "escherichia coli", "campylobacter",
    "norovirus", "clostridium", "cronobacter", "hepatitis a", "hepatitis",
    "pathogen", "bacteria", "mould", "mold", "insect", "moth", "larvae",
    "stec", "microbial", "ergot", "alternaria", "aflatoxin", "ochratoxin",
    "zearalenone", "deoxynivalenol", "patulin", "mycotoxin", "muscimol",
    "bacillus", "spoilage", "spoiled", "contracaecum", "contracoecum",
    "s.infantis", "s. infantis", "inflammatory lesions", "toxin", "toxins",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical",
    "residue", "histamine", "nickel", "metronidazole", "sorbic acid",
    "fenthion", "monocrotophos", "tetrahydrocannabinol", "thc",
]
_PHYSICAL_KW = [
    "extraneous material", "metal", "glass", "plastic", "fragment",
    "foreign object", "foreign material", "foreign matter", "choking",
]
_LABELING_KW = [
    "incorrect labelling", "incorrect label", "incorrectly labelled",
    "mislabel", "mispacked", "missing use-by date", "incorrect use-by date",
    "incorrect cooking instruction", "not permitted in food",
    "unauthorised", "unauthorized", "novel food", "medicinal product",
]
_NONCOMPLIANCE_KW = [
    "unregistered establishment", "unregistered premises",
    "without benefit of inspection", "not registered",
]

_FIRM_RE_LIST = [
    re.compile(r'([A-Z][A-Za-z0-9&.\',\- ]{1,60}?)\s+is (?:voluntarily )?recalling'),
    re.compile(r'([A-Z][A-Za-z0-9&.\',\- ]{1,60}?)\s+has (?:voluntarily )?recalled'),
    re.compile(r'([A-Z][A-Za-z0-9&.\',\- ]{1,60}?)\s+is withdrawing'),
]
_TITLE_FIRM_RE = re.compile(
    r'(?i:recall of|update.*?to recall of)\s+(?i:an? |various |specific |additional |all )*'
    r'(?i:batch(?:es)? of )?(?i:various )?([A-Z][\w&.\'\-]*(?:\s+[A-Z][\w&.\'\-]*){0,3})'
)


class FSAIIrelandCollector(BaseCollector):
    source_id = "fsai_ireland"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        count = 0
        for kind, list_url_tpl in (("food", FOOD_LIST_URL), ("allergen", ALLERGEN_LIST_URL)):
            for page in range(1, MAX_PAGES + 1):
                r = session.get(list_url_tpl.format(page=page), timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                links = _LISTING_LINK_RE.findall(r.text)
                if not links:
                    break

                stop_this_register = False
                for path in links:
                    detail_url = BASE_URL + path
                    dr = session.get(detail_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                    if dr.status_code != 200:
                        continue
                    pub_date = _parse_alert_date(dr.text)
                    if since and pub_date and pub_date < since.replace(tzinfo=None):
                        stop_this_register = True
                        break
                    yield {"kind": kind, "url": detail_url, "html": dr.text}
                    count += 1
                    if limit and count >= limit:
                        return
                if stop_this_register:
                    break

    def normalize(self, raw: dict) -> dict:
        html = raw["html"]
        kind = raw["kind"]
        title = _strip_tags(_first(_TITLE_RE, html)) or None
        pub_date = _parse_alert_date(html)
        pub_date_str = pub_date.date().isoformat() if pub_date else None
        fields = _extract_table_fields(html)
        sections = _extract_sections(html)

        message = sections.get("message", "")
        nature_of_danger = sections.get("nature of danger", "")
        action_required = sections.get("action required", "")
        classify_text = " ".join([title or "", fields.get("product identification", ""),
                                   message, nature_of_danger])

        record_id = (fields.get("allergy alert notification")
                     or fields.get("alert notification")
                     or _slug_from_url(raw["url"]))
        origin_country = fields.get("country of origin") or None
        product = fields.get("product identification") or None
        firm = _extract_firm(message) or _extract_firm_from_title(title)

        if kind == "allergen":
            hazard_category = "allergen"
            hazard_specific = fields.get("allergen(s)") or _extract_hazard_specific(classify_text)
            severity_raw = None
            severity_normalized = None
        else:
            category_label, category_desc = _extract_category(html)
            hazard_category = _infer_hazard_category(classify_text)
            hazard_specific = _extract_hazard_specific(classify_text)
            severity_raw = f"{category_label}: {category_desc}" if category_label else None
            severity_normalized = _normalize_category(category_label)

        description_parts = [p for p in [message, nature_of_danger] if p]
        batch_code = fields.get("batch code")
        if batch_code:
            description_parts.append(f"Batch code: {batch_code}")
        description = " ".join(description_parts) or None

        return {
            "id": f"fsai_ireland::{kind}::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product, "Ireland"),
            "record_url": raw["url"],
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": pub_date_str,
            "event_initiation_date": pub_date_str,
            "event_status": None,  # FSAI doesn't publish a formal open/closed status
            "origin_country": origin_country,
            "distribution_countries": json.dumps(["Ireland"]),
            "israel_relevance_flag": 1 if "israel" in classify_text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([]),
            "product_description": product,
            "product_category": None,
            "hazard_category": hazard_category,
            "hazard_specific": hazard_specific,
            "severity_raw": severity_raw,
            "severity_normalized": severity_normalized,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title,
            "description": description,
            "reason_for_recall": action_required or message or None,
        }


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _strip_tags(text: str | None) -> str | None:
    if not text:
        return text
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_alert_date(html: str) -> datetime | None:
    raw = _first(_DATE_RE, html)
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%A, %d %B %Y")
    except ValueError:
        return None


def _extract_table_fields(html: str) -> dict[str, str]:
    """Pull the `<table><td><strong>Label:</strong></td><td>Value</td></table>`
    key/value pairs out of a detail page. Keys are lowercased and stripped of
    a trailing "1"/"2" so "Category 1" and "Category 2" don't need separate
    handling here (that distinction is re-derived by `_extract_category`)."""
    fields: dict[str, str] = {}
    for label, value in _TABLE_ROW_RE.findall(html):
        key = re.sub(r"\s+\d+$", "", label).strip().lower()
        fields[key] = _strip_tags(value) or ""
    return fields


def _extract_category(html: str) -> tuple[str | None, str | None]:
    m = re.search(r'<td><strong>(Category\s*\d+):?</strong></td>\s*<td>([^<]*)</td>', html)
    if not m:
        return None, None
    return m.group(1).strip(), (_strip_tags(m.group(2)) or "").strip()


def _normalize_category(category_label: str | None) -> str | None:
    if not category_label:
        return None
    if "1" in category_label:
        return "high"
    if "2" in category_label:
        return "medium"
    return None


def _extract_sections(html: str) -> dict[str, str]:
    """Pull the free-text `<p><strong>Section:</strong><br/>...</p>` blocks
    (Message / Nature Of Danger / Action Required) that follow the summary
    table."""
    sections: dict[str, str] = {}
    for label, value in _SECTION_RE.findall(html):
        key = label.strip().lower()
        text = _strip_tags(value)
        if text:
            sections[key] = (sections.get(key, "") + " " + text).strip()
    return sections


def _slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _extract_firm(message: str) -> str | None:
    for pattern in _FIRM_RE_LIST:
        m = pattern.search(message)
        if m:
            return m.group(1).strip(" -")
    return None


def _extract_firm_from_title(title: str | None) -> str | None:
    if not title:
        return None
    m = _TITLE_FIRM_RE.search(title)
    if m:
        candidate = m.group(1).strip(" -")
        if candidate and candidate.lower() not in ("update", "additional"):
            return candidate
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
    for kw in _LABELING_KW:
        if kw in t:
            return "fraud"
    for kw in _NONCOMPLIANCE_KW:
        if kw in t:
            return "regulatory"
    return None


def _extract_hazard_specific(text: str) -> str | None:
    """Derived from the same keyword lists _infer_hazard_category uses, not
    a separate candidate list — see rasff.py's _extract_hazard_specific for
    why that matters."""
    t = text.lower()
    if any(kw in t for kw in _ALLERGEN_KW):
        for kw in _ALLERGEN_KW:
            if kw in t:
                return kw
    for hazard in _BIOLOGICAL_KW + _CHEMICAL_KW + _PHYSICAL_KW + _LABELING_KW + _NONCOMPLIANCE_KW:
        if hazard in t:
            return hazard
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
