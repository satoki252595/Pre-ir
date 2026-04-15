"""Shared HTTP utilities for scrapers.

Real-data-only. No mock/fallback data is ever generated. If a fetch fails,
the calling script must raise/exit so the CI build is marked as failed.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}


class FetchError(RuntimeError):
    """Raised when an HTTP fetch ultimately fails."""


def http_get(
    url: str,
    *,
    retries: int = 3,
    base_delay: float = 2.0,
    timeout: int = 25,
    headers: Optional[dict] = None,
    allow_status: tuple[int, ...] = (200,),
) -> str:
    """GET a URL with retries + exponential backoff. Returns decoded text.

    Raises FetchError on persistent failure.
    """
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)

    last_err: str = ""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=merged, timeout=timeout)
            if resp.status_code in allow_status:
                if resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding
                return resp.text
            last_err = f"HTTP {resp.status_code}"
            logger.warning(
                "GET %s -> %s (attempt %d/%d)", url, last_err, attempt + 1, retries
            )
        except requests.RequestException as exc:  # network errors
            last_err = repr(exc)
            logger.warning(
                "GET %s -> %s (attempt %d/%d)", url, last_err, attempt + 1, retries
            )

        if attempt < retries - 1:
            time.sleep(base_delay * (2 ** attempt))

    raise FetchError(f"GET failed for {url}: {last_err}")


def polite_sleep(seconds: float = 1.2) -> None:
    """Throttle between requests to be a good citizen."""
    time.sleep(seconds)
