# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Sign-in, form tokens, trusted host names, and which addresses the desk will bind."""

from __future__ import annotations

import io
import json
import re
import threading
import urllib.request
from contextlib import contextmanager
from email.header import Header
from pathlib import Path
from typing import Iterator, Mapping, Optional, Tuple
from urllib.parse import urlencode

import pytest

from bittytax.web.__main__ import main
from bittytax.web.auth import (
    LOGIN_LIMIT,
    LOGIN_WINDOW,
    DeskAuth,
    canonical_bind_host,
    cookie_is_secure,
    host_allowed,
    safe_next,
)
from bittytax.web.errors import DeskError
from bittytax.web.identity import actor_for
from bittytax.web.server import bind_server

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "web-sample-koinly.csv"
GIFT_NOTE = "sample gift to reclassify"
TAILNET = "mark-mac.tail.ts.net"
_CSRF_META = re.compile(r'name="csrf-token" content="([^"]*)"')


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Leave 3xx and 4xx responses for the test to read."""

    def http_response(self, request: urllib.request.Request, response: object) -> object:
        del request
        return response

    def https_response(self, request: urllib.request.Request, response: object) -> object:
        return self.http_response(request, response)


class _Client:
    """Talks to the desk without following redirects, so a 303 can be checked."""

    def __init__(self, base: str, host: str = "") -> None:
        self.base = base
        self.host = host
        self.cookie = ""
        self.set_cookie = ""
        self.csrf = ""
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, path: str, headers: Optional[Mapping[str, str]] = None) -> Tuple[int, str, str]:
        return self._open(urllib.request.Request(self.base + path), headers)

    def post_form(
        self,
        path: str,
        fields: Mapping[str, str],
        headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[int, str, str]:
        payload = dict(fields)
        if self.csrf and "csrf" not in payload:
            payload["csrf"] = self.csrf
        body = urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._open(request, headers)

    def post_file(self, path: str, filename: str, data: bytes) -> Tuple[int, str, str]:
        boundary = "----deskboundary7MA4YWxkTrZu0gW"
        csrf = b""
        if self.csrf:
            csrf = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf"\r\n\r\n'
                f"{self.csrf}\r\n"
            ).encode("utf-8")
        body = (
            csrf
            + (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode("utf-8")
            + data
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._open(request, None)

    def _open(
        self, request: urllib.request.Request, headers: Optional[Mapping[str, str]]
    ) -> Tuple[int, str, str]:
        if self.host:
            request.add_header("Host", self.host)
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with self.opener.open(request, timeout=60) as response:
            self._take(response.headers)
            body = response.read().decode("utf-8")
            status = int(response.status)
            location = str(response.headers.get("Location") or "")
        found = _CSRF_META.search(body)
        if found and found.group(1):
            self.csrf = found.group(1)
        return status, body, location

    def _take(self, headers: object) -> None:
        getter = getattr(headers, "get", None)
        raw = getter("Set-Cookie") if callable(getter) else None
        if not raw:
            return
        self.set_cookie = str(raw)
        self.cookie = self.set_cookie.split(";", 1)[0].strip()


@contextmanager
def _serving(root: Path) -> Iterator[str]:
    httpd = bind_server(root, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(httpd.server_address[1])
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_set_password_stores_a_hash_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("correct-horse\n"))
    main(["--set-password", "--user", "mark", "--data-dir", str(tmp_path), "--no-browser"])
    path = tmp_path / "desk-password.json"
    stored = path.read_text(encoding="utf-8")
    assert "correct-horse" not in stored
    assert json.loads(stored)["user"] == "mark"
    assert path.stat().st_mode & 0o777 == 0o600
    auth = DeskAuth(tmp_path)
    assert auth.check_password("correct-horse")
    assert auth.check_password("wrong-horse") is False
    assert not (ROOT / "desk-password.json").exists()


def test_short_password_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit) as caught:
        main(["--set-password", "--data-dir", str(tmp_path), "--no-browser"])
    assert caught.value.code == 2
    assert not (tmp_path / "desk-password.json").exists()


def test_user_flag_requires_set_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--user", "mark", "--data-dir", str(tmp_path), "--no-browser", "--host", "0.0.0.0"])
    assert caught.value.code == 2
    assert "--user" in capsys.readouterr().err


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "1.2.3.4", "100.63.255.255", "100.128.0.1", "example.com"],
)
def test_bind_refuses_public_and_unspecified_addresses(host: str) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--host", host, "--no-browser", "--data-dir", "/tmp/unused-desk-auth"])
    assert caught.value.code == 2


def test_every_interface_message(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0", "--no-browser", "--data-dir", "/tmp/unused-desk-auth"])
    assert "every interface" in capsys.readouterr().err


def test_bind_accepts_loopback_and_the_tailscale_range() -> None:
    assert canonical_bind_host("127.0.0.1") == "127.0.0.1"
    assert canonical_bind_host("localhost") == "127.0.0.1"
    assert canonical_bind_host("::1") == "::1"
    assert canonical_bind_host("100.64.0.1") == "100.64.0.1"
    assert canonical_bind_host("100.127.255.254") == "100.127.255.254"
    with pytest.raises(DeskError):
        canonical_bind_host("0.0.0.0")


def test_host_header_allows_this_computer_and_tailnet_names() -> None:
    assert host_allowed("127.0.0.1:8765", "127.0.0.1")
    assert host_allowed("[::1]:8765", "127.0.0.1")
    assert host_allowed(TAILNET, "127.0.0.1")
    assert host_allowed("100.64.0.8", "100.64.0.8")
    assert host_allowed("evil.example", "127.0.0.1") is False
    assert host_allowed("ts.net", "127.0.0.1") is False
    assert host_allowed("notts.net", "127.0.0.1") is False
    assert host_allowed("evil.ts.net.attacker.com", "127.0.0.1") is False
    assert host_allowed("100.64.0.8", "127.0.0.1") is False
    assert host_allowed("", "127.0.0.1") is False


def test_next_stays_on_this_site() -> None:
    assert safe_next("/report?tax_year=2026") == "/report?tax_year=2026"
    assert safe_next("https://evil.example/phish") == "/"
    assert safe_next("//evil.example") == "/"
    assert safe_next("report") == "/"


def test_cookie_is_secure_for_a_tailnet_name_only() -> None:
    assert cookie_is_secure(TAILNET, "127.0.0.1", {}) is True
    assert cookie_is_secure("127.0.0.1", "127.0.0.1", {}) is False
    forwarded = {"X-Forwarded-Proto": "https"}
    assert cookie_is_secure("127.0.0.1", "127.0.0.1", forwarded) is True
    assert cookie_is_secure("127.0.0.1", "100.64.0.2", forwarded) is False


def test_actor_trusts_the_tailscale_header_only_from_the_serve_proxy() -> None:
    encoded = Header("Márk", "utf-8").encode()
    assert (
        actor_for({"Tailscale-User-Login": "mark@example.com"}, "127.0.0.1", host=TAILNET)
        == "mark@example.com"
    )
    assert actor_for({"Tailscale-User-Login": encoded}, "127.0.0.1", host=TAILNET) == "Márk"
    assert actor_for({}, "127.0.0.1", host=TAILNET, session_user="mark") == "mark"
    assert actor_for({"Tailscale-User-Login": "spoof@example.com"}, "127.0.0.1") == "local"
    assert (
        actor_for(
            {"Tailscale-User-Login": "spoof@example.com"},
            "100.64.0.8",
            host="100.64.0.8",
            session_user="mark",
        )
        == "mark"
    )
    with pytest.raises(DeskError):
        actor_for({"Tailscale-User-Login": "spoof@example.com"}, "100.64.0.8", host="100.64.0.8")
    with pytest.raises(DeskError):
        actor_for({}, "8.8.8.8")


def test_login_limit_window(tmp_path: Path) -> None:
    now = {"t": 1000.0}
    auth = DeskAuth(tmp_path, clock=lambda: now["t"])
    for _ in range(LOGIN_LIMIT):
        auth.limiter.fail("127.0.0.1")
    assert auth.limiter.blocked("127.0.0.1")
    now["t"] += LOGIN_WINDOW + 1
    assert auth.limiter.blocked("127.0.0.1") is False


def test_this_computer_stays_open_and_post_needs_a_token(tmp_path: Path) -> None:
    DeskAuth(tmp_path).set_password("correct-horse", "mark")
    with _serving(tmp_path) as base:
        client = _Client(base)
        status, body, location = client.get("/")
        assert status == 200
        assert location == ""
        assert "Local tax desk" in body
        assert "Secure" not in client.set_cookie
        key = tmp_path / "desk-session.key"
        assert key.is_file()
        assert key.stat().st_mode & 0o777 == 0o600
        request_status, request_body = _post_without_token(client, "/import")
        assert request_status == 403
        assert "form token" in request_body


def test_tailnet_requires_a_password_file(tmp_path: Path) -> None:
    with _serving(tmp_path) as base:
        client = _Client(base, TAILNET)
        status, body, _ = client.get("/")
        assert status == 403
        assert "--set-password" in body


def test_untrusted_host_is_rejected(tmp_path: Path) -> None:
    with _serving(tmp_path) as base:
        client = _Client(base, "evil.example")
        status, body, _ = client.get("/")
        assert status == 400
        assert "host" in body.casefold()


def test_tailnet_login_csrf_and_who_is_recorded(tmp_path: Path) -> None:
    DeskAuth(tmp_path).set_password("correct-horse", "mark")
    with _serving(tmp_path) as base:
        remote = _Client(base, TAILNET)
        status, _, location = remote.get("/")
        assert status == 303
        assert location.startswith("/login?next=")
        assert "Secure" in remote.set_cookie
        status, page, _ = remote.get("/login")
        assert status == 200
        assert "Sign in" in page
        status, denied, _ = remote.post_form(
            "/login", {"password": "correct-horse", "csrf": "nope"}
        )
        assert status == 403
        assert "form token" in denied
        status, _, location = remote.post_form(
            "/login",
            {"password": "correct-horse", "next": "https://evil.example/phish"},
        )
        assert status == 303
        assert location == "/"
        status, home, _ = remote.get("/")
        assert status == 200
        assert "Local tax desk" in home
        status, _, location = remote.post_file("/import", SAMPLE.name, SAMPLE.read_bytes())
        assert status == 303
        status, imported, _ = remote.get(location)
        assert "Imported" in imported
        status, listing, _ = remote.get("/transactions?query=" + GIFT_NOTE.replace(" ", "%20"))
        match = re.search(r'href="(/transactions/[^"]+)"', listing)
        assert match is not None
        status, _, location = remote.post_form(
            match.group(1),
            {"type": "Income", "why": "Sample gift is income."},
            headers={"Tailscale-User-Login": "mark@example.com"},
        )
        assert status == 303
        _, saved, _ = remote.get(location)
        assert "mark@example.com" in saved
        local = _Client(base)
        _, again, _ = local.get(match.group(1))
        assert "Sign in" not in again
        status, _, location = local.post_form(
            match.group(1),
            {"type": "Gift-Received", "why": "Put the sample back."},
            headers={"Tailscale-User-Login": "spoof@example.com"},
        )
        assert status == 303
        _, saved, _ = local.get(location)
        assert "spoof@example.com" not in saved
        assert "local" in saved
        del status


def test_sign_in_is_rate_limited(tmp_path: Path) -> None:
    DeskAuth(tmp_path).set_password("correct-horse")
    with _serving(tmp_path) as base:
        client = _Client(base, TAILNET)
        _, page, _ = client.get("/login")
        assert "Sign in" in page
        for _ in range(LOGIN_LIMIT - 1):
            status, body, _ = client.post_form("/login", {"password": "wrong-horse"})
            assert status == 200
            assert "did not match" in body
        status, body, _ = client.post_form("/login", {"password": "wrong-horse"})
        assert status == 429
        status, body, _ = client.post_form("/login", {"password": "correct-horse"})
        assert status == 429
        assert "Too many" in body


def _post_without_token(client: _Client, path: str) -> Tuple[int, str]:
    saved = client.csrf
    client.csrf = ""
    try:
        status, body, _ = client.post_form(path, {"ignored": "1"})
    finally:
        client.csrf = saved
    return status, body
