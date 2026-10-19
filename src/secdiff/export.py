"""Export of the XBRL numbers table (CSV + Excel-readable output).

The exports carry EXACTLY the values shown in the report's numbers
table: every cell is produced by the same formatting the report itself
uses (`xbrl.fmt_money` and the same percentage format), so a download
can never disagree with the page it came from. No values are computed,
rounded or filled in here — formatting only.

- CSV: plain stdlib `csv`.
- XLS: SpreadsheetML 2003 XML (the XML spreadsheet format Excel and
  LibreOffice open natively), which the standard library can produce
  without any third-party dependency.
"""
from __future__ import annotations

import csv
import io
from xml.sax.saxutils import escape

from .xbrl import XbrlResult, fmt_money

HEADER = ["Item", "Older filing", "Newer filing", "Change", "%"]


def table_rows(xbrl: XbrlResult) -> list[list[str]]:
    """The numbers table as display strings: header + one row per
    changed concept, identical to the rendered report table."""
    rows = [list(HEADER)]
    for r in xbrl.rows:
        pct = f"{r.pct:+.1f}%" if r.pct is not None else "—"
        rows.append([r.label, fmt_money(r.old, r.unit),
                     fmt_money(r.new, r.unit),
                     fmt_money(r.change, r.unit), pct])
    return rows


def xbrl_to_csv(xbrl: XbrlResult) -> str:
    """The numbers table as CSV text (header row always present, even
    when the table is empty — an honest empty export, never fake data).
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(table_rows(xbrl))
    return buf.getvalue()


def xbrl_to_xls(xbrl: XbrlResult) -> str:
    """The numbers table as SpreadsheetML XML (opens as .xls in Excel).

    Cells are written as strings on purpose: the values are the exact
    display strings from the report table (e.g. "$394.33B"), and Excel
    must show them unchanged rather than reinterpret them.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?mso-application progid="Excel.Sheet"?>',
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
        '<Worksheet ss:Name="XBRL numbers"><Table>',
    ]
    for row in table_rows(xbrl):
        parts.append("<Row>" + "".join(
            f'<Cell><Data ss:Type="String">{escape(cell)}</Data></Cell>'
            for cell in row) + "</Row>")
    parts.append("</Table></Worksheet></Workbook>")
    return "\n".join(parts) + "\n"
