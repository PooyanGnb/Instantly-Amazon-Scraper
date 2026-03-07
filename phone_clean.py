"""
Phone Number Cleaner for CSV
Reads a CSV, cleans the phone numbers in the configured column, and writes output.
- Keeps only digits and a leading + (country code)
- Fixes German +490... -> +49 (erroneous extra 0 after country code)
- Keeps leading 0 for local format (e.g. 030...)
- Prefixes each value with ' for spreadsheet compatibility (so Excel/Sheets treat as text)
"""

import csv
import os
import re

# ============================================================================
# CONFIGURATION - Set your file and the column that contains phone numbers
# ============================================================================

CONFIG = {
    # Input/Output
    'INPUT_CSV': 'Bunch 3 - Final.csv',
    'OUTPUT_CSV': 'Bunch 3 - Ready.csv',   # Can be same file to overwrite, or a new path

    # Column that contains phone numbers (exact header name from your CSV)
    'COLUMN_PHONE': 'Company Phone',

    # Output column for cleaned numbers (if None, overwrites COLUMN_PHONE in place)
    'COLUMN_PHONE_OUTPUT': 'Company Phone',  # e.g. 'Phone Cleaned' to add a new column
}

# ============================================================================
# CLEANING LOGIC
# ============================================================================

def clean_phone(raw: str) -> str:
    """
    Clean a phone number for German-style use and spreadsheet compatibility.
    - Keeps only digits and a leading + (country code).
    - Fixes +490... -> +49 (drop erroneous 0 after German country code).
    - Leaves leading 0 as-is for local format (0...).
    - Returns value prefixed with ' so sheets don't reformat (e.g. strip leading zeros).
    """
    if raw is None or not isinstance(raw, str):
        return ""

    s = raw.strip()
    if not s:
        return ""

    # Build string: optional leading +, then only digits
    result = []
    if s.startswith('+'):
        result.append('+')
        s = s[1:]
    for c in s:
        if c.isdigit():
            result.append(c)

    num = ''.join(result)
    if not num:
        return ""

    # German: +490... is wrong (e.g. +49 (0)30... -> we get +49030...). Normalize to +49
    if num.startswith('+490'):
        num = '+49' + num[4:]

    # Prefix with ' so spreadsheet treats as text (keeps leading zeros, no auto-format)
    return "'" + num


def main():
    print("\n" + "=" * 60)
    print("Phone number cleaner")
    print("=" * 60)
    print(f"Input:  {CONFIG['INPUT_CSV']}")
    print(f"Output: {CONFIG['OUTPUT_CSV']}")
    print(f"Phone column: {CONFIG['COLUMN_PHONE']}")
    print(f"Output column: {CONFIG['COLUMN_PHONE_OUTPUT']}")
    print("=" * 60)

    if not os.path.exists(CONFIG['INPUT_CSV']):
        print(f"\nError: Input file not found: {CONFIG['INPUT_CSV']}")
        return

    # Read
    with open(CONFIG['INPUT_CSV'], 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames else []
        rows = list(reader)

    if not rows:
        print("No rows in file.")
        return

    if CONFIG['COLUMN_PHONE'] not in fieldnames:
        print(f"\nError: Column '{CONFIG['COLUMN_PHONE']}' not found. Available: {fieldnames}")
        return

    out_col = CONFIG['COLUMN_PHONE_OUTPUT'] or CONFIG['COLUMN_PHONE']
    if out_col not in fieldnames:
        fieldnames.append(out_col)

    # Clean
    cleaned_count = 0
    for row in rows:
        raw = row.get(CONFIG['COLUMN_PHONE'], '')
        cleaned = clean_phone(raw)
        row[out_col] = cleaned
        if cleaned:
            cleaned_count += 1

    # Write
    with open(CONFIG['OUTPUT_CSV'], 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Processed {len(rows)} rows, {cleaned_count} non-empty phone numbers written.")
    print("Values are prefixed with ' for spreadsheet compatibility.\n")


if __name__ == "__main__":
    main()
