import json
import logging
from typing import Optional, Dict, Any
import redis
from django.conf import settings

logger = logging.getLogger(__name__)


class ERPCache:
    """
    Redis-backed cache for ERP reconciliation lookups.
    
    Why Cache?
    In generate_invoices.py and enterprise AP environments, purchase orders are
    referenced repeatedly (e.g. 1 PO per ~3 invoices). Because the mock ERP is
    slow (200-900ms) and strictly rate-limited to 10 req/s, caching lookups saves
    substantial ERP capacity and dramatically increases pipeline throughput.
    
    Cache Key Construction:
    Must incorporate all 4 matching parameters: po_number, vendor_id, amount, and currency.
    If amount or vendor were omitted, an invoice with a different amount or wrong vendor
    could mistakenly receive a cached 'matched=True' from a different invoice, causing
    an erroneous payment!
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None, ttl: int = 300):
        self._client = redis_client
        self.ttl = ttl

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._client

    def _make_key(self, po_number: str, vendor_id: str, amount: Any, currency: str) -> str:
        # Standardize amount format to 2 decimal places to avoid float string mismatches
        formatted_amount = f"{float(amount):.2f}"
        return f"erp_cache:{po_number.strip()}:{vendor_id.strip()}:{formatted_amount}:{currency.strip().upper()}"

    def get_match(self, po_number: str, vendor_id: str, amount: Any, currency: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached ERP match result if available."""
        key = self._make_key(po_number, vendor_id, amount, currency)
        try:
            cached_data = self.client.get(key)
            if cached_data:
                logger.info(f"Cache HIT for key {key}")
                return json.loads(cached_data)
            logger.debug(f"Cache MISS for key {key}")
            return None
        except redis.RedisError as e:
            logger.warning(f"Redis cache read error: {e}. Bypassing cache.")
            return None

    def set_match(self, po_number: str, vendor_id: str, amount: Any, currency: str, result: Dict[str, Any]):
        """Store ERP match result in Redis with TTL."""
        key = self._make_key(po_number, vendor_id, amount, currency)
        try:
            self.client.setex(key, self.ttl, json.dumps(result))
            logger.debug(f"Stored result in cache for key {key} (TTL={self.ttl}s)")
        except redis.RedisError as e:
            logger.warning(f"Redis cache write error: {e}")
