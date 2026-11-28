# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Pair a withdrawal with a deposit. The calculator already treats those as transfers."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import DeskError
from .events import append_event

_TAG = "own_wallet_transfer"
_DEFAULTS = {
    "window_hours": 48,
    "fee_percent": "2",
    "fee_abs": "0",
    "auto_score": 80,
    "suggest_score": 50,
}


def load_settings(root: Path) -> Dict[str, Any]:
    """Window and fee tolerance. Missing keys use the defaults."""
    path = root / "match-settings.json"
    stored: Dict[str, Any] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            stored = loaded
    settings = dict(_DEFAULTS)
    settings.update({key: stored[key] for key in _DEFAULTS if key in stored})
    return _checked(settings)


def save_settings(root: Path, raw: Mapping[str, str]) -> Dict[str, Any]:
    """Store the form. Values outside the allowed range are refused."""
    settings = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if raw.get(key):
            settings[key] = raw[key]
    checked = _checked(settings)
    path = root / "match-settings.json"
    path.write_text(json.dumps(checked, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return checked


def match_view(desk: Any) -> Dict[str, Any]:
    """Suggestions, accepted pairs, and the current settings."""
    settings = load_settings(desk.root)
    active, rejected = _state(_read_log(desk.root))
    rows = desk.all_rows()
    proposals = propose(rows, settings)
    used = _used(active)
    suggested = []
    for item in proposals:
        key = (item["out_id"], item["in_id"])
        if key in active or key in rejected or item["out_id"] in used or item["in_id"] in used:
            continue
        if item["score"] >= int(settings["suggest_score"]):
            if item["score"] < int(settings["auto_score"]):
                suggested.append(item)
    return {
        "settings": settings,
        "suggested": suggested,
        "matched": list(active.values()),
        "note": (
            "A matched pair is stored as a withdrawal and a deposit. "
            "The calculator already treats those as a transfer between your own wallets. "
            "This is not a new tax rule."
        ),
    }


def apply_auto_matches(desk: Any, who: str) -> List[Dict[str, Any]]:
    """Accept every pair at or above the auto score. Lower scores stay suggested."""
    settings = load_settings(desk.root)
    active, rejected = _state(_read_log(desk.root))
    used = _used(active)
    applied = []
    for item in propose(desk.all_rows(), settings):
        key = (item["out_id"], item["in_id"])
        if key in rejected or item["out_id"] in used or item["in_id"] in used:
            continue
        if item["score"] < int(settings["auto_score"]):
            continue
        applied.append(_accept(desk, item, who, "auto"))
        used.add(item["out_id"])
        used.add(item["in_id"])
    return applied


def decide(desk: Any, out_id: str, in_id: str, action: str, who: str) -> Dict[str, Any]:
    """Accept, reject, or unmatch one pair. The choice is appended to the log."""
    if action not in {"accept", "reject", "unmatch"}:
        raise DeskError("Choose accept, reject, or unmatch.")
    settings = load_settings(desk.root)
    active, _rejected = _state(_read_log(desk.root))
    key = (out_id, in_id)
    if action == "unmatch":
        return _unmatch(desk, active.get(key), who)
    if action == "reject":
        return _reject(desk, out_id, in_id, who)
    found = _find_proposal(desk.all_rows(), settings, out_id, in_id)
    if found is None:
        raise DeskError("Those two rows are not a suggested pair.")
    return _accept(desk, found, who, "accept")


def propose(rows: Sequence[Mapping[str, Any]], settings: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Candidate pairs, highest score first. Nothing is written."""
    outgoing = [row for row in rows if _side(row) == "out"]
    incoming = [row for row in rows if _side(row) == "in"]
    window = int(settings["window_hours"]) * 3600
    percent = Decimal(str(settings["fee_percent"]))
    absolute = Decimal(str(settings["fee_abs"]))
    pairs = []
    for left in outgoing:
        for right in incoming:
            scored = _score(left, right, window, percent, absolute)
            if scored is None:
                continue
            score, reason = scored
            pairs.append(_pair(left, right, score, reason))
    pairs.sort(key=lambda item: (-int(item["score"]), item["out_id"], item["in_id"]))
    chosen: List[Dict[str, Any]] = []
    used: set = set()
    for item in pairs:
        if item["out_id"] in used or item["in_id"] in used:
            continue
        used.add(item["out_id"])
        used.add(item["in_id"])
        chosen.append(item)
    return chosen


def _accept(desk: Any, item: Mapping[str, Any], who: str, action: str) -> Dict[str, Any]:
    out_row = desk.transaction(str(item["out_id"]))
    in_row = desk.transaction(str(item["in_id"]))
    _retitle(desk, out_row, "Withdrawal", who)
    _retitle(desk, in_row, "Deposit", who)
    entry = {
        "action": action,
        "out_id": str(item["out_id"]),
        "in_id": str(item["in_id"]),
        "score": str(item["score"]),
        "reason": str(item["reason"]),
        "asset": str(item["asset"]),
        "out_before": str(out_row["type"]),
        "in_before": str(in_row["type"]),
        "out_tag": str(out_row.get("tag") or ""),
        "in_tag": str(in_row.get("tag") or ""),
        "who": who or "local",
    }
    _append(desk.root, entry)
    append_event(
        desk.root,
        "match-" + action,
        who,
        f"{action} transfer pair {item['asset']} score {item['score']}.",
        {"out_id": entry["out_id"], "in_id": entry["in_id"]},
    )
    return entry


def _reject(desk: Any, out_id: str, in_id: str, who: str) -> Dict[str, Any]:
    entry = {
        "action": "reject",
        "out_id": out_id,
        "in_id": in_id,
        "score": "",
        "reason": "rejected",
        "asset": "",
        "out_before": "",
        "in_before": "",
        "out_tag": "",
        "in_tag": "",
        "who": who or "local",
    }
    _append(desk.root, entry)
    append_event(
        desk.root,
        "match-reject",
        who,
        "Rejected a suggested transfer pair.",
        {"out_id": out_id, "in_id": in_id},
    )
    return entry


def _unmatch(desk: Any, current: Optional[Mapping[str, str]], who: str) -> Dict[str, Any]:
    if current is None:
        raise DeskError("That pair is not matched.")
    _restore(desk, str(current["out_id"]), str(current["out_before"]), str(current["out_tag"]), who)
    _restore(desk, str(current["in_id"]), str(current["in_before"]), str(current["in_tag"]), who)
    entry = {
        "action": "unmatch",
        "out_id": str(current["out_id"]),
        "in_id": str(current["in_id"]),
        "score": str(current.get("score") or ""),
        "reason": "unmatched",
        "asset": str(current.get("asset") or ""),
        "out_before": str(current.get("out_before") or ""),
        "in_before": str(current.get("in_before") or ""),
        "out_tag": "",
        "in_tag": "",
        "who": who or "local",
    }
    _append(desk.root, entry)
    append_event(
        desk.root,
        "match-unmatch",
        who,
        "Unmatched a transfer pair.",
        {"out_id": entry["out_id"], "in_id": entry["in_id"]},
    )
    return entry


def _retitle(desk: Any, row: Mapping[str, Any], new_type: str, who: str) -> None:
    if str(row["type"]) == new_type and str(row.get("tag") or "") == _TAG:
        return
    desk.reclassify(
        str(row["id"]),
        new_type,
        "Own-wallet transfer match.",
        who or "local",
        _TAG,
    )


def _restore(desk: Any, row_id: str, previous: str, tag: str, who: str) -> None:
    if not previous:
        return
    current = desk.transaction(row_id)
    if str(current["type"]) == previous and str(current.get("tag") or "") == tag:
        return
    desk.reclassify(
        row_id, previous, "Unmatch own-wallet transfer.", who or "local", tag, allow_same=True
    )


def _find_proposal(
    rows: Sequence[Mapping[str, Any]], settings: Mapping[str, Any], out_id: str, in_id: str
) -> Optional[Dict[str, Any]]:
    for item in propose(rows, settings):
        if item["out_id"] == out_id and item["in_id"] == in_id:
            return item
    return None


def _pair(
    left: Mapping[str, Any], right: Mapping[str, Any], score: int, reason: str
) -> Dict[str, Any]:
    return {
        "out_id": str(left["id"]),
        "in_id": str(right["id"]),
        "asset": str(left["sell_asset"]),
        "amount": str(left["sell_quantity"]),
        "out_wallet": str(left["wallet"]),
        "in_wallet": str(right["wallet"]),
        "score": score,
        "reason": reason,
    }


def _score(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    window: int,
    percent: Decimal,
    absolute: Decimal,
) -> Optional[Tuple[int, str]]:
    if str(left["wallet"]) == "" or str(left["wallet"]) == str(right["wallet"]):
        return None
    if str(left["sell_asset"]) != str(right["buy_asset"]):
        return None
    out_qty = _decimal(left.get("sell_quantity"))
    in_qty = _decimal(right.get("buy_quantity"))
    if out_qty <= 0 or in_qty <= 0:
        return None
    out_when = _when(str(left.get("timestamp") or ""))
    in_when = _when(str(right.get("timestamp") or ""))
    if out_when is None or in_when is None:
        return None
    delta = abs(int((out_when - in_when).total_seconds()))
    if delta > window:
        return None
    same_fee = str(left.get("fee_asset") or "") == str(left["sell_asset"])
    fee = _decimal(left.get("fee_quantity")) if same_fee else Decimal(0)
    diff = abs(out_qty - in_qty)
    limit = max(absolute, (out_qty * percent / Decimal(100)))
    limit = max(limit, fee)
    if diff > limit:
        return None
    closeness = 0 if window == 0 else 40 * (window - delta) / window
    amount_score = 50 if diff == 0 else 30
    score = int(closeness + amount_score + 10)
    reason = "same asset, amount inside the fee tolerance, inside the time window"
    return score, reason


def _side(row: Mapping[str, Any]) -> str:
    kind = str(row.get("type") or "")
    if kind.startswith("_"):
        return ""
    sold = str(row.get("sell_asset") or "") and str(row.get("sell_quantity") or "")
    bought = str(row.get("buy_asset") or "") and str(row.get("buy_quantity") or "")
    if sold and not bought:
        return "out"
    if bought and not sold:
        return "in"
    return ""


def _when(text: str) -> Optional[datetime]:
    cleaned = text.strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _checked(settings: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        window = int(settings["window_hours"])
        percent = Decimal(str(settings["fee_percent"]))
        absolute = Decimal(str(settings["fee_abs"]))
        auto = int(settings["auto_score"])
        suggest = int(settings["suggest_score"])
    except (ValueError, InvalidOperation, KeyError) as exc:
        raise DeskError("The match settings must be numbers.") from exc
    if window < 1 or window > 24 * 14:
        raise DeskError("The time window must be between 1 and 336 hours.")
    if percent < 0 or percent > 10 or absolute < 0:
        raise DeskError("The fee tolerance must be between 0 and 10 percent.")
    if not 0 <= suggest <= auto <= 100:
        raise DeskError("The suggest score cannot be above the auto score.")
    return {
        "window_hours": window,
        "fee_percent": f"{percent}",
        "fee_abs": f"{absolute}",
        "auto_score": auto,
        "suggest_score": suggest,
    }


def _state(rows: Sequence[Mapping[str, str]]) -> Tuple[Dict[Tuple[str, str], Dict[str, str]], set]:
    active: Dict[Tuple[str, str], Dict[str, str]] = {}
    rejected: set = set()
    for item in rows:
        key = (str(item.get("out_id") or ""), str(item.get("in_id") or ""))
        action = str(item.get("action") or "")
        if action in {"auto", "accept"}:
            active[key] = dict(item)
            rejected.discard(key)
        elif action == "reject":
            active.pop(key, None)
            rejected.add(key)
        elif action == "unmatch":
            active.pop(key, None)
            rejected.discard(key)
    return active, rejected


def _used(active: Mapping[Tuple[str, str], Mapping[str, str]]) -> set:
    found: set = set()
    for out_id, in_id in active:
        found.add(out_id)
        found.add(in_id)
    return found


def _read_log(root: Path) -> List[Dict[str, str]]:
    path = root / "matches.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                rows.append({str(key): str(value) for key, value in loaded.items()})
    return rows


def _append(root: Path, entry: Mapping[str, str]) -> None:
    path = root / "matches.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(entry), ensure_ascii=True, sort_keys=True) + "\n")
