# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Figures for the home page. Amounts come from the calculator or a cached price.

Nothing here is a new tax rule. A latest cached price times a quantity is only
a picture of the holdings. It is not advice.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..config import config
from .dashboard import dashboard, tax_year_containing
from .errors import DeskError
from .filing import readiness
from .prices import latest_unit_prices
from .store import Desk
from .year_end import year_end_card

_PENCE = Decimal("0.01")
_SPANS = ("1m", "1y", "ytd", "tax", "all")
_SPAN_LABELS = (
    ("1m", "1M"),
    ("1y", "1Y"),
    ("ytd", "YTD"),
    ("tax", "Tax year"),
    ("all", "All"),
)
_HOLD_SORTS = ("asset", "quantity", "value", "cost", "gain")
Point = Tuple[date, Decimal]


def initials(name: str) -> str:
    """Two letters for the person signed in, or LO on this computer."""
    if not name:
        return "LO"
    local = name.split("@", 1)[0]
    parts = [part for part in local.replace(".", " ").replace("_", " ").split() if part]
    if not parts:
        return "LO"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def avatar_slot(name: str) -> int:
    """Stable colour slot from 0 to 7 for a letter mark."""
    total = 0
    for char in name or "?":
        total = (total + ord(char)) % 8
    return total


def ago(stamp: str, now: Optional[datetime] = None) -> str:
    """A short age for an import time, such as ``3 days ago``."""
    if not stamp:
        return ""
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp
    moment = now or datetime.now(timezone.utc)
    seconds = int((moment - then).total_seconds())
    if seconds < 45:
        return "just now"
    steps = (
        (60, "minute"),
        (3600, "hour"),
        (86400, "day"),
        (86400 * 7, "week"),
        (86400 * 30, "month"),
        (86400 * 365, "year"),
    )
    chosen = "minute"
    count = max(1, seconds // 60)
    for size, label in steps:
        if seconds >= size:
            chosen = label
            count = max(1, seconds // size)
    suffix = "" if count == 1 else "s"
    return f"{count} {chosen}{suffix} ago"


def year_title(label: str) -> str:
    """``2025/26`` becomes ``2025-26 tax year``."""
    return f"{label.replace('/', '-')} tax year"


def import_summary(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Rows kept, rows skipped, and warnings for one stored import."""
    source_rows = int(entry.get("row_count") or 0)
    converted = int(entry.get("converted_rows") or 0)
    raw_report = entry.get("report")
    report: Dict[str, Any] = raw_report if isinstance(raw_report, dict) else {}
    skipped_reasons = list(report.get("skipped_reasons") or [])
    return {
        "id": str(entry.get("id") or ""),
        "filename": str(entry.get("filename") or ""),
        "format": str(entry.get("format") or ""),
        "imported_rows": converted,
        "skipped": max(len(skipped_reasons), source_rows - converted, 0),
        "warnings": int(entry.get("unmapped_rows") or 0),
        "imported_at": str(entry.get("imported_at") or ""),
        "idempotent": bool(entry.get("idempotent")),
        "unknown_columns": list(report.get("unknown_columns") or []),
        "date_problems": list(report.get("date_problems") or []),
        "number_problems": list(report.get("number_problems") or []),
        "skipped_reasons": skipped_reasons,
        "duplicates": list(report.get("duplicates") or []),
        "normalised": list(report.get("normalised") or []),
    }


def guided_steps(counts: Mapping[str, int], health_total: int) -> List[Dict[str, str]]:
    """The four home and report steps: done, in progress, or still to do."""
    imported = int(counts.get("imports", 0)) > 0
    review_open = (
        int(counts.get("needs_review", 0))
        + int(counts.get("queue", 0))
        + int(counts.get("unmapped", 0))
    )
    issues_clear = imported and health_total == 0
    review_clear = imported and review_open == 0 and issues_clear
    return [
        _step(
            "1",
            "Import",
            "Bring in a wallet or exchange file.",
            "/import",
            _state(True, imported),
        ),
        _step(
            "2",
            "Fix issues",
            "Clear the data-health list.",
            "/health",
            _state(imported, issues_clear),
        ),
        _step(
            "3",
            "Review",
            "Check the queues before you rely on a report.",
            "/review",
            _state(imported, review_clear),
        ),
        _step(
            "4",
            "Download report",
            "Save the PDF or the CSV for the year.",
            "/report",
            "current" if review_clear else "todo",
        ),
    ]


def build_home(
    desk: Desk,
    year: int,
    span: str,
    query: str,
    sort: str,
    direction: str,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Home page view. ``year`` 0 uses the tax year that contains today."""
    moment = date.today() if today is None else today
    if year and year != tax_year_containing(moment):
        moment = date(year - 1, 12, 1)
    board = dashboard(desk, moment)
    chosen = span if span in _SPANS else "all"
    rows = desk.all_rows()
    prices = latest_unit_prices(desk)
    points = _slice(_marks(rows, prices), chosen, moment, int(board["tax_year"]))
    hero, valued = _hero(board.get("holdings") or [])
    income = _income(desk, int(board["tax_year"]), bool(rows))
    health = desk.health(int(board["tax_year"]))
    health_total = int(health["total"])
    holdings = filter_holdings(board.get("holdings") or [], query, sort, direction)
    slices = _slices(board.get("holdings") or [])
    return {
        "tax_year": int(board["tax_year"]),
        "label": str(board["label"]),
        "banner": str(board["banner"]),
        "losses": str(board["losses_carried"]),
        "note": str(board.get("note") or ""),
        "allowance": str(board.get("allowance") or ""),
        "allowance_used": str(board.get("allowance_used") or ""),
        "allowance_remaining": str(board.get("allowance_remaining") or ""),
        "allowance_pct": _percent_used(board.get("allowance"), board.get("allowance_used")),
        "realised": str(board.get("realised") or ""),
        "income": income,
        "cost": _sum_field(board.get("holdings") or [], "cost"),
        "unrealised": _sum_field(board.get("holdings") or [], "unrealised"),
        "hero": hero,
        "hero_valued": valued,
        "change": _change(points),
        "span": chosen,
        "periods": [{"id": key, "label": label} for key, label in _SPAN_LABELS],
        "svg": _chart_svg(points),
        "points": [{"date": day.isoformat(), "value": f"{value}"} for day, value in points],
        "slices": slices,
        "donut": _ring(slices),
        "holdings": holdings,
        "hq": query,
        "hsort": sort if sort in _HOLD_SORTS else "value",
        "hdir": direction if direction in ("asc", "desc") else "desc",
        "steps": guided_steps(desk.counts(), health_total),
        "health_total": health_total,
        "health": health,
        "year_end": year_end_card(desk, moment),
        "filing": readiness(desk, int(board["tax_year"]), moment),
    }


def filter_holdings(
    rows: Sequence[Mapping[str, str]], query: str, sort: str, direction: str
) -> List[Mapping[str, str]]:
    """Search and sort the holdings already on the board."""
    text = query.casefold().strip()
    chosen = []
    for row in rows:
        blob = " ".join(str(row.get(key, "")) for key in ("asset", "quantity", "cost", "value"))
        if not text or text in blob.casefold():
            chosen.append(row)
    key_name = sort if sort in _HOLD_SORTS else "value"
    reverse = direction != "asc"

    def sort_key(row: Mapping[str, str]) -> Tuple[Any, str]:
        if key_name == "asset":
            return (str(row.get("asset", "")).casefold(), "")
        if key_name == "gain":
            return (_decimal(row.get("unrealised")), str(row.get("asset", "")))
        if key_name == "value":
            return (_decimal(row.get("value") or row.get("cost")), str(row.get("asset", "")))
        return (_decimal(row.get(key_name)), str(row.get("asset", "")))

    return sorted(chosen, key=sort_key, reverse=reverse)


def _state(started: bool, done: bool) -> str:
    if done:
        return "done"
    if started:
        return "current"
    return "todo"


def _step(number: str, title: str, detail: str, href: str, state: str) -> Dict[str, str]:
    labels = {"done": "Done", "current": "In progress", "todo": "To do"}
    return {
        "n": number,
        "title": title,
        "detail": detail,
        "href": href,
        "state": state,
        "label": labels.get(state, "To do"),
    }


def _income(desk: Desk, year: int, has_rows: bool) -> str:
    if not has_rows:
        return ""
    try:
        report = desk.report(year)
    except DeskError:
        return ""
    income = report.get("income") or {}
    return str(income.get("amount", ""))


def _hero(holdings: Sequence[Mapping[str, str]]) -> Tuple[str, bool]:
    valued = [_decimal(row.get("value")) for row in holdings if str(row.get("value") or "")]
    if valued:
        return f"{sum(valued, Decimal(0)).quantize(_PENCE)}", True
    costs = [_decimal(row.get("cost")) for row in holdings]
    if costs:
        return f"{sum(costs, Decimal(0)).quantize(_PENCE)}", False
    return "0.00", False


def _sum_field(holdings: Sequence[Mapping[str, str]], field: str) -> str:
    amounts = [_decimal(row.get(field)) for row in holdings if str(row.get(field) or "")]
    if not amounts:
        return ""
    return f"{sum(amounts, Decimal(0)).quantize(_PENCE)}"


def _percent_used(allowance: object, used: object) -> int:
    cap = _decimal(allowance)
    if cap <= 0:
        return 0
    ratio = _decimal(used) / cap * Decimal(100)
    percent = int(ratio.quantize(Decimal("1")))
    if percent < 0:
        return 0
    if percent > 100:
        return 100
    return percent


def _marks(rows: Sequence[Mapping[str, Any]], prices: Mapping[str, str]) -> List[Point]:
    fiat = {str(item).upper() for item in config.fiat_list}
    fiat.add("GBP")
    held: Dict[str, Decimal] = {}
    by_day: Dict[date, Decimal] = {}
    for row in sorted(rows, key=lambda item: str(item.get("timestamp") or "")):
        _apply(held, row, fiat)
        day = _as_date(str(row.get("date") or ""))
        if day is None:
            continue
        by_day[day] = _mark(held, prices, fiat)
    return sorted(by_day.items(), key=lambda item: item[0])


def _apply(held: Dict[str, Decimal], row: Mapping[str, Any], fiat: Set[str]) -> None:
    _move(held, str(row.get("buy_asset") or ""), row.get("buy_quantity"), fiat, Decimal(1))
    _move(held, str(row.get("sell_asset") or ""), row.get("sell_quantity"), fiat, Decimal(-1))


def _move(
    held: Dict[str, Decimal], asset: str, quantity: object, fiat: Set[str], sign: Decimal
) -> None:
    if not asset or asset.upper() in fiat:
        return
    held[asset] = held.get(asset, Decimal(0)) + sign * _decimal(quantity)


def _mark(held: Mapping[str, Decimal], prices: Mapping[str, str], fiat: Set[str]) -> Decimal:
    total = Decimal(0)
    for asset, quantity in held.items():
        if asset.upper() in fiat or quantity == 0:
            continue
        price = prices.get(asset) or ""
        if not price:
            continue
        total += quantity * _decimal(price)
    return total.quantize(_PENCE)


def _slice(points: Sequence[Point], span: str, today: date, tax_year: int) -> List[Point]:
    if not points:
        return []
    if span == "all":
        series = list(points)
        if series[0][1] != 0:
            series.insert(0, (series[0][0] - timedelta(days=1), Decimal("0.00")))
        if series[-1][0] < today:
            series.append((today, series[-1][1]))
        return series
    start = _span_start(span, today, tax_year)
    before = [item for item in points if item[0] < start]
    inside = [item for item in points if start <= item[0] <= today]
    carried = before[-1][1] if before else Decimal("0.00")
    series = [(start, carried)]
    series.extend(inside)
    if series[-1][0] < today:
        series.append((today, series[-1][1]))
    return series


def _span_start(span: str, today: date, tax_year: int) -> date:
    if span == "1m":
        return today - timedelta(days=30)
    if span == "1y":
        return today - timedelta(days=365)
    if span == "ytd":
        return date(today.year, 1, 1)
    if span == "tax":
        return config.get_tax_year_start(tax_year)
    return today


def _change(points: Sequence[Point]) -> Dict[str, str]:
    if not points:
        return {"amount": "0.00", "percent": "", "way": "flat"}
    delta = (points[-1][1] - points[0][1]).quantize(_PENCE)
    percent = ""
    if points[0][1] != 0:
        percent = f"{(delta / points[0][1] * Decimal(100)).quantize(Decimal('0.1'))}"
    if delta > 0:
        way = "up"
    elif delta < 0:
        way = "down"
    else:
        way = "flat"
    return {"amount": f"{delta}", "percent": percent, "way": way}


def _slices(holdings: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    weights = []
    for row in holdings:
        amount = _decimal(row.get("value"))
        if amount <= 0:
            amount = _decimal(row.get("cost"))
        if amount > 0:
            weights.append((str(row.get("asset") or ""), amount))
    total = sum((amount for _asset, amount in weights), Decimal(0))
    if total <= 0:
        return []
    slices = []
    for asset, amount in weights:
        pct = (amount / total * Decimal(100)).quantize(Decimal("0.1"))
        slices.append(
            {
                "asset": asset,
                "amount": f"{amount.quantize(_PENCE)}",
                "pct": f"{pct}",
                "slot": str(avatar_slot(asset)),
            }
        )
    return slices


def _chart_svg(points: Sequence[Point]) -> str:
    if len(points) < 2:
        return ""
    width, height = 800, 220
    left, right, top, bottom = 8, 8, 12, 8
    values = [item[1] for item in points]
    low = min(values)
    high = max(values)
    if low == high:
        low -= Decimal(1)
        high += Decimal(1)
    span = high - low
    inner_w = width - left - right
    inner_h = height - top - bottom
    last = len(points) - 1
    coords = []
    for index, (_day, value) in enumerate(points):
        xpos = left + inner_w * index / last
        ypos = top + float((high - value) / span) * inner_h
        coords.append((xpos, ypos))
    line = " ".join(f"{xpos:.1f},{ypos:.1f}" for xpos, ypos in coords)
    base = height - bottom
    area = f"{coords[0][0]:.1f},{base:.1f} " + line + f" {coords[-1][0]:.1f},{base:.1f}"
    return (
        '<svg class="chart-svg" viewBox="0 0 800 220" role="img" '
        'aria-label="Illustrative value over time">'
        f'<polygon class="chart-area" points="{area}"></polygon>'
        f'<polyline class="chart-line" points="{line}"></polyline>'
        "</svg>"
    )


def _ring(slices: Sequence[Mapping[str, str]]) -> str:
    """A ring built only from numbers, with no asset names inside."""
    if not slices:
        return ""
    radius = 42
    circ = 2 * math.pi * radius
    offset = 0.0
    rings = []
    for index, item in enumerate(slices):
        length = circ * float(item["pct"]) / 100
        rings.append(
            f'<circle class="seg seg-{index % 8}" r="{radius}" cx="60" cy="60" '
            f'stroke-dasharray="{length:.2f} {circ - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}"></circle>'
        )
        offset += length
    return (
        '<svg class="donut" viewBox="0 0 120 120" role="img" aria-label="Allocation">'
        + "".join(rings)
        + "</svg>"
    )


def _as_date(value: str) -> Optional[date]:
    if len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)
