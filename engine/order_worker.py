"""Bracket-order dispatcher: consumes decisions, places orders, records to DB."""
from __future__ import annotations

import math
import queue
import threading
import time

import config


class OrderWorker(threading.Thread):
    def __init__(
        self,
        db,
        log_queue,
        decision_queue: queue.Queue,
        stop_event: threading.Event,
        notional_per_trade: float | None = None,
        max_trading_capital: float | None = None,
        asset_class: str = "crypto",
    ) -> None:
        super().__init__(name=f"OrderWorker[{asset_class}]", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.decision_queue = decision_queue
        self.stop_event = stop_event
        self.asset_class = asset_class
        self.notional_per_trade = (
            notional_per_trade if notional_per_trade is not None
            else config.NOTIONAL_PER_TRADE
        )
        self.max_trading_capital = (
            max_trading_capital if max_trading_capital is not None
            else config.MAX_TRADING_CAPITAL
        )
        self._client = None
        # Throttle noisy log lines (SELL-skipped per symbol — happens every
        # AI cycle on bearish sentiment for unheld symbols).
        self._sell_skip_log_ts: dict[str, float] = {}

    def _log(self, text: str, tag: str = "ORDER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not config.ALPACA_KEY:
            self._log("Alpaca creds missing — order worker idle", tag="ERROR")
            return None
        try:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            return self._client
        except Exception as e:
            self._log(f"alpaca trading client init failed: {e}", tag="ERROR")
            return None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                verdict = self.decision_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(verdict)
            except Exception as e:
                self._log(f"order handler error: {e}", tag="ERROR")

    def _handle(self, verdict: dict) -> None:
        client = self._ensure_client()
        if client is None:
            return

        symbol = verdict["symbol"]
        side = verdict["decision"]  # BUY or SELL
        snap = verdict.get("snapshot") or {}
        price = float(snap.get("close") or 0)
        if price <= 0:
            self._log(f"{symbol}: no price snapshot, skipping order", tag="ERROR")
            return

        # --- SELL signal: Alpaca crypto does NOT support short selling.
        # We can only sell an asset we hold. If we hold none, treat the SELL
        # as informational ("AI thinks bearish") and skip rather than letting
        # the broker reject with insufficient balance.
        held_qty = 0.0
        if side == "SELL":
            held_qty = self._held_qty(client, symbol)
            if held_qty <= 0:
                # Throttle: log per symbol at most once per hour. The AI
                # surfaces SELL on every bearish sentiment burst even for
                # symbols we don't hold, which would otherwise spam ORDER log.
                now = time.time()
                last = self._sell_skip_log_ts.get(symbol, 0.0)
                if now - last > 3600:
                    self._sell_skip_log_ts[symbol] = now
                    self._log(
                        f"{symbol}: SELL skipped — no open long to close "
                        f"(Alpaca crypto can't short)",
                        tag="ORDER",
                    )
                return

        # --- BUY concentration filter:
        # The bot's "buy-sell-buy-sell" rhythm assumes ONE position per symbol
        # at a time.  Stacking multiple BUYs on the same symbol caused single
        # exit fills to be misattributed to one DB row (we've seen single
        # rows show -$11+ losses when the broker-side cumulative position
        # blew through SL).  Enforce: refuse a new BUY if we already hold
        # that asset (PositionMonitor will exit at TP/SL/QUICK, then we
        # reconsider entry on the next cycle).
        if side == "BUY":
            current_holding = self._held_qty(client, symbol)
            if current_holding > 0:
                self._log(
                    f"{symbol}: BUY skipped — position already open "
                    f"({current_holding:g} held). Wait for exit before re-entry.",
                    tag="ORDER",
                )
                return

        # Read live capital settings from DB each tick — settings dialog may
        # have updated them mid-run.
        max_capital = float(
            self.db.get_setting("max_trading_capital", self.max_trading_capital)
            or self.max_trading_capital
        )
        notional = float(
            self.db.get_setting("notional_per_trade", self.notional_per_trade)
            or self.notional_per_trade
        )

        if side == "BUY":
            # --- Capital cap: don't exceed max_capital across open positions
            exposure = self._current_exposure(client)
            budget_left = max_capital - exposure
            if budget_left <= 0:
                self._log(
                    f"{symbol}: budget cap reached "
                    f"(exposure=${exposure:.2f} / ${max_capital:.2f}) — skipping",
                    tag="ORDER",
                )
                return
            trade_notional = min(notional, budget_left)
        else:
            # SELL closes an existing long — no budget check.
            # Show context but don't gate the trade.
            budget_left = float("inf")
            trade_notional = held_qty * price  # full close

        # $5 floor only applies to NEW entries (BUY). For SELL we're trying
        # to exit an existing position — refusing to close a dust position
        # because it's < $5 traps the position forever and spams the AI
        # log with repeated "SELL skipped" lines every minute. Alpaca crypto
        # accepts ~$1 closes; below that we just leave the dust alone since
        # it can't be sold profitably anyway.
        floor = 5.0 if side == "BUY" else 1.0
        if trade_notional < floor:
            now = time.time()
            last = self._sell_skip_log_ts.get(symbol, 0.0)
            if now - last > 3600:
                self._sell_skip_log_ts[symbol] = now
                self._log(
                    f"{symbol}: {side} notional ${trade_notional:.2f} below "
                    f"${floor:.2f} floor — skipping",
                    tag="ORDER",
                )
            return

        # Precision: scale rounding to the asset's price magnitude.
        # >$1000 → 2 dp; $1-$1000 → 4 dp; <$1 → 6 dp.
        if price >= 1000:
            price_dp = 2
        elif price >= 1:
            price_dp = 4
        else:
            price_dp = 6

        # ---- ATR-based dynamic SL/TP (preferred).
        # Verdict carries ATR + multipliers when analyst is the math engine.
        # If absent (legacy verdict or ATR missing), fall back to fixed % so
        # we never submit an order with NULL bracket prices.
        atr_v = float(snap.get("atr") or 0)
        sl_mult = float(verdict.get("atr_sl_mult", 0) or 0)
        tp_mult = float(verdict.get("atr_tp_mult", 0) or 0)
        if side == "BUY" and atr_v > 0 and sl_mult > 0 and tp_mult > 0:
            sl_price = round(price - sl_mult * atr_v, price_dp)
            tp_price = round(price + tp_mult * atr_v, price_dp)
        elif side == "SELL" and atr_v > 0 and sl_mult > 0 and tp_mult > 0:
            sl_price = round(price + sl_mult * atr_v, price_dp)
            tp_price = round(price - tp_mult * atr_v, price_dp)
        else:
            sl_pct = float(self.db.get_setting("stop_loss_percent", 0.0080) or 0.0080)
            tp_pct = float(self.db.get_setting("take_profit_percent", 0.0150) or 0.0150)
            if side == "BUY":
                tp_price = round(price * (1 + tp_pct), price_dp)
                sl_price = round(price * (1 - sl_pct), price_dp)
            else:
                tp_price = round(price * (1 - tp_pct), price_dp)
                sl_price = round(price * (1 + sl_pct), price_dp)

        if side == "SELL":
            # Sell exactly what we hold (or notional/price, whichever is smaller).
            # FLOOR to 6dp — round() can bump qty above held_qty by sub-LSB
            # fractions (e.g. round(0.002550607, 6) = 0.002551) and Alpaca
            # rejects with "insufficient balance".
            raw = min(held_qty, notional / price) if notional > 0 else held_qty
            qty = math.floor(raw * 1_000_000) / 1_000_000
        else:
            qty = round(trade_notional / price, 6)
        if qty <= 0:
            self._log(f"{symbol}: qty rounds to 0, skipping", tag="ERROR")
            return

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except Exception as e:
            self._log(f"alpaca enums import failed: {e}", tag="ERROR")
            return

        # Alpaca crypto does NOT support BRACKET/OTOCO order classes (API code 42210000).
        # We submit a simple market order for entry; PositionMonitor watches the open
        # position and fires a market exit when tp_price/sl_price is hit.
        # Stocks: TimeInForce.DAY is required for fractional/non-RTH safety.
        tif = TimeInForce.DAY if self.asset_class == "stock" else TimeInForce.GTC
        try:
            req_kwargs = dict(
                symbol=symbol,
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=tif,
            )
            # Stocks BUY: use notional dollar amount so fractional shares
            # work for high-priced names (NVDA, MSFT). SELL closes whole qty.
            if self.asset_class == "stock" and side == "BUY":
                req_kwargs["notional"] = round(trade_notional, 2)
            else:
                req_kwargs["qty"] = qty
            req = MarketOrderRequest(**req_kwargs)
            order = client.submit_order(order_data=req)
        except Exception as e:
            self._log(f"{symbol} {side} submit failed: {e}", tag="ERROR")
            return

        order_id = getattr(order, "id", None) or getattr(order, "client_order_id", "")
        order_status = str(getattr(order, "status", "SUBMITTED")).split(".")[-1].upper()

        try:
            self.db.insert_order(
                str(order_id), symbol, side, qty, price,
                status=order_status, pnl=0.0,
                tp_price=tp_price, sl_price=sl_price,
                asset_class=self.asset_class,
            )
        except Exception as e:
            self._log(f"DB order insert failed: {e}", tag="ERROR")

        atr_tag = f"  ATR={atr_v:.4f}" if atr_v > 0 else "  ATR=n/a"
        self._log(
            f"{side} {symbol} qty={qty:g} @ ~${price:.4f} "
            f"(${trade_notional:.0f}, budget left ${budget_left - trade_notional:.0f}/"
            f"${max_capital:.0f})  "
            f"TP=${tp_price:.4f}  SL=${sl_price:.4f}{atr_tag}"
        )

    def _current_exposure(self, client) -> float:
        """Sum of |market_value| across open positions of THIS asset class.

        Crypto positions report `asset_class == 'crypto'`; equities report
        `'us_equity'`. We need to gate budget per class so a $1000 crypto
        cap doesn't get burned by a parallel stocks book.
        """
        try:
            positions = client.get_all_positions()
        except Exception as e:
            self._log(f"get_all_positions failed: {e}", tag="ERROR")
            return 0.0
        total = 0.0
        for p in positions or []:
            try:
                if not _matches_asset_class(p, self.asset_class):
                    continue
                total += abs(float(getattr(p, "market_value", 0) or 0))
            except Exception:
                continue
        return total

    def _held_qty(self, client, symbol: str) -> float:
        """Return the qty we currently hold of `symbol` (0 if none).

        Alpaca returns crypto position symbols WITHOUT a slash (e.g. "BTCUSD")
        while our DB and decision symbols use "BTC/USD".  Match either form.
        """
        try:
            positions = client.get_all_positions()
        except Exception as e:
            self._log(f"get_all_positions failed: {e}", tag="ERROR")
            return 0.0
        no_slash = symbol.replace("/", "")
        for p in positions or []:
            if not _matches_asset_class(p, self.asset_class):
                continue
            psym = (getattr(p, "symbol", "") or "").upper()
            if psym in (symbol.upper(), no_slash):
                try:
                    side = str(getattr(p, "side", "")).split(".")[-1].lower()
                    qty = abs(float(getattr(p, "qty", 0) or 0))
                    # Only counts longs — we can't reduce a short with SELL anyway
                    return qty if side == "long" else 0.0
                except Exception:
                    return 0.0
        return 0.0


def _matches_asset_class(pos, asset_class: str) -> bool:
    """Map our internal 'crypto'/'stock' to Alpaca's position.asset_class.

    Alpaca returns enum-like 'us_equity' for stocks, 'crypto' for crypto.
    """
    raw = str(getattr(pos, "asset_class", "")).split(".")[-1].lower()
    if asset_class == "crypto":
        return raw == "crypto"
    return raw in ("us_equity", "stock", "equity")
