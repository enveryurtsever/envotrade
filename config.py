"""Central configuration — reads `.env` and exposes constants."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


# --- Alpaca ---------------------------------------------------
ALPACA_MODE = _env("ALPACA_MODE", "paper").lower()
ALPACA_PAPER = ALPACA_MODE != "live"
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
)
ALPACA_KEY = _env("ALPACA_KEY")
ALPACA_SECRET = _env("ALPACA_SECRET")

# --- PostgreSQL ----------------------------------------------
DB_HOST = _env("DB_HOST", "localhost")
DB_PORT = int(_env("DB_PORT", "5432"))
DB_NAME = _env("DB_NAME", "envotrade")
DB_USER = _env("DB_USER", "envotrade")
DB_PASS = _env("DB_PASS")

# --- Anthropic Claude ----------------------------------------
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# Default to Haiku for both — ~5x cheaper than Sonnet per token.
# Set CLAUDE_DECISION_MODEL=claude-sonnet-4-6 in .env if you want higher reasoning
# quality at higher cost.
CLAUDE_DECISION_MODEL = _env("CLAUDE_DECISION_MODEL", "claude-haiku-4-5")
CLAUDE_SENTIMENT_MODEL = _env("CLAUDE_SENTIMENT_MODEL", "claude-haiku-4-5")

# --- Twitter (RapidAPI unofficial) ---------------------------
RAPIDAPI_KEY = _env("RAPIDAPI_KEY")
RAPIDAPI_HOST = _env("RAPIDAPI_HOST", "twitter-api45.p.rapidapi.com")
RAPIDAPI_BASE_URL = f"https://{RAPIDAPI_HOST}"

# Crypto-relevant VIP handles. Only VERIFIED humans / official accounts —
# we deliberately skip anon aggregators (BTC_Archive, DocumentingBTC) and
# clickbait YouTubers (IvanOnTech) because they re-publish other people's
# takes, polluting sentiment without adding signal. The Twitter agent
# round-robins through the list 3 at a time per cycle. Override via env
# (comma-separated, no @).
VIP_CRYPTO_HANDLES = [h.strip().lstrip("@") for h in _env(
    "VIP_CRYPTO_HANDLES",
    # Founders / exchange CEOs / on-chain analysts / macro voices with
    # documented market-moving impact.
    "cz_binance,VitalikButerin,saylor,elonmusk,APompliano,"
    "woonomic,CryptoHayes,balajis,RaoulGMI,PeterLBrandt,"
    "novogratz,aantonop,nayibbukele,justinsuntron,BarrySilbert",
).split(",") if h.strip()]

# US-equities VIPs. Same filter: real PMs, journalists, official outlets.
# Anon aggregator/meme accounts (DeItaone, zerohedge) excluded — they're
# fast but unreliable and we already get breaking news via the RSS feeds.
VIP_STOCKS_HANDLES = [h.strip().lstrip("@") for h in _env(
    "VIP_STOCKS_HANDLES",
    "elonmusk,jimcramer,elerianm,chamath,BillAckman,Carl_C_Icahn,"
    "mcuban,JimChanos,LizAnnSonders,LisaAbramowicz1,TruthGundlach,"
    "SquawkCNBC,SquawkStreet,RaoulGMI,michaeljburry,markets,"
    "BespokeInvest",
).split(",") if h.strip()]

# --- News RSS feeds ------------------------------------------
NEWS_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# --- Tuning ---------------------------------------------------
SCAN_INTERVAL_SEC = int(_env("SCAN_INTERVAL_SEC", "300"))
AI_CONFIDENCE_THRESHOLD = float(_env("AI_CONFIDENCE_THRESHOLD", "70"))

# --- Risk / capital allocation --------------------------------
# Hard cap on aggregate open-position notional (USD).  Even if the broker
# account has more, we never expose more than this.
MAX_TRADING_CAPITAL = float(_env("MAX_TRADING_CAPITAL", "1000"))
# How much USD to spend per trade.  With $100/trade and $1000 cap → up to 10
# concurrent positions, matching the 10-symbol watchlist.
NOTIONAL_PER_TRADE = float(_env("NOTIONAL_PER_TRADE", "100"))

# --- US Equities (Stocks) ------------------------------------
# Curated mega-cap whitelist — tight spreads, deep liquidity, well-defined
# earnings calendars. Mirrors LIQUID_MAJORS for crypto; expand later via
# bot_settings if performance warrants.
STOCKS_LIQUID_MAJORS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "META",
    "GOOGL", "TSLA", "AMD", "SPY", "QQQ",
]
# Eastern Time (broker reference). Scanner & strategy convert via zoneinfo.
# 09:35 (not 09:30) avoids the opening-print noise of the first 5 minutes
# where spreads are wide and order routing is volatile. 15:55 (not 16:00)
# leaves 5 minutes for EOD-flatten exits to fill before the real close.
STOCKS_MARKET_OPEN_ET = _env("STOCKS_MARKET_OPEN_ET", "09:35")
STOCKS_MARKET_CLOSE_ET = _env("STOCKS_MARKET_CLOSE_ET", "15:55")
# 15m EMA length for big-picture trend filter; 50 ≈ ~12.5 trading hours.
STOCKS_15M_EMA_LEN = int(_env("STOCKS_15M_EMA_LEN", "50"))
# Stop = entry − this × ATR(14, 1m).  1.5× matches the crypto preset.
STOCKS_ATR_STOP_MULT = float(_env("STOCKS_ATR_STOP_MULT", "1.5"))
STOCKS_ATR_TP_MULT = float(_env("STOCKS_ATR_TP_MULT", "3.0"))
# Independent scan cadence so stocks watchlist refresh doesn't piggyback
# on crypto's loop.
STOCKS_SCAN_INTERVAL_SEC = int(_env("STOCKS_SCAN_INTERVAL_SEC", "300"))
# Index-health filter benchmarks. If both indices are below their 15m EMA50
# AND below today's open, BUY signals are vetoed market-wide.
STOCKS_INDEX_SYMBOLS = ("SPY", "QQQ")

# --- Paths ---------------------------------------------------
ASSETS_DIR = BASE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
SCHEMA_FILE = BASE_DIR / "database" / "schema.sql"
