# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""One tax year's report pack. PDF when an optional library is installed."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .rules import accountant_review_items
from .sa108 import sa108_view

Renderer = Callable[[Sequence[str]], bytes]


def pack_context(desk: Any, tax_year: int) -> Dict[str, Any]:
    """Everything the printable pack and the PDF use."""
    report = desk.report(tax_year)
    return {
        "tax_year": tax_year,
        "report": report,
        "sa108": sa108_view(report, tax_year),
        "health": desk.health(tax_year),
        "accountant_review": accountant_review_items(tax_year),
        "transactions": _all_rows(desk),
        "pdf_note": "" if pdf_library_available() else _PDF_NOTE,
    }


def pack_lines(context: Mapping[str, Any]) -> List[str]:
    """Plain lines for the optional PDF. The HTML page shows the same sections."""
    report = context["report"]
    summary = report["summary"]
    lines = [
        f"BittyTax report pack {report.get('label', context['tax_year'])}",
        "This is the calculator's output. It is not tax advice.",
        "Summary",
        f"Disposals: {summary.get('disposals', '')}",
        f"Disposal proceeds: {summary.get('proceeds', '')}",
        f"Allowable costs: {summary.get('allowable_costs', '')}",
        f"Gains before losses: {summary.get('total_gain', '')}",
        f"Losses: {summary.get('losses', '')}",
        "SA108",
    ]
    sa108 = context.get("sa108")
    if sa108:
        if sa108.get("banner"):
            lines.append(str(sa108["banner"]))
        for box in sa108.get("boxes", []):
            shown = box["value"] or "Accountant review"
            lines.append(f"Box {box['box']} {box['label']}: {shown}")
        lines.append(f"Annual exempt amount: {sa108['allowance']['value']}")
    else:
        lines.append("No SA108 mapping for this year.")
    if summary.get("split_date"):
        lines.append("Box 51 inputs")
        lines.append(
            f"Gains before {summary.get('split_date_label', '')}: {summary.get('gain_before', '')}"
        )
        lines.append(
            "Gains on or after "
            f"{summary.get('split_date_label', '')}: {summary.get('gain_after', '')}"
        )
    lines.append("Disposals")
    for row in report.get("disposals", []):
        lines.append(
            f"{row.get('date', '')} {row.get('asset', '')} "
            f"qty {row.get('quantity', '')} gain {row.get('gain', '')} "
            f"rule {row.get('matching_rule', row.get('disposal_type', ''))}"
        )
    lines.append("Income")
    income = report.get("income") or {}
    lines.append(f"Income total: {income.get('amount', '')}")
    for row in income.get("rows", []):
        lines.append(f"{row.get('date', '')} {row.get('asset', '')} {row.get('amount', '')}")
    lines.append("Transactions")
    for row in context.get("transactions", []):
        lines.append(
            f"{row.get('date', '')} {row.get('type', '')} "
            f"{', '.join(row.get('assets') or [])} {row.get('wallet', '')}"
        )
    lines.append("Data health")
    health = context.get("health") or {}
    if not health.get("issues"):
        lines.append("No data-health warnings.")
    for issue in health.get("issues", []):
        lines.append(f"{issue.get('category', '')}: {issue.get('summary', '')}")
    lines.append("Accountant review")
    lines.append("AWAITING TAX REVIEW")
    reviews = context.get("accountant_review") or []
    if not reviews:
        lines.append("No accountant-review items for this year.")
    for item in reviews:
        lines.append(f"{item.get('title', '')}: {item.get('detail', '')}")
    return lines


def render_pdf(lines: Sequence[str], renderer: Optional[Renderer] = None) -> Optional[bytes]:
    """PDF bytes, or None when no PDF library is installed."""
    chosen = _load_fpdf if renderer is None else renderer
    if renderer is None and not pdf_library_available():
        return None
    return chosen(lines)


def pdf_library_available() -> bool:
    try:
        __import__("fpdf")
    except ImportError:
        return False
    return True


_PDF_NOTE = (
    "The optional PDF library is not installed. "
    "Print this page from the browser, or install the pdf extra."
)


def _all_rows(desk: Any) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    page = 1
    while True:
        listing = desk.transactions({}, page)
        rows.extend(listing["rows"])
        if page >= int(listing["pages"]):
            return rows
        page += 1


def _load_fpdf(lines: Sequence[str]) -> bytes:
    # Optional extra. Missing on a normal install, so the import stays local.
    from fpdf import FPDF  # pylint: disable=import-error,import-outside-toplevel

    document = FPDF()
    document.set_auto_page_break(auto=True, margin=15)
    document.add_page()
    document.set_font("Helvetica", size=10)
    for line in lines:
        document.multi_cell(0, 5, _latin(line))
    output = document.output()
    if isinstance(output, str):
        return output.encode("latin-1")
    return bytes(output)


def _latin(text: str) -> str:
    return text.encode("latin-1", errors="replace").decode("latin-1")
