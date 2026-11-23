"""Master orchestrator — boots workers, shares queues, clean shutdown."""
from __future__ import annotations

import logging
import queue
import threading
import time

import config
from agents.news_agent import NewsAgent
from agents.twitter_agent import TwitterAgent
from brain.ai_analyst import AIAnalyst, AIAnalystLoop
from brain.auto_tuner import AutoTuner
from brain.learning_loop import LearningLoop
from engine.order_worker import OrderWorker
from engine.position_monitor import PositionMonitor
from engine.position_sync import PositionSync
from engine.scanner_worker import ScannerWorker
from engine.stream_worker import StreamWorker

log = logging.getLogger(__name__)


# UI log levels that should also reach the rotating file log (envotrade.log).
# INFO/SENTIMENT/SCANNER/STREAM are too noisy for the file; we mirror the
# tags that surface real outcomes or failures.
_FILE_MIRROR_TAGS = {"ERROR", "ORDER", "AI", "LEARNING"}


class _MirroredQueue(queue.Queue):
    """A Queue that also writes selected log entries to python logging.

    Workers publish dicts like {"tag": "ORDER", "text": "..."} for the UI
    console.  Previously those messages never reached the rotating file log,
    so silent failures (e.g. Alpaca rejecting a bracket order) went unnoticed
    for hours.  This subclass mirrors important tags to python logging.
    """

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


class BotCoordinator:
    asset_class = "crypto"

    def __init__(self, db) -> None:
        self.db = db
        self.log_queue: queue.Queue = _MirroredQueue()
        self.decision_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()

        self._symbols: list[str] = []
        self._symbols_lock = threading.Lock()

        # Live price tape — populated by StreamWorker's trade tick callback,
        # consumed by the UI for sub-second watchlist / trades refreshes.
        # Reads are atomic in CPython so no lock needed for single key access.
        self.latest_prices: dict[str, dict] = {}

        self.analyst = AIAnalyst(db, self.log_queue)
        self.learning_loop = LearningLoop(db, self.log_queue)
        self.auto_tuner = AutoTuner(db, self.log_queue)
        # Per-trade post-mortems need to feed back into learning storage

        # One-time conservative preset to recover from low-WR period.
        self._apply_conservative_preset_once()

        self.scanner = ScannerWorker(
            log_queue=self.log_queue,
            stop_event=self.stop_event,
            on_symbols_update=self._on_symbols_update,
            db=db,
            asset_class=self.asset_class,
        )
        self.stream = StreamWorker(db, self.log_queue, self.stop_event,
                                   asset_class=self.asset_class)
        # Wire the tick callback so every trade updates our in-memory price tape
        self.stream.set_price_callback(self._on_live_price)
        self.order = OrderWorker(db, self.log_queue, self.decision_queue, self.stop_event,
                                 asset_class=self.asset_class)
        self.position_monitor = PositionMonitor(
            db, self.log_queue, self.stop_event,
            learning_loop=self.learning_loop,   # real-time loss post-mortem
            asset_class=self.asset_class,
        )
        self.position_sync = PositionSync(db, self.log_queue, self.stop_event,
                                          asset_class=self.asset_class)
        self.twitter = TwitterAgent(
            db, self.log_queue, self.stop_event,
            sentiment_scorer=self.analyst.score_sentiment,
            sentiment_batch_scorer=self.analyst.score_sentiment_batch,
            symbols_getter=self.get_symbols,
            vip_handles=config.VIP_CRYPTO_HANDLES,
        )
        self.news = NewsAgent(
            db, self.log_queue, self.stop_event,
            sentiment_scorer=self.analyst.score_sentiment,
            sentiment_batch_scorer=self.analyst.score_sentiment_batch,
            symbols_getter=self.get_symbols,
        )
        self.ai_loop = AIAnalystLoop(
            analyst=self.analyst,
            decision_queue=self.decision_queue,
            log_queue=self.log_queue,
            stop_event=self.stop_event,
            symbols_getter=self.get_symbols,
            learning_loop=self.learning_loop,
            auto_tuner=self.auto_tuner,
        )

        self._workers = [self.scanner, self.stream, self.order,
                         self.position_monitor, self.position_sync,
                         self.twitter, self.news, self.ai_loop]
        self._running = False

    # =================================================================
    @property
    def is_running(self) -> bool:
        return self._running

    def _apply_conservative_preset_once(self) -> None:
        """Idempotent presets per version. Each new version applies once.

        v1 (deprecated): static % SL/TP + quick-profit + sentiment trigger.
        v2 (current): math-decides + LLM news-veto. Strategy:
          • Decision is math: RSI + 15m EMA50 trend filter + ATR sizing.
          • SL = 1.5×ATR(14), TP = 3×ATR(14) → fixed 2:1 R:R, volatility-adapted.
          • Quick-profit DISABLED (kills R:R; can't be profitable without
            >70% WR on mean-reversion).
          • LLM only fires on bearish news bursts (>=3 items with score<=-0.5)
            as a catastrophic-event veto. Most cycles cost zero LLM tokens.
          • RSI thresholds: 30/70 (back to standard from 35/65 — combined
            with trend filter we don't need ultra-loose RSI).
        """
        try:
            current = int(self.db.get_setting("preset_version", 0) or 0)
        except Exception:
            current = 0

        if current < 2:
            preset_v2 = {
                # Math params
                "rsi_buy_threshold": 30.0,
                "rsi_sell_threshold": 70.0,
                "atr_period": 14.0,
                "atr_sl_mult": 1.5,
                "atr_tp_mult": 3.0,
                "trend_ema_15m_period": 50.0,
                # Disable quick-profit (R:R killer)
                "quick_profit_pct": 0.0,
                # LLM veto thresholds
                "news_veto_sentiment_threshold": -0.5,
                "news_veto_min_count": 3.0,
                # Fallback static SL/TP — only used if ATR is unavailable
                "stop_loss_percent": 0.010,
                "take_profit_percent": 0.020,
            }
            try:
                for key, val in preset_v2.items():
                    self.db.update_setting(key, val, "conservative preset v2")
                self.db.update_setting(
                    "preset_version", 2, "applied conservative preset v2",
                )
                self.log_queue.put({
                    "tag": "LEARNING",
                    "text": (
                        "applied preset v2 — math-decides + LLM news-veto, "
                        "SL=1.5×ATR, TP=3×ATR (2:1 R:R), 15m-EMA50 trend filter, "
                        "quick-profit OFF"
                    ),
                })
            except Exception as e:
                self.log_queue.put({
                    "tag": "ERROR",
                    "text": f"preset v2 application failed: {e}",
                })

    def get_symbols(self) -> list[str]:
        with self._symbols_lock:
            return list(self._symbols)

    def _on_symbols_update(self, symbols: list[str]) -> None:
        with self._symbols_lock:
            self._symbols = list(symbols)
        try:
            self.stream.update_symbols(symbols)
        except Exception as e:
            self.log_queue.put({"tag": "ERROR", "text": f"stream resubscribe: {e}"})

    def _on_live_price(self, symbol: str, price: float) -> None:
        """Called from StreamWorker's asyncio loop on every trade tick."""
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

    # =================================================================
    def start(self) -> None:
        if self._running:
            return
        self.stop_event.clear()
        self.log_queue.put({"tag": "INFO",
                            "text": f"Starting bot ({'PAPER' if config.ALPACA_PAPER else 'LIVE'})"})
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
        self.log_queue.put({"tag": "INFO", "text": "All workers stopped."})
