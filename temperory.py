"""
Temporary backfill script for old amazon_seller_pipeline outputs.

Input: an old Stage 4-style CSV (or similar) that already has seller fields.
Flow:
1) Remove headers that should be regenerated:
   seller region, seller rating, seller review count, seller email,
   seller number, seller incharge person, seller person title, rank
2) Treat cleaned CSV as Stage 2 input.
3) Re-run Stage 3 -> Stage 4 -> Stage 5 from amazon_seller_pipeline.

Config prefix: TEMP_
"""

import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import amazon_seller_pipeline as seller_pipeline
from config_env import override_from_env

CONFIG = {
    # File flow
    "INPUT_CSV": "data/seller_final.csv",  # old stage4-like csv
    "STAGE2_CLEAN_OUTPUT_CSV": "data/temp_stage2_clean.csv",
    "STAGE3_OUTPUT_CSV": "data/temp_stage3.csv",
    "STAGE3_WIPED_OUTPUT_CSV": "data/temp_stage3_wiped.csv",
    "STAGE4_OUTPUT_CSV": "data/temp_stage4.csv",
    "STAGE5_OUTPUT_CSV": "data/temp_stage5.csv",

    # Runtime/API
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

    # Batches
    "STAGE3_WRITE_BATCH_SIZE": 100,
    "STAGE4_BATCH_SIZE": 10,
    "STAGE5_WRITE_BATCH_SIZE": 50,

    # Apollo
    "APOLLO_BASE_URL": "https://api.apollo.io/api/v1",
    "APOLLO_API_TIMEOUT": 60,
    "APOLLO_HTTP_RETRIES": 3,

    # GPT
    "MODEL": "gpt-5-mini",
    "REASONING_EFFORT": "medium",
    "MAX_OUTPUT_TOKENS": 5000,
}

DROP_HEADERS = {
    "seller region",
    "seller rating",
    "seller review count",
    "seller email",
    "seller number",
    "seller incharge person",
    "seller person title",
    "rank",
}


def clean_input_to_stage2_like():
    in_path = CONFIG["INPUT_CSV"]
    out_path = CONFIG["STAGE2_CLEAN_OUTPUT_CSV"]
    print(f"\n🧹 Cleaning input headers")
    print(f"📁 Input:  {in_path}")
    print(f"📁 Output: {out_path}")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(in_path, "r", encoding="utf-8") as rf:
        reader = csv.DictReader(rf)
        in_headers = list(reader.fieldnames or [])
        out_headers = [h for h in in_headers if h not in DROP_HEADERS]

        rows = []
        for row in reader:
            rows.append({h: row.get(h, "") for h in out_headers})

    with open(out_path, "w", newline="", encoding="utf-8") as wf:
        writer = csv.DictWriter(wf, fieldnames=out_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"   ✓ Rows written: {len(rows)}")


def wire_config_into_seller_pipeline():
    # Redirect pipeline stages to temp files
    seller_pipeline.CONFIG["STAGE2_OUTPUT_CSV"] = CONFIG["STAGE2_CLEAN_OUTPUT_CSV"]
    seller_pipeline.CONFIG["STAGE3_OUTPUT_CSV"] = CONFIG["STAGE3_OUTPUT_CSV"]
    seller_pipeline.CONFIG["STAGE3_WIPED_OUTPUT_CSV"] = CONFIG["STAGE3_WIPED_OUTPUT_CSV"]
    seller_pipeline.CONFIG["STAGE4_OUTPUT_CSV"] = CONFIG["STAGE4_OUTPUT_CSV"]
    seller_pipeline.CONFIG["STAGE5_OUTPUT_CSV"] = CONFIG["STAGE5_OUTPUT_CSV"]

    # Shared tuning knobs
    for k in (
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
        "STAGE3_WRITE_BATCH_SIZE",
        "STAGE4_BATCH_SIZE",
        "STAGE5_WRITE_BATCH_SIZE",
        "APOLLO_BASE_URL",
        "APOLLO_API_TIMEOUT",
        "APOLLO_HTTP_RETRIES",
        "MODEL",
        "REASONING_EFFORT",
        "MAX_OUTPUT_TOKENS",
    ):
        seller_pipeline.CONFIG[k] = CONFIG[k]


def main():
    print("\n" + "=" * 90)
    print("TEMP BACKFILL PIPELINE (re-run Stage 3/4/5)")
    print("=" * 90)
    print(f"📁 Input old file: {CONFIG['INPUT_CSV']}")
    print(f"📁 Temp stage2:    {CONFIG['STAGE2_CLEAN_OUTPUT_CSV']}")
    print(f"📁 Temp stage3:    {CONFIG['STAGE3_OUTPUT_CSV']}")
    print(f"📁 Temp stage4:    {CONFIG['STAGE4_OUTPUT_CSV']}")
    print(f"📁 Temp stage5:    {CONFIG['STAGE5_OUTPUT_CSV']}")

    clean_input_to_stage2_like()
    wire_config_into_seller_pipeline()
    seller_pipeline.stage3()
    seller_pipeline.stage4()
    seller_pipeline.stage5()

    print("\n✅ TEMP BACKFILL COMPLETE")


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    override_from_env(CONFIG, env_prefix="TEMP_")

    api_key = (os.getenv("API_KEY") or "").strip()
    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    apollo_api_key = (os.getenv("APOLLO_API_KEY") or "").strip()

    if not api_key:
        raise ValueError("API_KEY not found in .env file.")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY not found in .env file.")
    if not apollo_api_key:
        raise ValueError("APOLLO_API_KEY not found in .env file.")

    # Inject credentials into imported pipeline module
    seller_pipeline.API_KEY = api_key
    seller_pipeline.APOLLO_API_KEY = apollo_api_key
    seller_pipeline.client = OpenAI(api_key=openai_api_key)
    seller_pipeline.load_public_email_domains()

    main()
