# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Desk settings, the settings audit, and confirmed-only tax choices.

Personal details stay in the data directory. An unconfirmed year file is not
applied. Treating other gains as capital gains is a comparison display only.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .backup import backup_now
from .errors import DeskError
from .events import append_event
from .rules import list_rule_years, load_rules
from .tx_store import load_dust, save_dust

SOURCES: Tuple[str, ...] = (
    "CoinGecko",
    "CoinPaprika",
    "CryptoCompare",
    "CoinStats",
    "BittyTaxAPI",
    "Frankfurter",
    "CoinDesk",
)
FIAT_SOURCES = {"BittyTaxAPI", "Frankfurter", "CoinDesk"}
TRUSTED_DEFAULT: Tuple[str, ...] = ("BTC", "ETH", "USDT", "USDC", "BNB", "SOL")
INCOME = ("airdrops", "forks", "mining", "rewards")
CURRENCIES = ("GBP", "USD", "EUR")
TIMEZONES = ("Europe/London", "UTC")
LANGUAGES = ("en-GB",)
COUNTRIES = ("UK",)
NUMBER_FORMATS = ("1,234.56", "1.234,56", "1 234.56")
TIME_FORMATS = ("24-hour", "12-hour")
CASHBACK = ("nil", "market")
COST_BASIS = "hmrc_share_pooling"
COST_BASIS_LABEL = "HMRC share pooling for individuals (same-day, 30-day, section 104)"
TAX_YEAR_START = "6 April"
RESET_WORD = "RESET"
OTHER_NOTE = (
    "This is a Koinly comparison display option. "
    "It is never included in the SA108 capital gains."
)
_TAX_KEYS = {
    "income_airdrops",
    "income_forks",
    "income_mining",
    "income_rewards",
    "cashback_cost",
    "cost_basis_method",
    "dust",
}
_INCOME_OFF = {
    "airdrops": {"Airdrop"},
    "forks": {"Fork"},
    "mining": {"Mining"},
    "rewards": {"Staking-Reward", "Referral"},
}
_OTHER_TYPES = {"Margin-Gain", "Margin-Loss", "Margin-Fee", "Margin-Fee-Rebate"}
_MONEY_HEADERS = {
    "gain",
    "net value",
    "proceeds",
    "sent cost basis",
    "received cost basis",
    "cost basis",
    "cost",
    "fee value",
}
_PENCE = Decimal("0.01")
_KEEP = {"backups", "backup.key"}


def load_settings(root: Path) -> Dict[str, Any]:
    """Stored settings, filled with the defaults. The file is local only."""
    merged = _defaults()
    loaded = _read_json(root / "desk-settings.json")
    for key in merged:
        if key in loaded:
            merged[key] = loaded[key]
    merged["source_order"] = _stored_order(merged.get("source_order"))
    merged["trusted_coins"] = _stored_coins(merged.get("trusted_coins"))
    merged["reports_stale"] = bool(merged.get("reports_stale"))
    merged["other_gains_as_capital"] = bool(merged.get("other_gains_as_capital"))
    merged["compact_small"] = bool(merged.get("compact_small"))
    return merged


def save_general(root: Path, form: Mapping[str, str], who: str) -> None:
    """Currency, country, clock, lock date, dust, and the report cover."""
    if form.get("tax_year_start", TAX_YEAR_START) != TAX_YEAR_START:
        raise DeskError("The UK tax year starts on 6 April. That date is fixed.")
    updates: Dict[str, Any] = {
        "base_currency": _choice(form.get("base_currency", ""), CURRENCIES, "Choose a currency."),
        "koinly_currency": _choice(
            form.get("koinly_currency", ""), CURRENCIES, "Choose the Koinly file currency."
        ),
        "home_country": _choice(
            form.get("home_country", ""), COUNTRIES, "The home country is the UK."
        ),
        "timezone": _choice(form.get("timezone", ""), TIMEZONES, "Choose a time zone."),
        "report_language": _choice(
            form.get("report_language", ""), LANGUAGES, "The report language is en-GB."
        ),
        "lock_before": _day(form.get("lock_before", "")),
        "name": _name(form.get("name", "")),
        "utr": _utr(form.get("utr", "")),
    }
    _save_updates(root, who, updates)
    _save_dust(root, who, form.get("dust", "0"))


def save_tax(root: Path, form: Mapping[str, str], who: str) -> None:
    """Income switches, cashback, and the one cost-basis method."""
    method = form.get("cost_basis_method", COST_BASIS)
    if method != COST_BASIS:
        raise DeskError("Only HMRC share pooling for individuals is offered.")
    cashback = form.get("cashback_cost", "nil")
    if cashback not in CASHBACK:
        raise DeskError("Cashback cost is nil or market value.")
    updates: Dict[str, Any] = {
        "cost_basis_method": method,
        "cashback_cost": cashback,
        "other_gains_as_capital": form.get("other_gains_as_capital") == "yes",
    }
    for name in INCOME:
        updates[f"income_{name}"] = "on" if form.get(f"income_{name}") == "on" else "off"
    _save_updates(root, who, updates)


def save_display(root: Path, form: Mapping[str, str], who: str) -> None:
    """Number format, time format, and compact small amounts."""
    updates = {
        "number_format": _choice(
            form.get("number_format", ""), NUMBER_FORMATS, "Choose a number format."
        ),
        "time_format": _choice(form.get("time_format", ""), TIME_FORMATS, "Choose a time format."),
        "compact_small": form.get("compact_small") == "yes",
    }
    _save_updates(root, who, updates)


def save_pricing(root: Path, form: Mapping[str, str], who: str) -> None:
    """Source order and the trusted-coin list."""
    updates = {
        "source_order": parse_order(form.get("source_order", "")),
        "trusted_coins": parse_coins(form.get("trusted_coins", "")),
    }
    _save_updates(root, who, updates)


def save_dust_setting(root: Path, who: str, raw: str) -> None:
    """The dust box on the transaction lists uses the same audit."""
    _save_dust(root, who, raw)


def recalculate(desk: Any, who: str) -> Dict[str, Any]:
    """Run the calculator for each year in the ledger, then clear the stale mark."""
    root = desk.root
    errors: List[str] = []
    ran: List[int] = []
    for year in _years(desk):
        try:
            desk.report(year)
        except DeskError as exc:
            errors.append(str(exc))
        else:
            ran.append(year)
    fresh = not errors
    if fresh:
        _set_flag(root, "reports_stale", False)
    record_change(root, who, "recalculate", "stale", "fresh" if fresh else "still stale")
    return {"ran": ran, "errors": errors, "stale": not fresh}


def reset_data(root: Path, typed: str, who: str) -> None:
    """Back up first, then remove the data files. The backup key stays."""
    if typed.strip() != RESET_WORD:
        raise DeskError("Type RESET to confirm. Nothing was deleted.")
    backup_now(root, "before a reset", who)
    for child in list(root.iterdir()):
        if child.name in _KEEP:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    record_change(root, who, "reset", "data directory", "reset after a backup")


def record_change(root: Path, who: str, key: str, old: object, new: object) -> None:
    """One audit line with the old value, the new value, and the time."""
    entry = {
        "at": _stamp(),
        "who": (who or "local")[:80],
        "key": key,
        "old": _show(old),
        "new": _show(new),
    }
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n"
    _append(root / "settings-audit.jsonl", line.encode("utf-8"))
    append_event(
        root, "settings", who, f"Changed {key}.", {"old": entry["old"], "new": entry["new"]}
    )


def load_audit(root: Path) -> List[Dict[str, str]]:
    path = root / "settings-audit.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_bytes().splitlines():
        if line.strip():
            rows.append(json.loads(line.decode("utf-8")))
    return rows


def reports_are_stale(root: Path) -> bool:
    return bool(load_settings(root)["reports_stale"])


def refuse_if_locked(root: Path, timestamp: str) -> None:
    """Rows before the lock date are read-only."""
    lock = str(load_settings(root).get("lock_before") or "")
    day = str(timestamp or "")[:10]
    if lock and day and day < lock:
        raise DeskError(f"That transaction is locked. Rows before {lock} are read-only.")


def feeds_sa108(name: str) -> bool:
    """Other gains never feed the SA108 capital gains."""
    if name == "other_gains_as_capital":
        return False
    return True


def other_gains_display(rows: Sequence[Mapping[str, Any]], enabled: bool) -> Dict[str, str]:
    """A comparison line. The amount is not an SA108 figure."""
    if not enabled:
        return {"shown": "no", "amount": "", "in_sa108": "no", "note": OTHER_NOTE}
    total = Decimal(0)
    for row in rows:
        if str(row.get("type") or "") in _OTHER_TYPES:
            total += _decimal(row.get("gain") or row.get("sell_value"))
    return {
        "shown": "yes",
        "amount": f"{total.quantize(_PENCE)}",
        "in_sa108": "no",
        "note": OTHER_NOTE,
    }


def year_switches(root: Path) -> List[Dict[str, str]]:
    """Each income switch, once per tax year, with that file's confirmed status."""
    settings = load_settings(root)
    rows = []
    for year in list_rule_years():
        rules = year.get("rules") or {}
        status = "confirmed" if rules.get("confirmed") else "unconfirmed"
        for name in INCOME:
            rows.append(
                {
                    "year": str(year.get("slug") or ""),
                    "name": name,
                    "value": str(settings.get(f"income_{name}") or "off"),
                    "status": status,
                    "applied": "yes" if status == "confirmed" else "no",
                }
            )
        rows.append(
            {
                "year": str(year.get("slug") or ""),
                "name": "cashback",
                "value": str(settings.get("cashback_cost") or "nil"),
                "status": status,
                "applied": "yes" if status == "confirmed" else "no",
            }
        )
    return rows


def apply_confirmed_rules(root: Path, source: Path, year: int) -> Optional[Path]:
    """Rewrite the tax file only when that year's rule file is confirmed."""
    rules = load_rules(year) or {}
    if not rules.get("confirmed"):
        return None
    text = source.read_text(encoding="utf-8")
    rewritten = rewrite_tax_csv(text, load_settings(root))
    if rewritten == text:
        return None
    path = root / f"tax-rules-{year}.csv"
    _write_bytes(path, rewritten.encode("utf-8"))
    return path


def rewrite_tax_csv(text: str, settings: Mapping[str, Any]) -> str:
    """Apply confirmed income switches and the cashback cost. Other gains are ignored."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return text
    off = {
        kind
        for name, kinds in _INCOME_OFF.items()
        if str(settings.get(f"income_{name}") or "off") != "on"
        for kind in kinds
    }
    nil = str(settings.get("cashback_cost") or "nil") == "nil"
    for row in rows[1:]:
        if len(row) < 4:
            continue
        if row[0] in off:
            row[0] = "Deposit"
        if nil and row[0] == "Cashback":
            row[3] = "0"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue()


def convert_dated_amounts(text: str, rates: Mapping[str, Decimal]) -> Tuple[str, int]:
    """Convert dated money columns. A row with no rate for its date is left as it is."""
    if not text.strip() or not rates:
        return text, 0
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return text, 0
    count = 0
    rows = []
    for raw in reader:
        day = _row_day(raw)
        rate = rates.get(day)
        if rate is not None and _scale_money(raw, rate):
            count += 1
        rows.append(raw)
    if count == 0:
        return text, 0
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(reader.fieldnames), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue(), count


def usd_gbp_rates(root: Path) -> Dict[str, Decimal]:
    """GBP for one USD on a date, taken from the local price cache."""
    loaded = _read_json(root / "price-cache.json")
    found: Dict[str, Decimal] = {}
    for key, entry in loaded.items():
        if not str(key).startswith("USD|") or not isinstance(entry, dict):
            continue
        day = str(key).split("|", 1)[1]
        try:
            found[day] = Decimal(str(entry.get("price") or ""))
        except InvalidOperation:
            continue
    return found


def parse_order(text: str) -> List[str]:
    parts = [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]
    if not parts:
        raise DeskError("Choose at least one price source.")
    if any(part not in SOURCES for part in parts):
        raise DeskError("Use the calculator price sources only.")
    if len(parts) != len(set(parts)):
        raise DeskError("Each price source can appear once.")
    return parts


def parse_coins(text: str) -> List[str]:
    found: List[str] = []
    for part in text.replace("\n", ",").split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        if len(symbol) > 40 or any(char.isspace() for char in symbol):
            raise DeskError("Enter an asset symbol, up to 40 characters.")
        if symbol not in found:
            found.append(symbol)
    return found


def format_number(raw: str, style: str, compact: bool = False) -> str:
    """A display amount. The default groups thousands with a comma."""
    try:
        amount = Decimal(str(raw).replace(",", ""))
    except InvalidOperation:
        return str(raw)
    if compact and amount != 0 and abs(amount) < _PENCE:
        return "<0.01" if amount > 0 else ">-0.01"
    negative = amount < 0
    whole, frac = f"{abs(amount):.2f}".split(".")
    if style == "1.234,56":
        body = f"{_groups(whole, '.')},{frac}"
    elif style == "1 234.56":
        body = f"{_groups(whole, ' ')}.{frac}"
    else:
        body = f"{_groups(whole, ',')}.{frac}"
    return f"-{body}" if negative else body


def format_clock(stamp: str, style: str) -> str:
    """24-hour or 12-hour. The timestamp begins with the date and the clock time."""
    text = str(stamp or "").strip()
    try:
        moment = datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return text
    if style == "12-hour":
        suffix = "am" if moment.hour < 12 else "pm"
        hour = moment.hour % 12 or 12
        return f"{moment:%Y-%m-%d} {hour:02d}:{moment:%M} {suffix}"
    return moment.strftime("%Y-%m-%d %H:%M")


def page_model(root: Path) -> Dict[str, Any]:
    """Everything the settings page renders besides the read-only rule files."""
    settings = load_settings(root)
    return {
        "values": settings,
        "audit": load_audit(root),
        "stale": bool(settings["reports_stale"]),
        "switches": year_switches(root),
        "sources": list(SOURCES),
        "currencies": list(CURRENCIES),
        "timezones": list(TIMEZONES),
        "tax_year_start": TAX_YEAR_START,
        "cost_basis_label": COST_BASIS_LABEL,
        "other_note": OTHER_NOTE,
        "number_preview": format_number(
            "1234.5", str(settings["number_format"]), bool(settings["compact_small"])
        ),
        "time_preview": format_clock("2025-06-01 15:04:00", str(settings["time_format"])),
        "source_text": ", ".join(settings["source_order"]),
        "trusted_text": ", ".join(settings["trusted_coins"]),
    }


def _save_updates(root: Path, who: str, updates: Mapping[str, Any]) -> None:
    current = load_settings(root)
    changed = False
    tax = False
    for key, new in updates.items():
        old = current.get(key)
        if _same(old, new):
            continue
        record_change(root, who, key, old, new)
        current[key] = new
        changed = True
        tax = tax or key in _TAX_KEYS
    if not changed:
        return
    if tax:
        current["reports_stale"] = True
    _write_settings(root, current)


def _save_dust(root: Path, who: str, raw: str) -> None:
    old = format(load_dust(root), "f")
    amount = save_dust(root, raw)
    new = format(amount, "f")
    if old == new:
        return
    record_change(root, who, "dust", old, new)
    _set_flag(root, "reports_stale", True)


def _set_flag(root: Path, key: str, value: bool) -> None:
    current = load_settings(root)
    current[key] = value
    _write_settings(root, current)


def _write_settings(root: Path, settings: Mapping[str, Any]) -> None:
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    _write_bytes(root / "desk-settings.json", text.encode("utf-8"))


def _defaults() -> Dict[str, Any]:
    values: Dict[str, Any] = {
        "base_currency": "GBP",
        "koinly_currency": "USD",
        "home_country": "UK",
        "timezone": "Europe/London",
        "report_language": "en-GB",
        "lock_before": "",
        "name": "",
        "utr": "",
        "number_format": "1,234.56",
        "time_format": "24-hour",
        "compact_small": False,
        "source_order": list(SOURCES),
        "trusted_coins": list(TRUSTED_DEFAULT),
        "cashback_cost": "nil",
        "cost_basis_method": COST_BASIS,
        "other_gains_as_capital": False,
        "reports_stale": False,
    }
    for name in INCOME:
        values[f"income_{name}"] = "on" if name in {"mining", "rewards"} else "off"
    return values


def _stored_order(value: object) -> List[str]:
    if not isinstance(value, list):
        return list(SOURCES)
    chosen = [str(item) for item in value if str(item) in SOURCES]
    return chosen or list(SOURCES)


def _stored_coins(value: object) -> List[str]:
    if not isinstance(value, list):
        return list(TRUSTED_DEFAULT)
    return [str(item).upper() for item in value if str(item).strip()]


def _choice(value: str, allowed: Sequence[str], message: str) -> str:
    if value not in allowed:
        raise DeskError(message)
    return value


def _day(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise DeskError("Enter the lock date as YYYY-MM-DD.") from exc
    return text


def _name(value: str) -> str:
    text = " ".join(value.split())
    if len(text) > 80:
        raise DeskError("The name is too long.")
    return text


def _utr(value: str) -> str:
    text = value.replace(" ", "").strip()
    if not text:
        return ""
    if len(text) != 10 or not text.isdigit():
        raise DeskError("Enter a 10-digit UTR, or leave it blank.")
    return text


def _years(desk: Any) -> List[int]:
    from datetime import date  # pylint: disable=import-outside-toplevel

    from .taxyear import tax_year_containing  # pylint: disable=import-outside-toplevel

    found: List[int] = []
    seen = set()
    for row in desk.all_rows():
        text = str(row.get("date") or "")[:10]
        if not text:
            continue
        try:
            year = tax_year_containing(date.fromisoformat(text))
        except ValueError:
            continue
        if year not in seen:
            seen.add(year)
            found.append(year)
    return found


def _row_day(raw: Mapping[str, Any]) -> str:
    for key, value in raw.items():
        if key and str(key).strip().casefold() == "date":
            return str(value or "")[:10]
    return ""


def _scale_money(raw: Dict[str, Any], rate: Decimal) -> bool:
    changed = False
    for key in list(raw):
        if not key or _header(str(key)) not in _MONEY_HEADERS:
            continue
        text = str(raw.get(key) or "").strip()
        if not text:
            continue
        raw[key] = f"{(_decimal(text) * rate).quantize(_PENCE)}"
        changed = True
    return changed


def _header(name: str) -> str:
    text = name.strip().casefold()
    if "(" in text:
        text = text.split("(", 1)[0]
    return " ".join(text.split())


def _groups(whole: str, sep: str) -> str:
    parts: List[str] = []
    while whole:
        parts.append(whole[-3:])
        whole = whole[:-3]
    return sep.join(reversed(parts))


def _same(old: object, new: object) -> bool:
    return _show(old) == _show(new)


def _show(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _append(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
