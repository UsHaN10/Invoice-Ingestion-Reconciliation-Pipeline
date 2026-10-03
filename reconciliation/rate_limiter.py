import logging
import time
from typing import Optional
import redis
from django.conf import settings

logger = logging.getLogger(__name__)


class GlobalRateLimiter:
    """
    Redis-backed global fixed-window rate limiter.
    
    Why Redis?
    In a distributed system with multiple Celery worker processes, each worker cannot
    maintain an independent local rate limit. A local 10 req/s limit per worker across
    5 workers would cause 50 req/s upstream, overwhelming the ERP and causing 429 floods.
    
    By storing the second-by-second request count in Redis (which is atomic and centralized),
    all workers share the exact same 10 req/s limit globally.
    
    Algorithm:
    1. Key is bucketed by unix second: 'erp:rate_limit:<epoch_second>'
    2. INCR the key atomically.
    3. If count <= limit: request is allowed immediately.
    4. If count > limit: current second is full. Sleep until the next second begins and retry.
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None, limit_per_sec: int = 10):
        self.limit = limit_per_sec
        self._client = redis_client

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._client

    def acquire(self, timeout: float = 10.0) -> bool:
        """
        Block and wait until a rate limit slot is available, or until timeout expires.
        Returns True if acquired, False if timed out.
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            current_sec = int(time.time())
            key = f"erp:rate_limit:{current_sec}"

            try:
                # Atomically increment counter and set a 2-second expiration
                pipe = self.client.pipeline()
                pipe.incr(key)
                pipe.expire(key, 2)
                results = pipe.execute()
                current_count = results[0]

                if current_count <= self.limit:
                    return True

                # Rate limit exceeded for this second: wait until the next second
                elapsed_in_second = time.time() % 1.0
                sleep_duration = max(0.05, 1.0 - elapsed_in_second + 0.02)
                logger.debug(
                    f"Rate limit reached ({current_count}/{self.limit} req/s). "
                    f"Sleeping {sleep_duration:.2f}s until next second."
                )
                time.sleep(sleep_duration)

            except redis.RedisError as e:
                logger.error(f"Redis rate limiter connection error: {e}. Falling back to throttle.")
                time.sleep(0.1)
                return True

        logger.warning(f"GlobalRateLimiter timed out after {timeout}s.")
        return False
