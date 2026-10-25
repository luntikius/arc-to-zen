import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from arc_to_zen.common import MigrationError, APPLE_EPOCH, database
from arc_to_zen.crypto import ArcDecryptor
from arc_to_zen.sessions import latest
from arc_to_zen.source import (add_archive, build_sidebars, cookie_record, discover,
                               identifier, read_history, read_secrets, Profile, workspace)
from arc_to_zen.transaction import Transaction, operation_lock, tree_digest, profile_ini, register_profiles
from arc_to_zen.zen import Gecko, stage_profile, check_sidebar
from arc_to_zen.cli import main
from fixtures import NOW, navigation, snss, synthetic_arc, write_json


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = synthetic_arc(Path(self.temp.name) / "Arc")

    def test_profiles_spaces_folders_and_identical_urls_keep_identity(self):
        before = tree_digest(self.root)
        profiles = discover(self.root)
        build_sidebars(self.root, profiles)
        self.assertEqual([p.name for p in profiles.values()], ["Your Arc", "work"])
        personal, work = [p.sidebar for p in profiles.values()]
        self.assertEqual([s["name"] for s in personal["spaces"]], ["Workspace 0"])
        self.assertEqual([s["name"] for s in work["spaces"]], ["Workspace 1"])
        self.assertEqual([f["name"] for f in personal["folders"]], ["Folder", "Empty nested folder"])
        self.assertEqual(personal["folders"][1]["parentId"], personal["folders"][0]["id"])
        self.assertEqual(personal["folders"][0]["collapsed"], False)
        self.assertEqual(personal["tabs"][0]["zenEssential"], True)
        pinned = next(t for t in personal["tabs"] if t["zenSyncId"] == identifier("item", "tab0a"))
        self.assertEqual(pinned["entries"][1]["url"], "https://same.example/current")
        self.assertEqual(pinned["index"], 2)
        self.assertEqual(pinned["_zenPinnedInitialState"]["entry"]["url"], "https://same.example/pinned")
        self.assertEqual(personal["tabs"][-1]["pinned"], False)
        self.assertEqual(personal["splitViewData"][0]["layoutTree"]["children"][0]["sizeInParent"], 40)
        self.assertEqual(tree_digest(self.root), before)

    def test_archive_exact_profile_dedup_ambiguity_and_dates(self):
        profiles = discover(self.root)
        mapping = build_sidebars(self.root, profiles)
        for p in profiles.values():
            read_history(p)
        def entry(url, source, last=None):
            tab = {"savedURL": url, "savedTitle": "Archive"}
            if last is not None:
                tab["timeLastActiveAt"] = last
            return {"source": source, "archivedAt": NOW / 1e6 - APPLE_EPOCH,
                    "sidebarItem": {"data": {"tab": tab}}}
        values = {"exact": entry("https://archive.example/", {"space": {"_0": "space1"}}),
            "repeat": entry("https://profile0.example/history", {"unknown": {}}),
            "unknown": entry("https://unknown.example/", {"littleArc": {}}),
            "ambiguous": entry("https://both.example/", {"unknown": {}}),
            "last": entry("https://last.example/", {"space": {"_0": "space0"}}, NOW / 1e6 - APPLE_EPOCH - 60)}
        for p in profiles.values():
            p.history.append({"url": "https://both.example/", "title": "", "date": NOW - 1, "transition": 1})
        from fixtures import dictionary
        write_json(self.root / "StorableArchiveItems.json", {"version": 1, "items": dictionary(values)})
        self.assertEqual(add_archive(self.root, profiles, mapping), 2)
        self.assertEqual(len(profiles["Default"].history), 3)
        self.assertEqual(len(profiles["Profile 1"].history), 3)
        self.assertEqual(profiles["Default"].history[-1]["date"], NOW - 60000000)
        self.assertEqual(profiles["Profile 1"].history[-1]["url"], "https://archive.example/")

    def test_nested_group_workspace_inheritance_preserves_isolation(self):
        import copy
        profiles = discover(self.root)
        build_sidebars(self.root, profiles)
        expected = profiles["Profile 1"].sidebar
        actual = copy.deepcopy(expected)
        split = next(f for f in actual["folders"] if f["splitViewGroup"])
        split["workspaceId"] = None
        check_sidebar(expected, actual)
        parent = next(f for f in actual["folders"] if f["id"] == split["parentId"])
        parent["workspaceId"] = "wrong-workspace"
        with self.assertRaises(MigrationError):
            check_sidebar(expected, actual)

    def test_unknown_schema_and_broken_references_fail_closed(self):
        p = self.root / "StorableSidebar.json"
        d = json.loads(p.read_text())
        d["version"] = 2
        write_json(p, d)
        with self.assertRaises(MigrationError):
            build_sidebars(self.root, discover(self.root))

    def test_cycle_or_missing_child_fails_before_any_write(self):
        path = self.root / "StorableSidebar.json"
        d = json.loads(path.read_text())
        items = d["sidebar"]["containers"][1]["items"]
        for item in items:
            if isinstance(item, dict) and item["id"] == "nested0":
                item["childrenIds"] = ["folder0"]
        write_json(path, d)
        before = tree_digest(self.root)
        with self.assertRaisesRegex(MigrationError, "cyclic"):
            build_sidebars(self.root, discover(self.root))
        self.assertEqual(tree_digest(self.root), before)

    def test_locked_sqlite_does_not_become_empty(self):
        path = self.root / "User Data/Default/History"
        with sqlite3.connect(path) as db:
            db.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(MigrationError):
                with database(path):
                    pass

    def test_wal_visits_are_visible(self):
        path = self.root / "User Data/Default/History"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO visits VALUES(2,1,?,0)", (NOW + 11644473600000001,))
            db.commit()
            with database(path) as reader:
                self.assertEqual(reader.execute("SELECT count(*) FROM visits").fetchone()[0], 2)

    def test_closed_wal_database_read_does_not_create_browser_sidecars(self):
        path = self.root / "User Data/Default/History"
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
        before = tree_digest(self.root)
        with database(path) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM visits").fetchone()[0], 1)
        self.assertEqual(tree_digest(self.root), before)

    def test_encrypted_login_and_cookie_read_from_source(self):
        path = self.root / "User Data/Default"
        with sqlite3.connect(path / "Login Data") as db:
            db.execute("INSERT INTO logins VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                "https://login.example:443/path", "", "u", "пользователь", "p", CryptoTests.encrypt("пароль".encode()),
                "https://login.example/", 0, 11644473600000000 + NOW, 3, 0))
        p = discover(self.root)["Default"]
        read_secrets(p, ArcDecryptor(lambda: b"synthetic-key"))
        self.assertEqual(p.logins[0]["origin"], "https://login.example")
        self.assertEqual(p.logins[0]["username"], "пользователь")
        self.assertEqual(p.logins[0]["password"], "пароль")

    def test_unknown_password_cipher_stops_without_keychain(self):
        path = self.root / "User Data/Default/Login Data"
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO logins VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                "https://login.example/", "", "u", "user", "p", b"v99unknown",
                "https://login.example/", 0, 11644473600000000 + NOW, 1, 0))
        provider = unittest.mock.Mock(side_effect=AssertionError("No key needed for unknown cipher"))
        with self.assertRaisesRegex(MigrationError, "Unknown password encryption"):
            read_secrets(discover(self.root)["Default"], ArcDecryptor(provider))
        provider.assert_not_called()

    def test_snss_generations_and_distinct_dialects(self):
        d = self.root / "User Data/Default/Sessions"
        (d / "Tabs_1").write_bytes(snss([(4, struct.pack("<iiq", 80, 0, 0)),
            (1, navigation(80, 0, "https://restore.example/", "日本語"))]))
        tabs = latest(d)
        self.assertEqual(sorted(tabs), [42, 80])
        self.assertEqual(tabs[80].state()["entries"][0]["title"], "日本語")
        (d / "Session_101").write_bytes(b"SNSS\3\0\0\0\xff")
        with self.assertRaises(MigrationError):
            latest(d)


class MergeTests(unittest.TestCase):
    def test_context_isolation_multiple_spaces_and_password_conflict(self):
        import copy
        from arc_to_zen.merge import merge_profiles
        with tempfile.TemporaryDirectory() as t:
            root = synthetic_arc(Path(t) / "Arc")
            profiles = discover(root); build_sidebars(root, profiles)
            a, b = profiles.values()
            extra = copy.deepcopy(a.sidebar["spaces"][0])
            extra.update(uuid="second-personal", name="Personal extra")
            a.sidebar["spaces"].append(extra)
            a.logins = [{"origin": "https://synthetic.example", "username": "user", "password": "one"}]
            b.logins = copy.deepcopy(a.logins)
            merged = merge_profiles([a, b])
            self.assertEqual([s["containerTabId"] for s in merged.sidebar["spaces"]], [1, 1, 2])
            self.assertEqual(len(merged.logins), 1)
            b.logins[0]["password"] = "two"
            with self.assertRaisesRegex(MigrationError, "Conflicting passwords"):
                merge_profiles([a, b])



class CryptoTests(unittest.TestCase):
    @staticmethod
    def encrypt(value):
        from arc_to_zen.crypto import aes_cbc
        key = hashlib.pbkdf2_hmac("sha1", b"synthetic-key", b"saltysalt", 1003, 16)
        return b"v10" + aes_cbc(value, key, b" " * 16, decrypt=False)

    def test_commoncrypto_nist_cbc_vector(self):
        from arc_to_zen.crypto import aes_cbc
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = aes_cbc(plain, key, iv, decrypt=False)
        self.assertEqual(encrypted[:16].hex(), "7649abac8119b246cee98e9b12e9197d")
        self.assertEqual(aes_cbc(encrypted, key, iv), plain)

    def test_unicode_lazy_key_and_unknown_versions(self):
        provider = unittest.mock.Mock(return_value=b"synthetic-key")
        decrypt = ArcDecryptor(provider)
        self.assertIsNone(decrypt(b"v20unsupported"))
        provider.assert_not_called()
        self.assertEqual(decrypt(self.encrypt("пароль 日本語 🔑".encode())).decode(), "пароль 日本語 🔑")
        provider.assert_called_once()
        with self.assertRaises(MigrationError):
            decrypt(b"v10broken")

    def test_denied_keychain_is_fatal_without_disclosing_values(self):
        def denied():
            raise MigrationError("Keychain denied")
        with self.assertRaisesRegex(MigrationError, "Keychain denied"):
            ArcDecryptor(denied)(self.encrypt(b"private"))

    def test_cookie_host_hash_security_and_partition(self):
        row = {"host_key": ".example.test", "name": "session", "path": "/", "value": "", "encrypted_value": self.encrypt(hashlib.sha256(b".example.test").digest() + "значение".encode()),
            "expires_utc": 11644473600000000 + NOW, "is_secure": 1, "is_httponly": 1, "is_persistent": 1,
            "samesite": 2, "source_scheme": 2, "top_frame_site_key": "https://top.example", "has_cross_site_ancestor": 0}
        cookie = cookie_record(row, 24, ArcDecryptor(lambda: b"synthetic-key"), now_s=NOW // 1000000 - 10)
        self.assertEqual(cookie["value"], "значение")
        self.assertEqual((cookie["secure"], cookie["httpOnly"], cookie["sameSite"]), (True, True, 2))
        self.assertEqual(cookie["originAttributes"], {"partitionKey": "(https,top.example)"})
        row["has_cross_site_ancestor"] = 1
        self.assertIsNone(cookie_record(row, 24, lambda _: self.fail("must skip before decrypt")))
        row["has_cross_site_ancestor"] = 0
        row["host_key"] = ".other.test"
        with self.assertRaises(MigrationError):
            cookie_record(row, 24, ArcDecryptor(lambda: b"synthetic-key"), now_s=NOW // 1000000 - 10)

    def test_expired_cookie_and_unspecified_same_site(self):
        row = {"host_key": "example.test", "name": "a", "path": "/", "value": "test", "encrypted_value": b"",
            "expires_utc": 11644473600000000 + NOW, "is_secure": 1, "is_httponly": 0, "is_persistent": 1,
            "samesite": -1, "source_scheme": 2, "top_frame_site_key": ""}
        self.assertIsNone(cookie_record(row, 24, lambda _: None, now_s=NOW // 1000000))
        c = cookie_record(row, 24, lambda _: None, now_s=NOW // 1000000 - 1)
        self.assertEqual(c["sameSite"], 256)
        row["has_cross_site_ancestor"] = 1
        # This flag is part of the partition key only. It must not reject normal
        # unpartitioned Chromium cookies (whose default flag value is true).
        self.assertIsNotNone(cookie_record(row, 24, lambda _: None, now_s=NOW // 1000000 - 1))


class CLITests(unittest.TestCase):
    def test_wizard_without_terminal_never_starts_replacement(self):
        with patch("builtins.open", side_effect=OSError("No terminal")), patch("arc_to_zen.cli.migrate") as migrate:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["wizard"]), 1)
            migrate.assert_not_called()

    def test_dry_run_is_read_only_and_never_opens_keychain(self):
        import plistlib
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            arc = synthetic_arc(base / "Arc")
            apps = []
            for name, version in (("Arc", "1.169.0"), ("Zen", "1.23.4b")):
                p = base / (name + ".app")
                (p / "Contents").mkdir(parents=True)
                (p / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
                apps.append(p)
            args = ["--arc-root", str(arc), "--zen-root", str(base / "zen"), "--runtime", str(base / "runtime"),
                    "--arc-app", str(apps[0]), "--zen-app", str(apps[1])]
            before = tree_digest(base)
            old_umask = os.umask(0o077)
            try:
                with patch("arc_to_zen.cli.resolve_apps"), patch.object(ArcDecryptor, "keychain", side_effect=AssertionError("No real secrets")), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args + ["migrate", "--dry-run"]), 0)
                    # Arc 1.170.0 retains the v1 data schemas and macOS v10 encryption.
                    with patch("arc_to_zen.cli.app_version", side_effect=["1.170.0", "1.23.4b"]):
                        self.assertEqual(main(args + ["migrate", "--dry-run"]), 0)
                    with patch("arc_to_zen.cli.app_version", side_effect=["1.171.0", "1.23.4b"]), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(args + ["migrate", "--dry-run"]), 0)
            finally:
                os.umask(old_umask)
            self.assertEqual(tree_digest(base), before)
            self.assertFalse((base / "runtime").exists())


@unittest.skipUnless(os.uname().sysname == "Darwin", "Atomic swap is macOS-specific")
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / "zen"
        self.target.mkdir()
        (self.target / "profiles.ini").write_text("[Profile0]\nName=Old\nIsRelative=1\nPath=Profiles/old\n")
        (self.target / "installs.ini").write_text("[TESTHASH]\nDefault=Profiles/old\n")
        (self.target / "old-data").write_text("keep this")
        self.original = tree_digest(self.target)
        self.runtime = self.base / "runs"
        self.runtime.mkdir()
        self.t = Transaction.create(self.runtime, self.target)
        self.t.stage.mkdir()
        (self.t.stage / "new-data").write_text("new")

    def test_exchange_backup_repeat_rollback_and_private_permissions(self):
        self.t.install({}, lambda: None)
        self.assertTrue((self.target / "new-data").exists())
        self.assertEqual(tree_digest(self.t.directory / "backup"), self.original)
        self.assertEqual((self.t.directory / "backup").stat().st_mode & 0o777, 0o700)
        self.t.rollback(False, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)
        self.t.rollback(False, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)

    def test_changes_require_force_and_are_retained(self):
        self.t.install({}, lambda: None)
        (self.target / "after").write_text("new browsing data")
        with self.assertRaisesRegex(MigrationError, "--force"):
            self.t.rollback(False, lambda: None)
        self.t.rollback(True, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)
        self.assertEqual((self.t.directory / "restore/after").read_text(), "new browsing data")

    def test_registration_preserves_gecko_key_case_and_new_user_profiles(self):
        profiles = {"imported": {"name": "Your Arc"}, "work": {"name": "work"}}
        register_profiles(self.t.stage, profiles, self.target)
        ini = profile_ini()
        ini.read(self.t.stage / "profiles.ini")
        self.assertEqual(ini["Profile0"]["Path"], "Profiles/imported")
        self.assertNotIn("path", ini["Profile0"])
        self.assertIn("Name=Your Arc\n", (self.t.stage / "profiles.ini").read_text())
        self.assertEqual(ini["InstallTESTHASH"]["Default"], "Profiles/imported")
        (self.t.stage / "Profiles/fresh").mkdir(parents=True)
        (self.target / "profiles.ini").write_text(
            "[Profile0]\nname=Your Arc\npath=Profiles/imported\n"
            "Name=New browsing\nIsRelative=1\nPath=Profiles/fresh\n")
        register_profiles(self.t.stage, profiles, self.target, preserve_existing=True)
        ini = profile_ini()
        ini.read(self.t.stage / "profiles.ini")
        self.assertEqual(ini["Profile2"]["Name"], "New browsing")
        self.assertEqual(ini["Profile2"]["Path"], "Profiles/fresh")

    def test_repair_exchange_keeps_post_migration_data_and_crash_recovery(self):
        self.t.install({}, lambda: None)
        (self.target / "after").write_text("new browsing data")
        before = tree_digest(self.target)
        real_save = self.t.save
        def save(d):
            if d.get("registration_repair", {}).get("phase") == "installed":
                raise RuntimeError("power loss after repair exchange")
            real_save(d)
        with patch.object(self.t, "verify"), patch.object(self.t, "save", save), self.assertRaises(RuntimeError):
            self.t.repair_registration(lambda: None)
        self.assertEqual((self.target / "after").read_text(), "new browsing data")
        retained = Path(self.t.load()["registration_repair"]["previous"])
        self.assertEqual(tree_digest(retained), before)
        self.t.repair_registration(lambda: None)
        self.assertEqual(self.t.load()["registration_repair"]["phase"], "installed")
        self.assertEqual(tree_digest(self.t.directory / "backup"), self.original)

    def test_crash_after_exchange_is_recoverable(self):
        real = self.t.save
        def save(d):
            if d["phase"] == "installed":
                raise RuntimeError("power loss after swap")
            real(d)
        with patch.object(self.t, "save", save), self.assertRaises(RuntimeError):
            self.t.install({}, lambda: None)
        self.assertEqual(self.t.load()["phase"], "applying")
        self.t.rollback(False, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)

    def test_crash_before_exchange_and_concurrent_operation(self):
        with patch("arc_to_zen.transaction.exchange", side_effect=RuntimeError("power loss")), self.assertRaises(RuntimeError):
            self.t.install({}, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)
        with self.assertRaisesRegex(MigrationError, "Unfinished"):
            Transaction.create(self.runtime, self.target)
        self.t.rollback(False, lambda: None)
        self.assertEqual(tree_digest(self.target), self.original)


@unittest.skipUnless(os.environ.get("ARC_TO_ZEN_INTEGRATION") == "1", "Opt in to isolated installed-Zen test")
class ZenIntegrationTests(unittest.TestCase):
    def test_repair_preserves_new_tabs_secrets_and_is_idempotent(self):
        from types import SimpleNamespace
        from arc_to_zen.repair import repair_spaces
        with tempfile.TemporaryDirectory(prefix="arc-to-zen-repair-") as t:
            base = Path(t)
            arc = synthetic_arc(base / "Arc")
            profiles = discover(arc); build_sidebars(arc, profiles)
            target = base / "zen"; target.mkdir()
            (target / "profiles.ini").write_text("[General]\nVersion=2\n")
            (target / "installs.ini").write_text("[TEST]\nDefault=Profiles/old\n")
            runtime = base / "runs"
            with operation_lock(runtime):
                original = Transaction.create(runtime, target)
                metadata = {}
                for i, p in enumerate(profiles.values()):
                    read_history(p)
                    if i == 0:
                        p.sidebar["spaces"].append(workspace({"id": "extra-empty", "title": "Space"}))
                    p.logins = [{"origin": "https://same.example", "username": str(i), "password": "日本語 secret",
                        "formActionOrigin": "", "httpRealm": None, "usernameElement": "", "passwordElement": ""}]
                    p.cookies = [{"host": "same.example", "path": "/", "name": "auth", "value": str(i),
                        "secure": True, "httpOnly": True, "session": True, "expiry": 0,
                        "originAttributes": {"partitionKey": "(https,top.example)"}, "sameSite": 0, "schemeMap": 2}]
                    p.bookmarks = [{"title": "User bookmark", "url": "https://bookmark.example/"}]
                    metadata[str(i)] = stage_profile(Path("/Applications/Zen.app"), original.stage / "Profiles" / str(i), p)
                original.install(metadata, lambda: None)
            # Simulate user activity after the old migration, using the native API.
            with Gecko(Path("/Applications/Zen.app"), target / "Profiles/1") as g:
                g.client.execute_async_script("""
                  const done = arguments[arguments.length - 1];
                  const w = Services.wm.getMostRecentWindow("navigator:browser");
                  const tab = w.gBrowser.addTab("about:config", {
                    triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});
                  w.gBrowser.selectedTab = tab;
                  w.setTimeout(() => done(true), 1000);
                """)
            before = tree_digest(target)
            args = SimpleNamespace(runtime=runtime, run=original.directory.name, zen_root=target, zen_app=Path("/Applications/Zen.app"), drop_empty_spaces=True)
            run = repair_spaces(args, lambda: None)
            tx = Transaction(runtime, run)
            self.assertEqual(tree_digest(tx.directory / "backup"), before)
            self.assertEqual(repair_spaces(args, lambda: None), run)
            profile = target / "Profiles" / identifier("profile", "combined")
            with Gecko(args.zen_app, profile) as g:
                exported = g.call("export", None)
                self.assertEqual(len(exported["logins"]), 2)
                self.assertEqual(len(exported["bookmarks"]), 2)
                self.assertEqual({c["value"] for c in exported["cookies"] if c["name"] == "auth"}, {"0", "1"})
                tabs = exported["state"]["windows"][0]["tabs"]
                self.assertTrue(any(e["url"] == "about:config" for tab in tabs for e in tab.get("entries", [])))
                self.assertEqual(len(exported["state"]["windows"][0]["spaces"]), 2)
            tx.rollback(True, lambda: None)
            self.assertEqual(tree_digest(target), before)

    def test_empty_profile(self):
        with tempfile.TemporaryDirectory(prefix="arc-to-zen-empty-") as t:
            p = Profile("Default", "Empty", Path(t) / "unused")
            p.sidebar = {"tabs": [], "folders": [], "groups": [], "splitViewData": [],
                "spaces": [workspace({"id": "empty", "title": "Empty"})]}
            p.sidebar["activeZenSpace"] = p.sidebar["spaces"][0]["uuid"]
            result = stage_profile(Path("/Applications/Zen.app"), Path(t) / "profile", p)
            self.assertEqual(result["counts"]["tabs"], 0)

    def test_two_synthetic_profiles_survive_two_restarts(self):
        with tempfile.TemporaryDirectory(prefix="arc-to-zen-integration-") as t:
            base = Path(t)
            root = synthetic_arc(base / "Arc")
            before = tree_digest(root)
            profiles = discover(root)
            build_sidebars(root, profiles)
            for i, p in enumerate(profiles.values()):
                read_history(p)
                p.history.append({"url": "https://too-long.example/" + "a" * 5000, "title": "Unsupported URL length", "date": NOW, "transition": 1})
                p.logins = [{"origin": "https://login.example", "formActionOrigin": "", "httpRealm": None,
                    "username": f"synthetic-{i}", "password": "тест 日本語 🔑", "usernameElement": "", "passwordElement": "", "timeCreated": NOW // 1000, "timesUsed": 1}]
                p.cookies = [{"host": "login.example", "path": "/", "name": "synthetic", "value": f"value-{i}", "secure": True,
                    "httpOnly": True, "session": True, "expiry": 0, "originAttributes": {}, "sameSite": 256, "schemeMap": 2},
                    {"host": "partition.example", "path": "/", "name": "chips", "value": "partitioned", "secure": True,
                    "httpOnly": True, "session": True, "expiry": 0, "originAttributes": {"partitionKey": "(https,top.example)"}, "sameSite": 0, "schemeMap": 2}]
                p.forms = [{"fieldname": "synthetic", "value": "日本語", "timesUsed": 2, "firstUsed": NOW, "lastUsed": NOW}]
                result = stage_profile(Path("/Applications/Zen.app"), base / f"profile{i}", p)
                self.assertEqual(result["counts"]["logins"], 1)
                self.assertEqual(result["counts"]["history"], 1)
                self.assertEqual(p.skipped["history"], 1)
                stored = json.loads((base / f"profile{i}/logins.json").read_text())
                self.assertNotIn("тест 日本語 🔑", json.dumps(stored, ensure_ascii=False))
                self.assertTrue((base / f"profile{i}/key4.db").exists())
                prefs = (base / f"profile{i}/prefs.js").read_text()
                self.assertIn('user_pref("zen.welcome-screen.seen", true);', prefs)
                self.assertNotIn('user_pref("marionette.', prefs)
            self.assertEqual(tree_digest(root), before)

    def test_cli_replace_verify_and_rollback_only_synthetic_roots(self):
        with tempfile.TemporaryDirectory(prefix="arc-to-zen-cli-integration-") as t:
            base = Path(t)
            arc = synthetic_arc(base / "Arc")
            for i, key in enumerate(("Default", "Profile 1")):
                path = arc / "User Data" / key
                with sqlite3.connect(path / "Login Data") as db:
                    db.execute("INSERT INTO logins VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        f"https://login{i}.example:443/path", "", "u", "пользователь", "p", CryptoTests.encrypt("пароль".encode()),
                        f"https://login{i}.example/", 0, 11644473600000000 + NOW, 3, 0))
                with sqlite3.connect(path / "Cookies") as db:
                    host = f"login{i}.example"
                    encrypted = CryptoTests.encrypt(hashlib.sha256(host.encode()).digest() + "cookie 日本語".encode())
                    db.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        host, "/", "session", "", encrypted, 0, 1, 1, 0, -1, 2, "", 0, 1))
            zen = base / "zen"
            zen.mkdir()
            (zen / "profiles.ini").write_text("[Profile0]\nName=Previous\nIsRelative=1\nPath=Profiles/old\n")
            (zen / "installs.ini").write_text("[TEST]\nDefault=Profiles/old\nLocked=1\n")
            (zen / "original").write_text("Keep this data")
            original, source = tree_digest(zen), tree_digest(arc)
            runtime = base / "runs"
            args = ["--arc-root", str(arc), "--zen-root", str(zen), "--runtime", str(runtime)]
            old_umask = os.umask(0o077)
            try:
                # Real browsers may remain running: this test exclusively targets
                # its temporary directories and never reads their profiles.
                with patch("arc_to_zen.cli.closed"), patch("arc_to_zen.cli.resolve_apps",
                    lambda a: (setattr(a, "arc_app", Path("/Applications/Zen.app")), setattr(a, "zen_app", Path("/Applications/Zen.app")))), patch.object(ArcDecryptor, "keychain", return_value=b"synthetic-key"):
                    self.assertEqual(main(args + ["migrate", "--replace"]), 0)
                    run = next(runtime.glob("*/journal.json")).parent.name
                    self.assertEqual(main(args + ["verify", "--run", run]), 0)
                    # Test Gecko's real, case-sensitive INI parser rather than
                    # reopening an explicit profile and bypassing registration.
                    with Gecko(Path("/Applications/Zen.app"), base / "registry-probe") as g:
                        selected = g.client.execute_script('''
                          const f = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
                          f.initWithPath(arguments[0]);
                          const parser = Cc["@mozilla.org/xpcom/ini-parser-factory;1"]
                            .getService(Ci.nsIINIParserFactory).createINIParser(f);
                          return [parser.getString("Profile0", "Name"),
                            parser.getString("Profile0", "Path"),
                            parser.getString("InstallTEST", "Default")];
                        ''', script_args=[str(zen / "profiles.ini")])
                        self.assertEqual(selected, ["Arc", "Profiles/" + identifier("profile", "combined"),
                                                    "Profiles/" + identifier("profile", "combined")])
                    self.assertEqual(main(args + ["rollback", "--run", run]), 0)
            finally:
                os.umask(old_umask)
            self.assertEqual(tree_digest(arc), source)
            self.assertEqual(tree_digest(zen), original)


if __name__ == "__main__":
    unittest.main()
