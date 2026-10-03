import pytest
from rest_framework.test import APIClient
from reconciliation.models import Invoice
from unittest.mock import patch


@pytest.mark.django_db
def test_reporting_summary_counts_and_match_rate():
    """
    Verify /api/reporting/summary/ aggregates all statuses correctly
    and calculates match rate: matched / (matched + unmatched) * 100.
    """
    # Seed 3 matched, 1 unmatched, 2 invalid, 1 dead lettered, 1 pending
    for i in range(3):
        Invoice.objects.create(invoice_id=f"INV-M-{i}", status=Invoice.STATUS_MATCHED)
    Invoice.objects.create(invoice_id="INV-U-1", status=Invoice.STATUS_UNMATCHED)
    for i in range(2):
        Invoice.objects.create(invoice_id=f"INV-INV-{i}", status=Invoice.STATUS_INVALID)
    Invoice.objects.create(invoice_id="INV-DL-1", status=Invoice.STATUS_DEAD_LETTERED)
    Invoice.objects.create(invoice_id="INV-P-1", status=Invoice.STATUS_PENDING)

    client = APIClient()
    response = client.get('/api/reporting/summary/')
    assert response.status_code == 200
    data = response.data

    assert data['total_invoices'] == 8
    assert data['counts']['matched'] == 3
    assert data['counts']['unmatched'] == 1
    assert data['counts']['invalid'] == 2
    assert data['counts']['dead_lettered'] == 1
    assert data['counts']['pending'] == 1

    # 3 / (3 + 1) * 100 = 75.0%
    assert data['match_rate_percentage'] == 75.0


@pytest.mark.django_db
def test_health_check_endpoint():
    """Verify /healthz returns status ok for database and redis."""
    client = APIClient()
    with patch('redis.Redis.from_url') as mock_redis:
        mock_instance = mock_redis.return_value
        mock_instance.ping.return_value = True

        response = client.get('/healthz')
        assert response.status_code == 200
        assert response.data['status'] == 'healthy'
        assert response.data['database'] == 'ok'
        assert response.data['redis'] == 'ok'


@pytest.mark.django_db
def test_metrics_endpoint_format():
    """Verify /metrics returns Prometheus formatted plain text."""
    Invoice.objects.create(invoice_id="INV-METRIC-1", status=Invoice.STATUS_MATCHED)

    client = APIClient()
    response = client.get('/metrics')
    assert response.status_code == 200
    assert "text/plain" in response.headers["Content-Type"]
    content = response.content.decode('utf-8')
    assert 'reconciliation_invoices_total{status="matched"} 1' in content
