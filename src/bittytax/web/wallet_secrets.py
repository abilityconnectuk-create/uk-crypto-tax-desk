# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""API keys stay in the data directory, or in the operating-system key store.

The file mode is 0600. A key is never returned to a page and never written to a log.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping

_SERVICE = "uk-crypto-tax-desk"
_ID = re.compile(r"^[0-9A-Za-z-]{8,80}$")
_FIELDS = ("api_key", "api_secret", "passphrase")


def write_text(path: Path, payload: str, mode: int = 0o600) -> None:
    """Write text with an explicit newline style. Binary mode keeps Windows honest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(temporary), flags, mode)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    except OSError:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    os.chmod(path, mode)


def append_text(path: Path, payload: str, mode: int = 0o600) -> None:
    """Append text in binary mode so a log line is the same on every system."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, mode)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def write_json(path: Path, payload: Mapping[str, Any], mode: int = 0o600) -> None:
    """JSON with a single trailing newline and no carriage returns."""
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    write_text(path, text, mode)


def read_json(path: Path) -> Dict[str, Any]:
    """A JSON object, or an empty mapping when the file is missing or unreadable."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def pack(api_key: str = "", api_secret: str = "", passphrase: str = "") -> Dict[str, str]:
    """The only fields a wallet secret may hold."""
    return {
        "api_key": api_key.strip(),
        "api_secret": api_secret.strip(),
        "passphrase": passphrase.strip(),
    }


def useful(payload: Mapping[str, str]) -> bool:
    """True when at least one secret field was supplied."""
    return any(str(payload.get(field) or "").strip() for field in _FIELDS)


def save_secret(
    root: Path,
    wallet_id: str,
    payload: Mapping[str, str],
    keychain: Any = None,
) -> str:
    """Store a secret. Returns ``keychain`` or ``file``. An empty payload is removed."""
    ident = _ident(wallet_id)
    body = {field: str(payload.get(field) or "") for field in _FIELDS}
    path = _path(root, ident)
    if not useful(body):
        forget_secret(root, ident, keychain)
        return ""
    encoded = json.dumps(body, sort_keys=True)
    if keychain is not None:
        keychain.set(ident, encoded)
        path.unlink(missing_ok=True)
        return "keychain"
    if _os_set(ident, encoded):
        path.unlink(missing_ok=True)
        return "keychain"
    write_text(path, encoded + "\n")
    return "file"


def load_secret(root: Path, wallet_id: str, keychain: Any = None) -> Dict[str, str]:
    """The stored secret, or blanks. Callers must not put this on a page or in a log."""
    ident = _ident(wallet_id)
    if keychain is not None:
        return _decode(str(keychain.get(ident) or ""))
    stored = _os_get(ident)
    if stored:
        return _decode(stored)
    path = _path(root, ident)
    if not path.is_file():
        return pack()
    return _decode(path.read_bytes().decode("utf-8"))


def forget_secret(root: Path, wallet_id: str, keychain: Any = None) -> None:
    """Remove a secret from the key store and from the data directory."""
    try:
        ident = _ident(wallet_id)
    except ValueError:
        return
    if keychain is not None:
        keychain.delete(ident)
    _os_delete(ident)
    _path(root, ident).unlink(missing_ok=True)


def scrub(text: str, secret: Mapping[str, str]) -> str:
    """Replace every saved key so an error can be shown."""
    cleaned = text
    for field in _FIELDS:
        part = str(secret.get(field) or "")
        if len(part) >= 4:
            cleaned = cleaned.replace(part, "redacted")
    return cleaned


def _ident(wallet_id: str) -> str:
    if not _ID.match(wallet_id or ""):
        raise ValueError("wallet id")
    return wallet_id


def _path(root: Path, wallet_id: str) -> Path:
    return root / "secrets" / "wallets" / f"{wallet_id}.json"


def _decode(raw: str) -> Dict[str, str]:
    if not raw.strip():
        return pack()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return pack()
    if not isinstance(loaded, dict):
        return pack()
    return pack(
        str(loaded.get("api_key") or ""),
        str(loaded.get("api_secret") or ""),
        str(loaded.get("passphrase") or ""),
    )


def _os_set(wallet_id: str, body: str) -> bool:
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(_SERVICE, wallet_id, body)
    except Exception:  # pylint: disable=broad-exception-caught
        return False
    return True


def _os_get(wallet_id: str) -> str:
    keyring = _keyring()
    if keyring is None:
        return ""
    try:
        found = keyring.get_password(_SERVICE, wallet_id)
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    return str(found or "")


def _os_delete(wallet_id: str) -> None:
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(_SERVICE, wallet_id)
    except Exception:  # pylint: disable=broad-exception-caught
        return


def _keyring() -> Any:
    try:
        import keyring  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    return keyring
