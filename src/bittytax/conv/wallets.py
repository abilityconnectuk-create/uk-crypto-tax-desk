# -*- coding: utf-8 -*-

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml


class WalletRegistryError(ValueError):
    """The wallet registry file is missing or not in the expected shape."""


@dataclass
class RegistryEntry:
    label: str
    address: str = ""
    account: str = ""
    chain: str = ""
    description: str = ""
    wallet: str = ""
    asset: str = ""


@dataclass
class TokenGroup:
    """Symbols that may be one holding. The first symbol is the shared-pool name."""

    symbols: Tuple[str, ...]

    def canonical(self) -> str:
        return self.symbols[0]


@dataclass
class MatchSettings:
    window: timedelta = timedelta(hours=24)
    amount_tolerance: Decimal = Decimal("0.02")
    amount_tolerance_abs: Decimal = Decimal(0)
    token_groups: List[TokenGroup] = field(default_factory=list)
    share_pools: bool = False


@dataclass
class WalletRegistry:
    own: List[RegistryEntry] = field(default_factory=list)
    payees: List[RegistryEntry] = field(default_factory=list)
    match: MatchSettings = field(default_factory=MatchSettings)


_TOP_LEVEL = {"own", "payees", "match", "token_groups"}
_ENTRY_KEYS = {"label", "address", "account", "chain", "description", "wallet", "asset"}
_MATCH_KEYS = {"window_hours", "amount_tolerance", "amount_tolerance_abs", "share_pools"}


def load_wallet_registry(path: str) -> WalletRegistry:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except OSError as exc:
        raise WalletRegistryError(f"Cannot read wallet registry {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WalletRegistryError(f"Wallet registry {path} is not valid YAML: {exc}") from exc

    if loaded is None:
        return WalletRegistry()
    if not isinstance(loaded, dict):
        raise WalletRegistryError(f"Wallet registry {path} must be a mapping")

    unknown = set(loaded) - _TOP_LEVEL
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"Wallet registry has unknown keys: {names}")

    registry = WalletRegistry(
        own=_load_entries(loaded.get("own"), "own"),
        payees=_load_entries(loaded.get("payees"), "payees"),
        match=_load_match(loaded.get("match")),
    )
    if "token_groups" in loaded:
        registry.match.token_groups = _load_token_groups(loaded.get("token_groups"))
    return registry


def override_match_settings(
    settings: MatchSettings,
    window_hours: Optional[str] = None,
    amount_tolerance: Optional[str] = None,
) -> None:
    if window_hours is not None:
        settings.window = _window_from_hours(window_hours)
    if amount_tolerance is not None:
        settings.amount_tolerance = _parse_tolerance(amount_tolerance, "amount_tolerance")


def find_entry(
    entries: Sequence[RegistryEntry], *candidates: str, chain: str = ""
) -> Optional[RegistryEntry]:
    for candidate in candidates:
        if not candidate or not candidate.strip():
            continue
        for entry in entries:
            if not _chain_ok(entry, chain):
                continue
            if _entry_matches(entry, candidate):
                return entry
    return None


def _chain_ok(entry: RegistryEntry, chain: str) -> bool:
    if not entry.chain or not chain:
        return True
    return entry.chain.casefold() == chain.casefold()


def _entry_matches(entry: RegistryEntry, candidate: str) -> bool:
    if entry.address and _norm_address(entry.address) == _norm_address(candidate):
        return True
    if entry.account and entry.account.strip().casefold() == candidate.strip().casefold():
        return True
    return False


def _norm_address(value: str) -> str:
    return "".join(value.split()).casefold()


def _load_entries(value: object, name: str) -> List[RegistryEntry]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError(f"{name} must be a list")
    return [_load_entry(item, f"{name}[{index}]") for index, item in enumerate(value, start=1)]


def _load_entry(value: object, where: str) -> RegistryEntry:
    item = _require_mapping(value, where)
    unknown = set(item) - _ENTRY_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"{where} has unknown keys: {names}")

    address = _optional_str(item, "address", where)
    account = _optional_str(item, "account", where)
    description = _optional_str(item, "description", where)
    wallet = _optional_str(item, "wallet", where)
    asset = _optional_str(item, "asset", where).upper()
    if wallet and not asset and not (address or account or description):
        raise WalletRegistryError(f"{where} wallet rule needs an asset")
    if not (address or account or description or (wallet and asset)):
        raise WalletRegistryError(
            f"{where} needs an address, an account name, a description, or a wallet and asset"
        )

    label = _optional_str(item, "label", where) or address or account or description or wallet
    return RegistryEntry(
        label=label,
        address=address,
        account=account,
        chain=_optional_str(item, "chain", where),
        description=description,
        wallet=wallet,
        asset=asset,
    )


def _load_match(value: object) -> MatchSettings:
    if value is None:
        return MatchSettings()
    item = _require_mapping(value, "match")
    unknown = set(item) - _MATCH_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"match has unknown keys: {names}")

    settings = MatchSettings()
    if "window_hours" in item:
        settings.window = _window_from_hours(item["window_hours"])
    if "amount_tolerance" in item:
        settings.amount_tolerance = _parse_tolerance(item["amount_tolerance"], "amount_tolerance")
    if "amount_tolerance_abs" in item:
        settings.amount_tolerance_abs = _parse_tolerance(
            item["amount_tolerance_abs"], "amount_tolerance_abs"
        )
    if "share_pools" in item:
        settings.share_pools = _parse_bool(item["share_pools"], "share_pools")
    return settings


def _load_token_groups(value: object) -> List[TokenGroup]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError("token_groups must be a list of symbol lists")

    groups: List[TokenGroup] = []
    for index, group in enumerate(value, start=1):
        if not isinstance(group, list) or not group:
            raise WalletRegistryError(f"token_groups[{index}] must be a non-empty list of symbols")
        symbols = []
        for symbol in group:
            if not isinstance(symbol, str) or not symbol.strip():
                raise WalletRegistryError(f"token_groups[{index}] symbols must be strings")
            symbols.append(symbol.strip().upper())
        groups.append(TokenGroup(symbols=tuple(symbols)))
    return groups


def _require_mapping(value: object, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise WalletRegistryError(f"{name} must be a mapping")
    return value


def _optional_str(item: Dict[str, Any], key: str, where: str) -> str:
    if key not in item or item[key] is None:
        return ""
    value = item[key]
    if not isinstance(value, str):
        raise WalletRegistryError(f"{where}.{key} must be a string")
    return value.strip()


def _parse_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in ("true", "yes", "1"):
            return True
        if folded in ("false", "no", "0"):
            return False
    raise WalletRegistryError(f"{name} must be true or false")


def _parse_decimal(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise WalletRegistryError(f"{name} is not a number: {value}") from exc


def _parse_tolerance(value: object, name: str) -> Decimal:
    decimal = _parse_decimal(value, name)
    if decimal < 0:
        raise WalletRegistryError(f"{name} cannot be negative")
    return decimal


def _window_from_hours(value: object) -> timedelta:
    hours = _parse_decimal(value, "window_hours")
    if hours < 0:
        raise WalletRegistryError("window_hours cannot be negative")
    return timedelta(seconds=int(hours * Decimal(3600)))
