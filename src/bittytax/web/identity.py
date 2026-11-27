"""
Decide whose name is written on a reclassification.

A browser on this computer is the local operator. When Tailscale Serve proxies
a tailnet browser, the request still arrives from this computer, and Serve
adds the signed-in tailnet user. That header is trusted only in that case.
A peer that connects straight to a Tailscale address can set the same header
itself, so the desk uses the signed-in session instead.
"""

from __future__ import annotations

import os
from email.header import decode_header
from typing import Mapping

from .auth import is_direct_loopback, is_tailnet_name, is_tailscale_ip
from .errors import DeskError

LOOPBACK = frozenset({"127.0.0.1", "::1"})
_TAILSCALE_LOGIN = "Tailscale-User-Login"


def actor_for(
    headers: object,
    client_host: str,
    host: str = "",
    session_user: str = "",
) -> str:
    """Return the name stored on a change."""
    if is_direct_loopback(host, client_host):
        return _local_actor()
    if client_host in LOOPBACK and is_tailnet_name(host):
        login = _tailscale_login(headers)
        if login:
            return login
        if session_user:
            return session_user
        raise DeskError("Sign in before changing a record.")
    if is_tailscale_ip(client_host):
        if session_user:
            return session_user
        raise DeskError("Sign in before changing a record.")
    raise DeskError("This desk accepts connections from this computer only.")


def trusted_tailscale_user(headers: object, client_host: str, host: str) -> str:
    """Return the tailnet user when Serve added it on a local connection.

    A browser that connects straight to a Tailscale address can send the same
    header, so that case returns an empty string.
    """
    if client_host in LOOPBACK and is_tailnet_name(host):
        return _tailscale_login(headers)
    return ""


def _local_actor() -> str:
    actor = os.environ.get("UK_CRYPTO_TAX_DESK_ACTOR", "local").strip()
    return actor or "local"


def _tailscale_login(headers: object) -> str:
    raw = _header(headers, _TAILSCALE_LOGIN)
    if not raw:
        return ""
    return _clean(_decode_words(raw))


def _header(headers: object, name: str) -> str:
    if isinstance(headers, Mapping):
        value = headers.get(name, "")
        return "" if value is None else str(value)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name, "")
    return "" if value is None else str(value)


def _decode_words(value: str) -> str:
    """Decode an RFC 2047 header value. Plain text is returned unchanged."""
    try:
        parts = decode_header(value)
    except (ValueError, LookupError):
        return value
    chunks: list[str] = []
    for text, encoding in parts:
        if isinstance(text, bytes):
            chunks.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks)


def _clean(value: str) -> str:
    text = " ".join(value.split())
    if not text or len(text) > 200:
        return ""
    return text
