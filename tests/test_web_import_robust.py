# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Messy exports: a per-file report, an idempotent re-import, and a redacted problem."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from bittytax.web.errors import DeskError
from bittytax.web.import_inspect import inspect_bytes, redact
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_SECRET = "0xDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF"
_HEADER = (
    "Date,Type,Tag,Sending Wallet,Sent Amount,Sent Currency,Sent Cost Basis,"
    "Receiving Wallet,Received Amount,Received Currency,Received Cost Basis,"
    "Fee Amount,Fee Currency,Gain (GBP),Net Value (GBP),Fee Value (GBP),"
    "TxSrc,TxDest,TxHash,Description"
)


def test_redact_keeps_shape_and_drops_characters() -> None:
    assert redact("1,234.56") == "#,###.##"
    assert _SECRET not in redact(_SECRET)
    assert "DEAD" not in redact(_SECRET)


def test_inspect_flags_blank_rows_mixed_dates_and_thousands() -> None:
    payload = (
        "\ufeff"
        + _HEADER.replace(",", ";")
        + "\n"
        + "2024-05-01 12:00:00;exchange;;Desk Exchange;1,250;GBP;;Desk Exchange;0.02;BTC;"
        + ";;;;1,250;;;;sample-messy-1;Bought a sample\n"
        + "\n"
        + "15/06/2024 09:00:00;exchange;;Desk Exchange;0.02;BTC;;Desk Exchange;900;GBP;"
        + ";;;;900;;;;sample-messy-2;Sold a sample\n"
        + "2024-05-01 12:00:00;exchange;;Desk Exchange;1,250;GBP;;Desk Exchange;0.02;BTC;"
        + ";;;;1,250;;;;sample-messy-1;Bought a sample\n"
    ).encode("utf-8")
    report = inspect_bytes("messy.csv", payload)
    assert any("byte-order mark" == note for note in report["normalised"])
    assert any(item["reason"] == "blank row" for item in report["skipped_reasons"])
    assert any("duplicate row" in item["reason"] for item in report["skipped_reasons"])
    assert any("mixed date" in item["reason"] for item in report["date_problems"])
    assert any(item["pattern"] == "#,###" for item in report["number_problems"])
    assert _SECRET not in json.dumps(report)


def test_semicolon_export_imports_once_and_a_repeat_does_not_duplicate(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    payload = (
        "\ufeff"
        + _HEADER.replace(",", ";")
        + "\n"
        + "\n"
        + "2024-06-01 12:00:00;exchange;;Desk Exchange;1,250;GBP;;Desk Exchange;0.02;BTC;"
        + ";;;;1,250;;;;sample-messy-buy;Bought a sample\n"
    ).encode("utf-8")
    entry = desk.import_known("messy.csv", payload, "sam")
    assert entry["idempotent"] is False
    assert entry["format"] == "Koinly"
    assert entry["converted_rows"] == 1
    report = entry["report"]
    assert any(item["reason"] == "blank row" for item in report["skipped_reasons"])
    assert report["number_problems"]
    assert any("rewritten" in note for note in report["normalised"])
    assert len(desk.all_rows()) == 1

    again = desk.import_known("messy.csv", payload, "sam")
    assert again["idempotent"] is True
    assert again["id"] == entry["id"]
    assert len(desk.all_rows()) == 1
    events = (desk.root / "events.jsonl").read_text(encoding="utf-8")
    assert "import-skip" in events
    assert oct((desk.root / "events.jsonl").stat().st_mode & 0o777) == "0o600"


def test_same_bytes_still_raise_from_import_bytes(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    original = SAMPLE.read_bytes()
    desk.import_bytes(SAMPLE.name, original)
    with pytest.raises(DeskError, match="already imported"):
        desk.import_bytes(SAMPLE.name, original)


def test_a_second_file_reports_cross_file_duplicates(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    original = SAMPLE.read_bytes()
    desk.import_bytes(SAMPLE.name, original)
    extra = original + b"\n"
    entry = desk.import_bytes("copy.csv", extra)
    assert entry["report"]["duplicates"]
    assert all("other_file" in item for item in entry["report"]["duplicates"])
    assert len(desk.all_rows()) == 12


def test_rejected_file_stores_a_pattern_without_the_value(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    payload = (f"When,What,Wallet\n" f"32/13/2024,not-a-type,{_SECRET}\n").encode("utf-8")
    with pytest.raises(DeskError, match="parser problem"):
        desk.import_bytes("broken.csv", payload)
    stored = (desk.root / "import-problems.json").read_text(encoding="utf-8")
    assert _SECRET not in stored
    assert "DEAD" not in stored
    problem = json.loads(stored)["problems"][0]
    assert problem["pattern"]
    assert not list(desk.raw_dir.rglob("*"))


def test_problem_page_hides_the_planted_value(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        body = _post_file(
            f"http://127.0.0.1:{port}/import",
            "broken.csv",
            f"When,What,Wallet\n32/13/2024,not-a-type,{_SECRET}\n".encode("utf-8"),
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "Parser problem" in body
    assert _SECRET not in body
    assert "Failing row pattern" in body


def _post_file(url: str, filename: str, data: bytes) -> str:
    home = url.rsplit("/import", 1)[0] + "/"
    with urlopen(home, timeout=30) as response:
        page = response.read().decode("utf-8")
        cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    token = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    boundary = "----deskboundaryrobust"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf"\r\n\r\n'
            f"{token}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("utf-8")
        + data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Cookie", cookie)
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{exc.code} {detail}") from exc
