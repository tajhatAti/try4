"""Entry point for the Telegram store bot (v2).

Run it with:  python bot.py
Configuration lives in `config.env` (see config.env.example) — never in the code.
"""
from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from app import config as cfg, runtime
from app.logger import log, setup_logging
from app.services import scheduler, secrets_guard
from app.storage import db


async def _tell_owner_database_empty(client: TelegramClient) -> None:
    """After a host reset the database is empty: tell the owner how to get everything back."""
    owner = (cfg.ADMIN_IDS or [0])[0]
    if not owner:
        return
    await asyncio.sleep(5)
    try:
        await client.send_message(
            owner,
            "🔄 <b>বট নতুন করে চালু হয়েছে — ডেটাবেস এখন ফাঁকা।</b>\n\n"
            "আগের সব ডেটা ফিরিয়ে আনতে: আপনার সর্বশেষ ব্যাকআপ ফাইল "
            "(<code>.bfixf.gz</code>) এই চ্যাটে পাঠান, তারপর <b>✅ রিস্টোর করো</b> চাপুন।\n"
            "ফাইল হাতে না থাকলে চ্যাটে ওপরের দিকে বটের পাঠানো সর্বশেষ ব্যাকআপটা খুঁজে নিন।",
            parse_mode="html")
    except Exception as exc:
        log.debug("empty-database notice not delivered: %s", exc)


async def on_startup(client: TelegramClient) -> None:
    me = await client.get_me()
    runtime.bot_username = me.username or ""
    runtime.bot_id = me.id
    # Remember it in the database too: the public website can then build
    # t.me deep links even while the bot itself is offline.
    if runtime.bot_username and os.getenv("BOT_EXTRA") != "1":
        # The public links must keep pointing at the *main* bot. An extra bot
        # records its own name in the `bots` table instead.
        db.set_meta("bot_username", runtime.bot_username)
    try:
        if os.getenv("BOT_EXTRA") == "1":
            from app.services import botrunner
            row = db.bots_store.bot_by_token(cfg.BOT_TOKEN)
            if row is None:
                bot_id = botrunner.add(cfg.BOT_TOKEN, label=os.getenv("BOT_LABEL") or "",
                                       admin_id=(cfg.ADMIN_IDS or [0])[0],
                                       api_id=cfg.API_ID, api_hash=cfg.API_HASH,
                                       origin="env")["id"]
                row = db.bots_store.bot(bot_id)
            botrunner.learn_username(row, runtime.bot_username)
    except Exception as exc:
        log.debug("extra bot registration skipped: %s", exc)
    log.info("Bot online as @%s (id=%s)", runtime.bot_username, me.id)

    stats = db.stats()
    log.info("Database: %s users · %s files · %s stores · %s grants",
             stats["users"], stats["files"], stats["stores"], stats["grants"])

    if not (stats["users"] or stats["files"]) and os.getenv("BOT_EXTRA") != "1":
        runtime.spawn(_tell_owner_database_empty(client))

    from app.services.command_catalog import announce_and_install
    runtime.spawn(announce_and_install(client))
    await seed_and_reconnect_sessions()
    if os.getenv("BOT_EXTRA") != "1":
        scheduler.start_all()
    else:
        from app.services import video_copy
        runtime.spawn(video_copy.monitor())  # shared lease, no duplicate broadcasts
    runtime.spawn(prepare_media_cache())


async def prepare_media_cache() -> None:
    """Drop unreadable old mirrors and make sure the cache channel exists."""
    try:
        from app.services import media_cache
        cleared = media_cache.clear_private_mirrors()
        if cleared:
            log.info("Forgot %s old copy/copies that lived in a private chat — they are "
                     "re-created in the cache channel", cleared)
        client = next(iter(runtime.user_clients.values()), None)
        if client is not None:
            await media_cache.storage_peer(client)
            await media_cache.poke_storage_channel(client)
    except Exception as exc:
        log.warning("media cache setup skipped: %s", exc)


async def seed_and_reconnect_sessions() -> None:
    """Bring every stored userbot session online (and seed the pre-filled one)."""
    from app.runtime import register_client

    if cfg.STRING_SESSION and cfg.ADMIN_IDS and db.session(cfg.ADMIN_IDS[0]) is None:
        db.save_session(cfg.ADMIN_IDS[0], cfg.STRING_SESSION)
        log.info("Seeded the pre-filled STRING_SESSION for admin %s", cfg.ADMIN_IDS[0])

    for row in db.all_sessions():
        admin_id = row["admin_id"]
        if admin_id in runtime.user_clients:
            continue                      # already online (e.g. after a restore)
        try:
            client = TelegramClient(StringSession(row["session_str"]), cfg.API_ID, cfg.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("Stored session for admin %s is no longer authorised", admin_id)
                continue
            me = await client.get_me()
            register_client(admin_id, client, me.id, me.first_name or "")
            db.save_session(admin_id, row["session_str"], me.id, me.first_name or "")
            log.info("Session online for admin %s (as %s, id=%s)", admin_id, me.first_name, me.id)
        except Exception as exc:
            log.error("Could not restore session for admin %s: %s", admin_id, exc)

    runtime.log_session_summary()


async def admin_error_notifier(text: str) -> None:
    client = runtime.get_client()
    if client is None:
        return
    from app.services.error_reports import redact
    from app.utils import esc
    for admin_id in cfg.ADMIN_IDS[:1]:  # technical diagnostics belong to the owner
        try:
            await asyncio.wait_for(client.send_message(
                admin_id, f"⚠️ <b>বটের সতর্কতা</b>\n{esc(redact(text).splitlines()[0][:240])}\nবিস্তারিত ব্যক্তিগত error inbox-এ সেভ হয়েছে।"), timeout=10)
        except Exception:
            pass


def main() -> int:
    setup_logging()

    if os.getenv("BOT_EXTRA") != "1":
        # The main bot is exactly what run.py does (database, credentials,
        # reconnect loop, extra bots) — one entry point, no drifting copies.
        from run import main as run_main
        return run_main()

    # Started by `app/services/botrunner.py` as an *extra* bot: this process only
    # polls its own token.
    log.info("Extra bot mode (%s): serving Telegram only", os.getenv("BOT_LABEL") or "bot")

    problems = cfg.validate()
    if problems:
        log.error("Configuration problem(s): %s", "; ".join(problems))
        return 2

    db.bind(cfg.DB_FILE)
    secrets_guard.log_report()

    def handle_loop_exception(loop, context):
        log.error("Unhandled loop error: %s (%s)", context.get("message"), context.get("exception"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_loop_exception)

    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2", loop=loop)
    client.parse_mode = "html"
    runtime.set_client(client)

    from app.handler_loader import load_handlers
    load_handlers(client)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    log.info("Connecting to Telegram…")
    try:
        client.start(bot_token=cfg.BOT_TOKEN)
    except Exception as exc:
        log.error("Could not connect to Telegram: %s", exc)
        return 3

    loop.run_until_complete(on_startup(client))
    try:
        client.run_until_disconnected()
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
    finally:
        try:
            loop.run_until_complete(disconnect_all(client))
        except Exception:
            pass
    return 0


async def disconnect_all(client: TelegramClient) -> None:
    for admin_id, userbot in list(runtime.user_clients.items()):
        try:
            await userbot.disconnect()
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass
    log.info("Bye 👋")


if __name__ == "__main__":
    sys.exit(main())
