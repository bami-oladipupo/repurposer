"""overrides.yaml: per-video hold / skip / publish plus caption replacements, without touching code.

Read fresh on every worker run so edits take effect on the next cycle. A malformed file is an
error surfaced in the run summary, never silently ignored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import config

VALID_ACTIONS = {"hold", "skip", "publish"}


class OverridesError(RuntimeError):
    pass


def load_overrides(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rel = cfg.get("overrides_file") or "overrides.yaml"
    path = Path(rel)
    if not path.is_absolute():
        path = config.ROOT / path
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OverridesError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise OverridesError(f"{path.name} must be a mapping keyed by tiktok_id")
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        entry = dict(value or {})
        action = entry.get("action")
        if action is not None and action not in VALID_ACTIONS:
            raise OverridesError(f"{path.name}: {key} has unknown action '{action}' (use hold, skip or publish)")
        out[str(key)] = entry
    return out


def override_for(overrides: dict[str, dict[str, Any]], tiktok_id: str) -> dict[str, Any]:
    return overrides.get(str(tiktok_id), {})
