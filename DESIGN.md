# System Architecture & Design Document

## 1. High-Level Architecture

The Invoice Ingestion & Reconciliation Pipeline is designed to ingest a bursty stream of invoices, decouple API ingestion from external ERP processing, enforce strict idempotency, cache repetitive lookups, coordinate a shared global rate limit across all workers, and expose near-real-time operational metrics.

```text
                 INVOICE
                    │
                    ▼
              ┌──────────┐
              │   API    │ (POST /api/invoices/)
              └────┬─────┘
                   │ Enqueues task
                   ▼
              ┌──────────┐
              │  REDIS   │ (Task Queue / Message Broker)
              │  QUEUE   │
              └────┬─────┘
                   │ Consumes task
                   ▼
              ┌──────────┐
              │  WORKER  │ (Celery Worker)
              └────┬─────┘
                   │
             ┌─────┴─────┐
             ▼           ▼
        VALIDATION     DEDUPE (DB Unique constraint & content_hash)
             │           │
             └─────┬─────┘
                   ▼
             ┌──────────┐
             │  CACHE   │ (Redis Key-Value Cache)
             └────┬─────┘
                  │
             Cache Miss
                  │
                  ▼
           ┌─────────────┐
           │ RATE LIMIT  │ (Redis Shared 10 req/s Counter)
           └──────┬──────┘
                  │
                  ▼
             ┌─────────┐
             │ MOCK ERP│ (Hostile Upstream: 10 req/s, 200-900ms, 429/503/timeout)
             └────┬────┘
                  │
          ┌───────┼────────┐
          ▼       ▼        ▼
       MATCHED UNMATCHED ERROR (Retry transient 429/503/timeout with backoff)
          │       │        │
          └───────┼────────┘
                  ▼
             ┌──────────┐
             │ DATABASE │ (SQLite / relational store)
             └────┬─────┘
                  │
                  ▼
             REPORTING (GET /api/reporting/summary/ & /metrics)
```

---

## 2. End-to-End Invoice Flow

1. **Ingestion**: The client posts an invoice payload to `POST /api/invoices/`.
2. **Immediate Persistence & Enqueueing**:
   - The API checks if `invoice_id` already exists. If yes, it acknowledges the existing record (`is_duplicate=True`) and does not re-enqueue.
   - If new, it inserts the record with `status='PENDING'` and enqueues a Celery background task (`process_invoice_task.delay(invoice_id)`).
   - Returns HTTP `202 Accepted` immediately without blocking on the slow ERP.
3. **Worker Pickup**: A Celery worker process dequeues the task from Redis.
4. **Idempotency Guard**: The worker verifies the invoice is not already in a terminal state (`MATCHED`, `UNMATCHED`, `INVALID`, `DUPLICATE`, `DEAD_LETTERED`). If so, it halts immediately.
5. **Validation**: The worker verifies required fields (`vendor_id`, `po_number`, `amount > 0`, valid currency). If malformed, the invoice is marked `INVALID` and halted without calling ERP.
6. **Near-Duplicate Check**: Computes/checks `content_hash`. If another invoice with identical content was already reconciled, marks status as `DUPLICATE`.
7. **Cache Check**: The worker queries Redis for key `erp_cache:{po_number}:{vendor_id}:{amount}:{currency}`.
   - If found, status is updated directly to `MATCHED` or `UNMATCHED` from the cache. ERP call is bypassed!
8. **Global Rate Limiting**: On cache miss, the worker requests a slot from the Redis-backed global rate limiter (shared across all workers). If current 1-second slot is saturated (>10 req/s), it waits until the next second.
9. **ERP Call & Reconciliation**: Worker invokes `ERPClient.match_invoice(...)`.
   - If matched: stores result in Redis cache (TTL=300s) and marks status `MATCHED`.
   - If business mismatch (unknown PO, vendor mismatch, amount outside 1% tolerance): stores in cache and marks status `UNMATCHED`.
10. **Failure & Retry Handling**:
    - Transient errors (`429`, `503`, timeout): worker increments `retry_count` and reschedules with exponential backoff (`2^attempt` seconds).
    - If `retry_count >= MAX_RETRIES` (3): moves to `DEAD_LETTERED`.
    - Non-retryable errors (400 Bad Request): marks `FAILED`.

---

## 3. Why Redis?

Redis serves three distinct, critical roles in this architecture:
1. **Task Queue / Broker**: Buffers sudden ingestion bursts (up to 4,000 invoices/minute) without overwhelming backend workers or dropping incoming requests.
2. **Global Rate Limiting**: Provides centralized, atomic counters (`INCR`, `EXPIRE`) to enforce the ERP's 10 req/s rate limit across all distributed worker processes.
3. **Lookup Cache**: Caches ERP reconciliation outcomes for identical PO/vendor/amount parameters, significantly reducing calls to the slow ERP.

---

## 4. Why Celery?

- **Decoupled Asynchronous Processing**: The ERP takes 200–900 ms per call and is rate-limited to 10 req/s. If the ingestion API called the ERP synchronously, a burst of 4,000 invoices would exhaust HTTP server worker threads, leading to gateway timeouts and crashed clients.
- **Controlled Concurrency**: Celery allows us to tune worker concurrency to match downstream capacity.
- **Built-in Retries & Backoff**: Celery's `retry(countdown=...)` mechanism provides clean exponential backoff without custom thread-sleeping loops.

---

## 5. Idempotency & Delivery Guarantees

In distributed systems, networks drop connections and clients retry. Upstream sources provide **at-least-once delivery**, which means duplicate messages are guaranteed to occur.

Our pipeline turns **at-least-once delivery into exactly-once outcomes**:
1. **Database Unique Constraint**: `invoice_id` has a unique index in the relational database. If two identical requests arrive concurrently, the database guarantees only one insert succeeds; the second catches `IntegrityError` and returns `is_duplicate=True`.
2. **Worker Terminal Status Guard**: If a message is redelivered to a worker, the worker checks if the invoice status is already terminal (`MATCHED`, `UNMATCHED`, etc.). If so, it skips execution immediately.
3. **Content-Hash Deduplication**: Synthetic feeds and real AP systems frequently generate duplicate bills with new invoice IDs. We calculate:
   `content_hash = sha256(vendor_id|po_number|amount|currency|invoice_number)[:16]`
   If an invoice with the same hash is already reconciled, the new record is marked `DUPLICATE`.

---

## 6. Global Rate Limiting Across Workers

The mock ERP enforces a hard global limit: **10 requests per second across all callers**.

### The Problem with Local Rate Limiting
If each worker process maintained an independent 10 req/s limiter, having 4 workers would result in up to 40 req/s sent to the ERP, causing severe HTTP 429 rate limit errors and cascading retry storms.

### Our Redis-Based Solution
We use a Redis fixed-window counter partitioned by epoch second:
- Key: `erp:rate_limit:<current_unix_timestamp>`
- When a worker prepares to call the ERP, it executes an atomic pipeline:
  ```python
  pipe = redis.pipeline()
  pipe.incr(key)
  pipe.expire(key, 2)
  count, _ = pipe.execute()
  ```
- If `count <= 10`: Request is allowed immediately.
- If `count > 10`: The slot is full. The worker computes the fraction of a second remaining until the next epoch second, sleeps, and tries again.
- All worker processes share this counter, guaranteeing the global ceiling is respected.

---

## 7. Retry Strategy & Error Classification

Not all errors are created equal. Retrying permanent errors is wasteful and dangerous; failing to retry transient errors results in data loss.

| Error Type | Status Code / Cause | Action | Reason |
|---|---|---|---|
| Rate Limited | HTTP 429 | Retry with Backoff | Transient: ERP was busy. Honoring `Retry-After` header. |
| Upstream Unavailable | HTTP 503 | Retry with Backoff | Transient: ERP gateway or server hiccup. |
| Timeout | RequestTimeout / 504 | Retry with Backoff | Transient: ERP took >3s or dropped connection. |
| Bad Request | HTTP 400 | No Retry (FAILED) | Permanent: Malformed request parameters. |
| PO Not Found | HTTP 200 `matched: False` | No Retry (UNMATCHED) | Permanent Business Result: Valid inquiry, PO does not exist. |
| Vendor Mismatch | HTTP 200 `matched: False` | No Retry (UNMATCHED) | Permanent Business Result: Vendor does not match PO. |
| Amount Out of Tolerance | HTTP 200 `matched: False` | No Retry (UNMATCHED) | Permanent Business Result: Amount differs by >1%. |

### Exponential Backoff Schedule
- Attempt 1: 2 seconds
- Attempt 2: 4 seconds
- Attempt 3: 8 seconds
- Exhausted (Attempt > 3): Moved to `DEAD_LETTERED` with detailed failure reason saved for operator inspection.

---

## 8. Caching Strategy & Invalidation

### Why Cache?
In invoice datasets, purchase orders are referenced repeatedly (~1 PO per ~3 invoices). Because ERP calls take 200–900 ms and are capped at 10 req/s, every cache hit avoids consuming a rate-limited ERP slot and provides sub-millisecond reconciliation.

### Cache Key Safety
The cache key is composed of all four reconciliation parameters:
`erp_cache:{po_number}:{vendor_id}:{amount:.2f}:{currency}`

**Why this matters**: If the cache key omitted `amount` or `vendor_id`, an invoice with an incorrect amount or vendor would hit the cache and falsely receive a `matched=True` status, leading to improper payment approval.

### TTL Assumption
We use a **300-second (5 minute) TTL**.
In business accounts payable, purchase order statuses and approved amounts do not fluctuate millisecond-by-millisecond. A 5-minute TTL provides high hit ratios during batch dumps while guaranteeing that changes in the ERP are picked up within 5 minutes.

---

## 9. Failure Scenarios & Resilience

1. **ERP Unavailable for 1 Hour**:
   - Workers catch HTTP 503 / timeouts and retry up to `MAX_RETRIES` (3).
   - Invoices that fail 3 times are safely marked `DEAD_LETTERED` with the exact error message preserved.
   - No data is lost; operators can query dead-lettered invoices and trigger reprocessing once ERP recovers.
2. **Worker Process Dies Mid-Task**:
   - Celery tasks are acknowledged after successful processing (`CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = True` or default broker acks). If a worker process abruptly dies, unacknowledged messages are requeued by Redis.
   - Because our worker logic is strictly idempotent, re-processing a restarted task produces the same outcome without duplication.
3. **Redis Unavailable**:
   - The Ingestion API will return HTTP 503, signaling upstream producers to back off and retry.
   - The health check endpoint `/healthz` will return HTTP 503 (`status: unhealthy, redis: error`), alerting operators immediately.
4. **Poison Messages (Malformed Invoices)**:
   - Malformed records (missing amount, negative values, blank vendor) are caught by our serializer / domain validation in step 1 of processing.
   - They are marked `INVALID` immediately, stored in DB for auditing, and never sent to ERP or retried.

---

## 10. Scaling to Production (12M+ Invoices)

### The Production Target
- **12 Million** historical invoices in the database.
- Bursts of **4,000 invoices/minute** (~67 invoices/sec).
- ERP hard bottleneck: **10 req/s** steady state.

### The Math: Bottleneck & Backlog
- Inflow during burst: **66.6 invoices/sec**.
- ERP throughput ceiling: **10 invoices/sec** (without caching).
- Deficit: ~56.6 invoices/sec accumulate in the queue during the burst.
- For a 10-minute burst (40,000 invoices):
  - Invoices processed during burst: ~6,000 (at 10/s) + cache hits (~15,000).
  - Residual queue depth: ~19,000 invoices.
  - Time to drain residual backlog at 10 req/s: **~31 minutes**.
- **Where the backlog lives**: In Redis queue memory. 40,000 lightweight task IDs consume under 10 MB of RAM in Redis, which is well within capacity.

### Scaling Architecture for 12M Rows
1. **Database Migration to PostgreSQL**: SQLite is used for the slice. In production, PostgreSQL with B-Tree indexes on `invoice_id`, `(status, created_at)`, and `content_hash` easily handles 12M rows.
2. **Reporting Optimization**: Rather than running `COUNT(*)` over 12M rows, use:
   - Redis Hash counters (`HINCRBY reporting:counts matched 1`) incremented on status transitions.
   - Read replicas for analytical aggregations.
3. **Worker Sizing**: Because each ERP call takes ~500ms, each worker thread handles ~2 requests/second. To saturate the 10 req/s ERP ceiling, 5–10 concurrent worker threads are sufficient. Adding hundreds of workers is unnecessary because the downstream ERP is the fixed bottleneck.

---

## 11. Explicit Tradeoffs & What Was Not Built

Because this implementation was built within a tight 24-hour window, conscious senior engineering tradeoffs were made:

| Decision | What We Built | What We Deliberately Deferred | Rationale |
|---|---|---|---|
| **Datastore** | SQLite with 20s timeout and WAL mode | PostgreSQL / Citus cluster | SQLite requires zero infrastructure setup for evaluation, supports transactional unique constraints, and easily handles thousands of test invoices. |
| **Rate Limiter** | Redis fixed-window counter (`INCR` + `EXPIRE`) | Leaky Bucket / Token Bucket Lua script | Fixed-window per second is simple, self-healing, easy to explain in an interview, and adheres strictly to the ERP's 1-second window. |
| **Cache Invalidation**| Time-to-Live (TTL = 300s) | Event-driven CDC / webhook cache invalidation | ERP does not provide webhook notifications on PO changes. Time-based TTL is robust, self-evicting, and prevents memory leaks. |
| **Frontend** | Clean REST API + `/metrics` endpoint | React / Vue dashboard | The assignment explicitly asked for no frontend; operational metrics are accessible via `/api/reporting/summary/` and `/metrics`. |
