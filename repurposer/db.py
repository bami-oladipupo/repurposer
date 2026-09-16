"""SQLite access shared by the worker and the web app.

WAL mode so the always-on web app and the 15 minute worker can read and write concurrently.
Every write goes through short transactions; nothing holds a lock across a network call.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config
from .timeutil import iso, utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)  # autocommit; explicit BEGIN below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so writers queue instead of failing mid-transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    done: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        # executescript commits any open transaction itself, so it runs outside tx().
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)", (sql_file.name, iso(utcnow())))
        done.append(sql_file.name)
    return done


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


# ---------- row helpers ----------

def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    return row_to_dict(conn.execute(sql, params).fetchone())


def get_video(conn: sqlite3.Connection, tiktok_id: str) -> dict[str, Any] | None:
    return one(conn, "SELECT * FROM videos WHERE tiktok_id = ?", (tiktok_id,))


def update_video(conn: sqlite3.Connection, tiktok_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE videos SET {cols} WHERE tiktok_id = ?", (*fields.values(), tiktok_id))


def insert_video(conn: sqlite3.Connection, **fields: Any) -> bool:
    """Insert if unseen. Returns True when a row was created. Never re-inserts."""
    fields.setdefault("first_seen", iso(utcnow()))
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT OR IGNORE INTO videos ({cols}) VALUES ({marks})", tuple(fields.values()))
    return cur.rowcount == 1


# ---------- settings ----------

def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, json.JSONDecodeError):
        return r["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# ---------- workflows ----------

def get_workflow(conn: sqlite3.Connection, platform: str) -> dict[str, Any] | None:
    wf = one(conn, "SELECT * FROM workflows WHERE platform = ?", (platform,))
    if wf is None:
        return None
    wf["hashtags"] = json.loads(wf["hashtags"] or "[]")
    wf["exclude_keywords"] = json.loads(wf["exclude_keywords"] or "[]")
    wf["extra"] = json.loads(wf["extra"] or "{}")
    return wf


def all_workflows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {p: wf for p in config.PLATFORMS if (wf := get_workflow(conn, p)) is not None}


def save_workflow(conn: sqlite3.Connection, platform: str, **fields: Any) -> None:
    for k in ("hashtags", "exclude_keywords", "extra"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = iso(utcnow())
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE workflows SET {cols} WHERE platform = ?", (*fields.values(), platform))


# ---------- slots ----------

def get_slots(conn: sqlite3.Connection, platform: str) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM slots WHERE platform = ? ORDER BY weekday, local_time", (platform,))


def replace_slots(conn: sqlite3.Connection, platform: str, slots: list[tuple[int, str]]) -> None:
    conn.execute("DELETE FROM slots WHERE platform = ?", (platform,))
    conn.executemany(
        "INSERT OR IGNORE INTO slots(platform, weekday, local_time) VALUES (?, ?, ?)",
        [(platform, wd, t) for wd, t in slots],
    )


# ---------- connections ----------

def get_connection(conn: sqlite3.Connection, platform: str) -> dict[str, Any] | None:
    return one(conn, "SELECT * FROM connections WHERE platform = ?", (platform,))


def upsert_connection(conn: sqlite3.Connection, platform: str, **fields: Any) -> None:
    fields["last_checked"] = iso(utcnow())
    existing = get_connection(conn, platform)
    if existing is None:
        fields["platform"] = platform
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        conn.execute(f"INSERT INTO connections ({cols}) VALUES ({marks})", tuple(fields.values()))
    else:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE connections SET {cols} WHERE platform = ?", (*fields.values(), platform))


# ---------- seeding ----------

def seed_from_config(conn: sqlite3.Connection, cfg: dict[str, Any]) -> bool:
    """Create the workflow rows, slots and settings the first time. Returns True if seeded."""
    if one(conn, "SELECT 1 AS x FROM workflows LIMIT 1") is not None:
        return False
    pub = cfg["publish"]
    ex = cfg["existing_content"]
    delay = pub.get("delay_minutes") or {}
    scope = "new_and_existing" if ex.get("enabled") else "new"
    mode = pub.get("mode", "schedule")
    auto = 0 if mode == "manual" else 1
    if mode == "manual":
        mode = "schedule"
    yt, ig = cfg["youtube"], cfg["instagram"]
    with tx(conn):
        conn.execute(
            """INSERT INTO workflows(platform, enabled, auto_publish, mode, content_scope, existing_start_from,
               existing_order, existing_include_before, delay_minutes, title_template, caption_template, hashtags,
               exclude_keywords, extra, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "youtube", int(bool(yt.get("enabled", True))), auto, mode, scope, ex.get("start_from"),
                ex.get("order", "newest_first"), ex.get("include_before"), int(delay.get("youtube", 0) or 0),
                yt.get("title_template", "{caption_first_line}"), yt.get("description_template", "{caption}\n\n{yt_hashtags}"),
                json.dumps(yt.get("hashtags") or []), json.dumps(ex.get("exclude_if_caption_contains") or []),
                json.dumps({"privacy": yt.get("privacy", "public"), "category_id": str(yt.get("category_id", "27")),
                            "made_for_kids": bool(yt.get("made_for_kids", False))}),
                iso(utcnow()),
            ),
        )
        conn.execute(
            """INSERT INTO workflows(platform, enabled, auto_publish, mode, content_scope, existing_start_from,
               existing_order, existing_include_before, delay_minutes, title_template, caption_template, hashtags,
               exclude_keywords, extra, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "instagram", int(bool(ig.get("enabled", False))), auto, mode, scope, ex.get("start_from"),
                ex.get("order", "newest_first"), ex.get("include_before"), int(delay.get("instagram", 0) or 0),
                None, ig.get("caption_template", "{caption}\n.\n.\n{ig_hashtags}"),
                json.dumps(ig.get("hashtags") or []), json.dumps(ex.get("exclude_if_caption_contains") or []),
                json.dumps({"share_to_feed": bool(ig.get("share_to_feed", True))}),
                iso(utcnow()),
            ),
        )
        for platform, times in (pub.get("default_slots") or {}).items():
            for wd in range(7):
                for t in times:
                    conn.execute("INSERT OR IGNORE INTO slots(platform, weekday, local_time) VALUES (?,?,?)", (platform, wd, t))
        set_setting(conn, "timezone", pub.get("timezone", "Europe/London"))
        set_setting(conn, "min_age_minutes", int(cfg["source"].get("min_age_minutes", 60)))
        set_setting(conn, "lookback_days", int(cfg["source"].get("lookback_days", 7)))
    return True


def timezone(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "timezone", "Europe/London")
