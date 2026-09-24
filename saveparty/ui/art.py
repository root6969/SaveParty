"""Procedurally drawn 'gamer' artwork - no asset files, everything generated.

Each game gets a deterministic neon gradient (hue from a hash of its id), so
banners and chips are consistent between runs and unique per game.
"""

from __future__ import annotations

import colorsys
import hashlib

import customtkinter as ctk
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

_cache: dict = {}


def _hue(seed: str) -> float:
    return (int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4], 16) % 360) / 360.0


def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _font(size: int):
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _gradient(width: int, height: int, h1: float, h2: float, v: float = 0.30) -> Image.Image:
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for x in range(width):
        t = x / max(width - 1, 1)
        draw.line([(x, 0), (x, height)], fill=_rgb(h1 + (h2 - h1) * t, 0.62, v))
    return img


def _grid(img: Image.Image, spacing: int = 36, alpha: int = 26) -> None:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size
    for x in range(0, w, spacing):
        draw.line([(x, 0), (x, h)], fill=(255, 255, 255, alpha))
    for y in range(0, h, spacing):
        draw.line([(0, y), (w, y)], fill=(255, 255, 255, alpha))
    img.paste(overlay, (0, 0), overlay)


def _glow(img: Image.Image, cx: int, cy: int, radius: int, color: tuple, alpha: int = 130) -> None:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(*color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(overlay, (0, 0), overlay)


def banner(seed: str, emblem: str, width: int = 880, height: int = 86) -> ctk.CTkImage:
    """Hero banner: neon gradient + grid + glows + big emblem letter."""
    key = ("banner", seed, emblem, width, height)
    if key in _cache:
        return _cache[key]
    h1 = _hue(seed)
    img = _gradient(width, height, h1, h1 + 0.12).convert("RGBA")
    _grid(img)
    _glow(img, int(width * 0.82), height // 2, height, _rgb(h1 + 0.5, 0.7, 0.9), alpha=70)
    _glow(img, int(width * 0.12), height // 3, height // 2, _rgb(h1, 0.8, 1.0), alpha=90)
    draw = ImageDraw.Draw(img)
    letter = (emblem or "?")[0].upper()
    font = _font(int(height * 1.15))
    draw.text((width - int(height * 1.0), -int(height * 0.22)), letter,
              font=font, fill=(255, 255, 255, 46))
    draw.text((16, height - 26), "SAVEPARTY", font=_font(13), fill=(255, 255, 255, 80))
    for y in range(0, height, 4):  # scanlines
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, 34))
    result = ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))
    _cache[key] = result
    return result


def chip(seed: str, letter: str, size: int = 30) -> ctk.CTkImage:
    """Small rounded gradient tile with the game's initial - used as list art."""
    key = ("chip", seed, letter, size)
    if key in _cache:
        return _cache[key]
    scale = 4  # draw big, downscale for smooth corners
    big = size * scale
    h1 = _hue(seed)
    img = _gradient(big, big, h1, h1 + 0.10, v=0.55).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 4, fill=255)
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)
    font = _font(int(big * 0.58))
    text = (letter or "?")[0].upper()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((big - box[2] + box[0]) / 2 - box[0], (big - box[3] + box[1]) / 2 - box[1] - big * 0.04),
        text, font=font, fill=(255, 255, 255, 235),
    )
    img = img.resize((size, size), Image.LANCZOS)
    result = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    _cache[key] = result
    return result


def brand_banner(width: int = 204, height: int = 58) -> ctk.CTkImage:
    """Sidebar art: purple-to-cyan gradient with a drawn gamepad silhouette."""
    key = ("brand", width, height)
    if key in _cache:
        return _cache[key]
    img = _gradient(width, height, 0.72, 0.52, v=0.42).convert("RGBA")
    _grid(img, spacing=18, alpha=20)
    draw = ImageDraw.Draw(img)
    cx, cy = width // 2, height // 2 + 2
    body_w, body_h = 92, 34
    draw.rounded_rectangle(
        [cx - body_w // 2, cy - body_h // 2, cx + body_w // 2, cy + body_h // 2],
        radius=16, fill=(16, 16, 22, 235), outline=(255, 255, 255, 90), width=2,
    )
    # d-pad
    dx, dy = cx - 26, cy
    draw.rectangle([dx - 9, dy - 3, dx + 9, dy + 3], fill=(255, 255, 255, 200))
    draw.rectangle([dx - 3, dy - 9, dx + 3, dy + 9], fill=(255, 255, 255, 200))
    # buttons
    draw.ellipse([cx + 16, cy - 9, cx + 26, cy + 1], fill=(124, 92, 255, 255))
    draw.ellipse([cx + 28, cy - 1, cx + 38, cy + 9], fill=(47, 224, 141, 255))
    result = ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))
    _cache[key] = result
    return result


def _glow_text(
    parts: list[tuple[str, tuple[int, int, int]]],
    font_size: int,
    glow_rgb: tuple[int, int, int],
    strength: float,
    width: int,
    height: int,
) -> Image.Image:
    """Render text as a real neon sign: three stacked bloom halos (wide, mid,
    tight) behind the letters, all breathing with `strength` 0..1, and the
    letters themselves brightening toward white at the peak."""
    font = _font(font_size)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    measure = ImageDraw.Draw(img)
    total = sum(measure.textlength(text, font=font) for text, _c in parts)
    x0 = (width - total) / 2
    y = (height - font_size) / 2 - font_size * 0.10

    # Stamp the text once; its alpha channel drives every halo layer.
    stamp = Image.new("L", (width, height), 0)
    stamp_draw = ImageDraw.Draw(stamp)
    x = x0
    for text, _color in parts:
        stamp_draw.text((x, y), text, font=font, fill=255)
        x += measure.textlength(text, font=font)

    peak = int(110 + 145 * strength)
    for blur, level in ((12 + 10 * strength, 0.55), (6 + 5 * strength, 0.85), (2.5, 1.0)):
        alpha = stamp.filter(ImageFilter.GaussianBlur(blur))
        alpha = alpha.point(lambda v, lv=level: int(min(255, v * 1.8) * lv * peak / 255))
        halo = Image.new("RGBA", (width, height), (*glow_rgb, 0))
        halo.putalpha(alpha)
        img.alpha_composite(halo)
        img.alpha_composite(halo)  # additive double pass = bright bloom core

    # Sharp letters on top, tinted toward white as the glow peaks.
    x = x0
    for text, color in parts:
        lit = tuple(int(c + (255 - c) * 0.45 * strength) for c in color)
        measure.text((x, y), text, font=font, fill=(*lit, 255))
        x += measure.textlength(text, font=font)
    return img


def _sweep_frame(
    parts: list[tuple[str, tuple[int, int, int]]],
    font_size: int,
    glow_rgb: tuple[int, int, int],
    center_frac: float,
    width: int,
    height: int,
    band_frac: float = 0.18,
    ambient: float = 0.26,
) -> Image.Image:
    """One frame of a left-to-right light sweep: a soft vertical band of glow
    centered at `center_frac` of the width slides across the letters, lighting
    the halo and adding a white shine where it passes. `ambient` keeps the text
    faintly lit everywhere so it never goes fully dark between sweeps."""
    import math

    font = _font(font_size)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    total = sum(measure.textlength(text, font=font) for text, _c in parts)
    x0 = (width - total) / 2
    y = (height - font_size) / 2 - font_size * 0.10

    # Alpha mask of the letters, reused for every glow layer.
    stamp = Image.new("L", (width, height), 0)
    stamp_draw = ImageDraw.Draw(stamp)
    x = x0
    for text, _color in parts:
        stamp_draw.text((x, y), text, font=font, fill=255)
        x += measure.textlength(text, font=font)

    # Moving spotlight: a 1px-tall gaussian band stretched to full height.
    center = center_frac * width
    sigma = max(band_frac * width, 1.0)
    column = [int(255 * math.exp(-((cx - center) ** 2) / (2 * sigma * sigma))) for cx in range(width)]
    strip = Image.new("L", (width, 1))
    strip.putdata(column)
    spot = strip.resize((width, height))

    lit = ImageChops.multiply(stamp, spot)  # letters within the moving light
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for blur, src, boost, doubled in (
        (11, lit, 1.0, True),
        (5, lit, 1.0, True),
        (6, stamp.point(lambda v: int(v * ambient)), 0.7, False),  # constant soft halo
    ):
        alpha = src.filter(ImageFilter.GaussianBlur(blur)).point(
            lambda v, b=boost: int(min(255, v * 1.7 * b))
        )
        halo = Image.new("RGBA", (width, height), (*glow_rgb, 0))
        halo.putalpha(alpha)
        img.alpha_composite(halo)
        if doubled:
            img.alpha_composite(halo)

    # Sharp letters, then a white shine riding the spotlight.
    draw = ImageDraw.Draw(img)
    x = x0
    for text, color in parts:
        draw.text((x, y), text, font=font, fill=(*color, 255))
        x += measure.textlength(text, font=font)
    shine = lit.point(lambda v: int(v * 0.65))
    white = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    white.putalpha(shine)
    img.alpha_composite(white)
    return img


def _sweep_series(parts, font_size, glow_rgb, n, width, height, **kw) -> list[ctk.CTkImage]:
    """A full left-to-right sweep cycle: the light enters off the left edge and
    exits off the right, then loops."""
    frames = []
    for i in range(n):
        center_frac = -0.28 + 1.56 * i / (n - 1)  # off-left -> off-right
        img = _sweep_frame(parts, font_size, glow_rgb, center_frac, width, height, **kw)
        frames.append(ctk.CTkImage(light_image=img, dark_image=img, size=(width, height)))
    return frames


def logo_frames(n: int = 40, width: int = 216, height: int = 56) -> list[ctk.CTkImage]:
    """The 'SaveParty' logo with a neon light sweeping left-to-right across it."""
    key = ("logo", n, width, height)
    if key in _cache:
        return _cache[key]
    frames = _sweep_series(
        [("Save", (236, 236, 246)), ("Party", _rgb(0.62, 0.55, 1.0))],
        30, _rgb(0.60, 0.75, 1.0), n, width, height, band_frac=0.15,
    )
    _cache[key] = frames
    return frames


def credit_frames(
    text: str = "Created by Rusu", n: int = 34, width: int = 216, height: int = 36
) -> list[ctk.CTkImage]:
    """The author credit with a blue light sweeping left-to-right."""
    key = ("credit", text, n, width, height)
    if key in _cache:
        return _cache[key]
    frames = _sweep_series(
        [(text, (205, 228, 255))], 17, (60, 155, 255), n, width, height, band_frac=0.17
    )
    _cache[key] = frames
    return frames


def pro_icon(size: int = 256) -> Image.Image:
    """The application icon: a chunky gamepad silhouette (transparent
    background - no tile), purple-to-cyan gradient shell, dark face plate,
    white d-pad, two bright buttons, and a soft drop shadow. Reads clearly
    from 256 px down to 16 px."""
    scale = size / 256
    cx, cy = size // 2, int(size * 0.50)

    # Silhouette: wide rounded body + two lower grip lobes.
    silhouette = Image.new("L", (size, size), 0)
    sil_draw = ImageDraw.Draw(silhouette)
    bw, bh = int(212 * scale), int(118 * scale)
    sil_draw.rounded_rectangle(
        [cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2],
        radius=int(58 * scale), fill=255,
    )
    grip = int(54 * scale)
    for gx in (cx - bw // 2 - int(2 * scale), cx + bw // 2 + int(2 * scale) - 2 * grip):
        sil_draw.ellipse([gx, cy - int(22 * scale), gx + 2 * grip, cy - int(22 * scale) + 2 * grip], fill=255)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # Drop shadow first, offset downward.
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 160))
    shadow.paste(shadow_layer, (0, int(10 * scale)), silhouette)
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(int(10 * scale))))
    # Gradient shell clipped to the silhouette.
    shell = _gradient(size, size, 0.72, 0.52, v=0.92).convert("RGBA")
    img.paste(shell, (0, 0), silhouette)

    draw = ImageDraw.Draw(img)
    # Dark face plate inside the body (grips keep the gradient color).
    inset = int(16 * scale)
    draw.rounded_rectangle(
        [cx - bw // 2 + inset, cy - bh // 2 + inset, cx + bw // 2 - inset, cy + bh // 2 - int(4 * scale)],
        radius=int(44 * scale), fill=(14, 14, 21, 255),
    )
    # D-pad.
    dx, dy = cx - int(56 * scale), cy + int(2 * scale)
    arm, thick = int(31 * scale), int(11 * scale)
    draw.rounded_rectangle([dx - arm, dy - thick, dx + arm, dy + thick], radius=thick, fill=(238, 238, 248, 255))
    draw.rounded_rectangle([dx - thick, dy - arm, dx + thick, dy + arm], radius=thick, fill=(238, 238, 248, 255))
    # Action buttons.
    r = int(16 * scale)
    for bx, by, color in (
        (cx + int(40 * scale), cy - int(12 * scale), (124, 92, 255, 255)),
        (cx + int(70 * scale), cy + int(12 * scale), (47, 224, 141, 255)),
    ):
        draw.ellipse([bx - r, by - r, bx + r, by + r], fill=color)
        draw.ellipse(
            [bx - r, by - r, bx + r, by + r],
            outline=(255, 255, 255, 90), width=max(1, int(3 * scale)),
        )
    return img


def save_ico(path) -> None:
    """Write the multi-resolution .ico used by the built exe."""
    base = pro_icon(256)
    base.save(
        path, format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )


def app_icon() -> Image.Image:
    """Runtime window/taskbar icon - the professional icon, downscaled."""
    return pro_icon(256).resize((64, 64), Image.LANCZOS)
