"""Main application window — 4-row layout (post-stocks refactor):

  Row 0  metric cards (6)
  Row 1  secondary metric cards (6)
  Row 2  body — Stats (left 70%) | Monitor (right 30%) for active tab
  Row 3  bottom bar: [CRYPTO|STOCKS] tabs (left) · Start/Stop (center) ·
         ENVORAI logo + Trading Terminal subtitle + PAPER + gear (right)

Two coordinators run in parallel; the tab swaps which body panel + log
queue is visible and which class the metric queries filter to.
"""
from __future__ import annotations

import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Literal

import customtkinter as ctk

import config
from agents.economic_calendar import EconomicCalendar
from database.db_manager import DBManager
from ui import theme
from ui.components.asset_tabs import AssetTabs
from ui.components.metric_card import MetricCard
from ui.components.monitor_panel import MonitorPanel
from ui.components.settings_dialog import SettingsDialog
from ui.components.stats_panel import StatsPanel
from ui.components.trade_reasoning_dialog import TradeReasoningDialog


AssetClass = Literal["crypto", "stock"]


class MainWindow(ctk.CTk):
    def __init__(
        self,
        db: DBManager,
        *,
        crypto_coordinator=None,
        stocks_coordinator=None,
        coordinator=None,  # legacy alias for crypto
    ) -> None:
        super().__init__()
        self.db = db
        self.crypto_coordinator = crypto_coordinator or coordinator
        self.stocks_coordinator = stocks_coordinator
        # Logging: each coordinator owns its own log queue. We default to the
        # crypto one to keep startup messages flowing into the visible console
        # before any bot is started.
        self._fallback_log_queue: queue.Queue = queue.Queue()

        self._active_class: AssetClass = "crypto"

        # Macro economic calendar — single instance shared by both tabs.
        # Cheap background thread, hourly RSS pull.
        self.economic_calendar = EconomicCalendar(
            log_queue=self._fallback_log_queue, max_events=3,
        )
        try:
            self.economic_calendar.start()
        except Exception:
            pass

        theme.set_mode("dark")
        p = theme.palette()

        self.title("Envorai Trading Terminal")
        self.minsize(1280, 800)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        try:
            self.state("zoomed")
        except Exception:
            pass
        self.configure(fg_color=p.bg)

        self.grid_columnconfigure(0, weight=7, uniform="cols")
        self.grid_columnconfigure(1, weight=3, uniform="cols")
        # Only the body row stretches
        self.grid_rowconfigure(2, weight=1)

        self._build_cards_row()           # row 0
        self._build_secondary_cards_row() # row 1
        self._build_body()                # row 2 (stretches)
        self._build_bottom_bar()          # row 3

        self._watchlist_baseline: list[dict] = []
        self._equity_buffer: deque = deque(maxlen=600)
        self._data_active = False
        self._metrics_busy = False
        self._equity_busy = False
        self._sentiment_busy = False
        self._watchlist_busy = False
        self._status_pulse_on = False
        self._values_hidden = False

        # Show the crypto panels first; the user can flip to stocks via tab.
        self._switch_to(self._active_class, initial=True)

        theme.on_theme_change(self._apply_theme)
        self.after(500, self._refresh_metrics)
        self.after(900, self._pulse_status_dot)

    # =================================================================
    def _build_cards_row(self) -> None:
        p = theme.palette()
        cards = ctk.CTkFrame(self, fg_color=p.bg)
        cards.grid(row=0, column=0, columnspan=2, padx=16, pady=(14, 0), sticky="ew")
        for i in range(6):
            cards.grid_columnconfigure(i, weight=1, uniform="metric")

        self.card_balance = MetricCard(
            cards, icon_name="wallet", title="Live Balance",
            value="$0.00", accent=p.accent,
        )
        self.card_budget = MetricCard(
            cards, icon_name="db", title="NET P&L",
            value="$0.00", accent=p.profit,
        )
        self.card_trades = MetricCard(
            cards, icon_name="activity", title="Total Trades",
            value="0", accent=p.accent,
        )
        self.card_winrate = MetricCard(
            cards, icon_name="trophy", title="Win Rate",
            value="0%", accent=p.profit,
        )
        self.card_pnl = MetricCard(
            cards, icon_name="chart-line", title="Today P&L",
            value="$0.00", accent=p.profit,
        )
        self.card_commission = MetricCard(
            cards, icon_name="alert", title="Commissions",
            value="$0.00", accent=p.loss,
        )

        for i, c in enumerate([
            self.card_balance, self.card_budget, self.card_trades,
            self.card_winrate, self.card_pnl, self.card_commission,
        ]):
            padx = (0, 4) if i == 0 else ((4, 0) if i == 5 else 4)
            c.grid(row=0, column=i, padx=padx, sticky="nsew")

        self._hide_btn = ctk.CTkButton(
            self.card_balance, text="Hide", width=54, height=22,
            command=self._on_toggle_hide,
            fg_color=p.panel_alt, hover_color=p.border,
            text_color=p.text_muted, corner_radius=11,
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        self._hide_btn.place(relx=1.0, x=-8, y=8, anchor="ne")

    # =================================================================
    def _build_secondary_cards_row(self) -> None:
        p = theme.palette()
        cards = ctk.CTkFrame(self, fg_color=p.bg)
        cards.grid(row=1, column=0, columnspan=2, padx=16, pady=(8, 0), sticky="ew")
        for i in range(6):
            cards.grid_columnconfigure(i, weight=1, uniform="metric2")

        self.card_open_positions = MetricCard(
            cards, icon_name="activity", title="Open Positions",
            value="0", accent=p.accent,
        )
        self.card_position_value = MetricCard(
            cards, icon_name="wallet", title="Position Value",
            value="$0.00", accent=p.accent,
        )
        self.card_buying_power = MetricCard(
            cards, icon_name="db", title="Buying Power",
            value="$0.00", accent=p.profit,
        )
        self.card_trading_budget = MetricCard(
            cards, icon_name="db", title="Trading Budget",
            value="$0.00", accent=p.accent,
        )
        self.card_avg_pnl = MetricCard(
            cards, icon_name="chart-line", title="Avg Trade P&L",
            value="$0.00", accent=p.profit,
        )
        self.card_ai_threshold = MetricCard(
            cards, icon_name="brain", title="AI Threshold",
            value="—", accent=p.accent,
        )

        for i, c in enumerate([
            self.card_open_positions, self.card_position_value,
            self.card_buying_power, self.card_trading_budget,
            self.card_avg_pnl, self.card_ai_threshold,
        ]):
            padx = (0, 4) if i == 0 else ((4, 0) if i == 5 else 4)
            c.grid(row=0, column=i, padx=padx, sticky="nsew")

    # =================================================================
    def _build_body(self) -> None:
        """Build TWO sets of body panels — one per asset class — and stash
        them. _switch_to() shows/hides via grid()/grid_remove() so each
        coordinator drains into its own console without cross-talk."""
        p = theme.palette()

        # Left containers (Stats panels live here, one per class)
        left = ctk.CTkFrame(self, fg_color=p.bg)
        left.grid(row=2, column=0, padx=(16, 8), pady=12, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self._left_container = left

        crypto_lq = (
            self.crypto_coordinator.log_queue
            if self.crypto_coordinator is not None
            else self._fallback_log_queue
        )
        stocks_lq = (
            self.stocks_coordinator.log_queue
            if self.stocks_coordinator is not None
            else self._fallback_log_queue
        )

        # Each StatsPanel owns its own EconomicCalendarPanel sitting next
        # to the console — pass the shared agent's getter so both tabs read
        # from the same in-memory cache (no double-polling). The equity
        # chart now lives inside StatsPanel (swapped with the donut), so
        # the period-change callback is wired here too.
        self.crypto_stats = StatsPanel(
            left, log_queue=crypto_lq,
            get_macro_events=self.economic_calendar.get_upcoming,
            on_period_change=self._on_period_change,
        )
        self.stocks_stats = StatsPanel(
            left, log_queue=stocks_lq,
            get_macro_events=self.economic_calendar.get_upcoming,
            on_period_change=self._on_period_change,
        )

        # Right containers (Monitor panels — now host the allocation donut
        # on top and the live trades ledger below).
        right = ctk.CTkFrame(self, fg_color=p.bg)
        right.grid(row=2, column=1, padx=(8, 16), pady=12, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self._right_container = right

        self.crypto_monitor = MonitorPanel(
            right,
            get_orders=lambda: self._safe_get_orders("crypto"),
            on_trade_click=self._open_reasoning_modal,
        )
        self.stocks_monitor = MonitorPanel(
            right,
            get_orders=lambda: self._safe_get_orders("stock"),
            on_trade_click=self._open_reasoning_modal,
        )

    # =================================================================
    def _build_bottom_bar(self) -> None:
        """Bottom bar: tabs (left) · Start/Stop (center) · ENVORAI brand
        + PAPER badge + settings gear (right)."""
        p = theme.palette()
        bar = ctk.CTkFrame(self, fg_color=p.panel, corner_radius=0, height=72)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_columnconfigure(1, weight=0)
        bar.grid_columnconfigure(2, weight=1)

        # --- Left: asset tabs + status -------------------------------
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, padx=20, pady=12, sticky="w")
        self._tabs = AssetTabs(
            left, on_change=self._on_tab_change, initial="crypto",
        )
        self._tabs.pack(side="left")
        self._status_label = ctk.CTkLabel(
            left, text="  idle",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._status_label.pack(side="left", padx=(12, 0))

        # --- Center: Start · dot · Stop ------------------------------
        group = ctk.CTkFrame(bar, fg_color="transparent")
        group.grid(row=0, column=1, pady=12)

        self.btn_start = ctk.CTkButton(
            group, text="  Start",
            image=theme.load_colored_icon("play", (255, 255, 255), size=16),
            compound="left", command=self._on_start,
            fg_color=p.profit, hover_color="#0EA371",
            text_color="white",
            width=150, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=19,
        )
        self.btn_start.pack(side="left", padx=(0, 10))

        self._status_dot = ctk.CTkLabel(
            group, text="●", text_color=p.text_muted,
            font=ctk.CTkFont(size=14), width=18,
        )
        self._status_dot.pack(side="left", padx=(0, 10))

        self.btn_stop = ctk.CTkButton(
            group, text="  Stop",
            image=theme.load_colored_icon("stop", _rgb(p.loss), size=16),
            compound="left", command=self._on_stop,
            fg_color="transparent", hover_color=p.panel_alt,
            border_color=p.loss, border_width=1,
            text_color=p.loss,
            width=150, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=19, state="disabled",
        )
        self.btn_stop.pack(side="left")

        # --- Right: ENVORAI brand + PAPER badge + settings -----------
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, padx=20, pady=8, sticky="e")

        mode_color = p.profit if config.ALPACA_PAPER else p.loss
        self._alpaca_badge = ctk.CTkLabel(
            right,
            text=" PAPER " if config.ALPACA_PAPER else " LIVE ",
            text_color="white", fg_color=mode_color, corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            width=70, height=24,
        )
        self._alpaca_badge.pack(side="left", padx=(0, 10))

        self._settings_btn = ctk.CTkButton(
            right, text="", width=38, height=38,
            image=theme.load_icon("gear", size=20),
            command=self._on_open_settings,
            fg_color=p.panel_alt, hover_color=p.border, corner_radius=19,
        )
        self._settings_btn.pack(side="left", padx=(0, 18))

        # ENVORAI logo + Trading Terminal subtitle, anchored to the far right.
        brand = ctk.CTkFrame(right, fg_color="transparent")
        brand.pack(side="left")
        logo_row = ctk.CTkFrame(brand, fg_color="transparent")
        logo_row.pack(anchor="e")
        self._logo = ctk.CTkLabel(
            logo_row, text="",
            image=theme.load_colored_icon("brain", _rgb(p.accent), size=22),
        )
        self._logo.pack(side="left", padx=(0, 6))
        self._app_title = ctk.CTkLabel(
            logo_row, text="ENVORAI",
            text_color=p.text,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self._app_title.pack(side="left")
        self._app_subtitle = ctk.CTkLabel(
            brand, text="Trading Terminal",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=9),
            anchor="e",
        )
        self._app_subtitle.pack(anchor="e", pady=(1, 0))

    # =================================================================
    # Tab switching
    # =================================================================
    @property
    def _active_coord(self):
        if self._active_class == "crypto":
            return self.crypto_coordinator
        return self.stocks_coordinator

    def _on_tab_change(self, new_class: AssetClass) -> None:
        if new_class == self._active_class:
            return
        self._switch_to(new_class)

    def _switch_to(self, cls: AssetClass, *, initial: bool = False) -> None:
        self._active_class = cls
        # Show / hide stats panel pair
        if cls == "crypto":
            self.stocks_stats.grid_remove()
            self.stocks_monitor.grid_remove()
            self.crypto_stats.grid(row=0, column=0, sticky="nsew")
            self.crypto_monitor.grid(row=0, column=0, sticky="nsew")
        else:
            self.crypto_stats.grid_remove()
            self.crypto_monitor.grid_remove()
            self.stocks_stats.grid(row=0, column=0, sticky="nsew")
            self.stocks_monitor.grid(row=0, column=0, sticky="nsew")

        # Sync Start/Stop button state to this tab's coordinator
        coord = self._active_coord
        running = bool(coord and coord.is_running)
        self._set_running(running, cls)

        # Force-refresh metrics / watchlist baseline for the new class.
        # Reset busy flags so any in-flight crypto fetch can't clobber the
        # stocks render, and blank the cards immediately so the user sees
        # the swap even when the new class has zero history.
        self._watchlist_baseline = []
        self._equity_buffer.clear()
        self._metrics_busy = False
        self._watchlist_busy = False
        self._equity_busy = False
        if not initial:
            self._blank_metric_cards()
            self._refresh_metrics()
            self._refresh_watchlist_baseline()

    def _blank_metric_cards(self) -> None:
        """Reset card values to placeholders during tab swap so stale numbers
        from the other asset class don't linger while the bg refresh runs."""
        for card in (
            self.card_balance, self.card_budget, self.card_pnl,
            self.card_commission, self.card_position_value,
            self.card_buying_power, self.card_trading_budget,
            self.card_avg_pnl,
        ):
            try:
                card.set_value("$0.00")
            except Exception:
                pass
        for card in (self.card_trades, self.card_open_positions):
            try:
                card.set_value("0")
            except Exception:
                pass
        try:
            self.card_winrate.set_value("0.0%")
        except Exception:
            pass

    # =================================================================
    # Top-bar runtime updates
    # =================================================================
    def _set_running(self, running: bool, cls: AssetClass | None = None) -> None:
        p = theme.palette()
        cls = cls or self._active_class
        self._status_label.configure(
            text=f"  {'running' if running else 'idle'}"
        )
        if running:
            self.btn_start.configure(
                state="disabled",
                fg_color="transparent",
                border_color=p.border, border_width=1,
                text_color=p.text_muted,
                image=theme.load_colored_icon("play", _rgb(p.text_muted), size=16),
            )
            self.btn_stop.configure(
                state="normal",
                fg_color=p.loss, border_width=0,
                hover_color="#C81E1E",
                text_color="white",
                image=theme.load_colored_icon("stop", (255, 255, 255), size=16),
            )
            self._status_dot.configure(text_color=p.profit)
        else:
            self.btn_start.configure(
                state="normal",
                fg_color=p.profit, border_width=0,
                hover_color="#0EA371",
                text_color="white",
                image=theme.load_colored_icon("play", (255, 255, 255), size=16),
            )
            self.btn_stop.configure(
                state="disabled",
                fg_color="transparent",
                border_color=p.loss, border_width=1,
                hover_color=p.panel_alt,
                text_color=p.loss,
                image=theme.load_colored_icon("stop", _rgb(p.loss), size=16),
            )
            self._status_dot.configure(text_color=p.text_muted)

    # =================================================================
    def _on_toggle_hide(self) -> None:
        self._values_hidden = not self._values_hidden
        for c in (
            self.card_balance, self.card_budget, self.card_trades,
            self.card_winrate, self.card_pnl, self.card_commission,
            self.card_open_positions, self.card_position_value,
            self.card_buying_power, self.card_trading_budget,
            self.card_avg_pnl, self.card_ai_threshold,
        ):
            c.set_hidden(self._values_hidden)
        self._hide_btn.configure(text="Show" if self._values_hidden else "Hide")

    def _on_open_settings(self) -> None:
        try:
            dialog = SettingsDialog(self, self.db, on_saved=self._on_settings_saved)
            dialog.focus()
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"settings dialog: {e}"}
            )

    def _on_settings_saved(self, values: dict) -> None:
        changed = ", ".join(f"{k}={v:g}" for k, v in values.items())
        self._active_log_queue().put(
            {"tag": "INFO", "text": f"settings updated: {changed}"}
        )

    def _on_start(self) -> None:
        coord = self._active_coord
        if coord is None:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"No {self._active_class} coordinator wired."}
            )
            return
        try:
            coord.start()
            self._set_running(True)
            self._start_data_feeds()
            self._active_log_queue().put(
                {"tag": "INFO", "text": f"{self._active_class.upper()} bot started."}
            )
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"start failed: {e}"}
            )

    def _start_data_feeds(self) -> None:
        if self._data_active:
            return
        self._data_active = True
        # Both monitor panels poll DB; safe to start both since they filter
        # by their own asset_class through the get_orders callback.
        self.crypto_monitor.start()
        self.stocks_monitor.start()
        self._refresh_watchlist_baseline()
        self.after(50, self._refresh_sentiment)
        self.after(50, self._tick_live)
        self.after(50, self._tick_equity)

    def _on_stop(self) -> None:
        coord = self._active_coord
        if coord is None:
            return
        try:
            coord.stop()
            self._set_running(False)
            # Only stop poll cycle if BOTH coordinators are idle
            other = (
                self.stocks_coordinator if self._active_class == "crypto"
                else self.crypto_coordinator
            )
            if not (other and other.is_running):
                self._data_active = False
                self.crypto_monitor.stop()
                self.stocks_monitor.stop()
            self._active_log_queue().put(
                {"tag": "INFO", "text": f"{self._active_class.upper()} bot stopped."}
            )
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"stop failed: {e}"}
            )

    def _on_period_change(self, period: str) -> None:
        self._refresh_chart(period)

    # =================================================================
    # Reasoning modal
    # =================================================================
    def _open_reasoning_modal(self, order: dict) -> None:
        """Open the AI Reasoning modal for a clicked trade row. The order
        dict is the same one rendered in MonitorPanel — pass it straight
        through so the modal can read symbol/ts/asset_class/tp/sl/etc."""
        try:
            TradeReasoningDialog(self, db=self.db, order=order)
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"reasoning modal: {e}"}
            )

    def _pulse_status_dot(self) -> None:
        p = theme.palette()
        coord = self._active_coord
        if coord and coord.is_running:
            self._status_pulse_on = not self._status_pulse_on
            self._status_dot.configure(
                text_color=p.profit if self._status_pulse_on else p.panel_alt,
            )
        else:
            self._status_dot.configure(text_color=p.text_muted)
        self.after(900, self._pulse_status_dot)

    # =================================================================
    # Metrics — asset-filtered
    # =================================================================
    def _active_log_queue(self) -> queue.Queue:
        coord = self._active_coord
        return coord.log_queue if coord is not None else self._fallback_log_queue

    def _refresh_metrics(self) -> None:
        if not self._metrics_busy:
            self._metrics_busy = True
            threading.Thread(target=self._metrics_bg, daemon=True).start()
        self.after(5000, self._refresh_metrics)

    def _is_active_running(self) -> bool:
        coord = self._active_coord
        return bool(coord and coord.is_running)

    def _metrics_bg(self) -> None:
        cls = self._active_class
        # Gate every dashboard query on the active tab's bot state. Without
        # this, the cards show historical DB rows for a tab whose bot the
        # user never started — exactly the confusion the user reported when
        # only stocks was started but the crypto tab showed populated cards.
        if not self._is_active_running():
            self.after(0, self._blank_metric_cards)
            self._metrics_busy = False
            return
        data: dict = {}
        try:
            try:
                data["stats"] = self.db.get_trade_stats(asset_class=cls)
            except Exception:
                data["stats"] = {"total": 0, "win_rate": 0.0,
                                 "today_pnl": 0.0, "total_pnl": 0.0}
            try:
                data["balance"] = self._fetch_balance()
            except Exception:
                data["balance"] = 0.0
            try:
                data["commission"] = self.db.get_total_commission(asset_class=cls)
            except Exception:
                data["commission"] = 0.0
            try:
                _, curve = self.db.get_equity_curve("daily", asset_class=cls)
                data["curve"] = curve or []
            except Exception:
                data["curve"] = []
            try:
                data["budget"] = self._compute_budget(cls)
            except Exception:
                data["budget"] = (0.0, 0.0)
            try:
                data["account"] = self._fetch_account_snapshot(cls)
            except Exception:
                data["account"] = {"positions_count": 0, "positions_value": 0.0,
                                   "buying_power": 0.0}
            try:
                data["allocations"] = self._fetch_allocations(cls)
            except Exception:
                data["allocations"] = []
            try:
                data["trading_budget"] = float(
                    self.db.get_setting("max_trading_capital", 1000.0) or 1000.0
                )
            except Exception:
                data["trading_budget"] = 0.0
            try:
                data["ai_threshold"] = float(
                    self.db.get_setting("ai_confidence_threshold", 65.0) or 65.0
                )
            except Exception:
                data["ai_threshold"] = 65.0
            # Surface the math knob (rsi_buy_threshold) and the pause-state so
            # the user sees when LearningLoop / auto_tuner moves them without
            # having to grep logs.
            try:
                data["rsi_buy_threshold"] = float(
                    self.db.get_setting("rsi_buy_threshold", 30.0) or 30.0
                )
            except Exception:
                data["rsi_buy_threshold"] = 30.0
            try:
                import time as _t
                pause_until = float(
                    self.db.get_setting("bot_pause_until_epoch", 0.0) or 0.0
                )
                data["pause_remaining_min"] = max(
                    0.0, (pause_until - _t.time()) / 60.0,
                ) if pause_until > 0 else 0.0
            except Exception:
                data["pause_remaining_min"] = 0.0
            self.after(0, lambda d=data, c=cls: self._apply_metrics(d, c))
        finally:
            self._metrics_busy = False

    def _apply_metrics(self, data: dict, cls: AssetClass | None = None) -> None:
        p = theme.palette()
        # Race-condition guard: drop stale renders that finished after the
        # user already switched to a different tab.
        if cls is not None and cls != self._active_class:
            return
        try:
            stats = data["stats"]
            self.card_balance.set_value(f"${data['balance']:,.2f}")
            self.card_trades.set_value(f"{stats['total']}")
            self.card_winrate.set_value(f"{stats['win_rate']:.1f}%")
            tp = stats["today_pnl"]
            self.card_pnl.set_value(f"{'+' if tp >= 0 else '-'}${abs(tp):,.2f}")
            self.card_commission.set_value(f"${data['commission']:,.2f}")
            curve = data["curve"]
            if curve:
                self.card_pnl.set_sparkline(curve[-30:])
                self.card_budget.set_sparkline(curve[-30:])
            total_pnl, pct = data["budget"]
            sign = "+" if total_pnl >= 0 else "-"
            self.card_budget.set_value(f"{sign}${abs(total_pnl):,.2f}")
            arrow = "▲" if pct > 0 else "▼" if pct < 0 else "→"
            self.card_budget._title_label.configure(
                text=f"NET P&L  ·  {arrow} {pct:+.2f}%",
            )

            acc = data["account"]
            self.card_open_positions.set_value(f"{acc['positions_count']}")
            self.card_position_value.set_value(f"${acc['positions_value']:,.2f}")
            self.card_buying_power.set_value(f"${acc['buying_power']:,.2f}")
            self.card_trading_budget.set_value(f"${data['trading_budget']:,.2f}")
            total_count = int(stats.get("total") or 0)
            total_realized = float(stats.get("total_pnl") or 0)
            avg = (total_realized / total_count) if total_count else 0.0
            sign = "+" if avg >= 0 else "-"
            self.card_avg_pnl.set_value(f"{sign}${abs(avg):,.2f}")
            # Compact thresholds + pause indicator in one card so the user
            # sees both math knobs (RSI buy bar) and circuit-breaker state
            # without scanning logs.
            pause_min = data.get("pause_remaining_min", 0.0)
            rsi_b = data.get("rsi_buy_threshold", 30.0)
            base = f"conf {data['ai_threshold']:.0f}%  ·  RSI≤{rsi_b:.0f}"
            if pause_min > 0:
                base = f"⏸ {pause_min:.0f}m  ·  {base}"
            self.card_ai_threshold.set_value(base)

            # Feed the portfolio donut — now lives on the monitor panel
            # (top-right slot, swapped with the equity chart).
            alloc = data.get("allocations") or []
            try:
                self._active_monitor_panel().donut.update_allocations(alloc)
            except Exception:
                pass
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"apply metrics: {e}"}
            )

    def _fetch_allocations(self, cls: AssetClass) -> list[dict]:
        """Per-symbol open-position market values, filtered to this class.

        Feeds the portfolio donut. Empty list when Alpaca creds are missing
        or the account has no positions of this class.
        """
        if not config.ALPACA_KEY:
            return []
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            positions = client.get_all_positions() or []
        except Exception:
            return []
        out: list[dict] = []
        for p in positions:
            if _pos_class(p) != cls:
                continue
            sym = (getattr(p, "symbol", "") or "").upper()
            try:
                mv = abs(float(getattr(p, "market_value", 0) or 0))
            except Exception:
                mv = 0.0
            if sym and mv > 0:
                out.append({"symbol": sym, "market_value": mv})
        return out

    def _fetch_account_snapshot(self, cls: AssetClass) -> dict:
        out = {"positions_count": 0, "positions_value": 0.0, "buying_power": 0.0}
        if not config.ALPACA_KEY:
            return out
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            try:
                positions = client.get_all_positions() or []
                filtered = [p for p in positions if _pos_class(p) == cls]
                out["positions_count"] = len(filtered)
                out["positions_value"] = sum(
                    float(getattr(p, "market_value", 0) or 0) for p in filtered
                )
            except Exception:
                pass
            try:
                acc = client.get_account()
                out["buying_power"] = float(getattr(acc, "buying_power", 0) or 0)
            except Exception:
                pass
        except Exception:
            pass
        return out

    def _compute_budget(self, cls: AssetClass) -> tuple[float, float]:
        # Realized + unrealized for this asset class via DB-backed view.
        total_pnl = 0.0
        try:
            rows = self.db.get_recent_trades_with_live_pnl(
                limit=500, asset_class=cls,
            )
        except Exception:
            rows = []
        coord = self._active_coord
        live_prices = dict(coord.latest_prices) if coord is not None else {}
        for r in rows:
            entry = float(r.get("entry_price") or 0)
            qty = float(r.get("qty") or 0)
            side = (r.get("side") or "").upper()
            if r.get("status", "").startswith("EXIT_"):
                continue
            realized = float(r.get("pnl") or 0)
            if realized != 0:
                total_pnl += realized
                continue
            tape = live_prices.get(r.get("symbol"))
            cur = float(tape["price"]) if tape else float(r.get("current_price") or 0)
            if entry > 0 and qty > 0 and cur > 0:
                direction = 1 if side == "BUY" else -1
                total_pnl += (cur - entry) * qty * direction
        max_cap = float(self.db.get_setting("max_trading_capital", 1000.0) or 1000.0)
        pct = (total_pnl / max_cap * 100.0) if max_cap > 0 else 0.0
        return total_pnl, pct

    def _refresh_watchlist_baseline(self) -> None:
        if self._watchlist_busy:
            return
        # No point fetching prices for a tab whose bot hasn't been started —
        # the watchlist stays on its "Portfolio prices will appear here..."
        # placeholder, which matches user expectation.
        if not self._is_active_running():
            return
        self._watchlist_busy = True
        threading.Thread(target=self._watchlist_baseline_bg, daemon=True).start()

    def _watchlist_baseline_bg(self) -> None:
        cls = self._active_class
        coord = self._active_coord
        baseline: list[dict] = []
        try:
            symbols: list[str] = []
            if coord is not None:
                try:
                    symbols = coord.get_symbols() or []
                except Exception:
                    symbols = []
            if not symbols:
                if cls == "crypto":
                    from engine.scanner_worker import DEFAULT_SYMBOLS
                    symbols = list(DEFAULT_SYMBOLS)
                else:
                    symbols = list(config.STOCKS_LIQUID_MAJORS)
            try:
                baseline = self.db.get_watchlist_snapshot(
                    symbols, history_bars=30, asset_class=cls,
                )
            except Exception as e:
                self.after(0, lambda: self._active_log_queue().put(
                    {"tag": "ERROR", "text": f"watchlist baseline: {e}"}
                ))

            def _apply(b=baseline, c=cls):
                if c == self._active_class:
                    self._watchlist_baseline = b
            self.after(0, _apply)
        finally:
            self._watchlist_busy = False

    def _tick_live(self) -> None:
        if not self._data_active:
            return
        if not self._is_active_running():
            self.after(800, self._tick_live)
            return
        try:
            self._apply_live_prices_to_watchlist()
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"live tick: {e}"}
            )
        self.after(800, self._tick_live)

    def _active_stats_panel(self) -> StatsPanel:
        return (
            self.crypto_stats if self._active_class == "crypto"
            else self.stocks_stats
        )

    def _apply_live_prices_to_watchlist(self) -> None:
        if not getattr(self, "_watchlist_baseline", None):
            return
        coord = self._active_coord
        live = dict(coord.latest_prices) if coord is not None else {}
        overlay = []
        for snap in self._watchlist_baseline:
            sym = snap["symbol"]
            tape = live.get(sym)
            if tape is None or not snap.get("history"):
                overlay.append(snap)
                continue
            new_snap = dict(snap)
            new_price = float(tape["price"])
            new_snap["price"] = new_price
            hist_seq = snap["history"]
            baseline = float(hist_seq[-5] if len(hist_seq) >= 5 else hist_seq[0])
            if baseline > 0:
                new_snap["change_pct"] = (new_price - baseline) / baseline * 100.0
            hist = list(hist_seq)
            if hist and abs(hist[-1] - new_price) / max(hist[-1], 1e-9) > 0.0001:
                hist = hist[1:] + [new_price] if len(hist) >= 30 else hist + [new_price]
                new_snap["history"] = hist
            overlay.append(new_snap)
        self._active_stats_panel().watchlist.update_snapshots(overlay)

    def _refresh_sentiment(self) -> None:
        if not self._data_active:
            return
        if not self._is_active_running():
            # Reschedule but skip the actual fetch; user shouldn't see the
            # tweet/news firehose for a tab whose bot is off.
            self.after(5000, self._refresh_sentiment)
            return
        if not self._sentiment_busy:
            self._sentiment_busy = True
            threading.Thread(target=self._sentiment_bg, daemon=True).start()
        self.after(5000, self._refresh_sentiment)

    def _sentiment_bg(self) -> None:
        try:
            try:
                feed = self.db.get_recent_sentiment_feed(limit=25)
            except Exception as e:
                self.after(0, lambda: self._active_log_queue().put(
                    {"tag": "ERROR", "text": f"sentiment fetch: {e}"}
                ))
                return
            # Sentiment feed is currently global (crypto news only); show on
            # the active tab's panel.
            self.after(0, lambda: self._active_stats_panel().sentiment_feed.update_feed(feed))
        finally:
            self._sentiment_busy = False

    def _refresh_chart(self, period: str) -> None:
        try:
            if self._equity_buffer:
                ts = [s[0] for s in self._equity_buffer]
                vals = [s[1] for s in self._equity_buffer]
                self._active_stats_panel().chart.update_curve(ts, vals)
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"chart refresh failed: {e}"}
            )

    def _active_monitor_panel(self) -> MonitorPanel:
        return (
            self.crypto_monitor if self._active_class == "crypto"
            else self.stocks_monitor
        )

    def _tick_equity(self) -> None:
        if not self._data_active:
            return
        if not self._is_active_running():
            self.after(2000, self._tick_equity)
            return
        if not self._equity_busy:
            self._equity_busy = True
            threading.Thread(target=self._equity_bg, daemon=True).start()
        self.after(2000, self._tick_equity)

    def _equity_bg(self) -> None:
        cls = self._active_class
        try:
            try:
                rows = self.db.get_recent_trades_with_live_pnl(
                    limit=500, asset_class=cls,
                )
            except Exception:
                return
            realized = sum(
                float(r.get("pnl") or 0) for r in rows if not r.get("is_open")
            )
            unrealized = 0.0
            coord = self._active_coord
            live_prices = dict(coord.latest_prices) if coord is not None else {}
            for r in rows:
                if not r.get("is_open"):
                    continue
                entry = float(r.get("entry_price") or 0)
                qty = float(r.get("qty") or 0)
                if entry <= 0 or qty <= 0:
                    continue
                direction = 1 if (r.get("side") or "").upper() == "BUY" else -1
                tape = live_prices.get(r.get("symbol"))
                cur = float(tape["price"]) if tape else float(r.get("current_price") or 0)
                if cur > 0:
                    unrealized += (cur - entry) * qty * direction
            total = realized + unrealized
            sample = (datetime.now(timezone.utc), total)
            self.after(0, lambda s=sample: self._apply_equity(s))
        finally:
            self._equity_busy = False

    def _apply_equity(self, sample: tuple) -> None:
        try:
            self._equity_buffer.append(sample)
            if self._equity_buffer:
                ts = [s[0] for s in self._equity_buffer]
                vals = [s[1] for s in self._equity_buffer]
                self._active_stats_panel().chart.update_curve(ts, vals)
        except Exception as e:
            self._active_log_queue().put(
                {"tag": "ERROR", "text": f"apply equity: {e}"}
            )

    def _fetch_balance(self) -> float:
        coord = self._active_coord
        if coord and hasattr(coord, "get_balance"):
            try:
                return float(coord.get_balance())
            except Exception:
                pass
        # Fallback to a direct query so the chip stays populated even before
        # either bot is started.
        if not config.ALPACA_KEY:
            return 0.0
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                config.ALPACA_KEY, config.ALPACA_SECRET,
                paper=config.ALPACA_PAPER,
            )
            acc = client.get_account()
            return float(getattr(acc, "equity", 0) or 0)
        except Exception:
            return 0.0

    def _safe_get_orders(self, cls: AssetClass) -> list[dict]:
        coord = (
            self.crypto_coordinator if cls == "crypto"
            else self.stocks_coordinator
        )
        # If THIS asset's bot isn't running we don't surface its DB rows.
        # Keeps the live-trades panel empty on a tab the user hasn't started.
        if coord is None or not coord.is_running:
            return []
        try:
            rows = self.db.get_recent_trades_with_live_pnl(
                limit=40, asset_class=cls,
            )
        except Exception:
            return []
        live = dict(coord.latest_prices)
        for r in rows:
            sym = r.get("symbol")
            if not r.get("is_open"):
                continue
            tape = live.get(sym)
            if tape is None:
                continue
            cur = float(tape["price"])
            r["current_price"] = cur
            entry = float(r.get("entry_price") or 0)
            qty = float(r.get("qty") or 0)
            direction = 1 if (r.get("side") or "").upper() == "BUY" else -1
            if entry > 0 and qty > 0:
                r["live_pnl"] = (cur - entry) * qty * direction
        return rows

    # =================================================================
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.bg)
        self._app_title.configure(text_color=p.text)
        self._app_subtitle.configure(text_color=p.text_muted)
        self._logo.configure(
            image=theme.load_colored_icon("brain", _rgb(p.accent), size=22),
        )


# ====================================================================
def _pos_class(pos) -> str:
    raw = str(getattr(pos, "asset_class", "")).split(".")[-1].lower()
    if raw == "crypto":
        return "crypto"
    if raw in ("us_equity", "stock", "equity"):
        return "stock"
    return raw


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
