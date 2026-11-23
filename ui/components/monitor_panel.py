"""Right panel: portfolio allocation donut (top) + live trades ledger
(dominant). The equity chart moved to the left column's stats panel."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

import customtkinter as ctk

from ui import theme
from ui.components.portfolio_donut import PortfolioDonut


class MonitorPanel(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        get_orders: Callable[[], list[dict]],
        on_period_change: Callable[[str], None] | None = None,  # kept for API compat
        on_trade_click: Callable[[dict], None] | None = None,
        **kwargs,
    ) -> None:
        p = theme.palette()
        super().__init__(master, fg_color=p.bg, **kwargs)
        self._get_orders = get_orders
        self._on_trade_click = on_trade_click

        # Layout: allocation donut on top, trades ledger dominant beneath.
        self.grid_rowconfigure(0, weight=3, minsize=180)   # donut strip
        self.grid_rowconfigure(1, weight=8)                # trades ledger
        self.grid_columnconfigure(0, weight=1)

        # --- Portfolio donut on top ---------------------------------
        self.donut = PortfolioDonut(self)
        self.donut.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        # --- Trades ledger below ------------------------------------
        ledger = ctk.CTkFrame(self, fg_color=p.panel,
                              border_color=p.border, border_width=1,
                              corner_radius=12)
        ledger.grid(row=1, column=0, sticky="nsew")
        ledger.grid_rowconfigure(2, weight=1)
        ledger.grid_columnconfigure(0, weight=1)

        ledger_header = ctk.CTkFrame(ledger, fg_color="transparent")
        ledger_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        self._ledger_dot = ctk.CTkLabel(
            ledger_header, text="●", text_color=p.profit,
            font=ctk.CTkFont(size=14),
        )
        self._ledger_dot.pack(side="left", padx=(0, 6))
        self._ledger_title = ctk.CTkLabel(
            ledger_header, text="LIVE TRADES",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._ledger_title.pack(side="left")
        self._ledger_count = ctk.CTkLabel(
            ledger_header, text="0", text_color=p.text_muted,
            font=ctk.CTkFont(size=11),
        )
        self._ledger_count.pack(side="right")

        # Column headers
        col_row = ctk.CTkFrame(ledger, fg_color=p.panel_alt, corner_radius=6)
        col_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 0))
        col_row.grid_columnconfigure(2, weight=1)
        for i, (txt, w, anchor) in enumerate([
            ("AGO",     50,  "w"),
            ("SIDE",    50,  "w"),
            ("SYMBOL",  85,  "w"),
            ("QTY/PX",  140, "e"),
            ("PNL",     90,  "e"),
        ]):
            ctk.CTkLabel(
                col_row, text=txt, width=w, anchor=anchor,
                text_color=p.text_muted,
                font=ctk.CTkFont(size=10, weight="bold"),
            ).grid(row=0, column=i, padx=4, pady=4, sticky="ew")

        # Scrollable rows
        self._scroll = ctk.CTkScrollableFrame(
            ledger, fg_color=p.panel, corner_radius=6,
            scrollbar_button_color=p.border,
        )
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._rows: dict[str, "TradeRow"] = {}

        self._empty = ctk.CTkLabel(
            self._scroll,
            text="No trades yet. Trades will appear here when the bot runs and conditions match.",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11),
            wraplength=380,
        )
        self._empty.pack(pady=20)

        self._orders_active = False
        self._orders_busy = False
        theme.on_theme_change(self._apply_theme)
        self.after(1000, self._tick_ages)
        self.after(900, self._pulse_dot)

    # ---------------------------------------------------------------
    def start(self) -> None:
        """Begin polling orders. Called by MainWindow on Start Bot."""
        if self._orders_active:
            return
        self._orders_active = True
        self.after(50, self._refresh_orders)

    def stop(self) -> None:
        """Pause order polling. Called by MainWindow on Stop Bot."""
        self._orders_active = False

    # ---------------------------------------------------------------
    def _refresh_orders(self) -> None:
        """UI ticker: spawn bg fetch only if previous one finished."""
        if not self._orders_active:
            return
        if not self._orders_busy:
            self._orders_busy = True
            threading.Thread(target=self._orders_bg, daemon=True).start()
        self.after(750, self._refresh_orders)

    def _orders_bg(self) -> None:
        try:
            try:
                rows = self._get_orders() or []
            except Exception:
                rows = []
            self.after(0, lambda r=rows: self._apply_orders(r))
        finally:
            self._orders_busy = False

    def _apply_orders(self, rows: list[dict]) -> None:
        """Minimal-diff refresh: update existing rows in place, destroy
        only the ones that are gone, create only the new ones.  Eliminates
        the every-tick flicker the user was seeing.
        """
        if rows and self._empty is not None:
            try:
                self._empty.destroy()
            except Exception:
                pass
            self._empty = None

        previous_set = set(self._rows.keys())
        new_sigs: list[str] = []
        sig_to_order: dict[str, dict] = {}
        for r in rows:
            oid = r.get("alpaca_order_id") or ""
            if not oid:
                continue
            new_sigs.append(oid)
            sig_to_order[oid] = r
        new_set = set(new_sigs)

        # 1) drop rows no longer present
        for old in list(self._rows.keys()):
            if old not in new_set:
                try:
                    self._rows[old].destroy()
                except Exception:
                    pass
                del self._rows[old]

        # 2) detect whether order changed; only re-pack when it did
        current_order = list(self._rows.keys())
        desired_existing = [s for s in new_sigs if s in self._rows]
        need_reorder = (current_order != desired_existing) or any(
            s not in self._rows for s in new_sigs
        )
        if need_reorder:
            for row in self._rows.values():
                try:
                    row.pack_forget()
                except Exception:
                    pass

        # 3) update in place (or create new); re-pack in newest-first order
        for sig in new_sigs:
            row = self._rows.get(sig)
            if row is None:
                row = TradeRow(
                    self._scroll, sig_to_order[sig],
                    on_click=self._on_trade_click,
                )
                self._rows[sig] = row
                row.pack(fill="x", padx=2, pady=2)
                if sig not in previous_set:
                    row.pulse_in()
            else:
                row.update_data(sig_to_order[sig])
                if need_reorder:
                    row.pack(fill="x", padx=2, pady=2)

        self._ledger_count.configure(text=str(len(self._rows)))

    def _tick_ages(self) -> None:
        for row in self._rows.values():
            row.refresh_age()
        self.after(1000, self._tick_ages)

    def _pulse_dot(self) -> None:
        p = theme.palette()
        self._dot_on = not getattr(self, "_dot_on", False)
        self._ledger_dot.configure(
            text_color=p.profit if self._dot_on else p.text_muted,
        )
        self.after(900, self._pulse_dot)

    # ---------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.bg)
        self._ledger_title.configure(text_color=p.text_muted)
        self._ledger_count.configure(text_color=p.text_muted)
        self._scroll.configure(fg_color=p.panel, scrollbar_button_color=p.border)
        if self._empty is not None:
            self._empty.configure(text_color=p.text_muted)


# ====================================================================
class TradeRow(ctk.CTkFrame):
    """Two-line trade row with TP/SL + live PnL.

    Static parts (side badge, symbol, qty, entry price, TP, SL) are built
    in __init__ and never re-drawn.  Dynamic parts (age, live PnL, NOW
    price, status, background tint) update in place via `update_data()`
    so the row doesn't flicker on every tick.
    """

    def __init__(
        self,
        master,
        order: dict,
        on_click: Callable[[dict], None] | None = None,
    ) -> None:
        p = theme.palette()
        side = (order.get("side") or "").upper()
        self._timestamp = _ensure_utc(order.get("timestamp"))
        self._base_bg = p.panel_alt
        self._order = dict(order)
        self._on_click = on_click

        super().__init__(master, fg_color=self._base_bg, corner_radius=8,
                         border_color=self._base_bg, border_width=1)
        if on_click is not None:
            # Bind to row and bubble from children so clicks land anywhere.
            self.bind("<Button-1>", self._handle_click)
            try:
                self.configure(cursor="hand2")
            except Exception:
                pass

        side_color = p.profit if side == "BUY" else p.loss

        # ===== Line 1: AGO · SIDE · SYMBOL · QTY/PRICE · PNL =====
        line1 = ctk.CTkFrame(self, fg_color="transparent")
        line1.pack(fill="x", padx=8, pady=(8, 2))
        line1.grid_columnconfigure(2, weight=1)

        self._age_lbl = ctk.CTkLabel(
            line1, text="—", text_color=p.text_muted,
            font=ctk.CTkFont(size=10), width=44, anchor="w",
        )
        self._age_lbl.grid(row=0, column=0, padx=(0, 4))

        ctk.CTkLabel(
            line1, text=f" {side} ",
            text_color="white", fg_color=side_color,
            corner_radius=4, width=46, height=20,
            font=ctk.CTkFont(size=10, weight="bold"),
        ).grid(row=0, column=1, padx=(0, 6))

        ctk.CTkLabel(
            line1, text=str(order.get("symbol", "")),
            text_color=p.text,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=2, sticky="w")

        qty = float(order.get("qty") or 0)
        entry = float(order.get("entry_price") or order.get("price") or 0)
        ctk.CTkLabel(
            line1, text=f"{qty:g} @ ${_fmt_price(entry)}",
            text_color=p.text_muted, font=ctk.CTkFont(size=11), anchor="e",
        ).grid(row=0, column=3, padx=4, sticky="e")

        # Dynamic — updated by update_data()
        self._pnl_lbl = ctk.CTkLabel(
            line1, text="—", text_color=p.text_muted,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="e", width=86,
        )
        self._pnl_lbl.grid(row=0, column=4, padx=(4, 0), sticky="e")

        # ===== Line 2: TP · SL · NOW · STATUS =====
        line2 = ctk.CTkFrame(self, fg_color="transparent")
        line2.pack(fill="x", padx=8, pady=(0, 8))
        line2.grid_columnconfigure(7, weight=1)

        tp = order.get("tp_price")
        sl = order.get("sl_price")

        # Static TP/SL
        if tp is not None:
            ctk.CTkLabel(line2, text="TP ", text_color=p.text_muted,
                         font=ctk.CTkFont(size=10)).grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(line2, text=f"${_fmt_price(float(tp))}",
                         text_color=p.profit,
                         font=ctk.CTkFont(size=11, weight="bold")
                         ).grid(row=0, column=1, padx=(0, 10), sticky="w")
        if sl is not None:
            ctk.CTkLabel(line2, text="SL ", text_color=p.text_muted,
                         font=ctk.CTkFont(size=10)).grid(row=0, column=2, sticky="w")
            ctk.CTkLabel(line2, text=f"${_fmt_price(float(sl))}",
                         text_color=p.loss,
                         font=ctk.CTkFont(size=11, weight="bold")
                         ).grid(row=0, column=3, padx=(0, 10), sticky="w")

        # Dynamic NOW (hidden when closed)
        self._now_label = ctk.CTkLabel(
            line2, text="NOW ", text_color=p.text_muted,
            font=ctk.CTkFont(size=10),
        )
        self._now_value = ctk.CTkLabel(
            line2, text="—", text_color=p.accent,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._now_label.grid(row=0, column=4, sticky="w")
        self._now_value.grid(row=0, column=5, padx=(0, 10), sticky="w")

        # Dynamic status
        self._status_lbl = ctk.CTkLabel(
            line2, text="—", text_color=p.text_muted,
            font=ctk.CTkFont(size=10, weight="bold"),
            anchor="e",
        )
        self._status_lbl.grid(row=0, column=8, sticky="e")

        # Initial paint
        self.update_data(order)
        self._bind_clicks_to_children()

    # ------------------------------------------------------------------
    def update_data(self, order: dict) -> None:
        """In-place refresh of mutating fields. No widget create/destroy."""
        p = theme.palette()
        # Keep the latest order snapshot for the reasoning modal.
        self._order = dict(order)
        is_open = bool(order.get("is_open"))
        live_pnl = float(order.get("live_pnl") or order.get("pnl") or 0)
        status = (order.get("status") or "").upper()
        cur_px = order.get("current_price")

        # Background tint — only reconfigure when sign changes
        if live_pnl > 0:
            new_bg = _solid("#10B98122", fallback=p.panel_alt)
        elif live_pnl < 0:
            new_bg = _solid("#EF444422", fallback=p.panel_alt)
        else:
            new_bg = p.panel_alt
        if new_bg != self._base_bg:
            self._base_bg = new_bg
            self.configure(fg_color=new_bg, border_color=new_bg)

        # PNL label
        pnl_color = (
            p.profit if live_pnl > 0
            else p.loss if live_pnl < 0
            else p.text_muted
        )
        sign = "+" if live_pnl >= 0 else "-"
        marker = "•" if is_open else " "
        self._pnl_lbl.configure(
            text=f"{marker} {sign}${abs(live_pnl):.2f}",
            text_color=pnl_color,
        )

        # NOW (live tick price) — show only when open
        if is_open and cur_px is not None:
            self._now_label.grid()
            self._now_value.grid()
            self._now_value.configure(text=f"${_fmt_price(float(cur_px))}")
        else:
            self._now_label.grid_remove()
            self._now_value.grid_remove()

        # Status — synthetic exit rows get readable labels
        if status == "EXIT_TP":
            status_text, status_color = "TP HIT", p.profit
        elif status == "EXIT_QUICK":
            status_text, status_color = "QUICK ✓", p.profit
        elif status == "EXIT_SL":
            status_text, status_color = "SL HIT", p.loss
        elif status in ("STOPPED", "CANCELED", "REJECTED"):
            status_text, status_color = status, p.loss
        elif is_open:
            status_text, status_color = "OPEN", p.accent
        elif status == "FILLED" and live_pnl > 0:
            status_text, status_color = "FILLED", p.profit
        else:
            status_text, status_color = (status or "—"), p.text_muted
        self._status_lbl.configure(text=status_text, text_color=status_color)

        self.refresh_age()

        self.refresh_age()

    # ------------------------------------------------------------------
    def refresh_age(self) -> None:
        if self._timestamp is None:
            return
        delta = (datetime.now(timezone.utc) - self._timestamp).total_seconds()
        if delta < 60:
            text = f"{int(delta)}s"
        elif delta < 3600:
            text = f"{int(delta / 60)}m"
        elif delta < 86400:
            text = f"{int(delta / 3600)}h"
        else:
            text = f"{int(delta / 86400)}d"
        self._age_lbl.configure(text=text)

    def pulse_in(self) -> None:
        p = theme.palette()
        self.configure(border_color=p.accent)
        self.after(900, lambda: self.configure(border_color=self._base_bg))

    def _handle_click(self, _event=None) -> None:
        if self._on_click is None:
            return
        try:
            self._on_click(self._order)
        except Exception:
            pass

    def _bind_clicks_to_children(self) -> None:
        """Walk descendants and bind <Button-1> so clicks anywhere on the
        row's surface open the reasoning modal — Tk does not bubble by
        default."""
        if self._on_click is None:
            return
        for w in self.winfo_children():
            try:
                w.bind("<Button-1>", self._handle_click, add="+")
            except Exception:
                pass
            for c in w.winfo_children():
                try:
                    c.bind("<Button-1>", self._handle_click, add="+")
                except Exception:
                    pass


# ====================================================================
def _fmt_price(px: float) -> str:
    if px >= 1000:
        return f"{px:,.2f}"
    if px >= 1:
        return f"{px:,.4f}"
    return f"{px:.5f}"


def _solid(hex_color: str, *, fallback: str) -> str:
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 9:
        return hex_color or fallback
    try:
        r = int(hex_color[1:3], 16); g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16); a = int(hex_color[7:9], 16) / 255.0
        fr = int(fallback[1:3], 16); fg = int(fallback[3:5], 16)
        fb = int(fallback[5:7], 16)
    except Exception:
        return fallback
    rr = int(fr * (1 - a) + r * a)
    gg = int(fg * (1 - a) + g * a)
    bb = int(fb * (1 - a) + b * a)
    return f"#{rr:02X}{gg:02X}{bb:02X}"


def _ensure_utc(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None
