# Technical Interview Guide: 24 Core Questions & Answers

This guide contains concise, clear, and technically precise answers to the 24 questions most likely to be asked about this invoice reconciliation pipeline.

---

### 1. Explain your architecture.
"We use an asynchronous producer-consumer architecture. Incoming invoices hit a lightweight Django REST API, which immediately records the invoice as `PENDING` in the database, pushes a job ID onto a Redis queue, and returns `202 Accepted`. Celery worker processes pull jobs from Redis, validate inputs, deduplicate duplicates, check a Redis cache, acquire a global 10 req/s rate-limit slot, reconcile against the ERP, and write the final outcome (`MATCHED` or `UNMATCHED`) to SQLite."

### 2. Why did you use Redis?
"Redis plays three critical roles:
1. **Task Queue/Broker**: Buffers incoming burst traffic so workers can consume at a controlled pace.
2. **Global Rate Limiter**: Stores shared epoch-second counters to enforce the ERP's 10 req/s limit across all workers.
3. **Lookup Cache**: Caches ERP reconciliation results for identical PO lookups with a 5-minute TTL."

### 3. Why did you use Celery?
"Celery provides robust background task execution, worker concurrency management, built-in exponential backoff retries, and task serialization out of the box, allowing us to decouple slow ERP calls from the user-facing API."

### 4. Why is the API asynchronous?
"The ERP takes 200–900 ms per call and is capped at 10 requests per second. If the API called the ERP synchronously, a burst of 4,000 invoices/minute would exhaust all HTTP worker threads immediately, causing gateway timeouts and server crashes. The async API returns `202 Accepted` in under 5 ms."

### 5. How does deduplication work?
"We deduplicate at two levels:
1. **Exact duplicates (`invoice_id`)**: A unique database constraint on `invoice_id`. If submitted again, the API catches the constraint violation and returns `is_duplicate: True`.
2. **Near duplicates (`content_hash`)**: We compute a SHA-256 hash of `vendor_id|po_number|amount|currency|invoice_number`. If a new invoice has a different `invoice_id` but matches an existing processed `content_hash`, the worker marks it as `DUPLICATE` without calling the ERP."

### 6. What is idempotency?
"Idempotency means that performing the same operation multiple times produces the exact same final state as performing it once. In our system: **At-least-once delivery + idempotent worker processing = exactly-once outcome**."

### 7. How do you handle concurrent duplicate invoices?
"We rely on the ACID relational database unique constraint on `invoice_id`. If two requests for the same invoice arrive at the exact same millisecond across two different API threads, the database enforces row uniqueness; one succeeds, and the other throws an `IntegrityError` which we catch and handle gracefully."

### 8. How do you respect the ERP's 10 req/s limit?
"We use a shared Redis fixed-window counter. Before making an ERP call, a worker atomically increments `erp:rate_limit:<current_unix_second>`. If the counter is $\le 10$, the call proceeds. If the counter exceeds 10, the worker sleeps until the next second begins."

### 9. Why is the rate limiter global?
"Because the ERP enforces a hard limit of 10 requests per second **across all callers combined**. If rate limiting were local to each worker, running 5 workers would send up to 50 requests/second, causing severe HTTP 429 rate limit errors."

### 10. What happens when ERP returns 429?
"The ERP client raises an `ERPRateLimitError`, reads the `Retry-After` header, and the Celery task schedules a retry with exponential backoff honoring that delay."

### 11. What happens when ERP returns 503?
"HTTP 503 is classified as a transient error (`ERPServiceUnavailableError`). The worker increments `retry_count` and retries with exponential backoff (2s, 4s, 8s)."

### 12. What happens on timeout?
"The ERP client uses a 3.0-second timeout. When the mock ERP injects a 6-second hang, the client raises `ERPTimeoutError`, catches it as a transient fault, and retries with backoff."

### 13. Which errors are retried?
"Only **transient system faults**:
- HTTP 429 (Rate limited)
- HTTP 503 (Upstream temporarily unavailable)
- Network timeouts / HTTP 504."

### 14. Which errors are not retried?
"**Permanent business outcomes and bad requests**:
- PO not found (`matched: False`)
- Vendor mismatch (`matched: False`)
- Amount outside 1% tolerance (`matched: False`)
- HTTP 400 Bad Request
- Malformed inputs (negative amount, blank vendor)
These are recorded immediately and never retried."

### 15. Why do you use caching?
"In typical invoice datasets, purchase orders are reused across multiple bills (~1 PO per 3 invoices). Caching the ERP match outcome with a 5-minute TTL allows repeat lookups to resolve in sub-millisecond time without consuming scarce ERP rate-limit slots."

### 16. What happens if the worker crashes?
"Celery uses task acknowledgments (`acks_late`). If a worker crashes mid-task, Redis requeues the unacknowledged job, and another worker picks it up. Because our processing logic is idempotent, reprocessing the task causes no harm or duplicate payment."

### 17. What happens if Redis goes down?
"If Redis goes down:
1. The Ingestion API cannot enqueue tasks and returns HTTP 503.
2. The `/healthz` endpoint immediately reports `status: unhealthy, redis: error`.
3. Workers pause until Redis reconnects. Existing data in SQLite remains intact."

### 18. How does the system handle 4,000 invoices/minute?
"The API accepts invoices and places them into the Redis queue in milliseconds. The Redis queue acts as a shock absorber. The workers then drain the queue at a steady, controlled rate of 10 req/s (plus cache hits), preventing system collapse."

### 19. What is the ERP bottleneck?
"The ERP bottleneck is its hard 10 req/s throughput limit and 200–900 ms response latency. No amount of worker scaling can increase throughput beyond 10 req/s without caching."

### 20. How would you scale this to 12M+ invoices?
"1. Migrate from SQLite to PostgreSQL with partitioned tables and B-Tree indexes.
2. Maintain pre-computed counters in Redis (`HINCRBY`) for instantaneous reporting aggregates.
3. Deploy 5–10 Celery worker threads to fully saturate the 10 req/s ERP pipe.
4. Scale Celery workers and Redis using read-replicas and connection pooling."

### 21. Why did you choose SQLite?
"SQLite requires zero external server setup, runs transactionally with file-based locking, and fully supports the relational constraints needed for this 24-hour evaluation slice."

### 22. What would you improve in production?
"1. Switch to PostgreSQL for high write concurrency.
2. Upgrade the rate limiter from fixed-window to a Token Bucket algorithm in Redis using Lua scripts.
3. Add a Dead Letter Queue management UI so AP operators can review and replay stuck invoices."

### 23. What did you intentionally NOT build?
"We deliberately avoided:
- Kafka / RabbitMQ (Redis is simpler and already present).
- Microservices / Kubernetes (unnecessary operational overhead for this scope).
- Frontend UI (plain REST API and Prometheus endpoints provide full observability).
- Complex event sourcing or CQRS (relational status transitions are much cleaner)."

### 24. What are the main tradeoffs?
"1. **Simplicity vs Microsecond Precision**: A Redis fixed-window rate limiter is much simpler than a sliding log or token bucket, but allows small boundary bursts.
2. **SQLite vs Postgres**: SQLite has table-level write locks under high concurrent load, but was chosen for portability and zero-friction setup.
3. **TTL Caching vs Real-Time Webhooks**: We chose a 5-minute TTL because the ERP provides no change webhooks, accepting up to 5 minutes of eventual consistency on PO updates."
