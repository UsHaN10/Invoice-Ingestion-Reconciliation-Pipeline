import pytest
from unittest.mock import patch
from django.db import IntegrityError
from rest_framework.test import APIClient
from reconciliation.models import Invoice
from reconciliation.tasks import process_invoice_task
import fakeredis


@pytest.mark.django_db
def test_ingestion_api_exact_duplicate_deduplication():
    """
    Ensure submitting the same invoice_id twice to the Ingestion API
    returns is_duplicate=True on the second attempt and does not create a second row.
    """
    client = APIClient()
    payload = {
        "invoice_id": "INV-DUP-001",
        "vendor_id": "V-1001",
        "po_number": "PO-000001",
        "amount": 2500.0,
        "currency": "USD",
        "invoice_number": "INV-NUM-1"
    }

    with patch('reconciliation.views.process_invoice_task.delay') as mock_task:
        # 1. First submission
        res1 = client.post('/api/invoices/', data=payload, format='json')
        assert res1.status_code == 202
        assert res1.data['is_duplicate'] is False
        assert mock_task.call_count == 1

        # 2. Second submission with identical invoice_id
        res2 = client.post('/api/invoices/', data=payload, format='json')
        assert res2.status_code == 200
        assert res2.data['is_duplicate'] is True
        # Background task must not be enqueued a second time
        assert mock_task.call_count == 1

    # Verify only one database record exists
    assert Invoice.objects.filter(invoice_id="INV-DUP-001").count() == 1


@pytest.mark.django_db
def test_database_unique_constraint_enforces_idempotency():
    """
    Test that the database-level unique constraint rejects duplicate invoice_id insertions.
    """
    Invoice.objects.create(
        invoice_id="INV-UNIQUE-01",
        vendor_id="V-1001",
        po_number="PO-000001",
        amount=100.0,
        currency="USD"
    )

    with pytest.raises(IntegrityError):
        Invoice.objects.create(
            invoice_id="INV-UNIQUE-01",
            vendor_id="V-1001",
            po_number="PO-000001",
            amount=100.0,
            currency="USD"
        )


@pytest.mark.django_db
def test_worker_content_hash_near_duplicate_detection():
    """
    Verify that an invoice with a new invoice_id but identical content_hash
    is flagged as DUPLICATE by the worker rather than re-reconciled.
    """
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    content_hash = "9f2c1a7b3e8d0f44"

    # Pre-existing matched invoice with this content hash
    Invoice.objects.create(
        invoice_id="INV-ORIGINAL-1",
        vendor_id="V-1001",
        po_number="PO-000001",
        amount=500.0,
        currency="USD",
        content_hash=content_hash,
        status=Invoice.STATUS_MATCHED
    )

    # Near duplicate invoice: new ID, but same content hash
    near_dup = Invoice.objects.create(
        invoice_id="INV-NEAR-DUP-2",
        vendor_id="V-1001",
        po_number="PO-000001",
        amount=500.0,
        currency="USD",
        content_hash=content_hash,
        status=Invoice.STATUS_PENDING
    )

    with patch('reconciliation.tasks.ERPCache.client', new_callable=lambda: fake_redis), \
         patch('reconciliation.tasks.GlobalRateLimiter.client', new_callable=lambda: fake_redis), \
         patch('reconciliation.tasks.ERPClient') as mock_erp:

        process_invoice_task("INV-NEAR-DUP-2")

        # ERP should NOT be called for near duplicates
        mock_erp.assert_not_called()

        near_dup.refresh_from_db()
        assert near_dup.status == Invoice.STATUS_DUPLICATE
        assert "INV-ORIGINAL-1" in near_dup.failure_reason


@pytest.mark.django_db
def test_worker_does_not_reprocess_terminal_invoice():
    """
    Ensure the worker respects idempotency: if an invoice is already in a terminal
    state (e.g. MATCHED), re-invoking the task is a safe no-op.
    """
    invoice = Invoice.objects.create(
        invoice_id="INV-TERMINAL-01",
        vendor_id="V-1001",
        po_number="PO-000001",
        amount=100.0,
        currency="USD",
        status=Invoice.STATUS_MATCHED
    )

    with patch('reconciliation.tasks.ERPClient') as mock_erp:
        process_invoice_task("INV-TERMINAL-01")
        mock_erp.assert_not_called()

        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_MATCHED
