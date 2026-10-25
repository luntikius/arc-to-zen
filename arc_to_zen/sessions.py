"""The two SNSS command dialects are distinct: Session_ and Tabs_ (restore).

Reference: Chromium components/sessions/core/{session_service_commands,
serialized_navigation_entry,tab_restore_service_impl}.cc.
PageState (POST bodies, form state, engine-specific objects) is never imported.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .common import MigrationError, compatible_url


class Pickle:
    def __init__(self, payload):
        if len(payload) < 4 or struct.unpack_from("<I", payload)[0] != len(payload) - 4:
            raise MigrationError("Invalid SNSS Pickle header.")
        self.data = payload
        self.pos = 4

    def integer(self):
        if self.pos + 4 > len(self.data):
            raise MigrationError("Truncated SNSS Pickle.")
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def string(self, utf16=False):
        length = self.integer()
        size = length * (2 if utf16 else 1)
        if length < 0 or self.pos + size > len(self.data):
            raise MigrationError("Invalid SNSS string length.")
        value = self.data[self.pos:self.pos + size]
        self.pos += (size + 3) & ~3
        try:
            return value.decode("utf-16-le" if utf16 else "utf-8")
        except UnicodeError as exc:
            raise MigrationError("Invalid SNSS string encoding.") from exc


@dataclass
class Tab:
    entries: dict = field(default_factory=dict)
    selected: int = 0
    window: int = 0
    position: int = 0

    def state(self):
        keys = sorted(self.entries)
        if not keys:
            return None
        # The selected index can refer outside the persisted navigation range.
        index = min(range(len(keys)), key=lambda i: abs(keys[i] - self.selected))
        return {"entries": [self.entries[k] for k in keys], "index": index + 1}


def commands(path):
    data = path.read_bytes()
    if len(data) < 8 or data[:4] != b"SNSS" or struct.unpack_from("<I", data, 4)[0] not in (1, 3):
        raise MigrationError("Unsupported Chromium session format.")
    offset = 8
    while offset < len(data):
        if offset + 2 > len(data):
            raise MigrationError("Truncated Chromium session record.")
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if size < 1 or offset + size > len(data):
            raise MigrationError("Truncated Chromium session payload.")
        yield data[offset], data[offset + 1:offset + size]
        offset += size


def parse(path: Path, restore=False):
    tabs = {}
    for cmd, payload in commands(path):
        if cmd == (1 if restore else 6):
            p = Pickle(payload)
            tabid, index = p.integer(), p.integer()
            url, title = p.string(), p.string(utf16=True)
            if compatible_url(url):
                tabs.setdefault(tabid, Tab()).entries[index] = {"url": url, "title": title}
        elif cmd == (4 if restore else 7):
            if len(payload) < 8:
                raise MigrationError("Invalid SNSS selection command.")
            tabid, index = struct.unpack_from("<ii", payload)
            tabs.setdefault(tabid, Tab()).selected = index
        elif not restore and cmd in (0, 2, 5, 11, 24):
            if len(payload) < 8:
                raise MigrationError("Invalid SNSS tab command.")
            first, second = struct.unpack_from("<ii", payload)
            if cmd == 0:
                tabs.setdefault(second, Tab()).window = first
            elif cmd == 2:
                tabs.setdefault(first, Tab()).position = second
            else:
                t = tabs.setdefault(first, Tab())
                if cmd == 5:
                    t.entries = {k: v for k, v in t.entries.items() if k < second}
                else:
                    count = second if cmd == 11 else struct.unpack_from("<i", payload, 8)[0]
                    start = 0 if cmd == 11 else second
                    t.entries = {(k - count if k >= start + count else k): v
                                 for k, v in t.entries.items() if not start <= k < start + count}
        elif cmd == (2 if restore else 16):
            if len(payload) < 4:
                raise MigrationError("Invalid SNSS close command.")
            tabs.pop(struct.unpack_from("<i", payload)[0], None)
        elif not restore and cmd == 17:
            window = struct.unpack_from("<i", payload)[0]
            tabs = {k: v for k, v in tabs.items() if v.window != window}
    return tabs


def latest(directory: Path):
    """Only the newest generation of each dialect; never resurrect old backups."""
    result = {}
    for prefix, restore in (("Tabs_", True), ("Session_", False)):
        files = [p for p in directory.glob(prefix + "*") if p.name[len(prefix):].isdigit()]
        if files:
            selected = max(files, key=lambda p: int(p.name[len(prefix):]))
            # The live Session_ state takes precedence over the tab restore cache.
            result.update(parse(selected, restore=restore))
    return {k: v for k, v in result.items() if v.entries}
