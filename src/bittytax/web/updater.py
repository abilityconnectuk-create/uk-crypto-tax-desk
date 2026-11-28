# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Update this checkout in place. The branch is never switched and nothing is merged."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import DeskError

Runner = Callable[..., Any]


def repo_root() -> Optional[Path]:
    start = Path(__file__).resolve()
    for parent in start.parents:
        if (parent / ".git").exists():
            return parent
    return None


def describe(repo: Optional[Path], run: Runner) -> Dict[str, str]:
    if repo is None or not (repo / ".git").exists():
        return {
            "is_repo": "no",
            "branch": "",
            "commit": "",
            "note": "This checkout is not a git repository.",
        }
    return {
        "is_repo": "yes",
        "branch": _text(repo, ["rev-parse", "--abbrev-ref", "HEAD"], run),
        "commit": _text(repo, ["rev-parse", "--short", "HEAD"], run),
        "note": "",
    }


def update_checkout(repo: Path, run: Runner) -> str:
    """Fast-forward the current branch and reinstall it. Local changes stop the update."""
    if not (repo / ".git").exists():
        raise DeskError("This checkout is not a git repository.")
    if _text(repo, ["status", "--porcelain"], run).strip():
        raise DeskError("Local changes are present. The update was not run.")
    pull = _run(repo, ["pull", "--ff-only"], run)
    if pull.returncode != 0:
        raise DeskError("The update could not fast-forward. Nothing was merged.")
    installed = run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        raise DeskError("The code was updated, but the install failed.")
    return "Restart the desk to load the update."


def _text(repo: Path, args: List[str], run: Runner) -> str:
    result = _run(repo, args, run)
    if result.returncode != 0:
        raise DeskError("Git could not read this checkout.")
    return str(result.stdout or "").strip()


def _run(repo: Path, args: List[str], run: Runner) -> Any:
    return run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
