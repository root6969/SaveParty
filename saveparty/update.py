"""Self-update over the shared cloud folder.

Every player already syncs the same cloud folder (Syncthing to the hub), so that
folder doubles as the update channel: the developer drops a new build plus an
``updates/latest.json`` descriptor there, everyone's Syncthing mirrors it, and on
the next launch SaveParty notices the newer version and offers to install it - no
internet call, no manual sending.

Cloud layout added by this module (inside a profile's cloud folder)::

    <cloud_dir>/updates/
        latest.json            version descriptor (see UpdateInfo)
        SaveParty-<version>.exe the build being distributed

A running Windows .exe cannot overwrite itself, so applying an update stages the
verified build next to the current exe and hands off to a tiny detached batch
that waits for this process to exit, swaps the file, and relaunches.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .profiles import Profile, data_home
from .snapshot import hash_file
from .util import (
    SavePartyError,
    copy2_retry,
    iso,
    machine_name,
    read_json,
    utc_now,
    write_json_atomic,
)

log = logging.getLogger("saveparty.update")

UPDATES_DIRNAME = "updates"
LATEST_NAME = "latest.json"
UPDATE_FORMAT = 1
STAGED_EXE_NAME = "SaveParty.update.exe"
EXE_PREFIX = "SaveParty"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    file: str  # exe file name inside updates/
    sha256: str
    size: int = 0
    notes: str = ""
    mandatory: bool = False
    published_by: str = ""
    published_at: str = ""


# ----------------------------------------------------------------------
# version comparison
# ----------------------------------------------------------------------


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    a, b = _parse_version(candidate), _parse_version(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


# ----------------------------------------------------------------------
# reading the channel
# ----------------------------------------------------------------------


def updates_dir(cloud_dir: str | Path) -> Path:
    return Path(cloud_dir) / UPDATES_DIRNAME


def read_latest(cloud_dir: str | Path) -> UpdateInfo | None:
    path = updates_dir(cloud_dir) / LATEST_NAME
    try:
        data = read_json(path)
    except Exception as exc:  # a partially-synced json is not fatal
        log.warning("could not read update descriptor: %s", exc)
        return None
    if not data:
        return None
    try:
        return UpdateInfo(
            version=str(data["version"]),
            file=str(data["file"]),
            sha256=str(data["sha256"]).lower(),
            size=int(data.get("size", 0)),
            notes=str(data.get("notes", "")),
            mandatory=bool(data.get("mandatory", False)),
            published_by=str(data.get("published_by", "")),
            published_at=str(data.get("published_at", "")),
        )
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("update descriptor is malformed: %s", exc)
        return None


def available_update(profiles: list[Profile]) -> tuple[UpdateInfo, str] | None:
    """The newest published build across the profiles' cloud folders, if any is
    newer than what we run. Returns (info, cloud_dir) or None."""
    best: tuple[UpdateInfo, str] | None = None
    seen: set[str] = set()
    for prof in profiles:
        cloud = prof.cloud_dir
        if not cloud or cloud in seen:
            continue
        seen.add(cloud)
        info = read_latest(cloud)
        if info is None or not is_newer(info.version, __version__):
            continue
        if best is None or is_newer(info.version, best[0].version):
            best = (info, cloud)
    return best


# ----------------------------------------------------------------------
# applying an update (frozen exe only)
# ----------------------------------------------------------------------


def running_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _verify(path: Path, info: UpdateInfo) -> bool:
    if not path.is_file():
        return False
    if info.size and path.stat().st_size != info.size:
        return False
    return hash_file(path) == info.sha256


def _launch_swap_script(staged: Path, target: Path) -> None:
    """Spawn a detached batch that waits for us to exit, swaps the exe, relaunches.

    A clean PATH keeps the batch on Windows' own find/ping/tasklist even if
    SaveParty was launched from a shell whose PATH shadows them with unix tools.
    CREATE_NO_WINDOW runs the swap invisibly; the relaunched app opens its own
    window via `start`.
    """
    script = data_home() / "update" / "apply_update.bat"
    script.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    script.write_text(
        "@echo off\r\n"
        "setlocal enableextensions\r\n"
        'set "PATH=%SystemRoot%\\system32;%SystemRoot%;%SystemRoot%\\system32\\Wbem"\r\n'
        'set "PID=%~1"\r\n'
        'set "SRC=%~2"\r\n'
        'set "DST=%~3"\r\n'
        ":wait\r\n"
        'tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul '
        "&& ( ping -n 2 127.0.0.1 >nul & goto wait )\r\n"
        ":swap\r\n"
        'move /y "%SRC%" "%DST%" >nul 2>&1 '
        "|| ( ping -n 2 127.0.0.1 >nul & goto swap )\r\n"
        # Let the filesystem/AV settle on the freshly-swapped exe, then relaunch
        # via Explorer so the new process starts in the normal user shell context
        # (a real double-click) instead of inheriting this hidden cmd's minimal
        # environment - which otherwise made the onefile bootloader fail to load
        # python3xx.dll on relaunch.
        "ping -n 3 127.0.0.1 >nul\r\n"
        '"%SystemRoot%\\explorer.exe" "%DST%"\r\n'
        'del "%~f0" >nul 2>&1\r\n',
        encoding="ascii",
    )
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(
        [comspec, "/c", str(script), str(pid), str(staged), str(target)],
        creationflags=flags,
        close_fds=True,
    )


def apply_update(info: UpdateInfo, cloud_dir: str | Path) -> bool:
    """Stage and swap in the new build. Returns True once the swap is handed off;
    the caller must then close the app so the batch can replace the exe. Returns
    False if the build cannot be verified yet (cloud still downloading it)."""
    source = updates_dir(cloud_dir) / info.file
    if not _verify(source, info):
        log.warning("update build not fully synced yet: %s", source)
        return False
    target = Path(sys.executable)
    staged = target.with_name(STAGED_EXE_NAME)
    try:
        if staged.exists():
            staged.unlink()
        copy2_retry(source, staged)
    except OSError as exc:
        log.warning("could not stage update: %s", exc)
        return False
    if hash_file(staged) != info.sha256:
        try:
            staged.unlink()
        except OSError:
            pass
        return False
    _launch_swap_script(staged, target)
    return True


# ----------------------------------------------------------------------
# publishing (developer side; run via `SaveParty.exe --publish-update ...`)
# ----------------------------------------------------------------------


def publish_update(
    cloud_dir: str | Path,
    *,
    player_name: str,
    version: str,
    exe_path: Path,
    notes: str = "",
    mandatory: bool = False,
) -> UpdateInfo:
    """Copy a build into the cloud updates/ folder and write latest.json."""
    source = Path(exe_path)
    if not source.is_file():
        raise SavePartyError(f"build not found: {source}")
    dest_dir = updates_dir(cloud_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"{EXE_PREFIX}-{version}.exe"
    dest = dest_dir / dest_name
    copy2_retry(source, dest)
    info = UpdateInfo(
        version=version,
        file=dest_name,
        sha256=hash_file(dest),
        size=dest.stat().st_size,
        notes=notes,
        mandatory=mandatory,
        published_by=player_name,
        published_at=iso(utc_now()),
    )
    write_json_atomic(dest_dir / LATEST_NAME, {
        "format": UPDATE_FORMAT,
        "app": "saveparty",
        "version": info.version,
        "file": info.file,
        "sha256": info.sha256,
        "size": info.size,
        "notes": info.notes,
        "mandatory": info.mandatory,
        "published_by": info.published_by,
        "published_at": info.published_at,
        "published_from": machine_name(),
    })
    return info


def publish_main(argv: list[str]) -> int:
    """Console entrypoint for `SaveParty.exe --publish-update ...`.

    Options: --version X (required), --exe PATH (default: the running exe),
    --notes "..", --mandatory, --cloud PATH or --profile TITLE (which profile's
    cloud folder to publish into; default: the only/first profile).
    """
    import argparse

    from .profiles import ProfileStore

    parser = argparse.ArgumentParser(prog="SaveParty --publish-update")
    parser.add_argument("--publish-update", action="store_true")
    parser.add_argument("--version", required=True)
    parser.add_argument("--exe", default=None)
    parser.add_argument("--notes", default="")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--cloud", default=None)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)

    store = ProfileStore().load()
    cloud = args.cloud
    if not cloud:
        profs = store.profiles
        if args.profile:
            profs = [p for p in profs if p.title.lower() == args.profile.lower()]
        if not profs:
            print("No matching profile with a cloud folder found. Use --cloud PATH.")
            return 1
        cloud = profs[0].cloud_dir
    exe = Path(args.exe) if args.exe else Path(sys.executable)
    info = publish_update(
        cloud, player_name=store.player_name, version=args.version,
        exe_path=exe, notes=args.notes, mandatory=args.mandatory,
    )
    tag = "MANDATORY" if info.mandatory else "optional"
    print(f"Published {info.file} (v{info.version}, {tag}) to {updates_dir(cloud)}")
    print(f"  sha256 {info.sha256[:16]}…  size {info.size} bytes")
    print("Syncthing will now mirror it to your friends; their next launch offers it.")
    return 0
