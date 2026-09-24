"""Advisory lock file in the shared cloud folder.

Best-effort mutual exclusion: cloud clients propagate files with seconds-to-
minutes of delay, so two players starting at the exact same moment can still
race. Acquisition writes a unique token, waits `verify_seconds`, and re-reads
to confirm the token survived (last-writer-wins detection). Heartbeats keep
the lock fresh while playing; a lock without recent heartbeats is stale.
"""

from __future__ import annotations

import enum
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .util import iso, machine_name, parse_iso, utc_now

LOCK_FILENAME = "saveparty.lock"


class LockStatus(enum.Enum):
    ACQUIRED = "acquired"
    HELD = "held"
    LOST = "lost"


@dataclass
class LockInfo:
    player: str
    machine: str
    pid: int
    token: str
    acquired_at: datetime | None
    heartbeat_at: datetime | None
    corrupt: bool = False


class CloudLock:
    def __init__(self, cloud_dir: Path, player: str, stale_minutes: int = 30, verify_seconds: float = 3.0):
        self.path = Path(cloud_dir) / LOCK_FILENAME
        self.player = player
        self.machine = machine_name()
        self.stale_minutes = stale_minutes
        self.verify_seconds = verify_seconds
        self.token: str | None = None

    def read(self) -> LockInfo | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return LockInfo(
                player=str(data.get("player", "unknown")),
                machine=str(data.get("machine", "unknown")),
                pid=int(data.get("pid", 0)),
                token=str(data.get("token", "")),
                acquired_at=parse_iso(data.get("acquired_at")),
                heartbeat_at=parse_iso(data.get("heartbeat_at")),
            )
        except (OSError, ValueError):
            return LockInfo("(unreadable lock)", "unknown", 0, "", None, None, corrupt=True)

    def is_mine_token(self, info: LockInfo | None) -> bool:
        return bool(info and self.token and info.token == self.token)

    def is_my_identity(self, info: LockInfo) -> bool:
        return info.player == self.player and info.machine == self.machine

    def heartbeat_age(self, info: LockInfo) -> float | None:
        stamp = info.heartbeat_at or info.acquired_at
        if stamp is None:
            try:
                return time.time() - self.path.stat().st_mtime
            except OSError:
                return None
        return (utc_now() - stamp).total_seconds()

    def is_stale(self, info: LockInfo) -> bool:
        age = self.heartbeat_age(info)
        return age is None or age > self.stale_minutes * 60

    def _payload(self, token: str, acquired_at: str | None = None) -> dict:
        now = iso(utc_now())
        return {
            "format": 1,
            "app": "saveparty",
            "player": self.player,
            "machine": self.machine,
            "pid": os.getpid(),
            "token": token,
            "acquired_at": acquired_at or now,
            "heartbeat_at": now,
        }

    def acquire(self, takeover: bool = False) -> tuple[LockStatus, LockInfo | None]:
        existing = self.read()
        if existing and not takeover and not self.is_my_identity(existing):
            return LockStatus.HELD, existing
        token = uuid.uuid4().hex
        payload = self._payload(token)
        if existing is None:
            try:
                with open(self.path, "x", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
            except FileExistsError:
                return LockStatus.HELD, self.read()
        else:
            tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        if self.verify_seconds > 0:
            time.sleep(self.verify_seconds)
        current = self.read()
        if current is None or current.token != token:
            return LockStatus.LOST, current
        self.token = token
        return LockStatus.ACQUIRED, current

    def heartbeat(self) -> bool:
        current = self.read()
        if current is None or not self.is_mine_token(current):
            return False
        payload = self._payload(
            self.token, acquired_at=iso(current.acquired_at) if current.acquired_at else None
        )
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return True

    def release(self) -> str:
        current = self.read()
        if current is None:
            self.token = None
            return "already-free"
        if not self.is_mine_token(current):
            return "not-owner"
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.token = None
        return "released"

    def force_release(self) -> None:
        self.path.unlink(missing_ok=True)
        self.token = None
