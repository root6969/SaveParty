"""Game profiles and per-machine storage locations.

profiles.json (in the SaveParty data folder) holds the player name and one
profile per synced game. Each profile carries everything the engine needs:
where the saves live, which cloud folder to sync through, how to detect and
launch the game, and safety knobs.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from .util import SavePartyError, default_player_name, read_json, slugify, write_json_atomic


def data_home() -> Path:
    override = os.environ.get("SAVEPARTY_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "SaveParty"
    return Path.home() / ".saveparty"


@dataclass
class Profile:
    id: str
    title: str
    game_id: str  # id from the games database, or "custom"
    save_dir: str
    cloud_dir: str
    process_names: list[str] = field(default_factory=list)
    launch: str = ""  # steam:// URI or executable path; empty = user launches manually
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=lambda: ["*.tmp"])
    backup_keep: int = 20
    settle_seconds: int = 10
    settle_max_wait: int = 180
    game_start_timeout: int = 600
    lock_stale_minutes: int = 30
    lock_heartbeat_seconds: int = 60
    lock_verify_seconds: float = 3.0
    cloud_wait_seconds: int = 300
    cloud_wait_interval: float = 10.0

    @classmethod
    def new(cls, title: str, game_id: str, save_dir: str, cloud_dir: str, **kw) -> "Profile":
        return cls(
            id=uuid.uuid4().hex[:8],
            title=title,
            game_id=game_id,
            save_dir=save_dir,
            cloud_dir=cloud_dir,
            **kw,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        missing = [k for k in ("id", "title", "save_dir", "cloud_dir") if not data.get(k)]
        if missing:
            raise SavePartyError(f"profile is missing required keys: {', '.join(missing)}")
        data.setdefault("game_id", "custom")
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    # -- derived paths --------------------------------------------------

    def slug(self) -> str:
        return f"{slugify(self.title)}-{self.id[:4]}"

    def backups_path(self) -> Path:
        return data_home() / "_backups" / self.slug()

    def staging_root(self) -> Path:
        return data_home() / "staging"

    def state_path(self) -> Path:
        key = "|".join(
            os.path.normcase(os.path.normpath(p)) for p in (self.cloud_dir, self.save_dir)
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return data_home() / "state" / f"{digest}.json"

    def exclude_dir_set(self) -> set[str]:
        return {d.lower() for d in self.exclude_dirs}

    def validate_paths(self) -> list[str]:
        problems = []
        if not Path(self.save_dir).is_dir():
            problems.append(f"save folder not found: {self.save_dir}")
        if not Path(self.cloud_dir).is_dir():
            problems.append(
                f"cloud folder not found: {self.cloud_dir} "
                "(is your cloud client running and the folder synced?)"
            )
        return problems


class ProfileStore:
    def __init__(self):
        self.path = data_home() / "profiles.json"
        self.player_name: str = default_player_name()
        self.profiles: list[Profile] = []
        self.auto_start_syncthing: bool = True  # start Syncthing on launch if it isn't running

    def load(self) -> "ProfileStore":
        try:
            data = read_json(self.path)
        except Exception as exc:
            raise SavePartyError(f"could not read {self.path}: {exc}") from exc
        if data:
            self.player_name = data.get("player_name") or self.player_name
            self.auto_start_syncthing = bool(data.get("auto_start_syncthing", True))
            self.profiles = [Profile.from_dict(p) for p in data.get("profiles", [])]
        return self

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "format": 1,
                "player_name": self.player_name,
                "auto_start_syncthing": self.auto_start_syncthing,
                "profiles": [p.to_dict() for p in self.profiles],
            },
        )

    def get(self, profile_id: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def add(self, profile: Profile) -> None:
        self.profiles.append(profile)
        self.save()

    def remove(self, profile_id: str) -> None:
        self.profiles = [p for p in self.profiles if p.id != profile_id]
        self.save()
