# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

import re
import unicodedata
from typing import Optional, Sequence

_HEX = frozenset("0123456789abcdef")
_CHAIN_HASH_MIN = 16

# Cyrillic and Greek letters that render like Latin letters in tickers.
_CONFUSABLES = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
        "І": "I",
        "Ѕ": "S",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "т": "t",
        "к": "k",
        "м": "m",
        "і": "i",
        "ѕ": "s",
    }
)

_INVISIBLE = dict.fromkeys(
    map(ord, " \t\r\n\u00a0\u200a\u200b\u200c\u200d\ufeff"),
    None,
)

# Account numbers and IBANs. A 0x address is left alone: its digits follow "x".
_ACCOUNT = re.compile(
    r"(?<![0-9A-Za-z])(?:[A-Z]{2}\d{2}[A-Z0-9]{10,30}|\d{2}-\d{2}-\d{2}|\d{6,})(?![0-9A-Za-z])"
)


def normalise_tx_hash(value: str) -> str:
    """Drop white space, a single leading 0x, and letter case."""
    text = "".join(value.split()).casefold()
    if text.startswith("0x"):
        text = text[2:]
    return text


def hashes_equal(left: str, right: str) -> bool:
    """True when two hashes are the same chain id after 0x and case normalisation.

    A short exchange id is not treated as the same hash as a 0x-prefixed value.
    That pair is matched separately, by destination, amount and time.
    """
    if not left.strip() or not right.strip():
        return False
    norm_left = normalise_tx_hash(left)
    norm_right = normalise_tx_hash(right)
    if not norm_left or norm_left != norm_right:
        return False
    left_prefixed = left.strip().casefold().startswith("0x")
    right_prefixed = right.strip().casefold().startswith("0x")
    if left_prefixed == right_prefixed:
        return True
    return len(norm_left) >= _CHAIN_HASH_MIN and all(char in _HEX for char in norm_left)


def hash_kind(value: str) -> str:
    """Return 'empty', 'chain', or 'exchange'."""
    if not value or not value.strip():
        return "empty"
    norm = normalise_tx_hash(value)
    if len(norm) >= _CHAIN_HASH_MIN and all(char in _HEX for char in norm):
        return "chain"
    return "exchange"


def confusable_key(symbol: str) -> str:
    text = unicodedata.normalize("NFKD", symbol)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(_CONFUSABLES)
    text = text.translate(_INVISIBLE)
    return text.upper()


def is_confusable_official(symbol: str, official: Sequence[str]) -> bool:
    """True when a ticker is not the official symbol but folds to one."""
    raw = symbol.strip().upper()
    names = {item.strip().upper() for item in official if item.strip()}
    if not raw or raw in names:
        return False
    return confusable_key(symbol) in names


def related_official(symbol: str, official: Sequence[str]) -> Optional[str]:
    """Official asset a lookalike points at, including a short suffix such as USDT.e."""
    names = [item.strip().upper() for item in official if item.strip()]
    raw = symbol.strip().upper()
    if not raw or raw in names:
        return None
    key = confusable_key(symbol)
    if key in names:
        return key
    for name in names:
        if key.startswith(name) and 0 < len(key) - len(name) <= 4:
            return name
    return None


def redact_account_numbers(text: str) -> str:
    """Replace IBANs, sort codes and long digit runs. Chain addresses are kept."""
    return _ACCOUNT.sub("[redacted]", text)
