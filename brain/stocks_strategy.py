"""Stocks trading strategy primitives.

Decision flow per 1m cycle (composed by StocksAILoop):

  1. is_market_open()           — gate on regular-session hours (ET)
  2. check_index_health()       — SPY+QQQ above 15m EMA50 AND day-open?
  3. for each symbol:
        get_15m_trend()         — fiyat 15m EMA50 üstünde mi?  (sadece "up")
        vwap_breakout_trigger() — 1m fiyat VWAP altından kırdı mı?
        atr_stop()              — entry − 1.5 × ATR(1m)

The crypto math (RSI / 15m EMA / ATR) is intentionally NOT reused — equity
microstructure rewards VWAP-anchored entries and institutional flow logic
rather than RSI mean reversion.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

import config
from brain import indicators

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- session
def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def is_market_open(now_utc: datetime | None = None) -> bool:
    """True iff `now_utc` falls inside regular US equities session.

    Weekend short-circuits. Holiday calendar is intentionally NOT consulted;
    Alpaca will reject orders on holidays and the worker logs an error, but
    we don't want to bake an annual holiday list into the bot. If misses
    become noisy we'll add it via Alpaca's market-clock endpoint.
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = _parse_hhmm(config.STOCKS_MARKET_OPEN_ET)
    close_t = _parse_hhmm(config.STOCKS_MARKET_CLOSE_ET)
    return open_t <= now.time() <= close_t


# ---------------------------------------------------------------- trend
def get_15m_trend(
    df_1m: pd.DataFrame, ema_length: int | None = None,
) -> dict:
    """Resample 1m close-only bars to 15m, return EMA-direction snapshot.

    Wrapper around indicators.trend_15m so callers don't need to import
    indicators directly and to centralize the default ema length.
    """
    ema_len = ema_length or config.STOCKS_15M_EMA_LEN
    return indicators.trend_15m(df_1m, ema_length=ema_len)


# ---------------------------------------------------------------- VWAP
def vwap_breakout_trigger(df_1m: pd.DataFrame, lookback: int = 5) -> bool:
    """True iff price crossed back ABOVE VWAP within the last `lookback` bars.

    The trade we want: price dipped under VWAP (intraday pullback), then
    a hacimli mum closed above it. We approximate "hacimli" by requiring
    the breakout bar's volume to be ≥ the rolling 20-bar median — Alpaca
    free-tier IEX data is partial volume so don't require a huge multiple.
    """
    if df_1m.empty or len(df_1m) < max(lookback + 1, 25):
        return False
    if "volume" not in df_1m:
        return False
    vw = indicators.vwap(df_1m)
    if vw.empty:
        return False
    close = df_1m["close"]
    last = close.iloc[-1]
    last_vw = vw.iloc[-1]
    if pd.isna(last_vw) or last <= last_vw:
        return False
    # Was there a dip under VWAP in the lookback window?
    window = close.iloc[-(lookback + 1):-1]
    vw_window = vw.iloc[-(lookback + 1):-1]
    dipped = (window < vw_window).any()
    if not dipped:
        return False
    # Volume confirmation
    vol = df_1m["volume"]
    median20 = vol.rolling(20).median().iloc[-1]
    if pd.isna(median20) or median20 <= 0:
        return True  # not enough volume history; let the trade through
    return float(vol.iloc[-1]) >= float(median20)


# ---------------------------------------------------------------- stop
def atr_stop(
    entry: float, atr_1m: float, mult: float | None = None,
) -> float:
    """Long stop = entry − mult × ATR(1m). Returns 0 if inputs are unusable
    so the caller can fall back to a percent stop without dividing by 0."""
    if entry <= 0 or atr_1m <= 0:
        return 0.0
    m = mult if mult is not None else config.STOCKS_ATR_STOP_MULT
    return entry - m * atr_1m


def atr_take_profit(
    entry: float, atr_1m: float, mult: float | None = None,
) -> float:
    if entry <= 0 or atr_1m <= 0:
        return 0.0
    m = mult if mult is not None else config.STOCKS_ATR_TP_MULT
    return entry + m * atr_1m


# ---------------------------------------------------------------- index
def check_index_health(
    spy_df: pd.DataFrame, qqq_df: pd.DataFrame,
    ema_length: int | None = None,
) -> dict:
    """Aggregate read on SPY + QQQ. Returns:
        {"healthy": bool, "spy_up": bool, "qqq_up": bool, "reason": str}

    Healthy ⇔ at least one of the two indices is above its 15m EMA50.
    Both broken → bear-leaning tape, BUY-side trades vetoed market-wide.
    """
    ema_len = ema_length or config.STOCKS_15M_EMA_LEN
    spy = indicators.trend_15m(spy_df, ema_length=ema_len)
    qqq = indicators.trend_15m(qqq_df, ema_length=ema_len)
    spy_up = bool(spy.get("known") and spy.get("direction") == "up")
    qqq_up = bool(qqq.get("known") and qqq.get("direction") == "up")
    healthy = spy_up or qqq_up
    if healthy:
        reason = (
            f"SPY={'up' if spy_up else 'down'} "
            f"QQQ={'up' if qqq_up else 'down'}"
        )
    else:
        reason = "both SPY and QQQ below 15m EMA50"
    return {"healthy": healthy, "spy_up": spy_up, "qqq_up": qqq_up,
            "reason": reason}
