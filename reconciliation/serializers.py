from rest_framework import serializers
from .models import Invoice


VALID_CURRENCIES = {'USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF'}


class InvoiceInputSerializer(serializers.Serializer):
    """
    Validates incoming raw invoice payloads from the ingestion API.
    """
    invoice_id = serializers.CharField(max_length=64, required=True, allow_blank=False)
    invoice_number = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    vendor_id = serializers.CharField(max_length=64, required=True, allow_blank=True)
    vendor_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    po_number = serializers.CharField(max_length=64, required=True, allow_blank=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=10, required=False, default='USD')
    cost_center = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    source = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    content_hash = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    issued_at = serializers.CharField(required=False, allow_blank=True)
    submitted_at = serializers.CharField(required=False, allow_blank=True)
    _defect = serializers.CharField(required=False, allow_blank=True)  # synthetic defect marker if present

    def validate(self, attrs):
        """
        Domain validation rule checks:
        - vendor_id must not be blank
        - po_number must not be blank
        - amount must be present and > 0
        - currency must be a recognized currency
        """
        errors = {}

        vendor_id = attrs.get('vendor_id')
        if not vendor_id or not vendor_id.strip():
            errors['vendor_id'] = 'Vendor ID cannot be blank.'

        po_number = attrs.get('po_number')
        if not po_number or not po_number.strip():
            errors['po_number'] = 'PO number cannot be blank.'

        amount = attrs.get('amount')
        if amount is None:
            errors['amount'] = 'Amount is required.'
        elif amount <= 0:
            errors['amount'] = f'Amount must be greater than zero, received {amount}.'

        currency = attrs.get('currency', '').strip().upper()
        if not currency or currency not in VALID_CURRENCIES:
            errors['currency'] = f'Invalid or unsupported currency: {currency}.'

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class InvoiceModelSerializer(serializers.ModelSerializer):
    """Serializer for exposing Invoice model records."""
    class Meta:
        model = Invoice
        fields = [
            'invoice_id',
            'invoice_number',
            'vendor_id',
            'vendor_name',
            'po_number',
            'amount',
            'currency',
            'cost_center',
            'source',
            'content_hash',
            'status',
            'failure_reason',
            'retry_count',
            'created_at',
            'updated_at',
            'processed_at',
            'erp_response',
        ]
