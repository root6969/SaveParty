"""Reading/writing current-generation Palworld saves (PlZ zlib and PlM Oodle).

Palworld 0.6+/1.0 saves changed in two ways that break the released
palworld-save-tools parsers: the container compression became Oodle ("PlM"
magic, handled here via pyooz), and several embedded blobs grew new fields.

This module parses only what SaveParty's Palworld tooling needs - the
character map and the guild map - with all unknown/new data preserved as
opaque bytes. Every other custom section of the save stays raw, untouched.

Safety property: `roundtrip_check()` re-encodes a freshly parsed save and
compares it byte-for-byte against the original. SaveParty refuses to modify a
save that does not round-trip exactly, so a future format change degrades to
a clean "character features unavailable" instead of a corrupted world.

Blob layouts adapted from palworld-save-tools (MIT, (c) Jun Siang Cheah).
"""

from __future__ import annotations

import contextlib
import copy
import io
import logging
import os
import zlib
from dataclasses import dataclass
from pathlib import Path

from .util import SavePartyError

log = logging.getLogger("saveparty")

try:
    from palworld_save_tools.archive import (
        UUID,
        FArchiveReader,
        FArchiveWriter,
        instance_id_reader,
        instance_id_writer,
        uuid_reader,
        uuid_writer,
    )
    from palworld_save_tools.gvas import GvasHeader
    from palworld_save_tools.paltypes import PALWORLD_TYPE_HINTS

    # A post-1.0 Palworld update added Int64Property *map values* (e.g.
    # worldSaveData.LevelObjectRecoverPartySaveData.PlayerLastUsedTimes) that the
    # released palworld_save_tools (0.24.0) cannot decode/encode, which broke the
    # character swap ("could not parse Level.sav ... Int64Property"). Teach both
    # directions until the library ships support. Idempotent.
    if not getattr(FArchiveReader, "_sp_int64_patch", False):
        _sp_r_pv = FArchiveReader.prop_value

        def _sp_read_prop_value(self, type_name, struct_type_name, path, _orig=_sp_r_pv):
            if type_name == "Int64Property":
                return self.i64()
            return _orig(self, type_name, struct_type_name, path)

        FArchiveReader.prop_value = _sp_read_prop_value
        _sp_w_pv = FArchiveWriter.prop_value

        def _sp_write_prop_value(self, type_name, struct_type_name, value, _orig=_sp_w_pv):
            if type_name == "Int64Property":
                return self.i64(value)
            return _orig(self, type_name, struct_type_name, value)

        FArchiveWriter.prop_value = _sp_write_prop_value
        FArchiveReader._sp_int64_patch = True

    HAVE_SAVE_TOOLS = True
except ImportError:  # pragma: no cover - optional feature dependency
    HAVE_SAVE_TOOLS = False
    UUID = FArchiveReader = FArchiveWriter = GvasHeader = None
    instance_id_reader = instance_id_writer = uuid_reader = uuid_writer = None
    PALWORLD_TYPE_HINTS = None

try:
    import ooz

    HAVE_OOZ = True
except ImportError:  # pragma: no cover - optional feature dependency
    HAVE_OOZ = False


class SaveFormatError(SavePartyError):
    pass


def _require_deps() -> None:
    missing = []
    if not HAVE_SAVE_TOOLS:
        missing.append("palworld-save-tools")
    if not HAVE_OOZ:
        missing.append("pyooz")
    if missing:
        raise SaveFormatError(
            f"missing package(s): {', '.join(missing)} - reinstall SaveParty "
            "requirements (pip install -r requirements.txt)."
        )


# ----------------------------------------------------------------------
# container (de)compression
# ----------------------------------------------------------------------


def _decompress(data: bytes) -> tuple[bytes, int, bytes]:
    """Return (gvas_bytes, save_type, magic) for a .sav container."""
    if len(data) < 12:
        raise SaveFormatError("save file is too small to be valid")
    uncompressed_len = int.from_bytes(data[0:4], "little")
    compressed_len = int.from_bytes(data[4:8], "little")
    magic = data[8:11]
    save_type = data[11]
    offset = 12
    if magic == b"CNK":  # Xbox chunked container wraps a normal one
        uncompressed_len = int.from_bytes(data[12:16], "little")
        compressed_len = int.from_bytes(data[16:20], "little")
        magic = data[20:23]
        save_type = data[23]
        offset = 24
    if magic == b"PlZ":
        raw = zlib.decompress(data[offset:])
        if save_type == 0x32:
            raw = zlib.decompress(raw)
        elif save_type != 0x31:
            raise SaveFormatError(f"unknown PlZ save type 0x{save_type:02X}")
    elif magic == b"PlM":
        raw = ooz.decompress(data[offset : offset + compressed_len], uncompressed_len)
        if raw is None or len(raw) != uncompressed_len:
            raise SaveFormatError("Oodle decompression produced the wrong length")
        raw = bytes(raw)
    else:
        raise SaveFormatError(f"unknown save container magic {magic!r}")
    if len(raw) != uncompressed_len:
        raise SaveFormatError("decompressed length does not match the header")
    return raw, save_type, magic


def _compress(raw: bytes, save_type: int, magic: bytes) -> bytes:
    # Always write the zlib "PlZ" container: every Palworld version reads it
    # (established by the community converter tools), and the game rewrites the
    # file in its native format on its next save. Available pyooz builds are
    # decompress-only, so PlM input round-trips out as PlZ by design.
    if magic not in (b"PlZ", b"PlM"):
        raise SaveFormatError(f"cannot write container magic {magic!r}")
    payload = zlib.compress(raw)
    compressed_len = len(payload)
    if save_type == 0x32:
        payload = zlib.compress(payload)
    return (
        len(raw).to_bytes(4, "little")
        + compressed_len.to_bytes(4, "little")
        + b"PlZ"
        + bytes([save_type])
        + payload
    )


# ----------------------------------------------------------------------
# custom blob parsers with unknown-tail preservation
# ----------------------------------------------------------------------


def _character_decode(reader, type_name, size, path):
    if type_name != "ArrayProperty":
        raise SaveFormatError(f"expected ArrayProperty at {path}, got {type_name}")
    value = reader.property(type_name, size, path, nested_caller_path=path)
    blob = bytes(value["value"]["values"])
    sub = reader.internal_copy(blob, debug=False)
    parsed = {"object": sub.properties_until_end()}
    parsed["trailing_bytes"] = blob[sub.data.tell() :]
    value["value"] = parsed
    return value


def _character_encode(writer, property_type, properties):
    if property_type != "ArrayProperty":
        raise SaveFormatError(f"expected ArrayProperty, got {property_type}")
    del properties["custom_type"]
    sub = FArchiveWriter()
    sub.properties(properties["value"]["object"])
    encoded = sub.bytes() + bytes(properties["value"]["trailing_bytes"])
    properties["value"] = {"values": [b for b in encoded]}
    return writer.property_inner(property_type, properties)


def _read_player_entry(reader, with_role: bool) -> dict:
    entry = {
        "player_uid": reader.guid(),
        "last_online": reader.i64(),
        "player_name": reader.fstring(),
    }
    if with_role:
        entry["role"] = reader.byte()
    return entry


def _write_player_entry(writer, entry: dict, with_role: bool) -> None:
    writer.guid(entry["player_uid"])
    writer.i64(entry["last_online"])
    writer.fstring(entry["player_name"])
    if with_role:
        writer.byte(entry["role"])


def _read_marker(reader) -> dict:
    return {
        "marker_id": reader.guid(),
        "icon_raw": bytes(reader.byte_list(24)),  # location vector, kept opaque
        "icon_type": reader.i32(),
        "owner_player_uid": reader.guid(),
    }


def _write_marker(writer, marker: dict) -> None:
    writer.guid(marker["marker_id"])
    writer.write(bytes(marker["icon_raw"]))
    writer.i32(marker["icon_type"])
    writer.guid(marker["owner_player_uid"])


def _read_role_permission(reader) -> dict:
    return {
        "role": reader.byte(),
        "permissions": reader.tarray(lambda r: r.byte()),
    }


def _write_role_permission(writer, entry: dict) -> None:
    writer.byte(entry["role"])
    writer.tarray(lambda w, v: w.byte(v), entry["permissions"])


def _read_guild_tail(sub) -> dict:
    """The guild blob tail has two layouts (a 2026-07 update added chest/role
    data) with no version flag; the one that lands exactly on EOF wins."""
    start = sub.data.tell()
    try:
        tail = {
            "chest_roles": sub.tarray(lambda r: r.byte()),
            "tail_i32": sub.i32(),
            "admin_player_uid": sub.guid(),
            "players": sub.tarray(lambda r: _read_player_entry(r, True)),
            "role_permissions": sub.tarray(_read_role_permission),
            "tail_bytes": bytes(sub.byte_list(4)),
            "tail_version": 2,
        }
        if sub.eof():
            return tail
    except Exception:
        pass  # not v2; try the pre-update layout
    sub.data.seek(start)
    return {
        "admin_player_uid": sub.guid(),
        "players": sub.tarray(lambda r: _read_player_entry(r, False)),
        "tail_bytes": bytes(sub.byte_list(4)),
        "tail_version": 1,
    }


def _group_decode(reader, type_name, size, path):
    if type_name != "MapProperty":
        raise SaveFormatError(f"expected MapProperty at {path}, got {type_name}")
    value = reader.property(type_name, size, path, nested_caller_path=path)
    for group in value["value"]:
        group_type = group["value"]["GroupType"]["value"]["value"]
        blob = bytes(group["value"]["RawData"]["value"]["values"])
        group["value"]["RawData"]["value"] = _group_decode_bytes(reader, blob, group_type)
    return value


def _group_decode_bytes(parent_reader, blob: bytes, group_type: str) -> dict:
    sub = parent_reader.internal_copy(blob, debug=False)
    data = {
        "group_type": group_type,
        "group_id": sub.guid(),
        "group_name": sub.fstring(),
        "individual_character_handle_ids": sub.tarray(instance_id_reader),
    }
    if group_type in (
        "EPalGroupType::Guild",
        "EPalGroupType::IndependentGuild",
        "EPalGroupType::Organization",
    ):
        data["org_type"] = sub.byte()
    if group_type == "EPalGroupType::Guild":
        data["guild_leading"] = bytes(sub.byte_list(4))
        data["base_ids"] = sub.tarray(uuid_reader)
        data["guild_i32_1"] = sub.i32()
        data["base_camp_level"] = sub.i32()
        data["base_points"] = sub.tarray(uuid_reader)
        data["guild_name"] = sub.fstring()
        data["name_modifier_uid"] = sub.guid()
        data["guild_markers"] = sub.tarray(_read_marker)
        data.update(_read_guild_tail(sub))
        if not sub.eof():
            raise SaveFormatError("guild blob has unconsumed bytes")
    elif group_type == "EPalGroupType::IndependentGuild":
        data["base_camp_level"] = sub.i32()
        data["base_points"] = sub.tarray(uuid_reader)
        data["guild_name"] = sub.fstring()
        data["player_uid"] = sub.guid()
        data["guild_name_2"] = sub.fstring()
        data["player_info"] = {
            "last_online": sub.i64(),
            "player_name": sub.fstring(),
        }
        data["trailing_bytes"] = blob[sub.data.tell() :]
    else:
        # Organization carries 12 opaque bytes; unknown types keep their tail.
        data["trailing_bytes"] = blob[sub.data.tell() :]
    return data


def _group_encode(writer, property_type, properties):
    if property_type != "MapProperty":
        raise SaveFormatError(f"expected MapProperty, got {property_type}")
    del properties["custom_type"]
    for group in properties["value"]:
        raw = group["value"]["RawData"]["value"]
        if "values" in raw:
            continue
        group["value"]["RawData"]["value"] = {
            "values": [b for b in _group_encode_bytes(raw)]
        }
    return writer.property_inner(property_type, properties)


def _group_encode_bytes(p: dict) -> bytes:
    sub = FArchiveWriter()
    sub.guid(p["group_id"])
    sub.fstring(p["group_name"])
    sub.tarray(instance_id_writer, p["individual_character_handle_ids"])
    if p["group_type"] in (
        "EPalGroupType::Guild",
        "EPalGroupType::IndependentGuild",
        "EPalGroupType::Organization",
    ):
        sub.byte(p["org_type"])
    if p["group_type"] == "EPalGroupType::Guild":
        sub.write(bytes(p["guild_leading"]))
        sub.tarray(uuid_writer, p["base_ids"])
        sub.i32(p["guild_i32_1"])
        sub.i32(p["base_camp_level"])
        sub.tarray(uuid_writer, p["base_points"])
        sub.fstring(p["guild_name"])
        sub.guid(p["name_modifier_uid"])
        sub.tarray(_write_marker, p["guild_markers"])
        if p["tail_version"] == 2:
            sub.tarray(lambda w, v: w.byte(v), p["chest_roles"])
            sub.i32(p["tail_i32"])
            sub.guid(p["admin_player_uid"])
            sub.tarray(lambda w, v: _write_player_entry(w, v, True), p["players"])
            sub.tarray(_write_role_permission, p["role_permissions"])
        else:
            sub.guid(p["admin_player_uid"])
            sub.tarray(lambda w, v: _write_player_entry(w, v, False), p["players"])
        sub.write(bytes(p["tail_bytes"]))
    elif p["group_type"] == "EPalGroupType::IndependentGuild":
        sub.i32(p["base_camp_level"])
        sub.tarray(uuid_writer, p["base_points"])
        sub.fstring(p["guild_name"])
        sub.guid(p["player_uid"])
        sub.fstring(p["guild_name_2"])
        sub.i64(p["player_info"]["last_online"])
        sub.fstring(p["player_info"]["player_name"])
        sub.write(bytes(p["trailing_bytes"]))
    else:
        sub.write(bytes(p["trailing_bytes"]))
    return sub.bytes()


CUSTOM_PROPERTIES = {
    ".worldSaveData.CharacterSaveParameterMap.Value.RawData": (
        _character_decode,
        _character_encode,
    ),
    ".worldSaveData.GroupSaveDataMap": (_group_decode, _group_encode),
}

# Map-key/value type hints the released library lacks for current saves.
# These are format facts (path -> UE property type), not code.
_EXTRA_TYPE_HINTS = {
    ".worldSaveData.GuildExtraSaveDataMap.Key": "Guid",
    ".worldSaveData.GuildExtraSaveDataMap.Value": "StructProperty",
    ".worldSaveData.EnemyCampSaveData.EnemyCampStatusMap.Value.TreasureBoxInfoMapBySpawnerName.Value": "StructProperty",
    ".worldSaveData.DungeonSaveData.DungeonSaveData.RewardSaveDataMap.Key": "Guid",
    ".worldSaveData.DungeonSaveData.DungeonSaveData.RewardSaveDataMap.Value": "StructProperty",
    ".worldSaveData.InvaderDeclarationSaveData.ValidatedStartPointIds.StructProperty": "Guid",
    ".SaveData.Local_MaxFriendshipPalIds.Key": "StructProperty",
    ".SaveData.Local_MaxFriendshipPalIds.Value": "StructProperty",
}


def _type_hints() -> dict:
    return {**PALWORLD_TYPE_HINTS, **_EXTRA_TYPE_HINTS}


@contextlib.contextmanager
def _quiet_parser():
    """The reader print()s guesses for unknown paths; capture them into the log."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield
    captured = buffer.getvalue().strip()
    if captured:
        for line in captured.splitlines():
            log.debug("save parser: %s", line)


# ----------------------------------------------------------------------
# documents
# ----------------------------------------------------------------------


@dataclass
class SavDocument:
    path: Path
    header: object
    properties: dict
    file_trailer: bytes
    save_type: int
    magic: bytes


def load_sav(path: Path) -> SavDocument:
    _require_deps()
    data = path.read_bytes()
    try:
        raw, save_type, magic = _decompress(data)
    except SaveFormatError:
        raise
    except Exception as exc:
        raise SaveFormatError(f"could not decompress {path.name}: {exc}") from exc
    try:
        with _quiet_parser():
            reader = FArchiveReader(raw, _type_hints(), CUSTOM_PROPERTIES, allow_nan=True)
            header = GvasHeader.read(reader)
            properties = reader.properties_until_end()
        trailer = raw[reader.data.tell() :]
    except Exception as exc:
        raise SaveFormatError(
            f"could not parse {path.name} ({exc}). A Palworld update may have changed "
            "the save format - character features are disabled until SaveParty updates; "
            "save syncing itself is unaffected."
        ) from exc
    return SavDocument(
        path=path,
        header=header,
        properties=properties,
        file_trailer=trailer,
        save_type=save_type,
        magic=magic,
    )


def encode_document(doc: SavDocument) -> bytes:
    """Serialize a document back to .sav container bytes (consumes the tree)."""
    with _quiet_parser():
        writer = FArchiveWriter(CUSTOM_PROPERTIES)
        doc.header.write(writer)
        writer.properties(doc.properties)
        raw = writer.bytes()
    return _compress(raw + doc.file_trailer, doc.save_type, doc.magic)


def write_sav(doc: SavDocument, path: Path | None = None) -> None:
    target = path or doc.path
    blob = encode_document(doc)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_bytes(blob)
    os.replace(tmp, target)


def roundtrip_check(path: Path) -> None:
    """Prove parse->encode reproduces the save byte-for-byte, or refuse surgery."""
    _require_deps()
    data = path.read_bytes()
    raw, _save_type, _magic = _decompress(data)
    doc = load_sav(path)
    doc_copy = copy.deepcopy(doc)
    with _quiet_parser():
        writer = FArchiveWriter(CUSTOM_PROPERTIES)
        doc_copy.header.write(writer)
        writer.properties(doc_copy.properties)
        rebuilt = writer.bytes() + doc_copy.file_trailer
    if rebuilt != raw:
        raise SaveFormatError(
            f"{path.name} does not survive a byte-identical round-trip "
            f"({len(rebuilt)} vs {len(raw)} bytes) - refusing to modify it. "
            "This usually means a Palworld update changed the save format."
        )
