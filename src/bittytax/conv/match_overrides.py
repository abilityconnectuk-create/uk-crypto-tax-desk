# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

from typing import List, Sequence

from .pairing import (  # pylint: disable=cyclic-import
    ReconcileResult,
    TransferLeg,
    _asset,
    _log,
    _merge_exchange,
    _tx_hash,
    record_transfer_pair,
)
from .txref import normalise_tx_hash
from .wallets import ForcePair, LeaveUnmatched, WalletRegistry


def apply_match_overrides(
    legs: Sequence[TransferLeg],
    registry: WalletRegistry,
    result: ReconcileResult,
    announce: bool = True,
) -> None:
    """Hold listed hashes, then apply explicit pairs. Both run before the heuristics."""
    _hold_back(legs, registry.match_overrides.leave_unmatched, result, announce)
    _force_pairs(legs, registry, result, announce)


def _hold_back(
    legs: Sequence[TransferLeg],
    rules: Sequence[LeaveUnmatched],
    result: ReconcileResult,
    announce: bool,
) -> None:
    if not rules:
        return
    for leg in legs:
        if leg.consumed or not leg.eligible or not _held(leg, rules):
            continue
        leg.held = True
        if announce:
            _log(result, "leave-unmatched", leg.row, leg.direction, "match override; not paired")


def _held(leg: TransferLeg, rules: Sequence[LeaveUnmatched]) -> bool:
    digest = normalise_tx_hash(_tx_hash(leg.row))
    if not digest:
        return False
    symbol = _asset(leg.row, leg.direction).strip().upper()
    for rule in rules:
        if normalise_tx_hash(rule.tx_hash) != digest:
            continue
        if not rule.asset or rule.asset.upper() == symbol:
            return True
    return False


def _force_pairs(
    legs: Sequence[TransferLeg],
    registry: WalletRegistry,
    result: ReconcileResult,
    announce: bool,
) -> None:
    for rule in registry.match_overrides.force_pair:
        outs = _hash_legs(legs, rule.out_hash, "out", rule.out_asset)
        incoming = _hash_legs(legs, rule.in_hash, "in", rule.in_asset)
        if len(outs) == 1 and len(incoming) == 1:
            _apply_force(outs[0], incoming[0], rule, registry, result)
            continue
        if announce and (len(outs) > 1 or len(incoming) > 1):
            _note_ambiguous(outs, incoming, result)


def _note_ambiguous(
    outs: Sequence[TransferLeg], incoming: Sequence[TransferLeg], result: ReconcileResult
) -> None:
    leg = outs[0] if outs else incoming[0]
    _log(
        result,
        "override-ambiguous",
        leg.row,
        leg.direction,
        "match override; hash does not match exactly one leg",
    )


def _apply_force(
    out: TransferLeg,
    incoming: TransferLeg,
    rule: ForcePair,
    registry: WalletRegistry,
    result: ReconcileResult,
) -> None:
    if rule.kind == "transfer":
        record_transfer_pair(
            out,
            incoming,
            registry.match,
            result,
            "match override; forced transfer",
            "override-transfer",
        )
    else:
        _merge_exchange(
            out,
            incoming,
            result,
            "override-exchange",
            "match override; forced exchange",
        )
    result.pinned.add(id(out.row))
    result.pinned.add(id(incoming.row))


def _hash_legs(
    legs: Sequence[TransferLeg], tx_hash: str, direction: str, asset: str = ""
) -> List[TransferLeg]:
    digest = normalise_tx_hash(tx_hash)
    if not digest:
        return []
    wanted = asset.strip().upper()
    found: List[TransferLeg] = []
    for leg in legs:
        if leg.direction != direction or not leg.eligible or leg.consumed or leg.held:
            continue
        if normalise_tx_hash(_tx_hash(leg.row)) != digest:
            continue
        if wanted and _asset(leg.row, direction).strip().upper() != wanted:
            continue
        found.append(leg)
    return found
