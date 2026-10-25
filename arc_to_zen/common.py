from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

CHROME_EPOCH_US = 11644473600000000
APPLE_EPOCH = 978307200


class MigrationError(Exception):
    """Messages must describe the stage, never include source values/secrets."""


def safe(value):
    """Render untrusted names and paths without terminal control sequences."""
    return ''.join(c for c in str(value) if c.isprintable() and c not in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MigrationError(f"Cannot read {path.name}; no changes applied.") from exc


def atomic_json(path: Path, value):
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def private_opener(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def validate_staging_tree(path: Path):
    """A writable browser copy must not redirect writes outside itself."""
    import stat
    if path.is_symlink() or not path.is_dir():
        raise MigrationError("Staging directory must be a real directory.")
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            if name in ("parent.lock", ".parentlock", "lock") and p.parent == path:
                continue
            mode = p.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise MigrationError("Staging profile contains a link or special file; replacement stopped.")


def fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_tree(path: Path):
    """Do not follow symlinks, including browser lock symlinks."""
    if path.is_symlink():
        raise MigrationError("Private directory cannot be a symlink.")
    for root, dirs, files in os.walk(path, followlinks=False):
        os.chmod(root, 0o700)
        for name in dirs + files:
            p = Path(root) / name
            if not p.is_symlink():
                os.chmod(p, 0o700 if p.is_dir() else 0o600)


def pairs(value):
    """Swift Codable dictionaries with non-string keys use alternating arrays."""
    if isinstance(value, dict):
        return list(value.items())
    if not isinstance(value, list) or len(value) % 2:
        raise MigrationError("Unknown Arc dictionary encoding.")
    return list(zip(value[::2], value[1::2]))


def profile_key(value):
    if value.get("default") is True:
        return "Default"
    try:
        key = value["custom"]["_0"]["directoryBasename"]
    except (KeyError, TypeError) as exc:
        raise MigrationError("Unknown Arc profile reference.") from exc
    if not isinstance(key, str) or Path(key).name != key or key in (".", ".."):
        raise MigrationError("Invalid Arc profile directory.")
    return key


def compatible_url(value, history=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        u = urlsplit(value)
        if u.scheme in ("http", "https", "ftp"):
            return bool(u.hostname)
        return not history and (u.scheme == "file" or value == "about:blank")
    except ValueError:
        return False


def chrome_us(value):
    return int(value) - CHROME_EPOCH_US


def arc_us(value):
    return round((float(value) + APPLE_EPOCH) * 1000000)


def app_version(app: Path):
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            return plistlib.load(f)["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError) as exc:
        raise MigrationError("Cannot read browser version.") from exc


@contextmanager
def database(path: Path):
    """Never immutable=1: ignoring a WAL can silently lose recent records."""
    connection = None
    snapshot = None
    try:
        read_path = path
        with open(path, "rb") as f:
            header = f.read(20)
        if header[18:20] == b"\x02\x02":
            # Even mode=ro can CREATE -wal/-shm next to a WAL-mode database.
            # Rebuild its shared-memory index only in a private disposable copy.
            snapshot = tempfile.TemporaryDirectory(prefix="arc-to-zen-read-")
            read_path = Path(snapshot.name) / "read.sqlite"
            sources = [path, path.with_name(path.name + "-wal")]
            def signature():
                return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in sources]
            before = signature()
            for src, suffix in zip(sources, ("", "-wal")):
                if src.exists():
                    dst = read_path.with_name(read_path.name + suffix)
                    shutil.copyfile(src, dst)
                    dst.chmod(0o600)
            if signature() != before:
                raise MigrationError(f"{path.name} changed during read; close both browsers.")
        # A checkpointed WAL DB may need to create a fresh WAL index on open.
        # Only the disposable copy may be opened writable; query_only still
        # rejects all business-data writes through the yielded connection.
        mode = "rw" if snapshot is not None else "ro"
        connection = sqlite3.connect(read_path.as_uri() + "?mode=" + mode, uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        yield connection
    except sqlite3.Error as exc:
        raise MigrationError(f"Cannot read {path.name} (locked or invalid database); close both browsers.") from exc
    finally:
        if connection is not None:
            connection.close()
        if snapshot is not None:
            snapshot.cleanup()


def table_exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
