# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Transaction tags for one tax year.

A tag changes the calculator only when that year's entry says confirmed.
Shipped files do not. The banner stays up until the owner edits the file.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .taxyear import tax_year_containing

BANNER = "awaiting owner approval"
_VALUE_INDEX = {"buy_value": 3, "sell_value": 6, "fee_value": 9}
_CACHE: Dict[str, Tuple[int, Dict[str, Any]]] = {}


def catalogue() -> List[Dict[str, Any]]:
    """Tags from every year file. The same id keeps the latest year's wording."""
    found: Dict[str, Dict[str, Any]] = {}
    for ending in (2025, 2026, 2027):
        for item in document(ending).get("tags") or []:
            if isinstance(item, dict) and item.get("id"):
                found[str(item["id"])] = item
    return list(found.values())


def document(ending_year: int) -> Dict[str, Any]:
    """The tag file for the year that ends in ``ending_year``, or an empty mapping."""
    path = _path(ending_year)
    if path is None or not path.is_file():
        return {}
    stamp = path.stat().st_mtime_ns
    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        loaded = {}
    _CACHE[str(path)] = (stamp, loaded)
    return loaded


def find(tag: str, ending_year: int) -> Optional[Dict[str, Any]]:
    """One tag, or None when this year has no such id."""
    if not tag:
        return None
    for item in document(ending_year).get("tags") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == tag:
            return item
    return None


def describe(tag: str, ending_year: int, row: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """Wording for the screen. An unconfirmed tag does not say the figures changed."""
    spec = find(tag, ending_year) or {}
    confirmed = bool(spec.get("confirmed"))
    banner = "" if confirmed else str(document(ending_year).get("banner") or BANNER)
    return {
        "id": tag,
        "label": str(spec.get("label") or tag),
        "effect": str(spec.get("effect") or ""),
        "confirmed": "yes" if confirmed else "no",
        "banner": banner,
        "changes_figures": "yes" if _would_change(spec, row or {}, confirmed) else "no",
        "note": str(spec.get("note") or "").strip(),
        "source": _source_title(spec),
        "hint": str(spec.get("hint") or ""),
        "box": str(spec.get("boxes") or ""),
        "flag": str(spec.get("flag") or ""),
    }


def default_for(row: Mapping[str, Any]) -> Dict[str, str]:
    """No tag keeps the treatment already implied by the transaction type."""
    kind = str(row.get("type") or "")
    if kind in {"Mining", "Staking-Reward", "Interest", "Income", "Dividend", "Referral"}:
        effect = "miscellaneous_income"
    elif kind in {"Trade", "Spend", "Gift-Sent", "Loan-Repayment", "Loan-Interest"}:
        effect = "disposal"
    elif kind in {"Deposit", "Gift-Received", "Loan", "Airdrop", "Fork", "Cashback"}:
        effect = "acquisition"
    elif kind == "Withdrawal":
        effect = "transfer"
    else:
        effect = "unchanged"
    return {"effect": effect, "changes_figures": "no", "banner": ""}


def rewrite_cells(
    row: Mapping[str, Any], cells: Sequence[str], *, force: bool = False
) -> List[str]:
    """Calculator cells. Unconfirmed tags return the cells unchanged."""
    revised = list(cells)
    if len(revised) < 13:
        return revised
    spec = find(str(row.get("tag") or ""), _year(row))
    if spec is None:
        return revised
    if not force and not spec.get("confirmed"):
        return revised
    return _apply(row, revised, spec, force)


def settle(
    rows: Sequence[Mapping[str, Any]],
    matrix: Sequence[Sequence[str]],
    *,
    force: bool = False,
) -> List[List[str]]:
    """A confirmed fee refund reduces the linked fee. Unconfirmed rows are left alone."""
    revised = [list(cells) for cells in matrix]
    for index, row in enumerate(rows):
        spec = find(str(row.get("tag") or ""), _year(row))
        if spec is None or spec.get("effect") != "fee_refund":
            continue
        if not force and not spec.get("confirmed"):
            continue
        linked = str(row.get("linked_fee") or "")
        if not linked:
            continue
        amount = _money(row.get("buy_value") or row.get("sell_value"))
        for other_index, other in enumerate(rows):
            if str(other.get("id") or "") != linked or len(revised[other_index]) < 10:
                continue
            reduced = _money(revised[other_index][9]) - amount
            if reduced < 0:
                reduced = Decimal(0)
            revised[other_index][9] = _plain(reduced)
            revised[index][0] = "_Ignored"
    return revised


def figures_unchanged(row: Mapping[str, Any], cells: Sequence[str]) -> bool:
    """True when the shipped year file would leave these cells as they are."""
    return rewrite_cells(row, cells) == list(cells)


def _would_change(spec: Mapping[str, Any], row: Mapping[str, Any], confirmed: bool) -> bool:
    if not confirmed or not spec:
        return False
    effect = str(spec.get("effect") or "")
    if effect == "employment":
        return False
    choice = str(row.get("airdrop_choice") or "")
    if effect == "airdrop" and choice not in {"income", "market", "nil"}:
        return False
    return True


def _apply(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    effect = str(spec.get("effect") or "")
    handlers = {
        "miscellaneous_income": _set_type,
        "employment": _keep,
        "airdrop": _airdrop,
        "fork": _fork,
        "acquisition": _disposal_or_type,
        "disposal": _disposal_or_type,
        "disposal_allowable": _set_type,
        "disposal_not_allowable": _set_type,
        "disposal_memo": _memo,
        "futures_realised": _futures_realised,
        "futures_fee": _futures_fee,
        "futures_funding": _futures_funding,
        "exchange": _exchange,
        "pool": _pool,
        "gift": _gift,
        "charity": _set_type,
        "keep_in_pool": _keep_in_pool,
        "cashback": _cashback,
        "fee_refund": _fee_refund,
    }
    handler = handlers.get(effect, _keep)
    return handler(row, cells, spec, force)


def _keep(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, spec, force
    return cells


def _set_type(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, force
    chosen = str(spec.get("calculator_type") or "")
    if chosen:
        cells[0] = chosen
    return cells


def _airdrop(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del spec, force
    choice = str(row.get("airdrop_choice") or "")
    if choice == "income":
        cells[0] = "Income"
    elif choice == "market":
        cells[0] = "Airdrop"
    elif choice == "nil":
        cells[0] = "Airdrop"
        cells[3] = "0"
    return cells


def _fork(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, force
    cells[0] = str(spec.get("calculator_type") or "Fork")
    return cells


def _disposal_or_type(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    if cells[2] and cells[5] and str(spec.get("calculator_type") or "") == "Trade":
        cells[0] = "Trade"
        return cells
    return _set_type(row, cells, spec, force)


def _memo(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    cells = _set_type(row, cells, spec, force)
    memo = str(spec.get("memo") or "Memo.")
    note = cells[12]
    if memo not in note:
        cells[12] = f"{note} | {memo}".strip(" |")
    return cells


def _futures_realised(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del spec, force
    amount = _money(row.get("sell_value") or row.get("buy_value") or cells[6] or cells[3])
    cells[0] = "Margin-Loss" if amount < 0 else "Margin-Gain"
    return cells


def _futures_fee(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, spec, force
    cells[0] = "Margin-Fee"
    return cells


def _futures_funding(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, spec, force
    cells[0] = "Margin-Fee"
    note = cells[12]
    if "funding" not in note.casefold():
        cells[12] = f"{note} | funding fee".strip(" |")
    return cells


def _exchange(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, force
    if cells[2] and cells[5]:
        cells[0] = "Trade"
    elif cells[5]:
        cells[0] = str(spec.get("calculator_type") or "Spend")
    elif cells[2]:
        cells[0] = "Deposit"
    return cells


def _pool(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    raw_protocols = spec.get("protocols")
    protocols: Mapping[str, Any] = raw_protocols if isinstance(raw_protocols, dict) else {}
    protocol = str(row.get("pool_protocol") or "")
    same = str(row.get("pool_same_token") or "") == "yes"
    receipt = str(row.get("pool_receipt") or "") == "yes"
    classified = str(protocols.get(protocol) or "") if protocol else ""
    restricted = str(row.get("pool_restricted") or "") == "yes"
    if classified == "not_disposal" and same and not receipt and restricted:
        if cells[5] and not cells[2]:
            cells[0] = "Withdrawal"
        elif cells[2] and not cells[5]:
            cells[0] = "Deposit"
        return cells
    return _exchange(row, cells, spec, force)


def _gift(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del spec, force
    cells[0] = "Gift-Spouse" if str(row.get("spouse") or "") == "yes" else "Gift-Sent"
    return cells


def _keep_in_pool(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row, spec, force
    cells[0] = "_Ignored"
    return cells


def _cashback(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del row
    cells[0] = str(spec.get("calculator_type") or "Cashback")
    switch = document(_year_from_cells(cells)).get("cashback_cost") or {}
    if not isinstance(switch, dict):
        return cells
    if not force and not switch.get("confirmed"):
        return cells
    if str(switch.get("value") or "nil") == "nil":
        cells[3] = "0"
    return cells


def _fee_refund(
    row: Mapping[str, Any], cells: List[str], spec: Mapping[str, Any], force: bool
) -> List[str]:
    del spec, force
    if str(row.get("linked_fee") or ""):
        cells[0] = "_Ignored"
        return cells
    cells[0] = "Deposit"
    cells[3] = "0"
    return cells


def _source_title(spec: Mapping[str, Any]) -> str:
    sources = spec.get("sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        return str(sources[0].get("title") or "")
    return ""


def _year(row: Mapping[str, Any]) -> int:
    text = str(row.get("timestamp") or row.get("date") or "")[:10]
    try:
        when = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        when = date.today()
    return tax_year_containing(when)


def _year_from_cells(cells: Sequence[str]) -> int:
    return _year({"timestamp": cells[11] if len(cells) > 11 else ""})


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", "").replace("£", ""))
    except InvalidOperation:
        return Decimal(0)


def _plain(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _path(ending_year: int) -> Optional[Path]:
    chosen = os.environ.get("UK_CRYPTO_TAX_DESK_RULES", "").strip()
    root = Path(chosen).expanduser() if chosen else _config_dir()
    if root is None:
        return None
    slug = f"{ending_year - 1}-{ending_year % 100:02d}"
    return root / "tags" / f"{slug}.yaml"


def _config_dir() -> Optional[Path]:
    start = Path(__file__).resolve()
    for parent in start.parents:
        candidate = parent / "config"
        if (candidate / "sa108").is_dir():
            return candidate
    return None
