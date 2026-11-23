"""Stocks scanner — curated mega-cap whitelist.

Mirrors engine.scanner_worker but for US equities. Mega-cap names + SPY/QQQ
ETFs so the strategy can pull index-health on the same data feed. List is
sized to match the $1k cap / $100 per trade default (≤10 concurrent).

The scanner is intentionally market-hours unaware — we keep the watchlist
published even at night so the UI shows the right names. The coordinator
gates trade decisions on `is_market_open()` in brain.stocks_strategy.
"""
from __future__ import annotations

import threading

import config


class StocksScannerWorker(threading.Thread):
    def __init__(
        self,
        log_queue,
        stop_event: threading.Event,
        on_symbols_update,
        db=None,
        interval: int | None = None,
    ) -> None:
        super().__init__(name="ScannerWorker[stock]", daemon=True)
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.on_symbols_update = on_symbols_update
        self.db = db
        self.interval = interval or config.STOCKS_SCAN_INTERVAL_SEC
        self.asset_class = "stock"
        self._current: list[str] = []

    def _log(self, text: str, tag: str = "SCANNER") -> None:
        self.log_queue.put({"tag": tag, "text": text})

    def run(self) -> None:
        self._scan_and_publish()
        while not self.stop_event.wait(self.interval):
            self._scan_and_publish()

    def _scan_and_publish(self) -> None:
        symbols = list(config.STOCKS_LIQUID_MAJORS)
        if self.db is not None:
            try:
                cap = int(
                    self.db.get_setting("stocks_max_watchlist_size", len(symbols))
                    or len(symbols)
                )
                symbols = symbols[:cap]
            except Exception:
                pass
        if symbols and symbols != self._current:
            added = sorted(set(symbols) - set(self._current))
            removed = sorted(set(self._current) - set(symbols))
            self._current = symbols
            if removed or added:
                self._log(
                    f"stocks watchlist updated ({len(symbols)} symbols)"
                    + (f" +{','.join(added)}" if added else "")
                    + (f" -{','.join(removed)}" if removed else "")
                )
            self.on_symbols_update(symbols)
