# --------- 1) Base image ---------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps (just enough to build wheels cleanly)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --------- 2) Install deps with caching ---------
# Copy only requirements first to leverage Docker layer cache
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# --------- 3) App code ---------
# Copy the rest of the project (assumes app.py is at repo root)
COPY . .

# --------- 4) Security: run as non-root ---------
# Create a non-root user and give it ownership of /app
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# --------- 5) Runtime config ---------
EXPOSE 8000

# Healthcheck hits the lightweight /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# --------- 6) Entrypoint ---------
# Use Uvicorn with workers tuned for I/O bound tasks.
# If you named your module differently, adjust "app:app".
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2", "--log-level", "info"]
