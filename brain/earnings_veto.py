"""Earnings + FOMC veto for stocks trades.

- Earnings: Alpaca corporate-actions endpoint covers announced earnings
  dates for US equities (free tier). We cache per-symbol per-session.
- FOMC: there is no first-class Alpaca endpoint; we instead ask Claude
  Haiku to scan today's news feed for the meeting flag. Cheap (<200 toks)
  and the answer changes at most ~8 times a year.

Both checks fail-OPEN (return False = "no veto") on any error — we'd
rather miss the veto than freeze the bot due to a transient API outage.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import config

log = logging.getLogger(__name__)


# In-memory cache: { symbol: (date_checked, has_earnings_within_window) }
_EARNINGS_CACHE: dict[str, tuple[date, bool]] = {}
_EARNINGS_LOCK = threading.Lock()

# FOMC cache: (date_checked, is_fomc_day_or_eve)
_FOMC_CACHE: tuple[date, bool] | None = None
_FOMC_LOCK = threading.Lock()


# ====================================================================
def check_earnings(symbol: str, days_window: int = 1) -> bool:
    """True iff `symbol` has an earnings announcement today or within
    `days_window` calendar days. Cached for the rest of the trading day.
    """
    today = date.today()
    with _EARNINGS_LOCK:
        cached = _EARNINGS_CACHE.get(symbol)
        if cached and cached[0] == today:
            return cached[1]

    flagged = _fetch_earnings_flag(symbol, days_window=days_window)
    with _EARNINGS_LOCK:
        _EARNINGS_CACHE[symbol] = (today, flagged)
    return flagged


def _fetch_earnings_flag(symbol: str, days_window: int) -> bool:
    """Query Alpaca corporate actions for earnings within window. Fail-open."""
    if not config.ALPACA_KEY:
        return False
    try:
        import requests
    except Exception:
        return False
    today = date.today()
    until = today + timedelta(days=days_window)
    url = "https://data.alpaca.markets/v1/corporate-actions"
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
    }
    params = {
        "symbols": symbol,
        "types": "earnings_announcement",
        "start": today.isoformat(),
        "end": until.isoformat(),
        "limit": 10,
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=8)
        if r.status_code != 200:
            log.debug("earnings fetch %s: HTTP %s", symbol, r.status_code)
            return False
        data = r.json() or {}
    except Exception as e:
        log.debug("earnings fetch %s failed: %s", symbol, e)
        return False
    actions = data.get("corporate_actions") or {}
    items = actions.get("earnings_announcements") or []
    if not items and isinstance(data, dict):
        # Some response shapes return a flat list keyed by symbol.
        items = data.get(symbol) or []
    return bool(items)


# ====================================================================
def check_fomc(
    news_items: Iterable[dict] | None = None,
    anthropic_client=None,
) -> bool:
    """True iff today is a FOMC day or the day before (volatility lockout).

    `news_items` is the recent sentiment feed (last ~24h). We pass titles
    to Haiku and ask a yes/no question. Cached per-calendar-day so the
    LLM is called at most once a day.
    """
    global _FOMC_CACHE
    today = date.today()
    with _FOMC_LOCK:
        if _FOMC_CACHE and _FOMC_CACHE[0] == today:
            return _FOMC_CACHE[1]

    flagged = False
    if anthropic_client is not None and news_items is not None:
        flagged = _ask_haiku_fomc(news_items, anthropic_client)
    with _FOMC_LOCK:
        _FOMC_CACHE = (today, flagged)
    return flagged


_FOMC_PROMPT = """You scan recent financial-news headlines for one thing only:
is TODAY a Federal Reserve FOMC rate-decision day, OR the calendar day BEFORE one?

You will receive a JSON array of recent headline summaries. Return JSON:
  {"fomc": true|false, "reason": "<one short sentence>"}

Default to false. Only return true if a headline explicitly references:
  • FOMC meeting today / yesterday / tomorrow
  • Fed rate decision today / tomorrow
  • Powell press conference today / tomorrow
General Fed commentary, dot-plot analysis, or speeches by other Fed officials
do NOT count.
"""


def _ask_haiku_fomc(news_items: Iterable[dict], client) -> bool:
    snippets = []
    for it in news_items:
        s = (it.get("summary") or "").strip()
        if s:
            snippets.append(s[:240])
        if len(snippets) >= 25:
            break
    if not snippets:
        return False
    try:
        resp = client.messages.create(
            model=config.CLAUDE_SENTIMENT_MODEL,
            max_tokens=100,
            system=[{
                "type": "text",
                "text": _FOMC_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": json.dumps(snippets)}],
        )
    except Exception as e:
        log.debug("fomc check failed: %s", e)
        return False
    try:
        blocks = resp.content
        text = ""
        for b in blocks:
            if getattr(b, "type", None) == "text":
                text = b.text
                break
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        return bool(data.get("fomc"))
    except Exception:
        return False
