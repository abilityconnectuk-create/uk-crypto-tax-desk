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
    tx_hash: str = ""
    on_date: str = ""
    amount: str = ""


@dataclass
class TokenGroup:
    """Symbols that may be one holding. The first symbol is the shared-pool name.

    symbols are one-to-one with each other. liquid_symbols share that pool but are
    not one-to-one (stETH under ETH). liquid=True marks every symbol as not one-to-one.
    A symbol may also sit in a separate wrap group and a separate liquid group.
    """

    symbols: Tuple[str, ...]
    liquid: bool = False
    liquid_symbols: Tuple[str, ...] = ()

    def canonical(self) -> str:
        return self.symbols[0]

    def all_symbols(self) -> Tuple[str, ...]:
        extra = tuple(symbol for symbol in self.liquid_symbols if symbol not in self.symbols)
        return self.symbols + extra


@dataclass
class MatchSettings:  # pylint: disable=too-many-instance-attributes
    window: timedelta = timedelta(hours=24)
    amount_tolerance: Decimal = Decimal("0.02")
    amount_tolerance_abs: Decimal = Decimal(0)
    token_groups: List[TokenGroup] = field(default_factory=list)
    share_pools: bool = False
    # Wider window for the fuzzy rule. The 24 hour window above is unchanged.
    fuzzy_window: timedelta = timedelta(hours=48)
    # Cap used when a wallet is a configured exchange account. Tighter than 48 hours.
    cex_window: timedelta = timedelta(hours=12)
    bridge_window: timedelta = timedelta(hours=96)
    official_assets: Tuple[str, ...] = ("USDT", "BTC", "ETH")
    blocked_symbols: Tuple[str, ...] = ()
    blocked_patterns: Tuple[str, ...] = ()


@dataclass
class AddressRule:
    label: str
    address: str
    category: str
    chain: str = ""


@dataclass
class UtilityRule:
    match: str
    treatment: str


@dataclass
class MarginRules:
    suffixes: Tuple[str, ...] = ("USDTM", "PERP")
    patterns: Tuple[str, ...] = ("realized gain", "daily total", "bot daily")


@dataclass
class TaxTreatment:
    """LP and collateral are not confirmed. The default leaves them on the queue."""

    lp: str = "review"
    collateral: str = "review"


@dataclass
class BridgeRules:
    patterns: Tuple[str, ...] = ("bridge",)
    contracts: List[AddressRule] = field(default_factory=list)


@dataclass
class CountException:
    reason: str
    wallet: str = ""
    tx_hash: str = ""


def _default_utilities() -> List[UtilityRule]:
    return [
        UtilityRule("launchpool", "income"),
        UtilityRule("launchpad", "income"),
        UtilityRule("dust conversion", "dust"),
        UtilityRule("dust", "dust"),
        UtilityRule("create-token-account", "ignore"),
        UtilityRule("create token account", "ignore"),
    ]


@dataclass
class ReconcileSettings:
    quantity_epsilon: Decimal = Decimal("0.00000001")
    cost_band_ratio: Decimal = Decimal("0.005")
    cost_band_abs: Decimal = Decimal(1)
    gains_band_ratio: Decimal = Decimal("0.01")
    income_abs: Decimal = Decimal("0.01")


@dataclass
class WalletRegistry:  # pylint: disable=too-many-instance-attributes
    own: List[RegistryEntry] = field(default_factory=list)
    payees: List[RegistryEntry] = field(default_factory=list)
    match: MatchSettings = field(default_factory=MatchSettings)
    spam_symbols: List[str] = field(default_factory=list)
    spam_patterns: List[str] = field(default_factory=list)
    venues: List[AddressRule] = field(default_factory=list)
    contracts: List[AddressRule] = field(default_factory=list)
    bridges: BridgeRules = field(default_factory=BridgeRules)
    utilities: List[UtilityRule] = field(default_factory=_default_utilities)
    margin: MarginRules = field(default_factory=MarginRules)
    tax_treatment: TaxTreatment = field(default_factory=TaxTreatment)
    reconcile: ReconcileSettings = field(default_factory=ReconcileSettings)
    count_exceptions: List[CountException] = field(default_factory=list)
    # None means the reconciliation command uses its built-in official names.
    asset_names: Optional[Dict[str, Tuple[str, ...]]] = None


_TOP_LEVEL = {
    "own",
    "payees",
    "match",
    "token_groups",
    "spam",
    "venues",
    "contracts",
    "bridges",
    "utilities",
    "margin",
    "tax_treatment",
    "official_assets",
    "reconcile",
    "count_exceptions",
    "review_exceptions",
    "asset_names",
}
_ADDRESS_CATEGORIES = {"transfer-to-venue", "swap", "lp", "collateral", "bridge"}
_UTILITY_TREATMENTS = {"income", "dust", "ignore"}
_TAX_TREATMENTS = {"review", "transfer", "disposal"}
_ENTRY_KEYS = {
    "label",
    "address",
    "account",
    "chain",
    "description",
    "wallet",
    "asset",
    "tx_hash",
    "date",
    "amount",
}
_MATCH_KEYS = {
    "window_hours",
    "amount_tolerance",
    "amount_tolerance_abs",
    "share_pools",
    "fuzzy_window_hours",
    "cex_window_hours",
    "bridge_window_hours",
}


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

    spam_symbols, spam_patterns = _load_spam(loaded.get("spam"))
    registry = WalletRegistry(
        own=_load_entries(loaded.get("own"), "own"),
        payees=_load_entries(loaded.get("payees"), "payees"),
        match=_load_match(loaded.get("match")),
        spam_symbols=spam_symbols,
        spam_patterns=spam_patterns,
    )
    if "token_groups" in loaded:
        registry.match.token_groups = _load_token_groups(loaded.get("token_groups"))
    if "official_assets" in loaded:
        registry.match.official_assets = tuple(
            symbol.upper()
            for symbol in _string_list(loaded.get("official_assets"), "official_assets")
        )
    registry.match.blocked_symbols = tuple(symbol.upper() for symbol in spam_symbols)
    registry.match.blocked_patterns = tuple(spam_patterns)
    if "venues" in loaded:
        registry.venues = _load_address_rules(loaded.get("venues"), "venues", "transfer-to-venue")
    if "contracts" in loaded:
        registry.contracts = _load_address_rules(loaded.get("contracts"), "contracts", "")
    if "bridges" in loaded:
        registry.bridges = _load_bridges(loaded.get("bridges"))
    if "utilities" in loaded:
        registry.utilities = _load_utilities(loaded.get("utilities"))
    if "margin" in loaded:
        registry.margin = _load_margin(loaded.get("margin"))
    if "tax_treatment" in loaded:
        registry.tax_treatment = _load_tax_treatment(loaded.get("tax_treatment"))
    if "reconcile" in loaded:
        registry.reconcile = _load_reconcile(loaded.get("reconcile"))
    if "count_exceptions" in loaded:
        registry.count_exceptions = _load_count_exceptions(loaded.get("count_exceptions"))
    if "asset_names" in loaded:
        registry.asset_names = _load_asset_names(loaded.get("asset_names"))
    return registry


def override_match_settings(
    settings: MatchSettings,
    window_hours: Optional[str] = None,
    amount_tolerance: Optional[str] = None,
    fuzzy_window_hours: Optional[str] = None,
    cex_window_hours: Optional[str] = None,
    bridge_window_hours: Optional[str] = None,
) -> None:
    if window_hours is not None:
        settings.window = _window_from_hours(window_hours, "window_hours")
    if amount_tolerance is not None:
        settings.amount_tolerance = _parse_tolerance(amount_tolerance, "amount_tolerance")
    if fuzzy_window_hours is not None:
        settings.fuzzy_window = _window_from_hours(fuzzy_window_hours, "fuzzy_window_hours")
    if cex_window_hours is not None:
        settings.cex_window = _window_from_hours(cex_window_hours, "cex_window_hours")
    if bridge_window_hours is not None:
        settings.bridge_window = _window_from_hours(bridge_window_hours, "bridge_window_hours")


def wallet_is_cex(wallet: str, entries: Sequence[RegistryEntry]) -> bool:
    """True when the wallet name is a configured exchange account, not a chain address."""
    folded = wallet.strip().casefold()
    if not folded:
        return False
    for entry in entries:
        if not entry.account:
            continue
        names = [entry.account, entry.label]
        if folded in {name.strip().casefold() for name in names if name}:
            return True
    return False


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
    tx_hash = _optional_str(item, "tx_hash", where)
    on_date = _optional_str(item, "date", where)
    amount = _optional_str(item, "amount", where)
    identified = bool(
        address
        or account
        or description
        or tx_hash
        or (wallet and asset)
        or (on_date and asset and amount)
    )
    if wallet and not asset and not identified:
        raise WalletRegistryError(f"{where} wallet rule needs an asset")
    if not identified:
        raise WalletRegistryError(
            f"{where} needs an address, account, description, tx hash, "
            "wallet and asset, or date, asset and amount"
        )

    label = (
        _optional_str(item, "label", where)
        or address
        or account
        or description
        or tx_hash
        or wallet
    )
    return RegistryEntry(
        label=label,
        address=address,
        account=account,
        chain=_optional_str(item, "chain", where),
        description=description,
        wallet=wallet,
        asset=asset,
        tx_hash=tx_hash,
        on_date=on_date,
        amount=amount,
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
    if "fuzzy_window_hours" in item:
        settings.fuzzy_window = _window_from_hours(item["fuzzy_window_hours"], "fuzzy_window_hours")
    if "cex_window_hours" in item:
        settings.cex_window = _window_from_hours(item["cex_window_hours"], "cex_window_hours")
    if "bridge_window_hours" in item:
        settings.bridge_window = _window_from_hours(
            item["bridge_window_hours"], "bridge_window_hours"
        )
    return settings


def _load_token_groups(value: object) -> List[TokenGroup]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError("token_groups must be a list of symbol lists")

    groups: List[TokenGroup] = []
    for index, group in enumerate(value, start=1):
        groups.append(_load_one_group(group, index))
    return groups


def _load_one_group(group: object, index: int) -> TokenGroup:
    if isinstance(group, list):
        return TokenGroup(symbols=tuple(_symbol_list(group, index)))
    item = _require_mapping(group, f"token_groups[{index}]")
    unknown = set(item) - {"symbols", "liquid", "members", "liquid_members"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"token_groups[{index}] has unknown keys: {names}")
    if "members" in item or "liquid_members" in item:
        members = _symbol_list(item.get("members") or item.get("symbols") or [], index)
        liquid_raw = item.get("liquid_members")
        liquid_members = _symbol_list(liquid_raw, index) if liquid_raw else []
        return TokenGroup(symbols=tuple(members), liquid_symbols=tuple(liquid_members))
    if "symbols" not in item:
        raise WalletRegistryError(f"token_groups[{index}] needs symbols")
    liquid = False
    if "liquid" in item:
        liquid = _parse_bool(item["liquid"], f"token_groups[{index}].liquid")
    return TokenGroup(symbols=tuple(_symbol_list(item["symbols"], index)), liquid=liquid)


def _symbol_list(value: object, index: int) -> List[str]:
    if not isinstance(value, list) or not value:
        raise WalletRegistryError(f"token_groups[{index}] must be a non-empty list of symbols")
    symbols = []
    for symbol in value:
        if not isinstance(symbol, str) or not symbol.strip():
            raise WalletRegistryError(f"token_groups[{index}] symbols must be strings")
        symbols.append(symbol.strip().upper())
    return symbols


def _load_spam(value: object) -> Tuple[List[str], List[str]]:
    if value is None:
        return [], []
    item = _require_mapping(value, "spam")
    unknown = set(item) - {"symbols", "patterns"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"spam has unknown keys: {names}")
    return _string_list(item.get("symbols"), "spam.symbols"), _pattern_list(
        item.get("patterns"), "spam.patterns"
    )


def _pattern_list(value: object, name: str) -> List[str]:
    """Keep white space, including a hair space. Only a truly empty string is rejected."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError(f"{name} must be a list of strings")
    patterns = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise WalletRegistryError(f"{name} entries must be non-empty strings")
        patterns.append(item.casefold())
    return patterns


def _string_list(value: object, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError(f"{name} must be a list of strings")
    texts = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WalletRegistryError(f"{name} entries must be strings")
        texts.append(item.strip())
    return texts


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


def _window_from_hours(value: object, name: str = "window_hours") -> timedelta:
    hours = _parse_decimal(value, name)
    if hours < 0:
        raise WalletRegistryError(f"{name} cannot be negative")
    return timedelta(seconds=int(hours * Decimal(3600)))


def _load_address_rules(value: object, name: str, default_category: str) -> List[AddressRule]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError(f"{name} must be a list")
    return [
        _load_address_rule(item, f"{name}[{index}]", default_category)
        for index, item in enumerate(value, start=1)
    ]


def _load_address_rule(value: object, where: str, default_category: str) -> AddressRule:
    item = _require_mapping(value, where)
    unknown = set(item) - {"label", "address", "chain", "category"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"{where} has unknown keys: {names}")
    address = _optional_str(item, "address", where)
    if not address:
        raise WalletRegistryError(f"{where} needs an address")
    category = _optional_str(item, "category", where) or default_category
    if category not in _ADDRESS_CATEGORIES:
        raise WalletRegistryError(f"{where}.category is not a known category: {category}")
    label = _optional_str(item, "label", where) or address
    return AddressRule(
        label=label,
        address=address,
        category=category,
        chain=_optional_str(item, "chain", where),
    )


def _load_bridges(value: object) -> BridgeRules:
    item = _require_mapping(value, "bridges")
    unknown = set(item) - {"patterns", "contracts"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"bridges has unknown keys: {names}")
    patterns = tuple(_pattern_list(item.get("patterns"), "bridges.patterns")) or ("bridge",)
    contracts = (
        _load_address_rules(item.get("contracts"), "bridges.contracts", "bridge")
        if "contracts" in item
        else []
    )
    return BridgeRules(patterns=patterns, contracts=contracts)


def _load_utilities(value: object) -> List[UtilityRule]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError("utilities must be a list")
    rules = []
    for index, raw in enumerate(value, start=1):
        item = _require_mapping(raw, f"utilities[{index}]")
        unknown = set(item) - {"match", "treatment"}
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise WalletRegistryError(f"utilities[{index}] has unknown keys: {names}")
        match = _optional_str(item, "match", f"utilities[{index}]")
        treatment = _optional_str(item, "treatment", f"utilities[{index}]")
        if not match:
            raise WalletRegistryError(f"utilities[{index}] needs match")
        if treatment not in _UTILITY_TREATMENTS:
            raise WalletRegistryError(
                f"utilities[{index}].treatment must be income, dust, or ignore"
            )
        rules.append(UtilityRule(match.casefold(), treatment))
    return rules


def _load_margin(value: object) -> MarginRules:
    item = _require_mapping(value, "margin")
    unknown = set(item) - {"asset_suffixes", "patterns"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"margin has unknown keys: {names}")
    suffixes = tuple(
        symbol.upper()
        for symbol in _string_list(item.get("asset_suffixes"), "margin.asset_suffixes")
    )
    patterns = tuple(_pattern_list(item.get("patterns"), "margin.patterns"))
    if not suffixes and not patterns:
        raise WalletRegistryError("margin needs asset_suffixes or patterns")
    return MarginRules(suffixes=suffixes or ("USDTM",), patterns=patterns)


def _load_tax_treatment(value: object) -> TaxTreatment:
    item = _require_mapping(value, "tax_treatment")
    unknown = set(item) - {"lp", "collateral"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"tax_treatment has unknown keys: {names}")
    treatment = TaxTreatment()
    if "lp" in item:
        treatment.lp = _treatment_value(item["lp"], "tax_treatment.lp")
    if "collateral" in item:
        treatment.collateral = _treatment_value(item["collateral"], "tax_treatment.collateral")
    return treatment


def _treatment_value(value: object, name: str) -> str:
    if not isinstance(value, str) or value.strip() not in _TAX_TREATMENTS:
        raise WalletRegistryError(f"{name} must be review, transfer, or disposal")
    return value.strip()


def _load_reconcile(value: object) -> ReconcileSettings:
    item = _require_mapping(value, "reconcile")
    allowed = {
        "quantity_epsilon",
        "cost_band_ratio",
        "cost_band_abs",
        "gains_band_ratio",
        "income_abs",
    }
    unknown = set(item) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise WalletRegistryError(f"reconcile has unknown keys: {names}")
    settings = ReconcileSettings()
    for key in allowed:
        if key in item:
            setattr(settings, key, _parse_tolerance(item[key], f"reconcile.{key}"))
    return settings


def _load_asset_names(value: object) -> Dict[str, Tuple[str, ...]]:
    item = _require_mapping(value, "asset_names")
    names: Dict[str, Tuple[str, ...]] = {}
    for symbol, aliases in item.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise WalletRegistryError("asset_names keys must be asset symbols")
        if isinstance(aliases, str):
            alias_list: List[object] = [aliases]
        elif isinstance(aliases, list):
            alias_list = list(aliases)
        else:
            raise WalletRegistryError(f"asset_names.{symbol} must be a list of names")
        cleaned = []
        for alias in alias_list:
            if not isinstance(alias, str) or not alias.strip():
                raise WalletRegistryError(f"asset_names.{symbol} names must be text")
            cleaned.append(alias.strip())
        names[symbol.strip().upper()] = tuple(cleaned)
    return names


def _load_count_exceptions(value: object) -> List[CountException]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WalletRegistryError("count_exceptions must be a list")
    rows = []
    for index, raw in enumerate(value, start=1):
        item = _require_mapping(raw, f"count_exceptions[{index}]")
        unknown = set(item) - {"reason", "wallet", "tx_hash"}
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise WalletRegistryError(f"count_exceptions[{index}] has unknown keys: {names}")
        reason = _optional_str(item, "reason", f"count_exceptions[{index}]")
        wallet = _optional_str(item, "wallet", f"count_exceptions[{index}]")
        tx_hash = _optional_str(item, "tx_hash", f"count_exceptions[{index}]")
        if not reason or not (wallet or tx_hash):
            raise WalletRegistryError(
                f"count_exceptions[{index}] needs a reason and a wallet or tx_hash"
            )
        rows.append(CountException(reason=reason, wallet=wallet, tx_hash=tx_hash))
    return rows
