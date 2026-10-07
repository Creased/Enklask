# curl_cffi handles API-style bot checks. Vinted additionally requires a real
# JavaScript browser session, so Camoufox runs headed inside its virtual Xvfb
# display and keeps its profile in the mounted data volume.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 \
    libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxi6 libxrandr2 \
    libxrender1 libxss1 libxtst6 libegl1 libgl1-mesa-dri libgbm1 \
    fonts-liberation fonts-noto-color-emoji fontconfig ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python -m camoufox fetch

COPY app ./app

# SQLite database lives here; mount a volume to persist it.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# Hits the app's /healthz endpoint. The slim image has no curl, so use Python's
# urllib (urlopen raises on a non-2xx response → non-zero exit).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
