"""Read the name and GUID out of TSU content files (.lvl tracks, .veh cars).

Why not tsu_veh
---------------
tsu_veh parses a whole vehicle, which means it also has to understand every
physics chunk — and it raises on files it does not fully understand (a modded
car with an unknown mask bit, an old FormatVersion). The panel only needs the
two fields the game itself keys on, and it needs them for *every* file an
admin uploads, including the ones tsu_veh rejects. So this reads the header
and stops at the name. It also covers .lvl, which tsu_veh does not know.

Format (little-endian, both file types are ZIPs holding `properties.bin`):

  0x00  u16  FormatId          111 = vehicle, 123 = level
  0x02  u16  FormatVersion
  0x04  u16  gameVersion
  0x06  u64  guid.a
  0x0E  u64  guid.b
  0x16  i64  creationTime      (.NET ticks)
  ----- vehicles only -----
  0x1E  u32  makerAccountId
  0x22  5 x u8  flags
  0x27  u8   header bitmask, followed by one byte per set bit
  ----- levels only -----
  0x1E  u8   unknown single byte
  -----
        u32 length + UTF-8 name
        u32 length + UTF-8 description

In a vehicle the description follows the name directly, which makes a good
cross-check: a handful of vehicles (all with an empty header bitmask) carry
one extra byte before the name, so the offset is searched over a small window
and only accepted when the description behind it parses too. A level puts
other fields between the two, so its name is read at the fixed offset. The
GUID never moves either way. Levels older than FormatVersion 3 use a
different header altogether and are rejected rather than guessed at.

The GUID the game shows ("1glbwm7c2w43-3gq3e3s") is the two u64s in base32
over an alphabet without the ambiguous letters i, o, u and y.
"""
from __future__ import annotations

import struct
import zipfile

VEHICLE_FORMAT_ID = 111
LEVEL_FORMAT_ID = 123
MIN_LEVEL_VERSION = 3
GUID_ALPHABET = "0123456789abcdefghjklmnpqrstvwxz"
# How far past the computed offset the name may sit (see the module docstring).
NAME_SEARCH_BYTES = 4
MAX_NAME_LEN = 120
MAX_DESCRIPTION_LEN = 8192


class ContentFileError(Exception):
    """The file is not a TSU content file we can read a name and GUID from."""


def _b32(value: int) -> str:
    if value == 0:
        return "0"
    out = ""
    while value:
        out = GUID_ALPHABET[value & 31] + out
        value >>= 5
    return out


def _string(data: bytes, offset: int, limit: int) -> tuple[str, int] | None:
    """(text, offset after it) for a u32-prefixed UTF-8 string, else None."""
    if offset + 4 > len(data):
        return None
    (length,) = struct.unpack_from("<I", data, offset)
    if not 0 <= length <= limit or offset + 4 + length > len(data):
        return None
    try:
        return data[offset + 4:offset + 4 + length].decode("utf-8"), \
            offset + 4 + length
    except UnicodeDecodeError:
        return None


def _plausible_name(found) -> str | None:
    if not found:
        return None
    name, _after = found
    return name if name.strip() and name.isprintable() else None


def _level_name(data: bytes, offset: int) -> str:
    name = _plausible_name(_string(data, offset, MAX_NAME_LEN))
    if name is None:
        raise ContentFileError(f"no readable name at offset {offset}")
    return name


def _vehicle_name(data: bytes, offset: int) -> str:
    """The name at (or just past) `offset`, verified by the description."""
    for start in range(offset, offset + NAME_SEARCH_BYTES + 1):
        found = _string(data, start, MAX_NAME_LEN)
        name = _plausible_name(found)
        # A string that merely looks like a name is followed by junk, not by
        # the description.
        if name and _string(data, found[1], MAX_DESCRIPTION_LEN) is not None:
            return name
    raise ContentFileError(f"no readable name near offset {offset}")


def parse_properties(data: bytes) -> dict:
    """{'kind', 'name', 'guid'} from a properties.bin."""
    if len(data) < 32:
        raise ContentFileError("properties.bin is too short")
    fmt, ver, _game = struct.unpack_from("<HHH", data, 0)
    guid_a, guid_b = struct.unpack_from("<QQ", data, 6)
    if fmt == LEVEL_FORMAT_ID:
        if ver < MIN_LEVEL_VERSION:
            raise ContentFileError(
                f"level FormatVersion {ver} uses an older header")
        kind, name = "track", _level_name(data, 0x1E + 1)
    elif fmt == VEHICLE_FORMAT_ID:
        offset = 0x27
        offset += 1 + bin(data[offset]).count("1")
        kind, name = "vehicle", _vehicle_name(data, offset)
    else:
        raise ContentFileError(f"unknown FormatId {fmt}")
    return {"kind": kind, "name": name,
            "guid": f"{_b32(guid_a)}-{_b32(guid_b)}"}


def read_content_file(path: str) -> dict:
    """{'kind', 'name', 'guid'} for a .lvl or .veh on disk."""
    try:
        with zipfile.ZipFile(path) as archive:
            data = archive.read("properties.bin")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ContentFileError(f"not a readable TSU file: {exc}") from exc
    return parse_properties(data)
