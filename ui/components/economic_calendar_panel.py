"""Compact economic-calendar list — top 3 upcoming high-impact events.

Data is pulled from `agents.economic_calendar.EconomicCalendar`. The panel
just polls `get_upcoming()` on a 60s tick and re-renders rows in place to
avoid Tk flicker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import customtkinter as ctk

from ui import theme

_FLAG_BY_COUNTRY: dict[str, str] = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "CHF": "🇨🇭", "AUD": "🇦🇺", "CAD": "🇨🇦", "NZD": "🇳🇿",
    "CNY": "🇨🇳",
}


class EconomicCalendarPanel(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        get_events: Callable[[int], list[dict]],
        refresh_ms: int = 60_000,
        **kwargs,
    ) -> None:
        p = theme.palette()
        super().__init__(
            master,
            fg_color=p.panel, border_color=p.border, border_width=1,
            corner_radius=12, **kwargs,
        )
        self._get_events = get_events
        self._refresh_ms = refresh_ms

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        self._title = ctk.CTkLabel(
            header, text="MACRO CALENDAR",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._title.pack(side="left")
        self._badge = ctk.CTkLabel(
            header, text="HIGH IMPACT",
            text_color=p.loss, fg_color=p.panel_alt, corner_radius=4,
            font=ctk.CTkFont(size=9, weight="bold"),
            padx=6, pady=2,
        )
        self._badge.pack(side="right")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        body.grid_columnconfigure(0, weight=1)
        self._body = body

        self._empty = ctk.CTkLabel(
            body, text="No high-impact events this week.",
            text_color=p.text_muted, font=ctk.CTkFont(size=10),
        )
        self._empty.pack(pady=8)
        self._rows: list[ctk.CTkFrame] = []
        self._last_sig: tuple | None = None

        theme.on_theme_change(self._apply_theme)
        self.after(500, self._tick)

    # ---------------------------------------------------------------
    def _tick(self) -> None:
        try:
            events = self._get_events(3) or []
        except Exception:
            events = []
        self._render(events)
        self.after(self._refresh_ms, self._tick)

    def _render(self, events: list[dict]) -> None:
        p = theme.palette()
        sig = tuple(
            (e.get("title"), e.get("country"), e.get("dt_utc"))
            for e in events
        )
        if sig == self._last_sig:
            return
        self._last_sig = sig

        for row in self._rows:
            try:
                row.destroy()
            except Exception:
                pass
        self._rows.clear()

        if not events:
            try:
                self._empty.pack(pady=8)
            except Exception:
                pass
            return
        try:
            self._empty.pack_forget()
        except Exception:
            pass

        for ev in events:
            self._rows.append(_make_row(self._body, ev, p))

    # ---------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.panel, border_color=p.border)
        self._title.configure(text_color=p.text_muted)
        self._empty.configure(text_color=p.text_muted)
        # Force redraw to pick up new colours
        self._last_sig = None


# ====================================================================
def _make_row(parent, ev: dict, p) -> ctk.CTkFrame:
    row = ctk.CTkFrame(parent, fg_color=p.panel_alt, corner_radius=6)
    row.pack(fill="x", pady=2)
    row.grid_columnconfigure(2, weight=1)

    flag = _FLAG_BY_COUNTRY.get(ev.get("country", ""), "🌐")
    ctk.CTkLabel(
        row, text=flag, font=ctk.CTkFont(size=14),
        width=22,
    ).grid(row=0, column=0, padx=(8, 4), pady=6)

    when = _friendly_when(ev.get("dt_utc"))
    ctk.CTkLabel(
        row, text=when, text_color=p.accent,
        font=ctk.CTkFont(size=10, weight="bold"),
        width=72, anchor="w",
    ).grid(row=0, column=1, padx=(0, 6), sticky="w")

    title = (ev.get("title") or "")[:34]
    ctk.CTkLabel(
        row, text=title, text_color=p.text,
        font=ctk.CTkFont(size=11), anchor="w",
    ).grid(row=0, column=2, padx=(0, 4), sticky="ew")

    fcst = (ev.get("forecast") or "").strip()
    prev = (ev.get("previous") or "").strip()
    tail = ""
    if fcst:
        tail = f"f:{fcst}"
        if prev:
            tail += f"  p:{prev}"
    elif prev:
        tail = f"p:{prev}"
    if tail:
        ctk.CTkLabel(
            row, text=tail, text_color=p.text_muted,
            font=ctk.CTkFont(size=10), anchor="e",
        ).grid(row=0, column=3, padx=(0, 8), sticky="e")
    return row


def _friendly_when(dt) -> str:
    """Human-readable countdown: '2h', '3d', 'now', etc."""
    if dt is None:
        return ""
    if not isinstance(dt, datetime):
        return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta < 0:
        return "live"
    if delta < 90:
        return "<1m"
    if delta < 3600:
        return f"{int(delta / 60)}m"
    if delta < 86400:
        return f"{int(delta / 3600)}h"
    return f"{int(delta / 86400)}d"
