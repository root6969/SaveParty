"""Sync engine: decides pull/push/conflict and orchestrates play sessions.

UI-agnostic: all user interaction goes through an `events` object with:

    log(text, style)                     style: info/success/warn/error/dim
    status(state, text)                  state: idle/syncing/waiting/playing/conflict/error
    confirm(text, default=True) -> bool
    resolve_conflict(info) -> str        one of "cloud" / "local" / "defer"

Decision model (three-way compare): a local state file records the manifest
generation+token and file snapshot from the last successful sync here.
"local changed" = disk differs from that snapshot; "cloud moved" = manifest
generation/token differ from it. Neither -> up to date; only cloud -> pull;
only local -> push; both -> conflict (never resolved silently).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .backup import list_backups, make_backup, safe_extract
from .cloudstore import CloudStore, new_token
from .game import is_game_running, launch_target, matching_processes
from .lock import CloudLock, LockStatus
from .profiles import Profile
from .snapshot import add_hashes, diff, scan_dir, snapshots_differ, total_size
from .util import (
    SavePartyError,
    human_ago,
    human_delta,
    human_size,
    iso,
    local_str,
    move_path,
    parse_iso,
    read_json,
    rmtree_robust,
    utc_now,
    write_json_atomic,
)

log = logging.getLogger("saveparty")

STATE_FORMAT = 1


class Verdict(Enum):
    NOTHING = "nothing"
    SEED = "seed"
    JOIN = "join"
    FIRST_TIME = "first-time"
    UP_TO_DATE = "up-to-date"
    PULL = "pull"
    PUSH = "push"
    CONFLICT = "conflict"
    CLOUD_MISSING = "cloud-missing"
    LOCAL_MISSING = "local-missing"
    LOCKED = "locked"


@dataclass
class Decision:
    verdict: Verdict
    snapshot: dict
    state: dict | None
    manifest: dict | None


class NullEvents:
    def log(self, text: str, style: str = "info") -> None:
        pass

    def status(self, state: str, text: str) -> None:
        pass

    def confirm(self, text: str, default: bool = True) -> bool:
        return default

    def resolve_conflict(self, info: dict) -> str:
        return "defer"


class SyncEngine:
    def __init__(self, profile: Profile, player_name: str, events=None):
        self.p = profile
        self.player = player_name
        self.events = events or NullEvents()
        self.store = CloudStore(profile)
        self.lock = CloudLock(
            Path(profile.cloud_dir),
            player_name,
            stale_minutes=profile.lock_stale_minutes,
            verify_seconds=profile.lock_verify_seconds,
        )
        self._cleanup_staging()

    # ------------------------------------------------------------------
    # local state
    # ------------------------------------------------------------------

    def scan_local(self) -> dict:
        return scan_dir(Path(self.p.save_dir), self.p.exclude_dir_set(), self.p.exclude_globs)

    def load_state(self) -> dict | None:
        try:
            data = read_json(self.p.state_path())
        except (OSError, ValueError):
            return None
        if not data or "snapshot" not in data or "generation" not in data:
            return None
        return data

    def save_state(self, manifest: dict, snapshot: dict) -> None:
        write_json_atomic(
            self.p.state_path(),
            {
                "format": STATE_FORMAT,
                "generation": manifest["generation"],
                "token": manifest["token"],
                "pushed_by": manifest.get("pushed_by", ""),
                "pushed_at": manifest.get("pushed_at", ""),
                "snapshot": snapshot,
                "synced_at": iso(utc_now()),
            },
        )

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    def evaluate(self) -> Decision:
        snapshot = self.scan_local()
        state = self.load_state()
        manifest = self.store.read_manifest(retries=1)
        return Decision(self._decide(snapshot, state, manifest), snapshot, state, manifest)

    def _decide(self, snapshot: dict, state: dict | None, manifest: dict | None) -> Verdict:
        local_has = bool(snapshot)
        if manifest is None:
            if state is not None:
                return Verdict.CLOUD_MISSING
            return Verdict.SEED if local_has else Verdict.NOTHING
        if state is None:
            return Verdict.FIRST_TIME if local_has else Verdict.JOIN
        if not local_has and state.get("snapshot"):
            return Verdict.LOCAL_MISSING
        changed = snapshots_differ(snapshot, state["snapshot"])
        head_moved = not (
            manifest["generation"] == state["generation"]
            and manifest.get("token") == state.get("token")
        )
        if not changed and not head_moved:
            return Verdict.UP_TO_DATE
        if not changed:
            return Verdict.PULL
        if not head_moved:
            return Verdict.PUSH
        return Verdict.CONFLICT

    # ------------------------------------------------------------------
    # core operations
    # ------------------------------------------------------------------

    def _ensure_game_closed(self, operation: str) -> None:
        if self.p.process_names and is_game_running(self.p.process_names):
            raise SavePartyError(
                f"{self.p.title} is running - close it before {operation}."
            )

    def do_pull(self, decision: Decision) -> None:
        """Download the cloud save: verify every checksum, stage the copy,
        back up the current local save, then swap folders atomically-ish."""
        self._ensure_game_closed("pulling")
        self.events.status("syncing", "Syncing… verifying cloud files")
        manifest = self.store.wait_until_verified(
            lambda text: self.events.status("syncing", f"Syncing… {text}")
        )
        staging_op = self.p.staging_root() / new_token()
        staged = staging_op / "save"
        target = Path(self.p.save_dir)
        try:
            self.events.status("syncing", "Syncing… downloading save files")
            self.store.pull_into(staged, manifest)
            backup_path = make_backup(
                target,
                self.p.backups_path(),
                "local-before-pull",
                self.p.exclude_dir_set(),
                self.p.exclude_globs,
                self.p.backup_keep,
            )
            if backup_path:
                self.events.log(f"Backed up local save to {backup_path.name}", "dim")
            self._preserve_excluded_dirs(target, staged)
            self._replace_dir(target, staged)
        finally:
            rmtree_robust(staging_op)
        snapshot = self.scan_local()  # re-scan real files so the base matches disk exactly
        self.save_state(manifest, snapshot)
        self.events.log(
            f"Pulled generation {manifest['generation']} "
            f"(by {manifest.get('pushed_by', '?')}, {human_size(total_size(snapshot))})",
            "success",
        )

    def do_push(self, decision: Decision) -> None:
        """Upload the local save: checksum it, back up the cloud's current
        version, copy only changed files, then publish the new manifest."""
        self._ensure_game_closed("pushing")
        newer = self.store.pending_higher_generation()
        if newer is not None:
            raise SavePartyError(
                f"refusing to push: a newer save (generation {newer}) exists but hasn't "
                "merged yet. Open Syncthing, let it reconcile the conflict, then Sync to "
                "pull before pushing - otherwise you'd overwrite everyone with an older world."
            )
        target = Path(self.p.save_dir)
        snapshot = self.scan_local()
        if not snapshot:
            raise SavePartyError(f"nothing to push: no save files in {target}")
        self.events.status("syncing", "Syncing… checksumming local save")
        hashed = add_hashes(target, snapshot)
        if decision.manifest is not None:
            backup_path = make_backup(
                self.store.saves_root,
                self.p.backups_path(),
                "cloud-before-push",
                self.p.exclude_dir_set(),
                self.p.exclude_globs,
                self.p.backup_keep,
            )
            if backup_path:
                self.events.log(f"Backed up cloud save to {backup_path.name}", "dim")
        self.events.status("syncing", "Syncing… uploading to the cloud folder")
        manifest = self.store.push_from(target, hashed, decision.manifest, self.player)
        self.save_state(manifest, snapshot)
        self.events.log(
            f"Pushed generation {manifest['generation']} ({human_size(total_size(snapshot))}).",
            "success",
        )
        self._wait_uploaded(manifest["generation"])

    def _wait_uploaded(self, generation: int) -> None:
        """After writing the push locally, wait until the other players' devices
        actually receive it. Writing to the shared folder is instant; Syncthing
        still has to upload. Without this, closing the PC right after a Sync could
        strand a generation locally and friends would never get it."""
        try:
            from . import syncthing

            def on_progress(pct: float, pending: list) -> None:
                self.events.status("syncing", f"Uploading to the hub… {int(pct)}%")

            result = syncthing.wait_uploaded(
                self.p.cloud_dir,
                on_progress=on_progress,
                timeout=float(self.p.cloud_wait_seconds),
            )
        except Exception:
            self.events.log(
                "Couldn't confirm the upload finished - keep your cloud client running a "
                "bit longer before shutting down.",
                "warn",
            )
            return

        if result.status == "uploaded":
            self.events.log(
                f"Generation {generation} is now on the hub - your friends can pull it. "
                "Safe to close.",
                "success",
            )
        elif result.status == "timeout":
            who = ", ".join(result.pending) or "a device"
            self.events.log(
                f"Still uploading to {who} - generation {generation} is NOT fully on the hub "
                "yet. Leave the PC and Syncthing on a little longer, or your friends won't get it.",
                "warn",
            )
        elif result.status == "no-peers":
            pass  # single device / plain folder: nothing to wait for
        else:  # unavailable
            self.events.log(
                "Keep your cloud client running until it finishes syncing before shutting "
                "the PC down.",
                "dim",
            )

    def export_conflict_copy(self) -> Path | None:
        safe = "".join(c for c in self.player if c.isalnum() or c in "-_") or "player"
        return make_backup(
            Path(self.p.save_dir),
            self.p.backups_path(),
            f"conflict-local-{safe}",
            self.p.exclude_dir_set(),
            self.p.exclude_globs,
            keep=0,  # never auto-pruned
        )

    def _preserve_excluded_dirs(self, old_root: Path, new_root: Path) -> None:
        """Excluded dirs are never synced; carry them over so a swap keeps them."""
        import os as _os

        exclude = self.p.exclude_dir_set()
        if not exclude or not old_root.is_dir():
            return
        for dirpath, dirnames, _files in _os.walk(old_root):
            for name in list(dirnames):
                if name.lower() in exclude:
                    src = Path(dirpath) / name
                    dst = new_root / src.relative_to(old_root)
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        move_path(src, dst)
                    dirnames.remove(name)

    def _replace_dir(self, target: Path, staged: Path) -> None:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            move_path(staged, target)
            return
        old = target.with_name(target.name + ".saveparty-old")
        if old.exists():
            rmtree_robust(old)
        move_path(target, old)
        try:
            move_path(staged, target)
        except BaseException:
            move_path(old, target)  # roll back so the player still has a save
            raise
        try:
            rmtree_robust(old)
        except OSError:
            self.events.log(f"Could not remove leftover folder {old} - safe to delete manually.", "warn")

    def _cleanup_staging(self) -> None:
        root = self.p.staging_root()
        if not root.is_dir():
            return
        cutoff = time.time() - 86400
        for child in root.iterdir():
            try:
                if child.stat().st_mtime < cutoff:
                    rmtree_robust(child)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # reconcile
    # ------------------------------------------------------------------

    def reconcile(self, check_lock: bool = True, assume_yes: bool = False) -> tuple[int, Verdict]:
        problems = self.p.validate_paths()
        if problems:
            for item in problems:
                self.events.log(item, "error")
            self.events.status("error", "Folders missing - check profile settings")
            return 1, Verdict.NOTHING
        if check_lock:
            code = self._check_foreign_lock()
            if code:
                return code, Verdict.LOCKED
        decision = self.evaluate()
        verdict = decision.verdict
        log.info("[%s] reconcile verdict: %s", self.p.title, verdict.value)
        code = self._act_on(decision, assume_yes)
        return code, verdict

    def _act_on(self, decision: Decision, assume_yes: bool) -> int:
        verdict = decision.verdict
        if verdict == Verdict.NOTHING:
            self.events.log("Nothing to sync yet: no local saves and the cloud folder is empty.", "info")
            self.events.status("idle", "Nothing to sync yet")
            return 0
        if verdict == Verdict.UP_TO_DATE:
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if verdict in (Verdict.PULL, Verdict.JOIN, Verdict.LOCAL_MISSING):
            if verdict == Verdict.JOIN:
                self.events.log("No local copy yet - downloading the shared save.", "info")
            self.do_pull(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if verdict == Verdict.PUSH:
            self.do_push(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if verdict == Verdict.SEED:
            size = human_size(total_size(decision.snapshot))
            if not assume_yes and not self.events.confirm(
                f"The cloud folder is empty. Upload your local save "
                f"({len(decision.snapshot)} files, {size}) as the shared save?",
                default=True,
            ):
                return 2
            self.do_push(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if verdict in (Verdict.FIRST_TIME, Verdict.CONFLICT):
            return self._conflict_flow(decision)
        if verdict == Verdict.CLOUD_MISSING:
            self.events.log(
                "The cloud manifest is gone but this PC synced with it before. Check the "
                "shared folder; use Push (force) to re-seed it from this machine.",
                "error",
            )
            self.events.status("error", "Cloud manifest missing")
            return 1
        return 1

    def _conflict_flow(self, decision: Decision) -> int:
        manifest = decision.manifest or {}
        state = decision.state or {}
        added, removed, changed = diff(state.get("snapshot", {}), decision.snapshot)
        info = {
            "title": self.p.title,
            "first_time": decision.verdict == Verdict.FIRST_TIME,
            "cloud_generation": manifest.get("generation"),
            "cloud_by": manifest.get("pushed_by", "?"),
            "cloud_at": local_str(parse_iso(manifest.get("pushed_at"))),
            "local_changed": len(added) + len(changed) + len(removed),
            "local_files": (added + changed + removed)[:8],
        }
        self.events.status("conflict", "Conflict - choose which version to keep")
        choice = self.events.resolve_conflict(info)
        if choice == "cloud":
            path = self.export_conflict_copy()
            if path:
                self.events.log(f"Your local save was kept as {path.name}", "warn")
            self.do_pull(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if choice == "local":
            self.do_push(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        self.events.log("Nothing changed - both versions are safe. Resolve when ready.", "info")
        return 2

    # ------------------------------------------------------------------
    # manual pull / push
    # ------------------------------------------------------------------

    def manual_pull(self, force: bool = False) -> int:
        """The Pull button: refuses to discard local changes unless forced
        (and even forced, the local version is exported as a backup first)."""
        code = self._check_foreign_lock()
        if code:
            return code
        decision = self.evaluate()
        if decision.verdict in (Verdict.PULL, Verdict.JOIN, Verdict.LOCAL_MISSING):
            self.do_pull(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if decision.verdict == Verdict.UP_TO_DATE:
            self.events.log("Already up to date - nothing to pull.", "info")
            return 0
        if decision.verdict in (Verdict.NOTHING, Verdict.SEED, Verdict.CLOUD_MISSING):
            self.events.log("There is no cloud save to pull.", "warn")
            return 1
        if not force:
            self.events.log(
                "Your local save has changes the cloud does not have - use Sync to "
                "resolve, or Pull (force) to overwrite local (a backup is kept).",
                "warn",
            )
            return 1
        if not self.events.confirm(
            "Overwrite your LOCAL save with the cloud version? A backup of your local "
            "files is kept.",
            default=False,
        ):
            return 2
        path = self.export_conflict_copy()
        if path:
            self.events.log(f"Local save exported to {path.name}", "warn")
        self.do_pull(decision)
        self.events.status("idle", "Idle (Up to Date)")
        return 0

    def manual_push(self, force: bool = False) -> int:
        """The Push button: refuses to overwrite friends' newer progress
        unless forced (the cloud version is backed up first regardless)."""
        code = self._check_foreign_lock()
        if code:
            return code
        decision = self.evaluate()
        if decision.verdict == Verdict.PUSH:
            self.do_push(decision)
            self.events.status("idle", "Idle (Up to Date)")
            return 0
        if decision.verdict == Verdict.SEED:
            return self._act_on(decision, assume_yes=False)
        if decision.verdict == Verdict.UP_TO_DATE:
            self.events.log("Already up to date - nothing to push.", "info")
            return 0
        if decision.verdict in (Verdict.NOTHING, Verdict.LOCAL_MISSING):
            self.events.log("There are no local save files to push.", "warn")
            return 1
        if not force:
            self.events.log(
                "The cloud has changes your local save does not have - use Sync to "
                "resolve, or Push (force) to overwrite the cloud (a backup is kept).",
                "warn",
            )
            return 1
        if not self.events.confirm(
            "Overwrite the CLOUD save with your local version? The cloud version is "
            "backed up first.",
            default=False,
        ):
            return 2
        self.do_push(decision)
        self.events.status("idle", "Idle (Up to Date)")
        return 0

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------

    def restore_backup(self, zip_path: Path) -> None:
        """Roll the local save back to a backup zip (current save is zipped
        first, so a restore is always reversible)."""
        self._ensure_game_closed("restoring a backup")
        target = Path(self.p.save_dir)
        staging_op = self.p.staging_root() / new_token()
        staged = staging_op / "save"
        try:
            count = safe_extract(zip_path, staged)
            backup_path = make_backup(
                target,
                self.p.backups_path(),
                "before-restore",
                self.p.exclude_dir_set(),
                self.p.exclude_globs,
                self.p.backup_keep,
            )
            if backup_path:
                self.events.log(f"Current save backed up to {backup_path.name}", "dim")
            self._preserve_excluded_dirs(target, staged)
            self._replace_dir(target, staged)
        finally:
            rmtree_robust(staging_op)
        self.events.log(
            f"Restored {zip_path.name} ({count} files). Use Sync to publish the "
            "restored version to your friends.",
            "success",
        )

    # ------------------------------------------------------------------
    # lock helpers
    # ------------------------------------------------------------------

    def _check_foreign_lock(self) -> int:
        """One-shot operations refuse to run while a friend holds the lock;
        our own leftover lock (crashed session) is cleared automatically."""
        info = self.lock.read()
        if info is None or self.lock.is_mine_token(info):
            return 0
        if self.lock.is_my_identity(info):
            if not (self.p.process_names and is_game_running(self.p.process_names)):
                self.lock.force_release()
                self.events.log("Cleared leftover lock from a previous session on this PC.", "dim")
            return 0
        if info.corrupt:
            self.events.log("The cloud lock file is unreadable (possibly mid-sync). Try again shortly.", "warn")
            return 3
        if self.lock.is_stale(info):
            self.events.log(
                f"Cloud locked by {info.player} but looks stale "
                f"(heartbeat {human_ago(info.heartbeat_at or info.acquired_at)}) - "
                "use Unlock if they are truly not playing.",
                "warn",
            )
            self.events.status("waiting", f"Locked by {info.player} (stale?)")
            return 3
        self.events.status("waiting", f"{info.player} is currently playing")
        self.events.log(f"{info.player} is currently playing - try again when they finish.", "info")
        return 3

    # ------------------------------------------------------------------
    # play session
    # ------------------------------------------------------------------

    def play_session(self, stop_event: threading.Event, assume_yes: bool = True) -> int:
        """Full session: lock -> pull -> launch -> watch -> push -> unlock.

        stop_event ends the session manually (mandatory exit path for games
        without process detection; optional early-stop for the rest).
        """
        problems = self.p.validate_paths()
        if problems:
            for item in problems:
                self.events.log(item, "error")
            self.events.status("error", "Folders missing - check profile settings")
            return 1

        # Don't even start while Syncthing is still delivering the latest world -
        # loading a half-synced folder is what put players on the wrong character.
        sync_wait = self._syncthing_wait()
        if sync_wait:
            self.events.log(sync_wait, "error")
            self.events.status("error", "Waiting for Syncthing to finish")
            return 5

        # Fork guard (needs no Syncthing API): if a newer save sits in a conflict
        # copy, someone played on a stale world and the sync tool kept the older
        # one as "live". Refuse to launch so we don't stack another split on top.
        newer = self.store.pending_higher_generation()
        if newer is not None:
            self.events.log(
                f"A newer save (generation {newer}) exists but hasn't merged - someone "
                "played on an out-of-date world. Open Syncthing, let it reconcile the "
                "conflict, then play. Not launching, to avoid another split.",
                "error",
            )
            self.events.status("error", "Newer save unmerged - resolve before playing")
            return 6

        attach = bool(self.p.process_names) and is_game_running(self.p.process_names)
        if attach:
            self.events.log(
                f"{self.p.title} is already running - skipping the pre-game pull; "
                "progress will still be pushed when it exits.",
                "warn",
            )

        try:
            # 1. Lock (wait politely while a friend plays).
            if not self._acquire_lock_waiting(stop_event):
                return 3

            if not attach:
                # 2. Pull latest / push leftovers; conflicts pause the launch.
                code, verdict = self.reconcile(check_lock=False, assume_yes=assume_yes)
                if code:
                    self.events.log("Not starting the game until the save state is resolved.", "error")
                    return code
                self._pre_session_game_hook()
                # 2b. Safeguard: never launch onto the wrong character.
                block = self._own_character_block()
                if block:
                    self.events.log(block, "error")
                    self.events.status("error", "Not launched - you'd be on the wrong character")
                    return 4
                # 3. Launch.
                if self.p.launch:
                    self.events.log(f"Launching {self.p.title}…", "info")
                    try:
                        launch_target(self.p.launch)
                    except OSError as exc:
                        self.events.log(f"Could not launch ({exc}) - start the game manually.", "warn")
                else:
                    self.events.log("Start the game whenever you are ready.", "info")
                if self.p.process_names:
                    if not self._wait_for_game_start(stop_event):
                        self.events.log("Game did not start - releasing the lock.", "warn")
                        return 0

            # 4. The session.
            self._session_loop(stop_event)

            # 5. Settle, then push this session's progress.
            self._wait_quiesce()
            code, verdict = self.reconcile(check_lock=False, assume_yes=True)
            if code == 0 and verdict == Verdict.UP_TO_DATE:
                self.events.log("No save changes were detected this session.", "info")
            return code
        finally:
            if self.lock.token:
                try:
                    if self.lock.release() == "released":
                        self.events.log("Cloud lock released.", "dim")
                except OSError as exc:
                    self.events.log(f"Could not release the cloud lock ({exc}).", "warn")

    def _pre_session_game_hook(self) -> None:
        """Game-specific pre-launch steps (currently: Palworld host-character swap).

        Never blocks the session - any failure degrades to playing as-is.
        """
        if self.p.game_id != "palworld":
            return
        try:
            from . import palworld

            palworld.pre_session(self.p, self.player, self.events)
        except Exception as exc:
            self.events.log(f"Palworld character step skipped ({exc}).", "warn")

    def _syncthing_wait(self) -> str | None:
        """A reason to wait for Syncthing (folder still catching up / hub not
        connected), or None. No-ops for non-Syncthing folders and unreachable
        daemons so it never false-blocks."""
        try:
            from . import syncthing

            return syncthing.wait_reason(self.p.cloud_dir)
        except Exception:
            return None

    def _own_character_block(self) -> str | None:
        """Palworld safeguard: a reason to refuse launch when this player's own
        character isn't the one that would load (swap couldn't place them), else None."""
        if self.p.game_id != "palworld":
            return None
        try:
            from . import palworld

            return palworld.hosting_conflict(self.p, self.player)
        except Exception:
            return None  # a safeguard must never itself break the session

    def _acquire_lock_waiting(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            status_, info = self.lock.acquire()
            if status_ == LockStatus.ACQUIRED:
                self.events.log("Cloud lock acquired - friends will see you playing.", "success")
                return True
            if status_ == LockStatus.LOST:
                self.events.log("Another player grabbed the lock at the same moment.", "warn")
                time.sleep(2)
                continue
            assert info is not None
            if info.corrupt or self.lock.is_stale(info):
                if self.events.confirm(
                    f"The cloud lock is held by {info.player} but looks stale "
                    f"(heartbeat {human_ago(info.heartbeat_at or info.acquired_at)}). "
                    "Take it over? Only if they are NOT playing.",
                    default=False,
                ):
                    status_, _ = self.lock.acquire(takeover=True)
                    if status_ == LockStatus.ACQUIRED:
                        self.events.log("Lock taken over.", "success")
                        return True
                    continue
                return False
            self.events.status("waiting", f"{info.player} is currently playing - waiting…")
            for _ in range(int(max(self.p.cloud_wait_interval, 3))):
                if stop_event.is_set():
                    return False
                time.sleep(1)
            current = self.lock.read()
            if current is None or self.lock.is_stale(current) or self.lock.is_my_identity(current):
                continue
        return False

    def _wait_for_game_start(self, stop_event: threading.Event) -> bool:
        deadline = time.monotonic() + self.p.game_start_timeout
        self.events.status("waiting", f"Waiting for {self.p.title} to start…")
        while time.monotonic() < deadline and not stop_event.is_set():
            if is_game_running(self.p.process_names):
                self.events.log(f"{self.p.title} is running.", "success")
                return True
            time.sleep(2)
        return False

    def _session_loop(self, stop_event: threading.Event) -> None:
        # While the game is running SaveParty stays completely HANDS-OFF the save
        # folder: no file watcher, no scanning, not a single open handle on the
        # save tree. On some machines (Boss, Lusia) Palworld's co-op *host* save
        # fails with "save failed" if anything else is touching the save folder
        # at the exact moment it writes its many files - a live watcher there was
        # enough to trigger it. Closing SaveParty during play fixed it for them;
        # this makes the app behave that way on its own. We only watch the game
        # process (to know when it exits) and heartbeat the cloud lock (which
        # lives in the cloud folder, NOT the save folder). Once the game exits,
        # _wait_quiesce scans the folder (stat-only) to sync - safe, game closed.
        started = time.monotonic()
        hb_interval = max(self.p.lock_heartbeat_seconds, 10)
        next_hb = time.monotonic() + hb_interval
        lock_lost_warned = False
        while not stop_event.is_set():
            if self.p.process_names and not matching_processes(self.p.process_names):
                break
            now = time.monotonic()
            if now >= next_hb:
                next_hb = now + hb_interval
                try:
                    if not self.lock.heartbeat() and not lock_lost_warned:
                        lock_lost_warned = True
                        self.events.log(
                            "Another player took the cloud lock during your session! "
                            "Coordinate before anyone syncs.",
                            "error",
                        )
                except OSError:
                    pass
            self.events.status(
                "playing",
                f"Playing (Cloud Locked) - {human_delta(now - started)}",
            )
            time.sleep(2)
        self.events.log(f"Session ended after {human_delta(time.monotonic() - started)}.", "info")

    def _wait_quiesce(self) -> None:
        target = Path(self.p.save_dir)
        if not target.is_dir():
            return
        settle = max(self.p.settle_seconds, 1)
        deadline = time.monotonic() + self.p.settle_max_wait
        exclude, globs = self.p.exclude_dir_set(), self.p.exclude_globs
        prev = scan_dir(target, exclude, globs)
        self.events.status("syncing", "Waiting for save files to settle…")
        while time.monotonic() < deadline:
            time.sleep(settle)
            cur = scan_dir(target, exclude, globs)
            if not snapshots_differ(prev, cur):
                return
            prev = cur

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status_summary(self) -> dict:
        """Structured status for the UI."""
        out: dict = {"title": self.p.title, "problems": self.p.validate_paths()}
        snapshot = self.scan_local()
        out["local_files"] = len(snapshot)
        out["local_size"] = human_size(total_size(snapshot))
        manifest = None
        try:
            manifest = self.store.read_manifest()
            out["cloud_error"] = None
        except SavePartyError as exc:
            out["cloud_error"] = str(exc)
        if manifest:
            out["cloud"] = (
                f"generation {manifest['generation']} · by {manifest.get('pushed_by', '?')} · "
                f"{local_str(parse_iso(manifest.get('pushed_at')))}"
            )
        else:
            out["cloud"] = "empty - no save pushed yet"
        info = self.lock.read()
        if info is None:
            out["lock"] = None
        else:
            stale = " (stale?)" if self.lock.is_stale(info) else ""
            out["lock"] = f"{info.player} since {local_str(info.acquired_at)}{stale}"
            out["lock_player"] = info.player
            out["lock_stale"] = self.lock.is_stale(info)
        if out["problems"]:
            out["verdict"] = "error"
            out["status_text"] = "Folders missing - open Settings"
        elif info is not None and not self.lock.is_stale(info):
            out["verdict"] = "playing"
            out["status_text"] = f"Playing (Cloud Locked by {info.player})"
        else:
            verdict = self._decide(snapshot, self.load_state(), manifest)
            mapping = {
                Verdict.UP_TO_DATE: ("idle", "Idle (Up to Date)"),
                Verdict.NOTHING: ("idle", "Nothing to sync yet"),
                Verdict.PULL: ("behind", "Behind cloud - Sync to pull"),
                Verdict.JOIN: ("behind", "Not downloaded yet - Sync to join"),
                Verdict.PUSH: ("ahead", "Ahead of cloud - Sync to push"),
                Verdict.SEED: ("ahead", "Cloud empty - Sync to upload"),
                Verdict.FIRST_TIME: ("conflict", "First sync - needs your choice"),
                Verdict.CONFLICT: ("conflict", "CONFLICT - Sync to resolve"),
                Verdict.LOCAL_MISSING: ("behind", "Local files missing - Sync to restore"),
                Verdict.CLOUD_MISSING: ("error", "Cloud manifest missing"),
            }
            out["verdict"], out["status_text"] = mapping.get(verdict, ("idle", verdict.value))
        out["backups"] = len(list_backups(self.p.backups_path()))
        return out
