"""Settings dialog — edit bot_settings DB rows from inside the app."""
from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk

from ui import theme


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    helper: str
    suffix: str = ""
    is_int: bool = False
    section: str = "General"


# Order is the order they render in the dialog.
FIELDS: list[FieldSpec] = [
    # capital / risk
    FieldSpec("max_trading_capital",     "Max trading capital",
              "Total value of all open positions cannot exceed this.",
              suffix="USD", section="Capital & Risk"),
    FieldSpec("notional_per_trade",       "Notional per trade",
              "USD size of each single order.",
              suffix="USD", section="Capital & Risk"),
    FieldSpec("alpaca_commission_pct",    "Alpaca commission (per side)",
              "Commission Alpaca charges per buy or sell. Typical crypto is "
              "0.0015 = 0.15% (round-trip 0.30%). Deducted from realized PnL.",
              section="Capital & Risk"),
    # ATR-based SL/TP (primary)
    FieldSpec("atr_sl_mult",              "ATR stop-loss multiplier",
              "SL = entry − this × ATR(14). 1.5 is a good default for crypto "
              "majors; raise if noise hits SL too often.",
              section="ATR Risk Sizing"),
    FieldSpec("atr_tp_mult",              "ATR take-profit multiplier",
              "TP = entry + this × ATR(14). 3.0 gives a 2:1 reward:risk vs the "
              "default SL multiplier.",
              section="ATR Risk Sizing"),
    FieldSpec("atr_period",               "ATR period",
              "Bars used for ATR average. 14 is standard.",
              is_int=True, section="ATR Risk Sizing"),
    FieldSpec("trend_ema_15m_period",     "15m trend EMA period",
              "BUY is blocked when last 15m close is below this EMA on the 15m "
              "timeframe. 50 = ~12.5h trend. Resampled from 1m bars.",
              is_int=True, section="ATR Risk Sizing"),
    # Fallback SL/TP — only used if ATR unavailable
    FieldSpec("stop_loss_percent",        "Fallback stop-loss %",
              "Used only when ATR is unavailable (cold start with <14 bars). "
              "0.0100 = 1.00%.",
              section="Fallback Risk"),
    FieldSpec("take_profit_percent",      "Fallback take-profit %",
              "Used only when ATR is unavailable. 0.0200 = 2.00%.",
              section="Fallback Risk"),
    FieldSpec("quick_profit_pct",         "Quick-profit % (0 = disabled)",
              "When >0, force-exits any position at this net profit. Leave at 0 "
              "for v2 — it destroys the ATR-derived risk:reward.",
              section="Fallback Risk"),
    # Signal thresholds
    FieldSpec("rsi_buy_threshold",        "RSI BUY threshold",
              "BUY fires when RSI drops at/below this AND 15m trend is up.",
              section="Signal Thresholds"),
    FieldSpec("rsi_sell_threshold",       "RSI SELL threshold",
              "SELL fires (closes existing long) when RSI rises at/above this.",
              section="Signal Thresholds"),
    FieldSpec("ai_confidence_threshold",  "AI confidence threshold (legacy)",
              "Kept for back-compat. Math decisions bypass this; auto-tuner "
              "still uses it as a soft selectivity dial.",
              suffix="%", section="Signal Thresholds"),
    # LLM news veto
    FieldSpec("news_veto_sentiment_threshold", "News veto sentiment threshold",
              "LLM news-veto fires when at least N items have score ≤ this. "
              "Negative = bearish. −0.5 = clearly bearish snippet.",
              section="LLM News Veto"),
    FieldSpec("news_veto_min_count",      "News veto min items",
              "Minimum bearish snippets needed before the LLM is consulted.",
              is_int=True, section="LLM News Veto"),
    # freshness (intraday)
    FieldSpec("tweet_max_age_min",        "Tweet freshness window",
              "Tweets older than this many minutes are ignored. For intraday "
              "trading 15-30 is recommended.",
              suffix="min", is_int=True, section="Data Freshness"),
    FieldSpec("news_max_age_min",         "News freshness window",
              "RSS news older than this many minutes is ignored. 60-120 is "
              "reasonable for crypto.",
              suffix="min", is_int=True, section="Data Freshness"),
]


class SettingsDialog(ctk.CTkToplevel):
    """Modal dialog that reads/writes bot_settings."""

    def __init__(self, master, db, on_saved=None) -> None:
        super().__init__(master)
        self.db = db
        self.on_saved = on_saved

        p = theme.palette()
        self.title("Settings")
        self.geometry("680x720")
        self.configure(fg_color=p.bg)
        self.transient(master)
        self.grab_set()

        # Header
        header = ctk.CTkFrame(self, fg_color=p.panel, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header, text="", image=theme.load_colored_icon("gear", _rgb(p.accent), size=28),
        ).pack(side="left", padx=(20, 8), pady=14)
        ctk.CTkLabel(
            header, text="Bot Settings",
            text_color=p.text,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left", pady=14)
        ctk.CTkLabel(
            header, text="  (changes are read live by the bot when saved)",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", pady=14)

        # Scrollable body
        body = ctk.CTkScrollableFrame(self, fg_color=p.bg, scrollbar_button_color=p.border)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 8))

        self._entries: dict[str, ctk.CTkEntry] = {}
        last_section = None
        for f in FIELDS:
            if f.section != last_section:
                last_section = f.section
                ctk.CTkLabel(
                    body, text=f.section.upper(),
                    text_color=p.text_muted,
                    font=ctk.CTkFont(size=11, weight="bold"),
                ).pack(anchor="w", padx=4, pady=(14, 6))

            row = ctk.CTkFrame(
                body, fg_color=p.panel, border_color=p.border, border_width=1,
                corner_radius=10,
            )
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(0, weight=1)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.grid(row=0, column=0, sticky="nsew", padx=14, pady=12)
            ctk.CTkLabel(
                left, text=f.label, text_color=p.text,
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                left, text=f.helper, text_color=p.text_muted,
                font=ctk.CTkFont(size=11),
                anchor="w", justify="left", wraplength=420,
            ).pack(anchor="w", pady=(2, 0))

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.grid(row=0, column=1, sticky="e", padx=14, pady=12)

            entry = ctk.CTkEntry(
                right, width=110, height=34,
                font=ctk.CTkFont(size=14, weight="bold"),
                justify="right",
                fg_color=p.panel_alt, border_color=p.border,
                text_color=p.text,
            )
            entry.pack(side="left")
            if f.suffix:
                ctk.CTkLabel(
                    right, text=f.suffix, text_color=p.text_muted,
                    font=ctk.CTkFont(size=12),
                ).pack(side="left", padx=(6, 0))

            try:
                current = self.db.get_setting(f.key)
            except Exception:
                current = None
            if current is not None:
                if f.is_int:
                    entry.insert(0, str(int(current)))
                else:
                    entry.insert(0, _fmt(current))
            self._entries[f.key] = entry

        # Footer
        footer = ctk.CTkFrame(self, fg_color=p.bg)
        footer.pack(fill="x", padx=20, pady=(8, 16))

        self._status = ctk.CTkLabel(
            footer, text="", text_color=p.text_muted,
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self._status.pack(side="left")

        ctk.CTkButton(
            footer, text="Cancel", width=110, height=40,
            command=self._on_cancel,
            fg_color=p.panel_alt, hover_color=p.border,
            text_color=p.text,
            corner_radius=10,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            footer, text="Save", width=140, height=40,
            command=self._on_save,
            fg_color=p.accent, hover_color=p.accent_soft,
            text_color="white",
            font=ctk.CTkFont(size=14, weight="bold"),
            corner_radius=10,
        ).pack(side="right")

        self.bind("<Escape>", lambda _e: self._on_cancel())
        self.bind("<Return>", lambda _e: self._on_save())

    # ------------------------------------------------------------------
    def _on_cancel(self) -> None:
        self.grab_release()
        self.destroy()

    def _on_save(self) -> None:
        errors: list[str] = []
        parsed: dict[str, float] = {}
        for f in FIELDS:
            raw = self._entries[f.key].get().strip().replace(",", ".")
            if not raw:
                errors.append(f"{f.label} cannot be empty")
                continue
            try:
                val = float(raw)
            except ValueError:
                errors.append(f"{f.label}: invalid number")
                continue
            if val < 0:
                errors.append(f"{f.label}: cannot be negative")
                continue
            parsed[f.key] = val

        if errors:
            self._status.configure(
                text="  " + "  •  ".join(errors[:2]),
                text_color=theme.palette().loss,
            )
            return

        try:
            for key, val in parsed.items():
                self.db.update_setting(key, val)
        except Exception as e:
            self._status.configure(
                text=f"  DB error: {e}",
                text_color=theme.palette().loss,
            )
            return

        if self.on_saved:
            try:
                self.on_saved(parsed)
            except Exception:
                pass

        self._status.configure(
            text="  ✓ saved",
            text_color=theme.palette().profit,
        )
        self.after(700, self._on_cancel)


def _fmt(v) -> str:
    f = float(v)
    if f == int(f):
        return f"{int(f)}"
    return f"{f:g}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
