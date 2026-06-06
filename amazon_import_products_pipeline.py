"""
Amazon Import Products Pipeline (5 stages)

Stage 1:
- Read ImportProducts-style CSV; map columns via .env (IMPORT_* prefix)
- Classify rows as seller vs reseller (Amazon retail, brand-seller match only)
- Output: sellers CSV + resellers CSV (sorted by seller name)

Stage 2:
- Product endpoint per ASIN; append Scraped Seller Name/ID/URL
- Cache by input seller name

Stage 3:
- Seller profile per Scraped Seller ID; region filter DE/CH/AT only
- Append seller region, postal code, rating, review count, description, about
- Cache by Scraped Seller ID; wiped = region failures only

Stage 4:
- GPT extract contacts from seller about + description

Stage 5:
- Apollo domain search; match Stage 4 person + GPT outreach pick
"""

import argparse
import csv
import html
import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import amazon_seller_pipeline as seller_sp
from dotenv import load_dotenv
from openai import OpenAI

from config_env import override_from_env

CONFIG = {
    "INPUT_CSV": "data/ImportProducts.csv",
    "COLUMN_ASIN_OR_URL": ["ASIN", "URL"],
    "COLUMN_SELLER_COUNT": ["Number of Active Sellers"],
    "COLUMN_SELLER_NAME": ["Seller"],
    "COLUMN_BRAND_NAME": ["Brand"],
    "STAGE1_SELLERS_OUTPUT_CSV": "data/import_sellers.csv",
    "STAGE1_RESELLERS_OUTPUT_CSV": "data/import_resellers.csv",
    "STAGE2_OUTPUT_CSV": "data/import_stage2.csv",
    "STAGE3_OUTPUT_CSV": "data/import_stage3.csv",
    "STAGE3_WIPED_OUTPUT_CSV": "data/import_stage3_wiped.csv",
    "STAGE4_OUTPUT_CSV": "data/import_stage4.csv",
    "STAGE5_OUTPUT_CSV": "data/import_stage5.csv",
    "START_FROM_LINE": None,
    "MAX_RECORDS": None,
    "AMAZON_DOMAIN": "amazon.de",
    "API_TIMEOUT": 60,
    "WAIT_BETWEEN_REQUESTS": 1,
    "WAIT_BETWEEN_BATCHES": 1,
    "SCROLL_TO_BOTTOM": "true",
    "WAIT_BOTTOM_CAROUSEL": "true",
    "WAIT_FOR_OFFERS": "true",
    "WAIT_FOR_VIDEO": "true",
    "HTTP": "true",
    "DEVICE": "desktop",
    "STAGE1_WRITE_BATCH_SIZE": 100,
    "STAGE2_WRITE_BATCH_SIZE": 100,
    "STAGE3_WRITE_BATCH_SIZE": 100,
    "STAGE4_BATCH_SIZE": 10,
    "STAGE5_WRITE_BATCH_SIZE": 50,
    "APOLLO_BASE_URL": "https://api.apollo.io/api/v1",
    "APOLLO_API_TIMEOUT": 60,
    "APOLLO_HTTP_RETRIES": 3,
    "APOLLO_SEARCH_BY_NAME_FALLBACK": False,
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 5000,
}

STAGE2_EXTRA = ["Scraped Seller Name", "Scraped Seller ID", "Scraped Seller URL"]
STAGE3_EXTRA = [
    "seller region",
    "seller postal code",
    "seller rating",
    "seller review count",
    "seller description",
    "seller about",
]
STAGE4_EXTRA = [
    "seller email",
    "seller number",
    "seller incharge person",
    "seller person title",
]
STAGE5_EXTRA = [
    "matched_contact_apollo_email",
    "matched_contact_apollo_title",
    "outreach_apollo_name",
    "outreach_apollo_title",
    "outreach_apollo_email",
]

ALLOWED_REGIONS = {"DE", "CH", "AT"}
ASIN_COL = "asin"

ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)
AMAZON_PATH_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)

_AMAZON_RETAIL_PATTERNS = (
    "amazon",
    "amazon.de",
    "amazon marketplace",
    "amazon eu",
    "amazon services",
    "amazon business",
)

API_KEY = ""
APOLLO_API_KEY = ""
client = None

DEBUG_LOG_PATH = Path(__file__).resolve().parent / ".cursor" / "debug-09ef33.log"


def _debug_log(location: str, message: str, data: dict, hypothesis_id: str = "", run_id: str = "pre-fix"):
    # #region agent log
    try:
        payload = {
            "sessionId": "09ef33",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # #endregion

SYSTEM_PROMPT_STAGE4 = """Extract structured fields from the provided seller texts only.

Rules:
- You receive two texts per row: "seller about" and "seller description" (may be empty or "null").
- Use ONLY those two texts. No web search, no tools, no outside knowledge, no guessing.
- Do NOT invent names, emails, phone numbers, or titles. If a value is not explicitly present in the texts, output null for that field.
- For "person in charge" and "person title": return exactly ONE person.
- If multiple people are present, choose the single person whose title/role is most relevant to Amazon listings, marketplace, e-commerce, marketing, brand content, or visual/creative ownership.
- If no title is present for any person, choose the first named person in the text.
- The "seller person title" must be the title/role of that same chosen person as written in the text (or a close trimmed form), not another person's title.
- If only role labels exist without a clear person name, use null for person and null for title.
- Output exactly one CSV line per input row.
- Format exactly: Nr;seller email;seller number;seller incharge person;seller person title
- If not found, use null for that field (lowercase).
- Keep phone as found in the text (including leading + when present).
- No extra text, no markdown, no explanations.
"""


def _column_aliases(key: str) -> List[str]:
    val = CONFIG.get(key)
    if isinstance(val, list):
        return [c for c in val if c]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def pick_column(row: dict, aliases: List[str]) -> str:
    for col in aliases:
        v = (row.get(col) or "").strip()
        if v:
            return v
    return ""


def extract_asin(value: str) -> str:
    if not value or not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    m = AMAZON_PATH_RE.search(s)
    if m:
        return m.group(1).upper()
    m = ASIN_RE.search(s)
    if m:
        return m.group(1).upper()
    return ""


def extract_asin_from_row(row: dict) -> str:
    for col in _column_aliases("COLUMN_ASIN_OR_URL"):
        found = extract_asin((row.get(col) or "").strip())
        if found:
            return found
    return ""


def normalize_name_key(name: str) -> str:
    s = html.unescape(str(name or "")).strip().lower()
    return re.sub(r"\s+", " ", s)


def alphanumeric_core(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_name_key(name), flags=re.IGNORECASE)


def is_amazon_retailer(seller_name: str) -> bool:
    n = normalize_name_key(seller_name)
    if not n:
        return False
    if n == "amazon" or n.startswith("amazon "):
        return True
    for pat in _AMAZON_RETAIL_PATTERNS:
        if pat in n:
            return True
    core = alphanumeric_core(seller_name)
    return core == "amazon" or core.startswith("amazon")


def brand_matches_seller(brand: str, seller_name: str) -> bool:
    brand = (brand or "").strip()
    seller_name = (seller_name or "").strip()
    if not brand or not seller_name:
        return False
    if normalize_name_key(brand) == normalize_name_key(seller_name):
        return True

    clean_brand = seller_sp.extract_clean_company_name(brand)
    clean_seller = seller_sp.extract_clean_company_name(seller_name)
    if not clean_brand or not clean_seller:
        return False

    cb = normalize_name_key(clean_brand)
    cs = normalize_name_key(clean_seller)
    if cs.startswith(cb):
        return True

    brand_alpha = alphanumeric_core(clean_brand)
    seller_alpha = alphanumeric_core(clean_seller)
    if brand_alpha and seller_alpha and brand_alpha in seller_alpha:
        return True

    tokens = [t for t in re.split(r"\s+", cb) if len(t) >= 2]
    if not tokens:
        return False
    pos = 0
    for tok in tokens:
        idx = cs.find(tok, pos)
        if idx < 0:
            return False
        pos = idx + len(tok)
    return True


def classify_row(row: dict) -> str:
    seller_name = pick_column(row, _column_aliases("COLUMN_SELLER_NAME"))
    brand = pick_column(row, _column_aliases("COLUMN_BRAND_NAME"))

    if is_amazon_retailer(seller_name):
        return "reseller"
    if brand_matches_seller(brand, seller_name):
        return "seller"
    return "reseller"


def stage1_sort_key(row: dict) -> tuple:
    seller_name = pick_column(row, _column_aliases("COLUMN_SELLER_NAME"))
    return (seller_name.lower(),)


def flush_rows(path: str, headers: List[str], rows: List[dict], mode: str):
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


def read_input_rows(path: str) -> Tuple[List[str], List[dict]]:
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows = list(reader)

    start = CONFIG.get("START_FROM_LINE")
    if start is not None and int(start) > 1:
        rows = rows[int(start) - 1 :]

    max_rec = CONFIG.get("MAX_RECORDS")
    if max_rec is not None:
        rows = rows[: int(max_rec)]

    return headers, rows


def build_headers(base: List[str], extra: List[str]) -> List[str]:
    out = list(base)
    for col in extra:
        if col not in out:
            out.append(col)
    return out


def sync_seller_module():
    shared_keys = (
        "AMAZON_DOMAIN",
        "API_TIMEOUT",
        "WAIT_BETWEEN_REQUESTS",
        "WAIT_BETWEEN_BATCHES",
        "SCROLL_TO_BOTTOM",
        "WAIT_BOTTOM_CAROUSEL",
        "WAIT_FOR_OFFERS",
        "WAIT_FOR_VIDEO",
        "HTTP",
        "DEVICE",
        "MODEL",
        "REASONING_EFFORT",
        "MAX_OUTPUT_TOKENS",
        "APOLLO_BASE_URL",
        "APOLLO_API_TIMEOUT",
        "APOLLO_HTTP_RETRIES",
        "APOLLO_SEARCH_BY_NAME_FALLBACK",
    )
    for k in shared_keys:
        if k in CONFIG:
            seller_sp.CONFIG[k] = CONFIG[k]
    seller_sp.API_KEY = API_KEY
    seller_sp.APOLLO_API_KEY = APOLLO_API_KEY
    seller_sp.client = client
    seller_sp.load_public_email_domains()


def seller_name_cache_key(row: dict) -> str:
    return normalize_name_key(pick_column(row, _column_aliases("COLUMN_SELLER_NAME")))


def scraped_seller_id_raw(row: dict) -> str:
    """Original-case seller ID for API calls (API requires uppercase)."""
    sid = (row.get("Scraped Seller ID") or "").strip()
    if sid and sid.lower() != "null":
        return sid
    return ""


def scraped_seller_id_cache_key(row: dict) -> str:
    """Lowercase seller ID for in-memory dedup cache only."""
    raw = scraped_seller_id_raw(row)
    return raw.lower() if raw else ""


def normalize_person_name(name: str) -> str:
    s = html.unescape(str(name or "")).strip().lower()
    s = re.sub(r"^(dr|prof|mr|mrs|ms|herr|frau)\.?\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^a-zäöüß\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def apollo_person_display_name(person: dict) -> str:
    name = (person.get("name") or "").strip()
    if name:
        return name
    fn = (person.get("first_name") or "").strip()
    ln = (person.get("last_name") or "").strip()
    return (fn + " " + ln).strip()


def match_person_name(stage4_name: str, apollo_name: str) -> bool:
    a = normalize_person_name(stage4_name)
    b = normalize_person_name(apollo_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if len(a_tokens) >= 2 and len(b_tokens) >= 2:
        overlap = a_tokens & b_tokens
        if len(overlap) >= 2:
            return True
        if len(overlap) >= 1 and (len(a_tokens) == 1 or len(b_tokens) == 1):
            return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def find_apollo_person_match(stage4_person: str, people: List[dict]) -> Optional[dict]:
    if not stage4_person or stage4_person.strip().lower() == "null":
        return None
    best = None
    best_score = 0.0
    for p in people:
        if not isinstance(p, dict):
            continue
        pname = apollo_person_display_name(p)
        if not pname:
            continue
        if match_person_name(stage4_person, pname):
            score = SequenceMatcher(None, normalize_person_name(stage4_person), normalize_person_name(pname)).ratio()
            if score > best_score:
                best_score = score
                best = p
    return best


def call_gpt_stage4_extract(batch: List[dict]) -> str:
    lines = [
        "Extract from seller about + seller description only (see system rules).",
        "Nr | seller about | seller description",
        "-" * 80,
    ]
    for i, row in enumerate(batch, 1):
        about = (row.get("seller about") or "").strip()
        desc = (row.get("seller description") or "").strip()
        lines.append(f"{i} | {about} | {desc}")
    lines.append("-" * 80)
    lines.append("Answer only as CSV:")
    lines.append("Nr;seller email;seller number;seller incharge person;seller person title")
    user_prompt = "\n".join(lines)
    try:
        response = client.responses.create(
            model=CONFIG["MODEL"],
            reasoning={"effort": CONFIG["REASONING_EFFORT"]},
            input=[
                {"role": "system", "content": SYSTEM_PROMPT_STAGE4},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=int(CONFIG["MAX_OUTPUT_TOKENS"]),
        )
        return (response.output_text or "").strip()
    except Exception as e:
        print(f"      ❌ GPT error: {e}")
        return ""


def call_gpt_stage4_with_retry(batch: List[dict], max_retries: int = 3):
    last_rows = [("null", "null", "null", "null") for _ in range(len(batch))]
    for attempt in range(1, max_retries + 1):
        raw = call_gpt_stage4_extract(batch)
        rows, complete = seller_sp.parse_stage4_response(raw, len(batch))
        last_rows = rows
        if complete:
            if attempt > 1:
                print(f"      ✅ GPT batch recovered on retry #{attempt}.")
            return rows
        print(f"      ⚠️  GPT output incomplete on attempt {attempt}/{max_retries}; retrying...")
        time.sleep(float(CONFIG.get("WAIT_BETWEEN_BATCHES", 1)))
    print("      ❌ GPT batch still incomplete after retries; filling missing rows with null.")
    return last_rows


def extract_postal_code(rows: list) -> str:
    if not isinstance(rows, list) or len(rows) < 2:
        return "null"
    postal = seller_sp.null_str(rows[-2]).strip()
    return postal if postal.lower() != "null" else "null"


def stage1():
    print("\n" + "=" * 90)
    print("STAGE 1 - Classify sellers vs resellers")
    print("=" * 90)
    print(f"📁 Input:     {CONFIG['INPUT_CSV']}")
    print(f"📁 Sellers:   {CONFIG['STAGE1_SELLERS_OUTPUT_CSV']}")
    print(f"📁 Resellers: {CONFIG['STAGE1_RESELLERS_OUTPUT_CSV']}")

    base_headers, rows = read_input_rows(CONFIG["INPUT_CSV"])
    headers = build_headers(base_headers, [ASIN_COL])

    sellers = []
    resellers = []
    for row in rows:
        out = dict(row)
        out[ASIN_COL] = extract_asin_from_row(row) or "null"
        bucket = classify_row(row)
        if bucket == "seller":
            sellers.append(out)
        else:
            resellers.append(out)

    sellers.sort(key=stage1_sort_key)
    resellers.sort(key=stage1_sort_key)

    batch_size = int(CONFIG["STAGE1_WRITE_BATCH_SIZE"])
    for path, data in (
        (CONFIG["STAGE1_SELLERS_OUTPUT_CSV"], sellers),
        (CONFIG["STAGE1_RESELLERS_OUTPUT_CSV"], resellers),
    ):
        mode = "w"
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            flush_rows(path, headers, chunk, mode)
            mode = "a"

    print(f"\n✅ STAGE 1 complete. Sellers: {len(sellers)} | Resellers: {len(resellers)}")


def stage2():
    print("\n" + "=" * 90)
    print("STAGE 2 - Product endpoint -> Scraped Seller fields")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE1_SELLERS_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE2_OUTPUT_CSV']}")

    sync_seller_module()
    with open(CONFIG["STAGE1_SELLERS_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE2_EXTRA)
    scrape_cache: Dict[str, Tuple[str, str, str]] = {}
    buf = []
    written = 0
    mode = "w"
    batch_size = int(CONFIG["STAGE2_WRITE_BATCH_SIZE"])

    for idx, row in enumerate(rows, 1):
        asin = (row.get(ASIN_COL) or "").strip()
        cache_key = seller_name_cache_key(row)
        print(f"\n📍 [S2] row #{idx} | asin: {asin or 'null'} | seller: {cache_key or 'N/A'}")

        if cache_key and cache_key in scrape_cache:
            seller_name, seller_id, seller_link = scrape_cache[cache_key]
            print("   ♻️  Reused cached scrape for seller name")
        else:
            seller_name = seller_id = seller_link = "null"
            if asin and asin.lower() != "null":
                data = seller_sp.fetch_json(seller_sp.build_product_url(asin))
                product = (data or {}).get("product_results", {})
                buybox = product.get("buybox", {}) if isinstance(product, dict) else {}
                fulfillment = buybox.get("fulfillment", {}) if isinstance(buybox, dict) else {}
                tps = fulfillment.get("third_party_seller", {}) if isinstance(fulfillment, dict) else {}
                if isinstance(tps, dict) and (tps.get("id") or tps.get("name") or tps.get("link")):
                    seller_id = seller_sp.null_str(tps.get("id"))
                    seller_name = seller_sp.null_str(tps.get("name"))
                    seller_link = seller_sp.null_str(tps.get("link"))
                else:
                    seller_name = seller_sp.null_str(product.get("sold_by"))
                time.sleep(float(CONFIG["WAIT_BETWEEN_REQUESTS"]))
            if cache_key:
                scrape_cache[cache_key] = (seller_name, seller_id, seller_link)

        out = dict(row)
        out["Scraped Seller Name"] = seller_name
        out["Scraped Seller ID"] = seller_id
        out["Scraped Seller URL"] = seller_link
        buf.append(out)

        if len(buf) >= batch_size:
            flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], headers, buf, mode)
            written += len(buf)
            print(f"   ✍️  Flushed {len(buf)} rows (total written: {written})")
            buf = []
            mode = "a"

    if buf:
        flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], headers, buf, mode)
        written += len(buf)
        print(f"   ✍️  Final flush {len(buf)} rows (total written: {written})")

    print(f"\n✅ STAGE 2 complete. Rows written: {written} | Cache hits: {len(rows) - len(scrape_cache)}")


def stage3():
    print("\n" + "=" * 90)
    print("STAGE 3 - Seller profile + region filter (DE/CH/AT)")
    print("=" * 90)
    print(f"📁 Input:        {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 Main output:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Wiped output: {CONFIG['STAGE3_WIPED_OUTPUT_CSV']}")

    sync_seller_module()
    with open(CONFIG["STAGE2_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE3_EXTRA)
    profile_cache: Dict[str, dict] = {}
    region_kept = []
    region_wiped = []

    for idx, row in enumerate(rows, 1):
        seller_id = scraped_seller_id_raw(row)
        cache_key = scraped_seller_id_cache_key(row)
        print(f"\n📍 [S3] row #{idx} | scraped seller id: {seller_id or 'null'}")

        if cache_key and cache_key in profile_cache:
            profile = profile_cache[cache_key]
            print("   ♻️  Reused cached seller profile")
        else:
            region = postal = rating = review_count = description = about = "null"
            addr_rows = None
            if seller_id:
                data = seller_sp.fetch_json(seller_sp.build_seller_url(seller_id))
                details = (data or {}).get("seller_profile", {}).get("seller_details", {})
                if isinstance(details, dict):
                    addr_rows = details.get("business_address_rows")
                    if isinstance(addr_rows, list) and addr_rows:
                        region = seller_sp.null_str(addr_rows[-1]).upper()
                        postal = extract_postal_code(addr_rows)
                    rating = seller_sp.null_str(details.get("rating"))
                    review_count = seller_sp.null_str(details.get("ratings_total"))
                    description = seller_sp.seller_text_field(details.get("detailed_information"))
                    about = seller_sp.seller_text_field(details.get("about_this_seller"))
                if idx <= 3 or (seller_id.startswith("A2YL") and idx <= 20):
                    _debug_log(
                        "stage3:api_response",
                        "seller profile fetch",
                        {
                            "row": idx,
                            "seller_id": seller_id,
                            "seller_id_lower": seller_id.lower(),
                            "addr_rows_len": len(addr_rows) if isinstance(addr_rows, list) else None,
                            "region": region,
                            "postal": postal,
                            "about_len": len(about) if about and about != "null" else 0,
                        },
                        hypothesis_id="H1",
                    )
                time.sleep(float(CONFIG["WAIT_BETWEEN_REQUESTS"]))
            profile = {
                "seller region": region,
                "seller postal code": postal,
                "seller rating": rating,
                "seller review count": review_count,
                "seller description": description,
                "seller about": about,
            }
            if cache_key:
                profile_cache[cache_key] = profile

        out = dict(row)
        out.update(profile)
        region_value = (out.get("seller region") or "").strip().upper()
        should_wipe = (not region_value) or (region_value == "NULL") or (region_value not in ALLOWED_REGIONS)
        if should_wipe:
            region_wiped.append(out)
        else:
            region_kept.append(out)

    batch_size = int(CONFIG["STAGE3_WRITE_BATCH_SIZE"])
    kept_n = wiped_n = 0
    for path, data, label in (
        (CONFIG["STAGE3_OUTPUT_CSV"], region_kept, "kept"),
        (CONFIG["STAGE3_WIPED_OUTPUT_CSV"], region_wiped, "wiped"),
    ):
        mode = "w"
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            flush_rows(path, headers, chunk, mode)
            if label == "kept":
                kept_n += len(chunk)
            else:
                wiped_n += len(chunk)
            mode = "a"

    print(f"\n✅ STAGE 3 complete. Main: {kept_n} | Wiped: {wiped_n}")


def stage4():
    print("\n" + "=" * 90)
    print("STAGE 4 - GPT extract contacts (about + description)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE4_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")

    with open(CONFIG["STAGE3_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    drop_cols = {"seller description", "seller about"}
    base_out_headers = [h for h in base_headers if h not in drop_cols]
    headers = build_headers(base_out_headers, STAGE4_EXTRA)

    mode = "w"
    written = 0
    gpt_batch = []
    batch_size = int(CONFIG["STAGE4_BATCH_SIZE"])

    def flush_gpt_batch(items: List[dict]):
        nonlocal mode, written
        if not items:
            return
        print(f"\n   🤖 Calling GPT for {len(items)} rows...")
        parsed = call_gpt_stage4_with_retry(items)
        batch_rows = []
        for i, row in enumerate(items):
            email, phone, incharge, title = parsed[i]
            out = {h: row.get(h, "null") for h in base_out_headers}
            out["seller email"] = seller_sp.gpt_text_field(email)
            out["seller number"] = seller_sp.phone_for_sheet(phone)
            out["seller incharge person"] = seller_sp.gpt_text_field(incharge)
            out["seller person title"] = seller_sp.gpt_text_field(title)
            batch_rows.append(out)

        flush_rows(CONFIG["STAGE4_OUTPUT_CSV"], headers, batch_rows, mode)
        written += len(batch_rows)
        print(f"   ✍️  Flushed {len(batch_rows)} rows after GPT run (total written: {written})")
        mode = "a"
        time.sleep(float(CONFIG.get("WAIT_BETWEEN_BATCHES", 1)))

    for idx, row in enumerate(rows, 1):
        print(f"\n📍 [S4] row #{idx}")
        gpt_batch.append(row)
        if len(gpt_batch) >= batch_size:
            flush_gpt_batch(gpt_batch)
            gpt_batch = []

    if gpt_batch:
        flush_gpt_batch(gpt_batch)

    print(f"\n✅ STAGE 4 complete. Rows written: {written}")


def stage5():
    print("\n" + "=" * 90)
    print("STAGE 5 - Apollo enrich (matched contact + outreach pick)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE4_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE5_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")

    sync_seller_module()

    with open(CONFIG["STAGE4_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE5_EXTRA)
    seller_cache: Dict[str, Tuple[str, str, str, str, str]] = {}
    buf = []
    written = 0
    skipped = 0
    cache_hits = 0
    mode = "w"
    batch_size = int(CONFIG["STAGE5_WRITE_BATCH_SIZE"])

    for idx, row in enumerate(rows, 1):
        raw_name = (row.get("Scraped Seller Name") or row.get("Seller") or "").strip()
        clean_name = seller_sp.extract_clean_company_name(raw_name)
        if not clean_name and raw_name and raw_name.lower() != "null":
            clean_name = re.sub(r"\s+", " ", html.unescape(raw_name)).strip()

        domain = seller_sp.extract_company_domain_from_email(row.get("seller email") or "")
        stage4_person = (row.get("seller incharge person") or "").strip()
        seller_id = scraped_seller_id_cache_key(row)
        seller_key = f"id:{seller_id}" if seller_id else f"name:{normalize_name_key(clean_name or raw_name)}"

        print(f"\n📍 [S5] row #{idx} | seller: {clean_name or raw_name or 'N/A'} | domain: {domain or 'none'}")

        if seller_key in seller_cache:
            matched_email, matched_title, outreach_name, outreach_title, outreach_email = seller_cache[seller_key]
            cache_hits += 1
            print("   ♻️  Reused cached seller result")
        else:
            matched_email = matched_title = outreach_name = outreach_title = outreach_email = "null"

            if not domain:
                print("   ⏭️  Skip: no usable company domain (name-search disabled)")
                skipped += 1
            else:
                org = seller_sp.apollo_search_company(domain, clean_name or raw_name)
                time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
                if not org or not isinstance(org, dict) or not org.get("id"):
                    print("   ⏭️  Skip: no Apollo organization")
                    skipped += 1
                else:
                    org_id = org.get("id")
                    people = seller_sp.apollo_list_people(org_id)
                    time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
                    if not people:
                        print("   ⏭️  Skip: no people on organization")
                        skipped += 1
                    else:
                        matched = find_apollo_person_match(stage4_person, people)
                        if matched and matched.get("id"):
                            person = seller_sp.apollo_match_person(matched["id"])
                            time.sleep(float(CONFIG.get("WAIT_BETWEEN_REQUESTS", 1)))
                            if person and isinstance(person, dict):
                                matched_email = seller_sp.gpt_text_field(person.get("email"))
                                matched_title = seller_sp.gpt_text_field(person.get("title"))
                                print("   ✓ Matched Stage 4 person in Apollo list")

                        people_lines = []
                        for p in people:
                            if not isinstance(p, dict):
                                continue
                            pid = (p.get("id") or "").strip()
                            tit = (p.get("title") or "").strip()
                            if pid and tit:
                                people_lines.append((pid, tit))

                        if not people_lines:
                            print("   ⏭️  Skip: no id/title pairs for GPT pick")
                            skipped += 1
                        else:
                            gpt_raw = seller_sp.call_gpt_stage5_rank_people(clean_name or raw_name or "", people_lines)
                            picks = seller_sp.parse_stage5_gpt_people(gpt_raw)
                            if not picks:
                                print("   ⏭️  Skip: GPT found no suitable outreach contacts")
                                skipped += 1
                            else:
                                ordered_ids = [p[0] for p in picks]
                                verified = seller_sp.pick_verified_apollo_contact(ordered_ids)
                                if not verified:
                                    print("   ⏭️  Skip: no verified Apollo email on outreach picks")
                                    skipped += 1
                                else:
                                    outreach_name, outreach_title, outreach_email = verified
                                    outreach_name = seller_sp.gpt_text_field(outreach_name)
                                    outreach_title = seller_sp.gpt_text_field(outreach_title)
                                    outreach_email = seller_sp.gpt_text_field(outreach_email)

            seller_cache[seller_key] = (
                matched_email,
                matched_title,
                outreach_name,
                outreach_title,
                outreach_email,
            )

        out = {h: row.get(h, "") for h in base_headers}
        out["matched_contact_apollo_email"] = matched_email
        out["matched_contact_apollo_title"] = matched_title
        out["outreach_apollo_name"] = outreach_name
        out["outreach_apollo_title"] = outreach_title
        out["outreach_apollo_email"] = outreach_email
        buf.append(out)

        if len(buf) >= batch_size:
            flush_rows(CONFIG["STAGE5_OUTPUT_CSV"], headers, buf, mode)
            written += len(buf)
            print(f"   ✍️  Flushed {len(buf)} rows (total written: {written})")
            buf = []
            mode = "a"

    if buf:
        flush_rows(CONFIG["STAGE5_OUTPUT_CSV"], headers, buf, mode)
        written += len(buf)
        print(f"   ✍️  Final flush {len(buf)} rows (total written: {written})")

    print(f"\n✅ STAGE 5 complete. Rows written: {written} | Skipped: {skipped} | Cache hits: {cache_hits}")


STAGES = {
    1: stage1,
    2: stage2,
    3: stage3,
    4: stage4,
    5: stage5,
}


def main(only_stage: Optional[int] = None):
    start = time.time()
    print("\n" + "=" * 90)
    print("AMAZON IMPORT PRODUCTS PIPELINE (5 STAGES)")
    print("=" * 90)
    print(f"📁 Input:     {CONFIG['INPUT_CSV']}")
    print(f"📁 S1 sellers:{CONFIG['STAGE1_SELLERS_OUTPUT_CSV']}")
    print(f"📁 S1 resell: {CONFIG['STAGE1_RESELLERS_OUTPUT_CSV']}")
    print(f"📁 S5 final:  {CONFIG['STAGE5_OUTPUT_CSV']}")

    if only_stage is not None:
        STAGES[only_stage]()
    else:
        for n in range(1, 6):
            STAGES[n]()

    elapsed = time.time() - start
    print("\n" + "=" * 90)
    print("🎉 PIPELINE COMPLETE")
    print(f"⏱️  Total time: {elapsed:.2f}s ({elapsed / 60:.2f} min)")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    override_from_env(CONFIG, env_prefix="IMPORT_")

    parser = argparse.ArgumentParser(description="Amazon Import Products Pipeline")
    parser.add_argument("--stage", type=int, choices=[1, 2, 3, 4, 5], help="Run a single stage only")
    args = parser.parse_args()

    API_KEY = (os.getenv("API_KEY") or "").strip()
    if not API_KEY:
        raise ValueError("API_KEY not found in .env file.")
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not found in .env file.")
    APOLLO_API_KEY = (os.getenv("APOLLO_API_KEY") or "").strip()
    if not APOLLO_API_KEY:
        raise ValueError("APOLLO_API_KEY not found in .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)
    seller_sp.load_public_email_domains()

    main(only_stage=args.stage)
