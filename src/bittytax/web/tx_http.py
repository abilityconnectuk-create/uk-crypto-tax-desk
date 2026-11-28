# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""HTTP for the transactions workspace. The calculator still files the figures."""

# The handler methods are the desk's HTTP surface. This module is that surface.
# pylint: disable=protected-access

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlencode

from .backup import backup_now
from .errors import DeskError
from .events import append_event
from .tag_set import document
from .tx_store import (
    add_rename,
    add_spam_token,
    delete_price,
    delete_rename,
    delete_spam_token,
    import_prices,
    latest_overrides,
    save_dust,
)
from .tx_workspace import (
    add_transaction,
    aggregate_rows,
    apply_import,
    build_page,
    export_csv,
    export_xlsx,
    open_row,
    prepare,
    read_filters,
    restore_rows,
    row_matches,
    save_row,
    selected_ids,
    settings_view,
    soft_delete,
    stage_import,
)
from .user_rules import apply_bulk

_OLD = {"reclassify", "tag", "spam", "ignore", "undo"}


def show_transactions(handler: Any, desk: Any, query: Mapping[str, List[str]]) -> None:
    view = build_page(desk, query, preview=_field(query, "preview") == "1")
    handler._page(
        "transactions.html",
        "Transactions",
        section="transactions",
        listing=view,
        filters=view["filters"],
        qs=urlencode(view["kept"]),
        sort=view["filters"]["sort"],
        per=view["per_page"],
        panel=_panel(desk, _field(query, "open")),
        panel_error="",
        open_id=_field(query, "open"),
        kinds=_types(),
        banner=view["banner"],
        error=_field(query, "error"),
    )


def show_transaction(handler: Any, desk: Any, row_id: str, error: str) -> None:
    try:
        row = open_row(desk, row_id)
    except DeskError as exc:
        handler._page(
            "transaction.html",
            "Transaction",
            section="transactions",
            row={"id": row_id, "type": "", "log": [], "allowed_types": [], "defi_tags": []},
            error=str(exc),
        )
        return
    handler._page(
        "transaction.html",
        "Transaction",
        section="transactions",
        row=row,
        error=error,
    )


def save_transaction(handler: Any, desk: Any, row_id: str) -> None:
    form = handler._req.form
    if form.get("editor") == "workspace":
        try:
            save_row(desk, row_id, form, handler._actor())
        except DeskError as exc:
            show_transaction(handler, desk, row_id, str(exc))
            return
        import_id, seq = row_id.split(":", 1)
        handler._redirect(f"/transactions/{import_id}/{seq}?saved=1")
        return
    try:
        desk.reclassify(
            row_id,
            form.get("type", ""),
            form.get("why", ""),
            handler._actor(),
            form.get("tag", ""),
        )
    except DeskError as exc:
        show_transaction(handler, desk, row_id, str(exc))
        return
    import_id, seq = row_id.split(":", 1)
    handler._redirect(f"/transactions/{import_id}/{seq}?saved=1")


def bulk(handler: Any, desk: Any) -> None:
    backup_now(desk.root, "before a bulk change", handler._actor())
    form = _multi(handler)
    action = _field(form, "action")
    who = handler._actor()
    ids = selected_ids(desk, form, form)
    new_type = _field(form, "bulk_type") or _field(form, "type")
    tag = _field(form, "bulk_tag") or _field(form, "tag")
    why = _field(form, "bulk_why") or _field(form, "why")
    if action in _OLD:
        apply_bulk(desk, ids, action, new_type, tag, why, who)
    elif action == "delete":
        soft_delete(desk, ids, who)
    elif action == "restore":
        restore_rows(desk, ids, who)
    elif action == "aggregate":
        aggregate_rows(desk, ids, who)
    else:
        raise DeskError("Choose a bulk action.")
    handler._redirect("/transactions")


def add(handler: Any, desk: Any) -> None:
    try:
        add_transaction(desk, handler._req.form, handler._actor())
    except DeskError as exc:
        show_transactions(handler, desk, {"error": [str(exc)]})
        return
    handler._redirect("/transactions?added=1")


def export_file(handler: Any, desk: Any, query: Mapping[str, List[str]], kind: str) -> None:
    source: Mapping[str, List[str]] = query
    if handler.command == "POST":
        source = _multi(handler)
    filters = read_filters(source)
    rows = [
        row
        for row in prepare(desk.all_rows(), _dust(desk), filters["clock"])
        if row_matches(row, filters)
    ]
    chosen = set(source.get("row_id") or [])
    if chosen:
        rows = [row for row in rows if row["id"] in chosen]
    overrides = latest_overrides(desk.root)
    if kind == "xlsx":
        body = export_xlsx(rows, overrides)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        name = "transactions.xlsx"
    else:
        body = export_csv(rows, overrides)
        mime = "text/csv; charset=utf-8"
        name = "transactions.csv"
    handler._send(200, body, mime, name)


def import_preview(handler: Any, desk: Any, filename: str, payload: bytes) -> None:
    try:
        stage_import(desk, payload, filename)
    except DeskError as exc:
        show_transactions(handler, desk, {"error": [str(exc)]})
        return
    handler._redirect("/transactions?preview=1")


def import_apply(handler: Any, desk: Any) -> None:
    backup_now(desk.root, "before a bulk change", handler._actor())
    apply_import(desk, handler._actor())
    handler._redirect("/transactions?imported=1")


def save_settings(handler: Any, desk: Any, upload: Optional[Tuple[str, bytes]] = None) -> None:
    form = handler._req.form
    action = form.get("settings_action", "")
    who = handler._actor()
    if action == "rename":
        add_rename(
            desk.root, form.get("source", ""), form.get("asset", ""), form.get("rename_to", "")
        )
    elif action == "rename-delete":
        delete_rename(desk.root, form.get("rule_id", ""))
    elif action == "spam":
        add_spam_token(desk.root, form.get("asset", ""))
    elif action == "spam-delete":
        delete_spam_token(desk.root, form.get("token_id", ""))
    elif action == "price-delete":
        delete_price(desk.root, form.get("price_id", ""))
    elif action == "dust":
        save_dust(desk.root, form.get("dust", ""))
    elif action == "prices":
        if upload is None:
            raise DeskError("Choose a price file.")
        _filename, payload = upload
        import_prices(desk.root, payload)
    else:
        raise DeskError("Choose a settings action.")
    append_event(desk.root, "tx-settings", who, "Updated transaction settings.", {"action": action})
    handler._redirect("/config")


def tag_years() -> List[dict]:
    """Year files for the settings page. Each one is unconfirmed."""
    found = []
    for ending in (2025, 2026, 2027):
        loaded = document(ending)
        if loaded:
            found.append(
                {
                    "slug": str(loaded.get("tax_year") or ""),
                    "banner": str(loaded.get("banner") or ""),
                    "confirmed": bool(loaded.get("confirmed")),
                    "note": str(loaded.get("note") or ""),
                }
            )
    return found


def config_lists(desk: Any) -> dict:
    return settings_view(desk.root)


def _panel(desk: Any, row_id: str) -> Any:
    if not row_id:
        return None
    try:
        return open_row(desk, row_id)
    except DeskError:
        return None


def _types() -> List[str]:
    from .convert import allowed_types  # pylint: disable=import-outside-toplevel

    return allowed_types()


def _dust(desk: Any) -> Any:
    from .tx_store import load_dust  # pylint: disable=import-outside-toplevel

    return load_dust(desk.root)


def _multi(handler: Any) -> dict:
    return parse_qs(handler._req.raw.decode("utf-8", errors="replace"), keep_blank_values=True)


def _field(query: Mapping[str, List[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""
