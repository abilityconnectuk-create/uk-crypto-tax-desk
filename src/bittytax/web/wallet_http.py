# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""HTTP for the wallets page. Keys are not copied into the response."""

# The handler methods are the desk's HTTP surface. This module is that surface.
# pylint: disable=protected-access

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .errors import DeskError
from .tag_set import catalogue
from .wallet_catalogue import chains, find_integration, integrations
from .wallets_desk import (
    accept_row,
    add_wallets,
    delete_wallet,
    describe_wallet,
    import_file,
    list_page,
    reconnect,
    sync_all,
    sync_wallet,
)


def handle(handler: Any, desk: Any, method: str, path: str) -> None:
    """Serve /wallets and its forms."""
    query = parse_qs(urlparse(handler.path).query)
    if path == "/wallets" and method == "GET":
        _list(handler, desk, query)
    elif path == "/wallets/add" and method == "GET":
        _add_form(handler, _first(query, "integration"), "", None, {})
    elif path == "/wallets/add" and method == "POST":
        _add_post(handler, desk)
    elif path == "/wallets/sync" and method == "POST":
        _sync_one(handler, desk)
    elif path == "/wallets/sync-all" and method == "POST":
        _sync_every(handler, desk)
    elif path == "/wallets/reconnect" and method == "GET":
        _key_form(handler, desk, "wallet_reconnect.html", "Reconnect", query)
    elif path == "/wallets/reconnect" and method == "POST":
        _reconnect(handler, desk)
    elif path == "/wallets/import" and method == "GET":
        _key_form(handler, desk, "wallet_import.html", "Import file", query)
    elif path == "/wallets/import" and method == "POST":
        _import(handler, desk)
    elif path == "/wallets/delete" and method == "GET":
        _key_form(handler, desk, "wallet_delete.html", "Delete wallet", query)
    elif path == "/wallets/delete" and method == "POST":
        _delete(handler, desk)
    elif path == "/wallets/accept" and method == "POST":
        accept_posted(handler, desk)
    else:
        handler._plain(404, "That page is not on this desk.")


def accept_posted(handler: Any, desk: Any) -> None:
    """Accept one staged row from Review or from the wallets page."""
    form = handler._req.form
    accept_row(desk, form.get("row_id", ""), _who(handler))
    target = form.get("next") or "/review"
    if target not in {"/wallets", "/review"}:
        target = "/review"
    year = form.get("tax_year", "").strip()
    if year:
        target = f"{target}?tax_year={year}"
    handler._redirect(target)


def _list(handler: Any, desk: Any, query: Mapping[str, List[str]]) -> None:
    listing = list_page(desk, _number(_first(query, "page"), 1), _number(_first(query, "per"), 10))
    handler._page("wallets.html", "Wallets", section="wallets", listing=listing)


def _add_form(
    handler: Any,
    integration: str,
    error: str,
    choice: Optional[Mapping[str, Any]],
    spec: Mapping[str, Any],
) -> None:
    chosen = None
    if integration:
        try:
            chosen = find_integration(integration)
        except DeskError as exc:
            error = error or str(exc)
    handler._page(
        "wallet_add.html",
        "Add wallet",
        section="wallets",
        integrations=integrations(),
        chains=chains(),
        chosen=chosen,
        checked=_checked(chosen, spec),
        error=error,
        choice=choice or {},
        spec=spec,
        tags=_tags(),
    )


def _add_post(handler: Any, desk: Any) -> None:
    spec = _spec(handler._req.form)
    try:
        result = add_wallets(desk, spec, upload=_upload(handler), who=_who(handler))
    except DeskError as exc:
        _add_form(handler, spec["integration"], str(exc), None, spec)
        return
    if result["ok"] == "choice":
        _add_form(handler, spec["integration"], "", result, spec)
        return
    handler._redirect("/wallets")


def _sync_one(handler: Any, desk: Any) -> None:
    try:
        sync_wallet(desk, handler._req.form.get("wallet_id", ""), who=_who(handler))
    except DeskError:
        handler._redirect("/wallets")
        return
    handler._redirect("/wallets")


def _sync_every(handler: Any, desk: Any) -> None:
    sync_all(desk, who=_who(handler))
    handler._redirect("/wallets")


def _reconnect(handler: Any, desk: Any) -> None:
    form = handler._req.form
    try:
        reconnect(desk, form.get("wallet_id", ""), form, who=_who(handler))
    except DeskError as exc:
        handler._page(
            "wallet_reconnect.html",
            "Reconnect",
            section="wallets",
            wallet=describe_wallet(desk, form.get("wallet_id", "")),
            error=str(exc),
        )
        return
    handler._redirect("/wallets")


def _import(handler: Any, desk: Any) -> None:
    uploaded = _upload(handler)
    if uploaded is None:
        raise DeskError("Choose a file to import.")
    filename, payload = uploaded
    import_file(desk, handler._req.form.get("wallet_id", ""), filename, payload, _who(handler))
    handler._redirect("/review")


def _delete(handler: Any, desk: Any) -> None:
    form = handler._req.form
    delete_wallet(desk, form.get("wallet_id", ""), form.get("confirm", ""), _who(handler))
    handler._redirect("/wallets")


def _key_form(
    handler: Any, desk: Any, template: str, title: str, query: Mapping[str, List[str]]
) -> None:
    wallet = describe_wallet(desk, _first(query, "wallet"))
    handler._page(template, title, section="wallets", wallet=wallet, error="")


def _spec(form: Mapping[str, str]) -> Dict[str, Any]:
    selected = []
    for key, value in form.items():
        if key.startswith("chain_") and value == "yes":
            selected.append(key[len("chain_") :])
    return {
        "integration": form.get("integration", "").strip(),
        "name": form.get("name", "").strip(),
        "method": form.get("method", "").strip(),
        "chains": selected,
        "address": form.get("address", "").strip(),
        "start_date": form.get("start_date", "").strip(),
        "deposit_tag": form.get("deposit_tag", "").strip(),
        "ignore_reported": form.get("ignore_reported", "") == "yes",
        "explorer_url": form.get("explorer_url", "").strip(),
        "rpc_url": form.get("rpc_url", "").strip(),
        "api_key": form.get("api_key", ""),
        "api_secret": form.get("api_secret", ""),
        "passphrase": form.get("passphrase", ""),
        "decision": form.get("decision", "").strip(),
    }


def _checked(chosen: Optional[Mapping[str, str]], spec: Mapping[str, Any]) -> List[str]:
    picked = [str(item) for item in spec.get("chains") or []]
    if picked:
        return picked
    if not chosen:
        return []
    if chosen["id"] in {item["id"] for item in chains()}:
        return [chosen["id"]]
    if chosen.get("family") == "solana":
        return ["solana"]
    if chosen.get("family") == "bitcoin":
        return ["bitcoin"]
    if chosen.get("family") == "evm":
        return ["ethereum"]
    return []


def _tags() -> List[Dict[str, str]]:
    found = []
    for item in catalogue():
        ident = str(item.get("id") or "")
        if ident:
            found.append({"id": ident, "label": str(item.get("label") or ident)})
    return found


def _upload(handler: Any) -> Optional[Tuple[str, bytes]]:
    content = str(handler.headers.get("Content-Type", ""))
    if "multipart/form-data" not in content:
        return None
    header = f"Content-Type: {content}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + bytes(handler._req.raw))
    if not message.is_multipart():
        return None
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in str(disposition) or not part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = b""
        return part.get_filename() or "upload.csv", payload
    return None


def _who(handler: Any) -> str:
    return str(handler._actor())


def _first(query: Mapping[str, List[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def _number(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default
