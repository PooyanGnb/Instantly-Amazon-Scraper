"""
Amazon Import Products Pipeline (5 stages)

Stage 1:
- Read ImportProducts-style CSV; map columns via .env (IMPORT_* prefix)
- Classify rows as seller vs reseller (Amazon retail, brand-seller match only)
- Output: sellers CSV + resellers CSV (sorted by seller name)

Stage 2:
- Product endpoint per ASIN; append Scraped Seller Name/ID/URL
- One scrape API call per input seller name; 10 parallel workers
- On empty/incomplete product page (API failure), try next ASIN for that seller

Stage 3:
- Seller profile per Scraped Seller ID; region filter DE/CH/AT only
- One profile API call per Scraped Seller ID; 10 parallel workers

Stage 4:
- GPT extract contacts from seller about + description (one GPT call per unique seller)
- 10 parallel workers; 429 retry with backoff

Stage 5:
- Apollo org search: domain from seller email, then name+location fallback (city/state/country)
  when IMPORT_APOLLO_SEARCH_BY_NAME_FALLBACK=true; match Stage 4 person + GPT outreach pick
- One Apollo+GPT chain per unique seller; 4 parallel workers; Apollo rate limit 180/min
"""

import argparse
import csv
import html
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    "COLUMN_CITY": ["Sitz", "City", "city"],
    "COLUMN_STATE": ["Bundesland", "State", "state"],
    "COLUMN_COUNTRY": ["Seller Country/Region", "seller region"],
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
    "STAGE2_WORKERS": 10,
    "STAGE3_WORKERS": 10,
    "STAGE4_WORKERS": 10,
    "STAGE5_WORKERS": 4,
    "API_HTTP_RETRIES": 5,
    "GPT_HTTP_RETRIES": 5,
    "APOLLO_BASE_URL": "https://api.apollo.io/api/v1",
    "APOLLO_API_TIMEOUT": 60,
    "APOLLO_HTTP_RETRIES": 3,
    "APOLLO_RATE_LIMIT_PER_MIN": 180,
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
- When multiple emails or phone numbers appear, pick exactly ONE email and ONE phone for the chosen person.
- Prefer extracting both email and phone from the SAME section (seller about OR seller description) when that section contains both for the chosen person.
- If only one of email/phone exists in that section, take the missing field from the other section.
- If multiple emails or phones exist without a clear person link, still prefer a pair from the same section before mixing across sections.
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


def write_csv_header_only(path: str, headers: List[str]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=headers, extrasaction="ignore").writeheader()


_scrape_request_progress: Optional[Any] = None


class RequestProgress:
    """Thread-safe counter for parallel scrape API requests."""

    def __init__(self, total: int, label: str = "request"):
        self.total = max(0, int(total))
        self.done = 0
        self._lock = threading.Lock()
        self.label = label

    def begin(self) -> str:
        with self._lock:
            self.done += 1
            current = self.done
        remaining = max(0, self.total - current)
        return f"[{current}/{self.total} {self.label}, {remaining} remaining]"


def _set_scrape_request_progress(progress: Optional[RequestProgress]) -> None:
    global _scrape_request_progress
    _scrape_request_progress = progress


def _log_scrape_request(detail: str) -> None:
    progress = _scrape_request_progress
    if progress is not None:
        print(f"   {progress.begin()} {detail}")
    else:
        print(f"   {detail}")


def _scrape_fetch_json(url: str):
    fn = getattr(seller_sp, "fetch_json_with_retry", None)
    if callable(fn):
        return fn(url)
    return seller_sp.fetch_json(url)


def _is_scrape_api_failure(data: Optional[dict]) -> bool:
    """True when the scrape failed and another seller ASIN should be tried."""
    if not data or not isinstance(data, dict):
        return True
    if data.get("status") == "Failure":
        return True
    status_code = data.get("status_code")
    if status_code is not None:
        try:
            if int(status_code) >= 400:
                return True
        except (TypeError, ValueError):
            pass
    err = str(data.get("error") or data.get("message") or "").lower()
    if err and ("empty" in err or "incomplete" in err):
        return True
    return False


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
        "APOLLO_RATE_LIMIT_PER_MIN",
        "API_HTTP_RETRIES",
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


def seller_dedup_key(row: dict) -> str:
    seller_id = scraped_seller_id_cache_key(row)
    if seller_id:
        return f"id:{seller_id}"
    raw_name = (row.get("Scraped Seller Name") or row.get("Seller") or "").strip()
    clean_name = seller_sp.extract_clean_company_name(raw_name)
    if not clean_name and raw_name and raw_name.lower() != "null":
        clean_name = re.sub(r"\s+", " ", html.unescape(raw_name)).strip()
    return f"name:{normalize_name_key(clean_name or raw_name)}"


def stage_work_key(stage: int, row: dict) -> str:
    if stage == 2:
        return seller_name_cache_key(row) or "__no_seller_name__"
    if stage == 3:
        return scraped_seller_id_cache_key(row) or "__no_seller_id__"
    return seller_dedup_key(row)


def build_seller_work_groups(
    rows: List[dict], key_fn: Callable[[dict], str]
) -> Tuple[Dict[str, dict], List[str]]:
    representatives: Dict[str, dict] = {}
    row_keys: List[str] = []
    for row in rows:
        key = key_fn(row)
        row_keys.append(key)
        if key not in representatives:
            representatives[key] = row
    return representatives, row_keys


def build_seller_row_groups(
    rows: List[dict], key_fn: Callable[[dict], str]
) -> Tuple[Dict[str, List[dict]], List[str]]:
    groups: Dict[str, List[dict]] = {}
    row_keys: List[str] = []
    for row in rows:
        key = key_fn(row)
        row_keys.append(key)
        groups.setdefault(key, []).append(row)
    return groups, row_keys


def _collect_row_asins(rows: List[dict]) -> List[str]:
    seen: set = set()
    asins: List[str] = []
    for row in rows:
        asin = (row.get(ASIN_COL) or "").strip().upper()
        if asin and asin.lower() != "null" and asin not in seen:
            seen.add(asin)
            asins.append(asin)
    return asins


def run_seller_worker_pool(
    work_items: List[Tuple[str, dict]],
    process_fn: Callable[[str, dict], Any],
    max_workers: int,
    label: str,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    workers = max(1, int(max_workers))
    print(f"   Parallel {label}: {len(work_items)} unique sellers, {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_key = {
            executor.submit(process_fn, key, row): key for key, row in work_items
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
                print(f"   ✓ Completed seller {key}")
            except Exception as e:
                print(f"      ❌ Worker error for {key}: {e}")
                results[key] = None
    return results


def _partition_work_chunks(
    work_items: List[Tuple[str, dict]], num_workers: int
) -> List[List[Tuple[str, dict]]]:
    workers = max(1, int(num_workers))
    chunks: List[List[Tuple[str, dict]]] = [[] for _ in range(workers)]
    for i, item in enumerate(work_items):
        chunks[i % workers].append(item)
    return [chunk for chunk in chunks if chunk]


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


def _is_gpt_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "rate_limit" in msg:
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429


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
    max_retries = int(CONFIG.get("GPT_HTTP_RETRIES", 5))
    last_err = None
    for attempt in range(1, max_retries + 1):
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
            last_err = e
            if _is_gpt_rate_limit_error(e) and attempt < max_retries:
                wait = min(2 ** attempt, 30)
                print(f"      ⚠️  GPT rate limit; waiting {wait:.1f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"      ❌ GPT error: {e}")
            return ""
    print(f"      ❌ GPT error after retries: {last_err}")
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


def polish_phone_for_sheet(v: str) -> str:
    """Normalize phone to +{country}{number} digits; prefix with ' for CSV/sheets."""
    s = (v or "").strip()
    if s.startswith("'"):
        s = s[1:].strip()
    if not s or s.lower() in ("null", "none", "n/a", "na", "-"):
        return "null"

    international = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return "null"

    if digits.startswith("00"):
        digits = digits[2:]
        international = True

    if international or digits.startswith("49"):
        if digits.startswith("49"):
            rest = digits[2:]
            if rest.startswith("0"):
                rest = rest[1:]
            digits = "49" + rest
        elif digits.startswith("0") and len(digits) > 1:
            digits = "49" + digits[1:]
        num = "+" + digits
    else:
        num = "+" + digits if s.startswith("+") else digits

    return "'" + num


def _extract_product_seller_fields(data: dict) -> Tuple[str, str, str]:
    seller_name = seller_id = seller_link = "null"
    product = data.get("product_results", {})
    buybox = product.get("buybox", {}) if isinstance(product, dict) else {}
    fulfillment = buybox.get("fulfillment", {}) if isinstance(buybox, dict) else {}
    tps = fulfillment.get("third_party_seller", {}) if isinstance(fulfillment, dict) else {}
    if isinstance(tps, dict) and (tps.get("id") or tps.get("name") or tps.get("link")):
        seller_id = seller_sp.null_str(tps.get("id"))
        seller_name = seller_sp.null_str(tps.get("name"))
        seller_link = seller_sp.null_str(tps.get("link"))
    else:
        seller_name = seller_sp.null_str(product.get("sold_by"))
    return seller_name, seller_id, seller_link


def _scrape_product_asin(asin: str) -> Tuple[str, str, str, bool]:
    """Returns seller fields and api_failed (True => try next ASIN for this seller)."""
    data = _scrape_fetch_json(seller_sp.build_product_url(asin))
    if _is_scrape_api_failure(data):
        return "null", "null", "null", True
    return (*_extract_product_seller_fields(data), False)


def _fetch_stage2_seller_scrape_group(key: str, group_rows: List[dict]) -> Tuple[str, str, str]:
    asins = _collect_row_asins(group_rows)
    best = ("null", "null", "null")
    for idx, asin in enumerate(asins):
        _log_scrape_request(f"Product scrape ASIN {asin} (seller: {key})")
        seller_name, seller_id, seller_link, api_failed = _scrape_product_asin(asin)
        if api_failed:
            remaining = len(asins) - idx - 1
            if remaining:
                print(f"    ⚠️  ASIN {asin} failed; trying next product ({remaining} left for seller).")
            else:
                print(f"    ⚠️  ASIN {asin} failed; no more products for this seller.")
            continue
        if seller_id != "null":
            return seller_name, seller_id, seller_link
        if seller_name != "null" and best[0] == "null":
            best = (seller_name, seller_id, seller_link)
    return best


def _null_stage3_profile() -> dict:
    return {
        "seller region": "null",
        "seller postal code": "null",
        "seller rating": "null",
        "seller review count": "null",
        "seller description": "null",
        "seller about": "null",
    }


def _fetch_stage3_seller_profile(_key: str, rep_row: dict) -> dict:
    seller_id = scraped_seller_id_raw(rep_row)
    if not seller_id:
        return _null_stage3_profile()

    region = postal = rating = review_count = description = about = "null"
    _log_scrape_request(f"Seller profile scrape ID {seller_id} (seller: {_key})")
    data = _scrape_fetch_json(seller_sp.build_seller_url(seller_id))
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
    return {
        "seller region": region,
        "seller postal code": postal,
        "seller rating": rating,
        "seller review count": review_count,
        "seller description": description,
        "seller about": about,
    }


def _stage4_worker_process(chunk: List[Tuple[str, dict]]) -> Dict[str, Tuple[str, str, str, str]]:
    cache: Dict[str, Tuple[str, str, str, str]] = {}
    batch_size = int(CONFIG["STAGE4_BATCH_SIZE"])
    gpt_batch: List[dict] = []
    gpt_keys: List[str] = []

    def flush_batch(items: List[dict], keys: List[str]):
        if not items:
            return
        print(f"      🤖 GPT batch: {len(items)} sellers")
        parsed = call_gpt_stage4_with_retry(items)
        for i, key in enumerate(keys):
            email, phone, incharge, title = parsed[i]
            cache[key] = (
                seller_sp.gpt_text_field(email),
                polish_phone_for_sheet(phone),
                seller_sp.gpt_text_field(incharge),
                seller_sp.gpt_text_field(title),
            )
        time.sleep(float(CONFIG.get("WAIT_BETWEEN_BATCHES", 1)))

    for key, row in chunk:
        gpt_batch.append(row)
        gpt_keys.append(key)
        if len(gpt_batch) >= batch_size:
            flush_batch(gpt_batch, gpt_keys)
            gpt_batch = []
            gpt_keys = []
    if gpt_batch:
        flush_batch(gpt_batch, gpt_keys)
    return cache


def build_organization_locations(city: str, state: str, country: str) -> List[str]:
    """Build deduped Apollo organization_locations from row fields (city, state, country)."""
    out: List[str] = []
    seen: set = set()
    for raw in (city, state, country):
        v = (raw or "").strip()
        if not v or v.lower() == "null":
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _build_stage5_locations(row: dict) -> List[str]:
    city = pick_column(row, _column_aliases("COLUMN_CITY"))
    state = pick_column(row, _column_aliases("COLUMN_STATE"))
    country = pick_column(row, _column_aliases("COLUMN_COUNTRY"))
    if not country or country.lower() == "null":
        country = (row.get("seller region") or "").strip()
    return build_organization_locations(city, state, country)


def _apollo_name_fallback_enabled() -> bool:
    v = CONFIG.get("APOLLO_SEARCH_BY_NAME_FALLBACK", False)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("0", "false", "no", "off", "none", ""):
        return False
    if s in ("1", "true", "yes", "on"):
        return True
    return False


def _process_stage5_seller(_key: str, rep_row: dict) -> Tuple[str, str, str, str, str]:
    raw_name = (rep_row.get("Scraped Seller Name") or rep_row.get("Seller") or "").strip()
    clean_name = seller_sp.extract_clean_company_name(raw_name)
    if not clean_name and raw_name and raw_name.lower() != "null":
        clean_name = re.sub(r"\s+", " ", html.unescape(raw_name)).strip()

    domain = seller_sp.extract_company_domain_from_email(rep_row.get("seller email") or "")
    locations = _build_stage5_locations(rep_row)
    stage4_person = (rep_row.get("seller incharge person") or "").strip()
    matched_email = matched_title = outreach_name = outreach_title = outreach_email = "null"

    company_name = clean_name or raw_name
    if not domain and not _apollo_name_fallback_enabled():
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email
    if not domain and not company_name:
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    org = seller_sp.apollo_search_company(domain, company_name, locations)
    if not org or not isinstance(org, dict) or not org.get("id"):
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    org_id = org.get("id")
    people = seller_sp.apollo_list_people(org_id)
    if not people:
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    matched = find_apollo_person_match(stage4_person, people)
    if matched and matched.get("id"):
        person = seller_sp.apollo_match_person(matched["id"])
        if person and isinstance(person, dict):
            matched_email = seller_sp.gpt_text_field(person.get("email"))
            matched_title = seller_sp.gpt_text_field(person.get("title"))

    people_lines = []
    for p in people:
        if not isinstance(p, dict):
            continue
        pid = (p.get("id") or "").strip()
        tit = (p.get("title") or "").strip()
        if pid and tit:
            people_lines.append((pid, tit))

    if not people_lines:
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    gpt_raw = seller_sp.call_gpt_stage5_rank_people(clean_name or raw_name or "", people_lines)
    picks = seller_sp.parse_stage5_gpt_people(gpt_raw)
    if not picks:
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    ordered_ids = [p[0] for p in picks]
    verified = seller_sp.pick_verified_apollo_contact(ordered_ids)
    if not verified:
        return matched_email, matched_title, outreach_name, outreach_title, outreach_email

    outreach_name, outreach_title, outreach_email = verified
    return (
        matched_email,
        matched_title,
        seller_sp.gpt_text_field(outreach_name),
        seller_sp.gpt_text_field(outreach_title),
        seller_sp.gpt_text_field(outreach_email),
    )


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
    print(f"👷 Workers: {CONFIG['STAGE2_WORKERS']}")

    sync_seller_module()
    with open(CONFIG["STAGE1_SELLERS_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE2_EXTRA)
    key_fn = lambda row: stage_work_key(2, row)
    seller_groups, row_keys = build_seller_row_groups(rows, key_fn)
    work_items = list(seller_groups.items())
    max_product_requests = sum(len(_collect_row_asins(group_rows)) for group_rows in seller_groups.values())
    print(f"   Max product scrape requests: {max_product_requests} (may stop early per seller on success)")
    _set_scrape_request_progress(RequestProgress(max_product_requests, "product scrapes"))
    try:
        scrape_cache = run_seller_worker_pool(
            work_items,
            _fetch_stage2_seller_scrape_group,
            CONFIG["STAGE2_WORKERS"],
            "stage 2 scrape",
        )
    finally:
        _set_scrape_request_progress(None)
    null_scrape = ("null", "null", "null")
    for key in seller_groups:
        if scrape_cache.get(key) is None:
            scrape_cache[key] = null_scrape

    buf = []
    written = 0
    mode = "w"
    batch_size = int(CONFIG["STAGE2_WRITE_BATCH_SIZE"])
    cache_hits = 0
    seen_keys: set = set()

    for idx, (row, key) in enumerate(zip(rows, row_keys), 1):
        if key in seen_keys:
            cache_hits += 1
        else:
            seen_keys.add(key)
        seller_name, seller_id, seller_link = scrape_cache.get(key, null_scrape)
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

    print(
        f"\n✅ STAGE 2 complete. Rows written: {written} | "
        f"Unique sellers: {len(seller_groups)} | Cache hits: {cache_hits}"
    )


def stage3():
    print("\n" + "=" * 90)
    print("STAGE 3 - Seller profile + region filter (DE/CH/AT)")
    print("=" * 90)
    print(f"📁 Input:        {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 Main output:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Wiped output: {CONFIG['STAGE3_WIPED_OUTPUT_CSV']}")
    print(f"👷 Workers: {CONFIG['STAGE3_WORKERS']}")

    sync_seller_module()
    with open(CONFIG["STAGE2_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE3_EXTRA)
    key_fn = lambda row: stage_work_key(3, row)
    representatives, row_keys = build_seller_work_groups(rows, key_fn)
    work_items = list(representatives.items())
    max_profile_requests = sum(1 for _, row in work_items if scraped_seller_id_raw(row))
    print(f"   Max profile scrape requests: {max_profile_requests}")
    _set_scrape_request_progress(RequestProgress(max_profile_requests, "profile scrapes"))
    try:
        profile_cache = run_seller_worker_pool(
            work_items,
            _fetch_stage3_seller_profile,
            CONFIG["STAGE3_WORKERS"],
            "stage 3 profile",
        )
    finally:
        _set_scrape_request_progress(None)
    null_profile = _null_stage3_profile()
    for key in representatives:
        if profile_cache.get(key) is None:
            profile_cache[key] = null_profile

    region_kept = []
    region_wiped = []
    cache_hits = 0
    seen_keys: set = set()

    for row, key in zip(rows, row_keys):
        if key in seen_keys:
            cache_hits += 1
        else:
            seen_keys.add(key)
        profile = profile_cache.get(key, null_profile)
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
        if not data:
            write_csv_header_only(path, headers)
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            flush_rows(path, headers, chunk, mode)
            if label == "kept":
                kept_n += len(chunk)
            else:
                wiped_n += len(chunk)
            mode = "a"

    print(
        f"\n✅ STAGE 3 complete. Main: {kept_n} | Wiped: {wiped_n} | "
        f"Unique sellers: {len(representatives)} | Cache hits: {cache_hits}"
    )


def stage4():
    print("\n" + "=" * 90)
    print("STAGE 4 - GPT extract contacts (about + description)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE4_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")
    print(f"👷 Workers: {CONFIG['STAGE4_WORKERS']}")

    stage3_path = Path(CONFIG["STAGE3_OUTPUT_CSV"])
    if not stage3_path.exists():
        print(f"   ⚠️  Stage 3 main output missing: {stage3_path}")
        print("   ⏭️  Stage 4 skipped — re-run stage 3 after stage 2 succeeds.")
        return

    with open(CONFIG["STAGE3_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    if not rows:
        print("   ⚠️  Stage 3 main output is empty — no rows to process.")
        write_csv_header_only(
            CONFIG["STAGE4_OUTPUT_CSV"],
            build_headers(
                [h for h in base_headers if h not in {"seller description", "seller about"}],
                STAGE4_EXTRA,
            ),
        )
        print(f"\n✅ STAGE 4 complete. Rows written: 0")
        return

    drop_cols = {"seller description", "seller about"}
    base_out_headers = [h for h in base_headers if h not in drop_cols]
    headers = build_headers(base_out_headers, STAGE4_EXTRA)

    key_fn = lambda row: stage_work_key(4, row)
    representatives, row_keys = build_seller_work_groups(rows, key_fn)
    work_items = list(representatives.items())
    chunks = _partition_work_chunks(work_items, CONFIG["STAGE4_WORKERS"])
    seller_gpt_cache: Dict[str, Tuple[str, str, str, str]] = {}
    null_gpt = ("null", "null", "null", "null")

    print(f"   Parallel stage 4 GPT: {len(work_items)} unique sellers, {len(chunks)} worker chunks")
    with ThreadPoolExecutor(max_workers=max(1, int(CONFIG["STAGE4_WORKERS"]))) as executor:
        futures = [executor.submit(_stage4_worker_process, chunk) for chunk in chunks]
        for future in as_completed(futures):
            try:
                seller_gpt_cache.update(future.result())
            except Exception as e:
                print(f"      ❌ Stage 4 worker chunk error: {e}")

    for key in representatives:
        if key not in seller_gpt_cache:
            seller_gpt_cache[key] = null_gpt

    mode = "w"
    written = 0
    cache_hits = 0
    seen_keys: set = set()
    buf: List[dict] = []
    batch_size = int(CONFIG["STAGE4_BATCH_SIZE"])

    for row, key in zip(rows, row_keys):
        if key in seen_keys:
            cache_hits += 1
        else:
            seen_keys.add(key)
        email, phone, incharge, title = seller_gpt_cache[key]
        out = {h: row.get(h, "null") for h in base_out_headers}
        out["seller email"] = email
        out["seller number"] = phone
        out["seller incharge person"] = incharge
        out["seller person title"] = title
        buf.append(out)

        if len(buf) >= batch_size:
            flush_rows(CONFIG["STAGE4_OUTPUT_CSV"], headers, buf, mode)
            written += len(buf)
            print(f"   ✍️  Flushed {len(buf)} rows (total written: {written})")
            buf = []
            mode = "a"

    if buf:
        flush_rows(CONFIG["STAGE4_OUTPUT_CSV"], headers, buf, mode)
        written += len(buf)
        print(f"   ✍️  Final flush {len(buf)} rows (total written: {written})")

    print(
        f"\n✅ STAGE 4 complete. Rows written: {written} | "
        f"Unique sellers: {len(representatives)} | Cache hits: {cache_hits}"
    )


def stage5():
    print("\n" + "=" * 90)
    print("STAGE 5 - Apollo enrich (matched contact + outreach pick)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE4_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE5_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")
    print(f"👷 Workers: {CONFIG['STAGE5_WORKERS']}")

    sync_seller_module()

    with open(CONFIG["STAGE4_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_headers = list(reader.fieldnames or [])
        rows = list(reader)

    headers = build_headers(base_headers, STAGE5_EXTRA)
    key_fn = lambda row: stage_work_key(5, row)
    representatives, row_keys = build_seller_work_groups(rows, key_fn)
    work_items = list(representatives.items())
    seller_cache = run_seller_worker_pool(
        work_items,
        _process_stage5_seller,
        CONFIG["STAGE5_WORKERS"],
        "stage 5 Apollo+GPT",
    )
    null_stage5 = ("null", "null", "null", "null", "null")
    skipped = 0
    for key in representatives:
        result = seller_cache.get(key)
        if result is None:
            seller_cache[key] = null_stage5
            skipped += 1
        elif result == null_stage5:
            skipped += 1

    buf = []
    written = 0
    cache_hits = 0
    seen_keys: set = set()
    mode = "w"
    batch_size = int(CONFIG["STAGE5_WRITE_BATCH_SIZE"])

    for row, seller_key in zip(rows, row_keys):
        if seller_key in seen_keys:
            cache_hits += 1
        else:
            seen_keys.add(seller_key)
        matched_email, matched_title, outreach_name, outreach_title, outreach_email = seller_cache.get(
            seller_key, null_stage5
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

    print(
        f"\n✅ STAGE 5 complete. Rows written: {written} | "
        f"Unique sellers: {len(representatives)} | Skipped sellers: {skipped} | Cache hits: {cache_hits}"
    )


STAGES = {
    1: stage1,
    2: stage2,
    3: stage3,
    4: stage4,
    5: stage5,
}

LAST_STAGE = max(STAGES)


def resolve_stages_to_run(
    only_stage: Optional[int] = None,
    exclude_from_stage: Optional[int] = None,
) -> List[int]:
    if only_stage is not None:
        return [only_stage]
    if exclude_from_stage is not None:
        return list(range(1, exclude_from_stage))
    return list(range(1, LAST_STAGE + 1))


def main(
    only_stage: Optional[int] = None,
    exclude_from_stage: Optional[int] = None,
):
    stages_to_run = resolve_stages_to_run(only_stage, exclude_from_stage)
    start = time.time()
    print("\n" + "=" * 90)
    print("AMAZON IMPORT PRODUCTS PIPELINE (5 STAGES)")
    print("=" * 90)
    print(f"📁 Input:     {CONFIG['INPUT_CSV']}")
    print(f"📁 S1 sellers:{CONFIG['STAGE1_SELLERS_OUTPUT_CSV']}")
    print(f"📁 S1 resell: {CONFIG['STAGE1_RESELLERS_OUTPUT_CSV']}")
    print(f"📁 S5 final:  {CONFIG['STAGE5_OUTPUT_CSV']}")
    if exclude_from_stage is not None:
        print(f"▶️  Running stages: {stages_to_run} (excluding {exclude_from_stage}–{LAST_STAGE})")

    for n in stages_to_run:
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
    parser.add_argument(
        "--exclude-from-stage",
        type=int,
        choices=[2, 3, 4, 5, 6],
        metavar="N",
        help="Run stages 1 through N-1, skipping N and later (e.g. 5 runs stages 1-4)",
    )
    args = parser.parse_args()
    if args.stage is not None and args.exclude_from_stage is not None:
        parser.error("Cannot use --stage together with --exclude-from-stage")

    stages_to_run = resolve_stages_to_run(args.stage, args.exclude_from_stage)

    API_KEY = (os.getenv("API_KEY") or "").strip()
    if not API_KEY and any(n >= 2 for n in stages_to_run):
        raise ValueError("API_KEY not found in .env file.")
    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not OPENAI_API_KEY and any(n >= 4 for n in stages_to_run):
        raise ValueError("OPENAI_API_KEY not found in .env file.")
    APOLLO_API_KEY = (os.getenv("APOLLO_API_KEY") or "").strip()
    if not APOLLO_API_KEY and 5 in stages_to_run:
        raise ValueError("APOLLO_API_KEY not found in .env file.")

    if OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY)
    seller_sp.load_public_email_domains()

    main(only_stage=args.stage, exclude_from_stage=args.exclude_from_stage)
