# Amazon Scraper + GPT pipeline (headless: uses HTTP APIs only, no browser)
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY config_env.py main.py gpt.py phone_clean.py ping_check.py ./

# Default: run scraper (override with: docker run ... python gpt.py or python phone_clean.py)
CMD ["python", "main.py"]
