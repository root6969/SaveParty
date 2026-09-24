"""Ask the local Syncthing daemon whether a folder is fully synced.

SaveParty's cloud folder is usually a Syncthing folder. Pulling from it while
Syncthing is still catching up means loading a stale or half-downloaded world -
which is exactly what put players onto the wrong character. This module talks to
Syncthing's own REST API so a session can wait until the folder reports "Up to
Date" and the hub is connected.

The API endpoint (port + key) is auto-discovered and VALIDATED: it reads the
live `--gui-address` / `--home` off the running Syncthing process, plus every
config.xml it can find, then tries each (address, key) pair until one actually
answers. Everything degrades gracefully - no Syncthing, a folder Syncthing
doesn't manage, or an unreachable/mis-keyed daemon all return a status that does
NOT block the session (SaveParty also works with plain folders, Drive, etc.)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

_DISCOVER_BACKOFF = 20.0        # seconds to wait before re-probing after a failed discovery
_endpoint_cache: dict = {"base": None, "key": None, "checked": 0.0}


def _config_candidates() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Syncthing" / "config.xml")
        # Some installs keep their Syncthing home inside a Windows packaged-app
        # LocalCache (e.g. set up from inside a sandboxed app). The running daemon
        # then uses an api key that ISN'T in the standard config - so the status
        # check silently degrades to "unknown" (-). Discover those homes too.
        try:
            out += sorted((Path(local) / "Packages").glob(
                "*/LocalCache/Local/Syncthing/config.xml"))
        except Exception:
            pass
    out += [
        home / "AppData" / "Local" / "Syncthing" / "config.xml",  # Windows
        home / ".config" / "syncthing" / "config.xml",            # Linux
        home / ".local" / "state" / "syncthing" / "config.xml",   # Linux (newer)
        home / "Library" / "Application Support" / "Syncthing" / "config.xml",  # macOS
    ]
    seen, uniq = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _url_from_address(address: str) -> str | None:
    address = (address or "").strip()
    if not address:
        return None
    host, _, port = address.rpartition(":")
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{port or '8384'}"


def _syncthing_process():
    """The running syncthing psutil.Process (the `serve` one), or None."""
    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if not name.startswith("syncthing"):
                    continue
                cmdline = proc.info.get("cmdline") or []
                # skip helper subprocesses (e.g. the monitor); prefer the `serve` one
                if not cmdline or "serve" in cmdline or len(cmdline) <= 1:
                    return proc
            except Exception:
                continue
    except Exception:
        return None
    return None


def is_running() -> bool:
    return _syncthing_process() is not None


def _running_syncthing_info() -> tuple[str | None, str | None]:
    """(gui_base_url, home_dir) read off the LIVE syncthing process - the real
    port and config location, whatever they are. Best effort; blank on failure."""
    proc = _syncthing_process()
    if proc is None:
        return None, None
    try:
        cmdline = " ".join(proc.cmdline() or [])
    except Exception:
        return None, None
    gui = None
    m = re.search(r"--gui-address[=\s]+(\S+)", cmdline)
    if m:
        gui = _url_from_address(m.group(1))
    home = None
    m = re.search(r'--home[=\s]+(?:"([^"]+)"|(\S+))', cmdline)
    if m:
        home = (m.group(1) or m.group(2)).strip()
    return gui, home


def _startup_shortcut() -> Path | None:
    """The Syncthing autostart shortcut in the user's Startup folder, if any -
    launching it starts Syncthing exactly as configured (right --home/port)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    try:
        for lnk in startup.glob("*.lnk"):
            if "sync" in lnk.name.lower():
                return lnk
    except Exception:
        pass
    return None


def ensure_running() -> str:
    """Start Syncthing if it isn't already, using its own autostart shortcut so it
    launches with the correct config (Syncthing enforces one instance per --home,
    so this can't spawn a rogue second copy). Returns a short status string."""
    if is_running():
        return "already running"
    shortcut = _startup_shortcut()
    if shortcut is not None:
        try:
            os.startfile(str(shortcut))  # noqa: S606 - launches the user's own Syncthing
            return "started"
        except Exception as exc:
            return f"could not start ({exc})"
    return "not found"


def _keys_and_bases() -> tuple[list[str], list[str]]:
    """Candidate api keys and base URLs, most-likely first."""
    gui, home = _running_syncthing_info()
    bases = ["http://127.0.0.1:8384"]
    if gui and gui not in bases:
        bases.insert(0, gui)  # the port the daemon is ACTUALLY on
    configs = list(_config_candidates())
    if home:
        configs.insert(0, Path(home) / "config.xml")
    keys: list[str] = []
    for cfg in configs:
        try:
            if not cfg.is_file():
                continue
            gui_el = ElementTree.parse(cfg).getroot().find("gui")
            if gui_el is None:
                continue
            key = (gui_el.findtext("apikey") or "").strip()
            if key and key not in keys:
                keys.append(key)
            url = _url_from_address(gui_el.findtext("address") or "")
            if url and url not in bases:
                bases.append(url)
        except Exception:
            continue
    return keys, bases


def _get(base: str, key: str, path: str):
    req = urllib.request.Request(base + path, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _works(base: str, key: str) -> bool:
    try:
        _get(base, key, "/rest/system/status")
        return True
    except Exception:
        return False


def _discover():
    """A validated (base_url, api_key), or None. Cached; a failed probe backs off
    for a bit so we don't re-run the (slow) process query on every refresh."""
    cache = _endpoint_cache
    if cache["base"] and _works(cache["base"], cache["key"]):
        return cache["base"], cache["key"]
    if time.time() - cache.get("checked", 0.0) < _DISCOVER_BACKOFF:
        return None
    cache["checked"] = time.time()
    keys, bases = _keys_and_bases()
    for base in bases:
        for key in keys:
            if _works(base, key):
                cache["base"], cache["key"] = base, key
                return base, key
    cache["base"] = cache["key"] = None
    return None


@dataclass
class SyncStatus:
    managed: bool = False       # Syncthing has a folder at this path we can reach
    reachable: bool = False     # the daemon answered
    up_to_date: bool = True     # folder idle with nothing left to pull
    connected: bool = True      # at least one other device is connected
    state: str = ""             # idle / syncing / scanning / ...
    need_items: int = 0
    need_bytes: int = 0

    @property
    def should_wait(self) -> bool:
        if not self.managed:
            return False
        if not self.reachable:
            return True
        return (not self.up_to_date) or (not self.connected)


def status(cloud_dir) -> SyncStatus:
    """Sync status of the Syncthing folder living at `cloud_dir` (best effort)."""
    # Fast path: Syncthing drops a `.stfolder` marker in the ROOT of every folder
    # it manages. No marker at cloud_dir OR any parent → not inside a Syncthing
    # folder → skip the API entirely. Walking UP lets the cloud folder be a
    # SUBfolder of one shared folder (e.g. Games/palworld inside a shared Games/),
    # so you can add many games without pairing a new Syncthing folder each time.
    try:
        here = Path(cloud_dir)
        chain = [here, *here.parents]
        if not any((p / ".stfolder").exists() for p in chain):
            return SyncStatus(managed=False)
    except Exception:
        return SyncStatus(managed=False)

    got = _discover()
    if not got:
        return SyncStatus(managed=False, reachable=False)  # can't reach/auth → don't gate
    base, key = got

    try:
        folders = _get(base, key, "/rest/config/folders")
    except Exception:
        return SyncStatus(managed=True, reachable=False, up_to_date=False, state="unreachable")

    target = os.path.normcase(os.path.normpath(str(cloud_dir)))
    folder_id = None
    best_len = -1
    for folder in folders if isinstance(folders, list) else []:
        path = folder.get("path")
        if not path:
            continue
        fp = os.path.normcase(os.path.normpath(path))
        # Match if the Syncthing folder IS the cloud dir, or CONTAINS it (cloud
        # dir is a subfolder). Pick the deepest (most specific) match. The
        # trailing separator stops "…\games" from matching "…\games2".
        if target == fp or target.startswith(fp + os.sep):
            if len(fp) > best_len:
                folder_id, best_len = folder.get("id"), len(fp)
    if not folder_id:
        return SyncStatus(managed=False, reachable=True)  # a .stfolder but not in this daemon

    try:
        st = _get(base, key, f"/rest/db/status?folder={folder_id}")
    except Exception:
        return SyncStatus(managed=True, reachable=False, up_to_date=False, state="unreachable")

    need_items = int(st.get("needTotalItems", st.get("needItems", 0)) or 0)
    need_bytes = int(st.get("needBytes", 0) or 0)
    state = str(st.get("state", ""))

    connected = True
    try:
        conns = _get(base, key, "/rest/system/connections").get("connections", {})
        connected = any(v.get("connected") for v in conns.values())
    except Exception:
        pass

    return SyncStatus(
        managed=True,
        reachable=True,
        up_to_date=(state == "idle" and need_items == 0 and need_bytes == 0),
        connected=connected,
        state=state,
        need_items=need_items,
        need_bytes=need_bytes,
    )


def wait_reason(cloud_dir) -> str | None:
    """A human message if a session should wait for Syncthing, else None."""
    st = status(cloud_dir)
    if not st.should_wait:
        return None
    if not st.reachable:
        return ("Syncthing isn't running or isn't answering - open it (and wait for the "
                "hub to connect) so the latest world is here before you play.")
    if not st.up_to_date:
        left = f"{st.need_items} item(s) left" if st.need_items else (st.state or "syncing")
        return (f"Syncthing is still catching up ({left}). Wait until it shows "
                "'Up to Date', then press Play.")
    if not st.connected:
        return ("Syncthing isn't connected to the hub right now, so your world may be "
                "behind. Wait for the hub to reconnect, then press Play.")
    return None


# ----------------------------------------------------------------------
# upload confirmation (after a push)
# ----------------------------------------------------------------------


@dataclass
class UploadStatus:
    status: str  # uploaded | timeout | no-peers | unavailable
    detail: str = ""
    pending: list = field(default_factory=list)


def _post(base: str, key: str, path: str) -> None:
    req = urllib.request.Request(base + path, headers={"X-API-Key": key}, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=8):
        pass


def _folder_match(folders, cloud_dir):
    """The Syncthing folder dict whose path IS or CONTAINS cloud_dir (deepest)."""
    target = os.path.normcase(os.path.normpath(str(cloud_dir)))
    best, best_len = None, -1
    for f in folders if isinstance(folders, list) else []:
        p = f.get("path")
        if not p:
            continue
        fp = os.path.normcase(os.path.normpath(p))
        if (target == fp or target.startswith(fp + os.sep)) and len(fp) > best_len:
            best, best_len = f, len(fp)
    return best


def wait_uploaded(cloud_dir, on_progress=None, timeout: float = 300.0, interval: float = 2.0):
    """Block until every peer sharing this folder has received the latest push.

    Polls Syncthing's per-device completion so a player can't close the PC right
    after a Sync while the new generation is still only local. `on_progress(pct,
    pending_names)` is called each poll for a UI bar. Degrades gracefully (no
    Syncthing / non-managed folder / no peers) so it never blocks a plain-folder
    setup."""
    got = _discover()
    if not got:
        return UploadStatus("unavailable", "Syncthing API not reachable")
    base, key = got
    try:
        my_id = str(_get(base, key, "/rest/system/status").get("myID", ""))
        folders = _get(base, key, "/rest/config/folders")
    except Exception as exc:
        return UploadStatus("unavailable", f"Syncthing not reachable ({exc})")
    folder = _folder_match(folders, cloud_dir)
    if not folder:
        return UploadStatus("unavailable", "no Syncthing folder maps to the cloud dir")
    fid = folder.get("id")
    peers = [
        d.get("deviceID")
        for d in folder.get("devices", [])
        if d.get("deviceID") and d.get("deviceID") != my_id
    ]
    if not peers:
        return UploadStatus("no-peers", "no other devices share this folder")

    names: dict = {}
    try:
        for d in _get(base, key, "/rest/config/devices"):
            names[d.get("deviceID")] = d.get("name") or str(d.get("deviceID", ""))[:7]
    except Exception:
        pass

    try:  # nudge Syncthing to notice the just-written files immediately
        _post(base, key, f"/rest/db/scan?folder={fid}")
    except Exception:
        pass

    deadline = time.monotonic() + timeout
    while True:
        worst = 100.0
        pending: list = []
        for did in peers:
            label = names.get(did, str(did)[:7])
            try:
                comp = _get(base, key, f"/rest/db/completion?folder={fid}&device={did}")
            except Exception:
                pending.append(label)
                worst = min(worst, 0.0)
                continue
            pct = float(comp.get("completion", 0.0))
            need = int(comp.get("needBytes", 0) or 0) + int(comp.get("needItems", 0) or 0)
            worst = min(worst, pct)
            if need > 0 or pct < 100.0:
                pending.append(label)
        if on_progress:
            try:
                on_progress(worst, pending)
            except Exception:
                pass
        if not pending:
            return UploadStatus("uploaded", "", [])
        if time.monotonic() >= deadline:
            return UploadStatus("timeout", f"still uploading after {int(timeout)}s", pending)
        time.sleep(min(interval, max(0.5, deadline - time.monotonic())))
