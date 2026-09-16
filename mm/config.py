"""Tiny config helpers: load base.yaml and merge a strategy overlay on top."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path, base: str | Path | None = "configs/base.yaml") -> dict[str, Any]:
    """Load ``path``; if ``base`` is given, load it first and shallow-merge top-level keys.

    Nested dicts are merged one level deep (e.g. ``engine.latency_ms`` overrides).
    """
    cfg: dict[str, Any] = {}
    if base is not None and Path(base).exists():
        with open(base) as f:
            cfg = yaml.safe_load(f) or {}
    with open(path) as f:
        overlay = yaml.safe_load(f) or {}
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


def local_midnight_ns(date: str, utc_offset_hours: int) -> int:
    """Midnight of ``date`` (YYYY-MM-DD) at a fixed UTC offset, as int ns since epoch."""
    tz = timezone(timedelta(hours=utc_offset_hours))
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
    return int(dt.timestamp()) * 1_000_000_000
