# Running the project on a VPS with Docker

This guide explains how to run the Amazon Scraper pipeline in Docker on an Ubuntu VPS so you can use a stable connection and change config (input/output files, start/end line, etc.) **without editing code**—via environment variables.

---

## Is Docker the best way on a VPS?

**Short answer:** Both work. For your case (stable connection, fewer connection errors), either is fine.

| Approach | Pros | Cons |
|----------|------|------|
| **Docker** | Same environment everywhere; no “works on my machine”; easy to re-deploy; config via env vars. | Extra layer; slightly more setup. |
| **Plain Python on VPS** | Simpler: upload code, `pip install -r requirements.txt`, run `python main.py`. No Docker needed. | You manage Python version and dependencies yourself. |

**Recommendation:** If you’re comfortable with SSH and Python, **running the scripts directly on Ubuntu** (upload repo, create `.env`, run `python main.py` then `gpt.py` then `phone_clean.py`) is the simplest way and will give you the same stable connection. Use **Docker** if you want a fixed, reproducible environment or plan to run several services on the same VPS.

Config is now **environment-based** in both cases: with or without Docker, you can set `INPUT_CSV`, `OUTPUT_CSV`, `START_FROM_LINE`, etc. in `.env` or in the shell, and the scripts will use those values without changing code.

---

## Run on VPS without Docker (simplest)

You **don’t need Docker**. Same scripts, same `.env`, same stable connection.

1. **Upload the project** to the VPS (e.g. `~/amazon-scraper`). Put your leads CSV in the project folder or in a `data/` subfolder (and set `INPUT_CSV` in `.env` to match, e.g. `data/leads.csv` or `leads.csv`).

2. **Install Python 3 and dependencies** (Ubuntu):
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip python3-venv
   cd ~/amazon-scraper
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Create `.env`** in the project root with `API_KEY`, `OPENAI_API_KEY`, and any overrides (`INPUT_CSV`, `OUTPUT_CSV`, etc.). Same as for Docker.

4. **Run the pipeline** (same order as in PROJECT.md):
   ```bash
   source venv/bin/activate   # if not already in venv
   python main.py             # step 1
   python gpt.py             # step 2 (set INPUT_CSV/OUTPUT_CSV in .env first)
   python phone_clean.py      # step 3
   ```

5. **To keep running after you disconnect**, use `nohup` and a log file:
   ```bash
   PYTHONUNBUFFERED=1 nohup python main.py >> main.log 2>&1 &
   tail -f main.log    # watch while connected; after reconnect, tail -f or cat main.log
   ```
   You’ll see `[1] 79244` (job ID) and `nohup: ignoring input` — that’s normal; the job is running. `PYTHONUNBUFFERED=1` makes output appear in `main.log` in real time so `tail -f` shows progress. Same idea for `gpt.py` → `gpt.log` and `phone_clean.py` → `phone_clean.log`. Or use **tmux** / **screen** and run the commands in the foreground inside a session.

No Docker install, no build step—just Python and the repo.

---

## Prerequisites on the VPS (for Docker)

- Ubuntu (or similar) with Docker and Docker Compose installed.
- A folder for the project (e.g. `~/amazon-scraper`).

Install Docker (if needed):

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# Log out and back in so the group takes effect
```

---

## 1. Get the code and data on the VPS

- Clone or upload the project into a directory, e.g. `~/amazon-scraper`.
- Put your **leads CSV** in the `data/` folder (e.g. `data/leads.csv`). All input/output CSVs will live under `data/` so they persist on the host.

---

## 2. Create `.env` on the VPS

In the project root (same folder as `docker-compose.yml`), create a `.env` file with your API keys and, optionally, config overrides:

```env
# Required for main.py
API_KEY=your_web_scraping_api_key

# Required for gpt.py
OPENAI_API_KEY=your_openai_api_key

# Optional: override defaults (no need to edit Python files)
INPUT_CSV=data/leads.csv
OUTPUT_CSV=data/enriched.csv
# START_FROM_LINE=1
# MAX_RECORDS=100
```

You can add more overrides here; see the **Environment variables** section below.

---

## 3. Build and run with Docker Compose

From the project root (use `docker-compose` instead of `docker compose` if your VPS has the older standalone binary):

```bash
docker compose build
docker compose up
```

By default this runs **main.py** (scraper step). To run a different step, override the command:

```bash
# Step 1: Scrape (default)
docker compose run --rm app python main.py

# Step 2: GPT (Variable 1 & 2)
docker compose run --rm app python gpt.py

# Step 3: Clean phones
docker compose run --rm app python phone_clean.py
```

For steps 2 and 3, set the right input/output in `.env` (or in `docker-compose.yml` under `environment`). Example for step 2:

```env
INPUT_CSV=data/enriched.csv
OUTPUT_CSV=data/final.csv
```

Then:

```bash
docker compose run --rm app python gpt.py
```

Output files will appear in `./data/` on the host.

---

## 4. Output files and logs

### Where are the output files?

**They are on the VPS directly.** You do **not** need to copy anything out of the container.

The compose file mounts your host folder `./data` into the container as `/app/data`. When a script writes to `data/enriched.csv`, it is writing to that shared folder, so the file appears at `./data/enriched.csv` on the VPS. You can open, download, or edit it there like any normal file.

### How do I see the same logs as when running locally?

When you run:

```bash
docker compose run --rm app python main.py
```

(or `gpt.py` / `phone_clean.py`), the script’s **stdout and stderr are attached to your terminal**. You get the same progress messages, batch info, and errors in the terminal where you ran the command—same as when you run the script locally without Docker.

To **also save** that output to a file (e.g. for later review or debugging):

```bash
docker compose run --rm app python main.py 2>&1 | tee main.log
```

Then you’ll see everything in the terminal and a copy in `main.log`. For gpt and phone_clean you can do the same with e.g. `tee gpt.log` and `tee phone_clean.log`.

### What happens if I get disconnected from the VPS while the code is running?

**Yes – the run is canceled.** If you run `docker compose run --rm app python main.py` in your SSH session and the connection drops (Wi‑Fi, sleep, network blip), the terminal gets a hangup signal and the process is usually killed. The container stops and you lose the rest of the run.

To avoid that, run the job in a way that **survives disconnect**, then reattach or read logs when you reconnect.

---

### Quick way: use the helper script (survives disconnect, no extra install)

In the project root there is **`run_survive.sh`**. It runs the step in the background with `nohup` and writes all output to a log file, so the job keeps running after you disconnect.

```bash
chmod +x run_survive.sh
./run_survive.sh main.py      # step 1: scrape (log: main.log)
./run_survive.sh gpt.py       # step 2: GPT (log: gpt.log)
./run_survive.sh phone_clean.py   # step 3: phones (log: phone_clean.log)
```

While still connected you can watch the log with `tail -f main.log`. After you **reconnect**, run `tail -f main.log` (if it’s still running) or `cat main.log` (when it’s finished) to see the full output.

---

### Option A: tmux or screen (same terminal when you come back)

Run the command inside **tmux** or **screen**. When you disconnect from SSH, the session stays alive on the VPS. When you reconnect, you reattach and see the same terminal with all output.

**Using tmux:**

```bash
# Install if needed: sudo apt install -y tmux
tmux new -s scraper
cd ~/amazon-scraper   # or your project path
docker compose run --rm app python main.py
# If you get disconnected, later:
tmux attach -t scraper
```

To detach without closing the session: press **Ctrl+B**, then **D**. To reattach: `tmux attach -t scraper`. When the script finishes, you can type `exit` or close the pane.

**Using screen:**

```bash
# Install if needed: sudo apt install -y screen
screen -S scraper
cd ~/amazon-scraper
docker compose run --rm app python main.py
# Detach: Ctrl+A then D. Reattach later:
screen -r scraper
```

---

### Option B: Docker detached + docker logs (see logs when reconnecting)

Run the container in the **background** (detached). After you reconnect, use `docker logs` to see or follow the output.

```bash
# Start in background (note the container ID printed at the end)
docker compose run -d --rm app python main.py

# List running containers (to find the one running main.py)
docker ps

# When you reconnect: stream logs (live) or show full output
docker logs -f <container_id>    # follow until it exits
docker logs <container_id>       # show all output after it finished
```

Replace `main.py` with `gpt.py` or `phone_clean.py` as needed. When the script exits, the container is removed (`--rm`), so you can’t get logs after that unless you saved them (e.g. Option C).

---

### Option C: nohup + log file (survives disconnect; read log file when back)

Run with **nohup** and redirect output to a file. The job keeps running after you disconnect. When you reconnect, read or tail the log file.

```bash
nohup docker compose run --rm app python main.py > main.log 2>&1 &
# Optional: see live output while still connected
tail -f main.log
```

You’ll see a job ID and `nohup: ignoring input` — that’s normal; the job is running.

After reconnect:

```bash
tail -f main.log    # follow if still running
cat main.log        # or just view full log when done
```

Use different filenames for each step (e.g. `gpt.log`, `phone_clean.log`).

---

## 5. Changing config without editing code

All important settings can be overridden with **environment variables**. The scripts read these after loading defaults, so you never need to change `INPUT_CSV`, `OUTPUT_CSV`, or start/end line in the code.

### Where to set them

- **Docker:** In `docker-compose.yml` under `environment:` or in a `.env` file in the project root (Docker Compose loads `.env` automatically).
- **Without Docker:** Export in the shell or put them in `.env` and run the scripts as usual.

### main.py (scraper)

| Variable | Example | Description |
|----------|---------|-------------|
| `INPUT_CSV` | `data/leads.csv` | Input leads CSV path (under `/app/data` in container). |
| `OUTPUT_CSV` | `data/enriched.csv` | Output enriched CSV path. |
| `START_FROM_LINE` | `1` or empty | First record to process (1-based). Omit or empty = from start. |
| `MAX_RECORDS` | `100` or empty | Max records to process. Omit or empty = all. |
| `WAIT_BETWEEN_REQUESTS` | `2` | Seconds between API requests. |
| `AMAZON_DOMAIN` | `amazon.de` | Amazon marketplace. |
| `COLUMN_SELLER_ID` | `Seller URL,Seller ID` | Comma-separated column names for seller ID. |
| `COLUMN_SELLER_NAME` | `Seller Name` | Column name for seller name. |

### gpt.py

| Variable | Example | Description |
|----------|---------|-------------|
| `INPUT_CSV` | `data/enriched.csv` | Input (enriched) CSV. |
| `OUTPUT_CSV` | `data/final.csv` | Output CSV with Variable 1 & 2. |
| `START_FROM_LINE` | `1` or empty | First record (1-based). |
| `MAX_RECORDS` | `100` or empty | Max records. |
| `BATCH_SIZE` | `5` | Records per GPT call. |
| `MODEL` | `gpt-5-mini` | OpenAI model. |
| `WAIT_BETWEEN_BATCHES` | `2` | Seconds between batch API calls. |
| `COLUMN_SELLER_ID` | `Seller URL,Seller ID` | Comma-separated. |
| `COLUMN_SELLER_NAME` | `Seller Name` | |
| `COLUMN_PRODUCT_NAME` | `Product Name` | |
| `COLUMN_PRODUCT_DESCRIPTION` | `Product Description` | |

### phone_clean.py

| Variable | Example | Description |
|----------|---------|-------------|
| `INPUT_CSV` | `data/final.csv` | Input CSV. |
| `OUTPUT_CSV` | `data/ready.csv` | Output CSV (can be same as input). |
| `COLUMN_PHONE` | `Company Phone` | Column with phone numbers. |
| `COLUMN_PHONE_OUTPUT` | `Company Phone` | Column to write cleaned numbers (same or new name). |

Paths in the container are under `/app/data`; the host directory `./data` is mounted there, so use `data/...` in these variables.

---

## 6. Headless / no browser

The scraper does **not** use a browser. It calls the **Web Scraping API** over HTTP (`requests.get`). So it is already headless and runs fine in Docker and on a server without a display. No extra “headless browser” setup is required.

---

## 7. Workflow summary on VPS

1. Put leads CSV in `data/` (e.g. `data/leads.csv`).
2. Set `.env` with `API_KEY`, `OPENAI_API_KEY`, and optionally `INPUT_CSV`, `OUTPUT_CSV`, etc.
3. **Step 1:** `docker compose run --rm app python main.py` → writes e.g. `data/enriched.csv`.
4. Set `INPUT_CSV=data/enriched.csv`, `OUTPUT_CSV=data/final.csv` (in `.env` or compose).
5. **Step 2:** `docker compose run --rm app python gpt.py` → writes `data/final.csv`.
6. Set `INPUT_CSV=data/final.csv`, `OUTPUT_CSV=data/ready.csv` (and phone column if needed).
7. **Step 3:** `docker compose run --rm app python phone_clean.py` → writes `data/ready.csv`.

All paths are configurable via environment variables; you don’t need to change any code.

---

## 8. Upload and download between laptop and VPS

Use **scp** or **rsync** over SSH (replace `user` with your VPS username and `VPS_IP` with the server IP or hostname).

### Upload (laptop → VPS)

**Single file** (e.g. a CSV into `data/`):
```bash
scp /path/on/laptop/file.csv user@VPS_IP:~/amazon-scraper/data/
```

**Whole project folder** (e.g. contents of `vps_upload`):
```bash
scp -r /path/to/vps_upload/* user@VPS_IP:~/amazon-scraper/
```

**Folder with rsync** (skips unchanged files; exclude venv and .env if you prefer):
```bash
rsync -avz --exclude 'venv' --exclude '.env' /path/to/vps_upload/ user@VPS_IP:~/amazon-scraper/
```

### Download (VPS → laptop)

**Single file** (e.g. output CSV or log):
```bash
scp user@VPS_IP:~/amazon-scraper/data/enriched.csv /path/on/laptop/
```

**Whole `data/` folder** (all CSVs):
```bash
scp -r user@VPS_IP:~/amazon-scraper/data /path/on/laptop/
```

**With rsync**:
```bash
rsync -avz user@VPS_IP:~/amazon-scraper/data/ /path/on/laptop/data/
```

### Using an SSH key

If you use a key (e.g. `ssh -i ~/.ssh/mykey user@VPS_IP`), add it to scp/rsync:

```bash
scp -i ~/.ssh/mykey -r ./vps_upload/* user@VPS_IP:~/amazon-scraper/
rsync -avz -e "ssh -i ~/.ssh/mykey" user@VPS_IP:~/amazon-scraper/data/ ./data/
```

---

## 9. Backup

A copy of the code before Docker-related changes is in **`backup_before_docker/`**. Use it if you need to revert or compare.
