"""Game process detection/launch and save-directory change monitoring."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import psutil

from .snapshot import scan_dir, snapshots_differ

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    HAVE_WATCHDOG = True
except ImportError:  # pragma: no cover - declared dependency
    HAVE_WATCHDOG = False


def matching_processes(names: list[str]) -> list[psutil.Process]:
    wanted = {n.lower() for n in names if n}
    if not wanted:
        return []
    procs = []
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if (proc.info.get("name") or "").lower() in wanted:
                procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_game_running(names: list[str]) -> bool:
    return bool(matching_processes(names))


def launch_target(target: str) -> None:
    """Open a steam:// URI or start an executable."""
    if not target:
        return
    if target.lower().startswith(("steam://", "http://", "https://")):
        os.startfile(target)  # noqa: S606 - protocol URL
    elif target.lower().endswith(".exe"):
        subprocess.Popen([target], cwd=str(Path(target).parent))
    else:
        os.startfile(target)  # noqa: S606


def list_running_processes(limit: int = 60) -> list[tuple[str, float]]:
    """(name, started) for running processes, newest first - for 'detect my game'."""
    seen: dict[str, float] = {}
    for proc in psutil.process_iter(attrs=["name", "create_time"]):
        try:
            name = proc.info.get("name") or ""
            if not name.lower().endswith(".exe"):
                continue
            started = proc.info.get("create_time") or 0
            if name not in seen or started > seen[name]:
                seen[name] = started
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    out = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    return out[:limit]


if HAVE_WATCHDOG:

    class _Handler(FileSystemEventHandler):
        def __init__(self, monitor: "ChangeMonitor"):
            self.monitor = monitor

        def on_any_event(self, event):
            if event.is_directory:
                return
            parts = {p.lower() for p in Path(str(event.src_path)).parts}
            if parts & self.monitor.exclude_dirs:
                return
            self.monitor._record()


class ChangeMonitor:
    """Tracks the time of the last write inside the save directory.

    Prefers a watchdog observer; falls back to a polling thread that diffs
    snapshots. Used for live status - sync decisions always re-scan the
    directory, so a missed event can never lose data.
    """

    def __init__(self, root: Path, exclude_dirs: set[str], exclude_globs: list[str], poll_seconds: float = 5.0):
        self.root = root
        self.exclude_dirs = exclude_dirs
        self.exclude_globs = exclude_globs
        self.poll_seconds = max(poll_seconds, 1.0)
        self.backend = "off"
        self._last: float | None = None
        self._data_lock = threading.Lock()
        self._observer = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _record(self) -> None:
        with self._data_lock:
            self._last = time.time()

    @property
    def last_change(self) -> float | None:
        with self._data_lock:
            return self._last

    def start(self) -> None:
        if HAVE_WATCHDOG:
            try:
                self._observer = Observer()
                self._observer.schedule(_Handler(self), str(self.root), recursive=True)
                self._observer.daemon = True
                self._observer.start()
                self.backend = "watchdog"
                return
            except Exception:
                self._observer = None
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.backend = "polling"

    def _poll_loop(self) -> None:
        prev = scan_dir(self.root, self.exclude_dirs, self.exclude_globs)
        while not self._stop.wait(self.poll_seconds):
            cur = scan_dir(self.root, self.exclude_dirs, self.exclude_globs)
            if snapshots_differ(prev, cur):
                self._record()
                prev = cur

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass
            self._observer = None
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=self.poll_seconds + 2)
            self._thread = None
        self.backend = "off"
