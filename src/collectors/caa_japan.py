"""Consumer Affairs Agency (消費者庁), Japan — food recall collector.

Endpoint (reverse-engineered — undocumented):
  Listing: POST https://www.recall.caa.go.jp/result/index.php
    form fields: screenkbn=01, category=1 (Food), viewCountdden=60 (page
    size — 60 is the site's own maximum), portarorder=2 (sort — newest
    first, confirmed by observation), actionorder=0, pagingHidden=<page#>
    (empty string for page 1). No session/cookie handshake required — a
    bare POST with these fields works even as the very first request.
  Detail: https://www.recall.caa.go.jp/result/detail.php?rcl=<11-digit-id>

Detail pages look AJAX-driven (empty `<span id="detailsN">` placeholders
+ Quill.js richtext widgets) but are actually fully server-rendered — each
field's content ships inline as a Quill Delta JSON literal inside a
`<script>` block:
    contentsText = '{\\"ops\\":[{\\"insert\\":\\"...text...\\"}]}';
with a double-escaping quirk (literal `\\\\n` for newlines, not `\\n`) that
needs unwinding before `json.loads` — see `_parse_quill_blocks`. No JS
execution or second request needed once you know this.

Four Quill blocks per page, in order: (1) contact info — skipped, (2)
response method — folded into description, (3) product identification
(structured "Label：Value" lines: 商品名/内容量/形態/JANコード/賞味期限/
販売地域/販売先/販売日/販売数量) — parsed, (4) remarks — contains the
actual detailed recall reason and, notably, sometimes a reported health
outcome (e.g. "1 person who consumed this developed throat swelling and
difficulty breathing") that no other source in this project reports as
cleanly. `_ILLNESS_RE` opportunistically extracts a reported-illness count
from this text.

364 records in the Food category as of 2026-08-31 (this project's
smallest per-source count, but each record is unusually rich — JAN
barcode, per-channel sales-quantity breakdown, actual health outcomes).

No formal severity/classification field exists — sixth source to hit the
documented Bi-Encoder score-suppression issue.

Product/company names and hazard text are in Japanese. Same policy as
samr_china.py: hazard_category comes from keyword-matching the ORIGINAL
Japanese text (reliable, translation-independent) via src/translation.py's
curated Japanese term dictionary where possible, falling back to the local
MarianMT ja-en model (no paid API) for names/text the dictionary doesn't
cover.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator

import requests

from .base import BaseCollector, make_retry_session
from ..translation import translate_batch_ja_to_en, known_term_lookup

BASE_URL = "https://www.recall.caa.go.jp"
LIST_URL = BASE_URL + "/result/index.php"
DETAIL_URL_TPL = BASE_URL + "/result/detail.php?rcl={rcl}&screenkbn=06"
FOOD_CATEGORY = "1"
PAGE_SIZE = 60
MAX_PAGES = 20

_ROW_RE = re.compile(
    r'category_\d">[^<]+</span></td>.*?'
    r'rcl=(\d+)&screenkbn=01"[^>]*>([^<]+)</a>.*?'
    r'result_list_post_date">([^<]+)</span></td>.*?'
    r'result_list_start_date">([^<]+)</span>',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<div class="detail_title">\s*<h3>(.*?)</h3>(?:.*?<p>(.*?)</p>)?', re.DOTALL)
_QUILL_RE = re.compile(r"contentsText = '(.*?)';", re.DOTALL)
_RESPONSE_START_RE = re.compile(r'対応開始日</span>\s*<span class="detail_text">\s*(.*?)\s*</span>', re.DOTALL)
_TITLE_FIRM_RE = re.compile(r'^(.+?)[「【]')
_ILLNESS_RE = re.compile(r'(\d+)\s*名.{0,15}?(?:発症|入院|症状)')

# Allergen checked first — "アレルゲン" co-occurs with "表示欠落" (labeling
# omission) far more often than not, and allergen is the more specific,
# actionable classification of the two.
_ALLERGEN_KW = ["アレルゲン", "アレルギー"]
_BIOLOGICAL_KW = [
    "腸管出血性大腸菌", "黄色ブドウ球菌", "ボツリヌス菌", "サルモネラ属菌",
    "サルモネラ菌", "サルモネラ", "ノロウイルス", "セレウス菌", "リステリア菌",
    "リステリア", "大腸菌", "カビ", "食中毒", "微生物", "菌数", "フグ",
    "酵母",
]
_CHEMICAL_KW = [
    "ヒスタミン", "農薬", "残留動物用医薬品", "カドミウム", "水銀", "ヒ素",
    "鉛", "食品添加物",
]
_PHYSICAL_KW = ["異物混入", "金属", "ガラス片", "プラスチック", "虫混入", "毛髪", "破裂"]
_LABELING_KW = ["表示欠落", "誤表示", "期限表示誤り", "賞味期限誤表示", "消費期限誤表示", "食品表示法違反"]
_NONCOMPLIANCE_KW = ["無許可", "未承認", "回収命令"]


class CAAJapanCollector(BaseCollector):
    source_id = "caa_japan"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        count = 0

        for page in range(1, MAX_PAGES + 1):
            data = {
                "screenkbn": "01",
                "category": FOOD_CATEGORY,
                "viewCountdden": str(PAGE_SIZE),
                "portarorder": "2",
                "actionorder": "0",
                "pagingHidden": "" if page == 1 else str(page),
            }
            try:
                r = session.post(LIST_URL, data=data, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"[caa_japan] listing page {page} failed, stopping: {e}")
                return
            rows = _ROW_RE.findall(r.text)
            if not rows:
                return

            stop = False
            for rcl, title_html, post_date_str, start_date_str in rows:
                post_date = _parse_jp_date(post_date_str)
                if since and post_date and post_date < since.replace(tzinfo=None):
                    stop = True
                    break

                detail_url = DETAIL_URL_TPL.format(rcl=rcl)
                try:
                    dr = session.get(detail_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                except requests.exceptions.RequestException as e:
                    # This site intermittently times out / rate-limits under
                    # sustained load — losing one record shouldn't crash a
                    # multi-hundred-record backfill (make_retry_session's own
                    # retries already ran and were exhausted by this point).
                    print(f"[caa_japan] skipping rcl={rcl}: {e}")
                    continue
                if dr.status_code != 200:
                    continue

                yield _build_raw(rcl, dr.text, detail_url, post_date_str, start_date_str)
                count += 1
                if limit and count >= limit:
                    return
            if stop:
                return

    def normalize(self, raw: dict) -> dict:
        fields = raw["fields"]
        translated = translate_batch_ja_to_en([
            raw["firm_ja"], fields.get("商品名", raw["title_ja"]), raw["reason_ja"],
        ])
        firm_en, product_en, reason_en = translated

        classify_text = f"{raw['title_ja']} {raw['subtitle_ja']} {raw['reason_ja']}"
        hazard_category = _infer_hazard_category(classify_text)
        hazard_specific_ja = _extract_hazard_specific(classify_text)
        hazard_specific_en = (translate_batch_ja_to_en([hazard_specific_ja])[0]
                               if hazard_specific_ja else None)

        illness_match = _ILLNESS_RE.search(raw["reason_ja"])
        illness_count = int(illness_match.group(1)) if illness_match else None

        return {
            "id": f"caa_japan::{raw['rcl']}",
            "source_id": self.source_id,
            "source_record_id": raw["rcl"],
            "fingerprint": _make_fingerprint(firm_en, product_en, "Japan"),
            "record_url": raw["detail_url"],
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": raw["post_date"],
            "event_initiation_date": raw["start_date"] or raw["post_date"],
            "event_status": None,  # not published as a formal open/closed status
            "origin_country": "Japan",
            "distribution_countries": json.dumps([fields.get("販売地域", "Japan")]
                                                   if fields.get("販売地域") == "全国" else ["Japan"]),
            "israel_relevance_flag": 0,  # domestic Japanese retail recalls — never Israel-relevant
            "recalling_firm": firm_en or None,
            "brand_names": json.dumps([]),
            "product_description": product_en or None,
            "product_category": None,
            "hazard_category": hazard_category,
            "hazard_specific": hazard_specific_en or None,
            # No formal classification scale exists here — left honestly null,
            # same convention as cdc_food_safety_rss.py / samr_china.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": illness_count,
            "title": f"{hazard_specific_en or 'Recall'} — {product_en}" if product_en else raw["title_ja"],
            "description": reason_en or None,
            "reason_for_recall": reason_en or None,
        }


def _build_raw(rcl: str, html: str, detail_url: str, post_date_str: str, start_date_str: str) -> dict:
    title_match = _TITLE_RE.search(html)
    title_ja = _strip_tags(title_match.group(1)) if title_match else ""
    subtitle_ja = _strip_tags(title_match.group(2)) if title_match and title_match.group(2) else ""

    quill_blocks = [_parse_quill_block(m) for m in _QUILL_RE.findall(html)]
    # Block 0 is always empty — it's the page's own `var contentsText = '';`
    # declaration, which the regex also (correctly) matches as a zero-length
    # capture. Real content starts at 1: contact(1), response method(2),
    # product ID(3), remarks(4). A trailing "site disclaimer" block is unused.
    product_id_text = quill_blocks[3] if len(quill_blocks) > 3 else ""
    reason_ja = quill_blocks[4] if len(quill_blocks) > 4 else ""

    fields = _parse_label_value_lines(product_id_text)
    response_start_match = _RESPONSE_START_RE.search(html)
    start_date = _parse_jp_date(_strip_tags(response_start_match.group(1))) if response_start_match else None

    firm_match = _TITLE_FIRM_RE.match(title_ja)
    firm_ja = firm_match.group(1).strip() if firm_match else title_ja

    return {
        "rcl": rcl,
        "detail_url": detail_url,
        "title_ja": title_ja,
        "subtitle_ja": subtitle_ja,
        "firm_ja": firm_ja,
        "fields": fields,
        "reason_ja": reason_ja or subtitle_ja,
        "post_date": (_parse_jp_date(post_date_str) or datetime.now(timezone.utc)).date().isoformat(),
        "start_date": start_date.date().isoformat() if start_date else None,
    }


def _parse_quill_block(raw_delta: str) -> str:
    """Unwind the site's double-escaped Quill Delta JSON and concatenate
    the plain-text `insert` runs. See module docstring."""
    cleaned = raw_delta.replace("\\\\", "\x00").replace('\\"', '"').replace("\\/", "/").replace("\x00", "\\")
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return ""
    return "".join(op.get("insert", "") for op in data.get("ops", []) if isinstance(op.get("insert"), str))


def _parse_label_value_lines(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.replace("​", "").replace("　", " ").strip()
        m = re.match(r"^([^\s：:]{2,10})\s*[：:]\s*(.+)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_jp_date(s: str) -> datetime | None:
    """'2026/08/31' or '2026年08月31日' → datetime."""
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _infer_hazard_category(text: str) -> str | None:
    if any(kw in text for kw in _ALLERGEN_KW):
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
    for kw in _LABELING_KW:
        if kw in text:
            return "fraud"
    for kw in _NONCOMPLIANCE_KW:
        if kw in text:
            return "regulatory"
    return None


def _extract_hazard_specific(text: str) -> str | None:
    """Derived from the same keyword lists _infer_hazard_category uses —
    see rasff.py's _extract_hazard_specific for why that matters."""
    for kw_list in (_ALLERGEN_KW, _BIOLOGICAL_KW, _CHEMICAL_KW, _PHYSICAL_KW, _LABELING_KW, _NONCOMPLIANCE_KW):
        for kw in kw_list:
            if kw in text:
                return kw
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
