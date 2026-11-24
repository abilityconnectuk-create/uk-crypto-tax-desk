# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

from datetime import timedelta
from typing import Callable, List, Optional, Sequence

from .datarow import DataRow
from .pairing import (  # pylint: disable=cyclic-import
    LegIndex,
    ReconcileResult,
    TransferLeg,
    _asset,
    _bridge_evidence,
    _chain,
    _collectible_leg,
    _gap,
    _leg_value,
    _merge_exchange,
    _record,
    _tx_hash,
    _wallets_differ,
    amounts_match,
    assets_compatible,
    record_transfer_pair,
)
from .txref import hashes_equal
from .wallets import WalletRegistry


def pair_bridges(
    legs: Sequence[TransferLeg], registry: WalletRegistry, result: ReconcileResult
) -> None:
    """Closest unused in-leg. A missing chain column uses the wallet entry."""
    incoming = [leg for leg in legs if leg.direction == "in" and leg.eligible and not leg.consumed]
    index = LegIndex(incoming, registry.match)
    for out in legs:
        if out.direction != "out" or not out.eligible or out.consumed:
            continue
        if not _bridge_evidence(out.row, registry):
            continue
        chosen = _closest_bridge_in(
            out, index.time_window(out, registry.match.bridge_window), registry
        )
        if chosen is None:
            continue
        if assets_compatible(_asset(out.row, "out"), _asset(chosen.row, "in"), registry.match):
            record_transfer_pair(
                out,
                chosen,
                registry.match,
                result,
                "bridge; same token, different chain",
                "bridge-transfer",
            )
        else:
            _merge_exchange(
                out,
                chosen,
                result,
                "bridge-exchange",
                "bridge; different token treated as an exchange",
            )


def _closest_bridge_in(
    out: TransferLeg, incoming: Sequence[TransferLeg], registry: WalletRegistry
) -> Optional[TransferLeg]:
    """Closest unused in-leg. The same asset wins, and a used leg stays used."""
    same: List[TransferLeg] = []
    changed: List[TransferLeg] = []
    for leg in incoming:
        if leg.consumed or leg.row is out.row or not _bridge_candidate(out, leg, registry):
            continue
        if assets_compatible(_asset(out.row, "out"), _asset(leg.row, "in"), registry.match):
            same.append(leg)
        else:
            changed.append(leg)
    pool = same or changed
    if not pool:
        return None
    return min(pool, key=_gap_from(out))


def _gap_from(out: TransferLeg) -> Callable[[TransferLeg], timedelta]:
    def _measure(incoming: TransferLeg) -> timedelta:
        return _gap(out, incoming)

    return _measure


def _bridge_candidate(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    if not _wallets_differ(out, incoming):
        return False
    if _gap(out, incoming) > registry.match.bridge_window:
        return False
    if not _reachable(out, incoming, registry):
        return False
    same = assets_compatible(_asset(out.row, "out"), _asset(incoming.row, "in"), registry.match)
    if same:
        return _values_close(out, incoming, registry) or amounts_match(
            out.row, incoming.row, registry.match
        )
    return _token_change(out, incoming, registry)


def _token_change(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    """Both local values must be known, non-zero, and within tolerance.

    A similar quantity does not qualify, and a marked token name is not a fungible in-leg.
    """
    if _collectible_leg(incoming):
        return False
    return _values_close(out, incoming, registry)


def _values_close(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    left = _leg_value(out)
    right = _leg_value(incoming)
    if left is None or right is None or left <= 0 or right <= 0:
        return False
    settings = registry.match
    limit = max(left, right) * settings.amount_tolerance + settings.amount_tolerance_abs
    return abs(left - right) <= limit


def _reachable(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    if _chains_differ(out, incoming, registry):
        return True
    if _bridge_evidence(incoming.row, registry):
        return True
    if hashes_equal(_tx_hash(out.row), _tx_hash(incoming.row)):
        return True
    return _own_wallet(_record(incoming.row).wallet, registry)


def _chains_differ(out: TransferLeg, incoming: TransferLeg, registry: WalletRegistry) -> bool:
    first = _effective_chain(out.row, registry)
    second = _effective_chain(incoming.row, registry)
    return bool(first and second and first.casefold() != second.casefold())


def _effective_chain(row: DataRow, registry: WalletRegistry) -> str:
    direct = _chain(row)
    if direct:
        return direct
    return _wallet_chain(_record(row).wallet, registry)


def _wallet_chain(wallet: str, registry: WalletRegistry) -> str:
    folded = wallet.strip().casefold()
    if not folded:
        return ""
    for entry in registry.own:
        names = [entry.label, entry.account, entry.wallet]
        if folded in {name.strip().casefold() for name in names if name} and entry.chain:
            return entry.chain
    return ""


def _own_wallet(wallet: str, registry: WalletRegistry) -> bool:
    folded = wallet.strip().casefold()
    if not folded:
        return False
    for entry in registry.own:
        for name in (entry.label, entry.account, entry.wallet):
            if name and name.strip().casefold() == folded:
                return True
    return False
