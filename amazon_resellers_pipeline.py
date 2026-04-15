"""
Amazon Resellers 3-Stage Pipeline

Stage 1:
- Read input CSV with "Amazon Link" (URL or ASIN)
- Fetch product endpoint by ASIN
- Append: Amazon Brand, Amazon Title
- Write output in batches of 10

Stage 2:
- Read stage1 output, search by Amazon Brand
- Pick up to 3 different products (same brand, distinct ASIN/title key)
- Append: Product1–3 Title and Link each
- Write output in batches of 5

Stage 3:
- GPT sees 3 candidates, drops one duplicate/weak line, outputs cleaned names for the 2 kept products
- Final CSV unchanged: original input columns + Product1 Link + Product2 Link + Product1 Name + Product2 Name
  (links/names refer to the two GPT-kept products, in ascending slot order 1–3)
- Write output in batches of 5
"""

import csv
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv
from openai import OpenAI


CONFIG = {
    # Files
    "INPUT_CSV": "data/Test Reseller.csv",
    "STAGE1_OUTPUT_CSV": "data/resellers_stage1.csv",
    "STAGE2_OUTPUT_CSV": "data/resellers_stage2.csv",
    "FINAL_OUTPUT_CSV": "data/resellers_final.csv",

    # Row range
    "START_FROM_LINE": None,      # 1-indexed; None = from beginning
    "MAX_RECORDS": None,          # None = all

    # Columns
    "COLUMN_AMAZON_LINK": "Amazon Link",

    # API settings
    "AMAZON_DOMAIN": "amazon.de",
    "API_TIMEOUT": 60,
    "WAIT_BETWEEN_REQUESTS": 2,
    "WAIT_BETWEEN_BATCHES": 2,
    "SCROLL_TO_BOTTOM": "true",
    "WAIT_BOTTOM_CAROUSEL": "true",
    "WAIT_FOR_OFFERS": "true",
    "WAIT_FOR_VIDEO": "true",
    "HTTP": "true",
    "DEVICE": "desktop",
    "PAGE": "1",

    # Stage flush sizes
    "STAGE1_WRITE_BATCH_SIZE": 10,
    "STAGE2_WRITE_BATCH_SIZE": 5,
    "STAGE3_BATCH_SIZE": 5,
    "STAGE3_WRITE_BATCH_SIZE": 5,

    # Search selection
    "SEARCH_MAX_RESULTS": 20,
    "MIN_RATING_PREFERRED": 4.0,

    # GPT settings (same style as gpt.py)
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 5000,
    "USE_WEB_SEARCH": False,
    "WEB_SEARCH_CONTEXT": "small",
    "MAX_TOOL_CALLS": 5,
}

# Load .env first so RESELLER_CONFIG_PREFIX and overrides are available.
load_dotenv(Path(__file__).resolve().parent / ".env")
from config_env import override_from_env

# CONFIG keys are overridden from .env as: {RESELLER_CONFIG_ENV_PREFIX}{KEY}
# Example: RESELLER_INPUT_CSV=data/in.csv, RESELLER_AMAZON_DOMAIN=amazon.de
# Optional meta (in .env or shell): RESELLER_CONFIG_PREFIX=MyApp → MyApp_INPUT_CSV=... (no trailing _ on MyApp)
_rp = (os.getenv("RESELLER_CONFIG_PREFIX") or "RESELLER").strip().rstrip("_")
RESELLER_CONFIG_ENV_PREFIX = f"{_rp}_" if _rp else "RESELLER_"

override_from_env(CONFIG, env_prefix=RESELLER_CONFIG_ENV_PREFIX)

API_KEY = (os.getenv("API_KEY") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

if not API_KEY:
    raise ValueError("API_KEY not found in .env file.")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not found in .env file.")

client = OpenAI(api_key=OPENAI_API_KEY)
BASE_URL = "https://ecom.webscrapingapi.com/v1"

ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)
AMAZON_PATH_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)


def count_records(path):
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return sum(1 for _ in reader)


def extract_asin(value):
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


def build_product_url(asin):
    params = {
        "engine": "amazon",
        "api_key": API_KEY,
        "type": "product",
        "product_id": asin,
        "amazon_domain": CONFIG["AMAZON_DOMAIN"],
        "scroll_to_bottom": CONFIG["SCROLL_TO_BOTTOM"],
        "wait_bottom_carousel": CONFIG["WAIT_BOTTOM_CAROUSEL"],
        "wait_for_offers": CONFIG["WAIT_FOR_OFFERS"],
        "wait_for_video": CONFIG["WAIT_FOR_VIDEO"],
        "http": CONFIG["HTTP"],
        "device": CONFIG["DEVICE"],
        "page": CONFIG["PAGE"],
    }
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{BASE_URL}?{query}"


def build_search_url(query):
    params = {
        "engine": "amazon",
        "api_key": API_KEY,
        "type": "search",
        "q": quote_plus(query),
        "amazon_domain": CONFIG["AMAZON_DOMAIN"],
        "scroll_to_bottom": CONFIG["SCROLL_TO_BOTTOM"],
        "wait_bottom_carousel": CONFIG["WAIT_BOTTOM_CAROUSEL"],
        "wait_for_offers": CONFIG["WAIT_FOR_OFFERS"],
        "wait_for_video": "false",
        "http": CONFIG["HTTP"],
        "device": CONFIG["DEVICE"],
        "page": CONFIG["PAGE"],
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{BASE_URL}?{query_string}"


def fetch_json(url):
    try:
        r = requests.get(url, timeout=CONFIG["API_TIMEOUT"])
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"    ❌ API error: {e}")
        return None


def flush_rows(path, headers, rows, mode):
    if not rows:
        return
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)


def parse_rating(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    m = re.search(r"(\d+[.,]?\d*)", s)
    if not m:
        return 0.0
    return float(m.group(1).replace(",", "."))


def normalize_title(title, brand=""):
    """
    Normalization for duplicate detection.
    Important: do NOT remove color words here, because color variants should be eligible
    as "different" products in Stage 2.
    """
    if not title:
        return ""
    s = title.lower()
    # Remove bracketed meta (e.g. " (Paar, weiß)" )
    s = re.sub(r"\([^)]*\)", " ", s)
    # Remove brand tokens to reduce false mismatches
    for token in [t.strip().lower() for t in brand.split() if t.strip()]:
        s = re.sub(rf"\b{re.escape(token)}\b", " ", s)
    tokens = re.split(r"[^a-z0-9äöüß]+", s)
    cleaned = [t for t in tokens if t]
    return " ".join(cleaned)


def same_product(title_a, title_b, brand=""):
    """
    Fallback heuristic for "same product" when ASINs are missing.
    We use a high threshold so color variants are usually NOT treated as duplicates.
    """
    a = normalize_title(title_a, brand)
    b = normalize_title(title_b, brand)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    score = SequenceMatcher(a=a, b=b).ratio()
    return score >= 0.985


def alphanumeric_key(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", (s or "").lower())


def is_brand_only_title(title: str, brand: str) -> bool:
    """
    True when the title carries no product descriptor beyond the brand
    (e.g. every hit is just "alife & kickin" while brand is AlifeKickin — token strip misses that).
    """
    t = (title or "").strip()
    if not t:
        return True
    tk = alphanumeric_key(title)
    bk = alphanumeric_key(brand)
    if not tk:
        return True
    if bk:
        if tk == bk:
            return True
        # Do not use substring (bk in tk): e.g. brand AMAZONAS would match a long book title.
        if SequenceMatcher(a=tk, b=bk).ratio() >= 0.88:
            return True
    nt = normalize_title(title, brand).strip()
    if not nt:
        return True
    return False


def is_broken_search_link(link: str) -> bool:
    if not link:
        return True
    l = link.lower()
    return "javascript:" in l or "void(0)" in l


def build_dp_url(asin: str) -> str:
    d = (CONFIG.get("AMAZON_DOMAIN") or "amazon.de").strip()
    if not d.startswith("www."):
        d = "www." + d
    return f"https://{d}/dp/{asin}"


def extract_asin_from_search_product(p: dict) -> str:
    link = (p.get("link") or "").strip()
    if link and not is_broken_search_link(link):
        a = extract_asin_from_candidate_link(link)
        if a:
            return a
    pid = (p.get("product_id") or "").strip()
    if len(pid) == 10 and re.match(r"^[A-Z0-9]{10}$", pid, re.I):
        return pid.upper()
    return extract_asin_from_candidate_link(link)


def extract_asin_from_candidate_link(link: str) -> str:
    # Candidate link might be a full URL or already an ASIN.
    # Reuse the same ASIN extractor used for Amazon Link input.
    return extract_asin(link or "")


def _candidate_conflict(existing, c, brand: str) -> bool:
    for x in existing:
        if x.get("asin") and c.get("asin") and x["asin"] == c["asin"]:
            return True
        if c["key"] == x["key"]:
            return True
        if same_product(x["title"], c["title"], brand):
            return True
    return False


def _greedy_pick_distinct(candidates, brand: str, max_n: int):
    picks = []
    for c in candidates:
        if len(picks) >= max_n:
            break
        if _candidate_conflict(picks, c, brand):
            continue
        picks.append(c)
    return picks


def _pick_id(c):
    return c.get("asin") or (c["title"], c["link"])


def choose_three_products(product_results, source_title, brand, source_asin: str):
    """
    Stage 2 product selection:
    - Drop brand-only / non-descriptive titles and broken links (use product_id + canonical /dp/ when needed).
    - Prefer high ratings; dedupe by distinct product key (normalized title beyond brand) and by ASIN.
    - Must not return the source ASIN; picks must differ by ASIN / key / same_product.
    - Returns up to 3 (title, link) pairs; missing slots are empty strings.
    """
    raw = []
    for p in product_results[: int(CONFIG["SEARCH_MAX_RESULTS"])]:
        title = (p.get("title") or "").strip()
        link = (p.get("link") or "").strip()
        if not title:
            continue
        if is_brand_only_title(title, brand):
            continue

        asin = extract_asin_from_search_product(p)
        if is_broken_search_link(link):
            link = build_dp_url(asin) if asin else ""
        if not link:
            continue

        if source_asin and asin and asin == source_asin:
            continue
        if not asin and same_product(title, source_title, brand):
            continue

        rating_data = p.get("rating")
        rating_value = parse_rating(
            rating_data.get("rating") if isinstance(rating_data, dict) else rating_data
        )

        key = normalize_title(title, brand)
        raw.append(
            {
                "asin": asin,
                "title": title,
                "link": link,
                "rating": rating_value,
                "key": key,
            }
        )

    if not raw:
        return ("",) * 6

    # Best rating per ASIN (when ASIN known)
    by_asin = {}
    no_asin = []
    for c in raw:
        a = c.get("asin") or ""
        if a:
            prev = by_asin.get(a)
            if prev is None or c["rating"] > prev["rating"]:
                by_asin[a] = c
        else:
            no_asin.append(c)

    merged = list(by_asin.values()) + no_asin

    # One row per distinct product identity (same title-after-brand = one pick)
    best_by_key = {}
    for c in merged:
        k = c["key"]
        if not k:
            continue
        prev = best_by_key.get(k)
        if prev is None or c["rating"] > prev["rating"]:
            best_by_key[k] = c

    distinct = sorted(best_by_key.values(), key=lambda x: x["rating"], reverse=True)
    if not distinct:
        return ("",) * 6

    preferred = [c for c in distinct if c["rating"] >= float(CONFIG["MIN_RATING_PREFERRED"])]
    pool = preferred if preferred else distinct

    picks = _greedy_pick_distinct(pool, brand, 3)
    if len(picks) < 3:
        picked_ids = {_pick_id(p) for p in picks}
        for c in distinct:
            if len(picks) >= 3:
                break
            if _pick_id(c) in picked_ids:
                continue
            if _candidate_conflict(picks, c, brand):
                continue
            picks.append(c)
            picked_ids.add(_pick_id(c))

    out = []
    for i in range(3):
        if i < len(picks):
            out.extend([picks[i]["title"], picks[i]["link"]])
        else:
            out.extend(["", ""])
    return tuple(out)


def stage1():
    print("\n" + "=" * 90)
    print("STAGE 1 - Product endpoint by ASIN (Amazon Link -> Amazon Brand + Amazon Title)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['INPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE1_OUTPUT_CSV']}")

    total = count_records(CONFIG["INPUT_CSV"])
    print(f"📋 Total input rows: {total}")

    start = CONFIG["START_FROM_LINE"] if CONFIG["START_FROM_LINE"] is not None else 1
    max_records = CONFIG["MAX_RECORDS"]
    print(f"▶️  Start record: {start}")
    print(f"📊 Max records: {max_records if max_records is not None else 'all'}")

    write_batch = []
    written = 0
    processed = 0
    mode = "w"
    headers = None

    with open(CONFIG["INPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        in_headers = list(reader.fieldnames or [])
        headers = in_headers + [h for h in ("Amazon Brand", "Amazon Title") if h not in in_headers]

        for idx, row in enumerate(reader, 1):
            if idx < start:
                continue
            if max_records is not None and processed >= max_records:
                break

            processed += 1
            raw_link = (row.get(CONFIG["COLUMN_AMAZON_LINK"]) or "").strip()
            asin = extract_asin(raw_link)
            print(f"\n📍 [S1] {processed} | record #{idx} | ASIN: {asin or 'N/A'}")

            brand = ""
            title = ""
            if asin:
                url = build_product_url(asin)
                data = fetch_json(url)
                if data:
                    product = data.get("product_results", {})
                    brand = (product.get("brand") or "").strip()
                    title = (product.get("title") or "").strip()
                    print(f"   ✓ Brand: {brand or 'N/A'}")
                    print(f"   ✓ Title: {(title[:80] + '...') if len(title) > 80 else (title or 'N/A')}")

                time.sleep(CONFIG["WAIT_BETWEEN_REQUESTS"])
            else:
                print(f"   ⚠️  Could not extract ASIN from {CONFIG['COLUMN_AMAZON_LINK']}")

            enriched = dict(row)
            enriched["Amazon Brand"] = brand
            enriched["Amazon Title"] = title
            write_batch.append(enriched)

            if len(write_batch) >= int(CONFIG["STAGE1_WRITE_BATCH_SIZE"]):
                flush_rows(CONFIG["STAGE1_OUTPUT_CSV"], headers, write_batch, mode)
                written += len(write_batch)
                print(f"   ✍️  Flushed {len(write_batch)} rows (total written: {written})")
                write_batch = []
                mode = "a"

    if write_batch:
        flush_rows(CONFIG["STAGE1_OUTPUT_CSV"], headers, write_batch, mode)
        written += len(write_batch)
        print(f"   ✍️  Final flush {len(write_batch)} rows (total written: {written})")

    print(f"\n✅ STAGE 1 complete. Rows written: {written}")


def stage2():
    print("\n" + "=" * 90)
    print("STAGE 2 - Brand search (pick up to 3 products, same brand)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE1_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['STAGE2_OUTPUT_CSV']}")

    write_batch = []
    mode = "w"
    written = 0

    with open(CONFIG["STAGE1_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        in_headers = list(reader.fieldnames or [])
        add = [
            "Product1 Title",
            "Product1 Link",
            "Product2 Title",
            "Product2 Link",
            "Product3 Title",
            "Product3 Link",
        ]
        out_headers = in_headers + [h for h in add if h not in in_headers]

        for idx, row in enumerate(reader, 1):
            brand = (row.get("Amazon Brand") or "").strip()
            source_title = (row.get("Amazon Title") or "").strip()
            source_link = (row.get(CONFIG["COLUMN_AMAZON_LINK"]) or "").strip()
            source_asin = extract_asin(source_link)
            print(f"\n📍 [S2] row #{idx} | brand: {brand or 'N/A'}")

            p1_title = p1_link = p2_title = p2_link = p3_title = p3_link = ""
            if brand:
                url = build_search_url(brand)
                data = fetch_json(url)
                if data:
                    products = data.get("search_results", {}).get("product_results", [])
                    p1_title, p1_link, p2_title, p2_link, p3_title, p3_link = choose_three_products(
                        products,
                        source_title,
                        brand,
                        source_asin=source_asin,
                    )
                    for n, t in ((1, p1_title), (2, p2_title), (3, p3_title)):
                        s = (t[:70] + "...") if len(t) > 70 else (t or "N/A")
                        print(f"   ✓ Product{n}: {s}")
                time.sleep(CONFIG["WAIT_BETWEEN_REQUESTS"])
            else:
                print("   ⚠️  Empty Amazon Brand; skipping search")

            enriched = dict(row)
            enriched["Product1 Title"] = p1_title
            enriched["Product1 Link"] = p1_link
            enriched["Product2 Title"] = p2_title
            enriched["Product2 Link"] = p2_link
            enriched["Product3 Title"] = p3_title
            enriched["Product3 Link"] = p3_link
            write_batch.append(enriched)

            if len(write_batch) >= int(CONFIG["STAGE2_WRITE_BATCH_SIZE"]):
                flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], out_headers, write_batch, mode)
                written += len(write_batch)
                print(f"   ✍️  Flushed {len(write_batch)} rows (total written: {written})")
                write_batch = []
                mode = "a"

    if write_batch:
        flush_rows(CONFIG["STAGE2_OUTPUT_CSV"], out_headers, write_batch, mode)
        written += len(write_batch)
        print(f"   ✍️  Final flush {len(write_batch)} rows (total written: {written})")

    print(f"\n✅ STAGE 2 complete. Rows written: {written}")


SYSTEM_PROMPT_STAGE3 = """Du wählst aus drei Amazon-Suchtreffern derselben Marke genau zwei verschiedene Produkte und bereinigst deren Titel zu Produktnamen.

Regeln:
- Entferne genau einen der drei Treffer (Entfernt = 1, 2 oder 3), der Duplikat, zu nah am anderen oder unsinnig ist (z. B. nur Markenname, gleicher Produkttyp ohne echte Differenz).
- Die beiden übrigen müssen klar unterschiedliche Produkte sein.
- Wenn nur zwei Spalten Titel haben: setze Entfernt = 0 und bereinige beide Titel.
- Wenn nur eine Spalte Titel hat: Entfernt = 0, Product1 Name = bereinigt oder unknown, Product2 Name = unknown.
- Wenn alle drei leer: Entfernt = 0, beide Namen unknown.
- Produktnamen: Markenname entfernen, Farbe/Größe/Maße/Stückzahl/Meta/Werbung entfernen; natürlicher Kurzname mit Produkttyp.
- Wenn aus einem Titel kein sinnvoller Produktname ableitbar ist: unknown
- Nur CSV ausgeben, keine Erklärungen.
- Format exakt: Nr;Entfernt;Product1 Name;Product2 Name
  Entfernt = 0 (nur zwei Kandidaten) oder 1–3 (welcher der drei entfällt).
  Product1 Name = bereinigter Name für den behaltenen Treffer mit der kleineren Nummer (1<2<3).
  Product2 Name = bereinigter Name für den behaltenen Treffer mit der größeren Nummer.
- Pro Eingabezeile genau eine Ausgabezeile; Nr = 1,2,3,... wie im Input.
"""


def call_gpt_clean(batch):
    lines = [
        "Drei Kandidaten (gleiche Marke). Wähle zwei verschiedene Produkte, entferne einen, liefere bereinigte Namen für die zwei Behaltenen (Reihenfolge nach kleinerer/groesserer Produktnummer).",
        "Nr | P1 Title | P1 Link | P2 Title | P2 Link | P3 Title | P3 Link",
        "-" * 80,
    ]
    for i, row in enumerate(batch, 1):
        t1 = (row.get("Product1 Title") or "").strip()
        l1 = (row.get("Product1 Link") or "").strip()
        t2 = (row.get("Product2 Title") or "").strip()
        l2 = (row.get("Product2 Link") or "").strip()
        t3 = (row.get("Product3 Title") or "").strip()
        l3 = (row.get("Product3 Link") or "").strip()
        lines.append(f"{i} | {t1} | {l1} | {t2} | {l2} | {t3} | {l3}")
    lines.append("-" * 80)
    lines.append("Antwort nur als CSV:")
    lines.append("Nr;Entfernt;Product1 Name;Product2 Name")
    user_prompt = "\n".join(lines)

    try:
        response = client.responses.create(
            model=CONFIG["MODEL"],
            reasoning={"effort": CONFIG["REASONING_EFFORT"]},
            input=[
                {"role": "system", "content": SYSTEM_PROMPT_STAGE3},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=int(CONFIG["MAX_OUTPUT_TOKENS"]),
        )
        out = (response.output_text or "").strip()
        return out
    except Exception as e:
        print(f"      ❌ GPT error: {e}")
        return ""


def parse_stage3_response(text, size):
    """Map GPT rows by Nr -> (Entfernt, Product1 Name, Product2 Name)."""
    out = [(0, "", "") for _ in range(size)]
    if not text:
        return out
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
    fallback_order = []
    for ln in data_lines:
        parts = [p.strip() for p in ln.split(";")]
        m0 = re.match(r"^(\d+)", parts[0].strip()) if parts else None
        if len(parts) >= 4 and m0:
            nr = int(m0.group(1))
            if 1 <= nr <= size:
                dr = parts[1].strip().lower()
                drop = int(dr) if dr.isdigit() else 0
                if drop not in (0, 1, 2, 3):
                    drop = 0
                indexed[nr] = (drop, parts[2], parts[3])
                continue
        if len(parts) >= 3:
            m = re.match(r"^(\d+)", parts[0].strip())
            if m:
                nr = int(m.group(1))
                if 1 <= nr <= size:
                    indexed[nr] = (0, parts[1], parts[2])
                    continue
            fallback_order.append((0, parts[-2], parts[-1]))
        elif len(parts) == 2:
            fallback_order.append((0, parts[0], parts[1]))

    for nr in range(1, size + 1):
        if nr in indexed:
            out[nr - 1] = indexed[nr]

    j = 0
    for i in range(size):
        if out[i] == (0, "", "") and j < len(fallback_order):
            out[i] = fallback_order[j]
            j += 1

    if indexed and len(indexed) != size:
        print(f"      ⚠️  GPT returned {len(indexed)}/{size} indexed rows; filled gaps from order/unknown.")
    return out


def stage3_filled_slot_indices(row) -> list:
    """1-based product slot numbers that have a non-empty title."""
    filled = []
    for k in (1, 2, 3):
        if (row.get(f"Product{k} Title") or "").strip():
            filled.append(k)
    return filled


def stage3_apply_gpt_to_final_row(row, drop: int, name1: str, name2: str, final_headers: list) -> dict:
    """
    Map GPT output + stage2 row -> final row: Product1/2 Link and Name (two kept products, ascending slot order).
    """
    slots = {}
    for k in (1, 2, 3):
        slots[k] = (
            (row.get(f"Product{k} Title") or "").strip(),
            (row.get(f"Product{k} Link") or "").strip(),
        )

    filled = stage3_filled_slot_indices(row)

    if not filled:
        link1 = link2 = ""
        t_src1 = t_src2 = ""
    elif len(filled) == 1:
        k = filled[0]
        link1, link2 = slots[k][1], ""
        t_src1, t_src2 = slots[k][0], ""
    elif len(filled) == 2:
        a, b = filled[0], filled[1]
        link1, link2 = slots[a][1], slots[b][1]
        t_src1, t_src2 = slots[a][0], slots[b][0]
    else:
        filled_set = set(filled)
        if drop not in (1, 2, 3):
            drop = 3
        kept = sorted([k for k in (1, 2, 3) if k != drop and k in filled_set])
        if len(kept) < 2:
            kept = sorted(filled)[:2]
        a, b = kept[0], kept[1]
        link1, link2 = slots[a][1], slots[b][1]
        t_src1, t_src2 = slots[a][0], slots[b][0]

    n1 = stage3_cell_or_unknown(name1, t_src1)
    n2 = stage3_cell_or_unknown(name2, t_src2)

    final_row = {h: row.get(h, "") for h in final_headers}
    final_row["Product1 Link"] = link1
    final_row["Product2 Link"] = link2
    final_row["Product1 Name"] = n1
    final_row["Product2 Name"] = n2
    return final_row


def stage3_cell_or_unknown(value: str, source_title: str) -> str:
    s = (value or "").strip()
    if not (source_title or "").strip():
        return "unknown"
    if not s or s.lower() in ("n/a", "na", "none", "-", "null"):
        return "unknown"
    return s


def stage3():
    print("\n" + "=" * 90)
    print("STAGE 3 - GPT: 2 von 3 Produkten wählen + Namen (Final: 2 Links / 2 Namen)")
    print("=" * 90)
    print(f"📁 Input:  {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 Output: {CONFIG['FINAL_OUTPUT_CSV']}")
    print(f"🤖 Model:  {CONFIG['MODEL']}")

    with open(CONFIG["STAGE2_OUTPUT_CSV"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        in_headers = list(reader.fieldnames or [])
        stage_added_drop = {
            "Amazon Brand",
            "Amazon Title",
            "Product1 Title",
            "Product2 Title",
            "Product3 Title",
            "Product3 Link",
        }
        base_input_headers = [
            h for h in in_headers if h not in stage_added_drop and h not in {"Product1 Link", "Product2 Link"}
        ]
        final_headers = base_input_headers + ["Product1 Link", "Product2 Link", "Product1 Name", "Product2 Name"]

        write_mode = "w"
        write_batch = []
        gpt_batch = []
        written = 0
        batch_size = int(CONFIG["STAGE3_BATCH_SIZE"])

        def flush_gpt_batch(items):
            nonlocal write_mode, written, write_batch
            if not items:
                return
            print(f"\n   🤖 Calling GPT for {len(items)} rows...")
            raw = call_gpt_clean(items)
            parsed = parse_stage3_response(raw, len(items))
            for i, row in enumerate(items):
                drop, p1_name, p2_name = parsed[i]
                write_batch.append(
                    stage3_apply_gpt_to_final_row(row, drop, p1_name, p2_name, final_headers)
                )

            if len(write_batch) >= int(CONFIG["STAGE3_WRITE_BATCH_SIZE"]):
                flush_rows(CONFIG["FINAL_OUTPUT_CSV"], final_headers, write_batch, write_mode)
                written += len(write_batch)
                print(f"   ✍️  Flushed {len(write_batch)} rows (total written: {written})")
                write_batch = []
                write_mode = "a"
            time.sleep(CONFIG["WAIT_BETWEEN_BATCHES"])

        for idx, row in enumerate(reader, 1):
            print(f"\n📍 [S3] row #{idx}")
            gpt_batch.append(row)
            if len(gpt_batch) >= batch_size:
                flush_gpt_batch(gpt_batch)
                gpt_batch = []

        if gpt_batch:
            flush_gpt_batch(gpt_batch)
        if write_batch:
            flush_rows(CONFIG["FINAL_OUTPUT_CSV"], final_headers, write_batch, write_mode)
            written += len(write_batch)
            print(f"   ✍️  Final flush {len(write_batch)} rows (total written: {written})")

    print(f"\n✅ STAGE 3 complete. Rows written: {written}")


def main():
    start = time.time()
    print("\n" + "=" * 90)
    print("AMAZON RESELLERS PIPELINE (3 STAGES)")
    print("=" * 90)
    print(f"📁 Input CSV: {CONFIG['INPUT_CSV']}")
    print(f"📁 Stage1:    {CONFIG['STAGE1_OUTPUT_CSV']}")
    print(f"📁 Stage2:    {CONFIG['STAGE2_OUTPUT_CSV']}")
    print(f"📁 Final:     {CONFIG['FINAL_OUTPUT_CSV']}")

    stage1()
    stage2()
    stage3()

    elapsed = time.time() - start
    print("\n" + "=" * 90)
    print("🎉 PIPELINE COMPLETE")
    print(f"⏱️  Total time: {elapsed:.2f}s ({elapsed/60:.2f} min)")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
