# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.
# pylint: disable=protected-access
"""Read-only view of the calculator settings and the wallet register."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..config import Config, config
from ..constants import BITTYTAX_PATH
from .errors import DeskError


def config_snapshot(wallets: Optional[Path]) -> Dict[str, Any]:
    """Values the desk can show. Secrets from the calculator settings are masked."""
    conf_path = Path(BITTYTAX_PATH) / Config.BITTYTAX_CONFIG
    shown: List[Dict[str, str]] = []
    for name in Config.DEFAULT_CONFIG:
        shown.append({"name": name, "value": _plain(config.config.get(name))})
    secrets = []
    for name in Config.OPTIONAL_CONFIG:
        if name in config.config and config.config[name]:
            secrets.append({"name": name, "value": Config._mask_data(str(config.config[name]))})
    return {
        "calculator_config": str(conf_path),
        "calculator_present": conf_path.is_file(),
        "local_currency": str(config.ccy),
        "local_timezone": str(config.local_timezone),
        "settings": shown,
        "secrets": secrets,
        "wallets_path": "" if wallets is None else str(wallets),
        "wallets_present": wallets is not None,
        "wallet_labels": _labels(wallets),
        "match": _section(wallets, "match"),
        "match_overrides": _section(wallets, "match_overrides"),
        "env_wallets": os.environ.get("UK_CRYPTO_TAX_DESK_WALLETS", ""),
    }


def _labels(path: Optional[Path]) -> List[str]:
    loaded = _load(path)
    labels = []
    for key in ("own", "payees"):
        rows = loaded.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("label"):
                labels.append(f"{key}: {row['label']}")
    return labels


def _section(path: Optional[Path], key: str) -> str:
    loaded = _load(path)
    if key not in loaded:
        return ""
    text = yaml.safe_dump({key: loaded[key]}, sort_keys=False)
    if len(text) > 20000:
        return text[:20000] + "\n…\n"
    return text


def _load(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeskError(f"Cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise DeskError(f"{path.name} is not valid YAML.") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise DeskError(f"{path.name} must be a mapping.")
    return loaded


def _plain(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json_compact(value)
    return str(value)


def json_compact(value: object) -> str:
    return json.dumps(value, sort_keys=True)
