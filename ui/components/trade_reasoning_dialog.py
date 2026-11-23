"""Modal that explains why the AI opened (or closed) a specific trade.

Loads three slices of context from the DB:
  • the closest trading_signals row (`raw_ai_output` is JSON: reason, veto)
  • a tight sentiment window around the order timestamp
  • the order itself (symbol, side, qty, entry, TP, SL)

Triggered from MonitorPanel.TradeRow on click — keep it cheap and self-
contained so it can be opened/closed multiple times per session.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import customtkinter as ctk

from ui import theme


class TradeReasoningDialog(ctk.CTkToplevel):
    def __init__(self, master, *, db, order: dict) -> None:
        super().__init__(master)
        p = theme.palette()
        self.title("Trade Reasoning")
        self.configure(fg_color=p.bg)
        self.geometry("640x560")
        try:
            self.transient(master)
        except Exception:
            pass

        self._db = db
        self._order = order
        self._build()
        self._load_context()

        try:
            self.after(100, self.lift)
            self.after(120, self.focus_force)
        except Exception:
            pass

    # ---------------------------------------------------------------
    def _build(self) -> None:
        p = theme.palette()
        # Header
        head = ctk.CTkFrame(self, fg_color=p.panel, corner_radius=0, height=72)
        head.pack(fill="x")
        head.pack_propagate(False)
        head_inner = ctk.CTkFrame(head, fg_color="transparent")
        head_inner.pack(side="left", padx=18, pady=10)

        side = (self._order.get("side") or "").upper()
        side_color = p.profit if side == "BUY" else p.loss
        sym = self._order.get("symbol", "")
        qty = float(self._order.get("qty") or 0)
        entry = float(self._order.get("entry_price") or self._order.get("price") or 0)

        line1 = ctk.CTkFrame(head_inner, fg_color="transparent")
        line1.pack(anchor="w")
        ctk.CTkLabel(
            line1, text=f" {side} ",
            text_color="white", fg_color=side_color,
            corner_radius=4, padx=8, pady=2,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            line1, text=str(sym),
            text_color=p.text, font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            head_inner, text=f"qty {qty:g}  ·  entry ${_fmt_price(entry)}",
            text_color=p.text_muted, font=ctk.CTkFont(size=11),
        ).pack(anchor="w")

        close_btn = ctk.CTkButton(
            head, text="✕", width=36, height=36,
            command=self.destroy,
            fg_color="transparent", hover_color=p.panel_alt,
            text_color=p.text_muted, corner_radius=18,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        close_btn.pack(side="right", padx=18, pady=18)

        # Body
        body = ctk.CTkFrame(self, fg_color=p.bg)
        body.pack(fill="both", expand=True, padx=18, pady=12)

        self._section_decision = _Section(body, "AI DECISION")
        self._section_decision.pack(fill="x", pady=(0, 10))
        self._section_levels = _Section(body, "TP / SL & RISK")
        self._section_levels.pack(fill="x", pady=(0, 10))
        self._section_sentiment = _Section(body, "SENTIMENT AROUND ENTRY")
        self._section_sentiment.pack(fill="both", expand=True)

    # ---------------------------------------------------------------
    def _load_context(self) -> None:
        order = self._order
        symbol = order.get("symbol", "")
        ts = order.get("timestamp")
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        asset_class = order.get("asset_class")

        # --- AI decision section
        try:
            sig = self._db.get_signal_for_order(symbol, ts, asset_class=asset_class)
        except Exception as e:
            sig = None
            self._section_decision.set_body(f"signal lookup failed: {e}")

        if sig is not None:
            decision = (sig.get("ai_decision") or "?").upper()
            conf = float(sig.get("ai_confidence") or 0)
            rsi = sig.get("rsi")
            raw = sig.get("raw_ai_output") or ""
            parsed = _parse_json(raw)
            reason = parsed.get("reason") if isinstance(parsed, dict) else None
            veto = parsed.get("veto") if isinstance(parsed, dict) else None
            sig_ts = sig.get("timestamp")
            lines = [f"decision: {decision}   confidence: {conf:.0f}%"]
            if rsi is not None:
                try:
                    lines.append(f"RSI at signal: {float(rsi):.1f}")
                except Exception:
                    pass
            if sig_ts is not None:
                try:
                    lines.append(f"signal time: {sig_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                except Exception:
                    pass
            if reason:
                lines.append("")
                lines.append(f"why: {reason}")
            if veto:
                lines.append("")
                lines.append(f"veto context: {json.dumps(veto, default=str)[:400]}")
            if not reason and not veto and raw:
                lines.append("")
                lines.append(raw[:600])
            self._section_decision.set_body("\n".join(lines))
        else:
            self._section_decision.set_body(
                "No signal row found near this order's timestamp.\n"
                "This can happen for manually-adopted positions or older "
                "trades whose signals were rotated out of the table.",
            )

        # --- TP / SL section
        tp = order.get("tp_price")
        sl = order.get("sl_price")
        entry = float(order.get("entry_price") or order.get("price") or 0)
        cur = order.get("current_price")
        pnl = order.get("live_pnl")
        if pnl is None:
            pnl = order.get("pnl")
        lines: list[str] = []
        if tp is not None:
            try:
                tp_f = float(tp)
                lines.append(f"TP: ${_fmt_price(tp_f)}   ({_pct_from(entry, tp_f)})")
            except Exception:
                pass
        if sl is not None:
            try:
                sl_f = float(sl)
                lines.append(f"SL: ${_fmt_price(sl_f)}   ({_pct_from(entry, sl_f)})")
            except Exception:
                pass
        if cur is not None:
            try:
                lines.append(f"now: ${_fmt_price(float(cur))}")
            except Exception:
                pass
        if pnl is not None:
            try:
                pnl_f = float(pnl)
                sign = "+" if pnl_f >= 0 else "-"
                lines.append(f"PnL: {sign}${abs(pnl_f):.2f}")
            except Exception:
                pass
        self._section_levels.set_body("\n".join(lines) or "no bracket levels recorded")

        # --- Sentiment section
        try:
            sent = self._db.get_sentiment_near(symbol, ts, window_min=60) if ts else []
        except Exception:
            sent = []
        if not sent:
            self._section_sentiment.set_body(
                "No sentiment items within ±60 minutes of entry."
            )
        else:
            blocks = []
            for s in sent[-12:][::-1]:
                source = s.get("source", "?")
                score = s.get("sentiment_score")
                try:
                    score_str = f"{float(score):+.2f}"
                except Exception:
                    score_str = "?"
                summary = (s.get("summary") or "").strip()[:200]
                tstr = ""
                try:
                    tstr = s["timestamp"].strftime("%H:%M")
                except Exception:
                    pass
                blocks.append(f"[{tstr}] {source} {score_str}  {summary}")
            self._section_sentiment.set_body("\n".join(blocks))


# ====================================================================
class _Section(ctk.CTkFrame):
    """Titled panel with a monospace body textbox."""

    def __init__(self, master, title: str) -> None:
        p = theme.palette()
        super().__init__(
            master, fg_color=p.panel, border_color=p.border, border_width=1,
            corner_radius=10,
        )
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self, text=title, text_color=p.text_muted,
            font=ctk.CTkFont(size=10, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        self._body = ctk.CTkTextbox(
            self, fg_color=p.panel, text_color=p.text,
            font=ctk.CTkFont(family="Menlo", size=11),
            activate_scrollbars=True, corner_radius=8,
            height=110,
        )
        self._body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self._body.configure(state="disabled")

    def set_body(self, text: str) -> None:
        self._body.configure(state="normal")
        self._body.delete("1.0", "end")
        self._body.insert("1.0", text)
        self._body.configure(state="disabled")


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {}


def _fmt_price(px: float) -> str:
    if px >= 1000:
        return f"{px:,.2f}"
    if px >= 1:
        return f"{px:,.4f}"
    return f"{px:.5f}"


def _pct_from(base: float, target: float) -> str:
    if base <= 0:
        return ""
    pct = (target - base) / base * 100.0
    return f"{pct:+.2f}%"
