# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""2026-27 is a stub copy, and the rules page only displays the rule files."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from bittytax.web.rules import confirmed_flags, list_rule_years, load_rules, load_sa108
from bittytax.web.server import bind_server


def test_2026_27_is_an_unconfirmed_copy_of_2025_26() -> None:
    later = load_rules(2027)
    earlier = load_rules(2026)
    assert later is not None and earlier is not None
    assert later["stub"] is True
    assert "not a new determination" in later["note"]
    assert later["parameters"]["annual_exempt_amount"]["value"] == "3000"
    copied = earlier["parameters"]["basic_rate"]["value"]
    assert later["parameters"]["basic_rate"]["value"] == copied
    flags = confirmed_flags(later) + confirmed_flags(load_sa108(2027))
    assert flags and all(flag is False for flag in flags)
    slugs = [year["slug"] for year in list_rule_years()]
    assert slugs == ["2024-25", "2025-26", "2026-27"]


def test_config_page_shows_the_rules_and_does_not_edit_them(tmp_path: Path) -> None:
    httpd: ThreadingHTTPServer = bind_server(tmp_path, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/config", timeout=30) as response:
            page = response.read().decode()
    finally:
        httpd.shutdown()
    assert "2026-27" in page
    assert "AWAITING TAX REVIEW" in page
    assert "self-assessment-capital-gains-summary-sa108" in page
    assert "Loan drawdown" in page
    assert 'action="/config"' not in page
