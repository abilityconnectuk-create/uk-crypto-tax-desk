# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Page models for the tax-review screens. The figures stay unconfirmed."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from .allocation_desk import links, list_lots, negligible_claims
from .disposal_match import from_report
from .errors import DeskError
from .futures import build_report, load_toggle
from .income_schedule import build_schedule
from .koinly_check import load_uploads
from .koinly_reconcile import load_tolerance, page_bits
from .missing_cost_report import build_report as missing_report
from .missing_cost_report import effect_total, in_window
from .queues import gaps, transfers
from .rules import load_sa108
from .spam_filter import candidates, read_log
from .valuation import annotate_rows, record_valuations


def _report(desk: Any, year: int) -> Dict[str, Any]:
    if not desk.all_rows():
        raise DeskError("Import a file before running this year.")
    return desk.report(year)


def income_page(desk: Any, year: int) -> Dict[str, Any]:
    try:
        report = _report(desk, year)
    except DeskError as exc:
        return {"error": str(exc), "schedule": build_schedule(None, [], year)}
    rows = [row for row in desk.all_rows() if _in_report(row, report)]
    return {"error": "", "schedule": build_schedule(report, rows, year)}


def futures_page(desk: Any, year: int) -> Dict[str, Any]:
    capital = Decimal(0)
    start, end = "", ""
    try:
        loaded: Optional[Dict[str, Any]] = _report(desk, year)
    except DeskError:
        loaded = None
    if loaded is not None:
        capital = Decimal(str((loaded.get("summary") or {}).get("gain") or "0"))
        start, end = str(loaded.get("start") or ""), str(loaded.get("end") or "")
    rows = [row for row in desk.all_rows() if _between(row, start, end)]
    return {
        "report": build_report(rows, capital, load_toggle(desk.root)),
        "year": year,
    }


def missing_page(desk: Any, year: int) -> Dict[str, Any]:
    try:
        report = _report(desk, year)
    except DeskError as exc:
        return {"error": str(exc), "missing": missing_report([])}
    return {
        "error": "",
        "missing": missing_report(report.get("disposals") or [], _gaps(report)),
    }


def koinly_page(desk: Any, year: int) -> Dict[str, Any]:
    summary, complete = load_uploads(desk.root, year)
    tolerance = load_tolerance(desk.root)
    try:
        report = _report(desk, year)
    except DeskError:
        report = None
    effect = Decimal(0) if report is None else effect_total(_gaps(report))
    bits = page_bits(report, summary, complete, tolerance, effect, Decimal(0))
    spec = load_sa108(year) or {}
    definition = ""
    for box in spec.get("boxes") or []:
        if isinstance(box, dict) and str(box.get("box")) == "13.1":
            definition = str(box.get("note") or "").strip()
    bits["definition"] = definition
    bits["tolerance"] = f"{tolerance}"
    bits["overrides"] = len(desk.change_log())
    return bits


def match_lines(desk: Any, year: int) -> Dict[str, Any]:
    try:
        report = _report(desk, year)
    except DeskError as exc:
        return {"error": str(exc), "matching": from_report(None)}
    return {"error": "", "matching": from_report(report)}


def spam_page(desk: Any) -> Dict[str, Any]:
    rows = desk.all_rows()
    return {"candidates": candidates(rows, []), "log": read_log(desk.root)}


def queue_page(desk: Any, year: int) -> Dict[str, Any]:
    try:
        review = desk.review(year) if desk.all_rows() else {"needs_review": [], "queue": []}
    except DeskError:
        review = {"needs_review": [], "queue": []}
    return {"transfers": transfers(review, _notes(desk)), "gaps": gaps(desk.root)}


def allocation_page(desk: Any) -> Dict[str, Any]:
    path = str(desk.records_path) if desk.records_path.is_file() else ""
    return {
        "lots": list_lots(path),
        "links": links(desk.root),
        "claims": negligible_claims(desk.root),
    }


def valuation_rows(desk: Any) -> Dict[str, Any]:
    rows = annotate_rows(desk.all_rows())
    record_valuations(desk.root, rows)
    return {"rows": rows}


def _gaps(report: Dict[str, Any]) -> List[Mapping[str, Any]]:
    return in_window(
        report.get("missing_cost") or [],
        str(report.get("start") or ""),
        str(report.get("end") or ""),
    )


def _notes(desk: Any) -> Dict[str, Dict[str, str]]:
    path = desk.root / "transfer-notes.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items() if isinstance(value, dict)}


def _in_report(row: Dict[str, Any], report: Dict[str, Any]) -> bool:
    return _between(row, str(report.get("start") or ""), str(report.get("end") or ""))


def _between(row: Dict[str, Any], start: str, end: str) -> bool:
    if not start or not end:
        return True
    day = str(row.get("date") or "")[:10]
    return bool(day) and start <= day <= end
