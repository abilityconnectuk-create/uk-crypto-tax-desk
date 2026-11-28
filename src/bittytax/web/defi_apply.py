# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Apply a DeFi tag to the calculator only when that rule-file entry is confirmed."""

from __future__ import annotations

import csv
import io
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .convert import POSSIBLE_SPAM
from .engine_run import run_tax_year
from .rules import load_rules
from .tag_set import rewrite_cells, settle
from .taxyear import tax_year_containing

_BANNER = "AWAITING TAX REVIEW"
_TRANSFER = "transfer"
_CONDITIONAL = {
    "accountant_review",
    "income_only_for_a_service",
}
_TYPE_FOR_EFFECT = {
    "acquisition_at_repayment_obligation": "Loan",
    "disposal_at_market_value": "Spend",
    "disposal_at_MV": "Spend",
    "miscellaneous_income": "Staking-Reward",
    "defi_miscellaneous_income": "Interest",
    "exchange_at_market_value": "Trade",
    "disposal_for_a_right": "Spend",
    "acquisition_at_market_value": "Gift-Received",
    "not_disposal": _TRANSFER,
}


def tax_cells(row: Mapping[str, Any], forced: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Cells for the calculator. The stored type is not changed here."""
    cells = list(row.get("cells") or [])
    if not cells or row.get("deleted"):
        return []
    if row.get("spam_listed"):
        cells[0] = POSSIBLE_SPAM
        return cells
    spec = forced if forced is not None else _spec(str(row.get("tag") or ""), _year(row))
    new_type = _type_for(row, spec)
    if new_type:
        cells[0] = new_type
    return rewrite_cells(row, cells)


def any_rewritten(rows: Sequence[Mapping[str, Any]]) -> bool:
    kept: List[Mapping[str, Any]] = []
    plain: List[List[str]] = []
    revised: List[List[str]] = []
    for row in rows:
        cells = list(row.get("cells") or [])
        if not cells or row.get("deleted"):
            continue
        kept.append(row)
        plain.append(list(cells))
        revised.append(tax_cells(row))
    return settle(kept, revised) != plain


def tax_csv(rows: Sequence[Mapping[str, Any]], header: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    kept: List[Mapping[str, Any]] = []
    revised: List[List[str]] = []
    for row in rows:
        cells = tax_cells(row)
        if not cells:
            continue
        kept.append(row)
        revised.append(cells)
    for cells in settle(kept, revised):
        writer.writerow(cells)
    return buffer.getvalue()


def pending_preview(desk: Any, tax_year: int) -> List[Dict[str, str]]:
    """What each unconfirmed tag on the ledger would do if its own entry were confirmed."""
    rows = desk.all_rows()
    baseline = desk.report(tax_year)
    previews = []
    for tag in _pending(rows, tax_year):
        spec = _spec(tag, tax_year) or {}
        alt = _run_forced(rows, tax_year, tag, _forced(tag, tax_year))
        previews.append(_compare(tag, spec, baseline, alt))
    return previews


def _pending(rows: Sequence[Mapping[str, Any]], tax_year: int) -> List[str]:
    found = []
    for tag in _tags_on(rows):
        spec = _spec(tag, tax_year)
        if spec is None or spec.get("confirmed") or tag in found:
            continue
        found.append(tag)
    return found


def _forced(tag: str, tax_year: int) -> Dict[str, Any]:
    spec = dict(_spec(tag, tax_year) or {})
    spec["confirmed"] = True
    return spec


def _run_forced(
    rows: Sequence[Mapping[str, Any]], tax_year: int, tag: str, forced: Mapping[str, Any]
) -> Dict[str, Any]:
    from .convert import bittytax_header  # pylint: disable=import-outside-toplevel

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(bittytax_header())
    for row in rows:
        spec = forced if str(row.get("tag") or "") == tag else None
        writer.writerow(tax_cells(row, spec))
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "forced.csv"
        path.write_text(buffer.getvalue(), encoding="utf-8")
        return run_tax_year(str(path), tax_year)


def _compare(
    tag: str, spec: Mapping[str, Any], baseline: Mapping[str, Any], alt: Mapping[str, Any]
) -> Dict[str, str]:
    before = _figures(baseline)
    after = _figures(alt)
    effect = str(spec.get("effect") or "")
    conditional = effect in _CONDITIONAL or effect == ""
    same = before == after
    if conditional:
        detail = (
            "The rule file does not name an unconditional type, so the figures stay as they are."
        )
    elif same:
        detail = "Confirming this entry would not change this year's figures."
    else:
        detail = (
            f"Income {before['income']} to {after['income']}. "
            f"Gain {before['gain']} to {after['gain']}."
        )
    return {
        "tag": tag,
        "effect": effect,
        "banner": _BANNER,
        "detail": detail,
        "changed": "no" if same else "yes",
    }


def _figures(report: Mapping[str, Any]) -> Dict[str, str]:
    summary = report.get("summary") or {}
    income = report.get("income") or {}
    return {"gain": str(summary.get("gain", "")), "income": str(income.get("amount", ""))}


def _tags_on(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    found = []
    for row in rows:
        tag = str(row.get("tag") or "")
        if tag and tag not in found:
            found.append(tag)
    return found


def _spec(tag: str, tax_year: int) -> Optional[Dict[str, Any]]:
    if not tag:
        return None
    rules = load_rules(tax_year) or {}
    for item in rules.get("defi", {}).get("tags", []):
        if isinstance(item, dict) and str(item.get("id") or "") == tag:
            return item
    return None


def _type_for(row: Mapping[str, Any], spec: Optional[Mapping[str, Any]]) -> str:
    if not spec or not spec.get("confirmed"):
        return ""
    effect = _effect(spec, str(row.get("wallet") or ""))
    if effect in _CONDITIONAL or not effect:
        return ""
    special = {
        "by_arrival": _by_arrival,
        "follows_deposit_withdrawal": _withdrawal_type,
        "follows_deposit_liquidation": _liquidation_type,
    }
    if effect in special:
        return special[effect](row)
    return _mapped_type(effect, row)


def _mapped_type(effect: str, row: Mapping[str, Any]) -> str:
    mapped = _TYPE_FOR_EFFECT.get(effect, "")
    if mapped == "Trade":
        return _exchange_type(row)
    if mapped == _TRANSFER:
        return _transfer_type(row)
    return mapped


def _effect(spec: Mapping[str, Any], wallet: str) -> str:
    options = spec.get("options")
    if isinstance(options, list) and options:
        return _protocol(spec, wallet)
    return str(spec.get("effect") or "")


def _protocol(spec: Mapping[str, Any], wallet: str) -> str:
    raw = spec.get("protocols")
    protocols: Dict[str, Any] = raw if isinstance(raw, dict) else {}
    folded = wallet.casefold()
    for key, value in protocols.items():
        name = str(key).casefold()
        if name and (name == folded or name in folded or folded in name):
            return str(value)
    default = spec.get("default")
    if default:
        return str(default)
    return ""


def _exchange_type(row: Mapping[str, Any]) -> str:
    buy = str(row.get("buy_asset") or "")
    sell = str(row.get("sell_asset") or "")
    if buy and sell:
        return "Trade"
    if sell:
        return "Spend"
    return ""


def _by_arrival(row: Mapping[str, Any]) -> str:
    """Separate reward tokens are income. Yield inside a receipt is capital."""
    note = str(row.get("note") or "").casefold()
    buy = str(row.get("buy_asset") or "")
    sell = str(row.get("sell_asset") or "")
    if sell and ("receipt" in note or "share" in note or buy):
        return ""
    if buy and not sell:
        return "Interest"
    return ""


def _withdrawal_type(row: Mapping[str, Any]) -> str:
    choice = _deposit_choice(row)
    if choice == "disposal_at_MV":
        return "Gift-Received"
    return ""


def _liquidation_type(row: Mapping[str, Any]) -> str:
    choice = _deposit_choice(row)
    if choice == "not_disposal":
        return "Spend"
    return ""


def _deposit_choice(row: Mapping[str, Any]) -> str:
    spec = _spec("collateral_deposit", _year(row))
    if spec is None or not spec.get("confirmed"):
        return ""
    return _protocol(spec, str(row.get("wallet") or ""))


def _transfer_type(row: Mapping[str, Any]) -> str:
    buy = str(row.get("buy_asset") or "")
    sell = str(row.get("sell_asset") or "")
    if sell and not buy:
        return "Withdrawal"
    if buy and not sell:
        return "Deposit"
    return ""


def _year(row: Mapping[str, Any]) -> int:
    text = str(row.get("timestamp") or "")[:10]
    try:
        when = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        when = date.today()
    return tax_year_containing(when)
