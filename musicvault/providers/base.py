"""
Base provider client with rate limiting, retry backoff, and SQLite caching.
"""
import time
import json
import logging
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("musicvault.providers")


class BaseProvider:
    """Base HTTP client for music metadata providers."""

    def __init__(self, name: str, default_delay: float = 1.0, max_requests: int = 50,
                 contact: str = ""):
        self.name = name
        self.default_delay = default_delay
        self.max_requests = max_requests
        self.request_count = 0
        self.is_blocked = False
        self.session = requests.Session()
        ua = f"MusicVault/1.0 ({contact})" if contact else "MusicVault/1.0"
        self.session.headers.update({"User-Agent": ua})

    def can_request(self) -> bool:
        """Check if provider is available and under budget."""
        return not self.is_blocked and self.request_count < self.max_requests

    def request(self, url: str, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, timeout: int = 25) -> Optional[Dict[str, Any]]:
        """
        Execute rate-limited HTTP GET request with retries and exponential backoff.
        """
        if not self.can_request():
            logger.warning(f"Provider {self.name} reached limit or is blocked.")
            return None

        merged_headers = dict(self.session.headers)
        if headers:
            merged_headers.update(headers)

        for attempt in range(3):
            time.sleep(self.default_delay)
            self.request_count += 1
            try:
                resp = self.session.get(url, params=params, headers=merged_headers, timeout=timeout)
                if resp.status_code in (401, 403):
                    logger.warning(f"Provider {self.name} authentication or permission denied (HTTP {resp.status_code}).")
                    self.is_blocked = True
                    return None
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5")) if resp.headers.get("Retry-After", "").isdigit() else 5.0
                    logger.warning(f"Provider {self.name} rate-limited. Backing off for {retry_after}s.")
                    time.sleep(retry_after)
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    if attempt < 2:
                        backoff = 3.0 * (2 ** attempt)
                        time.sleep(backoff)
                        continue
                    self.is_blocked = True
                    return None

                if resp.ok:
                    try:
                        return resp.json()
                    except ValueError:
                        return None
                else:
                    logger.debug(f"{self.name} HTTP {resp.status_code}")
                    return None
            except requests.RequestException as e:
                logger.debug(f"{self.name} connection error: {e}")
                if attempt < 2:
                    time.sleep(2.0 * (2 ** attempt))
                    continue
                return None
        return None
