"""Generate a high-res app icon (1024x1024 PNG): big 'E' + rising chart overlay."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent
SIZE = 1024

BG_TOP = (14, 17, 23)        # #0E1117
BG_BOT = (34, 41, 59)        # #22293B
ACCENT = (34, 211, 238)      # #22D3EE cyan
ACCENT_DIM = (8, 145, 178)
PROFIT = (16, 185, 129)      # #10B981


def vertical_gradient(size: int, top: tuple, bot: tuple) -> Image.Image:
    img = Image.new("RGB", (size, size), top)
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = (
            int(top[0] * (1 - t) + bot[0] * t),
            int(top[1] * (1 - t) + bot[1] * t),
            int(top[2] * (1 - t) + bot[2] * t),
        )
        for x in range(size):
            px[x, y] = c
    return img


def find_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size, index=0)
        except Exception:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def main() -> None:
    # rounded base with gradient
    base = vertical_gradient(SIZE, BG_TOP, BG_BOT).convert("RGBA")
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, SIZE, SIZE), radius=int(SIZE * 0.22), fill=255,
    )
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)

    # soft accent glow in lower-right
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (int(SIZE * 0.50), int(SIZE * 0.50),
         int(SIZE * 1.10), int(SIZE * 1.10)),
        fill=(*ACCENT_DIM, 70),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(SIZE * 0.10)))
    icon = Image.alpha_composite(icon, glow)

    d = ImageDraw.Draw(icon)

    # --- big E letter, vertically centered, cyan ---
    font = find_bold_font(int(SIZE * 0.78))
    try:
        bbox = d.textbbox((0, 0), "E", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        offx = -bbox[0]
        offy = -bbox[1]
    except Exception:
        tw, th, offx, offy = int(SIZE * 0.5), int(SIZE * 0.6), 0, 0
    tx = (SIZE - tw) // 2 + offx
    ty = (SIZE - th) // 2 + offy
    d.text((tx, ty), "E", fill=ACCENT, font=font)

    # --- rising chart line overlay across the upper portion ---
    pts = [
        (0.10, 0.42), (0.24, 0.36), (0.36, 0.40),
        (0.50, 0.26), (0.64, 0.30), (0.78, 0.16),
    ]
    pts_px = [(int(SIZE * x), int(SIZE * y)) for x, y in pts]
    stroke = int(SIZE * 0.045)

    # dark shadow stroke underneath for separation from the E
    shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.line([(x + int(SIZE * 0.008), y + int(SIZE * 0.008)) for x, y in pts_px],
            fill=(0, 0, 0, 180), width=stroke + 8, joint="curve")
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(6))
    icon = Image.alpha_composite(icon, shadow_layer)
    d = ImageDraw.Draw(icon)

    # main rising line in profit green
    d.line(pts_px, fill=PROFIT, width=stroke, joint="curve")

    # vertex dots
    for px, py in pts_px:
        r = int(stroke * 0.85)
        d.ellipse((px - r, py - r, px + r, py + r), fill=PROFIT)

    # arrow head at the end
    tip = pts_px[-1]
    a = int(SIZE * 0.07)
    d.polygon(
        [
            (tip[0] - int(a * 0.1), tip[1] - a),       # top
            (tip[0] + a, tip[1] - int(a * 0.1)),       # right
            (tip[0] - int(a * 0.1), tip[1] + a),       # bottom
        ],
        fill=PROFIT,
    )

    out_path = OUT / "app_icon.png"
    icon.save(out_path, "PNG")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
