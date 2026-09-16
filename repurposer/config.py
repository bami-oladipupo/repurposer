"""Configuration: config.yaml (committed, no secrets) plus .env (secrets, never committed).

Paths are resolved relative to the project root so the worker and the web app agree
regardless of the working directory they were launched from.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = Path(os.environ.get("REPURPOSER_CONFIG", ROOT / "config.yaml"))
DATA_DIR = Path(os.environ.get("REPURPOSER_DATA_DIR", ROOT / "data"))
MEDIA_DIR = Path(os.environ.get("REPURPOSER_MEDIA_DIR", ROOT / "media"))
LOG_DIR = Path(os.environ.get("REPURPOSER_LOG_DIR", ROOT / "logs"))
TOKEN_DIR = Path(os.environ.get("REPURPOSER_TOKEN_DIR", ROOT / "tokens"))
DB_PATH = Path(os.environ.get("REPURPOSER_DB", DATA_DIR / "repurposer.db"))

PLATFORMS = ("youtube", "instagram")
PREFIX = {"youtube": "yt", "instagram": "ig"}
PLATFORM_LABEL = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels"}


class ConfigError(RuntimeError):
    pass


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    _validate(cfg)
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    handle = (cfg.get("source") or {}).get("tiktok_handle") or ""
    if not handle:
        raise ConfigError("source.tiktok_handle is missing from config.yaml")
    if "YOUR_TIKTOK_HANDLE" in handle:
        raise ConfigError("source.tiktok_handle is still the placeholder; set it in config.yaml")
    for section in ("youtube", "instagram", "publish", "existing_content", "limits", "notify"):
        if section not in cfg:
            raise ConfigError(f"config.yaml is missing the '{section}' section")


def tiktok_profile_url(cfg: dict[str, Any]) -> str:
    handle = cfg["source"]["tiktok_handle"].strip()
    if handle.startswith("http"):
        return handle
    handle = handle.lstrip("@")
    return f"https://www.tiktok.com/@{handle}"


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def ensure_dirs() -> None:
    for d in (DATA_DIR, MEDIA_DIR, LOG_DIR, TOKEN_DIR):
        d.mkdir(parents=True, exist_ok=True)
