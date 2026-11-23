"""End-of-day self-learning loop.

Once per day:
1. Aggregate the last 24h of orders.
2. For each trade, fetch the signal + sentiment context that surrounded it.
3. Ask Claude Sonnet to extract:
   - narrative insights (why winners won, why losers lost)
   - atomic rules to remember for future trades
   - parameter adjustment suggestions (within safe bounds)
   - confidence calibration (did high-confidence trades actually win more?)
4. Write the result to `learning_journal` table.
5. Apply safe parameter changes to `bot_settings` (with bounds).

Future decisions (`AIAnalyst.decide`) include the most recent journal entries
in their context, so the bot literally learns from yesterday's mistakes.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

import anthropic

import config

log = logging.getLogger(__name__)


_LOSS_POSTMORTEM_SYSTEM = """You are the bot's real-time loss analyst.
You receive the FULL context of ONE losing trade — entry signals, sentiment
around entry, indicators, exit price — and produce a sharp diagnosis and
ONE atomic rule that, if applied, would have prevented this loss.

Be brutal and specific. Refer to measurable conditions (RSI value, sentiment
score, source count, time-of-day, MACD direction).

Output JSON ONLY:
{
  "diagnosis": "<2-sentence narrative: why it lost>",
  "rule_learned": "<single atomic rule starting with a verb (REJECT, REQUIRE, AVOID, DEMAND, SKIP); MUST be testable and reference a specific condition>"
}

The rule will be auto-applied to every future decision context. It must be
narrow enough not to block all trading (e.g., good: 'REJECT BUY on $SYMBOL
when sentiment_sources < 5 and avg_sentiment > 0.6 — unreliable burst';
bad: 'be more careful').
"""


_LEARNING_SYSTEM = """You are a self-improving trading bot's overnight analyst.
Given the day's trade outcomes (with surrounding signals + sentiment), produce
hard, actionable insights that future trade decisions can apply.

Be a hardass critic. Identify failure patterns FIRST, successes second.
Pull out atomic, testable rules — not vague platitudes. Each rule must reference
a measurable condition (RSI value, sentiment score, source, time-of-day, etc.).

Output JSON ONLY:
{
  "insights": "<2-4 sentence narrative — what worked, what didn't, why>",
  "rules_learned": [
    "<rule 1, atomic & testable>",
    "<rule 2>",
    ...up to 6
  ],
  "parameter_changes": {
    "ai_confidence_threshold": <new value or null>,
    "rsi_buy_threshold": <new value or null>,
    "rsi_sell_threshold": <new value or null>,
    "atr_sl_mult": <new value or null>,
    "atr_tp_mult": <new value or null>
  },
  "confidence_calibration": <float 0-100, "how well did stated confidence track actual win rate">
}

Rules for parameter_changes:
- Only suggest a change if you have STRONG evidence.
- Use null (not a value) for keys you don't want to change.
- SL/TP are sized by ATR multipliers. Increase atr_sl_mult if SL is being
  hit by noise (whipsaws); decrease atr_tp_mult if winners rarely reach TP.
- Stay within bounds:
  - ai_confidence_threshold: [55, 72]
  - rsi_buy_threshold: [20, 40]
  - rsi_sell_threshold: [60, 78]
  - atr_sl_mult: [1.0, 3.0]
  - atr_tp_mult: [1.5, 5.0]
"""


# Safe bounds applied when persisting param changes.
# v2: static SL/TP no longer tunable — they're only fallbacks now; live SL/TP
# is ATR-derived. Tunable knobs are the math thresholds (RSI bounds + ATR
# multipliers). ai_confidence_threshold kept for back-compat / human override.
_PARAM_BOUNDS = {
    "ai_confidence_threshold": (55.0, 72.0),
    "rsi_buy_threshold": (20.0, 40.0),
    "rsi_sell_threshold": (60.0, 78.0),
    "atr_sl_mult": (1.0, 3.0),
    "atr_tp_mult": (1.5, 5.0),
}

# Minimum closed-trade sample before the LLM is allowed to retune parameters.
# At N=20 a 50%-vs-25% WR difference clears a one-sided binomial p<0.05;
# below that we're tuning to noise. Matches auto_tuner._MIN_SAMPLE.
# Previously the gate was `len(orders) < 1` which let one bad 17-trade day
# (12 PENDING_NEW failures + 1 win) tighten rsi_buy_threshold 30→27 and
# starve the bot of entries entirely.
MIN_TRADES_FOR_LEARNING = 20

# Per-parameter daily delta cap (as a fraction of the current value). The LLM
# proposed 30→27 (10%) on a single day; without a cap it could just as easily
# propose 30→24 (20%) tomorrow and we'd be locked out again. Big moves now
# need conviction that repeats across multiple days.
_MAX_DAILY_PARAM_DELTA_PCT = 0.10


class LearningLoop:
    def __init__(self, db, log_queue) -> None:
        self.db = db
        self.log_queue = log_queue
        self.client = (
            anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY else None
        )

    def _log(self, text: str, tag: str = "LEARNING") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    # ==================================================================
    #  Per-trade post-mortem (real-time, fired by PositionMonitor on loss)
    # ==================================================================
    def analyze_loss(self, order: dict, exit_price: float, reason: str) -> None:
        """Called the instant a position closes at a loss.

        Fetches the FULL context that surrounded the entry (signals, indicator
        snapshot, sentiment burst) and asks Claude for one sharp rule.  Result
        lands in `loss_lessons` and is visible to all future `decide()` calls.
        """
        if self.client is None:
            return

        entry_price = float(order.get("price") or order.get("entry_price") or 0)
        qty = float(order.get("qty") or 0)
        side = (order.get("side") or "").upper()
        symbol = order.get("symbol", "")
        entry_ts = order.get("timestamp")
        direction = 1 if side == "BUY" else -1
        pnl = (exit_price - entry_price) * qty * direction

        # ---- Gather context AS IT WAS at entry time.
        # entry_ts can be None when PositionMonitor synthesises a fallback
        # entry_row (e.g. adopted/orphan positions). The DB helpers do
        # `ts - timedelta(...)` which crashes on None — skip the context
        # fetch entirely in that case and let the AI work with what we have.
        if entry_ts is None:
            signal_rows = []
            sentiment_rows = []
        else:
            signal_rows = self.db.get_signals_near(symbol, entry_ts, window_min=10) or []
            sentiment_rows = self.db.get_sentiment_near(symbol, entry_ts, window_min=60) or []
        signal_obj = None
        rsi_at_entry = None
        if signal_rows:
            s = signal_rows[-1]
            rsi_at_entry = float(s.get("rsi") or 0) if s.get("rsi") is not None else None
            signal_obj = {
                "rsi": rsi_at_entry,
                "ai_decision": s.get("ai_decision"),
                "ai_confidence": float(s.get("ai_confidence") or 0),
            }
        avg_sent = None
        sent_n = len(sentiment_rows)
        if sentiment_rows:
            scores = [float(r.get("sentiment_score") or 0) for r in sentiment_rows]
            avg_sent = round(statistics.fmean(scores), 3)

        # ---- Recent context the AI sees in decide() — feed it back so the AI
        # can spot patterns it failed to weight properly
        prior_lessons = self.db.get_recent_loss_lessons(symbol=symbol, n=3)

        payload = {
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty": qty,
            "pnl": round(pnl, 4),
            "close_reason": reason,                # 'STOP_LOSS' typically
            "duration_min": _duration_minutes(entry_ts),
            "signal_at_entry": signal_obj,
            "avg_sentiment_at_entry": avg_sent,
            "sentiment_sources_at_entry": sent_n,
            "sentiment_samples": [
                {"source": r.get("source"),
                 "score": float(r.get("sentiment_score") or 0),
                 "summary": (r.get("summary") or "")[:160]}
                for r in sentiment_rows[:8]
            ],
            "prior_loss_lessons_for_symbol": [
                {"rule": p.get("rule_learned"),
                 "pnl":  float(p.get("pnl") or 0)}
                for p in prior_lessons
            ],
        }

        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_DECISION_MODEL,
                max_tokens=400,
                system=[{
                    "type": "text",
                    "text": _LOSS_POSTMORTEM_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, default=str)[:6000],
                }],
            )
        except Exception as e:
            self._log(f"loss postmortem call failed: {e}", tag="ERROR")
            return

        data = _parse_json(_extract_text(resp))
        diagnosis = str(data.get("diagnosis") or "").strip()[:1000]
        rule = str(data.get("rule_learned") or "").strip()[:500]
        if not rule:
            return

        try:
            self.db.insert_loss_lesson(
                symbol=symbol, side=side,
                entry_ts=entry_ts,
                exit_ts=datetime.now(timezone.utc),
                entry_price=entry_price, exit_price=exit_price,
                qty=qty, pnl=round(pnl, 4),
                rsi_at_entry=rsi_at_entry,
                avg_sentiment_at_entry=avg_sent,
                sentiment_sources_at_entry=sent_n,
                rule_learned=rule, diagnosis=diagnosis,
                context_summary=payload,
            )
        except Exception as e:
            self._log(f"loss lesson insert failed: {e}", tag="ERROR")
            return

        sign = "+" if pnl >= 0 else ""
        self._log(
            f"loss post-mortem {symbol}: PnL {sign}${pnl:.2f} — {diagnosis[:160]}",
        )
        self._log(f"  ✦ rule: {rule[:200]}")

    # ------------------------------------------------------------------
    def should_run_for_today(self) -> bool:
        today = datetime.now(timezone.utc).date()
        try:
            return not self.db.has_journal_for_date(today)
        except Exception:
            return False

    def run_if_due(self) -> bool:
        if not self.should_run_for_today():
            return False
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        orders = self.db.get_orders_in_range(start, end)
        if len(orders) < MIN_TRADES_FOR_LEARNING:
            self._log(
                f"learning loop skipped — {len(orders)} trades in last 24h, "
                f"need {MIN_TRADES_FOR_LEARNING} for non-noise sample",
            )
            return False
        try:
            self.analyze_and_journal(orders, end.date())
            return True
        except Exception as e:
            self._log(f"learning loop failed: {e}", tag="ERROR")
            return False

    # ------------------------------------------------------------------
    def analyze_and_journal(self, orders: list[dict], journal_date: date) -> None:
        if not orders:
            return
        if self.client is None:
            self._log("Claude disabled — cannot run learning loop", tag="ERROR")
            return

        # ---- Aggregate stats
        wins = [o for o in orders if float(o.get("pnl") or 0) > 0]
        losses = [o for o in orders if float(o.get("pnl") or 0) < 0]
        total_pnl = sum(float(o.get("pnl") or 0) for o in orders)
        total = len(orders)
        win_rate = (len(wins) / total * 100) if total else 0.0

        # ---- Build per-trade enriched context
        enriched = []
        for o in orders:
            ts = o["timestamp"]
            signal_rows = self.db.get_signals_near(o["symbol"], ts, window_min=10) or []
            sentiment_rows = self.db.get_sentiment_near(o["symbol"], ts, window_min=60) or []
            signal_obj = None
            if signal_rows:
                s = signal_rows[-1]
                signal_obj = {
                    "rsi": float(s.get("rsi") or 0),
                    "ai_decision": s.get("ai_decision"),
                    "ai_confidence": float(s.get("ai_confidence") or 0),
                }
            avg_sent = None
            if sentiment_rows:
                scores = [float(r.get("sentiment_score") or 0) for r in sentiment_rows]
                avg_sent = round(statistics.fmean(scores), 3)

            enriched.append({
                "symbol": o["symbol"],
                "side": o["side"],
                "qty": float(o.get("qty") or 0),
                "entry_price": float(o.get("price") or 0),
                "pnl": float(o.get("pnl") or 0),
                "status": o.get("status"),
                "signal": signal_obj,
                "avg_sentiment": avg_sent,
                "sentiment_sources": len(sentiment_rows),
                "hour_utc": ts.astimezone(timezone.utc).hour if hasattr(ts, "astimezone") else None,
            })

        # ---- Calibration: do higher-confidence trades win more?
        with_conf = [e for e in enriched if e["signal"] and e["signal"]["ai_confidence"] > 0]
        calibration = 0.0
        if with_conf:
            high_conf = [e for e in with_conf if e["signal"]["ai_confidence"] >= 75]
            low_conf = [e for e in with_conf if e["signal"]["ai_confidence"] < 75]
            hc_wr = (sum(1 for e in high_conf if e["pnl"] > 0) / len(high_conf) * 100) if high_conf else 0
            lc_wr = (sum(1 for e in low_conf if e["pnl"] > 0) / len(low_conf) * 100) if low_conf else 0
            # 100 = perfectly calibrated (high-conf wins more), 0 = anti-calibrated
            calibration = max(0.0, min(100.0, 50.0 + (hc_wr - lc_wr)))

        # ---- Compose period summary
        period_summary = {
            "date": str(journal_date),
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(win_rate, 2),
            "avg_win_pnl": round(statistics.fmean([e["pnl"] for e in enriched if e["pnl"] > 0]), 4)
                if wins else 0.0,
            "avg_loss_pnl": round(statistics.fmean([e["pnl"] for e in enriched if e["pnl"] < 0]), 4)
                if losses else 0.0,
            "by_symbol": _group_by_symbol(enriched),
        }

        # ---- Previous journal context (last 3 entries → spiral upward)
        prior = self.db.get_recent_journal_entries(n=3)
        prior_compact = [
            {
                "date": str(p["journal_date"]),
                "win_rate": float(p.get("win_rate") or 0),
                "rules": list(p.get("rules_learned") or [])[:4],
            }
            for p in prior
        ]

        # ---- Read current params for context
        current_params = {}
        for k in _PARAM_BOUNDS:
            try:
                current_params[k] = float(self.db.get_setting(k) or 0)
            except Exception:
                current_params[k] = None

        # ---- Build prompt
        user_payload = {
            "period_summary": period_summary,
            "trades": enriched[:50],          # cap for token budget
            "current_parameters": current_params,
            "recent_journal": prior_compact,
            "confidence_calibration_score": round(calibration, 2),
        }

        try:
            resp = self.client.messages.create(
                model=config.CLAUDE_DECISION_MODEL,
                max_tokens=900,
                system=[{
                    "type": "text",
                    "text": _LEARNING_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{
                    "role": "user",
                    "content": json.dumps(user_payload, default=str)[:12000],
                }],
            )
        except Exception as e:
            self._log(f"learning Claude call failed: {e}", tag="ERROR")
            return

        raw = _extract_text(resp)
        data = _parse_json(raw)
        insights = str(data.get("insights") or "").strip()[:4000]
        rules = [str(r).strip()[:280] for r in (data.get("rules_learned") or [])][:6]
        param_changes_raw = data.get("parameter_changes") or {}

        # ---- Apply bounded parameter changes
        applied: dict[str, dict[str, float]] = {}
        for key, raw_val in param_changes_raw.items():
            if raw_val is None or key not in _PARAM_BOUNDS:
                continue
            try:
                new_val = float(raw_val)
            except Exception:
                continue
            lo, hi = _PARAM_BOUNDS[key]
            new_val = max(lo, min(hi, new_val))
            old_val = current_params.get(key)
            # Per-day delta cap: limit move to ±_MAX_DAILY_PARAM_DELTA_PCT
            # of current value. Repeated conviction across days can still
            # move the parameter all the way; one bad day cannot.
            if old_val is not None and old_val != 0:
                max_delta = abs(old_val) * _MAX_DAILY_PARAM_DELTA_PCT
                capped = max(old_val - max_delta, min(old_val + max_delta, new_val))
                if abs(capped - new_val) > 1e-9:
                    self._log(
                        f"param {key}: LLM proposed {new_val:.4f}, capped to "
                        f"{capped:.4f} (±{_MAX_DAILY_PARAM_DELTA_PCT*100:.0f}% "
                        f"of {old_val:.4f}/day)",
                    )
                    new_val = capped
            if old_val is not None and abs(new_val - old_val) < 1e-6:
                continue
            try:
                self.db.update_setting(key, new_val, "learning_loop")
                applied[key] = {"old": old_val, "new": new_val}
            except Exception as e:
                self._log(f"param update {key} failed: {e}", tag="ERROR")

        # R:R integrity guard. The AI can tune SL and TP independently and
        # we've seen it produce SL=2.0 / TP=2.5 (a 1.25:1 ratio that bleeds
        # on any WR < 44%). Enforce TP ≥ 2 × SL so the math always leaves
        # room to be profitable at a reasonable hit rate.
        try:
            sl = float(self.db.get_setting("atr_sl_mult", 1.5) or 1.5)
            tp = float(self.db.get_setting("atr_tp_mult", 3.0) or 3.0)
            min_tp = 2.0 * sl
            if tp < min_tp:
                clamped = max(min_tp, _PARAM_BOUNDS["atr_tp_mult"][0])
                clamped = min(clamped, _PARAM_BOUNDS["atr_tp_mult"][1])
                self.db.update_setting(
                    "atr_tp_mult", clamped,
                    f"R:R guard: forced TP {tp:.2f}→{clamped:.2f} "
                    f"(SL={sl:.2f}, target ≥2:1)",
                )
                applied["atr_tp_mult"] = {"old": tp, "new": clamped}
                self._log(
                    f"R:R guard fired: SL={sl:.2f}, TP {tp:.2f}→{clamped:.2f} "
                    f"(must be ≥ 2 × SL for profitable math)",
                )
        except Exception as e:
            self._log(f"R:R guard failed: {e}", tag="ERROR")

        # ---- Persist journal entry
        try:
            self.db.insert_journal_entry(
                journal_date=journal_date,
                total_trades=total,
                wins=len(wins),
                losses=len(losses),
                total_pnl=round(total_pnl, 4),
                win_rate=round(win_rate, 2),
                period_summary=period_summary,
                insights=insights,
                rules_learned=rules,
                parameter_changes=applied,
                confidence_calibration=round(calibration, 2),
            )
        except Exception as e:
            self._log(f"journal insert failed: {e}", tag="ERROR")
            return

        self._log(
            f"journal {journal_date}: {total} trades, WR={win_rate:.0f}%, "
            f"PnL=${total_pnl:.2f}, calibration={calibration:.0f}, "
            f"learned {len(rules)} rules, {len(applied)} params adjusted"
        )
        for r in rules:
            self._log(f"  • {r}")


# ====================================================================
def _group_by_symbol(enriched: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in enriched:
        s = e["symbol"]
        b = out.setdefault(s, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        b["trades"] += 1
        if e["pnl"] > 0:
            b["wins"] += 1
        elif e["pnl"] < 0:
            b["losses"] += 1
        b["pnl"] += e["pnl"]
    for s, b in out.items():
        b["pnl"] = round(b["pnl"], 4)
        b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
    return out


def _duration_minutes(entry_ts) -> float | None:
    if entry_ts is None:
        return None
    if hasattr(entry_ts, "tzinfo") and entry_ts.tzinfo is None:
        entry_ts = entry_ts.replace(tzinfo=timezone.utc)
    try:
        return round((datetime.now(timezone.utc) - entry_ts).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _extract_text(resp: Any) -> str:
    try:
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                return b.text
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")
    except Exception:
        pass
    return str(resp)


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}
