"""Human-friendly names for save candidates, across every supported game.

Two shapes of save exist in the wild:

* file-based - each save is a named file (Valheim ``World.fwl``, Terraria
  ``World.wld``, Factorio ``Map.zip`` …). The filename IS the name the player
  typed, so we surface the names of the files a folder contains.
* folder-based - each save is a folder. The folder name is usually already
  readable (Minecraft worlds, 7 Days world names); we just tidy the cryptic
  cases (a bare SteamID, Stardew's ``Farm_123456`` suffix, Palworld hex IDs -
  the last of which the UI upgrades further, asynchronously, to the real world
  name read from LevelMeta.sav).

Everything here is cheap (directory listings + string work) so it runs on the
UI thread; the only slow, game-specific parse (Palworld) stays async in the UI.
"""

from __future__ import annotations

import re
from pathlib import Path

# For a folder that holds these files, each matching file is one named save.
SAVE_GLOBS: dict[str, list[str]] = {
    "valheim": ["*.fwl"],
    "terraria": ["*.wld"],
    "factorio": ["*.zip"],
    "rimworld": ["*.rws"],
    "vintage-story": ["*.vcdbs"],
    "astroneer": ["*.savegame"],
    "satisfactory": ["*.sav"],
    "core-keeper": ["*.json"],
}

# Filenames (case-insensitive substring) that are backups/autosaves, not a save
# the player would recognise by name.
_NOISE = ("autosave", "_backup", "backup_", "_auto_", "calculatorcache", "_tmp", "servermanager")

_STEAMID = re.compile(r"^\d{16,20}$")
_STARDEW = re.compile(r"^(.+?)_\d{6,}$")


def _clean_stem(name: str) -> str:
    return name.strip()


def folder_saves(game_id: str, folder: Path) -> list[str]:
    """Named saves contained in a folder (file stems for file-based games),
    newest first, de-duplicated. Empty if the game isn't file-based."""
    globs = SAVE_GLOBS.get(game_id)
    if not globs:
        return []
    seen: dict[str, float] = {}
    for pattern in globs:
        try:
            for f in folder.glob(pattern):
                if not f.is_file():
                    continue
                low = f.name.lower()
                if any(n in low for n in _NOISE):
                    continue
                stem = _clean_stem(f.stem)
                if stem:
                    seen[stem] = max(seen.get(stem, 0.0), f.stat().st_mtime)
        except OSError:
            continue
    return [name for name, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]


def pretty(game_id: str, raw: str) -> str:
    """Tidy a raw folder/file name into something readable."""
    if _STEAMID.match(raw):
        return "Steam account saves"
    if game_id == "stardew-valley":
        m = _STARDEW.match(raw)
        if m:
            return m.group(1)
    # Palworld hex world IDs stay as-is here; the UI overwrites them with the
    # real world name (async) once LevelMeta.sav is parsed.
    return raw


def describe(game_id: str | None, candidate) -> tuple[str, str]:
    """(label, subtitle) for one save-picker row. `candidate` is a
    detector.SaveCandidate (has .label, .path, .is_subfolder, .detail())."""
    label = pretty(game_id or "", candidate.label)
    detail = candidate.detail()
    # File-based game: show the names of the saves this folder directly holds
    # (root or a per-account subfolder - whichever actually contains the files).
    if game_id in SAVE_GLOBS:
        names = folder_saves(game_id, candidate.path)
        if names:
            shown = ", ".join(names[:5]) + (f"  +{len(names) - 5}" if len(names) > 5 else "")
            return label, f"{len(names)} save(s):  {shown}  ·  {detail}"
    return label, detail
