# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Bulk edits and labelling rules are logged and can be previewed."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from bittytax.web.server import bind_server
from bittytax.web.store import Desk
from bittytax.web.user_rules import apply_bulk, apply_enabled_rules, preview_rule, save_rule

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"


def test_a_rule_previews_then_relabels_and_import_applies_it(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    rule = save_rule(
        desk.root,
        {"field": "description", "match": "sample gift", "set_type": "Income", "set_tag": ""},
    )
    gift = next(row for row in desk.all_rows() if "sample gift" in row["note"])
    assert gift["type"] != "Income"
    preview = preview_rule(desk.all_rows(), rule)
    assert [row["id"] for row in preview] == [gift["id"]]
    assert desk.transaction(gift["id"])["type"] != "Income"
    apply_enabled_rules(desk, "sam")
    assert desk.transaction(gift["id"])["type"] == "Income"
    extra = SAMPLE.read_bytes().replace(b"sample gift to reclassify", b"sample gift again")
    desk.import_bytes("again.csv", extra)
    again = next(row for row in desk.all_rows() if "sample gift again" in row["note"])
    assert again["type"] == "Income"
    log = (desk.root / "events.jsonl").read_text(encoding="utf-8")
    assert "rules" in log


def test_bulk_spam_and_undo(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    rows = desk.all_rows()[:2]
    before = [row["type"] for row in rows]
    ids = [row["id"] for row in rows]
    apply_bulk(desk, ids, "spam", "", "", "Sample spam mark.", "sam")
    for row_id in ids:
        assert desk.transaction(row_id)["type"] == "_Possible-Spam"
        assert desk.transaction(row_id)["status"] == "spam"
    apply_bulk(desk, ids, "undo", "", "", "", "sam")
    restored = [desk.transaction(row_id)["type"] for row_id in ids]
    assert restored == before
    log = (desk.root / "events.jsonl").read_text(encoding="utf-8")
    assert "bulk-spam" in log
    assert "bulk-undo" in log


def test_rules_page_offers_a_preview(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/rules", timeout=30) as response:
            rules = response.read().decode("utf-8")
        with urlopen(f"http://127.0.0.1:{port}/transactions", timeout=30) as response:
            listing = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "Save and preview" in rules
    assert "not a tax rule" in rules
    assert "Reclassify" in listing
    assert 'name="row_id"' in listing
