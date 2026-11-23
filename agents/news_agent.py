"""News ingestion via RSS feeds + Claude Haiku sentiment scoring."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser

import config

log = logging.getLogger(__name__)


CASHTAG_RE = re.compile(r"\b(BTC|ETH|SOL|XRP|DOGE|ADA|AVAX|LINK|TSLA|AAPL|NVDA|AMD|MSFT|GOOG|META|AMZN)\b")


class NewsAgent(threading.Thread):
    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
        sentiment_scorer,
        symbols_getter,
        sentiment_batch_scorer=None,
        poll_interval: int = 600,             # was 300 — halved cost
        max_items_per_tick: int = 6,          # cap per cycle
    ) -> None:
        super().__init__(name="NewsAgent", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.score = sentiment_scorer
        self.score_batch = sentiment_batch_scorer
        self.symbols_getter = symbols_getter
        self.poll_interval = poll_interval
        self.max_items_per_tick = max_items_per_tick
        self._seen: set[str] = set()

    def _log(self, text: str, tag: str = "SENTIMENT") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"news tick error: {e}", tag="ERROR")
            self.stop_event.wait(self.poll_interval)

    def _tick(self) -> None:
        symbols = self.symbols_getter() or []
        if not symbols:
            return

        # Live-read freshness threshold from DB
        try:
            max_age = float(self.db.get_setting("news_max_age_min", 120.0) or 120.0)
        except Exception:
            max_age = 120.0
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age)

        candidates: list[tuple[str, str, str]] = []  # (sym, title, text)
        stale_count = 0
        for url in config.NEWS_RSS_FEEDS:
            if len(candidates) >= self.max_items_per_tick:
                break
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                self._log(f"feed failed {url}: {e}", tag="ERROR")
                continue
            for entry in feed.entries[:10]:
                if len(candidates) >= self.max_items_per_tick:
                    break
                key = entry.get("id") or entry.get("link") or entry.get("title", "")
                h = hashlib.sha1(key.encode("utf-8")).hexdigest()
                if h in self._seen:
                    continue
                self._seen.add(h)

                published = _entry_timestamp(entry)
                if published is None or published < cutoff:
                    stale_count += 1
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = f"{title}. {_strip_html(summary)}"
                if not text.strip():
                    continue

                sym = _attribute_symbol(text, symbols)
                if not sym:
                    continue
                candidates.append((sym, title, text[:600]))

        # ---- ONE batched Haiku call for all candidates
        new_count = 0
        if candidates:
            texts = [t for _, _, t in candidates]
            try:
                if self.score_batch is not None:
                    scored = self.score_batch(texts)
                else:
                    scored = [self.score(t) for t in texts]
            except Exception as e:
                self._log(f"batch news scoring failed: {e}", tag="ERROR")
                scored = [(0.0, t[:120]) for t in texts]
            for (sym, title, _), (score, llm_summary) in zip(candidates, scored):
                try:
                    self.db.insert_sentiment(
                        "NEWSNOW", sym, score,
                        llm_summary or title[:240],
                    )
                    new_count += 1
                except Exception as e:
                    self._log(f"DB news insert failed: {e}", tag="ERROR")

        if new_count or stale_count:
            self._log(
                f"ingested {new_count} news (1 LLM call), stale={stale_count}, "
                f"across {len(symbols)} symbols"
            )
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2000:])


def _entry_timestamp(entry) -> datetime | None:
    """Best-effort timestamp from a feedparser entry, in UTC."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(field)
        if st:
            try:
                return datetime.fromtimestamp(mktime(st), tz=timezone.utc)
            except Exception:
                continue
    for field in ("published", "updated"):
        v = entry.get(field)
        if v:
            for fmt in (
                "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ",
            ):
                try:
                    dt = datetime.strptime(v, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except Exception:
                    continue
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def _attribute_symbol(text: str, symbols: list[str]) -> str | None:
    up = text.upper()
    for s in symbols:
        bare = s.replace("/USD", "")
        if bare in up:
            return s
    m = CASHTAG_RE.search(up)
    if m:
        match = m.group(1)
        for s in symbols:
            if s.replace("/USD", "") == match:
                return s
    return None
