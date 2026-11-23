"""Backtest harness for the crypto math strategy.

Replays `market_data` 1m bars through the SAME indicator stack the live bot
uses (`brain.indicators`) and the SAME entry logic (`brain.ai_analyst.decide`
math branch, no LLM, no sentiment), simulating ATR-bracketed exits the way
`engine.position_monitor` evaluates them (close-only checks, no intrabar
fills). Output: per-config trade count, WR, PnL, max DD, plus a final
go/no-go gate evaluation.

Why no LLM/sentiment: we want to measure raw strategy edge, not Claude's
news-veto skill. If the math has no edge, adding an LLM gate can't fix it.

Usage
-----
    # Fetch fresh history from Alpaca and run baseline backtest
    python -m tools.backtest --fetch-history --days 60 \
        --symbols BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD

    # Sweep a grid (default symbols + range)
    python -m tools.backtest --grid \
        rsi_buy=25,28,30,32,35 atr_sl=1.0,1.5,2.0 atr_tp=2.0,3.0,4.0

    # Backtest just BTC over a specific window
    python -m tools.backtest --symbols BTC/USD \
        --since 2026-04-15 --until 2026-05-17
"""
from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np
import pandas as pd

import config
from brain import indicators, stocks_strategy
from database.db_manager import DBManager


DEFAULT_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD"]
DEFAULT_STOCKS_SYMBOLS = config.STOCKS_LIQUID_MAJORS
DEFAULT_NOTIONAL = 100.0
DEFAULT_COMMISSION_PCT = 0.0015   # matches engine/position_monitor.py default
DEFAULT_STOCKS_COMMISSION_PCT = 0.0   # Alpaca stocks: 0% commission


# ====================================================================
#  Decision gate (lifted from plan; the "is there edge" line in the sand)
# ====================================================================
GATE_MIN_TRADES = 150
GATE_MIN_WR_PCT = 38.0
GATE_REQUIRE_POSITIVE_PNL = True
GATE_MAX_DD_OVER_WORST_LOSS = 3.0   # max DD must be < 3 × worst single loss


# ====================================================================
#  Data: load from DB, optionally backfill from Alpaca historical API
# ====================================================================
def fetch_history_from_alpaca(
    db: DBManager,
    symbols: list[str],
    days: int,
    asset_class: str = "crypto",
) -> dict[str, int]:
    """Backfill `market_data` from Alpaca historical bars.

    Crypto: public, no auth required.
    Stocks: free tier uses IEX feed (partial volume but enough for backtest);
    requires ALPACA_KEY/SECRET from config.

    Returns {symbol: rows_inserted}.
    """
    try:
        from alpaca.data.timeframe import TimeFrame
        if asset_class == "crypto":
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            client = CryptoHistoricalDataClient()
        else:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.enums import DataFeed
            client = StockHistoricalDataClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
            )
    except Exception as e:
        print(f"alpaca SDK not importable: {e}", file=sys.stderr)
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    inserted: dict[str, int] = {}

    for sym in symbols:
        if asset_class == "crypto":
            req = CryptoBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=start, end=end,
            )
            try:
                bars = client.get_crypto_bars(req)
            except Exception as e:
                print(f"  {sym}: fetch failed — {e}", file=sys.stderr)
                inserted[sym] = 0
                continue
        else:
            req = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=start, end=end,
                feed=DataFeed.IEX,   # free tier
            )
            try:
                bars = client.get_stock_bars(req)
            except Exception as e:
                print(f"  {sym}: fetch failed — {e}", file=sys.stderr)
                inserted[sym] = 0
                continue

        rows = []
        for bar in bars.data.get(sym, []):
            rows.append((bar.timestamp, sym, float(bar.close),
                         float(getattr(bar, "volume", 0) or 0)))

        if not rows:
            inserted[sym] = 0
            print(f"  {sym}: 0 bars returned", flush=True)
            continue

        # Bulk insert with execute_values — ~100x faster than per-row INSERT
        # over a remote DB. Dedup against existing rows in Python so we don't
        # need ON CONFLICT (schema may or may not have a uniqueness constraint).
        from psycopg2.extras import execute_values
        with db.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT timestamp FROM market_data "
                "WHERE symbol=%s AND asset_class=%s "
                "AND timestamp >= %s AND timestamp <= %s",
                (sym, asset_class, start, end),
            )
            existing = {r["timestamp"] for r in cur.fetchall()}
        fresh = [(ts, s, close, vol, asset_class)
                 for (ts, s, close, vol) in rows
                 if ts not in existing]
        if not fresh:
            inserted[sym] = 0
            print(f"  {sym}: 0 new bars ({len(rows)} fetched, all duplicates)",
                  flush=True)
            continue
        with db.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO market_data
                       (timestamp, symbol, price, volume, asset_class)
                   VALUES %s""",
                fresh,
                page_size=2000,
            )
        inserted[sym] = len(fresh)
        print(f"  {sym}: inserted {len(fresh)}/{len(rows)} bars "
              f"({len(rows) - len(fresh)} dups skipped)", flush=True)

    return inserted


def load_bars(
    db: DBManager,
    symbol: str,
    since: datetime | None,
    until: datetime | None,
    asset_class: str = "crypto",
) -> pd.DataFrame:
    rows = db.get_recent_bars(
        symbol, n=1_000_000, asset_class=asset_class,
        since=since, until=until,
    )
    df = indicators.to_dataframe(rows)
    return df


# ====================================================================
#  Vectorised indicator pre-compute (per symbol, once per atr/ema set)
# ====================================================================
@dataclass
class PrecomputedSeries:
    close: pd.Series       # 1m closes
    rsi: pd.Series         # RSI(14) per 1m bar
    atr: pd.Series         # ATR(atr_period) per 1m bar
    trend_up: pd.Series    # bool — 15m EMA-trend direction at each 1m bar


@dataclass
class PrecomputedStocks:
    close: pd.Series
    vwap: pd.Series
    breakout_signal: pd.Series   # bool — VWAP breakout trigger fired this bar
    atr: pd.Series
    trend_up: pd.Series
    rth_mask: pd.Series          # bool — within regular session


def precompute_stocks(
    df: pd.DataFrame, atr_period: int, trend_ema: int,
    vwap_lookback: int = 5, vol_median_window: int = 20,
) -> PrecomputedStocks:
    """Vectorised VWAP-breakout signal (matches stocks_strategy.vwap_breakout_trigger).

    Signal at bar i: close[i] > vwap[i]
        AND any close[i-lb..i-1] < vwap[i-lb..i-1]   (dipped recently)
        AND volume[i] >= rolling-median(volume, 20)  (volume confirmation)
    """
    close = df["close"]
    atr_s = indicators.atr_close(close, length=atr_period)
    vwap_s = indicators.vwap(df)

    above = close > vwap_s
    below_prev = close.shift(1) <= vwap_s.shift(1)  # below or equal previous bar
    # "Dipped in lookback window": rolling OR over prior `lookback` bars
    dipped = below_prev.rolling(vwap_lookback, min_periods=1).max().astype(bool)

    # Volume confirmation
    if "volume" in df.columns:
        vol = df["volume"].fillna(0)
        med20 = vol.rolling(vol_median_window, min_periods=5).median()
        vol_ok = (vol >= med20.fillna(0)) | (med20.isna())
    else:
        vol_ok = pd.Series(True, index=df.index)

    signal = above & dipped & vol_ok

    # 15m trend
    df_15 = indicators.resample_to_15m(df)
    if len(df_15) >= trend_ema:
        ema15 = indicators.ema(df_15["close"], trend_ema)
        trend_up_15 = (df_15["close"] > ema15)
        trend_up_1m = trend_up_15.reindex(df.index, method="ffill").fillna(False)
    else:
        trend_up_1m = pd.Series(False, index=df.index)

    # RTH mask (9:30–15:55 ET, weekdays)
    if isinstance(df.index, pd.DatetimeIndex):
        et = df.index.tz_convert("America/New_York")
        rth = (
            (et.dayofweek < 5)
            & ((et.hour > 9) | ((et.hour == 9) & (et.minute >= 30)))
            & ((et.hour < 15) | ((et.hour == 15) & (et.minute <= 55)))
        )
        rth_mask = pd.Series(rth, index=df.index)
    else:
        rth_mask = pd.Series(True, index=df.index)

    return PrecomputedStocks(
        close=close, vwap=vwap_s, breakout_signal=signal,
        atr=atr_s, trend_up=trend_up_1m, rth_mask=rth_mask,
    )


def simulate_stocks(
    symbol: str,
    pre: PrecomputedStocks,
    atr_sl: float,
    atr_tp: float,
    notional: float,
    commission_pct: float,
) -> list[Trade]:
    """Stocks simulator: VWAP breakout entries, ATR brackets, RTH only.

    Closes any open position at session close (15:55 ET) to avoid holding
    overnight — matches the live bot's intraday-only stance. End-of-data
    open positions close at the last bar's price.
    """
    closes = pre.close.to_numpy(dtype=float)
    signals = pre.breakout_signal.to_numpy(dtype=bool)
    atrs = pre.atr.to_numpy(dtype=float)
    trend_up = pre.trend_up.to_numpy(dtype=bool)
    rth = pre.rth_mask.to_numpy(dtype=bool)
    times = pre.close.index

    trades: list[Trade] = []
    n = len(closes)
    if n < 30:
        return trades

    in_pos = False
    entry_i = -1
    entry_price = 0.0
    qty = 0.0
    sl = 0.0
    tp = 0.0

    for i in range(n - 1):
        if not in_pos:
            if (rth[i] and signals[i] and trend_up[i]
                    and not np.isnan(atrs[i]) and atrs[i] > 0):
                entry_price = closes[i + 1]
                if entry_price <= 0:
                    continue
                qty = notional / entry_price
                sl = entry_price - atr_sl * atrs[i]
                tp = entry_price + atr_tp * atrs[i]
                entry_i = i + 1
                in_pos = True
        else:
            c = closes[i + 1]
            # Session-close forced exit (don't carry overnight)
            session_end = (
                rth[i] and not rth[i + 1]   # crossing out of RTH
            )
            hit_sl = c <= sl
            hit_tp = c >= tp
            if hit_sl or hit_tp or session_end:
                reason = "SL" if hit_sl else ("TP" if hit_tp else "EOD")
                exit_price = c
                gross = (exit_price - entry_price) * qty
                comm = (entry_price + exit_price) * qty * commission_pct
                net = gross - comm
                trades.append(Trade(
                    symbol=symbol,
                    entry_ts=times[entry_i],
                    exit_ts=times[i + 1],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    pnl_gross=gross,
                    pnl_net=net,
                    exit_reason=reason,
                ))
                in_pos = False

    if in_pos:
        exit_price = closes[-1]
        gross = (exit_price - entry_price) * qty
        comm = (entry_price + exit_price) * qty * commission_pct
        net = gross - comm
        trades.append(Trade(
            symbol=symbol,
            entry_ts=times[entry_i],
            exit_ts=times[-1],
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            pnl_gross=gross,
            pnl_net=net,
            exit_reason="END",
        ))

    return trades


def precompute(df: pd.DataFrame, atr_period: int, trend_ema: int) -> PrecomputedSeries:
    close = df["close"]
    rsi_s = indicators.rsi(close)
    atr_s = indicators.atr_close(close, length=atr_period)

    # 15m EMA trend — compute on resampled series, then forward-fill back
    # to the 1m index so each 1m bar inherits the "last known" trend.
    df_15 = indicators.resample_to_15m(df)
    if len(df_15) >= trend_ema:
        ema15 = indicators.ema(df_15["close"], trend_ema)
        trend_up_15 = (df_15["close"] > ema15)
        # Re-index onto 1m series, forward fill. Bars before the first 15m
        # close get NaN → treated as unknown (False).
        trend_up_1m = trend_up_15.reindex(df.index, method="ffill").fillna(False)
    else:
        trend_up_1m = pd.Series(False, index=df.index)

    return PrecomputedSeries(close=close, rsi=rsi_s, atr=atr_s, trend_up=trend_up_1m)


# ====================================================================
#  Simulator (per symbol, per grid config)
# ====================================================================
@dataclass
class Trade:
    symbol: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl_gross: float
    pnl_net: float
    exit_reason: str         # "TP" | "SL" | "END"


def simulate(
    symbol: str,
    pre: PrecomputedSeries,
    rsi_buy: float,
    atr_sl: float,
    atr_tp: float,
    notional: float,
    commission_pct: float,
    strategy: str = "mean",
) -> list[Trade]:
    """Single-pass simulation.

    strategy="mean"  (default): RSI[i] <= rsi_buy + 15m uptrend → BUY
                                 (oversold pullback in an uptrend).
    strategy="trend":            RSI[i] >= rsi_buy + 15m uptrend → BUY
                                 (momentum continuation: bought strength).

    Entry: at bar i, if signal AND trend_up[i] AND ATR[i] > 0 AND
    not currently in a position → enter at bar i+1's close (next-bar exec,
    avoids look-ahead). Bracket sized off ATR[i].

    Exit: from bar i+2 onward, check each bar's close. If close <= SL → SL
    exit at that close. If close >= TP → TP exit at that close. Otherwise
    hold. SL is checked first when a single bar crosses both (conservative
    — assume the adverse direction wins on ambiguity).

    End-of-data: any open position closes at the last bar's close as "END".
    """
    closes = pre.close.to_numpy(dtype=float)
    rsis = pre.rsi.to_numpy(dtype=float)
    atrs = pre.atr.to_numpy(dtype=float)
    trend_up = pre.trend_up.to_numpy(dtype=bool)
    times = pre.close.index

    trades: list[Trade] = []
    n = len(closes)
    if n < 30:
        return trades

    in_pos = False
    entry_i = -1
    entry_price = 0.0
    qty = 0.0
    sl = 0.0
    tp = 0.0

    for i in range(n - 1):
        if not in_pos:
            r = rsis[i]
            a = atrs[i]
            rsi_ok = (not np.isnan(r)) and (
                (r >= rsi_buy) if strategy == "trend" else (r <= rsi_buy)
            )
            if (rsi_ok
                    and trend_up[i]
                    and not np.isnan(a) and a > 0):
                # Enter at next bar's close
                entry_price = closes[i + 1]
                if entry_price <= 0:
                    continue
                qty = notional / entry_price
                sl = entry_price - atr_sl * a
                tp = entry_price + atr_tp * a
                entry_i = i + 1
                in_pos = True
        else:
            c = closes[i + 1]
            hit_sl = c <= sl
            hit_tp = c >= tp
            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"   # SL wins on simultaneous
                exit_price = c
                gross = (exit_price - entry_price) * qty
                comm = (entry_price + exit_price) * qty * commission_pct
                net = gross - comm
                trades.append(Trade(
                    symbol=symbol,
                    entry_ts=times[entry_i],
                    exit_ts=times[i + 1],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    pnl_gross=gross,
                    pnl_net=net,
                    exit_reason=reason,
                ))
                in_pos = False

    if in_pos:
        exit_price = closes[-1]
        gross = (exit_price - entry_price) * qty
        comm = (entry_price + exit_price) * qty * commission_pct
        net = gross - comm
        trades.append(Trade(
            symbol=symbol,
            entry_ts=times[entry_i],
            exit_ts=times[-1],
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            pnl_gross=gross,
            pnl_net=net,
            exit_reason="END",
        ))

    return trades


# ====================================================================
#  Reporting
# ====================================================================
@dataclass
class ConfigReport:
    rsi_buy: float
    atr_sl: float
    atr_tp: float
    trades: list[Trade]

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wr_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_net > 0)
        return 100.0 * wins / len(self.trades)

    @property
    def pnl_net(self) -> float:
        return float(sum(t.pnl_net for t in self.trades))

    @property
    def avg_pnl(self) -> float:
        return self.pnl_net / len(self.trades) if self.trades else 0.0

    @property
    def max_dd(self) -> float:
        """Peak-to-trough equity drawdown across the trade sequence."""
        if not self.trades:
            return 0.0
        eq = np.cumsum([t.pnl_net for t in self.trades])
        peak = np.maximum.accumulate(eq)
        return float(np.max(peak - eq))

    @property
    def worst_loss(self) -> float:
        losses = [t.pnl_net for t in self.trades if t.pnl_net < 0]
        return float(min(losses)) if losses else 0.0

    @property
    def sharpe(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        pnls = np.array([t.pnl_net for t in self.trades])
        sd = float(pnls.std(ddof=1))
        return float(pnls.mean() / sd) if sd > 0 else 0.0

    def passes_gate(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        ok = True
        if self.n < GATE_MIN_TRADES:
            ok = False
            reasons.append(f"trades {self.n} < {GATE_MIN_TRADES}")
        if self.wr_pct < GATE_MIN_WR_PCT:
            ok = False
            reasons.append(f"WR {self.wr_pct:.1f}% < {GATE_MIN_WR_PCT}%")
        if GATE_REQUIRE_POSITIVE_PNL and self.pnl_net <= 0:
            ok = False
            reasons.append(f"PnL ${self.pnl_net:+.2f} not positive")
        if self.worst_loss < 0:
            ratio = self.max_dd / abs(self.worst_loss)
            if ratio > GATE_MAX_DD_OVER_WORST_LOSS:
                ok = False
                reasons.append(
                    f"DD/worst_loss {ratio:.2f} > {GATE_MAX_DD_OVER_WORST_LOSS}"
                )
        return ok, reasons


def format_report(reports: list[ConfigReport]) -> str:
    lines: list[str] = []
    header = (
        f"{'rsi_buy':>7}  {'atr_sl':>6}  {'atr_tp':>6}  "
        f"{'N':>5}  {'WR%':>6}  {'PnL$':>10}  {'avg$':>7}  "
        f"{'maxDD$':>8}  {'sharpe':>6}  gate"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in sorted(reports, key=lambda x: -x.pnl_net):
        ok, reasons = r.passes_gate()
        flag = "PASS" if ok else "FAIL: " + "; ".join(reasons)
        lines.append(
            f"{r.rsi_buy:7.0f}  {r.atr_sl:6.2f}  {r.atr_tp:6.2f}  "
            f"{r.n:5d}  {r.wr_pct:6.1f}  {r.pnl_net:+10.2f}  "
            f"{r.avg_pnl:+7.3f}  {r.max_dd:8.2f}  {r.sharpe:6.2f}  {flag}"
        )
    return "\n".join(lines)


# ====================================================================
#  CLI
# ====================================================================
def parse_grid(args: Iterable[str]) -> dict[str, list[float]]:
    """Parse a list of 'name=v1,v2,v3' into {name: [v1, v2, v3]}."""
    out: dict[str, list[float]] = {}
    for a in args:
        if "=" not in a:
            continue
        k, vs = a.split("=", 1)
        out[k] = [float(x) for x in vs.split(",") if x.strip()]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-class", choices=["crypto", "stock"],
                    default="crypto")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated symbols. Defaults to liquid majors "
                         "of the chosen asset-class.")
    ap.add_argument("--since", help="ISO date / datetime; bars after this only")
    ap.add_argument("--until", help="ISO date / datetime; bars before this only")
    ap.add_argument("--fetch-history", action="store_true",
                    help="Backfill market_data from Alpaca before backtesting")
    ap.add_argument("--days", type=int, default=60,
                    help="Days of history to fetch with --fetch-history")
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--trend-ema", type=int, default=50)
    ap.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    ap.add_argument("--commission-pct", type=float, default=DEFAULT_COMMISSION_PCT)
    ap.add_argument("--grid", nargs="*", default=[],
                    help="Param sweep, e.g. rsi_buy=25,30 atr_sl=1.0,1.5 atr_tp=2.0,3.0")
    ap.add_argument("--strategy", choices=["mean", "trend"], default="mean",
                    help="mean-reversion (RSI<=X) or trend-following (RSI>=X)")
    args = ap.parse_args(argv)

    if args.symbols is None:
        default = (DEFAULT_STOCKS_SYMBOLS if args.asset_class == "stock"
                   else DEFAULT_SYMBOLS)
        args.symbols = ",".join(default)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    since = pd.Timestamp(args.since, tz="UTC").to_pydatetime() if args.since else None
    until = pd.Timestamp(args.until, tz="UTC").to_pydatetime() if args.until else None

    # Default to 0% commission for stocks (Alpaca free), 0.15% for crypto
    if args.commission_pct == DEFAULT_COMMISSION_PCT and args.asset_class == "stock":
        args.commission_pct = DEFAULT_STOCKS_COMMISSION_PCT

    db = DBManager()
    db.connect()

    if args.fetch_history:
        print(f"Fetching {args.days}d of 1m bars from Alpaca for "
              f"{len(symbols)} {args.asset_class} symbols...")
        fetch_history_from_alpaca(db, symbols, args.days,
                                  asset_class=args.asset_class)

    # Pre-flight: bar coverage per symbol
    print("\nBar coverage:")
    coverage_ok = True
    for sym in symbols:
        df = load_bars(db, sym, since, until, asset_class=args.asset_class)
        days = (df.index.max() - df.index.min()).total_seconds() / 86400 if len(df) else 0
        flag = "✓" if days >= 30 else "⚠"
        if days < 30:
            coverage_ok = False
        print(f"  {flag} {sym:10s}  bars={len(df):7d}  span={days:5.1f}d")
    if not coverage_ok:
        print("WARNING: <30 days for one or more symbols — gate result is "
              "low-confidence. Consider --fetch-history --days 60.")

    # Build grid
    grid = parse_grid(args.grid)
    rsi_buys = grid.get("rsi_buy", [30.0])
    atr_sls = grid.get("atr_sl", [1.5])
    atr_tps = grid.get("atr_tp", [3.0])
    combos = list(itertools.product(rsi_buys, atr_sls, atr_tps))
    print(f"\nGrid: {len(combos)} configs × {len(symbols)} symbols = "
          f"{len(combos) * len(symbols)} simulations")

    # Pre-compute indicators per symbol ONCE (grid only varies thresholds,
    # not atr_period / trend_ema). Use the asset-class-specific path.
    reports: list[ConfigReport] = []
    if args.asset_class == "stock":
        pre_by_symbol_s: dict[str, PrecomputedStocks] = {}
        for sym in symbols:
            df = load_bars(db, sym, since, until, asset_class="stock")
            if len(df) < 100:
                print(f"  {sym}: <100 bars, skipping")
                continue
            pre_by_symbol_s[sym] = precompute_stocks(
                df, args.atr_period, args.trend_ema,
            )
        # Stocks: rsi_buy column unused, treat each (atr_sl, atr_tp) combo
        # as a config. Collapse duplicate rsi_buys to a single iteration.
        seen_brackets: set[tuple[float, float]] = set()
        for rsi_buy, atr_sl, atr_tp in combos:
            key = (atr_sl, atr_tp)
            if key in seen_brackets:
                continue
            seen_brackets.add(key)
            all_trades: list[Trade] = []
            for sym, pre in pre_by_symbol_s.items():
                all_trades.extend(simulate_stocks(
                    sym, pre, atr_sl, atr_tp,
                    args.notional, args.commission_pct,
                ))
            reports.append(ConfigReport(
                rsi_buy=0.0, atr_sl=atr_sl, atr_tp=atr_tp,
                trades=all_trades,
            ))
    else:
        pre_by_symbol: dict[str, PrecomputedSeries] = {}
        for sym in symbols:
            df = load_bars(db, sym, since, until, asset_class="crypto")
            if len(df) < 100:
                print(f"  {sym}: <100 bars, skipping")
                continue
            pre_by_symbol[sym] = precompute(df, args.atr_period, args.trend_ema)
        for rsi_buy, atr_sl, atr_tp in combos:
            all_trades = []
            for sym, pre in pre_by_symbol.items():
                all_trades.extend(simulate(
                    sym, pre, rsi_buy, atr_sl, atr_tp,
                    args.notional, args.commission_pct,
                    strategy=args.strategy,
                ))
            reports.append(ConfigReport(
                rsi_buy=rsi_buy, atr_sl=atr_sl, atr_tp=atr_tp,
                trades=all_trades,
            ))

    print("\nResults (sorted by PnL desc):\n")
    print(format_report(reports))

    # Decision gate
    print("\n" + "=" * 60)
    passing = [r for r in reports if r.passes_gate()[0]]
    if passing:
        best = max(passing, key=lambda r: r.pnl_net)
        print(f"GATE PASS: {len(passing)} config(s) passed. Best:")
        print(f"  rsi_buy={best.rsi_buy:.0f}  atr_sl={best.atr_sl}  atr_tp={best.atr_tp}")
        print(f"  N={best.n}  WR={best.wr_pct:.1f}%  PnL=${best.pnl_net:+.2f}  "
              f"maxDD=${best.max_dd:.2f}  sharpe={best.sharpe:.2f}")
        print("Recommendation: lock these params and re-enable paper trading for "
              "2 weeks of out-of-sample validation before promotion.")
    else:
        print("GATE FAIL: no configuration passed the edge criteria.")
        print(f"  Required: ≥{GATE_MIN_TRADES} trades, WR≥{GATE_MIN_WR_PCT}%, "
              f"PnL>0, DD<{GATE_MAX_DD_OVER_WORST_LOSS}×worst-loss.")
        print("Recommendation: do NOT re-enable live trading on this strategy.")
        print("  Options (in order of effort):")
        print("  1) Add ONE confirmation filter (volume Z-score OR BB%) and re-test.")
        print("  2) Switch to trend-following (RSI>50 + breakout) and re-test.")
        print("  3) Keep crypto bot paper-only; shift effort to brain/stocks_strategy.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
