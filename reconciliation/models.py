import hashlib
from django.db import models
from django.utils import timezone


class Invoice(models.Model):
    """
    Main invoice record representing incoming invoices and their reconciliation state.
    
    Status Lifecycle:
      PENDING -> Enqueued, awaiting worker processing
      PROCESSING -> Currently being processed by a worker
      MATCHED -> Successfully verified against ERP system of record
      UNMATCHED -> Looked up in ERP, but PO not found, vendor mismatch, or amount outside tolerance
      INVALID -> Rejected upfront (malformed: missing fields, negative amount, bad currency)
      DUPLICATE -> Rejected because exact content/id already processed
      FAILED -> Processing encountered a non-retryable error
      DEAD_LETTERED -> Transient errors exceeded maximum retries
    """
    
    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_MATCHED = 'MATCHED'
    STATUS_UNMATCHED = 'UNMATCHED'
    STATUS_INVALID = 'INVALID'
    STATUS_DUPLICATE = 'DUPLICATE'
    STATUS_FAILED = 'FAILED'
    STATUS_DEAD_LETTERED = 'DEAD_LETTERED'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_MATCHED, 'Matched'),
        (STATUS_UNMATCHED, 'Unmatched'),
        (STATUS_INVALID, 'Invalid'),
        (STATUS_DUPLICATE, 'Duplicate'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_DEAD_LETTERED, 'Dead Lettered'),
    ]

    # Core Identifiers & Data
    invoice_id = models.CharField(max_length=64, unique=True, db_index=True)
    invoice_number = models.CharField(max_length=64, blank=True, default='')
    vendor_id = models.CharField(max_length=64, blank=True, default='')
    vendor_name = models.CharField(max_length=255, blank=True, default='')
    po_number = models.CharField(max_length=64, blank=True, default='', db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, blank=True, default='USD')
    cost_center = models.CharField(max_length=64, blank=True, default='')
    source = models.CharField(max_length=64, blank=True, default='')
    
    # Deduplication hash: sha256(vendor_id|po_number|amount|currency|invoice_number)[:16]
    content_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)

    # State tracking
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True
    )
    failure_reason = models.TextField(blank=True, default='')
    retry_count = models.IntegerField(default=0)

    # Auditing & payloads
    raw_payload = models.JSONField(default=dict, blank=True)
    erp_response = models.JSONField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['content_hash']),
        ]

    def __str__(self):
        return f"{self.invoice_id} ({self.status}) - {self.vendor_id} / {self.po_number}: {self.amount} {self.currency}"

    @staticmethod
    def calculate_content_hash(vendor_id: str, po_number: str, amount: float, currency: str, invoice_number: str) -> str:
        """
        Compute deterministic 16-character content hash to identify near-duplicates.
        Matches the hash function in generate_invoices.py.
        """
        basis = f"{vendor_id}|{po_number}|{amount}|{currency}|{invoice_number}"
        return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16]

    def mark_processed(self, status: str, failure_reason: str = '', erp_response: dict = None):
        """Helper to mark invoice terminal status and set timestamp."""
        self.status = status
        self.failure_reason = failure_reason
        self.erp_response = erp_response
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'failure_reason', 'erp_response', 'processed_at', 'updated_at'])
