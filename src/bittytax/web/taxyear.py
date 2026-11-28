# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""UK tax year from a calendar date. No desk or calculator imports."""

from __future__ import annotations

from datetime import date

from ..config import config


def tax_year_containing(day: date) -> int:
    """Ending year of the UK tax year that contains ``day``. 6 April 2026 is 2027."""
    start = (config.start_of_year_month, config.start_of_year_day)
    if (day.month, day.day) >= start:
        return day.year + 1
    return day.year
