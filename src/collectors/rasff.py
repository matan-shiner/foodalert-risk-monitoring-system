"""RASFF (Rapid Alert System for Food and Feed) collector.

Endpoint: POST https://webgate.ec.europa.eu/rasff-window/backend/public/notification/search/consolidated/
Docs:     https://webgate.ec.europa.eu/rasff-window/screen/search  (public portal)

No authentication required. Returns up to ~31k notifications (food + feed).
Paginated DESC by ecValidationDate — incremental fetch stops early on `since`.

Israel relevance (found 2026-08-31): the bulk search endpoint above only
returns `subject`/`notifyingCountry`/`originCountries` — free text plus two
of RASFF's four organisation roles. RASFF also tags a per-notification
"Organisations" list with all four roles (Notifying/Origin/Distribution/
**Operator** — the country of the business entity implicated, independent
of geography) via a separate per-notification detail endpoint:
  GET https://webgate.ec.europa.eu/rasff-window/backend/public/notification/view/id/{notifId}/en/
  → `organizationFlags`: [{"organization": {"code": "IL", "description":
     "Israel", ...}, "notificationFlags": [{"flagType": "OPERATOR"}, ...]}]
A country can appear here (e.g. as an Operator) without ever being named in
the free-text subject, so the old `"israel" in subject.lower()` check missed
real cases — e.g. notification 2026.7490 involved an Israeli operator but
never says "Israel" in its subject. Fixed by fetching this detail endpoint
per record and checking for country code "IL" in the organisation list,
in addition to (not instead of) the subject-text check. This adds one HTTP
call per record, acceptable for normal day-to-day incremental ingestion
(a handful of new records/day) — a one-time backfill script was used
separately to retrofit `israel_relevance_flag` on the pre-existing ~11k
historical rows without re-ingesting them.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Iterator

from .base import BaseCollector, make_retry_session

SEARCH_URL = (
    "https://webgate.ec.europa.eu/rasff-window/backend/public"
    "/notification/search/consolidated/"
)
DETAIL_URL_TPL = (
    "https://webgate.ec.europa.eu/rasff-window/backend/public"
    "/notification/view/id/{notif_id}/en/"
)
PAGE_SIZE = 100
TIMEOUT = 30

# RASFF notification classification → severity_normalized
_CLASSIFICATION_SEVERITY = {
    "alert notification":                   "high",
    "information notification for follow-up": "medium",
    "information notification for attention": "medium",
    "border rejection":                     "high",
    "news":                                 "low",
}

# RASFF riskDecision → severity_normalized (overrides classification when present)
_RISK_SEVERITY = {
    "serious":          "high",
    "potential risk":   "medium",
    "not serious":      "low",
    "no risk":          "low",
    "undecided":        "medium",
}


class RASFFCollector(BaseCollector):
    source_id = "rasff"

    def fetch_raw(
        self,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[dict]:
        """Page through results DESC by ecValidationDate, stop when records predate `since`."""
        since_date = since.strftime("%Y-%m-%d") if since else None
        page = 1
        fetched = 0

        session = make_retry_session()
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

        while True:
            payload = {
                "parameters": {
                    "pageNumber": page,
                    "pageSize": PAGE_SIZE,
                    "sortField": "notificationDate",
                    "sortOrder": "DESC",
                }
            }
            r = session.post(SEARCH_URL, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()

            items = data.get("notifications", [])
            if not items:
                return

            for item in items:
                # Check since cutoff — records sorted DESC so first stale item = done
                if since_date:
                    pub = _parse_rasff_date(item.get("ecValidationDate"))
                    if pub and pub < since_date:
                        return

                notif_id = item.get("notifId")
                if notif_id:
                    item["_israel_operator"] = _check_israel_organisation(session, notif_id)

                yield item
                fetched += 1
                if limit and fetched >= limit:
                    return

            if page >= data.get("totalPages", 1):
                return
            page += 1

    def normalize(self, raw: dict) -> dict:
        reference = raw.get("reference", "")
        notif_id = str(raw.get("notifId", ""))
        record_id = reference or notif_id

        subject = raw.get("subject") or ""
        product_cat = (raw.get("productCategory") or {}).get("description")
        product_type = (raw.get("productType") or {}).get("description", "")

        classification = (raw.get("notificationClassification") or {}).get("description", "").lower()
        risk_desc = (raw.get("riskDecision") or {}).get("description", "").lower()

        notifying = (raw.get("notifyingCountry") or {}).get("organizationName")
        origin_countries = [
            c.get("organizationName", "") for c in (raw.get("originCountries") or [])
            if c and c.get("organizationName")
        ]
        origin_country = origin_countries[0] if origin_countries else notifying

        pub_date = _parse_rasff_date(raw.get("ecValidationDate"))

        # Severity: risk decision takes priority over classification
        severity = _RISK_SEVERITY.get(risk_desc) or _CLASSIFICATION_SEVERITY.get(classification)

        return {
            "id": f"rasff::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(origin_country, subject),
            "record_url": (
                f"https://webgate.ec.europa.eu/rasff-window/screen/notification/{notif_id}"
                if notif_id else None
            ),
            "ingestion_date": datetime.utcnow().isoformat(timespec="seconds"),
            "source_published_date": pub_date,
            "event_initiation_date": pub_date,
            "event_status": None,
            "origin_country": origin_country,
            "distribution_countries": json.dumps(
                list({notifying} | set(origin_countries)) if notifying else origin_countries
            ),
            "israel_relevance_flag": 1 if (
                "israel" in subject.lower() or raw.get("_israel_operator")
            ) else 0,
            "recalling_firm": None,
            "brand_names": json.dumps([]),
            "product_description": subject or None,
            "product_category": product_cat,
            "hazard_category": _infer_hazard_category(subject),
            "hazard_specific": _extract_hazard_specific(subject),
            "severity_raw": f"{classification}/{risk_desc}".strip("/") or None,
            "severity_normalized": severity,
            "population_at_risk": _infer_population(subject, classification),
            "illness_count_reported": None,
            "title": subject or None,
            "description": _build_description(
                subject, product_type, product_cat, classification, risk_desc
            ),
            "reason_for_recall": subject or None,
        }


# ── helpers ───────────────────────────────────────────────────────────────────

def _check_israel_organisation(session, notif_id) -> bool:
    """True if Israel (country code IL) appears anywhere in the notification's
    Organisations list — as Notifying, Origin, Distribution, or Operator —
    regardless of whether it's additionally flagged for follow-up/attention.
    See module docstring for why this catches cases the subject-text check
    misses. Fails soft (returns False) on any request/parsing error — this
    is a best-effort enrichment on top of the subject-text check, not the
    sole source of truth."""
    try:
        r = session.get(DETAIL_URL_TPL.format(notif_id=notif_id), timeout=TIMEOUT)
        r.raise_for_status()
        orgs = r.json().get("organizationFlags", [])
        return any((o.get("organization") or {}).get("code") == "IL" for o in orgs)
    except Exception:
        return False


def _parse_rasff_date(s: str | None) -> str | None:
    """Convert 'DD-MM-YYYY HH:MM:SS' → 'YYYY-MM-DD'."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%d-%m-%Y").date().isoformat()
    except ValueError:
        return s[:10] if len(s) >= 10 else None


def _make_fingerprint(country: str | None, subject: str | None) -> str:
    text = " ".join([
        (country or "").lower(),
        re.sub(r"[^a-z0-9\s]", " ", (subject or "").lower())[:120],
        "eu",
    ])
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()


_BIOLOGICAL_KW = [
    "listeria monocytogenes", "clostridium botulinum", "escherichia coli",
    "salmonella", "listeria", "l. monocytogenes", "e. coli", "e.coli",
    "campylobacter", "norovirus", "clostridium", "staphylococcus",
    "enterobacter", "cronobacter", "vibrio", "hepatitis", "pathogen",
    "bacterial", "mould", "mold", "mycotoxin", "aflatoxin", "ochratoxin",
    "zearalenone", "deoxynivalenol", "patulin", "insect", "moth", "larvae",
    "maggot", "weevil", "stec", "microbial", "ergot", "alternaria", "bacteria",
    "bacillus", "temperature abuse", "insanitary conditions",
    "unsanitary conditions", "contracaecum", "contracoecum", "s.infantis",
    "s. infantis", "inflammatory lesions", "toxin", "toxins",
]
_CHEMICAL_KW = [
    "pesticide", "lead", "cadmium", "mercury", "arsenic", "chromium",
    "ethylene oxide", "mineral oil", "moah", "mosh", "dioxin", "pcb", "nitrate",
    "nitrite", "sudan", "melamine", "residue", "contaminant", "chemical",
    "additive", "colourant", "colorant", "preservative", "acrylamide",
    "bisphenol", "dehp", "dbp", "migration", "coumarin", "alkaloid",
    "hydrocyanic", "mrl", "maximum residue", "maximum permitted level",
    "unauthorised substance", "unauthorized substance",
    # Specific pesticide/veterinary-drug/unauthorized-ingredient names seen
    # in real RASFF subject lines — these never say the word "pesticide" or
    # "chemical" at all, e.g. "Chlorpyrifos in turmeric from India". RASFF's
    # own detail API confirms these carry hazardCategory "pesticide residues"
    # (see rasff.py module docstring), but that field needs a second API call
    # per record; matching the compound name directly in the subject text we
    # already ingest is far cheaper and catches the same cases.
    "chlorpyrifos", "acetamiprid", "dimethoate", "deltamethrin", "methoxychlor",
    "thiamethoxam", "oxamyl", "avermectin", "spirotetramat", "propiconazole",
    "flusilazole", "clothianidin", "dexamethasone", "sildenafil", "monacoline",
    "dmaa", "yohimbine", "yohimbe", "histamine", "nickel", "nitrofuran", "pfos",
    "pfoa", "benzo(a)pyrene", "sibutramin", "equipalazone", "phenylbutazone",
    "delta-9-thc", "thc", "cbd", "chlorothalonil", "chlorate", "narasin",
    "phenthoate", "phenthoat", "hexaconazole", "penconazole", "metronidazole",
    "glycidol", "glycidyl", "sorbic acid", "imazethapyr", "imazetapyr",
    "fenvalerate", "cypermethrin", "procymidone", "fenthion", "monocrotophos",
    "trichloroanisole",
    "trichloranisol",
]
_ALLERGEN_KW = [
    "allergen", "allergy", "allergic", "undeclared", "does not declare",
    "gluten", "casein", "lactose",
    "sulphite", "sulphur dioxide", "sulfite",
]
# These food names only indicate allergen when paired with allergy context words
_ALLERGEN_FOOD_KW = ["peanut", "tree nut", "soya", "soy", "sesame", "mustard", "lupin", "shellfish"]
_PHYSICAL_KW = ["metal", "glass", "plastic", "fragment", "foreign body", "foreign object"]
# RASFF's own hazard-substance data model doesn't cover these at all (its
# `hazards` array is genuinely empty for e.g. "unauthorised novel food
# ingredient" or "missing import controls" — verified against the live API,
# not guessed) — no structured field to lean on, so these stay keyword-based.
_LABELING_KW = [
    "labelling error", "labeling error", "incorrect label", "incorrect use-by",
    "incorrect best-before", "missing label", "mislabel", "wrong label",
    "not declared on the label", "absence of health certificate",
    "missing health certificate", "incorrect use by date",
    "improper official certificate", "improper certificate",
    "mismatching identification code", "no registration number",
    "failure to list", "incorrectly labelled", "incorrectly labeled",
    "incorrect information",
]
_NONCOMPLIANCE_KW = [
    "unauthorised", "unauthorized", "not authorised", "not authorized",
    "novel food", "missing import controls", "skipped veterinary controls",
    "breaking the cold chain", "cold chain", "non-compliant", "noncompliant",
    "unauthorised gmo", "unauthorized gmo", "without authorisation",
    "without authorization", "not subjected to official control",
    "not subjected to an official control", "illegal import",
]


def _infer_hazard_category(subject: str) -> str | None:
    text = (subject or "").lower()
    # Allergen: explicit allergen word, OR food ingredient + allergy context
    allergen_context = any(kw in text for kw in _ALLERGEN_KW)
    if allergen_context:
        return "allergen"
    if any(kw in text for kw in _ALLERGEN_FOOD_KW) and allergen_context:
        return "allergen"
    for kw in _BIOLOGICAL_KW:
        if kw in text:
            return "biological"
    for kw in _CHEMICAL_KW:
        if kw in text:
            return "chemical"
    for kw in _PHYSICAL_KW:
        if kw in text:
            return "physical"
    # Checked last: these only fire when no biological/chemical/allergen/
    # physical keyword matched, so a named hazardous substance (e.g.
    # "unauthorised substance chlorpyrifos") is still classified as the
    # more actionable "chemical" rather than the generic "regulatory" bucket.
    # Names match generate_dashboard.py's HAZARD_COLORS, which already had
    # "fraud"/"regulatory" entries defined but unused by any collector.
    for kw in _LABELING_KW:
        if kw in text:
            return "fraud"
    for kw in _NONCOMPLIANCE_KW:
        if kw in text:
            return "regulatory"
    return None


def _extract_hazard_specific(subject: str) -> str | None:
    """The specific keyword that drove _infer_hazard_category's decision —
    matched from the exact same lists (in the same priority order: allergen
    context, then biological, then chemical) rather than a separately
    maintained candidate list. That third list used to disagree with
    _infer_hazard_category for real records — e.g. this function would
    return "sesame" as hazard_specific while _infer_hazard_category
    returned None for hazard_category on the same text, because "sesame"
    wasn't allergy-context-gated here the way it was there. Deriving both
    from one source makes that class of bug structurally impossible.
    """
    text = (subject or "").lower()
    allergen_context = any(kw in text for kw in _ALLERGEN_KW)
    if allergen_context:
        for kw in _ALLERGEN_KW:
            if kw in text:
                return kw
        for kw in _ALLERGEN_FOOD_KW:
            if kw in text:
                return kw
    for kw in _BIOLOGICAL_KW:
        if kw in text:
            return kw
    for kw in _CHEMICAL_KW:
        if kw in text:
            return kw
    return None


def _infer_population(subject: str, classification: str) -> str | None:
    text = (subject or "").lower()
    if any(kw in text for kw in ["allergen", "allergy", "allergic", "undeclared", "casein", "gluten"]):
        return "allergic"
    if "infant" in text or "baby" in text or "children" in text:
        return "infants/children"
    return None


def _build_description(
    subject: str,
    product_type: str,
    product_cat: str | None,
    classification: str,
    risk_desc: str,
) -> str | None:
    parts = [subject]
    if product_type and product_type != "food":
        parts.append(f"Type: {product_type}")
    if classification:
        parts.append(f"Classification: {classification}")
    if risk_desc:
        parts.append(f"Risk: {risk_desc}")
    return " | ".join(p for p in parts if p) or None
