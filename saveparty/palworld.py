"""Palworld character-slot management: the host plays their own character.

Palworld hard-maps whoever hosts a world onto the fixed host slot
(UID 00000000000000000000000000000001). Before a session, SaveParty re-maps
the save so the current host's own character sits in the host slot and the
displaced character returns to its owner's guest slot. Covers player files,
the world character map, guild membership/admin/markers, and Dimensional Pal
Storage. Parsing goes through gvas_compat, which refuses to touch any save it
cannot reproduce byte-for-byte.

A registry file (characters.json in the cloud folder, identical to PalSync's
format - claims carry over) records which character UID belongs to which
player. The current host-slot owner is derived from the files themselves: the
registered player whose guest file is missing has their character in the host
slot; if no file is missing, initial_host_owner applies.

Approach follows xNul's palworld-host-save-fix and NFZ-441's PlM fork (MIT).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import gvas_compat as gc
from .backup import make_backup
from .profiles import Profile
from .util import SavePartyError, read_json, write_json_atomic

HOST_UID = "00000000000000000000000000000001"
REGISTRY_NAME = "characters.json"


class CharacterError(SavePartyError):
    pass


def hex_to_dashed(uid: str) -> str:
    uid = uid.lower()
    return f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}"


def dashed_to_hex(uid: str) -> str:
    return uid.replace("-", "").upper()


def _valid_uid(uid: str) -> bool:
    return len(uid) == 32 and all(c in "0123456789abcdefABCDEF" for c in uid)


def _uid_str(value) -> str:
    return str(value).lower()


def _uid_obj(dashed: str):
    return gc.UUID.from_str(dashed)


# ----------------------------------------------------------------------
# reading characters
# ----------------------------------------------------------------------


def _char_map(props: dict) -> list:
    try:
        return props["worldSaveData"]["value"]["CharacterSaveParameterMap"]["value"]
    except KeyError as exc:
        raise CharacterError(f"unexpected Level.sav layout (missing {exc})") from exc


def _guild_groups(props: dict) -> list:
    groups = props["worldSaveData"]["value"].get("GroupSaveDataMap", {}).get("value", [])
    return [
        g["value"]["RawData"]["value"]
        for g in groups
        if g["value"]["GroupType"]["value"]["value"] == "EPalGroupType::Guild"
    ]


def _save_parameter(entry: dict) -> dict:
    try:
        return entry["value"]["RawData"]["value"]["object"]["SaveParameter"]["value"]
    except (KeyError, TypeError):
        return {}


def _scalar(container: dict, key: str):
    value = container.get(key, {})
    while isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value


@dataclass
class CharInfo:
    uid: str  # hex32 upper
    instance_id: str  # dashed lowercase
    nickname: str
    level: int | None
    has_file: bool

    @property
    def is_host_slot(self) -> bool:
        return self.uid == HOST_UID


def list_characters(world_dir: Path) -> list[CharInfo]:
    level_path = world_dir / "Level.sav"
    if not level_path.is_file():
        raise CharacterError(
            f"no Level.sav in {world_dir} - for character features the profile must "
            "point at ONE world folder (the one with Level.sav inside)."
        )
    doc = gc.load_sav(level_path)
    players_dir = world_dir / "Players"
    out: list[CharInfo] = []
    for entry in _char_map(doc.properties):
        param = _save_parameter(entry)
        if not _scalar(param, "IsPlayer"):
            continue
        uid = dashed_to_hex(_uid_str(entry["key"]["PlayerUId"]["value"]))
        level_val = _scalar(param, "Level")
        out.append(
            CharInfo(
                uid=uid,
                instance_id=_uid_str(entry["key"]["InstanceId"]["value"]),
                nickname=str(_scalar(param, "NickName") or "(unnamed)"),
                level=int(level_val) if isinstance(level_val, (int, float)) else None,
                has_file=(players_dir / f"{uid}.sav").is_file(),
            )
        )
    out.sort(key=lambda c: (not c.is_host_slot, c.uid))
    return out


def world_name(world_dir: Path) -> str | None:
    """The human world name Palworld shows in its Continue menu, read from
    LevelMeta.sav (e.g. 'Chris and Luisa'). None if unavailable."""
    meta = world_dir / "LevelMeta.sav"
    if not meta.is_file():
        return None
    try:
        doc = gc.load_sav(meta)
        save_data = doc.properties.get("SaveData", {}).get("value", {})
        name = save_data.get("WorldName", {}).get("value")
        return str(name) if name else None
    except Exception:
        return None


def set_world_name(world_dir: Path, new_name: str) -> str:
    """Rename the world (the name Palworld shows in its Continue menu), stored in
    LevelMeta.sav → SaveData.WorldName. Returns the previous name. Only proceeds
    if LevelMeta.sav round-trips byte-for-byte first (safety gate)."""
    new_name = str(new_name).strip()
    if not new_name:
        raise CharacterError("world name cannot be empty")
    meta = world_dir / "LevelMeta.sav"
    if not meta.is_file():
        raise CharacterError(f"no LevelMeta.sav in {world_dir}")
    gc.roundtrip_check(meta)
    doc = gc.load_sav(meta)
    save_data = doc.properties.get("SaveData", {}).get("value", {})
    field = save_data.get("WorldName") if isinstance(save_data, dict) else None
    if not isinstance(field, dict) or "value" not in field:
        raise CharacterError("LevelMeta.sav has no WorldName field to rename")
    old = str(field["value"])
    field["value"] = new_name
    tmp = meta.with_name(meta.name + ".savepartytmp")
    gc.write_sav(doc, tmp)
    os.replace(tmp, meta)
    return old


@dataclass
class RemoveResult:
    uid: str
    nickname: str | None
    refs_removed: int
    files_removed: list[str] = field(default_factory=list)


def remove_character(world_dir: Path, target_uid: str) -> RemoveResult:
    """Delete one player character (by UID) from a world: its CharacterSave
    entry, guild membership/handles, and Players/<uid>.sav (+ _dps). Meant for
    cleaning up an abandoned or duplicate character. Refuses the host slot, and
    only proceeds if Level.sav round-trips byte-for-byte first (safety gate)."""
    target_uid = target_uid.upper()
    if not _valid_uid(target_uid):
        raise CharacterError(f"invalid character UID: {target_uid}")
    if target_uid == HOST_UID:
        raise CharacterError("refusing to remove the host-slot character")
    level_path = world_dir / "Level.sav"
    if not level_path.is_file():
        raise CharacterError(f"no Level.sav in {world_dir}")

    gc.roundtrip_check(level_path)  # only touch a save we reproduce exactly
    level = gc.load_sav(level_path)
    dashed = hex_to_dashed(target_uid)
    instance_id = nickname = None
    for entry in _char_map(level.properties):
        if dashed_to_hex(_uid_str(entry["key"]["PlayerUId"]["value"])) == target_uid:
            instance_id = _uid_str(entry["key"]["InstanceId"]["value"])
            nickname = str(_scalar(_save_parameter(entry), "NickName") or "(unnamed)")
            break
    if instance_id is None:
        raise CharacterError(f"no character with UID {target_uid} in this world")

    refs = _purge_character(level.properties, instance_id, dashed)
    tmp = level_path.with_name(level_path.name + ".savepartytmp")
    gc.write_sav(level, tmp)
    os.replace(tmp, level_path)

    result = RemoveResult(uid=target_uid, nickname=nickname, refs_removed=refs)
    players_dir = world_dir / "Players"
    for suffix in (".sav", "_dps.sav"):
        f = players_dir / f"{target_uid}{suffix}"
        if f.is_file():
            f.unlink()
            result.files_removed.append(f.name)
    return result


# ----------------------------------------------------------------------
# the swap / vacate operations
# ----------------------------------------------------------------------


def _player_save_data(doc: gc.SavDocument) -> dict:
    try:
        return doc.properties["SaveData"]["value"]
    except KeyError as exc:
        raise CharacterError(f"unexpected player save layout in {doc.path.name} ({exc})") from exc


def _player_instance(doc: gc.SavDocument) -> str:
    return _uid_str(_player_save_data(doc)["IndividualId"]["value"]["InstanceId"]["value"])


def _set_player_uid(doc: gc.SavDocument, dashed: str) -> None:
    data = _player_save_data(doc)
    data["PlayerUId"]["value"] = _uid_obj(dashed)
    data["IndividualId"]["value"]["PlayerUId"]["value"] = _uid_obj(dashed)


def _apply_uid_moves(props: dict, uid_moves: dict[str, str], instance_moves: dict[str, str]) -> int:
    """Simultaneously re-point characters. Each entry is compared once against
    its ORIGINAL value, so A->B plus C->A moves can never cascade."""
    touched = 0
    for entry in _char_map(props):
        inst = _uid_str(entry["key"]["InstanceId"]["value"])
        if inst in instance_moves:
            entry["key"]["PlayerUId"]["value"] = _uid_obj(instance_moves[inst])
            touched += 1
    for guild in _guild_groups(props):
        for handle in guild.get("individual_character_handle_ids", []):
            if _uid_str(handle.get("instance_id")) in instance_moves:
                handle["guid"] = _uid_obj(instance_moves[_uid_str(handle["instance_id"])])
                touched += 1
            elif _uid_str(handle.get("guid")) in uid_moves:
                handle["guid"] = _uid_obj(uid_moves[_uid_str(handle["guid"])])
                touched += 1
        admin = _uid_str(guild.get("admin_player_uid"))
        if admin in uid_moves:
            guild["admin_player_uid"] = _uid_obj(uid_moves[admin])
            touched += 1
        modifier = _uid_str(guild.get("name_modifier_uid"))
        if modifier in uid_moves:
            guild["name_modifier_uid"] = _uid_obj(uid_moves[modifier])
            touched += 1
        for marker in guild.get("guild_markers", []):
            cur = _uid_str(marker.get("owner_player_uid"))
            if cur in uid_moves:
                marker["owner_player_uid"] = _uid_obj(uid_moves[cur])
                touched += 1
        for member in guild.get("players", []):
            cur = _uid_str(member.get("player_uid"))
            if cur in uid_moves:
                member["player_uid"] = _uid_obj(uid_moves[cur])
                touched += 1
    touched += _relink_locker(props, uid_moves)
    return touched


def _relink_locker(props: dict, uid_moves: dict[str, str]) -> int:
    """Re-point Dimensional Pal Storage locker entries (Palworld 1.0+)."""
    arr = props["worldSaveData"]["value"].get("InLockerCharacterInstanceIDArray")
    if not isinstance(arr, dict):
        return 0
    values = arr.get("value")
    if isinstance(values, dict):
        values = values.get("values", [])
    if not isinstance(values, list):
        return 0
    touched = 0
    for entry in values:
        if not isinstance(entry, dict):
            continue
        holder = entry.get("PlayerUId")
        if isinstance(holder, dict) and "value" in holder:
            cur = _uid_str(holder["value"])
            if cur in uid_moves:
                holder["value"] = _uid_obj(uid_moves[cur])
                touched += 1
    return touched


def _purge_character(props: dict, instance_id: str, dashed_uid: str) -> int:
    """Remove a throwaway character's traces from Level.sav."""
    removed = 0
    char_map = _char_map(props)
    for i in range(len(char_map) - 1, -1, -1):
        if _uid_str(char_map[i]["key"]["InstanceId"]["value"]) == instance_id:
            del char_map[i]
            removed += 1
    for guild in _guild_groups(props):
        handles = guild.get("individual_character_handle_ids", [])
        for i in range(len(handles) - 1, -1, -1):
            if _uid_str(handles[i].get("instance_id")) == instance_id:
                del handles[i]
                removed += 1
        members = guild.get("players", [])
        for i in range(len(members) - 1, -1, -1):
            if _uid_str(members[i].get("player_uid")) == dashed_uid:
                del members[i]
                removed += 1
    return removed


def _move_dps_files(players_dir: Path, moves_hex: dict[str, str], messages: list[str]) -> None:
    """Rename per-player Dimensional Pal Storage files and fix owners inside."""
    plans = []
    for old_hex, new_hex in moves_hex.items():
        src = players_dir / f"{old_hex}_dps.sav"
        if src.is_file():
            plans.append((src, players_dir / f"{new_hex}_dps.sav", old_hex, new_hex))
    if not plans:
        return
    for src, _dst, _o, _n in plans:
        gc.roundtrip_check(src)
    staged = []
    for src, dst, old_hex, new_hex in plans:
        doc = gc.load_sav(src)
        old_dashed, new_dashed = hex_to_dashed(old_hex), hex_to_dashed(new_hex)
        count = 0
        try:
            values = doc.properties["SaveParameterArray"]["value"]
            if isinstance(values, dict):
                values = values.get("values", [])
            for entry in values if isinstance(values, list) else []:
                sp = entry.get("SaveParameter", {}).get("value", {})
                owner = sp.get("OwnerPlayerUId") if isinstance(sp, dict) else None
                if isinstance(owner, dict) and _uid_str(owner.get("value")) == old_dashed:
                    owner["value"] = _uid_obj(new_dashed)
                    count += 1
        except (KeyError, TypeError):
            pass
        tmp = src.with_name(src.name + ".savepartymove")
        gc.write_sav(doc, tmp)
        src.unlink()
        staged.append((tmp, dst))
        messages.append(f"pal storage: {src.name} -> {dst.name} ({count} owner refs updated)")
    for tmp, dst in staged:
        dst.unlink(missing_ok=True)  # only a stale throwaway's storage could be here
        os.replace(tmp, dst)


@dataclass
class SwapResult:
    incoming_uid: str
    displaced_uid: str
    discarded_nickname: str | None = None
    messages: list[str] = field(default_factory=list)


def _capture_throwaway(level, target_path: Path, participants: tuple[str, ...], target_dashed: str, result: SwapResult) -> None:
    """If a non-participant character sits at the target slot, purge its traces."""
    throwaway = gc.load_sav(target_path)
    throwaway_instance = _player_instance(throwaway)
    if throwaway_instance in participants:
        raise CharacterError(
            "target slot already holds a swap participant - the character registry "
            "looks wrong; open Characters to inspect."
        )
    for entry in _char_map(level.properties):
        if _uid_str(entry["key"]["InstanceId"]["value"]) == throwaway_instance:
            result.discarded_nickname = str(_scalar(_save_parameter(entry), "NickName") or "(unnamed)")
            break
    removed = _purge_character(level.properties, throwaway_instance, target_dashed)
    result.messages.append(
        f"discarded throwaway character '{result.discarded_nickname}' "
        f"({removed} world references removed)"
    )


def swap_host(world_dir: Path, incoming_uid: str, displaced_target_uid: str) -> SwapResult:
    """Move the character at incoming_uid into the host slot; the current host
    character moves to displaced_target_uid (its owner's guest slot)."""
    incoming_uid = incoming_uid.upper()
    displaced_target_uid = displaced_target_uid.upper()
    for uid in (incoming_uid, displaced_target_uid):
        if not _valid_uid(uid):
            raise CharacterError(f"invalid character UID: {uid}")
    if HOST_UID in (incoming_uid, displaced_target_uid):
        raise CharacterError("guest UIDs must differ from the host slot UID")
    if incoming_uid == displaced_target_uid:
        raise CharacterError("incoming and displaced-target UIDs must differ")

    players_dir = world_dir / "Players"
    level_path = world_dir / "Level.sav"
    host_path = players_dir / f"{HOST_UID}.sav"
    incoming_path = players_dir / f"{incoming_uid}.sav"
    target_path = players_dir / f"{displaced_target_uid}.sav"
    for path in (level_path, host_path, incoming_path):
        if not path.is_file():
            raise CharacterError(f"required save file missing: {path}")

    # Safety gates: byte-perfect round-trips before touching anything.
    gc.roundtrip_check(level_path)
    gc.roundtrip_check(host_path)
    gc.roundtrip_check(incoming_path)
    if target_path.is_file():
        gc.roundtrip_check(target_path)

    result = SwapResult(incoming_uid, displaced_target_uid)
    level = gc.load_sav(level_path)
    host_doc = gc.load_sav(host_path)
    incoming_doc = gc.load_sav(incoming_path)
    host_instance = _player_instance(host_doc)
    incoming_instance = _player_instance(incoming_doc)

    host_dashed = hex_to_dashed(HOST_UID)
    incoming_dashed = hex_to_dashed(incoming_uid)
    target_dashed = hex_to_dashed(displaced_target_uid)

    if target_path.is_file():
        _capture_throwaway(level, target_path, (host_instance, incoming_instance), target_dashed, result)

    touched = _apply_uid_moves(
        level.properties,
        {host_dashed: target_dashed, incoming_dashed: host_dashed},
        {host_instance: target_dashed, incoming_instance: host_dashed},
    )
    result.messages.append(f"re-pointed {touched} world references")
    _set_player_uid(host_doc, target_dashed)
    _set_player_uid(incoming_doc, host_dashed)

    gc.write_sav(level)
    gc.write_sav(host_doc, target_path)  # old host character -> its owner's slot
    gc.write_sav(incoming_doc, host_path)  # incoming character -> host slot
    incoming_path.unlink()
    _move_dps_files(players_dir, {HOST_UID: displaced_target_uid, incoming_uid: HOST_UID}, result.messages)

    check = gc.load_sav(level_path)
    seen = {
        _uid_str(e["key"]["InstanceId"]["value"]): dashed_to_hex(_uid_str(e["key"]["PlayerUId"]["value"]))
        for e in _char_map(check.properties)
    }
    if seen.get(incoming_instance) != HOST_UID or seen.get(host_instance) != displaced_target_uid:
        raise CharacterError(
            "post-swap verification failed - restore the 'before-charswap' backup "
            "from Backups before playing."
        )
    result.messages.append("post-swap verification passed")
    return result


def vacate_host(world_dir: Path, displaced_target_uid: str) -> SwapResult:
    """Move the host character to its owner's guest slot and leave the host
    slot EMPTY, so the next hosting session starts with character creation."""
    displaced_target_uid = displaced_target_uid.upper()
    if not _valid_uid(displaced_target_uid):
        raise CharacterError(f"invalid character UID: {displaced_target_uid}")
    if displaced_target_uid == HOST_UID:
        raise CharacterError("the displaced character cannot stay in the host slot")

    players_dir = world_dir / "Players"
    level_path = world_dir / "Level.sav"
    host_path = players_dir / f"{HOST_UID}.sav"
    target_path = players_dir / f"{displaced_target_uid}.sav"
    for path in (level_path, host_path):
        if not path.is_file():
            raise CharacterError(f"required save file missing: {path}")

    gc.roundtrip_check(level_path)
    gc.roundtrip_check(host_path)
    if target_path.is_file():
        gc.roundtrip_check(target_path)

    result = SwapResult(incoming_uid="", displaced_uid=displaced_target_uid)
    level = gc.load_sav(level_path)
    host_doc = gc.load_sav(host_path)
    host_instance = _player_instance(host_doc)
    host_dashed = hex_to_dashed(HOST_UID)
    target_dashed = hex_to_dashed(displaced_target_uid)

    if target_path.is_file():
        _capture_throwaway(level, target_path, (host_instance,), target_dashed, result)

    touched = _apply_uid_moves(
        level.properties, {host_dashed: target_dashed}, {host_instance: target_dashed}
    )
    result.messages.append(f"re-pointed {touched} world references")
    _set_player_uid(host_doc, target_dashed)

    gc.write_sav(level)
    gc.write_sav(host_doc, target_path)
    host_path.unlink()
    _move_dps_files(players_dir, {HOST_UID: displaced_target_uid}, result.messages)

    check = gc.load_sav(level_path)
    seen = {
        _uid_str(e["key"]["InstanceId"]["value"]): dashed_to_hex(_uid_str(e["key"]["PlayerUId"]["value"]))
        for e in _char_map(check.properties)
    }
    if seen.get(host_instance) != displaced_target_uid or host_path.is_file():
        raise CharacterError(
            "post-vacate verification failed - restore the 'before-charswap' backup "
            "from Backups before playing."
        )
    result.messages.append("host slot is now empty - the game will offer character creation")
    return result


# ----------------------------------------------------------------------
# registry (characters.json - identical to PalSync's, claims carry over)
# ----------------------------------------------------------------------


def registry_path(cloud_dir: Path) -> Path:
    return Path(cloud_dir) / REGISTRY_NAME


def read_registry(cloud_dir: Path) -> dict | None:
    try:
        data = read_json(registry_path(cloud_dir), retries=1, delay=2.0)
    except Exception:
        return None
    if not data or not isinstance(data.get("players"), dict):
        return None
    return data


def write_registry(cloud_dir: Path, registry: dict) -> None:
    registry.setdefault("format", 1)
    write_json_atomic(registry_path(cloud_dir), registry)


def resolve_host_owner(registry: dict, world_dir: Path) -> str | None:
    """Which registered player's character occupies the host slot right now?

    PRIMARY signal - the host-slot character's in-game NICKNAME. The nickname
    travels with the character when it is swapped into the host slot, so it
    directly names who is hosting (host nick 'Loldarkqueen' → Luisa). This is
    far more reliable than inferring it from which guest file is missing, which
    Palworld's unpredictable file (re)creation and cross-account duplicates kept
    breaking. FALLBACK to the guest-file heuristic only when the nickname can't
    be matched to exactly one registered player (unclaimed char in the slot, a
    renamed character, or a nickname collision)."""
    players = registry.get("players", {})

    # Primary: match the host-slot character's nickname to a registered player.
    try:
        host = next((c for c in list_characters(world_dir) if c.is_host_slot), None)
    except Exception:
        host = None
    if host is not None and host.nickname:
        want = host.nickname.casefold()
        by_nick = [
            name for name, info in players.items()
            if (info.get("nickname") or "").casefold() == want
        ]
        if len(by_nick) == 1:
            return by_nick[0]

    # Fallback: the player whose guest .sav is missing is the one in the host slot.
    players_dir = world_dir / "Players"
    missing = [
        name
        for name, info in players.items()
        if info.get("uid") and not (players_dir / f"{info['uid'].upper()}.sav").is_file()
    ]
    if len(missing) == 1:
        return missing[0]
    if not missing:
        return registry.get("initial_host_owner")
    return None  # more than one missing file: state needs human eyes


# ----------------------------------------------------------------------
# session hook (called by the engine before launching a Palworld session)
# ----------------------------------------------------------------------


def _heal_duplicate_host(world_dir: Path, registry: dict, profile: Profile, events) -> None:
    """Palworld sometimes leaves a character in BOTH the host slot and its own
    guest slot when a different Steam account hosts it. That duplicate makes it
    impossible to tell who is hosting, which jams the swap. Detect it - the
    host-slot character's nickname matches a claimed player whose guest file
    ALSO still exists - and delete the stale guest copy (keeping the host-slot
    one, which has the latest progress), so the world is consistent again."""
    try:
        chars = list_characters(world_dir)
    except Exception:
        return
    host = next((c for c in chars if c.is_host_slot), None)
    if host is None or not host.nickname:
        return
    char_uids = {c.uid.upper() for c in chars}  # PlayerUIds that have a real character
    # Duplicate signature: the host-slot character's nickname belongs to a claimed
    # player whose guest .sav still exists, yet that player has NO character in a
    # guest slot (their character IS the host-slot one). That guest .sav is a stale
    # orphan Palworld left behind - deleting it makes host detection work again.
    for info in registry.get("players", {}).values():
        uid = (info.get("uid") or "").upper()
        if not uid or info.get("nickname") != host.nickname:
            continue
        guest_file = world_dir / "Players" / f"{uid}.sav"
        if guest_file.is_file() and uid not in char_uids:
            try:
                make_backup(world_dir, profile.backups_path(), "before-dedup",
                            profile.exclude_dir_set(), profile.exclude_globs, profile.backup_keep)
                guest_file.unlink()
                dps = world_dir / "Players" / f"{uid}_dps.sav"
                if dps.is_file():
                    dps.unlink()
                events.log(
                    f"Auto-fixed a stale duplicate of '{host.nickname}' that Palworld "
                    "left behind.", "dim",
                )
            except Exception as exc:
                events.log(f"Could not auto-fix a duplicate ({exc}).", "warn")
            return


def pre_session(profile: Profile, player: str, events) -> None:
    """Put this player's own character into the host slot before hosting.

    No-ops quietly until the group has a character registry (Characters →
    claim). Never blocks the session: on failure the player can still play,
    at worst as the currently-hosted character.
    """
    world = Path(profile.save_dir)
    registry = read_registry(Path(profile.cloud_dir))
    if registry is None:
        return
    _heal_duplicate_host(world, registry, profile, events)  # auto-repair Palworld duplicates
    owner = resolve_host_owner(registry, world)
    if owner == player:
        events.log("Host slot already holds your character.", "dim")
        return
    me = registry.get("players", {}).get(player)
    owner_entry = registry.get("players", {}).get(owner or "", {})
    my_uid = (me or {}).get("uid")

    def backup() -> None:
        path = make_backup(
            world,
            profile.backups_path(),
            "before-charswap",
            profile.exclude_dir_set(),
            profile.exclude_globs,
            profile.backup_keep,
        )
        if path:
            events.log(f"World backed up to {path.name}", "dim")

    if not my_uid:
        # No character of ours anywhere yet: offer to free the host slot so
        # this session starts with character creation.
        if owner is None or not owner_entry.get("uid"):
            events.log("Character swap: no character claimed for you yet - open Characters.", "dim")
            return
        if not events.confirm(
            f"You have no Palworld character of your own in this world yet. Move "
            f"{owner}'s character back to their slot and CREATE YOURS this session?",
            default=True,
        ):
            events.log(f"Okay - this session you play {owner}'s character.", "info")
            return
        try:
            backup()
            result = vacate_host(world, owner_entry["uid"])
            for message in result.messages:
                events.log(message, "dim")
            registry.setdefault("players", {}).setdefault(player, {"uid": None})
            registry["initial_host_owner"] = player
            write_registry(Path(profile.cloud_dir), registry)
            events.log(
                "The host slot is yours - Palworld will show CHARACTER CREATION; that new "
                "character is YOU from now on. Later, join a friend's session once and "
                "claim the new slot in Characters.",
                "success",
            )
        except SavePartyError as exc:
            events.log(
                f"Could not free the host slot ({exc}). You can still play; restore the "
                "before-charswap backup if the world looks wrong.",
                "warn",
            )
        return

    if owner is None:
        events.log("Character swap skipped: cannot tell whose character is hosting - open Characters.", "warn")
        return
    if not owner_entry.get("uid"):
        events.log(f"Character swap skipped: {owner} has not claimed a slot yet.", "warn")
        return
    events.log(f"Character swap: your character takes the host slot; {owner}'s returns to their slot.", "info")
    try:
        backup()
        result = swap_host(world, my_uid, owner_entry["uid"])
        for message in result.messages:
            events.log(message, "dim")
        events.log("You will play as your own character this session.", "success")
    except SavePartyError as exc:
        events.log(
            f"Character swap not applied ({exc}). You can still play - restore the "
            "before-charswap backup if the world looks wrong.",
            "warn",
        )


def hosting_conflict(profile: Profile, player: str) -> str | None:
    """Safeguard: if launching now would host as someone ELSE's character (the
    pre-session swap could not place this player onto their own character),
    return a human-readable reason. Returns None when it's safe to play.

    The tell is simple and reliable: after a correct swap the player's own guest
    file is GONE (their character now sits in the host slot). If that guest file
    still exists, their character is parked in a guest slot and whoever is in the
    host slot is who they'd actually play - so we block."""
    registry = read_registry(Path(profile.cloud_dir))
    me = (registry or {}).get("players", {}).get(player) if registry else None
    my_uid = (me or {}).get("uid")
    if not my_uid:
        return None  # no character claimed yet - the game will offer creation, that's fine
    world = Path(profile.save_dir)
    if not (world / "Players" / f"{my_uid.upper()}.sav").is_file():
        return None  # guest file gone -> your character IS in the host slot. Good to go.
    # Your character is still in a guest slot; identify who's in the host slot.
    try:
        host = next((c for c in list_characters(world) if c.is_host_slot), None)
        who = host.nickname if host and host.nickname else "someone else"
    except Exception:
        who = "someone else"
    my_nick = me.get("nickname") or "your character"
    return (
        f"Not starting - you'd play as '{who}', not {my_nick}. The shared world is "
        f"out of sync on this PC (a duplicate or a stale copy). Make sure Syncthing shows "
        f"'Up to Date', open Characters to confirm everyone is claimed, then press Play again."
    )
