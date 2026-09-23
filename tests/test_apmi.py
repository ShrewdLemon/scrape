"""Offline tests for the APMI Insights scraper, using a captured action response."""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from openpyxl import load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apmi_insights.excel import columns, number, to_table, write_workbook
from apmi_insights.fetch import FetchError, find_action_id, parse_action_response

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Trimmed from the live page chunk; the id is a build hash.
CHUNK = ('85087:(e,a,t)=>{"use strict";t.d(a,{i:()=>s});var r=t(75828);let s=(0,'
         'r.createServerReference)("7f075bde660035e4cdc30b8268467414a80a4acfb6",r.callServer,'
         'void 0,r.findSourceMapURL,"fetchInvestmentApproach")}')


@pytest.fixture
def rows():
    with open(os.path.join(FIX, "apmi_equity_si_action.txt"), encoding="utf-8") as fh:
        return parse_action_response(fh.read())


def test_action_id_discovery():
    assert find_action_id(CHUNK) == "7f075bde660035e4cdc30b8268467414a80a4acfb6"
    with pytest.raises(FetchError):
        find_action_id("nothing here")


def test_parse_response(rows):
    assert len(rows) == 3
    assert rows[0]["iaName"] == "Customised Portfolio Approach 14"
    assert parse_action_response('0:{"a":"$@1"}\n1:[]\n') == []


def test_number_blanks():
    assert number("90.09") == 90.09
    assert number("-150.59") == -150.59
    for blank in ("", None, "NA", "-"):
        assert number(blank) is None


def test_table_matches_site_layout(rows):
    t = to_table(rows)
    assert t[0][:7] == ["Customised Portfolio Approach 14",
                        "Trivantage Capital Management India Private Limited",
                        "D", 0.42, date(2025, 8, 13), 90.09, -0.52]
    assert t[1][2] == "ND"
    assert t[0][7].endswith("/ia-insight-report/019936d5-e4e8-72e6-b1a9-5651e7b251a7")


def test_workbook(rows, tmp_path):
    out = tmp_path / "t.xlsx"
    write_workbook(str(out), rows, [("Rows scraped", 3)])
    wb = load_workbook(out)
    ws = wb.worksheets[0]
    assert [c.value for c in ws[1]] == [h for h, _, _ in columns("SI")]
    assert ws.max_row == 4 and ws.freeze_panes == "B2" and ws.auto_filter.ref == "A1:H4"
    assert ws["F2"].value == 90.09 and ws["F2"].number_format.startswith('0.0"%"')
    assert ws["H2"].value == "View Details" and ws["H2"].hyperlink is not None
    assert wb["Run Info"]["B1"].value == 3
