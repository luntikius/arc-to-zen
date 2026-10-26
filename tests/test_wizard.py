import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import synthetic_arc, write_json, NOW
from arc_to_zen.common import MigrationError, APPLE_EPOCH
from arc_to_zen.selection import Selection, catalog
from arc_to_zen.transfer import collect, scan, transfer, source_stamp, Cancelled
from arc_to_zen.transaction import tree_digest, Transaction
from arc_to_zen.terminal import safe, logo
from arc_to_zen.wizard import selection_screen, run
from arc_to_zen.demo import Demo, CATALOG
from arc_to_zen.cli import parser


def choose(data, profiles=None, spaces=None, categories=None):
    return Selection.from_args(SimpleNamespace(profiles=profiles, spaces=spaces, categories=categories), data)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = synthetic_arc(Path(self.temp.name) / 'Arc')
        self.data = catalog(self.root)
        self.args = SimpleNamespace(arc_root=self.root)

    def test_disabled_categories_and_unselected_profiles_are_never_opened(self):
        selection = choose(self.data, ['Default'], ['space0'], ['tabs'])
        # Removing every disabled database must still allow both scan and collect.
        for p in (self.root / 'User Data').iterdir():
            if not p.is_dir(): continue
            for name in ('History', 'Login Data', 'Cookies', 'Web Data'): (p / name).unlink()
        (self.root / 'StorableArchiveItems.json').unlink()
        with patch('arc_to_zen.transfer.ArcDecryptor', side_effect=AssertionError('No secrets selected')):
            result = scan(self.args, selection)
            value = collect(self.root, selection, secrets=True)
        self.assertEqual(result.counts['profiles'], 1)
        self.assertEqual(result.counts['spaces'], 1)
        self.assertEqual(result.counts['tabs'], 5)
        self.assertEqual(len(value.cookies), 0)
        self.assertTrue(all(t['userContextId'] == 1 for t in value.sidebar['tabs']))
        self.assertTrue(all(not s['_arcAppearance'] for s in value.sidebar['spaces']))

    def test_profile_only_and_web_categories_do_not_read_tabs_or_cards(self):
        for name in ('StorableWindows.json', 'StorableSessionRestorationData.json'):
            (self.root / name).unlink()
        p = self.root / 'User Data/Default'
        with sqlite3.connect(p / 'Web Data') as db:
            db.execute('INSERT INTO autofill VALUES ("city","synthetic",1,1,2)')
            db.execute('CREATE TABLE credit_cards(card_number_encrypted BLOB)')
            db.execute('INSERT INTO credit_cards VALUES (?)', (b'unknown-cipher',))
        selection = choose(self.data, ['Default'], ['none'], ['forms'])
        with patch('arc_to_zen.transfer.ArcDecryptor', side_effect=AssertionError('Disabled cards')):
            value = collect(self.root, selection, secrets=True)
        self.assertEqual(len(value.forms), 1)
        self.assertEqual(value.cards, [])
        self.assertEqual(len(value.sidebar['spaces']), 0)
        self.assertEqual(value.sidebar['tabs'], [])

    def test_passwords_and_cookies_have_independent_readers(self):
        selection = choose(self.data, ['Default'], ['none'], ['passwords'])
        (self.root / 'User Data/Default/Cookies').unlink()
        with patch('arc_to_zen.transfer.read_cookies', side_effect=AssertionError('Cookies disabled')):
            collect(self.root, selection, secrets=True)
        selection = choose(self.data, ['Profile 1'], ['none'], ['cookies'])
        (self.root / 'User Data/Profile 1/Login Data').unlink()
        with patch('arc_to_zen.transfer.read_passwords', side_effect=AssertionError('Passwords disabled')):
            collect(self.root, selection, secrets=True)

    def test_scan_has_no_keychain_and_binds_selection_and_source(self):
        selection = choose(self.data)
        before = tree_digest(self.root)
        with patch('arc_to_zen.transfer.ArcDecryptor', side_effect=AssertionError('Scan must not decrypt')):
            result = scan(self.args, selection)
        self.assertEqual(before, tree_digest(self.root))
        self.assertEqual(result.stamp, source_stamp(self.root, selection))
        self.assertNotEqual(result.stamp, source_stamp(self.root, choose(self.data, categories=['tabs'])))
        with (self.root / 'User Data/Default/History').open('ab') as f: f.write(b'changed')
        self.assertNotEqual(result.stamp, source_stamp(self.root, selection))

    def test_archive_never_reassigns_an_unselected_profile(self):
        entry = {'sidebarItem': {'data': {'tab': {'savedURL': 'https://profile0.example/history'}}},
                 'source': {'space': {'_0': 'space1'}}, 'archivedAt': NOW / 1e6 - APPLE_EPOCH + 10}
        write_json(self.root / 'StorableArchiveItems.json', {'version': 1, 'items': ['archive', entry]})
        selection = choose(self.data, ['Default'], ['none'], ['history', 'archive'])
        value = collect(self.root, selection)
        self.assertEqual(len(value.history), 1)
        self.assertEqual(value.skipped['archive'], 1)
        entry['source'] = {}
        write_json(self.root / 'StorableArchiveItems.json', {'version': 1, 'items': ['archive', entry]})
        self.assertEqual(len(collect(self.root, selection).history), 1)

    def test_selection_dependencies_and_cli_flags(self):
        for categories in (['themes'], ['icons'], ['archive'], ['unknown']):
            with self.assertRaises(MigrationError): choose(self.data, categories=categories)
        with self.assertRaises(MigrationError): choose(self.data, ['Default'], ['space1'])
        args = parser().parse_args(['migrate', '--dry-run', '--profiles', 'Default', '--spaces', 'none', '--categories', 'passwords'])
        self.assertEqual(Selection.from_args(args, self.data).spaces, frozenset())


class FakeUI:
    actions = []
    titles = []
    receipt = []
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def intro(self): pass
    def choose(self, title, lines, options, **kwargs):
        self.titles.append(title)
        answer = self.actions.pop(0)
        if callable(answer): answer = answer(options)
        assert answer in [value for _, value in options] or answer in ('cancel', 'back'), (title, answer)
        return answer
    def work(self, title, op, **kwargs):
        self.titles.append(title)
        return op(lambda *args: None)


class WizardTests(unittest.TestCase):
    def test_tree_dependencies_and_profile_retained_without_spaces(self):
        selection = choose(CATALOG)
        ui = FakeUI(); ui.actions = [('category', 'tabs'), ('category', 'themes'), ('space', 'personal-space'),
            ('space', 'projects-space'), ('continue', None)]
        result, selection = selection_screen(ui, CATALOG, selection)
        self.assertEqual(result, 'continue')
        self.assertIn('personal', selection.profiles)
        self.assertNotIn('personal-space', selection.spaces)
        self.assertIn('tabs', selection.categories)
        self.assertNotIn('icons', selection.categories)

    def test_demo_back_rescans_and_cannot_access_real_services(self):
        FakeUI.actions = ['continue', 'select', ('continue', None), 'back', ('category', 'passwords'),
                          ('continue', None), 'confirm', 'finish']
        FakeUI.titles = []
        args = parser().parse_args(['wizard', '--demo', '--dry-run', '--no-animation'])
        original = Demo.__init__
        def fast(instance, failure=None): original(instance, failure, delay=0)
        with patch('arc_to_zen.wizard.Terminal', FakeUI), patch.object(Demo, '__init__', fast), \
             patch('arc_to_zen.wizard.detect', side_effect=AssertionError('Real discovery')), \
             patch('arc_to_zen.wizard.catalog', side_effect=AssertionError('Real Arc')), \
             patch('arc_to_zen.wizard.transfer', side_effect=AssertionError('Real migration')), \
             patch('arc_to_zen.wizard.subprocess.run', side_effect=AssertionError('No browser launch')):
            self.assertEqual(run(args), 0)
        self.assertEqual(FakeUI.titles.count('Scanning Arc'), 1)
        self.assertEqual(FakeUI.titles.count('Preparing migration'), 2)
        self.assertLess(FakeUI.titles.index('Scanning Arc'), FakeUI.titles.index('Choose what comes with you'))
        self.assertIn('All set.', FakeUI.titles)

    def test_demo_failures_are_not_success(self):
        for failure in ('scan', 'transfer'):
            with self.subTest(failure=failure):
                backend = Demo(failure, delay=0)
                selection = choose(CATALOG)
                if failure == 'scan':
                    with self.assertRaises(MigrationError): backend.scan(selection, lambda *a: None)
                else:
                    scanned = backend.scan(selection, lambda *a: None)
                    with self.assertRaises(MigrationError): backend.transfer(scanned, lambda *a: None)

    def test_full_migration_skips_options_and_open_remains_isolated(self):
        FakeUI.actions = ['continue', 'full', 'confirm', 'open']
        FakeUI.titles = []
        args = parser().parse_args(['wizard', '--demo', '--no-animation'])
        original = Demo.__init__
        def fast(instance, failure=None): original(instance, failure, delay=0)
        with patch('arc_to_zen.wizard.Terminal', FakeUI), patch.object(Demo, '__init__', fast), \
             patch('arc_to_zen.wizard.catalog', side_effect=AssertionError('No real browser reads')), \
             patch('arc_to_zen.wizard.transfer', side_effect=AssertionError('No real transfer')), \
             patch('arc_to_zen.wizard.subprocess.run', side_effect=AssertionError('No browser launch')):
            self.assertEqual(run(args), 0)
        self.assertNotIn('Choose what comes with you', FakeUI.titles)
        self.assertLess(FakeUI.titles.index('Scanning Arc'), FakeUI.titles.index('How would you like to migrate?'))
        self.assertEqual(FakeUI.receipt, [])

    def test_finish_actions_open_only_the_chosen_destination_then_exit(self):
        from arc_to_zen.wizard import finished
        backend = Demo(delay=0)
        result = backend.transfer(backend.scan(choose(CATALOG), lambda *a: None), lambda *a: None)
        args = SimpleNamespace(zen_app=Path('/Synthetic/Zen.app'))
        for action, command in (('open', ['/usr/bin/open', '-a', '/Synthetic/Zen.app']),
                                ('finish', ['/usr/bin/open', 'https://github.com/luntikius/arc-to-zen'])):
            with self.subTest(action=action):
                ui = FakeUI(); ui.actions = [action]
                with patch('arc_to_zen.wizard.subprocess.run', return_value=SimpleNamespace(returncode=0)) as launch:
                    self.assertEqual(finished(ui, result, args, False), 0)
                launch.assert_called_once_with(command, capture_output=True, timeout=15)
                self.assertEqual(ui.actions, [])
                self.assertIn('To restore later, run the installer again and choose Restore from backup.', ui.receipt)

    def test_cancellation_and_terminal_sanitization(self):
        def cancelled(*args): raise Cancelled('Cancelled')
        with self.assertRaises(Cancelled): Demo(delay=0).scan(choose(CATALOG), cancelled)
        self.assertEqual(safe('name\x1b\x07\n\u202e'), 'name')
        self.assertEqual(len(logo('arc', 0)), 11)
        self.assertNotEqual(logo('arc', 0), logo('arc', 1))


@unittest.skipUnless(os.environ.get('ARC_TO_ZEN_INTEGRATION') == '1', 'Opt in to isolated Zen checks')
class SelectiveIntegrationTests(unittest.TestCase):
    def test_one_selected_space_no_secrets_and_stale_scan_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc'); zen = base / 'zen'; zen.mkdir()
            (zen / 'profiles.ini').write_text('[General]\nVersion=2\n')
            args = SimpleNamespace(arc_root=arc, zen_root=zen, runtime=base / 'runs', zen_app=Path('/Applications/Zen.app'))
            selection = choose(catalog(arc), ['Default'], ['space0'], ['tabs', 'themes', 'icons'])
            before = tree_digest(zen)
            with patch('arc_to_zen.transfer.ArcDecryptor', side_effect=AssertionError('No secrets')):
                scanned = scan(args, selection, probe=True)
                scanned.stamp = 'stale'
                with self.assertRaises(MigrationError): transfer(args, scanned, lambda: None)
                self.assertEqual(before, tree_digest(zen))
                scanned.stamp = source_stamp(arc, selection)
                result = transfer(args, scanned, lambda: None)
            tx = Transaction(args.runtime, result['run']); tx.verify()
            self.assertEqual(result['counts']['profiles'], 1)
            self.assertEqual(result['counts']['spaces'], 1)
            self.assertEqual(result['counts']['logins'], 0)
            self.assertEqual(result['counts']['tabs'], 5)
            tx.rollback(False, lambda: None)
            self.assertEqual(before, tree_digest(zen))

    def test_unsupported_native_records_are_counted_as_skipped(self):
        from arc_to_zen.zen import Gecko
        with tempfile.TemporaryDirectory() as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc'); zen = base / 'zen'; zen.mkdir()
            (zen / 'profiles.ini').write_text('[General]\nVersion=2\n')
            with sqlite3.connect(arc / 'User Data/Default/Web Data') as db:
                db.executescript('CREATE TABLE addresses(guid TEXT); CREATE TABLE address_type_tokens(guid TEXT,type INTEGER,value TEXT);')
                db.execute('INSERT INTO addresses VALUES ("synthetic")')
                db.execute('INSERT INTO address_type_tokens VALUES ("synthetic",7,"Synthetic Person")')
            args = SimpleNamespace(arc_root=arc, zen_root=zen, runtime=base / 'runs', zen_app=Path('/Applications/Zen.app'))
            selection = choose(catalog(arc), ['Default'], ['none'], ['addresses'])
            scanned = scan(args, selection, probe=True)
            self.assertEqual(scanned.counts['addresses'], 1)
            native = Gecko.call
            def unsupported(browser, op, data):
                return {'unsupported': len(data)} if op == 'addresses' else native(browser, op, data)
            with patch.object(Gecko, 'call', unsupported):
                result = transfer(args, scanned, lambda: None)
            self.assertEqual(result['counts']['addresses'], 0)
            self.assertEqual(result['skipped']['addresses'], 1)
            Transaction(args.runtime, result['run']).rollback(False, lambda: None)
