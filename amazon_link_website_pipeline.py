"""
Amazon Link + Website contact pipeline.

Input CSV with two configurable columns: Amazon link and Website.
Per row:
  1. Extract seller ID (+ ASIN) from the Amazon link.
  2. Amazon stage 2 (import pipeline): product scrape by ASIN → Scraped Seller Name/ID/URL.
  3. Amazon seller profile scrape (stage 3) + GPT contact extract from about/description (stage 4).
  4. Website: GPT with web_search tool (max 5 tool calls) to find one contact person
     (name, email, phone, title) from contact/about/team pages; prefer roles tied to
     Amazon listings / product visuals when multiple people exist.

Runs with 10 parallel workers by default.
"""

import csv
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import amazon_import_products_pipeline as imp
import amazon_seller_pipeline as seller_sp
from dotenv import load_dotenv
from openai import OpenAI

from config_env import override_from_env

CONFIG = {
    "INPUT_CSV": "data/mobinaenriched.csv",
    "OUTPUT_CSV": "data/link_website_output.csv",
    "COLUMN_AMAZON_LINK": ["Amazon Page", "amazon link", "Amazon URL"],
    "COLUMN_WEBSITE": ["Website", "website", "Company Website"],
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
    "WRITE_BATCH_SIZE": 50,
    "WORKERS": 10,
    "API_HTTP_RETRIES": 5,
    "GPT_HTTP_RETRIES": 5,
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 15000,
    "MAX_TOOL_CALLS": 5,
}

SELLER_ID_FROM_URL_RE = re.compile(r"[?&]seller=([A-Z0-9]+)", re.IGNORECASE)

EXTRACTED_COLS = ["Extracted Seller ID", "Extracted ASIN"]
STAGE2_COLS = ["Scraped Seller Name", "Scraped Seller ID", "Scraped Seller URL"]
STAGE3_COLS = [
    "seller region",
    "seller postal code",
    "seller rating",
    "seller review count",
    "seller description",
    "seller about",
]
STAGE4_COLS = [
    "seller email",
    "seller number",
    "seller incharge person",
    "seller person title",
]
WEBSITE_COLS = [
    "website person name",
    "website person email",
    "website person phone",
    "website person title",
]
OUTPUT_EXTRA = EXTRACTED_COLS + STAGE2_COLS + STAGE3_COLS + STAGE4_COLS + WEBSITE_COLS

SYSTEM_PROMPT_WEBSITE = """You find ONE best business contact from a company's website using web search.

Context:
- We are a visual / creative studio helping Amazon sellers improve product visuals, A+ content, infographics, listing images, and conversion-focused creative.
- You receive a company website URL and optional Amazon seller context (seller name, about text).

Task:
- Use web search to browse the website and related pages (contact, impressum, about us, team, leadership, management, etc.).
- Find exactly ONE person with their email, phone number, and job title when available on the site.
- Use ONLY information you find on the website or clearly attributed official pages from that domain. Do not invent contacts.
- If multiple people are found, choose the single person most relevant to Amazon marketplace, e-commerce, marketing, brand, content, creative, design, product visuals, merchandising, or marketplace operations.
- If no suitable person is found, output null for all fields.

Output format (exactly one line, semicolon-separated, no markdown):
website person name;website person email;website person phone;website person title
Use lowercase null for missing values.
"""

API_KEY = ""
client = None

_amazon_cache: Dict[str, dict] = {}
_website_cache: Dict[str, Tuple[str, str, str, str]] = {}
_cache_lock = threading.Lock()


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


def extract_seller_id_from_link(value: str) -> str:
    if not value or not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    match = SELLER_ID_FROM_URL_RE.search(s)
    if match:
        return match.group(1).strip().upper()
    if re.match(r"^[A-Z0-9]{10,20}$", s, re.IGNORECASE):
        return s.upper()
    return ""


def normalize_website_key(url: str) -> str:
    s = (url or "").strip().lower()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    parsed = urlparse(s)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def build_seller_profile_url(seller_id: str) -> str:
    domain = (CONFIG.get("AMAZON_DOMAIN") or "amazon.de").strip()
    sid = (seller_id or "").strip()
    if not sid or sid.lower() == "null":
        return "null"
    return f"https://www.{domain}/sp?seller={sid}"


def sync_pipeline_modules():
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
        "API_HTTP_RETRIES",
        "GPT_HTTP_RETRIES",
    )
    for k in shared_keys:
        if k in CONFIG:
            imp.CONFIG[k] = CONFIG[k]
            seller_sp.CONFIG[k] = CONFIG[k]
    imp.API_KEY = API_KEY
    seller_sp.API_KEY = API_KEY
    imp.client = client
    seller_sp.client = client
    seller_sp.load_public_email_domains()
    imp.sync_seller_module()


def _is_gpt_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "rate_limit" in msg:
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429


def _null_amazon_bundle() -> dict:
    out = {
        "Extracted Seller ID": "null",
        "Extracted ASIN": "null",
        "Scraped Seller Name": "null",
        "Scraped Seller ID": "null",
        "Scraped Seller URL": "null",
    }
    out.update(imp._null_stage3_profile())
    out["seller email"] = "null"
    out["seller number"] = "null"
    out["seller incharge person"] = "null"
    out["seller person title"] = "null"
    return out


def _run_amazon_enrichment(seller_id: str, asin: str) -> dict:
    scraped_name = scraped_id = scraped_url = "null"
    if asin:
        scraped_name, scraped_id, scraped_url, api_failed = imp._scrape_product_asin(asin)
        if api_failed:
            scraped_name = scraped_id = scraped_url = "null"

    profile_seller_id = scraped_id if scraped_id != "null" else seller_id
    if not profile_seller_id:
        profile_seller_id = ""

    if scraped_url == "null" and profile_seller_id:
        scraped_url = build_seller_profile_url(profile_seller_id)

    profile = imp._null_stage3_profile()
    if profile_seller_id:
        rep_row = {"Scraped Seller ID": profile_seller_id}
        profile = imp._fetch_stage3_seller_profile(profile_seller_id, rep_row)

    email = phone = person = title = "null"
    if (profile.get("seller about") or "").strip().lower() != "null" or (
        profile.get("seller description") or ""
    ).strip().lower() != "null":
        rows = imp.call_gpt_stage4_with_retry([profile])
        if rows:
            email, phone, person, title = rows[0]
            email = seller_sp.gpt_text_field(email)
            phone = imp.polish_phone_for_sheet(phone)
            person = seller_sp.gpt_text_field(person)
            title = seller_sp.gpt_text_field(title)

    out = {
        "Extracted Seller ID": seller_sp.null_str(seller_id) if seller_id else "null",
        "Extracted ASIN": seller_sp.null_str(asin) if asin else "null",
        "Scraped Seller Name": scraped_name,
        "Scraped Seller ID": scraped_id if scraped_id != "null" else seller_sp.null_str(profile_seller_id),
        "Scraped Seller URL": scraped_url,
    }
    out.update(profile)
    out["seller email"] = email
    out["seller number"] = phone
    out["seller incharge person"] = person
    out["seller person title"] = title
    return out


def get_amazon_enrichment(seller_id: str, asin: str) -> dict:
    cache_key = f"{seller_id}|{asin}"
    with _cache_lock:
        if cache_key in _amazon_cache:
            return dict(_amazon_cache[cache_key])
    result = _run_amazon_enrichment(seller_id, asin)
    with _cache_lock:
        _amazon_cache[cache_key] = dict(result)
    return result


def parse_website_gpt_response(text: str) -> Tuple[str, str, str, str]:
    null4 = ("null", "null", "null", "null")
    if not text:
        return null4
    for ln in text.splitlines():
        s = ln.strip().strip("`")
        if not s or s.lower().startswith("website person"):
            continue
        if s.startswith("#") or s.startswith("---"):
            continue
        parts = [p.strip() for p in s.split(";")]
        if len(parts) >= 4:
            return tuple(seller_sp.gpt_text_field(p) for p in parts[:4])
    return null4


def call_gpt_website_contacts(
    website: str,
    seller_name: str,
    seller_about: str,
    seller_description: str,
) -> Tuple[str, str, str, str]:
    if not website or website.lower() in ("null", "none", "n/a", "-"):
        return "null", "null", "null", "null"

    user_prompt = "\n".join(
        [
            f"Website URL: {website}",
            f"Amazon seller name (context only): {seller_name or 'null'}",
            f"Amazon seller about (context only): {(seller_about or 'null')[:1500]}",
            f"Amazon seller description (context only): {(seller_description or 'null')[:1500]}",
            "",
            "Search the website and find one best contact person for Amazon/creative outreach.",
            "Output exactly one line:",
            "website person name;website person email;website person phone;website person title",
        ]
    )

    max_retries = int(CONFIG.get("GPT_HTTP_RETRIES", 5))
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.responses.create(
                model=CONFIG["MODEL"],
                reasoning={"effort": CONFIG["REASONING_EFFORT"]},
                tools=[{"type": "web_search"}],
                max_tool_calls=int(CONFIG.get("MAX_TOOL_CALLS", 5)),
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT_WEBSITE},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=int(CONFIG["MAX_OUTPUT_TOKENS"]),
            )
            return parse_website_gpt_response((response.output_text or "").strip())
        except Exception as e:
            last_err = e
            if _is_gpt_rate_limit_error(e) and attempt < max_retries:
                wait = min(2 ** attempt, 30)
                print(f"      ⚠️  Website GPT rate limit; waiting {wait:.1f}s ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"      ❌ Website GPT error for {website}: {e}")
            return "null", "null", "null", "null"
    print(f"      ❌ Website GPT failed after retries for {website}: {last_err}")
    return "null", "null", "null", "null"


def get_website_contacts(
    website: str,
    seller_name: str,
    seller_about: str,
    seller_description: str,
) -> Tuple[str, str, str, str]:
    key = normalize_website_key(website)
    if not key:
        return "null", "null", "null", "null"
    with _cache_lock:
        if key in _website_cache:
            return _website_cache[key]
    result = call_gpt_website_contacts(website, seller_name, seller_about, seller_description)
    with _cache_lock:
        _website_cache[key] = result
    return result


def process_row(row: dict) -> dict:
    amazon_link = pick_column(row, _column_aliases("COLUMN_AMAZON_LINK"))
    website = pick_column(row, _column_aliases("COLUMN_WEBSITE"))

    seller_id = extract_seller_id_from_link(amazon_link)
    asin = imp.extract_asin(amazon_link)

    if not seller_id and not asin:
        amazon_data = _null_amazon_bundle()
    else:
        amazon_data = get_amazon_enrichment(seller_id, asin)

    seller_name = amazon_data.get("Scraped Seller Name") or "null"
    seller_about = amazon_data.get("seller about") or "null"
    seller_description = amazon_data.get("seller description") or "null"

    web_name, web_email, web_phone, web_title = get_website_contacts(
        website, seller_name, seller_about, seller_description
    )
    web_phone = imp.polish_phone_for_sheet(web_phone)

    out = dict(row)
    out.update(amazon_data)
    out["website person name"] = web_name
    out["website person email"] = web_email
    out["website person phone"] = web_phone
    out["website person title"] = web_title
    return out


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


def flush_rows(path: str, headers: List[str], rows: List[dict], mode: str):
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


def run():
    print("\n" + "=" * 90)
    print("AMAZON LINK + WEBSITE CONTACT PIPELINE")
    print("=" * 90)
    print(f"📁 Input:   {CONFIG['INPUT_CSV']}")
    print(f"📁 Output:  {CONFIG['OUTPUT_CSV']}")
    print(f"👷 Workers: {CONFIG['WORKERS']}")
    print(f"🔍 Web search max tool calls per record: {CONFIG['MAX_TOOL_CALLS']}")

    sync_pipeline_modules()
    base_headers, rows = read_input_rows(CONFIG["INPUT_CSV"])
    if not rows:
        print("   ⚠️  No input rows.")
        headers = imp.build_headers(base_headers, OUTPUT_EXTRA)
        flush_rows(CONFIG["OUTPUT_CSV"], headers, [], "w")
        return

    headers = imp.build_headers(base_headers, OUTPUT_EXTRA)
    workers = max(1, int(CONFIG["WORKERS"]))
    results: List[Optional[dict]] = [None] * len(rows)

    print(f"   Processing {len(rows)} rows with {workers} workers...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {executor.submit(process_row, row): i for i, row in enumerate(rows)}
        done = 0
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            done += 1
            try:
                results[idx] = future.result()
                print(f"   ✓ Row {idx + 1}/{len(rows)} complete ({done}/{len(rows)})")
            except Exception as e:
                print(f"   ❌ Row {idx + 1} error: {e}")
                out = dict(rows[idx])
                out.update(_null_amazon_bundle())
                out["website person name"] = "null"
                out["website person email"] = "null"
                out["website person phone"] = "null"
                out["website person title"] = "null"
                results[idx] = out

    out_rows = [r for r in results if r is not None]
    batch_size = int(CONFIG.get("WRITE_BATCH_SIZE", 50))
    mode = "w"
    written = 0
    for i in range(0, len(out_rows), batch_size):
        chunk = out_rows[i : i + batch_size]
        flush_rows(CONFIG["OUTPUT_CSV"], headers, chunk, mode)
        written += len(chunk)
        print(f"   ✍️  Flushed {len(chunk)} rows (total written: {written})")
        mode = "a"

    print(
        f"\n✅ Complete. Rows written: {written} | "
        f"Amazon unique keys cached: {len(_amazon_cache)} | "
        f"Website unique domains cached: {len(_website_cache)}"
    )


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    override_from_env(CONFIG, env_prefix="LINKWEB_")

    API_KEY = (os.getenv("API_KEY") or "").strip()
    if not API_KEY:
        raise ValueError("API_KEY not found in .env file.")
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not found in .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)
    run()
