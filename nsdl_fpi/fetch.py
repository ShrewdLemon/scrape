"""Client for NSDL's "Monthly FPI Net Investments" year-wise report.

The report is a classic ASP.NET WebForms page: the year dropdown (``ddl``) and
the currency switch (``hdnCurr``) are both ``__doPostBack`` round-trips that
carry the page's ``__VIEWSTATE``/``__EVENTVALIDATION``. So one year in one
currency is exactly one POST, and the whole 2002-to-date history is ~25
requests per currency.

The server drops connections from non-browser user agents (an empty reply),
hence the browser-like UA.
"""
from __future__ import annotations

import logging
import random
import re
import time

import requests

log = logging.getLogger(__name__)

URL = "https://www.fpi.nsdl.co.in/web/Reports/Yearwise.aspx?RptType=6"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CURRENCIES = ("INR", "USD")

_HIDDEN = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.I)
_YEARS = re.compile(r'<select[^>]*name="ddl"[^>]*>(.*?)</select>', re.S | re.I)
_OPTION = re.compile(r'<option[^>]*value="(\d{4})"', re.I)


def hidden_fields(html: str) -> dict[str, str]:
    return dict(_HIDDEN.findall(html))


def available_years(html: str) -> list[int]:
    m = _YEARS.search(html)
    if not m:
        raise ValueError("year dropdown (ddl) not found - has the page layout changed?")
    return sorted(int(y) for y in _OPTION.findall(m.group(1)))


class NsdlClient:
    """Keeps the WebForms state between postbacks, with polite spacing and retries."""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0,
                 retries: int = 4, timeout: float = 60.0, url: str = URL):
        self.url = url
        self.session = requests.Session()
        self.session.headers["User-Agent"] = DEFAULT_UA
        self.min_delay, self.max_delay = min_delay, max_delay
        self.retries, self.timeout = retries, timeout
        self._last = 0.0
        self._page: str | None = None

    def _wait(self) -> None:
        gap = random.uniform(self.min_delay, self.max_delay)
        sleep = self._last + gap - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        self._last = time.monotonic()

    def _request(self, method: str, expect: str = "tbls01", **kw) -> str:
        for attempt in range(1, self.retries + 1):
            self._wait()
            try:
                r = self.session.request(method, self.url, timeout=self.timeout, **kw)
                if r.status_code == 200 and expect in r.text:
                    return r.text
                log.warning("attempt %d: HTTP %s, %d bytes", attempt, r.status_code, len(r.text))
            except requests.RequestException as exc:
                log.warning("attempt %d: %s", attempt, exc)
            time.sleep(2 ** attempt)
            self._page = None  # stale viewstate is a common cause; start over
        raise RuntimeError(f"giving up after {self.retries} attempts")

    def landing(self) -> str:
        if self._page is None:
            self._page = self._request("GET", expect="__VIEWSTATE")
        return self._page

    def year(self, year: int, currency: str = "INR") -> str:
        """HTML of the report for one calendar year in one currency."""
        if currency not in CURRENCIES:
            raise ValueError(f"currency must be one of {CURRENCIES}")
        form = hidden_fields(self.landing())
        form.update({"__EVENTTARGET": "ddl", "__EVENTARGUMENT": "",
                     "ddl": str(year), "hdnCurr": currency})
        html = self._request("POST", data=form)
        self._page = html
        return html
