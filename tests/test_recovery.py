import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arc_to_zen.cli import parser
from arc_to_zen.common import MigrationError, atomic_json
from arc_to_zen.recovery import Recovery, DemoRecovery
from arc_to_zen.transaction import MARKER, Transaction, tree_digest
from arc_to_zen.wizard import run


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runtime = root/'runs'; self.target = root/'zen'
        self.target.mkdir(); self.runtime.mkdir()
        (self.target/'profiles.ini').write_text('[General]\nVersion=2\n')
        (self.target/'tabs').write_text('original tabs')
        self.before = tree_digest(self.target)
        self.tx = Transaction(self.runtime, '20261024-test'); self.tx.directory.mkdir()
        shutil.copytree(self.target, self.tx.directory/'backup')
        (self.target/'tabs').write_text('migrated tabs')
        atomic_json(self.target/MARKER, {'run': self.tx.directory.name})
        self.tx.save(dict(version=1, run=self.tx.directory.name, target=str(self.target), phase='installed',
                          before=self.before, installed=tree_digest(self.target)))
        self.backend = Recovery(self.runtime, self.target, lambda: None)

    def preview(self): return self.backend.preview(self.tx.directory.name, lambda *args: None)

    def test_cli_rollback_rejects_changes_during_restore_copy(self):
        def emit(stage, *args):
            if stage == 'Restoring Zen':
                (self.target / 'late-tab').write_text('new tab')
        with patch('arc_to_zen.transaction.exchange', side_effect=AssertionError('No exchange')):
            with self.assertRaisesRegex(MigrationError, 'changed'):
                self.tx.rollback(True, lambda: None, emit=emit)
        self.assertEqual((self.target / 'tabs').read_text(), 'migrated tabs')
        self.assertEqual((self.target / 'late-tab').read_text(), 'new tab')

    def test_restore_retains_later_changes_and_backup(self):
        (self.target/'new-tab').write_text('created after migration')
        preview = self.preview(); self.assertTrue(preview['changed'])
        current = tree_digest(self.target)
        # Portable exchange for this test; production keeps macOS RENAME_SWAP.
        def swap(a, b):
            other = b.with_name('swap'); a.rename(other); b.rename(a); other.rename(b)
        with patch('arc_to_zen.transaction.exchange', side_effect=swap):
            result = self.backend.restore(preview, lambda *args: None)
        self.assertEqual(tree_digest(self.target), self.before)
        self.assertEqual(tree_digest(Path(result['retained'])), current)
        self.assertEqual(tree_digest(self.tx.directory/'backup'), self.before)
        self.assertEqual(self.tx.load()['phase'], 'rolled_back')
        self.assertEqual(self.backend.backups(), ([], 0))

    def test_rejects_change_after_confirmation_and_corrupt_backup(self):
        preview = self.preview(); self.assertFalse(preview['changed'])
        (self.target/'new-tab').write_text('changed while confirmation was open')
        current = tree_digest(self.target)
        with patch('arc_to_zen.transaction.exchange', side_effect=AssertionError('No exchange')):
            with self.assertRaisesRegex(MigrationError, 'changed'):
                self.backend.restore(preview, lambda *args: None)
        self.assertEqual(tree_digest(self.target), current)
        (self.tx.directory/'backup/tabs').write_text('corrupted')
        with self.assertRaisesRegex(MigrationError, 'integrity'): self.preview()

    def test_rejects_change_during_restore_preparation(self):
        preview = self.preview()
        def change(stage):
            if stage == 'Restoring Zen': (self.target/'new-tab').write_text('changed during copy')
        with patch('arc_to_zen.transaction.exchange', side_effect=AssertionError('No exchange')):
            with self.assertRaisesRegex(MigrationError, 'changed'):
                self.backend.restore(preview, change)
        self.assertEqual((self.target/'tabs').read_text(), 'migrated tabs')

    def test_journal_change_and_wrong_owner_are_rejected(self):
        preview = self.preview()
        data = self.tx.load(); data['phase'] = 'rolling_back'; self.tx.save(data)
        with self.assertRaisesRegex(MigrationError, 'journal changed'):
            self.backend.restore(preview, lambda *args: None)
        atomic_json(self.target/MARKER, {'run': 'newer-run'})
        with self.assertRaisesRegex(MigrationError, 'latest migration'): self.preview()


class RecoveryWizardTests(unittest.TestCase):
    def test_demo_restore_requires_confirmation_and_never_touches_real_data(self):
        from test_wizard import FakeUI
        entry = lambda options: options[1][1]
        FakeUI.actions = ['restore', entry, 'cancel', entry, 'restore', 'backup', 'finish']
        FakeUI.titles = []
        args = parser().parse_args(['wizard', '--demo', '--no-animation'])
        original = DemoRecovery.__init__
        def fast(instance): original(instance, delay=0)
        with patch('arc_to_zen.wizard.Terminal', FakeUI), patch.object(DemoRecovery, '__init__', fast), \
             patch('arc_to_zen.recovery.Recovery', side_effect=AssertionError('No real backups')), \
             patch('arc_to_zen.wizard.applications', side_effect=AssertionError('No discovery')), \
             patch('arc_to_zen.wizard.subprocess.run', side_effect=AssertionError('No browser close/open')):
            self.assertEqual(run(args), 0)
        self.assertEqual(FakeUI.titles.count('Confirm restore'), 2)
        self.assertEqual(FakeUI.titles.count('Restoring Zen'), 1)
        self.assertIn('Restore complete.', FakeUI.titles)

    def test_direct_restore_does_not_require_arc(self):
        from test_wizard import FakeUI
        FakeUI.actions = ['back']; FakeUI.titles = []
        args = parser().parse_args(['wizard', '--demo', '--restore', '--no-animation'])
        with patch('arc_to_zen.wizard.Terminal', FakeUI), \
             patch('arc_to_zen.wizard.detect', side_effect=AssertionError('No Arc requirement')):
            self.assertEqual(run(args), 0)
        self.assertEqual(FakeUI.titles, ['Restore from backup'])
