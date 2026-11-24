import csv
from decimal import Decimal
from pathlib import Path
from typing import List

from test_transfer_match import BASE, _actions, _make, _registry, _run

from bittytax.bt_types import TrType
from bittytax.conv.transfer_match import NEEDS_REVIEW_TYPE, write_reconciliation_reports
from bittytax.conv.wallets import AddressRule, OfficialContract, TokenGroup
from bittytax.import_records import ImportRecords
from bittytax.t_row import TransactionRow

OFFICIAL = "0x0000000000000000000000000000000000000a11"
FAKE = "0x0000000000000000000000000000000000000a22"
POISON = "0x0000000000000000000000000000000000000b0b"
STAKE = "0x00000000000000000000000000000000000000d4"
SWAP_HASH = "0x" + "cd" * 16


def _usdt_rules() -> List[OfficialContract]:
    return [
        OfficialContract(
            asset="USDT",
            address=OFFICIAL,
            chain="ethereum",
            names=("Tether USD",),
        )
    ]


def test_fake_contract_and_poison_address_are_dropped_as_spam(tmp_path: Path) -> None:
    plain = _make(TrType.GIFT_RECEIVED, BASE, "USDT", "6", "Ledger", chain="ethereum")
    plain.row_dict["ContractAddress"] = FAKE
    untouched = _run(plain)
    assert plain.t_record is not None
    assert str(plain.t_record.t_type) != "_Possible-Spam"
    assert untouched.spam == []

    registry = _registry(share_pools=True)
    registry.official_contracts = _usdt_rules()
    registry.poison_addresses = [AddressRule("Lookalike", POISON, "poison", "ethereum")]
    registry.match.token_groups = [TokenGroup(("USDT", "TETHER USD"))]

    official = _make(TrType.GIFT_RECEIVED, BASE, "USDT", "10", "Ledger", chain="ethereum")
    official.row_dict["ContractAddress"] = OFFICIAL
    fake = _make(TrType.GIFT_RECEIVED, BASE, "USDT", "6", "Ledger", chain="ethereum")
    fake.row_dict["ContractAddress"] = FAKE
    named = _make(TrType.GIFT_RECEIVED, BASE, "Tether USD", "6", "Ledger", chain="ethereum")
    named.row_dict["ContractAddress"] = FAKE
    poisoned = _make(TrType.GIFT_SENT, BASE, "USDT", "1", "Ledger", destination=POISON)
    from_lookalike = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.01", "Ledger", source=POISON)
    result = _run(official, fake, named, poisoned, from_lookalike, registry=registry)

    assert official.t_record is not None
    assert str(official.t_record.t_type) != "_Possible-Spam"
    assert official.t_record.buy_asset == "USDT"

    for row, asset in ((fake, "USDT"), (named, "Tether USD"), (poisoned, "USDT")):
        assert row.t_record is not None
        assert str(row.t_record.t_type) == "_Possible-Spam"
        assert row.t_record.t_type not in (
            TrType.GIFT_SENT,
            TrType.GIFT_RECEIVED,
            TrType.SPEND,
            TrType.WITHDRAWAL,
            TrType.DEPOSIT,
            TrType.TRADE,
        )
        kept = row.t_record.sell_asset or row.t_record.buy_asset
        assert kept == asset
    assert named.t_record is not None and named.t_record.buy_asset == "Tether USD"
    assert from_lookalike.t_record is not None
    assert str(from_lookalike.t_record.t_type) == "_Possible-Spam"
    assert all(item.disposition != "possible-spam" for item in result.queue)
    assert all(item.asset != "Tether USD" for item in result.queue)
    reasons = {item.description for item in result.spam}
    assert any("not an official ethereum contract for USDT" in reason for reason in reasons)
    assert any("destination" in reason and POISON in reason for reason in reasons)
    assert any("source" in reason and POISON in reason for reason in reasons)

    spam_path = tmp_path / "records-possible-spam.csv"
    write_reconciliation_reports(
        result, str(tmp_path / "summary.csv"), str(tmp_path / "review.csv"), str(spam_path)
    )
    with open(spam_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert all("address-poisoning" in item["Description"] for item in rows)

    TransactionRow.skipped_spam_count = 0
    importer = ImportRecords()
    values = {column: "" for column in TransactionRow.HEADER}
    values["Type"] = "_Possible-Spam"
    values["Timestamp"] = "2024-06-01T12:00:00"
    values["Buy Quantity"] = "6"
    values["Buy Asset"] = "USDT"
    values["Wallet"] = "Ledger"
    imported = TransactionRow([values[column] for column in TransactionRow.HEADER], 2)
    importer._parse_imported_row(imported)  # pylint: disable=protected-access
    assert TransactionRow.skipped_spam_count == 1
    assert imported.t_record is None
    TransactionRow.skipped_spam_count = 0


def test_stake_contract_defaults_to_review() -> None:
    registry = _registry()
    registry.contracts = [AddressRule("Example stake", STAKE, "stake", "ethereum")]
    assert registry.tax_treatment.stake == "review"
    locked = _make(
        TrType.GIFT_SENT, BASE, "ETH", "2", "Ledger", destination=STAKE, chain="ethereum"
    )
    returned = _make(
        TrType.GIFT_RECEIVED, BASE, "ETH", "2", "Ledger", source=STAKE, chain="ethereum"
    )
    result = _run(locked, returned, registry=registry)
    assert locked.t_record is not None and locked.t_record.t_type == NEEDS_REVIEW_TYPE
    assert returned.t_record is not None and returned.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "configured option" in locked.t_record.note
    assert "not a confirmed rule" in locked.t_record.note
    assert {item.disposition for item in result.queue} == {"stake-lock", "stake-return"}
    assert locked.t_record.t_type is not TrType.SPEND
    assert returned.t_record.t_type is not TrType.DEPOSIT

    registry.tax_treatment.stake = "transfer"
    chosen = _make(
        TrType.GIFT_SENT, BASE, "ETH", "2", "Ledger", destination=STAKE, chain="ethereum"
    )
    _run(chosen, registry=registry)
    assert chosen.t_record is not None and chosen.t_record.t_type is TrType.WITHDRAWAL


def test_same_wallet_blank_destination_swap_is_off_unless_configured() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=SWAP_HASH)
    received = _make(TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=SWAP_HASH)
    held = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "same-wallet-swap" not in _actions(held)

    registry = _registry()
    registry.match.same_wallet_swaps = True
    out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=SWAP_HASH)
    incoming = _make(TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=SWAP_HASH)
    swapped = _run(out, incoming, registry=registry)
    assert out.t_record is not None and out.t_record.t_type is TrType.TRADE
    assert out.t_record.buy_asset == "USDC"
    assert out.t_record.buy_quantity == Decimal("100")
    assert incoming.t_record is None
    assert "same-wallet-swap" in _actions(swapped)
    assert "destination blank" in swapped.summary[0].detail

    lp_out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=SWAP_HASH)
    lp_in = _make(TrType.GIFT_RECEIVED, BASE, "CAKE-LP", "1", "Ledger", tx_hash=SWAP_HASH)
    reviewed = _run(lp_out, lp_in, registry=registry)
    assert lp_out.t_record is not None and lp_out.t_record.t_type == NEEDS_REVIEW_TYPE
    assert lp_in.t_record is not None and lp_in.t_record.t_type == NEEDS_REVIEW_TYPE
    assert {item.disposition for item in reviewed.queue} == {"lp-review"}

    other = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash=SWAP_HASH)
    elsewhere = _make(TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=SWAP_HASH)
    apart = _run(other, elsewhere, registry=registry)
    assert other.t_record is not None and other.t_record.t_type is not TrType.TRADE
    assert "same-wallet-swap" not in _actions(apart)


def test_same_wallet_swap_ignores_a_fee_leg() -> None:
    registry = _registry()
    registry.match.same_wallet_swaps = True
    fee_hash = "0x" + "ab" * 16
    fee = _make(TrType.GIFT_SENT, BASE, "SUI", "0.004", "Ledger", tx_hash=fee_hash)
    large = _make(TrType.GIFT_SENT, BASE, "USDC", "10000", "Ledger", tx_hash=fee_hash)
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "3.5", "Ledger", tx_hash=fee_hash)
    _run(fee, large, received, registry=registry)
    assert large.t_record is not None and large.t_record.t_type is TrType.TRADE
    assert large.t_record.buy_asset == "ETH"
    assert large.t_record.buy_quantity == Decimal("3.5")
    assert received.t_record is None
    assert fee.t_record is not None and fee.t_record.t_type is not TrType.TRADE

    other_hash = "0x" + "ef" * 16
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "5", "Ledger", tx_hash=other_hash)
    dust = _make(TrType.GIFT_RECEIVED, BASE, "SUI", "0.004", "Ledger", tx_hash=other_hash)
    bought = _make(TrType.GIFT_RECEIVED, BASE, "USDC", "8000", "Ledger", tx_hash=other_hash)
    _run(sent, dust, bought, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.TRADE
    assert sent.t_record.buy_asset == "USDC"
    assert sent.t_record.buy_quantity == Decimal("8000")
    assert bought.t_record is None
    assert dust.t_record is not None and dust.t_record.t_type is not TrType.TRADE


def test_usdt_ssui_swap_does_not_read_the_cleared_receive_leg() -> None:
    registry = _registry()
    registry.match.same_wallet_swaps = True
    tx_hash = "0x" + "58" * 16
    usdt = _make(TrType.GIFT_SENT, BASE, "USDT", "1000", "Ledger", tx_hash=tx_hash)
    fee = _make(TrType.GIFT_SENT, BASE, "SUI", "0.004", "Ledger", tx_hash=tx_hash)
    received = _make(TrType.GIFT_RECEIVED, BASE, "sSUI", "458", "Ledger", tx_hash=tx_hash)
    _run(usdt, fee, received, registry=registry)
    assert usdt.t_record is not None and usdt.t_record.t_type is TrType.TRADE
    assert usdt.t_record.sell_asset == "USDT"
    assert usdt.t_record.sell_quantity == Decimal("1000")
    assert usdt.t_record.buy_asset == "sSUI"
    assert usdt.t_record.buy_quantity == Decimal("458")
    assert received.t_record is None
    assert fee.t_record is not None and fee.t_record.t_type is not TrType.TRADE


def test_same_wallet_swaps_can_be_limited_to_a_wallet_or_chain() -> None:
    sui_hash = "0x" + "11" * 16
    eth_hash = "0x" + "22" * 16
    by_chain = _registry()
    by_chain.match.same_wallet_swaps = True
    by_chain.match.same_wallet_swap_chains = ("sui",)
    sui_out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=sui_hash, chain="sui")
    sui_in = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=sui_hash, chain="sui"
    )
    eth_out = _make(
        TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=eth_hash, chain="ethereum"
    )
    eth_in = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=eth_hash, chain="ethereum"
    )
    _run(sui_out, sui_in, eth_out, eth_in, registry=by_chain)
    assert sui_out.t_record is not None and sui_out.t_record.t_type is TrType.TRADE
    assert eth_out.t_record is not None and eth_out.t_record.t_type is not TrType.TRADE

    by_wallet = _registry()
    by_wallet.match.same_wallet_swaps = True
    by_wallet.match.same_wallet_swap_wallets = ("ledger",)
    kept = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=sui_hash, chain="sui")
    kept_in = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=sui_hash, chain="sui"
    )
    other = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Other", tx_hash=eth_hash, chain="sui")
    other_in = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Other", tx_hash=eth_hash, chain="sui"
    )
    _run(kept, kept_in, other, other_in, registry=by_wallet)
    assert kept.t_record is not None and kept.t_record.t_type is TrType.TRADE
    assert other.t_record is not None and other.t_record.t_type is not TrType.TRADE
