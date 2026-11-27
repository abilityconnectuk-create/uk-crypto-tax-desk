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
from .server import serve

_LOOPBACK = ("127.0.0.1", "localhost")


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bittytax_web",
        description="Local BittyTax desk. The server binds to 127.0.0.1 only.",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="directory for uploads and the ledger (default: ~/.uk-crypto-tax-desk)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="must be 127.0.0.1. Any other host is refused.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.host not in _LOOPBACK:
        sys.stderr.write("bittytax_web binds to 127.0.0.1 only.\n")
        raise SystemExit(2)
    root = _data_dir(args.data_dir)
    url = f"http://127.0.0.1:{args.port}/"
    sys.stderr.write(f"BittyTax desk at {url}\nData directory: {root}\n")
    if not args.no_browser:
        _open_browser(url)
    serve(root, args.port)


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
