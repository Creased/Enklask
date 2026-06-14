# Slim image — no browser needed. curl_cffi (in requirements.txt) impersonates a
# real browser's TLS/HTTP2 fingerprint, so Vinted, eBay and Leboncoin all crawl
# over plain HTTP past their bot checks. No Playwright/Chromium/Xvfb.
#
# (Facebook Marketplace is the only source that still needs a browser; it imports
# Playwright lazily and is off by default. To use it: install
# requirements-scrapers.txt and run `playwright install chromium`.)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite database lives here; mount a volume to persist it.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
