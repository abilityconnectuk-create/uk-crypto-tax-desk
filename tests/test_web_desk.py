# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""The local desk: import, reclassify, review queues, and report parity with the CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pytest

from bittytax.bt_types import TaxRules, Year
from bittytax.constants import CACHE_DIR
from bittytax.tax import CalculateCapitalGains
from bittytax.web.__main__ import main
from bittytax.web.config_view import config_snapshot
from bittytax.web.errors import DeskError
from bittytax.web.identity import actor_for
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
REVIEW_SAMPLE = ROOT / "examples" / "web-sample-review-koinly.csv"
WALLETS = ROOT / "examples" / "web-sample-wallets.yaml"
GIFT_NOTE = "sample gift to reclassify"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MONEY = r"[£\u00a3](-?[\d,]+\.\d{2})"


def test_import_records_the_manifest_and_keeps_the_raw_file(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    original = SAMPLE.read_bytes()
    entry = desk.import_bytes(SAMPLE.name, original)

    assert entry["sha256"] == hashlib.sha256(original).hexdigest()
    assert entry["row_count"] == 6
    assert entry["date_min"] == "2024-05-01"
    assert entry["date_max"] == "2025-09-15"
    assert entry["format"] == "Koinly"
    stored = (desk.root / entry["raw_path"]).read_bytes()
    assert stored == original
    assert len(desk.transactions()["rows"]) == 6

    with pytest.raises(DeskError, match="already imported"):
        desk.import_bytes(SAMPLE.name, original)
    assert len(list(desk.raw_dir.rglob("*.csv"))) == 1


def test_reclassify_appends_a_log_and_leaves_the_upload(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    original = SAMPLE.read_bytes()
    entry = desk.import_bytes(SAMPLE.name, original)
    gift = _gift(desk)
    assert gift["type"] != "Income"

    first = desk.reclassify(gift["id"], "Income", "Sample gift is income.", "sam")
    assert first["who"] == "sam"
    assert first["before_type"] == gift["type"]
    assert first["after_type"] == "Income"
    assert first["why"] == "Sample gift is income."
    updated = desk.transaction(gift["id"])
    assert updated["type"] == "Income"
    assert updated["status"] == "reclassified"
    assert (desk.root / entry["raw_path"]).read_bytes() == original
    ledger = json.loads((desk.root / "ledger.json").read_text(encoding="utf-8"))
    stored = next(row for row in ledger["rows"] if row["id"] == gift["id"])
    assert stored["cells"][0] == gift["type"]

    log_path = desk.root / "reclassifications.jsonl"
    first_line = log_path.read_text(encoding="utf-8").splitlines()[0]
    desk.reclassify(gift["id"], "Gift-Received", "Put the sample back.", "sam")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == first_line
    assert len(lines) == 2
    assert desk.transaction(gift["id"])["type"] == "Gift-Received"


def test_filters_narrow_the_ledger(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())

    btc = desk.transactions({"asset": "BTC"})
    assert btc["total"] == 4
    assert all("BTC" in row["assets"] for row in btc["rows"])

    window = desk.transactions({"date_from": "2025-04-06", "date_to": "2026-04-05"})
    assert window["total"] == 3
    assert {row["date"] for row in window["rows"]} == {"2025-08-01", "2025-09-01", "2025-09-15"}

    found = desk.transactions({"query": GIFT_NOTE})
    assert found["total"] == 1
    assert GIFT_NOTE in found["rows"][0]["note"]

    trades = desk.transactions({"kind": "Trade", "wallet": "Example Exchange"})
    assert trades["total"] >= 1
    assert all(row["type"] == "Trade" for row in trades["rows"])


def test_review_queues_list_unmatched_spam_unmapped_and_missing_cost(tmp_path: Path) -> None:
    shutil.copy(WALLETS, tmp_path / "wallets.yaml")
    desk = _desk(tmp_path)
    desk.import_bytes(REVIEW_SAMPLE.name, REVIEW_SAMPLE.read_bytes())
    listed = desk.review_lists()
    review = desk.review(2026)

    assert listed["queue"], "unmatched withdrawal should be queued"
    assert any(item["asset"] == "ETH" for item in listed["queue"])
    assert any(item["asset"] == "SCAM" for item in listed["spam"])
    assert any(row["type"].startswith("_") for row in review["unmapped"])
    assert any("mysterylabel" in row["type"].casefold() for row in review["unmapped"])
    assert review["missing_cost"], "a sale with no pool should record a missing cost"
    assert review["integrity_ok"] is False
    assert any(row["asset"] == "ETH" for row in review["missing_cost"])


def test_report_matches_the_bittytax_cli_and_reruns(tmp_path: Path) -> None:
    _seed_price_cache()
    desk = _desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    gift = _gift(desk)
    desk.reclassify(gift["id"], "Income", "Sample gift is income.", "local")

    report_2026 = desk.report(2026)
    report_2025 = desk.report(2025)
    stdout, snapshot = _cli(desk.records_path, tmp_path / "snapshot.json")
    sections = _year_sections(stdout)

    _assert_section(sections[report_2026["label"]], report_2026)
    _assert_section(sections[report_2025["label"]], report_2025)
    _assert_snapshot(snapshot, report_2026, "2026")
    _assert_snapshot(snapshot, report_2025, "2025")

    engine_split = CalculateCapitalGains(Year(2025), TaxRules.UK_INDIVIDUAL).split_date
    assert engine_split is not None
    assert report_2025["summary"]["split_date"] == engine_split.isoformat() == "2024-10-30"
    assert report_2026["summary"]["split_date"] == ""
    before = Decimal(report_2025["summary"]["gain_before"])
    after = Decimal(report_2025["summary"]["gain_after"])
    assert before > 0 and after > 0
    assert before + after == Decimal(report_2025["summary"]["gain"])
    assert Decimal(report_2026["summary"]["gain"]) > 0
    assert Decimal(report_2026["income"]["amount"]) > Decimal(report_2025["income"]["amount"])

    again = desk.report(2026)
    assert again == report_2026
    first_csv = desk.report_csv(2026)
    cache = desk.root / "report-cache"
    for path in cache.glob("2026-*.json"):
        path.unlink()
    assert desk.report(2026) == report_2026
    assert desk.report_csv(2026) == first_csv


def test_public_host_is_refused() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--host", "0.0.0.0", "--no-browser", "--data-dir", "/tmp/unused-desk"])
    assert caught.value.code == 2


def test_actor_refuses_a_remote_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UK_CRYPTO_TAX_DESK_ACTOR", "sam")
    assert actor_for({}, "127.0.0.1") == "sam"
    assert actor_for({}, "::1") == "sam"
    with pytest.raises(DeskError):
        actor_for({}, "10.1.1.1")


def test_config_view_shows_the_wallet_register(tmp_path: Path) -> None:
    shutil.copy(WALLETS, tmp_path / "wallets.yaml")
    shown = config_snapshot(tmp_path / "wallets.yaml")
    assert shown["wallets_present"] is True
    assert "window_hours" in shown["match"]
    assert "sample-leave-unmatched" in shown["match_overrides"]
    assert config_snapshot(None)["wallets_present"] is False


def test_server_imports_reclassifies_and_reports(tmp_path: Path) -> None:
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    address = httpd.server_address
    host, port = str(address[0]), int(address[1])
    assert host == "127.0.0.1"
    base = f"http://127.0.0.1:{port}"
    try:
        about = _get(base + "/about")
        assert "AGPL-3.0" in about
        assert "not tax advice" in about.casefold()
        imported = _post_file(base + "/import", SAMPLE.name, SAMPLE.read_bytes())
        assert "Imported" in imported
        listing = _get(base + "/transactions?query=" + quote(GIFT_NOTE))
        assert GIFT_NOTE in listing
        match = re.search(r'href="(/transactions/[^"]+)"', listing)
        assert match is not None
        page = _post(
            base + match.group(1),
            urlencode({"type": "Income", "why": "Sample gift is income."}).encode("utf-8"),
            "application/x-www-form-urlencoded",
        )
        assert "Income" in page
        assert "Sample gift is income." in page
        report = _get(base + "/report?tax_year=2026&run=1")
        assert "Disposal proceeds" in report
        downloaded = _get(base + "/report.csv?tax_year=2026")
        assert "disposal_proceeds" in downloaded
        style = _get(base + "/static/style.css")
        assert "--ink" in style
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_module_help_starts() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "bittytax.web", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "127.0.0.1" in completed.stdout


def _desk(root: Path) -> Desk:
    os.environ.pop("UK_CRYPTO_TAX_DESK_WALLETS", None)
    return Desk(root)


def _gift(desk: Desk) -> Dict[str, Any]:
    rows = desk.transactions({"query": GIFT_NOTE})["rows"]
    assert len(rows) == 1
    return desk.transaction(rows[0]["id"])


def _seed_price_cache() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    _touch_cache(
        os.path.join(CACHE_DIR, "BittyTaxAPI_assets.json"), {"timestamp": stamp, "assets": {}}
    )
    empty_ids = {"timestamp": stamp, "ids": {}}
    _touch_cache(os.path.join(CACHE_DIR, "CoinGecko_ids.json"), empty_ids)
    _touch_cache(os.path.join(CACHE_DIR, "CoinPaprika_ids.json"), dict(empty_ids))


def _touch_cache(path: str, empty: Mapping[str, Any]) -> None:
    payload: Dict[str, Any]
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and "timestamp" in loaded:
            loaded["timestamp"] = datetime.now().isoformat(timespec="seconds")
            payload = loaded
        else:
            payload = dict(empty)
    else:
        payload = dict(empty)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _cli(csv_path: Path, snapshot_path: Path) -> Tuple[str, Dict[str, Any]]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bittytax.bittytax",
            "--nopdf",
            "--skip-holdings",
            "--year-end-snapshot",
            str(snapshot_path),
            str(csv_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return _ANSI.sub("", completed.stdout), snapshot


def _year_sections(text: str) -> Dict[str, str]:
    parts = re.split(r"Tax Year - (\d{4}/\d{2})", text)
    sections: Dict[str, str] = {}
    pairs = iter(parts[1:])
    for label, body in zip(pairs, pairs):
        sections[label] = body.split("Appendix", 1)[0]
    return sections


def _assert_section(section: str, report: Mapping[str, Any]) -> None:
    summary = report["summary"]
    assert _integer_after(section, "Number of disposals:") == int(summary["disposals"])
    assert _money_after(section, "Disposal proceeds:") == Decimal(summary["proceeds"])
    assert _money_after(section, "Allowable costs") == Decimal(summary["allowable_costs"])
    gains = _money_after(section, "Gains in the year, before losses:")
    assert gains == Decimal(summary["total_gain"])
    assert _money_after(section, "Losses in the year:") == Decimal(summary["losses"])
    if summary["split_date"]:
        assert _money_after(section, "Gains before ") == Decimal(summary["gain_before"])
        assert _money_after(section, "Gains on or after ") == Decimal(summary["gain_after"])
    assert _income_total(section) == Decimal(report["income"]["amount"]), section


def _assert_snapshot(snapshot: Mapping[str, Any], report: Mapping[str, Any], year: str) -> None:
    totals = snapshot["years"][year]
    assert Decimal(totals["capital_gains"]) == Decimal(report["summary"]["gain"])
    assert Decimal(totals["income"]) == Decimal(report["income"]["amount"])
    previous = str(int(year) - 1)
    assert snapshot["pools"].get(previous, {}).get("assets", []) == report["pools"]["start"]
    assert snapshot["pools"].get(year, {}).get("assets", []) == report["pools"]["end"]


def _integer_after(text: str, label: str) -> int:
    match = re.search(re.escape(label) + r"\s+(\d+)", text)
    assert match is not None, text
    return int(match.group(1))


def _money_after(text: str, label: str) -> Decimal:
    match = re.search(re.escape(label) + r"[^\n]*?" + _MONEY, text)
    assert match is not None, f"{label} missing from:\n{text}"
    return Decimal(match.group(1).replace(",", ""))


def _income_total(section: str) -> Decimal:
    income = section.split("Income", 1)[1].split("Margin Trading", 1)[0]
    amounts = re.findall(r"Total\b[^\n]*?" + _MONEY, income)
    assert amounts, section
    return Decimal(amounts[-1].replace(",", ""))


def _get(url: str) -> str:
    with urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def _post(url: str, body: bytes, content_type: str) -> str:
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{exc.code} {detail}") from exc


def _post_file(url: str, filename: str, data: bytes) -> str:
    boundary = "----deskboundary7MA4YWxkTrZu0gW"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("utf-8")
        + data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    return _post(url, body, f"multipart/form-data; boundary={boundary}")
