# Invoice Ingestion & Reconciliation Pipeline

A resilient, beginner-friendly distributed pipeline built with Python, Django, Celery, and Redis to ingest, deduplicate, and reconcile invoice streams against a rate-limited mock ERP system.

---

## 1. Project Overview

In enterprise accounts payable (AP) systems, invoices arrive in bursty batches from multiple upstream sources (emails, ERPs, vendor portals). Before paying an invoice, it must be verified against an authoritative System of Record (ERP).

The challenge:
- The upstream ERP is slow (200–900 ms latency), hostile (~5% 503 errors, ~1% timeouts), and enforces a strict **global 10 requests/second** limit across all callers.
- Ingestion arrives in bursts (up to 4,000 invoices/minute).
- Duplicate invoices must never be processed twice (**idempotent execution**).

This project solves this by decoupling API ingestion via a **Redis queue**, distributing reconciliation jobs across **Celery workers**, enforcing a **Redis-backed global rate limiter**, caching repeated purchase order lookups, and persisting states in a transactional database.

---

## 2. Architecture

```text
                 INVOICE
                    │
                    ▼
              ┌──────────┐
              │   API    │  POST /api/invoices/
              └────┬─────┘
                   │
                   ▼
              ┌──────────┐
              │  REDIS   │  Task Queue / Message Broker
              │  QUEUE   │
              └────┬─────┘
                   │
                   ▼
              ┌──────────┐
              │  WORKER  │  Celery Worker Process
              └────┬─────┘
                   │
             ┌─────┴─────┐
             ▼           ▼
        VALIDATION     DEDUPE (DB Unique constraint & content_hash)
             │           │
             └─────┬─────┘
                   ▼
             ┌──────────┐
             │  CACHE   │  Redis Key-Value Store (TTL=300s)
             └────┬─────┘
                  │ (Cache Miss)
                  ▼
           ┌─────────────┐
           │ RATE LIMIT  │  Redis Global 10 req/s Counter
           └──────┬──────┘
                  │
                  ▼
             ┌─────────┐
             │ MOCK ERP│  System of Record (FastAPI, Port 9100)
             └────┬────┘
                  │
          ┌───────┼────────┐
          ▼       ▼        ▼
       MATCHED UNMATCHED ERROR (Retry transient 429/503/timeouts)
          │       │        │
          └───────┼────────┘
                  ▼
             ┌──────────┐
             │ DATABASE │  SQLite Relational Store
             └────┬─────┘
                  │
                  ▼
             REPORTING      GET /api/reporting/summary/ & /metrics
```

---

## 3. Technology Stack

- **Language**: Python 3.11+
- **Web Framework**: Django 5.x / 6.x + Django REST Framework
- **Task Queue & Broker**: Celery + Redis
- **Database**: SQLite
- **Containerization**: Docker Compose
- **Testing**: pytest, pytest-django, fakeredis

---

## 4. Quick Start with Docker (Recommended)

Start the entire system (API, Celery worker, Redis, and Mock ERP) with a single command:

```bash
docker compose up --build
```

Services will be available at:
- **Reconciliation API**: `http://localhost:8000`
- **Mock ERP**: `http://localhost:9100`
- **Redis Broker**: `localhost:6379`

To stop the system:
```bash
docker compose down
```

---

## 5. Local Setup without Docker

If you prefer to run locally on your host machine:

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run Database Migrations
```bash
python manage.py migrate
```

### 3. Start Mock ERP (Terminal 1)
```bash
uvicorn mock_erp:app --host 0.0.0.0 --port 9100
```

### 4. Start Redis
Make sure a local Redis server is running on `localhost:6379`.

### 5. Start Celery Worker (Terminal 2)
```bash
celery -A core worker -l info --concurrency=4
```

### 6. Start Django API Server (Terminal 3)
```bash
python manage.py runserver 0.0.0.0:8000
```

---

## 6. How to Run Tests

The test suite covers validation, idempotency, ERP retry/backoff, global rate limiting, and reporting. Tests use `fakeredis` and do not require external services to be running.

Run all tests:
```bash
pytest
```

Run specific test modules:
```bash
pytest tests/test_validation.py
pytest tests/test_idempotency.py
pytest tests/test_erp_client.py
pytest tests/test_rate_limiter.py
pytest tests/test_reporting.py
pytest tests/test_end_to_end.py
```

---

## 7. Generating & Submitting Invoices

### Generate Sample Invoices
Use the provided harness script to generate synthetic NDJSON invoices:
```bash
python generate_invoices.py --count 100 --seed 42 --out invoices.ndjson
```

### Submit Invoices via the Feeder Script
A feeder CLI script `submit_invoices.py` is included to stream invoices into the API:

```bash
# Generate 50 synthetic invoices and submit them automatically:
python submit_invoices.py --count 50

# Or pipe generated invoices directly:
python generate_invoices.py --count 100 | python submit_invoices.py
```

---

## 8. API Endpoints & Example Requests

### 1. Ingest Invoice
- **URL**: `POST /api/invoices/`
- **Description**: Accepts an invoice JSON, enqueues background processing, and returns immediately.
- **Example Request**:
  ```bash
  curl -X POST http://localhost:8000/api/invoices/ \
    -H "Content-Type: application/json" \
    -d '{
      "invoice_id": "INV-000000001",
      "invoice_number": "1003-48217",
      "vendor_id": "V-1003",
      "vendor_name": "Cornerstone Electric",
      "po_number": "PO-000001",
      "amount": 4210.55,
      "currency": "USD"
    }'
  ```
- **Response** (`202 Accepted`):
  ```json
  {
    "invoice_id": "INV-000000001",
    "status": "PENDING",
    "message": "Invoice accepted and queued for reconciliation",
    "is_duplicate": false
  }
  ```

### 2. View Invoice Status
- **URL**: `GET /api/invoices/<invoice_id>/`
- **Example Request**:
  ```bash
  curl http://localhost:8000/api/invoices/INV-000000001/
  ```

### 3. Reporting Summary
- **URL**: `GET /api/reporting/summary/`
- **Description**: Aggregates operational counts and match rate.
- **Example Request**:
  ```bash
  curl http://localhost:8000/api/reporting/summary/
  ```
- **Example Response**:
  ```json
  {
    "total_invoices": 100,
    "counts": {
      "pending": 5,
      "processing": 2,
      "matched": 68,
      "unmatched": 15,
      "invalid": 4,
      "failed": 2,
      "dead_lettered": 4,
      "duplicate": 2
    },
    "match_rate_percentage": 81.93,
    "stuck_backlog_count": 0,
    "timestamp": "2026-10-02T12:00:00Z"
  }
  ```

### 4. Health Check
- **URL**: `GET /healthz`
- **Response** (`200 OK`):
  ```json
  {
    "status": "healthy",
    "database": "ok",
    "redis": "ok",
    "timestamp": "2026-10-02T12:00:00Z"
  }
  ```

### 5. Prometheus Metrics
- **URL**: `GET /metrics`
- **Response**: Prometheus gauge metrics partitioned by invoice status.

---

## 9. Live 10-Minute Demonstration Flow

1. **Start System**: Run `docker compose up`.
2. **Verify Health**: Visit `http://localhost:8000/healthz` (returns healthy).
3. **Submit Clean Invoice**: Post a valid invoice. Observe in Celery logs: validation passed, ERP matched, DB status updated to `MATCHED`.
4. **Duplicate Prevention**: Re-submit the exact same invoice. Observe API returns `200 OK` with `is_duplicate: true`, preventing double-payment.
5. **Demonstrate Cache**: Submit a second invoice referencing the same PO. Observe worker log: `CACHE_HIT`, bypassing the slow ERP.
6. **Reporting Check**: Visit `http://localhost:8000/api/reporting/summary/` to inspect real-time metrics.

---

## 10. Known Limitations & Production Enhancements

- **SQLite Datastore**: Used for the self-contained 24-hour slice; would migrate to PostgreSQL in a high-concurrency production setting.
- **Fixed-Window Limiter**: Uses 1-second fixed windows in Redis; sufficient for 10 req/s, but a Token Bucket algorithm can smooth micro-bursts at the boundary of a second.
- **Single Redis Node**: For production, a Redis Sentinel or Redis Cluster configuration provides high availability.
"# Invoice-Ingestion-Reconciliation-Pipeline" 
