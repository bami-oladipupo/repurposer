"""Daily rotating logs under logs/, kept 30 days. One file per process (worker.log, web.log)."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from . import config


def setup(name: str = "worker", level: int = logging.INFO) -> Path:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.LOG_DIR / f"{name}.log"
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not any(isinstance(h, logging.handlers.TimedRotatingFileHandler) and getattr(h, "_rp_name", None) == name for h in root.handlers):
        fh = logging.handlers.TimedRotatingFileHandler(path, when="midnight", backupCount=30, encoding="utf-8", utc=True)
        fh.setFormatter(fmt)
        fh._rp_name = name  # type: ignore[attr-defined]
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    return path
