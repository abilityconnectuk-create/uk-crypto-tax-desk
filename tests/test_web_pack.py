# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The report pack is HTML when the optional PDF library is absent."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence
from urllib.request import urlopen

from bittytax.web.pack import pack_context, pack_lines, pdf_library_available, render_pdf
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_lines_cover_every_pack_section(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    context = pack_context(desk, 2026)
    text = "\n".join(pack_lines(context))
    for heading in ("Summary", "SA108", "Disposals", "Income", "Transactions", "Data health"):
        assert heading in text
    assert "AWAITING TAX REVIEW" in text
    assert "Accountant review" in text
    assert render_pdf(["Sample pack line"], renderer=_stub_pdf) == b"%PDF-sample"
    if not pdf_library_available():
        assert render_pdf(["Sample pack line"]) is None


def test_pack_page_and_pdf_route_fall_back_to_html(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/report/pack?tax_year=2026", timeout=60) as response:
            page = response.read().decode("utf-8")
            html_type = response.headers.get("Content-Type", "")
        with urlopen(
            f"http://127.0.0.1:{port}/report/pack.pdf?tax_year=2026", timeout=60
        ) as response:
            download = response.read()
            pdf_type = response.headers.get("Content-Type", "")
        with urlopen(f"http://127.0.0.1:{port}/report?tax_year=2026&run=1", timeout=60) as response:
            report = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "text/html" in html_type
    assert "Data health" in page
    assert "AWAITING TAX REVIEW" in page
    assert "Download report pack" in report
    if pdf_library_available():
        assert pdf_type.startswith("application/pdf")
        assert download.startswith(b"%PDF")
    else:
        assert "text/html" in pdf_type
        assert b"optional PDF library is not installed" in download


def _stub_pdf(lines: Sequence[str]) -> bytes:
    assert lines
    return b"%PDF-sample"
