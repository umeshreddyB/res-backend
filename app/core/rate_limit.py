"""Simple in-memory rate limiter.

WARNING: This only works correctly with a SINGLE worker process. If running multiple
Gunicorn/Uvicorn workers, each worker has its own counter — the effective limit is
multiplied by the number of workers. For strict multi-worker enforcement, store
counters in Redis or the database. See README for details.
"""
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from .config import settings


class RateLimiter:
    """Sliding-window rate limiter keyed by client IP."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_seconds
        hits = self._hits[key]
        # Remove expired entries
        self._hits[key] = hits = [t for t in hits if t > window_start]
        if len(hits) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
        hits.append(now)


login_limiter = RateLimiter(settings.rate_limit_login, settings.rate_limit_window_seconds)
register_limiter = RateLimiter(settings.rate_limit_register, settings.rate_limit_window_seconds)


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
