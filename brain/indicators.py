"""Technical indicators (RSI, Bollinger Bands, MACD, ATR, EMA) — pure NumPy/Pandas.

No `pandas-ta` dependency (numba/llvmlite stack pins Python<3.14).
Wilder-smoothed RSI matches TradingView/most charting platforms.

NOTE on ATR: `market_data` stores only close (no OHLC), so `atr_close()` uses
absolute close-to-close differences as the True Range proxy. On 1m crypto
majors with tight candles this tracks classic ATR within a few percent —
enough to size SL/TP dynamically. If we add high/low columns later, swap to
classic Wilder ATR with no other call-site changes.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def to_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Convert db.get_recent_bars rows to a TA-friendly DataFrame."""
    if not rows:
        return pd.DataFrame(columns=["timestamp", "close", "volume"])
    df = pd.DataFrame(rows)
    df["close"] = df["close"].astype(float)
    if "volume" in df:
        df["volume"] = df["volume"].astype(float).fillna(0)
    if "timestamp" in df:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.set_index("timestamp", inplace=True)
    return df


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)
    return pd.DataFrame({
        "lower": mid - std * sd,
        "mid":   mid,
        "upper": mid + std * sd,
    })


def ema(close: pd.Series, length: int) -> pd.Series:
    return close.ewm(span=length, adjust=False, min_periods=length).mean()


def atr_close(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder-smoothed close-to-close TR proxy. See module docstring."""
    tr = close.diff().abs()
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def resample_to_15m(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m close-only bars to 15m closes. Returns empty if input lacks
    a DatetimeIndex (caller is expected to feed `to_dataframe()` output)."""
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return pd.DataFrame(columns=["close"])
    out = df["close"].resample("15min", label="right", closed="right").last().dropna()
    return out.to_frame(name="close")


def trend_15m(df_1m: pd.DataFrame, ema_length: int = 50) -> dict:
    """Return 15m-EMA trend read derived by resampling 1m bars.

    `known=False` means insufficient history (< ema_length 15m bars after
    resample). Callers should treat unknown trend conservatively (skip BUY).
    """
    df_15m = resample_to_15m(df_1m)
    if len(df_15m) < ema_length:
        return {"known": False, "direction": None, "ema": None,
                "last_close_15m": None, "bars_15m": len(df_15m)}
    ema_v = float(ema(df_15m["close"], ema_length).iloc[-1])
    last = float(df_15m["close"].iloc[-1])
    direction = "up" if last > ema_v else "down"
    return {"known": True, "direction": direction, "ema": ema_v,
            "last_close_15m": last, "bars_15m": len(df_15m)}


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP from 1m close + volume.

    NOTE: We only persist close (no high/low), so typical-price degenerates
    to close. For minute bars on liquid mega-caps this tracks Bloomberg
    VWAP within a few bps — close enough to detect a clean breakout.
    Session anchoring (US/Eastern) resets the cumulative sums at each
    new trading day so pre-market noise doesn't drag the post-09:30 line.
    """
    if df.empty or "volume" not in df:
        return pd.Series(dtype="float64", index=df.index)
    if not isinstance(df.index, pd.DatetimeIndex):
        # Without a time index we can't session-anchor; fall back to a
        # single cumulative VWAP over all rows.
        pv = (df["close"] * df["volume"]).cumsum()
        cv = df["volume"].cumsum().replace(0.0, np.nan)
        return (pv / cv).fillna(method="ffill")
    et = df.index.tz_convert("America/New_York")
    session = et.normalize()  # group key per trading day
    pv = (df["close"] * df["volume"]).groupby(session).cumsum()
    cv = df["volume"].groupby(session).cumsum().replace(0.0, np.nan)
    return (pv / cv).ffill()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "hist": macd_line - signal_line,
    })


def latest_indicator_snapshot(df: pd.DataFrame, atr_period: int = 14,
                              trend_ema: int = 50) -> dict[str, Any]:
    """Per-symbol indicator snapshot.

    Returns ATR + 15m trend in addition to legacy RSI/BB/MACD so the math
    decision in `ai_analyst.decide()` can size SL/TP and gate BUYs by trend
    without recomputing.
    """
    if len(df) < 25:
        return {"rsi": None, "bb_pct": None, "macd_hist": None, "close": None,
                "atr": None, "trend": {"known": False, "direction": None,
                                       "ema": None, "last_close_15m": None,
                                       "bars_15m": 0}}

    close = df["close"]
    rsi_series = rsi(close)
    bb = bbands(close)
    mac = macd(close)
    atr_series = atr_close(close, length=atr_period)

    close_v = float(close.iloc[-1])
    rsi_v = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

    bb_pct = None
    if not bb.empty:
        upper = float(bb["upper"].iloc[-1])
        lower = float(bb["lower"].iloc[-1])
        if not np.isnan(upper) and not np.isnan(lower) and upper != lower:
            bb_pct = (close_v - lower) / (upper - lower)

    macd_hist = None
    if not mac.empty:
        h = mac["hist"].iloc[-1]
        if not np.isnan(h):
            macd_hist = float(h)

    atr_v = None
    if not atr_series.empty:
        a = atr_series.iloc[-1]
        if not np.isnan(a):
            atr_v = float(a)

    return {
        "rsi": rsi_v,
        "bb_pct": bb_pct,
        "macd_hist": macd_hist,
        "close": close_v,
        "atr": atr_v,
        "trend": trend_15m(df, ema_length=trend_ema),
    }


def should_trigger_ai(snapshot: dict, rsi_buy: float, rsi_sell: float) -> bool:
    """Math filter: returns True iff RSI crossed buy or sell threshold."""
    r = snapshot.get("rsi")
    if r is None:
        return False
    return r <= rsi_buy or r >= rsi_sell
