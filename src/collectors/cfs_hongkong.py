"""Centre for Food Safety (CFS), Hong Kong — recall collector.

Endpoint (backfill + ongoing): static per-year listing pages
  https://www.cfs.gov.hk/english/whatsnew/whatsnew_fa/whatsnew_fa_{year}.html
Endpoint (per-record detail): linked from each listing row
  https://www.cfs.gov.hk/english/whatsnew/whatsnew_fa/{year}_{n}.html

CFS also publishes an RSS feed (foodalert.xml, mirrored on data.gov.hk) with
the same content pre-flattened into CDATA `<p>Label: Value</p>` blocks, but
it only exposes a rolling ~10-item/~3-month window. The year-listing pages
cover the full public archive back to 2006 via simple static HTML (no JS/
AJAX needed to page through it — the year dropdown on the live site is
client-side only; the underlying pages it links to are plain GETs), so
this collector uses those directly for both backfill and ongoing ingestion
and does not need the RSS feed at all.

Listing rows are already tagged `data-category="1"` (Food alert) or `"2"`
(Allergy alert) — CFS's own version of the allergen-vs-general-hazard split
seen in fsai_ireland.py, but as an inline attribute rather than a separate
URL tree. Detail pages use a stable `<table class="colorTable1">` template
(Issue Date / Source of Information / Food Product / Product Name and
Description / Reason For Issuing Alert / Action Taken...) confirmed
identical across a 2020 sample and a 2026 sample — this is the richest,
most consistent structured format found across all collectors so far,
including a real "Reason For Issuing Alert" bullet list, better than
fsai_ireland.py's free-text paragraphs.

No formal severity/classification field exists here either (like
cdc_food_safety_rss.py / fsanz_australia.py / sfa_singapore.py) — this is
the fourth source to hit the documented Bi-Encoder score-suppression
issue. Still deliberately not patched per-source.

This collector directly closes the Frisolac Prestige infant formula /
Hong Kong-Macao gap that was the very first investigation of this project.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator

from .base import BaseCollector, make_retry_session

BASE_URL = "https://www.cfs.gov.hk"
YEAR_LIST_URL_TPL = BASE_URL + "/english/whatsnew/whatsnew_fa/whatsnew_fa_{year}.html"
EARLIEST_YEAR = 2006

_ROW_RE = re.compile(
    r'<tr class="datarow" data-category="(\d)">\s*'
    r'<td class="subHeader">([\d.]+).*?</td>\s*'
    r'<td class="categoryfield">[^<]*</td>\s*'
    r'<td>\s*<ul>\s*<li><a href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TABLE_RE = re.compile(r'<table class="colorTable1">(.*?)</table>', re.DOTALL)
_ROW_FIELD_RE = re.compile(r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', re.DOTALL)
_LINE_SPLIT_RE = re.compile(r'</p>\s*<p>|<br\s*/?>', re.IGNORECASE)
_LABEL_VALUE_RE = re.compile(r'^([^:]{2,40}):\s*(.+)$')
_RECORD_ID_RE = re.compile(r'/(\d{4}_\d+)\.html')

# Reused/extended from sfa_singapore.py's lists (most recently maintained set).
_ALLERGEN_KW = ["allergen", "allergy", "allergic", "undeclared", "gluten", "sulphite",
                "sulfite", "sulphur dioxide", "sulfur dioxide", "improperly declared",
                "does not declare", "mislabel"]
_ALLERGEN_FOOD_KW = [
    "peanut", "tree nut", "almond", "soy", "soya", "sesame", "milk", "egg",
    "wheat", "shellfish", "mustard",
]
_BIOLOGICAL_KW = [
    "listeria monocytogenes", "clostridium botulinum", "salmonella", "listeria",
    "l. monocytogenes", "e. coli", "e.coli", "escherichia coli", "shigatoxin",
    "campylobacter", "norovirus", "clostridium", "cronobacter", "hepatitis a",
    "pathogen", "bacteria", "mould", "mold", "insect", "moth",
    "larvae", "stec", "microbial", "ergot", "alternaria", "aflatoxin",
    "ochratoxin", "zearalenone", "deoxynivalenol", "patulin", "mycotoxin",
    "muscimol", "bacillus", "b. cereus", "cereulide", "spoilage", "spoiled",
    "contracaecum", "contracoecum", "s.infantis", "s. infantis",
    "inflammatory lesions", "toxin", "toxins",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical",
    "residue", "histamine", "nickel", "metronidazole", "sorbic acid",
    "fenthion", "monocrotophos", "cyclamate", "saccharin", "tadalafil",
    "ethylene oxide", "mineral oil", "formaldehyde", "methylmercury",
]
_PHYSICAL_KW = [
    "extraneous material", "metal", "glass", "plastic", "fragment",
    "foreign object", "foreign material", "foreign matter", "choking",
    "rust", "rubber",
]
_LABELING_KW = [
    "incorrect labelling", "incorrect label", "incorrectly labelled",
    "mislabel", "mispacked", "missing use-by date", "incorrect use-by date",
    "not permitted in food", "not permitted for use in food", "adulterated",
    "unauthorised", "unauthorized", "novel food", "medicinal product",
]
_NONCOMPLIANCE_KW = [
    "unregistered establishment", "unregistered premises",
    "without benefit of inspection", "not registered", "breached",
]


class CFSHongKongCollector(BaseCollector):
    source_id = "cfs_hongkong"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        count = 0
        current_year = datetime.now(timezone.utc).year

        for year in range(current_year, EARLIEST_YEAR - 1, -1):
            r = session.get(YEAR_LIST_URL_TPL.format(year=year), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue

            rows = _ROW_RE.findall(r.text)
            for category_num, date_str, href, title_html in rows:
                pub_date = _parse_hk_date(date_str)
                if since and pub_date and pub_date < since.replace(tzinfo=None):
                    return

                title = _strip_tags(title_html)
                detail_url = BASE_URL + href if href.startswith("/") else href
                dr = session.get(detail_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                if dr.status_code != 200:
                    continue

                yield {
                    "category_num": category_num,
                    "title": title,
                    "url": detail_url,
                    "pub_date": pub_date.isoformat() if pub_date else None,
                    "html": dr.text,
                }
                count += 1
                if limit and count >= limit:
                    return

    def normalize(self, raw: dict) -> dict:
        html = raw["html"]
        title = raw.get("title") or _first(re.compile(r"<h2>(.*?)</h2>"), html) or ""
        title = _strip_tags(title)
        fields = _extract_table_fields(html)
        subfields = _extract_subfields(fields.get("product name and description", ""))

        reason = _extract_list_text(fields.get("reason for issuing alert", ""))
        action = _extract_list_text(fields.get("action taken by the centre for food safety", ""))
        food_product = _strip_tags(fields.get("food product", "")) or None

        record_id = _first(_RECORD_ID_RE, raw["url"]) or title
        pub_date_str = (raw.get("pub_date") or "")[:10] or None

        classify_text = " ".join([title, food_product or "", reason, action])
        is_allergy_type = raw.get("category_num") == "2"

        firm = (subfields.get("importer") or subfields.get("manufacturer")
                or subfields.get("distributor") or subfields.get("retailer")
                or subfields.get("vendor"))
        product = (subfields.get("product name") or subfields.get("product")
                   or food_product or title or None)
        origin_country = subfields.get("place of origin")

        description_parts = [p for p in [reason, action] if p]
        description = " ".join(description_parts) or None

        return {
            "id": f"cfs_hongkong::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product, "Hong Kong"),
            "record_url": raw["url"],
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": pub_date_str,
            "event_initiation_date": pub_date_str,
            "event_status": None,  # CFS doesn't publish a formal open/closed status
            "origin_country": origin_country,
            "distribution_countries": json.dumps(["Hong Kong"]),
            "israel_relevance_flag": 1 if "israel" in classify_text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([subfields["brand"]] if subfields.get("brand") else []),
            "product_description": product,
            "product_category": food_product,
            "hazard_category": "allergen" if is_allergy_type else _infer_hazard_category(classify_text),
            "hazard_specific": _extract_hazard_specific(classify_text),
            # No formal classification scale exists here — left honestly null,
            # same convention as cdc_food_safety_rss.py / sfa_singapore.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": description or title or None,
            "reason_for_recall": reason or title or None,
        }


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#39;|&rsquo;", "'", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_hk_date(s: str) -> datetime | None:
    """'DD.M.YYYY' → datetime."""
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y")
    except ValueError:
        return None


def _extract_table_fields(html: str) -> dict[str, str]:
    """Pull the `<table class="colorTable1"><th>Label</th><td>Value</td>...`
    key/value pairs out of a CFS detail page."""
    m = _TABLE_RE.search(html)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for label, value in _ROW_FIELD_RE.findall(m.group(1)):
        fields[_strip_tags(label).lower()] = value  # keep raw HTML for sub-parsing
    return fields


def _extract_subfields(product_cell_html: str) -> dict[str, str]:
    """The "Product Name and Description" cell packs multiple "Label: Value"
    lines into one field, inconsistently — sometimes as separate `<p>` tags,
    sometimes as a single `<p>` with `<br/>`-separated lines. Split on both
    boundaries first, then peel "Label: Value" off each resulting line."""
    inner = re.sub(r"^\s*<p>|</p>\s*$", "", product_cell_html.strip(), flags=re.IGNORECASE)
    subfields: dict[str, str] = {}
    for line in _LINE_SPLIT_RE.split(inner):
        text = _strip_tags(line)
        m = _LABEL_VALUE_RE.match(text)
        if m:
            subfields[m.group(1).strip().lower()] = m.group(2).strip()
    return subfields


def _extract_list_text(cell_html: str) -> str:
    items = re.findall(r"<li>(.*?)</li>", cell_html, re.DOTALL)
    if items:
        return " ".join(_strip_tags(i) for i in items)
    return _strip_tags(cell_html)


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
