import asyncio
from typing import Any

from curl_cffi import requests as cffi_requests
from structlog import get_logger

from app.config import settings
from app.errors import BlockedException, RateLimitedException

logger = get_logger()


def _fetch_sync(
    url: str,
    impersonate: str = "chrome124",
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
) -> cffi_requests.Response:
    timeout = timeout or settings.request_timeout
    kwargs: dict[str, Any] = {
        "impersonate": impersonate,
        "timeout": timeout,
        "headers": headers,
    }
    if settings.proxy_url:
        kwargs["proxies"] = {
            "http": settings.proxy_url,
            "https": settings.proxy_url,
        }
    return cffi_requests.get(url, **kwargs)


async def fetch_fast(
    url: str,
    impersonate: str = "chrome124",
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
    max_retries: int | None = None,
) -> cffi_requests.Response:
    """Async wrapper with retry. Raises on 403/429 after retries exhausted."""
    retries = max_retries if max_retries is not None else settings.max_retries
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = await asyncio.to_thread(
                _fetch_sync, url, impersonate, timeout, headers
            )
            if resp.status_code == 429:
                raise RateLimitedException(f"429 on {url}")
            if resp.status_code == 403:
                raise BlockedException(f"403 on {url}")
            return resp
        except (RateLimitedException, BlockedException):
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = 0.5 * (attempt + 1)
                logger.warning(
                    "fetch_fast retry",
                    url=url,
                    attempt=attempt + 1,
                    error=str(e),
                )
                await asyncio.sleep(wait)

    raise last_exc  # type: ignore[misc]
