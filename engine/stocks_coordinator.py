"""StocksCoordinator — paralle to BotCoordinator but for US equities.

Same public surface (start, stop, is_running, get_symbols, get_live_price,
get_balance, latest_prices, log_queue) so the UI can swap between them
without conditional code.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import config
from agents.news_agent import NewsAgent
from agents.twitter_agent import TwitterAgent
from brain.stocks_analyst import StocksAILoop, StocksAnalyst
from engine.order_worker import OrderWorker
from engine.position_monitor import PositionMonitor
from engine.position_sync import PositionSync
from engine.stocks_scanner_worker import StocksScannerWorker
from engine.stocks_stream_worker import StocksStreamWorker

log = logging.getLogger(__name__)


_FILE_MIRROR_TAGS = {"ERROR", "ORDER", "STOCKS", "LEARNING"}


class _MirroredQueue(queue.Queue):
    """Mirror UI-bound log entries to python logging — same pattern as the
    crypto coordinator. Tags differ ("STOCKS" replaces "AI") so file logs
    can be greped by asset class."""

    def put(self, item, *args, **kwargs):  # type: ignore[override]
        try:
            if isinstance(item, dict):
                tag = str(item.get("tag", "INFO")).upper()
                text = str(item.get("text", ""))
                if tag == "ERROR":
                    log.error("[%s] %s", tag, text)
                elif tag in _FILE_MIRROR_TAGS:
                    log.info("[%s] %s", tag, text)
        except Exception:
            pass
        return super().put(item, *args, **kwargs)


class StocksCoordinator:
    asset_class = "stock"

    def __init__(self, db) -> None:
        self.db = db
        self.log_queue: queue.Queue = _MirroredQueue()
        self.decision_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()

        self._symbols: list[str] = []
        self._symbols_lock = threading.Lock()
        self.latest_prices: dict[str, dict] = {}

        self.analyst = StocksAnalyst(db, self.log_queue)

        self.scanner = StocksScannerWorker(
            log_queue=self.log_queue,
            stop_event=self.stop_event,
            on_symbols_update=self._on_symbols_update,
            db=db,
        )
        self.stream = StocksStreamWorker(db, self.log_queue, self.stop_event)
        self.stream.set_price_callback(self._on_live_price)
        self.order = OrderWorker(
            db, self.log_queue, self.decision_queue, self.stop_event,
            asset_class="stock",
        )
        self.position_monitor = PositionMonitor(
            db, self.log_queue, self.stop_event,
            learning_loop=None,  # learning loop is crypto-tuned for now
            asset_class="stock",
        )
        self.position_sync = PositionSync(
            db, self.log_queue, self.stop_event, asset_class="stock",
        )
        # Sentiment ingestion — equity-side VIPs + cashtag search.
        # Re-uses the (shared) AIAnalyst Haiku batch scorer so we don't
        # double-pay for the sentiment pipeline.
        from brain.ai_analyst import AIAnalyst
        self._sentiment_analyst = AIAnalyst(db, self.log_queue)
        self.twitter = TwitterAgent(
            db, self.log_queue, self.stop_event,
            sentiment_scorer=self._sentiment_analyst.score_sentiment,
            sentiment_batch_scorer=self._sentiment_analyst.score_sentiment_batch,
            symbols_getter=self.get_symbols,
            vip_handles=config.VIP_STOCKS_HANDLES,
        )
        self.news = NewsAgent(
            db, self.log_queue, self.stop_event,
            sentiment_scorer=self._sentiment_analyst.score_sentiment,
            sentiment_batch_scorer=self._sentiment_analyst.score_sentiment_batch,
            symbols_getter=self.get_symbols,
        )

        self.ai_loop = StocksAILoop(
            analyst=self.analyst,
            decision_queue=self.decision_queue,
            log_queue=self.log_queue,
            stop_event=self.stop_event,
            symbols_getter=self.get_symbols,
        )

        self._workers = [
            self.scanner, self.stream, self.order,
            self.position_monitor, self.position_sync,
            self.twitter, self.news,
            self.ai_loop,
        ]
        self._running = False

    # ----------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def get_symbols(self) -> list[str]:
        with self._symbols_lock:
            return list(self._symbols)

    def _on_symbols_update(self, symbols: list[str]) -> None:
        with self._symbols_lock:
            self._symbols = list(symbols)
        try:
            self.stream.update_symbols(symbols)
        except Exception as e:
            self.log_queue.put({"tag": "ERROR", "text": f"stocks stream resubscribe: {e}"})

    def _on_live_price(self, symbol: str, price: float) -> None:
        self.latest_prices[symbol] = {"price": price, "ts": time.time()}

    def get_live_price(self, symbol: str) -> float | None:
        d = self.latest_prices.get(symbol)
        return d["price"] if d else None

    def get_balance(self) -> float:
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            acc = client.get_account()
            return float(getattr(acc, "equity", 0) or 0)
        except Exception:
            return 0.0

    # ----------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self.stop_event.clear()
        self.log_queue.put({
            "tag": "INFO",
            "text": f"Starting stocks bot ({'PAPER' if config.ALPACA_PAPER else 'LIVE'})",
        })
        for w in self._workers:
            if not w.is_alive():
                w.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self.stop_event.set()
        for w in self._workers:
            try:
                w.join(timeout=5.0)
            except Exception:
                pass
        self._running = False
        self.log_queue.put({"tag": "INFO", "text": "All stocks workers stopped."})
