"""Left-column body. Two-column grid:

  Row 0 (3w)  Watchlist (col0, rowspan=2)   |  Sentiment feed (col1)
  Row 1 (3w)  ↑ continues                    |  Equity curve chart
  Row 2 (2w)  Live console                   |  Macro calendar

Watchlist gets the tall left run; sentiment + chart share the right run
(swapped with the right-column donut per user request); console + calendar
share the bottom strip so the macro calendar sits visually next to the
LIVE OPERATIONS feed.
"""
from __future__ import annotations

import queue
from typing import Callable

import customtkinter as ctk

from ui import theme
from ui.components.chart_canvas import ChartCanvas
from ui.components.economic_calendar_panel import EconomicCalendarPanel
from ui.components.live_console import LiveConsole
from ui.components.sentiment_feed import SentimentFeed
from ui.components.watchlist_panel import WatchlistPanel


class StatsPanel(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        log_queue: queue.Queue,
        get_macro_events: Callable[[int], list[dict]] | None = None,
        on_period_change: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        p = theme.palette()
        super().__init__(master, fg_color=p.bg, **kwargs)

        self.grid_columnconfigure(0, weight=1, uniform="cols")
        self.grid_columnconfigure(1, weight=1, uniform="cols")
        self.grid_rowconfigure(0, weight=3)   # sentiment top
        self.grid_rowconfigure(1, weight=3, minsize=180)   # chart row
        self.grid_rowconfigure(2, weight=2)   # console / calendar strip

        self.watchlist = WatchlistPanel(self)
        self.watchlist.grid(row=0, column=0, rowspan=2,
                            padx=(0, 4), pady=(0, 4), sticky="nsew")

        self.sentiment_feed = SentimentFeed(self)
        self.sentiment_feed.grid(row=0, column=1, padx=(4, 0), pady=(0, 4),
                                 sticky="nsew")

        self.chart = ChartCanvas(self)
        if on_period_change is not None:
            self.chart.set_period_callback(on_period_change)
        self.chart.grid(row=1, column=1, padx=(4, 0), pady=(4, 4),
                        sticky="nsew")

        self.console = LiveConsole(self, log_queue=log_queue)
        self.console.grid(row=2, column=0, padx=(0, 4), pady=(4, 0),
                          sticky="nsew")

        # Calendar sits next to the console. We own it (parent=self) so
        # grid_remove on the StatsPanel hides everything atomically.
        if get_macro_events is not None:
            self.calendar = EconomicCalendarPanel(self, get_events=get_macro_events)
            self.calendar.grid(row=2, column=1, padx=(4, 0), pady=(4, 0),
                               sticky="nsew")
        else:
            self.calendar = None
            self.console.grid_configure(columnspan=2, padx=(0, 0))
