"""Bot credentials: where they come from and who the admins are.

Precedence: real environment variables → `config.env` → `app/embedded_config.py`
(all three end up in `cfg`), then — only when those are empty, e.g. on a fresh
host after restoring a backup — the values saved in the database.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

from app import config as cfg
from app.logger import log
from app.storage import db

K_TOKEN, K_API_ID, K_API_HASH, K_ADMINS = (
    "cred:BOT_TOKEN", "cred:API_ID", "cred:API_HASH", "cred:ADMIN_IDS")

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# Set the moment usable credentials exist; `run.bot_worker` waits on it.
ready: asyncio.Event | None = None
# Live state the setup page polls: waiting | connecting | online | error
state: dict = {"state": "waiting", "detail": "", "username": ""}
_BOOT_TOKEN: str = ""        # token the running client was started with


def _event() -> asyncio.Event:
    global ready
    if ready is None:
        ready = asyncio.Event()
    return ready


def set_state(name: str, detail: str = "", username: str | None = None) -> None:
    state["state"] = name
    state["detail"] = detail
    if username is not None:
        state["username"] = username


def parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in re.split(r"[,\s;]+", raw or ""):
        if chunk.strip().lstrip("-").isdigit():
            value = int(chunk)
            if value > 0 and value not in ids:
                ids.append(value)
    return ids


def load() -> bool:
    """Copy the saved values into `cfg`. Returns True when everything is present."""
    token = cfg.BOT_TOKEN or db.get_meta(K_TOKEN, "")
    api_hash = cfg.API_HASH or db.get_meta(K_API_HASH, "")
    try:
        api_id = int(cfg.API_ID or db.get_meta(K_API_ID, "") or 0)
    except ValueError:
        api_id = 0
    admins = parse_admin_ids(db.get_meta(K_ADMINS, "")) or list(cfg.ADMIN_IDS)
    cfg.BOT_TOKEN, cfg.API_HASH, cfg.API_ID = token, api_hash, api_id
    cfg.ADMIN_IDS[:] = admins            # in place: modules hold a reference to this list
    ok = is_configured()
    if ok:
        _event().set()
    return ok


def poke() -> None:
    """Wake `run.bot_worker` after credentials appeared (used by the backup restore).

    `bot_worker` clears the event before waiting, so a wake-up that lands in that
    tiny window would be lost and the bot would sit idle until a restart. Setting
    the flag twice (now, and a moment later) makes that impossible.
    """
    event = _event()
    event.set()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(1.5, event.set)


def is_configured() -> bool:
    return bool(cfg.BOT_TOKEN and cfg.API_HASH and cfg.API_ID and cfg.ADMIN_IDS
                and TOKEN_RE.match(cfg.BOT_TOKEN))


def admins() -> list[int]:
    """Every Telegram id that may use the panel and the bot's admin commands."""
    saved = parse_admin_ids(db.get_meta(K_ADMINS, ""))
    return saved or list(cfg.ADMIN_IDS)


def admin_details() -> list[dict]:
    """The admin list with names, so the panel shows people, not numbers."""
    out = []
    known = {row["user_id"]: row for row in db.all_users_brief()}
    for index, user_id in enumerate(admins()):
        row = known.get(user_id) or {}
        out.append({"user_id": user_id, "name": row.get("name") or "",
                    "username": row.get("username") or "",
                    "owner": index == 0,
                    "has_session": db.session(user_id) is not None,
                    "seen": row.get("last_seen") or 0})
    return out


def save_admins(ids: list[int]) -> list[int]:
    """Store the whole admin list (the first one is the *owner*)."""
    clean: list[int] = []
    for value in ids:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in clean:
            clean.append(user_id)
    if not clean:
        return admins()
    db.set_meta(K_ADMINS, ",".join(str(user_id) for user_id in clean))
    cfg.ADMIN_IDS[:] = clean            # in place — modules hold this very list
    return clean


def add_admin(user_id: int) -> bool:
    ids = admins()
    if int(user_id) in ids:
        return False
    save_admins(ids + [int(user_id)])
    return True


def remove_admin(user_id: int) -> bool:
    """Remove somebody — the very first admin (the owner) can never be removed."""
    ids = admins()
    if len(ids) <= 1 or int(user_id) not in ids or ids[0] == int(user_id):
        return False
    save_admins([value for value in ids if value != int(user_id)])
    return True


def missing() -> list[str]:
    """Human readable list of what is still absent (for the startup log)."""
    out = []
    if not cfg.API_ID:
        out.append("API_ID")
    if not cfg.API_HASH or not HASH_RE.match(cfg.API_HASH):
        out.append("API_HASH (৩২ অক্ষরের হেক্স)")
    if not cfg.BOT_TOKEN or not TOKEN_RE.match(cfg.BOT_TOKEN):
        out.append("BOT_TOKEN")
    if not cfg.ADMIN_IDS:
        out.append("ADMIN_IDS")
    return out


def mark_started(token: str) -> None:
    global _BOOT_TOKEN
    _BOOT_TOKEN = token


def schedule_restart(delay: float = 1.5) -> None:
    """A different bot / API key needs a fresh login: forget the old session and restart."""
    async def _go() -> None:
        await asyncio.sleep(delay)
        for suffix in ("", "-journal"):
            try:
                os.remove(cfg.SESSION_STATE_FILE + suffix)
            except OSError:
                pass
        log.info("Restarting to apply the new bot credentials…")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass
