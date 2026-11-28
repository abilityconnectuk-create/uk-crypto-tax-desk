# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""SA108 boxes come from config and the calculator's gross figures."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict
from urllib.request import urlopen

from bittytax.web.rules import (
    accountant_review_items,
    confirmed_flags,
    list_rule_years,
    load_rules,
    load_sa108,
    parameter,
    sa108_publication_url,
    source_urls,
)
from bittytax.web.sa108 import sa108_view
from bittytax.web.server import bind_server
from bittytax.web.store import Desk

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
_SA108 = "https://www.gov.uk/government/publications/self-assessment-capital-gains-summary-sa108"
_FORBIDDEN = {str(number) for number in range(14, 23)}


def test_mappings_stay_unconfirmed_and_use_the_crypto_boxes_only() -> None:
    for ending in (2025, 2026):
        sa108 = load_sa108(ending)
        rules = load_rules(ending)
        assert sa108 is not None
        assert rules is not None
        assert confirmed_flags(sa108) and all(flag is False for flag in confirmed_flags(sa108))
        assert confirmed_flags(rules) and all(flag is False for flag in confirmed_flags(rules))
        boxes = {str(item["box"]) for item in sa108["boxes"]}
        assert {"13.1", "13.2", "13.3", "13.4", "13.5", "13.6", "13.7", "13.8"} <= boxes
        assert boxes.isdisjoint(_FORBIDDEN)
        assert "net" not in {item["key"] for item in sa108["boxes"]}
        assert _SA108 in source_urls(sa108)
        assert _SA108 in source_urls(rules)
        assert sa108["annual_exempt_amount"]["value"] == "3000"
        assert sa108["annual_exempt_amount"]["deduct_from_boxes"] is False
        assert parameter(ending, "annual_exempt_amount") == "3000"
        assert parameter(ending, "proceeds_reporting_threshold") == "50000"
        assert parameter(ending, "gains_reporting_threshold") == "3000"
    early = load_sa108(2025)
    later = load_sa108(2026)
    assert early is not None
    assert later is not None
    assert early["split"]["applies"] is True
    assert "51" in {item["box"] for item in early["boxes"]}
    assert later["split"]["applies"] is False
    assert "51" not in {item["box"] for item in later["boxes"]}
    assert sa108_publication_url() == _SA108


def test_boxes_use_gross_gains_and_do_not_use_a_four_times_allowance_test() -> None:
    view = sa108_view(_report(proceeds="15000.00", gains="70.00"), 2026)
    assert view is not None
    amounts = {box["box"]: box["value"] for box in view["boxes"]}
    assert amounts["13.1"] == "2"
    assert amounts["13.2"] == "15000.00"
    assert amounts["13.3"] == "40.00"
    assert amounts["13.4"] == "70.00"
    assert amounts["13.5"] == "10.00"
    assert amounts["13.4"] != "60.00"
    assert amounts["45"] == ""
    assert amounts["53"] == ""
    assert view["allowance"]["value"] == "3000"
    assert view["allowance"]["deducted"] is False
    assert view["banner"] == "AWAITING TAX REVIEW"
    assert view["trigger"]["hit"] is False
    over_proceeds = sa108_view(_report(proceeds="50000.01", gains="1.00"), 2026)
    assert over_proceeds is not None and over_proceeds["trigger"]["hit"] is True
    at_limit = sa108_view(_report(proceeds="50000.00", gains="3000.00"), 2026)
    assert at_limit is not None and at_limit["trigger"]["hit"] is False
    over_gains = sa108_view(_report(proceeds="1.00", gains="3000.01"), 2026)
    assert over_gains is not None and over_gains["trigger"]["hit"] is True


def test_2024_25_tags_disposals_around_30_october() -> None:
    view = sa108_view(_report(proceeds="100.00", gains="60.00"), 2025)
    assert view is not None and view["split"] is not None
    periods = {row["date"]: row["period"] for row in view["split"]["disposals"]}
    assert periods["2024-10-01"] == "before"
    assert periods["2024-11-15"] == "on or after"
    assert view["split"]["rates_before"] == "10% and 20%"
    assert view["split"]["rates_after"] == "18% and 24%"
    titles = [item["title"] for item in accountant_review_items(2025)]
    assert "SA108 box 53" in titles
    assert "SA108 box 51" in titles
    assert "SA108 box 45" in titles
    assert "SA108 box 47" in titles


def test_report_page_shows_the_unconfirmed_banner(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.import_bytes(SAMPLE.name, SAMPLE.read_bytes())
    httpd = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = int(httpd.server_address[1])
    try:
        with urlopen(f"http://127.0.0.1:{port}/report?tax_year=2025&run=1", timeout=60) as response:
            page = response.read().decode("utf-8")
        with urlopen(f"http://127.0.0.1:{port}/health?tax_year=2026", timeout=60) as response:
            health = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert "AWAITING TAX REVIEW" in page
    assert "13.1" in page
    assert "13.4" in page
    assert "13.5" in page
    assert "2024-10-30" in page
    assert _SA108 in page
    assert "SA108 box 53" in health
    assert any(item["slug"] == "2024-25" for item in list_rule_years())


def _report(proceeds: str, gains: str) -> Dict[str, Any]:
    return {
        "summary": {
            "disposals": "2",
            "proceeds": proceeds,
            "allowable_costs": "40.00",
            "total_gain": gains,
            "losses": "10.00",
            "gain": "60.00",
            "gain_before": "20.00",
            "gain_after": "40.00",
        },
        "disposals": [
            {
                "asset": "BTC",
                "date": "2024-10-01",
                "date_iso": "2024-10-01",
                "gain": "20.00",
                "proceeds": "50.00",
            },
            {
                "asset": "BTC",
                "date": "2024-11-15",
                "date_iso": "2024-11-15",
                "gain": "40.00",
                "proceeds": "50.00",
            },
        ],
    }
