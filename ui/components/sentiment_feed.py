"""Live sentiment feed — latest tweets + news with score, source, age.

Animations:
- newly-arrived rows briefly pulse with an accent border
- the "age" column ticks every second ("just now" → "12s" → "3m")
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import customtkinter as ctk

from ui import theme

SOURCE_LABEL = {
    "TWITTER":  "X",
    "NEWSNOW":  "NEWS",
}


class SentimentFeed(ctk.CTkFrame):
    def __init__(self, master, **kwargs) -> None:
        p = theme.palette()
        super().__init__(
            master,
            fg_color=p.panel,
            border_color=p.border,
            border_width=1,
            corner_radius=12,
            **kwargs,
        )
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header with live indicator
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        self._dot = ctk.CTkLabel(
            header, text="●", text_color=p.profit,
            font=ctk.CTkFont(size=14),
        )
        self._dot.pack(side="left", padx=(0, 6))
        self._title = ctk.CTkLabel(
            header, text="MARKET PULSE  •  TWITTER + NEWS",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._title.pack(side="left")
        self._count_lbl = ctk.CTkLabel(
            header, text="", text_color=p.text_muted,
            font=ctk.CTkFont(size=11),
        )
        self._count_lbl.pack(side="right")

        # Scrollable feed
        self._scroll = ctk.CTkScrollableFrame(
            self, fg_color=p.panel, corner_radius=6,
            scrollbar_button_color=p.border,
        )
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        self._rows: dict[str, "SentimentRow"] = {}   # signature -> row
        self._row_order: list[str] = []              # newest first
        self._newest_ts: datetime | None = None

        # Empty state
        self._empty = ctk.CTkLabel(
            self._scroll,
            text="Live Twitter & news sentiment feed will appear here once the bot is running...",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=12),
        )
        self._empty.pack(pady=30)

        # Live-tick the relative ages + header age
        self._tick_ages()
        self._tick_header_age()
        # Pulse the LIVE dot (paused when data is stale)
        self._pulse_dot()

        theme.on_theme_change(self._apply_theme)

    # ------------------------------------------------------------------
    def update_feed(self, entries: Iterable[dict]) -> None:
        """Cheap update: when item set is identical → no-op (header ticks
        separately). When new items arrive → only pack_forget+repack existing
        rows + create only the new ones. No flicker, no spurious redraws.
        """
        entries = list(entries)
        if entries and self._empty is not None:
            try:
                self._empty.destroy()
            except Exception:
                pass
            self._empty = None

        newest_ts: datetime | None = None
        new_sigs: list[str] = []
        sig_to_entry: dict[str, dict] = {}
        for e in entries:
            sig = _signature(e)
            new_sigs.append(sig)
            sig_to_entry[sig] = e
            ts = _ensure_utc(e.get("timestamp"))
            if ts and (newest_ts is None or ts > newest_ts):
                newest_ts = ts
        self._newest_ts = newest_ts

        # FAST PATH: nothing new, same order → just header ticks
        if new_sigs == self._row_order:
            return

        new_set = set(new_sigs)
        previous_sigs = set(self._rows.keys())

        # destroy gone
        for old_sig in list(self._rows.keys()):
            if old_sig not in new_set:
                try:
                    self._rows[old_sig].destroy()
                except Exception:
                    pass
                del self._rows[old_sig]

        # only repack if order changed
        order_changed = (self._row_order != new_sigs)
        if order_changed:
            for row in self._rows.values():
                try:
                    row.pack_forget()
                except Exception:
                    pass

        self._row_order = list(new_sigs)
        for sig in new_sigs:
            row = self._rows.get(sig)
            if row is None:
                row = SentimentRow(self._scroll, sig_to_entry[sig])
                self._rows[sig] = row
                row.pack(fill="x", padx=2, pady=2)
                if sig not in previous_sigs:
                    row.pulse_in()
            elif order_changed:
                row.pack(fill="x", padx=2, pady=2)

    def _refresh_header_age(self) -> None:
        p = theme.palette()
        if self._newest_ts is None:
            self._count_lbl.configure(
                text=f"{len(self._rows)} items",
                text_color=p.text_muted,
            )
            return
        delta = (datetime.now(timezone.utc) - self._newest_ts).total_seconds()
        age = _format_age(delta)
        is_stale = delta > 300  # > 5 minutes
        color = p.loss if is_stale else p.profit
        self._count_lbl.configure(
            text=f"{len(self._rows)} • newest {age}",
            text_color=color,
        )

    def _tick_header_age(self) -> None:
        self._refresh_header_age()
        self.after(1000, self._tick_header_age)

    # ------------------------------------------------------------------
    def _tick_ages(self) -> None:
        for row in self._rows.values():
            row.refresh_age()
        self.after(1000, self._tick_ages)

    def _pulse_dot(self) -> None:
        p = theme.palette()
        # If data is fresh, pulse green/muted. If stale, keep loss color solid.
        is_stale = False
        if self._newest_ts is not None:
            age = (datetime.now(timezone.utc) - self._newest_ts).total_seconds()
            is_stale = age > 300
        if is_stale:
            self._dot.configure(text_color=p.loss)
        else:
            self._dot_on = not getattr(self, "_dot_on", False)
            self._dot.configure(text_color=p.profit if self._dot_on else p.text_muted)
        self.after(900, self._pulse_dot)

    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.panel, border_color=p.border)
        self._title.configure(text_color=p.text_muted)
        self._count_lbl.configure(text_color=p.text_muted)
        self._scroll.configure(fg_color=p.panel, scrollbar_button_color=p.border)
        if self._empty is not None:
            self._empty.configure(text_color=p.text_muted)


# ====================================================================
class SentimentRow(ctk.CTkFrame):
    """One feed row: source badge | symbol | score | summary | age."""

    def __init__(self, master, entry: dict) -> None:
        p = theme.palette()
        score = float(entry.get("sentiment_score") or 0)
        if score > 0.15:
            color = p.profit
            bg = _solid(p.profit_soft, fallback=p.panel_alt)
        elif score < -0.15:
            color = p.loss
            bg = _solid(p.loss_soft, fallback=p.panel_alt)
        else:
            color = p.text_muted
            bg = p.panel_alt

        super().__init__(master, fg_color=bg, corner_radius=8,
                         border_color=bg, border_width=1)
        self._base_color = bg
        self._timestamp = _ensure_utc(entry.get("timestamp"))

        self.grid_columnconfigure(3, weight=1)

        source = (entry.get("source") or "").upper()
        source_label = SOURCE_LABEL.get(source, source[:5])
        self._source = ctk.CTkLabel(
            self, text=f" {source_label} ", text_color="white",
            fg_color=p.accent_soft if source == "TWITTER" else p.accent,
            corner_radius=4, width=44, height=20,
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        self._source.grid(row=0, column=0, padx=(10, 6), pady=8, sticky="w")

        self._symbol = ctk.CTkLabel(
            self, text=str(entry.get("symbol", "")),
            text_color=p.text, width=70,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._symbol.grid(row=0, column=1, padx=4, pady=8, sticky="w")

        sign = "+" if score >= 0 else ""
        self._score = ctk.CTkLabel(
            self, text=f"{sign}{score:.2f}",
            text_color=color, width=50,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._score.grid(row=0, column=2, padx=4, pady=8)

        summary = (entry.get("summary") or "")[:180]
        self._summary = ctk.CTkLabel(
            self, text=summary, text_color=p.text,
            font=ctk.CTkFont(size=11),
            anchor="w", justify="left",
        )
        self._summary.grid(row=0, column=3, padx=8, pady=8, sticky="ew")

        self._age = ctk.CTkLabel(
            self, text="just now",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=10),
            width=60, anchor="e",
        )
        self._age.grid(row=0, column=4, padx=(4, 10), pady=8, sticky="e")
        self.refresh_age()

    # ------------------------------------------------------------------
    def refresh_age(self) -> None:
        if self._timestamp is None:
            return
        now = datetime.now(timezone.utc)
        delta = (now - self._timestamp).total_seconds()
        if delta < 60:
            text = f"{int(delta)}s"
        elif delta < 3600:
            text = f"{int(delta / 60)}m"
        elif delta < 86400:
            text = f"{int(delta / 3600)}h"
        else:
            text = f"{int(delta / 86400)}d"
        self._age.configure(text=text)

    def pulse_in(self) -> None:
        """Brief accent border flash when row first appears."""
        p = theme.palette()
        self.configure(border_color=p.accent)
        self.after(800, lambda: self.configure(border_color=self._base_color))


# ====================================================================
def _format_age(seconds: float) -> str:
    """Compact human-readable age: 12s, 3m, 2h, 1d."""
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s / 60)}m"
    if s < 86400:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


def _signature(entry: dict) -> str:
    ts = entry.get("timestamp")
    ts_s = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    src = entry.get("source", "")
    sym = entry.get("symbol", "")
    summ = (entry.get("summary") or "")[:60]
    return f"{ts_s}|{src}|{sym}|{summ}"


def _solid(hex_color: str, *, fallback: str = "#222222") -> str:
    """Convert #RRGGBBAA → solid #RRGGBB by blending towards the active mode's
    base panel. This keeps the subtle green/red row tint vibrant but avoids
    Tk rejecting 8-hex strings.
    """
    if not hex_color:
        return fallback
    if len(hex_color) != 9 or not hex_color.startswith("#"):
        return hex_color
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        a = int(hex_color[7:9], 16) / 255.0
    except ValueError:
        return fallback
    # Blend over fallback (the panel background) using the alpha
    try:
        fr = int(fallback[1:3], 16)
        fg = int(fallback[3:5], 16)
        fb = int(fallback[5:7], 16)
    except (ValueError, IndexError):
        fr, fg, fb = 26, 31, 46
    rr = int(fr * (1 - a) + r * a)
    gg = int(fg * (1 - a) + g * a)
    bb = int(fb * (1 - a) + b * a)
    return f"#{rr:02X}{gg:02X}{bb:02X}"


def _ensure_utc(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None
