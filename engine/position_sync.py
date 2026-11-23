"""Reconcile Alpaca positions with our local `orders` table.

The bot writes to DB and submits to Alpaca, but the two stores can drift:
- Manual close in the Alpaca dashboard  →  DB still thinks position is open.
- Order submitted but DB insert failed  →  Alpaca-only orphan, untracked here.
- Bot stopped mid-flight                →  next start sees positions it never opened.

This worker walks both sides every 60 seconds and resolves the drift:
- ADOPT:  Alpaca position with no matching DB row → insert a synthetic
          orders row using the current TP/SL settings (so PositionMonitor
          starts managing it on the next tick).
- CLOSE:  Open DB row with no matching Alpaca position → mark as
          CLOSED_EXTERNAL with a best-effort realized PnL.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import config


class PositionSync(threading.Thread):
    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
        interval: int = 60,
        asset_class: str = "crypto",
    ) -> None:
        super().__init__(name=f"PositionSync[{asset_class}]", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.interval = interval
        self.asset_class = asset_class
        self._client = None

    def _log(self, text: str, tag: str = "ORDER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not config.ALPACA_KEY:
            return None
        try:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET, paper=config.ALPACA_PAPER,
            )
            return self._client
        except Exception as e:
            self._log(f"position_sync: client init failed: {e}", tag="ERROR")
            return None

    # ------------------------------------------------------------------
    def run(self) -> None:
        # Initial sync — runs once shortly after start (give StreamWorker a head start)
        self.stop_event.wait(2.0)
        while not self.stop_event.is_set():
            try:
                self.sync_once()
            except Exception as e:
                self._log(f"position_sync error: {e}", tag="ERROR")
            self.stop_event.wait(self.interval)

    # ------------------------------------------------------------------
    def sync_once(self) -> dict:
        """Returns counters: {'adopted': N, 'closed_external': N}."""
        client = self._ensure_client()
        if client is None:
            return {"adopted": 0, "closed_external": 0}

        try:
            alpaca_positions = client.get_all_positions() or []
        except Exception as e:
            self._log(f"get_all_positions failed: {e}", tag="ERROR")
            return {"adopted": 0, "closed_external": 0}

        # Build key sets. Alpaca crypto returns "BTCUSD"; our DB uses "BTC/USD".
        # Filter to THIS asset class so two coordinators don't fight each other
        # (crypto sync would otherwise close stocks DB rows and vice versa).
        alpaca_by_key: dict[tuple[str, str], object] = {}
        for p in alpaca_positions:
            if not _pos_matches_class(p, self.asset_class):
                continue
            raw_sym = getattr(p, "symbol", "") or ""
            sym = raw_sym.upper() if self.asset_class == "stock" else _to_db_symbol(raw_sym)
            side_raw = str(getattr(p, "side", "")).split(".")[-1].lower()
            side = "BUY" if side_raw == "long" else "SELL"
            alpaca_by_key[(sym, side)] = p

        try:
            db_entries = self.db.get_open_entries(asset_class=self.asset_class) or []
        except Exception as e:
            self._log(f"get_open_entries failed: {e}", tag="ERROR")
            return {"adopted": 0, "closed_external": 0}

        db_by_key: dict[tuple[str, str], dict] = {}
        for e in db_entries:
            db_by_key[(e["symbol"], (e["side"] or "").upper())] = e

        adopted = 0
        closed = 0

        # --- 1) ADOPT Alpaca orphans -----------------------------------
        for key, pos in alpaca_by_key.items():
            if key in db_by_key:
                continue
            if self._adopt(pos, key):
                adopted += 1

        # --- 2) CLOSE DB orphans (closed externally) -------------------
        for key, entry in db_by_key.items():
            if key in alpaca_by_key:
                continue
            if self._mark_externally_closed(entry):
                closed += 1

        if adopted or closed:
            self._log(
                f"position_sync: {adopted} adopted, {closed} closed_external "
                f"(alpaca={len(alpaca_by_key)}, db_open={len(db_by_key)})",
            )
        return {"adopted": adopted, "closed_external": closed}

    # ------------------------------------------------------------------
    def _adopt(self, pos, key: tuple[str, str]) -> bool:
        symbol, side = key
        try:
            entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)
            qty = abs(float(getattr(pos, "qty", 0) or 0))
        except Exception:
            return False
        if entry_price <= 0 or qty <= 0:
            return False

        sl_pct = float(self.db.get_setting("stop_loss_percent", 0.0035) or 0.0035)
        tp_pct = float(self.db.get_setting("take_profit_percent", 0.006) or 0.006)
        if side == "BUY":
            tp = entry_price * (1 + tp_pct)
            sl = entry_price * (1 - sl_pct)
        else:
            tp = entry_price * (1 - tp_pct)
            sl = entry_price * (1 + sl_pct)

        # Synthetic order id — won't collide with real Alpaca uuids
        synthetic_id = f"adopted-{symbol.replace('/', '')}-{int(time.time())}"
        try:
            self.db.insert_order(
                synthetic_id, symbol, side, qty, entry_price,
                status="FILLED", tp_price=tp, sl_price=sl,
                asset_class=self.asset_class,
            )
        except Exception as e:
            self._log(f"adopt {symbol} insert failed: {e}", tag="ERROR")
            return False

        self._log(
            f"ADOPTED {symbol} {side} qty={qty:g} @ ${_fmt(entry_price)}  "
            f"synthetic TP=${_fmt(tp)}  SL=${_fmt(sl)}",
        )
        return True

    # ------------------------------------------------------------------
    def _mark_externally_closed(self, entry: dict) -> bool:
        order_id = entry.get("alpaca_order_id")
        if not order_id:
            return False
        symbol = entry["symbol"]
        side = (entry.get("side") or "").upper()
        entry_price = float(entry.get("price") or 0)
        qty = float(entry.get("qty") or 0)

        # Best-effort exit price = latest tick in market_data
        try:
            recent = self.db.get_recent_bars(
                symbol, n=1, asset_class=self.asset_class,
            )
        except Exception:
            recent = []
        exit_price = (
            float(recent[0]["close"]) if recent else entry_price
        )

        direction = 1 if side == "BUY" else -1
        pnl = (exit_price - entry_price) * qty * direction

        try:
            self.db.update_order_status(order_id, "CLOSED_EXTERNAL", pnl)
        except Exception as e:
            self._log(f"close_external {order_id} failed: {e}", tag="ERROR")
            return False

        sign = "+" if pnl >= 0 else ""
        self._log(
            f"CLOSED_EXTERNAL {symbol} (not on Alpaca anymore) "
            f"qty={qty:g} entry=${_fmt(entry_price)} exit=${_fmt(exit_price)} "
            f"realized={sign}${pnl:.2f}",
        )
        return True


# ====================================================================
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


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.6f}"
