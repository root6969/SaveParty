"""Save-location detection, installed-game discovery, and the compatibility check."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .games_db import GAMES, GameDef
from .snapshot import scan_dir, total_size
from .util import human_size


def _template_vars() -> dict[str, str]:
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    roaming = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    return {
        "LOCALAPPDATA": local,
        "APPDATA": roaming,
        "LOCALLOW": str(Path(local).parent / "LocalLow"),
        "DOCUMENTS": str(home / "Documents"),
        "SAVEDGAMES": str(home / "Saved Games"),
        "USERPROFILE": str(home),
    }


def expand_template(template: str) -> list[Path]:
    """Expand placeholders and `*` segments; return only directories that exist."""
    text = template
    for key, value in _template_vars().items():
        text = text.replace("{" + key + "}", value)
    if "*" not in text:
        path = Path(text)
        return [path] if path.is_dir() else []
    head, _, tail = text.partition("*")
    base = Path(head.rstrip("\\/"))
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        candidate = Path(str(child) + tail) if tail else child
        if candidate.is_dir():
            out.append(candidate)
    return out


def detect_paths(game: GameDef) -> list[Path]:
    found: list[Path] = []
    for template in game.save_paths:
        for path in expand_template(template):
            if path not in found:
                found.append(path)
    return found


# ----------------------------------------------------------------------
# installed games & save candidates
# ----------------------------------------------------------------------


def _steam_install_dir() -> Path | None:
    """Steam's install location from the registry (works for any drive)."""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _type = winreg.QueryValueEx(key, "SteamPath")
        path = Path(str(value))
        return path if (path / "steamapps").is_dir() else None
    except OSError:
        return None


def steam_library_paths() -> list[Path]:
    """Steam install + extra library folders from libraryfolders.vdf."""
    bases: list[Path] = []
    registry_dir = _steam_install_dir()
    if registry_dir:
        bases.append(registry_dir)
    for candidate in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam",
    ):
        if (candidate / "steamapps").is_dir() and candidate not in bases:
            bases.append(candidate)
    out: list[Path] = []
    for base in bases:
        if base not in out:
            out.append(base)
        vdf = base / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                lib = Path(match.group(1).replace("\\\\", "\\"))
                if (lib / "steamapps").is_dir() and lib not in out:
                    out.append(lib)
    return out


def installed_steam_appids() -> set[int]:
    ids: set[int] = set()
    for lib in steam_library_paths():
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                ids.add(int(manifest.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
    return ids


@dataclass
class GameDetection:
    game: GameDef
    installed: bool  # Steam reports the game installed
    save_paths: list[Path] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.installed or bool(self.save_paths)


def detect_games() -> list[GameDetection]:
    """Every supported game, flagged with what this PC actually has.

    Cheap checks only (folder existence + Steam manifests) so the Add-game
    page can run it synchronously.
    """
    appids = installed_steam_appids()
    out = []
    for game in GAMES:
        out.append(
            GameDetection(
                game=game,
                installed=bool(game.steam_appid and game.steam_appid in appids),
                save_paths=detect_paths(game),
            )
        )
    out.sort(key=lambda d: (not d.present, d.game.title.lower()))
    return out


@dataclass
class SaveCandidate:
    path: Path
    label: str
    files: int
    size: int
    last_saved: datetime | None
    is_subfolder: bool = False

    def detail(self) -> str:
        when = "never" if self.last_saved is None else _ago(self.last_saved)
        return f"last saved {when}  ·  {self.files} files  ·  {human_size(self.size)}"


def _ago(dt: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def describe_dir(path: Path, exclude_dirs: set[str], exclude_globs: list[str]) -> tuple[int, int, datetime | None]:
    snap = scan_dir(path, exclude_dirs, exclude_globs)
    if not snap:
        return 0, 0, None
    newest = max(meta["mtime_ns"] for meta in snap.values()) / 1e9
    return len(snap), total_size(snap), datetime.fromtimestamp(newest, tz=timezone.utc)


def save_candidates(game: GameDef | None, roots: list[Path] | None = None) -> list[SaveCandidate]:
    """Selectable saves: each detected root plus its save-looking subfolders
    (worlds / slots), with name, last-saved time, and size."""
    exclude = {d.lower() for d in (game.exclude_dirs if game else [])}
    globs = game.exclude_globs if game else ["*.tmp"]
    out: list[SaveCandidate] = []
    for root in roots if roots is not None else (detect_paths(game) if game else []):
        files, size, last = describe_dir(root, exclude, globs)
        out.append(SaveCandidate(root, root.name, files, size, last))
        subs = []
        try:
            for sub in root.iterdir():
                if not sub.is_dir() or sub.name.lower() in exclude:
                    continue
                s_files, s_size, s_last = describe_dir(sub, exclude, globs)
                if s_files:
                    subs.append(SaveCandidate(sub, sub.name, s_files, s_size, s_last, is_subfolder=True))
        except OSError:
            pass
        subs.sort(
            key=lambda c: c.last_saved or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True
        )
        out.extend(subs[:10])
    return out


# ----------------------------------------------------------------------
# compatibility check
# ----------------------------------------------------------------------


@dataclass
class CheckItem:
    level: str  # ok / warn / fail
    title: str
    detail: str


def compatibility_check(save_dir: Path | None, game: GameDef | None) -> tuple[str, list[CheckItem]]:
    """Return (verdict, items). Verdict: 'ok', 'warn', or 'fail'."""
    items: list[CheckItem] = []

    if save_dir is None or not Path(save_dir).is_dir():
        items.append(
            CheckItem(
                "fail",
                "Save folder not found",
                "SaveParty could not locate this game's saves on this PC. "
                "Install/run the game once, or browse to the folder manually.",
            )
        )
        return "fail", items
    save_dir = Path(save_dir)
    items.append(CheckItem("ok", "Save folder found", str(save_dir)))

    exclude = set(d.lower() for d in (game.exclude_dirs if game else []))
    globs = game.exclude_globs if game else ["*.tmp"]
    snap = scan_dir(save_dir, exclude, globs)
    if not snap:
        items.append(
            CheckItem(
                "warn",
                "Folder is empty",
                "No save files yet - play the game once, or a friend will seed the shared save.",
            )
        )
    else:
        size = total_size(snap)
        if size > 2 * 1024**3:
            items.append(
                CheckItem(
                    "warn",
                    f"Large saves ({human_size(size)})",
                    "Syncing works but pushes/pulls will be slow on this cloud folder.",
                )
            )
        else:
            items.append(
                CheckItem("ok", f"{len(snap)} save file(s), {human_size(size)}", "Size looks fine.")
            )

    if game and game.require_file and not (save_dir / game.require_file).is_file():
        items.append(
            CheckItem(
                "fail",
                f"Wrong folder level - {game.require_file} not found here",
                f"Pick the folder that directly contains {game.require_file} "
                "(a single world/save, not the parent folder).",
            )
        )

    lowered = str(save_dir).lower()
    if os.sep + "steam" + os.sep + "userdata" + os.sep in lowered:
        items.append(
            CheckItem(
                "warn",
                "Folder is managed by Steam Cloud",
                "This location is inside Steam's userdata store. Steam Cloud will fight "
                "external sync - disable cloud saves for this game on every friend's PC.",
            )
        )

    cloud = game.steam_cloud if game else "unknown"
    if cloud == "on":
        items.append(
            CheckItem(
                "warn",
                "Game uses Steam Cloud by default",
                "Every friend must disable Steam Cloud for this game "
                "(Library > right-click > Properties > uncheck cloud saves), or the two "
                "sync systems will overwrite each other.",
            )
        )
    elif cloud == "optional":
        items.append(
            CheckItem(
                "warn",
                "Game has optional cloud saves",
                "Keep the shared saves stored locally inside the game's own menus.",
            )
        )
    elif cloud == "off":
        items.append(CheckItem("ok", "No Steam Cloud conflict", "The game does not cloud-sync these files."))
    else:
        items.append(
            CheckItem(
                "warn",
                "Steam Cloud status unknown",
                "If this game syncs saves via Steam Cloud, disable that on every friend's PC.",
            )
        )

    if game and game.process_names:
        items.append(
            CheckItem(
                "ok",
                "Automatic session detection",
                f"SaveParty watches {game.process_names[0]} and syncs when the game closes.",
            )
        )
    else:
        items.append(
            CheckItem(
                "warn",
                "No process detection",
                "SaveParty cannot tell when this game closes - click 'End session' when "
                "you stop playing (or set a process name in the profile settings).",
            )
        )

    if game and game.notes:
        items.append(CheckItem("warn" if "disable" in game.notes.lower() else "ok", "Game notes", game.notes))

    items.append(
        CheckItem(
            "ok",
            "Characters & co-op",
            (game.coop_notes if game and game.coop_notes else
             "No known character quirks - the shared save carries the world. "
             "Backups are made before every change either way."),
        )
    )

    if any(i.level == "fail" for i in items):
        return "fail", items
    if any(i.level == "warn" for i in items):
        return "warn", items
    return "ok", items


def cloud_folder_candidates() -> list[tuple[str, Path]]:
    """Cloud-synced base folders detected on this machine, as (provider, path)."""
    import ctypes
    import string

    out: list[tuple[str, Path]] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            try:
                if not root.exists():
                    continue
            except OSError:
                continue
            label = ctypes.create_unicode_buffer(261)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(str(root)), label, 261, None, None, None, None, 0
            )
            if (ok and label.value == "Google Drive") or (root / ".shortcut-targets-by-id").is_dir():
                try:
                    for child in sorted(root.iterdir()):
                        if child.is_dir() and not child.name.startswith((".", "$")):
                            try:
                                import stat as stat_mod

                                if child.stat().st_file_attributes & stat_mod.FILE_ATTRIBUTE_HIDDEN:
                                    continue
                            except (OSError, AttributeError):
                                pass
                            out.append(("Google Drive", child))
                except OSError:
                    continue
    mirror = Path.home() / "My Drive"
    if mirror.is_dir():
        out.append(("Google Drive", mirror))
    dropbox = Path.home() / "Dropbox"
    if dropbox.is_dir():
        out.append(("Dropbox", dropbox))
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive and Path(onedrive).is_dir():
        out.append(("OneDrive", Path(onedrive)))
    return out
