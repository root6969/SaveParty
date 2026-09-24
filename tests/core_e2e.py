"""End-to-end test of SaveParty's sync core (no UI, no real games).

Two simulated players share one cloud folder; the test drives the engine
in-process through: seed -> join -> push/pull -> excluded-dir preservation ->
conflict (resolve + defer) -> foreign lock -> restore -> half-uploaded cloud
detection -> a full play session (manual end).

Run:  python tests/core_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saveparty.engine import SyncEngine, Verdict  # noqa: E402
from saveparty.profiles import Profile  # noqa: E402
from saveparty.util import SavePartyError  # noqa: E402

_step_no = 0


def step(title: str) -> None:
    global _step_no
    _step_no += 1
    print(f"=== step {_step_no}: {title} ===")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ScriptedEvents:
    """Engine events with pre-programmed answers."""

    def __init__(self):
        self.logs: list[tuple[str, str]] = []
        self.confirm_answers: list[bool] = []
        self.conflict_answers: list[str] = []

    def log(self, text: str, style: str = "info") -> None:
        self.logs.append((style, text))

    def status(self, state: str, text: str) -> None:
        pass

    def confirm(self, text: str, default: bool = True) -> bool:
        return self.confirm_answers.pop(0) if self.confirm_answers else default

    def resolve_conflict(self, info: dict) -> str:
        return self.conflict_answers.pop(0) if self.conflict_answers else "defer"

    def text(self) -> str:
        return "\n".join(t for _s, t in self.logs)


def make_profile(save_dir: Path, cloud: Path) -> Profile:
    return Profile.new(
        title="TestGame",
        game_id="custom",
        save_dir=str(save_dir),
        cloud_dir=str(cloud),
        exclude_dirs=["backup"],
        lock_verify_seconds=0,
        cloud_wait_seconds=6,
        cloud_wait_interval=1.0,
        settle_seconds=1,
        settle_max_wait=3,
    )


def engine_for(player: str, home: Path, profile: Profile) -> tuple[SyncEngine, ScriptedEvents]:
    os.environ["SAVEPARTY_HOME"] = str(home)
    events = ScriptedEvents()
    return SyncEngine(profile, player, events), events


def append(path: Path, blob: bytes) -> None:
    with open(path, "ab") as fh:
        fh.write(blob)


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="saveparty-e2e-"))
    print(f"sandbox: {sandbox}")
    try:
        home_a, home_b = sandbox / "homeA", sandbox / "homeB"
        save_a, save_b = sandbox / "pcA" / "TestGame", sandbox / "pcB" / "TestGame"
        cloud = sandbox / "cloud" / "TestGame"
        for p in (save_a, save_b, cloud):
            p.mkdir(parents=True)
        (save_a / "world.dat").write_bytes(os.urandom(150_000))
        (save_a / "player.dat").write_bytes(os.urandom(4_000))
        junk = save_a / "backup"
        junk.mkdir()
        (junk / "old.zip").write_bytes(b"game's own backup, must never sync")
        profile_a = make_profile(save_a, cloud)
        profile_b = make_profile(save_b, cloud)

        step("PlayerA seeds the cloud (generation 1)")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        code, verdict = engine.reconcile(assume_yes=True)
        check(code == 0 and verdict == Verdict.SEED, f"seed failed: {code} {verdict}")
        manifest = json.loads((cloud / "manifest.json").read_text(encoding="utf-8"))
        check(manifest["generation"] == 1, "bad manifest generation")
        check((cloud / "saves" / "world.dat").is_file(), "world.dat missing in cloud")
        check(not (cloud / "saves" / "backup").exists(), "excluded dir leaked to cloud")

        step("PlayerB joins and pulls")
        engine, events = engine_for("PlayerB", home_b, profile_b)
        code, verdict = engine.reconcile()
        check(code == 0 and verdict == Verdict.JOIN, f"join failed: {code} {verdict}")
        check(sha(save_b / "world.dat") == sha(save_a / "world.dat"), "pulled file differs")

        step("PlayerB plays and pushes generation 2")
        append(save_b / "world.dat", os.urandom(9_000))
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        code, verdict = engine.reconcile()
        check(code == 0 and verdict == Verdict.PUSH, f"push failed: {code} {verdict}")
        check(list((home_b / "_backups").rglob("cloud-before-push_*.zip")), "cloud backup missing")

        step("PlayerA pulls generation 2; own backup/ dir survives")
        engine, _ = engine_for("PlayerA", home_a, profile_a)
        code, verdict = engine.reconcile()
        check(code == 0 and verdict == Verdict.PULL, f"pull failed: {code} {verdict}")
        check(sha(save_a / "world.dat") == sha(save_b / "world.dat"), "files differ after pull")
        check((save_a / "backup" / "old.zip").is_file(), "excluded dir lost during pull")
        check(list((home_a / "_backups").rglob("local-before-pull_*.zip")), "local backup missing")

        step("conflict: PlayerA keeps the cloud version")
        append(save_a / "world.dat", b"A" * 5_000)
        append(save_b / "world.dat", b"B" * 7_000)
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        check(engine.reconcile()[1] == Verdict.PUSH, "B push expected")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        events.conflict_answers = ["cloud"]
        code, verdict = engine.reconcile()
        check(code == 0 and verdict == Verdict.CONFLICT, f"conflict flow failed: {code} {verdict}")
        check(sha(save_a / "world.dat") == sha(save_b / "world.dat"), "conflict pull mismatch")
        check(list((home_a / "_backups").rglob("conflict-local-PlayerA_*.zip")), "conflict export missing")

        step("deferring a conflict changes nothing and returns 2")
        append(save_a / "world.dat", b"A2")
        append(save_b / "world.dat", b"B2" * 1_500)
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        engine.reconcile()  # generation 4
        before = sha(save_a / "world.dat")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        events.conflict_answers = ["defer"]
        code, verdict = engine.reconcile()
        check(code == 2 and verdict == Verdict.CONFLICT, "defer should return 2")
        check(sha(save_a / "world.dat") == before, "defer modified local files")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        events.conflict_answers = ["cloud"]
        check(engine.reconcile()[0] == 0, "resolving after defer failed")

        step("a fresh foreign lock blocks sync (code 3)")
        lock_payload = {
            "format": 1, "app": "saveparty", "player": "PlayerB", "machine": "other-pc",
            "pid": 1, "token": "deadbeef",
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (cloud / "saveparty.lock").write_text(json.dumps(lock_payload), encoding="utf-8")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        code, verdict = engine.reconcile()
        check(code == 3 and verdict == Verdict.LOCKED, "foreign lock not respected")
        check("currently playing" in events.text(), "missing playing message")
        engine.lock.force_release()
        check(not (cloud / "saveparty.lock").exists(), "unlock failed")

        step("restore a backup, then publish the restored save")
        engine, _ = engine_for("PlayerA", home_a, profile_a)
        backups = sorted((home_a / "_backups").rglob("local-before-pull_*.zip"))
        engine.restore_backup(backups[-1])
        check(list((home_a / "_backups").rglob("before-restore_*.zip")), "before-restore missing")
        engine, _ = engine_for("PlayerA", home_a, profile_a)
        code, verdict = engine.reconcile()
        check(code == 0 and verdict == Verdict.PUSH, "restore should make local ahead")
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        check(engine.reconcile()[1] == Verdict.PULL, "B should pull the restored save")
        check(sha(save_a / "world.dat") == sha(save_b / "world.dat"), "restore round-trip mismatch")

        step("a half-uploaded cloud is detected and never pulled")
        append(save_a / "world.dat", b"heal me")
        engine, _ = engine_for("PlayerA", home_a, profile_a)
        engine.reconcile()
        level = cloud / "saves" / "world.dat"
        level.write_bytes(level.read_bytes()[:40_000])  # simulate interrupted upload
        engine, events = engine_for("PlayerB", home_b, profile_b)
        try:
            engine.reconcile()
            raise AssertionError("corrupted cloud was pulled!")
        except SavePartyError as exc:
            check("incomplete" in str(exc), f"unexpected error: {exc}")
        append(save_a / "world.dat", b"heal push")
        engine, _ = engine_for("PlayerA", home_a, profile_a)
        check(engine.reconcile()[1] == Verdict.PUSH, "heal push failed")
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        check(engine.reconcile()[1] == Verdict.PULL, "post-heal pull failed")
        check(sha(save_a / "world.dat") == sha(save_b / "world.dat"), "heal round-trip mismatch")

        step("full play session with manual end (no process detection)")
        append(save_a / "world.dat", b"session progress")
        engine, events = engine_for("PlayerA", home_a, profile_a)
        stop = threading.Event()
        threading.Timer(2.0, stop.set).start()
        code = engine.play_session(stop)
        check(code == 0, f"play session failed: {code}\n{events.text()}")
        check(not (cloud / "saveparty.lock").exists(), "lock not released after session")
        manifest = json.loads((cloud / "manifest.json").read_text(encoding="utf-8"))
        check(manifest["pushed_by"] == "PlayerA", "session push missing")
        engine, _ = engine_for("PlayerB", home_b, profile_b)
        check(engine.reconcile()[1] == Verdict.PULL, "B should pull session progress")
        check(sha(save_a / "world.dat") == sha(save_b / "world.dat"), "session round-trip mismatch")

        print("\nALL CORE E2E STEPS PASSED")
        return 0
    finally:
        os.environ.pop("SAVEPARTY_HOME", None)
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
