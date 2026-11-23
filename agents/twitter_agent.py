"""Twitter/X sentiment ingestion via RapidAPI."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config

log = logging.getLogger(__name__)

_HEADERS = {
    "x-rapidapi-key": config.RAPIDAPI_KEY,
    "x-rapidapi-host": config.RAPIDAPI_HOST,
    "Content-Type": "application/json",
}


def _parse_created_at(raw: dict) -> datetime | None:
    """Parse a tweet timestamp from any of the fields RapidAPI may use."""
    for field in ("created_at", "tweet_created_at", "created", "date"):
        v = raw.get(field)
        if not v:
            continue
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
            except Exception:
                continue
        if isinstance(v, str):
            # Twitter classic: "Wed Oct 10 20:19:24 +0000 2018"
            for fmt in (
                "%a %b %d %H:%M:%S %z %Y",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(v, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except Exception:
                    continue
    return None


def _normalize(raw: dict) -> dict | None:
    tid = raw.get("tweet_id") or raw.get("id")
    if not tid:
        return None
    author = raw.get("author") or {}
    return {
        "tweet_id": str(tid),
        "author_handle": raw.get("screen_name") or author.get("screen_name") or "",
        "text": raw.get("text") or raw.get("full_text") or "",
        "lang": raw.get("lang") or "en",
        "created_at": _parse_created_at(raw),
        "favorites": int(raw.get("favorites") or raw.get("favorite_count") or 0),
        "retweets": int(raw.get("retweets") or raw.get("retweet_count") or 0),
        "views": int((raw.get("views") or {}).get("count", 0)
                     if isinstance(raw.get("views"), dict)
                     else raw.get("views") or 0),
        "replies": int(raw.get("replies") or raw.get("reply_count") or 0),
    }


def search_tweets(query: str, cursor: str | None = None) -> dict[str, Any]:
    """Search Latest tweets for a query string."""
    if not config.RAPIDAPI_KEY:
        log.warning("RAPIDAPI_KEY not set — twitter_agent disabled.")
        return {"tweets": [], "next_cursor": None}

    params = {"query": f"{query} -filter:replies", "search_type": "Latest"}
    if cursor:
        params["cursor"] = cursor
    try:
        r = requests.get(
            f"{config.RAPIDAPI_BASE_URL}/search.php",
            headers=_HEADERS, params=params, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("RapidAPI search failed: %s", e)
        return {"tweets": [], "next_cursor": None}

    raw_list = data.get("timeline") or data.get("tweets") or []
    tweets = [t for t in (_normalize(x) for x in raw_list) if t]
    return {"tweets": tweets, "next_cursor": data.get("next_cursor")}


def get_user_timeline(screen_name: str) -> list[dict]:
    if not config.RAPIDAPI_KEY:
        return []
    try:
        r = requests.get(
            f"{config.RAPIDAPI_BASE_URL}/timeline.php",
            headers=_HEADERS, params={"screenname": screen_name}, timeout=15,
        )
        r.raise_for_status()
        raw_list = r.json().get("timeline") or []
    except Exception as e:
        log.error("RapidAPI timeline failed: %s", e)
        return []
    return [t for t in (_normalize(x) for x in raw_list) if t]


def get_trending(woeid: str = "1") -> list[str]:
    """GET /trending.php — list of trending topic names (woeid=1 → global)."""
    if not config.RAPIDAPI_KEY:
        return []
    try:
        r = requests.get(
            f"{config.RAPIDAPI_BASE_URL}/trends.php",
            headers=_HEADERS, params={"woeid": woeid}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("RapidAPI trends failed: %s", e)
        return []
    trends = data.get("trends") or data.get("data") or []
    names: list[str] = []
    for t in trends:
        if isinstance(t, dict):
            n = t.get("name") or t.get("query") or t.get("topic")
            if n:
                names.append(str(n))
        elif isinstance(t, str):
            names.append(t)
    return names


def get_top_posts(country: str = "USA", period: str = "Daily",
                  rank_by: str = "Likes") -> list[dict]:
    """GET /top_posts.php — viral posts by likes/retweets/views."""
    if not config.RAPIDAPI_KEY:
        return []
    try:
        r = requests.get(
            f"{config.RAPIDAPI_BASE_URL}/top_posts.php",
            headers=_HEADERS,
            params={"country": country, "period": period, "type": rank_by},
            timeout=15,
        )
        r.raise_for_status()
        raw_list = r.json().get("timeline") or []
    except Exception as e:
        log.error("RapidAPI top_posts failed: %s", e)
        return []
    return [t for t in (_normalize(x) for x in raw_list) if t]


# =================================================================
class TwitterAgent(threading.Thread):
    """Polls Twitter for cashtag queries, scores sentiment, writes to DB.

    Token cost is bounded by:
      • `poll_interval` (default 300 s = 5 min between API calls)
      • `max_items_per_tick` (default 8 tweets scored per cycle)
      • a batch scorer that bundles N items into one Haiku call (~6x cheaper)
      • a pre-filter that drops trivially-short/URL-only tweets BEFORE LLM call
    """

    def __init__(
        self,
        db,
        log_queue,
        stop_event: threading.Event,
        sentiment_scorer,
        symbols_getter,
        sentiment_batch_scorer=None,
        vip_handles: list[str] | None = None,
        poll_interval: int = 300,             # was 180
        max_items_per_tick: int = 8,          # was 25
    ) -> None:
        super().__init__(name="TwitterAgent", daemon=True)
        self.db = db
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.score = sentiment_scorer                  # single-item fallback
        self.score_batch = sentiment_batch_scorer      # preferred
        self.symbols_getter = symbols_getter
        self.vip_handles = vip_handles or []
        # Round-robin cursor so every VIP gets scanned over the course of
        # ceil(len(vip_handles) / vip_batch_size) cycles instead of being
        # stuck on whichever 3 happened to be at the head of the list.
        self._vip_cursor = 0
        self._vip_batch_size = 3
        self.poll_interval = poll_interval
        self.max_items_per_tick = max_items_per_tick
        self._seen: set[str] = set()

    def _log(self, text: str, tag: str = "SENTIMENT") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def run(self) -> None:
        if not config.RAPIDAPI_KEY:
            self._log("Twitter disabled (RAPIDAPI_KEY missing)", tag="ERROR")
            return

        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log(f"twitter tick error: {e}", tag="ERROR")
            self.stop_event.wait(self.poll_interval)

    def _tick(self) -> None:
        symbols = self.symbols_getter() or []
        if not symbols:
            return

        try:
            max_age = float(self.db.get_setting("tweet_max_age_min", 30.0) or 30.0)
        except Exception:
            max_age = 30.0
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age)

        cashtag_query = " OR ".join(f"${s.replace('/USD', '')}" for s in symbols[:8])
        res = search_tweets(cashtag_query)
        candidates: list[tuple[dict, str]] = []  # (tweet, attributed_symbol)
        stale_count = 0
        filtered_trivial = 0

        for t in res["tweets"]:
            if len(candidates) >= self.max_items_per_tick:
                break
            if t["tweet_id"] in self._seen:
                continue
            self._seen.add(t["tweet_id"])
            if not _is_fresh(t, cutoff):
                stale_count += 1
                continue
            if not _is_meaningful(t["text"]):
                filtered_trivial += 1
                continue
            sym = _extract_cashtag(t["text"], symbols) or symbols[0]
            candidates.append((t, sym))

        # VIP handles — round-robin slice so the entire list is covered
        # over time instead of always polling the same first 3 names.
        vips = self.vip_handles
        if vips:
            start = self._vip_cursor % len(vips)
            end = start + self._vip_batch_size
            vip_batch = (vips + vips)[start:end]  # wrap around
            self._vip_cursor = (self._vip_cursor + self._vip_batch_size) % len(vips)
        else:
            vip_batch = []
        for handle in vip_batch:
            if len(candidates) >= self.max_items_per_tick:
                break
            timeline = get_user_timeline(handle)
            for t in timeline[:3]:
                if len(candidates) >= self.max_items_per_tick:
                    break
                if t["tweet_id"] in self._seen:
                    continue
                self._seen.add(t["tweet_id"])
                if not _is_fresh(t, cutoff):
                    stale_count += 1
                    continue
                if not _is_meaningful(t["text"]):
                    filtered_trivial += 1
                    continue
                sym = _extract_cashtag(t["text"], symbols) or symbols[0]
                candidates.append((t, sym))

        # ---- ONE Haiku call for the whole batch
        new_count = 0
        if candidates:
            texts = [t["text"] for t, _ in candidates]
            try:
                if self.score_batch is not None:
                    scored = self.score_batch(texts)
                else:
                    scored = [self.score(t) for t in texts]
            except Exception as e:
                self._log(f"batch sentiment scoring failed: {e}", tag="ERROR")
                scored = [(0.0, t[:120]) for t in texts]
            for (tweet, sym), (score, summary) in zip(candidates, scored):
                try:
                    self.db.insert_sentiment(
                        "TWITTER", sym, score,
                        summary or tweet["text"][:240],
                    )
                    new_count += 1
                except Exception as e:
                    self._log(f"DB sentiment insert failed: {e}", tag="ERROR")

        if new_count or stale_count or filtered_trivial:
            self._log(
                f"ingested {new_count} tweets (1 LLM call), "
                f"stale={stale_count}, trivial={filtered_trivial}"
            )

        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2000:])


def _is_meaningful(text: str) -> bool:
    """Pre-filter trivial tweets before spending tokens on them.

    Drops: empty / mostly-URL / mostly-mentions / under 25 alphanumeric chars.
    """
    if not text:
        return False
    # Strip URLs and @mentions to gauge actual content
    body = re.sub(r"https?://\S+", "", text)
    body = re.sub(r"@\w+", "", body)
    alpha = re.sub(r"[^A-Za-z0-9]", "", body)
    return len(alpha) >= 25


def _is_fresh(tweet: dict, cutoff: datetime) -> bool:
    """True iff tweet has a parseable timestamp newer than cutoff.

    Tweets without any timestamp are treated as stale to avoid stale data
    leaking into intraday signals.
    """
    ts = tweet.get("created_at")
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= cutoff


def _extract_cashtag(text: str, symbols: list[str]) -> str | None:
    up = text.upper()
    for s in symbols:
        bare = s.replace("/USD", "")
        if f"${bare}" in up or f" {bare} " in up:
            return s
    return None


# ---- CLI smoke test -----------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="$BTC")
    args = parser.parse_args()
    res = search_tweets(args.query)
    for t in res["tweets"][:5]:
        print(f"@{t['author_handle']}  ❤️{t['favorites']}  ♻{t['retweets']}")
        print(f"  {t['text'][:200]}")
