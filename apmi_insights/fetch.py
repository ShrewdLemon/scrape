"""Polite client for the APMI Insights investment-approach Server Action.

The site publishes no robots.txt (it 404s), so the 1.0-2.0 s spacing between
requests is a self-imposed budget; 429/5xx responses widen it for the rest of
the run, exactly as in :mod:`sebi_pmr.fetch`.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from sebi_pmr.fetch import DEFAULT_UA, RateLimiter

from . import PERIODS

log = logging.getLogger(__name__)

BASE = "https://insights.apmiindia.org"
ACTION_NAME = "fetchInvestmentApproach"
UNDEFINED = "$undefined"   # how React serialises ``undefined`` action arguments

_PAGE_CHUNK = re.compile(r'src="(/_next/static/chunks/app/investment-approaches/[^"]+\.js)"')
_ACTION_REF = re.compile(
    r'createServerReference\)\("([0-9a-f]{20,})"[^)]*?"' + ACTION_NAME + r'"\)')


class FetchError(RuntimeError):
    pass


def page_url(tab: int) -> str:
    return f"{BASE}/investment-approaches/{tab}"


def find_action_id(js: str) -> str:
    """Pull the ``fetchInvestmentApproach`` action id out of a page chunk."""
    m = _ACTION_REF.search(js)
    if not m:
        raise FetchError(f"no {ACTION_NAME} server reference in the page chunk")
    return m.group(1)


def parse_action_response(text: str) -> list[dict]:
    """Decode the React Server Components stream an action call returns.

    Line ``0:`` is the envelope (``{"a":"$@1",...}``) and line ``1:`` holds the
    action's return value - here a JSON list of rows.
    """
    for line in text.splitlines():
        if line.startswith("1:"):
            rows = json.loads(line[2:])
            if not isinstance(rows, list):
                raise FetchError(f"expected a list, got {type(rows).__name__}")
            return rows
    raise FetchError("no result line in action response: " + text[:200])


class ApmiClient:
    def __init__(self, tab: int = 0, min_delay: float = 1.0, max_delay: float = 2.0,
                 timeout: float = 60.0, retries: int = 4, user_agent: str = DEFAULT_UA):
        self.tab = tab
        self.url = page_url(tab)
        self.limiter = RateLimiter(min_delay, max_delay)
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.action_id: str | None = None
        self.requests_made = 0

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        for attempt in range(1, self.retries + 1):
            self.limiter.wait()
            self.requests_made += 1
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                log.warning("%s %s failed (%s), attempt %d/%d", method, url, exc,
                            attempt, self.retries)
            else:
                if r.status_code == 200:
                    return r
                log.warning("%s %s -> HTTP %d, attempt %d/%d", method, url, r.status_code,
                            attempt, self.retries)
                if r.status_code in (429, 503):
                    self.limiter.penalise()
                elif r.status_code < 500:
                    break
            time.sleep(2 ** attempt)
        raise FetchError(f"{method} {url} failed after {attempt} attempt(s)")

    def discover(self) -> str:
        """Locate the page's JS chunk and read the current action id from it."""
        html = self._request("GET", self.url).text
        m = _PAGE_CHUNK.search(html)
        if not m:
            raise FetchError("investment-approaches page chunk not found in HTML")
        self.action_id = find_action_id(self._request("GET", BASE + m.group(1)).text)
        log.info("action id %s", self.action_id)
        return self.action_id

    def page(self, page: int, page_size: int = 10, period: str = "SI") -> list[dict]:
        if self.action_id is None:
            self.discover()
        body = json.dumps([self.tab, PERIODS[period], UNDEFINED, UNDEFINED, UNDEFINED,
                           page, page_size])
        r = self._request("POST", self.url, data=body, headers={
            "Next-Action": self.action_id,
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
        })
        return parse_action_response(r.text)

    def all_pages(self, page_size: int = 10, period: str = "SI",
                  max_pages: int = 10_000) -> tuple[list[dict], int]:
        """Walk Next until an empty page; returns (rows, non-empty pages)."""
        rows: list[dict] = []
        for n in range(1, max_pages + 1):
            batch = self.page(n, page_size, period)
            if not batch:
                return rows, n - 1
            rows.extend(batch)
            if n % 20 == 0:
                log.info("page %d: %d rows so far", n, len(rows))
            if len(batch) < page_size:
                return rows, n
        raise FetchError(f"still getting rows after {max_pages} pages")

    def total(self, period: str = "SI", ceiling: int = 100_000) -> int:
        """Independent count: ask for everything in one oversized page."""
        return len(self.page(1, ceiling, period))
