# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Encrypted copies of the data directory. The key file is not inside the copy."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .errors import DeskError
from .events import append_event

_MAGIC = b"BTBK1"
_SKIP = {"backups", "backup.key"}
_BINARY = getattr(os, "O_BINARY", 0)


def backup_now(root: Path, reason: str, who: str = "local") -> Path:
    """Write one encrypted archive and drop older copies past the retention count."""
    root.mkdir(parents=True, exist_ok=True)
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    payload = _encrypt(_pack(root), _key(root))
    path = _create_archive(folder, stamp, payload)
    _prune(folder, _retention(root))
    append_event(root, "backup", who, "An encrypted backup was written.", {"reason": reason})
    return path


def ensure_daily_backup(root: Path) -> None:
    """One backup on the first start of a calendar day."""
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "last-day.txt"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == today:
        return
    backup_now(root, "daily")
    marker.write_text(today + "\n", encoding="utf-8")


def restore_backup(root: Path, name: str, who: str = "local") -> None:
    """Replace data files from one archive. The backup key is left in place."""
    path = _archive(root, name)
    payload = _decrypt(path.read_bytes(), _key(root))
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile() or _blocked(member.name):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
    append_event(root, "restore", who, "A backup was restored.", {"name": name})


def list_backups(root: Path) -> List[Dict[str, str]]:
    folder = root / "backups"
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.btbk")):
        rows.append({"name": path.name, "bytes": str(path.stat().st_size)})
    return rows


def save_retention(root: Path, count: str) -> None:
    try:
        number = int(count.strip())
    except ValueError as exc:
        raise DeskError("Enter a retention count.") from exc
    if number < 1 or number > 30:
        raise DeskError("Keep between 1 and 30 backups.")
    _write_json(root / "backup-settings.json", {"retention": number})


def retention_count(root: Path) -> int:
    return _retention(root)


def _pack(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if _blocked(relative):
                continue
            archive.add(path, arcname=relative)
    return buffer.getvalue()


def _blocked(name: str) -> bool:
    parts = Path(name).parts
    if name.startswith("/") or name.startswith("\\"):
        return True
    return not parts or parts[0] in _SKIP or ".." in parts


def _encrypt(payload: bytes, key: bytes) -> bytes:
    nonce = os.urandom(16)
    cipher = _xor(payload, key, nonce)
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return _MAGIC + nonce + cipher + mac


def _decrypt(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 16 + 32:
        raise DeskError("That backup could not be read.")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + 16]
    mac = blob[-32:]
    cipher = blob[len(_MAGIC) + 16 : -32]
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise DeskError("That backup could not be read.")
    return _xor(cipher, key, nonce)


def _xor(payload: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < len(payload):
        block = hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(left ^ right for left, right in zip(payload, out))


def _create_archive(folder: Path, stamp: str, payload: bytes) -> Path:
    """Pick a free name. Two backups in the same second must not overwrite."""
    for extra in range(100):
        name = f"{stamp}.btbk" if extra == 0 else f"{stamp}-{extra}.btbk"
        path = folder / name
        try:
            _write_private(path, payload)
        except FileExistsError:
            continue
        return path
    raise DeskError("The backup could not be written.")


def _write_private(path: Path, payload: bytes) -> None:
    """Create a new file at mode 0600. Binary mode keeps the ciphertext intact on Windows."""
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise DeskError("The backup could not be written.")
            view = view[written:]
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _key(root: Path) -> bytes:
    env = os.environ.get("BITTYTAX_DESK_PASSWORD", "").strip()
    if env:
        return hashlib.scrypt(
            env.encode("utf-8"), salt=b"uk-crypto-tax-desk-backup", n=2**14, r=8, p=1, dklen=32
        )
    path = root / "backup.key"
    if path.is_file():
        return path.read_bytes()
    secret = os.urandom(32)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
    try:
        os.write(descriptor, secret)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return secret


def _retention(root: Path) -> int:
    path = root / "backup-settings.json"
    if not path.is_file():
        return 7
    loaded = json.loads(path.read_text(encoding="utf-8"))
    try:
        return max(1, int(loaded.get("retention", 7)))
    except (TypeError, ValueError):
        return 7


def _prune(folder: Path, keep: int) -> None:
    files = sorted(folder.glob("*.btbk"))
    for path in files[:-keep]:
        path.unlink()


def _archive(root: Path, name: str) -> Path:
    if Path(name).name != name or not name.endswith(".btbk"):
        raise DeskError("Choose a backup from the list.")
    path = root / "backups" / name
    if not path.is_file():
        raise DeskError("Choose a backup from the list.")
    return path


def _write_json(path: Path, payload: Dict[str, int]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
