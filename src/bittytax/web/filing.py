# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Whether one tax year is ready to hand to an accountant.

Every figure comes from a report, a queue, or a rule file the desk already
has. This module does not calculate tax and it does not contact Koinly.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..config import config
from .allocation_desk import claim_codes, links, list_lots, negligible_claims
from .errors import DeskError
from .exchange_csv import open_gaps
from .income_schedule import build_schedule
from .koinly_check import load_uploads
from .koinly_reconcile import koinly_metrics
from .losses import schedule
from .matching import match_view
from .missing_cost_report import build_report, in_window
from .queues import gaps, transfers
from .rules import load_rules, load_sa108, slug_for
from .sa108 import sa108_view
from .taxyear import tax_year_containing

DEFAULT_THRESHOLD = Decimal("1")
_PENCE = Decimal("0.01")
_STORE_NOTE = "does not store it"
_STATUS_LABELS = {"confirmed", "awaiting tax review", "unconfirmed"}
_SKIP = {"sources", "note", "url", "title", "manual"}
_MONEY_BOXES = {"13.2": "proceeds", "13.3": "costs", "13.4": "gains", "13.5": "losses"}
_COUNT_BOXES = {"13.1": "disposals"}
_TITLES = {"defi": "DeFi", "desk_review": "Desk review", "sa108": "SA108"}
CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ("missing_cost", "Missing cost"),
    ("transfer", "Transfers"),
    ("accountant_review", "Accountant review"),
    ("unconfirmed_rule", "Unconfirmed rules"),
    ("koinly_difference", "Koinly differences"),
    ("exchange_gap", "Exchange gaps"),
    ("loss_deadline", "Loss deadlines"),
    ("awaiting_decision", "Awaiting decision"),
    ("accepted_gap", "Accepted gaps"),
    ("tx_warning", "Transaction warnings"),
)
_ORDER = {key: index for index, (key, _label) in enumerate(CATEGORIES)}
_CLASS = {
    "Ready": "ok",
    "Ready with accepted gaps": "progress",
    "Not ready": "bad",
}


def load_threshold(root: Path) -> Decimal:
    """The difference that still counts. The default is one pound."""
    path = root / "ready-threshold.json"
    if not path.is_file():
        return DEFAULT_THRESHOLD
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_THRESHOLD
    if not isinstance(loaded, dict):
        return DEFAULT_THRESHOLD
    try:
        return _threshold(str(loaded.get("threshold") or "1"))
    except DeskError:
        return DEFAULT_THRESHOLD


def save_threshold(root: Path, raw: str) -> Decimal:
    """Store the threshold. Binary mode keeps the newline byte on Windows."""
    amount = _threshold(raw)
    path = root / "ready-threshold.json"
    payload = json.dumps({"threshold": f"{amount.quantize(_PENCE)}"}) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return amount


def readiness(
    desk: Any, year: int, today: Optional[date] = None, threshold: Optional[Decimal] = None
) -> Dict[str, Any]:
    """Open items for one tax year, and whether that year is ready to file."""
    when = date.today() if today is None else today
    band = load_threshold(desk.root) if threshold is None else threshold
    report = _report(desk, year)
    rows = list(desk.all_rows())
    loss_rows = _loss_rows(desk, when)
    rules = load_rules(year)
    sa108 = load_sa108(year)
    suggested, unmatched, dates = _transfer_inputs(desk, year)
    boxes, income, koinly = _koinly_inputs(desk, report, rows, year, loss_rows)
    found: List[Dict[str, str]] = []
    found.extend(missing_items(_missing_rows(report), year))
    found.extend(transfer_items(suggested, unmatched, year, dates))
    found.extend(review_items(rows, rules, _lots(desk), year))
    found.extend(unconfirmed_items(rules, sa108, slug_for(year)))
    found.extend(koinly_differences(boxes, income, koinly, band, year))
    found.extend(exchange_gap_items(desk, year))
    found.extend(loss_items(loss_rows, year))
    found.extend(decision_items(negligible_claims(desk.root), _lots(desk), year))
    found.extend(accepted_gap_items(_spec_boxes(sa108), gaps(desk.root), year))
    found.extend(transaction_warning_items(desk, year))
    return summarise(found, year, band)


def missing_items(rows: Sequence[Mapping[str, Any]], year: int) -> List[Dict[str, str]]:
    """Disposals that still have no cost basis."""
    found = []
    for row in rows:
        if not _in_year(str(row.get("date") or ""), year):
            continue
        asset = str(row.get("asset") or "Disposal")
        when = str(row.get("date") or "")
        found.append(
            _item(
                "missing_cost",
                f"{asset} {when}".strip(),
                "Disposal with no cost basis.",
                _href("/missing-cost", year),
                True,
            )
        )
    return found


def transfer_items(
    suggested: Sequence[Mapping[str, Any]],
    unmatched: Sequence[Mapping[str, Any]],
    year: int,
    dates: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, str]]:
    """Unmatched transfers, and pairs below the auto score."""
    found = []
    known = dates or {}
    for item in suggested:
        when = known.get(str(item.get("out_id") or ""), "")
        if not _in_year(when, year):
            continue
        asset = str(item.get("asset") or "Transfer")
        found.append(
            _item(
                "transfer",
                f"{asset} score {item.get('score')}",
                f"Low confidence. {item.get('reason') or ''}".strip(),
                _href("/matches", year),
                True,
            )
        )
    for item in unmatched:
        if str(item.get("status") or "open") == "closed":
            continue
        if not _in_year(str(item.get("when") or ""), year):
            continue
        asset = str(item.get("asset") or "Transfer")
        when = str(item.get("when") or "")
        detail = str(item.get("detail") or "Unmatched own-wallet transfer.")
        found.append(
            _item("transfer", f"{asset} {when}".strip(), detail, _href("/matches", year), True)
        )
    return found


def review_items(
    rows: Sequence[Mapping[str, Any]],
    rules: Optional[Mapping[str, Any]],
    lots: Sequence[Mapping[str, Any]],
    year: int,
) -> List[Dict[str, str]]:
    """DeFi or allocation rows the rule file still leaves with an accountant."""
    flagged = _flagged_tags(rules)
    found = []
    for row in rows:
        tag = str(row.get("tag") or "")
        if tag not in flagged:
            continue
        if not _in_year(str(row.get("timestamp") or row.get("date") or ""), year):
            continue
        asset = str(row.get("buy_asset") or row.get("sell_asset") or row.get("asset") or "")
        when = _day(str(row.get("timestamp") or row.get("date") or ""))
        found.append(
            _item(
                "accountant_review",
                f"{flagged[tag]} {asset} {when}".strip(),
                "Flagged for accountant review.",
                _href("/defi", year),
                True,
            )
        )
    for lot in lots:
        if _money(lot.get("refunded")) > 0 or not _flagged(lot.get("accountant_review")):
            continue
        name = str(lot.get("label") or lot.get("asset") or "Allocation")
        found.append(
            _item(
                "accountant_review",
                name,
                "Allocation row flagged for accountant review.",
                "/allocations",
                True,
            )
        )
    return found


def unconfirmed_names(document: Optional[Mapping[str, Any]], file_label: str) -> List[str]:
    """Names of rules whose entry is unconfirmed, including a file-level flag."""
    if not isinstance(document, dict):
        return []
    names: List[str] = []
    if document.get("confirmed") is False or document.get("stub") is True:
        names.append(file_label)
    _walk(document, names)
    return _unique(names)


def unconfirmed_items(
    rules: Optional[Mapping[str, Any]], sa108: Optional[Mapping[str, Any]], slug: str
) -> List[Dict[str, str]]:
    """Unconfirmed year-file entries, shown by name."""
    found = []
    for name in unconfirmed_names(rules, f"{slug} rules"):
        found.append(
            _item(
                "unconfirmed_rule",
                name,
                f"The {slug} rules file is not confirmed.",
                "/config",
                True,
            )
        )
    for name in unconfirmed_names(sa108, f"{slug} SA108"):
        found.append(
            _item(
                "unconfirmed_rule",
                name,
                f"The {slug} SA108 file is not confirmed.",
                "/config",
                True,
            )
        )
    return found


def exchange_gap_items(desk: Any, year: int) -> List[Dict[str, str]]:
    """Exchange rows that do not agree with the imported Koinly file."""
    found = []
    for gap in open_gaps(desk, year):
        found.append(_item("exchange_gap", gap["name"], gap["detail"], gap["href"], True))
    return found


def koinly_differences(
    boxes: Sequence[Mapping[str, Any]],
    income: Mapping[str, Any],
    koinly: Mapping[str, Optional[Decimal]],
    threshold: Decimal,
    year: int,
) -> List[Dict[str, str]]:
    """Desk figures against the imported Koinly file, by box."""
    found = []
    by_box = {str(box.get("box") or ""): box for box in boxes}
    for number, metric in _COUNT_BOXES.items():
        found.extend(_box_diff(by_box.get(number), metric, koinly, threshold, year, False))
    for number, metric in _MONEY_BOXES.items():
        found.extend(_box_diff(by_box.get(number), metric, koinly, threshold, year, True))
    income_box = {"box": "17", "label": "Other UK income", "value": income.get("box_17", "")}
    found.extend(_income_diff(income_box, koinly.get("income"), threshold, year))
    return found


def loss_items(rows: Sequence[Mapping[str, Any]], year: int) -> List[Dict[str, str]]:
    """Unclaimed losses that are near, or past, the four-year deadline."""
    found = []
    for row in rows:
        try:
            loss_year = int(str(row.get("year") or "0"))
        except ValueError:
            continue
        if loss_year > year or str(row.get("claim_status") or "") == "claimed":
            continue
        if str(row.get("warning") or "") not in {"near", "past"}:
            continue
        if _money(row.get("gross_losses")) <= 0:
            continue
        state = "past" if row.get("warning") == "past" else "near"
        found.append(
            _item(
                "loss_deadline",
                f"Unclaimed loss {loss_year}",
                f"The four-year deadline {row.get('deadline') or ''} is {state}.",
                "/losses",
                True,
            )
        )
    return found


def decision_items(
    claims: Sequence[Mapping[str, Any]], lots: Sequence[Mapping[str, Any]], year: int
) -> List[Dict[str, str]]:
    """Negligible-value claims and refunds that still need a decision."""
    found = []
    for claim in claims:
        when = str(claim.get("claim_date") or claim.get("date") or "")
        if not _in_year(when, year) or _decided(claim):
            continue
        asset = str(claim.get("asset") or claim.get("payment") or "asset")
        found.append(
            _item(
                "awaiting_decision",
                f"Negligible-value claim {asset}",
                "Awaiting a decision.",
                "/allocations",
                True,
            )
        )
    for lot in lots:
        if str(lot.get("linked") or "") == "yes" or _money(lot.get("refunded")) <= 0:
            continue
        name = str(lot.get("asset") or lot.get("label") or lot.get("payment") or "Refund")
        found.append(
            _item(
                "awaiting_decision",
                f"Refund {name}",
                f"Refunded {lot.get('refunded')} is still open.",
                "/allocations",
                True,
            )
        )
    return found


def accepted_gap_items(
    boxes: Sequence[Mapping[str, Any]], known: Sequence[Mapping[str, Any]], year: int
) -> List[Dict[str, str]]:
    """Permanent gaps. They are recorded and they do not stop the year."""
    found = []
    for box in boxes:
        note = str(box.get("note") or "")
        if _STORE_NOTE not in note.casefold():
            continue
        label = str(box.get("label") or "").strip()
        number = str(box.get("box") or "")
        name = f"SA108 box {number} {label}".strip()
        found.append(
            _item(
                "accepted_gap",
                name,
                "Accepted gap. The calculator does not store this figure.",
                _href("/report", year),
                False,
            )
        )
    for gap in known:
        venue = str(gap.get("venue") or "Known gap")
        detail = str(gap.get("note") or "Known gap.")
        found.append(
            _item(
                "accepted_gap",
                venue,
                f"Accepted gap. {detail}".strip(),
                _href("/queues", year),
                False,
            )
        )
    return found


def summarise(items: Sequence[Mapping[str, str]], year: int, threshold: Decimal) -> Dict[str, Any]:
    """Status and counts. Accepted gaps do not by themselves make a year unready."""
    ordered = sorted(
        (dict(item) for item in items),
        key=lambda item: (_ORDER.get(item["category"], 99), item["name"], item["detail"]),
    )
    tally = {key: 0 for key, _label in CATEGORIES}
    for item in ordered:
        weight = int(item.get("weight") or 1)
        tally[item["category"]] = tally.get(item["category"], 0) + weight
    counts = [{"key": key, "label": label, "count": tally[key]} for key, label in CATEGORIES]
    blocking = sum(1 for item in ordered if item.get("blocking") == "yes")
    gap_count = sum(1 for item in ordered if item.get("kind") == "accepted gap")
    status = _status(blocking, gap_count)
    return {
        "tax_year": year,
        "label": config.format_tax_year(year),
        "status": status,
        "status_class": _CLASS[status],
        "items": ordered,
        "counts": counts,
        "blocking_count": blocking,
        "gap_count": gap_count,
        "summary": _blurb(status, blocking, gap_count),
        "threshold": f"{threshold.quantize(_PENCE)}",
    }


def items_csv(board: Mapping[str, Any]) -> str:
    """The open-item list. Line feeds stay as they are, including on Windows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tax_year", "status", "category", "name", "detail", "page", "kind"])
    for item in board.get("items") or []:
        writer.writerow(
            [
                board.get("tax_year", ""),
                board.get("status", ""),
                item.get("category_label", ""),
                item.get("name", ""),
                item.get("detail", ""),
                item.get("href", ""),
                item.get("kind", ""),
            ]
        )
    return buffer.getvalue()


def _missing_rows(report: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    if report is None:
        return []
    window = in_window(
        report.get("missing_cost") or [],
        str(report.get("start") or ""),
        str(report.get("end") or ""),
    )
    return list(build_report(report.get("disposals") or [], window)["rows"])


def _transfer_inputs(
    desk: Any, year: int
) -> Tuple[List[Mapping[str, Any]], List[Dict[str, str]], Dict[str, str]]:
    view = match_view(desk)
    dates = {
        str(row.get("id") or ""): str(row.get("timestamp") or row.get("date") or "")
        for row in desk.all_rows()
    }
    review: Dict[str, Any] = {"needs_review": [], "queue": []}
    if desk.all_rows():
        try:
            review = desk.review(year)
        except DeskError:
            review = {"needs_review": [], "queue": []}
    return list(view.get("suggested") or []), transfers(review, _notes(desk.root)), dates


def _koinly_inputs(
    desk: Any,
    report: Optional[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    year: int,
    loss_rows: Sequence[Mapping[str, str]],
) -> Tuple[List[Mapping[str, Any]], Mapping[str, Any], Mapping[str, Optional[Decimal]]]:
    boxes: List[Mapping[str, Any]] = []
    income: Mapping[str, Any] = {}
    if report is not None:
        loss_row = next((row for row in loss_rows if row.get("year") == str(year)), {})
        chosen = _year_rows(rows, report)
        view = sa108_view(report, year, loss_row, rows=chosen, claim_codes=claim_codes(desk.root))
        if view is not None:
            boxes = list(view.get("boxes") or [])
        income = build_schedule(report, chosen, year)
    summary, complete = load_uploads(desk.root, year)
    if not summary.strip() and not complete.strip():
        return boxes, income, {}
    return boxes, income, koinly_metrics(summary, complete)


def _lots(desk: Any) -> List[Dict[str, Any]]:
    path = str(desk.records_path) if desk.records_path.is_file() else ""
    found = list(list_lots(path))
    found.extend(links(desk.root))
    return found


def _loss_rows(desk: Any, today: date) -> List[Dict[str, str]]:
    try:
        return schedule(desk, today)
    except DeskError:
        return []


def _report(desk: Any, year: int) -> Optional[Dict[str, Any]]:
    if not desk.all_rows():
        return None
    try:
        return desk.report(year)
    except DeskError:
        return None


def _spec_boxes(document: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    if not isinstance(document, dict):
        return []
    return [box for box in document.get("boxes") or [] if isinstance(box, dict)]


def _box_diff(
    box: Optional[Mapping[str, Any]],
    metric: str,
    koinly: Mapping[str, Optional[Decimal]],
    threshold: Decimal,
    year: int,
    money: bool,
) -> List[Dict[str, str]]:
    if box is None or metric not in koinly:
        return []
    other = koinly.get(metric)
    ours = _optional_money(box.get("value"))
    if other is None or ours is None:
        return []
    return _diff_item(f"SA108 box {box.get('box')}", ours, other, threshold, year, money)


def _income_diff(
    box: Mapping[str, Any],
    other: Optional[Decimal],
    threshold: Decimal,
    year: int,
) -> List[Dict[str, str]]:
    ours = _optional_money(box.get("value"))
    if other is None or ours is None:
        return []
    return _diff_item(f"Income box {box.get('box')}", ours, other, threshold, year, True)


def _diff_item(
    name: str,
    ours: Decimal,
    other: Decimal,
    threshold: Decimal,
    year: int,
    money: bool,
) -> List[Dict[str, str]]:
    difference = ours - other
    if money and abs(difference) <= threshold:
        return []
    if not money and difference == 0:
        return []
    show = _show_money if money else _show_count
    detail = f"Desk {show(ours)}. Koinly {show(other)}. Difference {show(difference)}."
    return [_item("koinly_difference", name, detail, _href("/koinly", year), True)]


def _flagged_tags(rules: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    found: Dict[str, str] = {}
    if not isinstance(rules, dict):
        return found
    for tag in (rules.get("defi") or {}).get("tags") or []:
        if not isinstance(tag, dict):
            continue
        needs = tag.get("accountant_review") or tag.get("effect") == "accountant_review"
        ident = str(tag.get("id") or "")
        if needs and ident:
            found[ident] = str(tag.get("label") or ident)
    return found


def _walk(node: object, names: List[str]) -> None:
    if isinstance(node, list):
        for value in node:
            _consider(value, "", names)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _SKIP:
            continue
        _consider(value, str(key), names)


def _consider(value: object, key: str, names: List[str]) -> None:
    if isinstance(value, dict) and value.get("confirmed") is False:
        names.append(_entry_name(value, key))
    _walk(value, names)


def _entry_name(node: Mapping[str, Any], key: str) -> str:
    label = str(node.get("label") or "").strip()
    if label.casefold() in _STATUS_LABELS:
        label = ""
    number = str(node.get("box") or "")
    if number:
        return f"SA108 box {number} {label}".strip()
    if label:
        return label
    ident = str(node.get("id") or "").strip()
    if ident:
        return ident.replace("_", " ")
    pretty = _TITLES.get(key, key.replace("_", " ")).strip()
    return pretty or "Unconfirmed rule"


def _decided(claim: Mapping[str, Any]) -> bool:
    status = str(claim.get("status") or "")
    confirmed = claim.get("confirmed")
    return status == "claimed" and confirmed in {True, "true"}


def _flagged(value: object) -> bool:
    return value in {True, "true", "yes"}


def transaction_warning_items(desk: Any, year: int) -> List[Dict[str, str]]:
    """A count of transaction warnings for this tax year. Dust is already hidden."""
    from .tx_workspace import warning_total  # pylint: disable=import-outside-toplevel

    count = warning_total(desk, year)
    if not count:
        return []
    return [
        _item(
            "tx_warning",
            "Transaction warnings",
            f"{count} rows still have a warning.",
            "/transactions?warning=any",
            True,
            count,
        )
    ]


def _item(
    category: str, name: str, detail: str, href: str, blocking: bool, weight: int = 1
) -> Dict[str, str]:
    label = dict(CATEGORIES)[category]
    packed = {
        "category": category,
        "category_label": label,
        "name": name,
        "detail": detail,
        "href": href,
        "blocking": "yes" if blocking else "no",
        "kind": "open" if blocking else "accepted gap",
    }
    if weight != 1:
        packed["weight"] = str(weight)
    return packed


def _status(blocking: int, gap_count: int) -> str:
    if blocking:
        return "Not ready"
    if gap_count:
        return "Ready with accepted gaps"
    return "Ready"


def _blurb(status: str, blocking: int, gap_count: int) -> str:
    if status == "Ready":
        return "Nothing is open for this year."
    gap_text = _plural(gap_count, "accepted gap", "accepted gaps")
    if status == "Ready with accepted gaps":
        return f"{gap_text}. Nothing else is open."
    return f"{_plural(blocking, 'open item', 'open items')}. {gap_text}."


def _plural(count: int, one: str, many: str) -> str:
    word = one if count == 1 else many
    return f"{count} {word}"


def _href(path: str, year: int) -> str:
    return f"{path}?tax_year={year}"


def _year_rows(
    rows: Sequence[Mapping[str, Any]], report: Mapping[str, Any]
) -> List[Mapping[str, Any]]:
    start = str(report.get("start") or "")
    end = str(report.get("end") or "")
    chosen = []
    for row in rows:
        day = _day(str(row.get("date") or row.get("timestamp") or ""))
        if start and end and day and not start <= day <= end:
            continue
        chosen.append(row)
    return chosen


def _notes(root: Path) -> Dict[str, Dict[str, str]]:
    path = root / "transfer-notes.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items() if isinstance(value, dict)}


def _in_year(value: str, year: int) -> bool:
    day = _day(value)
    if not day:
        return True
    try:
        when = date.fromisoformat(day)
    except ValueError:
        return True
    return tax_year_containing(when) == year


def _day(value: str) -> str:
    text = value.strip()[:10]
    parts = text.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return text


def _unique(names: Sequence[str]) -> List[str]:
    seen = set()
    kept = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            kept.append(name)
    return kept


def _show_money(value: Decimal) -> str:
    return f"{value.quantize(_PENCE)}"


def _show_count(value: Decimal) -> str:
    if value == value.to_integral():
        return str(int(value))
    return _show_money(value)


def _optional_money(value: object) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("£", ""))
    except InvalidOperation:
        return None


def _money(value: object) -> Decimal:
    parsed = _optional_money(value)
    return Decimal(0) if parsed is None else parsed


def _threshold(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.strip() or "1")
    except InvalidOperation as exc:
        raise DeskError("Enter the threshold as a number.") from exc
    if amount < 0:
        raise DeskError("Enter the threshold as a positive number or zero.")
    return amount
