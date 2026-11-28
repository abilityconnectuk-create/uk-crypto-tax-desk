# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
# pylint: disable=protected-access
"""Check Coinbase, KuCoin and Revolut X exports against imported Koinly rows.

The conversion tool's parsers read the files. Transfer reconciliation is not
run, so a withdrawal stays a withdrawal. Nothing is written to Koinly. A row
enters the tax report only after it is accepted on Review.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import re
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from ..bt_types import BUY_TYPES, SELL_TYPES, TrType
from ..conv.bittytax_conv import _do_read_file, _get_file_info
from ..conv.datafile import DataFile
from ..conv.dataparser import DataParser
from ..conv.exceptions import (
    DataFilenameError,
    DataFormatNotSupported,
    DataFormatUnrecognised,
    DataRowError,
)
from ..conv.output_csv import OutputCsv
from .convert import _CONVERT_LOCK, _isolated_datafiles, allowed_types
from .errors import DeskError
from .events import append_event
from .sources import _rows
from .store import _new_id, _read_json, _write_json
from .taxyear import tax_year_containing

EXCHANGES = ("Coinbase", "KuCoin", "Revolut X")
TIME_TOLERANCE_SECONDS = 120
_AMOUNT_ABS = Decimal("0.00000001")
_AMOUNT_RATIO = Decimal("0.0001")
_WIDTH = 13
_TYPE = 0
_BUY_QTY = 1
_BUY_ASSET = 2
_SELL_QTY = 4
_SELL_ASSET = 5
_FEE_QTY = 7
_FEE_ASSET = 8
_WALLET = 10
_TIMESTAMP = 11
_FORMATS = {
    "Coinbase": "Coinbase",
    "KuCoin Trades": "KuCoin",
    "KuCoin Deposits": "KuCoin",
    "KuCoin Withdrawals": "KuCoin",
    "Revolut": "Revolut X",
}
_WALLETS = {"Coinbase": "Coinbase", "KuCoin": "KuCoin", "Revolut X": "Revolut"}
_KEYS = {"Coinbase": "coinbase", "KuCoin": "kucoin", "Revolut X": "revolut"}
_IN = {item.value for item in BUY_TYPES if item is not TrType.TRADE}
_OUT = {item.value for item in SELL_TYPES if item is not TrType.TRADE}
_TYPE_COLS = ("Transaction Type", "Type", "Side", "side", "direction")
_ASSET_COLS = ("Asset", "Symbol", "Coin", "Currency")
_QTY_COLS = ("Quantity Transacted", "Quantity", "Amount", "Filled Amount", "size")
_QUIET = {
    "notes",
    "remark",
    "remarks",
    "hash",
    "wallet address",
    "wallet address/account",
    "withdrawal address/account",
    "deposit address",
}
_SECRET = re.compile(r"0x[0-9A-Fa-f]{6,}|[1-9A-HJ-NP-Za-km-z]{26,}")
_EXPECTED = (
    "Expected a Coinbase transaction statement, a KuCoin spot trade history, "
    "deposit or withdrawal export, or a Revolut X statement."
)


def held_upload(filename: str, data: bytes) -> Optional[Dict[str, str]]:
    """Detection payload when the file is one of the exchange checks."""
    found = identify(filename, data)
    if found["ok"] != "yes":
        return None
    noted = dict(found)
    noted["note"] = (
        "This file is held for the exchange check. "
        "It is not added to the tax report until you accept it on Review."
    )
    return noted


def staged_location(desk: Any, filename: str, data: bytes, who: str) -> Tuple[str, str]:
    """Hold an exchange file. Return the redirect, or an error and no redirect."""
    found = identify(filename, data)
    if found["ok"] != "yes":
        return "", found["error"]
    entry = stage_import(desk, filename, data, who)
    again = "&again=1" if entry.get("idempotent") else ""
    name = quote(str(entry["exchange"]))
    return f"/exchanges?tax_year={entry['tax_year']}&exchange={name}{again}", ""


def identify(filename: str, data: bytes) -> Dict[str, str]:
    """Name the exchange export, or say why the header is not one of them."""
    if not data:
        return _no("The file is empty.")
    try:
        rows = _rows(filename, data)
    except (UnicodeError, ValueError, csv.Error, OSError):
        return _no("That file could not be read as a CSV or a spreadsheet.")
    widest: List[str] = []
    with _CONVERT_LOCK:
        for index, row in enumerate(rows):
            cells = [str(cell).strip() for cell in row]
            if not any(cells):
                continue
            if len(cells) > len(widest):
                widest = cells
            try:
                parser = DataParser.match_header(cells, index)
            except KeyError:
                continue
            if parser.name in _FORMATS:
                return {
                    "ok": "yes",
                    "name": parser.name,
                    "exchange": _FORMATS[parser.name],
                    "group": "Exchange check",
                    "error": "",
                    "header": ", ".join(cells),
                }
            return _no(_rejected(parser.name), parser.name, ", ".join(cells))
    return _no(_unknown(widest), header=", ".join(widest))


def stage_import(desk: Any, filename: str, data: bytes, who: str = "local") -> Dict[str, Any]:
    """Hold an exchange export for comparison. It is not added to the ledger."""
    found = identify(filename, data)
    if found["ok"] != "yes":
        raise DeskError(found["error"])
    mapped, unclear, skipped, format_name = _parse_bytes(filename, data)
    if format_name not in _FORMATS:
        raise DeskError(_rejected(format_name or "unknown"))
    exchange = _FORMATS[format_name]
    digest = hashlib.sha256(data).hexdigest()
    with desk._lock:
        store = _load(desk.root)
        for entry in store["imports"]:
            if entry.get("sha256") == digest:
                public = _public(entry, True)
                append_event(
                    desk.root,
                    "exchange-import",
                    who,
                    "Same exchange file already imported. No rows were added.",
                    {"filename": _safe_name(filename), "import_id": public["id"]},
                )
                return public
        import_id = _new_id()
        folder = desk.root / "exchange-raw" / import_id
        folder.mkdir(parents=True, exist_ok=False)
        raw_path = folder / _safe_name(filename)
        raw_path.write_bytes(data)
        rows = _staged_rows(import_id, mapped, unclear, _WALLETS[exchange])
        entry = {
            "id": import_id,
            "filename": raw_path.name,
            "sha256": digest,
            "exchange": exchange,
            "format": format_name,
            "wallet": _WALLETS[exchange],
            "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "skipped": skipped,
            "tax_year": _first_year(rows),
            "rows": rows,
        }
        store["imports"].append(entry)
        _save(desk.root, store)
        append_event(
            desk.root,
            "exchange-import",
            who,
            f"Held {raw_path.name} for the exchange check.",
            {
                "import_id": import_id,
                "exchange": exchange,
                "rows": str(len(rows)),
                "skipped": str(skipped),
            },
        )
    return _public(entry, False)


def accept_row(desk: Any, row_id: str, who: str, choice: Mapping[str, str]) -> str:
    """Copy one held row into the ledger. The caller has chosen any missing type."""
    with desk._lock:
        store = _load(desk.root)
        found = _find(store, row_id)
        if found is None:
            raise DeskError("That exchange row is not on the review queue.")
        _entry, row = found
        if row.get("accepted"):
            raise DeskError("That row is already in the tax report.")
        cells = _cells_for(row, choice)
        ledger_id = _append_ledger(desk, cells, who)
        row["accepted"] = True
        row["ledger_id"] = ledger_id
        _save(desk.root, store)
    return ledger_id


def compare(desk: Any, year: int, exchange: str) -> Dict[str, Any]:
    """Rows that do not agree, plus how many already match or were accepted."""
    key = _KEYS.get(exchange, "")
    exchange_legs, unclear, accepted = _exchange_side(desk, year, exchange)
    koinly_legs = _koinly_side(desk, year, key)
    exchange_legs = _dedupe(exchange_legs)
    koinly_legs = _dedupe(koinly_legs)
    open_rows, matched = _pair(exchange_legs, koinly_legs, year)
    open_rows.extend(_unclear_rows(unclear, year))
    open_rows.sort(key=lambda item: (item["timestamp"], item["kind"], item["title"]))
    return {"open": open_rows, "matched": matched, "accepted": accepted}


def open_gaps(desk: Any, year: int) -> List[Dict[str, str]]:
    """One Ready to file item for each open difference."""
    found = []
    for exchange in EXCHANGES:
        for row in compare(desk, year, exchange)["open"]:
            found.append(
                {
                    "name": f"{exchange}: {row['title']}",
                    "detail": row["detail"],
                    "href": row["href"],
                }
            )
    return found


def review_items(desk: Any, year: int) -> List[Dict[str, str]]:
    """Exchange rows waiting to be accepted. Mismatches stay on the comparison."""
    items = []
    for exchange in EXCHANGES:
        for row in compare(desk, year, exchange)["open"]:
            if row["kind"] not in {"exchange_only", "unclear"}:
                continue
            items.append(
                {
                    "id": row["id"],
                    "kind": "exchange",
                    "needs_type": "yes" if row["kind"] == "unclear" else "no",
                    "timestamp": row["timestamp"],
                    "asset": row["asset"],
                    "quantity": row["quantity"],
                    "wallet": row["wallet"],
                    "destination": "",
                    "tx_hash": "",
                    "description": row["detail"],
                    "disposition": "exchange review",
                    "exchange": exchange,
                }
            )
    return items


def show_comparison(handler: Any, desk: Any, year: int, exchange: str, again: bool) -> None:
    """Render the exchange comparison."""
    handler._page(
        "exchanges.html",
        "Exchange vs Koinly",
        section="exchanges",
        view=page(desk, year, exchange),
        tax_year=year,
        again=again,
    )


def page(desk: Any, year: int, exchange: str) -> Dict[str, Any]:
    """Comparison for one exchange, with a count for each exchange."""
    chosen = exchange if exchange in EXCHANGES else EXCHANGES[0]
    view = compare(desk, year, chosen)
    counts = []
    for name in EXCHANGES:
        counts.append({"name": name, "count": str(len(compare(desk, year, name)["open"]))})
    return {
        "exchange": chosen,
        "exchanges": counts,
        "rows": view["open"],
        "matched": view["matched"],
        "accepted": view["accepted"],
        "imports": _import_list(desk, chosen),
        "tolerance": "2 minutes",
    }


def _no(message: str, name: str = "", header: str = "") -> Dict[str, str]:
    return {
        "ok": "no",
        "name": name,
        "exchange": "",
        "group": "",
        "error": message,
        "header": header,
    }


def _unknown(header: Sequence[str]) -> str:
    cols = ", ".join(header) if header else "none"
    return f"Unknown header: {cols}. {_EXPECTED}"


def _rejected(name: str) -> str:
    return f"This file matches {name}. This check does not import it. {_EXPECTED}"


def _parse_bytes(
    filename: str, data: bytes
) -> Tuple[List[List[str]], List[Dict[str, str]], int, str]:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xls", ".xlsx"}:
        suffix = ".csv"
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            path = handle.name
        return _parse_path(path)
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def _parse_path(path: str) -> Tuple[List[List[str]], List[Dict[str, str]], int, str]:
    args = argparse.Namespace(unconfirmed=False, cryptoasset=None)
    with _CONVERT_LOCK:
        with _isolated_datafiles():
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    file_type, _digest = _get_file_info(path)
                    _do_read_file(file_type, path, args)
            except DataFormatUnrecognised as exc:
                raise DeskError(f"Unrecognised file: {exc} {_EXPECTED}") from exc
            except DataFormatNotSupported as exc:
                raise DeskError(f"File format is not supported: {exc}") from exc
            except DataFilenameError as exc:
                raise DeskError(str(exc)) from exc
            except (UnicodeDecodeError, csv.Error, OSError) as exc:
                raise DeskError("The file is not encoded as UTF-8 text or a spreadsheet.") from exc
            if not DataFile.data_files_ordered:
                raise DeskError(f"No rows were recognised. {_EXPECTED}")
            return _snapshot(list(DataFile.data_files_ordered))


def _snapshot(
    files: Sequence[Any],
) -> Tuple[List[List[str]], List[Dict[str, str]], int, str]:
    mapped: List[List[str]] = []
    unclear: List[Dict[str, str]] = []
    skipped = 0
    names: List[str] = []
    known = set(allowed_types())
    for data_file in files:
        name = str(data_file.parser.name)
        names.append(name)
        if name not in _FORMATS:
            raise DeskError(_rejected(name))
        wallet = _WALLETS[_FORMATS[name]]
        for data_row in data_file.data_rows:
            if not any(str(cell).strip() for cell in data_row.row):
                continue
            record = data_row.t_record
            if record is not None:
                cells = OutputCsv._to_bittytax_csv(record)
                if cells[_TYPE] in known:
                    mapped.append(cells)
                    continue
            if data_row.failure is not None or record is not None:
                unclear.append(_unclear_from(data_row, wallet))
                continue
            skipped += 1
    if not names:
        raise DeskError(f"No rows were recognised. {_EXPECTED}")
    return mapped, unclear, skipped, names[0]


def _unclear_from(data_row: Any, wallet: str) -> Dict[str, str]:
    if data_row.failure is not None:
        message = _public_failure(data_row.failure)
    else:
        message = "This row was not mapped to a known transaction type."
    return {
        "timestamp": _row_time(data_row),
        "asset": _column(data_row, _ASSET_COLS)[:32],
        "quantity": _column(data_row, _QTY_COLS)[:32],
        "raw_type": _column(data_row, _TYPE_COLS)[:80],
        "description": message,
        "wallet": wallet,
    }


def _column(data_row: Any, names: Sequence[str]) -> str:
    row = data_row.row_dict if isinstance(data_row.row_dict, dict) else {}
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _row_time(data_row: Any) -> str:
    stamp = data_row.timestamp
    if stamp is None or getattr(stamp, "year", 9999) >= 9999:
        return ""
    return OutputCsv._format_timestamp(stamp)


def _public_failure(failure: BaseException) -> str:
    if isinstance(failure, DataRowError) and failure.col_name.casefold() in _QUIET:
        return f"Unexpected {failure.col_name}."
    text = _SECRET.sub("redacted", str(failure))
    return text[:240]


def _staged_rows(
    import_id: str,
    mapped: Sequence[Sequence[str]],
    unclear: Sequence[Mapping[str, str]],
    wallet: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for cells in mapped:
        rows.append(_mapped_row(import_id, len(rows), list(cells)))
    for item in unclear:
        rows.append(_unclear_row(import_id, len(rows), item, wallet))
    return rows


def _mapped_row(import_id: str, index: int, cells: List[str]) -> Dict[str, Any]:
    asset, quantity = _primary_text(cells)
    return {
        "id": f"{import_id}-r{index:04d}",
        "kind": "mapped",
        "accepted": False,
        "ledger_id": "",
        "cells": cells,
        "timestamp": cells[_TIMESTAMP],
        "asset": asset,
        "quantity": quantity,
        "raw_type": cells[_TYPE],
        "description": "",
        "wallet": cells[_WALLET],
    }


def _unclear_row(
    import_id: str, index: int, item: Mapping[str, str], wallet: str
) -> Dict[str, Any]:
    return {
        "id": f"{import_id}-r{index:04d}",
        "kind": "unclear",
        "accepted": False,
        "ledger_id": "",
        "cells": [],
        "timestamp": item.get("timestamp") or "",
        "asset": item.get("asset") or "",
        "quantity": item.get("quantity") or "",
        "raw_type": item.get("raw_type") or "",
        "description": item.get("description") or "",
        "wallet": item.get("wallet") or wallet,
    }


def _primary_text(cells: Sequence[str]) -> Tuple[str, str]:
    direction = _direction(cells[_TYPE])
    if direction == "out":
        return cells[_SELL_ASSET], _plain(_decimal(cells[_SELL_QTY]))
    return cells[_BUY_ASSET], _plain(_decimal(cells[_BUY_QTY]))


def _cells_for(row: Mapping[str, Any], choice: Mapping[str, str]) -> List[str]:
    if row.get("kind") == "mapped":
        cells = [str(item) for item in row.get("cells") or []]
        if len(cells) >= _WIDTH and cells[_TYPE] in set(allowed_types()):
            return cells
    return _choice_cells(row, choice)


def _choice_cells(row: Mapping[str, Any], choice: Mapping[str, str]) -> List[str]:
    kind = str(choice.get("type") or "").strip()
    if kind not in set(allowed_types()):
        raise DeskError("Choose a BittyTax transaction type. This row was not guessed.")
    buy_qty = _positive(str(choice.get("buy_quantity") or ""), "received")
    sell_qty = _positive(str(choice.get("sell_quantity") or ""), "sent")
    buy_asset = _asset(str(choice.get("buy_asset") or ""))
    sell_asset = _asset(str(choice.get("sell_asset") or ""))
    _require_pair(buy_qty, buy_asset, "received")
    _require_pair(sell_qty, sell_asset, "sent")
    if not buy_qty and not sell_qty:
        raise DeskError("Enter the quantity that was received or sent.")
    when = str(row.get("timestamp") or "")
    if not when:
        raise DeskError("This row has no timestamp, so it cannot be added.")
    raw = str(row.get("raw_type") or "unknown")
    return [
        kind,
        _plain(buy_qty) if buy_qty else "",
        buy_asset,
        "",
        _plain(sell_qty) if sell_qty else "",
        sell_asset,
        "",
        "",
        "",
        "",
        str(row.get("wallet") or ""),
        when,
        f"Accepted from the exchange file ({raw}).",
    ]


def _require_pair(quantity: Decimal, asset: str, side: str) -> None:
    if quantity and not asset:
        raise DeskError(f"Enter the asset that was {side}.")
    if asset and not quantity:
        raise DeskError(f"Enter the quantity that was {side}.")


def _positive(text: str, side: str) -> Decimal:
    cleaned = text.strip().replace(",", "")
    if not cleaned:
        return Decimal(0)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise DeskError(f"The {side} quantity is not a number.") from exc
    if amount <= 0:
        raise DeskError(f"The {side} quantity must be greater than zero.")
    return amount


def _asset(text: str) -> str:
    cleaned = text.strip().upper()
    if not cleaned:
        return ""
    if re.fullmatch(r"[A-Z0-9]{1,12}", cleaned) is None:
        raise DeskError("An asset code is one to twelve letters or digits.")
    return cleaned


def _append_ledger(desk: Any, cells: List[str], who: str) -> str:
    ledger = _read_json(desk.root / "ledger.json", {"rows": []})
    import_id = _new_id()
    row_id = f"{import_id}:{len(ledger['rows']):04d}"
    ledger["rows"].append({"id": row_id, "import_id": import_id, "cells": cells})
    _write_json(desk.root / "ledger.json", ledger)
    desk._row_cache = None
    desk._write_effective_unlocked()
    append_event(
        desk.root,
        "exchange-accept",
        who,
        "Accepted an exchange row into the tax report.",
        {"row_id": row_id},
    )
    return row_id


def _exchange_side(
    desk: Any, year: int, exchange: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    legs: List[Dict[str, Any]] = []
    unclear: List[Dict[str, Any]] = []
    accepted = 0
    for entry in _load(desk.root)["imports"]:
        if entry.get("exchange") != exchange:
            continue
        for row in entry.get("rows") or []:
            if not isinstance(row, dict):
                continue
            when = _when(str(row.get("timestamp") or ""))
            if row.get("accepted"):
                if _in_year(when, year):
                    accepted += 1
                continue
            if row.get("kind") == "unclear":
                if _in_year(when, year):
                    unclear.append(row)
                continue
            leg = _leg(str(row.get("id") or ""), _review_href(year, str(row.get("id") or "")), row)
            if leg is None or not _in_year(leg["when"], year):
                continue
            legs.append(leg)
    return legs, _dedupe_unclear(unclear), accepted


def _koinly_side(desk: Any, year: int, key: str) -> List[Dict[str, Any]]:
    if not key:
        return []
    allowed = {
        str(item.get("id") or "")
        for item in desk.imports()
        if str(item.get("format") or "").casefold().startswith("koinly")
    }
    legs = []
    for row in desk.all_rows():
        if str(row.get("import_id") or "") not in allowed:
            continue
        if _venue(str(row.get("wallet") or "")) != key:
            continue
        when = _when(str(row.get("timestamp") or ""))
        if not _in_year(when, year):
            continue
        leg = _leg(str(row.get("id") or ""), _tx_href(str(row.get("id") or "")), row)
        if leg is not None:
            legs.append(leg)
    return legs


def _leg(row_id: str, href: str, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    cells = row.get("cells")
    if not isinstance(cells, list):
        cells = [
            str(row.get("type") or ""),
            str(row.get("buy_quantity") or ""),
            str(row.get("buy_asset") or ""),
            str(row.get("buy_value") or ""),
            str(row.get("sell_quantity") or ""),
            str(row.get("sell_asset") or ""),
            str(row.get("sell_value") or ""),
            str(row.get("fee_quantity") or ""),
            str(row.get("fee_asset") or ""),
            str(row.get("fee_value") or ""),
            str(row.get("wallet") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("note") or ""),
        ]
    if len(cells) < _WIDTH:
        return None
    when = _when(str(cells[_TIMESTAMP]))
    if when is None:
        return None
    kind = str(cells[_TYPE])
    return {
        "id": row_id,
        "href": href,
        "timestamp": str(cells[_TIMESTAMP]),
        "when": when,
        "direction": _direction(kind),
        "type": kind,
        "buy_quantity": _decimal(cells[_BUY_QTY]),
        "buy_asset": str(cells[_BUY_ASSET]).strip(),
        "sell_quantity": _decimal(cells[_SELL_QTY]),
        "sell_asset": str(cells[_SELL_ASSET]).strip(),
        "fee_quantity": _decimal(cells[_FEE_QTY]),
        "fee_asset": str(cells[_FEE_ASSET]).strip(),
        "wallet": str(cells[_WALLET]).strip(),
    }


def _pair(
    exchange_legs: Sequence[Mapping[str, Any]],
    koinly_legs: Sequence[Mapping[str, Any]],
    year: int,
) -> Tuple[List[Dict[str, str]], int]:
    used: Set[int] = set()
    open_rows: List[Dict[str, str]] = []
    matched = 0
    for leg in exchange_legs:
        choice = _best(leg, koinly_legs, used)
        if choice is None:
            open_rows.append(_only("exchange_only", leg, year))
            continue
        used.add(choice)
        other = koinly_legs[choice]
        difference = _difference(leg, other)
        if difference:
            open_rows.append(difference)
        else:
            matched += 1
    for index, other in enumerate(koinly_legs):
        if index not in used:
            open_rows.append(_only("koinly_only", other, year))
    return open_rows, matched


def _best(
    leg: Mapping[str, Any], koinly_legs: Sequence[Mapping[str, Any]], used: Set[int]
) -> Optional[int]:
    chosen: Optional[int] = None
    score: Optional[Tuple[float, Decimal]] = None
    for index, other in enumerate(koinly_legs):
        if index in used or not _candidate(leg, other):
            continue
        seconds = abs((leg["when"] - other["when"]).total_seconds())
        current = (seconds, _gap(leg, other))
        if score is None or current < score:
            chosen = index
            score = current
    return chosen


def _difference(left: Mapping[str, Any], right: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    if not _amounts_close(left, right):
        return _mismatch("amount", left, right)
    if not _fees_close(left, right):
        return _mismatch("fee", left, right)
    return None


def _only(kind: str, leg: Mapping[str, Any], year: int) -> Dict[str, str]:
    title = _describe(leg)
    if kind == "koinly_only":
        return _open_row(
            leg,
            kind,
            f"Koinly only: {title}",
            "Koinly has this row and the exchange file does not.",
            str(leg["href"]),
        )
    return _open_row(
        leg,
        kind,
        f"Exchange only: {title}",
        "The exchange file has this row and Koinly does not.",
        _review_href(year, str(leg["id"])),
    )


def _mismatch(kind: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, str]:
    title = _describe(left)
    if kind == "amount":
        detail = f"Exchange {_side(left)}. Koinly {_side(right)}."
        label = f"Amount mismatch: {title}"
    else:
        detail = f"Exchange fee {_fee_text(left)}. Koinly fee {_fee_text(right)}."
        label = f"Fee mismatch: {title}"
    return _open_row(left, kind, label, detail, str(right["href"]))


def _unclear_rows(rows: Sequence[Mapping[str, Any]], year: int) -> List[Dict[str, str]]:
    found = []
    for row in rows:
        raw = str(row.get("raw_type") or "row")
        asset = str(row.get("asset") or "")
        title = f"Needs a type: {raw} {asset}".strip()
        detail = str(row.get("description") or "This row was not given a transaction type.")
        found.append(
            {
                "id": str(row.get("id") or ""),
                "kind": "unclear",
                "title": title,
                "detail": detail,
                "href": _review_href(year, str(row.get("id") or "")),
                "timestamp": str(row.get("timestamp") or ""),
                "asset": asset,
                "quantity": str(row.get("quantity") or ""),
                "wallet": str(row.get("wallet") or ""),
            }
        )
    return found


def _open_row(
    leg: Mapping[str, Any], kind: str, title: str, detail: str, href: str
) -> Dict[str, str]:
    asset, quantity = _shown(leg)
    return {
        "id": str(leg["id"]),
        "kind": kind,
        "title": title,
        "detail": detail,
        "href": href,
        "timestamp": str(leg["timestamp"]),
        "asset": asset,
        "quantity": quantity,
        "wallet": str(leg["wallet"]),
    }


def _shown(leg: Mapping[str, Any]) -> Tuple[str, str]:
    if leg["direction"] == "out":
        return str(leg["sell_asset"]), _plain(leg["sell_quantity"])
    return str(leg["buy_asset"]), _plain(leg["buy_quantity"])


def _describe(leg: Mapping[str, Any]) -> str:
    kind = str(leg["type"])
    if leg["direction"] == "trade":
        return (
            f"{kind} {_plain(leg['sell_quantity'])} {leg['sell_asset']}"
            f" for {_plain(leg['buy_quantity'])} {leg['buy_asset']}"
        )
    if leg["direction"] == "in":
        return f"{kind} {_plain(leg['buy_quantity'])} {leg['buy_asset']}"
    if leg["direction"] == "out":
        return f"{kind} {_plain(leg['sell_quantity'])} {leg['sell_asset']}"
    return kind


def _side(leg: Mapping[str, Any]) -> str:
    if leg["direction"] == "trade":
        return (
            f"{_plain(leg['buy_quantity'])} {leg['buy_asset']}"
            f" for {_plain(leg['sell_quantity'])} {leg['sell_asset']}"
        )
    if leg["direction"] == "in":
        return f"{_plain(leg['buy_quantity'])} {leg['buy_asset']}"
    return f"{_plain(leg['sell_quantity'])} {leg['sell_asset']}"


def _fee_text(leg: Mapping[str, Any]) -> str:
    quantity, asset = _fee_pair(leg)
    if quantity == 0:
        return "no fee"
    return f"{_plain(quantity)} {asset.upper()}"


def _candidate(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if not _same_shape(left, right):
        return False
    seconds = abs((left["when"] - right["when"]).total_seconds())
    return seconds <= TIME_TOLERANCE_SECONDS


def _same_event(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    seconds = abs((left["when"] - right["when"]).total_seconds())
    return (
        seconds <= TIME_TOLERANCE_SECONDS
        and _same_shape(left, right)
        and _amounts_close(left, right)
    )


def _same_shape(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if not left["direction"] or left["direction"] != right["direction"]:
        return False
    if left["direction"] == "trade":
        return _asset_eq(left["buy_asset"], right["buy_asset"]) and _asset_eq(
            left["sell_asset"], right["sell_asset"]
        )
    if left["direction"] == "in":
        return _asset_eq(left["buy_asset"], right["buy_asset"])
    return _asset_eq(left["sell_asset"], right["sell_asset"])


def _asset_eq(left: str, right: str) -> bool:
    return bool(left) and left.casefold() == right.casefold()


def _amounts_close(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["direction"] == "trade":
        return _close(left["buy_quantity"], right["buy_quantity"]) and _close(
            left["sell_quantity"], right["sell_quantity"]
        )
    if left["direction"] == "in":
        return _close(left["buy_quantity"], right["buy_quantity"])
    return _close(left["sell_quantity"], right["sell_quantity"])


def _fees_close(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_qty, left_asset = _fee_pair(left)
    right_qty, right_asset = _fee_pair(right)
    if left_qty == 0 and right_qty == 0:
        return True
    return left_asset == right_asset and _close(left_qty, right_qty)


def _fee_pair(leg: Mapping[str, Any]) -> Tuple[Decimal, str]:
    quantity = leg["fee_quantity"]
    asset = str(leg["fee_asset"]).casefold()
    if quantity == 0 or not asset:
        return Decimal(0), ""
    return quantity, asset


def _gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> Decimal:
    if left["direction"] == "in":
        return abs(left["buy_quantity"] - right["buy_quantity"])
    if left["direction"] == "out":
        return abs(left["sell_quantity"] - right["sell_quantity"])
    return abs(left["buy_quantity"] - right["buy_quantity"]) + abs(
        left["sell_quantity"] - right["sell_quantity"]
    )


def _close(left: Decimal, right: Decimal) -> bool:
    gap = abs(left - right)
    if gap <= _AMOUNT_ABS:
        return True
    scale = max(abs(left), abs(right), Decimal(1))
    return gap / scale <= _AMOUNT_RATIO


def _dedupe(legs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for leg in sorted(legs, key=lambda item: (item["when"], str(item["id"]))):
        if any(_same_event(leg, other) for other in kept):
            continue
        kept.append(dict(leg))
    return kept


def _dedupe_unclear(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = (
            str(row.get("timestamp") or "")[:19],
            str(row.get("raw_type") or ""),
            str(row.get("asset") or ""),
            str(row.get("quantity") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(dict(row))
    return kept


def _direction(kind: str) -> str:
    if kind == TrType.TRADE.value:
        return "trade"
    if kind in _IN:
        return "in"
    if kind in _OUT:
        return "out"
    return ""


def _when(text: str) -> Optional[datetime]:
    head = text.strip()[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _in_year(when: Optional[datetime], year: int) -> bool:
    if when is None:
        return True
    return tax_year_containing(when.date()) == year


def _venue(wallet: str) -> str:
    text = re.sub(r"[^a-z0-9]", "", wallet.casefold())
    if text.startswith("coinbase") and not text.startswith(("coinbasepro", "coinbaseprime")):
        return "coinbase"
    if text.startswith("kucoin"):
        return "kucoin"
    if text.startswith("revolut"):
        return "revolut"
    return ""


def _decimal(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def _plain(amount: Decimal) -> str:
    text = format(amount.normalize(), "f")
    if text == "-0":
        return "0"
    return text


def _review_href(year: int, row_id: str) -> str:
    return f"/review?tax_year={year}#row-{row_id}"


def _tx_href(row_id: str) -> str:
    import_id, _sep, seq = row_id.rpartition(":")
    if not import_id or not seq:
        return "/transactions"
    return f"/transactions/{import_id}/{seq}"


def _first_year(rows: Sequence[Mapping[str, Any]]) -> int:
    for row in rows:
        when = _when(str(row.get("timestamp") or ""))
        if when is not None:
            return tax_year_containing(when.date())
    return tax_year_containing(date.today())


def _import_list(desk: Any, exchange: str) -> List[Dict[str, str]]:
    found = []
    for entry in _load(desk.root)["imports"]:
        if entry.get("exchange") != exchange:
            continue
        found.append(
            {
                "filename": str(entry.get("filename") or ""),
                "format": str(entry.get("format") or ""),
                "rows": str(len(entry.get("rows") or [])),
                "skipped": str(entry.get("skipped") or 0),
                "imported_at": str(entry.get("imported_at") or ""),
            }
        )
    return found


def _public(entry: Mapping[str, Any], idempotent: bool) -> Dict[str, Any]:
    rows = entry.get("rows") or []
    return {
        "id": str(entry.get("id") or ""),
        "filename": str(entry.get("filename") or ""),
        "exchange": str(entry.get("exchange") or ""),
        "format": str(entry.get("format") or ""),
        "tax_year": int(entry.get("tax_year") or tax_year_containing(date.today())),
        "idempotent": idempotent,
        "rows": len(rows) if isinstance(rows, list) else 0,
        "skipped": int(entry.get("skipped") or 0),
    }


def _find(store: Mapping[str, Any], row_id: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for entry in store["imports"]:
        if not isinstance(entry, dict):
            continue
        for row in entry.get("rows") or []:
            if isinstance(row, dict) and row.get("id") == row_id:
                return entry, row
    return None


def _load(root: Path) -> Dict[str, Any]:
    path = root / "exchanges.json"
    if not path.is_file():
        return {"imports": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeskError("The exchange import file could not be read.") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("imports"), list):
        raise DeskError("The exchange import file could not be read.")
    return loaded


def _save(root: Path, payload: Mapping[str, Any]) -> None:
    path = root / "exchanges.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name("exchanges.json.tmp")
    with temporary.open("wb") as handle:
        handle.write(text.encode("utf-8"))
    temporary.replace(path)


def _safe_name(filename: str) -> str:
    name = Path(filename or "upload.csv").name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return "upload.csv"
    return name
