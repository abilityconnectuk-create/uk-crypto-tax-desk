# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Map the calculator's capital-gains totals onto the SA108 boxes in config."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional

from .rules import load_sa108

_BANNER = "AWAITING TAX REVIEW"


def sa108_view(report: Mapping[str, Any], ending_year: int) -> Optional[Dict[str, Any]]:
    """Boxes for one report. ``None`` when that year has no mapping file."""
    spec = load_sa108(ending_year)
    if spec is None:
        return None
    summary = report.get("summary") or {}
    boxes = [_box(item, summary) for item in spec.get("boxes", []) if isinstance(item, dict)]
    allowance = spec.get("annual_exempt_amount") or {}
    trigger = _trigger(spec.get("reporting_trigger") or {}, summary)
    return {
        "tax_year": str(spec.get("tax_year", "")),
        "form_title": str(spec.get("form_title", "")),
        "section": str(spec.get("section", "")),
        "note": str(spec.get("note", "")).strip(),
        "confirmed": bool(spec.get("confirmed")),
        "banner": "" if spec.get("confirmed") else _BANNER,
        "sources": list(spec.get("sources") or []),
        "boxes": boxes,
        "allowance": {
            "value": str(allowance.get("value", "")),
            "note": str(allowance.get("note", "")).strip(),
            "confirmed": bool(allowance.get("confirmed")),
            "deducted": False,
        },
        "trigger": trigger,
        "split": _split(spec.get("split") or {}, summary, report.get("disposals") or []),
    }


def _box(item: Mapping[str, Any], summary: Mapping[str, Any]) -> Dict[str, str]:
    field = str(item.get("engine_field") or "")
    value = str(summary.get(field, "")) if field else ""
    return {
        "box": str(item.get("box", "")),
        "key": str(item.get("key", "")),
        "label": str(item.get("label", "")),
        "value": value,
        "note": str(item.get("note", "")).strip(),
        "confirmed": "yes" if item.get("confirmed") else "no",
    }


def _trigger(spec: Mapping[str, Any], summary: Mapping[str, Any]) -> Dict[str, Any]:
    proceeds_over = _decimal(spec.get("proceeds_over"))
    gains_over = _decimal(spec.get("gains_over"))
    proceeds = _decimal(summary.get("proceeds"))
    gains = _decimal(summary.get("total_gain"))
    proceeds_hit = proceeds > proceeds_over
    gains_hit = gains > gains_over
    return {
        "proceeds_over": str(spec.get("proceeds_over", "")),
        "gains_over": str(spec.get("gains_over", "")),
        "proceeds_hit": proceeds_hit,
        "gains_hit": gains_hit,
        "hit": proceeds_hit or gains_hit,
        "confirmed": bool(spec.get("confirmed")),
        "note": str(spec.get("note", "")).strip(),
    }


def _split(
    spec: Mapping[str, Any],
    summary: Mapping[str, Any],
    disposals: List[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not spec.get("applies"):
        return None
    day = str(spec.get("date", ""))
    before = spec.get("rates_before") or {}
    after = spec.get("rates_after") or {}
    tagged = []
    for row in disposals:
        when = str(row.get("date_iso") or "")
        period = "before" if when and when < day else "on or after"
        tagged.append(
            {
                "asset": str(row.get("asset", "")),
                "date": str(row.get("date") or when),
                "period": period,
                "gain": str(row.get("gain", "")),
                "proceeds": str(row.get("proceeds", "")),
            }
        )
    return {
        "date": day,
        "gain_before": str(summary.get("gain_before", "")),
        "gain_after": str(summary.get("gain_after", "")),
        "rates_before": f"{before.get('basic', '')}% and {before.get('higher', '')}%",
        "rates_after": f"{after.get('basic', '')}% and {after.get('higher', '')}%",
        "disposals": tagged,
        "confirmed": bool(spec.get("confirmed")),
        "note": str(spec.get("note", "")).strip(),
    }


def _decimal(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)
