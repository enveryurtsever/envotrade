"""Multi-asset scanner — curated top-liquid majors only.

Conservative-recovery mode: trade only the most liquid USD crypto pairs to
keep spreads tight, sentiment signal strong, and avoid the "warming up bars"
parade. Wider universe re-enabled later via `max_watchlist_size` setting if
performance warrants.

Rationale: a low-WR strategy bleeds extra on wide-spread alts (PAXG, HYPE,
PEPE etc.). Better to lose less on 5 quality names than more on 30 random ones.
"""
from __future__ import annotations

import threading

import config

# Curated whitelist — top USD crypto majors by liquidity & sentiment density.
LIQUID_MAJORS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD"]

# Legacy fallback — kept so other modules importing this name don't break.
DEFAULT_SYMBOLS = LIQUID_MAJORS


class ScannerWorker(threading.Thread):
    def __init__(
        self,
        log_queue,
        stop_event: threading.Event,
        on_symbols_update,
        db=None,
        interval: int | None = None,
        asset_class: str = "crypto",
    ) -> None:
        super().__init__(name=f"ScannerWorker[{asset_class}]", daemon=True)
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.on_symbols_update = on_symbols_update
        self.db = db
        self.interval = interval or config.SCAN_INTERVAL_SEC
        self.asset_class = asset_class
        self._current: list[str] = []
        self._client = None

    def _log(self, text: str, tag: str = "SCANNER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def run(self) -> None:
        # initial scan immediately
        self._scan_and_publish()
        while not self.stop_event.wait(self.interval):
            self._scan_and_publish()

    def _scan_and_publish(self) -> None:
        try:
            symbols = self._pick_symbols()
        except Exception as e:
            self._log(f"scan failed, using defaults: {e}", tag="ERROR")
            symbols = list(DEFAULT_SYMBOLS)
        if symbols and symbols != self._current:
            added = sorted(set(symbols) - set(self._current))
            removed = sorted(set(self._current) - set(symbols))
            self._current = symbols
            if removed or added:
                self._log(
                    f"watchlist updated ({len(symbols)} symbols)"
                    + (f" +{','.join(added)}" if added else "")
                    + (f" -{','.join(removed)}" if removed else "")
                )
            self.on_symbols_update(symbols)

    def _pick_symbols(self) -> list[str]:
        """Return the curated liquid-majors list, capped by `max_watchlist_size`.

        Conservative recovery mode: stay focused on names with tight spreads
        and dense sentiment data. To expand the universe later, raise the
        `max_watchlist_size` setting beyond len(LIQUID_MAJORS) and switch
        this method back to the dynamic Alpaca asset query.
        """
        max_n = len(LIQUID_MAJORS)
        if self.db is not None:
            try:
                max_n = int(
                    self.db.get_setting("max_watchlist_size", max_n) or max_n
                )
            except Exception:
                pass
        return list(LIQUID_MAJORS[:max_n])
