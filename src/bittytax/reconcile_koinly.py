# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Compare a Koinly export pack with BittyTax records and a 5 April snapshot."""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import dateutil.parser

from .config import config
from .conv.allocations import pending_allocations
from .conv.wallets import load_wallet_registry
from .review_queue import (
    QueueItem,
    ReviewException,
    load_review_exceptions,
    review_queue_failures,
)
from .year_end import tax_year_label, tax_year_of


@dataclass
class HoldingSet:
    as_of: str
    tax_year: str
    amounts: Dict[str, Decimal]
    costs: Dict[str, Decimal]
    tax_year_label: str = ""


@dataclass
class KoinlyPack:
    wallets: Dict[str, int] = field(default_factory=dict)
    types: Dict[str, int] = field(default_factory=dict)
    holdings: List[HoldingSet] = field(default_factory=list)
    income: Dict[str, Decimal] = field(default_factory=dict)
    other: Dict[str, Decimal] = field(default_factory=dict)
    gains: Dict[str, Decimal] = field(default_factory=dict)
    missing: Dict[str, Decimal] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class Desk:
    share_pools: bool = False
    token_groups: List[Dict[str, List[str]]] = field(default_factory=list)
    official_assets: List[str] = field(default_factory=lambda: ["USDT", "BTC", "ETH"])
    count_exceptions: List[Dict[str, str]] = field(default_factory=list)
    quantity_epsilon: Decimal = Decimal("0.00000001")
    cost_band_ratio: Decimal = Decimal("0.005")
    cost_band_abs: Decimal = Decimal(1)
    gains_band_ratio: Decimal = Decimal("0.01")
    income_abs: Decimal = Decimal("0.01")
    # None uses the built-in official names. An empty dict maps nothing.
    asset_names: Optional[Dict[str, Sequence[str]]] = None


# Names Koinly prints next to the ticker. A different name in brackets is not one of these.
_DEFAULT_ASSET_NAMES: Dict[str, Sequence[str]] = {
    "BTC": ("Bitcoin", "BTC (Bitcoin)"),
    "ETH": ("Ethereum", "ETH (Ethereum)"),
    "USDT": ("Tether", "USDT (Tether)"),
}
# A holdings currency cell with one of these names is a totals line, not an asset.
_SUMMARY_ASSETS = {"total", "totals", "grand total", "subtotal", "sum"}
_INCOME_TAGS = {
    "airdrop",
    "fork",
    "mining",
    "reward",
    "income",
    "other income",
    "lending interest",
    "cashback",
    "salary",
    "staking",
    "interest",
}
_MARGIN_TAGS = {
    "realized gain",
    "realized loss",
    "margin fee",
    "margin fee rebate",
    "funding fee",
    "funding",
}
_COUNTING_NOTES = (
    "T1 and T2 follow the rows the Koinly converter writes. "
    "A tax-report transfer is one withdrawal on the sending wallet and one deposit "
    "on the receiving wallet. T2 counts those two legs. When the engine writes the "
    "pair as one row, the note says transfer match and that row counts as both a "
    "withdrawal and a deposit. Two engine rows with the same match keep their own "
    "types. A buy, sell or exchange is one trade on a single wallet. "
    "A bulk-edit row with both a from amount and a to amount is one trade on the "
    "To wallet, including when its type says transfer. "
    "Reward, income, airdrop, mining, salary and lending interest are income. "
    "Realized gain and margin fee are margin. Those rows are not also counted as "
    "deposits or withdrawals. A wallet name drops the id after a semicolon.",
    "Y1, Y2 and Y3 compare only years present in that Koinly report. A snapshot "
    "year with no report is listed under skipped_years and is not treated as zero.",
    "Y3 compares Koinly net capital gains, which include futures, with spot "
    "capital gains plus the margin net.",
    "Y4 missing cost uses a numeric missing-cost column when the file has one. "
    "Otherwise a Missing cost basis indicator selects the lot, and the amount is "
    "the proceeds, or the gain when proceeds are blank. With no indicator, a "
    "disposal whose cost is zero or blank and whose proceeds are positive counts "
    "for those proceeds. Y4 does not fail the report.",
    "P1 and P2 map a Koinly label onto a symbol only when asset_names lists that "
    "official name. BTC (Bitcoin) can match BTC. BTC (Bear Trump Coin) cannot. "
    "The built-in names do not include Tether USD, so USDT (Tether USD) stays "
    "separate unless a local asset_names entry lists that name. "
    "A holdings row named Total, Totals, Grand Total or Subtotal is not an asset.",
)


_ENGINE_BUCKETS = {
    "Trade": "trade",
    "Deposit": "deposit",
    "Withdrawal": "withdrawal",
    "Income": "income",
    "Interest": "income",
    "Mining": "income",
    "Staking-Reward": "income",
    "Airdrop": "income",
    "Dividend": "income",
    "Margin-Gain": "margin",
    "Margin-Loss": "margin",
    "Margin-Fee": "margin",
    "Margin-Fee-Rebate": "margin",
    "Gift-Sent": "disposal",
    "Spend": "disposal",
    "Charity-Sent": "disposal",
    "Lost": "disposal",
    "Gift-Received": "acquisition",
    "Review-Acquisition": "acquisition",
    "Allocation": "allocation",
    "Allocation-Claim": "acquisition",
    "_Needs-Review": "review",
}


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bittytax reconcile",
        description=(
            "Compare a private Koinly export folder with BittyTax records and a "
            "5 April Section 104 snapshot. Writes a JSON pass/fail report."
        ),
    )
    parser.add_argument("--koinly", required=True, help="folder of Koinly CSV exports")
    parser.add_argument(
        "--records", required=True, help="BittyTax transaction CSV from bittytax_conv"
    )
    parser.add_argument("--snapshot", required=True, help="section104 snapshot JSON")
    parser.add_argument("--wallets", default="", help="local wallets.yaml (fake addresses only)")
    parser.add_argument("--exceptions", default="", help="review exception list, or a wallets file")
    parser.add_argument("--review-queue", default="", help="review-queue CSV from bittytax_conv")
    parser.add_argument("--output", required=True, help="path of the JSON report")
    parser.add_argument(
        "--review-rows",
        default="",
        help="review-row setting to record. Defaults to the accounting config.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_report(
        koinly_dir=args.koinly,
        records_path=args.records,
        snapshot_path=args.snapshot,
        wallets_path=args.wallets,
        exceptions_path=args.exceptions,
        queue_path=args.review_queue,
        review_rows=args.review_rows or str(config.review_rows),
    )
    folder = os.path.dirname(args.output)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=_json_default)
        handle.write("\n")
    print(f"reconciliation {'passed' if report['passed'] else 'failed'}: {args.output}")
    if not report["passed"]:
        sys.exit(1)


def build_report(
    koinly_dir: str,
    records_path: str,
    snapshot_path: str,
    wallets_path: str = "",
    exceptions_path: str = "",
    queue_path: str = "",
    review_rows: str = "conservative",
) -> Dict[str, Any]:
    """Return the machine-readable pass/fail report. Y4 never flips the result."""
    desk = _load_desk(wallets_path)
    exceptions = load_review_exceptions(exceptions_path) if exceptions_path else []
    skip_wallets = {item.get("wallet", "") for item in desk.count_exceptions if item.get("wallet")}
    pack = _read_pack(koinly_dir, skip_wallets, desk)
    engine = _read_engine(records_path)
    with open(snapshot_path, encoding="utf-8") as handle:
        snapshot = json.load(handle)

    checks: Dict[str, Any] = {}
    checks["T1"] = _check_wallets(pack, engine, desk.count_exceptions)
    checks["T2"] = _check_types(pack, engine)
    checks["P1"] = _check_quantities(pack, snapshot, desk)
    checks["P2"] = _check_costs(pack, snapshot, desk)
    checks["Y1"] = _check_year_total(
        pack, snapshot, "income", "income", desk.income_abs, Decimal(0)
    )
    checks["Y2"] = _check_year_total(
        pack, snapshot, "other", "margin_net", desk.income_abs, Decimal(0)
    )
    checks["Y3"] = _check_gains(pack, snapshot, desk)
    checks["Y4"] = _check_missing(pack, snapshot)
    checks["review_queue"] = _check_queue(queue_path, exceptions)

    blocking = [
        name
        for name, check in checks.items()
        if isinstance(check, dict) and not check["passed"] and check.get("affects_result", True)
    ]
    return {
        "passed": not blocking,
        "failing_checks": blocking,
        "review_rows": review_rows,
        "exceptions": [_exception_dict(item) for item in exceptions],
        "count_exceptions": desk.count_exceptions,
        "share_pools": desk.share_pools,
        "token_groups": desk.token_groups,
        "official_assets": desk.official_assets,
        "asset_names": {symbol: list(aliases) for symbol, aliases in _active_names(desk).items()},
        "holdings_as_of": [
            {
                "as_of": holding.as_of,
                "tax_year": holding.tax_year,
                "tax_year_label": holding.tax_year_label,
            }
            for holding in pack.holdings
        ],
        "notes": pack.notes,
        "checks": checks,
        "pending_allocations": pending_allocations(records_path),
    }


def _load_desk(path: str) -> Desk:
    desk = Desk()
    if not path:
        return desk
    registry = load_wallet_registry(path)
    desk.share_pools = registry.match.share_pools
    desk.token_groups = [
        {"symbols": list(group.symbols), "liquid_members": list(group.liquid_symbols)}
        for group in registry.match.token_groups
    ]
    desk.official_assets = list(registry.match.official_assets)
    desk.count_exceptions = [
        {"reason": item.reason, "wallet": item.wallet, "tx_hash": item.tx_hash}
        for item in registry.count_exceptions
    ]
    desk.quantity_epsilon = registry.reconcile.quantity_epsilon
    desk.cost_band_ratio = registry.reconcile.cost_band_ratio
    desk.cost_band_abs = registry.reconcile.cost_band_abs
    desk.gains_band_ratio = registry.reconcile.gains_band_ratio
    desk.income_abs = registry.reconcile.income_abs
    if registry.asset_names is not None:
        desk.asset_names = {
            symbol: list(aliases) for symbol, aliases in registry.asset_names.items()
        }
    return desk


def _read_pack(directory: str, skip_wallets: Set[str], desk: Desk) -> KoinlyPack:
    pack = KoinlyPack(notes=list(_COUNTING_NOTES))
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Koinly folder not found: {directory}")
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(directory, name)
        rows = _read_csv(path)
        kind = _kind(name, rows)
        if kind == "history":
            _consume_history(rows, pack, skip_wallets)
        elif kind == "holdings":
            pack.holdings.append(_consume_holdings(rows, desk))
        elif kind == "income":
            _add_totals(pack.income, _sum_by_year(rows, ("value", "income", "amount")))
        elif kind == "other":
            _add_totals(pack.other, _sum_by_year(rows, ("gain", "profit", "amount")))
        elif kind == "gains":
            _add_totals(pack.gains, _sum_by_year(rows, ("gain",)))
            _add_totals(pack.missing, _sum_missing(rows))
        else:
            pack.notes.append(f"ignored {name}")
    return pack


def _read_engine(path: str) -> Dict[str, Dict[str, int]]:
    wallets: Dict[str, int] = {}
    types: Dict[str, int] = {}
    paired: Dict[str, List[str]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            wallet = (row.get("Wallet") or "").strip()
            if wallet:
                wallets[wallet] = wallets.get(wallet, 0) + 1
            bucket = _ENGINE_BUCKETS.get((row.get("Type") or "").strip(), "other")
            match_id = _transfer_match_id(row.get("Note") or "")
            if match_id:
                paired.setdefault(match_id, []).append(bucket)
            else:
                types[bucket] = types.get(bucket, 0) + 1
    _count_paired_transfers(types, paired)
    return {"wallets": wallets, "types": types}


def _transfer_match_id(note: str) -> str:
    """Return the match number from a converter note, or an empty string."""
    found = re.search(r"transfer match (\d+)", note, re.IGNORECASE)
    if found is None:
        return ""
    return found.group(1)


def _count_paired_transfers(types: Dict[str, int], paired: Mapping[str, Sequence[str]]) -> None:
    """Count a paired transfer on the same basis as the Koinly deposit and withdrawal legs.

    Two engine rows that share a match keep their own types. One row means the pair was
    written as a single record, so it counts as one withdrawal and one deposit.
    """
    for buckets in paired.values():
        if len(buckets) == 1:
            types["withdrawal"] = types.get("withdrawal", 0) + 1
            types["deposit"] = types.get("deposit", 0) + 1
            continue
        for bucket in buckets:
            types[bucket] = types.get(bucket, 0) + 1


def _check_wallets(
    pack: KoinlyPack, engine: Mapping[str, Dict[str, int]], exceptions: Sequence[Mapping[str, str]]
) -> Dict[str, Any]:
    koinly = dict(pack.wallets)
    actual = dict(engine["wallets"])
    for item in exceptions:
        wallet = str(item.get("wallet", ""))
        if wallet:
            koinly.pop(wallet, None)
            actual.pop(wallet, None)
    names = sorted(set(koinly) | set(actual))
    failures = []
    for name in names:
        left = koinly.get(name, 0)
        right = actual.get(name, 0)
        if left != right:
            failures.append({"wallet": name, "koinly": left, "engine": right})
    return {
        "name": "T1 transaction counts per wallet",
        "passed": not failures,
        "failures": failures,
    }


def _check_types(pack: KoinlyPack, engine: Mapping[str, Dict[str, int]]) -> Dict[str, Any]:
    koinly = pack.types
    actual = engine["types"]
    names = sorted(set(koinly) | set(actual))
    failures = []
    for name in names:
        left = int(koinly.get(name, 0))
        right = int(actual.get(name, 0))
        if left != right:
            failures.append({"type": name, "koinly": left, "engine": right})
    return {"name": "T2 per-type counts", "passed": not failures, "failures": failures}


def _check_quantities(pack: KoinlyPack, snapshot: Mapping[str, Any], desk: Desk) -> Dict[str, Any]:
    official = {item.upper() for item in desk.official_assets}
    failures = []
    for holding in pack.holdings:
        year = holding.tax_year
        pools = _pools_for(snapshot, year)
        names = sorted(set(holding.amounts) | set(pools))
        for asset in names:
            koinly_qty = holding.amounts.get(asset, Decimal(0))
            engine_qty = pools.get(asset, Decimal(0))
            gap = abs(koinly_qty - engine_qty)
            allowed = Decimal(0) if asset.upper() in official else desk.quantity_epsilon
            if gap > allowed:
                failures.append(
                    {
                        "tax_year": year,
                        "asset": asset,
                        "koinly": f"{koinly_qty:f}",
                        "engine": f"{engine_qty:f}",
                        "as_of": holding.as_of,
                    }
                )
    return {
        "name": "P1 pool quantity at 5 April",
        "passed": not failures,
        "failures": failures,
    }


def _check_costs(pack: KoinlyPack, snapshot: Mapping[str, Any], desk: Desk) -> Dict[str, Any]:
    failures = []
    for holding in pack.holdings:
        year = holding.tax_year
        costs = _costs_for(snapshot, year)
        names = sorted(set(holding.costs) | set(costs))
        for asset in names:
            koinly_cost = holding.costs.get(asset, Decimal(0))
            engine_cost = costs.get(asset, Decimal(0))
            band = max(desk.cost_band_abs, abs(koinly_cost) * desk.cost_band_ratio)
            if abs(koinly_cost - engine_cost) > band:
                failures.append(
                    {
                        "tax_year": year,
                        "asset": asset,
                        "koinly": f"{koinly_cost:f}",
                        "engine": f"{engine_cost:f}",
                        "band": f"{band:f}",
                    }
                )
    return {"name": "P2 pool cost at 5 April", "passed": not failures, "failures": failures}


def _check_year_total(
    pack: KoinlyPack,
    snapshot: Mapping[str, Any],
    pack_key: str,
    snap_key: str,
    absolute: Decimal,
    ratio: Decimal,
) -> Dict[str, Any]:
    koinly: Dict[str, Decimal] = getattr(pack, pack_key)
    years = snapshot.get("years", {})
    if not isinstance(years, dict):
        years = {}
    names, skipped = _years_in_pack(koinly, years)
    failures = []
    for year in names:
        left = koinly[year]
        block = years.get(year, {})
        if not isinstance(block, dict):
            block = {}
        right = _decimal(block.get(snap_key, "0"))
        band = max(absolute, abs(left) * ratio)
        if abs(left - right) > band:
            failures.append(
                {
                    "tax_year": year,
                    "koinly": f"{left:f}",
                    "engine": f"{right:f}",
                    "band": f"{band:f}",
                }
            )
    labels = {
        "income": "Y1 yearly income",
        "other": "Y2 futures and other gains",
        "gains": "Y3 net capital gains",
    }
    return {
        "name": labels[pack_key],
        "passed": not failures,
        "failures": failures,
        "skipped_years": skipped,
        "definition": (
            "Compared only for years present in the Koinly report. A snapshot year "
            "with no report is listed in skipped_years and is not treated as zero."
        ),
    }


def _check_gains(pack: KoinlyPack, snapshot: Mapping[str, Any], desk: Desk) -> Dict[str, Any]:
    """The Koinly net figure includes futures. Compare it with spot gains plus margin."""
    koinly = pack.gains
    years = snapshot.get("years", {})
    if not isinstance(years, dict):
        years = {}
    names, skipped = _years_in_pack(koinly, years)
    failures = []
    for year in names:
        left = koinly[year]
        block = years.get(year, {})
        if not isinstance(block, dict):
            block = {}
        spot = _decimal(block.get("capital_gains", "0"))
        margin = _decimal(block.get("margin_net", "0"))
        right = spot + margin
        band = max(Decimal(1), abs(left) * desk.gains_band_ratio)
        if abs(left - right) > band:
            failures.append(
                {
                    "tax_year": year,
                    "koinly": f"{left:f}",
                    "engine": f"{right:f}",
                    "engine_spot": f"{spot:f}",
                    "engine_margin": f"{margin:f}",
                    "band": f"{band:f}",
                }
            )
    return {
        "name": "Y3 net capital gains",
        "passed": not failures,
        "failures": failures,
        "skipped_years": skipped,
        "definition": (
            "Koinly net capital gains, including futures, compared with spot "
            "capital gains plus the margin net. Compared only for years present "
            "in the Koinly report. A snapshot year with no report is listed in "
            "skipped_years and is not treated as zero."
        ),
    }


def _years_in_pack(
    koinly: Mapping[str, Decimal], years: Mapping[str, Any]
) -> Tuple[List[str], List[str]]:
    """Years the report contains, and snapshot years that report does not contain."""
    present = {str(year) for year in koinly}
    skipped = sorted(str(year) for year in years if str(year) not in present)
    return sorted(present), skipped


def _check_missing(pack: KoinlyPack, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    rows = []
    years = snapshot.get("years", {})
    if not isinstance(years, dict):
        years = {}
    koinly = pack.missing
    names = sorted(set(years) | set(koinly))
    for year in names:
        block = years.get(year, {})
        rows.append(
            {
                "tax_year": year,
                "engine_missing_cost": block.get("missing_cost", "0"),
                "koinly_missing_cost": f"{koinly.get(year, Decimal(0)):f}",
            }
        )
    return {
        "name": "Y4 missing cost beside Koinly shortfall",
        "passed": True,
        "affects_result": False,
        "definition": (
            "A numeric missing-cost column is summed when every value in it is a number. "
            "Otherwise a Missing cost basis mark of yes selects the lot, and the amount is "
            "the proceeds, or the gain when proceeds are blank. With no mark, a disposal "
            "whose cost is zero or blank and whose proceeds are positive counts for those "
            "proceeds. This check does not fail the report."
        ),
        "rows": rows,
    }


def _check_queue(path: str, exceptions: Sequence[ReviewException]) -> Dict[str, Any]:
    if not path:
        return {
            "name": "review queue",
            "passed": True,
            "failures": [],
            "note": "no review-queue file was passed",
        }
    items = _read_queue(path)
    left = review_queue_failures(items, exceptions)
    return {
        "name": "review queue",
        "passed": not left,
        "failures": [
            {
                "timestamp": item.timestamp,
                "asset": item.asset,
                "quantity": item.quantity,
                "wallet": item.wallet,
                "tx_hash": item.tx_hash,
                "disposition": item.disposition,
            }
            for item in left
        ],
    }


def _consume_history(
    rows: Sequence[Sequence[str]], pack: KoinlyPack, skip_wallets: Set[str]
) -> None:
    header_at = _header_index(rows)
    if header_at is None:
        return
    header = [cell.strip() for cell in rows[header_at]]
    index = {name.casefold(): pos for pos, name in enumerate(header)}
    bulk = "from wallet (read-only)" in index or "from amount" in index
    for raw in rows[header_at + 1 :]:
        if _blank(raw) or _deleted(raw, index) or _skipped_wallet(raw, index, skip_wallets):
            continue
        _count_history_row(pack, raw, index, bulk)


def _consume_holdings(rows: Sequence[Sequence[str]], desk: Desk) -> HoldingSet:
    as_of = ""
    for raw in rows:
        joined = " ".join(raw)
        if "balances as at" in joined.casefold():
            as_of = _parse_as_of(joined)
            break
    header_at = _header_index(rows)
    amounts: Dict[str, Decimal] = {}
    costs: Dict[str, Decimal] = {}
    if header_at is None:
        return HoldingSet(as_of=as_of, tax_year="", amounts=amounts, costs=costs)
    header = [cell.strip() for cell in rows[header_at]]
    asset_at = _find_col(header, ("currency", "asset", "coin"))
    amount_at = _find_amount_col(header)
    cost_at = _find_col(header, ("cost",))
    for raw in rows[header_at + 1 :]:
        if asset_at is None or amount_at is None or asset_at >= len(raw):
            continue
        label = raw[asset_at].strip()
        if not label or label.casefold().startswith("balance") or _summary_asset(label):
            continue
        asset = _canonical_asset(label, _active_names(desk))
        if not asset:
            continue
        quantity = _decimal(raw[amount_at] if amount_at < len(raw) else "")
        amounts[asset] = amounts.get(asset, Decimal(0)) + quantity
        if cost_at is not None and cost_at < len(raw) and raw[cost_at].strip():
            costs[asset] = costs.get(asset, Decimal(0)) + _decimal(raw[cost_at])
    tax_year = ""
    label = ""
    if as_of:
        year = tax_year_of(datetime.fromisoformat(as_of).date())
        tax_year = str(year)
        label = tax_year_label(year)
    return HoldingSet(
        as_of=as_of, tax_year=tax_year, tax_year_label=label, amounts=amounts, costs=costs
    )


def _sum_by_year(rows: Sequence[Sequence[str]], prefer: Sequence[str]) -> Dict[str, Decimal]:
    header_at = _header_index(rows)
    totals: Dict[str, Decimal] = {}
    if header_at is None:
        return totals
    header = [cell.strip() for cell in rows[header_at]]
    date_at = _find_col(header, ("date",))
    value_at = _find_col(header, prefer)
    if date_at is None or value_at is None:
        return totals
    for raw in rows[header_at + 1 :]:
        if date_at >= len(raw) or value_at >= len(raw) or not raw[date_at].strip():
            continue
        year = str(tax_year_of(_parse_date(raw[date_at])))
        totals[year] = totals.get(year, Decimal(0)) + _decimal(raw[value_at])
    return totals


def _sum_missing(rows: Sequence[Sequence[str]]) -> Dict[str, Decimal]:
    """Missing cost from a numeric column, an indicator, or a zero cost on a disposal."""
    header_at = _header_index(rows)
    totals: Dict[str, Decimal] = {}
    if header_at is None:
        return totals
    header = [cell.strip() for cell in rows[header_at]]
    numeric_at = _numeric_missing_col(header, rows, header_at)
    if numeric_at is not None:
        return _sum_marked_column(rows, header, header_at, numeric_at, totals)
    return _sum_missing_lots(rows, header, header_at, totals)


def _read_queue(path: str) -> List[QueueItem]:
    items = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            items.append(
                QueueItem(
                    timestamp=row.get("Date", ""),
                    asset=row.get("Asset", ""),
                    quantity=row.get("Amount", ""),
                    wallet=row.get("Wallet", ""),
                    tx_hash=row.get("TxHash", ""),
                    note=row.get("Description", ""),
                    disposition=row.get("Disposition", ""),
                )
            )
    return items


def _count_history_row(
    pack: KoinlyPack, raw: Sequence[str], index: Mapping[str, int], bulk: bool
) -> None:
    kind = _cell(raw, index, "type").strip().casefold()
    tag = _cell(raw, index, "tag") or _cell(raw, index, "label")
    if bulk:
        sending = _side_wallet(raw, index, "from wallet (read-only)", "from amount")
        receiving = _side_wallet(raw, index, "to wallet (read-only)", "to amount")
    else:
        sending = _side_wallet(raw, index, "sending wallet", "sent amount")
        receiving = _side_wallet(raw, index, "receiving wallet", "received amount")
    if (
        not sending
        and not receiving
        and "wallet" in index
        and "sending wallet" not in index
        and "from wallet (read-only)" not in index
    ):
        sending = _wallet_name(_cell(raw, index, "wallet"))
    for wallet in _wallets_for(kind, sending, receiving, bulk):
        pack.wallets[wallet] = pack.wallets.get(wallet, 0) + 1
    for bucket in _buckets_for(kind, tag, bool(sending), bool(receiving), bulk):
        pack.types[bucket] = pack.types.get(bucket, 0) + 1


def _wallets_for(kind: str, sending: str, receiving: str, bulk: bool) -> List[str]:
    folded = " ".join(kind.replace("_", " ").split())
    if bulk and sending and receiving:
        return [receiving]
    if not bulk and folded == "transfer":
        hits = []
        if sending:
            hits.append(sending)
        if receiving:
            hits.append(receiving)
        return hits
    if folded in ("buy", "sell", "exchange", "trade"):
        chosen = sending or receiving
        return [chosen] if chosen else []
    if folded in ("crypto withdrawal", "withdrawal", "fiat withdrawal"):
        return [sending] if sending else []
    chosen = receiving or sending
    return [chosen] if chosen else []


def _buckets_for(kind: str, tag: str, has_send: bool, has_receive: bool, bulk: bool) -> List[str]:
    folded = " ".join(kind.replace("_", " ").split())
    names: List[str] = []
    if bulk and has_send and has_receive:
        names = ["trade"]
    elif not bulk and folded == "transfer":
        if has_send:
            names.append("withdrawal")
        if has_receive:
            names.append("deposit")
        if not names:
            names = ["withdrawal", "deposit"]
    elif folded in ("buy", "sell", "exchange", "trade"):
        names = ["trade"]
    else:
        tagged = _tag_bucket(tag)
        if tagged:
            names = [tagged]
        elif folded in ("crypto deposit", "deposit"):
            names = ["deposit"]
        elif folded in ("crypto withdrawal", "withdrawal"):
            names = ["withdrawal"]
        elif folded in ("fiat deposit", "fiat withdrawal"):
            names = ["fiat"]
        elif folded in ("reward", "staking", "airdrop", "mining", "interest", "income"):
            names = ["income"]
        elif folded:
            names = ["other"]
    return names


def _tag_bucket(tag: str) -> str:
    folded = " ".join(tag.replace("_", " ").split()).casefold()
    if not folded:
        return ""
    if "realized" in folded or folded in _MARGIN_TAGS:
        return "margin"
    if folded in _INCOME_TAGS:
        return "income"
    return ""


def _side_wallet(
    raw: Sequence[str], index: Mapping[str, int], wallet_key: str, amount_key: str
) -> str:
    if wallet_key not in index:
        return ""
    wallet = _wallet_name(_cell(raw, index, wallet_key))
    if not wallet:
        return ""
    if amount_key not in index:
        return wallet
    amount = _cell(raw, index, amount_key)
    if amount.strip() in ("", "0", "0.0") or _decimal(amount) == 0:
        return ""
    return wallet


def _wallet_name(value: str) -> str:
    """Drop the ;id suffix Koinly adds to a wallet or currency cell."""
    return value.split(";", 1)[0].strip()


def _skipped_wallet(raw: Sequence[str], index: Mapping[str, int], skip_wallets: Set[str]) -> bool:
    names = [
        _cell(raw, index, key)
        for key in (
            "sending wallet",
            "receiving wallet",
            "from wallet (read-only)",
            "to wallet (read-only)",
            "wallet",
        )
        if key in index
    ]
    present = [_wallet_name(name) for name in names if name]
    return bool(present) and all(name in skip_wallets for name in present)


def _pools_for(snapshot: Mapping[str, Any], year: str) -> Dict[str, Decimal]:
    pools = snapshot.get("pools", {})
    block = pools.get(year, {}) if isinstance(pools, dict) else {}
    found = {}
    for asset in block.get("assets", []) if isinstance(block, dict) else []:
        found[str(asset["asset"])] = _decimal(asset.get("quantity", "0"))
    return found


def _costs_for(snapshot: Mapping[str, Any], year: str) -> Dict[str, Decimal]:
    pools = snapshot.get("pools", {})
    block = pools.get(year, {}) if isinstance(pools, dict) else {}
    found = {}
    for asset in block.get("assets", []) if isinstance(block, dict) else []:
        found[str(asset["asset"])] = _decimal(asset.get("cost", "0"))
    return found


def _kind(name: str, rows: Sequence[Sequence[str]]) -> str:
    folded = name.casefold()
    blob = " ".join(" ".join(row) for row in rows[:6]).casefold()
    if "balances as at" in blob or "holding" in folded or "eoy" in folded:
        return "holdings"
    if "income" in folded:
        return "income"
    if "other" in folded or "future" in folded:
        return "other"
    if "capital" in folded or ("gain" in folded and "other" not in folded):
        return "gains"
    header_at = _header_index(rows)
    if header_at is None:
        return "ignored"
    names = {cell.strip().casefold() for cell in rows[header_at]}
    if "type" in names and names & {"sending wallet", "from wallet (read-only)", "wallet"}:
        return "history"
    return "ignored"


def _header_index(rows: Sequence[Sequence[str]]) -> Optional[int]:
    for index, raw in enumerate(rows[:12]):
        names = {cell.strip().casefold() for cell in raw}
        if "date" in names or "currency" in names or "asset" in names or "type" in names:
            return index
    return None


def _find_col(header: Sequence[str], needles: Sequence[str]) -> Optional[int]:
    """Prefer needles in the order given, so value is chosen before amount."""
    for needle in needles:
        for index, cell in enumerate(header):
            folded = cell.strip().casefold()
            if needle not in folded or "fee" in folded:
                continue
            return index
    return None


def _find_amount_col(header: Sequence[str]) -> Optional[int]:
    for index, cell in enumerate(header):
        folded = cell.strip().casefold()
        if any(word in folded for word in ("amount", "quantity", "balance")):
            if "cost" in folded or "value" in folded or "worth" in folded:
                continue
            return index
    return None


def _parse_date(text: str) -> date:
    """ISO dates stay year-month-day. Other Koinly dates are day-first."""
    if re.match(r"\d{4}-\d{2}-\d{2}", text.strip()):
        parsed = dateutil.parser.parse(text, dayfirst=False)
    else:
        parsed = dateutil.parser.parse(text, dayfirst=True)
    return parsed.date()


def _parse_as_of(text: str) -> str:
    marker = text.casefold().find("balances as at")
    tail = text[marker + len("balances as at") :]
    tail = tail.replace("23:59", " ").replace(",", " ")
    iso = re.search(r"\d{4}-\d{2}-\d{2}", tail)
    try:
        if iso:
            return _parse_date(iso.group(0)).isoformat()
        return _parse_date(tail).isoformat()
    except (ValueError, OverflowError, TypeError):
        return ""


def _read_csv(path: str) -> List[List[str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _cell(raw: Sequence[str], index: Mapping[str, int], key: str) -> str:
    if key not in index or index[key] >= len(raw):
        return ""
    return raw[index[key]].strip()


def _deleted(raw: Sequence[str], index: Mapping[str, int]) -> bool:
    value = _cell(raw, index, "deleted").casefold()
    return value in ("true", "yes", "1")


def _blank(raw: Sequence[str]) -> bool:
    return not any(cell.strip() for cell in raw)


def _decimal(value: object) -> Decimal:
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return Decimal(0)
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    for symbol in ("£", "$", "€"):
        text = text.replace(symbol, "")
    try:
        number = Decimal(text)
    except (ArithmeticError, ValueError):
        return Decimal(0)
    if negative:
        return -number
    return number


def _active_names(desk: Desk) -> Mapping[str, Sequence[str]]:
    if desk.asset_names is None:
        return _DEFAULT_ASSET_NAMES
    return desk.asset_names


def _summary_asset(label: str) -> bool:
    """A Koinly holdings total line is not a pool asset."""
    folded = " ".join(label.casefold().split())
    if folded in _SUMMARY_ASSETS:
        return True
    if not folded.startswith("total"):
        return False
    rest = folded[len("total") :].strip()
    return rest == "" or rest.startswith("(")


def _canonical_asset(label: str, names: Mapping[str, Sequence[str]]) -> str:
    """Map an official Koinly label onto its symbol. A lookalike name stays as written."""
    text = label.strip()
    if not text:
        return text
    folded = text.casefold()
    for symbol, aliases in names.items():
        allowed = {symbol.casefold()}
        allowed.update(alias.casefold() for alias in aliases)
        if folded in allowed:
            return symbol.upper()
    match = re.fullmatch(r"([A-Za-z0-9]+)\s*\((.+)\)", text)
    if match is None:
        return text
    symbol = match.group(1).upper()
    inside = match.group(2).strip().casefold()
    allowed_inside = {alias.casefold() for alias in names.get(symbol, ())}
    if inside in allowed_inside:
        return symbol
    return text


def _numeric_missing_col(
    header: Sequence[str], rows: Sequence[Sequence[str]], header_at: int
) -> Optional[int]:
    for index, cell in enumerate(header):
        folded = cell.strip().casefold()
        if "missing cost" not in folded and "shortfall" not in folded:
            continue
        if _column_is_numeric(rows, header_at, index):
            return index
    return None


def _column_is_numeric(rows: Sequence[Sequence[str]], header_at: int, index: int) -> bool:
    saw = False
    for raw in rows[header_at + 1 :]:
        if index >= len(raw) or not raw[index].strip():
            continue
        saw = True
        if not _is_plain_number(raw[index]):
            return False
    return saw


def _is_plain_number(value: str) -> bool:
    text = value.strip().replace(",", "").replace(" ", "").replace("£", "").replace("$", "")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    if not text:
        return False
    try:
        Decimal(text)
    except (ArithmeticError, ValueError):
        return False
    return True


def _sum_marked_column(
    rows: Sequence[Sequence[str]],
    header: Sequence[str],
    header_at: int,
    column: int,
    totals: Dict[str, Decimal],
) -> Dict[str, Decimal]:
    date_at = _find_col(header, ("date",))
    for raw in rows[header_at + 1 :]:
        if column >= len(raw) or not raw[column].strip():
            continue
        if date_at is None or date_at >= len(raw) or not raw[date_at].strip():
            continue
        year = str(tax_year_of(_parse_date(raw[date_at])))
        totals[year] = totals.get(year, Decimal(0)) + _decimal(raw[column])
    return totals


def _sum_missing_lots(
    rows: Sequence[Sequence[str]],
    header: Sequence[str],
    header_at: int,
    totals: Dict[str, Decimal],
) -> Dict[str, Decimal]:
    date_at = _find_col(header, ("date",))
    cost_at = _find_disposal_cost(header)
    proceeds_at = _find_col(header, ("proceeds",))
    gain_at = _find_col(header, ("gain",))
    indicator_at = _indicator_col(header, rows, header_at)
    if date_at is None:
        return totals
    for raw in rows[header_at + 1 :]:
        if date_at >= len(raw) or not raw[date_at].strip():
            continue
        amount = _lot_missing_amount(raw, cost_at, proceeds_at, gain_at, indicator_at)
        if amount == 0:
            continue
        year = str(tax_year_of(_parse_date(raw[date_at])))
        totals[year] = totals.get(year, Decimal(0)) + amount
    return totals


def _find_disposal_cost(header: Sequence[str]) -> Optional[int]:
    for index, cell in enumerate(header):
        folded = cell.strip().casefold()
        if "cost" in folded and "missing" not in folded and "fee" not in folded:
            return index
    return None


def _indicator_col(
    header: Sequence[str], rows: Sequence[Sequence[str]], header_at: int
) -> Optional[int]:
    for index, cell in enumerate(header):
        folded = cell.strip().casefold()
        if "missing cost" not in folded and "shortfall" not in folded:
            continue
        if not _column_is_numeric(rows, header_at, index):
            return index
    return None


def _lot_missing_amount(
    raw: Sequence[str],
    cost_at: Optional[int],
    proceeds_at: Optional[int],
    gain_at: Optional[int],
    indicator_at: Optional[int],
) -> Decimal:
    proceeds = (
        _decimal(raw[proceeds_at])
        if proceeds_at is not None and proceeds_at < len(raw)
        else Decimal(0)
    )
    gain = _decimal(raw[gain_at]) if gain_at is not None and gain_at < len(raw) else Decimal(0)
    amount = proceeds if proceeds != 0 else gain
    if indicator_at is not None:
        flag = raw[indicator_at].strip().casefold() if indicator_at < len(raw) else ""
        if flag not in ("yes", "true", "1", "y", "missing"):
            return Decimal(0)
        return amount
    if cost_at is None or cost_at >= len(raw):
        return Decimal(0)
    cost_text = raw[cost_at].strip()
    if cost_text and _decimal(cost_text) != 0:
        return Decimal(0)
    if proceeds > 0:
        return proceeds
    if gain > 0:
        return gain
    return Decimal(0)


def _add_totals(target: Dict[str, Decimal], extra: Mapping[str, Decimal]) -> None:
    for key, value in extra.items():
        target[key] = target.get(key, Decimal(0)) + value


def _exception_dict(item: ReviewException) -> Dict[str, str]:
    return {
        "reason": item.reason,
        "tx_hash": item.tx_hash,
        "date": item.on_date,
        "asset": item.asset,
        "amount": item.amount,
        "wallet": item.wallet,
    }


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return f"{value:f}"
    raise TypeError(f"Cannot encode {type(value).__name__}")
