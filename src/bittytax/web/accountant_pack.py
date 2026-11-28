# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""One zip for an accountant. Every tax figure in it is still unconfirmed."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .events import read_events
from .losses import for_year
from .pack import pack_context, pack_lines
from .sa108 import sa108_view

_README = """\
UK Crypto Tax Desk accountant pack
This pack is the calculator's output for one tax year. It is not tax advice.
AWAITING TAX REVIEW applies to every figure that depends on a rule file.

report.html and report.pdf are the same narrative report.
disposals.csv, income.csv, holdings.csv and transactions.csv are the rows behind it.
sa108.csv is the SA108 boxes, including boxes 45 and 47. They are not confirmed.
accountant-review.txt lists items the rule file marks for an accountant.
awaiting-tax-review.txt repeats the banner and the unconfirmed boxes.
health.csv is the data-health list.
audit.txt is a summary of the desk log. It does not contain secrets.
"""


def build_zip(desk: Any, tax_year: int) -> bytes:
    context = pack_context(desk, tax_year)
    losses = for_year(desk, tax_year)
    sa108 = sa108_view(context["report"], tax_year, losses)
    lines = pack_lines(context)
    files: Dict[str, Any] = {
        "README.txt": _README,
        "report.html": _html(lines),
        "report.pdf": _pdf(lines),
        "disposals.csv": _rows(context["report"].get("disposals") or []),
        "income.csv": _rows((context["report"].get("income") or {}).get("rows") or []),
        "holdings.csv": _rows(context["report"].get("holdings") or []),
        "transactions.csv": _rows(list(context.get("transactions") or [])),
        "sa108.csv": _boxes(sa108),
        "accountant-review.txt": _reviews(context.get("accountant_review") or []),
        "awaiting-tax-review.txt": _awaiting(sa108),
        "health.csv": _rows((context.get("health") or {}).get("issues") or []),
        "audit.txt": _audit(desk.root),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            archive.writestr(name, data)
    return buffer.getvalue()


def _html(lines: Sequence[str]) -> str:
    body = "\n".join(f"<p>{_escape(line)}</p>" for line in lines)
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>Tax report</title></head><body>"
        "<p>AWAITING TAX REVIEW</p>" + body + "</body></html>"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rows(rows: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if not rows:
        writer.writerow(["none"])
        return buffer.getvalue()
    keys: List[str] = []
    for row in rows:
        for key in row:
            if key not in keys and key != "cells":
                keys.append(str(key))
    writer.writerow(keys)
    for row in rows:
        writer.writerow([row.get(key, "") for key in keys])
    return buffer.getvalue()


def _boxes(sa108: Optional[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["box", "label", "value", "confirmed"])
    if not sa108:
        return buffer.getvalue()
    for box in sa108.get("boxes") or []:
        writer.writerow(
            [
                box.get("box", ""),
                box.get("label", ""),
                box.get("value", ""),
                box.get("confirmed", ""),
            ]
        )
    return buffer.getvalue()


def _reviews(items: Sequence[Mapping[str, str]]) -> str:
    if not items:
        return "No accountant-review items for this year.\n"
    return "".join(f"{item.get('title', '')}: {item.get('detail', '')}\n" for item in items)


def _awaiting(sa108: Optional[Mapping[str, Any]]) -> str:
    lines = ["AWAITING TAX REVIEW"]
    if not sa108:
        return lines[0] + "\n"
    for box in sa108.get("boxes") or []:
        if box.get("confirmed") != "yes":
            lines.append(f"Box {box.get('box', '')} {box.get('label', '')}")
    return "\n".join(lines) + "\n"


def _audit(root: Any) -> str:
    rows = read_events(root)
    if not rows:
        return "No audit events.\n"
    return "".join(
        f"{row.get('at', '')} {row.get('who', '')} {row.get('summary', '')}\n" for row in rows
    )


def _pdf(lines: Sequence[str]) -> bytes:
    chunks: List[Sequence[str]] = [
        lines[index : index + 40] for index in range(0, max(len(lines), 1), 40)
    ]
    if not chunks:
        chunks = [[" "]]
    streams = []
    for chunk in chunks:
        commands = ["BT", "/F1 10 Tf"]
        y_pos = 800
        for line in chunk:
            safe = _pdf_text(line)
            commands.append(f"1 0 0 1 40 {y_pos} Tm ({safe}) Tj")
            y_pos -= 16
        commands.append("ET")
        streams.append("\n".join(commands).encode("latin-1", errors="replace"))
    return _pdf_file(streams)


def _pdf_text(line: str) -> str:
    raw = line.encode("latin-1", errors="replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_file(streams: Sequence[bytes]) -> bytes:
    count = len(streams)
    objects: Dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"
    kids = []
    number = 4
    for stream in streams:
        kids.append(f"{number} 0 R")
        objects[number] = (
            f"<< /Type /Page /Parent 2 0 R /Contents {number + 1} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode("ascii")
        objects[number + 1] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        number += 2
    joined = " ".join(kids)
    objects[2] = f"<< /Type /Pages /Count {count} /Kids [{joined}] >>".encode("ascii")
    output = bytearray(b"%PDF-1.4\n")
    offsets = {0: 0}
    for obj_number in range(1, number):
        offsets[obj_number] = len(output)
        output.extend(f"{obj_number} 0 obj\n".encode("ascii"))
        output.extend(objects[obj_number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {number}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for obj_number in range(1, number):
        output.extend(f"{offsets[obj_number]:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer << /Size {number} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    output.extend(trailer.encode("ascii"))
    return bytes(output)
