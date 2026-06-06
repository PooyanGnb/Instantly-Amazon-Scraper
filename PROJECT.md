# Project reference – order of use & script purposes

Use this doc so you don’t forget the pipeline order, what each script does, and how to configure it.

---

## Order of use (workflow)

Run the scripts in this order. Each step uses the output of the previous one (or a leads CSV for step 1).

| Step | Script        | Input file (example)     | Output file (example)     |
|------|---------------|---------------------------|----------------------------|
| **1** | **main.py**   | Leads CSV (e.g. `Bunch 3 - Verified.csv`) | Enriched CSV (e.g. `Bunch 3 - Amazon.csv`) |
| **2** | **gpt.py**    | Enriched CSV (e.g. `Bunch 3 - Amazon.csv`) | Final CSV (e.g. `Bunch 3 - Final.csv`) |
| **3** | **phone_clean.py** | Final CSV (e.g. `Bunch 3 - Final.csv`) | Ready CSV (e.g. `Bunch 3 - Ready.csv`) |
| **Alt flow** | **amazon_resellers_pipeline.py** | CSV with `Amazon Link` | Final reseller CSV (`Product1 Link`, `Product2 Link`, `Product1 Name`, `Product2 Name`) |
| **Alt flow** | **amazon_seller_niche_pipeline.py** | CSV/XLSX with `Seller ID` + `Seller Link` + lead columns | CSV/XLSX with niche rank + text contact + Apollo contact columns |
| **Alt flow** | **company_apollo_enrich.py** | CSV with `Company Name` + `Website` or email | Same columns + `person name`, `person title`, `person email`, `person phone`, `person id` |
| **Alt flow** | **amazon_import_products_pipeline.py** | CSV like `ImportProducts.csv` (ASIN/URL, Seller, Brand, seller count) | Stage 5: sellers enriched + Apollo contacts (`import_stage5.csv`); resellers split in Stage 1 |

**One-line summary:**  
Leads → **main.py** (scrape products) → **gpt.py** (Variable 1 & 2) → **phone_clean.py** (clean phones) → ready for use (e.g. email campaigns).

---

## Purpose of each script

### 1. main.py – Amazon product enrichment

- **Purpose:** Turn a list of Amazon seller leads into an enriched CSV with product data.
- **What it does:**
  - Reads a CSV of leads (must have a way to get **Seller ID**: column “Seller ID” or “Seller URL”).
  - For each seller, calls the Web Scraping API to get their top product, then product details.
  - Writes the same rows plus: **Product URL**, **Product Name**, **Product Description**.
- **Needs:** `.env` with `API_KEY` (Web Scraping API).
- **Config (top of file):** `INPUT_CSV`, `OUTPUT_CSV`, `START_FROM_LINE`, `MAX_RECORDS`, `AMAZON_DOMAIN`, `COLUMN_SELLER_ID`, `COLUMN_SELLER_NAME`.
- **Resume:** Skips sellers that are already in the output file (by Seller ID).

---

### 2. gpt.py – Variable 1 & Variable 2 for emails

- **Purpose:** Add two columns per row for use in email campaigns: short product name and visual selling points.
- **What it does:**
  - Reads the enriched CSV (with Product Name, Product Description).
  - Sends batches to GPT; GPT returns **Variable 1** (short product name, German) and **Variable 2** (visual selling points, German).
  - Writes the same columns plus **Variable 1**, **Variable 2**.
- **Needs:** `.env` with `OPENAI_API_KEY`.
- **Config (top of file):** `INPUT_CSV`, `OUTPUT_CSV`, `BATCH_SIZE`, `MODEL`, `START_FROM_LINE`, `MAX_RECORDS`, `COLUMN_SELLER_ID`, `COLUMN_SELLER_NAME`, `COLUMN_PRODUCT_NAME`, `COLUMN_PRODUCT_DESCRIPTION`.
- **Resume:** Skips rows that are already in the output file (by Seller ID).
- **Structure-agnostic:** Uses actual CSV headers; no fixed column list. Map your column names in CONFIG if they differ.

---

### 3. phone_clean.py – Clean phone numbers for spreadsheets

- **Purpose:** Clean the phone column and make it spreadsheet-safe (no auto-formatting, leading zeros kept).
- **What it does:**
  - Reads the CSV and the column you set (e.g. “Company Phone”).
  - Keeps only digits and a leading `+`; removes spaces, brackets, dashes, etc.
  - Fixes German numbers: `+490...` → `+49...`.
  - Prefixes each value with `'` so Excel/Sheets treat it as text.
  - Writes to the same or another column (or same file).
- **Needs:** No API; no `.env`.
- **Config (top of file):** `INPUT_CSV`, `OUTPUT_CSV`, `COLUMN_PHONE`, `COLUMN_PHONE_OUTPUT`.

---

---

### 6. amazon_import_products_pipeline.py – ImportProducts classify + seller enrich (5 stages)

- **Purpose:** Classify product rows as Amazon sellers vs resellers, then enrich **sellers only** through scrape, GPT, and Apollo.
- **Stage 1:** Read input CSV; map columns via `IMPORT_COLUMN_*` in `.env`. Classify: Amazon retail → reseller; brand–seller name match (fuzzy) → seller; otherwise → reseller. Seller count is ignored. Output `import_sellers.csv` + `import_resellers.csv` (sorted by seller name A→Z). Appends `asin`.
- **Stage 2:** Product API per ASIN; append `Scraped Seller Name`, `Scraped Seller ID`, `Scraped Seller URL`. Cache by input seller name.
- **Stage 3:** Seller profile per `Scraped Seller ID`; region filter **DE/CH/AT** only; append region, postal code, rating, reviews, description, about. Wiped = region failures only (no product cap).
- **Stage 4:** GPT extracts `seller email`, `seller number`, `seller incharge person`, `seller person title` from about + description.
- **Stage 5:** Apollo domain search (no name fallback); match Stage 4 person in org people list + GPT outreach pick. Appends `matched_contact_apollo_*` and `outreach_apollo_*` columns.
- **Needs:** `.env` with `API_KEY`, `OPENAI_API_KEY`, `APOLLO_API_KEY`, and `IMPORT_*` overrides.
- **Run:** `python amazon_import_products_pipeline.py` or `python amazon_import_products_pipeline.py --stage 3`

### 5. amazon_resellers_pipeline.py – Reseller discovery + product-name cleanup (3 stages)

- **Purpose:** End-to-end pipeline for reseller inputs where each row has an `Amazon Link` (URL or ASIN).
- **Stage 1:** Extract ASIN from `Amazon Link`, call product endpoint, add `Amazon Brand` + `Amazon Title`. Writes in batches of 10 by default.
- **Stage 2:** Search Amazon by `Amazon Brand`, pick 2 *different* products (not same as source title, avoid near-duplicate variants), add `Product1 Title`, `Product1 Link`, `Product2 Title`, `Product2 Link`. Writes in batches of 5 by default.
- **Stage 3:** GPT cleans `Product1/2 Title` to pure names (`Product1 Name`, `Product2 Name`), then writes final CSV with:
  - original input columns + `Product1 Link`, `Product2 Link`, `Product1 Name`, `Product2 Name`
  - removes temporary stage columns (`Amazon Brand`, `Amazon Title`, `Product1 Title`, `Product2 Title`)
- **Needs:** `.env` with both `API_KEY` and `OPENAI_API_KEY`.
- **Config:** uses same env-override style as other scripts through `config_env.py` (supports none/null/empty behavior).

### 4. ping_check.py – Utility (not part of pipeline)

- **Purpose:** Quick network check (pings 8.8.8.8).
- **Use:** When you want to verify connectivity; not required for the main workflow.

---

## Other information

### Environment (.env)

In project root, create `.env` with:

```env
API_KEY=your_web_scraping_api_key
OPENAI_API_KEY=your_openai_api_key
APOLLO_API_KEY=your_apollo_api_key

# New seller niche pipeline (amazon_seller_niche_pipeline.py)
SELLERNICHE_INPUT_FILE=data/seller_niche_input.csv
SELLERNICHE_STAGE1_OUTPUT_FILE=data/seller_niche_stage1.csv
SELLERNICHE_STAGE2_OUTPUT_FILE=data/seller_niche_stage2.csv
SELLERNICHE_RUN_STAGE2_PERSON_STAGE=true
SELLERNICHE_AMAZON_DOMAIN=amazon.de
SELLERNICHE_MODEL=gpt-5-mini
```

- **main.py** uses `API_KEY`.
- **gpt.py** uses `OPENAI_API_KEY`.
- **phone_clean.py** does not use `.env`.
- **amazon_seller_niche_pipeline.py** uses `API_KEY`, `OPENAI_API_KEY`, and `SELLERNICHE_*` overrides; `APOLLO_API_KEY` is required only when `SELLERNICHE_RUN_STAGE2_PERSON_STAGE=true`.
- **company_apollo_enrich.py** uses `OPENAI_API_KEY`, `APOLLO_API_KEY`, and `APOLLOCOMPANY_*` overrides (no Web Scraping API).
- **amazon_import_products_pipeline.py** uses `API_KEY`, `OPENAI_API_KEY`, `APOLLO_API_KEY`, and `IMPORT_*` overrides.

```env
# amazon_import_products_pipeline.py (prefix IMPORT_)
IMPORT_INPUT_CSV=data/ImportProducts.csv
IMPORT_COLUMN_ASIN_OR_URL=ASIN,URL
IMPORT_COLUMN_SELLER_COUNT=Number of Active Sellers
IMPORT_COLUMN_SELLER_NAME=Seller
IMPORT_COLUMN_BRAND_NAME=Brand
IMPORT_STAGE5_OUTPUT_CSV=data/import_stage5.csv
```

### CSV structure (flexible)

- **main.py** and **gpt.py** support different CSV layouts. At the top of each script you can set:
  - Which column(s) to use for Seller ID (e.g. “Seller ID” or “Seller URL”; if URL, ID is extracted automatically).
  - Seller name, product name, product description (gpt.py).
- If your CSV has different column names, edit the `COLUMN_*` lists in CONFIG; order = priority.

### Suggested file naming (example for one “bunch”)

- `Bunch 3 - Verified.csv` → leads (input to main.py)
- `Bunch 3 - Amazon.csv` → after main.py (enriched with product data)
- `Bunch 3 - Final.csv` → after gpt.py (with Variable 1 & 2)
- `Bunch 3 - Ready.csv` → after phone_clean.py (phones cleaned, ready for use)

### Resuming

- **main.py** and **gpt.py** both skip rows that are already present in their output file (matched by Seller ID). You can re-run and they will only process new rows.

### Dependencies

```bash
pip install -r requirements.txt
```

(requests, python-dotenv, openai)

### Config without editing code (Docker / VPS)

You can override all important settings with **environment variables** (no code change). See **[DOCKER_VPS.md](DOCKER_VPS.md)** for:
- Running in Docker on a VPS
- Full list of env vars (INPUT_CSV, OUTPUT_CSV, START_FROM_LINE, MAX_RECORDS, etc.)
- Docker vs plain Python on VPS

---

## Quick checklist for a new batch

1. Put leads CSV in project (or use existing, e.g. `Bunch X - Verified.csv`).
2. In **main.py**: set `INPUT_CSV` and `OUTPUT_CSV`, run `python main.py`.
3. In **gpt.py**: set `INPUT_CSV` = output of step 2, `OUTPUT_CSV` = final file, run `python gpt.py`.
4. In **phone_clean.py**: set `INPUT_CSV` = output of step 3, `OUTPUT_CSV` and phone column, run `python phone_clean.py`.
5. Use the last output (e.g. `Bunch X - Ready.csv`) for campaigns.

### Alternative reseller flow

1. Set `.env` keys for `amazon_resellers_pipeline.py` (input + stage outputs + final output).
2. Run `python amazon_resellers_pipeline.py`.
3. Use `FINAL_OUTPUT_CSV` (contains input columns + Product1/2 links + Product1/2 cleaned names).

### Alternative seller niche flow

1. Prepare a CSV/XLSX with headers: `Seller ID`, `Seller Link`, `PL URL`, `Seller Name`, `Website`, `Estimated Monthly Revenue`, `First Name`, `Last Name`, `Title`, `Email`, `Source`, `Phone Number`, `Country`, `State`.
2. Set `.env` keys with `SELLERNICHE_` prefix (at least input/output paths).
3. Run `python amazon_seller_niche_pipeline.py`.
4. Stage 1 output: input columns + `niche rank` (streamed writes).
5. Stage 2 output: input columns + `niche rank`, `text email`, `text number`, `text name`, `text title`, `apollo name`, `apollo title`, `apollo email` (streamed writes).

### Alternative import products flow

1. Put your CSV in `data/` (e.g. `ImportProducts.csv` with `URL`, `ASIN`, `Brand`, `Seller`, `Number of Active Sellers`).
2. Set `IMPORT_*` keys in `.env` (column mapping + output paths).
3. Run `python amazon_import_products_pipeline.py`.
4. Use `IMPORT_STAGE5_OUTPUT_CSV` for enriched sellers; `IMPORT_STAGE1_RESELLERS_OUTPUT_CSV` for classified resellers.

**Stage 1 rules:** Amazon retail → reseller; seller name matches brand (fuzzy, e.g. `KEMMLIT` ↔ `KEMMLIT Bauelemente GmbH`) → seller; otherwise → reseller. Seller count is not used.

### Alternative company Apollo flow

Use **`company_apollo_enrich.py`** when you have a plain company list (no Amazon scrape): **Company Name**, **Website**, and optional **Email** as separate columns.

1. Put your CSV in the project (e.g. `data/company_leads.csv`). All original columns are kept.
2. In `.env` set `APOLLOCOMPANY_INPUT_CSV`, `APOLLOCOMPANY_OUTPUT_CSV`, plus `OPENAI_API_KEY` and `APOLLO_API_KEY`.
3. Run `python company_apollo_enrich.py`.
4. Output adds: `person name`, `person title`, `person email`, `person phone`, `person id` (best of up to 3 GPT-ranked Apollo contacts with verified email; `null` when none).

**Domain rules:** **Website** is used first when the row has a non-empty website value (`http`/`https`/`www` and paths stripped to a host like `link.com`). **Email** domain is used only when website is empty and `APOLLOCOMPANY_COLUMN_EMAIL` is set in `.env` (leave empty to disable email entirely). Public mail domains are rejected via `public_email_domains.txt`. Optional `APOLLOCOMPANY_APOLLO_SEARCH_BY_NAME_FALLBACK=true` when no domain is available.

**Phone reveal (optional):** `APOLLOCOMPANY_REVEAL_PHONE_NUMBER=true` requires `APOLLOCOMPANY_WEBHOOK_URL`. The script sends `reveal_phone_number` and `webhook_url` on `people/match`; `person phone` is written as `null` until Apollo posts the number to your webhook. Use `person id` (Apollo `id` from the match response) to correlate callback data with CSV rows. A webhook receiver that auto-updates the CSV is not included in this repo.

**Column aliases** (first match wins): `APOLLOCOMPANY_COLUMN_COMPANY_NAME`, `APOLLOCOMPANY_COLUMN_WEBSITE`, `APOLLOCOMPANY_COLUMN_EMAIL` (comma-separated lists).

**Resume:** `APOLLOCOMPANY_SKIP_EXISTING=true` (default) skips rows already in the output file with a non-null `person email` (matched by company + website + email); new rows are appended.
