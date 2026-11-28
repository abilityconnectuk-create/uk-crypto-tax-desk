# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Per-file import report. Cell values are redacted before they are shown."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..conv.dataparser import DataParser
from .convert import _CONVERT_LOCK  # pylint: disable=protected-access

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
_SLASH = re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$")
_TEXT_DATE = re.compile(r"^\d{1,2} [A-Za-z]{3,9} \d{4}")
_THOUSAND = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_EURO = re.compile(r"^-?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
_HEX = re.compile(r"0x[0-9a-fA-F]{8,}")


def redact(value: str) -> str:
    """Keep the shape of a cell and drop the characters."""
    cleaned = _HEX.sub("0x", value)
    chars = []
    for char in cleaned[:80]:
        if char.isdigit():
            chars.append("#")
        elif char.isalpha():
            chars.append("A")
        else:
            chars.append(char)
    return "".join(chars)


def inspect_bytes(filename: str, data: bytes) -> Dict[str, Any]:
    """Describe delimiter, blanks, dates, numbers and unknown columns."""
    if _spreadsheet(filename, data):
        return _empty_report(filename, ["spreadsheet"])
    text, notes = _decode(data)
    delimiter = _delimiter(text)
    if delimiter != ",":
        notes.append(f"delimiter {delimiter!r}")
    rows = _table(text, delimiter)
    header, body, _header_line = _split_header(rows)
    parser_name, unknown = _unknown_columns(header)
    skipped, dates, numbers = _scan_body(body)
    return {
        "filename": Path(filename or "upload.csv").name,
        "parsed_rows": len(body),
        "blank_rows": sum(1 for item in skipped if item["reason"] == "blank row"),
        "skipped_reasons": skipped,
        "unknown_columns": unknown,
        "date_problems": dates,
        "number_problems": numbers,
        "duplicates": [],
        "normalised": notes,
        "parser": parser_name,
        "delimiter": delimiter,
        "header": header,
    }


def normalise_bytes(data: bytes) -> Optional[bytes]:
    """Return a comma-separated copy, or None when the bytes are already plain."""
    if _spreadsheet("", data):
        return None
    text, _notes = _decode(data)
    delimiter = _delimiter(text)
    rows = _table(text, delimiter)
    kept = []
    changed = data.startswith(b"\xef\xbb\xbf") or delimiter != ","
    for row in rows:
        if not any(cell.strip() for cell in row):
            changed = True
            continue
        cleaned = [_clean_number(cell) for cell in row]
        if cleaned != list(row):
            changed = True
        kept.append(cleaned)
    if not changed or not kept:
        return None
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(kept)
    return buffer.getvalue().encode("utf-8")


def fingerprint(cells: Sequence[str]) -> str:
    """Stable hash of a converted row. The cells themselves are not returned."""
    indexes = (0, 1, 2, 4, 5, 10, 11)
    parts = [cells[index].strip() if index < len(cells) else "" for index in indexes]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def cross_file_duplicates(
    rows: Sequence[Sequence[str]], known: Dict[str, Dict[str, str]]
) -> List[Dict[str, str]]:
    """Rows whose fingerprint is already stored for another file."""
    found = []
    for index, cells in enumerate(rows, start=1):
        previous = known.get(fingerprint(cells))
        if previous is None:
            continue
        found.append(
            {
                "row": str(index),
                "other_file": previous.get("filename", ""),
                "reason": "same type, asset, amount and time as a row already imported",
            }
        )
    return found


def load_prints(root: Path) -> Dict[str, Dict[str, str]]:
    path = root / "row-prints.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    prints = loaded.get("prints", {}) if isinstance(loaded, dict) else {}
    if not isinstance(prints, dict):
        return {}
    return {str(key): dict(value) for key, value in prints.items() if isinstance(value, dict)}


def store_prints(root: Path, rows: Sequence[Sequence[str]], import_id: str, filename: str) -> None:
    prints = load_prints(root)
    for cells in rows:
        prints.setdefault(fingerprint(cells), {"import_id": import_id, "filename": filename})
    path = root / "row-prints.json"
    payload = json.dumps({"prints": prints}, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def record_problem(root: Path, filename: str, data: bytes, message: str) -> Dict[str, Any]:
    """Store a redacted picture of the failing file. Raw cells are not kept."""
    try:
        report = inspect_bytes(filename, data)
    except (UnicodeError, csv.Error, ValueError):
        report = {"header": [], "skipped_reasons": [], "date_problems": [], "number_problems": []}
    pattern = _failing_pattern(data)
    problem = {
        "id": uuid.uuid4().hex[:12],
        "filename": Path(filename or "upload.csv").name,
        "message": "The converter rejected this file. The row below is a pattern, not the values.",
        "detail": _scrub_message(message),
        "header": [str(cell) for cell in report.get("header", [])],
        "pattern": pattern,
        "unknown_columns": list(report.get("unknown_columns") or []),
        "date_problems": list(report.get("date_problems") or []),
        "number_problems": list(report.get("number_problems") or []),
        "skipped_reasons": list(report.get("skipped_reasons") or []),
    }
    stored = _read_problems(root)
    stored.append(problem)
    _write_problems(root, stored)
    return problem


def load_problems(root: Path) -> List[Dict[str, Any]]:
    return _read_problems(root)


def find_problem(root: Path, problem_id: str) -> Optional[Dict[str, Any]]:
    for item in _read_problems(root):
        if item.get("id") == problem_id:
            return item
    return None


def latest_problem_id(root: Path) -> str:
    rows = _read_problems(root)
    if not rows:
        return ""
    return str(rows[-1].get("id") or "")


def _spreadsheet(filename: str, data: bytes) -> bool:
    name = filename.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return True
    return data[:4] == b"PK\x03\x04" or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _empty_report(filename: str, notes: List[str]) -> Dict[str, Any]:
    return {
        "filename": Path(filename or "upload.csv").name,
        "parsed_rows": 0,
        "blank_rows": 0,
        "skipped_reasons": [],
        "unknown_columns": [],
        "date_problems": [],
        "number_problems": [],
        "duplicates": [],
        "normalised": notes,
        "parser": "",
        "delimiter": "",
        "header": [],
    }


def _decode(data: bytes) -> Tuple[str, List[str]]:
    notes = []
    if data.startswith(b"\xef\xbb\xbf"):
        notes.append("byte-order mark")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        text = data.decode("latin-1")
        notes.append("decoded as latin-1")
    return text, notes


def _delimiter(text: str) -> str:
    line = ""
    for candidate in text.splitlines():
        if candidate.strip():
            line = candidate
            break
    counts = {",": line.count(","), ";": line.count(";"), "\t": line.count("\t")}
    chosen = max(counts, key=lambda key: counts[key])
    if counts[chosen] == 0:
        return ","
    return chosen


def _table(text: str, delimiter: str) -> List[List[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[cell.strip() for cell in row] for row in reader]


def _split_header(
    rows: Sequence[Sequence[str]],
) -> Tuple[List[str], List[Tuple[int, List[str]]], int]:
    for index, row in enumerate(rows, start=1):
        if any(cell.strip() for cell in row):
            rest = rows[index:]
            body = [(number, list(item)) for number, item in enumerate(rest, start=index + 1)]
            return list(row), body, index
    return [], [], 0


def _scan_body(
    body: Sequence[Tuple[int, List[str]]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    skipped: List[Dict[str, str]] = []
    dates: List[Dict[str, str]] = []
    numbers: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    families: Dict[int, Set[str]] = {}
    for line_no, row in body:
        if not any(cell.strip() for cell in row):
            skipped.append({"row": str(line_no), "reason": "blank row"})
            continue
        digest = hashlib.sha256("|".join(row).encode("utf-8")).hexdigest()
        if digest in seen:
            skipped.append({"row": str(line_no), "reason": "duplicate row in this file"})
        else:
            seen[digest] = line_no
        _collect_cells(line_no, row, dates, numbers, families)
    dates.extend(_mixed_dates(families))
    return skipped, dates, numbers


def _collect_cells(
    line_no: int,
    row: Sequence[str],
    dates: List[Dict[str, str]],
    numbers: List[Dict[str, str]],
    families: Dict[int, set],
) -> None:
    for index, cell in enumerate(row):
        family = _date_family(cell)
        if family:
            families.setdefault(index, set()).add(family)
            if family == "invalid":
                dates.append(
                    {"row": str(line_no), "pattern": redact(cell), "reason": "invalid date"}
                )
        if _THOUSAND.match(cell) or _EURO.match(cell):
            numbers.append(
                {
                    "row": str(line_no),
                    "pattern": redact(cell),
                    "reason": "thousands or decimal mark",
                }
            )


def _mixed_dates(families: Mapping[int, Set[str]]) -> List[Dict[str, str]]:
    found = []
    for index, kinds in families.items():
        if len(kinds) > 1:
            found.append(
                {
                    "row": "",
                    "pattern": ", ".join(sorted(str(item) for item in kinds)),
                    "reason": f"mixed date formats in column {index + 1}",
                }
            )
    return found


def _date_family(cell: str) -> str:
    family = ""
    if _ISO.match(cell):
        family = "iso"
    elif _TEXT_DATE.match(cell):
        family = "month-name"
    elif _SLASH.match(cell):
        family = _slash_family(cell)
    return family


def _slash_family(cell: str) -> str:
    parts = re.split(r"[/.-]", cell.split()[0])
    if len(parts) < 3:
        return "slash"
    first, second = int(parts[0]), int(parts[1])
    if first > 31 or second > 31 or first == 0 or second == 0:
        return "invalid"
    if first > 12:
        return "day-first"
    if second > 12:
        return "month-first"
    return "ambiguous"


def _clean_number(cell: str) -> str:
    if _THOUSAND.match(cell):
        return cell.replace(",", "")
    if _EURO.match(cell):
        return cell.replace(".", "").replace(",", ".")
    return cell


def _unknown_columns(header: Sequence[str]) -> Tuple[str, List[str]]:
    if not header:
        return "", []
    with _CONVERT_LOCK:
        saved = [
            (parser, list(parser.args), list(parser.in_header), parser.in_header_row_num)
            for parser in DataParser.parsers
        ]
        try:
            try:
                parser = DataParser.match_header(list(header), 0)
            except KeyError:
                parser = None
        finally:
            for item, args, cells, row_num in saved:
                item.args = args
                item.in_header = cells
                item.in_header_row_num = row_num
    if parser is None:
        return "", [cell for cell in header if cell]
    if len(header) == len(parser.header):
        return parser.name, []
    unknown = [cell for cell in header if cell and not _covered(cell, parser)]
    return parser.name, unknown


def _covered(cell: str, parser: DataParser) -> bool:
    for field in parser.header:
        if callable(field):
            try:
                if field(cell):
                    return True
            except (TypeError, re.error):
                continue
        elif cell == field:
            return True
    return False


def _failing_pattern(data: bytes) -> List[str]:
    try:
        text, _notes = _decode(data)
        rows = _table(text, _delimiter(text))
    except (UnicodeError, csv.Error, ValueError):
        return ["unreadable"]
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    chosen = data_rows[0] if data_rows else (rows[0] if rows else [])
    if not chosen:
        return ["empty"]
    return [_cell_shape(cell) for cell in chosen[:24]]


def _cell_shape(cell: str) -> str:
    family = _date_family(cell)
    if family:
        return f"date:{family}:{redact(cell)}"
    if _THOUSAND.match(cell) or _EURO.match(cell):
        return f"number:{redact(cell)}"
    if not cell.strip():
        return "blank"
    return f"text:{redact(cell)}"


def _scrub_message(message: str) -> str:
    return _HEX.sub("0x", message)[:300]


def _read_problems(root: Path) -> List[Dict[str, Any]]:
    path = root / "import-problems.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    rows = loaded.get("problems", []) if isinstance(loaded, dict) else []
    return [item for item in rows if isinstance(item, dict)]


def _write_problems(root: Path, problems: List[Dict[str, Any]]) -> None:
    path = root / "import-problems.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"problems": problems}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
