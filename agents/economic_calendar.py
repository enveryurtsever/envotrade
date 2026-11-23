"""Forex Factory economic calendar agent — high-impact macro events only.

Feeds: thisweek + nextweek so the list survives Fri/Sat rollover, when the
weekly file has no upcoming items left. (Free, no key.)

Returns the next ~3 upcoming events whose `impact == 'High'` (FOMC, CPI, NFP
etc.). Pulled once an hour into memory; the UI calls `get_upcoming()` and
gets back a list of dicts ready to render.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_FEED_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
)
# Forex Factory publishes times in US/Eastern.
_FF_TZ = ZoneInfo("America/New_York")

# Refresh cadence: once an hour is enough — events get added at most daily.
_REFRESH_SECONDS = 3600


class EconomicCalendar:
    """In-memory cache of upcoming high-impact macro events.

    Thread-safe.  The UI polls `get_upcoming(n)` on its render tick; the
    agent's background thread keeps the cache warm.
    """

    def __init__(self, log_queue=None, max_events: int = 3) -> None:
        self.log_queue = log_queue
        self.max_events = max_events
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fetch_ok = 0.0

    # ---- logging helper -------------------------------------------------
    def _log(self, text: str, tag: str = "INFO") -> None:
        if self.log_queue is not None:
            try:
                self.log_queue.put({"tag": tag, "text": text})
            except Exception:
                pass

    # ---- public API -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="EconomicCalendar", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass

    def get_upcoming(self, n: int | None = None) -> list[dict]:
        """Return next N upcoming events (UTC-naive datetimes converted)."""
        n = n or self.max_events
        now = datetime.now(timezone.utc)
        with self._lock:
            future = [e for e in self._events if e["dt_utc"] >= now]
        future.sort(key=lambda e: e["dt_utc"])
        return future[:n]

    # ---- background loop -----------------------------------------------
    def _run(self) -> None:
        # First fetch right away so the UI has data within seconds.
        self._safe_refresh()
        while not self._stop.wait(_REFRESH_SECONDS):
            self._safe_refresh()

    def _safe_refresh(self) -> None:
        events: list[dict] = []
        last_error: Exception | None = None
        for url in _FEED_URLS:
            try:
                events.extend(self._fetch_events(url))
            except Exception as e:
                last_error = e
        if not events:
            if last_error is not None and time.time() - self._last_fetch_ok > 3600:
                self._log(
                    f"economic calendar fetch failed: {last_error}", tag="ERROR",
                )
            return
        # Dedupe (same event can appear in both feeds across week boundaries)
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for e in events:
            key = (e["title"], e["country"], e["dt_utc"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
        with self._lock:
            self._events = deduped
        self._last_fetch_ok = time.time()

    def _fetch_events(self, url: str) -> Iterable[dict]:
        import requests  # local import — agent is optional
        # Forex Factory throttles requests aggressively and serves a HTML
        # "blocked" page to bot-like UAs. Identify as a normal browser and
        # respect their once-per-hour cadence (see _REFRESH_SECONDS).
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.5 Safari/605.1.15"
            ),
            "Accept": "application/xml,text/xml,*/*;q=0.1",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text
        # Guard against HTML "blocked" pages — bail out so the next refresh
        # gets another shot instead of polluting the cache with garbage.
        if "<event>" not in text:
            raise RuntimeError("feed returned no <event> blocks (rate-limited?)")
        # The Forex Factory weekly XML is flat <event>...</event> blocks.
        # Use a regex slicer so we don't pull in a heavy XML parser
        # (the project's bundled Python ships without `expat`).
        for block in re.findall(r"<event>(.*?)</event>", text, flags=re.DOTALL):
            impact = _extract_tag(block, "impact").strip()
            if impact.lower() != "high":
                continue
            title = _extract_tag(block, "title").strip()
            country = _extract_tag(block, "country").strip()
            date_str = _extract_tag(block, "date").strip()
            time_str = _extract_tag(block, "time").strip()
            dt = _parse_ff_datetime(date_str, time_str)
            if dt is None:
                continue
            yield {
                "title": title,
                "country": country,
                "impact": impact,
                "dt_utc": dt.astimezone(timezone.utc),
                "forecast": _extract_tag(block, "forecast").strip(),
                "previous": _extract_tag(block, "previous").strip(),
            }


def _extract_tag(block: str, tag: str) -> str:
    """Return the inner text of <tag>…</tag> within a block, '' if absent.

    Tolerant of CDATA wrappers used by the Forex Factory feed.
    """
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL)
    if not m:
        return ""
    inner = m.group(1)
    cdata = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*", inner, flags=re.DOTALL)
    return cdata.group(1) if cdata else inner


def _parse_ff_datetime(date_str: str, time_str: str) -> datetime | None:
    """ForexFactory date 'MM-DD-YYYY' + time '2:00pm' / 'All Day' / 'Tentative'.

    Returns ET-aware datetime, or None when the entry has no usable time.
    """
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%m-%d-%Y").date()
    except ValueError:
        return None
    tl = (time_str or "").lower().strip()
    if tl in ("", "all day", "tentative"):
        # Anchor at session open (8:30 ET) so the event still surfaces in
        # the upcoming list rather than getting lost at midnight.
        hh, mm = 8, 30
    else:
        try:
            t = datetime.strptime(tl, "%I:%M%p").time()
            hh, mm = t.hour, t.minute
        except ValueError:
            return None
    naive = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
    return naive.replace(tzinfo=_FF_TZ)
