# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Read-only imports. Keys stay in the data directory and are not written to a log."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from time import time
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.request import Request, urlopen

from .errors import DeskError
from .store import Desk

Fetch = Callable[..., bytes]
_WEI = Decimal(10) ** 18
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ETHERSCAN = "https://api.etherscan.io/v2/api"
_KRAKEN = "https://api.kraken.com/0/private/Ledgers"
_KRAKEN_PATH = "/0/private/Ledgers"
_HEADER = [
    "Txhash",
    "Blockno",
    "UnixTimestamp",
    "DateTime (UTC)",
    "From",
    "To",
    "ContractAddress",
    "Value_IN(ETH)",
    "Value_OUT(ETH)",
    "CurrentValue",
    "TxnFee(ETH)",
    "TxnFee(USD)",
    "Historical $Price/ETH",
    "Status",
    "ErrCode",
    "Method",
]


def import_ethereum(
    desk: Desk, address: str, api_key: str = "", fetch: Optional[Fetch] = None
) -> Dict[str, Any]:
    """Download normal transactions for one address and import the explorer CSV."""
    chosen = address.strip()
    if not _ADDRESS.match(chosen):
        raise DeskError("Enter one Ethereum address.")
    key = api_key.strip() or _read_secret(desk, "etherscan.key")
    if not key:
        raise DeskError("Enter an explorer API key. It is stored only in the data directory.")
    if api_key.strip():
        _write_private(desk.root / "secrets" / "etherscan.key", key)
    url = (
        f"{_ETHERSCAN}?chainid=1&module=account&action=txlist&address={chosen}"
        f"&startblock=0&endblock=99999999&sort=asc&apikey={key}"
    )
    payload = _get_json(url, key, fetch)
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise DeskError(_scrub(str(payload.get("message", "No transactions returned.")), key))
    if not rows:
        raise DeskError("The explorer returned no transactions for that address.")
    saved = desk.import_bytes(f"etherscan-{chosen[-6:]}.csv", _etherscan_csv(chosen, rows))
    _note(desk, "ethereum", len(rows))
    return saved


def import_kraken(
    desk: Desk, api_key: str = "", api_secret: str = "", fetch: Optional[Fetch] = None
) -> Dict[str, Any]:
    """Download ledger entries with a read-only key and import them as a Kraken CSV."""
    stored = _read_kraken(desk)
    key = api_key.strip() or stored.get("key", "")
    secret = api_secret.strip() or stored.get("secret", "")
    if not key or not secret:
        raise DeskError("Enter the Kraken API key and secret. They stay in the data directory.")
    if api_key.strip() or api_secret.strip():
        _write_private(
            desk.root / "secrets" / "kraken.json",
            json.dumps({"key": key, "secret": secret}),
        )
    nonce = str(_nonce())
    body = f"nonce={nonce}"
    headers = {
        "API-Key": key,
        "API-Sign": kraken_sign(_KRAKEN_PATH, nonce, body, secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = _post_json(_KRAKEN, body, headers, f"{key} {secret}", fetch)
    errors = payload.get("error") or []
    if errors:
        raise DeskError(_scrub(" ".join(str(item) for item in errors), f"{key} {secret}"))
    ledger = (payload.get("result") or {}).get("ledger") or {}
    if not isinstance(ledger, dict) or not ledger:
        raise DeskError("The exchange returned no ledger entries.")
    saved = desk.import_bytes("kraken-ledgers.csv", _kraken_csv(ledger))
    _note(desk, "kraken", len(ledger))
    return saved


def kraken_sign(path: str, nonce: str, body: str, secret: str) -> str:
    """Kraken's HMAC-SHA512 of the path plus SHA256(nonce + POST body)."""
    digest = hashlib.sha256(f"{nonce}{body}".encode("utf-8")).digest()
    mac = hmac.new(base64.b64decode(secret), path.encode("utf-8") + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode("ascii")


def secrets_present(desk: Desk) -> Dict[str, bool]:
    root = desk.root / "secrets"
    return {
        "etherscan": (root / "etherscan.key").is_file(),
        "kraken": (root / "kraken.json").is_file(),
    }


def _etherscan_csv(address: str, rows: List[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)
    own = address.lower()
    for row in rows:
        writer.writerow(_etherscan_line(own, row))
    return buffer.getvalue().encode("utf-8")


def _etherscan_line(own: str, row: Mapping[str, Any]) -> List[str]:
    value = _eth(row.get("value", "0"))
    incoming = str(row.get("to", "")).lower() == own
    failed = str(row.get("isError", "0")) == "1" or str(row.get("txreceipt_status", "1")) == "0"
    spent = Decimal(str(row.get("gasUsed", "0"))) * Decimal(str(row.get("gasPrice", "0")))
    fee = "0" if incoming else _eth(spent)
    stamp = str(row.get("timeStamp", "0"))
    return [
        str(row.get("hash", "")),
        str(row.get("blockNumber", "")),
        stamp,
        stamp,
        str(row.get("from", "")),
        str(row.get("to", "")),
        str(row.get("contractAddress", "")),
        value if incoming else "0",
        "0" if incoming else value,
        "",
        fee,
        "",
        "",
        "Error" if failed else "",
        "",
        str(row.get("functionName", "") or row.get("methodId", "")),
    ]


def _kraken_csv(ledger: Mapping[str, Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "txid",
            "refid",
            "time",
            "type",
            "subtype",
            "aclass",
            "subclass",
            "asset",
            "wallet",
            "amount",
            "fee",
            "balance",
        ]
    )
    for txid, row in ledger.items():
        writer.writerow(
            [
                txid,
                str(row.get("refid", "")),
                str(row.get("time", "")),
                str(row.get("type", "")),
                str(row.get("subtype", "")),
                str(row.get("aclass", "currency")),
                str(row.get("subclass", "")),
                str(row.get("asset", "")),
                str(row.get("wallet", "")),
                str(row.get("amount", "")),
                str(row.get("fee", "0")),
                str(row.get("balance", "")),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _eth(value: object) -> str:
    amount = Decimal(str(value or "0")) / _WEI
    return f"{amount.normalize():f}"


def _get_json(url: str, secret: str, fetch: Optional[Fetch]) -> Dict[str, Any]:
    caller = _fetch_urllib if fetch is None else fetch
    try:
        body = caller(url)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise DeskError(_scrub(str(exc), secret)) from exc
    return _decode(body, secret)


def _post_json(
    url: str, body: str, headers: Mapping[str, str], secret: str, fetch: Optional[Fetch]
) -> Dict[str, Any]:
    caller = _fetch_urllib if fetch is None else fetch
    try:
        downloaded = caller(url, body, dict(headers))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise DeskError(_scrub(str(exc), secret)) from exc
    return _decode(downloaded, secret)


def _decode(body: bytes, secret: str) -> Dict[str, Any]:
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeskError("The response was not JSON.") from exc
    if not isinstance(loaded, dict):
        raise DeskError(_scrub("The response was not a JSON object.", secret))
    return loaded


def _fetch_urllib(
    url: str, data: Optional[str] = None, headers: Optional[Mapping[str, str]] = None
) -> bytes:
    payload = None if data is None else data.encode("utf-8")
    request = Request(url, data=payload, headers=dict(headers or {}))
    with urlopen(request, timeout=30) as response:
        return bytes(response.read())


def _scrub(text: str, secret: str) -> str:
    cleaned = text
    for part in secret.split():
        if part:
            cleaned = cleaned.replace(part, "redacted")
    return cleaned


def _read_secret(desk: Desk, name: str) -> str:
    path = desk.root / "secrets" / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_kraken(desk: Desk) -> Dict[str, str]:
    path = desk.root / "secrets" / "kraken.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {"key": str(loaded.get("key", "")), "secret": str(loaded.get("secret", ""))}


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _note(desk: Desk, kind: str, rows: int) -> None:
    path = desk.root / "sync-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "rows": rows}) + "\n")


def _nonce() -> int:
    return int(time() * 1000)
