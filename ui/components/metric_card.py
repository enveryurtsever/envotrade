"""Single metric card: icon + label + value + sparkline."""
from __future__ import annotations

from typing import Sequence

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ui import theme


class MetricCard(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        icon_name: str,
        title: str,
        value: str = "—",
        accent: str | None = None,
        sparkline: bool = False,
        **kwargs,
    ) -> None:
        p = theme.palette()
        super().__init__(
            master,
            fg_color=p.panel,
            border_color=p.border,
            border_width=1,
            corner_radius=10,
            **kwargs,
        )
        self._accent_override = accent
        self._icon_name = icon_name
        self._hidden = False
        self._real_value = value
        self._real_sparkline: list[float] = []
        self._show_sparkline = sparkline

        # Layout: title at row 0, value at row 1, sparkline (optional) at
        # row 2, spacer at row 3 that absorbs any extra vertical space.
        # This keeps title+value aligned to the TOP across every card,
        # regardless of whether a sparkline is present.
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        accent_color = accent or p.accent
        self._icon_label = ctk.CTkLabel(
            self,
            text="",
            image=theme.load_colored_icon(icon_name, _hex_to_rgb(accent_color), size=22),
            width=32,
        )
        self._icon_label.grid(row=0, column=0, rowspan=2, padx=(10, 4), pady=(10, 0), sticky="n")

        self._title_label = ctk.CTkLabel(
            self, text=title.upper(),
            text_color=p.text_muted,
            font=ctk.CTkFont(size=9, weight="bold"),
            anchor="w",
        )
        self._title_label.grid(row=0, column=1, sticky="nw", padx=(0, 8), pady=(10, 0))

        self._value_label = ctk.CTkLabel(
            self, text=value,
            text_color=p.text,
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        )
        self._value_label.grid(row=1, column=1, sticky="nw", padx=(0, 8), pady=(2, 4))

        # sparkline (optional) — pinned to bottom via row 3 spacer above
        if self._show_sparkline:
            self._fig = Figure(figsize=(2.0, 0.35), dpi=100)
            self._fig.patch.set_facecolor(p.panel)
            self._ax = self._fig.add_subplot(111)
            self._ax.axis("off")
            self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=self)
            self._mpl_canvas.get_tk_widget().configure(highlightthickness=0, bg=p.panel)
            self._mpl_canvas.get_tk_widget().grid(
                row=2, column=0, columnspan=2, padx=10, pady=(4, 8), sticky="sew",
            )
        else:
            self._fig = None
            self._ax = None
            self._mpl_canvas = None

        theme.on_theme_change(self._apply_theme)

    # -----------------------------------------------------------
    def set_value(self, value: str) -> None:
        self._real_value = value
        if not self._hidden:
            self._value_label.configure(text=value)

    def set_sparkline(self, values: Sequence[float]) -> None:
        self._real_sparkline = list(values)
        if self._mpl_canvas is None:
            return
        if not self._hidden:
            self._draw_sparkline(self._real_sparkline)

    def _draw_sparkline(self, values: Sequence[float]) -> None:
        if self._mpl_canvas is None or self._ax is None or self._fig is None:
            return
        p = theme.palette()
        accent_color = self._accent_override or p.accent
        self._ax.clear()
        self._ax.axis("off")
        if values and len(values) > 1:
            xs = list(range(len(values)))
            self._ax.plot(xs, values, color=accent_color, linewidth=1.5)
            self._ax.fill_between(xs, values, min(values), color=accent_color, alpha=0.15)
        self._fig.patch.set_facecolor(p.panel)
        self._mpl_canvas.draw_idle()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._value_label.configure(text="••••••")
            if self._mpl_canvas is not None and self._ax is not None and self._fig is not None:
                self._ax.clear()
                self._ax.axis("off")
                self._fig.patch.set_facecolor(theme.palette().panel)
                self._mpl_canvas.draw_idle()
        else:
            self._value_label.configure(text=self._real_value)
            if self._real_sparkline:
                self._draw_sparkline(self._real_sparkline)

    # -----------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        accent_color = self._accent_override or p.accent
        self.configure(fg_color=p.panel, border_color=p.border)
        self._title_label.configure(text_color=p.text_muted)
        self._value_label.configure(text_color=p.text)
        self._icon_label.configure(
            image=theme.load_colored_icon(self._icon_name, _hex_to_rgb(accent_color), size=22)
        )
        if self._fig is not None and self._mpl_canvas is not None:
            self._fig.patch.set_facecolor(p.panel)
            self._mpl_canvas.get_tk_widget().configure(bg=p.panel)
            self._mpl_canvas.draw_idle()


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
