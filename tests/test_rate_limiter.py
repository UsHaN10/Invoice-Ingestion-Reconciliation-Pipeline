import time
import fakeredis
from reconciliation.rate_limiter import GlobalRateLimiter
from reconciliation.cache import ERPCache


def test_rate_limiter_allows_up_to_limit():
    """Verify rate limiter allows exactly limit requests within one second."""
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    limiter = GlobalRateLimiter(redis_client=fake_redis, limit_per_sec=5)

    # First 5 calls must succeed immediately
    for _ in range(5):
        assert limiter.acquire(timeout=0.5) is True


def test_rate_limiter_blocks_above_limit_across_workers():
    """
    Verify that two independent worker instances sharing the same Redis client
    jointly hit the limit.
    """
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    worker_1_limiter = GlobalRateLimiter(redis_client=fake_redis, limit_per_sec=4)
    worker_2_limiter = GlobalRateLimiter(redis_client=fake_redis, limit_per_sec=4)

    # Worker 1 takes 2 slots
    assert worker_1_limiter.acquire(timeout=0.1) is True
    assert worker_1_limiter.acquire(timeout=0.1) is True

    # Worker 2 takes 2 slots
    assert worker_2_limiter.acquire(timeout=0.1) is True
    assert worker_2_limiter.acquire(timeout=0.1) is True

    # 5th request should fail when timeout is very low (preventing sleep into next second)
    assert worker_1_limiter.acquire(timeout=0.01) is False


def test_erp_cache_key_isolation_and_hit():
    """
    Verify that ERPCache correctly stores and returns matches,
    and isolates by vendor, amount, and currency.
    """
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    cache = ERPCache(redis_client=fake_redis, ttl=60)

    match_result = {"matched": True, "po_amount": 1000.0, "status": "OPEN"}
    cache.set_match("PO-1", "V-1", 1000.0, "USD", match_result)

    # Cache hit for identical parameters
    hit = cache.get_match("PO-1", "V-1", 1000.0, "USD")
    assert hit is not None
    assert hit["matched"] is True

    # Cache miss if amount differs
    miss_amount = cache.get_match("PO-1", "V-1", 2000.0, "USD")
    assert miss_amount is None

    # Cache miss if vendor differs
    miss_vendor = cache.get_match("PO-1", "V-2", 1000.0, "USD")
    assert miss_vendor is None

    # Cache miss if currency differs
    miss_currency = cache.get_match("PO-1", "V-1", 1000.0, "EUR")
    assert miss_currency is None
