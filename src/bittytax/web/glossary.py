# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Plain-language lines for the desk.

A reviewer can edit the wording in this file. These lines are not tax rules
and they are not applied to a calculation. They are not advice.
"""

from __future__ import annotations

from typing import Dict

# One entry per term. ``text`` is a single sentence the page can show.
GLOSSARY: Dict[str, Dict[str, str]] = {
    "s104": {
        "term": "Section 104 pool",
        "text": (
            "The Section 104 pool is the running total of an asset's quantity and "
            "allowable cost after the same-day and 30-day matching. "
            "This is a plain description, not advice."
        ),
    },
    "same_day": {
        "term": "same-day rule",
        "text": (
            "The same-day rule matches a disposal with an acquisition of the same "
            "asset on the same day, before the pool is used. "
            "This is a plain description, not advice."
        ),
    },
    "thirty_day": {
        "term": "30-day rule",
        "text": (
            "The 30-day rule can match a disposal with an acquisition of the same "
            "asset in the following 30 days, including a date after 5 April. "
            "This is a plain description, not advice."
        ),
    },
    "sa108": {
        "term": "SA108 boxes",
        "text": (
            "SA108 is the capital gains summary in Self Assessment. The box numbers "
            "here are copied from the rule file and are not confirmed. This is not advice."
        ),
    },
    "annual_exempt": {
        "term": "annual exempt amount",
        "text": (
            "The annual exempt amount is the capital gains allowance for the tax year. "
            "It is shown here and is not deducted from the gain. This is not advice."
        ),
    },
    "allowable_costs": {
        "term": "allowable costs",
        "text": (
            "Allowable costs are the amounts the calculator sets against a disposal, "
            "including the pooled cost and certain fees. This is a plain description, "
            "not advice."
        ),
    },
}
