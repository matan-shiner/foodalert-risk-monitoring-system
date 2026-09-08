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
doesn't cover (product names, company names, rarer substances).

Three further mitigations, added after finding that neither bigger local
models (tested NLLB-200 at 600M and 1.3B — both hallucinate proper nouns
just as confidently as MarianMT, sometimes worse, e.g. turning "美团"
Meituan into "the United States") nor more decoding tricks meaningfully
close the gap on this kind of terse, code-mixed regulatory text:

1. Recurring entity names (e-commerce platforms, "Co., Ltd." suffixes)
   get substituted with their English form directly in the source text
   BEFORE translation, via _ENTITY_TERMS — MT models generally leave an
   already-English span alone rather than mistranslating it, which beats
   asking the model to translate "美团" itself (see `known_term_lookup`
   for the older, narrower exact-match dictionary this complements).
2. Dates and multi-segment reference codes (expiry dates, batch/product
   numbers like "74-1-171-5-50003") are stripped out before translation
   and reappended verbatim afterward — they're pure noise to a
   translation model and a frequent source of the "65: 65: 65:3"-style
   garbling seen in Thai records especially.
3. Long strings are split into sentences on native punctuation and
   translated one sentence at a time, then rejoined — small MT models
   handle single sentences noticeably better than run-on multi-clause
   paragraphs.
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

# Recurring entity names — e-commerce/delivery platforms and company-suffix
# words that show up constantly in these bulletins and that MT reliably
# mangles (e.g. NLLB rendered "美团" Meituan as "the United States";
# MarianMT has produced "Halo House Shop" for a mangled place name in the
# same sentence). Substituted directly into the source text before
# translation — unlike `known_term_lookup`, these match as substrings
# anywhere in a larger sentence, not just a whole standalone string.
# Longest keys first so multi-character platform names match before any
# shorter substring they might contain.
_ZH_ENTITY_TERMS: dict[str, str] = {
    "京东到家": "JD Daojia",
    "美团": "Meituan",
    "饿了么": "Ele.me",
    "淘宝": "Taobao",
    "天猫": "Tmall",
    "拼多多": "Pinduoduo",
    "京东": "JD.com",
    "抖音": "Douyin",
    "微信": "WeChat",
    "小红书": "Xiaohongshu",
    "手机APP": "mobile app",
    # Deliberately NOT substituting "有限公司"/"有限责任公司" ("Co., Ltd.") —
    # injecting a multi-word, punctuated English phrase mid-sentence
    # confused the model's word reordering worse than leaving it in
    # Chinese (verified: "Co., a dehydrated cucumber film produced by
    # Ltd. (whose operator is..."). Single-token proper nouns like
    # platform names substitute cleanly; punctuated phrases don't.
}

_JA_ENTITY_TERMS: dict[str, str] = {
    "株式会社": "Co., Ltd.",
    "有限会社": "Ltd.",
}

_TH_ENTITY_TERMS: dict[str, str] = {
    "บริษัท": "Company",
    "จำกัด": "Co., Ltd.",
}

_LANGUAGES = {
    "zh": {
        "model": "Helsinki-NLP/opus-mt-zh-en",
        "char_re": re.compile(r"[一-鿿]"),
        "terms": _ZH_TERMS,
        "entity_terms": _ZH_ENTITY_TERMS,
        # Chinese sentence-ending punctuation (full-width).
        "sentence_split_re": re.compile(r"(?<=[。！？])"),
    },
    "ja": {
        "model": "Helsinki-NLP/opus-mt-ja-en",
        # Hiragana/katakana/kanji — kanji-only overlaps CJK ideographs used
        # by Chinese too, but this module is always called knowing the
        # source language per-collector, so that ambiguity never matters.
        "char_re": re.compile(r"[぀-ゟ゠-ヿ一-鿿]"),
        "terms": _JA_TERMS,
        "entity_terms": _JA_ENTITY_TERMS,
        "sentence_split_re": re.compile(r"(?<=[。！？])"),
    },
    "th": {
        "model": "Helsinki-NLP/opus-mt-th-en",
        "char_re": re.compile(r"[฀-๿]"),
        "terms": _TH_TERMS,
        "entity_terms": _TH_ENTITY_TERMS,
        # Thai doesn't use a sentence-final punctuation mark, so there's
        # no reliable native split point — left unsegmented.
        "sentence_split_re": None,
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


def _substitute_entities(text: str, lang: str) -> str:
    """Replace known recurring entity names with their English form
    directly in the source text, longest match first, before translation."""
    terms = _LANGUAGES[lang]["entity_terms"]
    for k in sorted(terms, key=len, reverse=True):
        if k in text:
            text = text.replace(k, terms[k])
    return text


# Dates ("13/12/2568", "16/01/69", "2026-08-21") and multi-segment
# reference/batch codes ("74-1-171-5-50003", "12-1-088-0368" — 3+ numeric
# segments joined by '-' or '/'). Deliberately requires 3+ segments so a
# genuine short date (2 segments) isn't double-matched here, and so simple
# in-sentence numbers/ratios aren't touched.
_DATE_RE = re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b")
_REF_CODE_RE = re.compile(r"\b\d+(?:[-/]\d+){2,}\b")
_STRIP_RE = re.compile(f"(?:{_DATE_RE.pattern})|(?:{_REF_CODE_RE.pattern})")


def _extract_structured_tokens(text: str) -> tuple[str, list[str]]:
    """Pull dates/reference codes out of `text` so the MT model never has
    to translate them — it doesn't reproduce them faithfully (real example:
    "16/01/69 Exp:/01/71" mangled into "65: 65: 65:3" repeated). Returns
    (cleaned_text, [stripped_tokens_in_order]); the caller reappends them
    verbatim after translation."""
    tokens: list[str] = []

    def _sub(m: re.Match) -> str:
        tokens.append(m.group(0))
        return " "

    cleaned = _STRIP_RE.sub(_sub, text)
    cleaned = re.sub(r"[:,]\s*(?=[:,.]|$)", "", cleaned)  # dangling punctuation left behind
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, tokens


def _split_sentences(text: str, lang: str) -> list[str]:
    """Split into sentences on native punctuation so a small MT model
    translates one clause at a time instead of a whole run-on paragraph
    (no-op for languages without a reliable sentence-final mark, e.g. Thai)."""
    pattern = _LANGUAGES[lang]["sentence_split_re"]
    if not pattern:
        return [text]
    parts = [p.strip() for p in pattern.split(text) if p.strip()]
    return parts or [text]


def _preprocess(text: str, lang: str) -> tuple[list[str], list[str]]:
    """Full pipeline before handing text to the MT model: substitute known
    entities, strip structured tokens, split into sentences. Returns
    (sentences_to_translate, structured_tokens_to_reappend) — sentences is
    empty when the text was entirely structured tokens (e.g. just a date),
    so there's nothing left to run through the model at all."""
    substituted = _substitute_entities(text, lang)
    cleaned, tokens = _extract_structured_tokens(substituted)
    if not cleaned:
        return [], tokens
    return _split_sentences(cleaned, lang), tokens


def _reassemble(translated_sentences: list[str], tokens: list[str]) -> str:
    piece = " ".join(s for s in translated_sentences if s).strip()
    if tokens:
        piece = (piece + " [" + ", ".join(tokens) + "]").strip()
    return piece


@lru_cache(maxsize=4096)
def _translate_cached(text: str, lang: str) -> str:
    known = known_term_lookup(text, lang)
    if known:
        return known
    sentences, tokens = _preprocess(text, lang)
    if not sentences:
        return _reassemble([], tokens)
    model, tokenizer = _load_model(lang)
    batch = tokenizer(sentences, return_tensors="pt", padding=True, truncation=True)
    generated = model.generate(**batch, **_generate_kwargs())
    decoded = [_collapse_repeats(d.strip()) for d in tokenizer.batch_decode(generated, skip_special_tokens=True)]
    return _reassemble(decoded, tokens)


def translate_batch(texts: list[str], lang: str) -> list[str]:
    """Batch-translate `texts` (source language `lang`) to English. Much
    faster than translating one at a time for anything not already
    cached/dictionary-covered, since model inference batches efficiently.
    Each text is preprocessed independently (entity substitution, structured
    -token stripping, sentence splitting) but all resulting sentences across
    all texts are flattened into one model.generate() call for throughput,
    then regrouped back per original text."""
    results: list[str | None] = []
    to_translate: list[tuple[int, list[str], list[str]]] = []
    for i, text in enumerate(texts):
        if not text or not _has_text(text, lang):
            results.append(text)
            continue
        known = known_term_lookup(text, lang)
        if known:
            results.append(known)
            continue
        sentences, tokens = _preprocess(text, lang)
        results.append(None)
        to_translate.append((i, sentences, tokens))

    if to_translate:
        model, tokenizer = _load_model(lang)
        flat_sentences = [s for _, sents, _ in to_translate for s in sents]
        decoded: list[str] = []
        if flat_sentences:
            batch = tokenizer(flat_sentences, return_tensors="pt", padding=True, truncation=True)
            generated = model.generate(**batch, **_generate_kwargs())
            decoded = [_collapse_repeats(d.strip()) for d in tokenizer.batch_decode(generated, skip_special_tokens=True)]
        cursor = 0
        for idx, sentences, tokens in to_translate:
            n = len(sentences)
            results[idx] = _reassemble(decoded[cursor:cursor + n], tokens)
            cursor += n

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
