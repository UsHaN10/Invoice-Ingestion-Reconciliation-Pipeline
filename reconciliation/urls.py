from django.urls import path
from .views import (
    ingest_invoice_view,
    get_invoice_detail_view,
    reporting_summary_view,
    health_check_view,
    metrics_view,
)

urlpatterns = [
    # Ingestion & Invoice details
    path('api/invoices/', ingest_invoice_view, name='ingest_invoices'),
    path('api/invoices/<str:invoice_id>/', get_invoice_detail_view, name='invoice_detail'),

    # Reporting & Metrics
    path('api/reporting/summary/', reporting_summary_view, name='reporting_summary'),
    path('metrics', metrics_view, name='metrics'),
    path('metrics/', metrics_view, name='metrics_slash'),

    # Health Checks
    path('healthz', health_check_view, name='health_check'),
    path('healthz/', health_check_view, name='health_check_slash'),
]
