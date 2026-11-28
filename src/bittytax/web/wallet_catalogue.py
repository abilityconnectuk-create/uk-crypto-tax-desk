# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The add-wallet list, chains, and the unconfirmed contract-tag suggestions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from .errors import DeskError
from .rules import config_root

_GROUPS = {
    "exchange": "Exchanges",
    "wallet": "Wallets",
    "blockchain": "Blockchains",
    "service": "Services",
}
_API = {"coinbase", "kucoin"}
# id, name, group, family
_INTEGRATIONS: Sequence[tuple] = (
    ("coinbase", "Coinbase", "exchange", ""),
    ("kucoin", "KuCoin", "exchange", ""),
    ("kraken", "Kraken", "exchange", ""),
    ("binance", "Binance", "exchange", ""),
    ("gemini", "Gemini", "exchange", ""),
    ("revolut", "Revolut X", "exchange", ""),
    ("bitstamp", "Bitstamp", "exchange", ""),
    ("cryptocom", "Crypto.com", "exchange", ""),
    ("metamask", "MetaMask", "wallet", "evm"),
    ("ledger", "Ledger", "wallet", "evm"),
    ("trezor", "Trezor", "wallet", "evm"),
    ("exodus", "Exodus", "wallet", "evm"),
    ("phantom", "Phantom", "wallet", "solana"),
    ("rabby", "Rabby", "wallet", "evm"),
    ("ethereum", "Ethereum", "blockchain", "evm"),
    ("arbitrum", "Arbitrum", "blockchain", "evm"),
    ("base", "Base", "blockchain", "evm"),
    ("optimism", "Optimism", "blockchain", "evm"),
    ("polygon", "Polygon", "blockchain", "evm"),
    ("bnb", "BNB Chain", "blockchain", "evm"),
    ("robinhood", "Robinhood Chain", "blockchain", "evm"),
    ("solana", "Solana", "blockchain", "solana"),
    ("bitcoin", "Bitcoin", "blockchain", "bitcoin"),
    ("koinly", "Koinly", "service", ""),
    ("manual", "Manual", "service", ""),
)
# id, name, family, chain id, symbol, public endpoint
_CHAINS: Sequence[tuple] = (
    ("ethereum", "Ethereum", "evm", "1", "ETH", "https://api.etherscan.io/v2/api"),
    ("arbitrum", "Arbitrum", "evm", "42161", "ETH", "https://api.etherscan.io/v2/api"),
    ("base", "Base", "evm", "8453", "ETH", "https://api.etherscan.io/v2/api"),
    ("optimism", "Optimism", "evm", "10", "ETH", "https://api.etherscan.io/v2/api"),
    ("polygon", "Polygon", "evm", "137", "POL", "https://api.etherscan.io/v2/api"),
    ("bnb", "BNB Chain", "evm", "56", "BNB", "https://api.etherscan.io/v2/api"),
    ("robinhood", "Robinhood Chain", "evm", "4663", "ETH", ""),
    ("solana", "Solana", "solana", "", "SOL", "https://api.mainnet-beta.solana.com"),
    ("bitcoin", "Bitcoin", "bitcoin", "", "BTC", "https://blockstream.info/api"),
)
_FALLBACK = {
    "confirmed": False,
    "suggestions": {
        "bridge": {"tag": "swap", "label": "Bridge", "confirmed": False},
        "morpho": {"tag": "lending_interest", "label": "Morpho", "confirmed": False},
        "lp": {"tag": "liquidity_in", "label": "Liquidity pool", "confirmed": False},
        "vault": {"tag": "pool_in", "label": "Vault", "confirmed": False},
    },
    "words": {"bridge": "bridge", "morpho": "morpho", "vault": "vault", "liquidity": "lp"},
    "contracts": [],
    "chains": {"robinhood": {"chain_id": "4663", "explorer_url": "", "rpc_url": ""}},
}


def integrations() -> List[Dict[str, str]]:
    """Searchable add-wallet list."""
    found = []
    for ident, name, group, family in _INTEGRATIONS:
        found.append(
            {
                "id": ident,
                "name": name,
                "group": group,
                "group_label": _GROUPS[group],
                "family": family,
                "api": "yes" if ident in _API else "no",
            }
        )
    return found


def find_integration(ident: str) -> Dict[str, str]:
    """One integration, or an error when the id is not in the list."""
    key = ident.strip().casefold()
    for item in integrations():
        if item["id"] == key:
            return dict(item)
    raise DeskError("Choose an integration from the list.")


def chains() -> List[Dict[str, str]]:
    """Public chains the add form can tick."""
    return [_chain_row(row) for row in _CHAINS]


def find_chain(ident: str) -> Dict[str, str]:
    """One chain."""
    key = ident.strip().casefold()
    for item in chains():
        if item["id"] == key:
            return dict(item)
    raise DeskError("Choose a chain from the list.")


def evm_ids() -> List[str]:
    """Chain ids for the Ethereum-style networks, in catalogue order."""
    return [item["id"] for item in chains() if item["family"] == "evm"]


def load_suggestions(root: Optional[Path] = None) -> Dict[str, Any]:
    """Contract suggestions. ``confirmed`` stays false unless the file says otherwise."""
    document = dict(_FALLBACK)
    shipped = _yaml(_shipped_path())
    local = _yaml(root / "wallet-sync.yaml") if root is not None else {}
    document.update({key: value for key, value in shipped.items() if key != "chains"})
    if isinstance(shipped.get("chains"), dict):
        document["chains"] = _merge_chains(document.get("chains"), shipped["chains"])
    if isinstance(local.get("chains"), dict):
        document["chains"] = _merge_chains(document.get("chains"), local["chains"])
    for key in ("suggestions", "words"):
        incoming = local.get(key)
        if isinstance(incoming, dict):
            current = document.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(incoming)
            document[key] = merged
    incoming_contracts = local.get("contracts")
    if isinstance(incoming_contracts, list):
        current_contracts = document.get("contracts")
        prior = list(current_contracts) if isinstance(current_contracts, list) else []
        document["contracts"] = prior + list(incoming_contracts)
    if "confirmed" in local:
        document["confirmed"] = local["confirmed"]
    return _normalise(document)


def suggestion_for(root: Optional[Path], contract: str, method: str) -> Dict[str, str]:
    """A suggested tag for a bridge, Morpho, pool, or vault call. Empty when none match."""
    document = load_suggestions(root)
    kind = _kind(document, contract, method)
    if not kind:
        return {}
    spec = (document.get("suggestions") or {}).get(kind) or {}
    if not isinstance(spec, dict):
        return {}
    confirmed = spec.get("confirmed")
    if confirmed is None:
        confirmed = document.get("confirmed")
    return {
        "kind": kind,
        "tag": str(spec.get("tag") or ""),
        "label": str(spec.get("label") or kind),
        "confirmed": "yes" if confirmed is True else "no",
    }


def robinhood_urls(root: Optional[Path], explorer_url: str, rpc_url: str) -> Dict[str, str]:
    """Wallet URLs win. The configuration file supplies the rest. Chain id stays 4663."""
    chain = find_chain("robinhood")
    remote = (load_suggestions(root).get("chains") or {}).get("robinhood") or {}
    if not isinstance(remote, dict):
        remote = {}
    explorer = explorer_url.strip() or str(remote.get("explorer_url") or "")
    rpc = rpc_url.strip() or str(remote.get("rpc_url") or "")
    chain_id = str(remote.get("chain_id") or chain["chain_id"] or "4663")
    return {"explorer_url": explorer.rstrip("/"), "rpc_url": rpc.rstrip("/"), "chain_id": chain_id}


def _chain_row(row: tuple) -> Dict[str, str]:
    ident, name, family, chain_id, symbol, endpoint = row
    return {
        "id": ident,
        "name": name,
        "family": family,
        "chain_id": chain_id,
        "symbol": symbol,
        "endpoint": endpoint,
    }


def _shipped_path() -> Optional[Path]:
    try:
        path = config_root() / "wallet_sync.yaml"
    except DeskError:
        return None
    return path if path.is_file() else None


def _yaml(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _merge_chains(base: Any, extra: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in extra.items():
        current = dict(merged.get(key) or {}) if isinstance(merged.get(key), dict) else {}
        if isinstance(value, dict):
            current.update(value)
            merged[key] = current
    return merged


def _normalise(document: Mapping[str, Any]) -> Dict[str, Any]:
    raw_suggestions = document.get("suggestions")
    suggestions = raw_suggestions if isinstance(raw_suggestions, dict) else {}
    cleaned: Dict[str, Any] = {}
    for kind, spec in suggestions.items():
        if not isinstance(spec, dict):
            continue
        confirmed = spec.get("confirmed")
        if confirmed is None:
            confirmed = document.get("confirmed")
        cleaned[str(kind)] = {
            "tag": str(spec.get("tag") or ""),
            "label": str(spec.get("label") or kind),
            "confirmed": confirmed is True,
        }
    contracts: Dict[str, str] = {}
    for row in document.get("contracts") or []:
        if isinstance(row, dict) and row.get("address") and row.get("kind"):
            contracts[str(row["address"]).strip().casefold()] = str(row["kind"])
    raw_words = document.get("words")
    words = raw_words if isinstance(raw_words, dict) else {}
    return {
        "confirmed": document.get("confirmed") is True,
        "suggestions": cleaned,
        "contracts": contracts,
        "words": {str(key).casefold(): str(value) for key, value in words.items()},
        "chains": document.get("chains") if isinstance(document.get("chains"), dict) else {},
    }


def _kind(document: Mapping[str, Any], contract: str, method: str) -> str:
    contracts = document.get("contracts") or {}
    direct = contracts.get(contract.strip().casefold())
    if direct:
        return str(direct)
    folded = "".join(char for char in method.casefold() if char.isalnum())
    words = document.get("words") or {}
    for needle, kind in words.items():
        if needle and needle.casefold() in folded:
            return str(kind)
    return ""
