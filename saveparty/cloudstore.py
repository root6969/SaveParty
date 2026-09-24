"""The shared cloud folder: manifest, integrity verification, push/pull transfers.

Cloud layout:

    <cloud_dir>/
        saveparty.lock        advisory lock (see lock.py)
        manifest.json         who pushed what, generation counter, file hashes
        SAVEPARTY_README.txt  note for humans browsing the folder
        saves/                mirror of the synced save files

The manifest is written LAST during a push and lists a SHA-256 per file, so a
partially-propagated cloud folder (provider still uploading/downloading) is
detectable: SaveParty waits instead of syncing half a save. Generations are a
monotonic push counter - "who has the latest" is decided by generation + push
token, never by comparing clocks between machines.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Callable

from . import __version__
from .profiles import Profile
from .snapshot import Snapshot, hash_file, total_size
from .util import (
    SavePartyError,
    copy2_retry,
    iso,
    machine_name,
    read_json,
    utc_now,
    write_json_atomic,
)

SAVES_DIRNAME = "saves"
MANIFEST_NAME = "manifest.json"
CLOUD_README_NAME = "SAVEPARTY_README.txt"
MANIFEST_FORMAT = 1

CLOUD_README_TEXT = """This folder is managed by SaveParty (co-op save sync).

- saves/         the shared save files
- manifest.json  sync metadata (who pushed last, file checksums)
- saveparty.lock present while someone is playing

Please do not add, edit, or delete files here by hand - use the SaveParty app.
Share this folder with your co-op friends via your cloud provider.
"""


class CloudError(SavePartyError):
    pass


def new_token() -> str:
    return uuid.uuid4().hex


class CloudStore:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.root = Path(profile.cloud_dir)
        self.saves_root = self.root / SAVES_DIRNAME
        self.manifest_path = self.root / MANIFEST_NAME

    def read_manifest(self, retries: int = 0) -> dict | None:
        try:
            data = read_json(
                self.manifest_path, retries=retries, delay=self.profile.cloud_wait_interval
            )
        except Exception as exc:
            raise CloudError(
                f"cloud manifest is unreadable ({exc}). Your cloud client may still be "
                "syncing - wait a minute and retry."
            ) from exc
        if data is None:
            return None
        if data.get("format", 0) > MANIFEST_FORMAT:
            raise CloudError("cloud manifest was written by a newer SaveParty - update this PC.")
        if not isinstance(data.get("files"), dict) or "generation" not in data:
            raise CloudError(f"cloud manifest is malformed: {self.manifest_path}")
        return data

    def pending_higher_generation(self) -> int | None:
        """Fork safety net that needs NO Syncthing API: if a
        ``manifest.sync-conflict-*.json`` exists with a generation HIGHER than
        the live manifest, a newer save arrived and the sync tool demoted it to
        a conflict copy (someone played on a stale world, or two people pushed
        at once). Returns that higher generation so the caller can refuse to
        play/push over it; None when the live manifest is the newest present."""
        try:
            live = self.read_manifest()
        except CloudError:
            return None
        live_gen = int(live.get("generation", 0)) if live else 0
        highest = live_gen
        try:
            for cf in self.root.glob("manifest.sync-conflict-*.json"):
                try:
                    data = read_json(cf)
                    highest = max(highest, int((data or {}).get("generation", 0)))
                except Exception:
                    continue
        except Exception:
            return None
        return highest if highest > live_gen else None

    def verify(self, manifest: dict) -> list[str]:
        problems: list[str] = []
        for rel, meta in manifest["files"].items():
            path = self.saves_root / rel
            try:
                st = path.stat()
            except OSError:
                problems.append(f"missing: {rel}")
                continue
            if st.st_size != meta["size"]:
                problems.append(f"size mismatch: {rel}")
                continue
            expected = meta.get("sha256")
            if expected:
                try:
                    if hash_file(path) != expected:
                        problems.append(f"checksum mismatch: {rel}")
                except OSError as exc:
                    problems.append(f"unreadable: {rel} ({exc})")
        return problems

    def wait_until_verified(self, status: Callable[[str], None] | None = None) -> dict:
        """Re-read + verify the manifest until the cloud folder is consistent."""
        deadline = time.monotonic() + self.profile.cloud_wait_seconds
        attempt = 0
        while True:
            manifest = self.read_manifest(retries=2)
            if manifest is None:
                raise CloudError("cloud manifest disappeared while waiting for sync")
            problems = self.verify(manifest)
            if not problems:
                return manifest
            attempt += 1
            if time.monotonic() > deadline:
                sample = "; ".join(problems[:3])
                raise CloudError(
                    f"cloud folder is still incomplete after {self.profile.cloud_wait_seconds}s "
                    f"({len(problems)} file(s) not ready: {sample}). Make sure your cloud "
                    "client is running and fully synced; if the last player's upload was "
                    "interrupted, ask them to sync again."
                )
            if status:
                status(f"waiting for the cloud provider ({len(problems)} file(s) not ready, attempt {attempt})")
            time.sleep(self.profile.cloud_wait_interval)

    def pull_into(self, staging: Path, manifest: dict) -> int:
        """Copy manifest files cloud -> staging, verifying each file's hash."""
        copied = 0
        for rel, meta in manifest["files"].items():
            src = self.saves_root / rel
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2_retry(src, dst)
            if meta.get("sha256") and hash_file(dst) != meta["sha256"]:
                raise CloudError(
                    f"file changed while pulling: {rel}. The cloud provider is likely "
                    "still syncing - try again in a minute."
                )
            copied += meta["size"]
        return copied

    def plan_push(self, hashed_snap: Snapshot, prev_manifest: dict | None) -> tuple[list[str], list[str]]:
        prev_files: dict = prev_manifest["files"] if prev_manifest else {}
        to_copy = [
            rel
            for rel, meta in hashed_snap.items()
            if rel not in prev_files
            or prev_files[rel].get("sha256") != meta["sha256"]
            or prev_files[rel]["size"] != meta["size"]
        ]
        to_delete = [rel for rel in prev_files if rel not in hashed_snap]
        return sorted(to_copy), sorted(to_delete)

    def push_from(
        self, local_root: Path, hashed_snap: Snapshot, prev_manifest: dict | None, player: str
    ) -> dict:
        if not hashed_snap:
            raise CloudError("refusing to push an empty save folder to the cloud")
        to_copy, to_delete = self.plan_push(hashed_snap, prev_manifest)
        self.saves_root.mkdir(parents=True, exist_ok=True)
        for rel in to_copy:
            dst = self.saves_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2_retry(local_root / rel, dst)
        # Self-heal: a cloud provider can silently drop files it already synced.
        # Before publishing the manifest, make sure EVERY file it will list is
        # physically present (right size), re-copying any the provider lost -
        # otherwise the manifest would reference missing files and every puller
        # would hang on "N files not ready" forever.
        for rel, meta in hashed_snap.items():
            dst = self.saves_root / rel
            try:
                if dst.stat().st_size == meta["size"]:
                    continue
            except OSError:
                pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2_retry(local_root / rel, dst)
        for rel in to_delete:
            (self.saves_root / rel).unlink(missing_ok=True)
        self._prune_empty_dirs()
        manifest = {
            "format": MANIFEST_FORMAT,
            "app": "saveparty",
            "saveparty_version": __version__,
            "game": self.profile.title,
            "generation": (prev_manifest["generation"] + 1) if prev_manifest else 1,
            "token": new_token(),
            "pushed_by": player,
            "machine": machine_name(),
            "pushed_at": iso(utc_now()),
            "total_size": total_size(hashed_snap),
            "files": hashed_snap,
        }
        write_json_atomic(self.manifest_path, manifest)  # written last = commit point
        readme = self.root / CLOUD_README_NAME
        if not readme.exists():
            try:
                readme.write_text(CLOUD_README_TEXT, encoding="utf-8")
            except OSError:
                pass
        return manifest

    def _prune_empty_dirs(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.saves_root, topdown=False):
            if not dirnames and not filenames and Path(dirpath) != self.saves_root:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
