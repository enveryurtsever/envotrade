"""Stocks decision engine — VWAP-anchored entries on a confirmed uptrend.

Per cycle (1m):
  1. is_market_open() — outside RTH → skip
  2. check_fomc() — meeting day / eve → HOLD all
  3. check_index_health(SPY, QQQ) — bear tape → veto BUYs
  4. for each symbol:
       earnings veto → skip
       15m trend != up → skip
       1m VWAP breakout (with volume confirmation) → BUY
         stop = entry − STOCKS_ATR_STOP_MULT × ATR(1m)
         tp   = entry + STOCKS_ATR_TP_MULT  × ATR(1m)

Existing positions get SELL signals when 15m trend flips down (long-only).
"""
from __future__ import annotations

import json
import logging
import threading
import time

import anthropic

import config
from brain import earnings_veto, indicators, stocks_strategy

log = logging.getLogger(__name__)


class StocksAnalyst:
    asset_class = "stock"

    def __init__(self, db, log_queue) -> None:
        self.db = db
        self.log_queue = log_queue
        self.client = (
            anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY else None
        )
        self._bars_log_ts: dict[str, float] = {}

    def _log(self, text: str, tag: str = "STOCKS") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    # ------------------------------------------------------------ data
    def _bars(self, symbol: str, n: int = 1500):
        rows = self.db.get_recent_bars(symbol, n=n, asset_class="stock")
        return indicators.to_dataframe(rows)

    # ------------------------------------------------------------ entry
    def decide(
        self, symbol: str, *,
        index_healthy: bool,
        held_symbols: set[str] | None,
    ) -> dict | None:
        df = self._bars(symbol, n=1500)
        if len(df) < 25:
            now = time.time()
            last = self._bars_log_ts.get(symbol, 0.0)
            if now - last > 1800:
                self._bars_log_ts[symbol] = now
                self._log(f"{symbol}: warming up ({len(df)}/25 bars)")
            return None

        snap = indicators.latest_indicator_snapshot(
            df, atr_period=14, trend_ema=config.STOCKS_15M_EMA_LEN,
        )
        close = snap.get("close")
        atr_v = snap.get("atr") or 0.0
        trend = snap.get("trend") or {}
        is_held = bool(held_symbols and symbol in held_symbols)

        # ---- exit logic for held longs (cheap, doesn't need index/earnings)
        if is_held:
            if trend.get("known") and trend.get("direction") == "down":
                self._record_signal(symbol, "SELL", "trend_flipped_down_15m")
                return self._verdict(symbol, "SELL", snap, reason="trend_flipped_down_15m")
            return None

        # ---- entry pipeline ----------------------------------------
        if not index_healthy:
            return None
        if not (trend.get("known") and trend.get("direction") == "up"):
            return None
        if earnings_veto.check_earnings(symbol):
            self._log(f"{symbol}: earnings within 1d — skip")
            return None
        if atr_v <= 0:
            return None
        if not stocks_strategy.vwap_breakout_trigger(df):
            return None

        reason = (
            f"vwap_breakout + 15m_uptrend (ema={trend.get('ema'):.2f}) "
            f"close={close:.2f} atr={atr_v:.3f}"
        )
        self._record_signal(symbol, "BUY", reason)
        return self._verdict(symbol, "BUY", snap, reason=reason)

    # ------------------------------------------------------------
    def _verdict(self, symbol: str, decision: str, snap: dict, *, reason: str) -> dict:
        return {
            "symbol": symbol,
            "decision": decision,
            "confidence": 80.0,
            "reason": reason,
            "snapshot": snap,
            "atr_sl_mult": config.STOCKS_ATR_STOP_MULT,
            "atr_tp_mult": config.STOCKS_ATR_TP_MULT,
        }

    def _record_signal(self, symbol: str, decision: str, reason: str) -> None:
        try:
            self.db.insert_signal(
                symbol, 0.0, decision, 80.0,
                json.dumps({"reason": reason})[:2000],
                asset_class="stock",
            )
        except Exception as e:
            self._log(f"signal insert failed: {e}", tag="ERROR")


# ====================================================================
class StocksAILoop(threading.Thread):
    """Drives StocksAnalyst.decide once a minute during market hours.

    Holds the index-health gate at the cycle level so per-symbol decide()
    doesn't reload SPY/QQQ 10 times per minute.
    """

    def __init__(
        self,
        analyst: StocksAnalyst,
        decision_queue,
        log_queue,
        stop_event: threading.Event,
        symbols_getter,
        cycle_interval: int = 60,
    ) -> None:
        super().__init__(name="StocksAILoop", daemon=True)
        self.analyst = analyst
        self.decision_queue = decision_queue
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.symbols_getter = symbols_getter
        self.cycle_interval = cycle_interval
        self._last_market_closed_log = 0.0
        self._last_index_log = 0.0
        self._fomc_warned_today = False
        # Track the trading date for which we already flattened; clears at
        # the next market open so the next session can flatten again.
        self._eod_flattened_date: str | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                self.log_queue.put({"tag": "ERROR", "text": f"stocks cycle: {e}"})
            self.stop_event.wait(self.cycle_interval)

    def _cycle(self) -> None:
        if not stocks_strategy.is_market_open():
            # End-of-day flatten — the configured close (15:55 ET) is 5 min
            # before the real market close (16:00 ET), giving SELL orders
            # time to fill before the bell. Stocks held overnight expose us
            # to opening-gap risk that the backtest didn't model. Flatten
            # ONCE per session and re-arm at the next open.
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            today_et = datetime.now(timezone.utc).astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat()
            if self._eod_flattened_date != today_et:
                held = self._fetch_held_symbols()
                if held:
                    flat_count = 0
                    for sym in held:
                        snap = {"close": 0.0, "atr": 0.0}
                        try:
                            df = self.analyst._bars(sym, n=120)
                            if not df.empty:
                                snap["close"] = float(df["close"].iloc[-1])
                        except Exception:
                            pass
                        self.decision_queue.put({
                            "symbol": sym,
                            "decision": "SELL",
                            "confidence": 100.0,
                            "reason": "eod_flatten",
                            "snapshot": snap,
                            "atr_sl_mult": 0.0,
                            "atr_tp_mult": 0.0,
                        })
                        flat_count += 1
                    self.log_queue.put({
                        "tag": "STOCKS",
                        "text": f"EOD flatten — sent SELL for {flat_count} "
                                f"position(s): {sorted(held)}",
                    })
                self._eod_flattened_date = today_et

            if time.time() - self._last_market_closed_log > 600:
                self._last_market_closed_log = time.time()
                self.log_queue.put({
                    "tag": "STOCKS",
                    "text": "market closed — idle until next open",
                })
            return

        # FOMC veto — cached per day, hits Haiku at most once.
        try:
            news = self.analyst.db.get_recent_sentiment_feed(limit=25) or []
        except Exception:
            news = []
        if earnings_veto.check_fomc(news, anthropic_client=self.analyst.client):
            if not self._fomc_warned_today:
                self._fomc_warned_today = True
                self.log_queue.put({
                    "tag": "STOCKS",
                    "text": "FOMC day/eve — all trading paused",
                })
            return

        # Index health — fail-open on data shortage (treat as healthy so
        # the trend filter alone gates entries during cold start).
        index_healthy = True
        try:
            spy = self.analyst._bars("SPY", n=1500)
            qqq = self.analyst._bars("QQQ", n=1500)
            if not spy.empty and not qqq.empty:
                health = stocks_strategy.check_index_health(spy, qqq)
                index_healthy = bool(health.get("healthy"))
                if not index_healthy and time.time() - self._last_index_log > 300:
                    self._last_index_log = time.time()
                    self.log_queue.put({
                        "tag": "STOCKS",
                        "text": f"index bearish — BUY veto ({health.get('reason')})",
                    })
        except Exception as e:
            self.log_queue.put({"tag": "ERROR", "text": f"index health: {e}"})

        held = self._fetch_held_symbols()
        for sym in self.symbols_getter() or []:
            if self.stop_event.is_set():
                return
            try:
                verdict = self.analyst.decide(
                    sym, index_healthy=index_healthy, held_symbols=held,
                )
            except Exception as e:
                self.log_queue.put({"tag": "ERROR", "text": f"{sym}: {e}"})
                continue
            if verdict and verdict["decision"] in ("BUY", "SELL"):
                self.decision_queue.put(verdict)

    def _fetch_held_symbols(self) -> set[str] | None:
        if not config.ALPACA_KEY:
            return set()
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            positions = client.get_all_positions() or []
        except Exception:
            return None
        held: set[str] = set()
        for p in positions:
            klass = str(getattr(p, "asset_class", "")).split(".")[-1].lower()
            if klass not in ("us_equity", "stock", "equity"):
                continue
            sym = (getattr(p, "symbol", "") or "").upper()
            if sym:
                held.add(sym)
        return held
