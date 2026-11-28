# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Backups round-trip, and a label change can be undone from the audit log."""

from __future__ import annotations

import os
from pathlib import Path

from bittytax.web.backup import backup_now, ensure_daily_backup, restore_backup, save_retention
from bittytax.web.errors import DeskError
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_a_backup_restores_and_keeps_the_key_out_of_the_archive(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("sample note\n", encoding="utf-8")
    path = backup_now(tmp_path, "test")
    _assert_owner_only(path)
    _assert_owner_only(tmp_path / "backup.key")
    blob = path.read_bytes()
    assert b"sample note" not in blob
    assert b"backup.key" not in blob
    (tmp_path / "note.txt").write_text("changed\n", encoding="utf-8")
    restore_backup(tmp_path, path.name)
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "sample note\n"
    broken = bytearray(blob)
    broken[-1] ^= 1
    path.write_bytes(bytes(broken))
    try:
        restore_backup(tmp_path, path.name)
    except DeskError as exc:
        assert "could not be read" in str(exc)
    else:
        raise AssertionError("a damaged backup should be refused")


def _assert_owner_only(path: Path) -> None:
    """Require mode 0600 where the system records a file mode."""
    if os.name != "posix":
        return
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_retention_and_a_second_daily_start_do_not_pile_up(tmp_path: Path) -> None:
    save_retention(tmp_path, "1")
    backup_now(tmp_path, "one")
    backup_now(tmp_path, "two")
    names = list((tmp_path / "backups").glob("*.btbk"))
    assert len(names) == 1
    ensure_daily_backup(tmp_path)
    ensure_daily_backup(tmp_path)
    assert len(list((tmp_path / "backups").glob("*.btbk"))) == 1


def test_undo_restores_the_previous_type(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    row = desk.all_rows()[0]
    before = row["type"]
    desk.reclassify(row["id"], "Income", "sample change", "sam")
    assert desk.transaction(row["id"])["type"] == "Income"
    latest = desk.change_log()[-1]
    desk.reclassify(
        row["id"], str(latest["before_type"]), "Undo the latest change.", "sam", "", True
    )
    assert desk.transaction(row["id"])["type"] == before
