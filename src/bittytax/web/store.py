# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Files for one local desk. The raw upload is never rewritten."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .convert import (
    IGNORED,
    NEEDS_REVIEW,
    POSSIBLE_SPAM,
    Conversion,
    ReviewItem,
    allowed_types,
    bittytax_header,
    convert_file,
    unmapped_rows,
)
from .engine_run import report_csv, run_tax_year
from .errors import DeskError

PAGE_SIZE = 50
_SPECIAL = {NEEDS_REVIEW, POSSIBLE_SPAM, IGNORED}


class Desk:
    """One data directory: raw files, the derived ledger, and an append-only log."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw_dir = root / "raw"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    @property
    def records_path(self) -> Path:
        return self.root / "effective-records.csv"

    @property
    def wallets_path(self) -> Optional[Path]:
        chosen = os.environ.get("UK_CRYPTO_TAX_DESK_WALLETS", "").strip()
        if chosen:
            path = Path(chosen)
            return path if path.is_file() else None
        local = self.root / "wallets.yaml"
        if local.is_file():
            return local
        return None

    def import_bytes(self, filename: str, data: bytes) -> Dict[str, Any]:
        if not data:
            raise DeskError("The file is empty.")
        name = _safe_name(filename)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            manifest = _read_json(self.root / "imports.json", {"imports": []})
            for item in manifest["imports"]:
                if item["sha256"] == digest:
                    raise DeskError(
                        "This file is already imported. The stored copy was left unchanged."
                    )
            import_id = _new_id()
            folder = self.raw_dir / import_id
            folder.mkdir(parents=True, exist_ok=False)
            raw_path = folder / name
            saved = False
            try:
                raw_path.write_bytes(data)
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != digest:
                    raise DeskError("The stored file does not match the upload.")
                converted = convert_file(str(raw_path), _optional_path(self.wallets_path))
                saved = True
            finally:
                if not saved:
                    if raw_path.exists():
                        raw_path.unlink()
                    folder.rmdir()
            entry = _manifest_entry(
                import_id,
                name,
                digest,
                data,
                converted,
                raw_path.relative_to(self.root).as_posix(),
            )
            ledger = _read_json(self.root / "ledger.json", {"rows": []})
            review = _read_json(self.root / "review.json", {"imports": {}})
            start = len(ledger["rows"])
            for offset, cells in enumerate(converted.rows):
                ledger["rows"].append(
                    {
                        "id": f"{import_id}:{start + offset:04d}",
                        "import_id": import_id,
                        "cells": cells,
                    }
                )
            review["imports"][import_id] = {
                "needs_review": [_item_dict(item) for item in converted.needs_review],
                "queue": [_item_dict(item) for item in converted.queue],
                "spam": [_item_dict(item) for item in converted.spam],
            }
            manifest["imports"].append(entry)
            _write_json(self.root / "ledger.json", ledger)
            _write_json(self.root / "review.json", review)
            _write_json(self.root / "imports.json", manifest)
            self._write_effective_unlocked()
        return entry

    def imports(self) -> List[Dict[str, Any]]:
        return list(_read_json(self.root / "imports.json", {"imports": []})["imports"])

    def transactions(
        self, filters: Optional[Mapping[str, str]] = None, page: int = 1
    ) -> Dict[str, Any]:
        chosen = _filters(filters)
        rows = self._effective_rows()
        matched = [row for row in rows if _matches(row, chosen)]
        page = max(page, 1)
        start = (page - 1) * PAGE_SIZE
        return {
            "rows": matched[start : start + PAGE_SIZE],
            "total": len(matched),
            "page": page,
            "pages": max(1, (len(matched) + PAGE_SIZE - 1) // PAGE_SIZE),
            "assets": _unique(rows, "assets"),
            "wallets": _unique(rows, "wallets"),
            "types": _unique(rows, "types"),
        }

    def transaction(self, row_id: str) -> Dict[str, Any]:
        for row in self._effective_rows():
            if row["id"] == row_id:
                row["log"] = [item for item in self.change_log() if item["row_id"] == row_id]
                row["allowed_types"] = allowed_types()
                return row
        raise DeskError("That transaction is not in the ledger.")

    def reclassify(self, row_id: str, new_type: str, why: str, actor: str) -> Dict[str, str]:
        cleaned = why.strip()
        if not cleaned:
            raise DeskError("Say why this type should change.")
        if new_type not in allowed_types():
            raise DeskError("Choose a BittyTax transaction type.")
        if len(cleaned) > 500:
            raise DeskError("The reason is too long.")
        with self._lock:
            current = self.transaction(row_id)
            before = str(current["type"])
            if before == new_type:
                raise DeskError("That row already has this type.")
            entry = {
                "row_id": row_id,
                "at": _stamp(),
                "who": actor or "local",
                "why": cleaned,
                "before_type": before,
                "after_type": new_type,
            }
            with (self.root / "reclassifications.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_effective_unlocked()
        return entry

    def change_log(self) -> List[Dict[str, str]]:
        path = self.root / "reclassifications.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def review(self, tax_year: int) -> Dict[str, Any]:
        stored = _read_json(self.root / "review.json", {"imports": {}})
        needs_review: List[Dict[str, str]] = []
        queue: List[Dict[str, str]] = []
        spam: List[Dict[str, str]] = []
        for block in stored["imports"].values():
            needs_review.extend(block.get("needs_review", []))
            queue.extend(block.get("queue", []))
            spam.extend(block.get("spam", []))
        unmapped = []
        for row in self._effective_rows():
            if row["status"] == "unmapped":
                unmapped.append(
                    {
                        "id": row["id"],
                        "timestamp": row["timestamp"],
                        "type": row["type"],
                        "asset": ", ".join(row["assets"]),
                        "wallet": row["wallet"],
                        "note": row["note"],
                    }
                )
        report = self.report(tax_year) if self._ledger_rows() or self.imports() else None
        return {
            "needs_review": needs_review,
            "queue": queue,
            "spam": spam,
            "unmapped": unmapped,
            "missing_cost": [] if report is None else report["missing_cost"],
            "integrity_ok": True if report is None else report["integrity_ok"],
            "tax_year": tax_year,
            "label": "" if report is None else report["label"],
        }

    def report(self, tax_year: int) -> Dict[str, Any]:
        with self._lock:
            self._write_effective_unlocked()
            digest = hashlib.sha256(self.records_path.read_bytes()).hexdigest()
        cache = self.root / "report-cache" / f"{tax_year}-{digest}.json"
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8"))
        report = run_tax_year(str(self.records_path), tax_year)
        cache.parent.mkdir(parents=True, exist_ok=True)
        _write_json(cache, report)
        return report

    def report_csv(self, tax_year: int) -> str:
        return report_csv(self.report(tax_year))

    def counts(self) -> Dict[str, int]:
        review = self.review_lists()
        rows = self._effective_rows()
        return {
            "imports": len(self.imports()),
            "transactions": len(rows),
            "needs_review": len(review["needs_review"]),
            "queue": len(review["queue"]),
            "spam": len(review["spam"]),
            "unmapped": sum(1 for row in rows if row["status"] == "unmapped"),
        }

    def review_lists(self) -> Dict[str, List[Dict[str, str]]]:
        stored = _read_json(self.root / "review.json", {"imports": {}})
        collected: Dict[str, List[Dict[str, str]]] = {
            "needs_review": [],
            "queue": [],
            "spam": [],
        }
        for block in stored["imports"].values():
            for key, rows in collected.items():
                rows.extend(block.get(key, []))
        return collected

    def _effective_rows(self) -> List[Dict[str, Any]]:
        changes = _latest_changes(self.change_log())
        rows = []
        for stored in self._ledger_rows():
            cells = list(stored["cells"])
            change = changes.get(stored["id"])
            changed = change is not None
            if change is not None:
                cells[0] = change["after_type"]
            rows.append(_present(stored["id"], stored["import_id"], cells, changed))
        return rows

    def _ledger_rows(self) -> List[Dict[str, Any]]:
        return list(_read_json(self.root / "ledger.json", {"rows": []})["rows"])

    def _write_effective_unlocked(self) -> None:
        header = bittytax_header()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in self._effective_rows():
            writer.writerow(row["cells"])
        text = buffer.getvalue()
        current = (
            self.records_path.read_text(encoding="utf-8") if self.records_path.exists() else None
        )
        if current != text:
            self.records_path.write_text(text, encoding="utf-8")


def _present(row_id: str, import_id: str, cells: Sequence[str], changed: bool) -> Dict[str, Any]:
    kind = cells[0]
    assets = [cells[2], cells[5]]
    assets = [asset for asset in assets if asset]
    return {
        "id": row_id,
        "import_id": import_id,
        "cells": list(cells),
        "type": kind,
        "buy_quantity": cells[1],
        "buy_asset": cells[2],
        "buy_value": cells[3],
        "sell_quantity": cells[4],
        "sell_asset": cells[5],
        "sell_value": cells[6],
        "wallet": cells[10],
        "timestamp": cells[11],
        "date": cells[11][:10],
        "note": cells[12],
        "assets": assets,
        "status": _status(kind, changed),
        "changed": changed,
    }


def _status(kind: str, changed: bool) -> str:
    if changed:
        return "reclassified"
    if kind == POSSIBLE_SPAM:
        return "spam"
    if kind in {NEEDS_REVIEW, IGNORED}:
        return "needs-review"
    if kind.startswith("_"):
        return "unmapped"
    return "clear"


def _filters(filters: Optional[Mapping[str, str]]) -> Dict[str, str]:
    chosen = {
        "query": "",
        "date_from": "",
        "date_to": "",
        "asset": "",
        "wallet": "",
        "kind": "",
        "status": "",
    }
    if filters:
        for key in chosen:
            chosen[key] = str(filters.get(key, "") or "")
    return chosen


def _matches(row: Mapping[str, Any], chosen: Mapping[str, str]) -> bool:
    checks = (
        not chosen["date_from"] or str(row["date"]) >= chosen["date_from"],
        not chosen["date_to"] or str(row["date"]) <= chosen["date_to"],
        not chosen["asset"] or chosen["asset"] in row["assets"],
        not chosen["wallet"] or row["wallet"] == chosen["wallet"],
        not chosen["kind"] or row["type"] == chosen["kind"],
        not chosen["status"] or row["status"] == chosen["status"],
    )
    if not all(checks):
        return False
    query = chosen["query"].casefold()
    if not query:
        return True
    haystack = " ".join(str(cell) for cell in row["cells"]).casefold()
    return query in haystack or query in str(row["id"]).casefold()


def _unique(rows: Sequence[Mapping[str, Any]], field: str) -> List[str]:
    found = set()
    if field == "assets":
        for row in rows:
            found.update(row["assets"])
    elif field == "wallets":
        found.update(str(row["wallet"]) for row in rows if row["wallet"])
    else:
        found.update(str(row["type"]) for row in rows)
    return sorted(found)


def _latest_changes(log: Sequence[Mapping[str, str]]) -> Dict[str, Mapping[str, str]]:
    latest: Dict[str, Mapping[str, str]] = {}
    for entry in log:
        latest[entry["row_id"]] = entry
    return latest


def _manifest_entry(
    import_id: str,
    name: str,
    digest: str,
    data: bytes,
    converted: Conversion,
    raw_path: str,
) -> Dict[str, Any]:
    dates = sorted(cells[11][:10] for cells in converted.rows if len(cells) > 11 and cells[11])
    text_rows = _text_row_count(data)
    return {
        "id": import_id,
        "filename": name,
        "sha256": digest,
        "bytes": len(data),
        "row_count": text_rows if text_rows is not None else converted.source_rows,
        "converted_rows": len(converted.rows),
        "unmapped_rows": len(unmapped_rows(converted.rows)),
        "format": converted.format_name,
        "date_min": dates[0] if dates else "",
        "date_max": dates[-1] if dates else "",
        "imported_at": _stamp(),
        "raw_path": raw_path,
    }


def _text_row_count(data: bytes) -> Optional[int]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0
    return max(0, len(lines) - 1)


def _item_dict(item: ReviewItem) -> Dict[str, str]:
    return {
        "timestamp": item.timestamp,
        "asset": item.asset,
        "quantity": item.quantity,
        "wallet": item.wallet,
        "destination": item.destination,
        "tx_hash": item.tx_hash,
        "description": item.description,
        "disposition": item.disposition,
    }


def _safe_name(filename: str) -> str:
    name = Path(filename or "upload.csv").name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return "upload.csv"
    return name


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_path(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    return str(path)


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(default))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DeskError(f"{path.name} is not a desk file.")
    return loaded


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
