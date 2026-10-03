import pytest
from unittest.mock import patch, MagicMock
import requests
from reconciliation.erp_client import (
    ERPClient,
    ERPRateLimitError,
    ERPServiceUnavailableError,
    ERPTimeoutError,
    PermanentERPError,
)
from reconciliation.models import Invoice
from reconciliation.tasks import process_invoice_task
import fakeredis


def test_erp_client_matched_success():
    """Test successful 200 match response from ERP."""
    client = ERPClient(base_url="http://test-erp:9100")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "matched": True,
        "po_number": "PO-000123",
        "vendor_id": "V-1003",
        "po_amount": 4210.55,
        "currency": "USD",
        "status": "OPEN",
    }

    with patch('requests.get', return_value=mock_resp):
        res = client.match_invoice("PO-000123", "V-1003", 4210.55, "USD")
        assert res["matched"] is True
        assert res["po_number"] == "PO-000123"


def test_erp_client_unmatched_reasons():
    """Test unmatched 200 responses with business reasons (po_not_found, vendor_mismatch, amount)."""
    client = ERPClient(base_url="http://test-erp:9100")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "matched": False,
        "reason": "po_not_found",
        "po_number": "PO-999999"
    }

    with patch('requests.get', return_value=mock_resp):
        res = client.match_invoice("PO-999999", "V-1003", 100.0, "USD")
        assert res["matched"] is False
        assert res["reason"] == "po_not_found"


def test_erp_client_handles_429_rate_limit():
    """Test ERP returning 429 raises ERPRateLimitError."""
    client = ERPClient(base_url="http://test-erp:9100")
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "2"}

    with patch('requests.get', return_value=mock_resp):
        with pytest.raises(ERPRateLimitError) as exc_info:
            client.match_invoice("PO-000123", "V-1003", 100.0)
        assert exc_info.value.retry_after == 2


def test_erp_client_handles_503_unavailable():
    """Test ERP returning 503 raises ERPServiceUnavailableError."""
    client = ERPClient(base_url="http://test-erp:9100")
    mock_resp = MagicMock()
    mock_resp.status_code = 503

    with patch('requests.get', return_value=mock_resp):
        with pytest.raises(ERPServiceUnavailableError):
            client.match_invoice("PO-000123", "V-1003", 100.0)


def test_erp_client_handles_timeout():
    """Test network timeout raises ERPTimeoutError."""
    client = ERPClient(base_url="http://test-erp:9100")

    with patch('requests.get', side_effect=requests.exceptions.Timeout("Read timed out")):
        with pytest.raises(ERPTimeoutError):
            client.match_invoice("PO-000123", "V-1003", 100.0)


def test_erp_client_handles_400_permanent_error():
    """Test HTTP 400 Bad Request raises PermanentERPError."""
    client = ERPClient(base_url="http://test-erp:9100")
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request: Malformed query"

    with patch('requests.get', return_value=mock_resp):
        with pytest.raises(PermanentERPError):
            client.match_invoice("PO-000123", "V-1003", 100.0)


@pytest.mark.django_db
def test_transient_error_exhaustion_routes_to_dead_letter():
    """
    Verify that when transient ERP errors persist beyond MAX_RETRIES,
    the invoice is permanently routed to DEAD_LETTERED status.
    """
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    invoice = Invoice.objects.create(
        invoice_id="INV-RETRY-FAIL",
        vendor_id="V-1001",
        po_number="PO-000001",
        amount=100.0,
        currency="USD",
        retry_count=2,  # Already attempted twice, next failure reaches max (3)
        status=Invoice.STATUS_PROCESSING
    )

    with patch('reconciliation.tasks.ERPCache.client', new_callable=lambda: fake_redis), \
         patch('reconciliation.tasks.GlobalRateLimiter.client', new_callable=lambda: fake_redis), \
         patch.object(ERPClient, 'match_invoice', side_effect=ERPServiceUnavailableError("503 Upstream")):

        process_invoice_task("INV-RETRY-FAIL")

        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_DEAD_LETTERED
        assert "Max retries" in invoice.failure_reason
