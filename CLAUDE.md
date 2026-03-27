# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Service

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn app:app --host 0.0.0.0 --port 8000

# Run with Docker (required for Air-e operator, which needs CA bundle setup)
docker build -t request-scan . && docker run -p 8000:8000 request-scan
```

There is no test suite or lint configuration in this project.

## Architecture Overview

This is a **FastAPI service** that scans and audits request IDs on Colombian utility company websites (Afinia and Air-e). All jobs run as background threads and deliver results asynchronously via webhook callbacks.

### Two source files

- **`app.py`** — FastAPI app, request models, background workers, HTTP session factory
- **`audit_client.py`** — Reusable functions for validation, encryption, form priming, and data fetching; imported by `app.py`

### Three API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /scan/range` | Scan a numeric ID range to find valid IDs |
| `POST /audit/batch` | Fetch audit trail for a list of known IDs |
| `POST /status/refresh` | Detect status changes for tracked IDs |

### Operator configuration

The service supports two operators (`"afinia"` | `"aire"`). `make_session_for_operator()` returns a `requests.Session` with operator-specific SSL configuration. Air-e requires a custom CA bundle (`AIRE_CA_BUNDLE` env var, built into the Docker image) and a custom `UnsafeAdapter` that enables legacy TLS renegotiation.

### Background job pattern

All three endpoints start a background thread and immediately return `202`. Workers emit structured webhook events to the caller-supplied `webhook_url`:
- `started` — job begun
- `item` / `found` — per-result updates
- `finished` — job complete with summary
- `error` — per-item failures

Webhook delivery uses `_post_json_with_retries()` with up to 3 attempts.

### Scan strategies (`/scan/range`)

- **checkpoint** (default): Jumps to candidate IDs whose last two digits are in `(10, 30, 50, 70, 90)`, then expands sequentially around each hit. Faster for sparse ranges.
- **linear**: Simple sequential scan across the entire range.

### ASP.NET response unwrapping

Both operators return JSON wrapped in `{"d": <payload>}`. The `_unwrap_d()` helper strips this before processing.

### Key constants / defaults

- HTTP timeout: 30 s
- Webhook timeout: 10 s
- Inter-request delay: 200 ms (scan), 25 ms (audit/status)
- Retry sleep: 0.4–0.5 s
