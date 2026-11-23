"""Equity-curve Matplotlib canvas wrapper that reacts to theme changes."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ui import theme


class ChartCanvas(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        p = theme.palette()
        super().__init__(master, fg_color=p.panel, border_color=p.border,
                         border_width=1, corner_radius=12, **kwargs)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # period selector
        self._period_var = ctk.StringVar(value="Daily")
        self._segmented = ctk.CTkSegmentedButton(
            self,
            values=["Hourly", "Daily", "Weekly"],
            variable=self._period_var,
            command=self._on_period_change,
            fg_color=p.panel_alt,
            selected_color=p.accent,
            selected_hover_color=p.accent_soft,
            unselected_color=p.panel_alt,
            unselected_hover_color=p.border,
            height=22,
            font=ctk.CTkFont(size=10),
        )
        self._segmented.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")

        self._title = ctk.CTkLabel(
            self, text="EQUITY CURVE",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=9, weight="bold"),
        )
        self._title.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="e")

        # figure
        self._fig = Figure(figsize=(8, 2.2), dpi=100)
        self._fig.patch.set_facecolor(p.panel)
        self._ax = self._fig.add_subplot(111)
        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=self)
        self._mpl_canvas.get_tk_widget().configure(highlightthickness=0, bg=p.panel)
        self._mpl_canvas.get_tk_widget().grid(row=1, column=0, padx=8, pady=(2, 8), sticky="nsew")

        self._timestamps: list[datetime] = []
        self._values: list[float] = []
        self._period_callback = None

        theme.on_theme_change(self._apply_theme)

    # -----------------------------------------------------------
    def set_period_callback(self, fn) -> None:
        self._period_callback = fn

    def _on_period_change(self, value: str) -> None:
        if self._period_callback:
            self._period_callback(value.lower())

    def get_period(self) -> str:
        return self._period_var.get().lower()

    # -----------------------------------------------------------
    def update_curve(self, timestamps: Sequence[datetime], values: Sequence[float]) -> None:
        self._timestamps = list(timestamps)
        self._values = list(values)
        self._redraw()

    def _redraw(self) -> None:
        p = theme.palette()
        self._ax.clear()
        self._fig.patch.set_facecolor(p.panel)
        self._ax.set_facecolor(p.panel)

        if not self._timestamps:
            self._ax.text(
                0.5, 0.5, "No trades yet — start the bot to populate equity curve",
                ha="center", va="center", transform=self._ax.transAxes,
                color=p.text_muted, fontsize=8,
            )
            self._ax.axis("off")
            self._mpl_canvas.draw_idle()
            return

        xs = self._timestamps
        ys = self._values
        last = ys[-1]
        color = p.profit if last >= 0 else p.loss

        self._ax.plot(xs, ys, color=color, linewidth=1.5)
        self._ax.fill_between(xs, ys, min(0, min(ys)), color=color, alpha=0.15)
        self._ax.axhline(0, color=p.border, linewidth=0.7, linestyle="--", alpha=0.6)
        self._ax.grid(True, alpha=0.2)
        self._ax.tick_params(colors=p.text_muted, labelsize=7, pad=2)
        for spine in self._ax.spines.values():
            spine.set_color(p.border)
        self._fig.autofmt_xdate()
        self._fig.tight_layout(pad=0.4)
        self._mpl_canvas.draw_idle()

    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.panel, border_color=p.border)
        self._title.configure(text_color=p.text_muted)
        self._segmented.configure(
            fg_color=p.panel_alt, selected_color=p.accent,
            selected_hover_color=p.accent_soft,
            unselected_color=p.panel_alt, unselected_hover_color=p.border,
        )
        self._mpl_canvas.get_tk_widget().configure(bg=p.panel)
        self._redraw()
