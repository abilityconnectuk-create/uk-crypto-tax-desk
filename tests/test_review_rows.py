import pytest

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.exceptions import UnexpectedTransactionTypeError
from bittytax.t_row import TransactionRow


def _parse(t_type: str, **cells: str) -> TransactionRow:
    values = {column: "" for column in TransactionRow.HEADER}
    values["Type"] = t_type
    values["Timestamp"] = "2024-06-01T12:00:00"
    values.update(cells)
    row = TransactionRow([values[column] for column in TransactionRow.HEADER], 2)
    row.parse()
    return row


def test_needs_review_is_rejected_by_default() -> None:
    previous = config.review_rows
    config.config["review_rows"] = "reject"
    TransactionRow.review_assumed_count = 0
    try:
        with pytest.raises(UnexpectedTransactionTypeError):
            _parse("_Needs-Review", **{"Sell Quantity": "1", "Sell Asset": "ETH"})
        assert TransactionRow.review_assumed_count == 0
    finally:
        config.config["review_rows"] = previous
        TransactionRow.review_assumed_count = 0


def test_review_rows_disposal_and_transfer() -> None:
    previous = config.review_rows
    TransactionRow.review_assumed_count = 0
    try:
        config.config["review_rows"] = "disposal"
        sent = _parse("_Needs-Review", **{"Sell Quantity": "1", "Sell Asset": "ETH"})
        received = _parse("_Needs-Review", **{"Buy Quantity": "2", "Buy Asset": "BTC"})
        assert sent.t_record is not None and sent.t_record.t_type is TrType.GIFT_SENT
        assert received.t_record is not None and received.t_record.t_type is TrType.GIFT_RECEIVED
        assert TransactionRow.review_assumed_count == 2

        config.config["review_rows"] = "transfer"
        moved = _parse("_Needs-Review", **{"Sell Quantity": "1", "Sell Asset": "ETH"})
        assert moved.t_record is not None and moved.t_record.t_type is TrType.WITHDRAWAL
        assert TransactionRow.review_assumed_count == 3
    finally:
        config.config["review_rows"] = previous
        TransactionRow.review_assumed_count = 0


def test_note_marks_a_row_already_rewritten_by_the_converter() -> None:
    previous = config.review_rows
    config.config["review_rows"] = "reject"
    TransactionRow.review_assumed_count = 0
    try:
        row = _parse(
            "Gift-Sent",
            **{
                "Sell Quantity": "1",
                "Sell Asset": "ETH",
                "Note": "review row treated as disposal",
            },
        )
        assert row.t_record is not None and row.t_record.t_type is TrType.GIFT_SENT
        assert TransactionRow.review_assumed_count == 1
    finally:
        config.config["review_rows"] = previous
        TransactionRow.review_assumed_count = 0
