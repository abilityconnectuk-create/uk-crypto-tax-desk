# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Files the transaction workspace keeps in the data directory.

Writes use binary mode so Windows does not insert an extra carriage return.
Nothing here is sent to Koinly.
"""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import DeskError

_VALUE_INDEX = {"buy_value": 3, "sell_value": 6, "fee_value": 9}
_ASSET_INDEX = (2, 5, 8)


def bundle(root: Path) -> Dict[str, Any]:
    """Everything overlay needs, read once per ledger build."""
    return {
        "meta": load_meta(root),
        "overrides": latest_overrides(root),
        "renames": load_renames(root),
        "spam": {item["asset"].casefold() for item in load_spam_tokens(root)},
        "prices": price_index(root),
    }


def apply_bundle(
    packed: Dict[str, Any],
    cells: List[str],
    source_names: Sequence[str],
    facts: Mapping[str, str],
) -> List[str]:
    """Overlay one row. ``packed`` is the bundle from :func:`bundle`."""
    row_id = str(facts.get("row_id") or "")
    info = dict(packed["meta"].get(row_id) or {})
    revised = list(cells)
    used_override = _apply_overrides(revised, packed["overrides"], row_id)
    custom = _apply_prices(revised, packed["prices"])
    _rename(revised, source_names, packed["renames"])
    facts_out = {
        "tx_hash": info.get("tx_hash") or facts.get("tx_hash") or "",
        "sender": info.get("sender") or facts.get("sender") or "",
        "recipient": info.get("recipient") or facts.get("recipient") or "",
        "description": info.get("description") or facts.get("description") or "",
        "contract": info.get("contract") or "",
        "method": info.get("method") or "",
        "gain": facts.get("gain") or "",
        "chain": info.get("chain") or "",
    }
    packed.setdefault("rows", {})[row_id] = {
        "info": info,
        "facts": facts_out,
        "override": used_override,
        "custom": custom,
        "spam": _listed(revised, packed["spam"]),
    }
    return revised


def row_extra(packed: Dict[str, Any], row_id: str, tag_from_log: str) -> Dict[str, Any]:
    """Fields the list adds on top of the calculator cells."""
    stored = packed.get("rows", {}).get(row_id) or {}
    info = stored.get("info") or {}
    facts = stored.get("facts") or {}
    tag = str(info["tag"]) if "tag" in info else tag_from_log
    if stored.get("override"):
        source = "override"
    elif stored.get("custom"):
        source = "custom price"
    else:
        source = ""
    return {
        "tag": tag,
        "reviewed": info.get("reviewed") == "yes",
        "deleted": info.get("deleted") == "yes",
        "manual": info.get("manual") == "yes",
        "spam_listed": bool(stored.get("spam")),
        "tx_hash": facts.get("tx_hash") or "",
        "sender": facts.get("sender") or "",
        "recipient": facts.get("recipient") or "",
        "description": facts.get("description") or "",
        "contract": facts.get("contract") or "",
        "method": facts.get("method") or "",
        "gain": facts.get("gain") or "",
        "chain": facts.get("chain") or "",
        "value_source": info.get("value_source") or source,
        "airdrop_choice": info.get("airdrop_choice") or "",
        "spouse": info.get("spouse") or "",
        "pool_protocol": info.get("pool_protocol") or "",
        "pool_same_token": info.get("pool_same_token") or "",
        "pool_receipt": info.get("pool_receipt") or "",
        "pool_restricted": info.get("pool_restricted") or "",
        "linked_fee": info.get("linked_fee") or "",
        "manual_kind": info.get("manual_kind") or "",
        "aggregate_parts": info.get("aggregate_parts") or "",
    }


def load_meta(root: Path) -> Dict[str, Dict[str, str]]:
    loaded = _read_json(root / "tx-meta.json", {"rows": {}})
    rows = loaded.get("rows") if isinstance(loaded, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): dict(value) for key, value in rows.items() if isinstance(value, dict)}


def update_meta(root: Path, row_id: str, fields: Mapping[str, str]) -> None:
    current = load_meta(root)
    row = dict(current.get(row_id) or {})
    for key, value in fields.items():
        if value == "" and key != "tag":
            row.pop(key, None)
        else:
            row[key] = value
    if "tag" in fields:
        row["tag"] = fields["tag"]
    current[row_id] = {str(key): str(value) for key, value in row.items()}
    _write_json(root / "tx-meta.json", {"rows": current})


def load_renames(root: Path) -> List[Dict[str, str]]:
    return _rows(root / "asset-renames.json", "renames")


def add_rename(root: Path, source: str, asset: str, rename_to: str) -> Dict[str, str]:
    cleaned = {
        "id": _new_id(),
        "source": _token(source, "Enter the source."),
        "asset": _symbol(asset),
        "rename_to": _symbol(rename_to),
    }
    rows = load_renames(root)
    rows.append(cleaned)
    _write_json(root / "asset-renames.json", {"renames": rows})
    return cleaned


def delete_rename(root: Path, rule_id: str) -> None:
    _delete_row(root / "asset-renames.json", "renames", rule_id)


def load_spam_tokens(root: Path) -> List[Dict[str, str]]:
    return _rows(root / "spam-tokens.json", "tokens")


def add_spam_token(root: Path, asset: str) -> Dict[str, str]:
    symbol = _symbol(asset)
    rows = load_spam_tokens(root)
    if any(item["asset"].casefold() == symbol.casefold() for item in rows):
        raise DeskError("That token is already on the spam list.")
    row = {"id": _new_id(), "asset": symbol}
    rows.append(row)
    _write_json(root / "spam-tokens.json", {"tokens": rows})
    return row


def delete_spam_token(root: Path, token_id: str) -> None:
    _delete_row(root / "spam-tokens.json", "tokens", token_id)


def load_prices(root: Path) -> List[Dict[str, str]]:
    return _rows(root / "custom-prices.json", "prices")


def price_index(root: Path) -> Dict[Tuple[str, str], Decimal]:
    found: Dict[Tuple[str, str], Decimal] = {}
    for item in load_prices(root):
        found[(item["date"], item["asset"].casefold())] = Decimal(item["gbp"])
    return found


def import_prices(root: Path, data: bytes) -> int:
    """Replace nothing. New rows are added. A repeated date and asset updates the price."""
    parsed = _price_csv(data)
    rows = load_prices(root)
    index = {(item["date"], item["asset"].casefold()): item for item in rows}
    for item in parsed:
        key = (item["date"], item["asset"].casefold())
        if key in index:
            index[key]["gbp"] = item["gbp"]
        else:
            rows.append(item)
            index[key] = item
    _write_json(root / "custom-prices.json", {"prices": rows})
    return len(parsed)


def delete_price(root: Path, price_id: str) -> None:
    _delete_row(root / "custom-prices.json", "prices", price_id)


def load_dust(root: Path) -> Decimal:
    loaded = _read_json(root / "dust-threshold.json", {})
    try:
        return Decimal(str(loaded.get("gbp") or "0"))
    except InvalidOperation:
        return Decimal(0)


def save_dust(root: Path, raw: str) -> Decimal:
    try:
        amount = Decimal(str(raw or "0").replace(",", "").replace("£", ""))
    except InvalidOperation as exc:
        raise DeskError("Enter the dust threshold in pounds.") from exc
    if amount < 0:
        raise DeskError("The dust threshold cannot be negative.")
    _write_json(root / "dust-threshold.json", {"gbp": format(amount, "f")})
    return amount


def append_audit(root: Path, entry: Mapping[str, str]) -> None:
    line = json.dumps(dict(entry), ensure_ascii=True, sort_keys=True) + "\n"
    _append(root / "value-overrides.jsonl", line.encode("utf-8"))


def audit_log(root: Path) -> List[Dict[str, str]]:
    path = root / "value-overrides.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_bytes().splitlines():
        if line.strip():
            rows.append(json.loads(line.decode("utf-8")))
    return rows


def latest_overrides(root: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    found: Dict[Tuple[str, str], Dict[str, str]] = {}
    for entry in audit_log(root):
        if entry.get("field") in _VALUE_INDEX:
            found[(str(entry.get("row_id") or ""), str(entry["field"]))] = entry
    return found


def load_preview(root: Path) -> Dict[str, Any]:
    return _read_json(root / "tx-import-preview.json", {})


def save_preview(root: Path, payload: Mapping[str, Any]) -> None:
    _write_json(root / "tx-import-preview.json", payload)


def clear_preview(root: Path) -> None:
    path = root / "tx-import-preview.json"
    if path.is_file():
        path.unlink()


def koinly_facts(data: bytes) -> List[Dict[str, str]]:
    """Hash, wallets and description from a Koinly CSV. Other files return nothing."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    reader = csv.DictReader(io.StringIO(text))
    names = reader.fieldnames or []
    if "TxHash" not in names:
        return []
    found = []
    for raw in reader:
        if not any(str(value or "").strip() for value in raw.values()):
            continue
        gain = ""
        for key, value in raw.items():
            if key and key.startswith("Gain"):
                gain = str(value or "").strip()
        found.append(
            {
                "tx_hash": str(raw.get("TxHash") or "").strip(),
                "sender": str(raw.get("TxSrc") or "").strip(),
                "recipient": str(raw.get("TxDest") or "").strip(),
                "description": str(raw.get("Description") or "").strip(),
                "gain": gain,
            }
        )
    return found


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_overrides(
    cells: List[str], overrides: Mapping[Tuple[str, str], Mapping[str, str]], row_id: str
) -> bool:
    used = False
    for field, index in _VALUE_INDEX.items():
        latest = overrides.get((row_id, field))
        if latest is None:
            continue
        new = str(latest.get("new") or "")
        if new == "":
            continue
        cells[index] = new
        used = True
    return used


def _apply_prices(cells: List[str], prices: Mapping[Tuple[str, str], Decimal]) -> bool:
    if len(cells) < 12 or not prices:
        return False
    day = cells[11][:10]
    used = False
    for qty_index, asset_index, value_index in ((1, 2, 3), (4, 5, 6), (7, 8, 9)):
        if cells[value_index] or not cells[asset_index]:
            continue
        price = prices.get((day, cells[asset_index].casefold()))
        if price is None:
            continue
        cells[value_index] = _plain(_money(cells[qty_index]) * price)
        used = True
    return used


def _rename(
    cells: List[str], source_names: Sequence[str], rules: Sequence[Mapping[str, str]]
) -> None:
    if not rules:
        return
    folded = {name.casefold() for name in source_names if name}
    for index in _ASSET_INDEX:
        asset = cells[index]
        if not asset:
            continue
        for rule in rules:
            if rule["source"].casefold() not in folded:
                continue
            if rule["asset"].casefold() == asset.casefold():
                cells[index] = rule["rename_to"]
                break


def _listed(cells: Sequence[str], spam: Sequence[str]) -> bool:
    names = {item.casefold() for item in spam}
    return any(cells[index].casefold() in names for index in _ASSET_INDEX if cells[index])


def _price_csv(data: bytes) -> List[Dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DeskError("The price file must be UTF-8 text.") from exc
    reader = csv.DictReader(io.StringIO(text))
    names = {name.strip().casefold() for name in (reader.fieldnames or []) if name}
    if not {"date", "asset", "gbp"} <= names:
        raise DeskError("The price file needs the columns date, asset and gbp.")
    rows = []
    for raw in reader:
        folded = {
            (key or "").strip().casefold(): str(value or "").strip() for key, value in raw.items()
        }
        if not any(folded.values()):
            continue
        day = folded.get("date") or ""
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError as exc:
            raise DeskError("Each price date must be YYYY-MM-DD.") from exc
        try:
            price = Decimal(folded.get("gbp") or "")
        except InvalidOperation as exc:
            raise DeskError("Each GBP price must be a number.") from exc
        if price < 0:
            raise DeskError("A GBP price cannot be negative.")
        rows.append(
            {
                "id": _new_id(),
                "date": day,
                "asset": _symbol(folded.get("asset") or ""),
                "gbp": _plain(price),
            }
        )
    if not rows:
        raise DeskError("The price file has no rows.")
    return rows


def _rows(path: Path, key: str) -> List[Dict[str, str]]:
    loaded = _read_json(path, {key: []})
    rows = loaded.get(key) if isinstance(loaded, dict) else []
    if not isinstance(rows, list):
        return []
    return [dict(item) for item in rows if isinstance(item, dict)]


def _delete_row(path: Path, key: str, row_id: str) -> None:
    rows = [item for item in _rows(path, key) if item.get("id") != row_id]
    _write_json(path, {key: rows})


def _read_json(path: Path, default: Mapping[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        loaded = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(default)
    if not isinstance(loaded, dict):
        return dict(default)
    return loaded


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _write(path, text.encode("utf-8"))


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _append(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _token(value: str, message: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 80:
        raise DeskError(message)
    return cleaned


def _symbol(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 40 or any(char.isspace() for char in cleaned):
        raise DeskError("Enter an asset symbol, up to 40 characters.")
    return cleaned


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _plain(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
