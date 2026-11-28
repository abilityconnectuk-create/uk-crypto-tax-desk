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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
from .events import append_event
from .health import analyse
from .import_inspect import (
    cross_file_duplicates,
    inspect_bytes,
    load_prints,
    normalise_bytes,
    record_problem,
    store_prints,
)
from .rules import defi_tags
from .tx_store import apply_bundle, bundle, koinly_facts, row_extra, update_meta

PAGE_SIZE = 50
_SPECIAL = {NEEDS_REVIEW, POSSIBLE_SPAM, IGNORED}
_VALUE_INDEX = {"buy_value": 3, "sell_value": 6, "fee_value": 9}


class Desk:  # pylint: disable=too-many-public-methods
    """One data directory: raw files, the derived ledger, and an append-only log."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw_dir = root / "raw"
        self._lock = threading.Lock()
        self._row_cache: Optional[Tuple[Tuple[int, ...], List[Dict[str, Any]]]] = None
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

    def import_known(self, filename: str, data: bytes, who: str = "local") -> Dict[str, Any]:
        """Import ``data``, or return the stored entry when those bytes are already here."""
        if not data:
            raise DeskError("The file is empty.")
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            for item in self.imports():
                if item["sha256"] == digest:
                    found = dict(item)
                    found["idempotent"] = True
                    append_event(
                        self.root,
                        "import-skip",
                        who,
                        "Same file already imported. No rows were added.",
                        {"filename": _safe_name(filename), "import_id": str(item["id"])},
                    )
                    return found
        entry = self.import_bytes(filename, data, who)
        entry = dict(entry)
        entry["idempotent"] = False
        return entry

    def import_bytes(self, filename: str, data: bytes, who: str = "local") -> Dict[str, Any]:
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
            raw_path.write_bytes(data)
            if hashlib.sha256(raw_path.read_bytes()).hexdigest() != digest:
                raw_path.unlink()
                folder.rmdir()
                raise DeskError("The stored file does not match the upload.")
            try:
                converted, report = _convert_upload(raw_path, data, self.wallets_path)
            except DeskError as exc:
                _drop_raw(raw_path, folder)
                problem = record_problem(self.root, name, data, "rejected")
                append_event(
                    self.root,
                    "import-failed",
                    who,
                    "Import failed. A redacted parser problem was stored.",
                    {"filename": name, "problem_id": str(problem["id"])},
                )
                raise DeskError(
                    "The file could not be imported. Open the parser problem for the row pattern.",
                    str(problem["id"]),
                ) from exc
            report["duplicates"] = cross_file_duplicates(converted.rows, load_prints(self.root))
            entry = _manifest_entry(
                import_id,
                name,
                digest,
                data,
                converted,
                raw_path.relative_to(self.root).as_posix(),
            )
            entry["report"] = _public_report(report)
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
            store_prints(self.root, converted.rows, import_id, name)
            append_event(
                self.root,
                "import",
                who,
                f"Imported {name}.",
                {
                    "import_id": import_id,
                    "rows": str(len(converted.rows)),
                    "skipped": str(len(report.get("skipped_reasons") or [])),
                    "duplicates": str(len(report.get("duplicates") or [])),
                },
            )
            self._write_effective_unlocked()
        from .koinly_labels import apply_koinly_labels  # pylint: disable=import-outside-toplevel
        from .matching import apply_auto_matches  # pylint: disable=import-outside-toplevel
        from .user_rules import apply_enabled_rules  # pylint: disable=import-outside-toplevel

        apply_koinly_labels(self, entry, who)
        apply_enabled_rules(self, who)
        apply_auto_matches(self, who)
        return entry

    def imports(self) -> List[Dict[str, Any]]:
        return list(_read_json(self.root / "imports.json", {"imports": []})["imports"])

    def all_rows(self) -> List[Dict[str, Any]]:
        return self._effective_rows()

    def refresh_records(self) -> None:
        """Rewrite the derived CSV after a soft delete, restore, or overlay change."""
        with self._lock:
            self._row_cache = None
            self._write_effective_unlocked()

    def transactions(
        self,
        filters: Optional[Mapping[str, str]] = None,
        page: int = 1,
        per_page: Optional[int] = None,
        sort: str = "",
    ) -> Dict[str, Any]:
        chosen = _filters(filters)
        rows = self._effective_rows()
        matched = [row for row in rows if _matches(row, chosen)]
        ordered = _sort_rows(matched, sort)
        size = _page_size(per_page)
        page = max(page, 1)
        start = (page - 1) * size
        return {
            "rows": ordered[start : start + size],
            "total": len(ordered),
            "page": page,
            "pages": max(1, (len(ordered) + size - 1) // size),
            "per_page": size,
            "sort": sort,
            "assets": _unique(rows, "assets"),
            "wallets": _unique(rows, "wallets"),
            "types": _unique(rows, "types"),
            "tags": _unique_tags(rows),
        }

    def transaction(self, row_id: str) -> Dict[str, Any]:
        for row in self._effective_rows():
            if row["id"] == row_id:
                row["log"] = [item for item in self.change_log() if item["row_id"] == row_id]
                row["allowed_types"] = allowed_types()
                catalog = defi_tags()
                row["defi_tags"] = catalog
                chosen = str(row.get("tag") or "")
                row["tag_note"] = next(
                    (item["note"] for item in catalog if item["id"] == chosen), ""
                )
                return row
        raise DeskError("That transaction is not in the ledger.")

    def reclassify(
        self,
        row_id: str,
        new_type: str,
        why: str,
        actor: str,
        tag: str = "",
        allow_same: bool = False,
    ) -> Dict[str, str]:
        cleaned = why.strip()
        if not cleaned:
            raise DeskError("Say why this type should change.")
        if new_type not in allowed_types():
            raise DeskError("Choose a BittyTax transaction type.")
        if len(cleaned) > 500:
            raise DeskError("The reason is too long.")
        chosen = tag.strip()
        known = {item["id"] for item in defi_tags()}
        if chosen and chosen not in known:
            raise DeskError("Choose a DeFi tag from the rule file.")
        with self._lock:
            current = self.transaction(row_id)
            before = str(current["type"])
            same_tag = chosen == str(current.get("tag", ""))
            if before == new_type and same_tag:
                if allow_same:
                    return {
                        "row_id": row_id,
                        "at": _stamp(),
                        "who": actor or "local",
                        "why": cleaned,
                        "before_type": before,
                        "after_type": new_type,
                        "tag": chosen,
                    }
                raise DeskError("That row already has this type.")
            if before == new_type and not chosen and not allow_same:
                raise DeskError("That row already has this type.")
            entry = {
                "row_id": row_id,
                "at": _stamp(),
                "who": actor or "local",
                "why": cleaned,
                "before_type": before,
                "after_type": new_type,
                "tag": chosen,
            }
            with (self.root / "reclassifications.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_effective_unlocked()
        return entry

    def reclassify_many(self, changes: Sequence[Mapping[str, str]], actor: str) -> int:
        """Append many type changes, then write the ledger once."""
        if not changes:
            return 0
        allowed = set(allowed_types()) | {POSSIBLE_SPAM, IGNORED}
        known = {item["id"] for item in defi_tags()}
        with self._lock:
            current = {row["id"]: row for row in self._effective_rows()}
            lines = []
            for change in changes:
                row_id = str(change.get("row_id") or "")
                row = current.get(row_id)
                if row is None:
                    raise DeskError("That transaction is not in the ledger.")
                new_type = str(change.get("type") or "")
                if new_type not in allowed:
                    raise DeskError("Choose a BittyTax transaction type.")
                tag = str(change.get("tag") or "").strip()
                if tag and tag not in known:
                    raise DeskError("Choose a DeFi tag from the rule file.")
                why = str(change.get("why") or "").strip()
                if not why:
                    raise DeskError("Say why this type should change.")
                if str(row["type"]) == new_type and tag == str(row.get("tag") or ""):
                    continue
                lines.append(
                    {
                        "row_id": row_id,
                        "at": _stamp(),
                        "who": actor or "local",
                        "why": why[:500],
                        "before_type": str(row["type"]),
                        "after_type": new_type,
                        "tag": tag,
                    }
                )
            if not lines:
                return 0
            with (self.root / "reclassifications.jsonl").open("a", encoding="utf-8") as handle:
                for entry in lines:
                    handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_effective_unlocked()
        return len(lines)

    def append_manual(self, groups: Sequence[Sequence[str]], who: str = "local") -> List[str]:
        """Append rows typed in on the desk. The raw upload directory is not used."""
        if not groups:
            raise DeskError("Enter a transaction.")
        import_id = f"manual-{_new_id()}"
        with self._lock:
            ledger = _read_json(self.root / "ledger.json", {"rows": []})
            start = len(ledger["rows"])
            ids = []
            for offset, cells in enumerate(groups):
                if len(cells) != 13:
                    raise DeskError("A manual row is incomplete.")
                row_id = f"{import_id}:{start + offset:04d}"
                ledger["rows"].append(
                    {"id": row_id, "import_id": import_id, "cells": [str(cell) for cell in cells]}
                )
                ids.append(row_id)
            manifest = _read_json(self.root / "imports.json", {"imports": []})
            dates = sorted(str(cells[11])[:10] for cells in groups if len(cells) > 11 and cells[11])
            manifest["imports"].append(
                {
                    "id": import_id,
                    "filename": "Manual entry",
                    "sha256": "",
                    "bytes": 0,
                    "row_count": len(groups),
                    "converted_rows": len(groups),
                    "unmapped_rows": 0,
                    "format": "Manual",
                    "date_min": dates[0] if dates else "",
                    "date_max": dates[-1] if dates else "",
                    "imported_at": _stamp(),
                    "raw_path": "",
                }
            )
            _write_json(self.root / "ledger.json", ledger)
            _write_json(self.root / "imports.json", manifest)
            self._row_cache = None
            self._write_effective_unlocked()
        for row_id in ids:
            update_meta(self.root, row_id, {"manual": "yes"})
        append_event(
            self.root,
            "manual-add",
            who,
            "Added a manual transaction.",
            {"import_id": import_id, "rows": str(len(ids))},
        )
        return ids

    def patch_note(self, row_id: str, note: str) -> None:
        """Replace the note on the derived ledger. The uploaded file is not edited."""
        with self._lock:
            ledger = _read_json(self.root / "ledger.json", {"rows": []})
            found = False
            for stored in ledger["rows"]:
                if stored["id"] != row_id:
                    continue
                cells = list(stored["cells"])
                cells[12] = note
                stored["cells"] = cells
                found = True
            if not found:
                raise DeskError("That transaction is not in the ledger.")
            _write_json(self.root / "ledger.json", ledger)
            self._row_cache = None
            self._write_effective_unlocked()

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
            source = self._tax_source_unlocked()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = self.root / "report-cache" / f"{tax_year}-{digest}.json"
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8"))
        report = run_tax_year(str(source), tax_year)
        cache.parent.mkdir(parents=True, exist_ok=True)
        _write_json(cache, report)
        return report

    def _tax_source_unlocked(self) -> Path:
        """Calculator file. A confirmed DeFi tag can change a type. An unconfirmed tag cannot."""
        from .defi_apply import any_rewritten, tax_csv  # pylint: disable=import-outside-toplevel

        rows = self._effective_rows()
        if not any_rewritten(rows):
            return self.records_path
        path = self.root / "tax-records.csv"
        path.write_text(tax_csv(rows, bittytax_header()), encoding="utf-8")
        return path

    def report_csv(self, tax_year: int) -> str:
        return report_csv(self.report(tax_year))

    def health(self, tax_year: int) -> Dict[str, Any]:
        """Ledger problems for one tax year. A calculator failure does not hide the others."""
        rows = self._effective_rows()
        missing: List[Dict[str, str]] = []
        note = ""
        if rows:
            try:
                missing = list(self.report(tax_year).get("missing_cost", []))
            except DeskError as exc:
                note = str(exc)
        packed = analyse(rows, self.review_lists(), missing)
        packed["calculator_note"] = note
        packed["tax_year"] = tax_year
        return packed

    def health_count(self, tax_year: int) -> int:
        cached = self._read_health_cache(tax_year)
        if cached is not None:
            return cached
        total = int(self.health(tax_year)["total"])
        cache = self._health_cache_path(tax_year)
        cache.parent.mkdir(parents=True, exist_ok=True)
        _write_json(cache, {"total": total})
        return total

    def _health_cache_path(self, tax_year: int) -> Path:
        digest = _file_digest(self.records_path)
        return self.root / "health-cache" / f"{tax_year}-{digest}.json"

    def _read_health_cache(self, tax_year: int) -> Optional[int]:
        cache = self._health_cache_path(tax_year)
        if not cache.is_file():
            return None
        loaded = json.loads(cache.read_text(encoding="utf-8"))
        return int(loaded.get("total", 0))

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

    def price_fills(self) -> List[Dict[str, str]]:
        return _read_jsonl(self.root / "price-fills.jsonl")

    def apply_price_fills(self, entries: Sequence[Mapping[str, str]]) -> None:
        """Append GBP values. The uploaded file is not edited."""
        if not entries:
            return
        with self._lock:
            with (self.root / "price-fills.jsonl").open("a", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(dict(entry), ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._write_effective_unlocked()

    def _effective_rows(self) -> List[Dict[str, Any]]:
        stamp = _source_stamp(self.root)
        if self._row_cache is not None and self._row_cache[0] == stamp:
            return list(self._row_cache[1])
        rows = self._build_rows()
        self._row_cache = (stamp, rows)
        return list(rows)

    def _build_rows(self) -> List[Dict[str, Any]]:
        changes = _latest_changes(self.change_log())
        fills = _latest_fills(self.price_fills())
        overlay = bundle(self.root)
        facts = self._import_facts()
        sources = self._import_sources()
        rows = []
        for stored in self._ledger_rows():
            cells = list(stored["cells"])
            change = changes.get(stored["id"])
            changed = change is not None
            tag = ""
            if change is not None:
                cells[0] = change["after_type"]
                tag = str(change.get("tag") or "")
            for field, entry in fills.get(stored["id"], {}).items():
                index = _VALUE_INDEX.get(field)
                if index is not None:
                    cells[index] = entry["value"]
            fact = dict(facts.get(stored["id"]) or {})
            fact["row_id"] = stored["id"]
            names = list(sources.get(stored["import_id"], {}).get("names") or [])
            if len(cells) > 10 and cells[10]:
                names.append(str(cells[10]))
            cells = apply_bundle(overlay, cells, names, fact)
            packed = _present(stored["id"], stored["import_id"], cells, changed)
            packed.update(row_extra(overlay, stored["id"], tag))
            origin = sources.get(stored["import_id"], {})
            packed["import_source"] = str(origin.get("filename") or "")
            packed["import_type"] = str(origin.get("format") or "")
            packed["import_date"] = str(origin.get("imported_at") or "")[:10]
            if not packed["description"]:
                packed["description"] = packed["note"]
            if not packed["value_source"] and any(
                packed[key] for key in ("buy_value", "sell_value", "fee_value")
            ):
                packed["value_source"] = "market"
            rows.append(packed)
        return rows

    def _import_facts(self) -> Dict[str, Dict[str, str]]:
        found: Dict[str, Dict[str, str]] = {}
        manifest = {item["id"]: item for item in self.imports()}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for stored in self._ledger_rows():
            grouped.setdefault(str(stored["import_id"]), []).append(stored)
        for import_id, stored_rows in grouped.items():
            entry = manifest.get(import_id) or {}
            raw = self.root / str(entry.get("raw_path") or "")
            if not raw.is_file():
                continue
            parsed = koinly_facts(raw.read_bytes())
            if len(parsed) != len(stored_rows):
                continue
            for stored, fact in zip(stored_rows, parsed):
                found[str(stored["id"])] = fact
        return found

    def _import_sources(self) -> Dict[str, Dict[str, Any]]:
        found = {}
        for item in self.imports():
            names = [str(item.get("format") or ""), str(item.get("filename") or "")]
            found[str(item["id"])] = {
                "names": [name for name in names if name],
                "filename": str(item.get("filename") or ""),
                "format": str(item.get("format") or ""),
                "imported_at": str(item.get("imported_at") or ""),
            }
        return found

    def _ledger_rows(self) -> List[Dict[str, Any]]:
        return list(_read_json(self.root / "ledger.json", {"rows": []})["rows"])

    def _write_effective_unlocked(self) -> None:
        header = bittytax_header()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in self._effective_rows():
            if row.get("deleted"):
                continue
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
        "fee_quantity": cells[7],
        "fee_asset": cells[8],
        "fee_value": cells[9],
        "wallet": cells[10],
        "timestamp": cells[11],
        "date": cells[11][:10],
        "note": cells[12],
        "assets": assets,
        "status": _status(kind, changed),
        "changed": changed,
    }


def _status(kind: str, changed: bool) -> str:
    if kind == POSSIBLE_SPAM:
        return "spam"
    if kind in {NEEDS_REVIEW, IGNORED}:
        return "needs-review"
    if changed:
        return "reclassified"
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
        "tag": "",
        "extra": "",
    }
    if filters:
        for key in chosen:
            chosen[key] = str(filters.get(key, "") or "")
    return chosen


def _matches(row: Mapping[str, Any], chosen: Mapping[str, str]) -> bool:
    haystack = " ".join(str(cell) for cell in row["cells"]).casefold()
    query = chosen["query"].casefold()
    tag = chosen["tag"].casefold()
    note = str(row["note"]).casefold()
    extra = chosen["extra"].casefold()
    checks = (
        not chosen["date_from"] or str(row["date"]) >= chosen["date_from"],
        not chosen["date_to"] or str(row["date"]) <= chosen["date_to"],
        not chosen["asset"] or chosen["asset"] in row["assets"],
        not chosen["wallet"] or row["wallet"] == chosen["wallet"],
        not chosen["kind"] or row["type"] == chosen["kind"],
        _status_ok(str(row["status"]), chosen["status"]),
        not query or query in haystack or query in str(row["id"]).casefold(),
        not tag or tag in note or tag == str(row["status"]).casefold(),
        not extra or extra in haystack,
    )
    return all(checks)


def _status_ok(actual: str, wanted: str) -> bool:
    if not wanted:
        return True
    if wanted == "warnings":
        return actual in {"needs-review", "spam", "unmapped"}
    return actual == wanted


def _page_size(per_page: Optional[int]) -> int:
    if per_page in (25, 50, 100):
        return int(per_page)
    return PAGE_SIZE


def _sort_rows(rows: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    if sort == "value":
        return sorted(rows, key=_row_value, reverse=True)
    if sort == "type":
        return sorted(rows, key=lambda row: (str(row["type"]), str(row["timestamp"])))
    if sort == "asset":
        return sorted(rows, key=lambda row: (_first_asset(row), str(row["timestamp"])))
    if sort == "date":
        return sorted(rows, key=lambda row: str(row["timestamp"]), reverse=True)
    if sort == "date_asc":
        return sorted(rows, key=lambda row: str(row["timestamp"]))
    return list(rows)


def _first_asset(row: Mapping[str, Any]) -> str:
    assets = row.get("assets") or []
    return str(assets[0]) if assets else ""


def _row_value(row: Mapping[str, Any]) -> Decimal:
    for key in ("sell_value", "buy_value", "fee_value"):
        amount = _money(row.get(key))
        if amount != 0:
            return amount
    return Decimal(0)


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _unique_tags(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    found = set()
    for row in rows:
        status = str(row.get("status") or "")
        if status and status != "clear":
            found.add(status)
        note = str(row.get("note") or "").strip()
        if note and len(note) <= 24 and " " not in note:
            found.add(note)
    return sorted(found)


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


def _latest_fills(log: Sequence[Mapping[str, str]]) -> Dict[str, Dict[str, Mapping[str, str]]]:
    latest: Dict[str, Dict[str, Mapping[str, str]]] = {}
    for entry in log:
        latest.setdefault(entry["row_id"], {})[entry["field"]] = entry
    return latest


def _read_jsonl(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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


def _convert_upload(
    raw_path: Path, data: bytes, wallets: Optional[Path]
) -> Tuple[Conversion, Dict[str, Any]]:
    report = inspect_bytes(raw_path.name, data)
    try:
        converted = convert_file(str(raw_path), _optional_path(wallets))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        cleaned = normalise_bytes(data)
        if cleaned is None:
            raise DeskError("The converter rejected this file.") from exc
        notes = list(report.get("normalised") or [])
        notes.append("rewritten as comma-separated text for the parser")
        report["normalised"] = notes
        try:
            converted = _convert_cleaned(raw_path, cleaned, wallets)
        except Exception as cleaned_exc:  # pylint: disable=broad-exception-caught
            raise DeskError("The converter rejected this file.") from cleaned_exc
    return converted, report


def _convert_cleaned(raw_path: Path, cleaned: bytes, wallets: Optional[Path]) -> Conversion:
    temp = raw_path.with_name("_retry.csv")
    try:
        temp.write_bytes(cleaned)
        return convert_file(str(temp), _optional_path(wallets))
    finally:
        if temp.exists():
            temp.unlink()


def _drop_raw(raw_path: Path, folder: Path) -> None:
    if raw_path.exists():
        raw_path.unlink()
    if folder.exists():
        folder.rmdir()


def _public_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "parsed_rows",
        "blank_rows",
        "skipped_reasons",
        "unknown_columns",
        "date_problems",
        "number_problems",
        "duplicates",
        "normalised",
        "parser",
        "delimiter",
    )
    return {key: report.get(key) for key in keys}


def _source_stamp(root: Path) -> Tuple[int, ...]:
    names = (
        "ledger.json",
        "reclassifications.jsonl",
        "price-fills.jsonl",
        "tx-meta.json",
        "value-overrides.jsonl",
        "asset-renames.json",
        "spam-tokens.json",
        "custom-prices.json",
    )
    return tuple(_file_stamp(root / name) for name in names)


def _file_stamp(path: Path) -> int:
    if not path.is_file():
        return 0
    stat = path.stat()
    return stat.st_mtime_ns + stat.st_size


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
