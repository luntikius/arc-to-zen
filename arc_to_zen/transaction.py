"""Durable journal + macOS atomic directory exchange; backups are never deleted."""
from __future__ import annotations

import configparser
import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .common import (MigrationError, atomic_json, database, fsync_dir, private_tree,
                     read_json, private_opener, validate_staging_tree)
from .zen import read_lz4, comparable_sidebar, prepare_profile_group

MARKER = ".arc-to-zen-run.json"


def profile_ini():
    # Gecko's INI parser is case-sensitive. ConfigParser's default lowercasing
    # makes otherwise valid profiles invisible to normal application startup.
    ini = configparser.ConfigParser(interpolation=None)
    ini.optionxform = str
    return ini


def tree_digest(path):
    digest = hashlib.sha256()
    if not path.is_dir():
        raise MigrationError("Browser data directory is missing.")
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(root) / name
            digest.update(str(p.relative_to(path)).encode() + b"\x00")
            if p.is_symlink():
                digest.update(b"L" + os.readlink(p).encode())
            elif p.is_file():
                digest.update(b"F")
                with open(p, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        digest.update(chunk)
            else:
                digest.update(b"D")
    return digest.hexdigest()


def exchange(first, second):
    """RENAME_SWAP exchanges both non-empty directory entries in one operation."""
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename = lib.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(first), os.fsencode(second), 2):
        raise MigrationError(f"Atomic directory exchange failed (errno {ctypes.get_errno()}).")
    fsync_dir(first.parent)
    fsync_dir(second.parent)


def sync_tree(path):
    for root, _, files in os.walk(path):
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                with open(p, "rb") as f:
                    os.fsync(f.fileno())
        fsync_dir(Path(root))


def validate_target(root):
    if root.is_symlink() or not root.is_dir() or not (root / "profiles.ini").is_file():
        raise MigrationError("Replacement requires an existing, non-symlinked Zen data root with profiles.ini.")
    ini = profile_ini()
    ini.read(root / "profiles.ini")
    for s in ini.sections():
        if s.startswith("Profile"):
            rel = ini[s].get("Path", "")
            p = root / rel if ini[s].get("IsRelative") == "1" else Path(rel)
            if not p.resolve().is_relative_to(root.resolve()):
                raise MigrationError("External Zen profile detected; replacement is not supported.")


def register_profiles(stage, profiles, target, preserve_existing=False):
    ini = profile_ini()
    ini["General"] = {"StartWithLastProfile": "1", "Version": "2"}
    default = ""
    for i, (folder, meta) in enumerate(profiles.items()):
        rel = "Profiles/" + folder
        if i == 0:
            default = rel
        ini[f"Profile{i}"] = {"Name": meta["name"], "IsRelative": "1", "Path": rel}
        if meta.get("store_id"):
            ini[f"Profile{i}"]["StoreID"] = meta["store_id"]
        if i == 0:
            ini[f"Profile{i}"]["Default"] = "1"
    # Keep the installed app hash and the newly prepared shared profile group.
    if preserve_existing:
        current = profile_ini()
        current.read(target / "profiles.ini")
        known = {s["Path"] for s in ini.values() if "Path" in s}
        index = len(profiles)
        for section in current.sections():
            s = current[section]
            if not section.startswith("Profile") or not s.get("Path") or s["Path"] in known:
                continue
            if s.get("IsRelative") != "1" or not (stage / s["Path"]).is_dir():
                raise MigrationError("Cannot preserve an external or missing Zen profile.")
            ini[f"Profile{index}"] = {"Name": s.get("Name", "Saved profile"), "IsRelative": "1", "Path": s["Path"]}
            known.add(s["Path"])
            index += 1
    old = profile_ini()
    old.read(target / "installs.ini")
    installs = profile_ini()
    for s in old.sections():
        installs[s] = {"Default": default, "Locked": "1"}
        ini["Install" + s] = {"Default": default, "Locked": "1"}
    for name, value in (("profiles.ini", ini), ("installs.ini", installs)):
        with open(stage / name, "w", opener=private_opener) as f:
            value.write(f, space_around_delimiters=False)


@contextmanager
def operation_lock(runtime):
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    if runtime.is_symlink():
        raise MigrationError("Runtime directory cannot be a symlink.")
    os.chmod(runtime, 0o700)
    with open(runtime / ".lock", "a", opener=private_opener) as f:
        os.fchmod(f.fileno(), 0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError("Another migration operation is running.") from None
        yield


def marker(root):
    p = root / MARKER
    return read_json(p).get("run") if p.exists() else None


class Transaction:
    def __init__(self, runtime, run):
        if Path(run).name != run or run in (".", ".."):
            raise MigrationError("Invalid run ID.")
        self.directory = runtime / run
        self.journal = self.directory / "journal.json"
        if runtime.is_symlink() or self.directory.is_symlink() or self.journal.is_symlink():
            raise MigrationError("Recovery paths cannot be symlinks.")

    @classmethod
    def create(cls, runtime, target):
        # An interrupted operation must be reconciled before another replacement.
        for p in runtime.glob("*/journal.json"):
            if read_json(p).get("phase") not in ("installed", "rolled_back", "failed"):
                raise MigrationError(f"Unfinished run {p.parent.name}; use rollback --run before retrying.")
        run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        obj = cls(runtime, run)
        obj.directory.mkdir(mode=0o700)
        obj.save({"version": 1, "run": run, "target": str(target), "phase": "preparing", "profiles": {}})
        return obj

    def load(self):
        d = read_json(self.journal)
        if d.get("version") != 1 or d.get("run") != self.directory.name:
            raise MigrationError("Invalid migration journal.")
        return d

    def save(self, value):
        atomic_json(self.journal, value)

    @property
    def stage(self):
        return self.directory / "staging"

    def install(self, profiles, check_closed, expected_before=None, preserve_registration=False, emit=lambda *args: None):
        d = self.load()
        target = Path(d["target"])
        check_closed()
        validate_target(target)
        if target.stat().st_dev != self.stage.stat().st_dev:
            raise MigrationError("Staging and Zen must be on the same filesystem for atomic replacement.")
        before = tree_digest(target)
        if expected_before is not None and before != expected_before:
            raise MigrationError("Zen changed since preparation; no replacement performed.")
        emit("Backing up existing Zen")
        shutil.copytree(target, self.directory / "backup", symlinks=True)
        private_tree(self.directory / "backup")
        if tree_digest(self.directory / "backup") != before or tree_digest(target) != before:
            raise MigrationError("Zen data changed while taking the backup; close Zen and retry.")
        sync_tree(self.directory / "backup")
        if not preserve_registration:
            register_profiles(self.stage, profiles, target)
        atomic_json(self.stage / MARKER, {"run": d["run"]})
        private_tree(self.stage)
        # All content is closed/flushed before the directory exchange.
        sync_tree(self.stage)
        after = tree_digest(self.stage)
        emit("Installing verified profile")
        d.update(phase="applying", before=before, installed=after, profiles=profiles)
        self.save(d)
        check_closed()
        if tree_digest(target) != before:
            raise MigrationError("Zen data changed before replacement; no exchange performed.")
        exchange(self.stage, target)
        d["phase"] = "installed"
        self.save(d)

    def rollback(self, force, check_closed, expected_current=None, emit=lambda *args: None):
        d = self.load()
        target = Path(d["target"])
        check_closed()
        if d["phase"] == "rolled_back":
            return
        # A crash before the exchange leaves the original tree in place.
        if d["phase"] in ("preparing", "failed") or (d["phase"] == "applying" and marker(target) != d["run"]):
            if d.get("before") and tree_digest(target) != d["before"]:
                raise MigrationError("Cannot reconcile interrupted replacement: target changed.")
            d["phase"] = "rolled_back"
            self.save(d)
            return
        if d["phase"] == "rolling_back" and tree_digest(target) == d["before"]:
            d["phase"] = "rolled_back"
            self.save(d)
            return
        if marker(target) != d["run"]:
            raise MigrationError("This run does not own the current Zen data directory.")
        current = tree_digest(target)
        if expected_current is None:
            expected_current = current
        if not force and current != d["installed"]:
            raise MigrationError("Zen changed after migration; rollback requires --force. Current data will be retained.")
        backup = self.directory / "backup"
        emit("Checking backup integrity")
        if tree_digest(backup) != d["before"]:
            raise MigrationError("Backup integrity check failed.")
        emit("Preparing restored Zen")
        restore = self.directory / "restore"
        if restore.exists():
            # Never overwrite a previous recovery artifact.
            restore = self.directory / ("restore-" + uuid.uuid4().hex[:8])
        shutil.copytree(backup, restore, symlinks=True)
        private_tree(restore)
        sync_tree(restore)
        if tree_digest(restore) != d['before']:
            raise MigrationError('Prepared restore does not match the backup; no exchange performed.')
        d.update(phase="rolling_back", restored_copy=str(restore))
        self.save(d)
        check_closed()
        emit("Restoring Zen")
        if expected_current is not None and tree_digest(target) != expected_current:
            raise MigrationError("Zen changed after confirmation; no restore performed. Review the backup again.")
        exchange(restore, target)
        d["phase"] = "rolled_back"
        self.save(d)

    def repair_registration(self, check_closed, app=None):
        """Repair old lowercased registrations, retaining every current file."""
        d = self.load()
        target = Path(d["target"])
        check_closed()
        if d["phase"] != "installed" or marker(target) != d["run"]:
            raise MigrationError("Registration repair requires this run to be installed.")
        pending = d.get("registration_repair", {})
        if pending.get("phase") == "applying":
            digest = tree_digest(target)
            if digest == pending["after"]:
                pending["phase"] = "installed"
                d["installed"] = digest
                self.save(d)
                return
            if digest != pending["before"]:
                raise MigrationError("Interrupted registration repair has subsequent changes; use rollback --force for recovery.")
        if tree_digest(self.directory / "backup") != d["before"]:
            raise MigrationError("Backup integrity check failed.")
        before = tree_digest(target)
        repair = self.directory / ("registration-repair-" + uuid.uuid4().hex[:8])
        repair.mkdir(mode=0o700)
        ready = repair / "previous"
        shutil.copytree(target, ready, symlinks=True)
        validate_staging_tree(ready)
        private_tree(ready)
        if tree_digest(ready) != before or tree_digest(target) != before:
            raise MigrationError("Zen data changed during registration repair.")
        if app and d["profiles"]:
            prepare_profile_group(app, ready, d["profiles"])
        register_profiles(ready, d["profiles"], target, preserve_existing=True)
        for folder in d["profiles"]:
            prefs = ready / "Profiles" / folder / "prefs.js"
            lines = [line for line in prefs.read_text().splitlines()
                     if '"zen.welcome-screen.seen",' not in line]
            prefs.write_text("\n".join(lines) + '\nuser_pref("zen.welcome-screen.seen", true);\n')
        sync_tree(ready)
        after = tree_digest(ready)
        d["registration_repair"] = {"phase": "applying", "before": before,
                                    "after": after, "previous": str(ready)}
        self.save(d)
        check_closed()
        if tree_digest(target) != before:
            raise MigrationError("Zen changed before registration repair; no exchange performed.")
        exchange(ready, target)
        d["registration_repair"]["phase"] = "installed"
        d["installed"] = after
        self.save(d)

    def verify(self, check_registration=True):
        d = self.load()
        target = Path(d["target"])
        if d["phase"] not in ("installed", "applying") or marker(target) != d["run"]:
            raise MigrationError("Run is not installed; use rollback to reconcile an interrupted operation.")
        if check_registration and d.get("registration_hashes"):
            for name, expected_hash in d["registration_hashes"].items():
                if hashlib.sha256((target / name).read_bytes()).hexdigest() != expected_hash:
                    raise MigrationError("Zen registration changed after appearance repair.")
        elif check_registration:
            ini = profile_ini()
            ini.read(target / "profiles.ini")
            for i, (folder, meta) in enumerate(d["profiles"].items()):
                if f"Profile{i}" not in ini:
                    raise MigrationError("Zen profile registration is missing; use repair-registration --run.")
                section = ini[f"Profile{i}"]
                if (section.get("Name"), section.get("IsRelative"), section.get("Path")) != (meta["name"], "1", "Profiles/" + folder):
                    raise MigrationError("Zen profile registration is incomplete; use repair-registration --run.")
            expected = "Profiles/" + next(iter(d["profiles"]), "")
            installs = profile_ini()
            installs.read(target / "installs.ini")
            for section in installs.sections():
                if installs[section].get("Default") != expected:
                    raise MigrationError("Zen default profile does not select the migration.")
        for folder, meta in d["profiles"].items():
            profile = target / "Profiles" / folder
            if meta.get("store_id"):
                store_id = meta["store_id"]
                with database(target / "Profile Groups" / (store_id + ".sqlite")) as db:
                    registered = dict(db.execute("SELECT path,name FROM Profiles"))
                if registered.get("Profiles/" + folder) != meta["name"]:
                    raise MigrationError("Zen profile switcher registration is incomplete.")
                prefs = (profile / "prefs.js").read_text()
                for key, value in (("toolkit.profiles.storeID", store_id),
                                   ("browser.profiles.enabled", True), ("browser.profiles.created", True)):
                    if f"user_pref({json.dumps(key)}, {json.dumps(value)});" not in prefs:
                        raise MigrationError("Zen profile switcher preferences are incomplete.")
            with database(profile / "places.sqlite") as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise MigrationError("Zen history integrity check failed.")
                if db.execute("SELECT count(*) FROM moz_historyvisits").fetchone()[0] < meta["counts"]["history"]:
                    raise MigrationError("Zen history is incomplete.")
            sidebar = read_lz4(profile / "zen-sessions.jsonlz4")
            digest = hashlib.sha256(json.dumps(comparable_sidebar(sidebar), sort_keys=True).encode()).hexdigest()
            if digest != meta["sidebar_hash"]:
                raise MigrationError("Zen sidebar changed or is incomplete.")
            if meta.get("appearance_hash"):
                from .appearance import appearance_digest
                if appearance_digest(sidebar) != meta["appearance_hash"]:
                    raise MigrationError("Zen appearance changed or is incomplete.")
            if meta["counts"]["logins"]:
                logins = read_json(profile / "logins.json")
                if len(logins.get("logins", [])) < meta["counts"]["logins"] or not (profile / "key4.db").is_file():
                    raise MigrationError("Zen encrypted logins are incomplete.")
        if tree_digest(self.directory / "backup") != d["before"]:
            raise MigrationError("Backup integrity check failed.")
        return d
