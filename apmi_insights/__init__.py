"""APMI Insights scraper - the "Top ... based PMS Investment Approaches" table.

Source: <https://insights.apmiindia.org/investment-approaches/0>

How the page works
------------------
The page is a Next.js app that renders the table client-side.  The path
segment is the **asset-class tab**, not a page number::

    /investment-approaches/0  Equity      /2  Hybrid
    /investment-approaches/1  Debt        /3  Multi-Asset

Rows come from a Next.js *Server Action* named ``fetchInvestmentApproach``,
called as ``(strategy, period, aumFilter, ageFilter, serviceType, page,
pageSize)``.  The period buttons map to integer codes (``SI`` -> 10) and the
site pages through 10 rows at a time with Prev/Next buttons.  The action
returns a bare JSON list with no total count, so the end of the table is the
first empty page.

The action id is a build hash, so it is re-discovered from the page's JS chunk
on every run instead of being hard-coded.
"""
from __future__ import annotations

TABS = {0: "Equity", 1: "Debt", 2: "Hybrid", 3: "Multi-Asset"}
PERIODS = {"1M": 0, "3M": 1, "6M": 2, "1Y": 3, "2Y": 4, "3Y": 5, "5Y": 7, "SI": 10}
