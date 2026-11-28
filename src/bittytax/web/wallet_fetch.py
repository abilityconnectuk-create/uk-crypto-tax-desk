# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Read-only chain and exchange pulls. Tests pass a fetch stand-in."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

from .errors import DeskError
from .sync import _fetch_urllib, _scrub  # pylint: disable=protected-access

Fetch = Callable[..., bytes]
REFUSAL = "This key can trade or withdraw. Use a read-only key."
_READONLY_NOTICE = "The exchange did not confirm that this key is read-only."
_RPC_NOTICE = "Add an explorer URL to import transactions. The balance used the RPC."
_COINBASE = "https://api.coinbase.com"
_KUCOIN = "https://api.kucoin.com"
_READONLY_WORDS = {"general", "read", "readonly", "view", "public"}


def default_fetch(
    url: str, data: Optional[str] = None, headers: Optional[Mapping[str, str]] = None
) -> bytes:
    """The real HTTP call. Tests replace this or pass their own fetch."""
    return _fetch_urllib(url, data, headers)


def check_keys(settings: Mapping[str, str], fetch: Optional[Fetch] = None) -> str:
    """Refuse a key that can trade or withdraw. Returns a notice when the exchange is silent."""
    chosen = default_fetch if fetch is None else fetch
    secret = _secret(settings)
    kind = settings.get("kind") or ""
    if kind == "coinbase":
        return _read_permission(
            settings, chosen, secret, _COINBASE + "/api/v3/brokerage/key_permissions"
        )
    if kind == "kucoin":
        return _read_permission(settings, chosen, secret, _KUCOIN + "/api/v1/user/api-key")
    return ""


def pull(settings: Mapping[str, str], fetch: Optional[Fetch] = None) -> Dict[str, Any]:
    """Events, the next cursor, and reported balances. No key is written here."""
    chosen = default_fetch if fetch is None else fetch
    kind = settings.get("kind") or ""
    secret = _secret(settings)
    if kind == "coinbase":
        return _exchange_coinbase(settings, chosen, secret)
    if kind == "kucoin":
        return _exchange_kucoin(settings, chosen, secret)
    if kind == "solana":
        return _solana(settings, chosen, secret)
    if kind == "bitcoin":
        return _bitcoin(settings, chosen, secret)
    if kind == "evm":
        return _evm(settings, chosen, secret)
    raise DeskError("This wallet cannot be synced yet.")


def permission_problem(payload: Mapping[str, Any]) -> str:
    """A refusal when the exchange says the key can trade or withdraw."""
    if (
        _truthy(payload.get("can_trade"))
        or _truthy(payload.get("can_transfer"))
        or _truthy(payload.get("can_withdraw"))
    ):
        return REFUSAL
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    text = str(source.get("permission") or source.get("permissions") or "")
    folded = "".join(char for char in text.casefold() if char.isalnum())
    if "withdraw" in folded or "trade" in folded:
        return REFUSAL
    return ""


def _exchange_coinbase(settings: Mapping[str, str], fetch: Fetch, secret: str) -> Dict[str, Any]:
    notice = _read_permission(
        settings, fetch, secret, _COINBASE + "/api/v3/brokerage/key_permissions"
    )
    path = "/api/v3/brokerage/orders/historical/fills"
    query = f"?cursor={settings['cursor']}" if settings.get("cursor") else ""
    payload = _signed_get(settings, fetch, secret, _COINBASE, path, query, "coinbase")
    raw_fills = payload.get("fills")
    fills = raw_fills if isinstance(raw_fills, list) else []
    events = [_coinbase_fill(item) for item in fills if isinstance(item, dict)]
    accounts = _signed_get(
        settings, fetch, secret, _COINBASE, "/api/v3/brokerage/accounts", "", "coinbase"
    )
    return {
        "events": [event for event in events if event],
        "cursor": str(payload.get("cursor") or settings.get("cursor") or ""),
        "reported": _coinbase_balances(accounts),
        "notice": notice,
    }


def _exchange_kucoin(settings: Mapping[str, str], fetch: Fetch, secret: str) -> Dict[str, Any]:
    notice = _read_permission(settings, fetch, secret, _KUCOIN + "/api/v1/user/api-key")
    query = ""
    cursor = settings.get("cursor") or ""
    if cursor:
        query = f"?startAt={int(cursor) + 1}"
    deposits = _signed_get(settings, fetch, secret, _KUCOIN, "/api/v1/deposits", query, "kucoin")
    withdrawals = _signed_get(
        settings, fetch, secret, _KUCOIN, "/api/v1/withdrawals", query, "kucoin"
    )
    accounts = _signed_get(settings, fetch, secret, _KUCOIN, "/api/v1/accounts", "", "kucoin")
    events = _kucoin_events(_items(deposits), "in", cursor)
    events.extend(_kucoin_events(_items(withdrawals), "out", cursor))
    latest = cursor
    for event in events:
        block = event["block"]
        if block.isdigit() and (not latest or int(block) > int(latest)):
            latest = block
    return {
        "events": events,
        "cursor": latest,
        "reported": _kucoin_balances(accounts),
        "notice": notice,
    }


def _evm(settings: Mapping[str, str], fetch: Fetch, secret: str) -> Dict[str, Any]:
    explorer = settings.get("explorer_url") or ""
    rpc = settings.get("rpc_url") or ""
    if not explorer and rpc:
        amount = _rpc_balance(fetch, rpc, settings.get("address") or "", secret)
        symbol = settings.get("symbol") or "ETH"
        return {
            "events": [],
            "cursor": settings.get("cursor") or "",
            "reported": {symbol: amount} if amount else {},
            "notice": _RPC_NOTICE,
        }
    if not explorer:
        raise DeskError("Robinhood Chain needs an explorer or RPC URL.")
    start = _start_block(settings.get("cursor") or "")
    native = _explorer_rows(settings, fetch, secret, explorer, "txlist", start)
    try:
        tokens = _explorer_rows(settings, fetch, secret, explorer, "tokentx", start)
    except DeskError:
        tokens = []
    events = _evm_events(settings, native, tokens)
    _apply_gas(events, _gas_table(settings, native))
    reported = _explorer_balance(settings, fetch, secret, explorer)
    if not reported and rpc:
        amount = _rpc_balance(fetch, rpc, settings.get("address") or "", secret)
        if amount:
            reported = {settings.get("symbol") or "ETH": amount}
    cursor = _max_block([*native, *tokens], settings.get("cursor") or "")
    return {"events": events, "cursor": cursor, "reported": reported, "notice": ""}


def _solana(settings: Mapping[str, str], fetch: Fetch, secret: str) -> Dict[str, Any]:
    url = settings.get("rpc_url") or ""
    address = settings.get("address") or ""
    options: Dict[str, Any] = {"limit": 25}
    cursor = settings.get("cursor") or ""
    if cursor:
        options["until"] = cursor
    listed = _rpc(fetch, url, "getSignaturesForAddress", [address, options], secret)
    raw_signatures = listed.get("result")
    signatures = raw_signatures if isinstance(raw_signatures, list) else []
    events = []
    newest = cursor
    for item in signatures:
        if not isinstance(item, dict):
            continue
        signature = str(item.get("signature") or "")
        if not signature or signature == cursor:
            continue
        if not newest or newest == cursor:
            newest = signature
        body = _rpc(fetch, url, "getTransaction", [signature, {"encoding": "json"}], secret)
        event = _solana_event(
            address, signature, body.get("result"), str(item.get("blockTime") or "")
        )
        if event:
            events.append(event)
    if signatures and isinstance(signatures[0], dict) and signatures[0].get("signature"):
        newest = str(signatures[0]["signature"])
    balance = _rpc(fetch, url, "getBalance", [address], secret)
    reported = {}
    value = (
        (balance.get("result") or {}).get("value")
        if isinstance(balance.get("result"), dict)
        else None
    )
    if value is not None:
        reported["SOL"] = _plain(_units(value, 9))
    return {"events": events, "cursor": newest, "reported": reported, "notice": ""}


def _bitcoin(settings: Mapping[str, str], fetch: Fetch, secret: str) -> Dict[str, Any]:
    base = (settings.get("explorer_url") or "").rstrip("/")
    address = settings.get("address") or ""
    loaded = _loaded(_call(fetch, f"{base}/address/{address}/txs", None, {}, secret), secret)
    rows = loaded if isinstance(loaded, list) else []
    if isinstance(loaded, dict):
        nested = loaded.get("txs") if isinstance(loaded.get("txs"), list) else loaded.get("result")
        if nested is None and isinstance(loaded.get("data"), list):
            nested = loaded.get("data")
        rows = nested if isinstance(nested, list) else []
    events, cursor = _bitcoin_events(address, rows, settings.get("cursor") or "")
    balance_body = _loaded(_call(fetch, f"{base}/address/{address}", None, {}, secret), secret)
    balance = balance_body if isinstance(balance_body, dict) else {}
    reported = {}
    amount = _bitcoin_balance(balance)
    if amount is not None:
        reported["BTC"] = _plain(amount)
    return {"events": events, "cursor": cursor, "reported": reported, "notice": ""}


def _read_permission(settings: Mapping[str, str], fetch: Fetch, secret: str, url: str) -> str:
    style = "kucoin" if "kucoin" in url else "coinbase"
    path = url.split(".com", 1)[-1]
    host = url[: -len(path)] if path.startswith("/") else url
    payload = _signed_get(settings, fetch, secret, host, path, "", style)
    problem = permission_problem(payload)
    if problem:
        raise DeskError(problem)
    if _explicit_readonly(payload):
        return ""
    return _READONLY_NOTICE


def _signed_get(
    settings: Mapping[str, str],
    fetch: Fetch,
    secret: str,
    host: str,
    path: str,
    query: str,
    style: str,
) -> Dict[str, Any]:
    headers = (
        _kucoin_headers(settings, path, query)
        if style == "kucoin"
        else _coinbase_headers(settings, path)
    )
    return _json(_call(fetch, host + path + query, None, headers, secret), secret)


def _coinbase_headers(settings: Mapping[str, str], path: str) -> Dict[str, str]:
    stamp = str(int(datetime.now(timezone.utc).timestamp()))
    secret = settings.get("api_secret") or ""
    return {
        "CB-ACCESS-KEY": settings.get("api_key") or "",
        "CB-ACCESS-SIGN": _hmac(secret, f"{stamp}GET{path}"),
        "CB-ACCESS-TIMESTAMP": stamp,
    }


def _kucoin_headers(settings: Mapping[str, str], path: str, query: str) -> Dict[str, str]:
    stamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    secret = settings.get("api_secret") or ""
    phrase = settings.get("passphrase") or ""
    return {
        "KC-API-KEY": settings.get("api_key") or "",
        "KC-API-SIGN": _hmac(secret, f"{stamp}GET{path}{query}"),
        "KC-API-TIMESTAMP": stamp,
        "KC-API-PASSPHRASE": _hmac(secret, phrase),
        "KC-API-KEY-VERSION": "2",
    }


def _explorer_rows(
    settings: Mapping[str, str],
    fetch: Fetch,
    secret: str,
    explorer: str,
    action: str,
    start: str,
) -> List[Mapping[str, Any]]:
    params = {
        "chainid": settings.get("chain_id") or "",
        "module": "account",
        "action": action,
        "address": settings.get("address") or "",
        "startblock": start,
        "endblock": "99999999",
        "sort": "asc",
    }
    key = settings.get("api_key") or ""
    if key:
        params["apikey"] = key
    payload = _json(_call(fetch, f"{explorer}?{urlencode(params)}", None, {}, secret), secret)
    result = payload.get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    message = str(result or payload.get("message") or "")
    if "no transactions" in message.casefold():
        return []
    if action == "tokentx":
        raise DeskError(_scrub(message or "No token transactions returned.", secret))
    if message and "ok" not in message.casefold():
        raise DeskError(_scrub(message, secret))
    return []


def _explorer_balance(
    settings: Mapping[str, str], fetch: Fetch, secret: str, explorer: str
) -> Dict[str, str]:
    params = {
        "chainid": settings.get("chain_id") or "",
        "module": "account",
        "action": "balance",
        "address": settings.get("address") or "",
        "tag": "latest",
    }
    key = settings.get("api_key") or ""
    if key:
        params["apikey"] = key
    try:
        payload = _json(_call(fetch, f"{explorer}?{urlencode(params)}", None, {}, secret), secret)
    except DeskError:
        return {}
    amount = _wei_amount(payload.get("result"), 18)
    if not amount:
        return {}
    return {settings.get("symbol") or "ETH": amount}


def _evm_events(
    settings: Mapping[str, str],
    native: Sequence[Mapping[str, Any]],
    tokens: Sequence[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    own = (settings.get("address") or "").lower()
    chain = settings.get("chain") or ""
    symbol = settings.get("symbol") or "ETH"
    events: List[Dict[str, str]] = []
    for row in native:
        if str(row.get("isError", "0")) == "1":
            continue
        amount = _units(row.get("value"), 18)
        if amount == 0:
            continue
        events.extend(_native_legs(own, chain, symbol, row, amount))
    for row in tokens:
        if str(row.get("isError", "0")) == "1" or not row.get("tokenSymbol"):
            continue
        decimals = int(str(row.get("tokenDecimal") or "18") or "18")
        amount = _units(row.get("value"), decimals)
        if amount == 0:
            continue
        events.extend(_token_legs(own, chain, row, amount))
    return events


def _native_legs(
    own: str, chain: str, symbol: str, row: Mapping[str, Any], amount: Decimal
) -> List[Dict[str, str]]:
    return _direction_legs(
        own,
        chain,
        symbol,
        row,
        amount,
        str(row.get("contractAddress") or ""),
        str(row.get("functionName") or row.get("methodId") or ""),
    )


def _token_legs(
    own: str, chain: str, row: Mapping[str, Any], amount: Decimal
) -> List[Dict[str, str]]:
    return _direction_legs(
        own,
        chain,
        str(row.get("tokenSymbol") or ""),
        row,
        amount,
        str(row.get("contractAddress") or ""),
        str(row.get("functionName") or ""),
    )


def _direction_legs(
    own: str,
    chain: str,
    asset: str,
    row: Mapping[str, Any],
    amount: Decimal,
    contract: str,
    method: str,
) -> List[Dict[str, str]]:
    sender = str(row.get("from") or "").lower() == own
    receiver = str(row.get("to") or "").lower() == own
    if not sender and not receiver:
        return []
    found = []
    if sender:
        found.append(
            _transfer(
                chain,
                row,
                asset,
                "out",
                amount,
                str(row.get("to") or ""),
                contract,
                method,
            )
        )
    if receiver and not sender:
        found.append(
            _transfer(
                chain,
                row,
                asset,
                "in",
                amount,
                str(row.get("from") or ""),
                contract,
                method,
            )
        )
    elif receiver and sender:
        found.append(
            _transfer(
                chain,
                row,
                asset,
                "in",
                amount,
                str(row.get("from") or ""),
                contract,
                method,
            )
        )
    return found


def _transfer(
    chain: str,
    row: Mapping[str, Any],
    asset: str,
    direction: str,
    amount: Decimal,
    counterparty: str,
    contract: str,
    method: str,
) -> Dict[str, str]:
    return {
        "shape": "transfer",
        "chain": chain,
        "tx_hash": str(row.get("hash") or ""),
        "asset": asset,
        "direction": direction,
        "quantity": _plain(amount),
        "buy_asset": "",
        "buy_quantity": "",
        "sell_asset": "",
        "sell_quantity": "",
        "fee_quantity": "",
        "fee_asset": "",
        "timestamp": _from_unix(row.get("timeStamp")),
        "block": str(row.get("blockNumber") or ""),
        "counterparty": counterparty,
        "contract": contract,
        "method": method,
    }


def _gas_table(
    settings: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> Dict[str, Tuple[str, str]]:
    own = (settings.get("address") or "").lower()
    symbol = settings.get("symbol") or "ETH"
    found: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        if str(row.get("isError", "0")) == "1":
            continue
        if str(row.get("from") or "").lower() != own:
            continue
        spent = _units(_gas_wei(row), 18)
        digest = str(row.get("hash") or "")
        if digest and spent > 0:
            found[digest] = (_plain(spent), symbol)
    return found


def _apply_gas(events: List[Dict[str, str]], gas: Mapping[str, Tuple[str, str]]) -> None:
    used = set()
    for event in events:
        digest = event["tx_hash"]
        if event["direction"] != "out" or digest in used or digest not in gas:
            continue
        event["fee_quantity"], event["fee_asset"] = gas[digest]
        used.add(digest)


def _solana_event(
    address: str, signature: str, result: Any, block_time: str
) -> Optional[Dict[str, str]]:
    if not isinstance(result, dict):
        return None
    message = (result.get("transaction") or {}).get("message") or {}
    keys = [_account_key(item) for item in message.get("accountKeys") or []]
    if address not in keys:
        return None
    index = keys.index(address)
    meta = result.get("meta") or {}
    before = _at(meta.get("preBalances"), index)
    after = _at(meta.get("postBalances"), index)
    delta = after - before
    if delta == 0:
        return None
    fee = Decimal(str(meta.get("fee") or "0"))
    paid = index == 0 and fee > 0 and delta < 0
    quantity = -delta - fee if paid else abs(delta)
    if quantity <= 0:
        return None
    when = _from_unix(result.get("blockTime") or block_time)
    event = _blank_transfer(
        "solana", signature, "SOL", "out" if delta < 0 else "in", quantity, when
    )
    if paid:
        event["fee_quantity"] = _plain(_units(fee, 9))
        event["fee_asset"] = "SOL"
    return event


def _bitcoin_events(
    address: str, rows: Sequence[Any], cursor: str
) -> Tuple[List[Dict[str, str]], str]:
    minimum = int(cursor or "0")
    highest = minimum
    events: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_status = row.get("status")
        status = raw_status if isinstance(raw_status, dict) else {}
        height = int(str(status.get("block_height") or row.get("block_height") or "0") or "0")
        if cursor and height <= minimum:
            continue
        highest = max(highest, height)
        event = _bitcoin_event(address, row, height)
        if event:
            events.append(event)
    return events, str(highest if highest else cursor)


def _bitcoin_event(address: str, row: Mapping[str, Any], height: int) -> Optional[Dict[str, str]]:
    inputs = _satoshis(row.get("vin"), address, "prevout")
    outputs = _satoshis(row.get("vout"), address, "")
    net = outputs - inputs
    if net == 0:
        return None
    fee = Decimal(str(row.get("fee") or "0"))
    raw_status = row.get("status")
    status = raw_status if isinstance(raw_status, dict) else {}
    when = _from_unix(status.get("block_time") or row.get("block_time"))
    digest = str(row.get("txid") or row.get("hash") or "")
    if net > 0:
        return _blank_transfer("bitcoin", digest, "BTC", "in", _units(net, 8), when, str(height))
    principal = -net - fee if inputs > 0 and -net >= fee else -net
    if principal <= 0:
        return None
    event = _blank_transfer(
        "bitcoin", digest, "BTC", "out", _units(principal, 8), when, str(height)
    )
    if inputs > 0 and fee > 0:
        event["fee_quantity"] = _plain(_units(fee, 8))
        event["fee_asset"] = "BTC"
    return event


def _satoshis(rows: Any, address: str, nested: str) -> Decimal:
    total = Decimal(0)
    if not isinstance(rows, list):
        return total
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get(nested) if nested else row
        if not isinstance(source, dict):
            continue
        if str(source.get("scriptpubkey_address") or "") != address:
            continue
        total += Decimal(str(source.get("value") or "0"))
    return total


def _bitcoin_balance(payload: Mapping[str, Any]) -> Optional[Decimal]:
    stats = payload.get("chain_stats") if isinstance(payload.get("chain_stats"), dict) else None
    if stats is None and isinstance(payload.get("result"), dict):
        stats = payload["result"].get("chain_stats")
    if not isinstance(stats, dict):
        return None
    funded = Decimal(str(stats.get("funded_txo_sum") or "0"))
    spent = Decimal(str(stats.get("spent_txo_sum") or "0"))
    return _units(funded - spent, 8)


def _coinbase_fill(item: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    product = str(item.get("product_id") or "")
    base, _dash, quote = product.partition("-")
    if not base or not quote:
        return None
    try:
        size = Decimal(str(item.get("size") or "0"))
        price = Decimal(str(item.get("price") or "0"))
    except InvalidOperation:
        return None
    if size == 0:
        return None
    quote_qty = _plain(size * price)
    side = str(item.get("side") or "BUY").upper()
    when = str(item.get("trade_time") or "").replace("T", " ").replace("Z", "")[:19]
    fee = str(item.get("commission") or "")
    digest = str(item.get("trade_id") or item.get("entry_id") or "")
    event = _blank_transfer("", digest, base, "in", size, when)
    event["shape"] = "trade"
    event["direction"] = "trade"
    if side == "SELL":
        event["sell_asset"] = base
        event["sell_quantity"] = _plain(size)
        event["buy_asset"] = quote
        event["buy_quantity"] = quote_qty
        event["asset"] = base
        event["quantity"] = _plain(size)
    else:
        event["buy_asset"] = base
        event["buy_quantity"] = _plain(size)
        event["sell_asset"] = quote
        event["sell_quantity"] = quote_qty
    if fee and fee != "0":
        event["fee_quantity"] = fee
        event["fee_asset"] = quote
    return event


def _coinbase_balances(payload: Mapping[str, Any]) -> Dict[str, str]:
    raw_rows = payload.get("accounts")
    rows = raw_rows if isinstance(raw_rows, list) else []
    found: Dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        currency = str(row.get("currency") or "")
        balance = row.get("available_balance")
        if isinstance(balance, dict):
            raw = balance.get("value")
        else:
            raw = balance
        _add_balance(found, currency, raw)
    return {asset: _plain(amount) for asset, amount in found.items() if amount != 0}


def _kucoin_events(
    items: Sequence[Mapping[str, Any]], direction: str, cursor: str
) -> List[Dict[str, str]]:
    events = []
    floor = int(cursor) if cursor else 0
    for item in items:
        created = str(item.get("createdAt") or item.get("created_at") or "")
        if cursor and created.isdigit() and int(created) <= floor:
            continue
        try:
            amount = Decimal(str(item.get("amount") or "0"))
        except InvalidOperation:
            continue
        if amount == 0:
            continue
        asset = str(item.get("currency") or "")
        when = _from_millis(created) if created.isdigit() else str(item.get("time") or "")
        digest = str(item.get("walletTxId") or item.get("id") or "")
        event = _blank_transfer("", digest, asset, direction, amount, when, created)
        event["counterparty"] = str(item.get("address") or "")
        fee = str(item.get("fee") or "")
        if direction == "out" and fee and fee not in {"0", "0.0"}:
            event["fee_quantity"] = fee
            event["fee_asset"] = asset
        events.append(event)
    return events


def _kucoin_balances(payload: Mapping[str, Any]) -> Dict[str, str]:
    rows = payload.get("data")
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("accounts")
    if not isinstance(rows, list):
        return {}
    found: Dict[str, Decimal] = {}
    for row in rows:
        if isinstance(row, dict):
            _add_balance(
                found, str(row.get("currency") or ""), row.get("balance") or row.get("available")
            )
    return {asset: _plain(amount) for asset, amount in found.items() if amount != 0}


def _items(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    data = payload.get("data")
    rows: Any = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _blank_transfer(
    chain: str,
    digest: str,
    asset: str,
    direction: str,
    amount: Decimal,
    when: str,
    block: str = "",
) -> Dict[str, str]:
    return {
        "shape": "transfer",
        "chain": chain,
        "tx_hash": digest,
        "asset": asset,
        "direction": direction,
        "quantity": _plain(amount),
        "buy_asset": "",
        "buy_quantity": "",
        "sell_asset": "",
        "sell_quantity": "",
        "fee_quantity": "",
        "fee_asset": "",
        "timestamp": when,
        "block": block,
        "counterparty": "",
        "contract": "",
        "method": "",
    }


def _rpc(fetch: Fetch, url: str, method: str, params: List[Any], secret: str) -> Dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    payload = _json(_call(fetch, url, body, {"Content-Type": "application/json"}, secret), secret)
    if payload.get("error"):
        raise DeskError(_scrub("The node returned an error.", secret))
    return payload


def _rpc_balance(fetch: Fetch, url: str, address: str, secret: str) -> str:
    payload = _rpc(fetch, url, "eth_getBalance", [address, "latest"], secret)
    return _wei_amount(payload.get("result"), 18)


def _call(
    fetch: Fetch,
    url: str,
    data: Optional[str],
    headers: Mapping[str, str],
    secret: str,
) -> bytes:
    try:
        body = fetch(url, data, dict(headers))
    except DeskError:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise DeskError(_scrub(str(exc), secret)) from exc
    if not isinstance(body, bytes):
        raise DeskError("The response was not bytes.")
    return body


def _loaded(body: bytes, secret: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeskError(_scrub("The response was not JSON.", secret)) from exc


def _json(body: bytes, secret: str) -> Dict[str, Any]:
    loaded = _loaded(body, secret)
    if not isinstance(loaded, dict):
        raise DeskError(_scrub("The response was not a JSON object.", secret))
    return loaded


def _secret(settings: Mapping[str, str]) -> str:
    parts = [
        settings.get("api_key") or "",
        settings.get("api_secret") or "",
        settings.get("passphrase") or "",
        settings.get("explorer_url") or "",
        settings.get("rpc_url") or "",
    ]
    return " ".join(part for part in parts if part)


def _explicit_readonly(payload: Mapping[str, Any]) -> bool:
    if any(key in payload for key in ("can_trade", "can_transfer", "can_withdraw", "can_view")):
        return permission_problem(payload) == ""
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    text = str(source.get("permission") or source.get("permissions") or "")
    words = {part.strip().casefold() for part in text.replace("|", ",").split(",") if part.strip()}
    return bool(words) and words <= _READONLY_WORDS


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _hmac(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return digest.hex()


def _start_block(cursor: str) -> str:
    if not cursor:
        return "0"
    return str(int(cursor) + 1)


def _max_block(rows: Sequence[Mapping[str, Any]], cursor: str) -> str:
    highest = int(cursor or "0")
    seen = False
    for row in rows:
        try:
            highest = max(highest, int(str(row.get("blockNumber") or "0") or "0"))
            seen = True
        except ValueError:
            continue
    if not seen:
        return cursor
    return str(highest)


def _gas_wei(row: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(row.get("gasUsed") or "0")) * Decimal(str(row.get("gasPrice") or "0"))
    except InvalidOperation:
        return Decimal(0)


def _units(value: object, decimals: int) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except InvalidOperation:
        return Decimal(0)
    return amount / (Decimal(10) ** decimals)


def _wei_amount(value: object, decimals: int) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        amount = Decimal(int(value, 16)) if value.startswith("0x") else Decimal(value)
    except (InvalidOperation, ValueError):
        return ""
    return _plain(amount / (Decimal(10) ** decimals))


def _plain(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _from_unix(value: object) -> str:
    try:
        when = datetime.fromtimestamp(int(str(value or "0")), timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _from_millis(value: object) -> str:
    try:
        when = datetime.fromtimestamp(int(str(value or "0")) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _account_key(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("pubkey") or "")
    return str(item)


def _at(values: object, index: int) -> Decimal:
    if not isinstance(values, list) or index >= len(values):
        return Decimal(0)
    return Decimal(str(values[index] or "0"))


def _add_balance(found: Dict[str, Decimal], asset: str, raw: object) -> None:
    if not asset or raw in (None, ""):
        return
    try:
        found[asset] = found.get(asset, Decimal(0)) + Decimal(str(raw))
    except InvalidOperation:
        return
