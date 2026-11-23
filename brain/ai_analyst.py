"""Math-driven decision engine + LLM news-veto + sentiment scorer.

Decision flow:
  1. Math (RSI + 15m-EMA50 trend + ATR) chooses BUY/SELL/HOLD.
     - BUY only when RSI <= rsi_buy_threshold AND 15m trend is UP.
     - SELL only when we already hold the symbol and RSI >= rsi_sell_threshold
       (or trend has flipped DOWN). Alpaca crypto is long-only.
     - HOLD otherwise.
  2. LLM "safety net": for BUY signals only, if recent sentiment has any
     strongly bearish item (potential hack/crash/regulation news), ask Haiku
     to inspect those snippets and veto if catastrophic. Skipped entirely
     when sentiment is clean → most cycles cost ZERO LLM tokens.

Sentiment scoring (Twitter/News agents) still runs and is stored to DB —
it feeds the veto context but no longer wakes up the analyst on its own.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

import config
from brain import indicators

log = logging.getLogger(__name__)


_NEWS_VETO_SYSTEM = """You are a safety filter for an algorithmic trading bot.

The bot's math (RSI + trend + ATR) has already decided to BUY a crypto
symbol. Your ONE job: scan the recent Twitter/News snippets and decide if
there is a CATASTROPHIC event in progress that should veto the trade.

Veto only for severe, asset-specific negative events:
  • Exchange hack / wallet drain affecting this asset
  • Major protocol exploit, bridge hack
  • Regulatory shutdown (SEC enforcement, country ban)
  • De-peg / chain halt / collapse
  • Founder/CEO arrest, fraud revelation

Do NOT veto on:
  • Normal price drops, profit-taking, "overbought" chatter
  • Routine market commentary, opinion pieces, price predictions
  • Macro news not specific to this asset
  • Vague FUD without concrete events
  • Bearish sentiment in absence of a triggering event

Default to LET-IT-RIDE. The bot already has trend + ATR risk management;
your job is to catch the rare black-swan headline that math can't see.

Respond with a single JSON object only:
{"veto": <true|false>, "reason": "<one sentence — name the event or 'no catastrophic event'>"}
"""

_SENTIMENT_SYSTEM = """You score short financial text snippets for trader sentiment.
Output a JSON object: {"score": <float -1.0 to 1.0>, "summary": "<<= 15 words>"}.
-1.0 = strongly bearish, 0.0 = neutral, +1.0 = strongly bullish.
"""

_SENTIMENT_BATCH_SYSTEM = """You score multiple short financial text snippets
for trader sentiment in ONE call.  Input is a JSON array of strings.  Output
a JSON ARRAY of the SAME length, each element:
  {"score": <float -1.0..1.0>, "summary": "<<=15 words>"}
-1.0 = strongly bearish, 0.0 = neutral, +1.0 = strongly bullish.
Return ONLY the JSON array, nothing else.  Keep the order identical to input.
"""

_OPTIMIZE_SYSTEM = """You audit a trading bot's recent order history and propose new risk
parameters. You ONLY adjust stop_loss_percent (0.001-0.02) and take_profit_percent
(0.002-0.04). Output JSON:
{"stop_loss_percent": <float>, "take_profit_percent": <float>, "rationale": "<sentence>"}
"""


class AIAnalyst:
    def __init__(self, db, log_queue) -> None:
        self.db = db
        self.log_queue = log_queue
        self.client = (
            anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY else None
        )
        # Throttle per-symbol "insufficient bars" log — fires every cycle
        # for newly added symbols until 25 minutes of live bars accumulate.
        self._bars_log_ts: dict[str, float] = {}

    # ----- logging helper -------------------------------------------
    def _log(self, text: str, tag: str = "AI") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    # ----- sentiment ------------------------------------------------
    def score_sentiment(self, text: str) -> tuple[float, str]:
        """Single-item scoring (kept for backwards compat). Prefer score_sentiment_batch."""
        if not self.client or not text.strip():
            return 0.0, text[:120]
        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_SENTIMENT_MODEL,
                max_tokens=120,
                system=[{
                    "type": "text",
                    "text": _SENTIMENT_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": text[:1500]}],
            )
            out = _extract_text(resp)
            data = _parse_json(out)
            score = float(data.get("score", 0.0))
            score = max(-1.0, min(1.0, score))
            summary = str(data.get("summary", text[:120]))[:240]
            return score, summary
        except Exception as e:
            log.warning("sentiment scoring failed: %s", e)
            return 0.0, text[:120]

    def score_sentiment_batch(self, texts: list[str]) -> list[tuple[float, str]]:
        """Score N items in ONE Haiku call — ~6-10x cheaper than per-item.

        Returns a list aligned with `texts`. If the batch call fails the result
        is filled with neutral (0.0, truncated text) entries for resilience.
        """
        if not texts:
            return []
        if not self.client:
            return [(0.0, t[:120]) for t in texts]

        # Trim each text to keep batch under a reasonable size
        trimmed = [t[:600] for t in texts]
        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_SENTIMENT_MODEL,
                max_tokens=80 * max(1, len(trimmed)),
                system=[{
                    "type": "text",
                    "text": _SENTIMENT_BATCH_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": json.dumps(trimmed)}],
            )
        except Exception as e:
            log.warning("batch sentiment scoring failed: %s", e)
            return [(0.0, t[:120]) for t in texts]

        raw = _extract_text(resp).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            arr = json.loads(raw)
            if not isinstance(arr, list):
                raise ValueError("not a list")
        except Exception:
            return [(0.0, t[:120]) for t in texts]

        out: list[tuple[float, str]] = []
        for i, t in enumerate(texts):
            if i < len(arr) and isinstance(arr[i], dict):
                try:
                    s = float(arr[i].get("score", 0.0))
                    s = max(-1.0, min(1.0, s))
                except Exception:
                    s = 0.0
                summ = str(arr[i].get("summary", t[:120]))[:240]
                out.append((s, summ))
            else:
                out.append((0.0, t[:120]))
        return out

    # ----- cycle context (cheap; read once per cycle) ---------------
    def build_cycle_context(self) -> dict:
        """Cycle-scope settings shared across all symbols in this AI tick.

        Math-only — no LLM context needed since the LLM is now only a news
        veto. The block stays so the rest of the loop has one place to
        fetch tunable thresholds.
        """
        try:
            rsi_buy = self.db.get_setting("rsi_buy_threshold", 30.0)
            rsi_sell = self.db.get_setting("rsi_sell_threshold", 70.0)
            atr_sl_mult = self.db.get_setting("atr_sl_mult", 1.5) or 1.5
            atr_tp_mult = self.db.get_setting("atr_tp_mult", 3.0) or 3.0
            atr_period = int(self.db.get_setting("atr_period", 14) or 14)
            trend_ema = int(self.db.get_setting("trend_ema_15m_period", 50) or 50)
            news_veto_thr = self.db.get_setting("news_veto_sentiment_threshold", -0.5) or -0.5
            news_veto_min = int(self.db.get_setting("news_veto_min_count", 3) or 3)
        except Exception:
            rsi_buy, rsi_sell = 30.0, 70.0
            atr_sl_mult, atr_tp_mult = 1.5, 3.0
            atr_period, trend_ema = 14, 50
            news_veto_thr, news_veto_min = -0.5, 3

        return {
            "thresholds": {"rsi_buy": rsi_buy, "rsi_sell": rsi_sell},
            "atr": {"sl_mult": atr_sl_mult, "tp_mult": atr_tp_mult,
                    "period": atr_period},
            "trend_ema_15m": trend_ema,
            "news_veto": {"sentiment_threshold": news_veto_thr,
                          "min_count": news_veto_min},
        }

    # ----- decision -------------------------------------------------
    def decide(
        self,
        symbol: str,
        cycle_context: dict | None = None,
        held_symbols: "set[str] | dict[str, float] | None" = None,
    ) -> dict | None:
        """Math-driven decision with optional LLM news-veto on BUYs.

        held_symbols=None means "positions fetch failed" → fail-CLOSED on
        BUY (don't open new risk if we can't see our book) but allow SELL
        evaluation. When `held_symbols` is a dict (symbol→qty), `decide()`
        uses the qty to suppress SELL on dust positions Alpaca can't fill.
        """
        if cycle_context is None:
            cycle_context = self.build_cycle_context()
        rsi_buy = cycle_context["thresholds"]["rsi_buy"]
        rsi_sell = cycle_context["thresholds"]["rsi_sell"]
        atr_period = cycle_context["atr"]["period"]
        trend_ema = cycle_context["trend_ema_15m"]

        # Pull enough bars for both: 14-bar RSI/ATR + 50×15m EMA resample.
        # 50 * 15 = 750 minutes; pad to 1500 so the resample has clean buckets.
        bars = self.db.get_recent_bars(symbol, n=1500, asset_class="crypto")
        df = indicators.to_dataframe(bars)
        if len(df) < 25:
            import time as _t
            now = _t.time()
            last = self._bars_log_ts.get(symbol, 0.0)
            if now - last > 1800:
                self._bars_log_ts[symbol] = now
                self._log(f"{symbol}: warming up ({len(df)}/25 bars)")
            return None

        snap = indicators.latest_indicator_snapshot(
            df, atr_period=atr_period, trend_ema=trend_ema,
        )

        rsi_v = snap.get("rsi")
        atr_v = snap.get("atr")
        trend = snap.get("trend") or {}
        is_held = bool(held_symbols and symbol in held_symbols)

        # ---- MATH DECISION -----------------------------------------
        decision = "HOLD"
        reason = ""
        if is_held:
            # Suppress SELL on dust holdings (<$1 market value) — Alpaca
            # won't fill those anyway, and we'd spam the signal log every
            # cycle. PositionMonitor still fires TP/SL exits.
            close_v = snap.get("close") or 0.0
            held_qty = (
                held_symbols.get(symbol)
                if isinstance(held_symbols, dict) else None
            )
            is_dust = (
                held_qty is not None and close_v > 0
                and held_qty * close_v < 1.0
            )
            if is_dust:
                return None
            # Long-only exit decision: SELL if overbought OR trend flipped.
            if rsi_v is not None and rsi_v >= rsi_sell:
                decision, reason = "SELL", f"rsi_overbought rsi={rsi_v:.1f}"
            elif trend.get("known") and trend.get("direction") == "down":
                decision, reason = "SELL", "trend_flipped_down_15m"
        else:
            # BUY only when oversold AND trend up — and ATR available for SL.
            if held_symbols is None:
                # fail-closed on entries: don't open positions when we can't
                # see our book
                pass
            elif (rsi_v is not None and rsi_v <= rsi_buy
                  and trend.get("known") and trend.get("direction") == "up"
                  and atr_v is not None and atr_v > 0):
                decision, reason = "BUY", (
                    f"rsi_oversold rsi={rsi_v:.1f} + 15m_uptrend "
                    f"(ema={trend.get('ema'):.4f})"
                )

        if decision == "HOLD":
            return None

        # ---- LLM NEWS VETO (only for BUY; SELL exits should not be blocked)
        veto_meta: dict | None = None
        if decision == "BUY":
            try:
                sentiments = self.db.get_recent_sentiment(symbol, hours=1) or []
            except Exception:
                sentiments = []

            # ENFORCE the AI's own loss-lesson: "REJECT BUY when
            # sentiment_sources_at_entry == 0". DOGE -$13.74 trade entered
            # with zero sentiment context and stopped out hard. Until we
            # see real news/twitter coverage, this asset is invisible to
            # our risk model and we don't open new exposure on it.
            if not sentiments:
                self._log(
                    f"{symbol}: BUY blocked — 0 sentiment sources "
                    f"(rule from loss_lessons: don't trade blind)",
                )
                try:
                    self.db.insert_signal(
                        symbol, rsi_v or 0.0, "HOLD", 0,
                        json.dumps({"blocked": "no_sentiment_context"})[:2000],
                        asset_class="crypto",
                    )
                except Exception:
                    pass
                return None

            veto_thr = float(cycle_context["news_veto"]["sentiment_threshold"])
            veto_min = int(cycle_context["news_veto"]["min_count"])
            bearish = [s for s in sentiments
                       if (s.get("sentiment_score") is not None
                           and float(s["sentiment_score"]) <= veto_thr)]
            if len(bearish) >= veto_min and self.client is not None:
                vetoed, veto_reason = self._news_veto(symbol, bearish[:8])
                veto_meta = {"vetoed": vetoed, "reason": veto_reason,
                             "bearish_count": len(bearish)}
                if vetoed:
                    self._log(
                        f"{symbol}: BUY vetoed by news filter — {veto_reason}",
                    )
                    try:
                        self.db.insert_signal(
                            symbol, rsi_v or 0.0, "HOLD", 0,
                            json.dumps({"veto": veto_meta})[:2000],
                            asset_class="crypto",
                        )
                    except Exception:
                        pass
                    return None

        # Fixed math-confidence for DB logging. Order worker no longer gates
        # on ai_confidence_threshold for math-driven decisions (see
        # AIAnalystLoop._cycle); the selectivity knob is RSI + trend + ATR.
        confidence = 80.0
        try:
            self.db.insert_signal(
                symbol, rsi_v or 0.0, decision, confidence,
                json.dumps({"reason": reason, "veto": veto_meta},
                           default=str)[:2000],
                asset_class="crypto",
            )
        except Exception as e:
            self._log(f"signal insert failed: {e}", tag="ERROR")

        self._log(f"{symbol}: {decision} — {reason}")
        return {
            "symbol": symbol,
            "decision": decision,
            "confidence": confidence,
            "reason": reason,
            "snapshot": snap,
            "atr_sl_mult": cycle_context["atr"]["sl_mult"],
            "atr_tp_mult": cycle_context["atr"]["tp_mult"],
        }

    # ----- LLM news veto helper -------------------------------------
    def _news_veto(self, symbol: str, bearish_items: list[dict]) -> tuple[bool, str]:
        """Ask Haiku whether any bearish news constitutes a catastrophic event."""
        payload = {
            "symbol": symbol,
            "bearish_snippets": [
                {
                    "source": s.get("source"),
                    "score": float(s.get("sentiment_score") or 0),
                    "summary": (s.get("summary") or "")[:240],
                }
                for s in bearish_items
            ],
        }
        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_SENTIMENT_MODEL,   # cheaper model — this is a yes/no
                max_tokens=120,
                system=[{
                    "type": "text",
                    "text": _NEWS_VETO_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user",
                           "content": json.dumps(payload, default=str)[:3000]}],
            )
        except Exception as e:
            self._log(f"{symbol}: news veto call failed (allowing trade): {e}",
                      tag="ERROR")
            return False, "veto_call_failed"

        data = _parse_json(_extract_text(resp))
        return bool(data.get("veto")), str(data.get("reason", ""))[:240]

    # ----- self-optimize --------------------------------------------
    def self_optimize(self) -> None:
        if not self.client:
            return
        try:
            orders = self.db.get_last_100_orders()
        except Exception as e:
            self._log(f"optimize: cannot read orders: {e}", tag="ERROR")
            return
        if len(orders) < 10:
            self._log("optimize: skipping (need >=10 orders)")
            return

        summary = {
            "total": len(orders),
            "wins": sum(1 for o in orders if float(o.get("pnl") or 0) > 0),
            "losses": sum(1 for o in orders if float(o.get("pnl") or 0) < 0),
            "stopped": sum(1 for o in orders if (o.get("status") or "") == "STOPPED"),
            "avg_pnl": sum(float(o.get("pnl") or 0) for o in orders) / len(orders),
            "current_stop_loss": self.db.get_setting("stop_loss_percent", 0.0035),
            "current_take_profit": self.db.get_setting("take_profit_percent", 0.0060),
        }
        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_DECISION_MODEL,
                max_tokens=300,
                system=[{
                    "type": "text",
                    "text": _OPTIMIZE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": json.dumps(summary)}],
            )
        except Exception as e:
            self._log(f"optimize call failed: {e}", tag="ERROR")
            return

        data = _parse_json(_extract_text(resp))
        sl = float(data.get("stop_loss_percent") or summary["current_stop_loss"])
        tp = float(data.get("take_profit_percent") or summary["current_take_profit"])
        sl = max(0.001, min(0.02, sl))
        tp = max(0.002, min(0.04, tp))

        self.db.update_setting("stop_loss_percent", sl, "AI optimized")
        self.db.update_setting("take_profit_percent", tp, "AI optimized")
        self._log(f"optimized: SL={sl:.4f}  TP={tp:.4f}  ({data.get('rationale','')})")


# =================================================================
class AIAnalystLoop(threading.Thread):
    """Background loop: cycles symbols, calls decide(), pushes decisions to queue.
    Also runs the end-of-day learning loop once per UTC calendar day."""

    def __init__(
        self,
        analyst: AIAnalyst,
        decision_queue,
        log_queue,
        stop_event: threading.Event,
        symbols_getter,
        learning_loop=None,
        auto_tuner=None,
        cycle_interval: int = 90,
    ) -> None:
        super().__init__(name="AIAnalystLoop", daemon=True)
        self.analyst = analyst
        self.decision_queue = decision_queue
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.symbols_getter = symbols_getter
        self.learning_loop = learning_loop
        self.auto_tuner = auto_tuner
        self.cycle_interval = cycle_interval
        self._last_learning_check = 0.0
        self._last_tune_check = 0.0
        self._last_pause_log = 0.0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                self.log_queue.put({"tag": "ERROR", "text": f"AI cycle: {e}"})
            self.stop_event.wait(self.cycle_interval)

    def _fetch_held_symbols(self) -> "dict[str, float] | None":
        """Return {symbol: qty} for crypto positions. None on failure →
        decide() fails closed on BUY. Returning a dict (rather than a
        plain set) lets decide() consult qty for dust suppression.
        """
        if not config.ALPACA_KEY:
            return {}
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            positions = client.get_all_positions() or []
        except Exception:
            return None  # fail-open
        held: dict[str, float] = {}
        for p in positions:
            klass = str(getattr(p, "asset_class", "")).split(".")[-1].lower()
            if klass and klass != "crypto":
                continue
            raw = (getattr(p, "symbol", "") or "").upper()
            if "/" not in raw:
                for quote in ("USDT", "USDC", "USD", "EUR", "BTC"):
                    if raw.endswith(quote) and len(raw) > len(quote):
                        raw = f"{raw[:-len(quote)]}/{quote}"
                        break
            try:
                qty = abs(float(getattr(p, "qty", 0) or 0))
            except Exception:
                qty = 0.0
            held[raw] = qty
        return held

    def _cycle(self) -> None:
        # Circuit breaker: skip cycle if auto_tuner paused decisions.
        if self.auto_tuner is not None:
            paused, remaining = self.auto_tuner.is_paused()
            if paused:
                # Log once per 5 min to avoid spam
                if time.time() - self._last_pause_log > 300:
                    self._last_pause_log = time.time()
                    self.log_queue.put({
                        "tag": "LEARNING",
                        "text": f"decisions paused — {int(remaining // 60)}min remaining",
                    })
                return

        symbols = self.symbols_getter() or []
        try:
            cycle_context = self.analyst.build_cycle_context()
        except Exception as e:
            self.log_queue.put({"tag": "ERROR", "text": f"cycle context: {e}"})
            cycle_context = None

        # Fetch held positions once per cycle so decide() can route BUY vs SELL
        # without redundant Alpaca calls per symbol.
        held_symbols = self._fetch_held_symbols()

        for sym in symbols:
            if self.stop_event.is_set():
                return
            verdict = self.analyst.decide(
                sym, cycle_context=cycle_context, held_symbols=held_symbols,
            )
            if verdict and verdict["decision"] in ("BUY", "SELL"):
                self.decision_queue.put(verdict)

        # Auto-tuner: run every ~15 min (cheap, no LLM)
        if self.auto_tuner and (time.time() - self._last_tune_check > 900):
            self._last_tune_check = time.time()
            try:
                self.auto_tuner.tune()
            except Exception as e:
                self.log_queue.put({"tag": "ERROR", "text": f"auto_tuner: {e}"})

        # End-of-day learning: check at most once per hour to keep DB chatter low.
        if self.learning_loop and (time.time() - self._last_learning_check > 3600):
            self._last_learning_check = time.time()
            try:
                self.learning_loop.run_if_due()
            except Exception as e:
                self.log_queue.put({"tag": "ERROR", "text": f"learning_loop: {e}"})


# =================================================================
def _extract_text(resp: Any) -> str:
    try:
        blocks = resp.content
        for b in blocks:
            if getattr(b, "type", None) == "text":
                return b.text
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")
    except Exception:
        pass
    return str(resp)


def _parse_json(text: str) -> dict:
    text = text.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# ---- CLI smoke test -----------------------------------------------
if __name__ == "__main__":
    import argparse
    from database.db_manager import DBManager
    import queue as _q

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol")
    parser.add_argument("--optimize-now", action="store_true")
    args = parser.parse_args()

    db = DBManager()
    db.connect()
    analyst = AIAnalyst(db, _q.Queue())
    if args.optimize_now:
        analyst.self_optimize()
    elif args.symbol:
        out = analyst.decide(args.symbol)
        print(json.dumps(out, indent=2, default=str))
    db.close()
