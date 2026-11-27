# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""HTTP server for this computer only. There is no login; see identity.actor_for."""

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
from .config_view import config_snapshot
from .engine_run import supported_years
from .errors import DeskError
from .identity import actor_for
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


def serve(root: Path, port: int) -> None:
    """Serve ``root`` on 127.0.0.1 until the process is stopped."""
    httpd = bind_server(root, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def bind_server(root: Path, port: int) -> ThreadingHTTPServer:
    desk = Desk(root)
    handler = _handler_for(desk)
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def _handler_for(desk: Desk) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _dispatch(self, method: str) -> None:
            try:
                actor_for(self.headers, self.client_address[0])
            except DeskError as exc:
                self._plain(403, str(exc))
                return
            try:
                self._route(method)
            except DeskError as exc:
                self._error_page(str(exc))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._plain(500, f"Something went wrong: {exc}")

        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path.startswith("/static/"):
                self._static(path)
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
                ("GET", "/report"): lambda: self._report(query),
                ("GET", "/report.csv"): lambda: self._report_csv(query),
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
                "home.html", "Desk", section="home", counts=desk.counts(), imports=desk.imports()
            )

        def _import_form(self) -> None:
            self._page("import.html", "Import", section="import", error="")

        def _import_post(self) -> None:
            filename, payload = _uploaded_file(self.headers.get("Content-Type", ""), self._body())
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
            form = _form(self.headers.get("Content-Type", ""), self._body())
            try:
                actor = actor_for(self.headers, self.client_address[0])
                desk.reclassify(row_id, _form_value(form, "type"), _form_value(form, "why"), actor)
            except DeskError as exc:
                self._transaction(row_id, str(exc))
                return
            import_id, seq = row_id.split(":", 1)
            self._redirect(f"/transactions/{import_id}/{seq}?saved=1")

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
                years=supported_years(),
                tax_year=year,
                records_path=str(desk.records_path),
                error="",
            )

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
            body = _ENV.get_template(template).render(title=title, **context)
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

        def _error_page(self, message: str) -> None:
            body = _ENV.get_template("error.html").render(
                title="Problem", section="", error=message
            )
            self._send(400, body.encode("utf-8"), "text/html; charset=utf-8")

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
            self.end_headers()
            self.wfile.write(body)

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
    filename, _fields, payload = _multipart(content_type, body)
    if payload is None:
        raise DeskError("Choose a file to import.")
    return filename or "upload.csv", payload


def _multipart(content_type: str, body: bytes) -> Tuple[str, Dict[str, str], Optional[bytes]]:
    if "multipart/form-data" not in content_type:
        raise DeskError("The import form was not sent as a file upload.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    fields: Dict[str, str] = {}
    filename = ""
    payload: Optional[bytes] = None
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
            filename = uploaded
            payload = data
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return filename, fields, payload
