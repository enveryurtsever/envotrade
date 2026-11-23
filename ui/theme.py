"""Theme palettes, icon loader, Matplotlib style sync."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import customtkinter as ctk
import matplotlib as mpl
from PIL import Image

import config

Mode = Literal["dark", "light"]


@dataclass(frozen=True)
class Palette:
    bg: str
    panel: str
    panel_alt: str
    text: str
    text_muted: str
    accent: str
    accent_soft: str
    profit: str
    profit_soft: str
    loss: str
    loss_soft: str
    border: str
    icon_tint: tuple[int, int, int]


DARK = Palette(
    bg="#0E1117",
    panel="#1A1F2E",
    panel_alt="#22293B",
    text="#F1F5F9",
    text_muted="#94A3B8",
    accent="#22D3EE",
    accent_soft="#0E7490",
    profit="#10B981",
    profit_soft="#10B98122",
    loss="#EF4444",
    loss_soft="#EF444422",
    border="#2D3748",
    icon_tint=(241, 245, 249),
)

LIGHT = Palette(
    bg="#F8FAFC",
    panel="#FFFFFF",
    panel_alt="#F1F5F9",
    text="#0F172A",
    text_muted="#475569",
    accent="#0891B2",
    accent_soft="#CFFAFE",
    profit="#059669",
    profit_soft="#10B98122",
    loss="#DC2626",
    loss_soft="#EF444422",
    border="#E2E8F0",
    icon_tint=(15, 23, 42),
)


_listeners: list = []
_current_mode: Mode = "dark"


def palette(mode: Mode | None = None) -> Palette:
    return DARK if (mode or _current_mode) == "dark" else LIGHT


def current_mode() -> Mode:
    return _current_mode


def set_mode(mode: Mode) -> None:
    global _current_mode
    _current_mode = mode
    ctk.set_appearance_mode(mode)
    apply_matplotlib_style(mode)
    for cb in list(_listeners):
        try:
            cb(mode)
        except Exception:
            pass


def toggle_mode() -> Mode:
    set_mode("light" if _current_mode == "dark" else "dark")
    return _current_mode


def on_theme_change(callback) -> None:
    _listeners.append(callback)


def apply_matplotlib_style(mode: Mode) -> None:
    p = palette(mode)
    mpl.rcParams.update(
        {
            "figure.facecolor": p.panel,
            "axes.facecolor": p.panel,
            "savefig.facecolor": p.panel,
            "axes.edgecolor": p.border,
            "axes.labelcolor": p.text_muted,
            "axes.titlecolor": p.text,
            "xtick.color": p.text_muted,
            "ytick.color": p.text_muted,
            "grid.color": p.border,
            "grid.alpha": 0.4,
            "text.color": p.text,
            "lines.linewidth": 1.8,
            "lines.color": p.accent,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "font.size": 9,
        }
    )


# ---------- Icon loader ---------------------------------------
_icon_cache: dict[tuple[str, int], ctk.CTkImage] = {}


def load_icon(name: str, size: int = 24) -> ctk.CTkImage:
    """Return a CTkImage that auto-tints for the active theme."""
    key = (name, size)
    if key in _icon_cache:
        return _icon_cache[key]

    path = config.ICONS_DIR / f"{name}.png"
    if not path.exists():
        # placeholder if asset missing
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        _icon_cache[key] = ctk_img
        return ctk_img

    raw = Image.open(path).convert("RGBA")

    def _tinted(rgb: tuple[int, int, int]) -> Image.Image:
        r, g, b = rgb
        out = Image.new("RGBA", raw.size, (r, g, b, 0))
        out.putalpha(raw.split()[-1])
        return out.resize((size * 2, size * 2), Image.LANCZOS)

    ctk_img = ctk.CTkImage(
        light_image=_tinted(LIGHT.icon_tint),
        dark_image=_tinted(DARK.icon_tint),
        size=(size, size),
    )
    _icon_cache[key] = ctk_img
    return ctk_img


def load_colored_icon(name: str, color: tuple[int, int, int], size: int = 24) -> ctk.CTkImage:
    """Return a CTkImage tinted to a specific RGB (used for profit/loss/accent icons)."""
    path = config.ICONS_DIR / f"{name}.png"
    if not path.exists():
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    raw = Image.open(path).convert("RGBA")
    out = Image.new("RGBA", raw.size, (*color, 0))
    out.putalpha(raw.split()[-1])
    out = out.resize((size * 2, size * 2), Image.LANCZOS)
    return ctk.CTkImage(light_image=out, dark_image=out, size=(size, size))
