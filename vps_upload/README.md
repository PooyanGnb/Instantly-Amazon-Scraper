# VPS upload package – Amazon Scraper Docker

Upload this **entire folder** to your VPS (e.g. `~/amazon-scraper`). No need to upload backup, venv, or CSV files from your local machine.

## Quick start on the VPS

1. **Create `.env` from the example**
   ```bash
   cp .env.example .env
   nano .env   # or vim: add your real API_KEY and OPENAI_API_KEY
   ```

2. **Put your leads CSV in `data/`**
   - e.g. `data/leads.csv` (must match `INPUT_CSV` in `.env`)

3. **Build and run** (so it keeps running after you disconnect, use the script):
   ```bash
   docker compose build
   chmod +x run_survive.sh
   ./run_survive.sh main.py         # step 1: scrape → log in main.log
   # Then set INPUT_CSV=data/enriched.csv, OUTPUT_CSV=data/final.csv in .env
   ./run_survive.sh gpt.py          # step 2: GPT → gpt.log
   ./run_survive.sh phone_clean.py  # step 3: phones → phone_clean.log
   ```
   After reconnect: `tail -f main.log` (or the log for the step you ran).

Full instructions and all env vars: **DOCKER_VPS.md** (includes upload/download to VPS with scp/rsync).

## Output files and logs

- **Output CSVs** are written into `data/` on the VPS (the same folder is mounted into the container). You do **not** need to extract anything from the container.
- **Logs** appear in the terminal when you run `docker compose run --rm app python main.py` (or gpt.py / phone_clean.py). To also save to a file: `docker compose run --rm app python main.py 2>&1 | tee main.log`

See **DOCKER_VPS.md** §4 for details.

## Contents

| File / folder   | Purpose |
|-----------------|--------|
| `.env.example`  | Template for `.env` – copy to `.env` and add your API keys |
| `data/`         | Put input CSVs here; output CSVs appear here on the VPS |
| `run_survive.sh` | Run a step in background so it survives SSH disconnect; logs to main.log / gpt.log / phone_clean.log |
| `DOCKER_VPS.md` | Full Docker + env var guide; output files and logs |
| `PROJECT.md`    | Pipeline order and script purposes |
