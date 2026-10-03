import pytest
from unittest.mock import patch, MagicMock
from rest_framework.test import APIClient
from reconciliation.models import Invoice
from reconciliation.tasks import process_invoice_task
import fakeredis


@pytest.mark.django_db
def test_full_end_to_end_invoice_pipeline():
    """
    Test complete lifecycle:
    Ingestion -> Database Pending -> Worker Processing -> ERP Match -> Database Matched -> Cache Re-use -> Reporting.
    """
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    client = APIClient()

    invoice_payload = {
        "invoice_id": "INV-E2E-001",
        "invoice_number": "1003-99999",
        "vendor_id": "V-1003",
        "vendor_name": "Cornerstone Electric",
        "po_number": "PO-000123",
        "amount": 4210.55,
        "currency": "USD",
        "cost_center": "CC-014",
        "source": "property-erp"
    }

    # Step 1: Ingest via API
    with patch('reconciliation.views.process_invoice_task.delay') as mock_delay:
        resp = client.post('/api/invoices/', data=invoice_payload, format='json')
        assert resp.status_code == 202
        assert resp.data['status'] == Invoice.STATUS_PENDING
        mock_delay.assert_called_once_with("INV-E2E-001")

    # Step 2: Worker processes the invoice
    mock_erp_result = {
        "matched": True,
        "po_number": "PO-000123",
        "vendor_id": "V-1003",
        "po_amount": 4210.55,
        "currency": "USD",
        "status": "OPEN",
    }

    with patch('reconciliation.tasks.ERPCache.client', new_callable=lambda: fake_redis), \
         patch('reconciliation.tasks.GlobalRateLimiter.client', new_callable=lambda: fake_redis), \
         patch('reconciliation.tasks.ERPClient.match_invoice', return_value=mock_erp_result) as mock_erp_call:

        # Run task
        process_invoice_task("INV-E2E-001")
        mock_erp_call.assert_called_once()

        # Step 3: Check DB status
        inv = Invoice.objects.get(invoice_id="INV-E2E-001")
        assert inv.status == Invoice.STATUS_MATCHED
        assert inv.erp_response == mock_erp_result

        # Step 4: A second invoice with identical PO details should HIT the cache and NOT call ERP again!
        second_invoice_payload = {
            "invoice_id": "INV-E2E-002",
            "invoice_number": "1003-88888",
            "vendor_id": "V-1003",
            "po_number": "PO-000123",
            "amount": 4210.55,
            "currency": "USD"
        }
        with patch('reconciliation.views.process_invoice_task.delay'):
            client.post('/api/invoices/', data=second_invoice_payload, format='json')

        mock_erp_call.reset_mock()
        process_invoice_task("INV-E2E-002")

        # ERP was NOT called because cache supplied the answer!
        mock_erp_call.assert_not_called()
        inv2 = Invoice.objects.get(invoice_id="INV-E2E-002")
        assert inv2.status == Invoice.STATUS_MATCHED

    # Step 5: Check reporting endpoint reflects the 2 matched invoices
    rep_resp = client.get('/api/reporting/summary/')
    assert rep_resp.status_code == 200
    assert rep_resp.data['counts']['matched'] == 2
    assert rep_resp.data['match_rate_percentage'] == 100.0
