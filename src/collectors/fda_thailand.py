"""Food and Drug Administration, Thailand (อย.) — Food Division consumer-alert
collector.

Endpoint (public, no key, no auth):
  Listing: https://food.fda.moph.go.th/consumer-alertnews?page=N  (24 pages
    as of 2026-09-01, ~10 items/page; year-category archive pages
    /consumer-alertnews/category/verification-results-25XX confirm history
    back to 2561 BE = 2018 CE, though this collector just walks the plain
    paginated listing rather than the per-year categories).
  Detail: https://food.fda.moph.go.th/consumer-alertnews/<slug>

Two content shapes share this listing, both handled:
  1. Monthly "ประกาศผลการตรวจพิสูจน์อาหาร ประจำเดือน..." (Food Verification
     Results, monthly) bulletins — an actual HTML `<table>`, not free
     prose, with columns: No. / test-or-sample date / sample source /
     product+label details / lab finding / (PDF link, image-only, no text).
     Rows sometimes use `rowspan` to attach two distinct findings to one
     numbered incident (e.g. two separately-dated batches of the same
     product) — handled by only counting **non-empty-text** `<td>` cells
     per row: a full row yields 5 (No/date/location/product/finding), a
     rowspan-continuation row yields 2 (product/finding only), and the
     missing leading columns are carried forward from the last full row.
  2. Standalone single-product alerts (no table at all) — treated as one
     record using the article's own title/date/body text.

Thai dates use the Buddhist Era (BE = CE + 543) and appear in two formats:
the bulletin's own publish date is full month name + 4-digit BE year
("10 สิงหาคม 2569"); per-row sample dates are abbreviated month + 2-digit
BE year ("18 พ.ค. 69"). Both are converted to CE. A row's date is often
"-" (not applicable, e.g. an online-marketplace finding with no physical
inspection date) — falls back to the bulletin's own publish date.

No formal severity/classification field exists — seventh source to hit
the documented Bi-Encoder score-suppression issue. hazard_category is
keyword-matched against the Thai "finding" text directly (not translated
text) for the same reason as every other non-English collector in this
project: translation quality should never gate classification. Product/
firm names ARE translated (local th-en model via src/translation.py) for
readability, same imprecise-but-usable tier as samr_china.py/caa_japan.py.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterator

from .base import BaseCollector, make_retry_session, infer_product_category
from ..translation import translate_batch

BASE_URL = "https://food.fda.moph.go.th"
LIST_URL_TPL = BASE_URL + "/consumer-alertnews?page={page}"
MAX_PAGES = 30

_LISTING_ITEM_RE = re.compile(
    r'<a href="(https://food\.fda\.moph\.go\.th/consumer-alertnews/[^"?]+)" class="d-flex ">(.*?)</a>',
    re.DOTALL,
)
_LISTING_TITLE_RE = re.compile(r'<h[1-6][^>]*>(.*?)</h[1-6]>', re.DOTALL)
_LISTING_DATE_RE = re.compile(r'icon-calendar[^"]*"[^>]*>\s*</span>\s*([^<]+)<', re.DOTALL)
_TABLE_RE = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL)
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
_CELL_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_SINGLE_DATE_RE = re.compile(r'single__date">\s*<span[^>]*></span>\s*([^<]+?)\s*</div>', re.DOTALL)
_SINGLE_TITLE_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.DOTALL)
_CONTENT_RE = re.compile(r'single__content">(.*?)<div class="single__share', re.DOTALL)

_THAI_MONTHS_FULL = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5,
    "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10,
    "พฤศจิกายน": 11, "ธันวาคม": 12,
}
_THAI_MONTHS_ABBR = {
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}
_FULL_DATE_RE = re.compile(r'(\d{1,2})\s+(' + '|'.join(_THAI_MONTHS_FULL) + r')\s+(\d{4})')
_ABBR_DATE_RE = re.compile(r'(\d{1,2})\s+(' + '|'.join(re.escape(m) for m in _THAI_MONTHS_ABBR) + r')\s+(\d{2,4})')

_BIOLOGICAL_KW = ["แบคทีเรีย", "อีโคไล", "โคลิฟอร์ม", "ซาลโมเนลลา", "เชื้อรา", "ยีสต์", "จุลินทรีย์", "อี.โคไล"]
# Drug-adulterant names sometimes appear in Latin script even inside
# otherwise-Thai text (lab reports quoting the compound name directly) —
# both scripts are kept since either can appear. Matching is
# case-insensitive (see _infer_hazard_category/_extract_hazard_specific).
_CHEMICAL_KW = [
    "ซิลเดนาฟิล", "sildenafil", "ไซบูทรามีน", "sibutramine", "ยาแผนปัจจุบัน",
    "สารกันบูด", "สารเคมี", "ยาฆ่าแมลง", "ตะกั่ว", "ปรอท", "แคดเมียม",
    "สเตียรอยด์", "สารสเตียรอยด์", "วัตถุกันเสีย",
]
_PHYSICAL_KW = ["สิ่งแปลกปลอม", "เศษแก้ว", "เศษโลหะ", "เศษพลาสติก"]
_ALLERGEN_KW = ["สารก่อภูมิแพ้", "แพ้อาหาร"]
_LABELING_KW = ["ไม่มีฉลาก", "ฉลากไม่ถูกต้อง", "ปลอมเลขสารบบ", "แสดงฉลากไม่ถูกต้อง"]
_NONCOMPLIANCE_KW = ["ไม่ได้ขึ้นทะเบียน", "โรงงานเถื่อน", "ไม่ได้รับอนุญาต"]

# Checked in this order (most specific first) against the Thai product name
# text, same convention as caa_japan.py's _PRODUCT_CATEGORY_KW.
_PRODUCT_CATEGORY_KW: dict[str, list[str]] = {
    "seafood and fish products": ["ปลา", "กุ้ง", "ปู", "หอย", "อาหารทะเล", "ปลาหมึก"],
    "meat and poultry products": ["เนื้อหมู", "เนื้อวัว", "ไก่", "เนื้อสัตว์", "ไส้กรอก", "แฮม"],
    "dairy and eggs": ["นม", "ผลิตภัณฑ์นม", "ไข่", "เนย", "ชีส", "โยเกิร์ต"],
    "fruits and vegetables": ["ผัก", "ผลไม้", "มะเขือเทศ", "แตงกวา"],
    "cereals and bakery products": ["ขนมปัง", "ข้าว", "เส้นก๋วยเตี๋ยว", "แป้ง", "บะหมี่"],
    "confectionery and snacks": ["ขนมหวาน", "ช็อกโกแลต", "ลูกอม", "ขนมขบเคี้ยว"],
    "beverages": ["เครื่องดื่ม", "น้ำผลไม้", "ชา", "กาแฟ", "สุรา", "เบียร์", "ไวน์"],
    "sauces, condiments and seasonings": ["เครื่องปรุง", "ซอส", "น้ำปลา", "ซีอิ๊ว"],
    "dietary supplements": ["ผลิตภัณฑ์เสริมอาหาร", "อาหารเสริม"],
    "nuts, seeds and grains": ["ถั่ว", "เมล็ดพันธุ์"],
    "oils and fats": ["น้ำมัน", "น้ำมันพืช"],
    "prepared dishes and meals": ["อาหารสำเร็จรูป", "อาหารแช่แข็ง"],
}


class FDAThailandCollector(BaseCollector):
    source_id = "fda_thailand"

    def fetch_raw(self, since: datetime | None = None, limit: int | None = None) -> Iterator[dict]:
        session = make_retry_session()
        count = 0

        for page in range(1, MAX_PAGES + 1):
            r = session.get(LIST_URL_TPL.format(page=page), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                break
            items = _LISTING_ITEM_RE.findall(r.text)
            if not items:
                return

            stop = False
            for url, block in items:
                title_m = _LISTING_TITLE_RE.search(block)
                date_m = _LISTING_DATE_RE.search(block)
                title = _strip_tags(title_m.group(1)) if title_m else ""
                list_date_str = date_m.group(1).strip() if date_m else ""
                list_date = _parse_thai_date(list_date_str)

                if since and list_date and list_date < since.replace(tzinfo=None):
                    stop = True
                    break

                dr = session.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                if dr.status_code != 200:
                    continue

                entries = list(_parse_detail_page(dr.text, title, url, list_date))
                if not entries:
                    continue

                fields = ["firm_th", "product_th", "finding_th"]
                flat = [e[f] for e in entries for f in fields]
                translated = translate_batch(flat, "th")
                for i, entry in enumerate(entries):
                    base = i * len(fields)
                    entry["firm_en"] = translated[base]
                    entry["product_en"] = translated[base + 1]
                    entry["finding_en"] = translated[base + 2]

                for entry in entries:
                    yield entry
                    count += 1
                    if limit and count >= limit:
                        return
            if stop:
                return

    def normalize(self, raw: dict) -> dict:
        classify_text = f"{raw['finding_th']} {raw['title_th']}"
        hazard_category = _infer_hazard_category(classify_text)
        hazard_specific_th = _extract_hazard_specific(classify_text)
        hazard_specific_en = translate_batch([hazard_specific_th], "th")[0] if hazard_specific_th else None
        product_category = infer_product_category(raw["product_th"], _PRODUCT_CATEGORY_KW)

        record_id = f"{raw['slug']}::{raw['entry_index']}"

        return {
            "id": f"fda_thailand::{record_id}",
            "source_id": self.source_id,
            "source_record_id": record_id,
            "fingerprint": _make_fingerprint(raw["firm_en"], raw["product_en"], "Thailand"),
            "record_url": raw["detail_url"],
            "ingestion_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_published_date": raw["event_date"],
            "event_initiation_date": raw["event_date"],
            "event_status": None,  # not published as a formal open/closed status
            "origin_country": "Thailand",
            "distribution_countries": json.dumps(["Thailand"]),
            "israel_relevance_flag": 0,  # domestic Thai market alerts — never Israel-relevant
            "recalling_firm": raw["firm_en"] or None,
            "brand_names": json.dumps([]),
            "product_description": raw["product_en"] or None,
            "product_category": product_category,
            "hazard_category": hazard_category,
            "hazard_specific": hazard_specific_en or None,
            # No formal classification scale exists here — left honestly null,
            # same convention as cdc_food_safety_rss.py / samr_china.py.
            "severity_raw": None,
            "severity_normalized": None,
            "population_at_risk": None,
            "illness_count_reported": None,
            "title": f"{raw['finding_en']} — {raw['product_en']}" if raw["finding_en"] and raw["product_en"] else raw["title_th"],
            "description": raw["finding_en"] or None,
            "reason_for_recall": raw["finding_en"] or None,
        }


def _parse_detail_page(html: str, list_title: str, url: str,
                        list_date: datetime | None) -> Iterator[dict]:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    title_m = _SINGLE_TITLE_RE.search(html)
    title_th = _strip_tags(title_m.group(1)) if title_m else list_title
    date_m = _SINGLE_DATE_RE.search(html)
    pub_date = _parse_thai_date(date_m.group(1)) if date_m else list_date
    pub_date_str = (pub_date or datetime.now(timezone.utc)).date().isoformat()

    content_m = _CONTENT_RE.search(html)
    content = content_m.group(1) if content_m else html
    table_m = _TABLE_RE.search(content)

    if not table_m:
        # Standalone single-product alert — no table, one record.
        body = _strip_tags(content)
        if not body:
            return
        yield {
            "slug": slug, "detail_url": url, "entry_index": 1,
            "title_th": title_th, "event_date": pub_date_str,
            "firm_th": "", "product_th": title_th, "finding_th": body[:300],
        }
        return

    rows = _ROW_RE.findall(table_m.group(1))
    carry_no, carry_date, carry_location = None, pub_date_str, None
    entry_index = 0
    for row_html in rows:
        cells = [_strip_tags(c) for c in _CELL_RE.findall(row_html)]
        cells = [c for c in cells if c]  # drop empty (image-only PDF-link) cells
        if not cells or "ลำดับ" in cells[0]:
            continue  # header row

        if len(cells) >= 5:
            carry_no, row_date_str, carry_location = cells[0], cells[1], cells[2]
            row_date = _parse_thai_date(row_date_str)
            carry_date = row_date.date().isoformat() if row_date else pub_date_str
            product_th, finding_th = cells[3], cells[4]
        elif len(cells) == 2:
            product_th, finding_th = cells[0], cells[1]
        else:
            continue  # unrecognized shape — skip rather than guess wrong

        entry_index += 1
        yield {
            "slug": slug, "detail_url": url, "entry_index": entry_index,
            "title_th": title_th, "event_date": carry_date,
            "firm_th": carry_location or "", "product_th": product_th, "finding_th": finding_th,
        }


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&ldquo;|&rdquo;", '"', text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_thai_date(s: str) -> datetime | None:
    """Buddhist-Era dates, two formats: full month + 4-digit year
    ("10 สิงหาคม 2569") or abbreviated month + 2-digit year ("18 พ.ค. 69").
    BE year → CE by subtracting 543. A bare "-" (no applicable date, e.g.
    an online-marketplace finding) returns None — caller falls back to the
    bulletin's own publish date."""
    if not s or s.strip() == "-":
        return None
    m = _FULL_DATE_RE.search(s)
    if m:
        day, month_name, year_be = int(m.group(1)), m.group(2), int(m.group(3))
        return _safe_date(year_be - 543, _THAI_MONTHS_FULL[month_name], day)
    m = _ABBR_DATE_RE.search(s)
    if m:
        day, month_name, year_be = int(m.group(1)), m.group(2), int(m.group(3))
        if year_be < 100:
            year_be += 2500
        return _safe_date(year_be - 543, _THAI_MONTHS_ABBR[month_name], day)
    return None


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _infer_hazard_category(text: str) -> str | None:
    text = text.lower()
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
    text = text.lower()
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
