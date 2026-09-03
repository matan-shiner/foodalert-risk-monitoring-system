"""Local machine translation for collectors that need it (samr_china.py:
Chinese; caa_japan.py: Japanese; fda_thailand.py: Thai).

No paid translation API — uses small local Hugging Face models
(Helsinki-NLP/opus-mt-{zh,ja,th}-en, ~300MB each, auto-download on first
run the same way distilroberta-base already does for the Bi-Encoder). Runs
entirely on-device, no API key, no per-character billing.

Machine translation of short technical fragments (test-parameter names,
company names) is imperfect on its own — e.g. it renders Chinese "大肠菌群数"
("coliform count") as "the number of intestinal herds", and mistranslates
Japanese proper nouns like "成城石井" (a real supermarket chain) as "Ishii
Ishii". So a small curated per-language dictionary of recurring hazard
terms is checked first; MT is the fallback for everything the dictionary
doesn't cover (product names, company names, rarer substances). The
dictionary only needs to cover hazard *terminology* precisely —
product/company name translation is inherently approximate and MT is good
enough there.
"""
from __future__ import annotations
import re
from functools import lru_cache

# Recurring Chinese food-safety test-parameter / hazard-substance terms.
# Longest keys first so e.g. "高效氯氟氰菊酯" matches before "氯氟氰菊酯".
_ZH_TERMS: dict[str, str] = {
    "高效氯氟氰菊酯": "beta-cyfluthrin",
    "氯氟氰菊酯": "cyfluthrin",
    "糖精钠": "saccharin sodium",
    "甜蜜素": "cyclamate",
    "苯甲酸及其钠盐": "benzoic acid and its sodium salts",
    "苯甲酸": "benzoic acid",
    "山梨酸及其钾盐": "sorbic acid and its potassium salts",
    "山梨酸": "sorbic acid",
    "二氧化硫残留量": "sulfur dioxide residue",
    "二氧化硫": "sulfur dioxide",
    "大肠菌群数": "coliform count",
    "大肠菌群": "coliform",
    "菌落总数": "total plate count",
    "农药残留": "pesticide residue",
    "水胺硫磷": "isocarbophos",
    "过氧化值": "peroxide value",
    "酸价": "acid value",
    "苋菜红": "amaranth (dye)",
    "酸性红": "acid red (dye)",
    "全氮": "total nitrogen",
    "氨基酸态氮": "amino acid nitrogen",
    "不挥发酸": "non-volatile acid",
    "酒精度数": "alcohol content",
    "呋喃唑酮": "furazolidone",
    "氯霉素": "chloramphenicol",
    "黄曲霉毒素": "aflatoxin",
    "克伦特罗": "clenbuterol",
    "恩诺沙星": "enrofloxacin",
    "甲醛": "formaldehyde",
    "硼砂": "borax",
    "三聚氰胺": "melamine",
    "沙门氏菌": "salmonella",
    "金黄色葡萄球菌": "staphylococcus aureus",
    "志贺氏菌": "shigella",
    "诺如病毒": "norovirus",
    "致病性微生物": "pathogenic microorganism",
    "腐霉利": "procymidone",
    "毒死蜱": "chlorpyrifos",
    "克百威": "carbofuran",
    "噻虫嗪": "thiamethoxam",
    "氧乐果": "omethoate",
    "甲拌磷": "phorate",
    "铅": "lead",
    "镉": "cadmium",
    "汞": "mercury",
    "砷": "arsenic",
}

# Recurring Japanese food-safety hazard/allergen terms.
_JA_TERMS: dict[str, str] = {
    "腸管出血性大腸菌": "enterohemorrhagic E. coli (EHEC)",
    "黄色ブドウ球菌": "Staphylococcus aureus",
    "セレウス菌": "Bacillus cereus",
    "ボツリヌス菌": "Clostridium botulinum",
    "サルモネラ属菌": "Salmonella",
    "サルモネラ菌": "Salmonella",
    "サルモネラ": "Salmonella",
    "ノロウイルス": "norovirus",
    "リステリア菌": "Listeria",
    "リステリア": "Listeria",
    "ヒスタミン": "histamine",
    "カビ": "mold",
    "フグ": "pufferfish (fugu)",
    "酵母": "yeast",
    "破裂": "container rupture",
    "異物混入": "foreign object contamination",
    "表示欠落": "labeling omission",
    "誤表示": "mislabeling",
    "期限表示誤り": "incorrect date labeling",
    "賞味期限誤表示": "incorrect best-before date labeling",
    "消費期限誤表示": "incorrect use-by date labeling",
    "アレルゲン": "allergen",
    "食中毒": "food poisoning",
    "農薬": "pesticide",
    "カドミウム": "cadmium",
    "水銀": "mercury",
    "ヒ素": "arsenic",
    "鉛": "lead",
}

_TH_TERMS: dict[str, str] = {
    "แบคทีเรีย": "bacteria",
    "อีโคไล": "E. coli",
    "อี.โคไล": "E. coli",
    "โคลิฟอร์ม": "coliform",
    "ซาลโมเนลลา": "Salmonella",
    "เชื้อรา": "mold",
    "ยีสต์": "yeast",
    "จุลินทรีย์": "microorganism",
    "ซิลเดนาฟิล": "sildenafil",
    "ไซบูทรามีน": "sibutramine",
    "ยาแผนปัจจุบัน": "undeclared pharmaceutical substance",
    "สารกันบูด": "preservative",
    "วัตถุกันเสีย": "preservative",
    "ยาฆ่าแมลง": "pesticide",
    "ตะกั่ว": "lead",
    "ปรอท": "mercury",
    "แคดเมียม": "cadmium",
    "สเตียรอยด์": "steroid",
    "สิ่งแปลกปลอม": "foreign object",
    "เศษแก้ว": "glass fragment",
    "เศษโลหะ": "metal fragment",
    "เศษพลาสติก": "plastic fragment",
    "สารก่อภูมิแพ้": "allergen",
}

_LANGUAGES = {
    "zh": {
        "model": "Helsinki-NLP/opus-mt-zh-en",
        "char_re": re.compile(r"[一-鿿]"),
        "terms": _ZH_TERMS,
    },
    "ja": {
        "model": "Helsinki-NLP/opus-mt-ja-en",
        # Hiragana/katakana/kanji — kanji-only overlaps CJK ideographs used
        # by Chinese too, but this module is always called knowing the
        # source language per-collector, so that ambiguity never matters.
        "char_re": re.compile(r"[぀-ゟ゠-ヿ一-鿿]"),
        "terms": _JA_TERMS,
    },
    "th": {
        "model": "Helsinki-NLP/opus-mt-th-en",
        "char_re": re.compile(r"[฀-๿]"),
        "terms": _TH_TERMS,
    },
}

_models: dict[str, tuple] = {}


def _load_model(lang: str):
    if lang not in _models:
        from transformers import MarianMTModel, MarianTokenizer
        model_name = _LANGUAGES[lang]["model"]
        tokenizer = MarianTokenizer.from_pretrained(model_name)
        model = MarianMTModel.from_pretrained(model_name)
        _models[lang] = (model, tokenizer)
    return _models[lang]


def known_term_lookup(text: str, lang: str = "zh") -> str | None:
    """Exact-match a known hazard term, after stripping annotation
    parentheses (Chinese （...） / Japanese （...）)."""
    stripped = re.sub(r"[（(][^）)]*[）)]", "", text).strip()
    return _LANGUAGES[lang]["terms"].get(stripped)


def has_chinese(text: str) -> bool:
    return bool(_LANGUAGES["zh"]["char_re"].search(text or ""))


def has_japanese(text: str) -> bool:
    return bool(_LANGUAGES["ja"]["char_re"].search(text or ""))


def _has_text(text: str, lang: str) -> bool:
    return bool(_LANGUAGES[lang]["char_re"].search(text or ""))


def _generate_kwargs() -> dict:
    # Greedy decoding (the previous default) is prone to degenerate loops
    # ("cans, cans, cans, cans...") and fluent-sounding hallucination
    # ("I'm sorry, I'm sorry...") on the short, non-sentence-like fragments
    # these collectors feed it (product codes, table cells, legal
    # boilerplate) — content the model never saw in training. Beam search
    # with repetition controls substantially reduces both failure modes.
    return dict(
        max_new_tokens=128,
        num_beams=4,
        no_repeat_ngram_size=3,
        repetition_penalty=1.3,
        early_stopping=True,
    )


_WORD_RUN_RE = re.compile(r"\b(\S+)(?:[\s,.]+\1\b){2,}", re.IGNORECASE)


def _collapse_repeats(text: str) -> str:
    """Collapse a run of the same word/phrase repeated 3+ times in a row
    into a single occurrence — a safety net for degenerate output that
    survives even beam search + repetition penalty."""
    return _WORD_RUN_RE.sub(r"\1", text)


@lru_cache(maxsize=4096)
def _translate_cached(text: str, lang: str) -> str:
    known = known_term_lookup(text, lang)
    if known:
        return known
    model, tokenizer = _load_model(lang)
    batch = tokenizer([text], return_tensors="pt", padding=True, truncation=True)
    generated = model.generate(**batch, **_generate_kwargs())
    decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()
    return _collapse_repeats(decoded)


def translate_batch(texts: list[str], lang: str) -> list[str]:
    """Batch-translate `texts` (source language `lang`) to English. Much
    faster than translating one at a time for anything not already
    cached/dictionary-covered, since model inference batches efficiently."""
    results: list[str | None] = []
    to_translate: list[tuple[int, str]] = []
    for i, text in enumerate(texts):
        if not text or not _has_text(text, lang):
            results.append(text)
            continue
        known = known_term_lookup(text, lang)
        if known:
            results.append(known)
            continue
        results.append(None)
        to_translate.append((i, text))

    if to_translate:
        model, tokenizer = _load_model(lang)
        batch_texts = [t for _, t in to_translate]
        batch = tokenizer(batch_texts, return_tensors="pt", padding=True, truncation=True)
        generated = model.generate(**batch, **_generate_kwargs())
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
        for (idx, _original), translated in zip(to_translate, decoded):
            results[idx] = _collapse_repeats(translated.strip())

    return [r if r is not None else "" for r in results]


# Backward-compatible zh-specific wrappers (samr_china.py uses these).
def translate_zh_to_en(text: str) -> str:
    if not text or not has_chinese(text):
        return text
    return _translate_cached(text, "zh")


def translate_batch_zh_to_en(texts: list[str]) -> list[str]:
    return translate_batch(texts, "zh")


def translate_batch_ja_to_en(texts: list[str]) -> list[str]:
    return translate_batch(texts, "ja")
