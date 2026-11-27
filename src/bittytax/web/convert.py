# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
# pylint: disable=protected-access
"""Turn an exchange export into BittyTax rows, using the conversion tool."""

from __future__ import annotations

import argparse
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from ..bt_types import TrType
from ..config import config
from ..conv.bittytax_conv import _do_read_file, _get_file_info
from ..conv.datafile import DataFile
from ..conv.datamerge import DataMerge
from ..conv.exceptions import (
    DataFilenameError,
    DataFormatNotSupported,
    DataFormatUnrecognised,
)
from ..conv.output_csv import OutputBase, OutputCsv
from ..conv.transfer_match import reconcile_transfers
from ..conv.wallets import WalletRegistry, WalletRegistryError, load_wallet_registry
from .errors import DeskError

_CONVERT_LOCK = threading.Lock()

NEEDS_REVIEW = "_Needs-Review"
POSSIBLE_SPAM = "_Possible-Spam"
IGNORED = "_Ignored"


@dataclass
class ReviewItem:
    timestamp: str
    asset: str
    quantity: str
    wallet: str
    destination: str
    tx_hash: str
    description: str
    disposition: str


@dataclass
class Conversion:
    format_name: str
    source_rows: int
    header: List[str]
    rows: List[List[str]] = field(default_factory=list)
    needs_review: List[ReviewItem] = field(default_factory=list)
    queue: List[ReviewItem] = field(default_factory=list)
    spam: List[ReviewItem] = field(default_factory=list)


def bittytax_header() -> List[str]:
    return [name.replace("{{currency}}", config.ccy) for name in OutputBase.BITTYTAX_OUT_HEADER]


def allowed_types() -> List[str]:
    return sorted(item.value for item in TrType)


def convert_file(path: str, wallets: Optional[str]) -> Conversion:
    """Parse ``path`` the way ``bittytax_conv`` does, then reconcile transfers."""
    with _CONVERT_LOCK:
        with _isolated_datafiles():
            return _convert_file(path, wallets)


def _convert_file(path: str, wallets: Optional[str]) -> Conversion:
    args = argparse.Namespace(unconfirmed=False, cryptoasset=None)
    try:
        file_type, _digest = _get_file_info(path)
        _do_read_file(file_type, path, args)
    except DataFormatUnrecognised as exc:
        raise DeskError(f"Unrecognised file: {exc}") from exc
    except DataFormatNotSupported as exc:
        raise DeskError(f"File format is not supported: {exc}") from exc
    except DataFilenameError as exc:
        raise DeskError(str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise DeskError("The file is not encoded as UTF-8 text or a spreadsheet.") from exc

    if not DataFile.data_files_ordered:
        raise DeskError(
            "No rows were recognised. The conversion tool supports this file type, "
            "but this file did not match a parser."
        )

    DataMerge.match_merge(DataFile.data_files)
    try:
        registry = load_wallet_registry(wallets) if wallets else WalletRegistry()
    except WalletRegistryError as exc:
        raise DeskError(str(exc)) from exc

    containers = [data_file.data_rows for data_file in DataFile.data_files_ordered]
    rows = [row for container in containers for row in container]
    result = reconcile_transfers(
        rows,
        registry,
        legacy_gifts=False,
        review_as="conservative",
        containers=containers,
    )

    names = []
    source_rows = 0
    converted: List[List[str]] = []
    for data_file in DataFile.data_files_ordered:
        names.append(data_file.parser.name)
        for data_row in data_file.data_rows:
            if any(cell.strip() for cell in data_row.row if cell):
                source_rows += 1
            if data_row.t_record is not None:
                converted.append(OutputCsv._to_bittytax_csv(data_row.t_record))

    return Conversion(
        format_name=", ".join(dict.fromkeys(names)),
        source_rows=source_rows,
        header=bittytax_header(),
        rows=converted,
        needs_review=[_review_item(item) for item in result.review],
        queue=[_review_item(item) for item in result.queue],
        spam=[_review_item(item) for item in result.spam],
    )


def _review_item(item: object) -> ReviewItem:
    timestamp = getattr(item, "timestamp")
    quantity = getattr(item, "quantity")
    return ReviewItem(
        timestamp=f"{timestamp:%Y-%m-%d %H:%M:%S}",
        asset=str(getattr(item, "asset")),
        quantity=f"{quantity:f}",
        wallet=str(getattr(item, "wallet")),
        destination=str(getattr(item, "destination")),
        tx_hash=str(getattr(item, "tx_hash")),
        description=str(getattr(item, "description")),
        disposition=str(getattr(item, "disposition")),
    )


@contextmanager
def _isolated_datafiles() -> Iterator[None]:
    saved_files = DataFile.data_files
    saved_order = DataFile.data_files_ordered
    saved_duplicates = DataFile.remove_duplicates
    DataFile.data_files = {}
    DataFile.data_files_ordered = []
    DataFile.remove_duplicates = False
    try:
        yield
    finally:
        DataFile.data_files = saved_files
        DataFile.data_files_ordered = saved_order
        DataFile.remove_duplicates = saved_duplicates


def unmapped_rows(rows: Sequence[Sequence[str]]) -> List[int]:
    """Indexes whose type is a Koinly tag the converter did not map."""
    special = {NEEDS_REVIEW, POSSIBLE_SPAM, IGNORED}
    found = []
    for index, cells in enumerate(rows):
        kind = cells[0] if cells else ""
        if kind.startswith("_") and kind not in special:
            found.append(index)
    return found
