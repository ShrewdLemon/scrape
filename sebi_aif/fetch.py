"""Fetch SEBI's AIF statistics page.

Every quarter is rendered inline on one static page, so a full scrape is a
single GET.  Retries back off on network errors, 429 and 5xx; any other error
status fails straight away.
"""
from __future__ import annotations

import logging
import time

import requests

from .parse import URL

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 sebi-aif-scraper/1.0"
)


def fetch_page(url: str = URL, timeout: float = 60.0, retries: int = 4,
               user_agent: str = DEFAULT_UA) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            wait = min(2 ** attempt * 3, 90)
            log.warning("GET %s failed (%s); retry %d in %ds",
                        url, exc.__class__.__name__, attempt + 1, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = min(2 ** attempt * 5, 120)
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, min(int(retry_after), 300))
            log.warning("HTTP %d from sebi.gov.in; retry %d in %ds",
                        resp.status_code, attempt + 1, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        if "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = "utf-8"          # requests would otherwise assume ISO-8859-1
        return resp.text
    raise RuntimeError(f"GET {url} failed after {retries + 1} attempts") from last_exc
