# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Matching order for a disposal: same day, then the next 30 days, then the pool.

The 30-day window is a run of calendar days. It is not cut short on 5 April.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

MANUAL = "CRYPTO22200"
LABEL = "confirmed"
MATCHING_ORDER = ("same day", "next 30 days", "section 104 pool")


def matching_rule(disposal_type: str) -> str:
    """Plain name of the rule the calculator used for one disposal."""
    text = disposal_type.casefold()
    if "same day" in text:
        return "same day"
    if "breakfast" in text:
        return "next 30 days"
    if "section 104" in text:
        return "section 104 pool"
    if "ten day" in text:
        return "ten day"
    if "no gain" in text:
        return "no gain no loss"
    return disposal_type


def in_thirty_day_window(sell_on: date, buy_on: date) -> bool:
    """True when the acquisition falls in the 30 days after the disposal.

    The window is not closed on 5 April. A disposal on 1 April can match an
    acquisition on 20 April, which is the next tax year.
    """
    return sell_on < buy_on <= sell_on + timedelta(days=30)


def schedule(disposals: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """One line per disposal, with the matching rule beside it."""
    rows = []
    for row in disposals:
        kind = str(row.get("disposal_type") or "")
        rows.append(
            {
                "date": str(row.get("date") or ""),
                "asset": str(row.get("asset") or ""),
                "quantity": str(row.get("quantity") or ""),
                "proceeds": str(row.get("proceeds") or ""),
                "cost": str(row.get("cost") or ""),
                "gain": str(row.get("gain") or ""),
                "disposal_type": kind,
                "matching_rule": str(row.get("matching_rule") or matching_rule(kind)),
                "manual": MANUAL,
                "label": LABEL,
            }
        )
    return rows


def from_report(report: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Disposal lines for one calculator report."""
    rows = [] if report is None else schedule(report.get("disposals") or [])
    return {
        "order": list(MATCHING_ORDER),
        "manual": MANUAL,
        "label": LABEL,
        "confirmed": True,
        "rows": rows,
        "note": (
            "Matching runs same day, then the next 30 days, then the section 104 pool. "
            "The 30-day window crosses 5 April into the next tax year. "
            "The gain stays in the tax year of the disposal."
        ),
    }
