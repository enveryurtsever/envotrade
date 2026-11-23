"""Generate clean monochrome PNG icons (48x48) using Pillow.

Run once: `python assets/generate_icons.py`
Output: assets/icons/<name>.png  (white fill on transparent — tinted at runtime)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"
OUT.mkdir(parents=True, exist_ok=True)

SIZE = 96  # render large, downscale at use
STROKE = 6
COLOR = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
    return img, ImageDraw.Draw(img)


def save(img: Image.Image, name: str) -> None:
    img.save(OUT / f"{name}.png", "PNG")


def play() -> None:
    img, d = _canvas()
    d.polygon([(28, 18), (28, 78), (78, 48)], fill=COLOR)
    save(img, "play")


def stop() -> None:
    img, d = _canvas()
    d.rectangle((26, 26, 70, 70), fill=COLOR)
    save(img, "stop")


def sun() -> None:
    img, d = _canvas()
    d.ellipse((34, 34, 62, 62), outline=COLOR, width=STROKE)
    for ang in range(0, 360, 45):
        import math
        x = 48 + 28 * math.cos(math.radians(ang))
        y = 48 + 28 * math.sin(math.radians(ang))
        x2 = 48 + 40 * math.cos(math.radians(ang))
        y2 = 48 + 40 * math.sin(math.radians(ang))
        d.line((x, y, x2, y2), fill=COLOR, width=STROKE)
    save(img, "sun")


def moon() -> None:
    img, d = _canvas()
    d.ellipse((20, 20, 76, 76), fill=COLOR)
    d.ellipse((34, 14, 84, 64), fill=TRANSPARENT)
    save(img, "moon")


def chart_line() -> None:
    img, d = _canvas()
    d.line((16, 70, 36, 50, 52, 60, 70, 30, 84, 40), fill=COLOR, width=STROKE, joint="curve")
    d.line((10, 80, 86, 80), fill=COLOR, width=STROKE)
    save(img, "chart-line")


def wallet() -> None:
    img, d = _canvas()
    d.rounded_rectangle((14, 26, 82, 72), radius=6, outline=COLOR, width=STROKE)
    d.rectangle((62, 44, 86, 56), fill=COLOR)
    d.ellipse((70, 46, 78, 54), fill=TRANSPARENT)
    save(img, "wallet")


def trophy() -> None:
    img, d = _canvas()
    d.rectangle((30, 18, 66, 50), fill=COLOR)
    d.ellipse((22, 24, 36, 42), outline=COLOR, width=STROKE)
    d.ellipse((60, 24, 74, 42), outline=COLOR, width=STROKE)
    d.rectangle((36, 50, 60, 60), fill=COLOR)
    d.rectangle((28, 60, 68, 70), fill=COLOR)
    save(img, "trophy")


def alert() -> None:
    img, d = _canvas()
    d.polygon([(48, 14), (84, 78), (12, 78)], outline=COLOR, width=STROKE)
    d.rectangle((44, 34, 52, 56), fill=COLOR)
    d.ellipse((43, 62, 53, 72), fill=COLOR)
    save(img, "alert")


def twitter() -> None:
    img, d = _canvas()
    d.polygon(
        [(16, 18), (40, 50), (16, 78), (28, 78), (46, 58), (64, 78), (84, 78),
         (60, 46), (82, 18), (70, 18), (54, 38), (38, 18)],
        fill=COLOR,
    )
    save(img, "twitter")


def news() -> None:
    img, d = _canvas()
    d.rounded_rectangle((14, 18, 82, 78), radius=4, outline=COLOR, width=STROKE)
    d.line((22, 30, 74, 30), fill=COLOR, width=STROKE)
    d.line((22, 44, 74, 44), fill=COLOR, width=STROKE)
    d.line((22, 56, 60, 56), fill=COLOR, width=STROKE)
    d.line((22, 68, 50, 68), fill=COLOR, width=STROKE)
    save(img, "news")


def brain() -> None:
    img, d = _canvas()
    d.ellipse((14, 24, 50, 70), outline=COLOR, width=STROKE)
    d.ellipse((46, 24, 82, 70), outline=COLOR, width=STROKE)
    d.line((48, 30, 48, 64), fill=COLOR, width=STROKE)
    d.line((28, 44, 38, 44), fill=COLOR, width=STROKE)
    d.line((58, 44, 68, 44), fill=COLOR, width=STROKE)
    save(img, "brain")


def db() -> None:
    img, d = _canvas()
    d.ellipse((20, 16, 76, 30), outline=COLOR, width=STROKE)
    d.line((20, 23, 20, 73), fill=COLOR, width=STROKE)
    d.line((76, 23, 76, 73), fill=COLOR, width=STROKE)
    d.arc((20, 66, 76, 80), 0, 180, fill=COLOR, width=STROKE)
    d.arc((20, 42, 76, 56), 0, 180, fill=COLOR, width=STROKE)
    save(img, "db")


def activity() -> None:
    img, d = _canvas()
    d.line((10, 48, 30, 48, 38, 24, 48, 72, 58, 36, 68, 48, 86, 48),
           fill=COLOR, width=STROKE, joint="curve")
    save(img, "activity")


def gear() -> None:
    import math
    img, d = _canvas()
    cx, cy = 48, 48
    teeth = 8
    outer_r = 36
    inner_r = 28
    tooth_w = 8

    # build star/gear outline by alternating outer/inner points
    pts = []
    for i in range(teeth * 2):
        ang = (i / (teeth * 2)) * 2 * math.pi - math.pi / 2
        # at each "tooth" use outer_r, between teeth use inner_r
        # but to get rectangular teeth we use two outer points then two inner
        # Simpler: build teeth as rectangles overlaid on a base circle.
        r = outer_r if (i // 2) % 2 == 0 else inner_r
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    # Fall back to a clean approach: draw a circle + 8 rectangular teeth
    img2, d2 = _canvas()
    d2.ellipse((cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r), fill=COLOR)
    for i in range(teeth):
        ang = (i / teeth) * 2 * math.pi
        tx = cx + (inner_r + 2) * math.cos(ang)
        ty = cy + (inner_r + 2) * math.sin(ang)
        # rotate a small rectangle around (tx, ty)
        # for simplicity, approximate with a filled small circle for each tooth tip
        d2.ellipse(
            (tx - tooth_w, ty - tooth_w, tx + tooth_w, ty + tooth_w),
            fill=COLOR,
        )
    # center hole
    d2.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=TRANSPARENT)
    save(img2, "gear")


if __name__ == "__main__":
    for fn in (play, stop, sun, moon, chart_line, wallet, trophy,
               alert, twitter, news, brain, db, activity, gear):
        fn()
    print(f"Generated icons in {OUT}")
