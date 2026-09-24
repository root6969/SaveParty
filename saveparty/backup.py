"""Zipped backups: creation, listing, pruning, and safe extraction.

Every operation that replaces save data zips the outgoing version first, named
`<label>_<timestamp>.zip`. Labels in AUTO_PRUNE_LABELS keep the newest
`keep` archives per label; conflict exports are kept forever.
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .snapshot import scan_dir
from .util import SavePartyError

AUTO_PRUNE_LABELS = {
    "local-before-pull",
    "cloud-before-push",
    "before-restore",
    "before-charswap",
}


@dataclass
class BackupInfo:
    name: str
    path: Path
    size: int
    mtime: datetime
    label: str


def zip_dir(src: Path, dest_zip: Path, exclude_dirs: set[str], exclude_globs: list[str]) -> Path:
    snap = scan_dir(src, exclude_dirs, exclude_globs)
    if not snap:
        raise SavePartyError(f"nothing to back up in {src}")
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_name(dest_zip.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in sorted(snap):
            zf.write(src / rel, rel)
    os.replace(tmp, dest_zip)
    return dest_zip


def make_backup(
    src: Path,
    backups_dir: Path,
    label: str,
    exclude_dirs: set[str],
    exclude_globs: list[str],
    keep: int,
) -> Path | None:
    """Zip `src` into backups_dir as <label>_<stamp>.zip; None if src is empty."""
    if not scan_dir(src, exclude_dirs, exclude_globs):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups_dir / f"{label}_{stamp}.zip"
    counter = 1
    while dest.exists():
        dest = backups_dir / f"{label}_{stamp}-{counter}.zip"
        counter += 1
    zip_dir(src, dest, exclude_dirs, exclude_globs)
    prune_backups(backups_dir, keep)
    return dest


def _label_of(name: str) -> str:
    return name.rsplit("_", 1)[0] if "_" in name else name


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    removed: list[Path] = []
    if keep <= 0 or not backups_dir.is_dir():
        return removed
    by_label: dict[str, list[Path]] = {}
    for zip_path in backups_dir.glob("*.zip"):
        label = _label_of(zip_path.stem)
        if label in AUTO_PRUNE_LABELS:
            by_label.setdefault(label, []).append(zip_path)
    for paths in by_label.values():
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in paths[keep:]:
            try:
                old.unlink()
                removed.append(old)
            except OSError:
                pass
    return removed


def list_backups(backups_dir: Path) -> list[BackupInfo]:
    out: list[BackupInfo] = []
    if not backups_dir.is_dir():
        return out
    for zip_path in backups_dir.glob("*.zip"):
        try:
            st = zip_path.stat()
        except OSError:
            continue
        out.append(
            BackupInfo(
                name=zip_path.name,
                path=zip_path,
                size=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime).astimezone(),
                label=_label_of(zip_path.stem),
            )
        )
    out.sort(key=lambda b: b.mtime, reverse=True)
    return out


def safe_extract(zip_path: Path, target: Path) -> int:
    """Extract a backup zip, refusing absolute or traversal member paths."""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SavePartyError(f"backup archive is corrupted at {bad}: {zip_path}")
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or (parts and parts[0].endswith(":")):
                raise SavePartyError(f"unsafe path in backup archive: {member.filename}")
        zf.extractall(target)
        return sum(1 for m in zf.infolist() if not m.is_dir())
