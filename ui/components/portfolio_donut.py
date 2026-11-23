"""Compact donut chart of open-position allocation.

Caller hands in a list of dicts `{symbol, market_value}`. We render a thin
donut so the symbols' slices are easy to read at small sizes. Updates are
diff-aware: we skip a redraw when the underlying allocation didn't change
(prevents matplotlib flicker on the 2s refresh tick).

The widget is a *transparent* outer frame that wraps a single visible
"card" frame. All real content sits inside that one card so CustomTkinter
can't paint multiple rounded surfaces (we hit a render bug where grid'ing
children directly into a CTkFrame with corner_radius produced phantom
sub-cards on macOS).
"""
from __future__ import annotations

from typing import Sequence

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ui import theme

# Cyan, green, amber, purple, magenta, blue, orange, lime, pink, teal.
_SLICE_COLORS = [
    "#22D3EE", "#10B981", "#F59E0B", "#A78BFA", "#EC4899",
    "#3B82F6", "#FB923C", "#84CC16", "#F472B6", "#14B8A6",
]


class PortfolioDonut(ctk.CTkFrame):
    def __init__(self, master, **kwargs) -> None:
        p = theme.palette()
        # Outer wrapper is invisible — pure layout glue between the parent
        # grid cell and the visible card below.
        super().__init__(
            master, fg_color="transparent",
            corner_radius=0, border_width=0, **kwargs,
        )
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # The ONE visible card. Everything renders inside this.
        card = ctk.CTkFrame(
            self,
            fg_color=p.panel, border_color=p.border, border_width=1,
            corner_radius=12,
        )
        card.grid(row=0, column=0, sticky="nsew")
        self._card = card

        # Card internal grid: 2 columns (donut | legend), 2 rows (header | body)
        card.grid_columnconfigure(0, weight=0, minsize=120)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(0, weight=0)
        card.grid_rowconfigure(1, weight=1)

        # ---- Header row --------------------------------------------
        self._title = ctk.CTkLabel(
            card, text="ALLOCATION",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._title.grid(row=0, column=0, sticky="w",
                         padx=(16, 0), pady=(10, 4))
        self._total_lbl = ctk.CTkLabel(
            card, text="$0.00",
            text_color=p.text,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._total_lbl.grid(row=0, column=1, sticky="e",
                             padx=(0, 16), pady=(10, 4))

        # ---- Donut (row 1 col 0) -----------------------------------
        self._fig = Figure(figsize=(1.0, 1.0), dpi=100)
        self._fig.patch.set_facecolor(p.panel)
        self._ax = self._fig.add_subplot(111)
        self._ax.axis("off")
        self._fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)
        self._canvas = FigureCanvasTkAgg(self._fig, master=card)
        widget = self._canvas.get_tk_widget()
        widget.configure(highlightthickness=0, bg=p.panel)
        widget.grid(row=1, column=0, sticky="nsew",
                    padx=(16, 4), pady=(0, 12))

        # ---- Legend (row 1 col 1) ----------------------------------
        self._legend = ctk.CTkFrame(card, fg_color=p.panel)
        self._legend.grid(row=1, column=1, sticky="nsw",
                          padx=(0, 16), pady=(0, 12))
        self._legend_rows: list[ctk.CTkFrame] = []
        self._empty_lbl: ctk.CTkLabel | None = None

        self._last_sig: tuple | None = None
        theme.on_theme_change(self._apply_theme)

    # ---------------------------------------------------------------
    def update_allocations(self, allocations: Sequence[dict]) -> None:
        valid = [
            {"symbol": a["symbol"], "value": abs(float(a.get("market_value") or 0))}
            for a in allocations
            if a.get("symbol") and float(a.get("market_value") or 0) != 0
        ]
        valid.sort(key=lambda a: a["value"], reverse=True)

        total = sum(a["value"] for a in valid)
        sig = tuple((a["symbol"], round(a["value"], 4)) for a in valid)
        if sig == self._last_sig:
            return
        self._last_sig = sig

        p = theme.palette()
        self._ax.clear()
        self._ax.axis("off")

        for row in self._legend_rows:
            try:
                row.destroy()
            except Exception:
                pass
        self._legend_rows.clear()

        if not valid or total <= 0:
            self._total_lbl.configure(text="$0.00")
            self._fig.patch.set_facecolor(p.panel)
            self._canvas.draw_idle()
            if self._empty_lbl is None:
                self._empty_lbl = ctk.CTkLabel(
                    self._legend, text="No open positions",
                    text_color=p.text_muted, font=ctk.CTkFont(size=11),
                    fg_color=p.panel,
                )
            try:
                self._empty_lbl.pack(anchor="w", pady=4)
            except Exception:
                pass
            return

        if self._empty_lbl is not None:
            try:
                self._empty_lbl.pack_forget()
            except Exception:
                pass

        sizes = [a["value"] for a in valid]
        colors = [_SLICE_COLORS[i % len(_SLICE_COLORS)] for i in range(len(valid))]
        self._ax.pie(
            sizes, colors=colors, startangle=90,
            wedgeprops={"width": 0.32, "edgecolor": p.panel, "linewidth": 1.5},
        )
        self._ax.set_aspect("equal")
        self._fig.patch.set_facecolor(p.panel)
        self._canvas.draw_idle()

        self._total_lbl.configure(text=f"${total:,.2f}")

        for i, a in enumerate(valid):
            pct = (a["value"] / total) * 100.0
            # IMPORTANT: every row uses the SOLID panel color (no transparent).
            # On macOS CustomTkinter, fg_color="transparent" inside a
            # corner_radius parent can render as a separate rounded card —
            # the exact bug we keep chasing. Solid panel color is safe.
            row = ctk.CTkFrame(self._legend, fg_color=p.panel)
            row.pack(anchor="w", pady=1)
            ctk.CTkLabel(
                row, text="●", text_color=colors[i],
                font=ctk.CTkFont(size=14), fg_color=p.panel,
            ).pack(side="left", padx=(0, 4))
            ctk.CTkLabel(
                row, text=a["symbol"], text_color=p.text,
                font=ctk.CTkFont(size=11, weight="bold"),
                anchor="w", width=68, fg_color=p.panel,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=f"{pct:.1f}%", text_color=p.text_muted,
                font=ctk.CTkFont(size=11), anchor="e", width=52,
                fg_color=p.panel,
            ).pack(side="left")
            self._legend_rows.append(row)

    # ---------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self._card.configure(fg_color=p.panel, border_color=p.border)
        self._legend.configure(fg_color=p.panel)
        self._title.configure(text_color=p.text_muted)
        self._total_lbl.configure(text_color=p.text)
        self._fig.patch.set_facecolor(p.panel)
        self._canvas.get_tk_widget().configure(bg=p.panel)
        self._last_sig = None
