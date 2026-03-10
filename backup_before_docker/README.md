# Instantly Amazon Scraper

> **Quick reference:** See **[PROJECT.md](PROJECT.md)** for the **order of use**, purpose of each script, and a checklist so you don’t forget the workflow.

This project takes a list of **Amazon seller leads**, scrapes their Amazon store pages to get product data, then uses GPT to produce **two variables** per lead for use in email campaigns (e.g. short product name and visual selling points).

## Overview

| Step | Script   | What it does |
|------|----------|--------------|
| 1    | **main.py** | Reads a CSV of Amazon leads (Seller ID, Seller Name, etc.), calls the Web Scraping API to fetch each seller’s top product and its details, and writes an **enriched** CSV with Product URL, Product Name, and Product Description. |
| 2    | **gpt.py**  | Reads the enriched CSV, sends batches of product names and descriptions to GPT, and writes a **final** CSV with two extra columns: **Variable 1** (short product name) and **Variable 2** (visual selling points). |

So: **Leads CSV → main.py (scrape) → Enriched CSV → gpt.py (GPT) → Final CSV with 2 variables.**

## Pipeline

```
Input CSV (leads)     main.py (scrape)      Enriched CSV      gpt.py (GPT)      Final CSV
─────────────────→   ─────────────────→    ─────────────→    ─────────────→   ─────────────
Seller ID, Name, …   Web Scraping API      + Product URL,     OpenAI API        + Variable 1,
                     (seller products +    Product Name,      (batch)           Variable 2
                      product details)      Product Description
```

## The Two Amazon Variables (from gpt.py)

For each lead/product, GPT returns:

| Variable   | Description |
|-----------|-------------|
| **Variable 1** | Short, memorable product name (3–4 words, German), suitable for use in acquisition emails. |
| **Variable 2** | 2–3 concrete, visually representable selling points/features (from the product description), comma-separated, in German, for use in visuals. |

These are written as extra columns in the final CSV.

## Setup

### 1. Dependencies

```bash
pip install -r requirements.txt
```

- `requests` – API calls (Web Scraping API)
- `python-dotenv` – load `.env`
- `openai` – GPT (Responses API)

### 2. Environment variables

Create a `.env` in the project root:

```env
# Web Scraping API (used by main.py)
API_KEY=your_web_scraping_api_key

# OpenAI (used by gpt.py)
OPENAI_API_KEY=your_openai_api_key
```

- **main.py** needs `API_KEY` for [Web Scraping API](https://www.webscrapingapi.com/) (Amazon seller/products).
- **gpt.py** needs `OPENAI_API_KEY` for OpenAI (e.g. GPT).

## Usage

### Step 1: Scrape Amazon (main.py)

1. Put your leads CSV in the project folder (e.g. `NEW Jan 2026 - Not Reached Yet.csv`).
2. In **main.py**, set `CONFIG`:
   - `INPUT_CSV` – input leads file name
   - `OUTPUT_CSV` – enriched output (e.g. `NEW enriched_sellers.csv`)
   - `START_FROM_LINE` / `MAX_RECORDS` – optional range
   - `AMAZON_DOMAIN` – e.g. `amazon.de`
   - `WAIT_BETWEEN_REQUESTS` – delay between API calls
3. Run:

```bash
python main.py
```

Output: **Enriched CSV** with original columns plus **Product URL**, **Product Name**, **Product Description**.

### Step 2: GPT variables (gpt.py)

1. Use the enriched CSV as input (e.g. `NEW enriched_sellers.csv`).
2. In **gpt.py**, set `CONFIG`:
   - `INPUT_CSV` – enriched file
   - `OUTPUT_CSV` – final file (e.g. `NEW final_sellers.csv`)
   - `BATCH_SIZE` – records per GPT call (e.g. 5)
   - `MODEL` – e.g. `gpt-5-mini`
   - `START_FROM_LINE` / `MAX_RECORDS` – optional
3. Run:

```bash
python gpt.py
```

Output: **Final CSV** with all previous columns plus **Variable 1** and **Variable 2**.

## Configuration summary

### main.py

| Key                    | Purpose |
|------------------------|--------|
| `INPUT_CSV`            | Leads CSV filename |
| `OUTPUT_CSV`           | Enriched CSV filename |
| `START_FROM_LINE`      | 1-based record to start from (or `None`) |
| `MAX_RECORDS`          | Max records to process (or `None` = all) |
| `WAIT_BETWEEN_REQUESTS`| Seconds between API requests |
| `AMAZON_DOMAIN`        | e.g. `amazon.de` |

### gpt.py

| Key                    | Purpose |
|------------------------|--------|
| `INPUT_CSV`            | Enriched CSV filename |
| `OUTPUT_CSV`           | Final CSV filename |
| `START_FROM_LINE`      | 1-based record to start from (or `None`) |
| `MAX_RECORDS`          | Max records (or `None` = all) |
| `BATCH_SIZE`           | Records per GPT call |
| `MODEL`                | OpenAI model (e.g. `gpt-5-mini`) |
| `WAIT_BETWEEN_BATCHES` | Seconds between batch API calls |

## Input CSV (leads)

Expected to contain at least:

- **Seller ID** – used to fetch seller products and to skip already-processed rows in both scripts.

Other columns (Seller Name, Email, etc.) are passed through to the enriched and final CSVs.

## Output columns added

- **After main.py:** `Product URL`, `Product Name`, `Product Description`
- **After gpt.py:** `Variable 1`, `Variable 2` (short product name and visual selling points for Amazon/email use)

## Resuming

- **main.py** and **gpt.py** both skip sellers that already appear in their output file (by **Seller ID**). You can re-run and they will only process new leads.

## Other files

- **[PROJECT.md](PROJECT.md)** – project reference: order of use, purpose of each script, config summary, checklist.
- **phone_clean.py** – cleans phone numbers in a CSV column (digits only, +49 fix, `'` prefix for sheets); run after gpt.py.
- **ping_check.py** – utility (connectivity check); not part of the main pipeline.
- **.env** – not committed; hold `API_KEY` and `OPENAI_API_KEY` here.

## License

See [LICENSE](LICENSE).
