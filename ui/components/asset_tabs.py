"""CRYPTO / STOCKS tab switcher for the top bar.

Wraps CTkSegmentedButton with theme-aware colors and a typed callback.
The MainWindow owns which coordinator the rest of the UI reads from;
this widget just signals which one is active.
"""
from __future__ import annotations

from typing import Callable, Literal

import customtkinter as ctk

from ui import theme

AssetClass = Literal["crypto", "stock"]
_LABELS: dict[str, AssetClass] = {"CRYPTO": "crypto", "STOCKS": "stock"}
_LABEL_OF: dict[AssetClass, str] = {v: k for k, v in _LABELS.items()}


class AssetTabs(ctk.CTkFrame):
    def __init__(
        self,
        master,
        *,
        on_change: Callable[[AssetClass], None],
        initial: AssetClass = "crypto",
        **kwargs,
    ) -> None:
        p = theme.palette()
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_change = on_change
        self._current: AssetClass = initial

        self._btn = ctk.CTkSegmentedButton(
            self,
            values=list(_LABELS.keys()),
            command=self._handle,
            fg_color=p.panel_alt,
            selected_color=p.accent,
            selected_hover_color=p.accent_soft,
            unselected_color=p.panel_alt,
            unselected_hover_color=p.border,
            text_color=p.text,
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34,
            corner_radius=8,
        )
        self._btn.set(_LABEL_OF[initial])
        self._btn.pack(side="left")

        theme.on_theme_change(self._apply_theme)

    # ------------------------------------------------------------------
    def _handle(self, label: str) -> None:
        cls = _LABELS.get(label)
        if cls is None or cls == self._current:
            return
        self._current = cls
        try:
            self._on_change(cls)
        except Exception:
            pass

    def current(self) -> AssetClass:
        return self._current

    def set_current(self, cls: AssetClass) -> None:
        self._current = cls
        self._btn.set(_LABEL_OF[cls])

    # ------------------------------------------------------------------
    def _apply_theme(self, _mode: str) -> None:
        p = theme.palette()
        self._btn.configure(
            fg_color=p.panel_alt,
            selected_color=p.accent,
            selected_hover_color=p.accent_soft,
            unselected_color=p.panel_alt,
            unselected_hover_color=p.border,
            text_color=p.text,
        )
