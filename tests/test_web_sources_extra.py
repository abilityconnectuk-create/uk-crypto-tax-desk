# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Extra read-only sources. Every network call is a stand-in."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from bittytax.web.errors import DeskError
from bittytax.web.sources_extra import (
    import_coinbase,
    import_evm,
    import_kucoin,
    import_revolut_csv,
    import_solana,
)
from bittytax.web.store import Desk

ADDRESS = "0x2222222222222222222222222222222222222222"
SOLANA = "11111111111111111111111111111111"
KEY = "sample-explorer-key"
SECRET = "sample-secret-value"


def test_base_and_a_custom_explorer_are_read_only(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    seen: Dict[str, str] = {}

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        seen["url"] = url
        return _tx(ADDRESS)

    saved = import_evm(desk, ADDRESS, "base", KEY, fetch=fetch)
    assert "chainid=8453" in seen["url"]
    assert saved["format"] == "Generic"
    assert desk.all_rows()[0]["buy_asset"] == "ETH"
    _assert_owner_only(desk.root / "secrets" / "etherscan.key")

    def custom(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        seen["custom"] = url
        return _tx(ADDRESS, "2000000000000000000")

    import_evm(
        desk,
        ADDRESS,
        "custom",
        "",
        explorer_url="https://explorer.example/api",
        chain_id="999001",
        native_symbol="ETH",
        fetch=custom,
    )
    assert seen["custom"].startswith("https://explorer.example/api?")
    assert "chainid=999001" in seen["custom"]
    stored = json.loads((desk.root / "chains.json").read_text(encoding="utf-8"))
    assert stored["chains"][0]["chain_id"] == "999001"

    def fail(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        raise OSError(f"down {KEY} {url}")

    try:
        import_evm(desk, ADDRESS, "base", KEY, fetch=fail)
    except DeskError as exc:
        assert KEY not in str(exc)
    else:
        raise AssertionError("a failed fetch should be reported")


def test_solana_kucoin_coinbase_and_revolut_use_stand_ins(tmp_path: Path) -> None:
    desk = Desk(tmp_path)

    def solana(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del url, headers
        body = json.loads(data or "{}")
        if body["method"] == "getSignaturesForAddress":
            return json.dumps({"result": [{"signature": "sample-sig"}]}).encode("utf-8")
        return json.dumps(
            {
                "result": {
                    "blockTime": 1746000000,
                    "transaction": {"message": {"accountKeys": [SOLANA]}},
                    "meta": {"preBalances": [2000000000], "postBalances": [1000000000]},
                }
            }
        ).encode("utf-8")

    import_solana(desk, SOLANA, "https://rpc.example", solana)
    sol = next(row for row in desk.all_rows() if row["sell_asset"] == "SOL")
    assert sol["type"] == "Withdrawal"
    assert sol["sell_quantity"] == "1"

    def kucoin(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        assert url.endswith("/api/v1/deposits")
        assert headers is not None
        assert headers["KC-API-KEY"] == "sample-kucoin-key"
        assert SECRET not in url
        del data
        return json.dumps(
            {"data": {"items": [{"currency": "ETH", "amount": "0.2", "createdAt": 1746000000000}]}}
        ).encode("utf-8")

    import_kucoin(desk, "sample-kucoin-key", SECRET, "sample-phrase", kucoin)
    assert any(row["buy_asset"] == "ETH" and row["wallet"] == "KuCoin" for row in desk.all_rows())
    _assert_owner_only(desk.root / "secrets" / "kucoin.json")

    def coinbase(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        assert "/api/v3/brokerage/orders/historical/fills" in url
        assert headers is not None
        assert "sample-coinbase-key" not in url
        del data
        return json.dumps(
            {
                "fills": [
                    {
                        "product_id": "ETH-GBP",
                        "side": "BUY",
                        "size": "0.1",
                        "price": "2000",
                        "commission": "1",
                        "trade_time": "2025-05-01T12:00:00Z",
                    }
                ]
            }
        ).encode("utf-8")

    import_coinbase(desk, "sample-coinbase-key", SECRET, coinbase)
    trade = next(row for row in desk.all_rows() if row["note"] == "coinbase fill")
    assert trade["type"] == "Trade"
    assert trade["buy_asset"] == "ETH"
    _assert_owner_only(desk.root / "secrets" / "coinbase.json")

    text = (
        "Symbol,Type,Quantity,Price,Value,Fees,Date\n"
        "ETH,Receive,0.5,£2000.00,£1000.00,£0.00,2025-05-01 12:00:00\n"
    )
    import_revolut_csv(desk, "revolut.csv", text.encode("utf-8"))
    assert any(row["wallet"] == "Revolut" and row["type"] == "Deposit" for row in desk.all_rows())


def _tx(address: str, value: str = "1000000000000000000") -> bytes:
    return json.dumps(
        {
            "result": [
                {
                    "hash": "0xabc",
                    "timeStamp": "1714521600",
                    "from": "0x1111111111111111111111111111111111111111",
                    "to": address,
                    "value": value,
                    "isError": "0",
                }
            ]
        }
    ).encode("utf-8")


def _assert_owner_only(path: Path) -> None:
    assert path.is_file()
    assert os.stat(path).st_mode & 0o777 == 0o600
