"""
Password, session cookie, and host checks for the local desk.

The password hash and the session key live in the data directory. They are
never written into the repository. A browser on this computer can still open
the desk without a password. Any other accepted host must sign in.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from .errors import DeskError

PASSWORD_FILE = "desk-password.json"
SESSION_FILE = "desk-session.key"
COOKIE_NAME = "bittytax_desk"
MIN_PASSWORD = 8
SESSION_SECONDS = 7 * 24 * 60 * 60
LOGIN_LIMIT = 5
LOGIN_WINDOW = 10 * 60
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_TS_NET = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+ts\.net$")
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")

Clock = Callable[[], float]


@dataclass
class Session:
    """A signed browser session. An empty user means nobody has signed in."""

    user: str
    csrf: str
    exp: int
    fresh: bool = False


class LoginLimiter:
    """Counts failed sign-in attempts for one process."""

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        """Return whether this client must wait before another attempt."""
        with self._lock:
            return len(self._recent(key)) >= LOGIN_LIMIT

    def fail(self, key: str) -> None:
        """Record one rejected password."""
        with self._lock:
            recent = self._recent(key)
            recent.append(self._clock())
            self._failures[key] = recent

    def clear(self, key: str) -> None:
        """Forget failures after a correct password."""
        with self._lock:
            self._failures.pop(key, None)

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - LOGIN_WINDOW
        return [stamp for stamp in self._failures.get(key, []) if stamp >= cutoff]


class DeskAuth:
    """Reads the password hash and seals session cookies."""

    def __init__(self, root: Path, clock: Clock = time.time) -> None:
        self.root = root
        self._clock = clock
        self.limiter = LoginLimiter(clock)

    def password_is_set(self) -> bool:
        """Return whether a password hash is stored."""
        return (self.root / PASSWORD_FILE).is_file()

    def set_password(self, password: str, user: str = "") -> None:
        """Store a new password hash. The plain password is not written."""
        if len(password) < MIN_PASSWORD:
            raise DeskError(f"Use at least {MIN_PASSWORD} characters.")
        label = " ".join(user.split())
        if len(label) > 200:
            raise DeskError("That name is too long.")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        )
        payload = {
            "salt": salt.hex(),
            "hash": digest.hex(),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "user": label,
        }
        path = self.root / PASSWORD_FILE
        _write_private(path, json.dumps(payload, indent=2) + "\n")

    def stored_user(self) -> str:
        """Return the name stored beside the password, or an empty string."""
        path = self.root / PASSWORD_FILE
        if not path.is_file():
            return ""
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(stored, dict):
            return ""
        user = stored.get("user", "")
        return user if isinstance(user, str) else ""

    def check_password(self, password: str) -> bool:
        """Return whether the password matches the stored hash."""
        path = self.root / PASSWORD_FILE
        if not path.is_file():
            return False
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            salt = bytes.fromhex(str(stored["salt"]))
            expected = bytes.fromhex(str(stored["hash"]))
            n = int(stored["n"])
            r = int(stored["r"])
            p = int(stored["p"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p)
        return hmac.compare_digest(digest, expected)

    def new_session(self, user: str = "") -> Session:
        """Start a session. A signed-in session gets a fresh form token."""
        now = int(self._clock())
        return Session(
            user=user,
            csrf=secrets.token_urlsafe(24),
            exp=now + SESSION_SECONDS,
            fresh=True,
        )

    def open_session(self, cookie_header: str) -> Optional[Session]:
        """Return the session from the cookie, or None when it is missing."""
        token = cookie_value(cookie_header, COOKIE_NAME)
        if not token or "." not in token:
            return None
        body, signature = token.split(".", 1)
        expected = hmac.new(self._key(), body.encode("ascii"), hashlib.sha256).digest()
        try:
            given = _b64decode(signature)
        except ValueError:
            return None
        if not hmac.compare_digest(given, expected):
            return None
        try:
            payload = json.loads(_b64decode(body))
            user = str(payload["user"])
            csrf = str(payload["csrf"])
            exp = int(payload["exp"])
        except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if exp < int(self._clock()) or not csrf:
            return None
        return Session(user=user, csrf=csrf, exp=exp, fresh=False)

    def cookie(self, session: Session, *, secure: bool) -> str:
        """Return a Set-Cookie value for this session."""
        payload = {"user": session.user, "csrf": session.csrf, "exp": session.exp}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _b64encode(raw)
        signature = hmac.new(self._key(), body.encode("ascii"), hashlib.sha256).digest()
        token = body + "." + _b64encode(signature)
        parts = [
            f"{COOKIE_NAME}={token}",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
            f"Max-Age={SESSION_SECONDS}",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def csrf_ok(self, session: Session, given: str) -> bool:
        """Return whether the form token matches this session."""
        if not session.csrf or not given:
            return False
        return hmac.compare_digest(session.csrf, given)

    def _key(self) -> bytes:
        path = self.root / SESSION_FILE
        existing = _read_key(path)
        if existing:
            return existing
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raced = _read_key(path)
            if raced:
                return raced
            raise
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        os.chmod(path, 0o600)
        return key


def read_new_password(stream: object) -> str:
    """Read a new password from a prompt, or one line when input is piped."""
    isatty = getattr(stream, "isatty", None)
    if callable(isatty) and isatty():
        first = getpass.getpass("New desk password: ")
        second = getpass.getpass("Repeat password: ")
        if first != second:
            raise DeskError("The two passwords did not match.")
        password = first
    else:
        readline = getattr(stream, "readline", None)
        if not callable(readline):
            raise DeskError("No password was provided.")
        password = str(readline())
        if password.endswith("\n"):
            password = password[:-1]
    if len(password) < MIN_PASSWORD:
        raise DeskError(f"Use at least {MIN_PASSWORD} characters.")
    return password


def canonical_bind_host(host: str) -> str:
    """Return the address to bind, or raise when it is not allowed.

    The default is this computer only. An explicit Tailscale address in
    100.64.0.0/10 is also allowed. Listening on every interface is refused.
    """
    text = host.strip().lower()
    if text in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    if text == "::1":
        return "::1"
    if text in {"0.0.0.0", "::", ""}:
        raise DeskError(
            "bittytax_web refuses to listen on every interface. "
            "Use 127.0.0.1, or an explicit Tailscale address in 100.64.0.0/10."
        )
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        message = (
            "bittytax_web binds to 127.0.0.1, or an explicit Tailscale address in 100.64.0.0/10."
        )
        raise DeskError(message) from exc
    if address.is_loopback:
        return text
    if address in _TAILSCALE:
        return str(address)
    raise DeskError(
        "bittytax_web binds to 127.0.0.1, or an explicit Tailscale address "
        "in 100.64.0.0/10. Refusing " + text + "."
    )


def hostname_of(host_header: str) -> str:
    """Return the host name from a Host header, without the port."""
    value = host_header.strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 1 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def is_tailscale_ip(host: str) -> bool:
    """Return whether this address is inside the Tailscale carrier range."""
    try:
        return ipaddress.ip_address(host) in _TAILSCALE
    except ValueError:
        return False


def is_tailnet_name(name: str) -> bool:
    """Return whether this host is a tailnet name such as mac.tail.ts.net."""
    return bool(_TS_NET.fullmatch(name))


def is_direct_loopback(name: str, client: str) -> bool:
    """Return whether the browser opened the desk on this computer directly.

    An empty host with a loopback client counts as direct, so older callers
    that do not pass a host header stay on the local path.
    """
    if client not in {"127.0.0.1", "::1"}:
        return False
    return name in _LOOPBACK_NAMES or name == ""


def host_allowed(host_header: str, bind_host: str) -> bool:
    """Return whether this Host header is one the desk will answer."""
    name = hostname_of(host_header)
    if name in _LOOPBACK_NAMES or is_tailnet_name(name):
        return True
    if name == bind_host and (bind_host in _LOOPBACK_NAMES or is_tailscale_ip(bind_host)):
        return True
    return False


def cookie_is_secure(name: str, client: str, headers: object) -> bool:
    """Return whether the session cookie should be marked Secure.

    A tailnet name is always served over HTTPS by Tailscale Serve, even
    though the proxy connects to this process with plain HTTP. A browser on
    this computer uses plain HTTP, so that cookie must stay usable there.
    """
    if is_tailnet_name(name):
        return True
    if client not in {"127.0.0.1", "::1"}:
        return False
    proto = _header(headers, "X-Forwarded-Proto").split(",", maxsplit=1)[0].strip().lower()
    return proto == "https"


def safe_next(value: str) -> str:
    """Return a same-site path to open after sign-in."""
    text = value.strip()
    if not text.startswith("/") or text.startswith("//") or "://" in text:
        return "/"
    parts = urlsplit(text)
    if parts.scheme or parts.netloc:
        return "/"
    return text


def cookie_value(header: str, name: str) -> str:
    """Return one cookie value from a Cookie header."""
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value.strip().strip('"')
    return ""


def _header(headers: object, name: str) -> str:
    if isinstance(headers, Mapping):
        value = headers.get(name, "")
        return "" if value is None else str(value)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name, "")
    return "" if value is None else str(value)


def _read_key(path: Path) -> bytes:
    if not path.is_file():
        return b""
    key = path.read_bytes()
    return key if len(key) >= 32 else b""


def _write_private(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding)
