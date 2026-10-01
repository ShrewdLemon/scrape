"""Client for NSDL's "Archive (Trends in FPI/FII Investments)" daily report.

Same WebForms pattern as the year-wise report, but the only input is a
"To Date" (hidden field ``hdnDate``, ``dd-Mon-yyyy``) submitted via the
``btnSubmit1`` postback. The answer is every reporting day *of that month* up
to the date, plus month/year totals - so querying each month-end returns a
whole month, and Jan 2001 to Jan 2020 is 229 requests rather than ~4,800.
"""
from __future__ import annotations

import calendar
from datetime import date

from ..fetch import NsdlClient, hidden_fields

URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def months(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Inclusive (year, month) range."""
    (y, m), out = start, []
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class ArchiveClient(NsdlClient):
    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0, **kw):
        super().__init__(min_delay, max_delay, url=URL, **kw)

    def upto(self, day: date) -> str:
        """HTML of the report for ``day``'s month, up to and including ``day``."""
        form = hidden_fields(self.landing())
        form.update({"__EVENTTARGET": "btnSubmit1", "__EVENTARGUMENT": "",
                     "hdnDate": day.strftime("%d-%b-%Y"), "hdnFlag": "",
                     "HdnValexceldata": ""})
        html = self._request("POST", data=form)
        self._page = html
        return html

    def month(self, year: int, month: int) -> str:
        return self.upto(month_end(year, month))
