"""Alpaca live data stream → market_data table."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import config


class StreamWorker(threading.Thread):
    """Owns the asyncio event loop for the Alpaca CryptoDataStream client.

    Subscribes to BOTH:
      • per-minute bars  → written to `market_data` for indicators/charts
      • tick-by-tick trades → fired through `price_callback(symbol, price)` so
        the UI can refresh prices sub-second without polling the DB.
    """

    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
        asset_class: str = "crypto",
    ) -> None:
        super().__init__(name=f"StreamWorker[{asset_class}]", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.asset_class = asset_class
        self._symbols: list[str] = []
        self._symbols_lock = threading.Lock()
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._price_callback = None  # set by BotCoordinator

    def set_price_callback(self, fn) -> None:
        """fn(symbol: str, price: float) — called from the asyncio loop on
        every trade tick. Must be fast and non-blocking."""
        self._price_callback = fn

    def _log(self, text: str, tag: str = "STREAM") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def update_symbols(self, symbols: list[str]) -> None:
        with self._symbols_lock:
            self._symbols = list(symbols)
        if self._loop and self._loop.is_running() and self._stream is not None:
            asyncio.run_coroutine_threadsafe(self._resubscribe(symbols), self._loop)

    async def _resubscribe(self, symbols: list[str]) -> None:
        # bars
        try:
            self._stream.unsubscribe_bars(*self._stream._handlers["bars"].keys())  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._stream.subscribe_bars(self._on_bar, *symbols)
        except Exception as e:
            self._log(f"subscribe bars failed: {e}", tag="ERROR")

        # trades (for live tick prices)
        try:
            self._stream.unsubscribe_trades(*self._stream._handlers["trades"].keys())  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._stream.subscribe_trades(self._on_trade, *symbols)
            self._log(f"subscribed bars+trades: {', '.join(symbols)}")
        except Exception as e:
            self._log(f"subscribe trades failed: {e}", tag="ERROR")

    async def _on_bar(self, bar) -> None:
        try:
            ts = getattr(bar, "timestamp", None) or datetime.now(timezone.utc)
            price = float(getattr(bar, "close", 0) or 0)
            volume = float(getattr(bar, "volume", 0) or 0)
            symbol = getattr(bar, "symbol", "")
            if symbol and price:
                self.db.insert_market_tick(
                    symbol, price, volume, ts, asset_class=self.asset_class,
                )
        except Exception as e:
            self._log(f"bar handler error: {e}", tag="ERROR")

    async def _on_trade(self, trade) -> None:
        """High-frequency tick callback — keeps live UI prices fresh.

        Must NOT touch the DB or anything blocking; Alpaca dispatches all
        symbols through this single coroutine.
        """
        try:
            price = float(getattr(trade, "price", 0) or 0)
            symbol = getattr(trade, "symbol", "")
            if not (symbol and price):
                return
            if self._price_callback is not None:
                self._price_callback(symbol, price)
        except Exception:
            # Silently swallow — millions of trades; one log per error storm
            pass

    def run(self) -> None:
        if not config.ALPACA_KEY or not config.ALPACA_SECRET:
            self._log("Alpaca creds missing — stream disabled", tag="ERROR")
            return

        try:
            from alpaca.data.live import CryptoDataStream
        except Exception as e:
            self._log(f"alpaca-py not installed: {e}", tag="ERROR")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stream = CryptoDataStream(config.ALPACA_KEY, config.ALPACA_SECRET)

        with self._symbols_lock:
            initial = list(self._symbols)
        if initial:
            try:
                self._stream.subscribe_bars(self._on_bar, *initial)
                self._stream.subscribe_trades(self._on_trade, *initial)
                self._log(f"subscribed bars+trades: {', '.join(initial)}")
            except Exception as e:
                self._log(f"initial subscribe failed: {e}", tag="ERROR")

        # Run stream and stop watcher in parallel
        async def _runner():
            stream_task = asyncio.create_task(self._stream._run_forever())  # type: ignore[attr-defined]
            stop_task = asyncio.create_task(self._wait_stop())
            done, pending = await asyncio.wait(
                {stream_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

        try:
            self._loop.run_until_complete(_runner())
        except Exception as e:
            self._log(f"stream loop ended: {e}", tag="ERROR")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._log("stream worker stopped")

    async def _wait_stop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(0.5)
        try:
            await self._stream.stop_ws()  # type: ignore[union-attr]
        except Exception:
            pass
