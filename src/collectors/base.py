"""Base class for source-specific collectors."""
from __future__ import annotations
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_retry_session(retries: int = 3, backoff: float = 1.5) -> requests.Session:
    """Return a requests Session that automatically retries on transient errors.

    Retries on: 429 (rate-limit), 500/502/503/504 (server errors).
    Backoff: 1.5s, 3s, 6s between attempts.
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def infer_product_category(text: str, keyword_map: dict[str, list[str]]) -> str | None:
    """First matching category from an ordered {category: [keywords]} map,
    checked most-specific-first by the caller's ordering (e.g. "seafood"
    before a generic "prepared dishes" catch-all, so a frozen squid dish
    doesn't fall through to the catch-all just because it's also frozen)."""
    for category, keywords in keyword_map.items():
        if any(kw in text for kw in keywords):
            return category
    return None


# English-language fallback, checked against each collector's own translated
# product_description/title when the native-language keyword match (which
# runs first and is more precise/reliable when it hits) comes up empty. The
# native-language dictionaries are necessarily small (curated by hand, one
# language at a time); this one covers common food-noun vocabulary broadly
# in the one language every collector already translates into, so it lifts
# recall a lot even though individual matches are less precise. Ordered
# most-specific-first, same convention as the native dictionaries.
ENGLISH_PRODUCT_CATEGORY_KW: dict[str, list[str]] = {
    "seafood and fish products": [
        "fish", "shrimp", "prawn", "crab", "squid", "octopus", "shellfish",
        "oyster", "clam", "mussel", "scallop", "salmon", "tuna", "cod",
        "mackerel", "sardine", "eel", "seaweed", "kelp", "roe", "caviar",
        "lobster", "abalone", "anchovy", "fish ball", "fish cake", "fish paste",
    ],
    "meat and poultry products": [
        "beef", "pork", "chicken", "poultry", "duck", "lamb", "mutton",
        "goat meat", "sausage", "ham", "bacon", "jerky", "salami",
        "meatball", "minced meat", "meat floss", "tripe", "offal",
    ],
    "dairy and eggs": [
        "milk", "cheese", "yogurt", "yoghurt", "butter", "cream", "dairy",
        "egg", "custard", "ice cream", "gelato", "whey", "condensed milk",
    ],
    "fruits and vegetables": [
        "vegetable", "fruit", "tomato", "cucumber", "onion", "garlic",
        "potato", "carrot", "cabbage", "lettuce", "spinach", "mushroom",
        "eggplant", "pumpkin", "squash", "radish", "ginger", "celery",
        "bean sprout", "apple", "banana", "orange", "mango", "grape",
        "melon", "watermelon", "strawberry", "pear", "peach", "lemon",
        "lime", "pineapple", "coconut", "durian", "lychee", "papaya",
        "kiwi", "plum", "cherry", "raisin", "bean curd", "tofu",
    ],
    "cereals and bakery products": [
        "bread", "rice", "noodle", "pasta", "flour", "cake", "cookie",
        "biscuit", "cracker", "bun", "dumpling", "pastry", "wheat",
        "dough", "pancake", "waffle", "cereal", "oat", "bagel", "bakery",
    ],
    "confectionery and snacks": [
        "candy", "chocolate", "sweets", "snack", "chips", "crisps", "gum",
        "dessert", "pudding", "jelly", "marshmallow", "toffee", "caramel",
        "wafer", "mooncake", "popcorn",
    ],
    "beverages": [
        "juice", "tea", "coffee", "wine", "beer", "liquor", "soda",
        "beverage", "cola", "smoothie", "cocktail", "spirits", "alcohol",
        "sake", "whisky", "whiskey", "vodka", "rum", "mineral water",
    ],
    "sauces, condiments and seasonings": [
        "sauce", "vinegar", "dressing", "seasoning", "ketchup",
        "mayonnaise", "mustard", "marinade", "chili sauce", "fish sauce",
        "oyster sauce", "curry paste", "miso", "soy sauce",
    ],
    "herbs and spices": [
        "cinnamon", "clove", "nutmeg", "cumin", "coriander", "basil",
        "oregano", "paprika", "turmeric", "saffron", "black pepper",
        "peppercorn", "chili powder", "spice",
    ],
    "dietary supplements": [
        "supplement", "vitamin", "capsule", "probiotic", "protein powder",
        "collagen", "health food", "tonic", "herbal remedy",
    ],
    "nuts, seeds and grains": [
        "peanut", "almond", "cashew", "walnut", "pistachio", "sesame",
        "sunflower seed", "soybean", "quinoa", "nuts",
    ],
    "oils and fats": [
        "olive oil", "vegetable oil", "cooking oil", "lard", "margarine",
    ],
    "prepared dishes and meals": [
        "soup", "salad", "ready meal", "frozen meal", "sandwich",
        "burger", "pizza", "bento", "curry", "stew", "casserole", "pie",
        "quiche", "instant noodle",
    ],
}
_ENGLISH_PRODUCT_CATEGORY_RE = {
    category: re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b", re.IGNORECASE)
    for category, keywords in ENGLISH_PRODUCT_CATEGORY_KW.items()
}


def infer_product_category_en(text: str) -> str | None:
    """Fallback English-text product-category match with word boundaries
    (so "oil" doesn't match inside "boiled", "gum" inside "legume", etc.) —
    see ENGLISH_PRODUCT_CATEGORY_KW for why this exists alongside the
    native-language dictionaries."""
    if not text:
        return None
    for category, pattern in _ENGLISH_PRODUCT_CATEGORY_RE.items():
        if pattern.search(text):
            return category
    return None


class BaseCollector(ABC):
    """A collector pulls raw records from one source and yields normalized alert dicts."""

    source_id: str = ""

    @abstractmethod
    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        """Yield raw records from the source as-is."""

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Map a raw source record onto the unified schema."""

    def collect(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        for raw in self.fetch_raw(since=since, limit=limit):
            try:
                yield self.normalize(raw)
            except Exception as e:
                # Skip malformed records, surface in pipeline log.
                yield {"_error": str(e), "_source_id": self.source_id, "_raw": raw}
