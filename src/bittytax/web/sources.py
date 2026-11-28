# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Match an upload to a conversion parser by its header row. The file is not stored."""

from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional, Sequence

from ..conv.dataparser import DataParser, ParserType
from .convert import _CONVERT_LOCK  # pylint: disable=protected-access

_GROUP = {
    ParserType.EXCHANGE: ("exchange", "Exchanges"),
    ParserType.WALLET: ("wallet", "Wallets"),
    ParserType.EXPLORER: ("blockchain", "Blockchains"),
}


def parser_catalogue() -> List[Dict[str, str]]:
    """Unique parser names, for the add-data list."""
    with _CONVERT_LOCK:
        found: Dict[str, Dict[str, str]] = {}
        for parser in DataParser.parsers:
            key = parser.name.casefold()
            if key in found:
                continue
            group_id, group = _GROUP.get(parser.p_type, ("other", parser.p_type.value))
            found[key] = {"name": parser.name, "group_id": group_id, "group": group}
    return sorted(found.values(), key=lambda item: item["name"].casefold())


def detect_upload(filename: str, data: bytes) -> Dict[str, str]:
    """Return the parser name for ``data``, or an empty name when nothing matches."""
    if not data:
        return _miss("The file is empty.")
    try:
        rows = _rows(filename, data)
    except (UnicodeError, ValueError, csv.Error, OSError):
        return _miss("That file could not be read as a CSV or a spreadsheet.")
    with _CONVERT_LOCK:
        parser = _match(rows)
    if parser is None:
        return _miss("No parser recognised those headers.")
    _group_id, group = _GROUP.get(parser.p_type, ("other", parser.p_type.value))
    return {"ok": "yes", "name": parser.name, "group": group, "error": ""}


def _miss(message: str) -> Dict[str, str]:
    return {"ok": "no", "name": "", "group": "", "error": message}


def _rows(filename: str, data: bytes) -> List[List[str]]:
    name = filename.lower()
    if name.endswith(".xlsx") or data[:4] == b"PK\x03\x04":
        return _xlsx_rows(data)
    if name.endswith(".xls") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return _xls_rows(data)
    return _csv_rows(data)


def _csv_rows(data: bytes) -> List[List[str]]:
    text = data.decode("utf-8-sig")
    best: List[List[str]] = []
    for delimiter in (",", ";", "\t"):
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = []
        for index, row in enumerate(reader):
            if index >= 20:
                break
            rows.append([cell.strip() for cell in row])
        width = len(rows[0]) if rows else 0
        best_width = len(best[0]) if best else 0
        if width > best_width:
            best = rows
    return best


def _xlsx_rows(data: bytes) -> List[List[str]]:
    import openpyxl  # pylint: disable=import-outside-toplevel

    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        rows = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= 20:
                break
            rows.append(["" if cell is None else str(cell).strip() for cell in row])
        return rows
    finally:
        book.close()


def _xls_rows(data: bytes) -> List[List[str]]:
    import xlrd  # pylint: disable=import-outside-toplevel

    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    rows = []
    for index in range(min(20, sheet.nrows)):
        rows.append([str(sheet.cell_value(index, col)).strip() for col in range(sheet.ncols)])
    return rows


def _match(rows: Sequence[Sequence[str]]) -> Optional[DataParser]:
    for index, row in enumerate(rows):
        cells = list(row)
        if not any(cells):
            continue
        try:
            return DataParser.match_header(cells, index)
        except KeyError:
            continue
    return None
