"""Run the Telegram bot (entry point for hosting panels).

    python run.py      (main.py / bot.py do exactly the same)

What it does
    1. binds the database,
    2. reads the credentials from — in this order — real environment variables,
       `config.env`, `app/embedded_config.py`, and finally the database
       (so a restored backup brings the token back by itself),
    3. connects the bot and brings every saved userbot session online,
       retrying forever if Telegram is unreachable.

There is no website any more. Userbot sessions are pasted through the bot itself
(`/session`) and live in the database, so they travel with every backup.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from app import config as cfg, health, runtime
from app.logger import log, setup_logging
from app.services import credentials, secrets_guard
from app.storage import db

from app.handler_loader import HANDLER_MODULES, load_handlers


async def secrets_watchdog() -> None:
    """Warn (log + Telegram) when secrets sit inside a committed config.env."""
    try:
        report = secrets_guard.log_report()
        if report.get("secrets"):
            await secrets_guard.notify_admins_once_per_day()
    except Exception as exc:
        log.debug("secrets watchdog skipped: %s", exc)


async def bot_worker() -> int:
    """Connect the bot and keep it connected."""
    from telethon import errors

    from bot import admin_error_notifier, on_startup

    credentials.load()
    if not credentials.is_configured():
        log.error("Bot credentials missing: %s", "; ".join(credentials.missing()))
        log.error("Fill API_ID / API_HASH / BOT_TOKEN / ADMIN_IDS in app/embedded_config.py, "
                  "config.env or the host's environment variables, then restart.")
        return 2

    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2")
    client.parse_mode = "html"
    runtime.set_client(client)

    # Handlers register themselves on import — they must be imported *after* the
    # client exists, otherwise the buttons would answer to nobody.
    load_handlers(client)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    started = False
    while True:
        try:
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                log.info("Signing in to Telegram…")
                await client.start(bot_token=cfg.BOT_TOKEN)
            if not started:
                await on_startup(client)
                started = True
                log.info("Bot is online ✅")
            await client.run_until_disconnected()
        except (errors.ApiIdInvalidError, errors.AccessTokenInvalidError,
                errors.AccessTokenExpiredError) as exc:
            # Wrong credentials never fix themselves — stop with a clear message.
            log.error("Telegram rejected the credentials (API_ID/API_HASH/BOT_TOKEN): %s", exc)
            return 3
        except Exception as exc:
            log.error("Telegram connection problem: %s — retrying in 15 seconds…", exc)
        await asyncio.sleep(15)


def initialize_database() -> bool:
    try:
        db.bind(cfg.DB_FILE)
        cfg.DATABASE_UNAVAILABLE = False
        return True
    except Exception:
        if not str(cfg.DB_FILE).startswith(('postgresql://', 'postgres://')):
            raise
        log.exception('PostgreSQL unavailable')
        return False


async def run() -> int:
    health.start()
    while not initialize_database():
        log.warning('PostgreSQL still unavailable; retrying in 10 seconds')
        await asyncio.sleep(10)
    imported = db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if imported:
        log.info("Imported the legacy bot_db.json: %s", imported)

    tasks = [asyncio.create_task(secrets_watchdog())]
    main_task = asyncio.create_task(bot_worker())
    tasks.append(main_task)
    # Several bots, one database: bring the extra ones up in their own processes
    # (see app/services/botrunner.py).
    try:
        from app.services import botrunner
        autostart = botrunner.autostart()
        tasks.append(asyncio.create_task(botrunner.supervise()))
        if autostart.get("started") or autostart.get("failed"):
            log.info("extra bots: %s", autostart)
    except Exception as exc:
        log.warning("extra bots could not autostart: %s", exc)

    code = await main_task
    for task in tasks:
        if task is not main_task:
            task.cancel()
    return int(code or 0)


def main() -> int:
    setup_logging()
    log.info("Starting store bot (run.py)…")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
        return 0


if __name__ == "__main__":
    sys.exit(main())
