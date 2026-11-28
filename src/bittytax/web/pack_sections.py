# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Extra accountant-pack files. Every tax figure in them is still unconfirmed."""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .disposal_match import from_report
from .errors import DeskError
from .filing import readiness
from .income_schedule import build_schedule
from .koinly_check import load_uploads
from .koinly_reconcile import load_tolerance, page_bits
from .missing_cost_report import build_report, effect_total, in_window
from .queues import export_rows
from .rules import accountant_review_items, load_rules
from .valuation import MANUAL as VALUE_MANUAL

_BANNER = "AWAITING TAX REVIEW"


def extra_files(desk: Any, tax_year: int, context: Mapping[str, Any]) -> Dict[str, str]:
    """Named sections the accountant pack adds to the zip."""
    report = context["report"]
    sa108 = context.get("sa108")
    losses = context.get("losses") or {}
    rows = list(context.get("transactions") or [])
    gaps = in_window(
        report.get("missing_cost") or [],
        str(report.get("start") or ""),
        str(report.get("end") or ""),
    )
    missing = build_report(report.get("disposals") or [], gaps)
    income = build_schedule(report, _year_rows(rows, report), tax_year)
    summary, complete = load_uploads(desk.root, tax_year)
    effect = effect_total(gaps)
    recon = page_bits(report, summary, complete, load_tolerance(desk.root), effect, Decimal(0))
    review: Dict[str, Any] = {"needs_review": [], "queue": []}
    try:
        review = desk.review(tax_year)
    except DeskError:
        review = {"needs_review": [], "queue": []}
    return {
        "cover.txt": _cover(sa108, report, losses, tax_year, desk),
        "method-statement.txt": method_statement(tax_year),
        "disposal-schedule.csv": _csv(from_report(report)["rows"]),
        "pool-snapshots.csv": _pools(report.get("pools") or {}),
        "income-schedule.csv": _csv(income["rows"]),
        "koinly-reconciliation.csv": _recon_csv(recon.get("reconciliation")),
        "missing-cost.csv": _csv(missing["rows"]),
        "known-gaps.csv": _csv(export_rows(desk.root, review)),
        "open-questions.txt": _questions(tax_year, desk),
        "box-53-note.txt": _box53(sa108),
    }


def method_statement(tax_year: int) -> str:
    """Valuation, pooling, each DeFi rule, and the futures treatment."""
    rules = load_rules(tax_year) or {}
    valuation = rules.get("valuation") or {}
    lines = [
        "Method statement",
        _BANNER,
        (
            f"Valuation: {valuation.get('method', '')} "
            f"Manual: {valuation.get('manual') or VALUE_MANUAL}."
        ),
        (
            "Each event is valued in GBP at the time it happens. "
            "A USD total is not converted at one year-end rate."
        ),
        (
            "Pooling: one section 104 pool for each asset. "
            "Stablecoins, including USDT, USDC, USDF and RLUSD, are not merged."
        ),
        "Matching: same day, then the next 30 days, then the section 104 pool (CRYPTO22200).",
        (
            "The 30-day window crosses 5 April into the next tax year. "
            "The gain stays in the year of the disposal."
        ),
        (
            "Fees: an acquisition fee is added to cost (section 38(1)(a)). "
            "A disposal fee is an allowable cost (section 38(1)(c)) and goes in box 13.3. "
            "Box 13.2 stays gross. Swap fees are split in half."
        ),
        "Gas paid in a token is a disposal of that token, on that token's own pool.",
        (
            "Futures and perpetuals on a non-recognised exchange are miscellaneous income "
            "on SA100 page TR3, boxes 17 and 18. They are not SA108 crypto boxes. "
            "A loss only reduces later miscellaneous income of the same type."
        ),
        "",
        "DeFi rules",
    ]
    for tag in (rules.get("defi") or {}).get("tags") or []:
        if not isinstance(tag, dict):
            continue
        note = " ".join(str(tag.get("note") or "").split())
        confirmed = "true" if tag.get("confirmed") else "false"
        banner = "" if tag.get("confirmed") else f" {_BANNER}."
        lines.append(
            f"{tag.get('label', tag.get('id', ''))}: {note} "
            f"Manual: {tag.get('manual', '')}. Confirmed: {confirmed}.{banner}"
        )
    return "\n".join(lines) + "\n"


def _cover(
    sa108: Optional[Mapping[str, Any]],
    report: Mapping[str, Any],
    losses: Mapping[str, str],
    tax_year: int,
    desk: Any,
) -> str:
    lines = [
        "SA108 cover",
        _BANNER,
        f"Ready to file: {readiness(desk, tax_year)['status']}.",
        "Boxes 13.1 to 13.8, and the inputs for boxes 45, 47 and 51.",
    ]
    summary = report.get("summary") or {}
    if sa108:
        for box in sa108.get("boxes") or []:
            number = str(box.get("box") or "")
            if number not in {
                "13.1",
                "13.2",
                "13.3",
                "13.4",
                "13.5",
                "13.6",
                "13.7",
                "13.8",
                "45",
                "47",
                "51",
                "53",
            }:
                continue
            value = box.get("value") or "Accountant review"
            if number == "45" and losses.get("brought_used"):
                value = losses["brought_used"]
            if number == "47" and losses.get("carried"):
                value = losses["carried"]
            lines.append(f"Box {number} {box.get('label', '')}: {value}")
    lines.append("Box 51 inputs, not a calculated tax amount:")
    lines.append(f"Gains before the split: {summary.get('gain_before', '')}")
    lines.append(f"Gains on or after the split: {summary.get('gain_after', '')}")
    lines.append(f"Split date: {summary.get('split_date', '') or 'none'}")
    _cover_person(lines, desk)
    return "\n".join(lines) + "\n"


def _cover_person(lines: List[str], desk: Any) -> None:
    """Name and taxpayer reference from the local settings. Blank fields are omitted."""
    from .desk_settings import load_settings  # pylint: disable=import-outside-toplevel

    settings = load_settings(desk.root)
    if settings.get("name"):
        lines.append(f"Name: {settings['name']}")
    if settings.get("utr"):
        lines.append(f"UTR: {settings['utr']}")


def _questions(tax_year: int, desk: Any) -> str:
    lines = ["Open questions for the accountant", _BANNER]
    for item in accountant_review_items(tax_year):
        lines.append(f"{item.get('title', '')}: {item.get('detail', '')}")
    path = desk.root / "negligible-claims.json"
    if path.is_file() and path.stat().st_size > 2:
        lines.append("Negligible-value claims post a deemed disposal of the whole pool.")
    return "\n".join(lines) + "\n"


def _box53(sa108: Optional[Mapping[str, Any]]) -> str:
    note = (
        "Box 53 is X when any event is valued from a price source rather than "
        "actual sterling consideration. That includes every token-to-token swap. "
        "The method statement is box 54."
    )
    flag = ""
    if sa108:
        for box in sa108.get("boxes") or []:
            if str(box.get("box")) == "53":
                flag = str(box.get("value") or "")
                if box.get("note"):
                    note = str(box["note"]).strip()
    shown = flag or "blank"
    return f"Box 53 estimates and valuations: {shown}.\n{note}\nThe method statement is box 54.\n"


def _pools(pools: Mapping[str, Any]) -> str:
    rows: List[Dict[str, str]] = []
    for section in ("start", "end"):
        for row in pools.get(section) or []:
            packed = dict(row)
            packed["snapshot"] = section
            rows.append(packed)
    return _csv(rows)


def _recon_csv(reconciliation: Optional[Mapping[str, Any]]) -> str:
    if not reconciliation:
        return _csv([{"metric": "none", "note": "No Koinly file imported for this year."}])
    rows = []
    for row in reconciliation.get("rows") or []:
        causes = ", ".join(item.get("cause", "") for item in row.get("causes") or [])
        rows.append(
            {
                "metric": row.get("metric", ""),
                "desk": row.get("desk", ""),
                "koinly": row.get("koinly", ""),
                "difference": row.get("difference", ""),
                "causes": causes,
                "unexplained": "yes" if row.get("unexplained") else "no",
            }
        )
    return _csv(rows)


def _year_rows(
    rows: Sequence[Mapping[str, Any]], report: Mapping[str, Any]
) -> List[Mapping[str, Any]]:
    start = str(report.get("start") or "")
    end = str(report.get("end") or "")
    chosen = []
    for row in rows:
        day = str(row.get("date") or "")[:10]
        if start and end and day and not start <= day <= end:
            continue
        chosen.append(row)
    return chosen


def _csv(rows: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if not rows:
        writer.writerow(["none"])
        return buffer.getvalue()
    keys: List[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(str(key))
    writer.writerow(keys)
    for row in rows:
        writer.writerow([row.get(key, "") for key in keys])
    return buffer.getvalue()
