import ipaddress
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


def is_trusted_proxy(ip: str) -> bool:
    """Check whether the given IP is a configured trusted reverse proxy."""
    if not ip or ip == "unknown":
        return False
    try:
        client_addr = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for proxy_str in settings.trusted_proxy_list:
        if proxy_str == "*":
            return True
        try:
            if "/" in proxy_str:
                if client_addr in ipaddress.ip_network(proxy_str, strict=False):
                    return True
            else:
                if client_addr == ipaddress.ip_address(proxy_str):
                    return True
        except ValueError:
            if proxy_str == ip:
                return True
    return False


def get_client_ip(request: Request) -> str:
    """Extract client IP address.

    Only trusts the 'X-Forwarded-For' header when the request originates from a
    trusted proxy (configured via TRUSTED_PROXIES). Otherwise uses request.client.host
    to prevent IP spoofing attacks.
    """
    direct_ip = request.client.host if request.client else "unknown"

    if is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            if client_ip:
                return client_ip

    return direct_ip

