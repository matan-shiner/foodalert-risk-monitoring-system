"""Food Standards Australia New Zealand (FSANZ) recall RSS collector — Australia only.

Endpoint: https://www.foodstandards.gov.au/food-recalls-rss.xml
Docs:     none published — found via a <link rel> tag on
          https://www.foodstandards.gov.au/food-recalls/recall-alert

Despite the "Australia New Zealand" name, this feed only covers Australia —
verified directly: all 10 items in the live feed were Australian companies/
retailers, and New Zealand runs a completely separate, much larger recall
list via MPI (https://www.mpi.govt.nz/food-safety-home/food-recalls-and-
complaints/recalled-food-products) with no RSS/API of its own, only
server-rendered HTML. NZ coverage is a separate, harder (scraping) problem,
deliberately not attempted here — source_id is `fsanz_australia`, not
`fsanz`, specifically so it never gets assumed to include NZ.

Freshness/completeness were both checked against independent sources before
building this: FSANZ's own pubDate consistently *precedes* independent news
coverage (Inside FMCG, The Nightly) by about a day, and every recall
independently found for the current window matched FSANZ exactly, including
cross-checked against NSW Food Authority's own separate listing. Apparent
gaps (an Aldi gyoza recall, a Tasti protein-ball recall) turned out to be
2025 events aged out of the 10-item rolling window, not real misses.

Like cdc_food_safety_rss.py, this has no formal severity classification —
left honestly null rather than guessed. Unlike CDC's terse one-paragraph
blurb, FSANZ's description HTML has labeled sections ("Problem:", "Food
safety hazard:", "What to do:") which are extracted directly for cleaner
hazard-classification input.

KNOWN ISSUE — Bi-Encoder score suppression (found 2026-08-30, not yet fixed):
the ranking model scores every fsanz_australia record low regardless of the
real hazard. Verified directly: FSANZ's undeclared-allergen recalls score
1.0-2.3/10 on the dashboard's normalized scale, while equivalent
undeclared-allergen recalls from fda_enforcement (same hazard type, "Class I")
score 8.0-8.9/10. The model was trained heavily on text containing a formal
classification field ("Class I", "Class 1", etc.); fsanz_australia's
severity_raw is always null (there's no such field to report), and the
model appears to read that absence itself as a low-severity signal rather
than treating severity as genuinely unknown. This is the same structural
gap cdc_food_safety_rss.py has (no formal classification exists yet), except
permanent here rather than temporary — FSANZ will never publish one.
Deliberately not patched per-source (e.g. injecting a fake severity value
would misrepresent the data); revisit once more no-classification sources
exist and a general fix (e.g. retraining, or a template change that doesn't
penalize "unclassified") can be designed against real variety instead of
one source.
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

ENDPOINT = "https://www.foodstandards.gov.au/food-recalls-rss.xml"

# FSANZ titles are usually "<Firm> - <Product>", sometimes prefixed with
# "UPDATED DD.MM.YY | " when a recall notice is revised.
_UPDATED_PREFIX_RE = re.compile(r"^UPDATED\s+[\d.]+\s*\|\s*", re.IGNORECASE)

_BIOLOGICAL_KW = [
    "listeria monocytogenes", "clostridium botulinum", "salmonella", "listeria",
    "l. monocytogenes", "e. coli", "e.coli", "campylobacter", "norovirus",
    "clostridium", "cyclospora", "hepatitis a", "hepatitis", "pathogen",
    "bacteria", "insect", "moth", "larvae", "stec", "microbial", "ergot",
    "alternaria", "aflatoxin", "ochratoxin", "zearalenone", "deoxynivalenol",
    "patulin", "mycotoxin", "muscimol", "bacillus", "temperature abuse",
    "insanitary conditions", "unsanitary conditions", "contracaecum",
    "contracoecum", "s.infantis", "s. infantis", "inflammatory lesions",
    "toxin", "toxins",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chemical", "residue",
    "histamine", "nickel", "metronidazole", "sorbic acid", "fenthion",
    "monocrotophos",
]
# Unambiguous on their own — deliberately no bare food names ("milk"/"egg"/"wheat")
# here, since those are just common ingredients, not an allergy signal by themselves.
_ALLERGEN_KW = ["allergen", "allergy", "allergic", "undeclared", "does not declare"]
_PHYSICAL_KW = ["metal", "glass", "plastic", "fragment", "foreign object", "foreign matter"]


class FSANZAustraliaCollector(BaseCollector):
    source_id = "fsanz_australia"

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
                "description": item.findtext("description") or "",
                "link": item.findtext("link"),
                "guid": item.findtext("guid"),
                "pub_date": item.findtext("pubDate"),
            }
            count += 1
            if limit and count >= limit:
                return

    def normalize(self, raw: dict) -> dict:
        title = raw.get("title") or ""
        html = raw.get("description") or ""
        problem = _extract_labeled_section(html, "Problem")
        hazard_text = _extract_labeled_section(html, "Food safety hazard")
        classify_text = f"{title} {problem} {hazard_text}"

        record_id = _extract_record_id(raw.get("guid") or "") or title
        firm = _extract_firm(title)
        pub_date = _parse_rss_date(raw.get("pub_date"))
        pub_date_str = pub_date.date().isoformat() if pub_date else None

        return {
            "id": f"fsanz_australia::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(firm, title, "Australia"),
            "record_url": raw.get("link"),
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": pub_date_str,
            "event_initiation_date": pub_date_str,
            "event_status": None,  # FSANZ doesn't publish a formal open/closed status
            "origin_country": "Australia",
            "distribution_countries": json.dumps(["Australia"]),
            "israel_relevance_flag": 1 if "israel" in classify_text.lower() else 0,
            "recalling_firm": firm,
            "brand_names": json.dumps([]),
            "product_description": _strip_firm_prefix(title, firm) or title or None,
            "product_category": None,
            "hazard_category": _infer_hazard_category(classify_text),
            "hazard_specific": _extract_hazard_specific(classify_text),
            # No Class I/II/III-style scale exists here — left honestly null
            # rather than guessed, same convention as cdc_food_safety_rss.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": title or None,
            "description": (problem or hazard_text or None),
            "reason_for_recall": problem or None,
        }


def _parse_rss_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _extract_record_id(guid: str) -> str | None:
    """FSANZ guid looks like '7054 at https://www.foodstandards.gov.au'."""
    m = re.match(r"(\d+)\s+at\s+", guid)
    return m.group(1) if m else (guid or None)


def _extract_labeled_section(html: str, label: str) -> str:
    """Pull the text following a '<h2>Label:</h2><p>...</p>'-style header out
    of FSANZ's description HTML. Falls back to '' if the label isn't present
    (older-style notices sometimes omit "Food safety hazard")."""
    m = re.search(
        rf'{re.escape(label)}:?\s*(?:&nbsp;\s*)*</h2>\s*<p>(.*?)</p>',
        html, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_firm(title: str) -> str | None:
    t = _UPDATED_PREFIX_RE.sub("", title)
    parts = t.split(" - ", 1)
    firm = parts[0].strip() if parts else ""
    return firm or None


def _strip_firm_prefix(title: str, firm: str | None) -> str:
    t = _UPDATED_PREFIX_RE.sub("", title)
    if firm and t.startswith(firm):
        t = t[len(firm):].lstrip(" -")
    return t


def _infer_hazard_category(text: str) -> str | None:
    t = text.lower()
    if any(kw in t for kw in _ALLERGEN_KW):
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
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
