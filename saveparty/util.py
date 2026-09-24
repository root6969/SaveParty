"""Small shared helpers: errors, time/size formatting, atomic JSON I/O, path moves."""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path


class SavePartyError(RuntimeError):
    """Base class for user-facing SaveParty errors."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def local_str(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def human_ago(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    seconds = (utc_now() - dt.astimezone(timezone.utc)).total_seconds()
    if seconds < 0:
        return "in the future (clock skew?)"
    return human_delta(seconds) + " ago"


def human_delta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024 or unit == "GiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GiB"


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, retries: int = 0, delay: float = 2.0) -> dict | None:
    """Read a JSON file, returning None if missing.

    Retries transient decode/OS errors: a file mid-upload or mid-download by a
    cloud sync client can be momentarily truncated or locked.
    """
    attempt = 0
    while True:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if attempt >= retries:
                raise
            attempt += 1
            time.sleep(delay)


def machine_name() -> str:
    return platform.node() or "unknown-pc"


def default_player_name() -> str:
    try:
        return getpass.getuser() or "player"
    except Exception:
        return "player"


def move_path(src: Path, dst: Path) -> None:
    """Rename src to dst; falls back to copy+delete across volumes."""
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _clear_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree_robust(path: Path) -> None:
    """Remove a tree, retrying briefly (antivirus/indexers hold handles on Windows)."""
    for attempt in range(3):
        try:
            shutil.rmtree(path, onerror=_clear_readonly)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1.0)


def copy2_retry(src: Path, dst: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            shutil.copy2(src, dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(1.0)


def slugify(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "game"
