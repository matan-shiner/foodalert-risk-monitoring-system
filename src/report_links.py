"""Build human-readable source labels and outbound links for alert reports."""
from __future__ import annotations
from urllib.parse import quote

SOURCE_LABELS: dict[str, str] = {
    "fda_enforcement": "FDA — Recall Enforcement (openFDA)",
    "fsis": "USDA FSIS",
    "fsa_uk": "FSA UK — Food Alerts",
    "cdc_food_safety_rss": "CDC — Food Safety (pre-classification announcement)",
    "cfia_recalls": "CFIA — Canadian Food Inspection Agency",
}

FDA_RECALLS_PORTAL = "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts"
FSIS_RECALLS_INDEX = "https://www.fsis.usda.gov/recalls"
FSA_ALERTS_INDEX = "https://www.food.gov.uk/news-alerts"
CDC_FOOD_SAFETY_INDEX = "https://www.cdc.gov/foodsafety"
CFIA_RECALLS_INDEX = "https://recalls-rappels.canada.ca/en"


def source_label(source_id: str) -> str:
    return SOURCE_LABELS.get(source_id, source_id)


def build_alert_links(
    source_id: str,
    source_record_id: str | None,
    record_url: str | None,
) -> list[dict[str, str]]:
    """Return [{label, href}, ...] for report HTML tables."""
    links: list[dict[str, str]] = []
    rid = (source_record_id or "").strip()

    if source_id == "fda_enforcement" and rid:
        links.append(
            {
                "label": "Google",
                "href": f"https://www.google.com/search?q={quote('FDA recall ' + rid)}",
            }
        )
        if record_url:
            links.append({"label": "openFDA", "href": record_url})
        links.append({"label": "FDA Recalls", "href": FDA_RECALLS_PORTAL})

    elif source_id == "fsis":
        if record_url:
            href = record_url.replace("http://", "https://")
            links.append({"label": "FSIS page", "href": href})
        if rid:
            links.append(
                {
                    "label": "Google",
                    "href": f"https://www.google.com/search?q={quote('FSIS recall ' + rid)}",
                }
            )
        links.append({"label": "USDA Recalls", "href": FSIS_RECALLS_INDEX})

    elif source_id == "fsa_uk":
        if record_url:
            links.append({"label": "FSA UK page", "href": record_url})
        links.append({"label": "FSA Alerts", "href": FSA_ALERTS_INDEX})

    elif source_id == "cdc_food_safety_rss":
        if record_url:
            links.append({"label": "Announcement", "href": record_url})
        links.append({"label": "CDC Food Safety", "href": CDC_FOOD_SAFETY_INDEX})

    elif source_id == "cfia_recalls":
        if record_url:
            links.append({"label": "CFIA notice", "href": record_url})
        links.append({"label": "CFIA Recalls", "href": CFIA_RECALLS_INDEX})

    elif record_url:
        links.append({"label": "Source", "href": record_url})

    return links
