# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Map the calculator's capital-gains totals onto the SA108 boxes in the rule files."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .rules import load_sa108

_BANNER = "AWAITING TAX REVIEW"


def sa108_view(
    report: Mapping[str, Any],
    ending_year: int,
    losses: Optional[Mapping[str, str]] = None,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    claim_codes: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Boxes for one report. ``None`` when that year has no mapping file."""
    spec = load_sa108(ending_year)
    if spec is None:
        return None
    summary = report.get("summary") or {}
    boxes = [_box(item, summary) for item in spec.get("boxes", []) if isinstance(item, dict)]
    _fill_loss_boxes(boxes, losses or {})
    _fill_disposal_boxes(boxes, list(report.get("disposals") or []))
    _fill_claim_code(boxes, claim_codes or [])
    _fill_estimates(boxes, rows or [])
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


_SLICE_WORDS = ("same day", "breakfast", "section 104", "ten day")


def grouped_crypto_boxes(disposals: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """One disposal per token per day, plus each disposal the same-day pool does not cover.

    Slices of that one disposal are netted before the result goes in box 13.4 or 13.5.
    Returns ``None`` when the rows do not name a matching rule.
    """
    if not disposals or not any(str(row.get("disposal_type") or "") for row in disposals):
        return None
    groups: Dict[Any, List[Decimal]] = {}
    order: List[Any] = []
    for index, row in enumerate(disposals):
        kind = str(row.get("disposal_type") or "").casefold()
        asset = str(row.get("asset") or "")
        day = str(row.get("date_iso") or row.get("date") or "")
        if any(word in kind for word in _SLICE_WORDS):
            key: Any = ("day", asset, day)
        else:
            key = ("one", index)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(_decimal(row.get("gain")))
    gains = Decimal(0)
    losses = Decimal(0)
    for key in order:
        net = sum(groups[key], Decimal(0))
        if net >= 0:
            gains += net
        else:
            losses += -net
    return {
        "count": str(len(order)),
        "gains": f"{gains.quantize(Decimal('0.01'))}",
        "losses": f"{losses.quantize(Decimal('0.01'))}",
    }


def box_13_6(codes: Sequence[str]) -> str:
    """``NVC`` for one code, ``MUL`` when more than one code is recorded, otherwise blank."""
    unique: List[str] = []
    for code in codes:
        text = str(code or "").strip()
        if text and text not in unique:
            unique.append(text)
    if not unique:
        return ""
    if len(unique) > 1:
        return "MUL"
    return unique[0]


def estimates_flag(rows: Sequence[Mapping[str, Any]]) -> str:
    """``X`` when a value comes from a price source, including every token-to-token swap."""
    for row in rows:
        if _priced_from_a_source(row):
            return "X"
    return ""


def _priced_from_a_source(row: Mapping[str, Any]) -> bool:
    buy = str(row.get("buy_asset") or "").upper()
    sell = str(row.get("sell_asset") or "").upper()
    if buy and sell and buy != sell and "GBP" not in {buy, sell}:
        return True
    source = str(row.get("fx_source") or row.get("price_source") or "").strip()
    if not source:
        return False
    folded = source.casefold()
    if "actual" in folded or folded == "transaction gbp value":
        return False
    return True


def _fill_disposal_boxes(
    boxes: List[Dict[str, str]], disposals: Sequence[Mapping[str, Any]]
) -> None:
    figures = grouped_crypto_boxes(disposals)
    if figures is None:
        return
    for box in boxes:
        if box["box"] == "13.1":
            box["value"] = figures["count"]
        elif box["box"] == "13.4":
            box["value"] = figures["gains"]
        elif box["box"] == "13.5":
            box["value"] = figures["losses"]


def _fill_claim_code(boxes: List[Dict[str, str]], codes: Sequence[str]) -> None:
    code = box_13_6(codes)
    for box in boxes:
        if box["box"] == "13.6":
            box["value"] = code


def _fill_estimates(boxes: List[Dict[str, str]], rows: Sequence[Mapping[str, Any]]) -> None:
    flag = estimates_flag(rows) if rows else ""
    for box in boxes:
        if box["box"] == "53" and flag:
            box["value"] = flag


def _fill_loss_boxes(boxes: List[Dict[str, str]], losses: Mapping[str, str]) -> None:
    for box in boxes:
        if box["box"] == "45" and losses.get("brought_used"):
            box["value"] = str(losses["brought_used"])
        if box["box"] == "47" and losses.get("carried"):
            box["value"] = str(losses["carried"])


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
