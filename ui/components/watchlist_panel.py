"""Live portfolio / watchlist panel — symbol rows with price, %change, sparkline."""
from __future__ import annotations

from typing import Sequence

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ui import theme


class WatchlistPanel(ctk.CTkFrame):
    """Renders 10-ish symbol rows. Call `update_snapshots(data)` periodically."""

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
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self._title = ctk.CTkLabel(
            header, text="WATCHLIST  •  LIVE",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._title.pack(side="left")

        # Column headers
        col_row = ctk.CTkFrame(self, fg_color=p.panel_alt, corner_radius=6)
        col_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 2))
        col_row.grid_columnconfigure(3, weight=1)
        for i, (txt, w, anchor) in enumerate([
            ("SYMBOL",      120, "w"),
            ("PRICE",       130, "e"),
            ("CHG",         110, "e"),
            ("",            200, "w"),  # sparkline space
        ]):
            ctk.CTkLabel(
                col_row, text=txt, width=w, anchor=anchor,
                text_color=p.text_muted,
                font=ctk.CTkFont(size=10, weight="bold"),
            ).grid(row=0, column=i, padx=8, pady=6, sticky="ew")

        # Scrollable list
        self._scroll = ctk.CTkScrollableFrame(
            self, fg_color=p.panel, corner_radius=6,
            scrollbar_button_color=p.border,
        )
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.grid_rowconfigure(2, weight=1)

        self._rows: dict[str, "WatchlistRow"] = {}

        # Empty-state placeholder until first data arrives
        self._empty_label = ctk.CTkLabel(
            self._scroll,
            text="Portfolio prices will appear here once the bot is started...",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=12),
        )
        self._empty_label.pack(pady=40)

        theme.on_theme_change(self._apply_theme)

    # ------------------------------------------------------------------
    def update_snapshots(self, snapshots: Sequence[dict]) -> None:
        """`snapshots` items: {symbol, price, change_pct, history}."""
        valid = [s for s in snapshots if s.get("price") is not None]
        if valid and self._empty_label is not None:
            self._empty_label.destroy()
            self._empty_label = None

        seen = set()
        for snap in valid:
            sym = snap["symbol"]
            seen.add(sym)
            row = self._rows.get(sym)
            if row is None:
                row = WatchlistRow(self._scroll, symbol=sym)
                row.pack(fill="x", pady=2, padx=2)
                self._rows[sym] = row
            row.set_data(snap["price"], snap["change_pct"], snap["history"])

        # remove rows that aren't in current watchlist anymore
        for old in list(self._rows.keys()):
            if old not in seen:
                self._rows[old].destroy()
                del self._rows[old]

    # ------------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.panel, border_color=p.border)
        self._title.configure(text_color=p.text_muted)
        self._scroll.configure(fg_color=p.panel, scrollbar_button_color=p.border)
        if self._empty_label is not None:
            self._empty_label.configure(text_color=p.text_muted)


# ====================================================================
class WatchlistRow(ctk.CTkFrame):
    """Single watchlist row: symbol · price · change · sparkline."""

    def __init__(self, master, *, symbol: str) -> None:
        p = theme.palette()
        super().__init__(master, fg_color="transparent")
        self.symbol = symbol

        self.grid_columnconfigure(3, weight=1)

        # symbol with leading color dot
        sym_box = ctk.CTkFrame(self, fg_color="transparent")
        sym_box.grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self._dot = ctk.CTkLabel(
            sym_box, text="●", text_color=p.accent,
            font=ctk.CTkFont(size=14),
        )
        self._dot.pack(side="left", padx=(0, 6))
        self._symbol_lbl = ctk.CTkLabel(
            sym_box, text=symbol, text_color=p.text,
            font=ctk.CTkFont(size=13, weight="bold"),
            width=92, anchor="w",
        )
        self._symbol_lbl.pack(side="left")

        self._price_lbl = ctk.CTkLabel(
            self, text="—", text_color=p.text,
            font=ctk.CTkFont(size=14, weight="bold"),
            width=130, anchor="e",
        )
        self._price_lbl.grid(row=0, column=1, padx=8, pady=6, sticky="e")

        # change cell: arrow + percent
        chg_box = ctk.CTkFrame(self, fg_color="transparent")
        chg_box.grid(row=0, column=2, padx=8, pady=6, sticky="e")
        self._arrow = ctk.CTkLabel(
            chg_box, text="→", text_color=p.text_muted,
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self._arrow.pack(side="left", padx=(0, 4))
        self._chg_lbl = ctk.CTkLabel(
            chg_box, text="0.00%", text_color=p.text_muted,
            font=ctk.CTkFont(size=13, weight="bold"),
            width=70, anchor="e",
        )
        self._chg_lbl.pack(side="left")

        # mini matplotlib sparkline
        self._fig = Figure(figsize=(2.4, 0.4), dpi=100)
        self._fig.patch.set_facecolor(p.panel)
        self._ax = self._fig.add_subplot(111)
        self._ax.axis("off")
        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=self)
        widget = self._mpl_canvas.get_tk_widget()
        widget.configure(highlightthickness=0, bg=p.panel)
        widget.grid(row=0, column=3, padx=8, pady=4, sticky="ew")

        # Track last sparkline state to skip needless redraws (which cause flicker)
        self._last_hist_sig: tuple | None = None
        self._last_spark_color: str | None = None

        theme.on_theme_change(self._apply_theme)

    # ------------------------------------------------------------------
    def set_data(self, price: float, change_pct: float, history: list[float]) -> None:
        """Update the numeric/colour labels.  Sparkline canvas is redrawn ONLY
        when the underlying history actually changed, so the user doesn't see
        flicker on every 5-second refresh tick.
        """
        p = theme.palette()
        self._price_lbl.configure(text=f"${price:,.{_decimals_for_price(price)}f}")

        # arrow + color based on change_pct
        if change_pct is None:
            arrow_txt, color = "→", p.text_muted
            chg_text = "—"
        elif change_pct > 0.10:
            arrow_txt, color = "▲", p.profit
            chg_text = f"+{change_pct:.2f}%"
        elif change_pct < -0.10:
            arrow_txt, color = "▼", p.loss
            chg_text = f"{change_pct:.2f}%"
        else:
            arrow_txt, color = "→", p.text_muted
            chg_text = f"{change_pct:+.2f}%"

        self._arrow.configure(text=arrow_txt, text_color=color)
        self._chg_lbl.configure(text=chg_text, text_color=color)
        self._dot.configure(text_color=color)

        # Sparkline — only redraw when data/color actually changed
        hist_sig = tuple(history) if history else ()
        if hist_sig != self._last_hist_sig or color != self._last_spark_color:
            self._last_hist_sig = hist_sig
            self._last_spark_color = color
            self._ax.clear()
            self._ax.axis("off")
            if history and len(history) >= 2:
                xs = range(len(history))
                self._ax.plot(xs, history, color=color, linewidth=1.4)
                self._ax.fill_between(
                    xs, history, min(history),
                    color=color, alpha=0.15,
                )
            self._fig.patch.set_facecolor(p.panel)
            self._mpl_canvas.draw_idle()

    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self._symbol_lbl.configure(text_color=p.text)
        self._price_lbl.configure(text_color=p.text)
        # re-tint sparkline canvas bg
        self._fig.patch.set_facecolor(p.panel)
        self._mpl_canvas.get_tk_widget().configure(bg=p.panel)
        self._mpl_canvas.draw_idle()


def _decimals_for_price(p: float) -> int:
    if p is None or p == 0:
        return 2
    if p >= 1000:
        return 2
    if p >= 1:
        return 3
    return 5
