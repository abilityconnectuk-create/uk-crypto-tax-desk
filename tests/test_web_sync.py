# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Read-only sync. The network is a stand-in, and the key never appears in an error."""

from __future__ import annotations

import base64
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import urlopen

from bittytax.web.errors import DeskError
from bittytax.web.server import bind_server
from bittytax.web.store import Desk
from bittytax.web.sync import import_ethereum, import_kraken

ADDRESS = "0x2222222222222222222222222222222222222222"
EXPLORER = "sample-explorer-key"
KRAKEN_KEY = "sample-kraken-key"
KRAKEN_SECRET = base64.b64encode(b"sample-secret-value").decode("ascii")


def test_ethereum_import_uses_the_supplied_fetch_and_hides_the_key(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    seen: Dict[str, str] = {}

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        seen["url"] = url
        return json.dumps(
            {
                "status": "1",
                "result": [
                    {
                        "hash": "0xabc",
                        "blockNumber": "1",
                        "timeStamp": "1714521600",
                        "from": "0x1111111111111111111111111111111111111111",
                        "to": ADDRESS,
                        "contractAddress": "",
                        "value": "1000000000000000000",
                        "gasUsed": "21000",
                        "gasPrice": "1000000000",
                        "isError": "0",
                        "txreceipt_status": "1",
                        "functionName": "transfer",
                    }
                ],
            }
        ).encode("utf-8")

    saved = import_ethereum(desk, ADDRESS, EXPLORER, fetch)
    assert saved["row_count"] == 1
    row = desk.all_rows()[0]
    assert row["buy_asset"] == "ETH"
    assert row["buy_quantity"] == "1"
    assert EXPLORER in seen["url"]
    key_path = desk.root / "secrets" / "etherscan.key"
    _assert_owner_only(key_path)
    assert EXPLORER not in (desk.root / "sync-log.jsonl").read_text(encoding="utf-8")

    def fail(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        raise OSError(url)

    try:
        import_ethereum(desk, ADDRESS, EXPLORER, fail)
    except DeskError as exc:
        assert EXPLORER not in str(exc)
    else:
        raise AssertionError("a failed fetch should be reported")


def test_kraken_import_reads_a_ledger_without_the_network(tmp_path: Path) -> None:
    desk = Desk(tmp_path)

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        assert url.endswith("/0/private/Ledgers")
        assert headers is not None
        assert headers["API-Key"] == KRAKEN_KEY
        assert data is not None and data.startswith("nonce=")
        assert KRAKEN_SECRET not in (data or "")
        return json.dumps(
            {
                "error": [],
                "result": {
                    "ledger": {
                        "L1": {
                            "refid": "R1",
                            "time": "2024-05-01 12:00:00",
                            "type": "deposit",
                            "subtype": "",
                            "aclass": "currency",
                            "asset": "XXBT",
                            "amount": "0.1000",
                            "fee": "0.0000",
                            "balance": "0.1000",
                        }
                    }
                },
            }
        ).encode("utf-8")

    saved = import_kraken(desk, KRAKEN_KEY, KRAKEN_SECRET, fetch)
    assert saved["row_count"] == 1
    assert desk.all_rows()[0]["buy_asset"] == "BTC"
    secret_path = desk.root / "secrets" / "kraken.json"
    _assert_owner_only(secret_path)
    stored = secret_path.read_text(encoding="utf-8")
    assert KRAKEN_KEY in stored
    log = (desk.root / "sync-log.jsonl").read_text(encoding="utf-8")
    assert KRAKEN_KEY not in log
    assert KRAKEN_SECRET not in log

    def fail(url: str, data: Any = None, headers: Any = None) -> bytes:
        del url, data, headers
        raise OSError(f"rejected {KRAKEN_KEY} {KRAKEN_SECRET}")

    try:
        import_kraken(desk, KRAKEN_KEY, KRAKEN_SECRET, fail)
    except DeskError as exc:
        assert KRAKEN_KEY not in str(exc)
        assert KRAKEN_SECRET not in str(exc)
    else:
        raise AssertionError("a failed fetch should be reported")


def test_sync_page_does_not_repeat_a_stored_key(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    (desk.root / "secrets").mkdir()
    key_path = desk.root / "secrets" / "etherscan.key"
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(EXPLORER)
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/sync", timeout=30) as response:
            page = response.read().decode()
    finally:
        httpd.shutdown()
    assert "A key is already stored." in page
    assert EXPLORER not in page


def _assert_owner_only(path: Path) -> None:
    assert path.is_file()
    if os.name != "posix":
        return
    assert path.stat().st_mode & 0o777 == 0o600
