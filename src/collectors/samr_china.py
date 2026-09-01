"""State Administration for Market Regulation (SAMR), China — food safety
sampling-inspection bulletin collector.

China doesn't publish FDA/RASFF-style one-notice-per-recall alerts. Instead
SAMR periodically (roughly 2-3x/month) publishes a numbered bulletin
("市场监管总局办公厅关于N批次食品抽检不合格情况的通报" — "Announcement on N
non-compliant batches found in food sampling") bundling ~20-50 individual
non-compliant-product findings. This collector treats each individual
finding within a bulletin as one alert record (not the bulletin as a
whole) — this is the FDA-weekly-enforcement-report pattern, one URL
bundling many recalls.

Two bulletin templates exist and both must be handled:
  - Older/most bulletins group findings under hazard-category section
    headers: 食品添加剂超范围超限量使用问题 (additive misuse), 微生物污染问题
    (microbial contamination), 农药残留超标问题 (pesticide residue
    exceedance), 质量指标不达标问题 (quality-standard failure), 质量指标与
    标签标示值不符问题 (label mismatch) — `_SECTION_CATEGORY_MAP` maps these
    directly to hazard_category, no keyword guessing needed.
  - A newer flat-list template (first observed 2026-08, bulletins issued
    by "市场监管总局办公厅" with no section headers at all — found the hard
    way: an earlier version of this collector required a section header
    before accepting any entry, so it silently yielded ZERO records for
    every bulletin using this template, which happened to be most of the
    two most recent years' worth) — for these, `_infer_hazard_category_from_zh`
    keyword-matches the Chinese substance text directly as a fallback.
  Findings also don't always include a "标称...生产的" manufacturer clause
  (some just say "{retailer}销售的{product}") — `_ENTRY_FIELD_SIMPLE_RE` is
  the fallback extractor for those, using the retailer name as the only
  available firm identifier.

Endpoints (reverse-engineered — none are documented):
  Bulletin index: https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/spcjs/index.html
    Rendered client-side via a CMS "unit build" API:
    GET https://www.samr.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit
        ?webId=29e9522dc89d4e088a953d8cede72f4c
        &pageId=c50b450d9afa44e2a7f83f0c2a0c5ffc
        &parseType=bulidstatic&pageType=column&tagId=内容区域
        &tplSetId=5c30fb89ae5e48b9aefe3cdf49853830
        &paramJson={"pageNo": N, "pageSize": M}
    (found by reading the page's own /cms_files/default/script/ajax/page/page.js —
    pagination params must be nested inside a JSON-encoded `paramJson` field,
    not passed as bare query params, or the API silently ignores them and
    always returns page 1.)
  Bulletin detail: plain HTML, individual findings live inside
    `<div class="Three_xilan_07">` as a flat sequence of `<p>` tags — no
    table markup, but a very consistent sentence template per finding:
    "{retailer}销售的、标称{manufacturer}生产的{product}，其中{substance}
    不符合{standard}规定。" Section headers ("一、食品添加剂...问题") and
    per-finding numbering ("（一）", "（二）", ...) are both plain `<p>`
    text, not semantic markup, so section membership is recovered by
    scanning paragraphs in order.

224 bulletins found in the index as of 2026-08-31, reaching back to at
least 2018 — full historical depth is available, unlike sfa_singapore.py's
~2-year rolling window.

No formal severity/classification field exists (bulletins are structured
by hazard type, not severity) — fifth source to hit the documented
Bi-Encoder score-suppression issue.

Product/company names and full finding text are translated to English via
src/translation.py (local model, no paid API — see that module's docstring
for why hazard_category comes from the section header directly rather than
from translated/keyword-matched text: it's more reliable and doesn't
depend on translation quality at all).
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator

from .base import BaseCollector, make_retry_session
from ..translation import translate_batch_zh_to_en, known_term_lookup

BASE_URL = "https://www.samr.gov.cn"
INDEX_API_URL = BASE_URL + "/api-gateway/jpaas-publish-server/front/page/build/unit"
INDEX_API_PARAMS = {
    "webId": "29e9522dc89d4e088a953d8cede72f4c",
    "pageId": "c50b450d9afa44e2a7f83f0c2a0c5ffc",
    "parseType": "bulidstatic",
    "pageType": "column",
    "tagId": "内容区域",
    "tplSetId": "5c30fb89ae5e48b9aefe3cdf49853830",
}
PAGE_SIZE = 100
MAX_PAGES = 20

_LISTING_ITEM_RE = re.compile(
    r'nav04Left02_content">\s*<a href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'nav04Left02_contenttime">([^<]+)</li>',
    re.DOTALL,
)
_CONTENT_DIV_RE = re.compile(r'<div class="Three_xilan_07">(.*?)</div>', re.DOTALL)
_PARA_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL)
_SECTION_RE = re.compile(r'^[一二三四五六七八九十]+、(.+?问题)$')
_ENTRY_START_RE = re.compile(r'^（[一二三四五六七八九十]+）')
_ENTRY_FIELD_RE = re.compile(r'标称(.+?)(?:生产|出品)的、?(.+?)，其中(.+?)不符合(.+?)[。；]')
# Fallback for entries with no "标称...生产的" manufacturer clause at all —
# common in the newer flat-list bulletin template (see module docstring on
# _SECTION_RE below): "{retailer}销售的{product}，其中{substance}不符合...".
_ENTRY_FIELD_SIMPLE_RE = re.compile(r'^(.*?)销售的(.+?)，其中(.+?)不符合(.+?)[。；]')
_BATCH_COUNT_RE = re.compile(r'关于(\d+)批次')

# Chinese hazard-substance keywords, used as a classification fallback when
# a bulletin has no hazard-category section headers at all — a newer SAMR
# template (first seen 2026-08, no "一、食品添加剂..." style headers, just
# a flat numbered list) that _SECTION_CATEGORY_MAP can't cover. Longest/most
# specific terms first isn't required here since these are independent checks,
# not a single longest-match lookup.
_ZH_BIOLOGICAL_KW = [
    "菌落总数", "大肠菌群", "霉菌", "酵母菌", "沙门氏菌", "志贺氏菌",
    "单核细胞增生李斯特氏菌", "金黄色葡萄球菌", "蜡样芽孢杆菌", "铜绿假单胞菌",
    "乳酸菌数",
]
_ZH_CHEMICAL_KW = [
    "糖精钠", "甜蜜素", "苯甲酸", "山梨酸", "二氧化硫", "柠檬黄", "日落黄",
    "亮蓝", "诱惑红", "胭脂红", "脱氢乙酸", "农药残留", "兽药残留", "铅",
    "镉", "汞", "砷", "溴酸盐", "亚硝酸盐", "苋菜红", "酸性红",
]
_ZH_REGULATORY_KW = [
    "水分含量", "蛋白质含量", "酸价", "过氧化值", "界限指标", "维生素",
    "全氮", "氨基酸态氮", "不挥发酸", "酒精度数",
]
_ZH_FRAUD_KW = ["标签标示", "标示值不符"]

# Only "不达标"/label-mismatch sections aren't inherently a safety hazard —
# additive/microbial/pesticide sections are. Fraud fits label mismatch;
# regulatory fits generic quality-standard shortfalls that aren't a named
# hazard (e.g. a product's own peroxide value spec, not a national safety limit).
_SECTION_CATEGORY_MAP = {
    "食品添加剂超范围超限量使用问题": "chemical",
    "微生物污染问题": "biological",
    "农药残留超标问题": "chemical",
    "质量指标不达标问题": "regulatory",
    "质量指标与标签标示值不符问题": "fraud",
    "其他污染物污染问题": "chemical",
    "有机污染物问题": "chemical",
    "重金属污染问题": "chemical",
    "兽药残留超标问题": "chemical",
    "生物毒素污染问题": "biological",
}


class SAMRChinaCollector(BaseCollector):
    source_id = "samr_china"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        count = 0

        for page_no in range(1, MAX_PAGES + 1):
            params = dict(INDEX_API_PARAMS)
            params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": PAGE_SIZE})
            r = session.get(INDEX_API_URL, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.json().get("data", {}).get("html", "")
            items = _LISTING_ITEM_RE.findall(html)
            if not items:
                return

            stop = False
            for href, title_html, date_str in items:
                title = _strip_tags(title_html)
                if "批次食品抽检不合格情况" not in title:
                    continue  # skip non-bulletin announcements mixed into the same column

                pub_date = _parse_cn_date(date_str)
                if since and pub_date and pub_date < since.replace(tzinfo=None):
                    stop = True
                    break

                detail_url = href if href.startswith("http") else BASE_URL + href
                dr = session.get(detail_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                if dr.status_code != 200:
                    continue

                bulletin_entries = list(_parse_bulletin(
                    dr.text, title, detail_url,
                    pub_date.date().isoformat() if pub_date else None,
                ))
                if not bulletin_entries:
                    continue

                # Translate every field across the whole bulletin in one batch
                # call rather than 3-at-a-time per entry — a bulletin has
                # ~20-50 findings, so this cuts model.generate() call count
                # (the actual bottleneck, not raw token count) by the same
                # factor. See src/translation.py docstring for why MT is
                # used at all instead of a paid API.
                fields = ["manufacturer_zh", "product_zh", "substance_zh"]
                flat_texts = [e[f] for e in bulletin_entries for f in fields]
                translated = translate_batch_zh_to_en(flat_texts)
                for i, entry in enumerate(bulletin_entries):
                    base = i * len(fields)
                    entry["manufacturer_en"] = translated[base]
                    entry["product_en"] = translated[base + 1]
                    entry["substance_en"] = translated[base + 2]

                for entry in bulletin_entries:
                    yield entry
                    count += 1
                    if limit and count >= limit:
                        return
            if stop:
                return

    def normalize(self, raw: dict) -> dict:
        manufacturer_en = raw["manufacturer_en"]
        product_en = raw["product_en"]
        substance_en = raw["substance_en"]

        record_id = f"{raw['bulletin_ref']}::{raw['entry_index']}"

        hazard_category = _SECTION_CATEGORY_MAP.get(raw["section"])
        if hazard_category is None:
            hazard_category = _infer_hazard_category_from_zh(raw["substance_zh"])

        return {
            "id": f"samr_china::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(manufacturer_en, product_en, "China"),
            "record_url": raw["detail_url"],
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": raw["pub_date"],
            "event_initiation_date": raw["pub_date"],
            "event_status": None,  # SAMR doesn't publish a formal open/closed status
            "origin_country": "China",
            "distribution_countries": json.dumps(["China"]),
            "israel_relevance_flag": 0,  # domestic Chinese retail sampling — never Israel-relevant
            "recalling_firm": manufacturer_en or None,
            "brand_names": json.dumps([]),
            "product_description": product_en or None,
            "product_category": None,
            "hazard_category": hazard_category,
            "hazard_specific": substance_en or None,
            # No formal classification scale exists here — left honestly null,
            # same convention as cdc_food_safety_rss.py / sfa_singapore.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": f"{substance_en} found in {product_en}" if substance_en and product_en else raw["bulletin_title"],
            "description": f"{manufacturer_en} — {product_en}: {substance_en} did not meet standard. "
                            f"(Machine-translated from Chinese; original: {raw['entry_zh']})",
            "reason_for_recall": substance_en or None,
        }


def _parse_bulletin(html: str, bulletin_title: str, detail_url: str,
                     pub_date: str | None) -> Iterator[dict]:
    m = _CONTENT_DIV_RE.search(html)
    if not m:
        return
    content = m.group(1)
    paras = [_strip_tags(p) for p in _PARA_RE.findall(content)]
    paras = [p for p in paras if p]

    # Derived from the detail URL, not parsed bulletin text — the internal
    # reference number's format isn't stable ("〔2025年 第2号〕" for older
    # bulletins, "市监食检发〔2026〕103号" for the newer flat-list template,
    # possibly others). Falling back to `bulletin_title` on parse failure
    # was a real bug: multiple distinct bulletins share an identical title
    # (e.g. two separate "45批次" bulletins from different months), so their
    # entries collided on the same record_id and silently overwrote each
    # other via upsert. The URL's own unique hash segment never collides.
    url_match = re.search(r'art_([0-9a-f]+)\.html', detail_url)
    bulletin_ref = url_match.group(1) if url_match else detail_url

    # Section headers are absent entirely in a newer flat-list bulletin
    # template (see _ZH_*_KW comment) — `section` staying None throughout
    # is valid and handled downstream via keyword fallback, not skipped.
    section = None
    entry_index = 0
    for p in paras:
        sec_match = _SECTION_RE.match(p)
        if sec_match:
            section = sec_match.group(1)
            continue
        if _ENTRY_START_RE.match(p):
            entry_text = _ENTRY_START_RE.sub("", p)
        elif "销售" in p and "，其中" in p:
            entry_text = p  # standalone entry, no （一） numbering
        else:
            continue

        field_match = _ENTRY_FIELD_RE.search(entry_text)
        if field_match:
            manufacturer_zh = _clean_manufacturer(field_match.group(1))
            product_zh = field_match.group(2).strip("、 ")
            substance_zh = _clean_substance(field_match.group(3))
        else:
            simple_match = _ENTRY_FIELD_SIMPLE_RE.search(entry_text)
            if not simple_match:
                continue
            # No "标称...生产的" clause — fall back to the retailer name
            # (text before "销售的") as the only firm identifier available.
            manufacturer_zh = _clean_manufacturer(simple_match.group(1))
            product_zh = simple_match.group(2).strip("、 ")
            substance_zh = _clean_substance(simple_match.group(3))

        entry_index += 1
        yield {
            "bulletin_title": bulletin_title,
            "bulletin_ref": bulletin_ref,
            "detail_url": detail_url,
            "pub_date": pub_date,
            "section": section,
            "entry_index": entry_index,
            "entry_zh": entry_text,
            "manufacturer_zh": manufacturer_zh,
            "product_zh": product_zh,
            "substance_zh": substance_zh,
        }


def _infer_hazard_category_from_zh(substance_zh: str) -> str | None:
    """Fallback for bulletins with no hazard-category section header (see
    module docstring) — keyword-matches the ORIGINAL Chinese substance
    text, same rationale as matching on section names: translation quality
    should never gate classification."""
    for kw in _ZH_BIOLOGICAL_KW:
        if kw in substance_zh:
            return "biological"
    for kw in _ZH_CHEMICAL_KW:
        if kw in substance_zh:
            return "chemical"
    for kw in _ZH_FRAUD_KW:
        if kw in substance_zh:
            return "fraud"
    for kw in _ZH_REGULATORY_KW:
        if kw in substance_zh:
            return "regulatory"
    return None


def _clean_manufacturer(text: str) -> str:
    """Manufacturer clauses sometimes chain "商标授权许可的、X生产的" or
    "委托X生产的" — keep only the actual producer, i.e. text after the
    last "、" if a chain is present."""
    text = text.strip("、 ")
    if "、" in text:
        text = text.split("、")[-1]
    return text.strip()


def _clean_substance(text: str) -> str:
    text = text.strip()
    # Drop trailing qualifiers like "检验值"/"含量"/"残留量" — the
    # translation dictionary keys are the bare substance names.
    for suffix in ("检验值", "含量", "残留量", "数"):
        if text.endswith(suffix) and known_term_lookup(text[: -len(suffix)]):
            return text[: -len(suffix)]
    return text


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_cn_date(s: str) -> datetime | None:
    """'2026-08-21' or '2026年08月21日' → datetime."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _make_fingerprint(firm: str | None, product: str | None, country: str | None) -> str:
    text = " ".join([(firm or "").lower(), (product or "").lower()[:120], (country or "").lower()])
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.md5(text.encode()).hexdigest()
