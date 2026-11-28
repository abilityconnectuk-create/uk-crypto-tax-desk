# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Transactions list, warnings, manual rows, and bulk edit.

Figures follow the tag file only when that file says confirmed.
This module does not contact Koinly.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import dateutil.tz
from openpyxl import Workbook, load_workbook

from ..config import config
from ..conv.txref import hash_kind
from . import tx_store
from .errors import DeskError
from .events import append_event
from .tag_set import BANNER, catalogue, default_for, describe
from .taxyear import tax_year_containing

_UTC = dateutil.tz.UTC
_LONDON = dateutil.tz.gettz("Europe/London")
_FIAT = {code.upper() for code in config.FIAT_LIST}
_PAGE_SIZES = (25, 50, 100)
_CLASSES = (
    ("crypto_deposit", "Crypto deposit"),
    ("fiat_deposit", "Fiat deposit"),
    ("crypto_withdrawal", "Crypto withdrawal"),
    ("fiat_withdrawal", "Fiat withdrawal"),
    ("buy", "Buy"),
    ("sell", "Sell"),
    ("exchange", "Exchange"),
    ("transfer", "Transfer"),
)
_CLASS_LABELS = dict(_CLASSES)
_WARNINGS = (
    ("missing_purchase", "Missing purchase"),
    ("missing_price", "Missing price"),
    ("missing_acquisition_cost", "Missing acquisition cost"),
    ("missing_cost_basis", "Missing cost basis"),
)
_WARNING_LABELS = dict(_WARNINGS)
_ACQUIRE = {
    "Deposit",
    "Unstake",
    "Mining",
    "Staking-Reward",
    "Interest",
    "Dividend",
    "Income",
    "Gift-Received",
    "Review-Acquisition",
    "Fork",
    "Airdrop",
    "Referral",
    "Cashback",
    "Fee-Rebate",
    "Loan",
    "Margin-Gain",
    "Margin-Fee-Rebate",
}
_DISPOSE = {
    "Spend",
    "Gift-Sent",
    "Gift-Spouse",
    "Charity-Sent",
    "Loan-Repayment",
    "Loan-Interest",
    "Margin-Loss",
    "Margin-Fee",
    "Allocation",
    "Trade",
}
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_EXPLORERS = {
    "ethereum": "https://etherscan.io/tx/{hash}",
    "eth": "https://etherscan.io/tx/{hash}",
    "base": "https://basescan.org/tx/{hash}",
    "arbitrum": "https://arbiscan.io/tx/{hash}",
    "optimism": "https://optimistic.etherscan.io/tx/{hash}",
    "polygon": "https://polygonscan.com/tx/{hash}",
    "bsc": "https://bscscan.com/tx/{hash}",
    "bnb": "https://bscscan.com/tx/{hash}",
    "avalanche": "https://snowtrace.io/tx/{hash}",
    "avax": "https://snowtrace.io/tx/{hash}",
    "solana": "https://solscan.io/tx/{hash}",
    "sol": "https://solscan.io/tx/{hash}",
}
_CHAIN_WORDS = (
    "ethereum",
    "solana",
    "arbitrum",
    "optimism",
    "polygon",
    "avalanche",
    "base",
    "bsc",
)
_FILTER_KEYS = (
    "query",
    "wallet",
    "asset",
    "tx_class",
    "tag",
    "manual",
    "warning",
    "date_from",
    "date_to",
    "clock",
    "tax_quick",
    "amount_min",
    "amount_max",
    "net_min",
    "net_max",
    "fee_min",
    "fee_max",
    "gain_min",
    "gain_max",
    "description",
    "sender",
    "recipient",
    "tx_hash",
    "contract",
    "method",
    "value_source",
    "issues",
    "deleted",
    "spam",
    "import_source",
    "import_type",
    "import_date_from",
    "import_date_to",
    "sort",
)
_EXPORT = (
    "id",
    "type",
    "tag",
    "reviewed",
    "deleted",
    "manual",
    "buy_quantity",
    "buy_asset",
    "buy_value",
    "sell_quantity",
    "sell_asset",
    "sell_value",
    "fee_quantity",
    "fee_asset",
    "fee_value",
    "wallet",
    "timestamp",
    "note",
    "description",
    "sender",
    "recipient",
    "tx_hash",
    "contract",
    "method",
    "value_source",
    "gbp",
)
_EDITABLE = (
    "type",
    "tag",
    "reviewed",
    "deleted",
    "buy_value",
    "sell_value",
    "fee_value",
    "description",
    "sender",
    "recipient",
    "tx_hash",
    "note",
    "gbp",
)


def build_page(
    desk: Any, query: Mapping[str, Sequence[str]], preview: bool = False
) -> Dict[str, Any]:
    """One page of the transactions list."""
    filters = read_filters(query)
    page = _page_number(_first(query, "page"))
    per_page = _per_page(_first(query, "per"))
    prepared = prepare(desk.all_rows(), tx_store.load_dust(desk.root), filters["clock"])
    matched = [row for row in prepared if row_matches(row, filters)]
    ordered = _sort(matched, filters["sort"])
    start = (page - 1) * per_page
    visible = ordered[start : start + per_page]
    _attach_gbp(desk.root, visible)
    return {
        "groups": group_rows(visible),
        "rows": visible,
        "total": len(ordered),
        "page": page,
        "pages": max(1, (len(ordered) + per_page - 1) // per_page),
        "per_page": per_page,
        "filters": filters,
        "wallets": _unique(prepared, "wallet"),
        "assets": _assets(prepared),
        "tags": _tags(prepared),
        "classes": _CLASSES,
        "warnings": _WARNINGS,
        "banner": BANNER,
        "dust": format(tx_store.load_dust(desk.root), "f"),
        "preview": tx_store.load_preview(desk.root) if preview else {},
        "kept": {key: filters[key] for key in _FILTER_KEYS if filters.get(key)},
    }


def prepare(rows: Sequence[Mapping[str, Any]], dust: Decimal, clock: str) -> List[Dict[str, Any]]:
    """Copies of the rows with warnings, dates, and explorer links."""
    copied = [dict(row) for row in rows]
    _attach_warnings(copied, dust)
    for row in copied:
        row["filter_date"] = zoned_date(str(row.get("timestamp") or ""), clock)
        row["tx_class"] = classify(row)
        row["tx_class_label"] = _CLASS_LABELS.get(str(row["tx_class"]), "")
        row["explorer"] = explorer_url(row)
        row["warning_labels"] = [_WARNING_LABELS[code] for code in row["warnings"]]
        row.setdefault("description", row.get("note") or "")
        row.setdefault("value_source", "")
    return copied


def warning_total(desk: Any, year: int) -> int:
    """Rows in the tax year that still show a warning after the dust threshold."""
    rows = prepare(desk.all_rows(), tx_store.load_dust(desk.root), "utc")
    return sum(1 for row in rows if row["warnings"] and _in_year(str(row.get("date") or ""), year))


def read_filters(query: Mapping[str, Sequence[str]]) -> Dict[str, str]:
    chosen = {key: _first(query, key) for key in _FILTER_KEYS}
    if not chosen["clock"]:
        chosen["clock"] = "utc"
    if not chosen["sort"]:
        chosen["sort"] = "date"
    if chosen["tax_quick"].isdigit():
        start, end = tax_year_bounds(int(chosen["tax_quick"]))
        chosen["date_from"] = start
        chosen["date_to"] = end
    return chosen


def row_matches(row: Mapping[str, Any], chosen: Mapping[str, str]) -> bool:
    """True when the row passes every active filter."""
    checks = (
        _deleted_ok(row, chosen.get("deleted") or ""),
        _spam_ok(row, chosen.get("spam") or ""),
        _manual_ok(row, chosen.get("manual") or ""),
        _warning_ok(row, chosen.get("warning") or "", chosen.get("issues") or ""),
        not chosen.get("wallet") or row.get("wallet") == chosen["wallet"],
        not chosen.get("asset") or chosen["asset"] in (row.get("assets") or []),
        not chosen.get("tx_class") or row.get("tx_class") == chosen["tx_class"],
        not chosen.get("tag") or str(row.get("tag") or "") == chosen["tag"],
        _date_ok(str(row.get("filter_date") or ""), chosen),
        _query_ok(row, chosen.get("query") or ""),
        _range_ok(_quantity(row), chosen.get("amount_min") or "", chosen.get("amount_max") or ""),
        _range_ok(_net(row), chosen.get("net_min") or "", chosen.get("net_max") or ""),
        _range_ok(
            _money(row.get("fee_value")), chosen.get("fee_min") or "", chosen.get("fee_max") or ""
        ),
        _range_ok(
            _money(row.get("gain")), chosen.get("gain_min") or "", chosen.get("gain_max") or ""
        ),
        _contains(row.get("description"), chosen.get("description") or ""),
        _contains(row.get("sender"), chosen.get("sender") or ""),
        _contains(row.get("recipient"), chosen.get("recipient") or ""),
        _contains(row.get("tx_hash"), chosen.get("tx_hash") or ""),
        _contains(row.get("contract"), chosen.get("contract") or ""),
        _contains(row.get("method"), chosen.get("method") or ""),
        _equals(row.get("value_source"), chosen.get("value_source") or ""),
        _contains(row.get("import_source"), chosen.get("import_source") or ""),
        _contains(row.get("import_type"), chosen.get("import_type") or ""),
        _import_date_ok(str(row.get("import_date") or ""), chosen),
    )
    return all(checks)


def classify(row: Mapping[str, Any]) -> str:
    """Koinly-style type chip. The calculator type is left as it is."""
    kind = str(row.get("type") or "")
    note = str(row.get("note") or "").casefold()
    label = ""
    if str(row.get("manual_kind") or "") == "transfer":
        label = "transfer"
    elif str(row.get("tag") or "") == "own_wallet_transfer" or "own wallet transfer" in note:
        label = "transfer"
    elif kind == "Trade":
        label = _trade_class(str(row.get("buy_asset") or ""), str(row.get("sell_asset") or ""))
    elif kind == "Deposit":
        label = "fiat_deposit" if _fiat(str(row.get("buy_asset") or "")) else "crypto_deposit"
    elif kind == "Withdrawal":
        label = (
            "fiat_withdrawal" if _fiat(str(row.get("sell_asset") or "")) else "crypto_withdrawal"
        )
    return label


def _trade_class(buy: str, sell: str) -> str:
    if _fiat(sell) and buy and not _fiat(buy):
        return "buy"
    if _fiat(buy) and sell and not _fiat(sell):
        return "sell"
    return "exchange"


def zoned_date(timestamp: str, clock: str) -> str:
    """Calendar date. UK time converts a UTC timestamp to Europe/London."""
    text = timestamp.strip()
    if len(text) < 10:
        return ""
    if clock != "uk" or len(text) < 19 or _LONDON is None:
        return text[:10]
    try:
        naive = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:10]
    return naive.replace(tzinfo=_UTC).astimezone(_LONDON).date().isoformat()


def tax_year_bounds(ending_year: int) -> Tuple[str, str]:
    """6 April to 5 April, as ISO dates."""
    return f"{ending_year - 1}-04-06", f"{ending_year}-04-05"


def explorer_url(row: Mapping[str, Any]) -> str:
    """Block explorer for a hash when the chain is known. Otherwise empty."""
    digest = str(row.get("tx_hash") or "").strip()
    chain = _chain(row)
    pattern = _EXPLORERS.get(chain, "")
    if not digest or not pattern:
        return ""
    if chain in {"solana", "sol"}:
        if not _solana_hash(digest):
            return ""
        return pattern.format(hash=digest)
    if hash_kind(digest) != "chain":
        return ""
    return pattern.format(hash=digest)


def group_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Visible rows, most recent day first, under a date heading."""
    groups: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for row in rows:
        day = str(row.get("filter_date") or "")
        if current.get("date") != day:
            current = {"date": day, "label": _pretty(day), "rows": []}
            groups.append(current)
        held = current.get("rows")
        if isinstance(held, list):
            held.append(row)
    return groups


def open_row(desk: Any, row_id: str) -> Dict[str, Any]:
    """One row for the editor, including the tag banner and the value log."""
    base = desk.transaction(row_id)
    dust = tx_store.load_dust(desk.root)
    prepared = next(row for row in prepare(desk.all_rows(), dust, "utc") if row["id"] == row_id)
    prepared["log"] = base.get("log") or []
    prepared["allowed_types"] = base.get("allowed_types") or []
    prepared["defi_tags"] = base.get("defi_tags") or []
    _attach_gbp(desk.root, [prepared])
    ending = _row_year(prepared)
    spec = describe(str(prepared.get("tag") or ""), ending, prepared)
    if not prepared.get("tag"):
        spec = {"banner": "", "note": default_for(prepared)["effect"], "hint": "", "flag": ""}
    prepared["tag_banner"] = spec.get("banner") or ""
    prepared["tag_note"] = spec.get("note") or prepared.get("tag_note") or ""
    prepared["tag_hint"] = spec.get("hint") or ""
    prepared["tag_flag"] = spec.get("flag") or ""
    prepared["tag_choices"] = _tag_choices()
    prepared["audit"] = [
        entry for entry in tx_store.audit_log(desk.root) if entry.get("row_id") == row_id
    ]
    return prepared


def save_row(desk: Any, row_id: str, form: Mapping[str, str], who: str) -> None:
    """Set the tag, the reviewed mark, and the GBP override."""
    current = open_row(desk, row_id)
    tag = form.get("tag", "")
    if tag and tag not in {item["id"] for item in _tag_choices()}:
        raise DeskError("Choose a tag from the list.")
    if tag != str(current.get("tag") or ""):
        _audit(desk, row_id, "tag", str(current.get("tag") or ""), tag, who)
    reviewed = "yes" if form.get("reviewed") == "yes" else "no"
    if reviewed == "yes" and not current.get("reviewed"):
        _audit(desk, row_id, "reviewed", "no", "yes", who)
    elif reviewed == "no" and current.get("reviewed"):
        _audit(desk, row_id, "reviewed", "yes", "no", who)
    fields = {
        "tag": tag,
        "reviewed": reviewed,
        "airdrop_choice": form.get("airdrop_choice", ""),
        "spouse": "yes" if form.get("spouse") == "yes" else "",
        "pool_protocol": form.get("pool_protocol", ""),
        "pool_same_token": "yes" if form.get("pool_same_token") == "yes" else "",
        "pool_receipt": "yes" if form.get("pool_receipt") == "yes" else "",
        "pool_restricted": "yes" if form.get("pool_restricted") == "yes" else "",
        "linked_fee": form.get("linked_fee", ""),
        "chain": form.get("chain", ""),
    }
    tx_store.update_meta(desk.root, row_id, fields)
    if "gbp" in form:
        set_gbp(desk, row_id, form.get("gbp", ""), who)
    append_event(desk.root, "tx-edit", who, "Edited a transaction row.", {"row_id": row_id})


def set_gbp(desk: Any, row_id: str, raw: str, who: str) -> None:
    """Blank means the market value. Each change is an audit line."""
    row = next((item for item in desk.all_rows() if item["id"] == row_id), None)
    if row is None:
        raise DeskError("That transaction is not in the ledger.")
    new = _gbp(raw)
    latest = tx_store.latest_overrides(desk.root)
    for field in value_fields(row):
        entry = latest.get((row_id, field))
        current = ""
        if entry is not None and str(entry.get("new") or "") != "":
            current = str(entry["new"])
        if current == new:
            continue
        old = current or str(row.get(field) or "")
        if old == new:
            continue
        _audit(desk, row_id, field, old, new, who)


def add_transaction(desk: Any, form: Mapping[str, str], who: str) -> List[str]:
    """A typed-in deposit, withdrawal, trade, or transfer. The row is marked manual."""
    kind = form.get("kind", "")
    if kind not in {"deposit", "withdrawal", "trade", "transfer"}:
        raise DeskError("Choose Deposit, Withdrawal, Trade, or Transfer.")
    when = _timestamp(form.get("date", ""))
    asset_in = form.get("buy_asset", "").strip()
    asset_out = form.get("sell_asset", "").strip()
    qty_in = form.get("buy_quantity", "").strip()
    qty_out = form.get("sell_quantity", "").strip()
    wallet_from = form.get("from_wallet", "").strip()
    wallet_to = form.get("to_wallet", "").strip()
    value = _gbp(form.get("fiat_value", "")) if form.get("fiat_value", "").strip() else ""
    note = form.get("description", "").strip()
    digest = form.get("tx_hash", "").strip()
    if digest:
        note = f"{note} | tx {digest}".strip(" |")
    groups = _manual_cells(
        kind, qty_in, asset_in, qty_out, asset_out, value, wallet_from, wallet_to, when, note
    )
    row_ids = desk.append_manual(groups, who)
    shared = {
        "manual": "yes",
        "manual_kind": kind,
        "tag": form.get("tag", ""),
        "tx_hash": digest,
        "sender": wallet_from,
        "recipient": wallet_to,
        "description": form.get("description", "").strip(),
        "chain": form.get("chain", ""),
    }
    for row_id in row_ids:
        tx_store.update_meta(desk.root, row_id, shared)
        if value:
            set_gbp(desk, row_id, value, who)
    return row_ids


def selected_ids(
    desk: Any, form: Mapping[str, Sequence[str]], query: Mapping[str, Sequence[str]]
) -> List[str]:
    """Ticked rows, or every row that matches the current filters."""
    if _first(form, "scope") == "filtered":
        filters = read_filters(query)
        prepared = prepare(desk.all_rows(), tx_store.load_dust(desk.root), filters["clock"])
        return [str(row["id"]) for row in prepared if row_matches(row, filters)]
    return [str(item) for item in form.get("row_id", []) if str(item)]


def soft_delete(desk: Any, row_ids: Sequence[str], who: str) -> None:
    for row_id in row_ids:
        tx_store.update_meta(desk.root, row_id, {"deleted": "yes"})
        _audit(desk, row_id, "deleted", "no", "yes", who)
    append_event(
        desk.root, "tx-delete", who, "Soft-deleted transactions.", {"rows": str(len(row_ids))}
    )
    desk.refresh_records()


def restore_rows(desk: Any, row_ids: Sequence[str], who: str) -> None:
    for row_id in row_ids:
        tx_store.update_meta(desk.root, row_id, {"deleted": "no"})
        _audit(desk, row_id, "deleted", "yes", "no", who)
    append_event(
        desk.root, "tx-restore", who, "Restored transactions.", {"rows": str(len(row_ids))}
    )
    desk.refresh_records()


def aggregate_rows(desk: Any, row_ids: Sequence[str], who: str) -> str:
    """One manual row in place of several that share a type and assets."""
    chosen = [
        row for row in desk.all_rows() if row["id"] in set(row_ids) and not row.get("deleted")
    ]
    if len(chosen) < 2:
        raise DeskError("Choose at least two rows to aggregate.")
    if len({row["type"] for row in chosen}) != 1:
        raise DeskError("Aggregate rows of the same type.")
    if (
        len({row["buy_asset"] for row in chosen}) != 1
        or len({row["sell_asset"] for row in chosen}) != 1
    ):
        raise DeskError("Aggregate rows that share the same assets.")
    first = chosen[0]
    cells = _blank_cells()
    cells[0] = str(first["type"])
    cells[1] = _plain(_sum(chosen, "buy_quantity"))
    cells[2] = str(first["buy_asset"])
    cells[3] = _plain(_sum(chosen, "buy_value"))
    cells[4] = _plain(_sum(chosen, "sell_quantity"))
    cells[5] = str(first["sell_asset"])
    cells[6] = _plain(_sum(chosen, "sell_value"))
    cells[10] = str(first["wallet"])
    cells[11] = str(first["timestamp"])
    cells[12] = f"Aggregate of {len(chosen)} rows"
    created = desk.append_manual([cells], who)[0]
    tx_store.update_meta(
        desk.root,
        created,
        {
            "manual": "yes",
            "manual_kind": "aggregate",
            "description": cells[12],
            "aggregate_parts": ",".join(str(row["id"]) for row in chosen),
        },
    )
    soft_delete(desk, [str(row["id"]) for row in chosen], who)
    return created


def export_csv(
    rows: Sequence[Mapping[str, Any]], overrides: Mapping[Tuple[str, str], Mapping[str, str]]
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_EXPORT)
    for row in rows:
        writer.writerow([_export_value(row, key, overrides) for key in _EXPORT])
    return buffer.getvalue().encode("utf-8")


def export_xlsx(
    rows: Sequence[Mapping[str, Any]], overrides: Mapping[Tuple[str, str], Mapping[str, str]]
) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Transactions"
    sheet.append(list(_EXPORT))
    for row in rows:
        sheet.append([_export_value(row, key, overrides) for key in _EXPORT])
    handle = io.BytesIO()
    book.save(handle)
    return handle.getvalue()


def read_table(data: bytes, filename: str) -> List[Dict[str, str]]:
    """Rows from a CSV or XLSX edited from the export."""
    if filename.casefold().endswith(".xlsx") or data[:2] == b"PK":
        return _read_xlsx(data)
    return _read_csv(data)


def diff_table(
    current: Sequence[Mapping[str, Any]], uploaded: Sequence[Mapping[str, str]]
) -> List[Dict[str, Any]]:
    """Field changes. Unknown ids are refused. Nothing is written."""
    by_id = {str(row["id"]): row for row in current}
    changes = []
    for item in uploaded:
        row_id = str(item.get("id") or "").strip()
        if not row_id:
            continue
        if row_id not in by_id:
            raise DeskError("That file names a transaction that is not in the ledger.")
        fields = []
        exported = _export_mapping(by_id[row_id])
        for key in _EDITABLE:
            if key not in item:
                continue
            old = str(exported.get(key) or "")
            new = str(item.get(key) or "")
            if old != new:
                fields.append({"field": key, "old": old, "new": new})
        if fields:
            changes.append({"id": row_id, "fields": fields})
    return changes


def stage_import(desk: Any, data: bytes, filename: str) -> List[Dict[str, Any]]:
    uploaded = read_table(data, filename)
    current = prepare(desk.all_rows(), tx_store.load_dust(desk.root), "utc")
    _attach_gbp(desk.root, current)
    changes = diff_table(current, uploaded)
    tx_store.save_preview(desk.root, {"changes": changes})
    return changes


def apply_import(desk: Any, who: str) -> int:
    """Apply the staged diff. The raw upload is not edited."""
    preview = tx_store.load_preview(desk.root)
    changes = preview.get("changes") if isinstance(preview, dict) else None
    if not isinstance(changes, list) or not changes:
        raise DeskError("There is no import diff to apply.")
    count = 0
    for change in changes:
        if not isinstance(change, dict):
            continue
        _apply_change(desk, change, who)
        count += 1
    tx_store.clear_preview(desk.root)
    desk.refresh_records()
    append_event(desk.root, "tx-import", who, "Applied a transaction import.", {"rows": str(count)})
    return count


def settings_view(root: Any) -> Dict[str, Any]:
    return {
        "renames": tx_store.load_renames(root),
        "spam_tokens": tx_store.load_spam_tokens(root),
        "prices": tx_store.load_prices(root),
        "dust": format(tx_store.load_dust(root), "f"),
        "banner": BANNER,
        "tags": [
            {
                "id": str(item["id"]),
                "label": str(item.get("label") or item["id"]),
                "effect": str(item.get("effect") or ""),
                "confirmed": bool(item.get("confirmed")),
                "source": str((item.get("sources") or [{}])[0].get("title") or ""),
                "url": str((item.get("sources") or [{}])[0].get("url") or ""),
            }
            for item in catalogue()
        ],
    }


def value_fields(row: Mapping[str, Any]) -> List[str]:
    """GBP columns a blank override returns to the market value."""
    fields = []
    if row.get("sell_asset") and not _fiat(str(row["sell_asset"])):
        fields.append("sell_value")
    if row.get("buy_asset") and not _fiat(str(row["buy_asset"])):
        fields.append("buy_value")
    if not fields:
        fields.append("fee_value" if row.get("fee_asset") else "buy_value")
    return fields


def _attach_warnings(rows: List[Dict[str, Any]], dust: Decimal) -> None:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or ""))
    pools: Dict[str, Dict[str, Decimal]] = {}
    for row in ordered:
        codes: List[str] = []
        if not row.get("deleted") and not _spam_row(row):
            codes.extend(_price_warnings(row))
            codes.extend(_disposal_warnings(row, pools))
            _remember_acquisition(row, pools)
        if _dusty(row, dust):
            codes = [code for code in codes if code == "missing_price" and _known_gbp(row) is None]
        row["warnings"] = list(dict.fromkeys(codes))


def _price_warnings(row: Mapping[str, Any]) -> List[str]:
    found = []
    pairs = (
        ("buy_quantity", "buy_asset", "buy_value"),
        ("sell_quantity", "sell_asset", "sell_value"),
        ("fee_quantity", "fee_asset", "fee_value"),
    )
    for qty_key, asset_key, value_key in pairs:
        asset = str(row.get(asset_key) or "")
        if (
            asset
            and not _fiat(asset)
            and _money(row.get(qty_key)) > 0
            and not str(row.get(value_key) or "")
        ):
            found.append("missing_price")
    return found


def _disposal_warnings(row: Mapping[str, Any], pools: Dict[str, Dict[str, Decimal]]) -> List[str]:
    kind = str(row.get("type") or "")
    if kind not in _DISPOSE:
        return []
    asset = str(row.get("sell_asset") or "")
    quantity = _money(row.get("sell_quantity"))
    if not asset or _fiat(asset) or quantity <= 0:
        return []
    state = pools.setdefault(
        asset, {"qty": Decimal(0), "cost": Decimal(0), "with_cost": Decimal(0)}
    )
    found = []
    if state["qty"] <= 0:
        found.extend(["missing_purchase", "missing_cost_basis"])
    elif state["cost"] <= 0:
        found.extend(["missing_cost_basis", "missing_acquisition_cost"])
    elif state["with_cost"] < quantity:
        found.append("missing_acquisition_cost")
    taken = min(quantity, state["qty"]) if state["qty"] > 0 else Decimal(0)
    if state["qty"] > 0 and taken:
        share = taken / state["qty"]
        state["cost"] -= state["cost"] * share
        state["with_cost"] -= state["with_cost"] * share
        state["qty"] -= taken
    return found


def _remember_acquisition(row: Mapping[str, Any], pools: Dict[str, Dict[str, Decimal]]) -> None:
    kind = str(row.get("type") or "")
    if kind not in _ACQUIRE and kind != "Trade":
        return
    asset = str(row.get("buy_asset") or "")
    quantity = _money(row.get("buy_quantity"))
    if not asset or _fiat(asset) or quantity <= 0:
        return
    state = pools.setdefault(
        asset, {"qty": Decimal(0), "cost": Decimal(0), "with_cost": Decimal(0)}
    )
    state["qty"] += quantity
    if str(row.get("buy_value") or ""):
        state["cost"] += _money(row.get("buy_value"))
        state["with_cost"] += quantity


def _dusty(row: Mapping[str, Any], dust: Decimal) -> bool:
    if dust <= 0:
        return False
    amount = _known_gbp(row)
    if amount is None:
        return False
    return abs(amount) < dust


def _known_gbp(row: Mapping[str, Any]) -> Optional[Decimal]:
    found = [
        _money(row.get(key))
        for key in ("sell_value", "buy_value", "fee_value")
        if str(row.get(key) or "")
    ]
    if not found:
        return None
    return max(found, key=abs)


def _manual_cells(
    kind: str,
    qty_in: str,
    asset_in: str,
    qty_out: str,
    asset_out: str,
    value: str,
    wallet_from: str,
    wallet_to: str,
    when: str,
    note: str,
) -> List[List[str]]:
    if kind == "deposit":
        _need(qty_in, asset_in, "Enter the amount and the asset received.")
        return [
            _cells(
                "Deposit", qty_in, asset_in, value, "", "", "", wallet_to or wallet_from, when, note
            )
        ]
    if kind == "withdrawal":
        _need(qty_out, asset_out, "Enter the amount and the asset sent.")
        return [
            _cells(
                "Withdrawal",
                "",
                "",
                "",
                qty_out,
                asset_out,
                value,
                wallet_from or wallet_to,
                when,
                note,
            )
        ]
    if kind == "trade":
        _need(qty_in, asset_in, "Enter the amount and the asset bought.")
        _need(qty_out, asset_out, "Enter the amount and the asset sold.")
        return [
            _cells(
                "Trade",
                qty_in,
                asset_in,
                value,
                qty_out,
                asset_out,
                value,
                wallet_from or wallet_to,
                when,
                note,
            )
        ]
    _need(qty_out or qty_in, asset_out or asset_in, "Enter the amount and the asset.")
    asset = asset_out or asset_in
    quantity = qty_out or qty_in
    if not wallet_from or not wallet_to:
        raise DeskError("A transfer needs a from wallet and a to wallet.")
    return [
        _cells("Withdrawal", "", "", "", quantity, asset, value, wallet_from, when, note),
        _cells("Deposit", quantity, asset, value, "", "", "", wallet_to, when, note),
    ]


def _cells(
    kind: str,
    buy_qty: str,
    buy_asset: str,
    buy_value: str,
    sell_qty: str,
    sell_asset: str,
    sell_value: str,
    wallet: str,
    when: str,
    note: str,
) -> List[str]:
    if not wallet:
        raise DeskError("Enter a wallet.")
    return [
        kind,
        buy_qty,
        buy_asset,
        buy_value,
        sell_qty,
        sell_asset,
        sell_value,
        "",
        "",
        "",
        wallet,
        when,
        note,
    ]


def _blank_cells() -> List[str]:
    return [""] * 13


def _apply_change(desk: Any, change: Mapping[str, Any], who: str) -> None:
    row_id = str(change.get("id") or "")
    fields = {str(item["field"]): str(item["new"]) for item in change.get("fields") or []}
    row = next(item for item in desk.all_rows() if item["id"] == row_id)
    if "type" in fields and fields["type"] and fields["type"] != row["type"]:
        desk.reclassify(row_id, fields["type"], "Bulk edit import.", who, str(row.get("tag") or ""))
    if "tag" in fields:
        tx_store.update_meta(desk.root, row_id, {"tag": fields["tag"]})
        _audit(desk, row_id, "tag", str(row.get("tag") or ""), fields["tag"], who)
    meta: Dict[str, str] = {}
    for key in ("description", "sender", "recipient", "tx_hash"):
        if key in fields:
            meta[key] = fields[key]
    if "reviewed" in fields:
        meta["reviewed"] = "yes" if fields["reviewed"] in {"yes", "true", "1"} else "no"
    if "deleted" in fields:
        meta["deleted"] = "yes" if fields["deleted"] in {"yes", "true", "1"} else "no"
    if meta:
        tx_store.update_meta(desk.root, row_id, meta)
    for key in ("buy_value", "sell_value", "fee_value"):
        if key in fields:
            _audit(desk, row_id, key, str(row.get(key) or ""), fields[key], who)
    if "gbp" in fields:
        set_gbp(desk, row_id, fields["gbp"], who)
    if "note" in fields and fields["note"] != str(row.get("note") or ""):
        desk.patch_note(row_id, fields["note"])


def _audit(desk: Any, row_id: str, field: str, old: str, new: str, who: str) -> None:
    tx_store.append_audit(
        desk.root,
        {
            "row_id": row_id,
            "field": field,
            "old": old,
            "new": new,
            "at": tx_store.stamp(),
            "who": who or "local",
        },
    )


def _attach_gbp(root: Any, rows: Sequence[Dict[str, Any]]) -> None:
    latest = tx_store.latest_overrides(root)
    for row in rows:
        fields = value_fields(row)
        entry = latest.get((str(row.get("id") or ""), fields[0])) if fields else None
        row["gbp"] = "" if entry is None else str(entry.get("new") or "")


def _export_mapping(row: Mapping[str, Any]) -> Dict[str, str]:
    return {key: _export_value(row, key, {}) for key in _EXPORT}


def _export_value(
    row: Mapping[str, Any], key: str, overrides: Mapping[Tuple[str, str], Mapping[str, str]]
) -> str:
    if key == "reviewed":
        return "yes" if row.get("reviewed") else "no"
    if key == "deleted":
        return "yes" if row.get("deleted") else "no"
    if key == "manual":
        return "yes" if row.get("manual") else "no"
    if key == "gbp":
        if row.get("gbp") is not None and "gbp" in row:
            return str(row.get("gbp") or "")
        fields = value_fields(row)
        entry = overrides.get((str(row.get("id") or ""), fields[0])) if fields else None
        return "" if entry is None else str(entry.get("new") or "")
    return str(row.get(key) or "")


def _read_csv(data: bytes) -> List[Dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DeskError("The edited file must be UTF-8 text or a spreadsheet.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "id" not in reader.fieldnames:
        raise DeskError("The edited file needs an id column.")
    return [
        {str(key): str(value or "") for key, value in raw.items() if key}
        for raw in reader
        if any(str(value or "").strip() for value in raw.values())
    ]


def _read_xlsx(data: bytes) -> List[Dict[str, str]]:
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = book.active
    rows = list(sheet.iter_rows(values_only=True))
    book.close()
    if not rows:
        raise DeskError("The spreadsheet is empty.")
    header = [str(cell or "").strip() for cell in rows[0]]
    if "id" not in header:
        raise DeskError("The edited file needs an id column.")
    found = []
    for raw in rows[1:]:
        item = {
            header[index]: "" if cell is None else str(cell)
            for index, cell in enumerate(raw)
            if index < len(header)
        }
        if any(item.values()):
            found.append(item)
    return found


def _tag_choices() -> List[Dict[str, str]]:
    from .rules import defi_tags  # pylint: disable=import-outside-toplevel

    found: Dict[str, Dict[str, str]] = {}
    for item in catalogue():
        found[str(item["id"])] = {
            "id": str(item["id"]),
            "label": str(item.get("label") or item["id"]),
        }
    for item in defi_tags():
        found.setdefault(item["id"], {"id": item["id"], "label": item["label"]})
    return list(found.values())


def _sort(rows: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    if sort == "date_asc":
        return sorted(
            rows,
            key=lambda row: (str(row.get("filter_date") or ""), str(row.get("timestamp") or "")),
        )
    return sorted(
        rows,
        key=lambda row: (str(row.get("filter_date") or ""), str(row.get("timestamp") or "")),
        reverse=True,
    )


def _deleted_ok(row: Mapping[str, Any], wanted: str) -> bool:
    deleted = bool(row.get("deleted"))
    if wanted == "yes":
        return deleted
    if wanted == "any":
        return True
    return not deleted


def _spam_ok(row: Mapping[str, Any], wanted: str) -> bool:
    spam = _spam_row(row)
    if wanted == "yes":
        return spam
    if wanted == "no":
        return not spam
    return True


def _spam_row(row: Mapping[str, Any]) -> bool:
    return (
        bool(row.get("spam_listed"))
        or str(row.get("status") or "") == "spam"
        or str(row.get("type") or "") == "_Possible-Spam"
    )


def _manual_ok(row: Mapping[str, Any], wanted: str) -> bool:
    manual = bool(row.get("manual"))
    if wanted == "yes":
        return manual
    if wanted == "no":
        return not manual
    return True


def _warning_ok(row: Mapping[str, Any], warning: str, issues: str) -> bool:
    return _one_warning(row, warning) and _one_warning(row, issues)


def _one_warning(row: Mapping[str, Any], wanted: str) -> bool:
    codes = set(row.get("warnings") or [])
    if not wanted:
        return True
    if wanted == "any":
        return bool(codes)
    return wanted in codes


def _date_ok(day: str, chosen: Mapping[str, str]) -> bool:
    if chosen.get("date_from") and (not day or day < chosen["date_from"]):
        return False
    if chosen.get("date_to") and (not day or day > chosen["date_to"]):
        return False
    return True


def _import_date_ok(day: str, chosen: Mapping[str, str]) -> bool:
    if chosen.get("import_date_from") and (not day or day < chosen["import_date_from"]):
        return False
    if chosen.get("import_date_to") and (not day or day > chosen["import_date_to"]):
        return False
    return True


def _query_ok(row: Mapping[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("note", "description", "tx_hash", "wallet", "type", "tag", "id")
    ).casefold()
    return query.casefold() in haystack


def _range_ok(amount: Optional[Decimal], low: str, high: str) -> bool:
    if not low and not high:
        return True
    if amount is None:
        return False
    if low and amount < _money(low):
        return False
    if high and amount > _money(high):
        return False
    return True


def _contains(value: object, needle: str) -> bool:
    if not needle:
        return True
    return needle.casefold() in str(value or "").casefold()


def _equals(value: object, needle: str) -> bool:
    if not needle:
        return True
    return str(value or "").casefold() == needle.casefold()


def _quantity(row: Mapping[str, Any]) -> Optional[Decimal]:
    for key in ("buy_quantity", "sell_quantity", "fee_quantity"):
        if str(row.get(key) or ""):
            return _money(row.get(key))
    return None


def _net(row: Mapping[str, Any]) -> Optional[Decimal]:
    for key in ("sell_value", "buy_value"):
        if str(row.get(key) or ""):
            return _money(row.get(key))
    return None


def _chain(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("chain") or "").casefold().strip()
    if explicit in _EXPLORERS:
        return explicit
    tokens = _wallet_tokens(str(row.get("wallet") or ""))
    for name in (
        "ethereum",
        "eth",
        "solana",
        "sol",
        "arbitrum",
        "optimism",
        "polygon",
        "avalanche",
        "avax",
        "bsc",
        "bnb",
        "base",
    ):
        if name in tokens and name in _EXPLORERS:
            return name
    for name in _CHAIN_WORDS:
        if name in tokens:
            return name
    return ""


def _wallet_tokens(wallet: str) -> set:
    cleaned = "".join(char if char.isalnum() else " " for char in wallet.casefold())
    return set(cleaned.split())


def _solana_hash(value: str) -> bool:
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return 32 <= len(value) <= 88 and all(char in alphabet for char in value)


def _pretty(day: str) -> str:
    try:
        when = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day or "Undated"
    return f"{when.day} {_MONTHS[when.month - 1]} {when.year}"


def _in_year(day: str, year: int) -> bool:
    try:
        when = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return tax_year_containing(when) == year


def _row_year(row: Mapping[str, Any]) -> int:
    text = str(row.get("timestamp") or row.get("date") or "")[:10]
    try:
        when = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return tax_year_containing(datetime.now().date())
    return tax_year_containing(when)


def _timestamp(value: str) -> str:
    text = " ".join(value.split())
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%Y-%m-%d":
            return parsed.strftime("%Y-%m-%d 00:00:00")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    raise DeskError("Enter the date as YYYY-MM-DD.")


def _gbp(value: str) -> str:
    text = value.strip().replace(",", "").replace("£", "")
    if not text:
        return ""
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise DeskError("Enter the GBP value as a number, or leave it blank.") from exc
    return _plain(amount)


def _need(quantity: str, asset: str, message: str) -> None:
    if not quantity or not asset:
        raise DeskError(message)
    try:
        Decimal(str(quantity).replace(",", ""))
    except InvalidOperation as exc:
        raise DeskError("Enter the amount as a number.") from exc


def _sum(rows: Sequence[Mapping[str, Any]], key: str) -> Decimal:
    return sum((_money(row.get(key)) for row in rows), Decimal(0))


def _fiat(asset: str) -> bool:
    return asset.upper() in _FIAT


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", "").replace("£", ""))
    except InvalidOperation:
        return Decimal(0)


def _plain(amount: Decimal) -> str:
    if amount == 0:
        return ""
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or ""


def _unique(rows: Sequence[Mapping[str, Any]], key: str) -> List[str]:
    return sorted({str(row.get(key) or "") for row in rows if row.get(key)})


def _assets(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    found: Set[str] = set()
    for row in rows:
        found.update(str(asset) for asset in row.get("assets") or [] if asset)
    return sorted(found)


def _tags(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    found = {str(row.get("tag") or "") for row in rows if row.get("tag")}
    found.update(str(item["id"]) for item in catalogue())
    return sorted(found)


def _first(query: Mapping[str, Sequence[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""


def _page_number(value: str) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def _per_page(value: str) -> int:
    try:
        number = int(value or "50")
    except ValueError:
        return 50
    if number in _PAGE_SIZES:
        return number
    return 50
