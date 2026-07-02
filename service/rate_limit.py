"""Simple in-memory rate limiter for /v1/auth/* endpoints.
Default: 30 requests per minute per IP. Sliding window.
"""
import time
from collections import deque
from threading import Lock
from fastapi import Request
from fastapi.responses import JSONResponse


class IPRateLimiter:
    def __init__(self, max_per_minute: int = 30):
        self.max = max_per_minute
        self.lock = Lock()
        self.hits: dict = {}

    def _client_ip(self, request: Request) -> str:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, key: str):
        with self.lock:
            now = time.time()
            window = self.hits.setdefault(key, deque())
            while window and window[0] < now - 60:
                window.popleft()
            if len(window) >= self.max:
                return False, len(window)
            window.append(now)
            return True, len(window)


_limiter = IPRateLimiter(max_per_minute=30)


def rate_limit_middleware_factory():
    async def middleware(request: Request, call_next):
        if not request.url.path.startswith("/v1/auth/"):
            return await call_next(request)
        ip = _limiter._client_ip(request)
        allowed, count = _limiter.check(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": f"rate limit exceeded ({count}/min)"},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
    return middleware
