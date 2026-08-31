"""Singapore Food Agency (SFA) recall RSS collector.

Endpoint: https://www.sfa.gov.sg/rss/annual-listing-food-alerts
Docs:     none published — found via direct URL guess/verification, not
          linked from SFA's own newsroom page navigation.

Despite the retained "AVA Newsroom" branding (AVA = Agri-Food & Veterinary
Authority, SFA's pre-2019 predecessor), the feed is live and current
(verified against today's date) and is already scoped to food alerts and
recalls specifically — unlike SFA's general Newsroom listing, which mixes
in press releases, land tenders, and enforcement fines with no working
category filter found. Only ~40 items are exposed via the feed at any
time, rolling across roughly the last two years — no deeper archive was
found, so this source has real backfill-depth limits (~2 years) compared
to fsai_ireland.py (4+ years) or cfia_recalls.py.

Detail pages are freeform prose (numbered paragraphs inside
`<div class="sfContentBlock sf-Long-text">`), not a labeled table like
fsai_ireland.py — there is no formal severity/classification field here
either. This is the third source (after cdc_food_safety_rss.py and
fsanz_australia.py) to hit the documented Bi-Encoder score-suppression
issue; per prior instruction, still deliberately not patched per-source.

Firm/product/origin-country extraction is done from the RSS title alone
(reliable and consistent: "Recall of <Brand> <Product> ... due to <hazard>",
often with "from <Country>") rather than by parsing the prose detail page,
which — unlike FSAI's uniform table markup — varies too much notice-to-
notice for reliable structured extraction. The detail page is still fetched
for one thing: a cleaned, tag-stripped version of its narrative text, used
only to enrich the classification input and the stored description, not
for structured fields.
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

ENDPOINT = "https://www.sfa.gov.sg/rss/annual-listing-food-alerts"

_TITLE_PREFIX_RE = re.compile(
    r"^(?:additional\s+)?(?:update:?\s*)?(?:recall\s+of\s+)?",
    re.IGNORECASE,
)
_DUE_TO_RE = re.compile(r"\bdue\s+to\b", re.IGNORECASE)
_FROM_COUNTRY_RE = re.compile(r"\bfrom\s+(?:the\s+)?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})")
_CONTENT_BLOCK_RE = re.compile(
    r'<div class="sfContentBlock sf-Long-text"[^>]*>(.*?)</div>', re.DOTALL
)
_PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
_LEADING_NUMBER_RE = re.compile(r"^\d{1,2}\s+")

_FIRM_RE_LIST = [
    re.compile(r"directed the importer,\s*([^,]+?),\s*to recall", re.IGNORECASE),
    re.compile(r"directed the manufacturer,\s*([^,]+?),\s*to recall", re.IGNORECASE),
    re.compile(r"directed the distributor,\s*([^,]+?),\s*to recall", re.IGNORECASE),
]

# Reused/extended from fsai_ireland.py's mature lists, plus a few terms
# specific to hazard types seen only in SFA notices (cyclamate/saccharin
# exceedances, tadalafil adulteration, rust in cans, B. cereus/cereulide
# abbreviations that don't match the existing "bacillus"/"toxin" substrings).
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
    "hepatitis", "pathogen", "bacteria", "mould", "mold", "insect", "moth",
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
    "without benefit of inspection", "not registered",
]


class SFASingaporeCollector(BaseCollector):
    source_id = "sfa_singapore"

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

            link = item.findtext("link")
            description = ""
            if link:
                try:
                    dr = session.get(link, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                    dr.encoding = "utf-8"
                    if dr.status_code == 200:
                        description = _extract_description(dr.text)
                except Exception:
                    pass

            yield {
                "title": (item.findtext("title") or "").strip(),
                "guid": item.findtext("guid"),
                "link": link,
                "pub_date": item.findtext("pubDate"),
                "description": description,
            }
            count += 1
            if limit and count >= limit:
                return

    def normalize(self, raw: dict) -> dict:
        title = raw.get("title") or ""
        description = raw.get("description") or ""
        classify_text = f"{title} {description}"

        record_id = raw.get("guid") or title
        pub_date = _parse_rss_date(raw.get("pub_date"))
        pub_date_str = pub_date.date().isoformat() if pub_date else None

        firm = _extract_firm(description) or _extract_firm_from_title(title)
        origin_country = _extract_origin_country(title)
        product = _strip_title_to_product(title)

        return {
            "id": f"sfa_singapore::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, product, "Singapore"),
            "record_url": raw.get("link"),
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": pub_date_str,
            "event_initiation_date": pub_date_str,
            "event_status": None,  # SFA doesn't publish a formal open/closed status
            "origin_country": origin_country,
            "distribution_countries": json.dumps(["Singapore"]),
            "israel_relevance_flag": 1 if "israel" in classify_text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([]),
            "product_description": product or title or None,
            "product_category": None,
            "hazard_category": _infer_hazard_category(classify_text),
            "hazard_specific": _extract_hazard_specific(classify_text),
            # No formal classification scale exists here — left honestly null,
            # same convention as cdc_food_safety_rss.py / fsanz_australia.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": description or title or None,
            "reason_for_recall": title or None,
        }


def _parse_rss_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        # SFA uses "DD Mon YYYY HH:MM AM/PM" — not RFC-2822, parsedate_to_datetime fails.
        try:
            return datetime.strptime(s.strip(), "%d %b %Y %I:%M %p")
        except ValueError:
            return None


def _extract_description(html: str) -> str:
    """Pull the main narrative `<div class="sfContentBlock sf-Long-text">`
    block (the first one — the second, if present, is just the
    "Issued by the Singapore Food Agency" footer), strip tags, and drop the
    "2    ", "3    "-style leading paragraph numbers SFA prefixes onto
    every paragraph after the first."""
    blocks = _CONTENT_BLOCK_RE.findall(html)
    if not blocks:
        return ""
    main = blocks[0]
    paragraphs = []
    for p in _PARA_RE.findall(main):
        text = _strip_tags(p)
        if not text:
            continue
        text = _LEADING_NUMBER_RE.sub("", text)
        paragraphs.append(text)
    return " ".join(paragraphs)


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#39;|&rsquo;", "'", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_firm(description: str) -> str | None:
    for pattern in _FIRM_RE_LIST:
        m = pattern.search(description)
        if m:
            return m.group(1).strip(" -")
    return None


def _extract_firm_from_title(title: str) -> str | None:
    """Titles are typically "Recall of <Brand> <Product> due to <hazard>" —
    take the capitalized-word run right after the "Recall of" prefix, up to
    "due to". Agentless titles ("<Product> found to be adulterated...")
    correctly yield None here."""
    t = _TITLE_PREFIX_RE.sub("", title, count=1)
    m = _DUE_TO_RE.search(t)
    head = t[:m.start()] if m else t
    m2 = re.match(r"([A-Z][\w&.\'\-]*(?:\s+[A-Z][\w&.\'\-]*){0,3})", head)
    if m2:
        candidate = m2.group(1).strip(" -")
        excluded = ("update", "additional", "various", "one", "two", "three",
                    "four", "five", "six", "seven", "eight", "nine", "ten",
                    "select", "multiple", "several")
        if candidate and candidate.split()[0].lower() not in excluded:
            return candidate
    return None


def _extract_origin_country(title: str) -> str | None:
    m = _FROM_COUNTRY_RE.search(title)
    return m.group(1).strip() if m else None


def _strip_title_to_product(title: str) -> str:
    t = _TITLE_PREFIX_RE.sub("", title, count=1)
    m = _DUE_TO_RE.search(t)
    return (t[:m.start()] if m else t).strip(" -,")


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
