"""End-of-run summary via Telegram, email or stdout.

Silent runs with nothing to say send nothing. Runs with failures always send.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import requests

from . import config

log = logging.getLogger("repurposer.notify")
TELEGRAM_MAX = 3900


class NotifyError(RuntimeError):
    pass


def build_summary(run: dict[str, Any]) -> tuple[str, bool]:
    """Return (text, must_send). Plain text with simple sections; safe for Telegram and email."""
    lines: list[str] = []
    failures = 0
    published = run.get("published") or []
    if published:
        lines.append("Published")
        for plat, tiktok_id, link in published:
            lines.append(f"  {plat}: {tiktok_id} {link or ''}".rstrip())
    held = run.get("held") or []
    if held:
        lines.append("Held")
        for tiktok_id, reason in held:
            lines.append(f"  {tiktok_id}: {reason}")
    rolled = run.get("rolled") or []
    if rolled:
        lines.append("Slots moved (quota)")
        for plat, tiktok_id, why in rolled:
            lines.append(f"  {plat}: {tiktok_id} ({why})")
    deferred = [d for d in (run.get("deferred") or []) if d[2] != "quota"]  # quota moves are listed above
    if deferred:
        lines.append("Moved to a later slot")
        for plat, tiktok_id, why, new_slot in deferred:
            lines.append(f"  {plat}: {tiktok_id} ({why}) -> {new_slot or 'waiting for a free slot'}")
    failed = run.get("failed") or []
    if failed:
        failures += len(failed)
        lines.append("Failed")
        for plat, tiktok_id, err in failed:
            lines.append(f"  {plat}: {tiktok_id}\n    {err.strip()[:600]}")
    attention = run.get("needs_attention") or []
    if attention:
        lines.append("Needs manual attention (retries exhausted)")
        for plat, tiktok_id, err in attention:
            lines.append(f"  {plat}: {tiktok_id}\n    {(err or '').strip()[:300]}")
    alerts = run.get("alerts") or []
    if alerts:
        failures += len(alerts)
        lines.append("Alerts")
        for a in alerts:
            lines.append(f"  {a}")
    stage_errors = run.get("stage_errors") or []
    if stage_errors:
        failures += len(stage_errors)
        lines.append("Stage errors")
        for stage, err in stage_errors:
            lines.append(f"  {stage}: {err.strip()[:600]}")
    header = f"Repurposer run #{run.get('run_id', '?')} ({run.get('kind', 'cycle')}): " \
             f"{run.get('videos_seen', 0)} seen, {len(published)} published, {failures} problem(s)"
    text = header + ("\n\n" + "\n".join(lines) if lines else "")
    must_send = bool(published or failed or alerts or stage_errors or rolled or attention or deferred)
    return text, must_send


def send(cfg: dict[str, Any], text: str) -> None:
    method = (cfg.get("notify") or {}).get("method", "stdout")
    if method == "telegram":
        _telegram(text)
    elif method == "email":
        _email(text)
    else:
        print(text)


def _telegram(text: str) -> None:
    token, chat = config.env("TELEGRAM_BOT_TOKEN"), config.env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise NotifyError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from .env")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i + TELEGRAM_MAX] for i in range(0, len(text), TELEGRAM_MAX)] or [text]
    for chunk in chunks:
        resp = requests.post(url, json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True}, timeout=30)
        if resp.status_code != 200:
            raise NotifyError(f"Telegram returned {resp.status_code}: {resp.text[:300]}")


def _email(text: str) -> None:
    host, user, pwd, to = (config.env("SMTP_HOST"), config.env("SMTP_USER"), config.env("SMTP_PASSWORD"), config.env("SMTP_TO"))
    port = int(config.env("SMTP_PORT", "587") or 587)
    if not all((host, user, pwd, to)):
        raise NotifyError("SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_TO are required in .env")
    msg = EmailMessage()
    msg["Subject"] = text.splitlines()[0][:120]
    msg["From"], msg["To"] = user, to
    msg.set_content(text)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, pwd)
        smtp.send_message(msg)
