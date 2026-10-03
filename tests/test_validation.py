import pytest
from rest_framework.exceptions import ValidationError
from reconciliation.serializers import InvoiceInputSerializer
from reconciliation.models import Invoice
from reconciliation.tasks import process_invoice_task
import fakeredis
from unittest.mock import patch


@pytest.mark.django_db
def test_valid_invoice_payload():
    """Verify that a well-formed invoice payload passes serializer validation."""
    data = {
        "invoice_id": "INV-100",
        "vendor_id": "V-1001",
        "po_number": "PO-000001",
        "amount": "1500.00",
        "currency": "USD"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
def test_validation_fails_on_missing_amount():
    """Verify that an invoice missing amount raises a validation error."""
    data = {
        "invoice_id": "INV-101",
        "vendor_id": "V-1001",
        "po_number": "PO-000001",
        "currency": "USD"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert not serializer.is_valid()
    assert "amount" in serializer.errors


@pytest.mark.django_db
def test_validation_fails_on_negative_amount():
    """Verify that an invoice with a negative amount is rejected."""
    data = {
        "invoice_id": "INV-102",
        "vendor_id": "V-1001",
        "po_number": "PO-000001",
        "amount": "-50.00",
        "currency": "USD"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert not serializer.is_valid()
    assert "amount" in serializer.errors


@pytest.mark.django_db
def test_validation_fails_on_missing_po():
    """Verify that an invoice with an empty or blank PO is rejected."""
    data = {
        "invoice_id": "INV-103",
        "vendor_id": "V-1001",
        "po_number": "   ",
        "amount": "500.00",
        "currency": "USD"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert not serializer.is_valid()
    assert "po_number" in serializer.errors


@pytest.mark.django_db
def test_validation_fails_on_blank_vendor():
    """Verify that an invoice with an empty vendor is rejected."""
    data = {
        "invoice_id": "INV-104",
        "vendor_id": "",
        "po_number": "PO-000001",
        "amount": "500.00",
        "currency": "USD"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert not serializer.is_valid()
    assert "vendor_id" in serializer.errors


@pytest.mark.django_db
def test_validation_fails_on_invalid_currency():
    """Verify that an unknown or bogus currency is rejected."""
    data = {
        "invoice_id": "INV-105",
        "vendor_id": "V-1001",
        "po_number": "PO-000001",
        "amount": "500.00",
        "currency": "XYZ"
    }
    serializer = InvoiceInputSerializer(data=data)
    assert not serializer.is_valid()
    assert "currency" in serializer.errors


@pytest.mark.django_db
def test_worker_marks_invalid_invoice_without_calling_erp():
    """
    Ensure the worker directly marks malformed invoices as INVALID
    without making a wasteful call to the ERP.
    """
    invoice = Invoice.objects.create(
        invoice_id="INV-BAD-01",
        vendor_id="",  # Blank vendor
        po_number="PO-000001",
        amount=100.0,
        currency="USD",
        status=Invoice.STATUS_PENDING
    )

    with patch('reconciliation.tasks.ERPClient') as mock_erp_cls:
        process_invoice_task("INV-BAD-01")

        # Mock ERP client must NOT have been instantiated or called
        mock_erp_cls.assert_not_called()

        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_INVALID
        assert "vendor_id" in invoice.failure_reason
