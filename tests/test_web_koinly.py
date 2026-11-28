# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Koinly comparison uses invented CSVs and stores uploads only in the data directory."""

from __future__ import annotations

import http.cookiejar
import threading
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from bittytax.web.koinly_check import compare_year, load_uploads, store_uploads
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
SUMMARY = ROOT / "examples" / "koinly-sample-gains-summary.csv"
COMPLETE = ROOT / "examples" / "koinly-sample-complete.csv"


def test_summary_and_complete_files_parse_made_up_rows() -> None:
    report = {
        "disposals": [
            {
                "asset": "BTC",
                "cost": "4000",
                "fees": "0",
                "proceeds": "9000",
                "gain": "5000",
            }
        ]
    }
    compared = compare_year(report, SUMMARY.read_text(encoding="utf-8"), "")
    btc = next(row for row in compared["rows"] if row["asset"] == "BTC")
    assert btc["koinly_gain"] == "5000"
    assert btc["koinly_proceeds"] == "10000"
    assert btc["different"] is True
    eth = next(row for row in compared["rows"] if row["asset"] == "ETH")
    assert eth["koinly_gain"] == "-500"
    assert eth["different"] is True
    both = compare_year(
        report, SUMMARY.read_text(encoding="utf-8"), COMPLETE.read_text(encoding="utf-8")
    )
    assert both["source"] == "summary"
    assert len(both["complete_rows"]) == 2
    assert {row["asset"] for row in both["complete_rows"]} == {"BTC", "ETH"}


def test_uploads_stay_in_the_data_directory(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    store_uploads(desk.root, 2026, SUMMARY.read_bytes(), COMPLETE.read_bytes())
    saved = desk.root / "koinly" / "2026"
    assert (saved / "summary.csv").is_file()
    assert not (ROOT / "koinly").exists()
    summary, complete = load_uploads(desk.root, 2026)
    assert "BTC" in summary and "invented BTC disposal" in complete
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    opener = _opener(follow=True)
    try:
        page = _read(opener, port, "GET", "/koinly?tax_year=2026")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert 'class="diff"' in page
    assert "invented ETH disposal" in page
    assert "Koinly" in page


def test_the_form_stores_both_files(tmp_path: Path) -> None:
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    opener = _opener(follow=False)
    try:
        first = _read(opener, port, "GET", "/koinly")
        status = _post_files(
            opener,
            port,
            {
                "csrf": _csrf(first),
                "tax_year": "2026",
                "summary": ("summary.csv", SUMMARY.read_bytes()),
                "complete": ("complete.csv", COMPLETE.read_bytes()),
            },
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert status == 303
    stored = tmp_path / "koinly" / "2026" / "complete.csv"
    assert stored.is_file()
    assert b"invented ETH disposal" in stored.read_bytes()
    assert "koinly" not in str(ROOT)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(  # pylint: disable=too-many-arguments,useless-return
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


def _opener(follow: bool) -> Any:
    jar = http.cookiejar.CookieJar()
    handlers: List[Any] = [HTTPCookieProcessor(jar)]
    if not follow:
        handlers.append(_NoRedirect())
    return build_opener(*handlers)


def _read(opener: Any, port: int, method: str, path: str) -> str:
    request = Request(f"http://127.0.0.1:{port}{path}", method=method)
    with opener.open(request, timeout=60) as response:
        return response.read().decode("utf-8")


def _post_files(opener: Any, port: int, fields: Dict[str, Any]) -> int:
    boundary = "----koinlyboundary"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        if isinstance(value, tuple):
            filename, payload = value
            header = (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            )
            chunks.append(header.encode("utf-8"))
            chunks.append(payload)
        else:
            header = f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            chunks.append(header.encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    request = Request(f"http://127.0.0.1:{port}/koinly", data=b"".join(chunks), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with opener.open(request, timeout=60) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)


def _csrf(page: str) -> str:
    marker = 'name="csrf-token" content="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]
