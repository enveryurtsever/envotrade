"""Thread-safe PostgreSQL access layer."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

import config

log = logging.getLogger(__name__)


class DBManager:
    """Wraps a ThreadedConnectionPool with project-specific helpers."""

    def __init__(self, minconn: int = 2, maxconn: int = 10) -> None:
        self._lock = threading.Lock()
        self._pool: ThreadedConnectionPool | None = None
        self._minconn = minconn
        self._maxconn = maxconn

    # ---------- Pool lifecycle --------------------------------
    def connect(self) -> None:
        with self._lock:
            if self._pool is not None:
                return
            self._pool = ThreadedConnectionPool(
                self._minconn,
                self._maxconn,
                host=config.DB_HOST,
                port=config.DB_PORT,
                dbname=config.DB_NAME,
                user=config.DB_USER,
                password=config.DB_PASS,
                connect_timeout=10,
            )
            log.info("DB pool ready: %s@%s/%s", config.DB_USER, config.DB_HOST, config.DB_NAME)

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    @contextmanager
    def cursor(self, *, dict_rows: bool = False) -> Iterator[Any]:
        if self._pool is None:
            self.connect()
        assert self._pool is not None
        conn = self._pool.getconn()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if dict_rows else conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            self._pool.putconn(conn)

    # ---------- Schema ----------------------------------------
    def init_schema(self) -> None:
        self.connect()
        with open(config.SCHEMA_FILE, "r", encoding="utf-8") as f:
            sql = f.read()
        with self.cursor() as cur:
            cur.execute(sql)
        log.info("DB schema initialized.")

    # ---------- Inserts ---------------------------------------
    def insert_market_tick(
        self, symbol: str, price: float, volume: float | None,
        ts: datetime | None = None, asset_class: str = "crypto",
    ) -> None:
        ts = ts or datetime.now(timezone.utc)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO market_data (timestamp, symbol, price, volume, asset_class)
                   VALUES (%s, %s, %s, %s, %s)""",
                (ts, symbol, price, volume, asset_class),
            )

    def insert_sentiment(
        self, source: str, symbol: str, score: float, summary: str
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO market_sentiment (source, symbol, sentiment_score, summary)
                   VALUES (%s, %s, %s, %s)""",
                (source, symbol, score, summary),
            )

    def insert_signal(
        self, symbol: str, rsi: float, decision: str, confidence: float, raw: str,
        asset_class: str = "crypto",
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO trading_signals
                       (symbol, rsi, ai_decision, ai_confidence, raw_ai_output, asset_class)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (symbol, rsi, decision, confidence, raw, asset_class),
            )

    def insert_order(
        self,
        alpaca_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        status: str = "FILLED",
        pnl: float = 0.0,
        tp_price: float | None = None,
        sl_price: float | None = None,
        asset_class: str = "crypto",
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO orders (alpaca_order_id, symbol, side, qty, price,
                                       tp_price, sl_price, status, pnl, asset_class)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (alpaca_order_id) DO UPDATE
                     SET status=EXCLUDED.status, pnl=EXCLUDED.pnl,
                         tp_price=COALESCE(EXCLUDED.tp_price, orders.tp_price),
                         sl_price=COALESCE(EXCLUDED.sl_price, orders.sl_price)""",
                (alpaca_order_id, symbol, side, qty, price,
                 tp_price, sl_price, status, pnl, asset_class),
            )

    def get_open_entries(self, asset_class: str | None = None) -> list[dict]:
        """All orders that are still considered open (haven't been realized).

        Used by PositionSync to compare against the broker's live positions.
        Pass `asset_class` to restrict to one class — coordinators must filter
        so the crypto sync doesn't close stocks rows and vice versa.
        """
        with self.cursor(dict_rows=True) as cur:
            if asset_class is None:
                cur.execute(
                    """SELECT alpaca_order_id, timestamp, symbol, side, qty,
                              price, tp_price, sl_price, status, asset_class
                       FROM orders
                       WHERE status IN ('FILLED', 'SUBMITTED', 'NEW', 'PARTIALLY_FILLED')
                         AND COALESCE(pnl, 0) = 0
                       ORDER BY timestamp DESC"""
                )
            else:
                cur.execute(
                    """SELECT alpaca_order_id, timestamp, symbol, side, qty,
                              price, tp_price, sl_price, status, asset_class
                       FROM orders
                       WHERE status IN ('FILLED', 'SUBMITTED', 'NEW', 'PARTIALLY_FILLED')
                         AND COALESCE(pnl, 0) = 0
                         AND asset_class = %s
                       ORDER BY timestamp DESC""",
                    (asset_class,),
                )
            return list(cur.fetchall())

    def get_latest_open_entry(
        self, symbol: str, side: str, asset_class: str | None = None,
    ) -> dict | None:
        """Find the most recent FILLED entry order for (symbol, side) that
        hasn't been closed yet. Used by PositionMonitor to find TP/SL targets."""
        with self.cursor(dict_rows=True) as cur:
            if asset_class is None:
                cur.execute(
                    """SELECT alpaca_order_id, timestamp, symbol, side, qty,
                              price, tp_price, sl_price, status, asset_class
                       FROM orders
                       WHERE symbol=%s AND side=%s
                         AND status IN ('FILLED', 'SUBMITTED', 'NEW', 'PARTIALLY_FILLED')
                         AND pnl = 0.0
                       ORDER BY timestamp DESC
                       LIMIT 1""",
                    (symbol, side),
                )
            else:
                cur.execute(
                    """SELECT alpaca_order_id, timestamp, symbol, side, qty,
                              price, tp_price, sl_price, status, asset_class
                       FROM orders
                       WHERE symbol=%s AND side=%s AND asset_class=%s
                         AND status IN ('FILLED', 'SUBMITTED', 'NEW', 'PARTIALLY_FILLED')
                         AND pnl = 0.0
                       ORDER BY timestamp DESC
                       LIMIT 1""",
                    (symbol, side, asset_class),
                )
            return cur.fetchone()

    def update_order_status(self, alpaca_order_id: str, status: str, pnl: float,
                            commission_paid: float | None = None) -> None:
        with self.cursor() as cur:
            if commission_paid is None:
                cur.execute(
                    "UPDATE orders SET status=%s, pnl=%s WHERE alpaca_order_id=%s",
                    (status, pnl, alpaca_order_id),
                )
            else:
                cur.execute(
                    "UPDATE orders SET status=%s, pnl=%s, commission_paid=%s "
                    "WHERE alpaca_order_id=%s",
                    (status, pnl, commission_paid, alpaca_order_id),
                )

    def get_total_commission(self, asset_class: str | None = None) -> float:
        with self.cursor() as cur:
            if asset_class is None:
                cur.execute("SELECT COALESCE(SUM(commission_paid), 0) FROM orders")
            else:
                cur.execute(
                    "SELECT COALESCE(SUM(commission_paid), 0) FROM orders WHERE asset_class=%s",
                    (asset_class,),
                )
            return float(cur.fetchone()[0] or 0)

    # ---------- Settings --------------------------------------
    def get_setting(self, key: str, default: float | None = None) -> float | None:
        with self.cursor() as cur:
            cur.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        return float(row[0])

    def update_setting(self, key: str, value: float, description: str | None = None) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO bot_settings (key, value, description, updated_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (key) DO UPDATE
                     SET value=EXCLUDED.value, updated_at=NOW(),
                         description=COALESCE(EXCLUDED.description, bot_settings.description)""",
                (key, value, description),
            )

    # ---------- Queries for UI / brain ------------------------
    def get_recent_bars(
        self, symbol: str, n: int = 120, asset_class: str | None = None,
        *, since: datetime | None = None, until: datetime | None = None,
    ) -> list[dict]:
        """Most-recent bars, oldest-first.

        Default behaviour (live decide()): last `n` rows, no time filter.
        Optional `since`/`until` are inclusive bounds for the backtest
        harness, which sweeps a date range rather than a fixed count.
        When a time range is supplied, `n` becomes an upper safety cap
        and is raised accordingly by the caller.
        """
        clauses = ["symbol=%s"]
        params: list[Any] = [symbol]
        if asset_class is not None:
            clauses.append("asset_class=%s")
            params.append(asset_class)
        if since is not None:
            clauses.append("timestamp >= %s")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= %s")
            params.append(until)
        where = " AND ".join(clauses)
        params.append(n)
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""SELECT timestamp, price AS close, volume
                    FROM market_data
                    WHERE {where}
                    ORDER BY timestamp DESC
                    LIMIT %s""",
                tuple(params),
            )
            rows = cur.fetchall()
        rows.reverse()
        return rows

    def get_last_100_orders(self) -> list[dict]:
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT alpaca_order_id, timestamp, symbol, side, qty, price, pnl, status
                   FROM orders
                   ORDER BY timestamp DESC
                   LIMIT 100"""
            )
            return list(cur.fetchall())

    def get_recent_trades_with_live_pnl(
        self, limit: int = 40, asset_class: str | None = None,
    ) -> list[dict]:
        """Recent orders enriched with current price + unrealized PnL when open.

        For closed positions, `pnl` is the realized P&L written by the
        PositionMonitor. For open positions (status SUBMITTED/FILLED/NEW and
        realized pnl == 0), we look up the most recent market_data tick for
        the same symbol and compute `live_pnl = (cur - entry) * qty * dir`.
        """
        with self.cursor(dict_rows=True) as cur:
            if asset_class is None:
                cur.execute(
                    """SELECT o.alpaca_order_id, o.timestamp, o.symbol, o.side,
                              o.qty, o.price AS entry_price, o.tp_price, o.sl_price,
                              o.pnl, o.status, o.asset_class,
                              (SELECT md.price FROM market_data md
                               WHERE md.symbol = o.symbol
                               ORDER BY md.timestamp DESC LIMIT 1) AS current_price
                       FROM orders o
                       ORDER BY o.timestamp DESC
                       LIMIT %s""",
                    (limit,),
                )
            else:
                cur.execute(
                    """SELECT o.alpaca_order_id, o.timestamp, o.symbol, o.side,
                              o.qty, o.price AS entry_price, o.tp_price, o.sl_price,
                              o.pnl, o.status, o.asset_class,
                              (SELECT md.price FROM market_data md
                               WHERE md.symbol = o.symbol AND md.asset_class = o.asset_class
                               ORDER BY md.timestamp DESC LIMIT 1) AS current_price
                       FROM orders o
                       WHERE o.asset_class = %s
                       ORDER BY o.timestamp DESC
                       LIMIT %s""",
                    (asset_class, limit),
                )
            rows = list(cur.fetchall())

        for r in rows:
            entry = float(r.get("entry_price") or 0)
            qty = float(r.get("qty") or 0)
            side = (r.get("side") or "").upper()
            cur_px = r.get("current_price")
            realized = float(r.get("pnl") or 0)
            status = (r.get("status") or "").upper()
            # EXIT_TP / EXIT_SL are synthetic close-rows — already closed,
            # carry pnl=0 so they don't double-count.
            is_open = (realized == 0.0
                       and status not in ("STOPPED", "CANCELED", "REJECTED")
                       and not status.startswith("EXIT_"))
            if is_open and cur_px is not None and entry > 0:
                direction = 1 if side == "BUY" else -1
                r["live_pnl"] = (float(cur_px) - entry) * qty * direction
                r["is_open"] = True
            else:
                r["live_pnl"] = realized
                r["is_open"] = False
            r["current_price"] = float(cur_px) if cur_px is not None else None
        return rows

    def get_recent_sentiment(self, symbol: str, hours: int = 1) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT source, sentiment_score, summary, timestamp
                   FROM market_sentiment
                   WHERE symbol=%s AND timestamp >= %s
                   ORDER BY timestamp DESC
                   LIMIT 30""",
                (symbol, since),
            )
            return list(cur.fetchall())

    def get_budget_summary(self) -> dict:
        """Performance against the trading capital cap.

        Returns:
          - max_capital:   the configured cap (USD)
          - exposure:      sum(entry_price * qty) for OPEN long positions
          - realized_pnl:  sum(pnl) over closed trades
          - unrealized_pnl: live mark-to-market on open positions using
                           the most recent market_data tick per symbol
          - total_pnl:     realized + unrealized
          - return_pct:    total_pnl / max_capital * 100
        """
        max_cap = float(self.get_setting("max_trading_capital", 1000.0) or 1000.0)
        rows = self.get_recent_trades_with_live_pnl(limit=500)
        realized = sum(
            float(r.get("pnl") or 0)
            for r in rows if not r.get("is_open")
        )
        unrealized = sum(
            float(r.get("live_pnl") or 0)
            for r in rows if r.get("is_open")
        )
        exposure = sum(
            abs(float(r.get("entry_price") or 0) * float(r.get("qty") or 0))
            for r in rows if r.get("is_open")
        )
        total = realized + unrealized
        pct = (total / max_cap * 100.0) if max_cap > 0 else 0.0
        return {
            "max_capital":    max_cap,
            "exposure":       exposure,
            "realized_pnl":   realized,
            "unrealized_pnl": unrealized,
            "total_pnl":      total,
            "return_pct":     pct,
        }

    def get_trade_stats(self, asset_class: str | None = None) -> dict:
        with self.cursor(dict_rows=True) as cur:
            if asset_class is None:
                cur.execute(
                    """SELECT
                         COUNT(*) AS total,
                         COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                         COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                         COALESCE(SUM(pnl), 0) AS total_pnl,
                         COALESCE(SUM(CASE WHEN DATE(timestamp) = CURRENT_DATE THEN pnl ELSE 0 END), 0) AS today_pnl
                       FROM orders"""
                )
            else:
                cur.execute(
                    """SELECT
                         COUNT(*) AS total,
                         COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                         COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                         COALESCE(SUM(pnl), 0) AS total_pnl,
                         COALESCE(SUM(CASE WHEN DATE(timestamp) = CURRENT_DATE THEN pnl ELSE 0 END), 0) AS today_pnl
                       FROM orders
                       WHERE asset_class = %s""",
                    (asset_class,),
                )
            row = cur.fetchone() or {}
        total = int(row.get("total") or 0)
        wins = int(row.get("wins") or 0)
        return {
            "total": total,
            "wins": wins,
            "losses": int(row.get("losses") or 0),
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "total_pnl": float(row.get("total_pnl") or 0),
            "today_pnl": float(row.get("today_pnl") or 0),
        }

    # ---------- Sentiment live feed ---------------------------
    def get_recent_sentiment_feed(self, limit: int = 20) -> list[dict]:
        """Latest sentiment entries across all symbols, newest first."""
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT timestamp, source, symbol, sentiment_score, summary
                   FROM market_sentiment
                   ORDER BY timestamp DESC
                   LIMIT %s""",
                (limit,),
            )
            return list(cur.fetchall())

    # ---------- Watchlist live prices -------------------------
    def get_watchlist_snapshot(
        self, symbols: list[str], history_bars: int = 30,
        asset_class: str | None = None,
    ) -> list[dict]:
        """For each symbol: last_price, baseline_price (~60min ago) for change%,
        and a list of last N closes for a sparkline.
        Result is in the order of `symbols` (missing symbols → price=None).
        """
        if not symbols:
            return []
        out: list[dict] = []
        with self.cursor(dict_rows=True) as cur:
            for s in symbols:
                if asset_class is None:
                    cur.execute(
                        """SELECT timestamp, price
                           FROM market_data
                           WHERE symbol=%s
                           ORDER BY timestamp DESC
                           LIMIT %s""",
                        (s, history_bars),
                    )
                else:
                    cur.execute(
                        """SELECT timestamp, price
                           FROM market_data
                           WHERE symbol=%s AND asset_class=%s
                           ORDER BY timestamp DESC
                           LIMIT %s""",
                        (s, asset_class, history_bars),
                    )
                rows = list(cur.fetchall())
                if not rows:
                    out.append({"symbol": s, "price": None, "change_pct": None,
                                "history": []})
                    continue
                last_price = float(rows[0]["price"])
                # Use the oldest bar in our window as the change baseline.
                # With 1-min bars and history_bars=30 → ~30-min change.
                baseline = float(rows[-1]["price"]) if len(rows) > 1 else last_price
                change_pct = (
                    (last_price - baseline) / baseline * 100.0
                    if baseline else 0.0
                )
                history = [float(r["price"]) for r in reversed(rows)]
                out.append({
                    "symbol": s,
                    "price": last_price,
                    "change_pct": change_pct,
                    "history": history,
                    "last_ts": rows[0]["timestamp"],
                })
        return out

    # ---------- Trade audit (for learning loop) ---------------
    def get_orders_in_range(self, start: datetime, end: datetime) -> list[dict]:
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT alpaca_order_id, timestamp, symbol, side, qty, price, pnl, status
                   FROM orders
                   WHERE timestamp >= %s AND timestamp < %s
                   ORDER BY timestamp ASC""",
                (start, end),
            )
            return list(cur.fetchall())

    def get_signal_for_order(
        self, symbol: str, ts: datetime | None, asset_class: str | None = None,
        window_min: int = 15,
    ) -> dict | None:
        """Return the trading_signals row closest in time to `ts` for a
        given (symbol, asset_class). Used by the reasoning modal to look
        up which AI verdict produced a given order. Falls back to the
        most-recent signal for the symbol if no row sits inside the
        window — that still tells the user *why* the AI is leaning a
        certain way."""
        with self.cursor(dict_rows=True) as cur:
            if ts is not None:
                lo = ts - timedelta(minutes=window_min)
                hi = ts + timedelta(minutes=window_min)
                if asset_class is None:
                    cur.execute(
                        """SELECT timestamp, symbol, rsi, ai_decision,
                                  ai_confidence, raw_ai_output
                           FROM trading_signals
                           WHERE symbol=%s AND timestamp BETWEEN %s AND %s
                           ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s)))
                           LIMIT 1""",
                        (symbol, lo, hi, ts),
                    )
                else:
                    cur.execute(
                        """SELECT timestamp, symbol, rsi, ai_decision,
                                  ai_confidence, raw_ai_output
                           FROM trading_signals
                           WHERE symbol=%s AND asset_class=%s
                             AND timestamp BETWEEN %s AND %s
                           ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s)))
                           LIMIT 1""",
                        (symbol, asset_class, lo, hi, ts),
                    )
                row = cur.fetchone()
                if row is not None:
                    return row
            # Fallback: most-recent signal for the symbol regardless of window
            if asset_class is None:
                cur.execute(
                    """SELECT timestamp, symbol, rsi, ai_decision,
                              ai_confidence, raw_ai_output
                       FROM trading_signals
                       WHERE symbol=%s
                       ORDER BY timestamp DESC LIMIT 1""",
                    (symbol,),
                )
            else:
                cur.execute(
                    """SELECT timestamp, symbol, rsi, ai_decision,
                              ai_confidence, raw_ai_output
                       FROM trading_signals
                       WHERE symbol=%s AND asset_class=%s
                       ORDER BY timestamp DESC LIMIT 1""",
                    (symbol, asset_class),
                )
            return cur.fetchone()

    def get_signals_near(self, symbol: str, ts: datetime,
                         window_min: int = 5) -> list[dict]:
        """Return signals fired within ±window_min minutes of the given ts."""
        if ts is None:
            return []
        lo = ts - timedelta(minutes=window_min)
        hi = ts + timedelta(minutes=window_min)
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT timestamp, symbol, rsi, ai_decision, ai_confidence, raw_ai_output
                   FROM trading_signals
                   WHERE symbol=%s AND timestamp >= %s AND timestamp <= %s
                   ORDER BY timestamp ASC""",
                (symbol, lo, hi),
            )
            return list(cur.fetchall())

    def get_sentiment_near(self, symbol: str, ts: datetime,
                           window_min: int = 60) -> list[dict]:
        if ts is None:
            return []
        lo = ts - timedelta(minutes=window_min)
        hi = ts + timedelta(minutes=window_min)
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT timestamp, source, sentiment_score, summary
                   FROM market_sentiment
                   WHERE symbol=%s AND timestamp >= %s AND timestamp <= %s
                   ORDER BY timestamp ASC""",
                (symbol, lo, hi),
            )
            return list(cur.fetchall())

    # ---------- Per-trade loss lessons ------------------------
    def insert_loss_lesson(
        self,
        *,
        symbol: str,
        side: str,
        entry_ts: datetime | None,
        exit_ts: datetime | None,
        entry_price: float,
        exit_price: float,
        qty: float,
        pnl: float,
        rsi_at_entry: float | None,
        avg_sentiment_at_entry: float | None,
        sentiment_sources_at_entry: int | None,
        rule_learned: str,
        diagnosis: str,
        context_summary: dict,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO loss_lessons
                       (symbol, side, entry_ts, exit_ts,
                        entry_price, exit_price, qty, pnl,
                        rsi_at_entry, avg_sentiment_at_entry, sentiment_sources_at_entry,
                        rule_learned, diagnosis, context_summary)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s)""",
                (
                    symbol, side, entry_ts, exit_ts,
                    entry_price, exit_price, qty, pnl,
                    rsi_at_entry, avg_sentiment_at_entry, sentiment_sources_at_entry,
                    rule_learned, diagnosis, Json(context_summary),
                ),
            )

    def get_recent_loss_lessons(
        self, symbol: str | None = None, n: int = 5
    ) -> list[dict]:
        """Last N loss lessons globally, or for a specific symbol."""
        with self.cursor(dict_rows=True) as cur:
            if symbol:
                cur.execute(
                    """SELECT created_at, symbol, side, pnl,
                              rsi_at_entry, avg_sentiment_at_entry,
                              sentiment_sources_at_entry,
                              rule_learned, diagnosis
                       FROM loss_lessons
                       WHERE symbol=%s
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (symbol, n),
                )
            else:
                cur.execute(
                    """SELECT created_at, symbol, side, pnl,
                              rsi_at_entry, avg_sentiment_at_entry,
                              sentiment_sources_at_entry,
                              rule_learned, diagnosis
                       FROM loss_lessons
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (n,),
                )
            return list(cur.fetchall())

    # ---------- Learning journal ------------------------------
    def insert_journal_entry(
        self,
        journal_date: date,
        total_trades: int,
        wins: int,
        losses: int,
        total_pnl: float,
        win_rate: float,
        period_summary: dict,
        insights: str,
        rules_learned: list[str],
        parameter_changes: dict,
        confidence_calibration: float,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO learning_journal
                       (journal_date, total_trades, wins, losses, total_pnl, win_rate,
                        period_summary, insights, rules_learned,
                        parameter_changes, confidence_calibration)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (journal_date) DO UPDATE
                     SET total_trades=EXCLUDED.total_trades,
                         wins=EXCLUDED.wins,
                         losses=EXCLUDED.losses,
                         total_pnl=EXCLUDED.total_pnl,
                         win_rate=EXCLUDED.win_rate,
                         period_summary=EXCLUDED.period_summary,
                         insights=EXCLUDED.insights,
                         rules_learned=EXCLUDED.rules_learned,
                         parameter_changes=EXCLUDED.parameter_changes,
                         confidence_calibration=EXCLUDED.confidence_calibration,
                         created_at=NOW()""",
                (
                    journal_date, total_trades, wins, losses,
                    total_pnl, win_rate,
                    Json(period_summary), insights, rules_learned,
                    Json(parameter_changes), confidence_calibration,
                ),
            )

    def get_recent_journal_entries(self, n: int = 5) -> list[dict]:
        with self.cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT journal_date, total_trades, wins, losses,
                          total_pnl, win_rate, insights, rules_learned,
                          parameter_changes, confidence_calibration
                   FROM learning_journal
                   ORDER BY journal_date DESC
                   LIMIT %s""",
                (n,),
            )
            return list(cur.fetchall())

    def has_journal_for_date(self, d: date) -> bool:
        with self.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM learning_journal WHERE journal_date=%s",
                (d,),
            )
            return cur.fetchone() is not None

    def get_equity_curve(
        self, period: str = "daily", asset_class: str | None = None,
    ) -> tuple[list[datetime], list[float]]:
        """Return cumulative pnl curve aggregated by hour/day/week."""
        bucket = {"hourly": "hour", "daily": "day", "weekly": "week"}.get(
            period.lower(), "day"
        )
        with self.cursor() as cur:
            if asset_class is None:
                cur.execute(
                    f"""SELECT date_trunc('{bucket}', timestamp) AS bucket,
                              SUM(pnl) AS bucket_pnl
                       FROM orders
                       GROUP BY bucket
                       ORDER BY bucket ASC"""
                )
            else:
                cur.execute(
                    f"""SELECT date_trunc('{bucket}', timestamp) AS bucket,
                              SUM(pnl) AS bucket_pnl
                       FROM orders
                       WHERE asset_class=%s
                       GROUP BY bucket
                       ORDER BY bucket ASC""",
                    (asset_class,),
                )
            rows = cur.fetchall()
        timestamps: list[datetime] = []
        cumulative: list[float] = []
        running = 0.0
        for ts, pnl in rows:
            running += float(pnl or 0)
            timestamps.append(ts)
            cumulative.append(running)
        return timestamps, cumulative
