"""
Amazon Seller Product Enrichment Script
Fetches top products for sellers and enriches CSV with product details
"""

import csv
import re
import requests
import time
from pathlib import Path
from dotenv import load_dotenv
import os
from datetime import datetime

# ============================================================================
# CONFIGURATION SECTION - Modify these values as needed
# ============================================================================

CONFIG = {
    # Input/Output Files
    'INPUT_CSV': 'Bunch 3 - Verified.csv',           # Name of input CSV file
    'OUTPUT_CSV': 'Bunch 3 - Amazon1.csv', # Name of output CSV file
    
    # Processing Parameters
    'START_FROM_LINE': 301,  # Start from specific RECORD number (None = start from beginning)
                              # Example: 11 means start from the 11th record (line 12 in file)
    'MAX_RECORDS': None,      # Maximum records to process (None = process all)
                              # Example: 50 means process only 50 records
    
    # API Configuration
    'WAIT_BETWEEN_REQUESTS': 2,  # Seconds to wait between API requests
    'API_TIMEOUT': 60,             # Timeout for API requests in seconds
    
    # Amazon Configuration
    'AMAZON_DOMAIN': 'amazon.de',
    
    # API Parameters
    'SCROLL_TO_BOTTOM': 'true',
    'WAIT_BOTTOM_CAROUSEL': 'true',
    'WAIT_FOR_OFFERS': 'true',
    'WAIT_FOR_VIDEO': 'true',
    'HTTP': 'true',
    'DEVICE': 'desktop',
    'PAGE': '1',

    # -------------------------------------------------------------------------
    # CSV Column Mapping - adapt to your CSV structure (order or column names)
    # -------------------------------------------------------------------------
    # Script needs: (1) Seller ID for API calls, (2) Seller Name for console output.
    # List column names to try in order; first non-empty match is used.
    # For Seller ID: if the cell looks like an Amazon URL (e.g. ...?seller=XXX or .../sp?seller=XXX),
    #   the script will extract the seller ID automatically.
    #
    # Example - standard structure (Seller ID, Seller Name columns):
    #   'COLUMN_SELLER_ID': ['Seller ID', 'Seller URL'],
    #   'COLUMN_SELLER_NAME': ['Seller Name'],
    #
    # Example - Bunch/Verified structure (Seller URL only, no Seller ID column):
    #   'COLUMN_SELLER_ID': ['Seller URL', 'Seller ID'],
    #   'COLUMN_SELLER_NAME': ['Seller Name'],
    #
    'COLUMN_SELLER_ID': ['Seller ID', 'Seller URL'],
    'COLUMN_SELLER_NAME': ['Seller Name'],
}

# Override CONFIG from environment variables (for Docker/VPS - no code edit needed)
# Load .env from the same directory as this script (so it's found regardless of cwd)
load_dotenv(Path(__file__).resolve().parent / ".env")
from config_env import override_from_env
override_from_env(CONFIG)

# ============================================================================
# SCRIPT IMPLEMENTATION - Do not modify below unless you know what you're doing
# ============================================================================

API_KEY = (os.getenv('API_KEY') or '').strip()

if not API_KEY:
    raise ValueError("API_KEY not found in .env file! Put it in the same folder as main.py.")

BASE_URL = "https://ecom.webscrapingapi.com/v1"

# Regex to extract seller ID from Amazon URLs (e.g. ?seller=A1VXZR1R80P8X9 or &seller=...)
SELLER_ID_FROM_URL_RE = re.compile(r'[?&]seller=([A-Z0-9]+)', re.IGNORECASE)


def extract_seller_id_from_value(value):
    """
    Get seller ID from a cell value: either the raw value (if it looks like an ID)
    or extract from an Amazon URL containing ?seller=XXX or &seller=XXX.
    """
    if not value or not isinstance(value, str):
        return ''
    s = value.strip()
    if not s:
        return ''
    # URL: extract from query param
    match = SELLER_ID_FROM_URL_RE.search(s)
    if match:
        return match.group(1).strip()
    # Already looks like a seller ID (alphanumeric, typical length)
    if re.match(r'^[A-Z0-9]{10,20}$', s, re.IGNORECASE):
        return s
    return s


def get_seller_id_from_row(row):
    """Resolve seller ID from a CSV row using COLUMN_SELLER_ID mapping."""
    for col in CONFIG.get('COLUMN_SELLER_ID', ['Seller ID', 'Seller URL']):
        val = (row.get(col) or '').strip()
        if val:
            return extract_seller_id_from_value(val)
    return ''


def get_seller_name_from_row(row):
    """Resolve seller name from a CSV row using COLUMN_SELLER_NAME mapping (for display only)."""
    for col in CONFIG.get('COLUMN_SELLER_NAME', ['Seller Name']):
        val = (row.get(col) or '').strip()
        if val:
            return val
    return 'N/A'


def build_seller_products_url(seller_id):
    """Build URL for fetching seller products"""
    params = {
        'engine': 'amazon',
        'api_key': API_KEY,
        'type': 'seller_products',
        'seller_id': seller_id,
        'amazon_domain': CONFIG['AMAZON_DOMAIN'],
        'scroll_to_bottom': CONFIG['SCROLL_TO_BOTTOM'],
        'wait_bottom_carousel': CONFIG['WAIT_BOTTOM_CAROUSEL'],
        'wait_for_offers': CONFIG['WAIT_FOR_OFFERS'],
        'wait_for_video': 'false',  # False for seller products as per example
        'http': CONFIG['HTTP'],
        'device': CONFIG['DEVICE'],
        'page': CONFIG['PAGE']
    }
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    return f"{BASE_URL}?{query_string}"


def build_product_url(product_id):
    """Build URL for fetching product details"""
    params = {
        'engine': 'amazon',
        'api_key': API_KEY,
        'type': 'product',
        'product_id': product_id,
        'amazon_domain': CONFIG['AMAZON_DOMAIN'],
        'scroll_to_bottom': CONFIG['SCROLL_TO_BOTTOM'],
        'wait_bottom_carousel': CONFIG['WAIT_BOTTOM_CAROUSEL'],
        'wait_for_offers': CONFIG['WAIT_FOR_OFFERS'],
        'wait_for_video': CONFIG['WAIT_FOR_VIDEO'],
        'http': CONFIG['HTTP'],
        'device': CONFIG['DEVICE'],
        'page': CONFIG['PAGE']
    }
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    return f"{BASE_URL}?{query_string}"


def fetch_seller_products(seller_id):
    """Fetch products for a given seller"""
    url = build_seller_products_url(seller_id)
    
    try:
        response = requests.get(url, timeout=CONFIG['API_TIMEOUT'])
        response.raise_for_status()
        data = response.json()
        
        # Extract first product from results
        products = data.get('seller_products', {}).get('product_results', [])
        if products:
            return products[0]  # Return first product
        return None
        
    except Exception as e:
        print(f"    ❌ Error fetching seller products: {str(e)}")
        return None


def fetch_product_details(product_id):
    """Fetch detailed information for a product"""
    url = build_product_url(product_id)
    
    try:
        response = requests.get(url, timeout=CONFIG['API_TIMEOUT'])
        response.raise_for_status()
        data = response.json()
        
        product_results = data.get('product_results', {})
        return product_results
        
    except Exception as e:
        print(f"    ❌ Error fetching product details: {str(e)}")
        return None


def process_seller(seller_row):
    """Process a single seller and return enriched data"""
    seller_id = get_seller_id_from_row(seller_row)
    
    if not seller_id:
        return None
    
    start_time = time.time()
    
    print(f"\n{'='*80}")
    print(f"🔍 Processing Seller: {get_seller_name_from_row(seller_row)}")
    print(f"   Seller ID: {seller_id}")
    
    # Step 1: Get top product from seller
    print(f"   📦 Fetching seller products...")
    top_product = fetch_seller_products(seller_id)
    
    if not top_product:
        print(f"   ⚠️  No products found for seller")
        elapsed = time.time() - start_time
        print(f"   ⏱️  Time elapsed: {elapsed:.2f}s")
        return {**seller_row, 'Product URL': '', 'Product Name': '', 'Product Description': ''}
    
    product_id = top_product.get('product_id', '')
    print(f"   ✓ Found top product: {product_id}")
    
    # Wait before next request
    time.sleep(CONFIG['WAIT_BETWEEN_REQUESTS'])
    
    # Step 2: Get detailed product information
    print(f"   📋 Fetching product details...")
    product_details = fetch_product_details(product_id)
    
    if not product_details:
        print(f"   ⚠️  Could not fetch product details")
        elapsed = time.time() - start_time
        print(f"   ⏱️  Time elapsed: {elapsed:.2f}s")
        return {**seller_row, 'Product URL': '', 'Product Name': '', 'Product Description': ''}
    
    # Extract required fields
    amazon_url = product_details.get('link', '')
    product_name = product_details.get('title', '')
    product_features = product_details.get('product_features', [])
    
    # Join product features into a single description
    product_description = ' | '.join(product_features) if product_features else ''
    
    # Create enriched row
    enriched_row = {
        **seller_row,
        'Product URL': amazon_url,
        'Product Name': product_name,
        'Product Description': product_description
    }
    
    elapsed = time.time() - start_time
    
    print(f"   ✓ Product Name: {product_name[:60]}..." if len(product_name) > 60 else f"   ✓ Product Name: {product_name}")
    print(f"   ✓ Product URL: {amazon_url}")
    print(f"   ⏱️  Total time: {elapsed:.2f}s")
    print(f"{'='*80}")
    
    return enriched_row


def get_existing_seller_ids(output_file):
    """Get set of seller IDs that are already in the output file (uses same column mapping)."""
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
        print(f"⚠️  Warning: Could not read existing output file: {e}")
        return set()


def initialize_output_file(input_headers):
    """Initialize output CSV file with headers if it doesn't exist"""
    output_headers = input_headers + ['Product URL', 'Product Name', 'Product Description']
    
    if not os.path.exists(CONFIG['OUTPUT_CSV']):
        # Create output file with headers
        with open(CONFIG['OUTPUT_CSV'], 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=output_headers)
            writer.writeheader()
        print(f"✓ Created new output file: {CONFIG['OUTPUT_CSV']}")
    else:
        print(f"✓ Output file already exists, will append to: {CONFIG['OUTPUT_CSV']}")
    
    return output_headers


def append_to_output(row, output_headers):
    """Append a single row to output CSV"""
    with open(CONFIG['OUTPUT_CSV'], 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=output_headers)
        writer.writerow(row)


def main():
    """Main execution function"""
    print("\n" + "="*80)
    print("🚀 Amazon Seller Product Enrichment Script")
    print("="*80)
    print(f"\n📁 Input File: {CONFIG['INPUT_CSV']}")
    print(f"📁 Output File: {CONFIG['OUTPUT_CSV']}")
    print(f"⏰ Wait between requests: {CONFIG['WAIT_BETWEEN_REQUESTS']}s")
    
    if CONFIG['START_FROM_LINE']:
        print(f"▶️  Starting from record: #{CONFIG['START_FROM_LINE']} (line {CONFIG['START_FROM_LINE'] + 1} in file)")
    else:
        print(f"▶️  Starting from beginning")
    
    if CONFIG['MAX_RECORDS']:
        print(f"📊 Maximum records to process: {CONFIG['MAX_RECORDS']}")
    else:
        print(f"📊 Processing all records")
    
    # Read input CSV
    try:
        with open(CONFIG['INPUT_CSV'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            input_headers = reader.fieldnames
            all_rows = list(reader)
    except FileNotFoundError:
        print(f"\n❌ Error: Input file '{CONFIG['INPUT_CSV']}' not found!")
        return
    
    total_rows = len(all_rows)
    print(f"\n📋 Total records in input file: {total_rows}")
    
    # Get already processed seller IDs
    existing_seller_ids = get_existing_seller_ids(CONFIG['OUTPUT_CSV'])
    if existing_seller_ids:
        print(f"✓ Found {len(existing_seller_ids)} already processed sellers in output file")
    
    # Determine which rows to process
    # FIX: START_FROM_LINE now refers to record number (1-indexed)
    # Record 1 is at index 0, Record 11 is at index 10
    start_idx = (CONFIG['START_FROM_LINE'] - 1) if CONFIG['START_FROM_LINE'] is not None else 0
    
    if start_idx < 0:
        start_idx = 0
    
    if start_idx >= total_rows:
        print(f"\n⚠️  Warning: START_FROM_LINE ({CONFIG['START_FROM_LINE']}) is greater than total records ({total_rows})")
        print("Nothing to process!")
        return
    
    rows_to_process = all_rows[start_idx:]
    
    if CONFIG['MAX_RECORDS']:
        rows_to_process = rows_to_process[:CONFIG['MAX_RECORDS']]
    
    actual_count = len(rows_to_process)
    print(f"📊 Records to process: {actual_count}")
    
    # Initialize output file (create if doesn't exist, or verify if exists)
    output_headers = initialize_output_file(input_headers)
    
    # Process each seller
    processed = 0
    skipped = 0
    failed = 0
    script_start_time = time.time()
    
    print(f"\n{'='*80}")
    print("🏁 Starting processing...")
    print(f"{'='*80}\n")
    
    for idx, row in enumerate(rows_to_process, 1):
        seller_id = get_seller_id_from_row(row)
        
        print(f"\n📍 Progress: {idx}/{actual_count} | Record #{start_idx + idx}")
        
        # Skip if already processed
        if seller_id in existing_seller_ids:
            print(f"⏭️  Skipping Seller ID {seller_id} - Already processed")
            skipped += 1
            continue
        
        try:
            enriched_row = process_seller(row)
            
            if enriched_row:
                # Append to output file immediately
                append_to_output(enriched_row, output_headers)
                processed += 1
                print(f"   ✅ Successfully written to output file")
            else:
                failed += 1
                print(f"   ⚠️  Failed to process seller")
            
            # Wait before next seller (except for last one)
            if idx < actual_count:
                print(f"\n⏸️  Waiting {CONFIG['WAIT_BETWEEN_REQUESTS']}s before next seller...")
                time.sleep(CONFIG['WAIT_BETWEEN_REQUESTS'])
                
        except Exception as e:
            print(f"\n   ❌ Unexpected error: {str(e)}")
            failed += 1
            continue
    
    # Final summary
    total_elapsed = time.time() - script_start_time
    
    print(f"\n\n{'='*80}")
    print("🎉 PROCESSING COMPLETE!")
    print(f"{'='*80}")
    print(f"\n📊 Summary:")
    print(f"   ✅ Successfully processed: {processed}")
    print(f"   ⏭️  Skipped (already in output): {skipped}")
    print(f"   ❌ Failed: {failed}")
    print(f"   📝 Total attempted: {actual_count}")
    print(f"   ⏱️  Total time: {total_elapsed/60:.2f} minutes ({total_elapsed:.2f}s)")
    print(f"   📁 Output saved to: {CONFIG['OUTPUT_CSV']}")
    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    main()
