"""
Pick one random row per unique value in a CSV column.

Multiple rows may share the same key (e.g. Scraped Seller ID); this script
outputs one randomly chosen row for each distinct key value.

Usage:
  python csv_unique_random.py data/import_stage5.csv --key "Scraped Seller ID"
  python csv_unique_random.py input.csv -k Seller --output deduped.csv --seed 42
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def resolve_column(fieldnames: List[str], key: str) -> str:
    if not fieldnames:
        raise ValueError("CSV has no header row.")
    if key in fieldnames:
        return key
    lower_map = {h.lower(): h for h in fieldnames}
    found = lower_map.get(key.lower())
    if found:
        return found
    raise ValueError(
        f"Column {key!r} not found. Available columns: {', '.join(fieldnames)}"
    )


def pick_one_per_key(
    rows: List[dict], column: str, rng: random.Random
) -> tuple[List[dict], int]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        value = (row.get(column) or "").strip()
        groups[value].append(row)

    picked = [rng.choice(group) for group in groups.values()]
    duplicate_rows = sum(len(g) - 1 for g in groups.values())
    return picked, duplicate_rows


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_unique_random{input_path.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Output one random row per unique value in a CSV column."
    )
    parser.add_argument("input_csv", help="Input CSV path")
    parser.add_argument(
        "-k",
        "--key",
        required=True,
        help="Header name to group by (e.g. 'Scraped Seller ID')",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV path (default: <input>_unique_random.csv)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible picks",
    )
    args = parser.parse_args()

    input_path = Path(args.input_csv)
    if not input_path.is_file():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else default_output_path(input_path)
    rng = random.Random(args.seed)

    with open(input_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        column = resolve_column(fieldnames, args.key)
        rows = list(reader)

    if not rows:
        print("Input CSV has no data rows.")
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore").writeheader()
        print(f"Wrote empty output: {output_path}")
        return 0

    picked, dropped = pick_one_per_key(rows, column, rng)
    picked.sort(key=lambda r: (r.get(column) or "").strip().lower())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(picked)

    unique_keys = len(picked)
    print(f"Input rows:     {len(rows)}")
    print(f"Unique {column}: {unique_keys}")
    print(f"Rows dropped:   {dropped}")
    print(f"Output rows:    {len(picked)}")
    print(f"Wrote:          {output_path}")
    if args.seed is not None:
        print(f"Random seed:    {args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
