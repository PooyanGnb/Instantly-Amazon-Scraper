"""
Amazon Seller Multi-Stage Pipeline

Stage 1:
- Search Amazon by keyword for page 1..N
- Output rows:
  product link, product id, product title, price, review count, rating, recent sales, page, position

Stage 2:
- For each product id, call product endpoint
- Extract seller link/id/name from buybox.third_party_seller; fallback to sold_by for seller name
- Output adds:
  seller link, seller id, seller name

Stage 3:
- For rows with seller id, call seller_profile endpoint
- Extract seller region (last business_address_rows item), seller rating, seller review count,
  seller description (detailed_information), seller about (about_this_seller)
- Output adds:
  seller region, seller rating, seller review count, seller description, seller about, rank
- Region filter: only allowed regions DE/AT/NL/CH with non-empty region
- Seller cap: each seller id keeps at most 2 products in main output — best + worst search rank
  (lowest page then lowest position vs highest page then highest position); extras go to wiped
- rank: "best" / "worst" when exactly two kept rows for that seller; empty when only one
- Write two outputs:
  - main output: region-kept + cap-applied rows
  - wiped output: region-fail rows + seller-cap overflow rows

Stage 4:
- Read Stage 3 main output; GPT reads seller about + seller description only (no web search)
- Extract seller email, seller number, person in charge, their title (e.g. Geschäftsführer)
- Output removes seller description and seller about; adds:
  seller email, seller number, seller incharge person, seller person title
"""

import csv
import html
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv
from openai import OpenAI

from config_env import override_from_env

CONFIG = {
    # Stage controls
    "KEYWORD": "ALCLEAR",
    "TOTAL_PAGES": 1,

    # Files
    "STAGE1_OUTPUT_CSV": "data/seller_stage1.csv",
    "STAGE2_OUTPUT_CSV": "data/seller_stage2.csv",
    "STAGE3_OUTPUT_CSV": "data/seller_stage3.csv",
    "STAGE3_WIPED_OUTPUT_CSV": "data/seller_stage3_wiped.csv",
    "FINAL_OUTPUT_CSV": "data/seller_final.csv",

    # API settings
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

    # Flush sizes
    "STAGE1_WRITE_BATCH_SIZE": 100,
    "STAGE2_WRITE_BATCH_SIZE": 100,
    "STAGE3_WRITE_BATCH_SIZE": 100,
    "STAGE4_BATCH_SIZE": 10,

    # GPT settings
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 5000,
}

STAGE1_HEADERS = [
    "product link",
    "product id",
    "product title",
    "price",
    "review count",
    "rating",
    "recent sales",
    "page",
    "position",
]

STAGE2_HEADERS = STAGE1_HEADERS + ["seller link", "seller id", "seller name"]
STAGE3_HEADERS = STAGE2_HEADERS + [
    "seller region",
    "seller rating",
    "seller review count",
    "seller description",
    "seller about",
    "rank",
]
STAGE4_HEADERS = STAGE2_HEADERS + [
    "seller region",
    "seller rating",
    "seller review count",
    "seller email",
    "seller number",
    "seller incharge person",
    "seller person title",
    "rank",
]
ALLOWED_REGIONS = {"DE", "AT", "NL", "CH"}

BASE_URL = "https://ecom.webscrapingapi.com/v1"
client = None


def null_str(v):
    if v is None:
        return "null"
    if isinstance(v, str) and not v.strip():
        return "null"
    return str(v)


def seller_text_field(v):
    """HTML-unescape and trim; empty -> null (for seller API text fields)."""
    if v is None:
        return "null"
    s = html.unescape(str(v)).strip()
    if not s:
        return "null"
    return s


def search_rank_tuple(row):
    """Sort key: lower (page, position) = better rank (earlier in search results)."""
    try:
        p = int(float(str(row.get("page") or "").replace(",", ".").strip() or "999999"))
    except (ValueError, TypeError):
        p = 999999
    try:
        pos = int(float(str(row.get("position") or "").replace(",", ".").strip() or "999999"))
    except (ValueError, TypeError):
        pos = 999999
    return (p, pos)


def seller_cap_group_key(row, row_index):
    """Group products by seller id; missing id => one row per index (never merge)."""
    sid = (row.get("seller id") or "").strip()
    if not sid or sid.lower() == "null":
        return ("__noseller__", row_index)
    return ("seller", sid)


def stage3_apply_seller_cap_and_rank(kept_rows):
    """
    At most 2 products per seller in main output: best (min page, min position) and worst
    (max page, max position). If >2, middle ranks go to wiped list.
    rank: best / worst for the pair; empty string if only one product for that seller.
    """
    groups = defaultdict(list)
    for i, row in enumerate(kept_rows):
        groups[seller_cap_group_key(row, i)].append(row)

    final_kept = []
    extra_wiped = []

    for _key, rows in groups.items():
        sorted_r = sorted(rows, key=search_rank_tuple)
        n = len(sorted_r)
        if n <= 2:
            if n == 1:
                one = dict(sorted_r[0])
                one["rank"] = ""
                final_kept.append(one)
            else:
                best_row = dict(sorted_r[0])
                worst_row = dict(sorted_r[-1])
                best_row["rank"] = "best"
                worst_row["rank"] = "worst"
                final_kept.extend([best_row, worst_row])
        else:
            best_row = dict(sorted_r[0])
            worst_row = dict(sorted_r[-1])
            best_row["rank"] = "best"
            worst_row["rank"] = "worst"
            final_kept.extend([best_row, worst_row])
            for mid in sorted_r[1:-1]:
                w = dict(mid)
                w["rank"] = ""
                extra_wiped.append(w)

    return final_kept, extra_wiped


def parse_rating_text(v):
    # "4,7 von 5 Sternen" -> "4,7"
    if v is None:
        return "null"
    s = str(v).strip()
    if not s:
        return "null"
    m = re.search(r"(\d+[.,]\d+|\d+)", s)
    return m.group(1) if m else "null"


def parse_recent_sales(v):
    # Keep only formats like "100+".
    # Missing field => null
    if v is None:
        return "null"
    s = str(v).strip()
    if not s:
        return "null"
    m = re.search(r"(\d[\d\.\,\s]*\+)", s)
    if not m:
        return "0"
    return re.sub(r"\s+", "", m.group(1))


def fetch_json(url):
    try:
        r = requests.get(url, timeout=int(CONFIG["API_TIMEOUT"]))
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"    ❌ API error: {e}")
        return None


def flush_rows(path, headers, rows, mode):
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


def build_search_url(keyword, page):
    params = {
        "engine": "amazon",
        "api_key": API_KEY,
        "type": "search",
        "q": quote_plus(keyword),
        "amazon_domain": CONFIG["AMAZON_DOMAIN"],
        "scroll_to_bottom": CONFIG["SCROLL_TO_BOTTOM"],
        "wait_bottom_carousel": CONFIG["WAIT_BOTTOM_CAROUSEL"],
        "wait_for_offers": CONFIG["WAIT_FOR_OFFERS"],
        "wait_for_video": "false",
        "http": CONFIG["HTTP"],
        "device": CONFIG["DEVICE"],
        "page": str(page),
    }
    return BASE_URL + "?" + "&".join([f"{k}={v}" for k, v in params.items()])


def build_product_url(product_id):
    params = {
        "engine": "amazon",
        "api_key": API_KEY,
        "type": "product",
        "product_id": product_id,
        "amazon_domain": CONFIG["AMAZON_DOMAIN"],
        "scroll_to_bottom": CONFIG["SCROLL_TO_BOTTOM"],
        "wait_bottom_carousel": CONFIG["WAIT_BOTTOM_CAROUSEL"],
        "wait_for_offers": CONFIG["WAIT_FOR_OFFERS"],
        "wait_for_video": CONFIG["WAIT_FOR_VIDEO"],
        "http": CONFIG["HTTP"],
        "device": CONFIG["DEVICE"],
    }
    return BASE_URL + "?" + "&".join([f"{k}={v}" for k, v in params.items()])


def build_seller_url(seller_id):
    params = {
        "engine": "amazon",
        "api_key": API_KEY,
        "type": "seller_profile",
        "seller_id": seller_id,
        "amazon_domain": CONFIG["AMAZON_DOMAIN"],
        "http": CONFIG["HTTP"],
        "device": CONFIG["DEVICE"],
    }
    return BASE_URL + "?" + "&".join([f"{k}={v}" for k, v in params.items()])


SYSTEM_PROMPT_STAGE4 = """Extract structured fields from the provided seller texts only.

Rules:
- You receive two texts per row: "seller about" and "seller description" (may be empty or "null").
- Use ONLY those two texts. No web search, no tools, no outside knowledge, no guessing.
- Do NOT invent names, emails, phone numbers, or titles. If a value is not explicitly present in the texts, output null for that field.
- For "person in charge" and "person title": only extract when the text clearly states a named individual and their role (e.g. Geschäftsführer: Max Mustermann). If only a role label exists without a clear person name, use null for person and optionally the role word as title only if it appears verbatim next to a name pattern; when unsure, use null.
- Output exactly one CSV line per input row.
- Format exactly: Nr;seller email;seller number;seller incharge person;seller person title
- If not found, use null for that field (lowercase).
- Keep phone as found in the text (including leading + when present).
- No extra text, no markdown, no explanations.
"""


def call_gpt_stage4_extract(batch):
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


def parse_stage4_response(text, size):
    """
    Strict parser for stage4.
    Returns (rows, complete) where complete=True only when all Nr 1..size exist.
    """
    out = [("null", "null", "null", "null") for _ in range(size)]
    if not text:
        return out, False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    data_lines = []
    for ln in lines:
        s = ln.strip("`").strip()
        if not s:
            continue
        if s.lower().startswith("nr;"):
            continue
        if s.startswith("#") or s.startswith("---") or s.startswith("==="):
            continue
        data_lines.append(s)

    indexed = {}
    for ln in data_lines:
        parts = [p.strip() for p in ln.split(";")]
        if len(parts) >= 5:
            m = re.match(r"^(\d+)", parts[0])
            if m:
                nr = int(m.group(1))
                if 1 <= nr <= size:
                    indexed[nr] = (
                        parts[1] or "null",
                        parts[2] or "null",
                        parts[3] or "null",
                        parts[4] or "null",
                    )
        # Ignore malformed lines in strict mode; caller will retry.

    for nr in range(1, size + 1):
        if nr in indexed:
            out[nr - 1] = indexed[nr]

    return out, (len(indexed) == size)


def call_gpt_stage4_with_retry(batch, max_retries=3):
    """
    Retry stage4 GPT call when:
    - API/HTTP error happens
    - model skips rows / invalid indexed output
    """
    last_rows = [("null", "null", "null", "null") for _ in range(len(batch))]
    for attempt in range(1, max_retries + 1):
        raw = call_gpt_stage4_extract(batch)
        rows, complete = parse_stage4_response(raw, len(batch))
        last_rows = rows
        if complete:
            if attempt > 1:
                print(f"      ✅ GPT batch recovered on retry #{attempt}.")
            return rows
        print(f"      ⚠️  GPT output incomplete on attempt {attempt}/{max_retries}; retrying...")
        time.sleep(float(CONFIG["WAIT_BETWEEN_BATCHES"]))
    print("      ❌ GPT batch still incomplete after retries; filling missing rows with null.")
    return last_rows


def phone_for_sheet(v):
    s = (v or "").strip()
    if not s or s.lower() in ("null", "none", "n/a", "na", "-"):
        return "null"
    if s.startswith("'"):
        return s
    return "'" + s


def gpt_text_field(v):
    """Normalize a GPT string field to null when empty or placeholder."""
    s = (v or "").strip()
    if not s or s.lower() in ("null", "none", "n/a", "na", "-", "unknown"):
        return "null"
    return s


def stage1():
    print("\n" + "=" * 90)
    print("STAGE 1 - Search by keyword (page loop)")
    print("=" * 90)
    print(f"🔎 Keyword: {CONFIG['KEYWORD']}")
    print(f"📄 Pages:   {CONFIG['TOTAL_PAGES']}")
    print(f"📁 Output:  {CONFIG['STAGE1_OUTPUT_CSV']}")

    page_count = max(1, int(CONFIG["TOTAL_PAGES"]))
    keyword = str(CONFIG["KEYWORD"]).strip()
    if not keyword:
        raise ValueError("KEYWORD is empty.")

    mode = "w"
    buf = []
    written = 0
    batch_size = int(CONFIG["STAGE1_WRITE_BATCH_SIZE"])

    for page in range(1, page_count + 1):
        print(f"\n📍 [S1] page {page}/{page_count}")
        data = fetch_json(build_search_url(keyword, page))
        products = (data or {}).get("search_results", {}).get("product_results", [])
        print(f"   ✓ products: {len(products)}")

        for p in products:
            rating_obj = p.get("rating") if isinstance(p.get("rating"), dict) else {}
            row = {
                "product link": null_str(p.get("link")),
                "product id": null_str(p.get("product_id")),
                "product title": null_str(p.get("title")),
                "price": null_str((p.get("price") or {}).get("value") if isinstance(p.get("price"), dict) else None),
                "review count": null_str(rating_obj.get("total_ratings")),
                "rating": parse_rating_text(rating_obj.get("rating")),
                "recent sales": parse_recent_sales(p.get("recent_sales")),
                "page": str(page),
                "position": null_str(p.get("position")),
            }
            buf.append(row)

            if len(buf) >= batch_size:
                flush_rows(CONFIG["STAGE1_OUTPUT_CSV"], STAGE1_HEADERS, buf, mode)
                written += len(buf)
                print(f"   ✍️  Flushed {len(buf)} rows (total written: {written})")
                buf = []
                mode = "a"

        time.sleep(float(CONFIG["WAIT_BETWEEN_REQUESTS"]))

    if buf:
        flush_rows(CONFIG["STAGE1_OUTPUT_CSV"], STAGE1_HEADERS, buf, mode)
        written += len(buf)
        print(f"   ✍️  Final flush {len(buf)} rows (total written: {written})")

    print(f"\n✅ STAGE 1 complete. Rows written: {written}")


def stage2():
    print("\n" + "=" * 90)
    print("STAGE 2 - Product details -> seller data")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE1_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE2_OUTPUT_CSV']}")

    mode = "w"
    buf = []
    written = 0
    batch_size = int(CONFIG["STAGE2_WRITE_BATCH_SIZE"])

    with open(CONFIG["STAGE1_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for idx, row in enumerate(r, 1):
            pid = (row.get("product id") or "").strip()
            print(f"\n📍 [S2] row #{idx} | product id: {pid or 'null'}")

            seller_link = "null"
            seller_id = "null"
            seller_name = "null"

            if pid and pid.lower() != "null":
                data = fetch_json(build_product_url(pid))
                product = (data or {}).get("product_results", {})
                buybox = product.get("buybox", {}) if isinstance(product, dict) else {}
                fulfillment = buybox.get("fulfillment", {}) if isinstance(buybox, dict) else {}
                tps = fulfillment.get("third_party_seller", {}) if isinstance(fulfillment, dict) else {}

                if isinstance(tps, dict) and (tps.get("id") or tps.get("name") or tps.get("link")):
                    seller_id = null_str(tps.get("id"))
                    seller_name = null_str(tps.get("name"))
                    seller_link = null_str(tps.get("link"))
                else:
                    seller_name = null_str(product.get("sold_by"))

            out = {h: row.get(h, "null") for h in STAGE1_HEADERS}
            out["seller link"] = seller_link
            out["seller id"] = seller_id
            out["seller name"] = seller_name
            buf.append(out)

            if len(buf) >= batch_size:
                flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], STAGE2_HEADERS, buf, mode)
                written += len(buf)
                print(f"   ✍️  Flushed {len(buf)} rows (total written: {written})")
                buf = []
                mode = "a"

            time.sleep(float(CONFIG["WAIT_BETWEEN_REQUESTS"]))

    if buf:
        flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], STAGE2_HEADERS, buf, mode)
        written += len(buf)
        print(f"   ✍️  Final flush {len(buf)} rows (total written: {written})")

    print(f"\n✅ STAGE 2 complete. Rows written: {written}")


def stage3():
    print("\n" + "=" * 90)
    print("STAGE 3 - Seller profile + region filter + seller cap (max 2 / rank)")
    print("=" * 90)
    print(f"📁 Input:        {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 Main output:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Wiped output: {CONFIG['STAGE3_WIPED_OUTPUT_CSV']}")

    batch_size = int(CONFIG["STAGE3_WRITE_BATCH_SIZE"])
    region_kept = []
    region_wiped = []

    with open(CONFIG["STAGE2_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for idx, row in enumerate(r, 1):
            seller_id = (row.get("seller id") or "").strip()
            print(f"\n📍 [S3] row #{idx} | seller id: {seller_id or 'null'}")

            region = "null"
            seller_rating = "null"
            seller_review_count = "null"
            seller_description = "null"
            seller_about = "null"

            if seller_id and seller_id.lower() != "null":
                data = fetch_json(build_seller_url(seller_id))
                details = (data or {}).get("seller_profile", {}).get("seller_details", {})
                if isinstance(details, dict):
                    rows = details.get("business_address_rows")
                    if isinstance(rows, list) and rows:
                        region = null_str(rows[-1]).upper()
                    seller_rating = null_str(details.get("rating"))
                    seller_review_count = null_str(details.get("ratings_total"))
                    seller_description = seller_text_field(details.get("detailed_information"))
                    seller_about = seller_text_field(details.get("about_this_seller"))

            out = {h: row.get(h, "null") for h in STAGE2_HEADERS}
            out["seller region"] = region
            out["seller rating"] = seller_rating
            out["seller review count"] = seller_review_count
            out["seller description"] = seller_description
            out["seller about"] = seller_about

            region_value = (region or "").strip().upper()
            should_wipe = (not region_value) or (region_value == "NULL") or (region_value not in ALLOWED_REGIONS)
            if should_wipe:
                out["rank"] = ""
                region_wiped.append(out)
            else:
                region_kept.append(out)

            time.sleep(float(CONFIG["WAIT_BETWEEN_REQUESTS"]))

    final_kept, cap_wiped = stage3_apply_seller_cap_and_rank(region_kept)
    all_wiped = region_wiped + cap_wiped

    print(f"\n   📊 Region kept: {len(region_kept)} | Seller-cap wiped: {len(cap_wiped)} | Final main: {len(final_kept)}")

    mode_keep = "w"
    mode_wipe = "w"
    kept = 0
    wiped = 0

    for i in range(0, len(final_kept), batch_size):
        chunk = final_kept[i : i + batch_size]
        flush_rows(CONFIG["STAGE3_OUTPUT_CSV"], STAGE3_HEADERS, chunk, mode_keep)
        kept += len(chunk)
        print(f"   ✍️  Flushed kept {len(chunk)} rows (total written: {kept})")
        mode_keep = "a"

    for i in range(0, len(all_wiped), batch_size):
        chunk = all_wiped[i : i + batch_size]
        flush_rows(CONFIG["STAGE3_WIPED_OUTPUT_CSV"], STAGE3_HEADERS, chunk, mode_wipe)
        wiped += len(chunk)
        print(f"   ✍️  Flushed wiped {len(chunk)} rows (total written: {wiped})")
        mode_wipe = "a"

    print(f"\n✅ STAGE 3 complete. Main: {kept} | Wiped: {wiped}")


def stage4():
    print("\n" + "=" * 90)
    print("STAGE 4 - GPT extract contacts + person in charge (about + description)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['FINAL_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")

    mode = "w"
    gpt_batch = []
    written = 0
    gpt_batch_size = int(CONFIG["STAGE4_BATCH_SIZE"])

    with open(CONFIG["STAGE3_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        def flush_gpt_batch(items):
            nonlocal mode, written
            if not items:
                return
            print(f"\n   🤖 Calling GPT for {len(items)} rows...")
            parsed = call_gpt_stage4_with_retry(items, max_retries=3)
            batch_rows = []

            for i, row in enumerate(items):
                email, phone, incharge, title = parsed[i]
                email = gpt_text_field(email)
                incharge = gpt_text_field(incharge)
                title = gpt_text_field(title)
                phone = phone_for_sheet(phone)

                out = {h: row.get(h, "null") for h in STAGE2_HEADERS + ["seller region", "seller rating", "seller review count"]}
                out["seller email"] = email
                out["seller number"] = phone
                out["seller incharge person"] = incharge
                out["seller person title"] = title
                out["rank"] = (row.get("rank") or "").strip()
                batch_rows.append(out)

            # Flush immediately after each GPT run
            flush_rows(CONFIG["FINAL_OUTPUT_CSV"], STAGE4_HEADERS, batch_rows, mode)
            written += len(batch_rows)
            print(f"   ✍️  Flushed {len(batch_rows)} rows after GPT run (total written: {written})")
            mode = "a"
            time.sleep(float(CONFIG["WAIT_BETWEEN_BATCHES"]))

        for idx, row in enumerate(reader, 1):
            print(f"\n📍 [S4] row #{idx}")
            gpt_batch.append(row)
            if len(gpt_batch) >= gpt_batch_size:
                flush_gpt_batch(gpt_batch)
                gpt_batch = []

        if gpt_batch:
            flush_gpt_batch(gpt_batch)
    print(f"\n✅ STAGE 4 complete. Rows written: {written}")


def main():
    start = time.time()
    print("\n" + "=" * 90)
    print("AMAZON SELLER PIPELINE (4 STAGES)")
    print("=" * 90)
    print(f"🔎 Keyword: {CONFIG['KEYWORD']}")
    print(f"📄 Pages:   {CONFIG['TOTAL_PAGES']}")
    print(f"📁 S1:      {CONFIG['STAGE1_OUTPUT_CSV']}")
    print(f"📁 S2:      {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 S3 main: {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 S3 wipe: {CONFIG['STAGE3_WIPED_OUTPUT_CSV']}")
    print(f"📁 Final:   {CONFIG['FINAL_OUTPUT_CSV']}")

    stage1()
    stage2()
    stage3()
    stage4()

    elapsed = time.time() - start
    print("\n" + "=" * 90)
    print("🎉 PIPELINE COMPLETE")
    print(f"⏱️  Total time: {elapsed:.2f}s ({elapsed/60:.2f} min)")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    # Load .env and prefix overrides
    load_dotenv(Path(__file__).resolve().parent / ".env")

    # Fixed prefix for this script: SELLER_*
    override_from_env(CONFIG, env_prefix="SELLER_")

    API_KEY = (os.getenv("API_KEY") or "").strip()
    if not API_KEY:
        raise ValueError("API_KEY not found in .env file.")
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not found in .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)

    main()
