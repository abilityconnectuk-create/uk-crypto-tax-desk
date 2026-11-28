# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""A large ledger stays on the page cache, and an update never merges."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

from bittytax.web.errors import DeskError
from bittytax.web.store import Desk
from bittytax.web.updater import update_checkout


def test_twenty_thousand_rows_list_without_the_engine(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    rows = []
    for number in range(20000):
        rows.append(
            {
                "id": f"bench:{number:05d}",
                "import_id": "bench",
                "cells": [
                    "Deposit",
                    "1",
                    "ETH",
                    "10",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Sample",
                    "2025-06-01 12:00:00",
                    f"row {number}",
                ],
            }
        )
    (tmp_path / "ledger.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    started = time.perf_counter()
    page = desk.transactions({}, 1)
    elapsed = time.perf_counter() - started
    assert page["total"] == 20000
    assert len(page["rows"]) == 50
    assert page["pages"] == 400
    assert elapsed < 3
    again = time.perf_counter()
    desk.transactions({}, 2)
    assert time.perf_counter() - again < 3


def test_health_count_uses_the_cache(tmp_path: Path, monkeypatch: Any) -> None:
    desk = Desk(tmp_path)
    calls: List[int] = []

    def _run(path: str, year: int) -> dict:
        del path
        calls.append(year)
        return {
            "missing_cost": [],
            "integrity_ok": True,
            "summary": {},
            "income": {"amount": "0.00", "rows": []},
            "holdings": [],
            "disposals": [],
            "label": "2025/26",
        }

    monkeypatch.setattr("bittytax.web.store.run_tax_year", _run)
    (tmp_path / "ledger.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "id": "bench:00000",
                        "import_id": "bench",
                        "cells": [
                            "Deposit",
                            "1",
                            "ETH",
                            "10",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "Sample",
                            "2025-06-01 12:00:00",
                            "one",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert desk.health_count(2026) == desk.health_count(2026)
    assert calls == [2026]


def test_update_refuses_local_changes_and_never_merges(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    seen: List[List[str]] = []

    def run(args: List[str], **_kwargs: Any) -> SimpleNamespace:
        seen.append(list(args))
        if args[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout=" M README.md", stderr="")
        return SimpleNamespace(returncode=0, stdout="main", stderr="")

    try:
        update_checkout(tmp_path, run)
    except DeskError as exc:
        assert "Local changes" in str(exc)
    else:
        raise AssertionError("a dirty tree should be refused")
    assert all("merge" not in " ".join(item) for item in seen)
    assert all("checkout" not in item for item in seen)

    def clean(args: List[str], **_kwargs: Any) -> SimpleNamespace:
        seen.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    seen.clear()
    message = update_checkout(tmp_path, clean)
    assert message == "Restart the desk to load the update."
    assert ["git", "pull", "--ff-only"] in seen
    assert all("merge" not in " ".join(item) for item in seen)
    assert all("checkout" not in item for item in seen)
