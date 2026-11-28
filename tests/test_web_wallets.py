# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Wallets, read-only sync, and reconciliation. Every network call is a stand-in.

Sample keys, addresses and logins are made up. Nothing here contacts an exchange.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from bittytax.bt_types import TRANSFER_TYPES
from bittytax.web.errors import DeskError
from bittytax.web.filing import CATEGORIES, readiness
from bittytax.web.server import bind_server
from bittytax.web.store import Desk
from bittytax.web.wallet_fetch import REFUSAL, pull
from bittytax.web.wallet_secrets import load_secret, pack, save_secret
from bittytax.web.wallets_desk import (
    accept_row,
    add_wallets,
    delete_wallet,
    import_file,
    list_page,
    reconnect,
    sync_all,
    sync_wallet,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
EVM_A = "0x1111111111111111111111111111111111111111"
EVM_B = "0x2222222222222222222222222222222222222222"
BRIDGE = "0x00000000000000000000000000000000000000b1"
MORPHO = "0x00000000000000000000000000000000000000e1"
LP = "0x00000000000000000000000000000000000000d2"
VAULT = "0x00000000000000000000000000000000000000e2"
SOLANA = "11111111111111111111111111111111"
BITCOIN = "1SampleBitcoinAddress111111111111"
KEY = "sample-key-read-only"
SECRET = "sample-secret-value"
PHRASE = "sample-pass-phrase"
EXPLORER_KEY = "sample-explorer-key"
WHEN = str(int(datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp()))
_KUCOIN_URLS: List[str] = []
_CSRF = re.compile(r'name="csrf-token" content="([^"]*)"')
Fetch = Callable[..., bytes]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("A test tried to use the network.")

    monkeypatch.setattr("bittytax.web.wallet_secrets._keyring", lambda: None)
    monkeypatch.setattr("bittytax.web.wallet_fetch.default_fetch", _blocked)


def test_modules_keep_the_agpl_attribution() -> None:
    folder = ROOT / "src" / "bittytax" / "web"
    for name in (
        "wallet_secrets.py",
        "wallet_catalogue.py",
        "wallet_fetch.py",
        "wallets_desk.py",
        "wallet_http.py",
    ):
        text = (folder / name).read_text(encoding="utf-8")
        assert text.startswith(
            "# -*- coding: utf-8 -*-\n# Fork of BittyTax (AGPL-3.0). See LICENSE.\n"
        )
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in licence
    assert "Version 3" in licence
    assert "secrets/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_wallets_page_columns_sort_and_paging(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    for number in range(1, 12):
        add_wallets(desk, {"integration": "manual", "name": f"Wallet {number:02d}"})
    stored = json.loads((desk.root / "wallets.json").read_bytes().decode("utf-8"))
    for row in stored["wallets"]:
        day = int(str(row["name"]).rsplit(" ", 1)[-1])
        row["added_at"] = f"2025-01-{day:02d}T00:00:00Z"
    _write(desk.root / "wallets.json", stored)
    raw = (desk.root / "wallets.json").read_bytes()
    assert b"\r" not in raw
    first = list_page(desk, 1, 10)
    assert [row["name"] for row in first["rows"]] == [
        f"Wallet {number:02d}" for number in range(11, 1, -1)
    ]
    assert first["total"] == 11
    assert first["pages"] == 2
    assert first["sizes"] == [10, 25, 50, 100]
    second = list_page(desk, 2, 10)
    assert [row["name"] for row in second["rows"]] == ["Wallet 01"]
    assert len(list_page(desk, 1, 25)["rows"]) == 11
    assert list_page(desk, 1, 7)["per_page"] == 10
    page = _page(tmp_path)
    assert "Sync all" in page and ">Add<" in page
    assert "Transaction count" in page
    assert "Wallet 11" in page and "Wallet 01" not in page
    follow = _page(tmp_path, "/wallets?per=10&page=2")
    assert "Wallet 01" in follow and "Wallet 11" not in follow
    wide = _page(tmp_path, "/wallets?per=100")
    assert "Wallet 11" in wide and "Wallet 01" in wide


def test_add_flow_tabs_duplicate_name_and_three_exchange_choices(tmp_path: Path) -> None:
    listing = _page(tmp_path, "/wallets/add")
    for label in ("All", "Exchanges", "Wallets", "Blockchains", "Services"):
        assert label in listing
    assert 'id="parser-search"' in listing
    assert 'data-parser-group="service"' in listing
    coinbase = _page(tmp_path, "/wallets/add?integration=coinbase")
    assert "Read-only API (recommended)" in coinbase
    assert "File import" in coinbase
    assert "Add data later" in coinbase
    assert "READ-ONLY" in coinbase
    chain = _page(tmp_path, "/wallets/add?integration=ethereum")
    assert "Public address" in chain
    assert "Advanced options" in chain
    assert "Start import from" in chain
    assert "Tag deposits as" in chain
    assert "Ignore reported balances" in chain
    assert "chain id 4663" in chain
    assert "Arbitrum Nitro" in chain
    desk = Desk(tmp_path)
    first = add_wallets(desk, {"integration": "manual", "name": "Sample Desk"})
    assert first["ok"] == "created"
    choice = add_wallets(desk, {"integration": "manual", "name": "sample desk"})
    assert choice["ok"] == "choice"
    assert choice["existing"][0]["name"] == "Sample Desk"
    used = add_wallets(
        desk, {"integration": "manual", "name": "Sample Desk", "decision": "existing"}
    )
    assert used["ok"] == "existing"
    assert len(list_page(desk)["rows"]) == 1
    made = add_wallets(desk, {"integration": "manual", "name": "Sample Desk", "decision": "create"})
    assert made["ok"] == "created"
    assert len(list_page(desk)["rows"]) == 2
    shown = _browser(tmp_path)
    try:
        shown.get("/wallets/add?integration=manual")
        again = shown.post("/wallets/add", {"integration": "manual", "name": "Sample Desk"})
        assert "Use existing" in again and "Create anyway" in again
    finally:
        shown.stop()


def test_coinbase_and_kucoin_sync_reject_trade_permission(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    with pytest.raises(DeskError, match="trade or withdraw"):
        add_wallets(
            desk,
            _exchange("coinbase", "Coinbase", KEY, SECRET),
            fetch=_coinbase(trade=True),
        )
    assert not (desk.root / "secrets").exists()
    created = add_wallets(desk, _exchange("coinbase", "Coinbase", KEY, SECRET), fetch=_coinbase())
    assert created["ok"] == "created"
    _assert_private(desk)
    synced = sync_wallet(desk, created["wallets"][0]["id"], fetch=_coinbase())
    assert synced["added"] == 1
    assert synced["cursor"] == "cursor-sample-2"
    again = sync_wallet(desk, created["wallets"][0]["id"], fetch=_coinbase())
    assert again["added"] == 0
    staged = _review_rows(desk)
    assert staged[0]["type"] == "Trade"
    assert staged[0]["missed_by_koinly"] is True
    assert not desk.all_rows()
    ku_desk = Desk(tmp_path / "ku")
    with pytest.raises(DeskError, match="trade or withdraw"):
        add_wallets(
            ku_desk, _exchange("kucoin", "KuCoin", KEY, SECRET, PHRASE), fetch=_kucoin(trade=True)
        )
    assert not (ku_desk.root / "secrets").exists()
    _KUCOIN_URLS.clear()
    ku_wallet = add_wallets(
        ku_desk, _exchange("kucoin", "KuCoin", KEY, SECRET, PHRASE), fetch=_kucoin()
    )
    sync_wallet(ku_desk, ku_wallet["wallets"][0]["id"], fetch=_kucoin())
    sync_wallet(ku_desk, ku_wallet["wallets"][0]["id"], fetch=_kucoin())
    row = _review_rows(ku_desk)[0]
    assert row["type"] == "Deposit"
    assert row["asset"] == "ETH"
    assert Decimal(row["quantity"]) == Decimal("0.2")
    calls = list(_KUCOIN_URLS)
    assert any("startAt=" in url for url in calls)
    _assert_private(ku_desk)
    notice = add_wallets(
        Desk(tmp_path / "quiet"),
        _exchange("coinbase", "Quiet", KEY, SECRET),
        fetch=_coinbase(silent=True),
    )
    stored = json.loads((tmp_path / "quiet" / "wallets.json").read_bytes().decode("utf-8"))
    assert "read-only" in stored["wallets"][0]["notice"]
    assert notice["ok"] == "created"


def test_file_import_reuses_the_exchange_review_queue(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(desk, {"integration": "coinbase", "name": "Coinbase", "method": "file"})
    entry = import_file(desk, created["wallets"][0]["id"], "coinbase.csv", _coinbase_csv(), "local")
    assert entry["exchange"] == "Coinbase"
    assert not desk.all_rows()
    review = json.loads((desk.root / "exchanges.json").read_bytes().decode("utf-8"))
    assert review["imports"][0]["rows"][0]["cells"][0] == "Trade"
    assert list_page(desk)["rows"][0]["count"] == "1"
    assert "Last synced" in list_page(desk)["rows"][0]["status"]


def test_one_address_on_several_evm_chains_and_each_chain_id(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(
        desk,
        {
            "integration": "metamask",
            "name": "Sample",
            "address": EVM_A,
            "chains": ["ethereum", "arbitrum"],
            "api_key": EXPLORER_KEY,
        },
    )
    assert [row["name"] for row in created["wallets"]] == ["Sample - Ethereum", "Sample - Arbitrum"]
    assert {row["chain"] for row in created["wallets"]} == {"ethereum", "arbitrum"}
    seen: Dict[str, str] = {}

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        seen[url] = url
        return _explorer(url, EVM_A, EVM_B)

    for wallet in created["wallets"]:
        sync_wallet(desk, wallet["id"], fetch=fetch)
    assert any("chainid=1" in url and "action=txlist" in url for url in seen)
    assert any("chainid=42161" in url and "action=txlist" in url for url in seen)
    assert all(EXPLORER_KEY not in text for text in _public_text(desk))
    chain_ids = {
        "ethereum": "1",
        "arbitrum": "42161",
        "base": "8453",
        "optimism": "10",
        "polygon": "137",
        "bnb": "56",
    }
    for chain, chain_id in chain_ids.items():
        holder = Desk(tmp_path / chain)
        made = add_wallets(holder, {"integration": chain, "name": chain, "address": EVM_A})
        urls: List[str] = []

        def grab(  # pylint: disable=dangerous-default-value
            url: str,
            data: Optional[str] = None,
            headers: Optional[Dict[str, str]] = None,
            found: List[str] = urls,
        ) -> bytes:
            del data, headers
            found.append(url)
            return _explorer(url, EVM_A, EVM_B)

        sync_wallet(holder, made["wallets"][0]["id"], fetch=grab)
        assert any(f"chainid={chain_id}" in url for url in urls)


def test_robinhood_solana_and_bitcoin_use_public_apis(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    with pytest.raises(DeskError, match="explorer or RPC"):
        add_wallets(desk, {"integration": "robinhood", "name": "Robinhood", "address": EVM_A})
    rpc_only = add_wallets(
        desk,
        {
            "integration": "robinhood",
            "name": "Robinhood RPC",
            "address": EVM_A,
            "rpc_url": "https://rpc.example.test",
        },
    )
    rpc_calls: List[str] = []

    def rpc_fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del headers
        rpc_calls.append(url + " " + (data or ""))
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0xde0b6b3a7640000"}).encode(
            "utf-8"
        )

    result = sync_wallet(desk, rpc_only["wallets"][0]["id"], fetch=rpc_fetch)
    assert result["added"] == 0
    assert "eth_getBalance" in rpc_calls[0]
    assert "https://rpc.example.test" in rpc_calls[0]
    stored = json.loads((desk.root / "wallets.json").read_bytes().decode("utf-8"))
    robin = next(row for row in stored["wallets"] if row["name"] == "Robinhood RPC")
    assert "explorer URL" in robin["notice"]
    assert robin["reported"]["ETH"] == "1"
    both = add_wallets(
        desk,
        {
            "integration": "robinhood",
            "name": "Robinhood Explorer",
            "address": EVM_A,
            "explorer_url": "https://explorer.example.test/api",
            "rpc_url": "https://rpc.example.test",
        },
    )
    explorer_urls: List[str] = []

    def explorer_fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        explorer_urls.append(url)
        if "explorer.example.test" in url:
            return _explorer(url, EVM_A, EVM_B)
        raise AssertionError(url)

    sync_wallet(desk, both["wallets"][0]["id"], fetch=explorer_fetch)
    assert any("explorer.example.test" in url and "chainid=4663" in url for url in explorer_urls)
    solana = add_wallets(
        desk,
        {
            "integration": "solana",
            "name": "Solana",
            "address": SOLANA,
            "rpc_url": "https://solana.example.test",
        },
    )
    solana_calls: List[str] = []

    def solana_fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del headers
        solana_calls.append(data or url)
        if data and "getSignaturesForAddress" in data:
            return json.dumps(
                {"result": [{"signature": "sample-sig", "blockTime": int(WHEN)}]}
            ).encode("utf-8")
        if data and "getTransaction" in data:
            return json.dumps({"result": _solana_tx()}).encode("utf-8")
        return json.dumps({"result": {"value": 2000000000}}).encode("utf-8")

    sync_wallet(desk, solana["wallets"][0]["id"], fetch=solana_fetch)
    assert any(
        "https://solana.example.test" in call or "getSignaturesForAddress" in call
        for call in solana_calls
    )
    assert _review_rows(desk, "Solana")[0]["asset"] == "SOL"
    bitcoin = add_wallets(desk, {"integration": "bitcoin", "name": "Bitcoin", "address": BITCOIN})
    bitcoin_urls: List[str] = []

    def bitcoin_fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        bitcoin_urls.append(url)
        if url.endswith("/txs"):
            return json.dumps([_bitcoin_tx()]).encode("utf-8")
        return json.dumps(
            {"chain_stats": {"funded_txo_sum": 200000000, "spent_txo_sum": 50000000}}
        ).encode("utf-8")

    sync_wallet(desk, bitcoin["wallets"][0]["id"], fetch=bitcoin_fetch)
    assert any(url.startswith("https://blockstream.info/api/address/") for url in bitcoin_urls)
    row = _review_rows(desk, "Bitcoin")[0]
    assert row["asset"] == "BTC"
    assert row["direction"] == "out"
    assert Decimal(row["fee_quantity"]) == Decimal("0.0001")


def test_secrets_stay_private_and_a_keychain_is_used_when_supplied(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(desk, _exchange("coinbase", "Coinbase", KEY, SECRET), fetch=_coinbase())
    wallet_id = created["wallets"][0]["id"]
    path = desk.root / "secrets" / "wallets" / f"{wallet_id}.json"
    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"\r" not in path.read_bytes()
    loaded = load_secret(desk.root, wallet_id)
    assert loaded["api_key"] == KEY
    _assert_private(desk)
    page = _page(tmp_path)
    assert KEY not in page and SECRET not in page
    with pytest.raises(DeskError, match="redacted"):
        sync_wallet(desk, wallet_id, fetch=_raises(f"rejected {KEY}"))
    stored = json.loads((desk.root / "wallets.json").read_bytes().decode("utf-8"))
    assert KEY not in stored["wallets"][0]["last_error"]
    assert "redacted" in stored["wallets"][0]["last_error"]
    box = _Memory()
    other = tmp_path / "chain"
    other.mkdir()
    assert save_secret(other, wallet_id, pack(KEY, SECRET), keychain=box) == "keychain"
    assert not (other / "secrets").exists()
    assert load_secret(other, wallet_id, keychain=box)["api_secret"] == SECRET
    reconnect(
        desk, wallet_id, {"api_key": "sample-key-replaced", "api_secret": SECRET}, fetch=_coinbase()
    )
    assert load_secret(desk.root, wallet_id)["api_key"] == "sample-key-replaced"
    assert KEY not in path.read_text(encoding="utf-8")
    shown = _page(tmp_path, f"/wallets/reconnect?wallet={wallet_id}")
    assert "READ-ONLY" in shown
    assert "sample-key-replaced" not in shown


def test_synced_rows_wait_for_review_and_missed_by_koinly_is_deduped(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes(), "local")
    matched = [row for row in desk.all_rows() if row.get("tx_hash") == "sample-tx-eth-reward"]
    assert matched and matched[0]["wallet"] == "Example Exchange"
    created = add_wallets(
        desk, {"integration": "ethereum", "name": "Example Exchange", "address": EVM_A}
    )
    wallet_id = created["wallets"][0]["id"]
    sync_wallet(
        desk,
        wallet_id,
        fetch=_hashes(EVM_B, EVM_A, ("sample-tx-eth-reward", "1"), ("sample-missed-eth", "0.4")),
    )
    rows = _review_rows(desk)
    assert [row["tx_hash"] for row in rows] == ["sample-missed-eth"]
    assert rows[0]["missed_by_koinly"] is True
    assert "Missed by Koinly" in list_page(desk)["missed"][0]["description"]
    assert not any(row.get("tx_hash") == "sample-missed-eth" for row in desk.all_rows())
    ledger_id = accept_row(desk, rows[0]["id"])
    accepted = next(row for row in desk.all_rows() if row["id"] == ledger_id)
    assert accepted["tx_hash"] == "sample-missed-eth"
    assert accepted["chain"] == "ethereum"
    assert "Missed by Koinly" in accepted["note"]
    sync_wallet(desk, wallet_id, fetch=_hashes(EVM_B, EVM_A, ("sample-missed-eth", "0.4")))
    assert _review_rows(desk) == []
    kept = json.loads((desk.root / "wallet-review.json").read_bytes().decode("utf-8"))
    assert len(kept["rows"]) == 1 and kept["rows"][0]["accepted"] is True


def test_gas_is_a_fee_on_the_outgoing_row(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(desk, {"integration": "ethereum", "name": "Gas", "address": EVM_A})
    sync_wallet(
        desk,
        created["wallets"][0]["id"],
        fetch=_evm_fetch(EVM_A, EVM_B, value="1000000000000000000"),
    )
    rows = _review_rows(desk)
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert Decimal(rows[0]["quantity"]) == Decimal("1")
    assert Decimal(rows[0]["fee_quantity"]) == Decimal("0.000021")
    assert rows[0]["fee_asset"] == "ETH"
    ledger_id = accept_row(desk, rows[0]["id"])
    saved = next(row for row in desk.all_rows() if row["id"] == ledger_id)
    assert saved["type"] == "Withdrawal"
    assert Decimal(saved["fee_quantity"]) == Decimal("0.000021")
    assert saved["sell_asset"] == "ETH"


def test_own_wallet_transfer_is_not_a_disposal(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    sender = add_wallets(desk, {"integration": "ethereum", "name": "Left", "address": EVM_A})
    add_wallets(
        desk, {"integration": "ethereum", "name": "Right", "address": EVM_B, "chains": ["base"]}
    )
    sync_wallet(
        desk,
        sender["wallets"][0]["id"],
        fetch=_evm_fetch(EVM_A, EVM_B, value="1000000000000000000"),
    )
    row = _review_rows(desk, "Left")[0]
    assert row["type"] in {item.value for item in TRANSFER_TYPES}
    assert row["note"] == "Own-wallet transfer"
    assert row["type"] != "Trade"
    ledger_id = accept_row(desk, row["id"])
    saved = next(item for item in desk.all_rows() if item["id"] == ledger_id)
    assert saved["type"] in {item.value for item in TRANSFER_TYPES}


def test_bridge_morpho_lp_and_vault_stay_unconfirmed(tmp_path: Path) -> None:
    _force_confirmed(tmp_path)
    desk = Desk(tmp_path)
    created = add_wallets(desk, {"integration": "ethereum", "name": "Contracts", "address": EVM_A})
    sync_wallet(desk, created["wallets"][0]["id"], fetch=_contract_fetch())
    rows = {row["note"].split()[2].rstrip("."): row for row in _review_rows(desk)}
    assert rows["Bridge"]["type"] == "_Needs-Review"
    assert rows["Bridge"]["suggested_tag"] == "swap"
    assert rows["Morpho"]["suggested_tag"] == "lending_interest"
    assert rows["Liquidity"]["suggested_tag"] == "liquidity_in"
    assert rows["Vault"]["suggested_tag"] == "pool_in"
    for row in rows.values():
        assert row["confirmed"] is False
        assert "No tax treatment is applied." in row["note"]
    ledger_id = accept_row(desk, rows["Bridge"]["id"])
    saved = next(item for item in desk.all_rows() if item["id"] == ledger_id)
    assert saved["type"] == "_Needs-Review"
    assert saved["tag"] == "swap"
    meta = json.loads((desk.root / "tx-meta.json").read_bytes().decode("utf-8"))
    assert meta["rows"][ledger_id]["tag"] == "swap"
    assert meta["rows"][ledger_id].get("confirmed") != "yes"


def test_wallet_actions_incremental_sync_and_soft_delete(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(desk, {"integration": "ethereum", "name": "Moving", "address": EVM_A})
    wallet_id = created["wallets"][0]["id"]
    assert list_page(desk)["rows"][0]["status"] == "Import file"
    first = sync_wallet(desk, wallet_id, fetch=_evm_fetch(EVM_A, EVM_B, block="5"))
    assert first["cursor"] == "5"
    assert "Last synced" in list_page(desk)["rows"][0]["status"]
    second_urls: List[str] = []

    def second(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        second_urls.append(url)
        return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")

    again = sync_wallet(desk, wallet_id, fetch=second)
    assert again["added"] == 0
    assert any("startblock=6" in url for url in second_urls)
    assert sync_all(desk, fetch=second)["synced"] == 1
    with pytest.raises(DeskError, match="Confirm"):
        delete_wallet(desk, wallet_id, "")
    assert list_page(desk)["rows"]
    delete_wallet(desk, wallet_id, "yes")
    assert not list_page(desk)["rows"]
    assert not (desk.root / "secrets" / "wallets" / f"{wallet_id}.json").exists()
    reused = add_wallets(desk, {"integration": "ethereum", "name": "Moving", "address": EVM_A})
    assert reused["ok"] == "created"
    shown = _browser(tmp_path)
    try:
        page = shown.get("/wallets")
        assert "Moving" in page
        confirm = shown.get(f"/wallets/delete?wallet={reused['wallets'][0]['id']}")
        assert "Delete wallet" in confirm
        assert "Keep this wallet" in confirm
        after = shown.post(
            "/wallets/delete", {"wallet_id": reused["wallets"][0]["id"], "confirm": "yes"}
        )
        assert "No wallets yet" in after
        assert f'id="wallet-{reused["wallets"][0]["id"]}"' not in after
    finally:
        shown.stop()


def test_advanced_options_filter_deposits_and_ignore_reported_balances(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(
        desk,
        {
            "integration": "ethereum",
            "name": "Filtered",
            "address": EVM_A,
            "start_date": "2025-10-01",
            "deposit_tag": "airdrop",
            "ignore_reported": True,
        },
    )
    wallet_id = created["wallets"][0]["id"]
    sync_wallet(desk, wallet_id, fetch=_incoming(EVM_B, EVM_A, "2"))
    assert _review_rows(desk) == []
    stored = json.loads((desk.root / "wallets.json").read_bytes().decode("utf-8"))
    assert stored["wallets"][0]["cursor"] == "5"
    assert stored["wallets"][0]["reported"] == {}
    later = add_wallets(
        desk,
        {
            "integration": "base",
            "name": "Tagged",
            "address": EVM_A,
            "deposit_tag": "airdrop",
            "ignore_reported": True,
        },
    )
    sync_wallet(desk, later["wallets"][0]["id"], fetch=_incoming(EVM_B, EVM_A, "2"))
    row = _review_rows(desk, "Tagged")[0]
    assert row["type"] == "Deposit"
    assert row["suggested_tag"] == "airdrop"
    assert row["confirmed"] is False
    assert "Not confirmed." in row["note"]
    assert readiness(desk, 2026)
    assert not any(
        item["category"] == "balance_difference" for item in readiness(desk, 2026)["items"]
    )


def test_balance_differences_reach_ready_to_file(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    created = add_wallets(desk, {"integration": "ethereum", "name": "Counted", "address": EVM_A})
    sync_wallet(desk, created["wallets"][0]["id"], fetch=_incoming(EVM_B, EVM_A, "1", reported="2"))
    warning = list_page(desk)["rows"][0]["warning"]
    assert "Computed 0 ETH" in warning
    assert "reported 2 ETH" in warning
    board = readiness(desk, 2026)
    assert [row["key"] for row in board["counts"]] == [key for key, _label in CATEGORIES]
    assert next(row["count"] for row in board["counts"] if row["key"] == "balance_difference") == 1
    assert any(
        item["category"] == "balance_difference" and "Counted" in item["name"]
        for item in board["items"]
    )
    page = _page(tmp_path, "/ready?tax_year=2026")
    assert "Balance differences" in page
    assert "Counted ETH" in page


def test_pull_reports_balances_without_writing_a_secret(tmp_path: Path) -> None:
    result = pull(
        {
            "kind": "coinbase",
            "cursor": "",
            "api_key": KEY,
            "api_secret": SECRET,
            "passphrase": "",
            "chain": "",
            "chain_id": "",
            "symbol": "",
            "address": "",
            "explorer_url": "",
            "rpc_url": "",
        },
        fetch=_coinbase(),
    )
    assert result["reported"]["ETH"] == "1.5"
    assert result["events"][0]["fee_asset"] == "GBP"
    assert not list(tmp_path.rglob("*.json"))


class _Memory:
    def __init__(self) -> None:
        self.saved: Dict[str, str] = {}

    def set(self, ident: str, value: str) -> None:
        self.saved[ident] = value

    def get(self, ident: str) -> str:
        return self.saved.get(ident, "")

    def delete(self, ident: str) -> None:
        self.saved.pop(ident, None)


class _Browser:
    def __init__(self, root: Path) -> None:
        self.httpd: ThreadingHTTPServer = bind_server(root, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        port = int(self.httpd.server_address[1])
        self.base = f"http://127.0.0.1:{port}"
        self.cookie = ""
        self.csrf = ""

    def get(self, path: str) -> str:
        return self._open(Request(self.base + path))

    def post(self, path: str, fields: Dict[str, str]) -> str:
        payload = dict(fields)
        payload["csrf"] = self.csrf
        request = Request(self.base + path, data=urlencode(payload).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._open(request)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _open(self, request: Request) -> str:
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.headers.get("Set-Cookie")
                if raw:
                    self.cookie = str(raw).split(";", 1)[0].strip()
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{exc.code} {detail}") from exc
        found = _CSRF.search(body)
        if found:
            self.csrf = found.group(1)
        return body


def _browser(root: Path) -> _Browser:
    browser = _Browser(root)
    browser.get("/wallets")
    return browser


def _page(root: Path, path: str = "/wallets") -> str:
    browser = _browser(root)
    try:
        return browser.get(path)
    finally:
        browser.stop()


def _exchange(
    integration: str, name: str, key: str, secret: str, phrase: str = ""
) -> Dict[str, str]:
    return {
        "integration": integration,
        "name": name,
        "method": "api",
        "api_key": key,
        "api_secret": secret,
        "passphrase": phrase,
    }


def _coinbase(trade: bool = False, silent: bool = False) -> Fetch:
    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        if "key_permissions" in url:
            if silent:
                return json.dumps({}).encode("utf-8")
            return json.dumps({"can_view": True, "can_trade": trade, "can_transfer": False}).encode(
                "utf-8"
            )
        if "historical/fills" in url:
            fills = [] if "cursor=" in url else [_fill()]
            return json.dumps({"fills": fills, "cursor": "cursor-sample-2"}).encode("utf-8")
        if url.endswith("/accounts"):
            body = {"accounts": [{"currency": "ETH", "available_balance": {"value": "1.5"}}]}
            return json.dumps(body).encode("utf-8")
        raise AssertionError(url)

    return fetch


def _kucoin(trade: bool = False) -> Fetch:
    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        _KUCOIN_URLS.append(url)
        if url.endswith("/api-key"):
            permission = "General,Trade" if trade else "General"
            return json.dumps({"data": {"permission": permission}}).encode("utf-8")
        if url.split("?", 1)[0].endswith("/deposits"):
            if "startAt=" in url:
                return json.dumps({"data": {"items": []}}).encode("utf-8")
            item = {
                "currency": "ETH",
                "amount": "0.2",
                "createdAt": 1756713600000,
                "walletTxId": "sample-ku-1",
                "fee": "0",
            }
            return json.dumps({"data": {"items": [item]}}).encode("utf-8")
        if "withdrawals" in url:
            return json.dumps({"data": {"items": []}}).encode("utf-8")
        if url.endswith("/accounts"):
            return json.dumps({"data": [{"currency": "ETH", "balance": "0.2"}]}).encode("utf-8")
        raise AssertionError(url)

    return fetch


def _fill() -> Dict[str, str]:
    return {
        "product_id": "ETH-GBP",
        "side": "BUY",
        "size": "0.5",
        "price": "2000",
        "commission": "1",
        "trade_id": "sample-trade-1",
        "trade_time": "2025-09-01T12:00:00Z",
    }


def _raises(message: str) -> Fetch:
    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del url, data, headers
        raise RuntimeError(message)

    return fetch


def _explorer(url: str, sender: str, receiver: str) -> bytes:
    if "action=txlist" in url:
        start = "0"
        if "startblock=" in url:
            start = url.split("startblock=", 1)[1].split("&", 1)[0]
        rows = [] if start != "0" else [_tx(sender, receiver, "1000000000000000000")]
        return json.dumps({"message": "OK", "result": rows}).encode("utf-8")
    if "action=tokentx" in url:
        return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")
    if "action=balance" in url:
        return json.dumps({"message": "OK", "result": "2000000000000000000"}).encode("utf-8")
    raise AssertionError(url)


def _evm_fetch(
    sender: str, receiver: str, value: str = "1000000000000000000", block: str = "5"
) -> Fetch:
    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        if "action=txlist" in url:
            row = _tx(sender, receiver, value, block)
            return json.dumps({"message": "OK", "result": [row]}).encode("utf-8")
        if "action=tokentx" in url:
            return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")
        return json.dumps({"message": "OK", "result": "0"}).encode("utf-8")

    return fetch


def _incoming(sender: str, receiver: str, whole: str, reported: str = "2") -> Fetch:
    wei = str(int(Decimal(whole) * Decimal(10) ** 18))
    reported_wei = str(int(Decimal(reported) * Decimal(10) ** 18))

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        if "action=txlist" in url:
            return json.dumps({"message": "OK", "result": [_tx(sender, receiver, wei)]}).encode(
                "utf-8"
            )
        if "action=tokentx" in url:
            return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")
        return json.dumps({"message": "OK", "result": reported_wei}).encode("utf-8")

    return fetch


def _hashes(sender: str, receiver: str, *pairs: tuple) -> Fetch:
    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        if "action=txlist" in url:
            rows = []
            for index, pair in enumerate(pairs, start=1):
                digest, whole = pair
                wei = str(int(Decimal(whole) * Decimal(10) ** 18))
                row = _tx(sender, receiver, wei, str(index), digest)
                rows.append(row)
            return json.dumps({"message": "OK", "result": rows}).encode("utf-8")
        if "action=tokentx" in url:
            return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")
        return json.dumps({"message": "OK", "result": "0"}).encode("utf-8")

    return fetch


def _contract_fetch() -> Fetch:
    targets = (
        (BRIDGE, "Bridge"),
        (MORPHO, "Morpho"),
        (LP, "Liquidity pool"),
        (VAULT, "Vault"),
    )

    def fetch(
        url: str, data: Optional[str] = None, headers: Optional[Dict[str, str]] = None
    ) -> bytes:
        del data, headers
        if "action=txlist" in url:
            rows = []
            for index, (target, label) in enumerate(targets, start=1):
                row = _tx(
                    EVM_A, target, "1000000000000000000", str(index), f"sample-contract-{index}"
                )
                row["functionName"] = label
                rows.append(row)
            return json.dumps({"message": "OK", "result": rows}).encode("utf-8")
        if "action=tokentx" in url:
            return json.dumps({"message": "No transactions found", "result": []}).encode("utf-8")
        return json.dumps({"message": "OK", "result": "0"}).encode("utf-8")

    return fetch


def _tx(
    sender: str, receiver: str, value: str, block: str = "5", digest: str = ""
) -> Dict[str, str]:
    return {
        "hash": digest or "0xabc0000000000000000000000000000000000000000000000000000000000001",
        "blockNumber": block,
        "from": sender,
        "to": receiver,
        "value": value,
        "gasUsed": "21000",
        "gasPrice": "1000000000",
        "isError": "0",
        "timeStamp": WHEN,
        "functionName": "",
        "contractAddress": "",
    }


def _solana_tx() -> Dict[str, Any]:
    return {
        "blockTime": int(WHEN),
        "transaction": {"message": {"accountKeys": [SOLANA, "sample-other-account"]}},
        "meta": {"fee": 5000, "preBalances": [3000000000, 0], "postBalances": [1000000000, 0]},
    }


def _bitcoin_tx() -> Dict[str, Any]:
    return {
        "txid": "sample-btc-tx",
        "fee": 10000,
        "status": {"block_height": 9, "block_time": int(WHEN)},
        "vin": [{"prevout": {"scriptpubkey_address": BITCOIN, "value": 15000000}}],
        "vout": [{"scriptpubkey_address": "1OtherSampleAddress1111111111111", "value": 4000000}],
    }


def _coinbase_csv() -> bytes:
    header = [
        "ID",
        "Timestamp",
        "Transaction Type",
        "Asset",
        "Quantity Transacted",
        "Price Currency",
        "Price at Transaction",
        "Subtotal",
        "Total (inclusive of fees and/or spread)",
        "Fees and/or Spread",
        "Notes",
    ]
    row = [
        "sample-row",
        "2025-09-01 12:00:00 UTC",
        "Buy",
        "ETH",
        "0.5",
        "GBP",
        "2000.00",
        "1000.00",
        "1001.00",
        "1.00",
        "Bought 0.5 ETH for 1000.00 GBP",
    ]
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(
        [["Transactions"], ["User", "Sample Person"], header, row]
    )
    return buffer.getvalue().encode("utf-8")


def _review_rows(desk: Desk, wallet: str = "") -> List[Dict[str, Any]]:
    path = desk.root / "wallet-review.json"
    if not path.is_file():
        return []
    loaded = json.loads(path.read_bytes().decode("utf-8"))
    rows = [row for row in loaded["rows"] if not row.get("accepted")]
    if wallet:
        rows = [row for row in rows if row.get("wallet") == wallet]
    return rows


def _public_text(desk: Desk) -> List[str]:
    found = []
    for path in desk.root.rglob("*"):
        if not path.is_file() or "secrets" in path.parts:
            continue
        found.append(path.read_bytes().decode("utf-8", errors="replace"))
    return found


def _assert_private(desk: Desk) -> None:
    blob = "\n".join(_public_text(desk))
    for secret in (KEY, SECRET, PHRASE, EXPLORER_KEY):
        assert secret not in blob
    assert REFUSAL not in blob or "sample-key" not in blob


def _force_confirmed(root: Path) -> None:
    text = (
        "confirmed: true\n"
        "suggestions:\n"
        "  bridge:\n"
        "    tag: swap\n"
        "    label: Bridge\n"
        "    confirmed: true\n"
        "  morpho:\n"
        "    tag: lending_interest\n"
        "    label: Morpho\n"
        "    confirmed: true\n"
        "  lp:\n"
        "    tag: liquidity_in\n"
        "    label: Liquidity pool\n"
        "    confirmed: true\n"
        "  vault:\n"
        "    tag: pool_in\n"
        "    label: Vault\n"
        "    confirmed: true\n"
    )
    (root / "wallet-sync.yaml").write_bytes(text.encode("utf-8"))


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.write_bytes((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
