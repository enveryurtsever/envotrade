"""Auto-scrolling live operations console fed from a thread-safe queue."""
from __future__ import annotations

import queue
from datetime import datetime

import customtkinter as ctk

from ui import theme

LOG_TAG_COLORS = {
    "STREAM":    "#22D3EE",   # cyan — live data
    "AI":        "#A78BFA",   # purple — model decisions
    "ORDER":     "#10B981",   # green — execution
    "SENTIMENT": "#F59E0B",   # amber — twitter/news ingest
    "SCANNER":   "#3B82F6",   # blue — symbol scan
    "LEARNING":  "#FBBF24",   # gold — accumulated wisdom (was pink/negative)
    "ERROR":     "#EF4444",   # red — failures
    "INFO":      "#94A3B8",   # gray — neutral
}


class LiveConsole(ctk.CTkFrame):
    """Thread-safe textbox that drains a queue of {tag, text} dicts."""

    def __init__(self, master, log_queue: queue.Queue, **kwargs) -> None:
        p = theme.palette()
        super().__init__(
            master,
            fg_color=p.panel,
            border_color=p.border,
            border_width=1,
            corner_radius=12,
            **kwargs,
        )
        self.log_queue = log_queue
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        self._icon = ctk.CTkLabel(
            header, text="", image=theme.load_icon("activity", size=18),
        )
        self._icon.pack(side="left")
        self._title = ctk.CTkLabel(
            header, text="  LIVE OPERATIONS",
            text_color=p.text_muted,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._title.pack(side="left")

        self._console = ctk.CTkTextbox(
            self,
            fg_color=p.panel, text_color=p.text,
            font=ctk.CTkFont(family="Menlo", size=11),
            activate_scrollbars=True,
            corner_radius=8,
        )
        self._console.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
        self._console.configure(state="disabled")
        for tag, color in LOG_TAG_COLORS.items():
            try:
                self._console._textbox.tag_config(tag, foreground=color)
            except Exception:
                pass

        theme.on_theme_change(self._apply_theme)
        self.after(150, self._poll_logs)

    # ------------------------------------------------------------------
    def _poll_logs(self) -> None:
        drained = 0
        while drained < 50:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)
            drained += 1
        self.after(150, self._poll_logs)

    def _append_log(self, msg) -> None:
        if isinstance(msg, str):
            tag = "INFO"
            text = msg
        else:
            tag = (msg.get("tag") or "INFO").upper()
            text = msg.get("text", "")
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{tag:>9}]  {text}\n"
        self._console.configure(state="normal")
        start = self._console.index("end-1c")
        self._console.insert("end", line)
        end = self._console.index("end-1c")
        try:
            self._console._textbox.tag_add(tag, start, end)
        except Exception:
            pass
        line_count = int(self._console.index("end-1c").split(".")[0])
        if line_count > 2000:
            self._console.delete("1.0", f"{line_count - 1500}.0")
        self._console.see("end")
        self._console.configure(state="disabled")

    # ------------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self.configure(fg_color=p.panel, border_color=p.border)
        self._console.configure(fg_color=p.panel, text_color=p.text)
        self._title.configure(text_color=p.text_muted)
