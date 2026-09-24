"""File inventory scanning, hashing, and comparison.

A snapshot maps relative POSIX paths to {"size", "mtime_ns"} (plus "sha256"
once hashed). Local change detection compares a fresh scan against the
snapshot recorded at the last sync; cloud content comparison always uses
sizes and hashes, never cross-machine timestamps.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Callable

Snapshot = dict[str, dict]

_CHUNK = 1024 * 1024


def scan_dir(root: Path, exclude_dirs: set[str], exclude_globs: list[str]) -> Snapshot:
    snap: Snapshot = {}
    if not root.is_dir():
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in exclude_dirs]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in exclude_globs):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue  # vanished mid-scan
            rel = full.relative_to(root).as_posix()
            snap[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return snap


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def add_hashes(
    root: Path, snap: Snapshot, progress: Callable[[int], None] | None = None
) -> Snapshot:
    for rel, meta in snap.items():
        meta["sha256"] = hash_file(root / rel)
        if progress:
            progress(meta["size"])
    return snap


def total_size(snap: Snapshot) -> int:
    return sum(meta["size"] for meta in snap.values())


def snapshots_differ(a: Snapshot, b: Snapshot) -> bool:
    if a.keys() != b.keys():
        return True
    return any(
        a[k]["size"] != b[k]["size"] or a[k]["mtime_ns"] != b[k]["mtime_ns"] for k in a
    )


def diff(old: Snapshot, new: Snapshot) -> tuple[list[str], list[str], list[str]]:
    """Return (added, removed, changed) relative paths, sorted."""
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    changed = sorted(
        k
        for k in old.keys() & new.keys()
        if old[k]["size"] != new[k]["size"] or old[k]["mtime_ns"] != new[k]["mtime_ns"]
    )
    return added, removed, changed
