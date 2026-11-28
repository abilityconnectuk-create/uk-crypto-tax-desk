# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Desk rules and bulk edits. They relabel rows. They are not tax rules."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .convert import IGNORED, POSSIBLE_SPAM, allowed_types
from .errors import DeskError
from .events import append_event
from .rules import defi_tags

_FIELDS = {"wallet", "asset", "description", "type"}
_ACTIONS = {"reclassify", "tag", "spam", "ignore", "undo"}


def load_rules(root: Path) -> List[Dict[str, str]]:
    path = root / "user-rules.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    rows = loaded.get("rules", []) if isinstance(loaded, dict) else []
    return [dict(item) for item in rows if isinstance(item, dict)]


def save_rule(root: Path, form: Mapping[str, str]) -> Dict[str, str]:
    """Append one rule. It is not applied until a later step."""
    field = form.get("field", "")
    if field not in _FIELDS:
        raise DeskError("Choose wallet, asset, description, or type.")
    needle = " ".join(form.get("match", "").split())
    if not needle or len(needle) > 80:
        raise DeskError("Enter the text to match, up to 80 characters.")
    new_type = form.get("set_type", "").strip()
    tag = form.get("set_tag", "").strip()
    if new_type and new_type not in allowed_types():
        raise DeskError("Choose a BittyTax transaction type.")
    known = {item["id"] for item in defi_tags()}
    if tag and tag not in known:
        raise DeskError("Choose a DeFi tag from the rule file.")
    if not new_type and not tag:
        raise DeskError("Choose a type or a tag to set.")
    rule = {
        "id": uuid.uuid4().hex[:12],
        "field": field,
        "match": needle,
        "set_type": new_type,
        "set_tag": tag,
        "enabled": "yes",
    }
    rules = load_rules(root)
    rules.append(rule)
    _write(root, rules)
    return rule


def set_enabled(root: Path, rule_id: str, enabled: bool) -> None:
    rules = load_rules(root)
    for rule in rules:
        if rule.get("id") == rule_id:
            rule["enabled"] = "yes" if enabled else "no"
    _write(root, rules)


def preview_rule(
    rows: Sequence[Mapping[str, Any]], rule: Mapping[str, str]
) -> List[Dict[str, str]]:
    """Rows a rule would relabel. Nothing is written."""
    found = []
    for row in rows:
        if _hits(row, rule):
            found.append(
                {
                    "id": str(row["id"]),
                    "type": str(row["type"]),
                    "wallet": str(row["wallet"]),
                    "asset": ", ".join(row.get("assets") or []),
                    "note": str(row.get("note") or "")[:80],
                }
            )
    return found


def rule_changes(
    rows: Sequence[Mapping[str, Any]], rule: Mapping[str, str]
) -> List[Dict[str, str]]:
    """Changes for one enabled rule. Rows that already match are left out."""
    if rule.get("enabled") != "yes":
        return []
    changes = []
    for row in _matched(rows, rule):
        new_type = rule.get("set_type") or str(row["type"])
        tag = rule.get("set_tag") or ""
        if str(row["type"]) == new_type and str(row.get("tag") or "") == tag:
            continue
        changes.append(
            {
                "row_id": str(row["id"]),
                "type": new_type,
                "tag": tag,
                "why": f"Rule {rule.get('id', '')} on {rule.get('field', '')}.",
            }
        )
    return changes


def apply_enabled_rules(desk: Any, who: str) -> int:
    """Relabel rows for every enabled rule and append one log line."""
    rules = [rule for rule in load_rules(desk.root) if rule.get("enabled") == "yes"]
    if not rules:
        return 0
    changes: List[Dict[str, str]] = []
    seen = set()
    for rule in rules:
        for change in rule_changes(desk.all_rows(), rule):
            if change["row_id"] in seen:
                continue
            seen.add(change["row_id"])
            changes.append(change)
    count = desk.reclassify_many(changes, who or "local")
    if count:
        append_event(
            desk.root,
            "rules",
            who,
            f"Applied rules to {count} rows.",
            {"rows": str(count)},
        )
    return count


def bulk_changes(
    desk: Any, row_ids: Sequence[str], action: str, new_type: str, tag: str, why: str
) -> List[Dict[str, str]]:
    """Build the edits for one bulk action. Undo uses the previous type."""
    if action not in _ACTIONS:
        raise DeskError("Choose a bulk action.")
    cleaned = why.strip()
    if action != "undo" and not cleaned:
        raise DeskError("Say why these rows should change.")
    if not row_ids:
        raise DeskError("Select at least one transaction.")
    changes = []
    for row_id in row_ids:
        row = desk.transaction(row_id)
        changes.append(_one(row, action, new_type, tag, cleaned or "Undo bulk edit."))
    return changes


def apply_bulk(
    desk: Any, row_ids: Sequence[str], action: str, new_type: str, tag: str, why: str, who: str
) -> int:
    changes = bulk_changes(desk, row_ids, action, new_type, tag, why)
    count = desk.reclassify_many(changes, who or "local")
    append_event(
        desk.root,
        "bulk-" + action,
        who,
        f"Bulk {action} on {count} rows.",
        {"rows": str(count), "action": action},
    )
    return count


def _one(row: Mapping[str, Any], action: str, new_type: str, tag: str, why: str) -> Dict[str, str]:
    current = str(row["type"])
    chosen = tag
    target = new_type or current
    if action == "spam":
        target, chosen = POSSIBLE_SPAM, ""
    elif action == "ignore":
        target, chosen = IGNORED, ""
    elif action == "tag":
        target = current
    elif action == "undo":
        log = row.get("log") or []
        if not log:
            raise DeskError("That row has no earlier type to restore.")
        target = str(log[-1].get("before_type") or current)
        chosen = ""
    return {"row_id": str(row["id"]), "type": target, "tag": chosen, "why": why}


def _matched(rows: Sequence[Mapping[str, Any]], rule: Mapping[str, str]) -> List[Mapping[str, Any]]:
    return [row for row in rows if _hits(row, rule)]


def _hits(row: Mapping[str, Any], rule: Mapping[str, str]) -> bool:
    needle = str(rule.get("match") or "").casefold()
    field = str(rule.get("field") or "")
    if field == "wallet":
        haystack = str(row.get("wallet") or "")
    elif field == "asset":
        haystack = " ".join(row.get("assets") or [])
    elif field == "type":
        haystack = str(row.get("type") or "")
    else:
        haystack = str(row.get("note") or "")
    return bool(needle) and needle in haystack.casefold()


def _write(root: Path, rules: List[Dict[str, str]]) -> None:
    path = root / "user-rules.json"
    path.write_text(json.dumps({"rules": rules}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
