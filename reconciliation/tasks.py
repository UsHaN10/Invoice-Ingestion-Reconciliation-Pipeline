import logging
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from .models import Invoice
from .serializers import VALID_CURRENCIES
from .cache import ERPCache
from .rate_limiter import GlobalRateLimiter
from .erp_client import (
    ERPClient,
    TransientERPError,
    ERPRateLimitError,
    PermanentERPError,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def process_invoice_task(self, invoice_id: str):
    """
    Asynchronous Celery task to process and reconcile an invoice.
    
    Pipeline Steps:
    1. Idempotency Check: Don't re-process terminal invoices.
    2. Mark status -> PROCESSING.
    3. Validate data integrity (reject invalid records upfront).
    4. Deduplicate near-duplicates via content_hash.
    5. Cache lookup (avoid redundant ERP calls for identical POs).
    6. Global Rate Limit enforcement (respect 10 req/s globally across all workers).
    7. Call ERP system of record.
    8. Store outcome in DB and update cache.
    9. On transient failures (429, 503, timeout), retry with exponential backoff.
    10. If max retries exceeded, route to DEAD_LETTERED.
    """
    logger.info(f"[invoice_id={invoice_id}] [event=START_PROCESSING] [attempt={self.request.retries + 1}]")

    try:
        invoice = Invoice.objects.get(invoice_id=invoice_id)
    except Invoice.DoesNotExist:
        logger.error(f"[invoice_id={invoice_id}] [event=NOT_FOUND] Invoice record not found in DB.")
        return

    # 1. Idempotency guard: if already reached a terminal state, DO NOT duplicate processing
    terminal_statuses = {
        Invoice.STATUS_MATCHED,
        Invoice.STATUS_UNMATCHED,
        Invoice.STATUS_INVALID,
        Invoice.STATUS_DUPLICATE,
        Invoice.STATUS_DEAD_LETTERED,
    }
    if invoice.status in terminal_statuses:
        logger.info(
            f"[invoice_id={invoice_id}] [event=ALREADY_TERMINAL] "
            f"Invoice already in terminal status '{invoice.status}'. Preserving exactly-once outcome."
        )
        return

    # 2. Update status to PROCESSING
    invoice.status = Invoice.STATUS_PROCESSING
    invoice.save(update_fields=['status', 'updated_at'])

    # 3. Domain Validation (reject invalid records without wasting ERP calls)
    validation_error = None
    if not invoice.vendor_id or not invoice.vendor_id.strip():
        validation_error = "Missing or blank vendor_id"
    elif not invoice.po_number or not invoice.po_number.strip():
        validation_error = "Missing or blank po_number"
    elif invoice.amount is None:
        validation_error = "Missing amount"
    elif invoice.amount <= 0:
        validation_error = f"Non-positive amount: {invoice.amount}"
    elif invoice.currency.strip().upper() not in VALID_CURRENCIES:
        validation_error = f"Unsupported or invalid currency: {invoice.currency}"

    if validation_error:
        logger.warning(f"[invoice_id={invoice_id}] [event=VALIDATION_FAILED] reason='{validation_error}'")
        invoice.mark_processed(Invoice.STATUS_INVALID, failure_reason=validation_error)
        return

    # 4. Content-based Deduplication (Near-duplicate check)
    if invoice.content_hash:
        existing_match = Invoice.objects.filter(
            content_hash=invoice.content_hash,
            status__in=[Invoice.STATUS_MATCHED, Invoice.STATUS_UNMATCHED]
        ).exclude(id=invoice.id).first()

        if existing_match:
            reason = f"Duplicate content of earlier invoice {existing_match.invoice_id}"
            logger.info(f"[invoice_id={invoice_id}] [event=CONTENT_DUPLICATE] {reason}")
            invoice.mark_processed(Invoice.STATUS_DUPLICATE, failure_reason=reason)
            return

    # 5. Redis Cache Check
    cache = ERPCache()
    cached_result = cache.get_match(
        po_number=invoice.po_number,
        vendor_id=invoice.vendor_id,
        amount=invoice.amount,
        currency=invoice.currency
    )

    if cached_result is not None:
        matched = cached_result.get("matched", False)
        reason = cached_result.get("reason", "")
        new_status = Invoice.STATUS_MATCHED if matched else Invoice.STATUS_UNMATCHED
        logger.info(f"[invoice_id={invoice_id}] [event=CACHE_HIT] status={new_status}")
        invoice.mark_processed(new_status, failure_reason=reason, erp_response=cached_result)
        return

    # 6. Global Rate Limiter: wait for a free slot across all workers
    rate_limiter = GlobalRateLimiter()
    acquired = rate_limiter.acquire(timeout=10.0)
    if not acquired:
        logger.warning(f"[invoice_id={invoice_id}] [event=RATE_LIMIT_WAIT_TIMEOUT] Retrying task later.")
        raise self.retry(countdown=2)

    # 7. Call ERP System of Record
    client = ERPClient()
    try:
        erp_result = client.match_invoice(
            po_number=invoice.po_number,
            vendor_id=invoice.vendor_id,
            amount=invoice.amount,
            currency=invoice.currency
        )

        # Store verified result in Redis cache for subsequent identical invoices
        cache.set_match(
            po_number=invoice.po_number,
            vendor_id=invoice.vendor_id,
            amount=invoice.amount,
            currency=invoice.currency,
            result=erp_result
        )

        # 8. Record outcome
        matched = erp_result.get("matched", False)
        if matched:
            logger.info(f"[invoice_id={invoice_id}] [event=MATCHED] Successfully reconciled with ERP.")
            invoice.mark_processed(Invoice.STATUS_MATCHED, erp_response=erp_result)
        else:
            reason = erp_result.get("reason", "unknown_mismatch")
            logger.info(f"[invoice_id={invoice_id}] [event=UNMATCHED] reason='{reason}'")
            invoice.mark_processed(Invoice.STATUS_UNMATCHED, failure_reason=reason, erp_response=erp_result)

    except TransientERPError as exc:
        invoice.retry_count += 1
        invoice.save(update_fields=['retry_count', 'updated_at'])

        if invoice.retry_count >= settings.MAX_RETRIES:
            fail_msg = f"Max retries ({settings.MAX_RETRIES}) reached. Last error: {str(exc)}"
            logger.error(f"[invoice_id={invoice_id}] [event=DEAD_LETTERED] {fail_msg}")
            invoice.mark_processed(Invoice.STATUS_DEAD_LETTERED, failure_reason=fail_msg)
            return

        # Exponential backoff: 2s, 4s, 8s...
        backoff_countdown = 2 ** invoice.retry_count
        if isinstance(exc, ERPRateLimitError) and exc.retry_after:
            backoff_countdown = max(backoff_countdown, exc.retry_after)

        logger.warning(
            f"[invoice_id={invoice_id}] [event=RETRYING] "
            f"Transient error: {exc}. Retrying in {backoff_countdown}s (retry {invoice.retry_count}/{settings.MAX_RETRIES})"
        )
        raise self.retry(exc=exc, countdown=backoff_countdown)

    except PermanentERPError as exc:
        logger.error(f"[invoice_id={invoice_id}] [event=PERMANENT_ERROR] {exc}")
        invoice.mark_processed(Invoice.STATUS_FAILED, failure_reason=str(exc))

    except Exception as exc:
        logger.exception(f"[invoice_id={invoice_id}] [event=UNEXPECTED_ERROR] {exc}")
        invoice.mark_processed(Invoice.STATUS_FAILED, failure_reason=f"Unexpected error: {str(exc)}")
