# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2022

import copy
import re
import sys
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Optional, Union

from colorama import Fore
from typing_extensions import Unpack

from ...bt_types import TrType, UnmappedType
from ...config import config
from ..dataparser import DataParser, ParserArgs, ParserType
from ..datarow import TxRawPos
from ..exceptions import DataRowError, UnexpectedTypeError
from ..out_record import TransactionOutRecord

if TYPE_CHECKING:
    from ..datarow import DataRow

KOINLY_D_MAPPING = {
    "": TrType.GIFT_RECEIVED,
    "Airdrop": TrType.AIRDROP,
    "airdrop": TrType.AIRDROP,
    "Fork": TrType.FORK,
    "fork": TrType.FORK,
    "Mining": TrType.MINING,
    "mining": TrType.MINING,
    "Reward": TrType.STAKING_REWARD,
    "reward": TrType.STAKING_REWARD,
    "Income": TrType.INCOME,
    "income": TrType.INCOME,
    "Other income": TrType.INCOME,
    "other_income": TrType.INCOME,
    "Lending interest": TrType.INTEREST,
    "lending_interest": TrType.INTEREST,
    "Cashback": TrType.CASHBACK,
    "cashback": TrType.CASHBACK,
    "Salary": TrType.INCOME,
    "salary": TrType.INCOME,
    "Fee refund": TrType.FEE_REBATE,
    "fee_refund": TrType.FEE_REBATE,
    "Loan": TrType.LOAN,
    "loan": TrType.LOAN,
    "Margin loan": TrType.LOAN,
    "margin_loan": TrType.LOAN,
    "Realized gain": TrType.MARGIN_GAIN,
    "realized_gain": TrType.MARGIN_GAIN,
    # Removing liquidity is an acquisition at the Koinly value, not income.
    # Confirm with an accountant: this assumes the matching liquidity-in was a disposal.
    "Liquidity out": TrType.GIFT_RECEIVED,
    "liquidity_out": TrType.GIFT_RECEIVED,
    # The LP-token leg of a liquidity-in is a deposit. Map it as well as the sent leg.
    "Liquidity in": TrType.GIFT_RECEIVED,
    "liquidity_in": TrType.GIFT_RECEIVED,
    "Multi trade": TrType.GIFT_RECEIVED,
    "multi_trade": TrType.GIFT_RECEIVED,
}

KOINLY_W_MAPPING = {
    "": TrType.GIFT_SENT,
    "Gift": TrType.GIFT_SENT,
    "gift": TrType.GIFT_SENT,
    "Lost": TrType.LOST,
    "lost": TrType.LOST,
    "Donation": TrType.CHARITY_SENT,
    "donation": TrType.CHARITY_SENT,
    "Cost": TrType.SPEND,
    "cost": TrType.SPEND,
    "Loan fee": TrType.LOAN_INTEREST,
    "loan_fee": TrType.LOAN_INTEREST,
    "Margin fee": TrType.MARGIN_FEE,
    "margin_fee": TrType.MARGIN_FEE,
    "Loan repayment": TrType.LOAN_REPAYMENT,
    "loan_repayment": TrType.LOAN_REPAYMENT,
    "Margin repayment": TrType.LOAN_REPAYMENT,
    "margin_repayment": TrType.LOAN_REPAYMENT,
    "Realized gain": TrType.MARGIN_LOSS,
    "realized_gain": TrType.MARGIN_LOSS,
    # Adding liquidity and standalone fees are disposals. Liquidity-pool tax
    # treatment is not settled; confirm before relying on these rows.
    "Liquidity in": TrType.SPEND,
    "liquidity_in": TrType.SPEND,
    # Removing the LP token is a withdrawal. Map it as well as the tokens received.
    "Liquidity out": TrType.SPEND,
    "liquidity_out": TrType.SPEND,
    "Other fee": TrType.SPEND,
    "other_fee": TrType.SPEND,
    "Multi trade": TrType.SPEND,
    "multi_trade": TrType.SPEND,
}


def _clean_number(value: str) -> str:
    """Drop thousands separators such as 1,234.56. Leave other text unchanged."""
    text = value.strip().replace(" ", "").replace("\u00a0", "")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text):
        return text.replace(",", "")
    return text


def _koinly_decimal(value: str) -> Decimal:
    return Decimal(_clean_number(value))


def _fold_tag(tag: str) -> str:
    return " ".join(tag.replace("_", " ").split()).casefold()


def _lookup_tag(mapping: Dict[str, TrType], tag: str) -> Optional[TrType]:
    if tag in mapping:
        return mapping[tag]
    folded = _fold_tag(tag)
    for key, value in mapping.items():
        if _fold_tag(key) == folded:
            return value
    return None


def _local_fiat_value(
    asset: str, quantity: Optional[Decimal], converted: Optional[Decimal]
) -> Optional[Decimal]:
    # After foreign exchange, a pound amount's converted value often no longer equals the quantity.
    # The quantity of the local currency is the value.
    if quantity is not None and asset == config.ccy:
        return quantity
    return converted


_MAPPED_TAG_NOTES = {
    "liquidity in",
    "liquidity out",
    "multi trade",
    "other fee",
}


def _tag_note(note: str, tag: str) -> str:
    if _fold_tag(tag) not in _MAPPED_TAG_NOTES:
        return note
    marker = f"Koinly tag: {tag}"
    if marker in note:
        return note
    return f"{note} | {marker}" if note else marker


def parse_koinly(
    data_rows: List["DataRow"], parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    currency = parser.args[2].group(1)

    for row_index, data_row in enumerate(data_rows):
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f"row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_koinly_row(data_rows, parser, data_row, row_index, currency)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_koinly_row(
    data_rows: List["DataRow"],
    parser: DataParser,
    data_row: "DataRow",
    row_index: int,
    currency: str,
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date"])
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TxHash"),
        parser.in_header.index("TxSrc"),
        parser.in_header.index("TxDest"),
    )
    data_row.parsed = True

    if "Label" in row_dict:
        row_dict["Tag"] = row_dict["Label"]

    if row_dict["Fee Amount"]:
        fee_quantity = _koinly_decimal(row_dict["Fee Amount"])
    else:
        fee_quantity = None

    if row_dict[f"Fee Value ({currency})"]:
        fee_value = DataParser.convert_currency(
            _clean_number(row_dict[f"Fee Value ({currency})"]), currency, data_row.timestamp
        )
    else:
        fee_value = None
    fee_value = _local_fiat_value(row_dict["Fee Currency"], fee_quantity, fee_value)

    net_value = DataParser.convert_currency(
        _clean_number(row_dict[f"Net Value ({currency})"]), currency, data_row.timestamp
    )

    sent_quantity = _koinly_decimal(row_dict["Sent Amount"]) if row_dict["Sent Amount"] else None
    received_quantity = (
        _koinly_decimal(row_dict["Received Amount"]) if row_dict["Received Amount"] else None
    )
    tag = row_dict["Tag"]
    note = _tag_note(row_dict["Description"], tag)

    if row_dict["Type"] in ("buy", "sell", "exchange") or (
        _fold_tag(tag) == "multi trade" and sent_quantity and received_quantity
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=received_quantity,
            buy_asset=row_dict["Received Currency"],
            buy_value=_local_fiat_value(
                row_dict["Received Currency"], received_quantity, net_value
            ),
            sell_quantity=sent_quantity,
            sell_asset=row_dict["Sent Currency"],
            sell_value=_local_fiat_value(row_dict["Sent Currency"], sent_quantity, net_value),
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"] or row_dict["Receiving Wallet"],
            note=note,
        )
    elif row_dict["Type"] == "transfer":
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=sent_quantity,
            sell_asset=row_dict["Sent Currency"],
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"],
            note=note,
        )
        dup_data_row = copy.copy(data_row)
        dup_data_row.row = []
        dup_data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=received_quantity,
            buy_asset=row_dict["Received Currency"],
            wallet=row_dict["Receiving Wallet"],
            note=note,
        )
        data_rows.insert(row_index + 1, dup_data_row)
    elif row_dict["Type"] in ("fiat_deposit", "crypto_deposit"):
        mapped = _lookup_tag(KOINLY_D_MAPPING, tag)
        if mapped is not None:
            t_type: Union[TrType, UnmappedType] = mapped
        else:
            t_type = UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            buy_quantity=received_quantity,
            buy_asset=row_dict["Received Currency"],
            buy_value=_local_fiat_value(
                row_dict["Received Currency"], received_quantity, net_value
            ),
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Receiving Wallet"],
            note=note,
        )
    elif row_dict["Type"] in ("fiat_withdrawal", "crypto_withdrawal"):
        mapped_withdrawal = _lookup_tag(KOINLY_W_MAPPING, tag)
        if mapped_withdrawal is not None:
            t_type = mapped_withdrawal
        else:
            t_type = UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            sell_quantity=sent_quantity,
            sell_asset=row_dict["Sent Currency"],
            sell_value=_local_fiat_value(row_dict["Sent Currency"], sent_quantity, net_value),
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"],
            note=note,
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Type"), "Type", row_dict["Type"])


DataParser(
    ParserType.ACCOUNTING,
    "Koinly",
    [
        "Date",
        "Type",
        lambda h: h in ("Label", "Tag"),
        "Sending Wallet",
        "Sent Amount",
        "Sent Currency",
        "Sent Cost Basis",
        "Receiving Wallet",
        "Received Amount",
        "Received Currency",
        "Received Cost Basis",
        "Fee Amount",
        "Fee Currency",
        lambda h: re.match(r"Gain \((\w{3})\)", h),
        lambda h: re.match(r"Net Value \((\w{3})\)", h),
        lambda h: re.match(r"Fee Value \((\w{3})\)", h),
        "TxSrc",
        "TxDest",
        "TxHash",
        "Description",
    ],
    worksheet_name="Koinly",
    all_handler=parse_koinly,
)


# Koinly "Bulk edit in Excel" transactions export, see:
# https://support.koinly.io/en/articles/9490043-bulk-edit-in-excel
# One row per transaction, using From/To columns instead of the Sent/Received columns of the
# tax report export above. The direction is taken from the populated side: "To" only is a
# deposit, "From" only is a withdrawal, both is a trade. Currency and wallet cells carry a
# ";<id>" suffix (e.g. "WFLR;9546698") which is removed. The Tag column drives the type for
# deposits and withdrawals, an untagged transfer defaults to Deposit/Withdrawal.


def _strip_koinly_id(value: str) -> str:
    return value.split(";", 1)[0].strip()


def _get_koinly_value(amount: str, currency: str, timestamp: datetime) -> Optional[Decimal]:
    cleaned = _clean_number(amount)
    if cleaned and _koinly_decimal(cleaned) != 0:
        return DataParser.convert_currency(cleaned, _strip_koinly_id(currency), timestamp)
    return None


def parse_koinly_bulk_edit(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date (UTC)"])
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TxHash"),
        parser.in_header.index("TxSrc"),
        parser.in_header.index("TxDest"),
    )

    tag = row_dict["Tag"]
    from_amount = _clean_number(row_dict["From Amount"])
    to_amount = _clean_number(row_dict["To Amount"])
    has_from = bool(from_amount) and _koinly_decimal(from_amount) != 0
    has_to = bool(to_amount) and _koinly_decimal(to_amount) != 0

    value = _get_koinly_value(
        row_dict["Net Value (read-only)"],
        row_dict["Value Currency (read-only)"],
        data_row.timestamp,
    ) or _get_koinly_value(
        row_dict["Net Worth Amount"], row_dict["Net Worth Currency"], data_row.timestamp
    )

    if row_dict["Fee Amount"]:
        fee_quantity = _koinly_decimal(row_dict["Fee Amount"])
        fee_asset = _strip_koinly_id(row_dict["Fee Currency"])
    else:
        fee_quantity = None
        fee_asset = ""

    fee_value = _get_koinly_value(
        row_dict["Fee Value (read-only)"],
        row_dict["Value Currency (read-only)"],
        data_row.timestamp,
    ) or _get_koinly_value(
        row_dict["Fee Worth Amount"], row_dict["Fee Worth Currency"], data_row.timestamp
    )
    fee_value = _local_fiat_value(fee_asset, fee_quantity, fee_value)
    note = _tag_note(row_dict["Description"], tag)
    from_quantity = _koinly_decimal(from_amount) if has_from else None
    to_quantity = _koinly_decimal(to_amount) if has_to else None
    from_asset = _strip_koinly_id(row_dict["From Currency"])
    to_asset = _strip_koinly_id(row_dict["To Currency"])

    if has_from and has_to:
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=to_quantity,
            buy_asset=to_asset,
            buy_value=_local_fiat_value(to_asset, to_quantity, value),
            sell_quantity=from_quantity,
            sell_asset=from_asset,
            sell_value=_local_fiat_value(from_asset, from_quantity, value),
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_strip_koinly_id(row_dict["To Wallet (read-only)"]),
            note=note,
        )
    elif has_to:
        if tag == "":
            t_type: Union[TrType, UnmappedType] = TrType.DEPOSIT
        else:
            mapped = _lookup_tag(KOINLY_D_MAPPING, tag)
            t_type = mapped if mapped is not None else UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            buy_quantity=to_quantity,
            buy_asset=to_asset,
            buy_value=_local_fiat_value(to_asset, to_quantity, value),
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_strip_koinly_id(row_dict["To Wallet (read-only)"]),
            note=note,
        )
    elif has_from:
        if tag == "":
            t_type = TrType.WITHDRAWAL
        else:
            mapped_withdrawal = _lookup_tag(KOINLY_W_MAPPING, tag)
            t_type = mapped_withdrawal if mapped_withdrawal is not None else UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            sell_quantity=from_quantity,
            sell_asset=from_asset,
            sell_value=_local_fiat_value(from_asset, from_quantity, value),
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_strip_koinly_id(row_dict["From Wallet (read-only)"]),
            note=note,
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Type"), "Type", row_dict["Type"])


DataParser(
    ParserType.ACCOUNTING,
    "Koinly",
    [
        "ID (read-only)",
        "Parent ID (read-only)",
        "Date (UTC)",
        "Type",
        "Tag",
        "From Wallet (read-only)",
        "From Wallet ID",
        "From Amount",
        "From Currency",
        "To Wallet (read-only)",
        "To Wallet ID",
        "To Amount",
        "To Currency",
        "Fee Amount",
        "Fee Currency",
        "Net Worth Amount",
        "Net Worth Currency",
        "Fee Worth Amount",
        "Fee Worth Currency",
        "Net Value (read-only)",
        "Fee Value (read-only)",
        "Value Currency (read-only)",
        "Deleted",
        "From Source (read-only)",
        "To Source (read-only)",
        "Negative Balances (read-only)",
        "Missing Rates (read-only)",
        "Missing Cost Basis (read-only)",
        "Synced To Accounting At (UTC read-only)",
        "TxSrc",
        "TxDest",
        "TxHash",
        "Description",
    ],
    worksheet_name="Koinly",
    row_handler=parse_koinly_bulk_edit,
)
