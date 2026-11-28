# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Wallets, read-only sync, and the review queue. Nothing is written to Koinly."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import DeskError
from .events import append_event
from .exchange_csv import stage_import
from .taxyear import tax_year_containing
from .tx_store import update_meta
from .wallet_catalogue import (
    chains,
    find_chain,
    find_integration,
    robinhood_urls,
    suggestion_for,
)
from .wallet_fetch import Fetch, check_keys, pull
from .wallet_secrets import (
    append_text,
    forget_secret,
    load_secret,
    pack,
    read_json,
    save_secret,
    scrub,
    useful,
    write_json,
)
from .when import ago

PAGE_SIZES = (10, 25, 50, 100)
NEEDS_REVIEW = "_Needs-Review"
_TOLERANCE = Decimal("0.00000001")
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BTC = re.compile(r"^[13bcBC][a-zA-Z0-9]{25,90}$")
_RECONNECT = ("read-only", "reconnect", "permission", "api key", "401", "403", "trade or withdraw")
Key = Tuple[str, str, str, str, str]


def describe_wallet(desk: Any, wallet_id: str) -> Dict[str, str]:
    """Public fields for one wallet. Secrets are not included."""
    wallet = _require(desk.root, wallet_id)
    described = _public(wallet)
    described["method"] = str(wallet.get("method") or "")
    described["group"] = str(wallet.get("group") or "")
    return described


def list_page(desk: Any, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
    """One page of wallets, newest first, plus the missed-by-Koinly queue."""
    size = per_page if per_page in PAGE_SIZES else PAGE_SIZES[0]
    current = max(page, 1)
    rows = [row for row in _wallets(desk.root) if not row.get("deleted_at")]
    rows.sort(
        key=lambda item: (str(item.get("added_at") or ""), str(item.get("id") or "")), reverse=True
    )
    start = (current - 1) * size
    ledger = list(desk.all_rows())
    visible = []
    for wallet in rows[start : start + size]:
        age = ago(str(wallet.get("last_sync_at") or ""))
        visible.append(_view(desk, wallet, ledger, age))
    missed = [item for item in review_items(desk, 0) if item.get("missed") == "yes"]
    return {
        "rows": visible,
        "total": len(rows),
        "page": current,
        "pages": max(1, (len(rows) + size - 1) // size),
        "per_page": size,
        "sizes": list(PAGE_SIZES),
        "missed": missed,
    }


def review_items(desk: Any, year: int) -> List[Dict[str, str]]:
    """Synced rows waiting to be accepted. Year 0 keeps every open row."""
    items = []
    for row in _review(desk.root):
        if row.get("accepted"):
            continue
        if year and not _in_year(str(row.get("timestamp") or ""), year):
            continue
        items.append(_review_view(row))
    return items


def balance_warnings(desk: Any, year: int) -> List[Dict[str, str]]:
    """Reported balances that do not match the accepted rows. They block Ready to file."""
    found = []
    ledger = list(desk.all_rows())
    for wallet in _wallets(desk.root):
        if wallet.get("deleted_at") or wallet.get("ignore_reported"):
            continue
        reported = wallet.get("reported") if isinstance(wallet.get("reported"), dict) else {}
        if not reported:
            continue
        for asset, got, wanted in _differences(
            _computed(ledger, str(wallet.get("name") or "")), reported
        ):
            found.append(
                {
                    "category": "balance_difference",
                    "category_label": "Balance differences",
                    "name": f"{wallet.get('name')} {asset}",
                    "detail": f"Computed {_plain(got)} {asset}, reported {_plain(wanted)} {asset}.",
                    "href": f"/wallets?tax_year={year}",
                    "blocking": "yes",
                    "kind": "open",
                }
            )
    return found


def add_wallets(
    desk: Any,
    spec: Mapping[str, Any],
    fetch: Optional[Fetch] = None,
    upload: Optional[Tuple[str, bytes]] = None,
    who: str = "local",
) -> Dict[str, Any]:
    """Create one wallet, or one wallet per selected chain."""
    integration = find_integration(str(spec.get("integration") or ""))
    selected = _selected(integration, spec.get("chains") or [])
    named = _names(str(spec.get("name") or ""), integration, selected)
    method = _method(integration, str(spec.get("method") or ""))
    family = _family(integration, selected)
    address = _address(family, str(spec.get("address") or ""), method)
    start = _start(str(spec.get("start_date") or ""))
    tag = _deposit_tag(str(spec.get("deposit_tag") or ""))
    explorer, rpc = _urls(desk.root, selected, spec)
    secret = pack(
        str(spec.get("api_key") or ""),
        str(spec.get("api_secret") or ""),
        str(spec.get("passphrase") or ""),
    )
    notice = ""
    if method == "api":
        _require_exchange_secret(integration["id"], secret)
        notice = check_keys(_exchange_settings(integration["id"], secret), fetch)
    clashes = _clashes(desk.root, [name for name, _chain in named])
    decision = str(spec.get("decision") or "")
    if clashes and decision == "":
        return {
            "ok": "choice",
            "existing": [_public(row) for row in clashes],
            "names": [name for name, _c in named],
        }
    if clashes and decision == "existing":
        return {"ok": "existing", "wallets": [_public(row) for row in clashes]}
    if clashes and decision != "create":
        raise DeskError("Choose create anyway, or use the existing wallet.")
    created = []
    stored = _wallets(desk.root)
    for name, chain in named:
        chain_explorer, chain_rpc = _chain_urls(chain, explorer, rpc)
        record = _record(
            name,
            integration,
            chain,
            address,
            method,
            start,
            tag,
            spec,
            chain_explorer,
            chain_rpc,
            notice,
        )
        if useful(secret) and method in {"api", "address"}:
            record["secret_store"] = save_secret(desk.root, record["id"], secret)
        stored.append(record)
        created.append(record)
    write_json(desk.root / "wallets.json", {"wallets": stored})
    append_event(desk.root, "wallet-add", who, "Added a wallet.", {"count": str(len(created))})
    if upload and method == "file" and created:
        import_file(desk, created[0]["id"], upload[0], upload[1], who)
    return {"ok": "created", "wallets": [_public(row) for row in created]}


def sync_wallet(
    desk: Any, wallet_id: str, fetch: Optional[Fetch] = None, who: str = "local"
) -> Dict[str, Any]:
    """Pull new rows from the last cursor and hold them for review."""
    wallet = _require(desk.root, wallet_id)
    secret = load_secret(desk.root, wallet_id)
    try:
        result = pull(_sync_settings(desk.root, wallet, secret), fetch)
    except DeskError as exc:
        _mark_error(desk.root, wallet_id, scrub(str(exc), secret))
        raise DeskError(scrub(str(exc), secret)) from exc
    added = _stage(desk, wallet, result.get("events") or [], who)
    _finish_sync(desk.root, wallet_id, result, added)
    _log(desk.root, wallet_id, len(added))
    append_event(
        desk.root,
        "wallet-sync",
        who,
        "Synced a wallet.",
        {"wallet_id": wallet_id, "rows": str(len(added))},
    )
    return {"added": len(added), "cursor": str(result.get("cursor") or "")}


def sync_all(desk: Any, fetch: Optional[Fetch] = None, who: str = "local") -> Dict[str, Any]:
    """Sync every wallet that has an API key or a public address."""
    synced = 0
    failed: List[str] = []
    for wallet in _wallets(desk.root):
        if wallet.get("deleted_at") or wallet.get("method") not in {"api", "address"}:
            continue
        try:
            sync_wallet(desk, str(wallet["id"]), fetch, who)
            synced += 1
        except DeskError as exc:
            failed.append(f"{wallet.get('name')}: {exc}")
    return {"synced": synced, "failed": failed}


def import_file(
    desk: Any, wallet_id: str, filename: str, data: bytes, who: str = "local"
) -> Dict[str, Any]:
    """Hold a Coinbase, KuCoin, or Revolut X file on the exchange review queue."""
    _require(desk.root, wallet_id)
    entry = stage_import(desk, filename, data, who)
    stored = _wallets(desk.root)
    current = _find(stored, wallet_id)
    imports = [str(item) for item in current.get("file_imports") or []]
    imports.append(str(entry["id"]))
    current["file_imports"] = imports
    current["last_sync_at"] = _stamp()
    current["last_error"] = ""
    write_json(desk.root / "wallets.json", {"wallets": stored})
    return entry


def reconnect(
    desk: Any,
    wallet_id: str,
    spec: Mapping[str, str],
    fetch: Optional[Fetch] = None,
    who: str = "local",
) -> None:
    """Replace the stored key. The previous key is not shown."""
    wallet = _require(desk.root, wallet_id)
    secret = pack(spec.get("api_key", ""), spec.get("api_secret", ""), spec.get("passphrase", ""))
    if not useful(secret):
        raise DeskError("Enter the read-only key. It is not shown again after saving.")
    notice = ""
    if wallet.get("integration") in {"coinbase", "kucoin"}:
        if not secret["api_key"] or not secret["api_secret"]:
            raise DeskError("Enter the read-only API key and secret.")
        if wallet.get("integration") == "kucoin" and not secret["passphrase"]:
            raise DeskError("Enter the KuCoin passphrase.")
        notice = check_keys(_exchange_settings(str(wallet["integration"]), secret), fetch)
    store = save_secret(desk.root, wallet_id, secret)
    stored = _wallets(desk.root)
    current = _find(stored, wallet_id)
    current["secret_store"] = store
    current["last_error"] = ""
    current["notice"] = notice
    if current.get("method") == "later" and current.get("integration") in {"coinbase", "kucoin"}:
        current["method"] = "api"
    write_json(desk.root / "wallets.json", {"wallets": stored})
    append_event(
        desk.root, "wallet-reconnect", who, "Reconnected a wallet.", {"wallet_id": wallet_id}
    )


def delete_wallet(desk: Any, wallet_id: str, confirm: str, who: str = "local") -> None:
    """Hide a wallet after confirmation, and remove its key."""
    if confirm != "yes":
        raise DeskError("Confirm the deletion before this wallet is hidden.")
    _require(desk.root, wallet_id)
    stored = _wallets(desk.root)
    current = _find(stored, wallet_id)
    current["deleted_at"] = _stamp()
    current["secret_store"] = ""
    forget_secret(desk.root, wallet_id)
    write_json(desk.root / "wallets.json", {"wallets": stored})
    append_event(desk.root, "wallet-delete", who, "Hid a wallet.", {"wallet_id": wallet_id})


def accept_row(desk: Any, row_id: str, who: str = "local") -> str:
    """Copy one staged row into the ledger. An unconfirmed tag does not change the type."""
    stored = _review(desk.root)
    row = next((item for item in stored if item.get("id") == row_id), None)
    if row is None:
        raise DeskError("That synced row is not on the review queue.")
    if row.get("accepted"):
        raise DeskError("That row is already in the tax report.")
    cells = _cells(row)
    meta = {"tx_hash": str(row.get("tx_hash") or ""), "chain": str(row.get("chain") or "")}
    if row.get("suggested_tag"):
        meta["tag"] = str(row["suggested_tag"])
    ledger_id = _append_ledger(desk, cells, meta, who)
    row["accepted"] = True
    row["ledger_id"] = ledger_id
    write_json(desk.root / "wallet-review.json", {"rows": stored})
    return ledger_id


def _view(
    desk: Any, wallet: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]], age: str
) -> Dict[str, str]:
    name = str(wallet.get("name") or "")
    computed = _computed(ledger, name)
    raw_reported = wallet.get("reported")
    reported: Mapping[str, Any] = raw_reported if isinstance(raw_reported, dict) else {}
    warning = ""
    if reported and not wallet.get("ignore_reported"):
        diffs = _differences(computed, reported)
        if diffs:
            asset, got, wanted = diffs[0]
            warning = f"Computed {_plain(got)} {asset}, reported {_plain(wanted)} {asset}."
    error = str(wallet.get("last_error") or "")
    label = _label(wallet, age, error)
    return {
        "id": str(wallet.get("id") or ""),
        "name": name,
        "value": _value_text(computed, reported),
        "count": str(_tx_count(desk, wallet, ledger)),
        "status": label,
        "error": error if error and error != label else "",
        "notice": str(wallet.get("notice") or ""),
        "synced_at": str(wallet.get("last_sync_at") or ""),
        "warning": warning,
        "chain": str(wallet.get("chain") or ""),
    }


def _label(wallet: Mapping[str, Any], age: str, error: str) -> str:
    if error and any(marker in error.casefold() for marker in _RECONNECT):
        return "Reconnect"
    if error:
        return error
    if wallet.get("method") == "api" and not wallet.get("secret_store"):
        return "Reconnect"
    if wallet.get("last_sync_at"):
        return f"Last synced {age}"
    return "Import file"


def _stage(
    desk: Any, wallet: Mapping[str, Any], events: Sequence[Mapping[str, str]], who: str
) -> List[Dict[str, Any]]:
    del who
    ledger = list(desk.all_rows())
    existing = _ledger_keys(ledger)
    koinly = _ledger_keys(_koinly_rows(ledger))
    staged = _staged_keys(desk.root)
    owned = _owned(desk.root)
    review = _review(desk.root)
    added: List[Dict[str, Any]] = []
    for event in events:
        if _too_early(event, str(wallet.get("start_date") or "")):
            continue
        keys = _event_keys(str(wallet.get("name") or ""), event)
        if any(_seen(key, existing) or _seen(key, staged) for key in keys):
            continue
        missed = not any(_seen(key, koinly) for key in keys)
        row = _staged_row(desk.root, wallet, event, owned, missed)
        review.append(row)
        staged.extend(keys)
        added.append(row)
    if added:
        write_json(desk.root / "wallet-review.json", {"rows": review})
    return added


def _staged_row(
    root: Path,
    wallet: Mapping[str, Any],
    event: Mapping[str, str],
    owned: Set[str],
    missed: bool,
) -> Dict[str, Any]:
    suggestion = _suggestion(root, event)
    kind, note, tag = _treatment(wallet, event, owned, suggestion)
    buy_asset, buy_qty, sell_asset, sell_qty, asset, quantity = _sides(event)
    return {
        "id": _new_id(),
        "wallet_id": wallet.get("id"),
        "wallet": wallet.get("name"),
        "chain": event.get("chain") or wallet.get("chain") or "",
        "tx_hash": event.get("tx_hash") or "",
        "asset": asset,
        "direction": event.get("direction") or "",
        "quantity": quantity,
        "type": kind,
        "buy_quantity": buy_qty,
        "buy_asset": buy_asset,
        "sell_quantity": sell_qty,
        "sell_asset": sell_asset,
        "fee_quantity": event.get("fee_quantity") or "",
        "fee_asset": event.get("fee_asset") or "",
        "timestamp": event.get("timestamp") or "",
        "block": event.get("block") or "",
        "suggested_tag": tag,
        "confirmed": False,
        "missed_by_koinly": missed,
        "accepted": False,
        "ledger_id": "",
        "note": note,
    }


def _suggestion(root: Path, event: Mapping[str, str]) -> Dict[str, str]:
    if event.get("shape") == "trade":
        return {}
    found = suggestion_for(root, event.get("contract") or "", event.get("method") or "")
    if found:
        return found
    return suggestion_for(root, event.get("counterparty") or "", event.get("method") or "")


def _treatment(
    wallet: Mapping[str, Any],
    event: Mapping[str, str],
    owned: Set[str],
    suggestion: Mapping[str, str],
) -> Tuple[str, str, str]:
    if suggestion:
        label = suggestion.get("label") or suggestion.get("tag") or "contract"
        note = f"Suggested tag {label}. Not confirmed. No tax treatment is applied."
        return NEEDS_REVIEW, note, str(suggestion.get("tag") or "")
    if event.get("shape") == "trade":
        return "Trade", str(event.get("method") or ""), ""
    counterparty = str(event.get("counterparty") or "").casefold()
    if counterparty and counterparty in owned:
        kind = "Deposit" if event.get("direction") == "in" else "Withdrawal"
        return kind, "Own-wallet transfer", ""
    if event.get("direction") == "in":
        tag = str(wallet.get("deposit_tag") or "")
        note = "Suggested tag for this deposit. Not confirmed." if tag else ""
        return "Deposit", note, tag
    return "Withdrawal", "", ""


def _sides(event: Mapping[str, str]) -> Tuple[str, str, str, str, str, str]:
    if event.get("shape") == "trade":
        return (
            event.get("buy_asset") or "",
            event.get("buy_quantity") or "",
            event.get("sell_asset") or "",
            event.get("sell_quantity") or "",
            event.get("asset") or "",
            event.get("quantity") or "",
        )
    if event.get("direction") == "in":
        return (
            event.get("asset") or "",
            event.get("quantity") or "",
            "",
            "",
            event.get("asset") or "",
            event.get("quantity") or "",
        )
    return (
        "",
        "",
        event.get("asset") or "",
        event.get("quantity") or "",
        event.get("asset") or "",
        event.get("quantity") or "",
    )


def _finish_sync(
    root: Path, wallet_id: str, result: Mapping[str, Any], added: Sequence[Any]
) -> None:
    del added
    stored = _wallets(root)
    current = _find(stored, wallet_id)
    current["cursor"] = str(result.get("cursor") or current.get("cursor") or "")
    current["last_sync_at"] = _stamp()
    current["last_error"] = ""
    notice = str(result.get("notice") or "")
    if notice:
        current["notice"] = notice
    if not current.get("ignore_reported"):
        raw_reported = result.get("reported")
        reported = raw_reported if isinstance(raw_reported, dict) else {}
        current["reported"] = {str(key): str(value) for key, value in reported.items()}
    else:
        current["reported"] = {}
    write_json(root / "wallets.json", {"wallets": stored})


def _sync_settings(
    root: Path, wallet: Mapping[str, Any], secret: Mapping[str, str]
) -> Dict[str, str]:
    method = str(wallet.get("method") or "")
    if method in {"file", "later"} or (method != "api" and not wallet.get("address")):
        raise DeskError(
            "Import a file for this wallet. It has no read-only API key or public address."
        )
    if method == "api":
        if not secret.get("api_key") or not secret.get("api_secret"):
            raise DeskError("The read-only API key is missing. Reconnect this wallet.")
        return _exchange_settings(
            str(wallet.get("integration") or ""), secret, str(wallet.get("cursor") or "")
        )
    chain = find_chain(str(wallet.get("chain") or ""))
    explorer, rpc, chain_id = _endpoints(root, wallet, chain)
    return {
        "kind": chain["family"],
        "chain": chain["id"],
        "chain_id": chain_id,
        "symbol": chain["symbol"],
        "address": str(wallet.get("address") or ""),
        "cursor": str(wallet.get("cursor") or ""),
        "explorer_url": explorer,
        "rpc_url": rpc,
        "api_key": secret.get("api_key") or "",
        "api_secret": "",
        "passphrase": "",
    }


def _endpoints(
    root: Path, wallet: Mapping[str, Any], chain: Mapping[str, str]
) -> Tuple[str, str, str]:
    if chain["id"] == "robinhood":
        urls = robinhood_urls(
            root, str(wallet.get("explorer_url") or ""), str(wallet.get("rpc_url") or "")
        )
        return urls["explorer_url"], urls["rpc_url"], urls["chain_id"]
    if chain["family"] == "solana":
        return "", str(wallet.get("rpc_url") or chain["endpoint"]), ""
    if chain["family"] == "bitcoin":
        return str(wallet.get("explorer_url") or chain["endpoint"]), "", ""
    return chain["endpoint"], "", chain["chain_id"]


def _exchange_settings(
    integration: str, secret: Mapping[str, str], cursor: str = ""
) -> Dict[str, str]:
    return {
        "kind": integration,
        "chain": "",
        "chain_id": "",
        "symbol": "",
        "address": "",
        "cursor": cursor,
        "explorer_url": "",
        "rpc_url": "",
        "api_key": secret.get("api_key") or "",
        "api_secret": secret.get("api_secret") or "",
        "passphrase": secret.get("passphrase") or "",
    }


def _selected(integration: Mapping[str, str], chosen: Sequence[Any]) -> List[str]:
    if integration["group"] in {"exchange", "service"}:
        return []
    wanted = {str(item).strip().casefold() for item in chosen if str(item).strip()}
    if integration["id"] in {item["id"] for item in chains()} and not wanted:
        wanted = {integration["id"]}
    if not wanted and integration["family"] == "solana":
        wanted = {"solana"}
    if not wanted and integration["family"] == "bitcoin":
        wanted = {"bitcoin"}
    if not wanted:
        wanted = {"ethereum"}
    ordered = [item["id"] for item in chains() if item["id"] in wanted]
    if not ordered:
        raise DeskError("Choose a chain from the list.")
    families = {find_chain(item)["family"] for item in ordered}
    if len(families) > 1:
        raise DeskError("Add Solana or Bitcoin as its own wallet. One address is not used on both.")
    if integration["family"] in {"solana", "bitcoin"} and integration["family"] not in families:
        raise DeskError("Choose the chain that matches this wallet.")
    return ordered


def _names(
    base: str, integration: Mapping[str, str], selected: Sequence[str]
) -> List[Tuple[str, str]]:
    cleaned = " ".join(base.split()) or integration["name"]
    if len(cleaned) > 80:
        raise DeskError("The wallet name is too long.")
    if len(selected) <= 1:
        return [(cleaned, selected[0] if selected else "")]
    return [(f"{cleaned} - {find_chain(chain)['name']}", chain) for chain in selected]


def _method(integration: Mapping[str, str], requested: str) -> str:
    if integration["group"] in {"blockchain", "wallet"}:
        return "address"
    if integration["group"] == "service":
        return "later"
    choice = requested or ("api" if integration["api"] == "yes" else "file")
    if choice not in {"api", "file", "later"}:
        raise DeskError("Choose a read-only API, a file import, or add data later.")
    if choice == "api" and integration["api"] != "yes":
        raise DeskError("Read-only API sync is available for Coinbase and KuCoin.")
    return choice


def _family(integration: Mapping[str, str], selected: Sequence[str]) -> str:
    if selected:
        return find_chain(selected[0])["family"]
    return integration.get("family") or ""


def _address(family: str, address: str, method: str) -> str:
    if method != "address":
        return ""
    cleaned = address.strip()
    if family == "evm" and not _EVM.match(cleaned):
        raise DeskError("Enter one EVM address.")
    if family == "solana" and not _SOL.match(cleaned):
        raise DeskError("Enter one Solana address.")
    if family == "bitcoin" and not _BTC.match(cleaned):
        raise DeskError("Enter one Bitcoin address.")
    if not cleaned:
        raise DeskError("Enter a public address.")
    return cleaned


def _urls(root: Path, selected: Sequence[str], spec: Mapping[str, Any]) -> Tuple[str, str]:
    explorer = _http(str(spec.get("explorer_url") or ""))
    rpc = _http(str(spec.get("rpc_url") or ""))
    if "robinhood" not in selected:
        return explorer, rpc
    urls = robinhood_urls(root, explorer, rpc)
    if not urls["explorer_url"] and not urls["rpc_url"]:
        raise DeskError("Robinhood Chain needs an explorer or RPC URL.")
    return urls["explorer_url"], urls["rpc_url"]


def _chain_urls(chain: str, explorer: str, rpc: str) -> Tuple[str, str]:
    if chain == "robinhood":
        return explorer, rpc
    if chain == "solana":
        return "", rpc
    if chain == "bitcoin":
        return explorer, ""
    return "", ""


def _http(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("https://") and not cleaned.startswith("http://"):
        raise DeskError("The explorer or RPC URL must start with https://.")
    return cleaned.rstrip("/")


def _start(value: str) -> str:
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise DeskError("The start date must be a year, month and day.") from exc
    return value


def _deposit_tag(value: str) -> str:
    if not value:
        return ""
    from .tag_set import catalogue  # pylint: disable=import-outside-toplevel

    known = {str(item.get("id") or "") for item in catalogue()}
    if value not in known:
        raise DeskError("Choose a tag from the list.")
    return value


def _require_exchange_secret(integration: str, secret: Mapping[str, str]) -> None:
    if not secret.get("api_key") or not secret.get("api_secret"):
        raise DeskError("Enter the read-only API key and secret. Keys must be READ-ONLY.")
    if integration == "kucoin" and not secret.get("passphrase"):
        raise DeskError("Enter the KuCoin passphrase. The key must be read-only.")


def _record(
    name: str,
    integration: Mapping[str, str],
    chain: str,
    address: str,
    method: str,
    start: str,
    tag: str,
    spec: Mapping[str, Any],
    explorer: str,
    rpc: str,
    notice: str,
) -> Dict[str, Any]:
    return {
        "id": _new_id(),
        "name": name,
        "integration": integration["id"],
        "group": integration["group"],
        "kind": integration["group"],
        "chain": chain,
        "address": address,
        "method": method,
        "added_at": _stamp(),
        "last_sync_at": "",
        "last_error": "",
        "notice": notice,
        "cursor": "",
        "start_date": start,
        "deposit_tag": tag,
        "ignore_reported": bool(spec.get("ignore_reported")),
        "explorer_url": explorer,
        "rpc_url": rpc,
        "deleted_at": "",
        "reported": {},
        "file_imports": [],
        "secret_store": "",
    }


def _clashes(root: Path, names: Sequence[str]) -> List[Dict[str, Any]]:
    folded = {name.casefold() for name in names}
    return [
        row
        for row in _wallets(root)
        if not row.get("deleted_at") and str(row.get("name") or "").casefold() in folded
    ]


def _public(wallet: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "id": str(wallet.get("id") or ""),
        "name": str(wallet.get("name") or ""),
        "integration": str(wallet.get("integration") or ""),
        "chain": str(wallet.get("chain") or ""),
    }


def _tx_count(desk: Any, wallet: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> int:
    open_rows = [
        row
        for row in _review(desk.root)
        if row.get("wallet_id") == wallet.get("id") and not row.get("accepted")
    ]
    linked = _linked(desk.root, wallet.get("file_imports") or [])
    linked_ids = {str(row.get("ledger_id") or "") for row in linked if row.get("accepted")}
    own = [
        row
        for row in ledger
        if row.get("wallet") == wallet.get("name")
        and not row.get("deleted")
        and str(row.get("id") or "") not in linked_ids
    ]
    return len(open_rows) + len(own) + len(linked)


def _linked(root: Path, import_ids: Sequence[Any]) -> List[Dict[str, Any]]:
    wanted = {str(item) for item in import_ids}
    if not wanted:
        return []
    loaded = read_json(root / "exchanges.json")
    found: List[Dict[str, Any]] = []
    for item in loaded.get("imports") or []:
        if isinstance(item, dict) and str(item.get("id") or "") in wanted:
            found.extend(row for row in item.get("rows") or [] if isinstance(row, dict))
    return found


def _computed(rows: Sequence[Mapping[str, Any]], wallet: str) -> Dict[str, Decimal]:
    totals: Dict[str, Decimal] = {}
    for row in rows:
        if row.get("deleted") or row.get("wallet") != wallet:
            continue
        _apply(totals, row.get("buy_asset"), row.get("buy_quantity"), Decimal(1))
        _apply(totals, row.get("sell_asset"), row.get("sell_quantity"), Decimal(-1))
        _apply(totals, row.get("fee_asset"), row.get("fee_quantity"), Decimal(-1))
    return totals


def _apply(totals: Dict[str, Decimal], asset: object, quantity: object, sign: Decimal) -> None:
    name = str(asset or "")
    amount = _decimal(quantity)
    if not name or amount == 0:
        return
    totals[name] = totals.get(name, Decimal(0)) + (amount * sign)


def _differences(
    computed: Mapping[str, Decimal], reported: Mapping[str, Any]
) -> List[Tuple[str, Decimal, Decimal]]:
    folded = {asset.casefold(): amount for asset, amount in computed.items()}
    found = []
    for asset, raw in reported.items():
        try:
            wanted = Decimal(str(raw))
        except InvalidOperation:
            continue
        got = folded.get(str(asset).casefold(), Decimal(0))
        if abs(got - wanted) > _TOLERANCE:
            found.append((str(asset), got, wanted))
    return found


def _value_text(computed: Mapping[str, Decimal], reported: Mapping[str, Any]) -> str:
    parts = [
        f"{_plain(amount)} {asset}" for asset, amount in sorted(computed.items()) if amount != 0
    ]
    if parts:
        return ", ".join(parts)
    reported_parts = []
    for asset, raw in sorted(reported.items()):
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            continue
        if amount != 0:
            reported_parts.append(f"{_plain(amount)} {asset}")
    return ", ".join(reported_parts) if reported_parts else "-"


def _event_keys(wallet_name: str, event: Mapping[str, str]) -> List[Key]:
    wallet = wallet_name.casefold()
    chain = str(event.get("chain") or "").casefold()
    digest = _digest(event)
    if event.get("shape") == "trade":
        keys = []
        if event.get("buy_asset"):
            keys.append((chain, digest, wallet, event["buy_asset"].casefold(), "in"))
        if event.get("sell_asset"):
            keys.append((chain, digest, wallet, event["sell_asset"].casefold(), "out"))
        return keys
    return [
        (
            chain,
            digest,
            wallet,
            str(event.get("asset") or "").casefold(),
            str(event.get("direction") or ""),
        )
    ]


def _digest(event: Mapping[str, str]) -> str:
    digest = str(event.get("tx_hash") or "").strip()
    if digest:
        return digest.casefold()
    raw = "|".join(
        [
            event.get("timestamp") or "",
            event.get("asset") or "",
            event.get("direction") or "",
            event.get("quantity") or "",
        ]
    )
    return "local:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ledger_keys(rows: Sequence[Mapping[str, Any]]) -> List[Key]:
    found: List[Key] = []
    for row in rows:
        if row.get("deleted"):
            continue
        chain = str(row.get("chain") or "").casefold()
        digest = str(row.get("tx_hash") or "").strip().casefold()
        wallet = str(row.get("wallet") or "").casefold()
        if not digest:
            continue
        if _decimal(row.get("buy_quantity")) and row.get("buy_asset"):
            found.append((chain, digest, wallet, str(row["buy_asset"]).casefold(), "in"))
        if _decimal(row.get("sell_quantity")) and row.get("sell_asset"):
            found.append((chain, digest, wallet, str(row["sell_asset"]).casefold(), "out"))
    return found


def _koinly_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    found = []
    for row in rows:
        source = f"{row.get('import_type') or ''} {row.get('import_source') or ''}".casefold()
        if "koinly" in source:
            found.append(row)
    return found


def _staged_keys(root: Path) -> List[Key]:
    found: List[Key] = []
    for row in _review(root):
        if row.get("accepted"):
            continue
        event = {
            "shape": "trade" if row.get("type") == "Trade" else "transfer",
            "chain": str(row.get("chain") or ""),
            "tx_hash": str(row.get("tx_hash") or ""),
            "asset": str(row.get("asset") or ""),
            "direction": str(row.get("direction") or ""),
            "quantity": str(row.get("quantity") or ""),
            "timestamp": str(row.get("timestamp") or ""),
            "buy_asset": str(row.get("buy_asset") or ""),
            "sell_asset": str(row.get("sell_asset") or ""),
        }
        found.extend(_event_keys(str(row.get("wallet") or ""), event))
    return found


def _seen(key: Key, existing: Sequence[Key]) -> bool:
    chain, digest, wallet, asset, direction = key
    if not digest:
        return False
    for item in existing:
        if (item[1], item[2], item[3], item[4]) != (digest, wallet, asset, direction):
            continue
        if item[0] == chain or not item[0] or not chain:
            return True
    return False


def _owned(root: Path) -> Set[str]:
    found: Set[str] = set()
    for wallet in _wallets(root):
        if wallet.get("deleted_at"):
            continue
        address = str(wallet.get("address") or "").strip()
        if address:
            found.add(address.casefold())
    return found


def _too_early(event: Mapping[str, str], start: str) -> bool:
    if not start:
        return False
    stamp = str(event.get("timestamp") or "")[:10]
    return bool(stamp) and stamp < start


def _review_view(row: Mapping[str, Any]) -> Dict[str, str]:
    note = str(row.get("note") or "")
    if row.get("missed_by_koinly"):
        note = f"Missed by Koinly. {note}".strip()
    return {
        "id": str(row.get("id") or ""),
        "kind": "wallet",
        "needs_type": "no",
        "timestamp": str(row.get("timestamp") or ""),
        "asset": str(row.get("asset") or ""),
        "quantity": str(row.get("quantity") or ""),
        "wallet": str(row.get("wallet") or ""),
        "destination": "",
        "tx_hash": str(row.get("tx_hash") or ""),
        "description": note,
        "disposition": "Missed by Koinly" if row.get("missed_by_koinly") else "Wallet review",
        "suggested_tag": str(row.get("suggested_tag") or ""),
        "confirmed": "yes" if row.get("confirmed") is True else "no",
        "missed": "yes" if row.get("missed_by_koinly") else "no",
    }


def _cells(row: Mapping[str, Any]) -> List[str]:
    note = str(row.get("note") or "")
    if row.get("missed_by_koinly"):
        note = f"Missed by Koinly. {note}".strip()
    return [
        str(row.get("type") or ""),
        str(row.get("buy_quantity") or ""),
        str(row.get("buy_asset") or ""),
        "",
        str(row.get("sell_quantity") or ""),
        str(row.get("sell_asset") or ""),
        "",
        str(row.get("fee_quantity") or ""),
        str(row.get("fee_asset") or ""),
        "",
        str(row.get("wallet") or ""),
        str(row.get("timestamp") or ""),
        note,
    ]


def _append_ledger(desk: Any, cells: List[str], meta: Mapping[str, str], who: str) -> str:
    ledger = read_json(desk.root / "ledger.json")
    raw_rows = ledger.get("rows")
    rows: List[Any] = raw_rows if isinstance(raw_rows, list) else []
    import_id = _new_id()
    row_id = f"{import_id}:{len(rows):04d}"
    rows.append({"id": row_id, "import_id": import_id, "cells": cells})
    write_json(desk.root / "ledger.json", {"rows": rows})
    if any(meta.values()):
        update_meta(
            desk.root, row_id, {key: value for key, value in meta.items() if value or key == "tag"}
        )
    desk._row_cache = None  # pylint: disable=protected-access
    desk._write_effective_unlocked()  # pylint: disable=protected-access
    append_event(
        desk.root,
        "wallet-accept",
        who,
        "Accepted a synced row into the tax report.",
        {"row_id": row_id},
    )
    return row_id


def _mark_error(root: Path, wallet_id: str, message: str) -> None:
    stored = _wallets(root)
    current = _find(stored, wallet_id)
    current["last_error"] = message or "The sync failed."
    write_json(root / "wallets.json", {"wallets": stored})


def _log(root: Path, wallet_id: str, rows: int) -> None:
    line = '{"kind": "wallet", "wallet_id": "' + wallet_id + '", "rows": ' + str(rows) + "}\n"
    append_text(root / "sync-log.jsonl", line)


def _wallets(root: Path) -> List[Dict[str, Any]]:
    loaded = read_json(root / "wallets.json")
    rows = loaded.get("wallets")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _review(root: Path) -> List[Dict[str, Any]]:
    loaded = read_json(root / "wallet-review.json")
    rows = loaded.get("rows")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _require(root: Path, wallet_id: str) -> Dict[str, Any]:
    wallet = _find(_wallets(root), wallet_id)
    if wallet.get("deleted_at"):
        raise DeskError("That wallet was deleted.")
    return wallet


def _find(rows: Sequence[Dict[str, Any]], wallet_id: str) -> Dict[str, Any]:
    for row in rows:
        if row.get("id") == wallet_id:
            return row
    raise DeskError("That wallet is not on this desk.")


def _in_year(stamp: str, year: int) -> bool:
    try:
        found = date.fromisoformat(stamp[:10])
    except ValueError:
        return True
    return tax_year_containing(found) == year


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal(0)


def _plain(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
