# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""HTTP server for the desk. This computer is open. Any other host must sign in."""

from __future__ import annotations

import mimetypes
import re
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

import jinja2

from ..report import ReportLog
from . import DEFAULT_TAX_YEAR
from .auth import (
    DeskAuth,
    Session,
    canonical_bind_host,
    cookie_is_secure,
    host_allowed,
    hostname_of,
    is_direct_loopback,
    is_tailscale_ip,
    safe_next,
)
from .config_view import config_snapshot
from .dashboard import dashboard
from .engine_run import supported_years
from .errors import DeskError
from .identity import LOOPBACK, actor_for, trusted_tailscale_user
from .koinly_check import compare_year, load_uploads, store_uploads
from .pack import pack_context, pack_lines, render_pdf
from .prices import fetch_missing, load_misses, missing_slots
from .rules import accountant_review_items
from .sa108 import sa108_view
from .store import Desk

_STATIC = Path(__file__).resolve().parent / "static"
_ROW_PATH = re.compile(r"^/transactions/([0-9A-Za-zT-]+)/([0-9]+)$")
_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).resolve().parent / "templates"),
    autoescape=jinja2.select_autoescape(["html"]),
)


def _gbp(value: object) -> str:
    """Format a calculator amount the way the terminal report does."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    return ReportLog.format_value(amount)


_ENV.filters["gbp"] = _gbp
_MAX_BODY = 64 * 1024 * 1024


def serve(root: Path, port: int, host: str = "127.0.0.1") -> None:
    """Serve ``root`` until the process is stopped."""
    httpd = bind_server(root, port, host)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def bind_server(root: Path, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Bind the desk. The host must be this computer, or a Tailscale address."""
    bind_host = canonical_bind_host(host)
    desk = Desk(root)
    desk_auth = DeskAuth(root)
    handler = _handler_for(desk, desk_auth, bind_host)
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((bind_host, port), handler)


class _Request:  # pylint: disable=too-few-public-methods
    """One HTTP request after the host and session checks."""

    def __init__(self, client: str) -> None:
        self.client = client
        self.cookie_out = ""
        self.raw = b""
        self.form: Dict[str, str] = {}
        self.session = Session(user="", csrf="", exp=0)
        self.direct = False
        self.host_name = ""


def _handler_for(  # pylint: disable=too-many-statements
    desk: Desk, desk_auth: DeskAuth, bind_host: str
) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        _req: _Request

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _dispatch(self, method: str) -> None:
            self._req = _Request(self.client_address[0])
            try:
                if method == "POST":
                    self._req.raw = self._body()
                if not self._admit(method):
                    return
                self._route(method)
            except DeskError as exc:
                self._error_page(str(exc))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._plain(500, f"Something went wrong: {exc}")

        def _admit(self, method: str) -> bool:
            decision = self._decision(method)
            if decision is None:
                return True
            status, message = decision
            if status == 303:
                self._redirect(message)
            else:
                self._plain(status, message)
            return False

        def _decision(self, method: str) -> Optional[Tuple[int, str]]:
            host_header = self.headers.get("Host", "") or ""
            self._req.host_name = hostname_of(host_header)
            if not host_allowed(host_header, bind_host):
                return 400, "This desk does not accept that host name."
            if self._req.client not in LOOPBACK and not is_tailscale_ip(self._req.client):
                return 403, "This desk accepts this computer or a Tailscale address only."
            self._open_session()
            if method == "POST":
                self._req.form = _form(self.headers.get("Content-Type", ""), self._req.raw)
                token = self._req.form.get("csrf", "")
                if not desk_auth.csrf_ok(self._req.session, token):
                    return 403, "The form token did not match. Reload the page and try again."
            if self._req.direct:
                return None
            return self._remote_decision(method)

        def _remote_decision(self, method: str) -> Optional[Tuple[int, str]]:
            path = urlparse(self.path).path
            if not desk_auth.password_is_set():
                return (
                    403,
                    "Set a password with bittytax_web --set-password "
                    "before opening this desk from another device.",
                )
            if method == "GET" and path.startswith("/static/"):
                return None
            if path == "/login":
                return None
            if self._req.session.user:
                return None
            if method == "GET":
                target = self.path
                if not target.startswith("/") or target.startswith("//"):
                    target = "/"
                return 303, "/login?next=" + quote(target, safe="/?=&")
            return 403, "Sign in before sending that form."

        def _open_session(self) -> None:
            self._req.direct = is_direct_loopback(self._req.host_name, self._req.client)
            found = desk_auth.open_session(self.headers.get("Cookie", "") or "")
            self._remember(found if found is not None else desk_auth.new_session())

        def _remember(self, session: Session) -> None:
            self._req.session = session
            if not session.fresh:
                return
            secure = cookie_is_secure(self._req.host_name, self._req.client, self.headers)
            self._req.cookie_out = desk_auth.cookie(session, secure=secure)

        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path.startswith("/static/"):
                self._static(path)
                return
            if path == "/login" and method == "GET":
                self._login_get()
                return
            if path == "/login" and method == "POST":
                self._login_post()
                return
            if path == "/logout" and method == "POST":
                self._logout()
                return
            row = _ROW_PATH.match(path)
            if row and method == "GET":
                self._transaction(f"{row.group(1)}:{row.group(2)}", "")
                return
            if row and method == "POST":
                self._reclassify(f"{row.group(1)}:{row.group(2)}")
                return
            routes = {
                ("GET", "/"): self._home,
                ("GET", "/import"): self._import_form,
                ("POST", "/import"): self._import_post,
                ("GET", "/transactions"): lambda: self._transactions(query),
                ("GET", "/review"): lambda: self._review(query),
                ("GET", "/health"): lambda: self._health(query),
                ("GET", "/report"): lambda: self._report(query),
                ("GET", "/report/pack"): lambda: self._pack(query, pdf=False),
                ("GET", "/report/pack.pdf"): lambda: self._pack(query, pdf=True),
                ("GET", "/report.csv"): lambda: self._report_csv(query),
                ("GET", "/koinly"): lambda: self._koinly(query),
                ("POST", "/koinly"): self._koinly_post,
                ("GET", "/prices"): self._prices,
                ("POST", "/prices/fetch"): self._prices_fetch,
                ("GET", "/config"): self._config,
                ("GET", "/about"): self._about,
            }
            handler = routes.get((method, path))
            if handler is None:
                self._plain(404, "That page is not on this desk.")
                return
            handler()

        def _home(self) -> None:
            self._page(
                "home.html",
                "Desk",
                section="home",
                counts=desk.counts(),
                imports=desk.imports(),
                board=dashboard(desk),
            )

        def _import_form(self) -> None:
            self._page("import.html", "Import", section="import", error="")

        def _import_post(self) -> None:
            filename, payload = _uploaded_file(self.headers.get("Content-Type", ""), self._req.raw)
            entry = desk.import_bytes(filename, payload)
            self._redirect(f"/import?imported={quote(entry['id'])}")

        def _transactions(self, query: Mapping[str, List[str]]) -> None:
            filters = {
                key: _field(query, key)
                for key in ("query", "date_from", "date_to", "asset", "wallet", "kind", "status")
            }
            page = _page_number(_field(query, "page"))
            listing = desk.transactions(filters, page)
            kept = {key: value for key, value in filters.items() if value}
            self._page(
                "transactions.html",
                "Transactions",
                section="transactions",
                listing=listing,
                filters=filters,
                qs=urlencode(kept),
            )

        def _transaction(self, row_id: str, error: str) -> None:
            self._page(
                "transaction.html",
                "Reclassify",
                section="transactions",
                row=desk.transaction(row_id),
                error=error,
            )

        def _reclassify(self, row_id: str) -> None:
            form = self._req.form
            try:
                actor = actor_for(
                    self.headers,
                    self._req.client,
                    host=self._req.host_name,
                    session_user=self._req.session.user,
                )
                desk.reclassify(row_id, _form_value(form, "type"), _form_value(form, "why"), actor)
            except DeskError as exc:
                self._transaction(row_id, str(exc))
                return
            import_id, seq = row_id.split(":", 1)
            self._redirect(f"/transactions/{import_id}/{seq}?saved=1")

        def _login_get(self) -> None:
            if self._req.session.user:
                nxt = _field(parse_qs(urlparse(self.path).query), "next")
                self._redirect(safe_next(nxt or "/"))
                return
            self._login_form("")

        def _login_post(self) -> None:
            if not desk_auth.password_is_set():
                self._plain(
                    403, "Set a password with bittytax_web --set-password before signing in."
                )
                return
            if desk_auth.limiter.blocked(self._req.client):
                self._plain(429, "Too many sign-in attempts. Wait and try again.")
                return
            if not desk_auth.check_password(self._req.form.get("password", "")):
                desk_auth.limiter.fail(self._req.client)
                if desk_auth.limiter.blocked(self._req.client):
                    self._plain(429, "Too many sign-in attempts. Wait and try again.")
                    return
                self._login_form("That password did not match.")
                return
            desk_auth.limiter.clear(self._req.client)
            header_user = trusted_tailscale_user(
                self.headers, self._req.client, self._req.host_name
            )
            user = header_user or desk_auth.stored_user() or "desk"
            self._remember(desk_auth.new_session(user))
            self._redirect(safe_next(self._req.form.get("next", "/")))

        def _login_form(self, error: str) -> None:
            nxt = self._req.form.get("next") or _field(parse_qs(urlparse(self.path).query), "next")
            self._page(
                "login.html", "Sign in", section="login", error=error, next=safe_next(nxt or "/")
            )

        def _logout(self) -> None:
            self._remember(desk_auth.new_session())
            self._redirect("/")

        def _health(self, query: Mapping[str, List[str]]) -> None:
            year = _year(_field(query, "tax_year"))
            self._page(
                "health.html",
                "Health",
                section="health",
                health=desk.health(year),
                years=supported_years(),
                tax_year=year,
                accountant_review=accountant_review_items(year),
            )

        def _review(self, query: Mapping[str, List[str]]) -> None:
            year = _year(_field(query, "tax_year"))
            self._page(
                "review.html",
                "Review",
                section="review",
                review=desk.review(year),
                years=supported_years(),
                tax_year=year,
            )

        def _report(self, query: Mapping[str, List[str]]) -> None:
            year = _year(_field(query, "tax_year"))
            ran = _field(query, "run") == "1"
            report = desk.report(year) if ran else None
            self._page(
                "report.html",
                "Report",
                section="report",
                report=report,
                sa108=sa108_view(report, year) if report else None,
                years=supported_years(),
                tax_year=year,
                records_path=str(desk.records_path),
                error="",
            )

        def _pack(self, query: Mapping[str, List[str]], pdf: bool) -> None:
            year = _year(_field(query, "tax_year"))
            context = pack_context(desk, year)
            if pdf:
                body = render_pdf(pack_lines(context))
                if body is not None:
                    self._send(200, body, "application/pdf", f"bittytax-{year}-pack.pdf")
                    return
            self._page("pack.html", "Report pack", section="report", **context)

        def _koinly(self, query: Mapping[str, List[str]]) -> None:
            year = _year(_field(query, "tax_year"))
            summary, complete = load_uploads(desk.root, year)
            comparison = (
                compare_year(desk.report(year), summary, complete) if summary or complete else None
            )
            self._page(
                "koinly.html",
                "Koinly",
                section="koinly",
                comparison=comparison,
                years=supported_years(),
                tax_year=year,
            )

        def _koinly_post(self) -> None:
            fields, files = _parts(self.headers.get("Content-Type", ""), self._req.raw)
            year = _year(fields.get("tax_year", ""))
            summary = files.get("summary", ("", b""))[1]
            complete = files.get("complete", ("", b""))[1]
            if not summary and not complete:
                raise DeskError("Choose a Koinly summary or a complete tax report.")
            store_uploads(desk.root, year, summary, complete)
            self._redirect(f"/koinly?tax_year={year}")

        def _prices(self) -> None:
            rows = desk.all_rows()
            self._page(
                "prices.html",
                "Prices",
                section="prices",
                missing=missing_slots(rows),
                missed=load_misses(desk),
            )

        def _prices_fetch(self) -> None:
            fetch_missing(desk)
            self._redirect("/prices")

        def _report_csv(self, query: Mapping[str, List[str]]) -> None:
            year = _year(_field(query, "tax_year"))
            body = desk.report_csv(year).encode("utf-8")
            self._send(200, body, "text/csv; charset=utf-8", f"bittytax-{year}.csv")

        def _config(self) -> None:
            self._page(
                "config.html", "Config", section="config", config=config_snapshot(desk.wallets_path)
            )

        def _about(self) -> None:
            self._page("about.html", "About", section="about")

        def _page(self, template: str, title: str, **context: Any) -> None:
            context.setdefault("section", "")
            context.setdefault("error", "")
            context.setdefault("saved", "saved" in parse_qs(urlparse(self.path).query))
            context.setdefault("imported", _field(parse_qs(urlparse(self.path).query), "imported"))
            self._identity(context)
            body = _ENV.get_template(template).render(title=title, **context)
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

        def _error_page(self, message: str) -> None:
            context: Dict[str, Any] = {"title": "Problem", "section": "", "error": message}
            self._identity(context)
            body = _ENV.get_template("error.html").render(**context)
            self._send(400, body.encode("utf-8"), "text/html; charset=utf-8")

        def _identity(self, context: Dict[str, Any]) -> None:
            context.setdefault("csrf", self._req.session.csrf)
            context.setdefault("signed_in", bool(self._req.session.user))
            context.setdefault("actor", self._req.session.user)
            context.setdefault("health_count", desk.health_count(DEFAULT_TAX_YEAR))
            context.setdefault("accountant_review", [])

        def _static(self, path: str) -> None:
            name = path[len("/static/") :]
            if not name or "/" in name or name.startswith("."):
                self._plain(404, "Not found.")
                return
            file_path = _STATIC / name
            if not file_path.is_file():
                self._plain(404, "Not found.")
                return
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(200, file_path.read_bytes(), mime)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self._cookie_header()
            self.end_headers()

        def _plain(self, status: int, message: str) -> None:
            self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

        def _send(self, status: int, body: bytes, content_type: str, download: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self._security_headers()
            self._cookie_header()
            self.end_headers()
            self.wfile.write(body)

        def _cookie_header(self) -> None:
            if self._req.cookie_out:
                self.send_header("Set-Cookie", self._req.cookie_out)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.send_header("Cache-Control", "no-store")

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > _MAX_BODY:
                raise DeskError("That upload is larger than 64 MB.")
            return self.rfile.read(length)

    return Handler


def _year(value: str) -> int:
    if not value:
        return DEFAULT_TAX_YEAR
    try:
        year = int(value)
    except ValueError as exc:
        raise DeskError("The tax year must be a number.") from exc
    allowed = {item["year"] for item in supported_years()}
    if year not in allowed:
        raise DeskError("That tax year is not one the calculator supports.")
    return year


def _page_number(value: str) -> int:
    if not value:
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return 1


def _field(query: Mapping[str, List[str]], key: str) -> str:
    values = query.get(key, [])
    return values[0].strip() if values else ""


def _form(content_type: str, body: bytes) -> Dict[str, str]:
    if "multipart/form-data" in content_type:
        _filename, fields, _file = _multipart(content_type, body)
        return fields
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _form_value(form: Mapping[str, str], key: str) -> str:
    return form.get(key, "")


def _uploaded_file(content_type: str, body: bytes) -> Tuple[str, bytes]:
    _fields, files = _parts(content_type, body)
    uploaded = files.get("file")
    if uploaded is None and files:
        uploaded = next(iter(files.values()))
    if uploaded is None:
        raise DeskError("Choose a file to import.")
    filename, payload = uploaded
    return filename or "upload.csv", payload


def _multipart(content_type: str, body: bytes) -> Tuple[str, Dict[str, str], Optional[bytes]]:
    fields, files = _parts(content_type, body)
    if not files:
        return "", fields, None
    filename, payload = next(iter(files.values()))
    return filename, fields, payload


def _parts(content_type: str, body: bytes) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    if "multipart/form-data" not in content_type:
        raise DeskError("The import form was not sent as a file upload.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    fields: Dict[str, str] = {}
    files: Dict[str, Tuple[str, bytes]] = {}
    if not message.is_multipart():
        raise DeskError("The import form was empty.")
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            data = b""
        uploaded = part.get_filename()
        if uploaded:
            files[name] = (uploaded, data)
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files
