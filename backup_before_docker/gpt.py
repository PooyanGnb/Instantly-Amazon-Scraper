"""
GPT Product Feature Extraction Script
Processes enriched seller CSV and extracts short product names
and visual feature suggestions using GPT for email campaigns.
"""

import csv
import os
import re
import time
import json
from dotenv import load_dotenv
from openai import OpenAI

# ============================================================================
# CONFIGURATION SECTION - Modify these values as needed
# ============================================================================

CONFIG = {
    # Input/Output Files
    # Note: CSV files are stored in the "csv" subfolder
    'INPUT_CSV': 'Bunch 3 - Amazon.csv',
    'OUTPUT_CSV': 'Bunch 3 - Final.csv',

    # Processing Parameters
    'START_FROM_LINE': 301,   # 1-indexed record number (None = from beginning)
    'MAX_RECORDS': None,       # Max records to process (None = all)

    # Batch Settings
    'BATCH_SIZE': 5,           # Number of records per GPT call

    # GPT Settings
    'MODEL': 'gpt-5-mini',
    'REASONING_EFFORT': 'medium',
    'MAX_OUTPUT_TOKENS': 5000,

    # Web Search Settings (set to True if needed)
    'USE_WEB_SEARCH': False,
    'WEB_SEARCH_CONTEXT': 'small',
    'MAX_TOOL_CALLS': 5,

    # Rate Limiting
    'WAIT_BETWEEN_BATCHES': 2,  # Seconds between API calls

    # -------------------------------------------------------------------------
    # CSV Column Mapping - adapt to your CSV structure (order or column names)
    # -------------------------------------------------------------------------
    # List column names to try in order; first non-empty match is used.
    # For Seller ID: if the cell looks like an Amazon URL (e.g. ...?seller=XXX),
    #   the script will extract the seller ID automatically.
    'COLUMN_SELLER_ID': ['Seller ID', 'Seller URL'],
    'COLUMN_SELLER_NAME': ['Seller Name'],
    'COLUMN_PRODUCT_NAME': ['Product Name'],
    'COLUMN_PRODUCT_DESCRIPTION': ['Product Description'],
}

# ============================================================================
# SCRIPT IMPLEMENTATION
# ============================================================================

load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not found in .env file!")

client = OpenAI(api_key=OPENAI_API_KEY)

# Regex to extract seller ID from Amazon URLs (e.g. ?seller=A1VXZR1R80P8X9)
SELLER_ID_FROM_URL_RE = re.compile(r'[?&]seller=([A-Z0-9]+)', re.IGNORECASE)


def _extract_seller_id_from_value(value):
    """Get seller ID from a cell: raw value or extract from Amazon URL ?seller=XXX."""
    if not value or not isinstance(value, str):
        return ''
    s = value.strip()
    if not s:
        return ''
    match = SELLER_ID_FROM_URL_RE.search(s)
    if match:
        return match.group(1).strip()
    if re.match(r'^[A-Z0-9]{10,20}$', s, re.IGNORECASE):
        return s
    return s


def get_seller_id_from_row(row):
    """Resolve seller ID from a CSV row using COLUMN_SELLER_ID mapping."""
    for col in CONFIG.get('COLUMN_SELLER_ID', ['Seller ID', 'Seller URL']):
        val = (row.get(col) or '').strip() if col else ''
        if val:
            return _extract_seller_id_from_value(val)
    return ''


def get_seller_name_from_row(row):
    """Resolve seller name for display using COLUMN_SELLER_NAME mapping."""
    for col in CONFIG.get('COLUMN_SELLER_NAME', ['Seller Name']):
        val = (row.get(col) or '').strip() if col else ''
        if val:
            return val
    return 'N/A'


def get_product_name_from_row(row):
    """Resolve product name from row using COLUMN_PRODUCT_NAME mapping."""
    for col in CONFIG.get('COLUMN_PRODUCT_NAME', ['Product Name']):
        val = (row.get(col) or '').strip() if col else ''
        if val:
            return val
    return ''


def get_product_description_from_row(row):
    """Resolve product description from row using COLUMN_PRODUCT_DESCRIPTION mapping."""
    for col in CONFIG.get('COLUMN_PRODUCT_DESCRIPTION', ['Product Description']):
        val = (row.get(col) or '').strip() if col else ''
        if val:
            return val
    return ''


# ============================================================================
# PROMPT BUILDING
# ============================================================================

SYSTEM_PROMPT = """Du bist ein spezialisiertes Team aus professionellen Textern, Grafik-Designern und 3D-Künstlern mit langjähriger Erfahrung im E-Commerce und Amazon-Marketing in Deutschland.

Deine Aufgabe ist es, für jedes Produkt zwei Dinge zu liefern:

1. **Kurzer Produktname (Variable 1):** Kürze den Produktnamen auf den wichtigsten, einprägsamen Begriff oder eine kurze Phrase down. Der Name muss in einem deutschen Akquisitions-E-Mail natürlich klingen. Verwende den Kernbegriff oder die Produktkategorie auf Deutsch. Maximal 3-4 Wörter.

2. **Visuelle Selling-Points (Variable 2):** Analysiere das Produkt und die Beschreibung. Identifiziere 2-3 konkrete Vorteile/Features, die visuell in Produktbildern hervorgehoben werden könnten, um die Conversion-Rate zu steigern. Diese müssen aus der Produktbeschreibung stammen oder logisch ableitbar sein. Schreibe sie auf Deutsch, kurz und prägnant (ähnlich wie Stichpunkte, kommagetrennt). Maximal 8-10 Wörter pro Feature.

Wichtig:
- Alles auf Deutsch.
- Variable 1 muss extrem kurz sein (Kernbegriff).
- Variable 2 sind konkrete, visuell darstellbare Features – keine abstrakten Versprechen.
- Antworte NUR im angegebenen CSV-Format. Keine zusätzlichen Erklärungen oder Texte außerhalb des CSV-Blocks.
- Verwende KEINE Kommas innerhalb der Variablen (nutze stattdessen Semikolons oder Bindestriche als Trennzeichen innerhalb eines Feldes).
- Gib genau die gleiche Anzahl von Zeilen zurück wie du empfangen hast.
"""


def build_user_prompt(batch):
    """Build the user prompt with the batch of products"""
    lines = []
    lines.append("Hier sind die Produkte zum Verarbeiten:\n")
    lines.append("Nr | Produktname | Produktbeschreibung")
    lines.append("-" * 80)

    for i, record in enumerate(batch, 1):
        name = get_product_name_from_row(record) or 'N/A'
        desc = get_product_description_from_row(record) or 'N/A'
        lines.append(f"{i} | {name} | {desc}")

    lines.append("\n" + "-" * 80)
    lines.append("Antworte NUR im folgenden CSV-Format (mit Semikolon als Trennzeichen zwischen Spalten):")
    lines.append("Nr;Variable 1;Variable 2")
    lines.append("1;[Kurzer Name];[Visuelle Features]")
    lines.append("2;[Kurzer Name];[Visuelle Features]")
    lines.append("...")

    return "\n".join(lines)


# ============================================================================
# GPT API CALL
# ============================================================================

def call_gpt(batch):
    """Send a batch to GPT and get processed results (Responses API)"""

    user_prompt = build_user_prompt(batch)

    try:
        response = client.responses.create(
            model=CONFIG['MODEL'],  # gpt-5-mini
            reasoning={
                "effort": CONFIG['REASONING_EFFORT']  # low | medium | high
            },
            input=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            max_output_tokens=CONFIG['MAX_OUTPUT_TOKENS']
        )

        # ✅ Correct way to extract text from Responses API
        output_text = response.output_text

        if not output_text:
            print("      ⚠️ GPT returned empty output")
            return None

        return output_text.strip()

    except Exception as e:
        print(f"      ❌ GPT API Error: {str(e)}")
        return None


# ============================================================================
# RESPONSE PARSING
# ============================================================================

def parse_gpt_response(response_text, batch_size):
    """Parse the GPT CSV response into a list of (variable1, variable2) tuples"""
    results = []

    if not response_text:
        return [('' , '') for _ in range(batch_size)]

    # Extract lines, skip empty lines and header-like lines
    lines = [line.strip() for line in response_text.split('\n') if line.strip()]

    # Filter out markdown fences, header lines, separator lines
    filtered = []
    for line in lines:
        stripped = line.strip('`').strip()
        if not stripped:
            continue
        if stripped.startswith('Nr') and 'Variable' in stripped:
            continue  # Skip header row
        if stripped.startswith('---') or stripped.startswith('==='):
            continue  # Skip separators
        if stripped.startswith('#'):
            continue  # Skip markdown headers
        filtered.append(stripped)

    # Parse each valid data line
    for line in filtered:
        parts = line.split(';')
        if len(parts) >= 3:
            # Format: Nr;Variable1;Variable2
            var1 = parts[1].strip()
            var2 = parts[2].strip()
            results.append((var1, var2))
        elif len(parts) == 2:
            # Fallback: Variable1;Variable2 (no Nr prefix)
            var1 = parts[0].strip()
            var2 = parts[1].strip()
            # Check if first part is just a number (Nr column)
            if var1.isdigit():
                results.append(('', var2))
            else:
                results.append((var1, var2))

    # If we got fewer results than batch size, pad with empty strings
    while len(results) < batch_size:
        results.append(('', ''))

    # If we got more, trim to batch size
    results = results[:batch_size]

    return results


# ============================================================================
# FILE HANDLING
# ============================================================================

OUTPUT_EXTRA_COLUMNS = ['Variable 1', 'Variable 2']


def get_existing_seller_ids(output_file):
    """Read already-processed Seller IDs from output file (uses COLUMN_SELLER_ID mapping)."""
    if not os.path.exists(output_file):
        return set()
    try:
        seen = set()
        with open(output_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = get_seller_id_from_row(row)
                if sid:
                    seen.add(sid)
        return seen
    except Exception as e:
        print(f"⚠️  Could not read existing output file: {e}")
        return set()


def initialize_output_file(output_headers):
    """Create output file with headers if it doesn't exist. output_headers = input CSV columns + Variable 1, Variable 2."""
    if not os.path.exists(CONFIG['OUTPUT_CSV']):
        with open(CONFIG['OUTPUT_CSV'], 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=output_headers, extrasaction='ignore')
            writer.writeheader()
        print(f"   ✓ Created new output file: {CONFIG['OUTPUT_CSV']}")
    else:
        print(f"   ✓ Output file exists, will append to: {CONFIG['OUTPUT_CSV']}")


def append_batch_to_output(rows, output_headers):
    """Append enriched rows to the output CSV. Uses output_headers so any CSV structure is supported."""
    with open(CONFIG['OUTPUT_CSV'], 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=output_headers, extrasaction='ignore')
        for row in rows:
            # Write only keys that are in output_headers; fill missing with ''
            ordered_row = {k: row.get(k, '') for k in output_headers}
            writer.writerow(ordered_row)


# ============================================================================
# MAIN PROCESSING
# ============================================================================

def main():
    print("\n" + "=" * 80)
    print("🤖 GPT Product Feature Extraction Script")
    print("=" * 80)
    print(f"\n📁 Input File:  {CONFIG['INPUT_CSV']}")
    print(f"📁 Output File: {CONFIG['OUTPUT_CSV']}")
    print(f"📦 Batch Size:  {CONFIG['BATCH_SIZE']} records per GPT call")
    print(f"🤖 Model:       {CONFIG['MODEL']}")
    print(f"🔍 Web Search:  {'Enabled' if CONFIG['USE_WEB_SEARCH'] else 'Disabled'}")

    if CONFIG['START_FROM_LINE'] is not None:
        print(f"▶️  Starting from record: #{CONFIG['START_FROM_LINE']}")
    else:
        print(f"▶️  Starting from beginning")

    if CONFIG['MAX_RECORDS'] is not None:
        print(f"📊 Max records: {CONFIG['MAX_RECORDS']}")
    else:
        print(f"📊 Processing all records")

    # ---- Read input ----
    try:
        with open(CONFIG['INPUT_CSV'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            input_headers = list(reader.fieldnames) if reader.fieldnames else []
            all_rows = list(reader)
    except FileNotFoundError:
        print(f"\n❌ Error: Input file '{CONFIG['INPUT_CSV']}' not found!")
        return

    # Output = same columns as input + Variable 1, Variable 2 (structure-agnostic)
    output_headers = input_headers + [c for c in OUTPUT_EXTRA_COLUMNS if c not in input_headers]
    total_rows = len(all_rows)
    print(f"\n📋 Total records in input: {total_rows}")

    # ---- Determine slice to process ----
    start_idx = (CONFIG['START_FROM_LINE'] - 1) if CONFIG['START_FROM_LINE'] is not None else 0
    if start_idx < 0:
        start_idx = 0

    if start_idx >= total_rows:
        print(f"\n⚠️  START_FROM_LINE ({CONFIG['START_FROM_LINE']}) exceeds total records ({total_rows}). Nothing to do.")
        return

    rows_to_process = all_rows[start_idx:]
    if CONFIG['MAX_RECORDS'] is not None:
        rows_to_process = rows_to_process[:CONFIG['MAX_RECORDS']]

    # ---- Check existing output ----
    existing_ids = get_existing_seller_ids(CONFIG['OUTPUT_CSV'])
    if existing_ids:
        print(f"⏭️  Found {len(existing_ids)} already processed records in output file")

    initialize_output_file(output_headers)

    # ---- Filter out already processed ----
    pending_rows = []
    skipped_count = 0
    for row in rows_to_process:
        sid = get_seller_id_from_row(row)
        if sid in existing_ids:
            skipped_count += 1
        else:
            pending_rows.append(row)

    if skipped_count:
        print(f"⏭️  Skipping {skipped_count} already processed records")

    total_pending = len(pending_rows)
    print(f"📊 Records to process: {total_pending}")

    if total_pending == 0:
        print("\n✅ Nothing to process. All records already in output.")
        return

    # ---- Split into batches ----
    batches = []
    for i in range(0, total_pending, CONFIG['BATCH_SIZE']):
        batches.append(pending_rows[i:i + CONFIG['BATCH_SIZE']])

    total_batches = len(batches)
    print(f"📦 Total batches: {total_batches}")

    # ---- Process batches ----
    processed = 0
    failed = 0
    script_start = time.time()

    print(f"\n{'=' * 80}")
    print("🏁 Starting batch processing...")
    print(f"{'=' * 80}\n")

    for batch_idx, batch in enumerate(batches, 1):
        batch_start = time.time()
        batch_size = len(batch)

        print(f"\n{'─' * 80}")
        print(f"📦 Batch {batch_idx}/{total_batches} | Records: {batch_size}")
        print(f"{'─' * 80}")

        # Show what's in this batch
        for i, record in enumerate(batch):
            name = get_product_name_from_row(record) or 'N/A'
            seller = get_seller_name_from_row(record)
            display_name = f"{name[:55]}..." if len(name) > 55 else name
            print(f"   [{i+1}] {seller:30s} → {display_name}")

        print(f"\n   🤖 Calling GPT ({CONFIG['MODEL']})...")

        # Call GPT
        response_text = call_gpt(batch)

        if response_text is None:
            print(f"   ❌ Batch {batch_idx} FAILED - GPT returned no response")
            failed += batch_size
            continue

        print(f"\n   📝 Raw GPT Response:")
        print(f"   {'─' * 60}")
        for line in response_text.split('\n'):
            print(f"   | {line}")
        print(f"   {'─' * 60}")

        # Parse response
        parsed = parse_gpt_response(response_text, batch_size)

        # Build enriched rows
        enriched_rows = []
        for i, record in enumerate(batch):
            var1, var2 = parsed[i] if i < len(parsed) else ('', '')
            enriched = dict(record)
            enriched['Variable 1'] = var1
            enriched['Variable 2'] = var2
            enriched_rows.append(enriched)

            seller = get_seller_name_from_row(record)
            print(f"\n   ✅ [{i+1}] {seller}")
            print(f"       Variable 1: {var1}")
            print(f"       Variable 2: {var2}")

        # Write to output immediately
        append_batch_to_output(enriched_rows, output_headers)
        processed += batch_size

        batch_elapsed = time.time() - batch_start
        print(f"\n   ✍️  Batch {batch_idx} written to output | Time: {batch_elapsed:.2f}s")

        # Wait between batches (except last)
        if batch_idx < total_batches:
            print(f"\n   ⏸️  Waiting {CONFIG['WAIT_BETWEEN_BATCHES']}s before next batch...")
            time.sleep(CONFIG['WAIT_BETWEEN_BATCHES'])

    # ---- Final Summary ----
    total_elapsed = time.time() - script_start

    print(f"\n\n{'=' * 80}")
    print("🎉 PROCESSING COMPLETE!")
    print(f"{'=' * 80}")
    print(f"\n📊 Summary:")
    print(f"   ✅ Successfully processed: {processed}")
    print(f"   ⏭️  Skipped (already done): {skipped_count}")
    print(f"   ❌ Failed batches records:  {failed}")
    print(f"   📝 Total attempted:         {total_pending}")
    print(f"   ⏱️  Total time: {total_elapsed:.2f}s ({total_elapsed / 60:.2f} min)")
    print(f"   📁 Output: {CONFIG['OUTPUT_CSV']}")
    print(f"\n{'=' * 80}\n")


if __name__ == "__main__":
    main()
