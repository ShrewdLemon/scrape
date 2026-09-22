"""Introspect the portal's month-wide XML/XLS export.

The export ignores ``pmrId`` and returns the same payload for every manager,
which strongly suggests one document per month covering all of them.  Before
building an ingestion path on that, this reports what the document actually
contains: its shape, how many distinct registration numbers appear, and
whether the fields behind tables B, C, G and H are present.
"""
from __future__ import annotations

import re
from collections import Counter

REG_RE = re.compile(rb"INP\d{9}")

# Field names that would indicate the four tables we need are in the export.
WANTED_HINTS = [
    ("investment approach", (b"approach", b"invapproach", b"investmentapproach")),
    ("AUM break-up", (b"aum", b"assetunder", b"listed", b"unlisted")),
    ("inflow/outflow", (b"inflow", b"outflow", b"netinflow")),
    ("discretionary split", (b"discretionary", b"nondiscretionary", b"disc")),
]


def summarise_xml(body: bytes, max_depth: int = 4, sample_chars: int = 1600) -> str:
    """Describe an XML export without assuming any particular schema."""
    import xml.etree.ElementTree as ET

    out: list[str] = []
    text = body.lstrip(b"\xef\xbb\xbf")
    regs = set(REG_RE.findall(text))
    out.append(f"size: {len(body):,} bytes")
    out.append(f"distinct registration numbers (INP\\d{{9}}): {len(regs)}")
    if regs:
        shown = sorted(r.decode() for r in regs)[:6]
        out.append(f"  e.g. {', '.join(shown)}")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        out.append(f"XML PARSE FAILED: {exc}")
        out.append("first 600 bytes: " + repr(text[:600]))
        return "\n".join(out)

    out.append(f"root element: <{root.tag}> with {len(list(root))} direct children")

    def walk(node, depth=0):
        if depth >= max_depth:
            return
        counts = Counter(child.tag for child in node)
        for tag, n in counts.most_common(12):
            out.append(f"{'  ' * (depth + 1)}<{tag}> x{n}")
            first = next((c for c in node if c.tag == tag), None)
            if first is not None:
                walk(first, depth + 1)

    walk(root)

    # Leaf tags carry the actual field names; those are what matter.
    leaves = Counter()
    for node in root.iter():
        if len(list(node)) == 0 and node.tag:
            leaves[node.tag] += 1
    out.append(f"\ndistinct leaf field names: {len(leaves)}")
    for tag, n in leaves.most_common(40):
        out.append(f"  {tag} x{n}")

    lowered = b" ".join(t.lower().encode("utf-8", "replace") for t in leaves)
    out.append("\nfields suggesting the tables we need:")
    for label, needles in WANTED_HINTS:
        hit = [n.decode() for n in needles if n in lowered]
        out.append(f"  {label:<22} {'YES ' + str(hit) if hit else 'not obviously present'}")

    # A whole record, so the field-to-value mapping is visible.
    first_record = next((c for c in root), None)
    if first_record is not None:
        raw = ET.tostring(first_record, encoding="unicode")
        out.append(f"\nfirst record ({len(raw):,} chars, truncated):")
        out.append(raw[:sample_chars])
    return "\n".join(out)


def summarise_xls(body: bytes) -> str:
    """Describe the legacy .xls export as far as possible without extra deps."""
    out = [f"size: {len(body):,} bytes"]
    regs = set(REG_RE.findall(body))
    out.append(f"distinct registration numbers found in raw bytes: {len(regs)}")
    if regs:
        out.append("  e.g. " + ", ".join(sorted(r.decode() for r in regs)[:6]))
    try:
        import xlrd  # optional; legacy OLE2 .xls needs it
    except ImportError:
        out.append("(install xlrd to read sheet contents: pip install xlrd)")
        return "\n".join(out)
    try:
        book = xlrd.open_workbook(file_contents=body)
    except Exception as exc:
        out.append(f"xlrd could not open it: {type(exc).__name__}: {exc}")
        return "\n".join(out)
    out.append(f"sheets: {book.nsheets}")
    for sh in book.sheets():
        out.append(f"\n  sheet '{sh.name}': {sh.nrows} rows x {sh.ncols} cols")

        # The header is several merged rows deep; stacking each column's
        # non-empty header cells gives the real column name.
        header_rows = min(sh.nrows, 6)
        out.append(f"  --- column map (stacked from the first {header_rows} rows) ---")
        for c in range(sh.ncols):
            parts = []
            for r in range(header_rows):
                try:
                    v = str(sh.cell_value(r, c)).strip()
                except IndexError:
                    v = ""
                if v and v not in parts:
                    parts.append(v)
            out.append(f"   col {c:>3}: {' | '.join(parts) if parts else '(blank)'}")

        # The first row that carries a registration number is real data.
        for r in range(sh.nrows):
            row = [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
            if any(REG_RE.fullmatch(v.encode()) for v in row if v):
                out.append(f"\n  --- first data row (row {r}) ---")
                for c, v in enumerate(row):
                    if v:
                        out.append(f"   col {c:>3} = {v[:40]}")
                break
    return "\n".join(out)


def summarise(body: bytes, fmt_hint: str = "") -> str:
    head = body.lstrip(b"\xef\xbb\xbf")[:200].lower()
    if head.startswith(b"<?xml") or b"<" == head[:1] or "xml" in fmt_hint:
        return summarise_xml(body)
    if body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or "excel" in fmt_hint:
        return summarise_xls(body)
    return f"unrecognised payload, first 300 bytes: {body[:300]!r}"
