# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""More read-only sources. Tests pass a fetch stand-in. No live call is required."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from ..config import config
from .errors import DeskError
from .store import Desk
from .sync import (  # pylint: disable=protected-access
    _ADDRESS,
    _etherscan_csv,
    _fetch_urllib,
    _scrub,
    _write_private,
)

Fetch = Any
_LAMPORT = Decimal(10) ** 9
_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_CHAINS = {"ethereum": "1", "base": "8453"}
_ETHERSCAN = "https://api.etherscan.io/v2/api"
_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
_KUCOIN = "https://api.kucoin.com"
_COINBASE = "https://api.coinbase.com"


def import_evm(
    desk: Desk,
    address: str,
    chain: str = "base",
    api_key: str = "",
    explorer_url: str = "",
    chain_id: str = "",
    native_symbol: str = "",
    fetch: Optional[Fetch] = None,
) -> Dict[str, Any]:
    """Read one address from an Etherscan-family explorer. A custom URL adds a chain."""
    chosen = address.strip()
    if not _ADDRESS.match(chosen):
        raise DeskError("Enter one EVM address.")
    key = api_key.strip() or _stored(desk, "etherscan.key")
    if not key:
        raise DeskError("Enter an explorer API key. It is stored only in the data directory.")
    if api_key.strip():
        _write_private(desk.root / "secrets" / "etherscan.key", key)
    selected = _chain(desk, chain, explorer_url, chain_id)
    query = urlencode(
        {
            "chainid": selected["chain_id"],
            "module": "account",
            "action": "txlist",
            "address": chosen,
            "startblock": "0",
            "endblock": "99999999",
            "sort": "asc",
            "apikey": key,
        }
    )
    url = f"{selected['explorer_url']}?{query}"
    payload = _json(_call(fetch, url, secret=key), key)
    rows = payload.get("result")
    if not isinstance(rows, list) or not rows:
        raise DeskError(_scrub(str(payload.get("message", "No transactions returned.")), key))
    name = f"{selected['name']}-{chosen[-6:]}.csv"
    if selected["name"] == "ethereum":
        return desk.import_bytes(name, _etherscan_csv(chosen, rows))
    symbol = native_symbol.strip().upper() or "ETH"
    return desk.import_bytes(name, _generic_csv(_evm_rows(chosen, rows, symbol)))


def import_solana(
    desk: Desk, address: str, rpc_url: str = "", fetch: Optional[Fetch] = None
) -> Dict[str, Any]:
    """Read native SOL balance changes from a public JSON-RPC node."""
    chosen = address.strip()
    if not _SOLANA.match(chosen):
        raise DeskError("Enter one Solana address.")
    endpoint = rpc_url.strip() or _SOLANA_RPC
    listed = _rpc(endpoint, "getSignaturesForAddress", [chosen, {"limit": 25}], fetch)
    signatures = listed.get("result") if isinstance(listed, dict) else None
    if not isinstance(signatures, list) or not signatures:
        raise DeskError("The Solana node returned no signatures.")
    lines = []
    for item in signatures:
        signature = str(item.get("signature") or "")
        if not signature:
            continue
        body = _rpc(endpoint, "getTransaction", [signature, {"encoding": "json"}], fetch)
        row = _solana_row(chosen, signature, body.get("result") if isinstance(body, dict) else None)
        if row:
            lines.append(row)
    if not lines:
        raise DeskError("The Solana node returned no balance changes.")
    return desk.import_bytes(f"solana-{chosen[-6:]}.csv", _generic_csv(lines))


def import_kucoin(
    desk: Desk,
    api_key: str = "",
    api_secret: str = "",
    passphrase: str = "",
    fetch: Optional[Fetch] = None,
) -> Dict[str, Any]:
    """Read KuCoin deposits and hand them to the KuCoin deposits parser."""
    stored = _json_secret(desk, "kucoin.json")
    key = api_key.strip() or stored.get("key", "")
    secret = api_secret.strip() or stored.get("secret", "")
    phrase = passphrase.strip() or stored.get("passphrase", "")
    if not key or not secret or not phrase:
        raise DeskError(
            "Enter the KuCoin key, secret, and passphrase. They stay in the data directory."
        )
    if api_key.strip() or api_secret.strip() or passphrase.strip():
        _write_private(
            desk.root / "secrets" / "kucoin.json",
            json.dumps({"key": key, "secret": secret, "passphrase": phrase}),
        )
    path = "/api/v1/deposits"
    stamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    headers = {
        "KC-API-KEY": key,
        "KC-API-SIGN": _hmac(secret, f"{stamp}GET{path}"),
        "KC-API-TIMESTAMP": stamp,
        "KC-API-PASSPHRASE": _hmac(secret, phrase),
        "KC-API-KEY-VERSION": "2",
    }
    payload = _json(
        _call(fetch, _KUCOIN + path, headers=headers, secret=f"{key} {secret} {phrase}"),
        f"{key} {secret}",
    )
    items = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, dict) else []
    if not items:
        raise DeskError("The exchange returned no deposits.")
    return desk.import_bytes("kucoin-deposits.csv", _kucoin_csv(items))


def import_coinbase(
    desk: Desk, api_key: str = "", api_secret: str = "", fetch: Optional[Fetch] = None
) -> Dict[str, Any]:
    """Read Coinbase Advanced Trade fills and write them as generic rows."""
    stored = _json_secret(desk, "coinbase.json")
    key = api_key.strip() or stored.get("key", "")
    secret = api_secret.strip() or stored.get("secret", "")
    if not key or not secret:
        raise DeskError("Enter the Coinbase key and secret. They stay in the data directory.")
    if api_key.strip() or api_secret.strip():
        _write_private(
            desk.root / "secrets" / "coinbase.json",
            json.dumps({"key": key, "secret": secret}),
        )
    path = "/api/v3/brokerage/orders/historical/fills"
    stamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "CB-ACCESS-KEY": key,
        "CB-ACCESS-SIGN": _hmac(secret, f"{stamp}GET{path}"),
        "CB-ACCESS-TIMESTAMP": stamp,
    }
    payload = _json(
        _call(fetch, _COINBASE + path, headers=headers, secret=f"{key} {secret}"), f"{key} {secret}"
    )
    fills = payload.get("fills") if isinstance(payload, dict) else None
    if not isinstance(fills, list) or not fills:
        raise DeskError("The exchange returned no fills.")
    return desk.import_bytes(
        "coinbase-fills.csv", _generic_csv([_fill_row(item) for item in fills])
    )


def import_revolut_csv(desk: Desk, filename: str, data: bytes) -> Dict[str, Any]:
    """Import a Revolut CSV through the existing Revolut parser."""
    name = filename.strip() or "revolut.csv"
    return desk.import_bytes(name, data)


def extra_status(desk: Desk) -> Dict[str, Any]:
    root = desk.root / "secrets"
    return {
        "kucoin": (root / "kucoin.json").is_file(),
        "coinbase": (root / "coinbase.json").is_file(),
        "chains": _saved_chains(desk),
    }


def _chain(desk: Desk, chain: str, explorer_url: str, chain_id: str) -> Dict[str, str]:
    name = chain.strip().casefold() or "base"
    custom_url = explorer_url.strip().rstrip("/")
    custom_id = chain_id.strip()
    if custom_url or name == "custom":
        if not custom_url or not custom_id:
            raise DeskError("A custom chain needs an explorer URL and a chain id.")
        _remember_chain(desk, custom_id, custom_url)
        return {"name": "custom", "chain_id": custom_id, "explorer_url": custom_url}
    selected = _CHAINS.get(name)
    if not selected:
        raise DeskError("Choose Ethereum, Base, or a custom explorer URL.")
    return {"name": name, "chain_id": custom_id or selected, "explorer_url": _ETHERSCAN}


def _remember_chain(desk: Desk, chain_id: str, explorer_url: str) -> None:
    path = desk.root / "chains.json"
    loaded: Dict[str, Any] = {"chains": []}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in loaded.get("chains", []) if str(row.get("chain_id")) != chain_id]
    rows.append({"chain_id": chain_id, "explorer_url": explorer_url})
    _write_private(path, json.dumps({"chains": rows}, indent=2) + "\n")


def _saved_chains(desk: Desk) -> List[Dict[str, str]]:
    path = desk.root / "chains.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return [
        {"chain_id": str(row.get("chain_id", "")), "explorer_url": str(row.get("explorer_url", ""))}
        for row in loaded.get("chains", [])
        if isinstance(row, dict)
    ]


def _rpc(url: str, method: str, params: List[Any], fetch: Optional[Fetch]) -> Dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return _json(_call(fetch, url, data=body, headers={"Content-Type": "application/json"}), "")


def _evm_rows(address: str, rows: List[Mapping[str, Any]], symbol: str) -> List[Dict[str, str]]:
    own = address.lower()
    lines = []
    for row in rows:
        failed = str(row.get("isError", "0")) == "1"
        if failed:
            continue
        amount = Decimal(str(row.get("value", "0"))) / (Decimal(10) ** 18)
        if amount == 0:
            continue
        stamp = datetime.fromtimestamp(int(str(row.get("timeStamp") or "0")), timezone.utc)
        when = stamp.strftime("%Y-%m-%d %H:%M:%S")
        quantity = f"{amount.normalize():f}"
        note = str(row.get("hash") or "")[:18]
        incoming = str(row.get("to", "")).lower() == own
        if incoming:
            lines.append(_generic("Deposit", quantity, symbol, "", "", when, note))
        else:
            lines.append(_generic("Withdrawal", "", "", quantity, symbol, when, note))
    return lines


def _solana_row(
    address: str, signature: str, result: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, str]]:
    if not isinstance(result, dict):
        return None
    message = (result.get("transaction") or {}).get("message") or {}
    keys = list(message.get("accountKeys") or [])
    if address not in keys:
        return None
    index = keys.index(address)
    meta = result.get("meta") or {}
    before = Decimal(str((meta.get("preBalances") or [0])[index]))
    after = Decimal(str((meta.get("postBalances") or [0])[index]))
    moved = (after - before) / _LAMPORT
    if moved == 0:
        return None
    when = datetime.fromtimestamp(int(result.get("blockTime") or 0), timezone.utc)
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    quantity = f"{abs(moved).normalize():f}"
    if moved > 0:
        return _generic("Deposit", quantity, "SOL", "", "", stamp, signature)
    return _generic("Withdrawal", "", "", quantity, "SOL", stamp, signature)


def _fill_row(item: Mapping[str, Any]) -> Dict[str, str]:
    product = str(item.get("product_id") or "ETH-GBP")
    base, _dash, quote = product.partition("-")
    size = Decimal(str(item.get("size") or "0"))
    price = Decimal(str(item.get("price") or "0"))
    quote_qty = f"{(size * price).normalize():f}"
    when = str(item.get("trade_time") or "").replace("T", " ").replace("Z", "")
    side = str(item.get("side") or "BUY").upper()
    fee = str(item.get("commission") or "")
    if side == "SELL":
        return _generic(
            "Trade",
            "",
            "",
            f"{size.normalize():f}",
            base or "ETH",
            when,
            "coinbase fill",
            quote_qty,
            quote,
            fee,
        )
    return _generic(
        "Trade",
        f"{size.normalize():f}",
        base or "ETH",
        quote_qty,
        quote or "GBP",
        when,
        "coinbase fill",
        "",
        quote or "GBP",
        fee,
    )


def _generic(
    kind: str,
    buy_qty: str,
    buy_asset: str,
    sell_qty: str,
    sell_asset: str,
    stamp: str,
    note: str,
    sell_value: str = "",
    fee_asset: str = "",
    fee_qty: str = "",
) -> Dict[str, str]:
    return {
        "type": kind,
        "buy_quantity": buy_qty,
        "buy_asset": buy_asset,
        "sell_quantity": sell_qty,
        "sell_asset": sell_asset,
        "sell_value": sell_value,
        "fee_quantity": fee_qty,
        "fee_asset": fee_asset,
        "timestamp": stamp,
        "note": note,
    }


def _generic_csv(rows: Sequence[Mapping[str, str]]) -> bytes:
    currency = str(config.ccy)
    header = [
        "Type",
        "Buy Quantity",
        "Buy Asset",
        f"Buy Value in {currency}",
        "Sell Quantity",
        "Sell Asset",
        f"Sell Value in {currency}",
        "Fee Quantity",
        "Fee Asset",
        f"Fee Value in {currency}",
        "Wallet",
        "Timestamp",
        "Note",
        "Raw Data",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(
            [
                row.get("type", ""),
                row.get("buy_quantity", ""),
                row.get("buy_asset", ""),
                "",
                row.get("sell_quantity", ""),
                row.get("sell_asset", ""),
                row.get("sell_value", ""),
                row.get("fee_quantity", ""),
                row.get("fee_asset", ""),
                "",
                "Desk import",
                row.get("timestamp", ""),
                row.get("note", ""),
                "",
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _kucoin_csv(items: List[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Time", "Coin", "Amount", "Type", "Remark"])
    for item in items:
        stamp = _millis(item.get("createdAt"))
        writer.writerow(
            [
                stamp,
                str(item.get("currency") or ""),
                str(item.get("amount") or ""),
                "Deposit",
                "sample",
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _millis(value: object) -> str:
    try:
        when = datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError):
        return "2025-01-01 00:00:00"
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _hmac(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return digest.hex()


def _call(
    fetch: Optional[Fetch],
    url: str,
    data: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    secret: str = "",
) -> bytes:
    caller = _fetch_urllib if fetch is None else fetch
    try:
        return caller(url, data, dict(headers or {}))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise DeskError(_scrub(str(exc), secret)) from exc


def _json(body: bytes, secret: str) -> Dict[str, Any]:
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeskError("The response was not JSON.") from exc
    if not isinstance(loaded, dict):
        raise DeskError(_scrub("The response was not a JSON object.", secret))
    return loaded


def _stored(desk: Desk, name: str) -> str:
    path = desk.root / "secrets" / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _json_secret(desk: Desk, name: str) -> Dict[str, str]:
    path = desk.root / "secrets" / name
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items()}
