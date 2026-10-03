import logging
from typing import Dict, Any
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ERPClientError(Exception):
    """Base exception for ERP client errors."""
    pass


class TransientERPError(ERPClientError):
    """Temporary errors that can be safely retried."""
    pass


class ERPRateLimitError(TransientERPError):
    """HTTP 429 - Rate limit exceeded on ERP."""
    def __init__(self, message="ERP rate limit exceeded", retry_after: int = 1):
        super().__init__(message)
        self.retry_after = retry_after


class ERPServiceUnavailableError(TransientERPError):
    """HTTP 503 - ERP upstream is temporarily unavailable."""
    pass


class ERPTimeoutError(TransientERPError):
    """ERP call timed out (network timeout or HTTP 504)."""
    pass


class PermanentERPError(ERPClientError):
    """Non-retryable errors (e.g. HTTP 400 Bad Request)."""
    pass


class ERPClient:
    """
    HTTP client for reconciling invoices against the mock ERP system of record.
    
    Target Endpoint:
      GET /erp/purchase-orders/match?po_number=...&vendor_id=...&amount=...&currency=...
    """

    def __init__(self, base_url: str = None, timeout: float = None):
        self.base_url = (base_url or settings.ERP_BASE_URL).rstrip('/')
        self.timeout = timeout or settings.ERP_TIMEOUT_SECONDS

    def match_invoice(self, po_number: str, vendor_id: str, amount: float, currency: str = "USD") -> Dict[str, Any]:
        """
        Calls ERP to match an invoice against known Purchase Orders.
        
        Raises:
            ERPRateLimitError: If ERP responds with 429
            ERPServiceUnavailableError: If ERP responds with 503
            ERPTimeoutError: If request times out or responds with 504
            PermanentERPError: On unrecoverable 4xx client errors
        
        Returns:
            dict containing:
              - matched: True/False
              - po_number, vendor_id, po_amount, currency (if matched)
              - reason: "po_not_found" | "vendor_mismatch" | "amount_out_of_tolerance" (if unmatched)
        """
        url = f"{self.base_url}/erp/purchase-orders/match"
        params = {
            "po_number": po_number,
            "vendor_id": vendor_id,
            "amount": float(amount),
            "currency": currency,
        }

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            logger.warning(f"ERP request timed out after {self.timeout}s: {e}")
            raise ERPTimeoutError(f"ERP request timed out after {self.timeout}s") from e
        except requests.exceptions.RequestException as e:
            logger.warning(f"ERP network connection error: {e}")
            raise ERPServiceUnavailableError(f"ERP network error: {e}") from e

        # Handle status codes
        if response.status_code == 200:
            return response.json()

        elif response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            logger.warning(f"ERP 429 Rate Limited. Retry-After: {retry_after}s")
            raise ERPRateLimitError(retry_after=retry_after)

        elif response.status_code == 503:
            logger.warning("ERP 503 Service Unavailable (transient fault)")
            raise ERPServiceUnavailableError("ERP returned 503 Upstream Unavailable")

        elif response.status_code == 504:
            logger.warning("ERP 504 Gateway Timeout")
            raise ERPTimeoutError("ERP returned 504 Gateway Timeout")

        elif 400 <= response.status_code < 500:
            logger.error(f"ERP Client error {response.status_code}: {response.text}")
            raise PermanentERPError(f"ERP client error {response.status_code}: {response.text}")

        else:
            logger.error(f"Unexpected ERP response status {response.status_code}: {response.text}")
            raise ERPServiceUnavailableError(f"Unexpected ERP status {response.status_code}")
