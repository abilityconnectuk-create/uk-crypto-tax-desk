# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""A seasonal note. It is information only and it does not suggest a trade."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dashboard import dashboard
from .losses import for_year

_NOTE = "This is not advice and it does not suggest a trade."


def in_year_end_window(day: date) -> bool:
    """1 February through 5 April, inclusive."""
    if day.month in (2, 3):
        return True
    return day.month == 4 and day.day <= 5


def year_end_card(desk: Any, today: date) -> Optional[Dict[str, Any]]:
    if not in_year_end_window(today) or _dismissed(desk.root, today.year):
        return None
    board = dashboard(desk, today)
    losses: List[Dict[str, str]] = []
    for holding in board.get("holdings") or []:
        amount = str(holding.get("unrealised") or "")
        if amount.startswith("-"):
            losses.append({"asset": str(holding.get("asset") or ""), "amount": amount})
    carried = for_year(desk, int(board["tax_year"])).get("carried", "0.00")
    return {
        "allowance_remaining": str(board.get("allowance_remaining") or ""),
        "unrealised": losses,
        "losses_available": carried or "0.00",
        "note": _NOTE,
        "banner": "AWAITING TAX REVIEW",
    }


def dismiss_year_end(root: Path, today: date) -> None:
    path = root / "year-end-dismiss.json"
    loaded: Dict[str, Any] = {"years": []}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
    years = [int(item) for item in loaded.get("years", [])]
    if today.year not in years:
        years.append(today.year)
    path.write_text(json.dumps({"years": years}) + "\n", encoding="utf-8")


def _dismissed(root: Path, year: int) -> bool:
    path = root / "year-end-dismiss.json"
    if not path.is_file():
        return False
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return year in [int(item) for item in loaded.get("years", [])]
