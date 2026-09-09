"""Fallback GSFA mappings for sources whose OWN product-category field is
genuinely structured (a fixed taxonomy from the source's API, not text we
derived ourselves) — RASFF, CFIA, and FSIS. Used only when the primary
keyword classifier (gsfa_taxonomy.classify) finds nothing in the alert's
text, since keyword matching can be more precise (it can reach a deeper
GSFA code) when the text actually supports it; these coarse native
categories exist to recover coverage on the records where it can't.

Values of None mean the source's own category doesn't correspond to any
GSFA food-product code at all (animal feed, packaging, live animals,
pesticides, pet food) — deliberately left unmapped rather than forced into
a wrong bucket.
"""
from __future__ import annotations

RASFF_TO_GSFA: dict[str, str | None] = {
    "fruits and vegetables": "04.0",
    "nuts, nut products and seeds": "04.2",
    "poultry meat and poultry meat products": "08.0",
    "dietetic foods, food supplements and fortified foods": "13.6",
    "cereals and bakery products": "06.0",
    "herbs and spices": "12.2",
    "fish and fish products": "09.0",
    "meat and meat products (other than poultry)": "08.0",
    "other food product / mixed": "16.0",
    "feed materials": None,
    "confectionery": "05.0",
    "food contact materials": None,
    "cocoa and cocoa preparations, coffee and tea": "14.1.5",
    "milk and milk products": "01.0",
    "prepared dishes and snacks": "16.0",
    "bivalve molluscs and products thereof": "09.0",
    "crustaceans and products thereof": "09.0",
    "soups, broths, sauces and condiments": "12.5",
    "fats and oils": "02.0",
    "non-alcoholic beverages": "14.1",
    "pet food": None,
    "eggs and egg products": "10.0",
    "compound feeds": None,
    "ices and desserts": "03.0",
    "food additives and flavourings": None,
    "alcoholic beverages": "14.2",
    "animal by-products": None,
    "honey and royal jelly": "11.3",
    "cephalopods and products thereof": "09.0",
    "feed additives": None,
    "feed premixtures": None,
    "water for human consumption (other)": "14.1.1",
    "wine": "14.2.3",
    "natural mineral waters": "14.1.1.1",
    "gastropods": "09.0",
    "live animals": None,
    "plant protection products": None,
}

CFIA_TO_GSFA: dict[str, str | None] = {
    "candy, confectionary, snacks and sweeteners": "05.0",
    "nuts, grains, and seeds": "04.2",
    "other": "16.0",
    "dairy": "01.0",
    "processed": None,
    "multiple food items": "16.0",
    "frozen": None,
    "fresh": None,
    "condiments": "12.6",
    "herbs and spices": "12.2",
    "meat and poultry": "08.0",
    "grain products": "06.0",
    "fish and seafood": "09.0",
    "non-alcoholic": "14.1",
    "fruits and vegetables": "04.0",
    "bread": "07.1.1",
    "alcoholic": "14.2",
    "infant products": "13.1",
    "egg products": "10.2",
    "canned": None,
    "beverages": "14.0",
    "pasta": "06.4",
    "eggs": "10.1",
}

FSIS_TO_GSFA: dict[str, str | None] = {
    "meat & poultry": "08.0",
}

_SOURCE_MAPS: dict[str, dict[str, str | None]] = {
    "rasff": RASFF_TO_GSFA,
    "cfia_recalls": CFIA_TO_GSFA,
    "fsis": FSIS_TO_GSFA,
}


def structured_fallback(source_id: str, old_category: str | None) -> str | None:
    """Look up the GSFA code for a source's own structured category value.
    CFIA sometimes lists multiple categories joined by " - " for
    multi-product recalls (e.g. "Nuts, grains, and seeds - Candy,
    confectionary..."); just the first one is used as a reasonable
    single-category approximation."""
    if not old_category:
        return None
    src_map = _SOURCE_MAPS.get(source_id)
    if not src_map:
        return None
    key = old_category.strip().lower()
    if key in src_map:
        return src_map[key]
    first_part = key.split(" - ")[0].strip()
    return src_map.get(first_part)
