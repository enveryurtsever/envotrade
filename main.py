"""Envorai Trading Terminal — desktop entry point."""
from __future__ import annotations

import logging
import sys

import customtkinter as ctk

from database.db_manager import DBManager
from engine.bot_coordinator import BotCoordinator
from engine.stocks_coordinator import StocksCoordinator
from ui.main_window import MainWindow


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


def main() -> int:
    configure_logging()

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    db = DBManager()
    try:
        db.init_schema()
    except Exception as e:
        print(f"[WARN] DB init failed: {e}", file=sys.stderr)

    crypto = BotCoordinator(db)
    stocks = StocksCoordinator(db)
    app = MainWindow(
        db, crypto_coordinator=crypto, stocks_coordinator=stocks,
    )

    def _on_close():
        for c in (crypto, stocks):
            try:
                c.stop()
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            pass
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", _on_close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
