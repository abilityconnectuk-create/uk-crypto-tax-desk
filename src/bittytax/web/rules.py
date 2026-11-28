# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
"""Per-year tax parameters. The files are read only, and none are confirmed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .errors import DeskError

_SA108_URL = (
    "https://www.gov.uk/government/publications/self-assessment-capital-gains-summary-sa108"
)


def config_root() -> Path:
    """Directory that holds ``sa108/`` and ``rules/``."""
    chosen = os.environ.get("UK_CRYPTO_TAX_DESK_RULES", "").strip()
    if chosen:
        return Path(chosen).expanduser()
    start = Path(__file__).resolve()
    for parent in start.parents:
        candidate = parent / "config"
        if (candidate / "sa108").is_dir():
            return candidate
    raise DeskError("The tax-rule config directory was not found.")


def slug_for(ending_year: int) -> str:
    """``2026`` is the file ``2025-26``."""
    return f"{ending_year - 1}-{ending_year % 100:02d}"


def load_sa108(ending_year: int) -> Optional[Dict[str, Any]]:
    return _load_optional(config_root() / "sa108" / f"{slug_for(ending_year)}.yaml")


def load_rules(ending_year: int) -> Optional[Dict[str, Any]]:
    return _load_optional(config_root() / "rules" / f"{slug_for(ending_year)}.yaml")


def parameter(ending_year: int, name: str) -> str:
    """One parameter value, or an empty string when that year has no file."""
    rules = load_rules(ending_year) or {}
    item = rules.get("parameters", {}).get(name, {})
    if not isinstance(item, dict):
        return ""
    return str(item.get("value", ""))


def list_rule_years() -> List[Dict[str, Any]]:
    """Every year that has an SA108 file or a parameter file."""
    root = config_root()
    slugs = set()
    for folder in ("sa108", "rules"):
        path = root / folder
        if not path.is_dir():
            continue
        for item in path.glob("*.yaml"):
            slugs.add(item.stem)
    years = []
    for slug in sorted(slugs):
        ending = _ending_year(slug)
        years.append(
            {
                "slug": slug,
                "ending_year": ending,
                "sa108": load_sa108(ending) if ending else None,
                "rules": load_rules(ending) if ending else None,
            }
        )
    return years


def accountant_review_items(ending_year: int) -> List[Dict[str, str]]:
    """Items the config marks for an accountant. Nothing here is applied."""
    items: List[Dict[str, str]] = []
    sa108 = load_sa108(ending_year) or {}
    for box in sa108.get("boxes", []):
        if not isinstance(box, dict) or not box.get("accountant_review"):
            continue
        items.append(
            {
                "title": f"SA108 box {box.get('box', '')}",
                "detail": str(box.get("note") or box.get("label") or ""),
                "manual": "",
            }
        )
    rules = load_rules(ending_year) or {}
    for tag in rules.get("defi", {}).get("tags", []):
        if not isinstance(tag, dict):
            continue
        needs_review = tag.get("accountant_review") or tag.get("effect") == "accountant_review"
        if not needs_review:
            continue
        items.append(
            {
                "title": str(tag.get("label") or tag.get("id") or "DeFi"),
                "detail": str(tag.get("note") or ""),
                "manual": str(tag.get("manual") or ""),
            }
        )
    return items


def confirmed_flags(node: object) -> List[bool]:
    """Every ``confirmed`` flag in a loaded file, for tests and the config page."""
    found: List[bool] = []
    if isinstance(node, dict):
        if "confirmed" in node:
            found.append(bool(node["confirmed"]))
        for value in node.values():
            found.extend(confirmed_flags(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(confirmed_flags(value))
    return found


def source_urls(node: object) -> List[str]:
    found: List[str] = []
    if isinstance(node, dict):
        url = node.get("url")
        if isinstance(url, str):
            found.append(url)
        for value in node.values():
            found.extend(source_urls(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(source_urls(value))
    return found


def sa108_publication_url() -> str:
    return _SA108_URL


def _ending_year(slug: str) -> int:
    parts = slug.split("-")
    if len(parts) != 2 or not parts[1].isdigit():
        return 0
    century = parts[0][:2] if len(parts[0]) >= 2 else "20"
    return int(century + parts[1])


def _load_optional(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeskError(f"Cannot read {path.name}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise DeskError(f"{path.name} is not valid YAML.") from exc
    if not isinstance(loaded, dict):
        raise DeskError(f"{path.name} must be a mapping.")
    return loaded
