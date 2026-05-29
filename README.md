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

# Apollo (used by seller pipelines with Apollo stage)
APOLLO_API_KEY=your_apollo_api_key

# New seller niche pipeline config prefix (amazon_seller_niche_pipeline.py)
SELLERNICHE_INPUT_FILE=data/seller_niche_input.csv
SELLERNICHE_STAGE1_OUTPUT_FILE=data/seller_niche_stage1.csv
SELLERNICHE_STAGE2_OUTPUT_FILE=data/seller_niche_stage2.csv
SELLERNICHE_RUN_STAGE2_PERSON_STAGE=true
SELLERNICHE_AMAZON_DOMAIN=amazon.de
SELLERNICHE_MODEL=gpt-5-mini
```

- **main.py** needs `API_KEY` for [Web Scraping API](https://www.webscrapingapi.com/) (Amazon seller/products).
- **gpt.py** needs `OPENAI_API_KEY` for OpenAI (e.g. GPT).
- **amazon_seller_niche_pipeline.py** needs `API_KEY` and `OPENAI_API_KEY`. It needs `APOLLO_API_KEY` only when `SELLERNICHE_RUN_STAGE2_PERSON_STAGE=true` (person + Apollo stage).
- **company_apollo_enrich.py** needs `OPENAI_API_KEY` and `APOLLO_API_KEY` (no Web Scraping API).

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

## Alternative Flow: Amazon Resellers Pipeline

Use `amazon_resellers_pipeline.py` when your input has an `Amazon Link` column (URL or ASIN).

- **Stage 1:** product endpoint by ASIN → adds `Amazon Brand`, `Amazon Title`
- **Stage 2:** brand search → adds `Product1 Title`, `Product1 Link`, `Product2 Title`, `Product2 Link`
- **Stage 3:** GPT cleanup of titles → final keeps original input columns + `Product1 Link`, `Product2 Link`, `Product1 Name`, `Product2 Name`
- Batch writes are built-in (10 / 5 / 5 defaults), with verbose live logs in terminal.

Run:

```bash
python amazon_resellers_pipeline.py
```

Configure through `.env` using the same `config_env.py` style as other scripts (`none/null/empty` supported for nullable numeric keys).

## Alternative Flow: Seller Niche + Contact Pipeline

Use `amazon_seller_niche_pipeline.py` when your input has seller lead columns including `Seller ID` and `Seller Link`.

- Reads CSV or XLSX input.
- Extracts ASIN/product id from `Seller Link`, calls product endpoint, and GPT classifies `niche rank` (`good`, `neutral`, `bad`) from title/rating/review count.
- Writes Stage 1 output in real time: input headers + `niche rank`.
- Calls seller profile endpoint by `Seller ID`, extracts seller about/description, and GPT extracts one text contact (`text email`, `text number`, `text name`, `text title`).
- Runs Apollo enrichment with domain from text email and writes `apollo name`, `apollo title`, `apollo email` (verified).
- Writes Stage 2 output in real time: input headers + `niche rank` + text/apollo fields.

Run:

```bash
python amazon_seller_niche_pipeline.py
```

## Alternative Flow: Company Apollo Enrichment

Use `company_apollo_enrich.py` for a CSV with **Company Name**, **Website**, and optional **Email** (separate columns). No Amazon API.

- Resolves company domain from **Website** first; uses **Email** only when website is empty (and `APOLLOCOMPANY_COLUMN_EMAIL` is set in `.env`).
- Apollo: find organization → list employees → GPT ranks up to 3 suitable contacts (same rules as seller pipeline Stage 5).
- Returns the first GPT pick with a **verified** Apollo email; stores Apollo `person id` from the match response.
- Optional phone: `APOLLOCOMPANY_REVEAL_PHONE_NUMBER=true` + `APOLLOCOMPANY_WEBHOOK_URL` — `people/match` gets `reveal_phone_number` and `webhook_url`; `person phone` stays `null` until your webhook fills it later.
- Output: all input columns + `person name`, `person title`, `person email`, `person phone`, `person id`.

```bash
python company_apollo_enrich.py
```

Example `.env`:

```env
OPENAI_API_KEY=...
APOLLO_API_KEY=...
APOLLOCOMPANY_INPUT_CSV=data/company_leads.csv
APOLLOCOMPANY_OUTPUT_CSV=data/company_leads_apollo.csv
# APOLLOCOMPANY_APOLLO_SEARCH_BY_NAME_FALLBACK=true
# APOLLOCOMPANY_COLUMN_COMPANY_NAME=Company Name,company name
# APOLLOCOMPANY_COLUMN_WEBSITE=Website,website
# APOLLOCOMPANY_COLUMN_EMAIL=Email,email
# APOLLOCOMPANY_COLUMN_EMAIL=
APOLLOCOMPANY_REVEAL_PHONE_NUMBER=false
APOLLOCOMPANY_WEBHOOK_URL=
```

| Env var | Default | Purpose |
|---------|---------|---------|
| `APOLLOCOMPANY_INPUT_CSV` | `data/company_leads.csv` | Input CSV |
| `APOLLOCOMPANY_OUTPUT_CSV` | `data/company_leads_apollo.csv` | Output CSV |
| `APOLLOCOMPANY_MODEL` | `gpt-5-mini` | OpenAI model |
| `APOLLOCOMPANY_APOLLO_SEARCH_BY_NAME_FALLBACK` | `false` | Search Apollo by name when domain missing |
| `APOLLOCOMPANY_SKIP_EXISTING` | `true` | Skip rows already enriched in output |
| `APOLLOCOMPANY_WRITE_BATCH_SIZE` | `50` | Rows per CSV flush |
| `APOLLOCOMPANY_COLUMN_COMPANY_NAME` | `Company Name,company name,...` | Header aliases |
| `APOLLOCOMPANY_COLUMN_WEBSITE` | `Website,website` | Website header aliases |
| `APOLLOCOMPANY_COLUMN_EMAIL` | *(empty = disabled)* | Email header aliases; empty env disables email fallback |
| `APOLLOCOMPANY_REVEAL_PHONE_NUMBER` | `false` | Request Apollo phone reveal via webhook on `people/match` |
| `APOLLOCOMPANY_WEBHOOK_URL` | *(empty)* | Required when reveal is true; Apollo POSTs phone data here later |

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
- **[DOCKER_VPS.md](DOCKER_VPS.md)** – run on a VPS with Docker; config via env vars (no code edit); Docker vs plain Python.
- **phone_clean.py** – cleans phone numbers in a CSV column (digits only, +49 fix, `'` prefix for sheets); run after gpt.py.
- **amazon_resellers_pipeline.py** – 3-stage reseller flow (`Amazon Link` -> brand/title -> 2 related products -> GPT-clean names).
- **ping_check.py** – utility (connectivity check); not part of the main pipeline.
- **.env** – not committed; hold `API_KEY` and `OPENAI_API_KEY` here.
- **backup_before_docker/** – backup of code before Docker/env-based config was added.

## License

See [LICENSE](LICENSE).
