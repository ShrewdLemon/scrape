"""Polite HTTP client for the SEBI PMR portal.

Rate limiting
-------------
``https://www.sebi.gov.in/robots.txt`` allows this path and publishes **no**
``Crawl-delay``, so the 2.5-4.0 s spacing used here is a self-imposed
politeness budget rather than a site requirement.  Every request is separated
by a uniform random delay in that window; HTTP 429/503 widens the window for
the rest of the run (it never narrows again automatically), which keeps a long
scrape from hammering the portal if it starts pushing back.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

BASE = "https://www.sebi.gov.in/sebiweb/other/OtherAction.do"
LANDING = BASE + "?doPmr=yes"
EXPORT = BASE + "?doPmrExcel=yes"

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 sebi-pmr-scraper/1.0"
)

_OPTION = re.compile(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', re.S)
_SELECT_TMPL = r'<select[^>]*name="{}"[^>]*>(.*?)</select>'


@dataclass(frozen=True)
class PortfolioManager:
    """One entry of the portal's ``pmrId`` dropdown."""

    pmr_id: str       # the raw "INP...@@INP...@@NAME" option value the form needs
    reg_no: str
    name: str

    @property
    def slug(self) -> str:
        return self.reg_no or re.sub(r"\W+", "_", self.name)[:40]


class RateLimiter:
    """Uniform random spacing between requests, with a one-way penalty ratchet."""

    def __init__(self, min_delay: float = 2.5, max_delay: float = 4.0):
        if min_delay <= 0 or max_delay < min_delay:
            raise ValueError("need 0 < min_delay <= max_delay")
        self.min_delay = float(min_delay)
        self.max_delay = float(max_delay)
        self._last = 0.0

    def wait(self) -> None:
        target = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < target:
            time.sleep(target - elapsed)
        self._last = time.monotonic()

    def penalise(self, factor: float = 1.5, ceiling: float = 60.0) -> None:
        """Widen the delay window after the server pushes back."""
        self.min_delay = min(self.min_delay * factor, ceiling)
        self.max_delay = min(max(self.max_delay * factor, self.min_delay + 0.5), ceiling * 1.5)
        log.warning("rate limit widened to %.1f-%.1fs", self.min_delay, self.max_delay)


class PmrClient:
    """Session-backed client for the portfolio-manager monthly reports."""

    def __init__(self, min_delay: float = 2.5, max_delay: float = 4.0,
                 timeout: float = 60.0, retries: int = 4, user_agent: str = DEFAULT_UA):
        self.limiter = RateLimiter(min_delay, max_delay)
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        self._warm = False

    # ------------------------------------------------------------------ core
    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """One rate-limited request with retry/backoff on 429, 5xx and network errors."""
        last_exc = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt * 3, 90)
                log.warning("%s %s failed (%s); retry %d in %ds",
                            method, url, exc.__class__.__name__, attempt + 1, wait)
                time.sleep(wait)
                continue

            if resp.status_code in (429, 503):
                self.limiter.penalise()
                wait = min(2 ** attempt * 5, 120)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, min(int(retry_after), 300))
                log.warning("HTTP %d from portal; backing off %ds", resp.status_code, wait)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = min(2 ** attempt * 4, 90)
                log.warning("HTTP %d; retry %d in %ds", resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        raise RuntimeError(f"{method} {url} failed after {self.retries + 1} attempts") from last_exc

    def warm(self) -> str:
        """Fetch the landing page so the session carries a JSESSIONID."""
        html = self._request("GET", LANDING).text
        self._warm = True
        return html

    # ------------------------------------------------------------- catalogue
    def portfolio_managers(self, html: str | None = None) -> list[PortfolioManager]:
        """Every manager in the ``pmrId`` dropdown."""
        html = html if html is not None else self.warm()
        m = re.search(_SELECT_TMPL.format("pmrId"), html, re.S)
        if not m:
            raise RuntimeError("pmrId dropdown not found - portal layout changed")
        out = []
        for value, label in _OPTION.findall(m.group(1)):
            if not value.strip():
                continue
            parts = value.split("@@")
            reg = parts[0].strip() if parts else ""
            name = (parts[-1] if len(parts) > 1 else label).strip()
            out.append(PortfolioManager(pmr_id=value, reg_no=reg, name=re.sub(r"\s+", " ", name)))
        return out

    def available_years(self, html: str | None = None) -> list[int]:
        html = html if html is not None else self.warm()
        m = re.search(_SELECT_TMPL.format("year"), html, re.S)
        if not m:
            return []
        return sorted(int(v) for v, _ in _OPTION.findall(m.group(1)) if v.strip().isdigit())

    # ---------------------------------------------------------------- report
    def report_html(self, pm: PortfolioManager, year: int, month: int, method: str = "post") -> str:
        """The rendered PMR page for one manager-month."""
        if not self._warm:
            self.warm()
        payload = {"pmrId": pm.pmr_id, "year": str(year), "month": str(month),
                   "currdate": "", "format": ""}
        headers = {"Referer": LANDING}
        if method.lower() == "get":
            return self._request("GET", LANDING, params=payload, headers=headers).text
        return self._request("POST", LANDING, data=payload, headers=headers).text

    def export_bytes(self, pm: PortfolioManager | None, year: int, month: int,
                     fmt: str = "xml", method: str = "post") -> tuple[bytes, str, int]:
        """Raw bytes from the portal's Excel/XML export, its content-type and status.

        The page's own ``getPMRExcel``/``getPMRXml`` validators check only year
        and month, so ``pm=None`` probes whether the export will return a whole
        month in one call.  Unlike the report fetch this does not retry on an
        error status - a probe wants to see the failure, not paper over it.
        """
        if not self._warm:
            self.warm()
        payload = {"pmrId": pm.pmr_id if pm else "", "year": str(year),
                   "month": str(month), "currdate": "", "format": fmt}
        headers = {"Referer": LANDING}
        self.limiter.wait()
        if method.lower() == "get":
            resp = self.session.get(EXPORT, params=payload, headers=headers, timeout=self.timeout)
        else:
            resp = self.session.post(EXPORT, data=payload, headers=headers, timeout=self.timeout)
        return resp.content, resp.headers.get("Content-Type", ""), resp.status_code
