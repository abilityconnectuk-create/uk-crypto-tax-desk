# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Who is using the desk.

There is no login while the server is on this computer only. A later
deployment, behind a private network and a login, should replace
``actor_for`` and refuse any public bind.
"""

from __future__ import annotations

import os

from .errors import DeskError

LOOPBACK = frozenset({"127.0.0.1", "::1"})


def actor_for(headers: object, client_host: str) -> str:
    """Return the local actor, or refuse a connection from another computer."""
    if client_host not in LOOPBACK:
        raise DeskError("This desk accepts connections from this computer only.")
    # A future session check belongs here. The header map is unused until then.
    del headers
    actor = os.environ.get("UK_CRYPTO_TAX_DESK_ACTOR", "local").strip()
    return actor or "local"
