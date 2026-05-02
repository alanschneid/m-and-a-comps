"""
Internal HTTP client for SEC EDGAR.

Encapsulates SEC compliance requirements:
    - User-Agent header on every request (mandatory per SEC policy)
    - Rate limiting at 8 req/sec (below SEC's 10 req/sec hard limit)
    - Retry with exponential backoff on transient errors (502, 503, 504, 429)
    - Local file cache to avoid re-downloading filings

This module is internal to the data_sources package — the leading underscore
in the filename signals "do not import from outside this package".
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ──────── Constants ────────

USER_AGENT = "M&A Comps Auto-Refresh alanschneid1@gmail.com"
MIN_INTERVAL_SECONDS = 1.0 / 8.0          # ≈ 0.125s between requests (8 req/sec)
CACHE_DIR = Path("filings_cache")
CACHE_DIR.mkdir(exist_ok=True)


# ──────── The HTTP client ────────

class EdgarClient:
    """
    Polite, cached HTTP client for SEC EDGAR.

    Every public method respects:
        1. SEC User-Agent requirement
        2. SEC rate limit (8 req/sec, below the 10 req/sec ceiling)
        3. Local file cache (downloads happen at most once per URL)
        4. Automatic retries on transient HTTP errors

    Usage:
        client = EdgarClient()
        html = client.get("https://www.sec.gov/Archives/edgar/data/...")
        data = client.get_json("https://data.sec.gov/submissions/CIK0001353283.json")
    """

    def __init__(self, use_cache: bool = True) -> None:
        self.use_cache = use_cache
        self._last_request_ts: float = 0.0

        # Configure a requests Session with retry logic baked in.
        # Retries handle transient errors (server overloaded, brief outages).
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,                          # waits: 2s, 4s, 8s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)

        self._session = requests.Session()
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        })

    # ──────── Public methods ────────

    def get(self, url: str) -> str:
        """
        Fetch a URL and return the response body as text.
        Uses cache if available; otherwise downloads, caches, and returns.
        """
        cached = self._read_cache(url) if self.use_cache else None
        if cached is not None:
            return cached

        self._throttle()
        response = self._session.get(url, timeout=30)
        response.raise_for_status()                    # raises on 4xx/5xx

        body = response.text
        if self.use_cache:
            self._write_cache(url, body)
        return body

    def get_json(self, url: str) -> dict:
        """
        Fetch a URL and parse the response body as JSON.
        Used for SEC's data.sec.gov endpoints which return structured JSON.
        """
        body = self.get(url)
        return json.loads(body)

    # ──────── Internal helpers ────────

    def _throttle(self) -> None:
        """Sleep just long enough to stay under 8 req/sec."""
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_ts = time.time()

    def _cache_path(self, url: str) -> Path:
        """Map a URL to a deterministic cache file path."""
        # SHA-1 hash of the URL → fixed-length, filesystem-safe filename.
        # Length 16 is enough to avoid collisions in this use case.
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / f"{digest}.cache"

    def _read_cache(self, url: str) -> Optional[str]:
        path = self._cache_path(url)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _write_cache(self, url: str, body: str) -> None:
        self._cache_path(url).write_text(body, encoding="utf-8")