# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Start the local desk: ``python -m bittytax.web``."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Sequence

from . import DEFAULT_PORT
from .auth import DeskAuth, canonical_bind_host, read_new_password
from .errors import DeskError
from .server import serve


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bittytax_web",
        description=(
            "Local BittyTax desk. The server binds to 127.0.0.1 unless --host "
            "is a Tailscale address in 100.64.0.0/10."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="directory for uploads and the ledger (default: ~/.uk-crypto-tax-desk)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="127.0.0.1, or a Tailscale address in 100.64.0.0/10. Never 0.0.0.0.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="store a password for browsers that are not on this computer, then exit",
    )
    parser.add_argument(
        "--user",
        default="",
        help="name for the change log when Tailscale sends none (with --set-password)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.user and not args.set_password:
        sys.stderr.write("--user is used with --set-password.\n")
        raise SystemExit(2)
    root = _data_dir(args.data_dir)
    if args.set_password:
        _store_password(root, args.user)
        return
    try:
        bind_host = canonical_bind_host(args.host)
    except DeskError as exc:
        sys.stderr.write(str(exc) + "\n")
        raise SystemExit(2) from exc
    url = _local_url(bind_host, args.port)
    sys.stderr.write(f"BittyTax desk at {url}\nData directory: {root}\n")
    sys.stderr.write(
        "To open this from another device on your tailnet, run: "
        f"tailscale serve --bg {args.port}\n"
        "Do not run tailscale funnel. Funnel publishes the address on the public internet.\n"
    )
    if not args.no_browser and bind_host in {"127.0.0.1", "::1"}:
        _open_browser(f"http://127.0.0.1:{args.port}/")
    serve(root, args.port, bind_host)


def _store_password(root: Path, user: str) -> None:
    try:
        password = read_new_password(sys.stdin)
        DeskAuth(root).set_password(password, user)
    except DeskError as exc:
        sys.stderr.write(str(exc) + "\n")
        raise SystemExit(2) from exc
    sys.stderr.write(f"Password stored in {root}\n")


def _local_url(bind_host: str, port: int) -> str:
    if bind_host == "::1":
        return f"http://[::1]:{port}/"
    return f"http://{bind_host}:{port}/"


def _data_dir(chosen: str) -> Path:
    if chosen:
        return Path(chosen).expanduser()
    env = os.environ.get("UK_CRYPTO_TAX_DESK_DATA", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".uk-crypto-tax-desk"


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except OSError:
        pass


if __name__ == "__main__":
    main()
