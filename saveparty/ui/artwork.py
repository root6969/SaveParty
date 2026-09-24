"""Official game artwork from Steam's public CDN, by App ID.

Downloads each game's store banner (header.jpg) and library hero
(library_hero.jpg) once, caches them under the SaveParty data folder, and
serves them as CTkImages cropped to fit. Everything degrades gracefully:
no App ID, no internet, or a failed download just returns None, and the UI
falls back to the generated gradient art. This is the same public art that
launchers like Playnite use.
"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from ..games_db import get_game
from ..profiles import data_home
from .art import _font  # reuse the font loader

_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{name}"
_ASSETS = ("header.jpg", "library_hero.jpg")
_mem: dict = {}
_lock = threading.Lock()


def _cache_dir() -> Path:
    d = data_home() / "artwork"
    d.mkdir(parents=True, exist_ok=True)
    return d


def steam_appid(game_id: str | None) -> int | None:
    game = get_game(game_id) if game_id else None
    return game.steam_appid if game else None


def _local(appid: int, name: str) -> Path:
    return _cache_dir() / f"{appid}_{name}"


def _download(appid: int, name: str) -> Path | None:
    dest = _local(appid, name)
    if dest.exists() and dest.stat().st_size > 500:
        return dest
    try:
        req = urllib.request.Request(
            _CDN.format(appid=appid, name=name), headers={"User-Agent": "SaveParty/1.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        if data and len(data) > 500:
            dest.write_bytes(data)
            return dest
    except Exception:
        pass
    return None


def prefetch(appids) -> None:
    """Download art for these App IDs - call on a background thread."""
    for appid in appids:
        if appid:
            for name in _ASSETS:
                _download(appid, name)


def _pil(appid: int, name: str) -> Image.Image | None:
    path = _local(appid, name)
    if not path.exists():
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _local_icon_file(appid: int) -> Path | None:
    """The real square game icon from Steam's local library cache, if installed.

    Steam stores per-game art under appcache/librarycache/<appid>/: the store
    banner/hero live in hash-named subfolders, but the square icon sits loose
    in the folder as a small .jpg. That's the exact icon Steam shows.
    """
    try:
        from ..detector import _steam_install_dir

        root = _steam_install_dir()
    except Exception:
        root = None
    if not root:
        return None
    folder = root / "appcache" / "librarycache" / str(appid)
    if not folder.is_dir():
        return None
    jpgs = [f for f in folder.glob("*.jpg") if f.is_file()]  # glob is non-recursive → loose files only
    return min(jpgs, key=lambda f: f.stat().st_size) if jpgs else None


def _fade_bottom(img: Image.Image, fade_h: int, color: tuple) -> Image.Image:
    """Fade the bottom `fade_h` px of the image into `color`, so a control bar
    overlaid there stays readable (Steam-style hero gradient)."""
    img = img.convert("RGB")
    w, h = img.size
    fade_h = min(max(fade_h, 0), h)
    if fade_h == 0:
        return img
    solid = Image.new("RGB", (w, h), color)
    col = Image.new("L", (1, h), 0)
    cp = col.load()
    top = h - fade_h
    for y in range(h):
        if y < top:
            cp[0, y] = 0
            continue
        rel = (y - top) / max(fade_h - 1, 1)
        # Short gradient at the top of the band, then fully solid - so a control
        # bar placed anywhere in the band sits on solid colour (no dark boxes).
        cp[0, y] = 255 if rel >= 0.34 else int(rel / 0.34 * 255)
    return Image.composite(solid, img, col.resize((w, h)))


def _cover(img: Image.Image, w: int, h: int, darken: float = 0.0) -> Image.Image:
    """Scale-to-fill + center-crop to exactly w×h, optionally darkened."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
    iw, ih = img.size
    left, top = (iw - w) // 2, (ih - h) // 2
    img = img.crop((left, top, left + w, top + h))
    if darken > 0:
        img = ImageEnhance.Brightness(img).enhance(1.0 - darken)
    return img


def banner(
    appid: int | None, w: int, h: int, prefer_hero: bool = False, darken: float = 0.0,
    fade_h: int = 0, fade_color: tuple = (23, 23, 29),
):
    """Real game banner cropped to w×h, or None if unavailable. `fade_h` fades
    the bottom into `fade_color` for a control bar overlaid there."""
    if not appid:
        return None
    key = (appid, "banner", w, h, prefer_hero, darken, fade_h, fade_color)
    with _lock:
        if key in _mem:
            return _mem[key]
    order = ("library_hero.jpg", "header.jpg") if prefer_hero else ("header.jpg", "library_hero.jpg")
    img = next((p for p in (_pil(appid, n) for n in order) if p), None)
    result = None
    if img:
        cov = _cover(img, w, h, darken)
        if fade_h > 0:
            cov = _fade_bottom(cov, fade_h, fade_color)
        result = ctk.CTkImage(light_image=cov, dark_image=cov, size=(w, h))
    with _lock:
        _mem[key] = result
    return result


def _hero_base(appid: int | None, w: int, h: int, band_h: int) -> Image.Image:
    """Hero art with a blurred, darkened bottom band (Steam-style semi-transparent
    bar you can still see the art through). Cached - the blur is the costly part."""
    key = ("herobase", appid, w, h, band_h)
    with _lock:
        cached = _mem.get(key)
    if cached is not None:
        return cached.copy()
    img = None
    for name in ("library_hero.jpg", "header.jpg"):
        img = _pil(appid, name) if appid else None
        if img:
            break
    base = _cover(img, w, h).convert("RGB") if img else Image.new("RGB", (w, h), (32, 28, 44))
    band_top = max(0, h - band_h)
    region = base.crop((0, band_top, w, h))
    dark = ImageEnhance.Brightness(region.filter(ImageFilter.GaussianBlur(7))).enhance(0.34)
    grad = Image.new("L", (1, h - band_top), 255)
    gp = grad.load()
    for y in range(h - band_top):
        gp[0, y] = int(min(1.0, y / 46.0) * 255)  # blend in at the band's top edge
    base.paste(Image.composite(dark, region, grad.resize((w, h - band_top))), (0, band_top))
    with _lock:
        _mem[key] = base
    return base.copy()


def _shadow_text(draw, xy, text, font, fill):
    """Draw text with a soft dark shadow so it stays readable over any art
    (Steam does this - white text works on both bright and dark backgrounds)."""
    x, y = xy
    draw.text((x + 1, y + 2), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


def hero_bar(appid, w, h, band_h, title, status, status_rgb, badges, stats,
             scaling=1.0, left_reserve=300):
    """Compose the hero + a semi-transparent band with the title, status,
    badges and stats drawn INTO it (so they blend with no widget-box artifacts).
    Interactive buttons are overlaid by the caller on top.

    Everything is rendered at PHYSICAL resolution (w, h are physical px). The
    returned CTkImage is sized w/scaling × h/scaling so CustomTkinter displays
    it 1:1 with no blur, and `left_reserve` (physical px) keeps the baked stats
    clear of the overlaid PLAY + dropdown buttons.
    """
    s = scaling
    base = _hero_base(appid, w, h, band_h)
    draw = ImageDraw.Draw(base)
    band_top = h - band_h
    # Upper row of the band: title + status (left), badges (right).
    ty = band_top + int(18 * s)
    _shadow_text(draw, (int(24 * s), ty), title, _font(int(28 * s)), (245, 245, 250))
    _shadow_text(draw, (int(24 * s), ty + int(38 * s)), "●  " + (status or "")[:72],
                 _font(int(13 * s)), status_rgb)
    bfont = _font(int(12 * s))
    bx = w - int(24 * s)
    for text, rgb in reversed(badges):
        chip_w = int(draw.textlength(text, font=bfont) + 26 * s)
        draw.rounded_rectangle([bx - chip_w, ty + int(2 * s), bx, ty + int(30 * s)],
                               radius=int(11 * s), fill=rgb)
        draw.text((bx - chip_w + int(13 * s), ty + int(8 * s)), text, font=bfont, fill=(14, 14, 18))
        bx -= chip_w + int(8 * s)
    # Lower row: PLAY button is overlaid at the far left by the caller; the stat
    # blocks are baked to the right of the space it reserves.
    vfont, lfont = _font(int(19 * s)), _font(int(10 * s))
    sx, sy = left_reserve, h - int(56 * s)
    for value, label in stats:
        _shadow_text(draw, (sx, sy), value, vfont, (245, 245, 250))
        _shadow_text(draw, (sx, sy + int(27 * s)), label, lfont, (180, 180, 195))
        sx += int(max(draw.textlength(value, font=vfont), draw.textlength(label, font=lfont)) + 42 * s)
    size = (max(1, round(w / s)), max(1, round(h / s)))
    return ctk.CTkImage(light_image=base, dark_image=base, size=size)


def hero_aspect(appid: int | None) -> float | None:
    """height/width of the hero art, so the UI can show it uncropped."""
    if not appid:
        return None
    img = _pil(appid, "library_hero.jpg") or _pil(appid, "header.jpg")
    if img:
        w, h = img.size
        return h / w
    return None


def icon(appid: int | None, size: int):
    """The real square Steam game icon (from the local library cache), rounded.
    Falls back to a crop of the store banner if the icon isn't available."""
    if not appid:
        return None
    key = (appid, "icon", size)
    with _lock:
        if key in _mem:
            return _mem[key]
    img = None
    icon_file = _local_icon_file(appid)
    if icon_file:
        try:
            img = Image.open(icon_file).convert("RGB")
        except Exception:
            img = None
    if img is None:
        img = _pil(appid, "header.jpg")  # fallback: crop of the banner
    result = None
    if img:
        sq = _cover(img, size, size).convert("RGBA")
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 4, fill=255)
        sq.putalpha(mask)
        result = ctk.CTkImage(light_image=sq, dark_image=sq, size=(size, size))
    with _lock:
        _mem[key] = result
    return result
