"""Entry point for hosting panels (the first file panels look for).

    python main.py

Starts the Telegram bot (see ``run.py``). Credentials are NOT written here — they
come from ``app/embedded_config.py``, ``config.env`` or the host's environment
variables. Userbot sessions are pasted through the bot (``/session``), stored in
the database and restored together with every backup.
"""
from __future__ import annotations

from run import main

if __name__ == "__main__":
    raise SystemExit(main())
