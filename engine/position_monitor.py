"""Manual TP/SL manager — bracket orders aren't supported for Alpaca crypto.

Polls Alpaca open positions every few seconds.  For each one, looks up the
TP/SL prices stored in the matching `orders` row, compares against the latest
tick price from `market_data`, and fires a market exit if either bound is hit.
PnL is computed (current_price − entry_price) × qty and written back to the
order row.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import config


def _seconds_since(ts) -> float:
    """How many seconds ago `ts` happened (returns 1e9 if ts is missing)."""
    if ts is None:
        return 1e9
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 1e9


def _to_db_symbol(alpaca_symbol: str) -> str:
    s = (alpaca_symbol or "").upper()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "EUR", "BTC"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s


def _pos_matches_class(pos, asset_class: str) -> bool:
    raw = str(getattr(pos, "asset_class", "")).split(".")[-1].lower()
    if asset_class == "crypto":
        return raw == "crypto"
    return raw in ("us_equity", "stock", "equity")


class PositionMonitor(threading.Thread):
    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
        poll_interval: int = 5,
        learning_loop=None,
        asset_class: str = "crypto",
    ) -> None:
        super().__init__(name=f"PositionMonitor[{asset_class}]", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        # Optional — when set, real-time loss post-mortems are generated.
        self.learning_loop = learning_loop
        self.asset_class = asset_class
        self._client = None
        # alpaca_order_id -> True once closed (avoid double-exit if state is stale)
        self._closed: set[str] = set()

    def _log(self, text: str, tag: str = "ORDER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not config.ALPACA_KEY:
            return None
        try:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            return self._client
        except Exception as e:
            self._log(f"position_monitor: alpaca client init failed: {e}", tag="ERROR")
            return None

    # ------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"position_monitor tick error: {e}", tag="ERROR")
            self.stop_event.wait(self.poll_interval)

    def _tick(self) -> None:
        client = self._ensure_client()
        if client is None:
            return

        try:
            positions = client.get_all_positions()
        except Exception as e:
            self._log(f"get_all_positions failed: {e}", tag="ERROR")
            return

        for pos in positions or []:
            try:
                if not _pos_matches_class(pos, self.asset_class):
                    continue
                self._check_position(client, pos)
            except Exception as e:
                self._log(f"check_position failed: {e}", tag="ERROR")

    # ------------------------------------------------------------------
    def _check_position(self, client, pos: Any) -> None:
        symbol = getattr(pos, "symbol", "")
        # Stocks symbols have no quote suffix; the crypto-aware converter
        # passes them through unchanged but skip the call for clarity.
        if self.asset_class == "stock":
            db_symbol = symbol.upper()
        else:
            db_symbol = _to_db_symbol(symbol)
        side_raw = str(getattr(pos, "side", "")).split(".")[-1].lower()
        entry_side = "BUY" if side_raw == "long" else "SELL"

        qty = abs(float(getattr(pos, "qty", 0) or 0))
        if qty <= 0:
            return

        entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)
        current_price = float(
            getattr(pos, "current_price", 0)
            or getattr(pos, "market_value", 0) / qty if qty else 0
        )
        if current_price <= 0:
            return

        entry_order = self.db.get_latest_open_entry(
            db_symbol, entry_side, asset_class=self.asset_class,
        )
        if entry_order is None:
            return
        order_id = entry_order.get("alpaca_order_id")
        if order_id in self._closed:
            return

        # ---- QUICK-PROFIT EXIT (off by default since preset v2).
        # With ATR-based SL/TP the R:R is engineered into the bracket; clamping
        # winners early to 0.5% NET destroyed the math (losses ran to full SL
        # while winners were cut short → impossible to be profitable without
        # >70% WR). Keep the code path so users can re-enable per-setting if
        # they want, but skip entirely when quick_profit_pct <= 0.
        quick_pct = float(self.db.get_setting("quick_profit_pct", 0.0) or 0.0)
        if quick_pct > 0:
            commission_pct = float(self.db.get_setting("alpaca_commission_pct", 0.0015) or 0.0015)
            min_hold_s = float(self.db.get_setting("min_hold_seconds", 30) or 30)
            gross_target = quick_pct + 2 * commission_pct

            entry_ts = entry_order.get("timestamp")
            held_seconds = _seconds_since(entry_ts)

            direction = 1 if entry_side == "BUY" else -1
            pnl_pct = ((current_price - entry_price) / entry_price) * direction
            if held_seconds >= min_hold_s and pnl_pct >= gross_target:
                exit_side_str = "SELL" if entry_side == "BUY" else "BUY"
                self._submit_exit(
                    client, symbol, exit_side_str, qty,
                    entry_price, current_price, order_id, "QUICK_PROFIT", entry_side,
                )
                return

        # ---- BACKSTOP: TP / SL fall back if quick-profit never fired
        tp = entry_order.get("tp_price")
        sl = entry_order.get("sl_price")
        if tp is None or sl is None:
            return
        tp = float(tp); sl = float(sl)

        hit_tp = False
        hit_sl = False
        if entry_side == "BUY":
            if current_price >= tp:
                hit_tp = True
            elif current_price <= sl:
                hit_sl = True
        else:
            if current_price <= tp:
                hit_tp = True
            elif current_price >= sl:
                hit_sl = True

        if not (hit_tp or hit_sl):
            return

        reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
        exit_side_str = "SELL" if entry_side == "BUY" else "BUY"
        self._submit_exit(
            client, symbol, exit_side_str, qty,
            entry_price, current_price, order_id, reason, entry_side,
        )

    # ------------------------------------------------------------------
    def _submit_exit(
        self,
        client,
        alpaca_symbol: str,
        exit_side: str,
        qty: float,
        entry_price: float,
        current_price: float,
        original_order_id: str,
        reason: str,
        entry_side: str,
    ) -> None:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = MarketOrderRequest(
                symbol=alpaca_symbol,
                qty=qty,
                side=OrderSide.BUY if exit_side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
            client.submit_order(order_data=req)
        except Exception as e:
            self._log(f"exit submit failed {alpaca_symbol}: {e}", tag="ERROR")
            return

        # Compute NET PnL: gross move minus round-trip Alpaca commission.
        direction = 1 if entry_side == "BUY" else -1
        gross_pnl = (current_price - entry_price) * qty * direction
        try:
            commission_pct = float(self.db.get_setting("alpaca_commission_pct", 0.0015) or 0.0015)
        except Exception:
            commission_pct = 0.0015
        commission_cost = (entry_price + current_price) * qty * commission_pct
        pnl = gross_pnl - commission_cost
        status = "STOPPED" if reason == "STOP_LOSS" else "FILLED"

        try:
            self.db.update_order_status(
                original_order_id, status, pnl,
                commission_paid=commission_cost,
            )
        except Exception as e:
            self._log(f"DB update_order_status failed: {e}", tag="ERROR")

        self._closed.add(original_order_id)
        sign = "+" if pnl >= 0 else ""
        self._log(
            f"{reason} exit {alpaca_symbol} {exit_side} qty={qty:g} "
            f"@ ${current_price:.4f}  PnL={sign}${pnl:.2f}",
        )

        # Separate SELL row in the orders ledger for clear UI rendering.
        # PnL is on the entry row already (pnl=0 here avoids double-count).
        if reason == "TAKE_PROFIT":
            exit_status = "EXIT_TP"
        elif reason == "QUICK_PROFIT":
            exit_status = "EXIT_QUICK"
        else:
            exit_status = "EXIT_SL"
        exit_id = f"exit-{original_order_id[:24]}-{int(time.time())}"
        db_sym = (
            alpaca_symbol.upper() if self.asset_class == "stock"
            else _to_db_symbol(alpaca_symbol)
        )
        try:
            self.db.insert_order(
                exit_id, db_sym, exit_side,
                qty, current_price,
                status=exit_status, pnl=0.0,
                tp_price=None, sl_price=None,
                asset_class=self.asset_class,
            )
        except Exception as e:
            self._log(f"exit row insert failed: {e}", tag="ERROR")

        # Real-time learning: every loss triggers a focused post-mortem so
        # the next decide() for this symbol applies the lesson immediately.
        if pnl < 0 and self.learning_loop is not None:
            # Look up the original DB row to feed full context
            try:
                entry_row = self.db.get_latest_open_entry(
                    db_sym, entry_side, asset_class=self.asset_class,
                )
            except Exception:
                entry_row = None
            if entry_row is None:
                # row may have just been status-updated; rebuild minimal context
                entry_row = {
                    "alpaca_order_id": original_order_id,
                    "symbol": db_sym,
                    "side": entry_side,
                    "qty": qty,
                    "price": entry_price,
                    "timestamp": None,
                }
            try:
                self.learning_loop.analyze_loss(entry_row, current_price, reason)
            except Exception as e:
                self._log(f"loss analysis failed: {e}", tag="ERROR")


