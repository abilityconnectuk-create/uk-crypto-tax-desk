# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The redesigned pages, and header detection, stay on the same calculator."""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bittytax.web.glossary import GLOSSARY
from bittytax.web.present import build_home
from bittytax.web.server import bind_server
from bittytax.web.sources import detect_upload, parser_catalogue
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_CSRF = re.compile(r'name="csrf-token" content="([^"]*)"')


def test_glossary_names_the_terms_a_reviewer_can_edit() -> None:
    needed = {"s104", "same_day", "thirty_day", "sa108", "annual_exempt", "allowable_costs"}
    assert needed <= set(GLOSSARY)
    for item in GLOSSARY.values():
        assert item["term"]
        assert "not advice" in item["text"].casefold()


def test_detect_reads_a_koinly_header_and_rejects_an_unknown_one() -> None:
    found = detect_upload(SAMPLE.name, SAMPLE.read_bytes())
    assert found["ok"] == "yes"
    assert found["name"] == "Koinly"
    names = {item["name"] for item in parser_catalogue()}
    assert "Koinly" in names
    assert {"exchange", "wallet", "blockchain"} <= {item["group_id"] for item in parser_catalogue()}
    missed = detect_upload("notes.csv", b"alpha,beta\n1,2\n")
    assert missed["ok"] == "no"
    assert missed["name"] == ""


def test_home_view_marks_holdings_with_a_cached_price(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    (desk.root / "price-cache.json").write_text(
        json.dumps({"BTC|2025-12-01": {"price": "40000", "source": "sample"}}) + "\n",
        encoding="utf-8",
    )
    view = build_home(desk, 0, "all", "", "value", "desc", today=date(2026, 11, 28))
    assert view["label"] == "2026/27"
    assert view["banner"] == "AWAITING TAX REVIEW"
    assert Decimal(view["hero"]) == Decimal("10000.00")
    assert "chart-line" in view["svg"]
    assert view["steps"][0]["state"] == "done"
    assert any(row["asset"] == "BTC" for row in view["holdings"])


def test_new_pages_and_import_detection(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    browser = _Client(f"http://127.0.0.1:{port}")
    try:
        home = browser.get("/")
        assert "UK Crypto Tax Desk" in home
        assert "Local tax desk" in home
        assert "1 Import" in home
        report = browser.get("/report?tax_year=2026")
        assert "tax year" in report
        assert "31 January" in report
        assert "Online filing deadline" in report
        add = browser.get("/import/add")
        assert "Add your data" in add
        assert "Exchanges" in add
        assert "Blockchains" in add
        detected = browser.post_file("/import/detect", SAMPLE.name, SAMPLE.read_bytes())
        assert "Koinly" in detected
        imported = browser.post_file("/import", SAMPLE.name, SAMPLE.read_bytes())
        assert "Imported" in imported
        assert "rows imported" in imported
        listing = browser.get("/transactions")
        assert "Wallet" in listing
        assert "transactions" in listing
        assert 'name="per"' in listing
        match = re.search(r'href="(/transactions/[^"]+)"', listing)
        assert match is not None
        parts = match.group(1).strip("/").split("/")
        panel = browser.get(f"/transactions?open={parts[1]}:{parts[2]}")
        assert "Why" in panel
        assert "Save reclassification" in panel
        style = browser.get("/static/style.css")
        assert "--ink" in style
        assert "1f6feb" in style.casefold()
        script = browser.get("/static/desk.js")
        assert "ukctd-theme" in script
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


class _Client:
    """Sends the session cookie and the form token."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie = ""
        self.csrf = ""

    def get(self, path: str) -> str:
        return self._open(Request(self.base + path))

    def post_file(self, path: str, filename: str, data: bytes) -> str:
        boundary = "----deskboundarylook"
        csrf = b""
        if self.csrf:
            csrf = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf"\r\n\r\n'
                f"{self.csrf}\r\n"
            ).encode("utf-8")
        body = (
            csrf
            + (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode("utf-8")
            + data
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        request = Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._open(request)

    def _open(self, request: Request) -> str:
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.headers.get("Set-Cookie")
                if raw:
                    self.cookie = str(raw).split(";", 1)[0].strip()
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{exc.code} {detail}") from exc
        found = _CSRF.search(body)
        if found:
            self.csrf = found.group(1)
        return body
