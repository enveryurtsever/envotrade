"""Alpaca live data stream for US equities → market_data table.

Mirrors engine.stream_worker but uses alpaca.data.live.StockDataStream so
we get free-tier IEX bars + trades. Closely follows the crypto variant —
same callbacks, same price-tape contract — so BotCoordinator + UI can
treat both streams interchangeably.

NOTE on data feed: the free Alpaca data plan returns IEX-only ticks for
stocks, which is a small slice of consolidated volume. VWAP computed from
these bars will diverge from Bloomberg's; for an MVP that's acceptable.
Upgrade to SIP/full-feed to tighten the VWAP signal.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import config


class StocksStreamWorker(threading.Thread):
    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="StreamWorker[stock]", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.asset_class = "stock"
        self._symbols: list[str] = []
        self._symbols_lock = threading.Lock()
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._price_callback = None

    def set_price_callback(self, fn) -> None:
        self._price_callback = fn

    def _log(self, text: str, tag: str = "STREAM") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def update_symbols(self, symbols: list[str]) -> None:
        with self._symbols_lock:
            self._symbols = list(symbols)
        if self._loop and self._loop.is_running() and self._stream is not None:
            asyncio.run_coroutine_threadsafe(self._resubscribe(symbols), self._loop)

    async def _resubscribe(self, symbols: list[str]) -> None:
        try:
            self._stream.unsubscribe_bars(*self._stream._handlers["bars"].keys())  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._stream.subscribe_bars(self._on_bar, *symbols)
        except Exception as e:
            self._log(f"subscribe bars failed: {e}", tag="ERROR")

        try:
            self._stream.unsubscribe_trades(*self._stream._handlers["trades"].keys())  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._stream.subscribe_trades(self._on_trade, *symbols)
            self._log(f"stocks subscribed bars+trades: {', '.join(symbols)}")
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
        try:
            price = float(getattr(trade, "price", 0) or 0)
            symbol = getattr(trade, "symbol", "")
            if not (symbol and price):
                return
            if self._price_callback is not None:
                self._price_callback(symbol, price)
        except Exception:
            pass

    def run(self) -> None:
        if not config.ALPACA_KEY or not config.ALPACA_SECRET:
            self._log("Alpaca creds missing — stocks stream disabled", tag="ERROR")
            return

        try:
            from alpaca.data.live import StockDataStream
        except Exception as e:
            self._log(f"alpaca-py StockDataStream import failed: {e}", tag="ERROR")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # `feed=DataFeed.IEX` is the free tier (the constructor casts to
        # enum, so passing the raw string blows up with
        # "'str' object has no attribute 'value'" on alpaca-py >=0.30).
        # Users with SIP entitlement can swap this to DataFeed.SIP.
        try:
            from alpaca.data.enums import DataFeed
            self._stream = StockDataStream(
                config.ALPACA_KEY, config.ALPACA_SECRET, feed=DataFeed.IEX,
            )
        except Exception:
            self._stream = StockDataStream(
                config.ALPACA_KEY, config.ALPACA_SECRET,
            )

        with self._symbols_lock:
            initial = list(self._symbols)
        if initial:
            try:
                self._stream.subscribe_bars(self._on_bar, *initial)
                self._stream.subscribe_trades(self._on_trade, *initial)
                self._log(f"stocks subscribed bars+trades: {', '.join(initial)}")
            except Exception as e:
                self._log(f"initial subscribe failed: {e}", tag="ERROR")

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
            self._log(f"stocks stream loop ended: {e}", tag="ERROR")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._log("stocks stream worker stopped")

    async def _wait_stop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(0.5)
        try:
            await self._stream.stop_ws()  # type: ignore[union-attr]
        except Exception:
            pass
