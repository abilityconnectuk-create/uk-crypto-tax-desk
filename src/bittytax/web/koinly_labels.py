# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Apply the editable Koinly label table after the converter has read a file."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from .events import append_event
from .rules import config_root

_WHY = "Koinly label table."


def load_label_table(path: Optional[Path] = None) -> List[Dict[str, str]]:
    """Rows from ``config/koinly_labels.yaml``."""
    chosen = path or (config_root() / "koinly_labels.yaml")
    loaded = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    rows = []
    for item in loaded.get("labels", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "names": " ".join(str(name) for name in item.get("names") or []),
                "names_folded": "|".join(_fold(str(name)) for name in item.get("names") or []),
                "direction": str(item.get("direction") or ""),
                "type": str(item.get("type") or ""),
                "tag": str(item.get("tag") or ""),
            }
        )
    return rows


def apply_koinly_labels(desk: Any, entry: Mapping[str, Any], who: str) -> List[str]:
    """Set types and tags from the table. Unknown labels are listed for review."""
    if str(entry.get("format") or "") != "Koinly":
        return []
    raw = desk.root / str(entry.get("raw_path") or "")
    if not raw.is_file():
        return []
    source = _source_rows(raw.read_bytes())
    outputs = [row for row in desk.all_rows() if row.get("import_id") == entry.get("id")]
    table = load_label_table()
    changes: List[Dict[str, str]] = []
    unknown: List[str] = []
    cursor = 0
    for raw_row in source:
        width = 2 if _fold(raw_row["type"]) == "transfer" else 1
        group = outputs[cursor : cursor + width]
        cursor += width
        label = raw_row["label"]
        hit = _lookup(table, label, raw_row["type"], raw_row["direction"])
        plain = _fold(raw_row["type"]) in {"buy", "sell", "exchange", "transfer"}
        if label and hit is None and not plain:
            unknown.append(label)
        for row in group:
            if hit is None:
                continue
            new_type = hit["type"] or str(row["type"])
            changes.append(
                {"row_id": str(row["id"]), "type": new_type, "tag": hit["tag"], "why": _WHY}
            )
    if changes:
        desk.reclassify_many(changes, who)
    _store_unknown(desk.root, str(entry.get("id") or ""), unknown)
    if unknown:
        append_event(
            desk.root,
            "koinly-review",
            who,
            "Koinly labels were left for review.",
            {"labels": ", ".join(sorted(set(unknown)))},
        )
    return unknown


def unknown_labels(root: Path) -> List[str]:
    path = root / "koinly-review.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    found: List[str] = []
    for labels in loaded.get("imports", {}).values():
        found.extend(str(label) for label in labels)
    return found


def _lookup(
    table: Sequence[Mapping[str, str]], label: str, koinly_type: str, direction: str
) -> Optional[Mapping[str, str]]:
    folded = _fold(label)
    kind = _fold(koinly_type)
    for item in table:
        names = set(item["names_folded"].split("|"))
        if item["direction"] and item["direction"] != direction:
            continue
        if folded and folded in names:
            return item
    for item in table:
        names = set(item["names_folded"].split("|"))
        if item["direction"] and item["direction"] != direction:
            continue
        if not folded and kind in names:
            return item
    return None


def _source_rows(data: bytes) -> List[Dict[str, str]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        if not any((value or "").strip() for value in raw.values()):
            continue
        label = (raw.get("Label") or raw.get("Tag") or "").strip()
        kind = (raw.get("Type") or "").strip()
        sent = (raw.get("Sent Amount") or "").strip()
        received = (raw.get("Received Amount") or "").strip()
        if sent and not received:
            direction = "out"
        elif received and not sent:
            direction = "in"
        else:
            direction = "both"
        rows.append({"label": label, "type": kind, "direction": direction})
    return rows


def _store_unknown(root: Path, import_id: str, labels: List[str]) -> None:
    path = root / "koinly-review.json"
    loaded: Dict[str, Any] = {"imports": {}}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
    loaded.setdefault("imports", {})[import_id] = sorted(set(labels))
    path.write_text(json.dumps(loaded, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fold(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).casefold()
