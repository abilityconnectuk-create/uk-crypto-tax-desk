from datetime import timedelta
from decimal import Decimal

from test_transfer_match import BASE, LEDGER, OTHER, PAYEE, _actions, _make, _payee, _registry, _run

from bittytax.bt_types import TrType
from bittytax.conv.datarow import DataRow
from bittytax.conv.transfer_match import NEEDS_REVIEW_TYPE
from bittytax.conv.txref import hashes_equal
from bittytax.conv.wallets import AddressRule, BridgeRoute, ForcePair, LeaveUnmatched, RegistryEntry

CHAIN = "0x" + "ab" * 32
LOOKALIKE = "USD\u0422"
VENUE = "0x00000000000000000000000000000000000000c1"
LP = "0x00000000000000000000000000000000000000d2"


def test_hash_ignores_case_and_a_0x_prefix() -> None:
    body = "a" * 16
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="0x" + body)
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.5", "Ledger", tx_hash=body.upper())
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT
    assert _actions(result) == ["matched"]
    assert "tx hash" in result.summary[0].detail


def test_short_exchange_id_is_not_the_same_hash_as_a_0x_prefix() -> None:
    assert not hashes_equal("88442211", "0x88442211")
    assert hashes_equal("0x" + "ab" * 8, "AB" * 8)
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash="88442211")
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.5", "Ledger", tx_hash="0x88442211")
    _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE


def test_exchange_id_pairs_with_a_chain_hash_inside_the_window() -> None:
    registry = _registry(own=[RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum")])
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Exchange",
        destination=LEDGER,
        tx_hash="88442211",
    )
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Ledger ETH", tx_hash=CHAIN)
    result = _run(sent, received, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT
    assert _actions(result) == ["matched-cex"]


def test_exchange_id_outside_the_cex_window_is_not_paired() -> None:
    registry = _registry(
        own=[
            RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum"),
            RegistryEntry(label="Example Exchange", account="Example Exchange"),
        ]
    )
    sent = _make(
        TrType.GIFT_SENT,
        BASE,
        "ETH",
        "1",
        "Example Exchange",
        destination=LEDGER,
        tx_hash="88442211",
    )
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=13),
        "ETH",
        "1",
        "Ledger ETH",
        tx_hash=CHAIN,
    )
    result = _run(sent, received, registry=registry)
    assert "matched-cex" not in _actions(result)
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL


def test_fuzzy_match_accepts_the_tolerance_and_window_edges() -> None:
    registry = _registry(
        own=[
            RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum"),
            RegistryEntry(label="Cold store", address=OTHER, chain="ethereum"),
        ]
    )
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH")
    received = _make(TrType.GIFT_RECEIVED, BASE + timedelta(hours=48), "ETH", "0.98", "Cold store")
    result = _run(sent, received, registry=registry)
    assert _actions(result) == ["matched-fuzzy"]
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL


def test_fuzzy_match_rejects_just_outside_the_edges() -> None:
    registry = _registry(
        own=[
            RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum"),
            RegistryEntry(label="Cold store", address=OTHER, chain="ethereum"),
        ]
    )
    late = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=48, seconds=1),
        "ETH",
        "1",
        "Cold store",
    )
    wide = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH")
    _run(wide, late, registry=registry)
    assert wide.t_record is not None and wide.t_record.t_type == NEEDS_REVIEW_TYPE

    over = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH")
    short = _make(TrType.GIFT_RECEIVED, BASE + timedelta(hours=36), "ETH", "0.979", "Cold store")
    _run(over, short, registry=registry)
    assert over.t_record is not None and over.t_record.t_type == NEEDS_REVIEW_TYPE


def test_amount_and_time_alone_do_not_fuzzy_match() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange")
    received = _make(TrType.GIFT_RECEIVED, BASE + timedelta(hours=36), "ETH", "1", "Ledger")
    _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert received.t_record is not None and received.t_record.t_type == NEEDS_REVIEW_TYPE


def test_bridge_without_a_receive_is_not_left_in_the_pool() -> None:
    sent = _make(
        TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", destination=OTHER, chain="ethereum"
    )
    sent.row_dict["Description"] = "bridge to arbitrum"
    result = _run(sent)
    assert sent.t_record is not None
    assert sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sent.t_record.t_type is not TrType.WITHDRAWAL
    assert "missing destination-chain deposit" in sent.t_record.note
    assert result.queue[0].disposition == "missing-destination"


def test_same_token_bridge_is_a_transfer_and_a_different_token_is_an_exchange() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", chain="ethereum")
    received = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Ledger", chain="arbitrum")
    sent.row_dict["Description"] = "bridge out"
    received.row_dict["Description"] = "bridge in"
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT
    assert "bridge-transfer" in _actions(result)

    out = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", chain="ethereum", value="100")
    incoming = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "1", "Ledger", chain="arbitrum", value="100"
    )
    out.row_dict["Description"] = "bridge out"
    incoming.row_dict["Description"] = "bridge in"
    swapped = _run(out, incoming)
    assert out.t_record is not None and out.t_record.t_type is TrType.TRADE
    assert out.t_record.buy_asset == "USDC"
    assert incoming.t_record is None
    assert "bridge-exchange" in _actions(swapped)


def test_bridge_takes_the_closest_unused_in_leg_with_a_compatible_amount() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "USDC", "1000", "Exchange", chain="ethereum")
    too_small = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=10),
        "USDC",
        "100",
        "Ledger",
        chain="arbitrum",
    )
    close = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=1),
        "USDC",
        "980",
        "Ledger",
        chain="arbitrum",
    )
    later = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=10),
        "USDC",
        "1000",
        "Ledger",
        chain="arbitrum",
    )
    for row in (sent, too_small, close, later):
        row.row_dict["Description"] = "bridge"
    result = _run(sent, too_small, close, later)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert close.t_record is not None and close.t_record.t_type is TrType.DEPOSIT
    assert too_small.t_record is not None and too_small.t_record.t_type == NEEDS_REVIEW_TYPE
    assert later.t_record is not None and later.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "bridge-transfer" in _actions(result)


def test_bridge_does_not_reuse_an_in_leg() -> None:
    first = _make(TrType.GIFT_SENT, BASE, "USDT", "1000", "Exchange", chain="ethereum")
    second = _make(
        TrType.GIFT_SENT,
        BASE + timedelta(minutes=1),
        "USDT",
        "1000",
        "Exchange",
        chain="ethereum",
    )
    incoming = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=30),
        "USDT",
        "1000",
        "Ledger",
        chain="arbitrum",
    )
    for row in (first, second, incoming):
        row.row_dict["Description"] = "bridge"
    result = _run(first, second, incoming)
    assert first.t_record is not None and first.t_record.t_type is TrType.WITHDRAWAL
    assert incoming.t_record is not None and incoming.t_record.t_type is TrType.DEPOSIT
    assert second.t_record is not None and second.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result).count("bridge-transfer") == 1


def test_bridge_keeps_usdt_and_usdc_apart_and_a_token_change_is_an_exchange() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "USDC", "1000", "Exchange", chain="ethereum")
    other = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=20),
        "USDT",
        "1000",
        "Ledger",
        chain="arbitrum",
    )
    same = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=2),
        "USDC",
        "1000",
        "Ledger",
        chain="arbitrum",
    )
    for row in (sent, other, same):
        row.row_dict["Description"] = "bridge"
    result = _run(sent, other, same)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert same.t_record is not None and same.t_record.t_type is TrType.DEPOSIT
    assert other.t_record is not None and other.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "bridge-transfer" in _actions(result)
    assert "bridge-exchange" not in _actions(result)

    changed = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", chain="ethereum", value="100")
    bought = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=1),
        "USDC",
        "1",
        "Ledger",
        chain="arbitrum",
        value="100",
    )
    changed.row_dict["Description"] = "bridge out"
    bought.row_dict["Description"] = "bridge in"
    swapped = _run(changed, bought)
    assert changed.t_record is not None and changed.t_record.t_type is TrType.TRADE
    assert changed.t_record.buy_asset == "USDC"
    assert "bridge-exchange" in _actions(swapped)


def _without_chain(row: DataRow) -> DataRow:
    row.row_dict.pop("Chain", None)
    return row


def test_bridge_matches_on_value_when_the_input_has_no_chain_column() -> None:
    registry = _registry(
        own=[
            RegistryEntry(label="Exchange", account="Exchange", chain="ethereum"),
            RegistryEntry(label="Ledger", account="Ledger", chain="arbitrum"),
        ]
    )
    first = _without_chain(
        _make(
            TrType.GIFT_SENT,
            BASE,
            "USDC",
            "1000",
            "Exchange",
            tx_hash="0x" + "b1" * 16,
            value="1000",
        )
    )
    other = _without_chain(
        _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(minutes=5),
            "USDT",
            "1000",
            "Ledger",
            tx_hash="0x" + "b3" * 16,
            value="1000",
        )
    )
    close = _without_chain(
        _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(minutes=30),
            "USDC",
            "900",
            "Ledger",
            tx_hash="0x" + "b4" * 16,
            value="985",
        )
    )
    later = _without_chain(
        _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(hours=10),
            "USDC",
            "1000",
            "Ledger",
            tx_hash="0x" + "b5" * 16,
            value="1000",
        )
    )
    first.row_dict["Description"] = "bridge out"
    result = _run(first, other, close, later, registry=registry)
    assert first.t_record is not None and first.t_record.t_type is TrType.WITHDRAWAL
    assert close.t_record is not None and close.t_record.t_type is TrType.DEPOSIT
    assert other.t_record is not None and other.t_record.t_type == NEEDS_REVIEW_TYPE
    assert later.t_record is not None and later.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(result).count("bridge-transfer") == 1
    assert "bridge-exchange" not in _actions(result)

    again = _without_chain(
        _make(
            TrType.GIFT_SENT,
            BASE,
            "USDC",
            "1000",
            "Exchange",
            tx_hash="0x" + "b2" * 16,
            value="1000",
        )
    )
    extra = _without_chain(
        _make(
            TrType.GIFT_SENT,
            BASE + timedelta(minutes=1),
            "USDC",
            "1000",
            "Exchange",
            tx_hash="0x" + "b8" * 16,
            value="1000",
        )
    )
    only = _without_chain(
        _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(minutes=20),
            "USDC",
            "900",
            "Ledger",
            tx_hash="0x" + "b9" * 16,
            value="990",
        )
    )
    again.row_dict["Description"] = "bridge out"
    extra.row_dict["Description"] = "bridge out"
    reused = _run(again, extra, only, registry=registry)
    assert again.t_record is not None and again.t_record.t_type is TrType.WITHDRAWAL
    assert only.t_record is not None and only.t_record.t_type is TrType.DEPOSIT
    assert extra.t_record is not None and extra.t_record.t_type == NEEDS_REVIEW_TYPE
    assert _actions(reused).count("bridge-transfer") == 1

    changed = _without_chain(
        _make(
            TrType.GIFT_SENT,
            BASE,
            "ETH",
            "0.5",
            "Exchange",
            tx_hash="0x" + "b6" * 16,
            value="2000",
        )
    )
    bought = _without_chain(
        _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(hours=1),
            "USDC",
            "1500",
            "Ledger",
            tx_hash="0x" + "b7" * 16,
            value="1980",
        )
    )
    changed.row_dict["Description"] = "bridge out"
    swapped = _run(changed, bought, registry=registry)
    assert changed.t_record is not None and changed.t_record.t_type is TrType.TRADE
    assert changed.t_record.buy_asset == "USDC"
    assert "bridge-exchange" in _actions(swapped)


def test_usdt_to_usdc_bridge_is_an_exchange_and_same_asset_still_wins() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "USDT", "1500", "Exchange", chain="ethereum", value="1500")
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=2),
        "USDC",
        "1490",
        "Ledger",
        chain="arbitrum",
        value="1490",
    )
    sent.row_dict["Description"] = "bridge out"
    result = _run(sent, received)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.TRADE
    assert sent.t_record.buy_asset == "USDC"
    assert sent.t_record.buy_quantity == Decimal("1490")
    assert received.t_record is None
    assert "bridge-exchange" in _actions(result)

    out = _make(TrType.GIFT_SENT, BASE, "USDT", "1500", "Exchange", chain="ethereum", value="1500")
    same = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(minutes=30),
        "USDT",
        "1495",
        "Ledger",
        chain="arbitrum",
        value="1495",
    )
    other = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=3),
        "USDC",
        "1490",
        "Ledger",
        chain="arbitrum",
        value="1490",
    )
    out.row_dict["Description"] = "bridge out"
    preferred = _run(out, same, other)
    assert out.t_record is not None and out.t_record.t_type is TrType.WITHDRAWAL
    assert same.t_record is not None and same.t_record.t_type is TrType.DEPOSIT
    assert other.t_record is not None and other.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "bridge-transfer" in _actions(preferred)
    assert "bridge-exchange" not in _actions(preferred)


def test_token_change_ignores_a_receive_with_no_value() -> None:
    for symbol in ("CHARLIE", "VECARV", "X402"):
        sent = _make(
            TrType.GIFT_SENT,
            BASE,
            "USDT",
            "995.53",
            "Exchange",
            chain="ethereum",
            value="995.53",
        )
        incoming = _make(
            TrType.GIFT_RECEIVED,
            BASE + timedelta(hours=1),
            symbol,
            "1000",
            "Ledger",
            chain="arbitrum",
            value="0",
        )
        sent.row_dict["Description"] = "bridge out"
        result = _run(sent, incoming)
        assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
        assert sent.t_record.t_type is not TrType.TRADE
        assert "missing destination-chain deposit" in sent.t_record.note
        assert result.queue[0].disposition == "missing-destination"
        assert incoming.t_record is not None and incoming.t_record.buy_asset == symbol

    marked = _make(
        TrType.GIFT_SENT, BASE, "USDT", "995.53", "Exchange", chain="ethereum", value="995.53"
    )
    token = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=1),
        "TEDDY BEARS #12",
        "1",
        "Ledger",
        chain="arbitrum",
        value="995.53",
    )
    marked.row_dict["Description"] = "bridge out"
    held = _run(marked, token)
    assert marked.t_record is not None and marked.t_record.t_type == NEEDS_REVIEW_TYPE
    assert marked.t_record.t_type is not TrType.TRADE
    assert held.queue[0].disposition == "missing-destination"


def test_payee_stays_a_disposal() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", destination=PAYEE)
    result = _run(sent, registry=_registry(payees=[_payee()]))
    assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
    assert _actions(result) == ["payee-disposal"]


def test_futures_leg_is_margin_not_a_chain_transfer() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "BTCUSDTM", "1", "Exchange")
    sent.row_dict["Description"] = "bot daily total"
    result = _run(sent)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.MARGIN_LOSS
    assert _actions(result) == ["margin-internal"]
    assert result.queue == []


def test_fiat_movement_redacts_the_account_number() -> None:
    iban = "GB00EXAM00000000000001"
    sent = _make(TrType.GIFT_SENT, BASE, "GBP", "50", "Bank", destination=iban)
    sent.row_dict["Description"] = "bank 12345678"
    result = _run(sent)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert _actions(result) == ["fiat"]
    blob = f"{result.summary[0].destination} {result.summary[0].detail} {sent.t_record.note}"
    assert iban not in blob
    assert "12345678" not in blob
    assert "[redacted]" in result.summary[0].destination


def test_zero_value_lookalike_is_not_merged_into_usdt() -> None:
    registry = _registry(groups=[["USDT", "USDT.E"]], share_pools=True)
    sent = _make(TrType.GIFT_SENT, BASE, "USDT.e", "1", "Exchange", destination=OTHER)
    result = _run(sent, registry=registry)
    assert sent.t_record is not None
    assert str(sent.t_record.t_type) == "_Possible-Spam"
    assert sent.t_record.sell_asset != "USDT"
    assert result.spam[0].disposition == "possible-spam"

    priced = _make(
        TrType.GIFT_SENT, BASE, LOOKALIKE, "1", "Exchange", destination=OTHER, value="12"
    )
    registry = _registry(groups=[["USDT", LOOKALIKE.upper()]], share_pools=True)
    reviewed = _run(priced, registry=registry)
    assert priced.t_record is not None
    assert priced.t_record.t_type == NEEDS_REVIEW_TYPE
    assert priced.t_record.sell_asset == LOOKALIKE
    assert reviewed.queue[0].disposition == "lookalike"


def test_utilities_and_an_unconfirmed_lp_rule() -> None:
    income = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "0.1", "Exchange")
    income.row_dict["Description"] = "launchpool reward"
    dust = _make(TrType.GIFT_SENT, BASE, "ETH", "0.0001", "Exchange", destination=OTHER)
    dust.row_dict["Description"] = "dust conversion"
    ignored = _make(TrType.GIFT_SENT, BASE, "SOL", "0.01", "Exchange", destination=OTHER)
    ignored.row_dict["Description"] = "create-token-account"
    lp = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", destination=LP, chain="ethereum")
    registry = _registry()
    registry.contracts = [AddressRule("Example LP", LP, "lp", "ethereum")]
    result = _run(income, dust, ignored, lp, registry=registry)
    assert income.t_record is not None and income.t_record.t_type is TrType.INCOME
    assert dust.t_record is not None and dust.t_record.t_type is TrType.SPEND
    assert ignored.t_record is not None and str(ignored.t_record.t_type) == "_Ignored"
    assert lp.t_record is not None and lp.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "configured option" in lp.t_record.note
    assert "utility-income" in _actions(result)
    assert "utility-dust" in _actions(result)
    assert "utility-ignore" in _actions(result)
    assert any(item.disposition == "lp-add" for item in result.queue)


def test_transfer_to_a_configured_venue() -> None:
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", destination=VENUE)
    registry = _registry()
    registry.venues = [AddressRule("Example CEX deposit", VENUE, "transfer-to-venue")]
    result = _run(sent, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert "transfer-to-venue" in _actions(result)


def test_leave_unmatched_blocks_swap_bridge_and_fuzzy() -> None:
    swap_hash = "0x" + "51" * 32
    fuzzy_hash = "0x" + "52" * 32
    usdt_hash = "0x" + "53" * 32
    eth_hash = "0x" + "54" * 32
    registry = _registry(
        own=[
            RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum"),
            RegistryEntry(label="Cold store", address=OTHER, chain="ethereum"),
        ]
    )
    registry.match.same_wallet_swaps = True
    registry.match_overrides.leave_unmatched = [
        LeaveUnmatched(swap_hash),
        LeaveUnmatched(fuzzy_hash),
        LeaveUnmatched(usdt_hash, "USDT"),
    ]

    sold = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger", tx_hash=swap_hash, value="100")
    bought = _make(
        TrType.GIFT_RECEIVED, BASE, "USDC", "100", "Ledger", tx_hash=swap_hash, value="100"
    )
    swapped = _run(sold, bought, registry=registry)
    assert sold.t_record is not None and sold.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sold.t_record.t_type is not TrType.TRADE
    assert "leave-unmatched" in _actions(swapped)
    assert "same-wallet-swap" not in _actions(swapped)

    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH", tx_hash=fuzzy_hash)
    received = _make(
        TrType.GIFT_RECEIVED, BASE + timedelta(hours=1), "ETH", "1", "Cold store", tx_hash="0xfuzz"
    )
    fuzzy = _run(sent, received, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sent.t_record.t_type is not TrType.WITHDRAWAL
    assert "leave-unmatched" in _actions(fuzzy)
    assert "matched-fuzzy" not in _actions(fuzzy)
    assert "matched" not in _actions(fuzzy)

    held = _make(
        TrType.GIFT_SENT, BASE, "USDT", "1000", "Exchange", chain="ethereum", tx_hash=usdt_hash
    )
    held_in = _make(
        TrType.GIFT_RECEIVED, BASE, "USDT", "1000", "Ledger", chain="arbitrum", tx_hash="0xusdtin"
    )
    free = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", chain="ethereum", tx_hash=eth_hash)
    free_in = _make(
        TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Ledger", chain="arbitrum", tx_hash="0xethin"
    )
    for row in (held, held_in, free, free_in):
        row.row_dict["Description"] = "bridge"
    bridged = _run(held, held_in, free, free_in, registry=registry)
    assert held.t_record is not None and held.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "missing destination-chain deposit" in held.t_record.note
    assert free.t_record is not None and free.t_record.t_type is TrType.WITHDRAWAL
    assert free_in.t_record is not None and free_in.t_record.t_type is TrType.DEPOSIT
    assert "leave-unmatched" in _actions(bridged)
    assert _actions(bridged).count("bridge-transfer") == 1


def test_force_pair_writes_a_transfer_or_an_exchange() -> None:
    out_hash = "0x" + "61" * 32
    in_hash = "0x" + "62" * 32
    trade_out = "0x" + "63" * 32
    trade_in = "0x" + "64" * 32
    blocked_out = "0x" + "71" * 32
    blocked_in = "0x" + "72" * 32
    registry = _registry(
        own=[
            RegistryEntry(label="Ledger ETH", address=LEDGER, chain="ethereum"),
            RegistryEntry(label="Cold store", address=OTHER, chain="ethereum"),
        ]
    )
    registry.match_overrides.force_pair = [
        ForcePair(out_hash, in_hash, "transfer"),
        ForcePair(trade_out, trade_in, "exchange"),
    ]
    sent = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Exchange", tx_hash=out_hash)
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(days=10),
        "ETH",
        "9",
        "Ledger",
        tx_hash=in_hash,
    )
    moved = _run(sent, received, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type is TrType.WITHDRAWAL
    assert received.t_record is not None and received.t_record.t_type is TrType.DEPOSIT
    assert "override-transfer" in _actions(moved)
    assert "match override" in moved.summary[0].detail

    sold = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH", tx_hash=trade_out)
    bought = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Cold store", tx_hash=trade_in)
    traded = _run(sold, bought, registry=registry)
    assert sold.t_record is not None and sold.t_record.t_type is TrType.TRADE
    assert sold.t_record.buy_asset == "ETH"
    assert bought.t_record is None
    assert "override-exchange" in _actions(traded)
    assert "matched" not in _actions(traded)
    assert "matched-fuzzy" not in _actions(traded)

    registry.match_overrides.leave_unmatched = [LeaveUnmatched(blocked_out)]
    registry.match_overrides.force_pair.append(ForcePair(blocked_out, blocked_in, "transfer"))
    blocked = _make(TrType.GIFT_SENT, BASE, "ETH", "1", "Ledger ETH", tx_hash=blocked_out)
    other = _make(TrType.GIFT_RECEIVED, BASE, "ETH", "1", "Cold store", tx_hash=blocked_in)
    refused = _run(blocked, other, registry=registry)
    assert blocked.t_record is not None and blocked.t_record.t_type == NEEDS_REVIEW_TYPE
    assert "leave-unmatched" in _actions(refused)
    assert "override-transfer" not in _actions(refused)


def test_same_asset_only_route_leaves_a_missing_receive() -> None:
    registry = _registry()
    registry.bridges.routes.append(BridgeRoute("mayan", True, ("mayan",)))
    sent = _make(TrType.GIFT_SENT, BASE, "USDT", "1500", "Exchange", chain="ethereum", value="1500")
    received = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=2),
        "USDC",
        "1490",
        "Ledger",
        chain="arbitrum",
        value="1490",
    )
    sent.row_dict["Description"] = "Mayan USDT"
    missed = _run(sent, received, registry=registry)
    assert sent.t_record is not None and sent.t_record.t_type == NEEDS_REVIEW_TYPE
    assert sent.t_record.t_type is not TrType.TRADE
    assert "missing destination-chain deposit" in sent.t_record.note
    assert missed.queue[0].disposition == "missing-destination"
    assert "bridge-exchange" not in _actions(missed)

    same = _make(TrType.GIFT_SENT, BASE, "USDT", "1500", "Exchange", chain="ethereum", value="1500")
    same_in = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=1),
        "USDT",
        "1490",
        "Ledger",
        chain="arbitrum",
        value="1490",
    )
    same.row_dict["Description"] = "Mayan USDT"
    kept = _run(same, same_in, registry=registry)
    assert same.t_record is not None and same.t_record.t_type is TrType.WITHDRAWAL
    assert same_in.t_record is not None and same_in.t_record.t_type is TrType.DEPOSIT
    assert "bridge-transfer" in _actions(kept)

    plain = _make(
        TrType.GIFT_SENT, BASE, "USDT", "1500", "Exchange", chain="ethereum", value="1500"
    )
    changed = _make(
        TrType.GIFT_RECEIVED,
        BASE + timedelta(hours=2),
        "USDC",
        "1490",
        "Ledger",
        chain="arbitrum",
        value="1490",
    )
    plain.row_dict["Description"] = "bridge out"
    swapped = _run(plain, changed, registry=registry)
    assert plain.t_record is not None and plain.t_record.t_type is TrType.TRADE
    assert plain.t_record.buy_asset == "USDC"
    assert "bridge-exchange" in _actions(swapped)
