"""Shared HTTP utilities for scrapers.

Real-data-only: no mock/fallback data is ever generated. On failure, scripts
write diagnostic error state to the JSON output so the frontend can display
the real failure rather than pretending to have data.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# A realistic browser User-Agent set. GitHub Actions runners can be blocked
# by Cloudflare if the UA looks datacenter-y, so mimic a normal Chrome.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class FetchError(RuntimeError):
    """Raised when an HTTP fetch ultimately fails."""

    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


def make_session() -> requests.Session:
    """Return a configured Session (keep-alive, cookies, browser-like)."""
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def http_get(
    url: str,
    *,
    retries: int = 3,
    base_delay: float = 2.0,
    timeout: int = 25,
    headers: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    allow_status: tuple[int, ...] = (200,),
    return_bytes: bool = False,
):
    """GET a URL with retries + exponential backoff.

    Returns decoded text by default, or raw bytes if return_bytes=True.
    Raises FetchError on persistent failure (with status + truncated body
    attached for diagnostics).
    """
    sess = session or requests
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_status: Optional[int] = None
    last_body_snippet: Optional[str] = None
    last_err: str = ""

    for attempt in range(retries):
        try:
            resp = sess.get(url, headers=merged_headers, timeout=timeout, allow_redirects=True)
            last_status = resp.status_code
            if resp.status_code in allow_status:
                if return_bytes:
                    return resp.content
                if resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding
                return resp.text

            last_err = f"HTTP {resp.status_code}"
            try:
                last_body_snippet = resp.text[:400]
            except Exception:
                last_body_snippet = None
            logger.warning(
                "GET %s -> %s (attempt %d/%d)",
                url,
                last_err,
                attempt + 1,
                retries,
            )
        except requests.RequestException as exc:
            last_err = repr(exc)
            logger.warning(
                "GET %s -> %s (attempt %d/%d)",
                url,
                last_err,
                attempt + 1,
                retries,
            )

        if attempt < retries - 1:
            time.sleep(base_delay * (2 ** attempt))

    raise FetchError(
        f"GET failed for {url}: {last_err}",
        status=last_status,
        body=last_body_snippet,
    )


def polite_sleep(seconds: float = 1.2) -> None:
    """Throttle between requests."""
    time.sleep(seconds)
