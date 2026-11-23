"""Deterministic, LLM-free auto-tuner.

Runs every N minutes from the AI loop. Two responsibilities:

1) **Rolling confidence-threshold tuning** — bump `ai_confidence_threshold`
   up when the last ~30 trades are losing, down when they're winning. Bounded
   change per tick so we never chase noise, and capped to a sane band so the
   bot can't tune itself into doing nothing.

2) **Loss-streak circuit breaker** — if N consecutive losses occur, set
   `bot_pause_until_epoch` to (now + cooldown). AIAnalystLoop checks
   `is_paused()` and skips the cycle while active.

Complementary to LearningLoop (which is daily, LLM-driven, produces narrative
+ rules). This module costs zero tokens.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


# Bounds — widened upper cap so the tuner can lock the bot down hard
# when WR is catastrophic. Lower bound raised slightly to keep some
# selectivity even on a hot streak.
_CONF_MIN, _CONF_MAX = 60.0, 85.0
_CONF_UP_STEP = 2.0       # raise when losing → be more selective
_CONF_DOWN_STEP = 1.0     # lower when winning → be slightly more aggressive
_CONF_LOSS_WR = 40.0      # win rate below this triggers tightening
_CONF_WIN_WR = 60.0       # win rate above this allows loosening

_LOOKBACK_TRADES = 30
_MIN_SAMPLE = 20

_LOSS_STREAK_TRIGGER = 5
_PAUSE_COOLDOWN_SEC = 60 * 60   # 60 min


class AutoTuner:
    def __init__(self, db, log_queue) -> None:
        self.db = db
        self.log_queue = log_queue

    # ----- logging helper -------------------------------------------
    def _log(self, text: str, tag: str = "TUNER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    # ----- public API -----------------------------------------------
    def is_paused(self) -> tuple[bool, float]:
        """Return (paused, seconds_remaining)."""
        try:
            until = float(self.db.get_setting("bot_pause_until_epoch", 0.0) or 0.0)
        except Exception:
            return False, 0.0
        now = time.time()
        if until > now:
            return True, until - now
        return False, 0.0

    def tune(self) -> None:
        """Run all rules once. Each rule is independent and idempotent."""
        try:
            self._tune_confidence_threshold()
        except Exception as e:
            self._log(f"threshold tune failed: {e}", tag="ERROR")
        try:
            self._check_loss_streak()
        except Exception as e:
            self._log(f"loss-streak check failed: {e}", tag="ERROR")

    # ----- rule 1: confidence threshold -----------------------------
    def _tune_confidence_threshold(self) -> None:
        trades = self._recent_closed_trades(limit=_LOOKBACK_TRADES)
        if len(trades) < _MIN_SAMPLE:
            return
        wins = sum(1 for t in trades if float(t.get("pnl") or 0) > 0)
        wr = wins / len(trades) * 100.0

        cur = float(self.db.get_setting("ai_confidence_threshold", 65.0) or 65.0)
        if wr < _CONF_LOSS_WR:
            new = min(_CONF_MAX, cur + _CONF_UP_STEP)
        elif wr > _CONF_WIN_WR:
            new = max(_CONF_MIN, cur - _CONF_DOWN_STEP)
        else:
            return

        if abs(new - cur) < 0.5:
            return
        self.db.update_setting(
            "ai_confidence_threshold", new,
            f"auto_tuner: WR={wr:.0f}% over last {len(trades)}",
        )
        self._log(
            f"WR={wr:.0f}% over last {len(trades)} closed → "
            f"ai_confidence_threshold {cur:.0f}→{new:.0f}"
        )

    # ----- rule 2: loss-streak pause --------------------------------
    def _check_loss_streak(self) -> None:
        trades = self._recent_closed_trades(limit=10)
        if not trades:
            return
        streak = 0
        for t in trades:           # newest first
            pnl = float(t.get("pnl") or 0)
            if pnl < 0:
                streak += 1
            else:
                break
        if streak < _LOSS_STREAK_TRIGGER:
            return

        # Don't re-trigger if already paused
        paused, _ = self.is_paused()
        if paused:
            return

        until = time.time() + _PAUSE_COOLDOWN_SEC
        self.db.update_setting(
            "bot_pause_until_epoch", until,
            f"auto_tuner: {streak} consecutive losses",
        )
        when = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M UTC")
        self._log(
            f"⛔ {streak} consecutive losses — pausing decisions until {when} "
            f"({_PAUSE_COOLDOWN_SEC // 60}min cooldown)",
            tag="LEARNING",
        )

    # ----- helpers --------------------------------------------------
    def _recent_closed_trades(self, *, limit: int) -> list[dict]:
        """Return up to `limit` most-recent CLOSED orders (pnl != 0 or status
        indicates exit). Newest first."""
        rows = self.db.get_last_100_orders() or []
        out: list[dict] = []
        for r in rows:
            status = (r.get("status") or "").upper()
            pnl = float(r.get("pnl") or 0)
            # closed = realized pnl or explicit exit/close status
            if pnl != 0 or status in (
                "EXIT_TP", "EXIT_SL", "EXIT_QUICK", "STOPPED", "CANCELED",
            ):
                out.append(r)
            if len(out) >= limit:
                break
        return out
