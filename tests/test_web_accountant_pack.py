# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The accountant zip explains each file and keeps the review banner."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from bittytax.web.accountant_pack import build_zip
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_the_pack_contains_the_named_files(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    blob = build_zip(desk, 2025)
    expected = {
        "README.txt",
        "report.html",
        "report.pdf",
        "disposals.csv",
        "income.csv",
        "holdings.csv",
        "transactions.csv",
        "sa108.csv",
        "accountant-review.txt",
        "awaiting-tax-review.txt",
        "health.csv",
        "audit.txt",
    }
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert expected <= set(archive.namelist())
        readme = archive.read("README.txt").decode("utf-8")
        assert "AWAITING TAX REVIEW" in readme
        assert "sa108.csv" in readme
        assert archive.read("report.pdf").startswith(b"%PDF")
        waiting = archive.read("awaiting-tax-review.txt").decode("utf-8")
        assert "AWAITING TAX REVIEW" in waiting
