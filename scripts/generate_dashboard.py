"""Generate a self-contained HTML dashboard for the Israeli Ministry of Health.

All data is embedded as `const DATA = {...}` inside the HTML — no server required.
Chart.js is loaded from CDN; if offline, replace with a local copy.

Usage:
  python3 scripts/generate_dashboard.py
  python3 scripts/generate_dashboard.py --window 30
  python3 scripts/generate_dashboard.py --window 90 --out reports/dashboard_90d
  python3 scripts/generate_dashboard.py --counterfactuals reports/counterfactuals.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db

# ── thresholds (mirror rank_daily.py) ────────────────────────────────────────
CRITICAL_THRESHOLD = 0.95
HIGH_THRESHOLD     = 0.80
MEDIUM_THRESHOLD   = 0.50

# ── absolute score normalization ──────────────────────────────────────────────
# Empirical min/max observed across all 23,670 scored alerts.
# Fixed constants so the 1-10 scale is stable across runs and time periods.
SCORE_MIN = -5.65
SCORE_MAX =  6.71


def _absolute_score(raw: float) -> float:
    """Normalize bi_encoder_score to 1–10 scale using fixed empirical bounds."""
    s = 1 + 9 * (raw - SCORE_MIN) / (SCORE_MAX - SCORE_MIN)
    return round(max(1.0, min(10.0, s)), 1)

HAZARD_COLORS = {
    "biological":   "#c0392b",
    "chemical":     "#8e44ad",
    "allergen":     "#e67e22",
    "physical":     "#2980b9",
    "fraud":        "#16a085",
    "regulatory":   "#7f8c8d",
    "unclassified": "#bdc3c7",
}

SOURCE_LABELS = {
    "fda_enforcement": "FDA Enforcement (USA)",
    "fsis":            "USDA FSIS (USA)",
    "fsa_uk":          "FSA UK (United Kingdom)",
    "rasff":           "RASFF (European Union)",
}

SOURCE_REGION = {
    "fda_enforcement": "🇺🇸 USA",
    "fsis":            "🇺🇸 USA",
    "fsa_uk":          "🇬🇧 UK",
    "rasff":           "🇪🇺 EU",
}

SOURCE_COLORS = {
    "fda_enforcement": "#003087",
    "fsis":            "#007749",
    "fsa_uk":          "#9c1a1c",
    "rasff":           "#f0b400",
}

PERT_LABELS = {
    "hazard→Listeria":    "If hazard were Listeria monocytogenes",
    "hazard→mineral_oil": "If hazard were mineral oil (chronic chemical)",
    "hazard→allergen":    "If hazard were undeclared allergen",
    "severity→ClassI":    "If severity were Class I (highest)",
    "severity→ClassIII":  "If severity were Class III (lowest)",
    "dist→nationwide":    "If distributed nationwide (US)",
    "dist→local_only":    "If distribution were local only",
    "dist→+Israel":       "If Israel were added to distribution",
    "rte→add_RTE":        "If product were ready-to-eat",
    "rte→cooked":         "If product required thorough cooking",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _rank_within_window(alerts: list[dict]) -> list[dict]:
    n = len(alerts)
    if n == 0:
        return alerts
    order = sorted(range(n), key=lambda i: alerts[i]["bi_encoder_score"])
    for rank, idx in enumerate(order):
        alerts[idx]["window_percentile"] = rank / max(n - 1, 1)
    return alerts


def _tier(pct: float) -> str:
    if pct >= CRITICAL_THRESHOLD: return "critical"
    if pct >= HIGH_THRESHOLD:     return "high"
    if pct >= MEDIUM_THRESHOLD:   return "medium"
    return "low"


def _dist_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw) if raw.strip().startswith("[") else [raw]
        return [str(x) for x in val]
    except Exception:
        return [raw[:120]]


def _israel_reason(alert: dict) -> str:
    origin = (alert.get("origin_country") or "").lower()
    dist   = (alert.get("distribution_countries") or "").lower()
    title  = (alert.get("title") or "").lower()
    desc   = (alert.get("description") or "").lower()
    if "israel" in origin:
        return f"Product originates from Israel ({alert.get('origin_country')})"
    if "israel" in dist:
        return "Israel listed in distribution countries"
    if "israel" in title or "israel" in desc:
        return "Israel mentioned in alert text"
    return "Israel relevance detected"


_PATHOGEN_RE = re.compile(
    r'\b(listeria\s*(?:monocytogenes)?|salmonella(?:\s+\w+)?|e\.?\s*coli(?:\s+O\d+[:\-]H\d+)?'
    r'|clostridium(?:\s+(?:botulinum|perfringens))?|botulinum|campylobacter'
    r'|hepatitis\s+[AE]|cronobacter|staphylococcus(?:\s+aureus)?|vibrio(?:\s+\w+)?'
    r'|brucella|yersinia(?:\s+\w+)?|norovirus|norwalk|cryptosporidium|cyclospora'
    r'|bacillus\s+cereus|shigella)\b',
    re.IGNORECASE,
)
_ALLERGEN_RE = re.compile(
    r'\bundeclared\s+(?:allergen[s]?[:\-\s]*)?'
    r'(milk|dairy|egg[s]?|peanut[s]?|tree\s+nut[s]?|almond[s]?|walnut[s]?|cashew[s]?|pecan[s]?'
    r'|hazelnut[s]?|pistachio[s]?|wheat|gluten|soy(?:bean)?[s]?|sesame|fish|anchov\w+'
    r'|shellfish|shrimp|lobster|crab|mustard|sulfite[s]?|lupin|celery)\b'
    r'|(?:allergen[s]?)[:\s]+(milk|dairy|egg[s]?|peanut[s]?|tree\s+nut[s]?|almond[s]?'
    r'|wheat|gluten|soy(?:bean)?[s]?|sesame|fish|shellfish)',
    re.IGNORECASE,
)
_CHEMICAL_RE = re.compile(
    r'\b(lead|mercury|cadmium|arsenic|aflatoxin[s]?|mycotoxin[s]?|ochratoxin|patulin|zearalenone'
    r'|pesticide[s]?|dioxin[s]?|PCB[s]?|nitrite[s]?|nitrate[s]?|malachite\s+green'
    r'|acetamiprid|chlorpyrifos|imidacloprid|glyphosate|thiamethoxam|cypermethrin'
    r'|chlorate[s]?|perchlorate[s]?|erucic\s+acid|mineral\s+oil|MOSH|MOAH'
    r'|elevated\s+levels?\s+of\s+\w+|histamine|tyramine)\b',
    re.IGNORECASE,
)
_PHYSICAL_RE = re.compile(
    r'\b(foreign\s+(?:material|object|matter|plastic|bodies|body)'
    r'|metal\s+(?:piece[s]?|fragment[s]?|shav\w+|contamin\w+)'
    r'|glass(?:\s+(?:piece[s]?|fragment[s]?|shard[s]?|varying\w*|object[s]?))?'
    r'|plastic\s+(?:piece[s]?|fragment[s]?)'
    r'|bone\s+fragment[s]?|hard\s+(?:plastic|object)|sharp\s+object[s]?)\b',
    re.IGNORECASE,
)
_CONTAMINATION_RE = re.compile(
    r'contaminated?\s+with\s+([A-Za-z][A-Za-z0-9 .]+?)(?:\.|,|$)'
    r'|contamination\s+(?:with|of)\s+([A-Za-z][A-Za-z0-9 .]+?)(?:\.|,|$)'
    r'|presence\s+of\s+([A-Za-z][A-Za-z0-9 .]+?)(?:\s+in|\.|,|$)'
    r'|positive\s+(?:for\s+)?([A-Za-z][A-Za-z0-9 .]+?)(?:\.|,|\s+contamination|$)',
    re.IGNORECASE,
)
# RASFF title pattern: "[Hazard] in [product] from [country]"
_RASFF_TITLE_RE = re.compile(
    r'^([A-Za-z][A-Za-z0-9 /,\(\)\-\.]{3,60}?)\s+in\s+\w',
    re.IGNORECASE,
)
# Temperature/biological control issues
_TEMP_CONTROL_RE = re.compile(
    r'\b(temperature\s+(?:abuse|control|misuse)|under\s*-?\s*pasteur\w+|improper\s+(?:storage|handling)'
    r'|cold\s+chain|cadena\s+de\s+fr[íi]o|rotura\s+de\s+cadena)\b',
    re.IGNORECASE,
)


def _extract_hazard_from_text(text: str) -> tuple[str, str]:
    """Return (hazard_specific, hazard_category) extracted from free text, or ('', '')."""
    if not text:
        return "", ""
    t = text.strip()

    # Pathogens first (highest specificity)
    m = _PATHOGEN_RE.search(t)
    if m:
        return m.group(0).title(), "biological"

    # Generic contamination phrase — extract what follows
    m = _CONTAMINATION_RE.search(t)
    if m:
        extracted = next((g for g in m.groups() if g), None)
        if not extracted:
            return "", ""
        extracted = extracted.strip().rstrip(".")
        if extracted and len(extracted) < 60:
            sub_path = _PATHOGEN_RE.search(extracted)
            if sub_path:
                return sub_path.group(0).title(), "biological"
            sub_chem = _CHEMICAL_RE.search(extracted)
            if sub_chem:
                return extracted.title(), "chemical"
            return extracted.title(), "biological"

    # Allergens
    m = _ALLERGEN_RE.search(t)
    if m:
        allergen = (m.group(1) or m.group(2) or "").title()
        return f"Undeclared {allergen}", "allergen"

    # Simple undeclared pattern (catches "Undeclared Allergen - Soy Flour" etc.)
    m = re.search(r'undeclared\s+([A-Za-z][A-Za-z0-9 \-]+?)(?:\.|,|$)', t, re.IGNORECASE)
    if m:
        return f"Undeclared {m.group(1).strip().title()}", "allergen"

    # Chemical / heavy metal
    m = _CHEMICAL_RE.search(t)
    if m:
        return m.group(0).title(), "chemical"

    # Physical
    m = _PHYSICAL_RE.search(t)
    if m:
        return m.group(0).title(), "physical"

    # Temperature / cold chain issues → biological risk
    m = _TEMP_CONTROL_RE.search(t)
    if m:
        return "Temperature control issue", "biological"

    # RASFF-style "[Hazard] in [product] from [country]" — extract leading phrase
    m = _RASFF_TITLE_RE.match(t)
    if m:
        candidate = m.group(1).strip().rstrip(".,")
        if len(candidate) > 3:
            sub_path = _PATHOGEN_RE.search(candidate)
            if sub_path:
                return sub_path.group(0).title(), "biological"
            sub_chem = _CHEMICAL_RE.search(candidate)
            if sub_chem:
                return candidate.title(), "chemical"
            sub_alg = _ALLERGEN_RE.search(candidate)
            if sub_alg:
                return candidate.title(), "allergen"
            # Generic chemical-sounding compound (ends in -ide, -ate, -in, -ine)
            if re.search(r'\b\w+(?:ide|ates?|mycin|toxin|amine|chlor\w+)\b', candidate, re.I):
                return candidate.title(), "chemical"

    return "", ""


def _explain_score(a: dict) -> str:
    """One-line rule-based explanation of the key risk factors behind the score."""
    parts = []

    # Severity
    sev = (a.get("severity_raw") or "").strip()
    sev_l = sev.lower()
    # FDA classification (Class I / II / III)
    if "class i" in sev_l and "class ii" not in sev_l and "class iii" not in sev_l:
        parts.append("Class I recall — FDA's highest severity")
    elif "class iii" in sev_l:
        parts.append("Class III recall — FDA's lowest severity (low health risk)")
    elif "class ii" in sev_l:
        parts.append("Class II recall — moderate health risk")
    # RASFF notification types: "{type} notification/{risk_level}"
    elif "alert notification" in sev_l:
        risk = sev.split("/")[-1].strip().title() if "/" in sev else ""
        parts.append(f"RASFF Alert notification ({risk})" if risk else "RASFF Alert notification (highest tier)")
    elif "border rejection" in sev_l:
        risk = sev.split("/")[-1].strip().title() if "/" in sev else ""
        parts.append(f"EU border rejection ({risk})" if risk else "EU border rejection")
    elif "information notification for attention" in sev_l:
        risk = sev.split("/")[-1].strip().title() if "/" in sev else ""
        parts.append(f"RASFF Information notification ({risk})" if risk else "RASFF Information notification")
    elif "information notification for follow-up" in sev_l:
        risk = sev.split("/")[-1].strip().title() if "/" in sev else ""
        parts.append(f"RASFF Follow-up notification ({risk})" if risk else "RASFF Follow-up notification")
    elif "information notification" in sev_l:
        risk = sev.split("/")[-1].strip().title() if "/" in sev else ""
        parts.append(f"RASFF Information notification ({risk})" if risk else "RASFF Information notification")
    elif sev:
        parts.append(sev)

    # Hazard
    hz = (a.get("hazard_specific") or a.get("hazard_category") or "").strip()
    hz_l = hz.lower()
    HIGH_PATH = ("listeria", "clostridium", "botulinum", "hepatitis", "e. coli",
                 "escherichia", "campylobacter", "brucella", "salmonella typhi")
    if any(x in hz_l for x in HIGH_PATH):
        parts.append(f"{hz} (high-risk pathogen)")
    elif "salmonella" in hz_l:
        parts.append(f"Salmonella contamination")
    elif "allergen" in hz_l or "undeclared" in hz_l:
        parts.append(f"Undeclared allergen ({hz})")
    elif any(x in hz_l for x in ("pesticide", "mycotoxin", "aflatoxin", "heavy metal",
                                  "lead", "mercury", "cadmium", "dioxin")):
        parts.append(f"Chemical hazard ({hz})")
    elif any(x in hz_l for x in ("foreign", "physical", "glass", "metal fragment")):
        parts.append(f"Physical contamination ({hz})")
    elif hz and hz_l not in ("unclassified", "other", "unknown", ""):
        parts.append(hz)

    # Distribution scope
    dist = a.get("distribution_countries") or ""
    if isinstance(dist, list):
        dist_str = " ".join(dist).lower()
        n = len(dist)
    else:
        dist_str = dist.lower()
        n = 0
    if "nationwide" in dist_str:
        parts.append("nationwide distribution")
    elif n >= 15 or len(dist_str) > 400:
        parts.append("wide multi-country distribution")
    elif n >= 5:
        parts.append(f"distribution across {n} countries/regions")

    # Population at risk
    pop = (a.get("population_at_risk") or "").strip()
    if pop:
        parts.append(f"affects {pop}")

    # Illnesses
    ill = a.get("illness_count_reported")
    if ill and str(ill).strip() not in ("", "0", "None"):
        parts.append(f"{ill} illness(es) reported")

    if not parts:
        return ""
    return " · ".join(parts)


def _clean_alert(a: dict) -> dict:
    dist = _dist_list(a.get("distribution_countries"))

    # Fill missing hazard fields from free text when structured fields are absent
    hz_specific = a.get("hazard_specific") or ""
    hz_category = a.get("hazard_category") or ""
    if not hz_specific:
        text = a.get("description") or a.get("title") or a.get("product_description") or ""
        extracted_hz, extracted_cat = _extract_hazard_from_text(text)
        if extracted_hz:
            hz_specific = extracted_hz
        if extracted_cat and not hz_category:
            hz_category = extracted_cat

    return {
        "alert_id":            a["alert_id"],
        "source_id":           a["source_id"],
        "source_label":        SOURCE_LABELS.get(a["source_id"], a["source_id"]),
        "region":              SOURCE_REGION.get(a["source_id"], ""),
        "source_published_date": a.get("source_published_date") or "",
        "title":               (a.get("title") or a.get("product_description") or "")[:120],
        "hazard_specific":     hz_specific,
        "hazard_category":     hz_category or "unclassified",
        "severity_raw":        a.get("severity_raw") or "",
        "severity_normalized": a.get("severity_normalized") or "",
        "distribution_countries": dist,
        "origin_country":      a.get("origin_country") or "",
        "recalling_firm":      a.get("recalling_firm") or "",
        "product_category":    a.get("product_category") or "",
        "population_at_risk":  a.get("population_at_risk") or "",
        "illness_count":       a.get("illness_count_reported"),
        "description":         (a.get("description") or "")[:500],
        "record_url":          a.get("record_url") or "",
        "event_initiation_date": a.get("event_initiation_date") or "",
        "bi_encoder_score":    round(float(a["bi_encoder_score"]), 4),
        "global_percentile":   round(float(a.get("bi_encoder_percentile") or 0), 4),
        "absolute_score":      _absolute_score(float(a["bi_encoder_score"])),
        "window_percentile":   round(float(a.get("window_percentile", 0)), 4),
        "tier":                a.get("tier", "low"),
        "israel_relevance_flag": int(a.get("israel_relevance_flag") or 0),
        "israel_reason":       _israel_reason(a) if a.get("israel_relevance_flag") else "",
        "score_explanation":   _explain_score({
            **a,
            "hazard_specific":        hz_specific,
            "hazard_category":        hz_category,
            "distribution_countries": dist,  # already a list
            "illness_count_reported": a.get("illness_count_reported"),
        }),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window",          type=int,  default=30)
    ap.add_argument("--date",            default=None)
    ap.add_argument("--out",             type=Path, default=None)
    ap.add_argument("--counterfactuals", type=Path, default=Path("reports/counterfactuals.json"))
    args = ap.parse_args()

    ref_date          = date.fromisoformat(args.date) if args.date else date.today()
    window_start      = ref_date - timedelta(days=args.window)
    thirteen_mo_ago   = ref_date - timedelta(days=395)

    conn = db.connect()

    # ── Query A: window alerts ─────────────────────────────────────────────
    rows = conn.execute("""
        SELECT s.alert_id, s.bi_encoder_score, s.bi_encoder_percentile,
               a.source_id, a.source_published_date, a.title, a.product_description,
               a.hazard_specific, a.hazard_category, a.severity_raw, a.severity_normalized,
               a.distribution_countries, a.recalling_firm, a.origin_country,
               a.israel_relevance_flag, a.product_category, a.population_at_risk,
               a.illness_count_reported, a.record_url, a.description,
               a.event_initiation_date
        FROM alert_scores s JOIN alerts a ON a.id = s.alert_id
        WHERE a.source_published_date BETWEEN ? AND ?
        ORDER BY s.bi_encoder_score DESC
    """, (window_start.isoformat(), ref_date.isoformat())).fetchall()

    alerts_raw = [dict(r) for r in rows]
    print(f"Window {window_start} – {ref_date}: {len(alerts_raw)} alerts")

    _rank_within_window(alerts_raw)
    for a in alerts_raw:
        a["tier"] = _tier(a["window_percentile"])

    n_critical = sum(1 for a in alerts_raw if a["tier"] == "critical")
    n_high     = sum(1 for a in alerts_raw if a["tier"] == "high")
    n_medium   = sum(1 for a in alerts_raw if a["tier"] == "medium")
    israel_raw = [a for a in alerts_raw if a.get("israel_relevance_flag")]
    n_israel   = len(israel_raw)
    print(f"  Critical:{n_critical}  High:{n_high}  Medium:{n_medium}  Israel:{n_israel}")

    # medium+ for the visual feed; low tier embedded separately (slim fields, CSV-only)
    feed_alerts = [_clean_alert(a) for a in alerts_raw if a["tier"] != "low"]
    CSV_FIELDS = ("alert_id","source_published_date","tier","absolute_score","title",
                  "source_id","hazard_specific","hazard_category","severity_normalized",
                  "origin_country","distribution_countries","israel_relevance_flag","record_url")
    low_alerts = [
        {k: _clean_alert(a).get(k) for k in CSV_FIELDS}
        for a in alerts_raw if a["tier"] == "low"
    ]
    israel_alerts = [_clean_alert(a) for a in israel_raw]

    # ── Query B: Israel fallback ───────────────────────────────────────────
    israel_fallback = []
    if not israel_alerts:
        fb_rows = conn.execute("""
            SELECT a.id as alert_id, a.source_id, a.source_published_date,
                   a.title, a.product_description, a.hazard_specific, a.hazard_category,
                   a.severity_raw, a.severity_normalized, a.distribution_countries,
                   a.origin_country, a.recalling_firm, a.product_category,
                   a.population_at_risk, a.illness_count_reported, a.record_url,
                   a.description, a.israel_relevance_flag,
                   s.bi_encoder_score, s.bi_encoder_percentile
            FROM alerts a JOIN alert_scores s ON s.alert_id = a.id
            WHERE a.israel_relevance_flag = 1
            ORDER BY a.source_published_date DESC LIMIT 10
        """).fetchall()
        for r in fb_rows:
            d = dict(r)
            d["window_percentile"] = d.get("bi_encoder_percentile") or 0
            d["tier"] = "low"
            israel_fallback.append(_clean_alert(d))

    # ── Query C: 13-month trends ───────────────────────────────────────────
    trend_rows = conn.execute("""
        SELECT strftime('%Y-%m', a.source_published_date) AS month,
               COALESCE(a.hazard_category, 'unclassified') AS cat,
               COUNT(*) AS cnt
        FROM alerts a JOIN alert_scores s ON s.alert_id = a.id
        WHERE a.source_published_date BETWEEN ? AND ?
        GROUP BY month, cat ORDER BY month, cat
    """, (thirteen_mo_ago.isoformat(), ref_date.isoformat())).fetchall()

    # Build chart-ready structure
    months_set = sorted({r["month"] for r in trend_rows})
    cats_set   = sorted({r["cat"] for r in trend_rows})
    trend_map  = defaultdict(lambda: defaultdict(int))
    for r in trend_rows:
        trend_map[r["month"]][r["cat"]] = r["cnt"]
    trends = {
        "labels": months_set,
        "datasets": [
            {
                "label": cat,
                "data":  [trend_map[m][cat] for m in months_set],
                "color": HAZARD_COLORS.get(cat, "#95a5a6"),
            }
            for cat in cats_set
        ],
    }

    # ── Query C2: 13-month trends by product_category (top 10 cats only) ────
    prod_trend_rows = conn.execute("""
        SELECT strftime('%Y-%m', a.source_published_date) AS month,
               COALESCE(a.product_category, 'unclassified') AS cat,
               COUNT(*) AS cnt
        FROM alerts a JOIN alert_scores s ON s.alert_id = a.id
        WHERE a.source_published_date BETWEEN ? AND ?
        GROUP BY month, cat ORDER BY month, cat
    """, (thirteen_mo_ago.isoformat(), ref_date.isoformat())).fetchall()

    # Limit to top 10 product categories by total count
    prod_cat_totals: dict = defaultdict(int)
    for r in prod_trend_rows:
        prod_cat_totals[r["cat"]] += r["cnt"]
    top10_prod_cats = sorted(prod_cat_totals, key=lambda c: -prod_cat_totals[c])[:10]

    prod_trend_map: dict = defaultdict(lambda: defaultdict(int))
    for r in prod_trend_rows:
        prod_trend_map[r["month"]][r["cat"]] = r["cnt"]

    PRODUCT_COLORS = [
        "#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6",
        "#1abc9c","#e67e22","#34495e","#e91e63","#00bcd4",
    ]
    trends_product = {
        "labels": months_set,
        "datasets": [
            {
                "label": cat,
                "data":  [prod_trend_map[m][cat] for m in months_set],
                "color": PRODUCT_COLORS[i % len(PRODUCT_COLORS)],
            }
            for i, cat in enumerate(top10_prod_cats)
        ],
    }

    # ── Queries D1–D4: breakdowns ──────────────────────────────────────────
    def _breakdown(col: str, limit: int = 20) -> dict:
        rows2 = conn.execute(f"""
            SELECT COALESCE({col}, 'unknown') AS lbl, COUNT(*) AS cnt
            FROM alerts
            WHERE source_published_date BETWEEN ? AND ?
            GROUP BY lbl ORDER BY cnt DESC LIMIT ?
        """, (window_start.isoformat(), ref_date.isoformat(), limit)).fetchall()
        return {"labels": [r["lbl"] for r in rows2], "data": [r["cnt"] for r in rows2]}

    def _hazard_breakdown(limit: int = 20) -> dict:
        """Compute hazard_category breakdown using text extraction for alerts with NULL category."""
        rows2 = conn.execute("""
            SELECT hazard_category, description, title, product_description
            FROM alerts
            WHERE source_published_date BETWEEN ? AND ?
        """, (window_start.isoformat(), ref_date.isoformat())).fetchall()
        counts: dict[str, int] = {}
        for r in rows2:
            cat = (r["hazard_category"] or "").strip().lower()
            if not cat:
                text = r["description"] or r["title"] or r["product_description"] or ""
                _, cat = _extract_hazard_from_text(text)
            cat = cat or "unknown"
            counts[cat] = counts.get(cat, 0) + 1
        sorted_cats = sorted(counts.items(), key=lambda x: -x[1])[:limit]
        return {"labels": [c for c, _ in sorted_cats], "data": [n for _, n in sorted_cats]}

    breakdowns = {
        "hazard_category":  _hazard_breakdown(),
        "product_category": _breakdown("product_category", 10),
        "origin_country":   _breakdown("origin_country",   10),
        "source": {
            "labels": list(SOURCE_LABELS.values()),
            "data": [
                conn.execute(
                    "SELECT COUNT(*) FROM alerts WHERE source_id=? AND source_published_date BETWEEN ? AND ?",
                    (sid, window_start.isoformat(), ref_date.isoformat())
                ).fetchone()[0]
                for sid in SOURCE_LABELS
            ],
            "colors": list(SOURCE_COLORS.values()),
        },
    }

    conn.close()

    # ── Load counterfactuals ───────────────────────────────────────────────
    cf_map: dict = {}
    cf_path = args.counterfactuals
    if cf_path and Path(cf_path).exists():
        with open(cf_path, encoding="utf-8") as f:
            cf_data = json.load(f)
        EXCLUDED_PERTS = {"rte→add_RTE", "rte→cooked", "dist→nationwide", "dist→+Israel"}
        NORM_RANGE = SCORE_MAX - SCORE_MIN  # 12.36

        def _to_abs(raw: float) -> float:
            return round(max(1.0, min(10.0, 1 + 9 * (raw - SCORE_MIN) / NORM_RANGE)), 1)

        for analysis in cf_data.get("analyses", []):
            aid = analysis["alert_id"]
            perts = analysis.get("perturbations", [])
            orig_raw = analysis["original_score"]
            orig_abs = _to_abs(orig_raw)
            reliable = [p for p in perts if p["perturbation"] not in EXCLUDED_PERTS]
            top3  = sorted(reliable, key=lambda p: abs(p["delta"]), reverse=True)[:3]

            # Build per-perturbation lookup for badge tooltips (all reliable, not just top3)
            badge_map = {}
            for p in reliable:
                after_abs = _to_abs(p["perturbed_score"])
                delta_abs  = round(after_abs - orig_abs, 1)
                badge_map[p["perturbation"]] = {
                    "label":      PERT_LABELS.get(p["perturbation"], p["perturbation"]),
                    "delta_abs":  delta_abs,
                    "after_abs":  after_abs,
                }

            cf_map[aid] = {
                "original_score": orig_raw,
                "original_abs":   orig_abs,
                "top_driver":     analysis.get("top_driver", ""),
                "top3": [
                    {
                        "perturbation": p["perturbation"],
                        "label":        PERT_LABELS.get(p["perturbation"], p["perturbation"]),
                        "delta":        round(p["delta"], 4),
                        "delta_abs":    round(_to_abs(p["perturbed_score"]) - orig_abs, 1),
                        "after_abs":    _to_abs(p["perturbed_score"]),
                        "direction":    p["direction"],
                    }
                    for p in top3
                ],
                "badges": badge_map,
            }
        print(f"  Counterfactuals loaded: {len(cf_map)} alerts")
    else:
        print("  No counterfactuals file — CF blocks will be hidden")

    # ── Assemble DATA dict ─────────────────────────────────────────────────
    DATA = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ref_date":     ref_date.isoformat(),
            "window_days":  args.window,
            "window_start": window_start.isoformat(),
            "n_in_window":  len(alerts_raw),
            "n_critical":   n_critical,
            "n_high":       n_high,
            "n_medium":     n_medium,
            "n_israel":     n_israel,
        },
        "israel_alerts":   israel_alerts,
        "israel_fallback": israel_fallback,
        "alerts":          feed_alerts,
        "low_alerts":      low_alerts,
        "counterfactuals": cf_map,
        "trends":          trends,
        "trends_product":  trends_product,
        "breakdowns":      breakdowns,
        "pert_labels":     PERT_LABELS,
    }

    data_json = json.dumps(DATA, ensure_ascii=False, separators=(",", ":"))

    # ── Render HTML ────────────────────────────────────────────────────────
    html = _HTML_TEMPLATE.replace("__DATA_PLACEHOLDER__", data_json)

    out_prefix = args.out or Path(f"reports/dashboard_{ref_date.isoformat()}")
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(str(out_prefix) + ".html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved {out_path}  ({out_path.stat().st_size // 1024}KB)")


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FoodAlert — Risk Monitoring System</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/twemoji@14.0.2/dist/twemoji.min.js" crossorigin="anonymous"></script>
<style>
:root {
  --critical:#c0392b; --high:#d35400; --medium:#d4ac0d; --low:#7f8c8d;
  --israel:#1a5276;   --bg:#f5f7fa;   --card:#ffffff;   --text:#1a1f2e;
  --muted:#5a6478;    --border:#dde3ed; --radius:6px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
a{color:var(--israel);text-decoration:none} a:hover{text-decoration:underline}

/* ── Header ── */
.header{background:#1a1f2e;color:#fff;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.4)}
.header-left{display:flex;align-items:center;gap:14px}
.header-logos{display:flex;align-items:center;gap:10px}
.header-logos img{height:44px;width:auto;background:#fff;border-radius:6px;padding:4px 8px;}
.header h1{font-size:26px;font-weight:800;letter-spacing:.3px}
.header .meta{font-size:13px;color:#aab4c8;text-align:right;white-space:nowrap}
/* ── Overview donut ── */
.overview-row{display:flex;gap:16px;margin-bottom:24px;align-items:stretch}
.overview-wrap{display:flex;align-items:center;gap:20px;background:var(--card);border-radius:var(--radius);padding:16px 20px;box-shadow:var(--shadow);flex-shrink:0}
.overview-donut-wrap{position:relative;width:225px;height:225px;flex-shrink:0}
.overview-donut-center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;pointer-events:none}
.overview-donut-center .dn{font-size:22px;font-weight:800;color:var(--text);line-height:1}
.overview-donut-center .dl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:2px}
.overview-stats{display:flex;flex-direction:column;gap:3px}
.ov-stat{display:flex;align-items:center;gap:8px;cursor:pointer;padding:5px 8px;border-radius:6px;transition:background .15s;user-select:none;white-space:nowrap}
.ov-stat:hover{background:#f0f2f5}
.ov-dot{width:11px;height:11px;border-radius:50%;flex-shrink:0}
.ov-label{font-size:13px;color:var(--muted);width:64px}
.ov-val{font-size:15px;font-weight:700;color:var(--text);text-align:right;min-width:36px;font-variant-numeric:tabular-nums}
.ov-israel{border:1.5px solid var(--israel);background:#eaf2fb;margin-top:4px}
.ov-israel:hover{background:#d6eaf8}
.ov-israel .ov-label{color:var(--israel);font-weight:600}
.info-btn{font-size:14px;color:var(--muted);cursor:pointer;user-select:none;line-height:1}
.info-btn:hover{color:var(--text)}
.info-box{position:absolute;z-index:50;background:#fff;border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:12px;line-height:1.7;color:var(--text);box-shadow:0 4px 16px rgba(0,0,0,.12);max-width:280px;margin-top:4px}

/* ── Layout ── */
.container{max-width:1100px;margin:0 auto;padding:24px 16px}
.section{margin-bottom:36px}

/* ── Floating TOC ── */
#floating-toc{position:fixed;left:18px;top:50%;transform:translateY(-50%);background:var(--card);border:1px solid var(--border);border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,.10);padding:10px 0;z-index:200;min-width:150px;max-width:172px;display:none}
#floating-toc .toc-header{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);padding:0 13px 8px;border-bottom:1px solid var(--border);margin-bottom:4px}
#floating-toc ul{list-style:none;padding:0;margin:0}
#floating-toc li a{display:flex;align-items:center;gap:6px;padding:5px 13px;font-size:12px;color:var(--muted);cursor:pointer;border-left:3px solid transparent;transition:all .15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.4}
#floating-toc li a:hover{color:var(--text);background:#f5f7fa}
#floating-toc li a.toc-active{color:var(--israel);font-weight:700;border-left-color:var(--israel);background:#eaf2fb}
@media(min-width:1280px){#floating-toc{display:block}}
.section-title{font-size:17px;font-weight:700;margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid var(--border);display:flex;align-items:center;gap:8px}
.section-title .count{font-size:13px;font-weight:400;color:var(--muted);margin-left:4px}

/* ── Israel Watch ── */
.all-clear{background:#d5f5e3;border:1.5px solid #27ae60;border-radius:var(--radius);padding:18px 20px;display:flex;align-items:center;gap:14px;font-size:15px;font-weight:600;color:#1e8449}
.all-clear .icon{font-size:28px}
.all-clear .sub{font-size:13px;font-weight:400;color:#27ae60;margin-top:2px}
.israel-card{border-left:5px solid var(--israel);background:#eaf0fb;border-radius:var(--radius);margin-bottom:8px}
.fallback-note{font-size:12px;color:var(--muted);font-style:italic;margin-bottom:8px}

/* ── Alert cards ── */
details.alert-card{border-radius:var(--radius);margin-bottom:7px;overflow:hidden}
details.alert-card summary{cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
details.alert-card summary::-webkit-details-marker{display:none}
details.alert-card[open] summary{border-bottom:1px solid var(--border)}
.alert-card.critical{border-left:5px solid var(--critical);background:#fff8f8}
.alert-card.high    {border-left:5px solid var(--high);   background:#fff9f5}
.alert-card.medium  {border-left:5px solid var(--medium); background:#fffef0}
.alert-card.low     {border-left:5px solid var(--low);    background:#f8f9fa}
.alert-card.israel-feed{border-left:5px solid var(--israel);background:#eaf0fb}

.badge{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:700;white-space:nowrap}
.badge.critical{background:var(--critical);color:#fff}
.badge.high    {background:var(--high);    color:#fff}
.badge.medium  {background:var(--medium);  color:#fff}
.badge.low     {background:var(--low);     color:#fff}
.badge.israel  {background:var(--israel);  color:#fff}

.card-title{font-weight:600;font-size:14px;flex:1;min-width:0}
.card-meta{font-size:12px;color:var(--muted);white-space:nowrap}
.region-badge{font-size:11px;background:#f0f2f5;border:1px solid var(--border);border-radius:10px;padding:1px 7px;color:var(--text);white-space:nowrap}
.card-score{font-family:monospace;font-size:15px;font-weight:700;color:var(--text);background:#f0f2f5;border:1px solid var(--border);border-radius:8px;padding:2px 9px;white-space:nowrap}
.expand-hint{font-size:11px;color:var(--muted);margin-left:auto}

.card-body{padding:12px 16px;background:rgba(255,255,255,.7)}
.field-grid{display:grid;grid-template-columns:140px 1fr;gap:4px 12px;margin-bottom:10px}
.field-label{color:var(--muted);font-size:12px;font-weight:600;padding-top:1px}
.field-value{font-size:13px}
.source-link{display:inline-block;margin-top:6px;font-size:13px;font-weight:600;color:var(--israel)}

/* ── Impact badge (per-field CF delta) ── */
.impact-badge{display:inline-block;margin-left:6px;font-size:11px;font-family:monospace;
  font-weight:700;padding:1px 5px;border-radius:3px;cursor:help;vertical-align:middle}
.impact-badge.up{background:#e8f8f0;color:#1a8c4e}
.impact-badge.down{background:#fdecea;color:#c0392b}
.impact-badge.neutral{background:#f0f0f0;color:#666}

/* ── Counterfactual ── */
.cf-block{margin-top:12px;padding-top:10px;border-top:1px solid var(--border)}
.cf-title{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.cf-row{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.cf-label{font-size:12px;flex:1;min-width:0}
.cf-delta{font-family:monospace;font-size:12px;font-weight:700;white-space:nowrap;min-width:52px;text-align:right}
.cf-delta.up{color:#27ae60} .cf-delta.down{color:var(--critical)}
.cf-bar-wrap{width:100px;background:#eee;border-radius:3px;overflow:hidden;height:8px}
.cf-bar-fill{height:100%;border-radius:3px;min-width:3px}
.score-explain{margin-top:10px;padding:8px 10px;background:#f8f9fb;border-left:3px solid var(--muted);border-radius:0 4px 4px 0;font-size:12px;color:#444;line-height:1.5}
.score-explain .se-label{font-weight:700;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.5px;display:block;margin-bottom:3px}

/* ── Toggle button ── */
.toggle-btn{cursor:pointer;background:#fff;border:1.5px solid var(--border);border-radius:var(--radius);padding:8px 16px;font-size:13px;color:var(--muted);width:100%;text-align:center;margin:8px 0;transition:background .15s}
.toggle-btn:hover{background:var(--bg)}
.sort-pill{cursor:pointer;background:#fff;border:1.5px solid var(--border);border-radius:20px;padding:3px 12px;font-size:12px;color:var(--muted);transition:all .15s}
.sort-pill:hover{border-color:#aaa}
.sort-pill.sort-active{background:var(--text);color:#fff;border-color:var(--text)}
img.emoji{height:1em;width:1em;margin:0 .05em 0 .1em;vertical-align:-.1em;display:inline}
/* ── Floating filters ── */
#floating-filters{position:fixed;right:18px;top:80px;background:var(--card);border:1px solid var(--border);border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,.10);padding:0;z-index:90;width:210px;display:none}
#floating-filters .ff-header{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
#floating-filters .ff-body{padding:8px 10px;display:flex;flex-direction:column;gap:7px}
@media(min-width:1280px){#floating-filters{display:block}}
/* multi-select dropdown */
.ms-wrap{position:relative}
.ms-btn{width:100%;display:flex;justify-content:space-between;align-items:center;padding:4px 10px;border:1.5px solid var(--border);border-radius:6px;background:#fff;font-size:11px;color:var(--text);cursor:pointer;text-align:left;gap:4px}
.ms-btn:hover{border-color:#aaa}
.ms-btn.ms-active{border-color:var(--text);background:#f0f2f5}
.ms-btn .ms-arrow{color:var(--muted);font-size:9px;flex-shrink:0}
.ms-panel{display:none;position:absolute;right:0;top:calc(100% + 3px);width:100%;background:#fff;border:1.5px solid var(--border);border-radius:6px;box-shadow:0 4px 14px rgba(0,0,0,.12);z-index:300;max-height:200px;overflow-y:auto}
.ms-panel.open{display:block}
.ms-item{display:flex;align-items:center;gap:7px;padding:5px 10px;font-size:11px;color:var(--text);cursor:pointer;user-select:none}
.ms-item:hover{background:#f5f7fa}
.ms-item input[type=checkbox]{margin:0;cursor:pointer;accent-color:var(--text)}
.ms-sep{border:none;border-top:1px solid var(--border);margin:2px 0}
.ms-country-input{width:100%;box-sizing:border-box;padding:4px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:11px;color:var(--text);outline:none}
.ms-country-input:focus{border-color:#888}
#clear-filters-btn{background:none;border:1.5px solid #e74c3c;color:#e74c3c;border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer;transition:all .15s;display:none;width:100%}
#clear-filters-btn:hover{background:#e74c3c;color:#fff}
#filter-badge{font-size:10px;background:#e74c3c;color:#fff;border-radius:10px;padding:1px 6px;display:none}
.show-more-btn{cursor:pointer;background:#fff;border:1px solid var(--border);border-radius:var(--radius);padding:6px 14px;font-size:12px;color:var(--muted);display:block;margin:6px auto 14px;transition:background .15s}
.show-more-btn:hover{background:var(--bg)}

/* ── Charts ── */
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.chart-card{background:var(--card);border-radius:var(--radius);border:1px solid var(--border);padding:16px}
.chart-title{font-size:13px;font-weight:700;margin-bottom:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.chart-wrap{position:relative;height:260px}
.chart-wrap.tall{height:380px}
.chart-wrap.xtall{height:460px}
.trends-card{background:var(--card);border-radius:var(--radius);border:1px solid var(--border);padding:16px;margin-bottom:20px}
.trends-wrap{position:relative;height:260px}

/* ── About ── */
.about-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.about-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
.about-card h4{font-size:14px;margin-bottom:6px}
.about-card p{font-size:13px;color:var(--muted);line-height:1.6}
.pipeline{display:flex;gap:0;align-items:center;flex-wrap:wrap;margin:12px 0}
.pipe-step{background:var(--card);border:1.5px solid var(--border);border-radius:var(--radius);padding:8px 14px;font-size:12px;font-weight:600;text-align:center}
.pipe-arrow{color:var(--muted);font-size:18px;padding:0 4px}
.limitation{background:#fff8e1;border:1px solid #f9ca24;border-radius:var(--radius);padding:12px 16px;font-size:13px;color:#7d6608;margin-top:12px}

/* ── Footer ── */
footer{text-align:center;padding:20px;font-size:12px;color:var(--muted);border-top:1px solid var(--border);margin-top:20px}

@media(max-width:700px){
  .charts-grid,.about-grid{grid-template-columns:1fr}
  .field-grid{grid-template-columns:110px 1fr}
}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-logos">
      <img src="logos/moh.jpg" alt="משרד הבריאות" title="משרד הבריאות">
      <img src="logos/huji.svg" alt="האוניברסיטה העברית" title="האוניברסיטה העברית בירושלים">
    </div>
    <div>
      <h1>🛡️ FoodAlert — Risk Monitoring System</h1>
    </div>
  </div>
  <div class="meta" id="header-meta"></div>
  </div>
</div>

<div id="floating-filters">
  <div class="ff-header">
    <span>Filters</span>
    <span id="filter-badge"></span>
  </div>
  <div class="ff-body">
    <!-- Hazard dropdown -->
    <div class="ms-wrap" id="ms-wrap-hazard">
      <button class="ms-btn" id="ms-btn-hazard" onclick="toggleMsPanel('hazard',event)">
        <span id="ms-lbl-hazard">Hazard: All</span><span class="ms-arrow">▾</span>
      </button>
      <div class="ms-panel" id="msp-hazard">
        <label class="ms-item"><input type="checkbox" id="ms-all-hazard" checked onchange="toggleMsAll('hazard',this)"> All</label>
        <hr class="ms-sep">
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="hazard" value="biological" onchange="toggleMsOption('hazard',this)"> 🦠 Biological</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="hazard" value="chemical"   onchange="toggleMsOption('hazard',this)"> ⚗️ Chemical</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="hazard" value="allergen"   onchange="toggleMsOption('hazard',this)"> 🌾 Allergen</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="hazard" value="physical"   onchange="toggleMsOption('hazard',this)"> 🔩 Physical</label>
      </div>
    </div>
    <!-- Source dropdown -->
    <div class="ms-wrap" id="ms-wrap-source">
      <button class="ms-btn" id="ms-btn-source" onclick="toggleMsPanel('source',event)">
        <span id="ms-lbl-source">Source: All</span><span class="ms-arrow">▾</span>
      </button>
      <div class="ms-panel" id="msp-source">
        <label class="ms-item"><input type="checkbox" id="ms-all-source" checked onchange="toggleMsAll('source',this)"> All</label>
        <hr class="ms-sep">
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="source" value="rasff"           onchange="toggleMsOption('source',this)"> RASFF</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="source" value="fda_enforcement" onchange="toggleMsOption('source',this)"> FDA Enforcement</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="source" value="fsis"            onchange="toggleMsOption('source',this)"> USDA FSIS</label>
        <label class="ms-item"><input type="checkbox" class="ms-opt" data-t="source" value="fsa_uk"          onchange="toggleMsOption('source',this)"> FSA UK</label>
      </div>
    </div>
    <!-- Product dropdown (options populated by JS) -->
    <div class="ms-wrap" id="ms-wrap-product">
      <button class="ms-btn" id="ms-btn-product" onclick="toggleMsPanel('product',event)">
        <span id="ms-lbl-product">Product: All</span><span class="ms-arrow">▾</span>
      </button>
      <div class="ms-panel" id="msp-product">
        <label class="ms-item"><input type="checkbox" id="ms-all-product" checked onchange="toggleMsAll('product',this)"> All</label>
        <hr class="ms-sep">
        <!-- filled by JS -->
      </div>
    </div>
    <!-- Country dropdown (options populated by JS) -->
    <div class="ms-wrap" id="ms-wrap-country">
      <button class="ms-btn" id="ms-btn-country" onclick="toggleMsPanel('country',event)">
        <span id="ms-lbl-country">Country: All</span><span class="ms-arrow">▾</span>
      </button>
      <div class="ms-panel" id="msp-country">
        <label class="ms-item"><input type="checkbox" id="ms-all-country" checked onchange="toggleMsAll('country',this)"> All</label>
        <hr class="ms-sep">
        <!-- filled by JS -->
      </div>
    </div>
    <button id="clear-filters-btn" onclick="clearFilters()">× Clear all filters</button>
  </div>
</div>

<nav id="floating-toc">
  <div class="toc-header">On This Page</div>
  <ul>
    <li><a onclick="tocScrollTo('toc-overview')"><span>📊</span> Overview</a></li>
    <li><a onclick="tocScrollTo('toc-trends')"><span>📈</span> Alert Trends</a></li>
    <li><a onclick="tocScrollTo('toc-israel')"><span>🇮🇱</span> Israel Watch</a></li>
    <li><a onclick="tocScrollTo('toc-critical')"><span>🔴</span> Critical Alerts</a></li>
    <li><a onclick="tocScrollTo('toc-high')"><span>🟠</span> High Alerts</a></li>
    <li><a onclick="tocScrollTo('toc-medium', true)"><span>🟡</span> Medium Alerts</a></li>
    <li><a onclick="tocScrollTo('toc-breakdowns')"><span>📊</span> Data Sources</a></li>
    <li><a onclick="tocScrollTo('toc-about')"><span>ℹ️</span> About</a></li>
  </ul>
  <div style="border-top:1px solid var(--border);margin:6px 0 4px"></div>
  <div style="padding:6px 10px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:6px">Export CSV</div>
    <div style="display:flex;align-items:center;gap:4px;margin-bottom:3px">
      <input id="toc-export-days" type="number" value="30" min="1" max="365"
        oninput="checkExportDays(this)"
        style="width:46px;padding:3px 5px;border:1px solid var(--border);border-radius:5px;font-size:12px;text-align:center">
      <span style="font-size:11px;color:var(--muted)">days</span>
    </div>
    <div id="toc-days-warning" style="font-size:10px;color:#e67e22;display:none;margin-bottom:4px">
      ⚠️ Max available: ${DATA.meta.window_days}d
    </div>
    <div style="position:relative;margin-bottom:7px">
      <button id="tier-dd-btn" onclick="toggleTierDropdown(event)"
        style="width:100%;display:flex;justify-content:space-between;align-items:center;
               padding:4px 8px;border:1px solid var(--border);border-radius:5px;
               background:#fff;font-size:11px;cursor:pointer;color:var(--text)">
        <span id="tier-dd-label">Crit, High</span>
        <span style="font-size:9px;color:var(--muted)">▾</span>
      </button>
      <div id="tier-dd-panel"
        style="display:none;position:absolute;top:calc(100% + 2px);left:0;right:0;z-index:200;
               background:#fff;border:1px solid var(--border);border-radius:6px;
               box-shadow:0 4px 12px rgba(0,0,0,.1);padding:6px 8px">
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;padding:2px 0;border-bottom:1px solid var(--border);margin-bottom:4px">
          <input id="toc-exp-all" type="checkbox" onchange="tierAllChanged(this)"> All
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;padding:2px 0">
          <input id="toc-exp-crit" type="checkbox" checked onchange="tierChanged()"> <span style="color:var(--critical)">●</span> Critical
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;padding:2px 0">
          <input id="toc-exp-high" type="checkbox" checked onchange="tierChanged()"> <span style="color:var(--high)">●</span> High
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;padding:2px 0">
          <input id="toc-exp-med" type="checkbox" onchange="tierChanged()"> <span style="color:var(--medium)">●</span> Medium
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;padding:2px 0">
          <input id="toc-exp-low" type="checkbox" onchange="tierChanged()"> <span style="color:var(--muted)">●</span> Low
        </label>
      </div>
    </div>
    <button onclick="tocExportCSV()"
      style="width:100%;background:#1a1f2e;color:#fff;border:none;border-radius:6px;padding:6px 0;font-size:12px;font-weight:600;cursor:pointer">
      ⬇ Download CSV
    </button>
    <div id="toc-export-status" style="font-size:10px;color:var(--muted);margin-top:4px;text-align:center"></div>
  </div>
</nav>

<div class="container">

  <!-- OVERVIEW ROW: severity donut (left) + hazard donut (right) -->
  <div id="toc-overview" class="overview-row" style="align-items:flex-start">

    <!-- Left column -->
    <div style="display:flex;flex-direction:column;flex:1;min-width:0">
      <div class="section-title" style="position:relative">
        📊 Alerts by Severity
        <span class="info-btn" onclick="toggleTierInfo()" title="How are tiers defined?">ⓘ</span>
        <div id="tier-info-box" class="info-box" style="display:none;left:0;top:28px">
          Tiers are relative to the current 90-day window:<br>
          <b>Critical</b> — top 5% by risk score<br>
          <b>High</b> — top 5–20%<br>
          <b>Medium</b> — top 20–50%<br>
          <b>Low</b> — bottom 50%<br>
          <span style="color:var(--muted);font-size:11px">Score (1–10) is an absolute model score, fixed scale — comparable across time periods.</span>
        </div>
      </div>
      <div class="overview-wrap" style="flex:1">
        <div class="overview-donut-wrap">
          <canvas id="tierDonut"></canvas>
          <div class="overview-donut-center">
            <div class="dn" id="donut-total"></div>
            <div class="dl">Alerts</div>
          </div>
        </div>
        <div class="overview-stats" id="overview-stats"></div>
      </div>
    </div>

    <!-- Right column -->
    <div style="display:flex;flex-direction:column;flex:1">
      <div class="section-title">🦠 Hazard Type</div>
      <div style="background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px 20px;display:flex;align-items:center;gap:20px;flex:1">
        <div class="overview-donut-wrap">
          <canvas id="hazardChart"></canvas>
          <div class="overview-donut-center">
            <div class="dn" id="hazard-total"></div>
            <div class="dl">Alerts</div>
          </div>
        </div>
        <div id="hazard-legend" class="overview-stats"></div>
      </div>
    </div>

  </div>

  <!-- SECTION 1: Trends -->
  <div id="toc-trends" class="section">
    <div class="section-title">📈 Alert Trends — Last 13 Months</div>
    <div class="trends-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div class="chart-title" style="margin-bottom:0">Category trends — lines per category, bars = monthly total</div>
        <div style="display:flex;gap:6px">
          <button id="trend-btn-hazard" onclick="switchTrendBreakdown('hazard')"
            style="padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--israel);background:var(--israel);color:#fff;cursor:pointer;font-weight:600">
            Hazard type
          </button>
          <button id="trend-btn-product" onclick="switchTrendBreakdown('product')"
            style="padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--border);background:#fff;color:var(--muted);cursor:pointer">
            Product category
          </button>
        </div>
      </div>
      <div class="trends-wrap" style="height:400px"><canvas id="trendsLineChart"></canvas></div>
    </div>
    <div class="charts-grid" style="margin-top:16px">
      <div class="chart-card">
        <div class="chart-title">Product category (top 10)</div>
        <div class="chart-wrap xtall"><canvas id="productChart"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Origin country (top 10)</div>
        <div class="chart-wrap xtall"><canvas id="countryChart"></canvas></div>
      </div>
    </div>
  </div>

  <!-- SECTION 2: Israel Watch -->
  <div id="toc-israel" class="section">
    <div class="section-title">🇮🇱 Israel Watch</div>
    <div id="israel-section"></div>
  </div>

  <div id="critical-section-anchor"></div>

  <!-- Sort control -->
  <div style="display:flex;align-items:center;gap:8px;margin:4px 0 4px;padding:0 2px">
    <span style="font-size:12px;color:var(--muted)">Sort by:</span>
    <button id="sort-score-btn" class="sort-pill sort-active" onclick="setSortBy('score')">Score</button>
    <button id="sort-date-btn"  class="sort-pill"             onclick="setSortBy('date')">Date</button>
  </div>

  <!-- SECTION 3: Critical Alerts -->
  <div id="toc-critical" class="section">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>🔴 Critical Alerts <span class="count" id="critical-count"></span></span>
      <button class="toggle-btn" onclick="toggleCritical()" id="critical-toggle-btn"
        style="width:auto;margin:0;padding:4px 12px;font-size:12px">▴ Hide</button>
    </div>
    <div id="critical-section-body">
      <div id="critical-feed"></div>
      <button class="show-more-btn" id="critical-more-btn"></button>
    </div>
  </div>

  <!-- SECTION 4: High Alerts -->
  <div id="toc-high" class="section">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>🟠 High Alerts <span class="count" id="high-count"></span></span>
      <button class="toggle-btn" onclick="toggleHigh()" id="high-toggle-btn"
        style="width:auto;margin:0;padding:4px 12px;font-size:12px">▴ Hide</button>
    </div>
    <div id="high-section-body">
      <div id="high-feed"></div>
      <button class="show-more-btn" id="high-more-btn"></button>
    </div>
  </div>

  <!-- SECTION 5: Medium Alerts -->
  <div id="toc-medium" class="section">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>🟡 Medium Alerts <span class="count" id="medium-count"></span></span>
      <button class="toggle-btn" onclick="toggleMedium()" id="medium-toggle-btn"
        style="width:auto;margin:0;padding:4px 12px;font-size:12px">▾ Show</button>
    </div>
    <div id="medium-section" style="display:none">
      <div id="medium-feed"></div>
      <button class="show-more-btn" id="medium-more-btn"></button>
    </div>
  </div>

  <!-- SECTION 4: Breakdowns -->
  <div id="toc-breakdowns" class="section">
    <div class="section-title">📊 Breakdowns <span class="count" id="breakdown-window"></span></div>
    <div class="charts-grid">
      <div class="chart-card" style="grid-column:1/-1">
        <div class="chart-title">Data source</div>
        <div style="display:flex;gap:24px">
          <!-- Left half: donut + legend -->
          <div style="flex:1;display:flex;flex-direction:column;align-items:flex-start;gap:16px">
            <div style="position:relative;width:100%;max-width:320px;aspect-ratio:1;align-self:center">
              <canvas id="sourceChart"></canvas>
              <div class="overview-donut-center">
                <div class="dn" id="source-total"></div>
                <div class="dl">Alerts</div>
              </div>
            </div>
            <div id="source-legend" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;width:100%"></div>
          </div>
          <!-- Right half: source description cards -->
          <div style="flex:1">
          <div class="about-grid" style="margin:0">
            <div class="about-card" style="border-left:4px solid #003087">
              <h4>🇺🇸 FDA Enforcement Reports</h4>
              <p>US Food &amp; Drug Administration mandatory recall reports. Covers food, dietary supplements, cosmetics. Severity: Class I (serious health risk), Class II (temporary adverse health consequences), Class III (unlikely to cause health problems). ~15,900 alerts in database.</p>
            </div>
            <div class="about-card" style="border-left:4px solid #007749">
              <h4>🇺🇸 USDA FSIS Recalls</h4>
              <p>US Department of Agriculture Food Safety and Inspection Service. Covers meat, poultry, and egg products. Same Class I/II/III severity system as FDA. ~900 alerts in database.</p>
            </div>
            <div class="about-card" style="border-left:4px solid #9c1a1c">
              <h4>🇬🇧 FSA UK Food Alerts</h4>
              <p>UK Food Standards Agency. Alert types: AA (Allergy Alert), PRIN (Product Recall Information Notice), FAFA (Food Alert for Action — highest urgency). ~1,300 alerts in database.</p>
            </div>
            <div class="about-card" style="border-left:4px solid #003399">
              <h4>🇪🇺 RASFF (EU)</h4>
              <p>EU Rapid Alert System for Food and Feed. Covers all 27 EU member states + EEA. Notification types: serious risk, potentially serious, information. Includes origin country and distribution countries per alert. ~5,500 alerts in database (2025–2026).</p>
            </div>
          </div>
          </div><!-- end right half -->
        </div>
      </div>
    </div>
  </div>

  <!-- SECTION 6: About -->
  <div id="toc-about" class="section">
    <div class="section-title">ℹ️ About This Dashboard</div>
    <div class="about-grid">
      <div class="about-card">
        <h4>What is FoodSafe Intelligence?</h4>
        <p>An open-source intelligence (OSINT) system that automatically collects, ranks, and presents food safety alerts from official regulatory sources worldwide. Built for the Israeli Ministry of Health's Food Risk Management Unit to enable rapid awareness of emerging food safety threats.</p>
      </div>
      <div class="about-card">
        <h4>How are alerts ranked?</h4>
        <p>A <strong>Bi-Encoder AI model</strong> (DistilRoBERTa, 82M parameters) reads each alert's full text and produces a single risk score. It was trained on ~26,000 pairwise comparisons ("Is alert A more dangerous than B?") labeled by an LLM judge. Each alert is encoded once, making scoring fast and scalable.</p>
        <p style="margin-top:6px">Training used a 3-way split: 70% train / 15% validation / 15% test. The model achieved <strong>69.3% pairwise accuracy</strong> on the test set, vs. 67.3% for the severity-label baseline. Dropout (0.3) prevents overfitting.</p>
      </div>
      <div class="about-card">
        <h4>🇮🇱 Israel Watch logic</h4>
        <p>An alert is flagged as Israel-relevant when "Israel" appears in any of: origin country, distribution countries, title, or description. <strong>Limitation:</strong> this is text-based matching only — it does not consult actual import registries. Many products distributed in Israel may not be captured if not explicitly named.</p>
      </div>
      <div class="about-card">
        <h4>Score drivers (counterfactual analysis)</h4>
        <p>For selected top alerts, the system tests 10 hypothetical changes — replacing the hazard, severity, or distribution — and measures how the score would change. The largest changes reveal which factors drive the risk ranking.</p>
      </div>
    </div>

    <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:16px">
      <div class="chart-title" style="margin-bottom:12px">Processing pipeline</div>

      <div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">① Model training (one-time)</div>
      <div class="pipeline" style="margin-bottom:16px">
        <div class="pipe-step">📥 Collect sample<br><small>~330 alerts</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">🤖 LLM Labeling<br><small>26,000 pairwise comparisons<br>Claude judges A vs B</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">🧠 Train Bi-Encoder<br><small>DistilRoBERTa (BERT)<br>82M parameters · 3 epochs</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">✅ Trained model<br><small>69.3% pairwise accuracy<br>vs 67.3% baseline</small></div>
      </div>

      <div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">② Daily pipeline</div>
      <div class="pipeline">
        <div class="pipe-step">📥 Collect<br><small>FDA · FSIS · FSA UK · RASFF</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">🗄️ Store<br><small>SQLite DB</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">⚡ Score<br><small>Bi-Encoder AI<br>(trained model)</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">📊 Rank<br><small>Percentile tiers</small></div>
        <div class="pipe-arrow">→</div>
        <div class="pipe-step">📋 Report<br><small>This dashboard</small></div>
      </div>
    </div>

    <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:16px">
      <div class="chart-title" style="margin-bottom:10px">What appears in this dashboard?</div>
      <p style="font-size:13px;color:var(--muted);margin-bottom:10px">Alerts are ranked by the Bi-Encoder score within the current time window and assigned to tiers by percentile:</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#f0f2f5">
          <th style="padding:7px 10px;text-align:left;border:1px solid var(--border)">Tier</th>
          <th style="padding:7px 10px;text-align:left;border:1px solid var(--border)">Percentile (within window)</th>
          <th style="padding:7px 10px;text-align:left;border:1px solid var(--border)">Visibility</th>
        </tr>
        <tr>
          <td style="padding:7px 10px;border:1px solid var(--border)"><span class="badge critical">CRITICAL</span></td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Top 5%</td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Always visible, expanded by default (first 10)</td>
        </tr>
        <tr>
          <td style="padding:7px 10px;border:1px solid var(--border)"><span class="badge high">HIGH</span></td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Top 5–20%</td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Always visible (first 10 shown)</td>
        </tr>
        <tr>
          <td style="padding:7px 10px;border:1px solid var(--border)"><span class="badge medium">MEDIUM</span></td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Top 20–50%</td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Hidden by default — click toggle to show</td>
        </tr>
        <tr>
          <td style="padding:7px 10px;border:1px solid var(--border)"><span class="badge low">LOW</span></td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Bottom 50%</td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Not shown (stored in database only)</td>
        </tr>
        <tr style="background:#eaf0fb">
          <td style="padding:7px 10px;border:1px solid var(--border)"><span class="badge israel">🇮🇱 Israel</span></td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Any</td>
          <td style="padding:7px 10px;border:1px solid var(--border)">Always shown in Israel Watch — score does not matter</td>
        </tr>
      </table>
      <p style="font-size:12px;color:var(--muted);margin-top:8px">⚠️ <strong>Tiers</strong> are relative to the current window — a "Critical" alert is the most severe <em>among alerts in this period</em>. The <strong>X.X/10 score</strong> is absolute: normalized from the model's output range (−5.65 to +6.71) and is comparable across time periods. RASFF alerts were not included in model training; their scores are extrapolated.</p>
    </div>

    <div class="limitation">
      ⚠️ <strong>Data coverage note:</strong> This system monitors official regulatory sources in the US, UK, and EU. It does not monitor Israeli domestic alerts, Israeli import registries, or Asian/South American regulatory bodies. The Israel Watch section captures only alerts where Israel is explicitly mentioned in a foreign source.
    </div>
  </div>

</div>

<footer>
  FoodSafe Intelligence · Built for the Israeli Ministry of Health, Food Risk Management Unit ·
  Hebrew University of Jerusalem MBA Capstone 2026 ·
  Generated <span id="gen-time"></span>
</footer>

<script>
const DATA = __DATA_PLACEHOLDER__;

const TIER_BADGE = {
  critical: '<span class="badge critical">CRITICAL</span>',
  high:     '<span class="badge high">HIGH</span>',
  medium:   '<span class="badge medium">MEDIUM</span>',
  low:      '<span class="badge low">LOW</span>',
};

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// Returns an impact badge showing score change on the 1–10 scale.
// Picks the perturbation with the highest |delta_abs| among the provided keys.
function cfImpact(cf, pertKeys){
  if(!cf || !cf.badges) return '';
  const candidates = pertKeys.map(k => cf.badges[k]).filter(Boolean);
  if(!candidates.length) return '';
  const best = candidates.reduce((a,b) => Math.abs(b.delta_abs) > Math.abs(a.delta_abs) ? b : a);
  if(Math.abs(best.delta_abs) < 0.1) return '';
  const sign = best.delta_abs >= 0 ? '+' : '';
  const cls  = best.delta_abs > 0 ? 'up' : 'down';
  const origAbs = cf.original_abs != null ? cf.original_abs.toFixed(1) : '?';
  const title = `${best.label}: score would be ${best.after_abs.toFixed(1)}/10 instead of ${origAbs}/10 (${sign}${best.delta_abs.toFixed(1)} pts)`;
  return `<span class="impact-badge ${cls}" title="${title}">${sign}${best.delta_abs.toFixed(1)}</span>`;
}

function renderCard(a, extraClass=''){
  const cf   = DATA.counterfactuals[a.alert_id];
  const dist = (a.distribution_countries||[]).slice(0,6).join(', ') || '—';
  const SOURCE_LINK_LABEL = {
    'fda_enforcement': 'FDA',
    'fsis':            'USDA FSIS',
    'fsa_uk':          'FSA UK',
    'rasff':           'RASFF',
  };
  const srcLabel = SOURCE_LINK_LABEL[a.source_id] || a.source_id;
  // For FDA: API doesn't provide a direct permalink — link to general recalls search
  // and show recall number so user can find it manually if needed
  let link = '';
  if(a.source_id === 'fda_enforcement'){
    const recallNum = a.alert_id.replace('fda_enforcement::', '');
    const apiUrl = `https://api.fda.gov/food/enforcement.json?search=recall_number.exact:%22${recallNum}%22`;
    const firm = encodeURIComponent(a.recalling_firm || recallNum);
    const searchUrl = `https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts?search_api_fulltext=${firm}`;
    link = `<a class="source-link" href="${apiUrl}" target="_blank" rel="noopener">FDA Enforcement Record ↗</a>`
         + `<a class="source-link" style="margin-left:12px" href="${searchUrl}" target="_blank" rel="noopener">Search press release on FDA ↗</a>`;
  } else if(a.record_url){
    link = `<a class="source-link" href="${esc(a.record_url)}" target="_blank" rel="noopener">View on ${srcLabel} ↗</a>`;
  }
  const israelBadge = a.israel_relevance_flag
    ? '<span class="badge israel">🇮🇱 Israel</span>' : '';

  let cfHtml = '';
  if(cf && cf.top3 && cf.top3.length){
    const maxAbs = Math.max(...cf.top3.map(p=>Math.abs(p.delta_abs||0)));
    const rows = cf.top3.map(p=>{
      const dAbs = p.delta_abs || 0;
      const w = maxAbs>0 ? Math.round(Math.abs(dAbs)/maxAbs*100) : 0;
      const cls = dAbs>=0 ? 'up' : 'down';
      const sign = dAbs>=0 ? '+' : '';
      const col  = dAbs>=0 ? '#27ae60' : '#c0392b';
      const tooltip = p.after_abs != null
        ? `title="Score would be ${p.after_abs.toFixed(1)}/10 instead of ${cf.original_abs ? cf.original_abs.toFixed(1) : '?'}/10"` : '';
      return `<div class="cf-row">
        <span class="cf-label">${esc(p.label)}</span>
        <span class="cf-delta ${cls}" ${tooltip}>${sign}${dAbs.toFixed(1)} pts</span>
        <div class="cf-bar-wrap"><div class="cf-bar-fill" style="width:${w}%;background:${col}"></div></div>
      </div>`;
    }).join('');
    cfHtml = `<div class="cf-block">
      <div class="cf-title">Score drivers — counterfactual analysis</div>
      ${rows}
    </div>`;
  }

  const firmRow = a.recalling_firm
    ? `<div class="field-label">Recalling firm</div><div class="field-value">${esc(a.recalling_firm)}</div>` : '';
  const illRow = a.illness_count != null
    ? `<div class="field-label">Illnesses reported</div><div class="field-value">${a.illness_count}</div>` : '';
  const popRow = a.population_at_risk
    ? `<div class="field-label">Population at risk</div><div class="field-value">${esc(a.population_at_risk)}</div>` : '';
  const descRow = a.description
    ? `<div class="field-label">Description</div><div class="field-value">${esc(a.description)}${a.description.length>=500?'…':''}</div>` : '';
  const israelRow = a.israel_reason
    ? `<div class="field-label">Israel relevance</div><div class="field-value">${esc(a.israel_reason)}</div>` : '';

  const regionBadge = a.region ? `<span class="region-badge">${esc(a.region)}</span>` : '';

  const cardId = 'alert-' + a.alert_id.replace(/[^a-zA-Z0-9]/g, '-');
  return `<details class="alert-card ${a.tier} ${extraClass}" id="${cardId}">
  <summary>
    ${TIER_BADGE[a.tier]||''}
    <span class="card-score" title="Absolute risk score 1–10 (min-max normalized from model output range −5.65 to +6.71)">${a.absolute_score.toFixed(1)}<span style="font-size:11px;font-weight:400;opacity:.6">/10</span></span>
    ${israelBadge}
    ${regionBadge}
    <span class="card-title">${esc(a.title||a.alert_id)}</span>
    <span class="card-meta">${esc(a.source_published_date)}</span>
    <span class="expand-hint">▸</span>
  </summary>
  <div class="card-body">
    <div class="field-grid">
      <div class="field-label">Hazard</div>
      <div class="field-value">${esc(a.hazard_specific||a.hazard_category||'—')}</div>
      <div class="field-label">Severity</div>
      <div class="field-value">${esc(a.severity_raw||'—')}</div>
      ${a.event_initiation_date ? `<div class="field-label">Recall initiated</div><div class="field-value">${esc(a.event_initiation_date)}</div>` : ''}
      ${firmRow}
      <div class="field-label">Distribution</div>
      <div class="field-value">${esc(dist)}</div>
      <div class="field-label">Origin</div>
      <div class="field-value">${esc(a.origin_country||'—')}</div>
      ${popRow}${illRow}${israelRow}
      <div class="field-label">Risk score</div>
      <div class="field-value"><strong>${a.absolute_score.toFixed(1)}/10</strong> absolute &nbsp;·&nbsp; top <strong>${Math.round((1-a.window_percentile)*100)}%</strong> of current window → <strong>${a.tier.toUpperCase()}</strong></div>
      ${descRow}
    </div>
    ${link}
    ${a.score_explanation ? `<div class="score-explain"><span class="se-label">Why this score?</span>${esc(a.score_explanation)}</div>` : ''}
    ${cfHtml}
  </div>
</details>`;
}

// ── Header ────────────────────────────────────────────────────────────────
const m = DATA.meta;
document.getElementById('header-meta').textContent =
  `${m.window_days}-day window: ${m.window_start} – ${m.ref_date}  ·  ${m.n_in_window} alerts`;
document.getElementById('gen-time').textContent = m.generated_at;
document.getElementById('breakdown-window').textContent =
  `last ${m.window_days} days: ${m.window_start} – ${m.ref_date}`;
// ── Overview donut ───────────────────────────────────────────────────────
(function(){
  const total = m.n_in_window;
  const n_other = total - m.n_critical - m.n_high - m.n_medium;
  document.getElementById('donut-total').textContent = total;

  const TIERS = [
    { label:'Critical', n: m.n_critical, color:'#c0392b', scrollTo:'critical-section-anchor' },
    { label:'High',     n: m.n_high,     color:'#d35400', scrollTo:'high-feed' },
    { label:'Medium',   n: m.n_medium,   color:'#d4ac0d', scrollTo:'toc-medium', toggleMed:true },
    { label:'Low',      n: n_other,      color:'#aab4c8', scrollTo:null },
  ];

  function scrollWithOffset(el){
    const headerH = document.querySelector('.header').offsetHeight;
    const top = el.getBoundingClientRect().top + window.scrollY - headerH - 12;
    window.scrollTo({top, behavior:'smooth'});
  }

  function scrollToTier(t){
    if(!t.scrollTo) return;
    if(t.toggleMed){
      const sec = document.getElementById('medium-section');
      if(sec && (sec.style.display==='none' || !sec.style.display)) toggleMedium();
    }
    const el = document.getElementById(t.scrollTo);
    if(el) scrollWithOffset(el);
  }

  // Stats list (label + absolute number, no pct — pct shown on donut itself)
  const statsEl = document.getElementById('overview-stats');
  statsEl.innerHTML = TIERS.filter(t=>t.label!=='Low').map(t =>
    `<div class="ov-stat" onclick="scrollToTierById('${t.scrollTo}', ${t.toggleMed||false})">
      <div class="ov-dot" style="background:${t.color}"></div>
      <div class="ov-label">${t.label}</div>
      <div class="ov-val">${t.n}</div>
    </div>`
  ).join('') +
  `<div style="position:relative">
    <div class="ov-stat ov-israel" onclick="scrollToTierById('israel-section',false)">
      <div class="ov-dot">🇮🇱</div>
      <div class="ov-label">Israel Watch</div>
      <div class="ov-val">${m.n_israel}</div>
      <span class="info-btn" style="margin-left:6px" onclick="event.stopPropagation();toggleIsraelInfo()" title="Israel Watch logic">ⓘ</span>
    </div>
    <div id="israel-info-box" class="info-box" style="display:none;left:0;top:100%;max-width:320px">
      <b>🇮🇱 Israel Watch logic</b><br><br>
      An alert is flagged as Israel-relevant when "Israel" appears in any of: origin country, distribution countries, title, or description.<br><br>
      <span style="color:var(--muted);font-size:11px">⚠️ <b>Limitation:</b> This is text-based matching only — it does not consult actual import registries. Many products distributed in Israel may not be captured if not explicitly named.</span>
    </div>
  </div>`;

  // Chart — percentages shown directly on segments via datalabels plugin
  Chart.register(ChartDataLabels);
  const ctx = document.getElementById('tierDonut').getContext('2d');
  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: TIERS.map(t=>t.label),
      datasets: [{ data: TIERS.map(t=>t.n), backgroundColor: TIERS.map(t=>t.color), borderWidth:2, borderColor:'#fff' }]
    },
    options: {
      cutout: '60%',
      plugins: {
        legend: { display:false },
        tooltip: { callbacks: { label: c => ` ${c.label}: ${c.raw} (${total ? Math.round(c.raw/total*100) : 0}%)` } },
        datalabels: {
          color: '#fff',
          font: { weight:'bold', size:12 },
          formatter: (value) => {
            const pct = total ? Math.round(value / total * 100) : 0;
            return pct >= 4 ? pct + '%' : '';
          }
        }
      },
      onClick: (evt, elements) => {
        if(!elements.length) return;
        const t = TIERS[elements[0].index];
        scrollToTier(t);
      }
    }
  });
})();

function toggleTierInfo(){
  const box = document.getElementById('tier-info-box');
  box.style.display = box.style.display === 'none' ? 'block' : 'none';
}
function toggleIsraelInfo(){
  const box = document.getElementById('israel-info-box');
  box.style.display = box.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', e => {
  if(!e.target.classList.contains('info-btn')){
    ['tier-info-box','israel-info-box'].forEach(id => {
      const box = document.getElementById(id);
      if(box) box.style.display = 'none';
    });
  }
});

function scrollToTierById(id, toggleMed){
  if(toggleMed){
    const sec = document.getElementById('medium-section');
    if(sec && (sec.style.display==='none' || !sec.style.display)) toggleMedium();
  }
  const el = document.getElementById(id);
  if(!el) return;
  const headerH = document.querySelector('.header').offsetHeight;
  const top = el.getBoundingClientRect().top + window.scrollY - headerH - 12;
  window.scrollTo({top, behavior:'smooth'});
}

// ── Israel Watch ──────────────────────────────────────────────────────────
(function(){
  const el = document.getElementById('israel-section');
  if(DATA.israel_alerts.length > 0){
    el.innerHTML = DATA.israel_alerts.map(a=>renderCard(a,'israel-feed')).join('');
  } else {
    let html = `<div class="all-clear">
      <span class="icon">✅</span>
      <div>
        <div>All clear — no Israel-relevant alerts in the current ${m.window_days}-day window.</div>
        <div class="sub">No food safety alerts mentioning Israel were detected between ${m.window_start} and ${m.ref_date}.</div>
      </div>
    </div>`;
    if(DATA.israel_fallback.length > 0){
      html += `<details style="margin-top:12px">
        <summary style="cursor:pointer;color:var(--muted);font-size:13px;padding:6px 0">
          Most recent Israel-relevant alerts (outside current window) ▸
        </summary>
        <div class="fallback-note" style="margin-top:8px">These alerts are outside the current ${m.window_days}-day window. Shown for reference only.</div>
        ${DATA.israel_fallback.map(a=>renderCard(a,'israel-feed')).join('')}
      </details>`;
    }
    el.innerHTML = html;
  }
})();

// ── Alert Feed ────────────────────────────────────────────────────────────
const INITIAL_SHOW = 10;

// Sort + filter state
let _sortMode = 'score';
const _feedAlerts = {};
const _charts = {};
const _filters = { hazard: new Set(), source: new Set(), product: new Set(), country: new Set() };

function _passesFilters(a) {
  if (_filters.hazard.size && !_filters.hazard.has((a.hazard_category||'').toLowerCase())) return false;
  if (_filters.source.size && !_filters.source.has(a.source_id)) return false;
  if (_filters.product.size && !_filters.product.has((a.product_category||'').toLowerCase())) return false;
  if (_filters.country.size) {
    const origin = (a.origin_country||'').toLowerCase();
    const dist = (Array.isArray(a.distribution_countries)
      ? a.distribution_countries.join(' ')
      : String(a.distribution_countries||'')).toLowerCase();
    const match = [..._filters.country].some(q => origin.includes(q) || dist.includes(q));
    if (!match) return false;
  }
  return true;
}

// ── Multi-select dropdown helpers ────────────────────────────────
function toggleMsPanel(type, e) {
  e && e.stopPropagation();
  const panel = document.getElementById('msp-'+type);
  const isOpen = panel.classList.contains('open');
  document.querySelectorAll('.ms-panel').forEach(p => p.classList.remove('open'));
  if (!isOpen) panel.classList.add('open');
}

function toggleMsOption(type, cb) {
  const val = cb.value;
  if (cb.checked) _filters[type].add(val); else _filters[type].delete(val);
  const allCb = document.getElementById('ms-all-'+type);
  const opts  = document.querySelectorAll(`#msp-${type} .ms-opt`);
  allCb.checked = _filters[type].size === 0;
  _updateMsLabel(type);
  _applyAll();
}

function toggleMsAll(type, cb) {
  // "All" always means: clear all individual selections → no filter
  _filters[type].clear();
  document.querySelectorAll(`#msp-${type} .ms-opt`).forEach(o => o.checked = false);
  cb.checked = true; // keep All visually checked
  _updateMsLabel(type);
  _applyAll();
}

function _updateMsLabel(type) {
  const lbl = document.getElementById('ms-lbl-'+type);
  const btn = document.getElementById('ms-btn-'+type);
  if (!lbl) return;
  const typeName = type.charAt(0).toUpperCase()+type.slice(1);
  if (_filters[type].size === 0) {
    lbl.textContent = typeName+': All';
    btn.classList.remove('ms-active');
  } else {
    const names = [..._filters[type]].map(v=>v.charAt(0).toUpperCase()+v.slice(1));
    lbl.textContent = names.length <= 2 ? typeName+': '+names.join(', ') : typeName+': '+names.length+' selected';
    btn.classList.add('ms-active');
  }
}

function clearFilters() {
  ['hazard','source','product','country'].forEach(t => {
    _filters[t].clear();
    const allCb = document.getElementById('ms-all-'+t);
    if (allCb) allCb.checked = true;
    document.querySelectorAll(`#msp-${t} .ms-opt`).forEach(o => o.checked = false);
    _updateMsLabel(t);
  });
  _applyAll();
}

// Close dropdowns when clicking outside (but not when clicking inside a panel)
document.addEventListener('click', () =>
  document.querySelectorAll('.ms-panel').forEach(p => p.classList.remove('open')));
document.querySelectorAll('.ms-panel').forEach(p =>
  p.addEventListener('click', e => e.stopPropagation()));

function _applyAll() {
  const n = _filters.hazard.size + _filters.source.size + _filters.product.size + _filters.country.size;
  const clearBtn = document.getElementById('clear-filters-btn');
  const badge    = document.getElementById('filter-badge');
  clearBtn.style.display = n ? 'block' : 'none';
  badge.textContent      = n ? `${n} active` : '';
  badge.style.display    = n ? 'inline' : 'none';

  _rerenderFeeds();

  document.getElementById('critical-count').textContent = _feedAlerts.critical.filter(_passesFilters).length;
  document.getElementById('high-count').textContent     = _feedAlerts.high.filter(_passesFilters).length;
  document.getElementById('medium-count').textContent   = _feedAlerts.medium.filter(_passesFilters).length;

  if (n === 0) {
    _resetBreakdowns();
  } else {
    const all = [..._feedAlerts.critical, ..._feedAlerts.high, ..._feedAlerts.medium].filter(_passesFilters);
    _updateBreakdowns(all);
  }
}

function _resetBreakdowns() {
  const HAZARD_COLORS = ['#c0392b','#8e44ad','#e67e22','#2980b9','#16a085','#7f8c8d','#bdc3c7'];
  const SOURCE_COLORS = ['#003087','#007749','#9c1a1c','#f0b400'];

  if (_charts.hazard) {
    const hd = DATA.breakdowns.hazard_category;
    const hTotal = hd.data.reduce((s,v)=>s+v,0);
    _charts.hazard.data.labels = hd.labels;
    _charts.hazard.data.datasets[0].data = hd.data;
    _charts.hazard.data.datasets[0].backgroundColor = HAZARD_COLORS.slice(0, hd.labels.length);
    _charts.hazard.options.plugins.datalabels.formatter = v => {
      const pct = hTotal ? Math.round(v/hTotal*100) : 0;
      return pct >= 4 ? pct+'%' : '';
    };
    _charts.hazard.update();
    document.getElementById('hazard-total').textContent = hTotal;
    const legendEl = document.getElementById('hazard-legend');
    if (legendEl) legendEl.innerHTML = hd.labels.map((lbl,i) =>
      `<div class="ov-stat" style="cursor:default">
        <div class="ov-dot" style="background:${HAZARD_COLORS[i%HAZARD_COLORS.length]}"></div>
        <div class="ov-label" style="width:90px;text-transform:capitalize">${lbl}</div>
        <div class="ov-val">${hd.data[i]}</div>
      </div>`).join('');
  }
  if (_charts.product) {
    const pd = DATA.breakdowns.product_category;
    const pdTotal = pd.data.reduce((s,v)=>s+v,0);
    _charts.product.data.labels = pd.labels.map(l=>_truncLabel(l,4));
    _charts.product.data.datasets[0].data = pd.data;
    _charts.product.options.plugins.tooltip.callbacks.title = items => pd.labels[items[0].dataIndex];
    _charts.product.options.plugins.tooltip.callbacks.label = item => ` ${item.raw}  (${pdTotal ? Math.round(item.raw/pdTotal*100) : 0}%)`;
    _charts.product.options.scales.y.ticks.callback = (val,i) => _truncLabel(pd.labels[i]||'',4);
    _charts.product.update();
  }
  if (_charts.country) {
    const cd = DATA.breakdowns.origin_country;
    const cdTotal = cd.data.reduce((s,v)=>s+v,0);
    _charts.country.data.labels = cd.labels;
    _charts.country.data.datasets[0].data = cd.data;
    _charts.country.options.plugins.tooltip.callbacks.label = item => ` ${item.raw}  (${cdTotal ? Math.round(item.raw/cdTotal*100) : 0}%)`;
    _charts.country.update();
  }
  if (_charts.source) {
    const sd = DATA.breakdowns.source;
    const sTotal = sd.data.reduce((s,v)=>s+v,0);
    _charts.source.data.labels = sd.labels;
    _charts.source.data.datasets[0].data = sd.data;
    _charts.source.data.datasets[0].backgroundColor = sd.colors || SOURCE_COLORS;
    _charts.source.options.plugins.datalabels.formatter = v => {
      const pct = sTotal ? Math.round(v/sTotal*100) : 0;
      return pct >= 4 ? pct+'%' : '';
    };
    _charts.source.update();
    document.getElementById('source-total').textContent = sTotal;
    const legendEl = document.getElementById('source-legend');
    if (legendEl) legendEl.innerHTML = sd.labels.map((lbl,i) =>
      `<div class="ov-stat" style="cursor:default">
        <div class="ov-dot" style="background:${(sd.colors||SOURCE_COLORS)[i%SOURCE_COLORS.length]}"></div>
        <div class="ov-label" style="width:auto;flex:1;font-size:12px">${lbl}</div>
        <div class="ov-val">${sd.data[i]}</div>
      </div>`).join('');
  }
}

function _sortedAlerts(arr){
  const copy = [...arr];
  if(_sortMode === 'date'){
    copy.sort((a,b) => b.source_published_date.localeCompare(a.source_published_date));
  } else {
    copy.sort((a,b) => b.absolute_score - a.absolute_score);
  }
  return copy;
}

function renderFeedSection(alerts, containerId, btnId, label){
  const container = document.getElementById(containerId);
  const btn       = document.getElementById(btnId);
  if(!alerts.length){ btn.style.display='none'; return; }

  let showing = Math.min(INITIAL_SHOW, alerts.length);

  function render(){
    container.innerHTML = alerts.slice(0, showing).map(a=>renderCard(a)).join('');
    if(showing >= alerts.length){
      btn.style.display='none';
    } else {
      btn.style.display='block';
      btn.textContent = `▾ Show all ${alerts.length} ${label} alerts (${alerts.length - showing} more)`;
    }
  }

  btn.onclick = ()=>{ showing = alerts.length; render(); };
  render();
}

function _rerenderFeeds(){
  renderFeedSection(_sortedAlerts(_feedAlerts.critical.filter(_passesFilters)), 'critical-feed', 'critical-more-btn', 'critical');
  renderFeedSection(_sortedAlerts(_feedAlerts.high.filter(_passesFilters)),     'high-feed',     'high-more-btn',     'high');
  renderFeedSection(_sortedAlerts(_feedAlerts.medium.filter(_passesFilters)),   'medium-feed',   'medium-more-btn',   'medium');
}

function setSortBy(mode){
  _sortMode = mode;
  document.getElementById('sort-score-btn').classList.toggle('sort-active', mode==='score');
  document.getElementById('sort-date-btn').classList.toggle('sort-active',  mode==='date');
  _rerenderFeeds();
}

(function(){
  _feedAlerts.critical = DATA.alerts.filter(a=>a.tier==='critical');
  _feedAlerts.high     = DATA.alerts.filter(a=>a.tier==='high');
  _feedAlerts.medium   = DATA.alerts.filter(a=>a.tier==='medium');

  document.getElementById('critical-count').textContent = _feedAlerts.critical.length;
  document.getElementById('high-count').textContent     = _feedAlerts.high.length;
  document.getElementById('medium-count').textContent   = _feedAlerts.medium.length;

  _rerenderFeeds();

  // After all cards are in the DOM, handle deep-link hash navigation
  if (window.location.hash) openAlertFromHash();

  // Populate country multi-select panel (sorted alphabetically)
  const _allFeedAlerts = [..._feedAlerts.critical, ..._feedAlerts.high, ..._feedAlerts.medium];
  const _countries = [...new Set(_allFeedAlerts.map(a => a.origin_country).filter(Boolean))].sort();
  const _countryPanel = document.getElementById('msp-country');
  _countries.forEach(c => {
    const lbl = document.createElement('label');
    lbl.className = 'ms-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.className = 'ms-opt'; cb.dataset.t = 'country';
    cb.value = c.toLowerCase();
    cb.addEventListener('change', () => toggleMsOption('country', cb));
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(' ' + c));
    _countryPanel.appendChild(lbl);
  });

  // Populate product category multi-select panel (sorted by frequency)
  const _prodCounts = {};
  _allFeedAlerts.forEach(a => { if (a.product_category) _prodCounts[a.product_category] = (_prodCounts[a.product_category]||0)+1; });
  const _prodPanel = document.getElementById('msp-product');
  Object.entries(_prodCounts).sort((a,b)=>b[1]-a[1]).forEach(([cat]) => {
    const lbl = document.createElement('label');
    lbl.className = 'ms-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.className = 'ms-opt'; cb.dataset.t = 'product';
    cb.value = cat.toLowerCase();
    cb.addEventListener('change', () => toggleMsOption('product', cb));
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(' ' + cat.charAt(0).toUpperCase() + cat.slice(1)));
    _prodPanel.appendChild(lbl);
  });

  // Position floating-filters below the sticky header
  (function positionFilters(){
    const hdr = document.querySelector('.header');
    if(hdr) document.getElementById('floating-filters').style.top = (hdr.offsetHeight + 10) + 'px';
  })();

  const medToggleBtn = document.getElementById('medium-toggle-btn');
  if(_feedAlerts.medium.length > 0){
    medToggleBtn.textContent = `▾ Show (${_feedAlerts.medium.length})`;
  } else {
    medToggleBtn.style.display='none';
  }
})();

function toggleCritical(){
  const body = document.getElementById('critical-section-body');
  const btn  = document.getElementById('critical-toggle-btn');
  if(body.style.display==='none'){
    body.style.display='block';
    btn.textContent = '▴ Hide';
  } else {
    body.style.display='none';
    const n = DATA.alerts.filter(a=>a.tier==='critical').length;
    btn.textContent = `▾ Show (${n})`;
  }
}

function toggleHigh(){
  const body = document.getElementById('high-section-body');
  const btn  = document.getElementById('high-toggle-btn');
  if(body.style.display==='none'){
    body.style.display='block';
    btn.textContent = '▴ Hide';
  } else {
    body.style.display='none';
    const n = DATA.alerts.filter(a=>a.tier==='high').length;
    btn.textContent = `▾ Show (${n})`;
  }
}

function toggleMedium(){
  const body = document.getElementById('medium-section');
  const btn  = document.getElementById('medium-toggle-btn');
  if(body.style.display==='none'){
    body.style.display='block';
    btn.textContent = '▴ Hide';
  } else {
    body.style.display='none';
    const n = DATA.alerts.filter(a=>a.tier==='medium').length;
    btn.textContent = `▾ Show (${n})`;
  }
}

// ── Charts ────────────────────────────────────────────────────────────────
// shared tooltip callback: show "N (X%)" for both doughnut and bar
function pctTooltip(total){
  return {
    callbacks:{
      label: ctx => {
        const v = ctx.raw;
        const pct = total > 0 ? (v/total*100).toFixed(1) : '0.0';
        return ` ${ctx.label||ctx.dataset.label}: ${v} (${pct}%)`;
      }
    }
  };
}

function doughnutPctTooltip(data){
  const total = data.reduce((s,v)=>s+v, 0);
  return {
    callbacks:{
      label: ctx => {
        const v = ctx.raw;
        const pct = total > 0 ? (v/total*100).toFixed(1) : '0.0';
        return ` ${ctx.label}: ${v} (${pct}%)`;
      }
    }
  };
}

// Trends — tooltip shows count per hazard type + % of that month's total
// Trends line chart — switchable between hazard / product
let _trendChart = null;

const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function fmtMonth(lbl){ // "2025-05" → "May 25"
  const [y,m] = (lbl||'').split('-');
  const idx = parseInt(m) - 1;
  return (MONTH_ABBR[idx] || lbl) + ' ' + (y||'').slice(2);
}

function buildTrendDatasets(trendData){
  const monthTotals = trendData.labels.map((_,i) =>
    trendData.datasets.reduce((s,ds) => s + (ds.data[i]||0), 0)
  );
  return {
    monthTotals,
    chartDatasets: [
      {
        type:'bar', label:'Total', data:monthTotals,
        backgroundColor:'rgba(180,190,210,0.35)',
        borderColor:'rgba(180,190,210,0.6)',
        borderWidth:1, order:2,
        datalabels:{
          display:true, anchor:'end', align:'top',
          font:{size:11, weight:'bold'}, color:'#5a6478',
          formatter: v => v
        }
      },
      ...trendData.datasets.map(ds => ({
        type:'line', label:ds.label.charAt(0).toUpperCase()+ds.label.slice(1), data:ds.data,
        borderColor:ds.color, backgroundColor:'transparent',
        borderWidth:2.5, pointRadius:3, pointHoverRadius:5,
        tension:0.3, fill:false, order:1,
        datalabels:{display:false}
      }))
    ]
  };
}

function switchTrendBreakdown(mode){
  const trendData = mode === 'hazard' ? DATA.trends : DATA.trends_product;
  const {monthTotals, chartDatasets} = buildTrendDatasets(trendData);

  // Update button styles
  document.getElementById('trend-btn-hazard').style.cssText =
    mode==='hazard'
      ? 'padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--israel);background:var(--israel);color:#fff;cursor:pointer;font-weight:600'
      : 'padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--border);background:#fff;color:var(--muted);cursor:pointer';
  document.getElementById('trend-btn-product').style.cssText =
    mode==='product'
      ? 'padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--israel);background:var(--israel);color:#fff;cursor:pointer;font-weight:600'
      : 'padding:4px 12px;font-size:12px;border-radius:4px;border:1.5px solid var(--border);background:#fff;color:var(--muted);cursor:pointer';

  if(_trendChart){
    _trendChart.data.labels   = trendData.labels.map(fmtMonth);
    _trendChart.data.datasets = chartDatasets;
    _trendChart.options.plugins.tooltip.itemSort =
      (a,b) => { if(a.dataset.label==='Total') return 1; if(b.dataset.label==='Total') return -1; return b.raw-a.raw; };
    _trendChart.options.plugins.tooltip.callbacks.footer =
      items => `Total: ${monthTotals[items[0].dataIndex]}`;
    _trendChart.update();
  }
}

(function(){
  const {monthTotals, chartDatasets} = buildTrendDatasets(DATA.trends);
  _trendChart = new Chart(document.getElementById('trendsLineChart'),{
    type:'bar',
    data:{ labels: DATA.trends.labels.map(fmtMonth), datasets: chartDatasets },
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false},
      scales:{
        x:{ticks:{font:{size:14}}, title:{display:true, text:'Month', color:'#5a6478', font:{size:12}}},
        y:{beginAtZero:true, ticks:{font:{size:14}}, title:{display:true, text:'Number of Alerts', color:'#5a6478', font:{size:12}}}
      },
      plugins:{
        legend:{position:'bottom', align:'start', labels:{boxWidth:14, font:{size:14}}},
        tooltip:{
          itemSort: (a,b) => {
            if(a.dataset.label==='Total') return 1;
            if(b.dataset.label==='Total') return -1;
            return b.raw - a.raw;
          },
          filter: item => item.dataset.label !== 'Total',
          callbacks:{footer: items => `Total: ${monthTotals[items[0].dataIndex]}`}
        }
      }
    }
  });
})();

// Hazard doughnut
(function(){
  const labels  = DATA.breakdowns.hazard_category.labels;
  const hData   = DATA.breakdowns.hazard_category.data;
  const hTotal  = hData.reduce((s,v)=>s+v,0);
  const COLORS  = ['#c0392b','#8e44ad','#e67e22','#2980b9','#16a085','#7f8c8d','#bdc3c7'];

  // Center total
  document.getElementById('hazard-total').textContent = hTotal;

  // Custom legend — same style as severity donut
  const legendEl = document.getElementById('hazard-legend');
  legendEl.innerHTML = labels.map((lbl,i) => {
    const color = COLORS[i % COLORS.length];
    return `<div class="ov-stat" style="cursor:default">
      <div class="ov-dot" style="background:${color}"></div>
      <div class="ov-label" style="width:90px;text-transform:capitalize">${lbl}</div>
      <div class="ov-val">${hData[i]}</div>
    </div>`;
  }).join('');

  _charts.hazard = new Chart(document.getElementById('hazardChart'),{
    type:'doughnut',
    data:{
      labels,
      datasets:[{data:hData, backgroundColor:COLORS, borderWidth:2, borderColor:'#fff'}]
    },
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:doughnutPctTooltip(hData),
        datalabels:{
          color:'#fff',
          font:{weight:'bold',size:11},
          formatter: v => {
            const pct = hTotal ? Math.round(v/hTotal*100) : 0;
            return pct >= 4 ? pct+'%' : '';
          }
        }
      }
    }
  });
})();

// Product bar
const productTotal = DATA.breakdowns.product_category.data.reduce((s,v)=>s+v,0);
function _truncLabel(s, maxWords) {
  const words = s.split(' ');
  if (words.length <= maxWords) return s;
  return words.slice(0, maxWords).join(' ') + '…';
}
Chart.register({
  id: 'fixYAxis',
  afterLayout(chart) { if (chart.scales && chart.scales.y) chart.scales.y.width = 200; }
});

function _barChartOpts(color, total, fullLabels) {
  return {indexAxis:'y', responsive:true, maintainAspectRatio:false,
    plugins:{
      legend:{display:false},
      tooltip:{
        callbacks:{
          title: items => fullLabels[items[0].dataIndex],
          label: item => ` ${item.raw}  (${total ? Math.round(item.raw/total*100) : 0}%)`
        }
      },
      datalabels:{
        anchor:'end', align:'end', clamp:true,
        color:'#5a6478', font:{size:14},
        formatter: v => v
      }
    },
    layout:{padding:{right:40}},
    scales:{
      x:{beginAtZero:true, ticks:{font:{size:14}},
         title:{display:true, text:'Number of Alerts', color:'#5a6478', font:{size:13}}},
      y:{ticks:{font:{size:14}, crossAlign:'far',
           callback:(val,i) => _truncLabel(fullLabels[i], 4)},
         title:{display:false}}
    }
  };
}

_charts.product = new Chart(document.getElementById('productChart'),{
  type:'bar',
  data:{
    labels: DATA.breakdowns.product_category.labels.map(l=>_truncLabel(l,4)),
    datasets:[{data:DATA.breakdowns.product_category.data,backgroundColor:'#3498db',label:'alerts'}]
  },
  options: _barChartOpts('#3498db', productTotal, DATA.breakdowns.product_category.labels)
});

// Country bar
const countryTotal = DATA.breakdowns.origin_country.data.reduce((s,v)=>s+v,0);
_charts.country = new Chart(document.getElementById('countryChart'),{
  type:'bar',
  data:{
    labels: DATA.breakdowns.origin_country.labels,
    datasets:[{data:DATA.breakdowns.origin_country.data,backgroundColor:'#e67e22',label:'alerts'}]
  },
  options: _barChartOpts('#e67e22', countryTotal, DATA.breakdowns.origin_country.labels)
});

// Source doughnut
(function(){
  const sourceLabels = DATA.breakdowns.source.labels.map(l=>l.charAt(0).toUpperCase()+l.slice(1));
  const sourceData   = DATA.breakdowns.source.data;
  const sourceColors = DATA.breakdowns.source.colors||['#003087','#007749','#9c1a1c','#f0b400'];
  const sourceTotal  = sourceData.reduce((s,v)=>s+v,0);

  document.getElementById('source-total').textContent = sourceTotal;

  const legendEl = document.getElementById('source-legend');
  legendEl.innerHTML = sourceLabels.map((lbl,i) =>
    `<div class="ov-stat" style="cursor:default">
      <div class="ov-dot" style="background:${sourceColors[i%sourceColors.length]}"></div>
      <div class="ov-label" style="width:auto;flex:1;font-size:12px">${lbl}</div>
      <div class="ov-val">${sourceData[i]}</div>
    </div>`
  ).join('');

  _charts.source = new Chart(document.getElementById('sourceChart'),{
    type:'doughnut',
    data:{
      labels: sourceLabels,
      datasets:[{data:sourceData, backgroundColor:sourceColors, borderWidth:2, borderColor:'#fff'}]
    },
    options:{responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:doughnutPctTooltip(sourceData),
        datalabels:{
          color:'#fff',
          font:{weight:'bold',size:11},
          formatter: v => {
            const pct = sourceTotal ? Math.round(v/sourceTotal*100) : 0;
            return pct >= 4 ? pct+'%' : '';
          }
        }
      }
    }
  });
})();

// ── Filter helpers ────────────────────────────────────────────────
function _topN(arr, keyFn, n) {
  const counts = {};
  arr.forEach(a => { const k = keyFn(a) || 'unclassified'; counts[k] = (counts[k]||0)+1; });
  return Object.entries(counts)
    .sort((a,b) => b[1]-a[1])
    .slice(0, n)
    .reduce((acc,[k,v]) => { acc.labels.push(k); acc.data.push(v); return acc; },
            {labels:[], data:[]});
}

function _updateBreakdowns(filtered) {
  const HAZARD_COLORS = ['#c0392b','#8e44ad','#e67e22','#2980b9','#16a085','#7f8c8d','#bdc3c7'];
  const SOURCE_COLORS = ['#003087','#007749','#9c1a1c','#f0b400'];

  // Hazard doughnut
  if (_charts.hazard) {
    const hd = _topN(filtered, a => a.hazard_category, 10);
    const hTotal = hd.data.reduce((s,v)=>s+v,0);
    _charts.hazard.data.labels = hd.labels;
    _charts.hazard.data.datasets[0].data = hd.data;
    _charts.hazard.data.datasets[0].backgroundColor = HAZARD_COLORS.slice(0, hd.labels.length);
    _charts.hazard.options.plugins.datalabels.formatter = v => {
      const pct = hTotal ? Math.round(v/hTotal*100) : 0;
      return pct >= 4 ? pct+'%' : '';
    };
    _charts.hazard.update();
    document.getElementById('hazard-total').textContent = hTotal;
    const legendEl = document.getElementById('hazard-legend');
    if (legendEl) legendEl.innerHTML = hd.labels.map((lbl,i) =>
      `<div class="ov-stat" style="cursor:default">
        <div class="ov-dot" style="background:${HAZARD_COLORS[i%HAZARD_COLORS.length]}"></div>
        <div class="ov-label" style="width:90px;text-transform:capitalize">${lbl}</div>
        <div class="ov-val">${hd.data[i]}</div>
      </div>`).join('');
  }

  // Product bar
  if (_charts.product) {
    const pd = _topN(filtered, a => a.product_category, 10);
    const pdTotal = pd.data.reduce((s,v)=>s+v,0);
    _charts.product.data.labels = pd.labels.map(l=>_truncLabel(l,4));
    _charts.product.data.datasets[0].data = pd.data;
    _charts.product.options.plugins.tooltip.callbacks.title = items => pd.labels[items[0].dataIndex];
    _charts.product.options.plugins.tooltip.callbacks.label = item => ` ${item.raw}  (${pdTotal ? Math.round(item.raw/pdTotal*100) : 0}%)`;
    _charts.product.options.scales.y.ticks.callback = (val,i) => _truncLabel(pd.labels[i]||'',4);
    _charts.product.update();
  }

  // Country bar
  if (_charts.country) {
    const cd = _topN(filtered, a => a.origin_country, 10);
    const cdTotal = cd.data.reduce((s,v)=>s+v,0);
    _charts.country.data.labels = cd.labels;
    _charts.country.data.datasets[0].data = cd.data;
    _charts.country.options.plugins.tooltip.callbacks.label = item => ` ${item.raw}  (${cdTotal ? Math.round(item.raw/cdTotal*100) : 0}%)`;
    _charts.country.update();
  }

  // Source doughnut
  if (_charts.source) {
    const sd = _topN(filtered, a => a.source_id, 10);
    const sTotal = sd.data.reduce((s,v)=>s+v,0);
    const srcLabels = sd.labels.map(l=>l.charAt(0).toUpperCase()+l.slice(1));
    _charts.source.data.labels = srcLabels;
    _charts.source.data.datasets[0].data = sd.data;
    _charts.source.data.datasets[0].backgroundColor = SOURCE_COLORS.slice(0, sd.labels.length);
    _charts.source.options.plugins.datalabels.formatter = v => {
      const pct = sTotal ? Math.round(v/sTotal*100) : 0;
      return pct >= 4 ? pct+'%' : '';
    };
    _charts.source.update();
    document.getElementById('source-total').textContent = sTotal;
    const legendEl = document.getElementById('source-legend');
    if (legendEl) legendEl.innerHTML = srcLabels.map((lbl,i) =>
      `<div class="ov-stat" style="cursor:default">
        <div class="ov-dot" style="background:${SOURCE_COLORS[i%SOURCE_COLORS.length]}"></div>
        <div class="ov-label" style="width:auto;flex:1;font-size:12px">${lbl}</div>
        <div class="ov-val">${sd.data[i]}</div>
      </div>`).join('');
  }
}

// ── Floating TOC ──────────────────────────────────────────────────
(function(){
  const TOC_SECTIONS = [
    'toc-overview','toc-trends','toc-israel','toc-critical',
    'toc-high','toc-medium','toc-breakdowns','toc-about'
  ];
  const tocLinks = Array.from(document.querySelectorAll('#floating-toc li a'));

  function tocScrollTo(id, toggleMed){
    if(toggleMed){
      const sec = document.getElementById('medium-section');
      if(sec && getComputedStyle(sec).display === 'none') toggleMedium();
    }
    function doScroll(){
      const el = document.getElementById(id);
      if(!el) return;
      const headerH = document.querySelector('.header').offsetHeight;
      const top = el.getBoundingClientRect().top + window.scrollY - headerH - 16;
      window.scrollTo({top, behavior:'smooth'});
    }
    // Wait one frame so display:block takes effect before measuring position
    requestAnimationFrame(doScroll);
  }
  window.tocScrollTo = tocScrollTo;

  // Scroll spy: highlight which section is currently in view
  let currentActive = null;
  function updateActive(){
    const headerH = document.querySelector('.header').offsetHeight;
    const scrollY = window.scrollY + headerH + 32;
    let active = TOC_SECTIONS[0];
    for(const id of TOC_SECTIONS){
      const el = document.getElementById(id);
      if(!el) continue;
      if(getComputedStyle(el).display === 'none') continue;
      if(el.getBoundingClientRect().top + window.scrollY <= scrollY) active = id;
    }
    if(active !== currentActive){
      currentActive = active;
      tocLinks.forEach((a, i) => {
        const id = TOC_SECTIONS[i];
        a.classList.toggle('toc-active', id === active);
      });
    }
  }
  window.addEventListener('scroll', updateActive, {passive:true});
  updateActive();
})();

// Deep-link: open and scroll to a specific alert via URL hash (#alert-xxx)
function openAlertFromHash() {
  const hash = window.location.hash;
  if (!hash) return;
  const alertId = hash.slice(1);

  // Expand all tier sections so the target card is in the DOM
  ['critical-more-btn','high-more-btn','medium-more-btn'].forEach(btnId => {
    const btn = document.getElementById(btnId);
    if (btn && btn.style.display !== 'none') btn.click();
  });
  // Reveal collapsed bodies if hidden
  const critBody = document.getElementById('critical-section-body');
  if (critBody && critBody.style.display === 'none') toggleCritical();
  const highBody = document.getElementById('high-section-body');
  if (highBody && highBody.style.display === 'none') toggleHigh();
  const medSec = document.getElementById('medium-section');
  if (medSec && (medSec.style.display === 'none' || !medSec.style.display)) toggleMedium();

  const el = document.getElementById(alertId);
  if (!el) return;
  el.setAttribute('open', '');
  requestAnimationFrame(() => {
    const headerH = document.querySelector('.header').offsetHeight;
    const top = el.getBoundingClientRect().top + window.scrollY - headerH - 16;
    window.scrollTo({top, behavior:'smooth'});
    el.style.outline = '2px solid #2471a3';
    setTimeout(() => el.style.outline = '', 3000);
  });
}
  window.addEventListener('hashchange', openAlertFromHash);

function checkExportDays(input) {
  const val = parseInt(input.value);
  const maxDays = DATA.meta.window_days;
  const warn = document.getElementById('toc-days-warning');
  if (val > maxDays) {
    warn.textContent = `⚠️ Max available: ${maxDays}d`;
    warn.style.display = 'block';
    input.style.borderColor = '#e67e22';
  } else {
    warn.style.display = 'none';
    input.style.borderColor = '';
  }
}

function toggleTierDropdown(e) {
  e.stopPropagation();
  const panel = document.getElementById('tier-dd-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('#tier-dd-panel')) {
    const p = document.getElementById('tier-dd-panel');
    if (p) p.style.display = 'none';
  }
});
function tierAllChanged(allBox) {
  ['toc-exp-crit','toc-exp-high','toc-exp-med','toc-exp-low'].forEach(id => {
    document.getElementById(id).checked = allBox.checked;
  });
  updateTierLabel();
}
function tierChanged() {
  const ids = ['toc-exp-crit','toc-exp-high','toc-exp-med','toc-exp-low'];
  const all = ids.every(id => document.getElementById(id).checked);
  document.getElementById('toc-exp-all').checked = all;
  updateTierLabel();
}
function updateTierLabel() {
  const map = {
    'toc-exp-crit': 'Crit', 'toc-exp-high': 'High',
    'toc-exp-med': 'Med', 'toc-exp-low': 'Low'
  };
  const selected = Object.entries(map)
    .filter(([id]) => document.getElementById(id).checked)
    .map(([, label]) => label);
  const lbl = document.getElementById('tier-dd-label');
  if (!selected.length) lbl.textContent = 'None';
  else if (selected.length === 4) lbl.textContent = 'All tiers';
  else lbl.textContent = selected.join(', ');
}

function tocExportCSV() { exportCSV(); }

function exportCSV() {
  const days     = parseInt(document.getElementById('toc-export-days').value) || 30;
  const wantCrit = document.getElementById('toc-exp-crit').checked;
  const wantHigh = document.getElementById('toc-exp-high').checked;
  const wantMed  = document.getElementById('toc-exp-med').checked;
  const wantLow  = document.getElementById('toc-exp-low').checked;

  const cutoff = new Date(DATA.meta.ref_date);
  cutoff.setDate(cutoff.getDate() - days);
  const cutoffStr = cutoff.toISOString().slice(0, 10);

  const tiers = new Set();
  if (wantCrit) tiers.add('critical');
  if (wantHigh) tiers.add('high');
  if (wantMed)  tiers.add('medium');
  if (wantLow)  tiers.add('low');

  const mainRows = DATA.alerts.filter(a =>
    a.source_published_date >= cutoffStr && tiers.has(a.tier)
  );
  const lowRows = wantLow
    ? (DATA.low_alerts || []).filter(a => a.source_published_date >= cutoffStr)
    : [];
  const rows = [...mainRows, ...lowRows].sort((a, b) =>
    (b.absolute_score || 0) - (a.absolute_score || 0)
  );

  if (!rows.length) {
    document.getElementById('toc-export-status').textContent = 'No alerts found for selected filters.';
    return;
  }

  const cols = [
    'source_published_date','tier','absolute_score','title','source_id',
    'hazard_specific','hazard_category','severity_normalized',
    'origin_country','distribution_countries','israel_relevance_flag','record_url'
  ];

  const esc = v => {
    if (v === null || v === undefined) return '';
    const s = Array.isArray(v) ? v.join('; ') : String(v);
    return s.includes(',') || s.includes('"') || s.includes('\\n')
      ? '"' + s.replace(/"/g, '""') + '"' : s;
  };

  const lines = [cols.join(',')];
  for (const a of rows) {
    lines.push([
      esc(a.source_published_date),
      esc(a.tier),
      esc(a.absolute_score ? a.absolute_score.toFixed(1) : ''),
      esc(a.title || a.alert_id),
      esc(a.source_id),
      esc(a.hazard_specific),
      esc(a.hazard_category),
      esc(a.severity_normalized),
      esc(a.origin_country),
      esc(a.distribution_countries),
      esc(a.israel_relevance_flag ? 'Yes' : ''),
      esc(a.record_url),
    ].join(','));
  }

  const blob = new Blob(['﻿' + lines.join('\n')], {type: 'text/csv;charset=utf-8'});
  const url  = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `foodsafe_alerts_${DATA.meta.ref_date}_last${days}d.csv`;
  link.click();
  URL.revokeObjectURL(url);

  document.getElementById('toc-export-status').textContent = `✓ Exported ${rows.length} alerts`;
}
// Twemoji — replace emoji with SVG images for consistent rendering on all platforms (incl. Windows)
if (typeof twemoji !== 'undefined') {
  twemoji.parse(document.body, { folder: 'svg', ext: '.svg' });
  // Re-parse dynamically rendered cards when feed re-renders
  const _origRenderCard = renderCard;
  // observe DOM mutations in feed containers to re-parse new emoji
  const _twObserver = new MutationObserver(() => {
    twemoji.parse(document.body, { folder: 'svg', ext: '.svg' });
  });
  ['critical-feed','high-feed','medium-feed','israel-section'].forEach(id => {
    const el = document.getElementById(id);
    if (el) _twObserver.observe(el, { childList: true, subtree: true });
  });
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
