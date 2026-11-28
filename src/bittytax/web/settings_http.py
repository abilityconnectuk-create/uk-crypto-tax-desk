# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""HTTP for the settings page. The files stay in the data directory."""

# pylint: disable=protected-access

from __future__ import annotations

from typing import Any

from .desk_settings import (
    recalculate,
    reset_data,
    save_display,
    save_general,
    save_pricing,
    save_tax,
)
from .errors import DeskError


def save_desk(handler: Any, desk: Any) -> None:
    """One settings form. The action names the tab that was saved."""
    form = handler._req.form
    action = form.get("settings_action", "")
    who = handler._actor()
    if action == "general":
        save_general(desk.root, form, who)
    elif action == "tax":
        save_tax(desk.root, form, who)
    elif action == "display":
        save_display(desk.root, form, who)
    elif action == "pricing":
        save_pricing(desk.root, form, who)
    elif action == "recalculate":
        result = recalculate(desk, who)
        if result["errors"]:
            raise DeskError(result["errors"][0])
    elif action == "reset":
        reset_data(desk.root, form.get("confirm", ""), who)
    else:
        raise DeskError("Choose a settings action.")
    handler._redirect("/config?saved=1")
