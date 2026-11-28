# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
# pylint: disable=protected-access
"""Run one tax year through the BittyTax calculator.

Price services are not contacted. A row with no local-currency value stops the
run, instead of being priced from the network. The arithmetic is the
calculator's own.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Mapping

from ..audit import AuditRecords
from ..bittytax import _do_each_tax_year, _do_import, _do_integrity_check, _do_tax
from ..bt_types import TaxRules, Year
from ..config import config
from ..exceptions import ImportFailureError
from ..holdings import Holdings
from ..price.exceptions import DataSourceError
from ..report import ReportLog
from ..tax import CalculateCapitalGains, CalculateIncome, CapitalGainsReportEstimate
from .disposal_match import matching_rule
from .errors import DeskError

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PENCE = Decimal("0.01")


class _Capture(io.StringIO):
    def isatty(self) -> bool:  # noqa: D102
        return False


@contextmanager
def _offline_prices() -> Iterator[None]:
    saved = {
        "data_source_fiat": list(config.config.get("data_source_fiat", [])),
        "data_source_crypto": list(config.config.get("data_source_crypto", [])),
        "data_source_select": dict(config.config.get("data_source_select") or {}),
    }
    config.config["data_source_fiat"] = []
    config.config["data_source_crypto"] = []
    config.config["data_source_select"] = {}
    try:
        yield
    finally:
        config.config.update(saved)


@contextmanager
def _capture_output() -> Iterator[_Capture]:
    buffer = _Capture()
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout = buffer
    sys.stderr = buffer
    try:
        yield buffer
    finally:
        sys.stdout = stdout
        sys.stderr = stderr


def run_tax_year(csv_path: str, tax_year: int) -> Dict[str, Any]:
    """Calculate ``tax_year`` from a BittyTax CSV. ``tax_year`` is the year ending 5 April."""
    if Year(tax_year) not in CalculateCapitalGains.CG_DATA_INDIVIDUAL:
        raise DeskError(f"Tax year {tax_year} is not one the calculator supports.")

    with _offline_prices():
        with _capture_output() as buffer:
            try:
                report = _run(csv_path, tax_year)
            except ImportFailureError as exc:
                raise DeskError(
                    _failure_text(buffer, "The transaction records could not be imported.")
                ) from exc
            except DataSourceError as exc:
                raise DeskError(
                    "A row has no value in the local currency. "
                    "This desk does not look up prices. Add the value, then run the report again."
                ) from exc
    report["messages"] = _ANSI.sub("", buffer.getvalue()).strip()
    return report


def _failure_text(buffer: _Capture, lead: str) -> str:
    detail = _ANSI.sub("", buffer.getvalue()).strip()
    if not detail:
        return lead
    return f"{lead}\n{detail}"


def _run(csv_path: str, tax_year: int) -> Dict[str, Any]:
    records = _do_import(csv_path)
    audit = AuditRecords(records)
    tax, _value_asset = _do_tax(records, TaxRules.UK_INDIVIDUAL, False)
    tax.process_income()
    tax.process_margin_trades()
    payload = tax.snapshot_payload(str(config.review_rows))
    integrity_ok = _do_integrity_check(audit, tax.holdings)
    _do_each_tax_year(tax, Year(tax_year), False)
    packed = tax.tax_report[Year(tax_year)]
    return _pack(
        tax_year, integrity_ok, payload, packed["CapitalGains"], packed["Income"], tax.holdings
    )


def _pack(
    tax_year: int,
    integrity_ok: bool,
    payload: Mapping[str, Any],
    gains: CalculateCapitalGains,
    income: CalculateIncome,
    holdings: Mapping[Any, Holdings],
) -> Dict[str, Any]:
    start = config.get_tax_year_start(tax_year)
    end = config.get_tax_year_end(tax_year)
    pools = payload.get("pools", {})
    previous = pools.get(str(tax_year - 1), {})
    current = pools.get(str(tax_year), {})
    years = payload.get("years", {})
    summary = _summary(gains)
    return {
        "tax_year": tax_year,
        "label": config.format_tax_year(tax_year),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "Europe/London",
        "currency": str(config.ccy),
        "integrity_ok": integrity_ok,
        "messages": "",
        "summary": summary,
        "estimate": _estimate(gains),
        "disposals": _disposals(gains),
        "income": _income(income),
        "pools": {
            "start_as_of": str(previous.get("as_of", "")),
            "end_as_of": str(current.get("as_of", "")),
            "start": list(previous.get("assets", [])),
            "end": list(current.get("assets", [])),
        },
        "missing_cost": list(payload.get("missing_cost_rows", [])),
        "snapshot_year": dict(years.get(str(tax_year), {})),
        "holdings": _pool_rows(holdings),
    }


def _pool_rows(holdings: Mapping[Any, Holdings]) -> List[Dict[str, str]]:
    """Section 104 pools after the import. No live price is requested."""
    rows = []
    for asset in sorted(holdings, key=str):
        holding = holdings[asset]
        if holding.quantity == 0:
            continue
        rows.append(
            {
                "asset": str(holding.asset),
                "quantity": _qty(holding.quantity),
                "cost": _pence(holding.cost + holding.fees),
            }
        )
    return rows


def _summary(gains: CalculateCapitalGains) -> Dict[str, Any]:
    totals = gains.totals
    summary = gains.summary
    split_date = gains.split_date
    before = ""
    after = ""
    label = ""
    if split_date is not None:
        before = _pence(gains.split_totals["gain_before"])
        after = _pence(gains.split_totals["gain_after"])
        label = ReportLog.format_date2(split_date)
    limit = summary["proceeds_limit"]
    return {
        "disposals": str(int(summary["disposals"])),
        "proceeds": _pence(totals["proceeds"]),
        "cost": _pence(totals["cost"]),
        "fees": _pence(totals["fees"]),
        "allowable_costs": _pence(totals["cost"] + totals["fees"]),
        "gain": _pence(totals["gain"]),
        "total_gain": _pence(summary["total_gain"]),
        "losses": _pence(abs(summary["total_loss"])),
        "proceeds_limit": "" if limit is None else _pence(limit),
        "proceeds_warning": bool(summary["proceeds_warning"]),
        "split_date": "" if split_date is None else split_date.isoformat(),
        "split_date_label": label,
        "gain_before": before,
        "gain_after": after,
    }


def _estimate(gains: CalculateCapitalGains) -> Dict[str, Any]:
    packed = {"before": _estimate_side(gains.cgt_estimate), "after": None}
    if gains.split_date is not None:
        packed["after"] = _estimate_side(gains.cgt_estimate_split)
    return packed


def _estimate_side(estimate: CapitalGainsReportEstimate) -> Dict[str, str]:
    return {
        "allowance": _pence(estimate["allowance"]),
        "taxable_gain": _pence(estimate["taxable_gain"]),
        "cgt_basic": _pence(estimate["cgt_basic"]),
        "cgt_higher": _pence(estimate["cgt_higher"]),
        "basic_rate": f"{estimate['cgt_basic_rate']:f}%",
        "higher_rate": f"{estimate['cgt_higher_rate']:f}%",
        "allowance_used": _pence(estimate["allowance_used"]),
    }


def _disposals(gains: CalculateCapitalGains) -> List[Dict[str, str]]:
    rows = []
    for asset in sorted(gains.assets):
        for event in gains.assets[asset]:
            kind = event.format_disposal()
            rows.append(
                {
                    "asset": str(event.asset),
                    "date": ReportLog.format_date(event.date),
                    "date_iso": event.date.isoformat(),
                    "disposal_type": kind,
                    "matching_rule": matching_rule(kind),
                    "quantity": _qty(event.quantity),
                    "cost": _pence(event.cost),
                    "fees": _pence(event.fees),
                    "proceeds": _pence(event.proceeds),
                    "gain": _pence(event.gain),
                }
            )
    return rows


def _income(income: CalculateIncome) -> Dict[str, Any]:
    rows = []
    for asset in sorted(income.assets):
        for event in income.assets[asset]:
            rows.append(
                {
                    "asset": str(event.asset),
                    "date": ReportLog.format_date(event.date),
                    "date_iso": event.date.isoformat(),
                    "income_type": event.type.value,
                    "note": str(event.note),
                    "quantity": _qty(event.quantity),
                    "amount": _pence(event.amount),
                    "fees": _pence(event.fees),
                }
            )
    types = [
        {
            "type": name,
            "amount": _pence(income.type_totals[name]["amount"]),
            "fees": _pence(income.type_totals[name]["fees"]),
        }
        for name in sorted(income.type_totals)
    ]
    return {
        "amount": _pence(income.totals["amount"]),
        "fees": _pence(income.totals["fees"]),
        "types": types,
        "rows": rows,
    }


def _pence(value: Decimal) -> str:
    return f"{value.quantize(_PENCE)}"


def _qty(value: Decimal) -> str:
    return f"{value.normalize():f}"


def supported_years() -> List[Dict[str, Any]]:
    """Tax years the UK individual calculator knows, oldest first."""
    return [
        {"year": int(year), "label": config.format_tax_year(int(year))}
        for year in sorted(CalculateCapitalGains.CG_DATA_INDIVIDUAL)
    ]


def report_csv(report: Mapping[str, Any]) -> str:
    """Deterministic CSV of the calculator's summary, disposals, income and pools."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    header = [
        "section",
        "field",
        "asset",
        "date",
        "detail",
        "quantity",
        "cost",
        "fees",
        "proceeds",
        "gain",
        "amount",
    ]
    writer.writerow(header)
    summary = report["summary"]
    fixed = {
        "tax_year": str(report["label"]),
        "period_start": str(report["start"]),
        "period_end": str(report["end"]),
    }
    summary_fields = (
        "disposals",
        "proceeds",
        "allowable_costs",
        "total_gain",
        "losses",
        "gain",
        "gain_before",
        "gain_after",
        "split_date",
    )
    names = {
        "proceeds": "disposal_proceeds",
        "total_gain": "gains_before_losses",
        "gain_before": "gain_before_split",
        "gain_after": "gain_after_split",
    }
    for field, value in fixed.items():
        writer.writerow(["summary", field, "", "", value, "", "", "", "", "", ""])
    for key in summary_fields:
        writer.writerow(
            ["summary", names.get(key, key), "", "", str(summary[key]), "", "", "", "", "", ""]
        )

    for row in report["disposals"]:
        writer.writerow(
            [
                "disposal",
                "",
                row["asset"],
                row["date"],
                row["disposal_type"],
                row["quantity"],
                row["cost"],
                row["fees"],
                row["proceeds"],
                row["gain"],
                "",
            ]
        )
    income = report["income"]
    writer.writerow(
        ["income_total", "amount", "", "", "", "", "", income["fees"], "", "", income["amount"]]
    )
    for row in income["types"]:
        writer.writerow(
            ["income_type", row["type"], "", "", "", "", "", row["fees"], "", "", row["amount"]]
        )
    for row in income["rows"]:
        writer.writerow(
            [
                "income",
                row["income_type"],
                row["asset"],
                row["date"],
                row["note"],
                row["quantity"],
                "",
                row["fees"],
                "",
                "",
                row["amount"],
            ]
        )
    for section, rows in (
        ("pool_start", report["pools"]["start"]),
        ("pool_end", report["pools"]["end"]),
    ):
        for row in rows:
            writer.writerow(
                [
                    section,
                    "",
                    row["asset"],
                    "",
                    "",
                    row["quantity"],
                    row["cost"],
                    row["fees"],
                    "",
                    "",
                    "",
                ]
            )
    return buffer.getvalue()
