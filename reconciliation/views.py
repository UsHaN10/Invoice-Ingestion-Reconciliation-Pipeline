import logging
from datetime import timedelta
import redis
from django.conf import settings
from django.db import IntegrityError, connection
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Invoice
from .serializers import InvoiceInputSerializer, InvoiceModelSerializer
from .tasks import process_invoice_task

logger = logging.getLogger(__name__)


@api_view(['POST'])
def ingest_invoice_view(request):
    """
    Ingestion API Endpoint.
    
    Accepts an invoice JSON record, performs basic structural validation,
    ensures idempotency via database constraints, enqueues background processing,
    and returns 202 Accepted immediately without blocking on the slow ERP.
    """
    data = request.data
    is_batch = isinstance(data, list)
    items = data if is_batch else [data]

    results = []
    for item in items:
        invoice_id = item.get('invoice_id')
        if not invoice_id:
            results.append({
                'invoice_id': None,
                'status': 'REJECTED',
                'message': 'invoice_id is required'
            })
            continue

        # 1. Idempotency Check: check if invoice already exists
        existing = Invoice.objects.filter(invoice_id=invoice_id).first()
        if existing:
            logger.info(f"[invoice_id={invoice_id}] [event=INGEST_DUPLICATE] Invoice already exists with status {existing.status}")
            results.append({
                'invoice_id': invoice_id,
                'status': existing.status,
                'message': 'Duplicate submission: invoice already received',
                'is_duplicate': True
            })
            continue

        # 2. Extract fields
        vendor_id = item.get('vendor_id', '')
        po_number = item.get('po_number', '')
        amount = item.get('amount')
        currency = item.get('currency', 'USD')
        invoice_number = item.get('invoice_number', '')
        vendor_name = item.get('vendor_name', '')
        cost_center = item.get('cost_center', '')
        source = item.get('source', '')
        
        # Calculate or use existing content hash
        content_hash = item.get('content_hash')
        if not content_hash and vendor_id and po_number and amount:
            content_hash = Invoice.calculate_content_hash(
                vendor_id, po_number, amount, currency, invoice_number
            )

        # 3. Create invoice record in database
        try:
            invoice = Invoice.objects.create(
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                vendor_id=vendor_id,
                vendor_name=vendor_name,
                po_number=po_number,
                amount=amount if (amount is not None and str(amount).replace('.', '', 1).replace('-', '', 1).isdigit()) else None,
                currency=currency,
                cost_center=cost_center,
                source=source,
                content_hash=content_hash or '',
                raw_payload=item,
                status=Invoice.STATUS_PENDING,
            )
        except IntegrityError:
            # Concurrent duplicate submission handled gracefully by database constraint
            existing = Invoice.objects.get(invoice_id=invoice_id)
            results.append({
                'invoice_id': invoice_id,
                'status': existing.status,
                'message': 'Duplicate submission caught by unique constraint',
                'is_duplicate': True
            })
            continue

        # 4. Enqueue background Celery task
        process_invoice_task.delay(invoice.invoice_id)
        logger.info(f"[invoice_id={invoice_id}] [event=ENQUEUED] Enqueued to Redis queue.")

        results.append({
            'invoice_id': invoice_id,
            'status': Invoice.STATUS_PENDING,
            'message': 'Invoice accepted and queued for reconciliation',
            'is_duplicate': False
        })

    if not is_batch:
        single_result = results[0]
        http_status = status.HTTP_200_OK if single_result.get('is_duplicate') else status.HTTP_202_ACCEPTED
        return Response(single_result, status=http_status)

    return Response({'results': results}, status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
def get_invoice_detail_view(request, invoice_id):
    """Retrieve full processing details and audit status for a specific invoice."""
    try:
        invoice = Invoice.objects.get(invoice_id=invoice_id)
        serializer = InvoiceModelSerializer(invoice)
        return Response(serializer.data, status=status.HTTP_200_OK)
    except Invoice.DoesNotExist:
        return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
def reporting_summary_view(request):
    """
    Reporting API Endpoint.
    
    Returns aggregated metrics:
    - Counts by status (pending, processing, matched, unmatched, invalid, failed, dead_lettered, duplicate)
    - Match rate over reconciled invoices
    - Stuck backlog count (invoices pending or processing > 5 minutes)
    """
    stuck_threshold = timezone.now() - timedelta(minutes=5)

    aggregates = Invoice.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status=Invoice.STATUS_PENDING)),
        processing=Count('id', filter=Q(status=Invoice.STATUS_PROCESSING)),
        matched=Count('id', filter=Q(status=Invoice.STATUS_MATCHED)),
        unmatched=Count('id', filter=Q(status=Invoice.STATUS_UNMATCHED)),
        invalid=Count('id', filter=Q(status=Invoice.STATUS_INVALID)),
        failed=Count('id', filter=Q(status=Invoice.STATUS_FAILED)),
        dead_lettered=Count('id', filter=Q(status=Invoice.STATUS_DEAD_LETTERED)),
        duplicate=Count('id', filter=Q(status=Invoice.STATUS_DUPLICATE)),
        stuck_pending=Count('id', filter=Q(
            status__in=[Invoice.STATUS_PENDING, Invoice.STATUS_PROCESSING],
            created_at__lt=stuck_threshold
        )),
    )

    reconciled_count = aggregates['matched'] + aggregates['unmatched']
    match_rate = (
        round((aggregates['matched'] / reconciled_count) * 100, 2)
        if reconciled_count > 0 else 0.0
    )

    data = {
        'total_invoices': aggregates['total'],
        'counts': {
            'pending': aggregates['pending'],
            'processing': aggregates['processing'],
            'matched': aggregates['matched'],
            'unmatched': aggregates['unmatched'],
            'invalid': aggregates['invalid'],
            'failed': aggregates['failed'],
            'dead_lettered': aggregates['dead_lettered'],
            'duplicate': aggregates['duplicate'],
        },
        'match_rate_percentage': match_rate,
        'stuck_backlog_count': aggregates['stuck_pending'],
        'timestamp': timezone.now().isoformat(),
    }

    return Response(data, status=status.HTTP_200_OK)


@api_view(['GET'])
def health_check_view(request):
    """
    Health check endpoint for container orchestrators and monitoring tools.
    Verifies connectivity to database and Redis.
    """
    health_status = {
        'status': 'healthy',
        'database': 'unknown',
        'redis': 'unknown',
        'timestamp': timezone.now().isoformat()
    }
    is_healthy = True

    # Check Database
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        health_status['database'] = 'ok'
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'
        is_healthy = False

    # Check Redis
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        health_status['redis'] = 'ok'
    except Exception as e:
        health_status['redis'] = f'error: {str(e)}'
        is_healthy = False

    http_status = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    health_status['status'] = 'healthy' if is_healthy else 'unhealthy'
    return Response(health_status, status=http_status)


@api_view(['GET'])
def metrics_view(request):
    """
    Prometheus-compatible plain text metrics endpoint.
    Exposes key operational gauges.
    """
    counts = Invoice.objects.values('status').annotate(total=Count('id'))
    status_map = {item['status'].lower(): item['total'] for item in counts}

    lines = [
        "# HELP reconciliation_invoices_total Number of invoices partitioned by status.",
        "# TYPE reconciliation_invoices_total gauge",
    ]

    all_statuses = [
        'pending', 'processing', 'matched', 'unmatched',
        'invalid', 'duplicate', 'failed', 'dead_lettered'
    ]
    for st in all_statuses:
        val = status_map.get(st, 0)
        lines.append(f'reconciliation_invoices_total{{status="{st}"}} {val}')

    # Output plain text
    content = "\n".join(lines) + "\n"
    from django.http import HttpResponse
    return HttpResponse(content, content_type="text/plain; version=0.0.4; charset=utf-8")
